import type { ShownAttachment } from '../chatItems'

// Chips for attached images and files: in the composer (removable) and on a sent message.
export function AttachmentList({ items, onRemove }: { items: ShownAttachment[]; onRemove?: (index: number) => void }) {
  return (
    <ul className="attachments">
      {items.map((a, i) => (
        <li key={i} className="attachment" title={a.truncated ? `${a.name}（長いため、先頭の一部だけを Copilot に渡しました）` : a.name}>
          {a.url ? <img src={a.url} alt="" /> : <span aria-hidden="true">{a.kind === 'image' ? '🖼️' : '📄'}</span>}
          <span className="attachment-name">{a.name}</span>
          {a.truncated && <small>（一部のみ）</small>}
          {onRemove && (
            <button type="button" className="link danger" onClick={() => onRemove(i)} title="添付を外す" aria-label={`${a.name} の添付を外す`}>
              ×
            </button>
          )}
        </li>
      ))}
    </ul>
  )
}
