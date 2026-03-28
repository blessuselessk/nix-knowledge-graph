"""Ingest nix-index database file listings into TypeDB.

Uses nix-locate to find which packages provide which commands and files.
Downloads the pre-built database from nix-community/nix-index-database
if not already present.

Usage:
    python ingest/nix_index.py [system]

    system defaults to the current platform (e.g. aarch64-darwin).
"""

import os
import platform
import subprocess
import sys
from hashlib import sha256
from pathlib import Path

from typedb.driver import TypeDB, SessionType, TransactionType


TYPEDB_ADDRESS = os.environ.get("TYPEDB_ADDRESS", "localhost:1729")
DATABASE = os.environ.get("NKG_DATABASE", "nix-knowledge-graph")
DATA_DIR = Path(os.environ.get("NKG_DATA_DIR", "data"))
BATCH_SIZE = 50

NIX_INDEX_DB_URL = (
    "https://github.com/nix-community/nix-index-database"
    "/releases/latest/download/index-{system}"
)


def detect_system():
    """Detect the current Nix system identifier."""
    machine = platform.machine()
    system = platform.system().lower()

    arch_map = {"arm64": "aarch64", "x86_64": "x86_64", "aarch64": "aarch64"}
    os_map = {"darwin": "darwin", "linux": "linux"}

    arch = arch_map.get(machine, machine)
    os_name = os_map.get(system, system)
    return f"{arch}-{os_name}"


def ensure_database(system: str) -> Path:
    """Download the nix-index database if not present."""
    db_dir = DATA_DIR / "nix-index"
    db_dir.mkdir(parents=True, exist_ok=True)
    db_file = db_dir / f"index-{system}"

    if db_file.exists():
        print(f"  Using existing database at {db_file}")
        return db_dir

    url = NIX_INDEX_DB_URL.format(system=system)
    print(f"  Downloading nix-index database for {system}...")
    subprocess.run(["curl", "-L", "-o", str(db_file), url], check=True)
    return db_dir


def query_nix_locate(db_dir: Path, pattern: str, file_type: str = None):
    """Run nix-locate and parse results."""
    cmd = ["nix-locate", "--db", str(db_dir), "--top-level", "--regex", pattern]
    if file_type:
        cmd.extend(["--type", file_type])

    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print(f"  Warning: nix-locate failed: {result.stderr.strip()}")
        return

    for line in result.stdout.strip().split("\n"):
        if not line.strip():
            continue
        # Format: attr.path  size  type  /nix/store/hash-name/path
        parts = line.split()
        if len(parts) < 4:
            continue

        attr_path = parts[0]
        # Remove output suffix like ".out" or ".bin"
        if "." in attr_path:
            base, suffix = attr_path.rsplit(".", 1)
            if suffix in ("out", "bin", "dev", "lib", "man", "doc", "info"):
                attr_path = base

        file_path = parts[-1]
        yield attr_path, file_path


def escape(s: str) -> str:
    return s.replace("\\", "\\\\").replace('"', '\\"')


def ingest_commands(driver, db_dir: Path):
    """Find all executables in bin/ and create command entities + provides relations."""
    print("  Querying for commands (executables in bin/)...")
    commands = {}

    for attr_path, file_path in query_nix_locate(db_dir, r"/bin/[^/]+$", "x"):
        cmd_name = file_path.rsplit("/bin/", 1)[-1]
        if cmd_name and not cmd_name.startswith("."):
            if cmd_name not in commands:
                commands[cmd_name] = set()
            commands[cmd_name].add(attr_path)

    print(f"  Found {len(commands)} unique commands from {sum(len(v) for v in commands.values())} mappings")

    total = 0
    batch = []

    with driver.session(DATABASE, SessionType.DATA) as session:
        for cmd_name, attr_paths in commands.items():
            # Upsert command entity
            batch.append(
                f'insert $c isa command, has name "{escape(cmd_name)}";'
            )

            # Link to each providing package
            for attr_path in attr_paths:
                batch.append(
                    f'match $p isa package, has attr-path "{escape(attr_path)}"; '
                    f'$c isa command, has name "{escape(cmd_name)}"; '
                    f'insert (provider: $p, artifact: $c) isa provides;'
                )

            if len(batch) >= BATCH_SIZE:
                _flush(session, batch)
                total += len(batch)
                print(f"  {total} queries...", end="\r")
                batch = []

        if batch:
            _flush(session, batch)
            total += len(batch)

    print(f"  Ingested {len(commands)} commands")


def ingest_file_entries(driver, db_dir: Path):
    """Find notable file entries (libraries, man pages) and create file-entry entities."""
    print("  Querying for libraries and man pages...")

    entries = []

    # Shared libraries
    for attr_path, file_path in query_nix_locate(db_dir, r"/lib/[^/]+\.so", "r"):
        entries.append((attr_path, file_path))

    # Man pages
    for attr_path, file_path in query_nix_locate(db_dir, r"/share/man/.*\.[0-9]", "r"):
        entries.append((attr_path, file_path))

    print(f"  Found {len(entries)} file entries")

    total = 0
    batch = []

    with driver.session(DATABASE, SessionType.DATA) as session:
        for attr_path, file_path in entries:
            fid = sha256(file_path.encode()).hexdigest()[:16]
            batch.append(
                f'insert $f isa file-entry, has file-path "{escape(file_path)}";'
            )
            batch.append(
                f'match $p isa package, has attr-path "{escape(attr_path)}"; '
                f'$f isa file-entry, has file-path "{escape(file_path)}"; '
                f'insert (provider: $p, artifact: $f) isa provides;'
            )

            if len(batch) >= BATCH_SIZE:
                _flush(session, batch)
                total += len(batch)
                print(f"  {total} queries...", end="\r")
                batch = []

        if batch:
            _flush(session, batch)
            total += len(batch)

    print(f"  Ingested {len(entries)} file entries")


def _flush(session, batch):
    with session.transaction(TransactionType.WRITE) as tx:
        for q in batch:
            try:
                tx.query(q)
            except Exception:
                pass  # duplicates, missing refs
        tx.commit()
    batch.clear()


def main():
    system = sys.argv[1] if len(sys.argv) > 1 else detect_system()

    print(f"System: {system}")
    db_dir = ensure_database(system)

    # Verify nix-locate is available
    if not any(
        (Path(p) / "nix-locate").exists()
        for p in os.environ.get("PATH", "").split(":")
    ):
        print("nix-locate not found. Install nix-index or add it to your PATH.")
        sys.exit(1)

    print(f"Connecting to TypeDB at {TYPEDB_ADDRESS}...")
    with TypeDB.core_driver(TYPEDB_ADDRESS) as driver:
        ingest_commands(driver, db_dir)
        ingest_file_entries(driver, db_dir)

    print("Done.")


if __name__ == "__main__":
    main()
