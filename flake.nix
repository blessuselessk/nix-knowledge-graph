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
                "$VENV/bin/pip" install -q typedb-driver rich
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

              echo ""
              echo "==> Setup complete!"
              echo "    TypeDB:  localhost:1729"
              echo "    Database: nix-knowledge-graph"
              echo "    Data:    $NKG_DATA"
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
        in
        {
          default = {
            type = "app";
            program = "${nkg-setup}/bin/nkg-setup";
          };
          stop = {
            type = "app";
            program = "${nkg-stop}/bin/nkg-stop";
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
              pkgs.tldr
            ];

            shellHook = ''
              if [ ! -d .venv ]; then
                python -m venv .venv
                .venv/bin/pip install -q typedb-driver rich
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
