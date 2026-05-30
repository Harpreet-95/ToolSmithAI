import { useState, useEffect, useRef } from 'react'
import { composeIntent, interpretTask, askReport } from '../api/client'
import ProposalPreview from './ProposalPreview'
import ChartSection from './ChartSection'

const FONT = "system-ui, -apple-system, 'Segoe UI', sans-serif"
const MONO = "'Cascadia Code', 'Fira Code', 'JetBrains Mono', monospace"

// Quick start cards — icon left, text right, 2×2 grid (matches screenshot)
const QUICK_STARTS = [
  {
    prompt: 'Analyze my uploaded dataset, identify the top 3 anomalies, generate an executive report with KPIs, and highlight risks',
    text: 'Summarize my dataset and highlight anomalies',
    color: '#f472b6',
    iconType: 'bars',
  },
  {
    prompt: 'Generate an intelligence report with KPIs and executive summary',
    text: 'Generate an intelligence report with KPIs',
    color: '#38bdf8',
    iconType: 'document',
  },
  {
    prompt: 'Find trends in my dataset and predict a readiness score with forward-looking insights',
    text: 'Find trends and predict readiness score',
    color: '#fbbf24',
    iconType: 'trend',
  },
  {
    prompt: 'Detect drift patterns and surface high-severity risks with recommended mitigations',
    text: 'Detect drift patterns and surface risks',
    color: '#60a5fa',
    iconType: 'shield',
  },
]

function QsIcon({ type, color }) {
  const p = { width: 20, height: 20, viewBox: '0 0 24 24', fill: 'none', stroke: color, strokeWidth: 2, strokeLinecap: 'round', strokeLinejoin: 'round' }
  if (type === 'bars')     return <svg {...p}><line x1="18" y1="20" x2="18" y2="10"/><line x1="12" y1="20" x2="12" y2="4"/><line x1="6" y1="20" x2="6" y2="14"/></svg>
  if (type === 'document') return <svg {...p}><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/><line x1="16" y1="13" x2="8" y2="13"/><line x1="16" y1="17" x2="8" y2="17"/></svg>
  if (type === 'trend')    return <svg {...p}><polyline points="23 6 13.5 15.5 8.5 10.5 1 18"/><polyline points="17 6 23 6 23 12"/></svg>
  if (type === 'shield')   return <svg {...p}><path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/></svg>
  return null
}

// Suggested questions shown in empty AI assistant panel (matches screenshot)
const EMPTY_SUGGESTIONS = [
  'What are the top performing products?',
  'Show me revenue trend by region',
  'Identify unusual patterns in sales',
  'Explain this report summary',
]

const COPILOT_SUGGESTIONS = [
  "What's the highest risk in this report?",
  'Summarize key findings in 2 sentences',
  'What action should I prioritize first?',
  'Are there data quality issues to fix?',
]

// ─── Helpers ─────────────────────────────────────────────────────────────────

function getFileType(f) {
  const ext = (f || '').split('.').pop().toLowerCase()
  if (ext === 'csv') return 'CSV'
  if (['xlsx', 'xls'].includes(ext)) return 'Excel'
  if (ext === 'json') return 'JSON'
  if (ext === 'sql') return 'SQL'
  return ext.toUpperCase() || 'File'
}

const DS_COLOR = { CSV: '#10b981', Excel: '#34d399', JSON: '#8b5cf6', SQL: '#f59e0b' }
const DS_BG    = { CSV: '#10b9811a', Excel: '#34d3991a', JSON: '#8b5cf61a', SQL: '#f59e0b1a' }

function extractIntel(result) {
  if (!result) return null
  const report = result.dataset_report
  const aiMeta = result._ai_meta ?? null
  if (!report) {
    return { kind: 'basic', status: result.status, taskType: result.task_type, aiMeta, emailDelivery: result.email_delivery }
  }
  const sections   = report.sections || []
  const execSec    = sections.find(s => s.type === 'executive_summary')
  const kpiSec     = sections.find(s => s.type === 'kpi')
  const recSec     = sections.find(s => s.type === 'recommendation')
  const driftSec   = sections.find(s => s.type === 'drift_detection')
  const chartSecs  = sections.filter(s => s.type === 'chart')
  const kpis       = kpiSec?.kpis || []
  const recs       = recSec?.recommendations || []
  const drifts     = driftSec?.drifts || []
  const takes      = execSec?.key_takeaways || []
  const risks      = execSec?.risks || []
  const opps       = execSec?.opportunities || []
  return {
    kind:            'report',
    title:           report.title || 'Intelligence Report',
    reportId:        result.report_id,
    status:          result.status,
    aiMeta,
    execSummary:     execSec?.summary ?? null,
    keyTakeaways:    takes,
    risks,
    opportunities:   opps,
    kpis,
    recommendations: recs,
    drifts,
    watchlist:       drifts.slice(0, 3),
    chartSecs,
    topInsight:      takes[0] ?? recs[0]?.reason ?? null,
    highestRisk:     risks[0] ?? recs.find(r => r.priority === 'high')?.reason ?? null,
    topAction:       recs.find(r => r.priority === 'high')?.title ?? recs[0]?.title ?? opps[0] ?? null,
    topOpportunity:  opps[1] ?? opps[0] ?? null,
    emailDelivery:   result.email_delivery,
    started_at:      result.started_at,
    finished_at:     result.finished_at,
  }
}

// ─── Global CSS ───────────────────────────────────────────────────────────────

const WS_STYLES = `
@keyframes ws-fadeup {
  from { opacity: 0; transform: translateY(12px); }
  to   { opacity: 1; transform: translateY(0);    }
}
@keyframes ws-fadein {
  from { opacity: 0; }
  to   { opacity: 1; }
}
@keyframes ws-glow-pulse {
  0%,100% { opacity: 0.5; transform: scale(1);    }
  50%     { opacity: 1;   transform: scale(1.06); }
}
@keyframes ws-dot-blink {
  0%,100% { opacity: 1; }
  50%     { opacity: 0.2; }
}
@keyframes ws-spin { to { transform: rotate(360deg); } }
@keyframes ws-orb-float {
  0%,100% { transform: translateY(0px); }
  50%     { transform: translateY(-6px); }
}
@keyframes ws-orb-ring {
  from { transform: rotateX(70deg) rotateZ(0deg); }
  to   { transform: rotateX(70deg) rotateZ(360deg); }
}
@keyframes ws-orb-core {
  0%,100% { opacity: 1; transform: scale(1); }
  50%     { opacity: 0.75; transform: scale(1.35); }
}
@keyframes ws-data-flow {
  from { stroke-dashoffset: 20; }
  to   { stroke-dashoffset: 0; }
}
@keyframes ws-node-glow {
  0%,100% { opacity: 0.55; }
  50%     { opacity: 1; }
}
@keyframes ws-center-glow {
  0%,100% { opacity: 1; transform: scale(1); }
  50%     { opacity: 0.82; transform: scale(1.18); }
}
@keyframes ws-arc-cw  { from { transform: rotate(0deg);    } to { transform: rotate(360deg);  } }
@keyframes ws-arc-ccw { from { transform: rotate(0deg);    } to { transform: rotate(-360deg); } }
@keyframes ws-micro-float {
  0%,100% { transform: translate(0,0);        opacity: 0.45; }
  30%     { transform: translate(3px,-10px);  opacity: 0.90; }
  60%     { transform: translate(-2px,-16px); opacity: 0.60; }
  80%     { transform: translate(4px,-11px);  opacity: 0.78; }
}
@keyframes ws-core-breathe {
  0%,100% { opacity: 0.85; }
  50%     { opacity: 1;    }
}
@keyframes ws-radar-pulse {
  0%   { transform: scale(1);   opacity: 0.65; }
  100% { transform: scale(4.2); opacity: 0;    }
}

.ws-section  { animation: ws-fadeup 0.38s ease both; }
.ws-s1       { animation-delay: 0.05s; }
.ws-s2       { animation-delay: 0.12s; }
.ws-s3       { animation-delay: 0.19s; }
.ws-s4       { animation-delay: 0.26s; }
.ws-s5       { animation-delay: 0.33s; }
.ws-s6       { animation-delay: 0.40s; }

.ws-kpi-card {
  transition: transform 0.18s ease, box-shadow 0.18s ease;
  cursor: default;
}
.ws-kpi-card:hover {
  transform: translateY(-4px);
  box-shadow: 0 12px 32px rgba(124,58,237,0.14), 0 2px 8px rgba(0,0,0,0.10);
}
.ws-insight-card {
  transition: transform 0.18s ease, box-shadow 0.18s ease;
}
.ws-insight-card:hover { transform: translateY(-2px); box-shadow: 0 10px 28px rgba(0,0,0,0.12); }
.ws-chart-panel { transition: transform 0.20s ease, box-shadow 0.20s ease; }
.ws-chart-panel:hover { transform: translateY(-2px); box-shadow: 0 16px 48px rgba(0,0,0,0.14); }

.ws-qs-card { transition: transform 0.15s ease, box-shadow 0.15s ease, border-color 0.15s ease; cursor: pointer; }
.ws-qs-card:hover { transform: translateY(-2px); box-shadow: 0 6px 20px rgba(124,58,237,0.16); border-color: rgba(124,58,237,0.4) !important; }

.ws-upload-btn { transition: border-color 0.14s ease, color 0.14s ease, background 0.14s ease; }
.ws-upload-btn:hover { border-color: #7c3aed !important; color: #7c3aed !important; background: rgba(124,58,237,0.07) !important; }

.ws-suggest-row { transition: background 0.14s ease; cursor: default; }
.ws-suggest-row:hover { background: rgba(124,58,237,0.07) !important; }

.ws-copilot-q { transition: background 0.14s ease, border-color 0.14s ease, color 0.14s ease; }
.ws-copilot-q:hover { border-color: #7c3aed !important; color: #7c3aed !important; background: rgba(124,58,237,0.06) !important; }

.ws-action-btn { transition: opacity 0.14s ease, transform 0.14s ease, box-shadow 0.14s ease; }
.ws-action-btn:hover { opacity: 0.9; transform: translateY(-1px); box-shadow: 0 6px 20px rgba(124,58,237,0.35); }

.ws-ghost-btn { transition: border-color 0.14s ease, color 0.14s ease, background 0.14s ease; }
.ws-ghost-btn:hover { border-color: #7c3aed !important; color: #7c3aed !important; }
`

// ─── Result canvas subcomponents (unchanged) ──────────────────────────────────

function AiLiveBadge({ aiMeta }) {
  const active = aiMeta?.ai_enrichment_used
  const model  = aiMeta?.ai_model_used
    ? aiMeta.ai_model_used.replace('gpt-4o-mini', 'GPT-4o mini').replace('gpt-4o', 'GPT-4o')
    : null
  return (
    <span style={{
      display: 'inline-flex', alignItems: 'center', gap: '6px',
      background: active ? 'rgba(16,185,129,0.12)' : 'rgba(107,114,128,0.1)',
      border: `1px solid ${active ? 'rgba(16,185,129,0.3)' : 'rgba(107,114,128,0.2)'}`,
      borderRadius: '20px', padding: '4px 12px',
      fontSize: '0.7rem', fontWeight: '600', color: active ? '#10b981' : '#9ca3af', fontFamily: FONT,
    }}>
      <span style={{ width: '6px', height: '6px', borderRadius: '50%', flexShrink: 0, background: active ? '#10b981' : '#9ca3af', animation: active ? 'ws-dot-blink 1.8s ease infinite' : 'none' }} />
      {active ? 'AI Intelligence Active' : 'Standard Analysis'}
      {model && <span style={{ opacity: 0.65, fontWeight: '400', marginLeft: '2px' }}>· {model}</span>}
    </span>
  )
}

function ConfidenceRing({ confidence }) {
  if (confidence == null) return null
  const pct   = Math.round(confidence * 100)
  const color = confidence >= 0.85 ? '#10b981' : confidence >= 0.65 ? '#f59e0b' : '#9ca3af'
  const r = 14, circ = 2 * Math.PI * r
  const dash = (pct / 100) * circ
  return (
    <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
      <svg width="36" height="36" viewBox="0 0 36 36" style={{ flexShrink: 0 }}>
        <circle cx="18" cy="18" r={r} fill="none" stroke="rgba(255,255,255,0.08)" strokeWidth="3" />
        <circle cx="18" cy="18" r={r} fill="none" stroke={color} strokeWidth="3"
          strokeDasharray={`${dash} ${circ}`} strokeLinecap="round"
          style={{ transform: 'rotate(-90deg)', transformOrigin: '18px 18px', transition: 'stroke-dasharray 0.6s ease' }}
        />
        <text x="18" y="22" textAnchor="middle" fill={color} style={{ fontSize: '9px', fontWeight: '700', fontFamily: FONT }}>{pct}%</text>
      </svg>
      <div>
        <div style={{ fontSize: '0.6rem', color: 'rgba(255,255,255,0.45)', fontWeight: '600', textTransform: 'uppercase', letterSpacing: '0.08em' }}>Confidence</div>
        <div style={{ fontSize: '0.78rem', fontWeight: '700', color }}>{pct}%</div>
      </div>
    </div>
  )
}

function KpiCard({ kpi, C, delay = 0 }) {
  const statusMap = { good: { color: '#10b981' }, warning: { color: '#f59e0b' }, risk: { color: '#f87171' } }
  const st   = statusMap[kpi.status] || statusMap.good
  const fmtV = (v, fmt) => {
    if (v == null) return '—'
    if (fmt === 'percent')  return `${v}%`
    if (fmt === 'currency') return `$${Number(v).toLocaleString()}`
    if (fmt === 'number')   return Number(v).toLocaleString()
    return String(v)
  }
  const dir        = kpi.delta_direction
  const arrow      = dir === 'up' ? '↑' : dir === 'down' ? '↓' : null
  const deltaColor = dir === 'up' ? '#10b981' : dir === 'down' ? '#f87171' : C.textMuted
  return (
    <div className="ws-kpi-card" style={{
      background: C.surface, border: `1px solid ${C.border}`, borderLeft: `4px solid ${st.color}`,
      borderRadius: '14px', padding: '20px 20px 16px', flex: '1 1 148px', minWidth: '148px',
      boxShadow: '0 2px 8px rgba(0,0,0,0.08)', animation: `ws-fadeup 0.35s ease both`, animationDelay: `${delay}s`,
    }}>
      <div style={{ fontSize: '0.58rem', color: C.textMuted, fontWeight: '700', textTransform: 'uppercase', letterSpacing: '0.09em', marginBottom: '12px' }}>{kpi.label}</div>
      <div style={{ display: 'flex', alignItems: 'flex-end', gap: '8px', marginBottom: '6px', flexWrap: 'wrap' }}>
        <span style={{ fontSize: '2.1rem', fontWeight: '800', color: st.color, letterSpacing: '-2px', lineHeight: 1, fontFamily: MONO }}>{fmtV(kpi.value, kpi.format)}</span>
        {arrow && kpi.delta != null && (
          <span style={{ fontSize: '0.72rem', fontWeight: '700', color: deltaColor, background: `${deltaColor}18`, borderRadius: '6px', padding: '2px 7px', marginBottom: '4px', flexShrink: 0 }}>
            {arrow} {Math.abs(kpi.delta)}
          </span>
        )}
      </div>
      {(kpi.explanation || kpi.description) && (
        <div style={{ fontSize: '0.66rem', color: st.color, lineHeight: 1.45, fontWeight: '500', opacity: 0.85 }}>{kpi.explanation || kpi.description}</div>
      )}
    </div>
  )
}

function InsightCard({ label, text, accentColor, icon, C, delay = 0 }) {
  if (!text) return null
  return (
    <div className="ws-insight-card" style={{
      background: C.surface, border: `1px solid ${accentColor}20`, borderLeft: `4px solid ${accentColor}`,
      borderRadius: '14px', padding: '20px 22px', flex: 1, minWidth: '200px',
      boxShadow: '0 2px 12px rgba(0,0,0,0.06)', animation: `ws-fadeup 0.38s ease both`, animationDelay: `${delay}s`,
    }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: '8px', marginBottom: '14px' }}>
        <div style={{ width: '32px', height: '32px', borderRadius: '10px', background: `${accentColor}18`, border: `1px solid ${accentColor}28`, display: 'flex', alignItems: 'center', justifyContent: 'center', color: accentColor, flexShrink: 0 }}>{icon}</div>
        <span style={{ fontSize: '0.62rem', fontWeight: '800', color: accentColor, textTransform: 'uppercase', letterSpacing: '0.12em' }}>{label}</span>
      </div>
      <p style={{ margin: 0, fontSize: '0.85rem', color: C.text, lineHeight: 1.7 }}>{text}</p>
    </div>
  )
}

function CopilotPanel({ reportId, aiMeta, token, onSessionExpired, C }) {
  const [question, setQuestion] = useState('')
  const [answer,   setAnswer]   = useState(null)
  const [loading,  setLoading]  = useState(false)
  const [error,    setError]    = useState(null)
  const inputRef = useRef(null)

  async function handleAsk(q) {
    const trimmed = (q || question).trim()
    if (!trimmed || !reportId) return
    if (q) setQuestion(q)
    setLoading(true); setError(null); setAnswer(null)
    try {
      const data = await askReport(reportId, trimmed, token)
      setAnswer(data?.data?.answer ?? data?.answer ?? null)
    } catch (err) {
      if (err?.message?.startsWith('401:')) { onSessionExpired(); return }
      setError(err.message?.replace(/^\d+:\s*/, '') || 'Could not get an answer.')
    } finally { setLoading(false) }
  }

  const hasReport = !!reportId
  const conf = aiMeta?.confidence
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: '10px', position: 'sticky', top: '80px' }}>
      {aiMeta?.reasoning_summary && (
        <div style={{ background: C.surface, border: `1px solid ${C.border}`, borderRadius: '14px', padding: '16px 18px', boxShadow: '0 4px 20px rgba(0,0,0,0.08)', animation: 'ws-fadeup 0.35s ease both' }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: '7px', marginBottom: '12px' }}>
            <div style={{ width: '26px', height: '26px', borderRadius: '8px', background: 'rgba(167,139,250,0.15)', display: 'flex', alignItems: 'center', justifyContent: 'center', flexShrink: 0 }}>
              <svg width="13" height="13" viewBox="0 0 24 24" fill="#a78bfa"><path d="M12 2l2.4 7.4H22l-6.2 4.5 2.4 7.4L12 17l-6.2 4.3 2.4-7.4L2 9.4h7.6z"/></svg>
            </div>
            <span style={{ fontSize: '0.64rem', fontWeight: '800', color: '#a78bfa', textTransform: 'uppercase', letterSpacing: '0.12em' }}>AI Reasoning</span>
          </div>
          <p style={{ margin: '0 0 12px', fontSize: '0.78rem', color: C.textSec, lineHeight: 1.7 }}>{aiMeta.reasoning_summary}</p>
          {conf != null && (
            <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
              <div style={{ flex: 1, height: '5px', background: C.border, borderRadius: '3px', overflow: 'hidden' }}>
                <div style={{ height: '100%', width: `${Math.round(conf * 100)}%`, background: conf >= 0.85 ? 'linear-gradient(90deg,#10b981,#34d399)' : conf >= 0.65 ? 'linear-gradient(90deg,#f59e0b,#fbbf24)' : 'linear-gradient(90deg,#9ca3af,#d1d5db)', borderRadius: '3px', transition: 'width 0.7s ease' }} />
              </div>
              <span style={{ fontSize: '0.68rem', fontWeight: '700', color: C.textSec, minWidth: '32px', textAlign: 'right' }}>{Math.round(conf * 100)}%</span>
            </div>
          )}
        </div>
      )}
      {aiMeta && !aiMeta.reasoning_summary && (
        <div style={{ background: C.surface, border: `1px solid ${C.border}`, borderRadius: '14px', padding: '14px 18px', boxShadow: '0 4px 20px rgba(0,0,0,0.08)', animation: 'ws-fadeup 0.35s ease both' }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: '7px', marginBottom: '8px' }}>
            <div style={{ width: '26px', height: '26px', borderRadius: '8px', background: 'rgba(107,114,128,0.1)', display: 'flex', alignItems: 'center', justifyContent: 'center', flexShrink: 0 }}>
              <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="#9ca3af" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><circle cx="12" cy="12" r="10"/><path d="M12 8v4"/><path d="M12 16h.01"/></svg>
            </div>
            <span style={{ fontSize: '0.64rem', fontWeight: '800', color: '#9ca3af', textTransform: 'uppercase', letterSpacing: '0.12em' }}>Analysis Mode</span>
          </div>
          <p style={{ margin: 0, fontSize: '0.76rem', color: C.textMuted, lineHeight: 1.65 }}>
            {aiMeta.ai_enabled
              ? 'Standard analysis completed — AI reasoning was unavailable for this request.'
              : 'Standard analysis completed using deterministic rules.'}
          </p>
        </div>
      )}

      <div style={{ background: C.surface, border: `1px solid ${C.border}`, borderRadius: '14px', padding: '16px 18px', boxShadow: '0 4px 20px rgba(0,0,0,0.08)', animation: 'ws-fadeup 0.35s ease both', animationDelay: '0.07s' }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: '7px', marginBottom: '12px' }}>
          <div style={{ width: '26px', height: '26px', borderRadius: '8px', background: 'rgba(124,58,237,0.15)', display: 'flex', alignItems: 'center', justifyContent: 'center', flexShrink: 0 }}>
            <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="#7c3aed" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round"><circle cx="12" cy="12" r="10"/><path d="M9.09 9a3 3 0 0 1 5.83 1c0 2-3 3-3 3"/><circle cx="12" cy="17" r="0.5" fill="#7c3aed"/></svg>
          </div>
          <span style={{ fontSize: '0.64rem', fontWeight: '800', color: '#7c3aed', textTransform: 'uppercase', letterSpacing: '0.12em' }}>Ask this Report</span>
        </div>
        {!hasReport ? (
          <p style={{ margin: 0, fontSize: '0.74rem', color: C.textMuted, lineHeight: 1.55 }}>Report must be saved to enable AI Q&amp;A.</p>
        ) : (
          <>
            <div style={{ display: 'flex', gap: '6px', marginBottom: '10px' }}>
              <input ref={inputRef} placeholder="Ask a question…" value={question} onChange={e => setQuestion(e.target.value)} onKeyDown={e => { if (e.key === 'Enter' && !e.shiftKey) handleAsk() }}
                style={{ flex: 1, background: C.bg, border: `1px solid ${C.border}`, borderRadius: '9px', padding: '8px 12px', fontSize: '0.77rem', color: C.text, outline: 'none', fontFamily: FONT, transition: 'border-color 0.14s' }}
                onFocus={e => { e.target.style.borderColor = '#7c3aed' }} onBlur={e => { e.target.style.borderColor = C.border }}
              />
              <button onClick={() => handleAsk()} disabled={loading || !question.trim()}
                style={{ background: 'linear-gradient(135deg,#7c3aed,#8b5cf6)', border: 'none', borderRadius: '9px', padding: '8px 14px', color: '#fff', cursor: loading || !question.trim() ? 'not-allowed' : 'pointer', opacity: loading || !question.trim() ? 0.5 : 1, fontSize: '0.76rem', fontFamily: FONT, fontWeight: '700', flexShrink: 0 }}>
                {loading ? <div style={{ width: '10px', height: '10px', borderRadius: '50%', border: '2px solid #ffffff40', borderTopColor: '#fff', animation: 'ws-spin 0.7s linear infinite' }} /> : 'Ask'}
              </button>
            </div>
            <div style={{ display: 'flex', flexWrap: 'wrap', gap: '5px', marginBottom: answer || error ? '10px' : 0 }}>
              {COPILOT_SUGGESTIONS.map(s => (
                <button key={s} className="ws-copilot-q" onClick={() => handleAsk(s)}
                  style={{ background: C.bg, border: `1px solid ${C.borderAlt}`, borderRadius: '20px', padding: '4px 10px', fontSize: '0.67rem', color: C.textMuted, cursor: 'pointer', textAlign: 'left', fontFamily: FONT, fontWeight: '500' }}>
                  {s}
                </button>
              ))}
            </div>
            {error && <div style={{ fontSize: '0.73rem', color: C.danger, padding: '9px 12px', background: C.dangerSoft, borderRadius: '9px' }}>{error}</div>}
            {answer && !loading && (
              <div style={{ background: 'linear-gradient(135deg,rgba(124,58,237,0.06),rgba(139,92,246,0.04))', border: '1px solid rgba(124,58,237,0.18)', borderRadius: '12px', padding: '12px 14px', animation: 'ws-fadein 0.3s ease' }}>
                <div style={{ display: 'flex', alignItems: 'center', gap: '5px', marginBottom: '8px' }}>
                  <div style={{ width: '5px', height: '5px', borderRadius: '50%', background: '#7c3aed', animation: 'ws-dot-blink 1.6s ease infinite' }} />
                  <span style={{ fontSize: '0.6rem', color: '#a78bfa', fontWeight: '700', textTransform: 'uppercase', letterSpacing: '0.1em' }}>AI Answer</span>
                </div>
                <p style={{ margin: 0, fontSize: '0.8rem', color: C.text, lineHeight: 1.7 }}>{answer}</p>
              </div>
            )}
            {loading && (
              <div style={{ display: 'flex', alignItems: 'center', gap: '8px', padding: '8px 0' }}>
                {[0, 0.2, 0.4].map((d, i) => <div key={i} style={{ width: '7px', height: '7px', borderRadius: '50%', background: '#7c3aed', opacity: 0.5, animation: `ws-dot-blink 1.2s ease ${d}s infinite` }} />)}
                <span style={{ fontSize: '0.72rem', color: C.textMuted }}>Thinking…</span>
              </div>
            )}
          </>
        )}
      </div>
    </div>
  )
}

function ExecutiveHero({ intel, C, onOpenReport, setActiveNav }) {
  const aiMeta = intel.aiMeta
  const dur = intel.started_at && intel.finished_at ? `${((new Date(intel.finished_at) - new Date(intel.started_at)) / 1000).toFixed(1)}s` : null
  return (
    <div className="ws-section ws-s1" style={{ borderRadius: '18px', overflow: 'hidden', boxShadow: '0 4px 32px rgba(0,0,0,0.14)', position: 'relative', background: C.surface, border: `1px solid ${C.border}` }}>
      <div style={{ height: '4px', background: 'linear-gradient(90deg, #6d28d9 0%, #7c3aed 45%, #a78bfa 100%)' }} />
      <div style={{ position: 'absolute', top: '4px', right: 0, width: '340px', height: '220px', background: 'radial-gradient(ellipse at 80% 20%, rgba(124,58,237,0.09) 0%, transparent 70%)', pointerEvents: 'none' }} />
      <div style={{ padding: '30px 36px' }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: '10px', marginBottom: '16px', flexWrap: 'wrap' }}>
          <span style={{ fontSize: '0.58rem', fontWeight: '800', color: '#7c3aed', textTransform: 'uppercase', letterSpacing: '0.15em' }}>Executive Briefing</span>
          <span style={{ width: '3px', height: '3px', borderRadius: '50%', background: C.textMuted, display: 'inline-block' }} />
          {aiMeta && <AiLiveBadge aiMeta={aiMeta} />}
        </div>
        <div style={{ display: 'flex', alignItems: 'flex-start', justifyContent: 'space-between', gap: '24px', flexWrap: 'wrap' }}>
          <div style={{ flex: 1, minWidth: '240px' }}>
            <h2 style={{ margin: '0 0 16px', fontSize: 'clamp(1.3rem, 2.2vw, 1.65rem)', fontWeight: '800', color: C.text, letterSpacing: '-0.7px', lineHeight: 1.18 }}>{intel.title}</h2>
            {intel.execSummary && <p style={{ margin: 0, fontSize: '0.92rem', color: C.textSec, lineHeight: 1.8, maxWidth: '640px' }}>{intel.execSummary}</p>}
          </div>
          <div style={{ display: 'flex', flexDirection: 'column', gap: '8px', flexShrink: 0, alignItems: 'flex-end' }}>
            {aiMeta?.confidence != null && <div style={{ marginBottom: '4px' }}><ConfidenceRing confidence={aiMeta.confidence} /></div>}
            {intel.reportId && onOpenReport && (
              <button className="ws-action-btn" onClick={() => onOpenReport(intel.reportId)} style={{ background: 'linear-gradient(135deg, #7c3aed 0%, #8b5cf6 100%)', border: 'none', borderRadius: '11px', padding: '10px 20px', fontSize: '0.82rem', fontWeight: '700', color: '#fff', cursor: 'pointer', fontFamily: FONT, whiteSpace: 'nowrap', boxShadow: '0 4px 16px rgba(124,58,237,0.3)' }}>
                Open Full Workspace
              </button>
            )}
            {intel.reportId && setActiveNav && (
              <button className="ws-ghost-btn" onClick={() => setActiveNav('reports')} style={{ background: 'transparent', border: `1px solid ${C.border}`, borderRadius: '11px', padding: '8px 16px', fontSize: '0.76rem', fontWeight: '500', color: C.textMuted, cursor: 'pointer', fontFamily: FONT, whiteSpace: 'nowrap' }}>
                View in Reports
              </button>
            )}
          </div>
        </div>
      </div>
      {(dur || intel.reportId || intel.emailDelivery?.sent) && (
        <div style={{ display: 'flex', alignItems: 'center', gap: '20px', padding: '12px 36px', borderTop: `1px solid ${C.border}`, background: C.bg, flexWrap: 'wrap' }}>
          {dur && <div style={{ display: 'flex', alignItems: 'center', gap: '6px', fontSize: '0.7rem', color: C.textMuted }}><svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 16 14"/></svg>Generated in {dur}</div>}
          {intel.reportId && <div style={{ display: 'flex', alignItems: 'center', gap: '6px', fontSize: '0.7rem', color: '#10b981' }}><svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><polyline points="20 6 9 17 4 12"/></svg>Saved as report #{intel.reportId}</div>}
          {intel.emailDelivery?.sent && <div style={{ display: 'flex', alignItems: 'center', gap: '6px', fontSize: '0.7rem', color: '#10b981' }}><svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M4 4h16c1.1 0 2 .9 2 2v12c0 1.1-.9 2-2 2H4c-1.1 0-2-.9-2-2V6c0-1.1.9-2 2-2z"/><polyline points="22,6 12,13 2,6"/></svg>Delivered to {intel.emailDelivery.to}</div>}
        </div>
      )}
    </div>
  )
}

function WatchlistPanel({ watchlist, C }) {
  if (!watchlist?.length) return null
  const sevMap = { high: { color: '#f87171', bg: 'rgba(248,113,113,0.1)', label: 'HIGH' }, medium: { color: '#f59e0b', bg: 'rgba(245,158,11,0.1)', label: 'MED' }, low: { color: '#7c3aed', bg: 'rgba(124,58,237,0.1)', label: 'LOW' } }
  return (
    <div className="ws-section ws-s4" style={{ background: C.surface, border: `1px solid ${C.border}`, borderRadius: '16px', overflow: 'hidden', boxShadow: '0 2px 12px rgba(0,0,0,0.06)' }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: '9px', padding: '14px 22px', borderBottom: `1px solid ${C.border}`, background: 'rgba(245,158,11,0.04)' }}>
        <div style={{ width: '24px', height: '24px', borderRadius: '7px', background: 'rgba(245,158,11,0.15)', display: 'flex', alignItems: 'center', justifyContent: 'center', color: '#f59e0b' }}>
          <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round"><path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"/><circle cx="12" cy="12" r="3"/></svg>
        </div>
        <span style={{ fontSize: '0.65rem', fontWeight: '800', color: '#f59e0b', textTransform: 'uppercase', letterSpacing: '0.12em' }}>Watchlist — Drift Signals</span>
        <span style={{ marginLeft: 'auto', fontSize: '0.65rem', color: C.textMuted, background: C.bg, borderRadius: '10px', padding: '1px 8px', border: `1px solid ${C.border}` }}>{watchlist.length} signal{watchlist.length !== 1 ? 's' : ''}</span>
      </div>
      <div style={{ display: 'flex', flexDirection: 'column' }}>
        {watchlist.map((d, i) => {
          const sv  = sevMap[d.severity] || sevMap.low
          const pct = d.drift_percent != null ? `${d.drift_percent > 0 ? '+' : ''}${d.drift_percent}%` : null
          return (
            <div key={i} style={{ display: 'flex', alignItems: 'center', gap: '12px', padding: '14px 22px', borderBottom: i < watchlist.length - 1 ? `1px solid ${C.border}` : 'none', transition: 'background 0.14s' }}
              onMouseEnter={e => e.currentTarget.style.background = C.bg} onMouseLeave={e => e.currentTarget.style.background = 'transparent'}>
              <span style={{ fontSize: '0.59rem', fontWeight: '800', color: sv.color, background: sv.bg, borderRadius: '5px', padding: '2px 7px', textTransform: 'uppercase', letterSpacing: '0.06em', flexShrink: 0 }}>{sv.label}</span>
              {pct && <span style={{ fontSize: '1.05rem', fontWeight: '800', color: sv.color, lineHeight: 1, flexShrink: 0, fontFamily: MONO }}>{pct}</span>}
              <span style={{ fontSize: '0.82rem', color: C.text, fontWeight: '500', flex: 1, minWidth: 0 }}>{d.metric || '—'}</span>
              {d.description && <span style={{ fontSize: '0.71rem', color: C.textMuted, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', maxWidth: '200px', flexShrink: 0 }}>{d.description}</span>}
            </div>
          )
        })}
      </div>
    </div>
  )
}

function PriorityInsights({ recommendations, C }) {
  if (!recommendations?.length) return null
  const pMap = { high: { color: '#f87171', bg: 'rgba(248,113,113,0.1)', label: 'HIGH' }, medium: { color: '#f59e0b', bg: 'rgba(245,158,11,0.1)', label: 'MEDIUM' }, low: { color: '#10b981', bg: 'rgba(16,185,129,0.1)', label: 'LOW' } }
  return (
    <div className="ws-section ws-s5" style={{ background: C.surface, border: `1px solid ${C.border}`, borderRadius: '16px', overflow: 'hidden', boxShadow: '0 2px 12px rgba(0,0,0,0.06)' }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: '9px', padding: '14px 22px', borderBottom: `1px solid ${C.border}`, background: 'rgba(124,58,237,0.04)' }}>
        <div style={{ width: '24px', height: '24px', borderRadius: '7px', background: 'rgba(124,58,237,0.15)', display: 'flex', alignItems: 'center', justifyContent: 'center', color: '#a78bfa' }}>
          <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round"><path d="M9 11l3 3L22 4"/><path d="M21 12v7a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11"/></svg>
        </div>
        <span style={{ fontSize: '0.65rem', fontWeight: '800', color: '#a78bfa', textTransform: 'uppercase', letterSpacing: '0.12em' }}>Priority Insights</span>
        <span style={{ marginLeft: 'auto', fontSize: '0.65rem', color: C.textMuted, background: C.bg, borderRadius: '10px', padding: '1px 8px', border: `1px solid ${C.border}` }}>{Math.min(recommendations.length, 5)} items</span>
      </div>
      <div style={{ display: 'flex', flexDirection: 'column' }}>
        {recommendations.slice(0, 5).map((rec, i) => {
          const p = pMap[rec.priority] || pMap.low
          return (
            <div key={i} style={{ padding: '16px 22px', borderBottom: i < Math.min(recommendations.length, 5) - 1 ? `1px solid ${C.border}` : 'none', borderLeft: `3px solid ${p.color}`, transition: 'background 0.14s' }}
              onMouseEnter={e => e.currentTarget.style.background = C.bg} onMouseLeave={e => e.currentTarget.style.background = 'transparent'}>
              <div style={{ display: 'flex', alignItems: 'center', gap: '8px', marginBottom: rec.reason ? '6px' : 0, flexWrap: 'wrap' }}>
                <span style={{ display: 'inline-flex', alignItems: 'center', background: p.bg, color: p.color, borderRadius: '5px', padding: '1px 7px', fontSize: '0.59rem', fontWeight: '800', letterSpacing: '0.08em', flexShrink: 0, textTransform: 'uppercase' }}>{p.label}</span>
                <span style={{ fontSize: '0.84rem', fontWeight: '600', color: C.text }}>{rec.title || '—'}</span>
              </div>
              {rec.reason && <p style={{ margin: 0, fontSize: '0.77rem', color: C.textSec, lineHeight: 1.6 }}>{rec.reason}</p>}
            </div>
          )
        })}
      </div>
    </div>
  )
}

function ChartPanel({ sec, C, delay = 0 }) {
  return (
    <div className="ws-chart-panel" style={{ background: C.surface, border: `1px solid ${C.border}`, borderRadius: '16px', overflow: 'hidden', boxShadow: '0 2px 16px rgba(0,0,0,0.07)', animation: `ws-fadeup 0.38s ease both`, animationDelay: `${delay}s` }}>
      {sec.heading && (
        <div style={{ padding: '14px 22px', borderBottom: `1px solid ${C.border}`, display: 'flex', alignItems: 'center', gap: '8px' }}>
          <div style={{ width: '4px', height: '16px', background: 'linear-gradient(180deg,#7c3aed,#8b5cf6)', borderRadius: '2px', flexShrink: 0 }} />
          <span style={{ fontSize: '0.7rem', fontWeight: '700', color: C.textSec }}>{sec.heading}</span>
        </div>
      )}
      <div style={{ padding: '20px 22px' }}><ChartSection chart={sec.chart || {}} C={C} /></div>
    </div>
  )
}

function IntelligenceCanvas({ intel, C, onOpenReport, onExportReport, setActiveNav }) {
  if (!intel) return null
  if (intel.kind === 'basic') {
    const ok = intel.status === 'success' || intel.status === 'completed' || intel.status === 'ok'
    return (
      <div className="ws-section ws-s1" style={{ background: C.surface, border: `1px solid ${ok ? '#10b98130' : '#f8717130'}`, borderRadius: '18px', padding: '48px 36px', textAlign: 'center', boxShadow: '0 4px 24px rgba(0,0,0,0.08)' }}>
        <div style={{ width: '60px', height: '60px', borderRadius: '50%', background: ok ? 'rgba(16,185,129,0.12)' : 'rgba(248,113,113,0.12)', display: 'flex', alignItems: 'center', justifyContent: 'center', margin: '0 auto 18px', color: ok ? '#10b981' : '#f87171' }}>
          {ok ? <svg width="26" height="26" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round"><polyline points="20 6 9 17 4 12"/></svg>
               : <svg width="26" height="26" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round"><circle cx="12" cy="12" r="10"/><line x1="15" y1="9" x2="9" y2="15"/><line x1="9" y1="9" x2="15" y2="15"/></svg>}
        </div>
        <div style={{ fontSize: '1.15rem', fontWeight: '700', color: C.text, marginBottom: '8px' }}>{ok ? 'Workflow Completed' : 'Workflow Failed'}</div>
        <div style={{ fontSize: '0.8rem', color: C.textMuted }}>Task type: {intel.taskType || '—'} · Status: {intel.status || '—'}</div>
        {intel.emailDelivery?.sent && <div style={{ marginTop: '12px', fontSize: '0.8rem', color: '#10b981' }}>Delivered to {intel.emailDelivery.to}</div>}
      </div>
    )
  }
  const { kpis, topInsight, highestRisk, topAction, topOpportunity, recommendations, watchlist, chartSecs } = intel
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: '16px' }}>
      <ExecutiveHero intel={intel} C={C} onOpenReport={onOpenReport} setActiveNav={setActiveNav} />
      {kpis.length > 0 && (
        <div className="ws-section ws-s2">
          <div style={{ fontSize: '0.6rem', fontWeight: '800', color: C.textMuted, textTransform: 'uppercase', letterSpacing: '0.12em', marginBottom: '12px' }}>Key Performance Indicators</div>
          <div style={{ display: 'flex', gap: '10px', flexWrap: 'wrap' }}>{kpis.map((kpi, i) => <KpiCard key={i} kpi={kpi} C={C} delay={0.05 + i * 0.04} />)}</div>
        </div>
      )}
      {(topInsight || highestRisk || topAction) && (
        <div className="ws-section ws-s3" style={{ display: 'flex', gap: '12px', flexWrap: 'wrap' }}>
          <InsightCard label="Most Important Insight" text={topInsight} accentColor="#7c3aed" delay={0.10} C={C} icon={<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><circle cx="12" cy="12" r="10"/><line x1="12" y1="8" x2="12" y2="12"/><line x1="12" y1="16" x2="12.01" y2="16"/></svg>} />
          <InsightCard label="Highest Risk" text={highestRisk} accentColor="#f87171" delay={0.15} C={C} icon={<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M10.29 3.86L1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"/><line x1="12" y1="9" x2="12" y2="13"/><line x1="12" y1="17" x2="12.01" y2="17"/></svg>} />
          <InsightCard label="Recommended Action" text={topAction} accentColor="#10b981" delay={0.20} C={C} icon={<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><polygon points="13 2 3 14 12 14 11 22 21 10 12 10 13 2"/></svg>} />
          {topOpportunity && <InsightCard label="Key Opportunity" text={topOpportunity} accentColor="#a78bfa" delay={0.25} C={C} icon={<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><polyline points="23 6 13.5 15.5 8.5 10.5 1 18"/><polyline points="17 6 23 6 23 12"/></svg>} />}
        </div>
      )}
      <WatchlistPanel watchlist={watchlist} C={C} />
      <PriorityInsights recommendations={recommendations} C={C} />
      {chartSecs.length > 0 && (
        <div className="ws-section ws-s6">
          <div style={{ fontSize: '0.6rem', fontWeight: '800', color: C.textMuted, textTransform: 'uppercase', letterSpacing: '0.12em', marginBottom: '12px' }}>Visualizations</div>
          <div style={{ display: 'flex', flexDirection: 'column', gap: '12px' }}>{chartSecs.map((sec, i) => <ChartPanel key={i} sec={sec} C={C} delay={0.08 + i * 0.06} />)}</div>
        </div>
      )}
    </div>
  )
}

function WorkspaceLoading({ C }) {
  return (
    <div style={{ display: 'flex', flexDirection: 'column', alignItems: 'center', justifyContent: 'center', padding: '100px 24px', gap: '24px' }}>
      <div style={{ position: 'relative', width: '64px', height: '64px' }}>
        <div style={{ position: 'absolute', inset: 0, borderRadius: '50%', border: `3px solid ${C.border}`, borderTopColor: '#7c3aed', animation: 'ws-spin 0.85s linear infinite' }} />
        <div style={{ position: 'absolute', inset: '12px', borderRadius: '50%', background: 'radial-gradient(circle,rgba(124,58,237,0.15),transparent)', display: 'flex', alignItems: 'center', justifyContent: 'center', color: '#a78bfa' }}>
          <svg width="18" height="18" viewBox="0 0 24 24" fill="currentColor"><path d="M12 2l2.4 7.4H22l-6.2 4.5 2.4 7.4L12 17l-6.2 4.3 2.4-7.4L2 9.4h7.6z"/></svg>
        </div>
      </div>
      <div style={{ textAlign: 'center' }}>
        <div style={{ fontSize: '1.05rem', fontWeight: '700', color: C.text, marginBottom: '8px', letterSpacing: '-0.2px' }}>AI is analyzing your request</div>
        <div style={{ fontSize: '0.8rem', color: C.textMuted, lineHeight: 1.7 }}>Building intelligence report — this typically takes a few seconds.</div>
      </div>
      <div style={{ display: 'flex', gap: '8px', flexWrap: 'wrap', justifyContent: 'center' }}>
        {['Parsing intent', 'Analyzing data', 'Generating insights', 'Composing report'].map((step, i) => (
          <span key={step} style={{ fontSize: '0.68rem', color: C.textMuted, background: C.surface, border: `1px solid ${C.border}`, borderRadius: '20px', padding: '4px 12px', animation: `ws-dot-blink 2s ease ${i * 0.4}s infinite` }}>{step}</span>
        ))}
      </div>
    </div>
  )
}

// ── Empty AI Assistant panel — matches screenshot exactly ─────────────────────
function EmptyAssistantPanel({ C, proposal }) {
  return (
    <div style={{
      background: C.surface,
      border: `1px solid rgba(30,43,82,0.18)`,
      borderRadius: '18px',
      overflow: 'hidden',
      boxShadow: '0 4px 24px rgba(0,0,0,0.12)',
      display: 'flex',
      flexDirection: 'column',
      position: 'sticky',
      top: '80px',
      animation: 'ws-fadein 0.3s ease',
    }}>

      {/* ── Top bar: AI Assistant | Online ── */}
      <div style={{
        padding: '14px 18px',
        borderBottom: `1px solid rgba(30,43,82,0.18)`,
        display: 'flex', alignItems: 'center', gap: '8px',
      }}>
        <svg width="14" height="14" viewBox="0 0 24 24" fill="#a78bfa">
          <path d="M12 2l2.4 7.4H22l-6.2 4.5 2.4 7.4L12 17l-6.2 4.3 2.4-7.4L2 9.4h7.6z"/>
        </svg>
        <span style={{ fontSize: '0.72rem', fontWeight: '600', color: C.text }}>AI Assistant</span>
        <div style={{ marginLeft: 'auto', display: 'flex', alignItems: 'center', gap: '5px' }}>
          <span style={{ width: '7px', height: '7px', borderRadius: '50%', background: '#10b981', animation: 'ws-dot-blink 2.4s ease infinite', flexShrink: 0 }} />
          <span style={{ fontSize: '0.66rem', fontWeight: '600', color: '#10b981' }}>Online</span>
        </div>
      </div>

      {/* ── Orbital system visual ── */}
      {/* ── Quantum Neural Core ── */}
      <div style={{ padding: '28px 18px 18px', textAlign: 'center' }}>
        <div style={{ width: '126px', height: '126px', margin: '0 auto 14px', position: 'relative', overflow: 'visible' }}>
        <div style={{ position: 'absolute', top: '50%', left: '50%', width: '180px', height: '180px', transform: 'translate(-50%, -50%) scale(0.70)', transformOrigin: 'center center' }}>

          {/* Layered ambient glow */}
          <div style={{ position: 'absolute', inset: '-32px', borderRadius: '50%', background: 'radial-gradient(circle, rgba(88,101,242,0.22) 0%, rgba(139,92,246,0.08) 45%, transparent 70%)', animation: 'ws-glow-pulse 4s ease-in-out infinite', pointerEvents: 'none' }} />
          <div style={{ position: 'absolute', inset: '-8px', borderRadius: '50%', background: 'radial-gradient(circle, rgba(0,212,255,0.07) 0%, transparent 65%)', animation: 'ws-glow-pulse 3s ease-in-out 1.4s infinite', pointerEvents: 'none' }} />

          {/* Energy pulse rings */}
          {['0s','1.2s','2.4s'].map((delay, i) => (
            <div key={i} style={{ position: 'absolute', inset: 0, display: 'flex', alignItems: 'center', justifyContent: 'center', pointerEvents: 'none' }}>
              <div style={{ width: '32px', height: '32px', borderRadius: '50%', border: '1px solid rgba(88,101,242,0.60)', animation: `ws-radar-pulse 3.6s ease-out ${delay} infinite`, transformOrigin: '50% 50%' }} />
            </div>
          ))}

          {/* Ring 1 — electric blue, CW 5s */}
          <div style={{ position: 'absolute', inset: 0, transform: 'perspective(350px) rotateX(68deg) rotateZ(-15deg)' }}>
            <div style={{ position: 'absolute', inset: '6px', borderRadius: '50%', border: '1px solid rgba(0,212,255,0.22)' }} />
            {[
              { delay: '0s',    size: 5,   opacity: 1,    color: '#00d4ff', glow: true  },
              { delay: '-4.6s', size: 3.5, opacity: 0.70, color: '#00d4ff', glow: false },
              { delay: '-4.2s', size: 2.5, opacity: 0.42, color: '#00d4ff', glow: false },
              { delay: '-3.8s', size: 1.8, opacity: 0.18, color: '#00d4ff', glow: false },
            ].map((p, i) => (
              <div key={i} style={{ position: 'absolute', inset: 0, animation: `ws-arc-cw 5s linear ${p.delay} infinite`, transformOrigin: '50% 50%' }}>
                <div style={{ position: 'absolute', top: '6px', left: '50%', transform: 'translate(-50%,-50%)', width: `${p.size}px`, height: `${p.size}px`, borderRadius: '50%', background: p.color, opacity: p.opacity, boxShadow: p.glow ? `0 0 9px ${p.color}, 0 0 22px ${p.color}88` : 'none' }} />
              </div>
            ))}
          </div>

          {/* Ring 2 — neon purple, CCW 6.5s */}
          <div style={{ position: 'absolute', inset: 0, transform: 'perspective(350px) rotateX(72deg) rotateZ(55deg)' }}>
            <div style={{ position: 'absolute', inset: '22px', borderRadius: '50%', border: '1px solid rgba(168,85,247,0.22)' }} />
            {[
              { delay: '0s',    size: 4.5, opacity: 1,    color: '#a855f7', glow: true  },
              { delay: '-6.1s', size: 3,   opacity: 0.65, color: '#a855f7', glow: false },
              { delay: '-5.7s', size: 2.2, opacity: 0.38, color: '#a855f7', glow: false },
              { delay: '-5.3s', size: 1.5, opacity: 0.16, color: '#a855f7', glow: false },
            ].map((p, i) => (
              <div key={i} style={{ position: 'absolute', inset: 0, animation: `ws-arc-ccw 6.5s linear ${p.delay} infinite`, transformOrigin: '50% 50%' }}>
                <div style={{ position: 'absolute', top: '22px', left: '50%', transform: 'translate(-50%,-50%)', width: `${p.size}px`, height: `${p.size}px`, borderRadius: '50%', background: p.color, opacity: p.opacity, boxShadow: p.glow ? `0 0 9px ${p.color}, 0 0 22px ${p.color}88` : 'none' }} />
              </div>
            ))}
          </div>

          {/* Ring 3 — violet, CW 8s */}
          <div style={{ position: 'absolute', inset: 0, transform: 'perspective(350px) rotateX(75deg) rotateZ(115deg)' }}>
            <div style={{ position: 'absolute', inset: '6px', borderRadius: '50%', border: '1px solid rgba(129,140,248,0.20)' }} />
            {[
              { delay: '0s',    size: 4,   opacity: 1,    color: '#818cf8', glow: true  },
              { delay: '-7.5s', size: 2.8, opacity: 0.60, color: '#818cf8', glow: false },
              { delay: '-7.0s', size: 1.8, opacity: 0.28, color: '#818cf8', glow: false },
            ].map((p, i) => (
              <div key={i} style={{ position: 'absolute', inset: 0, animation: `ws-arc-cw 8s linear ${p.delay} infinite`, transformOrigin: '50% 50%' }}>
                <div style={{ position: 'absolute', top: '6px', left: '50%', transform: 'translate(-50%,-50%)', width: `${p.size}px`, height: `${p.size}px`, borderRadius: '50%', background: p.color, opacity: p.opacity, boxShadow: p.glow ? `0 0 8px ${p.color}, 0 0 18px ${p.color}88` : 'none' }} />
              </div>
            ))}
          </div>

          {/* Floating micro-particles */}
          {[
            { x: 22,  y: 42,  c: '#00d4ff', s: 2.5, d: '3.2s', dl: '0s'   },
            { x: 150, y: 28,  c: '#a855f7', s: 2,   d: '4.1s', dl: '0.8s' },
            { x: 14,  y: 120, c: '#818cf8', s: 1.8, d: '3.7s', dl: '1.5s' },
            { x: 158, y: 132, c: '#00d4ff', s: 2,   d: '4.5s', dl: '0.4s' },
            { x: 82,  y: 12,  c: '#c084fc', s: 1.5, d: '3.0s', dl: '1.2s' },
            { x: 86,  y: 162, c: '#38bdf8', s: 1.5, d: '4.8s', dl: '2.0s' },
            { x: 30,  y: 78,  c: '#a855f7', s: 1.5, d: '3.5s', dl: '0.6s' },
            { x: 144, y: 90,  c: '#818cf8', s: 2,   d: '4.2s', dl: '1.8s' },
          ].map((p, i) => (
            <div key={i} style={{ position: 'absolute', left: `${p.x}px`, top: `${p.y}px`, width: `${p.s}px`, height: `${p.s}px`, borderRadius: '50%', background: p.c, boxShadow: `0 0 ${p.s * 2.5}px ${p.c}aa`, animation: `ws-micro-float ${p.d} ease-in-out ${p.dl} infinite`, pointerEvents: 'none' }} />
          ))}

          {/* Glassmorphism core */}
          <div style={{ position: 'absolute', inset: 0, display: 'flex', alignItems: 'center', justifyContent: 'center', zIndex: 10 }}>
            <div style={{ position: 'absolute', width: '56px', height: '56px', borderRadius: '50%', border: '1px solid rgba(88,101,242,0.30)', boxShadow: '0 0 0 5px rgba(88,101,242,0.05), 0 0 24px rgba(88,101,242,0.20)', animation: 'ws-glow-pulse 2.8s ease-in-out infinite' }} />
            <div style={{ position: 'absolute', width: '46px', height: '46px', borderRadius: '50%', background: 'rgba(8,10,36,0.84)', border: '1px solid rgba(139,92,246,0.28)', boxShadow: 'inset 0 1px 0 rgba(255,255,255,0.09), 0 0 20px rgba(139,92,246,0.18)' }} />
            <div style={{ position: 'relative', zIndex: 1, width: '12px', height: '12px', borderRadius: '50%', background: 'radial-gradient(circle, #00d4ff 0%, #a855f7 70%)', boxShadow: '0 0 10px #00d4ff, 0 0 22px #a855f7aa', animation: 'ws-core-breathe 3.2s ease-in-out infinite' }} />
          </div>

        </div>
        </div>

        <p style={{ margin: 0, fontSize: '0.72rem', color: C.textSec, lineHeight: 1.65, padding: '0 8px' }}>
          {proposal
            ? 'Review the plan on the left, then approve and run to see full results.'
            : 'AI reasoning will appear after you compose or run an analysis.'}
        </p>
      </div>

      {/* ── Proposal-phase AI reasoning / fallback ── */}
      {proposal?.reasoning_summary && (
        <div style={{ padding: '0 18px 4px' }}>
          <div style={{ background: 'rgba(167,139,250,0.08)', border: '1px solid rgba(167,139,250,0.18)', borderRadius: '10px', padding: '12px 14px' }}>
            <div style={{ display: 'flex', alignItems: 'center', gap: '6px', marginBottom: '8px' }}>
              <svg width="11" height="11" viewBox="0 0 24 24" fill="#a78bfa"><path d="M12 2l2.4 7.4H22l-6.2 4.5 2.4 7.4L12 17l-6.2 4.3 2.4-7.4L2 9.4h7.6z"/></svg>
              <span style={{ fontSize: '0.6rem', fontWeight: '800', color: '#a78bfa', textTransform: 'uppercase', letterSpacing: '0.1em' }}>AI Reasoning</span>
            </div>
            <p style={{ margin: 0, fontSize: '0.75rem', color: C.textSec, lineHeight: 1.65 }}>{proposal.reasoning_summary}</p>
            {proposal.confidence != null && (
              <div style={{ display: 'flex', alignItems: 'center', gap: '8px', marginTop: '8px' }}>
                <div style={{ flex: 1, height: '4px', background: 'rgba(255,255,255,0.08)', borderRadius: '3px', overflow: 'hidden' }}>
                  <div style={{ height: '100%', width: `${Math.round(proposal.confidence * 100)}%`, background: proposal.confidence >= 0.85 ? 'linear-gradient(90deg,#10b981,#34d399)' : proposal.confidence >= 0.65 ? 'linear-gradient(90deg,#f59e0b,#fbbf24)' : 'linear-gradient(90deg,#9ca3af,#d1d5db)', borderRadius: '3px' }} />
                </div>
                <span style={{ fontSize: '0.66rem', fontWeight: '700', color: C.textSec, minWidth: '32px', textAlign: 'right' }}>{Math.round(proposal.confidence * 100)}%</span>
              </div>
            )}
          </div>
        </div>
      )}
      {proposal && !proposal.reasoning_summary && (
        <div style={{ padding: '0 18px 4px' }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: '7px', padding: '9px 12px', background: 'rgba(107,114,128,0.06)', border: '1px solid rgba(107,114,128,0.14)', borderRadius: '9px' }}>
            <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="#9ca3af" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" style={{ flexShrink: 0 }}><circle cx="12" cy="12" r="10"/><path d="M12 8v4"/><path d="M12 16h.01"/></svg>
            <span style={{ fontSize: '0.69rem', color: '#9ca3af', lineHeight: 1.4 }}>
              {proposal.ai_enabled
                ? 'Smart Plan generated — AI reasoning unavailable for this request.'
                : 'Smart Plan generated using deterministic rules.'}
            </span>
          </div>
        </div>
      )}

      {/* ── Suggested questions ── */}
      <div style={{ padding: '0 0 4px' }}>
        <div style={{
          padding: '0 18px 8px',
          fontSize: '0.58rem', fontWeight: '600', color: C.textMuted,
          textTransform: 'uppercase', letterSpacing: '0.14em',
        }}>
          Suggested Questions
        </div>
        <div style={{ display: 'flex', flexDirection: 'column' }}>
          {EMPTY_SUGGESTIONS.map((q, i) => (
            <div
              key={q}
              className="ws-suggest-row"
              style={{
                display: 'flex', alignItems: 'center', gap: '10px',
                padding: '11px 18px',
                borderTop: `1px solid rgba(30,43,82,0.18)`,
              }}
            >
              <span style={{ flex: 1, fontSize: '0.7rem', color: C.textSec, lineHeight: 1.4 }}>{q}</span>
              <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke={C.textMuted} strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round" style={{ flexShrink: 0 }}>
                <path d="m9 18 6-6-6-6"/>
              </svg>
            </div>
          ))}
        </div>
      </div>

      {/* ── Ask input ── */}
      <div style={{ padding: '12px 18px 8px', borderTop: `1px solid rgba(30,43,82,0.18)` }}>
        <div style={{ display: 'flex', gap: '8px', alignItems: 'center', opacity: 0.45 }}>
          <input
            disabled
            placeholder="Ask anything about your data…"
            style={{
              flex: 1, background: C.bg, border: `1px solid rgba(30,43,82,0.18)`,
              borderRadius: '24px', padding: '9px 16px', fontSize: '0.71rem',
              color: C.textMuted, outline: 'none', fontFamily: FONT, cursor: 'not-allowed',
            }}
          />
          <button disabled style={{
            width: '34px', height: '34px', borderRadius: '50%', flexShrink: 0,
            background: 'linear-gradient(135deg,#7c3aed,#6d28d9)',
            border: 'none', cursor: 'not-allowed',
            display: 'flex', alignItems: 'center', justifyContent: 'center',
          }}>
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="#fff" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
              <line x1="22" y1="2" x2="11" y2="13"/><polygon points="22 2 15 22 11 13 2 9 22 2"/>
            </svg>
          </button>
        </div>
      </div>

      {/* Disclaimer */}
      <div style={{ padding: '6px 18px 14px', textAlign: 'center' }}>
        <span style={{ fontSize: '0.57rem', color: C.textMuted, opacity: 0.7 }}>
          AI responses may vary. Verify important insights.
        </span>
      </div>
    </div>
  )
}

// ─── AI Workspace — main export ───────────────────────────────────────────────

export default function AIWorkspace({
  C, S,
  token, onSessionExpired,
  user,
  datasetList,
  selectedDatasetId, setSelectedDatasetId,
  externalResult, externalLoading, externalError,
  setActiveNav, onOpenReport, onExportReport,
  onUploadDataset,
}) {
  const fileInputRef = useRef(null)

  const [wsInput,           setWsInput]           = useState('')
  const [wsLoading,         setWsLoading]         = useState(false)
  const [wsResult,          setWsResult]          = useState(null)
  const [wsError,           setWsError]           = useState(null)
  const [wsProposal,        setWsProposal]        = useState(null)
  const [wsProposalLoading, setWsProposalLoading] = useState(false)
  const [wsProposalError,   setWsProposalError]   = useState(null)
  const [wsEmail,           setWsEmail]           = useState('')
  const [dsPicker,          setDsPicker]          = useState(false)

  useEffect(() => { if (externalLoading) { setWsResult(null); setWsError(null) } }, [externalLoading])
  useEffect(() => { if (externalResult) setWsResult(externalResult) }, [externalResult])
  useEffect(() => { if (externalError) setWsError(externalError) }, [externalError])

  const activeResult  = wsResult
  const activeLoading = wsLoading || externalLoading
  const activeError   = wsError
  const intel         = extractIntel(activeResult)
  const hasResult     = !!activeResult && !activeLoading
  const showComposer  = !hasResult && !activeLoading && !wsProposal
  const activeDs      = datasetList?.find(d => d.id === selectedDatasetId) || null
  const isEmailIntent = ['email', 'send', 'mail'].some(kw => wsInput.toLowerCase().includes(kw))
  const disabled      = wsLoading || wsProposalLoading || !wsInput.trim()

  async function handleCompose() {
    setWsProposalError(null); setWsProposal(null)
    const trimmed = wsInput.trim()
    if (!trimmed || trimmed.length < 5) { setWsProposalError('Please enter a task description (at least 5 characters).'); return }
    setWsProposalLoading(true)
    try {
      const data = await composeIntent(trimmed, selectedDatasetId || null, token, true)
      setWsProposal(data?.data ?? null)
    } catch (err) {
      if (err?.message?.startsWith('401:')) { onSessionExpired(); return }
      setWsProposalError(err.message?.replace(/^\d+:\s*/, '') || 'Failed to compose plan.')
    } finally { setWsProposalLoading(false) }
  }

  async function handleRun(sections = null, proposal = null) {
    const trimmed = wsInput.trim()
    if (!trimmed) return
    setWsError(null); setWsLoading(true); setWsResult(null)
    try {
      const data = await interpretTask(trimmed, token, selectedDatasetId || null, wsEmail.trim() || null, sections)
      const execResult = data?.data ?? null
      const aiMeta = proposal ? {
        reasoning_summary:  proposal.reasoning_summary  ?? null,
        confidence:         proposal.confidence         ?? null,
        ai_enrichment_used: proposal.ai_enrichment_used ?? false,
        ai_enabled:         proposal.ai_enabled         ?? false,
        ai_model_used:      proposal.ai_model_used      ?? null,
      } : null
      setWsResult(execResult ? { ...execResult, _ai_meta: aiMeta } : null)
    } catch (err) {
      if (err?.message?.startsWith('401:')) { onSessionExpired(); return }
      setWsError(err.message?.replace(/^\d+:\s*/, '') || 'Execution failed.')
    } finally { setWsLoading(false) }
  }

  function handleReset() {
    setWsResult(null); setWsError(null); setWsProposal(null); setWsProposalError(null); setWsInput('')
  }

  return (
    <div style={{ fontFamily: FONT }}>
      <style>{WS_STYLES}</style>

      {/* Hidden file input */}
      <input ref={fileInputRef} type="file" accept=".csv,.xlsx,.xls,.json,.sql" style={{ display: 'none' }}
        onChange={e => { const f = e.target.files?.[0]; if (f && onUploadDataset) onUploadDataset(f); e.target.value = '' }}
      />

      {/* ── Page header ── */}
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: '14px', flexWrap: 'wrap', gap: '12px' }}>
        <div>
          <div style={{ display: 'flex', alignItems: 'center', gap: '10px', marginBottom: '5px' }}>
            <h1 style={{ margin: 0, fontSize: '1.65rem', fontWeight: '600', letterSpacing: '-0.5px', lineHeight: 1 }}>
              <span style={{ color: '#8b5cf6' }}>AI</span>
              <span style={{ color: C.text }}> Intelligence Workspace</span>
            </h1>
            <svg width="36" height="36" viewBox="0 0 24 24" fill="#7c3aed" style={{ flexShrink: 0 }}>
              <path d="M12 2 L14 10 L22 12 L14 14 L12 22 L10 14 L2 12 L10 10 Z"/>
            </svg>
          </div>
          <p style={{ margin: 0, color: C.textMuted, fontSize: '0.7rem' }}>
            {hasResult ? `Analysis complete · ${intel?.title || 'Report ready'}` : 'Compose, analyze, and automate with the power of AI.'}
          </p>
        </div>
        {hasResult && (
          <button className="ws-ghost-btn" onClick={handleReset} style={{ display: 'flex', alignItems: 'center', gap: '7px', background: C.surface, border: `1px solid ${C.border}`, borderRadius: '11px', padding: '9px 18px', fontSize: '0.82rem', color: C.textSec, cursor: 'pointer', fontFamily: FONT, fontWeight: '600', boxShadow: '0 1px 4px rgba(0,0,0,0.08)' }}>
            <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round"><line x1="12" y1="5" x2="12" y2="19"/><line x1="5" y1="12" x2="19" y2="12"/></svg>
            New Analysis
          </button>
        )}
      </div>

      {/* ── Two-column layout ── */}
      <div style={{ display: 'flex', gap: '18px', alignItems: 'flex-start' }}>

        {/* ── Left: main canvas ── */}
        <div style={{ flex: 1, minWidth: 0 }}>

          {/* ── Intent Composer ── */}
          {showComposer && (
            <div style={{
              padding: '1px',
              borderRadius: '18px',
              background: `conic-gradient(from 270deg at 18px 18px, #a855f7 0deg, #a855f7 90deg, transparent 90deg) top left / 18px 18px no-repeat, conic-gradient(from 0deg at 0px 18px, #60a5fa 0deg, #60a5fa 90deg, transparent 90deg) top right / 18px 18px no-repeat, linear-gradient(to right, #a855f7 0%, #60a5fa 100%) top / 100% 1px no-repeat, linear-gradient(to bottom, #a855f7 0%, transparent 100%) left / 1px 100% no-repeat, linear-gradient(to bottom, #60a5fa 0%, transparent 100%) right / 1px 100% no-repeat, ${C.surface}`,
              boxShadow: '0 4px 28px rgba(0,0,0,0.12)',
              animation: 'ws-fadein 0.3s ease',
            }}>
            <div style={{
              background: C.surface,
              borderRadius: '17px',
              position: 'relative',
              overflow: 'visible',
            }}>
              {/* Inner top-left corner glow */}
              <div style={{ position: 'absolute', top: 0, left: 0, width: '200px', height: '140px', background: 'radial-gradient(ellipse at top left, rgba(168,85,247,0.22) 0%, rgba(96,165,250,0.08) 40%, transparent 65%)', pointerEvents: 'none', zIndex: 0 }} />


              <div style={{ padding: '22px 26px 26px', position: 'relative', zIndex: 1, background: 'transparent' }}>

                {/* ── Header: badge + buttons ── */}
                <div style={{ display: 'flex', alignItems: 'center', gap: '10px', marginBottom: '18px', flexWrap: 'wrap' }}>
                  <div style={{ display: 'flex', alignItems: 'center', gap: '7px', background: 'rgba(124,58,237,0.12)', border: '1px solid rgba(124,58,237,0.22)', borderRadius: '20px', padding: '5px 12px 5px 9px' }}>
                    <svg width="13" height="13" viewBox="0 0 24 24" fill="url(#badge-grad)">
                      <defs>
                        <linearGradient id="badge-grad" x1="0%" y1="0%" x2="100%" y2="0%">
                          <stop offset="0%" stopColor="#60a5fa"/>
                          <stop offset="100%" stopColor="#a855f7"/>
                        </linearGradient>
                      </defs>
                      <path d="M12 1L14 10L23 12L14 14L12 23L10 14L1 12L10 10Z"/>
                    </svg>
                    <span style={{ fontSize: '0.57rem', fontWeight: '600', textTransform: 'uppercase', letterSpacing: '0.14em', background: 'linear-gradient(to right, #60a5fa, #a855f7)', WebkitBackgroundClip: 'text', WebkitTextFillColor: 'transparent', backgroundClip: 'text' }}>AI Intelligence Composer</span>
                  </div>
                  <div style={{ marginLeft: 'auto', display: 'flex', gap: '8px' }}>
                    <button className="ws-ghost-btn" onClick={() => handleRun()} disabled={disabled}
                      style={{ display: 'flex', alignItems: 'center', gap: '6px', background: C.surface, border: `1px solid ${C.borderAlt}`, borderRadius: '10px', padding: '7px 16px', fontSize: '0.74rem', color: C.text, cursor: disabled ? 'not-allowed' : 'pointer', fontFamily: FONT, fontWeight: '500', opacity: disabled ? 0.45 : 1 }}>
                      <svg width="11" height="11" viewBox="0 0 24 24" fill="#3b82f6"><polygon points="5 3 19 12 5 21 5 3"/></svg>
                      {wsLoading ? 'Running…' : 'Run Directly'}
                    </button>
                    <button className="ws-action-btn" onClick={handleCompose} disabled={disabled}
                      style={{ display: 'flex', alignItems: 'center', gap: '7px', background: 'linear-gradient(to right, #1e40af, #5b21b6)', border: 'none', borderRadius: '10px', padding: '7px 18px', fontSize: '0.74rem', fontWeight: '700', color: '#fff', cursor: disabled ? 'not-allowed' : 'pointer', fontFamily: FONT, boxShadow: '0 4px 16px rgba(91,33,182,0.4), 0 2px 6px rgba(30,64,175,0.3)', opacity: disabled ? 0.55 : 1 }}>
                      {wsProposalLoading
                        ? <div style={{ width: '11px', height: '11px', borderRadius: '50%', border: '2px solid rgba(255,255,255,0.35)', borderTopColor: '#fff', animation: 'ws-spin 0.75s linear infinite' }} />
                        : <svg width="12" height="12" viewBox="0 0 24 24" fill="currentColor"><path d="M12 1L14 10L23 12L14 14L12 23L10 14L1 12L10 10Z"/></svg>}
                      {wsProposalLoading ? 'Composing…' : 'Compose Plan'}
                    </button>
                  </div>
                </div>

                {/* ── Colored headline — 2-line layout matching screenshot ── */}
                <h2 style={{ margin: '0 0 16px', fontSize: '1.38rem', fontWeight: '500', letterSpacing: '-0.4px', lineHeight: 1.45 }}>
                  <span style={{ color: C.text }}>What would you like ToolSmithAI to</span>
                  <br />
                  <span style={{ color: '#f472b6' }}>analyze</span>
                  <span style={{ color: C.text }}>, </span>
                  <span style={{ color: '#38bdf8' }}>automate</span>
                  <span style={{ color: C.text }}>, or </span>
                  <span style={{ color: '#c084fc' }}>monitor</span>
                  <span style={{ color: C.text }}>?</span>
                </h2>

                {/* ── Textarea with bottom-right icons ── */}
                <div style={{ position: 'relative', marginBottom: '16px' }}>
                  <textarea
                    placeholder="Example: Analyze my uploaded sales dataset, identify the top 3 anomalies, generate an executive report with KPIs, and highlight risks…"
                    rows={5}
                    value={wsInput}
                    onChange={e => { setWsInput(e.target.value); setWsProposalError(null) }}
                    onKeyDown={e => { if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) { e.preventDefault(); handleCompose() } }}
                    style={{
                      width: '100%', boxSizing: 'border-box',
                      background: C.bg, border: `1px solid ${C.border}`,
                      borderRadius: '12px', color: C.text,
                      fontSize: '0.82rem', padding: '14px 16px 40px',
                      outline: 'none', resize: 'none', lineHeight: 1.7,
                      fontFamily: FONT, transition: 'border-color 0.14s',
                    }}
                    onFocus={e => { e.target.style.borderColor = '#7c3aed' }}
                    onBlur={e => { e.target.style.borderColor = C.border }}
                  />
                  {/* Bottom-right icons inside textarea */}
                  <div style={{ position: 'absolute', bottom: '12px', right: '14px', display: 'flex', alignItems: 'center', gap: '8px', pointerEvents: 'none' }}>
                    <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke={C.textMuted} strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" style={{ opacity: 0.5 }}>
                      <path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4"/><polyline points="16 17 21 12 16 7"/><line x1="21" y1="12" x2="9" y2="12"/>
                    </svg>
                    <svg width="15" height="15" viewBox="0 0 24 24" fill={C.textMuted} style={{ opacity: 0.45 }}>
                      <path d="M12 2l2.4 7.4H22l-6.2 4.5 2.4 7.4L12 17l-6.2 4.3 2.4-7.4L2 9.4h7.6z"/>
                    </svg>
                  </div>
                </div>

                {/* Email recipient */}
                {isEmailIntent && (
                  <div style={{ display: 'flex', alignItems: 'center', gap: '9px', marginBottom: '14px' }}>
                    <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke={C.textMuted} strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M4 4h16c1.1 0 2 .9 2 2v12c0 1.1-.9 2-2 2H4c-1.1 0-2-.9-2-2V6c0-1.1.9-2 2-2z"/><polyline points="22,6 12,13 2,6"/></svg>
                    <input type="email" placeholder="Recipient email address" value={wsEmail} onChange={e => setWsEmail(e.target.value)}
                      style={{ flex: 1, background: C.bg, border: `1px solid ${C.border}`, borderRadius: '9px', padding: '8px 13px', fontSize: '0.82rem', color: C.text, fontFamily: FONT, outline: 'none', transition: 'border-color 0.14s' }}
                      onFocus={e => { e.target.style.borderColor = '#7c3aed' }} onBlur={e => { e.target.style.borderColor = C.border }}
                    />
                  </div>
                )}

                {/* ── SELECT DATASET section ── */}
                <div style={{ marginBottom: '20px' }}>
                  <div style={{ display: 'flex', alignItems: 'center', gap: '6px', marginBottom: '8px' }}>
                    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="#7c3aed" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round"><ellipse cx="12" cy="5" rx="9" ry="3"/><path d="M21 12c0 1.66-4 3-9 3s-9-1.34-9-3"/><path d="M3 5v14c0 1.66 4 3 9 3s9-1.34 9-3V5"/></svg>
                    <span style={{ fontSize: '0.63rem', fontWeight: '600', color: C.text, textTransform: 'uppercase', letterSpacing: '0.14em' }}>Select Dataset</span>
                  </div>

                  <div style={{ display: 'flex', alignItems: 'center', gap: '8px', flexWrap: 'wrap' }}>
                    {/* Dataset dropdown */}
                    <div style={{ position: 'relative', flex: 1, minWidth: '180px' }}>
                      {dsPicker && <div style={{ position: 'fixed', inset: 0, zIndex: 998 }} onClick={() => setDsPicker(false)} />}
                      <button className="ws-ghost-btn" onClick={() => setDsPicker(o => !o)}
                        style={{ display: 'flex', alignItems: 'center', gap: '8px', width: '100%', background: activeDs ? 'rgba(124,58,237,0.07)' : C.bg, border: `1px solid ${activeDs ? 'rgba(124,58,237,0.35)' : C.border}`, borderRadius: '10px', padding: '9px 14px', fontSize: '0.76rem', color: activeDs ? '#7c3aed' : C.textSec, cursor: 'pointer', fontFamily: FONT, fontWeight: '400', textAlign: 'left', position: 'relative', zIndex: 999 }}>
                        <span style={{ flex: 1 }}>{activeDs ? activeDs.filename : 'Choose an existing dataset'}</span>
                        <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round" style={{ opacity: 0.5, flexShrink: 0 }}><path d="m6 9 6 6 6-6"/></svg>
                      </button>

                      {dsPicker && (
                        <div style={{ position: 'absolute', top: 'calc(100% + 8px)', left: 0, zIndex: 999, width: '100%', minWidth: '280px', background: C.surface, border: `1px solid ${C.borderAlt}`, borderRadius: '14px', boxShadow: '0 16px 48px rgba(0,0,0,0.26)', overflow: 'hidden', animation: 'ws-fadeup 0.18s ease' }}>
                          <div style={{ padding: '10px 16px', borderBottom: `1px solid ${C.border}`, fontSize: '0.67rem', fontWeight: '700', color: C.textSec, textTransform: 'uppercase', letterSpacing: '0.08em' }}>
                            {datasetList?.length || 0} datasets available
                          </div>
                          <div style={{ maxHeight: '230px', overflowY: 'auto' }}>
                            {!datasetList?.length ? (
                              <div style={{ padding: '24px', textAlign: 'center', color: C.textMuted, fontSize: '0.82rem' }}>No datasets uploaded.</div>
                            ) : datasetList.map(ds => {
                              const isSel = ds.id === selectedDatasetId
                              const type  = getFileType(ds.filename)
                              const tc    = DS_COLOR[type] || C.textSec
                              const tbg   = DS_BG[type] || C.borderAlt
                              return (
                                <div key={ds.id} onClick={() => { setSelectedDatasetId(ds.id); setDsPicker(false) }}
                                  style={{ display: 'flex', alignItems: 'center', gap: '10px', padding: '11px 16px', cursor: 'pointer', background: isSel ? 'rgba(124,58,237,0.08)' : 'transparent', borderBottom: `1px solid ${C.border}`, transition: 'background 0.1s' }}
                                  onMouseEnter={e => { if (!isSel) e.currentTarget.style.background = C.borderAlt }}
                                  onMouseLeave={e => { if (!isSel) e.currentTarget.style.background = 'transparent' }}>
                                  <div style={{ width: '30px', height: '24px', borderRadius: '6px', background: tbg, border: `1px solid ${tc}40`, display: 'flex', alignItems: 'center', justifyContent: 'center', fontSize: '0.55rem', fontWeight: '700', color: tc, flexShrink: 0 }}>{type.slice(0, 3)}</div>
                                  <div style={{ flex: 1, minWidth: 0 }}>
                                    <div style={{ fontSize: '0.82rem', fontWeight: '500', color: isSel ? '#7c3aed' : C.text, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{ds.filename}</div>
                                    <div style={{ fontSize: '0.66rem', color: C.textMuted }}>{(ds.row_count || 0).toLocaleString()} rows · {ds.column_count} cols</div>
                                  </div>
                                  {isSel && <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="#7c3aed" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round"><polyline points="20 6 9 17 4 12"/></svg>}
                                </div>
                              )
                            })}
                          </div>
                          <div onClick={() => { setDsPicker(false); setActiveNav('datasets') }}
                            style={{ padding: '10px 16px', display: 'flex', alignItems: 'center', gap: '6px', cursor: 'pointer', color: '#7c3aed', fontSize: '0.78rem', fontWeight: '500', borderTop: `1px solid ${C.border}`, transition: 'background 0.12s' }}
                            onMouseEnter={e => e.currentTarget.style.background = 'rgba(124,58,237,0.07)'}
                            onMouseLeave={e => e.currentTarget.style.background = 'transparent'}>
                            <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><line x1="12" y1="5" x2="12" y2="19"/><line x1="5" y1="12" x2="19" y2="12"/></svg>
                            Manage datasets
                          </div>
                        </div>
                      )}
                    </div>

                    {/* Upload New Dataset button */}
                    <button className="ws-upload-btn" onClick={() => fileInputRef.current?.click()}
                      style={{ display: 'flex', alignItems: 'center', gap: '6px', background: C.bg, border: `1px solid ${C.border}`, borderRadius: '10px', padding: '9px 16px', fontSize: '0.76rem', color: C.textSec, cursor: 'pointer', fontFamily: FONT, fontWeight: '400', whiteSpace: 'nowrap' }}>
                      <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="17 8 12 3 7 8"/><line x1="12" y1="3" x2="12" y2="15"/></svg>
                      Upload New Dataset
                    </button>
                  </div>
                </div>

                {/* ── QUICK START EXAMPLES section ── */}
                <div style={{ marginBottom: '4px' }}>
                  <div style={{ display: 'flex', alignItems: 'center', gap: '6px', marginBottom: '10px' }}>
                    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="#7c3aed" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round"><polygon points="13 2 3 14 12 14 11 22 21 10 12 10 13 2"/></svg>
                    <span style={{ fontSize: '0.63rem', fontWeight: '600', color: C.text, textTransform: 'uppercase', letterSpacing: '0.14em' }}>Quick Start Examples</span>
                  </div>
                  <div style={{ display: 'grid', gridTemplateColumns: 'repeat(4, 1fr)', gap: '8px' }}>
                    {QUICK_STARTS.map(qs => (
                      <div key={qs.text} className="ws-qs-card" onClick={() => setWsInput(qs.prompt)}
                        style={{ background: C.bg, border: `1px solid ${C.border}`, borderRadius: '12px', padding: '13px 14px', display: 'flex', alignItems: 'flex-start', gap: '10px', userSelect: 'none' }}>
                        <div style={{ flexShrink: 0, width: '32px', height: '32px', borderRadius: '8px', background: `${qs.color}20`, display: 'flex', alignItems: 'center', justifyContent: 'center' }}>
                          <QsIcon type={qs.iconType} color={qs.color} />
                        </div>
                        <div style={{ fontSize: '0.7rem', color: C.text, lineHeight: 1.45, fontWeight: '400' }}>{qs.text}</div>
                      </div>
                    ))}
                  </div>
                </div>

                {/* Error */}
                {wsProposalError && (
                  <div style={{ marginTop: '14px', display: 'flex', alignItems: 'flex-start', gap: '8px', padding: '11px 16px', background: C.dangerSoft, border: `1px solid ${C.danger}40`, borderRadius: '11px', fontSize: '0.8rem', color: C.danger }}>
                    <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" style={{ flexShrink: 0, marginTop: '1px' }}><circle cx="12" cy="12" r="10"/><line x1="12" y1="8" x2="12" y2="12"/><line x1="12" y1="16" x2="12.01" y2="16"/></svg>
                    <span style={{ flex: 1 }}>{wsProposalError}</span>
                    <button onClick={() => setWsProposalError(null)} style={{ background: 'none', border: 'none', color: C.danger, cursor: 'pointer', padding: 0 }}>
                      <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>
                    </button>
                  </div>
                )}
              </div>
            </div>
            </div>
          )}

          {/* ── Proposal preview ── */}
          {wsProposal && !activeLoading && (
            <ProposalPreview proposal={wsProposal} C={C}
              onApprove={() => { const sections = wsProposal?.selected_sections ?? null; const proposal = wsProposal; setWsProposal(null); handleRun(sections, proposal) }}
              onEdit={() => setWsProposal(null)}
              onClear={() => { setWsProposal(null); setWsInput(''); setWsProposalError(null) }}
              onGoToDatasets={() => setActiveNav('datasets')}
            />
          )}

          {/* ── Loading ── */}
          {activeLoading && <WorkspaceLoading C={C} />}

          {/* ── Execution error ── */}
          {activeError && !activeLoading && !hasResult && (
            <div style={{ background: C.dangerSoft, border: `1px solid ${C.danger}40`, borderRadius: '18px', padding: '40px 36px', textAlign: 'center', animation: 'ws-fadein 0.25s ease' }}>
              <div style={{ fontSize: '0.85rem', color: C.danger, marginBottom: '10px', fontWeight: '700' }}>Execution Error</div>
              <p style={{ margin: '0 0 20px', fontSize: '0.84rem', color: C.danger, lineHeight: 1.7 }}>{activeError}</p>
              <button onClick={handleReset} style={{ background: C.danger, border: 'none', borderRadius: '10px', padding: '9px 22px', fontSize: '0.82rem', fontWeight: '700', color: '#fff', cursor: 'pointer', fontFamily: FONT }}>Try Again</button>
            </div>
          )}

          {/* ── Intelligence Canvas ── */}
          {hasResult && (
            <IntelligenceCanvas intel={intel} C={C} onOpenReport={onOpenReport} onExportReport={onExportReport} setActiveNav={setActiveNav} />
          )}
        </div>

        {/* ── Right column — always visible ── */}
        <div style={{ width: '340px', flexShrink: 0 }}>
          {hasResult && intel ? (
            <CopilotPanel reportId={intel.kind === 'report' ? intel.reportId : null} aiMeta={intel.aiMeta} token={token} onSessionExpired={onSessionExpired} C={C} />
          ) : (
            <EmptyAssistantPanel proposal={wsProposal} C={C} />
          )}
        </div>
      </div>
    </div>
  )
}
