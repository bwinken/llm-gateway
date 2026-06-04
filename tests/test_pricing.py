"""Tests for cost calculation — _calc_cost, including Azure cache pricing."""

from __future__ import annotations

from decimal import Decimal

from app.services.vllm_proxy import _calc_cost, _calc_cost_breakdown


class TestCalcCostBasics:
    def test_per_model_override(self):
        route = {"input_price_per_1m": 2.0, "output_price_per_1m": 6.0}
        # 1000 in, 500 out → (1000*2 + 500*6) / 1e6
        cost = _calc_cost(route, "llm", 1000, 500)
        assert cost == Decimal("2000") / 1_000_000 + Decimal("3000") / 1_000_000

    def test_falls_back_to_type_pricing(self):
        # Empty route → uses PRICING_MAP via model_type; test config has
        # llm = input 0.50 / output 1.50
        cost = _calc_cost({}, "llm", 1_000_000, 0)
        assert cost == Decimal("0.50")


class TestCachedTokenPricing:
    def test_cached_tokens_billed_at_discount(self):
        route = {
            "input_price_per_1m": 10.0,
            "output_price_per_1m": 30.0,
            "cached_input_price_per_1m": 2.5,   # cache read at 25% of input
        }
        # 10_000 input of which 8_000 cached, 1_000 output
        #   uncached: 2_000 * 10  = 20_000
        #   cached:   8_000 * 2.5 = 20_000
        #   output:   1_000 * 30  = 30_000
        #   total / 1e6 = 0.070
        cost = _calc_cost(route, "llm", 10_000, 1_000, cached_tokens=8_000)
        assert cost == Decimal("70000") / 1_000_000

    def test_no_cached_price_charges_full_input(self):
        # cached_tokens passed but route has no cached price → full input rate
        route = {"input_price_per_1m": 10.0, "output_price_per_1m": 30.0}
        cost = _calc_cost(route, "llm", 10_000, 1_000, cached_tokens=8_000)
        # all 10_000 at 10.0 + 1_000 at 30.0
        assert cost == Decimal("130000") / 1_000_000

    def test_cached_tokens_zero_is_noop(self):
        route = {
            "input_price_per_1m": 10.0,
            "output_price_per_1m": 30.0,
            "cached_input_price_per_1m": 2.5,
        }
        with_zero = _calc_cost(route, "llm", 10_000, 1_000, cached_tokens=0)
        without = _calc_cost(route, "llm", 10_000, 1_000)
        assert with_zero == without

    def test_cached_tokens_clamped_to_input(self):
        # downstream wrongly reports cached > prompt → clamp, never negative
        route = {
            "input_price_per_1m": 10.0,
            "output_price_per_1m": 30.0,
            "cached_input_price_per_1m": 2.5,
        }
        # cached clamped to 10_000 → all input at cached rate
        cost = _calc_cost(route, "llm", 10_000, 0, cached_tokens=99_999)
        assert cost == Decimal("25000") / 1_000_000

    def test_vllm_path_unaffected(self):
        # vLLM callers never pass cached_tokens — default 0, full input rate
        route = {"input_price_per_1m": 0.5, "output_price_per_1m": 1.5}
        cost = _calc_cost(route, "llm", 1_000_000, 0)
        assert cost == Decimal("0.50")


class TestCostBreakdown:
    """_calc_cost_breakdown feeds Langfuse costDetails; total must match _calc_cost."""

    def test_breakdown_components_sum_to_total(self):
        route = {
            "input_price_per_1m": 10.0,
            "output_price_per_1m": 30.0,
            "cached_input_price_per_1m": 2.5,
        }
        bd = _calc_cost_breakdown(route, "llm", 10_000, 1_000, cached_tokens=8_000)
        # uncached 2_000*10 + cached 8_000*2.5 = 20_000 + 20_000 = 40_000 input
        assert bd["input"] == Decimal("40000") / 1_000_000
        assert bd["cache_read_input_tokens"] == Decimal("20000") / 1_000_000
        assert bd["output"] == Decimal("30000") / 1_000_000
        assert bd["total"] == Decimal("70000") / 1_000_000
        # and total equals the scalar _calc_cost
        assert bd["total"] == _calc_cost(route, "llm", 10_000, 1_000, cached_tokens=8_000)

    def test_breakdown_no_cache(self):
        route = {"input_price_per_1m": 2.0, "output_price_per_1m": 6.0}
        bd = _calc_cost_breakdown(route, "llm", 1000, 500)
        assert bd["input"] == Decimal("2000") / 1_000_000
        assert bd["output"] == Decimal("3000") / 1_000_000
        assert bd["cache_read_input_tokens"] == Decimal("0")
        assert bd["total"] == _calc_cost(route, "llm", 1000, 500)
