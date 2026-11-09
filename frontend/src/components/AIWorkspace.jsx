import { useState, useEffect, useRef, useCallback } from 'react'
import { composeIntent, interpretTask, askComposer, askReport, askDataset, planEngineTool, saveEngineTool, submitEngineTool, approveEngineTool, listDataSources, promoteConceptMapping, exportComposerAnswer } from '../api/client'
import { useConversationState, buildComposerPayload } from '../hooks/useConversationState'
import ProposalPreview from './ProposalPreview'
import ChartSection from './ChartSection'
import { ExecutiveReportContent } from './ExecutiveReportView'

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

const DATABASE_QUICK_STARTS = [
  {
    prompt: 'How many students are there?',
    text: 'Count all students currently in the database',
    color: '#34d399',
    iconType: 'bars',
  },
  {
    prompt: 'Show total student enrollments by month across all available years.',
    text: 'Show monthly student enrollment trends',
    color: '#38bdf8',
    iconType: 'trend',
  },
  {
    prompt: 'Show the top 10 programs by number of enrolled students.',
    text: 'Rank the top 10 programs by enrollment',
    color: '#a78bfa',
    iconType: 'bars',
  },
  {
    prompt: 'How many students are enrolled in SQL classes?',
    text: 'Count students enrolled in SQL classes',
    color: '#fbbf24',
    iconType: 'document',
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

// Routes tool/workflow creation intent to the engine planner instead of interpretTask.
// Matches explicit construction verbs or scheduling/recurrence signals paired with automation
// nouns — never fires for plain analytics ("analyze my dataset", "generate a report").
const TOOL_CREATION_RE = /\b(create|build|make|set ?up|develop|design)\b.{0,60}\b(tool|workflow|automation|agent|pipeline)\b|\breusable\b.{0,40}\b(workflow|tool|process)\b|\bmonitoring tool\b|\balert workflow\b|\b(schedule[d]?|recurring|automated)\b.{0,40}\b(report|workflow|automation|monitoring|alert)\b|\b(weekly|daily|monthly)\b.{0,40}\b(report|workflow|automation|monitoring)\b/i

function isToolCreationIntent(input) {
  return TOOL_CREATION_RE.test(input || '')
}

// Routes metadata/catalog/governance questions to POST /v1/composer/ask.
// Fires only for lookup/question patterns — never for analytics or creation intents.
const COMPOSER_INTENT_RE = /\b(what|which|show|list|explain|describe|find|tell me|how many|are there|do we have)\b.{0,100}\b(table[s]?|column[s]?|field[s]?|schema[s]?|dictionary|domain[s]?|entit(?:y|ies)|pii|relationship[s]?|profil(?:e|ing)|lineage|catalog|metadata|governance)\b|\bshow\s+(dictionary|domain|entity|profil)\b|\bdictionary\s+status\b|\bdomain\s+assignment[s]?\b|\bentity\s+assignment[s]?\b|\bprofiling\s+status\b|\bpii\s+exists?\b|\bwhat\s+pii\b|\bwhat\s+needs\s+review\b|\bneeds?\s+review\b|\brelationship[s]?\s+exist\b|\bexplain\s+this\s+table\b|\bexplain.{0,30}table\b/i

function isComposerIntent(input) {
  if (!input) return false
  return COMPOSER_INTENT_RE.test(input)
}

// Routes report-generation prompts to POST /v1/composer/ask with dataset_id.
// Matches the 7 supported generation intents. Requires a dataset to be selected
// (enforced in handleRun before calling handleComposerAsk).
const REPORT_GEN_RE = /\b(generate|create|build|make)\s+(a\s+)?(report|intelligence\s+report|kpi\s+report|quality\s+report|pdf\s+report|executive\s+summary)\b|\banalyze\s+(this\s+)?(dataset|data)\b|\banalyse\s+(this\s+)?(dataset|data)\b|\bshow\s+trends?\b|\bcreate\s+executive\s+summary\b|\bbuild\s+kpi\s+report\b|\bgenerate\s+quality\s+report\b|\bcreate\s+pdf\s+report\b/i

function isReportGenerationIntent(input) {
  if (!input) return false
  return REPORT_GEN_RE.test(input)
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

const EC_PHASE_COLORS = [
  { bg: 'rgba(99,102,241,0.15)', border: 'rgba(99,102,241,0.28)', fg: '#a5b4fc' },
  { bg: 'rgba(59,130,246,0.15)',  border: 'rgba(59,130,246,0.28)',  fg: '#60a5fa' },
  { bg: 'rgba(20,184,166,0.15)', border: 'rgba(20,184,166,0.28)', fg: '#2dd4bf' },
  { bg: 'rgba(16,185,129,0.15)', border: 'rgba(16,185,129,0.28)', fg: '#34d399' },
  { bg: 'rgba(99,102,241,0.15)', border: 'rgba(99,102,241,0.28)', fg: '#a5b4fc' },
  { bg: 'rgba(16,185,129,0.15)', border: 'rgba(16,185,129,0.28)', fg: '#34d399' },
]

function ExecutionConsole({ result, wsInput, enginePlan, datasetName, activeDs, C, onOpenReport, onBack, onRunAgain, execDurationMs }) {
  const [activePhase, setActivePhase] = useState(0)

  useEffect(() => {
    if (result) return
    queueMicrotask(() => setActivePhase(0))
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

function EmptyAssistantPanel({ C, proposal, activeDs, activeSource, wsInput, token }) {
  const [question,     setQuestion]     = useState('')
  const [answer,       setAnswer]       = useState(null)
  const [loading,      setLoading]      = useState(false)
  const [error,        setError]        = useState(null)

  async function handleAsk(q) {
    const trimmed = (q ?? question).trim()
    if (!trimmed || !activeDs) return
    if (q != null) setQuestion(q)
    setLoading(true); setError(null); setAnswer(null)
    try {
      const data = await askDataset(activeDs.id, trimmed, wsInput || null, token)
      setAnswer(data?.data ?? null)
      setQuestion('')
    } catch (err) {
      setError(err.message?.replace(/^\d+:\s*/, '') || 'Could not get an answer.')
    } finally { setLoading(false) }
  }

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
          {activeDs || activeSource
            ? `Using ${activeDs?.filename || activeSource?.display_name} — ask a question below.`
            : proposal
              ? 'Review the plan on the left, then approve and run to see full results.'
              : 'Select a data source to enable AI Assistant.'}
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
          {EMPTY_SUGGESTIONS.map(q => (
            <div
              key={q}
              className="ws-suggest-row"
              onClick={() => handleAsk(q)}
              style={{
                display: 'flex', alignItems: 'center', gap: '10px',
                padding: '11px 18px',
                borderTop: `1px solid rgba(30,43,82,0.18)`,
                cursor: activeDs ? 'pointer' : 'default',
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

      {/* ── Answer ── */}
      {(answer || loading || error) && (
        <div style={{ padding: '0 18px 8px' }}>
          {loading && (
            <div style={{ display: 'flex', alignItems: 'center', gap: '8px', padding: '10px 0' }}>
              <span style={{ width: '7px', height: '7px', borderRadius: '50%', background: '#6366f1', animation: 'ws-dot-blink 1.2s ease infinite', flexShrink: 0 }} />
              <span style={{ fontSize: '0.70rem', color: C.textMuted }}>Thinking…</span>
            </div>
          )}
          {error && !loading && (
            <div style={{ padding: '8px 12px', background: 'rgba(248,113,113,0.08)', border: '1px solid rgba(248,113,113,0.20)', borderRadius: '9px' }}>
              <span style={{ fontSize: '0.70rem', color: '#f87171' }}>{error}</span>
            </div>
          )}
          {answer && !loading && (() => {
            const confColor = { high: '#10b981', medium: '#f59e0b', low: '#9ca3af' }[answer.confidence] ?? '#9ca3af'
            return (
              <div style={{ background: 'rgba(99,102,241,0.07)', border: '1px solid rgba(99,102,241,0.18)', borderRadius: '10px', padding: '10px 13px' }}>
                <div style={{ display: 'flex', alignItems: 'center', gap: '6px', marginBottom: '6px' }}>
                  <svg width="10" height="10" viewBox="0 0 24 24" fill={confColor}><path d="M12 2l2.4 7.4H22l-6.2 4.5 2.4 7.4L12 17l-6.2 4.3 2.4-7.4L2 9.4h7.6z"/></svg>
                  <span style={{ fontSize: '0.58rem', fontWeight: '700', textTransform: 'uppercase', letterSpacing: '0.1em', color: confColor }}>{answer.confidence ?? 'low'}</span>
                </div>
                <p style={{ margin: '0 0 8px', fontSize: '0.73rem', color: C.text, lineHeight: 1.6 }}>{answer.answer}</p>
                {answer.suggested_actions?.length > 0 && (
                  <div style={{ display: 'flex', flexDirection: 'column', gap: '4px' }}>
                    {answer.suggested_actions.map(a => (
                      <button key={a} onClick={() => handleAsk(a)} style={{
                        background: 'rgba(99,102,241,0.10)', border: '1px solid rgba(99,102,241,0.22)',
                        borderRadius: '7px', padding: '5px 10px', fontSize: '0.68rem', color: '#a5b4fc',
                        cursor: 'pointer', fontFamily: FONT, textAlign: 'left', lineHeight: 1.4,
                      }}>{a}</button>
                    ))}
                  </div>
                )}
              </div>
            )
          })()}
        </div>
      )}

      {/* ── Ask input ── */}
      <div style={{ padding: '12px 18px 8px', borderTop: `1px solid rgba(30,43,82,0.18)` }}>
        <div style={{ display: 'flex', gap: '8px', alignItems: 'center', opacity: activeDs ? 1 : 0.45 }}>
          <input
            disabled={!activeDs || loading}
            value={question}
            onChange={e => setQuestion(e.target.value)}
            onKeyDown={e => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); handleAsk() } }}
            placeholder={activeDs ? `Ask about ${activeDs.filename}…` : 'Ask anything about your data…'}
            style={{
              flex: 1, background: C.bg, border: `1px solid rgba(30,43,82,0.18)`,
              borderRadius: '24px', padding: '9px 16px', fontSize: '0.71rem',
              color: activeDs ? C.text : C.textMuted, outline: 'none', fontFamily: FONT,
              cursor: activeDs ? 'text' : 'not-allowed',
            }}
          />
          <button
            disabled={!activeDs || loading || !question.trim()}
            onClick={() => handleAsk()}
            style={{
              width: '34px', height: '34px', borderRadius: '50%', flexShrink: 0,
              background: 'linear-gradient(135deg,#6366f1,#4f46e5)',
              border: 'none', cursor: activeDs && !loading && question.trim() ? 'pointer' : 'not-allowed',
              display: 'flex', alignItems: 'center', justifyContent: 'center',
              opacity: activeDs && !loading && question.trim() ? 1 : 0.5,
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

// Phase 3, Step 3 — shown when "remember this choice" tries to teach a term
// a meaning that conflicts with what was already taught (409
// remembered_term_conflict). Conversational wording, not governance
// language — the user only ever sees "keep" or "replace", never
// confidence/state jargon.
export function RememberConflictPanel({ conflict, busy, onKeep, onReplace, C }) {
  if (!conflict) return null
  return (
    <div style={{
      position: 'fixed', bottom: '24px', right: '28px', zIndex: 9999,
      maxWidth: '380px', background: C.surface, border: `1px solid ${C.border}`,
      borderRadius: '12px', padding: '16px 18px',
      boxShadow: '0 8px 32px rgba(0,0,0,0.24)', animation: 'ws-fadeup 0.25s ease',
    }}>
      <div style={{ fontSize: '0.8rem', fontWeight: '600', color: C.text, lineHeight: 1.5, marginBottom: '12px' }}>
        You previously taught me that &quot;{conflict.term}&quot; means &quot;{conflict.existingCanonicalTerm}&quot;.
        Replace it with &quot;{conflict.requestedCanonicalTerm}&quot;?
      </div>
      <div style={{ display: 'flex', gap: '8px', flexWrap: 'wrap' }}>
        <button
          type="button"
          onClick={onKeep}
          disabled={busy}
          style={{ background: C.bg, border: `1px solid ${C.border}`, borderRadius: '8px', padding: '7px 14px', fontSize: '0.74rem', fontWeight: '600', color: C.textSec, cursor: busy ? 'not-allowed' : 'pointer', fontFamily: FONT }}
        >
          Keep existing meaning
        </button>
        <button
          type="button"
          onClick={onReplace}
          disabled={busy}
          style={{ background: '#6366f1', border: 'none', borderRadius: '8px', padding: '7px 14px', fontSize: '0.74rem', fontWeight: '700', color: '#fff', cursor: busy ? 'not-allowed' : 'pointer', fontFamily: FONT }}
        >
          {busy ? 'Replacing…' : 'Replace remembered meaning'}
        </button>
      </div>
    </div>
  )
}

function EngineOrchestrationPlan({
  plan, C, savedToolId, toolStatus,
  engineBusy, onSave, onSubmit, onApprove,
  onEdit, onClear,
  datasetList, planDatasetId, setPlanDatasetId,
}) {
  const nodes      = plan?.graph?.nodes ?? []
  const allInputs  = plan?.inputs ?? []
  const reqInputs  = allInputs.filter(i => i.required)
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
        <span style={{ fontSize: '0.78rem', fontWeight: '500', color: C.textSec }}>AI Tool Preview</span>
      </div>

      {/* ── Progress steps ── */}
      <div style={{ display: 'flex', alignItems: 'center' }}>
        {[
          { label: 'Dataset', done: !!planDatasetId },
          { label: 'Plan Ready', done: true },
          { label: 'Actions Reviewed', done: true },
          { label: 'Tool Saved', done: !!savedToolId },
        ].map((step, i, arr) => (
          <div key={step.label} style={{ display: 'flex', alignItems: 'center', flex: i < arr.length - 1 ? 1 : 'none' }}>
            <div style={{ display: 'flex', alignItems: 'center', gap: '5px', flexShrink: 0 }}>
              <div style={{ width: '18px', height: '18px', borderRadius: '50%', display: 'flex', alignItems: 'center', justifyContent: 'center', background: step.done ? '#10b981' : 'rgba(148,163,184,0.15)', border: step.done ? 'none' : '1.5px solid rgba(148,163,184,0.35)', flexShrink: 0 }}>
                {step.done
                  ? <svg width="9" height="9" viewBox="0 0 24 24" fill="none" stroke="#fff" strokeWidth="3" strokeLinecap="round" strokeLinejoin="round"><polyline points="20 6 9 17 4 12"/></svg>
                  : <span style={{ width: '5px', height: '5px', borderRadius: '50%', background: 'rgba(148,163,184,0.5)' }} />}
              </div>
              <span style={{ fontSize: '0.67rem', fontWeight: step.done ? '600' : '500', color: step.done ? '#10b981' : C.textMuted, whiteSpace: 'nowrap' }}>{step.label}</span>
            </div>
            {i < arr.length - 1 && (
              <div style={{ flex: 1, height: '1.5px', background: step.done ? 'rgba(16,185,129,0.3)' : 'rgba(148,163,184,0.18)', margin: '0 8px' }} />
            )}
          </div>
        ))}
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
                  <span style={{ fontSize: '0.55rem', fontWeight: '800', color: '#a5b4fc', textTransform: 'uppercase', letterSpacing: '0.16em' }}>AI-Generated Plan</span>
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
                    Save as Reusable Tool
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
            <div style={{ display: 'flex', flexDirection: 'column', gap: '10px' }}>
              {reqInputs.map(inp => {
                const isDataset = inp.name === 'dataset_id'

                if (isDataset) {
                  const activeDs = datasetList?.find(d => d.id === planDatasetId) || null
                  if (activeDs) {
                    return (
                      <div key={inp.name} style={{ padding: '12px 16px', background: 'rgba(16,185,129,0.06)', border: '1px solid rgba(16,185,129,0.22)', borderRadius: '12px', display: 'flex', alignItems: 'center', gap: '10px', flexWrap: 'wrap' }}>
                        <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="#10b981" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" style={{ flexShrink: 0 }}><ellipse cx="12" cy="5" rx="9" ry="3"/><path d="M21 12c0 1.66-4 3-9 3s-9-1.34-9-3"/><path d="M3 5v14c0 1.66 4 3 9 3s9-1.34 9-3V5"/></svg>
                        <span style={{ fontSize: '0.75rem', fontWeight: '600', color: '#10b981' }}>Selected Dataset</span>
                        <span style={{ fontSize: '0.8rem', color: C.text, fontWeight: '600' }}>{activeDs.filename}</span>
                        <button onClick={() => setPlanDatasetId(null)} style={{ marginLeft: 'auto', fontSize: '0.68rem', color: C.textMuted, background: 'none', border: `1px solid ${C.border}`, borderRadius: '6px', padding: '2px 8px', cursor: 'pointer', fontFamily: FONT }}>Change</button>
                      </div>
                    )
                  }
                  return (
                    <div key={inp.name} style={{ padding: '13px 16px', background: 'rgba(251,191,36,0.06)', border: '1px solid rgba(251,191,36,0.22)', borderRadius: '12px', display: 'flex', alignItems: 'center', gap: '12px', flexWrap: 'wrap' }}>
                      <div style={{ display: 'flex', alignItems: 'center', gap: '7px', flexShrink: 0 }}>
                        <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="#fbbf24" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><ellipse cx="12" cy="5" rx="9" ry="3"/><path d="M21 12c0 1.66-4 3-9 3s-9-1.34-9-3"/><path d="M3 5v14c0 1.66 4 3 9 3s9-1.34 9-3V5"/></svg>
                        <span style={{ fontSize: '0.8rem', fontWeight: '600', color: '#fbbf24' }}>Select your dataset</span>
                        <span style={{ fontSize: '0.76rem', color: C.textSec }}>— choose which dataset this tool will analyze</span>
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
                      </div>
                    </div>
                  )
                }

                return null
              })}
            </div>
          )}

          {/* ── What This Tool Does ── */}
          {nodes.length > 0 && (
            <div style={{ marginTop: '16px', padding: '16px 18px', background: 'rgba(16,185,129,0.05)', border: '1px solid rgba(16,185,129,0.18)', borderRadius: '12px' }}>
              <div style={{ fontSize: '0.66rem', fontWeight: '800', color: '#10b981', textTransform: 'uppercase', letterSpacing: '0.12em', marginBottom: '10px' }}>
                What This Tool Does
              </div>
              <div style={{ display: 'flex', flexDirection: 'column', gap: '7px', marginBottom: '12px' }}>
                {nodes.map((node, i) => {
                  const actionLabel = node.description || node.label || node.name || actionToLabel(node.action_type)
                  return (
                    <div key={node.id ?? node.node_id ?? i} style={{ display: 'flex', alignItems: 'flex-start', gap: '9px' }}>
                      <span style={{ color: '#10b981', fontWeight: '700', fontSize: '0.82rem', flexShrink: 0, lineHeight: 1.45 }}>✓</span>
                      <span style={{ fontSize: '0.8rem', color: C.text, lineHeight: 1.45 }}>{actionLabel}</span>
                    </div>
                  )
                })}
              </div>
              <div style={{ padding: '8px 12px', background: 'rgba(99,102,241,0.06)', border: '1px solid rgba(99,102,241,0.14)', borderRadius: '8px' }}>
                <span style={{ fontSize: '0.73rem', color: C.textSec, lineHeight: 1.5 }}>
                  This AI tool will automatically analyze the selected dataset and generate actionable business intelligence.
                </span>
              </div>
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

      {/* ── Save success ── */}
      {savedToolId && (
        <div style={{ background: 'rgba(16,185,129,0.07)', border: '1px solid rgba(16,185,129,0.28)', borderRadius: '16px', padding: '20px 24px', animation: 'ws-fadein 0.3s ease' }}>
          <div style={{ display: 'flex', alignItems: 'flex-start', gap: '14px', flexWrap: 'wrap' }}>
            <div style={{ width: '36px', height: '36px', borderRadius: '50%', background: '#10b981', display: 'flex', alignItems: 'center', justifyContent: 'center', flexShrink: 0 }}>
              <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="#fff" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round"><polyline points="20 6 9 17 4 12"/></svg>
            </div>
            <div style={{ flex: 1, minWidth: 0 }}>
              <div style={{ fontSize: '0.9rem', fontWeight: '700', color: '#10b981', marginBottom: '4px' }}>Tool Saved Successfully</div>
              <div style={{ fontSize: '0.82rem', color: C.textSec, marginBottom: '12px' }}>
                <strong style={{ color: C.text }}>{displayTitle}</strong> is now available in:
              </div>
              <div style={{ display: 'flex', flexDirection: 'column', gap: '4px', marginBottom: '16px' }}>
                {['AI Workspace — Saved Workflows panel', 'Tool Library'].map(loc => (
                  <div key={loc} style={{ display: 'flex', alignItems: 'center', gap: '7px', fontSize: '0.78rem', color: C.textSec }}>
                    <svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="#10b981" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round"><polyline points="20 6 9 17 4 12"/></svg>
                    {loc}
                  </div>
                ))}
              </div>
              <button onClick={onClear}
                style={{ display: 'inline-flex', alignItems: 'center', gap: '7px', background: '#10b981', border: 'none', borderRadius: '10px', padding: '8px 18px', fontSize: '0.78rem', fontWeight: '700', color: '#fff', cursor: 'pointer', fontFamily: FONT, boxShadow: '0 4px 14px rgba(16,185,129,0.3)' }}>
                <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round"><polygon points="5 3 19 12 5 21 5 3"/></svg>
                Run Now
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}

// ─── Contextual Intelligence Strip ───────────────────────────────────────────

function ContextualIntelligenceStrip({ workflowCount, datasetCount, reportsToday, successRate, alertCount, C, onNavigate }) {
  const rate = successRate !== null && successRate !== undefined ? parseFloat(successRate) : null
  const rateStr   = rate !== null ? `${rate.toFixed(1)}%` : '—'
  const rateColor = rate === null ? C.textMuted : rate >= 95 ? '#10b981' : rate >= 80 ? '#f59e0b' : '#f87171'
  const hasAlerts = (alertCount ?? 0) > 0

  const cards = [
    {
      label: 'Active Workflows',
      value: workflowCount ?? 0,
      sub: 'currently active',
      color: '#a5b4fc',
      nav: 'workflows',
      trend: null,
      critical: false,
      emphasized: false,
      icon: <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true"><circle cx="18" cy="5" r="3"/><circle cx="6" cy="12" r="3"/><circle cx="18" cy="19" r="3"/><line x1="8.59" y1="13.51" x2="15.42" y2="17.49"/><line x1="15.41" y1="6.51" x2="8.59" y2="10.49"/></svg>,
    },
    {
      label: 'Datasets Connected',
      value: datasetCount ?? 0,
      sub: 'data assets available',
      color: '#38bdf8',
      nav: 'datasets',
      trend: null,
      critical: false,
      emphasized: false,
      icon: <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true"><ellipse cx="12" cy="5" rx="9" ry="3"/><path d="M3 5v14c0 1.66 4.03 3 9 3s9-1.34 9-3V5"/><path d="M3 12c0 1.66 4.03 3 9 3s9-1.34 9-3"/></svg>,
    },
    {
      label: 'Reports Generated',
      value: reportsToday ?? 0,
      sub: 'total reports',
      color: '#f472b6',
      nav: 'reports',
      trend: { dir: 'up', label: 'growing' },
      critical: false,
      emphasized: false,
      icon: <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/><line x1="16" y1="13" x2="8" y2="13"/><line x1="16" y1="17" x2="8" y2="17"/></svg>,
    },
    {
      label: 'Success Rate',
      value: rateStr,
      sub: 'workflow executions',
      color: rateColor,
      nav: 'history',
      trend: rate !== null && rate >= 95 ? { dir: 'up', label: 'excellent' } : null,
      critical: false,
      emphasized: true,
      icon: <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true"><path d="M22 11.08V12a10 10 0 1 1-5.93-9.14"/><polyline points="22 4 12 14.01 9 11.01"/></svg>,
    },
    {
      label: 'Alerts',
      value: hasAlerts ? alertCount : 'None',
      sub: hasAlerts ? 'action required' : 'all clear',
      color: hasAlerts ? '#f87171' : '#10b981',
      nav: 'operations',
      trend: null,
      critical: hasAlerts,
      emphasized: false,
      icon: <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true"><path d="M18 8A6 6 0 0 0 6 8c0 7-3 9-3 9h18s-3-2-3-9"/><path d="M13.73 21a2 2 0 0 1-3.46 0"/></svg>,
    },
  ]

  return (
    <div role="region" aria-label="Platform KPIs" style={{ display: 'flex', gap: '8px' }}>
      {cards.map((card, i) => (
        <div key={i}
          role={card.nav ? 'button' : undefined}
          tabIndex={card.nav ? 0 : undefined}
          aria-label={`${card.label}: ${card.value}`}
          onClick={() => card.nav && onNavigate?.(card.nav)}
          onKeyDown={e => { if (card.nav && (e.key === 'Enter' || e.key === ' ')) { e.preventDefault(); onNavigate?.(card.nav) } }}
          style={{
            flex: 1, minWidth: 0,
            background: card.emphasized
              ? `linear-gradient(135deg, ${card.color}12 0%, ${card.color}06 100%)`
              : C.surface,
            border: `1px solid ${card.critical ? card.color : card.emphasized ? `${card.color}50` : C.border}`,
            borderRadius: '10px', padding: '8px 10px',
            display: 'flex', flexDirection: 'column', gap: '4px',
            cursor: card.nav ? 'pointer' : 'default',
            transition: 'border-color 0.15s, transform 0.15s, box-shadow 0.15s',
            boxShadow: card.emphasized ? `0 0 0 1px ${card.color}20, 0 2px 8px ${card.color}10` : 'none',
            position: 'relative', overflow: 'hidden',
          }}
          onMouseEnter={e => {
            if (card.nav) {
              e.currentTarget.style.borderColor = card.color
              e.currentTarget.style.transform = 'translateY(-1px)'
              e.currentTarget.style.boxShadow = `0 4px 12px ${card.color}25`
            }
          }}
          onMouseLeave={e => {
            if (card.nav) {
              e.currentTarget.style.borderColor = card.critical ? card.color : card.emphasized ? `${card.color}50` : C.border
              e.currentTarget.style.transform = 'translateY(0)'
              e.currentTarget.style.boxShadow = card.emphasized ? `0 0 0 1px ${card.color}20, 0 2px 8px ${card.color}10` : 'none'
            }
          }}
        >
          {/* Critical pulse indicator */}
          {card.critical && (
            <span style={{
              position: 'absolute', top: '7px', right: '7px',
              width: '6px', height: '6px', borderRadius: '50%',
              background: card.color,
              boxShadow: `0 0 0 0 ${card.color}`,
              animation: 'kpi-pulse 1.8s ease-in-out infinite',
            }} aria-hidden="true" />
          )}

          {/* Label row */}
          <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
            <span style={{
              fontSize: '0.6rem', fontWeight: '600', color: card.color,
              textTransform: 'uppercase', letterSpacing: '0.07em',
              overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap',
              paddingRight: card.critical ? '12px' : 0,
            }}>{card.label}</span>
            {!card.critical && (
              <span style={{ color: card.color, flexShrink: 0, opacity: 0.8 }}>{card.icon}</span>
            )}
          </div>

          {/* Value */}
          <div style={{
            fontSize: card.emphasized ? '1.15rem' : '1.05rem',
            fontWeight: card.emphasized ? '700' : '600',
            color: card.color,
            letterSpacing: '-0.3px', lineHeight: 1,
            overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap',
          }}>{card.value}</div>

          {/* Subtitle + trend */}
          <div style={{ display: 'flex', alignItems: 'center', gap: '4px' }}>
            <span style={{ fontSize: '0.6rem', color: C.textMuted, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{card.sub}</span>
            {card.trend && (
              <span style={{
                fontSize: '0.55rem', fontWeight: '600',
                color: card.trend.dir === 'up' ? '#10b981' : '#f87171',
                flexShrink: 0,
              }} aria-label={card.trend.label}>
                {card.trend.dir === 'up' ? '↑' : '↓'}
              </span>
            )}
          </div>
        </div>
      ))}

      <style>{`
        @keyframes kpi-pulse {
          0%   { box-shadow: 0 0 0 0 rgba(248,113,113,0.6); }
          70%  { box-shadow: 0 0 0 5px rgba(248,113,113,0); }
          100% { box-shadow: 0 0 0 0 rgba(248,113,113,0); }
        }
      `}</style>
    </div>
  )
}

// ─── Enterprise Composer result components ────────────────────────────────────

function ComposerLoading({ C }) {
  return (
    <div style={{
      background: C.surface, border: `1px solid ${C.border}`, borderRadius: '18px',
      padding: '48px 36px', textAlign: 'center', animation: 'ws-fadein 0.3s ease',
      boxShadow: '0 4px 24px rgba(0,0,0,0.08)',
    }}>
      <div style={{ display: 'inline-flex', alignItems: 'center', justifyContent: 'center', width: '52px', height: '52px', borderRadius: '14px', background: 'rgba(99,102,241,0.1)', marginBottom: '16px' }}>
        <div style={{ width: '22px', height: '22px', borderRadius: '50%', border: '2.5px solid rgba(99,102,241,0.25)', borderTopColor: '#6366f1', animation: 'ws-spin 0.75s linear infinite' }} />
      </div>
      <div style={{ fontSize: '0.6rem', fontWeight: '800', color: '#6366f1', textTransform: 'uppercase', letterSpacing: '0.16em', marginBottom: '8px' }}>Catalog Intelligence</div>
      <div style={{ fontSize: '0.95rem', fontWeight: '600', color: C.text, marginBottom: '6px' }}>Querying enterprise catalog…</div>
      <div style={{ fontSize: '0.75rem', color: C.textMuted }}>Resolving intent and collecting evidence from registered services</div>
    </div>
  )
}

const COMPOSER_NEXT_ACTION = {
  dictionary_query:   'Review dictionary entries and approve pending AI suggestions.',
  domain_query:       'Navigate to Domain Assignments to review and approve domain classifications.',
  entity_query:       'Navigate to Entity Assignments to review entity mappings.',
  pii_detection:      'Review PII columns and apply appropriate data classification policies.',
  profile_query:      'Check profiling status and schedule a fresh profile run if needed.',
  relationship_query: 'Explore the Knowledge Graph for full relationship details.',
  governance_query:   'Open the Governance Command Center for the full compliance view.',
  metadata_search:    'Use Enterprise Search for deeper cross-source metadata queries.',
  table_explanation:  'Navigate to the specific table in the Data Catalog for full context.',
}

const ANSWER_TYPE_COLORS = {
  dictionary_status:  { bg: 'rgba(99,102,241,0.10)',  text: '#6366f1', border: 'rgba(99,102,241,0.25)'  },
  domain_assignments: { bg: 'rgba(59,130,246,0.10)',  text: '#3b82f6', border: 'rgba(59,130,246,0.25)'  },
  entity_assignments: { bg: 'rgba(16,185,129,0.10)',  text: '#10b981', border: 'rgba(16,185,129,0.25)'  },
  profiling_status:   { bg: 'rgba(245,158,11,0.10)',  text: '#f59e0b', border: 'rgba(245,158,11,0.25)'  },
  governance:         { bg: 'rgba(16,185,129,0.10)',  text: '#10b981', border: 'rgba(16,185,129,0.25)'  },
  relationship_map:   { bg: 'rgba(139,92,246,0.10)',  text: '#8b5cf6', border: 'rgba(139,92,246,0.25)'  },
  review_needed:      { bg: 'rgba(249,115,22,0.10)',  text: '#f97316', border: 'rgba(249,115,22,0.25)'  },
  metadata_lookup:    { bg: 'rgba(99,102,241,0.10)',  text: '#6366f1', border: 'rgba(99,102,241,0.25)'  },
  no_data:            { bg: 'rgba(248,113,113,0.10)', text: '#f87171', border: 'rgba(248,113,113,0.25)' },
  access_restricted:  { bg: 'rgba(248,113,113,0.10)', text: '#f87171', border: 'rgba(248,113,113,0.25)' },
  unknown_intent:     { bg: 'rgba(148,163,184,0.10)', text: '#94a3b8', border: 'rgba(148,163,184,0.25)' },
  // core.answering.models.AnswerType values (enterprise_answer.answer_type) —
  // distinct vocabulary from the legacy business_answer types above.
  live_query:         { bg: 'rgba(16,185,129,0.10)',  text: '#10b981', border: 'rgba(16,185,129,0.25)'  },
  live_metadata:      { bg: 'rgba(99,102,241,0.10)',  text: '#6366f1', border: 'rgba(99,102,241,0.25)'  },
  restricted:         { bg: 'rgba(248,113,113,0.10)', text: '#f87171', border: 'rgba(248,113,113,0.25)' },
  unknown:            { bg: 'rgba(148,163,184,0.10)', text: '#94a3b8', border: 'rgba(148,163,184,0.25)' },
  clarification_needed: { bg: 'rgba(245,158,11,0.10)', text: '#f59e0b', border: 'rgba(245,158,11,0.25)' },
}

function BusinessAnswerBlock({ answer: ba, C }) {
  if (!ba) return null
  const typeColors = ANSWER_TYPE_COLORS[ba.answer_type] ?? ANSWER_TYPE_COLORS.metadata_lookup
  const conf       = ba.confidence ?? 0
  const typeLabel  = (ba.answer_type ?? 'unknown').replace(/_/g, ' ').replace(/\b\w/g, c => c.toUpperCase())

  return (
    <div style={{ background: C.surface, border: `1px solid ${C.border}`, borderRadius: '14px', padding: '20px', marginBottom: '12px', boxShadow: '0 2px 8px rgba(0,0,0,0.05)' }}>

      {/* header */}
      <div style={{ display: 'flex', alignItems: 'center', gap: '8px', marginBottom: '12px' }}>
        <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="#10b981" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round"><polyline points="20 6 9 17 4 12"/></svg>
        <span style={{ fontSize: '0.57rem', fontWeight: '700', color: C.textMuted, textTransform: 'uppercase', letterSpacing: '0.14em' }}>Business Answer</span>
        <span style={{ marginLeft: 'auto', background: typeColors.bg, border: `1px solid ${typeColors.border}`, color: typeColors.text, borderRadius: '20px', padding: '2px 9px', fontSize: '0.60rem', fontWeight: '700' }}>{typeLabel}</span>
      </div>

      {/* answer text */}
      <div style={{ fontSize: '0.90rem', fontWeight: '500', color: C.text, lineHeight: 1.55, marginBottom: '12px' }}>{ba.answer}</div>

      {/* source summary + confidence row */}
      <div style={{ display: 'grid', gridTemplateColumns: '1fr auto', gap: '12px', alignItems: 'center', marginBottom: (ba.limitations?.length > 0 || ba.next_suggested_action) ? '12px' : 0 }}>
        <div style={{ fontSize: '0.70rem', color: C.textMuted }}>{ba.source_summary}</div>
        <div style={{ display: 'flex', alignItems: 'center', gap: '7px', flexShrink: 0 }}>
          <div style={{ width: '80px', height: '5px', background: C.border, borderRadius: '3px', overflow: 'hidden' }}>
            <div style={{ height: '100%', width: `${Math.round(conf * 100)}%`, background: conf >= 0.7 ? '#10b981' : conf >= 0.4 ? '#f59e0b' : '#f87171', borderRadius: '3px', transition: 'width 0.4s ease' }} />
          </div>
          <span style={{ fontSize: '0.68rem', fontWeight: '700', color: C.textSec, minWidth: '28px' }}>{Math.round(conf * 100)}%</span>
        </div>
      </div>

      {/* limitations */}
      {ba.limitations?.length > 0 && (
        <div style={{ borderTop: `1px solid ${C.border}`, paddingTop: '10px', display: 'flex', flexDirection: 'column', gap: '4px', marginBottom: ba.next_suggested_action ? '10px' : 0 }}>
          {ba.limitations.map((lim, i) => (
            <div key={i} style={{ display: 'flex', gap: '6px', alignItems: 'flex-start', fontSize: '0.70rem', color: C.textMuted, lineHeight: 1.45 }}>
              <span style={{ flexShrink: 0, marginTop: '1px', color: '#f59e0b' }}>&#9432;</span>
              <span>{lim}</span>
            </div>
          ))}
        </div>
      )}

      {/* next suggested action */}
      {ba.next_suggested_action && (
        <div style={{ padding: '9px 13px', background: 'rgba(99,102,241,0.05)', border: '1px solid rgba(99,102,241,0.15)', borderRadius: '9px', display: 'flex', gap: '8px', alignItems: 'center' }}>
          <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="#6366f1" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round"><polyline points="9 18 15 12 9 6"/></svg>
          <span style={{ fontSize: '0.72rem', color: C.text, lineHeight: 1.4 }}>{ba.next_suggested_action}</span>
        </div>
      )}
    </div>
  )
}

// Renders result.enterprise_answer (core.answering.models.EnterpriseAnswer) —
// the Enterprise Answer Layer's deterministic, evidence-grounded answer,
// preferred over the legacy business_answer whenever present (e.g. SQL_REQUEST/
// live_query results, which business_answer has no dedicated branch for and
// otherwise renders as a generic "Catalog metadata retrieved..." fallback).
// Note: enterprise_answer.confidence is already 0-100 (unlike business_answer's
// 0-1 scale) — do not multiply by 100 again.
// Milestone M-25 — Enterprise Answer Value Rendering: bounded preview table
// for grouped/ranked/tabular result_preview rows. Row objects already carry
// business-labeled keys from core/answering/result_formatter.py — no raw
// column identifiers to hide here.
const RESULT_PREVIEW_PAGE_SIZE = 25

function ResultPreviewTable({ rows, C }) {
  const [page, setPage] = useState(0)
  // A new question/result replaces `rows` entirely — always land back on
  // page 1 rather than carrying over a stale page index into unrelated data.
  useEffect(() => { queueMicrotask(() => setPage(0)) }, [rows])

  if (!rows?.length) return null
  const columns = Object.keys(rows[0])
  const pageCount = Math.max(1, Math.ceil(rows.length / RESULT_PREVIEW_PAGE_SIZE))
  const currentPage = Math.min(page, pageCount - 1)
  const start = currentPage * RESULT_PREVIEW_PAGE_SIZE
  const shown = rows.slice(start, start + RESULT_PREVIEW_PAGE_SIZE)

  // Wide result sets (many business/technical columns) must scroll
  // horizontally within their own bounded container instead of expanding the
  // page. min-width: 0 here (and on every flex/grid ancestor up through
  // App.jsx's post-login shell) is load-bearing: a flex item's default
  // min-width is its content's min-content size, so without it this
  // container — and everything above it — would refuse to shrink below the
  // wide table's natural width and grow the whole page instead of scrolling.
  // A generous viewport-relative max-height (with its own overflow-y) keeps
  // results readable at a glance instead of the old fixed 420px box, while
  // still bounding runaway growth for very large previews. The header row
  // is sticky (both axes) and the first column is sticky so the row's
  // identifying value and column names stay visible while scrolling in
  // either direction. Row volume itself is bounded by paging
  // (RESULT_PREVIEW_PAGE_SIZE rows at a time) rather than rendering the
  // entire preview inline.
  return (
    <div style={{ borderTop: `1px solid ${C.border}`, paddingTop: '10px', marginBottom: '12px', minWidth: 0 }}>
      <div style={{ width: '100%', maxWidth: '100%', minWidth: 0, maxHeight: '70vh', overflow: 'auto', border: `1px solid ${C.border}`, borderRadius: '8px' }}>
        <table style={{ borderCollapse: 'collapse', fontSize: '0.74rem', width: 'max-content', minWidth: '100%' }}>
          <thead>
            <tr>
              {columns.map((col, idx) => (
                <th key={col} style={{
                  textAlign: 'left', padding: '6px 10px', color: C.textMuted, fontWeight: '700',
                  borderBottom: `1px solid ${C.border}`, whiteSpace: 'nowrap', background: C.surface,
                  position: 'sticky', top: 0, zIndex: idx === 0 ? 3 : 2,
                  ...(idx === 0 ? { left: 0, borderRight: `1px solid ${C.border}` } : {}),
                }}>{col}</th>
              ))}
            </tr>
          </thead>
          <tbody>
            {shown.map((row, i) => (
              <tr key={start + i}>
                {columns.map((col, idx) => (
                  <td key={col} style={{
                    padding: '6px 10px', color: C.text, borderBottom: `1px solid ${C.border}`, whiteSpace: 'nowrap',
                    ...(idx === 0 ? { position: 'sticky', left: 0, zIndex: 1, background: C.surface, borderRight: `1px solid ${C.border}` } : {}),
                  }}>{String(row[col] ?? '')}</td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: '10px', flexWrap: 'wrap', marginTop: '6px' }}>
        <span style={{ fontSize: '0.66rem', color: C.textMuted }}>
          {rows.length} results • Showing {start + 1}–{Math.min(start + RESULT_PREVIEW_PAGE_SIZE, rows.length)}
        </span>
        {pageCount > 1 && (
          <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
            <button type="button" onClick={() => setPage(p => Math.max(0, p - 1))} disabled={currentPage === 0}
              style={{ fontSize: '0.66rem', fontWeight: '600', padding: '4px 11px', borderRadius: '6px', border: `1px solid ${C.border}`, background: C.surface, color: currentPage === 0 ? C.textMuted : C.text, cursor: currentPage === 0 ? 'default' : 'pointer', fontFamily: FONT }}>
              Previous
            </button>
            <span style={{ fontSize: '0.66rem', color: C.textMuted }}>Page {currentPage + 1} of {pageCount}</span>
            <button type="button" onClick={() => setPage(p => Math.min(pageCount - 1, p + 1))} disabled={currentPage === pageCount - 1}
              style={{ fontSize: '0.66rem', fontWeight: '600', padding: '4px 11px', borderRadius: '6px', border: `1px solid ${C.border}`, background: C.surface, color: currentPage === pageCount - 1 ? C.textMuted : C.text, cursor: currentPage === pageCount - 1 ? 'default' : 'pointer', fontFamily: FONT }}>
              Next
            </button>
          </div>
        )}
      </div>
    </div>
  )
}

function EnterpriseAnswerBlock({ answer: ea, C }) {
  if (!ea) return null
  const typeColors = ANSWER_TYPE_COLORS[ea.answer_type] ?? ANSWER_TYPE_COLORS.metadata_lookup
  const typeLabel  = (ea.answer_type ?? 'unknown').replace(/_/g, ' ').replace(/\b\w/g, c => c.toUpperCase())
  const limitations = ea.limitations ?? []
  const nextActions = ea.next_actions ?? []
  const resultPreview = ea.result_preview ?? []
  const appliedFilters = ea.applied_filters ?? []
  const dateContext = ea.date_context ?? null
  const insight = ea.insight ?? null
  const chart = ea.chart ?? null
  const kpi = ea.kpi ?? null
  const groundedInsights = ea.insights ?? []

  return (
    <div style={{ background: C.surface, border: `1px solid ${C.border}`, borderRadius: '14px', padding: '20px', marginBottom: '12px', boxShadow: '0 2px 8px rgba(0,0,0,0.05)' }}>

      {/* header */}
      <div style={{ display: 'flex', alignItems: 'center', gap: '8px', marginBottom: '12px' }}>
        <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="#10b981" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round"><polyline points="20 6 9 17 4 12"/></svg>
        <span style={{ fontSize: '0.57rem', fontWeight: '700', color: C.textMuted, textTransform: 'uppercase', letterSpacing: '0.14em' }}>Enterprise Answer</span>
        <span style={{ marginLeft: 'auto', background: typeColors.bg, border: `1px solid ${typeColors.border}`, color: typeColors.text, borderRadius: '20px', padding: '2px 9px', fontSize: '0.60rem', fontWeight: '700' }}>{typeLabel}</span>
      </div>

      {/* answer text */}
      <div style={{ fontSize: '0.90rem', fontWeight: '500', color: C.text, lineHeight: 1.55, marginBottom: ea.summary ? '6px' : '12px' }}>{ea.answer}</div>

      {/* summary */}
      {ea.summary && (
        <div style={{ fontSize: '0.72rem', color: C.textMuted, lineHeight: 1.45, marginBottom: '12px' }}>{ea.summary}</div>
      )}

      {/* period-comparison insight — Day 4, Capability 2 (Business Insights).
          Only present for a time-bound single-scalar aggregate answer where
          data.insight_service actually computed one; absent everywhere else. */}
      {insight && (
        <div style={{
          display: 'flex', alignItems: 'center', gap: '7px', marginBottom: '12px',
          padding: '7px 12px', borderRadius: '9px',
          background: insight.direction === 'up' ? 'rgba(16,185,129,0.08)' : insight.direction === 'down' ? 'rgba(248,113,113,0.08)' : 'rgba(148,163,184,0.08)',
          border: `1px solid ${insight.direction === 'up' ? 'rgba(16,185,129,0.22)' : insight.direction === 'down' ? 'rgba(248,113,113,0.22)' : C.border}`,
        }}>
          <span style={{ fontSize: '0.8rem', lineHeight: 1 }} aria-hidden="true">
            {insight.direction === 'up' ? '▲' : insight.direction === 'down' ? '▼' : '▪'}
          </span>
          <span style={{ fontSize: '0.74rem', fontWeight: '700', color: insight.direction === 'up' ? '#10b981' : insight.direction === 'down' ? '#f87171' : C.textSec }}>
            {insight.percent_change > 0 ? '+' : ''}{insight.percent_change}%
          </span>
          <span style={{ fontSize: '0.72rem', color: C.textMuted }}>{insight.label}</span>
        </div>
      )}

      {/* applied filters + date context */}
      {(appliedFilters.length > 0 || dateContext) && (
        <div style={{ display: 'flex', flexWrap: 'wrap', gap: '5px', marginBottom: '12px' }}>
          {dateContext && (
            <span title={`${dateContext.start ?? ''} – ${dateContext.end ?? ''}`} style={{ background: 'rgba(16,185,129,0.08)', border: '1px solid rgba(16,185,129,0.18)', borderRadius: '20px', padding: '3px 9px', fontSize: '0.65rem', color: C.textSec, fontWeight: '500' }}>
              📅 {dateContext.label}
            </span>
          )}
          {appliedFilters.map((f, i) => (
            <span key={i} style={{ background: 'rgba(148,163,184,0.10)', border: `1px solid ${C.border}`, borderRadius: '20px', padding: '3px 9px', fontSize: '0.65rem', color: C.textSec, fontWeight: '500' }}>
              {f.label} {f.operator} {Array.isArray(f.value) ? f.value.join(' – ') : String(f.value ?? '')}
            </span>
          ))}
        </div>
      )}

      {/* automatic chart — Day 4, Capability 3. Only present when
          core.answering.result_formatter._build_chart_spec determined the
          result shape supports one (grouped/ranked, single dimension, a
          legible category count); reuses the existing ChartSection
          component unchanged — the table below is never removed, only
          supplemented. */}
      {chart && (
        <div style={{ marginBottom: '12px', padding: '12px', border: `1px solid ${C.border}`, borderRadius: '10px' }}>
          {chart.title && <div style={{ fontSize: '0.68rem', fontWeight: '700', color: C.textSec, marginBottom: '8px' }}>{chart.title}</div>}
          <ChartSection chart={chart} C={C} />
        </div>
      )}

      {/* single-scalar KPI — Phase 7, Step 6G. Mutually exclusive with
          `chart` by construction (core.sqlservice.result_chart_builder):
          a chart needs >=2 rows, a kpi needs exactly 1. Reuses the
          existing KpiCard component unchanged. */}
      {kpi && (
        <div style={{ marginBottom: '12px' }}>
          <KpiCard kpi={{ label: kpi.label, value: kpi.value, format: 'number' }} C={C} />
        </div>
      )}

      {/* grounded result insights — Phase 7, Step 6G. Populated by
          core.sqlservice.result_insight_builder (deterministic) or, for a
          genuinely complex result, the grounded AI answer path — every
          entry already cites real row/column evidence
          (core.sqlservice.answer_generation_service.AnswerInsight), never
          rendered for a truncated result (the builder itself suppresses
          those). Additive alongside the legacy `insight` badge above;
          does not replace it. */}
      {groundedInsights.length > 0 && (
        <div style={{ marginBottom: '12px' }}>
          <div style={{ display: 'flex', flexDirection: 'column', gap: '6px' }}>
            {groundedInsights.map((ins, i) => (
              <div key={i} style={{ padding: '9px 13px', background: 'rgba(99,102,241,0.04)', border: '1px solid rgba(99,102,241,0.14)', borderRadius: '9px' }}>
                <div style={{ fontSize: '0.73rem', fontWeight: '700', color: C.text, marginBottom: '2px' }}>{ins.title}</div>
                <div style={{ fontSize: '0.71rem', color: C.textSec, lineHeight: 1.45 }}>{ins.finding}</div>
              </div>
            ))}
          </div>
          <div style={{ fontSize: '0.63rem', color: C.textMuted, marginTop: '5px', fontStyle: 'italic' }}>
            Based on the returned result rows.
          </div>
        </div>
      )}

      {/* result preview (grouped/ranked/tabular rows) */}
      <ResultPreviewTable rows={resultPreview} C={C} />

      {/* truncation notice */}
      {ea.truncation_notice && (
        <div style={{ display: 'flex', gap: '6px', alignItems: 'flex-start', fontSize: '0.70rem', color: C.textMuted, lineHeight: 1.45, marginBottom: '12px' }}>
          <span style={{ flexShrink: 0, marginTop: '1px', color: '#f59e0b' }}>&#9432;</span>
          <span>{ea.truncation_notice}</span>
        </div>
      )}

      {/* limitations — kept primary: a caveat about the answer itself
          (e.g. "results truncated", "connection timed out") is user-facing,
          not a developer diagnostic, so it stays outside Technical details. */}
      {limitations.length > 0 && (
        <div style={{ borderTop: `1px solid ${C.border}`, paddingTop: '10px', display: 'flex', flexDirection: 'column', gap: '4px', marginBottom: nextActions.length ? '10px' : 0 }}>
          {limitations.map((lim, i) => (
            <div key={i} style={{ display: 'flex', gap: '6px', alignItems: 'flex-start', fontSize: '0.70rem', color: C.textMuted, lineHeight: 1.45 }}>
              <span style={{ flexShrink: 0, marginTop: '1px', color: '#f59e0b' }}>&#9432;</span>
              <span>{lim}</span>
            </div>
          ))}
        </div>
      )}

      {/* next actions */}
      {nextActions.length > 0 && (
        <div style={{ display: 'flex', flexDirection: 'column', gap: '6px' }}>
          {nextActions.map((action, i) => (
            <div key={i} style={{ padding: '9px 13px', background: 'rgba(99,102,241,0.05)', border: '1px solid rgba(99,102,241,0.15)', borderRadius: '9px', display: 'flex', gap: '8px', alignItems: 'center' }}>
              <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="#6366f1" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round"><polyline points="9 18 15 12 9 6"/></svg>
              <span style={{ fontSize: '0.72rem', color: C.text, lineHeight: 1.4 }}>{action}</span>
            </div>
          ))}
        </div>
      )}
    </div>
  )
}

// Renders EnterpriseAnswer.clarification (Phase 6.6 Enterprise Clarification
// Intelligence) — shown instead of EnterpriseAnswerBlock whenever answer_type
// is "clarification_needed". clarification.options is a flat list that can
// span multiple ambiguous terms (each option carries its own `term`), so
// options are grouped by term and one selection is required per group before
// resubmission is allowed. The backend matches a resubmitted selection by
// {term, table_fqn, column_name} (core.orchestrator.context_builder
// ._apply_clarification_overrides) — NOT by the option's `id`, which exists
// only as a UI list key.
export function ClarificationCard({ clarification, wsInput, C, onBack, onResolve, onCancel, onFreeTextResolve, resubmitted }) {
  const options = clarification?.options ?? []
  const [picked, setPicked]           = useState({})   // term -> option
  const [expanded, setExpanded]       = useState({})   // option.id -> bool
  const [submitting, setSubmitting]   = useState(false)
  // Phase 7, Step 5 audit fix — Part 7: a clarification with no structured
  // `options` (e.g. the AI-first pipeline's plain clarification_question,
  // which never populates `options`) used to leave "Try a different
  // question" as the ONLY path forward, which discards the pending
  // clarification state and starts an unrelated query instead of actually
  // answering it. onFreeTextResolve (wired to the same handleComposerAsk
  // call a recognized follow-up question already uses) sends the typed
  // text as a plain message with the pending conversation_state still
  // attached, which the backend's own is_new_pipeline_clarification_resume
  // routing (api/v1/composer.py) already treats as the clarification's
  // answer -- combining it with the original question via
  // build_combined_clarification_question. No backend change was needed;
  // only the frontend never exposed this already-supported path.
  const [freeTextAnswer, setFreeTextAnswer] = useState('')
  // Phase 2, Step 7b — opt-in only: this writes a persistent, source-scoped
  // HUMAN_APPROVED governance record (data/concept_mapping_service.py::
  // promote_clarification_selection), so it defaults unchecked rather than
  // silently remembering every resolved choice.
  const [remember, setRemember]       = useState(false)

  const groups = {}
  for (const opt of options) {
    const t = opt.term ?? ''
    if (!groups[t]) groups[t] = []
    groups[t].push(opt)
  }
  const termNames = Object.keys(groups)
  const allPicked = termNames.length > 0 && termNames.every(t => picked[t])

  function handleSubmit(e) {
    e.preventDefault()
    if (!allPicked || submitting) return
    setSubmitting(true)
    const selections = termNames.map(t => ({
      term:        picked[t].term,
      table_fqn:   picked[t].table_fqn,
      column_name: picked[t].column_name ?? null,
    }))
    onResolve(selections, remember)
  }

  function handleCancel() {
    if (submitting) return
    setSubmitting(true)
    onCancel()
  }

  function handleFreeTextSubmit(e) {
    e.preventDefault()
    const trimmed = freeTextAnswer.trim()
    if (!trimmed || submitting || !onFreeTextResolve) return
    setSubmitting(true)
    onFreeTextResolve(trimmed)
  }

  return (
    <div style={{ background: C.surface, border: `1px solid ${C.border}`, borderRadius: '14px', padding: '20px', marginBottom: '12px', boxShadow: '0 2px 8px rgba(0,0,0,0.05)', animation: 'ws-fadeup 0.35s ease' }}>

      {/* header */}
      <div style={{ display: 'flex', alignItems: 'center', gap: '8px', marginBottom: '12px' }}>
        <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="#f59e0b" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round"><circle cx="12" cy="12" r="10"/><line x1="12" y1="16" x2="12" y2="12"/><line x1="12" y1="8" x2="12.01" y2="8"/></svg>
        <span style={{ fontSize: '0.57rem', fontWeight: '700', color: C.textMuted, textTransform: 'uppercase', letterSpacing: '0.14em' }}>Clarification Needed</span>
      </div>

      {/* original question */}
      {wsInput && (
        <div style={{ fontSize: '0.72rem', color: C.textMuted, marginBottom: '10px' }}>For: <span style={{ color: C.text, fontWeight: '600' }}>{wsInput}</span></div>
      )}

      {/* stale/invalid-selection notice */}
      {resubmitted && (
        <div role="status" style={{ display: 'flex', gap: '8px', alignItems: 'flex-start', padding: '9px 13px', background: 'rgba(248,113,113,0.06)', border: '1px solid rgba(248,113,113,0.2)', borderRadius: '9px', marginBottom: '12px', fontSize: '0.74rem', color: C.text, lineHeight: 1.45 }}>
          <span style={{ flexShrink: 0, marginTop: '1px', color: '#f87171' }}>&#9888;</span>
          <span>That selection couldn&apos;t be applied. Please choose again below.</span>
        </div>
      )}

      {/* reason */}
      <div style={{ fontSize: '0.86rem', fontWeight: '500', color: C.text, lineHeight: 1.55, marginBottom: '16px' }}>
        {clarification?.reason || 'This question could be answered in more than one way.'}
      </div>

      {termNames.length === 0 ? (
        // No structured options -- always offer a free-text answer box
        // (Part 7) rather than leaving "Try a different question" as the
        // only path, which would silently abandon the pending clarification.
        <form onSubmit={handleFreeTextSubmit}>
          <label htmlFor="clarification-free-text-answer" style={{ display: 'block', fontSize: '0.68rem', fontWeight: '700', color: C.textMuted, textTransform: 'uppercase', letterSpacing: '0.08em', marginBottom: '8px' }}>
            Your answer
          </label>
          <textarea
            id="clarification-free-text-answer"
            value={freeTextAnswer}
            onChange={e => setFreeTextAnswer(e.target.value)}
            disabled={submitting}
            placeholder="Type your answer to the question above…"
            rows={2}
            style={{ width: '100%', boxSizing: 'border-box', resize: 'vertical', border: `1px solid ${C.border}`, borderRadius: '9px', padding: '10px 12px', fontSize: '0.82rem', color: C.text, background: C.bg, fontFamily: FONT, marginBottom: '14px' }}
          />
          <div style={{ display: 'flex', gap: '10px', flexWrap: 'wrap' }}>
            <button
              type="submit"
              disabled={!freeTextAnswer.trim() || submitting || !onFreeTextResolve}
              style={{ background: (!freeTextAnswer.trim() || submitting || !onFreeTextResolve) ? C.border : '#6366f1', border: 'none', borderRadius: '9px', padding: '9px 18px', fontSize: '0.78rem', fontWeight: '700', color: (!freeTextAnswer.trim() || submitting || !onFreeTextResolve) ? C.textMuted : '#fff', cursor: (!freeTextAnswer.trim() || submitting || !onFreeTextResolve) ? 'not-allowed' : 'pointer', fontFamily: FONT }}
            >
              {submitting ? 'Submitting…' : 'Send answer'}
            </button>
            <button
              type="button"
              onClick={handleCancel}
              disabled={submitting}
              style={{ background: C.bg, border: `1px solid ${C.border}`, borderRadius: '9px', padding: '9px 16px', fontSize: '0.78rem', fontWeight: '600', color: C.textSec, cursor: submitting ? 'not-allowed' : 'pointer', fontFamily: FONT }}
            >
              Cancel clarification
            </button>
            <button
              type="button"
              onClick={onBack}
              disabled={submitting}
              style={{ background: 'none', border: 'none', padding: '9px 4px', fontSize: '0.78rem', fontWeight: '600', color: C.textMuted, cursor: submitting ? 'not-allowed' : 'pointer', fontFamily: FONT }}
            >
              Ask a different question
            </button>
          </div>
        </form>
      ) : (
        <form onSubmit={handleSubmit}>
          {termNames.map(term => (
            <fieldset key={term} role="radiogroup" aria-label={`Choose a match for "${term}"`} style={{ border: `1px solid ${C.border}`, borderRadius: '10px', padding: '12px 14px', marginBottom: '12px' }}>
              <legend style={{ fontSize: '0.68rem', fontWeight: '700', color: C.textMuted, textTransform: 'uppercase', letterSpacing: '0.08em', padding: '0 4px' }}>For &quot;{term}&quot;</legend>
              <div style={{ display: 'flex', flexDirection: 'column', gap: '8px', marginTop: '6px' }}>
                {groups[term].map(opt => {
                  const isChecked = picked[term]?.id === opt.id
                  return (
                    <div key={opt.id} style={{ border: `1px solid ${isChecked ? '#6366f1' : C.border}`, borderRadius: '9px', padding: '10px 12px', background: isChecked ? 'rgba(99,102,241,0.06)' : 'transparent' }}>
                      <label style={{ display: 'flex', alignItems: 'flex-start', gap: '9px', cursor: 'pointer' }}>
                        <input
                          type="radio"
                          name={`clarify_${term}`}
                          value={opt.id}
                          checked={isChecked}
                          onChange={() => setPicked(p => ({ ...p, [term]: opt }))}
                          style={{ marginTop: '3px', flexShrink: 0 }}
                        />
                        <span style={{ fontSize: '0.82rem', fontWeight: '600', color: C.text }}>{opt.label}</span>
                      </label>
                      {/* Accuracy Program A4, Fix #3 — additive, optional metadata.
                          Hidden entirely for older responses without these fields. */}
                      {(opt.object_type || opt.domain || opt.entity) && (
                        <div style={{ display: 'flex', flexWrap: 'wrap', gap: '6px', marginTop: '5px', marginLeft: '24px' }}>
                          {opt.object_type && (
                            <span style={{ fontSize: '0.6rem', fontWeight: '700', color: C.textMuted, border: `1px solid ${C.border}`, borderRadius: '5px', padding: '1px 6px', textTransform: 'uppercase', letterSpacing: '0.04em' }}>
                              {opt.object_type}
                            </span>
                          )}
                          {opt.domain && (
                            <span style={{ fontSize: '0.62rem', fontWeight: '600', color: C.textMuted, border: `1px solid ${C.border}`, borderRadius: '5px', padding: '1px 6px' }}>
                              {opt.domain}
                            </span>
                          )}
                          {opt.entity && (
                            <span style={{ fontSize: '0.62rem', fontWeight: '600', color: C.textMuted, border: `1px solid ${C.border}`, borderRadius: '5px', padding: '1px 6px' }}>
                              {opt.entity}
                            </span>
                          )}
                        </div>
                      )}
                      {Array.isArray(opt.why_relevant) && opt.why_relevant.length > 0 && (
                        <ul style={{ margin: '5px 0 0 24px', padding: 0, listStyle: 'none' }}>
                          {opt.why_relevant.slice(0, 3).map((reason, i) => (
                            <li key={i} style={{ fontSize: '0.68rem', color: C.textMuted, lineHeight: 1.5 }}>
                              &#8226; {reason}
                            </li>
                          ))}
                        </ul>
                      )}
                      <button
                        type="button"
                        onClick={() => setExpanded(x => ({ ...x, [opt.id]: !x[opt.id] }))}
                        aria-expanded={!!expanded[opt.id]}
                        style={{ background: 'none', border: 'none', cursor: 'pointer', padding: '4px 0 0 24px', color: C.textMuted, fontFamily: FONT, fontSize: '0.65rem', fontWeight: '600', textDecoration: 'underline' }}
                      >
                        {expanded[opt.id] ? 'Hide technical details' : 'Show technical details'}
                      </button>
                      {expanded[opt.id] && (
                        <div style={{ marginLeft: '24px', marginTop: '6px', fontSize: '0.68rem', color: C.textMuted, fontFamily: MONO, lineHeight: 1.6 }}>
                          <div>{opt.table_fqn}</div>
                          {opt.column_name && <div>Column: {opt.column_name}</div>}
                          {typeof opt.score === 'number' && <div>Confidence: {opt.score.toFixed(2)}</div>}
                        </div>
                      )}
                    </div>
                  )
                })}
              </div>
            </fieldset>
          ))}

          {clarification?.expected_impact && (
            <div style={{ fontSize: '0.70rem', color: C.textMuted, lineHeight: 1.45, marginBottom: '14px' }}>{clarification.expected_impact}</div>
          )}

          <label style={{ display: 'flex', alignItems: 'flex-start', gap: '8px', marginBottom: '14px', cursor: submitting ? 'not-allowed' : 'pointer' }}>
            <input
              type="checkbox"
              checked={remember}
              disabled={submitting}
              onChange={() => setRemember(r => !r)}
              style={{ marginTop: '2px', flexShrink: 0 }}
            />
            <span style={{ fontSize: '0.74rem', color: C.textMuted, lineHeight: 1.4 }}>
              Remember {termNames.length > 1 ? 'these choices' : 'this choice'} for future questions on this data source
            </span>
          </label>

          <div style={{ display: 'flex', gap: '10px', flexWrap: 'wrap' }}>
            <button
              type="submit"
              disabled={!allPicked || submitting}
              style={{ background: (!allPicked || submitting) ? C.border : '#6366f1', border: 'none', borderRadius: '9px', padding: '9px 18px', fontSize: '0.78rem', fontWeight: '700', color: (!allPicked || submitting) ? C.textMuted : '#fff', cursor: (!allPicked || submitting) ? 'not-allowed' : 'pointer', fontFamily: FONT }}
            >
              {submitting ? 'Submitting…' : termNames.length > 1 ? 'Use these options' : 'Use this option'}
            </button>
            <button
              type="button"
              onClick={handleCancel}
              disabled={submitting}
              style={{ background: C.bg, border: `1px solid ${C.border}`, borderRadius: '9px', padding: '9px 16px', fontSize: '0.78rem', fontWeight: '600', color: C.textSec, cursor: submitting ? 'not-allowed' : 'pointer', fontFamily: FONT }}
            >
              Cancel clarification
            </button>
            <button
              type="button"
              onClick={onBack}
              disabled={submitting}
              style={{ background: 'none', border: 'none', padding: '9px 4px', fontSize: '0.78rem', fontWeight: '600', color: C.textMuted, cursor: submitting ? 'not-allowed' : 'pointer', fontFamily: FONT }}
            >
              Ask a different question
            </button>
          </div>
        </form>
      )}
    </div>
  )
}

function ComposerReportCard({ result, wsInput, C, onBack, onOpenReport }) {
  const reportId    = result.report_id ?? null
  const reportTitle = result.report_title ?? result.business_answer?.report_title ?? 'Intelligence Report'
  const ba          = result.business_answer ?? {}
  const status      = result.status ?? 'unknown'
  const failed      = ba.answer_type === 'report_generation_failed'

  const limitationColor = '#f59e0b'
  const successColor    = '#10b981'
  const failColor       = '#f87171'

  return (
    <div style={{ animation: 'ws-fadeup 0.35s ease' }}>

      {/* Header */}
      <div style={{ display: 'flex', alignItems: 'center', gap: '12px', marginBottom: '16px', padding: '14px 20px', background: C.surface, border: `1px solid ${C.border}`, borderRadius: '16px', boxShadow: '0 2px 12px rgba(0,0,0,0.07)' }}>
        <div style={{ width: '40px', height: '40px', borderRadius: '11px', background: failed ? 'rgba(248,113,113,0.12)' : 'rgba(16,185,129,0.12)', display: 'flex', alignItems: 'center', justifyContent: 'center', flexShrink: 0 }}>
          {failed
            ? <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke={failColor} strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><circle cx="12" cy="12" r="10"/><line x1="12" y1="8" x2="12" y2="12"/><line x1="12" y1="16" x2="12.01" y2="16"/></svg>
            : <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke={successColor} strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><polyline points="20 6 9 17 4 12"/></svg>
          }
        </div>
        <div style={{ flex: 1, minWidth: 0 }}>
          <div style={{ fontSize: '0.55rem', fontWeight: '700', color: failed ? failColor : successColor, textTransform: 'uppercase', letterSpacing: '0.14em', marginBottom: '2px' }}>
            {failed ? 'Report Generation Failed' : 'Enterprise Report Generated'}
          </div>
          <div style={{ fontSize: '0.88rem', fontWeight: '600', color: C.text, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{wsInput || 'Report generation'}</div>
        </div>
        <div style={{ display: 'flex', alignItems: 'center', gap: '8px', flexShrink: 0 }}>
          <span style={{ display: 'inline-flex', alignItems: 'center', gap: '5px', background: failed ? 'rgba(248,113,113,0.08)' : 'rgba(16,185,129,0.08)', border: `1px solid ${failed ? failColor : successColor}55`, borderRadius: '20px', padding: '3px 10px', fontSize: '0.57rem', fontWeight: '700', color: failed ? failColor : successColor, textTransform: 'uppercase', letterSpacing: '0.08em' }}>
            <span style={{ width: '5px', height: '5px', borderRadius: '50%', background: failed ? failColor : successColor }} />
            {status}
          </span>
          <button onClick={onBack} style={{ background: C.bg, border: `1px solid ${C.border}`, borderRadius: '9px', padding: '6px 14px', fontSize: '0.72rem', fontWeight: '600', color: C.textSec, cursor: 'pointer', fontFamily: FONT }}>
            New Query
          </button>
        </div>
      </div>

      {failed ? (
        /* ── Error state ── */
        <div style={{ background: 'rgba(248,113,113,0.07)', border: '1px solid rgba(248,113,113,0.25)', borderRadius: '14px', padding: '22px 24px', marginBottom: '12px' }}>
          <div style={{ fontSize: '0.57rem', fontWeight: '700', color: failColor, textTransform: 'uppercase', letterSpacing: '0.14em', marginBottom: '8px' }}>Error</div>
          <div style={{ fontSize: '0.86rem', color: C.text, lineHeight: 1.6 }}>{ba.answer}</div>
          {(ba.limitations ?? []).map((l, i) => (
            <div key={i} style={{ display: 'flex', gap: '6px', alignItems: 'flex-start', fontSize: '0.72rem', color: C.textMuted, lineHeight: 1.45, marginTop: '8px' }}>
              <span style={{ flexShrink: 0, color: limitationColor }}>&#9432;</span>
              <span>{l}</span>
            </div>
          ))}
          <div style={{ marginTop: '14px', fontSize: '0.74rem', color: '#6366f1' }}>{ba.next_suggested_action}</div>
        </div>
      ) : (
        <>
          {/* ── Report details card ── */}
          <div style={{ background: C.surface, border: '1px solid rgba(16,185,129,0.25)', borderRadius: '14px', padding: '20px 24px', marginBottom: '12px', boxShadow: '0 2px 8px rgba(16,185,129,0.07)' }}>
            <div style={{ display: 'flex', alignItems: 'center', gap: '7px', marginBottom: '16px' }}>
              <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke={successColor} strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/></svg>
              <span style={{ fontSize: '0.57rem', fontWeight: '700', color: C.textMuted, textTransform: 'uppercase', letterSpacing: '0.14em' }}>Report Details</span>
            </div>
            <div style={{ display: 'grid', gridTemplateColumns: 'auto 1fr', gap: '10px 20px', marginBottom: '16px' }}>
              <span style={{ fontSize: '0.69rem', color: C.textMuted, fontWeight: '600', alignSelf: 'center' }}>Title</span>
              <span style={{ fontSize: '0.85rem', fontWeight: '600', color: C.text }}>{reportTitle}</span>
              {reportId && (
                <>
                  <span style={{ fontSize: '0.69rem', color: C.textMuted, fontWeight: '600', alignSelf: 'center' }}>Report ID</span>
                  <span style={{ fontSize: '0.82rem', color: C.textSec, fontFamily: MONO }}>{reportId}</span>
                </>
              )}
              <span style={{ fontSize: '0.69rem', color: C.textMuted, fontWeight: '600', alignSelf: 'center' }}>Source</span>
              <span style={{ fontSize: '0.82rem', color: C.textSec }}>
                {result.executive_report
                  ? 'Live connected database — Executive Report pipeline'
                  : 'Uploaded dataset — report pipeline'}
              </span>
            </div>
            {/* Action buttons */}
            <div style={{ display: 'flex', gap: '8px', flexWrap: 'wrap' }}>
              {reportId && onOpenReport && (
                <button
                  onClick={() => onOpenReport(reportId)}
                  style={{ display: 'flex', alignItems: 'center', gap: '6px', background: successColor, border: 'none', borderRadius: '9px', padding: '8px 16px', fontSize: '0.74rem', fontWeight: '700', color: '#fff', cursor: 'pointer', fontFamily: FONT }}
                >
                  <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round"><path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6"/><polyline points="15 3 21 3 21 9"/><line x1="10" y1="14" x2="21" y2="3"/></svg>
                  View Report
                </button>
              )}
              <button
                onClick={onBack}
                style={{ background: C.bg, border: `1px solid ${C.border}`, borderRadius: '9px', padding: '8px 16px', fontSize: '0.74rem', fontWeight: '600', color: C.textSec, cursor: 'pointer', fontFamily: FONT }}
              >
                New Query
              </button>
            </div>
          </div>

          {/* ── Executive Report content: KPI cards, charts, tables, narrative ── */}
          {result.executive_report ? (
            <div style={{ background: C.surface, border: `1px solid ${C.border}`, borderRadius: '14px', padding: '18px 20px', marginBottom: '12px' }}>
              <ExecutiveReportContent report={result.executive_report} C={C} hideHeader />
            </div>
          ) : ba.answer && (
            <div style={{ background: C.surface, border: `1px solid ${C.border}`, borderRadius: '14px', padding: '18px 20px', marginBottom: '12px' }}>
              <div style={{ fontSize: '0.57rem', fontWeight: '700', color: C.textMuted, textTransform: 'uppercase', letterSpacing: '0.14em', marginBottom: '10px' }}>Executive Summary</div>
              <div style={{ fontSize: '0.86rem', color: C.text, lineHeight: 1.6 }}>{ba.answer.replace(/^Report generated successfully\.\s*/i, '')}</div>
            </div>
          )}

          {/* ── Export suggestion ── */}
          <div style={{ display: 'flex', alignItems: 'center', gap: '14px', padding: '14px 20px', background: 'rgba(99,102,241,0.06)', border: '1px solid rgba(99,102,241,0.2)', borderRadius: '14px' }}>
            <div style={{ width: '32px', height: '32px', borderRadius: '9px', background: 'rgba(99,102,241,0.14)', display: 'flex', alignItems: 'center', justifyContent: 'center', flexShrink: 0 }}>
              <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="#6366f1" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round"><polyline points="9 18 15 12 9 6"/></svg>
            </div>
            <div>
              <div style={{ fontSize: '0.57rem', fontWeight: '700', color: '#6366f1', textTransform: 'uppercase', letterSpacing: '0.12em', marginBottom: '3px' }}>Export Suggestion</div>
              <div style={{ fontSize: '0.78rem', color: C.text, lineHeight: 1.5 }}>
                {ba.next_suggested_action || 'Open the Reports tab to export as PDF, CSV, or Excel.'}
              </div>
            </div>
          </div>

          {/* ── Limitations ── */}
          {(ba.limitations ?? []).length > 0 && (
            <div style={{ marginTop: '10px', display: 'flex', flexDirection: 'column', gap: '4px' }}>
              {ba.limitations.map((l, i) => (
                <div key={i} style={{ display: 'flex', gap: '6px', alignItems: 'flex-start', fontSize: '0.70rem', color: C.textMuted, lineHeight: 1.45 }}>
                  <span style={{ flexShrink: 0, color: limitationColor, marginTop: '1px' }}>&#9432;</span>
                  <span>{l}</span>
                </div>
              ))}
            </div>
          )}
        </>
      )}
    </div>
  )
}

// Milestone M-32 — core.orchestrator.agent.AgentState.trace step names mapped
// to plain language for "How this answer was prepared". Deliberately never
// renders step.input_summary/output_summary (still concise but not written
// for an end user) or any SQL/sample values — see agent.py's own TraceStep
// docstring for what those fields may legitimately contain. An unmapped
// tool name (a future trace step this list hasn't caught up with yet) falls
// back to a humanized version of its own name rather than disappearing.
const AGENT_TRACE_LABELS = {
  resolve_intent:                'Understood the question',
  plan_and_generate_sql:         'Built a data query plan',
  prepare_selected_tables:       'Prepared metadata for selected tables',
  inspect_targeted_values:       'Checked a limited set of values',
  execute_sql:                   'Ran a read-only query',
  validate_execution_result:     'Validated the returned result',
  apply_conversation_context:    'Reused the previous question context',
  apply_clarification_selection: 'Applied your clarification',
  cancel_clarification:          'Cancelled clarification',
  request_clarification:         'Identified a clarifying question to ask',
  validate_join_path:            'Checked how the data connects',
  build_business_answer:         'Prepared the final answer',
  agent_loop:                    'Reached a processing limit',
  compute_period_comparison_insight: 'Compared against the previous period',
  // AI-first fast-path trace steps (api/v1/ai_first_composer_adapter.py).
  ai_query_planning:             'AI query planning — intent, schema, and SQL determined in one call',
}

function agentTraceStepLabel(tool) {
  return AGENT_TRACE_LABELS[tool] || String(tool || '').replace(/_/g, ' ').replace(/\b\w/g, c => c.toUpperCase())
}

// core.orchestrator.agent.AgentStatus values -> a controlled, non-alarming
// display. Only execution_failed (an actual runtime failure, not a
// deliberate stop) reads as a red/danger state — every other terminal
// status here is the agent correctly refusing or asking rather than
// guessing, so it renders as an amber/informational state instead of a
// generic error, per this milestone's "controlled refusal" requirement.
const AGENT_STATUS_DISPLAY = {
  answered:               { label: 'Answered',              color: '#10b981', bg: 'rgba(16,185,129,0.08)' },
  clarification_required: { label: 'Clarification Needed',  color: '#f59e0b', bg: 'rgba(245,158,11,0.08)' },
  safely_refused:         { label: 'Safely Refused',         color: '#f59e0b', bg: 'rgba(245,158,11,0.08)' },
  governance_blocked:     { label: 'Governance Blocked',     color: '#f59e0b', bg: 'rgba(245,158,11,0.08)' },
  execution_failed:       { label: 'Execution Failed',       color: '#f87171', bg: 'rgba(248,113,113,0.08)' },
  validation_failed:      { label: 'Validation Issue',       color: '#f59e0b', bg: 'rgba(245,158,11,0.08)' },
  step_limit_reached:     { label: 'Step Limit Reached',     color: '#f59e0b', bg: 'rgba(245,158,11,0.08)' },
  // Phase 7, Step 6F, Part 6 — a truthful badge for a reset/expired-
  // continuation turn: never shown as a green "answered" success (the
  // envelope's own top-level `status` field stays "success" by design --
  // the REQUEST succeeded, it's the CONTINUATION that could not be
  // trusted), and never conflated with a generic error either.
  state_reset:             { label: 'Context Reset',          color: '#f59e0b', bg: 'rgba(245,158,11,0.08)' },
  clarification_limit_reached: { label: 'Needs a Full Question', color: '#f59e0b', bg: 'rgba(245,158,11,0.08)' },
  question_too_long:       { label: 'Question Too Long',      color: '#f59e0b', bg: 'rgba(245,158,11,0.08)' },
  // Phase 8A — a data source that is not enabled for question answering gets
  // a typed, non-error answer (no query was run).
  not_enabled:             { label: 'Not Enabled',            color: '#f59e0b', bg: 'rgba(245,158,11,0.08)' },
}

// core.sqlservice.fast_path_generation_service's RESOLUTION_TYPE_* values
// -> a short, human-readable label for the Technical Details panel.
const FOLLOW_UP_RESOLUTION_TYPE_LABELS = {
  continuation:         'Continuation',
  clarification_answer: 'Clarification Answer',
  comparison:           'Comparison',
  drill_down:           'Drill-down',
  change:               'Changed Filter/Grouping/Sort',
  unrelated:             'New, Unrelated Question',
}

// Compact, collapsed-by-default trace — "concise... safe agent progress/
// evidence" per this milestone. Renders only step/status/mapped-label;
// never input_summary/output_summary/reason_code/duration, never SQL.
function AgentTraceSection({ trace, C }) {
  if (!trace || trace.length === 0) return null
  return (
    <details style={{ marginTop: '12px', borderTop: `1px solid ${C.border}`, paddingTop: '10px' }}>
      <summary style={{ cursor: 'pointer', fontSize: '0.68rem', color: C.textMuted, fontWeight: '600' }}>
        How this answer was prepared
      </summary>
      <div style={{ marginTop: '8px', display: 'flex', flexDirection: 'column', gap: '7px' }}>
        {trace.map((step, i) => (
          <div key={i} style={{ display: 'flex', alignItems: 'center', gap: '8px', fontSize: '0.72rem', color: C.textSec }}>
            <span style={{
              width: '6px', height: '6px', borderRadius: '50%', flexShrink: 0,
              background: step.status === 'ok' ? '#10b981' : step.status === 'blocked' ? '#f59e0b' : '#f87171',
            }} />
            <span>{agentTraceStepLabel(step.tool)}</span>
          </div>
        ))}
      </div>
    </details>
  )
}

// Milestone M-32 — the one place a follow-up question ("What about last
// quarter?", "Only California.", "Top 5 instead.", "Break it down by
// department.") can be asked without losing conversation_state. Only shown
// when the backend actually returned a conversation_state to continue —
// i.e. only after a real SQL-answerable turn (core.orchestrator.agent
// AgentState with a resolved query_plan), never for catalog/governance/
// dictionary answers or a clarification/report card. The frontend never
// parses or validates the follow-up phrase itself — that is entirely the
// backend's job (core.orchestrator.agent._classify_follow_up); this box
// just sends whatever the user types as the next question, exactly like a
// fresh question, with conversation_state attached.
function FollowUpBox({ onAsk, C }) {
  const [value, setValue] = useState('')

  function submit(e) {
    e.preventDefault()
    const trimmed = value.trim()
    if (!trimmed) return
    onAsk(trimmed)
    setValue('')
  }

  return (
    <form onSubmit={submit} style={{ display: 'flex', gap: '8px', marginTop: '12px' }}>
      <input
        type="text"
        value={value}
        onChange={e => setValue(e.target.value)}
        placeholder="Ask a follow-up — e.g. &quot;What about last quarter?&quot;"
        aria-label="Ask a follow-up"
        style={{ flex: 1, padding: '9px 12px', borderRadius: '9px', border: `1px solid ${C.border}`, background: C.bg, color: C.text, fontFamily: FONT, fontSize: '0.78rem' }}
      />
      <button type="submit" disabled={!value.trim()} style={{ background: '#6366f1', border: 'none', borderRadius: '9px', padding: '9px 16px', fontSize: '0.78rem', fontWeight: '600', color: '#fff', cursor: value.trim() ? 'pointer' : 'default', opacity: value.trim() ? 1 : 0.5, fontFamily: FONT }}>
        Ask
      </button>
    </form>
  )
}

// Day 4, Capability 5 — Export. Round-trips the exact enterprise_answer/
// agent_status this panel already received from composer/ask — never
// re-derives or re-fetches anything. Renders nothing at all (not even a
// disabled button) when the answer isn't in a state Day 4's own rule allows
// exporting: still needs clarification, or the agent didn't reach
// "answered" — the backend independently re-enforces the same gate
// (api/v1/composer.py::composer_export -> core.answering.export_service
// .check_exportable), so this is a UX affordance, not the only guard.
const _EXPORT_FORMATS = [
  { format: 'xlsx', label: 'Excel (.xlsx)' },
  { format: 'csv', label: 'CSV' },
  { format: 'pdf', label: 'PDF' },
]

// A successfully answered question: "answered" (agent-routed) or "ok" (AI-first
// fast path). Must stay in sync with core/answering/export_service.py.
const EXPORTABLE_AGENT_STATUSES = ['answered', 'ok']

function ExportDropdown({ question, enterpriseAnswer, agentStatus, C, token, onSessionExpired }) {
  const [open, setOpen] = useState(false)
  const [busyFormat, setBusyFormat] = useState(null)
  const [error, setError] = useState(null)

  const exportable = !!enterpriseAnswer
    && enterpriseAnswer.answer_type !== 'clarification_needed'
    && (agentStatus == null || EXPORTABLE_AGENT_STATUSES.includes(agentStatus))

  if (!exportable) return null

  async function handleExport(format) {
    setOpen(false)
    setError(null)
    setBusyFormat(format)
    try {
      await exportComposerAnswer(token, { question, enterpriseAnswer, agentStatus, format })
    } catch (err) {
      if (err?.message?.startsWith('401:')) { onSessionExpired?.(); return }
      setError(err.message?.replace(/^\d+:\s*/, '') || 'Export failed.')
    } finally {
      setBusyFormat(null)
    }
  }

  return (
    <div style={{ position: 'relative' }}>
      <button
        onClick={() => setOpen(o => !o)}
        disabled={!!busyFormat}
        aria-label="Export"
        style={{ background: C.bg, border: `1px solid ${C.border}`, borderRadius: '9px', padding: '6px 14px', fontSize: '0.72rem', fontWeight: '600', color: C.textSec, cursor: busyFormat ? 'default' : 'pointer', fontFamily: FONT, display: 'inline-flex', alignItems: 'center', gap: '5px', opacity: busyFormat ? 0.6 : 1 }}
      >
        {busyFormat ? 'Exporting…' : 'Export'}
        {!busyFormat && (
          <svg width="9" height="9" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="3" strokeLinecap="round" strokeLinejoin="round"><path d="m6 9 6 6 6-6"/></svg>
        )}
      </button>
      {open && (
        <>
          <div onClick={() => setOpen(false)} style={{ position: 'fixed', inset: 0, zIndex: 998 }} />
          <div style={{ position: 'absolute', top: 'calc(100% + 4px)', right: 0, zIndex: 999, background: C.surface, border: `1px solid ${C.border}`, borderRadius: '10px', boxShadow: '0 8px 24px rgba(0,0,0,0.18)', overflow: 'hidden', minWidth: '140px' }}>
            {_EXPORT_FORMATS.map(({ format, label }) => (
              <button
                key={format}
                onClick={() => handleExport(format)}
                style={{ display: 'block', width: '100%', textAlign: 'left', background: 'none', border: 'none', padding: '9px 14px', fontSize: '0.74rem', color: C.text, cursor: 'pointer', fontFamily: FONT }}
                onMouseEnter={e => { e.currentTarget.style.background = C.bg }}
                onMouseLeave={e => { e.currentTarget.style.background = 'none' }}
              >
                {label}
              </button>
            ))}
          </div>
        </>
      )}
      {error && (
        <div style={{ position: 'absolute', top: 'calc(100% + 4px)', right: 0, zIndex: 999, background: 'rgba(248,113,113,0.1)', border: '1px solid rgba(248,113,113,0.3)', borderRadius: '8px', padding: '6px 10px', fontSize: '0.68rem', color: '#f87171', whiteSpace: 'nowrap' }}>
          {error}
        </div>
      )}
    </div>
  )
}

export function ComposerResultPanel({ result, wsInput, C, onBack, onOpenReport, onResolveClarification, onCancelClarification, clarificationResubmitted, onAskFollowUp, token, onSessionExpired }) {
  const [evidenceExpanded, setEvidenceExpanded] = useState(false)

  if (!result) return null

  const intent          = result.resolved_intent ?? {}
  const intentType      = intent.intent_type ?? 'unknown'
  const confidence      = intent.confidence ?? 0
  const keywords        = intent.keywords_matched ?? []
  const services        = result.services_selected ?? []
  const evidenceSummary = result.evidence_summary ?? {}
  const governanceState = result.governance_state ?? null
  const warnings        = result.warnings ?? []
  const errors          = result.errors ?? []
  const status          = result.status ?? 'unknown'
  const evidenceItems   = result.evidence_package?.evidence ?? []
  const nextAction      = COMPOSER_NEXT_ACTION[intentType] ?? null
  const intentLabel     = intentType.replace(/_/g, ' ').replace(/\b\w/g, c => c.toUpperCase())
  const businessAnswer  = result.business_answer ?? null
  const enterpriseAnswer = result.enterprise_answer ?? null
  // Milestone M-31/M-32 — additive-only agent fields. Absent entirely for
  // the legacy (non-agent-routed) composer path, so every use below is
  // conditional and the pre-M-31 rendering is otherwise untouched.
  const agentStatus      = result.agent_status ?? null
  const agentTrace       = result.agent_trace ?? []
  const conversationState = result.conversation_state ?? null
  // Phase 7, Step 6E3E, Part 1 -- additive-only; absent (null) for every
  // non-fast-path response, so this renders nothing for those (same
  // pattern as the M-31/M-32 agent fields above).
  const planCacheStatus  = result.plan_cache_status ?? null
  const planCacheNote    = result.plan_cache_note ?? null
  // Phase 7, Step 6F, Part 6 — present only when this turn resolved
  // against a prior turn's context (a genuine follow-up/clarification
  // answer/comparison/drill-down/change); null for a fresh question.
  // Never includes a row value or raw
  // result data — see api/v1/ai_first_composer_adapter.py's own
  // follow_up_resolution field docstring.
  const followUpResolution     = result.follow_up_resolution ?? null
  const resolvedStandaloneQuestion = followUpResolution?.resolved_standalone_question ?? null
  const followUpResolutionType     = followUpResolution?.resolution_type ?? null
  // Demo cleanup — everything below is developer/diagnostic content that
  // used to render as always-visible cards; it now lives inside one
  // collapsed "Technical details" section so the primary answer stays the
  // focus. Sourced from enterpriseAnswer where it has its own (already
  // 0-100) confidence/citations/source fields; falls back to the composer's
  // intent-resolution confidence (0-1 scale) when there is no enterprise
  // answer, so exactly one Confidence value is ever shown (no duplicate).
  const citations        = enterpriseAnswer?.citations ?? []
  const sourceTables      = enterpriseAnswer?.source_tables ?? []
  const sourceColumns     = enterpriseAnswer?.source_columns ?? []
  const hasSourceData     = sourceTables.length > 0 || sourceColumns.length > 0
  const techConfidence    = enterpriseAnswer ? Math.round(enterpriseAnswer.confidence ?? 0) : Math.round(confidence * 100)

  // Route report_generation intent to the dedicated enterprise report card.
  if (intentType === 'report_generation') {
    return <ComposerReportCard result={result} wsInput={wsInput} C={C} onBack={onBack} onOpenReport={onOpenReport} />
  }

  // Phase 6.6 Enterprise Clarification Intelligence — a clarification_needed
  // answer gets its own focused card (business-friendly options, no unrelated
  // Evidence/Governance/Services sections) instead of the generic panel body.
  if (enterpriseAnswer?.answer_type === 'clarification_needed' && enterpriseAnswer.clarification) {
    const clarificationOptions = enterpriseAnswer.clarification.options ?? []
    // Phase 3, Step 3 — ClarificationCard itself keeps sending plain
    // {term, table_fqn, column_name} selections (its own tests pin that
    // exact shape). The chosen option's business label (e.g. "Customer"),
    // needed as remember_synonym's canonical_term, is looked up here from
    // the clarification payload already in scope and attached only for the
    // parent's benefit — a pure addition, not a change to ClarificationCard.
    function handleResolveWithLabels(selections, remember) {
      const withLabels = selections.map(sel => {
        const opt = clarificationOptions.find(o =>
          o.term === sel.term && o.table_fqn === sel.table_fqn && (o.column_name ?? null) === (sel.column_name ?? null)
        )
        return { ...sel, label: opt?.label ?? null }
      })
      onResolveClarification(withLabels, remember)
    }
    return (
      <ClarificationCard
        clarification={enterpriseAnswer.clarification}
        wsInput={wsInput}
        C={C}
        onBack={onBack}
        onResolve={handleResolveWithLabels}
        onCancel={onCancelClarification}
        onFreeTextResolve={onAskFollowUp}
        resubmitted={clarificationResubmitted}
      />
    )
  }

  // Milestone M-32 — when the agent ran, its own status (a controlled
  // refusal/clarification/block, not a generic error) drives the badge
  // instead of the legacy success/partial/failed status, so a deliberate
  // stop never reads as a red error. Absent agent_status (every non-agent
  // response) renders exactly as before — no change for those paths.
  const agentDisplay = agentStatus ? (AGENT_STATUS_DISPLAY[agentStatus] ?? null) : null
  const statusColor = agentDisplay?.color ?? (status === 'success' ? '#10b981' : status === 'partial' ? '#f59e0b' : '#f87171')
  const statusBg    = agentDisplay?.bg    ?? (status === 'success' ? 'rgba(16,185,129,0.08)' : status === 'partial' ? 'rgba(245,158,11,0.08)' : 'rgba(248,113,113,0.08)')
  const statusLabel = agentDisplay?.label ?? status

  return (
    <div style={{ animation: 'ws-fadeup 0.35s ease' }}>

      {/* Header */}
      <div style={{ display: 'flex', alignItems: 'center', gap: '12px', marginBottom: '16px', padding: '14px 20px', background: C.surface, border: `1px solid ${C.border}`, borderRadius: '16px', boxShadow: '0 2px 12px rgba(0,0,0,0.07)' }}>
        <div style={{ width: '40px', height: '40px', borderRadius: '11px', background: 'rgba(99,102,241,0.12)', display: 'flex', alignItems: 'center', justifyContent: 'center', flexShrink: 0 }}>
          <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="#6366f1" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><circle cx="11" cy="11" r="8"/><path d="m21 21-4.35-4.35"/></svg>
        </div>
        <div style={{ flex: 1, minWidth: 0 }}>
          <div style={{ fontSize: '0.55rem', fontWeight: '700', color: '#6366f1', textTransform: 'uppercase', letterSpacing: '0.14em', marginBottom: '2px' }}>Enterprise Catalog Intelligence</div>
          <div style={{ fontSize: '0.88rem', fontWeight: '600', color: C.text, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{wsInput || 'Catalog query'}</div>
        </div>
        <div style={{ display: 'flex', alignItems: 'center', gap: '8px', flexShrink: 0 }}>
          <span style={{ display: 'inline-flex', alignItems: 'center', gap: '5px', background: statusBg, border: `1px solid ${statusColor}55`, borderRadius: '20px', padding: '3px 10px', fontSize: '0.57rem', fontWeight: '700', color: statusColor, textTransform: 'uppercase', letterSpacing: '0.08em' }}>
            <span style={{ width: '5px', height: '5px', borderRadius: '50%', background: statusColor }} />
            {statusLabel}
          </span>
          <ExportDropdown
            question={wsInput} enterpriseAnswer={enterpriseAnswer} agentStatus={agentStatus}
            C={C} token={token} onSessionExpired={onSessionExpired}
          />
          <button onClick={onBack} style={{ background: C.bg, border: `1px solid ${C.border}`, borderRadius: '9px', padding: '6px 14px', fontSize: '0.72rem', fontWeight: '600', color: C.textSec, cursor: 'pointer', fontFamily: FONT }}>
            New Query
          </button>
        </div>
      </div>

      {/* Enterprise Answer takes priority when present (e.g. SQL_REQUEST/live_query
          results) — it's the Enterprise Answer Layer's deterministic, evidence-
          grounded answer. business_answer has no dedicated branch for those
          intents and would otherwise show a generic metadata fallback. Falls
          back to business_answer when enterprise_answer is absent (e.g. its
          server-side generation failed, or for intents it doesn't cover). */}
      {enterpriseAnswer
        ? <EnterpriseAnswerBlock answer={enterpriseAnswer} C={C} />
        : <BusinessAnswerBlock answer={businessAnswer} C={C} />}

      {/* Milestone M-32 — (only when the backend returned conversation_state
          to continue) an inline follow-up box so a follow-up question never
          has to go through "New Query" and lose context. Kept primary/
          visible — it's part of the user's Q&A flow, not a diagnostic. The
          agent trace itself moved into Technical details below. */}
      {conversationState && onAskFollowUp && <FollowUpBox onAsk={onAskFollowUp} C={C} />}

      {/* Technical details — everything below is developer/diagnostic
          content (Confidence, Citations, Agent Trace, Answer Context,
          Services Consulted, Evidence Summary, Governance/Warnings, and
          raw source table/column identifiers), collapsed into one section
          by design so the primary answer above stays the focus. Native
          <details>, closed by default; nothing above depends on it. */}
      <details className="ws-section ws-s1" style={{ background: C.surface, border: `1px solid ${C.border}`, borderRadius: '14px', padding: '18px 20px', marginBottom: '12px' }}>
        <summary style={{ cursor: 'pointer', fontSize: '0.68rem', color: C.textMuted, fontWeight: '700', textTransform: 'uppercase', letterSpacing: '0.1em' }}>
          Technical details
        </summary>

        <div style={{ marginTop: '18px', display: 'flex', flexDirection: 'column', gap: '20px' }}>

          {/* Confidence — the single confidence display for this answer;
              prefers the enterprise answer's own (already 0-100) value,
              falling back to intent-resolution confidence when there is
              no enterprise answer. */}
          <div>
            <div style={{ fontSize: '0.68rem', color: C.textMuted, marginBottom: '6px' }}>Confidence</div>
            <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
              <div style={{ flex: 1, height: '5px', background: C.border, borderRadius: '3px', overflow: 'hidden', maxWidth: '200px' }}>
                <div style={{ height: '100%', width: `${techConfidence}%`, background: techConfidence >= 70 ? '#10b981' : techConfidence >= 40 ? '#f59e0b' : '#f87171', borderRadius: '3px', transition: 'width 0.4s ease' }} />
              </div>
              <span style={{ fontSize: '0.72rem', fontWeight: '700', color: C.textSec, minWidth: '30px' }}>{techConfidence}%</span>
            </div>
          </div>

          {/* Citations */}
          {citations.length > 0 && (
            <div>
              <div style={{ fontSize: '0.68rem', color: C.textMuted, marginBottom: '6px' }}>Citations</div>
              <div style={{ display: 'flex', flexWrap: 'wrap', gap: '5px' }}>
                {citations.map((c, i) => (
                  <span key={i} title={c.reference || ''} style={{ background: 'rgba(99,102,241,0.08)', border: '1px solid rgba(99,102,241,0.18)', borderRadius: '20px', padding: '3px 9px', fontSize: '0.65rem', color: C.textSec, fontWeight: '500' }}>
                    {c.label || c.source_type || 'source'}
                  </span>
                ))}
              </div>
            </div>
          )}

          {/* Plan cache status. Only present for an AI-first fast-path
              response that reached plan lookup; absent (not rendered)
              for every other response shape. A HIT is never shown as "results from
              cache" — the note is explicit that SQL still ran live. */}
          {planCacheStatus && (
            <div>
              <div style={{ fontSize: '0.68rem', color: C.textMuted, marginBottom: '6px' }}>Plan Cache</div>
              <span style={{
                display: 'inline-flex', alignItems: 'center', gap: '5px', borderRadius: '20px', padding: '3px 10px',
                fontSize: '0.65rem', fontWeight: '700', textTransform: 'uppercase', letterSpacing: '0.06em',
                background: planCacheStatus === 'HIT' ? 'rgba(16,185,129,0.10)' : 'rgba(148,163,184,0.10)',
                border: `1px solid ${planCacheStatus === 'HIT' ? 'rgba(16,185,129,0.25)' : 'rgba(148,163,184,0.25)'}`,
                color: planCacheStatus === 'HIT' ? '#10b981' : '#94a3b8',
              }}>
                Plan cache: {planCacheStatus === 'HIT' ? 'Hit' : 'Miss'}
              </span>
              {planCacheNote && (
                <div style={{ fontSize: '0.70rem', color: C.textMuted, marginTop: '6px', lineHeight: 1.5 }}>{planCacheNote}</div>
              )}
            </div>
          )}

          {/* Follow-up resolution — Phase 7, Step 6F, Part 6. Only present
              when this turn genuinely resolved against a prior turn;
              shows WHAT was resolved (never a row value, never raw prior
              context) so the user can verify their follow-up was
              understood correctly before trusting the answer. */}
          {followUpResolution && resolvedStandaloneQuestion && followUpResolutionType && followUpResolutionType !== 'none' && (
            <div>
              <div style={{ fontSize: '0.68rem', color: C.textMuted, marginBottom: '6px' }}>Follow-up Resolution</div>
              <span style={{
                display: 'inline-flex', alignItems: 'center', gap: '5px', borderRadius: '20px', padding: '3px 10px',
                fontSize: '0.65rem', fontWeight: '700', textTransform: 'uppercase', letterSpacing: '0.06em',
                background: 'rgba(99,102,241,0.10)', border: '1px solid rgba(99,102,241,0.25)', color: '#6366f1',
              }}>
                {FOLLOW_UP_RESOLUTION_TYPE_LABELS[followUpResolutionType] ?? followUpResolutionType}
              </span>
              <div style={{ fontSize: '0.70rem', color: C.textSec, marginTop: '6px', lineHeight: 1.5 }}>
                Understood as: <span style={{ fontWeight: '600', color: C.text }}>&ldquo;{resolvedStandaloneQuestion}&rdquo;</span>
              </div>
            </div>
          )}

          {/* Agent trace — keeps its own inner collapse for the step list */}
          <AgentTraceSection trace={agentTrace} C={C} />

          {/* Answer Context */}
          <div>
            <div style={{ fontSize: '0.68rem', color: C.textMuted, marginBottom: '10px', fontWeight: '700', textTransform: 'uppercase', letterSpacing: '0.08em' }}>Answer Context</div>
            <div style={{ marginBottom: '11px' }}>
              <div style={{ fontSize: '0.68rem', color: C.textMuted, marginBottom: '3px' }}>Resolved Intent</div>
              <div style={{ fontSize: '0.86rem', fontWeight: '600', color: C.text }}>{intentLabel}</div>
            </div>
            {keywords.length > 0 && (
              <div>
                <div style={{ fontSize: '0.68rem', color: C.textMuted, marginBottom: '6px' }}>Keywords Matched</div>
                <div style={{ display: 'flex', flexWrap: 'wrap', gap: '4px' }}>
                  {keywords.slice(0, 6).map(k => (
                    <span key={k} style={{ background: 'rgba(99,102,241,0.1)', border: '1px solid rgba(99,102,241,0.2)', borderRadius: '20px', padding: '2px 8px', fontSize: '0.63rem', color: '#6366f1', fontWeight: '500' }}>{k}</span>
                  ))}
                </div>
              </div>
            )}
          </div>

          {/* Services Consulted */}
          <div>
            <div style={{ fontSize: '0.68rem', color: C.textMuted, marginBottom: '10px', fontWeight: '700', textTransform: 'uppercase', letterSpacing: '0.08em' }}>Services Consulted</div>
            {services.length > 0 ? (
              <div style={{ display: 'flex', flexDirection: 'column', gap: '6px' }}>
                {services.map((svc, i) => (
                  <div key={i} style={{ display: 'flex', alignItems: 'center', gap: '8px', padding: '7px 10px', background: 'rgba(59,130,246,0.06)', border: '1px solid rgba(59,130,246,0.14)', borderRadius: '8px' }}>
                    <div style={{ width: '6px', height: '6px', borderRadius: '50%', background: '#3b82f6', flexShrink: 0 }} />
                    <span style={{ fontSize: '0.74rem', color: C.text, fontFamily: MONO, fontWeight: '500' }}>{svc}</span>
                  </div>
                ))}
              </div>
            ) : (
              <div style={{ fontSize: '0.77rem', color: C.textMuted }}>No services invoked</div>
            )}
          </div>

          {/* Evidence Summary */}
          <div>
            <div style={{ fontSize: '0.68rem', color: C.textMuted, marginBottom: '10px', fontWeight: '700', textTransform: 'uppercase', letterSpacing: '0.08em' }}>Evidence Summary</div>
            <div style={{ display: 'grid', gridTemplateColumns: 'repeat(4, 1fr)', gap: '10px', marginBottom: evidenceItems.length > 0 ? '14px' : 0 }}>
              {[
                { label: 'Total Items',        value: evidenceSummary.total_items        ?? 0,   color: '#6366f1' },
                { label: 'Attempted',          value: evidenceSummary.services_attempted ?? 0,   color: '#3b82f6' },
                { label: 'Succeeded',          value: evidenceSummary.services_succeeded ?? 0,   color: '#10b981' },
                { label: 'Has Errors',         value: evidenceSummary.has_errors ? 'Yes' : 'No', color: evidenceSummary.has_errors ? '#f87171' : '#10b981' },
              ].map(({ label, value, color }) => (
                <div key={label} style={{ textAlign: 'center', padding: '12px 8px', background: `${color}0d`, border: `1px solid ${color}22`, borderRadius: '10px' }}>
                  <div style={{ fontSize: '1.25rem', fontWeight: '700', color, fontFamily: MONO, lineHeight: 1 }}>{value}</div>
                  <div style={{ fontSize: '0.61rem', color: C.textMuted, marginTop: '5px', lineHeight: 1.3 }}>{label}</div>
                </div>
              ))}
            </div>
            {evidenceItems.length > 0 && (
              <div>
                <button onClick={() => setEvidenceExpanded(o => !o)} style={{ display: 'flex', alignItems: 'center', gap: '6px', background: 'none', border: 'none', cursor: 'pointer', padding: 0, color: C.textSec, fontFamily: FONT, fontSize: '0.72rem', fontWeight: '600' }}>
                  <svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round" style={{ transform: evidenceExpanded ? 'rotate(90deg)' : 'none', transition: 'transform 0.2s' }}><path d="m9 18 6-6-6-6"/></svg>
                  {evidenceExpanded ? 'Hide' : 'Show'} evidence items ({evidenceItems.length})
                </button>
                {evidenceExpanded && (
                  <div style={{ marginTop: '10px', display: 'flex', flexDirection: 'column', gap: '6px' }}>
                    {evidenceItems.map((item, i) => (
                      <div key={item.evidence_id ?? i} style={{ padding: '9px 13px', background: C.bg, border: `1px solid ${C.border}`, borderRadius: '8px', display: 'grid', gridTemplateColumns: 'auto 1fr auto', gap: '10px', alignItems: 'center' }}>
                        <span style={{ fontSize: '0.63rem', color: C.textMuted, fontFamily: MONO }}>{item.source_service}</span>
                        <span style={{ fontSize: '0.72rem', color: C.text, fontWeight: '500' }}>{item.capability}</span>
                        <span style={{ fontSize: '0.63rem', fontWeight: '700', color: (item.confidence ?? 0) >= 0.7 ? '#10b981' : (item.confidence ?? 0) >= 0.4 ? '#f59e0b' : '#f87171' }}>{Math.round((item.confidence ?? 0) * 100)}%</span>
                      </div>
                    ))}
                  </div>
                )}
              </div>
            )}
          </div>

          {/* Governance + Warnings */}
          {(governanceState || warnings.length > 0 || errors.length > 0) && (
            <div style={{ display: 'grid', gridTemplateColumns: governanceState && (warnings.length > 0 || errors.length > 0) ? '1fr 1fr' : '1fr', gap: '12px' }}>
              {governanceState && (
                <div>
                  <div style={{ fontSize: '0.68rem', color: C.textMuted, marginBottom: '8px', fontWeight: '700', textTransform: 'uppercase', letterSpacing: '0.08em' }}>Governance</div>
                  <div style={{ fontSize: '0.85rem', fontWeight: '600', color: C.text }}>{governanceState}</div>
                </div>
              )}
              {(warnings.length > 0 || errors.length > 0) && (
                <div>
                  <div style={{ fontSize: '0.68rem', color: C.textMuted, marginBottom: '8px', fontWeight: '700', textTransform: 'uppercase', letterSpacing: '0.08em' }}>Warnings</div>
                  <div style={{ display: 'flex', flexDirection: 'column', gap: '6px' }}>
                    {warnings.map((w, i) => (
                      <div key={i} style={{ display: 'flex', gap: '7px', alignItems: 'flex-start', fontSize: '0.76rem', color: '#d97706', lineHeight: 1.45 }}>
                        <span style={{ marginTop: '1px', flexShrink: 0 }}>&#9888;</span>
                        <span>{w}</span>
                      </div>
                    ))}
                    {errors.map((e, i) => (
                      <div key={`err_${i}`} style={{ display: 'flex', gap: '7px', alignItems: 'flex-start', fontSize: '0.76rem', color: '#f87171', lineHeight: 1.45 }}>
                        <span style={{ marginTop: '1px', flexShrink: 0 }}>&#10005;</span>
                        <span>{typeof e === 'string' ? e : JSON.stringify(e)}</span>
                      </div>
                    ))}
                  </div>
                </div>
              )}
            </div>
          )}

          {/* Source table/column identifiers — raw technical identifiers,
              never shown in the primary answer by design. */}
          {hasSourceData && (
            <div>
              <div style={{ fontSize: '0.68rem', color: C.textMuted, marginBottom: '8px', fontWeight: '700', textTransform: 'uppercase', letterSpacing: '0.08em' }}>Source Data</div>
              {sourceTables.length > 0 && (
                <div style={{ fontSize: '0.68rem', color: C.textSec, marginBottom: '6px' }}>
                  <span style={{ color: C.textMuted }}>Source table(s): </span>{sourceTables.join(', ')}
                </div>
              )}
              {sourceColumns.length > 0 && (
                <div style={{ fontSize: '0.68rem', color: C.textSec, wordBreak: 'break-word' }}>
                  <span style={{ color: C.textMuted }}>Source column(s): </span>{sourceColumns.join(', ')}
                </div>
              )}
            </div>
          )}
        </div>
      </details>

      {/* Next Suggested Action */}
      {nextAction && (
        <div className="ws-section ws-s6" style={{ display: 'flex', alignItems: 'center', gap: '14px', padding: '16px 20px', background: 'rgba(99,102,241,0.06)', border: '1px solid rgba(99,102,241,0.2)', borderRadius: '14px' }}>
          <div style={{ width: '34px', height: '34px', borderRadius: '9px', background: 'rgba(99,102,241,0.14)', display: 'flex', alignItems: 'center', justifyContent: 'center', flexShrink: 0 }}>
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="#6366f1" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round"><polyline points="9 18 15 12 9 6"/></svg>
          </div>
          <div>
            <div style={{ fontSize: '0.57rem', fontWeight: '700', color: '#6366f1', textTransform: 'uppercase', letterSpacing: '0.12em', marginBottom: '3px' }}>Next Suggested Action</div>
            <div style={{ fontSize: '0.79rem', color: C.text, lineHeight: 1.5 }}>{nextAction}</div>
          </div>
        </div>
      )}
    </div>
  )
}

// ─── AI Workspace — main export ───────────────────────────────────────────────

export default function AIWorkspace({
  C,
  token, onSessionExpired,
  user,
  datasetList,
  selectedDatasetId, setSelectedDatasetId,
  datasetExplicit, setDatasetExplicit,
  externalResult, externalLoading, externalError,
  setActiveNav, onOpenReport,
  onUploadDataset,
  onExecutionComplete,
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
  const [wsProposalLoading] = useState(false)
  const [wsProposalError,   setWsProposalError]   = useState(null)
  const [wsEmail,           setWsEmail]           = useState('')
  const [enginePlan,        setEnginePlan]        = useState(null)
  const [enginePlanLoading, setEnginePlanLoading] = useState(false)
  const [savedToolId,       setSavedToolId]       = useState(null)
  const [toolStatus,        setToolStatus]        = useState(null)
  const [engineBusy,        setEngineBusy]        = useState(null)
  const [toast,             setToast]             = useState(null)
  // Phase 3, Step 3 — remembered_term_conflict: {sourceId, term, tableFqn,
  // columnName, existingCanonicalTerm, requestedCanonicalTerm} or null.
  const [rememberConflict,      setRememberConflict]      = useState(null)
  const [rememberConflictBusy,  setRememberConflictBusy]  = useState(false)
  const [showRawJson,       setShowRawJson]       = useState(false)
  const [planDatasetId,     setPlanDatasetId]     = useState(null)
  const [backToComposer,    setBackToComposer]    = useState(false)
  const [,                  setWsRunSource]       = useState(null) // null | workflow title string
  const [dsPendingRun,      setDsPendingRun]      = useState(null) // pending run args awaiting dataset confirmation
  const [noDsWarning,       setNoDsWarning]       = useState(false)
  const [composerResult,    setComposerResult]    = useState(null)
  const [composerLoading,   setComposerLoading]   = useState(false)
  const [clarificationResubmitted, setClarificationResubmitted] = useState(false)
  const [dataSourceList,      setDataSourceList]      = useState([])
  const [selectedDataSourceId, setSelectedDataSourceId] = useState(null)
  const [sourcePicker,        setSourcePicker]        = useState(false)
  const [sourceSearch,        setSourceSearch]        = useState('')

  // Milestone M-31/M-32 — AI Analyst Agent conversation context. See
  // frontend/src/hooks/useConversationState.js for the storage/reset
  // contract; conversation.reset() below is the ONLY place that starts a
  // genuinely new conversation (fresh conversation_id, no carried state).
  const conversation = useConversationState()

  const loadDataSources = useCallback(async () => {
    try {
      const data    = await listDataSources(token)
      const sources = Array.isArray(data?.data) ? data.data : Array.isArray(data) ? data : []
      setDataSourceList(sources)
    } catch { /* silently ignore — don't block primary UX */ }
  }, [token])

  useEffect(() => { if (externalLoading) queueMicrotask(() => { setWsResult(null); setWsError(null) }) }, [externalLoading])
  useEffect(() => { if (externalResult) queueMicrotask(() => setWsResult(externalResult)) }, [externalResult])
  useEffect(() => { if (externalError) queueMicrotask(() => setWsError(externalError)) }, [externalError])
  useEffect(() => { if (token) queueMicrotask(() => void loadDataSources()) }, [token, loadDataSources])
  useEffect(() => { if (!sourcePicker) queueMicrotask(() => setSourceSearch('')) }, [sourcePicker])
  // Conversation context is scoped to one data source and one authenticated
  // user — switching either means a different conversation, never a
  // follow-up of the old one.
  useEffect(() => { queueMicrotask(() => conversation.reset()) }, [selectedDataSourceId]) // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => { queueMicrotask(() => conversation.reset()) }, [token]) // eslint-disable-line react-hooks/exhaustive-deps

  const activeResult       = wsResult
  const activeLoading      = wsLoading || externalLoading
  const activeError        = wsError
  const intel              = extractIntel(activeResult)
  const hasResult          = !!activeResult && !activeLoading
  const hasComposerResult  = !!composerResult && !composerLoading
  const showComposer       = !activeLoading && !composerLoading && (backToComposer || (!hasResult && !hasComposerResult && !wsProposal && !enginePlan))
  const activeDs           = datasetList?.find(d => d.id === selectedDatasetId) || null
  const activeSource       = dataSourceList?.find(s => s.id === selectedDataSourceId) || null
  const isEmailIntent      = ['email', 'send', 'mail'].some(kw => wsInput.toLowerCase().includes(kw))
  const disabled           = wsLoading || wsProposalLoading || enginePlanLoading || composerLoading || !wsInput.trim()

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
      const data = await planEngineTool(trimmed, token, selectedDatasetId ? { dataset_id: selectedDatasetId } : null)
      const plan = data?.data ?? data
      setEnginePlan(plan)
      const planTitle = plan?.description || slugToTitle(plan?.name || 'Tool')
      showToast(`Orchestration plan ready — "${planTitle}"`)
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

  async function handleComposerAsk(query, datasetId = null, clarificationPayload = null) {
    setWsError(null)
    setWsResult(null)
    setComposerResult(null)
    setWsProposal(null)
    setComposerLoading(true)
    setBackToComposer(false)
    setClarificationResubmitted(!!clarificationPayload)
    execStartedAtRef.current = Date.now()
    try {
      // Milestone M-31/M-32 — session_id doubles as the AI Analyst Agent's
      // conversation_id, so it must stay stable across turns of the SAME
      // conversation (conversation.conversationId only rotates on
      // conversation.reset() — source change, auth change, or an explicit
      // new conversation). Regenerating it per request, as before, would
      // make every follow-up/clarification-resume request look like a
      // brand-new, unrelated conversation to the backend's isolation check.
      // buildComposerPayload is the one place conversation_state is attached
      // — used identically for a fresh question, a follow-up, a
      // clarification resume, and a clarification cancel, never a separate
      // path per caller.
      const payload = buildComposerPayload({
        conversationId:          conversation.conversationId,
        message:                 query,
        selectedDataSourceId:    selectedDataSourceId ?? null,
        datasetId,
        clarificationSelection:  clarificationPayload?.clarification_selection ?? null,
        cancelClarification:     !!clarificationPayload?.cancel_clarification,
        conversationState:       conversation.forSource(selectedDataSourceId ?? null),
      })
      const data = await askComposer(token, payload)
      conversation.capture(data, selectedDataSourceId ?? null)
      setComposerResult(data)
    } catch (err) {
      if (err?.message?.startsWith('401:')) { onSessionExpired(); return }
      setWsError(err.message?.replace(/^\d+:\s*/, '') || 'Catalog query failed.')
    } finally {
      setComposerLoading(false)
    }
  }

  // Phase 6.6 Enterprise Clarification Intelligence — resubmit the original
  // question unchanged, plus the structured selection or cancellation. The
  // backend is stateless: it re-derives the query plan from `message` and
  // only reads clarification_selection/cancel_clarification (no session
  // continuity required).
  //
  // Phase 2, Step 7b — when the user opted in via ClarificationCard's
  // "remember this choice" checkbox, also promote each selection to an
  // approved concept_table_mappings row (POST /v1/sources/{id}/concept-
  // mappings), so a future question using the same term resolves directly
  // instead of asking again. Best-effort and non-blocking: a failure here
  // must never prevent the original question from being answered — it only
  // means the choice wasn't remembered, which the user can simply redo.
  function handleClarificationResolve(selections, remember = false) {
    handleComposerAsk(wsInput, null, { clarification_selection: selections })
    if (remember && selectedDataSourceId != null) {
      for (const sel of selections) {
        promoteConceptMapping(
          selectedDataSourceId,
          { conceptTerm: sel.term, tableFqn: sel.table_fqn, columnName: sel.column_name, canonicalTerm: sel.label },
          token,
        ).then(res => {
          // Phase 3, Step 3 — a 409 remembered_term_conflict is returned as
          // data (see client.js), not thrown, so it lands here rather than
          // in .catch(). Surfacing it never touches the question/answer
          // flow above, which has already been kicked off independently.
          if (res?.reason === 'remembered_term_conflict') {
            setRememberConflict({
              sourceId: selectedDataSourceId,
              term: sel.term,
              tableFqn: sel.table_fqn,
              columnName: sel.column_name,
              existingCanonicalTerm: res.existing_canonical_term,
              requestedCanonicalTerm: res.requested_canonical_term,
            })
          }
        }).catch(err => console.warn('Failed to remember clarification choice:', err))
      }
    }
  }

  function handleKeepExistingMeaning() {
    // No request sent — the previously remembered meaning is left as-is.
    setRememberConflict(null)
  }

  function handleReplaceRememberedMeaning() {
    if (!rememberConflict) return
    setRememberConflictBusy(true)
    promoteConceptMapping(
      rememberConflict.sourceId,
      {
        conceptTerm: rememberConflict.term,
        tableFqn: rememberConflict.tableFqn,
        columnName: rememberConflict.columnName,
        canonicalTerm: rememberConflict.requestedCanonicalTerm,
        confirmReplace: true,
      },
      token,
    ).then(() => {
      showToast(`Updated — "${rememberConflict.term}" now means "${rememberConflict.requestedCanonicalTerm}".`)
      setRememberConflict(null)
    }).catch(err => {
      showToast(err.message?.replace(/^\d+:\s*/, '') || 'Failed to update remembered terminology.', false)
    }).finally(() => setRememberConflictBusy(false))
  }

  function handleClarificationCancel() {
    handleComposerAsk(wsInput, null, { cancel_clarification: true })
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
      const savedTitle = enginePlan?.description || slugToTitle(enginePlan?.name || 'Tool')
      showToast(`"${savedTitle}" saved to your Tool Library.`)
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
    // A selected live data source always routes through the Enterprise Composer —
    // Composer routes SQL sources to the AI-first path; dataset and catalog
    // capabilities use their dedicated application routes.
    // classification server-side; the frontend no longer pre-classifies this case
    // with a regex. Mutual exclusion in the dataset/source pickers guarantees
    // selectedDatasetId is null whenever selectedDataSourceId is set, so this
    // never competes with the dataset-oriented path below.
    if (overrideIntent === null && sections === null && proposal === null && selectedDataSourceId) {
      handleComposerAsk(trimmed)
      return
    }
    // ── Dataset-only workflow (no live source selected) — unchanged ──────────
    // Route catalog/metadata/governance questions to the Enterprise Composer API.
    if (overrideIntent === null && sections === null && proposal === null && isComposerIntent(trimmed)) {
      handleComposerAsk(trimmed)
      return
    }
    const dsId = overrideDsId !== undefined ? overrideDsId : (selectedDatasetId || null)
    // Route report-generation prompts through the Enterprise Composer when a dataset is selected.
    // Falls through to the standard interpretTask path when no dataset is available.
    if (overrideIntent === null && sections === null && proposal === null && isReportGenerationIntent(trimmed) && dsId) {
      handleComposerAsk(trimmed, dsId)
      return
    }

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
      if (onExecutionComplete) onExecutionComplete()
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
    setComposerResult(null); setComposerLoading(false); setClarificationResubmitted(false)
    // "New Query"/"Try Again" — the user is explicitly starting over, so the
    // AI Analyst Agent conversation resets too (fresh conversation_id, no
    // carried-over context). Asking a follow-up without losing context uses
    // ComposerResultPanel's own inline follow-up box instead, which calls
    // handleComposerAsk directly and never goes through handleReset.
    conversation.reset()
    // Session-saved workflow state is preserved until the page is reloaded.
  }

  return (
    <div style={{ fontFamily: FONT, paddingRight: '16px' }}>
      <style>{WS_STYLES}</style>
      <Toast toast={toast} />
      <RememberConflictPanel
        conflict={rememberConflict}
        busy={rememberConflictBusy}
        onKeep={handleKeepExistingMeaning}
        onReplace={handleReplaceRememberedMeaning}
        C={C}
      />

      {/* Hidden file input */}
      <input ref={fileInputRef} type="file" accept=".csv,.xlsx,.xls,.json,.sql" style={{ display: 'none' }}
        onChange={e => { const f = e.target.files?.[0]; if (f && onUploadDataset) onUploadDataset(f); e.target.value = '' }}
      />

      {/* ── Page header — only on composer/workspace, hidden during execution, result, and plan screen ── */}
      {!activeLoading && !composerLoading && (!hasResult || backToComposer) && !hasComposerResult && !enginePlan && (
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

                {/* ── UNIFIED DATA SOURCE section ── */}
                <div style={{ marginBottom: '14px', display: 'flex', alignItems: 'center', gap: '12px' }}>
                  <div style={{ display: 'flex', alignItems: 'center', gap: '6px', width: '158px', flexShrink: 0, whiteSpace: 'nowrap' }}>
                    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="#6366f1" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round"><path d="M10 13a5 5 0 0 0 7.54.54l3-3a5 5 0 0 0-7.07-7.07l-1.72 1.71"/><path d="M14 11a5 5 0 0 0-7.54-.54l-3 3a5 5 0 0 0 7.07 7.07l1.71-1.71"/></svg>
                    <span style={{ fontSize: '0.63rem', fontWeight: '600', color: C.text, textTransform: 'uppercase', letterSpacing: '0.11em', whiteSpace: 'nowrap' }}>Select Data Source</span>
                  </div>

                  <div style={{ position: 'relative', flex: 1, minWidth: 0 }}>
                    {sourcePicker && <div style={{ position: 'fixed', inset: 0, zIndex: 998 }} onClick={() => setSourcePicker(false)} />}
                    <button className="ws-ghost-btn" onClick={() => setSourcePicker(o => !o)}
                      style={{ display: 'flex', alignItems: 'center', gap: '8px', width: '100%', background: (activeSource || activeDs) ? 'rgba(99,102,241,0.07)' : C.bg, border: `1px solid ${(activeSource || activeDs) ? 'rgba(99,102,241,0.35)' : C.border}`, borderRadius: '9px', padding: '8px 12px', minHeight: 36, fontSize: '0.76rem', color: (activeSource || activeDs) ? '#6366f1' : C.textSec, cursor: 'pointer', fontFamily: FONT, fontWeight: '400', textAlign: 'left', position: 'relative', zIndex: 999 }}>
                      <span style={{ flex: 1, minWidth: 0, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{activeSource ? activeSource.display_name : activeDs ? activeDs.filename : 'Choose a database or uploaded dataset'}</span>
                      {activeDs && <span style={{ fontSize: '0.58rem', color: C.textMuted, border: `1px solid ${C.border}`, borderRadius: 4, padding: '1px 5px' }}>Dataset</span>}
                      {activeSource && <span style={{ fontSize: '0.58rem', color: C.textMuted, border: `1px solid ${C.border}`, borderRadius: 4, padding: '1px 5px' }}>Database</span>}
                      <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round" style={{ opacity: 0.5, flexShrink: 0 }}><path d="m6 9 6 6 6-6"/></svg>
                    </button>

                    {sourcePicker && (
                      <div style={{ position: 'absolute', bottom: 'calc(100% + 7px)', left: 0, zIndex: 999, width: '100%', minWidth: '360px', background: C.surface, border: `1px solid ${C.borderAlt}`, borderRadius: '11px', boxShadow: '0 14px 40px rgba(0,0,0,0.34)', overflow: 'hidden', animation: 'ws-fadeup 0.15s ease' }}>
                        {/* Search */}
                        <div style={{ padding: '7px 9px', borderBottom: `1px solid ${C.border}` }}>
                          <div style={{ display: 'flex', alignItems: 'center', gap: '7px', background: C.bg, border: `1px solid ${C.border}`, borderRadius: '7px', padding: '5px 9px' }}>
                            <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke={C.textMuted} strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" style={{ flexShrink: 0 }}><circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/></svg>
                            <input
                              autoFocus
                              type="text"
                              placeholder="Search…"
                              value={sourceSearch}
                              onChange={e => setSourceSearch(e.target.value)}
                              style={{ flex: 1, background: 'none', border: 'none', outline: 'none', fontSize: '0.74rem', color: C.text, fontFamily: FONT, caretColor: '#6366f1' }}
                            />
                            {sourceSearch && (
                              <button onClick={() => setSourceSearch('')} style={{ background: 'none', border: 'none', cursor: 'pointer', padding: 0, display: 'flex', alignItems: 'center', color: C.textMuted }}>
                                <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>
                              </button>
                            )}
                          </div>
                        </div>
                        {/* List */}
                        <div style={{ maxHeight: '310px', overflowY: 'auto', overscrollBehavior: 'contain', scrollbarWidth: 'thin' }}>
                          {/* Clear selection */}
                          <div onClick={() => { setSelectedDataSourceId(null); setSelectedDatasetId(null); setDatasetExplicit(false); setSourcePicker(false) }}
                            style={{ display: 'flex', alignItems: 'center', gap: '6px', padding: '7px 12px', cursor: 'pointer', background: !activeSource && !activeDs ? 'rgba(99,102,241,0.08)' : 'transparent', borderBottom: `1px solid ${C.border}` }}>
                            <div style={{ flex: 1, fontSize: '0.75rem', fontWeight: '500', color: !activeSource && !activeDs ? '#6366f1' : C.text }}>No data source selected</div>
                          </div>
                          <div style={{ position: 'sticky', top: 0, zIndex: 2, padding: '6px 10px 4px', background: C.surface, color: C.textMuted, fontSize: '.58rem', fontWeight: 700, textTransform: 'uppercase', letterSpacing: '.09em' }}>Database Connections</div>
                          {(() => {
                            const q = sourceSearch.toLowerCase().trim()
                            const filtered = (dataSourceList || []).filter(s => (s.display_name || '').toLowerCase().includes(q) || (s.source_type || '').toLowerCase().includes(q))
                            if (!filtered.length) return <div style={{ padding: '8px 12px', color: C.textMuted, fontSize: '0.7rem' }}>No matching databases.</div>
                            return filtered.map(src => {
                              const isSel = src.id === selectedDataSourceId
                              return (
                                <div key={src.id} onClick={() => { setSelectedDataSourceId(src.id); setSelectedDatasetId(null); setSourcePicker(false) }}
                                  style={{ display: 'grid', gridTemplateColumns: '32px minmax(0,1fr) auto 16px', alignItems: 'center', gap: '8px', padding: '6px 10px', minHeight: 38, cursor: 'pointer', background: isSel ? 'rgba(99,102,241,0.08)' : 'transparent', borderBottom: `1px solid ${C.border}`, transition: 'background 0.1s' }}
                                  onMouseEnter={e => { if (!isSel) e.currentTarget.style.background = C.borderAlt }}
                                  onMouseLeave={e => { if (!isSel) e.currentTarget.style.background = 'transparent' }}>
                                  <span style={{ color: '#6366f1', fontSize: '.58rem', fontWeight: 700 }}>DB</span>
                                  <div style={{ minWidth: 0, fontSize: '0.73rem', fontWeight: 500, color: isSel ? '#6366f1' : C.text, lineHeight: 1.25, display: '-webkit-box', WebkitLineClamp: 2, WebkitBoxOrient: 'vertical', overflow: 'hidden', overflowWrap: 'anywhere' }}>{src.display_name}</div>
                                  <span style={{ color: C.textMuted, fontSize: '.59rem', whiteSpace: 'nowrap' }}>{src.source_type}{src.source_status ? ` · ${src.source_status}` : ''}</span>
                                  {isSel && <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="#6366f1" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round"><polyline points="20 6 9 17 4 12"/></svg>}
                                </div>
                              )
                            })
                          })()}
                          <div style={{ position: 'sticky', top: 0, zIndex: 2, padding: '6px 10px 4px', background: C.surface, color: C.textMuted, fontSize: '.58rem', fontWeight: 700, textTransform: 'uppercase', letterSpacing: '.09em', borderTop: `1px solid ${C.border}` }}>Uploaded Datasets</div>
                          {(() => {
                            const q = sourceSearch.toLowerCase().trim()
                            const filtered = (datasetList || []).filter(ds => (ds.filename || '').toLowerCase().includes(q))
                            if (!filtered.length) return <div style={{ padding: '8px 12px', color: C.textMuted, fontSize: '0.7rem' }}>No matching uploaded datasets.</div>
                            return filtered.map(ds => {
                              const isSel = ds.id === selectedDatasetId
                              const type = getFileType(ds.filename)
                              const tc = DS_COLOR[type] || C.textSec
                              return <div key={`dataset-${ds.id}`} onClick={() => { setSelectedDatasetId(ds.id); setSelectedDataSourceId(null); setDatasetExplicit(true); setDsPendingRun(null); setNoDsWarning(false); setSourcePicker(false) }}
                                style={{ display: 'grid', gridTemplateColumns: '32px minmax(0,1fr) auto 16px', alignItems: 'center', gap: 8, padding: '6px 10px', minHeight: 38, cursor: 'pointer', background: isSel ? 'rgba(99,102,241,0.08)' : 'transparent', borderBottom: `1px solid ${C.border}` }}>
                                <span style={{ color: tc, fontSize: '.55rem', fontWeight: 700 }}>{type.slice(0, 3)}</span>
                                <div style={{ minWidth: 0, fontSize: '.73rem', color: isSel ? '#6366f1' : C.text, lineHeight: 1.25, display: '-webkit-box', WebkitLineClamp: 2, WebkitBoxOrient: 'vertical', overflow: 'hidden', overflowWrap: 'anywhere' }}>{ds.filename}</div>
                                <span style={{ color: C.textMuted, fontSize: '.59rem', whiteSpace: 'nowrap' }}>{(ds.row_count || 0).toLocaleString()} rows · {ds.column_count || 0} cols</span>
                                {isSel && <span style={{ color: '#6366f1' }}>✓</span>}
                              </div>
                            })
                          })()}
                        </div>
                        {/* Footer */}
                        <div style={{ display: 'flex', borderTop: `1px solid ${C.border}` }}>
                          <div onClick={() => { setSourcePicker(false); setActiveNav('data-sources') }} style={{ flex: 1, padding: '8px 12px', cursor: 'pointer', color: '#6366f1', fontSize: '0.7rem', fontWeight: 600 }}>Manage data sources</div>
                          <div onClick={() => { setSourcePicker(false); fileInputRef.current?.click() }} style={{ flex: 1, padding: '8px 12px', cursor: 'pointer', color: '#6366f1', fontSize: '0.7rem', fontWeight: 600, borderLeft: `1px solid ${C.border}` }}>Upload dataset</div>
                        </div>
                      </div>
                    )}
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
                      onClick={() => { setDsPendingRun(null); setSourcePicker(true) }}
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
                  <div style={{ marginBottom: '7px', fontSize: '0.59rem', fontWeight: '600', color: C.textMuted, textTransform: 'uppercase', letterSpacing: '0.12em' }}>Uploaded datasets</div>
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

                  <div style={{ margin: '12px 0 7px', fontSize: '0.59rem', fontWeight: '600', color: C.textMuted, textTransform: 'uppercase', letterSpacing: '0.12em' }}>Database connections</div>
                  <div style={{ display: 'grid', gridTemplateColumns: 'repeat(4, 1fr)', gap: '8px' }}>
                    {DATABASE_QUICK_STARTS.map(qs => (
                      <div key={qs.text} className="ws-qs-card" onClick={() => setWsInput(qs.prompt)}
                        style={{ background: C.bg, border: `1px solid ${C.border}`, borderRadius: '12px', padding: '11px 12px', display: 'flex', alignItems: 'flex-start', gap: '9px', userSelect: 'none' }}>
                        <div style={{ flexShrink: 0, width: '30px', height: '30px', borderRadius: '8px', background: `${qs.color}20`, display: 'flex', alignItems: 'center', justifyContent: 'center' }}>
                          <QsIcon type={qs.iconType} color={qs.color} />
                        </div>
                        <div style={{ fontSize: '0.7rem', color: C.text, lineHeight: 1.4, fontWeight: '400' }}>{qs.text}</div>
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

          {/* ── Composer loading indicator ── */}
          {composerLoading && <ComposerLoading C={C} />}

          {/* ── Composer result panel ── */}
          {hasComposerResult && !backToComposer && (
            <ComposerResultPanel
              result={composerResult}
              wsInput={wsInput}
              C={C}
              onBack={handleReset}
              onOpenReport={onOpenReport}
              onResolveClarification={handleClarificationResolve}
              onCancelClarification={handleClarificationCancel}
              clarificationResubmitted={clarificationResubmitted}
              onAskFollowUp={q => { setWsInput(q); handleComposerAsk(q) }}
              token={token}
              onSessionExpired={onSessionExpired}
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

        {/* ── Right column — hidden during execution or composer result (those are full-width) ── */}
        {!activeLoading && !composerLoading && !hasComposerResult && (!hasResult || backToComposer) && (
          <div style={{ width: '280px', flexShrink: 0, display: 'flex', flexDirection: 'column' }}>
            <EmptyAssistantPanel proposal={wsProposal} C={C} onSuggestionSelect={q => setWsInput(q)} activeDs={activeDs} activeSource={activeSource} wsInput={wsInput} token={token} />
          </div>
        )}
        {!activeLoading && hasResult && !backToComposer && !hasComposerResult && (
          <div style={{ width: '280px', flexShrink: 0, display: 'flex', flexDirection: 'column' }}>
            <CopilotPanel
              reportId={intel?.reportId ?? null}
              aiMeta={intel?.aiMeta ?? null}
              intel={intel}
              user={user}
              token={token}
              onSessionExpired={onSessionExpired}
              C={C}
            />
          </div>
        )}
      </div>
    </div>
  )
}
