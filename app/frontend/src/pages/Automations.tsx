import { useCallback, useEffect, useId, useLayoutEffect, useRef, useState, type InputHTMLAttributes } from 'react'
import { Link, useSearchParams } from 'react-router-dom'
import { api, ApiError, formatDate, json } from '../api'
import { hasResult, runAnswer, runStatusText, STATUS_LABELS } from '../automationRuns'
import { Markdown } from '../components/Markdown'
import { SelectAllButton } from '../components/SelectAllButton'
import { draftStore } from '../drafts'
import type { Automation, AutomationList, NotifySettings, RunNowResult, RunRecord, Schedule } from '../types'

const WEEKDAYS = ['月', '火', '水', '木', '金', '土', '日']
const INTERRUPTED_WATCH_MS = 5 * 60_000
// ジョブで実行する「今すぐ実行」は、ジョブの起動を待つので、記録が出るまで長めに待つ。
const RUN_NOW_JOB_WAIT_MS = 5 * 60_000

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

// 保存していない編集（オートメーションの id ごと、新規は ''）。ほかを編集したり、画面を移ったりしても戻せるように残す。
const editorDrafts = draftStore<Draft>()
// 画面を離れたときに開いていた編集の id。戻ったら同じ編集を開き直す。
const openEditorKey = draftStore<string>()
const editorKey = (d: Draft) => d.id ?? ''

const isIntIn = (v: number, min: number, max: number) => Number.isInteger(v) && v >= min && v <= max

// 数値の入力欄。消して打ち直している間は空欄のままにする（0 で埋めない）。空欄は NaN で持ち、保存前に止める。
function NumberInput({
  value,
  onValueChange,
  ...rest
}: Omit<InputHTMLAttributes<HTMLInputElement>, 'type' | 'value' | 'onChange'> & { value: number; onValueChange: (v: number) => void }) {
  return <input type="number" {...rest} value={Number.isNaN(value) ? '' : value} onChange={(e) => onValueChange(e.currentTarget.valueAsNumber)} />
}

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
  const [draft, setDraft] = useState<Draft | null>(() => {
    const key = openEditorKey.get('')
    return key === undefined ? null : editorDrafts.get(key) ?? null
  })
  // 残しておいた編集を開いたか（編集欄に、そのことを書いておく）。
  const [restored, setRestored] = useState(() => draft !== null)
  const draftRef = useRef(draft)
  // 開いたときの内容。ここから変わっていれば、閉じずに離れても編集を残す（画面に戻って開いた編集は、残したものなので空にしておく）。
  const draftBaseRef = useRef('')
  // null は「まだ読めていない」。読めたかどうかで、保存済みモデルが一覧にないときの書き方を変える。
  const [models, setModels] = useState<{ id: string; name: string }[] | null>(null)
  const [runs, setRuns] = useState<RunRecord[]>([])
  const [run, setRun] = useState<RunRecord | null>(null)
  const [runsOpen, setRunsOpen] = useState(true)
  const [refreshing, setRefreshing] = useState(false)
  const [deletingRunId, setDeletingRunId] = useState<string | null>(null)
  const [pendingRunIds, setPendingRunIds] = useState<Set<string>>(new Set())
  const pendingRunIdsRef = useRef(new Set<string>())
  // 実行中の記録を見張るためのカウンタ（進めると、次の読み直しが予約される）。
  const [tick, setTick] = useState(0)
  const [error, setError] = useState('')
  const [message, setMessage] = useState('')
  const detailRef = useRef<HTMLDivElement>(null)
  const selectedRunIdRef = useRef<string | null>(null)
  // 実際に表示できた記録の id。取得に失敗したとき、選択参照をここへ戻す拠り所にする。
  const displayedRunIdRef = useRef<string | null>(null)
  const selectionGenerationRef = useRef(0)
  const openRunRequestRef = useRef(0)
  const interruptedWatchRef = useRef<{ runId: string; since: number } | null>(null)
  const loadRequestRef = useRef(0)

  useLayoutEffect(() => {
    draftRef.current = draft
  })

  // 開いている編集が変わっていれば残し、変わっていなければ前に残したものも消す。残したかどうかを返す。
  const keepEditor = useCallback(() => {
    const d = draftRef.current
    if (!d) return false
    const changed = JSON.stringify(d) !== draftBaseRef.current
    if (changed) editorDrafts.set(editorKey(d), d)
    else editorDrafts.delete(editorKey(d))
    return changed
  }, [])

  const openEditor = useCallback(
    (fresh: Draft, restore = true) => {
      keepEditor()
      const kept = restore ? editorDrafts.get(editorKey(fresh)) : undefined
      draftBaseRef.current = JSON.stringify(fresh)
      setDraft(kept ?? fresh)
      setRestored(kept !== undefined)
    },
    [keepEditor],
  )

  const closeEditor = () => {
    if (draft) editorDrafts.delete(editorKey(draft))
    setDraft(null)
  }

  useEffect(
    () => () => {
      if (keepEditor()) openEditorKey.set('', editorKey(draftRef.current as Draft))
      else openEditorKey.delete('')
    },
    [keepEditor],
  )

  const load = useCallback(async () => {
    // 見張り・更新・削除の読み込みは重なることがある。後から始めた読み込みだけを反映し、古い応答で消した記録を戻さない。
    const request = ++loadRequestRef.current
    const data = await api<AutomationList>('/api/automations')
    if (request !== loadRequestRef.current) return
    setList(data)
    onUnreadChange(data.unread)
    const loaded = await api<RunRecord[]>('/api/automations/runs')
    if (request !== loadRequestRef.current) return
    setRuns(loaded)
  }, [onUnreadChange])

  const openRun = useCallback(
    async (automationId: string, runId: string, select = true) => {
      // 取得に失敗したら、楽観的に進めた選択を表示中の記録へ戻すために控えておく。
      const restoreRunId = displayedRunIdRef.current
      const restoreInterruptedWatch = interruptedWatchRef.current
      if (select) {
        selectionGenerationRef.current += 1
        if (selectedRunIdRef.current !== runId) interruptedWatchRef.current = null
        selectedRunIdRef.current = runId
      } else if (selectedRunIdRef.current !== runId) {
        return
      }
      const request = ++openRunRequestRef.current
      let record: RunRecord
      try {
        record = await api<RunRecord>(`/api/automations/${automationId}/runs/${runId}`)
      } catch (e) {
        // 選択参照だけが先へ進むと、表示中の記録のポーリングが二度と回らなくなる。
        // ほかの選択に追い越されていなければ、選択参照と見張りを表示中の記録へ戻す。
        if (selectedRunIdRef.current === runId && openRunRequestRef.current === request) {
          selectedRunIdRef.current = restoreRunId
          interruptedWatchRef.current = restoreInterruptedWatch
        }
        throw e
      }
      if (selectedRunIdRef.current !== runId || openRunRequestRef.current !== request) return
      if (record.status === 'interrupted' && !hasResult(record)) {
        if (interruptedWatchRef.current?.runId !== record.id) {
          interruptedWatchRef.current = { runId: record.id, since: Date.now() }
        }
      } else if (interruptedWatchRef.current?.runId === record.id) {
        interruptedWatchRef.current = null
      }
      setRun(record)
      displayedRunIdRef.current = record.id
      // 実行中の記録には読むものがないので、既読にしない（結果が出たときに未読のまま残す）。
      if (!record.read && hasResult(record)) {
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

  // 実行中の記録にはまだ結果がないので、終わるまで読み直して、開いたままでも結果に変わるようにする。
  // 中断表示は保存済みの running を書き換えないため、遅れて完了する可能性がある。選択中なら 5 分だけ見張る。
  // tick は毎回進めるので、途中の読み込みが失敗しても見張りは続く。
  const watchingOthers =
    runs.some((r) => r.status === 'running') || (list?.running_automation_ids.length ?? 0) > 0
  const watching = watchingOthers || (run !== null && !hasResult(run))
  useEffect(() => {
    if (!watching) return
    const interruptedWatch = interruptedWatchRef.current
    const interruptedWatchExpired =
      run?.status === 'interrupted' &&
      interruptedWatch?.runId === run.id &&
      Date.now() - interruptedWatch.since >= INTERRUPTED_WATCH_MS
    if (!watchingOthers && interruptedWatchExpired) return
    let cancelled = false
    const timer = window.setTimeout(async () => {
      await load().catch(() => undefined)
      // 読み込んでいる間にほかの実行を選ぶこともあるので、選び直されていたら開き直さない。
      if (cancelled) return
      if (run && !hasResult(run) && !interruptedWatchExpired)
        await openRun(run.automation_id, run.id, false).catch(() => undefined)
      if (!cancelled) setTick((n) => n + 1)
    }, 10_000)
    return () => {
      cancelled = true
      window.clearTimeout(timer)
    }
  }, [watching, watchingOthers, tick, run, load, openRun])

  useEffect(() => {
    // Deep links: "この質問を定期実行" (?new=1&prompt=) and GitHub notifications (?automation=&run=).
    if (params.get('new')) {
      // 渡された質問で始める（書きかけの新規作成は残しておくが、ここでは戻さない）。
      openEditor(emptyDraft(params.get('prompt') ?? ''), false)
      setParams({}, { replace: true })
    }
    const a = params.get('automation')
    const r = params.get('run')
    if (a && r) openRun(a, r).catch((e) => setError(e.message))
  }, [params, setParams, openRun, openEditor])

  const save = async () => {
    if (!draft) return
    setError('')
    // 旧 UI では空のモデルも保存できたので、編集したら既定値に正規化しておく。
    // 今の周期で使わない欄は、打ちかけ（空欄など）のままだと保存できないので、使える値に戻しておく。
    const { schedule: s, notify: n } = draft
    const body = json({
      ...draft,
      model: draft.model || 'auto',
      schedule: { ...s, month: isIntIn(s.month, 1, 12) ? s.month : 1, day: isIntIn(s.day, 1, 31) ? s.day : 1 },
      notify: { ...n, signal_value: Number.isFinite(n.signal_value) ? n.signal_value : 0 },
    })
    try {
      if (draft.id) await api(`/api/automations/${draft.id}`, { method: 'PUT', body })
      else await api('/api/automations', { method: 'POST', body })
      editorDrafts.delete(editorKey(draft))
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
    editorDrafts.delete(a.id)
    await load()
  }

  // 実行の記録を 1 件消す。チャットの 🤖 会話からも消える。
  const removeRun = async (r: RunRecord) => {
    // 「同じ会話に続ける」は 1 つの Copilot の会話を使い続けるので、記録を消しても Copilot はその回の内容を覚えている。
    const memory =
      r.conversation_mode === 'continue'
        ? '\n\n「同じ会話に続ける」のオートメーションです。Copilot が会話の中で覚えている内容は消えず、次の実行でも踏まえて答えます。'
        : ''
    if (!window.confirm(`「${r.name}」（${formatDate(r.started_at)}）の実行の記録を削除しますか？\nチャットの 🤖 会話からも消えます。${memory}`)) return
    setError('')
    setMessage('')
    setDeletingRunId(r.id)
    try {
      let gone = false
      try {
        const res = await api<{ unread: number }>(`/api/automations/${r.automation_id}/runs/${r.id}`, { method: 'DELETE' })
        onUnreadChange(res.unread)
      } catch (e) {
        // ほかの画面で消した記録や、保存期間を過ぎて消えた記録は、消せたものとして扱う。
        if (!(e instanceof ApiError && e.status === 404)) throw e
        gone = true
      }
      setRuns((current) => current.filter((x) => x.id !== r.id))
      // 開いていた記録なら選択を外す（読み込み中の取得や見張りが、消した記録を開き直さないようにする）。
      if (selectedRunIdRef.current === r.id) {
        selectionGenerationRef.current += 1
        openRunRequestRef.current += 1
        selectedRunIdRef.current = null
        displayedRunIdRef.current = null
        interruptedWatchRef.current = null
        setRun(null)
      }
      setMessage(gone ? 'この実行の記録はすでに削除されていました。' : '実行の記録を削除しました。')
      await load()
    } catch (e) {
      setError(e instanceof ApiError ? e.message : String(e))
    } finally {
      setDeletingRunId(null)
    }
  }

  const runNow = async (a: Automation) => {
    if (pendingRunIdsRef.current.has(a.id) || list?.running_automation_ids.includes(a.id)) return
    pendingRunIdsRef.current.add(a.id)
    setPendingRunIds(new Set(pendingRunIdsRef.current))
    const selectionGeneration = selectionGenerationRef.current
    setError('')
    try {
      const startedRun = await api<RunNowResult>(`/api/automations/${a.id}/run`, { method: 'POST' })
      // 画面の一覧は、ほかの画面で保存した設定より古いことがあるので、API が返した保存済みの設定で伝える。
      const name = startedRun.name ?? a.name
      const minutes = startedRun.max_runtime_minutes ?? a.max_runtime_minutes
      const limit = `実行時間の上限は、「最大実行時間」の設定の ${minutes} 分です（「編集」で 60 分まで変えられます）。`
      // 本番では、アプリが使われないと止まるため、実行はジョブに任せる。ジョブの起動には数分かかることがある。
      const inJob = startedRun.runner === 'job'
      if (inJob && !startedRun.job_started) {
        setMessage(`「${name}」の実行を受け付けました。実行用のジョブをすぐに起動できなかったため、次の定期確認（15 分以内）で実行します。始まると実行履歴に出ます。`)
        await load()
        return
      }
      setMessage(
        inJob
          ? `「${name}」の実行を受け付けました。実行用のジョブが起動すると（数分かかることがあります）実行履歴に出て、終わると結果に変わります。${limit}ジョブの起動を待つ時間は含みません。`
          : `「${name}」の実行を始めました。実行中も実行履歴に出て、終わると結果に変わります。${limit}`,
      )
      if (inJob) await load()
      // 同時に定期実行が始まっても取り違えないよう、API が割り当てた記録だけを待つ。
      const started = Date.now()
      await new Promise((resolve) => window.setTimeout(resolve, 2_000))
      while (true) {
        const record = await api<RunRecord>(`/api/automations/${a.id}/runs/${startedRun.run_id}`).catch(() => null)
        if (record) {
          await load()
          if (selectionGenerationRef.current === selectionGeneration) {
            openRun(a.id, startedRun.run_id).catch(() => undefined)
          }
          return
        }
        if (Date.now() - started >= (inJob ? RUN_NOW_JOB_WAIT_MS : 60_000)) {
          // 前の実行が続いていると、そのオートメーションは実行されない（API は受け付けたことだけを返す）。
          setMessage(
            inJob
              ? `「${name}」の実行はまだ始まっていません。実行用のジョブが起動すると実行履歴に出ます。`
              : `「${name}」の実行を確認できませんでした。ほかの実行が続いている可能性があります。`,
          )
          return
        }
        await new Promise((resolve) => window.setTimeout(resolve, 3_000))
      }
    } catch (e) {
      setError(e instanceof ApiError ? e.message : String(e))
    } finally {
      pendingRunIdsRef.current.delete(a.id)
      setPendingRunIds(new Set(pendingRunIdsRef.current))
    }
  }

  if (!list) return <div className="panel">{error || '読み込み中…'}</div>
  const usage = list.usage
  // 同じオートメーションは同時に実行できないので、実行中の記録がある間は「今すぐ実行」を押せないようにする。
  // 実行履歴は新しい 50 件までなので、上限のない一覧側の実行中のオートメーションで決める。
  const running = new Set([...list.running_automation_ids, ...pendingRunIds])
  return (
    <div className="stack">
      {message && <div className="banner ok">{message}</div>}
      {error && <div className="banner error">{error}</div>}
      <section className="panel">
        <div className="row">
          <h2 className="grow">オートメーション</h2>
          <button className="button primary" onClick={() => openEditor(emptyDraft())}>
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
                    <button
                      className="button icon"
                      onClick={() => runNow(a)}
                      disabled={running.has(a.id)}
                      title={running.has(a.id) ? '実行中です' : '今すぐ実行'}
                    >
                      <PlayIcon />
                      <span className="visually-hidden">
                        {running.has(a.id) ? `「${a.name}」は実行中です` : `「${a.name}」を今すぐ実行`}
                      </span>
                    </button>
                  </td>
                  <td>{a.name}</td>
                  <td>{describe(a.schedule)}</td>
                  <td>{a.enabled ? formatDate(a.state.next_run_at) : '停止中'}</td>
                  <td>{a.state.last_status ? STATUS_LABELS[a.state.last_status] ?? a.state.last_status : '—'}</td>
                  <td>{a.notify.github ? 'GitHub' : 'アプリ内のみ'}</td>
                  <td className="actions">
                    <button className="link" onClick={() => openEditor({ ...a })}>
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
          restored={restored}
          onSave={() => save()}
          onCancel={closeEditor}
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
              <li key={r.id} className={[r.read || !hasResult(r) ? '' : 'unread', run?.id === r.id ? 'selected' : ''].filter(Boolean).join(' ')}>
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
              <div className="row run-detail-head">
                <h3 className="grow">
                  {run.name}（{formatDate(run.started_at)}・{runStatusText(run)}）
                </h3>
                {/* 実行中の記録は、終わったときに結果で置き換わるので消せない（中断した記録は消せる） */}
                <button
                  className="button small danger"
                  onClick={() => removeRun(run)}
                  disabled={run.status === 'running' || deletingRunId === run.id}
                  title={run.status === 'running' ? '実行中は削除できません' : 'この実行の記録を削除'}
                >
                  {deletingRunId === run.id ? '削除中…' : '削除'}
                </button>
              </div>
              {/* 実行中の記録には結果がない（実行内容は終わってから記録される）。途切れた記録には途中までの出力だけが残ることがある */}
              {!hasResult(run) ? (
                run.status === 'running' ? (
                  <p className="hint">実行中です。終わると、ここに結果を表示します。</p>
                ) : runAnswer(run) ? (
                  <>
                    <p className="hint">
                      実行中のまま記録が途切れました（アプリや定期実行のジョブが止まった可能性があります）。途中までの出力を表示します。
                    </p>
                    <Markdown text={runAnswer(run)} />
                  </>
                ) : (
                  <p className="hint">
                    実行中のまま記録が途切れました（アプリや定期実行のジョブが止まった可能性があります）。結果は残っていません。
                  </p>
                )
              ) : (
                <>
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
                </>
              )}
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
  restored,
  onSave,
  onCancel,
}: {
  draft: Draft
  setDraft: (d: Draft) => void
  list: AutomationList
  models: { id: string; name: string }[] | null
  restored: boolean
  onSave: () => void
  onCancel: () => void
}) {
  const promptId = useId()
  const promptRef = useRef<HTMLTextAreaElement>(null)
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
  // 空欄や範囲外の数値はサーバーで弾かれる（理由が出ない）ので、ここで止めて直す欄を書いておく。
  const problems = [
    !isIntIn(draft.max_runtime_minutes, 1, 60) && '最大実行時間は 1〜60 の整数で入れてください。',
    s.kind === 'yearly' && !isIntIn(s.month, 1, 12) && '月は 1〜12 の整数で入れてください。',
    (s.kind === 'monthly' || s.kind === 'yearly') && !isIntIn(s.day, 1, 31) && '日は 1〜31 の整数で入れてください。',
    n.github && n.condition === 'signal' && !Number.isFinite(n.signal_value) && '通知の判定に使う数値を入れてください。',
  ].filter((p): p is string => typeof p === 'string')
  return (
    <section className="panel editor-panel">
      <h2>{draft.id ? 'オートメーションの編集' : '新しいオートメーション'}</h2>
      {restored && <p className="hint">保存していない編集を戻しました（「キャンセル」で破棄できます）。</p>}
      <label>
        名前
        <input value={draft.name} onChange={(e) => set('name', e.target.value)} maxLength={80} />
      </label>
      <div className="field">
        <div className="field-head">
          <label htmlFor={promptId}>実行する指示（{'{{today}} {{year}} {{month}} {{weekday}}'} が使えます）</label>
          <SelectAllButton target={promptRef} />
        </div>
        <textarea id={promptId} ref={promptRef} rows={10} value={draft.prompt} onChange={(e) => set('prompt', e.target.value)} />
      </div>
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
            <NumberInput min={1} max={12} value={s.month} onValueChange={(month) => setSchedule({ month })} />
          </label>
        )}
        {(s.kind === 'monthly' || s.kind === 'yearly') && (
          <label>
            日
            <NumberInput min={1} max={31} value={s.day} onValueChange={(day) => setSchedule({ day })} />
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
          最大実行時間（分、60 まで）
          <NumberInput min={1} max={60} value={draft.max_runtime_minutes} onValueChange={(v) => set('max_runtime_minutes', v)} />
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
                <NumberInput value={n.signal_value} onValueChange={(signal_value) => setNotify({ signal_value })} aria-label="比べる値" />
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
      {problems.map((p) => (
        <p key={p} className="error-text">
          {p}
        </p>
      ))}
      <div className="row">
        <button className="button primary" onClick={onSave} disabled={!draft.name || !draft.prompt || problems.length > 0}>
          保存
        </button>
        <button className="button" onClick={onCancel}>
          キャンセル
        </button>
      </div>
    </section>
  )
}
