"""Generate embeddings from the TypeDB knowledge graph.

Walks the graph, produces context-rich text chunks for each entity,
embeds them with llamafile + nomic-embed-text, and stores in sqlite-vec.

Usage:
    python embed/generate.py [--llamafile-port 8080]

Expects llamafile to be running with --embedding on the given port.
If not running, starts it automatically (requires model at data/model.gguf).
"""

import json
import os
import signal
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import requests
from typedb.driver import TypeDB, SessionType, TransactionType


TYPEDB_ADDRESS = os.environ.get("TYPEDB_ADDRESS", "localhost:1729")
DATABASE = os.environ.get("NKG_DATABASE", "nix-knowledge-graph")
DATA_DIR = Path(os.environ.get("NKG_DATA_DIR", "data"))
EMBED_DB = DATA_DIR / "embeddings.sqlite"
LLAMAFILE_PORT = int(os.environ.get("LLAMAFILE_PORT", "8080"))
LLAMAFILE_URL = f"http://127.0.0.1:{LLAMAFILE_PORT}"
MODEL_URL = (
    "https://huggingface.co/nomic-ai/nomic-embed-text-v1.5-GGUF"
    "/resolve/main/nomic-embed-text-v1.5.Q8_0.gguf"
)
BATCH_SIZE = 32


def ensure_model() -> Path:
    model_path = DATA_DIR / "nomic-embed-text-v1.5.Q8_0.gguf"
    if model_path.exists():
        return model_path
    print(f"  Downloading embedding model to {model_path}...")
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    subprocess.run(["curl", "-L", "-o", str(model_path), MODEL_URL], check=True)
    return model_path


def ensure_llamafile() -> Path:
    lf_path = DATA_DIR / "llamafile"
    if lf_path.exists():
        return lf_path
    print("  Downloading llamafile...")
    url = "https://github.com/mozilla-ai/llamafile/releases/download/0.10.0/llamafile-0.10.0"
    subprocess.run(["curl", "-L", "-o", str(lf_path), url], check=True)
    lf_path.chmod(0o755)
    return lf_path


def start_llamafile(model_path: Path):
    """Start llamafile embedding server if not already running."""
    try:
        requests.get(f"{LLAMAFILE_URL}/health", timeout=2)
        print("  Llamafile already running")
        return None
    except requests.ConnectionError:
        pass

    lf_path = ensure_llamafile()
    print("  Starting llamafile embedding server...")
    proc = subprocess.Popen(
        [
            str(lf_path),
            "--server",
            "--embedding",
            "--model", str(model_path),
            "--host", "127.0.0.1",
            "--port", str(LLAMAFILE_PORT),
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    for _ in range(60):
        try:
            requests.get(f"{LLAMAFILE_URL}/health", timeout=1)
            print("  Llamafile ready")
            return proc
        except requests.ConnectionError:
            time.sleep(1)

    proc.kill()
    raise RuntimeError("Failed to start llamafile")


def embed_texts(texts: list[str]) -> list[list[float]]:
    """Embed a batch of texts using llamafile."""
    embeddings = []
    for text in texts:
        # nomic-embed-text uses "search_document: " prefix for indexing
        prefixed = f"search_document: {text}"
        resp = requests.post(
            f"{LLAMAFILE_URL}/embedding",
            json={"content": prefixed},
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        embeddings.append(data["embedding"])
    return embeddings


def init_sqlite(db_path: Path):
    """Create the embeddings SQLite database."""
    conn = sqlite3.connect(db_path)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS chunks (
            id TEXT PRIMARY KEY,
            package TEXT,
            command TEXT,
            source TEXT,
            chunk_type TEXT,
            text TEXT,
            embedding BLOB
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_package ON chunks(package)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_command ON chunks(command)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_source ON chunks(source)")
    conn.commit()
    return conn


def extract_chunks(driver):
    """Walk TypeDB and produce embeddable chunks."""
    chunks = []

    with driver.session(DATABASE, SessionType.DATA) as session:
        # Package descriptions
        print("  Extracting package chunks...")
        with session.transaction(TransactionType.READ) as tx:
            results = tx.query(
                "match $p isa package, has attr-path $a, has description $d; "
                "select $a, $d;"
            )
            for row in results:
                attr = row.get("a").as_attribute().get_value()
                desc = row.get("d").as_attribute().get_value()
                if desc.strip():
                    chunks.append({
                        "id": f"pkg:{attr}",
                        "package": attr,
                        "command": None,
                        "source": "nixpkgs",
                        "chunk_type": "description",
                        "text": f"{attr.split('.')[-1]} - {desc}",
                    })

        # Command synopses
        print("  Extracting command chunks...")
        with session.transaction(TransactionType.READ) as tx:
            results = tx.query(
                "match $c isa command, has name $n; "
                "$c has synopsis $s; "
                "select $n, $s;"
            )
            for row in results:
                name = row.get("n").as_attribute().get_value()
                syn = row.get("s").as_attribute().get_value()
                if syn.strip():
                    chunks.append({
                        "id": f"cmd:{name}",
                        "package": None,
                        "command": name,
                        "source": "synopsis",
                        "chunk_type": "description",
                        "text": f"{name} - {syn}",
                    })

        # Examples (tldr, cheat.sh, navi)
        print("  Extracting example chunks...")
        with session.transaction(TransactionType.READ) as tx:
            results = tx.query(
                "match $ex isa example, has example-id $id, "
                "has source-type $src, has code $code; "
                "$ex has example-description $desc; "
                "(demonstration: $ex, subject: $c) isa demonstrates; "
                "$c has name $cmd; "
                "select $id, $src, $code, $desc, $cmd;"
            )
            for row in results:
                eid = row.get("id").as_attribute().get_value()
                src = row.get("src").as_attribute().get_value()
                code = row.get("code").as_attribute().get_value()
                desc = row.get("desc").as_attribute().get_value()
                cmd = row.get("cmd").as_attribute().get_value()

                text = f"{cmd}: {desc}\n{code}" if desc else f"{cmd}: {code}"
                chunks.append({
                    "id": f"ex:{eid}",
                    "package": None,
                    "command": cmd,
                    "source": src,
                    "chunk_type": "example",
                    "text": text,
                })

        # Man page sections
        print("  Extracting man page chunks...")
        with session.transaction(TransactionType.READ) as tx:
            results = tx.query(
                "match $s isa man-section, has section-id $id, "
                "has heading $h, has content $c, has section-type $t; "
                "(whole: $m, part: $s) isa has-section; "
                "$m has man-id $mid; "
                "select $id, $h, $c, $t, $mid;"
            )
            for row in results:
                sid = row.get("id").as_attribute().get_value()
                heading = row.get("h").as_attribute().get_value()
                content = row.get("c").as_attribute().get_value()
                sec_type = row.get("t").as_attribute().get_value()
                man_id = row.get("mid").as_attribute().get_value()
                cmd = man_id.rsplit(".", 1)[0]

                # Truncate long sections
                text = f"{cmd} man {heading}:\n{content[:2000]}"
                chunks.append({
                    "id": f"man:{sid}",
                    "package": None,
                    "command": cmd,
                    "source": "man",
                    "chunk_type": sec_type,
                    "text": text,
                })

        # Flags
        print("  Extracting flag chunks...")
        with session.transaction(TransactionType.READ) as tx:
            results = tx.query(
                "match $f isa flag, has flag-id $id, has flag-name $n; "
                "$f has flag-description $d; "
                "(owner: $c, option: $f) isa has-flag; "
                "$c has name $cmd; "
                "select $id, $n, $d, $cmd;"
            )
            for row in results:
                fid = row.get("id").as_attribute().get_value()
                fname = row.get("n").as_attribute().get_value()
                fdesc = row.get("d").as_attribute().get_value()
                cmd = row.get("cmd").as_attribute().get_value()

                text = f"{cmd} {fname}: {fdesc}"
                chunks.append({
                    "id": f"flag:{fid}",
                    "package": None,
                    "command": cmd,
                    "source": "man",
                    "chunk_type": "flag",
                    "text": text,
                })

    print(f"  Extracted {len(chunks)} chunks total")
    return chunks


def embed_and_store(chunks, sqlite_conn):
    """Embed chunks and store in SQLite."""
    total = 0

    for i in range(0, len(chunks), BATCH_SIZE):
        batch = chunks[i : i + BATCH_SIZE]
        texts = [c["text"] for c in batch]

        embeddings = embed_texts(texts)

        for chunk, emb in zip(batch, embeddings):
            sqlite_conn.execute(
                "INSERT OR REPLACE INTO chunks "
                "(id, package, command, source, chunk_type, text, embedding) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    chunk["id"],
                    chunk["package"],
                    chunk["command"],
                    chunk["source"],
                    chunk["chunk_type"],
                    chunk["text"],
                    json.dumps(emb),
                ),
            )

        sqlite_conn.commit()
        total += len(batch)
        print(f"  Embedded {total}/{len(chunks)} chunks...", end="\r")

    print(f"  Embedded {total} chunks total")


def main():
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    model_path = ensure_model()
    lf_proc = start_llamafile(model_path)

    try:
        print(f"Connecting to TypeDB at {TYPEDB_ADDRESS}...")
        with TypeDB.core_driver(TYPEDB_ADDRESS) as driver:
            chunks = extract_chunks(driver)

        if not chunks:
            print("No chunks to embed. Run ingesters first.")
            return

        print(f"Initializing embeddings DB at {EMBED_DB}...")
        conn = init_sqlite(EMBED_DB)

        print("Embedding chunks...")
        embed_and_store(chunks, conn)
        conn.close()

        print(f"\nDone. Embeddings stored at {EMBED_DB}")
        print(f"  {len(chunks)} chunks, {os.path.getsize(EMBED_DB) / 1e6:.1f} MB")

    finally:
        if lf_proc:
            print("Stopping llamafile...")
            lf_proc.send_signal(signal.SIGTERM)
            lf_proc.wait(timeout=10)


if __name__ == "__main__":
    main()
