#!/usr/bin/env python3
"""Run video-to-skill from a checkout or an installed generator bundle."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
import venv
from collections.abc import Callable, Iterable
from contextlib import suppress
from pathlib import Path
from typing import NamedTuple

ENGINE_PATH = Path(__file__).resolve()
REPOSITORY_ROOT = ENGINE_PATH.parents[1]
SOURCE_ROOT = REPOSITORY_ROOT / "src"
PYPROJECT_PATH = REPOSITORY_ROOT / "pyproject.toml"
POSIX_LAUNCHER_PATH = REPOSITORY_ROOT / "scripts" / "video-to-skill"
WINDOWS_LAUNCHER_PATH = REPOSITORY_ROOT / "scripts" / "video-to-skill.cmd"
BOOTSTRAP_GUARD = "VIDEO_TO_SKILL_BOOTSTRAPPED"
BOOTSTRAP_TIMEOUT_SECONDS = 300.0
INSTALL_TIMEOUT_SECONDS = 1_800.0
PROBE_TIMEOUT_SECONDS = 20.0
INTERNAL_CAPABILITY_COMMANDS = {"ensure-capability", "ensure-capabilities"}
CORE_IMPORTS = ("video_to_skill.cli",)
if SOURCE_ROOT.is_dir():
    sys.path.insert(0, str(SOURCE_ROOT))


class Capability(NamedTuple):
    extra: str
    imports: tuple[str, ...]
    label: str


CAPABILITIES = {
    "asr": Capability("asr", ("faster_whisper",), "local speech recognition"),
    "ocr": Capability("ocr", ("rapidocr", "onnxruntime"), "local OCR"),
    "diarization": Capability("diarization", ("whisperx",), "speaker diarization"),
}


class BootstrapFailure(RuntimeError):
    """A sanitized first-run or capability setup failure."""


def _user_data_root() -> Path:
    override = os.environ.get("VIDEO_TO_SKILL_RUNTIME_ROOT")
    if override:
        return Path(override).expanduser().resolve()
    if os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
    return base / "video-to-skill"


def _fingerprint_files() -> list[Path]:
    files = [
        PYPROJECT_PATH,
        ENGINE_PATH,
        POSIX_LAUNCHER_PATH,
        WINDOWS_LAUNCHER_PATH,
    ]
    if SOURCE_ROOT.is_dir():
        files.extend(
            path
            for path in SOURCE_ROOT.rglob("*")
            if path.is_file()
            and not path.is_symlink()
            and (path.suffix == ".py" or path.name == "py.typed")
        )
    return sorted(set(files), key=lambda path: path.as_posix())


def _runtime_fingerprint() -> str:
    digest = hashlib.sha256()
    digest.update(b"video-to-skill-runtime-v2\0")
    digest.update(str(REPOSITORY_ROOT.resolve()).encode())
    digest.update(b"\0")
    digest.update(f"{sys.version_info.major}.{sys.version_info.minor}".encode())
    for path in _fingerprint_files():
        relative = path.relative_to(REPOSITORY_ROOT).as_posix()
        digest.update(b"\0")
        digest.update(relative.encode())
        digest.update(b"\0")
        digest.update(path.read_bytes())
    return digest.hexdigest()[:20]


def _runtime_root() -> Path:
    return _user_data_root() / "runtimes" / _runtime_fingerprint()


def _runtime_python(runtime_root: Path | None = None) -> Path:
    root = runtime_root or _runtime_root()
    if os.name == "nt":
        return root / "Scripts" / "python.exe"
    return root / "bin" / "python"


def _read_runtime_record(runtime_root: Path) -> dict[str, object] | None:
    record_path = runtime_root / "runtime.json"
    if runtime_root.is_symlink() or not record_path.is_file() or record_path.is_symlink():
        return None
    try:
        payload = json.loads(record_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    expected = {
        "schema_version": 2,
        "engine": "video_to_skill",
        "source_root": str(REPOSITORY_ROOT.resolve()),
        "python_executable": str(_runtime_python(runtime_root)),
        "fingerprint": _runtime_fingerprint(),
    }
    if any(payload.get(key) != value for key, value in expected.items()):
        return None
    return payload


def _probe_imports(
    python_executable: Path,
    modules: Iterable[str],
    *,
    timeout: float = PROBE_TIMEOUT_SECONDS,
) -> bool:
    requested = tuple(modules)
    if not python_executable.is_file() or not requested:
        return False
    script = (
        "import importlib\n"
        f"modules = {json.dumps(requested)}\n"
        "for module in modules:\n"
        "    importlib.import_module(module)\n"
    )
    try:
        result = subprocess.run(
            [str(python_executable), "-I", "-c", script],
            check=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def _runtime_ready() -> bool:
    try:
        runtime_root = _runtime_root()
        return _read_runtime_record(runtime_root) is not None and _probe_imports(
            _runtime_python(runtime_root),
            CORE_IMPORTS,
        )
    except (OSError, UnicodeError, ValueError):
        return False


def _can_bootstrap_checkout() -> bool:
    return SOURCE_ROOT.is_dir() and PYPROJECT_PATH.is_file()


def _running_on_windows() -> bool:
    return os.name == "nt"


def _windows_process_is_alive(process_id: int) -> bool:
    """Probe a Windows process without sending a console or termination signal."""

    try:
        import ctypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        process_query_limited_information = 0x1000
        still_active = 259
        open_process = kernel32.OpenProcess
        open_process.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
        open_process.restype = ctypes.c_void_p
        get_exit_code = kernel32.GetExitCodeProcess
        get_exit_code.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong)]
        get_exit_code.restype = ctypes.c_int
        close_handle = kernel32.CloseHandle
        close_handle.argtypes = [ctypes.c_void_p]
        close_handle.restype = ctypes.c_int
        handle = open_process(
            process_query_limited_information,
            False,
            process_id,
        )
        if not handle:
            # Access denied means the process exists but cannot be queried. Other failures,
            # including an invalid PID, are safe to treat as no live owner.
            return ctypes.get_last_error() == 5
        try:
            exit_code = ctypes.c_ulong()
            if not get_exit_code(handle, ctypes.byref(exit_code)):
                return True
            return exit_code.value == still_active
        finally:
            close_handle(handle)
    except (AttributeError, OSError, TypeError, ValueError):
        # Never steal a Windows lock when the safe process API is unavailable.
        return True


def _process_is_alive(process_id: int) -> bool:
    if process_id <= 0:
        return False
    if _running_on_windows():
        return _windows_process_is_alive(process_id)
    try:
        os.kill(process_id, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _lock_is_stale(lock_path: Path) -> bool:
    try:
        age = time.time() - lock_path.stat().st_mtime
        process_id = int(lock_path.read_text(encoding="ascii").strip())
    except (OSError, UnicodeError, ValueError):
        return False
    return age > BOOTSTRAP_TIMEOUT_SECONDS and not _process_is_alive(process_id)


def _acquire_lock(lock_path: Path, ready: Callable[[], bool]) -> int | None:
    deadline = time.monotonic() + BOOTSTRAP_TIMEOUT_SECONDS
    no_follow = getattr(os, "O_NOFOLLOW", 0)
    while True:
        try:
            descriptor = os.open(
                lock_path,
                os.O_CREAT | os.O_EXCL | os.O_WRONLY | no_follow,
                0o600,
            )
            os.write(descriptor, str(os.getpid()).encode())
            return descriptor
        except FileExistsError:
            if ready():
                return None
            if _lock_is_stale(lock_path):
                with suppress(OSError):
                    lock_path.unlink()
                continue
            if time.monotonic() >= deadline:
                raise BootstrapFailure("setup lock timed out") from None
            time.sleep(0.25)
        except OSError as exc:
            raise BootstrapFailure("setup lock unavailable") from exc


def _acquire_bootstrap_lock(lock_path: Path) -> int | None:
    return _acquire_lock(lock_path, _runtime_ready)


def _release_lock(lock_path: Path, descriptor: int | None) -> None:
    if descriptor is None:
        return
    os.close(descriptor)
    with suppress(OSError):
        lock_path.unlink()


def _write_private_json(path: Path, payload: dict[str, object]) -> None:
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    with suppress(OSError):
        temporary.chmod(0o600)
    os.replace(temporary, path)


def _write_runtime_record(runtime_root: Path) -> None:
    record: dict[str, object] = {
        "schema_version": 2,
        "engine": "video_to_skill",
        "source_root": str(REPOSITORY_ROOT.resolve()),
        "python_executable": str(_runtime_python(runtime_root)),
        "fingerprint": _runtime_fingerprint(),
        "capabilities": {},
    }
    _write_private_json(runtime_root / "runtime.json", record)


def _record_capability(runtime_root: Path, name: str, capability: Capability) -> None:
    record = _read_runtime_record(runtime_root)
    if record is None:
        raise BootstrapFailure("runtime record unavailable")
    raw_capabilities = record.get("capabilities")
    capabilities = dict(raw_capabilities) if isinstance(raw_capabilities, dict) else {}
    capabilities[name] = {
        "extra": capability.extra,
        "imports": list(capability.imports),
        "verified_at": int(time.time()),
    }
    record["capabilities"] = capabilities
    _write_private_json(runtime_root / "runtime.json", record)


def _pip_install(
    runtime_python: Path,
    requirement: str,
    *,
    editable: bool,
) -> None:
    command = [
        str(runtime_python),
        "-m",
        "pip",
        "install",
        "--disable-pip-version-check",
        "--no-input",
    ]
    if editable:
        command.append("--editable")
    command.append(requirement)
    try:
        subprocess.run(
            command,
            check=True,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=INSTALL_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        raise BootstrapFailure("dependency installation failed") from exc


def _supported_python() -> bool:
    return (3, 11) <= sys.version_info[:2] < (3, 14)


def _prepare_runtime() -> Path:
    try:
        return _prepare_runtime_once()
    except BootstrapFailure:
        raise
    except (OSError, UnicodeError, ValueError) as exc:
        raise BootstrapFailure("runtime preparation failed") from exc


def _prepare_runtime_once() -> Path:
    if not _supported_python():
        raise BootstrapFailure("unsupported Python")
    if not _can_bootstrap_checkout():
        raise BootstrapFailure("source checkout unavailable")
    if _runtime_ready():
        return _runtime_python()

    runtime_root = _runtime_root()
    runtime_python = _runtime_python(runtime_root)
    runtimes_root = runtime_root.parent
    runtimes_root.mkdir(parents=True, exist_ok=True)
    with suppress(OSError):
        runtimes_root.chmod(0o700)
    lock_path = runtimes_root / f".{runtime_root.name}.lock"
    lock_descriptor: int | None = None
    touched_runtime = False
    try:
        lock_descriptor = _acquire_bootstrap_lock(lock_path)
        if lock_descriptor is None:
            return runtime_python
        if _runtime_ready():
            return runtime_python

        print(
            "Preparing the private video-to-skill runtime (first use or one-time repair)...",
            file=sys.stderr,
        )
        if runtime_root.is_symlink():
            raise BootstrapFailure("unsafe runtime path")
        if runtime_root.exists():
            shutil.rmtree(runtime_root)
        touched_runtime = True
        venv.EnvBuilder(with_pip=True, symlinks=os.name != "nt").create(runtime_root)
        _pip_install(runtime_python, str(REPOSITORY_ROOT), editable=True)
        if not _probe_imports(runtime_python, CORE_IMPORTS):
            raise BootstrapFailure("core import probe failed")
        _write_runtime_record(runtime_root)
        if not _runtime_ready():
            raise BootstrapFailure("runtime readiness probe failed")
        return runtime_python
    except (OSError, BootstrapFailure) as exc:
        if touched_runtime:
            shutil.rmtree(runtime_root, ignore_errors=True)
        if isinstance(exc, BootstrapFailure):
            raise
        raise BootstrapFailure("runtime preparation failed") from exc
    finally:
        _release_lock(lock_path, lock_descriptor)


def _bootstrap_checkout() -> None:
    try:
        runtime_python = _prepare_runtime()
    except BootstrapFailure as exc:
        if not _supported_python():
            print(
                "video-to-skill needs Python 3.11, 3.12, or 3.13 for its first-run setup.",
                file=sys.stderr,
            )
        else:
            print(
                "Could not prepare the private video-to-skill runtime. "
                "Check network access and the Python installation, then retry.",
                file=sys.stderr,
            )
        raise SystemExit(127) from exc

    os.environ[BOOTSTRAP_GUARD] = "1"
    runtime_scripts = str(runtime_python.parent)
    current_path = os.environ.get("PATH", "")
    path_entries = current_path.split(os.pathsep) if current_path else []
    if runtime_scripts not in path_entries:
        os.environ["PATH"] = os.pathsep.join([runtime_scripts, *path_entries])
    os.execv(
        str(runtime_python),
        [str(runtime_python), str(ENGINE_PATH), *sys.argv[1:]],
    )


def _capability_ready(runtime_python: Path, capability: Capability) -> bool:
    return _probe_imports(runtime_python, capability.imports)


def _ensure_capability(name: str) -> None:
    capability = CAPABILITIES.get(name)
    if capability is None:
        choices = ", ".join(sorted(CAPABILITIES))
        print(f"Unknown internal capability. Expected one of: {choices}.", file=sys.stderr)
        raise SystemExit(2)
    try:
        runtime_python = _prepare_runtime()
        if _capability_ready(runtime_python, capability):
            print(f"Capability ready: {name}")
            return

        runtime_root = _runtime_root()
        lock_path = runtime_root.parent / f".{runtime_root.name}.capabilities.lock"
        lock_descriptor = _acquire_lock(
            lock_path,
            lambda: _capability_ready(runtime_python, capability),
        )
        try:
            if lock_descriptor is not None and not _capability_ready(runtime_python, capability):
                print(f"Preparing {capability.label} (first use only)...", file=sys.stderr)
                local_extra = f"{REPOSITORY_ROOT}[{capability.extra}]"
                _pip_install(runtime_python, local_extra, editable=True)
                if not _capability_ready(runtime_python, capability):
                    raise BootstrapFailure("capability import probe failed")
                _record_capability(runtime_root, name, capability)
        finally:
            _release_lock(lock_path, lock_descriptor)
    except (BootstrapFailure, OSError, UnicodeError, ValueError) as exc:
        print(
            f"Could not prepare the optional {capability.label} capability. "
            "Check network access and available storage, then retry.",
            file=sys.stderr,
        )
        raise SystemExit(127) from exc
    print(f"Capability ready: {name}")


def _handle_internal_command(arguments: list[str]) -> bool:
    if not arguments or arguments[0] not in INTERNAL_CAPABILITY_COMMANDS:
        return False
    if len(arguments) != 2:
        print("Internal usage: ensure-capability asr|ocr|diarization", file=sys.stderr)
        raise SystemExit(2)
    if not _can_bootstrap_checkout():
        print(
            "Optional capabilities can only be prepared from a source-form generator Skill.",
            file=sys.stderr,
        )
        raise SystemExit(127)
    _ensure_capability(arguments[1].casefold())
    return True


def _running_in_private_runtime() -> bool:
    if not _can_bootstrap_checkout() or not _runtime_ready():
        return False
    try:
        return Path(sys.executable).resolve() == _runtime_python().resolve()
    except OSError:
        return False


def _load_engine() -> Callable[[], None]:
    try:
        from video_to_skill.cli import run
    except (ImportError, ModuleNotFoundError) as exc:
        if _can_bootstrap_checkout() and os.environ.get(BOOTSTRAP_GUARD) != "1":
            _bootstrap_checkout()
        print(
            "The video-to-skill engine environment is missing or damaged. "
            "Reinstall the generator Skill or repair its private runtime.",
            file=sys.stderr,
        )
        raise SystemExit(127) from exc
    return run


def main() -> None:
    if _handle_internal_command(sys.argv[1:]):
        return
    if _can_bootstrap_checkout() and not _running_in_private_runtime():
        if os.environ.get(BOOTSTRAP_GUARD) == "1":
            print(
                "The private video-to-skill runtime did not pass its import check.",
                file=sys.stderr,
            )
            raise SystemExit(127)
        _bootstrap_checkout()
    _load_engine()()


if __name__ == "__main__":
    main()
