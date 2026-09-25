import { useCallback, useEffect, useRef, useState } from 'react'
import { NavLink, Navigate, Route, Routes, useLocation } from 'react-router-dom'
import { api, ApiError, onAuthError, setCsrfToken } from './api'
import type { Me } from './types'
import { AutomationsPage } from './pages/Automations'
import { ChatPage } from './pages/Chat'
import { KnowledgePage } from './pages/Knowledge'
import { LoginPage } from './pages/Login'
import { PortfolioPage } from './pages/Portfolio'
import { SettingsPage } from './pages/Settings'

const NAV_ITEMS = [
  ['/chat', 'チャット'],
  ['/knowledge', '知識・メモリ'],
  ['/portfolio', '資産'],
  ['/automations', 'オートメーション'],
  ['/settings', '設定'],
] as const
// The drawer replaces the tabs at the same width the stylesheet switches to the mobile layout.
const MOBILE_QUERY = '(max-width: 760px)'

function NavItems({ unread, onNavigate }: { unread: number; onNavigate?: () => void }) {
  return (
    <>
      {NAV_ITEMS.map(([to, label]) => (
        <NavLink key={to} to={to} onClick={onNavigate}>
          {label}
          {to === '/automations' && unread > 0 && <span className="badge">{unread}</span>}
        </NavLink>
      ))}
    </>
  )
}

// A modal <dialog> keeps the keyboard inside the drawer and closes on Escape without extra handling.
function NavDrawer({ open, unread, onClose }: { open: boolean; unread: number; onClose: () => void }) {
  const ref = useRef<HTMLDialogElement>(null)
  useEffect(() => {
    const dialog = ref.current
    if (!dialog) return
    if (open && !dialog.open) dialog.showModal()
    else if (!open && dialog.open) dialog.close()
  }, [open])
  return (
    <dialog
      className="nav-drawer"
      ref={ref}
      aria-label="メニュー"
      onClose={onClose}
      onClick={(e) => {
        // Clicks on the scrim are dispatched to the dialog itself; the nav covers the drawer.
        if (e.target === ref.current) onClose()
      }}
    >
      <nav>
        <NavItems unread={unread} onNavigate={onClose} />
      </nav>
    </dialog>
  )
}

export default function App() {
  const [me, setMe] = useState<Me | null>(null)
  const [startupError, setStartupError] = useState('')
  const [reauth, setReauth] = useState(false)
  const [unread, setUnread] = useState(0)
  const [navOpen, setNavOpen] = useState(false)
  const location = useLocation()

  const loadMe = useCallback(async () => {
    setStartupError('')
    try {
      const data = await api<Me>('/api/me')
      if (data.authenticated) setCsrfToken(data.csrf_token)
      setMe(data)
    } catch (e) {
      // Only an explicit "not signed in" means logged out; network/server errors (e.g. a cold start) are retried.
      if (e instanceof ApiError && e.status === 401) setMe({ authenticated: false })
      else setStartupError(e instanceof Error ? e.message : String(e))
    }
  }, [])

  useEffect(() => {
    loadMe()
    return onAuthError((err) => {
      if (err.status === 401) setMe({ authenticated: false })
      else setReauth(true)
    })
  }, [loadMe])

  useEffect(() => {
    if (!me?.authenticated) return
    const refresh = () =>
      api<{ unread: number }>('/api/automations')
        .then((d) => setUnread(d.unread))
        .catch(() => undefined)
    refresh()
    const timer = window.setInterval(refresh, 60_000)
    return () => window.clearInterval(timer)
  }, [me])

  // Browser back/forward also leaves the drawer behind, and on a wide screen the tabs are back.
  useEffect(() => setNavOpen(false), [location])
  useEffect(() => {
    const mobile = window.matchMedia(MOBILE_QUERY)
    const sync = () => {
      if (!mobile.matches) setNavOpen(false)
    }
    mobile.addEventListener('change', sync)
    return () => mobile.removeEventListener('change', sync)
  }, [])

  if (startupError)
    return (
      <div className="center">
        <div className="login">
          <p>サーバーに接続できませんでした（起動中の場合があります）。</p>
          <p className="hint">{startupError}</p>
          <button className="button primary" onClick={loadMe}>
            再試行
          </button>
        </div>
      </div>
    )
  if (me === null) return <div className="center">読み込み中…</div>
  if (!me.authenticated)
    return <LoginPage devLogin={!!me.dev_login} oauthConfigured={me.oauth_configured !== false} onSignedIn={loadMe} />

  const logout = async () => {
    await api('/auth/logout', { method: 'POST' })
    setMe({ authenticated: false })
  }

  return (
    <div className="shell">
      <header className="topbar">
        <button
          className="icon-button nav-toggle"
          onClick={() => setNavOpen(true)}
          aria-label="メニューを開く"
          aria-expanded={navOpen}
          aria-haspopup="dialog"
        >
          <svg width="24" height="24" viewBox="0 0 24 24" aria-hidden="true" focusable="false">
            <path d="M3 6h18M3 12h18M3 18h18" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" />
          </svg>
        </button>
        <span className="brand">Life Helper</span>
        <nav aria-label="メインメニュー">
          <NavItems unread={unread} />
        </nav>
        <button className="link" onClick={logout} title={me.login}>
          ログアウト
        </button>
      </header>
      <NavDrawer open={navOpen} unread={unread} onClose={() => setNavOpen(false)} />
      {(reauth || !me.token_available) && (
        <div className="banner warn">
          GitHub への再ログインが必要です。<a href="/auth/login">ログインし直す</a>
        </div>
      )}
      <main className="content">
        <Routes>
          <Route path="/" element={<Navigate to="/chat" replace />} />
          <Route path="/chat" element={<ChatPage />} />
          <Route path="/knowledge" element={<KnowledgePage />} />
          <Route path="/portfolio" element={<PortfolioPage />} />
          <Route path="/automations" element={<AutomationsPage onUnreadChange={setUnread} />} />
          <Route path="/settings" element={<SettingsPage login={me.login} />} />
          <Route path="*" element={<Navigate to="/chat" replace />} />
        </Routes>
      </main>
    </div>
  )
}
