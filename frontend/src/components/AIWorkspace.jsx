import { useState, useEffect, useRef } from 'react'
import { composeIntent, interpretTask, askReport, planEngineTool, saveEngineTool, listEngineTools, executeEngineTool, getEngineRun, submitEngineTool, approveEngineTool } from '../api/client'
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

// ── Report style display metadata (maps backend style name → badge label/colors) ──
const STYLE_LABELS = {
  executive_brief:    { label: 'Executive Brief',    color: '#6366f1', bg: 'rgba(99,102,241,0.12)', border: 'rgba(99,102,241,0.28)' },
  visual_dashboard:   { label: 'Visual Dashboard',   color: '#38bdf8', bg: 'rgba(56,189,248,0.12)',  border: 'rgba(56,189,248,0.28)'  },
  analyst_deep_dive:  null, // default — no badge shown
  table_heavy_report: { label: 'Table-Heavy',        color: '#f59e0b', bg: 'rgba(245,158,11,0.12)',  border: 'rgba(245,158,11,0.28)'  },
  operational_report: { label: 'Operational',        color: '#10b981', bg: 'rgba(16,185,129,0.12)',  border: 'rgba(16,185,129,0.28)'  },
  anomaly_report:     { label: 'Anomaly Report',     color: '#f87171', bg: 'rgba(248,113,113,0.12)', border: 'rgba(248,113,113,0.28)' },
  kpi_summary:        { label: 'KPI Summary',        color: '#fbbf24', bg: 'rgba(251,191,36,0.12)',  border: 'rgba(251,191,36,0.28)'  },
  monitoring_report:  { label: 'Monitoring',         color: '#60a5fa', bg: 'rgba(96,165,250,0.12)',  border: 'rgba(96,165,250,0.28)'  },
}

function ReportStyleBadge({ style }) {
  if (!style) return null
  const meta = STYLE_LABELS[style]
  if (!meta) return null
  return (
    <span style={{
      display: 'inline-flex', alignItems: 'center', gap: '5px',
      background: meta.bg, border: `1px solid ${meta.border}`,
      borderRadius: '20px', padding: '3px 10px',
      fontSize: '0.57rem', fontWeight: '700', color: meta.color,
      textTransform: 'uppercase', letterSpacing: '0.10em', fontFamily: FONT,
      flexShrink: 0,
    }}>
      <span style={{ width: '5px', height: '5px', borderRadius: '50%', background: meta.color, flexShrink: 0 }} />
      {meta.label}
    </span>
  )
}

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

// Routes tool/workflow creation intent to the engine planner instead of interpretTask.
// Matches explicit construction verbs or scheduling/recurrence signals paired with automation
// nouns — never fires for plain analytics ("analyze my dataset", "generate a report").
const TOOL_CREATION_RE = /\b(create|build|make|set ?up|develop|design)\b.{0,60}\b(tool|workflow|automation|agent|pipeline)\b|\breusable\b.{0,40}\b(workflow|tool|process)\b|\bmonitoring tool\b|\balert workflow\b|\b(schedule[d]?|recurring|automated)\b.{0,40}\b(report|workflow|automation|monitoring|alert)\b|\b(weekly|daily|monthly)\b.{0,40}\b(report|workflow|automation|monitoring)\b/i

function isToolCreationIntent(input) {
  return TOOL_CREATION_RE.test(input || '')
}

const SCHEDULE_TYPE_MAP = [
  { re: /\b(weekly|week)\b/i,          type: 'weekly'    },
  { re: /\b(daily|day|every ?day)\b/i, type: 'daily'     },
  { re: /\b(monthly|month)\b/i,        type: 'monthly'   },
  { re: /\b(recurring|recur)\b/i,      type: 'recurring' },
  { re: /\b(automat(ed?|ic))\b/i,      type: 'automated' },
]

function extractScheduleType(input) {
  for (const { re, type } of SCHEDULE_TYPE_MAP) {
    if (re.test(input || '')) return type
  }
  return null
}

function extractIntel(result) {
  if (!result) return null
  const report = result.dataset_report
  const aiMeta = result._ai_meta ?? null
  if (!report) {
    return {
      kind: 'basic', status: result.status, taskType: result.task_type, aiMeta,
      emailDelivery:     result.email_delivery,
      notificationSent:  result.notification_sent  ?? null,
      notificationId:    result.notification_id    ?? null,
      scheduleRequested: result.schedule_requested ?? null,
      scheduleCreated:   result.schedule_created   ?? null,
      scheduleId:        result.schedule_id        ?? null,
    }
  }
  const sections = report.sections || []

  // ── Section extraction ──────────────────────────────────────────────────────
  const execSec    = sections.find(s => s.type === 'executive_summary')

  // business_kpis: semantic/intent-aware KPIs (Total Revenue, Customer Count,
  //   Top Region, Gross Margin, etc.) — driven by the semantic column classifier.
  // kpi: structural/schema-level KPIs (Total Records, Data Completeness,
  //   Numeric Columns, etc.) — identical shape for every dataset.
  // Prefer business_kpis when present and non-empty; fall back to kpi.
  const bizKpiSec  = sections.find(s => s.type === 'business_kpis')
  const kpiSec     = sections.find(s => s.type === 'kpi')

  const recSec     = sections.find(s => s.type === 'recommendation')
  const driftSec   = sections.find(s => s.type === 'drift_detection')
  const chartSecs  = sections.filter(s => s.type === 'chart')

  // Newly extracted sections — available in intel for rendering in future steps.
  const anomalySec         = sections.find(s => s.type === 'anomaly')        ?? null
  const trendSec           = sections.find(s => s.type === 'trend')           ?? null
  const forecastSec        = sections.find(s => s.type === 'forecast')        ?? null
  const insightPrioritySec = sections.find(s => s.type === 'insight_priority') ?? null
  const segmentationSec    = sections.find(s => s.type === 'segmentation_insights') ?? null
  const drilldownSec       = sections.find(s => s.type === 'drilldown_table') ?? null
  const textSec            = sections.find(s => s.type === 'text')            ?? null

  // ── Derived values ──────────────────────────────────────────────────────────
  // Use business KPIs if the section exists and contains at least one card;
  // fall back to structural KPIs so the KPI tab is never empty.
  const kpis       = (bizKpiSec?.kpis?.length ? bizKpiSec.kpis : null) ?? kpiSec?.kpis ?? []
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
    reportPlan:      report.report_plan   ?? null,
    execSummary:     execSec?.summary ?? null,
    keyTakeaways:    takes,
    risks,
    opportunities:   opps,
    kpis,
    recommendations: recs,
    drifts,
    watchlist:       drifts.slice(0, 3),
    chartSecs,
    // Newly available sections (rendered in a future step)
    anomalySec,
    trendSec,
    forecastSec,
    insightPrioritySec,
    segmentationSec,
    drilldownSec,
    textSec,
    topInsight:      takes[0] ?? recs[0]?.reason ?? null,
    highestRisk:     risks[0] ?? recs.find(r => r.priority === 'high')?.reason ?? null,
    topAction:       recs.find(r => r.priority === 'high')?.title ?? recs[0]?.title ?? opps[0] ?? null,
    topOpportunity:  opps[1] ?? opps[0] ?? null,
    emailDelivery:     result.email_delivery,
    notificationSent:  result.notification_sent  ?? null,
    notificationId:    result.notification_id    ?? null,
    scheduleRequested: result.schedule_requested ?? null,
    scheduleCreated:   result.schedule_created   ?? null,
    scheduleId:        result.schedule_id        ?? null,
    started_at:        result.started_at,
    finished_at:       result.finished_at,
  }
}

function normalizeExecutionResult(raw) {
  if (!raw) return raw
  if (raw.dataset_report) return raw
  if (raw.task_type === 'multi_step' || raw.step_results) {
    const reportStep = (raw.step_results ?? []).find(s => s.result?.dataset_report)
    if (reportStep) {
      const notifStep = (raw.step_results ?? []).find(
        s => s.result?.notification_sent != null || s.result?.notification_id != null
      )
      return {
        ...raw,
        dataset_report:    reportStep.result.dataset_report,
        report_id:         reportStep.result.report_id    ?? raw.report_id,
        notification_sent: notifStep?.result?.notification_sent ?? raw.notification_sent ?? null,
        notification_id:   notifStep?.result?.notification_id  ?? raw.notification_id  ?? null,
      }
    }
  }
  return raw
}

function buildStepsFromResult(result, wsInput, enginePlan) {
  const steps = []

  function actionTypeToLabel(at) {
    return (at || '').replace(/_/g, ' ').replace(/\b\w/g, c => c.toUpperCase())
  }

  function toStatus(raw) {
    if (raw === 'completed') return 'completed'
    if (raw === 'failed')    return 'failed'
    if (raw === 'skipped')   return 'skipped'
    return 'pending'
  }

  const stepResults = result?.step_results ?? []

  // ── A: Engine path ──────────────────────────────────────────────────────────
  // enginePlan.graph.nodes are ActionNodes from contracts.py; step_results use node_id.
  const graphNodes = (enginePlan?.graph?.nodes ?? []).filter(n => n != null)
  if (graphNodes.length > 0) {
    const byNodeId = {}
    for (const sr of stepResults) {
      if (sr?.node_id) byNodeId[sr.node_id] = sr
    }
    for (const node of graphNodes) {
      const sr = byNodeId[node.id] ?? null
      steps.push({
        id:          node.id,
        label:       node.label || actionTypeToLabel(node.action_type),
        status:      sr ? toStatus(sr.status) : 'pending',
        action_type: node.action_type,
        human_label: sr?.duration_ms != null ? `${sr.duration_ms}ms` : null,
      })
    }
  }

  // ── B: Interpret multi-step path ────────────────────────────────────────────
  // interpretTask step_results use { tool, operation, success, status } — not node_id.
  else if (stepResults.length > 0 && stepResults[0]?.tool !== undefined) {
    for (const sr of stepResults) {
      const id = `${sr.tool ?? 'step'}_${sr.operation ?? ''}`.replace(/\s+/g, '_')
      steps.push({
        id,
        label:       sr.operation ? actionTypeToLabel(sr.operation) : (sr.tool ?? 'Step'),
        status:      sr.success === false ? 'failed' : toStatus(sr.status),
        action_type: sr.operation || sr.tool || '',
        human_label: sr.duration_ms != null ? `${sr.duration_ms}ms` : null,
      })
    }
  }

  // ── B flat: Interpret single-step path ──────────────────────────────────────
  // No step_results array — derive from top-level result fields only.
  else if (result) {
    const ok     = result.status === 'success' || result.status === 'completed'
    const failed = result.status === 'failed'
    const mainStatus = ok ? 'completed' : failed ? 'failed' : 'pending'

    if (result.dataset_report || result.report_id ||
        (result.task_type ?? '').includes('dataset_report')) {
      steps.push({
        id:          'generate_report',
        label:       'Generate Dataset Report',
        status:      mainStatus,
        action_type: 'generate_dataset_report',
        human_label: null,
      })
    }

    if (result.email_delivery) {
      steps.push({
        id:          'send_email',
        label:       'Send Email',
        status:      result.email_delivery.sent ? 'completed' : 'failed',
        action_type: 'send_email',
        human_label: result.email_delivery.to ?? null,
      })
    }
  }

  // ── C: Schedule step ────────────────────────────────────────────────────────
  // Appended only when user intent includes scheduling or backend confirms schedule created.
  const schedType      = extractScheduleType(wsInput)
  const hasSchedIntent = !!schedType || enginePlan?.schedule?.enabled === true
  if (hasSchedIntent) {
    const schedCreated = result?.schedule_created
    const schedStatus  = (schedCreated === true || result?.schedule_id != null) ? 'completed'
                       : schedCreated === false                                  ? 'failed'
                       : 'pending'
    const typeLabel    = schedType
      ? { weekly: 'Weekly', daily: 'Daily', monthly: 'Monthly', recurring: 'Recurring', automated: 'Automated' }[schedType]
      : null
    steps.push({
      id:          'schedule_creation',
      label:       'Create Schedule',
      status:      schedStatus,
      action_type: 'schedule',
      human_label: enginePlan?.schedule?.human_label || typeLabel || 'Recurring',
    })
  }

  // ── D: Notification step ────────────────────────────────────────────────────
  // Appended last. Skipped if a notification node already appears from engine path (A).
  const notifKeyword    = /\b(notify|alert|notification)\b/i.test(wsInput ?? '')
  const notifInResult   = result?.notification_sent != null || result?.notification_id != null
  const notifInPlan     = graphNodes.some(n => /notif|send_notification/i.test(n.action_type ?? ''))
  const alreadyHasNotif = steps.some(s => /notif|send_notification/i.test(s.action_type ?? ''))

  if ((notifKeyword || notifInResult || notifInPlan) && !alreadyHasNotif) {
    const notifStatus = (result?.notification_sent === true || result?.notification_id != null) ? 'completed'
                      : result?.notification_sent === false                                       ? 'failed'
                      : 'pending'
    steps.push({
      id:          'send_notification',
      label:       'Send Notification',
      status:      notifStatus,
      action_type: 'send_notification',
      human_label: null,
    })
  }

  return steps
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
@keyframes ws-phase-active {
  0%,100% { box-shadow: 0 0 0 0 rgba(99,102,241,0.5); }
  50%     { box-shadow: 0 0 0 6px rgba(99,102,241,0);  }
}
@keyframes ws-progress-shimmer {
  0%   { background-position: 200% center; }
  100% { background-position: -200% center; }
}
@keyframes ws-reveal-up {
  from { opacity: 0; transform: translateY(18px); }
  to   { opacity: 1; transform: translateY(0); }
}
@keyframes ws-hero-ambient {
  0%,100% { opacity: 0.65; transform: scale(1);    }
  50%     { opacity: 1;   transform: scale(1.04); }
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
  box-shadow: 0 12px 32px rgba(99,102,241,0.14), 0 2px 8px rgba(0,0,0,0.10);
}
.ws-insight-card {
  transition: transform 0.18s ease, box-shadow 0.18s ease;
}
.ws-insight-card:hover { transform: translateY(-2px); box-shadow: 0 10px 28px rgba(0,0,0,0.12); }
.ws-chart-panel { transition: transform 0.20s ease, box-shadow 0.20s ease; }
.ws-chart-panel:hover { transform: translateY(-2px); box-shadow: 0 16px 48px rgba(0,0,0,0.14); }

.ws-qs-card { transition: transform 0.15s ease, box-shadow 0.15s ease, border-color 0.15s ease; cursor: pointer; }
.ws-qs-card:hover { transform: translateY(-2px); box-shadow: 0 6px 20px rgba(99,102,241,0.16); border-color: rgba(99,102,241,0.4) !important; }

.ws-upload-btn { transition: border-color 0.14s ease, color 0.14s ease, background 0.14s ease; }
.ws-upload-btn:hover { border-color: #6366f1 !important; color: #6366f1 !important; background: rgba(99,102,241,0.07) !important; }

.ws-suggest-row { transition: background 0.14s ease; cursor: default; }
.ws-suggest-row:hover { background: rgba(99,102,241,0.07) !important; }

.ws-copilot-q { transition: background 0.14s ease, border-color 0.14s ease, color 0.14s ease; }
.ws-copilot-q:hover { border-color: #6366f1 !important; color: #6366f1 !important; background: rgba(99,102,241,0.06) !important; }

.ws-action-btn { transition: opacity 0.14s ease, transform 0.14s ease, box-shadow 0.14s ease; }
.ws-action-btn:hover { opacity: 0.9; transform: translateY(-1px); box-shadow: 0 6px 20px rgba(99,102,241,0.35); }

.ws-ghost-btn { transition: border-color 0.14s ease, color 0.14s ease, background 0.14s ease; }
.ws-ghost-btn:hover { border-color: #6366f1 !important; color: #6366f1 !important; }
`

// ─── Execution phase definitions ─────────────────────────────────────────────

const EXEC_PHASES = [
  { label: 'Understanding request',     icon: 'star',     dur: 500  },
  { label: 'Selecting capabilities',    icon: 'layout',   dur: 650  },
  { label: 'Building workflow',         icon: 'database', dur: 750  },
  { label: 'Executing analysis',        icon: 'scan',     dur: 900  },
  { label: 'Generating report',         icon: 'bar',      dur: 850  },
  { label: 'Delivering results',        icon: 'done',     dur: 500  },
]

const SCHEDULE_PHASES = [
  { label: 'Request understood',  icon: 'star',     dur: 400 },
  { label: 'Dataset confirmed',   icon: 'database', dur: 500 },
  { label: 'Schedule created',    icon: 'layout',   dur: 700 },
  { label: 'Delivery configured', icon: 'done',     dur: 500 },
  { label: 'Automation saved',    icon: 'bar',      dur: 400 },
]

function ExecPhaseIcon({ type, color, size = 13 }) {
  const p = { width: size, height: size, viewBox: '0 0 24 24', fill: 'none', stroke: color, strokeWidth: 2, strokeLinecap: 'round', strokeLinejoin: 'round' }
  if (type === 'database') return <svg {...p}><ellipse cx="12" cy="5" rx="9" ry="3"/><path d="M21 12c0 1.66-4 3-9 3s-9-1.34-9-3"/><path d="M3 5v14c0 1.66 4 3 9 3s9-1.34 9-3V5"/></svg>
  if (type === 'layout')   return <svg {...p}><rect x="3" y="3" width="18" height="18" rx="2"/><line x1="3" y1="9" x2="21" y2="9"/><line x1="9" y1="21" x2="9" y2="9"/></svg>
  if (type === 'scan')     return <svg {...p}><path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"/><circle cx="12" cy="12" r="3"/></svg>
  if (type === 'shield')   return <svg {...p}><path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/></svg>
  if (type === 'bar')      return <svg {...p}><line x1="18" y1="20" x2="18" y2="10"/><line x1="12" y1="20" x2="12" y2="4"/><line x1="6" y1="20" x2="6" y2="14"/></svg>
  if (type === 'star')     return <svg {...p} fill={color}><path d="M12 2l2.4 7.4H22l-6.2 4.5 2.4 7.4L12 17l-6.2 4.3 2.4-7.4L2 9.4h7.6z"/></svg>
  if (type === 'chart')    return <svg {...p}><polyline points="22 12 18 12 15 21 9 3 6 12 2 12"/></svg>
  if (type === 'done')     return <svg {...p}><polyline points="20 6 9 17 4 12"/></svg>
  return null
}

function AIExecutionFlow({ C }) {
  const [activePhase, setActivePhase] = useState(0)

  useEffect(() => {
    let elapsed = 0
    const timers = EXEC_PHASES.map((phase, i) => {
      const t = setTimeout(() => setActivePhase(i), elapsed)
      elapsed += phase.dur
      return t
    })
    return () => timers.forEach(clearTimeout)
  }, [])

  const totalDur = EXEC_PHASES.reduce((s, p) => s + p.dur, 0)
  const elapsedDur = EXEC_PHASES.slice(0, activePhase + 1).reduce((s, p) => s + p.dur, 0)
  const progress = Math.min(95, Math.round((elapsedDur / totalDur) * 100))

  return (
    <div style={{
      background: C.surface, border: `1px solid ${C.border}`, borderRadius: '20px',
      overflow: 'hidden', boxShadow: '0 8px 48px rgba(0,0,0,0.14)', position: 'relative',
      animation: 'ws-fadein 0.35s ease',
    }}>
      {/* Ambient glow blobs */}
      <div style={{ position: 'absolute', top: '-80px', right: '-60px', width: '320px', height: '320px', background: 'radial-gradient(circle, rgba(99,102,241,0.11) 0%, transparent 65%)', pointerEvents: 'none', zIndex: 0 }} />
      <div style={{ position: 'absolute', bottom: '-50px', left: '-30px', width: '220px', height: '220px', background: 'radial-gradient(circle, rgba(59,130,246,0.07) 0%, transparent 65%)', pointerEvents: 'none', zIndex: 0 }} />

      <div style={{ position: 'relative', zIndex: 1 }}>
        {/* Header row */}
        <div style={{ padding: '28px 32px 22px', display: 'flex', alignItems: 'center', gap: '20px', borderBottom: `1px solid ${C.border}` }}>
          {/* Compact orbital */}
          <div style={{ width: '52px', height: '52px', position: 'relative', flexShrink: 0 }}>
            <div style={{ position: 'absolute', inset: 0, borderRadius: '50%', border: '1.5px solid rgba(99,102,241,0.28)', animation: 'ws-arc-cw 3.5s linear infinite' }}>
              <div style={{ position: 'absolute', top: 0, left: '50%', transform: 'translate(-50%,-50%)', width: '5px', height: '5px', borderRadius: '50%', background: '#6366f1', boxShadow: '0 0 8px #6366f1' }} />
            </div>
            <div style={{ position: 'absolute', inset: '11px', borderRadius: '50%', border: '1.5px solid rgba(59,130,246,0.28)', animation: 'ws-arc-ccw 2.4s linear infinite' }}>
              <div style={{ position: 'absolute', top: 0, left: '50%', transform: 'translate(-50%,-50%)', width: '4px', height: '4px', borderRadius: '50%', background: '#3b82f6', boxShadow: '0 0 6px #3b82f6' }} />
            </div>
            <div style={{ position: 'absolute', inset: 0, display: 'flex', alignItems: 'center', justifyContent: 'center' }}>
              <div style={{ width: '18px', height: '18px', borderRadius: '50%', background: 'radial-gradient(circle, #a5b4fc 0%, #6366f1 100%)', boxShadow: '0 0 14px rgba(99,102,241,0.55)', animation: 'ws-core-breathe 2s ease-in-out infinite' }} />
            </div>
          </div>

          <div style={{ flex: 1 }}>
            <div style={{ fontSize: '0.56rem', fontWeight: '800', color: '#6366f1', textTransform: 'uppercase', letterSpacing: '0.16em', marginBottom: '5px' }}>Analyzing</div>
            <div style={{ fontSize: '1.05rem', fontWeight: '700', color: C.text, letterSpacing: '-0.3px', marginBottom: '4px' }}>Preparing Your Report</div>
            <div style={{ fontSize: '0.74rem', color: C.textSec }}>Building your intelligence report…</div>
          </div>

          <div style={{ flexShrink: 0, textAlign: 'right' }}>
            <div style={{ fontSize: '2rem', fontWeight: '800', color: '#6366f1', lineHeight: 1, fontFamily: MONO, letterSpacing: '-1px' }}>{progress}%</div>
            <div style={{ fontSize: '0.6rem', color: C.textMuted, marginTop: '3px', textTransform: 'uppercase', letterSpacing: '0.08em' }}>estimated</div>
          </div>
        </div>

        {/* Progress bar */}
        <div style={{ height: '3px', background: C.border, overflow: 'hidden' }}>
          <div style={{
            height: '100%', width: `${progress}%`,
            background: 'linear-gradient(90deg, #4f46e5, #6366f1, #a5b4fc, #6366f1, #4f46e5)',
            backgroundSize: '300% 100%',
            animation: 'ws-progress-shimmer 2.2s linear infinite',
            transition: 'width 0.7s ease',
            borderRadius: '0 2px 2px 0',
          }} />
        </div>

        {/* Phase timeline — 2-column grid */}
        <div style={{ padding: '20px 32px 28px', display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '8px' }}>
          {EXEC_PHASES.map((phase, i) => {
            const isActive  = i === activePhase
            const isDone    = i < activePhase
            const isPending = i > activePhase
            return (
              <div key={i} style={{
                display: 'flex', alignItems: 'center', gap: '11px',
                padding: '11px 14px', borderRadius: '12px',
                background: isActive ? 'rgba(99,102,241,0.08)' : isDone ? 'rgba(16,185,129,0.05)' : 'transparent',
                border: `1px solid ${isActive ? 'rgba(99,102,241,0.25)' : isDone ? 'rgba(16,185,129,0.15)' : C.border}`,
                opacity: isPending ? 0.38 : 1,
                transition: 'opacity 0.4s ease, background 0.4s ease, border-color 0.4s ease',
              }}>
                {/* Status node */}
                <div style={{
                  width: '30px', height: '30px', borderRadius: '50%', flexShrink: 0,
                  background: isActive ? 'rgba(99,102,241,0.14)' : isDone ? 'rgba(16,185,129,0.12)' : C.bg,
                  border: `2px solid ${isActive ? '#6366f1' : isDone ? '#10b981' : C.borderAlt}`,
                  display: 'flex', alignItems: 'center', justifyContent: 'center',
                  animation: isActive ? 'ws-phase-active 1.6s ease-in-out infinite' : 'none',
                }}>
                  {isDone
                    ? <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="#10b981" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round"><polyline points="20 6 9 17 4 12"/></svg>
                    : isActive
                      ? <div style={{ width: '8px', height: '8px', borderRadius: '50%', background: '#6366f1', animation: 'ws-core-breathe 1s ease-in-out infinite' }} />
                      : <ExecPhaseIcon type={phase.icon} color={C.borderAlt} size={12} />
                  }
                </div>
                {/* Label */}
                <div style={{ flex: 1, minWidth: 0 }}>
                  <div style={{ fontSize: '0.73rem', fontWeight: isActive ? '700' : '500', color: isActive ? '#a5b4fc' : isDone ? '#10b981' : C.textMuted, lineHeight: 1.3 }}>{phase.label}</div>
                  {isActive  && <div style={{ fontSize: '0.59rem', color: '#6366f1', marginTop: '2px', opacity: 0.8 }}>processing…</div>}
                  {isDone    && <div style={{ fontSize: '0.59rem', color: '#10b981', marginTop: '2px', opacity: 0.7 }}>complete</div>}
                </div>
              </div>
            )
          })}
        </div>
      </div>
    </div>
  )
}

// ─── Execution Console ────────────────────────────────────────────────────────

function OutcomeEvent({ label, active, failed }) {
  const color = failed ? '#ef4444' : active ? '#10b981' : '#6b7280'
  return (
    <div style={{ display: 'flex', alignItems: 'center', gap: '7px' }}>
      <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke={color} strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
        {failed
          ? <><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></>
          : <polyline points="20 6 9 17 4 12"/>
        }
      </svg>
      <span style={{ fontSize: '0.70rem', fontWeight: '500', color: active || failed ? color : '#6b7280' }}>{label}</span>
    </div>
  )
}

const EC_STATUS = {
  pending:   { dotBg: 'transparent', dotBorder: '#6b7280', labelColor: '#6b7280', rowBg: 'transparent',              rowBorder: null },
  completed: { dotBg: 'rgba(16,185,129,0.12)', dotBorder: '#10b981', labelColor: '#10b981', rowBg: 'rgba(16,185,129,0.05)', rowBorder: 'rgba(16,185,129,0.18)' },
  failed:    { dotBg: 'rgba(239,68,68,0.12)',  dotBorder: '#ef4444', labelColor: '#ef4444', rowBg: 'rgba(239,68,68,0.05)',  rowBorder: 'rgba(239,68,68,0.18)'  },
  skipped:   { dotBg: 'rgba(245,158,11,0.1)',  dotBorder: '#f59e0b', labelColor: '#f59e0b', rowBg: 'rgba(245,158,11,0.04)', rowBorder: 'rgba(245,158,11,0.18)' },
}

function ECStatusIcon({ status }) {
  const s = { width: 11, height: 11, viewBox: '0 0 24 24', fill: 'none', strokeWidth: 2.5, strokeLinecap: 'round', strokeLinejoin: 'round' }
  if (status === 'completed') return <svg {...s} stroke="#10b981"><polyline points="20 6 9 17 4 12"/></svg>
  if (status === 'failed')    return <svg {...s} stroke="#ef4444"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>
  if (status === 'skipped')   return <svg {...s} stroke="#f59e0b"><line x1="5" y1="12" x2="19" y2="12"/><polyline points="12 5 19 12 12 19"/></svg>
  return <div style={{ width: 7, height: 7, borderRadius: '50%', background: '#6b7280' }} />
}

const EC_ACTION_LABELS = {
  generate_dataset_report: 'Generate Intelligence Report',
  generate_report:         'Generate Intelligence Report',
  send_notification:       'Send Notification',
  create_schedule:         'Create Schedule',
  save_workflow:           'Saving',
  create_tool:             'Create Tool',
  validate_workflow:       'Validate Request',
  build_workflow_graph:    'Prepare Analysis',
  send_email:              'Send Email',
}

const STATUS_DISPLAY_LABELS = {
  completed: 'Done',
  pending:   'Waiting',
  failed:    'Could Not Complete',
  skipped:   'Not Needed',
}

function ecDisplayLabel(step) {
  const at = (step.action_type ?? '').toLowerCase()
  if (at === 'schedule' || at === 'create_schedule') {
    const freq = (step.human_label ?? '').toLowerCase()
    if (freq === 'weekly')  return 'Create Weekly Schedule'
    if (freq === 'daily')   return 'Create Daily Schedule'
    if (freq === 'monthly') return 'Create Monthly Schedule'
    return 'Create Schedule'
  }
  return EC_ACTION_LABELS[at] ?? step.label
}

const EC_PHASE_COLORS = [
  { bg: 'rgba(99,102,241,0.15)', border: 'rgba(99,102,241,0.28)', fg: '#a5b4fc' },
  { bg: 'rgba(59,130,246,0.15)',  border: 'rgba(59,130,246,0.28)',  fg: '#60a5fa' },
  { bg: 'rgba(20,184,166,0.15)', border: 'rgba(20,184,166,0.28)', fg: '#2dd4bf' },
  { bg: 'rgba(16,185,129,0.15)', border: 'rgba(16,185,129,0.28)', fg: '#34d399' },
  { bg: 'rgba(99,102,241,0.15)', border: 'rgba(99,102,241,0.28)', fg: '#a5b4fc' },
  { bg: 'rgba(16,185,129,0.15)', border: 'rgba(16,185,129,0.28)', fg: '#34d399' },
]

function ActivityTimeline({ result, wsInput, datasetName, C }) {
  const [activePhase, setActivePhase] = useState(0)

  useEffect(() => {
    if (result) return
    setActivePhase(0)
    let elapsed = 0
    const timers = EXEC_PHASES.map((phase, i) => {
      const t = setTimeout(() => setActivePhase(i), elapsed)
      elapsed += phase.dur
      return t
    })
    return () => timers.forEach(clearTimeout)
  }, [result])

  const isComplete = !!result
  const reportOut  = isComplete && !!(result.report_id || result.dataset_report)
  const notifOut   = isComplete && !!(result.notification_sent || result.notification_id)

  const loadingEvents = [
    { label: 'Request Received',   minPhase: -1 },
    { label: 'Intent Parsed',      minPhase: 0  },
    ...(datasetName ? [{ label: 'Dataset Loaded',    minPhase: 0 }] : []),
    { label: 'Validation Passed',  minPhase: 1  },
    { label: 'Analysis Started',   minPhase: 2  },
    { label: 'KPI Generation',     minPhase: 3  },
    { label: 'Report Generated',   minPhase: 4  },
    { label: 'Delivering Results', minPhase: 5  },
  ]

  const completedEvents = [
    { label: 'Request Received'    },
    { label: 'Intent Parsed'       },
    ...(datasetName ? [{ label: 'Dataset Loaded' }] : []),
    { label: 'Validation Passed'   },
    { label: 'Analysis Started'    },
    { label: 'KPI Generation'      },
    ...(reportOut ? [{ label: 'Report Generated' }] : []),
    ...(notifOut  ? [{ label: 'Notification Sent' }] : []),
    { label: 'Execution Completed' },
  ]

  const events = isComplete ? completedEvents : loadingEvents

  return (
    <div style={{ background: C.surface, border: `1px solid ${C.border}`, borderRadius: '16px', overflow: 'hidden', display: 'flex', flexDirection: 'column' }}>
      <div style={{ padding: '14px 16px 12px', borderBottom: `1px solid ${C.border}` }}>
        <div style={{ fontSize: '0.50rem', fontWeight: '800', color: '#6366f1', textTransform: 'uppercase', letterSpacing: '0.16em', marginBottom: '4px' }}>Activity Timeline</div>
        <div style={{ fontSize: '0.72rem', fontWeight: '600', color: C.text }}>
          {isComplete ? 'Execution complete' : 'Execution in progress…'}
        </div>
      </div>
      <div style={{ padding: '14px 16px', overflowY: 'auto' }}>
        {events.map((ev, i) => {
          const isLast = i === events.length - 1
          const done   = isComplete ? true : activePhase > ev.minPhase
          const active = !isComplete && activePhase === ev.minPhase
          return (
            <div key={i} style={{ display: 'flex', gap: '10px', animation: `ws-fadeup 0.25s ease both`, animationDelay: `${i * 0.05}s` }}>
              <div style={{ display: 'flex', flexDirection: 'column', alignItems: 'center', width: '16px', flexShrink: 0 }}>
                <div style={{ width: '12px', height: '12px', borderRadius: '50%', flexShrink: 0, marginTop: '3px', background: done ? '#10b981' : active ? '#6366f1' : 'transparent', border: `2px solid ${done ? '#10b981' : active ? '#6366f1' : C.borderAlt}`, animation: active ? 'ws-core-breathe 1.2s ease infinite' : 'none', transition: 'background 0.3s ease, border-color 0.3s ease', display: 'flex', alignItems: 'center', justifyContent: 'center' }}>
                  {done && <svg width="6" height="6" viewBox="0 0 24 24" fill="none" stroke="#fff" strokeWidth="3" strokeLinecap="round" strokeLinejoin="round"><polyline points="20 6 9 17 4 12"/></svg>}
                </div>
                {!isLast && <div style={{ width: '2px', flex: 1, minHeight: '16px', background: done ? 'rgba(16,185,129,0.30)' : 'rgba(107,114,128,0.15)', borderRadius: '1px', margin: '3px 0' }} />}
              </div>
              <div style={{ flex: 1, paddingBottom: isLast ? '0' : '12px' }}>
                <div style={{ fontSize: '0.71rem', fontWeight: done ? '600' : active ? '700' : '400', color: done ? C.text : active ? '#a5b4fc' : C.textMuted, lineHeight: 1.4, transition: 'color 0.3s ease' }}>
                  {ev.label}
                </div>
                {active && (
                  <div style={{ fontSize: '0.57rem', color: '#6366f1', marginTop: '2px', display: 'flex', alignItems: 'center', gap: '4px' }}>
                    <div style={{ width: '4px', height: '4px', borderRadius: '50%', background: '#6366f1', animation: 'ws-core-breathe 1s ease infinite' }} />
                    In progress
                  </div>
                )}
              </div>
            </div>
          )
        })}
      </div>
    </div>
  )
}

function ExecutionConsole({ result, wsInput, enginePlan, datasetName, activeDs, intel, C, onOpenReport, setActiveNav, onBack, onRunAgain, execDurationMs, user }) {
  const [activePhase, setActivePhase] = useState(0)

  useEffect(() => {
    if (result) return
    setActivePhase(0)
    let elapsed = 0
    const timers = EXEC_PHASES.map((phase, i) => {
      const t = setTimeout(() => setActivePhase(i), elapsed)
      elapsed += phase.dur
      return t
    })
    return () => timers.forEach(clearTimeout)
  }, [result])

  const isComplete = !!result
  const isSuccess  = !result || result.status !== 'failed'
  const sColor     = isSuccess ? '#10b981' : '#ef4444'

  const totalPhaseDur   = EXEC_PHASES.reduce((s, p) => s + p.dur, 0)
  const elapsedPhaseDur = EXEC_PHASES.slice(0, activePhase + 1).reduce((s, p) => s + p.dur, 0)
  const phaseProgress   = !isComplete ? Math.min(95, Math.round((elapsedPhaseDur / totalPhaseDur) * 100)) : 100

  const taskType = result?.task_type ?? null
  const execType = taskType
    ? taskType.replace(/_/g, ' ').replace(/\b\w/g, c => c.toUpperCase())
    : enginePlan?.name ? 'Engine Workflow' : 'Analysis'

  const backendDurMs = result?.started_at && result?.finished_at
    ? new Date(result.finished_at) - new Date(result.started_at) : null
  const durMs = execDurationMs ?? backendDurMs
  const fmtTotal = (() => {
    if (durMs == null || durMs <= 0) return null
    const s = durMs / 1000
    if (s < 60) return `${s.toFixed(1)}s`
    const totalM = Math.floor(s / 60)
    if (totalM < 60) return `${totalM}m ${String(Math.floor(s % 60)).padStart(2, '0')}s`
    return `${Math.floor(totalM / 60)}h ${String(totalM % 60).padStart(2, '0')}m`
  })()

  const reportGenerated   = !!(result?.report_id || result?.dataset_report)
  const scheduleCreated   = result?.schedule_created === true || result?.schedule_id != null
  const emailSent         = !!(result?.email_delivery?.sent)
  const notifSent         = result?.notification_sent === true || result?.notification_id != null
  const emailConfigured   = result?.email_delivery_configured === true && !emailSent
  const notifConfigured   = result?.notification_configured === true && !notifSent
  const title           = wsInput.trim() || 'Analysis in progress'

  // Use schedule-focused phases when the result is a future-schedule-only configuration
  // (no report generated, schedule created). Fall back to the standard execution journey.
  const isScheduleOnly  = isComplete && scheduleCreated && !reportGenerated
  const stepsToRender   = isScheduleOnly ? SCHEDULE_PHASES : EXEC_PHASES
  const backendSteps    = isComplete ? (buildStepsFromResult(result, wsInput, enginePlan) ?? []) : []
  const hasBackendStepDurations = backendSteps.some(s => /^(\d+)ms$/.test(s?.human_label ?? ''))

  return (
    <div style={{ background: C.surface, border: `1px solid ${isComplete ? (isSuccess ? 'rgba(16,185,129,0.18)' : 'rgba(239,68,68,0.18)') : C.border}`, borderRadius: '20px', overflow: 'hidden', boxShadow: '0 8px 48px rgba(0,0,0,0.14)', animation: 'ws-fadein 0.35s ease', transition: 'border-color 0.5s ease' }}>

      {/* ── Progress bar ── */}
      <div style={{ height: '3px', background: isComplete ? 'transparent' : C.border, position: 'relative' }}>
        <div style={{
          position: 'absolute', inset: 0,
          width: `${phaseProgress}%`,
          background: isComplete
            ? (isSuccess ? 'linear-gradient(90deg,#10b981,#34d399)' : 'linear-gradient(90deg,#ef4444,#f87171)')
            : 'linear-gradient(90deg,#4f46e5,#6366f1,#a5b4fc,#6366f1,#4f46e5)',
          backgroundSize: isComplete ? '100%' : '300% 100%',
          animation: isComplete ? 'none' : 'ws-progress-shimmer 2.2s linear infinite',
          transition: 'width 0.7s ease, background 0.5s ease',
          borderRadius: '0 2px 2px 0',
        }} />
      </div>

      {/* ── Back to AI Workspace ── */}
      {isComplete && onBack && (
        <div style={{ padding: '8px 16px' }}>
          <button onClick={onBack} style={{ display: 'inline-flex', alignItems: 'center', gap: '5px', background: 'none', border: 'none', padding: '0', fontSize: '0.68rem', fontWeight: '600', color: C.textSec, cursor: 'pointer', fontFamily: FONT }}>
            <svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round"><line x1="19" y1="12" x2="5" y2="12"/><polyline points="12 19 5 12 12 5"/></svg>
            Back to AI Workspace
          </button>
        </div>
      )}

      {/* ── Header ── */}
      <div style={{ padding: '10px 24px 16px', borderBottom: `1px solid ${C.border}`, display: 'flex', alignItems: 'center', gap: '16px' }}>

        {/* Title + status badge */}
        <div style={{ flex: 1, minWidth: 0 }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: '8px', marginBottom: '5px' }}>
            <span style={{ fontSize: '0.85rem', fontWeight: '800', color: '#ffffff', textTransform: 'uppercase', letterSpacing: '0.16em' }}>Execution Console</span>
            {!isComplete ? (
              <span style={{ fontSize: '0.58rem', fontWeight: '700', color: '#6366f1', background: 'rgba(99,102,241,0.12)', border: '1px solid rgba(99,102,241,0.25)', borderRadius: '20px', padding: '2px 9px', display: 'inline-flex', alignItems: 'center', gap: '5px' }}>
                <div style={{ width: '5px', height: '5px', borderRadius: '50%', background: '#6366f1', animation: 'ws-core-breathe 1.2s ease infinite' }} />
                Running
              </span>
            ) : (
              <span style={{ fontSize: '0.58rem', fontWeight: '700', color: sColor, background: `${sColor}12`, border: `1px solid ${sColor}30`, borderRadius: '20px', padding: '2px 9px', animation: 'ws-fadein 0.4s ease' }}>
                {isSuccess ? 'Completed' : 'Failed'}
              </span>
            )}
          </div>
        </div>

        {/* Right: % progress while running → action buttons when done */}
        {!isComplete ? (
          <div style={{ textAlign: 'right', flexShrink: 0 }}>
            <div style={{ fontSize: '2.2rem', fontWeight: '800', color: '#6366f1', lineHeight: 1, fontFamily: MONO, letterSpacing: '-2px' }}>{phaseProgress}%</div>
            <div style={{ fontSize: '0.53rem', color: C.textMuted, marginTop: '2px', textTransform: 'uppercase', letterSpacing: '0.08em' }}>Overall Progress</div>
          </div>
        ) : (
          <div style={{ display: 'flex', gap: '7px', flexShrink: 0, animation: 'ws-fadein 0.4s ease' }}>
            {onRunAgain && (
              <button onClick={onRunAgain} style={{ display: 'flex', alignItems: 'center', gap: '5px', background: 'none', border: `1px solid ${C.border}`, borderRadius: '8px', padding: '6px 12px', fontSize: '0.71rem', fontWeight: '600', color: C.textSec, cursor: 'pointer', fontFamily: FONT, whiteSpace: 'nowrap' }}>
                <svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round"><polyline points="23 4 23 10 17 10"/><path d="M20.49 15a9 9 0 1 1-2.12-9.36L23 10"/></svg>
                Run Again
              </button>
            )}
            {result?.report_id && onOpenReport && (
              <button onClick={() => onOpenReport(result.report_id)} style={{ display: 'flex', alignItems: 'center', gap: '5px', background: '#6366f1', border: '1px solid #6366f1', borderRadius: '8px', padding: '6px 14px', fontSize: '0.71rem', fontWeight: '600', color: '#fff', cursor: 'pointer', fontFamily: FONT, whiteSpace: 'nowrap' }}>
                <svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/></svg>
                View Report
              </button>
            )}
          </div>
        )}
      </div>

      {/* ── Two-column body ── */}
      <div style={{ display: 'flex' }}>

        {/* Left: Metadata bar + Execution steps */}
        <div style={{ flex: 1, minWidth: 0, borderRight: `1px solid ${C.border}` }}>

          {/* Metadata columns */}
          {(() => {
            const runDate        = result?.started_at ? new Date(result.started_at).toISOString().slice(0,10).replace(/-/g,'') : null
            const runSeq         = result?.report_id  ? String(result.report_id).padStart(3,'0') : '001'
            const runId          = result?.run_id != null ? String(result.run_id) : (runDate ? `RUN-${runDate}-${runSeq}` : null)
            const execLabel      = runId ? `Execution #${runId}` : 'Execution'
            const fmtDT          = iso => {
              if (!iso) return null
              const d = new Date(iso)
              return [
                d.toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit', second: '2-digit' }),
                d.toLocaleDateString(undefined, { month: 'short', day: '2-digit', year: 'numeric' }),
              ]
            }
            const [startTime, startDate] = fmtDT(result?.started_at)  || []
            const [endTime,   endDate  ] = fmtDT(result?.finished_at) || []
            const rowCount = activeDs?.row_count != null ? Number(activeDs.row_count).toLocaleString() + ' rows' : null
            const cols = [
              { label: 'Started',        primary: startTime ?? null, secondary: startDate ?? null },
              { label: 'Finished',       primary: endTime   ?? null, secondary: endDate   ?? null },
              { label: 'Total Duration', primary: fmtTotal  ?? null, secondary: null, mono: true  },
              datasetName ? { label: 'Dataset',    primary: datasetName,      secondary: rowCount,   ellipsis: true } : null,
            ].filter(Boolean)
            return (
              <div style={{ display: 'flex', borderBottom: `1px solid ${C.border}` }}>
                {cols.map((col, idx) => (
                  <div key={col.label} style={{ flex: 1, minWidth: 0, padding: '5px 12px', borderRight: idx < cols.length - 1 ? `1px solid ${C.border}` : 'none' }}>
                    <div style={{ fontSize: '0.43rem', fontWeight: '700', color: C.textMuted, textTransform: 'uppercase', letterSpacing: '0.10em', marginBottom: '2px' }}>{col.label}</div>
                    {col.avatar && col.primary ? (
                      <div style={{ display: 'flex', alignItems: 'center', gap: '6px' }}>
                        <div style={{ width: '20px', height: '20px', borderRadius: '50%', background: 'linear-gradient(135deg,#6366f1,#8b5cf6)', display: 'flex', alignItems: 'center', justifyContent: 'center', fontSize: '0.55rem', fontWeight: '700', color: '#fff', flexShrink: 0 }}>
                          {col.primary[0].toUpperCase()}
                        </div>
                        <div style={{ fontSize: '0.62rem', fontWeight: '700', color: C.text, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{col.primary}</div>
                      </div>
                    ) : (
                      <div style={{ fontSize: '0.62rem', fontWeight: '700', color: col.primary ? C.text : C.textMuted, fontFamily: col.mono ? MONO : 'inherit', overflow: col.ellipsis ? 'hidden' : 'visible', textOverflow: col.ellipsis ? 'ellipsis' : 'clip', whiteSpace: col.ellipsis ? 'nowrap' : 'normal' }}>{col.primary ?? '—'}</div>
                    )}
                    {col.secondary && <div style={{ fontSize: '0.53rem', color: C.textMuted, marginTop: '1px' }}>{col.secondary}</div>}
                  </div>
                ))}
              </div>
            )
          })()}

          <div style={{ padding: '12px 20px 10px', borderBottom: `1px solid ${C.border}`, display: 'flex', alignItems: 'center', gap: '10px' }}>
            <span style={{ fontSize: '0.78rem', fontWeight: '700', color: C.text }}>Execution Steps</span>
            <span style={{ fontSize: '0.59rem', fontWeight: '700', color: C.textMuted, background: C.borderAlt, border: `1px solid ${C.border}`, borderRadius: '20px', padding: '2px 8px' }}>{stepsToRender.length} Steps</span>
          </div>

          <div style={{ padding: '10px 16px 14px' }}>
            {stepsToRender.map((phase, i) => {
              // Status is always driven by the running animation or the overall result —
              // never by individual backend step objects (preserves the 6-phase visual journey).
              const isFailed  = isComplete && !isSuccess
              const isDone    = isComplete ? isSuccess : i < activePhase
              const isActive  = !isComplete && i === activePhase
              const isPending = !isComplete && i > activePhase
              const isLast    = i === stepsToRender.length - 1
              const pc        = EC_PHASE_COLORS[i % EC_PHASE_COLORS.length]
              const iconType  = phase.icon
              const label     = phase.label
              const dotBg     = isFailed ? '#ef4444' : isDone ? '#10b981' : isActive ? '#6366f1' : 'transparent'
              const dotBorder = isFailed ? '#ef4444' : isDone ? '#10b981' : isActive ? '#6366f1' : C.borderAlt
              const iconColor = isFailed ? '#ef4444' : isDone ? pc.fg : isActive ? pc.fg : C.textMuted
              const iconBg    = isFailed ? 'rgba(239,68,68,0.14)' : isDone ? pc.bg : isActive ? pc.bg : C.bg
              const iconBdr   = isFailed ? 'rgba(239,68,68,0.28)' : isDone ? pc.border : isActive ? pc.border : C.border
              const rowBg     = isFailed ? 'rgba(239,68,68,0.04)' : (isComplete && isDone) ? 'rgba(16,185,129,0.04)' : isActive ? 'rgba(99,102,241,0.06)' : 'transparent'
              const rowBdr    = isFailed ? 'rgba(239,68,68,0.14)' : (isComplete && isDone) ? 'rgba(16,185,129,0.12)' : isActive ? 'rgba(99,102,241,0.18)' : 'transparent'
              const connBg    = isDone ? 'rgba(16,185,129,0.35)' : 'rgba(107,114,128,0.18)'

              return (
                <div key={i} style={{ display: 'flex', alignItems: 'flex-start', gap: '10px', animation: `ws-fadeup 0.3s ease both`, animationDelay: `${i * 0.05}s` }}>
                  {/* Number circle + connector */}
                  <div style={{ display: 'flex', flexDirection: 'column', alignItems: 'center', width: '28px', flexShrink: 0 }}>
                    <div style={{ width: '26px', height: '26px', borderRadius: '50%', flexShrink: 0, background: dotBg, border: `2px solid ${dotBorder}`, display: 'flex', alignItems: 'center', justifyContent: 'center', marginTop: '7px', animation: isActive ? 'ws-phase-active 1.6s ease-in-out infinite' : 'none', transition: 'background 0.4s ease, border-color 0.4s ease' }}>
                      {(isDone || isFailed)
                        ? <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="#fff" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
                            {isFailed ? <><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></> : <polyline points="20 6 9 17 4 12"/>}
                          </svg>
                        : <span style={{ fontSize: '0.57rem', fontWeight: '800', color: isActive ? '#fff' : C.textMuted }}>{i + 1}</span>
                      }
                    </div>
                    {!isLast && <div style={{ width: '2px', flex: 1, minHeight: '10px', background: connBg, borderRadius: '1px', margin: '3px 0', transition: 'background 0.4s ease' }} />}
                  </div>
                  {/* Step row */}
                  <div style={{ flex: 1, minWidth: 0, display: 'flex', alignItems: 'center', gap: '10px', padding: '7px 10px', borderRadius: '10px', background: rowBg, border: `1px solid ${rowBdr}`, marginBottom: isLast ? '0' : '4px', opacity: isPending ? 0.5 : 1, transition: 'background 0.3s ease, opacity 0.3s ease, border-color 0.3s ease' }}>
                    <div style={{ width: '30px', height: '30px', borderRadius: '8px', flexShrink: 0, background: iconBg, border: `1px solid ${iconBdr}`, display: 'flex', alignItems: 'center', justifyContent: 'center', transition: 'background 0.3s ease' }}>
                      <ExecPhaseIcon type={iconType} color={iconColor} size={13} />
                    </div>
                    <div style={{ flex: 1, minWidth: 0 }}>
                      <div style={{ fontSize: '0.76rem', fontWeight: isActive ? '700' : '500', color: (isDone || isFailed) ? C.text : isActive ? C.text : C.textMuted, lineHeight: 1.3, transition: 'color 0.3s ease' }}>{label}</div>
                    </div>
                    {isFailed   && <div style={{ fontSize: '0.57rem', fontWeight: '700', color: '#ef4444', background: 'rgba(239,68,68,0.12)',  border: '1px solid rgba(239,68,68,0.25)',  borderRadius: '20px', padding: '3px 9px', flexShrink: 0 }}>Failed</div>}
                    {!isFailed && isDone && (() => {
                      const hlMatch   = backendSteps[i]?.human_label?.match(/^(\d+)ms$/)
                      const backendMs = hlMatch ? parseInt(hlMatch[1]) : null
                      const estMs     = (!hasBackendStepDurations && execDurationMs != null && execDurationMs > 0)
                        ? Math.round(EXEC_PHASES[i].dur / totalPhaseDur * execDurationMs)
                        : null
                      const ms  = backendMs ?? estMs
                      const dur = (ms != null && ms > 0) ? `${(ms / 1000).toFixed(1)}s` : null
                      return (
                        <div style={{ display: 'flex', alignItems: 'center', gap: '6px', flexShrink: 0 }}>
                          <div style={{ fontSize: '0.57rem', fontWeight: '700', color: '#10b981', background: 'rgba(16,185,129,0.12)', border: '1px solid rgba(16,185,129,0.25)', borderRadius: '20px', padding: '3px 9px' }}>Completed</div>
                          {dur && <span style={{ fontSize: '0.60rem', color: C.textMuted, fontFamily: MONO }}>{dur}</span>}
                        </div>
                      )
                    })()}
                    {isActive   && <div style={{ fontSize: '0.57rem', fontWeight: '700', color: '#a5b4fc', background: 'rgba(99,102,241,0.12)', border: '1px solid rgba(99,102,241,0.25)', borderRadius: '20px', padding: '3px 9px', flexShrink: 0, display: 'flex', alignItems: 'center', gap: '5px' }}><div style={{ width: '5px', height: '5px', borderRadius: '50%', background: '#6366f1', animation: 'ws-core-breathe 1s ease infinite' }} />In Progress</div>}
                    {isPending  && <div style={{ fontSize: '0.57rem', color: C.textMuted, flexShrink: 0 }}>Pending</div>}
                  </div>
                </div>
              )
            })}
          </div>
        </div>

        {/* Right: context panel — request details while running, outcome events when complete */}
        <div style={{ width: '220px', flexShrink: 0, padding: '14px 18px' }}>
          {!isComplete ? (
            <>
              <div style={{ fontSize: '0.54rem', fontWeight: '800', color: '#6366f1', textTransform: 'uppercase', letterSpacing: '0.16em', marginBottom: '14px' }}>Execution Details</div>
              <div style={{ display: 'flex', flexDirection: 'column', gap: '12px' }}>
                <div>
                  <div style={{ fontSize: '0.50rem', fontWeight: '700', color: C.textMuted, textTransform: 'uppercase', letterSpacing: '0.10em', marginBottom: '3px' }}>Request</div>
                  <div style={{ fontSize: '0.70rem', color: C.textSec, lineHeight: 1.5, wordBreak: 'break-word' }}>{wsInput.trim().length > 100 ? wsInput.trim().slice(0, 100) + '…' : wsInput.trim() || '—'}</div>
                </div>
                <div>
                  <div style={{ fontSize: '0.50rem', fontWeight: '700', color: C.textMuted, textTransform: 'uppercase', letterSpacing: '0.10em', marginBottom: '3px' }}>Type</div>
                  <div style={{ fontSize: '0.70rem', fontWeight: '600', color: C.textSec }}>{execType}</div>
                </div>
                {datasetName && (
                  <div>
                    <div style={{ fontSize: '0.50rem', fontWeight: '700', color: C.textMuted, textTransform: 'uppercase', letterSpacing: '0.10em', marginBottom: '3px' }}>Dataset</div>
                    <div style={{ fontSize: '0.70rem', fontWeight: '600', color: C.textSec, wordBreak: 'break-all' }}>{datasetName}</div>
                  </div>
                )}
              </div>
            </>
          ) : (
            <div style={{ animation: 'ws-fadein 0.4s ease' }}>
              {/* Execution Summary */}
              <div style={{ fontSize: '0.52rem', fontWeight: '800', color: isSuccess ? '#10b981' : '#ef4444', textTransform: 'uppercase', letterSpacing: '0.12em', marginBottom: '8px' }}>Execution Summary</div>
              <div style={{ display: 'flex', flexDirection: 'column', gap: '6px', marginBottom: '14px' }}>
                {(() => {
                  const rd  = result?.started_at ? new Date(result.started_at).toISOString().slice(0,10).replace(/-/g,'') : null
                  const seq = result?.report_id  ? String(result.report_id).padStart(3,'0') : '001'
                  const rid = result?.run_id != null ? String(result.run_id) : (rd ? `RUN-${rd}-${seq}` : null)
                  return rid ? (
                    <div>
                      <div style={{ fontSize: '0.47rem', fontWeight: '700', color: C.textMuted, textTransform: 'uppercase', letterSpacing: '0.10em', marginBottom: '1px' }}>Execution ID</div>
                      <div style={{ fontSize: '0.68rem', fontWeight: '600', color: C.textSec, fontFamily: MONO, wordBreak: 'break-all' }}>{rid}</div>
                    </div>
                  ) : null
                })()}
                {datasetName && (
                  <div>
                    <div style={{ fontSize: '0.47rem', fontWeight: '700', color: C.textMuted, textTransform: 'uppercase', letterSpacing: '0.10em', marginBottom: '1px' }}>Dataset</div>
                    <div style={{ fontSize: '0.68rem', fontWeight: '600', color: C.textSec, wordBreak: 'break-all' }}>{datasetName}</div>
                  </div>
                )}
                <div>
                  <div style={{ fontSize: '0.47rem', fontWeight: '700', color: C.textMuted, textTransform: 'uppercase', letterSpacing: '0.10em', marginBottom: '1px' }}>Completed</div>
                  <div style={{ fontSize: '0.68rem', fontWeight: '600', color: C.textSec }}>{execType}</div>
                </div>
                {(reportGenerated || scheduleCreated || notifSent || emailSent || emailConfigured || notifConfigured) && (
                  <div>
                    <div style={{ fontSize: '0.47rem', fontWeight: '700', color: C.textMuted, textTransform: 'uppercase', letterSpacing: '0.10em', marginBottom: '4px' }}>Outputs</div>
                    <div style={{ display: 'flex', flexDirection: 'column', gap: '3px' }}>
                      {reportGenerated && (
                        <div style={{ display: 'flex', alignItems: 'center', gap: '5px' }}>
                          <svg width="9" height="9" viewBox="0 0 24 24" fill="none" stroke="#10b981" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round"><polyline points="20 6 9 17 4 12"/></svg>
                          <span style={{ fontSize: '0.68rem', fontWeight: '600', color: C.textSec }}>Intelligence Report</span>
                        </div>
                      )}
                      {scheduleCreated && (
                        <div style={{ display: 'flex', alignItems: 'center', gap: '5px' }}>
                          <svg width="9" height="9" viewBox="0 0 24 24" fill="none" stroke="#10b981" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round"><polyline points="20 6 9 17 4 12"/></svg>
                          <span style={{ fontSize: '0.68rem', fontWeight: '600', color: C.textSec }}>Schedule Created</span>
                        </div>
                      )}
                      {emailConfigured && (
                        <div style={{ display: 'flex', alignItems: 'center', gap: '5px' }}>
                          <svg width="9" height="9" viewBox="0 0 24 24" fill="none" stroke="#a5b4fc" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round"><polyline points="20 6 9 17 4 12"/></svg>
                          <span style={{ fontSize: '0.68rem', fontWeight: '600', color: C.textSec }}>Email Delivery Configured</span>
                        </div>
                      )}
                      {notifConfigured && (
                        <div style={{ display: 'flex', alignItems: 'center', gap: '5px' }}>
                          <svg width="9" height="9" viewBox="0 0 24 24" fill="none" stroke="#a5b4fc" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round"><polyline points="20 6 9 17 4 12"/></svg>
                          <span style={{ fontSize: '0.68rem', fontWeight: '600', color: C.textSec }}>In-App Notification Configured</span>
                        </div>
                      )}
                      {notifSent && (
                        <div style={{ display: 'flex', alignItems: 'center', gap: '5px' }}>
                          <svg width="9" height="9" viewBox="0 0 24 24" fill="none" stroke="#10b981" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round"><polyline points="20 6 9 17 4 12"/></svg>
                          <span style={{ fontSize: '0.68rem', fontWeight: '600', color: C.textSec }}>Notification Sent</span>
                        </div>
                      )}
                      {emailSent && (
                        <div style={{ display: 'flex', alignItems: 'center', gap: '5px' }}>
                          <svg width="9" height="9" viewBox="0 0 24 24" fill="none" stroke="#10b981" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round"><polyline points="20 6 9 17 4 12"/></svg>
                          <span style={{ fontSize: '0.68rem', fontWeight: '600', color: C.textSec }}>Email Delivered</span>
                        </div>
                      )}
                    </div>
                  </div>
                )}
                {!isSuccess && (
                  <div style={{ fontSize: '0.65rem', color: '#ef4444', marginTop: '2px' }}>Review execution steps for details</div>
                )}
              </div>
            </div>
          )}
        </div>
      </div>

    </div>
  )
}

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

function MiniSparkline({ direction }) {
  const pts = { up: [3,5,4,7,5,8,7,9], down: [9,7,8,5,7,4,5,3], flat: [5,6,5,6,5,7,5,6] }
  const data  = pts[direction] || pts.flat
  const color = direction === 'up' ? '#10b981' : direction === 'down' ? '#f87171' : '#64748b'
  const W = 80, H = 24
  const mn = Math.min(...data), mx = Math.max(...data), rng = (mx - mn) || 1
  const xs = data.map((_, i) => i / (data.length - 1) * W)
  const ys = data.map(v => H - ((v - mn) / rng) * (H - 4) - 1)
  const line = xs.map((x, i) => `${i ? 'L' : 'M'}${x.toFixed(1)},${ys[i].toFixed(1)}`).join(' ')
  const area = `${line} L${W},${H} L0,${H}Z`
  return (
    <svg width={W} height={H} viewBox={`0 0 ${W} ${H}`} style={{ display: 'block', marginTop: '8px' }} preserveAspectRatio="none">
      <path d={area} fill={color} fillOpacity="0.14" />
      <path d={line} fill="none" stroke={color} strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
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
      <MiniSparkline direction={dir || 'flat'} />
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

function CopilotPanel({ reportId, aiMeta, intel, user, token, onSessionExpired, C }) {
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

  const hasReport  = !!reportId
  const firstName  = user?.name?.split(' ')[0] || null
  const dur        = intel?.started_at && intel?.finished_at
    ? `${((new Date(intel.finished_at) - new Date(intel.started_at)) / 1000).toFixed(1)}s`
    : null
  const reportStyle = intel?.reportPlan?.report_style
    ?.replace(/_/g, ' ').replace(/\b\w/g, c => c.toUpperCase()) || null
  const conf = aiMeta?.confidence

  function AIcon({ type }) {
    const p = { width: 14, height: 14, viewBox: '0 0 24 24', fill: 'none', stroke: C.textMuted, strokeWidth: 1.9, strokeLinecap: 'round', strokeLinejoin: 'round' }
    if (type === 'calendar') return <svg {...p}><rect x="3" y="4" width="18" height="18" rx="2"/><line x1="16" y1="2" x2="16" y2="6"/><line x1="8" y1="2" x2="8" y2="6"/><line x1="3" y1="10" x2="21" y2="10"/></svg>
    if (type === 'share')    return <svg {...p}><circle cx="18" cy="5" r="3"/><circle cx="6" cy="12" r="3"/><circle cx="18" cy="19" r="3"/><line x1="8.59" y1="13.51" x2="15.42" y2="17.49"/><line x1="15.41" y1="6.51" x2="8.59" y2="10.49"/></svg>
    if (type === 'download') return <svg {...p}><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/></svg>
    if (type === 'grid')     return <svg {...p}><rect x="3" y="3" width="7" height="7"/><rect x="14" y="3" width="7" height="7"/><rect x="14" y="14" width="7" height="7"/><rect x="3" y="14" width="7" height="7"/></svg>
    if (type === 'bell')     return <svg {...p}><path d="M18 8A6 6 0 0 0 6 8c0 7-3 9-3 9h18s-3-2-3-9"/><path d="M13.73 21a2 2 0 0 1-3.46 0"/></svg>
    return null
  }

  return (
    <div style={{
      position: 'sticky', top: '16px',
      display: 'flex', flexDirection: 'column',
      background: C.surface, border: `1px solid ${C.border}`,
      borderRadius: '16px', overflow: 'hidden',
      boxShadow: '0 4px 24px rgba(0,0,0,0.12)',
      maxHeight: 'calc(100vh - 48px)', overflowY: 'auto',
    }}>

      {/* ── Header ── */}
      <div style={{ padding: '13px 16px', borderBottom: `1px solid ${C.borderAlt}`, display: 'flex', alignItems: 'center', gap: '8px', background: 'rgba(99,102,241,0.04)', flexShrink: 0, position: 'sticky', top: 0, zIndex: 2 }}>
        <div style={{ width: '26px', height: '26px', borderRadius: '8px', background: 'rgba(167,139,250,0.15)', display: 'flex', alignItems: 'center', justifyContent: 'center' }}>
          <svg width="13" height="13" viewBox="0 0 24 24" fill="#a5b4fc"><path d="M12 2 L14 10 L22 12 L14 14 L12 22 L10 14 L2 12 L10 10 Z"/></svg>
        </div>
        <span style={{ fontSize: '0.65rem', fontWeight: '800', color: C.text, letterSpacing: '0.08em', textTransform: 'uppercase' }}>AI Assistant</span>
        <div style={{ marginLeft: 'auto', display: 'flex', alignItems: 'center', gap: '5px' }}>
          <span style={{ width: '6px', height: '6px', borderRadius: '50%', background: '#10b981', animation: 'ws-dot-blink 2.4s ease infinite', flexShrink: 0 }} />
          <span style={{ fontSize: '0.62rem', fontWeight: '600', color: '#10b981' }}>Online</span>
        </div>
      </div>

      {/* ── Greeting + suggested questions ── */}
      <div style={{ padding: '14px 16px', borderBottom: `1px solid ${C.borderAlt}` }}>
        <p style={{ margin: '0 0 12px', fontSize: '0.80rem', color: C.textSec, lineHeight: 1.6 }}>
          {firstName ? `Hi ${firstName}! ` : ''}I can help you understand this report.
        </p>
        <div style={{ display: 'flex', flexDirection: 'column', gap: '5px' }}>
          {COPILOT_SUGGESTIONS.map(s => (
            <button key={s} className="ws-copilot-q" onClick={() => handleAsk(s)} disabled={!hasReport}
              style={{ background: C.bg, border: `1px solid ${C.borderAlt}`, borderRadius: '9px', padding: '8px 12px', fontSize: '0.73rem', color: hasReport ? C.textSec : C.textMuted, cursor: hasReport ? 'pointer' : 'default', textAlign: 'left', fontFamily: FONT, fontWeight: '400', opacity: hasReport ? 1 : 0.55, width: '100%', transition: 'all 0.14s' }}>
              {s}
            </button>
          ))}
        </div>
      </div>

      {/* ── AI Reasoning (compact) ── */}
      {aiMeta?.reasoning_summary && (
        <div style={{ padding: '12px 16px', borderBottom: `1px solid ${C.borderAlt}` }}>
          <div style={{ background: 'rgba(167,139,250,0.08)', border: '1px solid rgba(167,139,250,0.16)', borderRadius: '10px', padding: '10px 12px' }}>
            <div style={{ display: 'flex', alignItems: 'center', gap: '5px', marginBottom: '5px' }}>
              <svg width="9" height="9" viewBox="0 0 24 24" fill="#a5b4fc"><path d="M12 2l2.4 7.4H22l-6.2 4.5 2.4 7.4L12 17l-6.2 4.3 2.4-7.4L2 9.4h7.6z"/></svg>
              <span style={{ fontSize: '0.57rem', fontWeight: '800', color: '#a5b4fc', textTransform: 'uppercase', letterSpacing: '0.1em' }}>AI Reasoning</span>
            </div>
            <p style={{ margin: 0, fontSize: '0.72rem', color: C.textSec, lineHeight: 1.6 }}>{aiMeta.reasoning_summary}</p>
            {conf != null && (
              <div style={{ display: 'flex', alignItems: 'center', gap: '8px', marginTop: '8px' }}>
                <div style={{ flex: 1, height: '4px', background: C.border, borderRadius: '3px', overflow: 'hidden' }}>
                  <div style={{ height: '100%', width: `${Math.round(conf * 100)}%`, background: conf >= 0.85 ? 'linear-gradient(90deg,#10b981,#34d399)' : conf >= 0.65 ? 'linear-gradient(90deg,#f59e0b,#fbbf24)' : 'linear-gradient(90deg,#9ca3af,#d1d5db)', borderRadius: '3px', transition: 'width 0.7s ease' }} />
                </div>
                <span style={{ fontSize: '0.65rem', fontWeight: '700', color: C.textSec, minWidth: '28px', textAlign: 'right' }}>{Math.round(conf * 100)}%</span>
              </div>
            )}
          </div>
        </div>
      )}

      {/* ── Ask / Answer ── */}
      <div style={{ padding: '12px 16px', borderBottom: `1px solid ${C.borderAlt}` }}>
        <div style={{ display: 'flex', gap: '6px' }}>
          <input ref={inputRef} placeholder={hasReport ? 'Ask a follow-up question…' : 'Save report to enable Q&A'} value={question}
            onChange={e => setQuestion(e.target.value)} onKeyDown={e => { if (e.key === 'Enter' && !e.shiftKey) handleAsk() }}
            disabled={!hasReport}
            style={{ flex: 1, background: C.bg, border: `1px solid ${C.border}`, borderRadius: '8px', padding: '7px 11px', fontSize: '0.74rem', color: C.text, outline: 'none', fontFamily: FONT, transition: 'border-color 0.14s', opacity: hasReport ? 1 : 0.5 }}
            onFocus={e => { e.target.style.borderColor = '#6366f1' }} onBlur={e => { e.target.style.borderColor = C.border }}
          />
          <button onClick={() => handleAsk()} disabled={loading || !question.trim() || !hasReport}
            style={{ background: 'linear-gradient(135deg,#6366f1,#8b5cf6)', border: 'none', borderRadius: '8px', padding: '7px 11px', color: '#fff', cursor: (loading || !question.trim() || !hasReport) ? 'not-allowed' : 'pointer', opacity: (loading || !question.trim() || !hasReport) ? 0.45 : 1, flexShrink: 0 }}>
            {loading
              ? <div style={{ width: '10px', height: '10px', borderRadius: '50%', border: '2px solid #ffffff40', borderTopColor: '#fff', animation: 'ws-spin 0.7s linear infinite' }} />
              : <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="#fff" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round"><line x1="22" y1="2" x2="11" y2="13"/><polygon points="22 2 15 22 11 13 2 9 22 2"/></svg>}
          </button>
        </div>
        {error && <div style={{ fontSize: '0.71rem', color: C.danger, padding: '8px 10px', background: C.dangerSoft, borderRadius: '8px', marginTop: '8px' }}>{error}</div>}
        {answer && !loading && (
          <div style={{ background: 'rgba(99,102,241,0.06)', border: '1px solid rgba(99,102,241,0.18)', borderRadius: '10px', padding: '10px 12px', marginTop: '8px', animation: 'ws-fadein 0.3s ease' }}>
            <div style={{ display: 'flex', alignItems: 'center', gap: '5px', marginBottom: '5px' }}>
              <div style={{ width: '5px', height: '5px', borderRadius: '50%', background: '#6366f1', animation: 'ws-dot-blink 1.6s ease infinite' }} />
              <span style={{ fontSize: '0.58rem', color: '#a5b4fc', fontWeight: '700', textTransform: 'uppercase', letterSpacing: '0.1em' }}>AI Answer</span>
            </div>
            <p style={{ margin: 0, fontSize: '0.77rem', color: C.text, lineHeight: 1.65 }}>{answer}</p>
          </div>
        )}
        {loading && (
          <div style={{ display: 'flex', alignItems: 'center', gap: '7px', padding: '6px 0', marginTop: '4px' }}>
            {[0, 0.2, 0.4].map((d, i) => <div key={i} style={{ width: '6px', height: '6px', borderRadius: '50%', background: '#6366f1', opacity: 0.5, animation: `ws-dot-blink 1.2s ease ${d}s infinite` }} />)}
            <span style={{ fontSize: '0.7rem', color: C.textMuted }}>Thinking…</span>
          </div>
        )}
      </div>

      {/* ── Report Actions ── */}
      <div style={{ padding: '12px 16px', borderBottom: `1px solid ${C.borderAlt}` }}>
        <div style={{ fontSize: '0.59rem', fontWeight: '800', color: C.textMuted, textTransform: 'uppercase', letterSpacing: '0.12em', marginBottom: '6px' }}>Report Actions</div>
        {[
          { icon: 'calendar', label: 'Schedule This Report' },
          { icon: 'share',    label: 'Share Report' },
          { icon: 'download', label: 'Export Report',    right: <span style={{ marginLeft: 'auto', fontSize: '0.6rem', color: C.textMuted, background: C.bg, border: `1px solid ${C.border}`, borderRadius: '4px', padding: '1px 6px' }}>PDF</span> },
          { icon: 'grid',     label: 'Add to Dashboard' },
          { icon: 'bell',     label: 'Set Alert' },
        ].map((a, i) => (
          <button key={i}
            style={{ display: 'flex', alignItems: 'center', gap: '9px', background: 'none', border: 'none', padding: '8px 4px', borderRadius: '8px', cursor: 'pointer', fontFamily: FONT, fontSize: '0.76rem', color: C.textSec, width: '100%', textAlign: 'left', transition: 'background 0.12s' }}
            onMouseEnter={e => e.currentTarget.style.background = C.bg}
            onMouseLeave={e => e.currentTarget.style.background = 'none'}>
            <AIcon type={a.icon} />
            {a.label}
            {a.right}
          </button>
        ))}
      </div>

      {/* ── Report Info ── */}
      <div style={{ padding: '12px 16px' }}>
        <div style={{ fontSize: '0.59rem', fontWeight: '800', color: C.textMuted, textTransform: 'uppercase', letterSpacing: '0.12em', marginBottom: '8px' }}>Report Info</div>
        <div style={{ display: 'flex', flexDirection: 'column', gap: '7px' }}>
          {[
            intel?.reportId             && { label: 'Report ID',     value: `#RPT-${intel.reportId}` },
            intel?.started_at           && { label: 'Generated',     value: new Date(intel.started_at).toLocaleString(undefined, { month: 'short', day: 'numeric', year: 'numeric', hour: '2-digit', minute: '2-digit' }) },
            dur                         && { label: 'Duration',      value: dur },
            reportStyle                 && { label: 'Analysis Type', value: reportStyle },
          ].filter(Boolean).map((row, i) => (
            <div key={i} style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'baseline', gap: '8px' }}>
              <span style={{ fontSize: '0.67rem', color: C.textMuted, flexShrink: 0 }}>{row.label}</span>
              <span style={{ fontSize: '0.67rem', color: C.textSec, fontWeight: '600', textAlign: 'right', maxWidth: '160px', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{row.value}</span>
            </div>
          ))}
        </div>
      </div>
    </div>
  )
}

function PremiumExecutiveHero({ intel, C, onOpenReport, setActiveNav }) {
  const aiMeta = intel.aiMeta
  const dur = intel.started_at && intel.finished_at
    ? `${((new Date(intel.finished_at) - new Date(intel.started_at)) / 1000).toFixed(1)}s`
    : null
  const { topInsight, highestRisk, topAction, topOpportunity } = intel

  const miniCards = [
    topInsight    && { label: 'Top Insight',        text: topInsight,    accent: '#a5b4fc', bg: 'rgba(167,139,250,0.07)', border: 'rgba(167,139,250,0.20)', icon: <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="#a5b4fc" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><circle cx="12" cy="12" r="10"/><line x1="12" y1="8" x2="12" y2="12"/><line x1="12" y1="16" x2="12.01" y2="16"/></svg> },
    highestRisk   && { label: 'Highest Risk',       text: highestRisk,   accent: '#f87171', bg: 'rgba(248,113,113,0.06)', border: 'rgba(248,113,113,0.18)', icon: <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="#f87171" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M10.29 3.86L1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"/><line x1="12" y1="9" x2="12" y2="13"/><line x1="12" y1="17" x2="12.01" y2="17"/></svg> },
    topAction     && { label: 'Recommended Action', text: topAction,     accent: '#10b981', bg: 'rgba(16,185,129,0.06)',  border: 'rgba(16,185,129,0.18)',  icon: <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="#10b981" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><polygon points="13 2 3 14 12 14 11 22 21 10 12 10 13 2"/></svg> },
    topOpportunity && { label: 'Key Opportunity',   text: topOpportunity, accent: '#38bdf8', bg: 'rgba(56,189,248,0.06)', border: 'rgba(56,189,248,0.18)',  icon: <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="#38bdf8" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><polyline points="23 6 13.5 15.5 8.5 10.5 1 18"/><polyline points="17 6 23 6 23 12"/></svg> },
  ].filter(Boolean)

  return (
    <div className="ws-section ws-s1" style={{ position: 'relative', overflow: 'hidden', borderRadius: '20px', background: C.surface, border: '1px solid rgba(99,102,241,0.22)', boxShadow: '0 8px 48px rgba(0,0,0,0.16), 0 2px 8px rgba(0,0,0,0.08)' }}>
      {/* Accent gradient strip */}
      <div style={{ height: '3px', background: 'linear-gradient(90deg, #4f46e5 0%, #6366f1 40%, #a5b4fc 70%, #60a5fa 100%)' }} />

      {/* Ambient glows */}
      <div style={{ position: 'absolute', top: 0, right: 0, width: '420px', height: '280px', background: 'radial-gradient(ellipse at 90% 0%, rgba(99,102,241,0.10) 0%, rgba(59,130,246,0.04) 50%, transparent 70%)', pointerEvents: 'none', zIndex: 0, animation: 'ws-hero-ambient 5s ease-in-out infinite' }} />
      <div style={{ position: 'absolute', bottom: 0, left: 0, width: '240px', height: '180px', background: 'radial-gradient(ellipse at 0% 100%, rgba(16,185,129,0.05) 0%, transparent 70%)', pointerEvents: 'none', zIndex: 0 }} />

      <div style={{ position: 'relative', zIndex: 1 }}>
        {/* Main hero body */}
        <div style={{ padding: '30px 38px 26px' }}>
          {/* Meta row */}
          <div style={{ display: 'flex', alignItems: 'center', gap: '10px', marginBottom: '20px', flexWrap: 'wrap' }}>
            <div style={{ display: 'flex', alignItems: 'center', gap: '6px', background: 'rgba(99,102,241,0.1)', border: '1px solid rgba(99,102,241,0.22)', borderRadius: '20px', padding: '4px 11px 4px 8px' }}>
              <svg width="10" height="10" viewBox="0 0 24 24" fill="#a5b4fc"><path d="M12 2l2.4 7.4H22l-6.2 4.5 2.4 7.4L12 17l-6.2 4.3 2.4-7.4L2 9.4h7.6z"/></svg>
              <span style={{ fontSize: '0.56rem', fontWeight: '800', color: '#a5b4fc', textTransform: 'uppercase', letterSpacing: '0.16em' }}>Executive Briefing</span>
            </div>
            <ReportStyleBadge style={intel.reportPlan?.report_style} />
            {aiMeta && <AiLiveBadge aiMeta={aiMeta} />}
          </div>

          {/* Title + actions row */}
          <div style={{ display: 'flex', alignItems: 'flex-start', justifyContent: 'space-between', gap: '28px', flexWrap: 'wrap', marginBottom: miniCards.length ? '22px' : '0' }}>
            <div style={{ flex: 1, minWidth: '260px' }}>
              <h2 style={{ margin: '0 0 14px', fontSize: 'clamp(1.35rem, 2.4vw, 1.85rem)', fontWeight: '800', color: C.text, letterSpacing: '-0.8px', lineHeight: 1.15 }}>{intel.title}</h2>
              {intel.execSummary && <p style={{ margin: 0, fontSize: '0.9rem', color: C.textSec, lineHeight: 1.82, maxWidth: '580px' }}>{intel.execSummary}</p>}
            </div>
            <div style={{ display: 'flex', flexDirection: 'column', gap: '10px', alignItems: 'flex-end', flexShrink: 0 }}>
              {aiMeta?.confidence != null && <ConfidenceRing confidence={aiMeta.confidence} />}
              {intel.reportId && onOpenReport && (
                <button className="ws-action-btn" onClick={() => onOpenReport(intel.reportId)}
                  style={{ background: 'linear-gradient(135deg, #6366f1, #8b5cf6)', border: 'none', borderRadius: '10px', padding: '9px 18px', fontSize: '0.8rem', fontWeight: '700', color: '#fff', cursor: 'pointer', fontFamily: FONT, whiteSpace: 'nowrap', boxShadow: '0 4px 16px rgba(99,102,241,0.35)' }}>
                  Open Full Workspace
                </button>
              )}
              {intel.reportId && setActiveNav && (
                <button className="ws-ghost-btn" onClick={() => setActiveNav('reports')}
                  style={{ background: 'transparent', border: `1px solid ${C.border}`, borderRadius: '10px', padding: '7px 14px', fontSize: '0.74rem', fontWeight: '500', color: C.textMuted, cursor: 'pointer', fontFamily: FONT, whiteSpace: 'nowrap' }}>
                  View in Reports
                </button>
              )}
            </div>
          </div>

          {/* Insight mini-cards */}
          {miniCards.length > 0 && (
            <div style={{ display: 'grid', gridTemplateColumns: `repeat(${Math.min(miniCards.length, 2)}, 1fr)`, gap: '10px' }}>
              {miniCards.map((card, i) => (
                <div key={i} style={{ background: card.bg, border: `1px solid ${card.border}`, borderRadius: '12px', padding: '13px 16px', animation: `ws-reveal-up 0.4s ease both`, animationDelay: `${0.08 + i * 0.07}s` }}>
                  <div style={{ display: 'flex', alignItems: 'center', gap: '6px', marginBottom: '7px' }}>
                    <div style={{ width: '20px', height: '20px', borderRadius: '6px', background: `${card.accent}18`, display: 'flex', alignItems: 'center', justifyContent: 'center', flexShrink: 0 }}>{card.icon}</div>
                    <span style={{ fontSize: '0.57rem', fontWeight: '800', color: card.accent, textTransform: 'uppercase', letterSpacing: '0.11em' }}>{card.label}</span>
                  </div>
                  <p style={{ margin: 0, fontSize: '0.78rem', color: C.text, lineHeight: 1.65 }}>{card.text}</p>
                </div>
              ))}
            </div>
          )}
        </div>

        {/* Footer strip */}
        {(dur || intel.reportId || intel.emailDelivery?.sent) && (
          <div style={{ display: 'flex', alignItems: 'center', gap: '20px', padding: '11px 38px', borderTop: `1px solid ${C.border}`, background: C.bg, flexWrap: 'wrap' }}>
            {dur && <div style={{ display: 'flex', alignItems: 'center', gap: '5px', fontSize: '0.68rem', color: C.textMuted }}><svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 16 14"/></svg>Generated in {dur}</div>}
            {intel.reportId && <div style={{ display: 'flex', alignItems: 'center', gap: '5px', fontSize: '0.68rem', color: '#10b981' }}><svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><polyline points="20 6 9 17 4 12"/></svg>Saved as report #{intel.reportId}</div>}
            {intel.emailDelivery?.sent && <div style={{ display: 'flex', alignItems: 'center', gap: '5px', fontSize: '0.68rem', color: '#10b981' }}><svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M4 4h16c1.1 0 2 .9 2 2v12c0 1.1-.9 2-2 2H4c-1.1 0-2-.9-2-2V6c0-1.1.9-2 2-2z"/><polyline points="22,6 12,13 2,6"/></svg>Delivered to {intel.emailDelivery.to}</div>}
          </div>
        )}
      </div>
    </div>
  )
}

function WatchlistPanel({ watchlist, C }) {
  if (!watchlist?.length) return null
  const sevMap = { high: { color: '#f87171', bg: 'rgba(248,113,113,0.1)', label: 'HIGH' }, medium: { color: '#f59e0b', bg: 'rgba(245,158,11,0.1)', label: 'MED' }, low: { color: '#6366f1', bg: 'rgba(99,102,241,0.1)', label: 'LOW' } }
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
      <div style={{ display: 'flex', alignItems: 'center', gap: '9px', padding: '14px 22px', borderBottom: `1px solid ${C.border}`, background: 'rgba(99,102,241,0.04)' }}>
        <div style={{ width: '24px', height: '24px', borderRadius: '7px', background: 'rgba(99,102,241,0.15)', display: 'flex', alignItems: 'center', justifyContent: 'center', color: '#a5b4fc' }}>
          <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round"><path d="M9 11l3 3L22 4"/><path d="M21 12v7a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11"/></svg>
        </div>
        <span style={{ fontSize: '0.65rem', fontWeight: '800', color: '#a5b4fc', textTransform: 'uppercase', letterSpacing: '0.12em' }}>Priority Insights</span>
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
  const note = sec.explanation || sec.caption || sec.insight || null
  return (
    <div className="ws-chart-panel" style={{ background: C.surface, border: `1px solid ${C.border}`, borderRadius: '16px', overflow: 'hidden', boxShadow: '0 2px 16px rgba(0,0,0,0.07)', animation: `ws-fadeup 0.38s ease both`, animationDelay: `${delay}s` }}>
      {sec.heading && (
        <div style={{ padding: '14px 22px', borderBottom: `1px solid ${C.border}`, display: 'flex', alignItems: 'center', gap: '8px' }}>
          <div style={{ width: '4px', height: '16px', background: 'linear-gradient(180deg,#6366f1,#8b5cf6)', borderRadius: '2px', flexShrink: 0 }} />
          <span style={{ fontSize: '0.7rem', fontWeight: '700', color: C.textSec }}>{sec.heading}</span>
        </div>
      )}
      <div style={{ padding: '20px 22px' }}><ChartSection chart={sec.chart || {}} C={C} /></div>
      {note && (
        <div style={{ padding: '10px 22px 14px', borderTop: `1px solid ${C.border}`, display: 'flex', alignItems: 'flex-start', gap: '7px' }}>
          <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke={C.textMuted} strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" style={{ flexShrink: 0, marginTop: '2px', opacity: 0.6 }}><circle cx="12" cy="12" r="10"/><line x1="12" y1="8" x2="12" y2="12"/><line x1="12" y1="16" x2="12.01" y2="16"/></svg>
          <p style={{ margin: 0, fontSize: '0.72rem', color: C.textSec, lineHeight: 1.6 }}>{note}</p>
        </div>
      )}
    </div>
  )
}

// ─── Enterprise result sections ──────────────────────────────────────────────

function WorkflowTimeline({ intel, C }) {
  const totalMs  = intel.started_at && intel.finished_at
    ? (new Date(intel.finished_at) - new Date(intel.started_at))
    : null
  const totalSec = totalMs ? (totalMs / 1000).toFixed(1) : null
  const hasEmail = !!intel.emailDelivery?.sent

  function mkDur(pct) {
    if (!totalMs) return null
    const s = (totalMs * pct) / 1000
    return s >= 1 ? `${s.toFixed(1)}s` : `${(s * 1000).toFixed(0)}ms`
  }

  const steps = [
    {
      label: 'Request understood',
      desc:  'Intent parsed · capabilities selected · workflow planned',
      dur:   mkDur(0.08) || '0.3s',
      icon:  'star',   accent: '#a5b4fc',
    },
    {
      label: 'Dataset loaded',
      desc:  'Schema analysed · data quality validated · rows indexed',
      dur:   mkDur(0.18) || '0.8s',
      icon:  'database', accent: '#38bdf8',
    },
    {
      label: 'Analysis executed',
      desc:  [
        intel.kpis?.length > 0         && `${intel.kpis.length} KPIs computed`,
        intel.recommendations?.length > 0 && `${intel.recommendations.length} recommendations surfaced`,
        intel.watchlist?.length > 0    && `${intel.watchlist.length} drift signals detected`,
      ].filter(Boolean).join(' · ') || 'Patterns detected · anomalies ranked · insights extracted',
      dur:   mkDur(0.42) || '2.1s',
      icon:  'scan',   accent: '#f59e0b',
    },
    {
      label: 'Report generated',
      desc:  intel.title ? `"${intel.title}" compiled with executive summary` : 'Intelligence report compiled',
      dur:   mkDur(0.22) || '1.0s',
      icon:  'document', accent: '#6366f1',
    },
    {
      label: hasEmail ? 'Notification sent' : 'Notification prepared',
      desc:  hasEmail
        ? `Summary delivered to ${intel.emailDelivery.to || 'recipient'}`
        : 'Notification summary ready for delivery',
      dur:   mkDur(0.10) || '0.4s',
      icon:  'mail',   accent: hasEmail ? '#10b981' : '#94a3b8',
    },
  ]

  function TLIcon({ type, color, size = 11 }) {
    const p = { width: size, height: size, viewBox: '0 0 24 24', fill: 'none', stroke: color, strokeWidth: 2, strokeLinecap: 'round', strokeLinejoin: 'round' }
    if (type === 'star')     return <svg {...p} fill={color}><path d="M12 2l2.4 7.4H22l-6.2 4.5 2.4 7.4L12 17l-6.2 4.3 2.4-7.4L2 9.4h7.6z"/></svg>
    if (type === 'database') return <svg {...p}><ellipse cx="12" cy="5" rx="9" ry="3"/><path d="M21 12c0 1.66-4 3-9 3s-9-1.34-9-3"/><path d="M3 5v14c0 1.66 4 3 9 3s9-1.34 9-3V5"/></svg>
    if (type === 'scan')     return <svg {...p}><path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"/><circle cx="12" cy="12" r="3"/></svg>
    if (type === 'document') return <svg {...p}><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/></svg>
    if (type === 'mail')     return <svg {...p}><path d="M4 4h16c1.1 0 2 .9 2 2v12c0 1.1-.9 2-2 2H4c-1.1 0-2-.9-2-2V6c0-1.1.9-2 2-2z"/><polyline points="22,6 12,13 2,6"/></svg>
    return null
  }

  return (
    <div className="ws-section ws-s6" style={{ background: C.surface, border: `1px solid ${C.border}`, borderRadius: '16px', overflow: 'hidden', boxShadow: '0 2px 12px rgba(0,0,0,0.06)' }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: '9px', padding: '14px 22px', borderBottom: `1px solid ${C.border}`, background: 'rgba(99,102,241,0.03)' }}>
        <div style={{ width: '24px', height: '24px', borderRadius: '7px', background: 'rgba(99,102,241,0.14)', display: 'flex', alignItems: 'center', justifyContent: 'center' }}>
          <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="#6366f1" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round"><circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 16 14"/></svg>
        </div>
        <span style={{ fontSize: '0.65rem', fontWeight: '800', color: '#a5b4fc', textTransform: 'uppercase', letterSpacing: '0.12em' }}>Execution Timeline</span>
        {totalSec && (
          <span style={{ marginLeft: 'auto', fontSize: '0.67rem', color: C.textMuted, background: C.bg, borderRadius: '10px', padding: '2px 10px', border: `1px solid ${C.border}`, fontFamily: MONO }}>
            {totalSec}s total
          </span>
        )}
      </div>

      <div style={{ padding: '20px 22px 16px', display: 'flex', flexDirection: 'column' }}>
        {steps.map((step, i) => {
          const isLast = i === steps.length - 1
          return (
            <div key={i} style={{ display: 'flex', alignItems: 'stretch', animation: `ws-fadeup 0.35s ease both`, animationDelay: `${0.05 + i * 0.07}s` }}>
              {/* Connector column */}
              <div style={{ display: 'flex', flexDirection: 'column', alignItems: 'center', width: '36px', flexShrink: 0 }}>
                <div style={{ width: '28px', height: '28px', borderRadius: '50%', background: `${step.accent}14`, border: `2px solid ${step.accent}55`, display: 'flex', alignItems: 'center', justifyContent: 'center', flexShrink: 0, marginTop: '6px' }}>
                  <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="#10b981" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round"><polyline points="20 6 9 17 4 12"/></svg>
                </div>
                {!isLast && (
                  <div style={{ width: '2px', flex: 1, minHeight: '12px', background: `linear-gradient(180deg,${step.accent}35,${step.accent}08)`, borderRadius: '1px', margin: '3px 0' }} />
                )}
              </div>

              {/* Content */}
              <div style={{ flex: 1, marginLeft: '14px', paddingBottom: isLast ? '4px' : '14px' }}>
                <div style={{ display: 'flex', alignItems: 'center', gap: '8px', marginBottom: '4px', flexWrap: 'wrap' }}>
                  <div style={{ display: 'flex', alignItems: 'center', gap: '6px' }}>
                    <div style={{ width: '22px', height: '22px', borderRadius: '6px', background: `${step.accent}14`, display: 'flex', alignItems: 'center', justifyContent: 'center', flexShrink: 0 }}>
                      <TLIcon type={step.icon} color={step.accent} size={11} />
                    </div>
                    <span style={{ fontSize: '0.82rem', fontWeight: '700', color: C.text }}>{step.label}</span>
                  </div>
                  <span style={{ fontSize: '0.59rem', color: '#10b981', background: 'rgba(16,185,129,0.10)', border: '1px solid rgba(16,185,129,0.18)', borderRadius: '4px', padding: '1px 7px', fontWeight: '700', flexShrink: 0 }}>
                    ✓ Complete
                  </span>
                  {step.dur && (
                    <span style={{ fontSize: '0.62rem', color: C.textMuted, fontFamily: MONO, marginLeft: 'auto', flexShrink: 0 }}>{step.dur}</span>
                  )}
                </div>
                <p style={{ margin: 0, fontSize: '0.73rem', color: C.textSec, lineHeight: 1.6 }}>{step.desc}</p>
              </div>
            </div>
          )
        })}
      </div>
    </div>
  )
}

function GeneratedOutputs({ intel, C, onOpenReport, setActiveNav }) {
  const hasReport = !!intel.reportId
  const hasKpis   = (intel.kpis?.length || 0) > 0
  const hasEmail  = !!intel.emailDelivery?.sent

  const outputs = [
    hasReport && {
      key: 'report', icon: 'document', accent: '#6366f1',
      title:  intel.title || 'Intelligence Report',
      desc:   intel.execSummary
        ? (intel.execSummary.length > 110 ? intel.execSummary.slice(0, 110) + '…' : intel.execSummary)
        : 'Full executive report with KPIs, insights, and recommendations.',
      status: 'Ready', statusColor: '#10b981',
      actions: [
        onOpenReport && { label: 'View Report',       primary: true,  fn: () => onOpenReport(intel.reportId) },
        setActiveNav  && { label: 'Open in Reports',  primary: false, fn: () => setActiveNav('reports') },
      ].filter(Boolean),
    },
    {
      key: 'insights', icon: 'scan', accent: '#f59e0b',
      title:  'Dataset Insights',
      desc:   [
        intel.kpis?.length > 0         && `${intel.kpis.length} KPI${intel.kpis.length !== 1 ? 's' : ''}`,
        intel.recommendations?.length > 0 && `${intel.recommendations.length} recommendation${intel.recommendations.length !== 1 ? 's' : ''}`,
        intel.watchlist?.length > 0    && `${intel.watchlist.length} drift signal${intel.watchlist.length !== 1 ? 's' : ''}`,
      ].filter(Boolean).join(' · ') || 'Patterns, anomalies, and trends extracted from your dataset.',
      status: 'Ready', statusColor: '#10b981',
      actions: [],
    },
    hasKpis && {
      key: 'kpi', icon: 'bar', accent: '#38bdf8',
      title:  'KPI Summary',
      desc:   `${intel.kpis.length} performance indicator${intel.kpis.length !== 1 ? 's' : ''} computed${intel.kpis.some(k => k.status === 'risk') ? ' · risk flags detected' : ''}`,
      status: 'Ready', statusColor: '#10b981',
      actions: [],
    },
    hasEmail && {
      key: 'notification', icon: 'mail', accent: '#10b981',
      title:  'Notification',
      desc:   `Summary delivered to ${intel.emailDelivery.to || 'recipient'}`,
      status: 'Sent', statusColor: '#10b981',
      actions: [],
    },
  ].filter(Boolean)

  if (!outputs.length) return null

  function OutIcon({ type, color, size = 14 }) {
    const p = { width: size, height: size, viewBox: '0 0 24 24', fill: 'none', stroke: color, strokeWidth: 2, strokeLinecap: 'round', strokeLinejoin: 'round' }
    if (type === 'document') return <svg {...p}><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/><line x1="16" y1="13" x2="8" y2="13"/><line x1="16" y1="17" x2="8" y2="17"/></svg>
    if (type === 'scan')     return <svg {...p}><path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"/><circle cx="12" cy="12" r="3"/></svg>
    if (type === 'bar')      return <svg {...p}><line x1="18" y1="20" x2="18" y2="10"/><line x1="12" y1="20" x2="12" y2="4"/><line x1="6" y1="20" x2="6" y2="14"/></svg>
    if (type === 'mail')     return <svg {...p}><path d="M4 4h16c1.1 0 2 .9 2 2v12c0 1.1-.9 2-2 2H4c-1.1 0-2-.9-2-2V6c0-1.1.9-2 2-2z"/><polyline points="22,6 12,13 2,6"/></svg>
    return null
  }

  return (
    <div className="ws-section ws-s6" style={{ background: C.surface, border: `1px solid ${C.border}`, borderRadius: '16px', overflow: 'hidden', boxShadow: '0 2px 12px rgba(0,0,0,0.06)' }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: '9px', padding: '14px 22px', borderBottom: `1px solid ${C.border}`, background: 'rgba(56,189,248,0.03)' }}>
        <div style={{ width: '24px', height: '24px', borderRadius: '7px', background: 'rgba(56,189,248,0.14)', display: 'flex', alignItems: 'center', justifyContent: 'center' }}>
          <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="#38bdf8" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/></svg>
        </div>
        <span style={{ fontSize: '0.65rem', fontWeight: '800', color: '#38bdf8', textTransform: 'uppercase', letterSpacing: '0.12em' }}>Generated Outputs</span>
        <span style={{ marginLeft: 'auto', fontSize: '0.65rem', color: C.textMuted, background: C.bg, borderRadius: '10px', padding: '1px 8px', border: `1px solid ${C.border}` }}>
          {outputs.length} output{outputs.length !== 1 ? 's' : ''}
        </span>
      </div>

      <div style={{ padding: '16px 22px', display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(250px, 1fr))', gap: '12px' }}>
        {outputs.map((out, i) => (
          <div key={out.key}
            style={{ background: C.bg, border: `1px solid ${C.border}`, borderRadius: '14px', padding: '16px 18px', display: 'flex', flexDirection: 'column', gap: '10px', animation: `ws-fadeup 0.35s ease both`, animationDelay: `${0.06 + i * 0.06}s`, transition: 'border-color 0.14s, box-shadow 0.14s' }}
            onMouseEnter={e => { e.currentTarget.style.borderColor = `${out.accent}40`; e.currentTarget.style.boxShadow = `0 4px 16px ${out.accent}12` }}
            onMouseLeave={e => { e.currentTarget.style.borderColor = C.border; e.currentTarget.style.boxShadow = 'none' }}>
            <div style={{ display: 'flex', alignItems: 'center', gap: '10px' }}>
              <div style={{ width: '34px', height: '34px', borderRadius: '10px', background: `${out.accent}14`, border: `1px solid ${out.accent}28`, display: 'flex', alignItems: 'center', justifyContent: 'center', flexShrink: 0 }}>
                <OutIcon type={out.icon} color={out.accent} size={15} />
              </div>
              <div style={{ flex: 1, minWidth: 0 }}>
                <div style={{ fontSize: '0.81rem', fontWeight: '700', color: C.text, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', marginBottom: '3px' }}>{out.title}</div>
                <span style={{ display: 'inline-flex', alignItems: 'center', gap: '4px', fontSize: '0.58rem', fontWeight: '700', color: out.statusColor, background: `${out.statusColor}12`, borderRadius: '4px', padding: '1px 6px', textTransform: 'uppercase', letterSpacing: '0.06em' }}>
                  <span style={{ width: '4px', height: '4px', borderRadius: '50%', background: out.statusColor, flexShrink: 0 }} />
                  {out.status}
                </span>
              </div>
            </div>

            <p style={{ margin: 0, fontSize: '0.73rem', color: C.textSec, lineHeight: 1.6 }}>{out.desc}</p>

            {out.actions?.length > 0 && (
              <div style={{ display: 'flex', gap: '7px', flexWrap: 'wrap' }}>
                {out.actions.map((act, j) => (
                  <button key={j} onClick={act.fn}
                    style={{ background: act.primary ? `linear-gradient(135deg,${out.accent},${out.accent}cc)` : 'transparent', border: act.primary ? 'none' : `1px solid ${C.borderAlt}`, borderRadius: '8px', padding: '6px 13px', fontSize: '0.71rem', fontWeight: '700', color: act.primary ? '#fff' : C.textSec, cursor: 'pointer', fontFamily: FONT, boxShadow: act.primary ? `0 3px 10px ${out.accent}30` : 'none', transition: 'opacity 0.14s, transform 0.14s' }}
                    onMouseEnter={e => { e.currentTarget.style.opacity = '0.85'; e.currentTarget.style.transform = 'translateY(-1px)' }}
                    onMouseLeave={e => { e.currentTarget.style.opacity = '1';    e.currentTarget.style.transform = 'translateY(0)' }}>
                    {act.label}
                  </button>
                ))}
              </div>
            )}
          </div>
        ))}
      </div>
    </div>
  )
}

function ActionsCompleted({ intel, C }) {
  const hasEmail = !!intel.emailDelivery?.sent

  const actions = [
    {
      label:  'Analysed dataset',
      detail: intel.kpis?.length > 0
        ? `Processed data · extracted ${intel.kpis.length} KPI${intel.kpis.length !== 1 ? 's' : ''}`
        : 'Data patterns extracted and ranked by significance',
      color:  '#6366f1',
    },
    intel.kind === 'report' && {
      label:  'Generated intelligence report',
      detail: intel.title || 'Executive-level report with structured findings compiled',
      color:  '#38bdf8',
    },
    (intel.kpis?.length > 0 || intel.recommendations?.length > 0 || intel.watchlist?.length > 0) && {
      label:  'Surfaced insights',
      detail: [
        intel.kpis?.length > 0         && `${intel.kpis.length} KPI${intel.kpis.length !== 1 ? 's' : ''}`,
        intel.recommendations?.length > 0 && `${intel.recommendations.length} recommendation${intel.recommendations.length !== 1 ? 's' : ''}`,
        intel.watchlist?.length > 0    && `${intel.watchlist.length} drift signal${intel.watchlist.length !== 1 ? 's' : ''}`,
      ].filter(Boolean).join(' · ') + ' ready for review',
      color:  '#10b981',
    },
    hasEmail && {
      label:  'Notification delivered',
      detail: `Summary sent to ${intel.emailDelivery.to || 'recipient'}`,
      color:  '#f472b6',
    },
  ].filter(Boolean)

  if (!actions.length) return null

  return (
    <div className="ws-section ws-s6" style={{ background: C.surface, border: `1px solid ${C.border}`, borderRadius: '16px', overflow: 'hidden', boxShadow: '0 2px 12px rgba(0,0,0,0.06)' }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: '9px', padding: '14px 22px', borderBottom: `1px solid ${C.border}`, background: 'rgba(16,185,129,0.03)' }}>
        <div style={{ width: '24px', height: '24px', borderRadius: '7px', background: 'rgba(16,185,129,0.14)', display: 'flex', alignItems: 'center', justifyContent: 'center' }}>
          <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="#10b981" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round"><path d="M9 11l3 3L22 4"/><path d="M21 12v7a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11"/></svg>
        </div>
        <span style={{ fontSize: '0.65rem', fontWeight: '800', color: '#10b981', textTransform: 'uppercase', letterSpacing: '0.12em' }}>Actions Completed</span>
        <span style={{ marginLeft: 'auto', fontSize: '0.65rem', color: C.textMuted, background: C.bg, borderRadius: '10px', padding: '1px 8px', border: `1px solid ${C.border}` }}>
          {actions.length} action{actions.length !== 1 ? 's' : ''}
        </span>
      </div>

      <div style={{ padding: '14px 22px 18px', display: 'flex', flexDirection: 'column', gap: '8px' }}>
        {actions.map((action, i) => (
          <div key={i}
            style={{ display: 'flex', alignItems: 'center', gap: '13px', padding: '11px 14px', background: C.bg, borderRadius: '11px', border: `1px solid ${C.border}`, animation: `ws-fadeup 0.32s ease both`, animationDelay: `${0.05 + i * 0.06}s`, transition: 'border-color 0.14s' }}
            onMouseEnter={e => e.currentTarget.style.borderColor = `${action.color}30`}
            onMouseLeave={e => e.currentTarget.style.borderColor = C.border}>
            <div style={{ width: '28px', height: '28px', borderRadius: '50%', background: `${action.color}14`, border: `1.5px solid ${action.color}40`, display: 'flex', alignItems: 'center', justifyContent: 'center', flexShrink: 0 }}>
              <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke={action.color} strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round"><polyline points="20 6 9 17 4 12"/></svg>
            </div>
            <div style={{ flex: 1, minWidth: 0 }}>
              <div style={{ fontSize: '0.81rem', fontWeight: '700', color: C.text, marginBottom: '2px' }}>{action.label}</div>
              <div style={{ fontSize: '0.71rem', color: C.textMuted, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{action.detail}</div>
            </div>
            <span style={{ fontSize: '0.59rem', fontWeight: '700', color: '#10b981', background: 'rgba(16,185,129,0.10)', borderRadius: '4px', padding: '2px 7px', textTransform: 'uppercase', letterSpacing: '0.06em', flexShrink: 0 }}>Done</span>
          </div>
        ))}
      </div>
    </div>
  )
}

function SaveWorkflowCTA({ C, onSave, alreadySaved, saving }) {
  return (
    <div className="ws-section ws-s6" style={{ position: 'relative', overflow: 'hidden', borderRadius: '16px', background: C.surface, border: '1px solid rgba(99,102,241,0.20)', boxShadow: '0 4px 24px rgba(0,0,0,0.08)' }}>
      <div style={{ position: 'absolute', top: '-50px', right: '-50px', width: '220px', height: '220px', background: 'radial-gradient(circle, rgba(99,102,241,0.10) 0%, transparent 65%)', pointerEvents: 'none' }} />
      <div style={{ position: 'absolute', bottom: '-30px', left: '-20px', width: '140px', height: '140px', background: 'radial-gradient(circle, rgba(59,130,246,0.06) 0%, transparent 65%)', pointerEvents: 'none' }} />

      <div style={{ position: 'relative', padding: '22px 28px', display: 'flex', alignItems: 'center', gap: '20px', flexWrap: 'wrap' }}>
        <div style={{ width: '44px', height: '44px', borderRadius: '13px', background: 'rgba(99,102,241,0.14)', border: '1px solid rgba(99,102,241,0.25)', display: 'flex', alignItems: 'center', justifyContent: 'center', flexShrink: 0 }}>
          <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="#a5b4fc" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
            <path d="M19 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11l5 5v11a2 2 0 0 1-2 2z"/>
            <polyline points="17 21 17 13 7 13 7 21"/>
            <polyline points="7 3 7 8 15 8"/>
          </svg>
        </div>

        <div style={{ flex: 1, minWidth: '200px' }}>
          <div style={{ fontSize: '0.95rem', fontWeight: '700', color: C.text, marginBottom: '4px' }}>Save as Reusable Workflow</div>
          <p style={{ margin: 0, fontSize: '0.78rem', color: C.textSec, lineHeight: 1.6 }}>
            Turn this analysis into an automated workflow that runs on a schedule or on demand — no manual setup required.
          </p>
        </div>

        <div style={{ flexShrink: 0 }}>
          {alreadySaved ? (
            <div style={{ display: 'flex', alignItems: 'center', gap: '7px', padding: '9px 18px', background: 'rgba(16,185,129,0.12)', border: '1px solid rgba(16,185,129,0.28)', borderRadius: '10px', fontSize: '0.78rem', fontWeight: '700', color: '#10b981' }}>
              <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round"><polyline points="20 6 9 17 4 12"/></svg>
              Saved to Tools Library
            </div>
          ) : saving ? (
            <div style={{ display: 'flex', alignItems: 'center', gap: '9px', padding: '9px 18px', background: 'rgba(99,102,241,0.10)', border: '1px solid rgba(99,102,241,0.25)', borderRadius: '10px', fontSize: '0.78rem', fontWeight: '600', color: '#a5b4fc' }}>
              <div style={{ width: '13px', height: '13px', borderRadius: '50%', border: '2px solid rgba(167,139,250,0.35)', borderTopColor: '#a5b4fc', animation: 'ws-spin 0.75s linear infinite', flexShrink: 0 }} />
              Saving to library…
            </div>
          ) : (
            <button onClick={onSave}
              style={{ display: 'flex', alignItems: 'center', gap: '7px', background: 'linear-gradient(135deg,#4f46e5,#6366f1)', border: 'none', borderRadius: '10px', padding: '9px 18px', fontSize: '0.78rem', fontWeight: '700', color: '#fff', cursor: 'pointer', fontFamily: FONT, boxShadow: '0 4px 16px rgba(99,102,241,0.35)', transition: 'opacity 0.14s, transform 0.14s' }}
              onMouseEnter={e => { e.currentTarget.style.opacity = '0.9'; e.currentTarget.style.transform = 'translateY(-1px)' }}
              onMouseLeave={e => { e.currentTarget.style.opacity = '1';   e.currentTarget.style.transform = 'translateY(0)' }}>
              <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round"><path d="M19 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11l5 5v11a2 2 0 0 1-2 2z"/><polyline points="17 21 17 13 7 13 7 21"/><polyline points="7 3 7 8 15 8"/></svg>
              Save as Reusable Workflow
            </button>
          )}
        </div>
      </div>
    </div>
  )
}

function relTime(iso) {
  const m = Math.floor((Date.now() - new Date(iso).getTime()) / 60000)
  if (m < 1)  return 'just now'
  if (m < 60) return `${m}m ago`
  const h = Math.floor(m / 60)
  if (h < 24) return `${h}h ago`
  return `${Math.floor(h / 24)}d ago`
}

function ReusableWorkflows({ workflows, onRunWorkflow, C }) {
  const empty = !workflows.length

  return (
    <div style={{ background: C.surface, border: `1px solid ${C.border}`, borderRadius: '16px', overflow: 'hidden', boxShadow: '0 2px 12px rgba(0,0,0,0.06)', animation: 'ws-fadein 0.3s ease' }}>
      {/* Header */}
      <div style={{ display: 'flex', alignItems: 'center', gap: '9px', padding: '14px 22px', borderBottom: `1px solid ${C.border}`, background: 'rgba(99,102,241,0.03)' }}>
        <div style={{ width: '24px', height: '24px', borderRadius: '7px', background: 'rgba(99,102,241,0.14)', display: 'flex', alignItems: 'center', justifyContent: 'center', flexShrink: 0 }}>
          <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="#6366f1" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round"><polyline points="23 4 23 10 17 10"/><path d="M20.49 15a9 9 0 1 1-2.12-9.36L23 10"/></svg>
        </div>
        <span style={{ fontSize: '0.65rem', fontWeight: '800', color: '#a5b4fc', textTransform: 'uppercase', letterSpacing: '0.12em' }}>Reusable Workflows</span>
        {!empty && (
          <span style={{ marginLeft: 'auto', fontSize: '0.65rem', color: C.textMuted, background: C.bg, borderRadius: '10px', padding: '1px 8px', border: `1px solid ${C.border}` }}>
            {workflows.length} saved
          </span>
        )}
      </div>

      {empty ? (
        /* ── Empty state ── */
        <div style={{ padding: '36px 24px', textAlign: 'center' }}>
          <div style={{ width: '48px', height: '48px', borderRadius: '14px', background: 'rgba(99,102,241,0.08)', border: `1px solid rgba(99,102,241,0.18)`, display: 'flex', alignItems: 'center', justifyContent: 'center', margin: '0 auto 14px' }}>
            <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="#6366f1" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" style={{ opacity: 0.6 }}><polyline points="23 4 23 10 17 10"/><path d="M20.49 15a9 9 0 1 1-2.12-9.36L23 10"/></svg>
          </div>
          <div style={{ fontSize: '0.88rem', fontWeight: '700', color: C.textSec, marginBottom: '6px' }}>No reusable workflows yet</div>
          <p style={{ margin: 0, fontSize: '0.74rem', color: C.textMuted, lineHeight: 1.6, maxWidth: '280px', marginLeft: 'auto', marginRight: 'auto' }}>
            Run an analysis and save it to build your operational workflow library.
          </p>
        </div>
      ) : (
        /* ── Workflow card grid ── */
        <div style={{ padding: '16px 22px', display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(260px, 1fr))', gap: '12px' }}>
          {workflows.map((wf, i) => (
            <div key={wf.id}
              style={{ background: C.bg, border: `1px solid ${C.border}`, borderRadius: '14px', padding: '16px 18px', display: 'flex', flexDirection: 'column', gap: '10px', animation: `ws-fadeup 0.3s ease both`, animationDelay: `${i * 0.05}s`, transition: 'border-color 0.14s, box-shadow 0.14s' }}
              onMouseEnter={e => { e.currentTarget.style.borderColor = 'rgba(99,102,241,0.35)'; e.currentTarget.style.boxShadow = '0 4px 16px rgba(99,102,241,0.10)' }}
              onMouseLeave={e => { e.currentTarget.style.borderColor = C.border; e.currentTarget.style.boxShadow = 'none' }}>

              {/* Icon + title row */}
              <div style={{ display: 'flex', alignItems: 'flex-start', gap: '10px' }}>
                <div style={{ width: '32px', height: '32px', borderRadius: '9px', background: 'rgba(99,102,241,0.12)', border: '1px solid rgba(99,102,241,0.22)', display: 'flex', alignItems: 'center', justifyContent: 'center', flexShrink: 0, marginTop: '1px' }}>
                  <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="#a5b4fc" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><polygon points="13 2 3 14 12 14 11 22 21 10 12 10 13 2"/></svg>
                </div>
                <div style={{ flex: 1, minWidth: 0 }}>
                  <div style={{ fontSize: '0.83rem', fontWeight: '700', color: C.text, lineHeight: 1.3, marginBottom: '3px', overflow: 'hidden', display: '-webkit-box', WebkitLineClamp: 2, WebkitBoxOrient: 'vertical' }}>
                    {wf.title}
                  </div>
                  <div style={{ fontSize: '0.62rem', color: C.textMuted }}>{relTime(wf.createdAt)}</div>
                </div>
              </div>

              {/* Intent summary */}
              <p style={{ margin: 0, fontSize: '0.72rem', color: C.textSec, lineHeight: 1.55, overflow: 'hidden', display: '-webkit-box', WebkitLineClamp: 2, WebkitBoxOrient: 'vertical' }}>
                {wf.intent}
              </p>

              {/* Schedule badge */}
              {wf.scheduleType && { weekly: 'Weekly', daily: 'Daily', monthly: 'Monthly', recurring: 'Recurring', automated: 'Automated' }[wf.scheduleType] && (
                <div style={{ display: 'inline-flex', alignItems: 'center', gap: '5px', alignSelf: 'flex-start' }}>
                  <svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="#a5b4fc" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><rect x="3" y="4" width="18" height="18" rx="2"/><line x1="16" y1="2" x2="16" y2="6"/><line x1="8" y1="2" x2="8" y2="6"/><line x1="3" y1="10" x2="21" y2="10"/></svg>
                  <span style={{ fontSize: '0.62rem', fontWeight: '700', color: '#a5b4fc', background: 'rgba(167,139,250,0.10)', border: '1px solid rgba(167,139,250,0.22)', borderRadius: '5px', padding: '1px 7px', letterSpacing: '0.02em' }}>
                    {{ weekly: 'Weekly', daily: 'Daily', monthly: 'Monthly', recurring: 'Recurring', automated: 'Automated' }[wf.scheduleType]}
                  </span>
                </div>
              )}

              {/* Dataset tag */}
              {wf.datasetName && (
                <div style={{ display: 'flex', alignItems: 'center', gap: '5px' }}>
                  <svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="#fbbf24" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><ellipse cx="12" cy="5" rx="9" ry="3"/><path d="M21 12c0 1.66-4 3-9 3s-9-1.34-9-3"/><path d="M3 5v14c0 1.66 4 3 9 3s9-1.34 9-3V5"/></svg>
                  <span style={{ fontSize: '0.67rem', color: '#fbbf24', fontWeight: '600', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', maxWidth: '180px' }}>{wf.datasetName}</span>
                </div>
              )}

              {/* Run Again button */}
              <button onClick={() => onRunWorkflow(wf)}
                style={{ display: 'flex', alignItems: 'center', gap: '6px', background: 'linear-gradient(135deg,#4f46e5,#6366f1)', border: 'none', borderRadius: '9px', padding: '7px 14px', fontSize: '0.72rem', fontWeight: '700', color: '#fff', cursor: 'pointer', fontFamily: FONT, boxShadow: '0 3px 10px rgba(99,102,241,0.30)', transition: 'opacity 0.14s, transform 0.14s', alignSelf: 'flex-start', marginTop: 'auto' }}
                onMouseEnter={e => { e.currentTarget.style.opacity = '0.88'; e.currentTarget.style.transform = 'translateY(-1px)' }}
                onMouseLeave={e => { e.currentTarget.style.opacity = '1';    e.currentTarget.style.transform = 'translateY(0)' }}>
                <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="#fff" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round"><polygon points="5 3 19 12 5 21 5 3"/></svg>
                Run Again
              </button>
            </div>
          ))}
        </div>
      )}
    </div>
  )
}

// ─── Intelligence section renderers ──────────────────────────────────────────
// These components render the backend sections that were ignored before Step 1.
// Each is null-safe and returns null when the section is absent or empty.

function InsightPriorityPanel({ sec, C }) {
  const insights = sec?.insights
  if (!insights?.length) return null
  const sevMeta = {
    high:   { color: '#f87171', bg: 'rgba(248,113,113,0.10)', label: 'HIGH' },
    medium: { color: '#f59e0b', bg: 'rgba(245,158,11,0.10)',  label: 'MED'  },
    low:    { color: '#10b981', bg: 'rgba(16,185,129,0.10)',  label: 'LOW'  },
  }
  return (
    <div style={{ background: 'rgba(99,102,241,0.04)', border: '1px solid rgba(99,102,241,0.18)', borderRadius: '14px', overflow: 'hidden', animation: 'ws-fadein 0.25s ease' }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: '8px', padding: '10px 16px', borderBottom: '1px solid rgba(99,102,241,0.12)' }}>
        <svg width="11" height="11" viewBox="0 0 24 24" fill="#a5b4fc"><path d="M12 2l2.4 7.4H22l-6.2 4.5 2.4 7.4L12 17l-6.2 4.3 2.4-7.4L2 9.4h7.6z"/></svg>
        <span style={{ fontSize: '0.62rem', fontWeight: '800', color: '#a5b4fc', textTransform: 'uppercase', letterSpacing: '0.12em' }}>Top Insights</span>
        <span style={{ marginLeft: 'auto', fontSize: '0.61rem', color: '#a5b4fc', opacity: 0.7 }}>{insights.length} ranked</span>
      </div>
      <div style={{ display: 'flex', flexDirection: 'column' }}>
        {insights.slice(0, 5).map((ins, i) => {
          const sv = sevMeta[ins.severity] || sevMeta.low
          return (
            <div key={i}
              style={{ display: 'flex', alignItems: 'flex-start', gap: '10px', padding: '10px 16px', borderBottom: i < Math.min(insights.length, 5) - 1 ? '1px solid rgba(99,102,241,0.10)' : 'none', transition: 'background 0.12s' }}
              onMouseEnter={e => e.currentTarget.style.background = 'rgba(99,102,241,0.06)'}
              onMouseLeave={e => e.currentTarget.style.background = 'transparent'}>
              <span style={{ fontSize: '0.55rem', fontWeight: '800', color: sv.color, background: sv.bg, borderRadius: '4px', padding: '2px 6px', textTransform: 'uppercase', letterSpacing: '0.06em', flexShrink: 0, marginTop: '2px' }}>{sv.label}</span>
              <div style={{ flex: 1, minWidth: 0 }}>
                <div style={{ fontSize: '0.79rem', fontWeight: '600', color: C.text, marginBottom: ins.evidence ? '3px' : 0 }}>{ins.title}</div>
                {ins.evidence && <div style={{ fontSize: '0.70rem', color: C.textSec, lineHeight: 1.5 }}>{ins.evidence}</div>}
              </div>
            </div>
          )
        })}
      </div>
    </div>
  )
}

function AnomalyPanel({ sec, C }) {
  const anomalies = sec?.anomalies
  if (!anomalies?.length) return null
  const real = anomalies.filter(a => a.severity !== 'none' && a.category !== 'all_clear')
  if (!real.length) {
    return (
      <div style={{ display: 'flex', alignItems: 'center', gap: '10px', padding: '10px 16px', background: 'rgba(16,185,129,0.06)', border: '1px solid rgba(16,185,129,0.18)', borderRadius: '12px', animation: 'ws-fadein 0.25s ease' }}>
        <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="#10b981" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round"><path d="M22 11.08V12a10 10 0 1 1-5.93-9.14"/><polyline points="22 4 12 14.01 9 11.01"/></svg>
        <span style={{ fontSize: '0.76rem', fontWeight: '600', color: '#10b981' }}>No major anomalies detected</span>
        <span style={{ fontSize: '0.70rem', color: C.textMuted, marginLeft: '4px' }}>· {anomalies.length} check{anomalies.length !== 1 ? 's' : ''} passed</span>
      </div>
    )
  }
  const sevMeta = {
    high:   { color: '#f87171', bg: 'rgba(248,113,113,0.10)', label: 'HIGH' },
    medium: { color: '#f59e0b', bg: 'rgba(245,158,11,0.10)',  label: 'MED'  },
    low:    { color: '#6366f1', bg: 'rgba(99,102,241,0.08)',  label: 'LOW'  },
  }
  return (
    <div style={{ background: C.surface, border: '1px solid rgba(248,113,113,0.22)', borderRadius: '14px', overflow: 'hidden', animation: 'ws-fadein 0.25s ease' }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: '8px', padding: '10px 16px', borderBottom: '1px solid rgba(248,113,113,0.14)', background: 'rgba(248,113,113,0.04)' }}>
        <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="#f87171" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round"><path d="M10.29 3.86L1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"/><line x1="12" y1="9" x2="12" y2="13"/><line x1="12" y1="17" x2="12.01" y2="17"/></svg>
        <span style={{ fontSize: '0.62rem', fontWeight: '800', color: '#f87171', textTransform: 'uppercase', letterSpacing: '0.12em' }}>Anomalies & Risks</span>
        <span style={{ marginLeft: 'auto', fontSize: '0.61rem', color: '#f87171', opacity: 0.75 }}>{real.length} detected</span>
      </div>
      <div style={{ display: 'flex', flexDirection: 'column' }}>
        {real.slice(0, 5).map((a, i) => {
          const sv = sevMeta[a.severity] || sevMeta.low
          return (
            <div key={i}
              style={{ display: 'flex', alignItems: 'flex-start', gap: '10px', padding: '10px 16px', borderBottom: i < Math.min(real.length, 5) - 1 ? `1px solid ${C.border}` : 'none', transition: 'background 0.12s' }}
              onMouseEnter={e => e.currentTarget.style.background = C.bg}
              onMouseLeave={e => e.currentTarget.style.background = 'transparent'}>
              <span style={{ fontSize: '0.55rem', fontWeight: '800', color: sv.color, background: sv.bg, borderRadius: '4px', padding: '2px 6px', textTransform: 'uppercase', letterSpacing: '0.06em', flexShrink: 0, marginTop: '2px' }}>{sv.label}</span>
              <div style={{ flex: 1, minWidth: 0 }}>
                <div style={{ fontSize: '0.79rem', fontWeight: '600', color: C.text, marginBottom: a.evidence ? '3px' : 0 }}>{a.title}</div>
                {a.evidence && <div style={{ fontSize: '0.70rem', color: C.textSec, lineHeight: 1.5 }}>{a.evidence}</div>}
              </div>
            </div>
          )
        })}
      </div>
    </div>
  )
}

function TrendPanel({ sec, C }) {
  const trends = sec?.trends
  if (!trends?.length) return null
  const dirMeta = {
    up:      { color: '#10b981', symbol: '↑', label: 'Increasing' },
    down:    { color: '#f87171', symbol: '↓', label: 'Decreasing' },
    stable:  { color: '#94a3b8', symbol: '→', label: 'Stable'     },
    neutral: { color: '#94a3b8', symbol: '—', label: 'Neutral'    },
  }
  return (
    <div style={{ background: C.surface, border: `1px solid ${C.border}`, borderRadius: '14px', overflow: 'hidden', animation: 'ws-fadein 0.25s ease' }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: '8px', padding: '10px 16px', borderBottom: `1px solid ${C.border}`, background: 'rgba(56,189,248,0.03)' }}>
        <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="#38bdf8" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round"><polyline points="23 6 13.5 15.5 8.5 10.5 1 18"/><polyline points="17 6 23 6 23 12"/></svg>
        <span style={{ fontSize: '0.62rem', fontWeight: '800', color: '#38bdf8', textTransform: 'uppercase', letterSpacing: '0.12em' }}>Trend Intelligence</span>
        <span style={{ marginLeft: 'auto', fontSize: '0.61rem', color: C.textMuted }}>{trends.length} signal{trends.length !== 1 ? 's' : ''}</span>
      </div>
      <div style={{ padding: '12px 16px', display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(240px, 1fr))', gap: '10px' }}>
        {trends.map((t, i) => {
          const d = dirMeta[t.direction] || dirMeta.stable
          return (
            <div key={i} style={{ background: C.bg, border: `1px solid ${C.border}`, borderRadius: '10px', padding: '11px 14px' }}>
              <div style={{ display: 'flex', alignItems: 'center', gap: '8px', marginBottom: '5px' }}>
                <span style={{ fontSize: '1.05rem', fontWeight: '800', color: d.color, lineHeight: 1, flexShrink: 0 }}>{d.symbol}</span>
                <span style={{ fontSize: '0.78rem', fontWeight: '700', color: C.text, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{t.title}</span>
              </div>
              {t.evidence && <div style={{ fontSize: '0.70rem', color: C.textSec, lineHeight: 1.5 }}>{t.evidence}</div>}
            </div>
          )
        })}
      </div>
    </div>
  )
}

function ForecastPanel({ sec, C }) {
  if (!sec?.forecast_ready) return null
  const items  = sec.items || []
  const dirLine = items.find(it => it?.includes('Volume trend:')) || null
  const projLine = items.find(it => it?.includes('Projected average:')) || null
  return (
    <div style={{ background: C.surface, border: `1px solid ${C.border}`, borderRadius: '14px', overflow: 'hidden', animation: 'ws-fadein 0.25s ease' }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: '8px', padding: '10px 16px', borderBottom: `1px solid ${C.border}`, background: 'rgba(167,139,250,0.03)' }}>
        <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="#a5b4fc" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round"><path d="M22 12h-4l-3 9L9 3l-3 9H2"/></svg>
        <span style={{ fontSize: '0.62rem', fontWeight: '800', color: '#a5b4fc', textTransform: 'uppercase', letterSpacing: '0.12em' }}>Forecast · {sec.target_column}</span>
        {sec.horizon_periods != null && (
          <span style={{ marginLeft: 'auto', fontSize: '0.61rem', color: C.textMuted, background: C.bg, borderRadius: '8px', padding: '1px 7px', border: `1px solid ${C.border}` }}>{sec.horizon_periods} period{sec.horizon_periods !== 1 ? 's' : ''} ahead</span>
        )}
      </div>
      <div style={{ padding: '14px 16px' }}>
        {sec.chart && <ChartSection chart={sec.chart} C={C} />}
        {(dirLine || projLine) && (
          <div style={{ display: 'flex', flexDirection: 'column', gap: '4px', marginTop: sec.chart ? '12px' : 0 }}>
            {[dirLine, projLine].filter(Boolean).map((line, i) => (
              <div key={i} style={{ display: 'flex', alignItems: 'flex-start', gap: '7px', fontSize: '0.71rem', color: C.textSec, lineHeight: 1.5 }}>
                <span style={{ color: '#a5b4fc', flexShrink: 0 }}>·</span>{line}
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  )
}

function SegmentationPanel({ sec, C }) {
  const segments = sec?.segments
  if (!segments?.length) return null
  return (
    <div style={{ background: C.surface, border: `1px solid ${C.border}`, borderRadius: '14px', overflow: 'hidden', animation: 'ws-fadein 0.25s ease' }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: '8px', padding: '10px 16px', borderBottom: `1px solid ${C.border}`, background: 'rgba(16,185,129,0.03)' }}>
        <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="#10b981" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round"><rect x="3" y="3" width="7" height="7"/><rect x="14" y="3" width="7" height="7"/><rect x="14" y="14" width="7" height="7"/><rect x="3" y="14" width="7" height="7"/></svg>
        <span style={{ fontSize: '0.62rem', fontWeight: '800', color: '#10b981', textTransform: 'uppercase', letterSpacing: '0.12em' }}>Segment Analysis</span>
      </div>
      <div style={{ padding: '14px 16px', display: 'flex', flexDirection: 'column', gap: '16px' }}>
        {segments.slice(0, 3).map((seg, i) => {
          const top = seg.top_segments?.slice(0, 5) || []
          return (
            <div key={i}>
              <div style={{ fontSize: '0.64rem', fontWeight: '700', color: C.textSec, textTransform: 'uppercase', letterSpacing: '0.08em', marginBottom: '6px' }}>
                {seg.metric} by {seg.dimension}
              </div>
              {seg.insight_summary && (
                <div style={{ fontSize: '0.72rem', color: C.textSec, lineHeight: 1.5, marginBottom: '8px' }}>{seg.insight_summary}</div>
              )}
              {top.length > 0 && (
                <div style={{ display: 'flex', flexDirection: 'column', gap: '5px' }}>
                  {top.map((row, j) => {
                    const pct = row.pct_of_total != null ? Math.min(100, Math.round(row.pct_of_total)) : null
                    return (
                      <div key={j} style={{ display: 'flex', alignItems: 'center', gap: '10px' }}>
                        <span style={{ fontSize: '0.71rem', color: C.text, fontWeight: '500', minWidth: '90px', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{row.label}</span>
                        <div style={{ flex: 1, height: '5px', background: C.border, borderRadius: '3px', overflow: 'hidden' }}>
                          <div style={{ height: '100%', width: `${pct ?? 0}%`, background: 'linear-gradient(90deg,#10b981,#34d399)', borderRadius: '3px' }} />
                        </div>
                        {pct != null && <span style={{ fontSize: '0.66rem', color: C.textMuted, minWidth: '30px', textAlign: 'right' }}>{pct}%</span>}
                      </div>
                    )
                  })}
                </div>
              )}
            </div>
          )
        })}
      </div>
    </div>
  )
}

function DrilldownPanel({ sec, C }) {
  const tables = sec?.tables
  if (!tables?.length) return null
  return (
    <div style={{ background: C.surface, border: `1px solid ${C.border}`, borderRadius: '14px', overflow: 'hidden', animation: 'ws-fadein 0.25s ease' }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: '8px', padding: '10px 16px', borderBottom: `1px solid ${C.border}`, background: 'rgba(251,191,36,0.03)' }}>
        <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="#fbbf24" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round"><path d="M9 11l3 3L22 4"/><path d="M21 12v7a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11"/></svg>
        <span style={{ fontSize: '0.62rem', fontWeight: '800', color: '#fbbf24', textTransform: 'uppercase', letterSpacing: '0.12em' }}>Drilldown</span>
      </div>
      {tables.slice(0, 2).map((tbl, ti) => (
        <div key={ti} style={{ padding: '12px 16px', borderBottom: ti < Math.min(tables.length, 2) - 1 ? `1px solid ${C.border}` : 'none' }}>
          <div style={{ fontSize: '0.64rem', fontWeight: '700', color: C.textSec, textTransform: 'uppercase', letterSpacing: '0.08em', marginBottom: '6px' }}>
            {tbl.metric} by {tbl.dimension}
          </div>
          {tbl.summary && <div style={{ fontSize: '0.70rem', color: C.textMuted, marginBottom: '8px', lineHeight: 1.5 }}>{tbl.summary}</div>}
          {tbl.rows?.length > 0 && (
            <div style={{ overflowX: 'auto' }}>
              <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: '0.72rem' }}>
                <thead>
                  <tr>
                    {(tbl.columns || [tbl.dimension, 'Total', 'Share %']).slice(0, 4).map((col, ci) => (
                      <th key={ci} style={{ padding: '4px 10px', textAlign: ci === 0 ? 'left' : 'right', color: C.textMuted, fontWeight: '700', fontSize: '0.60rem', textTransform: 'uppercase', letterSpacing: '0.06em', borderBottom: `1px solid ${C.border}`, whiteSpace: 'nowrap', fontFamily: FONT }}>{col}</th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {tbl.rows.slice(0, 6).map((row, ri) => (
                    <tr key={ri}
                      onMouseEnter={e => e.currentTarget.style.background = C.bg}
                      onMouseLeave={e => e.currentTarget.style.background = 'transparent'}>
                      <td style={{ padding: '6px 10px', color: C.text, fontWeight: '500', maxWidth: '140px', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', borderBottom: `1px solid ${C.border}` }}>{row.label}</td>
                      <td style={{ padding: '6px 10px', color: C.text, textAlign: 'right', fontFamily: MONO, fontSize: '0.70rem', borderBottom: `1px solid ${C.border}` }}>
                        {row.value != null ? Number(row.value).toLocaleString(undefined, { maximumFractionDigits: 1 }) : '—'}
                      </td>
                      {row.count != null && (
                        <td style={{ padding: '6px 10px', color: C.textSec, textAlign: 'right', fontSize: '0.70rem', borderBottom: `1px solid ${C.border}` }}>{Number(row.count).toLocaleString()}</td>
                      )}
                      {row.pct_of_total != null && (
                        <td style={{ padding: '6px 10px', color: C.textMuted, textAlign: 'right', fontSize: '0.70rem', borderBottom: `1px solid ${C.border}` }}>{Math.round(row.pct_of_total)}%</td>
                      )}
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>
      ))}
    </div>
  )
}

// ─── Intent-adaptive helpers ─────────────────────────────────────────────────

function getDefaultTabForStyle(style) {
  if (style === 'visual_dashboard') return 'visuals'
  if (style === 'kpi_summary')      return 'kpis'
  return 'overview'
}

function buildAnswerSummary(intel) {
  if (!intel) return null
  const style = intel.reportPlan?.report_style
  const kpis  = intel.kpis || []

  if (style === 'kpi_summary') {
    if (!kpis.length) return null
    const top       = kpis[0]
    const formatted = top.value_formatted || top.value_display || String(top.value ?? '')
    return {
      accent: '#fbbf24',
      label:  'KPI Summary',
      text:   `${kpis.length} business KPI${kpis.length !== 1 ? 's' : ''} computed. Leading indicator: ${top.label} — ${formatted}.`,
    }
  }

  if (style === 'visual_dashboard') {
    const n = (intel.chartSecs || []).length
    return {
      accent: '#38bdf8',
      label:  'Visual Dashboard',
      text:   `${n > 0 ? `${n} chart${n !== 1 ? 's' : ''} generated.` : 'Visual analysis ready.'} Open the Visuals tab to explore all charts and trends.`,
    }
  }

  if (style === 'executive_brief') {
    const parts = [
      intel.execSummary,
      intel.topAction && `Recommended action: ${intel.topAction}`,
    ].filter(Boolean)
    if (!parts.length) return null
    return { accent: '#6366f1', label: 'Executive Brief', text: parts.join(' ') }
  }

  if (style === 'anomaly_report') {
    const flagged = (intel.anomalySec?.anomalies || []).filter(
      a => a.severity === 'high' || a.severity === 'medium'
    )
    if (!flagged.length && !intel.highestRisk) return null
    const leadText = flagged.length > 0
      ? `${flagged.length} anomal${flagged.length !== 1 ? 'ies' : 'y'} detected. Highest risk: ${flagged[0].title}.`
      : `Risk identified: ${intel.highestRisk}`
    return { accent: '#f87171', label: 'Risk & Anomalies', text: leadText }
  }

  if (style === 'operational_report' || style === 'monitoring_report') {
    const parts = [
      intel.watchlist?.length > 0       && `${intel.watchlist.length} drift signal${intel.watchlist.length !== 1 ? 's' : ''} detected.`,
      intel.recommendations?.length > 0 && `${intel.recommendations.length} recommended action${intel.recommendations.length !== 1 ? 's' : ''}.`,
    ].filter(Boolean)
    if (!parts.length) return null
    return {
      accent: '#10b981',
      label:  style === 'monitoring_report' ? 'Monitoring Report' : 'Operational Report',
      text:   parts.join(' '),
    }
  }

  // Dimension KPI — product/category/region/customer performance answer
  const dimKpi = kpis.find(k => ['product', 'category', 'region', 'customer'].includes(k.semantic_source))
  if (dimKpi) {
    const val  = dimKpi.value_formatted || dimKpi.value_display || String(dimKpi.value ?? '')
    const expl = dimKpi.explanation ? ` ${dimKpi.explanation}` : ''
    return { accent: '#a5b4fc', label: 'Top Finding', text: `${dimKpi.label}: ${val}.${expl}` }
  }

  // Revenue KPI
  const revKpi = kpis.find(k => k.semantic_source === 'revenue')
  if (revKpi) {
    const val = revKpi.value_formatted || String(revKpi.value ?? '')
    return { accent: '#10b981', label: 'Top Finding', text: `${revKpi.label}: ${val}. ${revKpi.explanation || ''}`.trim() }
  }

  // Fallback: exec summary
  if (intel.execSummary) {
    return { accent: '#94a3b8', label: 'Analysis Summary', text: intel.execSummary }
  }

  return null
}

function AnswerSummaryBlock({ intel, C }) {
  const summary = buildAnswerSummary(intel)
  if (!summary) return null
  return (
    <div style={{
      display: 'flex', alignItems: 'flex-start', gap: '12px',
      padding: '12px 18px',
      background: `${summary.accent}0d`,
      border: `1px solid ${summary.accent}30`,
      borderRadius: '12px',
      animation: 'ws-fadeup 0.30s ease both',
    }}>
      <div style={{
        width: '6px', height: '6px', borderRadius: '50%',
        background: summary.accent, flexShrink: 0, marginTop: '7px',
      }} />
      <div style={{ flex: 1, minWidth: 0 }}>
        <div style={{
          fontSize: '0.56rem', fontWeight: '800', color: summary.accent,
          textTransform: 'uppercase', letterSpacing: '0.12em', marginBottom: '4px',
        }}>
          {summary.label}
        </div>
        <p style={{ margin: 0, fontSize: '0.83rem', color: C.text, lineHeight: 1.65 }}>
          {summary.text}
        </p>
      </div>
    </div>
  )
}

function ReportDeliveryCard({ result, wsInput, C }) {
  if (!result) return null

  const schedType  = extractScheduleType(wsInput)
  const schedLabel = schedType
    ? ({ weekly: 'Weekly', daily: 'Daily', monthly: 'Monthly', recurring: 'Recurring', automated: 'Automated' })[schedType] ?? schedType
    : null

  const reportGenerated  = !!(result.report_id || result.dataset_report)
  const savedToReports   = !!result.report_id
  const scheduleCreated  = result.schedule_created === true || result.schedule_id != null
  const notifSent        = result.notification_sent === true || result.notification_id != null
  const emailSentCard    = !!(result.email_delivery?.sent)
  const emailConfigured  = result.email_delivery_configured === true && !emailSentCard
  const notifConfigured  = result.notification_configured === true && !notifSent
  const workflowCreated  = !!result.workflow_id ||
    /create_tool|create_workflow|save_workflow|build_workflow/i.test(result.task_type ?? '')

  const items = [
    reportGenerated && {
      label:  'Report Generated',
      detail: result.report_id ? `Report ID: ${result.report_id}` : null,
      color:  '#10b981',
    },
    savedToReports && {
      label:  'Saved to Reports',
      detail: null,
      color:  '#34d399',
    },
    scheduleCreated && {
      label:  'Schedule Created',
      detail: schedLabel ? `Schedule: ${schedLabel}` : result.schedule_id ? `ID: ${result.schedule_id}` : null,
      color:  '#a5b4fc',
    },
    emailConfigured && {
      label:  'Email Delivery Configured',
      detail: 'Will deliver on schedule',
      color:  '#a5b4fc',
    },
    notifConfigured && {
      label:  'In-App Notification Configured',
      detail: 'Will notify on schedule',
      color:  '#a5b4fc',
    },
    notifSent && {
      label:  'Notification Sent',
      detail: 'Notification Status: Sent',
      color:  '#38bdf8',
    },
    workflowCreated && {
      label:  'Workflow Created',
      detail: result.workflow_id ? `ID: ${result.workflow_id}` : null,
      color:  '#f59e0b',
    },
  ].filter(Boolean)

  if (items.length === 0) return null

  return (
    <div style={{
      background: C.surface, border: `1px solid ${C.border}`, borderRadius: '12px',
      padding: '10px 16px', display: 'flex', alignItems: 'center', gap: '12px', flexWrap: 'wrap',
      animation: 'ws-fadein 0.35s ease',
    }}>
      <span style={{ fontSize: '0.63rem', fontWeight: '700', color: C.textMuted, textTransform: 'uppercase', letterSpacing: '0.12em', flexShrink: 0 }}>Delivery Status</span>
      <div style={{ display: 'flex', alignItems: 'center', gap: '6px', flexWrap: 'wrap' }}>
        {items.map((item, i) => (
          <div key={i} style={{
            display: 'flex', alignItems: 'center', gap: '5px',
            padding: '4px 10px', background: `${item.color}0d`,
            border: `1px solid ${item.color}28`, borderRadius: '20px',
            animation: `ws-fadeup 0.25s ease both`, animationDelay: `${i * 0.05}s`,
          }}>
            <svg width="8" height="8" viewBox="0 0 24 24" fill="none" stroke={item.color} strokeWidth="2.8" strokeLinecap="round" strokeLinejoin="round"><polyline points="20 6 9 17 4 12"/></svg>
            <span style={{ fontSize: '0.70rem', fontWeight: '600', color: item.color, lineHeight: 1, whiteSpace: 'nowrap' }}>{item.label}</span>
          </div>
        ))}
      </div>
    </div>
  )
}

function WorkflowExplanationCard({ result, wsInput, enginePlan, datasetName, C }) {
  const hasCreationResult = !!result?.workflow_id ||
    /create_tool|create_workflow|save_workflow|build_workflow/i.test(result?.task_type ?? '')
  const hasCreationIntent = isToolCreationIntent(wsInput)
  const hasPlan           = !!enginePlan

  if (!hasPlan && !hasCreationIntent && !hasCreationResult) return null

  // ── Workflow Name ──────────────────────────────────────────────────────────
  const rawName = enginePlan?.description || (enginePlan?.name ? slugToTitle(enginePlan.name) : null)
  const wfName  = rawName
    ? (rawName.length <= 68 ? rawName : rawName.split(/[.!?]/)[0].trim() || rawName.slice(0, 68) + '…')
    : wsInput?.trim()
      ? (wsInput.trim().length <= 68 ? wsInput.trim() : wsInput.trim().slice(0, 68) + '…')
      : null

  // ── Workflow Type ──────────────────────────────────────────────────────────
  const schedType    = extractScheduleType(wsInput)
  const schedEnabled = enginePlan?.schedule?.enabled === true || !!schedType
  const isToolIntent = /\b(tool|automation|agent|pipeline)\b/i.test(wsInput ?? '')
  const wfType = schedEnabled                                   ? 'Scheduled Workflow'
    : isToolIntent                                              ? 'Automation'
    : (enginePlan?.graph?.nodes ?? []).length > 0              ? 'Workflow'
    : hasCreationResult                                        ? 'Workflow'
    : null

  // ── Output ─────────────────────────────────────────────────────────────────
  const reportOut = !!(result?.report_id || result?.dataset_report)
  const emailOut  = !!(result?.task_type?.includes('email') ||
    (enginePlan?.graph?.nodes ?? []).some(n => /send_email|email/i.test(n.action_type ?? '')))
  const outputLabel = reportOut && schedEnabled ? 'Scheduled Report'
    : reportOut && emailOut                     ? 'Email Report Delivery'
    : reportOut                                 ? 'Intelligence Report'
    : emailOut                                  ? 'Email Delivery'
    : null

  // ── Schedule ───────────────────────────────────────────────────────────────
  const schedFreq = enginePlan?.schedule?.human_label ??
    (schedType
      ? ({ weekly: 'Weekly', daily: 'Daily', monthly: 'Monthly', recurring: 'Recurring', automated: 'Automated' })[schedType]
      : null)

  // ── Notifications (only when actually sent) ────────────────────────────────
  const notifLabel = (result?.notification_sent === true || result?.notification_id != null)
    ? 'Enabled' : null

  // ── Status ─────────────────────────────────────────────────────────────────
  const status = (result?.schedule_created === true || result?.schedule_id != null) ? 'Scheduled'
    : result?.workflow_id                                                            ? 'Saved'
    : (result?.status === 'success' || result?.status === 'completed')              ? 'Executed'
    : null

  const fields = [
    wfName      && { label: 'Workflow Name', value: wfName },
    wfType      && { label: 'Workflow Type', value: wfType },
    datasetName && { label: 'Input',         value: datasetName },
    outputLabel && { label: 'Output',        value: outputLabel },
    schedFreq   && { label: 'Schedule',      value: schedFreq },
    notifLabel  && { label: 'Notifications', value: notifLabel },
    status      && { label: 'Status',        value: status },
  ].filter(Boolean)

  if (fields.length === 0) return null

  return (
    <div style={{
      background: C.surface, border: `1px solid ${C.border}`, borderRadius: '16px',
      overflow: 'hidden', boxShadow: '0 4px 24px rgba(0,0,0,0.08)',
      animation: 'ws-fadein 0.35s ease',
    }}>
      <div style={{ padding: '14px 20px 12px', borderBottom: `1px solid ${C.border}`, display: 'flex', alignItems: 'center', gap: '8px' }}>
        <div style={{ width: '18px', height: '18px', borderRadius: '50%', background: 'rgba(99,102,241,0.12)', border: '1.5px solid rgba(99,102,241,0.35)', display: 'flex', alignItems: 'center', justifyContent: 'center', flexShrink: 0 }}>
          <svg width="9" height="9" viewBox="0 0 24 24" fill="none" stroke="#6366f1" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
            <circle cx="18" cy="5" r="3"/><circle cx="6" cy="12" r="3"/><circle cx="18" cy="19" r="3"/>
            <line x1="8.59" y1="13.51" x2="15.42" y2="17.49"/><line x1="15.41" y1="6.51" x2="8.59" y2="10.49"/>
          </svg>
        </div>
        <div style={{ fontSize: '0.54rem', fontWeight: '800', color: '#6366f1', textTransform: 'uppercase', letterSpacing: '0.16em' }}>What ToolSmithAI Created</div>
      </div>
      <div style={{ padding: '14px 20px 16px', display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(160px, 1fr))', gap: '12px 20px' }}>
        {fields.map((f, i) => (
          <div key={i} style={{ animation: `ws-fadeup 0.3s ease both`, animationDelay: `${i * 0.05}s` }}>
            <div style={{ fontSize: '0.52rem', fontWeight: '800', color: C.textMuted, textTransform: 'uppercase', letterSpacing: '0.12em', marginBottom: '3px' }}>{f.label}</div>
            <div style={{ fontSize: '0.74rem', fontWeight: '600', color: C.textSec, lineHeight: 1.4, wordBreak: 'break-word' }}>{f.value}</div>
          </div>
        ))}
      </div>
    </div>
  )
}

function IntelligenceCanvas({ intel, C, onOpenReport, onExportReport, setActiveNav, onSaveWorkflow, workflowAlreadySaved, workflowSaving }) {
  // Hook must be called before any conditional returns (Rules of Hooks)
  const [activeTab, setActiveTab] = useState(() => getDefaultTabForStyle(intel?.reportPlan?.report_style))

  if (!intel) return null

  // ── Basic (non-report) result ─────────────────────────────────────────────
  if (intel.kind === 'basic') {
    const ok = intel.status === 'success' || intel.status === 'completed' || intel.status === 'ok'
    return (
      <div className="ws-section ws-s1" style={{ background: C.surface, border: `1px solid ${ok ? '#10b98130' : '#f8717130'}`, borderRadius: '18px', padding: '48px 36px', textAlign: 'center', boxShadow: '0 4px 24px rgba(0,0,0,0.08)' }}>
        <div style={{ width: '60px', height: '60px', borderRadius: '50%', background: ok ? 'rgba(16,185,129,0.12)' : 'rgba(248,113,113,0.12)', display: 'flex', alignItems: 'center', justifyContent: 'center', margin: '0 auto 18px', color: ok ? '#10b981' : '#f87171' }}>
          {ok ? <svg width="26" height="26" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round"><polyline points="20 6 9 17 4 12"/></svg>
               : <svg width="26" height="26" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round"><circle cx="12" cy="12" r="10"/><line x1="15" y1="9" x2="9" y2="15"/><line x1="9" y1="9" x2="15" y2="15"/></svg>}
        </div>
        <div style={{ fontSize: '1.15rem', fontWeight: '700', color: C.text, marginBottom: '8px' }}>{ok ? 'Analysis Complete' : 'Request Failed'}</div>
        <div style={{ fontSize: '0.8rem', color: C.textMuted }}>{ok ? 'Your request was processed successfully.' : 'Something went wrong. Please try again.'}</div>
        {intel.emailDelivery?.sent && <div style={{ marginTop: '12px', fontSize: '0.8rem', color: '#10b981' }}>Delivered to {intel.emailDelivery.to}</div>}
      </div>
    )
  }

  const { kpis, recommendations, watchlist, chartSecs } = intel

  // ── Adaptive layout from report_plan ──────────────────────────────────────
  const reportPlan  = intel.reportPlan || null
  const planStyle   = reportPlan?.report_style || 'analyst_deep_dive'
  const vizPref     = reportPlan?.visual_preference || 'balanced'

  // Chart visibility per style (0 = none, null = all)
  const chartLimits = {
    executive_brief: 0, kpi_summary: 0, anomaly_report: 0,
    visual_dashboard: null, analyst_deep_dive: null,
    table_heavy_report: 1, operational_report: 2, monitoring_report: 2,
  }
  const chartLimit   = Object.prototype.hasOwnProperty.call(chartLimits, planStyle) ? chartLimits[planStyle] : null
  const visibleCharts = chartLimit === 0 ? [] : chartLimit == null ? chartSecs : chartSecs.slice(0, chartLimit)

  // Layer ordering flags
  const chartsFirst   = planStyle === 'visual_dashboard'
  const insightFirst  = ['operational_report', 'anomaly_report', 'monitoring_report'].includes(planStyle)

  // KPI card min-width: kpi_summary gets wider cards for prominence
  const kpiMinWidth = planStyle === 'kpi_summary' ? '200px' : '160px'

  // ── Reusable layer fragments ──────────────────────────────────────────────
  const KpiLayer = kpis.length > 0 && (
    <div className="ws-section ws-s2">
      <div style={{ fontSize: '0.57rem', fontWeight: '800', color: C.textMuted, textTransform: 'uppercase', letterSpacing: '0.14em', marginBottom: '10px', display: 'flex', alignItems: 'center', gap: '6px' }}>
        <svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke={C.textMuted} strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round"><line x1="18" y1="20" x2="18" y2="10"/><line x1="12" y1="20" x2="12" y2="4"/><line x1="6" y1="20" x2="6" y2="14"/></svg>
        Key Performance Indicators
      </div>
      <div style={{ display: 'grid', gridTemplateColumns: `repeat(auto-fill, minmax(${kpiMinWidth}, 1fr))`, gap: '10px' }}>
        {kpis.map((kpi, i) => <KpiCard key={i} kpi={kpi} C={C} delay={0.04 + i * 0.04} />)}
      </div>
    </div>
  )

  const InsightLayer = (watchlist?.length > 0 || recommendations?.length > 0) && (
    <div className="ws-section ws-s3" style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(300px, 1fr))', gap: '14px' }}>
      {watchlist?.length > 0 && <WatchlistPanel watchlist={watchlist} C={C} />}
      {recommendations?.length > 0 && <PriorityInsights recommendations={recommendations} C={C} />}
    </div>
  )

  const ChartLayer = visibleCharts.length > 0 && (
    <div className="ws-section ws-s4">
      <div style={{ fontSize: '0.57rem', fontWeight: '800', color: C.textMuted, textTransform: 'uppercase', letterSpacing: '0.14em', marginBottom: '10px', display: 'flex', alignItems: 'center', gap: '6px' }}>
        <svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke={C.textMuted} strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round"><polyline points="22 12 18 12 15 21 9 3 6 12 2 12"/></svg>
        Visual Intelligence
        {vizPref === 'visual_heavy' && (
          <span style={{ fontSize: '0.54rem', color: C.textMuted, fontWeight: '500', marginLeft: '4px' }}>· visual-first layout</span>
        )}
      </div>
      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(340px, 1fr))', gap: '12px' }}>
        {visibleCharts.map((sec, i) => <ChartPanel key={i} sec={sec} C={C} delay={0.06 + i * 0.07} />)}
      </div>
    </div>
  )

  // ── Tab definitions (hide empty tabs) ────────────────────────────────────
  const tabs = [
    { id: 'overview',   label: 'Overview',   icon: 'home'  },
    kpis.length > 0          && { id: 'kpis',      label: 'KPIs',      icon: 'bar',   badge: kpis.length },
    visibleCharts.length > 0 && { id: 'visuals',   label: 'Visuals',   icon: 'chart', badge: visibleCharts.length },
    { id: 'execution',  label: 'Execution',  icon: 'clock' },
    { id: 'outputs',    label: 'Outputs',    icon: 'doc'   },
  ].filter(Boolean)

  function TabIcon({ type }) {
    const p = { width: 12, height: 12, viewBox: '0 0 24 24', fill: 'none', stroke: 'currentColor', strokeWidth: 2, strokeLinecap: 'round', strokeLinejoin: 'round' }
    if (type === 'home')  return <svg {...p}><path d="M3 9l9-7 9 7v11a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/><polyline points="9 22 9 12 15 12 15 22"/></svg>
    if (type === 'bar')   return <svg {...p}><line x1="18" y1="20" x2="18" y2="10"/><line x1="12" y1="20" x2="12" y2="4"/><line x1="6" y1="20" x2="6" y2="14"/></svg>
    if (type === 'chart') return <svg {...p}><polyline points="22 12 18 12 15 21 9 3 6 12 2 12"/></svg>
    if (type === 'clock') return <svg {...p}><circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 16 14"/></svg>
    if (type === 'doc')   return <svg {...p}><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/></svg>
    return null
  }

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: '12px' }}>

      {/* ── Executive hero — always visible ── */}
      <PremiumExecutiveHero intel={intel} C={C} onOpenReport={onOpenReport} setActiveNav={setActiveNav} />

      {/* ── Answer-first summary — direct response to user intent ── */}
      <AnswerSummaryBlock intel={intel} C={C} />

      {/* ── Tabbed dashboard card ── */}
      <div style={{ background: C.surface, border: `1px solid ${C.border}`, borderRadius: '16px', overflow: 'hidden', boxShadow: '0 2px 12px rgba(0,0,0,0.06)' }}>

        {/* Tab bar */}
        <div style={{ display: 'flex', alignItems: 'center', borderBottom: `1px solid ${C.border}`, background: C.bg, overflowX: 'auto' }}>
          {tabs.map(tab => {
            const on = activeTab === tab.id
            return (
              <button key={tab.id} onClick={() => setActiveTab(tab.id)} style={{
                display: 'flex', alignItems: 'center', gap: '6px',
                background: 'none', border: 'none',
                borderBottom: on ? '2px solid #6366f1' : '2px solid transparent',
                padding: '13px 16px 11px',
                fontSize: '0.75rem', fontWeight: on ? '700' : '500',
                color: on ? '#a5b4fc' : C.textMuted,
                cursor: 'pointer', fontFamily: FONT, whiteSpace: 'nowrap',
                transition: 'color 0.14s, border-color 0.14s',
              }}>
                <TabIcon type={tab.icon} />
                {tab.label}
                {tab.badge != null && (
                  <span style={{ fontSize: '0.57rem', fontWeight: '800', background: on ? 'rgba(99,102,241,0.18)' : 'rgba(255,255,255,0.06)', color: on ? '#a5b4fc' : C.textMuted, borderRadius: '10px', padding: '1px 6px', minWidth: '18px', textAlign: 'center' }}>
                    {tab.badge}
                  </span>
                )}
              </button>
            )
          })}

          {/* Compact Save Workflow CTA — right side of tab bar, always visible */}
          <div style={{ marginLeft: 'auto', display: 'flex', alignItems: 'center', padding: '0 16px', flexShrink: 0, borderLeft: `1px solid ${C.border}` }}>
            {workflowAlreadySaved ? (
              <div style={{ display: 'flex', alignItems: 'center', gap: '5px', fontSize: '0.68rem', fontWeight: '700', color: '#10b981' }}>
                <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round"><polyline points="20 6 9 17 4 12"/></svg>
                Saved to library
              </div>
            ) : workflowSaving ? (
              <div style={{ display: 'flex', alignItems: 'center', gap: '6px', fontSize: '0.68rem', color: '#a5b4fc' }}>
                <div style={{ width: '11px', height: '11px', borderRadius: '50%', border: '2px solid rgba(167,139,250,0.35)', borderTopColor: '#a5b4fc', animation: 'ws-spin 0.75s linear infinite', flexShrink: 0 }} />
                Saving…
              </div>
            ) : (
              <button onClick={onSaveWorkflow} style={{
                display: 'flex', alignItems: 'center', gap: '6px',
                background: 'rgba(99,102,241,0.10)', border: '1px solid rgba(99,102,241,0.25)',
                borderRadius: '8px', padding: '6px 12px',
                fontSize: '0.68rem', fontWeight: '700', color: '#a5b4fc',
                cursor: 'pointer', fontFamily: FONT, whiteSpace: 'nowrap',
                transition: 'background 0.14s',
              }}
                onMouseEnter={e => e.currentTarget.style.background = 'rgba(99,102,241,0.18)'}
                onMouseLeave={e => e.currentTarget.style.background = 'rgba(99,102,241,0.10)'}>
                <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round"><path d="M19 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11l5 5v11a2 2 0 0 1-2 2z"/><polyline points="17 21 17 13 7 13 7 21"/></svg>
                Save Workflow
              </button>
            )}
          </div>
        </div>

        {/* Tab content — key forces remount+fade on tab switch */}
        <div key={activeTab} style={{ padding: '22px', animation: 'ws-fadein 0.20s ease' }}>

          {/* ── Overview ── intent-adaptive content order ── */}
          {activeTab === 'overview' && (
            <div style={{ display: 'flex', flexDirection: 'column', gap: '14px' }}>
              {/* Dataset summary text — subtle info strip */}
              {intel.textSec?.items?.length > 0 && (
                <div style={{ display: 'flex', flexWrap: 'wrap', gap: '16px', padding: '9px 14px', background: C.bg, border: `1px solid ${C.border}`, borderRadius: '10px', fontSize: '0.71rem', color: C.textMuted, lineHeight: 1.5 }}>
                  {intel.textSec.items.slice(0, 2).map((item, i) => (
                    <span key={i}>{item}</span>
                  ))}
                </div>
              )}

              {/* kpi_summary: full KPI grid leads — all cards, wider layout */}
              {planStyle === 'kpi_summary' && kpis.length > 0 && (
                <div>
                  <div style={{ fontSize: '0.57rem', fontWeight: '800', color: C.textMuted, textTransform: 'uppercase', letterSpacing: '0.14em', marginBottom: '10px', display: 'flex', alignItems: 'center', gap: '6px' }}>
                    <svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke={C.textMuted} strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round"><line x1="18" y1="20" x2="18" y2="10"/><line x1="12" y1="20" x2="12" y2="4"/><line x1="6" y1="20" x2="6" y2="14"/></svg>
                    Business KPIs
                  </div>
                  <div style={{ display: 'grid', gridTemplateColumns: `repeat(auto-fill, minmax(${kpiMinWidth}, 1fr))`, gap: '10px' }}>
                    {kpis.map((kpi, i) => <KpiCard key={i} kpi={kpi} C={C} delay={0.03 + i * 0.04} />)}
                  </div>
                </div>
              )}

              {/* operational/anomaly/monitoring: risks and actions surface first */}
              {insightFirst && <AnomalyPanel sec={intel.anomalySec} C={C} />}
              {insightFirst && InsightLayer}

              {/* Default order: prioritized insights lead */}
              {!insightFirst && <InsightPriorityPanel sec={intel.insightPrioritySec} C={C} />}

              {/* KPI mini-grid — all styles except kpi_summary (already shown above) */}
              {planStyle !== 'kpi_summary' && kpis.length > 0 && (
                <div>
                  <div style={{ fontSize: '0.57rem', fontWeight: '800', color: C.textMuted, textTransform: 'uppercase', letterSpacing: '0.14em', marginBottom: '10px', display: 'flex', alignItems: 'center', gap: '6px' }}>
                    <svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke={C.textMuted} strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round"><line x1="18" y1="20" x2="18" y2="10"/><line x1="12" y1="20" x2="12" y2="4"/><line x1="6" y1="20" x2="6" y2="14"/></svg>
                    Key Metrics
                    {kpis.length > 4 && (
                      <button onClick={() => setActiveTab('kpis')} style={{ marginLeft: '4px', background: 'none', border: 'none', fontSize: '0.57rem', color: '#6366f1', cursor: 'pointer', fontFamily: FONT, fontWeight: '700', padding: 0 }}>
                        View all {kpis.length} →
                      </button>
                    )}
                  </div>
                  <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(148px, 1fr))', gap: '10px' }}>
                    {kpis.slice(0, 4).map((kpi, i) => <KpiCard key={i} kpi={kpi} C={C} delay={0.03 + i * 0.04} />)}
                  </div>
                </div>
              )}

              {/* Watchlist + recommendations — default position */}
              {!insightFirst && InsightLayer}

              {/* Anomalies — default position */}
              {!insightFirst && <AnomalyPanel sec={intel.anomalySec} C={C} />}

              {/* Insight priority — secondary for operational/anomaly styles */}
              {insightFirst && <InsightPriorityPanel sec={intel.insightPrioritySec} C={C} />}

              {/* Empty state — only when nothing at all to show */}
              {!kpis.length && !watchlist?.length && !recommendations?.length && !intel.insightPrioritySec && !intel.anomalySec && (
                <div style={{ textAlign: 'center', padding: '28px 0', color: C.textMuted, fontSize: '0.82rem' }}>
                  Analysis complete. Explore the KPIs, Visuals, and Execution tabs for details.
                </div>
              )}
            </div>
          )}

          {/* ── KPIs ── full grid + segmentation + drilldown */}
          {activeTab === 'kpis' && (
            <div style={{ display: 'flex', flexDirection: 'column', gap: '16px' }}>
              {KpiLayer || (
                <div style={{ textAlign: 'center', padding: '28px 0', color: C.textMuted, fontSize: '0.82rem' }}>No KPI data for this analysis.</div>
              )}
              <SegmentationPanel sec={intel.segmentationSec} C={C} />
              <DrilldownPanel sec={intel.drilldownSec} C={C} />
            </div>
          )}

          {/* ── Visuals ── charts + trends + forecast */}
          {activeTab === 'visuals' && (
            <div style={{ display: 'flex', flexDirection: 'column', gap: '16px' }}>
              {ChartLayer || (
                <div style={{ textAlign: 'center', padding: '28px 0', color: C.textMuted, fontSize: '0.82rem' }}>No chart visualizations for this report style.</div>
              )}
              <TrendPanel sec={intel.trendSec} C={C} />
              <ForecastPanel sec={intel.forecastSec} C={C} />
            </div>
          )}

          {/* ── Execution ── timeline + actions */}
          {activeTab === 'execution' && (
            <div style={{ display: 'flex', flexDirection: 'column', gap: '16px' }}>
              <WorkflowTimeline intel={intel} C={C} />
              <ActionsCompleted intel={intel} C={C} />
            </div>
          )}

          {/* ── Outputs ── generated files + delivery */}
          {activeTab === 'outputs' && (
            <div>
              <GeneratedOutputs intel={intel} C={C} onOpenReport={onOpenReport} setActiveNav={setActiveNav} />
            </div>
          )}
        </div>
      </div>
    </div>
  )
}

// WorkspaceLoading replaced by AIExecutionFlow (defined above with EXEC_PHASES)

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
      flex: 1,
      animation: 'ws-fadein 0.3s ease',
    }}>

      {/* ── Top bar: AI Assistant | Online ── */}
      <div style={{
        padding: '14px 18px',
        borderBottom: `1px solid rgba(30,43,82,0.18)`,
        display: 'flex', alignItems: 'center', gap: '8px',
      }}>
        <svg width="14" height="14" viewBox="0 0 24 24" fill="#a5b4fc">
          <path d="M12 2 L14 10 L22 12 L14 14 L12 22 L10 14 L2 12 L10 10 Z"/>
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
              <svg width="11" height="11" viewBox="0 0 24 24" fill="#a5b4fc"><path d="M12 2l2.4 7.4H22l-6.2 4.5 2.4 7.4L12 17l-6.2 4.3 2.4-7.4L2 9.4h7.6z"/></svg>
              <span style={{ fontSize: '0.6rem', fontWeight: '800', color: '#a5b4fc', textTransform: 'uppercase', letterSpacing: '0.1em' }}>AI Reasoning</span>
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
                ? 'Analysis prepared — additional AI reasoning not available for this request.'
                : 'Analysis prepared using standard rules.'}
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
            background: 'linear-gradient(135deg,#6366f1,#4f46e5)',
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

// ─── Engine Orchestration Plan components ─────────────────────────────────────

function slugToTitle(slug) {
  if (!slug) return 'Untitled Tool'
  return slug.split('_').map(w => w.charAt(0).toUpperCase() + w.slice(1)).join(' ')
}

function actionToLabel(action) {
  if (!action) return 'Step'
  const overrides = {
    fetch:                  'Load Data',
    generate_report:        'Generate Report',
    generate_dataset_report:'Generate Dataset Report',
    send_notification:      'Send Notification',
    send_email:             'Send Email',
    format_output:          'Format Output',
    run_analysis:           'Run Analysis',
    export_results:         'Export Results',
  }
  return overrides[action] || action.split('_').map(w => w.charAt(0).toUpperCase() + w.slice(1)).join(' ')
}

function inputFriendlyLabel(name) {
  if (name === 'dataset_id')  return 'Dataset'
  if (name === 'report_id')   return 'Report'
  if (name === 'user_id')     return 'User'
  if (name === 'email')       return 'Email address'
  if (name === 'recipient')   return 'Recipient'
  return name.replace(/_id$/, '').split('_').map(w => w.charAt(0).toUpperCase() + w.slice(1)).join(' ')
}

const ACTION_TYPE_META = {
  fetch:                  { color: '#38bdf8', label: 'Load Data'       },
  transform:              { color: '#a5b4fc', label: 'Transform'       },
  filter:                 { color: '#fbbf24', label: 'Filter'          },
  aggregate:              { color: '#10b981', label: 'Aggregate'       },
  notify:                 { color: '#f472b6', label: 'Notify'          },
  send_email:             { color: '#f472b6', label: 'Send Email'      },
  send_notification:      { color: '#f472b6', label: 'Notify'          },
  compute:                { color: '#60a5fa', label: 'Compute'         },
  generate_report:        { color: '#f59e0b', label: 'Generate Report' },
  generate_dataset_report:{ color: '#f59e0b', label: 'Generate Report' },
  validate:               { color: '#f87171', label: 'Validate'        },
  analyze:                { color: '#818cf8', label: 'Analyze'         },
  format_output:          { color: '#a5b4fc', label: 'Format Output'   },
  export_results:         { color: '#10b981', label: 'Export'          },
}

const ENGINE_STATUS_META = {
  draft:            { color: '#94a3b8', label: 'Draft'            },
  pending_approval: { color: '#fbbf24', label: 'Pending Approval' },
  approved:         { color: '#22c55e', label: 'Approved'         },
  deprecated:       { color: '#f87171', label: 'Deprecated'       },
}

function ActionTypeBadge({ type }) {
  const meta = type && ACTION_TYPE_META[type]
    ? ACTION_TYPE_META[type]
    : { color: '#94a3b8', label: actionToLabel(type) }
  return (
    <span style={{
      display: 'inline-block', padding: '2px 9px', borderRadius: '5px',
      fontSize: '0.64rem', fontWeight: '700', letterSpacing: '0.01em',
      fontFamily: FONT, whiteSpace: 'nowrap',
      background: `${meta.color}18`, color: meta.color, border: `1px solid ${meta.color}40`,
      flexShrink: 0,
    }}>{meta.label}</span>
  )
}

function Toast({ toast }) {
  if (!toast) return null
  return (
    <div style={{
      position: 'fixed', bottom: '24px', right: '28px', zIndex: 9999,
      display: 'flex', alignItems: 'center', gap: '10px',
      background: toast.ok ? 'rgba(16,185,129,0.97)' : 'rgba(248,113,113,0.97)',
      border: `1px solid ${toast.ok ? '#10b981' : '#f87171'}`,
      borderRadius: '12px', padding: '12px 18px',
      boxShadow: '0 8px 32px rgba(0,0,0,0.24)', animation: 'ws-fadeup 0.25s ease',
      maxWidth: '400px',
    }}>
      {toast.ok
        ? <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="#fff" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round"><polyline points="20 6 9 17 4 12"/></svg>
        : <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="#fff" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round"><circle cx="12" cy="12" r="10"/><line x1="12" y1="8" x2="12" y2="12"/><line x1="12" y1="16" x2="12.01" y2="16"/></svg>
      }
      <span style={{ fontSize: '0.8rem', fontWeight: '600', color: '#fff', fontFamily: FONT }}>{toast.text}</span>
    </div>
  )
}

function EngineOrchestrationPlan({
  plan, C, savedToolId, toolStatus,
  engineBusy, onSave, onSubmit, onApprove,
  onEdit, onClear, showRawJson, setShowRawJson,
  datasetList, planDatasetId, setPlanDatasetId,
}) {
  const nodes      = plan?.graph?.nodes ?? []
  const allInputs  = plan?.inputs ?? []
  const reqInputs  = allInputs.filter(i => i.required)
  const optInputs  = allInputs.filter(i => !i.required)
  const canSave    = !!plan && !savedToolId
  const canSubmit  = !!savedToolId && toolStatus === 'draft'
  const canApprove = !!savedToolId && toolStatus === 'pending_approval'
  const statusKey  = toolStatus ?? plan?.status ?? 'draft'
  const statusMeta = ENGINE_STATUS_META[statusKey] || ENGINE_STATUS_META.draft

  // Derive user-friendly title — prefer description over slug
  const internalName = plan?.name || ''
  const descTitle = plan?.description
    ? plan.description.length <= 64
      ? plan.description
      : (plan.description.split(/[.!?]/)[0].trim() || slugToTitle(internalName))
    : null
  const displayTitle = descTitle || slugToTitle(internalName)

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: '14px', animation: 'ws-fadein 0.35s ease' }}>

      {/* ── Back to Composer nav ── */}
      <div style={{ display: 'flex', alignItems: 'center', gap: '10px' }}>
        <button className="ws-ghost-btn" onClick={onEdit}
          style={{ display: 'flex', alignItems: 'center', gap: '7px', background: C.bg, border: `1px solid ${C.border}`, borderRadius: '10px', padding: '8px 16px', fontSize: '0.78rem', fontWeight: '600', color: C.textSec, cursor: 'pointer', fontFamily: FONT }}>
          <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round"><line x1="19" y1="12" x2="5" y2="12"/><polyline points="12 19 5 12 12 5"/></svg>
          Back to Composer
        </button>
        <span style={{ fontSize: '0.72rem', color: C.textMuted, opacity: 0.4 }}>›</span>
        <span style={{ fontSize: '0.78rem', fontWeight: '500', color: C.textSec }}>Orchestration Plan</span>
      </div>

      {/* ── Plan header card ── */}
      <div style={{ background: C.surface, border: '1px solid rgba(99,102,241,0.25)', borderRadius: '18px', overflow: 'hidden', boxShadow: '0 8px 40px rgba(0,0,0,0.12)' }}>
        <div style={{ height: '3px', background: 'linear-gradient(90deg,#4f46e5,#6366f1,#a5b4fc,#60a5fa)' }} />
        <div style={{ padding: '24px 28px 22px', position: 'relative' }}>
          <div style={{ position: 'absolute', top: 0, right: 0, width: '280px', height: '180px', background: 'radial-gradient(ellipse at 90% 0%, rgba(99,102,241,0.09) 0%, transparent 70%)', pointerEvents: 'none' }} />

          {/* Title + lifecycle buttons */}
          <div style={{ display: 'flex', alignItems: 'flex-start', justifyContent: 'space-between', gap: '16px', flexWrap: 'wrap', marginBottom: '20px' }}>
            <div style={{ flex: 1, minWidth: 0 }}>
              <div style={{ display: 'flex', alignItems: 'center', gap: '8px', marginBottom: '12px', flexWrap: 'wrap' }}>
                <div style={{ display: 'flex', alignItems: 'center', gap: '6px', background: 'rgba(99,102,241,0.12)', border: '1px solid rgba(99,102,241,0.25)', borderRadius: '20px', padding: '4px 11px 4px 8px' }}>
                  <svg width="10" height="10" viewBox="0 0 24 24" fill="#a5b4fc"><path d="M12 2l2.4 7.4H22l-6.2 4.5 2.4 7.4L12 17l-6.2 4.3 2.4-7.4L2 9.4h7.6z"/></svg>
                  <span style={{ fontSize: '0.55rem', fontWeight: '800', color: '#a5b4fc', textTransform: 'uppercase', letterSpacing: '0.16em' }}>Orchestration Plan</span>
                </div>
                <span style={{ display: 'inline-flex', alignItems: 'center', gap: '5px', background: `${statusMeta.color}18`, border: `1px solid ${statusMeta.color}40`, borderRadius: '20px', padding: '3px 10px', fontSize: '0.57rem', fontWeight: '700', color: statusMeta.color, textTransform: 'uppercase', letterSpacing: '0.10em' }}>
                  <span style={{ width: '5px', height: '5px', borderRadius: '50%', background: statusMeta.color, flexShrink: 0 }} />
                  {statusMeta.label}
                </span>
              </div>

              {/* User-friendly title derived from description or slug */}
              <h2 style={{ margin: '0 0 8px', fontSize: '1.4rem', fontWeight: '800', color: C.text, letterSpacing: '-0.5px', lineHeight: 1.2 }}>
                {displayTitle}
              </h2>
              {/* Show description as subtitle only if it differs from the derived title */}
              {plan?.description && plan.description !== displayTitle && (
                <p style={{ margin: 0, fontSize: '0.84rem', color: C.textSec, lineHeight: 1.72, maxWidth: '560px' }}>{plan.description}</p>
              )}
            </div>

            {/* Lifecycle action buttons (right column) */}
            <div style={{ display: 'flex', flexDirection: 'column', gap: '8px', alignItems: 'flex-end', flexShrink: 0 }}>
              <div style={{ display: 'flex', gap: '8px', flexWrap: 'wrap', justifyContent: 'flex-end' }}>
                {canSave && (
                  <button className="ws-action-btn" onClick={onSave} disabled={engineBusy === 'save'}
                    style={{ display: 'flex', alignItems: 'center', gap: '7px', background: 'linear-gradient(135deg,#1e3a5f,#1e40af)', border: 'none', borderRadius: '10px', padding: '8px 16px', fontSize: '0.76rem', fontWeight: '700', color: '#fff', cursor: engineBusy === 'save' ? 'not-allowed' : 'pointer', fontFamily: FONT, boxShadow: '0 4px 14px rgba(30,64,175,0.35)', opacity: engineBusy === 'save' ? 0.65 : 1 }}>
                    {engineBusy === 'save'
                      ? <div style={{ width: '10px', height: '10px', borderRadius: '50%', border: '2px solid rgba(255,255,255,0.35)', borderTopColor: '#fff', animation: 'ws-spin 0.75s linear infinite' }} />
                      : <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round"><path d="M19 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11l5 5v11a2 2 0 0 1-2 2z"/><polyline points="17 21 17 13 7 13 7 21"/><polyline points="7 3 7 8 15 8"/></svg>}
                    Save Tool
                  </button>
                )}
                {canSubmit && (
                  <button className="ws-action-btn" onClick={onSubmit} disabled={engineBusy === 'submit'}
                    style={{ display: 'flex', alignItems: 'center', gap: '7px', background: 'linear-gradient(135deg,#78350f,#b45309)', border: 'none', borderRadius: '10px', padding: '8px 16px', fontSize: '0.76rem', fontWeight: '700', color: '#fff', cursor: engineBusy === 'submit' ? 'not-allowed' : 'pointer', fontFamily: FONT, boxShadow: '0 4px 14px rgba(120,53,15,0.35)', opacity: engineBusy === 'submit' ? 0.65 : 1 }}>
                    {engineBusy === 'submit'
                      ? <div style={{ width: '10px', height: '10px', borderRadius: '50%', border: '2px solid rgba(255,255,255,0.35)', borderTopColor: '#fff', animation: 'ws-spin 0.75s linear infinite' }} />
                      : <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round"><line x1="22" y1="2" x2="11" y2="13"/><polygon points="22 2 15 22 11 13 2 9 22 2"/></svg>}
                    Submit for Approval
                  </button>
                )}
                {canApprove && (
                  <button className="ws-action-btn" onClick={onApprove} disabled={engineBusy === 'approve'}
                    style={{ display: 'flex', alignItems: 'center', gap: '7px', background: 'linear-gradient(135deg,#14532d,#166534)', border: 'none', borderRadius: '10px', padding: '8px 16px', fontSize: '0.76rem', fontWeight: '700', color: '#fff', cursor: engineBusy === 'approve' ? 'not-allowed' : 'pointer', fontFamily: FONT, boxShadow: '0 4px 14px rgba(20,83,45,0.35)', opacity: engineBusy === 'approve' ? 0.65 : 1 }}>
                    {engineBusy === 'approve'
                      ? <div style={{ width: '10px', height: '10px', borderRadius: '50%', border: '2px solid rgba(255,255,255,0.35)', borderTopColor: '#fff', animation: 'ws-spin 0.75s linear infinite' }} />
                      : <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round"><polyline points="20 6 9 17 4 12"/></svg>}
                    Approve
                  </button>
                )}
              </div>
            </div>
          </div>

          {/* ── Required inputs as business-friendly prompts ── */}
          {reqInputs.length > 0 && (
            <div style={{ display: 'flex', flexDirection: 'column', gap: '10px', marginBottom: optInputs.length > 0 ? '10px' : 0 }}>
              {reqInputs.map(inp => {
                const label = inputFriendlyLabel(inp.name)
                const isDataset = inp.name === 'dataset_id'

                if (isDataset) {
                  const activeDs = datasetList?.find(d => d.id === planDatasetId) || null
                  return (
                    <div key={inp.name} style={{ padding: '13px 16px', background: 'rgba(251,191,36,0.06)', border: '1px solid rgba(251,191,36,0.22)', borderRadius: '12px', display: 'flex', alignItems: 'center', gap: '12px', flexWrap: 'wrap' }}>
                      <div style={{ display: 'flex', alignItems: 'center', gap: '7px', flexShrink: 0 }}>
                        <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="#fbbf24" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><ellipse cx="12" cy="5" rx="9" ry="3"/><path d="M21 12c0 1.66-4 3-9 3s-9-1.34-9-3"/><path d="M3 5v14c0 1.66 4 3 9 3s9-1.34 9-3V5"/></svg>
                        <span style={{ fontSize: '0.8rem', fontWeight: '700', color: '#fbbf24' }}>Dataset required</span>
                        <span style={{ fontSize: '0.76rem', color: C.textSec }}>— choose a dataset to use</span>
                      </div>
                      <div style={{ display: 'flex', alignItems: 'center', gap: '8px', flex: 1, minWidth: '200px', flexWrap: 'wrap' }}>
                        {!datasetList?.length ? (
                          <span style={{ fontSize: '0.74rem', color: C.textMuted }}>No datasets available — upload one first.</span>
                        ) : (
                          <select value={planDatasetId || ''} onChange={e => setPlanDatasetId(e.target.value || null)}
                            style={{ flex: 1, minWidth: '200px', background: C.bg, border: `1px solid ${planDatasetId ? 'rgba(251,191,36,0.45)' : C.border}`, borderRadius: '8px', padding: '7px 12px', fontSize: '0.78rem', color: planDatasetId ? C.text : C.textMuted, fontFamily: FONT, outline: 'none', cursor: 'pointer' }}>
                            <option value="">Choose a dataset…</option>
                            {datasetList.map(ds => <option key={ds.id} value={ds.id}>{ds.filename}</option>)}
                          </select>
                        )}
                        {activeDs && (
                          <span style={{ fontSize: '0.7rem', color: '#10b981', display: 'flex', alignItems: 'center', gap: '4px', flexShrink: 0, fontWeight: '600' }}>
                            <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round"><polyline points="20 6 9 17 4 12"/></svg>
                            {activeDs.filename}
                          </span>
                        )}
                      </div>
                    </div>
                  )
                }

                return (
                  <div key={inp.name} style={{ padding: '11px 16px', background: 'rgba(251,191,36,0.04)', border: '1px solid rgba(251,191,36,0.18)', borderRadius: '10px', display: 'flex', alignItems: 'center', gap: '10px' }}>
                    <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="#fbbf24" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" style={{ flexShrink: 0 }}><circle cx="12" cy="12" r="10"/><line x1="12" y1="8" x2="12" y2="12"/><line x1="12" y1="16" x2="12.01" y2="16"/></svg>
                    <span style={{ fontSize: '0.8rem', color: C.textSec }}>
                      <span style={{ fontWeight: '700', color: '#fbbf24' }}>{label}</span> required
                      {inp.description ? ` — ${inp.description}` : ''}
                    </span>
                  </div>
                )
              })}
            </div>
          )}

          {/* Optional inputs — shown as compact summary */}
          {optInputs.length > 0 && (
            <div style={{ display: 'flex', alignItems: 'center', gap: '7px', flexWrap: 'wrap' }}>
              <span style={{ fontSize: '0.62rem', fontWeight: '600', color: C.textMuted, textTransform: 'uppercase', letterSpacing: '0.08em', flexShrink: 0 }}>Optional</span>
              {optInputs.map(inp => (
                <span key={inp.name} style={{ fontSize: '0.74rem', color: C.textMuted, background: C.bg, border: `1px solid ${C.border}`, borderRadius: '6px', padding: '2px 9px' }}>
                  {inputFriendlyLabel(inp.name)}
                </span>
              ))}
            </div>
          )}
        </div>
      </div>

      {/* ── Workflow steps ── */}
      {nodes.length > 0 && (
        <div style={{ background: C.surface, border: `1px solid ${C.border}`, borderRadius: '18px', overflow: 'hidden', boxShadow: '0 4px 20px rgba(0,0,0,0.08)' }}>
          <div style={{ padding: '14px 24px', borderBottom: `1px solid ${C.border}`, display: 'flex', alignItems: 'center', gap: '9px', background: 'rgba(99,102,241,0.04)' }}>
            <div style={{ width: '24px', height: '24px', borderRadius: '7px', background: 'rgba(99,102,241,0.15)', display: 'flex', alignItems: 'center', justifyContent: 'center' }}>
              <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="#6366f1" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round"><circle cx="18" cy="5" r="3"/><circle cx="6" cy="12" r="3"/><circle cx="18" cy="19" r="3"/><line x1="8.59" y1="13.51" x2="15.42" y2="17.49"/><line x1="15.41" y1="6.51" x2="8.59" y2="10.49"/></svg>
            </div>
            <span style={{ fontSize: '0.65rem', fontWeight: '800', color: '#a5b4fc', textTransform: 'uppercase', letterSpacing: '0.12em' }}>Workflow Steps</span>
            <span style={{ marginLeft: 'auto', fontSize: '0.65rem', color: C.textMuted, background: C.bg, borderRadius: '10px', padding: '1px 8px', border: `1px solid ${C.border}` }}>
              {nodes.length} step{nodes.length !== 1 ? 's' : ''}
            </span>
          </div>
          <div style={{ padding: '20px 24px', display: 'flex', flexDirection: 'column' }}>
            {nodes.map((node, i) => {
              const isLast = i === nodes.length - 1
              const nodeTitle = node.label || node.name || actionToLabel(node.action_type)
              return (
                <div key={node.id ?? node.node_id ?? i} style={{ display: 'flex', alignItems: 'stretch' }}>
                  {/* Step number + connector */}
                  <div style={{ display: 'flex', flexDirection: 'column', alignItems: 'center', width: '40px', flexShrink: 0, paddingTop: '10px' }}>
                    <div style={{ width: '28px', height: '28px', borderRadius: '50%', background: 'linear-gradient(135deg,rgba(99,102,241,0.18),rgba(139,92,246,0.10))', border: '1.5px solid rgba(99,102,241,0.38)', display: 'flex', alignItems: 'center', justifyContent: 'center', flexShrink: 0, zIndex: 1 }}>
                      <span style={{ fontSize: '0.67rem', fontWeight: '800', color: '#a5b4fc' }}>{i + 1}</span>
                    </div>
                    {!isLast && (
                      <div style={{ width: '2px', flex: 1, background: 'linear-gradient(180deg,rgba(99,102,241,0.28),rgba(99,102,241,0.06))', borderRadius: '1px', minHeight: '12px', marginTop: '4px' }} />
                    )}
                  </div>
                  {/* Node card — business labels only, internals hidden */}
                  <div style={{ flex: 1, marginLeft: '12px', marginBottom: isLast ? '0' : '8px', padding: '13px 16px', borderRadius: '12px', background: C.bg, border: `1px solid ${C.border}`, transition: 'border-color 0.14s, background 0.14s' }}
                    onMouseEnter={e => { e.currentTarget.style.borderColor = 'rgba(99,102,241,0.28)'; e.currentTarget.style.background = 'rgba(99,102,241,0.03)' }}
                    onMouseLeave={e => { e.currentTarget.style.borderColor = C.border; e.currentTarget.style.background = C.bg }}>
                    <div style={{ display: 'flex', alignItems: 'center', gap: '8px', marginBottom: node.description ? '6px' : 0, flexWrap: 'wrap' }}>
                      <ActionTypeBadge type={node.action_type} />
                      <span style={{ fontSize: '0.84rem', fontWeight: '600', color: C.text, flex: 1, minWidth: 0 }}>
                        {nodeTitle}
                      </span>
                    </div>
                    {node.description && (
                      <p style={{ margin: 0, fontSize: '0.76rem', color: C.textSec, lineHeight: 1.6 }}>{node.description}</p>
                    )}
                  </div>
                </div>
              )
            })}
          </div>
        </div>
      )}

      {/* ── Developer details — visually secondary, collapsed by default ── */}
      <div style={{ border: `1px solid ${C.border}`, borderRadius: '10px', overflow: 'hidden', opacity: 0.72 }}>
        <button onClick={() => setShowRawJson(p => !p)}
          style={{ width: '100%', background: 'none', border: 'none', padding: '9px 16px', display: 'flex', alignItems: 'center', gap: '8px', cursor: 'pointer', fontFamily: FONT, outline: 'none' }}>
          <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke={C.textMuted} strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
            {showRawJson ? <polyline points="18 15 12 9 6 15" /> : <polyline points="6 9 12 15 18 9" />}
          </svg>
          <span style={{ fontSize: '0.62rem', fontWeight: '600', color: C.textMuted, letterSpacing: '0.04em' }}>Developer details</span>
          <span style={{ marginLeft: 'auto', fontSize: '0.6rem', color: C.textMuted, opacity: 0.55 }}>internal name · raw schema · node IDs</span>
        </button>
        {showRawJson && (
          <div style={{ borderTop: `1px solid ${C.border}`, padding: '14px 16px', background: C.bg, display: 'flex', flexDirection: 'column', gap: '10px' }}>
            {/* Internal identifiers */}
            <div style={{ display: 'flex', flexWrap: 'wrap', gap: '10px', alignItems: 'center' }}>
              <div style={{ display: 'flex', alignItems: 'center', gap: '6px' }}>
                <span style={{ fontSize: '0.62rem', fontWeight: '600', color: C.textMuted }}>Internal name</span>
                <code style={{ fontFamily: MONO, fontSize: '0.68rem', color: C.textSec, background: C.surface, border: `1px solid ${C.border}`, borderRadius: '5px', padding: '2px 7px' }}>{internalName || '—'}</code>
              </div>
              {savedToolId && (
                <div style={{ display: 'flex', alignItems: 'center', gap: '6px' }}>
                  <span style={{ fontSize: '0.62rem', fontWeight: '600', color: C.textMuted }}>Tool ID</span>
                  <code style={{ fontFamily: MONO, fontSize: '0.68rem', color: C.textSec, background: C.surface, border: `1px solid ${C.border}`, borderRadius: '5px', padding: '2px 7px' }}>{savedToolId}</code>
                </div>
              )}
            </div>
            {/* Raw JSON schema */}
            <pre style={{ margin: 0, fontFamily: MONO, fontSize: '0.69rem', color: C.textSec, background: C.surface, border: `1px solid ${C.border}`, borderRadius: '7px', padding: '12px', overflow: 'auto', maxHeight: '300px', whiteSpace: 'pre' }}>
              {JSON.stringify(plan, null, 2)}
            </pre>
          </div>
        )}
      </div>
    </div>
  )
}

// ─── Contextual Intelligence Strip ───────────────────────────────────────────

function ContextualIntelligenceStrip({ workflowCount, nextScheduledAt, recentExecution, alertCount, suggestedAction, C, onNavigate }) {
  function fmtNext(ts) {
    if (!ts) return 'None scheduled'
    const diff = new Date(ts) - Date.now()
    if (diff < 0) return 'Overdue'
    const h = Math.floor(diff / 3600000)
    if (h < 1) return '< 1h'
    if (h < 24) return `${h}h`
    return `${Math.floor(h / 24)}d`
  }

  const recentOk = recentExecution
    ? (recentExecution.status === 'success' || recentExecution.status === 'completed')
    : null

  const cards = [
    {
      label: 'Active Workflows',
      value: workflowCount ?? 0,
      sub: 'saved',
      color: '#a5b4fc',
      nav: 'workflows',
      icon: <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><circle cx="18" cy="5" r="3"/><circle cx="6" cy="12" r="3"/><circle cx="18" cy="19" r="3"/><line x1="8.59" y1="13.51" x2="15.42" y2="17.49"/><line x1="15.41" y1="6.51" x2="8.59" y2="10.49"/></svg>,
    },
    {
      label: 'Next Scheduled',
      value: fmtNext(nextScheduledAt),
      sub: 'automation run',
      color: '#38bdf8',
      nav: 'scheduled',
      icon: <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><rect x="3" y="4" width="18" height="18" rx="2"/><line x1="16" y1="2" x2="16" y2="6"/><line x1="8" y1="2" x2="8" y2="6"/><line x1="3" y1="10" x2="21" y2="10"/></svg>,
    },
    {
      label: 'Recent Execution',
      value: recentOk === null ? 'None yet' : recentOk ? 'Success' : 'Failed',
      sub: recentExecution?.started_at
        ? new Date(recentExecution.started_at).toLocaleString(undefined, { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' })
        : '—',
      color: recentOk === null ? C.textMuted : recentOk ? '#10b981' : '#f87171',
      nav: 'history',
      icon: <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><polygon points="5 3 19 12 5 21 5 3"/></svg>,
    },
    {
      label: 'Alerts',
      value: alertCount ?? 0,
      sub: (alertCount ?? 0) > 0 ? 'unread' : 'all clear',
      color: (alertCount ?? 0) > 0 ? '#f59e0b' : '#10b981',
      nav: 'operations',
      icon: <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M18 8A6 6 0 0 0 6 8c0 7-3 9-3 9h18s-3-2-3-9"/><path d="M13.73 21a2 2 0 0 1-3.46 0"/></svg>,
    },
    (() => {
      const hasAlerts = (alertCount ?? 0) > 0
      const lastFailed = recentOk === false
      const status = (hasAlerts || lastFailed) ? 'Warning' : 'Healthy'
      const color  = (hasAlerts || lastFailed) ? '#f59e0b' : '#10b981'
      const sub    = hasAlerts ? `${alertCount} alert${alertCount !== 1 ? 's' : ''} active` : lastFailed ? 'Last run failed' : 'All systems normal'
      return {
        label: 'Operational Health',
        value: status,
        sub,
        color,
        nav: 'operations',
        icon: <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/></svg>,
      }
    })(),
  ]

  return (
    <div style={{ display: 'flex', gap: '8px' }}>
      {cards.map((card, i) => (
        <div key={i}
          onClick={() => card.nav && onNavigate?.(card.nav)}
          style={{
            flex: 1, minWidth: 0,
            background: C.surface, border: `1px solid ${C.border}`,
            borderRadius: '10px', padding: '8px 10px',
            display: 'flex', flexDirection: 'column', gap: '4px',
            cursor: card.nav ? 'pointer' : 'default',
            transition: 'border-color 0.15s, transform 0.15s',
          }}
          onMouseEnter={e => { if (card.nav) { e.currentTarget.style.borderColor = card.color; e.currentTarget.style.transform = 'translateY(-1px)' } }}
          onMouseLeave={e => { if (card.nav) { e.currentTarget.style.borderColor = C.border; e.currentTarget.style.transform = 'translateY(0)' } }}
        >
          {/* Label left, icon right */}
          <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
            <span style={{ fontSize: '0.6rem', fontWeight: '600', color: card.color, textTransform: 'uppercase', letterSpacing: '0.07em', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{card.label}</span>
            <span style={{ color: card.color, flexShrink: 0, opacity: 0.8 }}>{card.icon}</span>
          </div>
          {/* Large value */}
          <div style={{ fontSize: '1.05rem', fontWeight: '600', color: card.color, letterSpacing: '-0.3px', lineHeight: 1, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{card.value}</div>
          {/* Subtitle */}
          <div style={{ fontSize: '0.6rem', color: C.textMuted, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{card.sub}</div>
        </div>
      ))}
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
  datasetExplicit, setDatasetExplicit,
  externalResult, externalLoading, externalError,
  setActiveNav, onOpenReport, onExportReport,
  onUploadDataset,
  contextStats,
}) {
  const fileInputRef    = useRef(null)
  const execStartedAtRef = useRef(null)

  const [wsInput,           setWsInput]           = useState('')
  const [wsLoading,         setWsLoading]         = useState(false)
  const [wsResult,          setWsResult]          = useState(null)
  const [wsExecDurationMs,  setWsExecDurationMs]  = useState(null)
  const [wsError,           setWsError]           = useState(null)
  const [wsProposal,        setWsProposal]        = useState(null)
  const [wsProposalLoading, setWsProposalLoading] = useState(false)
  const [wsProposalError,   setWsProposalError]   = useState(null)
  const [wsEmail,           setWsEmail]           = useState('')
  const [dsPicker,          setDsPicker]          = useState(false)
  const [enginePlan,        setEnginePlan]        = useState(null)
  const [enginePlanLoading, setEnginePlanLoading] = useState(false)
  const [savedToolId,       setSavedToolId]       = useState(null)
  const [toolStatus,        setToolStatus]        = useState(null)
  const [engineBusy,        setEngineBusy]        = useState(null)
  const [toast,             setToast]             = useState(null)
  const [showRawJson,       setShowRawJson]       = useState(false)
  const [planDatasetId,     setPlanDatasetId]     = useState(null)
  const [backToComposer,    setBackToComposer]    = useState(false)
  const [savedWorkflows,    setSavedWorkflows]    = useState([])
  const [engineTools,       setEngineTools]       = useState([])
  const [workflowSaving,    setWorkflowSaving]    = useState(false)
  const [wsRunSource,       setWsRunSource]       = useState(null) // null | workflow title string
  const [dsPendingRun,      setDsPendingRun]      = useState(null) // pending run args awaiting dataset confirmation
  const [noDsWarning,       setNoDsWarning]       = useState(false)
  const [dsSearch,          setDsSearch]          = useState('')

  useEffect(() => { if (externalLoading) { setWsResult(null); setWsError(null) } }, [externalLoading])
  useEffect(() => { if (externalResult) setWsResult(externalResult) }, [externalResult])
  useEffect(() => { if (externalError) setWsError(externalError) }, [externalError])
  useEffect(() => { if (token) loadEngineTools() }, [token])
  useEffect(() => { if (!dsPicker) setDsSearch('') }, [dsPicker])

  const activeResult  = wsResult
  const activeLoading = wsLoading || externalLoading
  const activeError   = wsError
  const intel         = extractIntel(activeResult)
  const hasResult     = !!activeResult && !activeLoading
  const showComposer  = !activeLoading && (backToComposer || (!hasResult && !wsProposal && !enginePlan))
  const activeDs      = datasetList?.find(d => d.id === selectedDatasetId) || null
  const isEmailIntent = ['email', 'send', 'mail'].some(kw => wsInput.toLowerCase().includes(kw))
  const disabled             = wsLoading || wsProposalLoading || enginePlanLoading || !wsInput.trim()
  const workflowAlreadySaved = wsInput.trim().length > 0 && savedWorkflows.some(wf => wf.intent === wsInput.trim())

  // Merge session-saved workflows (rich metadata) with backend engine tools loaded on mount.
  // Session entries take priority — they carry the original intent string.
  const sessionToolIds = new Set(savedWorkflows.map(w => w.toolId).filter(Boolean))
  const allWorkflows = [
    ...savedWorkflows,
    ...engineTools
      .filter(t => t.status !== 'deprecated' && !sessionToolIds.has(t.tool_id))
      .map(t => ({
        id:           t.tool_id,
        toolId:       t.tool_id,
        title:        t.description || slugToTitle(t.name) || t.name || 'Saved Workflow',
        intent:       t.description || t.name || '',
        datasetId:    null,
        datasetName:  null,
        createdAt:    t.created_at || new Date().toISOString(),
        backendSaved: true,
      })),
  ]

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

  function showToast(text, ok = true) {
    setToast({ text, ok })
    setTimeout(() => setToast(null), 4500)
  }

  async function handleEnginePlan() {
    const trimmed = wsInput.trim()
    if (!trimmed || trimmed.length < 5) { setWsProposalError('Please enter a task description (at least 5 characters).'); return }
    setWsProposalError(null); setEnginePlan(null); setWsProposal(null)
    setSavedToolId(null); setToolStatus(null); setShowRawJson(false)
    setPlanDatasetId(selectedDatasetId || null)
    setEnginePlanLoading(true)
    try {
      const data = await planEngineTool(trimmed, token)
      const plan = data?.data ?? data
      setEnginePlan(plan)
      showToast(`Orchestration plan ready — "${plan?.name || 'Tool'}"`)
    } catch (err) {
      if (err?.message?.startsWith('401:')) { onSessionExpired(); return }
      // Fallback to compose flow
      try {
        const fallback = await composeIntent(trimmed, selectedDatasetId || null, token, true)
        setWsProposal(fallback?.data ?? null)
        showToast('Using smart compose fallback.')
      } catch (fbErr) {
        if (fbErr?.message?.startsWith('401:')) { onSessionExpired(); return }
        const msg = fbErr.message?.replace(/^\d+:\s*/, '') || 'Failed to generate plan.'
        setWsProposalError(msg)
        showToast(msg, false)
      }
    } finally { setEnginePlanLoading(false) }
  }

  async function handleSaveTool() {
    if (!enginePlan) return
    setEngineBusy('save')
    try {
      const scheduleType = extractScheduleType(wsInput)
      const planToSave   = scheduleType
        ? { ...enginePlan, schedule: { enabled: true, schedule_type: scheduleType, timezone: 'UTC', cron: '' } }
        : enginePlan
      const res = await saveEngineTool(planToSave, token)
      const d = res?.data ?? res
      setSavedToolId(d.tool_id)
      setToolStatus(d.status ?? 'draft')
      showToast(`Tool saved — ${(d.tool_id || '').slice(0, 8)}…`)
    } catch (err) {
      if (err?.message?.startsWith('401:')) { onSessionExpired(); return }
      showToast(err.message?.replace(/^\d+:\s*/, '') || 'Failed to save tool.', false)
    } finally { setEngineBusy(null) }
  }

  async function handleSubmitTool() {
    if (!savedToolId) return
    setEngineBusy('submit')
    try {
      await submitEngineTool(savedToolId, token)
      setToolStatus('pending_approval')
      showToast('Tool submitted for approval.')
    } catch (err) {
      if (err?.message?.startsWith('401:')) { onSessionExpired(); return }
      showToast(err.message?.replace(/^\d+:\s*/, '') || 'Failed to submit.', false)
    } finally { setEngineBusy(null) }
  }

  async function handleApproveTool() {
    if (!savedToolId) return
    setEngineBusy('approve')
    try {
      await approveEngineTool(savedToolId, token)
      setToolStatus('approved')
      showToast('Tool approved — ready for execution.')
    } catch (err) {
      if (err?.message?.startsWith('401:')) { onSessionExpired(); return }
      showToast(err.message?.replace(/^\d+:\s*/, '') || 'Failed to approve.', false)
    } finally { setEngineBusy(null) }
  }

  async function handleRun(sections = null, proposal = null, overrideIntent = null, overrideDsId = undefined, _skipDsCheck = false) {
    const trimmed = overrideIntent ?? wsInput.trim()
    if (!trimmed) return
    // Route tool/workflow creation intents to the engine planner.
    // Guard: only re-route direct user invocations — not saved-workflow runs
    // (overrideIntent set) and not proposal approvals (sections/proposal set).
    if (overrideIntent === null && sections === null && proposal === null && isToolCreationIntent(trimmed)) {
      handleEnginePlan()
      return
    }
    const dsId = overrideDsId !== undefined ? overrideDsId : (selectedDatasetId || null)

    // ── Dataset trust guards ──────────────────────────────────────────────────
    // Only apply when using the ambient selectedDatasetId (not an explicit override
    // from a saved workflow which carries its own intentional dataset binding).
    if (!_skipDsCheck && overrideDsId === undefined) {
      const isAnalysis = /\b(report|analyz|analysis|summarize|summarise|insight|overview|kpi|dashboard|anomal|trend|forecast|metric|revenue|sales|performance|breakdown|segment|risk|drift|detect)\b/i.test(trimmed)
      if (isAnalysis && !dsId) {
        setWsError('Select a dataset before running an analysis.')
        setNoDsWarning(true)
        return
      }
      if (dsId && !datasetExplicit) {
        setDsPendingRun({ sections, proposal, overrideIntent, overrideDsId })
        return
      }
    }
    // ─────────────────────────────────────────────────────────────────────────

    setWsError(null); setWsLoading(true); setWsResult(null); setWsExecDurationMs(null); setBackToComposer(false); setWsRunSource(null)
    execStartedAtRef.current = Date.now()
    try {
      const data = await interpretTask(trimmed, token, dsId, wsEmail.trim() || null, sections)
      const execResult = data?.data ?? null
      const aiMeta = proposal ? {
        reasoning_summary:  proposal.reasoning_summary  ?? null,
        confidence:         proposal.confidence         ?? null,
        ai_enrichment_used: proposal.ai_enrichment_used ?? false,
        ai_enabled:         proposal.ai_enabled         ?? false,
        ai_model_used:      proposal.ai_model_used      ?? null,
      } : null
      setWsExecDurationMs(execStartedAtRef.current ? Date.now() - execStartedAtRef.current : null)
      const normalized = normalizeExecutionResult(execResult)
      setWsResult(normalized ? { ...normalized, _ai_meta: aiMeta } : null)
    } catch (err) {
      if (err?.message?.startsWith('401:')) { onSessionExpired(); return }
      setWsError(err.message?.replace(/^\d+:\s*/, '') || 'Execution failed.')
    } finally { setWsLoading(false) }
  }

  function handleConfirmDataset() {
    const args = dsPendingRun
    setDsPendingRun(null)
    setDatasetExplicit(true)
    handleRun(args.sections, args.proposal, args.overrideIntent, args.overrideDsId, true)
  }

  function handleReset() {
    setWsResult(null); setWsError(null); setWsProposal(null); setWsProposalError(null); setWsInput(''); setWsExecDurationMs(null)
    setEnginePlan(null); setSavedToolId(null); setToolStatus(null)
    setEngineBusy(null); setToast(null); setShowRawJson(false); setPlanDatasetId(null); setBackToComposer(false); setWsRunSource(null)
    // savedWorkflows + engineTools intentionally preserved — they are the persistent library
  }

  async function loadEngineTools() {
    try {
      const data  = await listEngineTools(token)
      const tools = Array.isArray(data?.data) ? data.data : Array.isArray(data) ? data : []
      setEngineTools(tools)
    } catch { /* silently ignore — don't block primary UX */ }
  }

  async function handleSaveWorkflow() {
    const intent = wsInput.trim()
    if (!intent || workflowSaving) return
    if (savedWorkflows.some(wf => wf.intent === intent)) return

    setWorkflowSaving(true)
    try {
      // Step 1: generate tool definition from intent
      const context  = selectedDatasetId ? { dataset_id: selectedDatasetId } : null
      const planData = await planEngineTool(intent, token, context)
      const toolDef  = planData?.data ?? planData

      // Step 2: persist tool definition to engine backend
      const scheduleType   = extractScheduleType(intent)
      const toolDefToSave  = scheduleType
        ? { ...toolDef, schedule: { enabled: true, schedule_type: scheduleType, timezone: 'UTC', cron: '' } }
        : toolDef
      const saveData = await saveEngineTool(toolDefToSave, token)
      const saved    = saveData?.data ?? saveData
      const toolId   = saved?.tool_id ?? null

      // Step 3: add to local session list with full metadata
      const dsRecord = datasetList?.find(d => d.id === selectedDatasetId)
      const wf = {
        id:           toolId || Date.now().toString(),
        toolId,
        title:        intel?.title || toolDef?.description || (intent.length > 60 ? intent.slice(0, 60) + '…' : intent),
        intent,
        datasetId:    selectedDatasetId || null,
        datasetName:  dsRecord?.filename || null,
        createdAt:    new Date().toISOString(),
        backendSaved: true,
        scheduleType: scheduleType || null,
      }
      setSavedWorkflows(prev => [wf, ...prev.filter(w => w.intent !== intent)])

      // Step 4: refresh backend list so panel stays in sync
      loadEngineTools()

      showToast('Saved to Tools Library')
    } catch (err) {
      if (err?.message?.startsWith('401:')) { onSessionExpired(); return }
      showToast(err.message?.replace(/^\d+:\s*/, '') || 'Failed to save workflow — please try again.', false)
    } finally {
      setWorkflowSaving(false)
    }
  }

  async function handleRunWorkflow(wf) {
    // Restore intent + dataset into composer for display and future edits
    setWsInput(wf.intent)
    if (wf.datasetId) setSelectedDatasetId(wf.datasetId)

    // If no backend tool ID, fall back to intent-based rerun
    if (!wf.toolId) {
      handleRun(null, null, wf.intent, wf.datasetId || null)
      return
    }

    // ── Engine tool execution path ─────────────────────────────────────────
    setWsError(null); setWsLoading(true); setWsResult(null); setWsExecDurationMs(null)
    setBackToComposer(false); setWsRunSource(null)
    execStartedAtRef.current = Date.now()

    async function pollRun(runId) {
      const MAX = 30
      for (let i = 0; i < MAX; i++) {
        const d   = await getEngineRun(runId, token)
        const run = d?.data ?? d
        const s   = (run?.status ?? '').toLowerCase()
        if (s === 'completed' || s === 'success' || s === 'done') return run
        if (s === 'failed' || s === 'error' || s === 'cancelled') {
          throw new Error(run?.error_message || run?.error || 'Workflow execution failed')
        }
        await new Promise(r => setTimeout(r, 1500))
      }
      throw new Error('Workflow execution timed out — please try again.')
    }

    function normalizeRun(raw) {
      // Case 1: engine wraps the interpret result under .result
      if (raw?.result?.dataset_report) {
        return { ...raw.result, started_at: raw.started_at, finished_at: raw.finished_at }
      }
      // Case 2: .result has a status (interpret-compatible flat object)
      if (raw?.result?.status) {
        return {
          ...raw.result,
          started_at: raw.started_at ?? raw.result.started_at,
          finished_at: raw.finished_at ?? raw.result.finished_at,
        }
      }
      // Case 3: result already IS the interpret-compatible shape
      if (raw?.dataset_report) return raw
      // Case 1.5: report is embedded inside engine RunRecord step_results.
      // The engine stores generate_dataset_report output in
      // step_results[].output.report — normalizeRun must pull it out here
      // so extractIntel() receives a dataset_report and renders the full canvas.
      const reportStep = (raw?.step_results ?? []).find(
        s => s.action_type === 'generate_dataset_report' && s.output?.report
      )
      if (reportStep) {
        return {
          dataset_report: reportStep.output.report,
          status:         raw.status,
          started_at:     raw.started_at,
          finished_at:    raw.finished_at,
        }
      }
      // Case 4: basic completion only
      return {
        status:     raw?.status || 'completed',
        task_type:  'workflow_execution',
        started_at: raw?.started_at,
        finished_at: raw?.finished_at,
      }
    }

    try {
      const inputs = {}
      if (wf.datasetId) inputs.dataset_id  = wf.datasetId
      if (wf.intent)    inputs.intent_text = wf.intent   // forwarded so the engine report remains intent-aware

      const execData = await executeEngineTool(wf.toolId, inputs, token)
      const exec     = execData?.data ?? execData

      // Handle async (run_id returned) vs synchronous (result inline) execution
      let runResult = exec
      const status  = (exec?.status ?? '').toLowerCase()
      if (exec?.run_id && (status === 'running' || status === 'pending' || status === 'queued')) {
        runResult = await pollRun(exec.run_id)
      }

      const normalized = normalizeRun(runResult)
      setWsExecDurationMs(execStartedAtRef.current ? Date.now() - execStartedAtRef.current : null)
      setWsResult(normalizeExecutionResult(normalized))
      setWsRunSource(wf.title)  // marks result as coming from a saved workflow
    } catch (err) {
      if (err?.message?.startsWith('401:')) { onSessionExpired(); return }
      // Graceful fallback — show a message and rerun via interpretTask
      showToast('Engine execution unavailable — running from intent…', false)
      handleRun(null, null, wf.intent, wf.datasetId || null)
    } finally {
      setWsLoading(false)
    }
  }

  return (
    <div style={{ fontFamily: FONT, paddingRight: '16px' }}>
      <style>{WS_STYLES}</style>
      <Toast toast={toast} />

      {/* Hidden file input */}
      <input ref={fileInputRef} type="file" accept=".csv,.xlsx,.xls,.json,.sql" style={{ display: 'none' }}
        onChange={e => { const f = e.target.files?.[0]; if (f && onUploadDataset) onUploadDataset(f); e.target.value = '' }}
      />

      {/* ── Page header — only on composer/workspace, hidden during execution and result ── */}
      {!activeLoading && (!hasResult || backToComposer) && (
        <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: '14px', flexWrap: 'wrap', gap: '12px', marginTop: '2px' }}>
          <div>
            <div style={{ display: 'flex', alignItems: 'center', gap: '10px', marginBottom: '4px' }}>
              <h1 style={{ margin: 0, fontSize: '1.65rem', fontWeight: '600', letterSpacing: '-0.5px', lineHeight: 1 }}>
                <span style={{ color: '#8b5cf6' }}>AI</span>
                <span style={{ color: C.text }}> Intelligence Workspace</span>
              </h1>
              <svg width="36" height="36" viewBox="0 0 24 24" fill="#6366f1" style={{ flexShrink: 0 }}>
                <path d="M12 2 L14 10 L22 12 L14 14 L12 22 L10 14 L2 12 L10 10 Z"/>
              </svg>
            </div>
            <p style={{ margin: 0, color: C.textMuted, fontSize: '0.7rem' }}>
              {enginePlan ? `Orchestration plan ready · ${enginePlan?.name || 'Tool'}` : 'Orchestrate, analyze, and automate with AI.'}
            </p>
          </div>
          {!!enginePlan && (
            <button className="ws-ghost-btn" onClick={handleReset} style={{ display: 'flex', alignItems: 'center', gap: '7px', background: C.surface, border: `1px solid ${C.border}`, borderRadius: '11px', padding: '9px 18px', fontSize: '0.82rem', color: C.textSec, cursor: 'pointer', fontFamily: FONT, fontWeight: '600', boxShadow: '0 1px 4px rgba(0,0,0,0.08)' }}>
              <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round"><line x1="12" y1="5" x2="12" y2="19"/><line x1="5" y1="12" x2="19" y2="12"/></svg>
              New Plan
            </button>
          )}
        </div>
      )}

      {/* ── Two-column layout ── */}
      <div style={{ display: 'flex', gap: '18px', alignItems: 'stretch' }}>

        {/* ── Left: main canvas ── */}
        <div style={{ flex: 1, minWidth: 0 }}>

          {/* ── Contextual Intelligence Strip ── */}
          {showComposer && contextStats && (
            <div style={{ marginBottom: '12px' }}>
              <ContextualIntelligenceStrip {...contextStats} C={C} onNavigate={setActiveNav} />
            </div>
          )}

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


              <div style={{ padding: '14px 18px 18px', position: 'relative', zIndex: 1, background: 'transparent' }}>

                {/* ── Header: badge + buttons ── */}
                <div style={{ display: 'flex', alignItems: 'center', gap: '10px', marginBottom: '12px', flexWrap: 'wrap' }}>
                  <div style={{ display: 'flex', alignItems: 'center', gap: '4px', background: 'rgba(99,102,241,0.12)', border: '1px solid rgba(99,102,241,0.22)', borderRadius: '20px', padding: '2px 7px 2px 5px' }}>
                    <svg width="8" height="8" viewBox="0 0 24 24" fill="url(#badge-grad)">
                      <defs>
                        <linearGradient id="badge-grad" x1="0%" y1="0%" x2="100%" y2="0%">
                          <stop offset="0%" stopColor="#60a5fa"/>
                          <stop offset="100%" stopColor="#a855f7"/>
                        </linearGradient>
                      </defs>
                      <path d="M12 1L14 10L23 12L14 14L12 23L10 14L1 12L10 10Z"/>
                    </svg>
                    <span style={{ fontSize: '0.45rem', fontWeight: '600', textTransform: 'uppercase', letterSpacing: '0.12em', background: 'linear-gradient(to right, #60a5fa, #a855f7)', WebkitBackgroundClip: 'text', WebkitTextFillColor: 'transparent', backgroundClip: 'text' }}>AI Orchestration Engine</span>
                  </div>
                  <div style={{ marginLeft: 'auto', display: 'flex', gap: '8px', alignItems: 'center' }}>
                    <button className="ws-ghost-btn" onClick={handleEnginePlan} disabled={disabled}
                      style={{ display: 'flex', alignItems: 'center', gap: '6px', background: C.surface, border: `1px solid ${C.borderAlt}`, borderRadius: '10px', padding: '7px 14px', fontSize: '0.72rem', color: C.textSec, cursor: disabled ? 'not-allowed' : 'pointer', fontFamily: FONT, fontWeight: '500', opacity: disabled ? 0.45 : 1 }}>
                      {enginePlanLoading
                        ? <div style={{ width: '10px', height: '10px', borderRadius: '50%', border: '2px solid rgba(99,102,241,0.35)', borderTopColor: '#6366f1', animation: 'ws-spin 0.75s linear infinite' }} />
                        : <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke={C.textSec} strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><circle cx="18" cy="5" r="3"/><circle cx="6" cy="12" r="3"/><circle cx="18" cy="19" r="3"/><line x1="8.59" y1="13.51" x2="15.42" y2="17.49"/><line x1="15.41" y1="6.51" x2="8.59" y2="10.49"/></svg>}
                      {enginePlanLoading ? 'Planning…' : 'Compose Plan'}
                    </button>
                    <button className="ws-action-btn" onClick={() => handleRun()} disabled={disabled}
                      style={{ display: 'flex', alignItems: 'center', gap: '7px', background: 'linear-gradient(135deg, #6366f1, #8b5cf6)', border: 'none', borderRadius: '10px', padding: '8px 20px', fontSize: '0.76rem', fontWeight: '700', color: '#fff', cursor: disabled ? 'not-allowed' : 'pointer', fontFamily: FONT, boxShadow: '0 4px 16px rgba(99,102,241,0.45)', opacity: disabled ? 0.55 : 1 }}>
                      {wsLoading
                        ? <div style={{ width: '11px', height: '11px', borderRadius: '50%', border: '2px solid rgba(255,255,255,0.35)', borderTopColor: '#fff', animation: 'ws-spin 0.75s linear infinite' }} />
                        : <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="#fff" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round"><polygon points="5 3 19 12 5 21 5 3"/></svg>}
                      {wsLoading ? 'Running…' : 'Run Directly'}
                    </button>
                  </div>
                </div>

                {/* ── Prior result banner (shown when user navigated back) ── */}
                {backToComposer && hasResult && (
                  <div style={{ display: 'flex', alignItems: 'center', gap: '12px', marginBottom: '16px', padding: '10px 16px', background: 'rgba(16,185,129,0.07)', border: '1px solid rgba(16,185,129,0.22)', borderRadius: '12px', animation: 'ws-fadein 0.25s ease' }}>
                    <div style={{ width: '7px', height: '7px', borderRadius: '50%', background: '#10b981', flexShrink: 0, animation: 'ws-dot-blink 2s ease infinite' }} />
                    <span style={{ fontSize: '0.76rem', color: C.textSec, flex: 1, lineHeight: 1.4 }}>
                      Previous results ready — <strong style={{ color: C.text, fontWeight: '600' }}>{intel?.title || 'Analysis complete'}</strong>
                    </span>
                    <button onClick={() => setBackToComposer(false)}
                      style={{ background: 'linear-gradient(135deg,#10b981,#059669)', border: 'none', borderRadius: '8px', padding: '6px 14px', fontSize: '0.72rem', fontWeight: '700', color: '#fff', cursor: 'pointer', fontFamily: FONT, flexShrink: 0, whiteSpace: 'nowrap' }}>
                      View Results
                    </button>
                  </div>
                )}

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
                    rows={4}
                    value={wsInput}
                    onChange={e => { setWsInput(e.target.value); setWsProposalError(null); setDsPendingRun(null); setNoDsWarning(false) }}
                    onKeyDown={e => { if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) { e.preventDefault(); handleEnginePlan() } }}
                    style={{
                      width: '100%', boxSizing: 'border-box',
                      background: C.bg, border: `1px solid ${C.border}`,
                      borderRadius: '12px', color: C.text,
                      fontSize: '0.82rem', padding: '10px 14px 32px',
                      outline: 'none', resize: 'none', lineHeight: 1.7,
                      fontFamily: FONT, transition: 'border-color 0.14s',
                    }}
                    onFocus={e => { e.target.style.borderColor = '#6366f1' }}
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
                      onFocus={e => { e.target.style.borderColor = '#6366f1' }} onBlur={e => { e.target.style.borderColor = C.border }}
                    />
                  </div>
                )}

                {/* ── SELECT DATASET section ── */}
                <div style={{ marginBottom: '20px' }}>
                  <div style={{ display: 'flex', alignItems: 'center', gap: '6px', marginBottom: '8px' }}>
                    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="#6366f1" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round"><ellipse cx="12" cy="5" rx="9" ry="3"/><path d="M21 12c0 1.66-4 3-9 3s-9-1.34-9-3"/><path d="M3 5v14c0 1.66 4 3 9 3s9-1.34 9-3V5"/></svg>
                    <span style={{ fontSize: '0.63rem', fontWeight: '600', color: C.text, textTransform: 'uppercase', letterSpacing: '0.14em' }}>Select Dataset</span>
                  </div>

                  <div style={{ display: 'flex', alignItems: 'center', gap: '8px', flexWrap: 'wrap' }}>
                    {/* Dataset dropdown */}
                    <div style={{ position: 'relative', flex: 1, minWidth: '180px' }}>
                      {dsPicker && <div style={{ position: 'fixed', inset: 0, zIndex: 998 }} onClick={() => setDsPicker(false)} />}
                      <button className="ws-ghost-btn" onClick={() => setDsPicker(o => !o)}
                        style={{ display: 'flex', alignItems: 'center', gap: '8px', width: '100%', background: (activeDs && datasetExplicit) ? 'rgba(99,102,241,0.07)' : (activeDs && !datasetExplicit) ? 'rgba(245,158,11,0.07)' : C.bg, border: `1px solid ${(activeDs && datasetExplicit) ? 'rgba(99,102,241,0.35)' : (activeDs && !datasetExplicit) ? 'rgba(245,158,11,0.45)' : C.border}`, borderRadius: '10px', padding: '9px 14px', fontSize: '0.76rem', color: (activeDs && datasetExplicit) ? '#6366f1' : (activeDs && !datasetExplicit) ? '#d97706' : C.textSec, cursor: 'pointer', fontFamily: FONT, fontWeight: '400', textAlign: 'left', position: 'relative', zIndex: 999 }}>
                        <span style={{ flex: 1 }}>{activeDs ? activeDs.filename : 'Choose an existing dataset'}</span>
                        {activeDs && !datasetExplicit && <span style={{ fontSize: '0.60rem', fontWeight: '600', color: '#d97706', background: 'rgba(245,158,11,0.12)', border: '1px solid rgba(245,158,11,0.30)', borderRadius: '4px', padding: '1px 5px', flexShrink: 0 }}>confirm</span>}
                        <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round" style={{ opacity: 0.5, flexShrink: 0 }}><path d="m6 9 6 6 6-6"/></svg>
                      </button>

                      {dsPicker && (
                        <div style={{ position: 'absolute', top: 'calc(100% + 6px)', left: 0, zIndex: 999, width: '100%', minWidth: '260px', background: C.surface, border: `1px solid ${C.borderAlt}`, borderRadius: '12px', boxShadow: '0 8px 28px rgba(0,0,0,0.16)', overflow: 'hidden', animation: 'ws-fadeup 0.15s ease' }}>
                          {/* Search */}
                          <div style={{ padding: '8px 10px', borderBottom: `1px solid ${C.border}` }}>
                            <div style={{ display: 'flex', alignItems: 'center', gap: '7px', background: C.bg, border: `1px solid ${C.border}`, borderRadius: '7px', padding: '5px 9px' }}>
                              <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke={C.textMuted} strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" style={{ flexShrink: 0 }}><circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/></svg>
                              <input
                                autoFocus
                                type="text"
                                placeholder="Search…"
                                value={dsSearch}
                                onChange={e => setDsSearch(e.target.value)}
                                style={{ flex: 1, background: 'none', border: 'none', outline: 'none', fontSize: '0.74rem', color: C.text, fontFamily: FONT, caretColor: '#6366f1' }}
                              />
                              {dsSearch && (
                                <button onClick={() => setDsSearch('')} style={{ background: 'none', border: 'none', cursor: 'pointer', padding: 0, display: 'flex', alignItems: 'center', color: C.textMuted }}>
                                  <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>
                                </button>
                              )}
                            </div>
                          </div>
                          {/* List */}
                          <div style={{ maxHeight: '240px', overflowY: 'auto', overscrollBehavior: 'contain' }}>
                            {(() => {
                              const q = dsSearch.toLowerCase().trim()
                              const filtered = (datasetList || []).filter(ds => ds.filename.toLowerCase().includes(q))
                              if (!datasetList?.length) return (
                                <div style={{ padding: '18px', textAlign: 'center', color: C.textMuted, fontSize: '0.76rem' }}>No datasets uploaded.</div>
                              )
                              if (!filtered.length) return (
                                <div style={{ padding: '18px', textAlign: 'center', color: C.textMuted, fontSize: '0.76rem' }}>No matches for "{dsSearch}"</div>
                              )
                              return filtered.map(ds => {
                                const isSel = ds.id === selectedDatasetId
                                const type  = getFileType(ds.filename)
                                const tc    = DS_COLOR[type] || C.textSec
                                const tbg   = DS_BG[type] || C.borderAlt
                                return (
                                  <div key={ds.id} onClick={() => { setSelectedDatasetId(ds.id); setDatasetExplicit(true); setDsPendingRun(null); setNoDsWarning(false); setDsPicker(false) }}
                                    style={{ display: 'flex', alignItems: 'center', gap: '6px', padding: '5px 12px', cursor: 'pointer', background: isSel ? 'rgba(99,102,241,0.08)' : 'transparent', borderBottom: `1px solid ${C.border}`, transition: 'background 0.1s' }}
                                    onMouseEnter={e => { if (!isSel) e.currentTarget.style.background = C.borderAlt }}
                                    onMouseLeave={e => { if (!isSel) e.currentTarget.style.background = 'transparent' }}>
                                    <div style={{ width: '22px', height: '15px', borderRadius: '3px', background: tbg, border: `1px solid ${tc}40`, display: 'flex', alignItems: 'center', justifyContent: 'center', fontSize: '0.48rem', fontWeight: '700', color: tc, flexShrink: 0 }}>{type.slice(0, 3)}</div>
                                    <div style={{ flex: 1, minWidth: 0 }}>
                                      <div style={{ fontSize: '0.75rem', fontWeight: '500', color: isSel ? '#6366f1' : C.text, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', lineHeight: 1.3 }}>{ds.filename}</div>
                                      <div style={{ fontSize: '0.60rem', color: C.textMuted, lineHeight: 1.2 }}>{(ds.row_count || 0).toLocaleString()} rows · {ds.column_count} cols</div>
                                    </div>
                                    {isSel && <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="#6366f1" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round"><polyline points="20 6 9 17 4 12"/></svg>}
                                  </div>
                                )
                              })
                            })()}
                          </div>
                          {/* Footer */}
                          <div onClick={() => { setDsPicker(false); setActiveNav('datasets') }}
                            style={{ padding: '8px 12px', display: 'flex', alignItems: 'center', gap: '6px', cursor: 'pointer', color: '#6366f1', fontSize: '0.74rem', fontWeight: '500', borderTop: `1px solid ${C.border}`, transition: 'background 0.12s' }}
                            onMouseEnter={e => e.currentTarget.style.background = 'rgba(99,102,241,0.07)'}
                            onMouseLeave={e => e.currentTarget.style.background = 'transparent'}>
                            <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><line x1="12" y1="5" x2="12" y2="19"/><line x1="5" y1="12" x2="19" y2="12"/></svg>
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

                {/* ── No-dataset inline warning ── */}
                {noDsWarning && !activeDs && (
                  <div style={{ marginBottom: '16px', padding: '10px 14px', background: 'rgba(239,68,68,0.06)', border: '1px solid rgba(239,68,68,0.35)', borderRadius: '10px', display: 'flex', alignItems: 'center', gap: '10px' }}>
                    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="#dc2626" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" style={{ flexShrink: 0 }}><circle cx="12" cy="12" r="10"/><line x1="12" y1="8" x2="12" y2="12"/><line x1="12" y1="16" x2="12.01" y2="16"/></svg>
                    <span style={{ fontSize: '0.78rem', color: '#dc2626', lineHeight: 1.5 }}>
                      Select a dataset before running this analysis.
                    </span>
                  </div>
                )}

                {/* ── Dataset confirmation banner ── */}
                {dsPendingRun && activeDs && (
                  <div style={{ marginBottom: '16px', padding: '12px 16px', background: 'rgba(245,158,11,0.07)', border: '1px solid rgba(245,158,11,0.35)', borderRadius: '10px', display: 'flex', alignItems: 'center', gap: '12px', flexWrap: 'wrap' }}>
                    <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="#d97706" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" style={{ flexShrink: 0 }}><path d="M10.29 3.86L1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"/><line x1="12" y1="9" x2="12" y2="13"/><line x1="12" y1="17" x2="12.01" y2="17"/></svg>
                    <span style={{ flex: 1, fontSize: '0.78rem', color: C.text, lineHeight: 1.5 }}>
                      This will analyze <strong>{activeDs.filename}</strong>{' '}
                      <span style={{ color: C.textMuted }}>({(activeDs.row_count || 0).toLocaleString()} rows)</span>.
                      {' '}Is this the right dataset?
                    </span>
                    <button
                      onClick={() => { setDsPendingRun(null); setDsPicker(true) }}
                      style={{ background: 'none', border: `1px solid ${C.border}`, borderRadius: '7px', padding: '6px 13px', fontSize: '0.74rem', color: C.textSec, cursor: 'pointer', fontFamily: FONT, whiteSpace: 'nowrap' }}>
                      Change
                    </button>
                    <button
                      onClick={handleConfirmDataset}
                      style={{ background: 'rgba(245,158,11,0.15)', border: '1px solid rgba(245,158,11,0.40)', borderRadius: '7px', padding: '6px 14px', fontSize: '0.74rem', fontWeight: '600', color: '#d97706', cursor: 'pointer', fontFamily: FONT, whiteSpace: 'nowrap' }}>
                      Use this dataset
                    </button>
                  </div>
                )}

                {/* ── QUICK START EXAMPLES section ── */}
                <div style={{ marginBottom: '4px' }}>
                  <div style={{ display: 'flex', alignItems: 'center', gap: '6px', marginBottom: '10px' }}>
                    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="#6366f1" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round"><polygon points="13 2 3 14 12 14 11 22 21 10 12 10 13 2"/></svg>
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

          {/* ── Reusable Workflows library (composer view) ── */}
          {showComposer && (
            <ReusableWorkflows workflows={allWorkflows} onRunWorkflow={handleRunWorkflow} C={C} />
          )}

          {/* ── Engine orchestration plan (primary flow) ── */}
          {enginePlan && !activeLoading && !hasResult && (
            <EngineOrchestrationPlan
              plan={enginePlan}
              C={C}
              savedToolId={savedToolId}
              toolStatus={toolStatus}
              engineBusy={engineBusy}
              onSave={handleSaveTool}
              onSubmit={handleSubmitTool}
              onApprove={handleApproveTool}
              onEdit={() => setEnginePlan(null)}
              onClear={() => { setEnginePlan(null); setWsInput(''); setSavedToolId(null); setToolStatus(null); setShowRawJson(false); setPlanDatasetId(null) }}
              showRawJson={showRawJson}
              setShowRawJson={setShowRawJson}
              datasetList={datasetList}
              planDatasetId={planDatasetId}
              setPlanDatasetId={setPlanDatasetId}
            />
          )}

          {/* ── Compose proposal preview (fallback) ── */}
          {wsProposal && !activeLoading && (
            <ProposalPreview proposal={wsProposal} C={C}
              onApprove={() => { const sections = wsProposal?.selected_sections ?? null; const proposal = wsProposal; setWsProposal(null); handleRun(sections, proposal) }}
              onEdit={() => setWsProposal(null)}
              onClear={() => { setWsProposal(null); setWsInput(''); setWsProposalError(null) }}
              onGoToDatasets={() => setActiveNav('datasets')}
            />
          )}

          {/* ── Execution Console — loading and completion (single persistent instance) ── */}
          {(activeLoading || (hasResult && !backToComposer)) && (
            <ExecutionConsole
              result={wsResult}
              wsInput={wsInput}
              enginePlan={enginePlan}
              datasetName={activeDs?.filename ?? null}
              activeDs={activeDs}
              intel={intel}
              C={C}
              onOpenReport={onOpenReport}
              setActiveNav={setActiveNav}
              onBack={() => setBackToComposer(true)}
              onRunAgain={handleRun}
              execDurationMs={wsExecDurationMs}
              user={user}
            />
          )}

          {/* ── Execution error ── */}
          {activeError && !activeLoading && !hasResult && !noDsWarning && (
            <div style={{ background: C.dangerSoft, border: `1px solid ${C.danger}40`, borderRadius: '18px', padding: '40px 36px', textAlign: 'center', animation: 'ws-fadein 0.25s ease' }}>
              <div style={{ fontSize: '0.85rem', color: C.danger, marginBottom: '10px', fontWeight: '700' }}>Execution Error</div>
              <p style={{ margin: '0 0 20px', fontSize: '0.84rem', color: C.danger, lineHeight: 1.7 }}>{activeError}</p>
              <button onClick={handleReset} style={{ background: C.danger, border: 'none', borderRadius: '10px', padding: '9px 22px', fontSize: '0.82rem', fontWeight: '700', color: '#fff', cursor: 'pointer', fontFamily: FONT }}>Try Again</button>
            </div>
          )}

        </div>

        {/* ── Right column — hidden during execution (console is full-width), shown otherwise ── */}
        {!activeLoading && (!hasResult || backToComposer) && (
          <div style={{ width: '280px', flexShrink: 0, display: 'flex', flexDirection: 'column' }}>
            <EmptyAssistantPanel proposal={wsProposal} C={C} />
          </div>
        )}
      </div>
    </div>
  )
}
