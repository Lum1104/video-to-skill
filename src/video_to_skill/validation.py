"""Generated Agent Skill validation and shareability scanning."""

from __future__ import annotations

import hashlib
import json
import re
import shutil
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse, urlsplit

from pydantic import BaseModel, ConfigDict, Field

from video_to_skill.codecheck import check_markdown_code
from video_to_skill.errors import ValidationError
from video_to_skill.generation import COURSE_SKILL_MARKER
from video_to_skill.sources import MEDIA_EXTENSIONS
from video_to_skill.url_security import UrlParameterLimitError, has_sensitive_url_parameters
from video_to_skill.utils import run_command


class ValidationIssue(BaseModel):
    model_config = ConfigDict(extra="forbid")
    level: Literal["error", "warning"]
    code: str
    message: str
    path: str | None = None


class ValidationReport(BaseModel):
    model_config = ConfigDict(extra="forbid")
    skill_path: Path
    valid: bool
    issues: list[ValidationIssue] = Field(default_factory=list)
    files_checked: int = 0

    def add(
        self,
        level: Literal["error", "warning"],
        code: str,
        message: str,
        path: Path | None = None,
    ) -> None:
        self.issues.append(
            ValidationIssue(
                level=level,
                code=code,
                message=message,
                path=str(path) if path else None,
            )
        )
        if level == "error":
            self.valid = False


MAX_SECURITY_SCAN_FILES = 4_096
MAX_SECURITY_SCAN_FILE_BYTES = 2 * 1_024 * 1_024
MAX_SECURITY_SCAN_TOTAL_BYTES = 16 * 1_024 * 1_024

_LINK_RE = re.compile(r"!?\[[^\]]*\]\(([^)]+)\)")
_WEB_URL_RE = re.compile(r"""(?i)\bhttps?://[^\s<>{}\[\]"'`]+""")
_FILE_URI_RE = re.compile(r"""(?i)(?<![\w-])file:[^\s<>{}\[\]"'`)]+""")
_CREDENTIAL_ASSIGNMENT_RE = re.compile(
    r"""(?ix)
    (?P<key_quote>["']?)
    (?P<name>
        api[_-]?key|access[_-]?key|secret[_-]?key|client[_-]?secret|
        access[_-]?token|refresh[_-]?token|id[_-]?token|auth[_-]?token|
        token|secret|password|passwd|credential|private[_-]?key|session[_-]?id
    )
    (?P=key_quote)
    \s*[:=]\s*
    ["']?
    (?P<value>[A-Za-z0-9][A-Za-z0-9._~+/=@:-]{7,})
    """
)
_AUTHORIZATION_RE = re.compile(
    r"""(?ix)
    \b(?:authorization|proxy-authorization)\s*[:=]\s*["']?
    (?:bearer|basic)\s+[A-Za-z0-9._~+/=-]{12,}
    """
)
_COOKIE_RE = re.compile(r"(?i)\bcookie\s*:\s*[^\r\n]{12,}")
_PREFIXED_SECRET_PATTERNS = [
    re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b"),
    re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"),
    re.compile(r"\bgh(?:p|o|u|s|r)_[A-Za-z0-9]{20,}\b"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b"),
    re.compile(r"\bxox(?:a|b|p|r|s)-[A-Za-z0-9-]{16,}\b"),
    re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{8,}\b"),
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
]
_POSIX_PRIVATE_PATH_RE = re.compile(
    r"""(?x)
    (?<![A-Za-z0-9:])
    /
    (?:
        Users/[^/\s"'`<>]+|
        home/[^/\s"'`<>]+|
        root|
        private/(?:var|tmp)|
        var/(?:folders|tmp)|
        tmp|
        workspaces?|
        mnt/[A-Za-z]/Users/[^/\s"'`<>]+
    )
    (?:/[^\s"'`()<>[\]{}]+)+
    """
)
_POSIX_WORKSPACE_ARTIFACT_RE = re.compile(
    r"""(?x)
    (?<![A-Za-z0-9:])
    /
    (?:[^\s"'`()<>[\]{}]+/)*
    (?:evidence\.sqlite3|investigation-frames|\.codex|\.claude)
    (?:/[^\s"'`()<>[\]{}]+)*
    """
)
_WINDOWS_PRIVATE_PATH_RE = re.compile(
    r"""(?ix)
    (?<![A-Za-z0-9])
    [A-Z]:[\\/]+
    (?:
        Users[\\/]+[^\\/\s"'`<>|]+|
        Documents[ ]and[ ]Settings[\\/]+[^\\/\s"'`<>|]+|
        Temp|
        workspaces?|
        (?:[^\\/\s"'`<>|]+[\\/]+)*
        (?:evidence\.sqlite3|investigation-frames|[.]codex|[.]claude)
    )
    (?:[\\/]+[^\\/\s"'`<>|]+)*
    """
)
_UNC_PATH_RE = re.compile(
    r"""(?x)
    (?<![\\A-Za-z0-9])
    \\{2,}
    [^\\\s"'`<>|]+[\\]+
    [^\\\s"'`<>|]+
    (?:[\\]+[^\\\s"'`<>|]+)+
    """
)
_PLACEHOLDER_MARKERS = (
    "changeme",
    "demo",
    "dummy",
    "example",
    "notareal",
    "placeholder",
    "redacted",
    "replace",
    "sample",
    "your",
)
_KNOWN_TEXT_SUFFIXES = {
    ".cfg",
    ".conf",
    ".css",
    ".csv",
    ".html",
    ".htm",
    ".ini",
    ".java",
    ".js",
    ".json",
    ".jsx",
    ".md",
    ".properties",
    ".ps1",
    ".py",
    ".rb",
    ".rst",
    ".sh",
    ".sql",
    ".toml",
    ".ts",
    ".tsv",
    ".tsx",
    ".txt",
    ".xml",
    ".yaml",
    ".yml",
}
_RAW_TRANSCRIPT_RE = re.compile(r"(?i)(^|[-_.])(raw[-_.]?)?transcript(s)?($|[-_.])")
_NAME_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_EVIDENCE_MODALITIES = {"speech", "visual", "ocr", "metadata", "temporal"}


def _frontmatter(markdown: str) -> dict[str, str]:
    if not markdown.startswith("---\n"):
        return {}
    end = markdown.find("\n---", 4)
    if end < 0:
        return {}
    values: dict[str, str] = {}
    for line in markdown[4:end].splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        values[key.strip()] = value.strip().strip("\"'")
    return values


def _relative_links(markdown: str) -> list[str]:
    links = []
    for match in _LINK_RE.finditer(markdown):
        target = match.group(1).strip().split("#", 1)[0]
        if not target:
            continue
        parsed = urlparse(target)
        if parsed.scheme or target.startswith("#"):
            continue
        links.append(target.replace("%20", " "))
    return links


def _looks_like_placeholder(value: str) -> bool:
    normalized = re.sub(r"[^a-z0-9]", "", value.casefold())
    return any(
        normalized == marker or normalized.startswith(marker) for marker in _PLACEHOLDER_MARKERS
    )


def _credential_assignment_is_sensitive(match: re.Match[str]) -> bool:
    value = match.group("value")
    if _looks_like_placeholder(value):
        return False
    name = re.sub(r"[^a-z0-9]", "", match.group("name").casefold())
    return not (
        not match.group("key_quote")
        and name in {"credential", "secret", "token"}
        and value.isalpha()
    )


def _canonical_json_for_scan(path: Path, text: str) -> str | None:
    if path.suffix.casefold() != ".json":
        return None
    try:
        payload = json.loads(text)
        canonical = json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    except (json.JSONDecodeError, RecursionError, TypeError, ValueError):
        return None
    encoded = canonical.encode("utf-8")
    if len(encoded) > MAX_SECURITY_SCAN_FILE_BYTES:
        return encoded[:MAX_SECURITY_SCAN_FILE_BYTES].decode("utf-8", errors="ignore")
    return canonical


def _scan_text_security(path: Path, text: str, report: ValidationReport) -> None:
    """Scan one bounded textual artifact without logging matched secret material."""

    canonical_json = _canonical_json_for_scan(path, text)
    scan_text = text if canonical_json is None else f"{text}\n{canonical_json}"
    emitted: set[str] = set()

    def reject(code: str, message: str) -> None:
        if code not in emitted:
            report.add("error", code, message, path)
            emitted.add(code)

    if _FILE_URI_RE.search(scan_text):
        reject(
            "unsafe-file-link",
            "Shareable skills cannot contain local file: links.",
        )

    for match in _WEB_URL_RE.finditer(scan_text):
        raw_url = match.group(0).rstrip(".,;:!?)]}")
        try:
            parsed = urlsplit(raw_url)
            has_userinfo = parsed.username is not None or parsed.password is not None
        except ValueError:
            continue
        if has_userinfo:
            reject(
                "credentialed-url",
                "Shareable skills cannot contain credentials in URL userinfo.",
            )
        try:
            sensitive_parameters = has_sensitive_url_parameters(raw_url)
        except UrlParameterLimitError:
            reject(
                "sensitive-url-query",
                "A URL query or fragment exceeds the bounded security-validation limit.",
            )
        else:
            if not sensitive_parameters:
                continue
            reject(
                "sensitive-url-query",
                "Shareable skills cannot contain sensitive or expiring URL query or fragment fields.",
            )

    scrubbed = _FILE_URI_RE.sub(" ", _WEB_URL_RE.sub(" ", scan_text))
    if (
        _AUTHORIZATION_RE.search(scrubbed)
        or _COOKIE_RE.search(scrubbed)
        or any(pattern.search(scrubbed) for pattern in _PREFIXED_SECRET_PATTERNS)
        or any(
            _credential_assignment_is_sensitive(match)
            for match in _CREDENTIAL_ASSIGNMENT_RE.finditer(scrubbed)
        )
    ):
        reject(
            "possible-secret",
            "Possible credential, token, private key, or cookie material detected.",
        )

    if (
        _POSIX_PRIVATE_PATH_RE.search(scrubbed)
        or _POSIX_WORKSPACE_ARTIFACT_RE.search(scrubbed)
        or _WINDOWS_PRIVATE_PATH_RE.search(scrubbed)
        or _UNC_PATH_RE.search(scrubbed)
    ):
        reject(
            "private-path",
            "Shareable skills cannot contain absolute private or workspace paths.",
        )


def _decode_security_text(
    path: Path,
    data: bytes,
    *,
    known_text: bool,
    truncated: bool,
    report: ValidationReport,
) -> str | None:
    try:
        if data.startswith((b"\xff\xfe\x00\x00", b"\x00\x00\xfe\xff")):
            return data.decode("utf-32", errors="replace" if truncated else "strict")
        if data.startswith((b"\xff\xfe", b"\xfe\xff")):
            return data.decode("utf-16", errors="replace" if truncated else "strict")
        if b"\x00" in data:
            if known_text:
                report.add(
                    "error",
                    "unscannable-text",
                    "Declared textual artifact contains unsupported binary data.",
                    path,
                )
            return None
        return data.decode("utf-8", errors="replace" if truncated else "strict")
    except UnicodeError:
        if known_text:
            report.add(
                "error",
                "unscannable-text",
                "Declared textual artifact is not valid UTF-8/16/32 text.",
                path,
            )
        return None


def _read_security_text(path: Path, report: ValidationReport) -> tuple[str, int] | None:
    suffix = path.suffix.casefold()
    known_text = suffix in _KNOWN_TEXT_SUFFIXES or not suffix
    try:
        with path.open("rb") as handle:
            data = handle.read(MAX_SECURITY_SCAN_FILE_BYTES + 1)
    except OSError as exc:
        report.add(
            "error",
            "unreadable-artifact",
            f"Cannot read distributed artifact: {type(exc).__name__}.",
            path,
        )
        return None

    truncated = len(data) > MAX_SECURITY_SCAN_FILE_BYTES
    if truncated:
        data = data[:MAX_SECURITY_SCAN_FILE_BYTES]
    text = _decode_security_text(
        path,
        data,
        known_text=known_text,
        truncated=truncated,
        report=report,
    )
    if text is None:
        return None
    if truncated:
        report.add(
            "error",
            "security-scan-limit",
            (
                "Textual artifact exceeds the per-file security scan limit of "
                f"{MAX_SECURITY_SCAN_FILE_BYTES} bytes."
            ),
            path,
        )
    return text, len(data)


def _scan_distributed_text(
    root: Path,
    files: list[Path],
    report: ValidationReport,
) -> dict[Path, str]:
    """Scan textual package content under deterministic file and byte budgets."""

    selected = files[:MAX_SECURITY_SCAN_FILES]
    if len(files) > MAX_SECURITY_SCAN_FILES:
        report.add(
            "error",
            "security-scan-limit",
            (
                "Skill package exceeds the security scan file limit of "
                f"{MAX_SECURITY_SCAN_FILES} files."
            ),
            root,
        )

    texts: dict[Path, str] = {}
    scanned_bytes = 0
    total_limit_reported = False
    for path in selected:
        loaded = _read_security_text(path, report)
        if loaded is None:
            continue
        text, byte_count = loaded
        if scanned_bytes + byte_count > MAX_SECURITY_SCAN_TOTAL_BYTES:
            if not total_limit_reported:
                report.add(
                    "error",
                    "security-scan-limit",
                    (
                        "Textual package content exceeds the total security scan limit of "
                        f"{MAX_SECURITY_SCAN_TOTAL_BYTES} bytes."
                    ),
                    root,
                )
                total_limit_reported = True
            continue
        scanned_bytes += byte_count
        texts[path] = text
        _scan_text_security(path, text, report)
    return texts


def _validate_provenance(
    root: Path,
    report: ValidationReport,
    *,
    text: str | None,
) -> dict[str, object] | None:
    path = root / "provenance.json"
    if not path.exists():
        report.add(
            "error",
            "missing-provenance",
            "Shareable generated skills require provenance.json.",
            path,
        )
        return None
    if text is None:
        report.add(
            "error",
            "invalid-provenance",
            "provenance.json could not be scanned as bounded UTF text.",
            path,
        )
        return None
    try:
        payload = json.loads(text)
    except (json.JSONDecodeError, RecursionError) as exc:
        report.add("error", "invalid-provenance", str(exc), path)
        return None
    if not isinstance(payload, dict):
        report.add("error", "invalid-provenance", "Root must be an object.", path)
        return None
    if payload.get("schema_version") != 2:
        report.add(
            "error",
            "provenance-schema",
            "provenance.json must use schema_version 2.",
            path,
        )
    source_ids = {
        str(item.get("id"))
        for item in payload.get("sources", [])
        if isinstance(item, dict) and item.get("id")
    }
    if not source_ids:
        report.add("error", "provenance-sources", "No attributed sources found.", path)
    raw_units = payload.get("semantic_units")
    semantic_ids: set[str] = set()
    if not isinstance(raw_units, list) or not raw_units:
        report.add(
            "error",
            "missing-semantic-map",
            "V2 provenance requires semantic_units.",
            path,
        )
    else:
        for unit in raw_units:
            if not isinstance(unit, dict):
                report.add("error", "invalid-semantic-unit", "Unit must be an object.", path)
                continue
            unit_id = str(unit.get("id") or "")
            if not unit_id or unit_id in semantic_ids:
                report.add(
                    "error",
                    "duplicate-semantic-unit",
                    f"Semantic unit ID is missing or duplicated: {unit_id or '<missing>'}.",
                    path,
                )
            semantic_ids.add(unit_id)
            if unit.get("source_id") not in source_ids:
                report.add(
                    "error",
                    "unknown-semantic-source",
                    f"Semantic unit {unit_id or '<unknown>'} references an unknown source.",
                    path,
                )
            if unit.get("disposition") not in {
                "included",
                "merged",
                "context-only",
                "omitted",
            }:
                report.add(
                    "error",
                    "invalid-semantic-disposition",
                    f"Semantic unit {unit_id or '<unknown>'} has no valid disposition.",
                    path,
                )
            if unit.get("disposition") != "included" and not unit.get("disposition_reason"):
                report.add(
                    "error",
                    "missing-disposition-reason",
                    f"Semantic unit {unit_id or '<unknown>'} needs a disposition reason.",
                    path,
                )
    raw_claims = payload.get("claims", [])
    if not isinstance(raw_claims, list):
        report.add("error", "invalid-claims", "Claims must be an array.", path)
        return payload
    if not raw_claims:
        report.add(
            "error",
            "provenance-claims",
            "At least one grounded derivative claim is required.",
            path,
        )
    claim_ids: set[str] = set()
    for claim in raw_claims:
        if not isinstance(claim, dict):
            report.add("error", "invalid-claim", "Claim must be an object.", path)
            continue
        claim_id = str(claim.get("id") or "")
        if not claim_id or claim_id in claim_ids:
            report.add(
                "error",
                "duplicate-claim-id",
                f"Claim ID is missing or duplicated: {claim_id or '<missing>'}.",
                path,
            )
        claim_ids.add(claim_id)
        if not isinstance(claim.get("kind"), str) or not claim.get("kind"):
            report.add(
                "error",
                "missing-claim-kind",
                f"Claim {claim_id or '<unknown>'} has no kind.",
                path,
            )
        if not isinstance(claim.get("summary"), str) or not claim.get("summary"):
            report.add(
                "error",
                "missing-claim-summary",
                f"Claim {claim_id or '<unknown>'} has no derivative summary.",
                path,
            )
        if not isinstance(claim.get("inferred"), bool):
            report.add(
                "error",
                "missing-inference-flag",
                f"Claim {claim_id or '<unknown>'} must declare inferred=true/false.",
                path,
            )
        if claim.get("confidence") not in {"high", "medium", "low"}:
            report.add(
                "error",
                "invalid-claim-confidence",
                f"Claim {claim_id or '<unknown>'} must declare high/medium/low confidence.",
                path,
            )
        semantic_unit_ids = claim.get("semantic_unit_ids")
        if (
            not isinstance(semantic_unit_ids, list)
            or not semantic_unit_ids
            or any(unit_id not in semantic_ids for unit_id in semantic_unit_ids)
        ):
            report.add(
                "error",
                "invalid-claim-semantic-units",
                f"Claim {claim_id or '<unknown>'} must reference known semantic units.",
                path,
            )
        claim_file = claim.get("file")
        if not isinstance(claim_file, str):
            report.add(
                "error",
                "missing-claim-file",
                f"Claim {claim_id or '<unknown>'} has no rendered file.",
                path,
            )
        else:
            rendered_path = (root / claim_file).resolve()
            try:
                rendered_path.relative_to(root)
            except ValueError:
                report.add(
                    "error",
                    "escaping-claim-file",
                    f"Claim {claim_id} points outside the skill package.",
                    path,
                )
            else:
                if not rendered_path.is_file():
                    report.add(
                        "error",
                        "missing-claim-file",
                        f"Claim {claim_id} points to missing file {claim_file}.",
                        path,
                    )
        evidence = claim.get("evidence")
        if not isinstance(evidence, list) or not evidence:
            report.add(
                "error",
                "ungrounded-claim",
                f"Claim {claim.get('id', '<unknown>')} has no evidence.",
                path,
            )
            continue
        for item in evidence:
            if not isinstance(item, dict) or item.get("source_id") not in source_ids:
                report.add(
                    "error",
                    "unknown-evidence-source",
                    f"Claim {claim.get('id', '<unknown>')} references an unknown source.",
                    path,
                )
            start = item.get("start") if isinstance(item, dict) else None
            end = item.get("end") if isinstance(item, dict) else None
            if not isinstance(start, (int, float)):
                report.add(
                    "error",
                    "missing-evidence-time",
                    f"Claim {claim.get('id', '<unknown>')} lacks a numeric start time.",
                    path,
                )
            if isinstance(start, (int, float)) and isinstance(end, (int, float)) and end < start:
                report.add(
                    "error",
                    "invalid-evidence-time",
                    f"Claim {claim_id or '<unknown>'} ends before it starts.",
                    path,
                )
            if not isinstance(end, (int, float)):
                report.add(
                    "error",
                    "missing-evidence-end",
                    f"Claim {claim_id or '<unknown>'} lacks a numeric end time.",
                    path,
                )
            modalities: object = None
            if isinstance(item, dict):
                modalities = item.get("modalities")
                if modalities is None and isinstance(item.get("modality"), str):
                    modalities = str(item["modality"]).split("+")
            if (
                not isinstance(modalities, list)
                or not modalities
                or any(value not in _EVIDENCE_MODALITIES for value in modalities)
                or len(modalities) != len(set(modalities))
            ):
                report.add(
                    "error",
                    "invalid-evidence-modality",
                    (
                        f"Claim {claim_id or '<unknown>'} must declare unique `modalities` "
                        "from speech, visual, ocr, metadata, or temporal."
                    ),
                    path,
                )
            if (
                not isinstance(item, dict)
                or not isinstance(item.get("evidence_ids"), list)
                or not item.get("evidence_ids")
            ):
                report.add(
                    "error",
                    "missing-evidence-ids",
                    f"Claim {claim_id or '<unknown>'} lacks evidence IDs.",
                    path,
                )
    return payload


def _validate_course_skill_contract(
    root: Path,
    skill_text: str,
    report: ValidationReport,
) -> None:
    """Enforce the teaching contract only for versioned course skills."""

    if COURSE_SKILL_MARKER not in skill_text:
        return
    required_headings = {
        "missing-empty-invocation": (
            r"(?m)^## Empty invocation\s*$",
            "Course skill is missing the empty-invocation contract.",
        ),
        "missing-initial-context": (
            r"(?m)^## Initial context\s*$",
            "Course skill is missing its bounded initial-context contract.",
        ),
        "missing-interaction-behavior": (
            r"(?m)^## Interaction behavior\s*$",
            "Course skill is missing adaptive interaction behavior.",
        ),
        "missing-capability-profile": (
            r"(?m)^## Capability profile\s*$",
            "Course skill is missing its evidence-derived capability profile.",
        ),
        "missing-evidence-contract": (
            r"(?m)^## Evidence and uncertainty\s*$",
            "Course skill is missing its evidence and uncertainty contract.",
        ),
    }
    for code, (pattern, message) in required_headings.items():
        if not re.search(pattern, skill_text):
            report.add("error", code, message, root / "SKILL.md")

    behavioral_checks = {
        "missing-empty-no-side-effects": (
            r"(?is)load no supporting file.*inspect no project.*run no command.*create no file",
            "Empty invocation must have no supporting-file, project, command, or file side effects.",
        ),
        "missing-start-option": (
            r"(?ims)^## Empty invocation.*\bstart\b",
            "Empty invocation must offer a zero-friction `start` path.",
        ),
        "missing-three-question-bound": (
            r"(?is)\bat most three\b",
            "Initial context must ask at most three questions.",
        ),
        "missing-adaptive-teaching": (
            r"(?is)\bone useful cognitive move\b.*\b(adapt|answer)\b",
            "Learning behavior must teach one bounded move and adapt to the learner.",
        ),
        "missing-practice-feedback": (
            r"(?is)\b(attempt|retry)\b.*\b(rubric|feedback|misconception|hint)",
            "Practice mode must evaluate an attempt and provide bounded feedback.",
        ),
        "solution-leakage-risk": (
            r"(?is)\b(without|before|until)\b.{0,120}\b(solution|answer)\b",
            "Practice mode must withhold solutions until an attempt or explicit request.",
        ),
        "missing-application-context": (
            r"(?is)\buser(?:'s)? actual context\b",
            "Apply mode must inspect the user's real context before adapting the course method.",
        ),
        "missing-timestamp-grounding": (
            r"(?is)\b(timestamp|time-coded|timecode)\w*",
            "Consequential answers must be grounded with source timestamps.",
        ),
        "missing-honest-uncertainty": (
            r"(?is)\b(uncertain|uncertainty|low-confidence|inferred)\b",
            "The skill must state how to handle unsupported or uncertain material.",
        ),
        "missing-language-following": (
            r"(?is)respond in the user's language",
            "The skill must follow the user's interaction language by default.",
        ),
        "missing-knowledge-boundary": (
            r"(?is)outside or current knowledge",
            "The skill must distinguish source evidence from outside or current knowledge.",
        ),
    }
    for code, (pattern, message) in behavioral_checks.items():
        if not re.search(pattern, skill_text):
            report.add("error", code, message, root / "SKILL.md")

    for required in (
        "source-map.md",
        "sources.md",
        "provenance.json",
        "build-manifest.json",
    ):
        if not (root / required).is_file():
            report.add(
                "error",
                "missing-course-evidence-file",
                f"Course skill requires {required}.",
                root / required,
            )


def _validate_playbook_coverage(
    root: Path,
    payload: dict[str, object] | None,
    report: ValidationReport,
    *,
    texts: dict[Path, str],
) -> None:
    if payload is None:
        return
    claims = payload.get("claims")
    if not isinstance(claims, list):
        return
    claim_counts: dict[str, int] = {}
    for claim in claims:
        if (
            isinstance(claim, dict)
            and claim.get("kind") == "procedure-step"
            and isinstance(claim.get("file"), str)
        ):
            claim_file = str(claim["file"])
            claim_counts[claim_file] = claim_counts.get(claim_file, 0) + 1
    playbook_root = root / "playbooks"
    if not playbook_root.is_dir():
        return
    for path in playbook_root.rglob("*.md"):
        relative = path.relative_to(root).as_posix()
        text = texts.get(path)
        if text is None:
            continue
        procedure_match = re.search(r"(?ms)^## Procedure\s*$\n(?P<body>.*?)(?=^## |\Z)", text)
        if not procedure_match:
            report.add(
                "warning",
                "playbook-no-procedure",
                "Playbook has no `## Procedure` section.",
                path,
            )
            continue
        numbered_steps = re.findall(r"(?m)^\s*\d+[.)]\s+\S", procedure_match.group("body"))
        if len(numbered_steps) > claim_counts.get(relative, 0):
            report.add(
                "error",
                "ungrounded-procedure-steps",
                (
                    f"Playbook has {len(numbered_steps)} numbered steps but only "
                    f"{claim_counts.get(relative, 0)} procedure-step provenance claims."
                ),
                path,
            )


def _validate_build_manifest(
    root: Path,
    report: ValidationReport,
    *,
    text: str | None,
) -> None:
    path = root / "build-manifest.json"
    if not path.is_file():
        return
    if text is None:
        report.add(
            "error",
            "invalid-build-manifest",
            "build-manifest.json could not be scanned as bounded UTF text.",
            path,
        )
        return
    try:
        payload = json.loads(text)
    except (json.JSONDecodeError, RecursionError) as exc:
        report.add("error", "invalid-build-manifest", str(exc), path)
        return
    if not isinstance(payload, dict) or payload.get("schema_version") != 2:
        report.add(
            "error",
            "build-manifest-schema",
            "build-manifest.json must use schema_version 2.",
            path,
        )
        return
    build_id = payload.get("build_id")
    if not isinstance(build_id, str) or not re.fullmatch(r"v2s-[a-f0-9]{20}", build_id):
        report.add(
            "error",
            "invalid-build-id",
            "Build manifest requires a stable V2 build ID.",
            path,
        )
    managed = payload.get("managed_files")
    if not isinstance(managed, dict) or not managed:
        report.add(
            "error",
            "missing-managed-files",
            "Build manifest requires generated-file ownership hashes.",
            path,
        )
        return
    for relative, entry in managed.items():
        if not isinstance(relative, str) or not isinstance(entry, dict):
            report.add(
                "error",
                "invalid-managed-file",
                "Managed-file entries must map relative paths to objects.",
                path,
            )
            continue
        managed_path = (root / relative).resolve()
        try:
            managed_path.relative_to(root)
        except ValueError:
            report.add(
                "error",
                "escaping-managed-file",
                "Build manifest contains a path outside the Skill.",
                path,
            )
            continue
        if not managed_path.is_file():
            report.add(
                "error",
                "missing-managed-file",
                f"Build manifest points to missing generated file {relative}.",
                path,
            )
            continue
        expected = entry.get("sha256")
        digest = hashlib.sha256(managed_path.read_bytes()).hexdigest()
        if expected != digest:
            report.add(
                "error",
                "managed-file-modified",
                f"Generated file differs from its build receipt: {relative}.",
                managed_path,
            )


def validate_skill(
    root: Path, *, run_official: bool = True, check_code: bool = False
) -> ValidationReport:
    root = root.expanduser().resolve()
    if not root.is_dir():
        raise ValidationError(f"Skill directory does not exist: {root}")
    report = ValidationReport(skill_path=root, valid=True)
    skill_path = root / "SKILL.md"
    if not skill_path.is_file():
        report.add("error", "missing-skill", "SKILL.md is required.", skill_path)
        return report
    all_files = sorted(item for item in root.rglob("*") if item.is_file())
    markdown_files = [path for path in all_files if path.suffix.casefold() == ".md"]
    report.files_checked = len(all_files)
    text_files = _scan_distributed_text(root, all_files, report)

    skill_text = text_files.get(skill_path)
    if skill_text is None:
        report.add(
            "error",
            "unscannable-skill",
            "SKILL.md could not be scanned as bounded UTF text.",
            skill_path,
        )
        return report
    metadata = _frontmatter(skill_text)
    name = metadata.get("name", "")
    description = metadata.get("description", "")
    if not _NAME_RE.fullmatch(name):
        report.add(
            "error",
            "invalid-name",
            "Skill name must use lowercase letters, digits, and hyphens.",
            skill_path,
        )
    if len(name) > 64:
        report.add("error", "name-too-long", "Skill name exceeds 64 characters.", skill_path)
    if not description:
        report.add(
            "error", "missing-description", "Frontmatter description is required.", skill_path
        )
    elif len(description) > 1024:
        report.add(
            "error",
            "description-too-long",
            "Frontmatter description exceeds 1024 characters.",
            skill_path,
        )
    if len(skill_text.splitlines()) > 500:
        report.add(
            "warning",
            "resident-context-large",
            "SKILL.md exceeds 500 lines; move detail into on-demand files.",
            skill_path,
        )
    _validate_course_skill_contract(root, skill_text, report)

    for path in all_files:
        relative = path.relative_to(root)
        if path.suffix.lower() in MEDIA_EXTENSIONS or path.suffix.lower() in {
            ".wav",
            ".sqlite",
            ".sqlite3",
            ".vtt",
            ".srt",
        }:
            report.add(
                "error",
                "private-artifact",
                f"Shareable skill contains raw/intermediate media: {relative}",
                path,
            )
        if _RAW_TRANSCRIPT_RE.search(path.stem):
            report.add(
                "error",
                "raw-transcript",
                f"Shareable skill contains a transcript artifact: {relative}",
                path,
            )

    for path in markdown_files:
        text = text_files.get(path)
        if text is None:
            continue
        for target in _relative_links(text):
            linked = (path.parent / target).resolve()
            try:
                linked.relative_to(root)
            except ValueError:
                report.add(
                    "error",
                    "escaping-link",
                    f"Relative link escapes the skill package: {target}",
                    path,
                )
                continue
            if not linked.exists():
                report.add(
                    "error",
                    "broken-link",
                    f"Relative link does not exist: {target}",
                    path,
                )
        if check_code:
            for code_issue in check_markdown_code(path):
                report.add(
                    "warning" if code_issue.skipped else "error",
                    ("code-check-unavailable" if code_issue.skipped else "invalid-code-block"),
                    (
                        f"Code block {code_issue.block} "
                        f"({code_issue.language or 'plain'}): {code_issue.message}"
                    ),
                    path,
                )

    provenance = _validate_provenance(
        root,
        report,
        text=text_files.get(root / "provenance.json"),
    )
    _validate_build_manifest(
        root,
        report,
        text=text_files.get(root / "build-manifest.json"),
    )
    _validate_playbook_coverage(
        root,
        provenance,
        report,
        texts=text_files,
    )

    official = shutil.which("agentskills") or shutil.which("skills-ref")
    if run_official and official:
        result = run_command(
            [official, "validate", str(root)],
            timeout=120,
            check=False,
        )
        if result.returncode != 0:
            report.add(
                "error",
                "official-validator",
                (result.stderr or result.stdout).strip()[-2000:],
                skill_path,
            )
    return report


def render_validation_report(report: ValidationReport) -> str:
    status = "VALID" if report.valid else "INVALID"
    lines = [
        f"{status}: {report.skill_path}",
        f"Files checked: {report.files_checked}",
    ]
    for issue in report.issues:
        location = f" ({issue.path})" if issue.path else ""
        lines.append(f"- {issue.level.upper()} {issue.code}: {issue.message}{location}")
    if not report.issues:
        lines.append("- No issues found.")
    return "\n".join(lines)
