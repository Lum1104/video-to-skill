import json
from pathlib import Path

from video_to_skill.validation import validate_skill


def _valid_skill(root: Path) -> None:
    root.mkdir()
    (root / "chapters").mkdir()
    (root / "SKILL.md").write_text(
        """---
name: demo-skill
description: Apply and learn the demonstrated workflow.
---

# Demo

Read [Chapter 1](chapters/ch01.md).
""",
        encoding="utf-8",
    )
    (root / "chapters" / "ch01.md").write_text(
        "# Chapter 1\n\n## Evidence\n\nhttps://youtu.be/demo?t=12\n",
        encoding="utf-8",
    )
    (root / "provenance.json").write_text(
        json.dumps(
            {
                "schema_version": 2,
                "sources": [
                    {
                        "id": "youtube-demo",
                        "title": "Demo",
                        "url": "https://youtu.be/demo",
                    }
                ],
                "semantic_units": [
                    {
                        "id": "unit-1",
                        "source_id": "youtube-demo",
                        "start": 12,
                        "end": 20,
                        "kind": "claim",
                        "summary": "Apply the demonstrated workflow.",
                        "materiality": "core",
                        "disposition": "included",
                        "inferred": False,
                        "confidence": "high",
                        "modalities": ["speech"],
                        "evidence_ids": ["transcript-1"],
                    }
                ],
                "semantic_relations": [],
                "claims": [
                    {
                        "id": "claim-1",
                        "file": "chapters/ch01.md",
                        "kind": "concept",
                        "summary": "Apply the demonstrated workflow.",
                        "inferred": False,
                        "confidence": "high",
                        "semantic_unit_ids": ["unit-1"],
                        "evidence": [
                            {
                                "source_id": "youtube-demo",
                                "start": 12,
                                "end": 20,
                                "modality": "speech",
                                "evidence_ids": ["transcript-1"],
                            }
                        ],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )


def test_valid_shareable_skill(tmp_path: Path) -> None:
    root = tmp_path / "demo-skill"
    _valid_skill(root)
    report = validate_skill(root, run_official=False)
    assert report.valid, report.model_dump()


def test_raw_media_and_ungrounded_claim_are_rejected(tmp_path: Path) -> None:
    root = tmp_path / "demo-skill"
    _valid_skill(root)
    (root / "assets").mkdir()
    (root / "assets" / "raw.mp4").write_bytes(b"not video")
    payload = json.loads((root / "provenance.json").read_text())
    payload["claims"][0]["evidence"] = []
    (root / "provenance.json").write_text(json.dumps(payload), encoding="utf-8")
    report = validate_skill(root, run_official=False)
    codes = {item.code for item in report.issues}
    assert not report.valid
    assert "private-artifact" in codes
    assert "ungrounded-claim" in codes


def test_broken_relative_link_is_rejected(tmp_path: Path) -> None:
    root = tmp_path / "demo-skill"
    _valid_skill(root)
    (root / "SKILL.md").write_text(
        (root / "SKILL.md").read_text() + "\n[Missing](chapters/nope.md)\n",
        encoding="utf-8",
    )
    report = validate_skill(root, run_official=False)
    assert "broken-link" in {item.code for item in report.issues}


def test_code_check_rejects_invalid_python(tmp_path: Path) -> None:
    root = tmp_path / "demo-skill"
    _valid_skill(root)
    chapter = root / "chapters" / "ch01.md"
    chapter.write_text(
        chapter.read_text() + "\n```python\nif True print('bad')\n```\n",
        encoding="utf-8",
    )
    report = validate_skill(root, run_official=False, check_code=True)
    assert "invalid-code-block" in {item.code for item in report.issues}


def test_playbook_steps_require_provenance_claims(tmp_path: Path) -> None:
    root = tmp_path / "demo-skill"
    _valid_skill(root)
    (root / "playbooks").mkdir()
    (root / "playbooks" / "run.md").write_text(
        "# Run\n\n## Procedure\n\n1. Configure it.\n2. Verify it.\n\n## Evidence\n",
        encoding="utf-8",
    )
    report = validate_skill(root, run_official=False)
    assert "ungrounded-procedure-steps" in {item.code for item in report.issues}


def test_sensitive_query_parameter_in_markdown_url_is_rejected(tmp_path: Path) -> None:
    root = tmp_path / "demo-skill"
    _valid_skill(root)
    chapter = root / "chapters" / "ch01.md"
    chapter.write_text(
        chapter.read_text(encoding="utf-8")
        + ("\nhttps://www.youtube.com/watch?v=abc&access_token=super-secret-value-1234567890\n"),
        encoding="utf-8",
    )

    report = validate_skill(root, run_official=False)

    assert not report.valid
    assert "sensitive-url-query" in {item.code for item in report.issues}


def test_sensitive_fragment_parameters_in_markdown_are_rejected(tmp_path: Path) -> None:
    urls = (
        "https://example.com/course#access_token=super-secret-value-1234567890",
        "https://example.com/course#AUTH_TOKEN=super-secret-value-1234567890",
        "https://example.com/course#oauth_token=super-secret-value-1234567890",
        "https://example.com/course#bearer_token=super-secret-value-1234567890",
        "https://example.com/course#access%5Ftoken=super-secret-value-1234567890",
        "https://example.com/course#oauth%255Ftoken=super-secret-value-1234567890",
        "https://example.com/course#state=ok;access_token=super-secret-value-1234567890",
        "https://example.com/course#state=ok%3Baccess_token=super-secret-value-1234567890",
        "https://example.com/course#/callback?oauth_token=super-secret-value-1234567890",
    )
    for index, url in enumerate(urls):
        root = tmp_path / f"demo-skill-{index}"
        _valid_skill(root)
        chapter = root / "chapters" / "ch01.md"
        chapter.write_text(
            chapter.read_text(encoding="utf-8") + f"\n{url}\n",
            encoding="utf-8",
        )

        report = validate_skill(root, run_official=False)

        assert not report.valid, url
        assert "sensitive-url-query" in {item.code for item in report.issues}


def test_file_uri_markdown_link_is_rejected(tmp_path: Path) -> None:
    root = tmp_path / "demo-skill"
    _valid_skill(root)
    chapter = root / "chapters" / "ch01.md"
    chapter.write_text(
        chapter.read_text(encoding="utf-8")
        + "\n[Private evidence](file:///Users/alice/private/evidence.sqlite3)\n",
        encoding="utf-8",
    )

    report = validate_skill(root, run_official=False)

    assert not report.valid
    assert "unsafe-file-link" in {item.code for item in report.issues}


def test_sensitive_query_parameter_inside_provenance_json_is_rejected(
    tmp_path: Path,
) -> None:
    root = tmp_path / "demo-skill"
    _valid_skill(root)
    provenance_path = root / "provenance.json"
    payload = json.loads(provenance_path.read_text(encoding="utf-8"))
    payload["sources"][0]["url"] = (
        "https://www.youtube.com/watch?v=abc&x-amz-signature=super-secret-value-1234567890"
    )
    provenance_path.write_text(
        json.dumps(payload).replace("/", r"\/"),
        encoding="utf-8",
    )

    report = validate_skill(root, run_official=False)

    assert not report.valid
    assert "sensitive-url-query" in {item.code for item in report.issues}


def test_sensitive_fragment_parameters_inside_provenance_json_are_rejected(
    tmp_path: Path,
) -> None:
    urls = (
        "https://example.com/course#auth_token=super-secret-value-1234567890",
        "https://example.com/course#oauth_token=super-secret-value-1234567890",
        "https://example.com/course#bearer_token=super-secret-value-1234567890",
        "https://example.com/course#state=ok;access_token=super-secret-value-1234567890",
        "https://example.com/course#state=ok%3Baccess_token=super-secret-value-1234567890",
    )
    for index, url in enumerate(urls):
        root = tmp_path / f"demo-skill-{index}"
        _valid_skill(root)
        provenance_path = root / "provenance.json"
        payload = json.loads(provenance_path.read_text(encoding="utf-8"))
        payload["sources"][0]["url"] = url
        provenance_path.write_text(
            json.dumps(payload).replace("/", r"\/"),
            encoding="utf-8",
        )

        report = validate_skill(root, run_official=False)

        assert not report.valid, url
        assert "sensitive-url-query" in {item.code for item in report.issues}


def test_credentialed_url_and_json_credential_value_are_rejected(tmp_path: Path) -> None:
    root = tmp_path / "demo-skill"
    _valid_skill(root)
    (root / "connection.json").write_text(
        json.dumps(
            {
                "endpoint": "https://alice:private-password-123456@example.com/course",
                "api_key": "live-credential-value-1234567890",
            }
        ),
        encoding="utf-8",
    )

    report = validate_skill(root, run_official=False)
    codes = {item.code for item in report.issues}

    assert not report.valid
    assert "credentialed-url" in codes
    assert "possible-secret" in codes


def test_absolute_private_paths_are_rejected_across_platforms(tmp_path: Path) -> None:
    root = tmp_path / "demo-skill"
    _valid_skill(root)
    (root / "private-paths.txt").write_text(
        "\n".join(
            [
                "/Users/alice/private/course/evidence.sqlite3",
                r"C:\Users\alice\private\course\evidence.sqlite3",
                r"\\fileserver\private-share\course\evidence.sqlite3",
            ]
        ),
        encoding="utf-8",
    )

    report = validate_skill(root, run_official=False)

    assert not report.valid
    assert "private-path" in {item.code for item in report.issues}


def test_public_timestamp_links_placeholders_and_ordinary_paths_remain_safe(
    tmp_path: Path,
) -> None:
    root = tmp_path / "demo-skill"
    _valid_skill(root)
    chapter = root / "chapters" / "ch01.md"
    chapter.write_text(
        chapter.read_text(encoding="utf-8")
        + (
            "\nPublic evidence: https://www.youtube.com/watch?v=abc&t=42\n"
            "Use /usr/bin/env for the example and call the public /api/v1 route.\n"
            "The public guide is https://example.com/home/alice/tutorial.\n"
            "A protocol-relative public link can look like //example.com/docs/guide.\n"
            "The secret: practice deliberately and review feedback.\n"
        ),
        encoding="utf-8",
    )
    (root / "example-config.json").write_text(
        json.dumps({"api_key": "YOUR_API_KEY"}),
        encoding="utf-8",
    )

    report = validate_skill(root, run_official=False)

    assert report.valid, report.model_dump()
