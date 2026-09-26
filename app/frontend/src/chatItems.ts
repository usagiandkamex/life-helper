import type { ApprovalStatus, ChartData, HistoryMessage, Screenshot, TurnEvent } from './types'

export type ApprovalItem = {
  kind: 'approval'
  id: string
  turnId: string
  path: string
  diff: string
  status: ApprovalStatus
  written?: boolean
}

export type Item =
  | { kind: 'user'; text: string }
  | { kind: 'assistant'; text: string; streaming?: boolean }
  | {
      kind: 'tool'
      id?: string
      name: string
      args: string
      success?: boolean
      error?: string
      chart?: ChartData
      screenshot?: Screenshot
    }
  | { kind: 'file_write'; path: string; diff: string }
  | ApprovalItem
  | { kind: 'error'; message: string }
  | { kind: 'note'; text: string }

// turnId is the chat turn the events belong to; approval cards post their decision to it.
export function applyEvent(items: Item[], ev: TurnEvent, turnId = ''): Item[] {
  const next = [...items]
  // Not always the last item: a message sent with 「すぐに送信」 can appear while the answer is still streaming.
  const streaming = next.findLastIndex((i) => i.kind === 'assistant' && i.streaming)
  const current = streaming >= 0 ? (next[streaming] as Extract<Item, { kind: 'assistant' }>) : null
  const closeStreaming = () => {
    if (current) next[streaming] = { ...current, streaming: false }
  }
  const approvalIndex = (id: string) => next.findIndex((i) => i.kind === 'approval' && i.id === id)
  switch (ev.type) {
    case 'delta':
      if (current) next[streaming] = { ...current, text: current.text + ev.text }
      else next.push({ kind: 'assistant', text: ev.text, streaming: true })
      return next
    case 'message':
      if (current) next[streaming] = { kind: 'assistant', text: ev.content }
      else if (ev.content) next.push({ kind: 'assistant', text: ev.content })
      return next
    case 'tool_start':
      closeStreaming()
      next.push({ kind: 'tool', id: ev.id, name: ev.name, args: ev.args })
      return next
    case 'tool_end': {
      const idx = next.findIndex((i) => i.kind === 'tool' && i.id === ev.id)
      if (idx >= 0)
        next[idx] = {
          ...(next[idx] as Extract<Item, { kind: 'tool' }>),
          success: ev.success,
          error: ev.error,
          chart: ev.chart,
          screenshot: ev.screenshot,
        }
      return next
    }
    case 'file_write': {
      // A write the user approved on a card is shown on that card instead of a separate line.
      const idx = ev.approval_id ? approvalIndex(ev.approval_id) : -1
      if (idx >= 0) {
        next[idx] = { ...(next[idx] as ApprovalItem), written: true }
        return next
      }
      closeStreaming()
      next.push({ kind: 'file_write', path: ev.path, diff: ev.diff })
      return next
    }
    case 'approval_request':
      if (approvalIndex(ev.id) >= 0) return items
      closeStreaming()
      next.push({ kind: 'approval', id: ev.id, turnId, path: ev.path, diff: ev.diff, status: 'pending' })
      return next
    case 'approval_result': {
      const idx = approvalIndex(ev.id)
      if (idx >= 0) next[idx] = { ...(next[idx] as ApprovalItem), status: ev.status }
      return next
    }
    case 'error':
      closeStreaming()
      next.push({ kind: 'error', message: ev.message })
      return next
    case 'follow_up':
      // Only in automation runs: the answers below replied to a second request, not to the instruction.
      closeStreaming()
      next.push({ kind: 'note', text: '結果の報告を依頼し直しました' })
      return next
    case 'user':
      // A queued message starts a new answer; one sent with 「すぐに送信」 joins the answer that is still streaming.
      if (ev.mode !== 'now') closeStreaming()
      next.push({ kind: 'user', text: ev.text })
      return next
    case 'done':
    case 'end':
      closeStreaming()
      return next
    default:
      return items
  }
}

export function fromHistory(messages: HistoryMessage[]): Item[] {
  return messages.map((m) =>
    m.role === 'tool' ? { kind: 'tool', name: m.name, args: m.args, success: true } : { kind: m.role, text: m.content },
  )
}

export const TOOL_LABELS: Record<string, string> = {
  view: 'ファイルを読む',
  grep: '知識を検索',
  rg: '知識を検索',
  glob: 'ファイルを探す',
  // create / edit are no longer exposed, but older conversations still show them in their history.
  create: 'ファイルを作成',
  edit: 'ファイルを編集',
  write_knowledge_file: 'ファイルに保存',
  edit_knowledge_file: 'ファイルを編集',
  web_fetch: 'Web ページを参照',
  skill: 'スキルを使用',
  browser_open: 'ブラウザで開く',
  browser_read: 'ページを読む',
  browser_click: 'ページをクリック',
  browser_fill: 'フォームに入力',
  browser_scroll: 'ページをスクロール',
  browser_screenshot: 'スクリーンショット',
}
