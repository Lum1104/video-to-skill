"""Workspace-centered Author task planning and result acceptance."""

from __future__ import annotations

import json
import posixpath
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from pydantic import ValidationError as PydanticValidationError

from video_to_skill.artifact_language import (
    ArtifactLanguageDeclaration,
    canonical_artifact_language_state,
    declare_artifact_language,
    ensure_artifact_language_contract,
    load_artifact_language_contract,
    load_artifact_language_declaration,
)
from video_to_skill.curriculum import (
    load_canonical_curriculum_plan,
    load_canonical_curriculum_selection,
)
from video_to_skill.editions import validate_edition_author_identity
from video_to_skill.errors import ProcessingError
from video_to_skill.generation import CapabilityLevel, CourseAsset, SemanticUnit, SkillMode
from video_to_skill.orchestration import (
    AFFORDANCE_CATALOG,
    AuthorResult,
    CapabilityEvidence,
)
from video_to_skill.visual_assets import (
    MaterializedVisualAsset,
    canonical_visual_asset_candidates,
    visual_asset_candidate_packet,
)
from video_to_skill.work import AnalysisRun, WorkItem, WorkRole, WorkState
from video_to_skill.workspace import CanonicalOutput, CanonicalOutputSnapshot, Workspace

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

_MARKDOWN_TARGET_RE = re.compile(r"!?\[[^\]]*\]\((?P<target>[^)\s]+)(?:\s+[^)]*)?\)")
MAX_AUTHOR_DRAFT_BYTES = 512 * 1024


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
    curriculum_task: WorkItem,
    decision_task: WorkItem | None = None,
) -> WorkItem:
    if analyze_task.role != WorkRole.ANALYZE or analyze_task.state.value != "complete":
        raise ProcessingError("Authoring requires a completed integrated Analyze task")
    semantic_map = workspace.canonical_record("semantic-map")
    if semantic_map is None or semantic_map.producer_task_id != analyze_task.id:
        raise ProcessingError("Authoring requires the integrated canonical semantic map")
    if (
        curriculum_task.role != WorkRole.AUTHOR
        or curriculum_task.scope.get("kind") not in {"curriculum-planning", "curriculum-reuse"}
        or curriculum_task.state != WorkState.COMPLETE
    ):
        raise ProcessingError("Authoring requires a completed curriculum-planning task")
    curriculum_plan, curriculum_plan_record = load_canonical_curriculum_plan(
        workspace,
        expected_producer_task_id=curriculum_task.id,
    )
    reused_curriculum = curriculum_task.scope.get("kind") == "curriculum-reuse"
    expected_selection_producer = curriculum_task.id
    dependencies = [analyze_task.id, curriculum_task.id]
    if curriculum_plan.decision_required and not reused_curriculum:
        if (
            decision_task is None
            or decision_task.role != WorkRole.DECISION
            or decision_task.scope.get("kind") != "curriculum-plan-selection"
            or decision_task.scope.get("curriculum_task_id") != curriculum_task.id
            or decision_task.state != WorkState.COMPLETE
        ):
            raise ProcessingError("Authoring requires the completed material curriculum decision")
        expected_selection_producer = decision_task.id
        dependencies.append(decision_task.id)
    elif decision_task is not None:
        raise ProcessingError("A curriculum decision task is not valid for an automatic selection")
    curriculum_selection, selection_record = load_canonical_curriculum_selection(
        workspace,
        expected_producer_task_id=expected_selection_producer,
    )
    if curriculum_selection.curriculum_plan_digest != curriculum_plan_record.digest:
        raise ProcessingError("Selected curriculum does not belong to the canonical options")
    if curriculum_selection.selected_path_id not in {path.id for path in curriculum_plan.paths}:
        raise ProcessingError("Selected curriculum identifies an unknown path")
    language_contract, language_contract_digest = load_artifact_language_contract(workspace)
    language_declaration, language_declaration_digest = load_artifact_language_declaration(
        workspace,
        expected_contract_digest=language_contract_digest,
    )
    language_record = workspace.canonical_record("artifact-language-declaration")
    if language_record is None or language_record.producer_task_id != curriculum_task.id:
        raise ProcessingError(
            "Authoring requires the curriculum's canonical artifact-language declaration"
        )
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
    records["curriculum-options"] = (
        str(curriculum_plan_record.path),
        curriculum_plan_record.digest,
    )
    records["selected-curriculum"] = (str(selection_record.path), selection_record.digest)
    edition_instruction = ""
    if workspace.edition_id is not None:
        from video_to_skill.editions import assert_edition_analysis_lineage

        edition_state = assert_edition_analysis_lineage(workspace)
        requested_name = edition_state.configuration.skill_name
        stable_ids = edition_state.configuration.identity_baseline
        if requested_name is not None:
            edition_instruction += f" Use exactly {requested_name!r} as the Skill name."
        if stable_ids.established:
            edition_instruction += (
                " This is a language-only edition: preserve the logical artifact and claim IDs, "
                "their semantic-unit bindings, and every evidence timestamp exactly as supplied "
                "by the edition identity baseline."
            )
        else:
            edition_instruction += (
                " Establish stable logical artifact and claim IDs for this edition's durable "
                "identity family; later editions and repairs must preserve them exactly."
            )
    packet = {
        "instructions": (
            "Consume the selected canonical curriculum without redesigning or reopening it. "
            "Bind every planned path to justified artifacts and write every Markdown draft "
            "directly under the task output directory. Complete the full instructional-"
            "affordance ledger without creating one artifact per behavior. Keep solutions and "
            "answer-bearing rubrics separate and after-attempt. Do not exceed Analyze capability "
            "ceilings. Select only evidence-grounded visual_asset_candidates when a visual "
            "materially improves teaching or verification. Link every selected PNG from each "
            "used_by artifact, and leave decorative or redundant candidates unused. Curriculum "
            "decision fields are legacy-only and must remain false and null. Use exactly "
            f"{language_declaration.artifact_language!r} for all generated artifact prose, "
            "titles, interaction text, and metadata. Preserve original proper names and "
            "technical terms where appropriate, and echo this exact value in artifact_language."
            + edition_instruction
        ),
        "artifact_language_contract": language_contract.model_dump(mode="json"),
        "artifact_language_contract_digest": language_contract_digest,
        "artifact_language_declaration": language_declaration.model_dump(mode="json"),
        "artifact_language_declaration_digest": language_declaration_digest,
        "canonical_records": {
            kind: {"path": path, "digest": digest} for kind, (path, digest) in records.items()
        },
        "affordance_catalog": AFFORDANCE_CATALOG,
        "visual_asset_candidates": visual_asset_candidate_packet(workspace),
    }
    return workspace.ensure_work_item(
        run_id=run.id,
        role=WorkRole.AUTHOR,
        scope={
            "kind": "course-authoring",
            "revision": 1,
            "curriculum_task_id": curriculum_task.id,
            "curriculum_plan_digest": curriculum_plan_record.digest,
            "curriculum_selection_digest": selection_record.digest,
            "curriculum_selection_producer_task_id": expected_selection_producer,
            "artifact_language_contract_digest": language_contract_digest,
            "artifact_language_declaration_digest": language_declaration_digest,
        },
        persona_hint=AUTHOR_PERSONA,
        packet=packet,
        result_schema=AuthorResult.model_json_schema(mode="validation"),
        dependencies=dependencies,
        snapshot_digest=run.snapshot_digest,
    )


def _load_author_result(payload: bytes) -> AuthorResult:
    try:
        return AuthorResult.model_validate_json(payload)
    except PydanticValidationError as exc:
        raise ProcessingError(f"Invalid Author result: {exc}") from exc
    except (UnicodeDecodeError, ValueError) as exc:
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


def _draft_links_asset(content: str, artifact_path: str, asset_path: str) -> bool:
    artifact_directory = posixpath.dirname(artifact_path)
    for match in _MARKDOWN_TARGET_RE.finditer(content):
        target = match.group("target").strip("<>")
        parsed = urlparse(target)
        if parsed.scheme or parsed.netloc:
            continue
        normalized = posixpath.normpath(posixpath.join(artifact_directory, parsed.path))
        if normalized == asset_path:
            return True
    return False


def _visual_asset_candidates_by_id(
    workspace: Workspace,
) -> dict[str, tuple[MaterializedVisualAsset, Path]]:
    return {
        candidate.candidate_id: (candidate, image_path)
        for candidate, image_path in canonical_visual_asset_candidates(workspace)
    }


def _is_subsequence(expected: list[str], actual: list[str]) -> bool:
    position = 0
    for item in actual:
        if position < len(expected) and item == expected[position]:
            position += 1
    return position == len(expected)


def _validate_selected_curriculum(
    workspace: Workspace,
    task: WorkItem,
    result: AuthorResult,
) -> None:
    expected_plan_digest = task.scope.get("curriculum_plan_digest")
    if expected_plan_digest is None:
        return
    if not isinstance(expected_plan_digest, str):
        raise ProcessingError("Author task has an invalid curriculum plan digest")
    curriculum_task_id = task.scope.get("curriculum_task_id")
    expected_selection_digest = task.scope.get("curriculum_selection_digest")
    expected_selection_producer = task.scope.get("curriculum_selection_producer_task_id")
    if not all(
        isinstance(value, str)
        for value in (
            curriculum_task_id,
            expected_selection_digest,
            expected_selection_producer,
        )
    ):
        raise ProcessingError("Author task is missing its selected curriculum binding")
    plan, plan_record = load_canonical_curriculum_plan(
        workspace,
        expected_digest=expected_plan_digest,
        expected_producer_task_id=str(curriculum_task_id),
    )
    selection, _selection_record = load_canonical_curriculum_selection(
        workspace,
        expected_digest=str(expected_selection_digest),
        expected_producer_task_id=str(expected_selection_producer),
    )
    if selection.curriculum_plan_digest != plan_record.digest:
        raise ProcessingError("Author task selection targets different curriculum options")
    if result.curriculum_decision_required or result.curriculum_decision_summary is not None:
        raise ProcessingError("Curriculum decisions must be settled before artifact authoring")
    if result.curriculum.selected_path_id != selection.selected_path_id:
        raise ProcessingError("Author curriculum changed the selected canonical path")
    planned_ids = [path.id for path in plan.paths]
    authored_ids = [path.id for path in result.curriculum.paths]
    localized_edition = workspace.edition_id is not None
    if authored_ids != planned_ids or (
        not localized_edition and result.curriculum.rationale != plan.rationale
    ):
        raise ProcessingError("Author curriculum changed the canonical curriculum options")
    authored_paths = {path.id: path for path in result.curriculum.paths}
    artifacts = {artifact.id: artifact for artifact in result.artifacts}
    for planned_path in plan.paths:
        authored_path = authored_paths[planned_path.id]
        if authored_path.kind != planned_path.kind or (
            not localized_edition
            and (
                authored_path.title != planned_path.title
                or authored_path.use_when != planned_path.use_when
            )
        ):
            raise ProcessingError(
                f"Author curriculum changed canonical path metadata for {planned_path.id}"
            )
        authored_unit_sequence: list[str] = []
        seen_units: set[str] = set()
        for artifact_id in authored_path.artifact_ids:
            for unit_id in artifacts[artifact_id].semantic_unit_ids:
                if unit_id not in seen_units:
                    authored_unit_sequence.append(unit_id)
                    seen_units.add(unit_id)
        if not _is_subsequence(planned_path.unit_sequence, authored_unit_sequence):
            raise ProcessingError(
                f"Author artifacts do not preserve curriculum unit order for {planned_path.id}"
            )


def _author_language_declaration(
    workspace: Workspace,
    task: WorkItem,
    artifact_language: str,
) -> ArtifactLanguageDeclaration:
    expected_contract_digest = task.scope.get("artifact_language_contract_digest")
    expected_declaration_digest = task.scope.get("artifact_language_declaration_digest")
    if expected_contract_digest is None and expected_declaration_digest is None:
        contract, contract_digest = ensure_artifact_language_contract(workspace)
        return declare_artifact_language(
            contract,
            contract_digest,
            artifact_language,
            legacy=True,
        )
    if not isinstance(expected_contract_digest, str) or not isinstance(
        expected_declaration_digest, str
    ):
        raise ProcessingError("Author task has a partial artifact-language binding")
    bound_author_task_id = task.scope.get("repair_of", task.id)
    if not isinstance(bound_author_task_id, str):
        raise ProcessingError("Author task has an invalid artifact-language predecessor")
    language_state = canonical_artifact_language_state(
        workspace,
        expected_author_task_id=bound_author_task_id,
    )
    if language_state.contract_digest != expected_contract_digest:
        raise ProcessingError("Author task targets a stale artifact-language contract")
    if language_state.declaration_digest != expected_declaration_digest:
        raise ProcessingError("Author task targets a stale artifact-language declaration")
    actual = declare_artifact_language(
        language_state.contract,
        language_state.contract_digest,
        artifact_language,
        legacy=language_state.legacy,
    )
    if actual != language_state.declaration:
        raise ProcessingError("Author changed the canonical artifact-language declaration")
    return language_state.declaration


def _validate_author_result(
    workspace: Workspace,
    task: WorkItem,
    result: AuthorResult,
) -> tuple[
    ArtifactLanguageDeclaration,
    dict[str, Any] | None,
    list[CanonicalOutputSnapshot],
]:
    _validate_selected_curriculum(workspace, task, result)
    language_declaration = _author_language_declaration(
        workspace,
        task,
        result.artifact_language,
    )
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
            raise ProcessingError(f"Author {mode} capability exceeds the Analyze evidence ceiling")
    represented: set[str] = set()
    task_output = workspace.tasks_dir / task.id / "output"
    draft_content_by_path: dict[str, str] = {}
    draft_snapshots: list[CanonicalOutputSnapshot] = []
    for artifact in result.artifacts:
        if not set(artifact.semantic_unit_ids) <= known_units:
            raise ProcessingError(f"Artifact {artifact.id} references unknown semantic units")
        represented.update(artifact.semantic_unit_ids)
        draft = task_output / artifact.draft_path
        snapshot = workspace.canonical_output_file_snapshot(
            task.id,
            "artifact-draft",
            artifact.id,
            draft,
            max_bytes=MAX_AUTHOR_DRAFT_BYTES,
        )
        if snapshot.file.digest != artifact.draft_sha256:
            raise ProcessingError(f"Artifact {artifact.id} draft digest does not match")
        try:
            draft_content_by_path[artifact.path] = snapshot.file.payload.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ProcessingError(f"Could not read artifact {artifact.id} draft: {exc}") from exc
        draft_snapshots.append(snapshot)
    required_units = {
        unit.id
        for unit in units
        if unit.materiality in {"core", "supporting"} and unit.disposition == "included"
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
    claims_by_id = {claim.id: claim for claim in result.claims}
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
                raise ProcessingError(f"Claim {claim.id} evidence references an unrelated source")
            if not set(evidence.evidence_ids) <= allowed_evidence_by_source[evidence.source_id]:
                raise ProcessingError(
                    f"Claim {claim.id} references evidence outside its semantic units"
                )
            source = sources[evidence.source_id]
            if source.duration is not None and evidence.end > source.duration:
                raise ProcessingError(f"Claim {claim.id} evidence extends beyond source duration")
    candidate_assets = _visual_asset_candidates_by_id(workspace)
    for selected in result.assets:
        candidate_pair = candidate_assets.get(selected.candidate_id)
        if candidate_pair is None:
            raise ProcessingError(
                f"Author selected unknown visual asset candidate {selected.candidate_id}"
            )
        candidate, _image_path = candidate_pair
        linked_claims = [claims_by_id[claim_id] for claim_id in selected.claim_ids]
        linked_evidence_ids = {
            evidence_id
            for claim in linked_claims
            for evidence in claim.evidence
            if evidence.source_id == candidate.source_id
            and set(evidence.modalities) & {"visual", "temporal"}
            for evidence_id in evidence.evidence_ids
        }
        if not set(candidate.evidence_ids) <= linked_evidence_ids:
            raise ProcessingError(
                f"Visual asset {selected.path} evidence is not retained by its linked claims"
            )
        linked_semantic_units = {
            unit_id for claim in linked_claims for unit_id in claim.semantic_unit_ids
        }
        if not set(candidate.semantic_unit_ids) <= linked_semantic_units:
            raise ProcessingError(
                f"Visual asset {selected.path} semantic units are not retained by its linked claims"
            )
        for artifact_path in selected.used_by:
            if not _draft_links_asset(
                draft_content_by_path[artifact_path],
                artifact_path,
                selected.path,
            ):
                raise ProcessingError(
                    f"Artifact {artifact_path} does not link selected visual asset {selected.path}"
                )
    # A first Author over analyze-only evidence may establish the shared logical
    # identity family. Do so only after every structural and grounding check above
    # succeeds, so an otherwise invalid submission cannot win that CAS.
    identity_baseline = validate_edition_author_identity(
        workspace,
        name=result.name,
        artifacts=result.artifacts,
        claims=result.claims,
    )
    return language_declaration, identity_baseline, draft_snapshots


def _course_assets(workspace: Workspace, result: AuthorResult) -> list[CourseAsset]:
    candidates = _visual_asset_candidates_by_id(workspace)
    assets: list[CourseAsset] = []
    for selected in result.assets:
        candidate, image_path = candidates[selected.candidate_id]
        assets.append(
            CourseAsset(
                path=selected.path,
                source_path=image_path.relative_to(workspace.root),
                candidate_id=candidate.candidate_id,
                source_id=candidate.source_id,
                evidence_ids=candidate.evidence_ids,
                semantic_unit_ids=candidate.semantic_unit_ids,
                presentation=candidate.presentation,
                crop=candidate.crop,
                source_sha256=candidate.sha256,
                description=selected.description,
                used_by=selected.used_by,
                claim_ids=selected.claim_ids,
            )
        )
    return assets


def submit_author_result(
    workspace: Workspace,
    task_id: str,
    result_path: Path,
) -> WorkItem:
    task = workspace.get_work_item(task_id)
    if task.role != WorkRole.AUTHOR or task.scope.get("kind") not in {
        "course-authoring",
        "course-authoring-repair",
    }:
        raise ProcessingError(f"Task is not an Author task: {task_id}")
    result_snapshot = workspace.task_output_file_snapshot(
        task_id,
        result_path,
        max_bytes=8 * 1024 * 1024,
    )
    result = _load_author_result(result_snapshot.payload)
    if result.task_id != task_id or result.snapshot_digest != task.snapshot_digest:
        raise ProcessingError("Author result does not belong to this task snapshot")
    language_declaration, identity_baseline, draft_snapshots = _validate_author_result(
        workspace,
        task,
        result,
    )
    output = workspace.tasks_dir / task_id / "output"
    course_path = output / "course.json"
    curriculum_path = output / "curriculum.json"
    interaction_path = output / "interaction.json"
    capability_path = output / "capability-profile.json"
    artifact_plan_path = output / "artifact-plan.json"
    affordance_path = output / "instructional-affordances.json"
    claims_path = output / "claims.json"
    assets_path = output / "assets.json"
    workspace.write_json(
        course_path,
        {
            "name": result.name,
            "title": result.title,
            "description": result.description,
            "scope": result.scope,
            "artifact_language": language_declaration.artifact_language,
            "prerequisites": result.prerequisites,
            "core_principles": [item.model_dump(mode="json") for item in result.core_principles],
            "limitations": result.limitations,
            "curriculum_decision_required": result.curriculum_decision_required,
            "curriculum_decision_summary": result.curriculum_decision_summary,
        },
    )
    workspace.write_json(curriculum_path, result.curriculum)
    workspace.write_json(interaction_path, result.interaction)
    workspace.write_json(capability_path, result.capability_profile)
    workspace.write_json(
        artifact_plan_path,
        [item.model_dump(mode="json") for item in result.artifacts],
    )
    workspace.write_json(
        affordance_path,
        [item.model_dump(mode="json") for item in result.affordance_ledger],
    )
    workspace.write_json(
        claims_path,
        [item.model_dump(mode="json") for item in result.claims],
    )
    workspace.write_json(
        assets_path,
        [item.model_dump(mode="json") for item in _course_assets(workspace, result)],
    )
    canonical_outputs: list[CanonicalOutput] = [
        workspace.canonical_output_file_snapshot(task_id, "course", "default", course_path),
        workspace.canonical_output_file_snapshot(
            task_id,
            "curriculum",
            "default",
            curriculum_path,
        ),
        workspace.canonical_output_file_snapshot(
            task_id,
            "interaction",
            "default",
            interaction_path,
        ),
        workspace.canonical_output_file_snapshot(
            task_id,
            "capability-profile",
            "default",
            capability_path,
        ),
        workspace.canonical_output_file_snapshot(
            task_id,
            "artifact-plan",
            "default",
            artifact_plan_path,
        ),
        workspace.canonical_output_file_snapshot(
            task_id,
            "instructional-affordances",
            "default",
            affordance_path,
        ),
        workspace.canonical_output_file_snapshot(task_id, "claims", "default", claims_path),
        workspace.canonical_output_file_snapshot(task_id, "assets", "default", assets_path),
    ]
    if workspace.canonical_record("artifact-language-declaration") is None:
        language_path = output / "artifact-language.json"
        workspace.write_json(language_path, language_declaration)
        canonical_outputs.append(
            workspace.canonical_output_file_snapshot(
                task_id,
                "artifact-language-declaration",
                "default",
                language_path,
            )
        )
    canonical_outputs.extend(draft_snapshots)
    accepted, _records = workspace.accept_work_result(
        task_id=task_id,
        lease_token=result.lease_token,
        result_path=result_path,
        result_snapshot=result_snapshot,
        producer=result.producer.model_dump(mode="json"),
        canonical_outputs=canonical_outputs,
        identity_baseline=identity_baseline,
    )
    return accepted
