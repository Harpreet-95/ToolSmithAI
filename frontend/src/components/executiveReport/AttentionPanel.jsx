import { FONT } from './shared'

// Milestone 7 Part 3 "Areas Requiring Attention" — every string here already
// passed through core.analysis.management_language.classify_evidence_
// limitation on the backend, so it is guaranteed management-safe (no SQL/
// stop_code/internal exception text). This component only lays the
// already-safe sentences out; it performs no text transformation itself.
export default function AttentionPanel({ items, C }) {
  if (!items?.length) return null
  return (
    <section style={{ marginBottom: '20px' }}>
      <h3 style={{ margin: '0 0 12px', fontSize: '0.95rem', fontWeight: '800', color: C.text, fontFamily: FONT }}>Areas Requiring Attention</h3>
      <div style={{ background: 'rgba(245,158,11,0.06)', border: '1px solid rgba(245,158,11,0.22)', borderRadius: '12px', padding: '14px 16px', display: 'flex', flexDirection: 'column', gap: '8px' }}>
        {items.map((item, i) => (
          <div key={i} style={{ display: 'flex', gap: '8px', alignItems: 'flex-start', fontSize: '0.8rem', color: C.text, lineHeight: 1.5 }}>
            <span style={{ color: '#f59e0b', flexShrink: 0, marginTop: '1px' }} aria-hidden="true">&#9432;</span>
            <span>{item}</span>
          </div>
        ))}
      </div>
    </section>
  )
}
