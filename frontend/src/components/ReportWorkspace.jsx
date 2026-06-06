import { useState, useRef } from 'react'
import { askReport } from '../api/client'

const FONT = "system-ui, -apple-system, 'Segoe UI', sans-serif"

const SEC_LABEL = {
  executive_summary:    'Executive Summary',
  kpi:                  'Key Metrics',
  recommendation:       'Recommendations',
  anomaly:              'Anomalies',
  trend:                'Trends',
  predictive_readiness: 'Readiness',
  chart:                'Charts',
  historical_comparison:'History',
  drift_detection:      'Drift',
  ai_findings:          'AI Findings',
  ai_insights:          'AI Intelligence',
  ai_recommendations:   'AI Recommendations',
  ai_dashboard:         'Executive Intelligence',
  insight_priority:     'Prioritized Insights',
  drilldown_table:      'Drilldown Table',
  forecast:             'Forecast',
  text:                 'Overview',
}

const PRIORITY = {
  executive_summary:    'high',
  kpi:                  'high',
  anomaly:              'high',
  drift_detection:      'high',
  ai_findings:          'high',
  ai_insights:          'high',
  ai_recommendations:   'high',
  ai_dashboard:         'high',
  insight_priority:     'high',
  recommendation:       'medium',
  predictive_readiness: 'medium',
  trend:                'medium',
  historical_comparison:'medium',
  forecast:             'medium',
  drilldown_table:      'medium',
  chart:                'low',
  text:                 'low',
}

const ROLE_ALLOW = {
  ceo:        new Set(['executive_summary','kpi','ai_dashboard','ai_recommendations','recommendation','anomaly','forecast','ai_findings']),
  executive:  new Set(['executive_summary','kpi','recommendation','anomaly','ai_findings','ai_recommendations','ai_dashboard','insight_priority']),
  analyst:    null,
  ml:         new Set(['kpi','anomaly','predictive_readiness','trend','chart','drift_detection','ai_findings','ai_insights','insight_priority']),
  operations: new Set(['kpi','recommendation','anomaly','drift_detection','historical_comparison','ai_findings','ai_recommendations','ai_dashboard','drilldown_table']),
}

const MODES = [
  { key: 'analyst',    label: 'Analyst'    },
  { key: 'ceo',        label: 'CEO'        },
  { key: 'executive',  label: 'Executive'  },
  { key: 'ml',         label: 'ML'         },
  { key: 'operations', label: 'Operations' },
]

// Style badge (inline — no external dep needed)
const STYLE_META = {
  executive_brief:    { label: 'Executive Brief',    color: '#6366f1', bg: 'rgba(99,102,241,0.10)',  border: 'rgba(99,102,241,0.25)'  },
  visual_dashboard:   { label: 'Visual Dashboard',   color: '#38bdf8', bg: 'rgba(56,189,248,0.10)',  border: 'rgba(56,189,248,0.25)'  },
  table_heavy_report: { label: 'Table-Heavy',        color: '#f59e0b', bg: 'rgba(245,158,11,0.10)',  border: 'rgba(245,158,11,0.25)'  },
  operational_report: { label: 'Operational',        color: '#10b981', bg: 'rgba(16,185,129,0.10)',  border: 'rgba(16,185,129,0.25)'  },
  anomaly_report:     { label: 'Anomaly Report',     color: '#f87171', bg: 'rgba(248,113,113,0.10)', border: 'rgba(248,113,113,0.25)' },
  kpi_summary:        { label: 'KPI Summary',        color: '#fbbf24', bg: 'rgba(251,191,36,0.10)',  border: 'rgba(251,191,36,0.25)'  },
  monitoring_report:  { label: 'Monitoring',         color: '#60a5fa', bg: 'rgba(96,165,250,0.10)',  border: 'rgba(96,165,250,0.25)'  },
}

// SectionRenderer is passed as a prop so ReportSection stays in App.jsx and
// ReportWorkspace has no import dependency on it — keeping this extraction small.
export default function ReportWorkspace({ sections, reportMeta, C, onExport, onEmail, SectionRenderer, token, reportPlan }) {
  const heroSet   = new Set(reportPlan?.layout_metadata?.hero_sections || [])
  const [expanded, setExpanded] = useState(() => {
    const s = new Set()
    sections.forEach((sec, i) => {
      const t = sec.type || 'text'
      // Always expand executive_summary; also expand any hero section from plan
      if (t === 'executive_summary' || heroSet.has(t)) s.add(i)
    })
    return s
  })
  const [mode, setMode]               = useState('analyst')
  const [query, setQuery]             = useState('')
  const [emailInput, setEmailInput]   = useState('')
  const [emailSending, setEmailSending] = useState(false)
  const [emailStatus, setEmailStatus] = useState(null)
  const [askQ, setAskQ]               = useState('')
  const [askLoading, setAskLoading]   = useState(false)
  const [askResult, setAskResult]     = useState(null)
  const sectionRefs = useRef({})

  const toggle = i => setExpanded(prev => {
    const n = new Set(prev)
    n.has(i) ? n.delete(i) : n.add(i)
    return n
  })
  const scrollTo = i => {
    if (!expanded.has(i)) toggle(i)
    setTimeout(() => sectionRefs.current[i]?.scrollIntoView({ behavior: 'smooth', block: 'start' }), 60)
  }

  // ── Derived overview metrics ──────────────────────────────────────────────
  const metrics = (() => {
    let readiness = null, readLevel = null, anomalies = 0, trends = 0, recs = 0, drifts = 0
    for (const s of sections) {
      const t = s.type || 'text'
      if (t === 'predictive_readiness') { readiness = s.readiness_score ?? null; readLevel = s.readiness_level ?? null }
      if (t === 'anomaly')              anomalies = (s.anomalies || []).filter(a => a.title !== 'No Major Anomalies Detected').length
      if (t === 'trend')                trends    = (s.trends    || []).filter(t2 => t2.title !== 'No Significant Trends Detected').length
      if (t === 'recommendation')       recs      = (s.recommendations || []).length
      if (t === 'drift_detection')      drifts    = (s.drifts    || []).length
    }
    return { readiness, readLevel, anomalies, trends, recs, drifts }
  })()

  // ── Filtered sections ─────────────────────────────────────────────────────
  const visible = sections.map((s, i) => ({ ...s, _i: i })).filter(s => {
    const t     = s.type || 'text'
    const allow = ROLE_ALLOW[mode]
    if (allow && !allow.has(t)) return false
    if (query) {
      const q = query.toLowerCase()
      const h = (s.heading || '').toLowerCase()
      const l = (SEC_LABEL[t] || t).toLowerCase()
      if (!h.includes(q) && !l.includes(q) && !t.replace(/_/g,' ').includes(q)) return false
    }
    return true
  })

  // ── Section item count badge ──────────────────────────────────────────────
  const sectionCount = s => {
    const t = s.type || 'text'
    if (t === 'anomaly')              return (s.anomalies      || []).length
    if (t === 'trend')                return (s.trends         || []).length
    if (t === 'recommendation')       return (s.recommendations|| []).length
    if (t === 'kpi')                  return (s.kpis           || []).length
    if (t === 'historical_comparison')return (s.comparisons    || []).length
    if (t === 'drift_detection')      return (s.drifts         || []).length
    if (t === 'insight_priority')     return (s.insights       || []).length
    if (t === 'drilldown_table')      return (s.rows           || []).length
    if (t === 'ai_dashboard')         return (s.watchlist      || []).length
    if (t === 'text')                 return (s.items          || []).length
    return null
  }

  // ── Dynamic priority from report_plan (falls back to PRIORITY dict) ──────
  const planScores = reportPlan?.section_scores || null
  const getPriority = type => {
    if (planScores && Object.prototype.hasOwnProperty.call(planScores, type)) {
      const score = planScores[type]
      if (score >= 8) return 'high'
      if (score >= 5) return 'medium'
      return 'low'
    }
    return PRIORITY[type] || 'low'
  }

  // ── Priority border colors ────────────────────────────────────────────────
  const priorityStyle = p => ({
    high:   { borderColor: `${C.danger}50`,  bg: `${C.danger}06`  },
    medium: { borderColor: `${C.accent}40`,  bg: `${C.accent}05`  },
    low:    { borderColor: C.border,         bg: 'transparent'     },
  }[p] || { borderColor: C.border, bg: 'transparent' })

  const readLevelColor = l => ({ high: C.success, medium: C.warn, low: C.danger }[l] || C.textMuted)

  const handleEmail = async () => {
    if (!emailInput.trim() || emailSending) return
    setEmailSending(true); setEmailStatus(null)
    try { await onEmail(emailInput.trim()); setEmailStatus({ ok: true, msg: 'Report sent.' }) }
    catch  { setEmailStatus({ ok: false, msg: 'Send failed.' }) }
    finally { setEmailSending(false) }
  }

  const handleAsk = async () => {
    if (!askQ.trim() || askLoading || !reportMeta?.id || !token) return
    setAskLoading(true); setAskResult(null)
    try {
      const res = await askReport(reportMeta.id, askQ.trim(), token)
      setAskResult(res?.data || null)
    } catch (err) {
      setAskResult({ answer: `Error: ${err.message}`, cited_sections_used: [], confidence: 'low', fallback_used: true })
    } finally {
      setAskLoading(false)
    }
  }

  return (
    <div style={{ display: 'flex', gap: '0', position: 'relative' }}>

      {/* ── Main column ── */}
      <div style={{ flex: 1, minWidth: 0 }}>

        {/* ── Overview header ── */}
        <div style={{ background: C.surface, border: `1px solid ${C.border}`, borderRadius: '10px', padding: '14px 16px', marginBottom: '12px' }}>
          {/* Top row: meta + exports */}
          <div style={{ display: 'flex', alignItems: 'flex-start', justifyContent: 'space-between', gap: '12px', flexWrap: 'wrap', marginBottom: '12px' }}>
            <div>
              <div style={{ display: 'flex', alignItems: 'center', gap: '8px', flexWrap: 'wrap', marginBottom: '4px' }}>
                <div style={{ fontSize: '1rem', fontWeight: '700', color: C.text, letterSpacing: '-0.3px' }}>
                  {reportMeta?.title || 'Report'}
                </div>
                {/* Report style badge — only when plan specifies a non-default style */}
                {(() => {
                  const s = reportPlan?.report_style
                  const m = s ? STYLE_META[s] : null
                  if (!m) return null
                  return (
                    <span style={{ display: 'inline-flex', alignItems: 'center', gap: '4px', background: m.bg, border: `1px solid ${m.border}`, borderRadius: '20px', padding: '2px 8px', fontSize: '0.56rem', fontWeight: '700', color: m.color, textTransform: 'uppercase', letterSpacing: '0.09em', flexShrink: 0 }}>
                      <span style={{ width: '4px', height: '4px', borderRadius: '50%', background: m.color }} />
                      {m.label}
                    </span>
                  )
                })()}
              </div>
              <div style={{ display: 'flex', flexWrap: 'wrap', gap: '8px', alignItems: 'center' }}>
                {reportMeta?.dataset_filename && (
                  <span style={{ fontSize: '0.68rem', color: C.textMuted, display: 'flex', alignItems: 'center', gap: '4px' }}>
                    <svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><rect x="3" y="3" width="7" height="7"/><rect x="14" y="3" width="7" height="7"/><rect x="14" y="14" width="7" height="7"/><rect x="3" y="14" width="7" height="7"/></svg>
                    {reportMeta.dataset_filename}
                  </span>
                )}
                {reportMeta?.created_at && (
                  <span style={{ fontSize: '0.68rem', color: C.textMuted }}>{new Date(reportMeta.created_at).toLocaleString(undefined, { month: 'short', day: 'numeric', year: 'numeric', hour: '2-digit', minute: '2-digit' })}</span>
                )}
                {reportMeta?.task_type && (
                  <span style={{ background: `${C.accent}18`, color: C.accent, border: `1px solid ${C.accent}40`, borderRadius: '20px', padding: '1px 8px', fontSize: '0.62rem', fontWeight: '600', letterSpacing: '0.03em' }}>
                    {reportMeta.task_type === 'email_dataset_report' ? 'Emailed Report' : 'Dataset Report'}
                  </span>
                )}
              </div>
            </div>
            {/* Export + email buttons */}
            <div style={{ display: 'flex', gap: '6px', flexWrap: 'wrap', alignItems: 'flex-start' }}>
              {[['json','JSON'],['pdf','PDF'],['csv','CSV']].map(([fmt, label]) => (
                <button key={fmt} onClick={() => onExport(fmt)}
                  style={{ display: 'flex', alignItems: 'center', gap: '5px', background: C.bg, border: `1px solid ${C.border}`, borderRadius: '7px', padding: '5px 10px', fontSize: '0.69rem', color: C.textSec, cursor: 'pointer', fontFamily: FONT, fontWeight: '500', whiteSpace: 'nowrap' }}
                  onMouseEnter={e => { e.currentTarget.style.borderColor = C.accent; e.currentTarget.style.color = C.accent }}
                  onMouseLeave={e => { e.currentTarget.style.borderColor = C.border;  e.currentTarget.style.color = C.textSec }}>
                  <svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/></svg>
                  {label}
                </button>
              ))}
              <button onClick={() => scrollTo(sections.findIndex(s => s.type === 'executive_summary') >= 0 ? sections.findIndex(s => s.type === 'executive_summary') : 0)}
                style={{ display: 'flex', alignItems: 'center', gap: '5px', background: C.bg, border: `1px solid ${C.border}`, borderRadius: '7px', padding: '5px 10px', fontSize: '0.69rem', color: C.textSec, cursor: 'pointer', fontFamily: FONT, fontWeight: '500', whiteSpace: 'nowrap' }}
                onMouseEnter={e => { e.currentTarget.style.borderColor = C.accent; e.currentTarget.style.color = C.accent }}
                onMouseLeave={e => { e.currentTarget.style.borderColor = C.border;  e.currentTarget.style.color = C.textSec }}>
                <svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M4 4h16c1.1 0 2 .9 2 2v12c0 1.1-.9 2-2 2H4c-1.1 0-2-.9-2-2V6c0-1.1.9-2 2-2z"/><polyline points="22,6 12,13 2,6"/></svg>
                Email
              </button>
            </div>
          </div>
          {/* Metric cards */}
          <div style={{ display: 'flex', gap: '8px', flexWrap: 'wrap' }}>
            {metrics.readiness != null && (
              <div style={{ background: `${readLevelColor(metrics.readLevel)}12`, border: `1px solid ${readLevelColor(metrics.readLevel)}30`, borderRadius: '8px', padding: '8px 12px', minWidth: '80px' }}>
                <div style={{ fontSize: '1.4rem', fontWeight: '800', color: readLevelColor(metrics.readLevel), lineHeight: 1 }}>{metrics.readiness}</div>
                <div style={{ fontSize: '0.6rem', color: readLevelColor(metrics.readLevel), fontWeight: '600', textTransform: 'uppercase', letterSpacing: '0.06em', marginTop: '3px' }}>Readiness</div>
              </div>
            )}
            {[
              { val: metrics.anomalies, label: 'Anomalies',   color: metrics.anomalies > 0 ? C.danger : C.success },
              { val: metrics.trends,    label: 'Trends',      color: C.accent  },
              { val: metrics.recs,      label: 'Actions',     color: C.warn    },
              ...(metrics.drifts > 0 ? [{ val: metrics.drifts, label: 'Drift Alerts', color: C.danger }] : []),
            ].map(({ val, label, color }) => (
              <div key={label} style={{ background: `${color}10`, border: `1px solid ${color}28`, borderRadius: '8px', padding: '8px 12px', minWidth: '70px' }}>
                <div style={{ fontSize: '1.4rem', fontWeight: '800', color, lineHeight: 1 }}>{val}</div>
                <div style={{ fontSize: '0.6rem', color, fontWeight: '600', textTransform: 'uppercase', letterSpacing: '0.06em', marginTop: '3px' }}>{label}</div>
              </div>
            ))}
          </div>
        </div>

        {/* ── Controls: mode selector + search ── */}
        <div style={{ display: 'flex', gap: '8px', alignItems: 'center', marginBottom: '10px', flexWrap: 'wrap' }}>
          <div style={{ display: 'flex', background: C.bg, border: `1px solid ${C.border}`, borderRadius: '8px', padding: '2px', gap: '2px' }}>
            {MODES.map(m => (
              <button key={m.key} onClick={() => setMode(m.key)}
                style={{ background: mode === m.key ? C.accent : 'transparent', color: mode === m.key ? '#fff' : C.textSec, border: 'none', borderRadius: '6px', padding: '4px 10px', fontSize: '0.69rem', fontWeight: '600', cursor: 'pointer', fontFamily: FONT, transition: 'background 0.12s, color 0.12s', whiteSpace: 'nowrap' }}>
                {m.label}
              </button>
            ))}
          </div>
          <input
            placeholder="Filter sections…"
            value={query}
            onChange={e => setQuery(e.target.value)}
            style={{ flex: 1, minWidth: '140px', background: C.bg, border: `1px solid ${C.border}`, borderRadius: '8px', padding: '6px 10px', fontSize: '0.75rem', color: C.text, fontFamily: FONT, outline: 'none' }}
          />
          {query && (
            <button onClick={() => setQuery('')}
              style={{ background: 'none', border: 'none', color: C.textMuted, cursor: 'pointer', fontSize: '0.75rem', fontFamily: FONT, padding: '0 4px' }}>✕</button>
          )}
        </div>

        {/* ── Collapsible section panels ── */}
        {visible.length === 0 ? (
          <div style={{ padding: '32px', textAlign: 'center', color: C.textMuted, fontSize: '0.8rem' }}>No sections match this view.</div>
        ) : visible.map((sec, visIdx) => {
          const idx  = sec._i
          const t    = sec.type || 'text'
          const open = expanded.has(idx)
          const p    = getPriority(t)
          const ps   = priorityStyle(p)
          const cnt  = sectionCount(sec)
          const lbl  = sec.heading || SEC_LABEL[t] || t
          return (
            <div key={idx} ref={el => { sectionRefs.current[idx] = el }}
              style={{ marginBottom: '6px', background: open ? ps.bg : 'transparent', border: `1px solid ${open ? ps.borderColor : C.border}`, borderRadius: '10px', overflow: 'hidden', transition: 'border-color 0.15s, background 0.15s' }}>
              {/* Panel header */}
              <button onClick={() => toggle(idx)}
                style={{ width: '100%', display: 'flex', alignItems: 'center', gap: '10px', padding: '10px 14px', background: 'none', border: 'none', cursor: 'pointer', textAlign: 'left', fontFamily: FONT }}>
                <span style={{ fontSize: '0.62rem', color: open ? C.accent : C.textMuted, transition: 'transform 0.15s', display: 'inline-block', transform: open ? 'rotate(90deg)' : 'none', flexShrink: 0 }}>▶</span>
                <span style={{ flex: 1, fontSize: '0.8rem', fontWeight: p === 'high' ? '700' : '600', color: C.text, letterSpacing: p === 'high' ? '-0.2px' : 'normal' }}>{lbl}</span>
                {cnt != null && cnt > 0 && (
                  <span style={{ background: C.borderAlt, color: C.textMuted, borderRadius: '20px', padding: '1px 7px', fontSize: '0.62rem', fontWeight: '600', flexShrink: 0 }}>{cnt}</span>
                )}
                {p === 'high' && (
                  <span style={{ background: `${C.danger}18`, color: C.danger, border: `1px solid ${C.danger}30`, borderRadius: '4px', padding: '1px 6px', fontSize: '0.58rem', fontWeight: '700', letterSpacing: '0.05em', flexShrink: 0 }}>PRIORITY</span>
                )}
              </button>
              {/* Panel content */}
              {open && (
                <div style={{ padding: '4px 14px 14px' }}>
                  <SectionRenderer section={sec} C={C} />
                </div>
              )}
            </div>
          )
        })}

        {/* ── Ask This Report panel ── */}
        <div style={{ marginTop: '12px', paddingTop: '12px', borderTop: `1px solid ${C.border}` }}>
          <div style={{ fontSize: '0.68rem', fontWeight: '600', color: C.textSec, marginBottom: '7px', textTransform: 'uppercase', letterSpacing: '0.06em', display: 'flex', alignItems: 'center', gap: '7px' }}>
            Ask This Report
            <span style={{ background: `${C.accent}18`, color: C.accent, border: `1px solid ${C.accent}30`, borderRadius: '4px', padding: '1px 6px', fontSize: '0.58rem', fontWeight: '700', letterSpacing: '0.05em' }}>AI</span>
          </div>
          <div style={{ display: 'flex', gap: '7px', alignItems: 'center' }}>
            <input
              placeholder="Ask a question about this report…"
              value={askQ}
              onChange={e => { setAskQ(e.target.value); setAskResult(null) }}
              onKeyDown={e => { if (e.key === 'Enter') handleAsk() }}
              style={{ flex: 1, background: C.surface, border: `1px solid ${C.border}`, borderRadius: '7px', padding: '6px 10px', fontSize: '0.74rem', color: C.text, fontFamily: FONT, outline: 'none' }}
            />
            <button onClick={handleAsk} disabled={askLoading || !askQ.trim()}
              style={{ display: 'flex', alignItems: 'center', gap: '5px', background: askLoading ? C.bg : C.accent, border: `1px solid ${askLoading ? C.border : C.accent}`, borderRadius: '7px', padding: '6px 12px', fontSize: '0.72rem', color: askLoading ? C.textSec : '#fff', cursor: askLoading || !askQ.trim() ? 'not-allowed' : 'pointer', fontFamily: FONT, fontWeight: '600', opacity: !askQ.trim() ? 0.5 : 1, flexShrink: 0, transition: 'background 0.12s, color 0.12s' }}>
              {askLoading ? 'Thinking…' : 'Ask'}
            </button>
          </div>
          {askResult && (
            <div style={{ marginTop: '10px', background: `${C.accent}08`, border: `1px solid ${C.accent}28`, borderRadius: '9px', padding: '11px 14px' }}>
              <div style={{ fontSize: '0.75rem', color: C.text, lineHeight: 1.65, marginBottom: '8px' }}>{askResult.answer}</div>
              {(askResult.cited_sections_used || []).length > 0 && (
                <div style={{ display: 'flex', flexWrap: 'wrap', gap: '5px', marginBottom: '6px' }}>
                  {askResult.cited_sections_used.map((s, i) => (
                    <span key={i} style={{ background: C.borderAlt, color: C.textMuted, borderRadius: '4px', padding: '1px 7px', fontSize: '0.62rem', fontWeight: '500' }}>{s}</span>
                  ))}
                </div>
              )}
              <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
                {askResult.confidence && (
                  <span style={{ fontSize: '0.6rem', color: askResult.confidence === 'high' ? C.success : askResult.confidence === 'medium' ? C.warn : C.textMuted, fontWeight: '700', textTransform: 'uppercase', letterSpacing: '0.06em' }}>
                    {askResult.confidence} confidence
                  </span>
                )}
                {askResult.fallback_used && (
                  <span style={{ fontSize: '0.6rem', color: C.textMuted }}>· deterministic fallback</span>
                )}
              </div>
            </div>
          )}
        </div>

        {/* ── Email panel ── */}
        <div style={{ marginTop: '12px', paddingTop: '12px', borderTop: `1px solid ${C.border}` }}>
          <div style={{ fontSize: '0.68rem', fontWeight: '600', color: C.textSec, marginBottom: '7px', textTransform: 'uppercase', letterSpacing: '0.06em' }}>Email this report</div>
          <div style={{ display: 'flex', gap: '7px', alignItems: 'center' }}>
            <input type="email" placeholder="Recipient email" value={emailInput}
              onChange={e => { setEmailInput(e.target.value); setEmailStatus(null) }}
              style={{ flex: 1, background: C.surface, border: `1px solid ${C.border}`, borderRadius: '7px', padding: '6px 10px', fontSize: '0.74rem', color: C.text, fontFamily: FONT, outline: 'none' }} />
            <button onClick={handleEmail} disabled={emailSending}
              style={{ display: 'flex', alignItems: 'center', gap: '5px', background: C.bg, border: `1px solid ${C.border}`, borderRadius: '7px', padding: '6px 12px', fontSize: '0.72rem', color: C.textSec, cursor: emailSending ? 'not-allowed' : 'pointer', fontFamily: FONT, fontWeight: '500', opacity: emailSending ? 0.6 : 1, flexShrink: 0 }}
              onMouseEnter={e => { if (!emailSending) { e.currentTarget.style.borderColor = C.accent; e.currentTarget.style.color = C.accent }}}
              onMouseLeave={e => { e.currentTarget.style.borderColor = C.border; e.currentTarget.style.color = C.textSec }}>
              <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M4 4h16c1.1 0 2 .9 2 2v12c0 1.1-.9 2-2 2H4c-1.1 0-2-.9-2-2V6c0-1.1.9-2 2-2z"/><polyline points="22,6 12,13 2,6"/></svg>
              {emailSending ? 'Sending…' : 'Send'}
            </button>
          </div>
          {emailStatus && (
            <div style={{ marginTop: '6px', fontSize: '0.7rem', color: emailStatus.ok ? C.success : C.danger, display: 'flex', alignItems: 'center', gap: '5px' }}>
              <svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
                {emailStatus.ok ? <polyline points="20 6 9 17 4 12"/> : <><circle cx="12" cy="12" r="10"/><line x1="12" y1="8" x2="12" y2="12"/><line x1="12" y1="16" x2="12.01" y2="16"/></>}
              </svg>
              {emailStatus.msg}
            </div>
          )}
        </div>
      </div>

      {/* ── Right nav rail (hidden on narrow layouts) ── */}
      <div style={{ width: '130px', flexShrink: 0, marginLeft: '12px', display: 'none' }} className="rw-nav-rail">
        <div style={{ position: 'sticky', top: '20px', background: C.surface, border: `1px solid ${C.border}`, borderRadius: '10px', padding: '10px 0', overflow: 'hidden' }}>
          <div style={{ fontSize: '0.57rem', color: C.textMuted, fontWeight: '700', textTransform: 'uppercase', letterSpacing: '0.08em', padding: '0 12px', marginBottom: '6px' }}>Sections</div>
          {visible.map(sec => {
            const idx    = sec._i
            const t      = sec.type || 'text'
            const lbl    = SEC_LABEL[t] || sec.heading || t
            const p      = PRIORITY[t] || 'low'
            const isOpen = expanded.has(idx)
            return (
              <button key={idx} onClick={() => scrollTo(idx)}
                style={{ width: '100%', display: 'block', textAlign: 'left', background: 'none', border: 'none', cursor: 'pointer', padding: '5px 12px', fontFamily: FONT }}>
                <span style={{ fontSize: '0.68rem', color: isOpen ? C.accent : (p === 'high' ? C.text : C.textSec), fontWeight: isOpen ? '600' : '400', display: 'block', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                  {p === 'high' && <span style={{ color: C.danger, marginRight: '4px', fontSize: '0.5rem' }}>●</span>}
                  {lbl}
                </span>
              </button>
            )
          })}
        </div>
      </div>
    </div>
  )
}
