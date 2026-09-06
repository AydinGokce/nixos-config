# bio-head — always-on DataCrunch CPU node: orchestrator that
# launches ephemeral GPU instances per job. Lean, headless server (deliberately
# NOT built through the desktop `mkSystem` path). Installed via nixos-anywhere.
{ config, lib, pkgs, modulesPath, ... }:
let
  rfaaStorage = import ./rfaa-storage.nix;
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
      ${builtins.readFile ./dc.sh}
    '')
    (pkgs.writeShellScriptBin "bio-submit" ''
      export PATH=${lib.makeBinPath (with pkgs; [ rsync openssh coreutils gawk gnugrep gnused util-linux python3 gnutar gzip ])}:/run/current-system/sw/bin''${PATH:+:$PATH}
      ${builtins.readFile ./bio-submit.sh}
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
  ];

  # Ship the bio tool code (pinned requirements + helper CLIs from modules/bio)
  # to the head; bio-submit sends a verified snapshot to each GPU over SSH.
  environment.etc = {
    "bio-tools/dc-budget.py".source = ./dc-budget.py;
    "bio-tools/py".source = ../../modules/bio/py;                # esm_cli, rfaa patch, etc.
    "bio-tools/requirements".source = ../../modules/bio/requirements;
    "bio-tools/recipes".source = ./recipes;                     # per-tool bio-submit recipes
    "bio-tools/rfaa".source = ./rfaa;
    "bio-tools/cluster.sh".text = ''
      export RFAA_DB_VOLUME=${lib.escapeShellArg rfaaStorage.volumeId}
      export RFAA_DB_NFS=${lib.escapeShellArg rfaaStorage.nfs}
      export RFAA_DB_DIR=/mnt/bio-databases/rfaa
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
      TimeoutStartSec = 240;
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

  systemd.tmpfiles.rules = [
    "d /var/lib/dc            0700 root root - -"
    "d /root/.config         0700 root root - -"
    "d /root/.config/datacrunch 0700 root root - -"
    "d /root/.ssh            0700 root root - -"
    "d /var/lib/bio-runs     0755 root root - -"   # bio-submit pulls results here
  ];

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
    options = [ "nconnect=16" "x-systemd.automount" "noauto" "x-systemd.idle-timeout=600" ];
  };
  fileSystems."/mnt/bio-databases" = lib.mkIf (rfaaStorage.nfs != "") {
    device = rfaaStorage.nfs;
    fsType = "nfs";
    options = [ "nconnect=16" "x-systemd.automount" "noauto" ];
  };

  time.timeZone = "UTC";
  system.stateVersion = "24.11";
}
