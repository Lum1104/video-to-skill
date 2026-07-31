from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import stat
import struct
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from test_author import _analyzed_workspace
from typer.testing import CliRunner

from video_to_skill.cli import app
from video_to_skill.config import Settings
from video_to_skill.editions import create_edition_state, identity_baseline
from video_to_skill.errors import ProcessingError
from video_to_skill.evidence_bundles import (
    EvidenceBundleMode,
    export_evidence_bundle,
    verify_evidence_bundle,
)
from video_to_skill.models import (
    AgentObservation,
    EvidenceGap,
    EvidenceGapSeverity,
    EvidenceGapType,
    ObservationProducer,
    ObservationType,
    SemanticSegment,
    SourceDescriptor,
    SourcePlatform,
    TranscriptOrigin,
    TranscriptSegment,
    VisualEvent,
)
from video_to_skill.utils import stable_hash
from video_to_skill.workspace import Workspace


def _workspace(tmp_path: Path) -> Workspace:
    workspace = Workspace.create(
        root=tmp_path / "workspace",
        inputs=["https://media.example/video?token=manifest-secret"],
        settings=Settings(cache_root=tmp_path / "cache"),
        job_id="bundle-test",
    )
    source = SourceDescriptor(
        id="source-one",
        platform=SourcePlatform.YOUTUBE,
        locator="https://media.example/video?token=descriptor-secret",
        canonical_url="https://media.example/watch?v=one&signature=private",
        title="Evidence course",
        creator="Instructor",
        duration=60,
    )
    workspace.upsert_sources([source])
    source_dir = workspace.source_directory(source.id)
    media = source_dir / "media.mp4"
    media.write_bytes(b"private-media")
    caption = source_dir / "captions.en.vtt"
    caption.write_text(
        "WEBVTT\n\n00:00.000 --> 00:01.000\nRedistributable words\n", encoding="utf-8"
    )
    workspace.update_materialization(
        source.id,
        content_hash="content-one",
        source_dir=source_dir,
        media_path=media,
        caption_paths=[caption],
    )
    workspace.replace_transcripts(
        source.id,
        [
            TranscriptSegment(
                id="transcript-one",
                source_id=source.id,
                start=0,
                end=1,
                text="Redistributable words",
                origin=TranscriptOrigin.MANUAL_CAPTION,
            )
        ],
    )
    workspace.upsert_observations(
        [
            AgentObservation(
                source_id=source.id,
                start=0,
                end=1,
                type=ObservationType.CONCEPT,
                claim="A retained observation",
                transcript_ids=["transcript-one"],
                confidence=0.9,
                producer=ObservationProducer(name="test-observer"),
            )
        ]
    )
    workspace.upsert_gaps(
        [
            EvidenceGap(
                source_id=source.id,
                gap_type=EvidenceGapType.UNOBSERVED_CLAIM,
                severity=EvidenceGapSeverity.WARNING,
                message="A bounded evidence gap",
                suggested_next_action="Inspect another frame",
                start=2,
                end=3,
            )
        ]
    )
    contact = source_dir / "contact-sheets" / "overview.jpg"
    contact.parent.mkdir()
    contact.write_bytes(b"contact-sheet")
    return workspace


def _manifest(archive: zipfile.ZipFile) -> dict[str, object]:
    return json.loads(archive.read("bundle-manifest.json"))


def _canonical_zip_info(name: str) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
    info.create_system = 3
    info.compress_type = zipfile.ZIP_STORED
    info.external_attr = (stat.S_IFREG | 0o600) << 16
    info.flag_bits |= 0x800
    return info


def _compact_records(
    *, forbidden: bool = False, transcript: bool = False
) -> dict[str, tuple[str, bytes]]:
    records = {
        "sources/source-one/metadata.json": ("source-metadata", b"{}\n"),
        "evidence/observations.jsonl": ("observations", b""),
        "evidence/gaps.json": ("evidence-gaps", b"[]\n"),
        "logs/tool-runs.jsonl": ("sanitized-tool-runs", b""),
        "visuals/selected.json": ("selected-visual-index", b"[]\n"),
    }
    if forbidden:
        records["sources/source-one/media.mp4"] = ("raw-media", b"raw-video")
    if transcript:
        records["transcripts/source-one/normalized.jsonl"] = (
            "authorized-normalized-transcript",
            b"{}\n",
        )
    return records


def _archival_records() -> dict[str, tuple[str, bytes]]:
    return {
        "workspace/manifest.json": ("sanitized-workspace-manifest", b"{}\n"),
        "workspace/evidence.sqlite3": ("private-evidence-database", b"sqlite"),
        "workspace/logs/tool-runs.jsonl": ("sanitized-tool-runs", b""),
    }


def _write_policy_bundle(
    path: Path,
    records: dict[str, tuple[str, bytes]],
    *,
    mode: str = "compact",
    private_sensitive: bool = False,
    transcript_authorized: bool = False,
    mutate_info=None,
    archive_comment: bytes = b"",
    source_lineage: list[dict[str, object]] | None = None,
) -> None:
    files = [
        {
            "path": name,
            "sha256": hashlib.sha256(payload).hexdigest(),
            "size": len(payload),
            "kind": kind,
        }
        for name, (kind, payload) in sorted(records.items())
    ]
    manifest_without_id = {
        "schema_version": 1,
        "format": "video-to-skill-evidence-zip-v1",
        "mode": mode,
        "generator": {"name": "video-to-skill", "version": "test"},
        "workspace_schema_version": 10,
        "workspace_job_id": "test-job",
        "workspace_snapshot_digest": "snapshot",
        "analysis_depth": None,
        "source_lineage": source_lineage
        if source_lineage is not None
        else (
            [{"source_id": "source-one", "descriptor_sha256": "0" * 64}]
            if mode == "compact"
            else []
        ),
        "edition": None,
        "transcript_redistribution_authorized": transcript_authorized,
        "private_sensitive": private_sensitive,
        "files": files,
    }
    manifest = {
        **manifest_without_id,
        "bundle_id": "bundle-" + stable_hash(manifest_without_id, length=32),
    }
    manifest_payload = (
        json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode()
    with zipfile.ZipFile(path, mode="w", compression=zipfile.ZIP_STORED) as archive:
        manifest_info = _canonical_zip_info("bundle-manifest.json")
        if mutate_info is not None:
            mutate_info(manifest_info)
        archive.writestr(manifest_info, manifest_payload)
        for name, (_kind, payload) in sorted(records.items()):
            info = _canonical_zip_info(name)
            with archive.open(info, mode="w") as member:
                member.write(payload)
        archive.comment = archive_comment


def _patch_all_zip_flags(path: Path, flag_bits: int) -> None:
    payload = bytearray(path.read_bytes())
    cursor = 0
    while (offset := payload.find(b"PK\x03\x04", cursor)) >= 0:
        payload[offset + 6 : offset + 8] = flag_bits.to_bytes(2, "little")
        cursor = offset + 4
    cursor = 0
    while (offset := payload.find(b"PK\x01\x02", cursor)) >= 0:
        payload[offset + 8 : offset + 10] = flag_bits.to_bytes(2, "little")
        cursor = offset + 4
    path.write_bytes(payload)


def test_compact_bundle_uses_shareable_allowlist_and_transcript_opt_in(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    secret = workspace.source_directory("source-one") / "cookies.txt"
    secret.write_text("session=never-export", encoding="utf-8")
    behavior_target = workspace.analysis_dir / "behavior-targets" / "preview" / "SKILL.md"
    behavior_target.parent.mkdir(parents=True)
    behavior_target.write_text("generated Skill must not leak", encoding="utf-8")

    output = tmp_path / "compact.v2sbundle"
    report = export_evidence_bundle(workspace, output)

    assert report.mode == EvidenceBundleMode.COMPACT
    assert report.private_sensitive is False
    assert stat.S_IMODE(output.stat().st_mode) == 0o600
    with zipfile.ZipFile(output) as archive:
        names = archive.namelist()
        manifest = _manifest(archive)
        assert names == ["bundle-manifest.json", *sorted(names[1:])]
        assert "sources/source-one/metadata.json" in names
        assert "evidence/observations.jsonl" in names
        assert "evidence/gaps.json" in names
        assert "logs/tool-runs.jsonl" in names
        assert "visuals/contact-sheets/source-one/overview.jpg" in names
        assert not any(name.startswith("transcripts/") for name in names)
        assert not any("media.mp4" in name for name in names)
        assert not any("evidence.sqlite3" in name for name in names)
        assert not any("cookie" in name.casefold() for name in names)
        assert not any(name.endswith("SKILL.md") for name in names)
        metadata = json.loads(archive.read("sources/source-one/metadata.json"))
        assert "locator" not in metadata
        assert "canonical_url" not in metadata
        assert manifest["transcript_redistribution_authorized"] is False
        archive_bytes = b"".join(archive.read(name) for name in names)
        assert b"descriptor-secret" not in archive_bytes
        assert b"manifest-secret" not in archive_bytes
        assert b"never-export" not in archive_bytes
        assert os.fsencode(tmp_path) not in archive_bytes

    authorized = tmp_path / "compact-authorized.v2sbundle"
    export_evidence_bundle(
        workspace,
        authorized,
        authorize_transcript_redistribution=True,
    )
    with zipfile.ZipFile(authorized) as archive:
        names = archive.namelist()
        assert "transcripts/source-one/normalized.jsonl" in names
        assert "transcripts/source-one/caption-001.vtt" in names
        assert b"Redistributable words" in archive.read("transcripts/source-one/normalized.jsonl")
        assert _manifest(archive)["transcript_redistribution_authorized"] is True


def test_compact_bundle_contains_canonical_analysis_and_review_derivatives(tmp_path: Path) -> None:
    workspace, _task = _analyzed_workspace(tmp_path)
    output = tmp_path / "analyzed.v2sbundle"

    export_evidence_bundle(workspace, output)

    with zipfile.ZipFile(output) as archive:
        names = set(archive.namelist())
        assert "analysis/semantic-map.json" in names
        assert "analysis/semantic-relations.json" in names
        assert "analysis/capability-evidence.json" in names
        assert "analysis/semantic-coverage.json" in names
        assert "visuals/selected.json" in names


def test_archival_bundle_requires_confirmation_is_private_and_excludes_secrets(
    tmp_path: Path,
) -> None:
    workspace = _workspace(tmp_path)
    source_dir = workspace.source_directory("source-one")
    frames = source_dir / "frames"
    frames.mkdir()
    (frames / "frame-001.jpg").write_bytes(b"full-frame")
    cache = source_dir / "cache"
    cache.mkdir()
    (cache / "cached.bin").write_bytes(b"cache")
    (source_dir / "auth-token.txt").write_text("secret", encoding="utf-8")
    (source_dir / "download.lock").write_text("locked", encoding="utf-8")
    target = workspace.analysis_dir / "behavior-targets" / "preview"
    target.mkdir(parents=True)
    (target / "SKILL.md").write_text("generated output", encoding="utf-8")
    output = tmp_path / "private.v2sbundle"

    with pytest.raises(ProcessingError, match="confirm-private-archival"):
        export_evidence_bundle(workspace, output, mode="archival")

    report = export_evidence_bundle(
        workspace,
        output,
        mode="archival",
        confirm_private_archival=True,
    )

    assert report.private_sensitive is True
    assert report.warning is not None
    assert stat.S_IMODE(output.stat().st_mode) == 0o600
    verified = verify_evidence_bundle(output)
    assert verified.bundle_id == report.bundle_id
    with zipfile.ZipFile(output) as archive:
        names = archive.namelist()
        assert "workspace/evidence.sqlite3" in names
        assert "workspace/sources/source-one/media.mp4" in names
        assert "workspace/sources/source-one/frames/frame-001.jpg" in names
        assert "workspace/sources/source-one/captions.en.vtt" not in names
        assert "workspace/logs/tool-runs.jsonl" in names
        assert not any("cache" in name.casefold() for name in names)
        assert not any("auth" in name.casefold() for name in names)
        assert not any("token" in name.casefold() for name in names)
        assert not any(name.endswith(".lock") for name in names)
        assert not any("behavior-targets" in name for name in names)
        assert not any(name.endswith("SKILL.md") for name in names)
        sanitized_manifest = json.loads(archive.read("workspace/manifest.json"))
        assert "inputs" not in sanitized_manifest
        assert "workspace" not in sanitized_manifest


def test_archival_bundle_omits_operational_authority_and_sanitizes_retained_evidence(
    tmp_path: Path,
) -> None:
    workspace = _workspace(tmp_path)
    private_root = tmp_path / "private-project-root"
    secrets = {
        "RUN-LEASE-TOKEN",
        "ACCEPTED-LEASE-TOKEN",
        "COMPLETION-LEASE-TOKEN",
        "BUILD-LEASE-TOKEN",
        "DB-LEASE-TOKEN-HASH",
        "PRIVATE-SNAPSHOT-DIGEST",
        "RAW-TREE-SENTINEL",
        "FTS-SENTINEL",
        "SCOPE-SECRET-SENTINEL",
        "PERSONA-SECRET-SENTINEL",
        "PACKET-AUTHORITY-SENTINEL",
        "FAILURE-SECRET-SENTINEL",
        "SCALAR-SECRET",
    }
    (workspace.source_directory("source-one") / "harmless.json").write_text(
        json.dumps(
            {
                "value": "RAW-TREE-SENTINEL",
                "project": "/Users/private/project",
            }
        ),
        encoding="utf-8",
    )
    (workspace.analysis_dir / "run-config.json").write_text(
        json.dumps(
            {
                "output": str(tmp_path / "private-output"),
                "project_root": str(private_root),
                "skill_root": str(tmp_path / "private-skill-root"),
                "workspace": str(workspace.root),
                "nested": {
                    "lease_token": "RUN-LEASE-TOKEN",
                    "snapshot_digest": "PRIVATE-SNAPSHOT-DIGEST",
                },
            }
        ),
        encoding="utf-8",
    )
    result = workspace.analysis_dir / "results" / "accepted.json"
    result.parent.mkdir(parents=True)
    result.write_text(
        json.dumps(
            {
                "lease_token": "ACCEPTED-LEASE-TOKEN",
                "unknown_result_key": {
                    "project_root": str(private_root),
                    "snapshot_digest": "PRIVATE-SNAPSHOT-DIGEST",
                },
            }
        ),
        encoding="utf-8",
    )
    build = workspace.builds_dir / "build-private"
    build.mkdir(parents=True)
    (build / "completion.json").write_text(
        json.dumps(
            {
                "generated_path": str(tmp_path / "generated"),
                "installed_path": str(tmp_path / "installed"),
                "validation_report_path": str(tmp_path / "validation.json"),
                "workspace": str(workspace.root),
                "lease_token": "COMPLETION-LEASE-TOKEN",
            }
        ),
        encoding="utf-8",
    )
    (build / "validation-report.json").write_text(
        json.dumps(
            {
                "valid": True,
                "summary": "retained-build-evidence",
                "nested": {
                    "lease_token": "BUILD-LEASE-TOKEN",
                    "unknown_result_key": {"project_root": str(private_root)},
                },
            }
        ),
        encoding="utf-8",
    )
    now = "2026-01-01T00:00:00+00:00"
    with workspace.connect() as connection:
        connection.execute(
            "INSERT INTO analysis_runs(id, snapshot_digest, created_at, updated_at) "
            "VALUES(?, ?, ?, ?)",
            ("run-private", "PRIVATE-SNAPSHOT-DIGEST", now, now),
        )
        connection.execute(
            """
            INSERT INTO work_items(
                id, run_id, role, scope_json, persona_hint, packet_path, packet_digest,
                result_schema_path, snapshot_digest, state, lease_token_hash, lease_owner,
                lease_expires_at, execution_context_id, attempt_count, failure_reason,
                created_at, updated_at
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, 'leased', ?, ?, ?, ?, 1, ?, ?, ?)
            """,
            (
                "task-private",
                "run-private",
                "observer",
                json.dumps(
                    {
                        "unknown_result_key": {
                            "lease_token": "ACCEPTED-LEASE-TOKEN",
                            "project_root": str(private_root),
                        },
                        "opaque": "SCOPE-SECRET-SENTINEL",
                    }
                ),
                "PERSONA-SECRET-SENTINEL",
                str(tmp_path / "private-packet.json"),
                "PACKET-AUTHORITY-SENTINEL",
                str(tmp_path / "private-schema.json"),
                "PRIVATE-SNAPSHOT-DIGEST",
                "DB-LEASE-TOKEN-HASH",
                "private-worker",
                "2099-01-01T00:00:00+00:00",
                "private-execution-context",
                "FAILURE-SECRET-SENTINEL",
                now,
                now,
            ),
        )
        connection.execute(
            "INSERT INTO transcript_fts(id, source_id, text) VALUES(?, ?, ?)",
            ("fts-private", "source-one", "FTS-SENTINEL /Users/private/project"),
        )
        connection.execute(
            "UPDATE transcript_segments SET id=? WHERE id='transcript-one'",
            ("/Users/private/project/SCALAR-SECRET",),
        )

    output = tmp_path / "sanitized-archival.v2sbundle"
    export_evidence_bundle(
        workspace,
        output,
        mode="archival",
        confirm_private_archival=True,
    )

    with zipfile.ZipFile(output) as archive:
        names = set(archive.namelist())
        assert "workspace/analysis/run-config.json" not in names
        assert "workspace/sources/source-one/harmless.json" not in names
        assert not any("/analysis/results/" in f"/{name}" for name in names)
        assert "workspace/builds/build-private/completion.json" not in names
        retained_name = "workspace/builds/build-private/validation-report.json"
        assert retained_name in names
        retained = json.loads(archive.read(retained_name))
        assert retained["valid"] is True
        assert retained["summary"] == "retained-build-evidence"
        assert retained["nested"]["unknown_result_key"] == {}
        assert "workspace/observations/observations.jsonl" in names
        assert b"A retained observation" in archive.read(
            "workspace/observations/observations.jsonl"
        )
        manifest = _manifest(archive)
        bundle_bytes = b"".join(archive.read(name) for name in archive.namelist())
        manifest_bytes = json.dumps(manifest, sort_keys=True).encode()
        for secret in secrets:
            assert secret.encode() not in bundle_bytes
            assert secret.encode() not in manifest_bytes
        assert os.fsencode(tmp_path) not in bundle_bytes
        assert b"/Users/private/project" not in bundle_bytes

        database_copy = tmp_path / "archival-evidence.sqlite3"
        database_copy.write_bytes(archive.read("workspace/evidence.sqlite3"))
    with sqlite3.connect(database_copy) as connection:
        assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
        }
        assert tables == {
            "agent_observations",
            "archive_metadata",
            "canonical_heads",
            "canonical_records",
            "evidence_gaps",
            "semantic_segments",
            "sources",
            "transcript_segments",
            "visual_events",
        }
        assert connection.execute("SELECT COUNT(*) FROM sources").fetchone() == (1,)
        assert connection.execute("SELECT COUNT(*) FROM agent_observations").fetchone() == (1,)
        assert connection.execute("SELECT COUNT(*) FROM evidence_gaps").fetchone() == (1,)
        assert connection.execute("SELECT id FROM transcript_segments").fetchone() == (
            "transcript-one",
        )


@pytest.mark.parametrize(
    "path",
    [
        "workspace/analysis/results/accepted.json",
        "workspace/builds/build-one/completion.json",
    ],
)
def test_verifier_rejects_raw_archival_operational_records(tmp_path: Path, path: str) -> None:
    bundle = tmp_path / "forbidden-archival.v2sbundle"
    records = _archival_records()
    records[path] = ("private-workspace-file", b'{"lease_token":"secret"}\n')
    _write_policy_bundle(bundle, records, mode="archival", private_sensitive=True)

    with pytest.raises(ProcessingError, match="forbidden member or kind"):
        verify_evidence_bundle(bundle)


def test_verifier_rejects_checksummed_non_sqlite_archival_database(tmp_path: Path) -> None:
    bundle = tmp_path / "not-sqlite.v2sbundle"
    _write_policy_bundle(
        bundle,
        _archival_records(),
        mode="archival",
        private_sensitive=True,
    )

    with pytest.raises(ProcessingError, match="database is invalid"):
        verify_evidence_bundle(bundle)


def test_verifier_rejects_checksummed_database_with_operational_schema(tmp_path: Path) -> None:
    forged_database = tmp_path / "forged-operational.sqlite3"
    with sqlite3.connect(forged_database) as connection:
        connection.execute(
            "CREATE TABLE work_items(scope_json TEXT, persona_hint TEXT, "
            "packet_digest TEXT, failure_reason TEXT)"
        )
        connection.execute(
            "INSERT INTO work_items VALUES(?, ?, ?, ?)",
            (
                '{"opaque":"SCOPE-SECRET-SENTINEL"}',
                "PERSONA-SECRET-SENTINEL",
                "PACKET-AUTHORITY-SENTINEL",
                "FAILURE-SECRET-SENTINEL",
            ),
        )
    records = _archival_records()
    records["workspace/evidence.sqlite3"] = (
        "private-evidence-database",
        forged_database.read_bytes(),
    )
    bundle = tmp_path / "forged-operational.v2sbundle"
    _write_policy_bundle(bundle, records, mode="archival", private_sensitive=True)

    with pytest.raises(ProcessingError, match="database schema is forbidden"):
        verify_evidence_bundle(bundle)


def test_verifier_rejects_unsafe_sanitized_json_before_database_trust(tmp_path: Path) -> None:
    records = _archival_records()
    records["workspace/builds/build-one/validation-report.json"] = (
        "private-sanitized-build-record",
        b'{"nested":{"project":"/Users/private/project"}}\n',
    )
    bundle = tmp_path / "forged-json.v2sbundle"
    _write_policy_bundle(bundle, records, mode="archival", private_sensitive=True)

    with pytest.raises(ProcessingError, match="unsafe string"):
        verify_evidence_bundle(bundle)


def test_verifier_rejects_forbidden_source_metadata_in_valid_database(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    original = tmp_path / "original-private.v2sbundle"
    export_evidence_bundle(
        workspace,
        original,
        mode="archival",
        confirm_private_archival=True,
    )
    forged_database = tmp_path / "forged-source.sqlite3"
    with zipfile.ZipFile(original) as archive:
        forged_database.write_bytes(archive.read("workspace/evidence.sqlite3"))
    with sqlite3.connect(forged_database) as connection:
        raw = connection.execute(
            "SELECT descriptor_json FROM sources WHERE id='source-one'"
        ).fetchone()
        assert raw is not None
        descriptor = json.loads(raw[0])
        descriptor["locator"] = "sanitized-looking-but-forbidden"
        descriptor_text = json.dumps(
            descriptor,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        connection.execute(
            "UPDATE sources SET descriptor_json=? WHERE id='source-one'",
            (descriptor_text,),
        )
    descriptor_payload = (descriptor_text + "\n").encode()
    records = _archival_records()
    records["workspace/evidence.sqlite3"] = (
        "private-evidence-database",
        forged_database.read_bytes(),
    )
    bundle = tmp_path / "forged-source-metadata.v2sbundle"
    _write_policy_bundle(
        bundle,
        records,
        mode="archival",
        private_sensitive=True,
        source_lineage=[
            {
                "source_id": "source-one",
                "descriptor_sha256": hashlib.sha256(descriptor_payload).hexdigest(),
            }
        ],
    )

    with pytest.raises(ProcessingError, match="source metadata identity is invalid"):
        verify_evidence_bundle(bundle)


@pytest.mark.parametrize(
    ("table", "column", "bad_value"),
    [
        ("transcript_segments", "id", "/Users/private/project/SCALAR-SECRET"),
        ("visual_events", "timestamp", 42.0),
        ("semantic_segments", "ordinal", 7),
        ("agent_observations", "start", 12.0),
        ("evidence_gaps", "end", 99.0),
    ],
)
def test_verifier_rejects_typed_evidence_scalar_mismatch(
    tmp_path: Path,
    table: str,
    column: str,
    bad_value: object,
) -> None:
    workspace = _workspace(tmp_path)
    contact = workspace.source_directory("source-one") / "contact-sheets" / "overview.jpg"
    workspace.replace_visuals(
        "source-one",
        [
            VisualEvent(
                id="visual-one",
                source_id="source-one",
                timestamp=0.5,
                path=contact,
            )
        ],
    )
    workspace.replace_semantic_segments(
        "source-one",
        [
            SemanticSegment(
                id="semantic-one",
                source_id="source-one",
                ordinal=1,
                title="Opening",
                start=0,
                end=1,
                transcript_ids=["transcript-one"],
                visual_event_ids=["visual-one"],
            )
        ],
    )
    original = tmp_path / "typed-original.v2sbundle"
    export_evidence_bundle(
        workspace,
        original,
        mode="archival",
        confirm_private_archival=True,
    )
    forged_database = tmp_path / f"forged-{table}-{column}.sqlite3"
    with zipfile.ZipFile(original) as archive:
        forged_database.write_bytes(archive.read("workspace/evidence.sqlite3"))
        source_lineage = _manifest(archive)["source_lineage"]
    assert isinstance(source_lineage, list)
    with sqlite3.connect(forged_database) as connection:
        connection.execute(f"UPDATE {table} SET {column}=?", (bad_value,))
    records = _archival_records()
    records["workspace/evidence.sqlite3"] = (
        "private-evidence-database",
        forged_database.read_bytes(),
    )
    bundle = tmp_path / f"forged-{table}-{column}.v2sbundle"
    _write_policy_bundle(
        bundle,
        records,
        mode="archival",
        private_sensitive=True,
        source_lineage=source_lineage,
    )

    with pytest.raises(ProcessingError, match=f"{table}.{column} differs from data_json"):
        verify_evidence_bundle(bundle)


def test_bundle_is_deterministic_create_only_and_concurrency_safe(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    first = tmp_path / "first.v2sbundle"
    second = tmp_path / "second.v2sbundle"

    first_report = export_evidence_bundle(workspace, first)
    second_report = export_evidence_bundle(workspace, second)
    assert first.read_bytes() == second.read_bytes()
    assert first_report.bundle_id == second_report.bundle_id

    concurrent = tmp_path / "concurrent.v2sbundle"
    with ThreadPoolExecutor(max_workers=4) as executor:
        reports = list(
            executor.map(
                lambda _index: export_evidence_bundle(workspace, concurrent),
                range(4),
            )
        )
    assert {report.sha256 for report in reports} == {first_report.sha256}
    assert sum(report.existing_identical for report in reports) == 3

    different = tmp_path / "different.v2sbundle"
    different.write_bytes(b"do-not-overwrite")
    with pytest.raises(ProcessingError, match="different content"):
        export_evidence_bundle(workspace, different)
    assert different.read_bytes() == b"do-not-overwrite"


def test_bundle_rejects_symlinks_and_verifier_rejects_traversal(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    contact_root = workspace.source_directory("source-one") / "contact-sheets"
    (contact_root / "linked.jpg").symlink_to(contact_root / "overview.jpg")

    with pytest.raises(ProcessingError, match="symbolic links"):
        export_evidence_bundle(workspace, tmp_path / "symlink.v2sbundle")

    traversal = tmp_path / "traversal.v2sbundle"
    with zipfile.ZipFile(traversal, mode="w", compression=zipfile.ZIP_STORED) as archive:
        archive.writestr("bundle-manifest.json", b"{}")
        archive.writestr("../escape", b"bad")
    with pytest.raises(ProcessingError, match="unsafe"):
        verify_evidence_bundle(traversal)


@pytest.mark.parametrize("attack", ["caption-symlink", "parent-symlink", "external-escape"])
def test_compact_caption_authorization_rejects_symlinks_and_external_escape(
    tmp_path: Path,
    attack: str,
) -> None:
    workspace = _workspace(tmp_path)
    source_dir = workspace.source_directory("source-one")
    external = tmp_path / "outside"
    external.mkdir()
    external_caption = external / "private.vtt"
    external_caption.write_text("WEBVTT\n\nprivate\n", encoding="utf-8")
    if attack == "caption-symlink":
        candidate = source_dir / "linked.vtt"
        candidate.symlink_to(external_caption)
    elif attack == "parent-symlink":
        linked_parent = source_dir / "caption-parent"
        linked_parent.symlink_to(external, target_is_directory=True)
        candidate = linked_parent / external_caption.name
    else:
        candidate = external_caption
    with workspace.connect() as connection:
        connection.execute(
            "UPDATE sources SET caption_paths_json=? WHERE id=?",
            (json.dumps([str(candidate)]), "source-one"),
        )

    with pytest.raises(ProcessingError, match=r"unsafe|escapes"):
        export_evidence_bundle(
            workspace,
            tmp_path / f"{attack}.v2sbundle",
            authorize_transcript_redistribution=True,
        )


def test_declared_external_local_caption_keeps_original_trusted_root(tmp_path: Path) -> None:
    external = tmp_path / "local-source"
    external.mkdir()
    media = external / "lesson.mp4"
    media.write_bytes(b"local-media")
    caption = external / "lesson.en.vtt"
    caption.write_text("WEBVTT\n\nAuthorized local caption\n", encoding="utf-8")
    second_link = external / "lesson.copy.vtt"
    os.link(caption, second_link)
    workspace = Workspace.create(
        root=tmp_path / "workspace",
        inputs=[str(media)],
        settings=Settings(cache_root=tmp_path / "cache"),
        job_id="local-bundle-test",
    )
    source = SourceDescriptor(
        id="local-source-one",
        platform=SourcePlatform.LOCAL,
        locator=str(media),
        title="Local lesson",
    )
    workspace.upsert_sources([source])
    source_dir = workspace.source_directory(source.id)
    workspace.update_materialization(
        source.id,
        content_hash="local-content",
        source_dir=source_dir,
        media_path=media,
        caption_paths=[caption],
    )

    output = tmp_path / "local-caption.v2sbundle"
    export_evidence_bundle(
        workspace,
        output,
        authorize_transcript_redistribution=True,
    )
    with zipfile.ZipFile(output) as archive:
        assert "transcripts/local-source-one/caption-001.vtt" in archive.namelist()


def test_workspace_hardlinks_are_rejected_for_compact_and_archival(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    source_dir = workspace.source_directory("source-one")
    outside = tmp_path / "outside-frame.jpg"
    outside.write_bytes(b"outside-private-frame")
    compact_link = source_dir / "contact-sheets" / "hardlinked.jpg"
    os.link(outside, compact_link)

    with pytest.raises(ProcessingError, match="hard links"):
        export_evidence_bundle(workspace, tmp_path / "hardlink-compact.v2sbundle")

    compact_link.unlink()
    archival_link = source_dir / "borrowed.mp4"
    os.link(outside, archival_link)
    with workspace.connect() as connection:
        connection.execute(
            "UPDATE sources SET media_path=? WHERE id=?",
            (str(archival_link), "source-one"),
        )
    with pytest.raises(ProcessingError, match="hard links"):
        export_evidence_bundle(
            workspace,
            tmp_path / "hardlink-archival.v2sbundle",
            mode="archival",
            confirm_private_archival=True,
        )


def test_verifier_rejects_self_consistent_forbidden_compact_member(tmp_path: Path) -> None:
    bundle = tmp_path / "forbidden-compact.v2sbundle"
    _write_policy_bundle(bundle, _compact_records(forbidden=True))

    with pytest.raises(ProcessingError, match="forbidden member or kind"):
        verify_evidence_bundle(bundle)


@pytest.mark.parametrize(
    ("mode", "private_sensitive", "transcript_authorized", "records", "message"),
    [
        ("compact", True, False, _compact_records(), "cannot be marked private"),
        (
            "compact",
            False,
            False,
            _compact_records(transcript=True),
            "do not match redistribution authorization",
        ),
        ("archival", False, False, _archival_records(), "must be marked private"),
        (
            "archival",
            True,
            True,
            _archival_records(),
            "cannot claim transcript redistribution",
        ),
    ],
)
def test_verifier_enforces_mode_manifest_coherence(
    tmp_path: Path,
    mode: str,
    private_sensitive: bool,
    transcript_authorized: bool,
    records: dict[str, tuple[str, bytes]],
    message: str,
) -> None:
    bundle = tmp_path / f"coherence-{mode}-{private_sensitive}-{transcript_authorized}.v2sbundle"
    _write_policy_bundle(
        bundle,
        records,
        mode=mode,
        private_sensitive=private_sensitive,
        transcript_authorized=transcript_authorized,
    )

    with pytest.raises(ProcessingError, match=message):
        verify_evidence_bundle(bundle)


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda info: setattr(info, "date_time", (2024, 1, 1, 0, 0, 0)), "timestamp"),
        (lambda info: setattr(info, "external_attr", (stat.S_IFREG | 0o644) << 16), "permissions"),
        (lambda info: setattr(info, "create_system", 0), "creator system"),
        (lambda info: setattr(info, "compress_type", zipfile.ZIP_DEFLATED), "compression"),
        (lambda info: setattr(info, "extra", b"\x99\x99\x00\x00"), "extras"),
        (lambda info: setattr(info, "comment", b"member-comment"), "comments"),
    ],
)
def test_verifier_rejects_noncanonical_member_shape(
    tmp_path: Path,
    mutation,
    message: str,
) -> None:
    bundle = tmp_path / f"shape-{message.replace(' ', '-')}.v2sbundle"
    _write_policy_bundle(bundle, _compact_records(), mutate_info=mutation)

    with pytest.raises(ProcessingError, match=message):
        verify_evidence_bundle(bundle)


def test_verifier_rejects_archive_comment_and_unsupported_flags(tmp_path: Path) -> None:
    commented = tmp_path / "archive-comment.v2sbundle"
    _write_policy_bundle(commented, _compact_records(), archive_comment=b"not-canonical")
    with pytest.raises(ProcessingError, match="archive comments"):
        verify_evidence_bundle(commented)

    for flags in (0x1, 0x8):
        flagged = tmp_path / f"flags-{flags}.v2sbundle"
        _write_policy_bundle(flagged, _compact_records())
        _patch_all_zip_flags(flagged, flags)
        with pytest.raises(ProcessingError, match="unsupported ZIP flags"):
            verify_evidence_bundle(flagged)


def test_verifier_opens_members_and_checks_crc_and_local_header(tmp_path: Path) -> None:
    corrupted = tmp_path / "bad-crc.v2sbundle"
    _write_policy_bundle(corrupted, _compact_records())
    with zipfile.ZipFile(corrupted) as archive:
        info = archive.getinfo("evidence/gaps.json")
        filename_size, extra_size = struct.unpack_from(
            "<HH", corrupted.read_bytes(), info.header_offset + 26
        )
        data_offset = info.header_offset + 30 + filename_size + extra_size
    payload = bytearray(corrupted.read_bytes())
    payload[data_offset] ^= 0x01
    corrupted.write_bytes(payload)
    with pytest.raises(ProcessingError, match="valid archive"):
        verify_evidence_bundle(corrupted)

    mismatched = tmp_path / "bad-local-header.v2sbundle"
    _write_policy_bundle(mismatched, _compact_records())
    with zipfile.ZipFile(mismatched) as archive:
        info = archive.getinfo("evidence/gaps.json")
    payload = bytearray(mismatched.read_bytes())
    local_name_offset = info.header_offset + 30
    payload[local_name_offset] = ord("x")
    mismatched.write_bytes(payload)
    with pytest.raises(ProcessingError, match="local ZIP filename differs"):
        verify_evidence_bundle(mismatched)


def test_verifier_rejects_local_header_time_drift_with_canonical_central_record(
    tmp_path: Path,
) -> None:
    bundle = tmp_path / "local-time-drift.v2sbundle"
    _write_policy_bundle(bundle, _compact_records())
    with zipfile.ZipFile(bundle) as archive:
        info = archive.getinfo("evidence/gaps.json")
        assert info.date_time == (1980, 1, 1, 0, 0, 0)
    payload = bytearray(bundle.read_bytes())
    struct.pack_into("<H", payload, info.header_offset + 10, 1)
    bundle.write_bytes(payload)

    with pytest.raises(ProcessingError, match="local ZIP timestamp differs"):
        verify_evidence_bundle(bundle)


def test_edition_bundle_records_lineage_without_host_paths(tmp_path: Path) -> None:
    workspace, _task = _analyzed_workspace(tmp_path)
    baseline = identity_baseline(workspace)
    state = create_edition_state(
        workspace,
        edition_name="chinese-course",
        requested_output_language="Chinese",
        curriculum_mode="plan",
        curriculum_source_edition=None,
        source_curriculum_plan_digest=None,
        requested_curriculum_path_id=None,
        skill_name="evidence-course-zh",
        host="codex",
        output=tmp_path / "generated-output",
        output_is_default=False,
        project=False,
        project_root=tmp_path / "private-project-root",
        skill_root=tmp_path / "private-skill-root",
        run_official_validation=False,
        baseline=baseline,
        identity_drift_justification=None,
    )
    view = workspace.for_edition(state.configuration.edition_id)
    output = tmp_path / "edition.v2sbundle"

    export_evidence_bundle(view, output)

    with zipfile.ZipFile(output) as archive:
        manifest = _manifest(archive)
        edition = manifest["edition"]
        assert isinstance(edition, dict)
        assert edition["edition_id"] == state.configuration.edition_id
        assert edition["edition_name"] == "chinese-course"
        assert os.fsencode(tmp_path) not in archive.read("bundle-manifest.json")


def test_evidence_bundle_cli_exports_and_verifies(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    output = tmp_path / "cli.v2sbundle"
    runner = CliRunner()

    result = runner.invoke(
        app,
        ["evidence-bundle", str(workspace.root), "--output", str(output), "--json"],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["mode"] == "compact"
    verify = runner.invoke(app, ["verify-evidence-bundle", str(output), "--json"])
    assert verify.exit_code == 0, verify.output
    assert json.loads(verify.stdout)["bundle_id"] == payload["bundle_id"]

    refused = runner.invoke(
        app,
        [
            "evidence-bundle",
            str(workspace.root),
            "--output",
            str(tmp_path / "refused.v2sbundle"),
            "--mode",
            "archival",
        ],
    )
    assert refused.exit_code == 2
    assert "confirm-private-archival" in refused.output
