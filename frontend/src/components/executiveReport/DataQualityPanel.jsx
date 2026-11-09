import { FONT } from './shared'

// Milestone 7 Part 3 "Data Quality & Coverage" — deliberately secondary
// (Part 3: "This should be secondary information. Do not let DQ warnings
// dominate the dashboard."). Renders report.data_quality_overview (a single
// grounded paragraph) plus a small count of supporting notes, collapsed
// rather than a large card grid.
export default function DataQualityPanel({ overview, notes, C }) {
  if (!overview && !notes?.length) return null
  return (
    <section style={{ marginBottom: '20px' }}>
      <h4 style={{ margin: '0 0 8px', fontSize: '0.78rem', fontWeight: '700', color: C.textMuted, textTransform: 'uppercase', letterSpacing: '0.05em', fontFamily: FONT }}>
        Data Quality &amp; Coverage
      </h4>
      <div style={{ background: C.card, border: `1px solid ${C.border}`, borderRadius: '10px', padding: '12px 14px' }}>
        {overview && (
          <div style={{ fontSize: '0.76rem', color: C.textSec, lineHeight: 1.55 }}>{overview}</div>
        )}
        {notes?.length > 0 && (
          <div style={{ marginTop: overview ? '8px' : 0, fontSize: '0.7rem', color: C.textMuted, display: 'flex', flexDirection: 'column', gap: '3px' }}>
            {notes.slice(0, 5).map((n, i) => <div key={i}>· {n}</div>)}
            {notes.length > 5 && <div>+ {notes.length - 5} more</div>}
          </div>
        )}
      </div>
    </section>
  )
}
