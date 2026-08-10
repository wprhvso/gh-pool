{
  lib,
  callPackage,
  callPackages,
  python313,
  pyproject-nix,
  uv2nix,
  pyproject-build-systems,
  sourcePreference ? "wheel",
}:

let
  root = ../.;

  workspace = uv2nix.lib.workspace.loadWorkspace { workspaceRoot = root; };

  sourceOverrides = _final: prev: {
    gh-chrome = prev.gh-chrome.overrideAttrs (old: {
      src = lib.fileset.toSource {
        root = old.src;
        fileset = lib.fileset.unions [
          (old.src + "/pyproject.toml")
          (lib.fileset.maybeMissing (old.src + "/README.md"))
          (old.src + "/src")
        ];
      };
    });
  };

  pythonSet = (callPackage pyproject-nix.build.packages { python = python313; }).overrideScope (
    lib.composeManyExtensions [
      pyproject-build-systems.overlays.wheel
      (workspace.mkPyprojectOverlay { inherit sourcePreference; })
      sourceOverrides
    ]
  );

  venv = pythonSet.mkVirtualEnv "gh-chrome-env" workspace.deps.default;

  inherit (callPackages pyproject-nix.build.util { }) mkApplication;
in
(mkApplication {
  inherit venv;
  package = pythonSet.gh-chrome;
}).overrideAttrs
  (old: {
    passthru = (old.passthru or { }) // {
      inherit venv;
      inherit (pythonSet.gh-chrome) version;
    };

    meta = (old.meta or { }) // {
      description = "Chrome on a GitHub Actions runner, driven over HTTPS";
      mainProgram = "gh-chrome-server";
      platforms = lib.platforms.linux;
    };
  })
