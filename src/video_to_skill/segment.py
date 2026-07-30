"""Multi-signal semantic segmentation over the normalized timeline."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from itertools import pairwise

from video_to_skill.config import Settings
from video_to_skill.models import (
    SemanticSegment,
    SourceDescriptor,
    TranscriptSegment,
    VisualEvent,
    VisualKind,
)
from video_to_skill.utils import stable_hash

_WORD_RE = re.compile(r"[\w\u3400-\u9fff]+", re.UNICODE)
_STOP_WORDS = {
    "a",
    "an",
    "and",
    "are",
    "as",
    "at",
    "be",
    "by",
    "for",
    "from",
    "in",
    "is",
    "it",
    "of",
    "on",
    "or",
    "that",
    "the",
    "this",
    "to",
    "we",
    "will",
    "with",
    "you",
}


@dataclass(frozen=True)
class Boundary:
    timestamp: float
    score: float
    reason: str
    title: str | None = None


def _tokens(text: str) -> set[str]:
    return {
        token.casefold()
        for token in _WORD_RE.findall(text)
        if len(token) > 1 and token.casefold() not in _STOP_WORDS
    }


def _jaccard(left: set[str], right: set[str]) -> float:
    if not left or not right:
        return 1.0
    return len(left & right) / len(left | right)


def _topic_boundaries(
    transcripts: list[TranscriptSegment],
) -> list[Boundary]:
    boundaries: list[Boundary] = []
    window = 3
    for index in range(window, len(transcripts) - window):
        before = _tokens(" ".join(item.text for item in transcripts[index - window : index]))
        after = _tokens(" ".join(item.text for item in transcripts[index : index + window]))
        similarity = _jaccard(before, after)
        if similarity < 0.12:
            boundaries.append(
                Boundary(
                    timestamp=transcripts[index].start,
                    score=(0.12 - similarity) * 20 + 1,
                    reason="speech-topic-shift",
                )
            )
    return boundaries


def _candidate_boundaries(
    source: SourceDescriptor,
    transcripts: list[TranscriptSegment],
    visuals: list[VisualEvent],
) -> list[Boundary]:
    candidates: list[Boundary] = []
    for chapter in source.chapters:
        if chapter.start > 0:
            candidates.append(
                Boundary(
                    timestamp=chapter.start,
                    score=100,
                    reason="creator-chapter",
                    title=chapter.title,
                )
            )
    for previous, current in pairwise(transcripts):
        gap = current.start - previous.end
        if gap >= 4:
            candidates.append(
                Boundary(
                    timestamp=current.start,
                    score=min(8, 2 + gap / 2),
                    reason="speech-pause",
                )
            )
    candidates.extend(_topic_boundaries(transcripts))
    for event in visuals:
        if event.scene_score is not None and event.scene_score >= 0.45:
            candidates.append(
                Boundary(
                    timestamp=event.timestamp,
                    score=3 + event.scene_score * 4,
                    reason="strong-scene-change",
                )
            )
        elif event.kind in {VisualKind.SLIDE, VisualKind.CODE, VisualKind.UI}:
            candidates.append(
                Boundary(
                    timestamp=event.timestamp,
                    score=2,
                    reason=f"{event.kind.value}-state-change",
                )
            )
    return sorted(candidates, key=lambda item: item.timestamp)


def _choose_boundaries(
    duration: float,
    candidates: list[Boundary],
    settings: Settings,
) -> list[Boundary]:
    selected = [Boundary(0.0, 1000, "source-start")]
    cursor = 0.0
    while duration - cursor > settings.max_segment_seconds:
        minimum = cursor + settings.min_segment_seconds
        maximum = min(duration, cursor + settings.max_segment_seconds)
        target = min(duration, cursor + settings.target_segment_seconds)
        eligible = [
            item
            for item in candidates
            if minimum <= item.timestamp <= maximum
            and item.timestamp > selected[-1].timestamp + 0.5
        ]
        if eligible:
            chapter_candidates = [item for item in eligible if item.reason == "creator-chapter"]
            pool = chapter_candidates or eligible
            chosen = max(
                pool,
                key=lambda item: (
                    item.score - abs(item.timestamp - target) / settings.target_segment_seconds
                ),
            )
        else:
            chosen = Boundary(
                timestamp=maximum,
                score=0,
                reason="maximum-duration-split",
            )
        selected.append(chosen)
        cursor = chosen.timestamp

    for chapter in candidates:
        if (
            chapter.reason == "creator-chapter"
            and 0 < chapter.timestamp < duration
            and all(abs(item.timestamp - chapter.timestamp) > 0.5 for item in selected)
        ):
            selected.append(chapter)
    return sorted(selected, key=lambda item: item.timestamp)


def _derive_title(
    start: float,
    end: float,
    transcripts: list[TranscriptSegment],
    explicit: str | None,
    ordinal: int,
) -> str:
    if explicit:
        return explicit.strip()
    text = " ".join(item.text for item in transcripts if item.end >= start and item.start <= end)
    words = _WORD_RE.findall(text)
    if words:
        phrase = " ".join(words[:8])
        return phrase[:80]
    return f"Section {ordinal}"


def segment_timeline(
    source: SourceDescriptor,
    transcripts: list[TranscriptSegment],
    visuals: list[VisualEvent],
    settings: Settings,
) -> list[SemanticSegment]:
    ends = [item.end for item in transcripts]
    ends.extend(item.timestamp for item in visuals)
    if source.duration is not None:
        ends.append(source.duration)
    duration = (
        source.duration
        if source.duration is not None and source.duration > 0
        else max(ends, default=0)
    )
    if duration <= 0:
        return []

    candidates = _candidate_boundaries(source, transcripts, visuals)
    boundaries = _choose_boundaries(duration, candidates, settings)
    results: list[SemanticSegment] = []
    for index, boundary in enumerate(boundaries):
        end = boundaries[index + 1].timestamp if index + 1 < len(boundaries) else duration
        if math.isclose(end, boundary.timestamp, abs_tol=0.01):
            continue
        ordinal = len(results) + 1
        transcript_ids = [
            item.id for item in transcripts if item.end >= boundary.timestamp and item.start < end
        ]
        visual_ids = [item.id for item in visuals if boundary.timestamp <= item.timestamp < end]
        title = _derive_title(
            boundary.timestamp,
            end,
            transcripts,
            boundary.title,
            ordinal,
        )
        results.append(
            SemanticSegment(
                id=stable_hash(
                    {
                        "source": source.id,
                        "start": round(boundary.timestamp, 3),
                        "end": round(end, 3),
                    },
                    length=24,
                ),
                source_id=source.id,
                ordinal=ordinal,
                title=title,
                start=boundary.timestamp,
                end=end,
                transcript_ids=transcript_ids,
                visual_event_ids=visual_ids,
                boundary_reasons=[boundary.reason],
            )
        )
    return results
