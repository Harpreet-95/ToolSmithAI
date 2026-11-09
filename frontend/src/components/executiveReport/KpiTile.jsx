import { DIRECTION_META, FONT, fmtKpiValue, fmtPercent, looksLikeCurrency } from './shared'

// A single Business Snapshot KPI card (Milestone 7 Part 4). Renders exactly
// the fields a normalized KPI (see utils/executiveReport.js normalizeKpi)
// carries — never a fabricated secondary stat. `size` controls the featured
// (large, top-of-page) vs standard tile treatment.
export default function KpiTile({ kpi, C, size = 'standard' }) {
  if (!kpi) return null
  const dir = DIRECTION_META[kpi.direction] || DIRECTION_META.unknown
  const isCurrency = looksLikeCurrency(kpi.title, kpi.unit)
  // Live validation (Tuesday demo sanity check) found kpi.formattedValue
  // (core.analysis.models.KPIItem.formatted_value) is the evidence's full
  // natural-language answer SENTENCE (e.g. "There are 12,000 students in
  // the database."), never a short display-ready value — using it here
  // showed a run-on sentence instead of a number. The big value must always
  // come from kpi.value itself; formattedValue is only ever safe to mine
  // for title humanization (see utils/executiveReport.js).
  const displayValue = fmtKpiValue(kpi.value, { unit: kpi.unit, isCurrency })
  const pctStr = fmtPercent(kpi.percentChange)
  const large = size === 'featured'

  return (
    <div
      className="ws-kpi-card"
      style={{
        background: C.card, border: `1px solid ${C.border}`, borderRadius: '14px',
        padding: large ? '22px 22px 20px' : '16px 18px', flex: '1 1 200px', minWidth: large ? '210px' : '180px',
        display: 'flex', flexDirection: 'column', gap: large ? '10px' : '8px',
      }}
    >
      <div style={{
        fontSize: '0.68rem', color: C.textMuted, fontWeight: '700', textTransform: 'uppercase',
        letterSpacing: '0.06em', fontFamily: FONT,
      }}>
        {kpi.title}
      </div>

      <div style={{ fontSize: large ? '2.1rem' : '1.5rem', fontWeight: '800', color: C.text, letterSpacing: '-0.5px', lineHeight: 1.1 }}>
        {displayValue}
      </div>

      {kpi.previousValue != null && (
        <div style={{ display: 'flex', alignItems: 'center', gap: '6px', fontSize: '0.76rem', fontWeight: '700', color: dir.color }}>
          <span aria-hidden="true">{dir.symbol}</span>
          <span>{pctStr ?? 'n/a'}</span>
          {kpi.comparisonLabel && (
            <span style={{ color: C.textMuted, fontWeight: '500' }}>{kpi.comparisonLabel}</span>
          )}
        </div>
      )}

      {kpi.limitation && (
        <div style={{ fontSize: '0.66rem', color: C.textMuted }}>{kpi.limitation}</div>
      )}
      {kpi.warning && !kpi.limitation && (
        <div style={{ fontSize: '0.66rem', color: '#b45309' }}>{kpi.warning}</div>
      )}
    </div>
  )
}
