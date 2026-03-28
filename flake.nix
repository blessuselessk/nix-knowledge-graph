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
    in
    {
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
              # Set up venv for typedb-driver (not in nixpkgs)
              if [ ! -d .venv ]; then
                python -m venv .venv
                .venv/bin/pip install -q typedb-driver rich
              fi
              source .venv/bin/activate

              echo "nix-knowledge-graph dev shell"
              echo ""
              echo "  docker compose up -d           start TypeDB"
              echo "  rippkgs-index nixpkgs -o data/rippkgs-index.sqlite"
              echo "  python ingest/rippkgs.py       ingest package metadata"
              echo "  python ingest/tldr.py          ingest tldr examples"
              echo ""
            '';
          };
        }
      );
    };
}
