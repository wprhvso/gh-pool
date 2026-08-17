self:
{
  config,
  lib,
  pkgs,
  ...
}:
let
  inherit (lib) mkEnableOption mkIf mkOption types;
  cfg = config.services.pool.server;
in
{
  options.services.pool.server = {
    enable = mkEnableOption "pool server";

    package = mkOption {
      type = types.package;
      default = self.packages.${pkgs.stdenv.hostPlatform.system}.pool;
    };

    host = mkOption {
      type = types.str;
      default = "127.0.0.1";
    };

    port = mkOption {
      type = types.port;
      default = 8000;
    };

    dataDir = mkOption {
      type = types.str;
      default = "/var/lib/pool";
    };

    databaseUrl = mkOption {
      type = types.str;
      default = "postgresql+asyncpg://pool@/pool?host=/run/postgresql";
    };

    environmentFile = mkOption {
      type = types.nullOr types.path;
      default = null;
      description = "Файл с WORKER_TOKEN и CLIENT_TOKEN, читается systemd, в стор не попадает.";
    };

    settings = mkOption {
      type = types.attrsOf types.str;
      default = { };
      example = {
        FLUSH_EVERY = "0.2";
        LOST_AFTER = "300";
      };
    };

    postgresql = mkOption {
      type = types.bool;
      default = true;
      description = "Поднять локальный Postgres и завести в нём базу с ролью.";
    };

    openFirewall = mkOption {
      type = types.bool;
      default = false;
    };
  };

  config = mkIf cfg.enable {
    users.users.pool = {
      isSystemUser = true;
      group = "pool";
      home = cfg.dataDir;
    };
    users.groups.pool = { };

    services.postgresql = mkIf cfg.postgresql {
      enable = true;
      ensureDatabases = [ "pool" ];
      ensureUsers = [
        {
          name = "pool";
          ensureDBOwnership = true;
        }
      ];
    };

    systemd.tmpfiles.rules = [ "d ${cfg.dataDir} 0750 pool pool -" ];

    systemd.services.pool-server = {
      description = "pool server";
      wantedBy = [ "multi-user.target" ];
      after = [ "network.target" ] ++ lib.optional cfg.postgresql "postgresql.service";
      wants = lib.optional cfg.postgresql "postgresql.service";

      environment = {
        HOST = cfg.host;
        PORT = toString cfg.port;
        DATA_DIR = cfg.dataDir;
        DATABASE_URL = cfg.databaseUrl;
      }
      // cfg.settings;

      serviceConfig = {
        ExecStart = "${cfg.package}/bin/pool-server";
        EnvironmentFile = lib.optional (cfg.environmentFile != null) cfg.environmentFile;
        User = "pool";
        Group = "pool";
        WorkingDirectory = cfg.dataDir;
        ReadWritePaths = [ cfg.dataDir ];
        Restart = "always";
        RestartSec = 2;
        NoNewPrivileges = true;
        ProtectSystem = "strict";
        ProtectHome = true;
        PrivateTmp = true;
        PrivateDevices = true;
        ProtectKernelTunables = true;
        ProtectControlGroups = true;
        RestrictAddressFamilies = [
          "AF_INET"
          "AF_INET6"
          "AF_UNIX"
        ];
      };
    };

    networking.firewall.allowedTCPPorts = mkIf cfg.openFirewall [ cfg.port ];
  };
}
