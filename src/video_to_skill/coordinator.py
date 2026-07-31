"""Quiescence-driven orchestration over durable workspace tasks."""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from pathlib import Path
from typing import Literal

from pydantic import ValidationError as PydanticValidationError

from video_to_skill.analysis_depth import verify_analysis_depth_contract
from video_to_skill.analyze import (
    plan_analyze_integration_task,
    plan_analyze_tasks,
    submit_analyze_result,
)
from video_to_skill.artifact_language import (
    ArtifactLanguageContract,
    ensure_artifact_language_contract,
)
from video_to_skill.author import plan_author_task, submit_author_result
from video_to_skill.compiler import (
    WorkspaceBuildResult,
    build_workspace_skill,
    compile_workspace_blueprint,
    portable_build_matches,
)
from video_to_skill.config import Settings
from video_to_skill.curriculum import (
    load_canonical_curriculum_plan,
    plan_curriculum_task,
    submit_curriculum_plan_result,
)
from video_to_skill.editions import (
    LEGACY_EDITION_NAME,
    assert_edition_analysis_lineage,
    create_edition_state,
    current_analysis_lineage,
    curriculum_checkpoint,
    edition_state_exists,
    identity_baseline,
    load_edition_state,
    mark_edition_progress,
    materialize_reused_curriculum,
    public_edition_lineage,
)
from video_to_skill.errors import ProcessingError
from video_to_skill.generation import CurriculumDesign
from video_to_skill.installation import SkillHost, host_skill_root
from video_to_skill.models import AnalysisDepthContract
from video_to_skill.orchestration import (
    CurriculumSelection,
    DecisionResult,
    DeliverySelection,
    RunAction,
    RunEnvelope,
    SubmissionReceipt,
)
from video_to_skill.pipeline import extract_sources
from video_to_skill.review import (
    MAX_REPAIR_CYCLES,
    plan_author_repair_task,
    plan_behavior_trial_tasks,
    plan_review_task,
    submit_behavior_trial_result,
    submit_review_result,
)
from video_to_skill.utils import stable_hash
from video_to_skill.work import AnalysisRun, WorkItem, WorkRole, WorkState
from video_to_skill.workspace import Workspace

ProgressCallback = Callable[[str], None]


def _load_json(path: Path) -> object:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProcessingError(f"Invalid orchestration JSON {path}: {exc}") from exc


def _run_configuration(
    workspace: Workspace,
    *,
    host: SkillHost | None,
    output: Path | None,
    project: bool | None,
    project_root: Path | None,
    skill_root: Path | None,
    run_official_validation: bool,
    settings: Settings,
    output_language_override: str | None,
    refresh: bool,
) -> dict[str, object]:
    path = workspace.analysis_dir / "run-config.json"
    if workspace.edition_id is not None:
        edition_state = assert_edition_analysis_lineage(workspace)
        immutable = edition_state.configuration
        if host is not None and host.value != immutable.host:
            raise ProcessingError("Resume host differs from the edition configuration")
        if output is not None and output.expanduser().resolve() != immutable.output:
            raise ProcessingError("Resume output differs from the edition configuration")
        if project is not None and project != immutable.project:
            raise ProcessingError("Resume installation scope differs from the edition")
        if project_root is not None and project_root.resolve() != immutable.project_root:
            raise ProcessingError("Resume project root differs from the edition configuration")
        if skill_root is not None and skill_root.resolve() != immutable.skill_root:
            raise ProcessingError("Resume Skill root differs from the edition configuration")
        if (
            output_language_override is not None
            and output_language_override != immutable.requested_output_language
        ):
            raise ProcessingError("Resume language differs from the edition configuration")
        analysis_depth = workspace.analysis_depth_contract()
        if analysis_depth is None:
            raise ProcessingError("Workspace is missing its analysis-depth contract")
        verify_analysis_depth_contract(analysis_depth, settings=settings)
        artifact_contract, artifact_contract_digest = ensure_artifact_language_contract(
            workspace,
            immutable.requested_output_language,
            verify_source_resolution=True,
        )
        expected: dict[str, object] = {
            "edition_config_digest": immutable.config_digest,
            "host": immutable.host,
            "output": str(immutable.output),
            "output_is_default": immutable.output_is_default,
            "project": immutable.project,
            "project_root": str(immutable.project_root),
            "skill_root": str(immutable.skill_root) if immutable.skill_root is not None else None,
            "run_official_validation": immutable.run_official_validation,
            "artifact_language_contract": artifact_contract.model_dump(mode="json"),
            "artifact_language_contract_digest": artifact_contract_digest,
            "analysis_depth_contract": analysis_depth.model_dump(mode="json"),
            "analysis_depth_contract_digest": stable_hash(
                analysis_depth.model_dump(mode="json"),
                length=64,
            ),
        }
        if path.exists() or path.is_symlink():
            try:
                raw_configuration = json.loads(
                    workspace.read_file_bytes(path, max_bytes=1024 * 1024).decode("utf-8")
                )
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ProcessingError(f"Invalid edition run configuration: {exc}") from exc
            if raw_configuration != expected:
                raise ProcessingError(
                    "Edition run configuration failed immutable authority reconciliation"
                )
        else:
            workspace.write_json(path, expected)
        return expected
    if path.exists():
        raw_configuration = _load_json(path)
        if not isinstance(raw_configuration, dict):
            raise ProcessingError("Workspace run configuration must be a JSON object")
        existing: dict[str, object] = {str(key): item for key, item in raw_configuration.items()}
        configured_host = SkillHost(str(existing["host"]))
        if host is not None and host != configured_host:
            raise ProcessingError("Resume host differs from the workspace run configuration")
        if output is not None and output.expanduser().resolve() != Path(str(existing["output"])):
            raise ProcessingError("Resume output differs from the workspace run configuration")
        if project is not None and project != bool(existing["project"]):
            raise ProcessingError("Resume installation scope differs from the workspace")
        analysis_depth = workspace.analysis_depth_contract()
        if analysis_depth is None:
            raise ProcessingError("Workspace is missing its analysis-depth contract")
        verify_analysis_depth_contract(analysis_depth, settings=settings)
        analysis_depth_digest = stable_hash(
            analysis_depth.model_dump(mode="json"),
            length=64,
        )
        embedded_depth = existing.get("analysis_depth_contract")
        embedded_depth_digest = existing.get("analysis_depth_contract_digest")
        if embedded_depth is None and embedded_depth_digest is None:
            existing["analysis_depth_contract"] = analysis_depth.model_dump(mode="json")
            existing["analysis_depth_contract_digest"] = analysis_depth_digest
            workspace.write_json(path, existing)
        else:
            try:
                validated_depth = AnalysisDepthContract.model_validate(embedded_depth)
            except PydanticValidationError as exc:
                raise ProcessingError(f"Invalid persisted analysis-depth contract: {exc}") from exc
            if validated_depth != analysis_depth or embedded_depth_digest != analysis_depth_digest:
                inventory_refreshed = (
                    refresh
                    and validated_depth.requested == analysis_depth.requested
                    and validated_depth.characteristics.inventory_digest
                    != analysis_depth.characteristics.inventory_digest
                )
                if not inventory_refreshed:
                    raise ProcessingError(
                        "Run configuration analysis-depth contract failed its digest check"
                    )
                existing["analysis_depth_contract"] = analysis_depth.model_dump(mode="json")
                existing["analysis_depth_contract_digest"] = analysis_depth_digest
                workspace.write_json(path, existing)
        persisted_contract, persisted_digest = ensure_artifact_language_contract(
            workspace,
            output_language_override,
            verify_source_resolution=True,
        )
        embedded_contract = existing.get("artifact_language_contract")
        embedded_digest = existing.get("artifact_language_contract_digest")
        if embedded_contract is None and embedded_digest is None:
            existing["artifact_language_contract"] = persisted_contract.model_dump(mode="json")
            existing["artifact_language_contract_digest"] = persisted_digest
            workspace.write_json(path, existing)
        else:
            try:
                validated_embedded = ArtifactLanguageContract.model_validate(embedded_contract)
            except PydanticValidationError as exc:
                raise ProcessingError(
                    f"Invalid persisted artifact-language contract: {exc}"
                ) from exc
            if validated_embedded != persisted_contract or embedded_digest != persisted_digest:
                raise ProcessingError(
                    "Run configuration artifact-language contract failed its digest check"
                )
        return existing
    if host is None:
        raise ProcessingError("A new orchestration run requires --host")
    resolved_output = (
        output.expanduser().resolve()
        if output is not None
        else (Path.cwd() / "generated-skills" / "pending-course").resolve()
    )
    manifest = workspace.load_manifest()
    requested_output_language = output_language_override or manifest.output_language
    if not requested_output_language:
        requested_output_language = settings.output_language
    artifact_contract, artifact_contract_digest = ensure_artifact_language_contract(
        workspace,
        requested_output_language,
        verify_source_resolution=True,
    )
    analysis_depth = workspace.analysis_depth_contract()
    if analysis_depth is None:
        raise ProcessingError("Workspace is missing its analysis-depth contract")
    verify_analysis_depth_contract(analysis_depth, settings=settings)
    configuration: dict[str, object] = {
        "host": host.value,
        "output": str(resolved_output),
        "output_is_default": output is None,
        "project": bool(project),
        "project_root": str((project_root or Path.cwd()).resolve()),
        "skill_root": str(skill_root.resolve()) if skill_root is not None else None,
        "run_official_validation": run_official_validation,
        "artifact_language_contract": artifact_contract.model_dump(mode="json"),
        "artifact_language_contract_digest": artifact_contract_digest,
        "analysis_depth_contract": analysis_depth.model_dump(mode="json"),
        "analysis_depth_contract_digest": stable_hash(
            analysis_depth.model_dump(mode="json"),
            length=64,
        ),
    }
    workspace.write_json(path, configuration)
    return configuration


def _task_action(workspace: Workspace, item: WorkItem) -> RunAction:
    task_path = workspace.tasks_dir / item.id
    already_leased = item.state == WorkState.LEASED
    if item.state == WorkState.PENDING:
        item = workspace.lease_work_item(item.id, owner="host-main-agent").item
    elif item.state != WorkState.LEASED:
        raise ProcessingError(f"Task cannot be dispatched from state {item.state}: {item.id}")
    if item.role == WorkRole.DECISION:
        packet = _load_json(workspace.root / item.packet_path)
        assert isinstance(packet, dict)
        payload = packet["payload"]
        assert isinstance(payload, dict)
        return RunAction(
            kind="ask-user",
            task_id=item.id,
            task_path=task_path,
            role=item.role.value,
            already_leased=already_leased,
            prompt=str(payload["prompt"]),
            options=list(payload["options"]),
        )
    return RunAction(
        kind="dispatch-agent",
        task_id=item.id,
        task_path=task_path,
        role=item.role.value,
        persona_hint=item.persona_hint,
        parallel_group=f"{item.run_id}-{item.role.value}",
        already_leased=already_leased,
    )


def _actions_required(workspace: Workspace, items: list[WorkItem]) -> RunEnvelope:
    failed = [item for item in items if item.state == WorkState.FAILED]
    if failed:
        raise ProcessingError(
            "Required workspace tasks failed: "
            + ", ".join(f"{item.id} ({item.failure_reason})" for item in failed)
        )
    dispatchable = [item for item in items if item.state in {WorkState.PENDING, WorkState.LEASED}]
    if not dispatchable:
        raise ProcessingError("Coordinator reached an incomplete stage without dispatchable tasks")
    edition_id: str | None = None
    edition_name: str | None = None
    if workspace.edition_id is not None:
        state = assert_edition_analysis_lineage(workspace)
        edition_id = state.configuration.edition_id
        edition_name = state.configuration.edition_name
    return RunEnvelope(
        status="actions-required",
        workspace=workspace.root,
        edition_id=edition_id,
        edition_name=edition_name,
        actions=[_task_action(workspace, item) for item in dispatchable],
    )


def _course_requires_decision(workspace: Workspace) -> bool:
    record = workspace.canonical_record("course")
    if record is None:
        return False
    course = _load_json(workspace.root / record.path)
    return isinstance(course, dict) and bool(course.get("curriculum_decision_required"))


def _curriculum_requires_decision(
    workspace: Workspace,
    curriculum_task: WorkItem,
) -> bool:
    if curriculum_task.scope.get("kind") == "curriculum-reuse":
        return False
    curriculum, _record = load_canonical_curriculum_plan(
        workspace,
        expected_producer_task_id=curriculum_task.id,
    )
    return curriculum.decision_required


def _plan_decision_task(
    workspace: Workspace,
    run: AnalysisRun,
    author_task: WorkItem,
) -> WorkItem:
    curriculum_record = workspace.canonical_record("curriculum")
    course_record = workspace.canonical_record("course")
    if curriculum_record is None or course_record is None:
        raise ProcessingError("Curriculum decision requires canonical Author state")
    curriculum = CurriculumDesign.model_validate(
        _load_json(workspace.root / curriculum_record.path)
    )
    course = _load_json(workspace.root / course_record.path)
    assert isinstance(course, dict)
    prompt = str(
        course.get("curriculum_decision_summary") or "Choose the primary learning experience."
    )
    return workspace.ensure_work_item(
        run_id=run.id,
        role=WorkRole.DECISION,
        scope={
            "kind": "curriculum-selection",
            "author_task_id": author_task.id,
        },
        persona_hint="User curriculum decision.",
        packet={
            "prompt": prompt,
            "options": [
                {
                    "id": path.id,
                    "label": path.title,
                    "description": path.use_when,
                }
                for path in curriculum.paths
            ],
            "curriculum_path": str(curriculum_record.path),
        },
        result_schema=DecisionResult.model_json_schema(mode="validation"),
        dependencies=[author_task.id],
        snapshot_digest=run.snapshot_digest,
    )


def _plan_curriculum_decision_task(
    workspace: Workspace,
    run: AnalysisRun,
    curriculum_task: WorkItem,
) -> WorkItem:
    curriculum, curriculum_record = load_canonical_curriculum_plan(
        workspace,
        expected_producer_task_id=curriculum_task.id,
    )
    if not curriculum.decision_required or curriculum.decision_summary is None:
        raise ProcessingError("Curriculum options do not require a material user decision")
    return workspace.ensure_work_item(
        run_id=run.id,
        role=WorkRole.DECISION,
        scope={
            "kind": "curriculum-plan-selection",
            "curriculum_task_id": curriculum_task.id,
            "curriculum_plan_digest": curriculum_record.digest,
        },
        persona_hint="User curriculum decision.",
        packet={
            "prompt": curriculum.decision_summary,
            "options": [
                {
                    "id": path.id,
                    "label": path.title,
                    "description": path.use_when,
                }
                for path in curriculum.paths
            ],
            "curriculum_options_path": str(curriculum_record.path),
            "curriculum_options_digest": curriculum_record.digest,
        },
        result_schema=DecisionResult.model_json_schema(mode="validation"),
        dependencies=[curriculum_task.id],
        snapshot_digest=run.snapshot_digest,
    )


def _verified_task_packet(workspace: Workspace, task: WorkItem) -> dict[str, object]:
    packet = _load_json(workspace.root / task.packet_path)
    if not isinstance(packet, dict) or stable_hash(packet, length=64) != task.packet_digest:
        raise ProcessingError("Decision task packet failed its digest check")
    return packet


def _available_delivery_options(
    *,
    name: str,
    output: Path,
    skill_root: Path,
) -> list[dict[str, str]]:
    options: list[dict[str, str]] = []
    for ordinal in range(2, 102):
        suffix = f"-{ordinal}"
        prefix = name[: 64 - len(suffix)].rstrip("-")
        candidate = f"{prefix}{suffix}"
        candidate_output = output.with_name(candidate)
        if candidate_output.exists() or (skill_root / candidate).exists():
            continue
        options.append(
            {
                "id": candidate,
                "label": candidate,
                "description": (f"Generate at {candidate_output} and install as {candidate}."),
                "output": str(candidate_output),
            }
        )
        if len(options) == 3:
            return options
    raise ProcessingError("Could not find an available generated Skill name")


def _plan_delivery_decision_task(
    workspace: Workspace,
    run: AnalysisRun,
    *,
    author_task: WorkItem,
    review_task: WorkItem,
    build_id: str,
    name: str,
    output: Path,
    skill_root: Path,
    conflicts: list[str],
) -> WorkItem:
    options = _available_delivery_options(
        name=name,
        output=output,
        skill_root=skill_root,
    )
    return workspace.ensure_work_item(
        run_id=run.id,
        role=WorkRole.DECISION,
        scope={
            "kind": "delivery-name-selection",
            "author_task_id": author_task.id,
            "conflicting_build_id": build_id,
        },
        persona_hint="User generated Skill naming decision.",
        packet={
            "prompt": (
                f"The generated Skill name '{name}' conflicts with different content. "
                "Choose an available portable name and output."
            ),
            "options": options,
            "conflicts": conflicts,
            "build_id": build_id,
        },
        result_schema=DecisionResult.model_json_schema(mode="validation"),
        dependencies=[review_task.id],
        snapshot_digest=run.snapshot_digest,
    )


def submit_decision_result(
    workspace: Workspace,
    task_id: str,
    result_path: Path,
) -> WorkItem:
    task = workspace.get_work_item(task_id)
    if task.role != WorkRole.DECISION:
        raise ProcessingError(f"Task is not a user decision task: {task_id}")
    try:
        result = DecisionResult.model_validate_json(result_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, PydanticValidationError) as exc:
        raise ProcessingError(f"Invalid user decision result: {exc}") from exc
    if result.task_id != task.id or result.snapshot_digest != task.snapshot_digest:
        raise ProcessingError("User decision does not belong to this task snapshot")
    _verified_task_packet(workspace, task)
    canonical_outputs: list[tuple[str, str, Path]]
    if task.scope.get("kind") == "curriculum-plan-selection":
        curriculum_task_id = task.scope.get("curriculum_task_id")
        expected_plan_digest = task.scope.get("curriculum_plan_digest")
        if not isinstance(curriculum_task_id, str) or not isinstance(expected_plan_digest, str):
            raise ProcessingError("Curriculum decision is missing its canonical plan binding")
        curriculum_plan, curriculum_plan_record = load_canonical_curriculum_plan(
            workspace,
            expected_digest=expected_plan_digest,
            expected_producer_task_id=curriculum_task_id,
        )
        if result.selected_option_id not in {path.id for path in curriculum_plan.paths}:
            raise ProcessingError("User selected an unknown curriculum path")
        curriculum_selection = CurriculumSelection(
            curriculum_plan_digest=curriculum_plan_record.digest,
            selected_path_id=result.selected_option_id,
            source="user",
        )
        output = workspace.tasks_dir / task.id / "output" / "selected-curriculum.json"
        workspace.write_json(output, curriculum_selection)
        canonical_outputs = [("selected-curriculum", "default", output)]
    elif task.scope.get("kind") == "curriculum-selection":
        legacy_curriculum_record = workspace.canonical_record("curriculum")
        if legacy_curriculum_record is None:
            raise ProcessingError("User decision requires canonical curriculum state")
        legacy_curriculum = CurriculumDesign.model_validate(
            _load_json(workspace.root / legacy_curriculum_record.path)
        )
        if result.selected_option_id not in {path.id for path in legacy_curriculum.paths}:
            raise ProcessingError("User selected an unknown curriculum path")
        legacy_selection = legacy_curriculum.model_copy(
            update={"selected_path_id": result.selected_option_id}
        )
        output = workspace.tasks_dir / task.id / "output" / "curriculum.json"
        workspace.write_json(output, legacy_selection)
        canonical_outputs = [("curriculum", "default", output)]
    elif task.scope.get("kind") == "delivery-name-selection":
        packet = _verified_task_packet(workspace, task)
        payload = packet.get("payload")
        if not isinstance(payload, dict):
            raise ProcessingError("Delivery decision packet has no payload")
        options = payload.get("options")
        if not isinstance(options, list):
            raise ProcessingError("Delivery decision packet has no options")
        selected_option = next(
            (
                option
                for option in options
                if isinstance(option, dict) and option.get("id") == result.selected_option_id
            ),
            None,
        )
        if selected_option is None:
            raise ProcessingError("User selected an unknown delivery option")
        selected_name = selected_option.get("id")
        selected_output = selected_option.get("output")
        conflicting_build_id = task.scope.get("conflicting_build_id")
        if (
            not isinstance(selected_name, str)
            or not isinstance(selected_output, str)
            or not isinstance(conflicting_build_id, str)
        ):
            raise ProcessingError("Selected delivery option is malformed")
        try:
            selection = DeliverySelection(
                name=selected_name,
                output=Path(selected_output).resolve(),
                conflicting_build_id=conflicting_build_id,
            )
        except PydanticValidationError as exc:
            raise ProcessingError(f"Selected delivery option is invalid: {exc}") from exc
        output = workspace.tasks_dir / task.id / "output" / "delivery-selection.json"
        workspace.write_json(output, selection)
        canonical_outputs = [("delivery-selection", "default", output)]
    else:
        raise ProcessingError("Decision task has an unknown kind")
    accepted, _records = workspace.accept_work_result(
        task_id=task.id,
        lease_token=result.lease_token,
        result_path=result_path,
        producer=result.producer.model_dump(mode="json"),
        canonical_outputs=canonical_outputs,
    )
    return accepted


def submit_workspace_result(
    workspace: Workspace,
    task_id: str,
    result_path: Path,
) -> SubmissionReceipt:
    scoped_edition = workspace.task_edition_id(task_id)
    if scoped_edition is not None:
        from video_to_skill.editions import (
            assert_edition_analysis_lineage,
            load_edition_state_by_id,
        )

        state = load_edition_state_by_id(workspace, scoped_edition)
        workspace = workspace.for_edition(state.configuration.edition_id)
        assert_edition_analysis_lineage(workspace)
    item = workspace.get_work_item(task_id)
    submitted_result = Path(os.path.abspath(result_path))
    task_output = Path(os.path.abspath(workspace.tasks_dir / task_id / "output"))
    try:
        submitted_result.relative_to(task_output)
        workspace.read_file_bytes(submitted_result, max_bytes=8 * 1024 * 1024)
    except (ValueError, ProcessingError) as exc:
        raise ProcessingError(
            "Submitted result must be a regular task-output JSON file no larger than 8 MiB"
        ) from exc
    if item.role == WorkRole.ANALYZE:
        accepted = submit_analyze_result(workspace, task_id, submitted_result)
    elif item.role == WorkRole.AUTHOR:
        if item.scope.get("kind") == "curriculum-planning":
            accepted = submit_curriculum_plan_result(workspace, task_id, submitted_result)
        else:
            accepted = submit_author_result(workspace, task_id, submitted_result)
    elif item.role == WorkRole.REVIEW:
        if item.scope.get("kind") == "behavior-trial":
            accepted = submit_behavior_trial_result(workspace, task_id, submitted_result)
        else:
            accepted = submit_review_result(workspace, task_id, submitted_result)
    else:
        accepted = submit_decision_result(workspace, task_id, submitted_result)
    assert accepted.result_digest is not None
    return SubmissionReceipt(
        task_id=accepted.id,
        result_digest=accepted.result_digest,
    )


def _matching_tasks(
    tasks: list[WorkItem],
    *,
    scope_key: str,
    scope_value: object,
) -> list[WorkItem]:
    return [item for item in tasks if item.scope.get(scope_key) == scope_value]


def _delivery_targets(
    workspace: Workspace,
    configuration: dict[str, object],
    *,
    name: str,
) -> tuple[Path, Path]:
    if workspace.edition_id is not None:
        immutable = assert_edition_analysis_lineage(workspace).configuration
        authority: dict[str, object] = {
            "host": immutable.host,
            "output": str(immutable.output),
            "output_is_default": immutable.output_is_default,
            "project": immutable.project,
            "project_root": str(immutable.project_root),
            "skill_root": (str(immutable.skill_root) if immutable.skill_root is not None else None),
        }
    else:
        authority = configuration
    selection_record = workspace.canonical_record("delivery-selection")
    if selection_record is not None:
        try:
            selection = DeliverySelection.model_validate(
                _load_json(workspace.root / selection_record.path)
            )
        except PydanticValidationError as exc:
            raise ProcessingError(f"Invalid canonical delivery selection: {exc}") from exc
        if selection.name != name:
            raise ProcessingError("Canonical delivery selection disagrees with the build")
        output = selection.output
    else:
        output = Path(str(authority["output"]))
        if bool(authority["output_is_default"]):
            output = output.with_name(name)
    skill_root = (
        Path(str(authority["skill_root"]))
        if authority.get("skill_root")
        else host_skill_root(
            SkillHost(str(authority["host"])),
            project=bool(authority["project"]),
            project_root=Path(str(authority["project_root"])),
        )
    )
    return output, skill_root


def _delivery_conflicts(
    *,
    name: str,
    build_id: str,
    output: Path,
    skill_root: Path,
) -> list[str]:
    candidates = {
        "generated output": output,
        "installed Skill": skill_root / name,
    }
    return [
        f"{label}: {path}"
        for label, path in candidates.items()
        if (path.exists() or path.is_symlink()) and not portable_build_matches(path, build_id)
    ]


def _completion_payload(
    workspace: Workspace,
    result: WorkspaceBuildResult,
    *,
    review_tasks: list[WorkItem],
) -> dict[str, object]:
    manifest = workspace.load_manifest()
    semantic_record = workspace.canonical_record("semantic-coverage")
    affordance_record = workspace.canonical_record("instructional-affordances")
    if semantic_record is None or affordance_record is None:
        raise ProcessingError("Completion requires canonical coverage records")
    semantic_coverage = _load_json(workspace.root / semantic_record.path)
    affordances = _load_json(workspace.root / affordance_record.path)
    coverage_path = workspace.root / "coverage.json"
    if coverage_path.is_file():
        coverage = _load_json(coverage_path)
    else:
        retention_reports = workspace.visual_retention_reports()
        candidate_count = sum(report.candidate_count for report in retention_reports)
        retained_count = sum(report.retained_count for report in retention_reports)
        dropped_count = sum(report.dropped_count for report in retention_reports)
        coverage = {
            "visual_evidence": {
                "complete": not any(report.truncated for report in retention_reports),
                "candidate_count": candidate_count,
                "retained_count": retained_count,
                "dropped_count": dropped_count,
                "reports": [report.model_dump(mode="json") for report in retention_reports],
            }
        }
    assert isinstance(affordances, list)
    if not isinstance(coverage, dict):
        raise ProcessingError("Completion requires a valid coverage report")
    affordance_summary = {
        status: sum(isinstance(item, dict) and item.get("status") == status for item in affordances)
        for status in ("provided", "unsupported", "not-applicable")
    }
    prefix = "/" if result.host == SkillHost.CLAUDE else "$"
    payload = {
        **result.model_dump(mode="json"),
        "workspace": str(workspace.root),
        "workspace_retained": True,
        "processed_sources": len(workspace.list_sources()),
        "failed_sources": len(manifest.failed_sources),
        "semantic_coverage": semantic_coverage,
        "visual_evidence_coverage": coverage.get("visual_evidence", {}),
        "visual_retention_warnings": [
            warning.model_dump(mode="json")
            for warning in workspace.list_warnings()
            if warning.code == "visual-retention-truncated"
        ],
        "instructional_affordance_coverage": affordance_summary,
        "critic_repairs": max(
            (int(item.scope.get("repair_cycle", 0)) for item in review_tasks),
            default=0,
        ),
        "analysis_depth_contract": (
            manifest.analysis_depth.model_dump(mode="json")
            if manifest.analysis_depth is not None
            else None
        ),
        "invocation": f"{prefix}{result.name}",
    }
    edition_lineage = public_edition_lineage(workspace)
    if edition_lineage is not None:
        payload["edition_lineage"] = edition_lineage
    return payload


def advance_run(
    *,
    sources: list[str],
    workspace_path: Path,
    settings: Settings,
    output_language_override: str | None = None,
    host: SkillHost | None,
    output: Path | None = None,
    project: bool | None = None,
    project_root: Path | None = None,
    skill_root: Path | None = None,
    refresh: bool = False,
    run_official_validation: bool = True,
    progress: ProgressCallback = lambda _message: None,
    edition_name: str | None = None,
    curriculum_path_id: str | None = None,
    curriculum_source_edition: str | None = None,
    plan_curriculum: bool = False,
    skill_name: str | None = None,
    identity_drift_justification: str | None = None,
) -> RunEnvelope:
    pinned_analyze_task: WorkItem | None = None
    reused_curriculum_source: Workspace | None = None
    if edition_name is not None:
        if sources or refresh:
            raise ProcessingError(
                "Named editions reuse completed Analyze state and cannot accept sources or refresh"
            )
        base_workspace = Workspace.open(workspace_path)
        edition_id = "edition-" + stable_hash(
            {
                "job_id": base_workspace.load_manifest().job_id,
                "edition_name": edition_name,
            },
            length=20,
        )
        if edition_state_exists(base_workspace, edition_id):
            edition_state = load_edition_state(base_workspace, edition_name)
            configuration_state = edition_state.configuration
            if host is not None and host.value != configuration_state.host:
                raise ProcessingError("Resume host differs from the edition configuration")
            if output is not None and output.expanduser().resolve() != configuration_state.output:
                raise ProcessingError("Resume output differs from the edition configuration")
            if project is not None and project != configuration_state.project:
                raise ProcessingError("Resume installation scope differs from the edition")
            if skill_name is not None and skill_name != configuration_state.skill_name:
                raise ProcessingError("Resume Skill name differs from the edition configuration")
            if (
                curriculum_path_id is not None
                and curriculum_path_id != configuration_state.requested_curriculum_path_id
            ):
                raise ProcessingError("Resume curriculum differs from the edition configuration")
            if (
                curriculum_source_edition is not None
                and curriculum_source_edition != configuration_state.curriculum_source_edition
            ):
                raise ProcessingError(
                    "Resume curriculum source differs from the edition configuration"
                )
            if plan_curriculum and configuration_state.curriculum_mode != "plan":
                raise ProcessingError("Resume curriculum mode differs from the edition")
            if (
                identity_drift_justification is not None
                and " ".join(identity_drift_justification.split())
                != configuration_state.identity_drift_justification
            ):
                raise ProcessingError("Resume identity policy differs from the edition")
        else:
            if host is None:
                raise ProcessingError("A new named edition requires --host")
            requested_language = (
                output_language_override
                or base_workspace.load_manifest().output_language
                or settings.output_language
            )
            source_name = curriculum_source_edition or LEGACY_EDITION_NAME
            if curriculum_source_edition is None:
                candidate_source = base_workspace
            else:
                source_state = load_edition_state(
                    base_workspace,
                    curriculum_source_edition,
                )
                if source_state.configuration.analysis_lineage != current_analysis_lineage(
                    base_workspace
                ):
                    raise ProcessingError(
                        "Curriculum source edition belongs to different Analyze state"
                    )
                candidate_source = base_workspace.for_edition(source_state.configuration.edition_id)
            source_checkpoint = curriculum_checkpoint(candidate_source)
            curriculum_mode: Literal["reuse", "plan"] = (
                "plan" if plan_curriculum or source_checkpoint is None else "reuse"
            )
            source_plan_digest: str | None = None
            selected_path: str | None = None
            baseline = identity_baseline(candidate_source)
            drift_reason = identity_drift_justification
            if curriculum_mode == "reuse":
                assert source_checkpoint is not None
                source_plan, source_selection, source_plan_digest = source_checkpoint
                selected_path = curriculum_path_id or source_selection.selected_path_id
                if selected_path not in {path.id for path in source_plan.paths}:
                    raise ProcessingError(f"Unknown source curriculum path: {selected_path}")
                source_plan_record = candidate_source.canonical_record("curriculum-options")
                assert source_plan_record is not None
                source_plan_task = candidate_source.get_work_item(
                    source_plan_record.producer_task_id
                )
                if (
                    source_plan_task.scope.get("analyze_task_id")
                    != current_analysis_lineage(base_workspace).analyze_task_id
                ):
                    raise ProcessingError("Source curriculum is pinned to stale Analyze state")
            elif curriculum_path_id is not None:
                raise ProcessingError(
                    "--curriculum requires an existing source plan; omit it when planning anew"
                )
            resolved_output = (
                output.expanduser().resolve()
                if output is not None
                else (Path.cwd() / "generated-skills" / "pending-course").resolve()
            )
            edition_state = create_edition_state(
                base_workspace,
                edition_name=edition_name,
                requested_output_language=requested_language,
                curriculum_mode=curriculum_mode,
                curriculum_source_edition=(source_name if curriculum_mode == "reuse" else None),
                source_curriculum_plan_digest=source_plan_digest,
                requested_curriculum_path_id=selected_path,
                skill_name=skill_name,
                host=host.value,
                output=resolved_output,
                output_is_default=output is None,
                project=bool(project),
                project_root=(project_root or Path.cwd()).resolve(),
                skill_root=skill_root,
                run_official_validation=run_official_validation,
                baseline=(
                    baseline if curriculum_mode == "reuse" else identity_baseline(base_workspace)
                ),
                identity_drift_justification=drift_reason,
            )
        workspace = base_workspace.for_edition(edition_state.configuration.edition_id)
        assert_edition_analysis_lineage(workspace)
        pinned_analyze_task = base_workspace.get_work_item(
            edition_state.configuration.analysis_lineage.analyze_task_id
        )
        if edition_state.configuration.curriculum_mode == "reuse":
            resumed_source_name = edition_state.configuration.curriculum_source_edition
            if resumed_source_name in {None, LEGACY_EDITION_NAME}:
                reused_curriculum_source = base_workspace
            else:
                source_state = load_edition_state(base_workspace, resumed_source_name)
                reused_curriculum_source = base_workspace.for_edition(
                    source_state.configuration.edition_id
                )
    elif sources:
        workspace, _manifest = extract_sources(
            sources,
            settings,
            workspace_path=workspace_path,
            progress=progress,
            refresh=refresh,
        )
    elif refresh:
        retained = Workspace.open(workspace_path)
        workspace, _manifest = extract_sources(
            retained.load_manifest().inputs,
            settings,
            workspace_path=workspace_path,
            progress=progress,
            refresh=True,
        )
    else:
        workspace = Workspace.open(workspace_path)
    run = workspace.create_analysis_run(settings=settings)
    configuration = _run_configuration(
        workspace,
        host=host,
        output=output,
        project=project,
        project_root=project_root,
        skill_root=skill_root,
        run_official_validation=run_official_validation,
        settings=settings,
        output_language_override=output_language_override,
        refresh=refresh,
    )
    if pinned_analyze_task is not None and reused_curriculum_source is not None:
        edition_state = assert_edition_analysis_lineage(workspace)
        selected_path = edition_state.configuration.requested_curriculum_path_id
        if selected_path is None:
            raise ProcessingError("Reused edition curriculum has no selected path")
        materialize_reused_curriculum(
            workspace,
            run,
            analyze_task=pinned_analyze_task,
            source_workspace=reused_curriculum_source,
            selected_path_id=selected_path,
        )
    if workspace.edition_id is not None:
        mark_edition_progress(workspace, status="in-progress")
    for _transition in range(100):
        workspace.ready_work_items(run.id)
        if pinned_analyze_task is None:
            analyze_tasks = workspace.list_work_items(run.id, role=WorkRole.ANALYZE)
            if not analyze_tasks:
                plan_analyze_tasks(workspace, run)
                continue
            incomplete_analyze = [
                item for item in analyze_tasks if item.state != WorkState.COMPLETE
            ]
            if incomplete_analyze:
                return _actions_required(workspace, incomplete_analyze)
            integrated = [item for item in analyze_tasks if item.scope.get("integrated") is True]
            if not integrated:
                plan_analyze_integration_task(workspace, run, analyze_tasks)
                continue
            analyze_task = integrated[-1]
        else:
            analyze_task = pinned_analyze_task

        all_author_tasks = workspace.list_work_items(run.id, role=WorkRole.AUTHOR)
        curriculum_tasks = [
            item
            for item in all_author_tasks
            if item.scope.get("kind") in {"curriculum-planning", "curriculum-reuse"}
        ]
        course_author_tasks = [
            item
            for item in all_author_tasks
            if item.scope.get("kind") in {"course-authoring", "course-authoring-repair"}
        ]
        legacy_authoring = bool(course_author_tasks) and not curriculum_tasks
        decision_dependency: list[str] = []
        if legacy_authoring:
            incomplete_authors = [
                item for item in course_author_tasks if item.state != WorkState.COMPLETE
            ]
            if incomplete_authors:
                return _actions_required(workspace, incomplete_authors)
            author_task = course_author_tasks[-1]
            if _course_requires_decision(workspace):
                decisions = [
                    item
                    for item in _matching_tasks(
                        workspace.list_work_items(run.id, role=WorkRole.DECISION),
                        scope_key="author_task_id",
                        scope_value=author_task.id,
                    )
                    if item.scope.get("kind") == "curriculum-selection"
                ]
                if not decisions:
                    _plan_decision_task(workspace, run, author_task)
                    continue
                if decisions[-1].state != WorkState.COMPLETE:
                    return _actions_required(workspace, [decisions[-1]])
                decision_dependency = [decisions[-1].id]
        else:
            if not curriculum_tasks:
                plan_curriculum_task(workspace, run, analyze_task=analyze_task)
                continue
            incomplete_curricula = [
                item for item in curriculum_tasks if item.state != WorkState.COMPLETE
            ]
            if incomplete_curricula:
                return _actions_required(workspace, incomplete_curricula)
            curriculum_task = curriculum_tasks[-1]
            curriculum_decision: WorkItem | None = None
            if _curriculum_requires_decision(workspace, curriculum_task):
                decisions = [
                    item
                    for item in _matching_tasks(
                        workspace.list_work_items(run.id, role=WorkRole.DECISION),
                        scope_key="curriculum_task_id",
                        scope_value=curriculum_task.id,
                    )
                    if item.scope.get("kind") == "curriculum-plan-selection"
                ]
                if not decisions:
                    _plan_curriculum_decision_task(workspace, run, curriculum_task)
                    continue
                curriculum_decision = decisions[-1]
                if curriculum_decision.state != WorkState.COMPLETE:
                    return _actions_required(workspace, [curriculum_decision])
            bound_course_authors = [
                item
                for item in course_author_tasks
                if item.scope.get("curriculum_task_id") == curriculum_task.id
            ]
            if course_author_tasks and not bound_course_authors:
                raise ProcessingError(
                    "Workspace mixes legacy Author state with a curriculum checkpoint"
                )
            if not bound_course_authors:
                plan_author_task(
                    workspace,
                    run,
                    analyze_task=analyze_task,
                    curriculum_task=curriculum_task,
                    decision_task=curriculum_decision,
                )
                continue
            incomplete_authors = [
                item for item in bound_course_authors if item.state != WorkState.COMPLETE
            ]
            if incomplete_authors:
                return _actions_required(workspace, incomplete_authors)
            course_author_tasks = bound_course_authors
            author_task = course_author_tasks[-1]

        repair_cycle = max(0, int(author_task.scope.get("revision", 1)) - 1)
        behavior_trials = plan_behavior_trial_tasks(
            workspace,
            run,
            author_task=author_task,
            repair_cycle=repair_cycle,
        )
        incomplete_trials = [item for item in behavior_trials if item.state != WorkState.COMPLETE]
        if incomplete_trials:
            return _actions_required(workspace, incomplete_trials)
        trial_contract = behavior_trials[0].scope
        review_contract_keys = (
            "contract_version",
            "renderer_contract_version",
            "catalog_version",
            "catalog_digest",
            "reviewed_snapshot_digest",
            "target_build_id",
            "target_content_digest",
            "artifact_language_contract_digest",
            "artifact_language_declaration_digest",
        )
        current_analysis_depth = workspace.analysis_depth_contract()
        if current_analysis_depth is None:
            raise ProcessingError("Review requires a persisted analysis-depth contract")
        current_analysis_depth_digest = stable_hash(
            current_analysis_depth.model_dump(mode="json"),
            length=64,
        )
        expected_review_dependencies = sorted(
            {
                author_task.id,
                *(item.id for item in behavior_trials),
                *decision_dependency,
            }
        )
        reviews = [
            item
            for item in _matching_tasks(
                workspace.list_work_items(run.id, role=WorkRole.REVIEW),
                scope_key="author_task_id",
                scope_value=author_task.id,
            )
            if item.scope.get("kind") == "independent-review"
            and item.scope.get("author_task_id") == author_task.id
            and item.scope.get("repair_cycle") == repair_cycle
            and all(item.scope.get(key) == trial_contract.get(key) for key in review_contract_keys)
            and item.scope.get("analysis_depth_contract_digest") == current_analysis_depth_digest
            and item.dependencies == expected_review_dependencies
        ]
        if not reviews:
            plan_review_task(
                workspace,
                run,
                author_task=author_task,
                behavior_trial_tasks=behavior_trials,
                repair_cycle=repair_cycle,
                additional_dependencies=decision_dependency,
            )
            continue
        review_task = reviews[-1]
        if review_task.state != WorkState.COMPLETE:
            return _actions_required(workspace, [review_task])
        critic_record = workspace.canonical_record("critic-report")
        if critic_record is None or critic_record.producer_task_id != review_task.id:
            raise ProcessingError("Latest Review did not publish the canonical critic report")
        critic = _load_json(workspace.root / critic_record.path)
        assert isinstance(critic, dict)
        if critic.get("verdict") == "fail":
            if repair_cycle >= MAX_REPAIR_CYCLES:
                raise ProcessingError("Maximum Author repair cycles exceeded")
            repairs = _matching_tasks(
                course_author_tasks,
                scope_key="review_task_id",
                scope_value=review_task.id,
            )
            if not repairs:
                plan_author_repair_task(
                    workspace,
                    run,
                    failed_review_task=review_task,
                    prior_author_task=author_task,
                )
                continue
            if repairs[-1].state != WorkState.COMPLETE:
                return _actions_required(workspace, [repairs[-1]])
            continue
        if critic.get("verdict") != "pass":
            raise ProcessingError("Canonical critic report has an invalid verdict")

        blueprint, receipt, build_directory = compile_workspace_blueprint(workspace)
        configured_output, configured_skill_root = _delivery_targets(
            workspace,
            configuration,
            name=blueprint.name,
        )
        delivery_conflicts = _delivery_conflicts(
            name=blueprint.name,
            build_id=receipt.build_id,
            output=configured_output,
            skill_root=configured_skill_root,
        )
        if delivery_conflicts:
            delivery_decisions = [
                item
                for item in _matching_tasks(
                    workspace.list_work_items(run.id, role=WorkRole.DECISION),
                    scope_key="conflicting_build_id",
                    scope_value=receipt.build_id,
                )
                if item.scope.get("kind") == "delivery-name-selection"
            ]
            if not delivery_decisions:
                _plan_delivery_decision_task(
                    workspace,
                    run,
                    author_task=author_task,
                    review_task=review_task,
                    build_id=receipt.build_id,
                    name=blueprint.name,
                    output=configured_output,
                    skill_root=configured_skill_root,
                    conflicts=delivery_conflicts,
                )
                continue
            if delivery_decisions[-1].state != WorkState.COMPLETE:
                return _actions_required(workspace, [delivery_decisions[-1]])
            raise ProcessingError(
                "Completed delivery decision did not resolve generated Skill conflicts"
            )
        completion_path = build_directory / "completion.json"
        result = build_workspace_skill(
            workspace,
            host=SkillHost(str(configuration["host"])),
            output=configured_output,
            project=bool(configuration["project"]),
            project_root=Path(str(configuration["project_root"])),
            skill_root=configured_skill_root,
            run_official_validation=bool(configuration["run_official_validation"]),
        )
        completion = _completion_payload(
            workspace,
            result,
            review_tasks=[
                item
                for item in workspace.list_work_items(run.id, role=WorkRole.REVIEW)
                if item.scope.get("kind") == "independent-review"
            ],
        )
        workspace.write_json(completion_path, completion)
        if workspace.edition_id is not None:
            selection_record = workspace.canonical_record("selected-curriculum")
            curriculum_record = workspace.canonical_record("curriculum")
            if selection_record is None or curriculum_record is None:
                raise ProcessingError("Edition completion requires curriculum lineage")
            curriculum = _load_json(workspace.root / curriculum_record.path)
            assert isinstance(curriculum, dict)
            mark_edition_progress(
                workspace,
                status="complete",
                resolved_artifact_language=result.artifact_language,
                selected_curriculum_path_id=str(curriculum["selected_path_id"]),
                selected_curriculum_digest=selection_record.digest,
                completion=completion,
            )
            completed_edition_state = assert_edition_analysis_lineage(workspace)
        else:
            completed_edition_state = None
        return RunEnvelope(
            status="complete",
            workspace=workspace.root,
            edition_id=(
                completed_edition_state.configuration.edition_id
                if completed_edition_state is not None
                else None
            ),
            edition_name=(
                completed_edition_state.configuration.edition_name
                if completed_edition_state is not None
                else None
            ),
            completion=completion,
        )
    raise ProcessingError("Coordinator exceeded its deterministic transition bound")
