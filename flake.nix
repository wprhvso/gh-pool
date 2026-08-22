{
  description = "Пул задач на раннерах GitHub Actions, браузерные сессии и флот раннеров";

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
        (pkgs.callPackage pyproject-nix.build.packages { python = pkgs.python314; }).overrideScope (
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
          gh-pool = (pythonSet pkgs).mkVirtualEnv "gh-pool-env" deps;
          default = gh-pool;
        }
      );

      devShells = forAllSystems (
        system:
        let
          pkgs = nixpkgs.legacyPackages.${system};

          libs = with pkgs; [
            stdenv.cc.cc.lib
            zlib
            xorg.libX11
            xorg.libXtst
            xorg.libXi
            xorg.libXext
          ];
        in
        {
          default = pkgs.mkShell {
            packages = with pkgs; [
              python314
              uv
              ruff
              postgresql_17
              ffmpeg-full
              xorg.xorgserver
              xorg.xdpyinfo
              xdotool
              openbox
              x11vnc
              chromium
              zstd
            ];

            env = {
              LD_LIBRARY_PATH = lib.makeLibraryPath libs;
              UV_PYTHON_DOWNLOADS = "never";
              UV_NO_SYNC = "1";
              GH_POOL_CHROME_BINARY = "${pkgs.chromium}/bin/chromium";
            };

            shellHook = ''
              export PGDATA="$PWD/.pgdata"
              export PGHOST="$PWD/.pgsock"
              export GH_POOL_DATABASE_URL="postgresql:///pool?host=$PGHOST"
            '';
          };
        }
      );

      nixosModules = rec {
        client = import ./nix/client.nix self;
        default = client;
      };

      checks = forAllSystems (system: {
        inherit (self.packages.${system}) gh-pool;
      });
    };
}
