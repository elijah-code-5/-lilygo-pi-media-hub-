from __future__ import annotations

import argparse
import ipaddress
import io
import json
import mimetypes
import os
import re
import secrets
import shutil
import stat
import sys
import tempfile
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path, PurePosixPath
from tempfile import NamedTemporaryFile
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, quote, unquote, urlsplit
from urllib.request import Request, urlopen


MEDIA_TYPES = {
    "audio": {".aac", ".aiff", ".alac", ".flac", ".m4a", ".mp3", ".ogg", ".opus", ".wav", ".wma"},
    "video": {".avi", ".m4v", ".mkv", ".mov", ".mp4", ".mpeg", ".mpg", ".webm"},
}
MAX_LIBRARY_ITEMS = 5000
MAX_REQUEST_BYTES = 64 * 1024
MAX_APP_ARCHIVE_BYTES = 24 * 1024 * 1024
MAX_APP_UNPACKED_BYTES = 32 * 1024 * 1024
MAX_APP_FILES = 250
APP_ASSET_EXTENSIONS = {
    ".css", ".gif", ".htm", ".html", ".ico", ".jpeg", ".jpg", ".js", ".json",
    ".md", ".mp3", ".mp4", ".ogg", ".png", ".svg", ".txt", ".wav", ".webm",
    ".webp", ".woff", ".woff2",
}
APP_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,47}$")
GITHUB_PART = re.compile(r"^[A-Za-z0-9_.-]{1,100}$")


def load_config(path: Path) -> dict:
    config = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(config, dict):
        raise ValueError("Configuration must be a JSON object")
    host = config.get("host", "0.0.0.0")
    port = config.get("port", 8765)
    media_root = config.get("media_root")
    if not isinstance(host, str) or not host:
        raise ValueError("host must be a non-empty string")
    if not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65535:
        raise ValueError("port must be an integer from 1 to 65535")
    if not isinstance(media_root, str) or not media_root:
        raise ValueError("media_root must be a non-empty path")
    root = Path(media_root).expanduser().resolve()
    if not root.is_dir():
        raise ValueError(f"media_root is not an existing directory: {root}")

    catalog = config.get("catalog", [])
    ai = config.get("ai", {})
    if not isinstance(catalog, list) or not all(isinstance(item, dict) for item in catalog):
        raise ValueError("catalog must be a list of objects")
    if not isinstance(ai, dict):
        raise ValueError("ai must be an object")
    timeout = ai.get("timeout_seconds", 30)
    if not isinstance(timeout, (int, float)) or isinstance(timeout, bool) or not 1 <= timeout <= 120:
        raise ValueError("ai.timeout_seconds must be from 1 to 120")
    endpoint = ai.get("endpoint", "")
    if not isinstance(endpoint, str):
        raise ValueError("ai.endpoint must be a string")
    if ai.get("enabled", False) and urlsplit(endpoint).scheme not in {"http", "https"}:
        raise ValueError("enabled AI requires an http or https endpoint")
    admin_token = config.get("admin_token", "")
    if not isinstance(admin_token, str):
        raise ValueError("admin_token must be a string")
    return {
        **config,
        "host": host,
        "port": port,
        "media_root": root,
        "catalog": catalog,
        "admin_token": admin_token,
        "apps_dir": Path(config.get("apps_dir", "/var/lib/pi-media-hub/apps")).expanduser().resolve(),
        "_config_path": path.expanduser().resolve(),
        "ai": {**ai, "timeout_seconds": timeout},
    }


def validate_app_manifest(payload: object, *, require_url: bool = True) -> dict:
    if not isinstance(payload, dict):
        raise ValueError("App manifest must be a JSON object")
    app_id = payload.get("id")
    name = payload.get("name")
    description = payload.get("description", "")
    url = payload.get("url")
    if not isinstance(app_id, str) or not APP_ID.fullmatch(app_id):
        raise ValueError("App id must use lowercase letters, digits, and hyphens (1-48 characters)")
    if not isinstance(name, str) or not name.strip() or len(name) > 100:
        raise ValueError("App name must contain 1-100 characters")
    if not isinstance(description, str) or len(description) > 500:
        raise ValueError("App description must be at most 500 characters")
    if url is None and not require_url:
        url = ""
    if not isinstance(url, str) or len(url) > 2048:
        raise ValueError("App URL must be an http(s) URL")
    if url:
        parsed = urlsplit(url)
        if parsed.scheme not in {"https", "http"} or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError("App URL must be an http(s) URL without embedded credentials")
    return {"id": app_id, "name": name.strip(), "description": description.strip(), "url": url}


def install_app_archive(archive_data: bytes, apps_dir: Path, *, github_owner_repo: str = "") -> dict:
    if len(archive_data) > MAX_APP_ARCHIVE_BYTES:
        raise ValueError(f"App archive exceeds {MAX_APP_ARCHIVE_BYTES // (1024 * 1024)} MiB")
    try:
        archive = zipfile.ZipFile(io.BytesIO(archive_data))
    except (zipfile.BadZipFile, OSError) as error:
        raise ValueError("Selected file is not a valid ZIP archive") from error
    with archive:
        entries = [info for info in archive.infolist() if not info.is_dir()]
        if len(entries) > MAX_APP_FILES:
            raise ValueError(f"App archive contains more than {MAX_APP_FILES} files")
        names = []
        seen_names = set()
        for info in entries:
            name = info.filename
            path = PurePosixPath(name)
            if (
                "\\" in name or "\x00" in name or path.is_absolute()
                or any(part in {"", ".", ".."} for part in name.split("/"))
                or path.as_posix() in seen_names
            ):
                raise ValueError("App archive contains an unsafe path")
            seen_names.add(path.as_posix())
            if info.flag_bits & 0x1:
                raise ValueError("Encrypted app ZIP entries are not supported")
            mode = info.external_attr >> 16
            if stat.S_ISLNK(mode):
                raise ValueError("App archive may not contain symbolic links")
            if info.file_size > MAX_APP_UNPACKED_BYTES:
                raise ValueError("App archive contains an oversized file")
            names.append(path)
        total = sum(info.file_size for info in entries)
        if total > MAX_APP_UNPACKED_BYTES:
            raise ValueError(f"Unpacked app exceeds {MAX_APP_UNPACKED_BYTES // (1024 * 1024)} MiB")

        manifests = [path for path in names if path.name == "pi-media-hub-app.json"]
        if len(manifests) != 1:
            raise ValueError("App ZIP must contain one pi-media-hub-app.json manifest")
        manifest_path = manifests[0]
        prefix = manifest_path.parent
        manifest_info = next(info for info in entries if PurePosixPath(info.filename) == manifest_path)
        if manifest_info.file_size > 16 * 1024:
            raise ValueError("App manifest exceeds 16 KiB")
        try:
            manifest_data = archive.read(manifest_info)
            manifest = validate_app_manifest(json.loads(manifest_data), require_url=False)
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError, RuntimeError, zipfile.BadZipFile, NotImplementedError) as error:
            raise ValueError(f"Invalid app manifest: {error}") from error

        selected = []
        for info, path in zip(entries, names):
            try:
                relative = path.relative_to(prefix)
            except ValueError:
                continue
            if not relative.parts:
                continue
            if relative.suffix.lower() not in APP_ASSET_EXTENSIONS:
                continue
            selected.append((info, relative))
        if not any(path.as_posix() == "index.html" for _, path in selected):
            raise ValueError("App ZIP must include index.html beside its manifest")

        apps_dir.mkdir(parents=True, exist_ok=True)
        target = apps_dir / manifest["id"]
        if target.is_symlink():
            raise ValueError("Refusing to replace an app directory symlink")
        staging = Path(tempfile.mkdtemp(prefix=".app-install-", dir=apps_dir))
        try:
            for info, relative in selected:
                destination = staging.joinpath(*relative.parts)
                destination.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(info) as source, destination.open("wb") as output:
                    shutil.copyfileobj(source, output, length=64 * 1024)
            if target.exists():
                shutil.rmtree(target)
            os.replace(staging, target)
        except (OSError, zipfile.BadZipFile, RuntimeError, NotImplementedError) as error:
            shutil.rmtree(staging, ignore_errors=True)
            raise ValueError(f"Could not install app files: {error}") from error
    manifest["url"] = f"/apps/{manifest['id']}/index.html"
    manifest["version"] = "local"
    manifest["source"] = github_owner_repo or "uploaded ZIP"
    return manifest


def save_config(config: dict) -> None:
    path: Path = config["_config_path"]
    serializable = {key: value for key, value in config.items() if not key.startswith("_")}
    serializable["media_root"] = str(config["media_root"])
    serializable["apps_dir"] = str(config["apps_dir"])
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_name = None
    try:
        with NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as temporary:
            temporary_name = temporary.name
            json.dump(serializable, temporary, indent=2)
            temporary.write("\n")
            temporary.flush()
            os.fsync(temporary.fileno())
        os.chmod(temporary_name, 0o640)
        os.replace(temporary_name, path)
    except OSError:
        if temporary_name:
            Path(temporary_name).unlink(missing_ok=True)
        raise


class MediaHubServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address: tuple[str, int], config: dict):
        self.config = config
        super().__init__(address, MediaHubHandler)


class MediaHubHandler(BaseHTTPRequestHandler):
    server: MediaHubServer
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args: object) -> None:
        print(f"{self.client_address[0]} - {fmt % args}", file=sys.stderr)

    def _client_allowed(self) -> bool:
        try:
            address = ipaddress.ip_address(self.client_address[0])
        except ValueError:
            return False
        return address.is_private or address.is_loopback

    def _send_json(self, status: int, data: object) -> None:
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        if self.close_connection:
            self.send_header("Connection", "close")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _query(self) -> dict[str, list[str]]:
        return parse_qs(urlsplit(self.path).query, keep_blank_values=True)

    def _media_path(self, relative: str) -> Path | None:
        root: Path = self.server.config["media_root"]
        candidate = (root / unquote(relative)).resolve()
        if candidate != root and root not in candidate.parents:
            return None
        return candidate

    def do_GET(self) -> None:
        if not self._client_allowed():
            self._send_json(403, {"error": "Only private-network clients are allowed"})
            return
        path = urlsplit(self.path).path
        if path == "/":
            self._send_json(200, {"name": "Pi Media Hub", "endpoints": ["/health", "/api/status", "/api/library", "/api/apps"]})
        elif path == "/health":
            self._send_json(200, {"status": "ok"})
        elif path == "/api/status":
            self._send_json(200, {
                "status": "ok",
                "name": "Pi Media Hub",
                "ai_enabled": bool(self.server.config["ai"].get("enabled", False)),
                "media_root": str(self.server.config["media_root"]),
                "apps": len(self.server.config["catalog"]),
            })
        elif path == "/api/library":
            self._library()
        elif path == "/api/apps":
            self._send_json(200, {"apps": self.server.config["catalog"]})
        elif path == "/api/config/ai":
            if not self._authorized():
                self._send_json(401, {"error": "Admin token required"})
            else:
                self._send_json(200, self._public_ai_config())
        elif path.startswith("/api/apps/"):
            self._app_action(path.removeprefix("/api/apps/"))
        elif path.startswith("/apps/"):
            self._serve_app_asset(path.removeprefix("/apps/"))
        elif path == "/media":
            self._stream_media()
        else:
            self._send_json(404, {"error": "Not found"})

    def do_HEAD(self) -> None:
        if not self._client_allowed():
            self._send_json(403, {"error": "Only private-network clients are allowed"})
        elif urlsplit(self.path).path == "/media":
            self._stream_media()
        else:
            self._send_json(404, {"error": "Not found"})

    def do_POST(self) -> None:
        if not self._client_allowed():
            self._send_json(403, {"error": "Only private-network clients are allowed"})
            return
        path = urlsplit(self.path).path
        if path == "/api/chat":
            if not self._authorized():
                self.close_connection = True
                self._send_json(401, {"error": "Admin token required"})
                return
            self._ai_chat()
            return
        if not self._authorized():
            self.close_connection = True
            self._send_json(401, {"error": "Admin token required"})
            return
        if path == "/api/apps":
            self._add_app()
        elif path == "/api/apps/upload":
            self._upload_app()
        elif path == "/api/apps/import/github":
            self._import_github_app()
        elif path == "/api/config/ai":
            self._save_ai_config()
        else:
            self._send_json(404, {"error": "Not found"})

    def do_DELETE(self) -> None:
        if not self._client_allowed():
            self._send_json(403, {"error": "Only private-network clients are allowed"})
        elif not self._authorized():
            self._send_json(401, {"error": "Admin token required"})
        else:
            path = urlsplit(self.path).path
            if path.startswith("/api/apps/"):
                self._remove_app(path.removeprefix("/api/apps/"))
            else:
                self._send_json(404, {"error": "Not found"})

    def _authorized(self) -> bool:
        token = self.server.config.get("admin_token", "")
        supplied = self.headers.get("Authorization", "")
        expected = f"Bearer {token}" if token else ""
        return bool(expected) and secrets.compare_digest(supplied, expected)

    def _read_json_body(self, limit: int = MAX_REQUEST_BYTES) -> object | None:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self._send_json(400, {"error": "Invalid Content-Length"})
            return None
        if length < 1 or length > limit:
            self._send_json(413, {"error": f"Request body must be 1-{limit} bytes"})
            return None
        try:
            return json.loads(self.rfile.read(length))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._send_json(400, {"error": "Request body must be valid JSON"})
            return None

    def _ai_chat(self) -> None:
        ai = self.server.config["ai"]
        if not ai.get("enabled", False):
            self._send_json(503, {"error": "Local AI is not configured"})
            return
        payload = self._read_json_body()
        if payload is None:
            return
        body = json.dumps(payload).encode("utf-8")
        request = Request(
            ai["endpoint"],
            data=body,
            headers={"Content-Type": "application/json", "Accept": "application/json"},
            method="POST",
        )
        try:
            with urlopen(request, timeout=ai["timeout_seconds"]) as response:
                response_body = response.read(MAX_REQUEST_BYTES + 1)
                if len(response_body) > MAX_REQUEST_BYTES:
                    self._send_json(502, {"error": "AI backend response exceeded size limit"})
                    return
                self.send_response(response.status)
                self.send_header("Content-Type", response.headers.get("Content-Type", "application/json"))
                self.send_header("Content-Length", str(len(response_body)))
                self.end_headers()
                self.wfile.write(response_body)
        except HTTPError as error:
            self._send_json(502, {"error": f"AI backend returned HTTP {error.code}"})
        except (URLError, TimeoutError, OSError) as error:
            self._send_json(502, {"error": f"AI backend request failed: {error}"})

    def _add_app(self) -> None:
        payload = self._read_json_body()
        if payload is None:
            return
        try:
            manifest = validate_app_manifest(payload)
            apps = self.server.config["catalog"]
            apps[:] = [app for app in apps if app.get("id") != manifest["id"]]
            apps.append(manifest)
            save_config(self.server.config)
        except (ValueError, OSError) as error:
            self._send_json(400, {"error": str(error)})
            return
        self._send_json(201, {"app": manifest})

    def _import_github_app(self) -> None:
        payload = self._read_json_body(4096)
        if payload is None:
            return
        if not isinstance(payload, dict):
            self._send_json(400, {"error": "Expected owner, repo, and optional branch"})
            return
        owner, repo = payload.get("owner"), payload.get("repo")
        branch = payload.get("branch", "main")
        if (
            not isinstance(owner, str) or not GITHUB_PART.fullmatch(owner)
            or not isinstance(repo, str) or not GITHUB_PART.fullmatch(repo)
            or owner in {".", ".."} or repo in {".", ".."}
            or not isinstance(branch, str) or not re.fullmatch(r"[A-Za-z0-9._/-]{1,100}", branch)
            or any(part in {"", ".", ".."} for part in branch.split("/"))
        ):
            self._send_json(400, {"error": "Invalid GitHub owner, repository, or branch"})
            return
        archive_data = None
        failures = []
        for candidate_branch in (branch, "master") if branch == "main" else (branch,):
            archive_url = f"https://codeload.github.com/{owner}/{repo}/zip/refs/heads/{candidate_branch}"
            try:
                request = Request(archive_url, headers={"Accept": "application/zip", "User-Agent": "PiMediaHub"})
                with urlopen(request, timeout=8) as response:
                    if urlsplit(response.geturl()).hostname != "codeload.github.com":
                        raise ValueError("GitHub redirected outside codeload.github.com")
                    archive_data = response.read(MAX_APP_ARCHIVE_BYTES + 1)
                if len(archive_data) > MAX_APP_ARCHIVE_BYTES:
                    raise ValueError("GitHub app archive exceeds the size limit")
                break
            except (HTTPError, URLError, TimeoutError, OSError, ValueError) as error:
                failures.append(str(error))
        if archive_data is None:
            self._send_json(400, {"error": "Could not download GitHub repository archive: " + "; ".join(failures)})
            return
        try:
            manifest = install_app_archive(
                archive_data,
                self.server.config["apps_dir"],
                github_owner_repo=f"{owner}/{repo}",
            )
        except ValueError as error:
            self._send_json(400, {"error": str(error)})
            return
        apps = self.server.config["catalog"]
        apps[:] = [app for app in apps if app.get("id") != manifest["id"]]
        apps.append(manifest)
        try:
            save_config(self.server.config)
        except OSError as error:
            self._send_json(500, {"error": f"Could not save app catalog: {error}"})
            return
        self._send_json(201, {"app": manifest})

    def _upload_app(self) -> None:
        if self.headers.get_content_type() != "application/zip":
            self._send_json(415, {"error": "Upload a ZIP file with Content-Type: application/zip"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self._send_json(400, {"error": "Invalid Content-Length"})
            return
        if length < 1 or length > MAX_APP_ARCHIVE_BYTES:
            self._send_json(413, {"error": f"App ZIP must be 1-{MAX_APP_ARCHIVE_BYTES} bytes"})
            return
        try:
            archive_data = self.rfile.read(length)
            manifest = install_app_archive(archive_data, self.server.config["apps_dir"])
            apps = self.server.config["catalog"]
            apps[:] = [app for app in apps if app.get("id") != manifest["id"]]
            apps.append(manifest)
            save_config(self.server.config)
        except (ValueError, OSError) as error:
            self._send_json(400, {"error": str(error)})
            return
        self._send_json(201, {"app": manifest})

    def _remove_app(self, app_id: str) -> None:
        app_id = unquote(app_id)
        if not APP_ID.fullmatch(app_id):
            self._send_json(400, {"error": "Invalid app id"})
            return
        apps = self.server.config["catalog"]
        remaining = [app for app in apps if app.get("id") != app_id]
        if len(remaining) == len(apps):
            self._send_json(404, {"error": "App not found"})
            return
        self.server.config["catalog"] = remaining
        try:
            removed = next(app for app in apps if app.get("id") == app_id)
            if removed.get("source") or str(removed.get("url", "")).startswith(f"/apps/{app_id}/"):
                target = self.server.config["apps_dir"] / app_id
                if target.is_dir() and not target.is_symlink():
                    shutil.rmtree(target)
            save_config(self.server.config)
        except OSError as error:
            self._send_json(500, {"error": f"Could not save app catalog: {error}"})
            return
        self._send_json(200, {"removed": app_id})

    def _save_ai_config(self) -> None:
        payload = self._read_json_body(4096)
        if payload is None:
            return
        if not isinstance(payload, dict):
            self._send_json(400, {"error": "AI settings must be a JSON object"})
            return
        endpoint = payload.get("endpoint", "")
        enabled = payload.get("enabled", False)
        model = payload.get("model", "")
        if not isinstance(endpoint, str) or not isinstance(enabled, bool) or not isinstance(model, str):
            self._send_json(400, {"error": "endpoint/model must be strings and enabled must be boolean"})
            return
        parsed = urlsplit(endpoint)
        if enabled and (parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username):
            self._send_json(400, {"error": "Enabled AI requires an http(s) endpoint without embedded credentials"})
            return
        backend = payload.get("backend", "Custom")
        if backend not in {"Ollama", "OpenAI compatible", "Custom"}:
            self._send_json(400, {"error": "Unsupported AI backend"})
            return
        self.server.config["ai"].update({"endpoint": endpoint, "enabled": enabled, "model": model, "backend": backend})
        try:
            save_config(self.server.config)
        except OSError as error:
            self._send_json(500, {"error": f"Could not save AI settings: {error}"})
            return
        self._send_json(200, self._public_ai_config())

    def _public_ai_config(self) -> dict:
        ai = self.server.config["ai"]
        return {
            "enabled": bool(ai.get("enabled", False)),
            "endpoint": ai.get("endpoint", ""),
            "model": ai.get("model", ""),
            "backend": ai.get("backend", "Custom"),
        }

    def _app_action(self, app_id: str) -> None:
        app_id = unquote(app_id)
        app = next((item for item in self.server.config["catalog"] if item.get("id") == app_id), None)
        if app is None:
            self._send_json(404, {"error": "App not found"})
        else:
            self._send_json(200, {"app": app, "action": "open"})

    def _serve_app_asset(self, relative: str) -> None:
        parts = PurePosixPath(unquote(relative))
        if len(parts.parts) < 2 or any(part in {"", ".", ".."} for part in parts.parts):
            self._send_json(404, {"error": "App asset not found"})
            return
        app_id = parts.parts[0]
        if not APP_ID.fullmatch(app_id):
            self._send_json(404, {"error": "App asset not found"})
            return
        root: Path = self.server.config["apps_dir"] / app_id
        target = (root.joinpath(*parts.parts[1:])).resolve()
        try:
            target.relative_to(root.resolve())
            if target.suffix.lower() not in APP_ASSET_EXTENSIONS or not target.is_file():
                raise FileNotFoundError
            size = target.stat().st_size
            self.send_response(200)
            self.send_header("Content-Type", mimetypes.guess_type(target.name)[0] or "application/octet-stream")
            self.send_header("Content-Length", str(size))
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header(
                "Content-Security-Policy",
                "sandbox allow-scripts; default-src 'self' data:; connect-src 'none'; form-action 'none'; "
                "frame-src 'none'; object-src 'none'; base-uri 'none'",
            )
            self.end_headers()
            if self.command != "HEAD":
                with target.open("rb") as source:
                    shutil.copyfileobj(source, self.wfile, length=64 * 1024)
        except (OSError, ValueError):
            self._send_json(404, {"error": "App asset not found"})

    def _library(self) -> None:
        query = self._query()
        kind = query.get("kind", ["all"])[0].lower()
        search = query.get("q", [""])[0].casefold()
        if kind not in {"all", "audio", "video", "podcast"}:
            self._send_json(400, {"error": "kind must be all, audio, video, or podcast"})
            return
        root: Path = self.server.config["media_root"]
        items = []
        for directory, subdirs, filenames in os.walk(root, followlinks=False):
            subdirs[:] = [name for name in subdirs if not (Path(directory) / name).is_symlink()]
            for filename in filenames:
                candidate = Path(directory) / filename
                try:
                    resolved = candidate.resolve(strict=True)
                    resolved.relative_to(root)
                    if not resolved.is_file():
                        continue
                    suffix = resolved.suffix.lower()
                    if suffix not in MEDIA_TYPES["audio"] | MEDIA_TYPES["video"]:
                        continue
                    relative = resolved.relative_to(root).as_posix()
                    is_video = suffix in MEDIA_TYPES["video"]
                    category = "video" if is_video else (
                        "podcast" if any(part.startswith("podcast") for part in relative.casefold().split("/")) else "audio"
                    )
                    if kind != "all" and category != kind:
                        continue
                    if search and search not in relative.casefold():
                        continue
                    stat = resolved.stat()
                    items.append({
                        "name": filename,
                        "path": relative,
                        "kind": category,
                        "size": stat.st_size,
                        "modified": int(stat.st_mtime),
                        "mime_type": mimetypes.guess_type(filename)[0] or "application/octet-stream",
                        "url": f"/media?path={quote(relative, safe='/')}",
                    })
                    if len(items) >= MAX_LIBRARY_ITEMS:
                        break
                except (OSError, ValueError):
                    continue
            if len(items) >= MAX_LIBRARY_ITEMS:
                break
        self._send_json(200, {"items": items, "truncated": len(items) >= MAX_LIBRARY_ITEMS})

    def _stream_media(self) -> None:
        relative = self._query().get("path", [""])[0]
        if not relative:
            self._send_json(400, {"error": "path is required"})
            return
        target = self._media_path(relative)
        if target is None:
            self._send_json(403, {"error": "Path is outside the media root"})
            return
        try:
            if not target.is_file():
                self._send_json(404, {"error": "Media file not found"})
                return
            size = target.stat().st_size
            start, end, status = 0, size - 1, 200
            requested_range = self.headers.get("Range")
            if requested_range:
                match = re.fullmatch(r"bytes=(\d*)-(\d*)", requested_range.strip())
                if not match or size == 0:
                    self._send_range_error(size)
                    return
                first, last = match.groups()
                if first:
                    start = int(first)
                    end = min(int(last), size - 1) if last else size - 1
                elif last:
                    count = int(last)
                    start = max(0, size - count)
                if start >= size or end < start:
                    self._send_range_error(size)
                    return
                status = 206
            length = max(0, end - start + 1)
            self.send_response(status)
            self.send_header("Content-Type", mimetypes.guess_type(target.name)[0] or "application/octet-stream")
            self.send_header("Content-Length", str(length))
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("X-Content-Type-Options", "nosniff")
            if status == 206:
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            self.end_headers()
            if self.command != "HEAD":
                with target.open("rb") as media:
                    media.seek(start)
                    remaining = length
                    while remaining:
                        chunk = media.read(min(64 * 1024, remaining))
                        if not chunk:
                            break
                        self.wfile.write(chunk)
                        remaining -= len(chunk)
        except OSError:
            self._send_json(404, {"error": "Media file not found"})

    def _send_range_error(self, size: int) -> None:
        self.send_response(416)
        self.send_header("Content-Range", f"bytes */{size}")
        self.send_header("Content-Length", "0")
        self.end_headers()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Serve a private-LAN media library")
    parser.add_argument("--config", default="/etc/pi-media-hub/config.json", help="JSON configuration file")
    args = parser.parse_args(argv)
    try:
        config = load_config(Path(args.config))
        server = MediaHubServer((config["host"], config["port"]), config)
    except (OSError, json.JSONDecodeError, ValueError) as error:
        parser.error(str(error))
    print(f"Pi Media Hub listening on {config['host']}:{config['port']} (media: {config['media_root']})")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping Pi Media Hub")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
