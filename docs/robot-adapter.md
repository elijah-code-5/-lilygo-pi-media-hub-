# Robot adapter contract (preview)

The Robot page does **not** speak the Freenove stock protocol. No kit revision, ESP32 variant, motor driver, or camera was supplied, so this repository does not guess pins or claim that the car will move. The controller talks only to an adapter that explicitly implements the contract below. The exact kit still needs bench validation before any motor or OTA operation.

## Required safety properties

- Bind the adapter only to the robot's trusted private Wi-Fi network.
- Authenticate every route using `Authorization: Bearer <token>`. Configure a unique token in the controller. HTTP does not encrypt this token; use only a trusted isolated LAN.
- Status must truthfully report `profile: "pi-media-hub-robot-v1"`, `motor_stop: true`, the exact `board` profile, and a `motor_watchdog_ms` value from 100 through 600. The provided scaffold also reports `motor_enabled` and `ota_enabled`; both are false by default, and status alone does not certify physical hardware safety.
- Each movement request is a finite pulse of at most 400 ms. The firmware must stop motors itself on that deadline, connection loss, reset, and watchdog expiry; controller release/STOP is an additional request, not a replacement for the on-device watchdog.
- The `/api/stop` handler must immediately de-energize every motor and acknowledge only after applying stop. OTA must reject the wrong board/image and leave motors stopped before, during, and after update.
- Never implement generic command execution, shell, or arbitrary pin-setting endpoints.

## Fixed HTTP interface

All endpoints are on the configured private HTTP(S) origin; the controller does not accept a user-selected route.

| Request | Required response |
| --- | --- |
| `GET /api/status` | JSON with `profile`, `board`, `version`, `motor_stop: true`, and integer `motor_watchdog_ms` (100–600) |
| `POST /api/control` | Bearer-authenticated JSON `{"command":"forward|backward|left|right","duration_ms":50..400}`; validate the allowlist and return `{"accepted":true}` only when safely scheduled |
| `POST /api/stop` | Bearer-authenticated `{"command":"stop","duration_ms":0}`; stop immediately and return `{"accepted":true}` |
| `PUT /api/firmware/update` | Bearer-authenticated binary image with `X-Firmware-Board`, `X-Firmware-Version`, and `X-Firmware-SHA256`; verify board, size, and digest, keep motors stopped, and respond `{"status":"accepted"}` or `{"status":"updated"}` |

The controller provides bounded JSON programs with a 5-second total motion budget, sends a stop after execution, and supports explicit Wi-Fi update confirmation. The scaffold services a software stop deadline in its main loop and limits accepted client stalls, but that is not an independent hardware watchdog: a stuck interpreter/driver needs a hardware cutoff. Firmware ZIPs contain exactly `pi-media-hub-robot.json` and one `.bin` image. The manifest is:

```json
{
  "board": "exact-board-profile-id",
  "version": "1.0.0",
  "sha256": "lowercase-sha256-of-the-bin-file"
}
```

This SHA-256 detects corruption; it is **not** a publisher signature or proof that firmware is safe. OTA only works after the board's matching adapter firmware implements and has been tested against this contract. No Freenove kit driver, board-specific firmware build, automatic Wi-Fi provisioning, camera/face tracking, or physical hardware test is included yet.

## Details needed for a supported Freenove profile

Provide the exact product URL/model/revision, ESP32 module/board version, motor-driver board/chip, existing firmware and its network/control protocol, and camera model/location/interface (for example, an onboard camera module versus laptop webcam). Face tracking should first report detections; mapping a face position to motor motion requires a separately confirmed, bounded policy and a hardware emergency-stop/watchdog test.
