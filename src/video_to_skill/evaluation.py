"""Evaluation harness for labeled visual-state and boundary benchmarks."""

from __future__ import annotations

import json
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from video_to_skill.errors import ProcessingError
from video_to_skill.workspace import Workspace


class SourceLabels(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source_id: str
    critical_visual_timestamps: list[float] = Field(default_factory=list)
    semantic_boundary_timestamps: list[float] = Field(default_factory=list)


class EvaluationLabels(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: int = 1
    sources: list[SourceLabels]


class SourceMetrics(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source_id: str
    visual_hits: int
    visual_total: int
    visual_recall: float | None
    boundary_true_positives: int
    boundary_predictions: int
    boundary_total: int
    boundary_precision: float | None
    boundary_recall: float | None
    boundary_f1: float | None


class EvaluationReport(BaseModel):
    model_config = ConfigDict(extra="forbid")
    passed: bool
    tolerance_seconds: float
    visual_recall: float | None
    boundary_precision: float | None
    boundary_recall: float | None
    boundary_f1: float | None
    sources: list[SourceMetrics]


def _match(expected: list[float], predicted: list[float], tolerance: float) -> int:
    available = set(range(len(predicted)))
    hits = 0
    for target in sorted(expected):
        candidates = [index for index in available if abs(predicted[index] - target) <= tolerance]
        if not candidates:
            continue
        selected = min(candidates, key=lambda index: abs(predicted[index] - target))
        available.remove(selected)
        hits += 1
    return hits


def _ratio(numerator: int, denominator: int) -> float | None:
    return numerator / denominator if denominator else None


def _f1(precision: float | None, recall: float | None) -> float | None:
    if precision is None or recall is None:
        return None
    if precision + recall == 0:
        return 0.0
    return 2 * precision * recall / (precision + recall)


def load_labels(path: Path) -> EvaluationLabels:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        labels = EvaluationLabels.model_validate(payload)
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        raise ProcessingError(f"Invalid evaluation labels {path}: {exc}") from exc
    if labels.schema_version != 1:
        raise ProcessingError(f"Unsupported evaluation schema {labels.schema_version}; expected 1")
    return labels


def evaluate_workspace(
    workspace: Workspace,
    labels: EvaluationLabels,
    *,
    tolerance_seconds: float = 2.5,
    required_visual_recall: float = 0.90,
) -> EvaluationReport:
    known_ids = {source.id for source in workspace.list_sources()}
    metrics: list[SourceMetrics] = []
    for source_labels in labels.sources:
        if source_labels.source_id not in known_ids:
            raise ProcessingError(
                f"Evaluation labels reference unknown source: {source_labels.source_id}"
            )
        predicted_visuals = [item.timestamp for item in workspace.visuals(source_labels.source_id)]
        predicted_boundaries = [
            item.start
            for item in workspace.semantic_segments(source_labels.source_id)
            if item.start > 0
        ]
        visual_hits = _match(
            source_labels.critical_visual_timestamps,
            predicted_visuals,
            tolerance_seconds,
        )
        boundary_hits = _match(
            source_labels.semantic_boundary_timestamps,
            predicted_boundaries,
            tolerance_seconds,
        )
        boundary_precision = _ratio(boundary_hits, len(predicted_boundaries))
        boundary_recall = _ratio(boundary_hits, len(source_labels.semantic_boundary_timestamps))
        metrics.append(
            SourceMetrics(
                source_id=source_labels.source_id,
                visual_hits=visual_hits,
                visual_total=len(source_labels.critical_visual_timestamps),
                visual_recall=_ratio(visual_hits, len(source_labels.critical_visual_timestamps)),
                boundary_true_positives=boundary_hits,
                boundary_predictions=len(predicted_boundaries),
                boundary_total=len(source_labels.semantic_boundary_timestamps),
                boundary_precision=boundary_precision,
                boundary_recall=boundary_recall,
                boundary_f1=_f1(boundary_precision, boundary_recall),
            )
        )
    visual_hits = sum(item.visual_hits for item in metrics)
    visual_total = sum(item.visual_total for item in metrics)
    boundary_hits = sum(item.boundary_true_positives for item in metrics)
    boundary_predictions = sum(item.boundary_predictions for item in metrics)
    boundary_total = sum(item.boundary_total for item in metrics)
    visual_recall = _ratio(visual_hits, visual_total)
    boundary_precision = _ratio(boundary_hits, boundary_predictions)
    boundary_recall = _ratio(boundary_hits, boundary_total)
    passed = visual_recall is None or visual_recall >= required_visual_recall
    return EvaluationReport(
        passed=passed,
        tolerance_seconds=tolerance_seconds,
        visual_recall=visual_recall,
        boundary_precision=boundary_precision,
        boundary_recall=boundary_recall,
        boundary_f1=_f1(boundary_precision, boundary_recall),
        sources=metrics,
    )


def render_evaluation_report(report: EvaluationReport) -> str:
    def percent(value: float | None) -> str:
        return "n/a" if value is None else f"{value:.1%}"

    lines = [
        f"{'PASS' if report.passed else 'FAIL'}: evidence evaluation",
        f"Visual recall: {percent(report.visual_recall)}",
        (
            f"Boundary precision/recall/F1: {percent(report.boundary_precision)} / "
            f"{percent(report.boundary_recall)} / {percent(report.boundary_f1)}"
        ),
        f"Tolerance: ±{report.tolerance_seconds:.2f}s",
    ]
    for source in report.sources:
        lines.append(
            f"- {source.source_id}: visuals {source.visual_hits}/{source.visual_total}; "
            f"boundaries {source.boundary_true_positives}/{source.boundary_total}"
        )
    return "\n".join(lines)
