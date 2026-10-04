from __future__ import annotations

import argparse
import ipaddress
import json
import mimetypes
import os
import re
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, quote, unquote, urlsplit
from urllib.request import Request, urlopen


MEDIA_TYPES = {
    "audio": {".aac", ".aiff", ".alac", ".flac", ".m4a", ".mp3", ".ogg", ".opus", ".wav", ".wma"},
    "video": {".avi", ".m4v", ".mkv", ".mov", ".mp4", ".mpeg", ".mpg", ".webm"},
}
MAX_LIBRARY_ITEMS = 5000
MAX_REQUEST_BYTES = 64 * 1024


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
    return {
        **config,
        "host": host,
        "port": port,
        "media_root": root,
        "catalog": catalog,
        "ai": {**ai, "timeout_seconds": timeout},
    }


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
            })
        elif path == "/api/library":
            self._library()
        elif path == "/api/apps":
            self._send_json(200, {"apps": self.server.config["catalog"]})
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
        if urlsplit(self.path).path != "/api/chat":
            self._send_json(404, {"error": "Not found"})
            return
        ai = self.server.config["ai"]
        if not ai.get("enabled", False):
            self._send_json(503, {"error": "Local AI is not configured"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self._send_json(400, {"error": "Invalid Content-Length"})
            return
        if length < 1 or length > MAX_REQUEST_BYTES:
            self._send_json(413, {"error": f"Request body must be 1-{MAX_REQUEST_BYTES} bytes"})
            return
        body = self.rfile.read(length)
        try:
            json.loads(body)
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._send_json(400, {"error": "Request body must be valid JSON"})
            return
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
