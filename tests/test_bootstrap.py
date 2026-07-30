from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest


def _load_bootstrap() -> ModuleType:
    path = Path(__file__).resolve().parents[1] / "scripts" / "video_to_skill.py"
    spec = importlib.util.spec_from_file_location("video_to_skill_bootstrap_test", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


bootstrap = _load_bootstrap()


def _fake_runtime_python(runtime_root: Path) -> Path:
    relative = Path("Scripts/python.exe") if bootstrap.os.name == "nt" else Path("bin/python")
    python = runtime_root / relative
    python.parent.mkdir(parents=True, exist_ok=True)
    python.write_bytes(b"python")
    return python


def test_runtime_fingerprint_changes_with_source_and_engine(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "source-form"
    source = root / "src" / "video_to_skill"
    scripts = root / "scripts"
    source.mkdir(parents=True)
    scripts.mkdir()
    pyproject = root / "pyproject.toml"
    engine = scripts / "video_to_skill.py"
    posix = scripts / "video-to-skill"
    windows = scripts / "video-to-skill.cmd"
    module = source / "cli.py"
    pyproject.write_text("[project]\nname='video-to-skill'\n", encoding="utf-8")
    engine.write_text("ENGINE = 1\n", encoding="utf-8")
    posix.write_text("#!/bin/sh\n", encoding="utf-8")
    windows.write_text("@echo off\n", encoding="utf-8")
    module.write_text("VALUE = 1\n", encoding="utf-8")
    monkeypatch.setattr(bootstrap, "REPOSITORY_ROOT", root)
    monkeypatch.setattr(bootstrap, "SOURCE_ROOT", root / "src")
    monkeypatch.setattr(bootstrap, "PYPROJECT_PATH", pyproject)
    monkeypatch.setattr(bootstrap, "ENGINE_PATH", engine)
    monkeypatch.setattr(bootstrap, "POSIX_LAUNCHER_PATH", posix)
    monkeypatch.setattr(bootstrap, "WINDOWS_LAUNCHER_PATH", windows)

    original = bootstrap._runtime_fingerprint()
    module.write_text("VALUE = 2\n", encoding="utf-8")
    source_changed = bootstrap._runtime_fingerprint()
    engine.write_text("ENGINE = 2\n", encoding="utf-8")
    engine_changed = bootstrap._runtime_fingerprint()

    assert original != source_changed
    assert source_changed != engine_changed


def test_import_probe_is_isolated_bounded_and_silent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    python = tmp_path / "python"
    python.write_bytes(b"python")
    observed: dict[str, Any] = {}

    def fake_run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        observed["command"] = command
        observed.update(kwargs)
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(bootstrap.subprocess, "run", fake_run)

    assert bootstrap._probe_imports(python, ("video_to_skill.cli",), timeout=3.5)
    assert observed["command"][:3] == [str(python), "-I", "-c"]
    assert observed["timeout"] == 3.5
    assert observed["stdout"] is subprocess.DEVNULL
    assert observed["stderr"] is subprocess.DEVNULL


def test_pip_failure_is_sanitized_and_output_is_captured(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    python = tmp_path / "python"
    python.write_bytes(b"python")
    observed: dict[str, Any] = {}

    def fake_run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        observed.update(kwargs)
        raise subprocess.CalledProcessError(
            1,
            command,
            output=b"sensitive local path",
            stderr=b"credential-bearing index URL",
        )

    monkeypatch.setattr(bootstrap.subprocess, "run", fake_run)

    with pytest.raises(bootstrap.BootstrapFailure, match="dependency installation failed"):
        bootstrap._pip_install(python, "/local/source[asr]", editable=True)

    assert observed["capture_output"] is True
    assert observed["timeout"] == bootstrap.INSTALL_TIMEOUT_SECONDS


def test_windows_process_probe_never_uses_os_kill(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(bootstrap, "_running_on_windows", lambda: True)
    monkeypatch.setattr(bootstrap, "_windows_process_is_alive", lambda process_id: True)

    def unsafe_kill(process_id: int, signal: int) -> None:
        del process_id, signal
        raise AssertionError("os.kill must not be used for Windows liveness probes")

    monkeypatch.setattr(bootstrap.os, "kill", unsafe_kill)
    assert bootstrap._process_is_alive(42)


def test_runtime_ready_requires_successful_core_import(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = tmp_path / "runtime"
    _fake_runtime_python(runtime)
    monkeypatch.setattr(bootstrap, "_runtime_root", lambda: runtime)
    monkeypatch.setattr(bootstrap, "_runtime_fingerprint", lambda: "fingerprint")
    bootstrap._write_runtime_record(runtime)
    probes: list[tuple[str, ...]] = []

    def successful_probe(python: Path, modules: tuple[str, ...], *, timeout: float = 20) -> bool:
        del python, timeout
        probes.append(tuple(modules))
        return True

    monkeypatch.setattr(bootstrap, "_probe_imports", successful_probe)
    assert bootstrap._runtime_ready()
    assert probes == [bootstrap.CORE_IMPORTS]

    monkeypatch.setattr(bootstrap, "_probe_imports", lambda *args, **kwargs: False)
    assert not bootstrap._runtime_ready()


def test_damaged_runtime_is_rebuilt_once_then_reused(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = tmp_path / "runtimes" / "fingerprint"
    runtime.mkdir(parents=True)
    damaged = runtime / "damaged"
    damaged.write_text("broken", encoding="utf-8")
    monkeypatch.setattr(bootstrap, "_runtime_root", lambda: runtime)
    monkeypatch.setattr(bootstrap, "_runtime_fingerprint", lambda: "fingerprint")
    monkeypatch.setattr(bootstrap, "_can_bootstrap_checkout", lambda: True)
    monkeypatch.setattr(bootstrap, "_supported_python", lambda: True)
    builds: list[Path] = []
    installs: list[tuple[Path, str, bool]] = []

    class FakeBuilder:
        def __init__(self, **kwargs: Any) -> None:
            del kwargs

        def create(self, destination: Path) -> None:
            builds.append(destination)
            _fake_runtime_python(destination)

    def fake_install(python: Path, requirement: str, *, editable: bool) -> None:
        installs.append((python, requirement, editable))

    monkeypatch.setattr(bootstrap.venv, "EnvBuilder", FakeBuilder)
    monkeypatch.setattr(bootstrap, "_pip_install", fake_install)
    monkeypatch.setattr(bootstrap, "_probe_imports", lambda *args, **kwargs: True)

    prepared = bootstrap._prepare_runtime()
    reused = bootstrap._prepare_runtime()

    assert prepared == reused == bootstrap._runtime_python(runtime)
    assert builds == [runtime]
    assert installs == [(prepared, str(bootstrap.REPOSITORY_ROOT), True)]
    assert not damaged.exists()
    assert (runtime / "runtime.json").is_file()


def test_failed_runtime_repair_does_not_loop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = tmp_path / "runtimes" / "fingerprint"
    runtime.mkdir(parents=True)
    (runtime / "damaged").write_text("broken", encoding="utf-8")
    monkeypatch.setattr(bootstrap, "_runtime_root", lambda: runtime)
    monkeypatch.setattr(bootstrap, "_runtime_fingerprint", lambda: "fingerprint")
    monkeypatch.setattr(bootstrap, "_can_bootstrap_checkout", lambda: True)
    monkeypatch.setattr(bootstrap, "_supported_python", lambda: True)
    builds = 0
    installs = 0

    class FakeBuilder:
        def __init__(self, **kwargs: Any) -> None:
            del kwargs

        def create(self, destination: Path) -> None:
            nonlocal builds
            builds += 1
            _fake_runtime_python(destination)

    def failing_install(python: Path, requirement: str, *, editable: bool) -> None:
        nonlocal installs
        del python, requirement, editable
        installs += 1
        raise bootstrap.BootstrapFailure("simulated")

    monkeypatch.setattr(bootstrap.venv, "EnvBuilder", FakeBuilder)
    monkeypatch.setattr(bootstrap, "_pip_install", failing_install)

    with pytest.raises(bootstrap.BootstrapFailure):
        bootstrap._prepare_runtime()

    assert builds == 1
    assert installs == 1
    assert not runtime.exists()


def test_bootstrap_checkout_prepends_runtime_scripts_to_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime_python = _fake_runtime_python(tmp_path / "runtime")
    existing_path = os.pathsep.join(("/usr/local/bin", "/usr/bin"))
    observed: dict[str, Any] = {}
    monkeypatch.setattr(bootstrap, "_prepare_runtime", lambda: runtime_python)
    monkeypatch.setenv("PATH", existing_path)

    def fake_execv(executable: str, arguments: list[str]) -> None:
        observed["executable"] = executable
        observed["arguments"] = arguments
        observed["path"] = bootstrap.os.environ["PATH"]

    monkeypatch.setattr(bootstrap.os, "execv", fake_execv)

    bootstrap._bootstrap_checkout()

    assert observed["executable"] == str(runtime_python)
    assert observed["arguments"][:2] == [str(runtime_python), str(bootstrap.ENGINE_PATH)]
    assert observed["path"] == os.pathsep.join(
        (str(runtime_python.parent), existing_path),
    )
    assert bootstrap.os.environ[bootstrap.BOOTSTRAP_GUARD] == "1"


def test_capability_install_uses_local_editable_extra_without_heavy_download(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    runtime = tmp_path / "runtimes" / "fingerprint"
    runtime_python = _fake_runtime_python(runtime)
    monkeypatch.setattr(bootstrap, "_runtime_root", lambda: runtime)
    monkeypatch.setattr(bootstrap, "_prepare_runtime", lambda: runtime_python)
    readiness = iter((False, False, True))
    monkeypatch.setattr(
        bootstrap,
        "_capability_ready",
        lambda python, capability: next(readiness),
    )
    installs: list[tuple[Path, str, bool]] = []
    records: list[str] = []

    def fake_install(python: Path, requirement: str, *, editable: bool) -> None:
        installs.append((python, requirement, editable))

    monkeypatch.setattr(bootstrap, "_pip_install", fake_install)
    monkeypatch.setattr(
        bootstrap,
        "_record_capability",
        lambda root, name, capability: records.append(name),
    )

    bootstrap._ensure_capability("asr")

    assert installs == [
        (runtime_python, f"{bootstrap.REPOSITORY_ROOT}[asr]", True),
    ]
    assert records == ["asr"]
    assert "pip" not in capsys.readouterr().out.casefold()


def test_healthy_capability_is_idempotent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime_python = _fake_runtime_python(tmp_path / "runtime")
    monkeypatch.setattr(bootstrap, "_prepare_runtime", lambda: runtime_python)
    monkeypatch.setattr(bootstrap, "_capability_ready", lambda *args: True)

    def should_not_install(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("healthy capability must not run an installer")

    monkeypatch.setattr(bootstrap, "_pip_install", should_not_install)
    bootstrap._ensure_capability("ocr")


def test_source_form_internal_capability_command_is_available(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prepared: list[str] = []
    monkeypatch.setattr(bootstrap, "_can_bootstrap_checkout", lambda: True)
    monkeypatch.setattr(bootstrap, "_ensure_capability", prepared.append)

    assert bootstrap._handle_internal_command(["ensure-capability", "ocr"])
    assert prepared == ["ocr"]
