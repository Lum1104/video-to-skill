"""Command-line interface for deterministic video evidence processing."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Annotated

import typer

from video_to_skill import __version__
from video_to_skill.agentic import (
    MAX_ANNOTATION_PAYLOAD_BYTES,
    analyze_evidence_gaps,
    assemble_agent_context,
    ingest_annotations,
)
from video_to_skill.analysis_depth import (
    resolve_analysis_depth,
    verify_analysis_depth_contract,
)
from video_to_skill.config import Settings, load_settings
from video_to_skill.coordinator import advance_run, submit_workspace_result
from video_to_skill.doctor import diagnostics_ok, run_diagnostics
from video_to_skill.editions import load_edition_state
from video_to_skill.errors import ProcessingError, VideoToSkillError
from video_to_skill.evaluation import (
    evaluate_workspace,
    load_labels,
    render_evaluation_report,
)
from video_to_skill.evidence_bundles import (
    EvidenceBundleMode,
    export_evidence_bundle,
    verify_evidence_bundle,
)
from video_to_skill.installation import (
    SkillHost,
    host_skill_root,
    install_generated_skill,
    install_generator_skill,
)
from video_to_skill.investigation import (
    MAX_CONTACT_SHEET_EVENTS,
    extract_workspace_window_frames,
    generate_contact_sheet,
)
from video_to_skill.maintenance import clean_cached_media
from video_to_skill.models import AgentContext, EvidenceGap, VisualEvent
from video_to_skill.pipeline import (
    default_workspace_path,
    extract_sources,
    inspect_inputs_with_completeness,
)
from video_to_skill.query import (
    inventory_markdown,
    parse_time,
    query_workspace,
    render_query_markdown,
    select_source,
)
from video_to_skill.sources import describe_source
from video_to_skill.transcript import load_transcriber
from video_to_skill.utils import format_timestamp
from video_to_skill.validation import render_validation_report, validate_skill
from video_to_skill.workspace import Workspace

app = typer.Typer(
    name="video-to-skill",
    help="Compile videos and courses into grounded Agent Skill evidence.",
    no_args_is_help=True,
    pretty_exceptions_enable=False,
)


def _version(value: bool) -> None:
    if value:
        typer.echo(__version__)
        raise typer.Exit()


@app.callback()
def main(
    version: Annotated[
        bool,
        typer.Option("--version", callback=_version, is_eager=True, help="Show version."),
    ] = False,
) -> None:
    del version


def _settings(
    config: Path | None,
    *,
    language: str | None = None,
    output_language: str | None = None,
    analysis_depth: str | None = None,
    visual_profile: str | None = None,
    max_workers: int | None = None,
) -> Settings:
    return load_settings(
        config,
        language=language,
        output_language=output_language,
        analysis_depth=analysis_depth,
        visual_profile=visual_profile,
        max_workers=max_workers,
    )


def _output_format(value: str) -> str:
    normalized = value.casefold()
    if normalized not in {"markdown", "json"}:
        raise ProcessingError("--format must be 'markdown' or 'json'")
    return normalized


def _sample_visuals(events: list[VisualEvent], limit: int) -> list[VisualEvent]:
    if limit < 1 or limit > MAX_CONTACT_SHEET_EVENTS:
        raise ProcessingError(f"--max-events must be between 1 and {MAX_CONTACT_SHEET_EVENTS}")
    if len(events) <= limit:
        return events
    if limit == 1:
        return [events[0]]
    last = len(events) - 1
    indices = [round(position * last / (limit - 1)) for position in range(limit)]
    return [events[index] for index in indices]


def _render_agent_context(context: AgentContext) -> str:
    source = context.source
    lines = [
        f"# Agent Context: {source.title}",
        "",
        f"- Source ID: `{source.id}`",
        (
            f"- Window: {format_timestamp(context.window.start)}-"
            f"{format_timestamp(context.window.end)}"
        ),
        f"- Truncated: {'yes' if context.truncated else 'no'}",
        "",
        "## Transcript evidence",
        "",
    ]
    for transcript in context.transcripts:
        lines.append(
            f"- {format_timestamp(transcript.start)}-{format_timestamp(transcript.end)} "
            f"`{transcript.id}`: {transcript.text}"
        )
    lines.extend(["", "## Visual evidence", ""])
    for visual in context.visuals:
        lines.append(
            f"- {format_timestamp(visual.timestamp)} `{visual.kind.value}` "
            f"`{visual.id}`: `{visual.path}`"
        )
        if visual.ocr_text:
            lines.append(f"  - OCR: {visual.ocr_text.replace(chr(10), ' · ')}")
        if visual.description:
            lines.append(f"  - Visible state: {visual.description}")
    lines.extend(["", "## Agent observations", ""])
    for observation in context.observations:
        lines.append(
            f"- {format_timestamp(observation.start)}-"
            f"{format_timestamp(observation.end)} `{observation.type.value}` "
            f"`{observation.status.value}` confidence={observation.confidence:.2f} "
            f"`{observation.id}`: {observation.claim}"
        )
        references = [*observation.frame_ids, *observation.transcript_ids]
        if references:
            lines.append(f"  - Evidence: {', '.join(f'`{item}`' for item in references)}")
        if observation.uncertainty:
            lines.append(f"  - Uncertainty: {observation.uncertainty}")
    return "\n".join(lines).rstrip() + "\n"


def _render_gaps(gaps: list[EvidenceGap]) -> str:
    if not gaps:
        return "# Evidence Gaps\n\nNo evidence gaps detected.\n"
    lines = ["# Evidence Gaps", ""]
    for gap in gaps:
        state = "resolved" if gap.resolved else "open"
        lines.extend(
            [
                (
                    f"## {gap.severity.value.upper()} · {gap.gap_type.value} · "
                    f"{format_timestamp(gap.start)}-{format_timestamp(gap.end)}"
                ),
                "",
                f"- Gap ID: `{gap.id}`",
                f"- Source ID: `{gap.source_id}`",
                f"- State: {state}",
                f"- Finding: {gap.message}",
                f"- Next action: {gap.suggested_next_action}",
            ]
        )
        if gap.resolution:
            lines.append(f"- Resolution: {gap.resolution}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


@app.command()
def doctor(
    config: Annotated[
        Path | None, typer.Option("--config", help="Explicit TOML configuration.")
    ] = None,
    as_json: Annotated[bool, typer.Option("--json", help="Emit JSON.")] = False,
) -> None:
    """Check runtime programs, optional providers, credentials, and storage."""

    try:
        diagnostics = run_diagnostics(_settings(config))
    except VideoToSkillError as exc:
        typer.echo(f"ERROR: {exc}", err=True)
        raise typer.Exit(2) from exc
    if as_json:
        typer.echo(json.dumps([item.__dict__ for item in diagnostics], indent=2))
    else:
        for item in diagnostics:
            typer.echo(f"{item.status.upper():8} {item.capability}: {item.detail}")
    if not diagnostics_ok(diagnostics):
        raise typer.Exit(1)


@app.command("inspect")
def inspect_command(
    sources: Annotated[list[str], typer.Argument(help="URLs or local media paths.")],
    config: Annotated[Path | None, typer.Option("--config")] = None,
    language: Annotated[
        str | None,
        typer.Option(
            "--language",
            help="Preferred source caption/ASR language; does not set artifact language.",
        ),
    ] = None,
    analysis_depth: Annotated[
        str | None,
        typer.Option(
            "--analysis-depth",
            help="auto (recommended), standard, deep, or explicit archival.",
        ),
    ] = None,
    as_json: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Resolve inputs and report course structure without downloading media."""

    try:
        settings = _settings(config, language=language, analysis_depth=analysis_depth)
        inspection = inspect_inputs_with_completeness(sources, settings)
    except VideoToSkillError as exc:
        typer.echo(f"ERROR: {exc}", err=True)
        raise typer.Exit(2) from exc
    inspected = inspection.sources
    depth_contract = resolve_analysis_depth(inspected, inspection.reports, settings)
    if as_json:
        typer.echo(
            json.dumps(
                {
                    "sources": [item.model_dump(mode="json") for item in inspected],
                    "completeness": [item.model_dump(mode="json") for item in inspection.reports],
                    "analysis_depth": depth_contract.model_dump(mode="json"),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        if not inspected:
            raise typer.Exit(1)
        return
    total_duration = sum(item.duration or 0 for item in inspected)
    estimated_bytes = sum(
        int(item.metadata.get("estimated_media_bytes") or 0) for item in inspected
    )
    estimated_transcript_tokens = round(total_duration / 60 * 200)
    typer.echo(
        f"Sources: {len(inspected)} · duration: {total_duration / 60:.1f} min "
        f"· estimated media: {estimated_bytes / 1024**2:.1f} MiB\n"
        f"Estimated semantic input: ~{estimated_transcript_tokens:,} transcript tokens "
        "(provider pricing not included)"
    )
    typer.echo(
        f"Analysis depth: requested={depth_contract.requested.value} "
        f"recommended={depth_contract.recommended.value} "
        f"effective={depth_contract.effective.value} "
        f"profile={depth_contract.budget.profile_version}"
    )
    for reason in depth_contract.recommendation_reasons:
        typer.echo(f"  {reason}")
    for report in inspection.reports:
        expected = (
            str(report.expected_entries) if report.expected_entries is not None else "unknown"
        )
        proof = "proven" if report.completeness_proven else "not proven"
        typer.echo(
            f"- completeness={proof} expected={expected} "
            f"accessible={report.accessible_entries} "
            f"inaccessible={report.inaccessible_entries} failed={report.failed_entries} "
            f"input={report.locator}"
        )
        if report.disclaimer:
            typer.echo(f"  {report.disclaimer}")
    for item in inspected:
        typer.echo(f"- {describe_source(item)}")
        if item.captions:
            route = "platform captions"
        else:
            try:
                backend = load_transcriber(settings.asr_provider)
                route = f"media download + {backend.name} ASR"
            except VideoToSkillError:
                route = "media download + ASR required (backend unavailable)"
        periodic = depth_contract.budget.periodic_frame_interval_seconds
        visual_candidates = (
            depth_contract.budget.source_visual_event_limits.get(item.id, 0)
            if periodic is not None
            else 0
        )
        typer.echo(
            f"  captions={len(item.captions)} chapters={len(item.chapters)} id={item.id}\n"
            f"  route={route}; retained visual-event budget={visual_candidates} "
            + (
                f"at {periodic}s periodic sampling plus scene changes"
                if periodic is not None
                else "(visual processing disabled by transcript profile)"
            )
        )
    if not inspected:
        raise typer.Exit(1)


@app.command()
def extract(
    sources: Annotated[list[str], typer.Argument(help="URLs or local media paths.")],
    workspace: Annotated[
        Path | None, typer.Option("--workspace", help="Evidence workspace path.")
    ] = None,
    config: Annotated[Path | None, typer.Option("--config")] = None,
    language: Annotated[
        str | None,
        typer.Option(
            "--language",
            help="Preferred source caption/ASR language; does not set artifact language.",
        ),
    ] = None,
    output_language: Annotated[
        str | None,
        typer.Option(
            "--output-language",
            help="Canonical generated-artifact language, or 'source' (default).",
        ),
    ] = None,
    analysis_depth: Annotated[
        str | None,
        typer.Option(
            "--analysis-depth",
            help="auto (recommended), standard, deep, or explicit archival.",
        ),
    ] = None,
    visual_profile: Annotated[
        str | None,
        typer.Option("--visual-profile", help="adaptive, always, or transcript"),
    ] = None,
    max_workers: Annotated[int | None, typer.Option("--max-workers")] = None,
    refresh: Annotated[bool, typer.Option("--refresh", help="Re-inspect source metadata.")] = False,
    as_json: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Create or resume a normalized, queryable evidence workspace."""

    try:
        settings = _settings(
            config,
            language=language,
            output_language=output_language,
            analysis_depth=analysis_depth,
            visual_profile=visual_profile,
            max_workers=max_workers,
        )
        root = workspace or default_workspace_path(sources, settings)
        evidence, manifest = extract_sources(
            sources,
            settings,
            workspace_path=root,
            progress=lambda message: typer.echo(message, err=True),
            refresh=refresh,
        )
    except VideoToSkillError as exc:
        typer.echo(f"ERROR: {exc}", err=True)
        raise typer.Exit(2) from exc
    if as_json:
        typer.echo(manifest.model_dump_json(indent=2))
    else:
        typer.echo(f"Workspace: {evidence.root}")
        typer.echo(f"State: {manifest.state.value}")
        typer.echo(f"Sources: {len(manifest.sources)}")
        typer.echo(f"Warnings: {len(manifest.warnings)}")


@app.command()
def query(
    workspace: Annotated[Path, typer.Argument(help="Evidence workspace.")],
    source: Annotated[
        str | None, typer.Option("--source", help="Source index, ID, or title fragment.")
    ] = None,
    section: Annotated[int | None, typer.Option("--section")] = None,
    start: Annotated[str | None, typer.Option("--start", help="Seconds or HH:MM:SS.")] = None,
    end: Annotated[str | None, typer.Option("--end", help="Seconds or HH:MM:SS.")] = None,
    search: Annotated[str | None, typer.Option("--search", help="FTS5 query.")] = None,
    inventory: Annotated[
        bool, typer.Option("--inventory", help="List sources and semantic sections.")
    ] = False,
    as_json: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Retrieve a bounded evidence slice for grounded skill generation."""

    try:
        evidence = Workspace.open(workspace)
        if inventory:
            typer.echo(inventory_markdown(evidence))
            return
        results = query_workspace(
            evidence,
            source_selector=source,
            section=section,
            start=parse_time(start),
            end=parse_time(end),
            search=search,
        )
    except VideoToSkillError as exc:
        typer.echo(f"ERROR: {exc}", err=True)
        raise typer.Exit(2) from exc
    if as_json:
        typer.echo(
            json.dumps(
                [item.model_dump(mode="json") for item in results],
                ensure_ascii=False,
                indent=2,
            )
        )
    else:
        typer.echo(render_query_markdown(results))


@app.command("contact-sheet")
def contact_sheet(
    workspace: Annotated[Path, typer.Argument(help="Evidence workspace.")],
    source: Annotated[str, typer.Option("--source", help="Source index, ID, or title fragment.")],
    section: Annotated[int | None, typer.Option("--section", min=1)] = None,
    output: Annotated[
        Path | None, typer.Option("--output", help="Output JPEG; defaults inside workspace.")
    ] = None,
    force: Annotated[
        bool, typer.Option("--force", help="Replace an existing explicit output path.")
    ] = False,
    max_events: Annotated[
        int,
        typer.Option(
            "--max-events",
            min=1,
            max=MAX_CONTACT_SHEET_EVENTS,
            help="Maximum evenly sampled visual events.",
        ),
    ] = 36,
    columns: Annotated[int, typer.Option("--columns", min=1, max=10)] = 4,
    tile_width: Annotated[int, typer.Option("--tile-width", min=160, max=800)] = 320,
    as_json: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Render a bounded chronological visual overview for native agent inspection."""

    try:
        evidence = Workspace.open(workspace)
        selected = select_source(evidence, source)
        title = selected.title
        start: float | None = None
        end: float | None = None
        if section is not None:
            segment = next(
                (
                    item
                    for item in evidence.semantic_segments(selected.id)
                    if item.ordinal == section
                ),
                None,
            )
            if segment is None:
                raise ProcessingError(
                    f"Source '{selected.title}' has no semantic section {section}"
                )
            start, end = segment.start, segment.end
            title = f"{selected.title} · section {section}: {segment.title}"
        available = evidence.visuals(selected.id, start=start, end=end)
        sampled = _sample_visuals(available, max_events)
        if not sampled:
            raise ProcessingError(
                "No visual events are available for that scope; visual extraction may "
                "have been disabled or its cache may have been cleaned"
            )
        explicit_output = output is not None
        if output is None:
            suffix = f"section-{section:04d}" if section is not None else "overview"
            output = evidence.source_directory(selected.id) / "contact-sheets" / f"{suffix}.jpg"
        elif output.exists() and not force:
            raise ProcessingError(f"Output already exists: {output}. Use --force to replace it.")
        rendered = generate_contact_sheet(
            sampled,
            output,
            title=title,
            columns=columns,
            tile_width=tile_width,
        )
    except (VideoToSkillError, ValueError) as exc:
        typer.echo(f"ERROR: {exc}", err=True)
        raise typer.Exit(2) from exc
    payload = {
        "path": str(rendered),
        "source_id": selected.id,
        "section": section,
        "events_rendered": len(sampled),
        "events_available": len(available),
        "explicit_output": explicit_output,
    }
    if as_json:
        typer.echo(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        typer.echo(
            f"Contact sheet: {rendered}\n"
            f"Frames: {len(sampled)}/{len(available)} · source: {selected.id}"
        )


@app.command("frames")
def frames(
    workspace: Annotated[Path, typer.Argument(help="Evidence workspace.")],
    source: Annotated[str, typer.Option("--source", help="Source index, ID, or title fragment.")],
    from_time: Annotated[str, typer.Option("--from", help="Window start in seconds or HH:MM:SS.")],
    to_time: Annotated[
        str, typer.Option("--to", help="Exclusive window end in seconds or HH:MM:SS.")
    ],
    fps: Annotated[float, typer.Option("--fps", min=0.1, max=30)] = 1.0,
    deduplicate: Annotated[
        bool,
        typer.Option(
            "--deduplicate/--no-deduplicate",
            help="Drop near-identical adjacent samples.",
        ),
    ] = True,
    config: Annotated[Path | None, typer.Option("--config")] = None,
    as_json: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Extract and persist a short dense frame window for targeted investigation."""

    try:
        evidence = Workspace.open(workspace)
        selected = select_source(evidence, source)
        start = parse_time(from_time)
        end = parse_time(to_time)
        if start is None or end is None:
            raise ProcessingError("--from and --to are required")
        if selected.duration is not None and end > selected.duration + 1e-6:
            raise ProcessingError(
                f"Window end {end:g}s exceeds source duration {selected.duration:g}s"
            )
        events = extract_workspace_window_frames(
            evidence,
            selected.id,
            _settings(config),
            start=start,
            end=end,
            fps=fps,
            deduplicate=deduplicate,
        )
        evidence.upsert_visuals(events)
    except (VideoToSkillError, ValueError) as exc:
        typer.echo(f"ERROR: {exc}", err=True)
        raise typer.Exit(2) from exc
    if as_json:
        typer.echo(
            json.dumps(
                [item.model_dump(mode="json") for item in events],
                ensure_ascii=False,
                indent=2,
            )
        )
    else:
        typer.echo(
            f"Frames: {len(events)} · source: {selected.id} · "
            f"window: {format_timestamp(start)}-{format_timestamp(end)}"
        )
        for event in events:
            typer.echo(f"- {format_timestamp(event.timestamp)} `{event.id}` {event.path}")


@app.command("context")
def context(
    workspace: Annotated[Path, typer.Argument(help="Evidence workspace.")],
    source: Annotated[str, typer.Option("--source", help="Source index, ID, or title fragment.")],
    section: Annotated[int | None, typer.Option("--section", min=1)] = None,
    at: Annotated[
        str | None, typer.Option("--at", help="Center timestamp in seconds or HH:MM:SS.")
    ] = None,
    window: Annotated[
        float | None,
        typer.Option("--window", min=0.1, help="Seconds on each side of --at."),
    ] = None,
    output_format: Annotated[str, typer.Option("--format", help="markdown or json")] = "markdown",
    max_items: Annotated[int, typer.Option("--max-items", min=1, max=1000)] = 250,
) -> None:
    """Return a bounded synchronized transcript, visual, and observation packet."""

    try:
        evidence = Workspace.open(workspace)
        selected = select_source(evidence, source)
        timestamp = parse_time(at)
        if section is None and timestamp is None:
            raise ProcessingError("Provide exactly one of --section or --at")
        if section is not None and timestamp is not None:
            raise ProcessingError("Provide exactly one of --section or --at")
        if section is not None and window is not None:
            raise ProcessingError("--window can only be used with --at")
        contract = evidence.analysis_depth_contract()
        if contract is None:
            raise ProcessingError("Context requires a persisted analysis-depth contract")
        verify_analysis_depth_contract(contract)
        if max_items > contract.budget.context_max_items_per_kind:
            raise ProcessingError(
                f"--max-items {max_items} exceeds the {contract.effective.value} depth "
                f"context limit {contract.budget.context_max_items_per_kind}"
            )
        radius = (15.0 if window is None else window) if timestamp is not None else None
        packet = assemble_agent_context(
            evidence,
            source_id=selected.id,
            section=section,
            at=timestamp,
            window=radius,
            max_window_seconds=contract.budget.context_window_seconds,
            max_section_seconds=contract.budget.max_segment_seconds,
            max_items_per_kind=max_items,
        )
        selected_format = _output_format(output_format)
    except (VideoToSkillError, ValueError) as exc:
        typer.echo(f"ERROR: {exc}", err=True)
        raise typer.Exit(2) from exc
    if selected_format == "json":
        typer.echo(packet.model_dump_json(indent=2))
    else:
        typer.echo(_render_agent_context(packet), nl=False)


@app.command("annotate")
def annotate(
    workspace: Annotated[Path, typer.Argument(help="Evidence workspace.")],
    annotations: Annotated[Path, typer.Argument(help="Observation JSON file, or '-' for stdin.")],
    as_json: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Validate and persist agent observations grounded in workspace evidence."""

    try:
        evidence = Workspace.open(workspace)
        if str(annotations) == "-":
            binary_stdin = getattr(sys.stdin, "buffer", sys.stdin)
            payload: Path | str | bytes = binary_stdin.read(MAX_ANNOTATION_PAYLOAD_BYTES + 1)
        else:
            payload = annotations
        observations = ingest_annotations(evidence, payload)
    except (VideoToSkillError, ValueError) as exc:
        typer.echo(f"ERROR: {exc}", err=True)
        raise typer.Exit(2) from exc
    if as_json:
        typer.echo(
            json.dumps(
                [item.model_dump(mode="json") for item in observations],
                ensure_ascii=False,
                indent=2,
            )
        )
    else:
        typer.echo(f"Stored {len(observations)} grounded observation(s).")
        for observation in observations:
            typer.echo(f"- {observation.id}: {observation.claim}")


@app.command("gaps")
def gaps(
    workspace: Annotated[Path, typer.Argument(help="Evidence workspace.")],
    source: Annotated[
        str | None, typer.Option("--source", help="Source index, ID, or title fragment.")
    ] = None,
    output_format: Annotated[str, typer.Option("--format", help="markdown or json")] = "markdown",
    include_resolved: Annotated[
        bool, typer.Option("--include-resolved", help="Include manually resolved gaps.")
    ] = False,
) -> None:
    """Recompute actionable multimodal evidence gaps for the next agent turn."""

    try:
        evidence = Workspace.open(workspace)
        source_id = select_source(evidence, source).id if source is not None else None
        detected = analyze_evidence_gaps(evidence, source_id=source_id)
        visible = detected if include_resolved else [item for item in detected if not item.resolved]
        selected_format = _output_format(output_format)
    except (VideoToSkillError, ValueError) as exc:
        typer.echo(f"ERROR: {exc}", err=True)
        raise typer.Exit(2) from exc
    if selected_format == "json":
        typer.echo(
            json.dumps(
                [item.model_dump(mode="json") for item in visible],
                ensure_ascii=False,
                indent=2,
            )
        )
    else:
        typer.echo(_render_gaps(visible), nl=False)


@app.command("run")
def run_workflow(
    workspace: Annotated[
        Path,
        typer.Option("--workspace", help="Durable evidence and orchestration workspace."),
    ],
    sources: Annotated[
        list[str] | None,
        typer.Argument(help="URLs or local media paths; omit when resuming a workspace."),
    ] = None,
    host: Annotated[
        SkillHost | None,
        typer.Option(
            "--host",
            case_sensitive=False,
            help="Install for claude or codex; required only for a new run.",
        ),
    ] = None,
    output: Annotated[
        Path | None,
        typer.Option("--output", help="Portable generated Skill path."),
    ] = None,
    project: Annotated[
        bool | None,
        typer.Option(
            "--project/--user",
            help="Installation scope; omit on resume to reuse the workspace setting.",
        ),
    ] = None,
    config: Annotated[Path | None, typer.Option("--config")] = None,
    language: Annotated[
        str | None,
        typer.Option(
            "--language",
            help="Preferred source caption/ASR language; does not set artifact language.",
        ),
    ] = None,
    output_language: Annotated[
        str | None,
        typer.Option(
            "--output-language",
            help=(
                "Canonical generated-artifact language, or 'source'; on resume, an explicit "
                "value must match the persisted contract."
            ),
        ),
    ] = None,
    analysis_depth: Annotated[
        str | None,
        typer.Option(
            "--analysis-depth",
            help=(
                "auto (recommended), standard, deep, or explicit archival; resume must "
                "match the persisted request."
            ),
        ),
    ] = None,
    visual_profile: Annotated[
        str | None,
        typer.Option("--visual-profile", help="adaptive, always, or transcript"),
    ] = None,
    max_workers: Annotated[int | None, typer.Option("--max-workers")] = None,
    refresh: Annotated[bool, typer.Option("--refresh", help="Re-inspect source metadata.")] = False,
    skip_official: Annotated[
        bool,
        typer.Option("--skip-official", help="Do not call skills-ref during final validation."),
    ] = False,
) -> None:
    """Advance deterministic work until agent action is required or the Skill is complete."""

    try:
        settings = _settings(
            config,
            language=language,
            output_language=output_language,
            analysis_depth=analysis_depth,
            visual_profile=visual_profile,
            max_workers=max_workers,
        )
        envelope = advance_run(
            sources=list(sources or []),
            workspace_path=workspace,
            settings=settings,
            output_language_override=output_language,
            host=host,
            output=output,
            project=project,
            refresh=refresh,
            run_official_validation=not skip_official,
            progress=lambda message: typer.echo(message, err=True),
        )
    except (VideoToSkillError, ValueError) as exc:
        typer.echo(f"ERROR: {exc}", err=True)
        raise typer.Exit(2) from exc
    typer.echo(envelope.model_dump_json(indent=2))


@app.command("submit")
def submit_work_result(
    workspace: Annotated[Path, typer.Argument(help="Durable orchestration workspace.")],
    task_id: Annotated[str, typer.Argument(help="Task identity returned by run.")],
    result_file: Annotated[
        Path,
        typer.Argument(help="Role-specific JSON result inside the task output directory."),
    ],
) -> None:
    """Validate and atomically persist one worker or user-decision result."""

    try:
        evidence = Workspace.open(workspace)
        receipt = submit_workspace_result(evidence, task_id, result_file)
    except (VideoToSkillError, ValueError) as exc:
        typer.echo(f"ERROR: {exc}", err=True)
        raise typer.Exit(2) from exc
    typer.echo(receipt.model_dump_json(indent=2))


@app.command("edition")
def edition_workflow(
    workspace: Annotated[
        Path,
        typer.Argument(help="Completed evidence workspace whose Analyze state will be reused."),
    ],
    edition_name: Annotated[
        str,
        typer.Argument(help="Immutable lowercase-hyphenated edition name."),
    ],
    host: Annotated[
        SkillHost | None,
        typer.Option(
            "--host",
            case_sensitive=False,
            help="Install for claude or codex; required only for a new edition.",
        ),
    ] = None,
    output: Annotated[
        Path | None,
        typer.Option("--output", help="Portable generated Skill path."),
    ] = None,
    project: Annotated[
        bool | None,
        typer.Option(
            "--project/--user",
            help="Installation scope; omit on resume to reuse the edition setting.",
        ),
    ] = None,
    output_language: Annotated[
        str | None,
        typer.Option(
            "--output-language",
            help="Canonical artifact language or 'source'; immutable within this edition.",
        ),
    ] = None,
    curriculum: Annotated[
        str | None,
        typer.Option(
            "--curriculum",
            help="Existing planned curriculum path id to bind before Authoring.",
        ),
    ] = None,
    from_edition: Annotated[
        str | None,
        typer.Option(
            "--from-edition",
            help="Named edition whose curriculum checkpoint and identity baseline are reused.",
        ),
    ] = None,
    plan_curriculum: Annotated[
        bool,
        typer.Option(
            "--plan-curriculum",
            help="Run one bounded curriculum-planning task over the reused Analyze map.",
        ),
    ] = False,
    skill_name: Annotated[
        str | None,
        typer.Option("--skill-name", help="Required generated Skill name for this edition."),
    ] = None,
    identity_drift_reason: Annotated[
        str | None,
        typer.Option(
            "--identity-drift-reason",
            help="Explicit justification when logical artifact or claim ids must change.",
        ),
    ] = None,
    config: Annotated[Path | None, typer.Option("--config")] = None,
    analysis_depth: Annotated[
        str | None,
        typer.Option(
            "--analysis-depth",
            help="Must match the persisted evidence workspace depth request.",
        ),
    ] = None,
    max_workers: Annotated[int | None, typer.Option("--max-workers")] = None,
    skip_official: Annotated[
        bool,
        typer.Option("--skip-official", help="Do not call skills-ref during final validation."),
    ] = False,
) -> None:
    """Create or resume an isolated edition from immutable completed Analyze state."""

    try:
        settings = _settings(
            config,
            output_language=output_language,
            analysis_depth=analysis_depth,
            max_workers=max_workers,
        )
        envelope = advance_run(
            sources=[],
            workspace_path=workspace,
            settings=settings,
            output_language_override=output_language,
            host=host,
            output=output,
            project=project,
            refresh=False,
            run_official_validation=not skip_official,
            edition_name=edition_name,
            curriculum_path_id=curriculum,
            curriculum_source_edition=from_edition,
            plan_curriculum=plan_curriculum,
            skill_name=skill_name,
            identity_drift_justification=identity_drift_reason,
        )
    except (VideoToSkillError, ValueError) as exc:
        typer.echo(f"ERROR: {exc}", err=True)
        raise typer.Exit(2) from exc
    typer.echo(envelope.model_dump_json(indent=2))


@app.command()
def validate(
    skill_directory: Annotated[Path, typer.Argument(help="Generated skill folder.")],
    skip_official: Annotated[
        bool, typer.Option("--skip-official", help="Do not call skills-ref if installed.")
    ] = False,
    check_code: Annotated[
        bool,
        typer.Option(
            "--check-code",
            help="Parse-check supported fenced code blocks without executing them.",
        ),
    ] = False,
    as_json: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Validate Agent Skills format, grounding, links, and shareability."""

    try:
        report = validate_skill(
            skill_directory,
            run_official=not skip_official,
            check_code=check_code,
        )
    except VideoToSkillError as exc:
        typer.echo(f"ERROR: {exc}", err=True)
        raise typer.Exit(2) from exc
    typer.echo(report.model_dump_json(indent=2) if as_json else render_validation_report(report))
    if not report.valid:
        raise typer.Exit(1)


@app.command("tool-runs")
def tool_runs(
    workspace: Annotated[Path, typer.Argument(help="Evidence workspace.")],
    output: Annotated[
        Path | None,
        typer.Option(
            "--output",
            help="Workspace-contained JSONL path; defaults to logs/tool-runs.jsonl.",
        ),
    ] = None,
    as_json: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Export sanitized deterministic tool provenance inside the raw workspace."""

    try:
        evidence = Workspace.open(workspace)
        report = evidence.export_tool_runs(output)
    except VideoToSkillError as exc:
        typer.echo(f"ERROR: {exc}", err=True)
        raise typer.Exit(2) from exc
    if as_json:
        typer.echo(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        typer.echo(f"Tool runs: {report['records']} · {report['path']} · sha256={report['sha256']}")


@app.command("evidence-bundle")
def evidence_bundle(
    workspace: Annotated[Path, typer.Argument(help="Evidence workspace.")],
    output: Annotated[
        Path,
        typer.Option(
            "--output",
            help="Create-only .v2sbundle path outside the evidence workspace.",
        ),
    ],
    mode: Annotated[
        str,
        typer.Option("--mode", help="Bundle policy: compact or archival."),
    ] = EvidenceBundleMode.COMPACT.value,
    edition: Annotated[
        str | None,
        typer.Option("--edition", help="Named edition whose downstream evidence to include."),
    ] = None,
    authorize_transcript_redistribution: Annotated[
        bool,
        typer.Option(
            "--authorize-transcript-redistribution",
            help="Include transcript/caption text in a compact shareable bundle.",
        ),
    ] = False,
    confirm_private_archival: Annotated[
        bool,
        typer.Option(
            "--confirm-private-archival",
            help="Acknowledge that an archival bundle is private and sensitive.",
        ),
    ] = False,
    as_json: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Export deterministic compact or private archival evidence."""

    try:
        base = Workspace.open(workspace)
        evidence = base
        if edition is not None:
            state = load_edition_state(base, edition)
            evidence = base.for_edition(state.configuration.edition_id)
        report = export_evidence_bundle(
            evidence,
            output,
            mode=mode,
            authorize_transcript_redistribution=authorize_transcript_redistribution,
            confirm_private_archival=confirm_private_archival,
        )
    except VideoToSkillError as exc:
        typer.echo(f"ERROR: {exc}", err=True)
        raise typer.Exit(2) from exc
    if report.warning is not None:
        typer.echo(report.warning, err=True)
    if as_json:
        typer.echo(report.model_dump_json(indent=2))
    else:
        typer.echo(
            f"Evidence bundle: {report.mode.value} · {report.files} files · "
            f"{report.path} · sha256={report.sha256}"
        )


@app.command("verify-evidence-bundle")
def verify_bundle(
    bundle: Annotated[Path, typer.Argument(help="Evidence .v2sbundle file.")],
    as_json: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Verify an evidence bundle without extracting it."""

    try:
        report = verify_evidence_bundle(bundle)
    except VideoToSkillError as exc:
        typer.echo(f"ERROR: {exc}", err=True)
        raise typer.Exit(2) from exc
    if as_json:
        typer.echo(report.model_dump_json(indent=2))
    else:
        typer.echo(
            f"Verified evidence bundle: {report.bundle_id} · {report.files} files · "
            f"sha256={report.sha256}"
        )


@app.command()
def clean(
    workspace: Annotated[Path, typer.Argument(help="Evidence workspace.")],
    yes: Annotated[bool, typer.Option("--yes", help="Skip confirmation.")] = False,
) -> None:
    """Remove reproducible cached media while preserving evidence and manifests."""

    try:
        evidence = Workspace.open(workspace)
        if not yes and not typer.confirm(
            f"Remove cached media and frames under {evidence.sources_dir}?"
        ):
            raise typer.Abort()
        files, size = clean_cached_media(evidence)
    except VideoToSkillError as exc:
        typer.echo(f"ERROR: {exc}", err=True)
        raise typer.Exit(2) from exc
    typer.echo(f"Removed {files} files ({size / 1024**2:.1f} MiB).")


@app.command()
def evaluate(
    workspace: Annotated[Path, typer.Argument(help="Evidence workspace.")],
    labels: Annotated[Path, typer.Argument(help="Labeled evaluation JSON.")],
    tolerance: Annotated[
        float, typer.Option("--tolerance", help="Timestamp matching tolerance in seconds.")
    ] = 2.5,
    required_visual_recall: Annotated[
        float,
        typer.Option(
            "--required-visual-recall",
            min=0,
            max=1,
            help="Minimum critical visual-state recall.",
        ),
    ] = 0.90,
    as_json: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Score visual-state recall and semantic-boundary accuracy."""

    try:
        report = evaluate_workspace(
            Workspace.open(workspace),
            load_labels(labels),
            tolerance_seconds=tolerance,
            required_visual_recall=required_visual_recall,
        )
    except VideoToSkillError as exc:
        typer.echo(f"ERROR: {exc}", err=True)
        raise typer.Exit(2) from exc
    typer.echo(report.model_dump_json(indent=2) if as_json else render_evaluation_report(report))
    if not report.passed:
        raise typer.Exit(1)


@app.command("install-skill")
def install_skill(
    skill_root: Annotated[
        Path,
        typer.Argument(
            help=(
                "Agent Skills root; installs the complete video-to-skill generator "
                "bundle beneath it."
            )
        ),
    ],
    force: Annotated[
        bool,
        typer.Option(
            "--force",
            help="Update the managed files in an existing video-to-skill generator bundle.",
        ),
    ] = False,
) -> None:
    """Install the invocation-complete generator bundle into a compatible Agent host."""

    try:
        path, status = install_generator_skill(skill_root, overwrite=force)
    except VideoToSkillError as exc:
        typer.echo(f"ERROR: {exc}", err=True)
        raise typer.Exit(2) from exc
    typer.echo(f"{status.capitalize()}: {path}")


@app.command("install-generated")
def install_generated(
    skill_directory: Annotated[Path, typer.Argument(help="Validated generated skill folder.")],
    host: Annotated[
        SkillHost,
        typer.Option(
            "--host",
            case_sensitive=False,
            help="Install for claude or codex.",
        ),
    ],
    project: Annotated[
        bool,
        typer.Option(
            "--project",
            help="Install into the current project instead of the user Skill directory.",
        ),
    ] = False,
    skip_official: Annotated[
        bool,
        typer.Option("--skip-official", help="Do not call skills-ref if installed."),
    ] = False,
) -> None:
    """Validate and safely install a generated course Skill for the active host."""

    try:
        report = validate_skill(
            skill_directory,
            run_official=not skip_official,
            check_code=True,
        )
        if not report.valid:
            raise ProcessingError(render_validation_report(report))
        root = host_skill_root(host, project=project)
        path, status = install_generated_skill(skill_directory, root)
    except VideoToSkillError as exc:
        typer.echo(f"ERROR: {exc}", err=True)
        raise typer.Exit(2) from exc
    scope = "project" if project else "user"
    typer.echo(f"{status.capitalize()}: {path} ({host.value}, {scope})")


def run() -> None:
    try:
        app()
    except KeyboardInterrupt:
        typer.echo("Interrupted.", err=True)
        sys.exit(130)
