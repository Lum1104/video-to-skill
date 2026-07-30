"""Bounded evidence retrieval for the generator agent."""

from __future__ import annotations

from video_to_skill.errors import ProcessingError
from video_to_skill.models import QueryResult, SemanticSegment, SourceDescriptor
from video_to_skill.utils import format_timestamp, timestamp_url
from video_to_skill.workspace import Workspace


def _select_sources(
    sources: list[SourceDescriptor], selector: str | None
) -> list[SourceDescriptor]:
    if selector is None:
        return sources
    if selector.isdigit():
        index = int(selector)
        if 1 <= index <= len(sources):
            return [sources[index - 1]]
    matches = [
        source
        for source in sources
        if selector.casefold() in source.id.casefold()
        or selector.casefold() in source.title.casefold()
    ]
    if not matches:
        raise ProcessingError(f"No source matches selector: {selector}")
    if len(matches) > 1:
        raise ProcessingError(f"Source selector is ambiguous ({len(matches)} matches): {selector}")
    return matches


def select_source(workspace: Workspace, selector: str) -> SourceDescriptor:
    """Resolve exactly one human-facing source index, ID, or title fragment."""

    matches = _select_sources(workspace.list_sources(), selector)
    if len(matches) != 1:
        raise ProcessingError(
            "A source selector is required when the workspace contains multiple sources"
        )
    return matches[0]


def _overlaps(segment: SemanticSegment, start: float | None, end: float | None) -> bool:
    return (start is None or segment.end >= start) and (end is None or segment.start <= end)


def query_workspace(
    workspace: Workspace,
    *,
    source_selector: str | None = None,
    section: int | None = None,
    start: float | None = None,
    end: float | None = None,
    search: str | None = None,
    max_transcripts: int = 250,
) -> list[QueryResult]:
    selected = _select_sources(workspace.list_sources(), source_selector)
    results: list[QueryResult] = []
    remaining = max_transcripts
    for source in selected:
        sections = workspace.semantic_segments(source.id)
        if section is not None:
            sections = [item for item in sections if item.ordinal == section]
            if not sections:
                raise ProcessingError(f"Source '{source.title}' has no section {section}")
        sections = [item for item in sections if _overlaps(item, start, end)]
        effective_start = start
        effective_end = end
        if sections:
            effective_start = min(item.start for item in sections)
            effective_end = max(item.end for item in sections)
        transcripts = workspace.transcripts(
            source.id,
            start=effective_start,
            end=effective_end,
            search=search,
            limit=max(0, remaining),
        )
        remaining -= len(transcripts)
        if search and transcripts:
            hit_ids = {item.id for item in transcripts}
            sections = [
                item
                for item in sections
                if any(identifier in hit_ids for identifier in item.transcript_ids)
            ]
        visuals = workspace.visuals(source.id, start=effective_start, end=effective_end)
        results.append(
            QueryResult(
                source=source,
                segments=sections,
                transcripts=transcripts,
                visuals=visuals,
            )
        )
        if remaining <= 0:
            break
    return results


def inventory_markdown(workspace: Workspace) -> str:
    lines = ["# Evidence Inventory", ""]
    for source_index, source in enumerate(workspace.list_sources(), start=1):
        duration = f" · {format_timestamp(source.duration)}" if source.duration is not None else ""
        course = f" · course item {source.playlist_index}" if source.playlist_index else ""
        lines += [
            f"## {source_index}. {source.title}",
            "",
            f"`{source.id}` · {source.platform.value}{duration}{course}",
            "",
        ]
        for segment in workspace.semantic_segments(source.id):
            lines.append(
                f"- Section {segment.ordinal}: **{segment.title}** "
                f"({format_timestamp(segment.start)}-{format_timestamp(segment.end)})"
            )
        lines.append("")
    return "\n".join(lines)


def render_query_markdown(results: list[QueryResult]) -> str:
    lines: list[str] = []
    for result in results:
        source = result.source
        lines += [
            f"# {source.title}",
            "",
            f"- Source ID: `{source.id}`",
            f"- Platform: {source.platform.value}",
        ]
        if source.creator:
            lines.append(f"- Creator: {source.creator}")
        if source.canonical_url:
            lines.append(f"- URL: {source.canonical_url}")
        lines.append("")
        for section in result.segments:
            link = timestamp_url(source.canonical_url, section.start)
            heading = (
                f"## Section {section.ordinal}: {section.title} "
                f"({format_timestamp(section.start)}-{format_timestamp(section.end)})"
            )
            lines += [heading, ""]
            if link:
                lines += [f"Start: {link}", ""]
            lines += [
                f"Boundary: {', '.join(section.boundary_reasons)}",
                "",
                "### Timed transcript evidence",
                "",
            ]
            matching_transcripts = [
                item for item in result.transcripts if item.id in set(section.transcript_ids)
            ]
            for transcript in matching_transcripts:
                evidence_link = timestamp_url(source.canonical_url, transcript.start)
                marker = (
                    f"[{format_timestamp(transcript.start)}]({evidence_link})"
                    if evidence_link
                    else format_timestamp(transcript.start)
                )
                speaker = f" **{transcript.speaker}:**" if transcript.speaker else ""
                lines.append(f"- {marker}{speaker} {transcript.text} `evidence:{transcript.id}`")
            lines += ["", "### Visual evidence", ""]
            matching_visuals = [
                item for item in result.visuals if item.id in set(section.visual_event_ids)
            ]
            for visual in matching_visuals:
                evidence_link = timestamp_url(source.canonical_url, visual.timestamp)
                marker = (
                    f"[{format_timestamp(visual.timestamp)}]({evidence_link})"
                    if evidence_link
                    else format_timestamp(visual.timestamp)
                )
                lines.append(
                    f"- {marker} `{visual.kind.value}` `{visual.path}` `evidence:{visual.id}`"
                )
                if visual.ocr_text:
                    compact = visual.ocr_text.replace("\n", " · ")
                    lines.append(f"  - OCR: {compact}")
                if visual.description:
                    lines.append(f"  - Visible state: {visual.description}")
            lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def parse_time(value: str | None) -> float | None:
    if value is None:
        return None
    if value.replace(".", "", 1).isdigit():
        return float(value)
    pieces = value.split(":")
    try:
        if len(pieces) == 2:
            return int(pieces[0]) * 60 + float(pieces[1])
        if len(pieces) == 3:
            return int(pieces[0]) * 3600 + int(pieces[1]) * 60 + float(pieces[2])
    except ValueError as exc:
        raise ProcessingError(f"Invalid timestamp: {value}") from exc
    raise ProcessingError(f"Invalid timestamp: {value}")
