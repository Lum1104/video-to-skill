import os
import subprocess
from pathlib import Path

import pytest

from video_to_skill import authentication
from video_to_skill.authentication import (
    browser_cookie_session,
    cookie_settings_for_worker,
)
from video_to_skill.config import Settings


def test_browser_cookie_session_exports_once_and_cleans_up(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[list[str]] = []

    monkeypatch.setattr(authentication, "require_program", lambda _program: "yt-dlp")

    def fake_run(
        args: list[str],
        **_kwargs: object,
    ) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        cookie_path = Path(args[args.index("--cookies") + 1])
        cookie_path.write_text(
            "# Netscape HTTP Cookie File\n.example.com\tTRUE\t/\tFALSE\t0\tname\tvalue\n",
            encoding="utf-8",
        )
        return subprocess.CompletedProcess(args=args, returncode=0, stdout="", stderr="")

    monkeypatch.setattr(authentication, "run_command", fake_run)
    original = Settings(
        cache_root=tmp_path,
        yt_dlp="yt-dlp",
        cookies_from_browser="chrome",
    )

    with browser_cookie_session(original, ["https://youtu.be/example"]) as prepared:
        assert prepared.cookies_from_browser is None
        assert prepared.cookies_file is not None
        cookie_path = prepared.cookies_file
        assert cookie_path.is_file()
        assert prepared.configuration_hash == original.configuration_hash
        first_worker = cookie_settings_for_worker(prepared, "source-one")
        second_worker = cookie_settings_for_worker(prepared, "source-two")
        assert first_worker.cookies_file is not None
        assert second_worker.cookies_file is not None
        assert first_worker.cookies_file != second_worker.cookies_file
        assert first_worker.cookies_file.read_bytes() == second_worker.cookies_file.read_bytes()
        if os.name != "nt":
            assert cookie_path.stat().st_mode & 0o777 == 0o600
            assert cookie_path.parent.stat().st_mode & 0o777 == 0o700

    assert len(calls) == 1
    assert not cookie_path.exists()
    assert not first_worker.cookies_file.exists()
    assert not second_worker.cookies_file.exists()


def test_browser_cookie_session_skips_local_only_inputs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        authentication,
        "run_command",
        lambda *_args, **_kwargs: pytest.fail("cookie export should not run"),
    )
    settings = Settings(cookies_from_browser="chrome")

    with browser_cookie_session(settings, ["/private/video.mp4"]) as prepared:
        assert prepared is settings


def test_cookie_file_is_snapshotted_without_modifying_the_user_file(
    tmp_path: Path,
) -> None:
    user_cookie = tmp_path / "user-cookies.txt"
    content = "# Netscape HTTP Cookie File\n.example.com\tTRUE\t/\tFALSE\t0\tname\tvalue\n"
    user_cookie.write_text(content, encoding="utf-8")
    settings = Settings(cookies_file=user_cookie)

    with browser_cookie_session(settings, ["https://youtu.be/example"]) as prepared:
        assert prepared.cookies_file is not None
        assert prepared.cookies_file != user_cookie
        prepared.cookies_file.write_text("worker mutation", encoding="utf-8")

    assert user_cookie.read_text(encoding="utf-8") == content


def test_browser_cookie_session_fails_closed_without_a_cookie_jar(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(authentication, "require_program", lambda _program: "yt-dlp")
    monkeypatch.setattr(
        authentication,
        "run_command",
        lambda args, **_kwargs: subprocess.CompletedProcess(
            args=args,
            returncode=1,
            stdout="",
            stderr="keychain access denied",
        ),
    )

    with (
        pytest.raises(Exception, match="Could not create the temporary"),
        browser_cookie_session(
            Settings(cookies_from_browser="chrome"),
            ["https://www.youtube.com/watch?v=example"],
        ),
    ):
        pass
