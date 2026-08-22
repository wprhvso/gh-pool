self:
{
  config,
  lib,
  pkgs,
  ...
}:
let
  inherit (lib)
    mkEnableOption
    mkIf
    mkMerge
    mkOption
    types
    ;
  cli = config.programs.pool;
  worker = config.services.pool.worker;
  keeper = config.services.pool.keeper;

  default = self.packages.${pkgs.stdenv.hostPlatform.system}.gh-pool;

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
    export GH_POOL_SERVER=${lib.escapeShellArg cli.server}
    ${lib.optionalString (cli.tokenFile != null) ''
      if [ ! -r ${lib.escapeShellArg cli.tokenFile} ]; then
        echo "pool: не читается ${toString cli.tokenFile}" >&2
        exit 1
      fi
      GH_POOL_CLIENT_TOKEN=$(cat ${lib.escapeShellArg cli.tokenFile})
      export GH_POOL_CLIENT_TOKEN
    ''}
    exec ${cli.package}/bin/gh-pool "$@"
  '';

  keeperConfig = "%d/keeper.toml";

  keeperBuild = "${keeper.package}/bin/gh-pool-keeper build -c ${keeperConfig} --source ${keeper.workflows}";
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
      description = "Файл с GH_POOL_WORKER_TOKEN.";
    };
    settings = mkOption {
      type = types.attrsOf types.str;
      default = { };
      example = {
        GH_POOL_TASKS = "mypkg.tasks";
      };
    };
    otlpEndpoint = mkOption {
      type = types.str;
      default = "http://127.0.0.1:4317";
      description = "OTLP gRPC endpoint, куда уходят трейсы, метрики и логи.";
    };
    env = mkOption {
      type = types.str;
      default = "prod";
      description = "Окружение в ресурсных атрибутах телеметрии.";
    };
  };

  options.services.pool.keeper = {
    enable = mkEnableOption "pool keeper";
    package = packageOption;
    configFile = mkOption {
      type = types.path;
      description = "Конфиг с репами и токенами, см. keeper.toml.example.";
    };
    build = mkOption {
      type = types.bool;
      default = false;
      description = "Раскладывать по репам воркфлоу и секреты GH_POOL_SERVER/GH_POOL_WORKER_TOKEN перед запуском.";
    };
    workflows = mkOption {
      type = types.path;
      default = self + "/.github/workflows";
      description = "Каталог с воркфлоу, откуда build берёт файл по имени из конфига.";
    };
    otlpEndpoint = mkOption {
      type = types.str;
      default = "http://127.0.0.1:4317";
      description = "OTLP gRPC endpoint, куда уходят трейсы, метрики и логи.";
    };
    env = mkOption {
      type = types.str;
      default = "prod";
      description = "Окружение в ресурсных атрибутах телеметрии.";
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
        after = [ "network-online.target" ];
        wants = [ "network-online.target" ];

        environment = {
          GH_POOL_SERVER = worker.server;
          GH_POOL_WORKER_ID = worker.id;
          GH_POOL_SPOOL_DIR = "/tmp";
          GH_POOL_DEPS = "/var/cache/pool-worker/deps";
          OTEL_EXPORTER_OTLP_ENDPOINT = worker.otlpEndpoint;
          ENV = worker.env;
        }
        // worker.settings;

        serviceConfig = {
          ExecStart = "${worker.package}/bin/gh-pool-worker";
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
        after = [ "network-online.target" ];
        wants = [ "network-online.target" ];

        environment = {
          OTEL_EXPORTER_OTLP_ENDPOINT = keeper.otlpEndpoint;
          ENV = keeper.env;
        };

        serviceConfig = {
          ExecStartPre = lib.optional keeper.build keeperBuild;
          ExecStart = "${keeper.package}/bin/gh-pool-keeper run -c ${keeperConfig}";
          LoadCredential = [ "keeper.toml:${keeper.configFile}" ];
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
