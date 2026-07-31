"""Workspace-centered Author task planning and result acceptance."""

from __future__ import annotations

import json
from pathlib import Path

from pydantic import ValidationError as PydanticValidationError

from video_to_skill.errors import ProcessingError
from video_to_skill.generation import CapabilityLevel, SemanticUnit, SkillMode
from video_to_skill.orchestration import (
    AFFORDANCE_CATALOG,
    AuthorResult,
    CapabilityEvidence,
)
from video_to_skill.utils import atomic_write_json, hash_file
from video_to_skill.work import AnalysisRun, WorkItem, WorkRole
from video_to_skill.workspace import Workspace

AUTHOR_PERSONA = (
    "You are a principal learning-science architect and Agent Skill author with deep "
    "experience turning grounded knowledge graphs into adaptive instruction, deliberate "
    "practice, operational playbooks, feedback systems, and fast reference tools. Preserve "
    "instructional usefulness without inventing source support."
)

_LEVEL_RANK: dict[CapabilityLevel, int] = {
    "unsupported": 0,
    "light": 1,
    "medium": 2,
    "strong": 3,
}


def _canonical_path(workspace: Workspace, kind: str) -> tuple[str, str]:
    record = workspace.canonical_record(kind)
    if record is None:
        raise ProcessingError(f"Authoring requires canonical {kind}")
    return str(record.path), record.digest


def plan_author_task(
    workspace: Workspace,
    run: AnalysisRun,
    *,
    analyze_task: WorkItem,
) -> WorkItem:
    if analyze_task.role != WorkRole.ANALYZE or analyze_task.state.value != "complete":
        raise ProcessingError("Authoring requires a completed integrated Analyze task")
    semantic_map = workspace.canonical_record("semantic-map")
    if semantic_map is None or semantic_map.producer_task_id != analyze_task.id:
        raise ProcessingError("Authoring requires the integrated canonical semantic map")
    records = {
        kind: _canonical_path(workspace, kind)
        for kind in (
            "semantic-map",
            "semantic-relations",
            "capability-evidence",
            "semantic-coverage",
            "semantic-conflicts",
        )
    }
    packet = {
        "instructions": (
            "Design a thematic default curriculum and materially different alternate paths "
            "when justified. Write every Markdown artifact directly under the task output "
            "directory. Complete the full instructional-affordance ledger without creating "
            "one artifact per behavior. Keep solutions and answer-bearing rubrics separate "
            "and after-attempt. Do not exceed Analyze capability ceilings."
        ),
        "canonical_records": {
            kind: {"path": path, "digest": digest}
            for kind, (path, digest) in records.items()
        },
        "affordance_catalog": AFFORDANCE_CATALOG,
    }
    return workspace.ensure_work_item(
        run_id=run.id,
        role=WorkRole.AUTHOR,
        scope={"kind": "course-authoring", "revision": 1},
        persona_hint=AUTHOR_PERSONA,
        packet=packet,
        result_schema=AuthorResult.model_json_schema(mode="validation"),
        dependencies=[analyze_task.id],
        snapshot_digest=run.snapshot_digest,
    )


def _load_author_result(path: Path) -> AuthorResult:
    try:
        if not path.is_file() or path.stat().st_size > 8 * 1024 * 1024:
            raise ProcessingError("Author result must be a regular JSON file no larger than 8 MiB")
        return AuthorResult.model_validate_json(path.read_text(encoding="utf-8"))
    except PydanticValidationError as exc:
        raise ProcessingError(f"Invalid Author result: {exc}") from exc
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        if isinstance(exc, ProcessingError):
            raise
        raise ProcessingError(f"Could not read Author result: {exc}") from exc


def _load_semantic_units(workspace: Workspace) -> list[SemanticUnit]:
    record = workspace.canonical_record("semantic-map")
    if record is None:
        raise ProcessingError("Author submission requires a canonical semantic map")
    try:
        raw = json.loads((workspace.root / record.path).read_text(encoding="utf-8"))
        return [SemanticUnit.model_validate(item) for item in raw]
    except (OSError, ValueError, PydanticValidationError) as exc:
        raise ProcessingError(f"Invalid canonical semantic map: {exc}") from exc


def _load_capability_evidence(workspace: Workspace) -> list[CapabilityEvidence]:
    record = workspace.canonical_record("capability-evidence")
    if record is None:
        raise ProcessingError("Author submission requires canonical capability evidence")
    try:
        raw = json.loads((workspace.root / record.path).read_text(encoding="utf-8"))
        return [CapabilityEvidence.model_validate(item) for item in raw]
    except (OSError, ValueError, PydanticValidationError) as exc:
        raise ProcessingError(f"Invalid canonical capability evidence: {exc}") from exc


def _validate_author_result(
    workspace: Workspace,
    task: WorkItem,
    result: AuthorResult,
) -> None:
    units = _load_semantic_units(workspace)
    known_units = {unit.id for unit in units}
    units_by_id = {unit.id: unit for unit in units}
    sources = {source.id: source for source in workspace.list_sources()}
    ceilings = {item.mode: item.ceiling for item in _load_capability_evidence(workspace)}
    levels: dict[SkillMode, CapabilityLevel] = {
        "learn": result.capability_profile.learn,
        "practice": result.capability_profile.practice,
        "apply": result.capability_profile.apply,
        "reference": result.capability_profile.reference,
    }
    for mode, level in levels.items():
        if _LEVEL_RANK[level] > _LEVEL_RANK[ceilings[mode]]:
            raise ProcessingError(
                f"Author {mode} capability exceeds the Analyze evidence ceiling"
            )
    represented: set[str] = set()
    task_output = (workspace.tasks_dir / task.id / "output").resolve()
    for artifact in result.artifacts:
        if not set(artifact.semantic_unit_ids) <= known_units:
            raise ProcessingError(f"Artifact {artifact.id} references unknown semantic units")
        represented.update(artifact.semantic_unit_ids)
        draft = (task_output / artifact.draft_path).resolve()
        if (
            not draft.is_file()
            or draft.is_symlink()
            or not draft.is_relative_to(task_output)
        ):
            raise ProcessingError(f"Artifact {artifact.id} draft is outside its task output")
        if hash_file(draft) != artifact.draft_sha256:
            raise ProcessingError(f"Artifact {artifact.id} draft digest does not match")
    required_units = {
        unit.id
        for unit in units
        if unit.materiality in {"core", "supporting"}
        and unit.disposition == "included"
    }
    if missing := required_units - represented:
        raise ProcessingError(
            "Included material semantic units lack an authored artifact: "
            + ", ".join(sorted(missing))
        )
    for affordance in result.affordance_ledger:
        if not set(affordance.semantic_unit_ids) <= known_units:
            raise ProcessingError(
                f"Instructional affordance {affordance.id} references unknown semantic units"
            )
    artifact_paths = {artifact.path for artifact in result.artifacts}
    for claim in result.claims:
        if not set(claim.semantic_unit_ids) <= known_units:
            raise ProcessingError(f"Claim {claim.id} references unknown semantic units")
        if claim.file not in {"SKILL.md", "source-map.md", "sources.md"} | artifact_paths:
            raise ProcessingError(f"Claim {claim.id} references an unauthored file")
        referenced_units = [units_by_id[unit_id] for unit_id in claim.semantic_unit_ids]
        allowed_evidence_by_source = {
            source_id: {
                evidence_id
                for unit in referenced_units
                if unit.source_id == source_id
                for evidence_id in unit.evidence_ids
            }
            for source_id in {unit.source_id for unit in referenced_units}
        }
        for evidence in claim.evidence:
            if evidence.source_id not in allowed_evidence_by_source:
                raise ProcessingError(
                    f"Claim {claim.id} evidence references an unrelated source"
                )
            if not set(evidence.evidence_ids) <= allowed_evidence_by_source[evidence.source_id]:
                raise ProcessingError(
                    f"Claim {claim.id} references evidence outside its semantic units"
                )
            source = sources[evidence.source_id]
            if source.duration is not None and evidence.end > source.duration:
                raise ProcessingError(
                    f"Claim {claim.id} evidence extends beyond source duration"
                )


def submit_author_result(
    workspace: Workspace,
    task_id: str,
    result_path: Path,
) -> WorkItem:
    task = workspace.get_work_item(task_id)
    if task.role != WorkRole.AUTHOR:
        raise ProcessingError(f"Task is not an Author task: {task_id}")
    result = _load_author_result(result_path)
    if result.task_id != task_id or result.snapshot_digest != task.snapshot_digest:
        raise ProcessingError("Author result does not belong to this task snapshot")
    _validate_author_result(workspace, task, result)
    output = workspace.tasks_dir / task_id / "output"
    course_path = output / "course.json"
    curriculum_path = output / "curriculum.json"
    interaction_path = output / "interaction.json"
    capability_path = output / "capability-profile.json"
    artifact_plan_path = output / "artifact-plan.json"
    affordance_path = output / "instructional-affordances.json"
    claims_path = output / "claims.json"
    assets_path = output / "assets.json"
    atomic_write_json(
        course_path,
        {
            "name": result.name,
            "title": result.title,
            "description": result.description,
            "scope": result.scope,
            "artifact_language": result.artifact_language,
            "prerequisites": result.prerequisites,
            "core_principles": [
                item.model_dump(mode="json") for item in result.core_principles
            ],
            "limitations": result.limitations,
            "curriculum_decision_required": result.curriculum_decision_required,
            "curriculum_decision_summary": result.curriculum_decision_summary,
        },
    )
    atomic_write_json(curriculum_path, result.curriculum)
    atomic_write_json(interaction_path, result.interaction)
    atomic_write_json(capability_path, result.capability_profile)
    atomic_write_json(
        artifact_plan_path,
        [item.model_dump(mode="json") for item in result.artifacts],
    )
    atomic_write_json(
        affordance_path,
        [item.model_dump(mode="json") for item in result.affordance_ledger],
    )
    atomic_write_json(
        claims_path,
        [item.model_dump(mode="json") for item in result.claims],
    )
    atomic_write_json(
        assets_path,
        [item.model_dump(mode="json") for item in result.assets],
    )
    canonical_outputs: list[tuple[str, str, Path]] = [
        ("course", "default", course_path),
        ("curriculum", "default", curriculum_path),
        ("interaction", "default", interaction_path),
        ("capability-profile", "default", capability_path),
        ("artifact-plan", "default", artifact_plan_path),
        ("instructional-affordances", "default", affordance_path),
        ("claims", "default", claims_path),
        ("assets", "default", assets_path),
    ]
    for artifact in result.artifacts:
        canonical_outputs.append(
            ("artifact-draft", artifact.id, output / artifact.draft_path)
        )
    accepted, _records = workspace.accept_work_result(
        task_id=task_id,
        lease_token=result.lease_token,
        result_path=result_path,
        producer=result.producer.model_dump(mode="json"),
        canonical_outputs=canonical_outputs,
    )
    return accepted
