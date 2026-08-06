{
  description = "gh-chrome";

  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";

  outputs = { self, nixpkgs }:
    let
      system = "x86_64-linux";
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
      devShells.${system}.default = pkgs.mkShell {
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
          LD_LIBRARY_PATH = pkgs.lib.makeLibraryPath libs;
          UV_PYTHON_DOWNLOADS = "never";
          GH_CHROME_CHROME_BINARY = "${pkgs.chromium}/bin/chromium";
        };

        shellHook = ''
          export PGDATA="$PWD/.pgdata"
          export PGHOST="$PWD/.pgsock"
          export DATABASE_URL="postgresql:///gh_chrome?host=$PGHOST"
        '';
      };
    };
}
