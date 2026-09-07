{ lib, rustPlatform, makeWrapper, stdenv, libGL, libxkbcommon, wayland, libX11, libXcursor, libXi, libXrandr, dbus, mesa, xvfb-run, coreutils, runtimeShell }:
rustPlatform.buildRustPackage {
  pname = "bio-workbench-renderer";
  version = "0.3.0";
  src = lib.cleanSourceWith {
    src = ./.;
    filter = path: type: !(builtins.elem (baseNameOf path) [ "target" "result" ".git" ]) && !(lib.hasSuffix ".png" path);
  };
  cargoLock.lockFile = ./Cargo.lock;
  cargoBuildFlags = [ "--bin" "bio-render" ];
  cargoTestFlags = [ "--bin" "bio-render" ];
  nativeBuildInputs = [ makeWrapper ];
  postInstall = lib.optionalString stdenv.hostPlatform.isLinux ''
    wrapProgram $out/bin/bio-render \
      --prefix LD_LIBRARY_PATH : ${lib.makeLibraryPath [ libGL libxkbcommon wayland libX11 libXcursor libXi libXrandr dbus ]}
    cat > $out/bin/bio-render-headless <<'SCRIPT'
    #!${runtimeShell}
    set -euo pipefail
    export LIBGL_ALWAYS_SOFTWARE=1
    export GALLIUM_DRIVER=llvmpipe
    export LIBGL_DRIVERS_PATH=${mesa.drivers}/lib/dri
    export __EGL_VENDOR_LIBRARY_FILENAMES=${mesa.drivers}/share/glvnd/egl_vendor.d/50_mesa.json
    unset WAYLAND_DISPLAY EFRAME_SCREENSHOT_TO
    export WINIT_UNIX_BACKEND=x11
    export WINIT_X11_SCALE_FACTOR=1
    exec ${coreutils}/bin/timeout --kill-after=5s 150s ${xvfb-run}/bin/xvfb-run -a -s '-screen 0 2560x2160x24 -nolisten tcp' @out@/bin/bio-render "$@"
    SCRIPT
    substituteInPlace $out/bin/bio-render-headless --replace-fail '@out@' "$out"
    chmod +x $out/bin/bio-render-headless
  '';
  meta = {
    description = "Bio Workbench studio renderer for original PDB and mmCIF structures";
    mainProgram = "bio-render";
    platforms = lib.platforms.linux ++ lib.platforms.darwin;
  };
}
