"""Tests for pure formatting functions."""

import time

from claude_monitor import (
    format_model,
    format_tokens,
    format_ago,
    format_duration,
    format_context_bar,
    format_compactions,
    format_cost,
    estimate_cost,
)


class TestFormatModel:
    def test_opus(self):
        assert format_model("claude-opus-4-6") == "Opus 4.6"

    def test_sonnet(self):
        assert format_model("claude-sonnet-4-6") == "Sonnet 4.6"

    def test_haiku(self):
        assert format_model("claude-haiku-4-5") == "Haiku 4.5"

    def test_sonnet_45(self):
        assert format_model("claude-sonnet-4-5") == "Sonnet 4.5"

    def test_unknown_model(self):
        result = format_model("claude-future-5-0")
        assert "future" in result.lower()

    def test_empty(self):
        assert format_model("") == "—"

    def test_partial_match(self):
        assert format_model("claude-opus-4-6-20260301") == "Opus 4.6"


class TestFormatTokens:
    def test_millions(self):
        assert format_tokens(1_500_000) == "1.5M"

    def test_thousands(self):
        assert format_tokens(50_000) == "50k"

    def test_small(self):
        assert format_tokens(999) == "999"

    def test_zero(self):
        assert format_tokens(0) == "0"

    def test_exactly_1m(self):
        assert format_tokens(1_000_000) == "1.0M"

    def test_exactly_1k(self):
        assert format_tokens(1_000) == "1k"


class TestFormatAgo:
    def test_seconds(self):
        result = format_ago(time.time() - 30)
        assert result.endswith("s")

    def test_minutes(self):
        result = format_ago(time.time() - 180)
        assert result.endswith("m")

    def test_hours(self):
        result = format_ago(time.time() - 7200)
        assert result.endswith("h")

    def test_days(self):
        result = format_ago(time.time() - 100_000)
        assert result.endswith("d")


class TestFormatDuration:
    def test_seconds(self):
        now = time.time()
        assert format_duration(now - 45, now) == "45s"

    def test_minutes(self):
        now = time.time()
        assert format_duration(now - 300, now) == "5m"

    def test_hours_and_minutes(self):
        now = time.time()
        result = format_duration(now - 3660, now)
        assert result == "1h01m"

    def test_days(self):
        now = time.time()
        assert format_duration(now - 90000, now) == "1d"

    def test_no_created(self):
        assert format_duration(0, time.time()) == "—"

    def test_negative_created(self):
        assert format_duration(-1, time.time()) == "—"


class TestFormatContextBar:
    def test_full_context(self):
        result = format_context_bar(100)
        assert "100%" in result
        assert "bright_green" in result

    def test_low_context(self):
        result = format_context_bar(10)
        assert "10%" in result
        assert "red" in result

    def test_medium_context(self):
        result = format_context_bar(50)
        assert "50%" in result

    def test_zero(self):
        result = format_context_bar(0)
        assert "0%" in result
        assert "red" in result


class TestFormatCompactions:
    def test_zero(self):
        assert "—" in format_compactions(0)

    def test_one(self):
        result = format_compactions(1)
        assert "✻" in result
        assert "green" in result

    def test_five(self):
        result = format_compactions(5)
        assert result.count("✻") == 5
        assert "red" in result

    def test_more_than_five(self):
        result = format_compactions(7)
        assert "✻" in result
        assert "+2" in result


class TestFormatCost:
    def test_positive(self):
        assert format_cost(1.50) == "$1.50"

    def test_zero(self):
        assert "—" in format_cost(0)

    def test_large(self):
        assert format_cost(25.00) == "$25.00"

    def test_small(self):
        assert format_cost(0.01) == "$0.01"


class TestEstimateCost:
    def test_opus(self):
        cost = estimate_cost("claude-opus-4-6", 1_000_000, 1_000_000)
        assert cost == 15.0 + 75.0

    def test_sonnet(self):
        cost = estimate_cost("claude-sonnet-4-6", 1_000_000, 1_000_000)
        assert cost == 3.0 + 15.0

    def test_unknown(self):
        assert estimate_cost("gpt-4", 1_000_000, 1_000_000) == 0.0

    def test_zero_tokens(self):
        assert estimate_cost("claude-opus-4-6", 0, 0) == 0.0
