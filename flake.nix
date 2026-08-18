{
  description = "Self-hosted GitHub Actions runners on top of a pool";

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

      forAllSystems = lib.genAttrs [
        "x86_64-linux"
        "aarch64-linux"
      ];

      workspace = uv2nix.lib.workspace.loadWorkspace { workspaceRoot = ./.; };
      overlay = workspace.mkPyprojectOverlay { sourcePreference = "wheel"; };
      deps = workspace.deps.default;

      pythonSet =
        pkgs:
        (pkgs.callPackage pyproject-nix.build.packages { python = pkgs.python314; }).overrideScope (
          lib.composeManyExtensions [
            pyproject-build-systems.overlays.wheel
            overlay
          ]
        );

      package =
        pkgs:
        ((pythonSet pkgs).mkVirtualEnv "pool-runners-env" deps).overrideAttrs (old: {
          meta = (old.meta or { }) // {
            mainProgram = "pool-runners";
            description = "Self-hosted GitHub Actions runners on top of a pool";
            platforms = lib.platforms.linux;
          };
        });
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
              self.packages.${system}.pool-runners
              pkgs.just
              pkgs.ruff
              pkgs.uv
            ];
            env.UV_NO_SYNC = "1";
          };
        }
      );

      checks = forAllSystems (system: {
        inherit (self.packages.${system}) pool-runners;
      });

      formatter = forAllSystems (system: nixpkgs.legacyPackages.${system}.nixfmt);

      nixosModules.default = import ./nix/module.nix self;
    };
}
