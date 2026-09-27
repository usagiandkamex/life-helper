import { useCallback, useEffect, useLayoutEffect, useRef, useState } from 'react'
import { useNavigate, useSearchParams } from 'react-router-dom'
import { api, ApiError, formatDate, json } from '../api'
import { quoteDraft } from '../automationRuns'
import {
  applyEvent,
  applyWaitingEvent,
  fromHistory,
  shownAttachments,
  type Item,
  type WaitingMessage,
} from '../chatItems'
import { AttachmentList } from '../components/AttachmentList'
import { AutomationThreadView } from '../components/AutomationThread'
import { Disclaimer } from '../components/Markdown'
import { MessageItem } from '../components/MessageItem'
import type {
  AutomationThread,
  AutomationThreadDetail,
  Conversation,
  FollowUpMode,
  HistoryMessage,
  RunRecord,
  SentAttachment,
  TurnEvent,
  UnsentMessage,
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

// Files sent with a message while the chat answered, kept until its turn ends: they give the message its thumbnails
// when Copilot takes it, and go back to the composer if Copilot never does.
type SentFiles = { conversationId: string; turnId: string; files: PendingAttachment[] }

// Files put back into the composer are not checked when they come back, so sending checks the limits once more.
function attachmentProblem(files: PendingAttachment[]): string {
  if (files.length > MAX_ATTACHMENTS)
    return `添付できるのは ${MAX_ATTACHMENTS} 件までです。${files.length - MAX_ATTACHMENTS} 件外してから送信してください`
  if (files.reduce((sum, a) => sum + a.size, 0) > MAX_ATTACHMENT_BYTES)
    return '添付ファイルは合計 10 MB までです。いくつか外してから送信してください'
  return ''
}

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
  // Files still being read: counted against the limits, and sending waits for them.
  const [reading, setReading] = useState(0)
  const readingRef = useRef({ count: 0, bytes: 0 })
  const [sending, setSending] = useState(false)
  const [models, setModels] = useState<{ id: string; name: string }[]>([])
  const [model, setModel] = useState('auto')
  const [turnId, setTurnId] = useState<string | null>(null)
  // Messages sent while the chat answers that Copilot has not taken yet.
  const [waiting, setWaiting] = useState<WaitingMessage[]>([])
  // The kind of message being sent while the chat answers, until the server has it.
  const [followUpSending, setFollowUpSending] = useState<FollowUpMode | null>(null)
  // From 中断 until the turn has ended: nothing more can be sent to it.
  const [stopping, setStopping] = useState(false)
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
  // Unsent messages already put back into the composer: both the turn's end and the conversation report them.
  const restoredRef = useRef(new Set<string>())
  // By message id. Not across reloads: a message whose files are gone comes back without them.
  const sentFilesRef = useRef(new Map<string, SentFiles>())
  // A message sent just as the answer ended starts the next turn, which is followed once the current one has ended.
  const nextTurnRef = useRef<(() => void) | null>(null)

  const selectConversation = useCallback((id: string | null) => {
    generationRef.current += 1
    currentIdRef.current = id
    setCurrentId(id)
    threadIdRef.current = null
    setThreadId(null)
    setThread(null)
    setThreadBusy(false)
    setWaiting([])
    setStopping(false)
    nextTurnRef.current = null
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

  // Messages the turn could not send go back to the composer, so they can be edited and sent again; their files too,
  // when this page still has them. finished: the sent files whose turn has ended, which are no longer needed.
  const restoreUnsent = useCallback((unsent: UnsentMessage[] | undefined, finished: (sent: SentFiles) => boolean) => {
    const texts: string[] = []
    const files: PendingAttachment[] = []
    let lost = 0
    for (const m of unsent ?? []) {
      if (restoredRef.current.has(m.id)) continue
      restoredRef.current.add(m.id)
      texts.push(m.text)
      const sent = sentFilesRef.current.get(m.id)
      if (sent) files.push(...sent.files)
      else lost += m.attachments?.length ?? 0
    }
    for (const [id, sent] of sentFilesRef.current) if (finished(sent)) sentFilesRef.current.delete(id)
    if (texts.length === 0) return
    setInput((cur) => [...texts, cur.trim()].filter(Boolean).join('\n\n'))
    if (files.length) setAttachments((cur) => [...files, ...cur])
    const note = `送信できなかったメッセージ（${texts.length} 件）を入力欄に戻しました。`
    const lostNote = lost ? `添付 ${lost} 件は戻せなかったため、もう一度添付してください。` : ''
    setItems((prev) => [...prev, { kind: 'note', text: note + lostNote }])
  }, [])

  const attach = useCallback(
    (id: string) => {
      sourceRef.current?.close()
      setTurnId(id)
      setWaiting([]) // the replay below lists them again
      setStopping(false)
      // EventSource reconnects automatically and sends Last-Event-ID, so dropped connections resume.
      const source = new EventSource(`/api/turns/${id}/events`)
      sourceRef.current = source
      const ended = () => {
        setTurnId(null)
        setWaiting([])
        setStopping(false)
        loadConversations()
        const next = nextTurnRef.current
        nextTurnRef.current = null
        next?.()
      }
      source.onmessage = (msg) => {
        if (sourceRef.current !== source) return // another conversation was opened meanwhile
        const ev = JSON.parse(msg.data) as TurnEvent
        // A message sent with files from here shows their thumbnails (from the history, only their names).
        const thumbnails = ev.type === 'user' ? sentFilesRef.current.get(ev.id)?.files.map((f) => f.url) : undefined
        setItems((prev) => applyEvent(prev, ev, id, thumbnails))
        setWaiting((prev) => applyWaitingEvent(prev, ev))
        if (ev.type === 'end') {
          source.close()
          restoreUnsent(ev.unsent, (sent) => sent.turnId === id)
          ended()
        }
      }
      source.onerror = () => {
        if (sourceRef.current === source && source.readyState === EventSource.CLOSED) ended()
      }
    },
    [loadConversations, restoreUnsent],
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
        const data = await api<{ messages: HistoryMessage[]; busy: boolean; turn_id?: string; unsent?: UnsentMessage[] }>(
          `/api/conversations/${id}/messages`,
        )
        if (generationRef.current !== generation) return // a slower answer must not replace what was opened since
        setItems(fromHistory(data.messages))
        if (data.busy && data.turn_id) attach(data.turn_id)
        // The turn ended while this conversation was not followed.
        else restoreUnsent(data.unsent, (sent) => sent.conversationId === id)
      } catch (e) {
        if (generationRef.current === generation) setError((e as Error).message)
      }
    },
    [attach, restoreUnsent, selectConversation],
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

  // Checks what can be checked here (type, size, count) before reading the files for sending.
  const addAttachments = async (files: File[]) => {
    const generation = generationRef.current
    const problems: string[] = []
    const pending = readingRef.current
    let count = attachments.length + pending.count
    let bytes = attachments.reduce((sum, a) => sum + a.size, 0) + pending.bytes
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
    if (accepted.length === 0) return
    const reserved = { count: accepted.length, bytes: accepted.reduce((sum, f) => sum + f.size, 0) }
    pending.count += reserved.count
    pending.bytes += reserved.bytes
    setReading((n) => n + 1)
    try {
      const read = await Promise.all(accepted.map(readAttachment))
      // Switched to another conversation while reading: the files belong to the one they were added in.
      if (generationRef.current === generation) setAttachments((cur) => [...cur, ...read])
    } catch (e) {
      if (generationRef.current === generation) setError((e as Error).message)
    } finally {
      pending.count -= reserved.count
      pending.bytes -= reserved.bytes
      setReading((n) => n - 1)
    }
  }

  const send = async (confirmSensitive = false) => {
    const draft = input
    const prompt = input.trim()
    const files = attachments
    if ((!prompt && files.length === 0) || turnId || sending || reading || followUpSending) return
    const problem = attachmentProblem(files)
    if (problem) {
      setError(problem)
      return
    }
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
        const res = await api<{
          turn_id: string
          message: { content: string; attachments: SentAttachment[] }
        }>(
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
        const shown = shownAttachments(res.message.attachments, files.map((a) => a.url))
        setItems((prev) => [...prev, { kind: 'user', text: res.message.content, attachments: shown }])
        setInput((cur) => (cur === draft ? '' : cur))
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

  // While the chat answers: 'now' joins the answer in progress, 'later' waits until it is finished.
  const followUpBlocked = stopping || reading > 0 || followUpSending !== null
  const sendFollowUp = async (
    mode: FollowUpMode,
    confirmSensitive = false,
    prompt = input.trim(),
    files = attachments,
  ) => {
    const id = currentIdRef.current
    if ((!prompt && files.length === 0) || !id || !turnId || followUpBlocked) return
    const problem = attachmentProblem(files)
    if (problem) {
      setError(problem)
      return
    }
    setError('')
    // The text is cleared at once so that the next message can be typed meanwhile, and comes back if it cannot be
    // sent; the files stay in the composer until the server has them.
    setInput((cur) => (cur.trim() === prompt ? '' : cur))
    const restore = () => setInput((cur) => [prompt, cur.trim()].filter(Boolean).join('\n\n'))
    const generation = generationRef.current
    setFollowUpSending(mode)
    try {
      // message: when the answer ended meanwhile and the message started a new turn, as for send().
      const res = await api<{
        turn_id: string
        message_id?: string
        message?: { content: string; attachments: SentAttachment[] }
      }>(`/api/conversations/${id}/turns`, {
        method: 'POST',
        body: json({
          prompt,
          model,
          confirm_sensitive: confirmSensitive,
          mode,
          attachments: files.map((a) => ({ name: a.name, data: a.data })),
        }),
      })
      // Even if another conversation was opened meanwhile: the files come back with the message if it is not sent.
      if (res.message_id && files.length)
        sentFilesRef.current.set(res.message_id, { conversationId: id, turnId: res.turn_id, files })
      // The server has them now; reopening the same conversation meanwhile keeps the composer, so this comes first.
      setAttachments((cur) => cur.filter((a) => !files.includes(a)))
      if (generationRef.current !== generation) return
      if (res.message_id) return // listed above the composer until Copilot takes it
      // The answer ended meanwhile, so the message started a new turn; its events follow those of the current one.
      loadConversations()
      const text = res.message?.content ?? prompt
      const shown = shownAttachments(res.message?.attachments ?? [], files.map((a) => a.url))
      const start = () => {
        if (generationRef.current !== generation) return
        setItems((prev) => [...prev, { kind: 'user', text, attachments: shown }])
        attach(res.turn_id)
      }
      if (sourceRef.current && sourceRef.current.readyState !== EventSource.CLOSED) nextTurnRef.current = start
      else start()
    } catch (e) {
      if (generationRef.current !== generation) return
      if (e instanceof ApiError && e.code === 'sensitive_data') {
        const ok = window.confirm(`${e.message}\nこの内容を Copilot に送信しますか？（ファイルには保存されません）`)
        if (ok) await sendFollowUp(mode, true, prompt, files)
        else restore()
        return
      }
      restore()
      if (e instanceof ApiError && e.status === 409 && !e.code) {
        // The turn is ending (中断 or the last answer): it takes no more messages, and the next turn has not started.
        setError('回答を終えるところのため送信できませんでした。回答が終わってから、もう一度送信してください。')
        return
      }
      setError((e as Error).message)
    } finally {
      setFollowUpSending(null)
    }
  }

  const cancelWaiting = async (messageId: string) => {
    if (!turnId) return
    setError('')
    try {
      const res = await api<{ removed: boolean }>(`/api/turns/${turnId}/queue/${messageId}`, { method: 'DELETE' })
      if (!res.removed) setError('すでに送信したため、取り消せませんでした。')
    } catch (e) {
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
    if (!turnId) return
    setStopping(true)
    try {
      await api(`/api/turns/${turnId}/abort`, { method: 'POST' })
    } catch (e) {
      setStopping(false)
      setError((e as Error).message)
    }
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
              if (turnId) sendFollowUp('now')
              else send()
            }}
          >
            {waiting.length > 0 && (
              <ul className="waiting-messages" aria-label="送信待ちのメッセージ">
                {waiting.map((m) => (
                  <li key={m.id}>
                    <span className="waiting-state">{m.mode === 'now' ? '回答に反映待ち' : '回答後に送信'}</span>
                    <span className="waiting-text" title={m.text}>
                      {m.text}
                    </span>
                    {m.attachments && m.attachments.length > 0 && (
                      <span
                        className="waiting-files"
                        role="img"
                        aria-label={`添付 ${m.attachments.length} 件`}
                        title={m.attachments.map((a) => a.name).join('\n')}
                      >
                        📎 {m.attachments.length}
                      </span>
                    )}
                    {/* After 中断 the waiting messages come back to the composer instead. */}
                    {m.mode === 'later' && !stopping && (
                      <button
                        type="button"
                        className="link small"
                        onClick={() => cancelWaiting(m.id)}
                        aria-label={`「${m.text.slice(0, 40)}」の送信を取り消す`}
                      >
                        取り消す
                      </button>
                    )}
                  </li>
                ))}
              </ul>
            )}
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
              placeholder={
                turnId ? '回答中も追加で送信できます（Ctrl+Enter ですぐに送信）' : 'メッセージを入力（Ctrl+Enter で送信。画像は貼り付けでも添付できます）'
              }
              rows={3}
              onKeyDown={(e) => {
                if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) {
                  e.preventDefault()
                  if (turnId) sendFollowUp('now')
                  else send()
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
                <div className="composer-buttons">
                  <button
                    type="submit"
                    className="button primary"
                    disabled={(!input.trim() && attachments.length === 0) || followUpBlocked}
                    title="回答中の内容に反映します（元の依頼と、追加の内容の両方に対応します）"
                  >
                    {followUpSending === 'now' ? '送信中…' : reading ? '読み込み中…' : 'すぐに送信'}
                  </button>
                  <button
                    type="button"
                    className="button"
                    onClick={() => sendFollowUp('later')}
                    disabled={(!input.trim() && attachments.length === 0) || followUpBlocked}
                    title="今の回答が終わってから送信します"
                  >
                    {followUpSending === 'later' ? '送信中…' : 'あとで送信'}
                  </button>
                  <button type="button" className="button" onClick={abort} disabled={stopping}>
                    {stopping ? '中断中…' : '中断'}
                  </button>
                </div>
              ) : (
                <button
                  type="submit"
                  className="button primary"
                  disabled={(!input.trim() && attachments.length === 0) || sending || reading > 0 || !!followUpSending}
                >
                  {sending || followUpSending ? '送信中…' : reading ? '読み込み中…' : '送信'}
                </button>
              )}
            </div>
          </form>
        )}
      </section>
    </div>
  )
}
