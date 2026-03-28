"""Hybrid semantic + graph search over the nix knowledge graph.

Embeds the query with llamafile, finds nearest chunks in sqlite,
then enriches results with TypeDB graph context.

Usage:
    python query/search.py "process JSON on the command line"
    python query/search.py --source tldr "download files"
    python query/search.py --top 20 "build docker images"
"""

import argparse
import json
import math
import os
import sqlite3
import sys
from pathlib import Path

import requests
from typedb.driver import TypeDB, SessionType, TransactionType


TYPEDB_ADDRESS = os.environ.get("TYPEDB_ADDRESS", "localhost:1729")
DATABASE = os.environ.get("NKG_DATABASE", "nix-knowledge-graph")
DATA_DIR = Path(os.environ.get("NKG_DATA_DIR", "data"))
EMBED_DB = DATA_DIR / "embeddings.sqlite"
LLAMAFILE_PORT = int(os.environ.get("LLAMAFILE_PORT", "8080"))
LLAMAFILE_URL = f"http://127.0.0.1:{LLAMAFILE_PORT}"


def embed_query(text: str) -> list[float]:
    """Embed a search query."""
    prefixed = f"search_query: {text}"
    resp = requests.post(
        f"{LLAMAFILE_URL}/embedding",
        json={"content": prefixed},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()["embedding"]


def cosine_similarity(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(x * x for x in b))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


def search_embeddings(
    query_embedding: list[float],
    top_k: int = 10,
    source_filter: str = None,
) -> list[dict]:
    """Brute-force nearest neighbor search in the embeddings DB."""
    conn = sqlite3.connect(EMBED_DB)
    conn.row_factory = sqlite3.Row

    query = "SELECT * FROM chunks"
    params = []
    if source_filter:
        query += " WHERE source = ?"
        params.append(source_filter)

    rows = conn.execute(query, params).fetchall()

    scored = []
    for row in rows:
        emb = json.loads(row["embedding"])
        score = cosine_similarity(query_embedding, emb)
        scored.append({
            "id": row["id"],
            "package": row["package"],
            "command": row["command"],
            "source": row["source"],
            "chunk_type": row["chunk_type"],
            "text": row["text"],
            "score": score,
        })

    scored.sort(key=lambda x: x["score"], reverse=True)
    conn.close()
    return scored[:top_k]


def enrich_with_graph(results: list[dict]) -> list[dict]:
    """Add graph context to search results from TypeDB."""
    try:
        driver = TypeDB.core_driver(TYPEDB_ADDRESS)
    except Exception:
        return results  # TypeDB not available, return as-is

    with driver.session(DATABASE, SessionType.DATA) as session:
        for result in results:
            cmd = result.get("command")
            pkg = result.get("package")

            if cmd:
                with session.transaction(TransactionType.READ) as tx:
                    # Find providing packages
                    try:
                        pkgs = tx.query(
                            f'match (provider: $p, artifact: $c) isa provides; '
                            f'$c isa command, has name "{cmd}"; '
                            f'$p has attr-path $a; select $a;'
                        )
                        result["packages"] = [
                            r.get("a").as_attribute().get_value() for r in pkgs
                        ]
                    except Exception:
                        result["packages"] = []

                    # Find co-occurring commands
                    try:
                        coocs = tx.query(
                            f'match (companion: $c1, companion: $c2) isa co-occurs; '
                            f'$c1 has name "{cmd}"; '
                            f'$c2 has name $n; select $n;'
                        )
                        result["related_commands"] = list(set(
                            r.get("n").as_attribute().get_value() for r in coocs
                        ))[:5]
                    except Exception:
                        result["related_commands"] = []

            if pkg:
                with session.transaction(TransactionType.READ) as tx:
                    # Find commands this package provides
                    try:
                        cmds = tx.query(
                            f'match (provider: $p, artifact: $c) isa provides; '
                            f'$p isa package, has attr-path "{pkg}"; '
                            f'$c has name $n; select $n;'
                        )
                        result["commands"] = [
                            r.get("n").as_attribute().get_value() for r in cmds
                        ]
                    except Exception:
                        result["commands"] = []

    driver.close()
    return results


def format_results(results: list[dict]):
    """Pretty-print search results."""
    for i, r in enumerate(results, 1):
        score = r["score"]
        source = r["source"]
        chunk_type = r["chunk_type"]
        text = r["text"]

        # Truncate long text
        if len(text) > 200:
            text = text[:200] + "..."

        print(f"\n  {i}. [{source}/{chunk_type}] (score: {score:.3f})")
        print(f"     {text}")

        if r.get("packages"):
            print(f"     packages: {', '.join(r['packages'][:5])}")
        if r.get("commands"):
            print(f"     commands: {', '.join(r['commands'][:5])}")
        if r.get("related_commands"):
            print(f"     related:  {', '.join(r['related_commands'])}")


def main():
    parser = argparse.ArgumentParser(description="Search the nix knowledge graph")
    parser.add_argument("query", nargs="+", help="Search query")
    parser.add_argument("--top", type=int, default=10, help="Number of results")
    parser.add_argument("--source", help="Filter by source (tldr, cheat.sh, navi, man, nixpkgs)")
    parser.add_argument("--json", action="store_true", help="Output as JSON")
    args = parser.parse_args()

    query_text = " ".join(args.query)

    if not EMBED_DB.exists():
        print(f"Embeddings DB not found at {EMBED_DB}")
        print("Run: python embed/generate.py")
        sys.exit(1)

    # Check llamafile is running
    try:
        requests.get(f"{LLAMAFILE_URL}/health", timeout=2)
    except requests.ConnectionError:
        print(f"Llamafile not running on port {LLAMAFILE_PORT}")
        print("Start it with: python embed/generate.py (it auto-starts)")
        sys.exit(1)

    # Embed query
    query_emb = embed_query(query_text)

    # Search
    results = search_embeddings(query_emb, top_k=args.top, source_filter=args.source)

    # Enrich with graph context
    results = enrich_with_graph(results)

    if args.json:
        # Remove embeddings from JSON output
        for r in results:
            r.pop("embedding", None)
        print(json.dumps(results, indent=2))
    else:
        print(f'Results for: "{query_text}"')
        format_results(results)


if __name__ == "__main__":
    main()
