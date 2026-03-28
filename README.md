# nix-knowledge-graph

Semantic search over nixpkgs via TypeDB knowledge graph + embeddings.

Combines multiple sources into a richly-typed knowledge graph, then generates embeddings for hybrid structural + semantic search over Nix packages.

## Data sources

| Source | What it captures |
|--------|-----------------|
| [rippkgs](https://github.com/replit/rippkgs) | Package attr paths, names, versions, descriptions, dependencies |
| [nix-index-database](https://github.com/nix-community/nix-index-database) | File-to-package mappings (which package provides `/bin/jq`?) |
| [tldr-pages](https://github.com/tldr-pages/tldr) | Concise, practical command examples |
| [cheat.sh](https://github.com/chubin/cheat.sh) | Community-curated recipes and snippets |
| [navi](https://github.com/denisidoro/navi) | Interactive cheatsheets with parameterized commands |
| man pages | Full reference: flags, options, synopses, detailed examples |

## Architecture

```
rippkgs ─────┐
nix-index ───┤
tldr ────────┤
cheat.sh ────┼──▶ TypeDB ──▶ chunk ──▶ embed ──▶ sqlite-vec
navi ────────┤     (graph)              (llamafile)  (vector index)
man pages ───┘
```

TypeDB stores the full knowledge graph with typed entities and relations. Embeddings are generated from graph-contextualized chunks. Queries combine graph traversal (structural) with vector similarity (semantic).

## Quick start

One command sets up everything — starts TypeDB, generates the rippkgs index, clones tldr-pages, and ingests all sources:

```bash
nix run github:blessuselessk/nix-knowledge-graph
```

This will:
1. Start TypeDB 3.8.2 in Docker (`nkg-typedb` container)
2. Create a Python venv with `typedb-driver`
3. Generate the rippkgs index (~5 min, evaluates nixpkgs)
4. Clone tldr-pages
5. Ingest everything into TypeDB

Data is stored in `~/.local/share/nix-knowledge-graph/`.

To stop TypeDB:
```bash
nix run github:blessuselessk/nix-knowledge-graph#stop
```

### Development

```bash
nix develop
# Then run ingesters individually:
python ingest/rippkgs.py
python ingest/tldr.py
```

## TypeDB schema

See [schema/types.tql](schema/types.tql) for entity/relation definitions and [schema/rules.tql](schema/rules.tql) for inference rules.

Core entities: `package`, `command`, `file-entry`, `man-page`, `example`, `flag`

Core relations: `provides`, `depends-on`, `documents`, `demonstrates`, `co-occurs`

## Status

- [x] Project scaffold, TypeDB schema, Docker setup
- [x] rippkgs ingester (package metadata + dependencies)
- [x] tldr ingester (command examples)
- [ ] nix-index ingester (file listings)
- [ ] man page ingester
- [ ] cheat.sh ingester
- [ ] navi ingester
- [ ] Embedding pipeline (llamafile + nomic-embed-text)
- [ ] Query interface (hybrid graph + vector search)
- [ ] Package TypeDB for Nix (phase B)
