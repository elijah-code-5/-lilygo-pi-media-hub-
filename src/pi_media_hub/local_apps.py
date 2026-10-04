from __future__ import annotations

import json
import os
import tempfile
import wave
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit


def normalize_browser_url(value: str) -> str:
    value = value.strip()
    if not value:
        raise ValueError("Enter a web address.")
    candidate = value if "://" in value else "https://" + value
    parsed = urlsplit(candidate)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
    ):
        raise ValueError("Enter a valid http(s) web address without embedded credentials.")
    try:
        parsed.port
    except ValueError as error:
        raise ValueError("The web address contains an invalid port.") from error
    return candidate


def default_notes_path() -> Path:
    root = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share"))
    return root / "pi-media-hub" / "notes.json"


def load_notes(path: Path | None = None) -> list[dict]:
    source = path or default_notes_path()
    try:
        value = json.loads(source.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []
    except json.JSONDecodeError as error:
        raise ValueError(f"Notes file is not valid JSON: {source}") from error
    if not isinstance(value, list) or any(
        not isinstance(note, dict)
        or not isinstance(note.get("id"), str)
        or not isinstance(note.get("title"), str)
        or not isinstance(note.get("body"), str)
        or not isinstance(note.get("updated"), str)
        for note in value
    ):
        raise ValueError(f"Notes file has an unsupported format: {source}")
    return value


def save_notes(notes: list[dict], path: Path | None = None) -> None:
    target = path or default_notes_path()
    if len(notes) > 1000:
        raise ValueError("The notebook supports up to 1,000 notes.")
    for note in notes:
        if (
            not isinstance(note, dict)
            or not isinstance(note.get("id"), str)
            or not isinstance(note.get("title"), str)
            or not isinstance(note.get("body"), str)
            or not isinstance(note.get("updated"), str)
            or len(note["title"]) > 200
            or len(note["body"]) > 1_000_000
        ):
            raise ValueError("A note is invalid or exceeds the supported size.")
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary_name = None
    try:
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=target.parent, delete=False
        ) as temporary:
            temporary_name = temporary.name
            json.dump(notes, temporary, ensure_ascii=False, indent=2)
            temporary.write("\n")
            temporary.flush()
            os.fsync(temporary.fileno())
        os.chmod(temporary_name, 0o600)
        os.replace(temporary_name, target)
    except OSError:
        if temporary_name:
            Path(temporary_name).unlink(missing_ok=True)
        raise


def new_note(title: str = "Untitled note") -> dict:
    return {
        "id": datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S%f"),
        "title": title,
        "body": "",
        "updated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


class WavRecorder:
    def __init__(self, path: Path, sample_rate: int = 44100, channels: int = 1):
        self.path = path
        self.sample_rate = sample_rate
        self.channels = channels
        self._wave_file = None
        self._raw_file = None
        self._stream = None

    @property
    def recording(self) -> bool:
        return self._stream is not None

    def start(self) -> None:
        if self.recording:
            raise RuntimeError("A recording is already in progress.")
        try:
            import sounddevice as sd
        except ImportError as error:
            raise RuntimeError("Audio recording support is unavailable in this build.") from error
        self.path.parent.mkdir(parents=True, exist_ok=True)
        raw_file = self.path.open("wb")
        output = None
        try:
            os.chmod(self.path, 0o600)
            output = wave.open(raw_file, "wb")
            output.setnchannels(self.channels)
            output.setsampwidth(2)
            output.setframerate(self.sample_rate)

            def write_audio(indata, _frames, _time_info, _status):
                output.writeframesraw(bytes(indata))

            stream = sd.RawInputStream(
                samplerate=self.sample_rate,
                channels=self.channels,
                dtype="int16",
                callback=write_audio,
            )
            stream.start()
        except Exception as error:
            if output is not None:
                output.close()
            raw_file.close()
            raise RuntimeError(f"Could not start microphone recording: {error}") from error
        self._wave_file = output
        self._raw_file = raw_file
        self._stream = stream

    def stop(self) -> None:
        stream, output = self._stream, self._wave_file
        if stream is None:
            return
        self._stream = None
        self._wave_file = None
        raw_file = self._raw_file
        self._raw_file = None
        try:
            stream.stop()
        finally:
            try:
                stream.close()
            finally:
                try:
                    if output is not None:
                        output.close()
                finally:
                    if raw_file is not None:
                        raw_file.close()
