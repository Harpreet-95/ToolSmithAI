import { DIRECTION_META, FONT, fmtPercent, fmtPlainValue } from './shared'

// Milestone 7 Part 3 "Key Trends" — a visually clear before/after treatment
// for KPIs the backend already computed a period comparison for (Users:
// 167 → 10, ↓94%). Never recomputes a trend; `trends` are normalized KPIs
// (see utils/executiveReport.trendKpis) whose previous_value/percent_change/
// direction were already produced by the backend's own deterministic
// comparison arithmetic.
function TrendCard({ kpi, C }) {
  const dir = DIRECTION_META[kpi.direction] || DIRECTION_META.unknown
  const pctStr = fmtPercent(kpi.percentChange)
  return (
    <div style={{ background: C.card, border: `1px solid ${C.border}`, borderRadius: '12px', padding: '16px 18px', flex: '1 1 240px', minWidth: '220px' }}>
      <div style={{ fontSize: '0.68rem', color: C.textMuted, fontWeight: '700', textTransform: 'uppercase', letterSpacing: '0.06em', marginBottom: '10px' }}>
        {kpi.title}
      </div>
      <div style={{ display: 'flex', alignItems: 'baseline', gap: '10px', flexWrap: 'wrap' }}>
        <span style={{ fontSize: '1.05rem', fontWeight: '600', color: C.textSec }}>{fmtPlainValue(kpi.previousValue)}</span>
        <span style={{ color: C.textMuted }}>→</span>
        <span style={{ fontSize: '1.3rem', fontWeight: '800', color: C.text }}>{fmtPlainValue(kpi.value)}</span>
      </div>
      <div style={{ marginTop: '8px', display: 'flex', alignItems: 'center', gap: '6px', fontSize: '0.82rem', fontWeight: '700', color: dir.color }}>
        <span aria-hidden="true">{dir.symbol}</span>
        <span>{pctStr ?? 'n/a'}</span>
        {kpi.comparisonLabel && <span style={{ color: C.textMuted, fontWeight: '500' }}>{kpi.comparisonLabel}</span>}
      </div>
    </div>
  )
}

export default function TrendHighlights({ trends, keyChanges, C }) {
  const hasCards = trends?.length > 0
  const extraChangeLines = hasCards
    ? (keyChanges || []).filter(line => !trends.some(t => line.includes(t.title)))
    : (keyChanges || [])

  if (!hasCards && extraChangeLines.length === 0) return null

  return (
    <section style={{ marginBottom: '20px' }}>
      <h3 style={{ margin: '0 0 12px', fontSize: '0.95rem', fontWeight: '800', color: C.text, fontFamily: FONT }}>Key Trends</h3>
      {hasCards && (
        <div style={{ display: 'flex', flexWrap: 'wrap', gap: '12px', marginBottom: extraChangeLines.length ? '10px' : 0 }}>
          {trends.map(kpi => <TrendCard key={kpi.id} kpi={kpi} C={C} />)}
        </div>
      )}
      {extraChangeLines.length > 0 && (
        <div style={{ display: 'flex', flexDirection: 'column', gap: '6px' }}>
          {extraChangeLines.map((line, i) => (
            <div key={i} style={{ fontSize: '0.8rem', color: C.textSec, lineHeight: 1.5 }}>· {line}</div>
          ))}
        </div>
      )}
    </section>
  )
}
