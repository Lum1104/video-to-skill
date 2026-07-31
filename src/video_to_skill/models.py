"""Typed interchange models shared by acquisition, processing, and generation."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, field_validator, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)


class SourcePlatform(StrEnum):
    LOCAL = "local"
    YOUTUBE = "youtube"
    BILIBILI = "bilibili"


class SourceKind(StrEnum):
    VIDEO = "video"
    COURSE = "course"


class JobState(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETE = "complete"
    PARTIAL = "partial"
    FAILED = "failed"


class ProcessingStage(StrEnum):
    INSPECT = "inspect"
    ACQUIRE = "acquire"
    TRANSCRIBE = "transcribe"
    VISUALS = "visuals"
    SEGMENT = "segment"
    COMPLETE = "complete"


class TranscriptOrigin(StrEnum):
    MANUAL_CAPTION = "manual-caption"
    AUTO_CAPTION = "auto-caption"
    ASR = "asr"


class VisualKind(StrEnum):
    SCENE = "scene"
    PERIODIC = "periodic"
    SLIDE = "slide"
    CODE = "code"
    UI = "ui"
    PHYSICAL = "physical"
    TALKING_HEAD = "talking-head"
    UNKNOWN = "unknown"


class VisualOrigin(StrEnum):
    """How a retained frame entered the evidence graph."""

    BASELINE = "baseline"
    INVESTIGATION = "investigation"


class WarningSeverity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


class ObservationType(StrEnum):
    SLIDE = "slide"
    CODE = "code"
    UI = "ui"
    PHYSICAL = "physical"
    ACTION = "action"
    TRANSITION = "transition"
    CONCEPT = "concept"
    UNKNOWN = "unknown"


class ObservationStatus(StrEnum):
    OBSERVED = "observed"
    INFERRED = "inferred"
    CONTRADICTED = "contradicted"


class EvidenceGapType(StrEnum):
    NO_VISUAL_EVIDENCE = "no-visual-evidence"
    UNOBSERVED_CLAIM = "unobserved-claim"
    UNGROUNDED_OBSERVATION = "ungrounded-observation"
    WEAK_VISUAL_COVERAGE = "weak-visual-coverage"


class EvidenceGapSeverity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


class InspectionEntryStatus(StrEnum):
    ACCESSIBLE = "accessible"
    INACCESSIBLE = "inaccessible"
    FAILED = "failed"


_EVIDENCE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{2,127}$")


def _stable_evidence_id(prefix: str, values: Mapping[str, Any]) -> str:
    serialized = json.dumps(
        values,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    digest = hashlib.sha256(serialized.encode("utf-8")).hexdigest()[:24]
    return f"{prefix}_{digest}"


def _validate_evidence_id(value: str) -> str:
    value = value.strip()
    if not _EVIDENCE_ID_RE.fullmatch(value):
        raise ValueError(
            "evidence id must be 3-128 characters containing only letters, numbers, "
            "periods, underscores, colons, or hyphens"
        )
    return value


def _deduplicate_ids(values: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        normalized = value.strip()
        if not normalized:
            raise ValueError("evidence references cannot be empty")
        if normalized not in seen:
            seen.add(normalized)
            result.append(normalized)
    return result


def _canonical_reference_ids(value: Any) -> Any:
    if isinstance(value, list):
        return sorted(value, key=str)
    return value


class Chapter(StrictModel):
    title: str
    start: float = Field(ge=0)
    end: float | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def end_after_start(self) -> Chapter:
        if self.end is not None and self.end < self.start:
            raise ValueError("chapter end must be after start")
        return self


class CaptionTrack(StrictModel):
    language: str
    extension: str
    url: str | None = None
    automatic: bool = False
    name: str | None = None


class SourceDescriptor(StrictModel):
    id: str
    platform: SourcePlatform
    kind: SourceKind = SourceKind.VIDEO
    locator: str
    canonical_url: str | None = None
    title: str
    creator: str | None = None
    description: str | None = None
    duration: float | None = Field(default=None, ge=0)
    language: str | None = None
    playlist_title: str | None = None
    playlist_index: int | None = Field(default=None, ge=1)
    chapters: list[Chapter] = Field(default_factory=list)
    captions: list[CaptionTrack] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class RetiredSource(StrictModel):
    source: SourceDescriptor
    removed_at: datetime
    reason: str


class InspectionEntry(StrictModel):
    """One expected item from an inspected input, including inaccessible tombstones."""

    ordinal: int = Field(ge=1)
    status: InspectionEntryStatus
    source_id: str | None = None
    remote_id: str | None = None
    title: str | None = None
    locator: str | None = None
    reason: str | None = None

    @field_validator("source_id", "remote_id", "title", "locator", "reason")
    @classmethod
    def compact_optional_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        compact = " ".join(value.split())
        return compact or None

    @model_validator(mode="after")
    def validate_status_fields(self) -> InspectionEntry:
        if self.status == InspectionEntryStatus.ACCESSIBLE:
            if self.source_id is None:
                raise ValueError("accessible inspection entries require a source id")
        elif self.reason is None:
            raise ValueError("inaccessible and failed inspection entries require a reason")
        return self


class InspectionCompleteness(StrictModel):
    """Auditable completeness result for one input inspection."""

    locator: str
    platform: SourcePlatform
    expected_entries: int | None = Field(default=None, ge=0)
    accessible_entries: int = Field(ge=0)
    inaccessible_entries: int = Field(ge=0)
    failed_entries: int = Field(ge=0)
    completeness_proven: bool
    disclaimer: str | None = None
    entries: list[InspectionEntry] = Field(default_factory=list)
    inspected_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @field_validator("locator", "disclaimer")
    @classmethod
    def nonempty_report_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        compact = " ".join(value.split())
        if not compact:
            raise ValueError("inspection report text cannot be empty")
        return compact

    @model_validator(mode="after")
    def validate_counts(self) -> InspectionCompleteness:
        counts = {
            InspectionEntryStatus.ACCESSIBLE: 0,
            InspectionEntryStatus.INACCESSIBLE: 0,
            InspectionEntryStatus.FAILED: 0,
        }
        for entry in self.entries:
            counts[entry.status] += 1
        recorded = (
            self.accessible_entries,
            self.inaccessible_entries,
            self.failed_entries,
        )
        actual = (
            counts[InspectionEntryStatus.ACCESSIBLE],
            counts[InspectionEntryStatus.INACCESSIBLE],
            counts[InspectionEntryStatus.FAILED],
        )
        if recorded != actual:
            raise ValueError("inspection entry counts do not match retained entry records")
        if self.expected_entries is not None and len(self.entries) != self.expected_entries:
            raise ValueError("inspection entries must account for every expected item")
        can_prove = (
            self.expected_entries is not None
            and self.accessible_entries == self.expected_entries
            and self.inaccessible_entries == 0
            and self.failed_entries == 0
        )
        if self.completeness_proven != can_prove:
            raise ValueError("completeness_proven is inconsistent with inspection counts")
        if not self.completeness_proven and self.disclaimer is None:
            raise ValueError("incomplete or unknown inspections require a disclaimer")
        return self


class SourceInspection(StrictModel):
    """Typed result that augments, but does not replace, the legacy source-list API."""

    sources: list[SourceDescriptor]
    completeness: InspectionCompleteness


class InspectionBatch(StrictModel):
    sources: list[SourceDescriptor] = Field(default_factory=list)
    reports: list[InspectionCompleteness] = Field(default_factory=list)


class MaterializedSource(StrictModel):
    source: SourceDescriptor
    directory: Path
    media_path: Path | None = None
    audio_path: Path | None = None
    caption_paths: list[Path] = Field(default_factory=list)
    metadata_path: Path | None = None
    content_hash: str


class TranscriptWord(StrictModel):
    text: str
    start: float = Field(ge=0)
    end: float = Field(ge=0)
    probability: float | None = Field(default=None, ge=0, le=1)

    @model_validator(mode="after")
    def end_after_start(self) -> TranscriptWord:
        if self.end < self.start:
            raise ValueError("word end must be after start")
        return self


class TranscriptSegment(StrictModel):
    id: str
    source_id: str
    start: float = Field(ge=0)
    end: float = Field(ge=0)
    text: str
    language: str | None = None
    speaker: str | None = None
    origin: TranscriptOrigin
    confidence: float | None = Field(default=None, ge=0, le=1)
    words: list[TranscriptWord] = Field(default_factory=list)

    @field_validator("text")
    @classmethod
    def nonempty_text(cls, value: str) -> str:
        value = " ".join(value.split())
        if not value:
            raise ValueError("transcript text cannot be empty")
        return value

    @model_validator(mode="after")
    def end_after_start(self) -> TranscriptSegment:
        if self.end < self.start:
            raise ValueError("segment end must be after start")
        return self


class VisualEvent(StrictModel):
    id: str
    source_id: str
    timestamp: float = Field(ge=0)
    path: Path
    kind: VisualKind = VisualKind.UNKNOWN
    origin: VisualOrigin = VisualOrigin.BASELINE
    scene_score: float | None = Field(default=None, ge=0)
    perceptual_hash: str | None = None
    ocr_text: str | None = None
    ocr_confidence: float | None = Field(default=None, ge=0, le=1)
    description: str | None = None
    confidence: float | None = Field(default=None, ge=0, le=1)


class SemanticSegment(StrictModel):
    id: str
    source_id: str
    ordinal: int = Field(ge=1)
    title: str
    start: float = Field(ge=0)
    end: float = Field(ge=0)
    transcript_ids: list[str] = Field(default_factory=list)
    visual_event_ids: list[str] = Field(default_factory=list)
    boundary_reasons: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def end_after_start(self) -> SemanticSegment:
        if self.end < self.start:
            raise ValueError("semantic segment end must be after start")
        return self


class ObservationProducer(StrictModel):
    """Auditable producer identity without storing private chain-of-thought."""

    name: str = Field(min_length=1, max_length=120)
    model: str | None = Field(default=None, max_length=160)
    version: str | None = Field(default=None, max_length=80)
    run_id: str | None = Field(default=None, max_length=160)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @field_validator("name", "model", "version", "run_id")
    @classmethod
    def compact_metadata(cls, value: str | None) -> str | None:
        if value is None:
            return None
        compact = " ".join(value.split())
        if not compact:
            raise ValueError("producer metadata values cannot be empty")
        return compact


class AgentObservation(StrictModel):
    """A concise, persisted assertion grounded in timeline evidence."""

    id: str = ""
    source_id: str = Field(min_length=1, max_length=256)
    start: float = Field(ge=0, allow_inf_nan=False)
    end: float = Field(ge=0, allow_inf_nan=False)
    type: ObservationType = Field(
        validation_alias=AliasChoices("type", "observation_type"),
    )
    claim: str = Field(min_length=1, max_length=1200)
    frame_ids: list[str] = Field(default_factory=list)
    transcript_ids: list[str] = Field(default_factory=list)
    confidence: float = Field(ge=0, le=1, allow_inf_nan=False)
    status: ObservationStatus = ObservationStatus.OBSERVED
    uncertainty: str | None = Field(default=None, max_length=600)
    producer: ObservationProducer
    evidence_rationale: str | None = Field(default=None, max_length=600)

    @model_validator(mode="before")
    @classmethod
    def derive_stable_id(cls, value: Any) -> Any:
        if not isinstance(value, Mapping):
            return value
        data = dict(value)
        if data.get("id"):
            return data
        claim = " ".join(str(data.get("claim", "")).split())
        observation_type = data.get("type", data.get("observation_type", "unknown"))
        start_value = data.get("start")
        end_value = data.get("end")
        start: Any = start_value
        end: Any = end_value
        try:
            if start_value is not None:
                start = float(start_value)
            if end_value is not None:
                end = float(end_value)
        except (TypeError, ValueError):
            pass
        data["id"] = _stable_evidence_id(
            "obs",
            {
                "source_id": data.get("source_id"),
                "start": start,
                "end": end,
                "type": observation_type,
                "claim": claim,
                "frame_ids": _canonical_reference_ids(data.get("frame_ids", [])),
                "transcript_ids": _canonical_reference_ids(data.get("transcript_ids", [])),
            },
        )
        return data

    @field_validator("id")
    @classmethod
    def valid_id(cls, value: str) -> str:
        return _validate_evidence_id(value)

    @field_validator("source_id", "claim", "uncertainty", "evidence_rationale")
    @classmethod
    def compact_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        compact = " ".join(value.split())
        if not compact:
            raise ValueError("observation text values cannot be empty")
        return compact

    @field_validator("frame_ids", "transcript_ids")
    @classmethod
    def unique_references(cls, value: list[str]) -> list[str]:
        return _deduplicate_ids(value)

    @model_validator(mode="after")
    def valid_interval(self) -> AgentObservation:
        if self.end < self.start:
            raise ValueError("observation end must be after start")
        return self


class EvidenceWindow(StrictModel):
    start: float = Field(ge=0, allow_inf_nan=False)
    end: float = Field(ge=0, allow_inf_nan=False)

    @model_validator(mode="after")
    def valid_interval(self) -> EvidenceWindow:
        if self.end <= self.start:
            raise ValueError("evidence window end must be after start")
        return self


class AgentContext(StrictModel):
    """A finite context packet suitable for one agent tool turn."""

    source: SourceDescriptor
    window: EvidenceWindow
    segments: list[SemanticSegment]
    transcripts: list[TranscriptSegment]
    visuals: list[VisualEvent]
    observations: list[AgentObservation]
    truncated: bool = False


class EvidenceGap(StrictModel):
    """A deterministic, actionable deficiency in the current evidence graph."""

    id: str = ""
    source_id: str = Field(min_length=1, max_length=256)
    semantic_segment_id: str | None = Field(default=None, max_length=128)
    gap_type: EvidenceGapType
    severity: EvidenceGapSeverity
    message: str = Field(min_length=1, max_length=1200)
    related_transcript_ids: list[str] = Field(default_factory=list)
    related_visual_ids: list[str] = Field(default_factory=list)
    related_observation_ids: list[str] = Field(default_factory=list)
    missing_observation_types: list[ObservationType] = Field(default_factory=list)
    missing_transcript_ids: list[str] = Field(default_factory=list)
    missing_visual_ids: list[str] = Field(default_factory=list)
    suggested_next_action: str = Field(min_length=1, max_length=1200)
    start: float = Field(ge=0, allow_inf_nan=False)
    end: float = Field(ge=0, allow_inf_nan=False)
    resolved: bool = False
    resolved_at: datetime | None = None
    resolution: str | None = Field(default=None, max_length=600)

    @model_validator(mode="before")
    @classmethod
    def derive_stable_id(cls, value: Any) -> Any:
        if not isinstance(value, Mapping):
            return value
        data = dict(value)
        if data.get("id"):
            return data
        gap_type = data.get("gap_type", "unknown")
        data["id"] = _stable_evidence_id(
            "gap",
            {
                "source_id": data.get("source_id"),
                "semantic_segment_id": data.get("semantic_segment_id"),
                "gap_type": gap_type,
                "start": data.get("start"),
                "end": data.get("end"),
                "missing_observation_types": _canonical_reference_ids(
                    data.get("missing_observation_types", [])
                ),
                "missing_transcript_ids": _canonical_reference_ids(
                    data.get("missing_transcript_ids", [])
                ),
                "missing_visual_ids": _canonical_reference_ids(data.get("missing_visual_ids", [])),
                "related_transcript_ids": _canonical_reference_ids(
                    data.get("related_transcript_ids", [])
                ),
                "related_visual_ids": _canonical_reference_ids(data.get("related_visual_ids", [])),
                "related_observation_ids": _canonical_reference_ids(
                    data.get("related_observation_ids", [])
                ),
            },
        )
        return data

    @field_validator("id")
    @classmethod
    def valid_id(cls, value: str) -> str:
        return _validate_evidence_id(value)

    @field_validator(
        "source_id",
        "semantic_segment_id",
        "message",
        "suggested_next_action",
        "resolution",
    )
    @classmethod
    def compact_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        compact = " ".join(value.split())
        if not compact:
            raise ValueError("gap text values cannot be empty")
        return compact

    @field_validator(
        "related_transcript_ids",
        "related_visual_ids",
        "related_observation_ids",
        "missing_transcript_ids",
        "missing_visual_ids",
    )
    @classmethod
    def unique_references(cls, value: list[str]) -> list[str]:
        return _deduplicate_ids(value)

    @field_validator("missing_observation_types")
    @classmethod
    def unique_missing_observation_types(
        cls,
        value: list[ObservationType],
    ) -> list[ObservationType]:
        return list(dict.fromkeys(value))

    @model_validator(mode="after")
    def valid_state(self) -> EvidenceGap:
        if self.end < self.start:
            raise ValueError("gap end must be after start")
        if not self.resolved and (self.resolved_at is not None or self.resolution is not None):
            raise ValueError("unresolved gaps cannot have resolution metadata")
        return self


class WarningRecord(StrictModel):
    code: str
    message: str
    severity: WarningSeverity = WarningSeverity.WARNING
    source_id: str | None = None
    stage: ProcessingStage | None = None


class JobManifest(StrictModel):
    schema_version: int = 1
    job_id: str
    state: JobState
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    inputs: list[str]
    workspace: Path
    configuration_hash: str
    output_language: str = "source"
    sources: list[SourceDescriptor] = Field(default_factory=list)
    warnings: list[WarningRecord] = Field(default_factory=list)
    failed_sources: list[str] = Field(default_factory=list)


class QueryResult(StrictModel):
    source: SourceDescriptor
    segments: list[SemanticSegment]
    transcripts: list[TranscriptSegment]
    visuals: list[VisualEvent]
