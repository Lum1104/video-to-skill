from __future__ import annotations

import json
from pathlib import Path

import pytest
from test_author import _analyzed_workspace, _curriculum_result

from video_to_skill.curriculum import plan_curriculum_task, submit_curriculum_plan_result
from video_to_skill.errors import ProcessingError
from video_to_skill.utils import atomic_write_json
from video_to_skill.work import WorkState


def test_curriculum_plan_automatically_selects_recommended_path(tmp_path: Path) -> None:
    workspace, analyze_task = _analyzed_workspace(tmp_path)
    run = workspace.create_analysis_run(analyze_task.snapshot_digest)
    task = plan_curriculum_task(workspace, run, analyze_task=analyze_task)
    lease = workspace.lease_work_item(task.id, owner="codex")
    result_path = lease.output_directory / "result.json"
    atomic_write_json(result_path, _curriculum_result(task, lease.token))

    accepted = submit_curriculum_plan_result(workspace, task.id, result_path)

    assert accepted.state == WorkState.COMPLETE
    options_record = workspace.canonical_record("curriculum-options")
    selection_record = workspace.canonical_record("selected-curriculum")
    assert options_record is not None
    assert selection_record is not None
    selection = json.loads((workspace.root / selection_record.path).read_text(encoding="utf-8"))
    assert selection == {
        "schema_version": 1,
        "curriculum_plan_digest": options_record.digest,
        "selected_path_id": "thematic",
        "source": "recommended",
    }


def test_material_curriculum_plan_waits_for_user_selection(tmp_path: Path) -> None:
    workspace, analyze_task = _analyzed_workspace(tmp_path)
    run = workspace.create_analysis_run(analyze_task.snapshot_digest)
    task = plan_curriculum_task(workspace, run, analyze_task=analyze_task)
    lease = workspace.lease_work_item(task.id, owner="codex")
    result_path = lease.output_directory / "result.json"
    atomic_write_json(
        result_path,
        _curriculum_result(task, lease.token, decision_required=True),
    )

    submit_curriculum_plan_result(workspace, task.id, result_path)

    assert workspace.canonical_record("curriculum-options") is not None
    assert workspace.canonical_record("selected-curriculum") is None
    assert workspace.canonical_record("artifact-plan") is None


def test_curriculum_plan_rejects_unknown_units_and_packet_drift(tmp_path: Path) -> None:
    workspace, analyze_task = _analyzed_workspace(tmp_path)
    run = workspace.create_analysis_run(analyze_task.snapshot_digest)
    task = plan_curriculum_task(workspace, run, analyze_task=analyze_task)
    lease = workspace.lease_work_item(task.id, owner="codex")
    payload = _curriculum_result(task, lease.token).model_dump(mode="json")
    payload["curriculum"]["paths"][0]["unit_sequence"] = ["invented-unit"]
    result_path = lease.output_directory / "result.json"
    atomic_write_json(result_path, payload)

    with pytest.raises(ProcessingError, match="unknown semantic units"):
        submit_curriculum_plan_result(workspace, task.id, result_path)

    packet_path = workspace.root / task.packet_path
    packet = json.loads(packet_path.read_text(encoding="utf-8"))
    packet["payload"]["instructions"] = "tampered"
    atomic_write_json(packet_path, packet)
    atomic_write_json(result_path, _curriculum_result(task, lease.token))
    with pytest.raises(ProcessingError, match="packet failed its digest check"):
        submit_curriculum_plan_result(workspace, task.id, result_path)
