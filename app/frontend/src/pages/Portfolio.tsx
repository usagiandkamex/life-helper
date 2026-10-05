import { useCallback, useEffect, useState } from 'react'
import { api, formatDate, json, yen } from '../api'
import { AmountInput } from '../components/AmountInput'
import { LazyChart } from '../components/LazyChart'
import { Disclaimer } from '../components/Markdown'
import { afterPurchase, averageCost, groupNumber, parseNumber, totalsProblem, type Totals } from '../holdings'
import type { ChartData, FundAutoLink, FundCandidates, Holding, PortfolioView } from '../types'

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
  yahoo_finance: 'Yahoo Finance',
  stooq: 'Stooq',
  nav_site: '基準価額サイト',
  manual: '手入力',
  toushin_lib: '投資信託協会',
  mufg_api: '三菱UFJアセットマネジメント（旧 API）',
  rakuten_csv: '楽天投信投資顧問',
  daiwa_csv: '大和アセットマネジメント',
}
// What to type as the code when linking by hand, per provider.
const CODE_HINTS: Record<string, string> = {
  toushin_lib: 'ISIN コード（JP で始まる 12 桁）',
  rakuten_csv: '基準価額 CSV の 6 桁番号',
  daiwa_csv: '4 桁のファンドコード',
}
const MARKET_LABELS: Record<string, string> = { jp: '日本株', us: '米国株' }
// A search that failed says nothing about whether the fund exists, so it must not read as "no such fund".
const SEARCH_FAILED = '候補を取得できませんでした。時間をおいて探し直すか、コードを指定して紐付けるか、公式サイトの基準価額を手入力してください。'

const amount = (value: number) => value.toLocaleString('ja-JP', { maximumFractionDigits: 2 })
const percent = new Intl.NumberFormat('ja-JP', {
  style: 'percent',
  signDisplay: 'exceptZero',
  minimumFractionDigits: 1,
  maximumFractionDigits: 1,
})
// 損益の比率（取得額に対する割合、"+20.0%"）。評価額がない・取得額が 0 のときは出さない。
const gainRate = (gain: number | null, cost: number) => (gain === null || !(cost > 0) ? '' : percent.format(gain / cost))
// The editor shows the totals it is about to save in full: amount() and yen() round, which would make an added
// 0.001 口 or 0.1 円 look like no change at all.
const exact = (value: number) => value.toLocaleString('ja-JP', { maximumSignificantDigits: 17 })
const exactYen = (value: number) => `${exact(value)} 円`
const label = (labels: Record<string, string>, key: string | null | undefined) => (key ? labels[key] ?? key : '')
// 入力欄の値は 3 桁区切りつきの文字列なので、区切りを外して数値にする。fallback は空欄のときだけ使い、
// "." のような数字でない値は NaN のまま返して、呼び出し側の検証で止める（0 などに読み替えない）。
const numberField = (data: FormData, name: string, fallback = Number.NaN) => {
  const text = String(data.get(name) ?? '')
  return text.replace(/,/g, '').trim() === '' ? fallback : parseNumber(text)
}
const marketToday = () => {
  const parts = new Intl.DateTimeFormat('en-US', {
    day: '2-digit',
    month: '2-digit',
    timeZone: 'Asia/Tokyo',
    year: 'numeric',
  }).formatToParts(new Date())
  const value = (type: string) => parts.find((part) => part.type === type)?.value ?? ''
  return `${value('year')}-${value('month')}-${value('day')}`
}

function PriceCell({ holding }: { holding: Holding }) {
  const price = holding.price
  if (!price) return <>—</>
  const converted = price.local_currency === 'USD' && price.local_value !== null && price.local_value !== undefined
  const fund = holding.kind === 'fund'
  // A broker CSV is dated with the day it was imported, not with the 基準日 of the NAV inside it.
  const date = price.source === 'broker_csv' ? `CSV 取込日 ${price.date}` : `${price.date} 時点`
  return (
    <>
      <div>
        {`${amount(price.value)}\u00a0円`}
        {fund && ` / ${amount(holding.price_unit)}\u00a0口`}
      </div>
      <small>{date}</small>
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
          <small className="warn-text">価格が古いままです</small>
        </div>
      )}
    </>
  )
}

// 取得元の書き方は投資信託も個別銘柄も同じにする: 1 行目が表示中の価格の出どころ、2 行目がその取得元での銘柄の識別子。
// 投資信託は次の更新に使う取得元が表示中の価格の出どころと違うことがある（CSV を取り込んだ直後など）ので、そのときだけ続けて書く。
function SourceCell({ holding }: { holding: Holding }) {
  const price = holding.price
  const fund = holding.kind === 'fund'
  const source = price ? label(SOURCE_LABELS, price.source) : '未取得'
  // auto_nav は「公式ファンドに紐付いていて自動更新する投資信託」だけ true。
  const updater = holding.auto_nav && price?.source !== holding.fund?.provider ? label(SOURCE_LABELS, holding.fund?.provider) : ''
  const code = fund
    ? (holding.fund?.fund_code ?? '')
    : [label(MARKET_LABELS, price?.market), price?.symbol || holding.code].filter(Boolean).join(' ')
  return (
    <>
      <div>
        {source}
        {price?.source_url && (
          <>
            {' '}
            <a href={price.source_url} target="_blank" rel="noreferrer">
              出典
            </a>
          </>
        )}
      </div>
      {/* コードは表示中の価格の取得元での識別子。更新元が違うときは、そちらの識別子として書く。 */}
      {code && !updater && (
        <div>
          <small>{code}</small>
        </div>
      )}
      {updater && (
        <div>
          <small>
            次の更新は {updater}
            {code && `（${code}）`}
          </small>
        </div>
      )}
      {fund && !holding.auto_nav && (
        <div>
          <small className="warn-text">{holding.fund ? '自動では更新しません' : '取得元が未設定です'}</small>
        </div>
      )}
    </>
  )
}

function AutoLinkReport({
  report,
  busy,
  onPick,
}: {
  report: FundAutoLink
  busy: boolean
  onPick: (id: string) => void
}) {
  const pending = [...report.ambiguous, ...report.unmatched]
  if (report.linked.length === 0 && pending.length === 0 && report.errors.length === 0) return null
  const pick = (id: string) => (
    <>
      {' '}
      <button className="link small" onClick={() => onPick(id)} disabled={busy}>
        候補を見る
      </button>
    </>
  )
  return (
    <>
      {report.linked.length > 0 && (
        <div className="banner ok">
          ファンド名から公式ファンドに自動で紐付けました（違う場合は「取得元を設定」で選び直してください）:
          <ul>
            {report.linked.map((l) => (
              <li key={l.id}>
                {l.name} → {l.official_name}（{l.code}）
              </li>
            ))}
          </ul>
        </div>
      )}
      {pending.length > 0 && (
        <div className="banner warn">
          自動では紐付けられなかった投資信託（候補から選ぶか、手入力してください）:
          <ul>
            {report.ambiguous.map((a) => (
              <li key={a.id}>
                {a.name}: {a.reason}
                {pick(a.id)}
              </li>
            ))}
            {report.unmatched.map((u) => (
              <li key={u.id}>
                {u.name}: 同じ名前の公式ファンドが見つかりませんでした
                {pick(u.id)}
              </li>
            ))}
          </ul>
        </div>
      )}
      {report.errors.length > 0 && (
        <div className="banner error">
          公式ファンドを検索できなかった投資信託（時間をおいて「評価額を計算」をやり直してください）:
          <ul>
            {report.errors.map((e) => (
              <li key={e.id}>
                {e.name}: {e.error}
              </li>
            ))}
          </ul>
        </div>
      )}
    </>
  )
}

function averageText(holding: Holding, totals: Totals) {
  const average = averageCost(totals.cost, totals.quantity, holding.price_unit)
  if (average === null) return '—'
  return holding.kind === 'fund' ? `${amount(average)} 円 / ${amount(holding.price_unit)} 口` : `${amount(average)} 円`
}

function HoldingEditor({
  holding,
  busy,
  error,
  onSave,
  onClose,
}: {
  holding: Holding
  busy: boolean
  error: string
  onSave: (totals: Totals) => void
  onClose: () => void
}) {
  // The exact cost, not the rounded yen in the table, so an edit never drops the fraction a CSV import left.
  const current = { quantity: holding.quantity, cost: holding.cost_total_exact }
  const [mode, setMode] = useState<'buy' | 'total'>('buy')
  const [buyQuantity, setBuyQuantity] = useState('')
  const [buyCost, setBuyCost] = useState('')
  const [totalQuantity, setTotalQuantity] = useState(groupNumber(holding.quantity))
  const [totalCost, setTotalCost] = useState(groupNumber(holding.cost_total))
  const [costEdited, setCostEdited] = useState(false)

  let result: Totals | string | null
  if (mode === 'buy') {
    result = buyQuantity === '' || buyCost === '' ? null : afterPurchase(current, parseNumber(buyQuantity), parseNumber(buyCost))
  } else {
    // The field shows the cost rounded to yen; until it is edited, the exact cost is kept.
    const cost = costEdited ? parseNumber(totalCost) : current.cost
    const next = { quantity: parseNumber(totalQuantity), cost }
    result = totalsProblem(next) ?? next
  }
  const next = typeof result === 'object' ? result : null
  const changed = next !== null && (next.quantity !== current.quantity || next.cost !== current.cost)
  const modeButton = (value: typeof mode, text: string) => (
    <button
      type="button"
      className={`button small${mode === value ? ' primary' : ''}`}
      aria-pressed={mode === value}
      onClick={() => setMode(value)}
    >
      {text}
    </button>
  )

  return (
    <section className="panel" id="holding-editor">
      <h2>数量・取得額の編集: {holding.name}</h2>
      <p className="hint">
        {holding.account_label}・現在の数量 {exact(current.quantity)}・取得額 {exactYen(current.cost)}・平均取得単価{' '}
        {averageText(holding, current)}
      </p>
      <div className="row wrap">
        {modeButton('buy', '買い増し分を加算')}
        {modeButton('total', '合計値を直接編集')}
      </div>
      <form
        className="grid-form"
        onSubmit={(e) => {
          e.preventDefault()
          if (next && changed) onSave(next)
        }}
      >
        {mode === 'buy' ? (
          <>
            <label>
              購入数量（株・口）
              <AmountInput value={buyQuantity} onValueChange={setBuyQuantity} required />
            </label>
            <label>
              購入金額（円）
              <AmountInput value={buyCost} onValueChange={setBuyCost} required />
            </label>
          </>
        ) : (
          <>
            <label>
              数量の合計（株・口）
              <AmountInput value={totalQuantity} onValueChange={setTotalQuantity} required />
            </label>
            <label>
              取得額の合計（円）
              <AmountInput
                value={totalCost}
                onValueChange={(v) => {
                  setTotalCost(v)
                  setCostEdited(true)
                }}
                required
              />
            </label>
          </>
        )}
        <button className="button primary" disabled={busy || !changed}>
          保存
        </button>
        <button type="button" className="button" onClick={onClose}>
          閉じる
        </button>
      </form>
      {typeof result === 'string' && <p className="error-text">{result}</p>}
      {next && changed && (
        <p>
          保存後: 数量 {exact(current.quantity)} → {exact(next.quantity)}・取得額 {exactYen(current.cost)} → {exactYen(next.cost)}・平均取得単価{' '}
          {averageText(holding, next)}
        </p>
      )}
      {error && <p className="error-text">{error}</p>}
      <p className="hint">
        {mode === 'buy'
          ? '今回約定した数量と購入金額（手数料を含む受渡金額）を入力すると、今の合計に足して保存します。積立は約定ごとでも、月ごとにまとめてでも入力できます。'
          : '証券会社の画面に表示されている保有数量と取得金額（簿価）の合計を入力します。'}
        保存後の合計が証券会社の画面と合っているか確認してください。証券会社 CSV を取り込むと保有銘柄は CSV の内容に置き換わり、ここで編集した値も CSV の値になります。
      </p>
      {holding.price?.source === 'broker_csv' && (
        <p className="hint">
          CSV から取り込んだ評価額は、株価・基準価額を更新するまで、CSV の評価額を数量に合わせて按分した目安になります。「評価額を計算」で最新の価格に更新してください。
        </p>
      )}
    </section>
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
  const [searching, setSearching] = useState(false)
  // Only the id is kept: the editor reads the holding from the latest view, and closes once a CSV import replaces it.
  const [editId, setEditId] = useState<string | null>(null)
  // 表の下の編集欄で選んでいる銘柄。表からボタンを外したので、どの銘柄を編集するかはここで選ぶ。
  const [selectedId, setSelectedId] = useState('')
  // 手入力で追加したあとに入力欄を空に戻すための番号（3 桁区切りの入力欄は form.reset() では消えない）。
  const [addKey, setAddKey] = useState(0)
  const [chart, setChart] = useState<ChartData | null>(null)
  const [simResult, setSimResult] = useState<{ principal: number; expected_value: number; percentiles: Record<string, number>; after_tax: Record<string, number> } | null>(null)

  const load = useCallback(async () => setView(await api<PortfolioView>('/api/portfolio')), [])
  useEffect(() => {
    load().catch((e) => setError(e.message))
  }, [load])
  // The picker opens below the holdings table, often out of sight of the button that opened it.
  useEffect(() => {
    if (fundTarget) document.getElementById('fund-picker')?.scrollIntoView({ behavior: 'smooth', block: 'start' })
  }, [fundTarget])
  useEffect(() => {
    if (editId) document.getElementById('holding-editor')?.scrollIntoView({ behavior: 'smooth', block: 'start' })
  }, [editId])

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

  const importCsv = async (file: File) => {
    const form = new FormData()
    form.append('broker', broker)
    form.append('file', file)
    const ok = await run(
      () => api<PortfolioView>('/api/portfolio/import', { method: 'POST', body: form }),
      (v) => `${v.imported} 件の保有銘柄を取り込みました`,
    )
    // 取り込むと保有銘柄が入れ替わるので、取り込み前の銘柄を選んだままの編集欄は閉じる。
    if (ok) selectHolding('')
  }

  const calculate = async () => {
    setCalculating(true)
    try {
      await run(
        () => api<PortfolioView>('/api/portfolio/refresh-prices', { method: 'POST' }),
        (v) =>
          `評価額を計算しました: 合計 ${yen(v.total_value)}` +
          `（株価 ${v.refresh?.updated.length ?? 0} 件・基準価額 ${v.refresh_funds?.updated.length ?? 0} 件を反映）。` +
          `${v.refresh_funds?.auto_link?.linked.length ? `${v.refresh_funds.auto_link.linked.length} 件の投資信託をファンド名から公式ファンドに紐付けました。` : ''}` +
          `${v.refresh?.errors.length ? `${v.refresh.errors.length} 件は株価を取得できませんでした（理由は下に表示しています）。` : ''}` +
          `${v.refresh_funds?.errors.length ? `${v.refresh_funds.errors.length} 件は基準価額を取得できませんでした（直前の基準価額を残しています）。` : ''}` +
          `${v.missing_prices.length ? `価格が未登録の ${v.missing_prices.length} 件は合計に含めていません。` : ''}` +
          [v.refresh?.note, v.refresh_funds?.note].filter(Boolean).join(' '),
      )
    } finally {
      setCalculating(false)
    }
  }

  const searchFunds = (name: string) => {
    setCandidates(null)
    setSearching(true)
    run(async () => {
      setCandidates(await api<FundCandidates>(`/api/portfolio/fund-candidates?name=${encodeURIComponent(name)}`))
    }).finally(() => setSearching(false))
  }

  const openFundPicker = (h: Holding) => {
    setSelectedId(h.id)
    setEditId(null)
    setFundTarget(h)
    searchFunds(h.name)
  }

  const openEditor = (h: Holding) => {
    setSelectedId(h.id)
    setFundTarget(null)
    setCandidates(null)
    setError('')
    setEditId(h.id)
  }

  // 編集する銘柄を選び直したら、前の銘柄で開いていた編集欄・取得元の欄は閉じる。
  const selectHolding = (id: string) => {
    setSelectedId(id)
    setEditId(null)
    setFundTarget(null)
    setCandidates(null)
  }

  // Totals, not the purchase, are sent, so a double click or a retry cannot add the purchase twice. The totals it
  // was computed from go along, and the server refuses the update if another one changed them in the meantime.
  const saveHolding = async (target: Holding, totals: Totals) => {
    const ok = await run(
      () =>
        api<PortfolioView>('/api/portfolio/holdings', {
          method: 'POST',
          body: json({
            action: 'update',
            id: target.id,
            quantity: totals.quantity,
            cost_total: totals.cost,
            expected_quantity: target.quantity,
            expected_cost_total: target.cost_total_exact,
          }),
        }),
      () => `${target.name} を数量 ${exact(totals.quantity)}・取得額 ${exactYen(totals.cost)} に更新しました`,
    )
    if (ok) setEditId(null)
    // After a conflict the editor starts over from the latest totals.
    else load().catch(() => undefined)
  }

  // Linking by hand always needs this click: a similar name alone never decides which fund a holding is.
  // Only an identical name that exactly one official fund has is linked automatically, by the refresh.
  const linkFund = async (target: Holding, provider: string, fundCode: string, priceUnit = 10000) => {
    const ok = await run(
      () =>
        api<PortfolioView>('/api/portfolio/fund-link', {
          method: 'POST',
          body: json({ id: target.id, provider, fund_code: fundCode, price_unit: priceUnit }),
        }),
      (v) =>
        provider === 'manual'
          ? `${target.name} の基準価額を手入力に切り替えました`
          : `${target.name} を ${v.link?.official_name || fundCode} に紐付け、基準価額を取得しました`,
    )
    if (ok) {
      setFundTarget(null)
      setCandidates(null)
    }
  }

  // The manual fallback also has to set the NAV: switching the source alone would leave the old price in place.
  const setManualNav = async (target: Holding, priceUnit: number, nav: number, priceDate: string) => {
    if (!(nav > 0) || !(priceUnit >= 1) || !Number.isInteger(priceUnit)) {
      setError('基準価額には 0 より大きい数値を、価格単位には 1 以上の整数を入力してください')
      return
    }
    const ok = await run(() =>
      api<PortfolioView>('/api/portfolio/fund-link', {
        method: 'POST',
        body: json({ id: target.id, provider: 'manual', fund_code: '', price_unit: priceUnit, nav, price_date: priceDate }),
      }),
      () => `${target.name} の基準価額を手入力しました（${amount(nav)} 円 / ${amount(priceUnit)} 口）`,
    )
    if (ok) {
      setFundTarget(null)
      setCandidates(null)
    }
  }

  const addHolding = (form: HTMLFormElement) => {
    const data = new FormData(form)
    const totals = { quantity: numberField(data, 'quantity'), cost: numberField(data, 'cost_total') }
    const problem = totalsProblem(totals)
    if (problem) {
      setMessage('')
      setError(problem)
      return
    }
    const price = numberField(data, 'price')
    run(async () => {
      const v = await api<PortfolioView>('/api/portfolio/holdings', {
        method: 'POST',
        body: json({
          action: 'add',
          account: data.get('account'),
          kind: data.get('kind'),
          code: data.get('code') || '',
          name: data.get('name'),
          quantity: totals.quantity,
          cost_total: totals.cost,
          price: price > 0 ? price : null,
        }),
      })
      // 3 桁区切りの入力欄は form.reset() では空にならないので、フォームごと作り直す。
      setAddKey((n) => n + 1)
      return v
    }, () => '追加しました')
  }

  const removeHolding = async (target: Holding) => {
    if (!window.confirm(`${target.account_label}の「${target.name}」を削除しますか？`)) return
    const ok = await run(() =>
      api<PortfolioView>('/api/portfolio/holdings', { method: 'POST', body: json({ action: 'delete', id: target.id }) }),
    )
    if (ok) selectHolding('')
  }

  const simulate = async (form: HTMLFormElement) => {
    const data = new FormData(form)
    const initial = numberField(data, 'initial', 0)
    const monthly = numberField(data, 'monthly', 0)
    if (![initial, monthly].every((value) => Number.isFinite(value) && value >= 0)) {
      throw new Error('元本と毎月の積立には 0 以上の数値を入力してください')
    }
    const res = await api<{ principal: number; expected_value: number; percentiles: Record<string, number>; after_tax: Record<string, number>; yearly: Record<string, number>[] }>(
      '/api/portfolio/simulate',
      {
        method: 'POST',
        body: json({
          initial,
          monthly_contribution: monthly,
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
  const editing = editId ? view.holdings.find((h) => h.id === editId) : undefined
  // 削除や CSV の取り込みで銘柄が入れ替わったときは、選び直してもらう。
  const selected = view.holdings.find((h) => h.id === selectedId)
  // 価格未登録の銘柄は取得額だけが合計に入るので、そのときの合計の比率は出さない（実際より低く見えてしまう）。
  const totalRate = view.missing_prices.length > 0 ? '' : gainRate(view.total_gain, view.total_cost)
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
            {totalRate && `（${totalRate}）`}
          </small>
        </div>
        {Object.entries(view.accounts).map(([key, a]) => (
          <div className="card" key={key}>
            <h3>{a.label}</h3>
            <p className="big">{yen(a.value)}</p>
            <small>
              取得額 {yen(a.cost)} / 損益 {yen(a.value - a.cost)}
            </small>
          </div>
        ))}
      </section>
      <div className="row wrap">
        <button className="button primary" onClick={calculate} disabled={busy || calculating}>
          {calculating ? '計算中…' : '評価額を計算'}
        </button>
        <details className="hint grow">
          <summary>価格の更新について</summary>
          <p>
            株式・ETF・REIT は株価（日本株・米国株／Yahoo Finance の前日終値）、投資信託は基準価額（投資信託協会の投信総合検索ライブラリー・運用会社の公式
            CSV）を、それぞれ別に更新してから計算します。米国株は USD/JPY で円換算します。取得元が未設定の投資信託は、ファンド名が一致する公式ファンドが
            1 つだけなら自動で紐付けます。紐付けられなかった投資信託は「取得元を設定」から選ぶか、公式サイトの基準価額を手入力してください。
          </p>
        </details>
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
      {view.refresh_funds?.auto_link && (
        <AutoLinkReport
          report={view.refresh_funds.auto_link}
          busy={busy}
          onPick={(id) => {
            const target = view.holdings.find((h) => h.id === id)
            if (target) openFundPicker(target)
          }}
        />
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
        <h2>保有銘柄</h2>
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>口座</th>
                <th className="name">銘柄</th>
                <th className="num">数量</th>
                <th className="num">取得額</th>
                <th className="num">評価額</th>
                <th className="num">損益</th>
                <th>価格</th>
                <th>取得元</th>
              </tr>
            </thead>
            <tbody>
              {view.holdings.map((h) => (
                <tr key={h.id} className={h.id === selected?.id ? 'selected' : undefined}>
                  <td>{h.account_label}</td>
                  <td className="name">
                    {h.name}
                    {h.code && <small> ({h.code})</small>}
                  </td>
                  <td className="num">{h.quantity.toLocaleString('ja-JP')}</td>
                  <td className="num">{yen(h.cost_total)}</td>
                  <td className="num">{yen(h.value)}</td>
                  <td className={`num ${h.gain !== null && h.gain < 0 ? 'neg' : 'pos'}`}>
                    {yen(h.gain)}
                    <span className="rate">{gainRate(h.gain, h.cost_total)}</span>
                  </td>
                  <td>
                    <PriceCell holding={h} />
                  </td>
                  <td>
                    <SourceCell holding={h} />
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        {/* 行ごとにボタンを置くと表が読みにくいので、編集する銘柄はここで選ぶ。 */}
        <div className="row wrap holding-actions">
          <label className="grow">
            編集する銘柄
            <select value={selected?.id ?? ''} onChange={(e) => selectHolding(e.target.value)}>
              <option value="">選択してください</option>
              {view.holdings.map((h) => (
                <option key={h.id} value={h.id}>
                  {h.account_label}・{h.name}
                </option>
              ))}
            </select>
          </label>
          <button className="button small" onClick={() => selected && openEditor(selected)} disabled={busy || !selected}>
            数量・取得額を編集
          </button>
          <button
            className="button small"
            onClick={() => selected && openFundPicker(selected)}
            disabled={busy || selected?.kind !== 'fund'}
          >
            取得元を設定
          </button>
          <button className="button small danger" onClick={() => selected && removeHolding(selected)} disabled={busy || !selected}>
            削除
          </button>
        </div>
        <p className="hint">
          選んだ銘柄は表の中で色が付きます。「取得元を設定」は基準価額を取得する投資信託だけで使えます。
        </p>
      </section>

      {editing && (
        <HoldingEditor
          key={`${editing.id}:${editing.quantity}:${editing.cost_total_exact}`}
          holding={editing}
          busy={busy}
          error={error}
          onSave={(totals) => saveHolding(editing, totals)}
          onClose={() => setEditId(null)}
        />
      )}

      {fundTarget && (
        <section className="panel" id="fund-picker" key={fundTarget.id}>
          <h2>基準価額の取得元: {fundTarget.name}</h2>
          <p className="hint">
            {candidates?.note ?? (searching ? '公式ファンドの候補を探しています…' : SEARCH_FAILED)}
            {view.fund_providers.length > 1 &&
              `（取得元: ${view.fund_providers
                .filter((p) => p.provider !== 'manual')
                .map((p) => p.label)
                .join('、')}）`}
          </p>
          <form
            className="row wrap"
            onSubmit={(e) => {
              e.preventDefault()
              searchFunds(new FormData(e.currentTarget).get('name') as string)
            }}
          >
            <label>
              ファンド名
              <input name="name" defaultValue={fundTarget.name} required />
            </label>
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
            <p className="hint">
              {candidates.errors.length > 0
                ? SEARCH_FAILED
                : '候補が見つかりませんでした。ファンド名を短くして探し直すか、コードを指定して紐付けるか、公式サイトの基準価額を手入力してください。'}
            </p>
          )}
          {candidates && candidates.candidates.length > 0 && (
            <div className="table-wrap">
              <table>
                <thead>
                  <tr>
                    <th>公式のファンド名</th>
                    <th>運用会社</th>
                    <th>コード</th>
                    <th>基準価額</th>
                    <th>名前の一致度</th>
                    <th />
                  </tr>
                </thead>
                <tbody>
                  {candidates.candidates.map((c) => (
                    <tr key={`${c.provider}:${c.fund_code}`}>
                      <td>
                        {c.name}
                        {c.nickname && <small>（愛称: {c.nickname}）</small>}
                      </td>
                      <td>{c.manager}</td>
                      <td>
                        {c.fund_code}
                        {c.association_code && c.association_code !== c.fund_code && <small> / {c.association_code}</small>}
                      </td>
                      <td>{c.nav ? `${amount(c.nav)} 円（${c.date ?? '—'}）` : '—'}</td>
                      <td>{c.exact ? '一致' : `${Math.round(c.score * 100)}%`}</td>
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
              const data = new FormData(e.currentTarget)
              linkFund(fundTarget, String(data.get('provider')), String(data.get('fund_code')).trim())
            }}
          >
            <label>
              取得元
              <select name="provider">
                {view.fund_providers
                  .filter((p) => p.provider !== 'manual')
                  .map((p) => (
                    <option key={p.provider} value={p.provider}>
                      {label(SOURCE_LABELS, p.provider)}
                    </option>
                  ))}
              </select>
            </label>
            <label>
              コード
              <input name="fund_code" placeholder="ISIN コードなど" required />
            </label>
            <button className="button small" disabled={busy}>
              コードを指定して紐付ける
            </button>
          </form>
          <p className="hint">
            候補に見つからないときは、取得元ごとのコードを指定してください（
            {view.fund_providers
              .filter((p) => CODE_HINTS[p.provider])
              .map((p) => `${label(SOURCE_LABELS, p.provider)}: ${CODE_HINTS[p.provider]}`)
              .join('、')}
            ）。ISIN コードは証券会社や運用会社のファンドページに載っています。紐付けると公式名称を表示するので、保有しているファンドか確認してください。
          </p>
          <form
            className="row wrap"
            onSubmit={(e) => {
              e.preventDefault()
              const data = new FormData(e.currentTarget)
              setManualNav(fundTarget, numberField(data, 'price_unit'), numberField(data, 'nav'), String(data.get('price_date')))
            }}
          >
            <label>
              基準価額（円）
              <AmountInput name="nav" defaultValue={fundTarget.price?.value} required />
            </label>
            <label>
              価格単位（口）
              <AmountInput name="price_unit" defaultValue={fundTarget.price_unit} required />
            </label>
            <label>
              基準日
              <input name="price_date" type="date" defaultValue={fundTarget.price?.date ?? marketToday()} required />
            </label>
            <button className="button small" disabled={busy}>
              自動取得を使わず手入力にする
            </button>
          </form>
          <p className="hint">
            公式サイトに載っている基準価額と、その口数単位（通常 1 万口）を入力してください。評価額は「保有口数 ÷ 価格単位 ×
            基準価額」で計算します。エラーが出たときは基準価額が反映されていない場合があるので、表示されている基準価額を確認し、必要なら入力し直してください。
          </p>
        </section>
      )}

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
        <p className="hint warn-text">
          取り込むと保有銘柄は CSV の内容に置き換わります（手で編集した数量・取得額も CSV の値になります）。
        </p>
        <details className="hint">
          <summary>CSV 取り込みの使い方</summary>
          <p>
            CSV の評価額が最も正確です（取り込み時点）。株価・基準価額の更新は「評価額を計算」から行います。
            積立・買い増しの後は、CSV を取り込み直すか、「数量・取得額を編集」で購入分を加算してください。
          </p>
        </details>
        {/* CSV に載らない銘柄を足すときだけ使うので、畳んでおく。 */}
        <details className="collapse">
          <summary>手入力で追加</summary>
          <form
            key={addKey}
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
            <AmountInput name="quantity" placeholder="数量（株・口）" required />
            <AmountInput name="cost_total" placeholder="取得額（円）" required />
            <AmountInput name="price" placeholder="現在値（投信は1万口あたり）" />
            <button className="button" disabled={busy}>
              追加
            </button>
          </form>
        </details>
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
          <label>元本（円）<AmountInput name="initial" key={Math.round(view.total_value)} defaultValue={Math.round(view.total_value)} /></label>
          <label>毎月の積立（円）<AmountInput name="monthly" defaultValue={30000} /></label>
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
