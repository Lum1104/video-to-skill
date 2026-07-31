from __future__ import annotations

import json
from pathlib import Path

import pytest
from test_author import _analyzed_workspace, _author_result, _plan_course_author_task
from test_generation import _blueprint
from test_review import _complete_behavior_trials, _review_result

from video_to_skill.author import submit_author_result
from video_to_skill.compiler import (
    build_workspace_skill,
    compile_workspace_blueprint,
)
from video_to_skill.config import Settings
from video_to_skill.errors import ProcessingError
from video_to_skill.installation import SkillHost
from video_to_skill.models import (
    JobState,
    ObservationProducer,
    SemanticSegment,
    SourceDescriptor,
    SourcePlatform,
)
from video_to_skill.orchestration import (
    AFFORDANCE_CATALOG,
    ArtifactDraftSpec,
    InstructionalAffordance,
)
from video_to_skill.review import plan_review_task, submit_review_result
from video_to_skill.utils import atomic_write_json, hash_file
from video_to_skill.work import WorkRole
from video_to_skill.workspace import Workspace


def _compiled_workspace(tmp_path: Path, *, review_passes: bool = True) -> Workspace:
    blueprint = _blueprint()
    workspace = Workspace.create(
        root=tmp_path / "workspace",
        inputs=["https://youtu.be/example"],
        settings=Settings(cache_root=tmp_path),
    )
    source = SourceDescriptor(
        id="course-01",
        platform=SourcePlatform.YOUTUBE,
        locator="https://youtu.be/example",
        canonical_url="https://youtu.be/example",
        title="Transition Demo",
        creator="Instructor",
        duration=180,
    )
    workspace.upsert_sources([source])
    workspace.replace_semantic_segments(
        source.id,
        [
            SemanticSegment(
                id="section-1",
                source_id=source.id,
                ordinal=1,
                title="Observable transitions",
                start=0,
                end=180,
            )
        ],
    )
    workspace.set_job_state(JobState.COMPLETE)
    run = workspace.create_analysis_run()
    author = workspace.ensure_work_item(
        run_id=run.id,
        role=WorkRole.AUTHOR,
        scope={"kind": "course-authoring", "revision": 1},
        persona_hint="Principal curriculum architect.",
        packet={},
        result_schema={"type": "object"},
        snapshot_digest=run.snapshot_digest,
    )
    lease = workspace.lease_work_item(author.id, owner="codex")
    result_path = lease.output_directory / "result.json"
    atomic_write_json(result_path, {"producer": "author-worker"})
    artifact_for_mode = {
        mode: next(artifact for artifact in blueprint.artifacts if mode in artifact.modes)
        for mode in ("learn", "practice", "apply", "reference")
    }
    affordances = [
        InstructionalAffordance(
            id=f"affordance-{mode}-{kind}",
            mode=mode,
            kind=kind,
            status="provided",
            artifact_ids=[artifact_for_mode[mode].id],
            semantic_unit_ids=["unit-transition"],
            rationale="The authored course explicitly provides this surface.",
        )
        for mode, kinds in AFFORDANCE_CATALOG.items()
        for kind in kinds
    ]
    affordances_by_artifact: dict[str, list[str]] = {}
    for affordance in affordances:
        for artifact_id in affordance.artifact_ids:
            affordances_by_artifact.setdefault(artifact_id, []).append(affordance.id)
    artifact_specs = []
    canonical_outputs = []
    for artifact in blueprint.artifacts:
        draft = lease.output_directory / "drafts" / f"{artifact.id}.md"
        draft.parent.mkdir(parents=True, exist_ok=True)
        draft.write_text(artifact.content, encoding="utf-8")
        artifact_specs.append(
            ArtifactDraftSpec(
                id=artifact.id,
                path=artifact.path,
                title=artifact.title,
                modes=artifact.modes,
                disclosure=artifact.disclosure,
                use_when=artifact.use_when,
                independent_loading_reason=artifact.independent_loading_reason,
                semantic_unit_ids=artifact.semantic_unit_ids,
                affordance_ids=affordances_by_artifact.get(artifact.id, []),
                topics=artifact.topics,
                draft_path=str(draft.relative_to(lease.output_directory)),
                draft_sha256=hash_file(draft),
            )
        )
        canonical_outputs.append(("artifact-draft", artifact.id, draft))
    records = {
        "semantic-map": [item.model_dump(mode="json") for item in blueprint.semantic_units],
        "semantic-relations": [
            item.model_dump(mode="json") for item in blueprint.semantic_relations
        ],
        "semantic-coverage": {"material_units_accounted_for": True},
        "course": {
            "name": blueprint.name,
            "title": blueprint.title,
            "description": blueprint.description,
            "scope": blueprint.scope,
            "artifact_language": blueprint.artifact_language,
            "prerequisites": blueprint.prerequisites,
            "core_principles": [item.model_dump(mode="json") for item in blueprint.core_principles],
            "limitations": blueprint.limitations,
        },
        "curriculum": blueprint.curriculum.model_dump(mode="json"),
        "interaction": blueprint.interaction.model_dump(mode="json"),
        "capability-profile": blueprint.capability_profile.model_dump(mode="json"),
        "artifact-plan": [item.model_dump(mode="json") for item in artifact_specs],
        "instructional-affordances": [item.model_dump(mode="json") for item in affordances],
        "claims": [item.model_dump(mode="json") for item in blueprint.claims],
        "assets": [],
    }
    for kind, value in records.items():
        path = lease.output_directory / f"{kind}.json"
        atomic_write_json(path, value)
        canonical_outputs.append((kind, "default", path))
    author, _author_records = workspace.accept_work_result(
        task_id=author.id,
        lease_token=lease.token,
        result_path=result_path,
        producer=ObservationProducer(
            name="author-worker",
            run_id="author-run",
        ).model_dump(mode="json"),
        canonical_outputs=canonical_outputs,
    )
    trials = _complete_behavior_trials(workspace, run, author)
    review = plan_review_task(
        workspace,
        run,
        author_task=author,
        behavior_trial_tasks=trials,
    )
    review_lease = workspace.lease_work_item(review.id, owner="codex")
    review_result = review_lease.output_directory / "result.json"
    atomic_write_json(
        review_result,
        _review_result(
            workspace,
            review,
            review_lease.token,
            verdict="pass" if review_passes else "fail",
        ),
    )
    submit_review_result(workspace, review.id, review_result)
    return workspace


def test_compiler_uses_digest_references_in_workspace_receipt(tmp_path: Path) -> None:
    workspace = _compiled_workspace(tmp_path)

    blueprint, receipt, build_directory = compile_workspace_blueprint(workspace)

    assert blueprint.name == "transition-course"
    receipt_text = (build_directory / "blueprint.json").read_text(encoding="utf-8")
    assert receipt.build_id in receipt_text
    assert "Observe both the action and its result" not in receipt_text
    assert all(
        reference.draft_path.is_relative_to(Path("analysis")) for reference in receipt.artifacts
    )


def test_workspace_build_renders_validates_and_installs(tmp_path: Path) -> None:
    workspace = _compiled_workspace(tmp_path)

    result = build_workspace_skill(
        workspace,
        host=SkillHost.CODEX,
        output=tmp_path / "generated" / "transition-course",
        skill_root=tmp_path / "skills",
        run_official_validation=False,
    )

    assert result.generated_path.is_dir()
    assert result.installed_path.is_dir()
    assert result.validation_report_path.is_file()
    assert json.loads(result.validation_report_path.read_text(encoding="utf-8"))["valid"]


def test_compiler_refuses_failed_review(tmp_path: Path) -> None:
    workspace = _compiled_workspace(tmp_path, review_passes=False)

    with pytest.raises(ProcessingError, match="Review has not passed"):
        compile_workspace_blueprint(workspace)


def test_compiler_refuses_forged_passing_heads_after_failed_review(tmp_path: Path) -> None:
    workspace = _compiled_workspace(tmp_path, review_passes=False)
    critic_record = workspace.canonical_record("critic-report")
    behavior_record = workspace.canonical_record("behavior-report")
    assert critic_record is not None
    assert behavior_record is not None

    critic = json.loads((workspace.root / critic_record.path).read_text(encoding="utf-8"))
    behavior = json.loads((workspace.root / behavior_record.path).read_text(encoding="utf-8"))
    critic["verdict"] = "pass"
    critic["findings"] = []
    behavior["passed"] = True
    for check in behavior["checks"]:
        if check["applicability"] == "required":
            check["passed"] = True

    forged_critic = workspace.analysis_dir / "forged-critic-report.json"
    forged_behavior = workspace.analysis_dir / "forged-behavior-report.json"
    atomic_write_json(forged_critic, critic)
    atomic_write_json(forged_behavior, behavior)
    workspace.publish_canonical_record(
        kind="critic-report",
        record_id="default",
        source_path=forged_critic,
        producer_task_id=critic_record.producer_task_id,
        snapshot_digest=critic_record.snapshot_digest,
    )
    workspace.publish_canonical_record(
        kind="behavior-report",
        record_id="default",
        source_path=forged_behavior,
        producer_task_id=behavior_record.producer_task_id,
        snapshot_digest=behavior_record.snapshot_digest,
    )

    with pytest.raises(
        ProcessingError,
        match="Canonical Review heads differ from the accepted ReviewResult",
    ):
        compile_workspace_blueprint(workspace)


def test_compiler_refuses_canonical_snapshot_drift_after_review(tmp_path: Path) -> None:
    workspace = _compiled_workspace(tmp_path)
    interaction_record = workspace.canonical_record("interaction")
    assert interaction_record is not None
    interaction_path = workspace.root / interaction_record.path
    republished = workspace.analysis_dir / "republished-interaction.json"
    republished.write_bytes(interaction_path.read_bytes())
    workspace.publish_canonical_record(
        kind="interaction",
        record_id="default",
        source_path=republished,
        producer_task_id=interaction_record.producer_task_id,
        snapshot_digest=interaction_record.snapshot_digest,
    )

    with pytest.raises(ProcessingError, match="stale catalog or Skill preview"):
        compile_workspace_blueprint(workspace)


def test_compiler_refuses_legacy_passed_boolean_behavior_report(tmp_path: Path) -> None:
    workspace = _compiled_workspace(tmp_path)
    current = workspace.canonical_record("behavior-report")
    assert current is not None
    legacy_path = workspace.analysis_dir / "legacy-behavior-report.json"
    atomic_write_json(legacy_path, {"passed": True, "checks": []})
    workspace.publish_canonical_record(
        kind="behavior-report",
        record_id="default",
        source_path=legacy_path,
        producer_task_id=current.producer_task_id,
        snapshot_digest=current.snapshot_digest,
    )

    with pytest.raises(ProcessingError, match="Legacy behavior reports"):
        compile_workspace_blueprint(workspace)


def test_build_refuses_preseeded_output_with_matching_build_id(tmp_path: Path) -> None:
    workspace = _compiled_workspace(tmp_path)
    _blueprint_value, receipt, _build_directory = compile_workspace_blueprint(workspace)
    output = tmp_path / "generated" / "transition-course"
    output.mkdir(parents=True)
    atomic_write_json(output / "build-manifest.json", {"build_id": receipt.build_id})

    with pytest.raises(ProcessingError, match="missing required generated Skill files"):
        build_workspace_skill(
            workspace,
            host=SkillHost.CODEX,
            output=output,
            skill_root=tmp_path / "skills",
            run_official_validation=False,
        )


def test_build_refuses_symlinked_output_root(tmp_path: Path) -> None:
    workspace = _compiled_workspace(tmp_path)
    real_output_root = tmp_path / "real-output"
    real_output_root.mkdir()
    linked_output_root = tmp_path / "linked-output"
    linked_output_root.symlink_to(real_output_root, target_is_directory=True)

    with pytest.raises(ProcessingError, match="symlinked path components"):
        build_workspace_skill(
            workspace,
            host=SkillHost.CODEX,
            output=linked_output_root / "transition-course",
            skill_root=tmp_path / "skills",
            run_official_validation=False,
        )


def test_compiler_binds_selected_visual_to_integrated_candidate(tmp_path: Path) -> None:
    workspace, analyze_task = _analyzed_workspace(tmp_path, with_visual=True)
    run = workspace.create_analysis_run(analyze_task.snapshot_digest)
    author_task = _plan_course_author_task(workspace, run, analyze_task)
    author_lease = workspace.lease_work_item(author_task.id, owner="codex")
    draft = author_lease.output_directory / "course.md"
    draft.write_text(
        "# Evidence-Updated Conviction\n\n![Decision status](../assets/status-panel.png)\n",
        encoding="utf-8",
    )
    author_result = _author_result(
        author_task,
        author_lease.token,
        draft,
        with_visual=True,
    )
    author_result_path = author_lease.output_directory / "result.json"
    atomic_write_json(author_result_path, author_result)
    accepted_author = submit_author_result(workspace, author_task.id, author_result_path)

    trials = _complete_behavior_trials(workspace, run, accepted_author)
    review_task = plan_review_task(
        workspace,
        run,
        author_task=accepted_author,
        behavior_trial_tasks=trials,
    )
    review_lease = workspace.lease_work_item(review_task.id, owner="codex")
    review_result = _review_result(workspace, review_task, review_lease.token)
    review_result_path = review_lease.output_directory / "result.json"
    atomic_write_json(review_result_path, review_result)
    submit_review_result(workspace, review_task.id, review_result_path)

    blueprint, receipt, _build_directory = compile_workspace_blueprint(workspace)

    assert blueprint.assets[0].candidate_id == "status-panel"
    assert receipt.visual_asset_candidates_digest is not None
    options_record = workspace.canonical_record("curriculum-options")
    selection_record = workspace.canonical_record("selected-curriculum")
    assert options_record is not None
    assert selection_record is not None
    assert receipt.curriculum_options_digest == options_record.digest
    assert receipt.selected_curriculum_digest == selection_record.digest
    options_path = workspace.root / options_record.path
    original_options = options_path.read_bytes()
    options_path.write_bytes(original_options + b"\n")
    with pytest.raises(ProcessingError, match="curriculum plan failed its digest check"):
        compile_workspace_blueprint(workspace)
    options_path.write_bytes(original_options)

    image_record = workspace.canonical_record("visual-asset-image", "default:status-panel")
    assert image_record is not None
    image_path = workspace.root / image_record.path
    original_image = image_path.read_bytes()
    image_path.write_bytes(b"tampered")
    with pytest.raises(ProcessingError, match="image failed its digest check"):
        compile_workspace_blueprint(workspace)
    image_path.write_bytes(original_image)

    selection = json.loads((workspace.root / selection_record.path).read_text(encoding="utf-8"))
    drift_path = (
        workspace.tasks_dir / options_record.producer_task_id / "output" / "selection-drift.json"
    )
    drift_path.write_text(json.dumps(selection, separators=(",", ":")), encoding="utf-8")
    workspace.publish_canonical_record(
        kind="selected-curriculum",
        record_id="default",
        source_path=drift_path,
        producer_task_id=options_record.producer_task_id,
        snapshot_digest=selection_record.snapshot_digest,
    )
    with pytest.raises(ProcessingError, match="not bound to the current curriculum checkpoint"):
        compile_workspace_blueprint(workspace)
