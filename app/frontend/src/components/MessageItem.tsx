import { TOOL_LABELS, type Item } from '../chatItems'
import { LazyChart } from './LazyChart'
import { Markdown } from './Markdown'

export function MessageItem({ item, onSchedule }: { item: Item; onSchedule?: (text: string) => void }) {
  switch (item.kind) {
    case 'user':
      return (
        <div className="msg user">
          <div className="bubble">{item.text}</div>
          {onSchedule && (
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
              {item.success === false ? '⚠️' : item.success ? '✔' : '…'} {TOOL_LABELS[item.name] ?? item.name}
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
    case 'note':
      return <p className="hint msg-note">{item.text}</p>
  }
}
