"""End-to-end extraction pipeline with per-source stage recovery."""

from __future__ import annotations

import json
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from video_to_skill.authentication import (
    browser_cookie_session,
    cookie_settings_for_worker,
)
from video_to_skill.config import Settings
from video_to_skill.errors import DependencyError, ProcessingError, VideoToSkillError
from video_to_skill.models import (
    InspectionBatch,
    InspectionCompleteness,
    InspectionEntry,
    InspectionEntryStatus,
    JobManifest,
    JobState,
    MaterializedSource,
    ProcessingStage,
    SourceDescriptor,
    SourcePlatform,
    WarningRecord,
    WarningSeverity,
)
from video_to_skill.segment import segment_timeline
from video_to_skill.sources import (
    SourceRegistry,
    local_source_changed,
    source_cache_identity,
)
from video_to_skill.transcript import (
    captions_are_adequate,
    load_transcriber,
    transcribe,
)
from video_to_skill.utils import atomic_write_json, stable_hash
from video_to_skill.visual import analyze_visuals
from video_to_skill.workspace import Workspace

ProgressCallback = Callable[[str], None]
STAGE_REVISIONS = {
    ProcessingStage.ACQUIRE: 1,
    ProcessingStage.TRANSCRIBE: 2,
    ProcessingStage.VISUALS: 3,
    ProcessingStage.SEGMENT: 2,
}


def default_workspace_path(inputs: list[str], settings: Settings) -> Path:
    job_id = stable_hash(
        {"inputs": inputs, "configuration": settings.configuration_hash}, length=20
    )
    return settings.cache_root.expanduser() / job_id


def inspect_inputs(
    inputs: list[str],
    settings: Settings,
    *,
    registry: SourceRegistry | None = None,
) -> list[SourceDescriptor]:
    inspected = inspect_inputs_with_completeness(inputs, settings, registry=registry)
    if not inspected.sources:
        raise ProcessingError("No processable videos were found")
    return inspected.sources


def inspect_inputs_with_completeness(
    inputs: list[str],
    settings: Settings,
    *,
    registry: SourceRegistry | None = None,
) -> InspectionBatch:
    if not inputs:
        raise ProcessingError("At least one video URL or local media path is required")
    with browser_cookie_session(settings, inputs) as authenticated:
        return (registry or SourceRegistry()).inspect_with_completeness(
            inputs,
            authenticated,
        )


def _materialized_from_cache(
    workspace: Workspace, source: SourceDescriptor
) -> MaterializedSource | None:
    row = workspace.materialization_record(source.id)
    if row is None or not row["content_hash"] or not row["source_dir"]:
        return None
    source_dir = Path(row["source_dir"])
    media = Path(row["media_path"]) if row["media_path"] else None
    captions = [Path(item) for item in json.loads(row["caption_paths_json"])]
    if not source_dir.exists():
        return None
    if media is not None and not media.exists():
        return None
    captions = [path for path in captions if path.exists()]
    return MaterializedSource(
        source=source,
        directory=source_dir,
        media_path=media,
        caption_paths=captions,
        content_hash=row["content_hash"],
    )


def _record_warning(
    workspace: Workspace,
    *,
    code: str,
    message: str,
    source_id: str,
    stage: ProcessingStage,
    severity: WarningSeverity = WarningSeverity.WARNING,
) -> None:
    workspace.add_warning(
        WarningRecord(
            code=code,
            message=message,
            source_id=source_id,
            stage=stage,
            severity=severity,
        )
    )


def _process_source(
    source: SourceDescriptor,
    workspace: Workspace,
    settings: Settings,
    registry: SourceRegistry,
    progress: ProgressCallback,
) -> None:
    source_dir = workspace.source_directory(source.id)
    if not source.captions and source.platform != SourcePlatform.LOCAL:
        if settings.asr_provider == "none":
            raise DependencyError(f"'{source.title}' has no platform captions and ASR is disabled")
        load_transcriber(settings.asr_provider)
    need_media = settings.visual_profile != "transcript" or not source.captions
    acquire_key = stable_hash(
        {
            "revision": STAGE_REVISIONS[ProcessingStage.ACQUIRE],
            "source": source_cache_identity(source),
            "need_media": need_media,
            "yt_dlp": settings.yt_dlp,
        }
    )
    materialized = None
    if workspace.stage_complete(source.id, ProcessingStage.ACQUIRE, acquire_key):
        materialized = _materialized_from_cache(workspace, source)
    if materialized is None:
        progress(f"[{source.title}] acquiring source")
        workspace.start_stage(source.id, ProcessingStage.ACQUIRE, acquire_key)
        try:
            materialized = registry.materialize(source, source_dir, settings, need_media=need_media)
            workspace.update_materialization(
                source.id,
                content_hash=materialized.content_hash,
                source_dir=materialized.directory,
                media_path=materialized.media_path,
                caption_paths=materialized.caption_paths,
            )
            workspace.complete_stage(source.id, ProcessingStage.ACQUIRE, acquire_key)
        except Exception as exc:
            workspace.fail_stage(source.id, ProcessingStage.ACQUIRE, acquire_key, str(exc))
            raise

    if (
        materialized.media_path is None
        and settings.asr_provider != "none"
        and not captions_are_adequate(materialized, settings)
    ):
        progress(f"[{source.title}] captions are inadequate; acquiring media for ASR")
        acquire_key = stable_hash(
            {
                "revision": STAGE_REVISIONS[ProcessingStage.ACQUIRE],
                "source": source_cache_identity(source),
                "need_media": True,
                "yt_dlp": settings.yt_dlp,
            }
        )
        workspace.start_stage(source.id, ProcessingStage.ACQUIRE, acquire_key)
        try:
            materialized = registry.materialize(source, source_dir, settings, need_media=True)
            workspace.update_materialization(
                source.id,
                content_hash=materialized.content_hash,
                source_dir=materialized.directory,
                media_path=materialized.media_path,
                caption_paths=materialized.caption_paths,
            )
            workspace.complete_stage(source.id, ProcessingStage.ACQUIRE, acquire_key)
        except Exception as exc:
            workspace.fail_stage(source.id, ProcessingStage.ACQUIRE, acquire_key, str(exc))
            raise

    transcript_key = stable_hash(
        {
            "revision": STAGE_REVISIONS[ProcessingStage.TRANSCRIBE],
            "content": materialized.content_hash,
            "captions": [path.name for path in materialized.caption_paths],
            "language": settings.language,
            "asr": settings.asr_provider,
            "model": settings.asr_model,
        }
    )
    if not workspace.stage_complete(source.id, ProcessingStage.TRANSCRIBE, transcript_key):
        progress(f"[{source.title}] selecting captions / transcribing")
        workspace.start_stage(source.id, ProcessingStage.TRANSCRIBE, transcript_key)
        try:
            transcripts, warnings = transcribe(materialized, settings)
            workspace.replace_transcripts(source.id, transcripts)
            for warning in warnings:
                _record_warning(
                    workspace,
                    code=warning,
                    message=f"Transcript quality note for '{source.title}': {warning}",
                    source_id=source.id,
                    stage=ProcessingStage.TRANSCRIBE,
                    severity=WarningSeverity.INFO,
                )
            workspace.complete_stage(source.id, ProcessingStage.TRANSCRIBE, transcript_key)
        except Exception as exc:
            workspace.fail_stage(source.id, ProcessingStage.TRANSCRIBE, transcript_key, str(exc))
            raise
    transcripts = workspace.transcripts(source.id, limit=1_000_000)
    if not transcripts:
        raise ProcessingError(f"No transcript evidence for '{source.title}'")

    visual_key = stable_hash(
        {
            "revision": STAGE_REVISIONS[ProcessingStage.VISUALS],
            "content": materialized.content_hash,
            "profile": settings.visual_profile,
            "scene_threshold": settings.scene_threshold,
            "period": settings.periodic_frame_interval,
            "width": settings.frame_width,
            "ocr": settings.ocr_provider,
            "vision": settings.vision_provider,
            "vision_model": settings.vision_model,
        }
    )
    if not workspace.stage_complete(source.id, ProcessingStage.VISUALS, visual_key):
        progress(f"[{source.title}] analyzing visual state")
        workspace.start_stage(source.id, ProcessingStage.VISUALS, visual_key)
        try:
            visuals, warnings = analyze_visuals(materialized, settings)
            workspace.replace_visuals(source.id, visuals)
            for warning in warnings:
                _record_warning(
                    workspace,
                    code=warning.split(":", 1)[0],
                    message=f"Visual analysis note for '{source.title}': {warning}",
                    source_id=source.id,
                    stage=ProcessingStage.VISUALS,
                    severity=WarningSeverity.INFO,
                )
            workspace.complete_stage(source.id, ProcessingStage.VISUALS, visual_key)
        except Exception as exc:
            workspace.fail_stage(source.id, ProcessingStage.VISUALS, visual_key, str(exc))
            _record_warning(
                workspace,
                code="visual-analysis-failed",
                message=(
                    f"Visual processing failed for '{source.title}'; continuing with "
                    f"transcript evidence: {exc}"
                ),
                source_id=source.id,
                stage=ProcessingStage.VISUALS,
            )
    visuals = workspace.visuals(source.id)

    segment_key = stable_hash(
        {
            "revision": STAGE_REVISIONS[ProcessingStage.SEGMENT],
            "transcript_ids": [item.id for item in transcripts],
            "visual_ids": [item.id for item in visuals],
            "chapters": [item.model_dump(mode="json") for item in source.chapters],
            "minimum": settings.min_segment_seconds,
            "target": settings.target_segment_seconds,
            "maximum": settings.max_segment_seconds,
        }
    )
    if not workspace.stage_complete(source.id, ProcessingStage.SEGMENT, segment_key):
        progress(f"[{source.title}] building semantic timeline")
        workspace.start_stage(source.id, ProcessingStage.SEGMENT, segment_key)
        try:
            segments = segment_timeline(source, transcripts, visuals, settings)
            if not segments:
                raise ProcessingError("Timeline segmentation produced no sections")
            workspace.replace_semantic_segments(source.id, segments)
            workspace.complete_stage(source.id, ProcessingStage.SEGMENT, segment_key)
        except Exception as exc:
            workspace.fail_stage(source.id, ProcessingStage.SEGMENT, segment_key, str(exc))
            raise
    progress(f"[{source.title}] complete")


def _write_coverage_report(workspace: Workspace) -> Path:
    records = []
    for source in workspace.list_sources():
        transcripts = workspace.transcripts(source.id, limit=1_000_000)
        visuals = workspace.visuals(source.id)
        segments = workspace.semantic_segments(source.id)
        records.append(
            {
                "source_id": source.id,
                "title": source.title,
                "platform": source.platform.value,
                "duration": source.duration,
                "transcript_segments": len(transcripts),
                "transcript_characters": sum(len(item.text) for item in transcripts),
                "visual_events": len(visuals),
                "ocr_events": sum(bool(item.ocr_text) for item in visuals),
                "semantic_segments": len(segments),
                "timestamp_linkable": bool(source.canonical_url),
            }
        )
    path = workspace.root / "coverage.json"
    inspections = workspace.inspection_reports()
    retired = workspace.list_retired_sources()
    completeness_proven = bool(inspections) and all(
        item.completeness_proven for item in inspections
    )
    expected_entries = (
        sum(item.expected_entries for item in inspections if item.expected_entries is not None)
        if inspections and all(item.expected_entries is not None for item in inspections)
        else None
    )
    disclaimers = [item.disclaimer for item in inspections if item.disclaimer is not None]
    if not inspections:
        disclaimers.append(
            "No persisted input inspection report is available; course completeness is unknown."
        )
    atomic_write_json(
        path,
        {
            "schema_version": 2,
            "job_id": workspace.load_manifest().job_id,
            "course_completeness": {
                "proven": completeness_proven,
                "expected_entries": expected_entries,
                "accessible_entries": sum(item.accessible_entries for item in inspections),
                "inaccessible_entries": sum(item.inaccessible_entries for item in inspections),
                "failed_entries": sum(item.failed_entries for item in inspections),
                "disclaimers": disclaimers,
                "inspections": [item.model_dump(mode="json") for item in inspections],
            },
            "sources": records,
            "retired_sources": [item.model_dump(mode="json") for item in retired],
            "warnings": [item.model_dump(mode="json") for item in workspace.list_warnings()],
        },
    )
    return path


def _persist_inspections(
    workspace: Workspace,
    reports: list[InspectionCompleteness],
) -> None:
    workspace.replace_inspection_reports(reports)
    for report in reports:
        if report.completeness_proven:
            continue
        workspace.add_warning(
            WarningRecord(
                code="source-inspection-incomplete",
                message=report.disclaimer or "Source completeness could not be proven.",
                stage=ProcessingStage.INSPECT,
                severity=WarningSeverity.WARNING,
            )
        )


def _missing_local_inspection(source: SourceDescriptor) -> InspectionCompleteness:
    return InspectionCompleteness(
        locator=source.locator,
        platform=source.platform,
        expected_entries=1,
        accessible_entries=0,
        inaccessible_entries=1,
        failed_entries=0,
        completeness_proven=False,
        disclaimer=(
            f"Course completeness is not proven because local source '{source.title}' "
            "is no longer accessible."
        ),
        entries=[
            InspectionEntry(
                ordinal=1,
                status=InspectionEntryStatus.INACCESSIBLE,
                source_id=source.id,
                title=source.title,
                locator=source.locator,
                reason="the previously inspected local media file no longer exists",
            )
        ],
    )


def _extract_sources_authenticated(
    inputs: list[str],
    settings: Settings,
    *,
    workspace_path: Path | None = None,
    registry: SourceRegistry | None = None,
    progress: ProgressCallback | None = None,
    refresh: bool = False,
) -> tuple[Workspace, JobManifest]:
    registry = registry or SourceRegistry()
    progress = progress or (lambda _message: None)
    root = workspace_path or default_workspace_path(inputs, settings)
    workspace = Workspace.create(root=root, inputs=inputs, settings=settings)
    workspace.set_job_state(JobState.RUNNING)

    sources = workspace.list_sources()
    inspected_current_inputs = False
    if refresh or not sources:
        progress("Inspecting sources")
        inspected = inspect_inputs_with_completeness(inputs, settings, registry=registry)
        _persist_inspections(workspace, inspected.reports)
        sources = inspected.sources
        workspace.upsert_sources(sources, prune=True)
        inspected_current_inputs = True
        manifest = workspace.load_manifest()
        manifest.sources = sources
        workspace.save_manifest(manifest)
    elif sources:
        refreshed_sources: list[SourceDescriptor] = []
        local_reports: list[InspectionCompleteness] = []
        local_refresh_happened = False
        for source in sources:
            if local_source_changed(source):
                local_refresh_happened = True
                if Path(source.locator).is_file():
                    local_inspection = registry.adapter_for(
                        source.locator
                    ).inspect_with_completeness(
                        source.locator,
                        settings,
                    )
                    refreshed_sources.extend(local_inspection.sources)
                    local_reports.append(local_inspection.completeness)
                else:
                    local_reports.append(_missing_local_inspection(source))
            else:
                refreshed_sources.append(source)
        if local_refresh_happened:
            sources = refreshed_sources
            _persist_inspections(workspace, local_reports)
            workspace.upsert_sources(sources, prune=True)
            inspected_current_inputs = True
            manifest = workspace.load_manifest()
            manifest.sources = sources
            workspace.save_manifest(manifest)
    if not sources:
        if not inspected_current_inputs:
            raise ProcessingError("No processable videos were found")
        _write_coverage_report(workspace)
        workspace.set_job_state(JobState.FAILED)
        raise ProcessingError("No processable videos were found")

    failures: dict[str, str] = {}
    with ThreadPoolExecutor(max_workers=min(settings.max_workers, len(sources))) as executor:
        futures = {
            executor.submit(
                _process_source,
                source,
                workspace,
                cookie_settings_for_worker(settings, source.id),
                registry,
                progress,
            ): source
            for source in sources
        }
        for future in as_completed(futures):
            source = futures[future]
            try:
                future.result()
            except (VideoToSkillError, OSError, ValueError) as exc:
                failures[source.id] = str(exc)
                _record_warning(
                    workspace,
                    code="source-processing-failed",
                    message=f"'{source.title}' failed: {exc}",
                    source_id=source.id,
                    stage=ProcessingStage.COMPLETE,
                    severity=WarningSeverity.ERROR,
                )
            except Exception as exc:  # defensive boundary around optional backends
                failures[source.id] = f"{type(exc).__name__}: {exc}"
                _record_warning(
                    workspace,
                    code="unexpected-source-failure",
                    message=f"'{source.title}' failed unexpectedly: {type(exc).__name__}: {exc}",
                    source_id=source.id,
                    stage=ProcessingStage.COMPLETE,
                    severity=WarningSeverity.ERROR,
                )

    successful = [
        source
        for source in sources
        if source.id not in failures and workspace.semantic_segments(source.id)
    ]
    _write_coverage_report(workspace)
    if not successful:
        workspace.set_job_state(JobState.FAILED, failed_sources=list(failures))
        details = "; ".join(f"{key}: {value}" for key, value in failures.items())
        raise ProcessingError(f"All sources failed. {details}")
    inspection_incomplete = any(
        not report.completeness_proven for report in workspace.inspection_reports()
    )
    state = JobState.PARTIAL if failures or inspection_incomplete else JobState.COMPLETE
    manifest = workspace.set_job_state(state, failed_sources=list(failures))
    return workspace, manifest


def extract_sources(
    inputs: list[str],
    settings: Settings,
    *,
    workspace_path: Path | None = None,
    registry: SourceRegistry | None = None,
    progress: ProgressCallback | None = None,
    refresh: bool = False,
) -> tuple[Workspace, JobManifest]:
    """Extract all sources while reusing one browser-cookie snapshot."""

    root = workspace_path or default_workspace_path(inputs, settings)
    with browser_cookie_session(settings, inputs) as authenticated:
        return _extract_sources_authenticated(
            inputs,
            authenticated,
            workspace_path=root,
            registry=registry,
            progress=progress,
            refresh=refresh,
        )
