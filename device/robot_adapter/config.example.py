"""Copy to config.py on the target ESP32. All motor/OTA features stay off."""

WIFI_SSID = "YOUR_WIFI_SSID"
WIFI_PASSWORD = "YOUR_WIFI_PASSWORD"
ADMIN_TOKEN = "REPLACE_WITH_AT_LEAST_24_RANDOM_ASCII_CHARACTERS"

# Must match an explicitly reviewed board profile. Empty means status-only.
BOARD_ID = ""
FIRMWARE_VERSION = "0.1.0-template"
ALLOWED_CLIENT_PREFIXES = ("192.168.1.",)

# Safety gate: leave False until a board-specific driver has been implemented,
# reviewed, and tested with wheels lifted and a physical power cut available.
MOTOR_ENABLED = False

# OTA requires an inactive-slot writer supplied for the exact board/partition
# layout. No generic MicroPython flash writer is bundled.
OTA_ENABLED = False
