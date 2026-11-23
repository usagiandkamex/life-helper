export function LoadingIndicator({ label = '読み込み中…' }: { label?: string }) {
  return (
    <span className="loading-indicator" role="status">
      <span className="loading-spinner" aria-hidden="true" />
      {label}
    </span>
  )
}
