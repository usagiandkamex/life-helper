import { TOOL_LABELS, type Item } from '../chatItems'
import { ApprovalCard } from './ApprovalCard'
import { AttachmentList } from './AttachmentList'
import { LazyChart } from './LazyChart'
import { Markdown } from './Markdown'

export function MessageItem({ item, onSchedule }: { item: Item; onSchedule?: (text: string) => void }) {
  switch (item.kind) {
    case 'user':
      return (
        <div className="msg user">
          {item.attachments && item.attachments.length > 0 && <AttachmentList items={item.attachments} />}
          <div className="bubble">{item.text}</div>
          {/* A schedule keeps only the text, so a question about attached files is not offered. */}
          {onSchedule && !item.attachments?.length && (
            <button className="link small" onClick={() => onSchedule(item.text)}>
              この質問を定期実行
            </button>
          )}
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
              {item.success === false ? '⚠️' : item.success ? '✔' : '…'}{' '}
              {/* A look-up a research sub-agent ran in parallel, not the assistant itself. */}
              {item.subagent ? '調査 › ' : ''}
              {TOOL_LABELS[item.name] ?? item.name}
            </summary>
            <pre>{item.args}</pre>
            {item.error && <p className="error-text">{item.error}</p>}
          </details>
          {item.chart && <LazyChart chart={item.chart} />}
          {item.screenshot && (
            <figure className="screenshot">
              <a href={item.screenshot.url} target="_blank" rel="noreferrer">
                <img src={item.screenshot.url} alt="ブラウザのスクリーンショット" loading="lazy" />
              </a>
            </figure>
          )}
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
    case 'approval':
      return <ApprovalCard item={item} />
    case 'note':
      return <p className="hint msg-note">{item.text}</p>
  }
}
