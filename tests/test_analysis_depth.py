from __future__ import annotations

from copy import deepcopy

import pytest

from video_to_skill.analysis_depth import (
    resolve_analysis_depth,
    resolve_workspace_analysis_depth,
    verify_analysis_depth_contract,
)
from video_to_skill.config import Settings
from video_to_skill.errors import ProcessingError
from video_to_skill.models import (
    CaptionTrack,
    Chapter,
    SourceDescriptor,
    SourceKind,
    SourcePlatform,
)


def _source(
    source_id: str,
    *,
    duration: float,
    title: str = "Interview",
    chapters: int = 0,
    captions: bool = True,
    course: bool = False,
) -> SourceDescriptor:
    return SourceDescriptor(
        id=source_id,
        platform=SourcePlatform.YOUTUBE,
        kind=SourceKind.COURSE if course else SourceKind.VIDEO,
        locator=f"https://youtu.be/{source_id}",
        canonical_url=f"https://youtu.be/{source_id}",
        title=title,
        duration=duration,
        chapters=[
            Chapter(title=f"Chapter {index}", start=float(index * 300)) for index in range(chapters)
        ],
        captions=(
            [CaptionTrack(language="en", extension="vtt", automatic=False)] if captions else []
        ),
    )


def test_auto_recommendation_is_deterministic_and_never_silently_archival() -> None:
    sparse = [_source("sparse", duration=15 * 60)]
    first = resolve_analysis_depth(sparse, [], Settings())
    second = resolve_analysis_depth(deepcopy(sparse), [], Settings())

    assert first == second
    assert first.requested == "auto"
    assert first.recommended == "standard"
    assert first.effective == "standard"
    assert any(
        "Archival is never selected implicitly" in reason for reason in first.recommendation_reasons
    )

    dense = [
        _source(
            f"lesson-{index}",
            duration=2 * 3600,
            title="Coding UI workshop",
            chapters=8,
            course=True,
        )
        for index in range(6)
    ]
    recommended = resolve_analysis_depth(dense, [], Settings())

    assert recommended.recommended == "deep"
    assert recommended.effective == "deep"
    assert recommended.characteristics.semantic_density_score >= 4


def test_depth_changes_visual_semantic_retention_and_investigation_budgets() -> None:
    sources = [_source("course", duration=3 * 3600, title="Software demo", chapters=12)]
    contracts = {
        depth: resolve_analysis_depth(
            sources,
            [],
            Settings(analysis_depth=depth, vision_provider="none"),
        )
        for depth in ("standard", "deep", "archival")
    }
    standard = contracts["standard"].budget
    deep = contracts["deep"].budget
    archival = contracts["archival"].budget

    assert (
        standard.periodic_frame_interval_seconds
        > deep.periodic_frame_interval_seconds
        > archival.periodic_frame_interval_seconds
    )
    assert standard.frame_width < deep.frame_width < archival.frame_width
    assert (
        standard.source_visual_event_limits["course"]
        < deep.source_visual_event_limits["course"]
        < archival.source_visual_event_limits["course"]
    )
    assert (
        standard.target_segment_seconds
        > deep.target_segment_seconds
        > archival.target_segment_seconds
    )
    assert (
        standard.analyze_sections_per_task
        > deep.analyze_sections_per_task
        > archival.analyze_sections_per_task
    )
    assert (
        standard.investigation_max_frames_per_window
        < deep.investigation_max_frames_per_window
        < archival.investigation_max_frames_per_window
    )
    assert all(contract.budget.vision_provider == "none" for contract in contracts.values())


def test_transcript_profile_disables_visual_budgets_at_archival_depth() -> None:
    contract = resolve_analysis_depth(
        [_source("course", duration=3600, title="UI tutorial")],
        [],
        Settings(
            analysis_depth="archival",
            visual_profile="transcript",
            vision_provider="none",
        ),
    )

    assert contract.effective == "archival"
    assert contract.budget.visual_sampling_enabled is False
    assert contract.budget.periodic_frame_interval_seconds is None
    assert contract.budget.source_visual_event_limits == {"course": 0}
    assert contract.budget.vision_provider == "none"


def test_resume_reuses_contract_and_rejects_conflict_drift_and_inventory_change() -> None:
    sources = [_source("one", duration=3600)]
    settings = Settings(analysis_depth="deep")
    contract = resolve_analysis_depth(sources, [], settings)

    reused = resolve_workspace_analysis_depth(
        contract,
        sources,
        [],
        settings,
        refresh=False,
    )
    assert reused == contract

    with pytest.raises(ProcessingError, match="conflicts with persisted request"):
        resolve_workspace_analysis_depth(
            contract,
            sources,
            [],
            Settings(analysis_depth="standard"),
            refresh=False,
        )

    drifted = contract.model_copy(
        update={"budget": contract.budget.model_copy(update={"profile_version": "future-profile"})}
    )
    with pytest.raises(ProcessingError, match="profile differs"):
        verify_analysis_depth_contract(drifted)

    changed = [_source("one", duration=7200)]
    with pytest.raises(ProcessingError, match="--refresh"):
        resolve_workspace_analysis_depth(
            contract,
            changed,
            [],
            settings,
            refresh=False,
        )
    refreshed = resolve_workspace_analysis_depth(
        contract,
        changed,
        [],
        settings,
        refresh=True,
    )
    assert refreshed.characteristics.inventory_digest != contract.characteristics.inventory_digest
    assert refreshed.budget_digest != contract.budget_digest
