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

  # The settings attrset drops nulls, so a url left unset simply leaves
  # GH_CHROME_DATABASE_URL out of the unit's environment and the server falls
  # back to a default database name of its own — one nothing here created.
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
      GH_CHROME_GITHUB_REPO = cfg.github.repo;
      GH_CHROME_GITHUB_WORKFLOW = cfg.github.workflow;
      GH_CHROME_GITHUB_REF = cfg.github.ref;
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

  # Where the database and the role are actually made moved between nixpkgs
  # releases; systemd ignores an After on a unit that is not there, so naming
  # both costs nothing and saves a first start against an empty cluster.
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
        Files holding the secrets (GH_CHROME_TOKEN, GH_CHROME_GITHUB_PAT and,
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

    github = {
      repo = mkOption {
        type = types.nullOr types.str;
        default = null;
        example = "wprhvso/gh-chrome";
        description = ''
          Repository the browser workflow is dispatched in. Left null when it
          travels with the PAT in an environment file.
        '';
      };

      workflow = mkOption {
        type = types.str;
        default = "chrome.yml";
        description = "Workflow that hosts a single browser session.";
      };

      ref = mkOption {
        type = types.str;
        default = "main";
        description = "Ref the workflow is dispatched on.";
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
          GH_CHROME_GITHUB_PAT; the server refuses to start without a token.
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

    # A database on its own is not enough to connect to: without a role for the
    # unit's user there is nobody to log in as, and without ownership the first
    # thing the server does — create its migrations table — is refused by any
    # PostgreSQL since 15.
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
