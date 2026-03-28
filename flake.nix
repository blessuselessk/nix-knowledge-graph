{
  description = "Semantic search over nixpkgs via TypeDB knowledge graph + embeddings";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
    rippkgs.url = "github:replit/rippkgs/v1.1.0";
    nix-index-database = {
      url = "github:nix-community/nix-index-database";
      inputs.nixpkgs.follows = "nixpkgs";
    };
  };

  outputs =
    {
      nixpkgs,
      rippkgs,
      ...
    }:
    let
      supportedSystems = [
        "x86_64-linux"
        "aarch64-linux"
        "x86_64-darwin"
        "aarch64-darwin"
      ];
      forAllSystems = nixpkgs.lib.genAttrs supportedSystems;

      schemaDir = ./schema;
      ingestDir = ./ingest;
    in
    {
      apps = forAllSystems (
        system:
        let
          pkgs = nixpkgs.legacyPackages.${system};

          nkg-setup = pkgs.writeShellApplication {
            name = "nkg-setup";
            runtimeInputs = [
              pkgs.docker
              pkgs.python3
              pkgs.git
              pkgs.curl
              pkgs.nix-index
              rippkgs.packages.${system}.rippkgs-index
            ];
            text = ''
              set -euo pipefail

              NKG_DATA="''${XDG_DATA_HOME:-$HOME/.local/share}/nix-knowledge-graph"
              mkdir -p "$NKG_DATA"

              echo "==> Data directory: $NKG_DATA"

              # --- TypeDB ---
              echo ""
              echo "==> Checking TypeDB..."
              if docker ps --format '{{.Names}}' | grep -q nkg-typedb; then
                echo "    TypeDB already running"
              else
                echo "    Starting TypeDB 3.8.2..."
                docker run -d \
                  --name nkg-typedb \
                  -p 1729:1729 \
                  -v nkg-typedb-data:/opt/typedb-all-linux-x86_64/server/data \
                  typedb/typedb:3.8.2 \
                  > /dev/null
                echo "    Waiting for TypeDB to be ready..."
                for i in $(seq 1 30); do
                  if docker exec nkg-typedb bash -c "echo ok" > /dev/null 2>&1; then
                    break
                  fi
                  sleep 1
                done
                # Give the server a moment to fully initialize
                sleep 3
                echo "    TypeDB ready"
              fi

              # --- Python venv ---
              echo ""
              echo "==> Setting up Python environment..."
              VENV="$NKG_DATA/.venv"
              if [ ! -d "$VENV" ]; then
                python3 -m venv "$VENV"
                "$VENV/bin/pip" install -q typedb-driver rich requests
              fi
              PYTHON="$VENV/bin/python"

              # --- rippkgs index ---
              echo ""
              RIPPKGS_DB="$NKG_DATA/rippkgs-index.sqlite"
              if [ -f "$RIPPKGS_DB" ]; then
                echo "==> rippkgs index exists at $RIPPKGS_DB"
                echo "    Delete it to regenerate"
              else
                echo "==> Generating rippkgs index (this evaluates nixpkgs, ~5 min)..."
                rippkgs-index nixpkgs -o "$RIPPKGS_DB"
              fi

              # --- tldr pages ---
              echo ""
              TLDR_DIR="$NKG_DATA/tldr"
              if [ -d "$TLDR_DIR/pages" ]; then
                echo "==> tldr pages exist at $TLDR_DIR"
              else
                echo "==> Cloning tldr-pages..."
                git clone --depth=1 https://github.com/tldr-pages/tldr.git "$TLDR_DIR"
              fi

              # --- Ingest ---
              export TYPEDB_ADDRESS="localhost:1729"
              export NKG_DATABASE="nix-knowledge-graph"
              export NKG_SCHEMA_DIR="${schemaDir}"
              export NKG_DATA_DIR="$NKG_DATA"
              export NKG_RIPPKGS_DB="$RIPPKGS_DB"

              echo ""
              echo "==> Ingesting rippkgs package metadata..."
              "$PYTHON" ${ingestDir}/rippkgs.py "$RIPPKGS_DB"

              echo ""
              echo "==> Ingesting tldr pages..."
              "$PYTHON" ${ingestDir}/tldr.py "$TLDR_DIR"

              # --- nix-index ---
              echo ""
              echo "==> Ingesting nix-index file listings..."
              "$PYTHON" ${ingestDir}/nix_index.py

              # --- cheat.sh ---
              echo ""
              CHEAT_DIR="$NKG_DATA/cheat.sheets"
              if [ -d "$CHEAT_DIR/sheets" ]; then
                echo "==> cheat.sheets exist at $CHEAT_DIR"
              else
                echo "==> Cloning cheat.sheets..."
                git clone --depth=1 https://github.com/chubin/cheat.sheets.git "$CHEAT_DIR"
              fi
              echo "==> Ingesting cheat.sh sheets..."
              "$PYTHON" ${ingestDir}/cheat.py "$CHEAT_DIR/sheets/_default"

              # --- navi ---
              echo ""
              NAVI_DIR="$NKG_DATA/navi-cheats"
              if [ -d "$NAVI_DIR" ]; then
                echo "==> navi cheats exist at $NAVI_DIR"
              else
                echo "==> Cloning navi cheats..."
                git clone --depth=1 https://github.com/denisidoro/cheats.git "$NAVI_DIR"
              fi
              echo "==> Ingesting navi cheats..."
              "$PYTHON" ${ingestDir}/navi.py "$NAVI_DIR"

              # --- man pages ---
              echo ""
              echo "==> Ingesting man pages..."
              MAN_DIR=""
              if [ -d "/run/current-system/sw/share/man" ]; then
                MAN_DIR="/run/current-system/sw/share/man"
              elif [ -d "$HOME/.nix-profile/share/man" ]; then
                MAN_DIR="$HOME/.nix-profile/share/man"
              elif [ -d "/usr/share/man" ]; then
                MAN_DIR="/usr/share/man"
              fi
              if [ -n "$MAN_DIR" ]; then
                "$PYTHON" ${ingestDir}/man_pages.py "$MAN_DIR"
              else
                echo "    No man pages directory found, skipping"
              fi

              echo ""
              echo "==> Setup complete!"
              echo "    TypeDB:   localhost:1729"
              echo "    Database: nix-knowledge-graph"
              echo "    Data:     $NKG_DATA"
              echo ""
              echo "    Next: nix run .#embed   to generate embeddings"
              echo "          nix run .#search  to query"
            '';
          };

          nkg-status = pkgs.writeShellApplication {
            name = "nkg-status";
            runtimeInputs = [
              pkgs.docker
              pkgs.sqlite
              pkgs.coreutils
              pkgs.procps
              pkgs.gnugrep
            ];
            text = ''
              NKG_DATA="''${XDG_DATA_HOME:-$HOME/.local/share}/nix-knowledge-graph"

              echo "nix-knowledge-graph"
              echo "==================="
              echo ""

              # --- Docker ---
              if ! command -v docker &>/dev/null; then
                echo "  Docker:      not installed"
              elif ! docker info &>/dev/null; then
                echo "  Docker:      not running"
              else
                echo "  Docker:      ok"
              fi

              # --- TypeDB ---
              if docker ps --format '{{.Names}}' 2>/dev/null | grep -q nkg-typedb; then
                STARTED=$(docker inspect --format '{{.State.StartedAt}}' nkg-typedb 2>/dev/null)
                STATUS=$(docker inspect --format '{{.State.Status}}' nkg-typedb 2>/dev/null)
                HEALTH=""
                # Check if TypeDB is actually accepting connections
                if docker exec nkg-typedb bash -c "echo ok" &>/dev/null; then
                  HEALTH="healthy"
                else
                  HEALTH="starting"
                fi
                MEM=$(docker stats --no-stream --format '{{.MemUsage}}' nkg-typedb 2>/dev/null || echo "?")
                echo "  TypeDB:      $STATUS ($HEALTH) since $STARTED"
                echo "               mem: $MEM"
              elif docker ps -a --format '{{.Names}}' 2>/dev/null | grep -q nkg-typedb; then
                EXIT_CODE=$(docker inspect --format '{{.State.ExitCode}}' nkg-typedb 2>/dev/null || echo "?")
                echo "  TypeDB:      stopped (exit $EXIT_CODE)"
              else
                echo "  TypeDB:      not created"
              fi

              echo ""

              # --- rippkgs index ---
              RIPPKGS_DB="$NKG_DATA/rippkgs-index.sqlite"
              if [ -f "$RIPPKGS_DB" ]; then
                PKG_COUNT=$(sqlite3 "$RIPPKGS_DB" "SELECT COUNT(*) FROM packages;" 2>/dev/null || echo "?")
                WITH_DESC=$(sqlite3 "$RIPPKGS_DB" "SELECT COUNT(*) FROM packages WHERE description IS NOT NULL AND description != '''';" 2>/dev/null || echo "?")
                WITH_DEPS=$(sqlite3 "$RIPPKGS_DB" "SELECT COUNT(*) FROM packages WHERE propagatedBuildInputs IS NOT NULL;" 2>/dev/null || echo "?")
                SIZE=$(du -h "$RIPPKGS_DB" | cut -f1)
                MTIME=$(stat -c '%Y' "$RIPPKGS_DB" 2>/dev/null || stat -f '%m' "$RIPPKGS_DB" 2>/dev/null)
                AGE=$(( $(date +%s) - MTIME ))
                if [ "$AGE" -lt 3600 ]; then
                  AGE_STR="$((AGE / 60))m ago"
                elif [ "$AGE" -lt 86400 ]; then
                  AGE_STR="$((AGE / 3600))h ago"
                else
                  AGE_STR="$((AGE / 86400))d ago"
                fi
                echo "  Packages:    $PKG_COUNT total, $WITH_DESC with descriptions, $WITH_DEPS with deps ($SIZE, indexed $AGE_STR)"
              else
                echo "  Packages:    not indexed"
              fi

              # Check if rippkgs-index is currently running
              if pgrep -f "rippkgs-index" > /dev/null 2>&1; then
                PID=$(pgrep -f "rippkgs-index" | head -1)
                ELAPSED=$(ps -o etime= -p "$PID" 2>/dev/null | tr -d ' ')
                # Check if partial output exists
                PARTIAL="$NKG_DATA/rippkgs-index.sqlite"
                if [ -f "$PARTIAL" ]; then
                  PARTIAL_SIZE=$(du -h "$PARTIAL" | cut -f1)
                  echo "               INDEXING in progress (pid $PID, elapsed $ELAPSED, $PARTIAL_SIZE so far)"
                else
                  echo "               INDEXING in progress (pid $PID, elapsed $ELAPSED, evaluating nixpkgs...)"
                fi
              fi

              # --- tldr ---
              TLDR_DIR="$NKG_DATA/tldr"
              if [ -d "$TLDR_DIR/pages" ]; then
                TOTAL=$(find "$TLDR_DIR/pages" -name '*.md' | wc -l | tr -d ' ')
                COMMON=$(find "$TLDR_DIR/pages/common" -name '*.md' 2>/dev/null | wc -l | tr -d ' ')
                LINUX=$(find "$TLDR_DIR/pages/linux" -name '*.md' 2>/dev/null | wc -l | tr -d ' ')
                OSX=$(find "$TLDR_DIR/pages/osx" -name '*.md' 2>/dev/null | wc -l | tr -d ' ')
                echo "  tldr:        $TOTAL pages (common: $COMMON, linux: $LINUX, osx: $OSX)"
              else
                echo "  tldr:        not fetched"
              fi

              # Check if git clone for tldr is in progress
              if pgrep -f "git.*clone.*tldr" > /dev/null 2>&1; then
                echo "               CLONING in progress..."
              fi

              # --- Python venv ---
              VENV="$NKG_DATA/.venv"
              if [ -d "$VENV" ]; then
                TYPEDB_VER=$("$VENV/bin/pip" show typedb-driver 2>/dev/null | grep "^Version:" | cut -d' ' -f2 || echo "?")
                echo "  venv:        ok (typedb-driver $TYPEDB_VER)"
              else
                echo "  venv:        not created"
              fi

              # --- Ingest processes ---
              if pgrep -f "ingest/rippkgs.py" > /dev/null 2>&1; then
                PID=$(pgrep -f "ingest/rippkgs.py" | head -1)
                ELAPSED=$(ps -o etime= -p "$PID" 2>/dev/null | tr -d ' ')
                echo "  Ingest:      rippkgs.py running (pid $PID, elapsed $ELAPSED)"
              elif pgrep -f "ingest/tldr.py" > /dev/null 2>&1; then
                PID=$(pgrep -f "ingest/tldr.py" | head -1)
                ELAPSED=$(ps -o etime= -p "$PID" 2>/dev/null | tr -d ' ')
                echo "  Ingest:      tldr.py running (pid $PID, elapsed $ELAPSED)"
              else
                echo "  Ingest:      idle"
              fi

              # --- Disk ---
              if [ -d "$NKG_DATA" ]; then
                TOTAL_SIZE=$(du -sh "$NKG_DATA" 2>/dev/null | cut -f1)
                echo ""
                echo "  Data:        $NKG_DATA ($TOTAL_SIZE)"
              fi

              echo ""
              echo "  nix run .#setup   set up / ingest all sources"
              echo "  nix run .#embed   generate embeddings"
              echo "  nix run .#search  query the knowledge graph"
              echo "  nix run .#stop    stop TypeDB"
            '';
          };

          nkg-stop = pkgs.writeShellApplication {
            name = "nkg-stop";
            runtimeInputs = [ pkgs.docker ];
            text = ''
              echo "Stopping TypeDB..."
              docker stop nkg-typedb > /dev/null 2>&1 && docker rm nkg-typedb > /dev/null 2>&1 || true
              echo "Done."
            '';
          };

          nkg-embed = pkgs.writeShellApplication {
            name = "nkg-embed";
            runtimeInputs = [ pkgs.python3 pkgs.curl ];
            text = ''
              NKG_DATA="''${XDG_DATA_HOME:-$HOME/.local/share}/nix-knowledge-graph"
              VENV="$NKG_DATA/.venv"

              if [ ! -d "$VENV" ]; then
                echo "Run nix run .#setup first"
                exit 1
              fi

              export TYPEDB_ADDRESS="localhost:1729"
              export NKG_DATABASE="nix-knowledge-graph"
              export NKG_DATA_DIR="$NKG_DATA"
              export LLAMAFILE_PORT="8080"

              "$VENV/bin/python" ${./embed}/generate.py "$@"
            '';
          };

          nkg-search = pkgs.writeShellApplication {
            name = "nkg-search";
            runtimeInputs = [ pkgs.python3 ];
            text = ''
              NKG_DATA="''${XDG_DATA_HOME:-$HOME/.local/share}/nix-knowledge-graph"
              VENV="$NKG_DATA/.venv"

              if [ ! -d "$VENV" ]; then
                echo "Run nix run .#setup first"
                exit 1
              fi

              export TYPEDB_ADDRESS="localhost:1729"
              export NKG_DATABASE="nix-knowledge-graph"
              export NKG_DATA_DIR="$NKG_DATA"
              export LLAMAFILE_PORT="8080"

              "$VENV/bin/python" ${./query}/search.py "$@"
            '';
          };
        in
        {
          default = {
            type = "app";
            program = "${nkg-status}/bin/nkg-status";
          };
          status = {
            type = "app";
            program = "${nkg-status}/bin/nkg-status";
          };
          setup = {
            type = "app";
            program = "${nkg-setup}/bin/nkg-setup";
          };
          stop = {
            type = "app";
            program = "${nkg-stop}/bin/nkg-stop";
          };
          embed = {
            type = "app";
            program = "${nkg-embed}/bin/nkg-embed";
          };
          search = {
            type = "app";
            program = "${nkg-search}/bin/nkg-search";
          };
        }
      );

      devShells = forAllSystems (
        system:
        let
          pkgs = nixpkgs.legacyPackages.${system};
        in
        {
          default = pkgs.mkShell {
            packages = [
              pkgs.python3
              pkgs.python3Packages.pip
              rippkgs.packages.${system}.rippkgs
              rippkgs.packages.${system}.rippkgs-index
              pkgs.sqlite
              pkgs.jq
              pkgs.docker-compose
              pkgs.nix-index
              pkgs.tldr
            ];

            shellHook = ''
              if [ ! -d .venv ]; then
                python -m venv .venv
                .venv/bin/pip install -q typedb-driver rich requests
              fi
              source .venv/bin/activate

              echo "nix-knowledge-graph dev shell"
              echo ""
              echo "  nix run .            full setup (TypeDB + ingest all sources)"
              echo "  nix run .#stop       stop TypeDB"
              echo ""
            '';
          };
        }
      );
    };
}
