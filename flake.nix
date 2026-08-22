{
  description = "Пул задач на раннерах GitHub Actions, браузерные сессии и флот раннеров";

  inputs.nixpkgs.url = "github:nixos/nixpkgs/nixos-unstable";

  outputs =
    { nixpkgs, ... }:
    let
      inherit (nixpkgs) lib;

      systems = [
        "x86_64-linux"
        "aarch64-linux"
      ];

      forAllSystems = lib.genAttrs systems;
    in
    {
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

      formatter = forAllSystems (system: nixpkgs.legacyPackages.${system}.nixfmt);
    };
}
