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

export type Screenshot = { url: string }

export type ApprovalStatus = 'pending' | 'approved' | 'rejected' | 'expired' | 'cancelled'

export type TurnEvent =
  | { type: 'delta'; text: string }
  | { type: 'message'; content: string }
  | { type: 'tool_start'; id: string; name: string; args: string }
  | { type: 'tool_end'; id: string; success: boolean; error: string; result: string; chart?: ChartData; screenshot?: Screenshot }
  | { type: 'file_write'; path: string; diff: string; approval_id?: string | null }
  | { type: 'approval_request'; id: string; path: string; diff: string }
  | { type: 'approval_result'; id: string; status: Exclude<ApprovalStatus, 'pending'> }
  | { type: 'error'; message: string; code?: string }
  | { type: 'usage'; model: string }
  | { type: 'follow_up' }
  // A message sent while the chat answered: 'now' went into the answer in progress, 'later' waited its turn.
  | { type: 'user'; id: string; text: string; mode: 'now' | 'later' }
  | { type: 'queued'; id: string; text: string }
  // 'stopped': the answers ended (中断, timeout, error) before the message was sent.
  | { type: 'unqueued'; id: string; reason: 'cancelled' | 'stopped' }
  | { type: 'done' }
  | { type: 'end' }

export type HistoryMessage =
  | { role: 'user'; content: string }
  | { role: 'assistant'; content: string }
  | { role: 'tool'; name: string; args: string }

export type FileEntry = { path: string; size: number; modified: string; writable: boolean }

export type Price = {
  value: number
  date: string
  source: string
  market?: string | null
  symbol?: string | null
  local_currency?: string
  local_value?: number | null
  fx_rate?: number | null
  fx_date?: string | null
  fx_source?: string | null
  source_url?: string | null
  fetched_at?: string | null
}

export type FundRef = {
  provider: string
  fund_code: string
  manager: string
  isin: string | null
  association_code: string | null
  price_unit: number
  source_url: string | null
}

export type Holding = {
  id: string
  account: string
  account_label: string
  kind: string
  code: string
  name: string
  quantity: number
  cost_total: number
  cost_total_exact: number
  value: number | null
  gain: number | null
  price: Price | null
  fund: FundRef | null
  price_unit: number
  auto_nav: boolean
  stale: boolean
}

export type RefreshedPrice = {
  code: string
  name: string
  market: string | null
  symbol: string | null
  currency: string
  close: number | null
  close_jpy: number
  date: string
  fx_rate: number | null
  fx_date: string | null
}

export type RefreshedNav = {
  code: string
  name: string
  official_name: string
  nav: number
  price_unit: number
  date: string
  source: string
  source_url: string
}

export type FundProvider = { provider: string; label: string; manager: string; price_unit: number }

export type FundCandidate = {
  provider: string
  provider_label: string
  manager: string
  fund_code: string
  name: string
  nickname?: string
  isin: string | null
  association_code: string | null
  price_unit: number
  score: number
  exact?: boolean
  nav?: number | null
  date?: string | null
}

export type FundCandidates = {
  name: string
  candidates: FundCandidate[]
  errors: { code: string; error: string }[]
  note: string
}

export type FundAutoLink = {
  linked: { id: string; name: string; official_name: string; code: string }[]
  ambiguous: { id: string; name: string; reason: string }[]
  unmatched: { id: string; name: string }[]
  errors: { id: string; name: string; error: string }[]
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
  stale_prices: string[]
  manual_funds: { id: string; name: string }[]
  note: string
  brokers: { name: string; label: string }[]
  fund_providers: FundProvider[]
  updated_at: string | null
  imported?: number
  refresh?: { updated: RefreshedPrice[]; errors: { code: string; error: string }[]; note: string }
  refresh_funds?: {
    updated: RefreshedNav[]
    errors: { code: string; error: string }[]
    auto_link?: FundAutoLink
    manual: { id: string; name: string }[]
    note: string
  }
  link?: { ok: boolean; id: string; fund: FundRef; official_name: string | null }
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
  transcript_version?: number
  conversation_mode?: 'new' | 'continue'
  prompt?: string
  report?: { summary: string; notify: boolean } | null
  events_omitted?: number
  attempts?: number
  chat_thread_id?: string | null
}

// Finished automation runs shown in the chat, following the automation's conversation setting.
export type AutomationThread = {
  id: string
  mode: 'new' | 'continue'
  automation_id: string
  title: string
  updated_at: string
  latest_run_id: string
  latest_started_at: string
  latest_status: string | null
  unread: boolean
  run_count: number
}

export type AutomationThreadDetail = {
  thread: AutomationThread
  runs: RunRecord[]
  has_more: boolean
  has_newer: boolean
}

export type ConnectorStatus = { name: string; label: string; configured: boolean; last_used: string | null; cost: string }
