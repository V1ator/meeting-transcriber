"""Спільні безпечні операції для recording/transcription pipeline."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def load_dotenv(path: Path, *, override: bool = False) -> None:
    """Мінімальний parser KEY=VALUE без виконання shell-коду."""
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.split("#", 1)[0].strip().strip("'\"")
        if key and value and (override or key not in os.environ):
            os.environ[key] = value


def atomic_write_text(path: Path, text: str, *, mode: int | None = None) -> None:
    """Записує файл через fsync + atomic rename в межах тієї самої директорії."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp",
                                    dir=path.parent)
    tmp = Path(tmp_name)
    try:
        if mode is not None:
            os.fchmod(fd, mode)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        tmp.replace(path)
    except BaseException:
        try:
            os.close(fd)
        except OSError:
            pass
        tmp.unlink(missing_ok=True)
        raise


def atomic_write_json(path: Path, data: dict[str, Any], *, mode: int | None = None) -> None:
    atomic_write_text(path, json.dumps(data, ensure_ascii=False, indent=2) + "\n",
                      mode=mode)


def read_json(path: Path, default: Any = None) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return default


def file_sha256(path: Path, chunk_size: int = 4 * 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        while chunk := fh.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()


def file_fingerprint(path: Path, *, with_hash: bool = True) -> dict[str, Any]:
    stat = path.stat()
    result: dict[str, Any] = {
        "size": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
    }
    if with_hash:
        result["sha256"] = file_sha256(path)
    return result


def audio_info(path: Path) -> dict[str, Any]:
    import soundfile as sf

    info = sf.info(str(path))
    duration = info.frames / info.samplerate if info.samplerate else 0.0
    return {
        "file": path.name,
        "frames": info.frames,
        "sample_rate": info.samplerate,
        "channels": info.channels,
        "duration": round(duration, 6),
        "format": info.format,
        "subtype": info.subtype,
        "size": path.stat().st_size,
    }


def audio_signal_info(path: Path) -> dict[str, float]:
    """Streaming RMS/peak dBFS без завантаження всього WAV у пам'ять."""
    import math
    import numpy as np
    import soundfile as sf

    sum_squares = 0.0
    sample_count = 0
    peak = 0.0
    with sf.SoundFile(path) as source:
        for block in source.blocks(blocksize=65_536, dtype="float32", always_2d=True):
            values = block.astype("float64")
            sum_squares += float(np.sum(values * values))
            sample_count += values.size
            if values.size:
                peak = max(peak, float(np.max(np.abs(values))))
    rms = math.sqrt(sum_squares / sample_count) if sample_count else 0.0
    to_dbfs = lambda value: 20.0 * math.log10(max(value, 1e-12))
    return {"rms_dbfs": round(to_dbfs(rms), 2), "peak_dbfs": round(to_dbfs(peak), 2)}


def find_ffmpeg() -> str:
    for candidate in (
        shutil.which("ffmpeg"),
        "/opt/homebrew/bin/ffmpeg",
        "/usr/local/bin/ffmpeg",
    ):
        if candidate and Path(candidate).exists():
            return str(candidate)
    raise FileNotFoundError("ffmpeg не знайдено")


def normalized_audio(source: Path, target: Path, *, sample_rate: int = 16_000) -> Path:
    """Створює/оновлює атомарну mono PCM processing-копію аудіо."""
    meta_path = target.with_suffix(target.suffix + ".meta.json")
    source_fp = file_fingerprint(source, with_hash=False)
    expected = {"source": source_fp, "sample_rate": sample_rate, "channels": 1}
    if target.exists() and read_json(meta_path) == expected:
        try:
            info = audio_info(target)
            if info["sample_rate"] == sample_rate and info["channels"] == 1:
                return target
        except Exception:
            pass

    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{target.stem}.", suffix=".wav",
                                    dir=target.parent)
    os.close(fd)
    tmp = Path(tmp_name)
    try:
        subprocess.run(
            [find_ffmpeg(), "-hide_banner", "-loglevel", "error", "-y",
             "-i", str(source), "-vn", "-ac", "1", "-ar", str(sample_rate),
             "-c:a", "pcm_s16le", str(tmp)],
            check=True,
            capture_output=True,
        )
        info = audio_info(tmp)
        if info["duration"] <= 0 or info["channels"] != 1:
            raise ValueError(f"Некоректний normalized WAV: {info}")
        tmp.replace(target)
        atomic_write_json(meta_path, expected)
        return target
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def update_manifest(path: Path, **changes: Any) -> dict[str, Any]:
    data = read_json(path, {})
    if not isinstance(data, dict):
        data = {}
    data.update(changes)
    data["updated_at"] = utc_now()
    atomic_write_json(path, data)
    return data
