import { lazy, Suspense } from 'react'
import type { ChartData } from '../types'

// recharts is large, so it is only downloaded when a chart is actually shown.
const ChartView = lazy(() => import('./ChartView').then((m) => ({ default: m.ChartView })))

export function LazyChart({ chart }: { chart: ChartData }) {
  return (
    <Suspense fallback={<p className="hint">グラフを読み込み中…</p>}>
      <ChartView chart={chart} />
    </Suspense>
  )
}
