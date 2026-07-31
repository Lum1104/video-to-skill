from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
from PIL import Image
from typer.testing import CliRunner

from video_to_skill.cli import app
from video_to_skill.config import Settings
from video_to_skill.models import (
    SemanticSegment,
    SourceDescriptor,
    SourcePlatform,
    TranscriptOrigin,
    TranscriptSegment,
    VisualEvent,
    VisualKind,
)
from video_to_skill.utils import run_command
from video_to_skill.workspace import Workspace

runner = CliRunner()


def _evidence_workspace(tmp_path: Path) -> Workspace:
    workspace = Workspace.create(
        root=tmp_path / "workspace",
        inputs=["demo"],
        settings=Settings(cache_root=tmp_path),
    )
    source = SourceDescriptor(
        id="source",
        platform=SourcePlatform.LOCAL,
        locator=str(tmp_path / "demo.mp4"),
        title="Agent Demo",
        duration=120,
    )
    workspace.upsert_sources([source])
    transcript = TranscriptSegment(
        id="transcript",
        source_id=source.id,
        start=5,
        end=12,
        text="Click Save and verify that the status changes to complete.",
        origin=TranscriptOrigin.MANUAL_CAPTION,
    )
    frame_path = workspace.source_directory(source.id) / "frames" / "state.jpg"
    frame_path.parent.mkdir(parents=True)
    Image.new("RGB", (640, 360), "#2855a5").save(frame_path)
    visual = VisualEvent(
        id="visual",
        source_id=source.id,
        timestamp=9,
        path=frame_path,
        kind=VisualKind.UI,
    )
    segment = SemanticSegment(
        id="section",
        source_id=source.id,
        ordinal=1,
        title="Save state",
        start=0,
        end=30,
        transcript_ids=[transcript.id],
        visual_event_ids=[visual.id],
    )
    workspace.replace_transcripts(source.id, [transcript])
    workspace.replace_visuals(source.id, [visual])
    workspace.replace_semantic_segments(source.id, [segment])
    workspace.create_analysis_run(settings=Settings(cache_root=tmp_path))
    return workspace


def test_contact_sheet_and_context_commands(tmp_path: Path) -> None:
    workspace = _evidence_workspace(tmp_path)

    sheet_result = runner.invoke(
        app,
        [
            "contact-sheet",
            str(workspace.root),
            "--source",
            "1",
            "--section",
            "1",
            "--json",
        ],
    )
    assert sheet_result.exit_code == 0, sheet_result.output
    sheet_payload = json.loads(sheet_result.stdout)
    assert Path(sheet_payload["path"]).is_file()
    assert sheet_payload["events_rendered"] == 1

    explicit = tmp_path / "explicit.jpg"
    first_explicit = runner.invoke(
        app,
        [
            "contact-sheet",
            str(workspace.root),
            "--source",
            "1",
            "--output",
            str(explicit),
        ],
    )
    assert first_explicit.exit_code == 0, first_explicit.output
    refused = runner.invoke(
        app,
        [
            "contact-sheet",
            str(workspace.root),
            "--source",
            "1",
            "--output",
            str(explicit),
        ],
    )
    assert refused.exit_code == 2
    assert "Use --force" in refused.output

    context_result = runner.invoke(
        app,
        [
            "context",
            str(workspace.root),
            "--source",
            "Agent Demo",
            "--section",
            "1",
            "--format",
            "json",
        ],
    )
    assert context_result.exit_code == 0, context_result.output
    context_payload = json.loads(context_result.stdout)
    assert context_payload["source"]["id"] == "source"
    assert context_payload["window"] == {"start": 0.0, "end": 30.0}
    assert context_payload["transcripts"][0]["id"] == "transcript"
    assert context_payload["visuals"][0]["id"] == "visual"


def test_annotate_and_gaps_commands_persist_agent_state(tmp_path: Path) -> None:
    workspace = _evidence_workspace(tmp_path)
    annotations = tmp_path / "observations.json"
    annotations.write_text(
        json.dumps(
            {
                "observations": [
                    {
                        "source_id": "source",
                        "start": 5,
                        "end": 12,
                        "type": "ui",
                        "claim": "The visible interface contains the Save control.",
                        "frame_ids": ["visual"],
                        "transcript_ids": ["transcript"],
                        "confidence": 0.93,
                        "status": "observed",
                        "uncertainty": None,
                        "producer": {"name": "test-host", "run_id": "run-1"},
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    annotate_result = runner.invoke(
        app,
        ["annotate", str(workspace.root), str(annotations), "--json"],
    )
    assert annotate_result.exit_code == 0, annotate_result.output
    stored = json.loads(annotate_result.stdout)
    assert stored[0]["id"].startswith("obs_")
    assert Workspace.open(workspace.root).observations("source")[0].claim.startswith("The visible")

    gaps_result = runner.invoke(
        app,
        [
            "gaps",
            str(workspace.root),
            "--source",
            "source",
            "--format",
            "json",
        ],
    )
    assert gaps_result.exit_code == 0, gaps_result.output
    gaps = json.loads(gaps_result.stdout)
    assert all(gap["source_id"] == "source" for gap in gaps)


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="FFmpeg unavailable")
def test_frames_command_persists_dense_visual_events(tmp_path: Path) -> None:
    workspace = _evidence_workspace(tmp_path)
    media = tmp_path / "dense source.mkv"
    run_command(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=640x360:rate=10:duration=2",
            "-c:v",
            "ffv1",
            str(media),
        ],
        timeout=30,
    )
    workspace.update_materialization(
        "source",
        content_hash="content",
        source_dir=workspace.source_directory("source"),
        media_path=media,
        caption_paths=[],
    )
    source = workspace.get_source("source")
    source.duration = 2
    workspace.upsert_sources([source])

    result = runner.invoke(
        app,
        [
            "frames",
            str(workspace.root),
            "--source",
            "source",
            "--from",
            "0.25",
            "--to",
            "1.25",
            "--fps",
            "2",
            "--no-deduplicate",
            "--json",
        ],
    )
    assert result.exit_code == 0, result.output
    extracted = json.loads(result.stdout)
    assert [item["timestamp"] for item in extracted] == pytest.approx([0.25, 0.75])
    stored_ids = {item.id for item in Workspace.open(workspace.root).visuals("source")}
    assert {"visual", *(item["id"] for item in extracted)} <= stored_ids
