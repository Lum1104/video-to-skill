"""Workspace-centered Analyze task planning and result acceptance."""

from __future__ import annotations

import json
from collections.abc import Iterable
from pathlib import Path
from typing import Any, cast

from pydantic import ValidationError as PydanticValidationError

from video_to_skill.errors import ProcessingError
from video_to_skill.generation import SemanticUnit
from video_to_skill.orchestration import AnalyzeResult
from video_to_skill.utils import atomic_write_json, hash_file, stable_hash
from video_to_skill.work import AnalysisRun, WorkItem, WorkRole, WorkState
from video_to_skill.workspace import Workspace

MAX_ANALYZE_SECTIONS_PER_TASK = 8
MAX_ANALYZE_PACKET_ITEMS = 3_000
SHORT_COURSE_SECONDS = 60 * 60

ANALYZE_PERSONA = (
    "You are a senior multimodal evidence and semantic-analysis engineer with deep "
    "experience building high-recall knowledge graphs from courses, interviews, coding "
    "sessions, and physical demonstrations. Preserve claims, reasoning, examples, "
    "qualifications, conflicts, uncertainty, and provenance before curriculum design."
)


def _section_packet(workspace: Workspace, source_id: str, ordinal: int) -> dict[str, Any]:
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
        limit=MAX_ANALYZE_PACKET_ITEMS,
    )
    visuals = workspace.visuals(
        source_id,
        start=section.start,
        end=section.end,
        limit=MAX_ANALYZE_PACKET_ITEMS,
    )
    observations = workspace.observations(
        source_id,
        start=section.start,
        end=section.end,
        limit=MAX_ANALYZE_PACKET_ITEMS,
    )
    gaps = [
        gap
        for gap in workspace.gaps(source_id, resolved=None)
        if gap.end >= section.start and gap.start <= section.end
    ]
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
    }


def _chunks(values: list[int], size: int) -> Iterable[list[int]]:
    for index in range(0, len(values), size):
        yield values[index : index + size]


def plan_analyze_tasks(workspace: Workspace, run: AnalysisRun) -> list[WorkItem]:
    sources = workspace.list_sources()
    sections_by_source = {
        source.id: [section.ordinal for section in workspace.semantic_segments(source.id)]
        for source in sources
    }
    total_duration = sum(source.duration or 0 for source in sources)
    total_sections = sum(len(sections) for sections in sections_by_source.values())
    short_course = (
        len(sources) <= 3
        and total_duration <= SHORT_COURSE_SECONDS
        and total_sections <= MAX_ANALYZE_SECTIONS_PER_TASK * 3
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
                MAX_ANALYZE_SECTIONS_PER_TASK,
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
        section_packets = [
            _section_packet(workspace, source_id, ordinal)
            for source_id, ordinals in source_sections.items()
            for ordinal in ordinals
        ]
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
            "instructions": (
                "Perform high-recall extraction, terminology normalization, relation linking, "
                "materiality review, and capability-ceiling analysis. Do not design a curriculum. "
                "Use only evidence IDs in allowed_evidence_ids and record uncertainty."
            ),
            "sources": [
                source.model_dump(mode="json") for source in sources if source.id in source_sections
            ],
            "sections": section_packets,
            "allowed_evidence_ids": allowed_evidence_ids,
            "allowed_evidence_by_source": allowed_evidence_by_source,
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
    if not shard_tasks or any(item.role != WorkRole.ANALYZE for item in shard_tasks):
        raise ProcessingError("Analyze integration requires Analyze shard tasks")
    shard_results: list[AnalyzeResult] = []
    shard_result_paths: list[str] = []
    for item in shard_tasks:
        if item.state != WorkState.COMPLETE or item.result_path is None:
            raise ProcessingError("Analyze integration requires completed shard results")
        path = workspace.root / item.result_path
        if (
            not path.is_file()
            or path.is_symlink()
            or item.result_digest is None
            or hash_file(path) != item.result_digest
        ):
            raise ProcessingError(f"Accepted Analyze shard failed its digest check: {item.id}")
        shard_results.append(_load_analyze_result(path))
        shard_result_paths.append(str(item.result_path))
    packet = {
        "instructions": (
            "Integrate the completed shard results without deleting source-specific semantic "
            "units. Normalize terminology only where equivalence is supported, adjudicate or "
            "retain conflicts, recompute relations and coverage, and emit an integrated result."
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


def _load_analyze_result(path: Path) -> AnalyzeResult:
    try:
        if not path.is_file() or path.stat().st_size > 8 * 1024 * 1024:
            raise ProcessingError("Analyze result must be a regular JSON file no larger than 8 MiB")
        return AnalyzeResult.model_validate_json(path.read_text(encoding="utf-8"))
    except PydanticValidationError as exc:
        raise ProcessingError(f"Invalid Analyze result: {exc}") from exc
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        if isinstance(exc, ProcessingError):
            raise
        raise ProcessingError(f"Could not read Analyze result: {exc}") from exc


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
    result = _load_analyze_result(result_path)
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
    atomic_write_json(
        semantic_map_path,
        [unit.model_dump(mode="json") for unit in result.semantic_units],
    )
    atomic_write_json(
        relations_path,
        [relation.model_dump(mode="json") for relation in result.semantic_relations],
    )
    atomic_write_json(
        capability_path,
        [item.model_dump(mode="json") for item in result.capability_evidence],
    )
    atomic_write_json(coverage_path, result.coverage)
    atomic_write_json(
        conflicts_path,
        [item.model_dump(mode="json") for item in result.conflicts],
    )
    accepted, _records = workspace.accept_work_result(
        task_id=task_id,
        lease_token=result.lease_token,
        result_path=result_path,
        producer=result.producer.model_dump(mode="json"),
        canonical_outputs=[
            ("semantic-map", record_id, semantic_map_path),
            ("semantic-relations", record_id, relations_path),
            ("capability-evidence", record_id, capability_path),
            ("semantic-coverage", record_id, coverage_path),
            ("semantic-conflicts", record_id, conflicts_path),
        ],
    )
    return accepted
