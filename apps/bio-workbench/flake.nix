{
  description = "Bio Workbench — Electron desktop for cloud molecular models";
  inputs.nixpkgs.url = "github:NixOS/nixpkgs/8f0500b9660505dc3cb647775fe9a978a74b5283";
  outputs = { self, nixpkgs }:
    let
      systems = [ "x86_64-linux" "aarch64-linux" "x86_64-darwin" "aarch64-darwin" ];
      eachSystem = nixpkgs.lib.genAttrs systems;
    in {
      packages = eachSystem (system: let pkgs = import nixpkgs { inherit system; }; in rec {
        bio-workbench = pkgs.callPackage ./package.nix { };
        default = bio-workbench;
      });
      apps = eachSystem (system: rec {
        default = bio-workbench;
        bio-workbench = { type = "app"; program = "${self.packages.${system}.default}/bin/bio-workbench"; };
      });
      devShells = eachSystem (system: let pkgs = import nixpkgs { inherit system; }; in {
        default = pkgs.mkShell {
          packages = [ pkgs.nodejs pkgs.python3 pkgs.openssh pkgs.electron ];
          shellHook = ''
            export BIO_DESKTOP_PYTHON=${pkgs.python3}/bin/python3
            export BIO_SSH=${pkgs.openssh}/bin/ssh
          '';
        };
      });
    };
}
