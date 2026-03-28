"""Ingest cheat.sh community cheatsheets into TypeDB.

Parses sheets from the cheat.sheets repository. Each sheet is a plain text
file with comments (# lines) as descriptions and bare lines as commands.

Usage:
    python ingest/cheat.py [cheat-sheets-dir]

If no dir given, clones the cheat.sheets repository.
"""

import os
import subprocess
import sys
from hashlib import sha256
from pathlib import Path

from typedb.driver import TypeDB, SessionType, TransactionType


TYPEDB_ADDRESS = os.environ.get("TYPEDB_ADDRESS", "localhost:1729")
DATABASE = os.environ.get("NKG_DATABASE", "nix-knowledge-graph")
DATA_DIR = Path(os.environ.get("NKG_DATA_DIR", "data"))
CHEAT_REPO = "https://github.com/chubin/cheat.sheets.git"
BATCH_SIZE = 50


def ensure_cheat_sheets(data_dir: Path) -> Path:
    sheets_dir = data_dir / "cheat.sheets"
    if (sheets_dir / "sheets").exists():
        print(f"  Using existing sheets at {sheets_dir}")
        return sheets_dir / "sheets" / "_default"

    print(f"  Cloning cheat.sheets to {sheets_dir}...")
    subprocess.run(
        ["git", "clone", "--depth=1", CHEAT_REPO, str(sheets_dir)],
        check=True,
    )
    return sheets_dir / "sheets" / "_default"


def parse_cheat_sheet(path: Path):
    """Parse a cheat.sh sheet into examples.

    Format:
        # Description of the next command
        command --with flags

        # Another description
        # with multiple lines
        another-command
    """
    text = path.read_text(errors="replace")
    lines = text.strip().split("\n")

    command_name = path.name
    examples = []
    desc_lines = []

    for line in lines:
        stripped = line.strip()

        # Skip sheet-level metadata comments
        if stripped.startswith("##"):
            continue

        if stripped.startswith("#"):
            desc_lines.append(stripped.lstrip("# ").strip())
        elif stripped:
            desc = " ".join(desc_lines).strip()
            examples.append({
                "description": desc,
                "code": stripped,
            })
            desc_lines = []
        else:
            # Blank line resets description
            if not desc_lines:
                continue

    return {
        "command": command_name,
        "examples": examples,
    }


def escape(s: str) -> str:
    return s.replace("\\", "\\\\").replace('"', '\\"')


def ingest_cheat_sheets(driver, sheets_dir: Path):
    """Ingest cheat sheets into TypeDB."""
    sheet_files = [f for f in sheets_dir.iterdir() if f.is_file() and not f.name.startswith(".")]
    print(f"  Found {len(sheet_files)} cheat sheets")

    total_commands = 0
    total_examples = 0
    batch = []

    with driver.session(DATABASE, SessionType.DATA) as session:
        for sheet_path in sheet_files:
            parsed = parse_cheat_sheet(sheet_path)
            if not parsed["examples"]:
                continue

            cmd_name = parsed["command"]

            # Upsert command
            batch.append(
                f'insert $c isa command, has name "{escape(cmd_name)}";'
            )
            total_commands += 1

            for ex in parsed["examples"]:
                ex_id = sha256(
                    f"cheat:{cmd_name}:{ex['code']}".encode()
                ).hexdigest()[:16]

                parts = [
                    f'has example-id "{ex_id}"',
                    f'has source-type "cheat.sh"',
                    f'has code "{escape(ex["code"])}"',
                ]
                if ex["description"]:
                    parts.append(
                        f'has example-description "{escape(ex["description"][:500])}"'
                    )

                batch.append(f'insert $ex isa example, {", ".join(parts)};')

                batch.append(
                    f'match $c isa command, has name "{escape(cmd_name)}"; '
                    f'$ex isa example, has example-id "{ex_id}"; '
                    f'insert (demonstration: $ex, subject: $c) isa demonstrates;'
                )
                total_examples += 1

            if len(batch) >= BATCH_SIZE:
                _flush(session, batch)
                print(f"  {total_commands} commands, {total_examples} examples...", end="\r")

        if batch:
            _flush(session, batch)

    print(f"  {total_commands} commands, {total_examples} examples total")

    _link_commands_to_packages(driver)


def _link_commands_to_packages(driver):
    """Link cheat.sh commands to packages by name match."""
    print("  Linking commands to packages...")
    linked = 0

    with driver.session(DATABASE, SessionType.DATA) as session:
        with session.transaction(TransactionType.READ) as tx:
            results = tx.query("match $c isa command, has name $n; select $n;")
            names = []
            for row in results:
                names.append(row.get("n").as_attribute().get_value())

        batch = []
        for name in names:
            batch.append(
                f'match $p isa package, has pname "{escape(name)}"; '
                f'$c isa command, has name "{escape(name)}"; '
                f'insert (provider: $p, artifact: $c) isa provides;'
            )
            if len(batch) >= BATCH_SIZE:
                with session.transaction(TransactionType.WRITE) as tx:
                    for q in batch:
                        try:
                            tx.query(q)
                            linked += 1
                        except Exception:
                            pass
                    tx.commit()
                batch = []

        if batch:
            with session.transaction(TransactionType.WRITE) as tx:
                for q in batch:
                    try:
                        tx.query(q)
                        linked += 1
                    except Exception:
                        pass
                tx.commit()

    print(f"  Linked {linked} commands to packages")


def _flush(session, batch):
    with session.transaction(TransactionType.WRITE) as tx:
        for q in batch:
            try:
                tx.query(q)
            except Exception:
                pass
        tx.commit()
    batch.clear()


def main():
    data_dir = DATA_DIR
    if len(sys.argv) > 1:
        sheets_dir = Path(sys.argv[1])
    else:
        sheets_dir = ensure_cheat_sheets(data_dir)

    if not sheets_dir.exists():
        print(f"Sheets directory not found: {sheets_dir}")
        sys.exit(1)

    print(f"Connecting to TypeDB at {TYPEDB_ADDRESS}...")
    with TypeDB.core_driver(TYPEDB_ADDRESS) as driver:
        ingest_cheat_sheets(driver, sheets_dir)

    print("Done.")


if __name__ == "__main__":
    main()
