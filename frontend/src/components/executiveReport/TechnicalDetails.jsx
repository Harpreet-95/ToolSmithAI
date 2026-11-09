import { useState } from 'react'
import { FONT } from './shared'

// Milestone 7 Part 10 — technical diagnostics are preserved (never deleted)
// but moved out of the main management flow into a collapsed-by-default
// "Technical Details" disclosure. Renders report.technical_diagnostics
// verbatim ({task_id, question, domain, agent_status, stop_code,
// raw_message, limitation_class} dicts from core.analysis.report_builder)
// plus suggested_follow_up_questions — both real backend output, just not
// management-facing content.
export default function TechnicalDetails({ diagnostics, followUpQuestions, C }) {
  const [open, setOpen] = useState(false)
  const hasDiagnostics = diagnostics?.length > 0
  const hasFollowUps = followUpQuestions?.length > 0
  if (!hasDiagnostics && !hasFollowUps) return null

  return (
    <section style={{ marginTop: '28px', borderTop: `1px solid ${C.border}`, paddingTop: '14px' }}>
      <button
        onClick={() => setOpen(o => !o)}
        aria-expanded={open}
        style={{
          background: 'none', border: 'none', padding: 0, cursor: 'pointer',
          display: 'flex', alignItems: 'center', gap: '6px',
          fontSize: '0.74rem', fontWeight: '700', color: C.textMuted, fontFamily: FONT,
        }}
      >
        <span style={{ display: 'inline-block', transform: open ? 'rotate(90deg)' : 'none', transition: 'transform 0.15s ease' }}>▸</span>
        Technical Details
      </button>

      {open && (
        <div style={{ marginTop: '12px', display: 'flex', flexDirection: 'column', gap: '16px' }}>
          {hasFollowUps && (
            <div>
              <div style={{ fontSize: '0.68rem', fontWeight: '700', color: C.textMuted, textTransform: 'uppercase', letterSpacing: '0.05em', marginBottom: '6px' }}>
                Suggested Follow-up Analysis
              </div>
              <div style={{ display: 'flex', flexDirection: 'column', gap: '4px' }}>
                {followUpQuestions.map((q, i) => (
                  <div key={i} style={{ fontSize: '0.74rem', color: C.textSec }}>· {q}</div>
                ))}
              </div>
            </div>
          )}
          {hasDiagnostics && (
            <div>
              <div style={{ fontSize: '0.68rem', fontWeight: '700', color: C.textMuted, textTransform: 'uppercase', letterSpacing: '0.05em', marginBottom: '6px' }}>
                Diagnostic Trace ({diagnostics.length})
              </div>
              <div style={{ display: 'flex', flexDirection: 'column', gap: '6px', fontFamily: "'Cascadia Code','Fira Code',monospace", fontSize: '0.66rem' }}>
                {diagnostics.map((d, i) => (
                  <div key={i} style={{ background: C.card, border: `1px solid ${C.border}`, borderRadius: '6px', padding: '8px 10px', color: C.textSec }}>
                    <div>domain: {d.domain} · status: {d.agent_status || '—'} · code: {d.stop_code || '—'} · class: {d.limitation_class || '—'}</div>
                    {d.question && <div style={{ marginTop: '2px', color: C.textMuted }}>question: {d.question}</div>}
                    {d.raw_message && <div style={{ marginTop: '2px', color: C.textMuted }}>{d.raw_message}</div>}
                  </div>
                ))}
              </div>
            </div>
          )}
        </div>
      )}
    </section>
  )
}
