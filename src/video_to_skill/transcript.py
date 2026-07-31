"""Caption parsing, quality selection, and local ASR fallback."""

from __future__ import annotations

import html
import importlib.util
import os
import re
from abc import ABC, abstractmethod
from importlib.metadata import entry_points
from pathlib import Path
from typing import Any

from video_to_skill.config import Settings
from video_to_skill.errors import DependencyError, ProcessingError
from video_to_skill.models import (
    MaterializedSource,
    SourcePlatform,
    TranscriptOrigin,
    TranscriptSegment,
    TranscriptWord,
)
from video_to_skill.tool_runs import digest_value, python_package_version, tracked_operation
from video_to_skill.utils import run_command, stable_hash

_TIMING_RE = re.compile(
    r"(?P<start>\d{1,2}:\d{2}(?::\d{2})?[\.,]\d{3})\s+-->\s+"
    r"(?P<end>\d{1,2}:\d{2}(?::\d{2})?[\.,]\d{3})"
)
_TAG_RE = re.compile(r"<[^>]+>")
_ASS_TAG_RE = re.compile(r"\{\\[^}]+\}")


def parse_timestamp(value: str) -> float:
    normalized = value.replace(",", ".")
    parts = normalized.split(":")
    if len(parts) == 2:
        hours = "0"
        minutes, seconds = parts
    elif len(parts) == 3:
        hours, minutes, seconds = parts
    else:
        raise ValueError(f"Invalid subtitle timestamp: {value}")
    return int(hours) * 3600 + int(minutes) * 60 + float(seconds)


def _clean_caption_text(lines: list[str]) -> str:
    text = " ".join(line.strip() for line in lines if line.strip())
    text = _ASS_TAG_RE.sub("", text)
    text = _TAG_RE.sub("", text)
    text = html.unescape(text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def parse_caption(
    path: Path,
    *,
    source_id: str,
    language: str | None,
    origin: TranscriptOrigin,
) -> list[TranscriptSegment]:
    """Parse SRT/WebVTT cues without trusting embedded markup."""

    content = path.read_text(encoding="utf-8-sig", errors="replace")
    lines = content.splitlines()
    raw: list[TranscriptSegment] = []
    index = 0
    while index < len(lines):
        match = _TIMING_RE.search(lines[index])
        if not match:
            index += 1
            continue
        start = parse_timestamp(match.group("start"))
        end = parse_timestamp(match.group("end"))
        index += 1
        text_lines: list[str] = []
        while index < len(lines) and lines[index].strip():
            text_lines.append(lines[index])
            index += 1
        text = _clean_caption_text(text_lines)
        if text and end >= start:
            raw.append(
                TranscriptSegment(
                    id=stable_hash(
                        {
                            "source": source_id,
                            "start": round(start, 3),
                            "end": round(end, 3),
                            "text": text,
                        },
                        length=24,
                    ),
                    source_id=source_id,
                    start=start,
                    end=end,
                    text=text,
                    language=language,
                    origin=origin,
                )
            )
        index += 1
    return deduplicate_rolling_captions(raw)


def deduplicate_rolling_captions(
    segments: list[TranscriptSegment],
) -> list[TranscriptSegment]:
    """Collapse YouTube-style rolling cues while preserving time coverage."""

    result: list[TranscriptSegment] = []
    for current in sorted(segments, key=lambda item: (item.start, item.end)):
        if not result:
            result.append(current)
            continue
        previous = result[-1]
        previous_text = previous.text.casefold()
        current_text = current.text.casefold()
        overlaps = current.start <= previous.end + 0.25
        if overlaps and current_text == previous_text:
            previous.end = max(previous.end, current.end)
            continue
        if overlaps and current_text.startswith(previous_text):
            current.start = previous.start
            result[-1] = current
            continue
        if overlaps and previous_text.startswith(current_text):
            previous.end = max(previous.end, current.end)
            continue
        result.append(current)
    return result


def caption_quality(
    segments: list[TranscriptSegment], duration: float | None
) -> tuple[float, list[str]]:
    if not segments:
        return 0.0, ["caption-empty"]
    warnings: list[str] = []
    characters = sum(len(item.text) for item in segments)
    covered = sum(max(0, item.end - item.start) for item in segments)
    effective_duration = duration or max(item.end for item in segments)
    coverage = min(1.0, covered / effective_duration) if effective_duration else 0.0
    if characters < 50 and effective_duration > 120:
        warnings.append("caption-too-short")
    if coverage < 0.20 and effective_duration > 120:
        warnings.append("caption-low-coverage")
    duplicate_ratio = 1 - len({item.text.casefold() for item in segments}) / len(segments)
    if duplicate_ratio > 0.50:
        warnings.append("caption-high-duplication")
    score = min(1.0, coverage * 0.6 + min(characters / 1000, 1) * 0.4)
    if warnings:
        score *= 0.5
    return score, warnings


def _language_from_filename(path: Path) -> str | None:
    pieces = path.name.split(".")
    if len(pieces) >= 3:
        return pieces[-2]
    return None


def _origin_for_caption(materialized: MaterializedSource, language: str | None) -> TranscriptOrigin:
    candidates = materialized.source.captions
    for track in candidates:
        if language and track.language.casefold() == language.casefold():
            return (
                TranscriptOrigin.AUTO_CAPTION
                if track.automatic
                else TranscriptOrigin.MANUAL_CAPTION
            )
    return TranscriptOrigin.MANUAL_CAPTION


def best_caption(
    materialized: MaterializedSource,
    preferred_language: str | None = None,
) -> tuple[list[TranscriptSegment], Path | None, list[str]]:
    best: tuple[float, list[TranscriptSegment], Path, list[str]] | None = None
    for path in materialized.caption_paths:
        if path.suffix.lower() not in {".vtt", ".srt"}:
            continue
        language = _language_from_filename(path) or materialized.source.language
        origin = _origin_for_caption(materialized, language)
        segments = parse_caption(
            path,
            source_id=materialized.source.id,
            language=language,
            origin=origin,
        )
        score, warnings = caption_quality(segments, materialized.source.duration)
        manual_bonus = 0.10 if origin == TranscriptOrigin.MANUAL_CAPTION else 0
        preferred = preferred_language
        if preferred in {None, "auto"}:
            preferred = materialized.source.language
        if preferred in {None, "auto"}:
            preferred = "zh" if materialized.source.platform == SourcePlatform.BILIBILI else "en"
        language_bonus = (
            0.20
            if language and preferred and language.casefold().startswith(preferred.casefold())
            else 0
        )
        candidate = (score + manual_bonus + language_bonus, segments, path, warnings)
        if best is None or candidate[0] > best[0]:
            best = candidate
    if best is None:
        return [], None, ["caption-unavailable"]
    return best[1], best[2], best[3]


def captions_are_adequate(materialized: MaterializedSource, settings: Settings) -> bool:
    segments, _path, _warnings = best_caption(materialized, preferred_language=settings.language)
    score, _quality_warnings = caption_quality(segments, materialized.source.duration)
    return bool(segments and score >= 0.25)


class Transcriber(ABC):
    name: str

    @abstractmethod
    def available(self) -> bool:
        raise NotImplementedError

    @abstractmethod
    def transcribe(
        self,
        audio_path: Path,
        *,
        source_id: str,
        language: str | None,
        settings: Settings,
    ) -> list[TranscriptSegment]:
        raise NotImplementedError


class FasterWhisperTranscriber(Transcriber):
    name = "faster-whisper"

    def available(self) -> bool:
        return importlib.util.find_spec("faster_whisper") is not None

    def transcribe(
        self,
        audio_path: Path,
        *,
        source_id: str,
        language: str | None,
        settings: Settings,
    ) -> list[TranscriptSegment]:
        if not self.available():
            raise DependencyError(
                "No usable captions were found and faster-whisper is not installed. "
                "Install `video-to-skill[asr]` or configure another transcriber."
            )
        from faster_whisper import WhisperModel  # type: ignore[import-not-found]

        model = WhisperModel(settings.asr_model, device="auto", compute_type="auto")
        detected_language = None if language in {None, "auto"} else language
        raw_segments, info = model.transcribe(
            str(audio_path),
            language=detected_language,
            word_timestamps=True,
            vad_filter=True,
            condition_on_previous_text=False,
        )
        result: list[TranscriptSegment] = []
        for raw in raw_segments:
            text = raw.text.strip()
            if not text:
                continue
            words = [
                TranscriptWord(
                    text=word.word,
                    start=float(word.start),
                    end=float(word.end),
                    probability=float(word.probability),
                )
                for word in raw.words or []
                if word.start is not None and word.end is not None
            ]
            result.append(
                TranscriptSegment(
                    id=stable_hash(
                        {
                            "source": source_id,
                            "start": round(float(raw.start), 3),
                            "text": text,
                        },
                        length=24,
                    ),
                    source_id=source_id,
                    start=float(raw.start),
                    end=float(raw.end),
                    text=text,
                    language=getattr(info, "language", detected_language),
                    origin=TranscriptOrigin.ASR,
                    confidence=(
                        max(0.0, min(1.0, 1.0 + float(raw.avg_logprob)))
                        if raw.avg_logprob is not None
                        else None
                    ),
                    words=words,
                )
            )
        if not result:
            raise ProcessingError("ASR completed but produced no speech segments")
        return result


class WhisperXTranscriber(Transcriber):
    """Optional time-aligned ASR with speaker diarization support."""

    name = "whisperx"

    def available(self) -> bool:
        return importlib.util.find_spec("whisperx") is not None

    def transcribe(
        self,
        audio_path: Path,
        *,
        source_id: str,
        language: str | None,
        settings: Settings,
    ) -> list[TranscriptSegment]:
        if not self.available():
            raise DependencyError(
                "WhisperX is not installed. Install `video-to-skill[diarization]`."
            )
        import whisperx  # type: ignore[import-not-found]

        try:
            import torch  # type: ignore[import-not-found]

            use_cuda = bool(torch.cuda.is_available())
        except ImportError:
            use_cuda = False
        device = "cuda" if use_cuda else "cpu"
        compute_type = "float16" if use_cuda else "int8"
        audio: Any = whisperx.load_audio(str(audio_path))
        model: Any = whisperx.load_model(settings.asr_model, device, compute_type=compute_type)
        requested_language = None if language in {None, "auto"} else language
        raw: dict[str, Any] = model.transcribe(audio, batch_size=16, language=requested_language)
        detected_language = str(raw.get("language") or requested_language or "en")
        align_model, metadata = whisperx.load_align_model(
            language_code=detected_language, device=device
        )
        aligned: dict[str, Any] = whisperx.align(
            raw["segments"],
            align_model,
            metadata,
            audio,
            device,
            return_char_alignments=False,
        )
        if settings.diarize:
            token = os.getenv("HF_TOKEN")
            if not token:
                raise DependencyError("WhisperX diarization requires HF_TOKEN in the environment")
            diarizer = whisperx.DiarizationPipeline(token=token, device=device)
            speaker_turns = diarizer(audio)
            aligned = whisperx.assign_word_speakers(speaker_turns, aligned)

        result: list[TranscriptSegment] = []
        for raw_segment in aligned.get("segments") or []:
            if not isinstance(raw_segment, dict):
                continue
            text = str(raw_segment.get("text") or "").strip()
            start = raw_segment.get("start")
            end = raw_segment.get("end")
            if not text or not isinstance(start, (int, float)) or not isinstance(end, (int, float)):
                continue
            words: list[TranscriptWord] = []
            for raw_word in raw_segment.get("words") or []:
                if not isinstance(raw_word, dict):
                    continue
                word_start = raw_word.get("start")
                word_end = raw_word.get("end")
                if not isinstance(word_start, (int, float)) or not isinstance(
                    word_end, (int, float)
                ):
                    continue
                score = raw_word.get("score")
                words.append(
                    TranscriptWord(
                        text=str(raw_word.get("word") or ""),
                        start=float(word_start),
                        end=float(word_end),
                        probability=(float(score) if isinstance(score, (int, float)) else None),
                    )
                )
            confidences = [item.probability for item in words if item.probability is not None]
            result.append(
                TranscriptSegment(
                    id=stable_hash(
                        {
                            "source": source_id,
                            "start": round(float(start), 3),
                            "text": text,
                        },
                        length=24,
                    ),
                    source_id=source_id,
                    start=float(start),
                    end=float(end),
                    text=text,
                    language=detected_language,
                    speaker=raw_segment.get("speaker"),
                    origin=TranscriptOrigin.ASR,
                    confidence=(sum(confidences) / len(confidences) if confidences else None),
                    words=words,
                )
            )
        if not result:
            raise ProcessingError("WhisperX completed but produced no speech segments")
        return result


def load_transcriber(name: str) -> Transcriber:
    providers: dict[str, type[Transcriber]] = {
        FasterWhisperTranscriber.name: FasterWhisperTranscriber,
        WhisperXTranscriber.name: WhisperXTranscriber,
    }
    for entry_point in entry_points(group="video_to_skill.transcribers"):
        if entry_point.name in providers:
            continue
        loaded = entry_point.load()
        providers[entry_point.name] = loaded
    selected = FasterWhisperTranscriber.name if name == "auto" else name
    provider_type = providers.get(selected)
    if provider_type is None:
        raise DependencyError(
            f"Unknown ASR provider '{name}'. Available: {', '.join(sorted(providers))}"
        )
    provider = provider_type()
    if not isinstance(provider, Transcriber):
        raise DependencyError(f"ASR provider '{selected}' does not implement Transcriber")
    if not provider.available():
        if name == "auto":
            raise DependencyError(
                "No local ASR backend is installed. Install `video-to-skill[asr]`."
            )
        raise DependencyError(f"ASR provider '{selected}' is unavailable")
    return provider


def extract_audio(media_path: Path, destination: Path, settings: Settings) -> Path:
    destination.parent.mkdir(parents=True, exist_ok=True)
    run_command(
        [
            settings.ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(media_path),
            "-vn",
            "-ac",
            "1",
            "-ar",
            "16000",
            "-c:a",
            "pcm_s16le",
            str(destination),
        ],
        timeout=settings.command_timeout_seconds,
        provenance_operation="extract-asr-audio",
    )
    if not destination.is_file() or destination.stat().st_size == 0:
        raise ProcessingError(f"FFmpeg did not produce audio: {destination}")
    return destination


def transcribe(
    materialized: MaterializedSource,
    settings: Settings,
    *,
    transcriber: Transcriber | None = None,
) -> tuple[list[TranscriptSegment], list[str]]:
    captions, _caption_path, caption_warnings = best_caption(
        materialized, preferred_language=settings.language
    )
    score, _ = caption_quality(captions, materialized.source.duration)
    if captions and score >= 0.25:
        return captions, caption_warnings
    if settings.asr_provider == "none":
        raise ProcessingError(
            f"No adequate captions for '{materialized.source.title}' and ASR is disabled"
        )
    if materialized.media_path is None:
        raise ProcessingError(
            f"No adequate captions or downloaded media for '{materialized.source.title}'"
        )
    audio_path = materialized.directory / "audio-16khz.wav"
    if not audio_path.exists():
        extract_audio(materialized.media_path, audio_path, settings)
    backend = transcriber or load_transcriber(settings.asr_provider)
    requested_language = materialized.source.language or settings.language
    with tracked_operation(
        tool=backend.name,
        version=python_package_version(backend.name),
        operation="transcribe-audio",
        arguments={
            "language": requested_language,
            "model": settings.asr_model,
            "diarize": settings.diarize,
        },
        input_paths=[audio_path],
    ) as tool_run:
        segments = backend.transcribe(
            audio_path,
            source_id=materialized.source.id,
            language=requested_language,
            settings=settings,
        )
        tool_run.add_logical_output(
            "transcript-segments",
            materialized.source.id,
            digest_value([segment.model_dump(mode="json") for segment in segments]),
        )
    return segments, [*caption_warnings, "caption-replaced-by-asr"]
