{
  description = "Bio Workbench — native cloud molecular modeling desktop";
  inputs.nixpkgs.url = "github:NixOS/nixpkgs/8f0500b9660505dc3cb647775fe9a978a74b5283";
  outputs = { self, nixpkgs }:
    let
      systems = [ "x86_64-linux" "aarch64-linux" "x86_64-darwin" "aarch64-darwin" ];
      eachSystem = nixpkgs.lib.genAttrs systems;
    in {
      packages = eachSystem (system: let pkgs = import nixpkgs { inherit system; }; in rec {
        bio-workbench-rust = pkgs.callPackage ./package.nix { };
        bio-workbench = bio-workbench-rust;
        bio-renderer = pkgs.callPackage ./renderer-package.nix { };
        default = bio-workbench-rust;
      });
      apps = eachSystem (system: rec {
        default = bio-workbench-rust;
        bio-workbench-rust = { type = "app"; program = "${self.packages.${system}.default}/bin/bio-workbench-rust"; };
        bio-workbench = { type = "app"; program = "${self.packages.${system}.default}/bin/bio-workbench"; };
        bio-render = { type = "app"; program = "${self.packages.${system}.bio-renderer}/bin/bio-render"; };
      });
      devShells = eachSystem (system: let pkgs = import nixpkgs { inherit system; }; in {
        default = pkgs.mkShell {
          packages = [ pkgs.cargo pkgs.rustc pkgs.rustfmt pkgs.clippy pkgs.pkg-config pkgs.openssh ]
            ++ pkgs.lib.optionals pkgs.stdenv.hostPlatform.isLinux [ pkgs.zenity ];
          LD_LIBRARY_PATH = pkgs.lib.optionalString pkgs.stdenv.hostPlatform.isLinux
            (pkgs.lib.makeLibraryPath (with pkgs; [ libGL libxkbcommon wayland libX11 libXcursor libXi libXrandr dbus ]));
        };
      });
    };
}
