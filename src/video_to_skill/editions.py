"""Immutable same-evidence publication editions and their Analyze lineage."""

from __future__ import annotations

import json
import os
import re
import secrets
import stat
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

from video_to_skill.errors import ProcessingError
from video_to_skill.models import ObservationProducer
from video_to_skill.orchestration import CurriculumPlan, CurriculumSelection
from video_to_skill.utils import hash_file, stable_hash
from video_to_skill.work import AnalysisRun, WorkItem, WorkRole, WorkState
from video_to_skill.workspace import Workspace

ANALYZE_LINEAGE_KINDS = (
    "semantic-map",
    "semantic-relations",
    "capability-evidence",
    "semantic-coverage",
    "semantic-conflicts",
    "visual-asset-candidates",
)
LEGACY_EDITION_NAME = "legacy-default"
_EDITION_NAME_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_EDITION_ID_RE = re.compile(r"^edition-[a-f0-9]{20}$")
_IDENTITY_FAMILY_RE = re.compile(r"^identity-[a-f0-9]{20}$")
_MAX_STATE_BYTES = 512 * 1024


class EditionModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class EditionAnalysisLineage(EditionModel):
    analyze_task_id: str
    analysis_snapshot_digest: str
    source_snapshot_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    analysis_depth_contract_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    canonical_analyze_digests: dict[str, str]
    lineage_digest: str = Field(pattern=r"^[a-f0-9]{64}$")


class EditionIdentityBaseline(EditionModel):
    schema_version: Literal[1] = 1
    family_id: str = Field(pattern=r"^identity-[a-f0-9]{20}$")
    parent_family_id: str | None = Field(
        default=None,
        pattern=r"^identity-[a-f0-9]{20}$",
    )
    analysis_lineage_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    established: bool
    artifact_ids: list[str]
    claim_ids: list[str]
    artifact_semantic_units: dict[str, list[str]]
    claim_grounding_digests: dict[str, str]

    @model_validator(mode="after")
    def coherent_identity(self) -> EditionIdentityBaseline:
        if self.artifact_ids != sorted(set(self.artifact_ids)):
            raise ValueError("identity artifact ids must be sorted and unique")
        if self.claim_ids != sorted(set(self.claim_ids)):
            raise ValueError("identity claim ids must be sorted and unique")
        if set(self.artifact_semantic_units) != set(self.artifact_ids):
            raise ValueError("identity artifact bindings must match artifact ids")
        if set(self.claim_grounding_digests) != set(self.claim_ids):
            raise ValueError("identity claim grounding must match claim ids")
        if not self.established and any(
            (
                self.artifact_ids,
                self.claim_ids,
                self.artifact_semantic_units,
                self.claim_grounding_digests,
            )
        ):
            raise ValueError("pending identity baseline cannot contain logical identities")
        if self.established and not self.claim_ids:
            raise ValueError("established identity baseline requires at least one claim")
        return self


class EditionConfiguration(EditionModel):
    schema_version: Literal[1] = 1
    edition_id: str = Field(pattern=r"^edition-[a-f0-9]{20}$")
    edition_name: str
    config_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    analysis_lineage: EditionAnalysisLineage
    requested_output_language: str
    curriculum_mode: Literal["reuse", "plan"]
    curriculum_source_edition: str | None = None
    source_curriculum_plan_digest: str | None = None
    requested_curriculum_path_id: str | None = None
    skill_name: str | None = None
    host: Literal["claude", "codex"]
    output: Path
    output_is_default: bool
    project: bool
    project_root: Path
    skill_root: Path | None = None
    run_official_validation: bool
    identity_baseline: EditionIdentityBaseline
    identity_drift_justification: str | None = None

    @field_validator("edition_name")
    @classmethod
    def valid_edition_name(cls, value: str) -> str:
        if len(value) > 64 or not _EDITION_NAME_RE.fullmatch(value):
            raise ValueError("edition name must be a lowercase hyphenated name")
        return value


class EditionState(EditionModel):
    configuration: EditionConfiguration
    status: Literal["created", "in-progress", "complete"] = "created"
    resolved_artifact_language: str | None = None
    selected_curriculum_path_id: str | None = None
    selected_curriculum_digest: str | None = None
    completion: dict[str, Any] | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


def edition_id_for(workspace: Workspace, edition_name: str) -> str:
    if len(edition_name) > 64 or not _EDITION_NAME_RE.fullmatch(edition_name):
        raise ProcessingError("Edition name must be a lowercase hyphenated name")
    return "edition-" + stable_hash(
        {"job_id": workspace.load_manifest().job_id, "edition_name": edition_name},
        length=20,
    )


def edition_state_path(workspace: Workspace, edition_id: str) -> Path:
    return workspace.editions_dir / edition_id / "edition.json"


def _configuration_payload(configuration: EditionConfiguration) -> dict[str, Any]:
    return configuration.model_dump(mode="json", exclude={"config_digest"})


def _directory_flags() -> int:
    return os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)


@contextmanager
def _open_child_directory(
    parent_fd: int,
    name: str,
    *,
    create: bool,
) -> Iterator[int]:
    if create:
        with suppress(FileExistsError):
            os.mkdir(name, mode=0o700, dir_fd=parent_fd)
    descriptor = os.open(name, _directory_flags(), dir_fd=parent_fd)
    try:
        yield descriptor
    finally:
        os.close(descriptor)


@contextmanager
def _open_edition_directory(
    workspace: Workspace,
    edition_id: str,
    *,
    create: bool,
) -> Iterator[int]:
    if not _EDITION_ID_RE.fullmatch(edition_id):
        raise ProcessingError("Edition id is invalid")
    try:
        root_fd = os.open(workspace.root, _directory_flags())
        try:
            with (
                _open_child_directory(root_fd, "editions", create=create) as editions_fd,
                _open_child_directory(
                    editions_fd,
                    edition_id,
                    create=create,
                ) as edition_fd,
            ):
                yield edition_fd
        finally:
            os.close(root_fd)
    except (FileNotFoundError, NotADirectoryError) as exc:
        raise ProcessingError(f"Unknown workspace edition id: {edition_id}") from exc
    except OSError as exc:
        raise ProcessingError(f"Edition filesystem namespace is unsafe: {edition_id}") from exc


def _json_bytes(value: object) -> bytes:
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")
    return (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _read_bounded_file(directory_fd: int, name: str, *, missing: str) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(name, flags, dir_fd=directory_fd)
    except FileNotFoundError as exc:
        raise ProcessingError(missing) from exc
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > _MAX_STATE_BYTES:
            raise ProcessingError(missing)
        with os.fdopen(descriptor, "rb", closefd=False) as handle:
            return handle.read(_MAX_STATE_BYTES + 1)
    finally:
        os.close(descriptor)


def _write_temporary(directory_fd: int, name: str, payload: bytes) -> str:
    temporary_name = f".{name}.{secrets.token_hex(12)}.tmp"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(temporary_name, flags, 0o600, dir_fd=directory_fd)
    try:
        with os.fdopen(descriptor, "wb", closefd=False) as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
    except Exception:
        os.unlink(temporary_name, dir_fd=directory_fd)
        raise
    finally:
        os.close(descriptor)
    return temporary_name


def _replace_json_at(directory_fd: int, name: str, value: object) -> None:
    temporary_name = _write_temporary(directory_fd, name, _json_bytes(value))
    try:
        os.replace(
            temporary_name,
            name,
            src_dir_fd=directory_fd,
            dst_dir_fd=directory_fd,
        )
        os.fsync(directory_fd)
    finally:
        with suppress(FileNotFoundError):
            os.unlink(temporary_name, dir_fd=directory_fd)


def _cas_json_at(directory_fd: int, name: str, value: object) -> bool:
    """Install one immutable JSON file without a last-writer-wins replacement."""

    temporary_name = _write_temporary(directory_fd, name, _json_bytes(value))
    try:
        try:
            os.link(
                temporary_name,
                name,
                src_dir_fd=directory_fd,
                dst_dir_fd=directory_fd,
                follow_symlinks=False,
            )
        except FileExistsError:
            return False
        os.fsync(directory_fd)
        return True
    finally:
        os.unlink(temporary_name, dir_fd=directory_fd)


def _validated_state(raw: bytes, *, edition_id: str) -> EditionState:
    try:
        state = EditionState.model_validate_json(raw)
    except (UnicodeDecodeError, ValidationError) as exc:
        raise ProcessingError(f"Invalid workspace edition {edition_id}: {exc}") from exc
    if state.configuration.edition_id != edition_id:
        raise ProcessingError("Edition state id does not match its directory")
    expected_digest = stable_hash(_configuration_payload(state.configuration), length=64)
    if state.configuration.config_digest != expected_digest:
        raise ProcessingError("Edition configuration failed its digest check")
    return state


def edition_state_exists(workspace: Workspace, edition_id: str) -> bool:
    try:
        with _open_edition_directory(workspace, edition_id, create=False) as directory_fd:
            try:
                metadata = os.stat("edition.json", dir_fd=directory_fd, follow_symlinks=False)
            except FileNotFoundError:
                return False
            if not stat.S_ISREG(metadata.st_mode):
                raise ProcessingError("Edition state path is unsafe")
            return True
    except ProcessingError as exc:
        if str(exc).startswith("Unknown workspace edition id:"):
            return False
        raise


def load_edition_state(workspace: Workspace, edition_name: str) -> EditionState:
    edition_id = edition_id_for(workspace, edition_name)
    try:
        return load_edition_state_by_id(workspace, edition_id)
    except ProcessingError as exc:
        if str(exc).startswith("Unknown workspace edition id:"):
            raise ProcessingError(f"Unknown workspace edition: {edition_name}") from exc
        raise


def load_edition_state_by_id(workspace: Workspace, edition_id: str) -> EditionState:
    try:
        with _open_edition_directory(workspace, edition_id, create=False) as directory_fd:
            raw = _read_bounded_file(
                directory_fd,
                "edition.json",
                missing=f"Unknown workspace edition id: {edition_id}",
            )
    except OSError as exc:
        raise ProcessingError(f"Invalid workspace edition {edition_id}: {exc}") from exc
    return _validated_state(raw, edition_id=edition_id)


def save_edition_state(workspace: Workspace, state: EditionState) -> None:
    existing = load_edition_state_by_id(workspace, state.configuration.edition_id)
    if existing.configuration != state.configuration:
        raise ProcessingError("Edition immutable configuration cannot be changed")
    state.updated_at = datetime.now(UTC)
    with _open_edition_directory(
        workspace,
        state.configuration.edition_id,
        create=False,
    ) as directory_fd:
        _replace_json_at(directory_fd, "edition.json", state)


def _verified_canonical_json(workspace: Workspace, kind: str) -> Any | None:
    record = workspace.canonical_record(kind)
    if record is None:
        return None
    path = workspace.root / record.path
    try:
        if path.is_symlink() or not path.is_file() or hash_file(path) != record.digest:
            raise ProcessingError(f"Canonical {kind} failed its digest check")
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        if isinstance(exc, ProcessingError):
            raise
        raise ProcessingError(f"Invalid canonical {kind}: {exc}") from exc


def current_analysis_lineage(workspace: Workspace) -> EditionAnalysisLineage:
    if workspace.edition_id is not None:
        workspace = Workspace.open(workspace.root)
    canonical_digests: dict[str, str] = {}
    producer_task_id: str | None = None
    for kind in ANALYZE_LINEAGE_KINDS:
        record = workspace.canonical_record(kind)
        if record is None:
            raise ProcessingError("Edition creation requires completed integrated Analyze state")
        path = workspace.root / record.path
        if path.is_symlink() or not path.is_file() or hash_file(path) != record.digest:
            raise ProcessingError(f"Canonical Analyze record failed its digest check: {kind}")
        if producer_task_id is None:
            producer_task_id = record.producer_task_id
        elif producer_task_id != record.producer_task_id:
            raise ProcessingError("Canonical Analyze heads do not share one integrated producer")
        canonical_digests[kind] = record.digest
    assert producer_task_id is not None
    producer = workspace.get_work_item(producer_task_id)
    if (
        producer.role != WorkRole.ANALYZE
        or producer.state != WorkState.COMPLETE
        or producer.scope.get("integrated") is not True
    ):
        raise ProcessingError("Canonical Analyze producer is not completed integration work")
    current_snapshot = workspace.workspace_snapshot_digest()
    if producer.snapshot_digest != current_snapshot:
        raise ProcessingError(
            "Canonical Analyze state is stale for the current source/depth snapshot"
        )
    analysis_depth = workspace.analysis_depth_contract()
    if analysis_depth is None:
        raise ProcessingError("Edition creation requires an analysis-depth contract")
    depth_digest = stable_hash(analysis_depth.model_dump(mode="json"), length=64)
    packet_path = workspace.root / producer.packet_path
    try:
        packet = json.loads(packet_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProcessingError(f"Could not verify canonical Analyze packet: {exc}") from exc
    if (
        packet_path.is_symlink()
        or not isinstance(packet, dict)
        or stable_hash(packet, length=64) != producer.packet_digest
        or not isinstance(packet.get("payload"), dict)
        or packet["payload"].get("analysis_depth") != analysis_depth.model_dump(mode="json")
    ):
        raise ProcessingError("Canonical Analyze task is not bound to the persisted depth contract")
    source_snapshot_digest = stable_hash(
        [
            source.model_dump(mode="json", exclude_none=True)
            for source in workspace.list_sources(include_inactive=True)
        ],
        length=64,
    )
    payload: dict[str, Any] = {
        "analyze_task_id": producer.id,
        "analysis_snapshot_digest": current_snapshot,
        "source_snapshot_digest": source_snapshot_digest,
        "analysis_depth_contract_digest": depth_digest,
        "canonical_analyze_digests": canonical_digests,
    }
    return EditionAnalysisLineage(
        **payload,
        lineage_digest=stable_hash(payload, length=64),
    )


def assert_edition_analysis_lineage(workspace: Workspace) -> EditionState:
    if workspace.edition_id is None:
        raise ProcessingError("Edition lineage validation requires an edition workspace view")
    state = load_edition_state_by_id(workspace, workspace.edition_id)
    if (
        current_analysis_lineage(Workspace.open(workspace.root))
        != state.configuration.analysis_lineage
    ):
        raise ProcessingError(
            "Edition is pinned to different Analyze state; create a new edition after refresh"
        )
    return state


def curriculum_checkpoint(
    workspace: Workspace,
) -> tuple[CurriculumPlan, CurriculumSelection, str] | None:
    raw_plan = _verified_canonical_json(workspace, "curriculum-options")
    raw_selection = _verified_canonical_json(workspace, "selected-curriculum")
    if raw_plan is None and raw_selection is None:
        return None
    if raw_plan is None or raw_selection is None:
        raise ProcessingError("Curriculum checkpoint is incomplete")
    try:
        plan = CurriculumPlan.model_validate(raw_plan)
        selection = CurriculumSelection.model_validate(raw_selection)
    except ValidationError as exc:
        raise ProcessingError(f"Invalid source curriculum checkpoint: {exc}") from exc
    record = workspace.canonical_record("curriculum-options")
    assert record is not None
    if selection.curriculum_plan_digest != record.digest:
        raise ProcessingError("Source curriculum selection targets different options")
    return plan, selection, record.digest


def materialize_reused_curriculum(
    workspace: Workspace,
    run: AnalysisRun,
    *,
    analyze_task: WorkItem,
    source_workspace: Workspace,
    selected_path_id: str,
) -> WorkItem:
    """Bind an existing path without dispatching a second curriculum-planning pass."""

    from video_to_skill.artifact_language import (
        canonical_artifact_language_state,
        declare_artifact_language,
        load_artifact_language_contract,
    )

    state = assert_edition_analysis_lineage(workspace)
    checkpoint = curriculum_checkpoint(source_workspace)
    if checkpoint is None:
        raise ProcessingError("Requested curriculum reuse has no source checkpoint")
    plan, _source_selection, source_plan_digest = checkpoint
    if source_plan_digest != state.configuration.source_curriculum_plan_digest:
        raise ProcessingError("Source curriculum plan changed after edition creation")
    if selected_path_id not in {path.id for path in plan.paths}:
        raise ProcessingError(f"Unknown source curriculum path: {selected_path_id}")
    language_contract, contract_digest = load_artifact_language_contract(workspace)
    if language_contract.fixed_artifact_language is not None:
        artifact_language = language_contract.fixed_artifact_language
    else:
        try:
            artifact_language = canonical_artifact_language_state(
                source_workspace
            ).declaration.artifact_language
        except ProcessingError as exc:
            raise ProcessingError(
                "Reusing a curriculum with unresolved `source` language requires a source "
                "edition language declaration or a new curriculum plan"
            ) from exc
    declaration = declare_artifact_language(
        language_contract,
        contract_digest,
        artifact_language,
    )
    task = workspace.ensure_work_item(
        run_id=run.id,
        role=WorkRole.AUTHOR,
        scope={
            "kind": "curriculum-reuse",
            "analyze_task_id": analyze_task.id,
            "source_edition": state.configuration.curriculum_source_edition,
            "source_curriculum_plan_digest": source_plan_digest,
            "selected_path_id": selected_path_id,
            "artifact_language_contract_digest": contract_digest,
            "canonical_record_digests": (
                state.configuration.analysis_lineage.canonical_analyze_digests
            ),
        },
        persona_hint="Deterministic existing-curriculum edition binding.",
        packet={
            "instructions": (
                "Reuse the immutable curriculum plan and selected path. No agent work is "
                "required; this packet records the deterministic edition binding."
            ),
            "source_curriculum_plan_digest": source_plan_digest,
            "selected_path_id": selected_path_id,
            "artifact_language_contract_digest": contract_digest,
            "analysis_lineage_digest": state.configuration.analysis_lineage.lineage_digest,
        },
        result_schema={"type": "object", "additionalProperties": False},
        dependencies=[analyze_task.id],
        snapshot_digest=run.snapshot_digest,
    )
    if task.state == WorkState.COMPLETE:
        return task
    if task.state != WorkState.PENDING:
        raise ProcessingError("Deterministic curriculum binding is unexpectedly leased")
    lease = workspace.lease_work_item(task.id, owner="video-to-skill-edition-engine")
    output = lease.output_directory
    plan_path = output / "curriculum-options.json"
    selection_path = output / "selected-curriculum.json"
    language_path = output / "artifact-language.json"
    result_path = output / "result.json"
    workspace.write_json(plan_path, plan)
    plan_snapshot = workspace.canonical_output_file_snapshot(
        task.id,
        "curriculum-options",
        "default",
        plan_path,
    )
    workspace.write_json(
        selection_path,
        CurriculumSelection(
            curriculum_plan_digest=plan_snapshot.file.digest,
            selected_path_id=selected_path_id,
            source="edition",
        ),
    )
    workspace.write_json(language_path, declaration)
    workspace.write_json(result_path, {})
    result_snapshot = workspace.task_output_file_snapshot(task.id, result_path)
    accepted, _records = workspace.accept_work_result(
        task_id=task.id,
        lease_token=lease.token,
        result_path=result_path,
        result_snapshot=result_snapshot,
        producer=ObservationProducer(
            name="video-to-skill deterministic edition binder",
            version="1",
            run_id=run.id,
        ).model_dump(mode="json"),
        canonical_outputs=[
            plan_snapshot,
            workspace.canonical_output_file_snapshot(
                task.id,
                "selected-curriculum",
                "default",
                selection_path,
            ),
            workspace.canonical_output_file_snapshot(
                task.id,
                "artifact-language-declaration",
                "default",
                language_path,
            ),
        ],
    )
    return accepted


@contextmanager
def _open_identity_directory(workspace: Workspace, *, create: bool) -> Iterator[int]:
    try:
        root_fd = os.open(workspace.root, _directory_flags())
        try:
            with (
                _open_child_directory(root_fd, "analysis", create=create) as analysis_fd,
                _open_child_directory(
                    analysis_fd,
                    "identity-baselines",
                    create=create,
                ) as identity_fd,
            ):
                yield identity_fd
        finally:
            os.close(root_fd)
    except FileNotFoundError as exc:
        raise ProcessingError("Logical identity family is not established") from exc
    except OSError as exc:
        raise ProcessingError("Logical identity baseline namespace is unsafe") from exc


def _identity_filename(family_id: str) -> str:
    if not _IDENTITY_FAMILY_RE.fullmatch(family_id):
        raise ProcessingError("Logical identity family id is invalid")
    return f"{family_id}.json"


def _load_identity_family(
    workspace: Workspace,
    family_id: str,
) -> EditionIdentityBaseline | None:
    raw = workspace.identity_baseline_payload(family_id)
    if raw is None:
        return None
    try:
        baseline = EditionIdentityBaseline.model_validate(raw)
    except ValidationError as exc:
        raise ProcessingError(f"Invalid logical identity baseline: {exc}") from exc
    if baseline.family_id != family_id or not baseline.established:
        raise ProcessingError("Logical identity baseline has invalid family state")
    return baseline


def _persist_identity_family(
    workspace: Workspace,
    baseline: EditionIdentityBaseline,
    *,
    producer_task_id: str,
) -> EditionIdentityBaseline:
    if not baseline.established:
        raise ProcessingError("Cannot persist an unestablished identity family")
    stored = workspace.ensure_identity_baseline(
        payload=baseline.model_dump(mode="json"),
        producer_task_id=producer_task_id,
    )
    try:
        return EditionIdentityBaseline.model_validate(stored)
    except ValidationError as exc:
        raise ProcessingError(f"Invalid logical identity baseline: {exc}") from exc


def _identity_from_raw_records(
    *,
    family_id: str,
    parent_family_id: str | None,
    analysis_lineage_digest: str,
    artifacts: list[Any],
    claims: list[Any],
) -> EditionIdentityBaseline:
    artifact_semantic_units: dict[str, list[str]] = {}
    for value in artifacts:
        if not isinstance(value, dict) or not isinstance(value.get("id"), str):
            raise ProcessingError("Canonical artifact identity baseline is malformed")
        unit_ids = value.get("semantic_unit_ids")
        if not isinstance(unit_ids, list) or not all(isinstance(item, str) for item in unit_ids):
            raise ProcessingError("Canonical artifact semantic identity is malformed")
        artifact_semantic_units[str(value["id"])] = list(unit_ids)
    claim_grounding_digests: dict[str, str] = {}
    for value in claims:
        if not isinstance(value, dict) or not isinstance(value.get("id"), str):
            raise ProcessingError("Canonical claim identity baseline is malformed")
        grounding = {
            "semantic_unit_ids": value.get("semantic_unit_ids"),
            "evidence": value.get("evidence"),
        }
        claim_grounding_digests[str(value["id"])] = stable_hash(grounding, length=64)
    return EditionIdentityBaseline(
        family_id=family_id,
        parent_family_id=parent_family_id,
        analysis_lineage_digest=analysis_lineage_digest,
        established=True,
        artifact_ids=sorted(artifact_semantic_units),
        claim_ids=sorted(claim_grounding_digests),
        artifact_semantic_units=artifact_semantic_units,
        claim_grounding_digests=claim_grounding_digests,
    )


def _pending_identity_baseline(
    *,
    family_id: str,
    parent_family_id: str | None,
    analysis_lineage_digest: str,
) -> EditionIdentityBaseline:
    return EditionIdentityBaseline(
        family_id=family_id,
        parent_family_id=parent_family_id,
        analysis_lineage_digest=analysis_lineage_digest,
        established=False,
        artifact_ids=[],
        claim_ids=[],
        artifact_semantic_units={},
        claim_grounding_digests={},
    )


def identity_baseline(workspace: Workspace) -> EditionIdentityBaseline:
    base_workspace = (
        Workspace.open(workspace.root) if workspace.edition_id is not None else workspace
    )
    lineage = current_analysis_lineage(base_workspace)
    if workspace.edition_id is None:
        family_id = "identity-" + stable_hash(
            {"analysis_lineage_digest": lineage.lineage_digest},
            length=20,
        )
        parent_family_id = None
    else:
        source_state = assert_edition_analysis_lineage(workspace)
        family_id = source_state.configuration.identity_baseline.family_id
        parent_family_id = source_state.configuration.identity_baseline.parent_family_id
    raw_artifacts = _verified_canonical_json(workspace, "artifact-plan")
    raw_claims = _verified_canonical_json(workspace, "claims")
    if (raw_artifacts is None) != (raw_claims is None):
        raise ProcessingError("Logical identity source has an incomplete Author checkpoint")
    if raw_artifacts is not None:
        if not isinstance(raw_artifacts, list) or not isinstance(raw_claims, list):
            raise ProcessingError("Logical identity source checkpoint is malformed")
        baseline = _identity_from_raw_records(
            family_id=family_id,
            parent_family_id=parent_family_id,
            analysis_lineage_digest=lineage.lineage_digest,
            artifacts=raw_artifacts,
            claims=raw_claims,
        )
        claims_record = workspace.canonical_record("claims")
        assert claims_record is not None
        established = _persist_identity_family(
            workspace,
            baseline,
            producer_task_id=claims_record.producer_task_id,
        )
        if established != baseline:
            raise ProcessingError(
                "Source Author checkpoint conflicts with its durable logical identity family"
            )
        return baseline
    stored_baseline = _load_identity_family(base_workspace, family_id)
    if stored_baseline is not None:
        return stored_baseline
    return _pending_identity_baseline(
        family_id=family_id,
        parent_family_id=parent_family_id,
        analysis_lineage_digest=lineage.lineage_digest,
    )


def create_edition_state(
    workspace: Workspace,
    *,
    edition_name: str,
    requested_output_language: str,
    curriculum_mode: Literal["reuse", "plan"],
    curriculum_source_edition: str | None,
    source_curriculum_plan_digest: str | None,
    requested_curriculum_path_id: str | None,
    skill_name: str | None,
    host: Literal["claude", "codex"],
    output: Path,
    output_is_default: bool,
    project: bool,
    project_root: Path,
    skill_root: Path | None,
    run_official_validation: bool,
    baseline: EditionIdentityBaseline,
    identity_drift_justification: str | None,
) -> EditionState:
    edition_id = edition_id_for(workspace, edition_name)
    analysis_lineage = current_analysis_lineage(workspace)
    if baseline.analysis_lineage_digest != analysis_lineage.lineage_digest:
        raise ProcessingError("Logical identity baseline belongs to different Analyze lineage")
    compact_drift_reason = (
        " ".join(identity_drift_justification.split()) if identity_drift_justification else None
    )
    if compact_drift_reason:
        baseline = _pending_identity_baseline(
            family_id="identity-"
            + stable_hash(
                {
                    "parent_family_id": baseline.family_id,
                    "edition_id": edition_id,
                    "explicit_identity_drift": compact_drift_reason,
                },
                length=20,
            ),
            parent_family_id=baseline.family_id,
            analysis_lineage_digest=baseline.analysis_lineage_digest,
        )
    raw_configuration: dict[str, Any] = {
        "edition_id": edition_id,
        "edition_name": edition_name,
        "analysis_lineage": analysis_lineage,
        "requested_output_language": requested_output_language,
        "curriculum_mode": curriculum_mode,
        "curriculum_source_edition": curriculum_source_edition,
        "source_curriculum_plan_digest": source_curriculum_plan_digest,
        "requested_curriculum_path_id": requested_curriculum_path_id,
        "skill_name": skill_name,
        "host": host,
        "output": output.resolve(),
        "output_is_default": output_is_default,
        "project": project,
        "project_root": project_root.resolve(),
        "skill_root": skill_root.resolve() if skill_root is not None else None,
        "run_official_validation": run_official_validation,
        "identity_baseline": baseline,
        "identity_drift_justification": compact_drift_reason,
    }
    digest = stable_hash(
        EditionConfiguration(
            **raw_configuration,
            config_digest="0" * 64,
        ).model_dump(mode="json", exclude={"config_digest"}),
        length=64,
    )
    configuration = EditionConfiguration(**raw_configuration, config_digest=digest)
    state = EditionState(configuration=configuration)
    with _open_edition_directory(workspace, edition_id, create=True) as directory_fd:
        created = _cas_json_at(directory_fd, "edition.json", state)
        if created:
            return state
        existing_raw = _read_bounded_file(
            directory_fd,
            "edition.json",
            missing=f"Unknown workspace edition id: {edition_id}",
        )
    existing = _validated_state(existing_raw, edition_id=edition_id)
    if existing.configuration != configuration:
        raise ProcessingError("Edition name already exists with different immutable configuration")
    return existing


def public_edition_lineage(workspace: Workspace) -> dict[str, Any] | None:
    if workspace.edition_id is None:
        return None
    state = assert_edition_analysis_lineage(workspace)
    configuration = state.configuration
    return {
        "edition_id": configuration.edition_id,
        "edition_name": configuration.edition_name,
        "config_digest": configuration.config_digest,
        "analysis_lineage_digest": configuration.analysis_lineage.lineage_digest,
        "analyze_task_id": configuration.analysis_lineage.analyze_task_id,
        "analysis_snapshot_digest": configuration.analysis_lineage.analysis_snapshot_digest,
        "source_snapshot_digest": configuration.analysis_lineage.source_snapshot_digest,
        "analysis_depth_contract_digest": (
            configuration.analysis_lineage.analysis_depth_contract_digest
        ),
        "canonical_analyze_digests": configuration.analysis_lineage.canonical_analyze_digests,
        "curriculum_source_edition": configuration.curriculum_source_edition,
        "source_curriculum_plan_digest": configuration.source_curriculum_plan_digest,
        "requested_curriculum_path_id": configuration.requested_curriculum_path_id,
        "identity_family_id": configuration.identity_baseline.family_id,
        "identity_drift_justification": configuration.identity_drift_justification,
    }


def validate_edition_author_identity(
    workspace: Workspace,
    *,
    name: str,
    artifacts: list[Any],
    claims: list[Any],
) -> dict[str, Any] | None:
    if workspace.edition_id is None:
        return None
    state = assert_edition_analysis_lineage(workspace)
    configuration = state.configuration
    if configuration.skill_name is not None and name != configuration.skill_name:
        raise ProcessingError("Author changed the edition's requested Skill name")
    configured_baseline = configuration.identity_baseline
    candidate = _identity_from_raw_records(
        family_id=configured_baseline.family_id,
        parent_family_id=configured_baseline.parent_family_id,
        analysis_lineage_digest=configured_baseline.analysis_lineage_digest,
        artifacts=[item.model_dump(mode="json") for item in artifacts],
        claims=[item.model_dump(mode="json") for item in claims],
    )
    stored = _load_identity_family(workspace, candidate.family_id)
    expected = stored or (configured_baseline if configured_baseline.established else candidate)
    if configured_baseline.established and configured_baseline != expected:
        raise ProcessingError(
            "Edition configuration disagrees with its durable logical identity baseline"
        )
    if candidate.artifact_ids != expected.artifact_ids or candidate.claim_ids != expected.claim_ids:
        raise ProcessingError(
            "Edition changed logical artifact or claim ids outside its identity family"
        )
    if candidate.artifact_semantic_units != expected.artifact_semantic_units:
        raise ProcessingError(
            "Edition changed artifact semantic identity outside its identity family"
        )
    if candidate.claim_grounding_digests != expected.claim_grounding_digests:
        raise ProcessingError(
            "Edition changed claim evidence/timestamp identity outside its identity family"
        )
    # Persistence is deliberately deferred to Workspace.accept_work_result(), after
    # lease token, expiry, and snapshot verification inside its SQLite transaction.
    return candidate.model_dump(mode="json")


def mark_edition_progress(
    workspace: Workspace,
    *,
    status: Literal["in-progress", "complete"],
    resolved_artifact_language: str | None = None,
    selected_curriculum_path_id: str | None = None,
    selected_curriculum_digest: str | None = None,
    completion: dict[str, Any] | None = None,
) -> EditionState:
    if workspace.edition_id is None:
        raise ProcessingError("Cannot update edition status from an unscoped workspace")
    state = assert_edition_analysis_lineage(workspace)
    state.status = status
    if resolved_artifact_language is not None:
        state.resolved_artifact_language = resolved_artifact_language
    if selected_curriculum_path_id is not None:
        state.selected_curriculum_path_id = selected_curriculum_path_id
    if selected_curriculum_digest is not None:
        state.selected_curriculum_digest = selected_curriculum_digest
    if completion is not None:
        state.completion = completion
    save_edition_state(workspace, state)
    return state
