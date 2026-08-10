{
  description = "Chrome on a GitHub Actions runner, driven over HTTPS";

  inputs = {
    nixpkgs.url = "github:nixos/nixpkgs/nixos-unstable";
    pyproject-nix = {
      url = "github:pyproject-nix/pyproject.nix";
      inputs.nixpkgs.follows = "nixpkgs";
    };
    uv2nix = {
      url = "github:pyproject-nix/uv2nix";
      inputs.nixpkgs.follows = "nixpkgs";
      inputs.pyproject-nix.follows = "pyproject-nix";
    };
    pyproject-build-systems = {
      url = "github:pyproject-nix/build-system-pkgs";
      inputs.nixpkgs.follows = "nixpkgs";
      inputs.pyproject-nix.follows = "pyproject-nix";
      inputs.uv2nix.follows = "uv2nix";
    };
  };

  outputs =
    {
      self,
      nixpkgs,
      pyproject-nix,
      uv2nix,
      pyproject-build-systems,
    }:
    let
      inherit (nixpkgs) lib;

      systems = [
        "x86_64-linux"
        "aarch64-linux"
      ];

      forAllSystems = lib.genAttrs systems;

      buildArgs = { inherit pyproject-nix uv2nix pyproject-build-systems; };
    in
    {
      overlays.default = final: _prev: {
        gh-chrome = final.callPackage ./nix/package.nix buildArgs;
      };

      packages = forAllSystems (
        system:
        let
          gh-chrome = nixpkgs.legacyPackages.${system}.callPackage ./nix/package.nix buildArgs;
        in
        {
          inherit gh-chrome;
          default = gh-chrome;
          inherit (gh-chrome) venv;
        }
      );

      devShells = forAllSystems (
        system:
        let
          pkgs = nixpkgs.legacyPackages.${system};

          # The wheels uv installs, and Chrome itself, are linked against
          # libraries a Nix shell does not expose by itself.
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
              python313
              uv
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
              GH_CHROME_CHROME_BINARY = "${pkgs.chromium}/bin/chromium";
            };

            shellHook = ''
              export PGDATA="$PWD/.pgdata"
              export PGHOST="$PWD/.pgsock"
              export DATABASE_URL="postgresql:///gh_chrome?host=$PGHOST"
            '';
          };
        }
      );

      checks = forAllSystems (system: {
        inherit (self.packages.${system}) gh-chrome venv;
      });

      formatter = forAllSystems (system: nixpkgs.legacyPackages.${system}.nixfmt);

      nixosModules.default = import ./nix/module.nix self;
    };
}
