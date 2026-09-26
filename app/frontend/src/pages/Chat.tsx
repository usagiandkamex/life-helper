import { useCallback, useEffect, useLayoutEffect, useRef, useState } from 'react'
import { useNavigate, useSearchParams } from 'react-router-dom'
import { api, ApiError, formatDate, json } from '../api'
import { quoteDraft } from '../automationRuns'
import { applyEvent, fromHistory, type Item } from '../chatItems'
import { AutomationThreadView } from '../components/AutomationThread'
import { Disclaimer } from '../components/Markdown'
import { MessageItem } from '../components/MessageItem'
import type { AutomationThread, AutomationThreadDetail, Conversation, HistoryMessage, RunRecord, TurnEvent } from '../types'

// New automation runs are saved by a separate job, so the list is polled while the chat is on screen.
const THREAD_REFRESH_MS = 60_000

type Entry =
  | { kind: 'chat'; at: number; conversation: Conversation }
  | { kind: 'automation'; at: number; thread: AutomationThread }

export function ChatPage({ onUnreadChange }: { onUnreadChange: (unread: number) => void }) {
  const navigate = useNavigate()
  const [params, setParams] = useSearchParams()
  const [conversations, setConversations] = useState<Conversation[]>([])
  const [threads, setThreads] = useState<AutomationThread[]>([])
  const [currentId, setCurrentId] = useState<string | null>(null)
  // An automation conversation is read-only and shown instead of a chat conversation, never together with one.
  const [threadId, setThreadId] = useState<string | null>(null)
  const [thread, setThread] = useState<AutomationThreadDetail | null>(null)
  const [threadBusy, setThreadBusy] = useState(false)
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
  const drawerToggleRef = useRef<HTMLButtonElement | null>(null)
  const drawerCloseRef = useRef<HTMLButtonElement | null>(null)
  const drawerRef = useRef<HTMLElement | null>(null)
  // Async handlers must see the conversation that is active now, not the one captured when they started.
  const currentIdRef = useRef<string | null>(null)
  const threadIdRef = useRef<string | null>(null)
  // Bumped on every change of what is shown; a response started under an older value is dropped.
  const generationRef = useRef(0)
  const threadsRequestRef = useRef(0)
  const readQueueRef = useRef<Promise<void>>(Promise.resolve())
  const handledLinkRef = useRef<string | null>(null)
  // Where to scroll once a loaded automation conversation is rendered ('bottom' or an element id).
  const scrollTargetRef = useRef<string | null>(null)

  const selectConversation = useCallback((id: string | null) => {
    generationRef.current += 1
    currentIdRef.current = id
    setCurrentId(id)
    threadIdRef.current = null
    setThreadId(null)
    setThread(null)
    setThreadBusy(false)
  }, [])

  const loadConversations = useCallback(async () => {
    setConversations(await api<Conversation[]>('/api/conversations'))
  }, [])

  const loadThreads = useCallback(async () => {
    const request = ++threadsRequestRef.current
    const data = await api<AutomationThread[]>('/api/automations/chat')
    // Refreshes overlap (timer, focus, after read/hide): an older answer must not bring back a hidden entry.
    if (request === threadsRequestRef.current) setThreads(data)
  }, [])

  const attach = useCallback(
    (id: string) => {
      sourceRef.current?.close()
      setTurnId(id)
      // EventSource reconnects automatically and sends Last-Event-ID, so dropped connections resume.
      const source = new EventSource(`/api/turns/${id}/events`)
      sourceRef.current = source
      source.onmessage = (msg) => {
        if (sourceRef.current !== source) return // another conversation was opened meanwhile
        const ev = JSON.parse(msg.data) as TurnEvent
        setItems((prev) => applyEvent(prev, ev, id))
        if (ev.type === 'end') {
          source.close()
          setTurnId(null)
          loadConversations()
        }
      }
      source.onerror = () => {
        if (sourceRef.current === source && source.readyState === EventSource.CLOSED) {
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
      sourceRef.current = null
      setTurnId(null)
      // The composer belongs to the conversation it was typed in, so it must not follow us to another one.
      if (id !== currentIdRef.current) setInput('')
      selectConversation(id)
      const generation = generationRef.current
      setDrawer(false)
      setItems([])
      setError('')
      try {
        const data = await api<{ messages: HistoryMessage[]; busy: boolean; turn_id?: string }>(`/api/conversations/${id}/messages`)
        if (generationRef.current !== generation) return // a slower answer must not replace what was opened since
        setItems(fromHistory(data.messages))
        if (data.busy && data.turn_id) attach(data.turn_id)
      } catch (e) {
        if (generationRef.current === generation) setError((e as Error).message)
      }
    },
    [attach, selectConversation],
  )

  const markRead = useCallback(
    async (id: string, runs: RunRecord[]) => {
      const unread = runs.filter((r) => !r.read).map((r) => r.id)
      if (unread.length === 0) return
      // Serialize mutations: unread counts follow server completion order, not request start order.
      const request = readQueueRef.current.then(async () => {
        // Only the runs on screen: one that arrived after they were loaded stays unread.
        const res = await api<{ unread: number }>(`/api/automations/chat/${id}/read`, {
          method: 'POST',
          body: json({ run_ids: unread }),
        })
        onUnreadChange(res.unread)
      })
      readQueueRef.current = request.catch(() => undefined)
      await request
      await loadThreads()
    },
    [loadThreads, onUnreadChange],
  )

  const openThread = useCallback(
    async (id: string, anchorRunId?: string | null) => {
      sourceRef.current?.close()
      sourceRef.current = null
      setTurnId(null)
      selectConversation(null)
      const generation = generationRef.current
      threadIdRef.current = id
      setThreadId(id)
      setInput('')
      setDrawer(false)
      setItems([])
      setError('')
      try {
        const query = anchorRunId ? `?anchor=${encodeURIComponent(anchorRunId)}` : ''
        const detail = await api<AutomationThreadDetail>(`/api/automations/chat/${encodeURIComponent(id)}${query}`)
        if (generationRef.current !== generation) return
        scrollTargetRef.current = anchorRunId && detail.runs.some((r) => r.id === anchorRunId) ? `run-${anchorRunId}` : 'bottom'
        setThread(detail)
        markRead(id, detail.runs).catch(() => undefined)
      } catch (e) {
        if (generationRef.current === generation) setError((e as Error).message)
      }
    },
    [markRead, selectConversation],
  )

  const loadOlderRuns = async () => {
    const id = threadIdRef.current
    const first = thread?.runs[0]
    if (!id || !first) return
    const generation = generationRef.current
    setThreadBusy(true)
    try {
      const older = await api<AutomationThreadDetail>(`/api/automations/chat/${id}?before=${first.id}`)
      if (generationRef.current !== generation) return
      scrollTargetRef.current = `run-${first.id}` // keep reading where the user was
      setThread((cur) => cur && { ...cur, runs: [...older.runs, ...cur.runs], has_more: older.has_more })
      markRead(id, older.runs).catch(() => undefined)
    } catch (e) {
      if (generationRef.current === generation) setError((e as Error).message)
    } finally {
      if (generationRef.current === generation) setThreadBusy(false)
    }
  }

  // Only removes the entry from the list; an open conversation stays on screen.
  const hideThread = async (t: AutomationThread) => {
    if (!window.confirm(`「${t.title}」をチャットの一覧から消しますか？（オートメーション画面の実行履歴は残ります）`)) return
    try {
      // The run the list showed: a run that arrived since keeps the conversation in the list.
      await api(`/api/automations/chat/${t.id}/hide`, { method: 'POST', body: json({ run_id: t.latest_run_id }) })
      // On a phone the list covers the open conversation, which stays on screen.
      if (threadIdRef.current === t.id) setDrawer(false)
      await loadThreads()
    } catch (e) {
      setError((e as Error).message)
    }
  }

  // Starts a normal conversation: the result is quoted into the composer and sent with the user's question.
  const askAbout = (run: RunRecord) => {
    selectConversation(null)
    setItems([{ kind: 'note', text: `「${run.name}」の結果を入力欄に引用しました。質問を書き足して送信すると、新しい会話が始まります。` }])
    setError('')
    setInput(quoteDraft(run))
    window.requestAnimationFrame(() => {
      const el = inputRef.current
      if (!el) return
      el.focus()
      el.setSelectionRange(el.value.length, el.value.length)
      el.scrollTop = el.scrollHeight // the question goes below the quote
    })
  }

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
    const refresh = () => {
      if (document.visibilityState === 'visible') loadThreads().catch(() => undefined)
    }
    refresh()
    const timer = window.setInterval(refresh, THREAD_REFRESH_MS)
    window.addEventListener('focus', refresh)
    return () => {
      window.clearInterval(timer)
      window.removeEventListener('focus', refresh)
    }
  }, [loadThreads])

  useEffect(() => {
    // Deep link from the automations page: /chat?thread=<id>&run=<run id>
    const id = params.get('thread')
    if (!id) {
      handledLinkRef.current = null
      return
    }
    // StrictMode runs effects twice; open the linked conversation once.
    const key = params.toString()
    if (handledLinkRef.current === key) return
    handledLinkRef.current = key
    const run = params.get('run')
    setParams({}, { replace: true })
    openThread(id, run)
  }, [params, setParams, openThread])

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [items])

  useEffect(() => {
    const target = scrollTargetRef.current
    if (!thread || !target) return
    scrollTargetRef.current = null
    if (target === 'bottom') bottomRef.current?.scrollIntoView()
    else document.getElementById(target)?.scrollIntoView({ block: 'start' })
  }, [thread])

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

  // Every way the drawer closes (close button, picking an entry, a new conversation) hides the focused control,
  // so focus goes back to the toggle. Closing while focus is elsewhere (e.g. a deep link) leaves it where it is.
  useLayoutEffect(() => {
    if (!drawer && drawerRef.current?.contains(document.activeElement)) drawerToggleRef.current?.focus()
  }, [drawer])

  useEffect(() => {
    // Re-wrapping on a width change also changes the number of lines.
    window.addEventListener('resize', resizeInput)
    return () => window.removeEventListener('resize', resizeInput)
  }, [resizeInput])

  const newConversation = async () => {
    const generation = generationRef.current
    const conv = await api<Conversation>('/api/conversations', { method: 'POST', body: json({ model }) })
    await loadConversations()
    if (generationRef.current !== generation) return // something else was opened while it was being created
    await openConversation(conv.id)
  }

  const send = async (confirmSensitive = false) => {
    const prompt = input.trim()
    if (!prompt || turnId) return
    setError('')
    let generation = generationRef.current
    // The ref, not the render's value: a retry after the sensitive-data prompt must reuse the conversation just made.
    let id = currentIdRef.current
    if (!id) {
      const conv = await api<Conversation>('/api/conversations', { method: 'POST', body: json({ model }) })
      if (generationRef.current !== generation) {
        loadConversations()
        return
      }
      id = conv.id
      selectConversation(id)
      generation = generationRef.current
    }
    try {
      const res = await api<{ turn_id: string }>(`/api/conversations/${id}/turns`, {
        method: 'POST',
        body: json({ prompt, model, confirm_sensitive: confirmSensitive }),
      })
      loadConversations()
      // Opened something else meanwhile: the turn keeps running and is followed when its conversation is opened.
      if (generationRef.current !== generation) return
      setItems((prev) => [...prev, { kind: 'user', text: prompt }])
      setInput('')
      attach(res.turn_id)
    } catch (e) {
      if (generationRef.current !== generation) return
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
      if (currentIdRef.current === id) {
        selectConversation(null)
        setItems([])
        setInput('')
      }
      await loadConversations()
    } catch (e) {
      setError((e as Error).message)
    }
  }

  const organize = async () => {
    const generation = generationRef.current
    const res = await api<{ turn_id: string; conversation_id: string }>('/api/memories/organize', { method: 'POST' })
    await loadConversations()
    if (generationRef.current !== generation) return // it keeps running; its conversation shows it when opened
    selectConversation(res.conversation_id)
    setInput('')
    setItems([{ kind: 'user', text: 'メモリの整理を依頼しました。' }])
    attach(res.turn_id)
  }

  const abort = async () => {
    if (turnId) await api(`/api/turns/${turnId}/abort`, { method: 'POST' })
  }

  const entries: Entry[] = [
    ...conversations.map((c) => ({ kind: 'chat' as const, at: Date.parse(c.updated_at) || 0, conversation: c })),
    ...threads.map((t) => ({ kind: 'automation' as const, at: Date.parse(t.updated_at) || 0, thread: t })),
  ].sort((a, b) => b.at - a.at)

  return (
    <div className="chat">
      <aside id="conversation-list" ref={drawerRef} className={`conversations ${drawer ? 'open' : ''}`}>
        {/* On a phone the list covers its toggle button, so it can be closed from inside; focus goes back to the toggle. */}
        <div className="row list-head drawer-head">
          <h2 className="grow">会話一覧</h2>
          <button ref={drawerCloseRef} className="button small" onClick={() => setDrawer(false)}>
            閉じる
          </button>
        </div>
        <button className="button primary block" onClick={newConversation}>
          ＋ 新しい会話
        </button>
        <button className="button block" onClick={organize} disabled={!!turnId}>
          メモリを整理
        </button>
        <ul>
          {entries.map((e) =>
            e.kind === 'chat' ? (
              <li key={`chat-${e.conversation.id}`} className={e.conversation.id === currentId ? 'active' : ''}>
                <button className="link title" onClick={() => openConversation(e.conversation.id)}>
                  {e.conversation.busy && '⏳ '}
                  {e.conversation.title}
                </button>
                <button className="link danger" onClick={() => remove(e.conversation.id)} title="削除">
                  ×
                </button>
              </li>
            ) : (
              <li
                key={`automation-${e.thread.id}`}
                className={[e.thread.id === threadId && 'active', e.thread.unread && 'unread'].filter(Boolean).join(' ')}
              >
                <button
                  className="link title"
                  onClick={() => openThread(e.thread.id)}
                  title={`オートメーション「${e.thread.title}」の実行結果`}
                >
                  🤖 {e.thread.title}
                  {e.thread.mode === 'new' && <small className="thread-date">{formatDate(e.thread.latest_started_at)}</small>}
                </button>
                {e.thread.unread && (
                  <span className="unread-dot" role="img" aria-label="未読" title="未読">
                    ●
                  </span>
                )}
                <button
                  className="link danger"
                  onClick={() => hideThread(e.thread)}
                  title="一覧から消す（実行履歴は残ります）"
                  aria-label={`「${e.thread.title}」を一覧から消す`}
                >
                  ×
                </button>
              </li>
            ),
          )}
        </ul>
      </aside>
      <section className="thread">
        <div className="thread-toolbar">
          <button
            ref={drawerToggleRef}
            className="button small mobile-only"
            onClick={() => {
              setDrawer(!drawer)
              // The opened list covers this button; move focus into it once it is shown.
              if (!drawer) window.requestAnimationFrame(() => drawerCloseRef.current?.focus())
            }}
            aria-expanded={drawer}
            aria-controls="conversation-list"
          >
            会話一覧
          </button>
          <Disclaimer short />
        </div>
        <div className="messages" ref={messagesRef}>
          {threadId ? (
            thread ? (
              <AutomationThreadView
                detail={thread}
                busy={threadBusy}
                onLoadOlder={loadOlderRuns}
                onShowLatest={() => openThread(thread.thread.id)}
                onAsk={askAbout}
              />
            ) : (
              !error && <p className="hint">読み込み中…</p>
            )
          ) : (
            <>
              {items.length === 0 && (
                <div className="empty">
                  <p>何でも相談してください。例:</p>
                  <ul>
                    <li>今年のふるさと納税の上限はいくら？</li>
                    <li>今の資産の内訳を教えて</li>
                    <li>60 歳までの資産推移をシミュレーションして</li>
                  </ul>
                </div>
              )}
              {items.map((item, i) => (
                <MessageItem key={i} item={item} onSchedule={(text) => navigate(`/automations?new=1&prompt=${encodeURIComponent(text)}`)} />
              ))}
            </>
          )}
          <div ref={bottomRef} />
        </div>
        {error && <div className="banner error">{error}</div>}
        {threadId ? (
          <div className="composer readonly">
            <p className="hint">
              オートメーションの実行結果です（読み取り専用）。続けて聞くときは、各実行の「この結果について質問する」から新しい会話を始めてください。
            </p>
          </div>
        ) : (
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
        )}
      </section>
    </div>
  )
}
