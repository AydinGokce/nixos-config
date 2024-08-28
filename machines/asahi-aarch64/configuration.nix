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

  boot.loader.efi.canTouchEfiVariables = false;

  services.libinput.enable = true;

  hardware.asahi.peripheralFirmwareDirectory = ./firmware;
  hardware.bluetooth.enable = true;
  hardware.bluetooth.powerOnBoot = true;
  services.blueman.enable = true;

  services.flatpak.enable = true;
  services.flatpak.remotes = lib.mkOptionDefault [
    {
      name = "flathub-beta";
      location = "https://flathub.org/beta-repo/flathub-beta.flatpakrepo";
    }
  ];
  services.flatpak.packages = [];

  programs.bash.shellAliases = {
    r = "nixos-rebuild --flake .#asahi-aarch64 boot --use-remote-sudo" 
  };

  system.stateVersion = "24.05";
}
