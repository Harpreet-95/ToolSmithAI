/**
 * ChartSectionV2 — Recharts-based renderers for bar | line | pie | donut.
 *
 * All other chart types (scatter, heatmap, correlation_matrix, forecast,
 * stacked_bar, grouped_line) continue to use the legacy SVG renderer in
 * ChartSection.jsx.  The public API is unchanged: ChartSection({ chart, C }).
 */

import {
  BarChart, Bar, XAxis, YAxis, CartesianGrid, Tooltip,
  ResponsiveContainer, Cell, LabelList,
  AreaChart, Area,
  PieChart, Pie,
} from 'recharts'

// Matches the palette in ChartSection.jsx
const PALETTE = [
  '#6366f1', '#8b5cf6', '#ec4899', '#f59e0b', '#10b981',
  '#3b82f6', '#ef4444', '#14b8a6', '#f97316', '#a855f7',
]

function fmtVal(v) {
  if (v == null || !isFinite(v)) return '—'
  if (v >= 1_000_000) return `${(v / 1_000_000).toFixed(1)}M`
  if (v >= 10_000)    return `${Math.round(v / 1_000)}k`
  if (v >= 1_000)     return `${(v / 1_000).toFixed(1)}k`
  return Number(v).toLocaleString()
}

// ─── Shared tooltip for bar + line ────────────────────────────────────────────
function ChartTooltip({ active, payload, label, C }) {
  if (!active || !payload?.length) return null
  return (
    <div style={{
      background: C.surface,
      border: `1px solid ${C.border}`,
      borderRadius: 6,
      padding: '8px 12px',
      fontSize: '0.78rem',
      boxShadow: '0 4px 16px rgba(0,0,0,0.18)',
      maxWidth: 240,
    }}>
      {label != null && (
        <div style={{
          color: C.text,
          fontWeight: 600,
          marginBottom: 5,
          paddingBottom: 5,
          borderBottom: `1px solid ${C.border}`,
        }}>{label}</div>
      )}
      {payload.map((p, i) => (
        <div key={i} style={{
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'space-between',
          gap: 12,
          marginTop: i > 0 ? 3 : 0,
        }}>
          <span style={{ display: 'flex', alignItems: 'center', gap: 5, color: C.textSec }}>
            <span style={{
              width: 8, height: 8, borderRadius: 2,
              background: p.color, display: 'inline-block', flexShrink: 0,
            }} />
            {p.name}
          </span>
          <span style={{ fontWeight: 600, color: C.text }}>{fmtVal(p.value)}</span>
        </div>
      ))}
    </div>
  )
}

// ─── Pie / donut tooltip ──────────────────────────────────────────────────────
function PieTooltip({ active, payload, C }) {
  if (!active || !payload?.length) return null
  const d   = payload[0]
  const pct = d.payload?.pct ?? 0
  const idx = d.payload?.index ?? 0
  return (
    <div style={{
      background: C.surface,
      border: `1px solid ${C.border}`,
      borderRadius: 6,
      padding: '8px 12px',
      fontSize: '0.78rem',
      boxShadow: '0 4px 16px rgba(0,0,0,0.18)',
    }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 6, marginBottom: 4 }}>
        <span style={{
          width: 8, height: 8, borderRadius: 2,
          background: PALETTE[idx % PALETTE.length], flexShrink: 0,
        }} />
        <span style={{ color: C.text, fontWeight: 600 }}>{d.name}</span>
      </div>
      <div style={{ color: C.text, paddingLeft: 14 }}>
        {fmtVal(d.value)}
        <span style={{ color: C.textMuted, marginLeft: 6 }}>
          ({(pct * 100).toFixed(1)}%)
        </span>
      </div>
    </div>
  )
}

// ─── Bar chart ────────────────────────────────────────────────────────────────
function RechartsBar({ labels, series, C }) {
  const firstSer = series[0] || {}
  const serName  = firstSer.name || 'Value'
  const nums     = (firstSer.data || []).map(v =>
    (typeof v === 'number' && isFinite(v) ? v : 0)
  )
  if (!nums.length) return (
    <div style={{ fontSize: '0.75rem', color: C.textMuted }}>No chart data available.</div>
  )

  const n           = labels.length
  const multiColor  = n <= 12
  const shouldRotate = labels.some(l => String(l).length > 7) || n > 9

  const data = labels.map((label, i) => ({
    label: String(label),
    [serName]: nums[i],
  }))

  return (
    <div>
      <ResponsiveContainer width="100%" height={280}>
        <BarChart
          data={data}
          margin={{ top: 24, right: 16, left: 0, bottom: 4 }}
        >
          <CartesianGrid strokeDasharray="3 3" stroke={C.border} vertical={false} />
          <XAxis
            dataKey="label"
            tick={{ fill: C.textMuted, fontSize: 11 }}
            angle={shouldRotate ? -42 : 0}
            textAnchor={shouldRotate ? 'end' : 'middle'}
            interval={0}
            height={shouldRotate ? 72 : 28}
            tickFormatter={v => {
              const s = String(v)
              return s.length > 12 ? s.slice(0, 11) + '…' : s
            }}
            axisLine={{ stroke: C.border }}
            tickLine={{ stroke: C.border }}
          />
          <YAxis
            tick={{ fill: C.textMuted, fontSize: 11 }}
            tickFormatter={fmtVal}
            width={50}
            axisLine={false}
            tickLine={false}
          />
          <Tooltip
            content={(props) => <ChartTooltip {...props} C={C} />}
            cursor={{ fill: C.border, opacity: 0.2 }}
          />
          <Bar
            dataKey={serName}
            fill={PALETTE[0]}
            opacity={0.88}
            radius={[3, 3, 0, 0]}
            maxBarSize={52}
          >
            {multiColor && data.map((_, i) => (
              <Cell key={i} fill={PALETTE[i % PALETTE.length]} />
            ))}
            {n <= 15 && (
              <LabelList
                dataKey={serName}
                position="top"
                formatter={fmtVal}
                style={{ fill: C.textSec, fontSize: 10 }}
              />
            )}
          </Bar>
        </BarChart>
      </ResponsiveContainer>
      {serName && serName !== 'Value' && (
        <div style={{ fontSize: '0.65rem', color: C.textMuted, textAlign: 'center', marginTop: 4 }}>
          {serName}
        </div>
      )}
    </div>
  )
}

// ─── Line chart ───────────────────────────────────────────────────────────────
function RechartsLine({ labels, series, C }) {
  const firstSer = series[0] || {}
  const serName  = firstSer.name || 'Value'
  const nums     = (firstSer.data || []).map(v =>
    (typeof v === 'number' && isFinite(v) ? v : null)
  )
  if (nums.filter(v => v != null).length < 2) return (
    <div style={{ fontSize: '0.75rem', color: C.textMuted }}>Not enough data for line chart.</div>
  )

  const n           = labels.length
  const shouldRotate = labels.some(l => String(l).length > 6) || n > 10
  // Show ~10 ticks max on dense time series
  const tickInterval = n <= 12 ? 0 : Math.floor(n / 10)

  const data = labels.map((label, i) => ({
    label: String(label),
    [serName]: nums[i],
  }))

  return (
    <div>
      <ResponsiveContainer width="100%" height={280}>
        <AreaChart
          data={data}
          margin={{ top: 24, right: 16, left: 0, bottom: 4 }}
        >
          <defs>
            <linearGradient id="rechartsLineGrad" x1="0" y1="0" x2="0" y2="1">
              <stop offset="5%"  stopColor="#6366f1" stopOpacity={0.25} />
              <stop offset="90%" stopColor="#6366f1" stopOpacity={0.02} />
            </linearGradient>
          </defs>
          <CartesianGrid strokeDasharray="3 3" stroke={C.border} vertical={false} />
          <XAxis
            dataKey="label"
            tick={{ fill: C.textMuted, fontSize: 11 }}
            angle={shouldRotate ? -42 : 0}
            textAnchor={shouldRotate ? 'end' : 'middle'}
            interval={tickInterval}
            height={shouldRotate ? 72 : 28}
            tickFormatter={v => {
              const s = String(v)
              return s.length > 12 ? s.slice(0, 11) + '…' : s
            }}
            axisLine={{ stroke: C.border }}
            tickLine={{ stroke: C.border }}
          />
          <YAxis
            tick={{ fill: C.textMuted, fontSize: 11 }}
            tickFormatter={fmtVal}
            width={50}
            axisLine={false}
            tickLine={false}
          />
          <Tooltip
            content={(props) => <ChartTooltip {...props} C={C} />}
            cursor={{ stroke: C.border, strokeWidth: 1, strokeDasharray: '3 3' }}
          />
          <Area
            type="monotone"
            dataKey={serName}
            stroke="#6366f1"
            strokeWidth={2.2}
            fill="url(#rechartsLineGrad)"
            dot={{ r: 3.5, fill: '#6366f1', stroke: C.surface, strokeWidth: 1.5 }}
            activeDot={{ r: 5, fill: '#6366f1', stroke: C.surface, strokeWidth: 2 }}
            connectNulls={false}
          />
        </AreaChart>
      </ResponsiveContainer>
      {serName && serName !== 'Value' && (
        <div style={{ fontSize: '0.65rem', color: C.textMuted, textAlign: 'center', marginTop: 4 }}>
          {serName}
        </div>
      )}
    </div>
  )
}

// ─── Pie / Donut chart ────────────────────────────────────────────────────────
function RechartsPieDonut({ labels, series, C, isDonut }) {
  const firstSer = series[0] || {}
  const serName  = firstSer.name || ''
  const rawNums  = (firstSer.data || []).map(v =>
    Math.abs(typeof v === 'number' && isFinite(v) ? v : 0)
  )
  const total = rawNums.reduce((a, b) => a + b, 0)
  if (!total) return (
    <div style={{ fontSize: '0.75rem', color: C.textMuted }}>No chart data available.</div>
  )

  const limit = Math.min(labels.length, 8)
  const data  = labels.slice(0, limit).map((label, i) => ({
    name:  String(label),
    value: rawNums[i] || 0,
    pct:   (rawNums[i] || 0) / total,
    index: i,
  }))

  // Custom legend: colour swatch + label + value + pct
  const renderLegend = () => (
    <div style={{
      display: 'flex',
      flexWrap: 'wrap',
      gap: '5px 14px',
      justifyContent: 'center',
      padding: '8px 0 2px',
    }}>
      {data.map((entry, i) => (
        <div key={i} style={{ display: 'flex', alignItems: 'center', gap: 5 }}>
          <div style={{
            width: 8, height: 8, borderRadius: 2,
            background: PALETTE[i % PALETTE.length], flexShrink: 0,
          }} />
          <span style={{ fontSize: '0.67rem', color: C.textSec }}>
            {entry.name.length > 16 ? entry.name.slice(0, 15) + '…' : entry.name}
          </span>
          <span style={{ fontSize: '0.67rem', color: C.textMuted }}>
            {fmtVal(entry.value)} ({(entry.pct * 100).toFixed(0)}%)
          </span>
        </div>
      ))}
    </div>
  )

  return (
    <div style={{ display: 'flex', flexDirection: 'column', alignItems: 'center' }}>
      {/* pie/donut SVG — fixed height so the center overlay can use % positioning */}
      <div style={{ position: 'relative', width: '100%', height: 220 }}>
        <ResponsiveContainer width="100%" height={220}>
          <PieChart>
            <Pie
              data={data}
              cx="50%"
              cy="50%"
              innerRadius={isDonut ? '36%' : 0}
              outerRadius="60%"
              dataKey="value"
              strokeWidth={0}
            >
              {data.map((_, i) => (
                <Cell key={i} fill={PALETTE[i % PALETTE.length]} opacity={0.88} />
              ))}
            </Pie>
            <Tooltip content={(props) => <PieTooltip {...props} C={C} />} />
          </PieChart>
        </ResponsiveContainer>

        {/* Donut center: total value + "total" label */}
        {isDonut && (
          <div style={{
            position: 'absolute',
            top: '50%',
            left: '50%',
            transform: 'translate(-50%, -50%)',
            textAlign: 'center',
            pointerEvents: 'none',
            lineHeight: 1.3,
          }}>
            <div style={{ fontSize: '1rem', fontWeight: 700, color: C.text }}>
              {fmtVal(total)}
            </div>
            <div style={{ fontSize: '0.6rem', color: C.textMuted }}>total</div>
          </div>
        )}
      </div>

      {renderLegend()}

      {serName && (
        <div style={{ fontSize: '0.65rem', color: C.textMuted, marginTop: 4 }}>
          {serName}
        </div>
      )}
    </div>
  )
}

// ─── Public entry point ───────────────────────────────────────────────────────
export default function ChartSectionV2({ chart, C, chartType }) {
  const labels = chart?.labels || []
  const series = chart?.series || []

  if (chartType === 'bar')   return <RechartsBar      labels={labels} series={series} C={C} />
  if (chartType === 'line')  return <RechartsLine     labels={labels} series={series} C={C} />
  if (chartType === 'pie')   return <RechartsPieDonut labels={labels} series={series} C={C} isDonut={false} />
  if (chartType === 'donut') return <RechartsPieDonut labels={labels} series={series} C={C} isDonut={true} />
  return null
}
