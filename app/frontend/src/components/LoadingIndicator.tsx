export function LoadingIndicator({ label = 'Loading…' }: { label?: string }) {
  return (
    <div className="loading-indicator" role="status" aria-live="polite">
      <span className="loading-spinner" aria-hidden="true" />
      <span>{label}</span>
    </div>
  )
}
