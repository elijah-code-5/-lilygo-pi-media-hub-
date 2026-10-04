import json
import getpass
import io
import importlib.util
import os
import subprocess
import sys
import tempfile
import threading
import unittest
import wave
import zipfile
from unittest.mock import patch
from http.client import HTTPConnection
from pathlib import Path
from urllib.parse import quote

from pi_media_hub.server import MediaHubServer, load_config
from pi_media_hub.setup import main as setup_main
from pi_media_hub.gui import (
    build_flash_args,
    normalize_ai_endpoint,
    normalize_server_url,
    save_controller_settings,
)
from pi_media_hub.local_apps import WavRecorder, load_notes, new_note, normalize_browser_url, save_notes
from pi_media_hub.robot import (
    build_control_payload,
    check_adapter,
    load_firmware_package,
    normalize_robot_url,
    send_control,
    send_stop,
    upload_firmware,
    validate_program,
)
from device.robot_adapter import protocol as robot_protocol


class GuiTests(unittest.TestCase):
    def test_server_address_normalization(self):
        self.assertEqual(normalize_server_url(" http://pi.local:8765/ "), "http://pi.local:8765")
        for invalid in ("pi.local:8765", "ftp://pi.local", "http://user@pi.local", "http://pi.local/path"):
            with self.subTest(invalid=invalid):
                with self.assertRaises(ValueError):
                    normalize_server_url(invalid)

    def test_openai_endpoint_normalization(self):
        self.assertEqual(normalize_ai_endpoint("OpenAI compatible", "http://pi:8000"), "http://pi:8000/v1/chat/completions")
        self.assertEqual(normalize_ai_endpoint("OpenAI compatible", "http://pi:8000/v1/"), "http://pi:8000/v1/chat/completions")
        self.assertEqual(normalize_ai_endpoint("Ollama", "http://pi:11434/api/chat"), "http://pi:11434/api/chat")

    def test_firmware_flash_arguments_are_fixed_and_validated(self):
        with tempfile.TemporaryDirectory() as directory:
            image = Path(directory) / "firmware.bin"
            image.write_bytes(b"firmware")
            args = build_flash_args("/dev/ttyACM0", str(image), "0x0", "460800")
            self.assertEqual(args[:7], ["--chip", "esp32s3", "--port", "/dev/ttyACM0", "--baud", "460800", "write-flash"])
            for port in ("/dev/sda", "/tmp/serial", "--erase-all"):
                with self.subTest(port=port), self.assertRaises(ValueError):
                    build_flash_args(port, str(image), "0x0", "460800")
            with self.assertRaises(ValueError):
                build_flash_args("/dev/ttyACM0", str(image), "not-hex", "460800")

    def test_controller_settings_are_private_and_persistent(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.dict(os.environ, {"XDG_CONFIG_HOME": directory}):
                save_controller_settings({"server_url": "http://pi.local:8765", "admin_token": "secret"})
            settings_path = Path(directory) / "pi-media-hub/controller.json"
            self.assertEqual(json.loads(settings_path.read_text())["server_url"], "http://pi.local:8765")
            self.assertEqual(settings_path.stat().st_mode & 0o777, 0o600)

    def test_browser_address_validation(self):
        self.assertEqual(normalize_browser_url("example.org/path"), "https://example.org/path")
        self.assertEqual(normalize_browser_url("http://127.0.0.1:8080"), "http://127.0.0.1:8080")
        for invalid in ("", "ftp://example.org", "https://user:pass@example.org", "http://[bad"):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                normalize_browser_url(invalid)

    def test_notebook_persistence_is_private(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "notes.json"
            note = new_note("Plan")
            note["body"] = "A private note"
            save_notes([note], path)
            self.assertEqual(load_notes(path), [note])
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            with self.assertRaises(ValueError):
                save_notes([{"id": "bad"}], path)

    def test_recorder_writes_private_wav_with_mock_audio_device(self):
        import types

        class FakeStream:
            def __init__(self, *, callback, **_kwargs):
                self.callback = callback

            def start(self):
                self.callback(b"\0\0" * 4, 4, None, None)

            def stop(self):
                return None

            def close(self):
                return None

        fake_sounddevice = types.SimpleNamespace(RawInputStream=FakeStream)
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "recording.wav"
            with patch.dict(sys.modules, {"sounddevice": fake_sounddevice}):
                recorder = WavRecorder(output)
                recorder.start()
                self.assertTrue(recorder.recording)
                recorder.stop()
            self.assertFalse(recorder.recording)
            self.assertEqual(output.stat().st_mode & 0o777, 0o600)
            with wave.open(str(output), "rb") as recording:
                self.assertEqual(recording.getnchannels(), 1)
                self.assertEqual(recording.getframerate(), 44100)
                self.assertEqual(recording.getnframes(), 4)

    def test_robot_address_and_finite_control_are_restricted(self):
        self.assertEqual(normalize_robot_url("http://192.168.1.12:8080"), "http://192.168.1.12:8080")
        self.assertEqual(build_control_payload("forward", 200), {"command": "forward", "duration_ms": 200})
        self.assertEqual(build_control_payload("stop"), {"command": "stop", "duration_ms": 0})
        for address in ("http://example.org", "http://192.168.1.12/path", "http://user@192.168.1.12"):
            with self.subTest(address=address), self.assertRaises(ValueError):
                normalize_robot_url(address)
        for command, duration in (("run", 100), ("forward", 0), ("backward", 401), ("left", True)):
            with self.subTest(command=command, duration=duration), self.assertRaises(ValueError):
                build_control_payload(command, duration)

    def test_robot_program_is_bounded_to_fixed_commands(self):
        steps = validate_program([
            {"command": "forward", "duration_ms": 200},
            {"command": "stop", "duration_ms": 0},
            {"command": "left", "duration_ms": 300},
        ])
        self.assertEqual(len(steps), 2)
        for invalid in (
            [{"command": "forward", "duration_ms": 3000}, {"command": "backward", "duration_ms": 3000}],
            [{"command": "shell", "duration_ms": 100}],
            [{"command": "stop", "duration_ms": 0}],
        ):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                validate_program(invalid)

    def test_robot_api_requires_authenticated_safe_profile_and_stop(self):
        import io

        status = {
            "profile": "pi-media-hub-robot-v1",
            "board": "freenove-custom-v1",
            "version": "1.0.0",
            "motor_stop": True,
            "motor_watchdog_ms": 300,
        }
        with patch("pi_media_hub.robot.urlopen") as open_url:
            open_url.return_value.__enter__.return_value = io.BytesIO(json.dumps(status).encode())
            self.assertEqual(
                check_adapter("http://192.168.1.10:80", "a" * 24, "freenove-custom-v1"),
                status,
            )
            self.assertEqual(open_url.call_args.args[0].get_header("Authorization"), "Bearer " + "a" * 24)
            open_url.return_value.__enter__.return_value = io.BytesIO(
                json.dumps({"accepted": True}).encode()
            )
            self.assertEqual(
                send_control("http://192.168.1.10:80", "forward", 150, token="a" * 24),
                {"accepted": True},
            )
            request = open_url.call_args.args[0]
            self.assertEqual(request.full_url, "http://192.168.1.10:80/api/control")
            self.assertEqual(json.loads(request.data), {"command": "forward", "duration_ms": 150})
            open_url.return_value.__enter__.return_value = io.BytesIO(
                json.dumps({"accepted": True}).encode()
            )
            self.assertTrue(send_stop("http://192.168.1.10:80", token="a" * 24)["accepted"])
        with self.assertRaisesRegex(ValueError, "token"):
            send_stop("http://192.168.1.10:80", token="")

    def test_robot_firmware_package_requires_board_and_checksum(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "firmware.zip"
            image = b"firmware-data"
            import hashlib

            manifest = {
                "board": "freenove-custom-v1",
                "version": "1.0.0",
                "sha256": hashlib.sha256(image).hexdigest(),
            }
            with zipfile.ZipFile(path, "w") as archive:
                archive.writestr("pi-media-hub-robot.json", json.dumps(manifest))
                archive.writestr("firmware.bin", image)
            package = load_firmware_package(path, "freenove-custom-v1")
            self.assertEqual(package["image"], image)
            with self.assertRaisesRegex(ValueError, "does not match"):
                load_firmware_package(path, "other-board")
            manifest["sha256"] = "0" * 64
            with zipfile.ZipFile(path, "w") as archive:
                archive.writestr("pi-media-hub-robot.json", json.dumps(manifest))
                archive.writestr("firmware.bin", image)
            with self.assertRaisesRegex(ValueError, "checksum"):
                load_firmware_package(path, "freenove-custom-v1")

    def test_robot_firmware_protocol_defaults_fail_safe(self):
        self.assertFalse(robot_protocol.valid_token("wrong", "a" * 24))
        self.assertTrue(robot_protocol.valid_token("a" * 24, "a" * 24))
        self.assertEqual(
            robot_protocol.validate_motion({"command": "stop", "duration_ms": 0}, False),
            ("stop", 0),
        )
        for payload in (
            {"command": "forward", "duration_ms": 100},
            {"command": "gpio", "duration_ms": 1},
            {"command": "stop", "duration_ms": 5},
        ):
            with self.subTest(payload=payload):
                with self.assertRaises((ValueError, RuntimeError)):
                    robot_protocol.validate_motion(payload, False)
        with self.assertRaisesRegex(RuntimeError, "OTA is disabled"):
            robot_protocol.validate_ota_headers("board", "1.0", "a" * 64, 10, "board", False)
        self.assertTrue(
            robot_protocol.validate_ota_headers("board", "1.0", "a" * 64, 10, "board", True)
        )
        with self.assertRaisesRegex(ValueError, "mismatch"):
            robot_protocol.validate_ota_headers("wrong", "1.0", "a" * 64, 10, "board", True)
        with self.assertRaisesRegex(ValueError, "size"):
            robot_protocol.validate_ota_headers(
                "board", "1.0", "a" * 64, robot_protocol.MAX_FIRMWARE_BYTES + 1, "board", True
            )

    def test_robot_firmware_http_api_auth_and_motor_gate(self):
        config = type("Config", (), {
            "ADMIN_TOKEN": "a" * 24,
            "ALLOWED_CLIENT_PREFIXES": ("192.168.1.",),
            "MOTOR_ENABLED": False,
            "OTA_ENABLED": False,
            "BOARD_ID": "profile-not-configured",
            "FIRMWARE_VERSION": "0.1.0-template",
            "WIFI_SSID": "test",
            "WIFI_PASSWORD": "test",
        })
        network = type("Network", (), {"STA_IF": 0, "WLAN": lambda _interface: None})
        module_path = Path(__file__).parents[1] / "device/robot_adapter/main.py"
        robot_dir = str(module_path.parent)
        sys.path.insert(0, robot_dir)
        try:
            with patch.dict(sys.modules, {"config": config, "network": network}):
                spec = importlib.util.spec_from_file_location("robot_firmware_test", module_path)
                firmware = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(firmware)
        finally:
            sys.path.remove(robot_dir)

        class FakeSocket:
            def __init__(self, request):
                self.request = request
                self.response = bytearray()

            def recv(self, size):
                chunk, self.request = self.request[:size], self.request[size:]
                return chunk

            def send(self, data):
                self.response.extend(data)
                return len(data)

        def call(method, path, payload, *, authenticated=True, address=("192.168.1.5", 5000)):
            body = json.dumps(payload).encode()
            auth = "Authorization: Bearer {}\r\n".format("a" * 24 if authenticated else "wrong")
            request = (
                "{} {} HTTP/1.1\r\nHost: robot\r\n{}Content-Type: application/json\r\n"
                "Content-Length: {}\r\n\r\n".format(method, path, auth, len(body)).encode() + body
            )
            client = FakeSocket(request)
            firmware._handle_client(client, address)
            head, response_body = bytes(client.response).split(b"\r\n\r\n", 1)
            return int(head.split(b" ")[1]), json.loads(response_body)

        self.assertEqual(call("GET", "/api/status", {})[0], 200)
        self.assertEqual(
            call("POST", "/api/control", {"command": "forward", "duration_ms": 100}, authenticated=False)[0],
            401,
        )
        status, response = call("POST", "/api/control", {"command": "forward", "duration_ms": 100})
        self.assertEqual(status, 503)
        self.assertIn("disabled", response["error"])
        status, response = call("POST", "/api/stop", {})
        self.assertEqual(status, 200)
        self.assertTrue(response["accepted"])
        status, response = call(
            "PUT",
            "/api/firmware/update",
            {},
        )
        self.assertEqual(status, 501)
        self.assertEqual(
            call("GET", "/api/status", {}, address=("10.0.0.5", 5000))[0],
            403,
        )


class ServerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "media"
        self.root.mkdir()
        (self.root / "song.mp3").write_bytes(b"0123456789")
        (self.root / "movie.mp4").write_bytes(b"movie")
        podcast_dir = self.root / "Podcasts"
        podcast_dir.mkdir()
        (podcast_dir / "episode.ogg").write_bytes(b"podcast")
        self.config_path = Path(self.temp.name) / "config.json"
        self.config_path.write_text(json.dumps({
            "host": "127.0.0.1",
            "port": 8765,
            "media_root": str(self.root),
            "apps_dir": str(Path(self.temp.name) / "apps"),
            "admin_token": "test-admin-token",
            "catalog": [{"id": "library", "name": "Library"}],
            "ai": {"enabled": False},
        }))
        config = load_config(self.config_path)
        self.server = MediaHubServer(("127.0.0.1", 0), config)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.connection = HTTPConnection("127.0.0.1", self.server.server_port, timeout=3)

    def tearDown(self):
        self.connection.close()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)
        self.temp.cleanup()

    def request(self, path, method="GET", headers=None, body=None):
        self.connection.request(method, path, body=body, headers=headers or {})
        response = self.connection.getresponse()
        return response.status, response.getheaders(), response.read()

    def test_health_and_catalog(self):
        self.assertEqual(self.request("/health")[0], 200)
        status, _, body = self.request("/api/apps")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["apps"][0]["id"], "library")

    def test_library_filters_audio_video_and_podcasts(self):
        _, _, body = self.request("/api/library?kind=audio")
        self.assertEqual([item["name"] for item in json.loads(body)["items"]], ["song.mp3"])
        _, _, body = self.request("/api/library?kind=podcast")
        self.assertEqual([item["name"] for item in json.loads(body)["items"]], ["episode.ogg"])
        _, _, body = self.request("/api/library?kind=video")
        self.assertEqual([item["name"] for item in json.loads(body)["items"]], ["movie.mp4"])

    def test_library_pagination_preserves_total_and_rejects_bad_ranges(self):
        for index in range(5):
            (self.root / "track-{}.mp3".format(index)).write_bytes(b"track")
        status, _, body = self.request("/api/library?kind=audio&limit=2&offset=1")
        self.assertEqual(status, 200)
        result = json.loads(body)
        self.assertEqual(result["total"], 6)
        self.assertEqual(result["offset"], 1)
        self.assertEqual(len(result["items"]), 2)
        self.assertEqual(result["items"][0]["path"], "track-0.mp3")
        self.assertEqual(self.request("/api/library?limit=0")[0], 400)
        self.assertEqual(self.request("/api/library?offset=invalid")[0], 400)

    def test_media_stream_and_byte_range(self):
        status, headers, body = self.request("/media?path=song.mp3", headers={"Range": "bytes=2-5"})
        self.assertEqual(status, 206)
        self.assertEqual(body, b"2345")
        self.assertIn(("Content-Range", "bytes 2-5/10"), headers)

    def test_media_traversal_is_rejected(self):
        secret = Path(self.temp.name) / "secret.mp3"
        secret.write_bytes(b"private")
        status, _, _ = self.request("/media?path=" + quote("../secret.mp3"))
        self.assertIn(status, (403, 404))

    def test_invalid_library_filter(self):
        self.assertEqual(self.request("/api/library?kind=unknown")[0], 400)

    def test_app_management_requires_token_and_persists_manifest(self):
        manifest = {
            "id": "weather",
            "name": "Weather",
            "description": "Forecast",
            "url": "https://example.org/weather",
        }
        body = json.dumps(manifest)
        self.assertEqual(self.request("/api/apps", "POST", body=body)[0], 401)
        status, _, _ = self.request(
            "/api/apps",
            "POST",
            headers={"Authorization": "Bearer test-admin-token", "Content-Type": "application/json"},
            body=body,
        )
        self.assertEqual(status, 201)
        self.assertIn(manifest, json.loads(self.config_path.read_text())["catalog"])
        self.assertEqual(self.request("/api/apps/weather")[0], 200)
        status, _, _ = self.request(
            "/api/apps/weather",
            "DELETE",
            headers={"Authorization": "Bearer test-admin-token"},
        )
        self.assertEqual(status, 200)

    def test_ai_settings_are_protected_and_saved(self):
        payload = json.dumps({
            "enabled": True,
            "endpoint": "http://127.0.0.1:11434/api/chat",
            "model": "small-model",
            "backend": "Ollama",
        })
        self.assertEqual(self.request("/api/config/ai", "POST", body=payload)[0], 401)
        headers = {"Authorization": "Bearer test-admin-token", "Content-Type": "application/json"}
        self.assertEqual(self.request("/api/config/ai", "POST", headers=headers, body=payload)[0], 200)
        self.assertEqual(json.loads(self.config_path.read_text())["ai"]["model"], "small-model")
        self.assertEqual(self.request("/api/config/ai", headers={"Authorization": "Bearer test-admin-token"})[0], 200)
        self.assertEqual(self.request("/api/chat", "POST", body='{"message":"hello"}')[0], 401)

    def test_static_web_app_zip_upload_is_sandboxed_and_removable(self):
        archive = io.BytesIO()
        with zipfile.ZipFile(archive, "w") as bundle:
            bundle.writestr("demo-repo/pi-media-hub-app.json", json.dumps({
                "id": "demo",
                "name": "Demo app",
                "description": "A static demo",
            }))
            bundle.writestr("demo-repo/index.html", "<h1>Hello</h1>")
            bundle.writestr("demo-repo/app.js", "alert('sandboxed')")
            bundle.writestr("demo-repo/run.py", "must not be installed")
        headers = {
            "Authorization": "Bearer test-admin-token",
            "Content-Type": "application/zip",
        }
        status, _, response = self.request("/api/apps/upload", "POST", headers=headers, body=archive.getvalue())
        self.assertEqual(status, 201, response)
        installed = json.loads(response)["app"]
        self.assertEqual(installed["url"], "/apps/demo/index.html")
        self.assertTrue((Path(self.temp.name) / "apps/demo/index.html").is_file())
        self.assertFalse((Path(self.temp.name) / "apps/demo/run.py").exists())
        status, headers, body = self.request(installed["url"])
        self.assertEqual(status, 200)
        self.assertIn(("Content-Security-Policy", "sandbox allow-scripts; default-src 'self' data:; connect-src 'none'; form-action 'none'; frame-src 'none'; object-src 'none'; base-uri 'none'"), headers)
        self.assertEqual(body, b"<h1>Hello</h1>")
        self.assertEqual(self.request("/apps/demo/../config.json")[0], 404)

    def test_app_archive_rejects_path_traversal(self):
        archive = io.BytesIO()
        with zipfile.ZipFile(archive, "w") as bundle:
            bundle.writestr("demo/pi-media-hub-app.json", json.dumps({"id": "demo", "name": "Demo"}))
            bundle.writestr("demo/index.html", "safe")
            bundle.writestr("demo/../../escape.txt", "unsafe")
        self.assertEqual(
            self.request(
                "/api/apps/upload",
                "POST",
                headers={"Authorization": "Bearer test-admin-token", "Content-Type": "application/zip"},
                body=archive.getvalue(),
            )[0],
            400,
        )

    def test_github_import_fetches_fixed_host_archive(self):
        archive = io.BytesIO()
        with zipfile.ZipFile(archive, "w") as bundle:
            bundle.writestr("repo-main/pi-media-hub-app.json", json.dumps({
                "id": "remote-app",
                "name": "Remote App",
                "description": "Static Github import",
            }))
            bundle.writestr("repo-main/index.html", "remote")

        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return None

            def geturl(self):
                return "https://codeload.github.com/owner/repo/zip/refs/heads/main"

            def read(self, _limit):
                return archive.getvalue()

        with patch("pi_media_hub.server.urlopen", return_value=Response()) as fetch:
            status, _, body = self.request(
                "/api/apps/import/github",
                "POST",
                headers={"Authorization": "Bearer test-admin-token", "Content-Type": "application/json"},
                body=json.dumps({"owner": "owner", "repo": "repo"}),
            )
        self.assertEqual(status, 201, body)
        self.assertIn("codeload.github.com", fetch.call_args.args[0].full_url)
        app = json.loads(body)["app"]
        self.assertEqual(app["source"], "owner/repo")
        self.assertEqual(self.request(app["url"])[2], b"remote")


class SetupTests(unittest.TestCase):
    def test_install_copies_server_and_preserves_existing_config(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config_dir = root / "etc"
            config_dir.mkdir()
            config_path = config_dir / "config.json"
            config_path.write_text('{"media_root": "/existing"}\n', encoding="utf-8")
            result = setup_main([
                "install",
                "--prefix", str(root / "opt"),
                "--config-dir", str(config_dir),
                "--unit-dir", str(root / "systemd"),
                "--media-root", str(root / "media"),
                "--apps-dir", str(root / "var/apps"),
                "--service-user", getpass.getuser(),
            ])
            self.assertEqual(result, 0)
            self.assertEqual(json.loads(config_path.read_text())["media_root"], "/existing")
            self.assertTrue((root / "opt/lib/pi_media_hub/server.py").is_file())
            self.assertTrue((root / "opt/lib/pi_media_hub/__main__.py").is_file())
            unit = (root / "systemd/pi-media-hub.service").read_text()
            self.assertIn("User={}".format(getpass.getuser()), unit)
            self.assertIn("ExecStart=/usr/bin/python3 -m pi_media_hub.server", unit)
            environment = os.environ.copy()
            environment["PYTHONPATH"] = str(root / "opt/lib")
            result = subprocess.run(
                [sys.executable, "-m", "pi_media_hub.server", "--help"],
                env=environment,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            result = subprocess.run(
                [sys.executable, "-m", "pi_media_hub", "--help"],
                env=environment,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)

            new_config_dir = root / "new-etc"
            setup_main([
                "install",
                "--prefix", str(root / "new-opt"),
                "--config-dir", str(new_config_dir),
                "--unit-dir", str(root / "new-systemd"),
                "--media-root", str(root / "new-media"),
                "--apps-dir", str(root / "new-var/apps"),
                "--service-user", getpass.getuser(),
            ])
            created = json.loads((new_config_dir / "config.json").read_text())
            self.assertEqual(created["media_root"], str(root / "new-media"))
            self.assertEqual(created["apps_dir"], str(root / "new-var/apps"))
            self.assertGreaterEqual(len(created["admin_token"]), 32)


if __name__ == "__main__":
    unittest.main()
