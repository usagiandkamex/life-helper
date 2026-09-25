let csrfToken = ''

export function setCsrfToken(token: string) {
  csrfToken = token
}

export class ApiError extends Error {
  status: number
  detail: unknown
  constructor(status: number, detail: unknown) {
    super(typeof detail === 'string' ? detail : (detail as { message?: string })?.message ?? `HTTP ${status}`)
    this.status = status
    this.detail = detail
  }
  get code(): string | undefined {
    return typeof this.detail === 'object' && this.detail ? (this.detail as { code?: string }).code : undefined
  }
}

type Listener = (error: ApiError) => void
const listeners = new Set<Listener>()
export function onAuthError(listener: Listener): () => void {
  listeners.add(listener)
  return () => {
    listeners.delete(listener)
  }
}

export async function api<T>(path: string, init: RequestInit = {}): Promise<T> {
  const method = (init.method ?? 'GET').toUpperCase()
  const headers = new Headers(init.headers)
  if (method !== 'GET' && method !== 'HEAD') headers.set('X-CSRF-Token', csrfToken)
  if (init.body && !(init.body instanceof FormData) && !headers.has('Content-Type')) {
    headers.set('Content-Type', 'application/json')
  }
  const resp = await fetch(path, { ...init, headers, credentials: 'same-origin' })
  if (!resp.ok) {
    let detail: unknown = resp.statusText
    try {
      detail = (await resp.json()).detail
    } catch {
      /* non-JSON error body */
    }
    const error = new ApiError(resp.status, detail)
    if (resp.status === 401 || error.code === 'reauth') listeners.forEach((l) => l(error))
    throw error
  }
  const type = resp.headers.get('Content-Type') ?? ''
  return (type.includes('application/json') ? resp.json() : resp.text()) as Promise<T>
}

export const json = (body: unknown) => JSON.stringify(body)

export function yen(value: number | null | undefined): string {
  if (value === null || value === undefined) return '—'
  // 数字と「円」の間は改行しないように nbsp でつなぐ。
  return `${Math.round(value).toLocaleString('ja-JP')}\u00a0円`
}

export function formatDate(iso: string | null | undefined): string {
  if (!iso) return '—'
  return new Date(iso).toLocaleString('ja-JP', { dateStyle: 'short', timeStyle: 'short' })
}
