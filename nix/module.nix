self:
{
  config,
  lib,
  pkgs,
  ...
}:

let
  inherit (lib)
    escapeShellArg
    mkEnableOption
    mkIf
    mkOption
    optionals
    types
    ;

  cfg = config.services.pool-runners;
  seat = "%d/runners.toml";
in
{
  options.services.pool-runners = {
    enable = mkEnableOption "контроллер раннеров GitHub Actions на пуле";

    package = mkOption {
      type = types.package;
      default = self.packages.${pkgs.stdenv.hostPlatform.system}.default;
    };

    configFile = mkOption {
      type = types.nullOr types.path;
      default = null;
      example = "/run/secrets/runners.toml";
      description = "Конфиг с репами и токенами, читается root'ом до сброса прав.";
    };

    repos = mkOption {
      type = types.listOf types.str;
      default = [ ];
      example = [ "alice/app" ];
      description = "Репы для формы без конфига, токен тогда в environmentFile.";
    };

    environmentFile = mkOption {
      type = types.nullOr types.path;
      default = null;
      description = "Файл с GH_TOKEN, POOL_SERVER и POOL_CLIENT_TOKEN.";
    };

    settings = mkOption {
      type = types.attrsOf types.str;
      default = { };
      example = {
        RUNNERS_JOBS = "10";
        RUNNERS_DEBUG = "1";
      };
    };

    drainTimeout = mkOption {
      type = types.ints.unsigned;
      default = 180;
      description = "Сколько секунд дать на снос scale set и раннеров.";
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

  config = mkIf cfg.enable {
    assertions = [
      {
        assertion = (cfg.configFile != null) != (cfg.repos != [ ]);
        message = "services.pool-runners: или configFile, или repos, но не оба.";
      }
      {
        assertion = cfg.repos == [ ] || cfg.environmentFile != null;
        message = "services.pool-runners: форме с repos нужен environmentFile с GH_TOKEN.";
      }
    ];

    systemd.services.pool-runners = {
      description = "GitHub Actions runners on top of a pool";
      wantedBy = [ "multi-user.target" ];
      after = [ "network-online.target" ];
      wants = [ "network-online.target" ];
      environment = {
        OTEL_EXPORTER_OTLP_ENDPOINT = cfg.otlpEndpoint;
        ENV = cfg.env;
      }
      // cfg.settings;

      serviceConfig = {
        Type = "exec";
        DynamicUser = true;
        StateDirectory = "pool-runners";
        StateDirectoryMode = "0700";
        LoadCredential = optionals (cfg.configFile != null) [ "runners.toml:${cfg.configFile}" ];
        EnvironmentFile = optionals (cfg.environmentFile != null) [ cfg.environmentFile ];
        ExecStart =
          "${lib.getExe cfg.package} "
          + (
            if cfg.configFile != null then
              "-c ${seat}"
            else
              lib.concatMapStringsSep " " escapeShellArg cfg.repos
          );
        Restart = "always";
        RestartSec = "10s";
        TimeoutStopSec = "${toString (cfg.drainTimeout + 30)}s";

        AmbientCapabilities = [ "" ];
        CapabilityBoundingSet = [ "" ];
        DevicePolicy = "closed";
        LockPersonality = true;
        NoNewPrivileges = true;
        PrivateDevices = true;
        PrivateTmp = true;
        ProtectClock = true;
        ProtectControlGroups = true;
        ProtectHome = true;
        ProtectHostname = true;
        ProtectKernelLogs = true;
        ProtectKernelModules = true;
        ProtectKernelTunables = true;
        ProtectProc = "invisible";
        ProtectSystem = "strict";
        RestrictAddressFamilies = [
          "AF_INET"
          "AF_INET6"
          "AF_UNIX"
        ];
        RestrictNamespaces = true;
        RestrictRealtime = true;
        RestrictSUIDSGID = true;
        SystemCallArchitectures = "native";
        SystemCallFilter = [
          "@system-service"
          "~@privileged"
        ];
        UMask = "0077";
      };
    };
  };
}
