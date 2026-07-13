#!/usr/bin/env python3
"""Транскрипція двох доріжок, діаризація, quality gates і sync correction."""

from __future__ import annotations

import argparse
import collections
import datetime
import difflib
import os
import re
import warnings
from pathlib import Path
from typing import Any

from pipeline_utils import (
    atomic_write_json,
    atomic_write_text,
    audio_info,
    file_fingerprint,
    load_dotenv,
    normalized_audio,
    read_json,
    utc_now,
)

BASE = Path(__file__).parent
load_dotenv(BASE / ".env")
os.environ.setdefault("PYTORCH_ENABLE_MPS_FALLBACK", "1")
TRANSCRIPTS_DIR = BASE / "transcripts"
LANGUAGE = os.environ.get("TRANSCRIBE_LANGUAGE", "uk").strip().lower()
DIARIZE_MODEL = os.environ.get(
    "DIARIZE_MODEL", "pyannote/speaker-diarization-community-1"
)
MLX_MODEL = os.environ.get("MLX_MODEL", "mlx-community/whisper-large-v3-mlx")
DIARIZE_DEVICE = os.environ.get("DIARIZE_DEVICE", "auto").strip().lower()
DEDUP_MIC = os.environ.get("DEDUP_MIC", "true").lower() == "true"
MAX_NO_SPEECH = float(os.environ.get("MAX_NO_SPEECH", "0.60"))
MIN_AVG_LOGPROB = float(os.environ.get("MIN_AVG_LOGPROB", "-1.00"))
PERIODIC_REPEAT_MIN_COUNT = 4
PERIODIC_REPEAT_MIN_GAP = 20.0
PERIODIC_REPEAT_MAX_GAP = 40.0
GENERIC_HALLUCINATIONS = {"дякую"}
_DIARIZATION_PIPELINE = None
_DIARIZATION_DEVICE: str | None = None


def _language_arg() -> str | None:
    return None if LANGUAGE in {"", "auto", "none"} else LANGUAGE


def _cache_valid(path: Path, expected: dict[str, Any]) -> bool:
    data = read_json(path)
    return (isinstance(data, dict) and data.get("_meta") == expected
            and isinstance(data.get("segments"), list))


def _as_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def sanitize_asr_segments(raw_segments: list[dict], duration: float) -> tuple[list[dict], int]:
    """Відсікає явну тишу/галюцинації й обрізає timestamps до меж WAV."""
    clean: list[dict] = []
    dropped = 0
    for raw in raw_segments:
        text = str(raw.get("text", "")).strip()
        start = max(0.0, _as_float(raw.get("start")))
        end = min(duration, _as_float(raw.get("end"), start))
        no_speech = _as_float(raw.get("no_speech_prob"), 0.0)
        avg_logprob = _as_float(raw.get("avg_logprob"), 0.0)
        compression = _as_float(raw.get("compression_ratio"), 0.0)
        strong_silence = no_speech >= 0.75
        low_confidence_silence = no_speech > MAX_NO_SPEECH and avg_logprob < -0.5
        pathological_text = compression > 2.8 and avg_logprob < MIN_AVG_LOGPROB
        if (not text or start >= duration or end <= start
                or strong_silence or low_confidence_silence or pathological_text):
            dropped += 1
            continue
        segment = {
            "start": start,
            "end": end,
            "text": text,
        }
        for key in ("speaker", "avg_logprob", "no_speech_prob", "compression_ratio"):
            if key in raw:
                segment[key] = raw[key]
        words = []
        for word in raw.get("words") or []:
            w_start = max(start, _as_float(word.get("start"), start))
            w_end = min(end, _as_float(word.get("end"), w_start))
            if w_end > w_start and str(word.get("word", "")).strip():
                item = dict(word)
                item["start"], item["end"] = w_start, w_end
                words.append(item)
        if words:
            segment["words"] = words
        clean.append(segment)
    return clean, dropped


def _normalized_phrase(text: str) -> str:
    return re.sub(r"[^\w]+", " ", text.casefold()).strip()


def drop_periodic_repetitions(segments: list[dict]) -> tuple[list[dict], int]:
    """Прибирає Whisper loops: однакова фраза кожні ~30 с чотири+ рази."""
    occurrences: dict[str, list[tuple[int, float]]] = collections.defaultdict(list)
    for index, segment in enumerate(segments):
        phrase = _normalized_phrase(str(segment.get("text", "")))
        if phrase:
            occurrences[phrase].append((index, _as_float(segment.get("start"))))

    drop: set[int] = set()
    for phrase, positions in occurrences.items():
        if len(positions) < PERIODIC_REPEAT_MIN_COUNT:
            continue
        lengths = [1] * len(positions)
        previous: list[int | None] = [None] * len(positions)
        for current in range(len(positions)):
            for candidate in range(current - 1, -1, -1):
                gap = positions[current][1] - positions[candidate][1]
                if gap > PERIODIC_REPEAT_MAX_GAP:
                    break
                if (gap >= PERIODIC_REPEAT_MIN_GAP
                        and lengths[candidate] + 1 > lengths[current]):
                    lengths[current] = lengths[candidate] + 1
                    previous[current] = candidate

        periodic: set[int] = set()
        for current, length in enumerate(lengths):
            if length < PERIODIC_REPEAT_MIN_COUNT:
                continue
            cursor: int | None = current
            while cursor is not None:
                periodic.add(positions[cursor][0])
                cursor = previous[cursor]
        if not periodic:
            continue
        if phrase in GENERIC_HALLUCINATIONS:
            drop.update(periodic)
            # Якщо модель системно зациклилася, поодинокі occurrence тієї ж
            # короткої фрази теж ненадійні. Реальні нерегулярні replies зберігаємо.
            if len(periodic) >= 8 and len(periodic) / len(positions) >= 0.75:
                drop.update(index for index, _ in positions)
        else:
            first = min(periodic)
            drop.update(index for index in periodic if index != first)
    return [segment for index, segment in enumerate(segments) if index not in drop], len(drop)


def _unwrap_annotation(annotation):
    if hasattr(annotation, "itertracks"):
        return annotation
    for attr in ("speaker_diarization", "annotation", "diarization"):
        inner = getattr(annotation, attr, None)
        if inner is not None and hasattr(inner, "itertracks"):
            return inner
    raise TypeError(f"Невідомий формат діаризації: {type(annotation)}")


def _speaker_for(start: float, end: float,
                 turns: list[tuple[float, float, str]]) -> str | None:
    overlaps: dict[str, float] = {}
    for turn_start, turn_end, speaker in turns:
        overlap = min(end, turn_end) - max(start, turn_start)
        if overlap > 0:
            overlaps[speaker] = overlaps.get(speaker, 0.0) + overlap
    return max(overlaps, key=overlaps.get) if overlaps else None


def _join_words(words: list[str]) -> str:
    text = " ".join(word.strip() for word in words if word.strip())
    return re.sub(r"\s+([,.;:!?])", r"\1", text).strip()


def assign_word_speakers(segments: list[dict],
                         turns: list[tuple[float, float, str]]) -> list[dict]:
    """Призначає speaker на word level і розбиває ASR-сегмент при зміні спікера."""
    units: list[dict] = []
    for segment in segments:
        words = segment.get("words") or []
        if words:
            for word in words:
                start, end = float(word["start"]), float(word["end"])
                units.append({
                    "start": start,
                    "end": end,
                    "speaker": _speaker_for(start, end, turns) or "UNKNOWN",
                    "text": str(word.get("word", "")).strip(),
                })
        else:
            start, end = float(segment["start"]), float(segment["end"])
            item = dict(segment)
            item["speaker"] = _speaker_for(start, end, turns) or "UNKNOWN"
            units.append(item)

    grouped: list[dict] = []
    for unit in units:
        previous = grouped[-1] if grouped else None
        if (previous and previous["speaker"] == unit["speaker"]
                and unit["start"] - previous["end"] <= 1.0):
            if "_words" not in previous:
                previous["_words"] = [previous.pop("text")]
            previous["_words"].append(unit["text"])
            previous["end"] = max(previous["end"], unit["end"])
        else:
            grouped.append(dict(unit))
    for item in grouped:
        if "_words" in item:
            item["text"] = _join_words(item.pop("_words"))
    return [item for item in grouped if item.get("text", "").strip()]


def _get_diarization_pipeline(token: str):
    global _DIARIZATION_PIPELINE, _DIARIZATION_DEVICE
    if _DIARIZATION_PIPELINE is not None:
        return _DIARIZATION_PIPELINE, _DIARIZATION_DEVICE

    import torch
    with warnings.catch_warnings():
        warnings.filterwarnings(
            "ignore",
            message=r"\s*torchcodec is not installed correctly.*",
            category=UserWarning,
            module=r"pyannote\.audio\.core\.io",
        )
        from pyannote.audio import Pipeline

    pipeline = Pipeline.from_pretrained(DIARIZE_MODEL, token=token)
    if pipeline is None:
        raise RuntimeError(f"Не вдалося завантажити {DIARIZE_MODEL}")
    if DIARIZE_DEVICE == "auto":
        device = "mps" if torch.backends.mps.is_available() else "cpu"
    else:
        device = DIARIZE_DEVICE
    if device != "cpu":
        try:
            pipeline.to(torch.device(device))
        except (RuntimeError, NotImplementedError) as exc:
            print(f">>> {device} недоступний для pyannote → CPU "
                  f"({exc.__class__.__name__})")
            device = "cpu-fallback"
    _DIARIZATION_PIPELINE, _DIARIZATION_DEVICE = pipeline, device
    return pipeline, device


def _diarize(wav: Path, segments: list[dict], num_speakers: int | None) -> tuple[list[dict], str]:
    global _DIARIZATION_DEVICE
    token = os.environ.get("HF_TOKEN")
    if not token:
        raise RuntimeError("HF_TOKEN не заданий (див. README.md)")

    import soundfile as sf
    import torch
    pipeline, device = _get_diarization_pipeline(token)
    audio, sample_rate = sf.read(str(wav), dtype="float32", always_2d=True)
    waveform = torch.from_numpy(audio.T)
    kwargs = {"num_speakers": num_speakers} if num_speakers else {}

    try:
        annotation = pipeline({"waveform": waveform, "sample_rate": sample_rate}, **kwargs)
    except RuntimeError as exc:
        if device != "mps":
            raise
        print(f">>> MPS diarization fallback → CPU ({exc.__class__.__name__})")
        pipeline.to(torch.device("cpu"))
        annotation = pipeline({"waveform": waveform, "sample_rate": sample_rate}, **kwargs)
        device = "cpu-fallback"
        _DIARIZATION_DEVICE = device

    annotation = _unwrap_annotation(annotation)
    turns = [(turn.start, turn.end, speaker)
             for turn, _, speaker in annotation.itertracks(yield_label=True)]
    return assign_word_speakers(segments, turns), device


def run_mlx(wav: Path, out_json: Path, meta: dict[str, Any], *,
            diarize: bool, num_speakers: int | None,
            force: bool = False) -> Path:
    if not force and _cache_valid(out_json, meta):
        print(f">>> {out_json.name} cache hit")
        return out_json

    import mlx_whisper

    print(f">>> mlx-whisper {wav.name}")
    kwargs: dict[str, Any] = {
        "path_or_hf_repo": MLX_MODEL,
        "word_timestamps": True,
        "hallucination_silence_threshold": 2.0,
    }
    if _language_arg():
        kwargs["language"] = _language_arg()
    result = mlx_whisper.transcribe(str(wav), **kwargs)
    segments, dropped = sanitize_asr_segments(
        result.get("segments", []), audio_info(wav)["duration"]
    )
    segments, periodic_dropped = drop_periodic_repetitions(segments)
    diarize_device = None
    if diarize:
        print(f">>> pyannote діаризація {wav.name}")
        segments, diarize_device = _diarize(wav, segments, num_speakers)

    atomic_write_json(out_json, {
        "_meta": meta,
        "segments": segments,
        "quality": {
            "asr_segments_dropped": dropped,
            "periodic_repetitions_dropped": periodic_dropped,
            "diarize_device": diarize_device,
        },
    })
    return out_json


def load_segments(json_path: Path, *, duration: float,
                  fixed_speaker: str | None = None) -> list[dict]:
    data = read_json(json_path, {})
    clean, _ = sanitize_asr_segments(data.get("segments", []), duration)
    clean, _ = drop_periodic_repetitions(clean)
    return [{
        "start": segment["start"],
        "end": segment["end"],
        "speaker": fixed_speaker or segment.get("speaker") or "UNKNOWN",
        "text": segment["text"].strip(),
    } for segment in clean]


def localize_mic_speakers(segments: list[dict]) -> list[dict]:
    """Відокремлює mic labels від системних SPEAKER_N."""
    localized = []
    for segment in segments:
        item = dict(segment)
        speaker = str(item.get("speaker") or "UNKNOWN")
        if speaker == "UNKNOWN":
            item["speaker"] = "LOCAL_UNKNOWN"
        elif speaker.startswith("SPEAKER_"):
            item["speaker"] = "LOCAL_" + speaker.removeprefix("SPEAKER_")
        else:
            item["speaker"] = "LOCAL_" + speaker
        localized.append(item)
    return localized


def collapse_fragmented_speakers(segments: list[dict]) -> tuple[list[dict], dict]:
    """Згортає 3+ pyannote labels, якщо один займає >=95%, а решта мікрокластери."""
    durations: collections.Counter[str] = collections.Counter()
    for segment in segments:
        speaker = str(segment.get("speaker") or "UNKNOWN")
        if speaker != "UNKNOWN":
            durations[speaker] += max(
                0.0, _as_float(segment.get("end")) - _as_float(segment.get("start"))
            )
    if len(durations) < 3:
        return segments, {"collapsed": False, "merged_labels": []}

    dominant, dominant_duration = durations.most_common(1)[0]
    total = sum(durations.values())
    tiny_limit = max(15.0, total * 0.01)
    others = [speaker for speaker in durations if speaker != dominant]
    tiny = [speaker for speaker in others if durations[speaker] <= tiny_limit]
    if total <= 0 or dominant_duration / total < 0.95 or len(tiny) != len(others):
        return segments, {"collapsed": False, "merged_labels": []}

    collapsed = []
    for segment in segments:
        item = dict(segment)
        # Сильний single-speaker signal також закриває короткі VAD gaps (UNKNOWN).
        item["speaker"] = "SPEAKER_00"
        collapsed.append(item)
    return collapsed, {
        "collapsed": True,
        "dominant_label": dominant,
        "dominant_share": round(dominant_duration / total, 4),
        "merged_labels": sorted(tiny),
    }


def correct_mic_timeline(segments: list[dict], *, mic_duration: float,
                         sys_duration: float, session_manifest: dict) -> tuple[list[dict], dict]:
    timing = session_manifest.get("timing") or {}
    offset = _as_float(timing.get("mic_start_offset_seconds"), 0.0)
    scale = _as_float(timing.get("mic_time_scale"), 0.0)
    method = "recording-manifest"
    if scale <= 0:
        scale = sys_duration / mic_duration if mic_duration > 0 else 1.0
        offset = 0.0
        method = "legacy-common-end"
    corrected = []
    for segment in segments:
        item = dict(segment)
        item["start"] = max(0.0, offset + segment["start"] * scale)
        item["end"] = min(sys_duration, offset + segment["end"] * scale)
        if item["end"] > item["start"]:
            corrected.append(item)
    return corrected, {"offset": offset, "scale": scale, "method": method}


def dedup_mic(mic: list[dict], sys_segments: list[dict], *, enabled: bool = True,
              window: float = 2.0, threshold: float = 0.85) -> tuple[list[dict], int]:
    """Консервативно прибирає довгі дублікати; короткі «Так/Добре» не чіпає."""
    if not enabled:
        return mic, 0

    def norm(text: str) -> str:
        return re.sub(r"[^\w ]", "", text.lower()).strip()

    kept, dropped = [], 0
    for mine in mic:
        mine_text = norm(mine["text"])
        mine_words = mine_text.split()
        if len(mine_words) < 4 or len(mine_text) < 16:
            kept.append(mine)
            continue
        leak = False
        for other in sys_segments:
            if other["end"] < mine["start"] - window:
                continue
            if other["start"] > mine["end"] + window:
                break
            other_text = norm(other["text"])
            if len(other_text.split()) < 4:
                continue
            ratio = difflib.SequenceMatcher(None, mine_text, other_text).ratio()
            if ratio >= threshold:
                leak = True
                break
        if leak:
            dropped += 1
        else:
            kept.append(mine)
    return kept, dropped


def merge_consecutive(segments: list[dict], max_gap: float = 5.0) -> list[dict]:
    merged: list[dict] = []
    for segment in segments:
        previous = merged[-1] if merged else None
        if (previous and previous["speaker"] == segment["speaker"]
                and segment["start"] - previous["end"] <= max_gap):
            previous["text"] += " " + segment["text"]
            previous["end"] = max(previous["end"], segment["end"])
        else:
            merged.append(dict(segment))
    return merged


def fmt_ts(base: datetime.datetime | None, seconds: float) -> str:
    if base is not None:
        return (base + datetime.timedelta(seconds=seconds)).strftime("%H:%M:%S")
    return str(datetime.timedelta(seconds=int(seconds)))


def parse_session_start(prefix: str) -> datetime.datetime | None:
    match = re.search(r"(\d{4}-\d{2}-\d{2})_(\d{2})(\d{2})(\d{2})?",
                      Path(prefix).name)
    if not match:
        return None
    seconds = match.group(4) or "00"
    return datetime.datetime.strptime(
        f"{match.group(1)} {match.group(2)}:{match.group(3)}:{seconds}",
        "%Y-%m-%d %H:%M:%S",
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("session_prefix")
    parser.add_argument("--num-speakers", type=int, default=None)
    parser.add_argument("--force", action="store_true")
    parser.add_argument(
        "--diarize-mic",
        action="store_true",
        help="діаризувати людей біля локального мікрофона",
    )
    parser.add_argument(
        "--postprocess-only",
        action="store_true",
        help="перебудувати transcript з наявних ASR JSON без повторного запуску моделей",
    )
    args = parser.parse_args()
    if args.num_speakers is not None and args.num_speakers < 1:
        raise ValueError("--num-speakers має бути >= 1")

    mic_source = Path(args.session_prefix + "_mic.wav")
    sys_source = Path(args.session_prefix + "_sys.wav")
    for wav in (mic_source, sys_source):
        if not wav.exists():
            raise FileNotFoundError(wav)

    session = Path(args.session_prefix).name
    work_dir = TRANSCRIPTS_DIR / session
    work_dir.mkdir(parents=True, exist_ok=True)
    mic_json = work_dir / f"{mic_source.stem}.json"
    sys_json = work_dir / f"{sys_source.stem}.json"
    session_manifest = read_json(BASE / "recordings" / f"{session}.json", {}) or {}
    recording_config = session_manifest.get("recording") or {}
    mic_diarization = bool(recording_config.get("mic_diarization")) or args.diarize_mic

    common = {
        "schema_version": 2,
        "engine": "mlx",
        "language": _language_arg() or "auto",
        "whisper_model": MLX_MODEL,
        "diarize_model": DIARIZE_MODEL,
    }
    if args.postprocess_only:
        for cache in (mic_json, sys_json):
            if not cache.exists():
                raise FileNotFoundError(f"Немає ASR cache для postprocess: {cache}")
        mic_info, sys_info = audio_info(mic_source), audio_info(sys_source)
        mic_cache = read_json(mic_json, {}) or {}
        if mic_diarization and not (mic_cache.get("_meta") or {}).get("diarize"):
            audio_dir = work_dir / "audio"
            mic_wav = normalized_audio(mic_source, audio_dir / mic_source.name)
            segments, dropped = sanitize_asr_segments(
                mic_cache.get("segments", []), mic_info["duration"]
            )
            segments, periodic_dropped = drop_periodic_repetitions(segments)
            print(">>> pyannote діаризація наявного mic ASR cache")
            segments, diarize_device = _diarize(mic_wav, segments, None)
            cache_meta = dict(mic_cache.get("_meta") or common)
            cache_meta["diarize"] = True
            atomic_write_json(mic_json, {
                "_meta": cache_meta,
                "segments": segments,
                "quality": {
                    "asr_segments_dropped": dropped,
                    "periodic_repetitions_dropped": periodic_dropped,
                    "diarize_device": diarize_device,
                },
            })
        print("Postprocess-only: використовую наявні ASR JSON")
    else:
        audio_dir = work_dir / "audio"
        mic_wav = normalized_audio(mic_source, audio_dir / mic_source.name)
        sys_wav = normalized_audio(sys_source, audio_dir / sys_source.name)
        mic_info, sys_info = audio_info(mic_wav), audio_info(sys_wav)
        mic_meta = {
            **common,
            "source": file_fingerprint(mic_source),
            "diarize": mic_diarization,
        }
        sys_meta = {**common, "source": file_fingerprint(sys_source), "diarize": True,
                    "num_speakers": args.num_speakers}
        print("Движок транскрипції: MLX Whisper; processing audio: mono 16 kHz")
        mic_json = run_mlx(
            mic_wav, mic_json, mic_meta,
            diarize=mic_diarization, num_speakers=None, force=args.force,
        )
        sys_json = run_mlx(
            sys_wav, sys_json, sys_meta,
            diarize=True, num_speakers=args.num_speakers, force=args.force,
        )

    mic_segments = load_segments(
        mic_json,
        duration=mic_info["duration"],
        fixed_speaker=None if mic_diarization else "Я",
    )
    if mic_diarization:
        mic_segments = localize_mic_speakers(mic_segments)
    sys_segments = load_segments(sys_json, duration=sys_info["duration"])
    sys_segments, speaker_collapse = collapse_fragmented_speakers(sys_segments)
    mic_segments, sync = correct_mic_timeline(
        mic_segments, mic_duration=mic_info["duration"],
        sys_duration=sys_info["duration"], session_manifest=session_manifest,
    )
    aec = bool((session_manifest.get("recording") or {}).get("aec"))
    mic_segments, deduped = dedup_mic(
        mic_segments, sys_segments, enabled=DEDUP_MIC and not aec
    )
    segments = sorted(mic_segments + sys_segments, key=lambda item: item["start"])
    segments = merge_consecutive(segments)
    unknown = sum(1 for segment in sys_segments if segment["speaker"] == "UNKNOWN")
    unknown_ratio = unknown / len(sys_segments) if sys_segments else 0.0
    local_unknown = sum(
        1 for segment in mic_segments if segment["speaker"] == "LOCAL_UNKNOWN"
    )
    local_unknown_ratio = local_unknown / len(mic_segments) if mic_segments else 0.0

    base = parse_session_start(args.session_prefix)
    out_md = TRANSCRIPTS_DIR / f"{session}.md"
    lines = [f"# Транскрипт {session}", ""]
    for segment in segments:
        lines.append(f"[{fmt_ts(base, segment['start'])}] "
                     f"{segment['speaker']}: {segment['text']}")
    atomic_write_text(out_md, "\n".join(lines) + "\n")
    quality = {
        "segments": len(segments),
        "unknown_speaker_segments": unknown,
        "unknown_speaker_ratio": round(unknown_ratio, 4),
        "local_unknown_speaker_segments": local_unknown,
        "local_unknown_speaker_ratio": round(local_unknown_ratio, 4),
        "mic_deduplicated": deduped,
        "mic_diarization": mic_diarization,
        "speaker_collapse": speaker_collapse,
        "sync": sync,
        "mic_duration": mic_info["duration"],
        "sys_duration": sys_info["duration"],
    }
    atomic_write_json(work_dir / "manifest.json", {
        "schema_version": 2,
        "session": session,
        "completed_at": utc_now(),
        "config": common,
        "quality": quality,
        "output": str(out_md),
    })
    speakers = sorted({segment["speaker"] for segment in segments})
    print(f"Готово: {out_md}")
    print(f"Сегментів: {len(segments)}; спікери: {', '.join(speakers)}")
    print(f"Sync mic: offset={sync['offset']:.3f}s scale={sync['scale']:.7f}; "
          f"UNKNOWN={unknown_ratio:.1%}; dedup={deduped}")
    if speaker_collapse["collapsed"]:
        print(">>> pyannote micro-clusters → SPEAKER_00: "
              + ", ".join(speaker_collapse["merged_labels"]))
    if unknown_ratio > 0.15:
        print("⚠️ Понад 15% системних сегментів без надійного speaker label.")
    if local_unknown_ratio > 0.15:
        print("⚠️ Понад 15% локальних сегментів без надійного speaker label.")


if __name__ == "__main__":
    main()
