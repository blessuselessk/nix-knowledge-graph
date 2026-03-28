"""Ingest rippkgs SQLite index into TypeDB.

Usage:
    1. Generate the rippkgs index:
       rippkgs-index nixpkgs -o data/rippkgs-index.sqlite

    2. Start TypeDB:
       docker compose up -d

    3. Run this ingester:
       python ingest/rippkgs.py
"""

import sqlite3
import json
import os
import sys
from pathlib import Path

from typedb.driver import TypeDB, SessionType, TransactionType


TYPEDB_ADDRESS = os.environ.get("TYPEDB_ADDRESS", "localhost:1729")
DATABASE = os.environ.get("NKG_DATABASE", "nix-knowledge-graph")
SCHEMA_DIR = Path(os.environ.get("NKG_SCHEMA_DIR", "schema"))
RIPPKGS_DB = Path(os.environ.get("NKG_RIPPKGS_DB", "data/rippkgs-index.sqlite"))
BATCH_SIZE = 50


def create_database(driver):
    """Create the database and load schema."""
    if driver.databases.contains(DATABASE):
        print(f"Database '{DATABASE}' already exists, dropping...")
        driver.databases.get(DATABASE).delete()

    driver.databases.create(DATABASE)
    print(f"Created database '{DATABASE}'")

    # Load schema
    with driver.session(DATABASE, SessionType.SCHEMA) as session:
        for schema_file in [SCHEMA_DIR / "types.tql", SCHEMA_DIR / "rules.tql"]:
            path = schema_file
            if not path.exists():
                print(f"  Skipping {schema_file} (not found)")
                continue
            tql = path.read_text()
            with session.transaction(TransactionType.WRITE) as tx:
                tx.query(tql)
                tx.commit()
            print(f"  Loaded {schema_file}")


def read_rippkgs(db_path: Path):
    """Read packages from the rippkgs SQLite index."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    cursor = conn.execute("SELECT * FROM packages")

    for row in cursor:
        pkg = dict(row)
        # storePaths and build inputs are JSON strings
        if pkg.get("storePaths"):
            try:
                pkg["storePaths"] = json.loads(pkg["storePaths"])
            except (json.JSONDecodeError, TypeError):
                pkg["storePaths"] = {}
        if pkg.get("propagatedBuildInputs"):
            try:
                pkg["propagatedBuildInputs"] = json.loads(pkg["propagatedBuildInputs"])
            except (json.JSONDecodeError, TypeError):
                pkg["propagatedBuildInputs"] = []
        if pkg.get("propagatedNativeBuildInputs"):
            try:
                pkg["propagatedNativeBuildInputs"] = json.loads(
                    pkg["propagatedNativeBuildInputs"]
                )
            except (json.JSONDecodeError, TypeError):
                pkg["propagatedNativeBuildInputs"] = []
        yield pkg

    conn.close()


def escape(s: str) -> str:
    """Escape a string for TypeQL."""
    return s.replace("\\", "\\\\").replace('"', '\\"')


def ingest_packages(driver, db_path: Path):
    """Ingest packages from rippkgs into TypeDB."""
    total = 0
    batch = []

    with driver.session(DATABASE, SessionType.DATA) as session:
        for pkg in read_rippkgs(db_path):
            attr = pkg["attribute"]
            if not attr:
                continue

            # Build the insert query for this package
            attrs = [f'$p has attr-path "{escape(attr)}"']

            if pkg.get("name"):
                attrs.append(f'$p has pname "{escape(pkg["name"])}"')
            if pkg.get("version"):
                attrs.append(f'$p has version "{escape(pkg["version"])}"')
            if pkg.get("description"):
                attrs.append(f'$p has description "{escape(pkg["description"])}"')
            if pkg.get("long_description"):
                desc = pkg["long_description"][:2000]  # truncate very long descriptions
                attrs.append(f'$p has long-description "{escape(desc)}"')

            # Store paths (e.g. {"out": "/nix/store/..."})
            store_paths = pkg.get("storePaths") or {}
            for output_name, path in store_paths.items():
                if path:
                    attrs.append(f'$p has store-path "{escape(path)}"')

            query = "insert $p isa package, " + ", ".join(attrs) + ";"
            batch.append(query)

            if len(batch) >= BATCH_SIZE:
                with session.transaction(TransactionType.WRITE) as tx:
                    for q in batch:
                        tx.query(q)
                    tx.commit()
                total += len(batch)
                print(f"  Ingested {total} packages...", end="\r")
                batch = []

        # Flush remaining
        if batch:
            with session.transaction(TransactionType.WRITE) as tx:
                for q in batch:
                    tx.query(q)
                tx.commit()
            total += len(batch)

    print(f"  Ingested {total} packages total")
    return total


def ingest_dependencies(driver, db_path: Path):
    """Ingest dependency relations from rippkgs into TypeDB."""
    total = 0
    batch = []

    with driver.session(DATABASE, SessionType.DATA) as session:
        for pkg in read_rippkgs(db_path):
            attr = pkg["attribute"]
            if not attr:
                continue

            deps = set()
            for dep in pkg.get("propagatedBuildInputs") or []:
                deps.add(dep)
            for dep in pkg.get("propagatedNativeBuildInputs") or []:
                deps.add(dep)

            for dep in deps:
                query = (
                    f'match $p isa package, has attr-path "{escape(attr)}"; '
                    f'$d isa package, has attr-path "{escape(dep)}"; '
                    f"insert (dependent: $p, dependency: $d) isa depends-on;"
                )
                batch.append(query)

                if len(batch) >= BATCH_SIZE:
                    with session.transaction(TransactionType.WRITE) as tx:
                        for q in batch:
                            try:
                                tx.query(q)
                            except Exception:
                                pass  # dependency target may not exist
                        tx.commit()
                    total += len(batch)
                    print(f"  Linked {total} dependencies...", end="\r")
                    batch = []

        if batch:
            with session.transaction(TransactionType.WRITE) as tx:
                for q in batch:
                    try:
                        tx.query(q)
                    except Exception:
                        pass
                tx.commit()
            total += len(batch)

    print(f"  Linked {total} dependencies total")
    return total


def main():
    db_path = Path(sys.argv[1]) if len(sys.argv) > 1 else RIPPKGS_DB

    if not db_path.exists():
        print(f"rippkgs index not found at {db_path}")
        print("Generate it with: rippkgs-index nixpkgs -o data/rippkgs-index.sqlite")
        sys.exit(1)

    print(f"Connecting to TypeDB at {TYPEDB_ADDRESS}...")
    with TypeDB.core_driver(TYPEDB_ADDRESS) as driver:
        print("Creating database and loading schema...")
        create_database(driver)

        print(f"Reading packages from {db_path}...")
        ingest_packages(driver, db_path)

        print("Linking dependencies...")
        ingest_dependencies(driver, db_path)

    print("Done.")


if __name__ == "__main__":
    main()
