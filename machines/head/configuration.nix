# bio-head — always-on DataCrunch CPU node: orchestrator that
# launches ephemeral GPU instances per job. Lean, headless server (deliberately
# NOT built through the desktop `mkSystem` path). Installed via nixos-anywhere.
{ config, lib, pkgs, modulesPath, ... }:
let
  rfaaStorage = import ./rfaa-storage.nix;
  msaStorage = import ./msa-storage.nix;
  workbenchConfig = pkgs.writeText "bio-workbench-config.json" (builtins.toJSON {
    max_jobs = 10;
  });
in
{
  imports = [ (modulesPath + "/profiles/qemu-guest.nix") ]; # virtio drivers for KVM

  # Legacy-BIOS GRUB on the virtio disk (the instance has no EFI vars).
  boot.loader.grub = {
    enable = true;
    # disko's EF02 partition already registers /dev/vda; mkForce collapses to a
    # single entry so the two definitions don't trip the mirroredBoots assertion.
    devices = lib.mkForce [ "/dev/vda" ];
    efiSupport = false;
  };
  boot.loader.systemd-boot.enable = false;

  networking.hostName = "bio-head";
  networking.useDHCP = lib.mkDefault true;

  # Key-only SSH for root (nixos-anywhere + Aydin).
  services.openssh = {
    enable = true;
    settings.PermitRootLogin = "prohibit-password";
    settings.PasswordAuthentication = false;
  };
  users.users.root.openssh.authorizedKeys.keys = [
    "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIMZxDYvXnUamAyyZSFXM/3szlx8cSvGv2q8zEbeRfOAX datacrunch-automation"
    "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIKhgT1nfjBfMPyAw3EIJKM9WzhQ57/Vl5JjDpvCUu135 aydin@nixos"
    # Harrison gets the actor-scoped workbench RPC only, with no shell/forwarding.
    ''restrict,command="env BIO_WORKBENCH_ACTOR=harrison /run/current-system/sw/bin/bio-workbench rpc" ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIAy2tj+Uoq/hQ3C9VAb3Y4eTJp6rEIGSqqTZB4krNm0D harrison-bio-workbench''
  ];

  nix.settings.experimental-features = [ "nix-command" "flakes" ];
  nix.settings.trusted-users = [ "root" ];

  # Orchestration toolbox + the `dc` ephemeral-GPU CLI (reads creds from
  # /root/.config/datacrunch/credentials.env, ledgers spend under /var/lib/dc).
  environment.systemPackages = (with pkgs; [
    git curl wget jq tmux vim htop rsync openssh uv python3 tailscale util-linux
  ]) ++ [
    (pkgs.writeShellScriptBin "dc" ''
      export PATH=${lib.makeBinPath (with pkgs; [ curl jq openssh coreutils gawk util-linux gnugrep gnused python3 ])}''${PATH:+:$PATH}
      # Let provider cleanup settle across all submission controllers.
      export DC_LAUNCH_COOLDOWN_SECONDS="''${DC_LAUNCH_COOLDOWN_SECONDS-180}"
      ${builtins.readFile ./dc.sh}
    '')
    (pkgs.writeShellScriptBin "bio-submit" ''
      export PATH=${lib.makeBinPath (with pkgs; [ rsync openssh coreutils gawk gnugrep gnused util-linux python3 gnutar gzip ])}:/run/current-system/sw/bin''${PATH:+:$PATH}
      ${builtins.readFile ./bio-submit.sh}
    '')
    (pkgs.writeShellScriptBin "bio-library" ''
      export PATH=${lib.makeBinPath [ pkgs.systemd pkgs.python3 ]}:/run/current-system/sw/bin''${PATH:+:$PATH}
      umask 077
      exec ${pkgs.python3}/bin/python3 /etc/bio-tools/library/cli.py "$@"
    '')
    (pkgs.writeShellScriptBin "bio-workbench" ''
      export PATH=${lib.makeBinPath (with pkgs; [ python3 systemd openssh rsync coreutils util-linux ])}:/run/current-system/sw/bin''${PATH:+:$PATH}
      umask 077
      exec ${pkgs.python3}/bin/python3 /etc/bio-tools/workbench/cli.py "$@"
    '')
    (pkgs.writeShellScriptBin "bio-inference" ''
      set -euo pipefail
      export PATH=${lib.makeBinPath (with pkgs; [ python3 systemd openssh rsync coreutils util-linux ])}:/run/current-system/sw/bin''${PATH:+:$PATH}
      export BIO_INFERENCE_CONTROL_SOURCE=nfs.fin-02.datacrunch.io:/bio-shared-G523CVN6KYMH/inference-control
      for inference_arg in "$@"; do
        if [ "$inference_arg" = worker-start ]; then
          source "''${DC_CREDENTIALS_FILE:-/root/.config/datacrunch/credentials.env}"
          export DATACRUNCH_CLIENT_ID DATACRUNCH_CLIENT_SECRET
          break
        fi
      done
      exec ${pkgs.python3}/bin/python3 /etc/bio-tools/inference/cli.py "$@"
    '')
    (pkgs.writeShellScriptBin "bio-rfaa-databases" ''
      export PATH=${lib.makeBinPath (with pkgs; [ python3 curl gnutar gzip coreutils util-linux ])}''${PATH:+:$PATH}
      exec python3 /etc/bio-tools/rfaa/databases.py "$@"
    '')
    (pkgs.writeShellScriptBin "bio-rfaa-storage" ''
      set -euo pipefail
      export PATH=${lib.makeBinPath (with pkgs; [ python3 systemd util-linux coreutils ])}:/run/current-system/sw/bin''${PATH:+:$PATH}
      # Local receipt checks need no credentials; lifecycle operations use the
      # same private credential file as dc and never print its contents.
      case "''${1:-}" in
        register|expire)
          source "''${DC_CREDENTIALS_FILE:-/root/.config/datacrunch/credentials.env}"
          export DATACRUNCH_CLIENT_ID DATACRUNCH_CLIENT_SECRET ;;
      esac
      exec python3 /etc/bio-tools/rfaa/storage.py "$@"
    '')
    (pkgs.writeShellScriptBin "bio-msa-storage" ''
      exec /run/current-system/sw/bin/bio-rfaa-storage "$@" --profile colabfold
    '')
    (pkgs.writeShellScriptBin "bio-database-volume" ''
      set -euo pipefail
      source "''${DC_CREDENTIALS_FILE:-/root/.config/datacrunch/credentials.env}"
      export DATACRUNCH_CLIENT_ID DATACRUNCH_CLIENT_SECRET
      export DC_HELPER=/etc/bio-tools/dc-budget.py
      export DATABASE_STORAGE_HELPER=/etc/bio-tools/rfaa/storage.py
      exec ${pkgs.python3}/bin/python3 /etc/bio-tools/database-volume.py "$@"
    '')
    (pkgs.writeShellScriptBin "bio-msa" ''
      export PATH=${lib.makeBinPath (with pkgs; [ python3 coreutils ])}:/run/current-system/sw/bin''${PATH:+:$PATH}
      ${builtins.readFile ./bio-msa.sh}
    '')
    (pkgs.writeShellScriptBin "bio-msa-worker" ''
      set -euo pipefail
      source "''${DC_CREDENTIALS_FILE:-/root/.config/datacrunch/credentials.env}"
      export DATACRUNCH_CLIENT_ID DATACRUNCH_CLIENT_SECRET
      exec ${pkgs.python3}/bin/python3 /etc/bio-tools/msa/worker.py "$@"
    '')
    (pkgs.writeShellScriptBin "bio-msa-build-queue" ''
      set -euo pipefail
      export PATH=${lib.makeBinPath (with pkgs; [ python3 systemd util-linux coreutils ])}:/run/current-system/sw/bin''${PATH:+:$PATH}
      # Only ticks consult provider availability and reconcile cloud inventory.
      for queue_arg in "$@"; do
        if [ "$queue_arg" = tick ]; then
          source "''${DC_CREDENTIALS_FILE:-/root/.config/datacrunch/credentials.env}"
          export DATACRUNCH_CLIENT_ID DATACRUNCH_CLIENT_SECRET
          break
        fi
      done
      exec python3 /etc/bio-tools/msa/build-queue.py "$@"
    '')
  ];

  # Ship the bio tool code (pinned requirements + helper CLIs from modules/bio)
  # to the head; bio-submit sends a verified snapshot to each GPU over SSH.
  environment.etc = {
    "bio-tools/dc-budget.py".source = ./dc-budget.py;
    "bio-tools/database-volume.py".source = ./database-volume.py;
    "bio-tools/py".source = ../../modules/bio/py;                # esm_cli, rfaa patch, etc.
    "bio-tools/requirements".source = ../../modules/bio/requirements;
    "bio-tools/recipes".source = ./recipes;                     # per-tool bio-submit recipes
    "bio-tools/rfaa".source = ./rfaa;
    "bio-tools/rf3".source = ./rf3;
    "bio-tools/msa".source = ./msa;
    "bio-tools/library".source = ./library;
    "bio-tools/workbench".source = ./workbench;
    # Bursty interactive use: allocate temporary workers for up to ten jobs,
    # then return to zero GPU workers as their managed runs finish.
    "bio-tools/workbench-config.json".source = workbenchConfig;
    "bio-tools/inference".source = ./inference;
    "bio-tools/library-runtime.json".text = builtins.toJSON {
      python = "${pkgs.python312}/bin/python3.12";
      path = lib.makeBinPath [ pkgs.git pkgs.coreutils ];
      shared = "/mnt/bio-shared";
      library_paths = [ "${pkgs.stdenv.cc.cc.lib}/lib" "${pkgs.zlib}/lib"
        "${pkgs.libxrender}/lib" "${pkgs.libxext}/lib" "${pkgs.libx11}/lib" "${pkgs.expat}/lib" ];
      rfaa_config = "/var/lib/bio-library-runtime/rfaa.json";
    };
    "bio-tools/cluster.sh".text = ''
      export RFAA_DB_VOLUME=${lib.escapeShellArg rfaaStorage.volumeId}
      export RFAA_DB_NFS=${lib.escapeShellArg rfaaStorage.nfs}
      export RFAA_DB_DIR=/mnt/bio-databases/rfaa
      export MSA_DB_VOLUME=${lib.escapeShellArg msaStorage.volumeId}
      export MSA_DB_NFS=${lib.escapeShellArg msaStorage.nfs}
      export MSA_DB_ROOT=/mnt/bio-msa-databases/colabfold
      export BIO_MSA_DEFAULT_BACKEND=public
      export BIO_PUBLIC_MSA_LOCK=/mnt/bio-shared/coordination/public-msa.lock
    '';
  };

  # The launcher refuses new jobs if this monitor stops running. It reconciles
  # provider inventory/cost estimates and removes expired managed GPU workers.
  systemd.services.dc-budget-watchdog = {
    description = "Reconcile cloud spending and expire temporary compute";
    after = [ "network-online.target" ];
    wants = [ "network-online.target" ];
    serviceConfig = {
      Type = "oneshot";
      ExecStart = "/run/current-system/sw/bin/dc watchdog";
      # Burst expiry requests are issued across workers first; allow the
      # subsequent bounded instance/OS confirmations to finish for the batch.
      TimeoutStartSec = 900;
    };
  };
  systemd.timers.dc-budget-watchdog = {
    wantedBy = [ "timers.target" ];
    timerConfig = {
      OnBootSec = "30s";
      OnUnitActiveSec = "60s";
      AccuracySec = "5s";
    };
  };

  # No receipt means no work. Once an allocation is registered, the persistent
  # timer catches overdue storage after a reboot and retries incomplete cleanup.
  systemd.services.rfaa-storage-expiry = {
    description = "Expire the registered RFAA database volume";
    after = [ "network-online.target" ];
    wants = [ "network-online.target" ];
    serviceConfig = {
      Type = "oneshot";
      ExecStart = "/run/current-system/sw/bin/bio-rfaa-storage expire";
      TimeoutStartSec = 240;
    };
  };
  systemd.timers.rfaa-storage-expiry = {
    wantedBy = [ "timers.target" ];
    timerConfig = {
      OnCalendar = "*-*-* *:*:00";
      Persistent = true;
      AccuracySec = "1s";
    };
  };
  systemd.services.msa-storage-expiry = {
    description = "Reconcile explicit retirement of ColabFold database storage";
    after = [ "network-online.target" ];
    wants = [ "network-online.target" ];
    serviceConfig = {
      Type = "oneshot";
      ExecStart = "/run/current-system/sw/bin/bio-msa-storage expire";
      TimeoutStartSec = 240;
    };
  };
  systemd.timers.msa-storage-expiry = {
    wantedBy = [ "timers.target" ];
    timerConfig = {
      OnCalendar = "*-*-* *:*:00";
      Persistent = true;
      AccuracySec = "1s";
    };
  };

  # Explicit queue initialization freezes the requested panel and volume. Until
  # then the timer is inert; downloads, capacity and cleanup gate every rental.
  systemd.services.bio-msa-build-queue = {
    description = "Advance the registered private MSA database build and panel";
    after = [ "network-online.target" ];
    wants = [ "network-online.target" ];
    unitConfig.ConditionPathExists = "/var/lib/dc/msa-build-queue.json";
    environment.BIO_MSA_QUEUE_TOOLS_PIN = "/var/lib/bio-runs/msa-recent-public-inference-20260906/frozen-bio-tools/pin.json";
    serviceConfig = {
      Type = "oneshot";
      ExecStart = "/run/current-system/sw/bin/bio-msa-build-queue tick";
      TimeoutStartSec = 600;
      UMask = "0077";
    };
  };
  systemd.timers.bio-msa-build-queue = {
    wantedBy = [ "timers.target" ];
    timerConfig = {
      OnCalendar = "*-*-* *:00/15:00";
      Persistent = true;
      AccuracySec = "1min";
    };
  };

  # One explicitly registered full-database validation. Its private artifact
  # records durable intent before submission and never automatically retries.
  systemd.services.bio-rfaa-validation-trigger = {
    # RFAA is parked while RF3 and the other production tools are completed.
    enable = false;
    description = "Observe or submit the registered full RFAA validation once";
    after = [ "network-online.target" ];
    wants = [ "network-online.target" ];
    path = [ pkgs.systemd pkgs.util-linux pkgs.coreutils ];
    unitConfig.ConditionPathExists = "/var/lib/dc/rfaa-validation-trigger/config.json";
    serviceConfig = {
      Type = "oneshot";
      ExecStart = "${pkgs.python3}/bin/python3 /var/lib/dc/rfaa-validation-trigger/trigger.py tick --config /var/lib/dc/rfaa-validation-trigger/config.json";
      TimeoutStartSec = 300;
      UMask = "0077";
    };
  };
  systemd.timers.bio-rfaa-validation-trigger = {
    enable = false;
    wantedBy = [ "timers.target" ];
    timerConfig = {
      OnCalendar = "*-*-* *:00/15:00";
      Persistent = true;
      AccuracySec = "1min";
    };
  };

  systemd.tmpfiles.rules = [
    "d /var/lib/bio-workbench 0700 root root - -"
    "d /var/lib/bio-inference 0700 root root - -"
    "d /var/lib/bio-library 0700 root root - -"
    "d /var/lib/bio-library-runtime 0700 root root - -"
    "d /var/lib/dc            0700 root root - -"
    "d /var/lib/dc/rfaa-validation-trigger 0700 root root - -"
    "d /root/.config         0700 root root - -"
    "d /root/.config/datacrunch 0700 root root - -"
    "d /root/.ssh            0700 root root - -"
    "d /var/lib/bio-runs     0755 root root - -"   # bio-submit pulls results here
  ];

  systemd.services.bio-inference = {
    description = "Dispatch durable model requests and CPU postprocessing";
    restartTriggers = [ ./inference ];
    wantedBy = [ "multi-user.target" ];
    after = [ "network-online.target" "systemd-tmpfiles-setup.service" ];
    wants = [ "network-online.target" ];
    serviceConfig = {
      Type = "simple";
      ExecStart = "/run/current-system/sw/bin/bio-inference serve";
      Restart = "on-failure";
      RestartSec = 5;
      KillMode = "control-group";
      TimeoutStopSec = 30;
      UMask = "0077";
    };
  };

  # SSH reverse forwards expose this loopback-only TLS tunnel on temporary
  # workers. Native search clients serialize whole queries on the shared lock;
  # inference continues in parallel after each search releases that lock.
  systemd.services.bio-public-msa-proxy = {
    description = "Single head egress for native public MSA searches";
    wantedBy = [ "multi-user.target" ];
    after = [ "network-online.target" ];
    wants = [ "network-online.target" ];
    environment.PYTHONDONTWRITEBYTECODE = "1";
    serviceConfig = {
      ExecStart = "${pkgs.python3}/bin/python3 ${./msa/public_proxy.py}";
      DynamicUser = true;
      Restart = "on-failure";
      RestartSec = 3;
      NoNewPrivileges = true;
      PrivateTmp = true;
      ProtectSystem = "strict";
      ProtectHome = true;
      RestrictAddressFamilies = [ "AF_INET" "AF_INET6" ];
    };
  };

  systemd.services.bio-workbench = {
    description = "Durable desktop and Harrison molecular model jobs";
    restartTriggers = [ ./workbench workbenchConfig ];
    wantedBy = [ "multi-user.target" ];
    after = [ "network-online.target" "systemd-tmpfiles-setup.service" "bio-public-msa-proxy.service" ];
    wants = [ "network-online.target" "bio-public-msa-proxy.service" ];
    environment.BIO_WORKBENCH_CONFIG = "/etc/bio-tools/workbench-config.json";
    serviceConfig = {
      Type = "simple";
      ExecStart = "/run/current-system/sw/bin/bio-workbench daemon";
      Restart = "on-failure";
      RestartSec = 5;
      TimeoutStopSec = 30;
      UMask = "0077";
    };
  };

  systemd.services.bio-library-init = {
    description = "Initialize the authoritative construct library";
    wantedBy = [ "multi-user.target" ];
    after = [ "systemd-tmpfiles-setup.service" ];
    serviceConfig = {
      Type = "oneshot";
      ExecStart = "/run/current-system/sw/bin/bio-library init";
      UMask = "0077";
    };
  };

  # Tailscale daemon (join the tailnet later with `tailscale up --auth-key=...`).
  services.tailscale.enable = true;

  # Shared NFS (DataCrunch NVMe_Shared volume "bio-shared", FIN-02) for model
  # envs / weights / databases / run outputs — mounted here and on ephemeral GPU
  # nodes. automount + noauto so the head still boots if the share is detached.
  # NOTE: the export path is specific to this volume; update if it's recreated.
  boot.supportedFilesystems = [ "nfs" ];
  fileSystems."/mnt/bio-shared" = {
    device = "nfs.fin-02.datacrunch.io:/bio-shared-G523CVN6KYMH";
    fsType = "nfs";
    # This head/provider pair returned zero-filled reads over NFS 4.2. The
    # identical files read correctly over 4.1; pin it for every shared mount.
    options = [ "vers=4.1" "nconnect=16" "x-systemd.automount" "noauto" "x-systemd.idle-timeout=600" ];
  };
  # Only queue/control artifacts use this subtree, through this mount on every
  # client. Its separate metadata cache avoids NFS's 30-second negative lookups.
  fileSystems."/mnt/bio-inference-control" = {
    device = "nfs.fin-02.datacrunch.io:/bio-shared-G523CVN6KYMH/inference-control";
    fsType = "nfs";
    options = [ "vers=4.1" "nconnect=16" "hard" "nosharecache" "lookupcache=none" "actimeo=0"
      "nofail" "_netdev" ];
  };
  fileSystems."/mnt/bio-databases" = lib.mkIf (rfaaStorage.nfs != "") {
    device = rfaaStorage.nfs;
    fsType = "nfs";
    options = [ "vers=4.1" "nconnect=16" "x-systemd.automount" "noauto" ];
  };
  fileSystems."/mnt/bio-msa-databases" = lib.mkIf (msaStorage.nfs != "") {
    device = msaStorage.nfs;
    fsType = "nfs";
    options = [ "vers=4.1" "nconnect=16" "x-systemd.automount" "noauto" ];
  };

  time.timeZone = "UTC";
  system.stateVersion = "24.11";
}
