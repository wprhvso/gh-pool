self:
{
  config,
  lib,
  pkgs,
  ...
}:
let
  inherit (lib) mkEnableOption mkIf mkMerge mkOption types;
  cli = config.programs.pool;
  worker = config.services.pool.worker;
  keeper = config.services.pool.keeper;

  default = self.packages.${pkgs.stdenv.hostPlatform.system}.pool;

  packageOption = mkOption {
    type = types.package;
    inherit default;
  };

  serverOption = mkOption {
    type = types.str;
    default = "http://localhost:8000";
  };

  tokenFileOption = mkOption {
    type = types.nullOr types.path;
    default = null;
  };

  wrapper = pkgs.writeShellScriptBin "pool" ''
    export POOL_SERVER=${lib.escapeShellArg cli.server}
    ${lib.optionalString (cli.tokenFile != null) ''
      POOL_CLIENT_TOKEN=$(cat ${lib.escapeShellArg cli.tokenFile})
      export POOL_CLIENT_TOKEN
    ''}
    exec ${cli.package}/bin/pool "$@"
  '';
in
{
  options.programs.pool = {
    enable = mkEnableOption "pool cli";
    package = packageOption;
    server = serverOption;
    tokenFile = tokenFileOption // {
      description = "Файл с клиентским токеном, читается обёрткой при запуске.";
    };
  };

  options.services.pool.worker = {
    enable = mkEnableOption "pool worker";
    package = packageOption;
    server = serverOption;
    id = mkOption {
      type = types.str;
      default = "%H";
    };
    environmentFile = mkOption {
      type = types.nullOr types.path;
      default = null;
      description = "Файл с POOL_TOKEN.";
    };
    settings = mkOption {
      type = types.attrsOf types.str;
      default = { };
      example = {
        POOL_TASKS = "mypkg.tasks";
      };
    };
  };

  options.services.pool.keeper = {
    enable = mkEnableOption "pool keeper";
    package = packageOption;
    configFile = mkOption {
      type = types.path;
      description = "Конфиг с репами и токенами, см. keeper.toml.example.";
    };
  };

  config = mkMerge [
    (mkIf cli.enable {
      environment.systemPackages = [ wrapper ];
    })

    (mkIf worker.enable {
      systemd.services.pool-worker = {
        description = "pool worker";
        wantedBy = [ "multi-user.target" ];
        after = [ "network.target" ];

        environment = {
          POOL_SERVER = worker.server;
          WORKER_ID = worker.id;
          SPOOL_DIR = "/tmp";
          POOL_DEPS = "/var/cache/pool-worker/deps";
        }
        // worker.settings;

        serviceConfig = {
          ExecStart = "${worker.package}/bin/pool-worker";
          EnvironmentFile = lib.optional (worker.environmentFile != null) worker.environmentFile;
          DynamicUser = true;
          CacheDirectory = "pool-worker";
          PrivateTmp = true;
          Restart = "always";
          RestartSec = 5;
          KillSignal = "SIGTERM";
          TimeoutStopSec = 60;
        };
      };
    })

    (mkIf keeper.enable {
      systemd.services.pool-keeper = {
        description = "pool keeper";
        wantedBy = [ "multi-user.target" ];
        after = [ "network.target" ];

        serviceConfig = {
          ExecStart = "${keeper.package}/bin/pool-keeper run -c ${keeper.configFile}";
          DynamicUser = true;
          Restart = "always";
          RestartSec = 10;
          NoNewPrivileges = true;
          ProtectSystem = "strict";
          ProtectHome = true;
          PrivateTmp = true;
        };
      };
    })
  ];
}
