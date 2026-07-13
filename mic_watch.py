#!/usr/bin/env python3
"""
v2: автостарт запису по детекції мікрофона.

Кожні кілька секунд перевіряє через CoreAudio, чи якийсь процес використовує
мікрофон (Zoom/Meet/FaceTime стартував дзвінок). Якщо так і запис ще не йде —
показує діалог із вибором «Навушники» або «Динаміки». Вибір передається
у toggle_record.sh, тому другого popup немає.

Свідомо НЕ пише тихо: підтвердження = consent. Одне питання на один
«сеанс мікрофона» — відмовились, і до кінця дзвінка більше не турбує.

    Запускається як LaunchAgent (див. README.md). Лог: logs/mic-autostart.log.
"""

import ctypes
import datetime
import os
import subprocess
import sys
import time
from pathlib import Path

BASE = Path(__file__).parent
PID_FILE = BASE / ".record.pid"
TOGGLE = BASE / "toggle_record.sh"
REQUEST_DIR = BASE / ".control" / "requests"
REQUEST_MAX_AGE_SECONDS = 30
POLL_SECONDS = 4
CONTROL_POLL_SECONDS = 0.5
DIALOG_TIMEOUT = 25  # с; нема відповіді = «ні»

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
            if command != "toggle":
                log(f"ігнорую невідому control-команду: {command!r}")
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
        return "--aec"
    return None


def log(msg: str) -> None:
    print(f"[{datetime.datetime.now():%H:%M:%S}] {msg}", flush=True)


def main() -> None:
    log("mic-watch стартував")
    # Перезапуск сервісу посеред дзвінка не повинен показувати новий popup.
    # Спершу чекаємо, доки вже активний мікрофон звільниться.
    armed = not mic_in_use()  # одне питання на один новий сеанс мікрофона
    if not armed:
        log("мікрофон уже активний → чекаю нового сеансу")
    next_mic_check = 0.0
    while True:
        try:
            if consume_control_requests():
                # Після ручного stop не пропонувати одразу стартувати знову,
                # якщо Zoom/Meet досі тримає мікрофон відкритим.
                armed = False
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
                    label = "AEC/динаміки" if mode == "--aec" else "Raw/навушники"
                    log(f"згода → старт запису ({label})")
                    subprocess.run([str(TOGGLE), mode], check=False)
                else:
                    log("відмова/таймаут — до кінця дзвінка не турбую")
        except Exception as e:
            log(f"помилка: {e!r}")
        time.sleep(CONTROL_POLL_SECONDS)


if __name__ == "__main__":
    main()
