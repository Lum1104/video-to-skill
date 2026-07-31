from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from video_to_skill.config import Settings
from video_to_skill.errors import ProcessingError
from video_to_skill.utils import atomic_write_json
from video_to_skill.work import WorkRole, WorkState
from video_to_skill.workspace import SCHEMA_VERSION, Workspace


def _workspace(tmp_path: Path) -> Workspace:
    return Workspace.create(
        root=tmp_path / "workspace",
        inputs=["demo"],
        settings=Settings(cache_root=tmp_path),
    )


def test_workspace_migrates_orchestration_schema(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    with workspace.connect() as connection:
        tables = {
            str(row["name"])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        version = connection.execute(
            "SELECT value FROM metadata WHERE key='schema_version'"
        ).fetchone()
    assert {
        "analysis_runs",
        "work_items",
        "work_item_dependencies",
        "work_results",
        "canonical_records",
        "canonical_heads",
    } <= tables
    assert version is not None
    assert int(version["value"]) == SCHEMA_VERSION


def test_work_items_are_idempotent_and_dependency_ordered(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    analysis = workspace.create_analysis_run()
    first = workspace.ensure_work_item(
        run_id=analysis.id,
        role=WorkRole.ANALYZE,
        scope={"source_ids": ["source"]},
        persona_hint="Senior semantic analyst.",
        packet={"evidence": []},
        result_schema={"type": "object"},
    )
    repeated = workspace.ensure_work_item(
        run_id=analysis.id,
        role=WorkRole.ANALYZE,
        scope={"source_ids": ["source"]},
        persona_hint="Senior semantic analyst.",
        packet={"evidence": []},
        result_schema={"type": "object"},
    )
    blocked = workspace.ensure_work_item(
        run_id=analysis.id,
        role=WorkRole.AUTHOR,
        scope={"course": "default"},
        persona_hint="Principal curriculum architect.",
        packet={"semantic_map": first.id},
        result_schema={"type": "object"},
        dependencies=[first.id],
    )

    assert repeated.id == first.id
    assert [item.id for item in workspace.ready_work_items(analysis.id)] == [first.id]
    with pytest.raises(ProcessingError, match="dependencies are incomplete"):
        workspace.lease_work_item(blocked.id, owner="host")


def test_expired_lease_returns_to_pending(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    analysis = workspace.create_analysis_run()
    item = workspace.ensure_work_item(
        run_id=analysis.id,
        role=WorkRole.ANALYZE,
        scope={"course": "default"},
        persona_hint="Senior analyst.",
        packet={},
        result_schema={"type": "object"},
    )
    lease = workspace.lease_work_item(item.id, owner="host")
    assert lease.item.state == WorkState.LEASED
    with workspace.connect() as connection:
        connection.execute(
            "UPDATE work_items SET lease_expires_at=? WHERE id=?",
            ((datetime.now(UTC) - timedelta(seconds=1)).isoformat(), item.id),
        )

    [ready] = workspace.ready_work_items(analysis.id)
    assert ready.id == item.id
    assert ready.state == WorkState.PENDING


def test_canonical_records_keep_immutable_revisions(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    analysis = workspace.create_analysis_run()
    item = workspace.ensure_work_item(
        run_id=analysis.id,
        role=WorkRole.AUTHOR,
        scope={"artifact": "guide"},
        persona_hint="Principal curriculum architect.",
        packet={},
        result_schema={"type": "object"},
    )
    draft = workspace.tasks_dir / item.id / "output" / "guide.json"
    atomic_write_json(draft, {"revision": 1})
    first = workspace.publish_canonical_record(
        kind="artifact-spec",
        record_id="guide",
        source_path=draft,
        producer_task_id=item.id,
        snapshot_digest=analysis.snapshot_digest,
    )
    atomic_write_json(draft, {"revision": 2})
    second = workspace.publish_canonical_record(
        kind="artifact-spec",
        record_id="guide",
        source_path=draft,
        producer_task_id=item.id,
        snapshot_digest=analysis.snapshot_digest,
    )

    assert (first.revision, second.revision) == (1, 2)
    assert first.digest != second.digest
    assert first.path != second.path
    assert (workspace.root / first.path).read_text(encoding="utf-8").find('"revision": 1') >= 0
    assert workspace.canonical_record("artifact-spec", "guide") == second
    with sqlite3.connect(workspace.database_path) as connection:
        count = connection.execute(
            "SELECT COUNT(*) FROM canonical_records WHERE kind='artifact-spec'"
        ).fetchone()
    assert count == (2,)


def test_canonical_record_rejects_files_outside_workspace(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    analysis = workspace.create_analysis_run()
    item = workspace.ensure_work_item(
        run_id=analysis.id,
        role=WorkRole.AUTHOR,
        scope={},
        persona_hint="Principal curriculum architect.",
        packet={},
        result_schema={"type": "object"},
    )
    outside = tmp_path / "outside.json"
    atomic_write_json(outside, {})

    with pytest.raises(ProcessingError, match="regular workspace file"):
        workspace.publish_canonical_record(
            kind="artifact-spec",
            record_id="outside",
            source_path=outside,
            producer_task_id=item.id,
            snapshot_digest=analysis.snapshot_digest,
        )
