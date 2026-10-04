"""Fail-safe MicroPython HTTP adapter scaffold for an ESP32 robot.

Board-specific motor and dual-slot OTA code is intentionally opt-in. See the
repository's docs/robot-adapter.md before adapting or enabling either feature.
"""

import socket
import time

try:
    import ujson as json
except ImportError:
    import json

try:
    import uhashlib as hashlib
except ImportError:
    import hashlib

import network

import config
from protocol import MAX_FIRMWARE_BYTES, validate_motion, validate_ota_headers, valid_token


PROFILE = "pi-media-hub-robot-v1"
WATCHDOG_MS = 500
MAX_HEADER_BYTES = 4096
MAX_JSON_BYTES = 1024
READ_CHUNK = 1024
MOTOR_ENABLED = getattr(config, "MOTOR_ENABLED", False)
OTA_ENABLED = getattr(config, "OTA_ENABLED", False)
BOARD_ID = getattr(config, "BOARD_ID", "")
VERSION = getattr(config, "FIRMWARE_VERSION", "0.1.0-template")
TOKEN = getattr(config, "ADMIN_TOKEN", "")
ALLOWED_CLIENT_PREFIXES = getattr(config, "ALLOWED_CLIENT_PREFIXES", ())

motor = None
ota = None
active_command = "stop"
motor_deadline = None


def _json_bytes(value):
    return json.dumps(value).encode("utf-8")


def _send_all(client, data):
    view = data
    while view:
        sent = client.send(view)
        if not sent:
            raise OSError("Connection closed while sending response")
        view = view[sent:]


def _respond(client, status, payload):
    body = _json_bytes(payload)
    reason = {
        200: "OK",
        202: "Accepted",
        400: "Bad Request",
        401: "Unauthorized",
        403: "Forbidden",
        404: "Not Found",
        405: "Method Not Allowed",
        409: "Conflict",
        413: "Payload Too Large",
        500: "Internal Server Error",
        501: "Not Implemented",
        503: "Service Unavailable",
    }.get(status, "Error")
    _send_all(
        client,
        "HTTP/1.1 {} {}\r\nContent-Type: application/json\r\n"
        "Content-Length: {}\r\nConnection: close\r\n"
        "Cache-Control: no-store\r\n\r\n".format(status, reason, len(body)).encode(),
    )
    _send_all(client, body)


def _read_request(client):
    data = b""
    while b"\r\n\r\n" not in data:
        chunk = client.recv(512)
        if not chunk:
            raise ValueError("Incomplete HTTP headers")
        data += chunk
        if len(data) > MAX_HEADER_BYTES:
            raise OverflowError("HTTP headers exceed limit")
    head, body = data.split(b"\r\n\r\n", 1)
    lines = head.decode("latin-1").split("\r\n")
    request_parts = lines[0].split(" ")
    if len(request_parts) != 3:
        raise ValueError("Malformed request line")
    headers = {}
    for line in lines[1:]:
        if ":" in line:
            name, value = line.split(":", 1)
            headers[name.strip().lower()] = value.strip()
    length_text = headers.get("content-length", "0")
    if not length_text.isdigit():
        raise ValueError("Invalid Content-Length")
    length = int(length_text)
    return request_parts[0], request_parts[1], headers, length, body


def _read_body(client, body, length, maximum):
    if length > maximum:
        raise OverflowError("Request body exceeds limit")
    while len(body) < length:
        chunk = client.recv(min(READ_CHUNK, length - len(body)))
        if not chunk:
            raise ValueError("Incomplete request body")
        body += chunk
    return body[:length]


def _client_allowed(address):
    host = address[0]
    for prefix in ALLOWED_CLIENT_PREFIXES:
        if isinstance(prefix, str) and prefix and host.startswith(prefix):
            return True
    return False


def _authenticated(headers):
    value = headers.get("authorization", "")
    if not value.startswith("Bearer "):
        return False
    return valid_token(value[7:], TOKEN)


def _stop_motors():
    global active_command, motor_deadline
    active_command = "stop"
    motor_deadline = None
    if motor is not None:
        motor.stop()


def _service_watchdog():
    global active_command, motor_deadline
    if motor_deadline is not None and time.ticks_diff(time.ticks_ms(), motor_deadline) >= 0:
        _stop_motors()


def _handle_control(client, headers, body, length):
    global active_command, motor_deadline
    if not _authenticated(headers):
        _respond(client, 401, {"error": "Bearer token required"})
        return
    raw = _read_body(client, body, length, MAX_JSON_BYTES)
    try:
        command, duration = validate_motion(json.loads(raw.decode()), MOTOR_ENABLED)
    except RuntimeError as error:
        _respond(client, 503, {"error": str(error)})
        return
    except (ValueError, TypeError) as error:
        _respond(client, 400, {"error": str(error)})
        return
    try:
        if command == "stop":
            _stop_motors()
        else:
            motor.move(command)
            active_command = command
            motor_deadline = time.ticks_add(time.ticks_ms(), duration)
    except Exception as error:
        try:
            _stop_motors()
        finally:
            _respond(client, 500, {"error": "Motor driver fault: " + str(error)})
        return
    _respond(client, 200, {"accepted": True, "command": command, "duration_ms": duration})


def _handle_stop(client, headers, body, length):
    if not _authenticated(headers):
        _respond(client, 401, {"error": "Bearer token required"})
        return
    try:
        _stop_motors()
    except Exception as error:
        _respond(client, 500, {"error": "Could not apply STOP: " + str(error)})
        return
    _respond(client, 200, {"accepted": True, "command": "stop"})


def _handle_status(client, headers):
    if not _authenticated(headers):
        _respond(client, 401, {"error": "Bearer token required"})
        return
    _respond(client, 200, {
        "profile": PROFILE,
        "board": BOARD_ID,
        "version": VERSION,
        "motor_stop": True,
        "motor_watchdog_ms": WATCHDOG_MS,
        "motor_enabled": bool(MOTOR_ENABLED),
        "ota_enabled": bool(OTA_ENABLED),
        "command": active_command,
    })


def _handle_ota(client, headers, body, length):
    if not _authenticated(headers):
        _respond(client, 401, {"error": "Bearer token required"})
        return
    try:
        validate_ota_headers(
            headers.get("x-firmware-board"),
            headers.get("x-firmware-version"),
            headers.get("x-firmware-sha256"),
            length,
            BOARD_ID,
            OTA_ENABLED,
        )
    except RuntimeError as error:
        _respond(client, 501, {"error": str(error)})
        return
    except ValueError as error:
        _respond(client, 400, {"error": str(error)})
        return
    if ota is None:
        _respond(client, 501, {"error": "Verified board-specific OTA writer is not installed"})
        return

    digest = hashlib.sha256()
    remaining = length
    try:
        _stop_motors()
        ota.begin(BOARD_ID, headers["x-firmware-version"], length, headers["x-firmware-sha256"])
        pending = body
        while remaining:
            if pending:
                chunk = pending[:min(len(pending), READ_CHUNK, remaining)]
                pending = pending[len(chunk):]
            else:
                chunk = client.recv(min(READ_CHUNK, remaining))
            if not chunk:
                raise ValueError("Incomplete OTA image")
            ota.write(chunk)
            digest.update(chunk)
            remaining -= len(chunk)
        actual = "".join("%02x" % byte for byte in digest.digest())
        if actual != headers["x-firmware-sha256"]:
            raise ValueError("Firmware SHA-256 mismatch")
        ota.finish()
    except Exception as error:
        try:
            ota.abort()
        finally:
            _stop_motors()
            _respond(client, 400, {"error": "OTA rejected: " + str(error)})
        return
    _respond(client, 202, {"status": "accepted", "board": BOARD_ID, "version": headers["x-firmware-version"]})


def _handle_client(client, address):
    if not _client_allowed(address):
        _respond(client, 403, {"error": "Client is outside the configured LAN prefix"})
        return
    try:
        method, target, headers, length, body = _read_request(client)
        path = target.split("?", 1)[0]
        if method == "GET" and path == "/api/status":
            _handle_status(client, headers)
        elif method == "POST" and path == "/api/control":
            _handle_control(client, headers, body, length)
        elif method == "POST" and path == "/api/stop":
            _handle_stop(client, headers, body, length)
        elif method == "PUT" and path == "/api/firmware/update":
            _handle_ota(client, headers, body, length)
        else:
            _respond(client, 404, {"error": "Unknown adapter route"})
    except OverflowError as error:
        _respond(client, 413, {"error": str(error)})
    except (ValueError, OSError) as error:
        _respond(client, 400, {"error": str(error)})


def _connect_wifi():
    wlan = network.WLAN(network.STA_IF)
    wlan.active(True)
    if not wlan.isconnected():
        wlan.connect(config.WIFI_SSID, config.WIFI_PASSWORD)
        deadline = time.ticks_add(time.ticks_ms(), 20000)
        while not wlan.isconnected() and time.ticks_diff(deadline, time.ticks_ms()) > 0:
            time.sleep_ms(250)
    if not wlan.isconnected():
        raise RuntimeError("Wi-Fi connection timed out")
    return wlan


def run():
    global motor, ota
    if not TOKEN or TOKEN.startswith("REPLACE_") or len(TOKEN) < 24:
        raise RuntimeError("Set a unique 24+ character ADMIN_TOKEN in config.py")
    if not ALLOWED_CLIENT_PREFIXES:
        raise RuntimeError("Configure ALLOWED_CLIENT_PREFIXES for the trusted LAN")
    if MOTOR_ENABLED:
        if not BOARD_ID:
            raise RuntimeError("MOTOR_ENABLED requires an exact reviewed BOARD_ID")
        import motor_driver

        if getattr(motor_driver, "BOARD_ID", "") != BOARD_ID:
            raise RuntimeError("motor_driver.BOARD_ID must match config.BOARD_ID")
        motor = motor_driver
        motor.stop()
    if OTA_ENABLED:
        import ota_writer

        ota = ota_writer

    while True:
        try:
            wlan = _connect_wifi()
            print("Robot adapter {} at {}".format(BOARD_ID or "unconfigured", wlan.ifconfig()[0]))
            server = socket.socket()
            server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            server.bind(("0.0.0.0", 8080))
            server.listen(2)
            server.settimeout(0.1)
            while wlan.isconnected():
                _service_watchdog()
                try:
                    client, address = server.accept()
                except OSError:
                    continue
                try:
                    # Bound slow-client stalls so the movement watchdog is serviced.
                    client.settimeout(0.1)
                    _handle_client(client, address)
                finally:
                    client.close()
            _stop_motors()
            server.close()
        except Exception as error:
            try:
                _stop_motors()
            except Exception:
                pass
            print("Adapter stopped safely:", error)
            time.sleep(3)


if __name__ == "__main__":
    run()
