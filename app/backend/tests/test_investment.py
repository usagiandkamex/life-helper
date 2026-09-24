from __future__ import annotations

from pathlib import Path

import httpx
import pytest
import respx
from pydantic import SecretStr

from life_helper.market.broker_csv import BrokerCsvError, load_mapping, parse_broker_csv
from life_helper.market.portfolio import (
    CapitalGainsParams,
    GainItem,
    Holding,
    InvestmentSimParams,
    NisaUsage,
    Portfolio,
    Price,
    estimate_capital_gains_tax,
    nisa_allowance,
    simulate_investment,
    summarize,
)

from .conftest import sign_in

BROKER_DIR = Path(__file__).resolve().parents[1] / "src" / "life_helper" / "resources" / "broker_csv"
LIMITS = {
    "tsumitate_annual": 1_200_000,
    "growth_annual": 2_400_000,
    "lifetime_total": 18_000_000,
    "lifetime_growth": 12_000_000,
}


def test_market_value_and_summary():
    stock = Holding(account="tokutei", kind="stock", code="7203", name="トヨタ", quantity=100, cost_total=250_000)
    stock.apply_price(Price(value=3_000, date="2026-09-24", source="stooq"))
    fund = Holding(account="nisa_tsumitate", kind="fund", name="全世界株式", quantity=500_000, cost_total=1_000_000)
    fund.apply_price(Price(value=25_000, date="2026-09-24", source="manual"))
    unknown = Holding(account="ippan", kind="stock", code="1306", name="TOPIX ETF", quantity=10, cost_total=20_000)
    s = summarize(Portfolio(holdings=[stock, fund, unknown]))
    assert s["total_value"] == 300_000 + 1_250_000
    assert s["accounts"]["nisa_tsumitate"]["value"] == 1_250_000
    assert s["missing_prices"] == ["TOPIX ETF"]
    assert s["holdings"][0]["gain"] == 50_000


def test_apply_price_only_when_newer():
    h = Holding(account="tokutei", kind="stock", name="x", quantity=1, cost_total=1, valuation_yen=500)
    h.price = Price(value=500, date="2026-09-24", source="broker_csv")
    assert not h.apply_price(Price(value=400, date="2026-09-20", source="stooq"))
    assert h.market_value() == 500
    assert h.apply_price(Price(value=600, date="2026-09-25", source="stooq"))
    assert h.market_value() == 600


def test_nisa_allowance():
    p = Portfolio(
        holdings=[
            Holding(account="nisa_growth", kind="stock", name="a", quantity=1, cost_total=11_500_000),
            Holding(account="nisa_tsumitate", kind="fund", name="b", quantity=1, cost_total=3_000_000),
        ],
        nisa_annual_used={"2026": NisaUsage(tsumitate=600_000, growth=100_000)},
    )
    r = nisa_allowance(p, 2026, LIMITS)
    assert r["annual_remaining"]["tsumitate"] == 600_000
    # Growth is capped by the remaining lifetime growth allowance (12,000,000 - 11,500,000).
    assert r["annual_remaining"]["growth"] == 500_000
    assert r["lifetime"]["remaining"] == 3_500_000


def test_simulate_investment_zero_return_and_percentiles():
    r = simulate_investment(
        InvestmentSimParams(monthly_contribution=30_000, years=20, expected_return=0, volatility=0, expense_ratio=0)
    )
    assert r["principal"] == 7_200_000 and r["expected_value"] == 7_200_000
    assert r["percentiles"]["p50"] == pytest.approx(7_200_000, rel=1e-6)
    assert r["after_tax"]["tax_saved_by_nisa"] == 0


def test_simulate_investment_compound_and_tax():
    r = simulate_investment(
        InvestmentSimParams(initial=1_000_000, years=10, expected_return=0.05, volatility=0.2, expense_ratio=0)
    )
    assert r["expected_value"] == pytest.approx(1_000_000 * 1.05**10, rel=1e-6)
    gain = r["expected_value"] - 1_000_000
    assert r["after_tax"]["taxable"] == pytest.approx(r["expected_value"] - gain * 0.20315, abs=1)
    assert r["percentiles"]["p10"] < r["percentiles"]["p50"] < r["percentiles"]["p90"]
    # Seeded: identical inputs give identical results.
    assert (
        simulate_investment(InvestmentSimParams(initial=1_000_000, years=10))["percentiles"]
        == simulate_investment(InvestmentSimParams(initial=1_000_000, years=10))["percentiles"]
    )


def test_capital_gains_tax_with_loss_offset():
    r = estimate_capital_gains_tax(
        CapitalGainsParams(
            items=[
                GainItem(kind="sale", amount=100_000),
                GainItem(kind="sale", amount=-30_000),
                GainItem(kind="dividend", amount=10_000),
            ]
        )
    )
    assert r["taxable_amount"] == 80_000 and r["estimated_tax"] == 16_252
    loss = estimate_capital_gains_tax(CapitalGainsParams(items=[GainItem(kind="sale", amount=-50_000)]))
    assert loss["estimated_tax"] == 0 and loss["carryforward_loss"] == 50_000


SBI_CSV = """ポートフォリオ一覧
株式（特定預り）
銘柄（コード）,買付日,数量,取得単価,現在値,評価額
7203 トヨタ自動車,----/--/--,100,2500,3000,"300,000"

投資信託（金額/NISA預り(つみたて投資枠)）
ファンド名,買付日,数量,取得単価,現在値,評価額
eMAXIS Slim 全世界株式,----/--/--,"500,000","20,000","25,000","1,250,000"
"""

RAKUTEN_CSV = """種別,銘柄コード・ティッカー,銘柄,口座,保有数量,平均取得価額,現在値,時価評価額[円]
国内株式,1306,TOPIX連動型上場投資信託,特定,10,2000,2800,"28,000"
投資信託,,楽天・全米株式,NISA成長投資枠,"100,000","15,000","18,000","180,000"
"""


def test_parse_sbi_csv_with_sections_cp932():
    holdings = parse_broker_csv(SBI_CSV.encode("cp932"), load_mapping(BROKER_DIR, "sbi"))
    assert [(h.code, h.name, h.account, h.kind) for h in holdings] == [
        ("7203", "トヨタ自動車", "tokutei", "stock"),
        ("", "eMAXIS Slim 全世界株式", "nisa_tsumitate", "fund"),
    ]
    assert holdings[0].cost_total == 250_000 and holdings[0].market_value() == 300_000
    assert holdings[1].cost_total == pytest.approx(1_000_000)


def test_parse_rakuten_csv():
    holdings = parse_broker_csv(RAKUTEN_CSV.encode("utf-8-sig"), load_mapping(BROKER_DIR, "rakuten"))
    assert [(h.code, h.account, h.kind) for h in holdings] == [
        ("1306", "tokutei", "stock"),
        ("", "nisa_growth", "fund"),
    ]
    assert holdings[1].market_value() == 180_000


def test_parse_rejects_unexpected_header_and_unknown_broker():
    with pytest.raises(BrokerCsvError):
        parse_broker_csv(b"a,b,c\n1,2,3\n", load_mapping(BROKER_DIR, "sbi"))
    with pytest.raises(BrokerCsvError):
        load_mapping(BROKER_DIR, "../etc")


def test_portfolio_api_import_refresh_and_nisa(client, ctx, settings):
    csrf = sign_in(client, ctx)
    h = {"x-csrf-token": csrf}
    view = client.get("/api/portfolio").json()
    assert view["holdings"] == [] and {b["name"] for b in view["brokers"]} == {"sbi", "rakuten"}

    imported = client.post(
        "/api/portfolio/import",
        data={"broker": "rakuten"},
        files={"file": ("a.csv", RAKUTEN_CSV.encode("utf-8-sig"))},
        headers=h,
    ).json()
    assert imported["imported"] == 2 and imported["total_value"] == 208_000
    bad = client.post(
        "/api/portfolio/import", data={"broker": "sbi"}, files={"file": ("a.csv", b"x,y\n1,2\n")}, headers=h
    )
    assert bad.status_code == 400

    settings.stooq_api_key = SecretStr("stooqkey-ABCDEF123456")
    ctx.extras.pop("connectors", None)
    with respx.mock:
        respx.get("https://stooq.com/q/d/l/").mock(
            return_value=httpx.Response(200, text="Date,Open,High,Low,Close,Volume\n2099-01-01,1,1,1,3000,1\n")
        )
        refreshed = client.post("/api/portfolio/refresh-prices", headers=h).json()
    assert refreshed["refresh"]["updated"][0]["code"] == "1306"
    assert refreshed["total_value"] == 30_000 + 180_000
    assert "stooqkey" not in str(refreshed)

    nisa = client.put(
        "/api/portfolio/nisa-usage", json={"year": 2026, "tsumitate": 0, "growth": 1_000_000}, headers=h
    ).json()
    assert nisa["nisa"]["annual"]["growth"]["used"] in (0, 1_000_000)

    added = client.post(
        "/api/portfolio/holdings",
        json={
            "action": "add",
            "account": "ideco",
            "kind": "fund",
            "name": "iDeCo 全世界",
            "quantity": 1000,
            "cost_total": 1000,
        },
        headers=h,
    ).json()
    assert any(x["account"] == "ideco" for x in added["holdings"])
    assert client.post("/api/portfolio/holdings", json={"action": "delete", "id": "nope"}, headers=h).status_code == 400


def test_investment_tools_registered(ctx):
    from life_helper.tools.portfolio_tools import build_tools

    specs = {s.tool.name: s for s in build_tools(ctx)}
    assert set(specs) == {
        "get_portfolio",
        "get_stock_price",
        "refresh_stock_prices",
        "check_nisa_allowance",
        "simulate_investment",
        "estimate_capital_gains_tax",
        "update_holding",
    }
    assert specs["update_holding"].writes and specs["refresh_stock_prices"].writes
    assert not specs["get_portfolio"].writes
