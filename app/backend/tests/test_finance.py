from __future__ import annotations

from pathlib import Path

import pytest

from life_helper.tools.finance import (
    CalculationError,
    FurusatoInput,
    LifeEvent,
    LifePlanParams,
    estimate_furusato,
    safe_eval,
    simulate_lifeplan,
)
from life_helper.tools.tax_params import load_tax_params

TAX_DIR = Path(__file__).resolve().parents[1] / "src" / "life_helper" / "resources" / "tax_params"


def test_safe_eval_basic():
    assert safe_eval("(6,000,000 - 1640000) * 0.1") == pytest.approx(436000)
    assert safe_eval("round(10 / 3, 2)") == 3.33
    assert safe_eval("2 ** 10") == 1024
    assert safe_eval("max(1, 5, 3) × 2") == 10


@pytest.mark.parametrize(
    "expr",
    ["__import__('os')", "open('x')", "1 / 0", "10 ** 1000", "a + 1", "[1,2]", "(1).__class__", "x" * 600, "sqrt(-1)"],
)
def test_safe_eval_rejects_unsafe_or_invalid(expr):
    with pytest.raises(CalculationError):
        safe_eval(expr)


def test_furusato_single_6m_matches_reference_table():
    params = load_tax_params(TAX_DIR, 2025)
    result = estimate_furusato(params, FurusatoInput(year=2025, salary=6_000_000, social_insurance=900_000))
    assert result["estimated_limit_yen"] == 77_000
    assert result["breakdown"]["marginal_income_tax_rate"] == 0.10
    assert result["disclaimer"]


def test_furusato_married_5m_matches_reference_table():
    params = load_tax_params(TAX_DIR, 2025)
    result = estimate_furusato(
        params, FurusatoInput(year=2025, salary=5_000_000, social_insurance=750_000, has_spouse_deduction=True)
    )
    assert result["estimated_limit_yen"] == 49_000


def test_furusato_defaults_social_insurance_and_warns_housing_loan():
    params = load_tax_params(TAX_DIR, 2025)
    result = estimate_furusato(params, FurusatoInput(year=2025, salary=6_000_000, has_housing_loan_deduction=True))
    assert result["estimated_limit_yen"] == 77_000
    assert any("15%" in a for a in result["assumptions"])
    assert any("住宅ローン" in n for n in result["notes"])


def test_furusato_zero_income():
    params = load_tax_params(TAX_DIR, 2025)
    assert estimate_furusato(params, FurusatoInput(year=2025, salary=0))["estimated_limit_yen"] == 0


def test_tax_params_fallback_and_provisional_warning():
    params = load_tax_params(TAX_DIR, 2030)
    assert params.year == 2026
    assert any("2030" in w for w in params.warnings)
    assert any("未検証" in w for w in params.warnings)


def test_lifeplan_simple_cashflow():
    p = LifePlanParams(
        start_year=2026,
        current_age=40,
        end_age=42,
        cash_savings=1_000_000,
        annual_net_income=5_000_000,
        annual_living_expense=4_000_000,
        inflation_rate=0.0,
        investment_return_rate=0.0,
        events=[LifeEvent(label="車", age=41, amount=2_000_000)],
    )
    result = simulate_lifeplan(p)
    totals = [r["total_assets"] for r in result["rows"]]
    assert totals == [2_000_000, 1_000_000, 2_000_000]
    assert result["summary"]["min_total_assets_age"] == 41
    assert result["summary"]["depleted_age"] is None


def test_lifeplan_draws_from_investments_and_detects_depletion():
    p = LifePlanParams(
        start_year=2026,
        current_age=64,
        end_age=67,
        cash_savings=0,
        annual_net_income=0,
        retirement_age=64,
        annual_living_expense=1_000_000,
        inflation_rate=0.0,
        investment_balance=1_500_000,
        investment_return_rate=0.0,
    )
    rows = simulate_lifeplan(p)["rows"]
    assert rows[0]["investment"] == 500_000 and rows[0]["cash"] == 0
    assert simulate_lifeplan(p)["summary"]["depleted_age"] == 65


async def test_tools_are_registered(ctx):
    from life_helper.tools.finance import build_tools

    names = {spec.tool.name for spec in build_tools(ctx)}
    assert names == {"calculate", "estimate_furusato_limit", "simulate_lifeplan"}


def test_chart_extraction_keeps_only_chart_columns():
    import json

    from life_helper.copilot_integration.events import extract_chart

    p = LifePlanParams(
        start_year=2026, current_age=40, end_age=45, annual_net_income=5_000_000, annual_living_expense=4_000_000
    )
    chart = extract_chart(json.dumps(simulate_lifeplan(p)))
    assert chart["type"] == "lifeplan" and len(chart["data"]) == 6
    assert set(chart["data"][0]) == {"age", "cash", "investment", "total_assets"}
    assert extract_chart("not json") is None and extract_chart(json.dumps({"x": 1})) is None
