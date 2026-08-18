{
  description = "Self-hosted GitHub Actions runners on top of a pool";

  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";

  outputs =
    { self, nixpkgs }:
    let
      inherit (nixpkgs) lib;

      systems = [
        "x86_64-linux"
        "aarch64-linux"
      ];

      forAllSystems = lib.genAttrs systems;

      package =
        pkgs:
        pkgs.python313Packages.buildPythonApplication {
          pname = "pool-runners";
          version = "0.1.0";
          pyproject = true;
          src = lib.fileset.toSource {
            root = ./.;
            fileset = lib.fileset.unions [
              ./pyproject.toml
              ./README.md
              ./pool_runners
              ./tests
            ];
          };
          build-system = [ pkgs.python313Packages.hatchling ];
          nativeCheckInputs = [ pkgs.python313Packages.pytest ];
          checkPhase = ''
            runHook preCheck
            HOME=$TMPDIR PYTHONPATH=$PWD:$PYTHONPATH pytest -q
            runHook postCheck
          '';
          meta = {
            description = "Self-hosted GitHub Actions runners on top of a pool";
            mainProgram = "pool-runners";
            platforms = lib.platforms.linux;
          };
        };
    in
    {
      overlays.default = final: _prev: { pool-runners = package final; };

      packages = forAllSystems (
        system:
        let
          pool-runners = package nixpkgs.legacyPackages.${system};
        in
        {
          inherit pool-runners;
          default = pool-runners;
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
              pkgs.just
              pkgs.python313
              pkgs.ruff
              pkgs.uv
            ];
            env.UV_NO_SYNC = "1";
          };
        }
      );

      checks = forAllSystems (system: { inherit (self.packages.${system}) pool-runners; });

      formatter = forAllSystems (system: nixpkgs.legacyPackages.${system}.nixfmt);

      nixosModules.default = import ./nix/module.nix self;
    };
}
