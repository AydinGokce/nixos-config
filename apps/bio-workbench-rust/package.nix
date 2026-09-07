{ lib, rustPlatform, makeWrapper, libGL, libxkbcommon, wayland, libX11, libXcursor, libXi, libXrandr, stdenv, makeDesktopItem, pymol, openssh, dbus, zenity, mesa }:
let
  desktopItem = makeDesktopItem {
    name = "bio-workbench";
    desktopName = "Bio Workbench";
    comment = "Submit and compare cloud molecular models";
    exec = "bio-workbench %u";
    icon = "bio-workbench";
    mimeTypes = [ "x-scheme-handler/bio-workbench" ];
    startupWMClass = "bio-workbench";
    terminal = false;
    categories = [ "Science" "Biology" ];
  };
in rustPlatform.buildRustPackage {
  pname = "bio-workbench-rust";
  version = "0.3.0";
  src = lib.cleanSourceWith {
    src = ./.;
    filter = path: type: !(builtins.elem (baseNameOf path) [ "target" "result" ".git" ]) && !(lib.hasSuffix ".png" path);
  };
  cargoLock.lockFile = ./Cargo.lock;
  nativeBuildInputs = [ makeWrapper ];
  postInstall = ''
    for program in bio-workbench-rust bio-render; do
    wrapProgram $out/bin/$program ${lib.escapeShellArgs (
      [ "--set-default" "BIO_SSH" (lib.getExe openssh)
        "--prefix" "PATH" ":" (lib.makeBinPath ([ openssh ] ++ lib.optionals stdenv.hostPlatform.isLinux [ zenity ])) ]
      ++ lib.optionals stdenv.hostPlatform.isLinux [
        "--set-default" "BIO_WORKBENCH_PYMOL" (lib.getExe pymol)
        "--prefix" "LD_LIBRARY_PATH" ":" (lib.makeLibraryPath [ libGL libxkbcommon wayland libX11 libXcursor libXi libXrandr dbus ])
      ])}
    done
    ln -s bio-workbench-rust $out/bin/bio-workbench
    mkdir -p $out/share/icons/hicolor/scalable/apps
    cp ${./icon.svg} $out/share/icons/hicolor/scalable/apps/bio-workbench.svg
    ${lib.optionalString stdenv.hostPlatform.isLinux ''
      makeWrapper "$out/bin/bio-workbench" "$out/bin/bio-workbench-software" \
        --set LIBGL_ALWAYS_SOFTWARE 1 \
        --set GALLIUM_DRIVER llvmpipe \
        --set LIBGL_DRIVERS_PATH ${mesa.drivers}/lib/dri \
        --set __EGL_VENDOR_LIBRARY_FILENAMES ${mesa.drivers}/share/glvnd/egl_vendor.d/50_mesa.json
      mkdir -p $out/share/applications
      cp ${desktopItem}/share/applications/* $out/share/applications/
    ''}
    ${lib.optionalString stdenv.hostPlatform.isDarwin ''
      mkdir -p "$out/Applications/Bio Workbench.app/Contents/MacOS" "$out/Applications/Bio Workbench.app/Contents/Resources"
      ln -s $out/bin/bio-workbench "$out/Applications/Bio Workbench.app/Contents/MacOS/bio-workbench"
      cat > "$out/Applications/Bio Workbench.app/Contents/Info.plist" <<PLIST
      <?xml version="1.0" encoding="UTF-8"?>
      <!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
      <plist version="1.0"><dict><key>CFBundleName</key><string>Bio Workbench</string><key>CFBundleDisplayName</key><string>Bio Workbench</string><key>CFBundleIdentifier</key><string>org.harrison.bio-workbench</string><key>CFBundleVersion</key><string>0.3.0</string><key>CFBundleExecutable</key><string>bio-workbench</string><key>CFBundlePackageType</key><string>APPL</string><key>CFBundleURLTypes</key><array><dict><key>CFBundleURLName</key><string>Bio Workbench batch</string><key>CFBundleURLSchemes</key><array><string>bio-workbench</string></array></dict></array></dict></plist>
      PLIST
    ''}
  '';
  meta = {
    description = "Native Rust desktop for cloud molecular prediction and comparison";
    mainProgram = "bio-workbench";
    platforms = [ "x86_64-linux" "aarch64-linux" "x86_64-darwin" "aarch64-darwin" ];
  };
}
