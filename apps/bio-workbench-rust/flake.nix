{
  description = "Bio Workbench — offline native Rust design prototype";
  inputs.nixpkgs.url = "github:NixOS/nixpkgs/8f0500b9660505dc3cb647775fe9a978a74b5283";
  outputs = { self, nixpkgs }:
    let
      systems = [ "x86_64-linux" "aarch64-linux" "x86_64-darwin" "aarch64-darwin" ];
      eachSystem = nixpkgs.lib.genAttrs systems;
    in {
      packages = eachSystem (system: let pkgs = import nixpkgs { inherit system; }; in rec {
        bio-workbench-rust = pkgs.callPackage ./package.nix { };
        default = bio-workbench-rust;
      });
      apps = eachSystem (system: rec {
        default = bio-workbench-rust;
        bio-workbench-rust = { type = "app"; program = "${self.packages.${system}.default}/bin/bio-workbench-rust"; };
      });
      devShells = eachSystem (system: let pkgs = import nixpkgs { inherit system; }; in {
        default = pkgs.mkShell {
          packages = [ pkgs.cargo pkgs.rustc pkgs.rustfmt pkgs.clippy pkgs.pkg-config ];
          LD_LIBRARY_PATH = pkgs.lib.makeLibraryPath (with pkgs; [ libGL libxkbcommon wayland libX11 libXcursor libXi libXrandr ]);
        };
      });
    };
}
