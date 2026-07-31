from __future__ import annotations

import json
from pathlib import Path

from test_author import _analyzed_workspace, _author_result
from test_review import _review_result

from video_to_skill.config import Settings
from video_to_skill.coordinator import advance_run, submit_workspace_result
from video_to_skill.installation import SkillHost
from video_to_skill.orchestration import RunEnvelope
from video_to_skill.utils import atomic_write_json
from video_to_skill.workspace import Workspace


def _resume(
    workspace: Workspace,
    settings: Settings,
) -> RunEnvelope:
    return advance_run(
        sources=[],
        workspace_path=workspace.root,
        settings=settings,
        host=None,
    )


def test_run_and_submit_complete_without_main_agent_data_forwarding(
    tmp_path: Path,
) -> None:
    workspace, _analyze_task = _analyzed_workspace(tmp_path)
    settings = Settings(cache_root=tmp_path)

    author_envelope = advance_run(
        sources=[],
        workspace_path=workspace.root,
        settings=settings,
        host=SkillHost.CODEX,
        output=tmp_path / "generated" / "evidence-updated-conviction",
        skill_root=tmp_path / "skills",
        run_official_validation=False,
    )

    assert author_envelope.status == "actions-required"
    [author_action] = author_envelope.actions
    assert author_action.role == "author"
    assert "transcript" not in author_envelope.model_dump_json()
    author_task = workspace.get_work_item(author_action.task_id)
    lease = json.loads(
        (author_action.task_path / "lease.json").read_text(encoding="utf-8")
    )
    draft = author_action.task_path / "output" / "course.md"
    draft.write_text(
        "# Evidence-Updated Conviction\n\nUse retrieval, practice, application, and reference.\n",
        encoding="utf-8",
    )
    author_result = _author_result(
        author_task,
        str(lease["lease_token"]),
        draft,
    )
    author_result_path = author_action.task_path / "output" / "result.json"
    atomic_write_json(author_result_path, author_result)
    author_receipt = submit_workspace_result(
        workspace,
        author_task.id,
        author_result_path,
    )
    assert author_receipt.status == "complete"

    review_envelope = _resume(workspace, settings)
    [review_action] = review_envelope.actions
    assert review_action.role == "review"
    review_task = workspace.get_work_item(review_action.task_id)
    review_lease = json.loads(
        (review_action.task_path / "lease.json").read_text(encoding="utf-8")
    )
    review_result = _review_result(
        task_id=review_task.id,
        lease_token=str(review_lease["lease_token"]),
        snapshot_digest=review_task.snapshot_digest,
        reviewed_snapshot_digest=str(review_task.scope["reviewed_snapshot_digest"]),
    )
    review_result_path = review_action.task_path / "output" / "result.json"
    atomic_write_json(review_result_path, review_result)
    review_receipt = submit_workspace_result(
        workspace,
        review_task.id,
        review_result_path,
    )
    assert review_receipt.status == "complete"

    complete = _resume(workspace, settings)

    assert complete.status == "complete"
    assert complete.completion is not None
    assert Path(str(complete.completion["generated_path"])).is_dir()
    assert Path(str(complete.completion["installed_path"])).is_dir()
    assert complete.completion["instructional_affordance_coverage"]["provided"] == 5
    resumed = _resume(workspace, settings)
    assert resumed == complete
