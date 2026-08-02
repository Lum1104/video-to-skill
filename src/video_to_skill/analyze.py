"""Workspace-centered Analyze task planning and result acceptance."""

from __future__ import annotations

import json
from collections.abc import Iterable
from hashlib import sha256
from pathlib import Path
from typing import Any, cast

from pydantic import ValidationError as PydanticValidationError

from video_to_skill.analysis_depth import verify_analysis_depth_contract
from video_to_skill.errors import ProcessingError
from video_to_skill.generation import SemanticUnit
from video_to_skill.models import VisualEvent, VisualOrigin
from video_to_skill.orchestration import AnalyzeResult
from video_to_skill.utils import is_within, stable_hash
from video_to_skill.visual_assets import materialize_visual_asset_candidates
from video_to_skill.work import AnalysisRun, WorkItem, WorkRole, WorkState
from video_to_skill.workspace import CanonicalOutputSnapshot, Workspace

MAX_ANALYZE_SECTIONS_PER_TASK = 8
MAX_ANALYZE_PACKET_ITEMS = 3_000
SHORT_COURSE_SECONDS = 60 * 60
_PACKET_CATEGORIES = ("transcripts", "visuals", "observations", "gaps")

ANALYZE_PERSONA = (
    "You are a senior multimodal evidence and semantic-analysis engineer with deep "
    "experience building high-recall knowledge graphs from courses, interviews, coding "
    "sessions, and physical demonstrations. Preserve claims, reasoning, examples, "
    "qualifications, conflicts, uncertainty, and provenance before curriculum design."
)


def _section_packet(
    workspace: Workspace,
    source_id: str,
    ordinal: int,
    *,
    allocations: dict[str, int],
    available: dict[str, int],
) -> dict[str, Any]:
    section = next(
        (item for item in workspace.semantic_segments(source_id) if item.ordinal == ordinal),
        None,
    )
    if section is None:
        raise ProcessingError(f"Unknown semantic section {ordinal} for source {source_id}")
    transcripts = workspace.transcripts(
        source_id,
        start=section.start,
        end=section.end,
        limit=allocations["transcripts"],
    )
    visuals = workspace.visuals(
        source_id,
        start=section.start,
        end=section.end,
        limit=allocations["visuals"],
    )
    observations = workspace.observations(
        source_id,
        start=section.start,
        end=section.end,
        limit=allocations["observations"],
    )
    gaps = workspace.gaps(
        source_id,
        resolved=None,
        start=section.start,
        end=section.end,
        limit=allocations["gaps"],
    )
    included = {
        "transcripts": len(transcripts),
        "visuals": len(visuals),
        "observations": len(observations),
        "gaps": len(gaps),
    }
    if included != allocations:
        raise ProcessingError(
            f"Evidence changed while Analyze packet section {source_id}/{ordinal} was planned"
        )
    visually_material = any(
        visual.kind.value in {"slide", "code", "ui", "physical"} or visual.ocr_text
        for visual in visuals
    )
    return {
        "section": section.model_dump(mode="json"),
        "route": "multimodal" if visually_material else "speech-first",
        "transcripts": [item.model_dump(mode="json") for item in transcripts],
        "visuals": [item.model_dump(mode="json") for item in visuals],
        "observations": [item.model_dump(mode="json") for item in observations],
        "gaps": [item.model_dump(mode="json") for item in gaps],
        "evidence_coverage": {
            "categories": {
                category: {
                    "available": available[category],
                    "included": included[category],
                    "truncated": available[category] - included[category],
                }
                for category in _PACKET_CATEGORIES
            },
            "available": sum(available.values()),
            "included": sum(included.values()),
            "truncated": sum(available.values()) - sum(included.values()),
            "complete": available == included,
        },
    }


def _chunks(values: list[int], size: int) -> Iterable[list[int]]:
    for index in range(0, len(values), size):
        yield values[index : index + size]


def _fair_packet_allocations(
    available: list[dict[str, int]],
    *,
    limit: int,
) -> list[dict[str, int]]:
    """Round-robin one item per section/category until the task-wide cap is spent."""

    allocations = [{category: 0 for category in _PACKET_CATEGORIES} for _ in available]
    remaining = min(limit, MAX_ANALYZE_PACKET_ITEMS)
    while remaining > 0:
        progressed = False
        for index, counts in enumerate(available):
            for category in _PACKET_CATEGORIES:
                if allocations[index][category] >= counts[category]:
                    continue
                allocations[index][category] += 1
                remaining -= 1
                progressed = True
                if remaining == 0:
                    break
            if remaining == 0:
                break
        if not progressed:
            break
    return allocations


def plan_analyze_tasks(workspace: Workspace, run: AnalysisRun) -> list[WorkItem]:
    analysis_depth = workspace.analysis_depth_contract()
    if analysis_depth is None:
        raise ProcessingError("Analyze planning requires a persisted analysis-depth contract")
    verify_analysis_depth_contract(analysis_depth)
    budget = analysis_depth.budget
    sources = workspace.list_sources()
    sections_by_source = {
        source.id: [section.ordinal for section in workspace.semantic_segments(source.id)]
        for source in sources
    }
    total_duration = sum(source.duration or 0 for source in sources)
    total_sections = sum(len(sections) for sections in sections_by_source.values())
    short_course = (
        len(sources) <= budget.integrated_course_max_sources
        and total_duration
        <= min(
            SHORT_COURSE_SECONDS,
            budget.integrated_course_max_seconds,
        )
        and total_sections <= budget.integrated_course_max_sections
    )
    scopes: list[dict[str, object]] = []
    if short_course:
        scopes.append(
            {
                "kind": "course",
                "source_sections": sections_by_source,
                "integrated": True,
            }
        )
    else:
        for source in sources:
            for ordinals in _chunks(
                sections_by_source[source.id],
                min(MAX_ANALYZE_SECTIONS_PER_TASK, budget.analyze_sections_per_task),
            ):
                scopes.append(
                    {
                        "kind": "section-group",
                        "source_sections": {source.id: ordinals},
                        "integrated": False,
                    }
                )
    tasks: list[WorkItem] = []
    for scope in scopes:
        source_sections = cast(dict[str, list[int]], scope["source_sections"])
        section_keys = [
            (source_id, ordinal)
            for source_id, ordinals in source_sections.items()
            for ordinal in ordinals
        ]
        available = []
        for source_id, ordinal in section_keys:
            section = next(
                (
                    item
                    for item in workspace.semantic_segments(source_id)
                    if item.ordinal == ordinal
                ),
                None,
            )
            if section is None:
                raise ProcessingError(f"Unknown semantic section {ordinal} for source {source_id}")
            available.append(
                workspace.analysis_evidence_counts(
                    source_id,
                    start=section.start,
                    end=section.end,
                )
            )
        allocations = _fair_packet_allocations(
            available,
            limit=min(budget.analyze_packet_item_limit, MAX_ANALYZE_PACKET_ITEMS),
        )
        section_packets = [
            _section_packet(
                workspace,
                source_id,
                ordinal,
                allocations=allocation,
                available=counts,
            )
            for (source_id, ordinal), allocation, counts in zip(
                section_keys,
                allocations,
                available,
                strict=True,
            )
        ]
        available_total = sum(sum(counts.values()) for counts in available)
        included_total = sum(sum(counts.values()) for counts in allocations)
        packet_limit = min(budget.analyze_packet_item_limit, MAX_ANALYZE_PACKET_ITEMS)
        allowed_evidence_ids = sorted(
            {
                str(item["id"])
                for packet in section_packets
                for category in ("transcripts", "visuals", "observations")
                for item in packet[category]
            }
        )
        allowed_evidence_by_source: dict[str, list[str]] = {}
        for source_id in source_sections:
            allowed_evidence_by_source[source_id] = sorted(
                {
                    str(item["id"])
                    for packet in section_packets
                    for category in ("transcripts", "visuals", "observations")
                    for item in packet[category]
                    if item["source_id"] == source_id
                }
            )
        packet = {
            "analysis_depth": analysis_depth.model_dump(mode="json"),
            "instructions": (
                "Perform high-recall extraction, terminology normalization, relation linking, "
                "materiality review, and capability-ceiling analysis. Do not design a curriculum. "
                "Use only evidence IDs in allowed_evidence_ids and record uncertainty. The "
                "task-wide evidence_budget and each section's evidence_coverage explicitly "
                "report engine truncation; treat any truncation as partial packet coverage and "
                "do not infer that omitted evidence was reviewed. Propose "
                "visual asset candidates only when a frame, focused crop, or two-to-four-frame "
                "sequence materially improves teaching or preserves visible evidence that text "
                "cannot replace. Give normalized crop coordinates; never edit image pixels."
            ),
            "sources": [
                source.model_dump(mode="json") for source in sources if source.id in source_sections
            ],
            "sections": section_packets,
            "allowed_evidence_ids": allowed_evidence_ids,
            "allowed_evidence_by_source": allowed_evidence_by_source,
            "evidence_budget": {
                "profile_limit": budget.analyze_packet_item_limit,
                "absolute_safety_limit": MAX_ANALYZE_PACKET_ITEMS,
                "effective_limit": packet_limit,
                "allocation": "deterministic-section-category-round-robin",
                "available": available_total,
                "included": included_total,
                "truncated": available_total - included_total,
                "complete": available_total == included_total,
            },
            "investigation_policy": {
                "commands": ["context", "contact-sheet", "frames", "query", "gaps"],
                "dynamic_visual_origin": VisualOrigin.INVESTIGATION.value,
                "scope": "source-sections",
                "max_window_seconds": budget.investigation_window_seconds,
                "max_frames_per_window": budget.investigation_max_frames_per_window,
            },
        }
        tasks.append(
            workspace.ensure_work_item(
                run_id=run.id,
                role=WorkRole.ANALYZE,
                scope=scope,
                persona_hint=ANALYZE_PERSONA,
                packet=packet,
                result_schema=AnalyzeResult.model_json_schema(mode="validation"),
                snapshot_digest=run.snapshot_digest,
            )
        )
    return tasks


def plan_analyze_integration_task(
    workspace: Workspace,
    run: AnalysisRun,
    shard_tasks: list[WorkItem],
) -> WorkItem:
    analysis_depth = workspace.analysis_depth_contract()
    if analysis_depth is None:
        raise ProcessingError("Analyze integration requires a persisted analysis-depth contract")
    verify_analysis_depth_contract(analysis_depth)
    if not shard_tasks or any(item.role != WorkRole.ANALYZE for item in shard_tasks):
        raise ProcessingError("Analyze integration requires Analyze shard tasks")
    shard_results: list[AnalyzeResult] = []
    shard_result_paths: list[str] = []
    for item in shard_tasks:
        if item.state != WorkState.COMPLETE or item.result_path is None:
            raise ProcessingError("Analyze integration requires completed shard results")
        path = workspace.root / item.result_path
        payload = workspace.read_file_bytes(path, max_bytes=8 * 1024 * 1024)
        if item.result_digest is None or sha256(payload).hexdigest() != item.result_digest:
            raise ProcessingError(f"Accepted Analyze shard failed its digest check: {item.id}")
        shard_results.append(_load_analyze_result(payload))
        shard_result_paths.append(str(item.result_path))
    packet = {
        "analysis_depth": analysis_depth.model_dump(mode="json"),
        "instructions": (
            "Integrate the completed shard results without deleting source-specific semantic "
            "units. Normalize terminology only where equivalence is supported, adjudicate or "
            "retain conflicts, recompute relations and coverage, and emit an integrated result. "
            "Curate a bounded final set of non-duplicate visual asset candidates from shard "
            "evidence; retain only visuals with independent teaching or verification value."
        ),
        "shard_results": shard_result_paths,
        "allowed_evidence_ids": sorted(
            {
                evidence_id
                for result in shard_results
                for unit in result.semantic_units
                for evidence_id in unit.evidence_ids
            }
        ),
        "allowed_evidence_by_source": {
            source_id: sorted(
                {
                    evidence_id
                    for result in shard_results
                    for unit in result.semantic_units
                    if unit.source_id == source_id
                    for evidence_id in unit.evidence_ids
                }
            )
            for source_id in sorted(
                {unit.source_id for result in shard_results for unit in result.semantic_units}
            )
        },
        "required_semantic_unit_ids": sorted(
            {unit.id for result in shard_results for unit in result.semantic_units}
        ),
    }
    return workspace.ensure_work_item(
        run_id=run.id,
        role=WorkRole.ANALYZE,
        scope={"kind": "course-integration", "integrated": True},
        persona_hint=ANALYZE_PERSONA,
        packet=packet,
        result_schema=AnalyzeResult.model_json_schema(mode="validation"),
        dependencies=[item.id for item in shard_tasks],
        snapshot_digest=run.snapshot_digest,
    )


def _load_analyze_result(payload: bytes) -> AnalyzeResult:
    try:
        return AnalyzeResult.model_validate_json(payload)
    except PydanticValidationError as exc:
        raise ProcessingError(f"Invalid Analyze result: {exc}") from exc
    except (UnicodeDecodeError, ValueError) as exc:
        if isinstance(exc, ProcessingError):
            raise
        raise ProcessingError(f"Could not read Analyze result: {exc}") from exc


def _scoped_investigation_evidence(
    workspace: Workspace,
    task: WorkItem,
) -> dict[str, dict[str, VisualEvent]]:
    source_sections = task.scope.get("source_sections")
    if not isinstance(source_sections, dict):
        return {}
    scoped: dict[str, dict[str, VisualEvent]] = {}
    for raw_source_id, raw_ordinals in source_sections.items():
        source_id = str(raw_source_id)
        if not isinstance(raw_ordinals, list):
            continue
        ordinals = {int(ordinal) for ordinal in raw_ordinals}
        intervals = [
            (section.start, section.end)
            for section in workspace.semantic_segments(source_id)
            if section.ordinal in ordinals
        ]
        investigation_root = workspace.source_directory(source_id) / "investigation-frames"
        for event in workspace.visuals(source_id, origin=VisualOrigin.INVESTIGATION):
            raw_path = event.path.expanduser()
            resolved_path = raw_path.resolve()
            if (
                not intervals
                or not any(start <= event.timestamp <= end for start, end in intervals)
                or not resolved_path.is_file()
                or raw_path.is_symlink()
                or not is_within(resolved_path, investigation_root)
            ):
                continue
            scoped.setdefault(source_id, {})[event.id] = event
    return scoped


def _validate_analyze_evidence(
    workspace: Workspace,
    task: WorkItem,
    result: AnalyzeResult,
) -> None:
    packet_path = workspace.root / task.packet_path
    try:
        packet = json.loads(packet_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ProcessingError(f"Invalid Analyze task packet: {exc}") from exc
    if stable_hash(packet, length=64) != task.packet_digest:
        raise ProcessingError("Analyze task packet failed its digest check")
    allowed = set(packet["payload"].get("allowed_evidence_ids", []))
    allowed_by_source = {
        str(source_id): set(evidence_ids)
        for source_id, evidence_ids in packet["payload"]
        .get("allowed_evidence_by_source", {})
        .items()
    }
    investigation_policy = packet["payload"].get("investigation_policy", {})
    investigation_by_source = (
        _scoped_investigation_evidence(workspace, task)
        if investigation_policy.get("dynamic_visual_origin") == VisualOrigin.INVESTIGATION.value
        and investigation_policy.get("scope") == "source-sections"
        else {}
    )
    allowed.update(
        evidence_id
        for evidence_by_id in investigation_by_source.values()
        for evidence_id in evidence_by_id
    )
    for source_id, evidence_by_id in investigation_by_source.items():
        allowed_by_source.setdefault(source_id, set()).update(evidence_by_id)
    scoped_sources = {source_id for source_id in task.scope.get("source_sections", {})}
    known_sources = {source.id: source for source in workspace.list_sources()}
    if not scoped_sources:
        scoped_sources = set(known_sources)
    for unit in result.semantic_units:
        if unit.source_id not in scoped_sources:
            raise ProcessingError(
                f"Semantic unit {unit.id} references source outside its Analyze task"
            )
        if not set(unit.evidence_ids) <= allowed or not set(
            unit.evidence_ids
        ) <= allowed_by_source.get(unit.source_id, set()):
            raise ProcessingError(
                f"Semantic unit {unit.id} references evidence outside its Analyze packet"
            )
        source = known_sources[unit.source_id]
        if source.duration is not None and unit.end > source.duration:
            raise ProcessingError(f"Semantic unit {unit.id} extends beyond source duration")
    visuals_by_source = {
        source_id: {visual.id for visual in workspace.visuals(source_id)}
        for source_id in scoped_sources
    }
    for candidate in result.visual_asset_candidates:
        if candidate.source_id not in scoped_sources:
            raise ProcessingError(
                f"Visual asset candidate {candidate.id} references source outside its Analyze task"
            )
        if not set(candidate.evidence_ids) <= allowed_by_source.get(candidate.source_id, set()):
            raise ProcessingError(
                f"Visual asset candidate {candidate.id} references evidence outside its Analyze packet"
            )
        if not set(candidate.evidence_ids) <= visuals_by_source.get(candidate.source_id, set()):
            raise ProcessingError(
                f"Visual asset candidate {candidate.id} must reference visual evidence"
            )
    expected_integrated = bool(task.scope.get("integrated"))
    if result.integrated != expected_integrated:
        raise ProcessingError("Analyze result integration flag disagrees with its task")
    if set(result.coverage.source_ids) != {unit.source_id for unit in result.semantic_units}:
        raise ProcessingError("Semantic coverage source IDs disagree with submitted units")
    required_unit_ids = set(packet["payload"].get("required_semantic_unit_ids", []))
    submitted_unit_ids = {unit.id for unit in result.semantic_units}
    if not required_unit_ids <= submitted_unit_ids:
        raise ProcessingError(
            "Integrated Analyze result omits shard semantic units: "
            + ", ".join(sorted(required_unit_ids - submitted_unit_ids))
        )
    counts = {
        "core_units": sum(unit.materiality == "core" for unit in result.semantic_units),
        "supporting_units": sum(unit.materiality == "supporting" for unit in result.semantic_units),
        "contextual_units": sum(unit.materiality == "contextual" for unit in result.semantic_units),
        "incidental_units": sum(unit.materiality == "incidental" for unit in result.semantic_units),
        "included_units": sum(unit.disposition == "included" for unit in result.semantic_units),
        "merged_units": sum(unit.disposition == "merged" for unit in result.semantic_units),
        "context_only_units": sum(
            unit.disposition == "context-only" for unit in result.semantic_units
        ),
        "omitted_units": sum(unit.disposition == "omitted" for unit in result.semantic_units),
    }
    for field, expected in counts.items():
        if getattr(result.coverage, field) != expected:
            raise ProcessingError(f"Semantic coverage {field} does not match submitted units")
    _validate_merge_graph(result.semantic_units)


def _validate_merge_graph(units: list[SemanticUnit]) -> None:
    by_id = {unit.id: unit for unit in units}
    for unit in units:
        if unit.disposition != "merged":
            continue
        current = unit
        visited: set[str] = set()
        while current.disposition == "merged":
            if current.id in visited:
                raise ProcessingError(f"Semantic merge chain contains a cycle at {current.id}")
            visited.add(current.id)
            assert current.merged_into is not None
            if current.merged_into not in by_id:
                raise ProcessingError(f"Semantic unit {current.id} merges into an unknown unit")
            current = by_id[current.merged_into]
        if current.disposition != "included":
            raise ProcessingError(f"Semantic unit {unit.id} must merge into an included unit")


def submit_analyze_result(
    workspace: Workspace,
    task_id: str,
    result_path: Path,
) -> WorkItem:
    task = workspace.get_work_item(task_id)
    if task.role != WorkRole.ANALYZE:
        raise ProcessingError(f"Task is not an Analyze task: {task_id}")
    result_snapshot = workspace.task_output_file_snapshot(
        task_id,
        result_path,
        max_bytes=8 * 1024 * 1024,
    )
    result = _load_analyze_result(result_snapshot.payload)
    if result.task_id != task_id or result.snapshot_digest != task.snapshot_digest:
        raise ProcessingError("Analyze result does not belong to this task snapshot")
    _validate_analyze_evidence(workspace, task, result)
    output = workspace.tasks_dir / task_id / "output"
    record_id = "default" if result.integrated else task_id
    semantic_map_path = output / "semantic-map.json"
    relations_path = output / "semantic-relations.json"
    capability_path = output / "capability-evidence.json"
    coverage_path = output / "semantic-coverage.json"
    conflicts_path = output / "semantic-conflicts.json"
    workspace.write_json(
        semantic_map_path,
        [unit.model_dump(mode="json") for unit in result.semantic_units],
    )
    workspace.write_json(
        relations_path,
        [relation.model_dump(mode="json") for relation in result.semantic_relations],
    )
    workspace.write_json(
        capability_path,
        [item.model_dump(mode="json") for item in result.capability_evidence],
    )
    workspace.write_json(coverage_path, result.coverage)
    workspace.write_json(
        conflicts_path,
        [item.model_dump(mode="json") for item in result.conflicts],
    )
    materialized_assets = materialize_visual_asset_candidates(
        workspace,
        result.visual_asset_candidates,
        output / "visual-assets",
        record_prefix=record_id,
    )
    visual_assets_path = output / "visual-assets.json"
    workspace.write_json(
        visual_assets_path,
        [item.model_dump(mode="json") for item, _path in materialized_assets],
    )
    image_outputs: list[CanonicalOutputSnapshot] = []
    for item, path in materialized_assets:
        snapshot = workspace.canonical_output_file_snapshot(
            task_id,
            "visual-asset-image",
            item.image_record_id,
            path,
        )
        if snapshot.file.digest != item.sha256:
            raise ProcessingError(
                f"Materialized visual asset changed before acceptance: {item.candidate_id}"
            )
        image_outputs.append(snapshot)
    canonical_outputs = [
        workspace.canonical_output_file_snapshot(
            task_id, "semantic-map", record_id, semantic_map_path
        ),
        workspace.canonical_output_file_snapshot(
            task_id, "semantic-relations", record_id, relations_path
        ),
        workspace.canonical_output_file_snapshot(
            task_id, "capability-evidence", record_id, capability_path
        ),
        workspace.canonical_output_file_snapshot(
            task_id, "semantic-coverage", record_id, coverage_path
        ),
        workspace.canonical_output_file_snapshot(
            task_id, "semantic-conflicts", record_id, conflicts_path
        ),
        workspace.canonical_output_file_snapshot(
            task_id,
            "visual-asset-candidates",
            record_id,
            visual_assets_path,
        ),
        *image_outputs,
    ]
    accepted, _records = workspace.accept_work_result(
        task_id=task_id,
        lease_token=result.lease_token,
        result_path=result_path,
        result_snapshot=result_snapshot,
        producer=result.producer.model_dump(mode="json"),
        canonical_outputs=canonical_outputs,
    )
    return accepted
