import { formatDate } from '../api'
import { hasAnswer, runItems, runProblem, statusLabel } from '../automationRuns'
import type { AutomationThreadDetail, RunRecord } from '../types'
import { Markdown } from './Markdown'
import { MessageItem } from './MessageItem'

export function AutomationThreadView({
  detail,
  busy,
  onLoadOlder,
  onShowLatest,
  onAsk,
}: {
  detail: AutomationThreadDetail
  busy: boolean
  onLoadOlder: () => void
  onShowLatest: () => void
  onAsk: (run: RunRecord) => void
}) {
  return (
    <>
      {detail.thread.mode === 'continue' &&
        (detail.has_more ? (
          <button className="button small load-more" onClick={onLoadOlder} disabled={busy}>
            さらに前の実行を表示
          </button>
        ) : (
          <p className="hint">これより前の実行は、オートメーション画面の実行履歴で確認できます。</p>
        ))}
      {detail.runs.map((run) => (
        <RunTranscript key={run.id} run={run} onAsk={onAsk} />
      ))}
      {detail.has_newer && (
        <button className="button small load-more" onClick={onShowLatest} disabled={busy}>
          最新の実行まで表示
        </button>
      )}
    </>
  )
}

function RunTranscript({ run, onAsk }: { run: RunRecord; onAsk: (run: RunRecord) => void }) {
  const items = runItems(run)
  const problem = runProblem(run)
  // A run can end with report_result alone; then the recorded summary is the answer.
  const fallback = run.status === 'success' && !run.report && !hasAnswer(items) ? run.final_message || run.summary || '' : ''
  return (
    <article className="run-transcript" id={`run-${run.id}`}>
      <header className="run-heading">
        <span>🤖 {run.name}</span>
        <span>{formatDate(run.started_at)}</span>
        <span className={`run-status ${run.status}`}>{statusLabel(run.status)}</span>
        {run.issue_url && (
          <a href={run.issue_url} target="_blank" rel="noopener noreferrer">
            GitHub 通知
          </a>
        )}
      </header>
      {run.prompt && <MessageItem item={{ kind: 'user', text: run.prompt }} />}
      {!!run.events_omitted && <p className="hint">記録が長いため、途中経過のうち最初の {run.events_omitted} 件を省略しています。</p>}
      {items.map((item, i) => (
        <MessageItem key={i} item={item} />
      ))}
      {fallback && <MessageItem item={{ kind: 'assistant', text: fallback }} />}
      {run.report && (
        <div className="run-report">
          <strong>📋 結果の報告</strong>
          <Markdown text={run.report.summary} />
          <small>{run.report.notify ? '知らせるべき結果として報告されました' : '知らせる必要はないと報告されました'}</small>
        </div>
      )}
      {problem && <div className={`banner ${run.status === 'skipped_limit' ? 'warn' : 'error'}`}>{problem}</div>}
      {(run.attempts ?? 0) > 1 && <p className="hint">1 回目がエラーになったため再試行しました（表示は再試行したときの内容です）。</p>}
      {run.notify_error && <p className="error-text">通知エラー: {run.notify_error}</p>}
      <div>
        <button className="link small" onClick={() => onAsk(run)}>
          この結果について質問する
        </button>
      </div>
    </article>
  )
}
