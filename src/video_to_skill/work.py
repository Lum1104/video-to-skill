"""Typed durable work records shared by the coordinator and worker-facing CLI."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class WorkModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class WorkRole(StrEnum):
    ANALYZE = "analyze"
    AUTHOR = "author"
    REVIEW = "review"
    DECISION = "decision"


class WorkState(StrEnum):
    PENDING = "pending"
    LEASED = "leased"
    COMPLETE = "complete"
    FAILED = "failed"


class AnalysisRun(WorkModel):
    id: str
    snapshot_digest: str
    edition_id: str | None = None
    created_at: datetime
    updated_at: datetime


class WorkItem(WorkModel):
    id: str
    run_id: str
    role: WorkRole
    scope: dict[str, Any]
    persona_hint: str
    packet_path: Path
    packet_digest: str
    result_schema_path: Path
    snapshot_digest: str
    state: WorkState
    dependencies: list[str] = Field(default_factory=list)
    lease_owner: str | None = None
    lease_expires_at: datetime | None = None
    execution_context_id: str | None = None
    attempt_count: int = Field(default=0, ge=0)
    result_path: Path | None = None
    result_digest: str | None = None
    failure_reason: str | None = None
    created_at: datetime
    updated_at: datetime


class WorkLease(WorkModel):
    item: WorkItem
    token: str
    execution_context_id: str
    output_directory: Path


class CanonicalRecord(WorkModel):
    kind: str
    record_id: str
    revision: int = Field(ge=1)
    path: Path
    digest: str
    producer_task_id: str
    snapshot_digest: str
    created_at: datetime
