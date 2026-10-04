"""Small CPython/MicroPython shared validation helpers for the robot adapter."""

COMMANDS = ("forward", "backward", "left", "right")
MAX_PULSE_MS = 400
MAX_FIRMWARE_BYTES = 16 * 1024 * 1024


def valid_token(candidate, expected):
    if not isinstance(candidate, str) or not isinstance(expected, str):
        return False
    if len(candidate) != len(expected) or not expected:
        return False
    difference = 0
    for left, right in zip(candidate, expected):
        difference |= ord(left) ^ ord(right)
    return difference == 0


def validate_motion(payload, motor_enabled):
    if not isinstance(payload, dict):
        raise ValueError("Expected a JSON object")
    command = payload.get("command")
    duration = payload.get("duration_ms")
    if command == "stop":
        if duration != 0:
            raise ValueError("STOP duration must be zero")
        return "stop", 0
    if command not in COMMANDS:
        raise ValueError("Unsupported movement command")
    if not isinstance(duration, int) or isinstance(duration, bool):
        raise ValueError("Movement duration must be an integer")
    if not 50 <= duration <= MAX_PULSE_MS:
        raise ValueError("Movement pulse must be 50-400 ms")
    if not motor_enabled:
        raise RuntimeError("Motor output is disabled in this board configuration")
    return command, duration


def validate_ota_headers(board, version, sha256_hex, size, expected_board, enabled):
    if not enabled:
        raise RuntimeError("OTA is disabled until a verified inactive-slot writer is configured")
    if not expected_board or board != expected_board:
        raise ValueError("Firmware board profile mismatch")
    if not isinstance(version, str) or not 1 <= len(version) <= 64:
        raise ValueError("Invalid firmware version")
    if not isinstance(sha256_hex, str) or len(sha256_hex) != 64:
        raise ValueError("Invalid firmware SHA-256 header")
    for character in sha256_hex:
        if character not in "0123456789abcdef":
            raise ValueError("SHA-256 must be lowercase hexadecimal")
    if not isinstance(size, int) or not 0 < size <= MAX_FIRMWARE_BYTES:
        raise ValueError("Firmware image size exceeds the 16 MiB limit")
    return True
