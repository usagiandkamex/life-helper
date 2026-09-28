import { useCallback, useEffect, useRef, useState } from 'react'
import { Link, useSearchParams } from 'react-router-dom'
import { api, ApiError, formatDate, json } from '../api'
import { runAnswer, runStatusText, STATUS_LABELS } from '../automationRuns'
import { Markdown } from '../components/Markdown'
import type { Automation, AutomationList, NotifySettings, RunRecord, Schedule } from '../types'

const WEEKDAYS = ['月', '火', '水', '木', '金', '土', '日']

// 表の中の「今すぐ実行」。文字の代わりに再生の形を出す（名前は .visually-hidden で読み上げに残す）。
function PlayIcon() {
  return (
    <svg width="16" height="16" viewBox="0 0 24 24" aria-hidden="true" focusable="false">
      <path d="M8 5v14l11-7z" fill="currentColor" />
    </svg>
  )
}

type Draft = Omit<Automation, 'id' | 'state' | 'estimated_runs_per_month' | 'cron'> & { id?: string }

const emptyDraft = (prompt = ''): Draft => ({
  name: prompt ? prompt.slice(0, 30) : '',
  enabled: true,
  prompt,
  schedule: { kind: 'daily', time: '09:00', weekday: 0, day: 1, month: 1, cron: '' },
  conversation_mode: 'new',
  model: 'auto',
  allow_write: false,
  connectors: [],
  notify: { github: false, condition: 'report', signal_field: '', signal_op: '>', signal_value: 0, only_on_change: false, include_summary: false },
  max_runtime_minutes: 20,
})

function describe(s: Schedule): string {
  switch (s.kind) {
    case 'daily':
      return `毎日 ${s.time}`
    case 'weekly':
      return `毎週${WEEKDAYS[s.weekday]}曜 ${s.time}`
    case 'monthly':
      return `毎月${s.day}日 ${s.time}`
    case 'yearly':
      return `毎年${s.month}月${s.day}日 ${s.time}`
    default:
      return `cron: ${s.cron}`
  }
}

export function AutomationsPage({ onUnreadChange }: { onUnreadChange: (n: number) => void }) {
  const [params, setParams] = useSearchParams()
  const [list, setList] = useState<AutomationList | null>(null)
  const [draft, setDraft] = useState<Draft | null>(null)
  // null は「まだ読めていない」。読めたかどうかで、保存済みモデルが一覧にないときの書き方を変える。
  const [models, setModels] = useState<{ id: string; name: string }[] | null>(null)
  const [runs, setRuns] = useState<RunRecord[]>([])
  const [run, setRun] = useState<RunRecord | null>(null)
  const [runsOpen, setRunsOpen] = useState(true)
  const [refreshing, setRefreshing] = useState(false)
  const [error, setError] = useState('')
  const [message, setMessage] = useState('')
  const detailRef = useRef<HTMLDivElement>(null)

  const load = useCallback(async () => {
    const data = await api<AutomationList>('/api/automations')
    setList(data)
    onUnreadChange(data.unread)
    setRuns(await api<RunRecord[]>('/api/automations/runs'))
  }, [onUnreadChange])

  const openRun = useCallback(
    async (automationId: string, runId: string) => {
      const record = await api<RunRecord>(`/api/automations/${automationId}/runs/${runId}`)
      setRun(record)
      if (!record.read) {
        await api(`/api/automations/${automationId}/runs/${runId}/read`, { method: 'POST' })
        load()
      }
    },
    [load],
  )

  useEffect(() => {
    load().catch((e) => setError(e.message))
    // The list is only used to fill the model dropdown, so a failure (e.g. an expired token) is not shown here.
    api<{ models: { id: string; name: string }[] }>('/api/models')
      .then((d) => setModels(d.models))
      .catch(() => undefined)
  }, [load])

  useEffect(() => {
    // スマホでは結果が一覧の下に出るので、選んだら結果まで送る（横に並ぶ画面幅では動かさない）。
    if (run && window.matchMedia('(max-width: 760px)').matches) detailRef.current?.scrollIntoView({ block: 'start' })
  }, [run])

  useEffect(() => {
    // Deep links: "この質問を定期実行" (?new=1&prompt=) and GitHub notifications (?automation=&run=).
    if (params.get('new')) {
      setDraft(emptyDraft(params.get('prompt') ?? ''))
      setParams({}, { replace: true })
    }
    const a = params.get('automation')
    const r = params.get('run')
    if (a && r) openRun(a, r).catch((e) => setError(e.message))
  }, [params, setParams, openRun])

  const save = async () => {
    if (!draft) return
    setError('')
    // 旧 UI では空のモデルも保存できたので、編集したら既定値に正規化しておく。
    const body = json({ ...draft, model: draft.model || 'auto' })
    try {
      if (draft.id) await api(`/api/automations/${draft.id}`, { method: 'PUT', body })
      else await api('/api/automations', { method: 'POST', body })
      setDraft(null)
      setMessage('保存しました')
      await load()
    } catch (e) {
      setError(e instanceof ApiError ? e.message : String(e))
    }
  }

  // 定期実行は裏で動き、結果は終わったときに記録される。画面を開いたままでも新しい実行を取り込めるようにする。
  const refresh = async () => {
    setError('')
    setRefreshing(true)
    try {
      await load()
    } catch (e) {
      setError(e instanceof ApiError ? e.message : String(e))
    } finally {
      setRefreshing(false)
    }
  }

  const remove = async (a: Automation) => {
    if (!window.confirm(`「${a.name}」を削除しますか？`)) return
    await api(`/api/automations/${a.id}`, { method: 'DELETE' })
    await load()
  }

  const runNow = async (a: Automation) => {
    const known = new Set(runs.filter((r) => r.automation_id === a.id).map((r) => r.id))
    await api(`/api/automations/${a.id}/run`, { method: 'POST' })
    setMessage(`「${a.name}」を実行しています。終わると実行履歴に表示されます（最大 20 分）。`)
    // Poll until the new run record appears (runs are saved when they finish).
    const started = Date.now()
    const poll = async () => {
      const latest = await api<RunRecord[]>(`/api/automations/runs?automation_id=${a.id}`).catch(() => [] as RunRecord[])
      const fresh = latest.find((r) => !known.has(r.id))
      if (fresh) {
        await load()
        setMessage(`「${a.name}」の実行が終わりました。`)
        openRun(a.id, fresh.id).catch(() => undefined)
      } else if (Date.now() - started < 25 * 60_000) {
        window.setTimeout(poll, 10_000)
      }
    }
    window.setTimeout(poll, 5_000)
  }

  if (!list) return <div className="panel">{error || '読み込み中…'}</div>
  const usage = list.usage
  return (
    <div className="stack">
      {message && <div className="banner ok">{message}</div>}
      {error && <div className="banner error">{error}</div>}
      <section className="panel">
        <div className="row">
          <h2 className="grow">オートメーション</h2>
          <button className="button primary" onClick={() => setDraft(emptyDraft())}>
            ＋ 新規作成
          </button>
        </div>
        <p className="hint">
          今月の実行 {usage.runs_this_month} / 上限 {usage.monthly_limit} 回・推定 {usage.estimated_runs_per_month} 回/月（1 回の実行で Copilot のリクエストを 1 回以上使います）
        </p>
        {!list.github_notify_configured && <p className="hint">GitHub 通知は未設定です（docs/setup.md のステップ 1-2 と 2-3 で GitHub App を設定すると使えます）。</p>}
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th className="run-now">
                  <span className="visually-hidden">今すぐ実行</span>
                </th>
                <th>名前</th>
                <th>周期</th>
                <th>次回</th>
                <th>前回</th>
                <th>通知</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {list.automations.map((a) => (
                <tr key={a.id} className={a.enabled ? '' : 'muted'}>
                  <td className="run-now">
                    <button className="button icon" onClick={() => runNow(a)} title="今すぐ実行">
                      <PlayIcon />
                      <span className="visually-hidden">「{a.name}」を今すぐ実行</span>
                    </button>
                  </td>
                  <td>{a.name}</td>
                  <td>{describe(a.schedule)}</td>
                  <td>{a.enabled ? formatDate(a.state.next_run_at) : '停止中'}</td>
                  <td>{a.state.last_status ? STATUS_LABELS[a.state.last_status] ?? a.state.last_status : '—'}</td>
                  <td>{a.notify.github ? 'GitHub' : 'アプリ内のみ'}</td>
                  <td className="actions">
                    <button className="link" onClick={() => setDraft({ ...a })}>
                      編集
                    </button>
                    <button className="link danger" onClick={() => remove(a)}>
                      削除
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </section>

      {draft && (
        <Editor
          draft={draft}
          setDraft={setDraft}
          list={list}
          models={models}
          onSave={() => save()}
          onCancel={() => setDraft(null)}
        />
      )}

      <section className="panel">
        <div className="row list-head">
          <h2 className="grow">実行履歴</h2>
          <button className="button small" aria-label={refreshing ? '実行履歴を更新中' : '実行履歴を更新'} onClick={refresh} disabled={refreshing}>
            {refreshing ? '更新中…' : '更新'}
          </button>
          <button className="button small" aria-expanded={runsOpen} aria-controls="run-list" onClick={() => setRunsOpen(!runsOpen)}>
            {runsOpen ? '一覧を閉じる' : '一覧を開く'}
          </button>
        </div>
        {/* 履歴が増えても結果が押し下げられないように、一覧は高さを決めてスクロールさせ、結果はその横（スマホでは下）に出す。
            一覧を閉じると結果が幅いっぱいに広がる */}
        <div className={`runs-layout${runsOpen ? '' : ' list-closed'}`}>
          <ul id="run-list" className="runs" aria-label="実行履歴の一覧" hidden={!runsOpen}>
            {runs.map((r) => (
              <li key={r.id} className={[r.read ? '' : 'unread', run?.id === r.id ? 'selected' : ''].filter(Boolean).join(' ')}>
                <button className="link" aria-current={run?.id === r.id ? 'true' : undefined} onClick={() => openRun(r.automation_id, r.id)}>
                  <span className="run-name">{r.name}</span>
                  <small>
                    {formatDate(r.started_at)}・{runStatusText(r)}
                    {r.notified && ' 📣'}
                  </small>
                </button>
              </li>
            ))}
            {runs.length === 0 && <li className="hint">まだ実行されていません。</li>}
          </ul>
          {!run && runs.length > 0 && (
            <p className="hint run-detail">
              {runsOpen ? '一覧から実行を選ぶと、結果をここに表示します。' : '一覧を開いて実行を選ぶと、結果をここに表示します。'}
            </p>
          )}
          {run && (
            <div className="run-detail" ref={detailRef}>
              <h3>
                {run.name}（{formatDate(run.started_at)}・{runStatusText(run)}）
              </h3>
              {run.chat_thread_id && (
                <p>
                  <Link to={`/chat?thread=${encodeURIComponent(run.chat_thread_id)}&run=${encodeURIComponent(run.id)}`}>
                    チャットで見る
                  </Link>
                </p>
              )}
              {run.error && <div className="banner error">{run.error}</div>}
              <Markdown text={runAnswer(run)} />
              {run.signals && Object.keys(run.signals).length > 0 && <p className="hint">ツールの結果: {JSON.stringify(run.signals)}</p>}
              {run.issue_url && (
                <p className="hint">
                  GitHub 通知:{' '}
                  <a href={run.issue_url} target="_blank" rel="noopener noreferrer">
                    Issue
                  </a>
                </p>
              )}
              {run.notify_error && <p className="error-text">通知エラー: {run.notify_error}</p>}
              <details>
                <summary>実行の詳細（ツール・書き込み）</summary>
                <pre>{(run.events ?? []).map((e) => JSON.stringify(e)).join('\n')}</pre>
              </details>
            </div>
          )}
        </div>
      </section>
    </div>
  )
}

function Editor({
  draft,
  setDraft,
  list,
  models,
  onSave,
  onCancel,
}: {
  draft: Draft
  setDraft: (d: Draft) => void
  list: AutomationList
  models: { id: string; name: string }[] | null
  onSave: () => void
  onCancel: () => void
}) {
  const set = <K extends keyof Draft>(key: K, value: Draft[K]) => setDraft({ ...draft, [key]: value })
  const setSchedule = (patch: Partial<Schedule>) => set('schedule', { ...draft.schedule, ...patch })
  const setNotify = (patch: Partial<NotifySettings>) => set('notify', { ...draft.notify, ...patch })
  const s = draft.schedule
  const n = draft.notify
  // The saved model stays selectable even when the list is unavailable or no longer offers it, so opening the
  // editor never switches an automation to another model by itself. An empty model behaves like the default.
  const model = draft.model || 'auto'
  const available = models ?? []
  const modelOptions = [
    { id: 'auto', name: '自動（おまかせ）' },
    ...available.filter((m) => m.id !== 'auto'),
    // Only a loaded list can tell that a model is gone; an unreachable /api/models says nothing about it.
    ...(model !== 'auto' && !available.some((m) => m.id === model)
      ? [{ id: model, name: models ? `${model}（一覧にありません）` : model }]
      : []),
  ]
  return (
    <section className="panel editor-panel">
      <h2>{draft.id ? 'オートメーションの編集' : '新しいオートメーション'}</h2>
      <label>
        名前
        <input value={draft.name} onChange={(e) => set('name', e.target.value)} maxLength={80} />
      </label>
      <label>
        実行する指示（{'{{today}} {{year}} {{month}} {{weekday}}'} が使えます）
        <textarea rows={10} value={draft.prompt} onChange={(e) => set('prompt', e.target.value)} />
      </label>
      <div className="row wrap">
        <label>
          周期
          <select value={s.kind} onChange={(e) => setSchedule({ kind: e.target.value as Schedule['kind'] })}>
            <option value="daily">毎日</option>
            <option value="weekly">毎週</option>
            <option value="monthly">毎月</option>
            <option value="yearly">毎年</option>
            <option value="cron">cron 式</option>
          </select>
        </label>
        {s.kind === 'weekly' && (
          <label>
            曜日
            <select value={s.weekday} onChange={(e) => setSchedule({ weekday: Number(e.target.value) })}>
              {WEEKDAYS.map((w, i) => (
                <option key={w} value={i}>
                  {w}曜
                </option>
              ))}
            </select>
          </label>
        )}
        {s.kind === 'yearly' && (
          <label>
            月
            <input type="number" min={1} max={12} value={s.month} onChange={(e) => setSchedule({ month: Number(e.target.value) })} />
          </label>
        )}
        {(s.kind === 'monthly' || s.kind === 'yearly') && (
          <label>
            日
            <input type="number" min={1} max={31} value={s.day} onChange={(e) => setSchedule({ day: Number(e.target.value) })} />
          </label>
        )}
        {s.kind === 'cron' ? (
          <label>
            cron（分 時 日 月 曜日・日本時間）
            <input value={s.cron} onChange={(e) => setSchedule({ cron: e.target.value })} placeholder="0 9 * * 1-5" />
          </label>
        ) : (
          <label>
            時刻（日本時間）
            <input type="time" value={s.time} onChange={(e) => setSchedule({ time: e.target.value })} />
          </label>
        )}
      </div>
      <div className="row wrap">
        <label>
          会話
          <select value={draft.conversation_mode} onChange={(e) => set('conversation_mode', e.target.value as Draft['conversation_mode'])}>
            <option value="new">毎回新しい会話</option>
            <option value="continue">同じ会話に続ける（前回を踏まえる）</option>
          </select>
        </label>
        <label>
          モデル
          <select value={model} onChange={(e) => set('model', e.target.value)}>
            {modelOptions.map((m) => (
              <option key={m.id} value={m.id}>
                {m.name}
              </option>
            ))}
          </select>
        </label>
        <label>
          最大実行時間（分、20 まで）
          <input type="number" min={1} max={20} value={draft.max_runtime_minutes} onChange={(e) => set('max_runtime_minutes', Number(e.target.value))} />
        </label>
      </div>
      <label className="check">
        <input type="checkbox" checked={draft.allow_write} onChange={(e) => set('allow_write', e.target.checked)} />
        メモリ・ノート・保有銘柄の書き換えを許可する（既定は読み取り専用）
      </label>
      <label className="check">
        <input type="checkbox" checked={draft.enabled} onChange={(e) => set('enabled', e.target.checked)} />
        有効
      </label>
      <fieldset>
        <legend>使うコネクタ（API キーは Azure のシークレットで管理）</legend>
        {list.connectors.map((c) => (
          <label key={c.name} className="check">
            <input
              type="checkbox"
              checked={draft.connectors.includes(c.name)}
              onChange={(e) => set('connectors', e.target.checked ? [...draft.connectors, c.name] : draft.connectors.filter((x) => x !== c.name))}
            />
            {c.label} {!c.configured && <small>（キー未登録）</small>}
          </label>
        ))}
      </fieldset>
      <fieldset>
        <legend>通知</legend>
        <p className="hint">実行結果は常にアプリ内に記録されます。GitHub Issue での通知は、ここでオンにしたものだけです。</p>
        <label className="check">
          <input type="checkbox" checked={n.github} onChange={(e) => setNotify({ github: e.target.checked })} />
          GitHub で通知する
        </label>
        {n.github && (
          <>
            <label>
              通知する条件
              <select value={n.condition} onChange={(e) => setNotify({ condition: e.target.value as NotifySettings['condition'] })}>
                <option value="report">Copilot が「知らせるべき」と報告したとき</option>
                <option value="signal">ツールの結果の値で判定（例: 空室数 &gt; 0）</option>
                <option value="always">毎回</option>
              </select>
            </label>
            {n.condition === 'signal' && (
              <div className="row wrap">
                <input placeholder="値の名前（例: vacancy_count）" value={n.signal_field} onChange={(e) => setNotify({ signal_field: e.target.value })} />
                <select value={n.signal_op} onChange={(e) => setNotify({ signal_op: e.target.value as NotifySettings['signal_op'] })}>
                  {['>', '>=', '==', '!=', '<', '<='].map((op) => (
                    <option key={op}>{op}</option>
                  ))}
                </select>
                <input type="number" value={n.signal_value} onChange={(e) => setNotify({ signal_value: Number(e.target.value) })} />
              </div>
            )}
            <label className="check">
              <input type="checkbox" checked={n.only_on_change} onChange={(e) => setNotify({ only_on_change: e.target.checked })} />
              前回は条件を満たさず、今回満たしたときだけ通知する
            </label>
            <label className="check">
              <input type="checkbox" checked={n.include_summary} onChange={(e) => setNotify({ include_summary: e.target.checked })} />
              Issue に要約を含める（既定は含めない。GitHub に内容が残ります）
            </label>
          </>
        )}
      </fieldset>
      <div className="row">
        <button className="button primary" onClick={onSave} disabled={!draft.name || !draft.prompt}>
          保存
        </button>
        <button className="button" onClick={onCancel}>
          キャンセル
        </button>
      </div>
    </section>
  )
}
