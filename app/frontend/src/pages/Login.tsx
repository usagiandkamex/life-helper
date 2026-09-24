import { api } from '../api'

export function LoginPage({
  devLogin,
  oauthConfigured,
  onSignedIn,
}: {
  devLogin: boolean
  oauthConfigured: boolean
  onSignedIn: () => void
}) {
  const dev = async () => {
    await api('/auth/dev-login', { method: 'POST' })
    onSignedIn()
  }
  return (
    <div className="login">
      <h1>Life Helper</h1>
      <p>あなた専用の生活サポートチャットです。許可された GitHub アカウントでログインしてください。</p>
      {oauthConfigured ? (
        <a className="button primary" href="/auth/login">
          GitHub でログイン
        </a>
      ) : (
        <p className="banner warn">
          GitHub OAuth App がまだ設定されていません。構築手順（docs/setup.md）のステップ 2-2 を実施してください。
        </p>
      )}
      {devLogin && (
        <button className="button" onClick={dev}>
          開発用ログイン（gh auth token）
        </button>
      )}
    </div>
  )
}
