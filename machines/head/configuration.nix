# bio-head — always-on DataCrunch CPU node: SLURM controller / orchestrator that
# launches ephemeral GPU instances per job. Lean, headless server (deliberately
# NOT built through the desktop `mkSystem` path). Installed via nixos-anywhere.
{ config, lib, pkgs, modulesPath, ... }:

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
      export PATH=${lib.makeBinPath (with pkgs; [ curl jq openssh coreutils gawk util-linux gnugrep gnused ])}''${PATH:+:$PATH}
      ${builtins.readFile ./dc.sh}
    '')
  ];

  systemd.tmpfiles.rules = [
    "d /var/lib/dc            0700 root root - -"
    "d /root/.config         0700 root root - -"
    "d /root/.config/datacrunch 0700 root root - -"
    "d /root/.ssh            0700 root root - -"
  ];

  # Tailscale daemon (join the tailnet later with `tailscale up --auth-key=...`).
  services.tailscale.enable = true;

  time.timeZone = "UTC";
  system.stateVersion = "24.11";
}
