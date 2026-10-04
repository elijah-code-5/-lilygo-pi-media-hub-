from __future__ import annotations

import contextlib
import io
import json
import os
import platform
import queue
import re
import secrets
import threading
import tempfile
import time
import tkinter as tk
import webbrowser
from pathlib import Path
from tkinter import filedialog, messagebox, simpledialog, ttk
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit
from urllib.request import Request, urlopen

from .local_apps import (
    WavRecorder,
    default_notes_path,
    load_notes,
    new_note,
    normalize_browser_url,
    save_notes,
)
from .robot import (
    MAX_PULSE_MS,
    check_adapter,
    load_firmware_package,
    normalize_robot_url,
    send_control,
    send_stop,
    upload_firmware,
    validate_program,
)


DEFAULT_SERVER = "http://raspberrypi.local:8765"
REQUEST_TIMEOUT = 10
APP_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,47}$")

COLORS = {
    "background": "#10151d",
    "sidebar": "#151d28",
    "surface": "#1b2634",
    "surface2": "#233244",
    "border": "#2b3b4f",
    "text": "#edf3fb",
    "muted": "#a1b0c2",
    "accent": "#50d8bb",
    "blue": "#76a9ff",
    "danger": "#ff7e83",
}


def normalize_server_url(value: str) -> str:
    value = value.strip().rstrip("/")
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("Enter a Pi address such as http://192.168.1.20:8765")
    if parsed.path not in ("", "/") or parsed.query or parsed.fragment:
        raise ValueError("Use only the server origin, without a path, query, or fragment")
    try:
        parsed.port
    except ValueError as error:
        raise ValueError("The server address contains an invalid port") from error
    return value


def build_flash_args(port: str, image: str, offset: str, baud: str) -> list[str]:
    if not re.fullmatch(r"/dev/(tty(USB|ACM|S|AMA)[0-9]+|serial/by-id/[A-Za-z0-9._:+-]+)|COM[1-9][0-9]*", port, re.IGNORECASE):
        raise ValueError("Select a detected serial device")
    firmware = Path(image).expanduser().resolve()
    if not firmware.is_file() or firmware.suffix.lower() != ".bin":
        raise ValueError("Select an existing .bin firmware image")
    try:
        address = int(offset, 0)
        speed = int(baud)
    except ValueError as error:
        raise ValueError("Flash address and baud rate must be valid numbers") from error
    if not 0 <= address <= 0xFFFFFFFF or not 9600 <= speed <= 2_000_000:
        raise ValueError("Flash address or baud rate is outside the supported range")
    return ["--chip", "esp32s3", "--port", port, "--baud", str(speed), "write-flash", hex(address), str(firmware)]


def normalize_ai_endpoint(backend: str, endpoint: str) -> str:
    endpoint = endpoint.strip().rstrip("/")
    if backend != "OpenAI compatible" or not endpoint:
        return endpoint
    if endpoint.endswith("/chat/completions"):
        return endpoint
    if endpoint.endswith("/v1"):
        return endpoint + "/chat/completions"
    return endpoint + "/v1/chat/completions"


def load_controller_settings() -> dict:
    path = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "pi-media-hub" / "controller.json"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def save_controller_settings(settings: dict) -> None:
    path = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "pi-media-hub" / "controller.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_name = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as temporary:
            temporary_name = temporary.name
            json.dump(settings, temporary, indent=2)
            temporary.write("\n")
            temporary.flush()
            os.fsync(temporary.fileno())
        os.chmod(temporary_name, 0o600)
        os.replace(temporary_name, path)
    except OSError:
        if temporary_name:
            Path(temporary_name).unlink(missing_ok=True)
        raise


def request_json(base_url: str, path: str, body: object | None = None, *, token: str = "", method: str | None = None) -> object:
    data = None if body is None else json.dumps(body).encode("utf-8")
    headers = {"Accept": "application/json"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = "Bearer " + token
    request = Request(
        base_url + path,
        data=data,
        headers=headers,
        method=method or ("GET" if data is None else "POST"),
    )
    try:
        with urlopen(request, timeout=REQUEST_TIMEOUT) as response:
            raw = response.read(1024 * 1024 + 1)
            if len(raw) > 1024 * 1024:
                raise RuntimeError("Pi response exceeded the 1 MiB limit")
            return json.loads(raw)
    except HTTPError as error:
        detail = error.read(4096).decode("utf-8", errors="replace")
        raise RuntimeError(f"Pi returned HTTP {error.code}: {detail}") from error
    except (URLError, TimeoutError, OSError, json.JSONDecodeError) as error:
        raise RuntimeError(f"Could not contact Pi: {error}") from error


class MediaHubApp:
    PAGES = (
        "Overview",
        "Music",
        "Videos",
        "Media library",
        "Recorder",
        "Browser",
        "Notebook",
        "Assistant",
        "App shelf",
        "Robot",
        "T-HMI",
        "Setup",
    )

    def __init__(self, root: tk.Tk):
        self.root = root
        self.settings = load_controller_settings()
        self.root.title("Pi Media Hub")
        self.root.geometry("1160x780")
        self.root.minsize(900, 640)
        self.root.configure(bg=COLORS["background"])
        self.server_url = tk.StringVar(value=self.settings.get("server_url", DEFAULT_SERVER))
        self.admin_token = tk.StringVar(value=self.settings.get("admin_token", ""))
        self.connection_text = tk.StringVar(value="Pi not connected")
        self.active_page = tk.StringVar(value="Overview")
        self.kind = tk.StringVar(value="all")
        self.search = tk.StringVar()
        self.apps: list[dict] = []
        self.media_items: list[dict] = []
        self.local_server = None
        self.detected_ports = set()
        self.serial_connection = None
        self.serial_reader = None
        self.media_views = {}
        self.notes = []
        self.active_note_id = None
        self.note_save_after = None
        self.recorder = None
        self.recording_path = tk.StringVar(value=str(Path.home() / "Music" / "Recording.wav"))
        self.recording_status = tk.StringVar(value="Ready to record locally on this computer.")
        self.browser_address = tk.StringVar(value="https://")
        self.robot_address = tk.StringVar(value=self.settings.get("robot_url", ""))
        self.robot_board = tk.StringVar(value=self.settings.get("robot_board", ""))
        self.robot_token = tk.StringVar(value=self.settings.get("robot_token", ""))
        self.robot_status = tk.StringVar(value="No verified robot adapter connected.")
        self.robot_adapter_verified = False
        self.robot_firmware_path = tk.StringVar()
        self.robot_program_text = None
        self.robot_jobs = queue.Queue()
        self.robot_worker_active = False
        self.robot_worker_lock = threading.Lock()
        self.robot_cancel_event = threading.Event()
        self.closing = False
        self.note_title = None
        self.note_body = None
        self._style()
        self._build()
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        if self.settings.get("server_url"):
            self.root.after(250, self.connect)

    def _style(self):
        style = ttk.Style()
        style.theme_use("clam")
        style.configure(".", background=COLORS["background"], foreground=COLORS["text"], font=("Sans", 10))
        style.configure("TFrame", background=COLORS["background"])
        style.configure("Card.TFrame", background=COLORS["surface"])
        style.configure("Sidebar.TFrame", background=COLORS["sidebar"])
        style.configure("TLabel", background=COLORS["background"], foreground=COLORS["text"])
        style.configure("Muted.TLabel", foreground=COLORS["muted"])
        style.configure("Hero.TLabel", font=("Sans", 22, "bold"))
        style.configure("CardTitle.TLabel", background=COLORS["surface"], foreground=COLORS["muted"])
        style.configure("CardValue.TLabel", background=COLORS["surface"], font=("Sans", 21, "bold"))
        style.configure("TButton", background=COLORS["surface2"], foreground=COLORS["text"], padding=(12, 8), borderwidth=0)
        style.map("TButton", background=[("active", COLORS["border"]), ("disabled", COLORS["surface"])])
        style.configure("Accent.TButton", background=COLORS["accent"], foreground="#071711", font=("Sans", 10, "bold"))
        style.map("Accent.TButton", background=[("active", "#79ead0")])
        style.configure("Nav.TButton", background=COLORS["sidebar"], anchor="w", padding=(15, 11))
        style.map("Nav.TButton", background=[("active", COLORS["surface2"])])
        style.configure("Treeview", background=COLORS["surface"], fieldbackground=COLORS["surface"], foreground=COLORS["text"], rowheight=30, borderwidth=0)
        style.configure("Treeview.Heading", background=COLORS["surface2"], foreground=COLORS["muted"], relief="flat")
        style.map("Treeview", background=[("selected", "#285d62")])
        style.configure("TEntry", fieldbackground=COLORS["surface2"], foreground=COLORS["text"], insertcolor=COLORS["text"], bordercolor=COLORS["border"])
        style.configure("TCombobox", fieldbackground=COLORS["surface2"], foreground=COLORS["text"], arrowcolor=COLORS["text"])

    def _build(self):
        shell = ttk.Frame(self.root)
        shell.pack(fill="both", expand=True)
        sidebar = ttk.Frame(shell, style="Sidebar.TFrame", width=210, padding=(14, 20))
        sidebar.pack(side="left", fill="y")
        sidebar.pack_propagate(False)
        title = tk.Label(sidebar, text="PI  /  MEDIA", bg=COLORS["sidebar"], fg=COLORS["accent"], font=("Sans", 16, "bold"))
        title.pack(anchor="w", pady=(0, 24))
        tk.Label(sidebar, text="YOUR HOME LIBRARY", bg=COLORS["sidebar"], fg=COLORS["muted"], font=("Sans", 8, "bold")).pack(anchor="w", pady=(0, 8))
        for page in self.PAGES:
            ttk.Button(sidebar, text="  " + page, style="Nav.TButton", command=lambda p=page: self.show_page(p)).pack(fill="x", pady=2)
        ttk.Frame(sidebar, style="Sidebar.TFrame").pack(fill="both", expand=True)
        tk.Label(sidebar, text="Private LAN • Pi hosted", bg=COLORS["sidebar"], fg=COLORS["muted"], font=("Sans", 9)).pack(anchor="w")

        main = ttk.Frame(shell, padding=(22, 16, 22, 20))
        main.pack(side="left", fill="both", expand=True)
        top = ttk.Frame(main)
        top.pack(fill="x", pady=(0, 15))
        self.page_title = ttk.Label(top, text="Overview", style="Hero.TLabel")
        self.page_title.pack(side="left")
        ttk.Label(top, textvariable=self.connection_text, style="Muted.TLabel").pack(side="right")
        connect_bar = ttk.Frame(main, style="Card.TFrame", padding=10)
        connect_bar.pack(fill="x", pady=(0, 14))
        ttk.Label(connect_bar, text="PI SERVER", style="CardTitle.TLabel").pack(side="left", padx=(3, 8))
        address = ttk.Entry(connect_bar, textvariable=self.server_url)
        address.pack(side="left", fill="x", expand=True, padx=5)
        address.bind("<Return>", lambda _event: self.connect())
        ttk.Button(connect_bar, text="Connect", style="Accent.TButton", command=self.connect).pack(side="left", padx=(6, 2))

        self.page_host = ttk.Frame(main)
        self.page_host.pack(fill="both", expand=True)
        self.pages = {}
        self._build_overview()
        self._build_media()
        self._build_apps()
        self._build_ai()
        self._build_firmware()
        self._build_media_collection("Music", "audio")
        self._build_media_collection("Videos", "video")
        self._build_recorder()
        self._build_browser()
        self._build_notebook()
        self._build_robot()
        self._build_setup()
        self.show_page("Overview")

    def _card(self, parent, title, value, column):
        card = ttk.Frame(parent, style="Card.TFrame", padding=16)
        card.grid(row=0, column=column, sticky="nsew", padx=(0 if column == 0 else 8, 8))
        ttk.Label(card, text=title.upper(), style="CardTitle.TLabel").pack(anchor="w")
        label = ttk.Label(card, text=value, style="CardValue.TLabel")
        label.pack(anchor="w", pady=(8, 2))
        return label

    def _new_page(self, name):
        frame = ttk.Frame(self.page_host)
        self.pages[name] = frame
        return frame

    def _build_overview(self):
        page = self._new_page("Overview")
        ttk.Label(page, text="Everything on your Pi, in one place.", font=("Sans", 13)).pack(anchor="w", pady=(3, 16))
        cards = ttk.Frame(page)
        cards.pack(fill="x")
        cards.columnconfigure((0, 1, 2), weight=1)
        self.server_card = self._card(cards, "Pi status", "Not connected", 0)
        self.media_card = self._card(cards, "Media files", "—", 1)
        self.apps_card = self._card(cards, "Apps", "—", 2)
        guide = ttk.Frame(page, style="Card.TFrame", padding=18)
        guide.pack(fill="x", pady=18)
        ttk.Label(guide, text="GET STARTED", style="CardTitle.TLabel").pack(anchor="w")
        ttk.Label(
            guide,
            text="1. Start the Pi Media Hub service on your Raspberry Pi.\n"
                 "2. Enter its LAN address above, for example http://192.168.1.42:8765.\n"
                 "3. Browse albums and videos, add web apps, or configure your local AI backend.",
            justify="left",
        ).pack(anchor="w", pady=(10, 0))
        self._host_tools(page)

    def _host_tools(self, parent):
        if platform.machine().lower() not in {"aarch64", "arm64"}:
            return
        panel = ttk.Frame(parent, style="Card.TFrame", padding=16)
        panel.pack(fill="x", pady=8)
        ttk.Label(panel, text="RUN A TEMPORARY SERVER ON THIS PI", style="CardTitle.TLabel").pack(anchor="w")
        self.local_media = tk.StringVar(value=str(Path.home() / "Media"))
        row = ttk.Frame(panel, style="Card.TFrame")
        row.pack(fill="x", pady=(8, 0))
        ttk.Entry(row, textvariable=self.local_media).pack(side="left", fill="x", expand=True)
        ttk.Button(row, text="Choose folder", command=self.choose_media).pack(side="left", padx=6)
        self.local_button = ttk.Button(row, text="Start temporary server", command=self.toggle_local_server)
        self.local_button.pack(side="left")
        ttk.Label(panel, text="Temporary: this stops when the window closes. For boot startup, install the Pi service.", style="Muted.TLabel").pack(anchor="w", pady=(7, 0))

    def _build_media(self):
        page = self._new_page("Media library")
        ttk.Label(page, text="Browse your music, movies, and podcast folders.", style="Muted.TLabel").pack(anchor="w", pady=(0, 12))
        bar = ttk.Frame(page)
        bar.pack(fill="x", pady=(0, 10))
        search = ttk.Entry(bar, textvariable=self.search)
        search.pack(side="left", fill="x", expand=True)
        search.insert(0, "")
        search.bind("<Return>", lambda _event: self.load_library())
        ttk.Combobox(bar, textvariable=self.kind, state="readonly", width=12, values=("all", "audio", "video", "podcast")).pack(side="left", padx=8)
        ttk.Button(bar, text="Search", command=self.load_library).pack(side="left")
        ttk.Button(bar, text="Play / open", style="Accent.TButton", command=self.open_selected_media).pack(side="left", padx=(8, 0))
        self.library_tree = self._tree(page, ("name", "kind", "size", "folder"), ("Title", "Type", "Size", "Folder"), (310, 100, 110, 240))
        self.library_tree.bind("<Double-1>", lambda _event: self.open_selected_media())
        self.media_empty = ttk.Label(page, text="Connect to the Pi to load your library.", style="Muted.TLabel")
        self.media_empty.pack(anchor="w", pady=8)

    def _build_media_collection(self, name, kind):
        page = self._new_page(name)
        ttk.Label(
            page,
            text=f"Browse and open {kind} stored on the Raspberry Pi.",
            style="Muted.TLabel",
        ).pack(anchor="w", pady=(0, 12))
        actions = ttk.Frame(page)
        actions.pack(fill="x", pady=(0, 10))
        ttk.Button(actions, text="Refresh", command=lambda k=kind: self.load_media_collection(k)).pack(side="left")
        ttk.Button(
            actions,
            text="Play / open selected",
            style="Accent.TButton",
            command=lambda k=kind: self.open_media_collection_item(k),
        ).pack(side="left", padx=8)
        tree = self._tree(
            page,
            ("name", "size", "folder"),
            ("Title", "Size", "Folder"),
            (330, 110, 330),
        )
        tree.bind("<Double-1>", lambda _event, k=kind: self.open_media_collection_item(k))
        empty = ttk.Label(page, text=f"Connect to the Pi to browse {kind}.", style="Muted.TLabel")
        empty.pack(anchor="w", pady=8)
        self.media_views[kind] = {"tree": tree, "empty": empty, "items": []}

    def _build_apps(self):
        page = self._new_page("App shelf")
        ttk.Label(page, text="Curate shortcuts to trusted web apps. Imported entries do not run code on the Pi.", style="Muted.TLabel", wraplength=800).pack(anchor="w", pady=(0, 12))
        row = ttk.Frame(page)
        row.pack(fill="x", pady=(0, 10))
        for label, callback in (
            ("Create shortcut", self.create_app),
            ("Upload web app ZIP", self.upload_app_archive),
            ("Import GitHub repo", self.import_github),
            ("Add shortcut", self.import_manifest),
            ("Remove", self.remove_app),
            ("Launch", self.launch_app),
            ("Refresh", self.load_apps),
        ):
            ttk.Button(row, text=label, command=callback).pack(side="left", padx=(0, 6))
        self.app_tree = self._tree(page, ("name", "id", "description", "url"), ("App", "ID", "About", "Launch URL"), (160, 130, 220, 260))
        self.apps_empty = ttk.Label(page, text="Apps are shortcuts only—no downloaded code is executed.", style="Muted.TLabel")
        self.apps_empty.pack(anchor="w", pady=8)
        self._admin_token_control(page)

    def _admin_token_control(self, parent):
        box = ttk.LabelFrame(parent, text="Pi admin token", padding=8)
        box.pack(fill="x", pady=(10, 0))
        ttk.Entry(box, textvariable=self.admin_token, show="•").pack(side="left", fill="x", expand=True)
        ttk.Button(box, text="Save on this device", command=self.persist_settings).pack(side="left", padx=(8, 0))
        ttk.Label(parent, text="Get the token from /etc/pi-media-hub/config.json on the Pi. Stored locally with owner-only permissions.", style="Muted.TLabel").pack(anchor="w", pady=(5, 0))

    def _build_ai(self):
        page = self._new_page("Assistant")
        ttk.Label(page, text="Connect the Pi to a local model service—Ollama, an OpenAI-compatible server, or your own HTTP backend.", style="Muted.TLabel", wraplength=850).pack(anchor="w", pady=(0, 12))
        settings = ttk.Frame(page, style="Card.TFrame", padding=14)
        settings.pack(fill="x", pady=(0, 12))
        self.ai_enabled = tk.BooleanVar(value=False)
        self.ai_backend = tk.StringVar(value="Ollama")
        self.ai_endpoint = tk.StringVar()
        self.ai_model = tk.StringVar()
        row = ttk.Frame(settings, style="Card.TFrame")
        row.pack(fill="x", pady=4)
        ttk.Checkbutton(row, text="Enable configured Pi AI", variable=self.ai_enabled).pack(side="left")
        ttk.Combobox(row, textvariable=self.ai_backend, state="readonly", values=("Ollama", "OpenAI compatible", "Custom"), width=22).pack(side="right")
        self._field(settings, "Backend URL", self.ai_endpoint, "http://127.0.0.1:11434/api/chat")
        self._field(settings, "Model name", self.ai_model, "e.g. qwen2.5:1.5b")
        ttk.Button(settings, text="Save backend on Pi", style="Accent.TButton", command=self.save_ai_settings).pack(anchor="e", pady=(6, 0))
        self.chat_history = tk.Text(page, height=15, bg=COLORS["surface"], fg=COLORS["text"], insertbackground=COLORS["text"], relief="flat", wrap="word", padx=12, pady=12)
        self.chat_history.pack(fill="both", expand=True)
        chat = ttk.Frame(page)
        chat.pack(fill="x", pady=(10, 0))
        self.chat_input = ttk.Entry(chat)
        self.chat_input.pack(side="left", fill="x", expand=True)
        self.chat_input.bind("<Return>", lambda _event: self.send_chat())
        ttk.Button(chat, text="Send", style="Accent.TButton", command=self.send_chat).pack(side="left", padx=(8, 0))

    def _field(self, parent, label, variable, placeholder):
        row = ttk.Frame(parent, style="Card.TFrame")
        row.pack(fill="x", pady=4)
        ttk.Label(row, text=label, width=16, style="CardTitle.TLabel").pack(side="left")
        entry = ttk.Entry(row, textvariable=variable)
        entry.pack(side="left", fill="x", expand=True)
        ttk.Label(row, text=placeholder, style="Muted.TLabel").pack(side="right", padx=(6, 0))

    def _build_recorder(self):
        page = self._new_page("Recorder")
        ttk.Label(page, text="Voice recorder", style="Hero.TLabel").pack(anchor="w")
        ttk.Label(
            page,
            text="Record microphone input on this controller and save a standard WAV file. Nothing is uploaded to the Pi.",
            style="Muted.TLabel",
            wraplength=850,
        ).pack(anchor="w", pady=(8, 16))
        panel = ttk.Frame(page, style="Card.TFrame", padding=18)
        panel.pack(fill="x")
        row = ttk.Frame(panel, style="Card.TFrame")
        row.pack(fill="x")
        ttk.Entry(row, textvariable=self.recording_path).pack(side="left", fill="x", expand=True)
        ttk.Button(row, text="Choose file…", command=self.choose_recording_path).pack(side="left", padx=(8, 0))
        self.record_button = ttk.Button(
            panel,
            text="Start recording",
            style="Accent.TButton",
            command=self.toggle_recording,
        )
        self.record_button.pack(anchor="w", pady=(14, 6))
        ttk.Label(panel, textvariable=self.recording_status, style="Muted.TLabel", wraplength=850).pack(anchor="w")
        ttk.Label(
            page,
            text="Uses the selected/default system microphone. If no input device is available, check Linux microphone permissions and audio services.",
            style="Muted.TLabel",
            wraplength=850,
        ).pack(anchor="w", pady=12)

    def _build_browser(self):
        page = self._new_page("Browser")
        ttk.Label(page, text="Open a website", style="Hero.TLabel").pack(anchor="w")
        ttk.Label(
            page,
            text="This opens your address in the computer's default browser; it is not an embedded web engine.",
            style="Muted.TLabel",
        ).pack(anchor="w", pady=(8, 16))
        row = ttk.Frame(page, style="Card.TFrame", padding=14)
        row.pack(fill="x")
        address = ttk.Entry(row, textvariable=self.browser_address)
        address.pack(side="left", fill="x", expand=True)
        address.bind("<Return>", lambda _event: self.open_browser_address())
        ttk.Button(row, text="Open", style="Accent.TButton", command=self.open_browser_address).pack(side="left", padx=(8, 0))
        ttk.Label(
            page,
            text="For Pi apps, use App Shelf. App pages are sandboxed static web assets; native programs are not executed by the Pi server.",
            style="Muted.TLabel",
            wraplength=850,
        ).pack(anchor="w", pady=12)

    def _build_notebook(self):
        page = self._new_page("Notebook")
        ttk.Label(page, text="Notebook", style="Hero.TLabel").pack(anchor="w")
        ttk.Label(
            page,
            text="Private notes saved on this controller. Use text and Markdown; this is not a stylus/ink notebook.",
            style="Muted.TLabel",
        ).pack(anchor="w", pady=(8, 12))
        body = ttk.Frame(page)
        body.pack(fill="both", expand=True)
        left = ttk.Frame(body, style="Card.TFrame", padding=8)
        left.pack(side="left", fill="y", padx=(0, 10))
        ttk.Button(left, text="New note", command=self.create_note).pack(fill="x", pady=(0, 6))
        ttk.Button(left, text="Delete note", command=self.delete_note).pack(fill="x", pady=(0, 8))
        self.notes_tree = ttk.Treeview(left, columns=("title",), show="headings")
        self.notes_tree.heading("title", text="Your notes")
        self.notes_tree.column("title", width=210)
        self.notes_tree.pack(fill="both", expand=True)
        self.notes_tree.bind("<<TreeviewSelect>>", self.select_note)
        editor = ttk.Frame(body, style="Card.TFrame", padding=12)
        editor.pack(side="left", fill="both", expand=True)
        self.note_title_var = tk.StringVar()
        title = ttk.Entry(editor, textvariable=self.note_title_var, font=("Sans", 14, "bold"))
        title.pack(fill="x", pady=(0, 8))
        title.bind("<KeyRelease>", self.note_edited)
        self.note_body = tk.Text(
            editor,
            bg=COLORS["surface"],
            fg=COLORS["text"],
            insertbackground=COLORS["text"],
            relief="flat",
            wrap="word",
            padx=12,
            pady=12,
        )
        self.note_body.pack(fill="both", expand=True)
        self.note_body.bind("<<Modified>>", self.note_body_modified)
        ttk.Button(editor, text="Save note", command=self.save_current_note).pack(anchor="e", pady=(8, 0))
        self._load_notes_into_ui()

    def _build_robot(self):
        page = self._new_page("Robot")
        ttk.Label(page, text="Robot workshop", style="Hero.TLabel").pack(anchor="w")
        ttk.Label(
            page,
            text="Manual pulses, bounded programs, and checksum-checked Wi-Fi updates for a compatible ESP32 adapter.",
            style="Muted.TLabel",
            wraplength=850,
        ).pack(anchor="w", pady=(8, 12))
        connection = ttk.Frame(page, style="Card.TFrame", padding=12)
        connection.pack(fill="x", pady=(0, 10))
        ttk.Label(connection, text="Robot adapter address").pack(side="left")
        ttk.Entry(connection, textvariable=self.robot_address, width=32).pack(side="left", padx=8)
        ttk.Label(connection, text="Board profile").pack(side="left", padx=(8, 0))
        ttk.Entry(connection, textvariable=self.robot_board, width=20).pack(side="left", padx=8)
        ttk.Label(connection, text="Adapter token").pack(side="left", padx=(4, 0))
        ttk.Entry(connection, textvariable=self.robot_token, width=16, show="•").pack(side="left", padx=8)
        ttk.Button(connection, text="Verify adapter", command=self.verify_robot_adapter).pack(side="left")
        ttk.Label(page, textvariable=self.robot_status, style="Muted.TLabel", wraplength=850).pack(anchor="w", pady=(0, 10))

        control = ttk.Frame(page, style="Card.TFrame", padding=14)
        control.pack(fill="x", pady=(0, 10))
        ttk.Label(control, text="MANUAL • finite movement pulse, maximum 400 ms", style="CardTitle.TLabel").pack(anchor="w", pady=(0, 8))
        pad = ttk.Frame(control, style="Card.TFrame")
        pad.pack()
        for label, command, row, column in (
            ("Forward", "forward", 0, 1),
            ("Left", "left", 1, 0),
            ("STOP", "stop", 1, 1),
            ("Right", "right", 1, 2),
            ("Reverse", "backward", 2, 1),
        ):
            button = ttk.Button(
                pad,
                text=label,
                style="Accent.TButton" if command == "stop" else "TButton",
                command=lambda c=command: self.robot_action("stop") if c == "stop" else None,
            )
            button.grid(row=row, column=column, padx=4, pady=4, sticky="nsew")
            if command != "stop":
                button.bind("<ButtonPress-1>", lambda _event, c=command: self.robot_action(c, MAX_PULSE_MS))
                button.bind("<ButtonRelease-1>", lambda _event: self.robot_action("stop"))
        ttk.Label(
            control,
            text="Device-side watchdog is mandatory. Controls remain disabled until a compatible adapter reports its stop capability.",
            style="Muted.TLabel",
        ).pack(anchor="w", pady=(8, 0))

        programming = ttk.LabelFrame(page, text="Program (JSON steps, no code execution)", padding=10)
        programming.pack(fill="both", expand=True, pady=(0, 10))
        self.robot_program_text = tk.Text(
            programming,
            height=5,
            bg=COLORS["surface"],
            fg=COLORS["text"],
            insertbackground=COLORS["text"],
            relief="flat",
            wrap="none",
        )
        self.robot_program_text.pack(fill="both", expand=True)
        self.robot_program_text.insert(
            "1.0",
            '[{"command":"forward","duration_ms":150},{"command":"right","duration_ms":120}]',
        )
        program_bar = ttk.Frame(programming)
        program_bar.pack(fill="x", pady=(8, 0))
        ttk.Button(program_bar, text="Run bounded program…", command=self.run_robot_program).pack(side="left")
        ttk.Button(program_bar, text="Stop now", style="Accent.TButton", command=lambda: self.robot_action("stop")).pack(side="left", padx=8)

        firmware = ttk.LabelFrame(page, text="Wi-Fi OTA update", padding=10)
        firmware.pack(fill="x")
        ttk.Entry(firmware, textvariable=self.robot_firmware_path).pack(side="left", fill="x", expand=True)
        ttk.Button(firmware, text="Choose firmware ZIP…", command=self.choose_robot_firmware).pack(side="left", padx=6)
        ttk.Button(firmware, text="Verify & update…", command=self.update_robot_firmware).pack(side="left")
        ttk.Label(
            page,
            text="Face tracking, kit-specific pins, stock Freenove control, and board firmware builds are not enabled without the exact kit/camera and tested firmware. OTA requires the selected adapter's fixed endpoint.",
            style="Muted.TLabel",
            wraplength=850,
        ).pack(anchor="w", pady=(10, 0))

    def _build_setup(self):
        page = self._new_page("Setup")
        ttk.Label(page, text="Host & app setup", style="Hero.TLabel").pack(anchor="w")
        ttk.Label(
            page,
            text="The Raspberry Pi hosts media, static apps, and AI proxy settings. This panel helps with setup but never writes an OS image or executes remote shell commands.",
            style="Muted.TLabel",
            wraplength=850,
        ).pack(anchor="w", pady=(8, 14))
        panel = ttk.Frame(page, style="Card.TFrame", padding=16)
        panel.pack(fill="x")
        ttk.Label(panel, text="1 · Install Raspberry Pi OS", style="CardTitle.TLabel").pack(anchor="w")
        ttk.Label(
            panel,
            text="Use Raspberry Pi Imager on the SD card/USB drive, then boot the Pi and connect it to your trusted LAN. OS imaging is destructive and intentionally stays in the official Imager.",
            wraplength=850,
        ).pack(anchor="w", pady=6)
        ttk.Button(panel, text="Open Raspberry Pi Imager download page", command=lambda: webbrowser.open("https://www.raspberrypi.com/software/")).pack(anchor="w", pady=(0, 12))
        ttk.Label(panel, text="2 · Install the Pi Media Hub server", style="CardTitle.TLabel").pack(anchor="w")
        ttk.Label(
            panel,
            text="Copy the ARM64 AppImage to the Pi and run its documented `install` command once. It installs the Python service and systemd unit; it does not install the OS or issue shell commands from this controller.",
            wraplength=850,
        ).pack(anchor="w", pady=6)
        ttk.Label(panel, text="3 · Install apps", style="CardTitle.TLabel").pack(anchor="w")
        ttk.Label(
            panel,
            text="Use App Shelf to create shortcuts, upload a static web-app ZIP, or import a compatible GitHub repository. The Pi does not run arbitrary app code. Built-in Music, Videos, Recorder, Browser, Notebook, Assistant, and Robot tools are part of this desktop app.",
            wraplength=850,
        ).pack(anchor="w", pady=6)
        ttk.Button(panel, text="Open Pi Media Hub setup guide", command=lambda: webbrowser.open("https://github.com/elijah-code-5/-lilygo-pi-media-hub-/blob/elijah-code-5-pi-media-hub-mvp/README.md")).pack(anchor="w", pady=(6, 0))

    def _build_firmware(self):
        page = self._new_page("T-HMI")
        ttk.Label(page, text="T-HMI firmware workspace", style="Hero.TLabel").pack(anchor="w")
        ttk.Label(
            page,
            text="Select a prebuilt ESP32-S3 MicroPython .bin, identify the connected board's USB serial port, "
                 "and flash only after confirming the destructive operation. Firmware compilation is not provided.",
            style="Muted.TLabel", wraplength=850,
        ).pack(anchor="w", pady=(8, 14))
        card = ttk.Frame(page, style="Card.TFrame", padding=16)
        card.pack(fill="x")
        self.firmware_path = tk.StringVar()
        self.serial_port = tk.StringVar()
        self.flash_offset = tk.StringVar(value="0x0")
        self.flash_baud = tk.StringVar(value="460800")
        self.flash_status = tk.StringVar(value="No flash operation has run.")
        row = ttk.Frame(card, style="Card.TFrame")
        row.pack(fill="x", pady=5)
        ttk.Label(row, text="Firmware .bin", width=16, style="CardTitle.TLabel").pack(side="left")
        ttk.Entry(row, textvariable=self.firmware_path).pack(side="left", fill="x", expand=True)
        ttk.Button(row, text="Browse", command=self.choose_firmware).pack(side="left", padx=(6, 0))
        row = ttk.Frame(card, style="Card.TFrame")
        row.pack(fill="x", pady=5)
        ttk.Label(row, text="USB serial port", width=16, style="CardTitle.TLabel").pack(side="left")
        self.port_box = ttk.Combobox(row, textvariable=self.serial_port, width=38)
        self.port_box.pack(side="left")
        ttk.Button(row, text="Detect", command=self.detect_ports).pack(side="left", padx=6)
        row = ttk.Frame(card, style="Card.TFrame")
        row.pack(fill="x", pady=5)
        ttk.Label(row, text="Flash address", width=16, style="CardTitle.TLabel").pack(side="left")
        ttk.Entry(row, textvariable=self.flash_offset, width=14).pack(side="left")
        ttk.Label(row, text="Baud", style="CardTitle.TLabel").pack(side="left", padx=(18, 6))
        ttk.Entry(row, textvariable=self.flash_baud, width=12).pack(side="left")
        ttk.Button(row, text="Flash firmware…", style="Accent.TButton", command=self.flash_firmware).pack(side="right")
        ttk.Label(card, textvariable=self.flash_status, style="Muted.TLabel", wraplength=850).pack(anchor="w", pady=(8, 0))
        ttk.Label(
            card,
            text="Flash support writes a user-selected prebuilt image using bundled esptool. It does not compile MicroPython.",
            style="Muted.TLabel",
        ).pack(anchor="w", pady=(3, 0))
        console = ttk.LabelFrame(page, text="MicroPython serial test console", padding=10)
        console.pack(fill="both", expand=True, pady=(12, 0))
        ttk.Label(
            console,
            text="Connect after flashing to inspect boot output or use the board REPL. Commands are sent directly to the selected device.",
            style="Muted.TLabel",
        ).pack(anchor="w")
        self.serial_output = tk.Text(console, height=7, bg=COLORS["surface"], fg=COLORS["text"], insertbackground=COLORS["text"], relief="flat", wrap="word")
        self.serial_output.pack(fill="both", expand=True, pady=8)
        command_row = ttk.Frame(console)
        command_row.pack(fill="x")
        self.serial_command = ttk.Entry(command_row)
        self.serial_command.pack(side="left", fill="x", expand=True)
        self.serial_command.bind("<Return>", lambda _event: self.send_serial_command())
        ttk.Button(command_row, text="Send to REPL", command=self.send_serial_command).pack(side="left", padx=6)
        self.serial_button = ttk.Button(command_row, text="Connect console", command=self.toggle_serial_console)
        self.serial_button.pack(side="left")
        self.flash_log = tk.Text(page, height=9, bg=COLORS["surface"], fg=COLORS["muted"], insertbackground=COLORS["text"], relief="flat", wrap="word", state="disabled")
        self.flash_log.pack(fill="both", expand=True, pady=(8, 0))
        warning = ttk.Frame(page, style="Card.TFrame", padding=14)
        warning.pack(fill="x", pady=12)
        ttk.Label(warning, text="Before flashing", style="CardTitle.TLabel").pack(anchor="w")
        ttk.Label(
            warning,
            text="Close serial monitors first. Choose firmware built for the exact T-HMI ESP32-S3 revision and follow "
                 "its release's flash-offset instructions. The default 0x0 is only suitable for a merged image. "
                 "Flashing can erase the board. Use the board's BOOT/reset procedure if auto-download fails.",
            wraplength=850,
        ).pack(anchor="w", pady=(6, 0))

    @staticmethod
    def _tree(parent, keys, headings, widths):
        holder = ttk.Frame(parent)
        holder.pack(fill="both", expand=True)
        tree = ttk.Treeview(holder, columns=keys, show="headings")
        vertical = ttk.Scrollbar(holder, orient="vertical", command=tree.yview)
        horizontal = ttk.Scrollbar(holder, orient="horizontal", command=tree.xview)
        tree.configure(yscrollcommand=vertical.set, xscrollcommand=horizontal.set)
        for key, heading, width in zip(keys, headings, widths):
            tree.heading(key, text=heading)
            tree.column(key, width=width, minwidth=70, stretch=True)
        tree.grid(row=0, column=0, sticky="nsew")
        vertical.grid(row=0, column=1, sticky="ns")
        horizontal.grid(row=1, column=0, sticky="ew")
        holder.rowconfigure(0, weight=1)
        holder.columnconfigure(0, weight=1)
        return tree

    def show_page(self, name):
        self.active_page.set(name)
        self.page_title.configure(text=name)
        for child in self.page_host.winfo_children():
            child.pack_forget()
        self.pages[name].pack(fill="both", expand=True)
        if name == "Music":
            self.load_media_collection("audio")
        elif name == "Videos":
            self.load_media_collection("video")
        elif name == "Notebook":
            self._refresh_notes_tree()

    def open_browser_address(self):
        try:
            address = normalize_browser_url(self.browser_address.get())
        except ValueError as error:
            messagebox.showerror("Browser", str(error))
            return
        self.browser_address.set(address)
        if not webbrowser.open(address):
            messagebox.showerror("Browser", "Could not open the system browser.")

    def choose_recording_path(self):
        selected = filedialog.asksaveasfilename(
            defaultextension=".wav",
            initialfile="Recording.wav",
            filetypes=(("WAV audio", "*.wav"),),
        )
        if selected:
            self.recording_path.set(selected)

    def toggle_recording(self):
        if self.recorder and self.recorder.recording:
            try:
                self.recorder.stop()
                self.recording_status.set(f"Saved recording: {self.recorder.path}")
                self.record_button.configure(text="Start recording")
            except Exception as error:
                self.recording_status.set(f"Could not finish recording: {error}")
            finally:
                self.recorder = None
            return
        path = Path(self.recording_path.get()).expanduser()
        if path.suffix.lower() != ".wav":
            messagebox.showerror("Recorder", "Choose a .wav output file.")
            return
        self.recorder = WavRecorder(path)
        try:
            self.recorder.start()
        except (OSError, RuntimeError) as error:
            self.recorder = None
            self.recording_status.set(str(error))
            messagebox.showerror("Recorder", str(error))
            return
        self.recording_status.set(f"Recording to {path} — select Stop when done.")
        self.record_button.configure(text="Stop recording")

    def _load_notes_into_ui(self):
        try:
            self.notes = load_notes()
        except (OSError, ValueError) as error:
            messagebox.showerror("Notebook", str(error))
            self.notes = []
        self._refresh_notes_tree()

    def _refresh_notes_tree(self):
        self.notes_tree.delete(*self.notes_tree.get_children())
        for note in self.notes:
            self.notes_tree.insert("", "end", iid=note["id"], values=(note["title"] or "Untitled note",))

    def _active_note(self):
        return next((note for note in self.notes if note["id"] == self.active_note_id), None)

    def save_current_note(self):
        note = self._active_note()
        if not note:
            return
        title = self.note_title_var.get().strip()[:200] or "Untitled note"
        note["title"] = title
        note["body"] = self.note_body.get("1.0", "end-1c")
        from datetime import datetime, timezone

        note["updated"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        try:
            save_notes(self.notes)
        except (OSError, ValueError) as error:
            self.set_status("Notebook save failed: " + str(error))
            return
        if self.notes_tree.exists(note["id"]):
            self.notes_tree.item(note["id"], values=(note["title"],))
        self.set_status("Note saved on this computer.")

    def note_edited(self, _event=None):
        if self.note_save_after is not None:
            self.root.after_cancel(self.note_save_after)
        self.note_save_after = self.root.after(800, self.save_current_note)

    def note_body_modified(self, _event=None):
        if self.note_body.edit_modified():
            self.note_body.edit_modified(False)
            self.note_edited()

    def select_note(self, _event=None):
        selection = self.notes_tree.selection()
        if not selection:
            return
        selected_id = selection[0]
        if self.active_note_id == selected_id:
            return
        self.save_current_note()
        note = next((item for item in self.notes if item["id"] == selected_id), None)
        if not note:
            return
        self.active_note_id = note["id"]
        self.note_title_var.set(note["title"])
        self.note_body.delete("1.0", "end")
        self.note_body.insert("1.0", note["body"])
        self.note_body.edit_modified(False)

    def create_note(self):
        self.save_current_note()
        note = new_note()
        self.notes.insert(0, note)
        self.active_note_id = note["id"]
        try:
            save_notes(self.notes)
        except (OSError, ValueError) as error:
            self.notes.remove(note)
            self.active_note_id = None
            messagebox.showerror("Notebook", str(error))
            return
        self._refresh_notes_tree()
        self.notes_tree.selection_set(note["id"])
        self.note_title_var.set(note["title"])
        self.note_body.delete("1.0", "end")
        self.note_body.edit_modified(False)

    def delete_note(self):
        note = self._active_note()
        if not note:
            return
        if not messagebox.askyesno("Delete note", f"Delete {note['title']} from this computer?"):
            return
        self.notes.remove(note)
        self.active_note_id = None
        try:
            save_notes(self.notes)
        except (OSError, ValueError) as error:
            self.notes.append(note)
            messagebox.showerror("Notebook", str(error))
            return
        self._refresh_notes_tree()
        self.note_title_var.set("")
        self.note_body.delete("1.0", "end")

    def verify_robot_adapter(self):
        address = self.robot_address.get().strip()
        try:
            address = normalize_robot_url(address)
        except ValueError as error:
            messagebox.showerror("Robot", str(error))
            return
        token = self.robot_token.get().strip()
        if not token:
            messagebox.showerror("Robot", "Enter the adapter's bearer token before connecting.")
            return
        board = self.robot_board.get().strip()
        if not board:
            messagebox.showerror("Robot", "Enter the exact board profile identifier reported by the robot adapter.")
            return
        self.robot_address.set(address)
        self.robot_adapter_verified = False
        self.robot_status.set("Checking profile and stop watchdog…")

        def verified(result):
            self.robot_adapter_verified = True
            motors = "ENABLED" if result.get("motor_enabled") else "DISABLED (safe default)"
            ota = "enabled" if result.get("ota_enabled") else "disabled"
            self.robot_status.set(
                f"Verified {result.get('board', 'robot')} • firmware {result.get('version', 'unknown')} • "
                f"software stop bound {result['motor_watchdog_ms']} ms • motors {motors} • OTA {ota}."
            )
            self.settings.update({
                "robot_url": address,
                "robot_board": self.robot_board.get().strip(),
                "robot_token": token,
            })
            try:
                save_controller_settings(self.settings)
            except OSError as error:
                self.set_status("Robot settings save failed: " + str(error))

        self._async(lambda: check_adapter(address, token, board), verified)

    def _robot_async(self, action, success):
        self.robot_jobs.put((action, success))
        with self.robot_worker_lock:
            if self.robot_worker_active:
                return
            self.robot_worker_active = True

        def run_queue():
            while True:
                try:
                    current_action, on_success = self.robot_jobs.get_nowait()
                except queue.Empty:
                    with self.robot_worker_lock:
                        self.robot_worker_active = False
                        if self.robot_jobs.empty():
                            return
                        self.robot_worker_active = True
                    continue
                try:
                    result = current_action()
                except Exception as error:
                    if not self.closing:
                        self.root.after(0, lambda error=error: self.robot_status.set(str(error)))
                else:
                    if not self.closing:
                        self.root.after(0, lambda result=result, callback=on_success: callback(result))
                finally:
                    self.robot_jobs.task_done()

        threading.Thread(target=run_queue, daemon=True).start()

    def robot_action(self, command, duration_ms=200):
        address = self.robot_address.get().strip()
        if command != "stop" and not self.robot_adapter_verified:
            self.robot_status.set("Verify a compatible adapter before moving.")
            return
        if not address:
            self.robot_status.set("Enter the robot adapter address.")
            return
        token = self.robot_token.get().strip()
        if not token:
            self.robot_status.set("Enter the robot adapter token.")
            return
        if command == "stop":
            self.robot_cancel_event.set()
            while True:
                try:
                    self.robot_jobs.get_nowait()
                except queue.Empty:
                    break
                else:
                    self.robot_jobs.task_done()
            self._async(
                lambda: send_stop(address, token=token),
                lambda _result: self.robot_status.set("STOP acknowledged."),
            )
            return

        def move():
            try:
                return send_control(address, command, duration_ms, token=token)
            except Exception as error:
                try:
                    send_stop(address, token=token)
                except Exception as stop_error:
                    raise RuntimeError(f"{error}; emergency stop was not acknowledged: {stop_error}") from error
                raise RuntimeError(f"{error}; emergency stop was acknowledged.") from error

        self._robot_async(move, lambda _result: self.robot_status.set(f"{command.title()} pulse sent ({duration_ms} ms)."))

    def run_robot_program(self):
        if not self.robot_adapter_verified:
            self.robot_status.set("Verify a compatible adapter before running a program.")
            return
        try:
            steps = validate_program(json.loads(self.robot_program_text.get("1.0", "end-1c")))
        except (json.JSONDecodeError, ValueError) as error:
            messagebox.showerror("Robot program", str(error))
            return
        total = sum(step["duration_ms"] for step in steps)
        if not messagebox.askyesno(
            "Run robot program",
            f"Send {len(steps)} bounded movement pulse(s) totalling at most {total} ms? "
            "A STOP command will be sent afterward.",
            icon="warning",
        ):
            return
        self.robot_cancel_event.clear()
        address = self.robot_address.get().strip()
        token = self.robot_token.get().strip()

        def execute():
            try:
                for step in steps:
                    if self.robot_cancel_event.is_set():
                        break
                    send_control(
                        address,
                        step["command"],
                        step["duration_ms"],
                        token=token,
                    )
                    if self.robot_cancel_event.wait(step["duration_ms"] / 1000):
                        break
            finally:
                send_stop(address, token=token)
            return True

        self._robot_async(execute, lambda _result: self.robot_status.set("Program complete; STOP acknowledged."))

    def choose_robot_firmware(self):
        selected = filedialog.askopenfilename(
            filetypes=(("Robot firmware package", "*.zip"), ("ZIP archive", "*.zip"))
        )
        if selected:
            self.robot_firmware_path.set(selected)

    def update_robot_firmware(self):
        if not self.robot_adapter_verified:
            self.robot_status.set("Verify a compatible adapter before OTA.")
            return
        path = Path(self.robot_firmware_path.get()).expanduser()
        try:
            firmware = load_firmware_package(path, self.robot_board.get().strip())
        except (OSError, ValueError) as error:
            messagebox.showerror("Robot firmware", str(error))
            return
        if not messagebox.askyesno(
            "Confirm robot OTA update",
            f"Send version {firmware['version']} to board {firmware['board']}?\n"
            f"SHA-256: {firmware['sha256']}\n\n"
            "The robot must advertise the fixed OTA endpoint and motor-stop watchdog.",
            icon="warning",
        ):
            return
        self.robot_status.set("Uploading verified firmware; keep robot power connected…")
        address = self.robot_address.get().strip()
        token = self.robot_token.get().strip()

        def done(result):
            self.robot_status.set(f"OTA {result.get('status')} • {firmware['version']}.")

        self._robot_async(
            lambda: upload_firmware(
                address,
                firmware,
                token=token,
            ),
            done,
        )

    def persist_settings(self):
        self.settings.update({
            "server_url": self.server_url.get(),
            "admin_token": self.admin_token.get(),
            "robot_url": self.robot_address.get(),
            "robot_board": self.robot_board.get(),
            "robot_token": self.robot_token.get(),
        })
        try:
            save_controller_settings(self.settings)
            self.set_status("Connection settings saved on this computer.")
        except OSError as error:
            messagebox.showerror("Settings", f"Could not save settings: {error}")

    def set_status(self, text):
        self.connection_text.set(text)

    def _api(self, path, body=None, method=None):
        return request_json(normalize_server_url(self.server_url.get()), path, body, token=self.admin_token.get().strip(), method=method)

    def _async(self, action, success):
        def worker():
            try:
                result = action()
                self.root.after(0, lambda: success(result))
            except Exception as error:
                self.root.after(0, lambda error=error: self.set_status(str(error)))
        threading.Thread(target=worker, daemon=True).start()

    def connect(self):
        self.persist_settings()
        self.set_status("Connecting to Pi…")
        def complete(result):
            self.server_card.configure(text="ONLINE")
            self.apps_card.configure(text=str(result.get("apps", "—")))
            self.set_status(f"Connected • {result.get('name', 'Pi Media Hub')}")
            self.load_library()
            self.load_media_collection("audio")
            self.load_media_collection("video")
            self.load_apps()
            self._load_ai_settings()
        self._async(lambda: request_json(normalize_server_url(self.server_url.get()), "/api/status"), complete)

    def load_library(self):
        from urllib.parse import urlencode
        query = urlencode({"kind": self.kind.get(), "q": self.search.get().strip()})
        self._async(
            lambda: request_json(normalize_server_url(self.server_url.get()), "/api/library?" + query),
            self._show_library,
        )

    def load_media_collection(self, kind):
        from urllib.parse import urlencode

        query = urlencode({"kind": kind, "q": ""})
        self._async(
            lambda: request_json(normalize_server_url(self.server_url.get()), "/api/library?" + query),
            lambda result, k=kind: self._show_media_collection(k, result),
        )

    def _show_media_collection(self, kind, result):
        view = self.media_views[kind]
        view["items"] = result.get("items", [])
        tree = view["tree"]
        tree.delete(*tree.get_children())
        for index, item in enumerate(view["items"]):
            tree.insert(
                "",
                "end",
                iid=str(index),
                values=(
                    item.get("name"),
                    f"{item.get('size', 0) / 1_048_576:.1f} MB",
                    str(Path(item.get("path", "")).parent),
                ),
            )
        view["empty"].configure(text=f"{len(view['items'])} {kind} items on the Pi.")

    def open_media_collection_item(self, kind):
        view = self.media_views[kind]
        selection = view["tree"].selection()
        if not selection:
            return
        item = view["items"][int(selection[0])]
        webbrowser.open(normalize_server_url(self.server_url.get()) + item["url"])

    def _show_library(self, result):
        self.media_items = result.get("items", [])
        self.library_tree.delete(*self.library_tree.get_children())
        for index, item in enumerate(self.media_items):
            size_mb = f"{item.get('size', 0) / 1_048_576:.1f} MB"
            folder = str(Path(item.get("path", "")).parent)
            self.library_tree.insert("", "end", iid=str(index), values=(item.get("name"), item.get("kind"), size_mb, folder))
        self.media_card.configure(text=str(len(self.media_items)))
        self.media_empty.configure(text=f"{len(self.media_items)} items • double-click a row to open/play in your browser")

    def open_selected_media(self):
        selection = self.library_tree.selection()
        if not selection:
            return
        item = self.media_items[int(selection[0])]
        webbrowser.open(normalize_server_url(self.server_url.get()) + item["url"])

    def load_apps(self):
        self._async(lambda: self._api("/api/apps"), self._show_apps)

    def _load_ai_settings(self):
        def receive(result):
            self.ai_enabled.set(result.get("enabled", False))
            self.ai_endpoint.set(result.get("endpoint", ""))
            self.ai_model.set(result.get("model", ""))
            self.ai_backend.set(result.get("backend", "Custom"))
        self._async(lambda: self._api("/api/config/ai"), receive)

    def _show_apps(self, result):
        self.apps = result.get("apps", [])
        self.app_tree.delete(*self.app_tree.get_children())
        for index, app in enumerate(self.apps):
            self.app_tree.insert("", "end", iid=str(index), values=(app.get("name"), app.get("id"), app.get("description"), app.get("url")))
        self.apps_card.configure(text=str(len(self.apps)))
        self.apps_empty.configure(text=f"{len(self.apps)} shortcuts • launch opens the selected web app")

    def create_app(self):
        self._app_form()

    def _app_form(self, values=None):
        dialog = tk.Toplevel(self.root)
        dialog.title("Add app shortcut")
        dialog.configure(bg=COLORS["background"])
        dialog.transient(self.root)
        dialog.grab_set()
        variables = {}
        for label, default in (("id", ""), ("name", ""), ("description", ""), ("url", "")):
            row = ttk.Frame(dialog, padding=(12, 5))
            row.pack(fill="x")
            ttk.Label(row, text=label.title(), width=14).pack(side="left")
            variable = tk.StringVar(value=(values or {}).get(label, default))
            ttk.Entry(row, textvariable=variable, width=52).pack(side="left")
            variables[label] = variable
        def save():
            payload = {key: var.get().strip() for key, var in variables.items()}
            if not APP_ID.fullmatch(payload["id"]):
                messagebox.showerror("App ID", "Use lowercase letters, digits and hyphens.", parent=dialog)
                return
            if not payload["name"] or urlsplit(payload["url"]).scheme not in {"http", "https"}:
                messagebox.showerror("App details", "A name and http(s) launch URL are required.", parent=dialog)
                return
            dialog.destroy()
            self._async(lambda: self._api("/api/apps", payload), lambda _result: self.load_apps())
        ttk.Button(dialog, text="Save shortcut", command=save, style="Accent.TButton").pack(anchor="e", padx=12, pady=12)

    def import_manifest(self):
        selected = filedialog.askopenfilename(filetypes=(("Pi Media Hub app manifest", "*.json"), ("JSON files", "*.json")))
        if not selected:
            return
        try:
            manifest = json.loads(Path(selected).read_text(encoding="utf-8"))
            if not isinstance(manifest, dict):
                raise ValueError("Expected a JSON object")
        except (OSError, json.JSONDecodeError, ValueError) as error:
            messagebox.showerror("Import app", str(error))
            return
        self._async(lambda: self._api("/api/apps", manifest), lambda _result: self.load_apps())

    def upload_app_archive(self):
        selected = filedialog.askopenfilename(filetypes=(("Pi Media Hub web app", "*.zip"), ("ZIP archive", "*.zip")))
        if not selected:
            return
        try:
            content = Path(selected).read_bytes()
        except OSError as error:
            messagebox.showerror("Upload app", str(error))
            return
        if len(content) > 24 * 1024 * 1024:
            messagebox.showerror("Upload app", "ZIP archive exceeds the 24 MiB limit.")
            return
        self._async(lambda: self._upload_app_bytes(content), lambda _result: self.load_apps())

    def _upload_app_bytes(self, content):
        base = normalize_server_url(self.server_url.get())
        request = Request(
            base + "/api/apps/upload",
            data=content,
            headers={
                "Content-Type": "application/zip",
                "Accept": "application/json",
                "Authorization": "Bearer " + self.admin_token.get().strip(),
            },
            method="POST",
        )
        try:
            with urlopen(request, timeout=30) as response:
                return json.loads(response.read(4096))
        except HTTPError as error:
            raise RuntimeError("Pi returned HTTP {}: {}".format(error.code, error.read(4096).decode(errors="replace"))) from error
        except (URLError, OSError, json.JSONDecodeError) as error:
            raise RuntimeError("Could not upload web app: {}".format(error)) from error

    def import_github(self):
        source = simpledialog.askstring("Import GitHub app", "Repository as owner/name (must contain pi-media-hub-app.json):", parent=self.root)
        if not source:
            return
        parts = source.strip().removeprefix("https://github.com/").rstrip("/").removesuffix(".git").split("/")
        if len(parts) != 2:
            messagebox.showerror("GitHub import", "Enter owner/name.")
            return
        payload = {"owner": parts[0], "repo": parts[1], "branch": "main"}
        self._async(lambda: self._api("/api/apps/import/github", payload), lambda _result: self.load_apps())

    def selected_app(self):
        selection = self.app_tree.selection()
        return self.apps[int(selection[0])] if selection else None

    def remove_app(self):
        app = self.selected_app()
        if not app:
            return
        if not messagebox.askyesno("Remove shortcut", f"Remove {app['name']} from this Pi's app shelf?"):
            return
        self._async(
            lambda: self._api("/api/apps/" + quote(app["id"], safe=""), method="DELETE"),
            lambda _result: self.load_apps(),
        )

    def launch_app(self):
        app = self.selected_app()
        if app:
            address = app["url"]
            if address.startswith("/"):
                address = normalize_server_url(self.server_url.get()) + address
            webbrowser.open(address)

    def save_ai_settings(self):
        endpoint = normalize_ai_endpoint(self.ai_backend.get(), self.ai_endpoint.get())
        model = self.ai_model.get().strip()
        backend = self.ai_backend.get()
        self.ai_endpoint.set(endpoint)
        payload = {"enabled": self.ai_enabled.get(), "endpoint": endpoint, "model": model, "backend": backend}
        self._async(lambda: self._api("/api/config/ai", payload), lambda _result: self.set_status("AI backend settings saved on Pi."))

    def send_chat(self):
        message = self.chat_input.get().strip()
        if not message:
            return
        model, backend = self.ai_model.get().strip(), self.ai_backend.get()
        endpoint = normalize_ai_endpoint(backend, self.ai_endpoint.get())
        if backend == "OpenAI compatible":
            payload = {"model": model, "messages": [{"role": "user", "content": message}]}
        elif backend == "Ollama":
            payload = {"model": model, "messages": [{"role": "user", "content": message}], "stream": False}
        else:
            payload = {"message": message, "model": model}
        self.chat_history.insert("end", "YOU\n" + message + "\n\n")
        self.chat_input.delete(0, "end")
        def done(result):
            response = result.get("message", {}).get("content") or result.get("choices", [{}])[0].get("message", {}).get("content") or result.get("response") or json.dumps(result, ensure_ascii=False)
            self.chat_history.insert("end", "PI AI\n" + str(response) + "\n\n")
            self.chat_history.see("end")
        self._async(lambda: self._api("/api/chat", payload), done)

    def choose_media(self):
        selected = filedialog.askdirectory()
        if selected:
            self.local_media.set(selected)

    def toggle_local_server(self):
        if self.local_server:
            self.local_server.shutdown()
            self.local_server.server_close()
            self.local_server = None
            self.local_button.configure(text="Start temporary server")
            return
        from .server import MediaHubServer
        media = Path(self.local_media.get()).expanduser().resolve()
        if not media.is_dir():
            messagebox.showerror("Media folder", "Select an existing folder.")
            return
        user_config = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share")) / "pi-media-hub"
        user_config.mkdir(parents=True, exist_ok=True)
        config_path = user_config / "host-config.json"
        config = {
            "media_root": media,
            "apps_dir": user_config / "apps",
            "catalog": [],
            "admin_token": self.admin_token.get().strip(),
            "ai": {"enabled": False, "endpoint": "", "timeout_seconds": 30},
            "_config_path": config_path,
        }
        if not config["admin_token"]:
            config["admin_token"] = secrets.token_urlsafe(32)
        try:
            self.local_server = MediaHubServer(("0.0.0.0", 8765), config)
        except OSError as error:
            messagebox.showerror("Server start failed", str(error))
            return
        threading.Thread(target=self.local_server.serve_forever, daemon=True).start()
        self.local_button.configure(text="Stop temporary server")
        self.server_url.set("http://127.0.0.1:8765")
        self.admin_token.set(config["admin_token"])
        self.persist_settings()
        self.connect()

    def detect_ports(self):
        try:
            from serial.tools import list_ports
        except ImportError:
            self.flash_status.set("Serial support is missing from this build.")
            return
        devices = [port.device for port in list_ports.comports()]
        self.detected_ports = set(devices)
        self.port_box.configure(values=devices)
        if devices:
            self.serial_port.set(devices[0])
            self.flash_status.set("Found {} serial device(s).".format(len(devices)))
        else:
            self.flash_status.set("No serial ports detected. Connect the board and check Linux device permissions.")

    def choose_firmware(self):
        selected = filedialog.askopenfilename(filetypes=(("ESP32 firmware image", "*.bin"), ("All files", "*")))
        if selected:
            self.firmware_path.set(selected)

    def flash_firmware(self):
        if self.serial_port.get().strip() not in self.detected_ports:
            messagebox.showerror("Firmware selection", "Detect ports and select a currently connected USB serial device.")
            return
        try:
            args = build_flash_args(
                self.serial_port.get().strip(),
                self.firmware_path.get().strip(),
                self.flash_offset.get().strip(),
                self.flash_baud.get().strip(),
            )
        except ValueError as error:
            messagebox.showerror("Firmware selection", str(error))
            return
        if not messagebox.askyesno(
            "Confirm firmware flash",
            "This will write firmware to the selected ESP32-S3 and may erase existing contents.\n\n"
            f"Port: {self.serial_port.get()}\nImage: {self.firmware_path.get()}\nOffset: {args[-2]}\n\n"
            "Only continue if this image is for your exact T-HMI revision.",
            icon="warning",
        ):
            return
        self.flash_status.set("Flashing… do not unplug the board.")
        def flash():
            output = io.StringIO()
            try:
                import esptool
                with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
                    esptool.main(args)
                result, error = output.getvalue()[-5000:] or "Flash completed.", None
            except SystemExit as failure:
                result = output.getvalue()[-4000:]
                error = None if failure.code in (None, 0) else "esptool exited with status {}".format(failure.code)
            except Exception as failure:
                result, error = output.getvalue()[-4000:], str(failure)
            def finish():
                self.flash_status.set("Flash complete." if error is None else "Flash failed: " + error)
                self.flash_log.configure(state="normal")
                self.flash_log.delete("1.0", "end")
                self.flash_log.insert("1.0", result)
                self.flash_log.configure(state="disabled")
            self.root.after(0, finish)
        threading.Thread(target=flash, daemon=True).start()

    def toggle_serial_console(self):
        if self.serial_connection is not None:
            try:
                self.serial_connection.close()
            finally:
                self.serial_connection = None
                self.serial_button.configure(text="Connect console")
            return
        if self.serial_port.get().strip() not in self.detected_ports:
            messagebox.showerror("Serial console", "Detect ports and select a currently connected USB serial device.")
            return
        try:
            import serial
            self.serial_connection = serial.Serial(self.serial_port.get().strip(), int(self.flash_baud.get()), timeout=0.25)
        except (ImportError, OSError, ValueError) as error:
            messagebox.showerror("Serial console", str(error))
            self.serial_connection = None
            return
        self.serial_button.configure(text="Disconnect console")
        connection = self.serial_connection
        def read_output():
            while connection is self.serial_connection and connection.is_open:
                try:
                    data = connection.read(512)
                except (OSError, ValueError) as error:
                    self.root.after(0, lambda error=error: self.flash_status.set("Serial read failed: " + str(error)))
                    break
                if data:
                    text = data.decode("utf-8", errors="replace")
                    self.root.after(0, lambda text=text: self._append_serial_output(text))
        self.serial_reader = threading.Thread(target=read_output, daemon=True)
        self.serial_reader.start()

    def send_serial_command(self):
        if self.serial_connection is None or not self.serial_connection.is_open:
            messagebox.showinfo("Serial console", "Connect to the board first.")
            return
        command = self.serial_command.get()
        try:
            self.serial_connection.write((command + "\r\n").encode("utf-8"))
            self._append_serial_output("\n>>> " + command + "\n")
            self.serial_command.delete(0, "end")
        except (OSError, ValueError) as error:
            self.flash_status.set("Serial write failed: " + str(error))

    def _append_serial_output(self, text):
        self.serial_output.insert("end", text)
        self.serial_output.see("end")

    def close(self):
        self.closing = True
        self.robot_cancel_event.set()
        while True:
            try:
                self.robot_jobs.get_nowait()
            except queue.Empty:
                break
            else:
                self.robot_jobs.task_done()
        if self.recorder and self.recorder.recording:
            try:
                self.recorder.stop()
            except Exception as error:
                messagebox.showwarning("Recorder", f"Could not close recording cleanly: {error}")
        if self.robot_adapter_verified:
            try:
                send_stop(self.robot_address.get().strip(), token=self.robot_token.get().strip())
            except Exception as error:
                messagebox.showwarning("Robot stop", f"Could not confirm STOP before exit: {error}")
        if self.serial_connection is not None:
            self.serial_connection.close()
        if self.local_server is not None:
            self.local_server.shutdown()
            self.local_server.server_close()
        self.root.destroy()


def main():
    root = tk.Tk()
    MediaHubApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
