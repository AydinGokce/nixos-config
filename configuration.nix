{
  config,
  lib,
  pkgs,
  ...
}:

{
  imports = [
    ./hardware-configuration.nix
    ./apple-silicon-support
  ];

  # Use the systemd-boot EFI boot loader.
  boot.loader.systemd-boot.enable = true;
  boot.loader.efi.canTouchEfiVariables = false;

  networking.networkmanager.enable = true;

  time.timeZone = "America/Chicago";

  services.xserver.enable = true;
  services.xserver.desktopManager.xfce.enable = true;
  services.libinput.enable = true;
  services.xserver.windowManager.i3 = {
    enable = true;
    extraPackages = with pkgs; [
      dmenu
      i3status
      i3lock
    ];
  };

  hardware.asahi.peripheralFirmwareDirectory = ./firmware;
  hardware.bluetooth.enable = true;
  hardware.bluetooth.powerOnBoot = true;

  nix = {
    settings = {
      trusted-users = [
        "root"
        "aydin"
      ];
      auto-optimise-store = true;
      substituters = [ "https://cache.iog.io" ];
    };
    package = pkgs.nixFlakes;
    extraOptions = ''
      experimental-features = nix-command flakes
      builders-use-substitutes = true
    '';


    buildMachines = [ {
      hostName = "arm64-builder";
      system = "aarch64-linux";
      protocol = "ssh-ng";
      # if the builder supports building for multiple architectures, 
      # replace the previous line by, e.g.
      # systems = ["x86_64-linux" "aarch64-linux"];
      maxJobs = 1;
      speedFactor = 2;
      supportedFeatures = [ "nixos-test" "benchmark" "big-parallel" "kvm" ];
      mandatoryFeatures = [ ];
    }];
    distributedBuilds = true;
  };

  users.users.aydin = {
    isNormalUser = true;
    home = "/home/aydin";
    extraGroups = [ "wheel" ];
  };

  environment.systemPackages = with pkgs; [
    acpi
    alacritty
    anki
    apfs-fuse
    appimage-run
    bc
    blueman
    bluez
    calibre
    cargo
    # chromium
    electrum
    # firefox
    git
    gnumake
    htop
    keepassxc
    kicad
    mullvad-vpn
    neofetch
    nixfmt-rfc-style
    opkg-utils
    probe-rs
    psst
    qalculate-qt
    qbittorrent
    qemu
    signal-desktop
    tailscale
    tmux
    tree
    vim
    vscode
    wget
    x2goclient
    x2goserver
    zip
  ];

  environment.sessionVariables = {
    TERM = "alacritty";
  };

  nixpkgs.config.allowUnfree = true;

  services.resolved.enable = true;
  services.mullvad-vpn.enable = true;
  services.openssh.enable = true;
  services.blueman.enable = true;
  services.flatpak.enable = true;
  services.flatpak.remotes = lib.mkOptionDefault [
    {
      name = "flathub-beta";
      location = "https://flathub.org/beta-repo/flathub-beta.flatpakrepo";
    }
  ];
  services.flatpak.packages = [ "org.nanuc.Axolotl" ];
  services.tailscale.enable = true;

  xdg.portal.enable = true;
  xdg.portal.extraPortals = [ pkgs.xdg-desktop-portal-kde ];

  programs.dconf.enable = true;
  programs.bash.shellAliases = {
    jfu = "journalctl -fu";
    r = "sudo nixos-rebuild --flake ~/nixos-config/flake.nix#nixos-asahi";
  };

  programs.ssh.extraConfig = ''
    Host arm64-builder
      HostName 3.145.97.57
      User root
  '';

  system.stateVersion = "24.05";
}
