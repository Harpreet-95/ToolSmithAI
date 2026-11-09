import { FONT } from './shared'

const PRIORITY_META = {
  HIGH:   { color: '#ef4444', label: 'High priority' },
  MEDIUM: { color: '#f59e0b', label: 'Medium priority' },
  LOW:    { color: '#94a3b8', label: 'Low priority' },
}

// Milestone 7 Part 3 "Recommended Actions" — renders report.recommendations
// (core.analysis.models.AnalysisRecommendation dicts) verbatim.
export default function RecommendationsPanel({ recommendations, C }) {
  if (!recommendations?.length) return null
  return (
    <section style={{ marginBottom: '20px' }}>
      <h3 style={{ margin: '0 0 12px', fontSize: '0.95rem', fontWeight: '800', color: C.text, fontFamily: FONT }}>Recommended Actions</h3>
      <div style={{ display: 'flex', flexDirection: 'column', gap: '10px' }}>
        {recommendations.map(r => {
          const meta = PRIORITY_META[r.priority] || PRIORITY_META.LOW
          return (
            <div key={r.recommendation_id} style={{
              background: C.surface, border: `1px solid ${C.border}`, borderLeft: `3px solid ${meta.color}`,
              borderRadius: '10px', padding: '14px 16px',
            }}>
              <div style={{ display: 'flex', alignItems: 'center', gap: '8px', marginBottom: '6px' }}>
                <span style={{ fontSize: '0.6rem', fontWeight: '800', color: meta.color, textTransform: 'uppercase', letterSpacing: '0.06em' }}>{meta.label}</span>
              </div>
              <div style={{ fontSize: '0.85rem', fontWeight: '600', color: C.text, lineHeight: 1.5, marginBottom: r.rationale ? '4px' : 0 }}>{r.recommendation}</div>
              {r.rationale && (
                <div style={{ fontSize: '0.76rem', color: C.textSec, lineHeight: 1.5 }}>{r.rationale}</div>
              )}
              {r.limitation && (
                <div style={{ marginTop: '6px', fontSize: '0.68rem', color: C.textMuted }}>{r.limitation}</div>
              )}
            </div>
          )
        })}
      </div>
    </section>
  )
}
