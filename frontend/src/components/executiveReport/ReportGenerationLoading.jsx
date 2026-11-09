import { useEffect, useState } from 'react'
import { FONT } from './shared'

// Milestone 7 Part 11 — the Executive Report backend takes ~174-233s.
// Cycles through named stages of what the pipeline is truthfully doing
// (business-area discovery -> KPI/trend evaluation -> insight synthesis ->
// report assembly, matching core.analysis.data_analyst_agent's own bounded
// plan -> execute -> evaluate -> stop loop) WITHOUT claiming a fabricated
// completion percentage — the frontend has no way to know true progress, so
// it never invents one (Part 11: "Do NOT fake backend progress
// percentages"). Purely a presentation timer; it does not gate or affect
// when the real report actually arrives.
const STAGES = [
  'Preparing business context',
  'Discovering business areas and key entities',
  'Evaluating KPIs and trends',
  'Building management insights',
  'Preparing executive report',
]
const STAGE_INTERVAL_MS = 15_000

export default function ReportGenerationLoading({ C, sourceName }) {
  const [stageIndex, setStageIndex] = useState(0)

  useEffect(() => {
    const timer = setInterval(() => {
      setStageIndex(i => Math.min(i + 1, STAGES.length - 1))
    }, STAGE_INTERVAL_MS)
    return () => clearInterval(timer)
  }, [])

  return (
    <div style={{ background: C.surface, border: `1px solid ${C.border}`, borderRadius: '16px', padding: '40px 36px', textAlign: 'center' }}>
      <div style={{
        display: 'inline-flex', alignItems: 'center', justifyContent: 'center', width: '52px', height: '52px',
        borderRadius: '14px', background: 'rgba(99,102,241,0.1)', marginBottom: '18px',
      }}>
        <div style={{ width: '22px', height: '22px', borderRadius: '50%', border: '2.5px solid rgba(99,102,241,0.25)', borderTopColor: '#6366f1', animation: 'spin 0.9s linear infinite' }} />
      </div>

      <div style={{ fontSize: '1rem', fontWeight: '700', color: C.text, marginBottom: '6px', fontFamily: FONT }}>
        Building Executive Report{sourceName ? ` — ${sourceName}` : ''}
      </div>
      <div style={{ fontSize: '0.78rem', color: C.textMuted, marginBottom: '24px' }}>
        Executive analysis may take a few minutes. Every number is validated against live, governed data as it is computed.
      </div>

      <div style={{ display: 'flex', flexDirection: 'column', gap: '8px', maxWidth: '360px', margin: '0 auto', textAlign: 'left' }}>
        {STAGES.map((stage, i) => {
          const done = i < stageIndex
          const active = i === stageIndex
          return (
            <div key={stage} style={{
              display: 'flex', alignItems: 'center', gap: '10px', padding: '8px 12px', borderRadius: '9px',
              background: active ? 'rgba(99,102,241,0.08)' : 'transparent',
              opacity: i > stageIndex ? 0.45 : 1,
            }}>
              <span style={{
                width: '16px', height: '16px', borderRadius: '50%', flexShrink: 0,
                border: `2px solid ${done ? '#10b981' : active ? '#6366f1' : C.borderAlt}`,
                background: done ? '#10b981' : 'transparent',
                display: 'flex', alignItems: 'center', justifyContent: 'center',
              }}>
                {done && <span style={{ color: '#fff', fontSize: '9px', lineHeight: 1 }}>✓</span>}
              </span>
              <span style={{ fontSize: '0.76rem', color: active ? C.text : C.textSec, fontWeight: active ? '700' : '500' }}>
                {stage}{active ? '…' : ''}
              </span>
            </div>
          )
        })}
      </div>
    </div>
  )
}
