import { useCallback, useEffect, useLayoutEffect, useRef, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { api, ApiError, json } from '../api'
import { LazyChart } from '../components/LazyChart'
import { Disclaimer, Markdown } from '../components/Markdown'
import type { ChartData, Conversation, HistoryMessage, TurnEvent } from '../types'

type Item =
  | { kind: 'user'; text: string }
  | { kind: 'assistant'; text: string; streaming?: boolean }
  | { kind: 'tool'; id?: string; name: string; args: string; success?: boolean; error?: string; chart?: ChartData }
  | { kind: 'file_write'; path: string; diff: string }
  | { kind: 'error'; message: string }

function applyEvent(items: Item[], ev: TurnEvent): Item[] {
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
    case 'done':
    case 'end':
      closeStreaming()
      return next
    default:
      return items
  }
}

function fromHistory(messages: HistoryMessage[]): Item[] {
  return messages.map((m) =>
    m.role === 'tool' ? { kind: 'tool', name: m.name, args: m.args, success: true } : { kind: m.role, text: m.content },
  )
}

const TOOL_LABELS: Record<string, string> = {
  view: 'ファイルを読む',
  grep: '知識を検索',
  glob: 'ファイルを探す',
  create: 'ファイルを作成',
  edit: 'ファイルを編集',
  web_fetch: '公式サイトを参照',
  skill: 'スキルを使用',
}

export function ChatPage() {
  const navigate = useNavigate()
  const [conversations, setConversations] = useState<Conversation[]>([])
  const [currentId, setCurrentId] = useState<string | null>(null)
  const [items, setItems] = useState<Item[]>([])
  const [input, setInput] = useState('')
  const [models, setModels] = useState<{ id: string; name: string }[]>([])
  const [model, setModel] = useState('auto')
  const [turnId, setTurnId] = useState<string | null>(null)
  const [drawer, setDrawer] = useState(false)
  const [error, setError] = useState('')
  const sourceRef = useRef<EventSource | null>(null)
  const bottomRef = useRef<HTMLDivElement | null>(null)
  const messagesRef = useRef<HTMLDivElement | null>(null)
  const inputRef = useRef<HTMLTextAreaElement | null>(null)

  const loadConversations = useCallback(async () => {
    setConversations(await api<Conversation[]>('/api/conversations'))
  }, [])

  const attach = useCallback(
    (id: string) => {
      sourceRef.current?.close()
      setTurnId(id)
      // EventSource reconnects automatically and sends Last-Event-ID, so dropped connections resume.
      const source = new EventSource(`/api/turns/${id}/events`)
      sourceRef.current = source
      source.onmessage = (msg) => {
        const ev = JSON.parse(msg.data) as TurnEvent
        setItems((prev) => applyEvent(prev, ev))
        if (ev.type === 'end') {
          source.close()
          setTurnId(null)
          loadConversations()
        }
      }
      source.onerror = () => {
        if (source.readyState === EventSource.CLOSED) {
          setTurnId(null)
          loadConversations()
        }
      }
    },
    [loadConversations],
  )

  const openConversation = useCallback(
    async (id: string) => {
      sourceRef.current?.close()
      setTurnId(null)
      setCurrentId(id)
      setDrawer(false)
      setItems([])
      setError('')
      try {
        const data = await api<{ messages: HistoryMessage[]; busy: boolean; turn_id?: string }>(`/api/conversations/${id}/messages`)
        setItems(fromHistory(data.messages))
        if (data.busy && data.turn_id) attach(data.turn_id)
      } catch (e) {
        setError((e as Error).message)
      }
    },
    [attach],
  )

  useEffect(() => {
    loadConversations().catch((e) => setError(e.message))
    api<{ default: string; models: { id: string; name: string }[] }>('/api/models')
      .then((d) => {
        setModels(d.models)
        setModel(d.default)
      })
      .catch(() => undefined)
    return () => sourceRef.current?.close()
  }, [loadConversations])

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [items])

  // The composer grows with the text; CSS caps it at 5 lines and scrolls beyond that.
  const resizeInput = useCallback(() => {
    const el = inputRef.current
    if (!el) return
    const messages = messagesRef.current
    // A taller composer shrinks the thread, so keep the latest message in view when it was.
    const atBottom = messages ? messages.scrollHeight - messages.scrollTop - messages.clientHeight < 40 : false
    const style = getComputedStyle(el)
    const borders = parseFloat(style.borderTopWidth) + parseFloat(style.borderBottomWidth)
    el.style.height = 'auto' // back to the rows= size so the text can shrink it again
    el.style.height = `${el.scrollHeight + borders}px` // scrollHeight excludes borders
    if (messages && atBottom) messages.scrollTop = messages.scrollHeight
  }, [])

  useLayoutEffect(resizeInput, [input, resizeInput])

  useEffect(() => {
    // Re-wrapping on a width change also changes the number of lines.
    window.addEventListener('resize', resizeInput)
    return () => window.removeEventListener('resize', resizeInput)
  }, [resizeInput])

  const newConversation = async () => {
    const conv = await api<Conversation>('/api/conversations', { method: 'POST', body: json({ model }) })
    await loadConversations()
    await openConversation(conv.id)
  }

  const send = async (confirmSensitive = false) => {
    const prompt = input.trim()
    if (!prompt || turnId) return
    setError('')
    let id = currentId
    if (!id) {
      const conv = await api<Conversation>('/api/conversations', { method: 'POST', body: json({ model }) })
      id = conv.id
      setCurrentId(id)
    }
    try {
      const res = await api<{ turn_id: string }>(`/api/conversations/${id}/turns`, {
        method: 'POST',
        body: json({ prompt, model, confirm_sensitive: confirmSensitive }),
      })
      setItems((prev) => [...prev, { kind: 'user', text: prompt }])
      setInput('')
      attach(res.turn_id)
      loadConversations()
    } catch (e) {
      if (e instanceof ApiError && e.code === 'sensitive_data') {
        const ok = window.confirm(`${e.message}\nこの内容を Copilot に送信しますか？（ファイルには保存されません）`)
        if (ok) await send(true)
        return
      }
      setError((e as Error).message)
    }
  }

  const remove = async (id: string) => {
    if (!window.confirm('この会話を削除しますか？（Copilot 側の履歴も削除されます）')) return
    try {
      await api(`/api/conversations/${id}`, { method: 'DELETE' })
      if (currentId === id) {
        setCurrentId(null)
        setItems([])
      }
      await loadConversations()
    } catch (e) {
      setError((e as Error).message)
    }
  }

  const organize = async () => {
    const res = await api<{ turn_id: string; conversation_id: string }>('/api/memories/organize', { method: 'POST' })
    await loadConversations()
    setCurrentId(res.conversation_id)
    setItems([{ kind: 'user', text: 'メモリの整理を依頼しました。' }])
    attach(res.turn_id)
  }

  const abort = async () => {
    if (turnId) await api(`/api/turns/${turnId}/abort`, { method: 'POST' })
  }

  return (
    <div className="chat">
      <aside className={`conversations ${drawer ? 'open' : ''}`}>
        <button className="button primary block" onClick={newConversation}>
          ＋ 新しい会話
        </button>
        <button className="button block" onClick={organize} disabled={!!turnId}>
          メモリを整理
        </button>
        <ul>
          {conversations.map((c) => (
            <li key={c.id} className={c.id === currentId ? 'active' : ''}>
              <button className="link title" onClick={() => openConversation(c.id)}>
                {c.busy && '⏳ '}
                {c.title}
              </button>
              <button className="link danger" onClick={() => remove(c.id)} title="削除">
                ×
              </button>
            </li>
          ))}
        </ul>
      </aside>
      <section className="thread">
        <div className="thread-toolbar">
          <button className="button small mobile-only" onClick={() => setDrawer(!drawer)}>
            会話一覧
          </button>
          <Disclaimer />
        </div>
        <div className="messages" ref={messagesRef}>
          {items.length === 0 && (
            <div className="empty">
              <p>何でも相談してください。例:</p>
              <ul>
                <li>今年のふるさと納税の上限はいくら？</li>
                <li>NISA の枠はあとどれくらい残っている？</li>
                <li>60 歳までの資産推移をシミュレーションして</li>
              </ul>
            </div>
          )}
          {items.map((item, i) => (
            <MessageItem key={i} item={item} onSchedule={(text) => navigate(`/automations?new=1&prompt=${encodeURIComponent(text)}`)} />
          ))}
          <div ref={bottomRef} />
        </div>
        {error && <div className="banner error">{error}</div>}
        <form
          className="composer"
          onSubmit={(e) => {
            e.preventDefault()
            send()
          }}
        >
          <textarea
            ref={inputRef}
            value={input}
            onChange={(e) => setInput(e.target.value)}
            placeholder="メッセージを入力（Ctrl+Enter で送信）"
            rows={3}
            onKeyDown={(e) => {
              if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) {
                e.preventDefault()
                send()
              }
            }}
          />
          <div className="composer-actions">
            <select value={model} onChange={(e) => setModel(e.target.value)} aria-label="モデル">
              {(models.length ? models : [{ id: 'auto', name: 'auto' }]).map((m) => (
                <option key={m.id} value={m.id}>
                  {m.name}
                </option>
              ))}
            </select>
            {turnId ? (
              <button type="button" className="button" onClick={abort}>
                中断
              </button>
            ) : (
              <button type="submit" className="button primary" disabled={!input.trim()}>
                送信
              </button>
            )}
          </div>
        </form>
      </section>
    </div>
  )
}

function MessageItem({ item, onSchedule }: { item: Item; onSchedule: (text: string) => void }) {
  switch (item.kind) {
    case 'user':
      return (
        <div className="msg user">
          <div className="bubble">{item.text}</div>
          <button className="link small" onClick={() => onSchedule(item.text)}>
            この質問を定期実行
          </button>
        </div>
      )
    case 'assistant':
      return (
        <div className="msg assistant">
          <Markdown text={item.text} />
          {item.streaming && <span className="cursor">▍</span>}
        </div>
      )
    case 'tool':
      return (
        <div className="msg tool">
          <details>
            <summary>
              {item.success === false ? '⚠️' : item.success ? '✔' : '…'} {TOOL_LABELS[item.name] ?? item.name}
            </summary>
            <pre>{item.args}</pre>
            {item.error && <p className="error-text">{item.error}</p>}
          </details>
          {item.chart && <LazyChart chart={item.chart} />}
        </div>
      )
    case 'file_write':
      return (
        <div className="msg file-write">
          <details>
            <summary>💾 {item.path} に書き込みました（内容を確認）</summary>
            <pre>{item.diff}</pre>
          </details>
        </div>
      )
    case 'error':
      return <div className="banner error">{item.message}</div>
  }
}
