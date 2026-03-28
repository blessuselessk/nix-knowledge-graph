"""Ingest man pages into TypeDB.

Parses man pages (troff/groff format) into sections, extracts flags
from OPTIONS, and creates man-page, man-section, and flag entities.

Can read man pages from:
  - A directory of man page files (.1, .1.gz, etc.)
  - Nix store paths discovered via nix-locate

Usage:
    python ingest/man_pages.py [man-pages-dir]

If no dir given, attempts to find man pages from common locations.
"""

import gzip
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
BATCH_SIZE = 30


def find_man_pages(search_dir: Path):
    """Find man page files recursively."""
    for ext in ("*.1", "*.1.gz", "*.2", "*.2.gz", "*.3", "*.3.gz",
                "*.5", "*.5.gz", "*.7", "*.7.gz", "*.8", "*.8.gz"):
        yield from search_dir.rglob(ext)


def read_man_page(path: Path) -> str:
    """Read a man page file, handling gzip compression."""
    if path.suffix == ".gz":
        with gzip.open(path, "rt", errors="replace") as f:
            return f.read()
    else:
        return path.read_text(errors="replace")


def parse_troff(raw: str):
    """Parse troff/groff man page into sections.

    Returns dict with command name, section number, and list of sections.
    """
    lines = raw.split("\n")
    command_name = None
    section_num = None
    sections = []
    current_section = None
    current_content = []

    for line in lines:
        # .TH command section date ...
        th_match = re.match(r'\.TH\s+"?(\S+?)"?\s+"?(\d)"?', line, re.IGNORECASE)
        if th_match:
            command_name = th_match.group(1).lower()
            section_num = th_match.group(2)
            continue

        # .SH "SECTION NAME" or .SH SECTION NAME
        sh_match = re.match(r'\.SH\s+"?(.+?)"?\s*$', line)
        if sh_match:
            if current_section:
                sections.append({
                    "heading": current_section,
                    "content": _clean_troff("\n".join(current_content)),
                })
            current_section = sh_match.group(1).strip().upper()
            current_content = []
            continue

        # .SS subsection — fold into current section
        if line.startswith(".SS"):
            current_content.append(line[3:].strip())
            continue

        # Skip troff directives, keep text
        if line.startswith("."):
            # .B, .I, .BI etc. — keep the text argument
            text_match = re.match(r'\.\w+\s+(.*)', line)
            if text_match:
                current_content.append(text_match.group(1))
        else:
            current_content.append(line)

    # Last section
    if current_section:
        sections.append({
            "heading": current_section,
            "content": _clean_troff("\n".join(current_content)),
        })

    return {
        "command": command_name,
        "section_num": section_num or "1",
        "sections": sections,
    }


def _clean_troff(text: str) -> str:
    """Remove remaining troff formatting from text."""
    text = re.sub(r'\\f[BIRP]', '', text)  # font changes
    text = re.sub(r'\\-', '-', text)
    text = re.sub(r'\\&', '', text)
    text = re.sub(r'\\e', '\\', text)
    text = re.sub(r'\\s[+-]?\d+', '', text)  # size changes
    text = re.sub(r'\\n\(\w+', '', text)  # number registers
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text.strip()


def extract_flags(options_text: str):
    """Extract flags from an OPTIONS section."""
    flags = []
    # Match patterns like: -f, --flag, -f WORD, --flag=WORD
    for match in re.finditer(
        r'(?:^|\n)\s*((?:-\w(?:,\s*)?)?(?:--[\w-]+)(?:\s*[=\s]\s*\w+)?)\s*\n?\s*(.*?)(?=\n\s*(?:-\w|--\w)|\Z)',
        options_text,
        re.DOTALL,
    ):
        flag_str = match.group(1).strip()
        desc = match.group(2).strip()
        if not flag_str:
            continue

        short = None
        long = None
        for part in re.split(r'[,\s]+', flag_str):
            part = part.strip()
            if part.startswith("--"):
                long = part.split("=")[0].split()[0]
            elif part.startswith("-") and len(part) == 2:
                short = part

        flags.append({
            "name": long or short or flag_str,
            "short": short,
            "description": desc[:500],
        })

    return flags


def escape(s: str) -> str:
    return s.replace("\\", "\\\\").replace('"', '\\"')


def ingest_man_pages(driver, man_dir: Path):
    """Parse and ingest man pages from a directory."""
    pages = list(find_man_pages(man_dir))
    print(f"  Found {len(pages)} man page files")

    total = 0
    batch = []

    with driver.session(DATABASE, SessionType.DATA) as session:
        for page_path in pages:
            try:
                raw = read_man_page(page_path)
            except Exception as e:
                continue

            parsed = parse_troff(raw)
            cmd_name = parsed["command"]
            if not cmd_name or len(cmd_name) > 100:
                continue

            sec_num = parsed["section_num"]
            man_id = f"{cmd_name}.{sec_num}"

            # Create man-page entity
            raw_preview = raw[:5000]
            batch.append(
                f'insert $m isa man-page, '
                f'has man-id "{escape(man_id)}", '
                f'has section-number "{escape(sec_num)}", '
                f'has raw-content "{escape(raw_preview)}";'
            )

            # Link to command
            batch.append(
                f'match $c isa command, has name "{escape(cmd_name)}"; '
                f'$m isa man-page, has man-id "{escape(man_id)}"; '
                f'insert (documentation: $m, subject: $c) isa documents;'
            )

            # Create man-section entities for key sections
            for sec in parsed["sections"]:
                heading = sec["heading"]
                content = sec["content"][:3000]
                if not content.strip():
                    continue

                sec_id = sha256(f"{man_id}:{heading}".encode()).hexdigest()[:16]
                sec_type = _classify_section(heading)

                batch.append(
                    f'insert $s isa man-section, '
                    f'has section-id "{sec_id}", '
                    f'has heading "{escape(heading)}", '
                    f'has content "{escape(content)}", '
                    f'has section-type "{sec_type}";'
                )
                batch.append(
                    f'match $m isa man-page, has man-id "{escape(man_id)}"; '
                    f'$s isa man-section, has section-id "{sec_id}"; '
                    f'insert (whole: $m, part: $s) isa has-section;'
                )

                # Extract flags from OPTIONS
                if heading in ("OPTIONS", "FLAGS"):
                    for flag in extract_flags(content):
                        fid = sha256(
                            f"{man_id}:{flag['name']}".encode()
                        ).hexdigest()[:16]
                        flag_parts = [
                            f'has flag-id "{fid}"',
                            f'has flag-name "{escape(flag["name"])}"',
                        ]
                        if flag.get("short"):
                            flag_parts.append(
                                f'has short-form "{escape(flag["short"])}"'
                            )
                        if flag.get("description"):
                            flag_parts.append(
                                f'has flag-description "{escape(flag["description"])}"'
                            )

                        batch.append(
                            f'insert $fl isa flag, {", ".join(flag_parts)};'
                        )
                        batch.append(
                            f'match $c isa command, has name "{escape(cmd_name)}"; '
                            f'$fl isa flag, has flag-id "{fid}"; '
                            f'insert (owner: $c, option: $fl) isa has-flag;'
                        )

            if len(batch) >= BATCH_SIZE:
                _flush(session, batch)
                total += 1
                print(f"  {total} man pages...", end="\r")

        if batch:
            _flush(session, batch)
            total += 1

    print(f"  Ingested {total} man pages")


def _classify_section(heading: str) -> str:
    heading = heading.upper()
    if heading in ("NAME",):
        return "name"
    elif heading in ("SYNOPSIS", "SYNTAX"):
        return "synopsis"
    elif heading in ("DESCRIPTION", "OVERVIEW"):
        return "description"
    elif heading in ("OPTIONS", "FLAGS", "SWITCHES"):
        return "options"
    elif heading in ("EXAMPLES", "EXAMPLE", "USAGE"):
        return "examples"
    elif heading in ("SEE ALSO", "REFERENCES"):
        return "see_also"
    elif heading in ("EXIT STATUS", "RETURN VALUE", "RETURN VALUES"):
        return "exit_status"
    elif heading in ("ENVIRONMENT", "ENVIRONMENT VARIABLES"):
        return "environment"
    elif heading in ("FILES",):
        return "files"
    elif heading in ("BUGS", "KNOWN ISSUES"):
        return "bugs"
    elif heading in ("AUTHORS", "AUTHOR"):
        return "author"
    else:
        return "other"


def _flush(session, batch):
    with session.transaction(TransactionType.WRITE) as tx:
        for q in batch:
            try:
                tx.query(q)
            except Exception:
                pass
        tx.commit()
    batch.clear()


def find_default_man_dir():
    """Try to find man pages from common locations."""
    candidates = [
        Path("/run/current-system/sw/share/man"),       # NixOS
        Path.home() / ".nix-profile/share/man",          # nix profile
        Path("/usr/share/man"),                           # traditional
        Path("/usr/local/share/man"),                     # macOS Homebrew
    ]
    for d in candidates:
        if d.exists():
            return d
    return None


def main():
    if len(sys.argv) > 1:
        man_dir = Path(sys.argv[1])
    else:
        man_dir = find_default_man_dir()

    if not man_dir or not man_dir.exists():
        print("No man pages directory found.")
        print("Usage: python ingest/man_pages.py <man-pages-dir>")
        print("Try: /usr/share/man, ~/.nix-profile/share/man, etc.")
        sys.exit(1)

    print(f"Man pages directory: {man_dir}")
    print(f"Connecting to TypeDB at {TYPEDB_ADDRESS}...")

    with TypeDB.core_driver(TYPEDB_ADDRESS) as driver:
        ingest_man_pages(driver, man_dir)

    print("Done.")


if __name__ == "__main__":
    main()
