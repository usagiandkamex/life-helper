import { useCallback, useEffect, useRef, useState } from 'react'
import { NavLink, Navigate, Route, Routes } from 'react-router-dom'
import { api, ApiError, onAuthError, setCsrfToken } from './api'
import type { Me } from './types'
import { AutomationsPage } from './pages/Automations'
import { ChatPage } from './pages/Chat'
import { KnowledgePage } from './pages/Knowledge'
import { LoginPage } from './pages/Login'
import { PortfolioPage } from './pages/Portfolio'
import { SettingsPage } from './pages/Settings'

export default function App() {
  const [me, setMe] = useState<Me | null>(null)
  const [startupError, setStartupError] = useState('')
  const [reauth, setReauth] = useState(false)
  const [unread, setUnread] = useState(0)
  // Pages report the count after marking runs read; a poll that started before that must not undo it.
  const unreadVersion = useRef(0)
  const updateUnread = useCallback((n: number) => {
    unreadVersion.current += 1
    setUnread(n)
  }, [])

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
    const refresh = () => {
      // Started polls and page updates share one counter: only the latest of them sets the count.
      const version = ++unreadVersion.current
      api<{ unread: number }>('/api/automations')
        .then((d) => {
          if (version === unreadVersion.current) setUnread(d.unread)
        })
        .catch(() => undefined)
    }
    refresh()
    const timer = window.setInterval(refresh, 60_000)
    return () => window.clearInterval(timer)
  }, [me])

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
        <span className="brand">Life Helper</span>
        <nav>
          <NavLink to="/chat">チャット</NavLink>
          <NavLink to="/knowledge">知識・メモリ</NavLink>
          <NavLink to="/portfolio">資産</NavLink>
          <NavLink to="/automations">
            オートメーション{unread > 0 && <span className="badge">{unread}</span>}
          </NavLink>
          <NavLink to="/settings">設定</NavLink>
        </nav>
        <button className="link" onClick={logout} title={me.login}>
          ログアウト
        </button>
      </header>
      {(reauth || !me.token_available) && (
        <div className="banner warn">
          GitHub への再ログインが必要です。<a href="/auth/login">ログインし直す</a>
        </div>
      )}
      <main className="content">
        <Routes>
          <Route path="/" element={<Navigate to="/chat" replace />} />
          <Route path="/chat" element={<ChatPage onUnreadChange={updateUnread} />} />
          <Route path="/knowledge" element={<KnowledgePage />} />
          <Route path="/portfolio" element={<PortfolioPage />} />
          <Route path="/automations" element={<AutomationsPage onUnreadChange={updateUnread} />} />
          <Route path="/settings" element={<SettingsPage login={me.login} />} />
          <Route path="*" element={<Navigate to="/chat" replace />} />
        </Routes>
      </main>
    </div>
  )
}
