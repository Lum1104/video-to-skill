from __future__ import annotations

from pathlib import Path

import pytest
from PIL import Image

from video_to_skill import visual_assets
from video_to_skill.config import Settings
from video_to_skill.errors import ProcessingError
from video_to_skill.generation import NormalizedCrop, VisualAssetCandidate
from video_to_skill.models import SourceDescriptor, SourcePlatform, VisualEvent
from video_to_skill.visual_assets import materialize_visual_asset_candidates
from video_to_skill.workspace import Workspace


def _workspace(tmp_path: Path) -> tuple[Workspace, Path, Path]:
    workspace = Workspace.create(
        root=tmp_path / "workspace",
        inputs=["demo"],
        settings=Settings(cache_root=tmp_path),
    )
    workspace.upsert_sources(
        [
            SourceDescriptor(
                id="source",
                platform=SourcePlatform.LOCAL,
                locator="/tmp/demo.mp4",
                title="Visual demo",
                duration=60,
            )
        ]
    )
    first = workspace.root / "frames" / "first.png"
    second = workspace.root / "frames" / "second.png"
    first.parent.mkdir()
    image = Image.new("RGB", (100, 80), "red")
    blue = Image.new("RGB", (50, 80), "blue")
    image.paste(blue, (50, 0))
    image.save(first)
    blue.close()
    image.close()
    Image.new("RGB", (100, 80), "green").save(second)
    workspace.upsert_visuals(
        [
            VisualEvent(id="frame-first", source_id="source", timestamp=10, path=first),
            VisualEvent(id="frame-second", source_id="source", timestamp=20, path=second),
        ]
    )
    return workspace, first, second


def test_materializes_grounded_crop_and_sequence(tmp_path: Path) -> None:
    workspace, _first, _second = _workspace(tmp_path)
    candidates = [
        VisualAssetCandidate(
            id="status-panel",
            source_id="source",
            evidence_ids=["frame-first"],
            semantic_unit_ids=["unit-status"],
            presentation="crop",
            crop=NormalizedCrop(left=0.5, top=0, right=1, bottom=1),
            description="The status panel after the transition",
            teaching_value="The visible state cannot be recovered from speech alone.",
        ),
        VisualAssetCandidate(
            id="before-after",
            source_id="source",
            evidence_ids=["frame-first", "frame-second"],
            semantic_unit_ids=["unit-status"],
            presentation="sequence",
            description="The interface before and after the transition",
            teaching_value="The ordered states make the change observable.",
        ),
    ]

    materialized = materialize_visual_asset_candidates(
        workspace,
        candidates,
        tmp_path / "output",
        record_prefix="default",
    )

    crop, crop_path = materialized[0]
    sequence, sequence_path = materialized[1]
    assert (crop.width, crop.height) == (50, 80)
    with Image.open(crop_path) as image:
        assert image.getpixel((25, 40)) == (0, 0, 255)
        assert not image.getexif()
    assert sequence.width == 212
    assert sequence.height == 80
    assert sequence.timestamps == [10, 20]
    assert sequence.image_record_id == "default:before-after"
    assert sequence_path.is_file()


def test_materializer_rejects_non_visual_and_external_sources(tmp_path: Path) -> None:
    workspace, first, _second = _workspace(tmp_path)
    candidate = VisualAssetCandidate(
        id="missing-frame",
        source_id="source",
        evidence_ids=["transcript-only"],
        semantic_unit_ids=["unit-status"],
        presentation="frame",
        description="A missing frame",
        teaching_value="The candidate must resolve to visual evidence.",
    )
    with pytest.raises(ProcessingError, match="non-visual evidence"):
        materialize_visual_asset_candidates(
            workspace,
            [candidate],
            tmp_path / "output",
            record_prefix="default",
        )

    outside = tmp_path / "outside.png"
    first.replace(outside)
    with pytest.raises(ProcessingError, match="unavailable"):
        materialize_visual_asset_candidates(
            workspace,
            [
                candidate.model_copy(
                    update={
                        "id": "escaped-frame",
                        "evidence_ids": ["frame-first"],
                    }
                )
            ],
            tmp_path / "output",
            record_prefix="default",
        )


def test_materializer_enforces_total_output_bound(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace, _first, _second = _workspace(tmp_path)
    monkeypatch.setattr(visual_assets, "MAX_ASSET_OUTPUT_BYTES", 8)
    candidate = VisualAssetCandidate(
        id="bounded-frame",
        source_id="source",
        evidence_ids=["frame-first"],
        semantic_unit_ids=["unit-status"],
        presentation="frame",
        description="A bounded frame",
        teaching_value="The output must remain within the package asset budget.",
    )

    with pytest.raises(ProcessingError, match="total output byte bound"):
        materialize_visual_asset_candidates(
            workspace,
            [candidate],
            tmp_path / "output",
            record_prefix="default",
        )
