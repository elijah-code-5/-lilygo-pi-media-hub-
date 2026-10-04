"""MicroPython menu client for a LilyGO T-HMI.

Install this file as main.py after putting the exact-board display/touch driver
in t_hmi_adapter.py and user settings in config.py.
"""

import time

import network
try:
    import urequests
except ImportError:
    import requests as urequests
try:
    import ujson as json
except ImportError:
    import json

try:
    import config
except ImportError:
    config = None

try:
    import t_hmi_adapter
except ImportError:
    t_hmi_adapter = None


WIFI_SSID = getattr(config, "WIFI_SSID", "YOUR_WIFI_SSID")
WIFI_PASSWORD = getattr(config, "WIFI_PASSWORD", "YOUR_WIFI_PASSWORD")
SERVER_URL = getattr(config, "SERVER_URL", "http://192.168.1.10:8765").rstrip("/")
ADMIN_TOKEN = getattr(config, "ADMIN_TOKEN", "")
AI_MODEL = getattr(config, "AI_MODEL", "")
POLL_MS = 15000
PAGE_SIZE = 4
MENU = ("Status", "Music", "Videos", "Podcasts", "Assistant", "Wi-Fi info")
MEDIA_KINDS = {"Music": "audio", "Videos": "video", "Podcasts": "podcast"}


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


def get_json(path, authenticated=False):
    headers = {}
    if authenticated:
        if not ADMIN_TOKEN:
            raise RuntimeError("Set ADMIN_TOKEN in config.py to use Assistant")
        headers["Authorization"] = "Bearer " + ADMIN_TOKEN
    response = urequests.get(SERVER_URL + path, headers=headers)
    try:
        if response.status_code != 200:
            raise RuntimeError("Pi returned HTTP {}".format(response.status_code))
        return response.json()
    finally:
        response.close()


def post_json(path, payload):
    if not ADMIN_TOKEN:
        raise RuntimeError("Set ADMIN_TOKEN in config.py to use Assistant")
    response = urequests.post(
        SERVER_URL + path,
        data=json.dumps(payload),
        headers={
            "Authorization": "Bearer " + ADMIN_TOKEN,
            "Content-Type": "application/json",
        },
    )
    try:
        if response.status_code != 200:
            raise RuntimeError("Pi returned HTTP {}".format(response.status_code))
        return response.json()
    finally:
        response.close()


def show(lines):
    safe_lines = [str(line)[:36] for line in lines[:7]]
    print("\n".join(safe_lines))
    if t_hmi_adapter is not None:
        t_hmi_adapter.show(safe_lines)


def wait_key():
    if t_hmi_adapter is None:
        try:
            return input("up/down/select/back (q quits): ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            return "quit"
    key = t_hmi_adapter.read_key()
    return key.lower() if isinstance(key, str) else None


def wait_for_back(title, lines, seconds=5):
    end = time.ticks_add(time.ticks_ms(), seconds * 1000)
    while time.ticks_diff(end, time.ticks_ms()) > 0:
        key = wait_key()
        if key in ("back", "select", "enter", "q", "quit"):
            return
        time.sleep_ms(100)


def show_status(wlan):
    try:
        status = get_json("/api/status")
        lines = [
            "Pi Media Hub",
            "Pi: " + str(status.get("status", "unknown")),
            "Apps: " + str(status.get("apps", "?")),
            "AI enabled: " + str(status.get("ai_enabled", False)),
            "Wi-Fi: " + wlan.ifconfig()[0],
            "Select/back to return",
        ]
    except Exception as error:
        lines = ["Pi Media Hub", "Server unavailable", str(error), "Select/back to return"]
    show(lines)
    wait_for_back("Status", lines)


def show_library(title):
    kind = MEDIA_KINDS[title]
    offset = 0
    while True:
        try:
            data = get_json(
                "/api/library?kind={}&limit={}&offset={}".format(kind, PAGE_SIZE, offset)
            )
        except Exception as error:
            show([title, "Library unavailable", str(error), "Back to menu"])
            wait_for_back(title, [])
            return

        items = data.get("items", [])
        total = data.get("total", len(items))
        lines = [title, "{}/{} items".format(offset + 1 if total else 0, total)]
        for item in items[:4]:
            lines.append(item.get("name", "Unnamed media"))
        if not items:
            lines.append("No media found")
        lines.append("up/down pages; back")
        show(lines)

        key = wait_key()
        if key in ("back", "q", "quit"):
            return
        if key in ("down", "right", "next") and offset + PAGE_SIZE < total:
            offset += PAGE_SIZE
        elif key in ("up", "left", "previous") and offset > 0:
            offset = max(0, offset - PAGE_SIZE)
        else:
            time.sleep_ms(100)


def ask_assistant():
    prompt = ""
    if t_hmi_adapter is not None and hasattr(t_hmi_adapter, "read_text"):
        prompt = t_hmi_adapter.read_text("Ask Pi AI")
    else:
        try:
            prompt = input("Ask Pi AI: ").strip()
        except (EOFError, KeyboardInterrupt):
            return
    if not prompt:
        show(["Assistant", "No question entered", "Back to menu"])
        wait_for_back("Assistant", [], 3)
        return
    try:
        ai = get_json("/api/config/ai", authenticated=True)
        if not ai.get("enabled"):
            raise RuntimeError("Enable a model in desktop Assistant settings first")
        model = AI_MODEL or ai.get("model", "")
        if ai.get("backend") == "Ollama":
            payload = {
                "model": model,
                "messages": [{"role": "user", "content": prompt}],
                "stream": False,
            }
        elif ai.get("backend") == "OpenAI compatible":
            payload = {"model": model, "messages": [{"role": "user", "content": prompt}]}
        else:
            payload = {"message": prompt, "model": model}
        result = post_json("/api/chat", payload)
        answer = (
            result.get("message", {}).get("content")
            or result.get("choices", [{}])[0].get("message", {}).get("content")
            or result.get("response")
            or str(result)
        )
        chunks = [answer[i:i + 32] for i in range(0, len(answer), 32)]
        show(["Assistant"] + chunks[:5] + ["Select/back to return"])
    except Exception as error:
        show(["Assistant", "Request failed", str(error), "Back to menu"])
    wait_for_back("Assistant", [])


def run():
    if t_hmi_adapter is None:
        print("No t_hmi_adapter.py: using terminal input/display for development.")
    if WIFI_SSID == "YOUR_WIFI_SSID":
        show(["Edit config.py", "Set Wi-Fi SSID", "and password first"])
        return
    while True:
        try:
            wlan = connect_wifi()
            break
        except Exception as error:
            show(["Wi-Fi unavailable", str(error), "Retrying in 5s"])
            time.sleep(5)

    selected = 0
    last_poll = time.ticks_add(time.ticks_ms(), -POLL_MS)
    status_label = "Pi status: checking"
    while True:
        if not wlan.isconnected():
            show(["Wi-Fi disconnected", "Reconnecting…"])
            try:
                wlan = connect_wifi()
            except Exception:
                time.sleep(2)
                continue
        if time.ticks_diff(time.ticks_ms(), last_poll) >= POLL_MS:
            try:
                status = get_json("/api/status")
                status_label = "Pi: " + str(status.get("status", "unknown"))
            except Exception:
                status_label = "Pi: offline"
            last_poll = time.ticks_ms()

        lines = [
            "Pi Media Hub",
            status_label,
            "Wi-Fi " + wlan.ifconfig()[0],
            ">" + MENU[selected],
            "up/down; select; back",
        ]
        show(lines)
        key = wait_key()
        if key in ("up", "left"):
            selected = (selected - 1) % len(MENU)
        elif key in ("down", "right"):
            selected = (selected + 1) % len(MENU)
        elif key in ("back", "q", "quit"):
            return
        elif key in ("select", "enter"):
            choice = MENU[selected]
            if choice == "Status":
                show_status(wlan)
            elif choice in MEDIA_KINDS:
                show_library(choice)
            elif choice == "Assistant":
                ask_assistant()
            elif choice == "Wi-Fi info":
                show(["Wi-Fi connected", wlan.ifconfig()[0], "Pi Hub", SERVER_URL, "Select/back"])
                wait_for_back("Wi-Fi info", [])
        if t_hmi_adapter is not None:
            time.sleep_ms(120)


if __name__ == "__main__":
    run()
