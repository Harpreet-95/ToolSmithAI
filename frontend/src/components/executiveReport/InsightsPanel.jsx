import { FONT } from './shared'

const IMPACT_META = {
  POSITIVE_DEVELOPMENT: { color: '#10b981', bg: 'rgba(16,185,129,0.08)', label: 'Positive' },
  ATTENTION_REQUIRED:   { color: '#f59e0b', bg: 'rgba(245,158,11,0.08)', label: 'Attention' },
  NEUTRAL_CHANGE:       { color: '#6366f1', bg: 'rgba(99,102,241,0.08)', label: 'Change' },
  UNKNOWN_BUSINESS_IMPACT: { color: '#94a3b8', bg: 'rgba(148,163,184,0.08)', label: null },
}

// Milestone 7 Part 3 "Management Insights" — renders report.insights
// (core.analysis.models.AnalysisFinding dicts, plain-KPI findings already
// excluded backend-side). Each card shows the finding's own management-
// facing statement text verbatim; nothing here rewrites or infers wording.
export default function InsightsPanel({ insights, C }) {
  if (!insights?.length) return null
  return (
    <section style={{ marginBottom: '20px' }}>
      <h3 style={{ margin: '0 0 12px', fontSize: '0.95rem', fontWeight: '800', color: C.text, fontFamily: FONT }}>Management Insights</h3>
      <div style={{ display: 'flex', flexDirection: 'column', gap: '10px' }}>
        {insights.map(f => {
          const meta = IMPACT_META[f.business_impact] || IMPACT_META.UNKNOWN_BUSINESS_IMPACT
          return (
            <div key={f.finding_id} className="ws-insight-card" style={{ background: C.surface, border: `1px solid ${C.border}`, borderRadius: '12px', padding: '14px 16px' }}>
              <div style={{ display: 'flex', alignItems: 'center', gap: '8px', marginBottom: '6px', flexWrap: 'wrap' }}>
                <span style={{ fontSize: '0.85rem', fontWeight: '700', color: C.text }}>{f.title}</span>
                {meta.label && (
                  <span style={{ background: meta.bg, color: meta.color, borderRadius: '20px', padding: '1px 9px', fontSize: '0.6rem', fontWeight: '700', textTransform: 'uppercase', letterSpacing: '0.04em' }}>
                    {meta.label}
                  </span>
                )}
              </div>
              <div style={{ fontSize: '0.8rem', color: C.textSec, lineHeight: 1.55 }}>{f.statement}</div>
              {f.limitation && (
                <div style={{ marginTop: '6px', fontSize: '0.68rem', color: C.textMuted }}>{f.limitation}</div>
              )}
            </div>
          )
        })}
      </div>
    </section>
  )
}
