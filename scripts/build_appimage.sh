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
if ! python3 -c 'import sounddevice' >/dev/null 2>&1; then
  echo "sounddevice is required; install it with: python3 -m pip install sounddevice" >&2
  exit 2
fi
portaudio="$(ldconfig -p | awk '$1 == "libportaudio.so.2" {print $NF; exit}')"
if [[ ! -f "$portaudio" ]]; then
  echo "PortAudio runtime is required (for example: install libportaudio2)." >&2
  exit 2
fi

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT
appdir="$work/PiMediaHub.AppDir"
mkdir -p "$appdir/usr/bin" "$appdir/usr/lib" "$appdir/usr/share/pi-media-hub" "$work/bundle/pi_media_hub"
cp -L "$portaudio" "$appdir/usr/lib/libportaudio.so.2"
ln -s libportaudio.so.2 "$appdir/usr/lib/libportaudio.so"
cp "$root"/src/pi_media_hub/*.py "$work/bundle/pi_media_hub/"

(
  cd "$root"
  python3 -m PyInstaller --noconfirm --clean --onefile \
    --name pi-media-hub \
    --add-data "$work/bundle/pi_media_hub:pi_media_hub" \
    --add-data "$root/config.example.json:share/pi-media-hub" \
    --collect-all esptool \
    --collect-all serial \
    --collect-all sounddevice \
    --hidden-import sounddevice \
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
export LD_LIBRARY_PATH="$HERE/usr/lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
export LIBRARY_PATH="$HERE/usr/lib:${LIBRARY_PATH:-}"
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
python3 - "$appdir/pi-media-hub.png" <<'PY'
import math
import struct
import sys
import zlib

size = 128
rows = []
for y in range(size):
    row = bytearray([0])
    for x in range(size):
        distance = math.hypot(x - 64, y - 64)
        color = (16, 21, 29, 255)
        if distance < 48:
            color = (80, 216, 187, 255)
        if 15 < distance < 18:
            color = (237, 243, 251, 255)
        if distance < 10:
            color = (27, 38, 52, 255)
        if 79 < x < 88 and 33 < y < 73:
            color = (237, 243, 251, 255)
        if 70 < x < 88 and 67 < y < 73:
            color = (237, 243, 251, 255)
        row.extend(color)
    rows.append(bytes(row))

def chunk(kind, data):
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)

png = b"\x89PNG\r\n\x1a\n"
png += chunk(b"IHDR", struct.pack(">2I5B", size, size, 8, 6, 0, 0, 0))
png += chunk(b"IDAT", zlib.compress(b"".join(rows), 9))
png += chunk(b"IEND", b"")
with open(sys.argv[1], "wb") as icon:
    icon.write(png)
PY
chmod +x "$appdir/AppRun" "$appdir/usr/bin/pi-media-hub"
output="$root/dist/pi-media-hub-${target}.AppImage"
mkdir -p "$root/dist"
ARCH="$target" APPIMAGE_EXTRACT_AND_RUN=1 "$appimagetool_bin" "$appdir" "$output"
echo "Created $output"
