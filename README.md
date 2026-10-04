# Pi Media Hub

A small, self-hosted media server for a Raspberry Pi 5 (4 GB) with a MicroPython Wi-Fi status/menu client for a LilyGO T-HMI ESP32-S3. This is an initial MVP, not a complete media center: playback happens in a browser or another client using the stream URL; the ESP32 lists library entries but does not decode media.

## Run the server

Requires Python 3.10+ and no third-party runtime packages.

```sh
mkdir -p /srv/media
cp config.example.json config.json
# Edit config.json and set media_root to your media directory.
PYTHONPATH=src python3 -m pi_media_hub --config config.json
```

Check `http://127.0.0.1:8765/health` on the host. Available endpoints:

| Endpoint | Purpose |
| --- | --- |
| `GET /health`, `GET /api/status` | Health and basic server status |
| `GET /api/library?kind=all\|audio\|video\|podcast&q=...` | Media metadata and stream URLs |
| `GET /media?path=relative/path.mp3` | Stream a file; supports one HTTP byte range |
| `GET /api/apps` | Configured app catalog |
| `POST /api/chat` | Optional pass-through to a configured HTTP AI backend |

Podcast files are audio files under a directory named `Podcasts` (case-insensitive). Other common audio and video formats are listed. The library response is capped at 5,000 files.

## Raspberry Pi installation

On the Pi, install Python 3 and systemd, create a media directory, and run the installer as an account allowed to write `/opt`, `/etc`, and the systemd unit directory:

```sh
sudo mkdir -p /srv/media
sudo chown "$USER":"$USER" /srv/media
sudo ./pi-media-hub-setup-aarch64.AppImage install --media-root /srv/media --service-user "$USER"
sudoedit /etc/pi-media-hub/config.json
sudo systemctl daemon-reload
sudo systemctl enable --now pi-media-hub
systemctl status pi-media-hub
```

The setup utility copies the Python server to `/opt/pi-media-hub`, keeps an existing config rather than overwriting it, and writes a systemd service. It prints systemd commands but does not execute them. Review the generated unit and configuration before starting the service. The server runs as the selected unprivileged service user.

## AppImages

AppImages are Linux host setup utilities, **not ESP32 firmware**. Build a separate native image for each target:

```sh
python3 -m pip install PyInstaller
# Install appimagetool for this host architecture and put it on PATH.
scripts/build_appimage.sh x86_64   # on x86_64 Linux
scripts/build_appimage.sh aarch64  # on ARM64 Linux (e.g. Raspberry Pi OS 64-bit)
```

The build intentionally refuses cross-architecture builds. The AppImage packages the setup CLI; the installed server uses the host's `/usr/bin/python3`. CI builds x86_64 and ARM64 images on native runners. AppImage execution can require FUSE; on systems without it, use the AppImage's supported extract-and-run mode or install from a source checkout with `python3 -m pi_media_hub.setup install`.

## T-HMI MicroPython client

1. Flash a MicroPython ESP32-S3 firmware image compatible with your exact T-HMI revision using the board vendor's supported flashing tool. This repository does not include a board firmware binary or flash hardware automatically.
2. Install MicroPython's `urequests` module if it is not already present, copy `device/main.py` to the board, edit the Wi-Fi credentials and `SERVER_URL`, then run it from the MicroPython REPL.
3. For an on-device display/touch menu, install the display/touch drivers for your exact T-HMI revision and provide `t_hmi_adapter.py` next to `main.py`. The adapter contract is exactly `show(lines: list[str])` and `read_key()`, returning `"up"`, `"down"`, `"select"`, `"back"`, or `None`. `device/t_hmi_adapter.py.example` is a placeholder template, not a hardware driver. Without an adapter, the client displays status in the REPL and accepts `up`, `down`, `select`, or `q` over the serial console.

The T-HMI runs the status/menu client only. It does not play or decode audio/video. Board revisions and available MicroPython display/touch drivers differ; no display controller, touch IC, pin map, or tested hardware revision is assumed by this MVP.

## Optional local AI

AI is off by default. To connect an already-running local HTTP model service, set `ai.enabled` to `true` and `ai.endpoint` to its chat-compatible JSON endpoint in `config.json`. The hub forwards the supplied JSON body and returns the backend response; it downloads no model and makes no claim that inference runs well on 4 GB RAM. Configure only a backend you trust. Requests are size-limited and have a configurable timeout.

## Network and safety

The default listener is port 8765 on all interfaces so Wi-Fi clients can reach it, but the server accepts only loopback and private-network client IPs. Keep it on a trusted LAN/VLAN, restrict the port in the host firewall, and do not forward it from the internet. There is no user authentication or TLS in this MVP. Configure `host` to a specific LAN address if preferred. Media paths are resolved and constrained beneath `media_root`; symlinked directories are not traversed. The server exposes only supported audio/video files, and supports single-range streaming.

The AI endpoint is a trusted administrator setting; the hub makes outbound requests to that URL when AI is enabled. Do not point it at an untrusted endpoint.

## Development and tests

```sh
PYTHONPATH=src python3 -m unittest discover -s tests -v
python3 -m compileall -q src device tests tools
```
