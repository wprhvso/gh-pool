self:
{
  config,
  lib,
  pkgs,
  ...
}:

let
  inherit (lib)
    filterAttrs
    literalExpression
    mkEnableOption
    mkIf
    mkOption
    optional
    optionals
    types
    ;

  cfg = config.services.gh-chrome;

  databaseUrl =
    if cfg.database.url != null then
      cfg.database.url
    else if cfg.database.createLocally then
      "postgresql:///${cfg.database.name}?host=/run/postgresql"
    else
      null;

  settings = filterAttrs (_name: value: value != null) (
    {
      GH_CHROME_HOST = cfg.host;
      GH_CHROME_PORT = toString cfg.port;
      GH_CHROME_PUBLIC_URL = cfg.publicUrl;
      GH_CHROME_STORAGE = cfg.storage;
      GH_CHROME_DATABASE_URL = databaseUrl;
      GH_CHROME_POOL_SERVER = cfg.pool.server;
      GH_CHROME_RUNNER_SPEC = cfg.pool.runnerSpec;
      GH_CHROME_HEARTBEAT_TIMEOUT = toString cfg.timeouts.heartbeat;
      GH_CHROME_READY_TIMEOUT = toString cfg.timeouts.ready;
      GH_CHROME_WATCHDOG_INTERVAL = toString cfg.watchdogInterval;
      GH_CHROME_SEGMENT_SECONDS = toString cfg.segmentSeconds;
    }
    // cfg.extraEnvironment
  );

  hardening = {
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
      "AF_NETLINK"
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
    User = cfg.user;
    Group = cfg.group;
  };

  baseAfter = [
    "network-online.target"
  ]
  ++ optionals cfg.database.createLocally [
    "postgresql.service"
    "postgresql-setup.service"
  ];
  baseRequires = optional cfg.database.createLocally "postgresql.service";
in
{
  options.services.gh-chrome = {
    enable = mkEnableOption "the gh-chrome session server";

    package = mkOption {
      type = types.package;
      default = self.packages.${pkgs.stdenv.hostPlatform.system}.default;
      defaultText = literalExpression "gh-chrome.packages.\${system}.default";
      description = "Package providing the server, the runner and the client CLI.";
    };

    user = mkOption {
      type = types.str;
      default = "gh-chrome";
      description = "User the server runs as, and owner of the storage directory.";
    };

    group = mkOption {
      type = types.str;
      default = "gh-chrome";
      description = "Group the server runs as.";
    };

    host = mkOption {
      type = types.str;
      default = "127.0.0.1";
      description = "Address the HTTP API binds to.";
    };

    port = mkOption {
      type = types.port;
      default = 8000;
      description = "Port the HTTP API binds to.";
    };

    openFirewall = mkOption {
      type = types.bool;
      default = false;
      description = "Whether to open the API port in the firewall.";
    };

    publicUrl = mkOption {
      type = types.str;
      default = "http://127.0.0.1:8000";
      description = "Origin the runner connects back to and share links are built from.";
    };

    storage = mkOption {
      type = types.path;
      default = "/var/lib/gh-chrome";
      description = "Directory session recordings, browser profiles and uploads live in.";
    };

    environmentFiles = mkOption {
      type = types.listOf types.path;
      default = [ ];
      example = [ "/var/lib/secrets/gh-chrome" ];
      description = ''
        Files holding the secrets (GH_CHROME_TOKEN, GH_CHROME_POOL_TOKEN and,
        unless `database.url` is set, GH_CHROME_DATABASE_URL). systemd applies
        EnvironmentFile after Environment, so values defined here override the
        generated environment.
      '';
    };

    extraEnvironment = mkOption {
      type = types.attrsOf types.str;
      default = { };
      description = "Extra environment variables merged into the generated environment.";
    };

    database = {
      createLocally = mkOption {
        type = types.bool;
        default = false;
        description = "Whether to create the database on the local PostgreSQL instance and order against it.";
      };

      name = mkOption {
        type = types.str;
        default = "gh-chrome";
        description = ''
          PostgreSQL database name. With createLocally it has to match
          services.gh-chrome.user, which is what makes that user its owner.
        '';
      };

      url = mkOption {
        type = types.nullOr types.str;
        default = null;
        example = "postgresql:///gh_chrome?host=/run/postgresql";
        description = ''
          libpq connection string of the session database. Left null when the
          password makes it a secret, in which case an environment file has to
          carry GH_CHROME_DATABASE_URL.
        '';
      };
    };

    pool = {
      server = mkOption {
        type = types.nullOr types.str;
        default = null;
        example = "http://127.0.0.1:8084";
        description = ''
          Base URL of the pool a session's runner is started in. Left null when
          it travels with the client token in an environment file.
        '';
      };

      runnerSpec = mkOption {
        type = types.nullOr types.str;
        default = null;
        example = "gh-chrome[runner]==0.2.1";
        description = ''
          Requirement the pool task installs before it starts the runner.
          Null follows the version of the server itself.
        '';
      };
    };

    timeouts = {
      heartbeat = mkOption {
        type = types.numbers.positive;
        default = 30.0;
        description = "Seconds without a runner heartbeat before its session is declared dead.";
      };

      ready = mkOption {
        type = types.numbers.positive;
        default = 600.0;
        description = "Seconds a dispatched session waits for its runner to come up.";
      };
    };

    watchdogInterval = mkOption {
      type = types.numbers.positive;
      default = 5.0;
      description = "Seconds between watchdog sweeps over the live sessions.";
    };

    segmentSeconds = mkOption {
      type = types.numbers.positive;
      default = 1.0;
      description = "Length of a single recorded video segment.";
    };
  };

  config = mkIf cfg.enable {
    assertions = [
      {
        assertion = cfg.environmentFiles != [ ];
        message = ''
          services.gh-chrome.environmentFiles must provide GH_CHROME_TOKEN and
          GH_CHROME_POOL_TOKEN; the server refuses to start without a token.
        '';
      }
      {
        assertion = !cfg.database.createLocally || cfg.database.name == cfg.user;
        message = ''
          services.gh-chrome.database.createLocally makes the service user the
          owner of the database it creates, and NixOS grants that only when the
          role and the database have the same name. Set
          services.gh-chrome.database.name to "${cfg.user}", or create the
          database yourself and point services.gh-chrome.database.url at it.
        '';
      }
    ];

    users.users = mkIf (cfg.user == "gh-chrome") {
      gh-chrome = {
        isSystemUser = true;
        inherit (cfg) group;
      };
    };

    users.groups = mkIf (cfg.group == "gh-chrome") { gh-chrome = { }; };

    services.postgresql = mkIf cfg.database.createLocally {
      enable = true;
      ensureDatabases = [ cfg.database.name ];
      ensureUsers = [
        {
          name = cfg.user;
          ensureDBOwnership = true;
        }
      ];
    };

    networking.firewall.allowedTCPPorts = optional cfg.openFirewall cfg.port;

    systemd.tmpfiles.rules = [ "d ${cfg.storage} 0750 ${cfg.user} ${cfg.group} -" ];

    systemd.services.gh-chrome = {
      description = "gh-chrome session server";
      wantedBy = [ "multi-user.target" ];
      after = baseAfter;
      wants = [ "network-online.target" ];
      requires = baseRequires;
      environment = settings;

      serviceConfig = hardening // {
        Type = "exec";
        StateDirectory = "gh-chrome";
        StateDirectoryMode = "0750";
        ReadWritePaths = [ cfg.storage ];
        EnvironmentFile = cfg.environmentFiles;
        ExecStart = "${cfg.package}/bin/gh-chrome-server";
        Restart = "always";
        RestartSec = "10s";
        TimeoutStopSec = "60s";
      };
    };
  };
}
