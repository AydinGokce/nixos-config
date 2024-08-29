{
  config,
  lib,
  pkgs,
  ...
}:

{
  # Use the systemd-boot EFI boot loader.
  boot.loader.systemd-boot.enable = true;

  networking.networkmanager.enable = true;

  time.timeZone = "America/Chicago";

  # Select internationalisation properties.
  i18n.defaultLocale = "en_US.UTF-8";

  i18n.extraLocaleSettings = {
    LC_ADDRESS = "en_US.UTF-8";
    LC_IDENTIFICATION = "en_US.UTF-8";
    LC_MEASUREMENT = "en_US.UTF-8";
    LC_MONETARY = "en_US.UTF-8";
    LC_NAME = "en_US.UTF-8";
    LC_NUMERIC = "en_US.UTF-8";
    LC_PAPER = "en_US.UTF-8";
    LC_TELEPHONE = "en_US.UTF-8";
    LC_TIME = "en_US.UTF-8";
  };

  services.xserver.enable = true;
  services.xserver.desktopManager.xfce.enable = true;
  services.xserver.windowManager.i3 = {
    enable = true;
    extraPackages = with pkgs; [
      dmenu
      i3status
      i3lock
    ];
  };

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
    extraGroups = [ "networkmanager" "wheel" ];
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
    chromium
    electrum
    firefox
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
  ] ++ lib.optionals (stdenv.isx86_64) [ logseq ];

  environment.sessionVariables = {
    TERM = "alacritty";
  };

  nixpkgs.config.allowUnfree = true;
  nixpkgs.config.permittedInsecurePackages = [
    "electron-27.3.11"
  ];

  services.resolved.enable = true;
  services.mullvad-vpn.enable = true;
  services.openssh.enable = true;
  services.flatpak.enable = true;
  services.tailscale.enable = true;

  xdg.portal.enable = true;
  xdg.portal.extraPortals = [ pkgs.xdg-desktop-portal-kde ];

  programs.dconf.enable = true;
  programs.bash.shellAliases = {
    jfu = "journalctl -fu";
  };

  programs.ssh.extraConfig = ''
    Host arm64-builder
      HostName 3.145.97.57
      User root
  '';

  networking.extraHosts = ''
    0.0.0.0 twitter.com
    0.0.0.0 x.com
  '';
}
