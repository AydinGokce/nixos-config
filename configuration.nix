{
  config,
  lib,
  pkgs,
  ...
}:

{
  # Both machines run Mullvad + Tailscale; without this Mullvad's firewall
  # blocks the tailnet whenever the tunnel is up.
  imports = [ ./modules/mullvad-tailscale.nix ];

  # Use the systemd-boot EFI boot loader.
  boot.loader.systemd-boot.enable = false;

  networking.networkmanager.enable = true;

  time.timeZone = "America/New_York";

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
    package = pkgs.nixVersions.stable;
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
    extraGroups = [ "networkmanager" "wheel" "dialout" ];
  };

  environment.systemPackages = with pkgs; [
    acpi
    alacritty
    android-tools
    anki
    apfs-fuse
    appimage-run
    bc
    blueman
    bluez
    calibre
    cargo
    # OpenAI's desktop app (nixpkgs `chatgpt`) is macOS-only; give the web app
    # its own window and launcher entry via Chromium instead.
    (makeDesktopItem {
      name = "chatgpt";
      desktopName = "ChatGPT";
      exec = "chromium --app=https://chatgpt.com/";
      icon = "chatgpt";
      categories = [ "Network" "Chat" ];
    })
    chromium
    claude-code
    codex
    electrum
    element-desktop
    firebase-tools
    firefox
    git
    gnumake
    gparted
    htop
    kdePackages.dolphin
    keepassxc
    kicad
    killall
    libreoffice-qt  
    lm_sensors
    mullvad-vpn
    napari
    # neofetch (deprecated)
    hyfetch
    nixfmt-rfc-style
    nodejs_24
    opkg-utils
    probe-rs-tools
    psst
    # pymol
    qalculate-qt
    qbittorrent
    qemu
    signal-desktop
    steam
    tailscale
    tmux
    tree
    unzip
    usbutils
    vim
    vscode
    wget
    karere
    zip
  ] ++ lib.optionals (stdenv.isx86_64) [ discord logseq slack ];

  environment.sessionVariables = {
    TERM = "alacritty";
  };

  # Compressed swap in RAM: these machines run with no disk swap, so a single
  # memory spike (e.g. a from-source nix build) hard-freezes the box.
  zramSwap = {
    enable = true;
    memoryPercent = 50;
  };

  nixpkgs.config.allowUnfree = true;
  nixpkgs.config.permittedInsecurePackages = [
    "electron-39.8.10"
  ];

  services.resolved.enable = true;
  services.mullvad-vpn.enable = true;
  # Run the daemon from the same package as the CLI/GUI in systemPackages;
  # the default (pkgs.mullvad) can lag behind pkgs.mullvad-vpn, and a
  # version-skewed CLI fails to parse daemon settings ("invalid LWO
  # settings" gRPC errors on `mullvad lan get` etc.).
  services.mullvad-vpn.package = pkgs.mullvad-vpn;
  services.openssh.enable = true;
  services.flatpak.enable = true;
  services.tailscale.enable = true;

  xdg.portal.enable = true;
  xdg.portal.extraPortals = [ pkgs.kdePackages.xdg-desktop-portal-kde ];

  programs.dconf.enable = true;
  programs.bash.shellAliases = {
    jfu = "journalctl -fu";
  };

  programs.ssh.extraConfig = ''
    Host arm64-builder
      HostName 18.226.159.86
      User root
  '';

  networking.extraHosts = ''
    0.0.0.0 twitter.com
    0.0.0.0 x.com '';
    # survev.io
    # 0.0.0.0 a.thumbs.redditmedia.com
    # 0.0.0.0 about.reddit.com
    # 0.0.0.0 alb.reddit.com
    # 0.0.0.0 amp-reddit-com.cdn.ampproject.org
    # 0.0.0.0 b.thumbs.redditmedia.com
    # 0.0.0.0 c.thumbs.redditmedia.com
    # 0.0.0.0 d.thumbs.redditmedia.com
    # 0.0.0.0 e.reddit.com
    # 0.0.0.0 e.thumbs.redditmedia.com
    # 0.0.0.0 emoji.redditmedia.com
    # 0.0.0.0 en.reddit.com
    # 0.0.0.0 events.reddit.com
    # 0.0.0.0 external-preview.redd.it
    # 0.0.0.0 f.thumbs.redditmedia.com
    # 0.0.0.0 fr.reddit.com
    # 0.0.0.0 g.redditmedia.com
    # 0.0.0.0 gateway.reddit.com
    # 0.0.0.0 gp.reddit.com
    # 0.0.0.0 gql.reddit.com
    # 0.0.0.0 hw.reddit.com
    # 0.0.0.0 i.redd.it
    # 0.0.0.0 i.reddit.com
    # 0.0.0.0 i.redditmedia.com
    # 0.0.0.0 i.reddituploads.com
    # 0.0.0.0 it.reddit.com
    # 0.0.0.0 m.reddit.com
    # 0.0.0.0 meta-api.reddit.com
    # 0.0.0.0 mod.reddit.com
    # 0.0.0.0 new.reddit.com
    # 0.0.0.0 np.reddit.com
    # 0.0.0.0 oauth.reddit.com
    # 0.0.0.0 old.reddit.com
    # 0.0.0.0 oops.redditmedia.com
    # 0.0.0.0 out.reddit.com
    # 0.0.0.0 pixel.redditmedia.com
    # 0.0.0.0 preview.redd.it
    # 0.0.0.0 redd.it
    # 0.0.0.0 reddit-image.s3.amazonaws.com
    # 0.0.0.0 reddit-stream.com
    # 0.0.0.0 reddit-uploaded-media.s3-accelerate.amazonaws.com
    # 0.0.0.0 reddit-uploaded-video.s3-accelerate.amazonaws.com
    # 0.0.0.0 reddit.com
    # 0.0.0.0 reddit.map.fastly.net
    # 0.0.0.0 redditama.reddit.com
    # 0.0.0.0 redditblog.com
    # 0.0.0.0 redditgifts.com
    # 0.0.0.0 redditgifts.s3.amazonaws.com
    # 0.0.0.0 redditmedia.com
    # 0.0.0.0 s.reddit.com
    # 0.0.0.0 s.redditmedia.com
    # 0.0.0.0 sendbird.reddit.com
    # 0.0.0.0 ssl.reddit.com
    # 0.0.0.0 static.redditgifts.com
    # 0.0.0.0 stats.redditmedia.com
    # 0.0.0.0 strapi.reddit.com
    # 0.0.0.0 styles.redditmedia.com
    # 0.0.0.0 us.reddit.com
    # 0.0.0.0 v.redd.it
    # 0.0.0.0 wh.reddit.com
    # 0.0.0.0 www.np.reddit.com
    # 0.0.0.0 www.reddit-stream.com
    # 0.0.0.0 www.reddit.com
    # 0.0.0.0 www.redditblog.com
    # 0.0.0.0 www.redditgifts.com
    # 0.0.0.0 www.redditinc.com
    # 0.0.0.0 www.redditmedia.com
    # 0.0.0.0 www.redditstatic.com
}
