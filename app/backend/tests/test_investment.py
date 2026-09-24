from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
import respx
from pydantic import SecretStr

from life_helper.connectors.registry import get_connectors
from life_helper.market.broker_csv import BrokerCsvError, load_mapping, parse_broker_csv
from life_helper.market.funds import link_fund, refresh_fund_navs
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

from .conftest import mock_mufg, mock_stooq, sign_in, stooq_csv

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

    settings.stooq_api_key = SecretStr("stooqkey-ABCDEF123456")
    ctx.extras.pop("connectors", None)
    with respx.mock:
        respx.get("https://stooq.com/q/d/l/").mock(
            return_value=httpx.Response(200, text=stooq_csv(3_000, date.today().isoformat()))
        )
        refreshed = client.post("/api/portfolio/refresh-prices", headers=h).json()
    assert refreshed["refresh"]["updated"][0]["code"] == "1306"
    assert refreshed["total_value"] == 30_000 + 180_000
    assert "stooqkey" not in str(refreshed)

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


def _stooq_ready(ctx, settings) -> None:
    settings.stooq_api_key = SecretStr("stooqkey-ABCDEF123456")
    ctx.extras.pop("connectors", None)
    get_connectors(ctx)["stooq"].min_interval_seconds = 0


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
    _stooq_ready(ctx, settings)
    with respx.mock:
        route = mock_stooq({"7203.jp": 3_000, "1306.jp": 2_500, "msft.us": 100, "aapl.us": 200, "usdjpy": 150})
        view = client.post("/api/portfolio/refresh-prices", headers={"x-csrf-token": csrf}).json()

    updated = {u["code"]: u for u in view["refresh"]["updated"]}
    assert (updated["7203"]["market"], updated["7203"]["symbol"], updated["7203"]["currency"]) == (
        "jp",
        "7203.jp",
        "JPY",
    )
    assert updated["7203"]["close"] == 3_000 and updated["7203"]["close_jpy"] == 3_000
    # ETFs and REITs keep going through the Japanese market.
    assert (updated["1306"]["market"], updated["1306"]["symbol"]) == ("jp", "1306.jp")
    assert (updated["MSFT"]["market"], updated["MSFT"]["symbol"], updated["MSFT"]["currency"]) == (
        "us",
        "msft.us",
        "USD",
    )
    # 100 USD x 150 JPY/USD = 15,000 JPY
    assert updated["MSFT"]["close"] == 100 and updated["MSFT"]["close_jpy"] == 15_000
    assert (updated["MSFT"]["fx_rate"], updated["MSFT"]["fx_date"]) == (150, "2026-09-24")
    assert view["total_value"] == 100 * 3_000 + 10 * 2_500 + 10 * 15_000 + 5 * 30_000
    # One code failing does not stop the others, and the reason names the symbols that were tried.
    assert view["missing_prices"] == ["謎の銘柄"]
    assert view["refresh"]["errors"] == [
        {"code": "NOPE", "error": "nope.us と nope.jp を照会しましたが、価格データが見つかりませんでした"}
    ]
    # USD/JPY is fetched once for the whole refresh.
    assert [c.request.url.params["s"] for c in route.calls].count("usdjpy") == 1

    msft = next(h for h in view["holdings"] if h["code"] == "MSFT")
    assert msft["price"]["local_value"] == 100 and msft["price"]["local_currency"] == "USD"
    assert msft["price"]["value"] == 15_000 and msft["price"]["fx_source"] == "stooq"


async def test_us_price_is_not_stored_as_yen_without_fx(ctx, settings):
    with portfolio_store(ctx).transaction() as portfolio:
        portfolio.holdings = [
            Holding(account="tokutei", kind="stock", code="MSFT", name="Microsoft", quantity=10, cost_total=400_000)
        ]
    _stooq_ready(ctx, settings)
    with respx.mock:
        mock_stooq({"msft.us": 100})  # USD/JPY is unavailable
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
    _stooq_ready(ctx, settings)
    with respx.mock:
        mock_stooq({"7203.jp": 3_000})
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
    _stooq_ready(ctx, settings)
    with respx.mock:
        mock_stooq({"msft.us": 120, "usdjpy": 150})
        quote = await stock_price(ctx, "MSFT", today=today)
    assert (quote["close"], quote["close_jpy"], quote["fx_rate"], quote["cached"]) == (120, 18_000, 150, False)


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
    # Funds are priced by the fund manager, so the tool belongs to another connector than the stock one.
    assert specs["refresh_fund_navs"].writes and specs["refresh_fund_navs"].connector == "mufg_api"
    assert specs["refresh_stock_prices"].connector == "stooq"


# -- fund NAVs (投資信託の基準価額) --------------------------------------------------------------------------

NAV_DAY = previous_business_day(date.today())
OLDER_DAY = previous_business_day(NAV_DAY)


def _mufg_ready(ctx) -> None:
    get_connectors(ctx)["mufg_api"].min_interval_seconds = 0


def _fund(quantity: float, *, code: str = "0331418A", price_unit: float = 10_000, **kwargs) -> Holding:
    return Holding(
        account="nisa_tsumitate",
        kind="fund",
        name=kwargs.pop("name", "eMAXIS Slim 全世界株式（オール・カントリー）"),
        quantity=quantity,
        cost_total=kwargs.pop("cost_total", 1_000_000),
        fund=FundRef(provider="mufg_api", fund_code=code, price_unit=price_unit, manager="三菱UFJアセットマネジメント"),
        **kwargs,
    )


def test_fund_value_uses_the_price_unit_of_the_fund():
    fund = _fund(500_000)
    fund.apply_price(Price(value=25_000, date="2026-09-24", source="mufg_api"))
    assert fund.market_value() == 1_250_000
    # A fund quoted per 1 unit instead of per 10,000 must not be valued 10,000 times too low.
    per_unit = _fund(500, price_unit=1)
    per_unit.apply_price(Price(value=2.5, date="2026-09-24", source="mufg_api"))
    assert per_unit.market_value() == Decimal("1250")


def test_fund_value_has_no_floating_point_error():
    fund = _fund(1_182_307.279)
    fund.apply_price(Price(value=11_699, date="2026-09-24", source="mufg_api"))
    # 1,182,307.279 口 ÷ 10,000 × 11,699 円 exactly; in float this is 1383181.2857021003.
    assert fund.market_value() == Decimal("1383181.2857021")
    assert float(fund.market_value()) != 1_182_307.279 * 11_699 / 10_000
    # Yen are rounded half up, not with the banker's rounding that float round() uses.
    half = _fund(1_000_005)
    half.apply_price(Price(value=5_000, date="2026-09-24", source="mufg_api"))
    assert half.market_value() == Decimal("500002.5") and yen(half.market_value()) == 500_003


def test_summary_flags_old_navs_and_funds_without_a_source():
    linked = _fund(500_000)
    linked.apply_price(Price(value=25_000, date="2026-09-01", source="mufg_api"))
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
        portfolio.holdings = [_fund(500_000), _fund(300_000, code="0331C180", name="eMAXIS Slim 米国株式")]
    _mufg_ready(ctx)
    with respx.mock:
        mock_mufg({"0331418A": (25_341, NAV_DAY.strftime("%Y%m%d"))})
        result = await refresh_fund_navs(ctx)
    assert [(u["code"], u["nav"], u["date"], u["source"]) for u in result["updated"]] == [
        ("0331418A", 25_341, NAV_DAY.isoformat(), "mufg_api")
    ]
    assert result["updated"][0]["source_url"].startswith("https://developer.am.mufg.jp/")
    # The fund the API has no NAV for is reported, and the others are still updated.
    assert result["errors"] == [{"code": "0331C180", "error": "0331C180 のファンド情報が見つかりませんでした"}]
    holdings = portfolio_store(ctx).load().holdings
    assert holdings[0].market_value() == Decimal("1267050") and holdings[1].price is None
    assert holdings[0].price.fetched_at and holdings[0].price.source_url


async def test_refresh_fund_navs_keeps_the_newer_nav_and_survives_failures(ctx):
    with portfolio_store(ctx).transaction() as portfolio:
        portfolio.holdings = [_fund(500_000)]
        portfolio.holdings[0].apply_price(
            Price(value=25_000, date=NAV_DAY.isoformat(), source="broker_csv", fetched_at="keep-me")
        )
    _mufg_ready(ctx)
    with respx.mock:
        mock_mufg({"0331418A": (10_000, OLDER_DAY.strftime("%Y%m%d"))})
        stale = await refresh_fund_navs(ctx)
    assert stale["updated"] == [] and stale["errors"] == []
    kept = portfolio_store(ctx).load().holdings[0]
    assert kept.price.value == 25_000 and kept.price.date == NAV_DAY.isoformat()

    with respx.mock:
        respx.get(url__startswith="https://developer.am.mufg.jp").mock(return_value=httpx.Response(503, text=""))
        failed = await refresh_fund_navs(ctx)
    assert failed["updated"] == [] and "HTTP 503" in failed["errors"][0]["error"]
    # A failed fetch leaves the previous NAV and valuation in place instead of clearing them.
    after = portfolio_store(ctx).load().holdings[0]
    assert after.price.value == 25_000 and after.market_value() == 1_250_000


async def test_funds_without_a_source_are_left_to_manual_entry(ctx):
    manual = Holding(account="ideco", kind="fund", name="自動取得未対応ファンド", quantity=1_000, cost_total=10_000)
    manual.apply_price(Price(value=15_000, date="2026-09-01", source="manual"))
    with portfolio_store(ctx).transaction() as portfolio:
        portfolio.holdings = [manual]
    _mufg_ready(ctx)
    with respx.mock:
        # No request is made at all: nothing tells us which official fund this is.
        route = respx.get(url__startswith="https://developer.am.mufg.jp")
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
    _mufg_ready(ctx)
    with respx.mock:
        mock_mufg({"0331418A": (25_341, NAV_DAY.strftime("%Y%m%d"))})
        suggested = client.get(
            "/api/portfolio/fund-candidates", params={"name": "eMAXIS Slim 全世界株式"}, headers=headers
        ).json()
        # Names alone only produce candidates: similar fund names stay in the list and nothing is linked yet.
        assert [c["fund_code"] for c in suggested["candidates"]] == ["0331418A", "0331C180"]
        assert suggested["candidates"][0]["score"] > suggested["candidates"][1]["score"]
        assert portfolio_store(ctx).load().holdings[0].fund is None

        linked = client.post(
            "/api/portfolio/fund-link",
            json={"id": holding.id, "provider": "mufg_api", "fund_code": "0331418A"},
            headers=headers,
        ).json()
        unknown = client.post(
            "/api/portfolio/fund-link",
            json={"id": holding.id, "provider": "mufg_api", "fund_code": "99999999"},
            headers=headers,
        )
    assert linked["link"]["official_name"] == "ｅＭＡＸＩＳ Ｓｌｉｍ 全世界株式（オール・カントリー）"
    assert linked["holdings"][0]["fund"]["association_code"] == "0331418A"
    assert linked["holdings"][0]["fund"]["price_unit"] == 10_000
    assert linked["total_value"] == 1_267_050
    assert unknown.status_code == 400
    # The failed link left the confirmed one untouched.
    assert portfolio_store(ctx).load().holdings[0].fund.fund_code == "0331418A"

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
            _fund(300_000, code="0331C180", name="eMAXIS Slim 米国株式", cost_total=500_000),
        ]
    _stooq_ready(ctx, settings)
    _mufg_ready(ctx)
    with respx.mock:
        mock_stooq({"7203.jp": 3_000})
        mock_mufg({"0331418A": (25_341, NAV_DAY.strftime("%Y%m%d")), "0331C180": (30_000, NAV_DAY.strftime("%Y%m%d"))})
        view = client.post("/api/portfolio/refresh-prices", headers={"x-csrf-token": csrf}).json()
    assert [u["code"] for u in view["refresh"]["updated"]] == ["7203"]
    assert [u["code"] for u in view["refresh_funds"]["updated"]] == ["0331418A", "0331C180"]
    assert "Stooq" in view["refresh"]["note"] and "公式 API" in view["refresh_funds"]["note"]
    # 100 x 3,000 + 500,000 / 10,000 x 25,341 + 300,000 / 10,000 x 30,000
    assert view["total_value"] == 300_000 + 1_267_050 + 900_000
    assert view["missing_prices"] == [] and view["stale_prices"] == []


async def test_relinking_a_fund_drops_the_price_of_the_previous_one(ctx):
    holding = _fund(500_000)
    holding.apply_price(Price(value=25_000, date=NAV_DAY.isoformat(), source="mufg_api"))
    with portfolio_store(ctx).transaction() as portfolio:
        portfolio.holdings = [holding]
    _mufg_ready(ctx)
    with respx.mock:
        # The NAV of the fund now picked is older than the one already stored for the fund picked before.
        mock_mufg({"0331C180": (30_000, OLDER_DAY.strftime("%Y%m%d"))})
        result = await link_fund(ctx, holding.id, "mufg_api", "0331C180")
    assert result["fund"]["fund_code"] == "0331C180"
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
        fund.fund = FundRef(provider="mufg_api", fund_code="0331418A", manager="三菱UFJアセットマネジメント")
    # A CSV import replaces the holdings, but the fund the user already confirmed stays linked.
    again = [h for h in upload(RAKUTEN_CSV)["holdings"] if h["kind"] == "fund"]
    assert again[0]["fund"]["fund_code"] == "0331418A" and again[0]["auto_nav"] is True
    # A fund the user never confirmed is not linked just because its name looks similar.
    renamed = [h for h in upload(RAKUTEN_CSV.replace("楽天・全米株式", "楽天・全米株式インデックス"))["holdings"]]
    assert [h["fund"] for h in renamed if h["kind"] == "fund"] == [None]


def test_renaming_a_holding_clears_the_confirmed_fund_link(ctx):
    holding = _fund(500_000)
    with portfolio_store(ctx).transaction() as portfolio:
        portfolio.holdings = [holding]
    kept = UpdateHoldingParams(action="update", id=holding.id, quantity=600_000)
    assert apply_holding_update(ctx, kept) == {"ok": True, "id": holding.id}
    assert portfolio_store(ctx).load().holdings[0].fund.fund_code == "0331418A"
    # The row may now be a different fund, so the link has to be confirmed again instead of being reused.
    renamed = UpdateHoldingParams(action="update", id=holding.id, name="ひふみプラス")
    apply_holding_update(ctx, renamed)
    assert portfolio_store(ctx).load().holdings[0].fund is None
