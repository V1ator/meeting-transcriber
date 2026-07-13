#!/usr/bin/env python3
"""Двотрековий запис із atomic manifest, silence monitor і safe stop."""

from __future__ import annotations

import array
import ctypes
import datetime
import math
import os
import queue
import signal
import subprocess
import sys
import threading
import time
import uuid
import warnings
from pathlib import Path
from typing import Any

from pipeline_utils import atomic_write_json, audio_info, find_ffmpeg, load_dotenv, utc_now

BASE = Path(__file__).parent
load_dotenv(BASE / ".env")
RECORDINGS_DIR = BASE / "recordings"
PID_FILE = BASE / ".record.pid"
SAMPLE_RATE = 48_000
CHANNELS = 1
MAX_RECORD_SECONDS = int(os.environ.get("MAX_RECORD_SECONDS", "21600"))
SILENCE_POPUP = os.environ.get("SILENCE_POPUP", "true").lower() == "true"
SILENCE_SECONDS = float(os.environ.get("SILENCE_SECONDS", "90"))
SILENCE_MIN_RECORD_SECONDS = float(
    os.environ.get("SILENCE_MIN_RECORD_SECONDS", "120")
)
SILENCE_REPEAT_SECONDS = float(os.environ.get("SILENCE_REPEAT_SECONDS", "600"))
SILENCE_DIALOG_TIMEOUT = int(os.environ.get("SILENCE_DIALOG_TIMEOUT", "30"))
MIC_ACTIVITY_DBFS = float(os.environ.get("MIC_ACTIVITY_DBFS", "-42"))
SYSTEM_ACTIVITY_DBFS = float(os.environ.get("SYSTEM_ACTIVITY_DBFS", "-50"))

STOP = False
STOP_REASON: str | None = None
STOP_REQUESTED_AT: float | None = None
MIC_FIRST_FRAME_AT: float | None = None
MIC_DROPPED_BLOCKS = 0


def _request_stop(reason: str) -> None:
    global STOP, STOP_REASON, STOP_REQUESTED_AT
    STOP = True
    STOP_REASON = STOP_REASON or reason
    STOP_REQUESTED_AT = STOP_REQUESTED_AT or time.monotonic()


def _on_signal(signum, frame):
    name = "sigint" if signum == signal.SIGINT else "sigterm"
    _request_stop(name)


def _session_id() -> str:
    base = datetime.datetime.now().strftime("%Y-%m-%d_%H%M%S")
    candidate = base
    while any(RECORDINGS_DIR.glob(f"{candidate}*")):
        candidate = f"{base}_{uuid.uuid4().hex[:6]}"
    return candidate


def ensure_microphone_permission(timeout: float = 30.0) -> None:
    """Перевіряє/запитує macOS TCC до створення порожньої recording session."""
    import AVFoundation as AV

    status = int(AV.AVCaptureDevice.authorizationStatusForMediaType_(AV.AVMediaTypeAudio))
    if status == int(AV.AVAuthorizationStatusNotDetermined):
        finished = threading.Event()
        granted = False

        def on_permission(result):
            nonlocal granted
            granted = bool(result)
            finished.set()

        AV.AVCaptureDevice.requestAccessForMediaType_completionHandler_(
            AV.AVMediaTypeAudio, on_permission
        )
        if not finished.wait(timeout):
            raise PermissionError(
                "MICROPHONE_PERMISSION_DENIED: macOS не завершила запит дозволу"
            )
        if granted:
            return
        status = int(AV.AVAuthorizationStatusDenied)
    if status != int(AV.AVAuthorizationStatusAuthorized):
        raise PermissionError(
            "MICROPHONE_PERMISSION_DENIED: відкрийте System Settings → "
            "Privacy & Security → Microphone і дозвольте застосунок-лаунчер "
            "(Raycast, Shortcuts, SwiftBar або Terminal)"
        )


def _dbfs(rms: float) -> float:
    return 20.0 * math.log10(max(rms, 1e-12))


def pcm_rms_dbfs(data: bytes, sample_type: str, bits_per_sample: int,
                 *, max_samples: int = 4096) -> float:
    """RMS dBFS для interleaved PCM callback buffer без зовнішніх залежностей."""
    if not data or bits_per_sample not in {8, 16, 24, 32, 64}:
        return float("-inf")
    width = bits_per_sample // 8
    count = len(data) // width
    if count <= 0:
        return float("-inf")
    stride = max(1, count // max_samples)

    if sample_type == "float" and bits_per_sample in {32, 64}:
        typecode = "f" if bits_per_sample == 32 else "d"
        values = array.array(typecode)
        values.frombytes(data[:count * width])
        samples = (float(values[index]) for index in range(0, len(values), stride))
    elif sample_type == "signed_integer" and bits_per_sample in {8, 16, 32}:
        typecode = {8: "b", 16: "h", 32: "i"}[bits_per_sample]
        scale = float(1 << (bits_per_sample - 1))
        values = array.array(typecode)
        values.frombytes(data[:count * width])
        samples = (float(values[index]) / scale
                   for index in range(0, len(values), stride))
    elif sample_type == "signed_integer" and bits_per_sample == 24:
        scale = float(1 << 23)
        byte_stride = stride * 3
        samples = (
            int.from_bytes(data[index:index + 3], "little", signed=True) / scale
            for index in range(0, count * 3, byte_stride)
        )
    else:
        return float("-inf")

    squares = [sample * sample for sample in samples if math.isfinite(sample)]
    if not squares:
        return float("-inf")
    return _dbfs(math.sqrt(sum(squares) / len(squares)))


def ndarray_rms_dbfs(indata) -> float:
    """RMS для sounddevice ndarray із subsampling, придатний для callback."""
    samples = indata.reshape(-1)
    if samples.size == 0:
        return float("-inf")
    stride = max(1, samples.size // 2048)
    selected = samples[::stride]
    rms = float((selected.dot(selected) / selected.size) ** 0.5)
    return _dbfs(rms)


def av_buffer_rms_dbfs(buffer) -> float:
    """Максимальний RMS усіх Voice Processing channels з AVAudioPCMBuffer."""
    frame_count = int(buffer.frameLength())
    if frame_count <= 0:
        return float("-inf")
    stride = max(1, frame_count // 1024)
    fmt = buffer.format()
    common_format = int(fmt.commonFormat())
    channel_count = int(fmt.channelCount())
    with warnings.catch_warnings():
        warnings.filterwarnings("ignore", message=r"PyObjCPointer created:.*")
        if common_format == 1:
            pointer = buffer.floatChannelData()
            sample_type, scale = ctypes.c_float, 1.0
        elif common_format == 3:
            pointer = buffer.int16ChannelData()
            sample_type, scale = ctypes.c_int16, 32768.0
        elif common_format == 4:
            pointer = buffer.int32ChannelData()
            sample_type, scale = ctypes.c_int32, 2147483648.0
        else:
            return float("-inf")
    if pointer is None:
        return float("-inf")
    channels = ctypes.cast(
        pointer.pointerAsInteger,
        ctypes.POINTER(ctypes.POINTER(sample_type)),
    )
    loudest = float("-inf")
    for channel_index in range(channel_count):
        channel = channels[channel_index]
        squares = [
            (float(channel[index]) / scale) ** 2
            for index in range(0, frame_count, stride)
        ]
        if squares:
            loudest = max(loudest, _dbfs(math.sqrt(sum(squares) / len(squares))))
    return loudest


class AudioActivityMonitor:
    """Потокобезпечний rolling state активності двох доріжок."""

    def __init__(self, *, enabled: bool = SILENCE_POPUP,
                 silence_seconds: float = SILENCE_SECONDS,
                 min_record_seconds: float = SILENCE_MIN_RECORD_SECONDS,
                 repeat_seconds: float = SILENCE_REPEAT_SECONDS,
                 mic_threshold: float = MIC_ACTIVITY_DBFS,
                 system_threshold: float = SYSTEM_ACTIVITY_DBFS,
                 started_at: float | None = None) -> None:
        self.enabled = enabled
        self.silence_seconds = silence_seconds
        self.min_record_seconds = min_record_seconds
        self.repeat_seconds = repeat_seconds
        self.mic_threshold = mic_threshold
        self.system_threshold = system_threshold
        self.started_at = time.monotonic() if started_at is None else started_at
        self._lock = threading.Lock()
        self._mic_seen = False
        self._system_seen = False
        self._mic_last_activity = self.started_at
        self._system_last_activity = self.started_at
        self._last_prompt: float | None = None
        self._mic_dbfs = float("-inf")
        self._system_dbfs = float("-inf")
        self._prompt_count = 0

    def reset_start(self, started_at: float) -> None:
        """Починає grace period після фактичного запуску system tap."""
        with self._lock:
            self.started_at = started_at
            self._mic_last_activity = started_at
            self._system_last_activity = started_at
            self._last_prompt = None

    def observe_mic(self, dbfs: float, *, now: float | None = None) -> None:
        current = time.monotonic() if now is None else now
        with self._lock:
            self._mic_seen = True
            self._mic_dbfs = dbfs
            if dbfs >= self.mic_threshold:
                self._mic_last_activity = current

    def observe_system(self, dbfs: float, *, now: float | None = None) -> None:
        current = time.monotonic() if now is None else now
        with self._lock:
            self._system_seen = True
            self._system_dbfs = dbfs
            if dbfs >= self.system_threshold:
                self._system_last_activity = current

    def observe_system_buffer(self, buffer) -> None:
        try:
            fmt = buffer.format
            self.observe_system(pcm_rms_dbfs(
                buffer.data, fmt.sample_type, fmt.bits_per_sample
            ))
        except Exception as exc:
            # Meter ніколи не повинен зірвати system recording callback.
            print(f"[silence] system meter error: {exc.__class__.__name__}",
                  file=sys.stderr)

    def should_prompt(self, *, now: float | None = None) -> bool:
        current = time.monotonic() if now is None else now
        with self._lock:
            if not self.enabled or not self._mic_seen or not self._system_seen:
                return False
            if current - self.started_at < self.min_record_seconds:
                return False
            if current - self._mic_last_activity < self.silence_seconds:
                return False
            if current - self._system_last_activity < self.silence_seconds:
                return False
            if (self._last_prompt is not None
                    and current - self._last_prompt < self.repeat_seconds):
                return False
            return True

    def mark_prompted(self, *, now: float | None = None) -> None:
        current = time.monotonic() if now is None else now
        with self._lock:
            self._last_prompt = current
            self._prompt_count += 1

    def snapshot(self) -> dict[str, Any]:
        def finite_level(value: float) -> float | None:
            return round(value, 2) if math.isfinite(value) else None

        with self._lock:
            return {
                "silence_prompts": self._prompt_count,
                "last_mic_dbfs": finite_level(self._mic_dbfs),
                "last_system_dbfs": finite_level(self._system_dbfs),
                "mic_activity_threshold_dbfs": self.mic_threshold,
                "system_activity_threshold_dbfs": self.system_threshold,
            }


def ask_finish_after_silence(stop_event: threading.Event) -> bool:
    script = (
        f'display dialog "На обох доріжках немає аудіо вже '
        f'{int(SILENCE_SECONDS)} секунд.\\n\\nЗавершити запис?" '
        f'with title "Meeting Transcriber" '
        f'buttons {{"Продовжити", "Завершити запис"}} '
        f'default button "Завершити запис" giving up after {SILENCE_DIALOG_TIMEOUT}'
    )
    process = subprocess.Popen(
        ["/usr/bin/osascript", "-e", script],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    deadline = time.monotonic() + SILENCE_DIALOG_TIMEOUT + 5
    while process.poll() is None:
        if stop_event.is_set() or STOP or time.monotonic() >= deadline:
            process.terminate()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
            return False
        time.sleep(0.2)
    output = process.stdout.read().strip() if process.stdout else ""
    return ("button returned:Завершити запис" in output
            and "gave up:true" not in output)


class RecordingSupervisor:
    def __init__(self, monitor: AudioActivityMonitor,
                 *, max_seconds: float = MAX_RECORD_SECONDS) -> None:
        self.monitor = monitor
        self.max_seconds = max_seconds
        self._stop_event = threading.Event()
        self._thread = threading.Thread(target=self._run, name="record-supervisor",
                                        daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        self._thread.join(timeout=3)

    def _run(self) -> None:
        while not self._stop_event.wait(0.5):
            now = time.monotonic()
            if self.max_seconds > 0 and now - self.monitor.started_at >= self.max_seconds:
                print(f"[supervisor] ліміт {self.max_seconds:.0f} с → safe stop")
                _request_stop("max-duration")
                return
            if self.monitor.should_prompt(now=now):
                self.monitor.mark_prompted(now=now)
                print(f"[silence] обидві доріжки тихі {SILENCE_SECONDS:.0f} с → popup")
                if ask_finish_after_silence(self._stop_event):
                    print("[silence] користувач підтвердив завершення")
                    _request_stop("silence-confirmed")
                    return
                print("[silence] запис продовжується")


def start_system_audio(sys_path: Path, monitor: AudioActivityMonitor):
    """System CoreAudio tap + WAV + streaming level callback в одному session."""
    from catap import record_system_audio

    session = record_system_audio(
        output_path=sys_path,
        on_buffer=monitor.observe_system_buffer,
        max_pending_buffers=256,
    )
    session.start()
    started_at = time.monotonic()
    monitor.reset_start(started_at)
    return session, started_at


def record_mic_raw(mic_path: Path, system_session,
                   monitor: AudioActivityMonitor) -> None:
    import sounddevice as sd
    import soundfile as sf

    try:
        input_device = sd.query_devices(kind="input")
    except Exception as exc:
        raise RuntimeError(
            "MICROPHONE_DEVICE_MISSING: macOS не надає default input device"
        ) from exc
    print(f"[mic] Raw input: {input_device['name']}")

    audio_q: queue.Queue = queue.Queue(maxsize=256)

    def on_audio(indata, frames, time_info, status):
        global MIC_FIRST_FRAME_AT, MIC_DROPPED_BLOCKS
        MIC_FIRST_FRAME_AT = MIC_FIRST_FRAME_AT or time.monotonic()
        try:
            monitor.observe_mic(ndarray_rms_dbfs(indata))
        except Exception as exc:
            print(f"[silence] mic meter error: {exc.__class__.__name__}",
                  file=sys.stderr)
        if status:
            print(f"[mic] {status}", file=sys.stderr)
        try:
            audio_q.put_nowait(indata.copy())
        except queue.Full:
            MIC_DROPPED_BLOCKS += 1

    with sf.SoundFile(mic_path, "w", samplerate=SAMPLE_RATE,
                      channels=CHANNELS) as output:
        with sd.InputStream(samplerate=SAMPLE_RATE, channels=CHANNELS,
                            callback=on_audio):
            while not STOP:
                if not system_session.is_recording:
                    raise RuntimeError("Втрачено системну доріжку під час запису")
                try:
                    output.write(audio_q.get(timeout=0.5))
                except queue.Empty:
                    continue
            while not audio_q.empty():
                output.write(audio_q.get_nowait())


def _loudest_file_channel(path: Path) -> tuple[int, list[float]]:
    import numpy as np
    import soundfile as sf

    with sf.SoundFile(path) as source:
        sums = np.zeros(source.channels, dtype="float64")
        samples = 0
        for block in source.blocks(blocksize=65_536, dtype="float32", always_2d=True):
            sums += np.sum(block.astype("float64") ** 2, axis=0)
            samples += len(block)
    levels = [
        _dbfs(math.sqrt(float(value) / max(1, samples)))
        for value in sums
    ]
    return max(range(len(levels)), key=levels.__getitem__), levels


def to_mono(path: Path) -> dict[str, Any]:
    """Обирає найгучніший Voice Processing channel і зводить його в mono."""
    ffmpeg = find_ffmpeg()
    tmp = path.with_name(f".{path.stem}.mono.wav")
    selected, levels = _loudest_file_channel(path)
    try:
        subprocess.run(
            [ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-i", str(path),
             "-af", f"pan=mono|c0=c{selected}", "-c:a", "pcm_s16le", str(tmp)],
            check=True,
            capture_output=True,
        )
        tmp.replace(path)
        print(f"[mic] зведено в моно (канал {selected}, {levels[selected]:.1f} dBFS)")
        return {
            "aec_selected_channel": selected,
            "aec_channel_levels_dbfs": [round(level, 2) for level in levels],
        }
    except subprocess.CalledProcessError as exc:
        tmp.unlink(missing_ok=True)
        detail = exc.stderr.decode(errors="replace")[-300:]
        raise RuntimeError(f"Не вдалося звести AEC WAV у моно: {detail}") from exc


def _res_err(result):
    if isinstance(result, tuple):
        return result[0], (result[1] if len(result) > 1 else None)
    return result, None


def record_mic_aec(mic_path: Path, system_session,
                   monitor: AudioActivityMonitor) -> None:
    from Foundation import NSURL
    try:
        import AVFAudio as AV
    except ImportError:
        import AVFoundation as AV

    engine = AV.AVAudioEngine.alloc().init()
    node = engine.inputNode()
    ok, err = _res_err(node.setVoiceProcessingEnabled_error_(True, None))
    if not ok:
        raise RuntimeError(f"Voice Processing не ввімкнувся: {err}")

    def apply_min_ducking(when: str) -> None:
        try:
            config = AV.AVAudioVoiceProcessingOtherAudioDuckingConfiguration(
                enableAdvancedDucking=False,
                duckingLevel=AV.AVAudioVoiceProcessingOtherAudioDuckingLevelMin,
            )
            node.setVoiceProcessingOtherAudioDuckingConfiguration_(config)
            print(f"[mic] ducking → мінімум ({when})")
        except Exception as exc:
            print(f"[mic] ⚠️ ducking не налаштувався ({when}, "
                  f"{exc.__class__.__name__})", file=sys.stderr)

    apply_min_ducking("до старту")
    fmt = node.outputFormatForBus_(0)
    url = NSURL.fileURLWithPath_(str(mic_path))
    audio_file, err = _res_err(
        AV.AVAudioFile.alloc().initForWriting_settings_error_(url, fmt.settings(), None)
    )
    if audio_file is None:
        raise RuntimeError(f"Не створився вихідний файл: {err}")

    def tap(buffer, when):
        global MIC_FIRST_FRAME_AT
        MIC_FIRST_FRAME_AT = MIC_FIRST_FRAME_AT or time.monotonic()
        try:
            monitor.observe_mic(av_buffer_rms_dbfs(buffer))
        except Exception as exc:
            print(f"[silence] AEC meter error: {exc.__class__.__name__}",
                  file=sys.stderr)
        audio_file.writeFromBuffer_error_(buffer, None)

    node.installTapOnBus_bufferSize_format_block_(0, 4096, fmt, tap)
    ok, err = _res_err(engine.startAndReturnError_(None))
    if not ok:
        raise RuntimeError(f"AVAudioEngine не стартував: {err}")
    apply_min_ducking("після старту")
    print(f"[mic] AEC активний (формат: {int(fmt.sampleRate())} Hz, "
          f"{int(fmt.channelCount())} ch)")

    try:
        while not STOP:
            time.sleep(0.5)
            if not system_session.is_recording:
                raise RuntimeError("Втрачено системну доріжку під час запису")
    finally:
        node.removeTapOnBus_(0)
        engine.stop()
        del audio_file


def _cleanup_own_pid() -> None:
    try:
        if int(PID_FILE.read_text().strip()) == os.getpid():
            PID_FILE.unlink(missing_ok=True)
    except (FileNotFoundError, ValueError, OSError):
        pass


def resolve_aec_mode(argv: list[str] | None = None) -> bool:
    """CLI mode має пріоритет над fallback RECORD_AEC із .env."""
    args = sys.argv[1:] if argv is None else argv
    if "--aec" in args and "--raw" in args:
        raise ValueError("Оберіть лише один режим мікрофона: --aec або --raw")
    unknown = [arg for arg in args if arg not in {"--aec", "--raw"}]
    if unknown:
        raise ValueError(f"Невідомі аргументи: {' '.join(unknown)}")
    if "--aec" in args:
        return True
    if "--raw" in args:
        return False
    return os.environ.get("RECORD_AEC", "false").lower() == "true"


def main() -> None:
    global STOP
    use_aec = resolve_aec_mode()
    ensure_microphone_permission()
    RECORDINGS_DIR.mkdir(exist_ok=True)
    session = _session_id()
    mic_final = RECORDINGS_DIR / f"{session}_mic.wav"
    sys_final = RECORDINGS_DIR / f"{session}_sys.wav"
    mic_partial = RECORDINGS_DIR / f".{session}_mic.partial.wav"
    sys_partial = RECORDINGS_DIR / f".{session}_sys.partial.wav"
    manifest_path = RECORDINGS_DIR / f"{session}.json"
    started_wall = datetime.datetime.now().astimezone()
    started_monotonic = time.monotonic()
    monitor = AudioActivityMonitor(started_at=started_monotonic)
    supervisor = RecordingSupervisor(monitor)

    manifest = {
        "schema_version": 1,
        "session": session,
        "status": "recording",
        "created_at": utc_now(),
        "started_at": started_wall.isoformat(timespec="seconds"),
        "recording": {
            "aec": use_aec,
            "mic_diarization": use_aec,
            "mic_speaker_mode": "multiple" if use_aec else "single",
            "pid": os.getpid(),
            "silence_popup": SILENCE_POPUP,
        },
    }
    atomic_write_json(manifest_path, manifest)

    print(f"Сесія: {session} (мікрофон: {'AEC' if use_aec else 'сирий'})")
    print(f"  мікрофон  -> {mic_final}")
    print(f"  системний -> {sys_final}")
    signal.signal(signal.SIGINT, _on_signal)
    signal.signal(signal.SIGTERM, _on_signal)

    system_session = None
    sys_started_at: float | None = None
    error: BaseException | None = None
    try:
        system_session, sys_started_at = start_system_audio(sys_partial, monitor)
        supervisor.start()
        print("Запис почато. Ctrl+C — стоп.")
        if SILENCE_POPUP:
            print(f"Silence monitor: popup після {SILENCE_SECONDS:.0f} с тиші обох доріжок.")
        if use_aec:
            record_mic_aec(mic_partial, system_session, monitor)
        else:
            print("Нагадування: без навушників використовуйте --aec.")
            record_mic_raw(mic_partial, system_session, monitor)
    except BaseException as exc:
        error = exc
    finally:
        STOP = True
        supervisor.stop()
        if system_session is not None:
            try:
                system_session.close()
            except BaseException as exc:
                if error is None:
                    error = exc
                else:
                    print(f"[sys] secondary close error: {exc}", file=sys.stderr)

    try:
        if error is not None:
            raise error
        if use_aec:
            # System tap уже закритий: конвертація більше не додає sync drift.
            aec_quality = to_mono(mic_partial)
        else:
            aec_quality = {}
        mic_meta = audio_info(mic_partial)
        sys_meta = audio_info(sys_partial)
        if min(mic_meta["duration"], sys_meta["duration"]) <= 0.1:
            raise RuntimeError("Одна з доріжок порожня або не закрилася коректно")

        mic_partial.replace(mic_final)
        sys_partial.replace(sys_final)
        mic_meta = audio_info(mic_final)
        sys_meta = audio_info(sys_final)
        assert sys_started_at is not None
        start_offset = max(0.0, (MIC_FIRST_FRAME_AT or sys_started_at) - sys_started_at)
        target_mic_span = max(0.1, sys_meta["duration"] - start_offset)
        mic_scale = target_mic_span / mic_meta["duration"]
        manifest.update({
            "status": "recorded",
            "completed_at": utc_now(),
            "stop_reason": STOP_REASON or "normal",
            "tracks": {"mic": mic_meta, "sys": sys_meta},
            "timing": {
                "reference": "sys",
                "mic_start_offset_seconds": round(start_offset, 6),
                "mic_time_scale": round(mic_scale, 9),
                "method": "first-frame offset + common-end affine correction",
            },
            "quality": {
                "mic_dropped_blocks": MIC_DROPPED_BLOCKS,
                **aec_quality,
                **monitor.snapshot(),
            },
        })
        atomic_write_json(manifest_path, manifest)
        elapsed = datetime.datetime.now().astimezone() - started_wall
        print(f"\nЗапис завершено атомарно. Тривалість: {elapsed}.")
        print(f"  причина: {STOP_REASON or 'normal'}")
        print(f"  sync: mic offset={start_offset:.3f}s, scale={mic_scale:.7f}")
        for meta in (mic_meta, sys_meta):
            print(f"  {meta['file']}: {meta['duration']:.1f}s, "
                  f"{meta['channels']}ch, {meta['sample_rate']}Hz")
    except BaseException as exc:
        manifest.update({
            "status": "recording_failed",
            "completed_at": utc_now(),
            "stop_reason": STOP_REASON,
            "error": f"{exc.__class__.__name__}: {exc}",
        })
        atomic_write_json(manifest_path, manifest)
        print(f"Запис не завершено: {exc}", file=sys.stderr)
        raise
    finally:
        _cleanup_own_pid()


if __name__ == "__main__":
    main()
