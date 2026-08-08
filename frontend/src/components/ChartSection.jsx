/**
 * ChartSection — pure-SVG chart renderers, zero external dependencies.
 *
 * Supported chart_type values:
 *   bar | line | pie | donut          — v1, unchanged
 *   bar_horizontal                     — v3: same BarChart, forced HBarChart layout
 *   scatter                            — v2: {x_label, y_label, points:[{x,y}]}
 *   heatmap                            — v2: {x_labels, y_labels, values:[][]}
 *   correlation_matrix                 — v2: {columns, matrix:[][]}
 *
 * Chart recommendation:
 *   recommendChartType(chart) → inferred best chart_type string
 */

const FONT = "system-ui, -apple-system, 'Segoe UI', sans-serif"
const MONO = "'Cascadia Code', 'Fira Code', monospace"

// Categorical colour palette (cycles)
const PALETTE = [
  '#6366f1', '#8b5cf6', '#ec4899', '#f59e0b', '#10b981',
  '#3b82f6', '#ef4444', '#14b8a6', '#f97316', '#a855f7',
]

// ─── Helpers ─────────────────────────────────────────────────────────────────

function fmtVal(v) {
  if (v == null || !isFinite(v)) return '—'
  if (v >= 1_000_000) return `${(v / 1_000_000).toFixed(1)}M`
  if (v >= 10_000)    return `${Math.round(v / 1_000)}k`
  if (v >= 1_000)     return `${(v / 1_000).toFixed(1)}k`
  return Number(v).toLocaleString()
}

// Map a correlation value [-1, 1] to an rgba fill colour.
// Positive → indigo scale; Negative → red-orange scale; 0 → transparent.
function corrFill(v) {
  const clamped = Math.max(-1, Math.min(1, v ?? 0))
  const abs = Math.abs(clamped)
  if (clamped >= 0) return `rgba(99,102,241,${(abs * 0.82).toFixed(2)})`   // indigo
  return               `rgba(239,68,68,${(abs * 0.82).toFixed(2)})`         // red
}

// Readable text colour over a correlation cell
function corrText(v, C) {
  return Math.abs(v ?? 0) > 0.55 ? '#fff' : C.text
}

// ─── Lightweight chart-type recommendation ───────────────────────────────────
/**
 * Infer the best chart_type for a given chart data object.
 *
 * Rules:
 *   - Explicit chart_type (not 'auto') → respected as-is
 *   - Schema detection: points → scatter; matrix/columns → correlation_matrix;
 *     x_labels+values → heatmap
 *   - Time-series labels (month names / YYYY-MM patterns, n ≥ 4) → line
 *   - Numeric bin labels (all parseable floats, n ≥ 5) → bar (histogram style)
 *   - Few categories (2–7) → donut
 *   - Default → bar
 *
 * @param {object} chart – the chart sub-object from a section
 * @returns {string} recommended chart_type
 */
export function recommendChartType(chart) {
  const explicit = chart?.chart_type
  if (explicit && explicit !== 'auto') return explicit

  // Schema-shape detection
  if (chart?.historical)                          return 'forecast'
  if (chart?.points)                              return 'scatter'
  if (chart?.matrix || chart?.columns)            return 'correlation_matrix'
  if (chart?.x_labels && chart?.values)           return 'heatmap'

  const labels = chart?.labels || []
  const series = chart?.series || []
  const data   = (series[0]?.data || []).filter(v => typeof v === 'number' && isFinite(v))
  const n      = labels.length

  if (n === 0) return 'bar'

  // Time-series: month abbreviations or YYYY-MM date patterns
  const TIME_RE = /^(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec|\d{4}[-/]\d{2})/i
  if (n >= 4 && labels.some(l => TIME_RE.test(String(l)))) return 'line'

  // Distribution bins: all labels parse as finite floats
  const allNumericLabels = n >= 5 && labels.every(l => !isNaN(parseFloat(String(l))) && isFinite(parseFloat(String(l))))
  if (allNumericLabels) return 'bar'

  // Few meaningful categories → donut feels right
  if (n >= 2 && n <= 7 && data.length > 0) return 'donut'

  return 'bar'
}

// ─── Fallback: plain value table ─────────────────────────────────────────────
function FallbackTable({ labels, series, C }) {
  const serData = (series[0] || {}).data || []
  const name    = (series[0] || {}).name || ''
  return (
    <div>
      <div style={{ display: 'flex', flexDirection: 'column', gap: '3px' }}>
        {labels.map((lbl, i) => (
          <div key={i} style={{
            display: 'flex', justifyContent: 'space-between',
            fontSize: '0.73rem', padding: '4px 0',
            borderBottom: `1px solid ${C.border}`,
          }}>
            <span style={{ color: C.text, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', maxWidth: '68%' }}>
              {lbl}
            </span>
            <span style={{ color: C.textSec, fontWeight: '500' }}>
              {serData[i] != null ? Number(serData[i]).toLocaleString() : '—'}
            </span>
          </div>
        ))}
      </div>
      {name && <div style={{ fontSize: '0.65rem', color: C.textMuted, marginTop: '8px', textAlign: 'center' }}>{name}</div>}
    </div>
  )
}

// ─── Bar chart ────────────────────────────────────────────────────────────────

// Horizontal bar — used when labels are long or chart is dense (> 12 bars).
// Labels sit on the left, bars grow right, values appear after the bar.
function HBarChart({ labels, nums, maxVal, name, C }) {
  const n          = labels.length
  const LBL_W      = 130, VAL_W = 52, TOP = 10
  const BAR_H      = 20,  GAP   = 7
  const SVG_W      = 520, CHART_W = SVG_W - LBL_W - VAL_W
  const SVG_H      = TOP + n * (BAR_H + GAP) + 4
  const multiColor = n <= 12

  return (
    <div style={{ overflowX: 'auto' }}>
      <svg viewBox={`0 0 ${SVG_W} ${SVG_H}`} width="100%"
        style={{ display: 'block', minWidth: '280px', maxWidth: `${SVG_W}px` }}
        aria-label={name || 'bar chart'}>
        {labels.map((label, i) => {
          const val   = nums[i]
          const barW  = Math.max(2, Math.round((val / maxVal) * CHART_W * 0.95))
          const y     = TOP + i * (BAR_H + GAP)
          const color = multiColor ? PALETTE[i % PALETTE.length] : '#6366f1'
          const short = label.length > 20 ? label.slice(0, 19) + '…' : label
          return (
            <g key={i}>
              <text x={LBL_W - 8} y={y + BAR_H / 2 + 4} textAnchor="end"
                fontSize={8} fill={C.text} fontFamily={FONT}>{short}</text>
              <rect x={LBL_W} y={y} width={barW} height={BAR_H} fill={color} rx={3} opacity={0.86}>
                <title>{label}: {val.toLocaleString()}</title>
              </rect>
              <text x={LBL_W + barW + 5} y={y + BAR_H / 2 + 4} textAnchor="start"
                fontSize={8} fill={C.textSec} fontFamily={FONT} fontWeight="600">{fmtVal(val)}</text>
            </g>
          )
        })}
      </svg>
      {name && <div style={{ fontSize: '0.65rem', color: C.textMuted, textAlign: 'center', marginTop: '4px' }}>{name}</div>}
    </div>
  )
}

function BarChart({ labels, series, C, horizontal = false }) {
  const firstSer = series[0] || {}
  const nums     = (firstSer.data || []).map(v => (typeof v === 'number' && isFinite(v) ? v : 0))
  const name     = firstSer.name || ''
  if (!nums.length) return null

  // Sort descending by value so the tallest bar is always on the left
  const pairs        = labels.map((lbl, i) => ({ lbl: String(lbl), val: nums[i] }))
    .sort((a, b) => b.val - a.val)
  const sortedLabels = pairs.map(p => p.lbl)
  const sortedNums   = pairs.map(p => p.val)

  const n      = sortedLabels.length
  const maxVal = Math.max(...sortedNums, 1)

  // Day 4, Capability 3 — an explicit chart_type of "bar_horizontal" (a
  // ranked/top-N result) always uses the horizontal layout, in addition to
  // the pre-existing long-labels/dense-chart auto-switch below.
  if (horizontal || sortedLabels.some(l => l.length > 10) || n > 12) {
    return <HBarChart labels={sortedLabels} nums={sortedNums} maxVal={maxVal} name={name} C={C} />
  }

  // ── Vertical bar chart ──────────────────────────────────────────────────────
  const LEFT = 42, RIGHT = 10, TOP = 18, CH = 180, LBL_H = 52
  const SVG_W = 520, SVG_H = TOP + CH + LBL_H
  const CHART_W = SVG_W - LEFT - RIGHT, BASE_Y = TOP + CH
  const BAR_W = Math.max(10, Math.min(48, Math.floor(CHART_W / n * 0.7)))
  const gap   = (CHART_W - n * BAR_W) / (n + 1)
  const ticks = [0, 0.25, 0.5, 0.75, 1].map(f => ({
    y: TOP + CH - f * CH, val: Math.round(f * maxVal),
  }))
  const multiColor = n <= 12

  return (
    <div style={{ overflowX: 'auto' }}>
      <svg viewBox={`0 0 ${SVG_W} ${SVG_H}`} width="100%"
        style={{ display: 'block', minWidth: `${Math.min(SVG_W, 200)}px`, maxWidth: `${SVG_W}px` }}
        aria-label={name || 'bar chart'}>
        {ticks.map(({ y, val }, gi) => (
          <g key={gi}>
            <line x1={LEFT} y1={y} x2={SVG_W - RIGHT} y2={y}
              stroke={C.border} strokeWidth={gi === 0 ? 1.2 : 0.6}
              strokeDasharray={gi === 0 ? 'none' : '3 3'} />
            <text x={LEFT - 4} y={y + 3.5} textAnchor="end"
              fontSize={7} fill={C.textMuted} fontFamily={FONT}>{fmtVal(val)}</text>
          </g>
        ))}
        {sortedLabels.map((label, i) => {
          const val    = sortedNums[i]
          const barH   = Math.max(2, Math.round((val / maxVal) * CH * 0.96))
          const x      = LEFT + gap + i * (BAR_W + gap)
          const y      = BASE_Y - barH
          const color  = multiColor ? PALETTE[i % PALETTE.length] : '#6366f1'
          const short  = label.length > 11 ? label.slice(0, 10) + '…' : label
          const rotate = label.length > 7 || n > 9
          return (
            <g key={i}>
              <rect x={x} y={y} width={BAR_W} height={barH} fill={color} rx={3} opacity={0.86}>
                <title>{label}: {val.toLocaleString()}</title>
              </rect>
              {/* Value label — always visible, clearer font */}
              <text x={x + BAR_W / 2} y={y - 5} textAnchor="middle"
                fontSize={8} fill={C.textSec} fontFamily={FONT} fontWeight="500">{fmtVal(val)}</text>
              {rotate ? (
                <text x={x + BAR_W / 2} y={BASE_Y + 10} textAnchor="end"
                  fontSize={7} fill={C.textMuted} fontFamily={FONT}
                  transform={`rotate(-42,${x + BAR_W / 2},${BASE_Y + 10})`}>{short}</text>
              ) : (
                <text x={x + BAR_W / 2} y={BASE_Y + 14} textAnchor="middle"
                  fontSize={8} fill={C.textMuted} fontFamily={FONT}>{short}</text>
              )}
            </g>
          )
        })}
      </svg>
      {name && <div style={{ fontSize: '0.65rem', color: C.textMuted, textAlign: 'center', marginTop: '4px' }}>{name}</div>}
    </div>
  )
}

// ─── Line chart ───────────────────────────────────────────────────────────────
function LineChart({ labels, series, C }) {
  const firstSer = series[0] || {}
  const nums     = (firstSer.data || []).map(v => (typeof v === 'number' && isFinite(v) ? v : 0))
  const name     = firstSer.name || ''
  if (nums.length < 2) return <FallbackTable labels={labels} series={series} C={C} />

  const n = labels.length, maxVal = Math.max(...nums, 1)
  const minVal = Math.min(...nums, 0), range = maxVal - minVal || 1

  // ── Trend annotation ────────────────────────────────────────────────────────
  const startVal = nums[0]
  const endVal   = nums[n - 1]
  const rawPct   = startVal !== 0 ? ((endVal - startVal) / Math.abs(startVal)) * 100 : null
  const pctStr   = rawPct != null ? `${rawPct >= 0 ? '+' : ''}${rawPct.toFixed(1)}%` : null
  const pctColor = rawPct == null ? C.textMuted : rawPct > 0 ? '#10b981' : rawPct < 0 ? '#ef4444' : C.textMuted
  const pctIcon  = rawPct == null ? '→' : rawPct > 0 ? '↑' : rawPct < 0 ? '↓' : '→'

  // ── Min / max indices ────────────────────────────────────────────────────────
  const maxIdx = nums.indexOf(Math.max(...nums))
  const minIdx = nums.indexOf(Math.min(...nums))

  // Extra TOP headroom so max-point callout text stays inside the SVG
  const LEFT = 42, RIGHT = 16, TOP = 30, CH = 160, LBL_H = 48
  const SVG_W = 520, SVG_H = TOP + CH + LBL_H
  const CHART_W = SVG_W - LEFT - RIGHT, BASE_Y = TOP + CH
  const xStep = CHART_W / (n - 1)

  const pts = nums.map((val, i) => ({
    x: LEFT + i * xStep,
    y: TOP + CH * 0.96 - ((val - minVal) / range) * CH * 0.92,
    val,
  }))
  const linePts = pts.map(p => `${p.x.toFixed(1)},${p.y.toFixed(1)}`).join(' ')
  const areaPts = `${pts[0].x.toFixed(1)},${BASE_Y} ${linePts} ${pts[n-1].x.toFixed(1)},${BASE_Y}`
  const ticks = [0, 0.25, 0.5, 0.75, 1].map(f => ({
    val: Math.round(minVal + f * range), y: TOP + CH * 0.96 - f * CH * 0.92,
  }))

  return (
    <div>
      {/* Trend summary: start → end  ↑/↓ pct% */}
      <div style={{
        display: 'flex', justifyContent: 'space-between', alignItems: 'center',
        padding: '4px 10px', marginBottom: '5px',
        background: C.surface, border: `1px solid ${C.border}`, borderRadius: '5px',
        fontSize: '0.71rem',
      }}>
        <span style={{ color: C.textMuted }}>
          Start&nbsp;<span style={{ color: C.text, fontWeight: 600 }}>{fmtVal(startVal)}</span>
        </span>
        <span style={{ color: pctColor, fontWeight: 700, fontSize: '0.8rem' }}>
          {pctIcon} {pctStr ?? '—'}
        </span>
        <span style={{ color: C.textMuted }}>
          End&nbsp;<span style={{ color: C.text, fontWeight: 600 }}>{fmtVal(endVal)}</span>
        </span>
      </div>

      <div style={{ overflowX: 'auto' }}>
        <svg viewBox={`0 0 ${SVG_W} ${SVG_H}`} width="100%"
          style={{ display: 'block', minWidth: `${Math.min(SVG_W, 200)}px`, maxWidth: `${SVG_W}px` }}
          aria-label={name || 'line chart'}>
          {ticks.map(({ val, y }, gi) => (
            <g key={gi}>
              <line x1={LEFT} y1={y} x2={SVG_W - RIGHT} y2={y}
                stroke={C.border} strokeWidth={0.7} strokeDasharray="3 3" />
              <text x={LEFT - 4} y={y + 3.5} textAnchor="end"
                fontSize={7} fill={C.textMuted} fontFamily={FONT}>{fmtVal(val)}</text>
            </g>
          ))}
          <line x1={LEFT} y1={BASE_Y} x2={SVG_W - RIGHT} y2={BASE_Y} stroke={C.border} strokeWidth={1} />
          <polygon points={areaPts} fill="#6366f1" fillOpacity="0.10" />
          <polyline points={linePts} fill="none" stroke="#6366f1" strokeWidth={2.2}
            strokeLinecap="round" strokeLinejoin="round" />
          {pts.map((p, i) => {
            const isMax  = i === maxIdx
            const isMin  = i === minIdx
            const r      = (isMax || isMin) ? 5 : 3.5
            const fill   = isMax ? '#10b981' : isMin ? '#ef4444' : '#6366f1'
            // Clamp callout labels to remain inside the SVG viewport
            const anchor    = p.x < LEFT + 36 ? 'start' : p.x > SVG_W - RIGHT - 36 ? 'end' : 'middle'
            const maxLblY   = Math.max(TOP + 9, p.y - 9)
            const minLblY   = Math.min(BASE_Y - 4, p.y + 14)
            return (
              <g key={i}>
                <title>{labels[i]}: {p.val.toLocaleString()}</title>
                <circle cx={p.x} cy={p.y} r={r} fill={fill} stroke={C.surface} strokeWidth={1.5} />
                {isMax && (
                  <text x={p.x} y={maxLblY} textAnchor={anchor}
                    fontSize={7.5} fill="#10b981" fontFamily={FONT} fontWeight="700">
                    ↑ {fmtVal(p.val)}
                  </text>
                )}
                {isMin && !isMax && (
                  <text x={p.x} y={minLblY} textAnchor={anchor}
                    fontSize={7.5} fill="#ef4444" fontFamily={FONT} fontWeight="700">
                    ↓ {fmtVal(p.val)}
                  </text>
                )}
              </g>
            )
          })}
          {labels.map((label, i) => {
            const x = LEFT + i * xStep, lbl = String(label)
            const rotate = lbl.length > 6 || n > 10
            const short  = lbl.length > 12 ? lbl.slice(0, 11) + '…' : lbl
            return rotate ? (
              <text key={i} x={x} y={BASE_Y + 10} textAnchor="end"
                fontSize={7} fill={C.textMuted} fontFamily={FONT}
                transform={`rotate(-42,${x},${BASE_Y + 10})`}>{short}</text>
            ) : (
              <text key={i} x={x} y={BASE_Y + 15} textAnchor="middle"
                fontSize={7.5} fill={C.textMuted} fontFamily={FONT}>{short}</text>
            )
          })}
        </svg>
      </div>
      {name && <div style={{ fontSize: '0.65rem', color: C.textMuted, textAlign: 'center', marginTop: '4px' }}>{name}</div>}
    </div>
  )
}

// ─── Donut / pie chart ────────────────────────────────────────────────────────
function DonutChart({ labels, series, C }) {
  const firstSer = series[0] || {}
  const rawNums  = (firstSer.data || []).map(v => Math.abs(typeof v === 'number' && isFinite(v) ? v : 0))
  const name     = firstSer.name || ''
  const total    = rawNums.reduce((a, b) => a + b, 0)
  if (!total) return <FallbackTable labels={labels} series={series} C={C} />

  // Sort descending by value so the dominant slice is always first
  const all   = labels.map((lbl, i) => ({ lbl: String(lbl), val: rawNums[i] || 0 }))
    .sort((a, b) => b.val - a.val)

  // Show top 6; group anything beyond that into a single "Other (N)" slice
  const TOP_N = 6
  const shown = all.length > TOP_N + 1 ? all.slice(0, TOP_N) : all.slice()
  const rest  = all.length > TOP_N + 1 ? all.slice(TOP_N)   : []
  if (rest.length > 0) {
    shown.push({ lbl: `Other (${rest.length})`, val: rest.reduce((s, x) => s + x.val, 0) })
  }

  const CX = 90, CY = 90, OR = 74, IR = 44, SIZE = 180
  let angle = -Math.PI / 2

  const slices = shown.map((item, i) => {
    const frac = item.val / total
    const start = angle, end = angle + frac * 2 * Math.PI
    angle = end
    const x1o = CX + OR * Math.cos(start), y1o = CY + OR * Math.sin(start)
    const x2o = CX + OR * Math.cos(end),   y2o = CY + OR * Math.sin(end)
    const x1i = CX + IR * Math.cos(end),   y1i = CY + IR * Math.sin(end)
    const x2i = CX + IR * Math.cos(start), y2i = CY + IR * Math.sin(start)
    const lg   = end - start > Math.PI ? 1 : 0
    return {
      frac, val: item.val, color: PALETTE[i % PALETTE.length], label: item.lbl,
      d: `M${x1o.toFixed(2)} ${y1o.toFixed(2)} A${OR} ${OR} 0 ${lg} 1 ${x2o.toFixed(2)} ${y2o.toFixed(2)} L${x1i.toFixed(2)} ${y1i.toFixed(2)} A${IR} ${IR} 0 ${lg} 0 ${x2i.toFixed(2)} ${y2i.toFixed(2)} Z`,
    }
  })

  // Dominant = first slice (highest value after sort)
  const dom      = slices[0]
  const domShort = dom.label.length > 10 ? dom.label.slice(0, 9) + '…' : dom.label

  return (
    <div style={{ display: 'flex', flexDirection: 'column', alignItems: 'center', gap: '10px' }}>
      <svg viewBox={`0 0 ${SIZE} ${SIZE}`} width={SIZE} height={SIZE} aria-label={name || 'donut chart'}>
        {slices.map((s, i) => (
          <path key={i} d={s.d} fill={s.color} opacity={0.88}>
            <title>{s.label}: {s.val.toLocaleString()} ({(s.frac * 100).toFixed(1)}%)</title>
          </path>
        ))}
        {/* Center: total value */}
        <text x={CX} y={CY - 11} textAnchor="middle" fontSize={11} fontWeight="700"
          fill={C.text} fontFamily={FONT}>{fmtVal(total)}</text>
        <text x={CX} y={CY + 3} textAnchor="middle" fontSize={7}
          fill={C.textMuted} fontFamily={FONT}>total</text>
        {/* Center: dominant segment name + pct */}
        <text x={CX} y={CY + 16} textAnchor="middle" fontSize={6.5}
          fill={dom.color} fontFamily={FONT} fontWeight="600">
          {domShort} {(dom.frac * 100).toFixed(0)}%
        </text>
      </svg>

      {/* Legend: colour swatch + label + value + pct */}
      <div style={{ display: 'flex', flexWrap: 'wrap', gap: '5px 14px', justifyContent: 'center', maxWidth: '360px' }}>
        {slices.map((s, i) => (
          <div key={i} style={{ display: 'flex', alignItems: 'center', gap: '5px' }}>
            <div style={{ width: '8px', height: '8px', borderRadius: '2px', background: s.color, flexShrink: 0 }} />
            <span style={{ fontSize: '0.67rem', color: C.textSec }}>
              {s.label.length > 16 ? s.label.slice(0, 15) + '…' : s.label}
            </span>
            <span style={{ fontSize: '0.67rem', color: C.textMuted, whiteSpace: 'nowrap' }}>
              {fmtVal(s.val)} · {(s.frac * 100).toFixed(0)}%
            </span>
          </div>
        ))}
      </div>

      {name && <div style={{ fontSize: '0.65rem', color: C.textMuted }}>{name}</div>}
    </div>
  )
}

// ─── Stacked bar chart ────────────────────────────────────────────────────────
// Multi-series stacked bars. Each series[i].data[j] is stacked on top of [i-1].
function StackedBarChart({ labels, series, C }) {
  if (!labels.length || !series.length) return <FallbackTable labels={labels} series={series} C={C} />

  const n = labels.length
  const totals = labels.map((_, j) =>
    series.reduce((sum, s) => sum + (typeof s.data?.[j] === 'number' ? s.data[j] : 0), 0)
  )
  const maxTotal = Math.max(...totals, 1)

  const LEFT = 42, RIGHT = 10, TOP = 18, CH = 180, LBL_H = 52
  const SVG_W = 520, SVG_H = TOP + CH + LBL_H
  const CHART_W = SVG_W - LEFT - RIGHT, BASE_Y = TOP + CH
  const BAR_W = Math.max(10, Math.min(48, Math.floor(CHART_W / n * 0.7)))
  const gap = (CHART_W - n * BAR_W) / (n + 1)
  const ticks = [0, 0.25, 0.5, 0.75, 1].map(f => ({ y: TOP + CH - f * CH, val: Math.round(f * maxTotal) }))

  return (
    <div style={{ overflowX: 'auto' }}>
      <svg viewBox={`0 0 ${SVG_W} ${SVG_H}`} width="100%"
        style={{ display: 'block', minWidth: '200px', maxWidth: `${SVG_W}px` }}
        aria-label="stacked bar chart">
        {ticks.map(({ y, val }, gi) => (
          <g key={gi}>
            <line x1={LEFT} y1={y} x2={SVG_W - RIGHT} y2={y}
              stroke={C.border} strokeWidth={gi === 0 ? 1.2 : 0.6} strokeDasharray={gi === 0 ? 'none' : '3 3'} />
            <text x={LEFT - 4} y={y + 3.5} textAnchor="end" fontSize={7} fill={C.textMuted} fontFamily={FONT}>{fmtVal(val)}</text>
          </g>
        ))}
        {labels.map((label, j) => {
          const x = LEFT + gap + j * (BAR_W + gap)
          const lbl = String(label)
          const rotate = lbl.length > 7 || n > 9
          const short = lbl.length > 12 ? lbl.slice(0, 11) + '…' : lbl
          let stackY = BASE_Y
          return (
            <g key={j}>
              {series.map((s, si) => {
                const val = typeof s.data?.[j] === 'number' ? s.data[j] : 0
                const barH = Math.max(0, Math.round((val / maxTotal) * CH * 0.96))
                stackY -= barH
                return (
                  <rect key={si} x={x} y={stackY} width={BAR_W} height={barH}
                    fill={PALETTE[si % PALETTE.length]} opacity={0.85} rx={si === 0 ? 3 : 0}>
                    <title>{label} · {s.name || `Series ${si + 1}`}: {val.toLocaleString()}</title>
                  </rect>
                )
              })}
              {rotate ? (
                <text x={x + BAR_W / 2} y={BASE_Y + 10} textAnchor="end" fontSize={7} fill={C.textMuted} fontFamily={FONT}
                  transform={`rotate(-42,${x + BAR_W / 2},${BASE_Y + 10})`}>{short}</text>
              ) : (
                <text x={x + BAR_W / 2} y={BASE_Y + 14} textAnchor="middle" fontSize={7.5} fill={C.textMuted} fontFamily={FONT}>{short}</text>
              )}
            </g>
          )
        })}
      </svg>
      <div style={{ display: 'flex', flexWrap: 'wrap', gap: '5px 14px', justifyContent: 'center', marginTop: '6px' }}>
        {series.map((s, si) => (
          <div key={si} style={{ display: 'flex', alignItems: 'center', gap: '5px' }}>
            <div style={{ width: '8px', height: '8px', borderRadius: '2px', background: PALETTE[si % PALETTE.length] }} />
            <span style={{ fontSize: '0.67rem', color: C.textSec }}>{s.name || `Series ${si + 1}`}</span>
          </div>
        ))}
      </div>
    </div>
  )
}

// ─── Grouped line chart ───────────────────────────────────────────────────────
// Multi-series line chart, each series rendered in a distinct palette colour.
function GroupedLineChart({ labels, series, C }) {
  if (!labels.length || !series.length) return <FallbackTable labels={labels} series={series} C={C} />
  if (labels.length < 2) return <FallbackTable labels={labels} series={series} C={C} />

  const allNums = series.flatMap(s => (s.data || []).filter(v => typeof v === 'number' && isFinite(v)))
  if (!allNums.length) return <FallbackTable labels={labels} series={series} C={C} />

  const n = labels.length
  const maxVal = Math.max(...allNums, 1)
  const minVal = Math.min(...allNums, 0)
  const range = maxVal - minVal || 1

  const LEFT = 42, RIGHT = 16, TOP = 20, CH = 160, LBL_H = 48
  const SVG_W = 520, SVG_H = TOP + CH + LBL_H
  const CHART_W = SVG_W - LEFT - RIGHT, BASE_Y = TOP + CH
  const xStep = CHART_W / (n - 1)

  const toY = v => TOP + CH * 0.96 - ((v - minVal) / range) * CH * 0.92
  const ticks = [0, 0.25, 0.5, 0.75, 1].map(f => ({ val: Math.round(minVal + f * range), y: TOP + CH * 0.96 - f * CH * 0.92 }))

  return (
    <div style={{ overflowX: 'auto' }}>
      <svg viewBox={`0 0 ${SVG_W} ${SVG_H}`} width="100%"
        style={{ display: 'block', minWidth: '200px', maxWidth: `${SVG_W}px` }}
        aria-label="grouped line chart">
        {ticks.map(({ val, y }, gi) => (
          <g key={gi}>
            <line x1={LEFT} y1={y} x2={SVG_W - RIGHT} y2={y}
              stroke={C.border} strokeWidth={0.7} strokeDasharray="3 3" />
            <text x={LEFT - 4} y={y + 3.5} textAnchor="end" fontSize={7} fill={C.textMuted} fontFamily={FONT}>{fmtVal(val)}</text>
          </g>
        ))}
        <line x1={LEFT} y1={BASE_Y} x2={SVG_W - RIGHT} y2={BASE_Y} stroke={C.border} strokeWidth={1} />
        {series.map((s, si) => {
          const color = PALETTE[si % PALETTE.length]
          const nums = (s.data || []).map(v => (typeof v === 'number' && isFinite(v) ? v : null))
          const pts = nums.map((v, i) => v != null ? { x: LEFT + i * xStep, y: toY(v), v } : null)
          const linePts = pts.filter(Boolean).map(p => `${p.x.toFixed(1)},${p.y.toFixed(1)}`).join(' ')
          return (
            <g key={si}>
              {linePts && <polyline points={linePts} fill="none" stroke={color} strokeWidth={2}
                strokeLinecap="round" strokeLinejoin="round" opacity={0.85} />}
              {pts.map((p, i) => p && (
                <circle key={i} cx={p.x} cy={p.y} r={3} fill={color} stroke={C.surface} strokeWidth={1.2}>
                  <title>{labels[i]}: {p.v.toLocaleString()} ({s.name || `Series ${si + 1}`})</title>
                </circle>
              ))}
            </g>
          )
        })}
        {labels.map((label, i) => {
          const x = LEFT + i * xStep, lbl = String(label)
          const rotate = lbl.length > 6 || n > 10
          const short = lbl.length > 12 ? lbl.slice(0, 11) + '…' : lbl
          return rotate ? (
            <text key={i} x={x} y={BASE_Y + 10} textAnchor="end" fontSize={7} fill={C.textMuted} fontFamily={FONT}
              transform={`rotate(-42,${x},${BASE_Y + 10})`}>{short}</text>
          ) : (
            <text key={i} x={x} y={BASE_Y + 15} textAnchor="middle" fontSize={7.5} fill={C.textMuted} fontFamily={FONT}>{short}</text>
          )
        })}
      </svg>
      <div style={{ display: 'flex', flexWrap: 'wrap', gap: '5px 14px', justifyContent: 'center', marginTop: '6px' }}>
        {series.map((s, si) => (
          <div key={si} style={{ display: 'flex', alignItems: 'center', gap: '5px' }}>
            <svg width="20" height="4"><line x1="0" y1="2" x2="20" y2="2" stroke={PALETTE[si % PALETTE.length]} strokeWidth="2" /></svg>
            <span style={{ fontSize: '0.67rem', color: C.textSec }}>{s.name || `Series ${si + 1}`}</span>
          </div>
        ))}
      </div>
    </div>
  )
}

// ─── Scatter plot ─────────────────────────────────────────────────────────────
// Schema: { chart_type:"scatter", x_label, y_label, points:[{x,y,label?}] }
function ScatterPlot({ chart, C }) {
  const pts    = (chart.points || []).filter(p => isFinite(p?.x) && isFinite(p?.y))
  const xLabel = chart.x_label || 'X'
  const yLabel = chart.y_label || 'Y'

  if (pts.length < 2) {
    return <div style={{ fontSize: '0.75rem', color: C.textMuted }}>Not enough data points for scatter plot (need ≥ 2).</div>
  }

  const xs = pts.map(p => p.x), ys = pts.map(p => p.y)
  const xMin = Math.min(...xs), xMax = Math.max(...xs)
  const yMin = Math.min(...ys), yMax = Math.max(...ys)
  const xRange = xMax - xMin || 1, yRange = yMax - yMin || 1

  const LEFT = 48, RIGHT = 16, TOP = 12, CH = 200, LBL_H = 48, AXIS_LBL_W = 18
  const SVG_W = 520, SVG_H = TOP + CH + LBL_H + AXIS_LBL_W
  const CHART_W = SVG_W - LEFT - RIGHT, BASE_Y = TOP + CH

  const toSVG = (x, y) => ({
    sx: LEFT + ((x - xMin) / xRange) * CHART_W,
    sy: TOP  + ((yMax - y) / yRange) * CH * 0.94 + CH * 0.03,
  })

  // Simple linear regression for trend line
  const n = pts.length
  const xMean = xs.reduce((a, b) => a + b, 0) / n
  const yMean = ys.reduce((a, b) => a + b, 0) / n
  const slope = xs.reduce((s, x, i) => s + (x - xMean) * (ys[i] - yMean), 0) /
                xs.reduce((s, x) => s + (x - xMean) ** 2, 0) || 0
  const intercept = yMean - slope * xMean
  const trendY1 = slope * xMin + intercept
  const trendY2 = slope * xMax + intercept
  const tp1 = toSVG(xMin, trendY1), tp2 = toSVG(xMax, trendY2)

  const xTicks = [0, 0.25, 0.5, 0.75, 1].map(f => ({ val: xMin + f * xRange, sx: LEFT + f * CHART_W }))
  const yTicks = [0, 0.25, 0.5, 0.75, 1].map(f => ({ val: yMin + f * yRange, sy: BASE_Y - f * CH * 0.94 - CH * 0.03 }))

  // Cap dots rendered to 500 to stay performant
  const visiblePts = pts.length > 500 ? pts.filter((_, i) => i % Math.ceil(pts.length / 500) === 0) : pts

  return (
    <div style={{ overflowX: 'auto' }}>
      <svg viewBox={`0 0 ${SVG_W} ${SVG_H}`} width="100%"
        style={{ display: 'block', minWidth: `${Math.min(SVG_W, 200)}px`, maxWidth: `${SVG_W}px` }}
        aria-label={`Scatter: ${xLabel} vs ${yLabel}`}>

        {/* Y gridlines */}
        {yTicks.map(({ val, sy }, gi) => (
          <g key={gi}>
            <line x1={LEFT} y1={sy} x2={SVG_W - RIGHT} y2={sy}
              stroke={C.border} strokeWidth={0.6} strokeDasharray="3 3" />
            <text x={LEFT - 4} y={sy + 3.5} textAnchor="end"
              fontSize={6.5} fill={C.textMuted} fontFamily={FONT}>{fmtVal(val)}</text>
          </g>
        ))}

        {/* Axes */}
        <line x1={LEFT} y1={TOP} x2={LEFT} y2={BASE_Y} stroke={C.border} strokeWidth={1} />
        <line x1={LEFT} y1={BASE_Y} x2={SVG_W - RIGHT} y2={BASE_Y} stroke={C.border} strokeWidth={1} />

        {/* X tick labels */}
        {xTicks.map(({ val, sx }, gi) => (
          <text key={gi} x={sx} y={BASE_Y + 13} textAnchor="middle"
            fontSize={6.5} fill={C.textMuted} fontFamily={FONT}>{fmtVal(val)}</text>
        ))}

        {/* Trend line */}
        <line x1={tp1.sx} y1={tp1.sy} x2={tp2.sx} y2={tp2.sy}
          stroke="#6366f1" strokeWidth={1.2} strokeDasharray="5 3" opacity={0.55} />

        {/* Data points */}
        {visiblePts.map((p, i) => {
          const { sx, sy } = toSVG(p.x, p.y)
          return (
            <circle key={i} cx={sx} cy={sy} r={3} fill="#6366f1" opacity={0.55}>
              {p.label && <title>{p.label}</title>}
            </circle>
          )
        })}

        {/* Axis labels */}
        <text x={SVG_W / 2} y={SVG_H - 2} textAnchor="middle"
          fontSize={8} fill={C.textSec} fontFamily={FONT}>{xLabel}</text>
        <text x={10} y={TOP + CH / 2} textAnchor="middle" fontSize={8}
          fill={C.textSec} fontFamily={FONT}
          transform={`rotate(-90,10,${TOP + CH / 2})`}>{yLabel}</text>
      </svg>
      <div style={{ fontSize: '0.65rem', color: C.textMuted, textAlign: 'center', marginTop: '4px' }}>
        {pts.length.toLocaleString()} points · dashed line = linear trend
      </div>
    </div>
  )
}

// ─── Heatmap ─────────────────────────────────────────────────────────────────
// Schema: { chart_type:"heatmap", x_labels, y_labels, values:number[][] }
// values[row][col] where row indexes y_labels and col indexes x_labels.
function HeatmapChart({ chart, C }) {
  const xLabels = chart.x_labels || []
  const yLabels = chart.y_labels || []
  const values  = chart.values  || []
  const name    = chart.name    || ''

  if (!xLabels.length || !yLabels.length || !values.length) {
    return <div style={{ fontSize: '0.75rem', color: C.textMuted }}>Insufficient heatmap data.</div>
  }

  const allVals = values.flat().filter(v => typeof v === 'number' && isFinite(v))
  if (!allVals.length) {
    return <div style={{ fontSize: '0.75rem', color: C.textMuted }}>No numeric values in heatmap data.</div>
  }

  const vMin = Math.min(...allVals), vMax = Math.max(...allVals), vRange = vMax - vMin || 1
  const normalise = v => (v - vMin) / vRange  // 0–1

  const nCols = xLabels.length, nRows = yLabels.length
  const CELL = Math.max(20, Math.min(56, Math.floor(440 / Math.max(nCols, nRows))))
  const LBL_TOP = 54, LBL_LEFT = 72, PAD_R = 14, PAD_B = 12
  const SVG_W = LBL_LEFT + nCols * CELL + PAD_R
  const SVG_H = LBL_TOP  + nRows * CELL + PAD_B

  // Colour: low → yellow-orange, mid → neutral, high → indigo
  const heatColor = n => {
    const r = Math.round(239 - (239 - 99)  * n)
    const g = Math.round(68  + (102 - 68)  * n)
    const b = Math.round(68  + (241 - 68)  * n)
    return `rgb(${r},${g},${b})`
  }

  return (
    <div style={{ overflowX: 'auto' }}>
      <svg viewBox={`0 0 ${SVG_W} ${SVG_H}`} width="100%"
        style={{ display: 'block', minWidth: `${Math.min(SVG_W, 200)}px`, maxWidth: `${SVG_W}px` }}
        aria-label={name || 'heatmap'}>

        {/* X-axis labels (rotated) */}
        {xLabels.map((lbl, ci) => {
          const x = LBL_LEFT + ci * CELL + CELL / 2
          const short = String(lbl).length > 10 ? String(lbl).slice(0, 9) + '…' : String(lbl)
          return (
            <text key={ci} x={x} y={LBL_TOP - 6} textAnchor="end"
              fontSize={7} fill={C.textMuted} fontFamily={FONT}
              transform={`rotate(-45,${x},${LBL_TOP - 6})`}>{short}</text>
          )
        })}

        {/* Cells */}
        {yLabels.map((rowLbl, ri) => (
          <g key={ri}>
            {/* Y-axis label */}
            <text x={LBL_LEFT - 5} y={LBL_TOP + ri * CELL + CELL / 2 + 3.5}
              textAnchor="end" fontSize={7} fill={C.textMuted} fontFamily={FONT}>
              {String(rowLbl).length > 10 ? String(rowLbl).slice(0, 9) + '…' : String(rowLbl)}
            </text>
            {xLabels.map((_, ci) => {
              const raw = (values[ri] || [])[ci]
              const v   = typeof raw === 'number' && isFinite(raw) ? raw : null
              const n   = v != null ? normalise(v) : 0.5
              const fill = v != null ? heatColor(n) : C.borderAlt
              const textColor = (n > 0.55 || n < 0.25) ? '#fff' : C.text
              return (
                <g key={ci}>
                  <rect x={LBL_LEFT + ci * CELL} y={LBL_TOP + ri * CELL}
                    width={CELL - 1} height={CELL - 1} rx={2} fill={fill}>
                    <title>{rowLbl} × {xLabels[ci]}: {v != null ? v.toFixed(3) : '—'}</title>
                  </rect>
                  {CELL >= 28 && v != null && (
                    <text
                      x={LBL_LEFT + ci * CELL + CELL / 2}
                      y={LBL_TOP + ri * CELL + CELL / 2 + 3.5}
                      textAnchor="middle" fontSize={Math.min(7.5, CELL * 0.25)}
                      fill={textColor} fontFamily={FONT} fontWeight="500">
                      {Math.abs(v) >= 1000 ? fmtVal(v) : v.toFixed(2)}
                    </text>
                  )}
                </g>
              )
            })}
          </g>
        ))}
      </svg>
      {name && <div style={{ fontSize: '0.65rem', color: C.textMuted, textAlign: 'center', marginTop: '4px' }}>{name}</div>}
    </div>
  )
}

// ─── Correlation matrix ───────────────────────────────────────────────────────
// Schema: { chart_type:"correlation_matrix", columns:string[], matrix:number[][] }
// matrix[i][j] = Pearson r between columns[i] and columns[j]; diagonal = 1.0
function CorrelationMatrix({ chart, C }) {
  const cols   = chart.columns || []
  const matrix = chart.matrix  || []

  if (cols.length < 2 || !matrix.length) {
    return <div style={{ fontSize: '0.75rem', color: C.textMuted }}>Not enough numeric columns for correlation matrix (need ≥ 2).</div>
  }

  const n    = cols.length
  const CELL = Math.max(24, Math.min(56, Math.floor(420 / n)))
  const LBL  = 68   // left label area
  const TOP  = 58   // top label area
  const PAD  = 12
  const SVG_W = LBL + n * CELL + PAD
  const SVG_H = TOP + n * CELL + PAD

  return (
    <div style={{ overflowX: 'auto' }}>
      <svg viewBox={`0 0 ${SVG_W} ${SVG_H}`} width="100%"
        style={{ display: 'block', minWidth: `${Math.min(SVG_W, 200)}px`, maxWidth: `${SVG_W}px` }}
        aria-label="Correlation matrix">

        {/* Column headers (rotated, top) */}
        {cols.map((col, ci) => {
          const x = LBL + ci * CELL + CELL / 2
          const short = col.length > 11 ? col.slice(0, 10) + '…' : col
          return (
            <text key={ci} x={x} y={TOP - 8} textAnchor="end"
              fontSize={7} fill={C.textSec} fontFamily={FONT}
              transform={`rotate(-45,${x},${TOP - 8})`}>{short}</text>
          )
        })}

        {/* Row label + cells */}
        {cols.map((rowCol, ri) => (
          <g key={ri}>
            <text x={LBL - 5} y={TOP + ri * CELL + CELL / 2 + 3.5}
              textAnchor="end" fontSize={7} fill={C.textSec} fontFamily={FONT}>
              {rowCol.length > 11 ? rowCol.slice(0, 10) + '…' : rowCol}
            </text>
            {cols.map((_, ci) => {
              const raw = (matrix[ri] || [])[ci]
              const v   = typeof raw === 'number' && isFinite(raw) ? raw : null
              const isDiag = ri === ci
              const fill   = isDiag ? C.accentSoft : (v != null ? corrFill(v) : C.borderAlt)
              const border = isDiag ? `1px solid ${C.accent}40` : 'none'
              const tColor = isDiag ? C.accent : (v != null ? corrText(v, C) : C.textMuted)
              const label  = isDiag ? '—' : (v != null ? v.toFixed(2) : '?')
              const absV   = Math.abs(v ?? 0)
              const strength = absV >= 0.7 ? 'strong' : absV >= 0.4 ? 'moderate' : 'weak'

              return (
                <g key={ci}>
                  <rect
                    x={LBL + ci * CELL} y={TOP + ri * CELL}
                    width={CELL - 1} height={CELL - 1} rx={2}
                    fill={fill} stroke={isDiag ? C.accent : 'none'} strokeWidth={isDiag ? 0.8 : 0}
                    strokeOpacity={0.4}>
                    <title>
                      {rowCol} × {cols[ci]}: {v != null ? `${v.toFixed(4)} (${strength})` : '—'}
                    </title>
                  </rect>
                  {CELL >= 26 && (
                    <text
                      x={LBL + ci * CELL + CELL / 2}
                      y={TOP + ri * CELL + CELL / 2 + 3.5}
                      textAnchor="middle" fontSize={Math.min(8, CELL * 0.22)}
                      fill={tColor} fontFamily={FONT} fontWeight={isDiag ? '700' : '500'}>
                      {label}
                    </text>
                  )}
                </g>
              )
            })}
          </g>
        ))}
      </svg>

      {/* Colour legend */}
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'center', gap: '16px', marginTop: '8px', flexWrap: 'wrap' }}>
        {[
          { color: 'rgba(239,68,68,0.75)',  label: '−1  strong negative' },
          { color: 'rgba(239,68,68,0.35)',  label: '−0.4  moderate' },
          { color: C.borderAlt,              label: '0  none' },
          { color: 'rgba(99,102,241,0.35)', label: '+0.4  moderate' },
          { color: 'rgba(99,102,241,0.75)', label: '+1  strong positive' },
        ].map(({ color, label }) => (
          <div key={label} style={{ display: 'flex', alignItems: 'center', gap: '5px' }}>
            <div style={{ width: '12px', height: '12px', borderRadius: '2px', background: color, border: `1px solid ${C.border}` }} />
            <span style={{ fontSize: '0.62rem', color: C.textMuted }}>{label}</span>
          </div>
        ))}
      </div>
    </div>
  )
}

// ─── Forecast chart ───────────────────────────────────────────────────────────
function ForecastChart({ chart, C }) {
  const labels    = chart?.labels     || []
  const hist      = chart?.historical || []
  const fc        = chart?.forecast   || []
  const upper     = chart?.upper_band || []
  const lower     = chart?.lower_band || []
  const startIdx  = chart?.forecast_start_index ?? labels.length

  const n = labels.length
  if (n === 0) return <div style={{ fontSize: '0.75rem', color: C.textMuted }}>No forecast data.</div>

  const allVals = [...hist, ...fc, ...upper, ...lower].filter(v => v != null && isFinite(v))
  if (!allVals.length) return <div style={{ fontSize: '0.75rem', color: C.textMuted }}>No forecast data.</div>

  const maxVal = Math.max(...allVals, 1)
  const minVal = Math.max(0, Math.min(...allVals) * 0.9)
  const range  = maxVal - minVal || 1

  const LEFT = 48, RIGHT = 10, TOP = 18, CH = 160, LBL_H = 58
  const SVG_W = 560, SVG_H = TOP + CH + LBL_H
  const CHART_W = SVG_W - LEFT - RIGHT
  const BASE_Y  = TOP + CH

  const xPos = i => LEFT + (n > 1 ? (i / (n - 1)) * CHART_W : CHART_W / 2)
  const yPos = v  => BASE_Y - ((v - minVal) / range) * CH

  const pathFrom = series => {
    let d = ''
    series.forEach((v, i) => {
      if (v == null) return
      const px = xPos(i), py = yPos(v)
      d += (d === '' || series[i - 1] == null) ? `M ${px} ${py}` : ` L ${px} ${py}`
    })
    return d
  }

  const bandPolygon = () => {
    const ups = upper.map((v, i) => v != null ? [xPos(i), yPos(v)] : null).filter(Boolean)
    const los = lower.map((v, i) => v != null ? [xPos(i), yPos(v)] : null).filter(Boolean)
    if (!ups.length || !los.length) return ''
    return [...ups, ...[...los].reverse()].map(([x, y]) => `${x},${y}`).join(' ')
  }

  const ticks = [0, 0.25, 0.5, 0.75, 1].map(f => ({
    y: BASE_Y - f * CH, val: minVal + f * range,
  }))

  const step    = n > 14 ? Math.ceil(n / 8) : 1
  const xLabels = labels
    .map((lbl, i) => ({ lbl, i }))
    .filter(({ i }) => i % step === 0 || i === n - 1)

  const sepX = startIdx > 0 && startIdx < n ? xPos(startIdx - 0.5) : null

  const HIST_C = '#6366f1'
  const FC_C   = '#8b5cf6'
  const BAND_C = 'rgba(99,102,241,0.12)'

  return (
    <div style={{ overflowX: 'auto' }}>
      <svg viewBox={`0 0 ${SVG_W} ${SVG_H}`} width="100%"
        style={{ display: 'block', minWidth: '280px', maxWidth: `${SVG_W}px` }}
        aria-label="Forecast chart">

        {ticks.map(({ y, val }, gi) => (
          <g key={gi}>
            <line x1={LEFT} y1={y} x2={SVG_W - RIGHT} y2={y}
              stroke={C.border} strokeWidth={gi === 0 ? 1.2 : 0.6}
              strokeDasharray={gi === 0 ? 'none' : '3 3'} />
            <text x={LEFT - 4} y={y + 3.5} textAnchor="end"
              fontSize={7} fill={C.textMuted} fontFamily={FONT}>
              {fmtVal(Math.round(val))}
            </text>
          </g>
        ))}

        {bandPolygon() && <polygon points={bandPolygon()} fill={BAND_C} />}

        {sepX != null && (
          <line x1={sepX} y1={TOP} x2={sepX} y2={BASE_Y}
            stroke={FC_C} strokeWidth={1} strokeDasharray="5 3" opacity={0.55} />
        )}

        {pathFrom(hist) && (
          <path d={pathFrom(hist)} fill="none" stroke={HIST_C} strokeWidth={2}
            strokeLinecap="round" strokeLinejoin="round" />
        )}

        {pathFrom(fc) && (
          <path d={pathFrom(fc)} fill="none" stroke={FC_C} strokeWidth={2}
            strokeDasharray="7 4" strokeLinecap="round" strokeLinejoin="round" />
        )}

        {xLabels.map(({ lbl, i }) => (
          <text key={i} x={xPos(i)} y={BASE_Y + 11}
            textAnchor="end"
            transform={`rotate(-40,${xPos(i)},${BASE_Y + 11})`}
            fontSize={6.5} fill={i >= startIdx ? FC_C : C.textMuted} fontFamily={FONT}>
            {String(lbl).slice(0, 12)}
          </text>
        ))}
      </svg>

      <div style={{ display: 'flex', alignItems: 'center', gap: '16px', marginTop: '6px',
        flexWrap: 'wrap', justifyContent: 'center' }}>
        {[
          { color: HIST_C, dash: false, label: 'Historical' },
          { color: FC_C,   dash: true,  label: 'Forecast'   },
        ].map(({ color, dash, label }) => (
          <div key={label} style={{ display: 'flex', alignItems: 'center', gap: '5px' }}>
            <svg width="20" height="4">
              <line x1="0" y1="2" x2="20" y2="2" stroke={color} strokeWidth="2"
                strokeDasharray={dash ? '6 3' : 'none'} />
            </svg>
            <span style={{ fontSize: '0.65rem', color: C.textMuted }}>{label}</span>
          </div>
        ))}
        <div style={{ display: 'flex', alignItems: 'center', gap: '5px' }}>
          <div style={{ width: '14px', height: '10px', background: BAND_C,
            border: `1px solid ${HIST_C}40`, borderRadius: '2px' }} />
          <span style={{ fontSize: '0.65rem', color: C.textMuted }}>Confidence Band</span>
        </div>
      </div>
    </div>
  )
}

// ─── Public entry point ───────────────────────────────────────────────────────
export default function ChartSection({ chart, C }) {
  // Resolve the effective chart type, falling back to recommendation if absent
  const chartType = recommendChartType(chart)
  const labels    = chart?.labels  || []
  const series    = chart?.series  || []

  // Specialized schemas don't need labels/series
  if (chartType === 'forecast')           return <ForecastChart      chart={chart} C={C} />
  if (chartType === 'correlation_matrix') return <CorrelationMatrix  chart={chart} C={C} />
  if (chartType === 'heatmap')            return <HeatmapChart       chart={chart} C={C} />
  if (chartType === 'scatter')            return <ScatterPlot        chart={chart} C={C} />

  // Standard label+series charts need at least labels
  if (!labels.length) {
    return <div style={{ fontSize: '0.75rem', color: C.textMuted }}>No chart data available.</div>
  }

  if (chartType === 'bar')                          return <BarChart         labels={labels} series={series} C={C} />
  if (chartType === 'bar_horizontal')               return <BarChart         labels={labels} series={series} C={C} horizontal />
  if (chartType === 'line')                         return <LineChart        labels={labels} series={series} C={C} />
  if (chartType === 'pie' || chartType === 'donut') return <DonutChart       labels={labels} series={series} C={C} />
  if (chartType === 'stacked_bar')                  return <StackedBarChart  labels={labels} series={series} C={C} />
  if (chartType === 'grouped_line')                 return <GroupedLineChart labels={labels} series={series} C={C} />

  return <FallbackTable labels={labels} series={series} C={C} />
}
