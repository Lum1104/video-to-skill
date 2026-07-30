"""Small deterministic helpers and safe subprocess execution."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import unicodedata
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

from video_to_skill.errors import DependencyError, ProcessingError

_SECRET_RE = re.compile(
    r"(?i)(authorization|api[_-]?key|token|cookie|password)([\"'=:\s]+)([^\s,;]+)"
)


def stable_hash(value: Any, *, length: int = 32) -> str:
    serialized = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str
    )
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()[:length]


def hash_file(path: Path, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()


def slugify(value: str, *, fallback: str = "video-skill") -> str:
    normalized = unicodedata.normalize("NFKD", value)
    ascii_text = normalized.encode("ascii", "ignore").decode("ascii").lower()
    slug = re.sub(r"[^a-z0-9]+", "-", ascii_text).strip("-")
    return slug[:63].rstrip("-") or fallback


def format_timestamp(seconds: float) -> str:
    total = max(0, round(seconds))
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours:02d}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"


def timestamp_url(url: str | None, seconds: float) -> str | None:
    if not url:
        return None
    parsed = urlparse(url)
    host = parsed.netloc.lower()
    query = parse_qs(parsed.query, keep_blank_values=True)
    if "youtube.com" in host or host == "youtu.be":
        query["t"] = [str(max(0, int(seconds)))]
        return urlunparse(parsed._replace(query=urlencode(query, doseq=True)))
    if "bilibili.com" in host or host.endswith("b23.tv"):
        query["t"] = [str(max(0, int(seconds)))]
        return urlunparse(parsed._replace(query=urlencode(query, doseq=True)))
    return url


def redact(text: str) -> str:
    return _SECRET_RE.sub(r"\1\2<redacted>", text)


def require_program(program: str) -> str:
    resolved = shutil.which(program)
    if not resolved:
        raise DependencyError(
            f"Required program '{program}' was not found on PATH. "
            f"Run `video-to-skill doctor` for setup guidance."
        )
    return resolved


def run_command(
    args: Sequence[str],
    *,
    timeout: int,
    cwd: Path | None = None,
    env: Mapping[str, str] | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    """Run without a shell so untrusted locators cannot become commands."""

    if not args:
        raise ValueError("command cannot be empty")
    try:
        result = subprocess.run(
            list(args),
            cwd=cwd,
            env=dict(env) if env else None,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise ProcessingError(f"Command timed out after {timeout}s: {args[0]}") from exc
    except OSError as exc:
        raise ProcessingError(f"Could not start {args[0]}: {exc}") from exc
    if check and result.returncode != 0:
        detail = redact((result.stderr or result.stdout).strip())[-2000:]
        raise ProcessingError(f"{args[0]} failed with exit {result.returncode}: {detail}")
    return result


def atomic_write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent, prefix=f".{path.name}.", suffix=".tmp"
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def atomic_write_json(path: Path, value: Any) -> None:
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")
    atomic_write_text(path, json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n")


def is_within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError:
        return False
    return True
