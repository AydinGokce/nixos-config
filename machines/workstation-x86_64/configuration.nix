{ config, pkgs, ... }:

{
  imports =
    [
      ./hardware-configuration.nix
      ../../modules/bio
    ];

  # Protein-design / ML-biology toolkit (bio-* CLIs). Tuned for this box's
  # RTX 3070 (8GB); a cloud-GPU machine reuses modules/bio and raises these.
  bio = {
    enable = true;
    gpu.vramGB = 8;
    esm.defaultModel = "esm2_t33_650M_UR50D";
  };

  boot.loader.efi.canTouchEfiVariables = true;
  boot.loader.systemd-boot.enable = false;
  boot.loader.grub = {
  enable = true;
  device = "nodev";
  efiSupport = true;
  useOSProber = true;
};
  boot.loader.efi.efiSysMountPoint = "/boot/efi";

  networking.hostName = "nixos";

  services.xserver.videoDrivers = [ "nvidia" ];

  # Enable the XFCE Desktop Environment.
  services.xserver.displayManager.lightdm.enable = true;
  
  # Configure keymap in X11
  services.xserver = {
    xkb.layout = "us";
    xkb.variant = "";
  };

  # Enable CUPS to print documents.
  services.printing.enable = true;

  # Enable sound with pipewire.
  hardware.pulseaudio.enable = false;
  security.rtkit.enable = true;
  services.pipewire = {
    enable = true;
    alsa.enable = true;
    alsa.support32Bit = true;
    pulse.enable = true;
    # If you want to use JACK applications, uncomment this
    #jack.enable = true;

    # use the example session manager (no others are packaged yet so this is enabled by default,
    # no need to redefine it in your config for now)
    #media-session.enable = true;
  };

  hardware.nvidia.open=true;

  programs.bash.shellAliases = {
    r = "nixos-rebuild --flake .#workstation-x86_64 --use-remote-sudo";
  };

  # This value determines the NixOS release from which the default
  # settings for stateful data, like file locations and database versions
  # on your system were taken. It‘s perfectly fine and recommended to leave
  # this value at the release version of the first install of this system.
  # Before changing this value read the documentation for this option
  # (e.g. man configuration.nix or on https://nixos.org/nixos/options.html).
  system.stateVersion = "23.11"; # Did you read the comment?

}

