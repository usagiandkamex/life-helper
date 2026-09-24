export type Me =
  | { authenticated: false; dev_login?: boolean; oauth_configured?: boolean }
  | { authenticated: true; login: string; csrf_token: string; token_available: boolean; environment: string }

export type Conversation = {
  id: string
  title: string
  model: string
  created_at: string
  updated_at: string
  started: boolean
  busy: boolean
}

export type ChartData = { type: string; x: string; series: string[]; data: Record<string, number | null>[] }

export type TurnEvent =
  | { type: 'delta'; text: string }
  | { type: 'message'; content: string }
  | { type: 'tool_start'; id: string; name: string; args: string }
  | { type: 'tool_end'; id: string; success: boolean; error: string; result: string; chart?: ChartData }
  | { type: 'file_write'; path: string; diff: string }
  | { type: 'error'; message: string; code?: string }
  | { type: 'usage'; model: string }
  | { type: 'done' }
  | { type: 'end' }

export type HistoryMessage =
  | { role: 'user'; content: string }
  | { role: 'assistant'; content: string }
  | { role: 'tool'; name: string; args: string }

export type FileEntry = { path: string; size: number; modified: string; writable: boolean }

export type Holding = {
  id: string
  account: string
  account_label: string
  kind: string
  code: string
  name: string
  quantity: number
  cost_total: number
  value: number | null
  gain: number | null
  price: { value: number; date: string; source: string } | null
}

export type PortfolioView = {
  holdings: Holding[]
  accounts: Record<string, { label: string; value: number; cost: number }>
  allocation: Record<string, number>
  total_value: number
  total_cost: number
  total_gain: number
  oldest_price_date: string | null
  missing_prices: string[]
  note: string
  nisa: {
    year: number
    annual: Record<'tsumitate' | 'growth', { limit: number; used: number }>
    annual_remaining: Record<'tsumitate' | 'growth', number>
    lifetime: { limit: number; used_book_value: number; remaining: number; growth_remaining: number }
    notes: string[]
    warnings: string[]
  }
  brokers: { name: string; label: string }[]
  updated_at: string | null
  imported?: number
  refresh?: { updated: { code: string; name: string; close: number; date: string }[]; errors: { code: string; error: string }[]; note: string }
}

export type Schedule = {
  kind: 'daily' | 'weekly' | 'monthly' | 'yearly' | 'cron'
  time: string
  weekday: number
  day: number
  month: number
  cron: string
}

export type NotifySettings = {
  github: boolean
  condition: 'always' | 'report' | 'signal'
  signal_field: string
  signal_op: '>' | '>=' | '==' | '!=' | '<' | '<='
  signal_value: number
  only_on_change: boolean
  include_summary: boolean
}

export type Automation = {
  id: string
  name: string
  enabled: boolean
  prompt: string
  schedule: Schedule
  conversation_mode: 'new' | 'continue'
  model: string
  allow_write: boolean
  connectors: string[]
  notify: NotifySettings
  max_runtime_minutes: number
  state: { next_run_at: string | null; last_run_at: string | null; last_status: string | null; last_condition_met: boolean | null }
  estimated_runs_per_month: number
  cron: string
}

export type AutomationList = {
  automations: Automation[]
  usage: { runs_this_month: number; monthly_limit: number; estimated_runs_per_month: number }
  unread: number
  github_notify_configured: boolean
  connectors: { name: string; label: string; configured: boolean }[]
}

export type RunRecord = {
  id: string
  automation_id: string
  name: string
  started_at: string
  finished_at?: string
  status: string
  summary?: string
  error?: string | null
  final_message?: string
  signals?: Record<string, number>
  notified: boolean
  notify_error?: string
  issue_url?: string
  read: boolean
  requests?: number
  events?: TurnEvent[]
}

export type ConnectorStatus = { name: string; label: string; configured: boolean; last_used: string | null; cost: string }
