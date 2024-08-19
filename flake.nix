{
    description = "Aydin's Nix Config";

    inputs = {
        nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
        rust-overlay.url = "github:oxalica/rust-overlay";
        nix-flatpak.url = "github:gmodena/nix-flatpak/?ref=v0.4.1";
    };

    outputs = { nixpkgs, rust-overlay, nix-flatpak, ... }: {
        nixosConfigurations.nixos-asahi = nixpkgs.lib.nixosSystem {
            system = "aarch64-linux";
            modules = [
                ./configuration.nix
                nix-flatpak.nixosModules.nix-flatpak
                ({ pkgs, ... }: {
                    nixpkgs.overlays = [ rust-overlay.overlays.default ];
                    environment.systemPackages = [
                        (pkgs.rust-bin.stable.latest.default.override {
                            targets = [ "thumbv7em-none-eabi" ];
                        }) 
                    ];
                })
            ];
        };
    };
}
