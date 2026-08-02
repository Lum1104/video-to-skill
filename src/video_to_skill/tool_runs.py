"""Engine-owned, sanitized provenance for deterministic tool executions."""

from __future__ import annotations

import importlib.metadata
import json
import os
import re
import subprocess
import threading
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from video_to_skill.utils import hash_file, is_within, stable_hash

TOOL_RUN_SCHEMA = """
CREATE TABLE IF NOT EXISTS tool_runs (
    id TEXT PRIMARY KEY,
    identity_digest TEXT NOT NULL UNIQUE,
    tool TEXT NOT NULL,
    tool_version TEXT,
    operation TEXT NOT NULL,
    source_id TEXT,
    stage TEXT,
    input_digests_json TEXT NOT NULL,
    arguments_json TEXT NOT NULL,
    cache_key TEXT,
    attempt_count INTEGER NOT NULL DEFAULT 0 CHECK(attempt_count >= 0),
    cache_hit_count INTEGER NOT NULL DEFAULT 0 CHECK(cache_hit_count >= 0),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS tool_run_source_stage
    ON tool_runs(source_id, stage, updated_at);
CREATE TABLE IF NOT EXISTS tool_run_attempts (
    id TEXT PRIMARY KEY,
    tool_run_id TEXT NOT NULL,
    generation INTEGER NOT NULL CHECK(generation >= 1),
    status TEXT NOT NULL CHECK(status IN ('running', 'complete', 'failed')),
    outputs_json TEXT NOT NULL DEFAULT '[]',
    return_code INTEGER,
    error_kind TEXT,
    started_at TEXT NOT NULL,
    completed_at TEXT,
    duration_ms INTEGER,
    updated_at TEXT NOT NULL,
    UNIQUE(tool_run_id, generation),
    FOREIGN KEY (tool_run_id) REFERENCES tool_runs(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS tool_run_attempt_status
    ON tool_run_attempts(tool_run_id, status, generation);
"""

MAX_SANITIZE_DEPTH = 8
MAX_SANITIZE_NODES = 2_048
MAX_SANITIZE_BYTES = 256 * 1_024
MAX_SANITIZE_STRING_CHARS = 4_096
MAX_ARGUMENT_BYTES = 256 * 1_024
MAX_OUTPUT_BYTES = 768 * 1_024
MAX_EXPORTED_RECORD_BYTES = 1024 * 1_024

_SECRET_KEY_RE = re.compile(
    r"(?i)(authorization|api.?key|token|cookie|password|passwd|secret|credential|session)"
)
_SECRET_FLAG_RE = re.compile(
    r"(?i)^--?(authorization|add-header|api.?key|token|cookie|cookies|cookies-from-browser|password|passwd|username|secret|credential|session)(?:=|$)"
)
_BEARER_RE = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]+")
_ASSIGNMENT_SECRET_RE = re.compile(
    r"(?i)(?P<prefix>[\"']?(?:x[-_])?(?:api[-_]?key|token|password|passwd|cookie|secret|authorization|credential|session)[\"']?\s*[:=]\s*[\"']?)[^\"'\s,;}\]]+"
)
_USERINFO_RE = re.compile(r"(?i)(?<![A-Za-z0-9._~+\-])[^\s:@/]+:[^\s@/]+@(?P<host>[A-Za-z0-9.-]+)")
_TEMPORARY_COMPONENT_RE = re.compile(r"^\.(?:[^/]*\.)?[A-Za-z0-9_-]{8,}(?:\.tmp)?$")
_PRINTF_PATTERN_RE = re.compile(r"%(?:\([^)]+\))?[#0 +\-]*\d*(?:\.\d+)?[a-zA-Z]")
_WINDOWS_ABSOLUTE_RE = re.compile(r"^(?:[A-Za-z]:[\\/]|\\\\)")
_EMBEDDED_POSIX_PATH_RE = re.compile(r"(?P<prefix>^|[\s'\"=:(,\[\]{])/(?!/)[^\s'\";,)\]}]+")
_EMBEDDED_WINDOWS_PATH_RE = re.compile(
    r"(?P<prefix>^|[\s'\"=:(,\[\]{])(?:[A-Za-z]:[\\/]|\\\\)[^\s'\";,)\]}]+"
)
_URI_RE = re.compile(r"(?i)(?P<uri>[a-z][a-z0-9+.-]*://[^\s'\"<>]+)")
_OPAQUE_URI_SCHEMES = "data|s3|gs|ftp|ftps|ssh|scp|mongodb|postgres|postgresql|redis"
_OPAQUE_URI_RE = re.compile(rf"(?i)^(?P<scheme>{_OPAQUE_URI_SCHEMES}):")
_EMBEDDED_OPAQUE_URI_RE = re.compile(
    rf"(?i)(?P<prefix>^|[\s'\"=:(,\[\]{{])(?P<scheme>{_OPAQUE_URI_SCHEMES}):[^\s'\"<>]+"
)
_TILDE_PATH_RE = re.compile(r"(?P<prefix>^|[\s'\"=:(,\[\]{])~(?:[^/\s'\"]*)/[^\s'\";,)\]}]+")
_PUBLIC_IDENTITY_QUERY_KEYS = frozenset({"aid", "bvid", "list", "p", "v"})
_VERSION_CACHE: dict[str, str | None] = {}
_VERSION_LOCK = threading.Lock()


class ToolRunStore(Protocol):
    root: Path

    def start_tool_run(self, start: ToolRunStart) -> str: ...

    def finish_tool_run(self, finish: ToolRunFinish) -> None: ...


@dataclass(frozen=True)
class ToolRunStart:
    id: str
    identity_digest: str
    tool: str
    tool_version: str | None
    operation: str
    source_id: str | None
    stage: str | None
    input_digests: dict[str, str]
    arguments: dict[str, Any]
    cache_key: str | None
    started_at: datetime


@dataclass(frozen=True)
class ToolRunFinish:
    attempt_id: str
    status: str
    outputs: list[dict[str, object]]
    return_code: int | None
    error_kind: str | None
    completed_at: datetime
    duration_ms: int


@dataclass(frozen=True)
class ToolRunContext:
    store: ToolRunStore
    workspace_root: Path
    source_id: str | None
    stage: str | None
    cache_key: str | None
    input_digests: dict[str, str]
    file_digests: dict[tuple[str, int, int], str] = field(default_factory=dict)


_CURRENT_CONTEXT: ContextVar[ToolRunContext | None] = ContextVar(
    "video_to_skill_tool_run_context",
    default=None,
)


def _sha256_json(value: Any) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return sha256(payload).hexdigest()


def _validated_digest(value: str) -> str:
    normalized = value.casefold()
    if not re.fullmatch(r"[a-f0-9]{64}", normalized):
        raise ValueError("tool-run input digests must be complete SHA-256 values")
    return normalized


def bounded_json_text(value: Any, *, max_bytes: int, label: str) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    if len(payload.encode("utf-8")) > max_bytes:
        raise ValueError(f"{label} exceeds the {max_bytes}-byte limit")
    return payload


@contextmanager
def tool_run_scope(
    store: ToolRunStore,
    *,
    source_id: str | None,
    stage: str | None,
    cache_key: str | None = None,
    input_digests: Mapping[str, str] | None = None,
) -> Iterator[None]:
    """Bind engine identity to every nested subprocess/provider call."""

    if len(input_digests or {}) > MAX_SANITIZE_NODES:
        raise ValueError(f"tool-run inputs cannot exceed {MAX_SANITIZE_NODES} items")
    normalized_inputs: dict[str, str] = {}
    for key, value in sorted((input_digests or {}).items()):
        sanitized_key = _sanitize_text(str(key), workspace_root=store.root)
        if sanitized_key in normalized_inputs:
            raise ValueError("tool-run input labels collide after sanitization")
        normalized_inputs[sanitized_key] = _validated_digest(value)
    context = ToolRunContext(
        store=store,
        workspace_root=store.root.resolve(),
        source_id=source_id,
        stage=stage,
        cache_key=cache_key,
        input_digests=normalized_inputs,
    )
    token = _CURRENT_CONTEXT.set(context)
    try:
        yield
    finally:
        _CURRENT_CONTEXT.reset(token)


def current_tool_run_context() -> ToolRunContext | None:
    return _CURRENT_CONTEXT.get()


def digest_value(value: Any) -> str:
    """Return a complete digest for a structured logical input or output."""

    return _sha256_json(value)


def _safe_url_label(value: str) -> str | None:
    try:
        parsed = urlparse(value)
    except ValueError:
        return None
    if parsed.scheme not in {"http", "https"}:
        return None
    host = (parsed.hostname or "unknown-host").casefold()
    return f"<url-host:{host}>"


def _safe_workspace_path(path: Path, root: Path) -> str:
    try:
        lexical = path.expanduser().resolve(strict=False)
        lexical_root = root.expanduser().resolve(strict=False)
    except (OSError, RuntimeError):
        return "<external-path>"
    try:
        relative = lexical.relative_to(lexical_root)
    except ValueError:
        return "<external-path>"
    parts = [
        "<temporary>" if _TEMPORARY_COMPONENT_RE.fullmatch(part) else part
        for part in relative.parts
    ]
    return "workspace:" + Path(*parts).as_posix()


def _sanitize_uri(value: str) -> str:
    try:
        parsed = urlparse(value)
    except ValueError:
        return "<uri>"
    scheme = parsed.scheme.casefold() or "unknown"
    if scheme == "file":
        return "<external-path>"
    host = (parsed.hostname or "unknown-host").casefold()
    if scheme in {"http", "https"}:
        return f"<url-host:{host}>"
    return f"<uri:{scheme}:{host}>"


def _sanitize_text(value: str, *, workspace_root: Path) -> str:
    if len(value) > MAX_SANITIZE_STRING_CHARS:
        raise ValueError(f"tool-run strings cannot exceed {MAX_SANITIZE_STRING_CHARS} characters")
    if value.startswith("~") and re.match(r"^~(?:[^/\s]*)/", value):
        return "<external-path>"
    if _WINDOWS_ABSOLUTE_RE.match(value):
        return "<external-path>"
    if Path(value).is_absolute():
        return _safe_workspace_path(Path(value), workspace_root)
    if opaque_match := _OPAQUE_URI_RE.match(value):
        if "://" in value:
            return _sanitize_uri(value)
        return f"<uri:{opaque_match.group('scheme').casefold()}>"
    text = _BEARER_RE.sub("Bearer <redacted>", value)
    text = _URI_RE.sub(lambda match: _sanitize_uri(match.group("uri")), text)
    text = _USERINFO_RE.sub(lambda match: f"<credentials>@{match.group('host').casefold()}", text)
    text = _EMBEDDED_OPAQUE_URI_RE.sub(
        lambda match: match.group("prefix") + f"<uri:{match.group('scheme').casefold()}>",
        text,
    )
    text = _ASSIGNMENT_SECRET_RE.sub(lambda match: match.group("prefix") + "<redacted>", text)
    text = _TILDE_PATH_RE.sub(lambda match: match.group("prefix") + "<external-path>", text)
    text = _EMBEDDED_WINDOWS_PATH_RE.sub(
        lambda match: match.group("prefix") + "<external-path>", text
    )
    text = _EMBEDDED_POSIX_PATH_RE.sub(
        lambda match: match.group("prefix") + "<external-path>", text
    )
    return text


@dataclass
class _SanitizeBudget:
    nodes: int = 0
    bytes: int = 0

    def add_node(self) -> None:
        self.nodes += 1
        if self.nodes > MAX_SANITIZE_NODES:
            raise ValueError(f"tool-run values cannot exceed {MAX_SANITIZE_NODES} cumulative nodes")

    def add_text(self, value: str) -> None:
        if len(value) > MAX_SANITIZE_STRING_CHARS:
            raise ValueError(
                f"tool-run strings cannot exceed {MAX_SANITIZE_STRING_CHARS} characters"
            )
        self.bytes += len(value.encode("utf-8"))
        if self.bytes > MAX_SANITIZE_BYTES:
            raise ValueError(
                f"tool-run values cannot exceed the {MAX_SANITIZE_BYTES}-byte cumulative budget"
            )


def _validate_sanitize_budget(
    value: Any,
    *,
    budget: _SanitizeBudget,
    depth: int = 0,
) -> None:
    if depth > MAX_SANITIZE_DEPTH:
        raise ValueError(f"tool-run values cannot exceed depth {MAX_SANITIZE_DEPTH}")
    budget.add_node()
    if value is None or isinstance(value, (bool, int, float)):
        budget.add_text(json.dumps(value, separators=(",", ":")))
        return
    if isinstance(value, Path):
        budget.add_text(str(value))
        return
    if isinstance(value, Mapping):
        for item_key, item_value in value.items():
            budget.add_node()
            budget.add_text(str(item_key))
            _validate_sanitize_budget(item_value, budget=budget, depth=depth + 1)
        return
    if isinstance(value, (list, tuple)):
        for item in value:
            _validate_sanitize_budget(item, budget=budget, depth=depth + 1)
        return
    budget.add_text(str(value))


def sanitize_value(
    value: Any,
    *,
    workspace_root: Path,
    key: str | None = None,
    _depth: int = 0,
    _budget_checked: bool = False,
) -> Any:
    """Recursively retain reproducibility knobs while removing secret and host data."""

    if not _budget_checked:
        _validate_sanitize_budget(value, budget=_SanitizeBudget(), depth=_depth)
    if _depth > MAX_SANITIZE_DEPTH:
        raise ValueError(f"tool-run values cannot exceed depth {MAX_SANITIZE_DEPTH}")
    if key is not None and _SECRET_KEY_RE.search(key):
        return "<redacted>"
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, Path):
        return _safe_workspace_path(value, workspace_root)
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for item_key, item_value in sorted(value.items(), key=lambda item: str(item[0])):
            raw_key = str(item_key)
            sanitized_key = _sanitize_text(raw_key, workspace_root=workspace_root)
            if sanitized_key in result:
                raise ValueError("tool-run keys collide after sanitization")
            result[sanitized_key] = sanitize_value(
                item_value,
                workspace_root=workspace_root,
                key=raw_key,
                _depth=_depth + 1,
                _budget_checked=True,
            )
        return result
    if isinstance(value, (list, tuple)):
        return [
            sanitize_value(
                item,
                workspace_root=workspace_root,
                _depth=_depth + 1,
                _budget_checked=True,
            )
            for item in value
        ]
    return _sanitize_text(str(value), workspace_root=workspace_root)


def sanitize_arguments(args: Sequence[str], *, workspace_root: Path) -> list[str]:
    """Normalize argv without retaining credentials, URLs, or private absolute paths."""

    _validate_sanitize_budget(args, budget=_SanitizeBudget())
    sanitized: list[str] = []
    redact_next = False
    for index, raw in enumerate(args):
        if index == 0:
            sanitized.append(_sanitize_text(Path(raw).name, workspace_root=workspace_root))
            continue
        if redact_next:
            sanitized.append("<redacted>")
            redact_next = False
            continue
        if _SECRET_FLAG_RE.match(raw):
            if "=" in raw:
                sanitized.append(raw.split("=", 1)[0] + "=<redacted>")
            else:
                sanitized.append(raw)
                redact_next = True
            continue
        if raw.startswith("-") and "=" in raw:
            option, option_value = raw.split("=", 1)
            sanitized.append(
                option
                + "="
                + str(
                    sanitize_value(
                        option_value,
                        workspace_root=workspace_root,
                        _budget_checked=True,
                    )
                )
            )
            continue
        sanitized.append(
            str(sanitize_value(raw, workspace_root=workspace_root, _budget_checked=True))
        )
    return sanitized


def _tool_name(program: str) -> str:
    return Path(program).name.casefold()


def _first_version_line(program: str) -> str | None:
    tool = _tool_name(program)
    package = {"yt-dlp": "yt-dlp"}.get(tool)
    if package is not None:
        try:
            return importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError:
            pass
    flag = "-version" if tool in {"ffmpeg", "ffprobe"} else "--version"
    try:
        result = subprocess.run(
            [program, flag],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    first = (result.stdout or result.stderr).splitlines()
    if not first:
        return None
    compact = " ".join(first[0].split())
    return compact[:240] or None


def resolve_tool_version(program: str) -> str | None:
    """Resolve a bounded version string without recording command output."""

    key = os.path.abspath(program) if Path(program).is_absolute() else program
    with _VERSION_LOCK:
        if key in _VERSION_CACHE:
            return _VERSION_CACHE[key]
        version = _first_version_line(program)
        _VERSION_CACHE[key] = version
        return version


def python_package_version(package: str) -> str | None:
    try:
        return importlib.metadata.version(package)
    except importlib.metadata.PackageNotFoundError:
        return None


def _infer_input_paths(args: Sequence[str]) -> list[Path]:
    tool = _tool_name(args[0]) if args else ""
    candidates: list[Path] = []
    for index, item in enumerate(args[:-1]):
        if item == "-i":
            candidates.append(Path(args[index + 1]))
    if tool == "ffprobe" and args:
        candidates.append(Path(args[-1]))
    return candidates


def _expand_output_pattern(path: Path) -> list[Path]:
    if "%" not in path.name:
        return [path] if path.is_file() else []
    pattern = _PRINTF_PATTERN_RE.sub("*", path.name)
    try:
        return sorted(candidate for candidate in path.parent.glob(pattern) if candidate.is_file())
    except OSError:
        return []


def _infer_output_paths(args: Sequence[str]) -> list[Path]:
    if not args:
        return []
    tool = _tool_name(args[0])
    raw_paths: list[Path] = []
    if tool in {"ffmpeg", "yt-dlp"}:
        if tool == "ffmpeg" and len(args) > 1:
            raw_paths.append(Path(args[-1]))
        for index, item in enumerate(args[:-1]):
            if item in {"-o", "--output"}:
                raw_paths.append(Path(args[index + 1]))
    result: list[Path] = []
    for path in raw_paths:
        result.extend(_expand_output_pattern(path))
    return sorted(set(result))


def _path_inputs(paths: Sequence[Path], context: ToolRunContext) -> dict[str, str]:
    result = dict(context.input_digests)
    for index, path in enumerate(paths):
        try:
            resolved = path.expanduser().resolve(strict=True)
            if resolved.is_file():
                result[f"file-{index}"] = _cached_file_digest(resolved, context)
        except (OSError, RuntimeError):
            continue
    return dict(sorted(result.items()))


def _cached_file_digest(path: Path, context: ToolRunContext) -> str:
    metadata = path.stat()
    key = (str(path), metadata.st_size, metadata.st_mtime_ns)
    digest = context.file_digests.get(key)
    if digest is None:
        digest = hash_file(path)
        context.file_digests[key] = digest
    return digest


def _output_records(paths: Sequence[Path], context: ToolRunContext) -> list[dict[str, object]]:
    outputs: list[dict[str, object]] = []
    for path in sorted(set(paths)):
        try:
            resolved = path.expanduser().resolve(strict=True)
            if not resolved.is_file() or not is_within(resolved, context.workspace_root):
                continue
            relative = resolved.relative_to(context.workspace_root).as_posix()
            outputs.append(
                {
                    "kind": "file",
                    "path": relative,
                    "sha256": _cached_file_digest(resolved, context),
                    "size": resolved.stat().st_size,
                }
            )
        except (OSError, RuntimeError, ValueError):
            continue
    return outputs


@dataclass
class TrackedToolRun:
    context: ToolRunContext | None
    id: str | None
    attempt_id: str | None
    started_at: datetime
    inferred_outputs: list[Path] = field(default_factory=list)
    outputs: list[dict[str, object]] = field(default_factory=list)
    return_code: int | None = None

    def add_output(self, path: str, digest: str, *, size: int | None = None) -> None:
        """Add one workspace-relative file output and its content digest."""

        if self.context is None:
            return
        candidate = Path(path)
        if (
            candidate.is_absolute()
            or "\\" in path
            or not candidate.parts
            or any(part in {"", ".", ".."} for part in candidate.parts)
        ):
            raise ValueError("tool-run output paths must be workspace-relative")
        output: dict[str, object] = {
            "kind": "file",
            "path": path,
            "sha256": _validated_digest(digest),
        }
        if size is not None:
            output["size"] = size
        self.outputs.append(output)

    def add_logical_output(
        self,
        record_type: str,
        record_id: str,
        semantic_digest: str,
    ) -> None:
        """Add a typed semantic output that can be verified against workspace records."""

        if self.context is None:
            return
        if record_type not in {
            "transcript-segments",
            "visual-event-ocr",
            "visual-event-analysis",
        }:
            raise ValueError(f"unsupported logical tool output: {record_type}")
        if (
            not record_id
            or len(record_id) > 512
            or any(ord(character) < 32 for character in record_id)
        ):
            raise ValueError("logical tool output has an invalid record id")
        self.outputs.append(
            {
                "kind": "logical-record",
                "record_type": record_type,
                "record_id": record_id,
                "semantic_sha256": _validated_digest(semantic_digest),
            }
        )


@contextmanager
def tracked_operation(
    *,
    tool: str,
    operation: str,
    arguments: Mapping[str, Any] | Sequence[str] | None = None,
    version: str | None = None,
    input_paths: Sequence[Path] = (),
    output_paths: Sequence[Path] = (),
) -> Iterator[TrackedToolRun]:
    """Record one logical operation when called inside an engine provenance scope."""

    context = current_tool_run_context()
    started_at = datetime.now(UTC)
    if context is None:
        yield TrackedToolRun(
            context=None,
            id=None,
            attempt_id=None,
            started_at=started_at,
        )
        return
    safe_tool = _sanitize_text(Path(tool).name.casefold(), workspace_root=context.workspace_root)
    safe_operation = _sanitize_text(operation, workspace_root=context.workspace_root)
    safe_arguments = sanitize_value(arguments or {}, workspace_root=context.workspace_root)
    if not isinstance(safe_arguments, (dict, list)):
        safe_arguments = {"value": safe_arguments}
    bounded_json_text(
        safe_arguments,
        max_bytes=MAX_ARGUMENT_BYTES,
        label="tool-run arguments",
    )
    inputs = _path_inputs(input_paths, context)
    raw_version = version if version is not None else resolve_tool_version(tool)
    sanitized_version = sanitize_value(raw_version, workspace_root=context.workspace_root)
    resolved_version = str(sanitized_version) if sanitized_version is not None else None
    identity = {
        "tool": safe_tool,
        "tool_version": resolved_version,
        "operation": safe_operation,
        "source_id": context.source_id,
        "stage": context.stage,
        "input_digests": inputs,
        "arguments": safe_arguments,
        "cache_key": context.cache_key,
    }
    identity_digest = _sha256_json(identity)
    run_id = f"toolrun-{identity_digest[:24]}"
    attempt_id = context.store.start_tool_run(
        ToolRunStart(
            id=run_id,
            identity_digest=identity_digest,
            tool=safe_tool,
            tool_version=resolved_version,
            operation=safe_operation,
            source_id=context.source_id,
            stage=context.stage,
            input_digests=inputs,
            arguments={"argv": safe_arguments}
            if isinstance(safe_arguments, list)
            else safe_arguments,
            cache_key=context.cache_key,
            started_at=started_at,
        )
    )
    tracked = TrackedToolRun(
        context=context,
        id=run_id,
        attempt_id=attempt_id,
        started_at=started_at,
        inferred_outputs=list(output_paths),
    )
    error: BaseException | None = None
    try:
        yield tracked
    except BaseException as exc:
        error = exc
        raise
    finally:
        completed_at = datetime.now(UTC)
        status = "complete"
        if error is not None or (tracked.return_code is not None and tracked.return_code != 0):
            status = "failed"
        outputs = [*_output_records(tracked.inferred_outputs, context), *tracked.outputs]
        deduplicated = {
            bounded_json_text(
                item,
                max_bytes=MAX_SANITIZE_STRING_CHARS,
                label="tool output",
            ): item
            for item in outputs
        }
        prepared_outputs = [deduplicated[key] for key in sorted(deduplicated)]
        bounded_json_text(
            prepared_outputs,
            max_bytes=MAX_OUTPUT_BYTES,
            label="tool-run outputs",
        )
        assert tracked.attempt_id is not None
        context.store.finish_tool_run(
            ToolRunFinish(
                attempt_id=tracked.attempt_id,
                status=status,
                outputs=prepared_outputs,
                return_code=tracked.return_code,
                error_kind=type(error).__name__ if error is not None else None,
                completed_at=completed_at,
                duration_ms=max(
                    0,
                    round((completed_at - started_at).total_seconds() * 1000),
                ),
            )
        )


@contextmanager
def tracked_command(
    args: Sequence[str],
    *,
    operation: str | None = None,
    output_paths: Sequence[Path] | None = None,
) -> Iterator[TrackedToolRun]:
    """Record a subprocess through the shared runner boundary."""

    context = current_tool_run_context()
    default_operation = (
        operation or (context.stage if context is not None else "command") or "command"
    )
    root = context.workspace_root if context is not None else Path.cwd()
    with tracked_operation(
        tool=args[0],
        operation=default_operation,
        arguments=sanitize_arguments(args, workspace_root=root),
        input_paths=_infer_input_paths(args),
        output_paths=(_infer_output_paths(args) if output_paths is None else output_paths),
    ) as tracked:
        yield tracked


def opaque_input_set_id(inputs: Sequence[str]) -> str:
    """Identify an inspection input set without retaining locators or signed URLs."""

    identities: list[dict[str, str]] = []
    for locator in inputs:
        if (url_label := _safe_url_label(locator)) is not None:
            parsed = urlparse(locator)
            stable_query = urlencode(
                sorted(
                    (key, value)
                    for key, value in parse_qsl(parsed.query, keep_blank_values=True)
                    if key.casefold() in _PUBLIC_IDENTITY_QUERY_KEYS
                )
            )
            stable_locator = urlunparse(
                (
                    parsed.scheme.casefold(),
                    (parsed.hostname or "").casefold(),
                    parsed.path,
                    "",
                    stable_query,
                    "",
                )
            )
            identities.append(
                {
                    "kind": "url",
                    "host": url_label,
                    "sha256": _sha256_json(stable_locator),
                }
            )
            continue
        path = Path(locator).expanduser()
        digest = hash_file(path) if path.is_file() else _sha256_json({"kind": "missing-local"})
        identities.append({"kind": "local", "sha256": digest})
    return f"input-set-{stable_hash(identities, length=20)}"
