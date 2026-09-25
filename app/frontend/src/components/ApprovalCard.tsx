import { useState } from 'react'
import { api, ApiError, json } from '../api'
import type { ApprovalItem } from '../chatItems'

type Decision = 'approve' | 'approve_all' | 'reject'

function diffClass(line: string, index: number): string {
  // Only the two file headers start with ---/+++; a content line such as "+++ note" is an addition.
  if (index < 2 && (line.startsWith('--- ') || line.startsWith('+++ '))) return 'meta'
  if (line.startsWith('@@') || line.startsWith('\\')) return 'meta'
  if (line.startsWith('+')) return 'add'
  if (line.startsWith('-')) return 'del'
  return ''
}

function DiffView({ diff }: { diff: string }) {
  return (
    <pre className="diff">
      {diff.split('\n').map((line, i) => (
        <span key={i} className={diffClass(line, i)}>
          {line}
          {'\n'}
        </span>
      ))}
    </pre>
  )
}

const APPROVAL_LABELS: Record<Exclude<ApprovalItem['status'], 'pending'>, string> = {
  approved: 'への書き込みを承認しました',
  rejected: 'への書き込みを却下しました',
  expired: 'には、承認されなかったため書き込みませんでした',
  cancelled: 'には、回答が終わったため書き込みませんでした',
}

export function ApprovalCard({ item }: { item: ApprovalItem }) {
  const [sending, setSending] = useState<Decision | null>(null)
  const [error, setError] = useState('')

  const decide = async (decision: Decision) => {
    setSending(decision)
    setError('')
    try {
      await api(`/api/turns/${item.turnId}/approvals/${item.id}`, { method: 'POST', body: json({ decision }) })
    } catch (e) {
      // 409: already decided (another tab, or the answer ended); the result arrives on the stream.
      if (!(e instanceof ApiError && e.status === 409)) setError((e as Error).message)
    } finally {
      setSending(null)
    }
  }

  if (item.status === 'pending') {
    return (
      <div className="msg approval pending" role="group" aria-label={`${item.path} への書き込みの承認`}>
        <p className="approval-title">
          ✏️ <code>{item.path}</code> に書き込もうとしています。内容を確認してください。
        </p>
        <DiffView diff={item.diff} />
        <div className="approval-actions">
          <button className="button primary small" disabled={!!sending} onClick={() => decide('approve')}>
            承認
          </button>
          <button className="button small" disabled={!!sending} onClick={() => decide('approve_all')}>
            この回答中はすべて承認
          </button>
          <button className="button danger small" disabled={!!sending} onClick={() => decide('reject')}>
            却下
          </button>
        </div>
        {error && <p className="error-text">{error}</p>}
      </div>
    )
  }
  const label =
    item.status === 'approved' && item.written
      ? `💾 ${item.path} に書き込みました`
      : `${item.status === 'approved' ? '✔' : '✖'} ${item.path} ${APPROVAL_LABELS[item.status]}`
  return (
    <div className={`msg approval ${item.status}`}>
      <details>
        <summary>{label}（内容を確認）</summary>
        <DiffView diff={item.diff} />
      </details>
    </div>
  )
}
