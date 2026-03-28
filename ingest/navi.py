"""Ingest navi cheatsheets into TypeDB.

Parses .cheat files from denisidoro/cheats and community repos.

Navi .cheat format:
    % tag1, tag2

    # Description of the command
    command --flag <arg>

    $ arg: possible-values

Usage:
    python ingest/navi.py [cheats-dir]

If no dir given, clones the default cheats repository.
"""

import os
import re
import subprocess
import sys
from hashlib import sha256
from pathlib import Path

from typedb.driver import TypeDB, SessionType, TransactionType


TYPEDB_ADDRESS = os.environ.get("TYPEDB_ADDRESS", "localhost:1729")
DATABASE = os.environ.get("NKG_DATABASE", "nix-knowledge-graph")
DATA_DIR = Path(os.environ.get("NKG_DATA_DIR", "data"))
CHEATS_REPO = "https://github.com/denisidoro/cheats.git"
BATCH_SIZE = 50


def ensure_cheats(data_dir: Path) -> Path:
    cheats_dir = data_dir / "navi-cheats"
    if cheats_dir.exists():
        print(f"  Using existing cheats at {cheats_dir}")
        return cheats_dir

    print(f"  Cloning navi cheats to {cheats_dir}...")
    subprocess.run(
        ["git", "clone", "--depth=1", CHEATS_REPO, str(cheats_dir)],
        check=True,
    )
    return cheats_dir


def parse_cheat_file(path: Path):
    """Parse a .cheat file into tagged examples.

    Returns list of examples with tags, descriptions, and commands.
    """
    text = path.read_text(errors="replace")
    lines = text.strip().split("\n")

    current_tags = []
    examples = []
    current_desc = None

    for line in lines:
        stripped = line.strip()

        # Tag line: % git, branch
        if stripped.startswith("%"):
            current_tags = [t.strip() for t in stripped[1:].split(",") if t.strip()]
            continue

        # Description: # Do something
        if stripped.startswith("#"):
            current_desc = stripped.lstrip("# ").strip()
            continue

        # Variable definition: $ var: command
        if stripped.startswith("$"):
            continue

        # Semicolon-only or empty lines
        if not stripped or stripped == ";":
            continue

        # Command line
        cmd_line = stripped

        # Extract the base command name (first word)
        base_cmd = cmd_line.split()[0] if cmd_line.split() else None
        # Remove any variable placeholders for the base command
        if base_cmd and base_cmd.startswith("<"):
            base_cmd = None

        examples.append({
            "tags": list(current_tags),
            "description": current_desc or "",
            "code": cmd_line,
            "base_command": base_cmd,
        })
        current_desc = None

    return examples


def escape(s: str) -> str:
    return s.replace("\\", "\\\\").replace('"', '\\"')


def ingest_navi_cheats(driver, cheats_dir: Path):
    """Ingest navi cheatsheets into TypeDB."""
    cheat_files = list(cheats_dir.rglob("*.cheat"))
    print(f"  Found {len(cheat_files)} .cheat files")

    total_examples = 0
    commands_seen = set()
    batch = []

    with driver.session(DATABASE, SessionType.DATA) as session:
        for cheat_file in cheat_files:
            examples = parse_cheat_file(cheat_file)

            for ex in examples:
                base_cmd = ex["base_command"]
                if not base_cmd:
                    continue

                # Upsert command
                if base_cmd not in commands_seen:
                    batch.append(
                        f'insert $c isa command, has name "{escape(base_cmd)}";'
                    )
                    commands_seen.add(base_cmd)

                ex_id = sha256(
                    f"navi:{base_cmd}:{ex['code']}".encode()
                ).hexdigest()[:16]

                parts = [
                    f'has example-id "{ex_id}"',
                    f'has source-type "navi"',
                    f'has code "{escape(ex["code"])}"',
                ]
                if ex["description"]:
                    parts.append(
                        f'has example-description "{escape(ex["description"][:500])}"'
                    )
                if ex["tags"]:
                    for tag in ex["tags"][:5]:
                        parts.append(f'has tags "{escape(tag)}"')

                batch.append(f'insert $ex isa example, {", ".join(parts)};')

                batch.append(
                    f'match $c isa command, has name "{escape(base_cmd)}"; '
                    f'$ex isa example, has example-id "{ex_id}"; '
                    f'insert (demonstration: $ex, subject: $c) isa demonstrates;'
                )

                # If the command references other commands in the pipeline,
                # create demonstrates relations for those too
                pipe_cmds = _extract_pipe_commands(ex["code"])
                for pipe_cmd in pipe_cmds:
                    if pipe_cmd != base_cmd and pipe_cmd not in commands_seen:
                        batch.append(
                            f'insert $c2 isa command, has name "{escape(pipe_cmd)}";'
                        )
                        commands_seen.add(pipe_cmd)
                    if pipe_cmd != base_cmd:
                        batch.append(
                            f'match $c2 isa command, has name "{escape(pipe_cmd)}"; '
                            f'$ex isa example, has example-id "{ex_id}"; '
                            f'insert (demonstration: $ex, subject: $c2) isa demonstrates;'
                        )

                total_examples += 1

                if len(batch) >= BATCH_SIZE:
                    _flush(session, batch)
                    print(
                        f"  {len(commands_seen)} commands, {total_examples} examples...",
                        end="\r",
                    )

        if batch:
            _flush(session, batch)

    print(f"  {len(commands_seen)} commands, {total_examples} examples total")


def _extract_pipe_commands(code: str):
    """Extract command names from a pipeline."""
    cmds = set()
    # Split on pipes, &&, ||, ;
    segments = re.split(r'\||\&\&|\|\||;', code)
    for seg in segments:
        seg = seg.strip()
        if not seg:
            continue
        # First word is the command (skip variable placeholders)
        word = seg.split()[0] if seg.split() else ""
        if word and not word.startswith("<") and not word.startswith("$"):
            # Strip path prefix
            word = word.rsplit("/", 1)[-1]
            if re.match(r'^[a-zA-Z][\w.-]*$', word):
                cmds.add(word)
    return cmds


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
        cheats_dir = Path(sys.argv[1])
    else:
        cheats_dir = ensure_cheats(data_dir)

    if not cheats_dir.exists():
        print(f"Cheats directory not found: {cheats_dir}")
        sys.exit(1)

    print(f"Connecting to TypeDB at {TYPEDB_ADDRESS}...")
    with TypeDB.core_driver(TYPEDB_ADDRESS) as driver:
        ingest_navi_cheats(driver, cheats_dir)

    print("Done.")


if __name__ == "__main__":
    main()
