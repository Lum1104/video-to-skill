"""Deterministic product-level analysis-depth recommendation and budgets."""

from __future__ import annotations

import math
from collections.abc import Sequence

from video_to_skill.config import Settings
from video_to_skill.errors import ProcessingError
from video_to_skill.models import (
    AnalysisBudgetSummary,
    AnalysisDepth,
    AnalysisDepthCharacteristics,
    AnalysisDepthContract,
    InspectionCompleteness,
    SourceDescriptor,
    SourceKind,
)
from video_to_skill.utils import stable_hash

ANALYSIS_BUDGET_PROFILE_VERSION = "analysis-depth-budget-v1"
MAX_RETAINED_VISUAL_EVENTS_PER_SOURCE = 10_000
MAX_ANALYZE_PACKET_ITEMS = 3_000
MAX_INVESTIGATION_WINDOW_SECONDS = 300
MAX_INVESTIGATION_WINDOW_FRAMES = 1_800

_VISUAL_SIGNALS = {
    "code",
    "coding",
    "demo",
    "demonstration",
    "diagram",
    "drawing",
    "interface",
    "lab",
    "presentation",
    "screen",
    "slide",
    "tutorial",
    "ui",
    "walkthrough",
    "workshop",
}


def _source_has_visual_signal(source: SourceDescriptor) -> bool:
    values: list[str] = [source.title, source.description or "", source.playlist_title or ""]
    for key in ("tags", "categories"):
        raw = source.metadata.get(key)
        if isinstance(raw, list):
            values.extend(str(item) for item in raw)
        elif isinstance(raw, str):
            values.append(raw)
    tokens = {
        token.strip(".,:;!?()[]{}<>\"'").casefold() for value in values for token in value.split()
    }
    return bool(tokens & _VISUAL_SIGNALS)


def inspect_analysis_characteristics(
    sources: Sequence[SourceDescriptor],
    reports: Sequence[InspectionCompleteness] = (),
) -> AnalysisDepthCharacteristics:
    """Summarize only inspectable source/course characteristics."""

    ordered = sorted(sources, key=lambda item: item.id)
    total_duration = sum(source.duration or 0 for source in ordered)
    known_expected = [report.expected_entries for report in reports]
    expected_item_count = (
        sum(int(value) for value in known_expected if value is not None)
        if reports and all(value is not None for value in known_expected)
        else None
    )
    chapters = sum(len(source.chapters) for source in ordered)
    duration_hours = total_duration / 3600
    chapter_density = chapters / duration_hours if duration_hours > 0 else float(chapters)
    captioned = sum(bool(source.captions) for source in ordered)
    course_sources = sum(source.kind == SourceKind.COURSE for source in ordered)
    visual_sources = sum(_source_has_visual_signal(source) for source in ordered)
    unknown_durations = sum(source.duration is None for source in ordered)

    density_score = 0
    if total_duration >= 4 * 3600:
        density_score += 2
    elif total_duration >= 90 * 60:
        density_score += 1
    if len(ordered) >= 12:
        density_score += 2
    elif len(ordered) >= 4:
        density_score += 1
    if chapters >= 20 or chapter_density >= 6:
        density_score += 2
    elif chapters >= 5 or chapter_density >= 2:
        density_score += 1
    if ordered and visual_sources / len(ordered) >= 0.5:
        density_score += 2
    elif visual_sources:
        density_score += 1
    if course_sources:
        density_score += 1
    if unknown_durations:
        density_score += 1
    if ordered and captioned / len(ordered) < 0.5:
        density_score += 1

    inventory_digest = stable_hash(
        {
            "sources": [
                {
                    "id": source.id,
                    "platform": source.platform.value,
                    "kind": source.kind.value,
                    "duration": source.duration,
                    "chapters": [
                        {"title": chapter.title, "start": chapter.start, "end": chapter.end}
                        for chapter in source.chapters
                    ],
                    "captions": [
                        {
                            "language": caption.language,
                            "extension": caption.extension,
                            "automatic": caption.automatic,
                        }
                        for caption in source.captions
                    ],
                    "visual_signal": _source_has_visual_signal(source),
                }
                for source in ordered
            ],
            "reports": [
                {
                    "locator_digest": stable_hash(report.locator, length=24),
                    "expected": report.expected_entries,
                    "accessible": report.accessible_entries,
                    "inaccessible": report.inaccessible_entries,
                    "failed": report.failed_entries,
                    "complete": report.completeness_proven,
                }
                for report in sorted(reports, key=lambda item: item.locator)
            ],
        },
        length=64,
    )
    return AnalysisDepthCharacteristics(
        inventory_digest=inventory_digest,
        total_duration_seconds=total_duration,
        unknown_duration_sources=unknown_durations,
        source_count=len(ordered),
        expected_item_count=expected_item_count,
        captioned_source_count=captioned,
        chapter_count=chapters,
        chapter_density_per_hour=round(chapter_density, 3),
        course_source_count=course_sources,
        visual_signal_source_count=visual_sources,
        semantic_density_score=min(12, density_score),
    )


def recommend_analysis_depth(
    characteristics: AnalysisDepthCharacteristics,
) -> tuple[AnalysisDepth, list[str]]:
    """Recommend standard/deep; archival remains an explicit cost/storage boundary."""

    recommendation = (
        AnalysisDepth.DEEP
        if characteristics.semantic_density_score >= 4
        else AnalysisDepth.STANDARD
    )
    expected = (
        str(characteristics.expected_item_count)
        if characteristics.expected_item_count is not None
        else "unknown"
    )
    reasons = [
        (
            f"Inspected {characteristics.source_count} accessible source(s), "
            f"{characteristics.total_duration_seconds / 3600:.2f} hour(s), and "
            f"{characteristics.chapter_count} creator chapter(s); expected course items: "
            f"{expected}."
        ),
        (
            f"Caption coverage is {characteristics.captioned_source_count}/"
            f"{characteristics.source_count}; inspectable visual/content signals occur in "
            f"{characteristics.visual_signal_source_count}/"
            f"{characteristics.source_count} source(s)."
        ),
        (
            f"Deterministic semantic-density score "
            f"{characteristics.semantic_density_score}/12 selects {recommendation.value} "
            "(deep begins at 4)."
        ),
        (
            "Archival is never selected implicitly because its frame retention, storage, "
            "and review boundary is material; request archival explicitly when preservation "
            "is the publishing intent."
        ),
    ]
    if characteristics.unknown_duration_sources:
        reasons.insert(
            2,
            f"{characteristics.unknown_duration_sources} source duration(s) are unknown; "
            "the recommendation includes an uncertainty point.",
        )
    return recommendation, reasons


def _clamp_int(value: float, minimum: int, maximum: int) -> int:
    return max(minimum, min(maximum, round(value)))


def _effective_budget(
    *,
    depth: AnalysisDepth,
    characteristics: AnalysisDepthCharacteristics,
    sources: Sequence[SourceDescriptor],
    settings: Settings,
) -> AnalysisBudgetSummary:
    if depth == AnalysisDepth.AUTO:
        raise ValueError("analysis budget requires a concrete depth")
    parameters = {
        AnalysisDepth.STANDARD: {
            "period": 1.50,
            "scene": 1.15,
            "width": 0.80,
            "same_dedup": 6,
            "sequential_dedup": 4,
            "visual_rate": 96,
            "minimum": 1.20,
            "target": 1.40,
            "maximum": 1.20,
            "sections": 8,
            "packet": 1_800,
            "integrated_seconds": 3_600,
            "integrated_sources": 3,
            "integrated_sections": 24,
            "investigation_seconds": 90,
            "investigation_frames": 90,
            "context_seconds": 180,
            "context_items": 250,
        },
        AnalysisDepth.DEEP: {
            "period": 0.80,
            "scene": 0.90,
            "width": 1.00,
            "same_dedup": 5,
            "sequential_dedup": 3,
            "visual_rate": 192,
            "minimum": 1.00,
            "target": 1.00,
            "maximum": 0.90,
            "sections": 6,
            "packet": 2_400,
            "integrated_seconds": 1_800,
            "integrated_sources": 2,
            "integrated_sections": 12,
            "investigation_seconds": 180,
            "investigation_frames": 360,
            "context_seconds": 360,
            "context_items": 500,
        },
        AnalysisDepth.ARCHIVAL: {
            "period": 0.40,
            "scene": 0.72,
            "width": 1.25,
            "same_dedup": 3,
            "sequential_dedup": 1,
            "visual_rate": 384,
            "minimum": 0.75,
            "target": 0.60,
            "maximum": 0.60,
            "sections": 4,
            "packet": 3_000,
            "integrated_seconds": 900,
            "integrated_sources": 1,
            "integrated_sections": 6,
            "investigation_seconds": 300,
            "investigation_frames": 1_200,
            "context_seconds": 600,
            "context_items": 1_000,
        },
    }[depth]
    density_adjustment = 0.85 if characteristics.semantic_density_score >= 7 else 1.0
    visual_sampling = settings.visual_profile != "transcript"
    periodic = (
        _clamp_int(
            settings.periodic_frame_interval * float(parameters["period"]) * density_adjustment,
            5,
            600,
        )
        if visual_sampling
        else None
    )
    scene = (
        round(
            max(
                0.01,
                min(
                    1.0,
                    settings.scene_threshold * float(parameters["scene"]) * density_adjustment,
                ),
            ),
            4,
        )
        if visual_sampling
        else None
    )
    frame_width = (
        _clamp_int(settings.frame_width * float(parameters["width"]), 320, 3840)
        if visual_sampling
        else None
    )
    if frame_width is not None:
        frame_width -= frame_width % 2

    minimum = _clamp_int(settings.min_segment_seconds * float(parameters["minimum"]), 10, 600)
    target = _clamp_int(
        settings.target_segment_seconds * float(parameters["target"]) * density_adjustment,
        30,
        1800,
    )
    maximum = _clamp_int(settings.max_segment_seconds * float(parameters["maximum"]), 60, 3600)
    target = max(minimum, min(target, maximum))
    visual_rate = int(parameters["visual_rate"]) if visual_sampling else 0
    known_durations = [source.duration for source in sources if source.duration is not None]
    unknown_fallback = sum(known_durations) / len(known_durations) if known_durations else 3600.0
    source_limits: dict[str, int] = {}
    for source in sorted(sources, key=lambda item: item.id):
        if not visual_sampling:
            source_limits[source.id] = 0
            continue
        duration = source.duration if source.duration is not None else unknown_fallback
        scaled = math.ceil(max(60.0, duration) / 3600 * visual_rate)
        chapter_allowance = max(4, len(source.chapters) * 2)
        source_limits[source.id] = min(
            MAX_RETAINED_VISUAL_EVENTS_PER_SOURCE,
            scaled + chapter_allowance,
        )

    return AnalysisBudgetSummary(
        profile_version=ANALYSIS_BUDGET_PROFILE_VERSION,
        visual_profile=settings.visual_profile,
        vision_provider=settings.vision_provider,
        visual_sampling_enabled=visual_sampling,
        periodic_frame_interval_seconds=periodic,
        scene_threshold=scene,
        frame_width=frame_width,
        visual_same_moment_dedup_distance=(
            int(parameters["same_dedup"]) if visual_sampling else None
        ),
        visual_sequential_dedup_distance=(
            int(parameters["sequential_dedup"]) if visual_sampling else None
        ),
        visual_events_per_hour_soft_limit=visual_rate,
        source_visual_event_limits=source_limits,
        min_segment_seconds=minimum,
        target_segment_seconds=target,
        max_segment_seconds=maximum,
        analyze_sections_per_task=int(parameters["sections"]),
        analyze_packet_item_limit=min(MAX_ANALYZE_PACKET_ITEMS, int(parameters["packet"])),
        integrated_course_max_seconds=int(parameters["integrated_seconds"]),
        integrated_course_max_sources=int(parameters["integrated_sources"]),
        integrated_course_max_sections=int(parameters["integrated_sections"]),
        investigation_window_seconds=min(
            MAX_INVESTIGATION_WINDOW_SECONDS,
            int(parameters["investigation_seconds"]),
        ),
        investigation_max_frames_per_window=min(
            MAX_INVESTIGATION_WINDOW_FRAMES,
            int(parameters["investigation_frames"]),
        ),
        context_window_seconds=min(600, int(parameters["context_seconds"])),
        context_max_items_per_kind=min(1_000, int(parameters["context_items"])),
        safety_maxima={
            "periodic_frame_interval_seconds": 600,
            "frame_width": 3840,
            "retained_visual_events_per_source": MAX_RETAINED_VISUAL_EVENTS_PER_SOURCE,
            "analyze_packet_items": MAX_ANALYZE_PACKET_ITEMS,
            "investigation_window_seconds": MAX_INVESTIGATION_WINDOW_SECONDS,
            "investigation_window_frames": MAX_INVESTIGATION_WINDOW_FRAMES,
            "context_window_seconds": 600,
            "context_items_per_kind": 1_000,
            "source_duration_seconds": settings.max_source_duration_seconds,
            "course_items": settings.max_course_items,
        },
    )


def resolve_analysis_depth(
    sources: Sequence[SourceDescriptor],
    reports: Sequence[InspectionCompleteness],
    settings: Settings,
    *,
    legacy_compatibility: bool = False,
) -> AnalysisDepthContract:
    characteristics = inspect_analysis_characteristics(sources, reports)
    recommended, reasons = recommend_analysis_depth(characteristics)
    requested = settings.analysis_depth
    effective = recommended if requested == AnalysisDepth.AUTO else requested
    if requested != AnalysisDepth.AUTO:
        reasons.append(
            f"Explicit request selects {effective.value}; the automatic recommendation "
            f"remains {recommended.value}."
        )
    if legacy_compatibility:
        reasons.append(
            "Legacy workspace compatibility resolution was created from its retained source "
            "inventory before new analysis work was planned."
        )
    budget = _effective_budget(
        depth=effective,
        characteristics=characteristics,
        sources=sources,
        settings=settings,
    )
    return AnalysisDepthContract(
        requested=requested,
        recommended=recommended,
        effective=effective,
        recommendation_reasons=reasons,
        characteristics=characteristics,
        budget=budget,
        budget_digest=stable_hash(budget.model_dump(mode="json"), length=64),
        legacy_compatibility=legacy_compatibility,
    )


def verify_analysis_depth_contract(
    contract: AnalysisDepthContract,
    *,
    settings: Settings | None = None,
) -> None:
    """Reject corrupt or version-drifted persisted contracts before reuse."""

    if contract.budget.profile_version != ANALYSIS_BUDGET_PROFILE_VERSION:
        raise ProcessingError(
            "Workspace analysis-depth budget profile differs from this engine version. "
            "Use a new workspace or an explicit migration; resume will not silently drift."
        )
    digest = stable_hash(contract.budget.model_dump(mode="json"), length=64)
    if digest != contract.budget_digest:
        raise ProcessingError("Workspace analysis-depth budget failed its digest check")
    if (
        settings is not None
        and settings.analysis_depth_explicit
        and contract.requested != settings.analysis_depth
    ):
        raise ProcessingError(
            f"Resume analysis depth {settings.analysis_depth.value} conflicts with persisted "
            f"request {contract.requested.value}. Use the persisted depth or a new workspace."
        )


def resolve_workspace_analysis_depth(
    existing: AnalysisDepthContract | None,
    sources: Sequence[SourceDescriptor],
    reports: Sequence[InspectionCompleteness],
    settings: Settings,
    *,
    refresh: bool,
    legacy_compatibility_if_missing: bool = True,
) -> AnalysisDepthContract:
    """Resolve once, reuse on resume, and recompute only for an explicit refresh."""

    if existing is None:
        return resolve_analysis_depth(
            sources,
            reports,
            settings,
            legacy_compatibility=legacy_compatibility_if_missing,
        )
    verify_analysis_depth_contract(existing, settings=settings)
    resolution_settings = settings
    if not settings.analysis_depth_explicit:
        resolution_settings = settings.model_copy(update={"analysis_depth": existing.requested})
    if not refresh:
        current = inspect_analysis_characteristics(sources, reports)
        if current.inventory_digest != existing.characteristics.inventory_digest:
            raise ProcessingError(
                "Source inventory or inspectable density changed since analysis depth was "
                "resolved. Resume with --refresh to recompute the effective budget contract."
            )
        recomputed = resolve_analysis_depth(sources, reports, resolution_settings)
        if (
            recomputed.recommended != existing.recommended
            or recomputed.effective != existing.effective
            or recomputed.budget != existing.budget
            or recomputed.budget_digest != existing.budget_digest
        ):
            raise ProcessingError(
                "Workspace analysis-depth profile would drift under this engine. Use a new "
                "workspace or an explicit migration; resume will not rewrite analyzed budgets."
            )
        return existing
    return resolve_analysis_depth(sources, reports, resolution_settings)


def effective_source_settings(
    settings: Settings,
    contract: AnalysisDepthContract,
    *,
    source_id: str | None = None,
) -> Settings:
    """Project the durable global extraction budget onto existing low-level settings."""

    verify_analysis_depth_contract(contract)
    budget = contract.budget
    updates: dict[str, int | float] = {
        "min_segment_seconds": budget.min_segment_seconds,
        "target_segment_seconds": budget.target_segment_seconds,
        "max_segment_seconds": budget.max_segment_seconds,
    }
    if budget.visual_sampling_enabled:
        assert budget.periodic_frame_interval_seconds is not None
        assert budget.scene_threshold is not None
        assert budget.frame_width is not None
        updates.update(
            periodic_frame_interval=budget.periodic_frame_interval_seconds,
            scene_threshold=budget.scene_threshold,
            frame_width=budget.frame_width,
        )
    effective = settings.model_copy(update=updates)
    effective._analysis_budget = budget
    effective._analysis_visual_event_limit = (
        budget.source_visual_event_limits.get(source_id) if source_id is not None else None
    )
    return effective
