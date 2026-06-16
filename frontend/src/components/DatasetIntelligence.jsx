import { useState, useMemo } from 'react'
import ChartSection from './ChartSection'

const FONT = "system-ui, -apple-system, 'Segoe UI', sans-serif"
const MONO = "'Cascadia Code', 'Fira Code', 'JetBrains Mono', monospace"

// ─── Quality score ─────────────────────────────────────────────────────────────
function computeQuality(ds) {
  const rowCount       = ds.row_count || 1
  const colCount       = ds.column_count || 1
  const numericProfile = ds.numeric_profile    || {}
  const missing        = ds.missing_values     || {}
  const catMeta        = ds.categorical_meta   || {}

  let score = 100
  const issues = []

  // Missing value penalties
  for (const [col, cnt] of Object.entries(missing)) {
    const pct = cnt / rowCount
    if      (pct > 0.50) { score -= 14; issues.push({ col, type: 'severe_missing',   pct }) }
    else if (pct > 0.20) { score -= 7;  issues.push({ col, type: 'high_missing',      pct }) }
    else if (pct > 0.05) { score -= 3;  issues.push({ col, type: 'moderate_missing',  pct }) }
    else if (pct > 0)    { score -= 1 }
  }

  // Outlier-heavy numeric columns
  for (const [col, stats] of Object.entries(numericProfile)) {
    if (stats.outlier_count && rowCount > 0) {
      const pct = stats.outlier_count / rowCount
      if      (pct > 0.10) { score -= 5; issues.push({ col, type: 'high_outliers',     pct }) }
      else if (pct > 0.05) { score -= 2; issues.push({ col, type: 'moderate_outliers', pct }) }
    }
  }

  // Constant columns (only 1 distinct value)
  for (const [col, meta] of Object.entries(catMeta)) {
    if (meta.distinct_count === 1) { score -= 5; issues.push({ col, type: 'constant_column' }) }
  }

  // Dataset size
  if      (rowCount < 10)  { score -= 20; issues.push({ type: 'few_rows',      val: rowCount }) }
  else if (rowCount < 100) { score -= 5;  issues.push({ type: 'small_dataset', val: rowCount }) }
  if (colCount < 2)        { score -= 10 }

  score = Math.max(0, Math.min(100, Math.round(score)))
  const grade      = score >= 90 ? 'A' : score >= 75 ? 'B' : score >= 60 ? 'C' : score >= 40 ? 'D' : 'F'
  const gradeColor = score >= 75 ? '#10b981' : score >= 60 ? '#f59e0b' : '#ef4444'
  const label      = score >= 90 ? 'Excellent' : score >= 75 ? 'Good' : score >= 60 ? 'Fair' : score >= 40 ? 'Poor' : 'Critical'
  return { score, grade, gradeColor, label, issues }
}

// ─── Helpers ──────────────────────────────────────────────────────────────────
function getColType(col, ds) {
  if ((ds.numeric_columns || []).includes(col) || col in (ds.numeric_profile || {})) return 'numeric'
  const dateCols = (ds.date_profile?.date_columns || []).map(d => d.column)
  if (dateCols.includes(col)) return 'date'
  if (col in (ds.categorical_profile || {})) return 'categorical'
  return 'text'
}

const TYPE_STYLE = {
  numeric:     { label: 'NUM',  color: '#6366f1', bg: '#6366f11a' },
  categorical: { label: 'CAT',  color: '#10b981', bg: '#10b9811a' },
  date:        { label: 'DATE', color: '#f59e0b', bg: '#f59e0b1a' },
  text:        { label: 'TXT',  color: '#6b7280', bg: '#6b72801a' },
}

function fmtN(v, dec = 2) {
  if (v == null || !isFinite(v)) return '—'
  return Number(v).toLocaleString(undefined, { maximumFractionDigits: dec })
}

function pct(count, total) {
  if (!total || !count) return '0%'
  return `${((count / total) * 100).toFixed(1)}%`
}

function SecLabel({ text, C }) {
  return (
    <div style={{ fontSize: '0.61rem', color: C.textMuted, fontWeight: '700', textTransform: 'uppercase', letterSpacing: '0.09em', marginBottom: '8px' }}>
      {text}
    </div>
  )
}

function TypeBadge({ type }) {
  const s = TYPE_STYLE[type] || TYPE_STYLE.text
  return (
    <span style={{ fontSize: '0.58rem', fontWeight: '700', padding: '1px 5px', borderRadius: '3px', background: s.bg, color: s.color, fontFamily: MONO, letterSpacing: '0.04em', flexShrink: 0 }}>
      {s.label}
    </span>
  )
}

function ScoreCircle({ score, grade, color, size = 64 }) {
  const r = (size - 8) / 2
  const circ = 2 * Math.PI * r
  const dash = (score / 100) * circ
  return (
    <svg width={size} height={size} viewBox={`0 0 ${size} ${size}`} style={{ flexShrink: 0 }}>
      <circle cx={size / 2} cy={size / 2} r={r} fill="none" stroke="#1b1f3520" strokeWidth={5} />
      <circle cx={size / 2} cy={size / 2} r={r} fill="none" stroke={color} strokeWidth={5}
        strokeDasharray={`${dash} ${circ}`} strokeLinecap="round"
        transform={`rotate(-90 ${size / 2} ${size / 2})`} />
      <text x={size / 2} y={size / 2 - 4} textAnchor="middle" fontSize={size * 0.25}
        fontWeight="700" fill={color} fontFamily={FONT}>{grade}</text>
      <text x={size / 2} y={size / 2 + size * 0.15} textAnchor="middle" fontSize={size * 0.14}
        fill="#6b7280" fontFamily={FONT}>{score}</text>
    </svg>
  )
}

// ─── Build correlation chart object from raw pairs ────────────────────────────
function buildCorrChart(pairs) {
  if (!pairs || pairs.length < 2) return null
  const seen = new Set(), cols = []
  for (const { column_a, column_b } of pairs) {
    if (!seen.has(column_a)) { cols.push(column_a); seen.add(column_a) }
    if (!seen.has(column_b)) { cols.push(column_b); seen.add(column_b) }
  }
  const limited = cols.slice(0, 8)
  const lookup = {}
  for (const { column_a, column_b, correlation } of pairs) {
    lookup[`${column_a}||${column_b}`] = correlation
    lookup[`${column_b}||${column_a}`] = correlation
  }
  const matrix = limited.map(r => limited.map(c =>
    r === c ? 1.0 : (lookup[`${r}||${c}`] ?? 0)
  ))
  return { chart_type: 'correlation_matrix', columns: limited, matrix }
}

// ─── Tab: Overview ────────────────────────────────────────────────────────────
function OverviewTab({ ds, quality, C, onGenerateReport, hasReport, report }) {
  const cols    = ds.columns || []
  const numCols = (ds.numeric_columns || []).length
  const dateCols = (ds.date_profile?.date_columns || []).length
  const catCols = Math.max(0, cols.length - numCols - dateCols)
  const totalMissing = Object.values(ds.missing_values || {}).reduce((a, b) => a + b, 0)
  const missingRate  = ds.row_count > 0 && cols.length > 0
    ? totalMissing / (ds.row_count * cols.length) : 0

  const kpis = [
    { label: 'Rows',        value: (ds.row_count || 0).toLocaleString(), color: C.accent },
    { label: 'Columns',     value: ds.column_count || 0,                  color: '#8b5cf6' },
    { label: 'Numeric',     value: numCols,                               color: '#6366f1' },
    { label: 'Categorical', value: catCols,                               color: '#10b981' },
    { label: 'Date',        value: dateCols,                              color: '#f59e0b' },
    { label: 'Missing %',   value: `${(missingRate * 100).toFixed(1)}%`,  color: missingRate > 0.1 ? '#ef4444' : '#10b981' },
  ]

  // Top missing columns
  const topMissing = Object.entries(ds.missing_values || {})
    .filter(([, c]) => c > 0)
    .sort((a, b) => b[1] - a[1])
    .slice(0, 4)

  // Top correlation pair
  const pairs = ds.correlation_profile || []
  const topPair = pairs.length ? pairs[0] : null

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: '18px' }}>

      {/* KPI grid */}
      <div>
        <SecLabel text="Dataset Snapshot" C={C} />
        <div style={{ display: 'grid', gridTemplateColumns: 'repeat(3, 1fr)', gap: '8px' }}>
          {kpis.map(({ label, value, color }) => (
            <div key={label} style={{ background: C.bg, border: `1px solid ${C.border}`, borderRadius: '10px', padding: '12px 14px' }}>
              <div style={{ fontSize: '0.6rem', color: C.textMuted, fontWeight: '700', textTransform: 'uppercase', letterSpacing: '0.08em', marginBottom: '6px' }}>{label}</div>
              <div style={{ fontSize: '1.2rem', fontWeight: '700', color, fontFamily: MONO, letterSpacing: '-0.02em' }}>{value}</div>
            </div>
          ))}
        </div>
      </div>

      {/* Column type breakdown bar */}
      {cols.length > 0 && (
        <div>
          <SecLabel text="Column Types" C={C} />
          <div style={{ height: '8px', borderRadius: '6px', overflow: 'hidden', display: 'flex', gap: '1px' }}>
            {numCols > 0 && <div style={{ flex: numCols, background: '#6366f1', minWidth: '2px' }} title={`${numCols} numeric`} />}
            {catCols > 0 && <div style={{ flex: catCols, background: '#10b981', minWidth: '2px' }} title={`${catCols} categorical`} />}
            {dateCols > 0 && <div style={{ flex: dateCols, background: '#f59e0b', minWidth: '2px' }} title={`${dateCols} date`} />}
            {cols.length - numCols - catCols - dateCols > 0 && <div style={{ flex: cols.length - numCols - catCols - dateCols, background: C.borderAlt, minWidth: '2px' }} />}
          </div>
          <div style={{ display: 'flex', gap: '14px', marginTop: '6px', flexWrap: 'wrap' }}>
            {[
              { label: `${numCols} Numeric`,     color: '#6366f1' },
              { label: `${catCols} Categorical`, color: '#10b981' },
              { label: `${dateCols} Date`,       color: '#f59e0b' },
            ].filter(item => parseInt(item.label) > 0).map(({ label, color }) => (
              <div key={label} style={{ display: 'flex', alignItems: 'center', gap: '5px' }}>
                <div style={{ width: '7px', height: '7px', borderRadius: '2px', background: color }} />
                <span style={{ fontSize: '0.68rem', color: C.textSec }}>{label}</span>
              </div>
            ))}
          </div>
        </div>
      )}

      {/* Intelligence highlights */}
      <div style={{ display: 'grid', gridTemplateColumns: topMissing.length > 0 && topPair ? '1fr 1fr' : '1fr', gap: '12px' }}>

        {/* Missing values highlight */}
        {topMissing.length > 0 && (
          <div style={{ background: C.bg, border: `1px solid ${C.border}`, borderRadius: '10px', padding: '13px 14px' }}>
            <div style={{ fontSize: '0.6rem', color: C.textMuted, fontWeight: '700', textTransform: 'uppercase', letterSpacing: '0.08em', marginBottom: '9px' }}>Top Missing</div>
            <div style={{ display: 'flex', flexDirection: 'column', gap: '5px' }}>
              {topMissing.map(([col, cnt]) => {
                const p = Math.min(1, cnt / (ds.row_count || 1))
                return (
                  <div key={col}>
                    <div style={{ display: 'flex', justifyContent: 'space-between', fontSize: '0.72rem', marginBottom: '2px' }}>
                      <span style={{ color: C.text, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', maxWidth: '60%', fontFamily: MONO }}>{col}</span>
                      <span style={{ color: '#f59e0b', fontWeight: '600', fontFamily: MONO }}>{(p * 100).toFixed(1)}%</span>
                    </div>
                    <div style={{ height: '3px', borderRadius: '2px', background: C.border }}>
                      <div style={{ height: '100%', borderRadius: '2px', background: '#f59e0b', width: `${Math.max(3, p * 100)}%` }} />
                    </div>
                  </div>
                )
              })}
            </div>
          </div>
        )}

        {/* Top correlation */}
        {topPair && (
          <div style={{ background: C.bg, border: `1px solid ${C.border}`, borderRadius: '10px', padding: '13px 14px' }}>
            <div style={{ fontSize: '0.6rem', color: C.textMuted, fontWeight: '700', textTransform: 'uppercase', letterSpacing: '0.08em', marginBottom: '9px' }}>Strongest Correlation</div>
            <div style={{ fontSize: '1.4rem', fontWeight: '800', fontFamily: MONO, color: topPair.correlation >= 0 ? '#6366f1' : '#ef4444', letterSpacing: '-0.03em', marginBottom: '4px' }}>
              {topPair.correlation > 0 ? '+' : ''}{topPair.correlation.toFixed(3)}
            </div>
            <div style={{ fontSize: '0.72rem', color: C.textSec, fontFamily: MONO, lineHeight: 1.5 }}>
              <span style={{ color: C.text }}>{topPair.column_a}</span>
              <span style={{ color: C.textMuted }}> ↔ </span>
              <span style={{ color: C.text }}>{topPair.column_b}</span>
            </div>
            <div style={{ marginTop: '4px' }}>
              <span style={{
                fontSize: '0.62rem', padding: '1px 6px', borderRadius: '3px', fontWeight: '600',
                background: topPair.strength === 'strong' ? '#6366f11a' : '#f59e0b1a',
                color: topPair.strength === 'strong' ? '#6366f1' : '#f59e0b',
              }}>{topPair.strength}</span>
            </div>
          </div>
        )}
      </div>

      {/* Generate Report button */}
      {onGenerateReport && (
        <div style={{ borderTop: `1px solid ${C.border}`, paddingTop: '16px' }}>
          <button
            onClick={onGenerateReport}
            style={{ background: 'linear-gradient(135deg,#6366f1 0%,#8b5cf6 100%)', color: '#fff', border: 'none', borderRadius: '8px', padding: '9px 20px', fontSize: '0.8rem', fontWeight: '600', cursor: 'pointer', fontFamily: FONT }}
          >
            {hasReport ? 'Regenerate Report' : 'Generate Quick Report'}
          </button>
        </div>
      )}

      {/* Legacy report display */}
      {report && (
        <div style={{ borderTop: `1px solid ${C.border}`, paddingTop: '16px' }}>
          <SecLabel text="Quick Report" C={C} />
          {report.map((section, i) => (
            <div key={i} style={{ marginBottom: i < report.length - 1 ? '14px' : 0 }}>
              <div style={{ fontSize: '0.62rem', color: C.textSec, fontWeight: '700', textTransform: 'uppercase', letterSpacing: '0.08em', marginBottom: '7px' }}>{section.heading}</div>
              <div style={{ display: 'flex', flexDirection: 'column', gap: '5px' }}>
                {(section.items || []).map((item, j) => (
                  <div key={j} style={{ display: 'flex', alignItems: 'flex-start', gap: '8px', fontSize: '0.8rem', color: C.text, lineHeight: 1.6 }}>
                    <span style={{ color: C.accent, flexShrink: 0, fontWeight: '700' }}>→</span>
                    <span>{item}</span>
                  </div>
                ))}
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  )
}

// ─── Tab: Columns ─────────────────────────────────────────────────────────────
function ColumnsTab({ ds, C }) {
  const [search, setSearch] = useState('')
  const cols = ds.columns || []
  const filtered = cols.filter(c => !search || c.toLowerCase().includes(search.toLowerCase()))

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: '12px' }}>

      <input
        value={search}
        onChange={e => setSearch(e.target.value)}
        placeholder="Search columns…"
        style={{ background: C.bg, border: `1px solid ${C.border}`, borderRadius: '8px', padding: '7px 12px', fontSize: '0.8rem', color: C.text, fontFamily: FONT, outline: 'none', width: '100%', boxSizing: 'border-box' }}
      />

      <div style={{ display: 'flex', flexDirection: 'column', gap: '7px' }}>
        {filtered.length === 0 && (
          <div style={{ textAlign: 'center', color: C.textMuted, fontSize: '0.8rem', padding: '20px 0' }}>No columns match "{search}"</div>
        )}
        {filtered.map(col => {
          const type      = getColType(col, ds)
          const missCnt   = (ds.missing_values || {})[col] || 0
          const missPct   = ds.row_count > 0 ? (missCnt / ds.row_count) * 100 : 0
          const numStats  = (ds.numeric_profile || {})[col]
          const catData   = (ds.categorical_profile || {})[col] || []
          const catMeta   = (ds.categorical_meta || {})[col]
          const distinctN = catMeta?.distinct_count
          const dateInfo  = (ds.date_profile?.date_columns || []).find(d => d.column === col)

          return (
            <div key={col} style={{ background: C.bg, border: `1px solid ${C.border}`, borderRadius: '10px', padding: '12px 14px' }}>
              <div style={{ display: 'flex', alignItems: 'center', gap: '8px', marginBottom: '8px' }}>
                <TypeBadge type={type} />
                <span style={{ fontSize: '0.82rem', fontWeight: '600', color: C.text, fontFamily: MONO, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', flex: 1 }}>{col}</span>
                {missCnt > 0 && (
                  <span style={{ fontSize: '0.65rem', fontWeight: '600', color: '#f59e0b', background: '#f59e0b1a', padding: '1px 6px', borderRadius: '4px', flexShrink: 0 }}>
                    {missPct.toFixed(1)}% missing
                  </span>
                )}
              </div>

              <div style={{ display: 'flex', gap: '16px', flexWrap: 'wrap' }}>
                <span style={{ fontSize: '0.68rem', color: C.textMuted }}>
                  Missing: <span style={{ color: missCnt > 0 ? '#f59e0b' : C.success, fontWeight: '500' }}>{missCnt.toLocaleString()}</span>
                </span>
                {distinctN != null && (
                  <span style={{ fontSize: '0.68rem', color: C.textMuted }}>
                    Distinct: <span style={{ color: C.textSec, fontWeight: '500' }}>{distinctN.toLocaleString()}</span>
                  </span>
                )}
              </div>

              {/* Numeric stats */}
              {type === 'numeric' && numStats && (
                <div style={{ display: 'grid', gridTemplateColumns: 'repeat(4, 1fr)', gap: '6px', marginTop: '8px' }}>
                  {[
                    { label: 'Min',    val: numStats.min    },
                    { label: 'Max',    val: numStats.max    },
                    { label: 'Mean',   val: numStats.mean   },
                    { label: 'Std',    val: numStats.std    },
                  ].map(({ label, val }) => (
                    <div key={label} style={{ background: C.surface, border: `1px solid ${C.border}`, borderRadius: '6px', padding: '5px 8px' }}>
                      <div style={{ fontSize: '0.57rem', color: C.textMuted, textTransform: 'uppercase', letterSpacing: '0.07em', marginBottom: '2px' }}>{label}</div>
                      <div style={{ fontSize: '0.74rem', fontWeight: '600', color: C.text, fontFamily: MONO }}>{fmtN(val)}</div>
                    </div>
                  ))}
                </div>
              )}
              {type === 'numeric' && numStats?.outlier_count > 0 && (
                <div style={{ marginTop: '5px', fontSize: '0.67rem', color: '#ef4444' }}>
                  ⚠ {numStats.outlier_count} outlier{numStats.outlier_count !== 1 ? 's' : ''} ({pct(numStats.outlier_count, ds.row_count)})
                </div>
              )}

              {/* Categorical top values */}
              {type === 'categorical' && catData.length > 0 && (
                <div style={{ marginTop: '8px', display: 'flex', gap: '5px', flexWrap: 'wrap' }}>
                  {catData.slice(0, 5).map(({ value, count }) => (
                    <span key={value} style={{ fontSize: '0.67rem', padding: '2px 7px', borderRadius: '4px', background: C.surface, border: `1px solid ${C.border}`, color: C.textSec, fontFamily: MONO }}>
                      {String(value).length > 18 ? String(value).slice(0, 17) + '…' : value}
                      <span style={{ marginLeft: '4px', color: C.textMuted }}>×{count.toLocaleString()}</span>
                    </span>
                  ))}
                </div>
              )}

              {/* Date info */}
              {type === 'date' && dateInfo && (
                <div style={{ marginTop: '8px', fontSize: '0.7rem', color: C.textSec, fontFamily: MONO }}>
                  {dateInfo.earliest?.slice(0, 10)} → {dateInfo.latest?.slice(0, 10)}
                  {dateInfo.date_range_days != null && ` (${dateInfo.date_range_days} days)`}
                </div>
              )}
            </div>
          )
        })}
      </div>
    </div>
  )
}

// ─── Tab: Correlations ────────────────────────────────────────────────────────
function CorrelationsTab({ ds, C }) {
  const [pairSearch, setPairSearch] = useState('')
  const pairs  = ds.correlation_profile || []
  const chart  = useMemo(() => buildCorrChart(pairs), [pairs])

  if (!pairs.length) {
    return (
      <div style={{ textAlign: 'center', color: C.textMuted, fontSize: '0.82rem', padding: '32px 0' }}>
        No correlation data available.<br />
        <span style={{ fontSize: '0.74rem' }}>Dataset needs ≥ 2 numeric columns.</span>
      </div>
    )
  }

  const q = pairSearch.toLowerCase()
  const filteredPairs = pairs.filter(p =>
    !q || p.column_a.toLowerCase().includes(q) || p.column_b.toLowerCase().includes(q)
  )

  const positive = filteredPairs.filter(p => p.correlation > 0).slice(0, 6)
  const negative = filteredPairs.filter(p => p.correlation < 0).slice(0, 6)

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: '18px' }}>

      {/* Correlation matrix */}
      {chart && (
        <div>
          <SecLabel text="Correlation Matrix" C={C} />
          <ChartSection chart={chart} C={C} />
        </div>
      )}

      {/* Pair search */}
      <input
        value={pairSearch}
        onChange={e => setPairSearch(e.target.value)}
        placeholder="Search columns in pairs…"
        style={{ background: C.bg, border: `1px solid ${C.border}`, borderRadius: '8px', padding: '7px 12px', fontSize: '0.8rem', color: C.text, fontFamily: FONT, outline: 'none', width: '100%', boxSizing: 'border-box' }}
      />

      {/* Positive & negative lists */}
      <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '12px' }}>

        <div>
          <SecLabel text={`Strongest Positive (${positive.length})`} C={C} />
          <div style={{ display: 'flex', flexDirection: 'column', gap: '5px' }}>
            {positive.length === 0 && <div style={{ fontSize: '0.74rem', color: C.textMuted }}>None found.</div>}
            {positive.map((p, i) => (
              <div key={i} style={{ background: C.bg, border: `1px solid #6366f120`, borderLeft: '3px solid #6366f1', borderRadius: '8px', padding: '8px 11px' }}>
                <div style={{ fontSize: '1rem', fontWeight: '800', color: '#6366f1', fontFamily: MONO, letterSpacing: '-0.02em' }}>
                  +{p.correlation.toFixed(3)}
                </div>
                <div style={{ fontSize: '0.68rem', color: C.textSec, fontFamily: MONO, lineHeight: 1.4 }}>
                  {p.column_a} ↔ {p.column_b}
                </div>
                <span style={{ fontSize: '0.6rem', color: '#6366f1', fontWeight: '600' }}>{p.strength}</span>
              </div>
            ))}
          </div>
        </div>

        <div>
          <SecLabel text={`Strongest Negative (${negative.length})`} C={C} />
          <div style={{ display: 'flex', flexDirection: 'column', gap: '5px' }}>
            {negative.length === 0 && <div style={{ fontSize: '0.74rem', color: C.textMuted }}>None found.</div>}
            {negative.map((p, i) => (
              <div key={i} style={{ background: C.bg, border: `1px solid #ef444420`, borderLeft: '3px solid #ef4444', borderRadius: '8px', padding: '8px 11px' }}>
                <div style={{ fontSize: '1rem', fontWeight: '800', color: '#ef4444', fontFamily: MONO, letterSpacing: '-0.02em' }}>
                  {p.correlation.toFixed(3)}
                </div>
                <div style={{ fontSize: '0.68rem', color: C.textSec, fontFamily: MONO, lineHeight: 1.4 }}>
                  {p.column_a} ↔ {p.column_b}
                </div>
                <span style={{ fontSize: '0.6rem', color: '#ef4444', fontWeight: '600' }}>{p.strength}</span>
              </div>
            ))}
          </div>
        </div>
      </div>
    </div>
  )
}

// ─── Tab: Quality ─────────────────────────────────────────────────────────────
const ISSUE_META = {
  severe_missing:    { label: col => `${col} — >50% missing`,     color: '#ef4444', icon: '⚠' },
  high_missing:      { label: col => `${col} — >20% missing`,     color: '#f97316', icon: '⚠' },
  moderate_missing:  { label: col => `${col} — >5% missing`,      color: '#f59e0b', icon: '⚑' },
  high_outliers:     { label: col => `${col} — >10% outliers`,    color: '#f97316', icon: '⊙' },
  moderate_outliers: { label: col => `${col} — >5% outliers`,     color: '#f59e0b', icon: '⊙' },
  constant_column:   { label: col => `${col} — constant values`,  color: '#6b7280', icon: '≡' },
  few_rows:          { label: (_, v) => `Only ${v} rows`,         color: '#ef4444', icon: '↕' },
  small_dataset:     { label: (_, v) => `${v} rows (small)`,      color: '#f59e0b', icon: '↕' },
}

function QualityTab({ ds, quality, C }) {
  const { score, grade, gradeColor, label, issues } = quality
  const rowCount = ds.row_count || 1

  // Missing ranking — all columns, sorted by missing %
  const missingRanked = Object.entries(ds.missing_values || {})
    .map(([col, cnt]) => ({ col, cnt, pct: cnt / rowCount }))
    .sort((a, b) => b.pct - a.pct)
    .filter(({ cnt }) => cnt > 0)

  // Outlier ranking
  const outlierRanked = Object.entries(ds.numeric_profile || {})
    .filter(([, s]) => s.outlier_count > 0)
    .map(([col, s]) => ({ col, cnt: s.outlier_count, pct: s.outlier_count / rowCount }))
    .sort((a, b) => b.pct - a.pct)
    .slice(0, 8)

  const gradeDesc = {
    A: 'The dataset is in excellent condition. Very few issues detected.',
    B: 'The dataset is in good condition with minor issues.',
    C: 'The dataset has notable issues that may affect analysis quality.',
    D: 'Significant data quality problems detected. Review recommended.',
    F: 'Critical quality issues. This dataset requires substantial cleaning.',
  }

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: '18px' }}>

      {/* Score hero */}
      <div style={{ background: C.bg, border: `1px solid ${C.border}`, borderRadius: '12px', padding: '18px 20px', display: 'flex', alignItems: 'center', gap: '20px' }}>
        <ScoreCircle score={score} grade={grade} color={gradeColor} size={80} />
        <div>
          <div style={{ fontSize: '1.1rem', fontWeight: '700', color: gradeColor, marginBottom: '4px' }}>{label}</div>
          <div style={{ fontSize: '0.75rem', color: C.textSec, lineHeight: 1.55, maxWidth: '340px' }}>{gradeDesc[grade]}</div>
          <div style={{ marginTop: '8px', fontSize: '0.68rem', color: C.textMuted }}>
            {issues.length === 0 ? 'No issues detected.' : `${issues.length} issue${issues.length !== 1 ? 's' : ''} detected`}
          </div>
        </div>
      </div>

      {/* Issues */}
      {issues.length > 0 && (
        <div>
          <SecLabel text="Issues Detected" C={C} />
          <div style={{ display: 'flex', flexDirection: 'column', gap: '5px' }}>
            {issues.map((iss, i) => {
              const meta = ISSUE_META[iss.type] || { label: () => iss.type, color: C.textMuted, icon: '•' }
              return (
                <div key={i} style={{ display: 'flex', alignItems: 'center', gap: '10px', background: C.bg, border: `1px solid ${meta.color}25`, borderLeft: `3px solid ${meta.color}`, borderRadius: '8px', padding: '8px 12px' }}>
                  <span style={{ fontSize: '0.8rem', flexShrink: 0, color: meta.color }}>{meta.icon}</span>
                  <span style={{ fontSize: '0.76rem', color: C.text }}>{meta.label(iss.col, iss.val)}</span>
                  {iss.pct != null && (
                    <span style={{ marginLeft: 'auto', fontSize: '0.68rem', fontWeight: '600', color: meta.color, fontFamily: MONO, flexShrink: 0 }}>
                      {(iss.pct * 100).toFixed(1)}%
                    </span>
                  )}
                </div>
              )
            })}
          </div>
        </div>
      )}

      {/* Missing value ranking */}
      {missingRanked.length > 0 && (
        <div>
          <SecLabel text={`Missing Values — ${missingRanked.length} affected column${missingRanked.length !== 1 ? 's' : ''}`} C={C} />
          <div style={{ display: 'flex', flexDirection: 'column', gap: '5px' }}>
            {missingRanked.map(({ col, cnt, pct: p }) => (
              <div key={col} style={{ background: C.bg, border: `1px solid ${C.border}`, borderRadius: '8px', padding: '8px 12px' }}>
                <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '4px' }}>
                  <span style={{ fontSize: '0.76rem', color: C.text, fontFamily: MONO, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', maxWidth: '70%' }}>{col}</span>
                  <div style={{ display: 'flex', gap: '10px', alignItems: 'center', flexShrink: 0 }}>
                    <span style={{ fontSize: '0.68rem', color: C.textMuted }}>{cnt.toLocaleString()} rows</span>
                    <span style={{ fontSize: '0.72rem', fontWeight: '700', color: p > 0.2 ? '#ef4444' : p > 0.05 ? '#f59e0b' : C.textSec, fontFamily: MONO }}>
                      {(p * 100).toFixed(1)}%
                    </span>
                  </div>
                </div>
                <div style={{ height: '3px', borderRadius: '2px', background: C.border }}>
                  <div style={{ height: '100%', borderRadius: '2px', width: `${Math.max(2, p * 100)}%`, background: p > 0.2 ? '#ef4444' : p > 0.05 ? '#f59e0b' : '#10b981' }} />
                </div>
              </div>
            ))}
          </div>
        </div>
      )}

      {/* Outlier ranking */}
      {outlierRanked.length > 0 && (
        <div>
          <SecLabel text="Outlier-Heavy Columns" C={C} />
          <div style={{ display: 'flex', flexDirection: 'column', gap: '5px' }}>
            {outlierRanked.map(({ col, cnt, pct: p }) => (
              <div key={col} style={{ display: 'flex', alignItems: 'center', gap: '12px', background: C.bg, border: `1px solid ${C.border}`, borderRadius: '8px', padding: '8px 12px' }}>
                <span style={{ fontSize: '0.76rem', color: C.text, fontFamily: MONO, flex: 1, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{col}</span>
                <span style={{ fontSize: '0.68rem', color: C.textMuted, flexShrink: 0 }}>{cnt.toLocaleString()} outliers</span>
                <span style={{ fontSize: '0.72rem', fontWeight: '700', color: p > 0.1 ? '#ef4444' : '#f59e0b', fontFamily: MONO, flexShrink: 0 }}>
                  {(p * 100).toFixed(1)}%
                </span>
              </div>
            ))}
          </div>
        </div>
      )}

      {issues.length === 0 && missingRanked.length === 0 && outlierRanked.length === 0 && (
        <div style={{ textAlign: 'center', padding: '24px 0', color: C.success }}>
          <div style={{ fontSize: '1.4rem', marginBottom: '6px' }}>✓</div>
          <div style={{ fontSize: '0.82rem' }}>No quality issues detected.</div>
        </div>
      )}
    </div>
  )
}

// ─── Tab: Preview ─────────────────────────────────────────────────────────────
function PreviewTab({ ds, C }) {
  const rows = ds.sample_rows || []
  const cols = ds.columns || []

  if (rows.length === 0) {
    return (
      <div style={{ textAlign: 'center', color: C.textMuted, fontSize: '0.82rem', padding: '32px 0' }}>
        No preview rows available.
      </div>
    )
  }

  return (
    <div>
      <SecLabel text={`Sample Rows (${rows.length})`} C={C} />
      <div style={{ overflowX: 'auto', borderRadius: '8px', border: `1px solid ${C.border}` }}>
        <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: '0.77rem', fontFamily: MONO }}>
          <thead>
            <tr style={{ background: C.bg }}>
              {cols.map(col => (
                <th key={col} style={{ padding: '9px 14px', textAlign: 'left', color: C.textSec, fontWeight: '600', borderBottom: `1px solid ${C.border}`, whiteSpace: 'nowrap' }}>
                  {col}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {rows.map((row, i) => (
              <tr key={i} style={{ background: i % 2 === 1 ? `${C.border}18` : 'transparent' }}>
                {cols.map(col => {
                  const val = row[col]
                  const isEmpty = val === '' || val == null
                  return (
                    <td key={col} style={{
                      padding: '8px 14px',
                      color: isEmpty ? C.textMuted : C.text,
                      borderBottom: i < rows.length - 1 ? `1px solid ${C.border}` : 'none',
                      whiteSpace: 'nowrap',
                    }}>
                      {isEmpty ? '—' : String(val)}
                    </td>
                  )
                })}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  )
}

// ─── Main component ────────────────────────────────────────────────────────────
export default function DatasetIntelligence({ ds, C, S, onGenerateReport, hasReport, report }) {
  const [tab, setTab] = useState('overview')
  const quality = useMemo(() => computeQuality(ds), [ds])

  const TABS = [
    { id: 'overview',     label: 'Overview'      },
    { id: 'columns',      label: 'Columns'       },
    { id: 'correlations', label: 'Correlations'  },
    { id: 'quality',      label: `Quality · ${quality.grade}` },
    { id: 'preview',      label: 'Preview'       },
  ]

  const TAB_ICONS = {
    overview:     <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><rect x="3" y="3" width="7" height="7"/><rect x="14" y="3" width="7" height="7"/><rect x="14" y="14" width="7" height="7"/><rect x="3" y="14" width="7" height="7"/></svg>,
    columns:      <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><line x1="8" y1="6" x2="21" y2="6"/><line x1="8" y1="12" x2="21" y2="12"/><line x1="8" y1="18" x2="21" y2="18"/><line x1="3" y1="6" x2="3.01" y2="6"/><line x1="3" y1="12" x2="3.01" y2="12"/><line x1="3" y1="18" x2="3.01" y2="18"/></svg>,
    correlations: <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><circle cx="18" cy="18" r="3"/><circle cx="6" cy="6" r="3"/><path d="m13.5 12.5-7 7"/><path d="m10.5 11.5 7-7"/></svg>,
    quality:      <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/></svg>,
    preview:      <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><rect x="3" y="3" width="18" height="18" rx="2"/><line x1="3" y1="9" x2="21" y2="9"/><line x1="3" y1="15" x2="21" y2="15"/><line x1="9" y1="9" x2="9" y2="21"/></svg>,
  }

  return (
    <div style={{ background: C.surface, border: `1px solid ${C.border}`, borderRadius: '14px', overflow: 'hidden' }}>

      {/* Header */}
      <div style={{ padding: '16px 20px 0', background: C.bg, borderBottom: `1px solid ${C.border}` }}>
        <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: '12px' }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: '10px', minWidth: 0 }}>
            <div style={{ width: '32px', height: '32px', borderRadius: '8px', background: '#6366f11a', border: '1px solid #6366f125', display: 'flex', alignItems: 'center', justifyContent: 'center', flexShrink: 0 }}>
              <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="#6366f1" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><ellipse cx="12" cy="5" rx="9" ry="3"/><path d="M21 12c0 1.66-4 3-9 3s-9-1.34-9-3"/><path d="M3 5v14c0 1.66 4 3 9 3s9-1.34 9-3V5"/></svg>
            </div>
            <div style={{ minWidth: 0 }}>
              <div style={{ fontSize: '0.6rem', color: C.textMuted, textTransform: 'uppercase', letterSpacing: '0.1em', fontWeight: '600' }}>Dataset Intelligence</div>
              <div style={{ fontSize: '0.88rem', fontWeight: '700', color: C.text, fontFamily: MONO, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{ds.filename}</div>
            </div>
          </div>
          <div style={{ display: 'flex', alignItems: 'center', gap: '10px', flexShrink: 0 }}>
            {ds.uploaded_at && (
              <span style={{ fontSize: '0.67rem', color: C.textMuted }}>{new Date(ds.uploaded_at).toLocaleDateString()}</span>
            )}
            <ScoreCircle score={quality.score} grade={quality.grade} color={quality.gradeColor} size={52} />
          </div>
        </div>

        {/* Tab navigation */}
        <div style={{ display: 'flex', gap: '2px' }}>
          {TABS.map(t => {
            const active = tab === t.id
            return (
              <button key={t.id} onClick={() => setTab(t.id)} style={{
                display: 'flex', alignItems: 'center', gap: '5px',
                padding: '7px 14px', fontSize: '0.76rem', fontWeight: active ? '600' : '400',
                color: active ? '#6366f1' : C.textSec,
                background: 'transparent', border: 'none', cursor: 'pointer',
                borderBottom: active ? '2px solid #6366f1' : '2px solid transparent',
                marginBottom: '-1px', fontFamily: FONT, borderRadius: '0', whiteSpace: 'nowrap',
                transition: 'color 0.1s',
              }}>
                <span style={{ color: active ? '#6366f1' : C.textMuted }}>{TAB_ICONS[t.id]}</span>
                {t.label}
              </button>
            )
          })}
        </div>
      </div>

      {/* Tab content */}
      <div style={{ padding: '20px' }}>
        {tab === 'overview'     && <OverviewTab     ds={ds} quality={quality} C={C} onGenerateReport={onGenerateReport} hasReport={hasReport} report={report} />}
        {tab === 'columns'      && <ColumnsTab       ds={ds} C={C} />}
        {tab === 'correlations' && <CorrelationsTab  ds={ds} C={C} />}
        {tab === 'quality'      && <QualityTab        ds={ds} quality={quality} C={C} />}
        {tab === 'preview'      && <PreviewTab        ds={ds} C={C} />}
      </div>
    </div>
  )
}
