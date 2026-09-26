import { useCallback, useEffect, useMemo, useState } from 'react'
import { api, ApiError, formatDate, json } from '../api'
import { Markdown } from '../components/Markdown'
import type { FileEntry } from '../types'

const GROUP_LABELS: Record<string, string> = {
  profile: 'プロフィール（毎回の会話に反映）',
  memories: 'メモリ（会話から覚えたこと）',
  notes: 'ノート',
  plans: 'ライフプラン',
  money: '資産',
  docs: 'アップロードした資料',
  '': 'その他',
}

// 形式ちがい・サイズ超過など、そのファイルだけが拒否された場合に /api/files/upload が返す状態コード。
const FILE_ERROR_STATUSES = [400, 413]

const summarize = (items: string[], limit = 3): string =>
  items.length > limit ? `${items.slice(0, limit).join('、')} ほか ${items.length - limit} 件` : items.join('、')

export function KnowledgePage() {
  const [files, setFiles] = useState<FileEntry[]>([])
  const [selected, setSelected] = useState<string | null>(null)
  const [content, setContent] = useState('')
  const [writable, setWritable] = useState(false)
  const [editing, setEditing] = useState(false)
  const [message, setMessage] = useState('')
  const [error, setError] = useState('')
  const [uploading, setUploading] = useState<{ current: number; total: number } | null>(null)
  const [listOpen, setListOpen] = useState(true)

  const load = useCallback(async () => setFiles(await api<FileEntry[]>('/api/files')), [])
  useEffect(() => {
    load().catch((e) => setError(e.message))
  }, [load])

  const groups = useMemo(() => {
    const out: Record<string, FileEntry[]> = {}
    for (const f of files) {
      const key = f.path.includes('/') ? f.path.split('/')[0] : ''
      ;(out[key] ??= []).push(f)
    }
    return out
  }, [files])

  const open = async (path: string) => {
    setError('')
    setMessage('')
    const data = await api<{ content: string; writable: boolean }>(`/api/files/content?path=${encodeURIComponent(path)}`)
    setSelected(path)
    setContent(data.content)
    setWritable(data.writable)
    setEditing(false)
  }

  const save = async () => {
    if (!selected) return
    try {
      await api('/api/files/content', { method: 'PUT', body: json({ path: selected, content }) })
      setEditing(false)
      setMessage('保存しました')
      await load()
    } catch (e) {
      setError(e instanceof ApiError ? e.message : String(e))
    }
  }

  const remove = async () => {
    if (!selected || !window.confirm(`${selected} を削除しますか？`)) return
    await api(`/api/files?path=${encodeURIComponent(selected)}`, { method: 'DELETE' })
    setSelected(null)
    setContent('')
    await load()
  }

  const newNote = async () => {
    const name = window.prompt('ノートのファイル名（例: 2026-ふるさと納税）')
    if (!name) return
    const path = `notes/${name.replace(/[\\/]/g, '_')}${name.endsWith('.md') ? '' : '.md'}`
    try {
      await api('/api/files/content', { method: 'PUT', body: json({ path, content: `# ${name}\n\n` }) })
      await load()
      await open(path)
      setEditing(true)
    } catch (e) {
      setError((e as Error).message)
    }
  }

  const upload = async (files: File[]) => {
    if (!files.length) return
    setError('')
    setMessage('')
    const saved: string[] = []
    const redacted = new Set<string>()
    const failed: string[] = []
    const notes: string[] = []
    const problems: string[] = []
    try {
      // 1 ファイルずつ順番に送る: 同名ファイルの連番（docs/x-2.md）が取り込んだ順に決まる。
      for (const [index, file] of files.entries()) {
        setUploading({ current: index + 1, total: files.length })
        const form = new FormData()
        form.append('file', file)
        try {
          const res = await api<{ path: string; redacted: string[] }>('/api/files/upload', {
            method: 'POST',
            body: form,
          })
          saved.push(res.path)
          for (const kind of res.redacted) redacted.add(kind)
        } catch (e) {
          // そのファイルだけの問題なら残りを続ける。認証切れ・通信・サーバーのエラーは続けても失敗するので中断する。
          if (e instanceof ApiError && FILE_ERROR_STATUSES.includes(e.status)) {
            failed.push(`${file.name}（${e.message}）`)
            continue
          }
          const rest = files.length - index - 1
          problems.push(
            `${file.name}（${(e as Error).message}）で中断しました${rest ? `。残りの ${rest} 件は取り込んでいません` : ''}`,
          )
          break
        }
      }
      if (saved.length) {
        notes.push(
          saved.length === 1
            ? `${saved[0]} に取り込みました`
            : `${saved.length} 件を取り込みました（${summarize(saved)}）`,
        )
        if (redacted.size) notes.push(`${[...redacted].join('・')}は削除して保存しました`)
      }
      if (failed.length) problems.unshift(`取り込めませんでした: ${summarize(failed)}`)
      try {
        if (saved.length) {
          await load()
          await open(saved[0])
        }
      } catch (e) {
        problems.push((e as Error).message)
      }
      // open() は両方のバナーを消すため、最後にまとめて表示する。
      setMessage(notes.join('。'))
      setError(problems.join(' / '))
    } finally {
      setUploading(null)
    }
  }

  return (
    <div className={`split${listOpen ? '' : ' list-closed'}`}>
      <aside className="panel">
        <div className="row list-head">
          <h2 className="grow">ファイル一覧</h2>
          <button className="button small" aria-expanded={listOpen} aria-controls="file-list" onClick={() => setListOpen(!listOpen)}>
            {listOpen ? '閉じる' : '開く'}
          </button>
        </div>
        {/* 取り込みは一覧を閉じても続くので、進み具合は見出しの下に出す */}
        {!listOpen && uploading && (
          <p className="hint" role="status">
            取り込み中…（{uploading.current}/{uploading.total}）
          </p>
        )}
        {/* 閉じても消さずに隠す: 開閉ボタンの aria-controls が指す先を残す */}
        <div id="file-list" hidden={!listOpen}>
          <div className="row wrap">
            <button className="button small" onClick={newNote}>
              ＋ ノート
            </button>
            <label className="button small">
              {uploading ? `取り込み中…（${uploading.current}/${uploading.total}）` : '資料を取り込む'}
              <input
                type="file"
                accept=".md,.txt,.pdf"
                multiple
                hidden
                disabled={!!uploading}
                onChange={(e) => {
                  const files = Array.from(e.currentTarget.files ?? [])
                  // 同じファイルを選び直しても再度取り込めるように、送信前に選択を空にする。
                  e.currentTarget.value = ''
                  upload(files)
                }}
              />
            </label>
            <a className="button small" href="/api/export.zip">
              ZIP で書き出し
            </a>
          </div>
          {Object.entries(GROUP_LABELS)
            .filter(([key]) => groups[key]?.length)
            .map(([key, label]) => (
              <div key={key} className="file-group">
                <h3>{label}</h3>
                <ul>
                  {groups[key].map((f) => (
                    <li key={f.path} className={f.path === selected ? 'active' : ''}>
                      <button className="link" onClick={() => open(f.path)}>
                        {f.path.split('/').slice(1).join('/') || f.path}
                      </button>
                      <small>{formatDate(f.modified)}</small>
                    </li>
                  ))}
                </ul>
              </div>
            ))}
          <p className="hint">
            資料は複数まとめて選べます。PDF はテキストを抽出して保存します（スキャン画像の PDF は非対応）。原本は保存しません。
          </p>
        </div>
      </aside>
      <section className="panel grow">
        {message && <div className="banner ok">{message}</div>}
        {error && <div className="banner error">{error}</div>}
        {!selected && (
          <p className="hint">{listOpen ? '左の一覧からファイルを選んでください。' : 'ファイル一覧を開いて、ファイルを選んでください。'}</p>
        )}
        {selected && (
          <>
            <div className="row">
              <h2 className="grow">{selected}</h2>
              {writable && !editing && (
                <button className="button small" onClick={() => setEditing(true)}>
                  編集
                </button>
              )}
              {writable && editing && (
                <button className="button small primary" onClick={save}>
                  保存
                </button>
              )}
              {writable && (
                <button className="button small danger" onClick={remove}>
                  削除
                </button>
              )}
            </div>
            {editing ? (
              <textarea className="editor" value={content} onChange={(e) => setContent(e.target.value)} />
            ) : selected.endsWith('.md') ? (
              <Markdown text={content} />
            ) : (
              <pre className="file-view">{content}</pre>
            )}
            {!writable && <p className="hint">このファイルは画面からは編集できません。</p>}
          </>
        )}
      </section>
    </div>
  )
}
