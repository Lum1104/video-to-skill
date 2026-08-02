from pathlib import Path

import pytest

from video_to_skill.artifact_language import (
    ArtifactLanguageDeclaration,
    declare_artifact_language,
    ensure_artifact_language_contract,
    resolve_artifact_language_contract,
)
from video_to_skill.config import Settings
from video_to_skill.errors import ProcessingError
from video_to_skill.models import (
    SourceDescriptor,
    SourcePlatform,
    TranscriptOrigin,
    TranscriptSegment,
)
from video_to_skill.workspace import Workspace


def _workspace(
    tmp_path: Path,
    languages: list[str | None],
    *,
    descriptor_languages: list[str | None] | None = None,
) -> Workspace:
    workspace = Workspace.create(
        root=tmp_path / "workspace",
        inputs=[f"source-{index}" for index in range(len(languages))],
        settings=Settings(cache_root=tmp_path),
    )
    descriptors = []
    for index, _language in enumerate(languages):
        descriptor_language = (
            descriptor_languages[index] if descriptor_languages is not None else None
        )
        source = SourceDescriptor(
            id=f"source-{index}",
            platform=SourcePlatform.LOCAL,
            locator=f"/tmp/source-{index}.mp4",
            title=f"Source {index}",
            language=descriptor_language,
        )
        descriptors.append(source)
    workspace.upsert_sources(descriptors)
    for index, language in enumerate(languages):
        if language is not None:
            workspace.replace_transcripts(
                descriptors[index].id,
                [
                    TranscriptSegment(
                        id=f"transcript-{index}",
                        source_id=descriptors[index].id,
                        start=0,
                        end=10,
                        text="Grounded content.",
                        language=language,
                        origin=TranscriptOrigin.MANUAL_CAPTION,
                    )
                ],
            )
    return workspace


def test_explicit_language_locale_is_fixed_and_normalized(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path, [None])

    contract = resolve_artifact_language_contract(workspace, "ZH_hans_cn")
    digest = ensure_artifact_language_contract(workspace, "ZH_hans_cn")[1]

    assert contract.requested_output_language == "zh-Hans-CN"
    assert contract.fixed_artifact_language == "zh-Hans-CN"
    assert contract.resolution == "explicit"
    assert declare_artifact_language(contract, digest, "zh-Hans-CN").declaration_state == (
        "resolved"
    )
    with pytest.raises(ProcessingError, match="conflicts with the fixed"):
        declare_artifact_language(contract, digest, "English")


def test_source_uses_selected_transcript_language_before_descriptor(tmp_path: Path) -> None:
    workspace = _workspace(
        tmp_path,
        ["en-US"],
        descriptor_languages=["zh-Hans"],
    )

    contract = resolve_artifact_language_contract(workspace, "source")

    assert contract.resolution == "source-single"
    assert contract.source_languages == ["en-US"]
    assert contract.fixed_artifact_language == "en-US"


def test_mixed_source_requires_one_observed_language(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path, ["en", "zh-Hans"])
    contract = resolve_artifact_language_contract(workspace, "source")
    digest = ensure_artifact_language_contract(workspace, "source")[1]

    assert contract.resolution == "source-mixed"
    assert contract.fixed_artifact_language is None
    assert contract.source_languages == ["en", "zh-Hans"]
    declaration = declare_artifact_language(contract, digest, "zh-Hans")
    assert declaration.artifact_language == "zh-Hans"
    assert declaration.declaration_state == "agent-declared"
    with pytest.raises(ProcessingError, match="one of the observed"):
        declare_artifact_language(contract, digest, "French")


def test_partly_unknown_source_requires_bounded_agent_declaration(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path, ["en", None])
    contract = resolve_artifact_language_contract(workspace, "source")
    digest = ensure_artifact_language_contract(workspace, "source")[1]

    assert contract.resolution == "source-unknown"
    assert contract.source_languages == ["en"]
    declaration = declare_artifact_language(contract, digest, "Japanese")
    assert declaration.artifact_language == "Japanese"
    with pytest.raises(ProcessingError, match="concrete language"):
        declare_artifact_language(contract, digest, "source")


@pytest.mark.parametrize(
    "unknown_language",
    [None, "", "   ", "und", "unknown", "x", "x" * 81, "en\nfr"],
)
def test_partial_unknown_selected_transcript_retains_candidate_but_requires_declaration(
    tmp_path: Path,
    unknown_language: str | None,
) -> None:
    workspace = _workspace(tmp_path, [None], descriptor_languages=["en"])
    source = workspace.list_sources()[0]
    workspace.replace_transcripts(
        source.id,
        [
            TranscriptSegment(
                id="transcript-known",
                source_id=source.id,
                start=0,
                end=5,
                text="Known language.",
                language="en",
                origin=TranscriptOrigin.MANUAL_CAPTION,
            ),
            TranscriptSegment(
                id="transcript-unknown",
                source_id=source.id,
                start=5,
                end=10,
                text="Unknown language metadata.",
                language=unknown_language,
                origin=TranscriptOrigin.MANUAL_CAPTION,
            ),
        ],
    )

    contract = resolve_artifact_language_contract(workspace, "source")

    assert contract.resolution == "source-unknown"
    assert contract.source_languages == ["en"]
    assert contract.fixed_artifact_language is None


@pytest.mark.parametrize("mixed_language", ["mixed", "mul", "multilingual", "MUL"])
def test_pure_mixed_source_sentinel_is_mixed_not_unknown(
    tmp_path: Path,
    mixed_language: str,
) -> None:
    workspace = _workspace(tmp_path, [mixed_language])

    contract = resolve_artifact_language_contract(workspace, "source")

    assert contract.resolution == "source-mixed"
    assert contract.source_languages == []
    assert contract.fixed_artifact_language is None


@pytest.mark.parametrize("sentinel", ["auto", "unknown", "und", "mixed", "mul", "zxx"])
def test_declaration_model_rejects_non_concrete_language_sentinels(sentinel: str) -> None:
    with pytest.raises(ValueError, match="concrete"):
        ArtifactLanguageDeclaration(
            contract_digest="a" * 64,
            requested_output_language="source",
            resolution="source-unknown",
            artifact_language=sentinel,
            declaration_state="agent-declared",
        )


def test_persisted_contract_rejects_conflicting_intent(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path, ["en"])
    ensure_artifact_language_contract(workspace, "English")

    with pytest.raises(ProcessingError, match="differs from the persisted"):
        ensure_artifact_language_contract(workspace, "French")
