"""SQLite-backed resumable evidence workspace."""

from __future__ import annotations

import json
import secrets
import shutil
import sqlite3
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from pathlib import Path
from typing import Any, Protocol, cast

from video_to_skill.config import Settings
from video_to_skill.errors import ProcessingError
from video_to_skill.models import (
    AgentObservation,
    EvidenceGap,
    EvidenceGapType,
    InspectionCompleteness,
    JobManifest,
    JobState,
    ObservationStatus,
    ObservationType,
    ProcessingStage,
    RetiredSource,
    SemanticSegment,
    SourceDescriptor,
    TranscriptSegment,
    VisualEvent,
    VisualOrigin,
    WarningRecord,
)
from video_to_skill.utils import atomic_write_json, hash_file, is_within, stable_hash
from video_to_skill.work import (
    AnalysisRun,
    CanonicalRecord,
    WorkItem,
    WorkLease,
    WorkRole,
    WorkState,
)

SCHEMA_VERSION = 6

_AGENT_EVIDENCE_SCHEMA = """
CREATE TABLE IF NOT EXISTS agent_observations (
    id TEXT PRIMARY KEY,
    source_id TEXT NOT NULL,
    start REAL NOT NULL,
    end REAL NOT NULL,
    observation_type TEXT NOT NULL,
    status TEXT NOT NULL,
    data_json TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (source_id) REFERENCES sources(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS observation_source_time
    ON agent_observations(source_id, start, end);
CREATE INDEX IF NOT EXISTS observation_source_type
    ON agent_observations(source_id, observation_type);
CREATE TABLE IF NOT EXISTS evidence_gaps (
    id TEXT PRIMARY KEY,
    source_id TEXT NOT NULL,
    semantic_segment_id TEXT,
    gap_type TEXT NOT NULL,
    severity TEXT NOT NULL,
    start REAL NOT NULL,
    end REAL NOT NULL,
    resolved INTEGER NOT NULL DEFAULT 0 CHECK(resolved IN (0, 1)),
    data_json TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (source_id) REFERENCES sources(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS gap_source_time
    ON evidence_gaps(source_id, start, end);
CREATE INDEX IF NOT EXISTS gap_source_resolved
    ON evidence_gaps(source_id, resolved, severity);
CREATE INDEX IF NOT EXISTS gap_source_section
    ON evidence_gaps(source_id, semantic_segment_id);
"""

_INSPECTION_SCHEMA = """
CREATE TABLE IF NOT EXISTS inspection_reports (
    locator TEXT PRIMARY KEY,
    platform TEXT NOT NULL,
    report_json TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""

_ORCHESTRATION_SCHEMA = """
CREATE TABLE IF NOT EXISTS analysis_runs (
    id TEXT PRIMARY KEY,
    snapshot_digest TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS work_items (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    role TEXT NOT NULL,
    scope_json TEXT NOT NULL,
    persona_hint TEXT NOT NULL,
    packet_path TEXT NOT NULL,
    packet_digest TEXT NOT NULL,
    result_schema_path TEXT NOT NULL,
    snapshot_digest TEXT NOT NULL,
    state TEXT NOT NULL CHECK(state IN ('pending', 'leased', 'complete', 'failed')),
    lease_token_hash TEXT,
    lease_owner TEXT,
    lease_expires_at TEXT,
    execution_context_id TEXT,
    attempt_count INTEGER NOT NULL DEFAULT 0,
    result_path TEXT,
    result_digest TEXT,
    failure_reason TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (run_id) REFERENCES analysis_runs(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS work_item_run_state
    ON work_items(run_id, state, created_at);
CREATE TABLE IF NOT EXISTS work_item_dependencies (
    task_id TEXT NOT NULL,
    dependency_id TEXT NOT NULL,
    PRIMARY KEY (task_id, dependency_id),
    FOREIGN KEY (task_id) REFERENCES work_items(id) ON DELETE CASCADE,
    FOREIGN KEY (dependency_id) REFERENCES work_items(id) ON DELETE RESTRICT,
    CHECK(task_id != dependency_id)
);
CREATE TABLE IF NOT EXISTS work_results (
    task_id TEXT PRIMARY KEY,
    result_path TEXT NOT NULL,
    result_digest TEXT NOT NULL,
    producer_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY (task_id) REFERENCES work_items(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS canonical_records (
    kind TEXT NOT NULL,
    record_id TEXT NOT NULL,
    revision INTEGER NOT NULL CHECK(revision >= 1),
    path TEXT NOT NULL,
    digest TEXT NOT NULL,
    producer_task_id TEXT NOT NULL,
    snapshot_digest TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (kind, record_id, revision),
    FOREIGN KEY (producer_task_id) REFERENCES work_items(id) ON DELETE RESTRICT
);
CREATE TABLE IF NOT EXISTS canonical_heads (
    kind TEXT NOT NULL,
    record_id TEXT NOT NULL,
    revision INTEGER NOT NULL,
    PRIMARY KEY (kind, record_id),
    FOREIGN KEY (kind, record_id, revision)
        REFERENCES canonical_records(kind, record_id, revision) ON DELETE RESTRICT
);
"""

_SQL_REFERENCE_BATCH_SIZE = 500


class _OwnedEvidence(Protocol):
    id: str
    source_id: str


class Workspace:
    """Owns durable processing state; each operation opens its own DB connection."""

    def __init__(self, root: Path):
        self.root = root.resolve()
        self.database_path = self.root / "evidence.sqlite3"
        self.manifest_path = self.root / "manifest.json"
        self.sources_dir = self.root / "sources"
        self.analysis_dir = self.root / "analysis"
        self.tasks_dir = self.analysis_dir / "tasks"

    @classmethod
    def create(
        cls,
        *,
        root: Path,
        inputs: list[str],
        settings: Settings,
        job_id: str | None = None,
    ) -> Workspace:
        job_id = job_id or stable_hash(
            {"inputs": inputs, "configuration": settings.configuration_hash}, length=20
        )
        workspace = cls(root)
        workspace.root.mkdir(parents=True, exist_ok=True)
        workspace.sources_dir.mkdir(parents=True, exist_ok=True)
        workspace.tasks_dir.mkdir(parents=True, exist_ok=True)
        workspace._initialize_database()
        if not workspace.manifest_path.exists():
            manifest = JobManifest(
                job_id=job_id,
                state=JobState.PENDING,
                inputs=inputs,
                workspace=workspace.root,
                configuration_hash=settings.configuration_hash,
            )
            workspace.save_manifest(manifest)
        else:
            manifest = workspace.load_manifest()
            if manifest.configuration_hash != settings.configuration_hash:
                raise ProcessingError(
                    "Workspace configuration differs from this run. Use a new workspace "
                    "or restore the original configuration."
                )
            if manifest.inputs != inputs:
                raise ProcessingError("Workspace inputs differ from this run. Use a new workspace.")
        return workspace

    @classmethod
    def open(cls, root: Path) -> Workspace:
        workspace = cls(root)
        if not workspace.database_path.is_file() or not workspace.manifest_path.is_file():
            raise ProcessingError(f"Not a video-to-skill workspace: {root}")
        workspace._check_schema()
        return workspace

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.database_path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 30000")
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _initialize_database(self) -> None:
        with self.connect() as connection:
            metadata_exists = connection.execute(
                """
                SELECT 1 FROM sqlite_master
                WHERE type='table' AND name='metadata'
                """
            ).fetchone()
            existing_version = (
                connection.execute(
                    "SELECT value FROM metadata WHERE key='schema_version'"
                ).fetchone()
                if metadata_exists
                else None
            )
            if existing_version is not None:
                try:
                    version = int(existing_version["value"])
                except (TypeError, ValueError) as exc:
                    raise ProcessingError("Workspace has an invalid schema version") from exc
                if version < 1 or version > SCHEMA_VERSION:
                    raise ProcessingError(
                        f"Unsupported workspace schema {version}; expected at most "
                        f"{SCHEMA_VERSION}."
                    )
            connection.executescript(
                """
                PRAGMA journal_mode = WAL;
                CREATE TABLE IF NOT EXISTS metadata (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS sources (
                    id TEXT PRIMARY KEY,
                    ordinal INTEGER NOT NULL,
                    descriptor_json TEXT NOT NULL,
                    content_hash TEXT,
                    source_dir TEXT,
                    media_path TEXT,
                    caption_paths_json TEXT NOT NULL DEFAULT '[]',
                    active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0, 1)),
                    removed_at TEXT,
                    removal_reason TEXT,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS stages (
                    source_id TEXT NOT NULL,
                    stage TEXT NOT NULL,
                    state TEXT NOT NULL,
                    cache_key TEXT NOT NULL,
                    error TEXT,
                    started_at TEXT,
                    completed_at TEXT,
                    PRIMARY KEY (source_id, stage),
                    FOREIGN KEY (source_id) REFERENCES sources(id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS transcript_segments (
                    id TEXT PRIMARY KEY,
                    source_id TEXT NOT NULL,
                    start REAL NOT NULL,
                    end REAL NOT NULL,
                    data_json TEXT NOT NULL,
                    FOREIGN KEY (source_id) REFERENCES sources(id) ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS transcript_source_time
                    ON transcript_segments(source_id, start, end);
                CREATE VIRTUAL TABLE IF NOT EXISTS transcript_fts USING fts5(
                    id UNINDEXED,
                    source_id UNINDEXED,
                    text,
                    tokenize='unicode61'
                );
                CREATE TABLE IF NOT EXISTS visual_events (
                    id TEXT PRIMARY KEY,
                    source_id TEXT NOT NULL,
                    timestamp REAL NOT NULL,
                    origin TEXT NOT NULL DEFAULT 'baseline',
                    data_json TEXT NOT NULL,
                    FOREIGN KEY (source_id) REFERENCES sources(id) ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS visual_source_time
                    ON visual_events(source_id, timestamp);
                CREATE TABLE IF NOT EXISTS semantic_segments (
                    id TEXT PRIMARY KEY,
                    source_id TEXT NOT NULL,
                    ordinal INTEGER NOT NULL,
                    start REAL NOT NULL,
                    end REAL NOT NULL,
                    title TEXT NOT NULL,
                    data_json TEXT NOT NULL,
                    FOREIGN KEY (source_id) REFERENCES sources(id) ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS semantic_source_ordinal
                    ON semantic_segments(source_id, ordinal);
                CREATE TABLE IF NOT EXISTS warnings (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    code TEXT NOT NULL,
                    source_id TEXT,
                    data_json TEXT NOT NULL
                );
                """
            )
            connection.executescript(_AGENT_EVIDENCE_SCHEMA)
            connection.executescript(_INSPECTION_SCHEMA)
            connection.executescript(_ORCHESTRATION_SCHEMA)
            if existing_version is None:
                connection.execute(
                    "INSERT INTO metadata(key, value) VALUES('schema_version', ?)",
                    (str(SCHEMA_VERSION),),
                )
            else:
                self._upgrade_schema(connection, int(existing_version["value"]))
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS visual_source_origin_time
                ON visual_events(source_id, origin, timestamp)
                """
            )
            connection.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS work_item_execution_context
                ON work_items(execution_context_id)
                WHERE execution_context_id IS NOT NULL
                """
            )

    @staticmethod
    def _ensure_column(
        connection: sqlite3.Connection,
        *,
        table: str,
        column: str,
        declaration: str,
    ) -> None:
        columns = {
            str(row["name"]) for row in connection.execute(f"PRAGMA table_info({table})").fetchall()
        }
        if column not in columns:
            connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {declaration}")

    def _upgrade_schema(self, connection: sqlite3.Connection, version: int) -> None:
        if version < 2:
            connection.executescript(_AGENT_EVIDENCE_SCHEMA)
        if version < 3:
            self._ensure_column(
                connection,
                table="sources",
                column="active",
                declaration="INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0, 1))",
            )
            self._ensure_column(
                connection,
                table="sources",
                column="removed_at",
                declaration="TEXT",
            )
            self._ensure_column(
                connection,
                table="sources",
                column="removal_reason",
                declaration="TEXT",
            )
            connection.executescript(_INSPECTION_SCHEMA)
        if version < 4:
            self._ensure_column(
                connection,
                table="visual_events",
                column="origin",
                declaration="TEXT NOT NULL DEFAULT 'baseline'",
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS visual_source_origin_time
                ON visual_events(source_id, origin, timestamp)
                """
            )
        if version < 5:
            connection.executescript(_ORCHESTRATION_SCHEMA)
        if version < 6:
            self._ensure_column(
                connection,
                table="work_items",
                column="execution_context_id",
                declaration="TEXT",
            )
        connection.execute(
            "UPDATE metadata SET value=? WHERE key='schema_version'",
            (str(SCHEMA_VERSION),),
        )

    def _check_schema(self) -> None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT value FROM metadata WHERE key='schema_version'"
            ).fetchone()
            if row is None:
                raise ProcessingError("Workspace schema version is missing")
            try:
                version = int(row["value"])
            except (TypeError, ValueError) as exc:
                raise ProcessingError("Workspace has an invalid schema version") from exc
            if 1 <= version < SCHEMA_VERSION:
                self._upgrade_schema(connection, version)
                version = SCHEMA_VERSION
        if version != SCHEMA_VERSION:
            raise ProcessingError(
                f"Unsupported workspace schema {version}; expected {SCHEMA_VERSION}. "
                "Upgrade video-to-skill or create a fresh workspace."
            )

    def load_manifest(self) -> JobManifest:
        try:
            data = json.loads(self.manifest_path.read_text(encoding="utf-8"))
            return JobManifest.model_validate(data)
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            raise ProcessingError(f"Invalid workspace manifest: {exc}") from exc

    def save_manifest(self, manifest: JobManifest) -> None:
        manifest.updated_at = datetime.now(UTC)
        atomic_write_json(self.manifest_path, manifest)

    def workspace_snapshot_digest(self) -> str:
        """Return a compact identity for task inputs without loading evidence bodies."""

        manifest = self.load_manifest()
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT id, active, content_hash, updated_at
                FROM sources
                ORDER BY ordinal, id
                """
            ).fetchall()
            stage_rows = connection.execute(
                """
                SELECT source_id, stage, state, cache_key, completed_at
                FROM stages
                ORDER BY source_id, stage
                """
            ).fetchall()
        return stable_hash(
            {
                "job_id": manifest.job_id,
                "configuration": manifest.configuration_hash,
                "sources": [dict(row) for row in rows],
                "stages": [dict(row) for row in stage_rows],
            }
        )

    def create_analysis_run(self, snapshot_digest: str | None = None) -> AnalysisRun:
        snapshot = snapshot_digest or self.workspace_snapshot_digest()
        run_id = f"run-{stable_hash({'job': self.load_manifest().job_id, 'snapshot': snapshot}, length=24)}"
        now = datetime.now(UTC)
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO analysis_runs(id, snapshot_digest, created_at, updated_at)
                VALUES(?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET updated_at=excluded.updated_at
                """,
                (run_id, snapshot, now.isoformat(), now.isoformat()),
            )
            row = connection.execute(
                "SELECT * FROM analysis_runs WHERE id=?",
                (run_id,),
            ).fetchone()
        assert row is not None
        return AnalysisRun(
            id=str(row["id"]),
            snapshot_digest=str(row["snapshot_digest"]),
            created_at=datetime.fromisoformat(str(row["created_at"])),
            updated_at=datetime.fromisoformat(str(row["updated_at"])),
        )

    @staticmethod
    def _row_to_work_item(
        row: sqlite3.Row,
        dependencies: list[str],
    ) -> WorkItem:
        return WorkItem(
            id=str(row["id"]),
            run_id=str(row["run_id"]),
            role=WorkRole(str(row["role"])),
            scope=json.loads(str(row["scope_json"])),
            persona_hint=str(row["persona_hint"]),
            packet_path=Path(str(row["packet_path"])),
            packet_digest=str(row["packet_digest"]),
            result_schema_path=Path(str(row["result_schema_path"])),
            snapshot_digest=str(row["snapshot_digest"]),
            state=WorkState(str(row["state"])),
            dependencies=dependencies,
            lease_owner=cast(str | None, row["lease_owner"]),
            lease_expires_at=(
                datetime.fromisoformat(str(row["lease_expires_at"]))
                if row["lease_expires_at"]
                else None
            ),
            execution_context_id=cast(str | None, row["execution_context_id"]),
            attempt_count=int(row["attempt_count"]),
            result_path=Path(str(row["result_path"])) if row["result_path"] else None,
            result_digest=cast(str | None, row["result_digest"]),
            failure_reason=cast(str | None, row["failure_reason"]),
            created_at=datetime.fromisoformat(str(row["created_at"])),
            updated_at=datetime.fromisoformat(str(row["updated_at"])),
        )

    def get_work_item(self, task_id: str) -> WorkItem:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM work_items WHERE id=?",
                (task_id,),
            ).fetchone()
            if row is None:
                raise ProcessingError(f"Unknown workspace task: {task_id}")
            dependencies = [
                str(item["dependency_id"])
                for item in connection.execute(
                    """
                    SELECT dependency_id
                    FROM work_item_dependencies
                    WHERE task_id=?
                    ORDER BY dependency_id
                    """,
                    (task_id,),
                ).fetchall()
            ]
        return self._row_to_work_item(row, dependencies)

    def list_work_items(
        self,
        run_id: str,
        *,
        role: WorkRole | None = None,
        state: WorkState | None = None,
    ) -> list[WorkItem]:
        clauses = ["run_id=?"]
        parameters: list[object] = [run_id]
        if role is not None:
            clauses.append("role=?")
            parameters.append(role.value)
        if state is not None:
            clauses.append("state=?")
            parameters.append(state.value)
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM work_items WHERE "
                + " AND ".join(clauses)
                + " ORDER BY created_at, id",
                parameters,
            ).fetchall()
            result: list[WorkItem] = []
            for row in rows:
                dependencies = [
                    str(item["dependency_id"])
                    for item in connection.execute(
                        """
                        SELECT dependency_id FROM work_item_dependencies
                        WHERE task_id=? ORDER BY dependency_id
                        """,
                        (row["id"],),
                    ).fetchall()
                ]
                result.append(self._row_to_work_item(row, dependencies))
        return result

    def ensure_work_item(
        self,
        *,
        run_id: str,
        role: WorkRole,
        scope: dict[str, Any],
        persona_hint: str,
        packet: dict[str, Any],
        result_schema: dict[str, Any],
        dependencies: Iterable[str] = (),
        snapshot_digest: str | None = None,
    ) -> WorkItem:
        dependency_ids = sorted(set(dependencies))
        snapshot = snapshot_digest or self.workspace_snapshot_digest()
        task_id = f"task-{role.value}-{stable_hash({'run': run_id, 'role': role, 'scope': scope, 'dependencies': dependency_ids, 'snapshot': snapshot}, length=20)}"
        task_directory = self.tasks_dir / task_id
        packet_path = task_directory / "packet.json"
        result_schema_path = task_directory / "result-schema.json"
        output_directory = task_directory / "output"
        task_directory.mkdir(parents=True, exist_ok=True)
        output_directory.mkdir(parents=True, exist_ok=True)
        if any(path.is_symlink() for path in (task_directory, output_directory)):
            raise ProcessingError(f"Task directory cannot be a symlink: {task_directory}")
        packet_payload = {
            "task_id": task_id,
            "run_id": run_id,
            "role": role.value,
            "scope": scope,
            "snapshot_digest": snapshot,
            "dependencies": dependency_ids,
            "output_directory": str(output_directory),
            "payload": packet,
        }
        schema_payload = {
            "task_id": task_id,
            "role": role.value,
            "snapshot_digest": snapshot,
            "schema": result_schema,
        }
        packet_digest = stable_hash(packet_payload, length=64)
        atomic_write_json(packet_path, packet_payload)
        atomic_write_json(result_schema_path, schema_payload)
        now = datetime.now(UTC)
        with self.connect() as connection:
            if (
                connection.execute(
                    "SELECT 1 FROM analysis_runs WHERE id=?",
                    (run_id,),
                ).fetchone()
                is None
            ):
                raise ProcessingError(f"Unknown analysis run: {run_id}")
            missing_dependencies = [
                dependency
                for dependency in dependency_ids
                if connection.execute(
                    "SELECT 1 FROM work_items WHERE id=? AND run_id=?",
                    (dependency, run_id),
                ).fetchone()
                is None
            ]
            if missing_dependencies:
                raise ProcessingError(
                    "Task dependencies are unknown or belong to another run: "
                    + ", ".join(missing_dependencies)
                )
            connection.execute(
                """
                INSERT INTO work_items(
                    id, run_id, role, scope_json, persona_hint, packet_path,
                    packet_digest, result_schema_path, snapshot_digest, state,
                    created_at, updated_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)
                ON CONFLICT(id) DO NOTHING
                """,
                (
                    task_id,
                    run_id,
                    role.value,
                    json.dumps(scope, ensure_ascii=False, sort_keys=True),
                    " ".join(persona_hint.split()),
                    str(packet_path.relative_to(self.root)),
                    packet_digest,
                    str(result_schema_path.relative_to(self.root)),
                    snapshot,
                    now.isoformat(),
                    now.isoformat(),
                ),
            )
            for dependency in dependency_ids:
                connection.execute(
                    """
                    INSERT OR IGNORE INTO work_item_dependencies(task_id, dependency_id)
                    VALUES(?, ?)
                    """,
                    (task_id, dependency),
                )
        atomic_write_json(
            task_directory / "task.json",
            {
                "task_id": task_id,
                "role": role.value,
                "persona_hint": " ".join(persona_hint.split()),
                "packet_path": str(packet_path),
                "result_schema_path": str(result_schema_path),
                "output_directory": str(output_directory),
            },
        )
        return self.get_work_item(task_id)

    def ready_work_items(self, run_id: str, *, role: WorkRole | None = None) -> list[WorkItem]:
        now = datetime.now(UTC).isoformat()
        with self.connect() as connection:
            connection.execute(
                """
                UPDATE work_items
                SET state='pending', lease_token_hash=NULL, lease_owner=NULL,
                    lease_expires_at=NULL, execution_context_id=NULL, updated_at=?
                WHERE run_id=? AND state='leased' AND lease_expires_at <= ?
                """,
                (now, run_id, now),
            )
            parameters: list[object] = [run_id]
            role_clause = ""
            if role is not None:
                role_clause = "AND item.role=?"
                parameters.append(role.value)
            rows = connection.execute(
                f"""
                SELECT item.*
                FROM work_items AS item
                WHERE item.run_id=? AND item.state='pending' {role_clause}
                  AND NOT EXISTS (
                    SELECT 1
                    FROM work_item_dependencies AS dependency
                    JOIN work_items AS prerequisite
                      ON prerequisite.id=dependency.dependency_id
                    WHERE dependency.task_id=item.id
                      AND prerequisite.state!='complete'
                  )
                ORDER BY item.created_at, item.id
                """,
                parameters,
            ).fetchall()
            result: list[WorkItem] = []
            for row in rows:
                dependencies = [
                    str(item["dependency_id"])
                    for item in connection.execute(
                        """
                        SELECT dependency_id FROM work_item_dependencies
                        WHERE task_id=? ORDER BY dependency_id
                        """,
                        (row["id"],),
                    ).fetchall()
                ]
                result.append(self._row_to_work_item(row, dependencies))
        return result

    def lease_work_item(
        self,
        task_id: str,
        *,
        owner: str,
        lease_seconds: int = 3600,
    ) -> WorkLease:
        if lease_seconds < 1:
            raise ProcessingError("Task lease duration must be positive")
        token = secrets.token_urlsafe(32)
        execution_context_id = f"ctx-{secrets.token_urlsafe(24)}"
        token_hash = sha256(token.encode("utf-8")).hexdigest()
        now = datetime.now(UTC)
        expires = now + timedelta(seconds=lease_seconds)
        with self.connect() as connection:
            row = connection.execute(
                "SELECT run_id, state, lease_expires_at FROM work_items WHERE id=?",
                (task_id,),
            ).fetchone()
            if row is None:
                raise ProcessingError(f"Unknown workspace task: {task_id}")
            if row["state"] == WorkState.LEASED.value and (
                row["lease_expires_at"] is None
                or datetime.fromisoformat(str(row["lease_expires_at"])) > now
            ):
                raise ProcessingError(f"Task is already leased: {task_id}")
            if row["state"] not in {WorkState.PENDING.value, WorkState.LEASED.value}:
                raise ProcessingError(f"Task cannot be leased from state {row['state']}: {task_id}")
            blocked = connection.execute(
                """
                SELECT 1
                FROM work_item_dependencies AS dependency
                JOIN work_items AS prerequisite ON prerequisite.id=dependency.dependency_id
                WHERE dependency.task_id=? AND prerequisite.state!='complete'
                LIMIT 1
                """,
                (task_id,),
            ).fetchone()
            if blocked is not None:
                raise ProcessingError(f"Task dependencies are incomplete: {task_id}")
            connection.execute(
                """
                UPDATE work_items
                SET state='leased', lease_token_hash=?, lease_owner=?,
                    lease_expires_at=?, execution_context_id=?,
                    attempt_count=attempt_count + 1, updated_at=?
                WHERE id=?
                """,
                (
                    token_hash,
                    owner,
                    expires.isoformat(),
                    execution_context_id,
                    now.isoformat(),
                    task_id,
                ),
            )
        task_directory = self.tasks_dir / task_id
        atomic_write_json(
            task_directory / "lease.json",
            {
                "task_id": task_id,
                "lease_token": token,
                "execution_context_id": execution_context_id,
                "owner": owner,
                "expires_at": expires.isoformat(),
            },
        )
        return WorkLease(
            item=self.get_work_item(task_id),
            token=token,
            execution_context_id=execution_context_id,
            output_directory=task_directory / "output",
        )

    def fail_work_item(self, task_id: str, reason: str) -> WorkItem:
        now = datetime.now(UTC).isoformat()
        with self.connect() as connection:
            updated = connection.execute(
                """
                UPDATE work_items
                SET state='failed', failure_reason=?, lease_token_hash=NULL,
                    lease_owner=NULL, lease_expires_at=NULL, execution_context_id=NULL,
                    updated_at=?
                WHERE id=? AND state IN ('pending', 'leased')
                """,
                (" ".join(reason.split())[:2000], now, task_id),
            ).rowcount
            if updated != 1:
                raise ProcessingError(f"Task cannot be failed from its current state: {task_id}")
        return self.get_work_item(task_id)

    def work_result_producer(self, task_id: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT producer_json FROM work_results WHERE task_id=?",
                (task_id,),
            ).fetchone()
        if row is None:
            return None
        value = json.loads(str(row["producer_json"]))
        return cast(dict[str, Any], value)

    def publish_canonical_record(
        self,
        *,
        kind: str,
        record_id: str,
        source_path: Path,
        producer_task_id: str,
        snapshot_digest: str,
    ) -> CanonicalRecord:
        source = source_path.resolve()
        if not source.is_file() or source.is_symlink() or not is_within(source, self.root):
            raise ProcessingError("Canonical record source must be a regular workspace file")
        now = datetime.now(UTC)
        with self.connect() as connection:
            task = connection.execute(
                "SELECT state FROM work_items WHERE id=?",
                (producer_task_id,),
            ).fetchone()
            if task is None:
                raise ProcessingError(f"Unknown producer task: {producer_task_id}")
            head = connection.execute(
                """
                SELECT revision FROM canonical_heads
                WHERE kind=? AND record_id=?
                """,
                (kind, record_id),
            ).fetchone()
            revision = (int(head["revision"]) + 1) if head is not None else 1
            suffix = source.suffix if source.suffix in {".json", ".md"} else ".data"
            record_directory = (
                self.analysis_dir
                / "records"
                / stable_hash(kind, length=20)
                / stable_hash(record_id, length=20)
            )
            destination = record_directory / f"r{revision}{suffix}"
            destination.parent.mkdir(parents=True, exist_ok=True)
            if destination.exists():
                raise ProcessingError(f"Canonical record revision already exists: {destination}")
            temporary = destination.with_name(f".{destination.name}.{secrets.token_hex(8)}.tmp")
            try:
                shutil.copyfile(source, temporary)
                temporary.replace(destination)
            finally:
                temporary.unlink(missing_ok=True)
            digest = hash_file(destination)
            relative = destination.relative_to(self.root)
            connection.execute(
                """
                INSERT INTO canonical_records(
                    kind, record_id, revision, path, digest, producer_task_id,
                    snapshot_digest, created_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    kind,
                    record_id,
                    revision,
                    str(relative),
                    digest,
                    producer_task_id,
                    snapshot_digest,
                    now.isoformat(),
                ),
            )
            connection.execute(
                """
                INSERT INTO canonical_heads(kind, record_id, revision)
                VALUES(?, ?, ?)
                ON CONFLICT(kind, record_id) DO UPDATE SET revision=excluded.revision
                """,
                (kind, record_id, revision),
            )
        return CanonicalRecord(
            kind=kind,
            record_id=record_id,
            revision=revision,
            path=relative,
            digest=digest,
            producer_task_id=producer_task_id,
            snapshot_digest=snapshot_digest,
            created_at=now,
        )

    def accept_work_result(
        self,
        *,
        task_id: str,
        lease_token: str,
        result_path: Path,
        producer: dict[str, Any],
        canonical_outputs: Iterable[tuple[str, str, Path]] = (),
    ) -> tuple[WorkItem, list[CanonicalRecord]]:
        """Atomically accept a validated result and advance canonical record heads."""

        item = self.get_work_item(task_id)
        result = result_path.resolve()
        task_output = (self.tasks_dir / task_id / "output").resolve()
        if (
            not result.is_file()
            or result.is_symlink()
            or not is_within(result, task_output)
            or any(parent.is_symlink() for parent in [result, *result.parents[:2]])
        ):
            raise ProcessingError("Task result must be a regular file in its task output directory")
        result_digest = hash_file(result)
        now = datetime.now(UTC)
        if item.state == WorkState.COMPLETE:
            if item.result_digest == result_digest:
                return item, []
            raise ProcessingError(f"Task already completed with a different result: {task_id}")
        if item.state != WorkState.LEASED:
            raise ProcessingError(f"Task is not leased: {task_id}")
        if item.lease_expires_at is None or item.lease_expires_at <= now:
            raise ProcessingError(f"Task lease has expired: {task_id}")
        if self.workspace_snapshot_digest() != item.snapshot_digest:
            raise ProcessingError(f"Workspace snapshot changed while task was leased: {task_id}")
        output_specs = list(canonical_outputs)
        for _kind, _record_id, source_path in output_specs:
            source = source_path.resolve()
            if not source.is_file() or source.is_symlink() or not is_within(source, task_output):
                raise ProcessingError(
                    "Canonical outputs must be regular files in the task output directory"
                )
        token_hash = sha256(lease_token.encode("utf-8")).hexdigest()
        accepted_result = self.analysis_dir / "results" / f"{task_id}.json"
        accepted_result.parent.mkdir(parents=True, exist_ok=True)
        temporary_result = accepted_result.with_name(
            f".{accepted_result.name}.{secrets.token_hex(8)}.tmp"
        )
        prepared_records: list[CanonicalRecord] = []
        prepared_paths: list[Path] = []
        try:
            shutil.copyfile(result, temporary_result)
            temporary_result.replace(accepted_result)
            with self.connect() as connection:
                row = connection.execute(
                    """
                    SELECT state, lease_token_hash, lease_expires_at, snapshot_digest
                    FROM work_items WHERE id=?
                    """,
                    (task_id,),
                ).fetchone()
                if row is None:
                    raise ProcessingError(f"Unknown workspace task: {task_id}")
                if row["state"] != WorkState.LEASED.value:
                    raise ProcessingError(f"Task is not leased: {task_id}")
                if not secrets.compare_digest(str(row["lease_token_hash"] or ""), token_hash):
                    raise ProcessingError(f"Task lease token is invalid: {task_id}")
                expires = datetime.fromisoformat(str(row["lease_expires_at"]))
                if expires <= now:
                    raise ProcessingError(f"Task lease has expired: {task_id}")
                for kind, record_id, source_path in output_specs:
                    head = connection.execute(
                        """
                        SELECT revision FROM canonical_heads
                        WHERE kind=? AND record_id=?
                        """,
                        (kind, record_id),
                    ).fetchone()
                    revision = int(head["revision"]) + 1 if head is not None else 1
                    suffix = (
                        source_path.suffix
                        if source_path.suffix in {".json", ".md", ".png"}
                        else ".data"
                    )
                    destination = (
                        self.analysis_dir
                        / "records"
                        / stable_hash(kind, length=20)
                        / stable_hash(record_id, length=20)
                        / f"r{revision}{suffix}"
                    )
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    if destination.exists():
                        raise ProcessingError(
                            f"Canonical record revision already exists: {destination}"
                        )
                    temporary = destination.with_name(
                        f".{destination.name}.{secrets.token_hex(8)}.tmp"
                    )
                    try:
                        shutil.copyfile(source_path, temporary)
                        temporary.replace(destination)
                    finally:
                        temporary.unlink(missing_ok=True)
                    prepared_paths.append(destination)
                    record = CanonicalRecord(
                        kind=kind,
                        record_id=record_id,
                        revision=revision,
                        path=destination.relative_to(self.root),
                        digest=hash_file(destination),
                        producer_task_id=task_id,
                        snapshot_digest=item.snapshot_digest,
                        created_at=now,
                    )
                    connection.execute(
                        """
                        INSERT INTO canonical_records(
                            kind, record_id, revision, path, digest, producer_task_id,
                            snapshot_digest, created_at
                        ) VALUES(?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            record.kind,
                            record.record_id,
                            record.revision,
                            str(record.path),
                            record.digest,
                            record.producer_task_id,
                            record.snapshot_digest,
                            record.created_at.isoformat(),
                        ),
                    )
                    connection.execute(
                        """
                        INSERT INTO canonical_heads(kind, record_id, revision)
                        VALUES(?, ?, ?)
                        ON CONFLICT(kind, record_id) DO UPDATE SET revision=excluded.revision
                        """,
                        (record.kind, record.record_id, record.revision),
                    )
                    prepared_records.append(record)
                connection.execute(
                    """
                    INSERT INTO work_results(
                        task_id, result_path, result_digest, producer_json, created_at
                    ) VALUES(?, ?, ?, ?, ?)
                    """,
                    (
                        task_id,
                        str(accepted_result.relative_to(self.root)),
                        result_digest,
                        json.dumps(producer, ensure_ascii=False, sort_keys=True),
                        now.isoformat(),
                    ),
                )
                connection.execute(
                    """
                    UPDATE work_items
                    SET state='complete', result_path=?, result_digest=?,
                        lease_token_hash=NULL, lease_owner=NULL, lease_expires_at=NULL,
                        failure_reason=NULL, updated_at=?
                    WHERE id=?
                    """,
                    (
                        str(accepted_result.relative_to(self.root)),
                        result_digest,
                        now.isoformat(),
                        task_id,
                    ),
                )
        except Exception:
            for path in prepared_paths:
                path.unlink(missing_ok=True)
            accepted_result.unlink(missing_ok=True)
            raise
        finally:
            temporary_result.unlink(missing_ok=True)
        return self.get_work_item(task_id), prepared_records

    def canonical_record(self, kind: str, record_id: str = "default") -> CanonicalRecord | None:
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT record.*
                FROM canonical_heads AS head
                JOIN canonical_records AS record
                  ON record.kind=head.kind
                 AND record.record_id=head.record_id
                 AND record.revision=head.revision
                WHERE head.kind=? AND head.record_id=?
                """,
                (kind, record_id),
            ).fetchone()
        if row is None:
            return None
        return CanonicalRecord(
            kind=str(row["kind"]),
            record_id=str(row["record_id"]),
            revision=int(row["revision"]),
            path=Path(str(row["path"])),
            digest=str(row["digest"]),
            producer_task_id=str(row["producer_task_id"]),
            snapshot_digest=str(row["snapshot_digest"]),
            created_at=datetime.fromisoformat(str(row["created_at"])),
        )

    def set_job_state(
        self,
        state: JobState,
        *,
        failed_sources: list[str] | None = None,
    ) -> JobManifest:
        manifest = self.load_manifest()
        manifest.state = state
        if failed_sources is not None:
            manifest.failed_sources = failed_sources
        manifest.sources = self.list_sources()
        manifest.warnings = self.list_warnings()
        self.save_manifest(manifest)
        return manifest

    def upsert_sources(
        self,
        sources: Iterable[SourceDescriptor],
        *,
        prune: bool = False,
        retirement_reason: str = "not present in the latest successful source inspection",
    ) -> None:
        """Upsert active sources and tombstone, never delete, missing sources on prune."""

        now = datetime.now(UTC).isoformat()
        source_list = list(sources)
        source_ids = [source.id for source in source_list]
        if len(source_ids) != len(set(source_ids)):
            raise ProcessingError("A source batch cannot contain duplicate ids")
        with self.connect() as connection:
            if prune:
                if source_ids:
                    placeholders = ",".join("?" for _ in source_ids)
                    connection.execute(
                        f"""
                        UPDATE sources
                        SET active=0, removed_at=?, removal_reason=?, updated_at=?
                        WHERE active=1 AND id NOT IN ({placeholders})
                        """,
                        (now, retirement_reason, now, *source_ids),
                    )
                else:
                    connection.execute(
                        """
                        UPDATE sources
                        SET active=0, removed_at=?, removal_reason=?, updated_at=?
                        WHERE active=1
                        """,
                        (now, retirement_reason, now),
                    )
            for ordinal, source in enumerate(source_list, start=1):
                connection.execute(
                    """
                    INSERT INTO sources(
                        id, ordinal, descriptor_json, active, removed_at,
                        removal_reason, updated_at
                    )
                    VALUES(?, ?, ?, 1, NULL, NULL, ?)
                    ON CONFLICT(id) DO UPDATE SET
                        ordinal=excluded.ordinal,
                        descriptor_json=excluded.descriptor_json,
                        active=1,
                        removed_at=NULL,
                        removal_reason=NULL,
                        updated_at=excluded.updated_at
                    """,
                    (source.id, ordinal, source.model_dump_json(), now),
                )

    def list_sources(self, *, include_inactive: bool = False) -> list[SourceDescriptor]:
        where = "" if include_inactive else " WHERE active=1"
        with self.connect() as connection:
            rows = connection.execute(
                f"SELECT descriptor_json FROM sources{where} ORDER BY active DESC, ordinal, id"
            ).fetchall()
        return [SourceDescriptor.model_validate_json(row["descriptor_json"]) for row in rows]

    def list_retired_sources(self) -> list[RetiredSource]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT descriptor_json, removed_at, removal_reason
                FROM sources
                WHERE active=0
                ORDER BY removed_at, ordinal, id
                """
            ).fetchall()
        return [
            RetiredSource(
                source=SourceDescriptor.model_validate_json(row["descriptor_json"]),
                removed_at=datetime.fromisoformat(row["removed_at"]),
                reason=str(row["removal_reason"]),
            )
            for row in rows
            if row["removed_at"] is not None and row["removal_reason"] is not None
        ]

    def get_source(self, source_id: str) -> SourceDescriptor:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT descriptor_json FROM sources WHERE id=?", (source_id,)
            ).fetchone()
        if row is None:
            raise ProcessingError(f"Unknown source id: {source_id}")
        return SourceDescriptor.model_validate_json(row["descriptor_json"])

    def replace_inspection_reports(
        self,
        reports: Iterable[InspectionCompleteness],
    ) -> None:
        """Persist the latest complete accounting for each inspected input."""

        items = list(reports)
        locators = [item.locator for item in items]
        if len(locators) != len(set(locators)):
            raise ProcessingError("An inspection batch cannot contain duplicate locators")
        now = datetime.now(UTC).isoformat()
        with self.connect() as connection:
            connection.executemany(
                """
                INSERT INTO inspection_reports(locator, platform, report_json, updated_at)
                VALUES(?, ?, ?, ?)
                ON CONFLICT(locator) DO UPDATE SET
                    platform=excluded.platform,
                    report_json=excluded.report_json,
                    updated_at=excluded.updated_at
                """,
                [
                    (
                        item.locator,
                        item.platform.value,
                        item.model_dump_json(),
                        now,
                    )
                    for item in items
                ],
            )

    def inspection_reports(self) -> list[InspectionCompleteness]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT report_json FROM inspection_reports ORDER BY locator"
            ).fetchall()
        return [InspectionCompleteness.model_validate_json(row["report_json"]) for row in rows]

    def update_materialization(
        self,
        source_id: str,
        *,
        content_hash: str,
        source_dir: Path,
        media_path: Path | None,
        caption_paths: list[Path],
    ) -> None:
        paths = [str(path.resolve()) for path in caption_paths]
        with self.connect() as connection:
            connection.execute(
                """
                UPDATE sources SET content_hash=?, source_dir=?, media_path=?,
                    caption_paths_json=?, updated_at=?
                WHERE id=?
                """,
                (
                    content_hash,
                    str(source_dir.resolve()),
                    str(media_path.resolve()) if media_path else None,
                    json.dumps(paths),
                    datetime.now(UTC).isoformat(),
                    source_id,
                ),
            )

    def materialization_record(self, source_id: str) -> sqlite3.Row | None:
        with self.connect() as connection:
            return cast(
                sqlite3.Row | None,
                connection.execute(
                    """
                    SELECT content_hash, source_dir, media_path, caption_paths_json
                    FROM sources WHERE id=?
                    """,
                    (source_id,),
                ).fetchone(),
            )

    def stage_complete(self, source_id: str, stage: ProcessingStage, cache_key: str) -> bool:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT state, cache_key FROM stages WHERE source_id=? AND stage=?",
                (source_id, stage.value),
            ).fetchone()
        return bool(row and row["state"] == "complete" and row["cache_key"] == cache_key)

    def start_stage(self, source_id: str, stage: ProcessingStage, cache_key: str) -> None:
        with self.connect() as connection:
            warning_rows = connection.execute(
                "SELECT id, data_json FROM warnings WHERE source_id=?", (source_id,)
            ).fetchall()
            stale_warning_ids: list[int] = []
            for row in warning_rows:
                try:
                    warning = WarningRecord.model_validate_json(row["data_json"])
                except ValueError:
                    continue
                if warning.stage == stage:
                    stale_warning_ids.append(int(row["id"]))
            if stale_warning_ids:
                placeholders = ",".join("?" for _ in stale_warning_ids)
                connection.execute(
                    f"DELETE FROM warnings WHERE id IN ({placeholders})",
                    stale_warning_ids,
                )
            connection.execute(
                """
                INSERT INTO stages(source_id, stage, state, cache_key, started_at)
                VALUES(?, ?, 'running', ?, ?)
                ON CONFLICT(source_id, stage) DO UPDATE SET
                    state='running',
                    cache_key=excluded.cache_key,
                    error=NULL,
                    started_at=excluded.started_at,
                    completed_at=NULL
                """,
                (source_id, stage.value, cache_key, datetime.now(UTC).isoformat()),
            )

    def complete_stage(self, source_id: str, stage: ProcessingStage, cache_key: str) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                UPDATE stages SET state='complete', cache_key=?, error=NULL, completed_at=?
                WHERE source_id=? AND stage=?
                """,
                (cache_key, datetime.now(UTC).isoformat(), source_id, stage.value),
            )

    def fail_stage(
        self, source_id: str, stage: ProcessingStage, cache_key: str, error: str
    ) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO stages(source_id, stage, state, cache_key, error, completed_at)
                VALUES(?, ?, 'failed', ?, ?, ?)
                ON CONFLICT(source_id, stage) DO UPDATE SET
                    state='failed',
                    cache_key=excluded.cache_key,
                    error=excluded.error,
                    completed_at=excluded.completed_at
                """,
                (
                    source_id,
                    stage.value,
                    cache_key,
                    error[-4000:],
                    datetime.now(UTC).isoformat(),
                ),
            )

    def replace_transcripts(self, source_id: str, segments: Iterable[TranscriptSegment]) -> None:
        items = list(segments)
        identifiers = [item.id for item in items]
        if len(identifiers) != len(set(identifiers)):
            raise ProcessingError("A transcript batch cannot contain duplicate ids")
        if any(item.source_id != source_id for item in items):
            raise ProcessingError("Replacement transcripts must all belong to the source")
        with self.connect() as connection:
            self._ensure_sources_exist(connection, [source_id])
            self._ensure_ids_keep_source(
                connection,
                table="transcript_segments",
                items=items,
            )
            old_ids = [
                row["id"]
                for row in connection.execute(
                    "SELECT id FROM transcript_segments WHERE source_id=?", (source_id,)
                )
            ]
            connection.execute("DELETE FROM transcript_segments WHERE source_id=?", (source_id,))
            for old_id in old_ids:
                connection.execute("DELETE FROM transcript_fts WHERE id=?", (old_id,))
            for segment in items:
                connection.execute(
                    """
                    INSERT INTO transcript_segments(id, source_id, start, end, data_json)
                    VALUES(?, ?, ?, ?, ?)
                    """,
                    (
                        segment.id,
                        source_id,
                        segment.start,
                        segment.end,
                        segment.model_dump_json(),
                    ),
                )
                connection.execute(
                    "INSERT INTO transcript_fts(id, source_id, text) VALUES(?, ?, ?)",
                    (segment.id, source_id, segment.text),
                )

    def transcripts(
        self,
        source_id: str,
        *,
        start: float | None = None,
        end: float | None = None,
        search: str | None = None,
        limit: int = 500,
    ) -> list[TranscriptSegment]:
        params: list[object] = [source_id]
        where = ["t.source_id=?"]
        join = ""
        if search:
            join = " JOIN transcript_fts f ON f.id=t.id"
            where.append("f.text MATCH ?")
            params.append(search)
        if start is not None:
            where.append("t.end>=?")
            params.append(start)
        if end is not None:
            where.append("t.start<=?")
            params.append(end)
        params.append(limit)
        query = (
            "SELECT t.data_json FROM transcript_segments t"
            + join
            + " WHERE "
            + " AND ".join(where)
            + " ORDER BY t.start, t.end, t.id LIMIT ?"
        )
        try:
            with self.connect() as connection:
                rows = connection.execute(query, params).fetchall()
        except sqlite3.OperationalError as exc:
            if search:
                raise ProcessingError(f"Invalid full-text search query: {search}") from exc
            raise
        return [TranscriptSegment.model_validate_json(row["data_json"]) for row in rows]

    def replace_visuals(
        self,
        source_id: str,
        events: Iterable[VisualEvent],
        *,
        origin: VisualOrigin = VisualOrigin.BASELINE,
    ) -> None:
        """Replace one visual origin while retaining evidence from every other origin."""

        items = list(events)
        self._ensure_unique_ids(items)
        if any(item.source_id != source_id for item in items):
            raise ProcessingError("Replacement visuals must all belong to the source")
        if any(item.origin != origin for item in items):
            raise ProcessingError("Replacement visuals must all match the requested origin")
        with self.connect() as connection:
            self._ensure_sources_exist(connection, [source_id])
            self._ensure_ids_keep_source(
                connection,
                table="visual_events",
                items=items,
            )
            self._ensure_visual_ids_keep_origin(connection, items)
            connection.execute(
                "DELETE FROM visual_events WHERE source_id=? AND origin=?",
                (source_id, origin.value),
            )
            connection.executemany(
                """
                INSERT INTO visual_events(id, source_id, timestamp, origin, data_json)
                VALUES(?, ?, ?, ?, ?)
                """,
                [
                    (
                        event.id,
                        source_id,
                        event.timestamp,
                        event.origin.value,
                        event.model_dump_json(),
                    )
                    for event in items
                ],
            )

    def upsert_visuals(self, events: Iterable[VisualEvent]) -> None:
        """Atomically add or update frames without replacing baseline visual evidence."""

        items = list(events)
        self._ensure_unique_ids(items)
        with self.connect() as connection:
            self._ensure_sources_exist(connection, (item.source_id for item in items))
            self._ensure_ids_keep_source(
                connection,
                table="visual_events",
                items=items,
            )
            self._ensure_visual_ids_keep_origin(connection, items)
            connection.executemany(
                """
                INSERT INTO visual_events(id, source_id, timestamp, origin, data_json)
                VALUES(?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    timestamp=excluded.timestamp,
                    data_json=excluded.data_json
                """,
                [
                    (
                        event.id,
                        event.source_id,
                        event.timestamp,
                        event.origin.value,
                        event.model_dump_json(),
                    )
                    for event in items
                ],
            )

    def visuals(
        self,
        source_id: str,
        *,
        start: float | None = None,
        end: float | None = None,
        origin: VisualOrigin | None = None,
        limit: int | None = None,
    ) -> list[VisualEvent]:
        if limit is not None and limit < 1:
            return []
        params: list[object] = [source_id]
        where = ["source_id=?"]
        if start is not None:
            where.append("timestamp>=?")
            params.append(start)
        if end is not None:
            where.append("timestamp<=?")
            params.append(end)
        if origin is not None:
            where.append("origin=?")
            params.append(origin.value)
        query = (
            "SELECT data_json FROM visual_events WHERE "
            + " AND ".join(where)
            + " ORDER BY timestamp, id"
        )
        if limit is not None:
            query += " LIMIT ?"
            params.append(limit)
        with self.connect() as connection:
            rows = connection.execute(query, params).fetchall()
        return [VisualEvent.model_validate_json(row["data_json"]) for row in rows]

    def replace_semantic_segments(
        self, source_id: str, segments: Iterable[SemanticSegment]
    ) -> None:
        items = list(segments)
        identifiers = [item.id for item in items]
        if len(identifiers) != len(set(identifiers)):
            raise ProcessingError("A semantic-segment batch cannot contain duplicate ids")
        if any(item.source_id != source_id for item in items):
            raise ProcessingError("Replacement semantic segments must all belong to the source")
        with self.connect() as connection:
            self._ensure_sources_exist(connection, [source_id])
            self._ensure_ids_keep_source(
                connection,
                table="semantic_segments",
                items=items,
            )
            connection.execute("DELETE FROM semantic_segments WHERE source_id=?", (source_id,))
            connection.executemany(
                """
                INSERT INTO semantic_segments(
                    id, source_id, ordinal, start, end, title, data_json
                ) VALUES(?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        segment.id,
                        source_id,
                        segment.ordinal,
                        segment.start,
                        segment.end,
                        segment.title,
                        segment.model_dump_json(),
                    )
                    for segment in items
                ],
            )

    def semantic_segments(self, source_id: str) -> list[SemanticSegment]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT data_json FROM semantic_segments
                WHERE source_id=? ORDER BY ordinal
                """,
                (source_id,),
            ).fetchall()
        return [SemanticSegment.model_validate_json(row["data_json"]) for row in rows]

    def add_warning(self, warning: WarningRecord) -> None:
        with self.connect() as connection:
            duplicate = connection.execute(
                """
                SELECT 1 FROM warnings
                WHERE code=? AND source_id IS ? AND data_json=?
                """,
                (warning.code, warning.source_id, warning.model_dump_json()),
            ).fetchone()
            if duplicate:
                return
            connection.execute(
                "INSERT INTO warnings(code, source_id, data_json) VALUES(?, ?, ?)",
                (warning.code, warning.source_id, warning.model_dump_json()),
            )

    def list_warnings(self) -> list[WarningRecord]:
        with self.connect() as connection:
            rows = connection.execute("SELECT data_json FROM warnings ORDER BY id").fetchall()
        return [WarningRecord.model_validate_json(row["data_json"]) for row in rows]

    @staticmethod
    def _ensure_unique_ids(
        items: Iterable[_OwnedEvidence],
    ) -> None:
        identifiers = [item.id for item in items]
        if len(identifiers) != len(set(identifiers)):
            raise ProcessingError("An evidence batch cannot contain duplicate ids")

    @staticmethod
    def _ensure_sources_exist(
        connection: sqlite3.Connection,
        source_ids: Iterable[str],
    ) -> None:
        for source_id in sorted(set(source_ids)):
            exists = connection.execute(
                "SELECT 1 FROM sources WHERE id=?",
                (source_id,),
            ).fetchone()
            if exists is None:
                raise ProcessingError(f"Unknown source id: {source_id}")

    @staticmethod
    def _ensure_ids_keep_source(
        connection: sqlite3.Connection,
        *,
        table: str,
        items: Iterable[_OwnedEvidence],
    ) -> None:
        for item in items:
            row = connection.execute(
                f"SELECT source_id FROM {table} WHERE id=?",
                (item.id,),
            ).fetchone()
            if row is not None and row["source_id"] != item.source_id:
                raise ProcessingError(
                    f"Evidence id '{item.id}' already belongs to source '{row['source_id']}'"
                )

    @staticmethod
    def _ensure_visual_ids_keep_origin(
        connection: sqlite3.Connection,
        items: Iterable[VisualEvent],
    ) -> None:
        for item in items:
            row = connection.execute(
                "SELECT origin FROM visual_events WHERE id=?",
                (item.id,),
            ).fetchone()
            if row is not None and row["origin"] != item.origin.value:
                raise ProcessingError(
                    f"Visual id '{item.id}' already belongs to origin '{row['origin']}'"
                )

    @staticmethod
    def _reference_rows(
        connection: sqlite3.Connection,
        *,
        table: str,
        columns: str,
        source_id: str,
        identifiers: Iterable[str],
    ) -> dict[str, sqlite3.Row]:
        """Load only specifically referenced evidence, in SQLite-safe batches."""

        requested = sorted(set(identifiers))
        result: dict[str, sqlite3.Row] = {}
        for offset in range(0, len(requested), _SQL_REFERENCE_BATCH_SIZE):
            batch = requested[offset : offset + _SQL_REFERENCE_BATCH_SIZE]
            placeholders = ",".join("?" for _ in batch)
            rows = connection.execute(
                f"""
                SELECT id, {columns}
                FROM {table}
                WHERE source_id=? AND id IN ({placeholders})
                """,
                (source_id, *batch),
            ).fetchall()
            result.update((str(row["id"]), row) for row in rows)
        return result

    def _validate_observation_references(
        self,
        connection: sqlite3.Connection,
        observations: list[AgentObservation],
        neighborhood_seconds: float,
    ) -> None:
        source_ids = sorted({item.source_id for item in observations})
        sources: dict[str, SourceDescriptor] = {}
        transcript_ids: dict[str, set[str]] = {source_id: set() for source_id in source_ids}
        frame_ids: dict[str, set[str]] = {source_id: set() for source_id in source_ids}

        for source_id in source_ids:
            row = connection.execute(
                """
                SELECT descriptor_json
                FROM sources
                WHERE id=? AND active=1
                """,
                (source_id,),
            ).fetchone()
            if row is None:
                raise ProcessingError(f"Unknown source id: {source_id}")
            sources[source_id] = SourceDescriptor.model_validate_json(row["descriptor_json"])

        for observation in observations:
            transcript_ids[observation.source_id].update(observation.transcript_ids)
            frame_ids[observation.source_id].update(observation.frame_ids)

        transcript_rows = {
            source_id: self._reference_rows(
                connection,
                table="transcript_segments",
                columns="start, end",
                source_id=source_id,
                identifiers=transcript_ids[source_id],
            )
            for source_id in source_ids
        }
        visual_rows = {
            source_id: self._reference_rows(
                connection,
                table="visual_events",
                columns="timestamp",
                source_id=source_id,
                identifiers=frame_ids[source_id],
            )
            for source_id in source_ids
        }

        for observation in observations:
            source = sources[observation.source_id]
            if source.duration is not None and observation.end > source.duration + 1e-6:
                raise ProcessingError(
                    f"Observation '{observation.id}' ends at {observation.end:g}s, beyond "
                    f"source duration {source.duration:g}s"
                )
            lower = max(0.0, observation.start - neighborhood_seconds)
            upper = observation.end + neighborhood_seconds
            source_transcripts = transcript_rows[observation.source_id]
            source_visuals = visual_rows[observation.source_id]
            for transcript_id in observation.transcript_ids:
                transcript = source_transcripts.get(transcript_id)
                if transcript is None:
                    raise ProcessingError(
                        f"Observation '{observation.id}' references transcript "
                        f"'{transcript_id}' outside source '{observation.source_id}'"
                    )
                if float(transcript["end"]) < lower or float(transcript["start"]) > upper:
                    raise ProcessingError(
                        f"Transcript '{transcript_id}' is not within "
                        f"{neighborhood_seconds:g}s of observation '{observation.id}'"
                    )
            for frame_id in observation.frame_ids:
                visual = source_visuals.get(frame_id)
                if visual is None:
                    raise ProcessingError(
                        f"Observation '{observation.id}' references frame '{frame_id}' "
                        f"outside source '{observation.source_id}'"
                    )
                if float(visual["timestamp"]) < lower or float(visual["timestamp"]) > upper:
                    raise ProcessingError(
                        f"Frame '{frame_id}' is not within {neighborhood_seconds:g}s of "
                        f"observation '{observation.id}'"
                    )

    @staticmethod
    def _write_observations(
        connection: sqlite3.Connection,
        observations: list[AgentObservation],
        *,
        updated_at: str,
    ) -> None:
        connection.executemany(
            """
            INSERT INTO agent_observations(
                id, source_id, start, end, observation_type, status,
                data_json, updated_at
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                start=excluded.start,
                end=excluded.end,
                observation_type=excluded.observation_type,
                status=excluded.status,
                data_json=excluded.data_json,
                updated_at=excluded.updated_at
            """,
            [
                (
                    item.id,
                    item.source_id,
                    item.start,
                    item.end,
                    item.type.value,
                    item.status.value,
                    item.model_dump_json(),
                    updated_at,
                )
                for item in observations
            ],
        )

    def upsert_observations(self, observations: Iterable[AgentObservation]) -> None:
        """Atomically insert or update observations without moving ids between sources."""

        items = list(observations)
        self._ensure_unique_ids(items)
        now = datetime.now(UTC).isoformat()
        with self.connect() as connection:
            self._ensure_sources_exist(connection, (item.source_id for item in items))
            self._ensure_ids_keep_source(
                connection,
                table="agent_observations",
                items=items,
            )
            self._write_observations(
                connection,
                items,
                updated_at=now,
            )

    def upsert_grounded_observations(
        self,
        observations: Iterable[AgentObservation],
        *,
        neighborhood_seconds: float,
    ) -> None:
        """Validate references and upsert observations in one reserved transaction."""

        items = list(observations)
        self._ensure_unique_ids(items)
        now = datetime.now(UTC).isoformat()
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._validate_observation_references(
                connection,
                items,
                neighborhood_seconds,
            )
            self._ensure_ids_keep_source(
                connection,
                table="agent_observations",
                items=items,
            )
            self._write_observations(
                connection,
                items,
                updated_at=now,
            )

    def replace_observations(
        self,
        source_id: str,
        observations: Iterable[AgentObservation],
    ) -> None:
        """Atomically replace one source's observations."""

        items = list(observations)
        self._ensure_unique_ids(items)
        if any(item.source_id != source_id for item in items):
            raise ProcessingError("Replacement observations must all belong to the source")
        now = datetime.now(UTC).isoformat()
        with self.connect() as connection:
            self._ensure_sources_exist(connection, [source_id])
            self._ensure_ids_keep_source(
                connection,
                table="agent_observations",
                items=items,
            )
            connection.execute(
                "DELETE FROM agent_observations WHERE source_id=?",
                (source_id,),
            )
            connection.executemany(
                """
                INSERT INTO agent_observations(
                    id, source_id, start, end, observation_type, status,
                    data_json, updated_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        item.id,
                        item.source_id,
                        item.start,
                        item.end,
                        item.type.value,
                        item.status.value,
                        item.model_dump_json(),
                        now,
                    )
                    for item in items
                ],
            )

    def get_observation(self, observation_id: str) -> AgentObservation:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT data_json FROM agent_observations WHERE id=?",
                (observation_id,),
            ).fetchone()
        if row is None:
            raise ProcessingError(f"Unknown observation id: {observation_id}")
        return AgentObservation.model_validate_json(row["data_json"])

    def observations(
        self,
        source_id: str,
        *,
        start: float | None = None,
        end: float | None = None,
        observation_type: ObservationType | None = None,
        status: ObservationStatus | None = None,
        limit: int = 500,
    ) -> list[AgentObservation]:
        if limit < 1:
            return []
        params: list[object] = [source_id]
        where = ["source_id=?"]
        if start is not None:
            where.append("end>=?")
            params.append(start)
        if end is not None:
            where.append("start<=?")
            params.append(end)
        if observation_type is not None:
            where.append("observation_type=?")
            params.append(observation_type.value)
        if status is not None:
            where.append("status=?")
            params.append(status.value)
        params.append(limit)
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT data_json FROM agent_observations WHERE "
                + " AND ".join(where)
                + " ORDER BY start, end, id LIMIT ?",
                params,
            ).fetchall()
        return [AgentObservation.model_validate_json(row["data_json"]) for row in rows]

    def list_observations(
        self,
        source_id: str,
        *,
        start: float | None = None,
        end: float | None = None,
        limit: int = 500,
    ) -> list[AgentObservation]:
        return self.observations(source_id, start=start, end=end, limit=limit)

    def delete_observation(self, observation_id: str) -> bool:
        with self.connect() as connection:
            cursor = connection.execute(
                "DELETE FROM agent_observations WHERE id=?",
                (observation_id,),
            )
            return cursor.rowcount > 0

    def upsert_gaps(
        self,
        gaps: Iterable[EvidenceGap],
        *,
        preserve_resolution: bool = True,
    ) -> None:
        """Atomically persist gaps, preserving manual resolution by default."""

        items = list(gaps)
        self._ensure_unique_ids(items)
        now = datetime.now(UTC).isoformat()
        with self.connect() as connection:
            self._ensure_sources_exist(connection, (item.source_id for item in items))
            self._ensure_ids_keep_source(connection, table="evidence_gaps", items=items)
            persisted: list[EvidenceGap] = []
            for item in items:
                if preserve_resolution and not item.resolved:
                    row = connection.execute(
                        "SELECT data_json FROM evidence_gaps WHERE id=?",
                        (item.id,),
                    ).fetchone()
                    if row is not None:
                        previous = EvidenceGap.model_validate_json(row["data_json"])
                        if previous.resolved:
                            item = EvidenceGap.model_validate(
                                {
                                    **item.model_dump(),
                                    "resolved": True,
                                    "resolved_at": previous.resolved_at,
                                    "resolution": previous.resolution,
                                }
                            )
                persisted.append(item)
            connection.executemany(
                """
                INSERT INTO evidence_gaps(
                    id, source_id, semantic_segment_id, gap_type, severity,
                    start, end, resolved, data_json, updated_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    semantic_segment_id=excluded.semantic_segment_id,
                    gap_type=excluded.gap_type,
                    severity=excluded.severity,
                    start=excluded.start,
                    end=excluded.end,
                    resolved=excluded.resolved,
                    data_json=excluded.data_json,
                    updated_at=excluded.updated_at
                """,
                [
                    (
                        item.id,
                        item.source_id,
                        item.semantic_segment_id,
                        item.gap_type.value,
                        item.severity.value,
                        item.start,
                        item.end,
                        int(item.resolved),
                        item.model_dump_json(),
                        now,
                    )
                    for item in persisted
                ],
            )

    def replace_gaps(
        self,
        source_id: str,
        gaps: Iterable[EvidenceGap],
        *,
        preserve_resolution: bool = True,
    ) -> None:
        """Atomically replace one source's detected gaps."""

        items = list(gaps)
        self._ensure_unique_ids(items)
        if any(item.source_id != source_id for item in items):
            raise ProcessingError("Replacement gaps must all belong to the source")
        now = datetime.now(UTC).isoformat()
        with self.connect() as connection:
            self._ensure_sources_exist(connection, [source_id])
            self._ensure_ids_keep_source(connection, table="evidence_gaps", items=items)
            if preserve_resolution:
                old_rows = connection.execute(
                    "SELECT id, data_json FROM evidence_gaps WHERE source_id=?",
                    (source_id,),
                ).fetchall()
                old = {
                    row["id"]: EvidenceGap.model_validate_json(row["data_json"]) for row in old_rows
                }
                items = [
                    EvidenceGap.model_validate(
                        {
                            **item.model_dump(),
                            "resolved": True,
                            "resolved_at": old[item.id].resolved_at,
                            "resolution": old[item.id].resolution,
                        }
                    )
                    if item.id in old and old[item.id].resolved and not item.resolved
                    else item
                    for item in items
                ]
            connection.execute("DELETE FROM evidence_gaps WHERE source_id=?", (source_id,))
            connection.executemany(
                """
                INSERT INTO evidence_gaps(
                    id, source_id, semantic_segment_id, gap_type, severity,
                    start, end, resolved, data_json, updated_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        item.id,
                        item.source_id,
                        item.semantic_segment_id,
                        item.gap_type.value,
                        item.severity.value,
                        item.start,
                        item.end,
                        int(item.resolved),
                        item.model_dump_json(),
                        now,
                    )
                    for item in items
                ],
            )

    def get_gap(self, gap_id: str) -> EvidenceGap:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT data_json FROM evidence_gaps WHERE id=?",
                (gap_id,),
            ).fetchone()
        if row is None:
            raise ProcessingError(f"Unknown evidence gap id: {gap_id}")
        return EvidenceGap.model_validate_json(row["data_json"])

    def gaps(
        self,
        source_id: str | None = None,
        *,
        gap_type: EvidenceGapType | None = None,
        resolved: bool | None = None,
        limit: int | None = 1000,
    ) -> list[EvidenceGap]:
        if limit is not None and limit < 1:
            return []
        params: list[object] = []
        where: list[str] = []
        if source_id is not None:
            where.append("source_id=?")
            params.append(source_id)
        if gap_type is not None:
            where.append("gap_type=?")
            params.append(gap_type.value)
        if resolved is not None:
            where.append("resolved=?")
            params.append(int(resolved))
        query = "SELECT data_json FROM evidence_gaps"
        if where:
            query += " WHERE " + " AND ".join(where)
        query += " ORDER BY source_id, start, end, gap_type, id"
        if limit is not None:
            query += " LIMIT ?"
            params.append(limit)
        with self.connect() as connection:
            rows = connection.execute(query, params).fetchall()
        return [EvidenceGap.model_validate_json(row["data_json"]) for row in rows]

    def list_gaps(
        self,
        source_id: str | None = None,
        *,
        resolved: bool | None = None,
        limit: int | None = 1000,
    ) -> list[EvidenceGap]:
        return self.gaps(source_id, resolved=resolved, limit=limit)

    def resolve_gap(
        self,
        gap_id: str,
        *,
        resolved: bool = True,
        resolution: str | None = None,
    ) -> EvidenceGap:
        gap = self.get_gap(gap_id)
        updates: dict[str, object] = {
            "resolved": resolved,
            "resolved_at": datetime.now(UTC) if resolved else None,
            "resolution": resolution if resolved else None,
        }
        updated = EvidenceGap.model_validate({**gap.model_dump(), **updates})
        self.upsert_gaps([updated], preserve_resolution=False)
        return updated

    def delete_gap(self, gap_id: str) -> bool:
        with self.connect() as connection:
            cursor = connection.execute(
                "DELETE FROM evidence_gaps WHERE id=?",
                (gap_id,),
            )
            return cursor.rowcount > 0

    def source_directory(self, source_id: str) -> Path:
        directory = (self.sources_dir / source_id).resolve()
        if not is_within(directory, self.sources_dir):
            raise ProcessingError(f"Unsafe source id: {source_id}")
        directory.mkdir(parents=True, exist_ok=True)
        return directory
