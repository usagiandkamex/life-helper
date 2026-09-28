import { formatDate } from './api'
import { applyEvent, type Item } from './chatItems'
import type { RunRecord } from './types'

export const STATUS_LABELS: Record<string, string> = {
  running: '実行中',
  // A run that stopped while it was running (the app or the job ended); the API reports it instead of "running".
  interrupted: '中断',
  success: '成功',
  error: '失敗',
  timeout: 'タイムアウト',
  reauth: '再ログインが必要',
  skipped_limit: '上限のため未実行',
}

// The instruction is quoted in full (automation prompts are capped at 8,000) and the result is clipped so both,
// plus the question, stay within the 50,000-character limit of a chat message.
const QUOTE_PROMPT_LIMIT = 8000
const QUOTE_RESULT_LIMIT = 8000

export const statusLabel = (status: string) => STATUS_LABELS[status] ?? status

// 実行は始まったときに履歴に記録され、終わったときに結果で置き換わる。実行中と、実行中のまま止まった記録には、
// まだ読むものがない（既読にも未読にもせず、結果の代わりに状況だけを見せる）。
export const hasResult = (run: RunRecord) => run.status !== 'running' && run.status !== 'interrupted'

// How long a run took. Runs recorded before a crash (and older records) have no finished_at, so it can be empty.
export function runDuration(run: RunRecord): string {
  const started = Date.parse(run.started_at ?? '')
  const finished = Date.parse(run.finished_at ?? '')
  if (Number.isNaN(started) || Number.isNaN(finished) || finished < started) return ''
  const seconds = Math.floor((finished - started) / 1000)
  if (seconds < 60) return `${seconds}秒`
  const minutes = Math.floor(seconds / 60)
  const rest = seconds % 60
  return rest ? `${minutes}分${rest}秒` : `${minutes}分`
}

// The status of a run with the time it took, as shown in the run history ("成功 10分").
export const runStatusText = (run: RunRecord) => [statusLabel(run.status), runDuration(run)].filter(Boolean).join(' ')

export function runItems(run: RunRecord): Item[] {
  const items = (run.events ?? []).reduce<Item[]>((acc, ev) => applyEvent(acc, ev), [])
  // report_result is shown as the report card instead of a tool call.
  return items.filter((i) => !(i.kind === 'tool' && i.name === 'report_result'))
}

export const hasAnswer = (items: Item[]) => items.some((i) => i.kind === 'assistant' && i.text.trim())

// Why a run did not succeed, for runs that stopped before or during the Copilot session.
export const runProblem = (run: RunRecord) => (run.status === 'success' ? '' : run.error || run.summary || statusLabel(run.status))

// What the run produced. report_result carries the result the user is meant to read, but older runs and runs that
// stopped early have only summary or the last message, so all three are combined without repeating the same text.
export function runAnswer(run: RunRecord): string {
  const parts: string[] = []
  for (const value of [run.report?.summary, run.summary, run.final_message]) {
    const text = (value ?? '').trim()
    if (!text || parts.some((p) => p.includes(text))) continue
    // summary is a clipped copy of the last message in runs recorded without a report, so the full text wins.
    const clipped = parts.findIndex((p) => text.includes(p))
    if (clipped >= 0) parts[clipped] = text
    else parts.push(text)
  }
  return parts.join('\n\n')
}

function clip(text: string, limit: number): string {
  const trimmed = text.trim()
  return trimmed.length > limit ? `${trimmed.slice(0, limit)}\n…（長いため以降を省略）` : trimmed
}

export function quoteDraft(run: RunRecord): string {
  // A run can answer and still fail afterwards, so the answer is quoted together with the reason.
  const answer = runAnswer(run)
  const problem = runProblem(run)
  const result = [answer, problem && !answer.includes(problem) ? `（${problem}）` : ''].filter(Boolean).join('\n\n')
  const status = run.status === 'success' ? '' : `（${statusLabel(run.status)}）`
  return [
    `オートメーション「${run.name}」（${formatDate(run.started_at)}）の結果について質問です。`,
    '',
    '--- 指示 ---',
    clip(run.prompt ?? '', QUOTE_PROMPT_LIMIT),
    `--- 結果${status} ---`,
    clip(result, QUOTE_RESULT_LIMIT),
    '--- ここまで ---',
    '',
    '',
  ].join('\n')
}
