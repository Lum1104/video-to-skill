from pathlib import Path

from video_to_skill.config import Settings
from video_to_skill.evaluation import EvaluationLabels, SourceLabels, evaluate_workspace
from video_to_skill.models import (
    SemanticSegment,
    SourceDescriptor,
    SourcePlatform,
    VisualEvent,
)
from video_to_skill.workspace import Workspace


def test_evaluation_matches_events_with_tolerance(tmp_path: Path) -> None:
    workspace = Workspace.create(
        root=tmp_path / "workspace",
        inputs=["demo"],
        settings=Settings(cache_root=tmp_path),
    )
    source = SourceDescriptor(
        id="source",
        platform=SourcePlatform.LOCAL,
        locator="/tmp/demo.mp4",
        title="Demo",
    )
    workspace.upsert_sources([source])
    workspace.replace_visuals(
        source.id,
        [
            VisualEvent(
                id="visual-1",
                source_id=source.id,
                timestamp=10.2,
                path=tmp_path / "frame.jpg",
            )
        ],
    )
    workspace.replace_semantic_segments(
        source.id,
        [
            SemanticSegment(
                id="section-1",
                source_id=source.id,
                ordinal=1,
                title="Start",
                start=0,
                end=30,
            ),
            SemanticSegment(
                id="section-2",
                source_id=source.id,
                ordinal=2,
                title="Next",
                start=30,
                end=60,
            ),
        ],
    )
    labels = EvaluationLabels(
        sources=[
            SourceLabels(
                source_id=source.id,
                critical_visual_timestamps=[10],
                semantic_boundary_timestamps=[29.5],
            )
        ]
    )
    report = evaluate_workspace(workspace, labels, tolerance_seconds=1)
    assert report.passed
    assert report.visual_recall == 1
    assert report.boundary_f1 == 1


def test_evaluation_fails_visual_recall_threshold(tmp_path: Path) -> None:
    workspace = Workspace.create(
        root=tmp_path / "workspace",
        inputs=["demo"],
        settings=Settings(cache_root=tmp_path),
    )
    source = SourceDescriptor(
        id="source",
        platform=SourcePlatform.LOCAL,
        locator="/tmp/demo.mp4",
        title="Demo",
    )
    workspace.upsert_sources([source])
    labels = EvaluationLabels(
        sources=[
            SourceLabels(
                source_id=source.id,
                critical_visual_timestamps=[10],
            )
        ]
    )
    report = evaluate_workspace(workspace, labels)
    assert not report.passed
    assert report.visual_recall == 0
