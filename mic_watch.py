#!/usr/bin/env python3
"""
Черга ручних команд запису з опційним автостартом по детекції мікрофона.

Кожні кілька секунд перевіряє через CoreAudio, чи якийсь процес використовує
мікрофон (Zoom/Meet/FaceTime стартував дзвінок). Якщо так і запис ще не йде —
показує діалог із вибором «Навушники» або «Динаміки». Вибір передається
у toggle_record.sh, тому другого popup немає.

Свідомо НЕ пише тихо: підтвердження = consent. Одне питання на один
«сеанс мікрофона» — відмовились, і до кінця дзвінка більше не турбує.

Коли MIC_AUTO_START=false, CoreAudio не опитується і popup на початку дзвінка
не показується. LaunchAgent залишається активним лише для ручних команд.

Запускається як LaunchAgent (див. README.md). Лог: logs/mic-autostart.log.
"""

import ctypes
import datetime
import json
import os
import secrets
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from pipeline_utils import load_dotenv

BASE = Path(__file__).parent
load_dotenv(BASE / ".env")

PID_FILE = BASE / ".record.pid"
TOGGLE = BASE / "toggle_record.sh"
REQUEST_DIR = BASE / ".control" / "requests"
REQUEST_MAX_AGE_SECONDS = 30
POLL_SECONDS = 4
CONTROL_POLL_SECONDS = 0.5
AUDIO_CONTROL_HOST = "127.0.0.1"
AUDIO_CONTROL_PORT = 43119
AUDIO_CONTROL_HEADER = "audio-control-v1"
DIALOG_TIMEOUT = 25  # с; нема відповіді = «ні»
MIC_AUTO_START = os.environ.get("MIC_AUTO_START", "false").lower() == "true"
AUDIO_PIPELINE_ENABLED = (
    os.environ.get("AUDIO_PIPELINE_ENABLED", "true").lower() == "true"
)

_ca = ctypes.CDLL(
    "/System/Library/Frameworks/CoreAudio.framework/Versions/A/CoreAudio")


def _fourcc(code: str) -> int:
    return int.from_bytes(code.encode("ascii"), "big")


class _PropAddr(ctypes.Structure):
    _fields_ = [("selector", ctypes.c_uint32),
                ("scope", ctypes.c_uint32),
                ("element", ctypes.c_uint32)]


_SYSTEM_OBJECT = 1
_SEL_DEVICES = _fourcc("dev#")          # kAudioHardwarePropertyDevices
_SEL_STREAM_CONF = _fourcc("slay")      # kAudioDevicePropertyStreamConfiguration
_SEL_RUNNING = _fourcc("gone")          # kAudioDevicePropertyDeviceIsRunningSomewhere
_SCOPE_GLOBAL = _fourcc("glob")
_SCOPE_INPUT = _fourcc("inpt")


def _get_property(obj_id: int, selector: int, scope: int) -> bytes | None:
    addr = _PropAddr(selector, scope, 0)
    size = ctypes.c_uint32(0)
    if _ca.AudioObjectGetPropertyDataSize(
            ctypes.c_uint32(obj_id), ctypes.byref(addr), 0, None,
            ctypes.byref(size)) != 0 or size.value == 0:
        return None
    buf = ctypes.create_string_buffer(size.value)
    if _ca.AudioObjectGetPropertyData(
            ctypes.c_uint32(obj_id), ctypes.byref(addr), 0, None,
            ctypes.byref(size), buf) != 0:
        return None
    return buf.raw[:size.value]


def mic_in_use() -> bool:
    """True, якщо будь-який вхідний аудіопристрій зараз використовується."""
    raw = _get_property(_SYSTEM_OBJECT, _SEL_DEVICES, _SCOPE_GLOBAL) or b""
    device_ids = [int.from_bytes(raw[i:i + 4], sys.byteorder)
                  for i in range(0, len(raw), 4)]
    for dev in device_ids:
        conf = _get_property(dev, _SEL_STREAM_CONF, _SCOPE_INPUT)
        if not conf or int.from_bytes(conf[:4], sys.byteorder) == 0:
            continue  # не вхідний пристрій
        running = _get_property(dev, _SEL_RUNNING, _SCOPE_GLOBAL)
        if running and int.from_bytes(running[:4], sys.byteorder):
            return True
    return False


def recording_active() -> bool:
    try:
        os.kill(int(PID_FILE.read_text().strip()), 0)
        return True
    except (FileNotFoundError, ValueError, ProcessLookupError, PermissionError):
        return False


def enqueue_control_request(command: str) -> Path:
    """Atomically enqueue a narrowly-scoped local recorder command."""
    if command not in {"toggle", "start"}:
        raise ValueError(f"unknown audio command: {command!r}")
    REQUEST_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    token = f"{time.time_ns()}.{os.getpid()}.{secrets.token_hex(6)}"
    temporary = REQUEST_DIR / f".{token}.tmp"
    request = REQUEST_DIR / f"{token}.request"
    descriptor = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL,
        0o600,
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(f"{command}\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, request)
    finally:
        temporary.unlink(missing_ok=True)
    return request


def audio_control_authorized(origin: str, control_header: str) -> bool:
    """Reject normal web-page requests; accept only the extension bridge."""
    if control_header != AUDIO_CONTROL_HEADER:
        return False
    return not origin or origin.startswith("chrome-extension://")


class AudioControlHandler(BaseHTTPRequestHandler):
    """Minimal loopback bridge from the Meet widget to the command queue."""

    server_version = "MeetingTranscriberAudio/1"

    def log_message(self, _format: str, *_args: object) -> None:
        return

    def _send_json(self, status: int, payload: dict[str, object]) -> None:
        encoded = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        origin = self.headers.get("Origin", "")
        if origin.startswith("chrome-extension://"):
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def do_OPTIONS(self) -> None:  # noqa: N802 — BaseHTTPRequestHandler API
        origin = self.headers.get("Origin", "")
        requested = self.headers.get("Access-Control-Request-Headers", "").lower()
        if (
            not origin.startswith("chrome-extension://")
            or "x-meeting-transcriber" not in requested
        ):
            self._send_json(403, {"ok": False})
            return
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", origin)
        self.send_header("Access-Control-Allow-Methods", "POST, OPTIONS")
        self.send_header(
            "Access-Control-Allow-Headers", "X-Meeting-Transcriber, Content-Type"
        )
        self.send_header("Access-Control-Max-Age", "600")
        self.end_headers()

    def do_POST(self) -> None:  # noqa: N802 — BaseHTTPRequestHandler API
        if self.path != "/recording/start":
            self._send_json(404, {"ok": False, "error": "not_found"})
            return
        if not audio_control_authorized(
            self.headers.get("Origin", ""),
            self.headers.get("X-Meeting-Transcriber", ""),
        ):
            self._send_json(403, {"ok": False, "error": "forbidden"})
            return
        try:
            content_length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            content_length = -1
        if content_length < 0 or content_length > 1024:
            self._send_json(413, {"ok": False, "error": "invalid_body"})
            return
        if content_length:
            self.rfile.read(content_length)
        enqueue_control_request("start")
        self._send_json(202, {"ok": True, "queued": True})


def start_audio_control_server() -> ThreadingHTTPServer | None:
    """Start the private loopback endpoint without blocking the queue worker."""
    try:
        server = ThreadingHTTPServer(
            (AUDIO_CONTROL_HOST, AUDIO_CONTROL_PORT), AudioControlHandler
        )
    except OSError as error:
        log(f"audio control недоступний: {error}")
        return None
    server.daemon_threads = True
    threading.Thread(
        target=server.serve_forever,
        name="audio-control",
        daemon=True,
    ).start()
    log(f"audio control: http://{AUDIO_CONTROL_HOST}:{AUDIO_CONTROL_PORT}")
    return server


def consume_control_requests() -> int:
    """Виконує валідні команди SwiftBar у стабільному launchd-контексті."""
    REQUEST_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    handled = 0
    for request in sorted(REQUEST_DIR.glob("*.request")):
        try:
            age = time.time() - request.stat().st_mtime
            command = request.read_text(encoding="utf-8").strip()
            request.unlink(missing_ok=True)
            if age > REQUEST_MAX_AGE_SECONDS:
                log(f"ігнорую застарілу control-команду ({age:.0f} с)")
                continue
            if command not in {"toggle", "start"}:
                log(f"ігнорую невідому control-команду: {command!r}")
                continue
            if command == "start" and recording_active():
                log("ручна команда → запис уже активний")
                handled += 1
                continue
            log("ручна команда → toggle")
            subprocess.run([str(TOGGLE)], check=False)
            handled += 1
        except FileNotFoundError:
            continue
        except Exception as error:
            log(f"помилка control-команди {request.name}: {error!r}")
    return handled


def ask_to_record() -> str | None:
    script = (
        f'display dialog "Схоже, почався дзвінок.\n\n'
        f'Навушники — один локальний спікер.\n'
        f'Динаміки — кілька людей біля мікрофона.\n\n'
        f'Учасники в курсі?" with title "Meeting Transcriber" '
        f'buttons {{"Пропустити", "Навушники", "Динаміки"}} '
        f'default button "Динаміки" '
        f'giving up after {DIALOG_TIMEOUT}'
    )
    out = subprocess.run(["osascript", "-e", script],
                         capture_output=True, text=True)
    result = out.stdout.strip()
    if "gave up:true" in result:
        return None
    if "button returned:Навушники" in result:
        return "--raw"
    if "button returned:Динаміки" in result:
        return "--speakers"
    return None


def log(msg: str) -> None:
    print(f"[{datetime.datetime.now():%H:%M:%S}] {msg}", flush=True)


def main() -> None:
    log("mic-watch стартував")
    if not AUDIO_PIPELINE_ENABLED:
        log("модуль audio вимкнено; фоновий процес не приймає команди")
        while True:
            time.sleep(3600)
    start_audio_control_server()
    if not MIC_AUTO_START:
        log("моніторинг мікрофона на паузі; очікую лише ручні команди")
    # Перезапуск сервісу посеред дзвінка не повинен показувати новий popup.
    # Спершу чекаємо, доки вже активний мікрофон звільниться.
    armed = MIC_AUTO_START and not mic_in_use()
    if MIC_AUTO_START and not armed:
        log("мікрофон уже активний → чекаю нового сеансу")
    next_mic_check = 0.0
    while True:
        try:
            if consume_control_requests():
                # Після ручного stop не пропонувати одразу стартувати знову,
                # якщо Zoom/Meet досі тримає мікрофон відкритим.
                armed = False
            if not MIC_AUTO_START:
                time.sleep(CONTROL_POLL_SECONDS)
                continue
            now = time.monotonic()
            if now < next_mic_check:
                time.sleep(CONTROL_POLL_SECONDS)
                continue
            next_mic_check = now + POLL_SECONDS
            active = mic_in_use()
            if not active:
                armed = True
            elif armed and not recording_active():
                armed = False
                log("мікрофон активний → питаю")
                mode = ask_to_record()
                if mode:
                    label = (
                        "Raw/динаміки + діаризація"
                        if mode == "--speakers"
                        else "Raw/навушники"
                    )
                    log(f"згода → старт запису ({label})")
                    subprocess.run([str(TOGGLE), mode], check=False)
                else:
                    log("відмова/таймаут — до кінця дзвінка не турбую")
        except Exception as e:
            log(f"помилка: {e!r}")
        time.sleep(CONTROL_POLL_SECONDS)


if __name__ == "__main__":
    main()
