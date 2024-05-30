{ config, lib, pkgs, ... }:

{
  imports =
    [
      ./hardware-configuration.nix
      ./apple-silicon-support
    ];

  # Use the systemd-boot EFI boot loader.
  boot.loader.systemd-boot.enable = true;
  boot.loader.efi.canTouchEfiVariables = false;

  networking.wireless.iwd = {
    enable = true;
    settings.General.EnableNetworkConfiguration = true;
  };

  time.timeZone = "America/Chicago";

  services.xserver.enable = true;
  services.xserver.desktopManager.xfce.enable = true;
  services.xserver.libinput.enable = true;
  services.xserver.windowManager.i3 = {
    enable = true;
    extraPackages = with pkgs; [
      dmenu
      i3status
      i3lock
    ];	
  };

  sound.enable = true;

  hardware.asahi.peripheralFirmwareDirectory = ./firmware;
  hardware.bluetooth.enable = true;
  hardware.bluetooth.powerOnBoot = true;
  
  nix = {
    settings = {
      trusted-users = [ "root" "aydin" ];
      auto-optimise-store = true;
      substituters = [ "https://cache.iog.io"  ];
    };
    package = pkgs.nixFlakes;
    extraOptions = ''
      experimental-features = nix-command flakes
    '';
  };

  users.users.aydin = {
    isNormalUser = true;
    home = "/home/aydin";
    extraGroups = [ "wheel" ];
  };

  environment.systemPackages = with pkgs; [
    vim
    wget
    firefox
    tree
    neofetch
    signal-desktop
    vscode
    alacritty
    chromium
    appimage-run
    opkg-utils
    tmux
    acpi
    bc
    blueman
    bluez
    calibre
    evince
    psst
    git
    cargo
    probe-rs
    anki
    qemu
    zip
  ];

  environment.sessionVariables = {
    TERM = "alacritty";
  };

  nixpkgs.config.allowUnfree = true;

  services.openssh.enable = true;
  services.blueman.enable = true;
  services.flatpak.enable = true;

  xdg.portal.enable = true; 
  xdg.portal.extraPortals = [ pkgs.xdg-desktop-portal-kde ]; 
 
  programs.dconf.enable = true;
  programs.bash.shellAliases = {
    jfu = "journalctl -fu";
    rs = "sudo nixos-rebuild --flake ~/nixos-config/flake.nix#nixos-asahi switch";
  };

  system.stateVersion = "24.05";
}
