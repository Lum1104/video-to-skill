"""Deterministic rendering for the shareable course-skill layer.

The host agent performs semantic synthesis and produces a ``CourseSkillBlueprint``.
This module turns that bounded, evidence-linked blueprint into a portable skill
package. Raw media and the extraction workspace are deliberately out of scope.
"""

from __future__ import annotations

import hashlib
import json
import os
import posixpath
import re
import shutil
import tempfile
from collections import defaultdict
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Literal
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from PIL import Image, UnidentifiedImageError
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pydantic import ValidationError as PydanticValidationError

from video_to_skill import __version__
from video_to_skill.errors import ProcessingError
from video_to_skill.models import JobState, SourceDescriptor
from video_to_skill.url_security import UrlParameterLimitError, has_sensitive_url_parameters
from video_to_skill.utils import atomic_write_json, atomic_write_text, format_timestamp

if TYPE_CHECKING:
    from video_to_skill.workspace import Workspace

COURSE_SKILL_MARKER = "<!-- video-to-skill:course-skill:v2 -->"
_SKILL_NAME_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_MARKDOWN_TARGET_RE = re.compile(r"!?\[[^\]]*]\(([^)]+)\)")
_ROOT_ARTIFACTS = {
    "learning-path.md",
    "glossary.md",
    "patterns.md",
    "cheatsheet.md",
}
_SOURCE_IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}
MAX_COURSE_ASSETS = 24
MAX_ASSET_INPUT_BYTES = 20 * 1024 * 1024
MAX_ASSET_OUTPUT_BYTES = 20 * 1024 * 1024
MAX_ASSET_DIMENSION = 4096
MAX_ASSET_PIXELS = 16_000_000
MAX_WORKSPACE_LEDGER_ENTRIES = 10_000

SkillMode = Literal["learn", "practice", "apply", "reference"]
ArtifactDisclosure = Literal["normal", "after-attempt"]
VisualAssetPresentation = Literal["frame", "crop", "sequence"]
Confidence = Literal["high", "medium", "low"]
CoverageStatus = Literal["complete", "partial", "failed", "skipped"]
EvidenceModality = Literal["speech", "visual", "ocr", "metadata", "temporal"]
CapabilityLevel = Literal["strong", "medium", "light", "unsupported"]
SemanticMateriality = Literal["core", "supporting", "contextual", "incidental"]
SemanticDisposition = Literal["included", "merged", "context-only", "omitted"]
SemanticUnitKind = Literal[
    "question",
    "claim",
    "reason",
    "example",
    "analogy",
    "definition",
    "distinction",
    "qualification",
    "counterpoint",
    "prediction",
    "recommendation",
    "warning",
    "value-judgment",
    "open-question",
]
SemanticRelationKind = Literal[
    "answers",
    "supports",
    "explains",
    "exemplifies",
    "qualifies",
    "contrasts-with",
    "depends-on",
    "updates",
    "contradicts",
    "raises",
    "leaves-unresolved",
]
CurriculumKind = Literal[
    "source-faithful",
    "thematic",
    "application-first",
    "custom",
]
CourseCoverageState = Literal["complete", "partial", "unproven"]
CourseLedgerEntryKind = Literal["active-source", "retired-source", "inspection-entry"]
CourseLedgerEntryStatus = Literal["accessible", "retired", "inaccessible", "failed"]


class GenerationModel(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)


def _compact(value: str) -> str:
    return " ".join(value.split())


def _compact_required(value: str) -> str:
    compact = _compact(value)
    if not compact:
        raise ValueError("value cannot be empty")
    return compact


def _safe_artifact_path(value: str) -> str:
    path = PurePosixPath(value.strip())
    if path.is_absolute() or ".." in path.parts or "\\" in value:
        raise ValueError("artifact path must be a safe relative POSIX path")
    if path.suffix.casefold() != ".md":
        raise ValueError("course artifacts must be Markdown files")
    if len(path.parts) == 1:
        if path.as_posix() not in _ROOT_ARTIFACTS:
            allowed = ", ".join(sorted(_ROOT_ARTIFACTS))
            raise ValueError(f"root artifact must be one of: {allowed}")
    else:
        if len(path.parts) > 4 or path.parts[0] in {"assets", "."}:
            raise ValueError("artifact collection path is unsafe or too deeply nested")
        if any(not re.fullmatch(r"[a-z0-9][a-z0-9._-]*", part) for part in path.parts):
            raise ValueError(
                "artifact paths must use lowercase letters, digits, dots, hyphens, or underscores"
            )
    return path.as_posix()


class CourseSkillSource(GenerationModel):
    id: str = Field(min_length=1, max_length=256)
    title: str = Field(min_length=1, max_length=500)
    creator: str | None = Field(default=None, max_length=300)
    platform: str = Field(min_length=1, max_length=80)
    url: str | None = Field(default=None, max_length=4000)
    coverage: CoverageStatus
    notes: str | None = Field(default=None, max_length=1000)

    @field_validator("id", "title", "creator", "platform", "notes")
    @classmethod
    def compact_text(cls, value: str | None) -> str | None:
        return _compact_required(value) if value is not None else None

    @field_validator("url")
    @classmethod
    def public_url_only(cls, value: str | None) -> str | None:
        if value is None:
            return None
        parsed = urlparse(value)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("source URL must be a public HTTP(S) URL")
        if parsed.username is not None or parsed.password is not None:
            raise ValueError("source URL cannot contain userinfo or embedded credentials")
        try:
            sensitive = has_sensitive_url_parameters(value)
        except UrlParameterLimitError as exc:
            raise ValueError(
                "source URL exceeds the bounded query/fragment parameter limit"
            ) from exc
        if sensitive:
            raise ValueError(
                "source URL contains a sensitive or temporary query/fragment parameter; "
                "use the stable public source URL"
            )
        return value


class CourseInspectionLedger(GenerationModel):
    """Sanitized, immutable accounting for one persisted input inspection."""

    key: str = Field(min_length=8, max_length=96)
    platform: str = Field(min_length=1, max_length=80)
    expected_entries: int | None = Field(default=None, ge=0)
    accessible_entries: int = Field(ge=0)
    inaccessible_entries: int = Field(ge=0)
    failed_entries: int = Field(ge=0)
    completeness_proven: bool
    disclaimer_digest: str | None = Field(default=None, pattern=r"^[a-f0-9]{24}$")


class CourseCoverageLedgerEntry(GenerationModel):
    """One source or expected inspection item in the workspace snapshot."""

    key: str = Field(min_length=8, max_length=96)
    kind: CourseLedgerEntryKind
    status: CourseLedgerEntryStatus
    source_id: str | None = Field(default=None, min_length=1, max_length=256)
    title: str = Field(min_length=1, max_length=500)
    platform: str = Field(min_length=1, max_length=80)
    workspace_coverage: CoverageStatus | None = None
    inspection_key: str | None = Field(default=None, min_length=8, max_length=96)
    ordinal: int | None = Field(default=None, ge=1)
    detail_digest: str = Field(pattern=r"^[a-f0-9]{24}$")

    @field_validator("source_id", "title", "platform")
    @classmethod
    def compact_entry_text(cls, value: str | None) -> str | None:
        return _compact_required(value) if value is not None else None

    @model_validator(mode="after")
    def coherent_entry(self) -> CourseCoverageLedgerEntry:
        if self.kind == "active-source":
            if self.status != "accessible" or self.source_id is None:
                raise ValueError("active source entries must be accessible and name a source_id")
            if self.workspace_coverage is None:
                raise ValueError("active source entries require workspace_coverage")
        elif self.kind == "retired-source":
            if self.status != "retired" or self.source_id is None:
                raise ValueError("retired source entries must be retired and name a source_id")
            if self.workspace_coverage != "skipped":
                raise ValueError("retired source entries must use skipped workspace coverage")
        else:
            if self.inspection_key is None or self.ordinal is None:
                raise ValueError("inspection entries require an inspection key and ordinal")
            if self.workspace_coverage is not None:
                raise ValueError("inspection entries do not declare workspace coverage")
        return self


class CourseCoverageLedger(GenerationModel):
    """Exact, privacy-preserving snapshot used to prove full-course accounting."""

    schema_version: Literal[1] = 1
    workspace_job_id: str = Field(min_length=1, max_length=256)
    input_count: int = Field(ge=0)
    input_fingerprint: str = Field(pattern=r"^[a-f0-9]{24}$")
    state: CourseCoverageState
    inspections: list[CourseInspectionLedger] = Field(
        default_factory=list,
        max_length=MAX_WORKSPACE_LEDGER_ENTRIES,
    )
    entries: list[CourseCoverageLedgerEntry] = Field(
        default_factory=list,
        max_length=MAX_WORKSPACE_LEDGER_ENTRIES,
    )

    @model_validator(mode="after")
    def unique_ledger_keys(self) -> CourseCoverageLedger:
        inspection_keys = [item.key for item in self.inspections]
        if len(inspection_keys) != len(set(inspection_keys)):
            raise ValueError("course coverage inspection keys must be unique")
        entry_keys = [item.key for item in self.entries]
        if len(entry_keys) != len(set(entry_keys)):
            raise ValueError("course coverage entry keys must be unique")
        known_inspections = set(inspection_keys)
        unknown = {
            item.inspection_key
            for item in self.entries
            if item.inspection_key is not None and item.inspection_key not in known_inspections
        }
        if unknown:
            raise ValueError("course coverage entries reference unknown inspections")
        return self


class ClaimEvidence(GenerationModel):
    source_id: str = Field(min_length=1, max_length=256)
    start: float = Field(ge=0, allow_inf_nan=False)
    end: float = Field(ge=0, allow_inf_nan=False)
    modalities: list[EvidenceModality] = Field(min_length=1)
    evidence_ids: list[str] = Field(min_length=1)

    @field_validator("source_id")
    @classmethod
    def compact_source_id(cls, value: str) -> str:
        return _compact_required(value)

    @field_validator("evidence_ids")
    @classmethod
    def unique_evidence_ids(cls, value: list[str]) -> list[str]:
        compact = [_compact_required(item) for item in value]
        if len(compact) != len(set(compact)):
            raise ValueError("evidence_ids cannot contain duplicates")
        return compact

    @field_validator("modalities")
    @classmethod
    def unique_modalities(cls, value: list[EvidenceModality]) -> list[EvidenceModality]:
        if len(value) != len(set(value)):
            raise ValueError("modalities cannot contain duplicates")
        return value

    @model_validator(mode="after")
    def valid_interval(self) -> ClaimEvidence:
        if self.end < self.start:
            raise ValueError("evidence end must be after start")
        return self


class CourseSkillClaim(GenerationModel):
    id: str = Field(min_length=1, max_length=160)
    file: str
    anchor: str | None = Field(default=None, max_length=200)
    kind: str = Field(min_length=1, max_length=120)
    summary: str = Field(min_length=1, max_length=1200)
    inferred: bool
    confidence: Confidence
    semantic_unit_ids: list[str] = Field(min_length=1)
    evidence: list[ClaimEvidence] = Field(min_length=1)

    @field_validator("id", "kind", "summary", "anchor")
    @classmethod
    def compact_text(cls, value: str | None) -> str | None:
        return _compact_required(value) if value is not None else None

    @field_validator("file")
    @classmethod
    def safe_claim_file(cls, value: str) -> str:
        if value in {"SKILL.md", "source-map.md", "sources.md"}:
            return value
        return _safe_artifact_path(value)

    @field_validator("semantic_unit_ids")
    @classmethod
    def compact_semantic_unit_ids(cls, value: list[str]) -> list[str]:
        compact = [_compact_required(item) for item in value]
        if len(compact) != len(set(compact)):
            raise ValueError("claim semantic_unit_ids cannot contain duplicates")
        return compact


class CourseArtifact(GenerationModel):
    id: str = Field(min_length=1, max_length=160)
    path: str
    title: str = Field(min_length=1, max_length=300)
    modes: list[SkillMode] = Field(min_length=1, max_length=4)
    disclosure: ArtifactDisclosure
    use_when: str = Field(min_length=1, max_length=500)
    independent_loading_reason: str = Field(min_length=1, max_length=500)
    semantic_unit_ids: list[str] = Field(min_length=1)
    topics: list[str] = Field(default_factory=list, max_length=30)
    content: str = Field(min_length=1, max_length=500_000)

    @field_validator("path")
    @classmethod
    def safe_path(cls, value: str) -> str:
        return _safe_artifact_path(value)

    @field_validator("id", "title", "use_when", "independent_loading_reason")
    @classmethod
    def compact_text(cls, value: str) -> str:
        return _compact_required(value)

    @field_validator("modes")
    @classmethod
    def unique_modes(cls, value: list[SkillMode]) -> list[SkillMode]:
        if len(value) != len(set(value)):
            raise ValueError("artifact modes cannot contain duplicates")
        return value

    @field_validator("topics", "semantic_unit_ids")
    @classmethod
    def compact_unique_lists(cls, value: list[str]) -> list[str]:
        compact = [_compact_required(item) for item in value]
        if len(compact) != len(set(compact)):
            raise ValueError("artifact list values cannot contain duplicates")
        return compact


class NormalizedCrop(GenerationModel):
    """One normalized crop box over a source frame."""

    left: float = Field(ge=0, lt=1)
    top: float = Field(ge=0, lt=1)
    right: float = Field(gt=0, le=1)
    bottom: float = Field(gt=0, le=1)

    @model_validator(mode="after")
    def positive_area(self) -> NormalizedCrop:
        if self.right <= self.left or self.bottom <= self.top:
            raise ValueError("normalized crop must have positive width and height")
        if self.right - self.left < 0.02 or self.bottom - self.top < 0.02:
            raise ValueError("normalized crop is too small to remain useful")
        return self


class VisualAssetCandidate(GenerationModel):
    """One evidence-grounded visual proposed by Analyze for deterministic materialization."""

    id: str = Field(
        min_length=1,
        max_length=80,
        pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$",
    )
    source_id: str = Field(min_length=1, max_length=160)
    evidence_ids: list[str] = Field(min_length=1, max_length=4)
    semantic_unit_ids: list[str] = Field(min_length=1, max_length=20)
    presentation: VisualAssetPresentation
    crop: NormalizedCrop | None = None
    description: str = Field(min_length=1, max_length=500)
    teaching_value: str = Field(min_length=1, max_length=1000)

    @field_validator(
        "source_id",
        "description",
        "teaching_value",
    )
    @classmethod
    def compact_text(cls, value: str) -> str:
        return _compact_required(value)

    @field_validator("evidence_ids", "semantic_unit_ids")
    @classmethod
    def unique_ids(cls, value: list[str]) -> list[str]:
        compact = [_compact_required(item) for item in value]
        if len(compact) != len(set(compact)):
            raise ValueError("visual asset candidate IDs must be unique")
        return compact

    @model_validator(mode="after")
    def coherent_presentation(self) -> VisualAssetCandidate:
        if self.presentation == "frame":
            if len(self.evidence_ids) != 1 or self.crop is not None:
                raise ValueError("frame candidates require one evidence ID and no crop")
        elif self.presentation == "crop":
            if len(self.evidence_ids) != 1 or self.crop is None:
                raise ValueError("crop candidates require one evidence ID and one crop")
        elif len(self.evidence_ids) < 2:
            raise ValueError("sequence candidates require two to four evidence IDs")
        return self


class CourseAsset(GenerationModel):
    """One indispensable, sanitized visual copied from the private workspace."""

    path: str
    source_path: Path
    description: str = Field(min_length=1, max_length=500)
    used_by: list[str] = Field(min_length=1, max_length=20)
    claim_ids: list[str] = Field(min_length=1, max_length=20)

    @field_validator("path")
    @classmethod
    def safe_destination(cls, value: str) -> str:
        path = PurePosixPath(value.strip())
        if (
            path.is_absolute()
            or len(path.parts) != 2
            or path.parts[0] != "assets"
            or ".." in path.parts
            or "\\" in value
            or path.suffix.casefold() != ".png"
        ):
            raise ValueError("asset destination must be `assets/<safe-name>.png`")
        if not re.fullmatch(r"[a-z0-9][a-z0-9._-]*\.png", path.name):
            raise ValueError("asset filename must use lowercase letters, digits, dots, or hyphens")
        return path.as_posix()

    @field_validator("description")
    @classmethod
    def compact_description(cls, value: str) -> str:
        return _compact_required(value)

    @field_validator("used_by")
    @classmethod
    def safe_used_by(cls, value: list[str]) -> list[str]:
        compact = [_safe_artifact_path(item) for item in value]
        if len(compact) != len(set(compact)):
            raise ValueError("asset used_by paths cannot contain duplicates")
        return compact

    @field_validator("claim_ids")
    @classmethod
    def compact_claim_ids(cls, value: list[str]) -> list[str]:
        compact = [_compact_required(item) for item in value]
        if len(compact) != len(set(compact)):
            raise ValueError("asset claim_ids cannot contain duplicates")
        return compact


def _artifact_uses_asset(artifact: CourseArtifact, asset_path: str) -> bool:
    artifact_directory = posixpath.dirname(artifact.path)
    for match in _MARKDOWN_TARGET_RE.finditer(artifact.content):
        target = match.group(1).strip().split("#", 1)[0]
        if not target or urlparse(target).scheme:
            continue
        normalized = posixpath.normpath(posixpath.join(artifact_directory, target))
        if normalized == asset_path:
            return True
    return False


class CorePrinciple(GenerationModel):
    text: str = Field(min_length=1, max_length=600)
    claim_id: str = Field(min_length=1, max_length=160)

    @field_validator("text", "claim_id")
    @classmethod
    def compact_text(cls, value: str) -> str:
        return _compact_required(value)


class SemanticUnit(GenerationModel):
    """One high-recall, source-ordered unit retained before curriculum design."""

    id: str = Field(min_length=1, max_length=160)
    source_id: str = Field(min_length=1, max_length=256)
    start: float = Field(ge=0, allow_inf_nan=False)
    end: float = Field(ge=0, allow_inf_nan=False)
    speaker: str | None = Field(default=None, max_length=300)
    kind: SemanticUnitKind
    summary: str = Field(min_length=1, max_length=1200)
    detail: str | None = Field(default=None, max_length=5000)
    materiality: SemanticMateriality
    disposition: SemanticDisposition
    disposition_reason: str | None = Field(default=None, max_length=1000)
    merged_into: str | None = Field(default=None, max_length=160)
    inferred: bool
    confidence: Confidence
    modalities: list[EvidenceModality] = Field(min_length=1)
    evidence_ids: list[str] = Field(min_length=1)

    @field_validator(
        "id",
        "source_id",
        "speaker",
        "summary",
        "detail",
        "disposition_reason",
        "merged_into",
    )
    @classmethod
    def compact_text(cls, value: str | None) -> str | None:
        return _compact_required(value) if value is not None else None

    @field_validator("modalities")
    @classmethod
    def unique_modalities(cls, value: list[EvidenceModality]) -> list[EvidenceModality]:
        if len(value) != len(set(value)):
            raise ValueError("semantic unit modalities cannot contain duplicates")
        return value

    @field_validator("evidence_ids")
    @classmethod
    def unique_evidence_ids(cls, value: list[str]) -> list[str]:
        compact = [_compact_required(item) for item in value]
        if len(compact) != len(set(compact)):
            raise ValueError("semantic unit evidence_ids cannot contain duplicates")
        return compact

    @model_validator(mode="after")
    def coherent_unit(self) -> SemanticUnit:
        if self.end < self.start:
            raise ValueError("semantic unit end must be after start")
        if self.disposition == "merged":
            if self.merged_into is None:
                raise ValueError("merged semantic units require merged_into")
        elif self.merged_into is not None:
            raise ValueError("only merged semantic units can declare merged_into")
        if self.disposition != "included" and self.disposition_reason is None:
            raise ValueError("merged, context-only, and omitted semantic units require a reason")
        return self


class SemanticRelation(GenerationModel):
    from_unit_id: str = Field(min_length=1, max_length=160)
    to_unit_id: str = Field(min_length=1, max_length=160)
    kind: SemanticRelationKind
    explanation: str | None = Field(default=None, max_length=1000)

    @field_validator("from_unit_id", "to_unit_id", "explanation")
    @classmethod
    def compact_text(cls, value: str | None) -> str | None:
        return _compact_required(value) if value is not None else None

    @model_validator(mode="after")
    def distinct_units(self) -> SemanticRelation:
        if self.from_unit_id == self.to_unit_id:
            raise ValueError("semantic relations must connect two different units")
        return self


class CapabilityProfile(GenerationModel):
    learn: CapabilityLevel
    practice: CapabilityLevel
    apply: CapabilityLevel
    reference: CapabilityLevel
    rationale: str = Field(min_length=1, max_length=1200)

    @field_validator("rationale")
    @classmethod
    def compact_rationale(cls, value: str) -> str:
        return _compact_required(value)


class CurriculumPath(GenerationModel):
    id: str = Field(min_length=1, max_length=160)
    title: str = Field(min_length=1, max_length=300)
    kind: CurriculumKind
    use_when: str = Field(min_length=1, max_length=500)
    artifact_ids: list[str] = Field(min_length=1)

    @field_validator("id", "title", "use_when")
    @classmethod
    def compact_text(cls, value: str) -> str:
        return _compact_required(value)

    @field_validator("artifact_ids")
    @classmethod
    def unique_artifact_ids(cls, value: list[str]) -> list[str]:
        compact = [_compact_required(item) for item in value]
        if len(compact) != len(set(compact)):
            raise ValueError("curriculum path artifact_ids cannot contain duplicates")
        return compact


class CurriculumDesign(GenerationModel):
    selected_path_id: str = Field(min_length=1, max_length=160)
    rationale: str = Field(min_length=1, max_length=1200)
    paths: list[CurriculumPath] = Field(min_length=1)

    @field_validator("selected_path_id", "rationale")
    @classmethod
    def compact_text(cls, value: str) -> str:
        return _compact_required(value)

    @model_validator(mode="after")
    def coherent_paths(self) -> CurriculumDesign:
        path_ids = [path.id for path in self.paths]
        if len(path_ids) != len(set(path_ids)):
            raise ValueError("curriculum path ids must be unique")
        if self.selected_path_id not in set(path_ids):
            raise ValueError("selected_path_id must identify a curriculum path")
        return self


class CourseInteraction(GenerationModel):
    welcome: str = Field(min_length=1, max_length=360)
    starter_questions: list[str] = Field(min_length=1, max_length=3)

    @field_validator("welcome")
    @classmethod
    def compact_welcome(cls, value: str) -> str:
        compact = _compact_required(value)
        sentence_count = len(re.findall(r"[.!?\u3002\uff01\uff1f](?:\s|$)", compact))
        if sentence_count > 2:
            raise ValueError("empty-invocation welcome must use at most two sentences")
        if "start" not in compact.casefold():
            raise ValueError("empty-invocation welcome must offer `start`")
        return compact

    @field_validator("starter_questions")
    @classmethod
    def compact_questions(cls, value: list[str]) -> list[str]:
        compact = [_compact_required(item) for item in value]
        if len(compact) != len(set(compact)):
            raise ValueError("starter questions cannot contain duplicates")
        return compact


class CourseSkillBlueprint(GenerationModel):
    """The semantic handoff between the generator agent and package renderer."""

    schema_version: Literal[2] = 2
    name: str = Field(min_length=1, max_length=64)
    title: str = Field(min_length=1, max_length=300)
    description: str = Field(min_length=1, max_length=1024)
    scope: str = Field(min_length=1, max_length=1000)
    artifact_language: str = Field(min_length=2, max_length=80)
    interaction: CourseInteraction
    capability_profile: CapabilityProfile
    curriculum: CurriculumDesign
    prerequisites: list[str] = Field(default_factory=list, max_length=30)
    core_principles: list[CorePrinciple] = Field(default_factory=list, max_length=24)
    semantic_units: list[SemanticUnit] = Field(min_length=1)
    semantic_relations: list[SemanticRelation] = Field(default_factory=list)
    artifacts: list[CourseArtifact] = Field(default_factory=list)
    assets: list[CourseAsset] = Field(default_factory=list, max_length=MAX_COURSE_ASSETS)
    sources: list[CourseSkillSource] = Field(min_length=1)
    coverage_ledger: CourseCoverageLedger | None = None
    claims: list[CourseSkillClaim] = Field(min_length=1)
    limitations: list[str] = Field(default_factory=list, max_length=30)
    parent_build_id: str | None = Field(
        default=None,
        pattern=r"^v2s-[a-f0-9]{20}$",
    )

    @field_validator("name")
    @classmethod
    def valid_name(cls, value: str) -> str:
        if not _SKILL_NAME_RE.fullmatch(value):
            raise ValueError("skill name must use lowercase letters, digits, and hyphens")
        return value

    @field_validator("title", "description", "scope", "artifact_language")
    @classmethod
    def compact_text(cls, value: str) -> str:
        return _compact_required(value)

    @field_validator("prerequisites", "limitations")
    @classmethod
    def compact_lists(cls, value: list[str]) -> list[str]:
        compact = [_compact_required(item) for item in value]
        if len(compact) != len(set(compact)):
            raise ValueError("list values cannot contain duplicates")
        return compact

    @model_validator(mode="after")
    def coherent_graph(self) -> CourseSkillBlueprint:
        source_ids = [item.id for item in self.sources]
        if len(source_ids) != len(set(source_ids)):
            raise ValueError("source ids must be unique")

        artifact_paths = [item.path for item in self.artifacts]
        if len(artifact_paths) != len(set(artifact_paths)):
            raise ValueError("artifact paths must be unique")
        artifact_ids = [item.id for item in self.artifacts]
        if len(artifact_ids) != len(set(artifact_ids)):
            raise ValueError("artifact ids must be unique")
        known_artifacts = set(artifact_ids)
        withheld_artifact_ids = {
            artifact.id for artifact in self.artifacts if artifact.disclosure == "after-attempt"
        }
        for path in self.curriculum.paths:
            unknown_artifacts = set(path.artifact_ids) - known_artifacts
            if unknown_artifacts:
                raise ValueError(
                    f"curriculum path '{path.id}' references unknown artifacts: "
                    + ", ".join(sorted(unknown_artifacts))
                )
            if leaked_artifacts := set(path.artifact_ids) & withheld_artifact_ids:
                raise ValueError(
                    f"curriculum path '{path.id}' cannot index after-attempt artifacts: "
                    + ", ".join(sorted(leaked_artifacts))
                )
        withheld_artifacts = [
            artifact for artifact in self.artifacts if artifact.disclosure == "after-attempt"
        ]
        if withheld_artifacts and not any(
            artifact.disclosure == "normal" and "practice" in artifact.modes
            for artifact in self.artifacts
        ):
            raise ValueError(
                "after-attempt artifacts require at least one separate, normally disclosed "
                "practice artifact"
            )

        semantic_ids = [unit.id for unit in self.semantic_units]
        if len(semantic_ids) != len(set(semantic_ids)):
            raise ValueError("semantic unit ids must be unique")
        known_semantic_units = set(semantic_ids)
        semantic_units_by_id = {unit.id: unit for unit in self.semantic_units}
        for unit in self.semantic_units:
            if unit.source_id not in set(source_ids):
                raise ValueError(
                    f"semantic unit '{unit.id}' references unknown source '{unit.source_id}'"
                )
            if unit.merged_into is not None and unit.merged_into not in known_semantic_units:
                raise ValueError(
                    f"semantic unit '{unit.id}' merges into unknown unit '{unit.merged_into}'"
                )
        merge_terminals: dict[str, str] = {}
        for unit in self.semantic_units:
            if unit.disposition != "merged":
                continue
            current = unit
            visited: set[str] = set()
            while current.disposition == "merged":
                if current.id in visited:
                    raise ValueError(
                        f"semantic unit merge chain contains a cycle at '{current.id}'"
                    )
                visited.add(current.id)
                assert current.merged_into is not None
                current = semantic_units_by_id[current.merged_into]
            if current.disposition != "included":
                raise ValueError(
                    f"semantic unit '{unit.id}' merge chain must terminate at an included unit"
                )
            merge_terminals[unit.id] = current.id
        relation_keys: set[tuple[str, str, str]] = set()
        for relation in self.semantic_relations:
            if (
                relation.from_unit_id not in known_semantic_units
                or relation.to_unit_id not in known_semantic_units
            ):
                raise ValueError("semantic relations must reference known semantic units")
            key = (relation.from_unit_id, relation.to_unit_id, relation.kind)
            if key in relation_keys:
                raise ValueError("semantic relations cannot contain duplicates")
            relation_keys.add(key)

        represented_units: set[str] = set()
        for artifact in self.artifacts:
            unknown_units = set(artifact.semantic_unit_ids) - known_semantic_units
            if unknown_units:
                raise ValueError(
                    f"artifact '{artifact.id}' references unknown semantic units: "
                    + ", ".join(sorted(unknown_units))
                )
            represented_units.update(artifact.semantic_unit_ids)
        missing_material = {
            unit.id
            for unit in self.semantic_units
            if unit.materiality in {"core", "supporting"}
            and unit.disposition == "included"
            and unit.id not in represented_units
        }
        if missing_material:
            raise ValueError(
                "included core or supporting semantic units need a course artifact: "
                + ", ".join(sorted(missing_material))
            )
        unrepresented_merge_targets = set(merge_terminals.values()) - represented_units
        if unrepresented_merge_targets:
            raise ValueError(
                "semantic unit merge chains must terminate at artifact-linked units: "
                + ", ".join(sorted(unrepresented_merge_targets))
            )

        rendered_files = {"SKILL.md", "source-map.md", "sources.md", *artifact_paths}

        claim_ids = [item.id for item in self.claims]
        if len(claim_ids) != len(set(claim_ids)):
            raise ValueError("claim ids must be unique")
        known_claims = set(claim_ids)
        known_sources = set(source_ids)
        for claim in self.claims:
            if claim.file not in rendered_files:
                raise ValueError(f"claim '{claim.id}' points to an unknown rendered file")
            unknown_units = set(claim.semantic_unit_ids) - known_semantic_units
            if unknown_units:
                raise ValueError(
                    f"claim '{claim.id}' references unknown semantic units: "
                    + ", ".join(sorted(unknown_units))
                )
            for evidence in claim.evidence:
                if evidence.source_id not in known_sources:
                    raise ValueError(
                        f"claim '{claim.id}' references unknown source '{evidence.source_id}'"
                    )
        claimed_files = {claim.file for claim in self.claims}
        if ungrounded_artifacts := set(artifact_paths) - claimed_files:
            raise ValueError(
                "every course artifact needs at least one provenance claim; missing: "
                + ", ".join(sorted(ungrounded_artifacts))
            )
        asset_paths = [asset.path for asset in self.assets]
        if len(asset_paths) != len(set(asset_paths)):
            raise ValueError("asset destination paths must be unique")
        artifacts_by_path = {artifact.path: artifact for artifact in self.artifacts}
        claims_by_id = {claim.id: claim for claim in self.claims}
        for asset in self.assets:
            for used_by in asset.used_by:
                linked_artifact = artifacts_by_path.get(used_by)
                if linked_artifact is None:
                    raise ValueError(
                        f"asset '{asset.path}' is used by unknown artifact '{used_by}'"
                    )
                if not _artifact_uses_asset(linked_artifact, asset.path):
                    raise ValueError(f"artifact '{used_by}' does not link to asset '{asset.path}'")
            grounded = False
            for claim_id in asset.claim_ids:
                linked_claim = claims_by_id.get(claim_id)
                if linked_claim is None:
                    raise ValueError(f"asset '{asset.path}' references unknown claim '{claim_id}'")
                if linked_claim.file not in asset.used_by:
                    continue
                if any(
                    {"visual", "temporal"} & set(evidence.modalities)
                    for evidence in linked_claim.evidence
                ):
                    grounded = True
            if not grounded:
                raise ValueError(
                    f"asset '{asset.path}' needs a visual or temporal claim for an artifact "
                    "that links to it"
                )
        for principle in self.core_principles:
            if principle.claim_id not in known_claims:
                raise ValueError(f"core principle references unknown claim '{principle.claim_id}'")
            claim = next(item for item in self.claims if item.id == principle.claim_id)
            if claim.file != "SKILL.md":
                raise ValueError(
                    f"core principle claim '{principle.claim_id}' must render in SKILL.md"
                )
        return self


def _ledger_digest(value: object) -> str:
    serialized = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()[:24]


def _canonical_input_locator(value: str) -> str:
    parsed = urlparse(value)
    if parsed.scheme in {"http", "https"} and parsed.netloc:
        return value
    return str(Path(value).expanduser().resolve(strict=False))


def _safe_workspace_public_url(value: str | None) -> str | None:
    if value is None:
        return None
    try:
        return CourseSkillSource(
            id="url-safety-check",
            title="URL safety check",
            platform="workspace",
            url=value,
            coverage="partial",
        ).url
    except PydanticValidationError:
        return None


def _workspace_source_coverage(
    source_id: str,
    *,
    processed_source_ids: set[str],
    failed_source_ids: set[str],
) -> CoverageStatus:
    if source_id in failed_source_ids:
        return "failed"
    return "complete" if source_id in processed_source_ids else "partial"


def _course_source_from_workspace(
    source: SourceDescriptor,
    coverage: CoverageStatus,
) -> CourseSkillSource:
    return CourseSkillSource(
        id=source.id,
        title=source.title,
        creator=source.creator,
        platform=source.platform.value,
        url=_safe_workspace_public_url(source.canonical_url),
        coverage=coverage,
    )


def coverage_ledger_from_workspace(workspace: Workspace) -> CourseCoverageLedger:
    """Build the exact sanitized full-course contract for a persisted workspace."""

    manifest = workspace.load_manifest()
    active_sources = workspace.list_sources()
    retired_sources = workspace.list_retired_sources()
    inspections = workspace.inspection_reports()
    with workspace.connect() as connection:
        processed_source_ids = {
            str(row["source_id"])
            for row in connection.execute(
                "SELECT DISTINCT source_id FROM semantic_segments"
            ).fetchall()
        }
    failed_source_ids = set(manifest.failed_sources)

    entries: list[CourseCoverageLedgerEntry] = []
    active_coverages: dict[str, CoverageStatus] = {}
    for source in active_sources:
        coverage = _workspace_source_coverage(
            source.id,
            processed_source_ids=processed_source_ids,
            failed_source_ids=failed_source_ids,
        )
        active_coverages[source.id] = coverage
        entries.append(
            CourseCoverageLedgerEntry(
                key=f"active-{_ledger_digest(source.id)}",
                kind="active-source",
                status="accessible",
                source_id=source.id,
                title=source.title,
                platform=source.platform.value,
                workspace_coverage=coverage,
                detail_digest=_ledger_digest(
                    {
                        "source": source.model_dump(mode="json"),
                        "coverage": coverage,
                    }
                ),
            )
        )

    for retired in retired_sources:
        source = retired.source
        entries.append(
            CourseCoverageLedgerEntry(
                key=f"retired-{_ledger_digest(source.id)}",
                kind="retired-source",
                status="retired",
                source_id=source.id,
                title=source.title,
                platform=source.platform.value,
                workspace_coverage="skipped",
                detail_digest=_ledger_digest(retired.model_dump(mode="json")),
            )
        )

    inspection_ledgers: list[CourseInspectionLedger] = []
    for report in inspections:
        inspection_key = f"input-{_ledger_digest(report.locator)}"
        inspection_ledgers.append(
            CourseInspectionLedger(
                key=inspection_key,
                platform=report.platform.value,
                expected_entries=report.expected_entries,
                accessible_entries=report.accessible_entries,
                inaccessible_entries=report.inaccessible_entries,
                failed_entries=report.failed_entries,
                completeness_proven=report.completeness_proven,
                disclaimer_digest=(
                    _ledger_digest(report.disclaimer) if report.disclaimer is not None else None
                ),
            )
        )
        for position, entry in enumerate(report.entries, start=1):
            title = (
                f"Local expected item {entry.ordinal}"
                if report.platform.value == "local"
                else entry.title or f"Expected item {entry.ordinal}"
            )
            entries.append(
                CourseCoverageLedgerEntry(
                    key=(
                        "inspection-"
                        + _ledger_digest(
                            {
                                "locator": report.locator,
                                "ordinal": entry.ordinal,
                                "position": position,
                            }
                        )
                    ),
                    kind="inspection-entry",
                    status=entry.status.value,
                    source_id=entry.source_id,
                    title=title,
                    platform=report.platform.value,
                    inspection_key=inspection_key,
                    ordinal=entry.ordinal,
                    detail_digest=_ledger_digest(
                        {
                            "inspection_key": inspection_key,
                            "entry": entry.model_dump(mode="json"),
                        }
                    ),
                )
            )

    if (
        len(entries) > MAX_WORKSPACE_LEDGER_ENTRIES
        or len(inspection_ledgers) > MAX_WORKSPACE_LEDGER_ENTRIES
    ):
        raise ProcessingError(
            "Workspace course inventory exceeds the 10,000-entry blueprint authoring "
            "bound; split the course into explicitly scoped skills."
        )

    input_locators = {_canonical_input_locator(value) for value in manifest.inputs}
    report_locators = {_canonical_input_locator(report.locator) for report in inspections}
    reports_cover_inputs = bool(inspections) and input_locators == report_locators
    active_source_ids = {source.id for source in active_sources}
    reported_accessible_ids = {
        entry.source_id
        for report in inspections
        for entry in report.entries
        if entry.status.value == "accessible" and entry.source_id is not None
    }
    accessible_inventory_consistent = active_source_ids == reported_accessible_ids
    if not reports_cover_inputs or any(report.expected_entries is None for report in inspections):
        state: CourseCoverageState = "unproven"
    elif (
        any(not report.completeness_proven for report in inspections)
        or not accessible_inventory_consistent
        or any(coverage != "complete" for coverage in active_coverages.values())
        or manifest.state != JobState.COMPLETE
    ):
        state = "partial"
    else:
        state = "complete"

    return CourseCoverageLedger(
        workspace_job_id=manifest.job_id,
        input_count=len(manifest.inputs),
        input_fingerprint=_ledger_digest(manifest.inputs),
        state=state,
        inspections=sorted(inspection_ledgers, key=lambda item: item.key),
        entries=sorted(entries, key=lambda item: item.key),
    )


def blueprint_seed_from_workspace(workspace: Workspace) -> dict[str, object]:
    """Return a transcript-free authoring seed with immutable inventory prefilled."""

    ledger = coverage_ledger_from_workspace(workspace)
    coverage_by_source = {
        item.source_id: item.workspace_coverage
        for item in ledger.entries
        if item.kind == "active-source"
        and item.source_id is not None
        and item.workspace_coverage is not None
    }
    sources = [
        _course_source_from_workspace(
            source,
            coverage_by_source.get(source.id, "partial"),
        ).model_dump(mode="json", exclude_none=True)
        for source in workspace.list_sources()
    ]
    limitations = (
        []
        if ledger.state == "complete"
        else [
            (
                f"Persisted workspace course coverage is {ledger.state}; keep every "
                "unavailable or failed inventory entry visible in the final skill."
            )
        ]
    )
    return {
        "schema_version": 2,
        "name": "replace-with-course-skill-name",
        "title": "Replace with the course title",
        "description": (
            "Replace with the source-specific requests that should trigger this course. "
            "Do not claim a broad generic domain."
        ),
        "scope": "Replace with the evidence-bounded learning scope.",
        "artifact_language": "replace-with-canonical-artifact-language",
        "interaction": {
            "welcome": (
                "Replace with an inviting, course-specific outcome and offer `start` "
                "in no more than two sentences."
            ),
            "starter_questions": ["Replace with one to three short, high-information questions."],
        },
        "capability_profile": {
            "learn": "strong",
            "practice": "medium",
            "apply": "medium",
            "reference": "strong",
            "rationale": "Replace with evidence-based relative capability depth.",
        },
        "curriculum": {
            "selected_path_id": "thematic",
            "rationale": "Replace with why the selected design fits the semantic map.",
            "paths": [],
        },
        "prerequisites": [],
        "core_principles": [],
        "semantic_units": [],
        "semantic_relations": [],
        "artifacts": [],
        "assets": [],
        "sources": sources,
        "coverage_ledger": ledger.model_dump(mode="json"),
        "claims": [],
        "limitations": limitations,
        "parent_build_id": None,
    }


def _summarize_values(values: set[str]) -> str:
    ordered = sorted(values)
    rendered = ", ".join(ordered[:8])
    return rendered + (f", … (+{len(ordered) - 8})" if len(ordered) > 8 else "")


def validate_blueprint_against_workspace(
    blueprint: CourseSkillBlueprint,
    workspace: Workspace,
) -> CourseCoverageLedger:
    """Reject source omissions, ledger drift, and unsupported coverage upgrades."""

    expected_ledger = coverage_ledger_from_workspace(workspace)
    regeneration = (
        f"Resume `video-to-skill run --workspace {workspace.root}` so the blueprint is "
        "recompiled from current canonical workspace state."
    )
    if blueprint.coverage_ledger is None:
        raise ProcessingError(
            "Blueprint is missing coverage_ledger, so full-course accounting cannot be "
            f"verified. {regeneration}"
        )

    problems: list[str] = []
    actual_ledger = blueprint.coverage_ledger
    if actual_ledger.state != expected_ledger.state:
        problems.append(
            f"course coverage state is {actual_ledger.state!r}, but the workspace proves "
            f"{expected_ledger.state!r}"
        )

    expected_entries = {item.key: item for item in expected_ledger.entries}
    actual_entries = {item.key: item for item in actual_ledger.entries}
    if missing := set(expected_entries) - set(actual_entries):
        problems.append(f"coverage ledger omits entries: {_summarize_values(missing)}")
    if extra := set(actual_entries) - set(expected_entries):
        problems.append(f"coverage ledger adds unknown entries: {_summarize_values(extra)}")
    changed_entries = {
        key
        for key in set(expected_entries) & set(actual_entries)
        if expected_entries[key] != actual_entries[key]
    }
    if changed_entries:
        problems.append(f"coverage ledger changes entries: {_summarize_values(changed_entries)}")

    expected_inspections = {item.key: item for item in expected_ledger.inspections}
    actual_inspections = {item.key: item for item in actual_ledger.inspections}
    if missing := set(expected_inspections) - set(actual_inspections):
        problems.append(f"coverage ledger omits inspections: {_summarize_values(missing)}")
    if extra := set(actual_inspections) - set(expected_inspections):
        problems.append(f"coverage ledger adds inspections: {_summarize_values(extra)}")
    changed_inspections = {
        key
        for key in set(expected_inspections) & set(actual_inspections)
        if expected_inspections[key] != actual_inspections[key]
    }
    if changed_inspections:
        problems.append(
            f"coverage ledger changes inspections: {_summarize_values(changed_inspections)}"
        )
    if (
        actual_ledger.workspace_job_id != expected_ledger.workspace_job_id
        or actual_ledger.input_count != expected_ledger.input_count
        or actual_ledger.input_fingerprint != expected_ledger.input_fingerprint
    ):
        problems.append("coverage ledger belongs to a different workspace input snapshot")

    expected_sources = {source.id: source for source in workspace.list_sources()}
    actual_sources = {source.id: source for source in blueprint.sources}
    if missing := set(expected_sources) - set(actual_sources):
        problems.append(f"blueprint omits active workspace sources: {_summarize_values(missing)}")
    if extra := set(actual_sources) - set(expected_sources):
        problems.append(
            f"blueprint adds sources absent from the workspace: {_summarize_values(extra)}"
        )

    expected_coverage = {
        item.source_id: item.workspace_coverage
        for item in expected_ledger.entries
        if item.kind == "active-source"
        and item.source_id is not None
        and item.workspace_coverage is not None
    }
    coverage_rank = {"skipped": 0, "failed": 1, "partial": 2, "complete": 3}
    for source_id in set(expected_sources) & set(actual_sources):
        descriptor = expected_sources[source_id]
        actual = actual_sources[source_id]
        expected_url = _safe_workspace_public_url(descriptor.canonical_url)
        mismatches = []
        if actual.title != descriptor.title:
            mismatches.append("title")
        if actual.creator != descriptor.creator:
            mismatches.append("creator")
        if actual.platform != descriptor.platform.value:
            mismatches.append("platform")
        if actual.url != expected_url:
            mismatches.append("url")
        if mismatches:
            problems.append(
                f"source {source_id!r} changes workspace metadata: {', '.join(mismatches)}"
            )
        maximum = expected_coverage.get(source_id, "partial")
        if coverage_rank[actual.coverage] > coverage_rank[maximum]:
            problems.append(
                f"source {source_id!r} upgrades workspace coverage from "
                f"{maximum!r} to {actual.coverage!r}"
            )

    if problems or actual_ledger != expected_ledger:
        if not problems:
            problems.append("coverage ledger differs from the persisted workspace snapshot")
        raise ProcessingError(
            "Blueprint does not match the persisted full-course contract:\n- "
            + "\n- ".join(problems)
            + f"\n{regeneration}"
        )
    return expected_ledger


def _timestamp_url(url: str | None, seconds: float) -> str | None:
    if not url:
        return None
    parsed = urlparse(url)
    host = parsed.netloc.casefold()
    if "youtube.com" in host or "youtu.be" in host:
        query = dict(parse_qsl(parsed.query, keep_blank_values=True))
        query["t"] = str(int(seconds))
        return urlunparse(parsed._replace(query=urlencode(query)))
    if "bilibili.com" in host:
        query = dict(parse_qsl(parsed.query, keep_blank_values=True))
        query["t"] = str(int(seconds))
        return urlunparse(parsed._replace(query=urlencode(query)))
    return url


def _source_map(blueprint: CourseSkillBlueprint) -> dict[str, CourseSkillSource]:
    return {item.id: item for item in blueprint.sources}


def _evidence_pointer(
    claim: CourseSkillClaim,
    sources: dict[str, CourseSkillSource],
) -> str:
    item = claim.evidence[0]
    source = sources[item.source_id]
    label = f"{source.title} {format_timestamp(item.start)}"
    link = _timestamp_url(source.url, item.start)
    pointer = f"[{label}]({link})" if link else label
    qualifier = "inferred, " if claim.inferred else ""
    return f"{pointer}; {qualifier}{claim.confidence} confidence; `{claim.id}`"


def render_course_skill_markdown(blueprint: CourseSkillBlueprint) -> str:
    """Render the V2 host-neutral, teaching-first ``SKILL.md``."""

    claims = {item.id: item for item in blueprint.claims}
    sources = _source_map(blueprint)
    artifacts = {item.id: item for item in blueprint.artifacts}
    capability = blueprint.capability_profile
    lines = [
        "---",
        f"name: {blueprint.name}",
        f"description: {json.dumps(blueprint.description, ensure_ascii=False)}",
        "---",
        COURSE_SKILL_MARKER,
        f"# {blueprint.title}",
        "",
        blueprint.scope,
        "",
        "## Operating contract",
        "",
        (
            "Act as an evidence-grounded teacher and practitioner for this course. Infer "
            "whether the user wants to learn, practice, apply, or look something up; do "
            "not make them choose an internal mode. Move between behaviors when useful, "
            "load only the smallest relevant artifact, and never treat this package as "
            "knowledge beyond its stated scope."
        ),
        "",
        (
            f"Use {blueprint.artifact_language} as the canonical artifact language and "
            "respond in the user's language unless they request otherwise."
        ),
        "",
        "## Empty invocation",
        "",
        "When invoked without a specific request, load no supporting file, inspect no project, run no command, and create no file. Reply with only this welcome and wait:",
        "",
        f"> {blueprint.interaction.welcome}",
        "",
        "Do not add a syllabus, mode menu, prerequisite list, evidence explanation, or feature list to that welcome.",
        "",
        "## Initial context",
        "",
        "After the user chooses to begin, ask only the missing questions from this course-specific set, at most three in one turn. Say that short answers are enough:",
        "",
        *[f"- {question}" for question in blueprint.interaction.starter_questions],
        "",
        "If the user already supplied enough context or asks a precise source question, proceed without an intake.",
        "",
        "## Interaction behavior",
        "",
        "- **Learn:** introduce one useful cognitive move, ground it in a source example when useful, ask one retrieval or transfer question, and adapt to the answer. Do not dump a chapter or announce a formal lesson unless requested.",
        "- **Practice:** present a task and success criteria without loading its solution. Evaluate the attempt naturally, give the smallest useful hint, allow a retry, and reveal a rubric or solution only after an attempt or explicit request.",
        "- **Apply:** inspect only the missing parts of the user's actual context, map source assumptions to it, label adaptations as inference, and use observable checkpoints. Do not pretend a conceptual source demonstrated an operational procedure.",
        "- **Reference:** answer the precise question first, load the smallest relevant artifact or [source map](source-map.md), preserve exact details only when supported, and cite source timestamps for consequential claims.",
        "",
        "Keep learner progress in the active conversation or host memory, never in this shareable package.",
        "",
        "## Evidence and uncertainty",
        "",
        (
            "Treat `provenance.json` as the machine-readable claim and semantic evidence "
            "ledger, [source-map.md](source-map.md) as the high-recall meaning map, and "
            "[sources.md](sources.md) as the source-acquisition record. A visible-state "
            "claim requires visual evidence; an action or transition requires ordered "
            "temporal evidence. Never upgrade low-confidence or inferred material into "
            "an authoritative instruction."
        ),
        "",
        (
            "Use source-grounded material first. Mark teaching or application inference "
            "naturally. When the request calls for outside or current knowledge, keep it "
            "distinct from what the source establishes; ask permission only when a "
            "material external action, private data, paid access, or a user-requested "
            "source-only boundary requires it."
        ),
        "",
    ]

    if blueprint.prerequisites:
        lines.extend(
            ["## Prerequisites", "", *[f"- {item}" for item in blueprint.prerequisites], ""]
        )

    if blueprint.core_principles:
        lines.extend(["## Core principles", ""])
        for principle in blueprint.core_principles:
            claim = claims[principle.claim_id]
            lines.append(f"- {principle.text} _Evidence: {_evidence_pointer(claim, sources)}._")
        lines.append("")

    lines.extend(
        [
            "## Capability profile",
            "",
            f"- Learn: **{capability.learn}**",
            f"- Practice: **{capability.practice}**",
            f"- Apply: **{capability.apply}**",
            f"- Reference: **{capability.reference}**",
            f"- Rationale: {capability.rationale}",
            "",
            "These levels describe relative depth, not user-visible modes or artifact quotas.",
            "",
            "## Learning paths",
            "",
        ]
    )
    ordered_paths = sorted(
        blueprint.curriculum.paths,
        key=lambda item: item.id != blueprint.curriculum.selected_path_id,
    )
    for path in ordered_paths:
        marker = " **Recommended.**" if path.id == blueprint.curriculum.selected_path_id else ""
        lines.append(f"### {path.title}{marker}")
        lines.extend(["", path.use_when, ""])
        for artifact_id in path.artifact_ids:
            artifact = artifacts[artifact_id]
            topics = f" Topics: {', '.join(artifact.topics)}." if artifact.topics else ""
            lines.append(f"- [{artifact.title}]({artifact.path}) — {artifact.use_when}.{topics}")
        lines.append("")
    lines.extend(
        [
            f"Curriculum rationale: {blueprint.curriculum.rationale}",
            "",
            "## Additional material",
            "",
        ]
    )
    indexed_ids = {
        artifact_id for path in blueprint.curriculum.paths for artifact_id in path.artifact_ids
    }
    additional = [
        artifact
        for artifact in blueprint.artifacts
        if artifact.id not in indexed_ids and artifact.disclosure == "normal"
    ]
    if additional:
        for artifact in additional:
            behaviors = ", ".join(artifact.modes)
            lines.append(
                f"- [{artifact.title}]({artifact.path}) — {artifact.use_when}. "
                f"Behaviors: {behaviors}."
            )
    else:
        lines.append("- No additional indexed artifact.")
    if any(artifact.disclosure == "after-attempt" for artifact in blueprint.artifacts):
        lines.append(
            "- Matching rubrics, answer keys, or solutions are intentionally unindexed; "
            "load one only after an attempt or explicit request."
        )
    lines.append("")

    lines.extend(
        [
            "## Scope and limits",
            "",
            *(
                [f"- {item}" for item in blueprint.limitations]
                if blueprint.limitations
                else ["- No additional source-specific limitation was recorded."]
            ),
            *(
                [
                    "- Full-course coverage was not verified against a persisted evidence "
                    "workspace; never describe this package as complete."
                ]
                if blueprint.coverage_ledger is None
                else [
                    (
                        "- Persisted source-acquisition accounting is "
                        f"**{blueprint.coverage_ledger.state}**."
                    )
                ]
            ),
            "- This skill contains derivative teaching material, not raw video, complete subtitles, or the extraction workspace.",
            "- Consult [source-map.md](source-map.md) for semantic coverage, [sources.md](sources.md) for acquisition coverage, `provenance.json` for exact mappings, and `build-manifest.json` for the portable build receipt.",
            "",
        ]
    )
    return "\n".join(lines)


def render_source_map_markdown(blueprint: CourseSkillBlueprint) -> str:
    """Render the chronological, high-recall semantic map."""

    lines = [
        "# Canonical source map",
        "",
        (
            "Load this file for source-faithful context, long-tail examples, qualifications, "
            "tensions, predictions, or open questions. Curriculum artifacts may merge "
            "presentation, but this map retains the original semantic units."
        ),
        "",
    ]
    units_by_source: dict[str, list[SemanticUnit]] = defaultdict(list)
    for unit in blueprint.semantic_units:
        units_by_source[unit.source_id].append(unit)
    for source in blueprint.sources:
        lines.extend([f"## {source.title}", ""])
        for unit in sorted(
            units_by_source.get(source.id, []),
            key=lambda item: (item.start, item.end, item.id),
        ):
            link = _timestamp_url(source.url, unit.start)
            timestamp = (
                f"[{format_timestamp(unit.start)}-{format_timestamp(unit.end)}]({link})"
                if link
                else f"{format_timestamp(unit.start)}-{format_timestamp(unit.end)}"
            )
            lines.extend(
                [
                    f"### {timestamp} · {unit.kind} · `{unit.id}`",
                    "",
                    unit.summary,
                    "",
                ]
            )
            if unit.detail:
                lines.extend([unit.detail, ""])
            metadata = [
                f"Materiality: {unit.materiality}",
                f"Disposition: {unit.disposition}",
                f"Confidence: {unit.confidence}",
                f"Status: {'inferred' if unit.inferred else 'observed'}",
                f"Modalities: {'+'.join(unit.modalities)}",
            ]
            if unit.speaker:
                metadata.insert(0, f"Speaker: {unit.speaker}")
            lines.extend([f"- {item}" for item in metadata])
            if unit.disposition_reason:
                lines.append(f"- Disposition reason: {unit.disposition_reason}")
            if unit.merged_into:
                lines.append(f"- Merged into: `{unit.merged_into}`")
            lines.append("")
    if blueprint.semantic_relations:
        lines.extend(["## Semantic relations", ""])
        for relation in blueprint.semantic_relations:
            explanation = f" — {relation.explanation}" if relation.explanation else ""
            lines.append(
                f"- `{relation.from_unit_id}` **{relation.kind}** "
                f"`{relation.to_unit_id}`{explanation}"
            )
        lines.append("")
    return "\n".join(lines)


def render_sources_markdown(blueprint: CourseSkillBlueprint) -> str:
    """Render source coverage and a human-readable timestamp evidence map."""

    sources = _source_map(blueprint)
    lines = [
        "# Sources and evidence map",
        "",
        "Load this file when checking course coverage, locating the demonstrated moment, or auditing a claim. Failed and partial sources remain visible.",
        "",
        "## Course coverage",
        "",
        "| Source | Creator | Platform | Coverage | Notes |",
        "|---|---|---|---|---|",
    ]
    for source in blueprint.sources:
        title = f"[{source.title}]({source.url})" if source.url else source.title
        lines.append(
            "| "
            + " | ".join(
                (
                    title.replace("|", "\\|"),
                    (source.creator or "Unknown").replace("|", "\\|"),
                    source.platform.replace("|", "\\|"),
                    source.coverage,
                    (source.notes or "—").replace("|", "\\|"),
                )
            )
            + " |"
        )

    lines.extend(["", "## Full-course accounting", ""])
    if blueprint.coverage_ledger is None:
        lines.extend(
            [
                "**Unverified:** this package was built without a persisted workspace "
                "coverage contract. Do not claim that it covers every course item.",
                "",
            ]
        )
    else:
        ledger = blueprint.coverage_ledger
        lines.extend(
            [
                f"- Persisted course coverage: **{ledger.state}**",
                f"- Workspace inputs accounted for: {ledger.input_count}",
                f"- Inspection reports accounted for: {len(ledger.inspections)}",
                "",
                "| Inventory entry | Kind | Status | Workspace coverage | Source ID |",
                "|---|---|---|---|---|",
            ]
        )
        for entry in ledger.entries:
            lines.append(
                "| "
                + " | ".join(
                    (
                        entry.title.replace("|", "\\|"),
                        entry.kind,
                        entry.status,
                        entry.workspace_coverage or "—",
                        (entry.source_id or "—").replace("|", "\\|"),
                    )
                )
                + " |"
            )
        lines.append("")

    lines.extend(["", "## Claim evidence", ""])
    for claim in blueprint.claims:
        evidence_parts: list[str] = []
        for evidence in claim.evidence:
            source = sources[evidence.source_id]
            label = (
                f"{source.title} "
                f"{format_timestamp(evidence.start)}-{format_timestamp(evidence.end)}"
            )
            link = _timestamp_url(source.url, evidence.start)
            pointer = f"[{label}]({link})" if link else label
            evidence_parts.append(f"{pointer} ({'+'.join(evidence.modalities)})")
        qualifiers = [
            claim.confidence,
            "inferred" if claim.inferred else "observed",
        ]
        lines.append(
            f"- `{claim.id}` — {claim.summary} "
            f"_({', '.join(qualifiers)}; {'; '.join(evidence_parts)})_"
        )
    if blueprint.assets:
        lines.extend(["", "## Included visual evidence", ""])
        for asset in blueprint.assets:
            claim_ids = ", ".join(f"`{claim_id}`" for claim_id in asset.claim_ids)
            lines.append(
                f"- ![{asset.description}]({asset.path}) {asset.description} (supports {claim_ids})"
            )
    lines.append("")
    return "\n".join(lines)


def _render_artifact(artifact: CourseArtifact) -> str:
    body = artifact.content.strip()
    if not body.startswith("#"):
        body = f"# {artifact.title}\n\n{body}"
    return body + "\n"


def _resolve_asset_source(asset: CourseAsset, workspace: Path) -> Path:
    raw = asset.source_path.expanduser()
    candidate = raw if raw.is_absolute() else workspace / raw
    lexical = Path(os.path.abspath(candidate))
    try:
        relative = lexical.relative_to(workspace)
    except ValueError as exc:
        raise ProcessingError(
            f"Asset source escapes the evidence workspace: {asset.source_path}"
        ) from exc

    cursor = workspace
    for part in relative.parts:
        cursor = cursor / part
        if cursor.is_symlink():
            raise ProcessingError(f"Asset source cannot use symlinks: {asset.source_path}")
    try:
        source = lexical.resolve(strict=True)
    except OSError as exc:
        raise ProcessingError(f"Asset source is unavailable: {asset.source_path}") from exc
    if not source.is_relative_to(workspace):
        raise ProcessingError(f"Asset source escapes the evidence workspace: {asset.source_path}")
    if not source.is_file():
        raise ProcessingError(f"Asset source is not a file: {asset.source_path}")
    if source.suffix.casefold() not in _SOURCE_IMAGE_EXTENSIONS:
        raise ProcessingError(f"Asset source format is unsupported: {source.suffix or '<none>'}")
    if source.stat().st_size > MAX_ASSET_INPUT_BYTES:
        raise ProcessingError(
            f"Asset source exceeds {MAX_ASSET_INPUT_BYTES} bytes: {asset.source_path}"
        )
    return source


def _sanitize_asset(asset: CourseAsset, workspace: Path, destination: Path) -> int:
    source = _resolve_asset_source(asset, workspace)
    try:
        with Image.open(source) as image:
            width, height = image.size
            if (
                width < 1
                or height < 1
                or width > MAX_ASSET_DIMENSION
                or height > MAX_ASSET_DIMENSION
                or width * height > MAX_ASSET_PIXELS
            ):
                raise ProcessingError(f"Asset dimensions exceed the safe bound: {width}x{height}")
            if image.format not in {"JPEG", "PNG", "WEBP"}:
                raise ProcessingError(
                    f"Decoded asset format is unsupported: {image.format or '<unknown>'}"
                )
            if getattr(image, "n_frames", 1) != 1:
                raise ProcessingError("Animated images cannot be included as course assets")
            image.verify()

        with Image.open(source) as image:
            image.load()
            has_alpha = image.mode in {"RGBA", "LA"} or (
                image.mode == "P" and "transparency" in image.info
            )
            clean = image.convert("RGBA" if has_alpha else "RGB")
            destination.parent.mkdir(parents=True, exist_ok=True)
            clean.save(destination, format="PNG", optimize=False, compress_level=9)
            clean.close()
    except ProcessingError:
        raise
    except (Image.DecompressionBombError, UnidentifiedImageError, OSError) as exc:
        raise ProcessingError(
            f"Could not sanitize course asset {asset.source_path}: {exc}"
        ) from exc

    size = destination.stat().st_size
    if size > MAX_ASSET_OUTPUT_BYTES:
        destination.unlink(missing_ok=True)
        raise ProcessingError(
            f"Sanitized asset exceeds {MAX_ASSET_OUTPUT_BYTES} bytes: {asset.path}"
        )
    return size


def provenance_payload(blueprint: CourseSkillBlueprint) -> dict[str, object]:
    """Return validator-compatible provenance without transcript text."""

    return {
        "schema_version": 2,
        "sources": [
            {
                "id": source.id,
                "title": source.title,
                "url": source.url,
                "coverage": source.coverage,
            }
            for source in blueprint.sources
        ],
        "course_coverage": (
            blueprint.coverage_ledger.model_dump(mode="json")
            if blueprint.coverage_ledger is not None
            else {"schema_version": 1, "state": "unverified"}
        ),
        "semantic_units": [
            unit.model_dump(mode="json", exclude_none=True) for unit in blueprint.semantic_units
        ],
        "semantic_relations": [
            relation.model_dump(mode="json", exclude_none=True)
            for relation in blueprint.semantic_relations
        ],
        "claims": [claim.model_dump(mode="json", exclude_none=True) for claim in blueprint.claims],
    }


def _public_blueprint_payload(blueprint: CourseSkillBlueprint) -> dict[str, object]:
    payload = blueprint.model_dump(mode="json", exclude_none=True)
    assets = payload.get("assets")
    if isinstance(assets, list):
        for asset in assets:
            if isinstance(asset, dict):
                asset.pop("source_path", None)
    return payload


def _build_id(blueprint: CourseSkillBlueprint) -> str:
    payload = _public_blueprint_payload(blueprint)
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return f"v2s-{hashlib.sha256(encoded).hexdigest()[:20]}"


def course_skill_build_id(blueprint: CourseSkillBlueprint) -> str:
    """Return the stable public build identity for a compiled blueprint."""

    return _build_id(blueprint)


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def build_manifest_payload(
    blueprint: CourseSkillBlueprint,
    package_root: Path,
) -> dict[str, object]:
    """Return the portable build receipt and generated-file ownership hashes."""

    artifact_ids = {artifact.path: artifact.id for artifact in blueprint.artifacts}
    managed_files: dict[str, object] = {}
    for path in sorted(item for item in package_root.rglob("*") if item.is_file()):
        relative = path.relative_to(package_root).as_posix()
        if relative == "build-manifest.json":
            continue
        entry: dict[str, object] = {"sha256": _file_sha256(path)}
        if artifact_id := artifact_ids.get(relative):
            entry["artifact_id"] = artifact_id
        managed_files[relative] = entry
    source_snapshot = [
        source.model_dump(mode="json", exclude_none=True) for source in blueprint.sources
    ]
    semantic_snapshot = [
        unit.model_dump(mode="json", exclude_none=True) for unit in blueprint.semantic_units
    ]
    return {
        "schema_version": 2,
        "build_id": _build_id(blueprint),
        "parent_build_id": blueprint.parent_build_id,
        "generator": {
            "name": "video-to-skill",
            "version": __version__,
        },
        "artifact_language": blueprint.artifact_language,
        "curriculum": {
            "selected_path_id": blueprint.curriculum.selected_path_id,
            "selected_kind": next(
                path.kind
                for path in blueprint.curriculum.paths
                if path.id == blueprint.curriculum.selected_path_id
            ),
        },
        "source_snapshot_digest": _ledger_digest(source_snapshot),
        "workspace_snapshot_digest": (
            _ledger_digest(blueprint.coverage_ledger.model_dump(mode="json"))
            if blueprint.coverage_ledger is not None
            else None
        ),
        "semantic_map_digest": _ledger_digest(semantic_snapshot),
        "managed_files": managed_files,
    }


def _overlaps(left: Path, right: Path) -> bool:
    return left == right or left.is_relative_to(right) or right.is_relative_to(left)


def render_course_skill_package(
    blueprint: CourseSkillBlueprint,
    destination: Path,
    *,
    workspace_root: Path | None = None,
) -> Path:
    """Create a new shareable course skill atomically.

    The destination must not already exist. Updates should be rendered to a new
    sibling staging path and installed only after validation.
    """

    target = destination.expanduser().resolve()
    workspace: Path | None = None
    if workspace_root is not None:
        workspace = workspace_root.expanduser().resolve()
        if _overlaps(target, workspace):
            raise ProcessingError(
                "The shareable skill and evidence workspace must be separate directories"
            )
    if blueprint.assets and workspace is None:
        raise ProcessingError("Course assets require an explicit evidence workspace")
    if workspace is not None and blueprint.assets and not workspace.is_dir():
        raise ProcessingError(f"Evidence workspace does not exist: {workspace}")
    if target.exists():
        raise ProcessingError(
            f"Skill destination already exists: {target}. Render updates to a new staging path."
        )

    target.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(
        tempfile.mkdtemp(prefix=f".{blueprint.name}-render-", dir=str(target.parent))
    ).resolve()
    try:
        atomic_write_text(staging / "SKILL.md", render_course_skill_markdown(blueprint))
        atomic_write_text(
            staging / "source-map.md",
            render_source_map_markdown(blueprint),
        )
        atomic_write_text(staging / "sources.md", render_sources_markdown(blueprint))
        atomic_write_json(staging / "provenance.json", provenance_payload(blueprint))
        for artifact in blueprint.artifacts:
            atomic_write_text(staging / artifact.path, _render_artifact(artifact))
        if workspace is not None:
            total_asset_bytes = 0
            for asset in blueprint.assets:
                total_asset_bytes += _sanitize_asset(
                    asset,
                    workspace,
                    staging / asset.path,
                )
                if total_asset_bytes > MAX_ASSET_OUTPUT_BYTES:
                    raise ProcessingError(
                        "Combined sanitized course assets exceed the package byte bound"
                    )
        atomic_write_json(
            staging / "build-manifest.json",
            build_manifest_payload(blueprint, staging),
        )
        if target.exists():
            raise ProcessingError(f"Skill destination appeared during rendering: {target}")
        os.replace(staging, target)
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return target


def blueprint_from_json(path: Path) -> CourseSkillBlueprint:
    """Load a strict blueprint produced by the host agent."""

    try:
        return CourseSkillBlueprint.model_validate_json(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ProcessingError(f"Could not read course skill blueprint: {exc}") from exc
    except PydanticValidationError as exc:
        errors = exc.errors(include_url=False, include_input=False)
        error_types = sorted({str(error.get("type", "validation_error")) for error in errors})
        kinds = ", ".join(error_types[:8])
        if len(error_types) > 8:
            kinds += ", ..."
        raise ProcessingError(
            f"Invalid course skill blueprint ({len(errors)} issue(s): {kinds})"
        ) from exc


def claims_by_file(blueprint: CourseSkillBlueprint) -> dict[str, list[CourseSkillClaim]]:
    """Group claims for critic passes without exposing source transcript text."""

    grouped: dict[str, list[CourseSkillClaim]] = defaultdict(list)
    for claim in blueprint.claims:
        grouped[claim.file].append(claim)
    return dict(grouped)
