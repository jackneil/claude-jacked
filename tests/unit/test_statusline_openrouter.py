"""OpenRouter cost rendering for the Claude Code statusline."""

import json

import pytest

from jacked import statusline


@pytest.fixture
def home(tmp_path):
    h = tmp_path / "home"
    (h / ".claude").mkdir(parents=True)
    return h


def _assistant(usage):
    return {
        "type": "assistant",
        "message": {"model": "openai/gpt-5.6-luna", "usage": usage},
    }


def test_statusline_renders_authoritative_latest_transcript_cost(tmp_path, home):
    transcript = tmp_path / "session.jsonl"
    transcript.write_text(
        json.dumps(_assistant({
            "prompt_tokens": 1200,
            "completion_tokens": 340,
            "cost": 0.0042,
        })) + "\n",
        encoding="utf-8",
    )

    line = statusline.render({"transcript_path": str(transcript)}, home=str(home))

    assert "$0.0042" in line
    assert "~" not in line


def test_statusline_omits_cost_when_wrapper_dropped_provider_cost(tmp_path, home):
    transcript = tmp_path / "session.jsonl"
    transcript.write_text(
        json.dumps(_assistant({
            "prompt_tokens": 1200,
            "completion_tokens": 340,
        })) + "\n",
        encoding="utf-8",
    )

    line = statusline.render({"transcript_path": str(transcript)}, home=str(home))

    assert "$" not in line


def test_statusline_does_not_show_an_older_cost_after_latest_response_drops_it(
    tmp_path, home
):
    transcript = tmp_path / "session.jsonl"
    transcript.write_text(
        "\n".join([
            json.dumps(_assistant({"prompt_tokens": 1, "completion_tokens": 1, "cost": 0.1})),
            json.dumps(_assistant({"prompt_tokens": 2, "completion_tokens": 2})),
        ]) + "\n",
        encoding="utf-8",
    )

    line = statusline.render({"transcript_path": str(transcript)}, home=str(home))

    assert "$" not in line
