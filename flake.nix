{
    description = "Aydin's Nix Config";

    inputs = {
        nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
        rust-overlay.url = "github:oxalica/rust-overlay";
        nix-flatpak.url = "github:gmodena/nix-flatpak/?ref=v0.4.1";
    };

    outputs = { nixpkgs, rust-overlay, nix-flatpak, ... }: 
    let 
        mkSystem = name: system: extraModules: extraConfig: nixpkgs.lib.nixosSystem {
            inherit system;
            modules = [
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
        };
    };
}
