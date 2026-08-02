from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

import pytest
from test_author import _analyzed_workspace, _author_result, _curriculum_result
from test_editions import (
    _edition_view,
    _resume_edition,
    _workspace_with_material_curriculum,
)

from video_to_skill.config import Settings
from video_to_skill.coordinator import advance_run, submit_workspace_result
from video_to_skill.editions import (
    LEGACY_EDITION_NAME,
    create_edition_state,
    current_analysis_lineage,
    edition_id_for,
    identity_baseline,
    load_edition_state,
)
from video_to_skill.errors import ProcessingError
from video_to_skill.installation import SkillHost
from video_to_skill.models import ObservationProducer, ProcessingStage
from video_to_skill.utils import atomic_write_json
from video_to_skill.workspace import Workspace


def _new_reused_edition(
    workspace: Workspace,
    settings: Settings,
    tmp_path: Path,
    *,
    name: str,
):
    return advance_run(
        sources=[],
        workspace_path=workspace.root,
        settings=settings,
        output_language_override="English",
        host=SkillHost.CODEX,
        output=tmp_path / "generated" / name,
        skill_root=tmp_path / "skills",
        run_official_validation=False,
        edition_name=name,
        curriculum_path_id="thematic",
        skill_name=name,
    )


def _submit_planned_curriculum(
    base: Workspace,
    view: Workspace,
    settings: Settings,
    edition_name: str,
    envelope,
):
    [action] = envelope.actions
    task = view.get_work_item(action.task_id)
    lease = json.loads((action.task_path / "lease.json").read_text(encoding="utf-8"))
    result_path = action.task_path / "output" / "result.json"
    atomic_write_json(
        result_path,
        _curriculum_result(task, str(lease["lease_token"]), decision_required=False),
    )
    submit_workspace_result(base, task.id, result_path)
    return _resume_edition(base, settings, edition_name)


def _write_author_result(
    view: Workspace,
    envelope,
    *,
    skill_name: str,
    mutate_identity: bool = False,
    late_invalid_evidence: bool = False,
) -> tuple[str, Path]:
    [action] = envelope.actions
    task = view.get_work_item(action.task_id)
    lease = json.loads((action.task_path / "lease.json").read_text(encoding="utf-8"))
    draft = action.task_path / "output" / "course.md"
    draft.write_text("# Course\n\nA grounded course.\n", encoding="utf-8")
    result = _author_result(task, str(lease["lease_token"]), draft).model_copy(
        update={"name": skill_name}
    )
    if mutate_identity:
        artifact = result.artifacts[0].model_copy(update={"id": "artifact-mutated"})
        claim = result.claims[0].model_copy(update={"id": "claim-mutated"})
        curriculum = result.curriculum.model_copy(
            update={
                "paths": [
                    path.model_copy(update={"artifact_ids": [artifact.id]})
                    for path in result.curriculum.paths
                ]
            }
        )
        affordances = [
            item.model_copy(
                update={
                    "artifact_ids": [artifact.id] if item.artifact_ids else [],
                }
            )
            for item in result.affordance_ledger
        ]
        result = result.model_copy(
            update={
                "artifacts": [artifact],
                "claims": [claim],
                "curriculum": curriculum,
                "affordance_ledger": affordances,
            }
        )
    if late_invalid_evidence:
        claim = result.claims[0]
        claim = claim.model_copy(
            update={
                "evidence": [
                    evidence.model_copy(update={"evidence_ids": ["invented-evidence"]})
                    for evidence in claim.evidence
                ]
            }
        )
        result = result.model_copy(update={"claims": [claim]})
    result_path = action.task_path / "output" / "result.json"
    atomic_write_json(result_path, result)
    return task.id, result_path


def test_edition_view_cannot_read_or_mutate_another_editions_task_or_heads(
    tmp_path: Path,
) -> None:
    base, settings, _analyze_task_id = _workspace_with_material_curriculum(tmp_path)
    _new_reused_edition(base, settings, tmp_path, name="left-edition")
    right = _new_reused_edition(base, settings, tmp_path, name="right-edition")
    left_view = _edition_view(base, "left-edition")
    right_view = _edition_view(base, "right-edition")
    [right_action] = right.actions
    right_task_id = right_action.task_id

    with pytest.raises(ProcessingError, match=r"(?i)edition|scope|owned"):
        left_view.get_work_item(right_task_id)
    with pytest.raises(ProcessingError, match=r"(?i)edition|scope|owned"):
        left_view.lease_work_item(right_task_id, owner="attacker")
    with pytest.raises(ProcessingError, match=r"(?i)edition|scope|owned"):
        left_view.fail_work_item(right_task_id, "cross-edition sabotage")

    right_lease = json.loads((right_action.task_path / "lease.json").read_text(encoding="utf-8"))
    forged_result = right_action.task_path / "output" / "forged-result.json"
    atomic_write_json(forged_result, {"forged": True})
    with pytest.raises(ProcessingError, match=r"(?i)edition|scope|owned"):
        left_view.accept_work_result(
            task_id=right_task_id,
            lease_token=str(right_lease["lease_token"]),
            result_path=forged_result,
            producer=ObservationProducer(name="attacker").model_dump(mode="json"),
        )

    original_head = left_view.canonical_record("selected-curriculum")
    right_head = right_view.canonical_record("selected-curriculum")
    assert original_head is not None and right_head is not None
    right_head_producer = right_view.get_work_item(right_head.producer_task_id)
    with pytest.raises(ProcessingError, match=r"(?i)edition|scope|owned"):
        left_view.publish_canonical_record(
            kind="selected-curriculum",
            record_id="default",
            source_path=base.root / right_head.path,
            producer_task_id=right_head_producer.id,
            snapshot_digest=right_head_producer.snapshot_digest,
        )
    assert left_view.canonical_record("selected-curriculum") == original_head

    with pytest.raises(ProcessingError, match=r"(?i)edition|stage|evidence"):
        left_view.start_stage("source", ProcessingStage.TRANSCRIBE, "attacker-cache-key")


def test_analyze_only_editions_share_baseline_and_do_not_auto_authorize_drift(
    tmp_path: Path,
) -> None:
    base, _analyze_task = _analyzed_workspace(tmp_path)
    settings = Settings(cache_root=tmp_path)

    first = advance_run(
        sources=[],
        workspace_path=base.root,
        settings=settings,
        output_language_override="English",
        host=SkillHost.CODEX,
        output=tmp_path / "generated" / "first",
        skill_root=tmp_path / "skills",
        run_official_validation=False,
        edition_name="first-planned",
        plan_curriculum=True,
        skill_name="first-planned",
    )
    first_view = _edition_view(base, "first-planned")
    first_author = _submit_planned_curriculum(base, first_view, settings, "first-planned", first)
    poisoned_task_id, poisoned_result_path = _write_author_result(
        first_view,
        first_author,
        skill_name="first-planned",
        mutate_identity=True,
        late_invalid_evidence=True,
    )
    with pytest.raises(ProcessingError, match=r"(?i)evidence|ground"):
        submit_workspace_result(base, poisoned_task_id, poisoned_result_path)

    first_task_id, first_result_path = _write_author_result(
        first_view,
        first_author,
        skill_name="first-planned",
    )
    submit_workspace_result(base, first_task_id, first_result_path)

    second = advance_run(
        sources=[],
        workspace_path=base.root,
        settings=settings,
        output_language_override="English",
        host=SkillHost.CODEX,
        output=tmp_path / "generated" / "second",
        skill_root=tmp_path / "skills",
        run_official_validation=False,
        edition_name="second-planned",
        plan_curriculum=True,
        skill_name="second-planned",
    )
    second_view = _edition_view(base, "second-planned")
    second_author = _submit_planned_curriculum(
        base, second_view, settings, "second-planned", second
    )
    first_state = load_edition_state(base, "first-planned")
    second_state = load_edition_state(base, "second-planned")
    assert first_state.configuration.identity_drift_justification is None
    assert second_state.configuration.identity_drift_justification is None
    first_baseline = first_state.configuration.identity_baseline.model_dump(mode="json")
    second_baseline = second_state.configuration.identity_baseline.model_dump(mode="json")
    assert first_baseline
    assert second_baseline
    assert first_baseline.get("family_id") == second_baseline.get("family_id")
    assert first_baseline.get("family_id")

    second_task_id, second_result_path = _write_author_result(
        second_view,
        second_author,
        skill_name="second-planned",
        mutate_identity=True,
    )
    with pytest.raises(ProcessingError, match=r"(?i)identity|artifact|claim"):
        submit_workspace_result(base, second_task_id, second_result_path)


def test_wrong_lease_token_cannot_poison_first_identity_baseline(tmp_path: Path) -> None:
    base, _analyze_task = _analyzed_workspace(tmp_path)
    settings = Settings(cache_root=tmp_path)
    planned = advance_run(
        sources=[],
        workspace_path=base.root,
        settings=settings,
        output_language_override="English",
        host=SkillHost.CODEX,
        output=tmp_path / "generated" / "token-baseline",
        skill_root=tmp_path / "skills",
        run_official_validation=False,
        edition_name="token-baseline",
        plan_curriculum=True,
        skill_name="token-baseline",
    )
    view = _edition_view(base, "token-baseline")
    author = _submit_planned_curriculum(base, view, settings, "token-baseline", planned)
    poisoned_task_id, poisoned_path = _write_author_result(
        view,
        author,
        skill_name="token-baseline",
        mutate_identity=True,
    )
    poisoned = json.loads(poisoned_path.read_text(encoding="utf-8"))
    poisoned["lease_token"] = "wrong-but-structurally-valid-token"
    atomic_write_json(poisoned_path, poisoned)

    family_id = load_edition_state(base, "token-baseline").configuration.identity_baseline.family_id
    with pytest.raises(ProcessingError, match=r"(?i)lease token|invalid"):
        submit_workspace_result(base, poisoned_task_id, poisoned_path)
    assert view.identity_baseline_payload(family_id) is None

    valid_task_id, valid_path = _write_author_result(
        view,
        author,
        skill_name="token-baseline",
    )
    submit_workspace_result(base, valid_task_id, valid_path)
    stored = view.identity_baseline_payload(family_id)
    assert stored is not None
    assert stored["artifact_ids"] == ["artifact-course"]
    assert stored["claim_ids"] == ["claim-conviction"]


def test_all_edition_descendant_write_leaves_reject_symlink_parents(
    tmp_path: Path,
) -> None:
    base, settings, _analyze_task_id = _workspace_with_material_curriculum(tmp_path)
    envelope = _new_reused_edition(base, settings, tmp_path, name="leaf-safety")
    view = _edition_view(base, "leaf-safety")
    [action] = envelope.actions

    task_escape = tmp_path / "task-output-escape"
    task_escape.mkdir()
    task_output = action.task_path / "output"
    task_output.rmdir()
    task_output.symlink_to(task_escape, target_is_directory=True)
    with pytest.raises(ProcessingError, match=r"(?i)unsafe|symlink|namespace"):
        view.write_json(task_output / "result.json", {"attack": True})
    assert list(task_escape.iterdir()) == []
    task_output.unlink()
    view.ensure_directory(task_output)

    for label, symlink_path, target in (
        (
            "results",
            view.analysis_dir / "results" / f"{action.task_id}.json",
            view.analysis_dir / "results" / f"{action.task_id}.json",
        ),
        (
            "records",
            view.analysis_dir / "records" / "kind-hash",
            view.analysis_dir / "records" / "kind-hash" / "id-hash" / "r1.json",
        ),
        (
            "build",
            view.builds_dir / "build-adversarial",
            view.builds_dir / "build-adversarial" / "blueprint.json",
        ),
    ):
        escape = tmp_path / f"{label}-escape"
        escape.mkdir()
        symlink_path.parent.mkdir(parents=True, exist_ok=True)
        symlink_path.symlink_to(
            escape,
            target_is_directory=symlink_path != target,
        )
        with pytest.raises(ProcessingError, match=r"(?i)unsafe|symlink|namespace"):
            view.write_json(target, {"attack": True})
        assert list(escape.iterdir()) == []


def test_edition_run_config_tampering_fails_closed(tmp_path: Path) -> None:
    base, settings, _analyze_task_id = _workspace_with_material_curriculum(tmp_path)
    _new_reused_edition(base, settings, tmp_path, name="delivery-edition")
    view = _edition_view(base, "delivery-edition")
    run_config_path = view.analysis_dir / "run-config.json"
    run_config = json.loads(run_config_path.read_text(encoding="utf-8"))
    attacker_output = tmp_path / "attacker-output"
    attacker_skill_root = tmp_path / "attacker-skills"
    run_config["output"] = str(attacker_output)
    run_config["skill_root"] = str(attacker_skill_root)
    run_config["host"] = SkillHost.CLAUDE.value
    atomic_write_json(run_config_path, run_config)

    with pytest.raises(ProcessingError, match=r"(?i)edition|configuration|authority|digest"):
        _resume_edition(base, settings, "delivery-edition")
    assert not attacker_output.exists()
    assert not attacker_skill_root.exists()


@pytest.mark.parametrize("symlink_level", ["editions", "edition"])
def test_edition_creation_rejects_symlinked_namespace_parent(
    tmp_path: Path,
    symlink_level: str,
) -> None:
    base, _analyze_task = _analyzed_workspace(tmp_path)
    settings = Settings(cache_root=tmp_path)
    escape = tmp_path / "outside-edition-namespace"
    escape.mkdir()
    if symlink_level == "editions":
        base.editions_dir.symlink_to(escape, target_is_directory=True)
    else:
        base.editions_dir.mkdir()
        edition_id = edition_id_for(base, "symlink-attack")
        (base.editions_dir / edition_id).symlink_to(escape, target_is_directory=True)

    with pytest.raises(ProcessingError, match=r"(?i)symlink|namespace|directory|edition"):
        advance_run(
            sources=[],
            workspace_path=base.root,
            settings=settings,
            output_language_override="English",
            host=SkillHost.CODEX,
            output=tmp_path / "generated" / "symlink-attack",
            skill_root=tmp_path / "skills",
            run_official_validation=False,
            edition_name="symlink-attack",
            plan_curriculum=True,
            skill_name="symlink-attack",
        )
    assert list(escape.iterdir()) == []


def test_same_name_creation_is_compare_and_set_and_conflicts_do_not_overwrite(
    tmp_path: Path,
) -> None:
    base, _analyze_task = _analyzed_workspace(tmp_path)
    barrier = Barrier(2)

    def create(output_name: str):
        barrier.wait()
        return create_edition_state(
            base,
            edition_name="racing-edition",
            requested_output_language="English",
            curriculum_mode="plan",
            curriculum_source_edition=None,
            source_curriculum_plan_digest=None,
            requested_curriculum_path_id=None,
            skill_name="racing-edition",
            host="codex",
            output=tmp_path / output_name,
            output_is_default=False,
            project=False,
            project_root=tmp_path,
            skill_root=tmp_path / "skills",
            run_official_validation=False,
            baseline=identity_baseline(base),
            identity_drift_justification=None,
        )

    successes = []
    failures = []
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(create, name) for name in ("winner-a", "winner-b")]
        for future in futures:
            try:
                successes.append(future.result())
            except ProcessingError as exc:
                failures.append(exc)

    assert len(successes) == 1
    assert len(failures) == 1
    assert "configuration" in str(failures[0]).lower() or "exists" in str(failures[0]).lower()
    persisted = load_edition_state(base, "racing-edition")
    assert persisted.configuration == successes[0].configuration

    same = create_edition_state(
        base,
        edition_name="racing-edition",
        requested_output_language=persisted.configuration.requested_output_language,
        curriculum_mode=persisted.configuration.curriculum_mode,
        curriculum_source_edition=persisted.configuration.curriculum_source_edition,
        source_curriculum_plan_digest=persisted.configuration.source_curriculum_plan_digest,
        requested_curriculum_path_id=persisted.configuration.requested_curriculum_path_id,
        skill_name=persisted.configuration.skill_name,
        host=persisted.configuration.host,
        output=persisted.configuration.output,
        output_is_default=persisted.configuration.output_is_default,
        project=persisted.configuration.project,
        project_root=persisted.configuration.project_root,
        skill_root=persisted.configuration.skill_root,
        run_official_validation=persisted.configuration.run_official_validation,
        baseline=persisted.configuration.identity_baseline,
        identity_drift_justification=persisted.configuration.identity_drift_justification,
    )
    assert same.configuration == persisted.configuration
    assert current_analysis_lineage(base) == persisted.configuration.analysis_lineage
    assert persisted.configuration.curriculum_source_edition is None
    assert persisted.configuration.edition_name != LEGACY_EDITION_NAME

    matching_barrier = Barrier(2)

    def create_matching():
        matching_barrier.wait()
        return create_edition_state(
            base,
            edition_name="matching-edition",
            requested_output_language="English",
            curriculum_mode="plan",
            curriculum_source_edition=None,
            source_curriculum_plan_digest=None,
            requested_curriculum_path_id=None,
            skill_name="matching-edition",
            host="codex",
            output=tmp_path / "matching-output",
            output_is_default=False,
            project=False,
            project_root=tmp_path,
            skill_root=tmp_path / "skills",
            run_official_validation=False,
            baseline=identity_baseline(base),
            identity_drift_justification=None,
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        matching_states = list(pool.map(lambda _index: create_matching(), range(2)))
    assert matching_states[0].configuration == matching_states[1].configuration
    assert (
        load_edition_state(base, "matching-edition").configuration
        == matching_states[0].configuration
    )
