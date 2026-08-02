"""Bounded curriculum planning before full course artifact authoring."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from pydantic import ValidationError as PydanticValidationError

from video_to_skill.artifact_language import (
    ArtifactLanguageDeclaration,
    declare_artifact_language,
    ensure_artifact_language_contract,
    load_artifact_language_contract,
)
from video_to_skill.errors import ProcessingError
from video_to_skill.generation import CurriculumDesign, SemanticUnit
from video_to_skill.orchestration import (
    ArtifactDraftSpec,
    CurriculumPlan,
    CurriculumPlanResult,
    CurriculumSelection,
)
from video_to_skill.utils import hash_file, stable_hash
from video_to_skill.work import AnalysisRun, CanonicalRecord, WorkItem, WorkRole, WorkState
from video_to_skill.workspace import CanonicalOutputSnapshot, Workspace

CURRICULUM_PLANNER_PERSONA = (
    "You are a principal learning-science and curriculum architect with deep experience "
    "turning grounded semantic graphs into materially distinct, coherent learning journeys. "
    "Preserve source meaning, conceptual dependencies, and application value without planning "
    "publication artifacts or drafting course content."
)

_CURRICULUM_INPUT_KINDS = (
    "semantic-map",
    "semantic-relations",
    "capability-evidence",
    "semantic-coverage",
    "semantic-conflicts",
)


@dataclass(frozen=True)
class CanonicalCurriculumCheckpoint:
    plan: CurriculumPlan
    plan_record: CanonicalRecord
    selection: CurriculumSelection
    selection_record: CanonicalRecord


def _verified_checkpoint_path(
    workspace: Workspace,
    record: CanonicalRecord,
    *,
    label: str,
) -> Path:
    if record.path.is_absolute() or ".." in record.path.parts:
        raise ProcessingError(f"Canonical {label} path is unsafe")
    path = workspace.root / record.path
    parents = list(path.parents)
    try:
        workspace_parent_index = parents.index(workspace.root)
    except ValueError as exc:
        raise ProcessingError(f"Canonical {label} path is outside the workspace") from exc
    workspace_parents = parents[:workspace_parent_index]
    try:
        if (
            path.is_symlink()
            or any(parent.is_symlink() for parent in workspace_parents)
            or not path.is_file()
            or path.stat().st_size > 2 * 1024 * 1024
            or hash_file(path) != record.digest
        ):
            raise ProcessingError(f"Canonical {label} failed its digest check")
    except OSError as exc:
        raise ProcessingError(f"Could not verify canonical {label}: {exc}") from exc
    return path


def _canonical_path(workspace: Workspace, kind: str) -> tuple[str, str]:
    record = workspace.canonical_record(kind)
    if record is None:
        raise ProcessingError(f"Curriculum planning requires canonical {kind}")
    return str(record.path), record.digest


def _load_semantic_units(workspace: Workspace) -> list[SemanticUnit]:
    record = workspace.canonical_record("semantic-map")
    if record is None:
        raise ProcessingError("Curriculum planning requires a canonical semantic map")
    path = workspace.root / record.path
    try:
        if hash_file(path) != record.digest:
            raise ProcessingError("Canonical semantic map failed its digest check")
        raw = json.loads(path.read_text(encoding="utf-8"))
        return [SemanticUnit.model_validate(item) for item in raw]
    except (OSError, ValueError, PydanticValidationError) as exc:
        if isinstance(exc, ProcessingError):
            raise
        raise ProcessingError(f"Invalid canonical semantic map: {exc}") from exc


def plan_curriculum_task(
    workspace: Workspace,
    run: AnalysisRun,
    *,
    analyze_task: WorkItem,
) -> WorkItem:
    if (
        analyze_task.role != WorkRole.ANALYZE
        or analyze_task.state != WorkState.COMPLETE
        or analyze_task.scope.get("integrated") is not True
    ):
        raise ProcessingError("Curriculum planning requires a completed integrated Analyze task")
    semantic_map = workspace.canonical_record("semantic-map")
    if semantic_map is None or semantic_map.producer_task_id != analyze_task.id:
        raise ProcessingError("Curriculum planning requires the integrated canonical semantic map")
    records = {kind: _canonical_path(workspace, kind) for kind in _CURRICULUM_INPUT_KINDS}
    record_digests = {kind: digest for kind, (_path, digest) in records.items()}
    language_contract, language_contract_digest = ensure_artifact_language_contract(workspace)
    language_instruction = (
        f"Use exactly {language_contract.fixed_artifact_language!r} as the canonical artifact "
        "language and echo it in artifact_language."
        if language_contract.fixed_artifact_language is not None
        else (
            "Declare exactly one observed source language in artifact_language from: "
            + ", ".join(language_contract.source_languages)
            + "."
            if language_contract.resolution == "source-mixed" and language_contract.source_languages
            else (
                "Source language is mixed or unknown. Use evidence-informed judgment to declare "
                "one concrete canonical artifact language in artifact_language; never return "
                "`source`."
            )
        )
    )
    return workspace.ensure_work_item(
        run_id=run.id,
        role=WorkRole.AUTHOR,
        scope={
            "kind": "curriculum-planning",
            "analyze_task_id": analyze_task.id,
            "canonical_record_digests": record_digests,
            "artifact_language_contract_digest": language_contract_digest,
        },
        persona_hint=CURRICULUM_PLANNER_PERSONA,
        packet={
            "instructions": (
                "Design the curriculum checkpoint only. Recommend a thematic path and propose "
                "two or three materially different paths when the source justifies them. Order "
                "each path's semantic unit IDs as its learning sequence. Mark decision_required "
                "only when choosing among the paths would materially change the user's learning "
                "experience, and provide a concise user-facing decision summary then. Do not "
                "design artifact boundaries or IDs, write Markdown drafts, create claims, or "
                f"select assets. {language_instruction} Write all user-facing curriculum "
                "metadata in that same language while preserving proper names and technical "
                "terms where appropriate."
            ),
            "artifact_language_contract": language_contract.model_dump(mode="json"),
            "artifact_language_contract_digest": language_contract_digest,
            "canonical_records": {
                kind: {"path": path, "digest": digest} for kind, (path, digest) in records.items()
            },
        },
        result_schema=CurriculumPlanResult.model_json_schema(mode="validation"),
        dependencies=[analyze_task.id],
        snapshot_digest=run.snapshot_digest,
    )


def load_canonical_curriculum_plan(
    workspace: Workspace,
    *,
    expected_digest: str | None = None,
    expected_producer_task_id: str | None = None,
) -> tuple[CurriculumPlan, CanonicalRecord]:
    record = workspace.canonical_record("curriculum-options")
    if record is None:
        raise ProcessingError("A canonical curriculum plan is required")
    if expected_digest is not None and record.digest != expected_digest:
        raise ProcessingError("Canonical curriculum plan changed after the task was planned")
    if (
        expected_producer_task_id is not None
        and record.producer_task_id != expected_producer_task_id
    ):
        raise ProcessingError("Canonical curriculum plan has an unexpected producer")
    path = _verified_checkpoint_path(workspace, record, label="curriculum plan")
    try:
        return CurriculumPlan.model_validate_json(path.read_text(encoding="utf-8")), record
    except (OSError, UnicodeDecodeError, PydanticValidationError) as exc:
        if isinstance(exc, ProcessingError):
            raise
        raise ProcessingError(f"Invalid canonical curriculum plan: {exc}") from exc


def load_canonical_curriculum_selection(
    workspace: Workspace,
    *,
    expected_digest: str | None = None,
    expected_producer_task_id: str | None = None,
) -> tuple[CurriculumSelection, CanonicalRecord]:
    record = workspace.canonical_record("selected-curriculum")
    if record is None:
        raise ProcessingError("A canonical curriculum selection is required")
    if expected_digest is not None and record.digest != expected_digest:
        raise ProcessingError("Canonical curriculum selection changed after the task was planned")
    if (
        expected_producer_task_id is not None
        and record.producer_task_id != expected_producer_task_id
    ):
        raise ProcessingError("Canonical curriculum selection has an unexpected producer")
    path = _verified_checkpoint_path(workspace, record, label="curriculum selection")
    try:
        return CurriculumSelection.model_validate_json(path.read_text(encoding="utf-8")), record
    except (OSError, UnicodeDecodeError, PydanticValidationError) as exc:
        if isinstance(exc, ProcessingError):
            raise
        raise ProcessingError(f"Invalid canonical curriculum selection: {exc}") from exc


def _completed_checkpoint_task(
    workspace: Workspace,
    task_id: str,
    *,
    label: str,
) -> WorkItem:
    task = workspace.get_work_item(task_id)
    if task.state != WorkState.COMPLETE:
        raise ProcessingError(f"Canonical {label} producer task is not complete")
    return task


def load_canonical_curriculum_checkpoint(
    workspace: Workspace,
) -> CanonicalCurriculumCheckpoint | None:
    plan_record = workspace.canonical_record("curriculum-options")
    selection_record = workspace.canonical_record("selected-curriculum")
    if (plan_record is None) != (selection_record is None):
        raise ProcessingError("Canonical curriculum checkpoint requires both options and selection")
    if plan_record is None or selection_record is None:
        curriculum_record = workspace.canonical_record("curriculum")
        if curriculum_record is not None:
            author_task = workspace.get_work_item(curriculum_record.producer_task_id)
            checkpoint_scope_keys = {
                "curriculum_task_id",
                "curriculum_plan_digest",
                "curriculum_selection_digest",
                "curriculum_selection_producer_task_id",
            }
            if checkpoint_scope_keys & set(author_task.scope):
                raise ProcessingError(
                    "Checkpoint-bound Author state is missing canonical curriculum records"
                )
        return None
    plan, plan_record = load_canonical_curriculum_plan(workspace)
    selection, selection_record = load_canonical_curriculum_selection(workspace)
    if selection.curriculum_plan_digest != plan_record.digest:
        raise ProcessingError("Canonical curriculum selection targets different options")
    if selection.selected_path_id not in {path.id for path in plan.paths}:
        raise ProcessingError("Canonical curriculum selection identifies an unknown path")

    plan_task = _completed_checkpoint_task(
        workspace,
        plan_record.producer_task_id,
        label="curriculum plan",
    )
    if plan_task.role != WorkRole.AUTHOR or plan_task.scope.get("kind") not in {
        "curriculum-planning",
        "curriculum-reuse",
    }:
        raise ProcessingError("Canonical curriculum plan has an invalid producer task")
    selection_task = _completed_checkpoint_task(
        workspace,
        selection_record.producer_task_id,
        label="curriculum selection",
    )
    if selection.source == "edition":
        if (
            plan_task.scope.get("kind") != "curriculum-reuse"
            or selection_task.id != plan_task.id
            or plan_task.scope.get("selected_path_id") != selection.selected_path_id
        ):
            raise ProcessingError("Reused edition curriculum has an invalid producer")
    elif selection.source == "recommended":
        if plan.decision_required or selection_task.id != plan_task.id:
            raise ProcessingError("Recommended curriculum selection has an invalid producer")
    elif (
        not plan.decision_required
        or selection_task.role != WorkRole.DECISION
        or selection_task.scope.get("kind") != "curriculum-plan-selection"
        or selection_task.scope.get("curriculum_task_id") != plan_task.id
        or selection_task.scope.get("curriculum_plan_digest") != plan_record.digest
    ):
        raise ProcessingError("User curriculum selection has an invalid decision binding")
    return CanonicalCurriculumCheckpoint(
        plan=plan,
        plan_record=plan_record,
        selection=selection,
        selection_record=selection_record,
    )


def validate_curriculum_checkpoint_author_binding(
    workspace: Workspace,
    checkpoint: CanonicalCurriculumCheckpoint,
) -> WorkItem:
    curriculum_record = workspace.canonical_record("curriculum")
    if curriculum_record is None:
        raise ProcessingError("Curriculum checkpoint requires final artifact-bound curriculum")
    author_task = _completed_checkpoint_task(
        workspace,
        curriculum_record.producer_task_id,
        label="artifact-bound curriculum",
    )
    if (
        author_task.role != WorkRole.AUTHOR
        or author_task.scope.get("kind") not in {"course-authoring", "course-authoring-repair"}
        or author_task.scope.get("curriculum_task_id") != checkpoint.plan_record.producer_task_id
        or author_task.scope.get("curriculum_plan_digest") != checkpoint.plan_record.digest
        or author_task.scope.get("curriculum_selection_digest")
        != checkpoint.selection_record.digest
        or author_task.scope.get("curriculum_selection_producer_task_id")
        != checkpoint.selection_record.producer_task_id
    ):
        raise ProcessingError("Artifact Author is not bound to the current curriculum checkpoint")
    return author_task


def validate_artifact_bound_curriculum(
    checkpoint: CanonicalCurriculumCheckpoint,
    curriculum: CurriculumDesign,
    artifacts: list[ArtifactDraftSpec],
    *,
    allow_localized_metadata: bool = False,
) -> None:
    if curriculum.selected_path_id != checkpoint.selection.selected_path_id:
        raise ProcessingError("Final curriculum differs from the selected canonical path")
    planned_paths = checkpoint.plan.paths
    if (not allow_localized_metadata and curriculum.rationale != checkpoint.plan.rationale) or [
        path.id for path in curriculum.paths
    ] != [path.id for path in planned_paths]:
        raise ProcessingError("Final curriculum differs from the canonical options")
    artifacts_by_id = {artifact.id: artifact for artifact in artifacts}
    for planned_path, final_path in zip(planned_paths, curriculum.paths, strict=True):
        if final_path.kind != planned_path.kind or (
            not allow_localized_metadata
            and (
                final_path.title != planned_path.title
                or final_path.use_when != planned_path.use_when
            )
        ):
            raise ProcessingError(f"Final curriculum path metadata differs for {planned_path.id}")
        unit_sequence: list[str] = []
        seen_units: set[str] = set()
        for artifact_id in final_path.artifact_ids:
            artifact = artifacts_by_id.get(artifact_id)
            if artifact is None:
                raise ProcessingError(
                    f"Final curriculum path references unknown artifact {artifact_id}"
                )
            for unit_id in artifact.semantic_unit_ids:
                if unit_id not in seen_units:
                    unit_sequence.append(unit_id)
                    seen_units.add(unit_id)
        position = 0
        for unit_id in unit_sequence:
            if (
                position < len(planned_path.unit_sequence)
                and unit_id == planned_path.unit_sequence[position]
            ):
                position += 1
        if position != len(planned_path.unit_sequence):
            raise ProcessingError(f"Final curriculum unit sequence differs for {planned_path.id}")


def _load_curriculum_plan_result(payload: bytes) -> CurriculumPlanResult:
    try:
        return CurriculumPlanResult.model_validate_json(payload)
    except PydanticValidationError as exc:
        raise ProcessingError(f"Invalid curriculum plan result: {exc}") from exc
    except (UnicodeDecodeError, ValueError) as exc:
        if isinstance(exc, ProcessingError):
            raise
        raise ProcessingError(f"Could not read curriculum plan result: {exc}") from exc


def _validate_curriculum_plan(
    workspace: Workspace,
    task: WorkItem,
    curriculum: CurriculumPlan,
) -> None:
    try:
        packet = json.loads((workspace.root / task.packet_path).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProcessingError(f"Invalid curriculum task packet: {exc}") from exc
    if not isinstance(packet, dict) or stable_hash(packet, length=64) != task.packet_digest:
        raise ProcessingError("Curriculum task packet failed its digest check")
    expected_digests = task.scope.get("canonical_record_digests")
    if not isinstance(expected_digests, dict):
        raise ProcessingError("Curriculum task is missing its canonical input digests")
    analyze_task_id = task.scope.get("analyze_task_id")
    try:
        for kind in _CURRICULUM_INPUT_KINDS:
            record = workspace.canonical_record(kind)
            if record is None:
                raise ProcessingError("Curriculum plan targets stale canonical Analyze state")
            record_path = workspace.root / record.path
            if (
                record.digest != expected_digests.get(kind)
                or record.producer_task_id != analyze_task_id
                or not record_path.is_file()
                or record_path.is_symlink()
                or hash_file(record_path) != record.digest
            ):
                raise ProcessingError("Curriculum plan targets stale canonical Analyze state")
    except OSError as exc:
        raise ProcessingError(f"Could not verify canonical Analyze state: {exc}") from exc
    units = _load_semantic_units(workspace)
    known_units = {unit.id for unit in units}
    required_units = {
        unit.id
        for unit in units
        if unit.materiality in {"core", "supporting"} and unit.disposition == "included"
    }
    for path in curriculum.paths:
        path_units = set(path.unit_sequence)
        if not path_units <= known_units:
            raise ProcessingError(f"Curriculum path {path.id} references unknown semantic units")
        if missing := required_units - path_units:
            raise ProcessingError(
                f"Curriculum path {path.id} omits included material semantic units: "
                + ", ".join(sorted(missing))
            )


def _curriculum_language_declaration(
    workspace: Workspace,
    task: WorkItem,
    artifact_language: str,
) -> ArtifactLanguageDeclaration:
    expected_digest = task.scope.get("artifact_language_contract_digest")
    if not isinstance(expected_digest, str):
        raise ProcessingError("Curriculum task is missing its artifact-language binding")
    contract, contract_digest = load_artifact_language_contract(workspace)
    if contract_digest != expected_digest:
        raise ProcessingError("Curriculum task targets a stale artifact-language contract")
    return declare_artifact_language(contract, contract_digest, artifact_language)


def submit_curriculum_plan_result(
    workspace: Workspace,
    task_id: str,
    result_path: Path,
) -> WorkItem:
    task = workspace.get_work_item(task_id)
    if task.role != WorkRole.AUTHOR or task.scope.get("kind") != "curriculum-planning":
        raise ProcessingError(f"Task is not a curriculum-planning Author task: {task_id}")
    result_snapshot = workspace.task_output_file_snapshot(
        task_id,
        result_path,
        max_bytes=2 * 1024 * 1024,
    )
    result = _load_curriculum_plan_result(result_snapshot.payload)
    if result.task_id != task.id or result.snapshot_digest != task.snapshot_digest:
        raise ProcessingError("Curriculum plan result does not belong to this task snapshot")
    _validate_curriculum_plan(workspace, task, result.curriculum)
    language_declaration = _curriculum_language_declaration(
        workspace,
        task,
        result.artifact_language,
    )
    output_directory = workspace.tasks_dir / task.id / "output"
    options_path = output_directory / "curriculum-options.json"
    language_path = output_directory / "artifact-language.json"
    workspace.write_json(options_path, result.curriculum)
    workspace.write_json(language_path, language_declaration)
    options_snapshot = workspace.canonical_output_file_snapshot(
        task.id,
        "curriculum-options",
        "default",
        options_path,
    )
    canonical_outputs: list[CanonicalOutputSnapshot] = [
        options_snapshot,
        workspace.canonical_output_file_snapshot(
            task.id,
            "artifact-language-declaration",
            "default",
            language_path,
        ),
    ]
    if not result.curriculum.decision_required:
        selection_path = output_directory / "selected-curriculum.json"
        workspace.write_json(
            selection_path,
            CurriculumSelection(
                curriculum_plan_digest=options_snapshot.file.digest,
                selected_path_id=result.curriculum.recommended_path_id,
                source="recommended",
            ),
        )
        canonical_outputs.append(
            workspace.canonical_output_file_snapshot(
                task.id,
                "selected-curriculum",
                "default",
                selection_path,
            )
        )
    accepted, _records = workspace.accept_work_result(
        task_id=task.id,
        lease_token=result.lease_token,
        result_path=result_path,
        result_snapshot=result_snapshot,
        producer=result.producer.model_dump(mode="json"),
        canonical_outputs=canonical_outputs,
    )
    return accepted
