"""Quiescence-driven orchestration over durable workspace tasks."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

from pydantic import ValidationError as PydanticValidationError

from video_to_skill.analyze import (
    plan_analyze_integration_task,
    plan_analyze_tasks,
    submit_analyze_result,
)
from video_to_skill.author import plan_author_task, submit_author_result
from video_to_skill.compiler import (
    WorkspaceBuildResult,
    build_workspace_skill,
    compile_workspace_blueprint,
)
from video_to_skill.config import Settings
from video_to_skill.errors import ProcessingError
from video_to_skill.generation import CurriculumDesign
from video_to_skill.installation import SkillHost
from video_to_skill.orchestration import (
    DecisionResult,
    RunAction,
    RunEnvelope,
    SubmissionReceipt,
)
from video_to_skill.pipeline import extract_sources
from video_to_skill.review import (
    MAX_REPAIR_CYCLES,
    plan_author_repair_task,
    plan_review_task,
    submit_review_result,
)
from video_to_skill.utils import atomic_write_json
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
) -> dict[str, object]:
    path = workspace.analysis_dir / "run-config.json"
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
        return existing
    if host is None:
        raise ProcessingError("A new orchestration run requires --host")
    resolved_output = (
        output.expanduser().resolve()
        if output is not None
        else (Path.cwd() / "generated-skills" / "pending-course").resolve()
    )
    configuration: dict[str, object] = {
        "host": host.value,
        "output": str(resolved_output),
        "output_is_default": output is None,
        "project": bool(project),
        "project_root": str((project_root or Path.cwd()).resolve()),
        "skill_root": str(skill_root.resolve()) if skill_root is not None else None,
        "run_official_validation": run_official_validation,
    }
    atomic_write_json(path, configuration)
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
    return RunEnvelope(
        status="actions-required",
        workspace=workspace.root,
        actions=[_task_action(workspace, item) for item in dispatchable],
    )


def _course_requires_decision(workspace: Workspace) -> bool:
    record = workspace.canonical_record("course")
    if record is None:
        return False
    course = _load_json(workspace.root / record.path)
    return isinstance(course, dict) and bool(course.get("curriculum_decision_required"))


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
    curriculum_record = workspace.canonical_record("curriculum")
    if curriculum_record is None:
        raise ProcessingError("User decision requires canonical curriculum state")
    curriculum = CurriculumDesign.model_validate(
        _load_json(workspace.root / curriculum_record.path)
    )
    if result.selected_path_id not in {path.id for path in curriculum.paths}:
        raise ProcessingError("User selected an unknown curriculum path")
    selected = curriculum.model_copy(update={"selected_path_id": result.selected_path_id})
    output = workspace.tasks_dir / task.id / "output" / "curriculum.json"
    atomic_write_json(output, selected)
    accepted, _records = workspace.accept_work_result(
        task_id=task.id,
        lease_token=result.lease_token,
        result_path=result_path,
        producer=result.producer.model_dump(mode="json"),
        canonical_outputs=[("curriculum", "default", output)],
    )
    return accepted


def submit_workspace_result(
    workspace: Workspace,
    task_id: str,
    result_path: Path,
) -> SubmissionReceipt:
    item = workspace.get_work_item(task_id)
    submitted_result = result_path.absolute()
    resolved_result = result_path.resolve()
    task_output = (workspace.tasks_dir / task_id / "output").resolve()
    if (
        not resolved_result.is_file()
        or submitted_result.is_symlink()
        or not resolved_result.is_relative_to(task_output)
        or resolved_result.stat().st_size > 8 * 1024 * 1024
    ):
        raise ProcessingError(
            "Submitted result must be a regular task-output JSON file no larger than 8 MiB"
        )
    if item.role == WorkRole.ANALYZE:
        accepted = submit_analyze_result(workspace, task_id, resolved_result)
    elif item.role == WorkRole.AUTHOR:
        accepted = submit_author_result(workspace, task_id, resolved_result)
    elif item.role == WorkRole.REVIEW:
        accepted = submit_review_result(workspace, task_id, resolved_result)
    else:
        accepted = submit_decision_result(workspace, task_id, resolved_result)
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
    assert isinstance(affordances, list)
    affordance_summary = {
        status: sum(isinstance(item, dict) and item.get("status") == status for item in affordances)
        for status in ("provided", "unsupported", "not-applicable")
    }
    prefix = "/" if result.host == SkillHost.CLAUDE else "$"
    return {
        **result.model_dump(mode="json"),
        "workspace": str(workspace.root),
        "workspace_retained": True,
        "processed_sources": len(workspace.list_sources()),
        "failed_sources": len(manifest.failed_sources),
        "semantic_coverage": semantic_coverage,
        "instructional_affordance_coverage": affordance_summary,
        "critic_repairs": max(0, len(review_tasks) - 1),
        "invocation": f"{prefix}{result.name}",
    }


def advance_run(
    *,
    sources: list[str],
    workspace_path: Path,
    settings: Settings,
    host: SkillHost | None,
    output: Path | None = None,
    project: bool | None = None,
    project_root: Path | None = None,
    skill_root: Path | None = None,
    refresh: bool = False,
    run_official_validation: bool = True,
    progress: ProgressCallback = lambda _message: None,
) -> RunEnvelope:
    if sources:
        workspace, _manifest = extract_sources(
            sources,
            settings,
            workspace_path=workspace_path,
            progress=progress,
            refresh=refresh,
        )
    else:
        workspace = Workspace.open(workspace_path)
    configuration = _run_configuration(
        workspace,
        host=host,
        output=output,
        project=project,
        project_root=project_root,
        skill_root=skill_root,
        run_official_validation=run_official_validation,
    )
    run = workspace.create_analysis_run()
    for _transition in range(100):
        workspace.ready_work_items(run.id)
        analyze_tasks = workspace.list_work_items(run.id, role=WorkRole.ANALYZE)
        if not analyze_tasks:
            plan_analyze_tasks(workspace, run)
            continue
        incomplete_analyze = [item for item in analyze_tasks if item.state != WorkState.COMPLETE]
        if incomplete_analyze:
            return _actions_required(workspace, incomplete_analyze)
        integrated = [item for item in analyze_tasks if item.scope.get("integrated") is True]
        if not integrated:
            plan_analyze_integration_task(workspace, run, analyze_tasks)
            continue
        analyze_task = integrated[-1]

        author_tasks = workspace.list_work_items(run.id, role=WorkRole.AUTHOR)
        if not author_tasks:
            plan_author_task(workspace, run, analyze_task=analyze_task)
            continue
        incomplete_authors = [item for item in author_tasks if item.state != WorkState.COMPLETE]
        if incomplete_authors:
            return _actions_required(workspace, incomplete_authors)
        author_task = author_tasks[-1]

        decision_dependency: list[str] = []
        if _course_requires_decision(workspace):
            decisions = _matching_tasks(
                workspace.list_work_items(run.id, role=WorkRole.DECISION),
                scope_key="author_task_id",
                scope_value=author_task.id,
            )
            if not decisions:
                _plan_decision_task(workspace, run, author_task)
                continue
            if decisions[-1].state != WorkState.COMPLETE:
                return _actions_required(workspace, [decisions[-1]])
            decision_dependency = [decisions[-1].id]

        reviews = _matching_tasks(
            workspace.list_work_items(run.id, role=WorkRole.REVIEW),
            scope_key="author_task_id",
            scope_value=author_task.id,
        )
        repair_cycle = max(0, int(author_task.scope.get("revision", 1)) - 1)
        if not reviews:
            plan_review_task(
                workspace,
                run,
                author_task=author_task,
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
                author_tasks,
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

        blueprint, _receipt, build_directory = compile_workspace_blueprint(workspace)
        completion_path = build_directory / "completion.json"
        if completion_path.exists():
            completion = _load_json(completion_path)
            assert isinstance(completion, dict)
            return RunEnvelope(
                status="complete",
                workspace=workspace.root,
                completion=completion,
            )
        configured_output = Path(str(configuration["output"]))
        if bool(configuration["output_is_default"]):
            configured_output = configured_output.with_name(blueprint.name)
        configured_skill_root = (
            Path(str(configuration["skill_root"])) if configuration.get("skill_root") else None
        )
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
            review_tasks=workspace.list_work_items(run.id, role=WorkRole.REVIEW),
        )
        atomic_write_json(completion_path, completion)
        return RunEnvelope(
            status="complete",
            workspace=workspace.root,
            completion=completion,
        )
    raise ProcessingError("Coordinator exceeded its deterministic transition bound")
