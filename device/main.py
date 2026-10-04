"""MicroPython Wi-Fi status/menu client for a LilyGO T-HMI."""

import json
import time

import network
import urequests

try:
    import t_hmi_adapter
except ImportError:
    t_hmi_adapter = None


WIFI_SSID = "YOUR_WIFI_SSID"
WIFI_PASSWORD = "YOUR_WIFI_PASSWORD"
SERVER_URL = "http://192.168.1.10:8765"
POLL_SECONDS = 15


def connect_wifi():
    wlan = network.WLAN(network.STA_IF)
    wlan.active(True)
    if not wlan.isconnected():
        wlan.connect(WIFI_SSID, WIFI_PASSWORD)
        deadline = time.ticks_add(time.ticks_ms(), 20000)
        while not wlan.isconnected() and time.ticks_diff(deadline, time.ticks_ms()) > 0:
            time.sleep_ms(250)
    if not wlan.isconnected():
        raise RuntimeError("Wi-Fi connection timed out")
    return wlan


def get_json(path):
    response = urequests.get(SERVER_URL + path)
    try:
        if response.status_code != 200:
            raise RuntimeError("Server returned HTTP {}".format(response.status_code))
        return response.json()
    finally:
        response.close()


def show(lines):
    print("\n".join(lines))
    if t_hmi_adapter is not None:
        t_hmi_adapter.show(lines)


def run():
    wlan = connect_wifi()
    menu = ["Status", "Audio", "Video", "Podcasts"]
    selected = 0
    while True:
        show([
            "Pi Media Hub",
            "Wi-Fi: " + wlan.ifconfig()[0],
            "Server: " + SERVER_URL,
            "> " + menu[selected],
            "up/down/select; q to quit",
        ])
        try:
            status = get_json("/api/status")
            show(["Pi Media Hub", "Server: " + status["status"], "AI: " + str(status["ai_enabled"]), "> " + menu[selected]])
        except Exception as error:
            show(["Pi Media Hub", "Server unavailable", str(error)[:32]])

        key = None
        if t_hmi_adapter is not None:
            key = t_hmi_adapter.read_key()
        else:
            try:
                key = input("up/down/select/q: ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                return
        if key in ("up", "left"):
            selected = (selected - 1) % len(menu)
        elif key in ("down", "right"):
            selected = (selected + 1) % len(menu)
        elif key in ("select", "enter"):
            kinds = {"Audio": "audio", "Video": "video", "Podcasts": "podcast"}
            kind = kinds.get(menu[selected])
            if kind:
                try:
                    items = get_json("/api/library?kind=" + kind)["items"]
                    names = [item["name"] for item in items[:4]] or ["No media found"]
                    show([menu[selected] + " ({})".format(len(items))] + names)
                except Exception as error:
                    show(["Library unavailable", str(error)[:32]])
            else:
                show(["Pi Media Hub", "Choose a media section"])
            time.sleep(2)
        elif key in ("q", "quit"):
            return
        time.sleep(POLL_SECONDS if t_hmi_adapter is not None else 0)


run()
