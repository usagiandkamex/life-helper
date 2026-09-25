import { formatDate } from './api'
import { applyEvent, type Item } from './chatItems'
import type { RunRecord } from './types'

export const STATUS_LABELS: Record<string, string> = {
  success: '成功',
  error: '失敗',
  timeout: 'タイムアウト',
  reauth: '再ログインが必要',
  skipped_limit: '上限のため未実行',
}

// Leaves room for the question within the 20,000-character limit of a chat message.
const QUOTE_PROMPT_LIMIT = 2000
const QUOTE_RESULT_LIMIT = 8000

export const statusLabel = (status: string) => STATUS_LABELS[status] ?? status

export function runItems(run: RunRecord): Item[] {
  const items = (run.events ?? []).reduce<Item[]>((acc, ev) => applyEvent(acc, ev), [])
  // report_result is shown as the report card instead of a tool call.
  return items.filter((i) => !(i.kind === 'tool' && i.name === 'report_result'))
}

export const hasAnswer = (items: Item[]) => items.some((i) => i.kind === 'assistant' && i.text.trim())

// Why a run did not succeed, for runs that stopped before or during the Copilot session.
export const runProblem = (run: RunRecord) => (run.status === 'success' ? '' : run.error || run.summary || statusLabel(run.status))

function clip(text: string, limit: number): string {
  const trimmed = text.trim()
  return trimmed.length > limit ? `${trimmed.slice(0, limit)}\n…（長いため以降を省略）` : trimmed
}

export function quoteDraft(run: RunRecord): string {
  // A run can answer and still fail afterwards, so the answer is quoted together with the reason.
  const answer = run.final_message || run.report?.summary || (run.status === 'success' ? run.summary : '') || ''
  const problem = runProblem(run)
  const result = [answer, problem && problem !== answer ? `（${problem}）` : ''].filter(Boolean).join('\n\n')
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
