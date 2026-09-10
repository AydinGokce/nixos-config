{
    description = "Aydin's Nix Config";

    inputs = {
        nixpkgs.url = "github:NixOS/nixpkgs/nixos-26.05";
        # codex on 26.05 lags upstream (0.146 as of 2026-09); unstable-small
        # carries 0.153+. Used only for the codex overlay below.
        nixpkgs-unstable.url = "github:NixOS/nixpkgs/nixos-unstable-small";
        rust-overlay.url = "github:oxalica/rust-overlay";
        nix-flatpak.url = "github:gmodena/nix-flatpak/?ref=v0.4.1";
        disko.url = "github:nix-community/disko";
        disko.inputs.nixpkgs.follows = "nixpkgs";
    };

    outputs = { nixpkgs, nixpkgs-unstable, rust-overlay, nix-flatpak, disko, ... }:
    let
        codexOverlay = final: prev: {
            codex = nixpkgs-unstable.legacyPackages.${prev.stdenv.hostPlatform.system}.codex;
        };
        mkSystem = name: system: extraModules: extraConfig: nixpkgs.lib.nixosSystem {
            inherit system;
            modules = [
                { nixpkgs.overlays = [ codexOverlay ]; }
                ./configuration.nix
                (./machines + "/${name}/configuration.nix")
                (./machines + "/${name}/hardware-configuration.nix")
            ] ++ extraModules ++ [ extraConfig ];
        };
    in {
        nixosConfigurations = {
            asahi-aarch64 = mkSystem "asahi-aarch64" "aarch64-linux" [
                nix-flatpak.nixosModules.nix-flatpak
                ({ pkgs, ... }: {
                    nixpkgs.overlays = [ rust-overlay.overlays.default ];
                    environment.systemPackages = [ pkgs.rust-bin.stable.latest.default ];
                })
            ] { };
            workstation-x86_64 = mkSystem "workstation-x86_64" "x86_64-linux" [] {};

            # DataCrunch always-on head / orchestrator node (headless server,
            # deliberately NOT via mkSystem so it skips the desktop config).
            # Installed onto a stock Ubuntu instance with nixos-anywhere.
            head = nixpkgs.lib.nixosSystem {
                system = "x86_64-linux";
                modules = [
                    disko.nixosModules.disko
                    ./machines/head/configuration.nix
                    ./machines/head/disko.nix
                ];
            };

            # generate an iso with `nix build .#nixosConfigurations.rescue-aarch64.config.system.build.isoImage`
            rescue-aarch64 = nixpkgs.lib.nixosSystem {
                system = "aarch64-linux";
                modules = [
                    ({ pkgs, modulesPath, ... }: {
                        imports = [ (modulesPath + "/installer/cd-dvd/installation-cd-minimal.nix") ];
                        environment.systemPackages = [ pkgs.neovim ];
                    })
                ];
            };
        };
    };
}
