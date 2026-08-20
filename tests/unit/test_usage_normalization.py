"""RED-phase contract tests for provider-neutral transcript usage analytics.

These tests intentionally describe the normalization boundary before its
implementation. They use the existing analytics parser as the integration
surface so the production code remains untouched during TDD RED.
"""

import json

import pytest

from jacked.web.analytics_scanner import _parse_assistant_message



def _record(*, usage, model="openai/gpt-4o-mini", message_id="or-1"):
    return {
        "type": "assistant",
        "timestamp": "2026-08-19T12:00:00Z",
        "sessionId": "session-openrouter",
        "message": {
            "id": message_id,
            "model": model,
            "role": "assistant",
            "usage": usage,
        },
    }


class TestOpenRouterChatCompletionsUsage:
    def test_maps_prompt_and_completion_tokens(self):
        result = _parse_assistant_message(
            _record(
                usage={
                    "prompt_tokens": 1200,
                    "completion_tokens": 340,
                    "total_tokens": 1540,
                }
            ),
            "session-openrouter",
            "project-openrouter",
            is_subagent=False,
        )

        assert result is not None
        assert result["input_tokens"] == 1200
        assert result["output_tokens"] == 340
        assert result["total_tokens"] == 1540

    def test_maps_cached_prompt_tokens_from_prompt_details(self):
        result = _parse_assistant_message(
            _record(
                usage={
                    "prompt_tokens": 1200,
                    "completion_tokens": 340,
                    "prompt_tokens_details": {"cached_tokens": 900},
                }
            ),
            "session-openrouter",
            "project-openrouter",
            is_subagent=False,
        )

        assert result is not None
        assert result["cache_read_tokens"] == 900

    def test_preserves_provider_reported_cost_as_authoritative(self):
        result = _parse_assistant_message(
            _record(
                usage={
                    "prompt_tokens": 1200,
                    "completion_tokens": 340,
                    "cost": 0.0042,
                    "cost_details": {"upstream_inference_cost": 0.0038},
                }
            ),
            "session-openrouter",
            "project-openrouter",
            is_subagent=False,
        )

        assert result is not None
        assert result["cost_usd"] == pytest.approx(0.0042)
        assert result["cost_source"] == "provider"
        assert result["cost_is_authoritative"] is True


class TestAnthropicUsage:
    def test_maps_native_input_output_and_cache_fields(self):
        result = _parse_assistant_message(
            _record(
                model="claude-sonnet-4-6",
                usage={
                    "input_tokens": 1200,
                    "output_tokens": 340,
                    "cache_read_input_tokens": 900,
                    "cache_creation_input_tokens": 80,
                },
            ),
            "session-anthropic",
            "project-anthropic",
            is_subagent=False,
        )

        assert result is not None
        assert result["input_tokens"] == 1200
        assert result["output_tokens"] == 340
        assert result["cache_read_tokens"] == 900
        assert result["cache_create_tokens"] == 80

    def test_openrouter_anthropic_skin_uses_native_fields_and_provider_cost(self):
        result = _parse_assistant_message(
            _record(
                model="anthropic/claude-sonnet-4.6",
                usage={
                    "input_tokens": 1200,
                    "output_tokens": 340,
                    "cache_read_input_tokens": 900,
                    "cache_creation_input_tokens": 80,
                    "cost": 0.0067,
                },
            ),
            "session-openrouter-anthropic",
            "project-openrouter",
            is_subagent=False,
        )

        assert result is not None
        assert result["input_tokens"] == 1200
        assert result["cache_read_tokens"] == 900
        assert result["cache_create_tokens"] == 80
        assert result["cost_usd"] == pytest.approx(0.0067)
        assert result["cost_source"] == "provider"


class TestCostProvenance:
    @pytest.mark.parametrize(
        "cost_value",
        [None, "not-a-number", -0.1, {"amount": 0.01}],
    )
    def test_malformed_or_missing_cost_falls_back_to_estimate(self, cost_value):
        usage = {
            "prompt_tokens": 1_000_000,
            "completion_tokens": 0,
        }
        if cost_value is not None:
            usage["cost"] = cost_value

        result = _parse_assistant_message(
            _record(usage=usage),
            "session-openrouter",
            "project-openrouter",
            is_subagent=False,
        )

        assert result is not None
        assert result["cost_usd"] is None
        assert result["cost_source"] == "estimate"
        assert result["cost_is_authoritative"] is False
        assert result["estimated_cost_usd"] > 0

    @pytest.mark.parametrize("bad", [float("inf"), float("nan")])
    def test_nonfinite_token_values_are_ignored(self, bad):
        result = _parse_assistant_message(
            _record(usage={"prompt_tokens": bad, "completion_tokens": 1}),
            "session-openrouter",
            "project-openrouter",
            is_subagent=False,
        )
        assert result["input_tokens"] == 0
        assert result["output_tokens"] == 1

    def test_provider_cost_is_not_replaced_by_local_model_estimate(self):
        result = _parse_assistant_message(
            _record(
                usage={
                    "prompt_tokens": 1_000_000,
                    "completion_tokens": 0,
                    "cost": 0.123456,
                }
            ),
            "session-openrouter",
            "project-openrouter",
            is_subagent=False,
        )

        assert result is not None
        assert result["cost_usd"] == pytest.approx(0.123456)
        assert result["estimated_cost_usd"] != pytest.approx(0.123456)
        assert result["cost_source"] == "provider"


class TestAnalyticsParserExpectations:
    def test_parser_accepts_openrouter_assistant_transcript_line(self, tmp_path):
        path = tmp_path / "openrouter.jsonl"
        path.write_text(
            json.dumps(
                _record(
                    usage={
                        "prompt_tokens": 42,
                        "completion_tokens": 8,
                        "prompt_tokens_details": {"cached_tokens": 7},
                        "cost": 0.0002,
                    }
                )
            )
            + "\n",
            encoding="utf-8",
        )

        from jacked.web.analytics_scanner import parse_jsonl_from_offset

        parsed, offset = parse_jsonl_from_offset(path, 0)

        assert offset == path.stat().st_size
        assert len(parsed) == 1
        assert parsed[0]["input_tokens"] == 42
        assert parsed[0]["output_tokens"] == 8
        assert parsed[0]["cache_read_tokens"] == 7
        assert parsed[0]["cost_usd"] == pytest.approx(0.0002)
        assert parsed[0]["cost_source"] == "provider"
