"""Source inspection and materialization for local, YouTube, and Bilibili media."""

from __future__ import annotations

import json
import mimetypes
from abc import ABC, abstractmethod
from importlib.metadata import entry_points
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from video_to_skill.config import Settings
from video_to_skill.errors import SourceError
from video_to_skill.models import (
    CaptionTrack,
    Chapter,
    InspectionBatch,
    InspectionCompleteness,
    InspectionEntry,
    InspectionEntryStatus,
    MaterializedSource,
    SourceDescriptor,
    SourceInspection,
    SourceKind,
    SourcePlatform,
)
from video_to_skill.utils import hash_file, require_program, run_command, stable_hash

MEDIA_EXTENSIONS = {
    ".mp4",
    ".mkv",
    ".webm",
    ".mov",
    ".m4v",
    ".avi",
    ".mp3",
    ".m4a",
    ".wav",
    ".flac",
    ".ogg",
    ".opus",
}
CAPTION_EXTENSIONS = {".vtt", ".srt", ".ass", ".ttml"}


def _sidecar_caption_paths(media_path: Path) -> list[Path]:
    prefix = f"{media_path.stem}."
    return sorted(
        candidate.resolve()
        for candidate in media_path.parent.iterdir()
        if candidate.is_file()
        and candidate.name.startswith(prefix)
        and candidate.suffix.lower() in CAPTION_EXTENSIONS
    )


def _sidecar_language(media_path: Path, caption_path: Path) -> str | None:
    remainder = caption_path.name[len(media_path.stem) + 1 :]
    pieces = remainder.split(".")
    return pieces[0] if len(pieces) > 1 else None


class SourceAdapter(ABC):
    platform: SourcePlatform

    @abstractmethod
    def accepts(self, locator: str) -> bool:
        raise NotImplementedError

    @abstractmethod
    def inspect(self, locator: str, settings: Settings) -> list[SourceDescriptor]:
        raise NotImplementedError

    def inspect_with_completeness(
        self,
        locator: str,
        settings: Settings,
    ) -> SourceInspection:
        """Inspect with a complete-by-construction report for legacy adapters."""

        sources = self.inspect(locator, settings)
        entries = [
            InspectionEntry(
                ordinal=index,
                status=InspectionEntryStatus.ACCESSIBLE,
                source_id=source.id,
                remote_id=str(source.metadata.get("id") or "") or None,
                title=source.title,
                locator=source.canonical_url or source.locator,
            )
            for index, source in enumerate(sources, start=1)
        ]
        return SourceInspection(
            sources=sources,
            completeness=InspectionCompleteness(
                locator=locator,
                platform=self.platform,
                expected_entries=len(entries),
                accessible_entries=len(entries),
                inaccessible_entries=0,
                failed_entries=0,
                completeness_proven=True,
                entries=entries,
            ),
        )

    @abstractmethod
    def materialize(
        self,
        source: SourceDescriptor,
        destination: Path,
        settings: Settings,
        *,
        need_media: bool,
    ) -> MaterializedSource:
        raise NotImplementedError


def _float(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return max(0.0, result)


def _chapters(data: dict[str, Any]) -> list[Chapter]:
    result: list[Chapter] = []
    for raw in data.get("chapters") or []:
        if not isinstance(raw, dict):
            continue
        start = _float(raw.get("start_time"))
        if start is None:
            continue
        result.append(
            Chapter(
                title=str(raw.get("title") or f"Chapter {len(result) + 1}"),
                start=start,
                end=_float(raw.get("end_time")),
            )
        )
    return sorted(result, key=lambda item: item.start)


def _captions(data: dict[str, Any]) -> list[CaptionTrack]:
    tracks: list[CaptionTrack] = []
    for automatic, key in ((False, "subtitles"), (True, "automatic_captions")):
        group = data.get(key) or {}
        if not isinstance(group, dict):
            continue
        for language, variants in group.items():
            usable = [raw for raw in variants or [] if isinstance(raw, dict)]
            if not usable:
                continue
            priority = {"vtt": 0, "srt": 1, "ttml": 2, "json3": 3}
            raw = min(
                usable,
                key=lambda item: priority.get(str(item.get("ext") or ""), 10),
            )
            tracks.append(
                CaptionTrack(
                    language=str(language),
                    extension=str(raw.get("ext") or "vtt"),
                    automatic=automatic,
                    name=raw.get("name"),
                )
            )
    return tracks


def _estimated_media_bytes(data: dict[str, Any]) -> int | None:
    candidates: list[int] = []
    for item in data.get("formats") or []:
        if not isinstance(item, dict):
            continue
        height = item.get("height")
        if isinstance(height, (int, float)) and height > 720:
            continue
        size = item.get("filesize") or item.get("filesize_approx")
        if isinstance(size, (int, float)) and size > 0:
            candidates.append(int(size))
    return max(candidates) if candidates else None


def _source_from_ytdlp(
    data: dict[str, Any],
    *,
    platform: SourcePlatform,
    fallback_locator: str,
    playlist_title: str | None = None,
    playlist_index: int | None = None,
) -> SourceDescriptor:
    remote_id = str(data.get("id") or stable_hash(data, length=16))
    canonical = data.get("webpage_url") or data.get("original_url") or fallback_locator
    title = str(data.get("title") or data.get("fulltitle") or remote_id)
    descriptor = SourceDescriptor(
        id=f"{platform.value}-{remote_id}",
        platform=platform,
        locator=str(canonical),
        canonical_url=str(canonical),
        title=title,
        creator=data.get("channel") or data.get("uploader") or data.get("creator"),
        description=data.get("description"),
        duration=_float(data.get("duration")),
        language=data.get("language"),
        playlist_title=playlist_title or data.get("playlist_title") or data.get("playlist"),
        playlist_index=playlist_index or data.get("playlist_index"),
        chapters=_chapters(data),
        captions=_captions(data),
        metadata=(
            {
                key: data.get(key)
                for key in (
                    "id",
                    "extractor",
                    "extractor_key",
                    "upload_date",
                    "timestamp",
                    "release_timestamp",
                    "license",
                    "tags",
                    "categories",
                    "thumbnail",
                )
                if data.get(key) is not None
            }
            | (
                {"estimated_media_bytes": estimated}
                if (estimated := _estimated_media_bytes(data)) is not None
                else {}
            )
        ),
    )
    return descriptor


def source_cache_identity(source: SourceDescriptor) -> dict[str, Any]:
    """Exclude signed/expiring URLs while retaining evidence-changing metadata."""

    return {
        "id": source.id,
        "platform": source.platform.value,
        "canonical_url": source.canonical_url,
        "duration": source.duration,
        "chapters": [item.model_dump(mode="json") for item in source.chapters],
        "captions": [
            {
                "language": item.language,
                "extension": item.extension,
                "automatic": item.automatic,
            }
            for item in source.captions
        ],
        "upload_date": source.metadata.get("upload_date"),
        "release_timestamp": source.metadata.get("release_timestamp"),
        "local_sidecar_captions": source.metadata.get("sidecar_captions"),
    }


class LocalSourceAdapter(SourceAdapter):
    platform = SourcePlatform.LOCAL

    def accepts(self, locator: str) -> bool:
        path = Path(locator).expanduser()
        return path.exists() and path.is_file()

    def inspect(self, locator: str, settings: Settings) -> list[SourceDescriptor]:
        path = Path(locator).expanduser().resolve()
        if not path.is_file():
            raise SourceError(f"Local input does not exist or is not a file: {path}")
        if path.suffix.lower() not in MEDIA_EXTENSIONS:
            guessed, _ = mimetypes.guess_type(path)
            if not guessed or not (guessed.startswith("video/") or guessed.startswith("audio/")):
                raise SourceError(f"Unsupported local media type: {path.suffix or path.name}")
        size = path.stat().st_size
        if size > settings.max_local_file_bytes:
            raise SourceError(
                f"Local file exceeds configured limit "
                f"({size} > {settings.max_local_file_bytes} bytes): {path}"
            )
        require_program(settings.ffprobe)
        result = run_command(
            [
                settings.ffprobe,
                "-v",
                "error",
                "-show_format",
                "-show_streams",
                "-of",
                "json",
                str(path),
            ],
            timeout=settings.command_timeout_seconds,
            provenance_operation="inspect-local-media",
        )
        try:
            probe = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise SourceError(f"ffprobe returned invalid metadata for {path}") from exc
        duration = _float((probe.get("format") or {}).get("duration"))
        if duration and duration > settings.max_source_duration_seconds:
            raise SourceError(
                f"Media duration {duration:.0f}s exceeds configured limit "
                f"{settings.max_source_duration_seconds}s: {path}"
            )
        tags = (probe.get("format") or {}).get("tags") or {}
        content_hash = hash_file(path)
        sidecars = _sidecar_caption_paths(path)
        sidecar_metadata = [
            {
                "name": item.name,
                "size": item.stat().st_size,
                "mtime_ns": item.stat().st_mtime_ns,
                "sha256": hash_file(item),
            }
            for item in sidecars
        ]
        return [
            SourceDescriptor(
                id=f"local-{content_hash[:20]}",
                platform=SourcePlatform.LOCAL,
                locator=str(path),
                title=str(tags.get("title") or path.stem),
                creator=tags.get("artist") or tags.get("author"),
                duration=duration,
                captions=[
                    CaptionTrack(
                        language=_sidecar_language(path, item) or "und",
                        extension=item.suffix.lower().lstrip("."),
                        automatic=False,
                        name=item.name,
                    )
                    for item in sidecars
                ],
                metadata={
                    "filename": path.name,
                    "size": size,
                    "mtime_ns": path.stat().st_mtime_ns,
                    "sha256": content_hash,
                    "sidecar_captions": sidecar_metadata,
                    "format": (probe.get("format") or {}).get("format_name"),
                    "streams": [
                        {
                            key: stream.get(key)
                            for key in (
                                "codec_type",
                                "codec_name",
                                "width",
                                "height",
                                "sample_rate",
                            )
                            if stream.get(key) is not None
                        }
                        for stream in probe.get("streams") or []
                    ],
                },
            )
        ]

    def materialize(
        self,
        source: SourceDescriptor,
        destination: Path,
        settings: Settings,
        *,
        need_media: bool,
    ) -> MaterializedSource:
        del settings, need_media
        path = Path(source.locator).expanduser().resolve()
        if not path.is_file():
            raise SourceError(f"Local media disappeared: {path}")
        destination.mkdir(parents=True, exist_ok=True)
        caption_paths = _sidecar_caption_paths(path)
        content_hash = stable_hash(
            {
                "media": str(source.metadata.get("sha256") or hash_file(path)),
                "captions": [
                    {"name": item.name, "sha256": hash_file(item)} for item in caption_paths
                ],
            }
        )
        return MaterializedSource(
            source=source,
            directory=destination,
            media_path=path,
            caption_paths=caption_paths,
            content_hash=content_hash,
        )


class YtDlpSourceAdapter(SourceAdapter):
    hosts: tuple[str, ...] = ()

    def accepts(self, locator: str) -> bool:
        try:
            parsed = urlparse(locator)
        except ValueError:
            return False
        if parsed.scheme not in {"http", "https"}:
            return False
        host = parsed.hostname or ""
        return any(host == allowed or host.endswith(f".{allowed}") for allowed in self.hosts)

    def _base_args(self, settings: Settings) -> list[str]:
        require_program(settings.yt_dlp)
        args = [settings.yt_dlp, "--ignore-config", "--no-warnings"]
        if settings.cookies_from_browser:
            args += ["--cookies-from-browser", settings.cookies_from_browser]
        if settings.cookies_file:
            cookie_path = settings.cookies_file.expanduser().resolve()
            if not cookie_path.is_file():
                raise SourceError(f"Cookie file does not exist: {cookie_path}")
            args += ["--cookies", str(cookie_path)]
        return args

    @staticmethod
    def _reported_entry_count(payload: dict[str, Any], observed: int) -> int:
        reported: list[int] = []
        for key in ("playlist_count", "n_entries"):
            raw_value = payload.get(key)
            if not isinstance(raw_value, (int, str)):
                continue
            try:
                value = int(raw_value)
            except (TypeError, ValueError):
                continue
            if value >= 0:
                reported.append(value)
        return max([observed, *reported])

    @staticmethod
    def _missing_entry(
        raw: object,
        *,
        ordinal: int,
        fallback_locator: str,
    ) -> InspectionEntry:
        if raw is None:
            return InspectionEntry(
                ordinal=ordinal,
                status=InspectionEntryStatus.INACCESSIBLE,
                locator=fallback_locator,
                reason=(
                    "yt-dlp returned no metadata; this item may be private, deleted, "
                    "geo-blocked, or otherwise unavailable"
                ),
            )
        if not isinstance(raw, dict):
            return InspectionEntry(
                ordinal=ordinal,
                status=InspectionEntryStatus.FAILED,
                locator=fallback_locator,
                reason=f"yt-dlp returned a malformed {type(raw).__name__} playlist entry",
            )
        availability = str(raw.get("availability") or "").strip()
        error = str(raw.get("error") or "").strip()
        status = (
            InspectionEntryStatus.INACCESSIBLE
            if availability and availability.lower() not in {"public", "unlisted"}
            else InspectionEntryStatus.FAILED
        )
        reason = error or (
            f"yt-dlp reported availability '{availability}' without a stable video id"
            if availability
            else "yt-dlp returned an ID-less playlist entry"
        )
        return InspectionEntry(
            ordinal=ordinal,
            status=status,
            title=str(raw.get("title") or "").strip() or None,
            locator=str(raw.get("webpage_url") or raw.get("url") or fallback_locator),
            reason=reason[:1200],
        )

    def inspect_with_completeness(
        self,
        locator: str,
        settings: Settings,
    ) -> SourceInspection:
        result = run_command(
            [
                *self._base_args(settings),
                "--dump-single-json",
                "--skip-download",
                "--ignore-errors",
                locator,
            ],
            timeout=settings.command_timeout_seconds,
            provenance_operation="inspect-remote-source",
        )
        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise SourceError(f"yt-dlp returned invalid metadata for {locator}") from exc
        if not isinstance(payload, dict):
            raise SourceError(f"yt-dlp returned malformed metadata for {locator}")

        raw_entries = payload.get("entries")
        if isinstance(raw_entries, list):
            playlist_title = payload.get("title") or payload.get("playlist_title")
            sources: list[SourceDescriptor] = []
            entries: list[InspectionEntry] = []
            expected_entries = self._reported_entry_count(payload, len(raw_entries))
            for index in range(1, expected_entries + 1):
                if index > len(raw_entries):
                    entries.append(
                        InspectionEntry(
                            ordinal=index,
                            status=InspectionEntryStatus.FAILED,
                            locator=locator,
                            reason=(
                                "yt-dlp reported this playlist position but omitted its "
                                "entry metadata"
                            ),
                        )
                    )
                    continue
                item = raw_entries[index - 1]
                if not isinstance(item, dict) or not item.get("id"):
                    entries.append(
                        self._missing_entry(
                            item,
                            ordinal=index,
                            fallback_locator=locator,
                        )
                    )
                    continue
                source = _source_from_ytdlp(
                    item,
                    platform=self.platform,
                    fallback_locator=locator,
                    playlist_title=playlist_title,
                    playlist_index=index,
                )
                source.kind = SourceKind.COURSE
                sources.append(source)
                entries.append(
                    InspectionEntry(
                        ordinal=index,
                        status=InspectionEntryStatus.ACCESSIBLE,
                        source_id=source.id,
                        remote_id=str(item["id"]),
                        title=source.title,
                        locator=source.canonical_url or source.locator,
                    )
                )
        else:
            sources = [
                _source_from_ytdlp(
                    payload,
                    platform=self.platform,
                    fallback_locator=locator,
                )
            ]
            entries = [
                InspectionEntry(
                    ordinal=1,
                    status=InspectionEntryStatus.ACCESSIBLE,
                    source_id=sources[0].id,
                    remote_id=str(payload.get("id") or "") or None,
                    title=sources[0].title,
                    locator=sources[0].canonical_url or sources[0].locator,
                )
            ]
            expected_entries = 1
        if expected_entries > settings.max_course_items:
            raise SourceError(
                f"Course contains {expected_entries} items, exceeding configured limit "
                f"{settings.max_course_items}"
            )
        for source in sources:
            if (
                source.duration is not None
                and source.duration > settings.max_source_duration_seconds
            ):
                raise SourceError(
                    f"Video '{source.title}' exceeds configured duration limit "
                    f"({source.duration:.0f}s > {settings.max_source_duration_seconds}s)"
                )
        accessible_entries = sum(
            item.status == InspectionEntryStatus.ACCESSIBLE for item in entries
        )
        inaccessible_entries = sum(
            item.status == InspectionEntryStatus.INACCESSIBLE for item in entries
        )
        failed_entries = sum(item.status == InspectionEntryStatus.FAILED for item in entries)
        completeness_proven = (
            accessible_entries == expected_entries
            and inaccessible_entries == 0
            and failed_entries == 0
        )
        missing = inaccessible_entries + failed_entries
        disclaimer = (
            None
            if completeness_proven
            else (
                f"Course completeness is not proven: {missing} of {expected_entries} "
                "expected entries were inaccessible or could not be inspected."
            )
        )
        return SourceInspection(
            sources=sources,
            completeness=InspectionCompleteness(
                locator=locator,
                platform=self.platform,
                expected_entries=expected_entries,
                accessible_entries=accessible_entries,
                inaccessible_entries=inaccessible_entries,
                failed_entries=failed_entries,
                completeness_proven=completeness_proven,
                disclaimer=disclaimer,
                entries=entries,
            ),
        )

    def inspect(self, locator: str, settings: Settings) -> list[SourceDescriptor]:
        inspected = self.inspect_with_completeness(locator, settings)
        if not inspected.sources:
            raise SourceError(f"No accessible videos were found at {locator}")
        return inspected.sources

    def _preferred_caption_languages(self, source: SourceDescriptor, requested: str) -> list[str]:
        available = source.captions
        if not available:
            return []
        requested_lower = requested.lower()
        candidates: list[CaptionTrack] = []
        if requested_lower != "auto":
            candidates.extend(
                track
                for track in available
                if track.language.lower() == requested_lower
                or track.language.lower().startswith(f"{requested_lower}-")
            )
        if source.language:
            candidates.extend(
                track
                for track in available
                if track.language.lower().startswith(source.language.lower())
            )
        platform_default = "zh" if self.platform == SourcePlatform.BILIBILI else "en"
        candidates.extend(
            track for track in available if track.language.startswith(platform_default)
        )
        candidates.extend(track for track in available if track.language.startswith("en"))
        candidates.extend(track for track in available if track.language.startswith("zh"))
        candidates.extend(available)
        unique: list[CaptionTrack] = []
        seen: set[str] = set()
        for track in sorted(candidates, key=lambda item: item.automatic):
            if track.language not in seen:
                unique.append(track)
                seen.add(track.language)
        return [track.language for track in unique[:2]]

    def materialize(
        self,
        source: SourceDescriptor,
        destination: Path,
        settings: Settings,
        *,
        need_media: bool,
    ) -> MaterializedSource:
        destination.mkdir(parents=True, exist_ok=True)
        output_template = str(destination / "media.%(ext)s")
        args = [
            *self._base_args(settings),
            "--no-playlist",
            "--write-info-json",
            "--retries",
            "5",
            "--fragment-retries",
            "5",
            "--socket-timeout",
            "30",
            "--max-filesize",
            str(settings.max_download_bytes),
            "-o",
            output_template,
        ]
        languages = self._preferred_caption_languages(source, settings.language)
        if languages:
            args += [
                "--write-subs",
                "--write-auto-subs",
                "--sub-langs",
                ",".join(languages),
                "--sub-format",
                "vtt/srt/best",
            ]
        if need_media:
            args += [
                "-f",
                "bv*[height<=720]+ba/b[height<=720]/ba/b",
                "--merge-output-format",
                "mp4",
            ]
        else:
            args.append("--skip-download")
        args.append(source.locator)
        run_command(
            args,
            timeout=settings.command_timeout_seconds,
            provenance_operation="materialize-remote-source",
        )

        caption_paths = sorted(
            path
            for path in destination.iterdir()
            if path.is_file() and path.suffix.lower() in CAPTION_EXTENSIONS
        )
        info_paths = sorted(destination.glob("*.info.json"))
        media_candidates = sorted(
            path
            for path in destination.iterdir()
            if path.is_file() and path.suffix.lower() in MEDIA_EXTENSIONS
        )
        media_path = media_candidates[0] if media_candidates else None
        if need_media and media_path is None:
            raise SourceError(f"yt-dlp did not produce media for '{source.title}'")
        content_hash = stable_hash(
            {
                "source": source_cache_identity(source),
                "media_size": media_path.stat().st_size if media_path else None,
                "captions": [
                    {
                        "name": path.name,
                        "size": path.stat().st_size,
                        "sha256": hash_file(path),
                    }
                    for path in caption_paths
                ],
            }
        )
        return MaterializedSource(
            source=source,
            directory=destination,
            media_path=media_path,
            caption_paths=caption_paths,
            metadata_path=info_paths[0] if info_paths else None,
            content_hash=content_hash,
        )


class YouTubeSourceAdapter(YtDlpSourceAdapter):
    platform = SourcePlatform.YOUTUBE
    hosts = ("youtube.com", "youtu.be")


class BilibiliSourceAdapter(YtDlpSourceAdapter):
    platform = SourcePlatform.BILIBILI
    hosts = ("bilibili.com", "b23.tv")


class SourceRegistry:
    def __init__(self, adapters: list[SourceAdapter] | None = None):
        if adapters is not None:
            self.adapters = adapters
            return
        self.adapters = [
            LocalSourceAdapter(),
            YouTubeSourceAdapter(),
            BilibiliSourceAdapter(),
        ]
        built_in_names = {"local", "youtube", "bilibili"}
        for entry_point in entry_points(group="video_to_skill.source_adapters"):
            if entry_point.name in built_in_names:
                continue
            candidate = entry_point.load()()
            if not isinstance(candidate, SourceAdapter):
                raise SourceError(
                    f"Source adapter entry point '{entry_point.name}' does not "
                    "implement SourceAdapter"
                )
            self.adapters.append(candidate)

    def adapter_for(self, locator: str) -> SourceAdapter:
        matches = [adapter for adapter in self.adapters if adapter.accepts(locator)]
        if not matches:
            parsed = urlparse(locator)
            if parsed.scheme in {"http", "https"}:
                raise SourceError(
                    "Unsupported URL host. Production inputs are limited to YouTube and Bilibili."
                )
            raise SourceError(f"Input is not a supported media file or URL: {locator}")
        return matches[0]

    def inspect(self, locators: list[str], settings: Settings) -> list[SourceDescriptor]:
        batch = self.inspect_with_completeness(locators, settings)
        if not batch.sources:
            raise SourceError("No accessible videos were found in the requested inputs")
        return batch.sources

    def inspect_with_completeness(
        self,
        locators: list[str],
        settings: Settings,
    ) -> InspectionBatch:
        sources: list[SourceDescriptor] = []
        reports: list[InspectionCompleteness] = []
        seen: set[str] = set()
        for locator in locators:
            adapter = self.adapter_for(locator)
            inspected = adapter.inspect_with_completeness(locator, settings)
            reports.append(inspected.completeness)
            for source in inspected.sources:
                if source.id not in seen:
                    sources.append(source)
                    seen.add(source.id)
        return InspectionBatch(sources=sources, reports=reports)

    def materialize(
        self,
        source: SourceDescriptor,
        destination: Path,
        settings: Settings,
        *,
        need_media: bool,
    ) -> MaterializedSource:
        adapter = next((item for item in self.adapters if item.platform == source.platform), None)
        if adapter is None:
            raise SourceError(f"No adapter registered for {source.platform}")
        return adapter.materialize(source, destination, settings, need_media=need_media)


def describe_source(source: SourceDescriptor) -> str:
    duration = ""
    if source.duration is not None:
        duration = f" ({source.duration / 60:.1f} min)"
    course = f" [{source.playlist_index}]" if source.playlist_index else ""
    return f"{source.platform.value}{course}: {source.title}{duration}"


def local_source_changed(source: SourceDescriptor) -> bool:
    if source.platform != SourcePlatform.LOCAL:
        return False
    path = Path(source.locator)
    if not path.is_file():
        return True
    stat = path.stat()
    if stat.st_size != source.metadata.get("size") or stat.st_mtime_ns != source.metadata.get(
        "mtime_ns"
    ):
        return True
    expected = {
        (item.get("name"), item.get("size"), item.get("mtime_ns"))
        for item in source.metadata.get("sidecar_captions") or []
        if isinstance(item, dict)
    }
    current = {
        (item.name, item.stat().st_size, item.stat().st_mtime_ns)
        for item in _sidecar_caption_paths(path)
    }
    return expected != current
