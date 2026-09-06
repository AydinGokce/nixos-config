{ lib, stdenvNoCC, buildNpmPackage, makeWrapper, python3, openssh, electron, makeDesktopItem, nodejs }:
let
  source = lib.cleanSourceWith {
    src = ./.;
    filter = path: type: !(builtins.elem (baseNameOf path) [ "node_modules" "dist" "result" "__pycache__" ".pytest_cache" "test-results" "playwright-report" ]);
  };
  frontend = buildNpmPackage {
    pname = "bio-workbench-renderer";
    version = "0.1.0";
    src = "${source}/frontend";
    inherit nodejs;
    npmDepsHash = "sha256-B35q+Ky8R2BjI3wFYcGHH9oY9gLQyvuHq4CpyzjH+tk=";
    npmFlags = [ "--ignore-scripts" ];
    doCheck = true;
    checkPhase = ''
      runHook preCheck
      npm test
      runHook postCheck
    '';
    installPhase = ''
      runHook preInstall
      mkdir -p $out
      cp -r dist/. $out/
      runHook postInstall
    '';
  };
  desktopItem = makeDesktopItem {
    name = "bio-workbench";
    desktopName = "Bio Workbench";
    comment = "Submit and compare cloud molecular models";
    exec = "bio-workbench %u";
    icon = "bio-workbench";
    terminal = false;
    categories = [ "Science" "Biology" ];
    mimeTypes = [ "x-scheme-handler/bio-workbench" ];
    startupWMClass = "Bio Workbench";
  };
in stdenvNoCC.mkDerivation {
  pname = "bio-workbench";
  version = "0.1.0";
  src = source;
  nativeBuildInputs = [ makeWrapper ];
  dontBuild = true;
  doCheck = true;
  checkPhase = ''
    PYTHONPATH=$PWD/backend ${python3}/bin/python3 -m unittest discover -s tests -v
    ${nodejs}/bin/node --check desktop/main.cjs
    ${nodejs}/bin/node --check desktop/preload.cjs
    ${nodejs}/bin/node --test desktop/security.test.cjs
    ${nodejs}/bin/node --test desktop/state.test.cjs
  '';
  installPhase = ''
    runHook preInstall
    mkdir -p $out/share/bio-workbench/frontend $out/bin $out/share/icons/hicolor/scalable/apps
    cp -r backend desktop $out/share/bio-workbench/
    cp -r ${frontend} $out/share/bio-workbench/frontend/dist
    cp desktop/icon.svg $out/share/icons/hicolor/scalable/apps/bio-workbench.svg
    makeWrapper ${electron}/bin/electron $out/bin/bio-workbench \
      --add-flags $out/share/bio-workbench/desktop \
      --set BIO_DESKTOP_PYTHON ${python3}/bin/python3 \
      --set BIO_SSH ${openssh}/bin/ssh
    ${lib.optionalString stdenvNoCC.hostPlatform.isLinux ''
      mkdir -p $out/share/applications
      cp ${desktopItem}/share/applications/* $out/share/applications/
    ''}
    ${lib.optionalString stdenvNoCC.hostPlatform.isDarwin ''
      mkdir -p "$out/Applications/Bio Workbench.app/Contents/MacOS" "$out/Applications/Bio Workbench.app/Contents/Resources"
      ln -s $out/bin/bio-workbench "$out/Applications/Bio Workbench.app/Contents/MacOS/bio-workbench"
      cat > "$out/Applications/Bio Workbench.app/Contents/Info.plist" <<PLIST
      <?xml version="1.0" encoding="UTF-8"?>
      <!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
      <plist version="1.0"><dict><key>CFBundleName</key><string>Bio Workbench</string><key>CFBundleDisplayName</key><string>Bio Workbench</string><key>CFBundleIdentifier</key><string>org.harrison.bio-workbench</string><key>CFBundleVersion</key><string>0.1.0</string><key>CFBundleExecutable</key><string>bio-workbench</string><key>CFBundlePackageType</key><string>APPL</string><key>CFBundleURLTypes</key><array><dict><key>CFBundleURLName</key><string>Bio Workbench batch</string><key>CFBundleURLSchemes</key><array><string>bio-workbench</string></array></dict></array></dict></plist>
      PLIST
    ''}
    runHook postInstall
  '';
  meta = {
    description = "Electron desktop for cloud molecular prediction and comparison";
    mainProgram = "bio-workbench";
    platforms = [ "x86_64-linux" "aarch64-linux" "x86_64-darwin" "aarch64-darwin" ];
  };
}
