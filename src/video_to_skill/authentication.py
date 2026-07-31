"""One-shot browser-cookie snapshots for a complete extraction run."""

from __future__ import annotations

import os
import shutil
import tempfile
from collections.abc import Iterator, Sequence
from contextlib import contextmanager, suppress
from pathlib import Path
from urllib.parse import urlparse

from video_to_skill.config import Settings
from video_to_skill.errors import ProcessingError
from video_to_skill.utils import redact, require_program, run_command, stable_hash

_AUTHENTICATED_HOSTS = ("youtube.com", "youtu.be", "bilibili.com", "b23.tv")
_MAX_COOKIE_JAR_BYTES = 64 * 1024 * 1024


def _cookie_probe_url(inputs: Sequence[str]) -> str | None:
    for locator in inputs:
        try:
            parsed = urlparse(locator)
        except ValueError:
            continue
        if parsed.scheme not in {"http", "https"}:
            continue
        host = (parsed.hostname or "").casefold()
        if any(host == allowed or host.endswith(f".{allowed}") for allowed in _AUTHENTICATED_HOSTS):
            return locator
    return None


def _secure_private_path(path: Path, mode: int) -> None:
    if os.name != "nt":
        path.chmod(mode)


def _export_browser_cookies(
    settings: Settings,
    *,
    probe_url: str,
    cookie_path: Path,
) -> None:
    browser = settings.cookies_from_browser
    if browser is None:
        raise ValueError("browser cookie export requires cookies_from_browser")
    require_program(settings.yt_dlp)
    result = run_command(
        [
            settings.yt_dlp,
            "--ignore-config",
            "--no-warnings",
            "--cookies-from-browser",
            browser,
            "--cookies",
            str(cookie_path),
            "--skip-download",
            "--ignore-errors",
            "--flat-playlist",
            "--playlist-end",
            "1",
            probe_url,
        ],
        timeout=settings.command_timeout_seconds,
        check=False,
    )
    if not cookie_path.is_file() or cookie_path.is_symlink() or cookie_path.stat().st_size == 0:
        detail = redact((result.stderr or result.stdout).strip())[-1200:]
        suffix = f" Details: {detail}" if detail else ""
        raise ProcessingError(
            "Could not create the temporary browser-cookie snapshot. "
            "Authorize the browser keychain once, provide a cookies.txt file, "
            f"or continue with public sources.{suffix}"
        )
    if cookie_path.stat().st_size > _MAX_COOKIE_JAR_BYTES:
        raise ProcessingError("Browser-cookie snapshot exceeds the 64 MiB safety limit")
    _secure_private_path(cookie_path, 0o600)


def _snapshot_cookie_file(source: Path, destination: Path) -> None:
    resolved = source.expanduser().resolve()
    if not resolved.is_file():
        raise ProcessingError(f"Cookie file does not exist: {resolved}")
    size = resolved.stat().st_size
    if size == 0:
        raise ProcessingError("Cookie file is empty")
    if size > _MAX_COOKIE_JAR_BYTES:
        raise ProcessingError("Cookie file exceeds the 64 MiB safety limit")
    shutil.copyfile(resolved, destination)
    _secure_private_path(destination, 0o600)


@contextmanager
def browser_cookie_session(
    settings: Settings,
    inputs: Sequence[str],
) -> Iterator[Settings]:
    """Snapshot one cookie source, then reuse private jars for the full run."""

    if settings.cookies_from_browser is None and settings.cookies_file is None:
        yield settings
        return
    probe_url = _cookie_probe_url(inputs)
    if probe_url is None:
        yield settings
        return

    with tempfile.TemporaryDirectory(prefix="video-to-skill-auth-") as temporary:
        auth_directory = Path(temporary)
        _secure_private_path(auth_directory, 0o700)
        cookie_path = auth_directory / "cookies.txt"
        if settings.cookies_from_browser is not None:
            _export_browser_cookies(
                settings,
                probe_url=probe_url,
                cookie_path=cookie_path,
            )
        else:
            assert settings.cookies_file is not None
            _snapshot_cookie_file(settings.cookies_file, cookie_path)
        prepared = settings.model_copy(
            update={
                "cookies_from_browser": None,
                "cookies_file": cookie_path,
            }
        )
        prepared._authentication_cache_key = settings.authentication_cache_key
        prepared._cookie_session_root = auth_directory
        try:
            yield prepared
        finally:
            for retained_cookie in auth_directory.rglob("*.txt"):
                with suppress(OSError):
                    retained_cookie.write_bytes(b"")


def cookie_settings_for_worker(settings: Settings, worker_key: str) -> Settings:
    """Give one concurrent worker an isolated copy of the session cookie jar."""

    session_root = settings._cookie_session_root
    cookie_path = settings.cookies_file
    if session_root is None or cookie_path is None:
        return settings
    worker_directory = session_root / "workers"
    worker_directory.mkdir(mode=0o700, exist_ok=True)
    _secure_private_path(worker_directory, 0o700)
    worker_cookie = worker_directory / f"{stable_hash(worker_key, length=20)}.txt"
    shutil.copyfile(cookie_path, worker_cookie)
    _secure_private_path(worker_cookie, 0o600)
    prepared = settings.model_copy(update={"cookies_file": worker_cookie})
    prepared._authentication_cache_key = settings.authentication_cache_key
    prepared._cookie_session_root = session_root
    return prepared
