{
    description = "Aydin's Nix Config";

    inputs = {
        nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
        rust-overlay.url = "github:oxalica/rust-overlay";
    };

    outputs = { nixpkgs, rust-overlay, ... }: {
        nixosConfigurations.nixos-asahi = nixpkgs.lib.nixosSystem {
            system = "aarch64-linux";
            modules = [
                ./configuration.nix # Your system configuration.
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