from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
import respx

from life_helper.connectors.base import ConnectorError
from life_helper.connectors.fund_nav import MAX_NAV
from life_helper.connectors.registry import get_connectors
from life_helper.connectors.yahoo_finance import RATE_LIMIT_MESSAGE
from life_helper.market import clock
from life_helper.market.broker_csv import BrokerCsvError, load_mapping, parse_broker_csv
from life_helper.market.clock import market_today
from life_helper.market.funds import (
    _plausible,
    auto_link_funds,
    fund_connectors,
    link_fund,
    refresh_fund_navs,
    suggest_funds,
)
from life_helper.market.portfolio import (
    CapitalGainsParams,
    FundRef,
    GainItem,
    Holding,
    InvestmentSimParams,
    Portfolio,
    PortfolioStore,
    Price,
    estimate_capital_gains_tax,
    previous_business_day,
    simulate_investment,
    summarize,
    yen,
)
from life_helper.market.service import portfolio_store, refresh_stock_prices, stock_price
from life_helper.tools.portfolio_tools import UpdateHoldingParams, apply_holding_update

from .conftest import (
    ALL_COUNTRY,
    FANG_PLUS,
    SCHD,
    SP500,
    mock_daiwa,
    mock_rakuten,
    mock_toushin,
    mock_yahoo,
    sign_in,
    toushin_calls,
    yahoo_chart,
    yahoo_symbols,
)

BROKER_DIR = Path(__file__).resolve().parents[1] / "src" / "life_helper" / "resources" / "broker_csv"


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


def test_legacy_nisa_usage_in_saved_yaml_is_dropped(tmp_path):
    """Portfolios saved before the NISA allowance feature was removed still load, minus the unused key."""
    store = PortfolioStore(tmp_path)
    store.path.parent.mkdir(parents=True, exist_ok=True)
    store.path.write_text(
        "holdings:\n"
        "  - {id: abc, account: nisa_growth, kind: stock, name: a, quantity: 1, cost_total: 100000}\n"
        "nisa_annual_used:\n"
        "  '2026': {tsumitate: 600000, growth: 100000}\n",
        encoding="utf-8",
    )
    saved = store.save(store.load())
    assert [(h.id, h.account, h.cost_total) for h in saved.holdings] == [("abc", "nisa_growth", 100_000)]
    assert "nisa_annual_used" not in store.path.read_text(encoding="utf-8")


def test_legacy_price_without_market_loads(tmp_path):
    """Prices saved before US stocks were supported have no market, symbol or currency and are yen prices."""
    store = PortfolioStore(tmp_path)
    store.path.parent.mkdir(parents=True, exist_ok=True)
    store.path.write_text(
        "holdings:\n"
        "  - {id: abc, account: tokutei, kind: stock, code: '7203', name: トヨタ, quantity: 100,"
        " cost_total: 250000, price: {value: 3000, date: '2026-09-20', source: stooq}}\n",
        encoding="utf-8",
    )
    holding = store.load().holdings[0]
    assert (holding.price.market, holding.price.symbol, holding.price.local_value) == (None, None, None)
    assert holding.price.local_currency == "JPY" and holding.market_value() == 300_000


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


def test_parse_rejects_values_the_portfolio_cannot_hold():
    # Without this check the value is saved as a plain float and only fails later, when summarize() rounds the
    # portfolio to yen. The import replaces every holding, so the whole CSV is refused instead of the one row.
    for csv_text in (
        RAKUTEN_CSV.replace('"28,000"', "1e30"),  # 評価額
        RAKUTEN_CSV.replace(",特定,10,", ",特定,inf,"),  # 数量
        RAKUTEN_CSV.replace(",特定,10,2000,", ",特定,10,-2000,"),  # 取得単価
        RAKUTEN_CSV.replace(",2800,", ",1e30,"),  # 現在値
    ):
        with pytest.raises(BrokerCsvError):
            parse_broker_csv(csv_text.encode("utf-8-sig"), load_mapping(BROKER_DIR, "rakuten"))


def test_portfolio_api_import_refresh_and_holdings(client, ctx, settings):
    csrf = sign_in(client, ctx)
    h = {"x-csrf-token": csrf}
    view = client.get("/api/portfolio").json()
    assert view["holdings"] == [] and {b["name"] for b in view["brokers"]} == {"sbi", "rakuten"}
    assert "nisa" not in view

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

    ctx.extras.pop("connectors", None)
    with respx.mock:
        respx.get(url__startswith="https://query1.finance.yahoo.com/").mock(
            return_value=httpx.Response(200, json=yahoo_chart([(market_today().isoformat(), 3_000)]))
        )
        # The imported fund has no source yet, so the refresh looks for it by name (and finds none here).
        mock_toushin({})
        refreshed = client.post("/api/portfolio/refresh-prices", headers=h).json()
    assert refreshed["refresh"]["updated"][0]["code"] == "1306"
    assert refreshed["refresh_funds"]["auto_link"]["unmatched"][0]["name"] == "楽天・全米株式"
    assert refreshed["total_value"] == 30_000 + 180_000

    # The NISA allowance endpoint is gone (405 because only the SPA catch-all, which is GET, matches the path).
    removed = client.put(
        "/api/portfolio/nisa-usage", json={"year": 2026, "tsumitate": 0, "growth": 1_000_000}, headers=h
    )
    assert removed.status_code in (404, 405)

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


def _yahoo_ready(ctx) -> None:
    ctx.extras.pop("connectors", None)
    get_connectors(ctx)["yahoo_finance"].min_interval_seconds = 0


def test_refresh_prices_values_japanese_and_us_stocks(client, ctx, settings):
    csrf = sign_in(client, ctx)
    with portfolio_store(ctx).transaction() as portfolio:
        portfolio.holdings = [
            Holding(account="tokutei", kind="stock", code="7203", name="トヨタ", quantity=100, cost_total=250_000),
            Holding(account="tokutei", kind="etf", code="1306", name="TOPIX ETF", quantity=10, cost_total=20_000),
            Holding(account="tokutei", kind="stock", code="MSFT", name="Microsoft", quantity=10, cost_total=400_000),
            Holding(account="nisa_growth", kind="stock", code="AAPL", name="Apple", quantity=5, cost_total=100_000),
            Holding(account="ippan", kind="stock", code="NOPE", name="謎の銘柄", quantity=1, cost_total=1_000),
        ]
    _yahoo_ready(ctx)
    with respx.mock:
        route = mock_yahoo({"7203.T": 3_000, "1306.T": 2_500, "MSFT": 100, "AAPL": 200, "JPY=X": 150})
        view = client.post("/api/portfolio/refresh-prices", headers={"x-csrf-token": csrf}).json()

    updated = {u["code"]: u for u in view["refresh"]["updated"]}
    assert (updated["7203"]["market"], updated["7203"]["symbol"], updated["7203"]["currency"]) == (
        "jp",
        "7203.T",
        "JPY",
    )
    assert updated["7203"]["close"] == 3_000 and updated["7203"]["close_jpy"] == 3_000
    # ETFs and REITs keep going through the Japanese market.
    assert (updated["1306"]["market"], updated["1306"]["symbol"]) == ("jp", "1306.T")
    assert (updated["MSFT"]["market"], updated["MSFT"]["symbol"], updated["MSFT"]["currency"]) == (
        "us",
        "MSFT",
        "USD",
    )
    # 100 USD x 150 JPY/USD = 15,000 JPY
    assert updated["MSFT"]["close"] == 100 and updated["MSFT"]["close_jpy"] == 15_000
    assert (updated["MSFT"]["fx_rate"], updated["MSFT"]["fx_date"]) == (150, "2026-09-24")
    assert view["total_value"] == 100 * 3_000 + 10 * 2_500 + 10 * 15_000 + 5 * 30_000
    # One code failing does not stop the others, and the reason names the symbols that were tried.
    assert view["missing_prices"] == ["謎の銘柄"]
    assert view["refresh"]["errors"] == [
        {"code": "NOPE", "error": "NOPE と NOPE.T を照会しましたが、価格データが見つかりませんでした"}
    ]
    # USD/JPY is fetched once for the whole refresh.
    assert yahoo_symbols(route).count("JPY=X") == 1

    msft = next(h for h in view["holdings"] if h["code"] == "MSFT")
    assert msft["price"]["local_value"] == 100 and msft["price"]["local_currency"] == "USD"
    assert msft["price"]["value"] == 15_000 and msft["price"]["fx_source"] == "yahoo_finance"
    assert msft["price"]["source"] == "yahoo_finance"


async def test_us_price_is_not_stored_as_yen_without_fx(ctx, settings):
    with portfolio_store(ctx).transaction() as portfolio:
        portfolio.holdings = [
            Holding(account="tokutei", kind="stock", code="MSFT", name="Microsoft", quantity=10, cost_total=400_000)
        ]
    _yahoo_ready(ctx)
    with respx.mock:
        mock_yahoo({"MSFT": 100})  # USD/JPY is unavailable
        result = await refresh_stock_prices(ctx)
    assert result["updated"] == []
    assert result["errors"][0]["code"] == "MSFT"
    assert "100.0 USD" in result["errors"][0]["error"] and "円換算できませんでした" in result["errors"][0]["error"]
    assert portfolio_store(ctx).load().holdings[0].price is None


async def test_price_cache_without_market_is_refetched(ctx, settings):
    """Entries cached before US stocks were supported have no market or yen value, so they are fetched again."""
    today = date.today()
    portfolio_store(ctx).cache_price(
        "7203", today, {"code": "7203", "symbol": "7203.jp", "date": "2026-09-20", "close": 2_000, "source": "stooq"}
    )
    _yahoo_ready(ctx)
    with respx.mock:
        mock_yahoo({"7203.T": 3_000})
        quote = await stock_price(ctx, "7203", today=today)
        assert (quote["market"], quote["close"], quote["close_jpy"], quote["cached"]) == ("jp", 3_000, 3_000, False)
        assert (await stock_price(ctx, "7203", today=today))["cached"] is True


async def test_incomplete_price_cache_is_refetched(ctx, settings):
    """A cached entry missing the exchange-rate fields is fetched again instead of failing the refresh."""
    today = date.today()
    portfolio_store(ctx).cache_price(
        "MSFT",
        today,
        {
            "code": "MSFT",
            "symbol": "msft.us",
            "market": "us",
            "currency": "USD",
            "close": 100,
            "close_jpy": 15_000,
            "date": "2026-09-20",
            "source": "stooq",
        },
    )
    _yahoo_ready(ctx)
    with respx.mock:
        mock_yahoo({"MSFT": 120, "JPY=X": 150})
        quote = await stock_price(ctx, "MSFT", today=today)
    assert (quote["close"], quote["close_jpy"], quote["fx_rate"], quote["cached"]) == (120, 18_000, 150, False)


def _cached_quote(**extra) -> dict:
    return {
        "code": "7203",
        "symbol": "7203.T",
        "market": "jp",
        "currency": "JPY",
        "close": 3_000,
        "close_jpy": 3_000,
        "date": "2026-09-24",
        "source": "yahoo_finance",
        "fx_rate": None,
        "fx_date": None,
        "fx_source": None,
    } | extra


async def test_price_cache_expires_once_the_session_settles(ctx):
    """A price fetched while its session runs is only cached until the close settles, not for the whole day."""
    today = date.today()
    store = portfolio_store(ctx)
    store.cache_price("7203", today, _cached_quote(valid_until=(datetime.now(UTC) + timedelta(hours=1)).isoformat()))
    _yahoo_ready(ctx)
    with respx.mock:
        route = mock_yahoo({"7203.T": 3_100})
        assert (await stock_price(ctx, "7203", today=today))["cached"] is True
        store.cache_price(
            "7203", today, _cached_quote(valid_until=(datetime.now(UTC) - timedelta(minutes=1)).isoformat())
        )
        quote = await stock_price(ctx, "7203", today=today)
    assert (quote["close"], quote["cached"]) == (3_100, False)
    assert yahoo_symbols(route) == ["7203.T"]


async def test_us_quote_is_cached_until_the_price_or_the_rate_settles(ctx, monkeypatch):
    soon = datetime.now(UTC) + timedelta(hours=1)
    later = soon + timedelta(hours=2)
    _yahoo_ready(ctx)
    connector = get_connectors(ctx)["yahoo_finance"]

    async def previous_close(code, *, today=None, now=None):
        return _cached_quote(code="MSFT", symbol="MSFT", market="us", currency="USD", close=100) | {
            "valid_until": later.isoformat()
        }

    async def usd_jpy(*, today=None, now=None):
        return {
            "pair": "USDJPY",
            "symbol": "JPY=X",
            "date": "2026-09-24",
            "rate": 150,
            "source": "yahoo_finance",
            "valid_until": soon.isoformat(),
        }

    monkeypatch.setattr(connector, "previous_close", previous_close)
    monkeypatch.setattr(connector, "usd_jpy", usd_jpy)
    quote = await stock_price(ctx, "MSFT", today=date.today())
    assert quote["close_jpy"] == 15_000
    assert datetime.fromisoformat(quote["valid_until"]) == soon


async def test_quote_the_portfolio_cannot_hold_is_not_stored(ctx, settings):
    """A price outside the stored range would make summarize() fail for every holding, not just this one."""
    with portfolio_store(ctx).transaction() as portfolio:
        portfolio.holdings = [
            Holding(account="tokutei", kind="stock", code="7203", name="トヨタ", quantity=10, cost_total=20_000)
        ]
    _yahoo_ready(ctx)
    # A number too large to round to yen, and one too large to even be turned into a float.
    for close in (1e30, 10**400):
        with respx.mock:
            mock_yahoo({"7203.T": close})
            result = await refresh_stock_prices(ctx)
        assert result["updated"] == []
        assert result["errors"] == [{"code": "7203", "error": "Yahoo Finance から取得した株価が不正です"}]
        assert portfolio_store(ctx).load().holdings[0].price is None
        assert portfolio_store(ctx).cached_price("7203", market_today()) is None


async def test_converted_price_beyond_the_limit_is_refused(ctx, monkeypatch):
    """The yen value decides: a close and a rate that are each within range can still multiply out of it."""
    _yahoo_ready(ctx)
    connector = get_connectors(ctx)["yahoo_finance"]

    async def previous_close(code, *, today=None, now=None):
        return _cached_quote(code="MSFT", symbol="MSFT", market="us", currency="USD", close=1e9)

    async def usd_jpy(*, today=None, now=None):
        return {"pair": "USDJPY", "symbol": "JPY=X", "date": "2026-09-24", "rate": 150, "source": "yahoo_finance"}

    monkeypatch.setattr(connector, "previous_close", previous_close)
    monkeypatch.setattr(connector, "usd_jpy", usd_jpy)
    with pytest.raises(ConnectorError):
        await stock_price(ctx, "MSFT", today=date.today())
    assert portfolio_store(ctx).cached_price("MSFT", date.today()) is None


async def test_price_cache_beyond_the_limit_is_refetched(ctx):
    """Entries cached before prices were bounded are fetched again, instead of failing the refresh all day."""
    today = date.today()
    store = portfolio_store(ctx)
    store.cache_price(
        "MSFT",
        today,
        _cached_quote(code="MSFT", symbol="MSFT", market="us", currency="USD", close=100, close_jpy=1e30)
        | {"fx_rate": 1e28},
    )
    store.cache_price(
        "USDJPY", today, {"pair": "USDJPY", "symbol": "JPY=X", "date": "2026-09-24", "rate": 1e28, "source": "stooq"}
    )
    _yahoo_ready(ctx)
    with respx.mock:
        mock_yahoo({"MSFT": 120, "JPY=X": 150})
        quote = await stock_price(ctx, "MSFT", today=today)
    assert (quote["close_jpy"], quote["fx_rate"], quote["cached"]) == (18_000, 150, False)


async def test_rate_limit_stops_the_refresh(ctx):
    with portfolio_store(ctx).transaction() as portfolio:
        portfolio.holdings = [
            Holding(account="tokutei", kind="stock", code=code, name=code, quantity=1, cost_total=1_000)
            for code in ("7203", "8306", "MSFT")
        ]
    _yahoo_ready(ctx)
    with respx.mock:
        route = respx.get(url__startswith="https://query1.finance.yahoo.com/").mock(
            return_value=httpx.Response(429, text="Too Many Requests")
        )
        result = await refresh_stock_prices(ctx)
    assert result["updated"] == []
    assert [e["code"] for e in result["errors"]] == ["7203", "8306", "MSFT"]
    assert all("利用制限" in e["error"] for e in result["errors"])
    # Once Yahoo throttles, the remaining holdings are not requested.
    assert route.call_count == 1


async def test_rate_limit_on_the_exchange_rate_also_stops_the_refresh(ctx):
    with portfolio_store(ctx).transaction() as portfolio:
        portfolio.holdings = [
            Holding(account="tokutei", kind="stock", code=code, name=code, quantity=1, cost_total=1_000)
            for code in ("AAPL", "MSFT")
        ]
    _yahoo_ready(ctx)
    with respx.mock:
        # Registered first, so it wins over the catch-all chart route.
        respx.get("https://query1.finance.yahoo.com/v8/finance/chart/JPY=X").mock(
            return_value=httpx.Response(429, text="Too Many Requests")
        )
        route = mock_yahoo({"AAPL": 200, "MSFT": 100})
        result = await refresh_stock_prices(ctx)
    assert [e["code"] for e in result["errors"]] == ["AAPL", "MSFT"]
    assert "円換算できませんでした" in result["errors"][0]["error"] and "利用制限" in result["errors"][0]["error"]
    # MSFT was never requested, so its reason must not repeat what was fetched for AAPL.
    assert result["errors"][1]["error"] == RATE_LIMIT_MESSAGE
    assert yahoo_symbols(route) == ["AAPL"]


def test_investment_tools_registered(ctx):
    from life_helper.tools.portfolio_tools import build_tools

    specs = {s.tool.name: s for s in build_tools(ctx)}
    assert set(specs) == {
        "get_portfolio",
        "get_stock_price",
        "refresh_stock_prices",
        "get_fund_nav",
        "refresh_fund_navs",
        "simulate_investment",
        "estimate_capital_gains_tax",
        "update_holding",
    }
    assert specs["update_holding"].writes and specs["refresh_stock_prices"].writes
    assert not specs["get_portfolio"].writes
    # Funds are priced by the fund library and the managers, so the tool belongs to other connectors than stocks.
    assert specs["refresh_fund_navs"].writes
    assert specs["refresh_fund_navs"].connector == ("toushin_lib", "rakuten_csv", "daiwa_csv")
    assert specs["refresh_stock_prices"].connector == "yahoo_finance"
    assert specs["refresh_stock_prices"].allowed(["yahoo_finance"])
    # An automation gets the fund tools only when it selected every source they can reach.
    assert specs["refresh_fund_navs"].allowed(["toushin_lib", "rakuten_csv", "daiwa_csv"])
    assert not specs["refresh_fund_navs"].allowed(["daiwa_csv"])
    assert not specs["refresh_stock_prices"].allowed(["daiwa_csv"])


# -- fund NAVs (投資信託の基準価額) --------------------------------------------------------------------------

NAV_DAY = previous_business_day(market_today())
OLDER_DAY = previous_business_day(NAV_DAY)
ALL_COUNTRY_ISIN = ALL_COUNTRY["isinCd"]
SP500_ISIN = SP500["isinCd"]


def _jp(day: date) -> str:
    """A 基準日 the way the fund library's CSV writes it."""
    return f"{day.year}年{day.month:02d}月{day.day:02d}日"


def _funds_ready(ctx) -> None:
    for connector in fund_connectors(ctx).values():
        connector.min_interval_seconds = 0


def _fund(quantity: float, *, code: str = ALL_COUNTRY_ISIN, price_unit: float = 10_000, **kwargs) -> Holding:
    return Holding(
        account="nisa_tsumitate",
        kind="fund",
        name=kwargs.pop("name", "eMAXIS Slim 全世界株式（オール・カントリー）"),
        quantity=quantity,
        cost_total=kwargs.pop("cost_total", 1_000_000),
        fund=FundRef(
            provider=kwargs.pop("provider", "toushin_lib"),
            fund_code=code,
            price_unit=price_unit,
            manager=kwargs.pop("manager", "三菱UFJアセットマネジメント"),
        ),
        **kwargs,
    )


def test_fund_value_uses_the_price_unit_of_the_fund():
    fund = _fund(500_000)
    fund.apply_price(Price(value=25_000, date="2026-09-24", source="toushin_lib"))
    assert fund.market_value() == 1_250_000
    # A fund quoted per 1 unit instead of per 10,000 must not be valued 10,000 times too low.
    per_unit = _fund(500, price_unit=1)
    per_unit.apply_price(Price(value=2.5, date="2026-09-24", source="toushin_lib"))
    assert per_unit.market_value() == Decimal("1250")


def test_fund_value_has_no_floating_point_error():
    fund = _fund(1_182_307.279)
    fund.apply_price(Price(value=11_699, date="2026-09-24", source="toushin_lib"))
    # 1,182,307.279 口 ÷ 10,000 × 11,699 円 exactly; in float this is 1383181.2857021003.
    assert fund.market_value() == Decimal("1383181.2857021")
    assert float(fund.market_value()) != 1_182_307.279 * 11_699 / 10_000
    # Yen are rounded half up, not with the banker's rounding that float round() uses.
    half = _fund(1_000_005)
    half.apply_price(Price(value=5_000, date="2026-09-24", source="toushin_lib"))
    assert half.market_value() == Decimal("500002.5") and yen(half.market_value()) == 500_003


def test_a_value_no_longer_allowed_still_opens_the_screen(client, ctx):
    # A file written before the paths that save a holding checked their values (or edited by hand) can hold a
    # number too large to round with the decimal context. The screen must still open, or it could never be fixed.
    csrf = sign_in(client, ctx)
    oversized = Holding(account="tokutei", kind="stock", code="7203", name="トヨタ", quantity=1e30, cost_total=1e30)
    oversized.apply_price(Price(value=3_000, date="2026-09-24", source="manual"))
    with portfolio_store(ctx).transaction() as portfolio:
        portfolio.holdings = [oversized]
    view = client.get("/api/portfolio")
    assert view.status_code == 200
    assert view.json()["holdings"][0]["value"] == 3 * 10**33
    repaired = client.post(
        "/api/portfolio/holdings",
        json={"action": "update", "id": oversized.id, "quantity": 100, "cost_total": 200_000},
        headers={"x-csrf-token": csrf},
    )
    assert repaired.status_code == 200 and repaired.json()["total_value"] == 300_000


def test_official_nav_replaces_a_broker_csv_nav_dated_with_the_import_day():
    def imported(kind: str = "fund") -> Holding:
        h = Holding(account="nisa_growth", kind=kind, name="x", quantity=10_000, cost_total=1, valuation_yen=40_000)
        # The broker CSV was imported on 9/24, but the NAV in it is the one of 9/18 (before a long weekend).
        h.price = Price(value=43_966, date="2026-09-24", source="broker_csv")
        return h

    fund = imported()
    assert fund.apply_price(Price(value=43_966, date="2026-09-18", source="toushin_lib"))
    assert (fund.price.source, fund.valuation_yen) == ("toushin_lib", None)
    # A NAV far older than the import is not the newer one, so the broker's value stays.
    ancient = imported()
    assert not ancient.apply_price(Price(value=30_000, date="2026-09-01", source="toushin_lib"))
    assert ancient.price.source == "broker_csv" and ancient.market_value() == 40_000
    # Only an official NAV does that: a hand-entered one, or a stock price, still has to be newer.
    assert not imported().apply_price(Price(value=1, date="2026-09-18", source="manual"))
    assert not imported("stock").apply_price(Price(value=1, date="2026-09-18", source="stooq"))
    # Between two official NAVs the newer one still wins.
    official = _fund(10_000, price=Price(value=44_842, date="2026-09-24", source="toushin_lib"))
    assert not official.apply_price(Price(value=43_966, date="2026-09-18", source="toushin_lib"))


def test_links_to_the_retired_mufg_api_move_to_the_fund_library(tmp_path):
    price = {"value": 25_000, "date": "2026-09-01", "source": "mufg_api"}
    base = {"account": "nisa_tsumitate", "kind": "fund", "quantity": 1, "cost_total": 1, "price": price}
    by_isin = Holding.model_validate(
        base
        | {
            "name": "a",
            "fund": {
                "provider": "mufg_api",
                "fund_code": "0331418A",
                "isin": "JP90C000H1T1",
                "association_code": "0331418A",
                "manager": "三菱UFJアセットマネジメント",
            },
        }
    )
    assert by_isin.fund.model_dump(exclude={"price_unit", "source_url"}) == {
        "provider": "toushin_lib",
        "fund_code": "JP90C000H1T1",
        "manager": "三菱UFJアセットマネジメント",
        "isin": "JP90C000H1T1",
        "association_code": "0331418A",
    }
    # A malformed ISIN loses to a valid ISIN in the fund code.
    by_code = Holding.model_validate(
        base | {"name": "b", "fund": {"provider": "mufg_api", "fund_code": "jp90c000gkc6", "isin": "JP90"}}
    )
    assert (by_code.fund.provider, by_code.fund.fund_code, by_code.fund.isin) == ("toushin_lib", SP500_ISIN, SP500_ISIN)
    # Without any ISIN the link is dropped, so the fund is matched by name again; its price stays.
    dropped = Holding.model_validate(base | {"name": "c", "fund": {"provider": "mufg_api", "fund_code": "253425"}})
    assert dropped.fund is None and dropped.price.source == "mufg_api" and dropped.market_value() == Decimal("2.5")

    store = PortfolioStore(tmp_path)
    store.path.parent.mkdir(parents=True)
    store.path.write_text(
        "holdings:\n- account: ideco\n  kind: fund\n  name: d\n  quantity: 1\n  cost_total: 1\n"
        "  fund: {provider: mufg_api, fund_code: 0331418A, isin: JP90C000H1T1}\n",
        encoding="utf-8",
    )
    assert store.load().holdings[0].fund.provider == "toushin_lib"


def test_saved_automations_keep_the_fund_tools_after_the_mufg_api_is_retired(client, ctx):
    from life_helper.automation.models import Automation
    from life_helper.tools.registry import build_tools

    saved = Automation(name="x", prompt="y", connectors=["mufg_api", "rakuten_csv", "daiwa_csv", "toushin_lib"])
    assert saved.connectors == ["toushin_lib", "rakuten_csv", "daiwa_csv"]
    assert "refresh_fund_navs" in {s.tool.name for s in build_tools(ctx, connectors=saved.connectors)}
    # An automation opened on the screen before the switch is sent back with the old name.
    csrf = sign_in(client, ctx)
    body = {
        "name": "基準価額",
        "prompt": "基準価額を更新",
        "schedule": {"kind": "daily", "time": "09:00"},
        "connectors": ["mufg_api", "rakuten_csv", "daiwa_csv"],
    }
    created = client.post("/api/automations", json=body, headers={"x-csrf-token": csrf})
    assert created.status_code == 200 and created.json()["connectors"] == ["toushin_lib", "rakuten_csv", "daiwa_csv"]


def test_summary_flags_old_navs_and_funds_without_a_source():
    linked = _fund(500_000)
    linked.apply_price(Price(value=25_000, date="2026-09-01", source="toushin_lib"))
    unlinked = Holding(account="ideco", kind="fund", name="自動取得できないファンド", quantity=1_000, cost_total=1_000)
    unlinked.apply_price(Price(value=12_000, date="2026-09-24", source="manual"))
    s = summarize(Portfolio(holdings=[linked, unlinked]), today=date(2026, 9, 25))
    assert s["stale_prices"] == ["eMAXIS Slim 全世界株式（オール・カントリー）"]
    assert s["manual_funds"] == [{"id": unlinked.id, "name": "自動取得できないファンド"}]
    assert s["holdings"][0]["auto_nav"] is True and s["holdings"][0]["price_unit"] == 10_000
    assert s["holdings"][1]["auto_nav"] is False and s["holdings"][1]["stale"] is False
    # A NAV published for the previous business day is the newest one there is, so it is not old.
    fresh = summarize(Portfolio(holdings=[linked]), today=date(2026, 9, 2))
    assert fresh["stale_prices"] == []


async def test_refresh_fund_navs_values_holdings(ctx):
    with portfolio_store(ctx).transaction() as portfolio:
        portfolio.holdings = [_fund(500_000), _fund(300_000, code=SP500_ISIN, name="eMAXIS Slim 米国株式")]
    _funds_ready(ctx)
    with respx.mock:
        mock_toushin({ALL_COUNTRY_ISIN: (25_341, _jp(NAV_DAY))})
        result = await refresh_fund_navs(ctx)
    assert [(u["code"], u["nav"], u["date"], u["source"]) for u in result["updated"]] == [
        (ALL_COUNTRY_ISIN, 25_341, NAV_DAY.isoformat(), "toushin_lib")
    ]
    assert result["updated"][0]["source_url"].startswith("https://toushin-lib.fwg.ne.jp/")
    assert result["updated"][0]["official_name"] == ALL_COUNTRY["fundNm"]
    # The fund the library has no NAV for is reported, and the others are still updated.
    assert result["errors"] == [
        {"code": SP500_ISIN, "error": f"{SP500_ISIN} の基準価額が投資信託協会のライブラリーにありません"}
    ]
    holdings = portfolio_store(ctx).load().holdings
    assert holdings[0].market_value() == Decimal("1267050") and holdings[1].price is None
    assert holdings[0].price.fetched_at and holdings[0].price.source_url
    # The 協会コード confirmed on the fund page is saved with the link.
    assert holdings[0].fund.association_code == ALL_COUNTRY["associFundCd"]


async def test_refresh_fund_navs_keeps_the_newer_nav_and_survives_failures(ctx):
    with portfolio_store(ctx).transaction() as portfolio:
        portfolio.holdings = [_fund(500_000)]
        portfolio.holdings[0].apply_price(
            Price(value=25_000, date=NAV_DAY.isoformat(), source="toushin_lib", fetched_at="keep-me")
        )
    _funds_ready(ctx)
    with respx.mock:
        mock_toushin({ALL_COUNTRY_ISIN: (10_000, _jp(OLDER_DAY))})
        stale = await refresh_fund_navs(ctx)
    assert stale["updated"] == [] and stale["errors"] == []
    kept = portfolio_store(ctx).load().holdings[0]
    assert kept.price.value == 25_000 and kept.price.date == NAV_DAY.isoformat()

    with respx.mock:
        respx.route(url__startswith="https://toushin-lib.fwg.ne.jp").mock(return_value=httpx.Response(503, text=""))
        failed = await refresh_fund_navs(ctx)
    assert failed["updated"] == [] and "HTTP 503" in failed["errors"][0]["error"]
    # A failed fetch leaves the previous NAV and valuation in place instead of clearing them.
    after = portfolio_store(ctx).load().holdings[0]
    assert after.price.value == 25_000 and after.market_value() == 1_250_000


async def test_refresh_fund_navs_updates_every_source_independently(ctx):
    daiwa = _fund(250_000, provider="daiwa_csv", code="3346", name="iFreeNEXT FANG+インデックス", manager="大和")
    rakuten = _fund(100_000, provider="rakuten_csv", code="100124", name="楽天・全米株式", manager="楽天投信")
    with portfolio_store(ctx).transaction() as portfolio:
        portfolio.holdings = [_fund(500_000), daiwa, rakuten]
    _funds_ready(ctx)
    with respx.mock:
        mock_toushin({ALL_COUNTRY_ISIN: (25_341, _jp(NAV_DAY))})
        mock_daiwa({"3346": (28_251, NAV_DAY.strftime("%Y%m%d"))})
        mock_rakuten({"100124": (13_999, NAV_DAY.strftime("%Y/%m/%d"))})
        result = await refresh_fund_navs(ctx)
    # Every source is asked with its own connector, and each NAV keeps the source it came from.
    assert [(u["code"], u["source"]) for u in result["updated"]] == [
        (ALL_COUNTRY_ISIN, "toushin_lib"),
        ("3346", "daiwa_csv"),
        ("100124", "rakuten_csv"),
    ]
    assert result["errors"] == []
    # 500,000 / 10,000 x 25,341 + 250,000 / 10,000 x 28,251 + 100,000 / 10,000 x 13,999
    assert summarize(portfolio_store(ctx).load())["total_value"] == 1_267_050 + 706_275 + 139_990

    with respx.mock:
        mock_toushin({ALL_COUNTRY_ISIN: (25_500, _jp(NAV_DAY))})
        mock_rakuten({"100124": (14_100, NAV_DAY.strftime("%Y/%m/%d"))})
        respx.get(url__startswith="https://www.daiwa-am.co.jp").mock(return_value=httpx.Response(503, text=""))
        second = await refresh_fund_navs(ctx)
    # One source being down never blocks the others, and its fund keeps the NAV it had.
    assert [u["code"] for u in second["updated"]] == [ALL_COUNTRY_ISIN, "100124"]
    assert second["errors"][0]["code"] == "3346" and "HTTP 503" in second["errors"][0]["error"]
    assert portfolio_store(ctx).load().holdings[1].price.value == 28_251


SBI_SP500 = "eMAXIS Slim 米国株式(S&P500)"
SBI_SCHD = "楽天・シュワブ・高配当株式・米国ファンド(四半期決算型)(楽天・SCHD)"


def _imported(name: str, nav: float, quantity: float = 800_000, account: str = "nisa_tsumitate") -> Holding:
    """A fund as a broker CSV import leaves it: no source, and a NAV dated with the import day."""
    return Holding(
        account=account,
        kind="fund",
        name=name,
        quantity=quantity,
        cost_total=1_000_000,
        price=Price(value=nav, date=market_today().isoformat(), source="broker_csv"),
        valuation_yen=quantity * nav / 10_000,
    )


async def test_refresh_links_funds_whose_name_exactly_one_official_fund_has(ctx):
    with portfolio_store(ctx).transaction() as portfolio:
        portfolio.holdings = [_imported(SBI_SP500, 43_966), _imported(SBI_SCHD, 13_248, 769_127, "nisa_growth")]
    _funds_ready(ctx)
    with respx.mock:
        mock_toushin({SP500_ISIN: (44_842, _jp(NAV_DAY)), SCHD["isinCd"]: (13_246, _jp(NAV_DAY))})
        result = await refresh_fund_navs(ctx)
    links = result["auto_link"]
    assert [(link["name"], link["code"]) for link in links["linked"]] == [
        (SBI_SP500, SP500_ISIN),
        (SBI_SCHD, SCHD["isinCd"]),
    ]
    assert links["ambiguous"] == links["unmatched"] == links["errors"] == []
    # The official NAVs replace the broker CSV ones although those carry the (later) import day.
    assert [(u["code"], u["nav"], u["date"]) for u in result["updated"]] == [
        (SP500_ISIN, 44_842, NAV_DAY.isoformat()),
        (SCHD["isinCd"], 13_246, NAV_DAY.isoformat()),
    ]
    holdings = portfolio_store(ctx).load().holdings
    assert [h.fund.provider for h in holdings] == ["toushin_lib", "toushin_lib"]
    assert holdings[1].fund.association_code == SCHD["associFundCd"] and holdings[1].fund.manager == "楽天投信投資顧問"
    # 800,000 / 10,000 x 44,842 + 769,127 / 10,000 x 13,246
    assert summarize(portfolio_store(ctx).load())["total_value"] == 3_587_360 + 1_018_786

    with respx.mock:
        route = mock_toushin({SP500_ISIN: (44_900, _jp(NAV_DAY)), SCHD["isinCd"]: (13_300, _jp(NAV_DAY))})
        again = await refresh_fund_navs(ctx)
    # Linked funds are not searched again.
    assert again["auto_link"]["linked"] == [] and toushin_calls(route, "/FdsWeb/FDST999900/fundDataSearch") == []


async def test_refresh_leaves_funds_it_cannot_link_unambiguously_to_the_user(ctx):
    twin = SP500 | {"isinCd": "JP90C000ZZZ1", "associFundCd": "0331999Z", "fundNm": "eMAXIS Slim米国株式(S&P500)"}
    manual = Holding(
        account="ideco", kind="fund", name=SBI_SP500, quantity=1, cost_total=1, fund=FundRef(provider="manual")
    )
    with portfolio_store(ctx).transaction() as portfolio:
        portfolio.holdings = [
            _imported(SBI_SP500, 43_966),
            _imported("ひふみプラス", 70_000),
            # Quoted per unit rather than per 10,000 units: linking it would value it 10,000 times too high.
            _imported("eMAXIS Slim 全世界株式(オール・カントリー)", 2.5341),
            manual,
        ]
    _funds_ready(ctx)
    with respx.mock:
        route = mock_toushin({ALL_COUNTRY_ISIN: (25_341, _jp(NAV_DAY))}, funds=(ALL_COUNTRY, SP500, twin))
        result = await refresh_fund_navs(ctx)
    links = result["auto_link"]
    assert links["linked"] == [] and links["errors"] == []
    assert [(a["name"], a["reason"]) for a in links["ambiguous"]] == [
        (SBI_SP500, "同じ名前の公式ファンドが 2 件あります"),
        ("eMAXIS Slim 全世界株式(オール・カントリー)", "保有中の基準価額と公式の基準価額が大きく違います"),
    ]
    assert [u["name"] for u in links["unmatched"]] == ["ひふみプラス"]
    # The fund set to manual entry is not searched at all, although its name is searched for another holding.
    searched = [
        json.loads(c.request.content)["t_keyword"] for c in toushin_calls(route, "/FdsWeb/FDST999900/fundDataSearch")
    ]
    assert searched.count(SBI_SP500) == 1
    assert [h.fund for h in portfolio_store(ctx).load().holdings][:3] == [None, None, None]

    # A complete search is kept for the day; the next day (a fresh cache) searches again.
    fund_connectors(ctx)["toushin_lib"]._searched.clear()
    with respx.mock:
        respx.route(url__startswith="https://toushin-lib.fwg.ne.jp").mock(return_value=httpx.Response(503, text=""))
        down = await refresh_fund_navs(ctx)
    # A search that failed is an error, never evidence that no official fund has the name.
    assert down["auto_link"]["unmatched"] == [] and len(down["auto_link"]["errors"]) == 3
    assert "HTTP 503" in down["auto_link"]["errors"][0]["error"]


def test_plausible_excludes_exactly_double_or_half():
    held = _imported("f", 10_000)
    assert _plausible(held, 20_000) is False
    assert _plausible(held, 5_000) is False
    assert _plausible(held, 19_999) is True
    assert _plausible(held, 5_001) is True


async def test_auto_link_needs_the_search_and_the_fund_page_to_agree(ctx):
    # The search lists the S&P 500 fund under another fund's 協会コード; the fund page of its ISIN says otherwise.
    listed = SP500 | {"associFundCd": ALL_COUNTRY["associFundCd"]}
    with portfolio_store(ctx).transaction() as portfolio:
        portfolio.holdings = [_imported(SBI_SP500, 43_966)]
    _funds_ready(ctx)
    with respx.mock:
        search = mock_toushin({}, funds=(listed,)).side_effect
        pages = mock_toushin({SP500_ISIN: (44_842, _jp(NAV_DAY))}).side_effect

        def handler(request: httpx.Request) -> httpx.Response:
            return (search if request.method == "POST" else pages)(request)

        respx.route(url__startswith="https://toushin-lib.fwg.ne.jp").mock(side_effect=handler)
        result = await auto_link_funds(ctx)
    assert result["linked"] == [] and "一致しない" in result["errors"][0]["error"]
    assert portfolio_store(ctx).load().holdings[0].fund is None


async def test_auto_link_fails_closed_when_the_search_omits_the_association_code(ctx):
    # The search result has no usable 協会コード at all, so the required agreement with the fund page is
    # unestablished; this must not be treated as if the codes simply matched.
    unlisted = SP500 | {"associFundCd": ""}
    with portfolio_store(ctx).transaction() as portfolio:
        portfolio.holdings = [_imported(SBI_SP500, 43_966)]
    _funds_ready(ctx)
    with respx.mock:
        search = mock_toushin({}, funds=(unlisted,)).side_effect
        pages = mock_toushin({SP500_ISIN: (44_842, _jp(NAV_DAY))}).side_effect

        def handler(request: httpx.Request) -> httpx.Response:
            return (search if request.method == "POST" else pages)(request)

        respx.route(url__startswith="https://toushin-lib.fwg.ne.jp").mock(side_effect=handler)
        result = await auto_link_funds(ctx)
    assert result["linked"] == [] and "一致しない" in result["errors"][0]["error"]
    assert portfolio_store(ctx).load().holdings[0].fund is None


async def test_auto_link_needs_the_search_and_the_fund_page_to_agree_on_the_name(ctx):
    # The search returns the S&P 500 fund's ISIN and 協会コード under another fund's name; the fund page says
    # otherwise, so the name the search claimed must not be enough to link the holding.
    mislabeled = SP500 | {"fundNm": "ひふみプラス"}
    with portfolio_store(ctx).transaction() as portfolio:
        portfolio.holdings = [_imported("ひふみプラス", 43_966)]
    _funds_ready(ctx)
    with respx.mock:
        search = mock_toushin({}, funds=(mislabeled,)).side_effect
        pages = mock_toushin({SP500_ISIN: (44_842, _jp(NAV_DAY))}).side_effect

        def handler(request: httpx.Request) -> httpx.Response:
            return (search if request.method == "POST" else pages)(request)

        respx.route(url__startswith="https://toushin-lib.fwg.ne.jp").mock(side_effect=handler)
        result = await auto_link_funds(ctx)
    assert result["linked"] == [] and "ファンド名が一致しない" in result["errors"][0]["error"]
    assert portfolio_store(ctx).load().holdings[0].fund is None


async def test_auto_link_fetches_a_fund_two_holdings_name_differently_only_once(ctx):
    # The same fund in two accounts: one broker writes the official name, the other appends the nickname.
    with portfolio_store(ctx).transaction() as portfolio:
        portfolio.holdings = [_imported(SBI_SCHD, 13_248), _imported(SCHD["fundNm"], 13_248, 100_000, "tokutei")]
    _funds_ready(ctx)
    with respx.mock:
        route = mock_toushin({SCHD["isinCd"]: (13_246, _jp(NAV_DAY))})
        result = await auto_link_funds(ctx)
    assert [link["code"] for link in result["linked"]] == [SCHD["isinCd"], SCHD["isinCd"]]
    # Both names were searched, but the fund page and its CSV were fetched once for the fund they share.
    assert len(toushin_calls(route, "/FdsWeb/FDST999900/fundDataSearch")) > 1
    assert len(toushin_calls(route, "/FdsWeb/FDST030000")) == 1
    assert len(toushin_calls(route, "/FdsWeb/FDST030000/csv-file-download")) == 1


async def test_auto_link_never_overrides_a_change_made_while_searching(ctx, monkeypatch):
    with portfolio_store(ctx).transaction() as portfolio:
        portfolio.holdings = [_imported(SBI_SP500, 43_966), _imported(SBI_SCHD, 13_248)]
    _funds_ready(ctx)
    connector = fund_connectors(ctx)["toushin_lib"]
    original = connector.search_funds

    async def edited_while_searching(name, **kwargs):
        with portfolio_store(ctx).transaction() as portfolio:
            if name == SBI_SP500:
                portfolio.holdings[0].name = "ひふみプラス"
            else:
                portfolio.holdings[1].fund = FundRef(provider="manual")
        return await original(name, **kwargs)

    monkeypatch.setattr(connector, "search_funds", edited_while_searching)
    with respx.mock:
        mock_toushin({SP500_ISIN: (44_842, _jp(NAV_DAY)), SCHD["isinCd"]: (13_246, _jp(NAV_DAY))})
        result = await auto_link_funds(ctx)
    # Both funds were found and their NAVs fetched; only the change made meanwhile kept them from being linked.
    assert result["linked"] == [] and result["errors"] == [] and result["ambiguous"] == []
    holdings = portfolio_store(ctx).load().holdings
    assert holdings[0].name == "ひふみプラス" and holdings[0].fund is None
    assert holdings[1].fund.provider == "manual"


async def test_link_fund_by_code_shows_the_official_name_of_the_csv_manager(ctx):
    holding = Holding(account="tokutei", kind="fund", name="FANG+", quantity=250_000, cost_total=500_000)
    with portfolio_store(ctx).transaction() as portfolio:
        portfolio.holdings = [holding]
    _funds_ready(ctx)
    with respx.mock:
        mock_toushin({})
        mock_daiwa({"3346": (28_251, NAV_DAY.strftime("%Y%m%d"))})
        # The library knows no fund by that name here, and 大和 publishes no list to match names against.
        assert (await suggest_funds(ctx, "iFreeNEXT FANG+インデックス"))["candidates"] == []
        result = await link_fund(ctx, holding.id, "daiwa_csv", "3346")
    # The code came from the official fund page, and the name it answers with is what confirms the link.
    assert result["official_name"] == FANG_PLUS
    assert result["fund"]["source_url"] == "https://www.daiwa-am.co.jp/funds/detail/3346/detail_top.html"
    linked = portfolio_store(ctx).load().holdings[0]
    assert linked.price.source == "daiwa_csv" and linked.market_value() == Decimal("706275")

    with respx.mock:
        mock_daiwa({})
        unknown = await link_fund(ctx, holding.id, "daiwa_csv", "9999")
    assert "CSV ではありません" in unknown["error"]
    # The failed link left the confirmed one, and its NAV, untouched.
    assert portfolio_store(ctx).load().holdings[0].fund.fund_code == "3346"


async def test_link_fund_rejects_a_response_when_the_holding_changed_while_fetching(ctx, monkeypatch):
    holding = Holding(account="tokutei", kind="fund", name="全世界株式", quantity=500_000, cost_total=1_000_000)
    with portfolio_store(ctx).transaction() as portfolio:
        portfolio.holdings = [holding]
    _funds_ready(ctx)
    connector = fund_connectors(ctx)["toushin_lib"]
    original_fund_nav = connector.fund_nav

    async def changed_while_fetching(*args, **kwargs):
        with portfolio_store(ctx).transaction() as portfolio:
            portfolio.holdings[0].name = "変更後のファンド"
        return await original_fund_nav(*args, **kwargs)

    monkeypatch.setattr(connector, "fund_nav", changed_while_fetching)
    with respx.mock:
        mock_toushin({ALL_COUNTRY_ISIN: (25_341, _jp(NAV_DAY))})
        result = await link_fund(ctx, holding.id, "toushin_lib", ALL_COUNTRY_ISIN)
    assert "変更されました" in result["error"]
    changed = portfolio_store(ctx).load().holdings[0]
    assert changed.name == "変更後のファンド" and changed.fund is None and changed.price is None


async def test_funds_set_to_manual_entry_are_left_alone(ctx):
    manual = Holding(
        account="ideco",
        kind="fund",
        name="自動取得未対応ファンド",
        quantity=1_000,
        cost_total=10_000,
        fund=FundRef(provider="manual"),
    )
    manual.apply_price(Price(value=15_000, date="2026-09-01", source="manual"))
    with portfolio_store(ctx).transaction() as portfolio:
        portfolio.holdings = [manual]
    _funds_ready(ctx)
    with respx.mock:
        # No request is made at all: the user chose to enter this NAV by hand.
        route = respx.route(url__startswith="https://toushin-lib.fwg.ne.jp")
        result = await refresh_fund_navs(ctx)
    assert not route.called
    assert result["updated"] == [] and result["manual"] == [{"id": manual.id, "name": "自動取得未対応ファンド"}]
    assert portfolio_store(ctx).load().holdings[0].market_value() == 1_500


async def test_link_fund_needs_a_confirmed_fund_code(client, ctx):
    csrf = sign_in(client, ctx)
    headers = {"x-csrf-token": csrf}
    holding = Holding(account="nisa_tsumitate", kind="fund", name="全世界株式", quantity=500_000, cost_total=1_000_000)
    with portfolio_store(ctx).transaction() as portfolio:
        portfolio.holdings = [holding]
    _funds_ready(ctx)
    with respx.mock:
        mock_toushin({ALL_COUNTRY_ISIN: (25_341, _jp(NAV_DAY))})
        suggested = client.get(
            "/api/portfolio/fund-candidates", params={"name": "eMAXIS Slim 全世界株式"}, headers=headers
        ).json()
        # Names alone only produce candidates: a similar name is offered, not exact, and nothing is linked yet.
        assert [(c["fund_code"], c["exact"]) for c in suggested["candidates"]] == [(ALL_COUNTRY_ISIN, False)]
        assert portfolio_store(ctx).load().holdings[0].fund is None

        linked = client.post(
            "/api/portfolio/fund-link",
            json={"id": holding.id, "provider": "toushin_lib", "fund_code": ALL_COUNTRY_ISIN},
            headers=headers,
        ).json()
        unknown = client.post(
            "/api/portfolio/fund-link",
            json={"id": holding.id, "provider": "toushin_lib", "fund_code": "JP90C0000000"},
            headers=headers,
        )
    # The official name comes from the fund page of that ISIN, for the user to check the link against.
    assert linked["link"]["official_name"] == ALL_COUNTRY["fundNm"]
    assert linked["holdings"][0]["fund"]["association_code"] == ALL_COUNTRY["associFundCd"]
    assert linked["holdings"][0]["fund"]["price_unit"] == 10_000
    assert linked["total_value"] == 1_267_050
    assert unknown.status_code == 400 and "見つかりませんでした" in unknown.json()["detail"]
    # The failed link left the confirmed one untouched.
    assert portfolio_store(ctx).load().holdings[0].fund.fund_code == ALL_COUNTRY_ISIN

    manual = client.post(
        "/api/portfolio/fund-link", json={"id": holding.id, "provider": "manual"}, headers=headers
    ).json()
    assert manual["holdings"][0]["auto_nav"] is False and manual["manual_funds"][0]["id"] == holding.id


def test_refresh_prices_updates_stocks_and_funds_independently(client, ctx, settings):
    csrf = sign_in(client, ctx)
    with portfolio_store(ctx).transaction() as portfolio:
        portfolio.holdings = [
            Holding(account="tokutei", kind="stock", code="7203", name="トヨタ", quantity=100, cost_total=250_000),
            _fund(500_000),
            _fund(300_000, code=SP500_ISIN, name="eMAXIS Slim 米国株式", cost_total=500_000),
        ]
    _yahoo_ready(ctx)
    _funds_ready(ctx)
    with respx.mock:
        mock_yahoo({"7203.T": 3_000})
        mock_toushin({ALL_COUNTRY_ISIN: (25_341, _jp(NAV_DAY)), SP500_ISIN: (30_000, _jp(NAV_DAY))})
        view = client.post("/api/portfolio/refresh-prices", headers={"x-csrf-token": csrf}).json()
    assert [u["code"] for u in view["refresh"]["updated"]] == ["7203"]
    assert [u["code"] for u in view["refresh_funds"]["updated"]] == [ALL_COUNTRY_ISIN, SP500_ISIN]
    assert "Yahoo Finance" in view["refresh"]["note"] and "投資信託協会" in view["refresh_funds"]["note"]
    # 100 x 3,000 + 500,000 / 10,000 x 25,341 + 300,000 / 10,000 x 30,000
    assert view["total_value"] == 300_000 + 1_267_050 + 900_000
    assert view["missing_prices"] == [] and view["stale_prices"] == []


def test_manual_nav_links_source_price_unit_and_basis_date_together(client, ctx):
    csrf = sign_in(client, ctx)
    holding = Holding(account="ideco", kind="fund", name="手入力ファンド", quantity=1_200, cost_total=1_000)
    with portfolio_store(ctx).transaction() as portfolio:
        portfolio.holdings = [holding]
    response = client.post(
        "/api/portfolio/fund-link",
        json={
            "id": holding.id,
            "provider": "manual",
            "price_unit": 1,
            "nav": 2.5,
            "price_date": NAV_DAY.isoformat(),
        },
        headers={"x-csrf-token": csrf},
    )
    assert response.status_code == 200
    saved = portfolio_store(ctx).load().holdings[0]
    assert saved.fund.provider == "manual" and saved.fund.price_unit == 1
    assert (saved.price.value, saved.price.date, saved.price.source) == (2.5, NAV_DAY.isoformat(), "manual")


def test_manual_nav_rejects_an_excessive_value_or_future_basis_date(client, ctx):
    csrf = sign_in(client, ctx)
    holding = Holding(account="ideco", kind="fund", name="手入力ファンド", quantity=1_200, cost_total=1_000)
    with portfolio_store(ctx).transaction() as portfolio:
        portfolio.holdings = [holding]
    too_large = client.post(
        "/api/portfolio/fund-link",
        json={"id": holding.id, "provider": "manual", "nav": MAX_NAV + 1, "price_date": market_today().isoformat()},
        headers={"x-csrf-token": csrf},
    )
    future = client.post(
        "/api/portfolio/fund-link",
        json={
            "id": holding.id,
            "provider": "manual",
            "nav": 2.5,
            "price_date": (market_today() + timedelta(days=1)).isoformat(),
        },
        headers={"x-csrf-token": csrf},
    )
    assert too_large.status_code == 422
    assert future.status_code == 400
    assert portfolio_store(ctx).load().holdings[0].price is None


async def test_manual_nav_rejects_a_price_unit_the_valuation_cannot_use(client, ctx):
    # The valuation divides by the price unit, so a unit below 1 口 would make it far larger than any holding.
    csrf = sign_in(client, ctx)
    holding = Holding(account="ideco", kind="fund", name="手入力ファンド", quantity=1_000, cost_total=1_000)
    with portfolio_store(ctx).transaction() as portfolio:
        portfolio.holdings = [holding]
    too_small = client.post(
        "/api/portfolio/fund-link",
        json={"id": holding.id, "provider": "manual", "price_unit": 1e-300},
        headers={"x-csrf-token": csrf},
    )
    assert too_small.status_code == 422
    assert await link_fund(ctx, holding.id, "manual", price_unit=0.5) == {
        "error": "価格単位は 1 以上 1,000,000 以下で指定してください"
    }
    assert portfolio_store(ctx).load().holdings[0].fund is None


async def test_a_quote_whose_price_unit_is_out_of_range_is_not_linked(ctx):
    # The connectors quote a unit of their own, so the answer is checked before a link or a refresh saves it.
    holding = Holding(account="nisa_tsumitate", kind="fund", name=ALL_COUNTRY["fundNm"], quantity=1_000, cost_total=1)
    with portfolio_store(ctx).transaction() as portfolio:
        portfolio.holdings = [holding]
    _funds_ready(ctx)
    get_connectors(ctx)["toushin_lib"].price_unit = 0.001
    with respx.mock:
        mock_toushin({ALL_COUNTRY_ISIN: (25_341, _jp(NAV_DAY))})
        linked = await link_fund(ctx, holding.id, "toushin_lib", ALL_COUNTRY_ISIN)
        refreshed = await refresh_fund_navs(ctx)
    assert "価格単位" in linked["error"]
    assert refreshed["auto_link"]["errors"] and "価格単位" in refreshed["auto_link"]["errors"][0]["error"]
    saved = portfolio_store(ctx).load().holdings[0]
    assert saved.fund is None and saved.price is None


async def test_a_refreshed_quote_whose_price_unit_is_out_of_range_keeps_the_saved_nav(ctx):
    kept = _fund(500_000)
    kept.apply_price(Price(value=25_000, date=OLDER_DAY.isoformat(), source="toushin_lib"))
    daiwa = _fund(250_000, provider="daiwa_csv", code="3346", name="iFreeNEXT FANG+インデックス", manager="大和")
    with portfolio_store(ctx).transaction() as portfolio:
        portfolio.holdings = [kept, daiwa]
    _funds_ready(ctx)
    get_connectors(ctx)["toushin_lib"].price_unit = 0.001
    with respx.mock:
        mock_toushin({ALL_COUNTRY_ISIN: (25_341, _jp(NAV_DAY))})
        mock_daiwa({"3346": (28_251, NAV_DAY.strftime("%Y%m%d"))})
        result = await refresh_fund_navs(ctx)
    # The refused fund keeps the NAV and the unit it was linked with; the fund of another source still updates.
    assert [u["code"] for u in result["updated"]] == ["3346"]
    assert [(e["code"], "価格単位" in e["error"]) for e in result["errors"]] == [(ALL_COUNTRY_ISIN, True)]
    unchanged, updated = portfolio_store(ctx).load().holdings
    assert (unchanged.price.value, unchanged.price.date) == (25_000, OLDER_DAY.isoformat())
    assert unchanged.fund.price_unit == 10_000 and updated.price.value == 28_251


def test_market_today_is_the_japanese_date_even_when_utc_is_still_yesterday(monkeypatch):
    """The container runs on UTC, so between 00:00 and 09:00 JST today would look like tomorrow to it."""

    class _Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 9, 24, 15, 30, tzinfo=UTC).astimezone(tz)

    monkeypatch.setattr(clock, "datetime", _Clock)
    assert clock.market_today() == date(2026, 9, 25)


async def test_manual_nav_accepts_the_current_japanese_date(ctx):
    holding = Holding(account="ideco", kind="fund", name="手入力ファンド", quantity=1_200, cost_total=1_000)
    with portfolio_store(ctx).transaction() as portfolio:
        portfolio.holdings = [holding]
    today = await link_fund(ctx, holding.id, "manual", manual_nav=2.5, price_date=market_today())
    assert today["ok"] is True
    assert portfolio_store(ctx).load().holdings[0].price.date == market_today().isoformat()


async def test_link_fund_rejects_incomplete_or_non_manual_nav_input(ctx):
    holding = Holding(account="ideco", kind="fund", name="手入力ファンド", quantity=1_200, cost_total=1_000)
    with portfolio_store(ctx).transaction() as portfolio:
        portfolio.holdings = [holding]
    incomplete = await link_fund(ctx, holding.id, "manual", manual_nav=2.5)
    non_manual = await link_fund(ctx, holding.id, "toushin_lib", ALL_COUNTRY_ISIN, manual_nav=2.5, price_date=NAV_DAY)
    too_large = await link_fund(ctx, holding.id, "manual", manual_nav=MAX_NAV + 1, price_date=NAV_DAY)
    future = await link_fund(ctx, holding.id, "manual", manual_nav=2.5, price_date=market_today() + timedelta(days=1))
    retired = await link_fund(ctx, holding.id, "mufg_api", "0331418A")
    assert "両方指定" in incomplete["error"]
    assert "手入力のときだけ" in non_manual["error"]
    assert "基準価額" in too_large["error"]
    assert "未来の日付" in future["error"]
    assert "対応していないデータ提供元" in retired["error"]


async def test_relinking_a_fund_drops_the_price_of_the_previous_one(ctx):
    holding = _fund(500_000)
    holding.apply_price(Price(value=25_000, date=NAV_DAY.isoformat(), source="toushin_lib"))
    with portfolio_store(ctx).transaction() as portfolio:
        portfolio.holdings = [holding]
    _funds_ready(ctx)
    with respx.mock:
        # The NAV of the fund now picked is older than the one already stored for the fund picked before.
        mock_toushin({SP500_ISIN: (30_000, _jp(OLDER_DAY))})
        result = await link_fund(ctx, holding.id, "toushin_lib", SP500_ISIN)
    assert result["fund"]["fund_code"] == SP500_ISIN
    # The holding is another fund now, so it is valued with that fund's NAV instead of keeping the previous one.
    relinked = portfolio_store(ctx).load().holdings[0]
    assert (relinked.price.value, relinked.price.date) == (30_000, OLDER_DAY.isoformat())
    assert relinked.market_value() == 1_500_000


async def test_manual_fallback_can_use_another_price_unit(ctx):
    holding = Holding(account="ideco", kind="fund", name="1 口単位のファンド", quantity=1_200, cost_total=1_000)
    with portfolio_store(ctx).transaction() as portfolio:
        portfolio.holdings = [holding]
    assert (await link_fund(ctx, holding.id, "manual", "", 1))["fund"]["price_unit"] == 1
    with portfolio_store(ctx).transaction() as portfolio:
        portfolio.holdings[0].apply_price(Price(value=2.5, date=NAV_DAY.isoformat(), source="manual"))
    assert portfolio_store(ctx).load().holdings[0].market_value() == Decimal("3000")


def test_csv_reimport_keeps_the_confirmed_fund_link(client, ctx):
    csrf = sign_in(client, ctx)
    headers = {"x-csrf-token": csrf}

    def upload(csv: str) -> dict:
        return client.post(
            "/api/portfolio/import",
            data={"broker": "rakuten"},
            files={"file": ("a.csv", csv.encode("utf-8-sig"))},
            headers=headers,
        ).json()

    upload(RAKUTEN_CSV)
    with portfolio_store(ctx).transaction() as portfolio:
        fund = next(h for h in portfolio.holdings if h.kind == "fund")
        fund.fund = FundRef(provider="toushin_lib", fund_code=ALL_COUNTRY_ISIN, manager="三菱UFJアセットマネジメント")
    # A CSV import replaces the holdings, but the fund the user already confirmed stays linked.
    again = [h for h in upload(RAKUTEN_CSV)["holdings"] if h["kind"] == "fund"]
    assert again[0]["fund"]["fund_code"] == ALL_COUNTRY_ISIN and again[0]["auto_nav"] is True
    # A fund whose name changed is not carried over just because it looks similar; the refresh matches it again.
    renamed = [h for h in upload(RAKUTEN_CSV.replace("楽天・全米株式", "楽天・全米株式インデックス"))["holdings"]]
    assert [h["fund"] for h in renamed if h["kind"] == "fund"] == [None]


def test_renaming_a_holding_clears_the_confirmed_fund_link(ctx):
    holding = _fund(
        500_000, price=Price(value=20_000, date="2024-05-01", source="toushin_lib"), valuation_yen=1_000_000
    )
    with portfolio_store(ctx).transaction() as portfolio:
        portfolio.holdings = [holding]
    kept = UpdateHoldingParams(action="update", id=holding.id, quantity=600_000)
    assert apply_holding_update(ctx, kept) == {"ok": True, "id": holding.id}
    assert portfolio_store(ctx).load().holdings[0].fund.fund_code == ALL_COUNTRY_ISIN
    # The row may now be a different fund, so the link has to be confirmed again instead of being reused.
    renamed = UpdateHoldingParams(action="update", id=holding.id, name="ひふみプラス")
    apply_holding_update(ctx, renamed)
    stored = portfolio_store(ctx).load().holdings[0]
    # The NAV of the previous fund must not value the new one, so the price and the stored valuation go too.
    assert (stored.fund, stored.price, stored.valuation_yen, stored.market_value()) == (None, None, None, None)


def test_changing_the_security_code_of_a_holding_clears_the_previous_instruments_data(ctx):
    holding = _fund(
        500_000, price=Price(value=20_000, date="2024-05-01", source="toushin_lib"), valuation_yen=1_000_000
    )
    holding.code = "0331418A"
    with portfolio_store(ctx).transaction() as portfolio:
        portfolio.holdings = [holding]
    # The code now identifies another instrument, so neither its NAV source nor its price may be reused.
    recoded = UpdateHoldingParams(action="update", id=holding.id, code="03312179")
    apply_holding_update(ctx, recoded)
    stored = portfolio_store(ctx).load().holdings[0]
    assert (stored.fund, stored.price, stored.valuation_yen, stored.market_value()) == (None, None, None, None)


def test_changing_the_kind_of_a_holding_keeps_a_price_given_in_the_same_update(ctx):
    holding = _fund(500_000, price=Price(value=20_000, date="2024-05-01", source="toushin_lib"))
    with portfolio_store(ctx).transaction() as portfolio:
        portfolio.holdings = [holding]
    retyped = UpdateHoldingParams(action="update", id=holding.id, kind="stock", quantity=100, price=3_000)
    apply_holding_update(ctx, retyped)
    stored = portfolio_store(ctx).load().holdings[0]
    assert (stored.fund, stored.price.value, stored.market_value()) == (None, 3_000, Decimal("300000"))


def _csv_stock() -> Holding:
    # Broker CSVs quote US stocks in USD but value them in yen, and both end up on the holding as they are.
    return Holding(
        account="tokutei",
        kind="stock",
        code="MSFT",
        name="Microsoft",
        quantity=10,
        cost_total=500_000.25,
        price=Price(value=400, date="2026-09-24", source="broker_csv"),
        valuation_yen=600_000,
    )


def _store(ctx, *holdings: Holding) -> None:
    with portfolio_store(ctx).transaction() as portfolio:
        portfolio.holdings = list(holdings)


def test_buying_more_of_a_csv_holding_scales_its_csv_valuation(ctx):
    holding = _csv_stock()
    _store(ctx, holding)
    bought = UpdateHoldingParams(action="update", id=holding.id, quantity=15, cost_total=800_000.25)
    assert apply_holding_update(ctx, bought) == {"ok": True, "id": holding.id}
    stored = portfolio_store(ctx).load().holdings[0]
    # 15 × 400 would value the USD price as yen; the CSV's own yen valuation per share is kept instead.
    assert (stored.quantity, stored.cost_total, stored.valuation_yen) == (15, 800_000.25, 900_000)
    assert stored.price.value == 400 and stored.market_value() == Decimal("900000")
    view = summarize(portfolio_store(ctx).load())["holdings"][0]
    assert (view["value"], view["cost_total"], view["cost_total_exact"], view["gain"]) == (
        900_000,
        800_000,
        800_000.25,
        100_000,
    )


def test_changing_only_the_cost_keeps_the_valuation(ctx):
    holding = _csv_stock()
    _store(ctx, holding)
    apply_holding_update(ctx, UpdateHoldingParams(action="update", id=holding.id, cost_total=700_000))
    stored = portfolio_store(ctx).load().holdings[0]
    assert (stored.quantity, stored.valuation_yen, stored.price.value) == (10, 600_000, 400)
    assert summarize(portfolio_store(ctx).load())["holdings"][0]["gain"] == -100_000


def test_a_holding_priced_by_the_app_is_revalued_from_its_price(ctx):
    holding = _fund(500_000, price=Price(value=20_000, date="2026-09-24", source="toushin_lib"))
    _store(ctx, holding)
    apply_holding_update(ctx, UpdateHoldingParams(action="update", id=holding.id, quantity=600_000))
    assert portfolio_store(ctx).load().holdings[0].market_value() == Decimal("1200000")


def test_buying_again_after_reaching_zero_does_not_value_the_csv_price_as_yen(ctx):
    holding = _csv_stock()
    _store(ctx, holding)
    apply_holding_update(ctx, UpdateHoldingParams(action="update", id=holding.id, quantity=0))
    assert portfolio_store(ctx).load().holdings[0].market_value() == Decimal("0")
    # Nothing is left to scale, and the CSV price may be in USD, so the value stays unknown until a price is fetched.
    apply_holding_update(ctx, UpdateHoldingParams(action="update", id=holding.id, quantity=5))
    stored = portfolio_store(ctx).load().holdings[0]
    assert (stored.quantity, stored.price, stored.valuation_yen, stored.market_value()) == (5, None, None, None)
    assert summarize(portfolio_store(ctx).load())["missing_prices"] == ["Microsoft"]


def test_an_update_based_on_totals_that_changed_meanwhile_is_rejected(ctx):
    holding = _csv_stock()
    _store(ctx, holding)
    bought = UpdateHoldingParams(
        action="update",
        id=holding.id,
        quantity=15,
        cost_total=800_000.25,
        expected_quantity=10,
        expected_cost_total=500_000.25,
    )
    assert apply_holding_update(ctx, bought) == {"ok": True, "id": holding.id}
    # Sending the same update again (a double click or a retry) neither fails nor adds the purchase twice.
    assert apply_holding_update(ctx, bought) == {"ok": True, "id": holding.id}
    stored = portfolio_store(ctx).load().holdings[0]
    assert (stored.quantity, stored.cost_total, stored.valuation_yen) == (15, 800_000.25, 900_000)
    # Another purchase computed from the totals before the first one would silently undo it.
    stale = bought.model_copy(update={"quantity": 12, "cost_total": 620_000.25})
    updated_at = portfolio_store(ctx).load().updated_at
    assert apply_holding_update(ctx, stale)["conflict"] is True
    # A refused update is not saved at all.
    stored = portfolio_store(ctx).load()
    assert (stored.holdings[0].quantity, stored.updated_at) == (15, updated_at)


def test_an_update_that_would_overflow_the_valuation_is_rejected(ctx):
    holding = _csv_stock()
    holding.quantity, holding.valuation_yen = 1, 1_000_000_000_000
    _store(ctx, holding)
    huge = UpdateHoldingParams(action="update", id=holding.id, quantity=1_000_000_000_000)
    updated_at = portfolio_store(ctx).load().updated_at
    assert "error" in apply_holding_update(ctx, huge)
    stored = portfolio_store(ctx).load()
    assert (stored.holdings[0].quantity, stored.holdings[0].valuation_yen, stored.updated_at) == (
        1,
        1_000_000_000_000,
        updated_at,
    )
    # The valuation of another instrument is dropped, not scaled, so it cannot overflow.
    renamed = huge.model_copy(update={"name": "Apple", "code": "AAPL"})
    assert apply_holding_update(ctx, renamed) == {"ok": True, "id": holding.id}
    stored = portfolio_store(ctx).load().holdings[0]
    assert (stored.quantity, stored.price, stored.valuation_yen) == (1_000_000_000_000, None, None)
    for bad in ({"quantity": float("inf")}, {"quantity": 10**13}, {"cost_total": 10**16}, {"price": float("nan")}):
        with pytest.raises(ValueError):
            UpdateHoldingParams(action="update", id=holding.id, **bad)


def test_portfolio_api_updates_quantity_and_cost_of_an_imported_holding(client, ctx):
    headers = {"x-csrf-token": sign_in(client, ctx)}
    client.post(
        "/api/portfolio/import",
        data={"broker": "rakuten"},
        files={"file": ("a.csv", RAKUTEN_CSV.encode("utf-8-sig"))},
        headers=headers,
    )
    fund = next(h for h in client.get("/api/portfolio").json()["holdings"] if h["kind"] == "fund")
    assert (fund["quantity"], fund["cost_total_exact"], fund["value"]) == (100_000, 150_000, 180_000)
    bought = {
        "action": "update",
        "id": fund["id"],
        "quantity": fund["quantity"] + 20_000,
        "cost_total": fund["cost_total_exact"] + 30_000,
        "expected_quantity": fund["quantity"],
        "expected_cost_total": fund["cost_total_exact"],
    }
    view = client.post("/api/portfolio/holdings", json=bought, headers=headers)
    assert view.status_code == 200
    updated = next(h for h in view.json()["holdings"] if h["id"] == fund["id"])
    assert (updated["quantity"], updated["cost_total"], updated["value"], updated["gain"]) == (
        120_000,
        180_000,
        216_000,
        36_000,
    )
    stale = bought | {"quantity": 110_000, "cost_total": 165_000}
    conflict = client.post("/api/portfolio/holdings", json=stale, headers=headers)
    assert conflict.status_code == 409 and "最新の値" in conflict.json()["detail"]
    too_many = client.post(
        "/api/portfolio/holdings", json={"action": "update", "id": fund["id"], "quantity": 10**13}, headers=headers
    )
    assert too_many.status_code == 422
    assert (
        next(h for h in client.get("/api/portfolio").json()["holdings"] if h["id"] == fund["id"])["quantity"] == 120_000
    )
