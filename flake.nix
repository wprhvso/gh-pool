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
      inputs = {
        nixpkgs.follows = "nixpkgs";
        pyproject-nix.follows = "pyproject-nix";
        uv2nix.follows = "uv2nix";
      };
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

      machine =
        system:
        lib.nixosSystem {
          inherit system;
          modules = [
            self.nixosModules.default
            {
              boot.loader.grub.enable = false;
              fileSystems."/" = {
                device = "/dev/disk/by-label/nixos";
                fsType = "ext4";
              };
              system.stateVersion = "25.05";
              services.gh-chrome = {
                enable = true;
                publicUrl = "https://chrome.example.com";
                database.createLocally = true;
                environmentFiles = [ "/var/lib/secrets/gh-chrome" ];
              };
            }
          ];
        };

      moduleFacts =
        system:
        let
          inherit (machine system) config;
          failed = lib.filter (item: !item.assertion) config.assertions;
          unit = config.systemd.services.gh-chrome;
          database = config.services.postgresql;
          owner = lib.filter (user: user.ensureDBOwnership) database.ensureUsers;
        in
        {
          failures = lib.concatMapStringsSep "\n" (item: item.message) failed;
          url = unit.environment.GH_CHROME_DATABASE_URL or "";
          databases = lib.concatStringsSep " " database.ensureDatabases;
          owners = lib.concatMapStringsSep " " (user: user.name) owner;
        };

      moduleScript = ''
        set -eu
        if [ -n "$failures" ]; then
          printf 'the module asserts:\n%s\n' "$failures" >&2
          exit 1
        fi
        case "$url" in
          *"/run/postgresql"*) ;;
          *) echo "the unit has no local database url: '$url'" >&2; exit 1 ;;
        esac
        case " $databases " in
          *" gh-chrome "*) ;;
          *) echo "no database was created: '$databases'" >&2; exit 1 ;;
        esac
        case " $owners " in
          *" gh-chrome "*) ;;
          *) echo "the service user owns nothing: '$owners'" >&2; exit 1 ;;
        esac
        echo ok > "$out"
      '';

      moduleCheck =
        system:
        let
          pkgs = nixpkgs.legacyPackages.${system};
        in
        pkgs.runCommand "gh-chrome-module" (moduleFacts system) moduleScript;
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
        module = moduleCheck system;
      });

      formatter = forAllSystems (system: nixpkgs.legacyPackages.${system}.nixfmt);

      nixosModules.default = import ./nix/module.nix self;
    };
}
