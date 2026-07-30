"""Opt-in, parse-only checks for code blocks in generated skills."""

from __future__ import annotations

import ast
import json
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

_FENCE_RE = re.compile(
    r"(?ms)^```(?P<language>[A-Za-z0-9_+#.-]*)[^\n]*\n"
    r"(?P<code>.*?)^```\s*$"
)


@dataclass(frozen=True)
class CodeIssue:
    path: Path
    block: int
    language: str
    message: str
    skipped: bool = False


def _subprocess_syntax(program: str, suffix: str, code: str) -> str | None:
    resolved = shutil.which(program)
    if not resolved:
        return f"{program} is unavailable"
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", suffix=suffix, delete=False
    ) as handle:
        handle.write(code)
        temporary = Path(handle.name)
    try:
        command = (
            [resolved, "-n", str(temporary)]
            if program in {"bash", "sh"}
            else [resolved, "--check", str(temporary)]
        )
        result = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
            env={"PATH": str(Path(resolved).parent)},
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return str(exc)
    finally:
        temporary.unlink(missing_ok=True)
    if result.returncode:
        return (result.stderr or result.stdout).strip()[-1000:]
    return None


def check_markdown_code(path: Path) -> list[CodeIssue]:
    text = path.read_text(encoding="utf-8", errors="replace")
    issues: list[CodeIssue] = []
    for block, match in enumerate(_FENCE_RE.finditer(text), start=1):
        language = match.group("language").casefold()
        code = match.group("code")
        try:
            if language in {"py", "python"}:
                ast.parse(code)
            elif language == "json":
                json.loads(code)
            elif language in {"sh", "shell", "bash"}:
                program = "bash" if language == "bash" else "sh"
                message = _subprocess_syntax(program, ".sh", code)
                if message:
                    issues.append(
                        CodeIssue(
                            path,
                            block,
                            language,
                            message,
                            "unavailable" in message,
                        )
                    )
            elif language in {"js", "javascript", "mjs", "cjs"}:
                message = _subprocess_syntax("node", ".js", code)
                if message:
                    issues.append(
                        CodeIssue(
                            path,
                            block,
                            language,
                            message,
                            "unavailable" in message,
                        )
                    )
        except (SyntaxError, json.JSONDecodeError) as exc:
            issues.append(CodeIssue(path, block, language, str(exc)))
    return issues
