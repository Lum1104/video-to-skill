"""Skill decomposition: split an analyzed video/course into one or multiple focused Skills."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import Field, model_validator
from pydantic import ValidationError as PydanticValidationError

from video_to_skill.errors import ProcessingError
from video_to_skill.generation import SemanticUnit
from video_to_skill.models import ObservationProducer
from video_to_skill.orchestration import OrchestrationModel
from video_to_skill.utils import atomic_write_json, hash_file, stable_hash
from video_to_skill.work import AnalysisRun, CanonicalRecord, WorkItem, WorkRole, WorkState
from video_to_skill.workspace import Workspace

DECOMPOSE_PERSONA = (
    "Skill architect. Inspect the canonical semantic map and relations, then decide "
    "whether the material belongs in one focused Skill or multiple independent Skills. "
    "Split whenever the video teaches distinct, independently triggerable capabilities "
    "or workflows; keep a single Skill when the material forms one cohesive capability."
)

_DECOMPOSE_INPUT_KINDS = (
    "semantic-map",
    "semantic-relations",
    "capability-evidence",
    "semantic-coverage",
)

_SKILL_NAME_PATTERN = r"^[a-z0-9]+(?:-[a-z0-9]+)*$"


class SkillCandidate(OrchestrationModel):
    """One focused Skill proposed by decomposition."""

    id: str = Field(min_length=1, max_length=64, pattern=_SKILL_NAME_PATTERN)
    name: str = Field(min_length=1, max_length=64, pattern=_SKILL_NAME_PATTERN)
    title: str = Field(min_length=1)
    description: str = Field(min_length=1)
    rationale: str = Field(min_length=1)
    semantic_unit_ids: list[str] = Field(min_length=1)
    prerequisites: list[str] = Field(default_factory=list)
    estimated_scope: Literal["focused", "standard", "deep"] = "standard"

    @model_validator(mode="after")
    def validate_units(self) -> SkillCandidate:
        if len(set(self.semantic_unit_ids)) != len(self.semantic_unit_ids):
            raise ValueError("SkillCandidate semantic_unit_ids must be unique")
        return self


class SkillPackPlan(OrchestrationModel):
    """Decomposition plan mapping canonical semantic units to 1..N focused Skills."""

    decomposition_mode: Literal["single", "multi"]
    rationale: str = Field(min_length=1)
    skills: list[SkillCandidate] = Field(min_length=1, max_length=8)

    @model_validator(mode="after")
    def validate_plan(self) -> SkillPackPlan:
        if self.decomposition_mode == "single" and len(self.skills) != 1:
            raise ValueError("Single-skill decomposition must declare exactly one Skill")
        if self.decomposition_mode == "multi" and len(self.skills) < 2:
            raise ValueError("Multi-skill decomposition must declare at least two Skills")
        ids = [skill.id for skill in self.skills]
        if len(set(ids)) != len(ids):
            raise ValueError("SkillCandidate ids must be unique")
        names = [skill.name for skill in self.skills]
        if len(set(names)) != len(names):
            raise ValueError("SkillCandidate names must be unique")
        return self


class SkillPackSelection(OrchestrationModel):
    """Confirmed subset of decomposed Skills to author, review, and install."""

    selected_skill_ids: list[str] = Field(min_length=1)
    source: Literal["auto", "user", "override"]
    plan_digest: str = Field(pattern=r"^[a-f0-9]{64}$")

    @model_validator(mode="after")
    def validate_selected(self) -> SkillPackSelection:
        if len(set(self.selected_skill_ids)) != len(self.selected_skill_ids):
            raise ValueError("selected_skill_ids must be unique")
        return self


class SkillDecompositionResult(OrchestrationModel):
    """Typed result submitted by the Skill Decomposition subagent."""

    task_id: str = Field(min_length=1)
    lease_token: str = Field(min_length=1)
    snapshot_digest: str = Field(min_length=1)
    producer: ObservationProducer
    plan: SkillPackPlan


@dataclass(frozen=True)
class CanonicalSkillPackCheckpoint:
    plan: SkillPackPlan
    plan_record: CanonicalRecord
    selection: SkillPackSelection | None
    selection_record: CanonicalRecord | None


def _canonical_path(workspace: Workspace, kind: str) -> tuple[str, str]:
    record = workspace.canonical_record(kind)
    if record is None:
        raise ProcessingError(f"Skill decomposition requires canonical {kind}")
    return str(record.path), record.digest


def _load_semantic_units(workspace: Workspace) -> list[SemanticUnit]:
    record = workspace.canonical_record("semantic-map")
    if record is None:
        raise ProcessingError("Skill decomposition requires a canonical semantic map")
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


def _verified_json(workspace: Workspace, record: CanonicalRecord, *, label: str) -> object:
    if record.path.is_absolute() or ".." in record.path.parts:
        raise ProcessingError(f"Canonical {label} path is unsafe")
    path = workspace.root / record.path
    try:
        if not path.is_file() or hash_file(path) != record.digest:
            raise ProcessingError(f"Canonical {label} failed its digest check")
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        if isinstance(exc, ProcessingError):
            raise
        raise ProcessingError(f"Invalid canonical {label}: {exc}") from exc


def plan_decompose_task(
    workspace: Workspace,
    run: AnalysisRun,
    *,
    analyze_task: WorkItem,
) -> WorkItem:
    """Create or return the bounded Skill decomposition task after Analyze completes."""

    if analyze_task.role != WorkRole.ANALYZE or analyze_task.state != WorkState.COMPLETE:
        raise ProcessingError("Skill decomposition requires a completed Analyze task")
    if analyze_task.result_digest is None:
        raise ProcessingError("Completed Analyze task is missing its result digest")

    canonical_inputs: dict[str, dict[str, str]] = {}
    for kind in _DECOMPOSE_INPUT_KINDS:
        path_text, digest = _canonical_path(workspace, kind)
        canonical_inputs[kind] = {"path": path_text, "digest": digest}

    sources = workspace.list_sources()
    total_duration = sum((source.duration or 0.0) for source in sources)
    units = _load_semantic_units(workspace)

    scope = {
        "kind": "skill-decomposition",
        "analyze_task_id": analyze_task.id,
        "analyze_result_digest": analyze_task.result_digest,
        "semantic_map_digest": canonical_inputs["semantic-map"]["digest"],
    }
    packet = {
        "instructions": [
            "Read the canonical semantic map, relations, and capability evidence.",
            "Decide whether this source material should produce one cohesive Skill "
            "(decomposition_mode='single') or multiple focused, independently triggerable "
            "Skills (decomposition_mode='multi').",
            "Split long or multi-topic videos into distinct Skills when they cover separate "
            "workflows, tools, or mental models that a user would invoke independently. "
            "Each Skill should be crisp, self-contained, and have a clear kebab-case name.",
            "Do not over-fragment tightly coupled steps of a single procedure into trivial Skills.",
            "Every semantic_unit_id from the canonical semantic map must appear in at least one "
            "SkillCandidate (shared foundational units may appear in more than one Skill).",
            "Write a JSON file matching result_schema.json and submit it with "
            "`video-to-skill submit --workspace <workspace> --task <task-id> --result <path>`.",
        ],
        "source_summary": {
            "source_count": len(sources),
            "total_duration_seconds": round(total_duration, 1),
            "semantic_unit_count": len(units),
            "titles": [source.title for source in sources if source.title],
        },
        "canonical": canonical_inputs,
    }
    return workspace.ensure_work_item(
        run_id=run.id,
        role=WorkRole.AUTHOR,
        scope=scope,
        persona_hint=DECOMPOSE_PERSONA,
        packet=packet,
        result_schema=SkillDecompositionResult.model_json_schema(),
        snapshot_digest=run.snapshot_digest,
        dependencies=[analyze_task.id],
    )


def submit_decompose_result(
    workspace: Workspace,
    task_id: str,
    result_path: Path,
) -> WorkItem:
    """Validate and publish a Skill decomposition plan (auto-selecting when single-skill)."""

    task = workspace.get_work_item(task_id)
    if task.role != WorkRole.AUTHOR or task.scope.get("kind") != "skill-decomposition":
        raise ProcessingError(f"Task {task_id} is not a Skill decomposition task")
    try:
        raw = json.loads(result_path.read_text(encoding="utf-8"))
        result = SkillDecompositionResult.model_validate(raw)
    except (OSError, ValueError, PydanticValidationError) as exc:
        raise ProcessingError(f"Invalid Skill decomposition result: {exc}") from exc

    if result.task_id != task.id:
        raise ProcessingError("Skill decomposition result task_id does not match task")
    if result.snapshot_digest != task.snapshot_digest:
        raise ProcessingError("Skill decomposition result snapshot_digest does not match task")

    units = _load_semantic_units(workspace)
    units_by_id = {unit.id: unit for unit in units}
    known_unit_ids = set(units_by_id)
    covered_unit_ids: set[str] = set()
    for skill in result.plan.skills:
        unknown = [uid for uid in skill.semantic_unit_ids if uid not in known_unit_ids]
        if unknown:
            raise ProcessingError(
                f"Skill '{skill.id}' references unknown semantic unit ids: {', '.join(unknown)}"
            )
        skill_unit_set = set(skill.semantic_unit_ids)
        for uid in skill.semantic_unit_ids:
            merged_target = units_by_id[uid].merged_into
            if merged_target is not None and merged_target not in skill_unit_set:
                raise ProcessingError(
                    f"Skill '{skill.id}' includes merged unit '{uid}' without its "
                    f"merged_into target '{merged_target}'"
                )
        covered_unit_ids.update(skill.semantic_unit_ids)
    missing = sorted(known_unit_ids - covered_unit_ids)
    if missing:
        raise ProcessingError(
            f"Skill decomposition omitted canonical semantic units: {', '.join(missing)}"
        )

    output_dir = workspace.tasks_dir / task.id / "output"
    plan_path = output_dir / "skill-pack-plan.json"
    atomic_write_json(plan_path, result.plan.model_dump(mode="json"))
    plan_digest = hash_file(plan_path)

    canonical_outputs: list[tuple[str, str, Path]] = [
        ("skill-pack-plan", "default", plan_path),
    ]
    if len(result.plan.skills) == 1:
        selection = SkillPackSelection(
            selected_skill_ids=[result.plan.skills[0].id],
            source="auto",
            plan_digest=plan_digest,
        )
        selection_path = output_dir / "selected-skill-pack.json"
        atomic_write_json(selection_path, selection.model_dump(mode="json"))
        canonical_outputs.append(("selected-skill-pack", "default", selection_path))

    accepted, _records = workspace.accept_work_result(
        task_id=task.id,
        lease_token=result.lease_token,
        result_path=result_path,
        producer=result.producer.model_dump(mode="json"),
        canonical_outputs=canonical_outputs,
    )
    return accepted


def load_canonical_skill_pack_plan(workspace: Workspace) -> tuple[SkillPackPlan, CanonicalRecord]:
    """Load and verify the canonical SkillPackPlan."""

    record = workspace.canonical_record("skill-pack-plan")
    if record is None:
        raise ProcessingError("Workspace does not have a canonical skill-pack-plan")
    raw = _verified_json(workspace, record, label="skill-pack-plan")
    try:
        plan = SkillPackPlan.model_validate(raw)
    except PydanticValidationError as exc:
        raise ProcessingError(f"Invalid canonical skill-pack-plan: {exc}") from exc
    return plan, record


def load_canonical_skill_pack_checkpoint(workspace: Workspace) -> CanonicalSkillPackCheckpoint:
    """Load the canonical SkillPackPlan and optional SkillPackSelection."""

    plan, plan_record = load_canonical_skill_pack_plan(workspace)
    selection_record = workspace.canonical_record("selected-skill-pack")
    selection: SkillPackSelection | None = None
    if selection_record is not None:
        raw = _verified_json(workspace, selection_record, label="selected-skill-pack")
        try:
            selection = SkillPackSelection.model_validate(raw)
        except PydanticValidationError as exc:
            raise ProcessingError(f"Invalid canonical selected-skill-pack: {exc}") from exc
        if selection.plan_digest != plan_record.digest:
            raise ProcessingError(
                "Canonical selected-skill-pack does not match the current skill-pack-plan"
            )
        known_ids = {skill.id for skill in plan.skills}
        unknown = [sid for sid in selection.selected_skill_ids if sid not in known_ids]
        if unknown:
            raise ProcessingError(
                f"Selected skill ids are not in the skill-pack-plan: {', '.join(unknown)}"
            )
    return CanonicalSkillPackCheckpoint(
        plan=plan,
        plan_record=plan_record,
        selection=selection,
        selection_record=selection_record,
    )


def apply_skill_pack_selection(
    workspace: Workspace,
    *,
    plan_task_id: str,
    selected_skill_ids: list[str],
    source: Literal["auto", "user", "override"],
) -> CanonicalRecord:
    """Publish a validated SkillPackSelection canonical record."""

    plan, plan_record = load_canonical_skill_pack_plan(workspace)
    known_ids = {skill.id for skill in plan.skills}
    if not selected_skill_ids:
        raise ProcessingError("At least one Skill must be selected")
    unknown = [sid for sid in selected_skill_ids if sid not in known_ids]
    if unknown:
        raise ProcessingError(f"Unknown skill id(s): {', '.join(unknown)}")
    selection = SkillPackSelection(
        selected_skill_ids=selected_skill_ids,
        source=source,
        plan_digest=plan_record.digest,
    )
    output_dir = workspace.tasks_dir / plan_task_id / "output"
    output_dir.mkdir(parents=True, exist_ok=True)
    digest_suffix = stable_hash(selection.model_dump(mode="json"), length=12)
    selection_path = output_dir / f"selected-skill-pack-{digest_suffix}.json"
    atomic_write_json(selection_path, selection.model_dump(mode="json"))
    return workspace.publish_canonical_record(
        kind="selected-skill-pack",
        record_id="default",
        source_path=selection_path,
        producer_task_id=plan_task_id,
        snapshot_digest=plan_record.snapshot_digest,
    )


def selected_skill_candidates(workspace: Workspace) -> list[SkillCandidate]:
    """Return the ordered SkillCandidates selected for authoring and installation."""

    checkpoint = load_canonical_skill_pack_checkpoint(workspace)
    if checkpoint.selection is None:
        raise ProcessingError("Skill pack selection has not been confirmed yet")
    by_id = {skill.id: skill for skill in checkpoint.plan.skills}
    return [by_id[skill_id] for skill_id in checkpoint.selection.selected_skill_ids]


def scoped_skill_candidate(workspace: Workspace, skill_id: str | None) -> SkillCandidate | None:
    """Resolve one SkillCandidate by id from the canonical skill-pack-plan."""

    if skill_id is None:
        return None
    plan, _record = load_canonical_skill_pack_plan(workspace)
    for skill in plan.skills:
        if skill.id == skill_id:
            return skill
    raise ProcessingError(f"Skill '{skill_id}' was not found in canonical skill-pack-plan")
