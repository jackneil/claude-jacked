from __future__ import annotations

from jacked.credentials.models import WriterWitness
from jacked.credentials.writer_fence import StaticWriterInspector, WriterFence


def test_fence_allows_only_matching_v2_writers() -> None:
    fence = WriterFence(
        StaticWriterInspector(
            (WriterWitness("service", 2, 7, True, "signed-manifest"),)
        )
    )

    result = fence.inspect(required_protocol_epoch=2, capability_epoch=7)

    assert result.is_allowed is True


def test_legacy_writer_fails_closed() -> None:
    fence = WriterFence(
        StaticWriterInspector((WriterWitness("legacy", 1, 7, True, "pid-only"),))
    )

    result = fence.inspect(required_protocol_epoch=2, capability_epoch=7)

    assert result.is_allowed is False
    assert "legacy" in result.reason


def test_ambiguous_writer_fails_closed() -> None:
    fence = WriterFence(StaticWriterInspector((), is_complete=False))

    result = fence.inspect(required_protocol_epoch=2, capability_epoch=7)

    assert result.is_allowed is False
    assert "exclude" in result.reason
