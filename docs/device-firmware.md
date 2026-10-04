# Device firmware source and installation

This repository contains **MicroPython source files**, not a compiled T-HMI or robot firmware image. The two AppImages are Linux desktop applications and are not firmware. No physical board has been tested here.

## LilyGO T-HMI ESP32-S3

### Firmware image source and fit

The official MicroPython [ESP32_GENERIC_S3 downloads page](https://micropython.org/download/ESP32_GENERIC_S3/) describes its release `.bin` as suitable for most ESP32-S3 boards with at least 4 MiB flash, with PSRAM auto-detection. The current stable `.bin` linked there as of 2026-10-04 is [ESP32_GENERIC_S3-20260824-v1.29.0.bin](https://micropython.org/resources/firmware/ESP32_GENERIC_S3-20260824-v1.29.0.bin). If the exact T-HMI revision uses Octal PSRAM, select the page's `ESP32_GENERIC_S3` **spiram-oct** variant instead. Verify flash size, PSRAM type, and the board revision before flashing; generic MicroPython does not include the T-HMI screen/touch driver.

### Flash and install the menu client

On Arch, put the board in its documented ESP32-S3 download mode and identify its serial port. Avoid `erase-flash` unless you intentionally want to erase the whole board. In a terminal:

```sh
python -m venv ~/venvs/pi-media-hub-dev
~/venvs/pi-media-hub-dev/bin/pip install esptool mpremote
~/venvs/pi-media-hub-dev/bin/esptool --chip esp32s3 --port /dev/ttyACM0 --baud 460800 write-flash 0x0 ESP32_GENERIC_S3-20260824-v1.29.0.bin
```

The binary is downloaded from the official link above; check that the selected generic/Octal variant matches the actual module. If auto-download does not start, follow the board vendor's BOOT/RESET sequence. If you explicitly choose to wipe a board before installing MicroPython, the destructive command is:

```sh
~/venvs/pi-media-hub-dev/bin/esptool --chip esp32s3 --port /dev/ttyACM0 erase-flash
```

Edit a copy of `device/config.example.py` as `config.py`, setting Wi-Fi and the Pi server URL. The optional `ADMIN_TOKEN` enables Pi-side Assistant requests; it is the Pi's admin token, so protect it and use only a trusted LAN. Install the source:

```sh
~/venvs/pi-media-hub-dev/bin/mpremote connect /dev/ttyACM0 mip install requests
~/venvs/pi-media-hub-dev/bin/mpremote connect /dev/ttyACM0 fs cp device/main.py :main.py
~/venvs/pi-media-hub-dev/bin/mpremote connect /dev/ttyACM0 fs cp config.py :config.py
```

Reset the board. MicroPython runs `main.py` at boot. The client displays Pi health, Wi-Fi address, paginated music/video/podcast names, and can call the configured Pi Assistant. Media playback itself opens in a browser on the computer, not the T-HMI. If using the display/touch panel, implement `device/t_hmi_adapter.py.example` as `t_hmi_adapter.py` using the display and touch driver for the **exact** T-HMI revision. Its example is an interface stub, not a hardware driver; without it the client falls back to the serial REPL/console.

### Validation and recovery

The device source uses MicroPython's `network`, `urequests`/`requests`, and `time` modules. The Pi library API accepts `limit` and `offset` so a small screen fetches short pages. On the first boot inspect the serial REPL for Wi-Fi/configuration errors. The desktop's T-HMI page flashes a user-selected `.bin` only after explicit confirmation; the official MicroPython image is a user-supplied image and is not bundled here.

## ESP32 Freenove robot adapter scaffold

Files:

- `device/robot_adapter/main.py` — authenticated HTTP server with finite commands, a 500 ms software stop bound, stop route, and optional OTA receiver.
- `device/robot_adapter/protocol.py` — small MicroPython/CPython validation helpers.
- `device/robot_adapter/config.example.py` — credentials/network example; motors and OTA off.
- `device/robot_adapter/motor_driver.py.example` — intentionally nonfunctional motor-driver template.
- `device/robot_adapter/ota_writer.py.example` — interface template; there is no generic ESP32 MicroPython partition writer.

Copy `config.example.py` to `config.py`, set a unique 24+ character token and your Wi-Fi/LAN client prefix, and leave `MOTOR_ENABLED=False`, `OTA_ENABLED=False`. Upload `main.py` and `protocol.py` with `mpremote`:

```sh
cp device/robot_adapter/config.example.py config.py
# Edit config.py with your SSID, password, a fresh ADMIN_TOKEN, and your LAN prefix.
~/venvs/pi-media-hub-dev/bin/mpremote connect /dev/ttyACM0 fs cp device/robot_adapter/main.py :main.py
~/venvs/pi-media-hub-dev/bin/mpremote connect /dev/ttyACM0 fs cp device/robot_adapter/protocol.py :protocol.py
~/venvs/pi-media-hub-dev/bin/mpremote connect /dev/ttyACM0 fs cp config.py :config.py
```

Reset the ESP32. Verify only on the same trusted LAN, substituting the private IP and token:

```sh
curl -H 'Authorization: Bearer YOUR_TOKEN' http://192.168.1.50:8080/api/status
curl -X POST -H 'Authorization: Bearer YOUR_TOKEN' -H 'Content-Type: application/json' \
  -d '{}' http://192.168.1.50:8080/api/stop
```

In this safe default the ESP32 reports adapter status and accepts STOP, but refuses movement and firmware upload. **It will not move the Freenove car by default.**

Before enabling motion, identify the exact Freenove product/revision, ESP32 module, motor-driver chip/board, motor wiring, flash/partition layout, and any camera. Implement only the fixed `stop()` and allowlisted `move(command)` functions in a board-specific `motor_driver.py`; set a matching exact `BOARD_ID`, inspect the code, and test with drive wheels lifted and an accessible physical power cut. The adapter's 500 ms software stop bound is serviced by its event loop; it cannot stop motors if the interpreter or motor driver blocks. A verified driver must make outputs safe on reset and loss/fault, and hardware-level cutoff is strongly recommended. Do not advertise watchdog safety until those properties are actually measured on hardware.

OTA remains off until the exact firmware has a documented dual-slot/rollback arrangement and a reviewed `ota_writer.py`. The writer must write only an inactive slot, support abort without damaging the active image, and validate/activate safely. Then set `OTA_ENABLED=True` and test recovery on the target. The HTTP receiver validates the fixed board/version/size/SHA-256 headers, but SHA-256 is not a signature; do not expose this unencrypted API outside a trusted isolated LAN.

The robot source is generic MicroPython scaffolding, not a compiled binary or a Freenove-compatible firmware build. There is no face tracking/camera code, and no known-safe face-to-motion policy until the camera and kit are identified and the behavior is separately tested.
