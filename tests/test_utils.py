from pathlib import Path

from video_to_skill.utils import (
    format_timestamp,
    is_within,
    slugify,
    stable_hash,
    timestamp_url,
)


def test_stable_hash_is_order_independent() -> None:
    assert stable_hash({"a": 1, "b": 2}) == stable_hash({"b": 2, "a": 1})


def test_slugify_is_agent_skill_compatible() -> None:
    assert slugify("Practical 中文 Course!") == "practical-course"
    assert len(slugify("x" * 100)) <= 63


def test_timestamp_format_and_urls() -> None:
    assert format_timestamp(65) == "01:05"
    assert format_timestamp(3661) == "01:01:01"
    assert timestamp_url("https://youtu.be/abc?list=xyz", 65) == (
        "https://youtu.be/abc?list=xyz&t=65"
    )
    assert timestamp_url("https://www.bilibili.com/video/BV123", 12.9) == (
        "https://www.bilibili.com/video/BV123?t=12"
    )


def test_is_within_rejects_sibling(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    assert is_within(root / "child", root)
    assert not is_within(tmp_path / "other", root)
