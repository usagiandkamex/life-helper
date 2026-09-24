import { useCallback, useEffect, useState } from 'react'
import { api, formatDate, json, yen } from '../api'
import { LazyChart } from '../components/LazyChart'
import { Disclaimer } from '../components/Markdown'
import type { ChartData, PortfolioView } from '../types'

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
const SOURCE_LABELS: Record<string, string> = { broker_csv: '証券会社 CSV', stooq: 'Stooq', nav_site: '基準価額サイト', manual: '手入力' }

export function PortfolioPage() {
  const [view, setView] = useState<PortfolioView | null>(null)
  const [message, setMessage] = useState('')
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  const [broker, setBroker] = useState('sbi')
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
    } catch (e) {
      setError((e as Error).message)
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

  const refresh = () =>
    run(
      () => api<PortfolioView>('/api/portfolio/refresh-prices', { method: 'POST' }),
      (v) => `${v.refresh?.updated.length ?? 0} 件の株価を更新しました。${v.refresh?.errors.length ? `取得できなかった銘柄: ${v.refresh.errors.map((e) => e.code).join(', ')}。` : ''}${v.refresh?.note ?? ''}`,
    )

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

  const saveNisa = (form: HTMLFormElement) => {
    const data = new FormData(form)
    run(
      () =>
        api<PortfolioView>('/api/portfolio/nisa-usage', {
          method: 'PUT',
          body: json({ year: view?.nisa.year, tsumitate: Number(data.get('tsumitate')), growth: Number(data.get('growth')) }),
        }),
      () => 'NISA の買付額を保存しました',
    )
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
          <button className="button" onClick={refresh} disabled={busy}>
            株価を更新（Stooq・前日終値）
          </button>
        </div>
        <p className="hint">CSV の取り込みは保有銘柄を置き換えます。CSV の評価額が最も正確です（取り込み時点）。</p>
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
                <th>価格（日付・出どころ）</th>
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
                  <td>{h.price ? `${h.price.value.toLocaleString('ja-JP')}（${h.price.date}・${SOURCE_LABELS[h.price.source] ?? h.price.source}）` : '—'}</td>
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
          <input name="code" placeholder="証券コード（任意）" />
          <input name="name" placeholder="銘柄名" required />
          <input name="quantity" type="number" step="any" min="0" placeholder="数量（株・口）" required />
          <input name="cost_total" type="number" min="0" placeholder="取得額（円）" required />
          <input name="price" type="number" step="any" min="0" placeholder="現在値（投信は1万口あたり）" />
          <button className="button" disabled={busy}>
            手入力で追加
          </button>
        </form>
      </section>

      <section className="panel">
        <h2>NISA（{view.nisa.year} 年）</h2>
        <div className="cards">
          <div className="card">
            <h3>つみたて投資枠の残り</h3>
            <p className="big">{yen(view.nisa.annual_remaining.tsumitate)}</p>
            <small>年間 {yen(view.nisa.annual.tsumitate.limit)}</small>
          </div>
          <div className="card">
            <h3>成長投資枠の残り</h3>
            <p className="big">{yen(view.nisa.annual_remaining.growth)}</p>
            <small>年間 {yen(view.nisa.annual.growth.limit)}</small>
          </div>
          <div className="card">
            <h3>生涯投資枠の残り</h3>
            <p className="big">{yen(view.nisa.lifetime.remaining)}</p>
            <small>簿価 {yen(view.nisa.lifetime.used_book_value)} / {yen(view.nisa.lifetime.limit)}</small>
          </div>
        </div>
        <form
          className="row wrap"
          onSubmit={(e) => {
            e.preventDefault()
            saveNisa(e.currentTarget)
          }}
        >
          <label>
            今年のつみたて投資枠の買付額
            <input name="tsumitate" type="number" min="0" defaultValue={view.nisa.annual.tsumitate.used} />
          </label>
          <label>
            今年の成長投資枠の買付額
            <input name="growth" type="number" min="0" defaultValue={view.nisa.annual.growth.used} />
          </label>
          <button className="button" disabled={busy}>
            保存
          </button>
        </form>
        {[...view.nisa.notes, ...view.nisa.warnings].map((n) => (
          <p key={n} className="hint">
            {n}
          </p>
        ))}
      </section>

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
