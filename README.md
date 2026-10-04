# Pi Media Hub

Pi Media Hub pairs a Raspberry Pi 5 media/server host with a desktop controller for the laptop and a MicroPython status client for the LilyGO T-HMI ESP32-S3. The controller is a GUI AppImage; it does not need a terminal for everyday browsing and control. The server is installed on the Pi, not the Arch laptop. The desktop includes Music and Videos browser views, a local WAV microphone recorder, external browser launcher, private notes, AI chat, app shelf, T-HMI workspace, and a guarded robot-adapter preview.

## Download and launch the desktop controller

Use the latest successful [CI run](https://github.com/elijah-code-5/-lilygo-pi-media-hub-/actions) and download `pi-media-hub-x86_64` for an Arch x86_64 laptop, or `pi-media-hub-aarch64` for 64-bit ARM Linux. These are Linux desktop controllers, not firmware.

On Arch, make the downloaded AppImage executable (file-manager Properties → Permissions → Allow executing, or once in a terminal):

```sh
chmod +x ~/Downloads/pi-media-hub-x86_64.AppImage
```

Then double-click it. If AppImage reports missing FUSE, launch from a terminal once with `APPIMAGE_EXTRACT_AND_RUN=1 ~/Downloads/pi-media-hub-x86_64.AppImage`; the AppImage still starts the GUI. The desktop session needs Tk support (included in the image) and a graphical display. The controller remembers the Pi address and admin token in `~/.config/pi-media-hub/controller.json` with owner-only file permissions.

In the window, enter the Pi's LAN URL (for example `http://192.168.1.42:8765` or `http://raspberrypi.local:8765`) and connect. The Overview reports host/library/catalog status. Music and Videos browse the corresponding Pi media categories and open byte-range media in the desktop's default browser. Recorder writes a standard WAV from the laptop's default microphone to a location you choose; PortAudio is bundled in the AppImage. Browser opens HTTP(S) URLs in your default browser. Notebook stores private text/Markdown notes locally with owner-only file permissions (it is not a stylus/ink notebook). Assistant configures the Pi-side backend and provides chat. App Shelf can create web shortcuts, upload a packaged static web-app ZIP, import a compatible GitHub repository, launch, and remove entries. Setup links to the official Raspberry Pi Imager and describes the Pi service/app workflow; it deliberately does not image disks or run remote shell commands. T-HMI detects USB serial ports, flashes a user-selected ESP32-S3 `.bin` using bundled esptool after an explicit confirmation, and provides a serial REPL console.

## Start the server on the Raspberry Pi

The Pi hosts the media and should be on the same trusted LAN as the controller. Copy the ARM64 AppImage to the Pi. For a persistent server that starts at boot, install the service once (the desktop app itself is not an automatic service installer):

```sh
sudo mkdir -p /srv/media
sudo chown "$USER":"$USER" /srv/media
sudo env APPIMAGE_EXTRACT_AND_RUN=1 ./pi-media-hub-aarch64.AppImage install --media-root /srv/media --service-user "$USER"
sudo systemctl daemon-reload
sudo systemctl enable --now pi-media-hub
```

The setup command installs the Python server, creates a random Pi admin token, preserves an existing media configuration, and writes the corrected systemd entrypoint. On the Pi, copy the `admin_token` value from `/etc/pi-media-hub/config.json` into the controller's Pi admin token field. Treat this token as a password. On an ARM64 Pi with a desktop, the GUI also has a temporary local server control and folder picker; that server stops when the GUI closes. The systemd installation is preferred for normal Pi hosting.

To verify the service, open `http://127.0.0.1:8765/health` on the Pi or connect from the controller. If it fails, inspect `sudo journalctl -u pi-media-hub -b --no-pager -n 60`.

## App Shelf: shortcuts and packaged static apps

Create Shortcut registers an ordinary HTTP(S) web link. **Launch opens it in the laptop's browser; it does not run arbitrary programs on the Pi.** Upload Web App ZIP installs a small static web app on the Pi; GitHub import downloads the selected repository's ZIP from GitHub. Both expect a root `pi-media-hub-app.json` manifest and `index.html`. For a GitHub repository the required manifest format is:

```json
{
  "id": "my-app",
  "name": "My App",
  "description": "A static web app"
}
```

For ZIP uploads, place the manifest and `index.html` inside one top-level folder and select the ZIP. `examples/hello-web-app/` is a tiny example. Imported package files are restricted to static web asset types and served with a browser sandbox policy; Python, shell scripts, and other executable server-side content are not run. The Pi limits ZIPs to 24 MiB compressed, 32 MiB unpacked, and 250 files. GitHub projects must include the manifest and a static app entry page; ordinary source repos are not automatically converted or built. App add/import/remove and AI configuration require the admin token.

## Media library and playback

The server indexes common audio (`mp3`, `m4a`, `flac`, `ogg`, `wav`, and others) and video (`mp4`, `mkv`, `webm`, and others) in `media_root`. Audio beneath a folder whose name starts with `podcast` is categorized as a podcast. The library returns metadata; selecting a file opens its byte-range-capable stream URL in the browser. Codec support and playback controls are provided by that browser, not by the Pi service or T-HMI.

## Local AI

In Assistant, set an already-running backend URL and model. Supported request shapes in the controller are Ollama `/api/chat`, OpenAI-compatible `/v1/chat/completions`, and a custom JSON endpoint. Enable and save the settings on the Pi, then chat. The Pi makes outbound requests to that configured URL and stores the setting in its protected config. No model is downloaded or built by this project. A Pi 5 with 4 GB has limited capacity; use a small quantized model, reduce context, or host inference elsewhere on your trusted LAN. Model installation, performance, and compatibility depend on the backend.

## Built-in apps and installing apps

The desktop AppImage supplies Music, Videos, Recorder, Browser, Notebook, Assistant, Robot, and T-HMI tools. Use App Shelf for user-added Pi shortcuts and static web apps: ZIP upload and GitHub import require `pi-media-hub-app.json` and `index.html`. Imported app files are validated static assets; Pi does not execute app source or install operating-system packages. The Setup page opens the official Raspberry Pi Imager download page and gives the service-install steps. OS imaging is destructive and must be performed explicitly in Raspberry Pi Imager; remote imaging/install via this controller is not supported.

## Freenove robot (adapter preview; not a stock-kit driver)

Robot supports manual finite pulses, a small allowlisted JSON motion program, and an explicit-confirmation OTA upload **only** for ESP32 firmware implementing the fixed [robot adapter contract](docs/robot-adapter.md). Movement controls require a private-network address, bearer token, exact board profile, advertised stop capability, and a 100–600 ms on-device watchdog. Every motion pulse is capped at 400 ms; STOP is sent on release/program completion and on controller exit where possible. Robot updates verify a user-supplied ZIP's board ID and SHA-256, but SHA-256 is not a signature. The adapter must stop motors independently on timeout, reset, Wi-Fi loss, and OTA.

No Freenove stock protocol or motor pins are guessed. No physical car can be driven until the exact kit revision/ESP32/motor-driver profile has matching adapter firmware and bench testing. Face tracking, camera support, board-specific custom firmware builds, Wi-Fi provisioning, and hardware validation are **not implemented**. The camera model/location and exact kit product details are required before those can be safely supported. Do not connect or flash a robot using an unrelated T-HMI `.bin`.

## T-HMI firmware and serial testing

Connect the board's USB cable to the Arch laptop, not to a headless Pi. In the controller's T-HMI page, detect the board's serial port, select a prebuilt `.bin`, set the offset prescribed by that firmware, and explicitly confirm Flash. `0x0` is only appropriate for a merged image. The bundled esptool invocation is fixed to the ESP32-S3 chip and selected serial port; the app does not accept shell commands or silently flash. The serial console can inspect boot output and send user-entered MicroPython REPL lines.

This MVP flashes a firmware image supplied by you; it does **not** compile MicroPython firmware, identify every T-HMI revision automatically, or claim hardware testing. Use firmware and flash instructions matching the exact board revision, close other serial monitors, and expect flashing to erase existing contents. ESP32-S3 auto-download may need the board BOOT/RESET procedure. On Arch, add your user to the system's serial-device access group (commonly `uucp`) and log out/in again if the port is permission denied. No firmware is flashed until you choose an image, port, and confirm.

The separate T-HMI `device/main.py` remains a MicroPython Wi-Fi status/library menu client. It needs MicroPython's `urequests` and a revision-specific display/touch adapter; the supplied adapter example is a contract/template, not a board driver.

## Service API and configuration

| Endpoint | Purpose |
| --- | --- |
| `GET /health`, `GET /api/status` | Host health and basic counts |
| `GET /api/library?kind=all\|audio\|video\|podcast&q=...` | Filtered media metadata |
| `GET /media?path=relative/file.mp3` | Stream media with single byte ranges |
| `GET /api/apps` | Read app shelf |
| `POST /api/apps`, `POST /api/apps/upload`, `POST /api/apps/import/github`, `DELETE /api/apps/{id}` | Token-protected app shelf management |
| `GET/POST /api/config/ai` | Token-protected AI settings |
| `POST /api/chat` | Token-protected pass-through request to configured model backend |

Example server configuration is in `config.example.json`. The installer fills an empty admin token with a fresh random value; direct/manual server setups must set `admin_token` to a long random secret before enabling management operations. Podcast files are detected under a folder named `Podcasts` (case-insensitive). The library caps results at 5,000 files.

## Network safety and limits

The service listens on port 8765 and accepts only loopback/private-network client addresses. Keep it on a trusted LAN/VLAN, allow the port only on that LAN, and never forward it to the public internet. It has no TLS; use a trusted isolated LAN. Admin token protects write operations and AI requests. Media paths are constrained beneath the configured root, symlinked media directories are not traversed, and app ZIP extraction rejects traversal/symlinks and serves only allowlisted static asset types. The sandbox is defense-in-depth; install only apps you trust. The AI URL is an admin-controlled outbound destination.

Limitations: no user accounts, multi-user permissions, browser-native codec guarantees, automatic USB flashing across all board revisions, firmware builds, remote SSH installation, face tracking, tested Freenove motor support, or arbitrary native Pi app execution. Uploaded/GitHub apps are static browser apps, not OS packages. Robot adapter HTTP is unencrypted: use only on a trusted isolated network, never expose robot or hub endpoints to the public internet.

## Build and verify

Build on the matching native Linux architecture; cross-architecture AppImage builds are intentionally rejected:

```sh
python3 -m pip install PyInstaller esptool pyserial sounddevice
sudo pacman -S portaudio       # Arch Linux build host
sudo apt install libportaudio2 # Debian/Ubuntu build host
scripts/build_appimage.sh x86_64    # x86_64 Linux
scripts/build_appimage.sh aarch64   # ARM64 Linux
```

Run server tests with:

```sh
PYTHONPATH=src python3 -m unittest discover -s tests -v
python3 -m compileall -q src device tests tools
```
