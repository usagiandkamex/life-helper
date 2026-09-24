import { CartesianGrid, Legend, Line, LineChart, ResponsiveContainer, Tooltip, XAxis, YAxis } from 'recharts'
import type { ChartData } from '../types'

const LABELS: Record<string, string> = {
  cash: '預貯金',
  investment: '投資',
  total_assets: '資産合計',
  principal: '元本',
  value: '評価額',
}
const COLORS = ['#2563eb', '#16a34a', '#dc2626', '#9333ea']

export function ChartView({ chart }: { chart: ChartData }) {
  const title = chart.type === 'investment' ? '積立シミュレーション' : 'ライフプラン（資産推移）'
  return (
    <figure className="chart">
      <figcaption>{title}</figcaption>
      <ResponsiveContainer width="100%" height={260}>
        <LineChart data={chart.data} margin={{ top: 8, right: 16, left: 8, bottom: 8 }}>
          <CartesianGrid strokeDasharray="3 3" />
          <XAxis dataKey={chart.x} tickFormatter={(v) => `${v}${chart.x === 'age' ? '歳' : '年'}`} />
          <YAxis tickFormatter={(v: number) => `${Math.round(v / 10000).toLocaleString('ja-JP')}万`} width={70} />
          <Tooltip formatter={(v) => `${Math.round(Number(v)).toLocaleString('ja-JP')} 円`} />
          <Legend formatter={(key) => LABELS[String(key)] ?? key} />
          {chart.series.map((s, i) => (
            <Line key={s} type="monotone" dataKey={s} name={s} stroke={COLORS[i % COLORS.length]} dot={false} />
          ))}
        </LineChart>
      </ResponsiveContainer>
    </figure>
  )
}
