import json
import getpass
import tempfile
import threading
import unittest
from http.client import HTTPConnection
from pathlib import Path
from urllib.parse import quote

from pi_media_hub.server import MediaHubServer, load_config
from pi_media_hub.setup import main as setup_main


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
                "--service-user", getpass.getuser(),
            ])
            self.assertEqual(result, 0)
            self.assertEqual(json.loads(config_path.read_text())["media_root"], "/existing")
            self.assertTrue((root / "opt/lib/pi_media_hub/server.py").is_file())
            unit = (root / "systemd/pi-media-hub.service").read_text()
            self.assertIn("User={}".format(getpass.getuser()), unit)
            self.assertIn("ExecStart=/usr/bin/python3 -m pi_media_hub", unit)

            new_config_dir = root / "new-etc"
            setup_main([
                "install",
                "--prefix", str(root / "new-opt"),
                "--config-dir", str(new_config_dir),
                "--unit-dir", str(root / "new-systemd"),
                "--media-root", str(root / "new-media"),
                "--service-user", getpass.getuser(),
            ])
            created = json.loads((new_config_dir / "config.json").read_text())
            self.assertEqual(created["media_root"], str(root / "new-media"))


if __name__ == "__main__":
    unittest.main()
