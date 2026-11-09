// Milestone 7 Part 7 — report.executive_summary rendered verbatim in a
// compact panel near the top. Never regenerated/rewritten (no frontend AI
// call); this only lays existing text out.
export default function ExecutiveSummaryPanel({ summary, businessOverview, C }) {
  if (!summary && !businessOverview) return null
  return (
    <div style={{
      background: C.card, border: `1px solid ${C.border}`, borderRadius: '12px',
      padding: '16px 18px', marginBottom: '22px',
    }}>
      {summary && (
        <div style={{ fontSize: '0.85rem', color: C.text, lineHeight: 1.6, fontWeight: '500' }}>{summary}</div>
      )}
      {businessOverview && (
        <div style={{ fontSize: '0.76rem', color: C.textSec, lineHeight: 1.6, marginTop: summary ? '8px' : 0 }}>{businessOverview}</div>
      )}
    </div>
  )
}
