import type { ChartData, HistoryMessage, TurnEvent } from './types'

export type Item =
  | { kind: 'user'; text: string }
  | { kind: 'assistant'; text: string; streaming?: boolean }
  | { kind: 'tool'; id?: string; name: string; args: string; success?: boolean; error?: string; chart?: ChartData }
  | { kind: 'file_write'; path: string; diff: string }
  | { kind: 'error'; message: string }
  | { kind: 'note'; text: string }

export function applyEvent(items: Item[], ev: TurnEvent): Item[] {
  const next = [...items]
  const last = next[next.length - 1]
  const closeStreaming = () => {
    if (last?.kind === 'assistant' && last.streaming) next[next.length - 1] = { ...last, streaming: false }
  }
  switch (ev.type) {
    case 'delta':
      if (last?.kind === 'assistant' && last.streaming) next[next.length - 1] = { ...last, text: last.text + ev.text }
      else next.push({ kind: 'assistant', text: ev.text, streaming: true })
      return next
    case 'message':
      if (last?.kind === 'assistant' && last.streaming) next[next.length - 1] = { kind: 'assistant', text: ev.content }
      else if (ev.content) next.push({ kind: 'assistant', text: ev.content })
      return next
    case 'tool_start':
      closeStreaming()
      next.push({ kind: 'tool', id: ev.id, name: ev.name, args: ev.args })
      return next
    case 'tool_end': {
      const idx = next.findIndex((i) => i.kind === 'tool' && i.id === ev.id)
      if (idx >= 0) next[idx] = { ...(next[idx] as Extract<Item, { kind: 'tool' }>), success: ev.success, error: ev.error, chart: ev.chart }
      return next
    }
    case 'file_write':
      closeStreaming()
      next.push({ kind: 'file_write', path: ev.path, diff: ev.diff })
      return next
    case 'error':
      closeStreaming()
      next.push({ kind: 'error', message: ev.message })
      return next
    case 'follow_up':
      // Only in automation runs: the answers below replied to a second request, not to the instruction.
      closeStreaming()
      next.push({ kind: 'note', text: '結果の報告を依頼し直しました' })
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
  glob: 'ファイルを探す',
  create: 'ファイルを作成',
  edit: 'ファイルを編集',
  web_fetch: '公式サイトを参照',
  skill: 'スキルを使用',
}
