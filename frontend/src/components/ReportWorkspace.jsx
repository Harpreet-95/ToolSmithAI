import { useState } from 'react'
import { askReport } from '../api/client'

const FONT = "system-ui, -apple-system, 'Segoe UI', sans-serif"

const STYLE_META = {
  executive_brief:    { label: 'Executive Brief',    color: '#6366f1', bg: 'rgba(99,102,241,0.10)',  border: 'rgba(99,102,241,0.25)'  },
  visual_dashboard:   { label: 'Visual Dashboard',   color: '#38bdf8', bg: 'rgba(56,189,248,0.10)',  border: 'rgba(56,189,248,0.25)'  },
  table_heavy_report: { label: 'Table-Heavy',        color: '#f59e0b', bg: 'rgba(245,158,11,0.10)',  border: 'rgba(245,158,11,0.25)'  },
  operational_report: { label: 'Operational',        color: '#10b981', bg: 'rgba(16,185,129,0.10)',  border: 'rgba(16,185,129,0.25)'  },
  anomaly_report:     { label: 'Anomaly Report',     color: '#f87171', bg: 'rgba(248,113,113,0.10)', border: 'rgba(248,113,113,0.25)' },
  kpi_summary:        { label: 'KPI Summary',        color: '#fbbf24', bg: 'rgba(251,191,36,0.10)',  border: 'rgba(251,191,36,0.25)'  },
  monitoring_report:  { label: 'Monitoring',         color: '#60a5fa', bg: 'rgba(96,165,250,0.10)',  border: 'rgba(96,165,250,0.25)'  },
}

const INTENT_META = {
  executive_brief:   { label: 'Executive Brief',   color: '#6366f1', bg: 'rgba(99,102,241,0.10)',  border: 'rgba(99,102,241,0.25)'  },
  kpi_scorecard:     { label: 'KPI Scorecard',     color: '#fbbf24', bg: 'rgba(251,191,36,0.10)',  border: 'rgba(251,191,36,0.25)'  },
  anomaly_focus:     { label: 'Anomaly Focus',     color: '#f87171', bg: 'rgba(248,113,113,0.10)', border: 'rgba(248,113,113,0.25)' },
  trend_monitoring:  { label: 'Trend Monitoring',  color: '#60a5fa', bg: 'rgba(96,165,250,0.10)',  border: 'rgba(96,165,250,0.25)'  },
  data_quality:      { label: 'Data Quality',      color: '#f59e0b', bg: 'rgba(245,158,11,0.10)',  border: 'rgba(245,158,11,0.25)'  },
  visual_dashboard:  { label: 'Visual Dashboard',  color: '#38bdf8', bg: 'rgba(56,189,248,0.10)',  border: 'rgba(56,189,248,0.25)'  },
  full_intelligence: { label: 'Full Intelligence', color: '#10b981', bg: 'rgba(16,185,129,0.10)',  border: 'rgba(16,185,129,0.25)'  },
}

const SEC_LABEL = {
  executive_summary:    'Executive Summary',
  kpi:                  'Key Metrics',
  business_kpis:        'Business KPIs',
  recommendation:       'Recommendations',
  ai_recommendations:   'AI Recommendations',
  anomaly:              'Anomalies',
  trend:                'Trends',
  predictive_readiness: 'Predictive Readiness',
  chart:                'Chart',
  historical_comparison:'Historical Comparison',
  drift_detection:      'Drift Detection',
  ai_findings:          'AI Findings',
  ai_insights:          'AI Intelligence',
  ai_dashboard:         'Executive Intelligence',
  insight_priority:     'Prioritized Insights',
  drilldown_table:      'Data Table',
  forecast:             'Forecast',
  text:                 'Overview',
  segmentation:         'Segmentation',
}

const TABS = [
  { key: 'overview', label: 'Overview' },
  { key: 'charts',   label: 'Charts'   },
  { key: 'data',     label: 'Data'     },
  { key: 'ask',      label: 'Ask AI'   },
]

// Derived owner from action_type — frontend only, no backend field needed
const ACTION_OWNER = {
  clean_data: 'Data Team',
  schedule:   'Analytics Team',
  review:     'Analyst',
  segment:    'Growth Team',
}

// ── Section routing ────────────────────────────────────────────────────────────
// type:'kpi'          → g.metaKpi (dataset metadata, Data tab only)
// type:'business_kpis'→ g.kpi    (business KPIs, Overview only)
function groupSections(sections) {
  const g = {
    execSummary:    [],
    kpi:            [],   // business_kpis only
    metaKpi:        [],   // dataset metadata kpis — Data tab only
    insight:        [],
    recommendation: [],
    chart:          [],
    data:           [],
  }
  for (const sec of sections) {
    const t = sec.type || 'text'
    if      (t === 'executive_summary')                                        g.execSummary.push(sec)
    else if (t === 'business_kpis')                                            g.kpi.push(sec)
    else if (t === 'kpi')                                                      g.metaKpi.push(sec)
    else if (['insight_priority','ai_insights','ai_findings','ai_dashboard'].includes(t)) g.insight.push(sec)
    else if (['recommendation','ai_recommendations'].includes(t))              g.recommendation.push(sec)
    else if (t === 'chart')                                                    g.chart.push(sec)
    else                                                                       g.data.push(sec)
  }
  return g
}

// ── Shared label style ────────────────────────────────────────────────────────
function SectionLabel({ text, C }) {
  return (
    <div style={{ fontSize: '0.68rem', fontWeight: '700', color: C.textSec, textTransform: 'uppercase', letterSpacing: '0.09em', marginBottom: '12px' }}>
      {text}
    </div>
  )
}

// ── ExecutiveKpiGrid ──────────────────────────────────────────────────────────
// Renders business_kpis cards with status border, formatted value, delta badge.
// Never shows dataset metadata (Total Records, Total Features, etc.).
function ExecutiveKpiGrid({ sections, C }) {
  const kpis = sections.flatMap(s => s.kpis || s.metrics || [])
  if (!kpis.length) return null

  return (
    <div style={{ display: 'flex', flexWrap: 'nowrap', gap: '10px', overflowX: 'auto' }}>
      {kpis.map((kpi, i) => {
        const statusColor  = { good: C.success, warning: C.warn, risk: C.danger }[kpi.status] || C.textMuted
        const displayValue = kpi.value_formatted || kpi.value_display || String(kpi.value ?? '—')
        const hasDelta     = kpi.delta !== null && kpi.delta !== undefined

        return (
          <div key={i} style={{
            flex:         '1 1 0',
            minWidth:     0,
            background:   C.surface,
            border:       `1px solid ${C.border}70`,
            borderLeft:   `3px solid ${statusColor}`,
            borderRadius: '10px',
            padding:      '8px 12px',
          }}>
            <div style={{ fontSize: '0.68rem', color: C.textSec, fontWeight: '500', lineHeight: 1.3, marginBottom: '3px' }}>
              {kpi.label}
            </div>
            <div style={{ fontSize: '1.35rem', fontWeight: '600', color: C.text, lineHeight: 1, letterSpacing: '-0.3px', marginBottom: hasDelta ? '6px' : 0 }}>
              {displayValue}
            </div>
            {hasDelta && (
              <div style={{ display: 'flex', alignItems: 'center', gap: '6px', flexWrap: 'wrap' }}>
                <span style={{ display: 'inline-flex', alignItems: 'center', gap: '2px', fontSize: '0.68rem', fontWeight: '700', color: statusColor }}>
                  <span>{kpi.delta_direction === 'up' ? '↑' : kpi.delta_direction === 'down' ? '↓' : '→'}</span>
                  <span>{Math.abs(Number(kpi.delta)).toFixed(1)}%</span>
                </span>
              </div>
            )}
          </div>
        )
      })}
    </div>
  )
}

// ── ExecutiveIntelligencePanel ────────────────────────────────────────────────
// Renders Key Decisions / Risks / Opportunities from executive_summary
// structured fields: key_takeaways[], risks[], opportunities[].
function ExecutiveIntelligencePanel({ sections, C }) {
  const decisions     = sections.flatMap(s => s.key_takeaways  || []).slice(0, 5)
  const risks         = sections.flatMap(s => s.risks          || []).slice(0, 4)
  const opportunities = sections.flatMap(s => s.opportunities  || []).slice(0, 4)

  const panels = [
    { key: 'decisions',     label: 'Key Decisions',  items: decisions,     color: C.accent  },
    { key: 'risks',         label: 'Risks',           items: risks,         color: C.danger  },
    { key: 'opportunities', label: 'Opportunities',   items: opportunities, color: C.success },
  ].filter(p => p.items.length > 0)

  if (!panels.length) return null

  return (
    <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(200px, 1fr))', gap: '16px', alignItems: 'start' }}>
      {panels.map(panel => (
        <div key={panel.key} style={{ background: C.surface, border: `1px solid ${C.border}`, borderRadius: '10px', padding: '18px 20px' }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: '7px', marginBottom: '12px' }}>
            <div style={{ width: '6px', height: '6px', borderRadius: '50%', background: panel.color, flexShrink: 0 }} />
            <span style={{ fontSize: '0.67rem', fontWeight: '700', color: panel.color, textTransform: 'uppercase', letterSpacing: '0.09em' }}>
              {panel.label}
            </span>
          </div>
          <div style={{ display: 'flex', flexDirection: 'column', gap: '9px' }}>
            {panel.items.map((item, j) => (
              <div key={j} style={{ display: 'flex', gap: '9px', alignItems: 'flex-start' }}>
                <div style={{ width: '3px', height: '3px', borderRadius: '50%', background: panel.color, opacity: 0.55, flexShrink: 0, marginTop: '8px' }} />
                <span style={{ fontSize: '0.79rem', color: C.textSec, lineHeight: 1.6 }}>{item}</span>
              </div>
            ))}
          </div>
        </div>
      ))}
    </div>
  )
}

// ── ExecutiveRecommendationList ───────────────────────────────────────────────
// Renders recommendations with:
//   title, priority badge, confidence badge,
//   business_impact (= rec.reason), owner (derived from action_type)
function ExecutiveRecommendationList({ sections, C }) {
  const recs = sections.flatMap(s => s.recommendations || [])
  if (!recs.length) return null

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: '10px' }}>
      {recs.map((rec, i) => {
        const priorityColor    = { high: C.danger, medium: C.warn, low: C.success }[rec.priority] || C.textMuted
        const confidenceColor  = { high: C.success, medium: C.warn }[rec.confidence] || C.textMuted
        const owner            = ACTION_OWNER[rec.action_type] || 'Team'

        return (
          <div key={i} style={{ background: C.surface, border: `1px solid ${C.border}`, borderRadius: '10px', padding: '16px 20px' }}>

            {/* Title + badges */}
            <div style={{ display: 'flex', alignItems: 'flex-start', justifyContent: 'space-between', gap: '12px', marginBottom: rec.reason ? '10px' : '10px' }}>
              <span style={{ fontSize: '0.84rem', fontWeight: '700', color: C.text, lineHeight: 1.4, flex: 1 }}>
                {rec.title}
              </span>
              <div style={{ display: 'flex', gap: '5px', flexShrink: 0, flexWrap: 'wrap', justifyContent: 'flex-end' }}>
                <span style={{ background: `${priorityColor}15`, color: priorityColor, border: `1px solid ${priorityColor}30`, borderRadius: '4px', padding: '2px 7px', fontSize: '0.6rem', fontWeight: '700', textTransform: 'uppercase', letterSpacing: '0.07em' }}>
                  {rec.priority}
                </span>
                {rec.confidence && (
                  <span style={{ background: `${confidenceColor}12`, color: confidenceColor, border: `1px solid ${confidenceColor}25`, borderRadius: '4px', padding: '2px 7px', fontSize: '0.6rem', fontWeight: '600', letterSpacing: '0.04em' }}>
                    {rec.confidence} conf
                  </span>
                )}
              </div>
            </div>

            {/* Business impact — derived from rec.reason */}
            {rec.reason && (
              <div style={{ fontSize: '0.78rem', color: C.textSec, lineHeight: 1.6, marginBottom: '10px' }}>
                <span style={{ fontSize: '0.62rem', fontWeight: '700', color: C.textMuted, textTransform: 'uppercase', letterSpacing: '0.07em', marginRight: '6px' }}>
                  Business Impact
                </span>
                {rec.reason}
              </div>
            )}

            {/* Owner */}
            <div style={{ display: 'flex', alignItems: 'center', gap: '5px', paddingTop: '8px', borderTop: `1px solid ${C.border}` }}>
              <svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" style={{ color: C.textMuted, flexShrink: 0 }}><path d="M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2"/><circle cx="12" cy="7" r="4"/></svg>
              <span style={{ fontSize: '0.67rem', color: C.textMuted, fontWeight: '500' }}>{owner}</span>
            </div>

          </div>
        )
      })}
    </div>
  )
}

// ── ExecutiveSummaryProse ─────────────────────────────────────────────────────
// Renders only the leadership narrative text (section.summary field).
// Placed at the bottom of Overview, below all visual intelligence.
function ExecutiveSummaryProse({ sections, C }) {
  const texts = sections.map(s => s.summary).filter(Boolean)
  if (!texts.length) return null

  return (
    <div style={{ background: C.surface, border: `1px solid ${C.border}`, borderRadius: '10px', padding: '22px 26px' }}>
      {texts.map((text, i) => (
        <p key={i} style={{ margin: i > 0 ? '12px 0 0' : 0, fontSize: '0.84rem', color: C.textSec, lineHeight: 1.75 }}>
          {text}
        </p>
      ))}
    </div>
  )
}

// ── KpiGrid — dataset metadata only (Data tab) ────────────────────────────────
function KpiGrid({ sections, C }) {
  const kpis = sections.flatMap(s => s.kpis || s.metrics || [])
  if (!kpis.length) return null

  return (
    <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(180px, 1fr))', gap: '12px' }}>
      {kpis.map((kpi, i) => {
        const val = kpi.value_formatted || String(kpi.value ?? '—')
        return (
          <div key={i} style={{ background: C.surface, border: `1px solid ${C.border}`, borderRadius: '10px', padding: '16px 18px' }}>
            <div style={{ fontSize: '1.4rem', fontWeight: '800', color: C.text, lineHeight: 1, letterSpacing: '-0.5px', marginBottom: '6px' }}>
              {val}
            </div>
            <div style={{ fontSize: '0.74rem', color: C.textSec, fontWeight: '500', lineHeight: 1.3 }}>{kpi.label}</div>
            {kpi.description && (
              <div style={{ fontSize: '0.67rem', color: C.textMuted, marginTop: '5px', lineHeight: 1.4 }}>{kpi.description}</div>
            )}
          </div>
        )
      })}
    </div>
  )
}

// ── SectionBlock ──────────────────────────────────────────────────────────────
function SectionBlock({ section, C, SectionRenderer, label }) {
  const heading = label || section.heading || SEC_LABEL[section.type || 'text'] || section.type
  return (
    <div style={{ marginBottom: '24px' }}>
      {heading && (
        <div style={{ fontSize: '0.68rem', fontWeight: '700', color: C.textSec, textTransform: 'uppercase', letterSpacing: '0.09em', marginBottom: '10px' }}>
          {heading}
        </div>
      )}
      <div style={{ background: C.surface, border: `1px solid ${C.border}`, borderRadius: '10px', padding: '20px 24px' }}>
        <SectionRenderer section={section} C={C} />
      </div>
    </div>
  )
}

// ── TabEmpty ──────────────────────────────────────────────────────────────────
function TabEmpty({ message, C }) {
  return (
    <div style={{ padding: '64px 0', textAlign: 'center' }}>
      <div style={{ fontSize: '0.85rem', color: C.textMuted }}>{message}</div>
    </div>
  )
}

// ── Main component ────────────────────────────────────────────────────────────
export default function ReportWorkspace({ sections, reportMeta, C, onExport, onEmail, SectionRenderer, token, reportPlan, onBack }) {
  const [activeTab,    setActiveTab]    = useState('overview')
  const [emailInput,   setEmailInput]   = useState('')
  const [emailSending, setEmailSending] = useState(false)
  const [emailStatus,  setEmailStatus]  = useState(null)
  const [emailOpen,    setEmailOpen]    = useState(false)
  const [exportOpen,   setExportOpen]   = useState(false)
  const [moreOpen,     setMoreOpen]     = useState(false)
  const [askQ,         setAskQ]         = useState('')
  const [askLoading,   setAskLoading]   = useState(false)
  const [askResult,    setAskResult]    = useState(null)

  const g          = groupSections(sections)
  const intentType = reportPlan?.strategy_intent_type || null
  const styleMeta  = (intentType ? INTENT_META[intentType] : null)
                  || (reportPlan?.report_style ? STYLE_META[reportPlan.report_style] : null)
  const proseFirst = intentType === 'executive_brief' || intentType === 'kpi_scorecard'

  const tabCount = {
    charts: g.chart.length,
    data:   g.data.length + g.metaKpi.length,
  }

  // Top 3 charts for Overview row — ranked by backend overview_rank when available,
  // otherwise falls back to first 3 in original order (backwards compatible).
  const overviewCharts = g.chart.some(s => s.overview_chart != null)
    ? [...g.chart].filter(s => s.overview_chart).sort((a, b) => (a.overview_rank ?? 99) - (b.overview_rank ?? 99)).slice(0, 3)
    : g.chart.slice(0, 3)

  const handleEmail = async () => {
    if (!emailInput.trim() || emailSending) return
    setEmailSending(true)
    setEmailStatus(null)
    try {
      await onEmail(emailInput.trim())
      setEmailStatus({ ok: true, msg: 'Report sent.' })
    } catch {
      setEmailStatus({ ok: false, msg: 'Send failed.' })
    } finally {
      setEmailSending(false)
    }
  }

  const handleAsk = async () => {
    if (!askQ.trim() || askLoading || !reportMeta?.id || !token) return
    setAskLoading(true)
    setAskResult(null)
    try {
      const res = await askReport(reportMeta.id, askQ.trim(), token)
      setAskResult(res?.data || null)
    } catch (err) {
      setAskResult({ answer: `Error: ${err.message}`, cited_sections_used: [], confidence: 'low', fallback_used: true })
    } finally {
      setAskLoading(false)
    }
  }

  const menuItemBase = {
    display: 'flex', alignItems: 'center', gap: '8px', width: '100%',
    background: 'transparent', border: 'none', borderRadius: '5px',
    padding: '7px 10px', fontSize: '0.75rem', cursor: 'pointer',
    fontFamily: FONT, fontWeight: '500', textAlign: 'left',
  }

  // Shorthand for section label divs used repeatedly in Overview
  const sl = (text) => (
    <div style={{ fontSize: '0.68rem', fontWeight: '700', color: C.textSec, textTransform: 'uppercase', letterSpacing: '0.09em', marginBottom: '12px' }}>
      {text}
    </div>
  )

  return (
    <div style={{ display: 'flex', flexDirection: 'column', fontFamily: FONT, minHeight: '100%' }}>

      {/* ── DASHBOARD HEADER ──────────────────────────────────────────────── */}
      <div style={{ background: C.surface, padding: '4px 32px 0', position: 'sticky', top: 0, zIndex: 10 }}>

        {/* Title + actions row */}
        <div style={{ display: 'flex', alignItems: 'flex-start', justifyContent: 'space-between', gap: '20px', flexWrap: 'wrap', marginBottom: '2px' }}>

          {/* Left: title and meta */}
          <div style={{ flex: 1, minWidth: 0 }}>
            <div style={{ display: 'flex', alignItems: 'center', gap: '10px', flexWrap: 'wrap', marginBottom: '4px' }}>
              <h1 style={{ margin: 0, fontSize: '1.25rem', fontWeight: '600', color: C.text, letterSpacing: '-0.3px', lineHeight: 1.2 }}>
                {reportMeta?.title || 'Report'}
              </h1>
              {styleMeta && (
                <span style={{ display: 'inline-flex', alignItems: 'center', gap: '4px', background: styleMeta.bg, border: `1px solid ${styleMeta.border}`, borderRadius: '20px', padding: '1px 6px', fontSize: '0.5rem', fontWeight: '700', color: styleMeta.color, textTransform: 'uppercase', letterSpacing: '0.08em', flexShrink: 0 }}>
                  <span style={{ width: '3px', height: '3px', borderRadius: '50%', background: styleMeta.color }} />
                  {styleMeta.label}
                </span>
              )}
            </div>
            <div style={{ display: 'flex', flexWrap: 'wrap', gap: '10px', alignItems: 'center', marginBottom: '0px' }}>
              {reportMeta?.dataset_filename && (
                <span style={{ fontSize: '0.72rem', color: C.textMuted }}>
                  Generated from <span style={{ fontWeight: '500', color: C.textSec }}>{reportMeta.dataset_filename}</span>
                </span>
              )}
              {reportMeta?.created_at && (
                <span style={{ fontSize: '0.72rem', color: C.textMuted }}>
                  · {new Date(reportMeta.created_at).toLocaleString(undefined, { month: 'short', day: 'numeric', year: 'numeric', hour: '2-digit', minute: '2-digit' })}
                </span>
              )}
              {reportMeta?.task_type && (
                <span style={{ background: `${C.accent}18`, color: C.accent, border: `1px solid ${C.accent}40`, borderRadius: '20px', padding: '2px 9px', fontSize: '0.63rem', fontWeight: '600' }}>
                  {reportMeta.task_type === 'email_dataset_report' ? 'Emailed Report' : 'Dataset Report'}
                </span>
              )}
            </div>
          </div>

          {/* Right: [Export ▼] [⋯] */}
          <div style={{ display: 'flex', gap: '7px', alignItems: 'center', flexShrink: 0 }}>

            {/* Export ▼ */}
            <div style={{ position: 'relative' }}>
              {exportOpen && <div style={{ position: 'fixed', inset: 0, zIndex: 98 }} onClick={() => setExportOpen(false)} />}
              <button
                onClick={() => setExportOpen(v => !v)}
                style={{ display: 'flex', alignItems: 'center', gap: '5px', background: exportOpen ? C.accentSoft : 'transparent', border: `1px solid ${exportOpen ? C.accent : C.border}`, borderRadius: '7px', padding: '6px 12px', fontSize: '0.72rem', color: exportOpen ? C.accent : C.textSec, cursor: 'pointer', fontFamily: FONT, fontWeight: '500', transition: 'border-color 0.12s, color 0.12s, background 0.12s' }}
                onMouseEnter={e => { if (!exportOpen) { e.currentTarget.style.borderColor = C.accent; e.currentTarget.style.color = C.accent } }}
                onMouseLeave={e => { if (!exportOpen) { e.currentTarget.style.borderColor = C.border; e.currentTarget.style.color = C.textSec } }}>
                <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/></svg>
                Export
                <svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round" style={{ transition: 'transform 0.15s', transform: exportOpen ? 'rotate(180deg)' : 'none' }}><polyline points="6 9 12 15 18 9"/></svg>
              </button>
              {exportOpen && (
                <div style={{ position: 'absolute', top: 'calc(100% + 6px)', right: 0, background: C.surface, border: `1px solid ${C.border}`, borderRadius: '8px', padding: '4px', zIndex: 99, minWidth: '120px', boxShadow: '0 8px 24px rgba(0,0,0,0.25)' }}>
                  {[['pdf', 'PDF'], ['csv', 'CSV'], ['json', 'JSON']].map(([fmt, label]) => (
                    <button key={fmt}
                      onClick={() => { onExport(fmt); setExportOpen(false) }}
                      style={{ ...menuItemBase, color: C.textSec }}
                      onMouseEnter={e => { e.currentTarget.style.background = C.borderAlt; e.currentTarget.style.color = C.text }}
                      onMouseLeave={e => { e.currentTarget.style.background = 'transparent'; e.currentTarget.style.color = C.textSec }}>
                      <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/></svg>
                      {label}
                    </button>
                  ))}
                </div>
              )}
            </div>

            {/* ⋯ More menu */}
            <div style={{ position: 'relative' }}>
              {moreOpen && <div style={{ position: 'fixed', inset: 0, zIndex: 98 }} onClick={() => setMoreOpen(false)} />}
              <button
                onClick={() => setMoreOpen(v => !v)}
                title="More options"
                style={{ display: 'flex', alignItems: 'center', justifyContent: 'center', background: moreOpen ? C.accentSoft : 'transparent', border: `1px solid ${moreOpen ? C.accent : C.border}`, borderRadius: '7px', padding: '6px 9px', color: moreOpen ? C.accent : C.textSec, cursor: 'pointer', transition: 'border-color 0.12s, color 0.12s, background 0.12s' }}
                onMouseEnter={e => { if (!moreOpen) { e.currentTarget.style.borderColor = C.accent; e.currentTarget.style.color = C.accent } }}
                onMouseLeave={e => { if (!moreOpen) { e.currentTarget.style.borderColor = C.border; e.currentTarget.style.color = C.textSec } }}>
                <svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor"><circle cx="5" cy="12" r="2"/><circle cx="12" cy="12" r="2"/><circle cx="19" cy="12" r="2"/></svg>
              </button>
              {moreOpen && (
                <div style={{ position: 'absolute', top: 'calc(100% + 6px)', right: 0, background: C.surface, border: `1px solid ${C.border}`, borderRadius: '8px', padding: '4px', zIndex: 99, minWidth: '185px', boxShadow: '0 8px 24px rgba(0,0,0,0.25)' }}>

                  <button disabled style={{ ...menuItemBase, color: C.textMuted, opacity: 0.45, cursor: 'not-allowed' }}>
                    <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><circle cx="18" cy="5" r="3"/><circle cx="6" cy="12" r="3"/><circle cx="18" cy="19" r="3"/><line x1="8.59" y1="13.51" x2="15.42" y2="17.49"/><line x1="15.41" y1="6.51" x2="8.59" y2="10.49"/></svg>
                    <span style={{ flex: 1 }}>Share Report</span>
                    <span style={{ fontSize: '0.57rem', fontWeight: '700', letterSpacing: '0.05em' }}>SOON</span>
                  </button>

                  <button
                    onClick={() => { setEmailOpen(true); setEmailInput(''); setEmailStatus(null); setMoreOpen(false) }}
                    style={{ ...menuItemBase, color: C.textSec }}
                    onMouseEnter={e => { e.currentTarget.style.background = C.borderAlt; e.currentTarget.style.color = C.text }}
                    onMouseLeave={e => { e.currentTarget.style.background = 'transparent'; e.currentTarget.style.color = C.textSec }}>
                    <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M4 4h16c1.1 0 2 .9 2 2v12c0 1.1-.9 2-2 2H4c-1.1 0-2-.9-2-2V6c0-1.1.9-2 2-2z"/><polyline points="22,6 12,13 2,6"/></svg>
                    Email Report
                  </button>

                  <button disabled style={{ ...menuItemBase, color: C.textMuted, opacity: 0.45, cursor: 'not-allowed' }}>
                    <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 16 14"/></svg>
                    <span style={{ flex: 1 }}>Schedule Delivery</span>
                    <span style={{ fontSize: '0.57rem', fontWeight: '700', letterSpacing: '0.05em' }}>SOON</span>
                  </button>

                  <div style={{ height: '1px', background: C.border, margin: '4px 6px' }} />

                  <button disabled style={{ ...menuItemBase, color: C.textMuted, opacity: 0.45, cursor: 'not-allowed' }}>
                    <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><rect x="9" y="9" width="13" height="13" rx="2" ry="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg>
                    <span style={{ flex: 1 }}>Duplicate</span>
                    <span style={{ fontSize: '0.57rem', fontWeight: '700', letterSpacing: '0.05em' }}>SOON</span>
                  </button>

                  <button disabled style={{ ...menuItemBase, color: C.textMuted, opacity: 0.45, cursor: 'not-allowed' }}>
                    <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><polyline points="3 6 5 6 21 6"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a1 1 0 0 1 1-1h4a1 1 0 0 1 1 1v2"/></svg>
                    <span style={{ flex: 1 }}>Delete</span>
                    <span style={{ fontSize: '0.57rem', fontWeight: '700', letterSpacing: '0.05em' }}>SOON</span>
                  </button>

                </div>
              )}
            </div>

          </div>
        </div>

      </div>

      {/* ── TAB CONTENT ───────────────────────────────────────────────────── */}
      <div style={{ flex: 1, padding: '0 32px 40px 20px', boxSizing: 'border-box' }}>

        {/* ── OVERVIEW TAB ── Executive Brief layout ───────────────────────── */}
        {activeTab === 'overview' && (
          <div>

            {/* 1. Business KPIs — executive-only, no dataset metadata */}
            <div style={{ marginBottom: '8px' }}>
              {g.kpi.length > 0 ? (
                <ExecutiveKpiGrid sections={g.kpi} C={C} />
              ) : (
                <div style={{ background: C.surface, border: `1px solid ${C.border}`, borderRadius: '10px', padding: '20px 24px' }}>
                  <div style={{ fontSize: '0.82rem', color: C.textMuted, marginBottom: '4px', fontWeight: '500' }}>
                    No business KPIs detected
                  </div>
                  <div style={{ fontSize: '0.75rem', color: C.textMuted, opacity: 0.75, lineHeight: 1.5 }}>
                    Business KPIs are generated when revenue, margin, customer, or product columns are present in the dataset.
                  </div>
                </div>
              )}
            </div>

            {/* 2. Top 3 ranked charts — directly under KPI row */}
            <div style={{ marginBottom: '32px' }}>
              {overviewCharts.length > 0 ? (
                <>
                  <div style={{ display: 'grid', gridTemplateColumns: `repeat(${Math.min(overviewCharts.length, 3)}, 1fr)`, gap: '10px' }}>
                    {overviewCharts.map((sec, i) => (
                      <div key={`chart-prev-${i}`} style={{ height: '100%' }}>
                        <SectionRenderer section={sec} C={C} />
                      </div>
                    ))}
                  </div>
                  {g.chart.length > 3 && (
                    <button
                      onClick={() => setActiveTab('charts')}
                      style={{ marginTop: '10px', background: 'none', border: 'none', color: C.accent, cursor: 'pointer', fontSize: '0.74rem', fontFamily: FONT, fontWeight: '500', padding: 0, display: 'flex', alignItems: 'center', gap: '4px' }}>
                      View all {g.chart.length} charts
                      <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round"><line x1="5" y1="12" x2="19" y2="12"/><polyline points="12 5 19 12 12 19"/></svg>
                    </button>
                  )}
                </>
              ) : (
                <div style={{ fontSize: '0.72rem', color: C.textMuted }}>No chart recommendations available.</div>
              )}
            </div>

            {/* 3a. Prose-first: render summary before Intelligence for executive_brief / kpi_scorecard */}
            {proseFirst && g.execSummary.length > 0 && (
              <div style={{ marginBottom: '32px' }}>
                {sl('Executive Summary')}
                <ExecutiveSummaryProse sections={g.execSummary} C={C} />
              </div>
            )}

            {/* 3b. Key Decisions / Risks / Opportunities — from exec summary structured fields */}
            {g.execSummary.length > 0 && (
              <div style={{ marginBottom: '32px' }}>
                {sl('Intelligence')}
                <ExecutiveIntelligencePanel sections={g.execSummary} C={C} />
              </div>
            )}

            {/* 3. AI Analysis — insight sections rendered via SectionRenderer */}
            {g.insight.length > 0 && (
              <div style={{ marginBottom: '32px' }}>
                {sl('AI Analysis')}
                <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(280px, 1fr))', gap: '16px', alignItems: 'start' }}>
                  {g.insight.map((sec, i) => (
                    <div key={`insight-${i}`} style={{ background: C.surface, border: `1px solid ${C.border}`, borderRadius: '10px', padding: '20px 24px' }}>
                      <SectionRenderer section={sec} C={C} />
                    </div>
                  ))}
                </div>
              </div>
            )}

            {/* 4. Recommendations — priority + business impact + confidence + owner */}
            {g.recommendation.length > 0 && (
              <div style={{ marginBottom: '32px' }}>
                {sl('Recommendations')}
                <ExecutiveRecommendationList sections={g.recommendation} C={C} />
              </div>
            )}

            {/* 6. Executive Summary prose — skipped when proseFirst (already rendered above) */}
            {!proseFirst && g.execSummary.length > 0 && (
              <div style={{ marginBottom: '24px' }}>
                {sl('Executive Summary')}
                <ExecutiveSummaryProse sections={g.execSummary} C={C} />
              </div>
            )}

            {/* Empty state — only when truly nothing is in the report */}
            {g.kpi.length === 0 && g.execSummary.length === 0 && g.insight.length === 0 && g.recommendation.length === 0 && g.chart.length === 0 && (
              <TabEmpty message="No overview content in this report." C={C} />
            )}

          </div>
        )}

        {/* ── CHARTS TAB ─────────────────────────────────────────────────────── */}
        {activeTab === 'charts' && (
          g.chart.length > 0 ? (
            <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(460px, 1fr))', gap: '20px' }}>
              {g.chart.map((sec, i) => (
                <div key={i} style={{ background: C.surface, border: `1px solid ${C.border}`, borderRadius: '10px', padding: '20px 24px' }}>
                  {(sec.heading || sec.title) && (
                    <div style={{ fontSize: '0.83rem', fontWeight: '700', color: C.text, marginBottom: '8px' }}>
                      {sec.heading || sec.title}
                    </div>
                  )}
                  {sec.explanation && (
                    <div style={{ marginBottom: '14px' }}>
                      <span style={{ fontSize: '0.62rem', fontWeight: '700', color: C.textMuted, textTransform: 'uppercase', letterSpacing: '0.07em', marginRight: '6px' }}>
                        Executive Takeaway
                      </span>
                      <span style={{ fontSize: '0.75rem', color: C.textSec, lineHeight: 1.55 }}>
                        {sec.explanation}
                      </span>
                    </div>
                  )}
                  <SectionRenderer section={sec} C={C} />
                </div>
              ))}
            </div>
          ) : (
            <TabEmpty message="No charts in this report." C={C} />
          )
        )}

        {/* ── DATA TAB ───────────────────────────────────────────────────────── */}
        {activeTab === 'data' && (
          <div style={{ maxWidth: '1040px' }}>

            {/* Dataset Profile — metadata KPIs (Total Records, Total Features, etc.) */}
            {g.metaKpi.length > 0 && (
              <div style={{ marginBottom: '28px' }}>
                <div style={{ fontSize: '0.68rem', fontWeight: '700', color: C.textSec, textTransform: 'uppercase', letterSpacing: '0.09em', marginBottom: '12px' }}>
                  Dataset Profile
                </div>
                <KpiGrid sections={g.metaKpi} C={C} />
              </div>
            )}

            {/* Remaining data sections */}
            {g.data.length > 0 ? g.data.map((sec, i) => (
              <SectionBlock key={i} section={sec} C={C} SectionRenderer={SectionRenderer}
                label={sec.heading || SEC_LABEL[sec.type || 'text']} />
            )) : (
              g.metaKpi.length === 0 && (
                <TabEmpty message="No additional data in this report." C={C} />
              )
            )}

          </div>
        )}

        {/* ── ASK AI TAB ─────────────────────────────────────────────────────── */}
        {activeTab === 'ask' && (
          <div style={{ maxWidth: '700px' }}>
            <div style={{ marginBottom: '22px' }}>
              <div style={{ display: 'flex', alignItems: 'center', gap: '9px', marginBottom: '6px' }}>
                <span style={{ fontSize: '1.05rem', fontWeight: '700', color: C.text }}>Ask This Report</span>
                <span style={{ background: `${C.accent}18`, color: C.accent, border: `1px solid ${C.accent}30`, borderRadius: '4px', padding: '1px 6px', fontSize: '0.6rem', fontWeight: '700', letterSpacing: '0.05em' }}>AI</span>
              </div>
              <div style={{ fontSize: '0.79rem', color: C.textSec, lineHeight: 1.5 }}>
                Ask questions about the data, trends, and findings in this report.
              </div>
            </div>
            <div style={{ display: 'flex', gap: '8px', alignItems: 'center' }}>
              <input
                placeholder="Ask a question about this report…"
                value={askQ}
                onChange={e => { setAskQ(e.target.value); setAskResult(null) }}
                onKeyDown={e => { if (e.key === 'Enter') handleAsk() }}
                style={{ flex: 1, background: C.bg, border: `1px solid ${C.border}`, borderRadius: '8px', padding: '9px 13px', fontSize: '0.8rem', color: C.text, fontFamily: FONT, outline: 'none' }}
              />
              <button onClick={handleAsk} disabled={askLoading || !askQ.trim()}
                style={{ flexShrink: 0, background: askLoading || !askQ.trim() ? C.bg : C.accent, border: `1px solid ${askLoading || !askQ.trim() ? C.border : C.accent}`, borderRadius: '8px', padding: '9px 18px', fontSize: '0.78rem', color: askLoading || !askQ.trim() ? C.textSec : '#fff', cursor: askLoading || !askQ.trim() ? 'not-allowed' : 'pointer', fontFamily: FONT, fontWeight: '600', opacity: !askQ.trim() ? 0.5 : 1, transition: 'background 0.12s, color 0.12s, border-color 0.12s' }}>
                {askLoading ? 'Thinking…' : 'Ask'}
              </button>
            </div>
            {askResult && (
              <div style={{ marginTop: '14px', background: `${C.accent}08`, border: `1px solid ${C.accent}28`, borderRadius: '10px', padding: '16px 18px' }}>
                <div style={{ fontSize: '0.82rem', color: C.text, lineHeight: 1.65, marginBottom: '10px' }}>{askResult.answer}</div>
                {(askResult.cited_sections_used || []).length > 0 && (
                  <div style={{ display: 'flex', flexWrap: 'wrap', gap: '5px', marginBottom: '8px' }}>
                    {askResult.cited_sections_used.map((s, i) => (
                      <span key={i} style={{ background: C.borderAlt, color: C.textMuted, borderRadius: '4px', padding: '1px 7px', fontSize: '0.62rem', fontWeight: '500' }}>{s}</span>
                    ))}
                  </div>
                )}
                <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
                  {askResult.confidence && (
                    <span style={{ fontSize: '0.63rem', color: askResult.confidence === 'high' ? C.success : askResult.confidence === 'medium' ? C.warn : C.textMuted, fontWeight: '700', textTransform: 'uppercase', letterSpacing: '0.06em' }}>
                      {askResult.confidence} confidence
                    </span>
                  )}
                  {askResult.fallback_used && (
                    <span style={{ fontSize: '0.63rem', color: C.textMuted }}>· deterministic fallback</span>
                  )}
                </div>
              </div>
            )}
          </div>
        )}

      </div>

      {/* ── EMAIL MODAL ───────────────────────────────────────────────────────── */}
      {emailOpen && (
        <div
          style={{ position: 'fixed', inset: 0, zIndex: 200, display: 'flex', alignItems: 'center', justifyContent: 'center', background: 'rgba(0,0,0,0.5)', backdropFilter: 'blur(3px)' }}
          onClick={e => { if (e.target === e.currentTarget) { setEmailOpen(false); setEmailInput(''); setEmailStatus(null) } }}>
          <div style={{ background: C.surface, border: `1px solid ${C.border}`, borderRadius: '12px', padding: '24px', width: '100%', maxWidth: '420px', margin: '0 20px', boxShadow: '0 24px 56px rgba(0,0,0,0.45)' }}>

            <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: '18px' }}>
              <div style={{ display: 'flex', alignItems: 'center', gap: '9px' }}>
                <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke={C.accent} strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M4 4h16c1.1 0 2 .9 2 2v12c0 1.1-.9 2-2 2H4c-1.1 0-2-.9-2-2V6c0-1.1.9-2 2-2z"/><polyline points="22,6 12,13 2,6"/></svg>
                <span style={{ fontSize: '0.95rem', fontWeight: '700', color: C.text }}>Email Report</span>
              </div>
              <button
                onClick={() => { setEmailOpen(false); setEmailInput(''); setEmailStatus(null) }}
                style={{ display: 'flex', alignItems: 'center', justifyContent: 'center', background: 'none', border: 'none', color: C.textMuted, cursor: 'pointer', padding: '4px', borderRadius: '5px' }}
                onMouseEnter={e => { e.currentTarget.style.color = C.text; e.currentTarget.style.background = C.borderAlt }}
                onMouseLeave={e => { e.currentTarget.style.color = C.textMuted; e.currentTarget.style.background = 'none' }}>
                <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>
              </button>
            </div>

            {emailStatus?.ok ? (
              <div style={{ textAlign: 'center', padding: '8px 0 4px' }}>
                <div style={{ width: '44px', height: '44px', borderRadius: '50%', background: `${C.success}18`, border: `1.5px solid ${C.success}40`, display: 'flex', alignItems: 'center', justifyContent: 'center', margin: '0 auto 14px' }}>
                  <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke={C.success} strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round"><polyline points="20 6 9 17 4 12"/></svg>
                </div>
                <div style={{ fontSize: '0.92rem', fontWeight: '700', color: C.text, marginBottom: '5px' }}>Report sent</div>
                <div style={{ fontSize: '0.77rem', color: C.textSec }}>Sent to {emailInput}</div>
                <button
                  onClick={() => { setEmailOpen(false); setEmailInput(''); setEmailStatus(null) }}
                  style={{ marginTop: '20px', background: C.accent, border: 'none', borderRadius: '8px', padding: '8px 28px', fontSize: '0.8rem', color: '#fff', cursor: 'pointer', fontFamily: FONT, fontWeight: '600' }}>
                  Done
                </button>
              </div>
            ) : (
              <>
                <div style={{ fontSize: '0.78rem', color: C.textSec, lineHeight: 1.5, marginBottom: '14px' }}>
                  Send a copy of this report to a recipient by email.
                </div>
                <input
                  type="email"
                  placeholder="Recipient email address"
                  value={emailInput}
                  autoFocus
                  onChange={e => { setEmailInput(e.target.value); setEmailStatus(null) }}
                  onKeyDown={e => { if (e.key === 'Enter') handleEmail() }}
                  style={{ width: '100%', boxSizing: 'border-box', background: C.bg, border: `1px solid ${emailStatus?.ok === false ? C.danger : C.border}`, borderRadius: '8px', padding: '9px 12px', fontSize: '0.8rem', color: C.text, fontFamily: FONT, outline: 'none' }}
                />
                {emailStatus?.ok === false && (
                  <div style={{ marginTop: '7px', fontSize: '0.72rem', color: C.danger, display: 'flex', alignItems: 'center', gap: '5px' }}>
                    <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round"><circle cx="12" cy="12" r="10"/><line x1="12" y1="8" x2="12" y2="12"/><line x1="12" y1="16" x2="12.01" y2="16"/></svg>
                    {emailStatus.msg}
                  </div>
                )}
                <div style={{ display: 'flex', gap: '8px', justifyContent: 'flex-end', marginTop: '18px' }}>
                  <button
                    onClick={() => { setEmailOpen(false); setEmailInput(''); setEmailStatus(null) }}
                    style={{ background: 'transparent', border: `1px solid ${C.border}`, borderRadius: '8px', padding: '7px 16px', fontSize: '0.78rem', color: C.textSec, cursor: 'pointer', fontFamily: FONT, fontWeight: '500' }}
                    onMouseEnter={e => { e.currentTarget.style.borderColor = C.textSec }}
                    onMouseLeave={e => { e.currentTarget.style.borderColor = C.border }}>
                    Cancel
                  </button>
                  <button
                    onClick={handleEmail}
                    disabled={emailSending || !emailInput.trim()}
                    style={{ background: emailSending || !emailInput.trim() ? C.bg : C.accent, border: `1px solid ${emailSending || !emailInput.trim() ? C.border : C.accent}`, borderRadius: '8px', padding: '7px 20px', fontSize: '0.78rem', color: emailSending || !emailInput.trim() ? C.textSec : '#fff', cursor: emailSending || !emailInput.trim() ? 'not-allowed' : 'pointer', fontFamily: FONT, fontWeight: '600', opacity: !emailInput.trim() ? 0.5 : 1, transition: 'background 0.12s, border-color 0.12s, color 0.12s' }}>
                    {emailSending ? 'Sending…' : 'Send'}
                  </button>
                </div>
              </>
            )}

          </div>
        </div>
      )}

    </div>
  )
}
