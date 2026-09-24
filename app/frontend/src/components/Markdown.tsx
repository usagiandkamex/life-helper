import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'

// react-markdown does not render raw HTML by default, which keeps model output and file contents inert.
export function Markdown({ text }: { text: string }) {
  return (
    <div className="markdown">
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        components={{
          a: ({ href, children }) => (
            <a href={href} target="_blank" rel="noopener noreferrer nofollow">
              {children}
            </a>
          ),
        }}
      >
        {text}
      </ReactMarkdown>
    </div>
  )
}

export function Disclaimer() {
  return (
    <p className="disclaimer">
      お金に関する結果は目安です。専門家（税理士・FP）の助言や投資助言ではありません。最終的な判断は公式の情報や専門家に確認してください。
    </p>
  )
}
