import json
import os
import sqlite3
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from typer.testing import CliRunner

from video_to_skill.cli import app
from video_to_skill.config import Settings
from video_to_skill.errors import ProcessingError
from video_to_skill.models import (
    MaterializedSource,
    ProcessingStage,
    SourceDescriptor,
    SourcePlatform,
    TranscriptOrigin,
    TranscriptSegment,
    VisualEvent,
    VisualKind,
)
from video_to_skill.tool_runs import (
    MAX_SANITIZE_BYTES,
    MAX_SANITIZE_DEPTH,
    MAX_SANITIZE_NODES,
    MAX_SANITIZE_STRING_CHARS,
    ToolRunFinish,
    ToolRunStart,
    digest_value,
    sanitize_arguments,
    sanitize_value,
    tool_run_scope,
    tracked_operation,
)
from video_to_skill.transcript import Transcriber, transcribe
from video_to_skill.utils import hash_file, run_command
from video_to_skill.visual import OCRProvider, VisionAnalyzer, analyze_visuals
from video_to_skill.workspace import SCHEMA_VERSION, Workspace


def _workspace(tmp_path: Path) -> Workspace:
    return Workspace.create(
        root=tmp_path / "workspace",
        inputs=["demo"],
        settings=Settings(cache_root=tmp_path / "cache"),
        job_id="tool-run-test",
    )


def test_recursive_sanitizer_removes_secrets_urls_and_private_paths(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    internal = root / "sources" / "media.mp4"
    private = tmp_path / "private" / "cookies.txt"
    raw = {
        "authorization": "Bearer top-secret",
        "nested": {
            "api_token": "sk-private",
            "url": "https://media.example/video?Policy=signed&token=secret",
            "internal": internal,
            "private": private,
        },
    }

    sanitized = sanitize_value(raw, workspace_root=root)
    encoded = json.dumps(sanitized, sort_keys=True)
    assert "top-secret" not in encoded
    assert "sk-private" not in encoded
    assert "Policy" not in encoded
    assert "token=secret" not in encoded
    assert str(tmp_path) not in encoded
    assert sanitized["authorization"] == "<redacted>"
    assert sanitized["nested"]["url"] == "<url-host:media.example>"
    assert sanitized["nested"]["internal"] == "workspace:sources/media.mp4"
    assert sanitized["nested"]["private"] == "<external-path>"
    embedded = sanitize_value(
        "tool built at /Users/alice/private with token=abc123",
        workspace_root=root,
    )
    assert embedded == "tool built at <external-path> with token=<redacted>"
    adversarial = sanitize_value(
        {
            "embedded": "fetch https://user:pass@host.example/a?token=secret now",
            "object": "s3://access:secret@bucket/key?X-Amz-Signature=secret",
            "file": "file:///Users/alice/private.txt",
            "home": "read ~/private/cookies.txt",
            "windows": r"open C:\Users\alice\cookies.txt",
            "punctuation_paths": "paths,/Users/alice/private and,~/secret/file",
            "structured_windows": r"list=[C:\Users\alice\secret.txt]",
            "structured_header": 'headers={"X-Api-Key":"sk-secret"}',
            "userinfo": "connect user:password@internal.example",
            "data_uri": "embedded data:text/plain,TOPSECRET",
            "opaque_s3": "embedded s3:bucket/private-key",
        },
        workspace_root=root,
    )
    adversarial_encoded = json.dumps(adversarial, sort_keys=True)
    assert "secret" not in adversarial_encoded
    assert "alice" not in adversarial_encoded
    assert adversarial["embedded"] == "fetch <url-host:host.example> now"
    assert adversarial["object"] == "<uri:s3:bucket>"
    assert adversarial["file"] == "<external-path>"
    assert adversarial["home"] == "read <external-path>"
    assert adversarial["windows"] == "open <external-path>"
    assert adversarial["punctuation_paths"] == ("paths,<external-path> and,<external-path>")
    assert adversarial["structured_windows"] == "list=[<external-path>]"
    assert adversarial["structured_header"] == ('headers={"X-Api-Key":"<redacted>"}')
    assert adversarial["userinfo"] == "connect <credentials>@internal.example"
    assert adversarial["data_uri"] == "embedded <uri:data>"
    assert adversarial["opaque_s3"] == "embedded <uri:s3>"

    nested: object = "value"
    for _ in range(MAX_SANITIZE_DEPTH + 2):
        nested = [nested]
    with pytest.raises(ValueError, match="depth"):
        sanitize_value(nested, workspace_root=root)
    with pytest.raises(ValueError, match="cumulative nodes"):
        sanitize_value(list(range(MAX_SANITIZE_NODES)), workspace_root=root)
    with pytest.raises(ValueError, match="characters"):
        sanitize_value("x" * (MAX_SANITIZE_STRING_CHARS + 1), workspace_root=root)

    branching = {f"branch-{index}": [index, index + 1] for index in range(700)}
    assert len(branching) < MAX_SANITIZE_NODES
    assert all(len(value) < MAX_SANITIZE_NODES for value in branching.values())
    with pytest.raises(ValueError, match="cumulative nodes"):
        sanitize_value(branching, workspace_root=root)

    byte_heavy = ["x" * 4_000 for _ in range((MAX_SANITIZE_BYTES // 4_000) + 1)]
    with pytest.raises(ValueError, match="cumulative budget"):
        sanitize_value(byte_heavy, workspace_root=root)

    arguments = sanitize_arguments(
        [
            "/opt/tools/yt-dlp",
            "--cookies",
            str(private),
            "--add-header=Authorization: Bearer abc123",
            "https://media.example/video?token=secret",
            str(internal),
        ],
        workspace_root=root,
    )
    assert arguments == [
        "yt-dlp",
        "--cookies",
        "<redacted>",
        "--add-header=<redacted>",
        "<url-host:media.example>",
        "workspace:sources/media.mp4",
    ]


def test_subprocess_boundary_records_outputs_failures_resume_and_deterministic_export(
    tmp_path: Path,
) -> None:
    workspace = _workspace(tmp_path)
    output = workspace.root / "artifacts" / "result.txt"
    output.parent.mkdir()
    secret = "never-persist-this-secret"
    signed_url = f"https://media.example/video?token={secret}&expires=1"
    private_path = tmp_path / "private" / "cookies.txt"
    input_digest = digest_value({"source": "stable-input"})

    with tool_run_scope(
        workspace,
        source_id="source-1",
        stage=ProcessingStage.ACQUIRE.value,
        cache_key="acquire-key",
        input_digests={"source": input_digest},
    ):
        run_command(
            [
                sys.executable,
                "-c",
                "from pathlib import Path; import sys; Path(sys.argv[1]).write_text('done')",
                str(output),
                signed_url,
                "--cookies",
                str(private_path),
            ],
            timeout=30,
            provenance_operation="materialize-test-source",
            provenance_outputs=[output],
        )
        with pytest.raises(ProcessingError, match="failed with exit 7"):
            run_command(
                [
                    sys.executable,
                    "-c",
                    "import os, sys; sys.stderr.write(os.environ['PROVENANCE_SECRET']); raise SystemExit(7)",
                ],
                timeout=30,
                env={"PROVENANCE_SECRET": secret},
                provenance_operation="failing-test-tool",
            )

    records = workspace.tool_run_records()
    assert len(records) == 2
    complete = next(
        record for record in records if record["operation"] == "materialize-test-source"
    )
    failed = next(record for record in records if record["operation"] == "failing-test-tool")
    assert complete["tool"] == Path(sys.executable).name.casefold()
    assert complete["tool_version"]
    assert complete["input_sha256"] == {"source": input_digest}
    assert complete["outputs"] == [
        {
            "kind": "file",
            "path": "artifacts/result.txt",
            "sha256": hash_file(output),
            "size": 4,
        }
    ]
    assert complete["execution"]["status"] == "complete"
    assert failed["execution"]["status"] == "failed"
    assert failed["execution"]["return_code"] == 7
    assert failed["execution"]["error_kind"] == "ProcessingError"

    assert (
        workspace.record_tool_cache_hit(
            "source-1",
            ProcessingStage.ACQUIRE,
            "acquire-key",
        )
        == 1
    )
    complete = next(
        record
        for record in workspace.tool_run_records()
        if record["operation"] == "materialize-test-source"
    )
    assert complete["execution"]["cache_hit_count"] == 1

    first_report = workspace.export_tool_runs()
    first_bytes = (workspace.root / "logs" / "tool-runs.jsonl").read_bytes()
    second_report = workspace.export_tool_runs()
    second_bytes = (workspace.root / "logs" / "tool-runs.jsonl").read_bytes()
    assert first_report == second_report
    assert first_bytes == second_bytes
    assert first_report["records"] == 2
    serialized = first_bytes.decode("utf-8")
    assert secret not in serialized
    assert str(tmp_path) not in serialized
    assert "expires=1" not in serialized
    exported = [json.loads(line) for line in serialized.splitlines()]
    assert all(item["schema_version"] == 1 for item in exported)
    assert all("stdout" not in item and "stderr" not in item for item in exported)

    with pytest.raises(ProcessingError, match="inside the raw workspace"):
        workspace.export_tool_runs(tmp_path / "outside.jsonl")


def test_tool_runs_are_idempotent_under_concurrent_writers(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    logical_output_digest = digest_value({"rows": 4})

    def record_once(_: int) -> None:
        with (
            tool_run_scope(
                workspace,
                source_id="source-1",
                stage=ProcessingStage.TRANSCRIBE.value,
                cache_key="shared-key",
                input_digests={"audio": digest_value("same-audio")},
            ),
            tracked_operation(
                tool="test-provider",
                version="1.0",
                operation="transcribe-audio",
                arguments={"model": "small", "language": "en"},
            ) as tool_run,
        ):
            tool_run.add_logical_output(
                "transcript-segments",
                "source-1",
                logical_output_digest,
            )

    with ThreadPoolExecutor(max_workers=8) as executor:
        list(executor.map(record_once, range(24)))

    [record] = workspace.tool_run_records()
    assert record["execution"]["status"] == "complete"
    assert record["execution"]["attempt_count"] == 24
    assert len(record["attempts"]) == 24
    assert record["outputs"] == [
        {
            "kind": "logical-record",
            "record_id": "source-1",
            "record_type": "transcript-segments",
            "semantic_sha256": logical_output_digest,
            "verification": "missing",
        }
    ]
    with sqlite3.connect(workspace.database_path) as connection:
        assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)


class _FakeTranscriber(Transcriber):
    name = "test-asr-provider"

    def available(self) -> bool:
        return True

    def transcribe(
        self,
        audio_path: Path,
        *,
        source_id: str,
        language: str | None,
        settings: Settings,
    ) -> list[TranscriptSegment]:
        del audio_path, language, settings
        return [
            TranscriptSegment(
                id="transcript-1",
                source_id=source_id,
                start=0,
                end=1,
                text="Engine-owned provenance.",
                origin=TranscriptOrigin.ASR,
            )
        ]


def test_asr_provider_is_recorded_without_agent_mediation(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    source = SourceDescriptor(
        id="source-1",
        platform=SourcePlatform.LOCAL,
        locator=str(tmp_path / "private" / "source.mp4"),
        title="Private source",
    )
    workspace.upsert_sources([source])
    source_directory = workspace.source_directory(source.id)
    media_path = source_directory / "media.mp4"
    audio_path = source_directory / "audio-16khz.wav"
    media_path.write_bytes(b"media")
    audio_path.write_bytes(b"audio")
    materialized = MaterializedSource(
        source=source,
        directory=source_directory,
        media_path=media_path,
        content_hash="content",
    )

    with tool_run_scope(
        workspace,
        source_id=source.id,
        stage=ProcessingStage.TRANSCRIBE.value,
        cache_key="transcribe-key",
        input_digests={"materialized": digest_value("content")},
    ):
        segments, warnings = transcribe(
            materialized,
            Settings(cache_root=tmp_path, asr_provider="test-asr-provider"),
            transcriber=_FakeTranscriber(),
        )
    workspace.replace_transcripts(source.id, segments)

    assert segments[0].text == "Engine-owned provenance."
    assert "caption-replaced-by-asr" in warnings
    [record] = workspace.tool_run_records()
    assert record["tool"] == "test-asr-provider"
    assert record["operation"] == "transcribe-audio"
    assert record["arguments"] == {
        "diarize": False,
        "language": "auto",
        "model": "small",
    }
    assert record["outputs"] == [
        {
            "kind": "logical-record",
            "record_id": source.id,
            "record_type": "transcript-segments",
            "semantic_sha256": digest_value(
                [segment.model_dump(mode="json") for segment in segments]
            ),
            "verification": "verified",
        }
    ]


class _FakeOCR(OCRProvider):
    name = "test-ocr-provider"

    def available(self) -> bool:
        return True

    def recognize(self, path: Path) -> tuple[str, float | None]:
        del path
        return "Settings Save Cancel", 0.9


class _FakeVision(VisionAnalyzer):
    name = "test-vision-provider"

    def available(self) -> bool:
        return True

    def analyze(self, event: VisualEvent, settings: Settings) -> VisualEvent:
        del settings
        event.kind = VisualKind.UI
        event.description = "A settings dialog is visible."
        event.confidence = 0.8
        return event


def test_ocr_and_vision_providers_share_the_engine_recorder(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = _workspace(tmp_path)
    source = SourceDescriptor(
        id="source-1",
        platform=SourcePlatform.LOCAL,
        locator=str(tmp_path / "private" / "source.mp4"),
        title="Private source",
    )
    workspace.upsert_sources([source])
    source_directory = workspace.source_directory(source.id)
    media_path = source_directory / "media.mp4"
    frame_path = source_directory / "frames" / "frame.jpg"
    frame_path.parent.mkdir()
    media_path.write_bytes(b"media")
    frame_path.write_bytes(b"frame")
    materialized = MaterializedSource(
        source=source,
        directory=source_directory,
        media_path=media_path,
        content_hash="content",
    )
    event = VisualEvent(
        id="visual-1",
        source_id=source.id,
        timestamp=1,
        path=frame_path,
    )
    monkeypatch.setattr("video_to_skill.visual.has_video_stream", lambda *_args: True)
    monkeypatch.setattr(
        "video_to_skill.visual.extract_candidate_frames",
        lambda *_args, **_kwargs: [event],
    )

    with tool_run_scope(
        workspace,
        source_id=source.id,
        stage=ProcessingStage.VISUALS.value,
        cache_key="visual-key",
        input_digests={"materialized": digest_value("content")},
    ):
        events, warnings = analyze_visuals(
            materialized,
            Settings(
                cache_root=tmp_path,
                visual_profile="always",
                ocr_provider="auto",
                vision_provider="auto",
            ),
            ocr=_FakeOCR(),
            vision=_FakeVision(),
        )
    workspace.replace_visuals(source.id, events)

    assert warnings == []
    assert events[0].description == "A settings dialog is visible."
    records = workspace.tool_run_records()
    assert {(record["tool"], record["operation"]) for record in records} == {
        ("test-ocr-provider", "recognize-frame"),
        ("test-vision-provider", "analyze-frame"),
    }
    assert all(record["outputs"][0]["kind"] == "logical-record" for record in records)
    assert all(record["outputs"][0]["verification"] == "verified" for record in records)


def test_schema_upgrade_and_cli_export_preserve_legacy_workspace(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    with workspace.connect() as connection:
        connection.execute("DROP TABLE tool_run_attempts")
        connection.execute("DROP TABLE tool_runs")
        connection.execute("UPDATE metadata SET value='8' WHERE key='schema_version'")

    reopened = Workspace.open(workspace.root)
    with reopened.connect() as connection:
        assert (
            int(
                connection.execute(
                    "SELECT value FROM metadata WHERE key='schema_version'"
                ).fetchone()["value"]
            )
            == SCHEMA_VERSION
        )
        assert connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='tool_runs'"
        ).fetchone()

    runner = CliRunner()
    result = runner.invoke(app, ["tool-runs", str(reopened.root), "--json"])
    assert result.exit_code == 0, result.output
    report = json.loads(result.stdout)
    assert report["records"] == 0
    assert report["path"] == "logs/tool-runs.jsonl"
    assert (reopened.root / "logs" / "tool-runs.jsonl").read_bytes() == b""


def test_late_completion_cannot_replace_newer_attempt_state(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    started = datetime.now(UTC)
    start = ToolRunStart(
        id="toolrun-stale-completion",
        identity_digest="a" * 64,
        tool="provider",
        tool_version="1",
        operation="transcribe",
        source_id="source-1",
        stage=ProcessingStage.TRANSCRIBE.value,
        input_digests={"audio": "b" * 64},
        arguments={"model": "small"},
        cache_key="shared",
        started_at=started,
    )
    first_attempt = workspace.start_tool_run(start)
    second_attempt = workspace.start_tool_run(
        ToolRunStart(**{**start.__dict__, "started_at": started + timedelta(seconds=1)})
    )
    workspace.finish_tool_run(
        ToolRunFinish(
            attempt_id=second_attempt,
            status="complete",
            outputs=[],
            return_code=0,
            error_kind=None,
            completed_at=started + timedelta(seconds=2),
            duration_ms=1_000,
        )
    )
    workspace.finish_tool_run(
        ToolRunFinish(
            attempt_id=first_attempt,
            status="failed",
            outputs=[],
            return_code=1,
            error_kind="ProcessingError",
            completed_at=started + timedelta(seconds=3),
            duration_ms=3_000,
        )
    )

    [record] = workspace.tool_run_records()
    assert record["execution"]["status"] == "complete"
    assert [attempt["status"] for attempt in record["attempts"]] == ["failed", "complete"]


def test_schema_nine_tool_run_is_migrated_to_an_attempt(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    now = datetime.now(UTC).isoformat()
    with workspace.connect() as connection:
        connection.execute("DROP TABLE tool_run_attempts")
        connection.execute("DROP TABLE tool_runs")
        connection.executescript(
            """
            CREATE TABLE tool_runs (
                id TEXT PRIMARY KEY, identity_digest TEXT NOT NULL UNIQUE,
                tool TEXT NOT NULL, tool_version TEXT, operation TEXT NOT NULL,
                source_id TEXT, stage TEXT, input_digests_json TEXT NOT NULL,
                arguments_json TEXT NOT NULL, outputs_json TEXT NOT NULL,
                cache_key TEXT, status TEXT NOT NULL, return_code INTEGER,
                error_kind TEXT, attempt_count INTEGER NOT NULL,
                cache_hit_count INTEGER NOT NULL, first_started_at TEXT NOT NULL,
                last_started_at TEXT NOT NULL, completed_at TEXT,
                duration_ms INTEGER, updated_at TEXT NOT NULL
            );
            """
        )
        connection.execute(
            """
            INSERT INTO tool_runs VALUES(
                'legacy', ?, 'ffmpeg', '1', 'probe', 'source-1', 'acquire',
                '{}', '{}', '[]', 'key', 'failed', 1, 'ProcessingError',
                3, 2, ?, ?, ?, 12, ?
            )
            """,
            ("d" * 64, now, now, now, now),
        )
        connection.execute("UPDATE metadata SET value='9' WHERE key='schema_version'")

    reopened = Workspace.open(workspace.root)
    [record] = reopened.tool_run_records()
    assert record["execution"]["status"] == "failed"
    assert record["execution"]["attempt_count"] == 3
    assert record["execution"]["cache_hit_count"] == 2
    assert record["attempts"][0]["id"] == "legacy-attempt-000003"


def test_export_is_logs_only_no_clobber_and_bounded(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    original_manifest = workspace.manifest_path.read_bytes()
    with pytest.raises(ProcessingError, match="invalid"):
        workspace.export_tool_runs(workspace.manifest_path)
    assert workspace.manifest_path.read_bytes() == original_manifest
    with pytest.raises(ProcessingError, match="invalid"):
        workspace.export_tool_runs(workspace.database_path)

    destination = workspace.root / "logs" / "tool-runs.jsonl"
    destination.parent.mkdir()
    destination.write_bytes(b"do-not-overwrite\n")
    with pytest.raises(ProcessingError, match="different content"):
        workspace.export_tool_runs(destination)
    assert destination.read_bytes() == b"do-not-overwrite\n"

    destination.unlink()
    with workspace.connect() as connection:
        now = datetime.now(UTC).isoformat()
        connection.execute(
            """
            INSERT INTO tool_runs(
                id, identity_digest, tool, operation, input_digests_json,
                arguments_json, attempt_count, created_at, updated_at
            ) VALUES('oversized', ?, 'tool', 'operation', '{}', '{}', 1, ?, ?)
            """,
            ("c" * 64, now, now),
        )
        connection.execute(
            """
            INSERT INTO tool_run_attempts(
                id, tool_run_id, generation, status, outputs_json, started_at, updated_at
            ) VALUES('oversized-attempt', 'oversized', 1, 'complete', ?, ?, ?)
            """,
            (json.dumps([{"value": "x" * (MAX_SANITIZE_STRING_CHARS + 1)}]), now, now),
        )
    with pytest.raises(ValueError, match="characters"):
        workspace.export_tool_runs(destination)
    assert not destination.exists()


@pytest.mark.skipif(not hasattr(os, "symlink"), reason="symlinks unavailable")
def test_export_rejects_symlinked_workspace_path(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (workspace.root / "logs").symlink_to(outside, target_is_directory=True)

    with pytest.raises(ProcessingError, match="unsafe"):
        workspace.export_tool_runs()
    assert list(outside.iterdir()) == []
