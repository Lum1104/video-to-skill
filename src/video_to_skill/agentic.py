"""Persistent, bounded evidence tools for a host agent."""

from __future__ import annotations

import json
import math
import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from pydantic import ValidationError as PydanticValidationError

from video_to_skill.errors import ProcessingError
from video_to_skill.models import (
    AgentContext,
    AgentObservation,
    EvidenceGap,
    EvidenceGapSeverity,
    EvidenceGapType,
    EvidenceWindow,
    ObservationStatus,
    ObservationType,
    SemanticSegment,
    TranscriptSegment,
    VisualEvent,
    VisualKind,
    VisualRetentionReport,
)
from video_to_skill.workspace import Workspace

DEFAULT_MAX_CONTEXT_SECONDS = 600.0
DEFAULT_MAX_CONTEXT_ITEMS = 250
REFERENCE_NEIGHBORHOOD_SECONDS = 30.0
MAX_ANNOTATION_PAYLOAD_BYTES = 2 * 1024 * 1024
MAX_ANNOTATIONS_PER_BATCH = 500
LONG_PROCEDURAL_SECTION_SECONDS = 90.0
VISUAL_SAMPLE_SECONDS = 45.0

_PROCEDURAL_PATTERN = re.compile(
    r"\b(?:"
    r"add|apply|attach|choose|click|close|configure|connect|create|cut|drag|drop|"
    r"enter|execute|fold|install|mix|navigate|open|place|press|remove|run|select|"
    r"set|swipe|tap|turn|type"
    r")\b|点击|选择|打开|输入|按下|拖动|安装|配置|创建|添加|删除|运行|连接|放置|折叠",
    re.IGNORECASE,
)

_TRANSCRIPT_CLAIM_PATTERNS = {
    ObservationType.CODE: re.compile(
        r"\b(?:api|class|code|command|compile|function|script|terminal)\b|"
        r"代码|命令|终端|函数|脚本|编译",
        re.IGNORECASE,
    ),
    ObservationType.UI: re.compile(
        r"\b(?:button|checkbox|dialog|dropdown|interface|menu|panel|screen|tab|window)\b|"
        r"按钮|复选框|对话框|下拉|界面|菜单|面板|屏幕|选项卡|窗口",
        re.IGNORECASE,
    ),
    ObservationType.PHYSICAL: re.compile(
        r"\b(?:board|cable|hand|screw|tool|wire)\b|"
        r"板|电缆|手部|螺丝|工具|电线",
        re.IGNORECASE,
    ),
    ObservationType.ACTION: _PROCEDURAL_PATTERN,
}

_VISUAL_TO_OBSERVATION_TYPE = {
    VisualKind.CODE: ObservationType.CODE,
    VisualKind.UI: ObservationType.UI,
    VisualKind.PHYSICAL: ObservationType.PHYSICAL,
}


def visual_retention_gaps(report: VisualRetentionReport) -> list[EvidenceGap]:
    """Project durable baseline-retention loss into actionable evidence gaps."""

    if not report.truncated:
        return []
    return [
        EvidenceGap(
            source_id=report.source_id,
            gap_type=EvidenceGapType.VISUAL_RETENTION_TRUNCATED,
            severity=EvidenceGapSeverity.WARNING,
            message=(
                f"Baseline visual retention dropped {interval.dropped_count} candidate(s) "
                f"in {interval.start:g}-{interval.end:g}s under the persisted "
                "analysis-depth cap; visual coverage is partial."
            ),
            suggested_next_action=(
                f"Use bounded frame investigation within {interval.start:g}-"
                f"{interval.end:g}s only if the interval is material, or create a new "
                "workspace with a deeper explicit profile."
            ),
            start=interval.start,
            end=interval.end,
        )
        for interval in report.affected_intervals
    ]


def _bounded_interval(
    workspace: Workspace,
    *,
    source_id: str,
    section: int | str | None,
    at: float | None,
    window: float | None,
    start: float | None,
    end: float | None,
    max_window_seconds: float,
) -> tuple[EvidenceWindow, list[SemanticSegment]]:
    source = workspace.get_source(source_id)
    numeric_bounds = [
        value for value in (at, window, start, end, max_window_seconds) if value is not None
    ]
    if any(not math.isfinite(value) for value in numeric_bounds):
        raise ProcessingError("Context bounds must be finite numbers")
    section_mode = section is not None
    at_mode = at is not None or window is not None
    range_mode = start is not None or end is not None
    if sum((section_mode, at_mode, range_mode)) != 1:
        raise ProcessingError(
            "Context requires exactly one bound: section, at plus window, or start plus end"
        )
    if max_window_seconds <= 0:
        raise ProcessingError("max_window_seconds must be positive")

    all_segments = workspace.semantic_segments(source_id)
    if section_mode:
        if isinstance(section, int):
            matches = [item for item in all_segments if item.ordinal == section]
        else:
            section_text = str(section)
            matches = [item for item in all_segments if item.id == section_text]
            if not matches and section_text.isdigit():
                matches = [item for item in all_segments if item.ordinal == int(section_text)]
        if not matches:
            raise ProcessingError(f"Source '{source.title}' has no semantic section {section}")
        if len(matches) != 1:
            raise ProcessingError(f"Semantic section selector is ambiguous: {section}")
        selected = matches[0]
        lower, upper = selected.start, selected.end
        segments = matches
    elif at_mode:
        if at is None or window is None:
            raise ProcessingError("Both at and window are required for timestamp context")
        if at < 0 or window <= 0:
            raise ProcessingError("at must be non-negative and window must be positive")
        if source.duration is not None and at > source.duration:
            raise ProcessingError(
                f"Context timestamp {at:g}s exceeds source duration {source.duration:g}s"
            )
        lower = max(0.0, at - window)
        upper = at + window
        if source.duration is not None:
            upper = min(upper, source.duration)
        segments = [item for item in all_segments if item.end >= lower and item.start <= upper]
    else:
        if start is None or end is None:
            raise ProcessingError("Both start and end are required for range context")
        if start < 0 or end <= start:
            raise ProcessingError("Context end must be greater than its non-negative start")
        if source.duration is not None and end > source.duration:
            raise ProcessingError(
                f"Context end {end:g}s exceeds source duration {source.duration:g}s"
            )
        lower, upper = start, end
        segments = [item for item in all_segments if item.end >= lower and item.start <= upper]

    if upper <= lower:
        raise ProcessingError("Context window is empty")
    if upper - lower > max_window_seconds:
        raise ProcessingError(
            f"Context window is {upper - lower:g}s; maximum is {max_window_seconds:g}s"
        )
    return EvidenceWindow(start=lower, end=upper), segments


def assemble_agent_context(
    workspace: Workspace,
    *,
    source_id: str,
    section: int | str | None = None,
    at: float | None = None,
    window: float | None = None,
    start: float | None = None,
    end: float | None = None,
    max_window_seconds: float = DEFAULT_MAX_CONTEXT_SECONDS,
    max_items_per_kind: int = DEFAULT_MAX_CONTEXT_ITEMS,
) -> AgentContext:
    """Build one finite evidence packet.

    ``at`` plus ``window`` uses ``window`` as a radius, so ``at=120, window=30``
    requests 90-150 seconds (clamped to the source duration).
    """

    if max_items_per_kind < 1:
        raise ProcessingError("max_items_per_kind must be positive")
    source = workspace.get_source(source_id)
    bounded_window, segments = _bounded_interval(
        workspace,
        source_id=source_id,
        section=section,
        at=at,
        window=window,
        start=start,
        end=end,
        max_window_seconds=max_window_seconds,
    )
    item_limit = max_items_per_kind + 1
    transcripts = workspace.transcripts(
        source_id,
        start=bounded_window.start,
        end=bounded_window.end,
        limit=item_limit,
    )
    visuals = workspace.visuals(
        source_id,
        start=bounded_window.start,
        end=bounded_window.end,
        limit=item_limit,
    )
    observations = workspace.observations(
        source_id,
        start=bounded_window.start,
        end=bounded_window.end,
        limit=item_limit,
    )
    truncated = any(
        len(items) > max_items_per_kind for items in (segments, transcripts, visuals, observations)
    )
    return AgentContext(
        source=source,
        window=bounded_window,
        segments=segments[:max_items_per_kind],
        transcripts=transcripts[:max_items_per_kind],
        visuals=visuals[:max_items_per_kind],
        observations=observations[:max_items_per_kind],
        truncated=truncated,
    )


def _decode_annotation_payload(
    payload: str | bytes | Path | Mapping[str, Any] | Sequence[Mapping[str, Any]],
    *,
    max_payload_bytes: int,
    max_observations: int,
) -> list[Mapping[str, Any]]:
    if max_payload_bytes < 1 or max_observations < 1:
        raise ProcessingError("Annotation payload bounds must be positive")
    raw: Any = payload
    if isinstance(payload, Path):
        try:
            if not payload.is_file():
                raise ProcessingError(f"Annotation JSON is not a regular file: {payload}")
            with payload.open("rb") as handle:
                serialized = handle.read(max_payload_bytes + 1)
            if len(serialized) > max_payload_bytes:
                raise ProcessingError(f"Annotation JSON exceeds the {max_payload_bytes}-byte limit")
            raw = json.loads(serialized.decode("utf-8"))
        except OSError as exc:
            raise ProcessingError(f"Could not read annotation JSON: {exc}") from exc
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ProcessingError(f"Invalid annotation JSON: {exc}") from exc
    elif isinstance(payload, bytes):
        if len(payload) > max_payload_bytes:
            raise ProcessingError(f"Annotation JSON exceeds the {max_payload_bytes}-byte limit")
        try:
            raw = json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ProcessingError(f"Invalid annotation JSON: {exc}") from exc
    elif isinstance(payload, str):
        if len(payload.encode("utf-8")) > max_payload_bytes:
            raise ProcessingError(f"Annotation JSON exceeds the {max_payload_bytes}-byte limit")
        try:
            raw = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise ProcessingError(f"Invalid annotation JSON: {exc}") from exc
    elif isinstance(payload, Mapping) or (
        isinstance(payload, Sequence) and not isinstance(payload, (str, bytes))
    ):
        try:
            serialized = json.dumps(
                payload,
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
        except (TypeError, ValueError) as exc:
            raise ProcessingError(f"Annotations are not JSON serializable: {exc}") from exc
        if len(serialized) > max_payload_bytes:
            raise ProcessingError(f"Annotation JSON exceeds the {max_payload_bytes}-byte limit")

    if isinstance(raw, Mapping) and "observations" in raw:
        if set(raw) != {"observations"}:
            extras = ", ".join(sorted(str(key) for key in set(raw) - {"observations"}))
            raise ProcessingError(f"Unknown annotation batch fields: {extras}")
        raw = raw["observations"]
    elif isinstance(raw, Mapping):
        raw = [raw]

    if isinstance(raw, Sequence) and not isinstance(raw, (str, bytes)):
        raw = list(raw)
    if not isinstance(raw, list):
        raise ProcessingError("Annotations must be one object, a list, or an observations batch")
    if len(raw) > max_observations:
        raise ProcessingError(
            f"Annotation batch has {len(raw)} observations; maximum is {max_observations}"
        )
    if any(not isinstance(item, Mapping) for item in raw):
        raise ProcessingError("Every annotation must be a JSON object")
    return list(raw)


def _observation_models(
    payload: str | bytes | Path | Mapping[str, Any] | Sequence[Mapping[str, Any]],
    *,
    max_payload_bytes: int,
    max_observations: int,
) -> list[AgentObservation]:
    raw_items = _decode_annotation_payload(
        payload,
        max_payload_bytes=max_payload_bytes,
        max_observations=max_observations,
    )
    try:
        items = [AgentObservation.model_validate(item) for item in raw_items]
    except PydanticValidationError as exc:
        raise ProcessingError(f"Invalid agent observation: {exc}") from exc
    identifiers = [item.id for item in items]
    if len(identifiers) != len(set(identifiers)):
        raise ProcessingError("Annotation batch contains duplicate observation ids")
    return items


def _validate_neighborhood_seconds(neighborhood_seconds: float) -> None:
    if not math.isfinite(neighborhood_seconds) or neighborhood_seconds < 0:
        raise ProcessingError("neighborhood_seconds must be finite and non-negative")


def ingest_annotations(
    workspace: Workspace,
    payload: str | bytes | Path | Mapping[str, Any] | Sequence[Mapping[str, Any]],
    *,
    neighborhood_seconds: float = REFERENCE_NEIGHBORHOOD_SECONDS,
    max_payload_bytes: int = MAX_ANNOTATION_PAYLOAD_BYTES,
    max_observations: int = MAX_ANNOTATIONS_PER_BATCH,
) -> list[AgentObservation]:
    """Strictly validate and atomically upsert one annotation batch."""

    observations = _observation_models(
        payload,
        max_payload_bytes=max_payload_bytes,
        max_observations=max_observations,
    )
    _validate_neighborhood_seconds(neighborhood_seconds)
    workspace.upsert_grounded_observations(
        observations,
        neighborhood_seconds=neighborhood_seconds,
    )
    return observations


def _segment_transcripts(
    segment: SemanticSegment,
    transcript_by_id: Mapping[str, TranscriptSegment],
) -> list[TranscriptSegment]:
    return [
        transcript_by_id[identifier]
        for identifier in segment.transcript_ids
        if identifier in transcript_by_id
    ]


def _segment_visuals(
    segment: SemanticSegment,
    visual_by_id: Mapping[str, VisualEvent],
) -> list[VisualEvent]:
    explicit_ids = set(segment.visual_event_ids)
    return sorted(
        (
            visual
            for visual in visual_by_id.values()
            if visual.id in explicit_ids or segment.start <= visual.timestamp < segment.end
        ),
        key=lambda item: (item.timestamp, item.id),
    )


def _overlapping_observations(
    segment: SemanticSegment,
    observations: Sequence[AgentObservation],
) -> list[AgentObservation]:
    return [
        item for item in observations if item.end >= segment.start and item.start <= segment.end
    ]


def _has_procedural_claim(transcripts: Sequence[TranscriptSegment]) -> bool:
    return any(_PROCEDURAL_PATTERN.search(item.text) for item in transcripts)


def _gaps_for_segment(
    segment: SemanticSegment,
    *,
    transcripts: list[TranscriptSegment],
    visuals: list[VisualEvent],
    observations: list[AgentObservation],
) -> list[EvidenceGap]:
    gaps: list[EvidenceGap] = []
    transcript_ids = [item.id for item in transcripts]
    visual_ids = [item.id for item in visuals]
    active_observations = [
        item for item in observations if item.status != ObservationStatus.CONTRADICTED
    ]

    if not visuals:
        gaps.append(
            EvidenceGap(
                source_id=segment.source_id,
                semantic_segment_id=segment.id,
                gap_type=EvidenceGapType.NO_VISUAL_EVIDENCE,
                severity=EvidenceGapSeverity.WARNING,
                message=(
                    f"Section {segment.ordinal} '{segment.title}' has no retained visual evidence."
                ),
                related_transcript_ids=transcript_ids,
                suggested_next_action=(
                    f"Inspect or extract frames in {segment.start:g}-{segment.end:g}s, "
                    "then annotate visible state."
                ),
                start=segment.start,
                end=segment.end,
            )
        )

    claimed_types = {
        observation_type
        for visual in visuals
        if (observation_type := _VISUAL_TO_OBSERVATION_TYPE.get(visual.kind)) is not None
    }
    claim_transcript_ids: dict[ObservationType, list[str]] = {}
    for observation_type, pattern in _TRANSCRIPT_CLAIM_PATTERNS.items():
        matching_ids = [item.id for item in transcripts if pattern.search(item.text)]
        if matching_ids:
            claimed_types.add(observation_type)
            claim_transcript_ids[observation_type] = matching_ids
    observed_types = {item.type for item in active_observations}
    missing_types = sorted(claimed_types - observed_types, key=lambda item: item.value)
    if missing_types:
        missing_labels = ", ".join(item.value for item in missing_types)
        relevant_transcript_ids = [
            item.id
            for item in transcripts
            if any(
                item.id in claim_transcript_ids.get(observation_type, [])
                for observation_type in missing_types
            )
        ]
        relevant_visual_ids = [
            item.id
            for item in visuals
            if _VISUAL_TO_OBSERVATION_TYPE.get(item.kind) in missing_types
        ]
        gaps.append(
            EvidenceGap(
                source_id=segment.source_id,
                semantic_segment_id=segment.id,
                gap_type=EvidenceGapType.UNOBSERVED_CLAIM,
                severity=EvidenceGapSeverity.WARNING,
                message=(
                    f"Section {segment.ordinal} contains {missing_labels} evidence or claims "
                    "without a matching agent observation."
                ),
                related_transcript_ids=relevant_transcript_ids,
                related_visual_ids=relevant_visual_ids,
                missing_observation_types=missing_types,
                suggested_next_action=(
                    f"Request context for {segment.start:g}-{segment.end:g}s and annotate "
                    f"the missing types: {missing_labels}."
                ),
                start=segment.start,
                end=segment.end,
            )
        )

    duration = segment.end - segment.start
    if (
        duration >= LONG_PROCEDURAL_SECTION_SECONDS
        and transcripts
        and _has_procedural_claim(transcripts)
    ):
        timeline_visuals = sorted(
            item.timestamp for item in visuals if segment.start <= item.timestamp <= segment.end
        )
        timeline_points = [segment.start, *timeline_visuals, segment.end]
        maximum_uncovered_span = max(
            timeline_points[index + 1] - timeline_points[index]
            for index in range(len(timeline_points) - 1)
        )
        maximum_allowed_span = VISUAL_SAMPLE_SECONDS * 2
        if maximum_uncovered_span > maximum_allowed_span:
            gaps.append(
                EvidenceGap(
                    source_id=segment.source_id,
                    semantic_segment_id=segment.id,
                    gap_type=EvidenceGapType.WEAK_VISUAL_COVERAGE,
                    severity=EvidenceGapSeverity.ERROR,
                    message=(
                        f"Procedural section {segment.ordinal} spans {duration:g}s but has "
                        f"a {maximum_uncovered_span:g}s interval without retained visual "
                        f"evidence; the review limit is {maximum_allowed_span:g}s."
                    ),
                    related_transcript_ids=transcript_ids,
                    related_visual_ids=visual_ids,
                    suggested_next_action=(
                        f"Inspect additional frames across {segment.start:g}-{segment.end:g}s "
                        "at action and state-change timestamps."
                    ),
                    start=segment.start,
                    end=segment.end,
                )
            )
    return gaps


def detect_evidence_gaps(
    workspace: Workspace,
    *,
    source_id: str | None = None,
) -> list[EvidenceGap]:
    """Deterministically derive current evidence deficiencies without persisting them."""

    if source_id is not None:
        sources = [workspace.get_source(source_id)]
    else:
        sources = workspace.list_sources()
    gaps: list[EvidenceGap] = []
    for source in sources:
        transcripts = workspace.transcripts(source.id, limit=1_000_000)
        visuals = workspace.visuals(source.id)
        observations = workspace.observations(source.id, limit=1_000_000)
        transcript_by_id = {item.id: item for item in transcripts}
        visual_by_id = {item.id: item for item in visuals}
        segments = workspace.semantic_segments(source.id)
        retention = workspace.visual_retention_report(source.id)
        if retention is not None:
            gaps.extend(visual_retention_gaps(retention))
        for segment in segments:
            segment_transcripts = _segment_transcripts(segment, transcript_by_id)
            segment_visuals = _segment_visuals(segment, visual_by_id)
            segment_observations = _overlapping_observations(segment, observations)
            gaps.extend(
                _gaps_for_segment(
                    segment,
                    transcripts=segment_transcripts,
                    visuals=segment_visuals,
                    observations=segment_observations,
                )
            )

        for observation in observations:
            missing_visual_ids = [
                identifier for identifier in observation.frame_ids if identifier not in visual_by_id
            ]
            missing_transcript_ids = [
                identifier
                for identifier in observation.transcript_ids
                if identifier not in transcript_by_id
            ]
            missing_frame_grounding = not observation.frame_ids or bool(missing_visual_ids)
            missing_transcript_grounding = not observation.transcript_ids or bool(
                missing_transcript_ids
            )
            missing_grounding = [
                label
                for label, is_missing in (
                    ("frame", missing_frame_grounding),
                    ("transcript", missing_transcript_grounding),
                )
                if is_missing
            ]
            if not missing_grounding:
                continue
            matching_segment = next(
                (
                    segment
                    for segment in segments
                    if observation.end >= segment.start and observation.start <= segment.end
                ),
                None,
            )
            gaps.append(
                EvidenceGap(
                    source_id=source.id,
                    semantic_segment_id=(
                        matching_segment.id if matching_segment is not None else None
                    ),
                    gap_type=EvidenceGapType.UNGROUNDED_OBSERVATION,
                    severity=(
                        EvidenceGapSeverity.ERROR
                        if len(missing_grounding) == 2
                        else EvidenceGapSeverity.WARNING
                    ),
                    message=(
                        f"Observation '{observation.id}' is missing "
                        f"{' and '.join(missing_grounding)} grounding."
                    ),
                    related_transcript_ids=observation.transcript_ids,
                    related_visual_ids=observation.frame_ids,
                    related_observation_ids=[observation.id],
                    missing_transcript_ids=missing_transcript_ids,
                    missing_visual_ids=missing_visual_ids,
                    suggested_next_action=(
                        f"Request context around {observation.start:g}-{observation.end:g}s "
                        f"and attach {' and '.join(missing_grounding)} evidence."
                    ),
                    start=observation.start,
                    end=observation.end,
                )
            )
    return sorted(
        gaps,
        key=lambda item: (
            item.source_id,
            item.start,
            item.end,
            item.gap_type.value,
            item.id,
        ),
    )


def analyze_evidence_gaps(
    workspace: Workspace,
    *,
    source_id: str | None = None,
    persist: bool = True,
) -> list[EvidenceGap]:
    """Detect gaps and reconcile the durable gap set for each analyzed source."""

    gaps = detect_evidence_gaps(workspace, source_id=source_id)
    if not persist:
        return gaps
    source_ids = (
        [source_id] if source_id is not None else [source.id for source in workspace.list_sources()]
    )
    for current_source_id in source_ids:
        workspace.replace_gaps(
            current_source_id,
            [gap for gap in gaps if gap.source_id == current_source_id],
            preserve_resolution=True,
        )
    persisted = [
        gap
        for current_source_id in source_ids
        for gap in workspace.gaps(current_source_id, limit=None)
    ]
    return sorted(
        persisted,
        key=lambda item: (
            item.source_id,
            item.start,
            item.end,
            item.gap_type.value,
            item.id,
        ),
    )


# Short aliases keep CLI integrations readable while the explicit names remain public.
assemble_context = assemble_agent_context
analyze_gaps = analyze_evidence_gaps
