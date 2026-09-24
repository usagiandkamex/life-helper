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

export function KnowledgePage() {
  const [files, setFiles] = useState<FileEntry[]>([])
  const [selected, setSelected] = useState<string | null>(null)
  const [content, setContent] = useState('')
  const [writable, setWritable] = useState(false)
  const [editing, setEditing] = useState(false)
  const [message, setMessage] = useState('')
  const [error, setError] = useState('')

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

  const upload = async (file: File) => {
    const form = new FormData()
    form.append('file', file)
    try {
      const res = await api<{ path: string; redacted: string[] }>('/api/files/upload', { method: 'POST', body: form })
      setMessage(
        res.redacted.length
          ? `${res.path} に取り込みました（${res.redacted.join('・')}は削除して保存しました）`
          : `${res.path} に取り込みました`,
      )
      await load()
      await open(res.path)
    } catch (e) {
      setError((e as Error).message)
    }
  }

  return (
    <div className="split">
      <aside className="panel">
        <div className="row wrap">
          <button className="button small" onClick={newNote}>
            ＋ ノート
          </button>
          <label className="button small">
            資料を取り込む
            <input type="file" accept=".md,.txt,.pdf" hidden onChange={(e) => e.target.files?.[0] && upload(e.target.files[0])} />
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
        <p className="hint">PDF はテキストを抽出して保存します（スキャン画像の PDF は非対応）。原本は保存しません。</p>
      </aside>
      <section className="panel grow">
        {message && <div className="banner ok">{message}</div>}
        {error && <div className="banner error">{error}</div>}
        {!selected && <p className="hint">左の一覧からファイルを選んでください。</p>}
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
