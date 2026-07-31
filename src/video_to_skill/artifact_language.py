"""Durable canonical artifact-language negotiation and validation."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from video_to_skill.config import normalize_concrete_language, normalize_output_language
from video_to_skill.errors import ProcessingError
from video_to_skill.utils import atomic_write_json, hash_file, stable_hash
from video_to_skill.workspace import Workspace

ArtifactLanguageResolution = Literal[
    "explicit",
    "source-single",
    "source-mixed",
    "source-unknown",
]
ArtifactLanguageDeclarationState = Literal[
    "resolved",
    "agent-declared",
    "legacy-agent-declared",
]

_UNKNOWN_LANGUAGE_VALUES = {"", "auto", "und", "unknown", "zxx"}
_MIXED_LANGUAGE_VALUES = {"mul", "mixed", "multilingual"}
_CONTRACT_FILENAME = "artifact-language.json"


class ArtifactLanguageModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ArtifactLanguageContract(ArtifactLanguageModel):
    schema_version: Literal[1] = 1
    requested_output_language: str
    resolution: ArtifactLanguageResolution
    fixed_artifact_language: str | None = None
    source_languages: list[str] = Field(default_factory=list, max_length=64)

    @field_validator("requested_output_language")
    @classmethod
    def normalize_request(cls, value: str) -> str:
        return normalize_output_language(value)

    @field_validator("fixed_artifact_language")
    @classmethod
    def normalize_fixed(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return normalize_concrete_language(value)

    @field_validator("source_languages")
    @classmethod
    def normalize_sources(cls, value: list[str]) -> list[str]:
        normalized = [_normalize_observed_language(item) for item in value]
        if any(item is None for item in normalized):
            raise ValueError("source language candidates must be concrete language labels")
        concrete = sorted({str(item) for item in normalized}, key=str.casefold)
        if len(concrete) != len(value):
            raise ValueError("source language candidates must be unique and sorted")
        return concrete

    @model_validator(mode="after")
    def coherent_resolution(self) -> ArtifactLanguageContract:
        if self.resolution == "explicit":
            if (
                self.requested_output_language == "source"
                or self.fixed_artifact_language != self.requested_output_language
                or self.source_languages
            ):
                raise ValueError("explicit artifact-language resolution is inconsistent")
        elif self.resolution == "source-single":
            if (
                self.requested_output_language != "source"
                or self.fixed_artifact_language is None
                or self.source_languages != [self.fixed_artifact_language]
            ):
                raise ValueError("single-source artifact-language resolution is inconsistent")
        elif self.requested_output_language != "source" or self.fixed_artifact_language is not None:
            raise ValueError("mixed or unknown source language must require a declaration")
        return self

    @property
    def requires_agent_declaration(self) -> bool:
        return self.fixed_artifact_language is None


class ArtifactLanguageDeclaration(ArtifactLanguageModel):
    schema_version: Literal[1] = 1
    contract_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    requested_output_language: str
    resolution: ArtifactLanguageResolution
    artifact_language: str
    declaration_state: ArtifactLanguageDeclarationState

    @field_validator("requested_output_language")
    @classmethod
    def normalize_request(cls, value: str) -> str:
        return normalize_output_language(value)

    @field_validator("artifact_language")
    @classmethod
    def normalize_artifact_language(cls, value: str) -> str:
        return normalize_concrete_language(value)

    @model_validator(mode="after")
    def concrete_language(self) -> ArtifactLanguageDeclaration:
        if self.resolution == "explicit" and (
            self.requested_output_language == "source"
            or self.artifact_language.casefold() != self.requested_output_language.casefold()
        ):
            raise ValueError("explicit artifact-language declaration is inconsistent")
        if self.resolution != "explicit" and self.requested_output_language != "source":
            raise ValueError("source-derived declarations must retain the `source` request")
        if self.declaration_state == "resolved" and self.resolution not in {
            "explicit",
            "source-single",
        }:
            raise ValueError("only deterministic resolutions can be marked resolved")
        if self.declaration_state == "agent-declared" and self.resolution not in {
            "source-mixed",
            "source-unknown",
        }:
            raise ValueError("agent declarations require mixed or unknown source language")
        return self


def artifact_language_contract_digest(contract: ArtifactLanguageContract) -> str:
    return stable_hash(contract.model_dump(mode="json"), length=64)


def artifact_language_declaration_digest(declaration: ArtifactLanguageDeclaration) -> str:
    return stable_hash(declaration.model_dump(mode="json"), length=64)


def _normalize_observed_language(value: str | None) -> str | None:
    if value is None:
        return None
    if value.strip().casefold() in _UNKNOWN_LANGUAGE_VALUES | _MIXED_LANGUAGE_VALUES:
        return None
    try:
        normalized = normalize_concrete_language(value)
    except ValueError:
        return None
    return normalized


def _source_language_state(workspace: Workspace) -> tuple[list[str], bool, bool]:
    candidates: set[str] = set()
    any_unknown = False
    any_mixed = False
    sources = workspace.list_sources()
    if not sources:
        return [], True, False
    for source in sources:
        transcripts = workspace.transcripts(source.id, limit=1_000_000)
        raw_languages: list[str | None]
        if transcripts:
            raw_languages = [segment.language for segment in transcripts]
        else:
            raw_languages = [source.language]
        source_candidates: set[str] = set()
        source_has_mixed_evidence = False
        for raw in raw_languages:
            normalized = _normalize_observed_language(raw)
            if normalized is None:
                if raw is not None and raw.strip().casefold() in _MIXED_LANGUAGE_VALUES:
                    any_mixed = True
                    source_has_mixed_evidence = True
                else:
                    any_unknown = True
                continue
            source_candidates.add(normalized)
        if not source_candidates:
            if not source_has_mixed_evidence:
                any_unknown = True
            continue
        if len(source_candidates) > 1:
            any_mixed = True
        candidates.update(source_candidates)
    return sorted(candidates, key=str.casefold), any_unknown, any_mixed


def resolve_artifact_language_contract(
    workspace: Workspace,
    requested_output_language: str,
) -> ArtifactLanguageContract:
    requested = normalize_output_language(requested_output_language)
    if requested != "source":
        return ArtifactLanguageContract(
            requested_output_language=requested,
            resolution="explicit",
            fixed_artifact_language=requested,
        )
    candidates, any_unknown, any_mixed = _source_language_state(workspace)
    if not any_unknown and not any_mixed and len(candidates) == 1:
        return ArtifactLanguageContract(
            requested_output_language="source",
            resolution="source-single",
            fixed_artifact_language=candidates[0],
            source_languages=candidates,
        )
    return ArtifactLanguageContract(
        requested_output_language="source",
        resolution="source-unknown" if any_unknown else "source-mixed",
        source_languages=candidates,
    )


def artifact_language_contract_path(workspace: Workspace) -> Path:
    return workspace.analysis_dir / _CONTRACT_FILENAME


def persist_artifact_language_contract(
    workspace: Workspace,
    contract: ArtifactLanguageContract,
) -> str:
    path = artifact_language_contract_path(workspace)
    if path.exists():
        existing, digest = load_artifact_language_contract(workspace)
        if existing != contract:
            raise ProcessingError("Workspace artifact-language contract is immutable")
        return digest
    atomic_write_json(path, contract)
    return artifact_language_contract_digest(contract)


def load_artifact_language_contract(
    workspace: Workspace,
) -> tuple[ArtifactLanguageContract, str]:
    path = artifact_language_contract_path(workspace)
    try:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 64 * 1024:
            raise ProcessingError("Workspace artifact-language contract is missing or unsafe")
        contract = ArtifactLanguageContract.model_validate_json(path.read_text(encoding="utf-8"))
        return contract, artifact_language_contract_digest(contract)
    except (OSError, UnicodeDecodeError, ValidationError, json.JSONDecodeError) as exc:
        if isinstance(exc, ProcessingError):
            raise
        raise ProcessingError(f"Invalid workspace artifact-language contract: {exc}") from exc


def ensure_artifact_language_contract(
    workspace: Workspace,
    requested_output_language: str | None = None,
    *,
    verify_source_resolution: bool = False,
) -> tuple[ArtifactLanguageContract, str]:
    path = artifact_language_contract_path(workspace)
    if path.exists():
        contract, digest = load_artifact_language_contract(workspace)
        if requested_output_language is not None and (
            contract.requested_output_language.casefold()
            != normalize_output_language(requested_output_language).casefold()
        ):
            raise ProcessingError(
                "Requested output language differs from the persisted workspace contract"
            )
        if verify_source_resolution and contract.requested_output_language == "source":
            current = resolve_artifact_language_contract(
                workspace,
                contract.requested_output_language,
            )
            if current != contract:
                raise ProcessingError(
                    "Source-language evidence differs from the persisted artifact-language "
                    "contract; use a new workspace"
                )
        return contract, digest
    requested = normalize_output_language(
        requested_output_language or workspace.load_manifest().output_language
    )
    legacy_language = _legacy_course_artifact_language(workspace)
    if legacy_language is not None:
        if requested not in {"source", legacy_language} and (
            requested.casefold() != legacy_language.casefold()
        ):
            raise ProcessingError(
                "Requested output language conflicts with the completed legacy course"
            )
        contract = ArtifactLanguageContract(
            requested_output_language=legacy_language,
            resolution="explicit",
            fixed_artifact_language=legacy_language,
        )
    else:
        contract = resolve_artifact_language_contract(workspace, requested)
    return contract, persist_artifact_language_contract(workspace, contract)


def declare_artifact_language(
    contract: ArtifactLanguageContract,
    contract_digest: str,
    artifact_language: str,
    *,
    legacy: bool = False,
) -> ArtifactLanguageDeclaration:
    try:
        declared = normalize_concrete_language(artifact_language)
    except ValueError as exc:
        raise ProcessingError(str(exc)) from exc
    if contract.fixed_artifact_language is not None:
        if declared.casefold() != contract.fixed_artifact_language.casefold():
            raise ProcessingError(
                "Declared artifact language conflicts with the fixed output-language contract"
            )
        canonical = contract.fixed_artifact_language
        state: ArtifactLanguageDeclarationState = "legacy-agent-declared" if legacy else "resolved"
    else:
        if contract.resolution == "source-mixed" and contract.source_languages:
            candidates = {item.casefold(): item for item in contract.source_languages}
            if declared.casefold() not in candidates:
                raise ProcessingError(
                    "Mixed-source artifact language must be one of the observed source "
                    f"languages: {', '.join(contract.source_languages)}"
                )
            canonical = candidates[declared.casefold()]
        else:
            canonical = declared
        state = "legacy-agent-declared" if legacy else "agent-declared"
    return ArtifactLanguageDeclaration(
        contract_digest=contract_digest,
        requested_output_language=contract.requested_output_language,
        resolution=contract.resolution,
        artifact_language=canonical,
        declaration_state=state,
    )


def load_artifact_language_declaration(
    workspace: Workspace,
    *,
    expected_contract_digest: str | None = None,
) -> tuple[ArtifactLanguageDeclaration, str]:
    record = workspace.canonical_record("artifact-language-declaration")
    if record is None:
        raise ProcessingError("A canonical artifact-language declaration is required")
    path = workspace.root / record.path
    try:
        if (
            path.is_symlink()
            or not path.is_file()
            or path.stat().st_size > 64 * 1024
            or hash_file(path) != record.digest
        ):
            raise ProcessingError("Canonical artifact-language declaration failed its digest check")
        declaration = ArtifactLanguageDeclaration.model_validate_json(
            path.read_text(encoding="utf-8")
        )
    except (OSError, UnicodeDecodeError, ValidationError) as exc:
        if isinstance(exc, ProcessingError):
            raise
        raise ProcessingError(f"Invalid artifact-language declaration: {exc}") from exc
    if (
        expected_contract_digest is not None
        and declaration.contract_digest != expected_contract_digest
    ):
        raise ProcessingError("Artifact-language declaration targets a different contract")
    return declaration, artifact_language_declaration_digest(declaration)


@dataclass(frozen=True)
class CanonicalArtifactLanguageState:
    contract: ArtifactLanguageContract
    contract_digest: str
    declaration: ArtifactLanguageDeclaration
    declaration_digest: str
    contract_path: Path | None
    declaration_path: Path | None
    legacy: bool = False


def _verified_course_language(workspace: Workspace) -> str | None:
    record = workspace.canonical_record("course")
    if record is None:
        return None
    path = workspace.root / record.path
    try:
        if path.is_symlink() or not path.is_file() or hash_file(path) != record.digest:
            raise ProcessingError("Canonical course failed its artifact-language digest check")
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict) or not isinstance(raw.get("artifact_language"), str):
            raise ProcessingError("Canonical course has no concrete artifact language")
        language = normalize_concrete_language(str(raw["artifact_language"]))
        return language
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        if isinstance(exc, ProcessingError):
            raise
        raise ProcessingError(f"Invalid canonical course artifact language: {exc}") from exc


def _legacy_course_artifact_language(workspace: Workspace) -> str | None:
    course_record = workspace.canonical_record("course")
    if course_record is None:
        return None
    author_task = workspace.get_work_item(course_record.producer_task_id)
    contract_digest = author_task.scope.get("artifact_language_contract_digest")
    declaration_digest = author_task.scope.get("artifact_language_declaration_digest")
    if (contract_digest is None) != (declaration_digest is None):
        raise ProcessingError("Canonical Author has a partial artifact-language binding")
    if contract_digest is not None:
        return None
    language = _verified_course_language(workspace)
    if language is None:
        raise ProcessingError("Completed legacy Author has no concrete artifact language")
    return language


def _legacy_explicit_language_state(
    workspace: Workspace,
    language: str,
    *,
    contract_path: Path | None,
) -> CanonicalArtifactLanguageState:
    contract = ArtifactLanguageContract(
        requested_output_language=language,
        resolution="explicit",
        fixed_artifact_language=language,
    )
    contract_digest = artifact_language_contract_digest(contract)
    if contract_path is not None:
        persisted, persisted_digest = load_artifact_language_contract(workspace)
        if persisted != contract:
            raise ProcessingError(
                "Persisted artifact-language contract conflicts with the completed legacy course"
            )
        contract_digest = persisted_digest
    declaration = declare_artifact_language(
        contract,
        contract_digest,
        language,
        legacy=True,
    )
    return CanonicalArtifactLanguageState(
        contract=contract,
        contract_digest=contract_digest,
        declaration=declaration,
        declaration_digest=artifact_language_declaration_digest(declaration),
        contract_path=(
            (contract_path or artifact_language_contract_path(workspace)).relative_to(
                workspace.root
            )
        ),
        declaration_path=None,
        legacy=True,
    )


def canonical_artifact_language_state(
    workspace: Workspace,
    *,
    expected_author_task_id: str | None = None,
) -> CanonicalArtifactLanguageState:
    """Reconstruct and verify the language state; preserve completed legacy workspaces."""

    contract_path = artifact_language_contract_path(workspace)
    declaration_record = workspace.canonical_record("artifact-language-declaration")
    if not contract_path.exists() and declaration_record is None:
        course_language = _legacy_course_artifact_language(workspace)
        if course_language is None:
            raise ProcessingError("Canonical artifact-language state is missing")
        return _legacy_explicit_language_state(
            workspace,
            course_language,
            contract_path=None,
        )
    if contract_path.exists() and declaration_record is None:
        course_language = _legacy_course_artifact_language(workspace)
        if course_language is not None:
            return _legacy_explicit_language_state(
                workspace,
                course_language,
                contract_path=contract_path,
            )
        raise ProcessingError("Canonical artifact-language state is incomplete")
    if not contract_path.exists() or declaration_record is None:
        raise ProcessingError("Canonical artifact-language state is incomplete")
    contract, contract_digest = load_artifact_language_contract(workspace)
    declaration, declaration_digest = load_artifact_language_declaration(
        workspace,
        expected_contract_digest=contract_digest,
    )
    expected_declaration = declare_artifact_language(
        contract,
        contract_digest,
        declaration.artifact_language,
        legacy=declaration.declaration_state == "legacy-agent-declared",
    )
    if declaration != expected_declaration:
        raise ProcessingError("Canonical artifact-language declaration violates its contract")
    course_language = _verified_course_language(workspace)
    if (
        course_language is not None
        and course_language.casefold() != declaration.artifact_language.casefold()
    ):
        raise ProcessingError("Canonical course changed the declared artifact language")
    if expected_author_task_id is not None:
        author_task = workspace.get_work_item(expected_author_task_id)
        if (
            author_task.scope.get("artifact_language_contract_digest") != contract_digest
            or author_task.scope.get("artifact_language_declaration_digest") != declaration_digest
        ):
            raise ProcessingError("Canonical Author is not bound to artifact-language state")
    return CanonicalArtifactLanguageState(
        contract=contract,
        contract_digest=contract_digest,
        declaration=declaration,
        declaration_digest=declaration_digest,
        contract_path=contract_path.relative_to(workspace.root),
        declaration_path=declaration_record.path,
    )
