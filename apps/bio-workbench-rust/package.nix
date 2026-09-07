{ lib, rustPlatform, makeWrapper, libGL, libxkbcommon, wayland, libX11, libXcursor, libXi, libXrandr, stdenv, makeDesktopItem, pymol }:
let
  desktopItem = makeDesktopItem {
    name = "bio-workbench-rust";
    desktopName = "Bio Workbench — Rust design prototype";
    comment = "Offline native interface design; no cloud execution";
    exec = "bio-workbench-rust";
    terminal = false;
    categories = [ "Science" "Biology" ];
  };
in rustPlatform.buildRustPackage {
  pname = "bio-workbench-rust";
  version = "0.2.0";
  src = lib.cleanSourceWith {
    src = ./.;
    filter = path: type: !(builtins.elem (baseNameOf path) [ "target" "result" ".git" ]) && !(lib.hasSuffix ".png" path);
  };
  cargoLock.lockFile = ./Cargo.lock;
  nativeBuildInputs = [ makeWrapper ];
  postInstall = lib.optionalString stdenv.hostPlatform.isLinux ''
    wrapProgram $out/bin/bio-workbench-rust \
      --set-default BIO_WORKBENCH_PYMOL ${lib.getExe pymol} \
      --prefix LD_LIBRARY_PATH : ${lib.makeLibraryPath [ libGL libxkbcommon wayland libX11 libXcursor libXi libXrandr ]}
    mkdir -p $out/share/applications
    cp ${desktopItem}/share/applications/* $out/share/applications/
  '';
  meta = {
    description = "Offline native Rust design prototype for Bio Workbench";
    mainProgram = "bio-workbench-rust";
    platforms = [ "x86_64-linux" "aarch64-linux" "x86_64-darwin" "aarch64-darwin" ];
  };
}
