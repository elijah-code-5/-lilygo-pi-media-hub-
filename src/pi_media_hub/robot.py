from __future__ import annotations

import hashlib
import ipaddress
import json
import re
import stat
import zipfile
from pathlib import Path, PurePosixPath
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen


COMMANDS = {"forward", "backward", "left", "right", "stop"}
MAX_PULSE_MS = 400
MAX_PROGRAM_MS = 5000
MAX_FIRMWARE_BYTES = 16 * 1024 * 1024
BOARD_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
VERSION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.+-]{0,63}$")


def _validate_token(token: str) -> str:
    if not isinstance(token, str) or not 16 <= len(token) <= 256 or any(
        ord(character) < 33 or ord(character) > 126 for character in token
    ):
        raise ValueError("Adapter token must be 16-256 visible ASCII characters.")
    return token


def normalize_robot_url(value: str) -> str:
    candidate = value.strip().rstrip("/")
    parsed = urlsplit(candidate)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("Enter a robot HTTP(S) origin, without credentials or a path.")
    try:
        address = ipaddress.ip_address(parsed.hostname.split("%", 1)[0])
    except ValueError:
        if parsed.hostname not in {"localhost"} and not parsed.hostname.lower().endswith(".local"):
            raise ValueError("Robot address must be a private IP or a .local hostname.")
    else:
        if not (address.is_private or address.is_loopback or address.is_link_local):
            raise ValueError("Robot address must be on a private/local network.")
    try:
        parsed.port
    except ValueError as error:
        raise ValueError("The robot address contains an invalid port.") from error
    return candidate


def build_control_payload(command: str, duration_ms: int = 0) -> dict:
    if command not in COMMANDS:
        raise ValueError("Unsupported robot command.")
    if command == "stop":
        return {"command": "stop", "duration_ms": 0}
    if not isinstance(duration_ms, int) or isinstance(duration_ms, bool) or not 50 <= duration_ms <= MAX_PULSE_MS:
        raise ValueError(f"Movement pulses must be 50-{MAX_PULSE_MS} ms.")
    return {"command": command, "duration_ms": duration_ms}


def validate_program(steps: object) -> list[dict]:
    if not isinstance(steps, list) or not 1 <= len(steps) <= 20:
        raise ValueError("A robot program must have 1-20 steps.")
    validated = []
    total = 0
    for step in steps:
        if not isinstance(step, dict):
            raise ValueError("Each robot program step must be an object.")
        command = step.get("command")
        duration = step.get("duration_ms")
        payload = build_control_payload(command, duration)
        if command == "stop":
            continue
        total += duration
        if total > MAX_PROGRAM_MS:
            raise ValueError(f"Robot programs are limited to {MAX_PROGRAM_MS} ms of motion.")
        validated.append(payload)
    if not validated:
        raise ValueError("A robot program needs at least one movement step.")
    return validated


def load_firmware_package(path: Path, expected_board: str) -> dict:
    if not isinstance(expected_board, str) or not BOARD_ID.fullmatch(expected_board):
        raise ValueError("Enter the exact board profile identifier from the robot firmware.")
    if path.stat().st_size > MAX_FIRMWARE_BYTES + 64 * 1024:
        raise ValueError("Firmware ZIP exceeds the 16 MiB image limit.")
    try:
        archive = zipfile.ZipFile(path)
    except (OSError, zipfile.BadZipFile) as error:
        raise ValueError("Select a ZIP containing the firmware image and manifest.") from error
    with archive:
        if len(archive.infolist()) > 10:
            raise ValueError("Firmware ZIP contains too many entries.")
        entries = [item for item in archive.infolist() if not item.is_dir()]
        if len(entries) != 2 or sum(item.file_size for item in entries) > MAX_FIRMWARE_BYTES:
            raise ValueError("Firmware package must contain exactly a small manifest and one image (max 16 MiB).")
        by_name = {item.filename: item for item in entries}
        if len(by_name) != 2 or "pi-media-hub-robot.json" not in by_name:
            raise ValueError("Firmware ZIP must contain pi-media-hub-robot.json and one .bin image.")
        for name, info in by_name.items():
            pure = PurePosixPath(name)
            mode = info.external_attr >> 16
            if (
                "\\" in name
                or pure.is_absolute()
                or any(part in {"", ".", ".."} for part in name.split("/"))
                or stat.S_ISLNK(mode)
            ):
                raise ValueError("Firmware package contains an unsafe path.")
        image_names = [name for name in by_name if name.lower().endswith(".bin")]
        if len(image_names) != 1:
            raise ValueError("Firmware ZIP must contain exactly one .bin image.")
        try:
            manifest = json.loads(archive.read(by_name["pi-media-hub-robot.json"]))
        except (UnicodeDecodeError, json.JSONDecodeError, RuntimeError) as error:
            raise ValueError("Firmware manifest is not valid JSON.") from error
        if not isinstance(manifest, dict):
            raise ValueError("Firmware manifest must be a JSON object.")
        if by_name["pi-media-hub-robot.json"].file_size > 32 * 1024:
            raise ValueError("Firmware manifest exceeds 32 KiB.")
        if manifest.get("board") != expected_board:
            raise ValueError("Firmware board profile does not match the selected target.")
        version = manifest.get("version")
        if not isinstance(version, str) or not VERSION_ID.fullmatch(version):
            raise ValueError("Firmware manifest needs a valid version.")
        image_name = image_names[0]
        image = archive.read(by_name[image_name])
        digest = hashlib.sha256(image).hexdigest()
        if manifest.get("sha256") != digest:
            raise ValueError("Firmware image checksum does not match its manifest.")
        return {"board": expected_board, "version": version, "sha256": digest, "image": image}


def _request_json(base_url: str, path: str, payload: dict, *, token: str, timeout: float = 3) -> dict:
    token = _validate_token(token)
    data = json.dumps(payload).encode("utf-8")
    request = Request(
        normalize_robot_url(base_url) + path,
        data=data,
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Authorization": "Bearer " + token,
        },
        method="POST",
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            result = json.loads(response.read(16_384))
    except HTTPError as error:
        raise RuntimeError(f"Robot adapter returned HTTP {error.code}.") from error
    except (URLError, TimeoutError, OSError, json.JSONDecodeError) as error:
        raise RuntimeError(f"Could not contact the robot adapter: {error}") from error
    if not isinstance(result, dict):
        raise RuntimeError("Robot adapter returned an invalid response.")
    return result


def check_adapter(base_url: str, token: str, expected_board: str) -> dict:
    token = _validate_token(token)
    request = Request(
        normalize_robot_url(base_url) + "/api/status",
        headers={"Accept": "application/json", "Authorization": "Bearer " + token},
    )
    try:
        with urlopen(request, timeout=3) as response:
            result = json.loads(response.read(16_384))
    except HTTPError as error:
        raise RuntimeError(f"Robot adapter returned HTTP {error.code}.") from error
    except (URLError, TimeoutError, OSError, json.JSONDecodeError) as error:
        raise RuntimeError(f"Could not contact the robot adapter: {error}") from error
    if not isinstance(result, dict):
        raise RuntimeError("Robot adapter returned an invalid status.")
    if (
        result.get("profile") != "pi-media-hub-robot-v1"
        or result.get("motor_stop") is not True
        or not isinstance(result.get("motor_watchdog_ms"), int)
        or not 100 <= result["motor_watchdog_ms"] <= 600
    ):
        raise RuntimeError(
            "This device does not advertise the Pi Media Hub robot profile and a 100-600 ms motor-stop watchdog."
        )
    if not isinstance(expected_board, str) or not BOARD_ID.fullmatch(expected_board) or result.get("board") != expected_board:
        raise RuntimeError("The adapter board profile does not match the selected exact board identifier.")
    return result


def send_control(base_url: str, command: str, duration_ms: int = 0, *, token: str) -> dict:
    payload = build_control_payload(command, duration_ms)
    result = _request_json(base_url, "/api/control", payload, token=token)
    if result.get("accepted") is not True:
        raise RuntimeError("Robot adapter did not confirm the command.")
    return result


def send_stop(base_url: str, *, token: str) -> dict:
    result = _request_json(
        base_url,
        "/api/stop",
        {"command": "stop", "duration_ms": 0},
        token=token,
    )
    if result.get("accepted") is not True:
        raise RuntimeError("Robot adapter did not confirm the stop command.")
    return result


def upload_firmware(base_url: str, firmware: dict, *, token: str, timeout: float = 30) -> dict:
    token = _validate_token(token)
    image = firmware.get("image")
    if not isinstance(image, bytes) or not 0 < len(image) <= MAX_FIRMWARE_BYTES:
        raise ValueError("Firmware image is empty or exceeds the 16 MiB limit.")
    board = firmware.get("board")
    version = firmware.get("version")
    if not isinstance(board, str) or not BOARD_ID.fullmatch(board):
        raise ValueError("Firmware has an invalid board profile.")
    if not isinstance(version, str) or not VERSION_ID.fullmatch(version):
        raise ValueError("Firmware has an invalid version.")
    request = Request(
        normalize_robot_url(base_url) + "/api/firmware/update",
        data=image,
        headers={
            "Content-Type": "application/octet-stream",
            "X-Firmware-Board": board,
            "X-Firmware-Version": version,
            "X-Firmware-SHA256": hashlib.sha256(image).hexdigest(),
            "Authorization": "Bearer " + token,
        },
        method="PUT",
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            result = json.loads(response.read(16_384))
    except HTTPError as error:
        raise RuntimeError(f"Robot OTA endpoint returned HTTP {error.code}.") from error
    except (URLError, TimeoutError, OSError, json.JSONDecodeError) as error:
        raise RuntimeError(f"Could not upload firmware to robot: {error}") from error
    if not isinstance(result, dict) or result.get("status") not in {"accepted", "updated"}:
        raise RuntimeError("Robot OTA endpoint did not confirm an accepted update.")
    return result
