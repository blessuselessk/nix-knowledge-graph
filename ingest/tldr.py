"""Ingest tldr-pages into TypeDB.

Parses tldr markdown pages and creates command + example entities,
then links them to existing packages via the provides relation.

Usage:
    python ingest/tldr.py [path-to-tldr-pages]

If no path given, clones tldr-pages/tldr from GitHub.
"""

import os
import re
import sys
import subprocess
from pathlib import Path
from hashlib import sha256

from typedb.driver import TypeDB, SessionType, TransactionType


TYPEDB_ADDRESS = os.environ.get("TYPEDB_ADDRESS", "localhost:1729")
DATABASE = os.environ.get("NKG_DATABASE", "nix-knowledge-graph")
TLDR_REPO = "https://github.com/tldr-pages/tldr.git"
TLDR_DIR = Path(os.environ.get("NKG_DATA_DIR", "data")) / "tldr"
BATCH_SIZE = 50


def ensure_tldr_pages(tldr_dir: Path) -> Path:
    """Clone or update tldr-pages."""
    pages_dir = tldr_dir / "pages"
    if pages_dir.exists():
        print(f"Using existing tldr pages at {tldr_dir}")
        return pages_dir

    print(f"Cloning tldr-pages to {tldr_dir}...")
    subprocess.run(
        ["git", "clone", "--depth=1", TLDR_REPO, str(tldr_dir)],
        check=True,
    )
    return pages_dir


def parse_tldr_page(path: Path):
    """Parse a tldr markdown page into command name + examples.

    tldr format:
        # command-name
        > Description of the command.
        > More info: <url>

        - Example description:

        `command --flag arg`
    """
    text = path.read_text(encoding="utf-8", errors="replace")
    lines = text.strip().split("\n")

    command_name = None
    description_lines = []
    examples = []

    current_desc = None

    for line in lines:
        line = line.strip()

        # Command name
        if line.startswith("# "):
            command_name = line[2:].strip()

        # Description line
        elif line.startswith("> "):
            desc = line[2:].strip()
            if not desc.startswith("More info:"):
                description_lines.append(desc)

        # Example description
        elif line.startswith("- ") and line.endswith(":"):
            current_desc = line[2:-1].strip()

        # Example code
        elif line.startswith("`") and line.endswith("`"):
            code = line[1:-1]
            examples.append({
                "description": current_desc or "",
                "code": code,
            })
            current_desc = None

    if not command_name:
        return None

    return {
        "command": command_name,
        "synopsis": " ".join(description_lines),
        "examples": examples,
    }


def escape(s: str) -> str:
    """Escape a string for TypeQL."""
    return s.replace("\\", "\\\\").replace('"', '\\"')


def ingest_tldr(driver, pages_dir: Path):
    """Ingest parsed tldr pages into TypeDB."""
    # Collect all pages from linux, common, osx dirs
    page_files = []
    for platform in ["common", "linux", "osx"]:
        platform_dir = pages_dir / platform
        if platform_dir.exists():
            page_files.extend(platform_dir.glob("*.md"))

    print(f"  Found {len(page_files)} tldr pages")

    total_commands = 0
    total_examples = 0
    batch = []

    with driver.session(DATABASE, SessionType.DATA) as session:
        for page_file in page_files:
            parsed = parse_tldr_page(page_file)
            if not parsed or not parsed["examples"]:
                continue

            cmd_name = parsed["command"]
            synopsis = parsed["synopsis"]

            # Upsert command entity
            query = (
                f'match $c isa command, has name "{escape(cmd_name)}"; '
                f'insert $c has synopsis "{escape(synopsis)}";'
            )
            # Try match first; if no match, insert fresh
            insert_query = (
                f'insert $c isa command, '
                f'has name "{escape(cmd_name)}", '
                f'has synopsis "{escape(synopsis)}";'
            )
            batch.append(("upsert_command", cmd_name, insert_query))
            total_commands += 1

            # Insert examples
            for ex in parsed["examples"]:
                ex_id = sha256(
                    f"tldr:{cmd_name}:{ex['code']}".encode()
                ).hexdigest()[:16]

                ex_query = (
                    f'insert $ex isa example, '
                    f'has example-id "{ex_id}", '
                    f'has source-type "tldr", '
                    f'has code "{escape(ex["code"])}", '
                    f'has example-description "{escape(ex["description"])}";'
                )
                batch.append(("insert_example", ex_id, ex_query))

                # Link example to command
                link_query = (
                    f'match $c isa command, has name "{escape(cmd_name)}"; '
                    f'$ex isa example, has example-id "{ex_id}"; '
                    f'insert (demonstration: $ex, subject: $c) isa demonstrates;'
                )
                batch.append(("link", None, link_query))
                total_examples += 1

            if len(batch) >= BATCH_SIZE:
                _flush_batch(session, batch)
                print(
                    f"  {total_commands} commands, {total_examples} examples...",
                    end="\r",
                )
                batch = []

        if batch:
            _flush_batch(session, batch)

    print(f"  {total_commands} commands, {total_examples} examples total")

    # Link commands to packages where we can
    _link_commands_to_packages(driver)


def _flush_batch(session, batch):
    """Execute a batch of queries."""
    with session.transaction(TransactionType.WRITE) as tx:
        for kind, id_, query in batch:
            try:
                tx.query(query)
            except Exception as e:
                if "already exists" not in str(e) and kind != "upsert_command":
                    pass  # skip duplicates silently
        tx.commit()


def _link_commands_to_packages(driver):
    """Link command entities to packages by matching command name to package pname."""
    print("  Linking commands to packages...")
    linked = 0

    with driver.session(DATABASE, SessionType.DATA) as session:
        # Get all commands
        with session.transaction(TransactionType.READ) as tx:
            results = tx.query("match $c isa command, has name $n; select $n;")
            command_names = []
            for row in results:
                command_names.append(row.get("n").as_attribute().get_value())

        # For each command, try to find a package with matching pname
        batch = []
        for cmd_name in command_names:
            query = (
                f'match $p isa package, has pname "{escape(cmd_name)}"; '
                f'$c isa command, has name "{escape(cmd_name)}"; '
                f'insert (provider: $p, artifact: $c) isa provides;'
            )
            batch.append(query)

            if len(batch) >= BATCH_SIZE:
                with session.transaction(TransactionType.WRITE) as tx:
                    for q in batch:
                        try:
                            tx.query(q)
                            linked += 1
                        except Exception:
                            pass  # no matching package
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


def main():
    tldr_dir = Path(sys.argv[1]) if len(sys.argv) > 1 else TLDR_DIR
    pages_dir = ensure_tldr_pages(tldr_dir)

    print(f"Connecting to TypeDB at {TYPEDB_ADDRESS}...")
    with TypeDB.core_driver(TYPEDB_ADDRESS) as driver:
        print("Ingesting tldr pages...")
        ingest_tldr(driver, pages_dir)

    print("Done.")


if __name__ == "__main__":
    main()
