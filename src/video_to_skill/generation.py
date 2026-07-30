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
from typing import TYPE_CHECKING, Any, Literal
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from PIL import Image, UnidentifiedImageError
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pydantic import ValidationError as PydanticValidationError

from video_to_skill.errors import ProcessingError
from video_to_skill.models import JobState, SourceDescriptor
from video_to_skill.url_security import UrlParameterLimitError, has_sensitive_url_parameters
from video_to_skill.utils import atomic_write_json, atomic_write_text, format_timestamp

if TYPE_CHECKING:
    from video_to_skill.workspace import Workspace

COURSE_SKILL_MARKER = "<!-- video-to-skill:course-skill:v1 -->"
_SKILL_NAME_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_MARKDOWN_TARGET_RE = re.compile(r"!?\[[^\]]*]\(([^)]+)\)")
_ROOT_ARTIFACTS = {
    "learning-path.md",
    "glossary.md",
    "patterns.md",
    "cheatsheet.md",
}
_ARTIFACT_DIRECTORIES = {
    "chapters",
    "playbooks",
    "exercises",
    "solutions",
    "reference",
}
_SOURCE_IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}
MAX_COURSE_ASSETS = 24
MAX_ASSET_INPUT_BYTES = 20 * 1024 * 1024
MAX_ASSET_OUTPUT_BYTES = 20 * 1024 * 1024
MAX_ASSET_DIMENSION = 4096
MAX_ASSET_PIXELS = 16_000_000
MAX_WORKSPACE_LEDGER_ENTRIES = 10_000
MAX_BLUEPRINT_AUTHORING_JSON_BYTES = 8 * 1024 * 1024

SkillMode = Literal["learn", "practice", "apply", "reference"]
Confidence = Literal["high", "medium", "low"]
CoverageStatus = Literal["complete", "partial", "failed", "skipped"]
EvidenceModality = Literal["speech", "visual", "ocr", "metadata", "temporal"]
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
    elif path.parts[0] not in _ARTIFACT_DIRECTORIES:
        allowed = ", ".join(sorted(_ARTIFACT_DIRECTORIES))
        raise ValueError(f"artifact must be inside one of: {allowed}")
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

    @model_validator(mode="before")
    @classmethod
    def accept_legacy_modality(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        data = dict(value)
        legacy = data.pop("modality", None)
        if "modalities" not in data and legacy is not None:
            if not isinstance(legacy, str):
                return data
            data["modalities"] = legacy.split("+")
        return data

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
    evidence: list[ClaimEvidence] = Field(min_length=1)

    @field_validator("id", "kind", "summary", "anchor")
    @classmethod
    def compact_text(cls, value: str | None) -> str | None:
        return _compact_required(value) if value is not None else None

    @field_validator("file")
    @classmethod
    def safe_claim_file(cls, value: str) -> str:
        if value in {"SKILL.md", "sources.md"}:
            return value
        return _safe_artifact_path(value)


class CourseArtifact(GenerationModel):
    path: str
    title: str = Field(min_length=1, max_length=300)
    mode: SkillMode
    use_when: str = Field(min_length=1, max_length=500)
    topics: list[str] = Field(default_factory=list, max_length=30)
    content: str = Field(min_length=1, max_length=500_000)

    @field_validator("path")
    @classmethod
    def safe_path(cls, value: str) -> str:
        return _safe_artifact_path(value)

    @field_validator("title", "use_when")
    @classmethod
    def compact_text(cls, value: str) -> str:
        return _compact_required(value)

    @field_validator("topics")
    @classmethod
    def compact_topics(cls, value: list[str]) -> list[str]:
        compact = [_compact_required(item) for item in value]
        if len(compact) != len(set(compact)):
            raise ValueError("artifact topics cannot contain duplicates")
        return compact


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


class CourseSkillBlueprint(GenerationModel):
    """The semantic handoff between the generator agent and package renderer."""

    schema_version: Literal[1] = 1
    name: str = Field(min_length=1, max_length=64)
    title: str = Field(min_length=1, max_length=300)
    description: str = Field(min_length=1, max_length=1024)
    scope: str = Field(min_length=1, max_length=1000)
    prerequisites: list[str] = Field(default_factory=list, max_length=30)
    core_principles: list[CorePrinciple] = Field(default_factory=list, max_length=24)
    artifacts: list[CourseArtifact] = Field(default_factory=list)
    assets: list[CourseAsset] = Field(default_factory=list, max_length=MAX_COURSE_ASSETS)
    sources: list[CourseSkillSource] = Field(min_length=1)
    coverage_ledger: CourseCoverageLedger | None = None
    claims: list[CourseSkillClaim] = Field(min_length=1)
    limitations: list[str] = Field(default_factory=list, max_length=30)

    @field_validator("name")
    @classmethod
    def valid_name(cls, value: str) -> str:
        if not _SKILL_NAME_RE.fullmatch(value):
            raise ValueError("skill name must use lowercase letters, digits, and hyphens")
        return value

    @field_validator("title", "description", "scope")
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
        required_modes: set[SkillMode] = {"learn", "practice", "apply", "reference"}
        available_modes = {item.mode for item in self.artifacts}
        if missing_modes := required_modes - available_modes:
            raise ValueError(
                "course skill needs at least one artifact for every mode; missing: "
                + ", ".join(sorted(missing_modes))
            )
        if not any(item.path.startswith("exercises/") for item in self.artifacts):
            raise ValueError("practice mode requires at least one exercise")
        if not any(item.path.startswith("solutions/") for item in self.artifacts):
            raise ValueError("practice mode requires a separate rubric or solution")
        rendered_files = {"SKILL.md", "sources.md", *artifact_paths}

        claim_ids = [item.id for item in self.claims]
        if len(claim_ids) != len(set(claim_ids)):
            raise ValueError("claim ids must be unique")
        known_claims = set(claim_ids)
        known_sources = set(source_ids)
        for claim in self.claims:
            if claim.file not in rendered_files:
                raise ValueError(f"claim '{claim.id}' points to an unknown rendered file")
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
                artifact = artifacts_by_path.get(used_by)
                if artifact is None:
                    raise ValueError(
                        f"asset '{asset.path}' is used by unknown artifact '{used_by}'"
                    )
                if not _artifact_uses_asset(artifact, asset.path):
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
        "schema_version": 1,
        "name": "replace-with-course-skill-name",
        "title": "Replace with the course title",
        "description": (
            "Replace with when and why an Agent should teach, practice, apply, or "
            "reference this course."
        ),
        "scope": "Replace with the evidence-bounded learning scope.",
        "prerequisites": [],
        "core_principles": [],
        "artifacts": [],
        "assets": [],
        "sources": sources,
        "coverage_ledger": ledger.model_dump(mode="json"),
        "claims": [],
        "limitations": limitations,
    }


def blueprint_authoring_payload(workspace: Workspace | None = None) -> dict[str, object]:
    """Emit the strict schema and optional workspace-derived full-inventory seed."""

    return {
        "schema_version": 1,
        "blueprint_schema": CourseSkillBlueprint.model_json_schema(mode="validation"),
        "blueprint_seed": (
            blueprint_seed_from_workspace(workspace) if workspace is not None else None
        ),
        "authoring_contract": {
            "seed_is_complete": False,
            "required_modes": ["learn", "practice", "apply", "reference"],
            "notes": [
                "Keep the preseeded sources and coverage_ledger unchanged.",
                "Add grounded artifacts and claims; do not copy transcripts or raw media.",
                "build-skill --workspace revalidates the ledger before rendering or installing.",
            ],
        },
    }


def encode_blueprint_authoring_payload(payload: dict[str, object]) -> str:
    """Serialize authoring data with a hard output bound and no silent truncation."""

    encoded = json.dumps(payload, ensure_ascii=False, indent=2)
    if len(encoded.encode("utf-8")) > MAX_BLUEPRINT_AUTHORING_JSON_BYTES:
        raise ProcessingError(
            "Blueprint schema and workspace seed exceed the 8 MiB authoring output bound; "
            "split the course into explicitly scoped skills."
        )
    return encoded


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
        f"Regenerate the authoring seed with `video-to-skill blueprint-schema --workspace "
        f"{workspace.root}` and preserve its sources and coverage_ledger."
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


def _artifact_index(artifacts: list[CourseArtifact], mode: SkillMode) -> list[str]:
    selected = [
        item for item in artifacts if item.mode == mode and not item.path.startswith("solutions/")
    ]
    if not selected:
        return ["- No dedicated file. Use the evidence map and the closest indexed material."]
    lines: list[str] = []
    for item in selected:
        topics = f" Topics: {', '.join(item.topics)}." if item.topics else ""
        lines.append(f"- [{item.title}]({item.path}) — {item.use_when}.{topics}")
    if mode == "practice" and any(item.path.startswith("solutions/") for item in artifacts):
        lines.append(
            "- Matching rubrics and solutions are under `solutions/`; load one only after "
            "an attempt or explicit request."
        )
    return lines


def render_course_skill_markdown(blueprint: CourseSkillBlueprint) -> str:
    """Render the resident, teaching-first ``SKILL.md``."""

    claims = {item.id: item for item in blueprint.claims}
    sources = _source_map(blueprint)
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
        "Act as an evidence-grounded teacher and practitioner for this course. Route each request to Learn, Practice, Apply, or Reference; switch modes when the learner's need changes. Load only the smallest relevant file and never treat the package as knowledge beyond its stated scope.",
        "",
        "When the request is ambiguous, begin in Learn mode with one short diagnostic question. Do not ask for a long intake form.",
        "",
        "## Modes",
        "",
        "### Learn",
        "",
        "1. Diagnose the learner's goal and prerequisite mastery with one question or micro-task at a time.",
        "2. Choose the next lesson from the learning index; skip material the learner demonstrates they already understand.",
        "3. Teach one bounded concept using an explanation, one grounded demonstration, and its timestamped evidence.",
        "4. Ask for retrieval or application before revealing the answer. If mastery is weak, explain the misconception differently and assign a smaller follow-up.",
        "5. End with what was mastered, what remains uncertain, and the recommended next lesson. Keep learner progress in the conversation or host memory, never in this shareable package.",
        "",
        "### Practice",
        "",
        "1. Select an exercise that matches the learner's goal and observed level.",
        "2. Present one task and its success criteria without loading or revealing the solution.",
        "3. Evaluate the attempt against the rubric, distinguish conceptual errors from execution slips, and cite the relevant course evidence.",
        "4. Give the smallest useful hint, allow a retry, then load the solution only after an attempt or an explicit request.",
        "",
        "### Apply",
        "",
        "1. Inspect the user's actual context, constraints, and desired outcome before choosing a playbook.",
        "2. Map course assumptions to the current situation and label any unsupported adaptation as an inference.",
        "3. Execute or guide the demonstrated method through observable checkpoints; do not blindly run source-derived commands.",
        "4. Verify the result, recover from failures with grounded alternatives, and report deviations from the demonstrated workflow.",
        "",
        "### Reference",
        "",
        "1. Answer the precise question first, then load only the indexed chapter or reference needed to support it.",
        "2. Preserve exact terminology, labels, commands, and thresholds only when the evidence supports them.",
        "3. Cite the source and timestamp for consequential claims. Distinguish what the course demonstrates from pedagogical interpretation or outside knowledge.",
        "",
        "## Evidence and uncertainty",
        "",
        "Treat `provenance.json` as the claim-to-evidence ledger and [sources.md](sources.md) as the human-readable timestamp map. A visible-state claim requires visual evidence; an action or transition requires ordered temporal evidence. Never upgrade low-confidence or inferred material into an authoritative instruction. If the package cannot answer, state the missing evidence and ask whether to continue with clearly labeled outside knowledge.",
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

    indexes: tuple[tuple[str, SkillMode], ...] = (
        ("Learning index", "learn"),
        ("Practice index", "practice"),
        ("Application index", "apply"),
        ("Reference index", "reference"),
    )
    for heading, mode in indexes:
        lines.extend([f"## {heading}", "", *_artifact_index(blueprint.artifacts, mode), ""])

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
                    f"- Persisted full-course accounting is **{blueprint.coverage_ledger.state}**."
                ]
            ),
            "- This skill contains derivative teaching material, not raw video, complete subtitles, or the extraction workspace.",
            "- Consult [sources.md](sources.md) for coverage and `provenance.json` for exact claim mappings.",
            "",
        ]
    )
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
        "schema_version": 1,
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
        "claims": [claim.model_dump(mode="json", exclude_none=True) for claim in blueprint.claims],
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
