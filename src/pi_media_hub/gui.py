from __future__ import annotations

import json
import platform
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen


DEFAULT_SERVER = "http://raspberrypi.local:8765"
REQUEST_TIMEOUT = 8


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


def request_json(base_url: str, path: str, body: object | None = None) -> object:
    data = None if body is None else json.dumps(body).encode("utf-8")
    request = Request(
        base_url + path,
        data=data,
        headers={"Accept": "application/json", **({"Content-Type": "application/json"} if data is not None else {})},
        method="GET" if data is None else "POST",
    )
    try:
        with urlopen(request, timeout=REQUEST_TIMEOUT) as response:
            return json.loads(response.read(1024 * 1024 + 1))
    except HTTPError as error:
        detail = error.read(4096).decode("utf-8", errors="replace")
        raise RuntimeError(f"Server returned HTTP {error.code}: {detail}") from error
    except (URLError, TimeoutError, OSError, json.JSONDecodeError) as error:
        raise RuntimeError(f"Could not contact Pi server: {error}") from error


class MediaHubApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("Pi Media Hub")
        self.root.geometry("900x640")
        self.server_url = tk.StringVar(value=DEFAULT_SERVER)
        self.status_text = tk.StringVar(value="Not connected")
        self.kind = tk.StringVar(value="all")
        self.local_server = None
        self.local_thread = None
        self.local_media = None
        self._build()
        self.root.protocol("WM_DELETE_WINDOW", self.close)

    def _build(self):
        outer = ttk.Frame(self.root, padding=12)
        outer.pack(fill="both", expand=True)
        ttk.Label(outer, text="Pi Media Hub", font=("TkDefaultFont", 18, "bold")).pack(anchor="w")
        connect = ttk.Frame(outer)
        connect.pack(fill="x", pady=(12, 8))
        ttk.Label(connect, text="Raspberry Pi address").pack(side="left")
        address = ttk.Entry(connect, textvariable=self.server_url, width=44)
        address.pack(side="left", padx=8, fill="x", expand=True)
        address.bind("<Return>", lambda _event: self.connect())
        ttk.Button(connect, text="Connect", command=self.connect).pack(side="left")
        ttk.Label(outer, textvariable=self.status_text).pack(anchor="w", pady=(0, 8))

        tabs = ttk.Notebook(outer)
        tabs.pack(fill="both", expand=True)
        library = ttk.Frame(tabs, padding=10)
        apps = ttk.Frame(tabs, padding=10)
        ai = ttk.Frame(tabs, padding=10)
        tabs.add(library, text="Library")
        tabs.add(apps, text="App catalog")
        tabs.add(ai, text="Local AI")

        controls = ttk.Frame(library)
        controls.pack(fill="x", pady=(0, 8))
        ttk.Label(controls, text="Show").pack(side="left")
        filter_box = ttk.Combobox(
            controls, textvariable=self.kind, state="readonly",
            values=("all", "audio", "video", "podcast"), width=12,
        )
        filter_box.pack(side="left", padx=6)
        ttk.Button(controls, text="Refresh", command=self.load_library).pack(side="left")
        ttk.Button(controls, text="Open selected", command=self.open_selected).pack(side="left", padx=6)
        self.library_list = self._tree(library, ("name", "kind", "size"), ("Name", "Type", "Bytes"))

        ttk.Button(apps, text="Refresh catalog", command=self.load_apps).pack(anchor="w", pady=(0, 8))
        self.app_list = self._tree(apps, ("name", "description", "url"), ("Name", "Description", "URL"))
        ttk.Button(apps, text="Open selected app", command=self.open_app).pack(anchor="w", pady=8)

        ttk.Label(
            ai,
            text="AI is configured on the Pi. Submit JSON in the backend's expected format; "
                 "the hub forwards it to the configured HTTP endpoint.",
            wraplength=800,
        ).pack(anchor="w")
        self.ai_input = tk.Text(ai, height=9, wrap="word")
        self.ai_input.insert("1.0", '{\n  "message": "Hello"\n}')
        self.ai_input.pack(fill="x", pady=8)
        ttk.Button(ai, text="Send JSON to Pi AI", command=self.send_ai).pack(anchor="w")
        self.ai_output = tk.Text(ai, height=12, wrap="word", state="disabled")
        self.ai_output.pack(fill="both", expand=True, pady=(8, 0))

        if platform.machine().lower() in {"aarch64", "arm64"}:
            self._build_local_host(outer)
        ttk.Label(
            outer,
            text="The laptop app is a controller, not the media server. The Pi server must be running "
                 "and reachable on the same LAN. No SSH setup or firmware flashing is performed.",
            wraplength=850,
        ).pack(anchor="w", pady=(8, 0))

    @staticmethod
    def _tree(parent, keys, headings):
        tree = ttk.Treeview(parent, columns=keys, show="headings")
        for key, heading in zip(keys, headings):
            tree.heading(key, text=heading)
            tree.column(key, width=160 if key == "name" else 110, stretch=True)
        tree.pack(fill="both", expand=True)
        return tree

    def _build_local_host(self, parent):
        box = ttk.LabelFrame(parent, text="Host media from this Raspberry Pi", padding=8)
        box.pack(fill="x", pady=(8, 0))
        self.media_dir = tk.StringVar(value="/srv/media")
        ttk.Entry(box, textvariable=self.media_dir).pack(side="left", fill="x", expand=True)
        ttk.Button(box, text="Choose folder", command=self.choose_media).pack(side="left", padx=6)
        self.host_button = ttk.Button(box, text="Start local server", command=self.toggle_local_server)
        self.host_button.pack(side="left")
        ttk.Label(
            box,
            text="Runs only while this window is open. Use the documented installer for a persistent system service.",
            wraplength=850,
        ).pack(anchor="w", pady=(6, 0))

    def choose_media(self):
        selected = filedialog.askdirectory(initialdir=self.media_dir.get())
        if selected:
            self.media_dir.set(selected)

    def toggle_local_server(self):
        if self.local_server is not None:
            self.local_server.shutdown()
            self.local_server.server_close()
            self.local_server = None
            self.local_thread = None
            self.local_media = None
            self.host_button.configure(text="Start local server")
            self.status_text.set("Local server stopped")
            return
        from pathlib import Path
        from .server import MediaHubServer

        media = Path(self.media_dir.get()).expanduser().resolve()
        if not media.is_dir():
            messagebox.showerror("Media folder", "Choose an existing media directory.")
            return
        try:
            server = MediaHubServer(("0.0.0.0", 8765), {
                "media_root": media,
                "catalog": [],
                "ai": {"enabled": False},
            })
        except OSError as error:
            messagebox.showerror("Could not start server", str(error))
            return
        self.local_server = server
        self.local_media = media
        self.local_thread = threading.Thread(target=server.serve_forever, daemon=True)
        self.local_thread.start()
        self.host_button.configure(text="Stop local server")
        self.server_url.set("http://127.0.0.1:8765")
        self.connect()

    def connect(self):
        try:
            base = normalize_server_url(self.server_url.get())
        except ValueError as error:
            messagebox.showerror("Server address", str(error))
            return
        self.server_url.set(base)
        self.status_text.set("Connecting…")

        def work():
            try:
                status = request_json(base, "/api/status")
                message = "Connected to {} (AI {})".format(
                    status.get("name", "Pi Media Hub"),
                    "enabled" if status.get("ai_enabled") else "disabled",
                )
                self.root.after(0, lambda: self.status_text.set(message))
                self.root.after(0, self.load_library)
                self.root.after(0, self.load_apps)
            except Exception as error:
                self.root.after(0, lambda error=error: self.status_text.set(str(error)))

        threading.Thread(target=work, daemon=True).start()

    def _base(self):
        return normalize_server_url(self.server_url.get())

    def _load(self, path, callback):
        try:
            base = self._base()
        except ValueError as error:
            self.status_text.set(str(error))
            return

        def work():
            try:
                result = request_json(base, path)
                self.root.after(0, lambda: callback(result))
            except Exception as error:
                self.root.after(0, lambda error=error: self.status_text.set(str(error)))

        threading.Thread(target=work, daemon=True).start()

    def load_library(self):
        self._load("/api/library?kind=" + self.kind.get(), self._show_library)

    def _show_library(self, result):
        self.library_list.delete(*self.library_list.get_children())
        for item in result.get("items", []):
            self.library_list.insert("", "end", values=(item.get("name"), item.get("kind"), item.get("size")), tags=(item.get("url", ""),))
        self.status_text.set("Connected; {} media files listed".format(len(result.get("items", []))))

    def open_selected(self):
        import webbrowser

        selected = self.library_list.selection()
        if not selected:
            return
        relative = self.library_list.item(selected[0], "tags")[0]
        if relative:
            webbrowser.open(self._base() + relative)

    def load_apps(self):
        self._load("/api/apps", self._show_apps)

    def _show_apps(self, result):
        self.app_list.delete(*self.app_list.get_children())
        for app in result.get("apps", []):
            self.app_list.insert(
                "", "end",
                values=(app.get("name", app.get("id", "")), app.get("description", ""), app.get("url", "")),
                tags=(app.get("url", ""),),
            )

    def open_app(self):
        import webbrowser

        selected = self.app_list.selection()
        if not selected:
            return
        address = self.app_list.item(selected[0], "tags")[0]
        if address:
            from urllib.parse import urljoin

            webbrowser.open(urljoin(self._base() + "/", address))

    def send_ai(self):
        try:
            body = json.loads(self.ai_input.get("1.0", "end").strip())
            base = self._base()
        except (json.JSONDecodeError, ValueError) as error:
            messagebox.showerror("AI request", str(error))
            return
        if not isinstance(body, dict):
            messagebox.showerror("AI request", "JSON request must be an object.")
            return
        self.status_text.set("Sending request to Pi AI…")

        def work():
            try:
                result = request_json(base, "/api/chat", body)
                output = json.dumps(result, indent=2, ensure_ascii=False)
                self.root.after(0, lambda: self._set_ai_output(output))
                self.root.after(0, lambda: self.status_text.set("AI response received"))
            except Exception as error:
                self.root.after(0, lambda error=error: self.status_text.set(str(error)))

        threading.Thread(target=work, daemon=True).start()

    def _set_ai_output(self, value):
        self.ai_output.configure(state="normal")
        self.ai_output.delete("1.0", "end")
        self.ai_output.insert("1.0", value)
        self.ai_output.configure(state="disabled")

    def close(self):
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
