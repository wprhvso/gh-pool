{
  description = "GitHub Actions as a generic worker pool";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";

    pyproject-nix = {
      url = "github:pyproject-nix/pyproject.nix";
      inputs.nixpkgs.follows = "nixpkgs";
    };

    uv2nix = {
      url = "github:pyproject-nix/uv2nix";
      inputs.pyproject-nix.follows = "pyproject-nix";
      inputs.nixpkgs.follows = "nixpkgs";
    };

    pyproject-build-systems = {
      url = "github:pyproject-nix/build-system-pkgs";
      inputs.pyproject-nix.follows = "pyproject-nix";
      inputs.uv2nix.follows = "uv2nix";
      inputs.nixpkgs.follows = "nixpkgs";
    };
  };

  outputs =
    {
      self,
      nixpkgs,
      pyproject-nix,
      uv2nix,
      pyproject-build-systems,
      ...
    }:
    let
      inherit (nixpkgs) lib;

      forAllSystems = lib.genAttrs lib.systems.flakeExposed;

      workspace = uv2nix.lib.workspace.loadWorkspace { workspaceRoot = ./.; };
      overlay = workspace.mkPyprojectOverlay { sourcePreference = "wheel"; };
      deps = workspace.deps.optionals;

      pythonSet =
        pkgs:
        (pkgs.callPackage pyproject-nix.build.packages { python = pkgs.python312; }).overrideScope (
          lib.composeManyExtensions [
            pyproject-build-systems.overlays.wheel
            overlay
          ]
        );
    in
    {
      packages = forAllSystems (
        system:
        let
          pkgs = nixpkgs.legacyPackages.${system};
        in
        rec {
          pool = (pythonSet pkgs).mkVirtualEnv "pool-env" deps;
          default = pool;
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
              self.packages.${system}.pool
              pkgs.uv
              pkgs.postgresql_16
              pkgs.ruff
            ];
            env.UV_NO_SYNC = "1";
          };
        }
      );

      nixosModules = {
        server = import ./nix/server.nix self;
        client = import ./nix/client.nix self;
        default = {
          imports = [
            self.nixosModules.server
            self.nixosModules.client
          ];
        };
      };

      checks = forAllSystems (system: {
        inherit (self.packages.${system}) pool;
      });
    };
}
