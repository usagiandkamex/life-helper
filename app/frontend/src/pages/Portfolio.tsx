import { useCallback, useEffect, useState } from 'react'
import { api, formatDate, json, yen } from '../api'
import { LazyChart } from '../components/LazyChart'
import { Disclaimer } from '../components/Markdown'
import type { ChartData, FundCandidates, Holding, PortfolioView } from '../types'

const ACCOUNTS = [
  ['nisa_tsumitate', 'NISA つみたて投資枠'],
  ['nisa_growth', 'NISA 成長投資枠'],
  ['tokutei', '特定口座'],
  ['ippan', '一般口座'],
  ['ideco', 'iDeCo'],
] as const
const KINDS = [
  ['fund', '投資信託'],
  ['stock', '株式'],
  ['etf', 'ETF'],
  ['reit', 'REIT'],
] as const
const SOURCE_LABELS: Record<string, string> = {
  broker_csv: '証券会社 CSV',
  stooq: 'Stooq',
  nav_site: '基準価額サイト',
  manual: '手入力',
  mufg_api: '三菱UFJアセットマネジメント',
}
const MARKET_LABELS: Record<string, string> = { jp: '日本株', us: '米国株' }

const amount = (value: number) => value.toLocaleString('ja-JP', { maximumFractionDigits: 2 })
const label = (labels: Record<string, string>, key: string | null | undefined) => (key ? labels[key] ?? key : '')

function PriceCell({ holding }: { holding: Holding }) {
  const price = holding.price
  if (!price) return <>—</>
  const market = [label(MARKET_LABELS, price.market), price.symbol].filter(Boolean).join(' ')
  const converted = price.local_currency === 'USD' && price.local_value !== null && price.local_value !== undefined
  const fund = holding.kind === 'fund'
  return (
    <>
      <div>
        {amount(price.value)} 円{fund && ` / ${amount(holding.price_unit)} 口`}
      </div>
      <small>
        {market && `${market}・`}
        {fund ? '基準日 ' : ''}
        {price.date}・{label(SOURCE_LABELS, price.source)}
        {price.source_url && (
          <>
            ・
            <a href={price.source_url} target="_blank" rel="noreferrer">
              出典
            </a>
          </>
        )}
        {price.fetched_at && `・取得 ${formatDate(price.fetched_at)}`}
      </small>
      {converted && (
        <div>
          <small>
            {amount(price.local_value as number)} USD
            {price.fx_rate
              ? ` × ${amount(price.fx_rate)} 円/USD（${price.fx_date ?? '—'}・${label(SOURCE_LABELS, price.fx_source)}）`
              : '（円換算前）'}
          </small>
        </div>
      )}
      {holding.stale && (
        <div>
          <small className="warn-text">{fund ? '基準価額が古いままです' : '価格が古いままです'}</small>
        </div>
      )}
    </>
  )
}

export function PortfolioPage() {
  const [view, setView] = useState<PortfolioView | null>(null)
  const [message, setMessage] = useState('')
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  const [calculating, setCalculating] = useState(false)
  const [broker, setBroker] = useState('sbi')
  const [fundTarget, setFundTarget] = useState<Holding | null>(null)
  const [candidates, setCandidates] = useState<FundCandidates | null>(null)
  const [chart, setChart] = useState<ChartData | null>(null)
  const [simResult, setSimResult] = useState<{ principal: number; expected_value: number; percentiles: Record<string, number>; after_tax: Record<string, number> } | null>(null)

  const load = useCallback(async () => setView(await api<PortfolioView>('/api/portfolio')), [])
  useEffect(() => {
    load().catch((e) => setError(e.message))
  }, [load])

  const run = async (fn: () => Promise<PortfolioView | void>, done?: (v: PortfolioView) => string) => {
    setBusy(true)
    setError('')
    setMessage('')
    try {
      const v = await fn()
      if (v) {
        setView(v)
        if (done) setMessage(done(v))
      }
      return true
    } catch (e) {
      setError((e as Error).message)
      return false
    } finally {
      setBusy(false)
    }
  }

  const importCsv = (file: File) => {
    const form = new FormData()
    form.append('broker', broker)
    form.append('file', file)
    run(() => api<PortfolioView>('/api/portfolio/import', { method: 'POST', body: form }), (v) => `${v.imported} 件の保有銘柄を取り込みました`)
  }

  const calculate = async () => {
    setCalculating(true)
    try {
      await run(
        () => api<PortfolioView>('/api/portfolio/refresh-prices', { method: 'POST' }),
        (v) =>
          `評価額を計算しました: 合計 ${yen(v.total_value)}` +
          `（株価 ${v.refresh?.updated.length ?? 0} 件・基準価額 ${v.refresh_funds?.updated.length ?? 0} 件を反映）。` +
          `${v.refresh?.errors.length ? `${v.refresh.errors.length} 件は株価を取得できませんでした（理由は下に表示しています）。` : ''}` +
          `${v.refresh_funds?.errors.length ? `${v.refresh_funds.errors.length} 件は基準価額を取得できませんでした（直前の基準価額を残しています）。` : ''}` +
          `${v.missing_prices.length ? `価格が未登録の ${v.missing_prices.length} 件は合計に含めていません。` : ''}` +
          `${v.refresh?.note ?? ''}`,
      )
    } finally {
      setCalculating(false)
    }
  }

  const searchFunds = (name: string) => {
    setCandidates(null)
    run(async () => {
      setCandidates(await api<FundCandidates>(`/api/portfolio/fund-candidates?name=${encodeURIComponent(name)}`))
    })
  }

  const openFundPicker = (h: Holding) => {
    setFundTarget(h)
    searchFunds(h.name)
  }

  // Linking always needs this click: a similar name alone never decides which fund a holding is.
  const linkFund = async (target: Holding, provider: string, fundCode: string, priceUnit = 10000) => {
    const ok = await run(
      () =>
        api<PortfolioView>('/api/portfolio/fund-link', {
          method: 'POST',
          body: json({ id: target.id, provider, fund_code: fundCode, price_unit: priceUnit }),
        }),
      () =>
        provider === 'manual'
          ? `${target.name} の基準価額を手入力に切り替えました`
          : `${target.name} を ${fundCode} に紐付け、基準価額を取得しました`,
    )
    if (ok) {
      setFundTarget(null)
      setCandidates(null)
    }
  }

  const addHolding = (form: HTMLFormElement) => {
    const data = new FormData(form)
    const price = Number(data.get('price'))
    run(async () => {
      const v = await api<PortfolioView>('/api/portfolio/holdings', {
        method: 'POST',
        body: json({
          action: 'add',
          account: data.get('account'),
          kind: data.get('kind'),
          code: data.get('code') || '',
          name: data.get('name'),
          quantity: Number(data.get('quantity')),
          cost_total: Number(data.get('cost_total')),
          price: price > 0 ? price : null,
        }),
      })
      form.reset()
      return v
    }, () => '追加しました')
  }

  const removeHolding = (id: string) => {
    if (window.confirm('この銘柄を削除しますか？'))
      run(() => api<PortfolioView>('/api/portfolio/holdings', { method: 'POST', body: json({ action: 'delete', id }) }))
  }

  const simulate = async (form: HTMLFormElement) => {
    const data = new FormData(form)
    const res = await api<{ principal: number; expected_value: number; percentiles: Record<string, number>; after_tax: Record<string, number>; yearly: Record<string, number>[] }>(
      '/api/portfolio/simulate',
      {
        method: 'POST',
        body: json({
          initial: Number(data.get('initial')),
          monthly_contribution: Number(data.get('monthly')),
          years: Number(data.get('years')),
          expected_return: Number(data.get('rate')) / 100,
          volatility: Number(data.get('vol')) / 100,
          expense_ratio: Number(data.get('fee')) / 100,
        }),
      },
    )
    setSimResult(res)
    setChart({ type: 'investment', x: 'year', series: ['principal', 'value'], data: res.yearly })
  }

  if (!view) return <div className="panel">{error || '読み込み中…'}</div>
  return (
    <div className="stack">
      <Disclaimer />
      {message && <div className="banner ok">{message}</div>}
      {error && <div className="banner error">{error}</div>}
      <section className="cards">
        <div className="card">
          <h3>評価額合計</h3>
          <p className="big">{yen(view.total_value)}</p>
          <small>
            取得額 {yen(view.total_cost)} / 損益 {yen(view.total_gain)}
          </small>
        </div>
        {Object.entries(view.accounts).map(([key, a]) => (
          <div className="card" key={key}>
            <h3>{a.label}</h3>
            <p className="big">{yen(a.value)}</p>
            <small>取得額 {yen(a.cost)}</small>
          </div>
        ))}
      </section>
      <div className="row wrap">
        <button className="button primary" onClick={calculate} disabled={busy || calculating}>
          {calculating ? '計算中…' : '評価額を計算'}
        </button>
        <span className="hint">
          株式・ETF・REIT は株価（日本株・米国株／Stooq 前日終値）、投資信託は基準価額（運用会社の公式 API）を、
          それぞれ別に更新してから計算します。米国株は USD/JPY で円換算します。
          自動取得に対応していない投資信託は、公式サイトの基準価額を手入力してください。
        </span>
      </div>
      {view.refresh && view.refresh.errors.length > 0 && (
        <div className="banner error">
          株価を取得できなかった銘柄:
          <ul>
            {view.refresh.errors.map((e) => (
              <li key={e.code}>
                {e.code}: {e.error}
              </li>
            ))}
          </ul>
        </div>
      )}
      {view.refresh_funds && view.refresh_funds.errors.length > 0 && (
        <div className="banner error">
          基準価額を取得できなかったファンド（直前の基準価額を残しています）:
          <ul>
            {view.refresh_funds.errors.map((e) => (
              <li key={e.code}>
                {e.code}: {e.error}
              </li>
            ))}
          </ul>
        </div>
      )}
      {(view.stale_prices.length > 0 || view.manual_funds.length > 0) && (
        <div className="banner warn">
          {view.stale_prices.length > 0 && <div>価格が古いままの銘柄: {view.stale_prices.join('、')}</div>}
          {view.manual_funds.length > 0 && (
            <div>基準価額を自動取得していない投資信託: {view.manual_funds.map((f) => f.name).join('、')}</div>
          )}
        </div>
      )}
      <p className="hint">
        {view.note} 最も古い価格の日付: {view.oldest_price_date ?? '—'}
        {view.missing_prices.length > 0 && ` / 価格未登録: ${view.missing_prices.join('、')}`}
      </p>

      <section className="panel">
        <h2>保有銘柄の更新</h2>
        <div className="row wrap">
          <select value={broker} onChange={(e) => setBroker(e.target.value)}>
            {view.brokers.map((b) => (
              <option key={b.name} value={b.name}>
                {b.label}
              </option>
            ))}
          </select>
          <label className="button">
            保有証券 CSV を取り込む
            <input type="file" accept=".csv" hidden disabled={busy} onChange={(e) => e.target.files?.[0] && importCsv(e.target.files[0])} />
          </label>
        </div>
        <p className="hint">CSV の取り込みは保有銘柄を置き換えます。CSV の評価額が最も正確です（取り込み時点）。株価の更新は「評価額を計算」から行います。</p>
      </section>

      <section className="panel">
        <h2>保有銘柄</h2>
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>口座</th>
                <th>銘柄</th>
                <th>数量</th>
                <th>取得額</th>
                <th>評価額</th>
                <th>損益</th>
                <th>価格（市場・日付・出どころ）</th>
                <th>基準価額の取得元</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {view.holdings.map((h) => (
                <tr key={h.id}>
                  <td>{h.account_label}</td>
                  <td>
                    {h.name}
                    {h.code && <small> ({h.code})</small>}
                  </td>
                  <td>{h.quantity.toLocaleString('ja-JP')}</td>
                  <td>{yen(h.cost_total)}</td>
                  <td>{yen(h.value)}</td>
                  <td className={h.gain !== null && h.gain < 0 ? 'neg' : 'pos'}>{yen(h.gain)}</td>
                  <td>
                    <PriceCell holding={h} />
                  </td>
                  <td>
                    {h.kind !== 'fund' ? (
                      '—'
                    ) : (
                      <>
                        <div>{h.auto_nav ? label(SOURCE_LABELS, h.fund?.provider) : '手入力'}</div>
                        {h.fund?.fund_code && <small>{h.fund.fund_code}</small>}
                        {!h.auto_nav && (
                          <div>
                            <small className="warn-text">自動取得未対応</small>
                          </div>
                        )}
                        <button className="link small" onClick={() => openFundPicker(h)} disabled={busy}>
                          取得元を設定
                        </button>
                      </>
                    )}
                  </td>
                  <td>
                    <button className="link danger" onClick={() => removeHolding(h.id)}>
                      削除
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        <form
          className="grid-form"
          onSubmit={(e) => {
            e.preventDefault()
            addHolding(e.currentTarget)
          }}
        >
          <select name="account">{ACCOUNTS.map(([v, l]) => <option key={v} value={v}>{l}</option>)}</select>
          <select name="kind">{KINDS.map(([v, l]) => <option key={v} value={v}>{l}</option>)}</select>
          <input name="code" placeholder="証券コード・ティッカー（任意）" />
          <input name="name" placeholder="銘柄名" required />
          <input name="quantity" type="number" step="any" min="0" placeholder="数量（株・口）" required />
          <input name="cost_total" type="number" min="0" placeholder="取得額（円）" required />
          <input name="price" type="number" step="any" min="0" placeholder="現在値（投信は1万口あたり）" />
          <button className="button" disabled={busy}>
            手入力で追加
          </button>
        </form>
      </section>

      {fundTarget && (
        <section className="panel">
          <h2>基準価額の取得元: {fundTarget.name}</h2>
          <p className="hint">
            {candidates?.note ?? '公式ファンドの候補を探しています…'}
            {view.fund_providers.length > 1 &&
              `（対応運用会社: ${view.fund_providers
                .filter((p) => p.provider !== 'manual')
                .map((p) => p.manager)
                .join('、')}）`}
          </p>
          <form
            className="row wrap"
            onSubmit={(e) => {
              e.preventDefault()
              searchFunds(new FormData(e.currentTarget).get('name') as string)
            }}
          >
            <input name="name" defaultValue={fundTarget.name} required />
            <button className="button" disabled={busy}>
              候補を探す
            </button>
            <button type="button" className="button" onClick={() => setFundTarget(null)}>
              閉じる
            </button>
          </form>
          {candidates?.errors.map((e) => (
            <p className="error-text" key={e.code}>
              {e.code}: {e.error}
            </p>
          ))}
          {candidates && candidates.candidates.length === 0 && (
            <p className="hint">候補が見つかりませんでした。公式サイトの基準価額を手入力してください。</p>
          )}
          {candidates && candidates.candidates.length > 0 && (
            <div className="table-wrap">
              <table>
                <thead>
                  <tr>
                    <th>公式のファンド名</th>
                    <th>運用会社</th>
                    <th>ファンドコード</th>
                    <th>名前の一致度</th>
                    <th />
                  </tr>
                </thead>
                <tbody>
                  {candidates.candidates.map((c) => (
                    <tr key={`${c.provider}:${c.fund_code}`}>
                      <td>{c.name}</td>
                      <td>{c.manager}</td>
                      <td>
                        {c.fund_code}
                        {c.isin && <small> / {c.isin}</small>}
                      </td>
                      <td>{Math.round(c.score * 100)}%</td>
                      <td>
                        <button
                          className="button small"
                          disabled={busy}
                          onClick={() => linkFund(fundTarget, c.provider, c.fund_code)}
                        >
                          このファンドにする
                        </button>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
          <p className="hint">
            候補は名前が似ているだけのファンドを含みます。公式名称とファンドコードを確認してから選んでください。
          </p>
          <form
            className="row wrap"
            onSubmit={(e) => {
              e.preventDefault()
              linkFund(fundTarget, 'manual', '', Number(new FormData(e.currentTarget).get('price_unit')))
            }}
          >
            <label>
              価格単位（口）
              <input name="price_unit" type="number" min="1" step="1" defaultValue={fundTarget.price_unit} required />
            </label>
            <button className="button small" disabled={busy}>
              自動取得を使わず手入力にする
            </button>
          </form>
        </section>
      )}

      <section className="panel">
        <h2>積立シミュレーション</h2>
        <form
          className="grid-form"
          onSubmit={(e) => {
            e.preventDefault()
            simulate(e.currentTarget).catch((err) => setError(err.message))
          }}
        >
          <label>元本（円）<input name="initial" type="number" min="0" defaultValue={Math.round(view.total_value)} /></label>
          <label>毎月の積立（円）<input name="monthly" type="number" min="0" defaultValue={30000} /></label>
          <label>年数<input name="years" type="number" min="1" max="60" defaultValue={20} /></label>
          <label>想定利回り（%）<input name="rate" type="number" step="0.1" defaultValue={4} /></label>
          <label>リスク（%）<input name="vol" type="number" step="1" defaultValue={15} /></label>
          <label>信託報酬（%）<input name="fee" type="number" step="0.01" defaultValue={0.1} /></label>
          <button className="button primary">計算</button>
        </form>
        {simResult && (
          <p>
            元本 {yen(simResult.principal)} → 期待値 {yen(simResult.expected_value)}（10%: {yen(simResult.percentiles.p10)} / 50%: {yen(simResult.percentiles.p50)} / 90%:{' '}
            {yen(simResult.percentiles.p90)}）。NISA なら課税口座より {yen(simResult.after_tax.tax_saved_by_nisa)} 有利（目安）。
          </p>
        )}
        {chart && <LazyChart chart={chart} />}
      </section>
      <p className="hint">最終更新: {formatDate(view.updated_at)}</p>
    </div>
  )
}
