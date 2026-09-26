import { useCallback, useEffect, useLayoutEffect, useRef, useState } from 'react'
import { useNavigate, useSearchParams } from 'react-router-dom'
import { api, ApiError, formatDate, json } from '../api'
import { quoteDraft } from '../automationRuns'
import { applyEvent, fromHistory, type Item, type ShownAttachment } from '../chatItems'
import { AttachmentList } from '../components/AttachmentList'
import { AutomationThreadView } from '../components/AutomationThread'
import { Disclaimer } from '../components/Markdown'
import { MessageItem } from '../components/MessageItem'
import type {
  AttachmentInfo,
  AutomationThread,
  AutomationThreadDetail,
  Conversation,
  HistoryMessage,
  RunRecord,
  TurnEvent,
} from '../types'

// New automation runs are saved by a separate job, so the list is polled while the chat is on screen.
const THREAD_REFRESH_MS = 60_000

// The server checks the same limits and the file contents; these only give an early message.
const MAX_ATTACHMENTS = 5
const MAX_ATTACHMENT_BYTES = 10 * 1024 * 1024
const IMAGE_TYPES = ['image/png', 'image/jpeg', 'image/gif', 'image/webp']
const FILE_SUFFIXES = ['.txt', '.md', '.csv', '.tsv', '.json', '.pdf']
const ATTACH_ACCEPT = [...IMAGE_TYPES, ...FILE_SUFFIXES].join(',')

type PendingAttachment = { name: string; image: boolean; size: number; data: string; url?: string }

function readAttachment(file: File): Promise<PendingAttachment> {
  const image = IMAGE_TYPES.includes(file.type)
  // A pasted screenshot may come without a name (anything else without one is refused before this).
  const name = file.name || `image.${file.type.slice('image/'.length)}`
  return new Promise((resolve, reject) => {
    const reader = new FileReader()
    reader.onload = () => {
      const url = String(reader.result)
      resolve({ name, image, size: file.size, data: url.slice(url.indexOf(',') + 1), url: image ? url : undefined })
    }
    reader.onerror = () => reject(new Error(`${name} を読み込めませんでした`))
    reader.readAsDataURL(file)
  })
}

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
  const [attachments, setAttachments] = useState<PendingAttachment[]>([])
  const [sending, setSending] = useState(false)
  const [models, setModels] = useState<{ id: string; name: string }[]>([])
  const [model, setModel] = useState('auto')
  const [turnId, setTurnId] = useState<string | null>(null)
  const [drawer, setDrawer] = useState(false)
  const [error, setError] = useState('')
  const sourceRef = useRef<EventSource | null>(null)
  const bottomRef = useRef<HTMLDivElement | null>(null)
  const messagesRef = useRef<HTMLDivElement | null>(null)
  const inputRef = useRef<HTMLTextAreaElement | null>(null)
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
      if (id !== currentIdRef.current) {
        setInput('')
        setAttachments([])
      }
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
      setAttachments([])
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
    setAttachments([])
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

  // Checks what can be checked here (type, size, count) before reading the files for sending.
  const addAttachments = async (files: File[]) => {
    const generation = generationRef.current
    const problems: string[] = []
    let count = attachments.length
    let bytes = attachments.reduce((sum, a) => sum + a.size, 0)
    const accepted = files.filter((f) => {
      const name = f.name || '貼り付けたデータ'
      const allowed = IMAGE_TYPES.includes(f.type) || FILE_SUFFIXES.some((s) => f.name.toLowerCase().endsWith(s))
      if (!allowed) problems.push(`${name} は添付できない形式です（画像・テキスト・CSV・JSON・PDF に対応）`)
      else if (f.size === 0) problems.push(`${name} は空のファイルです`)
      else if (count >= MAX_ATTACHMENTS) problems.push(`添付できるのは ${MAX_ATTACHMENTS} 件までです`)
      else if (bytes + f.size > MAX_ATTACHMENT_BYTES) problems.push(`${name} は添付できません（合計 10 MB まで）`)
      else {
        count += 1
        bytes += f.size
        return true
      }
      return false
    })
    if (problems.length) setError([...new Set(problems)].join(' / '))
    try {
      const read = await Promise.all(accepted.map(readAttachment))
      // Switched to another conversation while reading: the files belong to the one they were added in.
      if (generationRef.current === generation) setAttachments((cur) => [...cur, ...read].slice(0, MAX_ATTACHMENTS))
    } catch (e) {
      if (generationRef.current === generation) setError((e as Error).message)
    }
  }

  const send = async (confirmSensitive = false) => {
    const prompt = input.trim()
    const files = attachments
    if ((!prompt && files.length === 0) || turnId || sending) return
    setError('')
    setSending(true)
    try {
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
        const res = await api<{ turn_id: string; message: { content: string; attachments: AttachmentInfo[] } }>(
          `/api/conversations/${id}/turns`,
          {
            method: 'POST',
            body: json({
              prompt,
              model,
              confirm_sensitive: confirmSensitive,
              attachments: files.map((a) => ({ name: a.name, data: a.data })),
            }),
          },
        )
        loadConversations()
        // Opened something else meanwhile: the turn keeps running and is followed when its conversation is opened.
        if (generationRef.current !== generation) return
        // The server lists the images first, in the order they were attached; they keep their thumbnails here.
        const thumbnails = files.filter((a) => a.image).map((a) => a.url)
        const shown: ShownAttachment[] = res.message.attachments.map((a) => (a.kind === 'image' ? { ...a, url: thumbnails.shift() } : a))
        setItems((prev) => [...prev, { kind: 'user', text: res.message.content, attachments: shown }])
        setInput('')
        setAttachments((cur) => cur.filter((a) => !files.includes(a)))
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
    } finally {
      setSending(false)
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
        setAttachments([])
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
    setAttachments([])
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
      <aside className={`conversations ${drawer ? 'open' : ''}`}>
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
          <button className="button small mobile-only" onClick={() => setDrawer(!drawer)}>
            会話一覧
          </button>
          <Disclaimer />
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
            {attachments.length > 0 && (
              <div>
                <AttachmentList
                  items={attachments.map((a) => ({ name: a.name, kind: a.image ? 'image' : 'file', url: a.url }))}
                  onRemove={(index) => setAttachments((cur) => cur.filter((_, i) => i !== index))}
                />
                <p className="hint">
                  添付はこの会話の中だけで使います（知識ベースには保存されません）。画像の中身は機微情報チェックの対象外です。
                </p>
              </div>
            )}
            <textarea
              ref={inputRef}
              value={input}
              onChange={(e) => setInput(e.target.value)}
              placeholder="メッセージを入力（Ctrl+Enter で送信。画像は貼り付けでも添付できます）"
              rows={3}
              onKeyDown={(e) => {
                if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) {
                  e.preventDefault()
                  send()
                }
              }}
              onPaste={(e) => {
                const files = Array.from(e.clipboardData.files)
                // A copy from Excel or Word also carries a picture of it: the text wins, so a table pastes as text.
                if (files.length === 0 || e.clipboardData.getData('text/plain').trim()) return
                e.preventDefault()
                addAttachments(files)
              }}
            />
            <div className="composer-actions">
              <div className="composer-tools">
                <label className="button small" title="画像・テキスト・CSV・JSON・PDF を添付（5 件・合計 10 MB まで）">
                  📎 添付
                  <input
                    type="file"
                    accept={ATTACH_ACCEPT}
                    multiple
                    hidden
                    disabled={sending}
                    onChange={(e) => {
                      const files = Array.from(e.currentTarget.files ?? [])
                      // Cleared so that choosing the same file again adds it again.
                      e.currentTarget.value = ''
                      addAttachments(files)
                    }}
                  />
                </label>
                <select value={model} onChange={(e) => setModel(e.target.value)} aria-label="モデル">
                  {(models.length ? models : [{ id: 'auto', name: 'auto' }]).map((m) => (
                    <option key={m.id} value={m.id}>
                      {m.name}
                    </option>
                  ))}
                </select>
              </div>
              {turnId ? (
                <button type="button" className="button" onClick={abort}>
                  中断
                </button>
              ) : (
                <button type="submit" className="button primary" disabled={(!input.trim() && attachments.length === 0) || sending}>
                  {sending ? '送信中…' : '送信'}
                </button>
              )}
            </div>
          </form>
        )}
      </section>
    </div>
  )
}
