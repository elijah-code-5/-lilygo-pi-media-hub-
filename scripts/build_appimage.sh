#!/usr/bin/env bash
set -euo pipefail

target="${1:-}"
if [[ "$target" != "x86_64" && "$target" != "aarch64" ]]; then
  echo "Usage: $0 x86_64|aarch64" >&2
  exit 2
fi

machine="$(uname -m)"
if [[ "$target" != "$machine" ]]; then
  echo "AppImages must be built natively: requested $target on $machine." >&2
  echo "Use a native runner/container for the target architecture; this script does not cross-compile." >&2
  exit 2
fi
appimagetool_bin="${APPIMAGETOOL:-appimagetool}"
if [[ "$appimagetool_bin" == */* ]]; then
  if [[ ! -x "$appimagetool_bin" ]]; then
    echo "appimagetool is not executable: $appimagetool_bin" >&2
    exit 2
  fi
elif ! command -v "$appimagetool_bin" >/dev/null 2>&1; then
  echo "appimagetool is required (set APPIMAGETOOL to its executable path)." >&2
  exit 2
fi
if ! python3 -m PyInstaller --version >/dev/null 2>&1; then
  echo "PyInstaller is required; install it with: python3 -m pip install pyinstaller" >&2
  exit 2
fi

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT
appdir="$work/PiMediaHub.AppDir"
mkdir -p "$appdir/usr/bin" "$appdir/usr/share/pi-media-hub"

(
  cd "$root"
  python3 -m PyInstaller --noconfirm --clean --onefile \
    --name pi-media-hub \
    --add-data "$root/src/pi_media_hub:pi_media_hub" \
    --add-data "$root/config.example.json:share/pi-media-hub" \
    --paths "$root/src" \
    --specpath "$work" \
    --distpath "$work/dist" \
    --workpath "$work/build" \
    tools/setup_appimage.py
)
cp "$work/dist/pi-media-hub" "$appdir/usr/bin/pi-media-hub"
cp config.example.json "$appdir/usr/share/pi-media-hub/config.example.json"
cat > "$appdir/AppRun" <<'EOF'
#!/usr/bin/env sh
HERE="$(dirname "$(readlink -f "$0")")"
exec "$HERE/usr/bin/pi-media-hub" "$@"
EOF
cat > "$appdir/pi-media-hub.desktop" <<'EOF'
[Desktop Entry]
Type=Application
Name=Pi Media Hub
Comment=Connect to and control a Pi Media Hub
Exec=pi-media-hub
Icon=pi-media-hub
Categories=Utility;
Terminal=false
EOF
printf 'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+j8ioAAAAASUVORK5CYII=' | base64 -d > "$appdir/pi-media-hub.png"
chmod +x "$appdir/AppRun" "$appdir/usr/bin/pi-media-hub"
output="$root/dist/pi-media-hub-${target}.AppImage"
mkdir -p "$root/dist"
ARCH="$target" APPIMAGE_EXTRACT_AND_RUN=1 "$appimagetool_bin" "$appdir" "$output"
echo "Created $output"
