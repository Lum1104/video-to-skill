"""Deterministic, policy-bounded evidence bundle export."""

from __future__ import annotations

import json
import os
import re
import secrets
import sqlite3
import stat
import struct
import tempfile
import zipfile
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from enum import StrEnum
from hashlib import sha256
from pathlib import Path, PurePosixPath
from typing import Any, BinaryIO, Literal, cast

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from video_to_skill import __version__
from video_to_skill.editions import load_edition_state_by_id
from video_to_skill.errors import ProcessingError
from video_to_skill.generation import CourseAsset
from video_to_skill.models import (
    AgentObservation,
    EvidenceGap,
    SemanticSegment,
    SourceDescriptor,
    SourcePlatform,
    TranscriptSegment,
    VisualEvent,
)
from video_to_skill.tool_runs import sanitize_value
from video_to_skill.utils import stable_hash
from video_to_skill.workspace import SCHEMA_VERSION, Workspace

BUNDLE_SCHEMA_VERSION = 1
BUNDLE_FORMAT = "video-to-skill-evidence-zip-v1"
BUNDLE_EXTENSION = ".v2sbundle"
_FIXED_ZIP_TIME = (1980, 1, 1, 0, 0, 0)
_MAX_MANIFEST_BYTES = 16 * 1024 * 1024
_MAX_BUNDLE_MEMBERS = 100_000
_CHUNK_SIZE = 1024 * 1024
_SAFE_COMPONENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_CAPTION_SUFFIXES = frozenset(
    {".vtt", ".srt", ".ass", ".ttml", ".json3", ".srv1", ".srv2", ".srv3"}
)
_GENERATED_SKILL_FILES = frozenset(
    {"SKILL.md", "source-map.md", "sources.md", "provenance.json", "build-manifest.json"}
)
_EXCLUDED_DIRECTORY_NAMES = frozenset(
    {
        ".cache",
        "cache",
        "caches",
        "locks",
        "__pycache__",
        "behavior-targets",
        "tasks",
    }
)
_SECRET_NAME_PARTS = (
    "authorization",
    "credential",
    "cookies",
    "cookie",
    "keychain",
    "password",
    "private-key",
    "secret",
    "token",
)
_COMPACT_EXACT_KINDS = {
    "evidence/observations.jsonl": "observations",
    "evidence/gaps.json": "evidence-gaps",
    "logs/tool-runs.jsonl": "sanitized-tool-runs",
    "visuals/selected.json": "selected-visual-index",
    "analysis/semantic-map.json": "semantic-map",
    "analysis/semantic-relations.json": "semantic-relations",
    "analysis/capability-evidence.json": "capability-evidence",
    "analysis/semantic-coverage.json": "semantic-coverage",
    "analysis/semantic-conflicts.json": "semantic-conflicts",
    "design/curriculum-options.json": "curriculum-options",
    "design/selected-curriculum.json": "selected-curriculum",
    "design/curriculum.json": "curriculum",
    "design/artifact-plan.json": "artifact-plan",
    "design/instructional-affordances.json": "instructional-affordances",
    "review/critic-report.json": "critic-report",
    "review/behavior-report.json": "behavior-report",
}
_COMPACT_REQUIRED = frozenset(
    {
        "evidence/observations.jsonl",
        "evidence/gaps.json",
        "logs/tool-runs.jsonl",
        "visuals/selected.json",
    }
)
_BUILD_KINDS = {
    "blueprint.json": "build-blueprint",
    "critic-report.json": "build-critic-report",
    "validation-report.json": "build-validation-report",
    "behavior-report.json": "build-behavior-report",
}
_ARCHIVAL_EXACT_KINDS = {
    "workspace/manifest.json": "sanitized-workspace-manifest",
    "workspace/evidence.sqlite3": "private-evidence-database",
    "workspace/logs/tool-runs.jsonl": "sanitized-tool-runs",
    "workspace/coverage.json": "private-sanitized-evidence-record",
    "workspace/observations/gaps.json": "private-sanitized-evidence-record",
    "workspace/observations/observations.jsonl": "private-sanitized-evidence-record",
}
_ARCHIVAL_REQUIRED = frozenset(
    {
        "workspace/manifest.json",
        "workspace/evidence.sqlite3",
        "workspace/logs/tool-runs.jsonl",
    }
)
_ARCHIVAL_DROP_KEYS = frozenset(
    {
        "execution_context_id",
        "generated_path",
        "installed_path",
        "lease",
        "lease_expires_at",
        "lease_owner",
        "lease_token",
        "lease_token_hash",
        "output",
        "packet_path",
        "project_root",
        "result_path",
        "skill_root",
        "snapshot_digest",
        "task_path",
        "validation_report_path",
        "workspace",
    }
)
_ARCHIVAL_BUILD_FILES = frozenset(_BUILD_KINDS)
_ARCHIVAL_IMAGE_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".webp"})
_ARCHIVAL_MEDIA_SUFFIXES = frozenset(
    {
        ".aac",
        ".avi",
        ".flac",
        ".m4a",
        ".m4v",
        ".mkv",
        ".mov",
        ".mp3",
        ".mp4",
        ".ogg",
        ".opus",
        ".wav",
        ".webm",
    }
)
_ARCHIVAL_DB_COLUMNS = {
    "archive_metadata": ("key", "value"),
    "sources": ("id", "ordinal", "descriptor_json", "content_hash", "active"),
    "transcript_segments": ("id", "source_id", "start", "end", "data_json"),
    "visual_events": ("id", "source_id", "timestamp", "data_json"),
    "semantic_segments": ("id", "source_id", "ordinal", "start", "end", "data_json"),
    "agent_observations": ("id", "source_id", "start", "end", "data_json"),
    "evidence_gaps": ("id", "source_id", "start", "end", "data_json"),
    "canonical_records": ("kind", "record_id", "revision", "path", "digest", "created_at"),
    "canonical_heads": ("kind", "record_id", "revision"),
}
_ARCHIVAL_DB_SCHEMA = """
PRAGMA foreign_keys = ON;
CREATE TABLE archive_metadata(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE sources(
    id TEXT PRIMARY KEY, ordinal INTEGER NOT NULL, descriptor_json TEXT NOT NULL,
    content_hash TEXT, active INTEGER NOT NULL CHECK(active IN (0, 1))
);
CREATE TABLE transcript_segments(
    id TEXT PRIMARY KEY, source_id TEXT NOT NULL, start REAL NOT NULL, end REAL NOT NULL,
    data_json TEXT NOT NULL, FOREIGN KEY(source_id) REFERENCES sources(id) ON DELETE CASCADE
);
CREATE TABLE visual_events(
    id TEXT PRIMARY KEY, source_id TEXT NOT NULL, timestamp REAL NOT NULL,
    data_json TEXT NOT NULL, FOREIGN KEY(source_id) REFERENCES sources(id) ON DELETE CASCADE
);
CREATE TABLE semantic_segments(
    id TEXT PRIMARY KEY, source_id TEXT NOT NULL, ordinal INTEGER NOT NULL,
    start REAL NOT NULL, end REAL NOT NULL, data_json TEXT NOT NULL,
    FOREIGN KEY(source_id) REFERENCES sources(id) ON DELETE CASCADE
);
CREATE TABLE agent_observations(
    id TEXT PRIMARY KEY, source_id TEXT NOT NULL, start REAL NOT NULL, end REAL NOT NULL,
    data_json TEXT NOT NULL, FOREIGN KEY(source_id) REFERENCES sources(id) ON DELETE CASCADE
);
CREATE TABLE evidence_gaps(
    id TEXT PRIMARY KEY, source_id TEXT NOT NULL, start REAL NOT NULL, end REAL NOT NULL,
    data_json TEXT NOT NULL, FOREIGN KEY(source_id) REFERENCES sources(id) ON DELETE CASCADE
);
CREATE TABLE canonical_records(
    kind TEXT NOT NULL, record_id TEXT NOT NULL, revision INTEGER NOT NULL,
    path TEXT NOT NULL, digest TEXT NOT NULL, created_at TEXT NOT NULL,
    PRIMARY KEY(kind, record_id, revision)
);
CREATE TABLE canonical_heads(
    kind TEXT NOT NULL, record_id TEXT NOT NULL, revision INTEGER NOT NULL,
    PRIMARY KEY(kind, record_id),
    FOREIGN KEY(kind, record_id, revision)
        REFERENCES canonical_records(kind, record_id, revision) ON DELETE CASCADE
);
"""
_ARCHIVAL_SOURCE_METADATA_KEYS = frozenset(
    {
        "captions",
        "chapters",
        "creator",
        "duration",
        "id",
        "kind",
        "language",
        "playlist_index",
        "playlist_title",
        "platform",
        "title",
    }
)


class EvidenceBundleMode(StrEnum):
    COMPACT = "compact"
    ARCHIVAL = "archival"


class _BundleModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class EvidenceBundleFile(_BundleModel):
    path: str
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    size: int = Field(ge=0)
    kind: str


class EvidenceBundleManifest(_BundleModel):
    schema_version: Literal[1] = 1
    format: Literal["video-to-skill-evidence-zip-v1"] = "video-to-skill-evidence-zip-v1"
    bundle_id: str = Field(pattern=r"^bundle-[a-f0-9]{32}$")
    mode: EvidenceBundleMode
    generator: dict[str, str]
    workspace_schema_version: int = Field(ge=1)
    workspace_job_id: str
    workspace_snapshot_digest: str
    analysis_depth: dict[str, Any] | None
    source_lineage: list[dict[str, Any]]
    edition: dict[str, Any] | None
    transcript_redistribution_authorized: bool
    private_sensitive: bool
    files: list[EvidenceBundleFile]


class EvidenceBundleReport(_BundleModel):
    schema_version: Literal[1] = 1
    path: Path
    bundle_id: str
    mode: EvidenceBundleMode
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    size: int = Field(ge=0)
    files: int = Field(ge=0)
    existing_identical: bool = False
    private_sensitive: bool
    warning: str | None = None


@dataclass(frozen=True)
class _Entry:
    path: str
    kind: str
    sha256: str
    size: int
    payload: bytes | None = None
    source_root: Path | None = None
    source_path: Path | None = None
    allow_hardlinks: bool = False


@dataclass(frozen=True)
class _CaptionSource:
    trusted_root: Path
    path: Path
    allow_hardlinks: bool


def _json_bytes(value: object) -> bytes:
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode("utf-8")


def _jsonl_bytes(values: list[object]) -> bytes:
    return b"".join(_json_bytes(value) for value in values)


def _safe_member_name(value: str) -> str:
    path = PurePosixPath(value)
    if (
        not value
        or path.is_absolute()
        or path.as_posix() != value
        or "\\" in value
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise ProcessingError(f"Evidence bundle member path is unsafe: {value!r}")
    return value


def _safe_component(value: str, *, prefix: str) -> str:
    compact = value.strip()
    if compact not in {".", ".."} and _SAFE_COMPONENT_RE.fullmatch(compact):
        return compact
    return f"{prefix}-{stable_hash(value, length=20)}"


def _lexical_child(root: Path, *parts: str) -> Path:
    lexical_root = Path(os.path.abspath(root))
    candidate = Path(os.path.abspath(lexical_root.joinpath(*parts)))
    if candidate == lexical_root or not candidate.is_relative_to(lexical_root):
        raise ProcessingError("Evidence bundle workspace path escapes its trusted root")
    return candidate


def _open_directory_components(path: Path) -> list[int]:
    lexical = Path(os.path.abspath(path))
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | nofollow
    descriptors: list[int] = []
    try:
        anchor = lexical.anchor or os.path.sep
        current_fd = os.open(anchor, flags)
        descriptors.append(current_fd)
        anchor_parts = len(Path(anchor).parts)
        for part in lexical.parts[anchor_parts:]:
            current_fd = os.open(part, flags, dir_fd=current_fd)
            descriptors.append(current_fd)
        return descriptors
    except OSError as exc:
        for descriptor in reversed(descriptors):
            os.close(descriptor)
        raise ProcessingError(f"Evidence bundle directory path is unsafe: {lexical}") from exc


@contextmanager
def _open_regular_beneath(
    root: Path,
    path: Path,
    *,
    allow_hardlinks: bool = False,
) -> Iterator[tuple[int, os.stat_result]]:
    lexical_root = Path(os.path.abspath(root))
    lexical_path = Path(os.path.abspath(path))
    try:
        relative = lexical_path.relative_to(lexical_root)
    except ValueError as exc:
        raise ProcessingError("Evidence bundle input escapes its trusted root") from exc
    if not relative.parts or any(part in {"", ".", ".."} for part in relative.parts):
        raise ProcessingError("Evidence bundle input path is invalid")
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | nofollow
    descriptors: list[int] = []
    try:
        descriptors = _open_directory_components(lexical_root)
        current_fd = descriptors[-1]
        for part in relative.parts[:-1]:
            current_fd = os.open(part, directory_flags, dir_fd=current_fd)
            descriptors.append(current_fd)
        descriptor = os.open(relative.parts[-1], os.O_RDONLY | nofollow, dir_fd=current_fd)
        descriptors.append(descriptor)
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise ProcessingError("Evidence bundle inputs must be regular files")
        if metadata.st_nlink != 1 and not allow_hardlinks:
            raise ProcessingError("Evidence bundle workspace inputs cannot be hard links")
        yield descriptor, metadata
    except OSError as exc:
        raise ProcessingError(f"Evidence bundle input path is unsafe: {relative}") from exc
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def _digest_source(
    root: Path,
    path: Path,
    *,
    allow_hardlinks: bool = False,
) -> tuple[str, int]:
    digest = sha256()
    size = 0
    with (
        _open_regular_beneath(root, path, allow_hardlinks=allow_hardlinks) as (
            descriptor,
            metadata,
        ),
        os.fdopen(os.dup(descriptor), "rb") as handle,
    ):
        while chunk := handle.read(_CHUNK_SIZE):
            digest.update(chunk)
            size += len(chunk)
    if size != metadata.st_size:
        raise ProcessingError("Evidence bundle input changed while it was inspected")
    return digest.hexdigest(), size


def _payload_entry(path: str, payload: bytes, *, kind: str) -> _Entry:
    return _Entry(
        path=_safe_member_name(path),
        kind=kind,
        sha256=sha256(payload).hexdigest(),
        size=len(payload),
        payload=payload,
    )


def _source_entry(
    path: str,
    *,
    kind: str,
    source_root: Path,
    source_path: Path,
    expected_sha256: str | None = None,
    allow_hardlinks: bool = False,
) -> _Entry:
    digest, size = _digest_source(
        source_root,
        source_path,
        allow_hardlinks=allow_hardlinks,
    )
    if expected_sha256 is not None and digest != expected_sha256:
        raise ProcessingError(f"Evidence bundle source failed its digest check: {path}")
    return _Entry(
        path=_safe_member_name(path),
        kind=kind,
        sha256=digest,
        size=size,
        source_root=source_root,
        source_path=source_path,
        allow_hardlinks=allow_hardlinks,
    )


def _add_entry(entries: dict[str, _Entry], entry: _Entry) -> None:
    previous = entries.get(entry.path)
    if previous is not None:
        if previous.sha256 != entry.sha256 or previous.size != entry.size:
            raise ProcessingError(f"Evidence bundle member collision: {entry.path}")
        return
    entries[entry.path] = entry


def _sanitize_json(value: object, workspace: Workspace) -> object:
    try:
        return sanitize_value(value, workspace_root=workspace.root)
    except ValueError as exc:
        raise ProcessingError(f"Evidence bundle metadata could not be sanitized: {exc}") from exc


def _sanitized_string(value: str, workspace: Workspace) -> str:
    sanitized = _sanitize_json(value, workspace)
    if not isinstance(sanitized, str):
        raise ProcessingError("Sanitized evidence bundle identity must be text")
    return sanitized


def _source_metadata(source: Any, workspace: Workspace) -> dict[str, Any]:
    captions = [
        {
            "language": item.language,
            "extension": item.extension,
            "automatic": item.automatic,
            "name": item.name,
        }
        for item in source.captions
    ]
    payload: dict[str, Any] = {
        "id": source.id,
        "platform": source.platform.value,
        "kind": source.kind.value,
        "title": source.title,
        "creator": source.creator,
        "duration": source.duration,
        "language": source.language,
        "playlist_title": source.playlist_title,
        "playlist_index": source.playlist_index,
        "chapters": [item.model_dump(mode="json") for item in source.chapters],
        "captions": captions,
    }
    sanitized = _sanitize_json(payload, workspace)
    if not isinstance(sanitized, dict):
        raise ProcessingError("Sanitized source metadata must be an object")
    return cast(dict[str, Any], sanitized)


def _canonical_json(workspace: Workspace, kind: str) -> object | None:
    record = workspace.canonical_record(kind)
    if record is None:
        return None
    path = workspace.root / record.path
    with _open_regular_beneath(workspace.root, path) as (descriptor, metadata):
        if metadata.st_size > 64 * 1024 * 1024:
            raise ProcessingError(f"Canonical {kind} exceeds the bundle metadata limit")
        with os.fdopen(os.dup(descriptor), "rb") as handle:
            payload = handle.read()
    if sha256(payload).hexdigest() != record.digest:
        raise ProcessingError(f"Canonical {kind} failed its digest check")
    try:
        parsed = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProcessingError(f"Canonical {kind} is not valid JSON") from exc
    return _sanitize_json(parsed, workspace)


def _compact_canonical_entries(workspace: Workspace, entries: dict[str, _Entry]) -> None:
    destinations = {
        "semantic-map": "analysis/semantic-map.json",
        "semantic-relations": "analysis/semantic-relations.json",
        "capability-evidence": "analysis/capability-evidence.json",
        "semantic-coverage": "analysis/semantic-coverage.json",
        "semantic-conflicts": "analysis/semantic-conflicts.json",
        "curriculum-options": "design/curriculum-options.json",
        "selected-curriculum": "design/selected-curriculum.json",
        "curriculum": "design/curriculum.json",
        "artifact-plan": "design/artifact-plan.json",
        "instructional-affordances": "design/instructional-affordances.json",
        "critic-report": "review/critic-report.json",
        "behavior-report": "review/behavior-report.json",
    }
    for kind, destination in destinations.items():
        value = _canonical_json(workspace, kind)
        if value is not None:
            _add_entry(entries, _payload_entry(destination, _json_bytes(value), kind=kind))


def _compact_evidence_records(workspace: Workspace, entries: dict[str, _Entry]) -> None:
    sources = workspace.list_sources(include_inactive=True)
    observations: list[object] = []
    for source in sources:
        source_component = _safe_component(source.id, prefix="source")
        _add_entry(
            entries,
            _payload_entry(
                f"sources/{source_component}/metadata.json",
                _json_bytes(_source_metadata(source, workspace)),
                kind="source-metadata",
            ),
        )
        observations.extend(
            item.model_dump(mode="json")
            for item in workspace.observations(source.id, limit=1_000_000)
        )
    sanitized_observations = _sanitize_json(observations, workspace)
    sanitized_gaps = _sanitize_json(
        [item.model_dump(mode="json") for item in workspace.gaps(limit=None)],
        workspace,
    )
    _add_entry(
        entries,
        _payload_entry(
            "evidence/observations.jsonl",
            _jsonl_bytes(cast(list[object], sanitized_observations)),
            kind="observations",
        ),
    )
    _add_entry(
        entries,
        _payload_entry(
            "evidence/gaps.json",
            _json_bytes(sanitized_gaps),
            kind="evidence-gaps",
        ),
    )
    tool_records = Workspace.open(workspace.root).tool_run_records()
    _add_entry(
        entries,
        _payload_entry(
            "logs/tool-runs.jsonl",
            _jsonl_bytes(cast(list[object], tool_records)),
            kind="sanitized-tool-runs",
        ),
    )


def _compact_visual_entries(workspace: Workspace, entries: dict[str, _Entry]) -> None:
    raw_assets = _canonical_json(workspace, "assets")
    selected_metadata: list[dict[str, Any]] = []
    if raw_assets is not None:
        if not isinstance(raw_assets, list):
            raise ProcessingError("Canonical selected visual assets must be a list")
        try:
            assets = [CourseAsset.model_validate(item) for item in raw_assets]
        except ValidationError as exc:
            raise ProcessingError(f"Canonical selected visual assets are invalid: {exc}") from exc
        for asset in sorted(assets, key=lambda item: (item.candidate_id, item.path)):
            image_source = workspace.root / asset.source_path
            suffix = image_source.suffix.casefold()
            if suffix not in {".png", ".jpg", ".jpeg", ".webp"}:
                raise ProcessingError("Selected visual evidence uses an unsupported image format")
            destination = (
                "visuals/selected/"
                f"{_safe_component(asset.candidate_id, prefix='visual')}-{asset.source_sha256[:12]}"
                f"{suffix}"
            )
            _add_entry(
                entries,
                _source_entry(
                    destination,
                    kind="selected-visual",
                    source_root=workspace.root,
                    source_path=image_source,
                    expected_sha256=asset.source_sha256,
                ),
            )
            metadata = asset.model_dump(mode="json", exclude={"source_path"})
            metadata["bundle_path"] = destination
            selected_metadata.append(metadata)
    _add_entry(
        entries,
        _payload_entry(
            "visuals/selected.json",
            _json_bytes(_sanitize_json(selected_metadata, workspace)),
            kind="selected-visual-index",
        ),
    )
    for descriptor in workspace.list_sources(include_inactive=True):
        source_component = _safe_component(descriptor.id, prefix="source")
        contact_root = _lexical_child(workspace.sources_dir, descriptor.id, "contact-sheets")
        if not contact_root.exists():
            continue
        for path in _walk_regular_files(contact_root, archival=False):
            if path.suffix.casefold() not in {".jpg", ".jpeg", ".png", ".webp"}:
                continue
            relative = path.relative_to(contact_root).as_posix()
            destination = f"visuals/contact-sheets/{source_component}/{relative}"
            _add_entry(
                entries,
                _source_entry(
                    destination,
                    kind="contact-sheet",
                    source_root=contact_root,
                    source_path=path,
                ),
            )


def _compact_build_entries(workspace: Workspace, entries: dict[str, _Entry]) -> None:
    root = workspace.builds_dir
    if not root.exists():
        return
    if root.is_symlink() or not root.is_dir():
        raise ProcessingError("Build evidence root is unsafe")
    allowed = {
        "blueprint.json",
        "critic-report.json",
        "validation-report.json",
        "behavior-report.json",
    }
    for build in sorted(root.iterdir(), key=lambda path: path.name):
        if build.is_symlink():
            raise ProcessingError("Build evidence cannot use symbolic links")
        if not build.is_dir():
            continue
        build_component = _safe_component(build.name, prefix="build")
        for name in sorted(allowed):
            source = build / name
            if not source.exists():
                continue
            with _open_regular_beneath(root, source) as (descriptor, metadata):
                if metadata.st_size > 64 * 1024 * 1024:
                    raise ProcessingError("Build evidence metadata exceeds its size limit")
                with os.fdopen(os.dup(descriptor), "rb") as handle:
                    payload = handle.read()
            try:
                value = json.loads(payload)
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ProcessingError(f"Build evidence is invalid JSON: {name}") from exc
            _add_entry(
                entries,
                _payload_entry(
                    f"builds/{build_component}/{name}",
                    _json_bytes(_sanitize_json(value, workspace)),
                    kind=f"build-{name.removesuffix('.json')}",
                ),
            )


def _caption_sources(workspace: Workspace, source: SourceDescriptor) -> list[_CaptionSource]:
    row = Workspace.open(workspace.root).materialization_record(str(source.id))
    if row is None:
        return []
    try:
        values = json.loads(str(row["caption_paths_json"]))
    except json.JSONDecodeError as exc:
        raise ProcessingError("Workspace caption materialization record is invalid") from exc
    if not isinstance(values, list) or not all(isinstance(value, str) for value in values):
        raise ProcessingError("Workspace caption materialization record is invalid")
    expected_workspace_root = _lexical_child(workspace.sources_dir, source.id)
    recorded_source_root = (
        Path(os.path.abspath(str(row["source_dir"]))) if row["source_dir"] else None
    )
    recorded_media = Path(os.path.abspath(str(row["media_path"]))) if row["media_path"] else None
    local_media = (
        Path(os.path.abspath(source.locator)) if source.platform == SourcePlatform.LOCAL else None
    )
    validated: list[_CaptionSource] = []
    for value in values:
        candidate = Path(os.path.abspath(value))
        in_workspace_source = (
            recorded_source_root == expected_workspace_root
            and candidate.is_relative_to(expected_workspace_root)
        )
        local_sidecar = (
            local_media is not None
            and recorded_media == local_media
            and candidate.parent == local_media.parent
            and candidate.name.startswith(f"{local_media.stem}.")
        )
        if in_workspace_source:
            validated.append(
                _CaptionSource(
                    trusted_root=expected_workspace_root,
                    path=candidate,
                    allow_hardlinks=False,
                )
            )
        elif local_sidecar:
            assert local_media is not None
            validated.append(
                _CaptionSource(
                    trusted_root=local_media.parent,
                    path=candidate,
                    allow_hardlinks=True,
                )
            )
        else:
            raise ProcessingError("Caption materialization escapes its authorized source boundary")
    return validated


def _compact_transcript_entries(workspace: Workspace, entries: dict[str, _Entry]) -> None:
    for source in workspace.list_sources(include_inactive=True):
        component = _safe_component(source.id, prefix="source")
        transcripts = [
            item.model_dump(mode="json")
            for item in workspace.transcripts(source.id, limit=1_000_000)
        ]
        _add_entry(
            entries,
            _payload_entry(
                f"transcripts/{component}/normalized.jsonl",
                _jsonl_bytes(cast(list[object], _sanitize_json(transcripts, workspace))),
                kind="authorized-normalized-transcript",
            ),
        )
        caption_sources = sorted(
            _caption_sources(workspace, source),
            key=lambda item: os.fspath(item.path),
        )
        for ordinal, caption in enumerate(caption_sources, start=1):
            suffix = caption.path.suffix.casefold()
            if suffix not in _CAPTION_SUFFIXES:
                continue
            _add_entry(
                entries,
                _source_entry(
                    f"transcripts/{component}/caption-{ordinal:03d}{suffix}",
                    kind="authorized-caption",
                    source_root=caption.trusted_root,
                    source_path=caption.path,
                    allow_hardlinks=caption.allow_hardlinks,
                ),
            )


def _name_is_sensitive(path: Path) -> bool:
    name = path.name.casefold()
    if name in {".env", ".env.local", "id_rsa", "id_ed25519"}:
        return True
    if name.endswith((".lock", ".lck", ".pid", "-wal", "-shm")):
        return True
    if name.endswith((".info.json", ".ytdl")):
        return True
    return any(part in name for part in _SECRET_NAME_PARTS)


def _walk_regular_files(root: Path, *, archival: bool) -> Iterator[Path]:
    if root.is_symlink():
        raise ProcessingError(f"Evidence bundle roots cannot be symbolic links: {root.name}")
    if not root.exists():
        return
    if not root.is_dir():
        raise ProcessingError(f"Evidence bundle root is not a directory: {root.name}")
    for current, directories, filenames in os.walk(root, topdown=True, followlinks=False):
        current_path = Path(current)
        kept_directories: list[str] = []
        for name in sorted(directories):
            candidate = current_path / name
            if candidate.is_symlink():
                raise ProcessingError("Evidence bundle inputs cannot contain symbolic links")
            if name.casefold() in _EXCLUDED_DIRECTORY_NAMES or _name_is_sensitive(candidate):
                continue
            kept_directories.append(name)
        directories[:] = kept_directories
        for name in sorted(filenames):
            candidate = current_path / name
            if candidate.is_symlink():
                raise ProcessingError("Evidence bundle inputs cannot contain symbolic links")
            metadata = candidate.stat(follow_symlinks=False)
            if not stat.S_ISREG(metadata.st_mode):
                raise ProcessingError("Evidence bundle inputs cannot contain special files")
            if metadata.st_nlink != 1:
                raise ProcessingError("Evidence bundle workspace inputs cannot be hard links")
            if _name_is_sensitive(candidate) or candidate.name in _GENERATED_SKILL_FILES:
                continue
            if archival and candidate.suffix.casefold() in {".tmp", ".part"}:
                continue
            yield candidate


def _sanitized_workspace_manifest(workspace: Workspace) -> dict[str, Any]:
    manifest = Workspace.open(workspace.root).load_manifest()
    return {
        "schema_version": manifest.schema_version,
        "job_id": manifest.job_id,
        "state": manifest.state.value,
        "configuration_hash": manifest.configuration_hash,
        "output_language": manifest.output_language,
        "analysis_depth": (
            manifest.analysis_depth.model_dump(mode="json")
            if manifest.analysis_depth is not None
            else None
        ),
        "failed_sources": sorted(manifest.failed_sources),
        "warnings": [item.model_dump(mode="json") for item in manifest.warnings],
    }


def _sanitized_edition_state(workspace: Workspace) -> dict[str, Any] | None:
    if workspace.edition_id is None:
        return None
    state = load_edition_state_by_id(Workspace.open(workspace.root), workspace.edition_id)
    configuration = state.configuration
    return {
        "schema_version": 1,
        "edition_id": configuration.edition_id,
        "edition_name": configuration.edition_name,
        "config_digest": configuration.config_digest,
        "analysis_lineage": configuration.analysis_lineage.model_dump(mode="json"),
        "requested_output_language": configuration.requested_output_language,
        "curriculum_mode": configuration.curriculum_mode,
        "curriculum_source_edition": configuration.curriculum_source_edition,
        "requested_curriculum_path_id": configuration.requested_curriculum_path_id,
        "skill_name": configuration.skill_name,
        "identity_baseline": configuration.identity_baseline.model_dump(mode="json"),
        "status": state.status,
        "resolved_artifact_language": state.resolved_artifact_language,
        "selected_curriculum_path_id": state.selected_curriculum_path_id,
        "selected_curriculum_digest": state.selected_curriculum_digest,
    }


def _archival_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", value.casefold()).strip("_")


def _archival_key_is_private(value: str) -> bool:
    normalized = _archival_key(value)
    if normalized in _ARCHIVAL_DROP_KEYS:
        return True
    return any(
        marker in normalized
        for marker in (
            "access_token",
            "api_key",
            "authorization",
            "cookie",
            "credential",
            "lease_",
            "password",
            "secret",
            "session_token",
            "snapshot_digest",
        )
    )


def _archival_sanitize(value: object, workspace: Workspace) -> object:
    """Sanitize untrusted archival JSON without retaining authority-bearing keys."""

    if isinstance(value, dict):
        result: dict[str, object] = {}
        for raw_key, item in sorted(value.items(), key=lambda pair: str(pair[0])):
            key = str(raw_key)
            if _archival_key_is_private(key):
                continue
            sanitized_key = _sanitized_string(key, workspace)
            if sanitized_key in result:
                raise ProcessingError("Archival record keys collide after sanitization")
            result[sanitized_key] = _archival_sanitize(item, workspace)
        return result
    if isinstance(value, (list, tuple)):
        return [_archival_sanitize(item, workspace) for item in value]
    if isinstance(value, str) and len(value) > 4_096:
        return f"<omitted-oversized-text:{sha256(value.encode('utf-8')).hexdigest()}>"
    return _sanitize_json(value, workspace)


def _database_json(value: object, workspace: Workspace) -> str:
    return json.dumps(
        _archival_sanitize(value, workspace),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _canonical_path_allowed(workspace: Workspace, relative: Path) -> bool:
    if relative.is_absolute() or any(part in {"", ".", ".."} for part in relative.parts):
        return False
    base = Workspace.open(workspace.root)
    roots = [base.analysis_dir / "records"]
    if workspace.edition_dir is not None:
        roots.append(workspace.edition_dir / "analysis" / "records")
    lexical = Path(os.path.abspath(workspace.root / relative))
    return any(lexical.is_relative_to(Path(os.path.abspath(root))) for root in roots)


def _build_archival_database(workspace: Workspace, snapshot: Path) -> None:
    base = Workspace.open(workspace.root)
    try:
        with base.connect() as source, sqlite3.connect(snapshot) as destination:
            source.execute("BEGIN")
            destination.execute("PRAGMA secure_delete = ON")
            destination.executescript(_ARCHIVAL_DB_SCHEMA)
            destination.execute(
                "INSERT INTO archive_metadata(key, value) VALUES(?, ?)",
                ("archive_schema_version", "1"),
            )
            for row in source.execute(
                "SELECT id, ordinal, descriptor_json, content_hash, active "
                "FROM sources ORDER BY ordinal, id"
            ):
                descriptor = SourceDescriptor.model_validate_json(str(row["descriptor_json"]))
                destination.execute(
                    "INSERT INTO sources VALUES(?, ?, ?, ?, ?)",
                    (
                        str(row["id"]),
                        int(row["ordinal"]),
                        _database_json(_source_metadata(descriptor, workspace), workspace),
                        str(row["content_hash"]) if row["content_hash"] else None,
                        int(row["active"]),
                    ),
                )
            typed_tables: tuple[tuple[str, type[BaseModel], tuple[str, ...]], ...] = (
                (
                    "transcript_segments",
                    TranscriptSegment,
                    ("id", "source_id", "start", "end"),
                ),
                ("visual_events", VisualEvent, ("id", "source_id", "timestamp")),
                (
                    "semantic_segments",
                    SemanticSegment,
                    ("id", "source_id", "ordinal", "start", "end"),
                ),
                (
                    "agent_observations",
                    AgentObservation,
                    ("id", "source_id", "start", "end"),
                ),
                ("evidence_gaps", EvidenceGap, ("id", "source_id", "start", "end")),
            )
            for table, model, scalar_columns in typed_tables:
                placeholders = ", ".join("?" for _ in (*scalar_columns, "data_json"))
                for row in source.execute(f"SELECT data_json FROM {table} ORDER BY data_json"):
                    value = model.model_validate_json(str(row["data_json"]))
                    canonical = _archival_sanitize(value.model_dump(mode="json"), workspace)
                    if not isinstance(canonical, dict):
                        raise ProcessingError("Canonical archival evidence must be an object")
                    canonical_model = model.model_validate(canonical)
                    canonical_dump = canonical_model.model_dump(mode="json")
                    destination.execute(
                        f"INSERT INTO {table} VALUES({placeholders})",
                        (
                            *(canonical_dump[column] for column in scalar_columns),
                            _database_json(canonical_dump, workspace),
                        ),
                    )
            retained_records: set[tuple[str, str, int]] = set()
            for row in source.execute(
                "SELECT kind, record_id, revision, path, digest, created_at "
                "FROM canonical_records ORDER BY kind, record_id, revision"
            ):
                relative = Path(str(row["path"]))
                if not _canonical_path_allowed(workspace, relative):
                    continue
                key = (str(row["kind"]), str(row["record_id"]), int(row["revision"]))
                retained_records.add(key)
                destination.execute(
                    "INSERT INTO canonical_records VALUES(?, ?, ?, ?, ?, ?)",
                    (*key, relative.as_posix(), str(row["digest"]), str(row["created_at"])),
                )
            for row in source.execute(
                "SELECT kind, record_id, revision FROM canonical_heads ORDER BY kind, record_id"
            ):
                key = (str(row["kind"]), str(row["record_id"]), int(row["revision"]))
                if key in retained_records:
                    destination.execute("INSERT INTO canonical_heads VALUES(?, ?, ?)", key)
            destination.commit()
            destination.execute("VACUUM")
            source.rollback()
    except (sqlite3.Error, ValidationError) as exc:
        raise ProcessingError(f"Could not build the archival evidence database: {exc}") from exc


def _sqlite_snapshot_entry(workspace: Workspace, temporary_root: Path) -> _Entry:
    snapshot = temporary_root / "evidence.sqlite3"
    _build_archival_database(workspace, snapshot)
    return _source_entry(
        "workspace/evidence.sqlite3",
        kind="private-evidence-database",
        source_root=temporary_root,
        source_path=snapshot,
    )


def _archival_source_entries(workspace: Workspace, entries: dict[str, _Entry]) -> None:
    base = Workspace.open(workspace.root)
    for descriptor in base.list_sources(include_inactive=True):
        component = _safe_component(descriptor.id, prefix="source")
        source_root = _lexical_child(base.sources_dir, descriptor.id)
        materialization = base.materialization_record(descriptor.id)
        if materialization is not None and materialization["media_path"]:
            media = Path(os.path.abspath(str(materialization["media_path"])))
            if media.is_relative_to(source_root):
                suffix = media.suffix.casefold()
                if suffix not in _ARCHIVAL_MEDIA_SUFFIXES:
                    raise ProcessingError("Archival source media uses an unsupported file type")
                _add_entry(
                    entries,
                    _source_entry(
                        f"workspace/sources/{component}/media{suffix}",
                        kind="private-source-media",
                        source_root=source_root,
                        source_path=media,
                    ),
                )
        audio = source_root / "audio-16khz.wav"
        if audio.exists():
            _add_entry(
                entries,
                _source_entry(
                    f"workspace/sources/{component}/audio-16khz.wav",
                    kind="private-source-audio",
                    source_root=source_root,
                    source_path=audio,
                ),
            )
        for directory in ("frames", "investigation-frames", "contact-sheets"):
            visual_root = source_root / directory
            if not visual_root.exists():
                continue
            for visual in _walk_regular_files(visual_root, archival=True):
                if visual.suffix.casefold() not in _ARCHIVAL_IMAGE_SUFFIXES:
                    continue
                relative = visual.relative_to(visual_root).as_posix()
                _add_entry(
                    entries,
                    _source_entry(
                        f"workspace/sources/{component}/{directory}/{relative}",
                        kind="private-source-visual",
                        source_root=visual_root,
                        source_path=visual,
                    ),
                )


def _read_archival_json(
    root: Path,
    path: Path,
    workspace: Workspace,
    *,
    expected_sha256: str | None = None,
) -> object:
    with _open_regular_beneath(root, path) as (descriptor, metadata):
        if metadata.st_size > 64 * 1024 * 1024:
            raise ProcessingError("Archival JSON evidence exceeds its size limit")
        with os.fdopen(os.dup(descriptor), "rb") as handle:
            payload = handle.read()
    if expected_sha256 is not None and sha256(payload).hexdigest() != expected_sha256:
        raise ProcessingError(f"Canonical archival evidence failed its digest check: {path.name}")
    try:
        parsed = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProcessingError(f"Archival evidence is invalid JSON: {path.name}") from exc
    return _archival_sanitize(parsed, workspace)


def _archival_build_entries(
    workspace: Workspace,
    entries: dict[str, _Entry],
    root: Path,
    destination_root: str,
) -> None:
    if not root.exists():
        return
    if root.is_symlink() or not root.is_dir():
        raise ProcessingError("Archival build evidence root is unsafe")
    for build in sorted(root.iterdir(), key=lambda path: path.name):
        if build.is_symlink():
            raise ProcessingError("Archival build evidence cannot use symbolic links")
        if not build.is_dir():
            continue
        component = _safe_component(build.name, prefix="build")
        for name in sorted(_ARCHIVAL_BUILD_FILES):
            path = build / name
            if not path.exists():
                continue
            value = _read_archival_json(root, path, workspace)
            _add_entry(
                entries,
                _payload_entry(
                    f"{destination_root}/{component}/{name}",
                    _json_bytes(value),
                    kind="private-sanitized-build-record",
                ),
            )


def _archival_canonical_entries(workspace: Workspace, entries: dict[str, _Entry]) -> None:
    base = Workspace.open(workspace.root)
    roots = [base.analysis_dir / "records"]
    if workspace.edition_dir is not None:
        roots.append(workspace.edition_dir / "analysis" / "records")
    with base.connect() as connection:
        rows = connection.execute(
            "SELECT path, digest FROM canonical_records ORDER BY path"
        ).fetchall()
    for row in rows:
        relative = Path(str(row["path"]))
        source = workspace.root / relative
        lexical_source = Path(os.path.abspath(source))
        if not any(lexical_source.is_relative_to(Path(os.path.abspath(root))) for root in roots):
            continue
        destination = f"workspace/{relative.as_posix()}"
        suffix = source.suffix.casefold()
        if suffix == ".json":
            value = _read_archival_json(
                workspace.root,
                source,
                workspace,
                expected_sha256=str(row["digest"]),
            )
            _add_entry(
                entries,
                _payload_entry(
                    destination,
                    _json_bytes(value),
                    kind="private-sanitized-canonical-record",
                ),
            )
        elif suffix in _ARCHIVAL_IMAGE_SUFFIXES:
            _add_entry(
                entries,
                _source_entry(
                    destination,
                    kind="private-canonical-binary",
                    source_root=workspace.root,
                    source_path=source,
                    expected_sha256=str(row["digest"]),
                ),
            )


def _archival_evidence_entries(workspace: Workspace, entries: dict[str, _Entry]) -> None:
    observations: list[object] = []
    for source in workspace.list_sources(include_inactive=True):
        observations.extend(
            item.model_dump(mode="json")
            for item in workspace.observations(source.id, limit=1_000_000)
        )
    _add_entry(
        entries,
        _payload_entry(
            "workspace/observations/observations.jsonl",
            _jsonl_bytes(cast(list[object], _archival_sanitize(observations, workspace))),
            kind="private-sanitized-evidence-record",
        ),
    )
    _add_entry(
        entries,
        _payload_entry(
            "workspace/observations/gaps.json",
            _json_bytes(
                _archival_sanitize(
                    [item.model_dump(mode="json") for item in workspace.gaps(limit=None)],
                    workspace,
                )
            ),
            kind="private-sanitized-evidence-record",
        ),
    )


def _external_archival_media(workspace: Workspace, entries: dict[str, _Entry]) -> None:
    base = Workspace.open(workspace.root)
    for source in base.list_sources(include_inactive=True):
        component = _safe_component(source.id, prefix="source")
        row = base.materialization_record(source.id)
        if row is None:
            continue
        candidates: list[tuple[str, Path]] = []
        if row["media_path"]:
            candidates.append(("media", Path(str(row["media_path"]))))
        try:
            caption_values = json.loads(str(row["caption_paths_json"]))
        except json.JSONDecodeError as exc:
            raise ProcessingError("Workspace caption materialization record is invalid") from exc
        if not isinstance(caption_values, list) or not all(
            isinstance(value, str) for value in caption_values
        ):
            raise ProcessingError("Workspace caption materialization record is invalid")
        candidates.extend(
            (f"caption-{index:03d}", Path(value)) for index, value in enumerate(caption_values, 1)
        )
        for label, candidate in candidates:
            lexical = Path(os.path.abspath(candidate))
            try:
                lexical.relative_to(workspace.root)
            except ValueError:
                pass
            else:
                continue
            if source.platform.value != "local":
                raise ProcessingError("Remote source materialization escapes the workspace")
            local_media = Path(os.path.abspath(source.locator))
            if label == "media":
                authorized = lexical == local_media
            else:
                authorized = (
                    lexical.parent == local_media.parent
                    and lexical.name.startswith(f"{local_media.stem}.")
                    and lexical.suffix.casefold() in _CAPTION_SUFFIXES
                )
            if not authorized:
                raise ProcessingError("Local source materialization escapes its source boundary")
            if _name_is_sensitive(lexical):
                raise ProcessingError("Private archival media cannot use a secret-bearing filename")
            if not lexical.exists():
                continue
            suffix = lexical.suffix.casefold()
            destination = f"workspace/external-sources/{component}/{label}{suffix}"
            _add_entry(
                entries,
                _source_entry(
                    destination,
                    kind="private-external-source",
                    source_root=lexical.parent,
                    source_path=lexical,
                    allow_hardlinks=True,
                ),
            )


def _archival_entries(workspace: Workspace, temporary_root: Path) -> dict[str, _Entry]:
    entries: dict[str, _Entry] = {}
    base = Workspace.open(workspace.root)
    _add_entry(
        entries,
        _payload_entry(
            "workspace/manifest.json",
            _json_bytes(_archival_sanitize(_sanitized_workspace_manifest(workspace), workspace)),
            kind="sanitized-workspace-manifest",
        ),
    )
    _add_entry(entries, _sqlite_snapshot_entry(workspace, temporary_root))
    _archival_source_entries(workspace, entries)
    _archival_canonical_entries(workspace, entries)
    _archival_build_entries(workspace, entries, base.builds_dir, "workspace/builds")
    _archival_evidence_entries(workspace, entries)
    coverage_path = workspace.root / "coverage.json"
    if coverage_path.exists():
        _add_entry(
            entries,
            _payload_entry(
                "workspace/coverage.json",
                _json_bytes(_read_archival_json(workspace.root, coverage_path, workspace)),
                kind="private-sanitized-evidence-record",
            ),
        )
    if workspace.edition_id is not None:
        assert workspace.edition_dir is not None
        edition = _sanitized_edition_state(workspace)
        assert edition is not None
        _add_entry(
            entries,
            _payload_entry(
                f"workspace/editions/{workspace.edition_id}/edition.json",
                _json_bytes(_archival_sanitize(edition, workspace)),
                kind="sanitized-edition-state",
            ),
        )
        _archival_build_entries(
            workspace,
            entries,
            workspace.edition_dir / "builds",
            f"workspace/editions/{workspace.edition_id}/builds",
        )
    _external_archival_media(workspace, entries)
    _add_entry(
        entries,
        _payload_entry(
            "workspace/logs/tool-runs.jsonl",
            _jsonl_bytes(cast(list[object], base.tool_run_records())),
            kind="sanitized-tool-runs",
        ),
    )
    return entries


def _compact_entries(
    workspace: Workspace,
    *,
    authorize_transcript_redistribution: bool,
) -> dict[str, _Entry]:
    entries: dict[str, _Entry] = {}
    _compact_evidence_records(workspace, entries)
    _compact_canonical_entries(workspace, entries)
    _compact_visual_entries(workspace, entries)
    _compact_build_entries(workspace, entries)
    if authorize_transcript_redistribution:
        _compact_transcript_entries(workspace, entries)
    return entries


def _source_lineage(workspace: Workspace) -> list[dict[str, Any]]:
    lineage: list[dict[str, Any]] = []
    base = Workspace.open(workspace.root)
    for source in base.list_sources(include_inactive=True):
        metadata = _source_metadata(source, workspace)
        row = base.materialization_record(source.id)
        lineage.append(
            {
                "source_id": metadata["id"],
                "descriptor_sha256": sha256(_json_bytes(metadata)).hexdigest(),
                "content_hash": str(row["content_hash"]) if row and row["content_hash"] else None,
            }
        )
    return lineage


def _edition_lineage(workspace: Workspace) -> dict[str, Any] | None:
    state = _sanitized_edition_state(workspace)
    if state is None:
        return None
    return {
        "edition_id": state["edition_id"],
        "edition_name": state["edition_name"],
        "config_digest": state["config_digest"],
        "analysis_lineage_digest": cast(dict[str, Any], state["analysis_lineage"])[
            "lineage_digest"
        ],
        "selected_curriculum_path_id": state["selected_curriculum_path_id"],
        "selected_curriculum_digest": state["selected_curriculum_digest"],
    }


def _manifest(
    workspace: Workspace,
    *,
    mode: EvidenceBundleMode,
    transcript_authorized: bool,
    entries: dict[str, _Entry],
) -> EvidenceBundleManifest:
    base = Workspace.open(workspace.root)
    manifest = base.load_manifest()
    files = [
        EvidenceBundleFile(path=item.path, sha256=item.sha256, size=item.size, kind=item.kind)
        for item in sorted(entries.values(), key=lambda item: item.path)
    ]
    payload: dict[str, Any] = {
        "schema_version": BUNDLE_SCHEMA_VERSION,
        "format": BUNDLE_FORMAT,
        "mode": mode.value,
        "generator": {"name": "video-to-skill", "version": __version__},
        "workspace_schema_version": SCHEMA_VERSION,
        "workspace_job_id": _sanitized_string(manifest.job_id, workspace),
        "workspace_snapshot_digest": base.workspace_snapshot_digest(),
        "analysis_depth": (
            manifest.analysis_depth.model_dump(mode="json")
            if manifest.analysis_depth is not None
            else None
        ),
        "source_lineage": cast(
            list[dict[str, Any]],
            _sanitize_json(_source_lineage(workspace), workspace),
        ),
        "edition": _edition_lineage(workspace),
        "transcript_redistribution_authorized": transcript_authorized,
        "private_sensitive": mode == EvidenceBundleMode.ARCHIVAL,
        "files": [item.model_dump(mode="json") for item in files],
    }
    bundle_id = "bundle-" + stable_hash(payload, length=32)
    return EvidenceBundleManifest(bundle_id=bundle_id, **payload)


def _member_path_forbidden(path: str) -> bool:
    parts = PurePosixPath(path).parts
    if any(part.casefold() in _EXCLUDED_DIRECTORY_NAMES for part in parts):
        return True
    if any(part in _GENERATED_SKILL_FILES for part in parts):
        return True
    return any(_name_is_sensitive(Path(part)) for part in parts)


def _compact_record_allowed(record: EvidenceBundleFile) -> bool:
    if _COMPACT_EXACT_KINDS.get(record.path) == record.kind:
        return True
    path = PurePosixPath(record.path)
    parts = path.parts
    if (
        len(parts) == 3
        and parts[0] == "sources"
        and parts[2] == "metadata.json"
        and record.kind == "source-metadata"
    ):
        return True
    if (
        len(parts) == 3
        and parts[:2] == ("visuals", "selected")
        and path.suffix.casefold() in {".png", ".jpg", ".jpeg", ".webp"}
        and record.kind == "selected-visual"
    ):
        return True
    if (
        len(parts) >= 4
        and parts[:2] == ("visuals", "contact-sheets")
        and path.suffix.casefold() in {".png", ".jpg", ".jpeg", ".webp"}
        and record.kind == "contact-sheet"
    ):
        return True
    if len(parts) == 3 and parts[0] == "builds" and _BUILD_KINDS.get(parts[2]) == record.kind:
        return True
    if len(parts) == 3 and parts[0] == "transcripts":
        if parts[2] == "normalized.jsonl":
            return record.kind == "authorized-normalized-transcript"
        if (
            re.fullmatch(r"caption-[0-9]{3}\.[A-Za-z0-9]+", parts[2])
            and path.suffix.casefold() in _CAPTION_SUFFIXES
        ):
            return record.kind == "authorized-caption"
    return False


def _archival_record_allowed(record: EvidenceBundleFile) -> bool:
    if _ARCHIVAL_EXACT_KINDS.get(record.path) == record.kind:
        return True
    parts = PurePosixPath(record.path).parts
    if (
        len(parts) == 4
        and parts[:2] == ("workspace", "external-sources")
        and re.fullmatch(r"(?:media|caption-[0-9]{3})\.[A-Za-z0-9]+", parts[3])
        and record.kind == "private-external-source"
    ):
        return True
    if (
        len(parts) == 4
        and parts[:2] == ("workspace", "editions")
        and parts[3] == "edition.json"
        and record.kind == "sanitized-edition-state"
    ):
        return True
    if parts[:2] == ("workspace", "editions") and parts[-1] == "edition.json":
        return False
    if len(parts) == 4 and parts[:2] == ("workspace", "sources"):
        suffix = PurePosixPath(record.path).suffix.casefold()
        if parts[3] == f"media{suffix}" and suffix in _ARCHIVAL_MEDIA_SUFFIXES:
            return record.kind == "private-source-media"
        if parts[3] == "audio-16khz.wav":
            return record.kind == "private-source-audio"
        return False
    if (
        len(parts) >= 5
        and parts[:2] == ("workspace", "sources")
        and parts[3] in {"frames", "investigation-frames", "contact-sheets"}
        and PurePosixPath(record.path).suffix.casefold() in _ARCHIVAL_IMAGE_SUFFIXES
    ):
        return record.kind == "private-source-visual"
    shared_canonical = len(parts) == 6 and parts[:3] == (
        "workspace",
        "analysis",
        "records",
    )
    edition_canonical = (
        len(parts) == 8
        and parts[0] == "workspace"
        and parts[1] == "editions"
        and parts[3:5]
        == (
            "analysis",
            "records",
        )
    )
    if shared_canonical or edition_canonical:
        if PurePosixPath(record.path).suffix.casefold() == ".json":
            return record.kind == "private-sanitized-canonical-record"
        if PurePosixPath(record.path).suffix.casefold() in _ARCHIVAL_IMAGE_SUFFIXES:
            return record.kind == "private-canonical-binary"
        return False
    shared_build = len(parts) == 4 and parts[:2] == ("workspace", "builds")
    edition_build = (
        len(parts) == 6
        and parts[0] == "workspace"
        and parts[1] == "editions"
        and parts[3] == "builds"
    )
    if shared_build or edition_build:
        return (
            parts[-1] in _ARCHIVAL_BUILD_FILES and record.kind == "private-sanitized-build-record"
        )
    return False


def _validate_archival_value(value: object) -> None:
    verifier_root = Path("/__video_to_skill_archival_verifier__")
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str) or _archival_key_is_private(key):
                raise ProcessingError("Archival JSON contains a forbidden private key")
            try:
                sanitized_key = sanitize_value(key, workspace_root=verifier_root)
            except ValueError as exc:
                raise ProcessingError(f"Archival JSON key is invalid: {exc}") from exc
            if sanitized_key != key:
                raise ProcessingError("Archival JSON contains an unsafe key")
            _validate_archival_value(item)
        return
    if isinstance(value, list):
        for item in value:
            _validate_archival_value(item)
        return
    if isinstance(value, str):
        try:
            sanitized = sanitize_value(value, workspace_root=verifier_root)
        except ValueError as exc:
            raise ProcessingError(f"Archival JSON string is invalid: {exc}") from exc
        if sanitized != value:
            raise ProcessingError("Archival JSON contains an unsafe string")
        return
    if value is not None and not isinstance(value, (bool, int, float)):
        raise ProcessingError("Archival JSON contains an unsupported value")


def _parse_archival_json(payload: bytes, *, json_lines: bool) -> list[object]:
    try:
        if json_lines:
            values = [json.loads(line) for line in payload.splitlines() if line.strip()]
        else:
            values = [json.loads(payload)]
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProcessingError(f"Sanitized archival JSON is invalid: {exc}") from exc
    for value in values:
        _validate_archival_value(value)
    return values


def _validate_archival_json_members(
    archive: zipfile.ZipFile, manifest: EvidenceBundleManifest
) -> None:
    json_kinds = {
        "private-sanitized-build-record",
        "private-sanitized-canonical-record",
        "private-sanitized-evidence-record",
        "sanitized-edition-state",
        "sanitized-tool-runs",
        "sanitized-workspace-manifest",
    }
    for record in manifest.files:
        if record.kind not in json_kinds:
            continue
        json_lines = record.path.endswith(".jsonl")
        _parse_archival_json(archive.read(record.path), json_lines=json_lines)


def _database_canonical_path_allowed(path: str, manifest: EvidenceBundleManifest) -> bool:
    candidate = PurePosixPath(path)
    if candidate.is_absolute() or candidate.as_posix() != path:
        return False
    parts = candidate.parts
    if any(part in {"", ".", ".."} for part in parts):
        return False
    if len(parts) == 5 and parts[:2] == ("analysis", "records"):
        return True
    if manifest.edition is None:
        return False
    edition_id = manifest.edition.get("edition_id")
    return (
        len(parts) == 8
        and parts[:2] == ("editions", edition_id)
        and parts[2:4] == ("analysis", "records")
    )


def _validate_database_json_row(
    raw: object,
    model: type[BaseModel],
    *,
    label: str,
) -> dict[str, Any]:
    try:
        parsed = json.loads(str(raw))
        validated = model.model_validate(parsed).model_dump(mode="json")
    except (json.JSONDecodeError, ValidationError) as exc:
        raise ProcessingError(f"Archival database {label} is invalid: {exc}") from exc
    _validate_archival_value(parsed)
    if parsed != validated:
        raise ProcessingError(f"Archival database {label} is not canonical")
    return cast(dict[str, Any], parsed)


def _verify_archival_database(
    archive: zipfile.ZipFile,
    manifest: EvidenceBundleManifest,
) -> set[str]:
    with tempfile.TemporaryDirectory(prefix="video-to-skill-verify-db-") as temporary:
        database = Path(temporary) / "evidence.sqlite3"
        with archive.open("workspace/evidence.sqlite3") as source, database.open("wb") as output:
            while chunk := source.read(_CHUNK_SIZE):
                output.write(chunk)
        try:
            with sqlite3.connect(f"file:{database}?mode=ro", uri=True) as connection:
                connection.row_factory = sqlite3.Row
                integrity = connection.execute("PRAGMA integrity_check").fetchall()
                if [str(row[0]) for row in integrity] != ["ok"]:
                    raise ProcessingError("Archival evidence database failed its integrity check")
                if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
                    raise ProcessingError("Archival evidence database has broken references")
                objects = connection.execute(
                    "SELECT type, name FROM sqlite_master "
                    "WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name"
                ).fetchall()
                tables = {str(row["name"]) for row in objects if row["type"] == "table"}
                if tables != set(_ARCHIVAL_DB_COLUMNS) or any(
                    row["type"] in {"trigger", "view"} for row in objects
                ):
                    raise ProcessingError("Archival evidence database schema is forbidden")
                for table, expected_columns in _ARCHIVAL_DB_COLUMNS.items():
                    columns = tuple(
                        str(row["name"])
                        for row in connection.execute(f"PRAGMA table_info({table})")
                    )
                    if columns != expected_columns:
                        raise ProcessingError(
                            f"Archival evidence database columns are forbidden: {table}"
                        )
                metadata = connection.execute(
                    "SELECT key, value FROM archive_metadata ORDER BY key"
                ).fetchall()
                if [(row["key"], row["value"]) for row in metadata] != [
                    ("archive_schema_version", "1")
                ]:
                    raise ProcessingError("Archival evidence database metadata is invalid")
                lineage = {
                    str(item["source_id"]): str(item["descriptor_sha256"])
                    for item in manifest.source_lineage
                }
                source_ids: set[str] = set()
                for row in connection.execute(
                    "SELECT id, descriptor_json, content_hash FROM sources ORDER BY id"
                ):
                    source_id = str(row["id"])
                    _validate_archival_value(source_id)
                    try:
                        descriptor = json.loads(str(row["descriptor_json"]))
                    except json.JSONDecodeError as exc:
                        raise ProcessingError("Archival source metadata is invalid") from exc
                    if (
                        not isinstance(descriptor, dict)
                        or set(descriptor) != _ARCHIVAL_SOURCE_METADATA_KEYS
                        or descriptor.get("id") != source_id
                    ):
                        raise ProcessingError("Archival source metadata identity is invalid")
                    _validate_archival_value(descriptor)
                    if sha256(_json_bytes(descriptor)).hexdigest() != lineage.get(source_id):
                        raise ProcessingError("Archival source metadata differs from lineage")
                    if row["content_hash"] is not None:
                        _validate_archival_value(str(row["content_hash"]))
                    source_ids.add(source_id)
                if source_ids != set(lineage):
                    raise ProcessingError("Archival database sources differ from lineage")
                typed_tables: tuple[tuple[str, type[BaseModel], tuple[str, ...]], ...] = (
                    (
                        "transcript_segments",
                        TranscriptSegment,
                        ("id", "source_id", "start", "end"),
                    ),
                    ("visual_events", VisualEvent, ("id", "source_id", "timestamp")),
                    (
                        "semantic_segments",
                        SemanticSegment,
                        ("id", "source_id", "ordinal", "start", "end"),
                    ),
                    (
                        "agent_observations",
                        AgentObservation,
                        ("id", "source_id", "start", "end"),
                    ),
                    ("evidence_gaps", EvidenceGap, ("id", "source_id", "start", "end")),
                )
                for table, model, scalar_columns in typed_tables:
                    selected = ", ".join((*scalar_columns, "data_json"))
                    for row in connection.execute(f"SELECT {selected} FROM {table} ORDER BY id"):
                        canonical = _validate_database_json_row(
                            row["data_json"], model, label=f"{table}.data_json"
                        )
                        for column in scalar_columns:
                            if row[column] != canonical[column]:
                                raise ProcessingError(
                                    f"Archival database {table}.{column} differs from data_json"
                                )
                            _validate_archival_value(row[column])
                for row in connection.execute(
                    "SELECT kind, record_id, revision, path, digest, created_at "
                    "FROM canonical_records ORDER BY kind, record_id, revision"
                ):
                    for column in ("kind", "record_id", "created_at"):
                        _validate_archival_value(str(row[column]))
                    if not _database_canonical_path_allowed(str(row["path"]), manifest):
                        raise ProcessingError("Archival canonical record path is forbidden")
                    if not re.fullmatch(r"[a-f0-9]{64}", str(row["digest"])):
                        raise ProcessingError("Archival canonical record digest is invalid")
                return source_ids
        except sqlite3.Error as exc:
            raise ProcessingError(f"Archival evidence database is invalid: {exc}") from exc


def _validate_archival_contents(archive: zipfile.ZipFile, manifest: EvidenceBundleManifest) -> None:
    _validate_archival_json_members(archive, manifest)
    source_ids = _verify_archival_database(archive, manifest)
    source_components = {_safe_component(source_id, prefix="source") for source_id in source_ids}
    for record in manifest.files:
        parts = PurePosixPath(record.path).parts
        if (
            len(parts) >= 3
            and parts[:2]
            in {
                ("workspace", "sources"),
                ("workspace", "external-sources"),
            }
            and parts[2] not in source_components
        ):
            raise ProcessingError("Archival source asset has no database lineage")


def _validate_manifest_policy(manifest: EvidenceBundleManifest) -> None:
    paths = [record.path for record in manifest.files]
    if len(paths) != len(set(paths)):
        raise ProcessingError("Evidence bundle manifest contains duplicate file paths")
    if any(_member_path_forbidden(path) for path in paths):
        raise ProcessingError("Evidence bundle manifest contains a forbidden path")
    path_set = set(paths)
    if manifest.mode == EvidenceBundleMode.COMPACT:
        if manifest.private_sensitive:
            raise ProcessingError("Compact evidence bundles cannot be marked private-sensitive")
        if not path_set >= _COMPACT_REQUIRED:
            raise ProcessingError("Compact evidence bundle is missing required derivatives")
        if not all(_compact_record_allowed(record) for record in manifest.files):
            raise ProcessingError("Compact evidence bundle contains a forbidden member or kind")
        transcript_records = [
            record
            for record in manifest.files
            if record.kind in {"authorized-normalized-transcript", "authorized-caption"}
            or record.path.startswith("transcripts/")
        ]
        if bool(transcript_records) != manifest.transcript_redistribution_authorized:
            raise ProcessingError(
                "Compact transcript members do not match redistribution authorization"
            )
        source_records = [record for record in manifest.files if record.kind == "source-metadata"]
        if len(source_records) != len(manifest.source_lineage):
            raise ProcessingError("Compact source metadata does not match source lineage")
    else:
        if not manifest.private_sensitive:
            raise ProcessingError("Archival evidence bundles must be marked private-sensitive")
        if manifest.transcript_redistribution_authorized:
            raise ProcessingError(
                "Private archival bundles cannot claim transcript redistribution authorization"
            )
        if not path_set >= _ARCHIVAL_REQUIRED:
            raise ProcessingError("Archival evidence bundle is missing required private records")
        if not all(_archival_record_allowed(record) for record in manifest.files):
            raise ProcessingError("Archival evidence bundle contains a forbidden member or kind")
        edition_records = [
            record for record in manifest.files if record.kind == "sanitized-edition-state"
        ]
        if manifest.edition is None:
            if edition_records or any(path.startswith("workspace/editions/") for path in paths):
                raise ProcessingError("Legacy archival bundle cannot contain edition records")
        else:
            edition_id = manifest.edition.get("edition_id")
            expected = f"workspace/editions/{edition_id}/edition.json"
            if len(edition_records) != 1 or edition_records[0].path != expected:
                raise ProcessingError("Archival edition lineage does not match its state record")


def _zip_info(name: str) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(_safe_member_name(name), date_time=_FIXED_ZIP_TIME)
    info.create_system = 3
    info.compress_type = zipfile.ZIP_STORED
    info.external_attr = (stat.S_IFREG | 0o600) << 16
    info.flag_bits |= 0x800
    return info


def _zip64_extra_only(extra: bytes) -> bool:
    if not extra:
        return True
    offset = 0
    records = 0
    while offset + 4 <= len(extra):
        header_id = int.from_bytes(extra[offset : offset + 2], "little")
        length = int.from_bytes(extra[offset + 2 : offset + 4], "little")
        offset += 4
        if header_id != 0x0001 or length == 0 or offset + length > len(extra):
            return False
        records += 1
        offset += length
    return offset == len(extra) and records == 1


def _validate_zip_shape(info: zipfile.ZipInfo) -> None:
    if info.date_time != _FIXED_ZIP_TIME:
        raise ProcessingError("Evidence bundle member has a non-canonical timestamp")
    if info.create_system != 3:
        raise ProcessingError("Evidence bundle member has a non-canonical creator system")
    if info.external_attr != (stat.S_IFREG | 0o600) << 16:
        raise ProcessingError("Evidence bundle member has non-canonical permissions")
    if info.compress_type != zipfile.ZIP_STORED:
        raise ProcessingError("Evidence bundle uses an unsupported compression method")
    expected_flags = 0 if info.filename.isascii() else 0x800
    if info.flag_bits != expected_flags:
        raise ProcessingError("Evidence bundle member has unsupported ZIP flags")
    if info.comment:
        raise ProcessingError("Evidence bundle member comments are not allowed")
    if info.extra and (
        max(info.file_size, info.compress_size) < zipfile.ZIP64_LIMIT
        or not _zip64_extra_only(info.extra)
    ):
        raise ProcessingError("Evidence bundle member extras are not canonical")
    needs_zip64 = max(info.file_size, info.compress_size) >= zipfile.ZIP64_LIMIT
    expected_version = 45 if needs_zip64 else 20
    if info.create_version != expected_version or info.extract_version != expected_version:
        raise ProcessingError("Evidence bundle member has a non-canonical ZIP version")
    if info.internal_attr != 0 or info.volume != 0 or info.reserved != 0:
        raise ProcessingError("Evidence bundle member has non-canonical ZIP metadata")


def _validate_local_zip_header(raw: BinaryIO, info: zipfile.ZipInfo) -> None:
    raw.seek(info.header_offset)
    header = raw.read(30)
    if len(header) != 30:
        raise ProcessingError("Evidence bundle local ZIP header is truncated")
    (
        signature,
        extract_version,
        flags,
        compression,
        dos_time,
        dos_date,
        crc,
        compressed_size,
        file_size,
        filename_length,
        extra_length,
    ) = struct.unpack("<4s5H3L2H", header)
    if signature != b"PK\x03\x04":
        raise ProcessingError("Evidence bundle local ZIP header signature is invalid")
    if flags != info.flag_bits:
        raise ProcessingError("Evidence bundle local ZIP flags differ from the central record")
    if compression != info.compress_type:
        raise ProcessingError(
            "Evidence bundle local ZIP compression differs from the central record"
        )
    year, month, day, hour, minute, second = info.date_time
    expected_time = (hour << 11) | (minute << 5) | (second // 2)
    expected_date = ((year - 1980) << 9) | (month << 5) | day
    if dos_time != expected_time or dos_date != expected_date:
        raise ProcessingError("Evidence bundle local ZIP timestamp differs from the central record")
    expected_filename = info.filename.encode("utf-8" if info.flag_bits & 0x800 else "ascii")
    filename = raw.read(filename_length)
    extra = raw.read(extra_length)
    if filename != expected_filename:
        raise ProcessingError("Evidence bundle local ZIP filename differs from the central record")
    needs_zip64 = max(info.file_size, info.compress_size) >= zipfile.ZIP64_LIMIT
    if needs_zip64:
        expected_extra = (
            struct.pack("<HH", 0x0001, 16)
            + info.file_size.to_bytes(8, "little")
            + info.compress_size.to_bytes(8, "little")
        )
        expected_compressed_size = 0xFFFFFFFF
        expected_file_size = 0xFFFFFFFF
        expected_version = 45
    else:
        expected_extra = b""
        expected_compressed_size = info.compress_size
        expected_file_size = info.file_size
        expected_version = 20
    if extra != expected_extra:
        raise ProcessingError("Evidence bundle local ZIP extras are not canonical")
    if extract_version != expected_version:
        raise ProcessingError("Evidence bundle local ZIP version is not canonical")
    if (
        crc != info.CRC
        or compressed_size != expected_compressed_size
        or file_size != expected_file_size
    ):
        raise ProcessingError(
            "Evidence bundle local ZIP sizes or CRC differ from the central record"
        )


def _write_entry(archive: zipfile.ZipFile, entry: _Entry) -> None:
    digest = sha256()
    size = 0
    with archive.open(
        _zip_info(entry.path),
        "w",
        force_zip64=entry.size >= zipfile.ZIP64_LIMIT,
    ) as output:
        if entry.payload is not None:
            output.write(entry.payload)
            digest.update(entry.payload)
            size = len(entry.payload)
        else:
            assert entry.source_root is not None and entry.source_path is not None
            with _open_regular_beneath(
                entry.source_root,
                entry.source_path,
                allow_hardlinks=entry.allow_hardlinks,
            ) as (descriptor, metadata):
                with os.fdopen(os.dup(descriptor), "rb") as source:
                    while chunk := source.read(_CHUNK_SIZE):
                        output.write(chunk)
                        digest.update(chunk)
                        size += len(chunk)
                if size != metadata.st_size:
                    raise ProcessingError("Evidence bundle input changed during archive creation")
    if digest.hexdigest() != entry.sha256 or size != entry.size:
        raise ProcessingError("Evidence bundle input changed after manifest construction")


def _write_bundle(
    handle: BinaryIO, manifest: EvidenceBundleManifest, entries: dict[str, _Entry]
) -> None:
    manifest_payload = _json_bytes(manifest)
    with zipfile.ZipFile(
        handle, mode="w", compression=zipfile.ZIP_STORED, allowZip64=True
    ) as archive:
        archive.writestr(_zip_info("bundle-manifest.json"), manifest_payload)
        for entry in sorted(entries.values(), key=lambda item: item.path):
            _write_entry(archive, entry)


def _hash_open_file(handle: BinaryIO) -> tuple[str, int]:
    handle.seek(0)
    digest = sha256()
    size = 0
    while chunk := handle.read(_CHUNK_SIZE):
        digest.update(chunk)
        size += len(chunk)
    handle.seek(0)
    return digest.hexdigest(), size


def _files_equal(first_fd: int, second_fd: int) -> bool:
    first_stat = os.fstat(first_fd)
    second_stat = os.fstat(second_fd)
    if first_stat.st_size != second_stat.st_size:
        return False
    os.lseek(first_fd, 0, os.SEEK_SET)
    os.lseek(second_fd, 0, os.SEEK_SET)
    while True:
        first = os.read(first_fd, _CHUNK_SIZE)
        second = os.read(second_fd, _CHUNK_SIZE)
        if first != second:
            return False
        if not first:
            return True


def _destination(workspace: Workspace, output: Path) -> Path:
    destination = Path(os.path.abspath(output.expanduser()))
    if destination.suffix.casefold() != BUNDLE_EXTENSION:
        raise ProcessingError(f"Evidence bundle output must end with {BUNDLE_EXTENSION}")
    root = Path(os.path.abspath(workspace.root))
    if destination == root or destination.is_relative_to(root):
        raise ProcessingError("Evidence bundles must be written outside the source workspace")
    destination.parent.mkdir(parents=True, exist_ok=True)
    for component in [destination.parent, *destination.parent.parents]:
        if component.is_symlink():
            raise ProcessingError("Evidence bundle output cannot use symlinked directories")
    return destination


def _publish_bundle(
    workspace: Workspace,
    output: Path,
    manifest: EvidenceBundleManifest,
    entries: dict[str, _Entry],
) -> EvidenceBundleReport:
    destination = _destination(workspace, output)
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | nofollow
    parent_fd = os.open(destination.parent, directory_flags)
    temporary_name = f".{destination.name}.{secrets.token_hex(12)}.tmp"
    temporary_fd = -1
    existing_identical = False
    try:
        temporary_fd = os.open(
            temporary_name,
            os.O_RDWR | os.O_CREAT | os.O_EXCL | nofollow,
            0o600,
            dir_fd=parent_fd,
        )
        with os.fdopen(os.dup(temporary_fd), "w+b") as handle:
            _write_bundle(handle, manifest, entries)
            handle.flush()
            os.fsync(handle.fileno())
        temporary_path = destination.parent / temporary_name
        verification = verify_evidence_bundle(temporary_path)
        if verification.bundle_id != manifest.bundle_id:
            raise ProcessingError("Evidence bundle self-verification returned a different identity")
        try:
            os.link(
                temporary_name,
                destination.name,
                src_dir_fd=parent_fd,
                dst_dir_fd=parent_fd,
                follow_symlinks=False,
            )
        except FileExistsError:
            metadata = os.stat(destination.name, dir_fd=parent_fd, follow_symlinks=False)
            if not stat.S_ISREG(metadata.st_mode):
                raise ProcessingError(
                    "Evidence bundle output target is not a regular file"
                ) from None
            existing_fd = os.open(destination.name, os.O_RDONLY | nofollow, dir_fd=parent_fd)
            try:
                if not _files_equal(temporary_fd, existing_fd):
                    raise ProcessingError(
                        "Evidence bundle output already exists with different content"
                    )
            finally:
                os.close(existing_fd)
            existing_identical = True
        os.chmod(destination.name, 0o600, dir_fd=parent_fd, follow_symlinks=False)
        os.fsync(parent_fd)
        with os.fdopen(os.dup(temporary_fd), "rb") as handle:
            digest, size = _hash_open_file(handle)
    except OSError as exc:
        raise ProcessingError(f"Evidence bundle output path is unsafe: {destination}") from exc
    finally:
        if temporary_fd >= 0:
            os.close(temporary_fd)
        with suppress(FileNotFoundError):
            os.unlink(temporary_name, dir_fd=parent_fd)
        os.close(parent_fd)
    warning = (
        "PRIVATE ARCHIVAL BUNDLE: contains sensitive source evidence; keep it out of normal Git "
        "history and generated Skill distributions."
        if manifest.private_sensitive
        else None
    )
    return EvidenceBundleReport(
        path=destination,
        bundle_id=manifest.bundle_id,
        mode=manifest.mode,
        sha256=digest,
        size=size,
        files=len(entries),
        existing_identical=existing_identical,
        private_sensitive=manifest.private_sensitive,
        warning=warning,
    )


def export_evidence_bundle(
    workspace: Workspace,
    output: Path,
    *,
    mode: EvidenceBundleMode | str = EvidenceBundleMode.COMPACT,
    authorize_transcript_redistribution: bool = False,
    confirm_private_archival: bool = False,
) -> EvidenceBundleReport:
    """Export one deterministic evidence bundle without invoking an LLM."""

    try:
        resolved_mode = EvidenceBundleMode(mode)
    except ValueError as exc:
        raise ProcessingError("Evidence bundle mode must be 'compact' or 'archival'") from exc
    if resolved_mode == EvidenceBundleMode.ARCHIVAL and not confirm_private_archival:
        raise ProcessingError("Private archival export requires --confirm-private-archival")
    if resolved_mode == EvidenceBundleMode.ARCHIVAL and authorize_transcript_redistribution:
        raise ProcessingError(
            "Transcript redistribution authorization applies only to compact bundles"
        )
    with tempfile.TemporaryDirectory(prefix="video-to-skill-bundle-") as temporary:
        temporary_root = Path(temporary).resolve()
        if resolved_mode == EvidenceBundleMode.COMPACT:
            entries = _compact_entries(
                workspace,
                authorize_transcript_redistribution=authorize_transcript_redistribution,
            )
        else:
            entries = _archival_entries(workspace, temporary_root)
        if len(entries) > _MAX_BUNDLE_MEMBERS - 1:
            raise ProcessingError("Evidence bundle contains too many files")
        manifest = _manifest(
            workspace,
            mode=resolved_mode,
            transcript_authorized=authorize_transcript_redistribution,
            entries=entries,
        )
        return _publish_bundle(workspace, output, manifest, entries)


def verify_evidence_bundle(path: Path) -> EvidenceBundleReport:
    """Verify member safety, manifest identity, checksums, and deterministic archive shape."""

    archive_path = Path(os.path.abspath(path.expanduser()))
    if archive_path.is_symlink() or not archive_path.is_file():
        raise ProcessingError("Evidence bundle must be a regular non-symlink file")
    digest = sha256()
    size = 0
    with archive_path.open("rb") as raw:
        while chunk := raw.read(_CHUNK_SIZE):
            digest.update(chunk)
            size += len(chunk)
    try:
        with zipfile.ZipFile(archive_path, mode="r") as archive:
            infos = archive.infolist()
            if not infos or len(infos) > _MAX_BUNDLE_MEMBERS:
                raise ProcessingError("Evidence bundle member count is invalid")
            if archive.comment:
                raise ProcessingError("Evidence bundle archive comments are not allowed")
            names = [info.filename for info in infos]
            if len(names) != len(set(names)):
                raise ProcessingError("Evidence bundle contains duplicate members")
            for info in infos:
                _safe_member_name(info.filename)
            for info in infos:
                file_type = (info.external_attr >> 16) & 0o170000
                if file_type not in {0, stat.S_IFREG} or info.is_dir():
                    raise ProcessingError("Evidence bundle contains a non-regular member")
                _validate_zip_shape(info)
            if names[0] != "bundle-manifest.json":
                raise ProcessingError("Evidence bundle manifest must be the first member")
            if infos[0].header_offset != 0:
                raise ProcessingError("Evidence bundle contains a non-canonical archive prefix")
            with archive_path.open("rb") as local_headers:
                for info in infos:
                    _validate_local_zip_header(local_headers, info)
            manifest_info = infos[0]
            if manifest_info.file_size > _MAX_MANIFEST_BYTES:
                raise ProcessingError("Evidence bundle manifest exceeds its size limit")
            try:
                manifest = EvidenceBundleManifest.model_validate_json(archive.read(manifest_info))
            except (UnicodeDecodeError, ValidationError) as exc:
                raise ProcessingError(f"Evidence bundle manifest is invalid: {exc}") from exc
            expected_names = ["bundle-manifest.json", *[item.path for item in manifest.files]]
            if names != expected_names or names[1:] != sorted(names[1:]):
                raise ProcessingError("Evidence bundle members do not match the sorted manifest")
            file_records = {item.path: item for item in manifest.files}
            for info in infos[1:]:
                record = file_records[info.filename]
                if info.file_size != record.size:
                    raise ProcessingError(f"Evidence bundle member size mismatch: {info.filename}")
                member_digest = sha256()
                member_size = 0
                with archive.open(info, "r") as member:
                    while chunk := member.read(_CHUNK_SIZE):
                        member_digest.update(chunk)
                        member_size += len(chunk)
                if member_size != record.size or member_digest.hexdigest() != record.sha256:
                    raise ProcessingError(
                        f"Evidence bundle member checksum mismatch: {info.filename}"
                    )
            _validate_manifest_policy(manifest)
            if manifest.mode == EvidenceBundleMode.ARCHIVAL:
                _validate_archival_contents(archive, manifest)
            identity_payload = manifest.model_dump(
                mode="json",
                exclude={"bundle_id"},
            )
            expected_id = "bundle-" + stable_hash(identity_payload, length=32)
            if manifest.bundle_id != expected_id:
                raise ProcessingError("Evidence bundle identity digest is invalid")
    except (EOFError, KeyError, OSError, RuntimeError, zipfile.BadZipFile) as exc:
        raise ProcessingError(f"Evidence bundle is not a valid archive: {exc}") from exc
    warning = (
        "PRIVATE ARCHIVAL BUNDLE: contains sensitive source evidence; keep it out of normal Git "
        "history and generated Skill distributions."
        if manifest.private_sensitive
        else None
    )
    return EvidenceBundleReport(
        path=archive_path,
        bundle_id=manifest.bundle_id,
        mode=manifest.mode,
        sha256=digest.hexdigest(),
        size=size,
        files=len(manifest.files),
        private_sensitive=manifest.private_sensitive,
        warning=warning,
    )
