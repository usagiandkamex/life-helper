import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react'
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
import { SelectAllButton } from '../components/SelectAllButton'
import { draftEpoch, draftStore } from '../drafts'
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
const ATTACHMENT_REFRESH_MS = 60_000

// The server checks the same limits and the file contents; these only give an early message.
const MAX_ATTACHMENTS = 5
const MAX_ATTACHMENT_BYTES = 10 * 1024 * 1024
// Same limit as the turns API (MAX_PROMPT_CHARS), so a long error log is explained here instead of failing
// with HTTP 422.
const MAX_PROMPT_CHARS = 50_000
// Counted like Python does, in code points: an emoji is one character here too, not two.
const promptLength = (text: string) => [...text].length
const IMAGE_TYPES = ['image/png', 'image/jpeg', 'image/gif', 'image/webp']
const FILE_SUFFIXES = ['.txt', '.md', '.csv', '.tsv', '.json', '.pdf']
const ATTACH_ACCEPT = [...IMAGE_TYPES, ...FILE_SUFFIXES].join(',')

type PendingAttachment = { name: string; image: boolean; size: number; data: string; url?: string }

// The chat page is unmounted when another tab is opened, so what was open is kept here and opened again on return.
let lastOpened: { kind: 'chat' | 'automation'; id: string } | null = null

// What is being written in each conversation (text, files and the model chosen for it), kept while another
// conversation or tab is open: it does not follow to another conversation, and is back when its own is opened again.
// model null: the model the conversation last answered with (or the default). NEW_CONVERSATION: one not created yet.
type Composer = { input: string; attachments: PendingAttachment[]; model: string | null }
const NEW_CONVERSATION = ''
// Files being read for a composer (key as in composerKeyRef), held against its limits until they are attached.
type PendingRead = { key: string | null; count: number; bytes: number }
const EMPTY_COMPOSER: Composer = { input: '', attachments: [], model: null }
const composers = draftStore<Composer>()
// Kept outside the page: a read that ends after another tab was opened still counts until its files are attached,
// also for the page shown when coming back.
const pendingReads = new Map<symbol, PendingRead>()

function keepComposer(key: string, composer: Composer) {
  if (composer.input.trim() || composer.attachments.length > 0 || composer.model !== null) composers.set(key, composer)
  else composers.delete(key)
}

type ComposerUpdate = {
  input?: (cur: string) => string
  attachments?: (cur: PendingAttachment[]) => PendingAttachment[]
}
// The chat page on screen, if any. A request that ends after its page was left (another tab, then maybe back to a
// new page) changes the composer where it is now: on screen, or kept.
let shownComposer: { key: () => string | null; update: (update: ComposerUpdate) => void; showReading: () => void } | null =
  null

// epoch: when the request was started. After signing out since, nothing is written back (the drafts are gone).
function applyComposerUpdate(key: string, update: ComposerUpdate, epoch: number) {
  if (epoch !== draftEpoch()) return
  if (shownComposer?.key() === key) {
    shownComposer.update(update)
    return
  }
  const cur = composers.get(key) ?? EMPTY_COMPOSER
  keepComposer(key, {
    ...cur,
    input: update.input ? update.input(cur.input) : cur.input,
    attachments: update.attachments ? update.attachments(cur.attachments) : cur.attachments,
  })
}

// Windows draws the 📎 emoji as an unusual clip, so the attachment icon is drawn instead of written.
function ClipIcon({ size = 20 }: { size?: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" aria-hidden="true" focusable="false">
      <path
        d="M21.55 10.23 12.35 19.43a5.5 5.5 0 0 1-7.78-7.78l7.78-7.78a3.65 3.65 0 0 1 5.16 5.16l-6.37 6.37a1.8 1.8 0 0 1-2.55-2.55l4.95-4.95"
        fill="none"
        stroke="currentColor"
        strokeWidth="2"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  )
}

// 「すべて選択」: a dotted frame around a filled square, like the selection it makes.
function SelectAllIcon() {
  return (
    <svg width="20" height="20" viewBox="0 0 24 24" aria-hidden="true" focusable="false">
      <rect x="3" y="3" width="18" height="18" rx="2" fill="none" stroke="currentColor" strokeWidth="2" strokeDasharray="3 2.4" />
      <rect x="8" y="8" width="8" height="8" rx="1" fill="currentColor" />
    </svg>
  )
}

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

// Why the typed text cannot be sent, or "" when it can. How much has to go is said, because a pasted error log is
// not something the length of which can be guessed.
function promptProblem(prompt: string): string {
  const over = promptLength(prompt) - MAX_PROMPT_CHARS
  if (over <= 0) return ''
  return `メッセージは ${MAX_PROMPT_CHARS.toLocaleString('ja-JP')} 文字までです。${over.toLocaleString('ja-JP')} 文字減らすか、いくつかに分けて送信してください`
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
  const [input, setInput] = useState(() => composers.get(NEW_CONVERSATION)?.input ?? '')
  const [attachments, setAttachments] = useState(() => composers.get(NEW_CONVERSATION)?.attachments ?? [])
  // Files still being read, by the composer they were added in: counted against its limits, and sending from it waits
  // for them. `reading` is the number of reads for the composer on screen.
  // Files may still be read for the new conversation of a page left before.
  const [reading, setReading] = useState(() => [...pendingReads.values()].filter((r) => r.key === NEW_CONVERSATION).length)
  const [sending, setSending] = useState(false)
  const [models, setModels] = useState<{ id: string; name: string }[]>([])
  // Chosen in this composer; null until a model is picked here.
  const [model, setModel] = useState(() => composers.get(NEW_CONVERSATION)?.model ?? null)
  const [defaultModel, setDefaultModel] = useState('auto')
  const [turnId, setTurnId] = useState<string | null>(null)
  // Messages sent while the chat answers that Copilot has not taken yet.
  const [waiting, setWaiting] = useState<WaitingMessage[]>([])
  // The kind of message being sent while the chat answers, until the server has it.
  const [followUpSending, setFollowUpSending] = useState<FollowUpMode | null>(null)
  // From 中断 until the turn has ended: nothing more can be sent to it.
  const [stopping, setStopping] = useState(false)
  const [drawer, setDrawer] = useState(false)
  const [error, setError] = useState('')
  // What is sent, counted while it is typed: a pasted error log is over the limit long before that can be seen.
  const promptChars = useMemo(() => promptLength(input.trim()), [input])
  const promptTooLong = promptChars > MAX_PROMPT_CHARS
  // Near the limit the count is worth the room it takes in the composer; a short message never sees it.
  const showPromptChars = promptChars > MAX_PROMPT_CHARS * 0.9
  const sourceRef = useRef<EventSource | null>(null)
  // Opening a conversation ends after this page is left (another tab was chosen): the event stream is not started
  // then, because the cleanup that would close it has already run.
  const mountedRef = useRef(true)
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
  // New output scrolls the thread only while its end is in view: reading an earlier answer keeps the place.
  const followRef = useRef(true)
  const lastScrollTopRef = useRef(0)
  // A conversation that was just loaded starts at its end without scrolling there, which on a long one would
  // otherwise run down the whole history.
  const jumpRef = useRef(false)
  // Approvals already brought into view. Several writes can wait at once, so the latest pending one is not enough:
  // resolving one would make an older, already shown approval look new again.
  const approvalsRef = useRef(new Set<string>())
  // Unsent messages already put back into the composer: both the turn's end and the conversation report them.
  const restoredRef = useRef(new Set<string>())
  // By message id. Not across reloads: a message whose files are gone comes back without them.
  const sentFilesRef = useRef(new Map<string, SentFiles>())
  // While a message sent during the answer is being posted, the turn's events wait: its user or end event can arrive
  // before the response that tells which files belong to it.
  const heldEventsRef = useRef<{ holds: number; queue: (() => void)[] }>({ holds: 0, queue: [] })
  // A message sent just as the answer ended starts the next turn, which is followed once the current one has ended.
  const nextTurnRef = useRef<(() => void) | null>(null)
  // Reopening what was open runs once per visit to this page (StrictMode runs effects twice).
  const restoredSessionRef = useRef(false)
  // The conversation the composer on screen belongs to (null while an automation conversation is shown), and what it
  // holds as of the last render, to be kept when another conversation is opened.
  const composerKeyRef = useRef<string | null>(NEW_CONVERSATION)
  const composerRef = useRef<Composer>(EMPTY_COMPOSER)
  useLayoutEffect(() => {
    composerRef.current = { input, attachments, model }
  })
  // Drafts kept since this page was opened; signing out starts a new epoch (see drafts.ts).
  const epochRef = useRef(draftEpoch())

  const pendingReadsFor = (key: string | null) => [...pendingReads.values()].filter((r) => r.key === key)
  const showReading = useCallback(() => {
    setReading(pendingReadsFor(composerKeyRef.current).length)
  }, [])

  // The composer of a new conversation goes into the conversation just made, with the files still being read for it.
  const moveNewComposer = (id: string) => {
    composers.delete(NEW_CONVERSATION)
    composerKeyRef.current = id
    for (const [token, r] of pendingReads) if (r.key === NEW_CONVERSATION) pendingReads.set(token, { ...r, key: id })
  }

  // Once this page is left, what it shows is no longer kept: the page shown later may have changed the drafts since.
  const keepShownComposer = useCallback(() => {
    if (mountedRef.current && composerKeyRef.current !== null) keepComposer(composerKeyRef.current, composerRef.current)
  }, [])

  // Shows the composer of another conversation (null: none). The one on screen is kept unless its conversation is gone.
  const switchComposer = useCallback(
    (key: string | null, keep = true) => {
      if (keep) keepShownComposer()
      composerKeyRef.current = key
      const next = (key !== null && composers.get(key)) || EMPTY_COMPOSER
      composerRef.current = next
      setInput(next.input)
      setAttachments(next.attachments)
      setModel(next.model)
      showReading()
    },
    [keepShownComposer, showReading],
  )

  // For a request that ends after another conversation (or tab) was opened: it changes the composer it was started
  // from.
  const updateComposer = useCallback(
    (key: string, update: ComposerUpdate) => applyComposerUpdate(key, update, epochRef.current),
    [],
  )

  useEffect(() => {
    const shown = {
      key: () => composerKeyRef.current,
      update: (update: ComposerUpdate) => {
        // Also into the ref, for this page being left before the change is rendered.
        const cur = composerRef.current
        composerRef.current = {
          ...cur,
          input: update.input ? update.input(cur.input) : cur.input,
          attachments: update.attachments ? update.attachments(cur.attachments) : cur.attachments,
        }
        if (update.input) setInput(update.input)
        if (update.attachments) setAttachments(update.attachments)
      },
      showReading,
    }
    shownComposer = shown
    return () => {
      if (shownComposer === shown) shownComposer = null
    }
  }, [showReading])

  const selectConversation = useCallback((id: string | null) => {
    generationRef.current += 1
    // What is shown next starts at its latest message; its content going back to the top is not the user scrolling up.
    followRef.current = true
    lastScrollTopRef.current = 0
    currentIdRef.current = id
    setCurrentId(id)
    lastOpened = id ? { kind: 'chat', id } : null
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
      if (!mountedRef.current) return
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
      const handle = (run: () => void) => {
        const held = heldEventsRef.current
        if (held.holds > 0) held.queue.push(run)
        else run()
      }
      source.onmessage = (msg) => handle(() => {
        if (sourceRef.current !== source) return // another conversation was opened meanwhile
        const ev = JSON.parse(msg.data) as TurnEvent
        // A message sent with files from here shows their thumbnails (from the history, only their names).
        const thumbnails = ev.type === 'user' ? sentFilesRef.current.get(ev.id)?.files.map((f) => f.url) : undefined
        setItems((prev) => applyEvent(prev, ev, id, thumbnails))
        setWaiting((prev) => applyWaitingEvent(prev, ev))
        if (ev.type === 'user' || ev.type === 'unqueued') sentFilesRef.current.delete(ev.id)
        if (ev.type === 'end') {
          source.close()
          restoreUnsent(ev.unsent, (sent) => sent.turnId === id)
          ended()
        }
      })
      source.onerror = () => handle(() => {
        if (sourceRef.current === source && source.readyState === EventSource.CLOSED) ended()
      })
    },
    [loadConversations, restoreUnsent],
  )

  const openConversation = useCallback(
    async (id: string) => {
      sourceRef.current?.close()
      sourceRef.current = null
      setTurnId(null)
      // The composer belongs to the conversation it was typed in, so it must not follow us to another one.
      switchComposer(id)
      selectConversation(id)
      const generation = generationRef.current
      setDrawer(false)
      setItems([])
      setError('')
      try {
        const data = await api<{ messages: HistoryMessage[]; busy: boolean; turn_id?: string; unsent?: UnsentMessage[] }>(
          `/api/conversations/${id}/messages`,
        )
        // A slower answer must not replace what was opened since; after this page was left, the page shown when
        // coming back opens it again (and puts back what was not sent).
        if (generationRef.current !== generation || !mountedRef.current) return
        // The whole history arrives at once, so it is shown at its end instead of scrolled there.
        jumpRef.current = true
        setItems(fromHistory(data.messages))
        if (data.busy && data.turn_id) attach(data.turn_id)
        // The turn ended while this conversation was not followed.
        else restoreUnsent(data.unsent, (sent) => sent.conversationId === id)
      } catch (e) {
        // A conversation deleted elsewhere must not be opened again every time this page is shown.
        if (e instanceof ApiError && e.status === 404 && lastOpened?.id === id) lastOpened = null
        if (generationRef.current === generation) setError((e as Error).message)
      }
    },
    [attach, restoreUnsent, selectConversation, switchComposer],
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
      lastOpened = { kind: 'automation', id }
      switchComposer(null)
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
        // Its runs may have been deleted; then it must not be opened again every time this page is shown.
        if (e instanceof ApiError && e.status === 404 && lastOpened?.id === id) lastOpened = null
        if (generationRef.current === generation) setError((e as Error).message)
      }
    },
    [markRead, selectConversation, switchComposer],
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
    switchComposer(NEW_CONVERSATION)
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
    mountedRef.current = true
    loadConversations().catch((e) => setError(e.message))
    api<{ default: string; models: { id: string; name: string }[] }>('/api/models')
      .then((d) => {
        setModels(d.models)
        setDefaultModel(d.default)
      })
      .catch(() => undefined)
    return () => {
      keepShownComposer()
      mountedRef.current = false
      sourceRef.current?.close()
    }
  }, [loadConversations, keepShownComposer])

  useEffect(() => {
    // A turn keeps running after its conversation is closed. Keep only unread files, including those awaiting
    // restoration after an abort; the server expires that turn 15 minutes after completion.
    let disposed = false
    let checking = false
    const followed = (sent: SentFiles) => {
      const source = sourceRef.current
      return source !== null && source.readyState !== EventSource.CLOSED && source.url.endsWith(`/api/turns/${sent.turnId}/events`)
    }
    const refresh = async () => {
      if (checking) return
      checking = true
      try {
        const entries = [...sentFilesRef.current].filter(([, sent]) => !followed(sent))
        const turns = new Set(entries.map(([, sent]) => sent.turnId))
        for (const id of turns) {
          if (disposed) return
          let unread: Set<string>
          try {
            const status = await api<{ unread_ids: string[] }>(`/api/turns/${id}`)
            unread = new Set(status.unread_ids)
          } catch (e) {
            if (!(e instanceof ApiError && e.status === 404)) continue // retry transient failures, without losing drafts
            unread = new Set()
          }
          if (disposed) return
          for (const [messageId, sent] of entries) {
            // A conversation may have reopened or a POST may have registered files while the request was in flight.
            if (sent.turnId === id && !followed(sent) && !unread.has(messageId) && sentFilesRef.current.get(messageId) === sent)
              sentFilesRef.current.delete(messageId)
          }
        }
      } finally {
        checking = false
      }
    }
    const timer = window.setInterval(refresh, ATTACHMENT_REFRESH_MS)
    window.addEventListener('focus', refresh)
    return () => {
      disposed = true
      window.clearInterval(timer)
      window.removeEventListener('focus', refresh)
    }
  }, [])

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
    // 別のタブから戻ったときは、移動する前に開いていた会話をもう一度開く。
    if (restoredSessionRef.current) return
    // The guard is set before the deep link is checked: once that link is handled it is removed from the URL, and
    // this effect must not open the previous conversation over it afterwards.
    restoredSessionRef.current = true
    if (params.get('thread')) return
    const last = lastOpened
    if (!last) return
    const open = last.kind === 'chat' ? openConversation : openThread
    open(last.id)
  }, [params, openConversation, openThread])

  // A conversation just opened is shown at its end right away (see jumpRef); this runs before the smooth scroll below.
  useLayoutEffect(() => {
    if (!jumpRef.current) return
    jumpRef.current = false
    const el = messagesRef.current
    if (el) el.scrollTop = el.scrollHeight
  }, [items])

  useEffect(() => {
    // A write waiting for approval holds up the answer: it is brought into view even while an earlier answer is read.
    for (const item of items) {
      if (item.kind !== 'approval' || item.status !== 'pending') continue
      if (!approvalsRef.current.has(item.id)) {
        approvalsRef.current.add(item.id)
        followRef.current = true
      }
    }
    if (followRef.current) bottomRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [items])

  // Only scrolling up stops following: the smooth scroll to new output passes through places short of the end.
  const trackScroll = (el: HTMLDivElement) => {
    if (el.scrollHeight - el.scrollTop - el.clientHeight < 40) followRef.current = true
    else if (el.scrollTop < lastScrollTopRef.current) followRef.current = false
    lastScrollTopRef.current = el.scrollTop
  }

  // An automation conversation is also loaded as a whole, so it is placed before it is shown, not scrolled to.
  useLayoutEffect(() => {
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

  // A model picked in this composer wins; otherwise the one the conversation last answered with, while it is offered.
  const conversationModel = conversations.find((c) => c.id === currentId)?.model ?? ''
  const offered = (id: string) => models.length === 0 || models.some((m) => m.id === id)
  const selectedModel = model ?? (conversationModel && offered(conversationModel) ? conversationModel : defaultModel)
  const modelOptions = models.length ? [...models] : [{ id: 'auto', name: 'auto' }]
  if (!modelOptions.some((m) => m.id === selectedModel)) modelOptions.push({ id: selectedModel, name: selectedModel })

  const newConversation = async () => {
    const generation = generationRef.current
    const conv = await api<Conversation>('/api/conversations', { method: 'POST', body: json({ model: selectedModel }) })
    await loadConversations()
    // Something else was opened while it was being created, or this page was left.
    if (generationRef.current !== generation || !mountedRef.current) return
    // What was being written for a new conversation goes into the one just made.
    if (composerKeyRef.current === NEW_CONVERSATION) moveNewComposer(conv.id)
    await openConversation(conv.id)
  }

  // Checks what can be checked here (type, size, count) before reading the files for sending.
  const addAttachments = async (files: File[]) => {
    const generation = generationRef.current
    const key = composerKeyRef.current
    const problems: string[] = []
    const pending = pendingReadsFor(key)
    let count = attachments.length + pending.reduce((sum, r) => sum + r.count, 0)
    let bytes = attachments.reduce((sum, a) => sum + a.size, 0) + pending.reduce((sum, r) => sum + r.bytes, 0)
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
    const token = Symbol()
    pendingReads.set(token, { key, count: accepted.length, bytes: accepted.reduce((sum, f) => sum + f.size, 0) })
    showReading()
    try {
      const read = await Promise.all(accepted.map(readAttachment))
      // Switched to another conversation while reading: the files belong to the one they were added in (or, for a new
      // conversation, the one it has become since).
      const target = pendingReads.get(token)?.key ?? null
      if (target !== null) updateComposer(target, { attachments: (cur) => [...cur, ...read] })
    } catch (e) {
      if (generationRef.current === generation) setError((e as Error).message)
    } finally {
      pendingReads.delete(token)
      shownComposer?.showReading()
    }
  }

  const send = async (confirmSensitive = false) => {
    const draft = input
    const prompt = input.trim()
    const files = attachments
    if ((!prompt && files.length === 0) || turnId || sending || reading || followUpSending) return
    const problem = promptProblem(prompt) || attachmentProblem(files)
    if (problem) {
      setError(problem)
      return
    }
    setError('')
    setSending(true)
    // Sending from here goes back to the latest message; scrolling up while it is being sent still wins.
    followRef.current = true
    try {
      let generation = generationRef.current
      // The ref, not the render's value: a retry after the sensitive-data prompt must reuse the conversation just made.
      let id = currentIdRef.current
      if (!id) {
        const conv = await api<Conversation>('/api/conversations', { method: 'POST', body: json({ model: selectedModel }) })
        // Not sent; what was written stays in the composer of a new conversation.
        if (generationRef.current !== generation || !mountedRef.current) {
          loadConversations()
          return
        }
        id = conv.id
        selectConversation(id)
        // The composer goes with what is being sent into the conversation just made.
        moveNewComposer(id)
        generation = generationRef.current
      }
      try {
        const res = await api<{
          turn_id: string
          message: { content: string; attachments: SentAttachment[]; at?: string }
        }>(
          `/api/conversations/${id}/turns`,
          {
            method: 'POST',
            body: json({
              prompt,
              model: selectedModel,
              confirm_sensitive: confirmSensitive,
              attachments: files.map((a) => ({ name: a.name, data: a.data })),
            }),
          },
        )
        loadConversations()
        // Sent: also when another conversation was opened meanwhile, the kept composer must not offer it again.
        updateComposer(id, {
          input: (cur) => (cur === draft ? '' : cur),
          attachments: (cur) => cur.filter((a) => !files.includes(a)),
        })
        // Opened something else meanwhile: the turn keeps running and is followed when its conversation is opened.
        if (generationRef.current !== generation) return
        const shown = shownAttachments(res.message.attachments, files.map((a) => a.url))
        setItems((prev) => [...prev, { kind: 'user', text: res.message.content, attachments: shown, at: res.message.at }])
        attach(res.turn_id)
      } catch (e) {
        // Not sent; what was written is still in its composer (asking about it waits until it is opened again).
        if (generationRef.current !== generation || !mountedRef.current) return
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
    const problem = promptProblem(prompt) || attachmentProblem(files)
    if (problem) {
      setError(problem)
      return
    }
    setError('')
    // The text is cleared at once so that the next message can be typed meanwhile, and comes back if it cannot be
    // sent; the files stay in the composer until the server has them.
    setInput((cur) => (cur.trim() === prompt ? '' : cur))
    // Back into the composer of its conversation, also when another one has been opened since.
    const restore = () => updateComposer(id, { input: (cur) => [prompt, cur.trim()].filter(Boolean).join('\n\n') })
    const generation = generationRef.current
    setFollowUpSending(mode)
    try {
      // message: when the answer ended meanwhile and the message started a new turn, as for send().
      let res: {
        turn_id: string
        message_id?: string
        message?: { content: string; attachments: SentAttachment[]; at?: string }
      }
      const held = heldEventsRef.current
      held.holds += 1
      try {
        res = await api<typeof res>(`/api/conversations/${id}/turns`, {
          method: 'POST',
          body: json({
            prompt,
            model: selectedModel,
            confirm_sensitive: confirmSensitive,
            mode,
            attachments: files.map((a) => ({ name: a.name, data: a.data })),
          }),
        })
        // Even if another conversation was opened meanwhile: the files come back with the message if it is not sent.
        if (res.message_id && files.length)
          sentFilesRef.current.set(res.message_id, { conversationId: id, turnId: res.turn_id, files })
        // The server has them now, also when another conversation was opened meanwhile; this comes first (and before
        // the held events, whose end may put them back).
        updateComposer(id, { attachments: (cur) => cur.filter((a) => !files.includes(a)) })
      } finally {
        held.holds -= 1
        if (held.holds === 0) for (const run of held.queue.splice(0)) run()
      }
      if (generationRef.current !== generation) return
      if (res.message_id) return // listed above the composer until Copilot takes it
      // The answer ended meanwhile, so the message started a new turn; its events follow those of the current one.
      loadConversations()
      const text = res.message?.content ?? prompt
      const shown = shownAttachments(res.message?.attachments ?? [], files.map((a) => a.url))
      const start = () => {
        if (generationRef.current !== generation) return
        setItems((prev) => [...prev, { kind: 'user', text, attachments: shown, at: res.message?.at }])
        attach(res.turn_id)
      }
      if (sourceRef.current && sourceRef.current.readyState !== EventSource.CLOSED) nextTurnRef.current = start
      else start()
    } catch (e) {
      if (generationRef.current !== generation || !mountedRef.current) {
        restore()
        return
      }
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
      if (res.removed) sentFilesRef.current.delete(messageId)
      else setError('すでに送信したため、取り消せませんでした。')
    } catch (e) {
      setError((e as Error).message)
    }
  }

  const remove = async (id: string) => {
    if (!window.confirm('この会話を削除しますか？（Copilot 側の履歴も削除されます）')) return
    try {
      await api(`/api/conversations/${id}`, { method: 'DELETE' })
      for (const [messageId, sent] of sentFilesRef.current)
        if (sent.conversationId === id) sentFilesRef.current.delete(messageId)
      // Files still being read for it are dropped when read, instead of making its composer again.
      for (const [token, r] of pendingReads) if (r.key === id) pendingReads.delete(token)
      composers.delete(id)
      if (currentIdRef.current === id) {
        selectConversation(null)
        setItems([])
        switchComposer(NEW_CONVERSATION, false)
      }
      await loadConversations()
    } catch (e) {
      setError((e as Error).message)
    }
  }

  const organize = async () => {
    const generation = generationRef.current
    const res = await api<{ turn_id: string; conversation_id: string; message: { at?: string } }>(
      '/api/memories/organize',
      { method: 'POST' },
    )
    await loadConversations()
    // It keeps running; its conversation shows it when opened.
    if (generationRef.current !== generation || !mountedRef.current) return
    selectConversation(res.conversation_id)
    switchComposer(res.conversation_id)
    // The request itself is long; what is shown instead says what was asked, at the time it was asked.
    setItems([{ kind: 'user', text: 'メモリの整理を依頼しました。', at: res.message.at }])
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
        <div className="messages" ref={messagesRef} onScroll={(e) => trackScroll(e.currentTarget)}>
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
        {error && (
          <div className="banner error" role="alert">
            {error}
          </div>
        )}
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
                        <ClipIcon size={14} /> {m.attachments.length}
                      </span>
                    )}
                    {/* After 中断 the waiting messages come back to the composer instead. */}
                    {m.mode === 'later' && !stopping && (
                      <button
                        type="button"
                        className="link small"
                        onClick={() => cancelWaiting(m.id)}
                        aria-label={`「${m.text.slice(0, 40) || m.attachments?.map((a) => a.name).join('、') || '添付のみのメッセージ'}」の送信を取り消す`}
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
              // Read out with the input field instead of at every keystroke, which a live region would do.
              aria-describedby={showPromptChars ? 'prompt-chars' : undefined}
              aria-invalid={promptTooLong || undefined}
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
            {/* Shown only near the limit, so the usual short message keeps the composer as it is. Sending says
                how much has to go; here the count alone is enough. */}
            {showPromptChars && (
              <p id="prompt-chars" className={promptTooLong ? 'banner error' : 'hint'}>
                {promptChars.toLocaleString('ja-JP')} / {MAX_PROMPT_CHARS.toLocaleString('ja-JP')} 文字
                {promptTooLong && '（このままでは送信できません）'}
              </p>
            )}
            <div className="composer-actions">
              <div className="composer-tools">
                <label className="button icon" title="画像・テキスト・CSV・JSON・PDF を添付（5 件・合計 10 MB まで）">
                  <ClipIcon />
                  <span className="visually-hidden">添付</span>
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
                <SelectAllButton target={inputRef} className="button icon" title="入力した文章をすべて選択">
                  <SelectAllIcon />
                  <span className="visually-hidden">入力した文章をすべて選択</span>
                </SelectAllButton>
                <select value={selectedModel} onChange={(e) => setModel(e.target.value)} aria-label="モデル">
                  {modelOptions.map((m) => (
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
                    {/* On a phone the label stays 送信 (the row has no room for more); the whole label is still read out. */}
                    <span className="wide-only">
                      {followUpSending === 'now' ? '送信中…' : reading ? '読み込み中…' : 'すぐに送信'}
                    </span>
                    <span className="mobile-only" aria-hidden="true">
                      送信
                    </span>
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
                  <button type="button" className="button stop" onClick={abort} disabled={stopping}>
                    <span className="stop-icon mobile-only" aria-hidden="true" />
                    <span className="wide-only">{stopping ? '中断中…' : '中断'}</span>
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
