"""Independent Review task planning, acceptance, and repair planning."""

from __future__ import annotations

import json
from pathlib import Path

from pydantic import ValidationError as PydanticValidationError

from video_to_skill.author import AUTHOR_PERSONA
from video_to_skill.errors import ProcessingError
from video_to_skill.orchestration import AuthorResult, ReviewResult
from video_to_skill.utils import atomic_write_json, stable_hash
from video_to_skill.work import AnalysisRun, WorkItem, WorkRole, WorkState
from video_to_skill.workspace import Workspace

REVIEW_PERSONA = (
    "You are a senior independent Agent Skill critic and learning-product evaluator with "
    "deep expertise in evidence grounding, semantic retention, instructional design, "
    "progressive disclosure, behavior testing, safety, and operational usability. Audit the "
    "actual canonical drafts and report concise findings without reconstructing the author's "
    "private reasoning."
)

MAX_REPAIR_CYCLES = 3

_REVIEW_RECORD_KINDS = (
    "semantic-map",
    "semantic-relations",
    "semantic-coverage",
    "course",
    "curriculum",
    "interaction",
    "capability-profile",
    "artifact-plan",
    "instructional-affordances",
    "claims",
    "assets",
)


def _review_snapshot(workspace: Workspace) -> tuple[str, dict[str, object]]:
    records: dict[str, dict[str, str]] = {}
    for kind in _REVIEW_RECORD_KINDS:
        record = workspace.canonical_record(kind)
        if record is None:
            raise ProcessingError(f"Review requires canonical {kind}")
        records[kind] = {
            "path": str(record.path),
            "digest": record.digest,
            "producer_task_id": record.producer_task_id,
        }
    artifact_plan = workspace.root / records["artifact-plan"]["path"]
    try:
        artifacts = json.loads(artifact_plan.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ProcessingError(f"Invalid canonical artifact plan: {exc}") from exc
    draft_records: dict[str, dict[str, str]] = {}
    for artifact in artifacts:
        artifact_id = str(artifact["id"])
        record = workspace.canonical_record("artifact-draft", artifact_id)
        if record is None:
            raise ProcessingError(f"Review requires canonical draft for {artifact_id}")
        draft_records[artifact_id] = {
            "path": str(record.path),
            "digest": record.digest,
            "producer_task_id": record.producer_task_id,
        }
    records["artifact-drafts"] = {
        "path": "",
        "digest": stable_hash(draft_records, length=64),
        "producer_task_id": "",
    }
    snapshot = stable_hash(
        {"records": records, "drafts": draft_records},
        length=64,
    )
    return snapshot, {"records": records, "artifact_drafts": draft_records}


def plan_review_task(
    workspace: Workspace,
    run: AnalysisRun,
    *,
    author_task: WorkItem,
    repair_cycle: int = 0,
    additional_dependencies: list[str] | None = None,
) -> WorkItem:
    if author_task.role != WorkRole.AUTHOR or author_task.state != WorkState.COMPLETE:
        raise ProcessingError("Review requires a completed Author task")
    if repair_cycle < 0 or repair_cycle > MAX_REPAIR_CYCLES:
        raise ProcessingError("Review repair cycle is outside the supported range")
    reviewed_snapshot, record_packet = _review_snapshot(workspace)
    packet = {
        "instructions": (
            "Independently audit source-meaning retention and instructional-affordance "
            "coverage, then grounding, disclosure, runtime behavior, safety, and scope. "
            "Inspect canonical files directly. Treat missing quick reference, operational "
            "validation or recovery, capstone scoring, progressive hints, retry, and teaching "
            "scaffolds as product losses when the claimed capability requires them."
        ),
        "reviewed_snapshot_digest": reviewed_snapshot,
        "canonical": record_packet,
        "author_task_id": author_task.id,
        "repair_cycle": repair_cycle,
    }
    return workspace.ensure_work_item(
        run_id=run.id,
        role=WorkRole.REVIEW,
        scope={
            "kind": "independent-review",
            "author_task_id": author_task.id,
            "reviewed_snapshot_digest": reviewed_snapshot,
            "repair_cycle": repair_cycle,
        },
        persona_hint=REVIEW_PERSONA,
        packet=packet,
        result_schema=ReviewResult.model_json_schema(mode="validation"),
        dependencies=[author_task.id, *(additional_dependencies or [])],
        snapshot_digest=run.snapshot_digest,
    )


def _load_review_result(path: Path) -> ReviewResult:
    try:
        if not path.is_file() or path.stat().st_size > 4 * 1024 * 1024:
            raise ProcessingError("Review result must be a regular JSON file no larger than 4 MiB")
        return ReviewResult.model_validate_json(path.read_text(encoding="utf-8"))
    except PydanticValidationError as exc:
        raise ProcessingError(f"Invalid Review result: {exc}") from exc
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        if isinstance(exc, ProcessingError):
            raise
        raise ProcessingError(f"Could not read Review result: {exc}") from exc


def submit_review_result(
    workspace: Workspace,
    task_id: str,
    result_path: Path,
) -> WorkItem:
    task = workspace.get_work_item(task_id)
    if task.role != WorkRole.REVIEW:
        raise ProcessingError(f"Task is not a Review task: {task_id}")
    result = _load_review_result(result_path)
    if result.task_id != task.id or result.snapshot_digest != task.snapshot_digest:
        raise ProcessingError("Review result does not belong to this task snapshot")
    expected_snapshot = str(task.scope["reviewed_snapshot_digest"])
    current_snapshot, _records = _review_snapshot(workspace)
    if (
        result.reviewed_snapshot_digest != expected_snapshot
        or current_snapshot != expected_snapshot
    ):
        raise ProcessingError("Review result targets a stale canonical authoring snapshot")
    author_task_id = str(task.scope["author_task_id"])
    author_producer = workspace.work_result_producer(author_task_id)
    if author_producer is None:
        raise ProcessingError("Review cannot identify the canonical Author producer")
    reviewer = result.producer.model_dump(mode="json")
    same_name = reviewer.get("name") == author_producer.get("name")
    same_run = (
        reviewer.get("run_id") is not None
        and reviewer.get("run_id") == author_producer.get("run_id")
    )
    if same_name or same_run:
        raise ProcessingError("Review producer must be independent of the Author producer")
    output = workspace.tasks_dir / task.id / "output"
    review_path = output / "critic-report.json"
    behavior_path = output / "behavior-report.json"
    atomic_write_json(
        review_path,
        {
            "verdict": result.verdict,
            "reviewed_snapshot_digest": result.reviewed_snapshot_digest,
            "repair_cycle": task.scope["repair_cycle"],
            "findings": [
                finding.model_dump(mode="json") for finding in result.findings
            ],
        },
    )
    atomic_write_json(
        behavior_path,
        {
            "passed": all(check.passed for check in result.behavior_checks),
            "checks": [
                check.model_dump(mode="json") for check in result.behavior_checks
            ],
        },
    )
    accepted, _canonical = workspace.accept_work_result(
        task_id=task.id,
        lease_token=result.lease_token,
        result_path=result_path,
        producer=reviewer,
        canonical_outputs=[
            ("critic-report", "default", review_path),
            ("behavior-report", "default", behavior_path),
        ],
    )
    return accepted


def plan_author_repair_task(
    workspace: Workspace,
    run: AnalysisRun,
    *,
    failed_review_task: WorkItem,
    prior_author_task: WorkItem,
) -> WorkItem:
    if failed_review_task.role != WorkRole.REVIEW or failed_review_task.state != WorkState.COMPLETE:
        raise ProcessingError("Author repair requires a completed Review task")
    review_record = workspace.canonical_record("critic-report")
    if review_record is None or review_record.producer_task_id != failed_review_task.id:
        raise ProcessingError("Author repair requires the failed canonical critic report")
    try:
        report = json.loads((workspace.root / review_record.path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ProcessingError(f"Invalid canonical critic report: {exc}") from exc
    if report.get("verdict") != "fail":
        raise ProcessingError("Passing reviews do not create Author repair tasks")
    repair_cycle = int(failed_review_task.scope["repair_cycle"]) + 1
    if repair_cycle > MAX_REPAIR_CYCLES:
        raise ProcessingError("Maximum Author repair cycles exceeded")
    if prior_author_task.result_path is None:
        raise ProcessingError("Author repair requires the prior accepted Author result")
    packet = {
        "instructions": (
            "Repair every blocking critic finding against the current canonical state. "
            "Write revised drafts only in this task's output directory and submit a complete "
            "Author result so publication remains atomic. Preserve unaffected grounded "
            "material and do not weaken capability claims merely to hide missing affordances."
        ),
        "prior_author_result": str(prior_author_task.result_path),
        "critic_report": str(review_record.path),
        "repair_cycle": repair_cycle,
    }
    return workspace.ensure_work_item(
        run_id=run.id,
        role=WorkRole.AUTHOR,
        scope={
            "kind": "course-authoring-repair",
            "revision": repair_cycle + 1,
            "repair_of": prior_author_task.id,
            "review_task_id": failed_review_task.id,
        },
        persona_hint=AUTHOR_PERSONA,
        packet=packet,
        result_schema=AuthorResult.model_json_schema(mode="validation"),
        dependencies=[failed_review_task.id],
        snapshot_digest=run.snapshot_digest,
    )
