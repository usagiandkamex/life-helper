import { useEffect, useState } from 'react'
import { api, formatDate } from '../api'
import type { ConnectorStatus } from '../types'

export function SettingsPage({ login }: { login: string }) {
  const [connectors, setConnectors] = useState<ConnectorStatus[]>([])
  const [notify, setNotify] = useState<boolean | null>(null)

  useEffect(() => {
    api<ConnectorStatus[]>('/api/connectors').then(setConnectors).catch(() => undefined)
    api<{ github_notify_configured: boolean }>('/api/automations')
      .then((d) => setNotify(d.github_notify_configured))
      .catch(() => undefined)
  }, [])

  return (
    <div className="stack">
      <section className="panel">
        <h2>アカウント</h2>
        <p>
          ログイン中: <strong>{login}</strong>（Copilot の利用枠・課金はこの GitHub アカウントです）
        </p>
        <p className="hint">ログアウトすると、ほかの端末のログインも無効になります。オートメーションは保存済みのトークンで動き続けます。</p>
      </section>
      <section className="panel">
        <h2>外部サービス（コネクタ）</h2>
        <p className="hint">API キーは Azure のシークレットでだけ管理します。画面やチャットからは入力できません。</p>
        <table>
          <thead>
            <tr>
              <th>サービス</th>
              <th>キー</th>
              <th>最終利用</th>
              <th>費用</th>
            </tr>
          </thead>
          <tbody>
            {connectors.map((c) => (
              <tr key={c.name}>
                <td>{c.label}</td>
                <td>{c.configured ? '登録済み' : '未登録'}</td>
                <td>{formatDate(c.last_used)}</td>
                <td>{c.cost}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </section>
      <section className="panel">
        <h2>GitHub 通知</h2>
        <p>{notify === null ? '—' : notify ? '設定済み（GitHub App）' : '未設定'}</p>
      </section>
    </div>
  )
}
