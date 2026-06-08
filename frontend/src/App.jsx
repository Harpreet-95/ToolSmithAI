import { useState, useEffect, useRef, lazy, Suspense } from 'react'
import { interpretTask, registerUser, loginUser, getUsage, getMyData, uploadDataset, getDatasets, getDatasetById, deleteDataset, renameDataset, createScheduledWorkflow, getScheduledWorkflows, deleteScheduledWorkflow, pauseScheduledWorkflow, resumeScheduledWorkflow, getWorkflows, saveWorkflow, deleteWorkflow, getRecommendations, getInsights, retryExecution, rerunExecution, getScheduleHealth, getWorkflowTemplates, explainContext, createMultiStepWorkflow, runWorkflowById, getReports, getReportById, deleteReport, exportReport, emailReport, getNotifications, markNotificationRead, deleteNotification, getScheduleRuns, getScheduleRunHistory, runScheduleNow, composeIntent, getWorkspaces, attachWorkspaceExecution, saveWorkspaceById, createWorkflowDraftFromWorkspace } from './api/client'
import ErrorBoundary from './components/ErrorBoundary'
import ChartSection from './components/ChartSection'

const ReportWorkspace    = lazy(() => import('./components/ReportWorkspace'))
const WorkflowResult     = lazy(() => import('./components/WorkflowResult'))
const DynamicToolComposer = lazy(() => import('./components/DynamicToolComposer'))
const ProposalPreview    = lazy(() => import('./components/ProposalPreview'))
const DatasetIntelligence = lazy(() => import('./components/DatasetIntelligence'))
const WorkspaceHistory    = lazy(() => import('./components/WorkspaceHistory'))
const OperationsCenter   = lazy(() => import('./components/OperationsCenter'))
const AdminDashboard     = lazy(() => import('./components/AdminDashboard'))
const AIWorkspace        = lazy(() => import('./components/AIWorkspace'))
const EngineWorkspace    = lazy(() => import('./components/EngineWorkspace'))

// ─── Lazy-load fallback ────────────────────────────────────────────────────────
function LazyFallback() {
  return (
    <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'center', padding: '40px 0', color: '#40475e', fontSize: '0.8rem' }}>
      Loading…
    </div>
  )
}

// ─── Design tokens ─────────────────────────────────────────────────────────────
const C_DARK = {
  bg:          '#07091a',
  sidebar:     '#080c1c',
  surface:     '#0d1128',
  border:      '#1e2b52',
  borderAlt:   '#283465',
  accent:      '#6366f1',
  accentSoft:  '#6366f11a',
  text:        '#eef0ff',
  textSec:     '#dde1ff',
  textMuted:   '#545c82',
  success:     '#10b981',
  successSoft: '#10b9811a',
  warn:        '#f59e0b',
  warnSoft:    '#f59e0b1a',
  danger:      '#f87171',
  dangerSoft:  '#f871711a',
}

const C_LIGHT = {
  bg:          '#f8f8fb',
  sidebar:     '#f0f0f6',
  surface:     '#ffffff',
  border:      '#e2e2ec',
  borderAlt:   '#d0d0de',
  accent:      '#6366f1',
  accentSoft:  '#6366f112',
  text:        '#111118',
  textSec:     '#5c5c72',
  textMuted:   '#9898b0',
  success:     '#059669',
  successSoft: '#05966912',
  warn:        '#d97706',
  warnSoft:    '#d9770612',
  danger:      '#dc2626',
  dangerSoft:  '#dc262612',
}

// LoginView always uses dark; DashboardView shadows this with the resolved theme
const C = C_DARK

const FONT      = "system-ui, -apple-system, 'Segoe UI', sans-serif"
const MONO      = "'Cascadia Code', 'Fira Code', 'JetBrains Mono', monospace"
const SIDEBAR_W = 216
const HEADER_H  = 56

function _relTime(iso) {
  if (!iso) return ''
  const diff = Date.now() - new Date(iso).getTime()
  const s = Math.floor(diff / 1000)
  if (s < 60) return 'just now'
  const m = Math.floor(s / 60)
  if (m < 60) return `${m}m ago`
  const h = Math.floor(m / 60)
  if (h < 24) return `${h}h ago`
  return `${Math.floor(h / 24)}d ago`
}

function _fmtDuration(ms) {
  if (ms == null) return '—'
  if (ms < 1000) return `${ms}ms`
  if (ms < 60000) return `${(ms / 1000).toFixed(1)}s`
  return `${Math.floor(ms / 60000)}m ${Math.round((ms % 60000) / 1000)}s`
}

// ─── Shared style primitives ───────────────────────────────────────────────────
function makeS(C) {
  return {
    card: {
      background: C.surface,
      border: `1px solid ${C.border}`,
      borderRadius: '10px',
      padding: '14px 16px',
    },
    input: {
      width: '100%',
      boxSizing: 'border-box',
      background: C.bg,
      border: `1px solid ${C.border}`,
      borderRadius: '8px',
      color: C.text,
      fontSize: '0.88rem',
      padding: '10px 14px',
      outline: 'none',
      fontFamily: MONO,
      letterSpacing: '0.02em',
    },
    textarea: {
      width: '100%',
      boxSizing: 'border-box',
      background: C.bg,
      border: `1px solid ${C.border}`,
      borderRadius: '8px',
      color: C.text,
      fontSize: '0.9rem',
      padding: '12px 14px',
      outline: 'none',
      resize: 'vertical',
      lineHeight: 1.65,
    },
    btnPrimary: {
      background: C.accent,
      color: '#fff',
      border: 'none',
      borderRadius: '8px',
      padding: '10px 22px',
      fontSize: '0.88rem',
      fontWeight: '600',
      cursor: 'pointer',
      fontFamily: FONT,
      letterSpacing: '0.01em',
    },
    label: {
      display: 'block',
      fontSize: '0.7rem',
      color: C.textSec,
      fontWeight: '600',
      marginBottom: '7px',
      letterSpacing: '0.06em',
      textTransform: 'uppercase',
    },
    badge: (color, bg) => ({
      display: 'inline-flex',
      alignItems: 'center',
      gap: '5px',
      background: bg,
      color: color,
      border: `1px solid ${color}30`,
      borderRadius: '20px',
      padding: '3px 10px',
      fontSize: '0.7rem',
      fontWeight: '600',
      letterSpacing: '0.04em',
      fontFamily: FONT,
    }),
    dot: (color) => ({
      width: '5px',
      height: '5px',
      borderRadius: '50%',
      background: color,
      flexShrink: 0,
    }),
  }
}

// LoginView always uses dark; DashboardView shadows this with the resolved theme
const S = makeS(C_DARK)

// ─── Dataset helpers ──────────────────────────────────────────────────────────
function getFileType(filename) {
  const ext = (filename || '').split('.').pop().toLowerCase()
  if (ext === 'csv') return 'CSV'
  if (['xlsx', 'xls'].includes(ext)) return 'Excel'
  if (ext === 'json') return 'JSON'
  if (ext === 'sql') return 'SQL'
  return ext.toUpperCase() || 'File'
}
function fmtRelTime(iso) {
  const diff = Date.now() - new Date(iso).getTime()
  const m = Math.floor(diff / 60000)
  const h = Math.floor(diff / 3600000)
  const d = Math.floor(diff / 86400000)
  if (m < 1) return 'just now'
  if (m < 60) return `${m}m ago`
  if (h < 24) return `${h}h ago`
  return `${d}d ago`
}
const DS_TYPE_STYLE = {
  CSV:   { color: '#10b981', bg: '#10b9811a' },
  Excel: { color: '#34d399', bg: '#34d3991a' },
  JSON:  { color: '#8b5cf6', bg: '#8b5cf61a' },
  SQL:   { color: '#f59e0b', bg: '#f59e0b1a' },
}

function fmtHistorySummary(row) {
  const tt = (row.task_type || '').toLowerCase()
  const intent = (row.intent || '').trim()
  const labelMap = {
    generate_dataset_report: 'Generate Report',
    email_dataset_report:    'Email Report',
    send_email:              'Send Email',
    generate_report:         'Generate Report',
    set_reminder:            'Set Reminder',
  }
  const label = labelMap[tt]
  if (label) return label
  if (intent) return intent.length > 52 ? intent.slice(0, 52) + '…' : intent
  return row.summary || '—'
}

function fmtHistorySubtitle(row) {
  const tt = (row.task_type || '').toLowerCase()
  const intent = (row.intent || '').trim()
  const typed = new Set(['generate_dataset_report','email_dataset_report','send_email','generate_report','set_reminder'])
  if (typed.has(tt) && intent) return intent.length > 58 ? intent.slice(0, 58) + '…' : intent
  return null
}

function fmtSrcLabel(row) {
  const raw = row.source_label || row.trigger_source || ''
  const map = {
    Manual:       'User Triggered',
    Scheduler:    'Scheduled',
    Workflow:     'Workflow Run',
    Composer:     'AI Generated',
    interpreter:  'User Triggered',
    scheduler:    'Scheduled',
    workflow_api: 'Workflow Run',
    composer:     'AI Generated',
  }
  return map[raw] || raw || '—'
}

function fmtHistoryDuration(row) {
  const ms = row.duration_ms
  const label = row.duration_label || ''
  if (ms === 0 || ms === null || ms === undefined) return 'Instant'
  if (label === '0ms') return 'Instant'
  return label || '—'
}

// ─── History task-type metadata ────────────────────────────────────────────────
const _HIST_TASK_META = {
  send_email: {
    color: '#3b82f6', bg: '#3b82f618',
    label: 'Email automation',
    icon: <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.9" strokeLinecap="round" strokeLinejoin="round"><line x1="22" y1="2" x2="11" y2="13"/><polygon points="22 2 15 22 11 13 2 9 22 2"/></svg>,
  },
  generate_report: {
    color: '#8b5cf6', bg: '#8b5cf618',
    label: 'AI report generation',
    icon: <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.9" strokeLinecap="round" strokeLinejoin="round"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/><line x1="16" y1="13" x2="8" y2="13"/><line x1="16" y1="17" x2="8" y2="17"/></svg>,
  },
  generate_dataset_report: {
    color: '#10b981', bg: '#10b98118',
    label: 'Dataset report',
    icon: <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.9" strokeLinecap="round" strokeLinejoin="round"><line x1="18" y1="20" x2="18" y2="10"/><line x1="12" y1="20" x2="12" y2="4"/><line x1="6" y1="20" x2="6" y2="14"/></svg>,
  },
  email_dataset_report: {
    color: '#06b6d4', bg: '#06b6d418',
    label: 'Dataset email report',
    icon: <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.9" strokeLinecap="round" strokeLinejoin="round"><path d="M4 4h16c1.1 0 2 .9 2 2v12c0 1.1-.9 2-2 2H4c-1.1 0-2-.9-2-2V6c0-1.1.9-2 2-2z"/><polyline points="22,6 12,13 2,6"/></svg>,
  },
  set_reminder: {
    color: '#f59e0b', bg: '#f59e0b18',
    label: 'Reminder workflow',
    icon: <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.9" strokeLinecap="round" strokeLinejoin="round"><path d="M18 8A6 6 0 0 0 6 8c0 7-3 9-3 9h18s-3-2-3-9"/><path d="M13.73 21a2 2 0 0 1-3.46 0"/></svg>,
  },
}
const _HIST_TASK_DEFAULT = {
  color: '#64748b', bg: '#64748b18',
  label: 'Automation workflow',
  icon: <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.9" strokeLinecap="round" strokeLinejoin="round"><polygon points="13 2 3 14 12 14 11 22 21 10 12 10 13 2"/></svg>,
}
function getHistMeta(taskType) {
  return _HIST_TASK_META[taskType] || _HIST_TASK_DEFAULT
}

// ─── Static data ───────────────────────────────────────────────────────────────
const STAT_CARDS = [
  { label: 'Tasks Run',            value: '—', accent: C.accent,  soft: C.accentSoft  },
  { label: 'Successful Workflows', value: '—', accent: C.success, soft: C.successSoft },
  { label: 'Workflow Runs',        value: '—', accent: C.warn,    soft: C.warnSoft    },
]

const NAV_ITEMS = [
  { id: 'ai-workspace', label: 'AI Workspace', icon: <svg width="15" height="15" viewBox="0 0 24 24" fill="currentColor"><path d="M12 2 L14 10 L22 12 L14 14 L12 22 L10 14 L2 12 L10 10 Z"/></svg> },
  { id: 'overview',  label: 'Overview',     icon: <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="m3 9 9-7 9 7v11a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/><polyline points="9 22 9 12 15 12 15 22"/></svg> },
  { id: 'workflows', label: 'Workflows',    icon: <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><circle cx="18" cy="5" r="3"/><circle cx="6" cy="12" r="3"/><circle cx="18" cy="19" r="3"/><line x1="8.59" y1="13.51" x2="15.42" y2="17.49"/><line x1="15.41" y1="6.51" x2="8.59" y2="10.49"/></svg> },
  { id: 'datasets',  label: 'Datasets',     icon: <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><ellipse cx="12" cy="5" rx="9" ry="3"/><path d="M21 12c0 1.66-4 3-9 3s-9-1.34-9-3"/><path d="M3 5v14c0 1.66 4 3 9 3s9-1.34 9-3V5"/></svg> },
  { id: 'scheduled',      label: 'Scheduled',       icon: <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><rect x="3" y="4" width="18" height="18" rx="2" ry="2"/><line x1="16" y1="2" x2="16" y2="6"/><line x1="8" y1="2" x2="8" y2="6"/><line x1="3" y1="10" x2="21" y2="10"/></svg> },
  { id: 'sched-activity', label: 'Sched. Activity', icon: <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><polyline points="22 12 18 12 15 21 9 3 6 12 2 12"/></svg> },
  { id: 'history',        label: 'History',         icon: <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 16 14"/></svg> },
  { id: 'operations',     label: 'Ops Center',      icon: <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><circle cx="12" cy="12" r="2"/><path d="M16.24 7.76a6 6 0 0 1 0 8.49m-8.48-.01a6 6 0 0 1 0-8.49m11.31-2.82a10 10 0 0 1 0 14.14m-14.14 0a10 10 0 0 1 0-14.14"/></svg> },
  { id: 'workspaces', label: 'Workspaces',   icon: <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><rect x="2" y="3" width="20" height="14" rx="2"/><path d="M8 21h8M12 17v4"/></svg> },
  { id: 'reports',   label: 'Reports',      icon: <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/><line x1="16" y1="13" x2="8" y2="13"/><line x1="16" y1="17" x2="8" y2="17"/></svg> },
  { id: 'usage',     label: 'Usage',        icon: <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><line x1="18" y1="20" x2="18" y2="10"/><line x1="12" y1="20" x2="12" y2="4"/><line x1="6" y1="20" x2="6" y2="14"/></svg> },
  { id: 'engine',    label: 'Engine Lab',   icon: <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M13 2L3 14h9l-1 8 10-12h-9l1-8z"/></svg> },
  { id: 'settings',  label: 'Settings',     icon: <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 0 1-2.83-2.83l.06-.06A1.65 1.65 0 0 0 4.68 15a1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 0 1 2.83-2.83l.06.06A1.65 1.65 0 0 0 9 4.68a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 0 1 2.83 2.83l-.06.06A1.65 1.65 0 0 0 19.4 9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z"/></svg> },
]

// Quick-start icon components (module-level so JSX is stable — avoids [object Object])
const QSIconReport    = () => <svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/><line x1="16" y1="13" x2="8" y2="13"/><line x1="16" y1="17" x2="8" y2="17"/></svg>
const QSIconAnalyze   = () => <svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round"><line x1="18" y1="20" x2="18" y2="10"/><line x1="12" y1="20" x2="12" y2="4"/><line x1="6" y1="20" x2="6" y2="14"/></svg>
const QSIconWorkflow  = () => <svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round"><polygon points="13 2 3 14 12 14 11 22 21 10 12 10 13 2"/></svg>
const QSIconSchedule  = () => <svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round"><rect x="3" y="4" width="18" height="18" rx="2" ry="2"/><line x1="16" y1="2" x2="16" y2="6"/><line x1="8" y1="2" x2="8" y2="6"/><line x1="3" y1="10" x2="21" y2="10"/></svg>
const QSIconEmail     = () => <svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round"><path d="M4 4h16c1.1 0 2 .9 2 2v12c0 1.1-.9 2-2 2H4c-1.1 0-2-.9-2-2V6c0-1.1.9-2 2-2z"/><polyline points="22,6 12,13 2,6"/></svg>

const FEATURES = [
  { title: 'AI-Powered',       sub: 'Smart intent interpretation', icon: 'brain'   },
  { title: 'Dynamic Tool',     sub: 'Generation',                  icon: 'tools'   },
  { title: 'Smart Workflow',   sub: 'Execution',                   icon: 'network' },
  { title: 'Secure & Reliable', sub: 'Enterprise-grade security',  icon: 'shield'  },
]

const WAVE_ARCS = [
  { w: '78%', h: '190px', b: '-100px', l: '-6%', opacity: 0.40 },
  { w: '66%', h: '150px', b: '-78px',  l: '2%',  opacity: 0.26 },
  { w: '54%', h: '115px', b: '-58px',  l: '9%',  opacity: 0.16 },
]

const PARTICLES = Array.from({ length: 110 }, (_, i) => ({
  x:   `${3  + ((i * 31 + 17) % 90)}%`,
  y:   `${3  + ((i * 47 + 11) % 88)}%`,
  s:   1.2 + (i % 4) * 0.8,
  del: `${(i * 0.37) % 5.5}s`,
  dur: `${1.5 + (i % 7) * 0.42}s`,
  op:  0.10 + (i % 9) * 0.038,
  col: i % 4 === 0 ? '#58a6ff' : i % 4 === 1 ? '#818cf8' : i % 4 === 2 ? '#b067f5' : '#67e8f9',
}))

// ─── Feature card icons ────────────────────────────────────────────────────────
function BrainIcon() {
  return (
    <svg width="32" height="32" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
      <path d="M12 5a3 3 0 1 0-5.997.125 4 4 0 0 0-2.526 5.77 4 4 0 0 0 .556 6.588A4 4 0 1 0 12 18Z"/>
      <path d="M12 5a3 3 0 1 1 5.997.125 4 4 0 0 1 2.526 5.77 4 4 0 0 1-.556 6.588A4 4 0 1 1 12 18Z"/>
      <path d="M15 13a4.5 4.5 0 0 1-3-4 4.5 4.5 0 0 1-3 4"/>
    </svg>
  )
}
function ToolsIcon() {
  return (
    <svg width="32" height="32" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
      <path d="M14.7 6.3a1 1 0 0 0 0 1.4l1.6 1.6a1 1 0 0 0 1.4 0l3.77-3.77a6 6 0 0 1-7.94 7.94l-6.91 6.91a2.12 2.12 0 0 1-3-3l6.91-6.91a6 6 0 0 1 7.94-7.94l-3.76 3.76z"/>
    </svg>
  )
}
function NetworkIcon() {
  return (
    <svg width="32" height="32" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
      <circle cx="12" cy="4" r="2"/><circle cx="4" cy="20" r="2"/><circle cx="20" cy="20" r="2"/>
      <path d="M12 6v4M8.5 17.5 12 10l3.5 7.5M6 20h12"/>
    </svg>
  )
}
function ShieldIcon() {
  return (
    <svg width="32" height="32" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
      <path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/>
      <path d="m9 12 2 2 4-4"/>
    </svg>
  )
}
const ICON_MAP = { brain: BrainIcon, tools: ToolsIcon, network: NetworkIcon, shield: ShieldIcon }

// ─── Small icon components ─────────────────────────────────────────────────────
function GoogleIcon() {
  return (
    <svg width="20" height="20" viewBox="0 0 24 24">
      <path d="M22.56 12.25c0-.78-.07-1.53-.2-2.25H12v4.26h5.92c-.26 1.37-1.04 2.53-2.21 3.31v2.77h3.57c2.08-1.92 3.28-4.74 3.28-8.09z" fill="#4285F4"/>
      <path d="M12 23c2.97 0 5.46-.98 7.28-2.66l-3.57-2.77c-.98.66-2.23 1.06-3.71 1.06-2.86 0-5.29-1.93-6.16-4.53H2.18v2.84C3.99 20.53 7.7 23 12 23z" fill="#34A853"/>
      <path d="M5.84 14.09c-.22-.66-.35-1.36-.35-2.09s.13-1.43.35-2.09V7.07H2.18C1.43 8.55 1 10.22 1 12s.43 3.45 1.18 4.93l2.85-2.22.81-.62z" fill="#FBBC05"/>
      <path d="M12 5.38c1.62 0 3.06.56 4.21 1.64l3.15-3.15C17.45 2.09 14.97 1 12 1 7.7 1 3.99 3.47 2.18 7.07l3.66 2.84c.87-2.6 3.3-4.53 6.16-4.53z" fill="#EA4335"/>
    </svg>
  )
}

function MicrosoftIcon() {
  return (
    <svg width="20" height="20" viewBox="0 0 21 21">
      <rect x="1"  y="1"  width="9" height="9" fill="#F25022"/>
      <rect x="11" y="1"  width="9" height="9" fill="#7FBA00"/>
      <rect x="1"  y="11" width="9" height="9" fill="#00A4EF"/>
      <rect x="11" y="11" width="9" height="9" fill="#FFB900"/>
    </svg>
  )
}

// ─── Login view ────────────────────────────────────────────────────────────────
function LoginView({ onSignIn, sessionExpired }) {
  const [email,        setEmail]        = useState('')
  const [password,     setPassword]     = useState('')
  const [showPassword, setShowPassword] = useState(false)
  const [rememberMe,   setRememberMe]   = useState(false)
  const [formError,    setFormError]    = useState('')
  const [loginLoading, setLoginLoading] = useState(false)
  const [mode,         setMode]         = useState('login')
  const [regName,      setRegName]      = useState('')
  const [regEmail,     setRegEmail]     = useState('')
  const [regPassword,  setRegPassword]  = useState('')
  const [regConfirm,   setRegConfirm]   = useState('')
  const [regError,     setRegError]     = useState('')
  const [regSuccess,   setRegSuccess]   = useState(false)
  const [regLoading,   setRegLoading]   = useState(false)

  const GT = {
    background: 'linear-gradient(135deg, #818cf8 0%, #6366f1 45%, #8b5cf6 100%)',
    WebkitBackgroundClip: 'text',
    WebkitTextFillColor: 'transparent',
    backgroundClip: 'text',
  }

  async function handleSubmit() {
    if (!email.trim() || !password.trim()) {
      setFormError('Please enter email and password.')
      return
    }
    setFormError('')
    setLoginLoading(true)
    try {
      const data = await loginUser(email, password)
      onSignIn(data.access_token, data.user)
    } catch (err) {
      setFormError(err.message.replace(/^\d+:\s*/, ''))
    } finally {
      setLoginLoading(false)
    }
  }

  async function handleRegister() {
    setRegError('')
    if (!regName.trim() || !regEmail.trim() || !regPassword.trim() || !regConfirm.trim()) {
      setRegError('All fields are required.')
      return
    }
    if (regPassword.length < 6) {
      setRegError('Password must be at least 6 characters.')
      return
    }
    if (regPassword !== regConfirm) {
      setRegError('Passwords do not match.')
      return
    }
    setRegLoading(true)
    try {
      await registerUser({ name: regName, email: regEmail, password: regPassword, role: 'user' })
      setRegPassword('')
      setRegConfirm('')
      setRegSuccess(true)
      setEmail(regEmail)
      setMode('login')
    } catch (err) {
      setRegError(err.message.replace(/^\d+:\s*/, ''))
    } finally {
      setRegLoading(false)
    }
  }

  return (
    <div style={{
      minHeight: '100vh',
      background: 'linear-gradient(145deg, #060818 0%, #0a0c1e 55%, #07091a 100%)',
      display: 'flex',
      flexWrap: 'wrap',
      fontFamily: FONT,
      color: '#ffffff',
      position: 'relative',
      overflowX: 'hidden',
    }}>
      <style>{`
        @keyframes tsFloat {
          0%,100% { transform: translateY(0px); }
          50%      { transform: translateY(-18px); }
        }
        .ts-signin-btn { transition: opacity 0.15s ease, transform 0.15s ease, box-shadow 0.2s ease; }
        .ts-signin-btn:hover { opacity: 0.9 !important; transform: translateY(-1px) !important; box-shadow: 0 8px 40px rgba(99,102,241,0.55) !important; }
        .ts-signin-btn:active { transform: translateY(0) !important; }
        .ts-input:focus { border-color: rgba(99,102,241,0.65) !important; box-shadow: 0 0 0 3px rgba(99,102,241,0.14) !important; }
        @media (min-width: 1100px) { .rw-nav-rail { display: block !important; } }
        .ts-social-btn { transition: background 0.18s ease, border-color 0.18s ease; }
        .ts-social-btn:hover { background: rgba(255,255,255,0.07) !important; border-color: rgba(255,255,255,0.18) !important; }
        .ts-feat-card { transition: background 0.2s ease, border-color 0.2s ease; }
        .ts-feat-card:hover { background: rgba(139,92,246,0.12) !important; border-color: rgba(139,92,246,0.55) !important; }
        .toolsmith-logo { width: 80px; height: auto; object-fit: contain; display: block; }
        .brand-logo-large { width: 110px; }
        .brand-logo-small { width: 82px; }
        @media (max-width: 860px) {
          .ts-landing-left { display: none !important; }
          .ts-landing-right { flex: 1 1 100% !important; min-height: 100vh !important; padding: 32px 20px !important; align-items: center !important; justify-content: center !important; }
        }
      `}</style>

      {/* Dot-grid texture */}
      <div style={{
        position: 'absolute', inset: 0,
        backgroundImage: 'radial-gradient(rgba(99,102,241,0.13) 1px, transparent 1px)',
        backgroundSize: '28px 28px',
        pointerEvents: 'none', zIndex: 0,
      }} />

      {/* Circuit-line overlay */}
      <svg style={{
        position: 'absolute', inset: 0, width: '100%', height: '100%',
        pointerEvents: 'none', zIndex: 0, opacity: 0.042,
      }} preserveAspectRatio="none">
        <defs>
          <pattern id="circ-tile" width="90" height="90" patternUnits="userSpaceOnUse">
            <path d="M 90,28 L 62,28 L 62,0" fill="none" stroke="#818cf8" strokeWidth="0.8"/>
            <path d="M 0,62 L 28,62 L 28,90" fill="none" stroke="#818cf8" strokeWidth="0.8"/>
            <circle cx="62" cy="28" r="3" fill="#818cf8"/>
            <circle cx="28" cy="62" r="3" fill="#818cf8"/>
          </pattern>
        </defs>
        <rect width="100%" height="100%" fill="url(#circ-tile)"/>
      </svg>

      {/* Full-page particles — fills the gap between panels */}
      <div style={{ position: 'absolute', inset: 0, overflow: 'hidden', pointerEvents: 'none', zIndex: 0 }}>
        {Array.from({ length: 50 }, (_, i) => ({
          x:   `${3  + ((i * 41 + 19) % 92)}%`,
          y:   `${3  + ((i * 59 + 13) % 90)}%`,
          s:   1.0 + (i % 4) * 0.75,
          del: `${(i * 0.39) % 6.0}s`,
          dur: `${1.7 + (i % 7) * 0.4}s`,
          op:  0.08 + (i % 8) * 0.035,
          col: i % 4 === 0 ? '#58a6ff' : i % 4 === 1 ? '#818cf8' : i % 4 === 2 ? '#b067f5' : '#67e8f9',
        })).map((p, i) => (
          <div key={i} style={{
            position: 'absolute', left: p.x, top: p.y,
            width: `${p.s}px`, height: `${p.s}px`,
            borderRadius: '50%', background: p.col, opacity: p.op,
            animation: `tsFloat ${p.dur} ${p.del} ease-in-out infinite`,
          }} />
        ))}
      </div>

      {/* Ambient glows */}
      <div style={{
        position: 'absolute', top: '-200px', left: '-100px',
        width: '700px', height: '600px',
        background: 'radial-gradient(ellipse, rgba(99,102,241,0.14) 0%, transparent 65%)',
        pointerEvents: 'none', zIndex: 0,
      }} />
      <div style={{
        position: 'absolute', bottom: '-150px', right: '20%',
        width: '500px', height: '500px',
        background: 'radial-gradient(circle, rgba(139,92,246,0.10) 0%, transparent 60%)',
        pointerEvents: 'none', zIndex: 0,
      }} />

      {/* ══════════════════════════════════════════════════════ */}
      {/* LEFT PANEL — 58%                                      */}
      {/* ══════════════════════════════════════════════════════ */}
      <div className="ts-landing-left" style={{
        flex: '1 1 480px',
        paddingLeft: 'clamp(32px, 5vw, 72px)',
        paddingTop: '56px',
        paddingBottom: 'clamp(40px, 5vh, 72px)',
        paddingRight: 'clamp(32px, 5vw, 64px)',
        display: 'flex',
        flexDirection: 'column',
        position: 'relative',
        zIndex: 1,
        minWidth: 0,
        boxSizing: 'border-box',
      }}>

        {/* Floating particles */}
        <div style={{ position: 'absolute', inset: 0, overflow: 'hidden', pointerEvents: 'none', zIndex: 0 }}>
          {PARTICLES.map((p, i) => (
            <div key={i} style={{
              position: 'absolute', left: p.x, top: p.y,
              width: `${p.s}px`, height: `${p.s}px`,
              borderRadius: '50%', background: p.col, opacity: p.op,
              animation: `tsFloat ${p.dur} ${p.del} ease-in-out infinite`,
            }} />
          ))}
        </div>

        {/* Brand row: logo + (ToolSmithAI title + tagline stacked) */}
        <div style={{
          display: 'flex',
          alignItems: 'flex-start',
          gap: '20px',
          position: 'relative', zIndex: 1,
        }}>
          <img
            src="/toolsmith-logo-transparent.png"
            alt="ToolSmithAI Logo"
            className="toolsmith-logo brand-logo-large"
          />
          <div style={{ display: 'flex', flexDirection: 'column' }}>
            <div style={{
              fontSize: 'clamp(36px, 4vw, 60px)',
              fontWeight: '650',
              lineHeight: 1,
              letterSpacing: '-1.8px',
              display: 'flex',
              alignItems: 'center',
            }}>
              <span style={{ color: '#ffffff' }}>ToolSmith</span>
              <span style={GT}>AI</span>
            </div>
            <p style={{
              margin: '12px 0 0',
              marginLeft: '12px',
              fontSize: '15px',
              fontWeight: '400',
              opacity: 0.78,
              letterSpacing: '0.2px',
              color: '#a0aec0',
            }}>
              Build. Automate. Accelerate.
            </p>
          </div>
        </div>

        {/* Main headline — nowrap on first line guarantees no per-word stacking */}
        <h1 style={{
          margin: '48px 0 22px',
          fontSize: 'clamp(22px, 2.9vw, 40px)',
          fontWeight: '600',
          lineHeight: 1.14,
          letterSpacing: '-1.2px',
          color: '#eef0f8',
          position: 'relative', zIndex: 1,
        }}>
          <span style={{ display: 'block', whiteSpace: 'nowrap' }}>Intelligent Task Automation</span>
          <span>Powered by <span style={GT}>AI</span></span>
        </h1>

        {/* Body paragraph */}
        <p style={{
          margin: '0 0 40px',
          fontSize: '17px',
          lineHeight: 1.8,
          fontWeight: '400',
          opacity: 0.82,
          maxWidth: '560px',
          color: '#a0b0cc',
          position: 'relative', zIndex: 1,
        }}>
          Transform your ideas into powerful automated workflows.
          ToolSmithAI interprets your intent, builds the right tools,
          and executes every step with precision and reliability.
        </p>

        {/* Feature cards */}
        <div style={{
          display: 'flex',
          gap: '18px',
          marginTop: '20px',
          position: 'relative', zIndex: 1,
        }}>
          {FEATURES.map(({ title, sub, icon }) => {
            const IconComponent = ICON_MAP[icon]
            return (
              <div
                key={title}
                className="ts-feat-card"
                style={{
                  width: '155px',
                  height: '155px',
                  borderRadius: '20px',
                  padding: '20px',
                  border: '1px solid rgba(139,92,246,0.35)',
                  background: 'rgba(139,92,246,0.06)',
                  boxSizing: 'border-box',
                  display: 'flex',
                  flexDirection: 'column',
                  alignItems: 'center',
                  cursor: 'default',
                  flexShrink: 0,
                }}
              >
                <div style={{ color: '#818cf8' }}><IconComponent /></div>
                <div style={{
                  fontSize: '14px',
                  fontWeight: '600',
                  lineHeight: 1.35,
                  marginTop: '14px',
                  color: '#e2e8f0',
                  textAlign: 'center',
                }}>
                  {title}
                </div>
                <div style={{
                  fontSize: '12px',
                  lineHeight: 1.55,
                  opacity: 0.72,
                  marginTop: '3px',
                  color: '#a0b0cc',
                  textAlign: 'center',
                }}>
                  {sub}
                </div>
              </div>
            )
          })}
        </div>

        {/* Bottom wave decoration */}
        <div style={{ position: 'absolute', bottom: 0, left: 0, right: 0, height: '260px', overflow: 'hidden', pointerEvents: 'none' }}>
          <div style={{
            position: 'absolute', bottom: '-30px', left: '-8%',
            width: '88%', height: '200px',
            background: 'radial-gradient(ellipse 75% 100% at 28% 100%, rgba(99,102,241,0.22) 0%, rgba(139,92,246,0.07) 55%, transparent 78%)',
          }} />
          {WAVE_ARCS.map((a, i) => (
            <div key={i} style={{
              position: 'absolute', bottom: a.b, left: a.l,
              width: a.w, height: a.h,
              borderTop:   `1px solid rgba(99,102,241,${a.opacity})`,
              borderLeft:  `1px solid rgba(99,102,241,${a.opacity * 0.45})`,
              borderRight: `1px solid rgba(99,102,241,${a.opacity * 0.45})`,
              borderRadius: '50%',
            }} />
          ))}
        </div>
      </div>

      {/* ══════════════════════════════════════════════════════ */}
      {/* RIGHT PANEL — 42%                                     */}
      {/* ══════════════════════════════════════════════════════ */}
      <div className="ts-landing-right" style={{
        flex: '1 1 380px',
        display: 'flex',
        alignItems: 'flex-start',
        justifyContent: 'center',
        padding: '56px 44px 32px 28px',
        position: 'relative',
        zIndex: 1,
        boxSizing: 'border-box',
      }}>

        {/* Right panel floating particles */}
        <div style={{ position: 'absolute', inset: 0, overflow: 'hidden', pointerEvents: 'none', zIndex: 0 }}>
          {Array.from({ length: 60 }, (_, i) => ({
            x:   `${4  + ((i * 29 + 11) % 90)}%`,
            y:   `${4  + ((i * 43 +  9) % 88)}%`,
            s:   1.2 + (i % 4) * 0.8,
            del: `${(i * 0.43) % 5.8}s`,
            dur: `${1.6 + (i % 6) * 0.45}s`,
            op:  0.10 + (i % 8) * 0.042,
            col: i % 4 === 0 ? '#58a6ff' : i % 4 === 1 ? '#818cf8' : i % 4 === 2 ? '#b067f5' : '#67e8f9',
          })).map((p, i) => (
            <div key={i} style={{
              position: 'absolute', left: p.x, top: p.y,
              width: `${p.s}px`, height: `${p.s}px`,
              borderRadius: '50%', background: p.col, opacity: p.op,
              animation: `tsFloat ${p.dur} ${p.del} ease-in-out infinite`,
            }} />
          ))}
        </div>

        {/* Card glow */}
        <div style={{
          position: 'absolute', top: '50%', left: '50%',
          transform: 'translate(-50%, -50%)',
          width: '560px', height: '560px',
          background: 'radial-gradient(circle, rgba(99,102,241,0.18) 0%, transparent 65%)',
          pointerEvents: 'none',
        }} />

        {/* Login card */}
        <div style={{
          width: '100%',
          maxWidth: '420px',
          padding: '32px 36px',
          borderRadius: '24px',
          transform: 'none',
          background: 'rgba(6, 8, 28, 0.96)',
          backdropFilter: 'blur(30px)',
          WebkitBackdropFilter: 'blur(30px)',
          border: '1px solid rgba(99,102,241,0.38)',
          boxShadow: '0 0 0 1px rgba(99,102,241,0.10), 0 0 60px rgba(99,102,241,0.18), inset 0 1px 0 rgba(255,255,255,0.06)',
          boxSizing: 'border-box',
          position: 'relative',
        }}>

          {mode === 'login' ? (<>

          {/* Welcome Back */}
          <h2 style={{
            margin: 0,
            fontSize: '26px',
            fontWeight: '500',
            lineHeight: 1.08,
            letterSpacing: '-0.8px',
            color: '#eef0f8',
            textAlign: 'center',
          }}>
            Welcome <span style={GT}>Back</span>
          </h2>

          {/* Subtitle */}
          <p style={{
            fontSize: '14px',
            lineHeight: 1.6,
            opacity: 0.78,
            marginTop: '10px',
            marginBottom: '26px',
            color: '#a0b0cc',
            textAlign: 'center',
          }}>
            Sign in to your ToolSmithAI account
          </p>

          {/* Session expired banner */}
          {sessionExpired && (
            <div style={{
              background: 'rgba(245,158,11,0.12)',
              border: '1px solid rgba(245,158,11,0.30)',
              borderRadius: '10px',
              padding: '12px 16px',
              marginBottom: '20px',
              fontSize: '14px',
              color: '#fcd34d',
            }}>
              Your session expired. Please log in again.
            </div>
          )}

          {/* Registration success (shown after auto-switch from register form) */}
          {regSuccess && (
            <div style={{
              background: 'rgba(16,185,129,0.10)',
              border: '1px solid rgba(16,185,129,0.28)',
              borderRadius: '10px',
              padding: '12px 16px',
              marginBottom: '20px',
              fontSize: '14px',
              color: '#6ee7b7',
            }}>
              Account created. You can now sign in.
            </div>
          )}

          {/* Form error */}
          {formError && (
            <div style={{
              background: 'rgba(248,113,113,0.10)',
              border: '1px solid rgba(248,113,113,0.28)',
              borderRadius: '10px',
              padding: '12px 16px',
              marginBottom: '20px',
              fontSize: '14px',
              color: '#fca5a5',
            }}>
              {formError}
            </div>
          )}

          {/* Email */}
          <label style={{
            display: 'block',
            fontSize: '14px',
            fontWeight: '500',
            marginBottom: '10px',
            color: '#c8d4ec',
          }}>Email address</label>
          <div style={{ position: 'relative', marginBottom: '20px' }}>
            <span style={{ position: 'absolute', left: '20px', top: '50%', transform: 'translateY(-50%)', pointerEvents: 'none' }}>
              <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="#5a6080" strokeWidth="2" strokeLinecap="round">
                <rect x="2" y="4" width="20" height="16" rx="2"/>
                <path d="m22 7-8.97 5.7a1.94 1.94 0 01-2.06 0L2 7"/>
              </svg>
            </span>
            <input
              type="email"
              placeholder="Enter your email"
              value={email}
              onChange={e => setEmail(e.target.value)}
              onKeyDown={e => e.key === 'Enter' && handleSubmit()}
              className="ts-input"
              style={{
                width: '100%', boxSizing: 'border-box',
                height: '50px',
                background: 'rgba(255,255,255,0.05)',
                border: '1px solid rgba(255,255,255,0.12)',
                borderRadius: '11px',
                color: '#ffffff',
                fontSize: '14px',
                paddingLeft: '54px',
                paddingRight: '20px',
                outline: 'none',
                fontFamily: FONT,
              }}
            />
          </div>

          {/* Password */}
          <label style={{
            display: 'block',
            fontSize: '14px',
            fontWeight: '500',
            marginBottom: '10px',
            color: '#c8d4ec',
          }}>Password</label>
          <div style={{ position: 'relative', marginBottom: '20px' }}>
            <span style={{ position: 'absolute', left: '20px', top: '50%', transform: 'translateY(-50%)', pointerEvents: 'none' }}>
              <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="#5a6080" strokeWidth="2" strokeLinecap="round">
                <rect x="3" y="11" width="18" height="11" rx="2"/>
                <path d="M7 11V7a5 5 0 0110 0v4"/>
              </svg>
            </span>
            <input
              type={showPassword ? 'text' : 'password'}
              placeholder="Enter your password"
              value={password}
              onChange={e => setPassword(e.target.value)}
              onKeyDown={e => e.key === 'Enter' && handleSubmit()}
              className="ts-input"
              style={{
                width: '100%', boxSizing: 'border-box',
                height: '50px',
                background: 'rgba(255,255,255,0.05)',
                border: '1px solid rgba(255,255,255,0.12)',
                borderRadius: '11px',
                color: '#ffffff',
                fontSize: '14px',
                paddingLeft: '54px',
                paddingRight: '50px',
                outline: 'none',
                fontFamily: FONT,
              }}
            />
            <button
              type="button"
              onClick={() => setShowPassword(v => !v)}
              style={{
                position: 'absolute', right: '16px', top: '50%',
                transform: 'translateY(-50%)',
                background: 'none', border: 'none', cursor: 'pointer',
                color: '#5a6080', padding: '4px', display: 'flex',
              }}
            >
              {showPassword ? (
                <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round">
                  <path d="M17.94 17.94A10.07 10.07 0 0112 20c-7 0-11-8-11-8a18.45 18.45 0 015.06-5.94M9.9 4.24A9.12 9.12 0 0112 4c7 0 11 8 11 8a18.5 18.5 0 01-2.16 3.19m-6.72-1.07a3 3 0 11-4.24-4.24M1 1l22 22"/>
                </svg>
              ) : (
                <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round">
                  <path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"/>
                  <circle cx="12" cy="12" r="3"/>
                </svg>
              )}
            </button>
          </div>

          {/* Remember me + Forgot */}
          <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: '20px' }}>
            <label style={{ display: 'flex', alignItems: 'center', gap: '8px', cursor: 'pointer' }}>
              <input
                type="checkbox"
                checked={rememberMe}
                onChange={e => setRememberMe(e.target.checked)}
                style={{ accentColor: '#6366f1', width: '15px', height: '15px', cursor: 'pointer' }}
              />
              <span style={{ fontSize: '12px', color: '#8892a4' }}>Remember me</span>
            </label>
            <span style={{ fontSize: '12px', color: '#818cf8', cursor: 'pointer', fontWeight: '500' }}>
              Forgot password?
            </span>
          </div>

          {/* Sign In button */}
          <button
            onClick={handleSubmit}
            disabled={loginLoading}
            className="ts-signin-btn"
            style={{
              width: '100%',
              height: '50px',
              background: 'linear-gradient(135deg, #3b82f6 0%, #6366f1 55%, #8b5cf6 100%)',
              color: '#ffffff',
              border: 'none',
              borderRadius: '11px',
              fontSize: '16px',
              fontWeight: '600',
              cursor: loginLoading ? 'not-allowed' : 'pointer',
              fontFamily: FONT,
              letterSpacing: '0.01em',
              display: 'flex',
              alignItems: 'center',
              justifyContent: 'center',
              gap: '8px',
              marginTop: '18px',
              marginBottom: '0',
              opacity: loginLoading ? 0.6 : 1,
              boxShadow: '0 4px 24px rgba(99,102,241,0.40), 0 2px 8px rgba(59,130,246,0.24)',
            }}
          >
            {loginLoading ? 'Signing in...' : <><span>Sign In</span><span style={{ fontSize: '18px' }}>→</span></>}
          </button>

          {/* Sign up prompt */}
          <div style={{
            borderTop: '1px solid rgba(255,255,255,0.07)',
            borderBottom: '1px solid rgba(255,255,255,0.07)',
            padding: '12px 0',
            marginTop: '16px',
            marginBottom: '16px',
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'center',
            gap: '8px',
          }}>
            <span style={{ fontSize: '12px', color: '#8892a4' }}>Don't have an account?</span>
            <button
              type="button"
              onClick={() => { setMode('register'); setFormError('') }}
              style={{
                background: 'none',
                border: 'none',
                color: '#818cf8',
                fontSize: '12px',
                fontWeight: '600',
                cursor: 'pointer',
                fontFamily: FONT,
                padding: 0,
                letterSpacing: '0.01em',
              }}
            >
              Sign Up
            </button>
          </div>

          {/* Divider */}
          <div style={{ display: 'flex', alignItems: 'center', gap: '12px', marginBottom: '12px' }}>
            <div style={{ flex: 1, height: '1px', background: 'rgba(255,255,255,0.07)' }} />
            <span style={{ fontSize: '11px', color: '#7a8aaa', whiteSpace: 'nowrap' }}>or continue with</span>
            <div style={{ flex: 1, height: '1px', background: 'rgba(255,255,255,0.07)' }} />
          </div>

          {/* Social buttons — side by side */}
          <div style={{ display: 'flex', gap: '10px', marginBottom: '14px' }}>
            <button
              className="ts-social-btn"
              onClick={() => setFormError('Social login is not connected in this local demo.')}
              style={{
                flex: 1,
                display: 'flex', alignItems: 'center', justifyContent: 'center', gap: '8px',
                height: '42px',
                background: 'rgba(255,255,255,0.07)',
                border: '1px solid rgba(255,255,255,0.18)',
                borderRadius: '10px',
                color: '#e2e8f0',
                fontSize: '12px', fontWeight: '600',
                cursor: 'pointer', fontFamily: FONT,
              }}
            >
              <GoogleIcon /> Google
            </button>
            <button
              className="ts-social-btn"
              onClick={() => setFormError('Social login is not connected in this local demo.')}
              style={{
                flex: 1,
                display: 'flex', alignItems: 'center', justifyContent: 'center', gap: '8px',
                height: '42px',
                background: 'rgba(255,255,255,0.07)',
                border: '1px solid rgba(255,255,255,0.18)',
                borderRadius: '10px',
                color: '#e2e8f0',
                fontSize: '12px', fontWeight: '600',
                cursor: 'pointer', fontFamily: FONT,
              }}
            >
              <MicrosoftIcon /> Microsoft
            </button>
          </div>

          {/* Footer */}
          <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'center', gap: '16px' }}>
            {['Secure', 'Encrypted', 'Trusted'].map((word, i) => (
              <div key={word} style={{ display: 'flex', alignItems: 'center', gap: '5px' }}>
                {i > 0 && <span style={{ color: '#4a5570', fontSize: '11px' }}>•</span>}
                <span style={{ color: '#6a7590', fontSize: '12px' }}>◈</span>
                <span style={{ color: '#6a7590', fontSize: '12px' }}>{word}</span>
              </div>
            ))}
          </div>

          </>) : (<>

          {/* Create your account */}
          <h2 style={{
            margin: 0,
            fontSize: '26px',
            fontWeight: '500',
            lineHeight: 1.08,
            letterSpacing: '-0.8px',
            color: '#eef0f8',
            textAlign: 'center',
          }}>
            Create your <span style={GT}>account</span>
          </h2>

          {/* Subtitle */}
          <p style={{
            fontSize: '14px',
            lineHeight: 1.6,
            opacity: 0.78,
            marginTop: '10px',
            marginBottom: '26px',
            color: '#a0b0cc',
            textAlign: 'center',
          }}>
            Start building AI-powered workflows with ToolSmithAI.
          </p>

          {/* Register error */}
          {regError && (
            <div style={{
              background: 'rgba(248,113,113,0.10)',
              border: '1px solid rgba(248,113,113,0.28)',
              borderRadius: '10px',
              padding: '12px 16px',
              marginBottom: '18px',
              fontSize: '14px',
              color: '#fca5a5',
            }}>
              {regError}
            </div>
          )}

          {/* Register success */}
          {regSuccess && (
            <div style={{
              background: 'rgba(16,185,129,0.10)',
              border: '1px solid rgba(16,185,129,0.28)',
              borderRadius: '10px',
              padding: '12px 16px',
              marginBottom: '18px',
              fontSize: '14px',
              color: '#6ee7b7',
            }}>
              Account created. You can now sign in.
            </div>
          )}

          {/* Full name */}
          <label style={{ display: 'block', fontSize: '14px', fontWeight: '500', marginBottom: '10px', color: '#c8d4ec' }}>Full name</label>
          <div style={{ position: 'relative', marginBottom: '18px' }}>
            <span style={{ position: 'absolute', left: '20px', top: '50%', transform: 'translateY(-50%)', pointerEvents: 'none' }}>
              <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="#5a6080" strokeWidth="2" strokeLinecap="round">
                <path d="M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2"/>
                <circle cx="12" cy="7" r="4"/>
              </svg>
            </span>
            <input
              type="text"
              placeholder="Enter your full name"
              value={regName}
              onChange={e => setRegName(e.target.value)}
              className="ts-input"
              style={{
                width: '100%', boxSizing: 'border-box',
                height: '50px',
                background: 'rgba(255,255,255,0.05)',
                border: '1px solid rgba(255,255,255,0.12)',
                borderRadius: '11px',
                color: '#ffffff',
                fontSize: '14px',
                paddingLeft: '54px',
                paddingRight: '20px',
                outline: 'none',
                fontFamily: FONT,
              }}
            />
          </div>

          {/* Email */}
          <label style={{ display: 'block', fontSize: '14px', fontWeight: '500', marginBottom: '10px', color: '#c8d4ec' }}>Email address</label>
          <div style={{ position: 'relative', marginBottom: '18px' }}>
            <span style={{ position: 'absolute', left: '20px', top: '50%', transform: 'translateY(-50%)', pointerEvents: 'none' }}>
              <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="#5a6080" strokeWidth="2" strokeLinecap="round">
                <rect x="2" y="4" width="20" height="16" rx="2"/>
                <path d="m22 7-8.97 5.7a1.94 1.94 0 01-2.06 0L2 7"/>
              </svg>
            </span>
            <input
              type="email"
              placeholder="Enter your email"
              value={regEmail}
              onChange={e => setRegEmail(e.target.value)}
              className="ts-input"
              style={{
                width: '100%', boxSizing: 'border-box',
                height: '50px',
                background: 'rgba(255,255,255,0.05)',
                border: '1px solid rgba(255,255,255,0.12)',
                borderRadius: '11px',
                color: '#ffffff',
                fontSize: '14px',
                paddingLeft: '54px',
                paddingRight: '20px',
                outline: 'none',
                fontFamily: FONT,
              }}
            />
          </div>

          {/* Password */}
          <label style={{ display: 'block', fontSize: '14px', fontWeight: '500', marginBottom: '10px', color: '#c8d4ec' }}>Password</label>
          <div style={{ position: 'relative', marginBottom: '18px' }}>
            <span style={{ position: 'absolute', left: '20px', top: '50%', transform: 'translateY(-50%)', pointerEvents: 'none' }}>
              <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="#5a6080" strokeWidth="2" strokeLinecap="round">
                <rect x="3" y="11" width="18" height="11" rx="2"/>
                <path d="M7 11V7a5 5 0 0110 0v4"/>
              </svg>
            </span>
            <input
              type="password"
              placeholder="Minimum 6 characters"
              value={regPassword}
              onChange={e => setRegPassword(e.target.value)}
              className="ts-input"
              style={{
                width: '100%', boxSizing: 'border-box',
                height: '50px',
                background: 'rgba(255,255,255,0.05)',
                border: '1px solid rgba(255,255,255,0.12)',
                borderRadius: '11px',
                color: '#ffffff',
                fontSize: '14px',
                paddingLeft: '54px',
                paddingRight: '20px',
                outline: 'none',
                fontFamily: FONT,
              }}
            />
          </div>

          {/* Confirm password */}
          <label style={{ display: 'block', fontSize: '14px', fontWeight: '500', marginBottom: '10px', color: '#c8d4ec' }}>Confirm password</label>
          <div style={{ position: 'relative', marginBottom: '22px' }}>
            <span style={{ position: 'absolute', left: '20px', top: '50%', transform: 'translateY(-50%)', pointerEvents: 'none' }}>
              <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="#5a6080" strokeWidth="2" strokeLinecap="round">
                <rect x="3" y="11" width="18" height="11" rx="2"/>
                <path d="M7 11V7a5 5 0 0110 0v4"/>
              </svg>
            </span>
            <input
              type="password"
              placeholder="Re-enter your password"
              value={regConfirm}
              onChange={e => setRegConfirm(e.target.value)}
              onKeyDown={e => e.key === 'Enter' && handleRegister()}
              className="ts-input"
              style={{
                width: '100%', boxSizing: 'border-box',
                height: '50px',
                background: 'rgba(255,255,255,0.05)',
                border: '1px solid rgba(255,255,255,0.12)',
                borderRadius: '11px',
                color: '#ffffff',
                fontSize: '14px',
                paddingLeft: '54px',
                paddingRight: '20px',
                outline: 'none',
                fontFamily: FONT,
              }}
            />
          </div>

          {/* Create Account button */}
          <button
            onClick={handleRegister}
            disabled={regLoading}
            className="ts-signin-btn"
            style={{
              width: '100%',
              height: '50px',
              background: 'linear-gradient(135deg, #3b82f6 0%, #6366f1 55%, #8b5cf6 100%)',
              color: '#ffffff',
              border: 'none',
              borderRadius: '11px',
              fontSize: '16px',
              fontWeight: '600',
              cursor: regLoading ? 'not-allowed' : 'pointer',
              fontFamily: FONT,
              letterSpacing: '0.01em',
              display: 'flex',
              alignItems: 'center',
              justifyContent: 'center',
              gap: '8px',
              opacity: regLoading ? 0.6 : 1,
              boxShadow: '0 4px 24px rgba(99,102,241,0.40), 0 2px 8px rgba(59,130,246,0.24)',
            }}
          >
            {regLoading ? 'Creating account...' : 'Create Account'}
          </button>

          {/* Back to Sign In */}
          <div style={{
            borderTop: '1px solid rgba(255,255,255,0.07)',
            paddingTop: '20px',
            marginTop: '20px',
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'center',
          }}>
            <button
              type="button"
              onClick={() => { setMode('login'); setRegError(''); setRegSuccess(false) }}
              style={{
                background: 'none',
                border: 'none',
                color: '#818cf8',
                fontSize: '14px',
                fontWeight: '500',
                cursor: 'pointer',
                fontFamily: FONT,
                padding: 0,
                display: 'flex',
                alignItems: 'center',
                gap: '6px',
                letterSpacing: '0.01em',
              }}
            >
              <span style={{ fontSize: '16px', lineHeight: 1 }}>←</span> Back to Sign In
            </button>
          </div>

          </>)}

        </div>
      </div>
    </div>
  )
}

// ─── Confirm modal ─────────────────────────────────────────────────────────────
function ConfirmModal({ title, body, confirmLabel, onConfirm, onCancel, C, S }) {
  return (
    <div style={{
      position: 'fixed', inset: 0, zIndex: 200,
      background: 'rgba(0,0,0,0.65)', backdropFilter: 'blur(2px)',
      display: 'flex', alignItems: 'center', justifyContent: 'center',
    }}>
      <div style={{
        background: C.surface, border: `1px solid ${C.borderAlt}`,
        borderRadius: '14px', padding: '28px 32px',
        width: '400px', maxWidth: 'calc(100vw - 32px)',
        boxShadow: '0 24px 64px rgba(0,0,0,0.55)', fontFamily: FONT,
      }}>
        <h3 style={{ margin: '0 0 10px', fontSize: '1rem', fontWeight: '700', color: C.text }}>{title}</h3>
        <p style={{ margin: '0 0 24px', fontSize: '0.84rem', color: C.textSec, lineHeight: 1.6 }}>{body}</p>
        <div style={{ display: 'flex', gap: '10px', justifyContent: 'flex-end' }}>
          <button onClick={onCancel} style={{ background: 'transparent', border: `1px solid ${C.border}`, borderRadius: '8px', padding: '8px 18px', color: C.textSec, fontSize: '0.84rem', cursor: 'pointer', fontFamily: FONT }}>
            Cancel
          </button>
          <button onClick={onConfirm} style={{ background: C.danger, border: 'none', borderRadius: '8px', padding: '8px 18px', color: '#fff', fontSize: '0.84rem', fontWeight: '600', cursor: 'pointer', fontFamily: FONT }}>
            {confirmLabel}
          </button>
        </div>
      </div>
    </div>
  )
}

// ─── Multi-step workflow builder ───────────────────────────────────────────────
const MULTI_STEP_TYPES = [
  { type: 'analyze_dataset',          label: 'Analyze Dataset'       },
  { type: 'generate_dataset_report',  label: 'Generate Report'       },
  { type: 'email_dataset_report',     label: 'Email Report'          },
  { type: 'send_notification',        label: 'Send Notification'     },
]

function WorkflowStepBuilder({ steps, onStepsChange, C, S }) {
  const [selectedType, setSelectedType] = useState(MULTI_STEP_TYPES[0].type)

  function addStep() {
    if (steps.length >= 10) return
    const info = MULTI_STEP_TYPES.find(t => t.type === selectedType)
    onStepsChange([...steps, { id: `s${Date.now()}`, type: selectedType, label: info.label }])
  }

  function removeStep(idx) {
    onStepsChange(steps.filter((_, i) => i !== idx))
  }

  function moveStep(idx, dir) {
    const next = [...steps]
    const target = idx + dir
    if (target < 0 || target >= next.length) return
    ;[next[idx], next[target]] = [next[target], next[idx]]
    onStepsChange(next)
  }

  const iconBtn = (color, disabled) => ({
    width: '26px', height: '26px', display: 'flex', alignItems: 'center', justifyContent: 'center',
    background: 'transparent', border: `1px solid ${C.border}`, borderRadius: '6px',
    cursor: disabled ? 'default' : 'pointer', color, opacity: disabled ? 0.3 : 1, flexShrink: 0,
  })

  return (
    <div>
      {steps.length === 0 && (
        <div style={{ padding: '18px', background: C.bg, border: `1px dashed ${C.border}`, borderRadius: '10px', marginBottom: '12px', textAlign: 'center' }}>
          <div style={{ fontSize: '0.73rem', color: C.textMuted }}>No steps yet — select a type below and click Add Step</div>
        </div>
      )}

      {steps.map((step, i) => (
        <div key={step.id} style={{
          display: 'flex', alignItems: 'center', gap: '10px',
          marginBottom: '6px', background: C.surface,
          border: `1px solid ${C.border}`, borderRadius: '9px', padding: '10px 14px',
        }}>
          <div style={{ width: '22px', height: '22px', borderRadius: '50%', background: C.accentSoft, color: C.accent, fontSize: '0.65rem', fontWeight: '700', display: 'flex', alignItems: 'center', justifyContent: 'center', flexShrink: 0 }}>
            {i + 1}
          </div>
          <span style={{ flex: 1, fontSize: '0.78rem', color: C.textSec, fontWeight: '400' }}>{step.label}</span>
          <button onClick={() => moveStep(i, -1)} disabled={i === 0} style={iconBtn(C.textMuted, i === 0)}>
            <svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round"><path d="m18 15-6-6-6 6"/></svg>
          </button>
          <button onClick={() => moveStep(i, 1)} disabled={i === steps.length - 1} style={iconBtn(C.textMuted, i === steps.length - 1)}>
            <svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round"><path d="m6 9 6 6 6-6"/></svg>
          </button>
          <button onClick={() => removeStep(i)} style={iconBtn(C.danger, false)}>
            <svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>
          </button>
        </div>
      ))}

      <div style={{ marginTop: '12px' }}>
        <div style={{ fontSize: '0.67rem', color: C.textMuted, fontWeight: '500', textTransform: 'uppercase', letterSpacing: '0.06em', marginBottom: '8px' }}>Step type</div>
        <div style={{ display: 'flex', gap: '6px', flexWrap: 'wrap', marginBottom: '12px' }}>
          {MULTI_STEP_TYPES.map(t => {
            const active = selectedType === t.type
            return (
              <button key={t.type} onClick={() => setSelectedType(t.type)} style={{
                padding: '5px 13px', borderRadius: '20px', fontSize: '0.73rem',
                cursor: 'pointer', fontFamily: FONT, fontWeight: active ? '500' : '400',
                border: `1px solid ${active ? C.accent : C.border}`,
                background: active ? C.accentSoft : 'transparent',
                color: active ? C.accent : C.textSec,
                transition: 'border-color 0.12s, background 0.12s, color 0.12s',
              }}>
                {t.label}
              </button>
            )
          })}
        </div>
        <button
          onClick={addStep}
          disabled={steps.length >= 10}
          style={{ ...S.btnPrimary, padding: '7px 18px', fontSize: '0.78rem', background: 'linear-gradient(135deg, #6366f1 0%, #8b5cf6 100%)', opacity: steps.length >= 10 ? 0.5 : 1 }}
        >
          + Add Step
        </button>
        {steps.length >= 10 && (
          <span style={{ marginLeft: '10px', fontSize: '0.71rem', color: C.warn }}>Maximum 10 steps reached.</span>
        )}
      </div>
    </div>
  )
}

// ─── Multi-step execution result renderer ──────────────────────────────────────
// ─── Report section renderer — type-dispatched ────────────────────────────────
// Handles both v1 reports (no 'type' field) and v2 reports ('type' present).
// Add new cases here as enterprise section types are introduced.
function ReportSection({ section, C }) {
  const secType = section.type || 'text'
  return (
    <div style={{ background: C.surface, border: `1px solid ${C.border}70`, borderRadius: '10px', padding: '14px 16px', height: '100%', boxSizing: 'border-box' }}>
      <div style={{ fontSize: '0.64rem', color: C.textMuted, fontWeight: '600', textTransform: 'uppercase', letterSpacing: '0.07em', marginBottom: '8px' }}>
        {section.heading}
      </div>
      {(() => {
        switch (secType) {
          case 'kpi': {
            const fmtKpiVal = (value, format) => {
              if (value == null) return '—'
              if (format === 'percent')  return `${value}%`
              if (format === 'currency') return `$${Number(value).toLocaleString()}`
              if (format === 'number')   return Number(value).toLocaleString()
              return String(value)
            }
            const KPI_STATUS = {
              good:    { color: C.success, bg: `${C.success}18`, border: `${C.success}30` },
              warning: { color: C.warn,    bg: `${C.warn}18`,    border: `${C.warn}30`    },
              risk:    { color: C.danger,  bg: `${C.danger}18`,  border: `${C.danger}30`  },
            }
            const DeltaTag = ({ delta, dir }) => {
              if (delta == null) return null
              const color = dir === 'up' ? C.success : dir === 'down' ? C.danger : C.textMuted
              const arrow = dir === 'up' ? '↑' : dir === 'down' ? '↓' : '→'
              return (
                <span style={{ fontSize: '0.62rem', color, fontWeight: '700', background: `${color}18`, borderRadius: '4px', padding: '1px 5px', flexShrink: 0 }}>
                  {arrow} {delta > 0 ? `+${delta}` : delta}
                </span>
              )
            }
            return (
              <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(150px, 1fr))', gap: '10px' }}>
                {(section.kpis || []).map((kpi, j) => {
                  const st = KPI_STATUS[kpi.status] || KPI_STATUS.good
                  return (
                    <div key={j} style={{ background: C.bg, border: `1px solid ${kpi.status ? st.border : C.border}`, borderRadius: '9px', padding: '11px 13px', position: 'relative' }}>
                      {kpi.status && kpi.status !== 'good' && (
                        <div style={{ position: 'absolute', top: '8px', right: '9px', width: '6px', height: '6px', borderRadius: '50%', background: st.color }} />
                      )}
                      <div style={{ fontSize: '0.61rem', color: C.textMuted, fontWeight: '600', textTransform: 'uppercase', letterSpacing: '0.06em', marginBottom: '5px' }}>
                        {kpi.label || '—'}
                      </div>
                      <div style={{ display: 'flex', alignItems: 'baseline', gap: '6px', marginBottom: '4px', flexWrap: 'wrap' }}>
                        <span style={{ fontSize: '1.25rem', fontWeight: '700', color: kpi.status ? st.color : C.text, letterSpacing: '-0.5px', lineHeight: 1 }}>
                          {fmtKpiVal(kpi.value, kpi.format)}
                        </span>
                        <DeltaTag delta={kpi.delta} dir={kpi.delta_direction} />
                      </div>
                      {kpi.explanation && (
                        <div style={{ fontSize: '0.64rem', color: st.color, fontWeight: '500', lineHeight: 1.4, marginBottom: '2px' }}>{kpi.explanation}</div>
                      )}
                      {!kpi.explanation && kpi.description && (
                        <div style={{ fontSize: '0.66rem', color: C.textMuted, lineHeight: 1.4 }}>{kpi.description}</div>
                      )}
                    </div>
                  )
                })}
              </div>
            )
          }
          case 'recommendation': {
            const recs = section.recommendations || []
            if (!recs.length) {
              return <div style={{ fontSize: '0.75rem', color: C.textMuted }}>No recommendations available.</div>
            }
            const PRIORITY = {
              high:   { color: C.danger,  bg: C.dangerSoft,  label: 'HIGH'   },
              medium: { color: C.warn,    bg: C.warnSoft,    label: 'MEDIUM' },
              low:    { color: C.success, bg: C.successSoft, label: 'LOW'    },
            }
            const ACTION_LABEL = {
              review:     'Review',
              clean_data: 'Clean Data',
              monitor:    'Monitor',
              segment:    'Segment',
              schedule:   'Schedule',
              export:     'Export',
            }
            return (
              <div style={{ display: 'flex', flexDirection: 'column', gap: '8px' }}>
                {recs.map((rec, j) => {
                  const p = PRIORITY[rec.priority] || PRIORITY.low
                  return (
                    <div key={j} style={{ background: C.bg, border: `1px solid ${C.border}`, borderRadius: '9px', padding: '10px 13px' }}>
                      <div style={{ display: 'flex', alignItems: 'center', gap: '8px', marginBottom: '5px', flexWrap: 'wrap' }}>
                        <span style={{ display: 'inline-flex', alignItems: 'center', background: p.bg, color: p.color, borderRadius: '4px', padding: '1px 6px', fontSize: '0.6rem', fontWeight: '700', letterSpacing: '0.07em', flexShrink: 0 }}>
                          {p.label}
                        </span>
                        <span style={{ fontSize: '0.78rem', fontWeight: '600', color: C.text }}>
                          {rec.title || '—'}
                        </span>
                      </div>
                      {rec.reason && (
                        <div style={{ fontSize: '0.74rem', color: C.textSec, lineHeight: 1.55, marginBottom: '5px' }}>{rec.reason}</div>
                      )}
                      <div style={{ display: 'flex', gap: '6px', flexWrap: 'wrap' }}>
                        {rec.action_type && (
                          <span style={{ fontSize: '0.62rem', color: C.textMuted, background: C.borderAlt, borderRadius: '4px', padding: '1px 6px', fontWeight: '500' }}>
                            {ACTION_LABEL[rec.action_type] || rec.action_type}
                          </span>
                        )}
                        {rec.confidence && (
                          <span style={{ fontSize: '0.62rem', color: C.textMuted, background: C.borderAlt, borderRadius: '4px', padding: '1px 6px', fontWeight: '500' }}>
                            {rec.confidence} confidence
                          </span>
                        )}
                      </div>
                    </div>
                  )
                })}
              </div>
            )
          }
          case 'executive_summary': {
            const { summary, key_takeaways: takeaways, risks, opportunities } = section
            const isAI = section.ai_generated === true
            const hasContent = summary || takeaways?.length || risks?.length || opportunities?.length
            if (!hasContent) {
              return <div style={{ fontSize: '0.75rem', color: C.textMuted }}>No executive summary available.</div>
            }
            const renderList = (label, items, dotColor) => {
              if (!items?.length) return null
              return (
                <div style={{ marginTop: '10px' }}>
                  <div style={{ fontSize: '0.62rem', fontWeight: '700', color: dotColor, textTransform: 'uppercase', letterSpacing: '0.07em', marginBottom: '5px' }}>{label}</div>
                  <div style={{ display: 'flex', flexDirection: 'column', gap: '4px' }}>
                    {items.map((item, j) => (
                      <div key={j} style={{ display: 'flex', alignItems: 'flex-start', gap: '7px', fontSize: '0.76rem', color: C.text, lineHeight: 1.55 }}>
                        <span style={{ color: dotColor, fontWeight: '700', flexShrink: 0, marginTop: '1px' }}>•</span>
                        <span>{item}</span>
                      </div>
                    ))}
                  </div>
                </div>
              )
            }
            return (
              <div>
                {isAI && (
                  <div style={{ display: 'inline-flex', alignItems: 'center', gap: '5px', background: '#10b9811a', border: '1px solid #10b98130', borderRadius: '4px', padding: '2px 8px', marginBottom: '8px' }}>
                    <span style={{ width: '5px', height: '5px', borderRadius: '50%', background: '#10b981', flexShrink: 0, display: 'inline-block' }} />
                    <span style={{ fontSize: '0.6rem', fontWeight: '700', color: '#10b981', letterSpacing: '0.06em', textTransform: 'uppercase' }}>AI Generated</span>
                  </div>
                )}
                {summary && <p style={{ margin: '0 0 2px', fontSize: '0.82rem', color: C.text, lineHeight: 1.65 }}>{summary}</p>}
                {renderList('Key Takeaways', takeaways,     C.accent)}
                {renderList('Risks',         risks,         C.danger)}
                {renderList('Opportunities', opportunities, C.success)}
              </div>
            )
          }
          case 'chart': {
            const chart = section.chart || {}
            return (
              <div>
                <ChartSection chart={chart} C={C} />
                {section.explanation && (
                  <div style={{ fontSize: '0.74rem', color: C.textSec, lineHeight: 1.6, marginTop: '10px' }}>
                    {section.explanation}
                  </div>
                )}
                {(chart.insight || chart.caption) && (
                  <div style={{ fontSize: '0.67rem', color: C.textMuted, lineHeight: 1.5, marginTop: '6px', fontStyle: 'italic' }}>
                    {chart.insight || chart.caption}
                  </div>
                )}
              </div>
            )
          }
          case 'drift_detection': {
            const drifts = section.drifts || []
            const bw     = section.baseline_window || {}
            if (!drifts.length) return null
            const DR_SEV = {
              high:   { color: C.danger,  bg: C.dangerSoft,  label: 'HIGH'   },
              medium: { color: C.warn,    bg: C.warnSoft,    label: 'MEDIUM' },
              low:    { color: C.accent,  bg: C.accentSoft,  label: 'LOW'    },
            }
            const DIR_ICON = { increase: '↑', decrease: '↓' }
            return (
              <div>
                {(bw.snapshot_count || bw.start) && (
                  <div style={{ fontSize: '0.65rem', color: C.textMuted, marginBottom: '10px', fontFamily: MONO }}>
                    Baseline: {bw.snapshot_count || '?'} snapshot{bw.snapshot_count !== 1 ? 's' : ''}
                    {bw.start ? ` · ${bw.start.replace('T',' ')} → ${(bw.end || bw.start).replace('T',' ')}` : ''}
                  </div>
                )}
                <div style={{ display: 'flex', flexDirection: 'column', gap: '7px' }}>
                  {drifts.map((d, j) => {
                    try {
                      const sv   = DR_SEV[d.severity] || DR_SEV.low
                      const icon = DIR_ICON[d.direction] || '↕'
                      const pct  = d.drift_percent != null ? `${d.drift_percent > 0 ? '+' : ''}${d.drift_percent}%` : '—'
                      return (
                        <div key={j} style={{ background: C.bg, border: `1px solid ${sv.color}28`, borderLeft: `3px solid ${sv.color}`, borderRadius: '9px', padding: '9px 13px' }}>
                          <div style={{ display: 'flex', alignItems: 'center', gap: '8px', marginBottom: '5px', flexWrap: 'wrap' }}>
                            <span style={{ display: 'inline-flex', alignItems: 'center', gap: '3px', background: sv.bg, color: sv.color, borderRadius: '4px', padding: '1px 6px', fontSize: '0.6rem', fontWeight: '700', letterSpacing: '0.07em', flexShrink: 0 }}>
                              {icon} {sv.label}
                            </span>
                            <span style={{ fontSize: '1rem', fontWeight: '800', color: sv.color, lineHeight: 1, flexShrink: 0 }}>{pct}</span>
                            <span style={{ fontSize: '0.78rem', fontWeight: '600', color: C.text }}>{d.metric || '—'}</span>
                          </div>
                          <div style={{ display: 'flex', gap: '16px', marginBottom: d.description ? '4px' : 0, flexWrap: 'wrap' }}>
                            <span style={{ fontSize: '0.68rem', color: C.textMuted }}>
                              Current: <span style={{ color: C.text, fontWeight: '600' }}>{d.current_value ?? '—'}</span>
                            </span>
                            <span style={{ fontSize: '0.68rem', color: C.textMuted }}>
                              Baseline avg: <span style={{ color: C.textSec }}>{d.baseline_value ?? '—'}</span>
                            </span>
                          </div>
                          {d.description && (
                            <div style={{ fontSize: '0.69rem', color: C.textMuted, fontFamily: MONO, lineHeight: 1.5 }}>{d.description}</div>
                          )}
                        </div>
                      )
                    } catch (_) { return null }
                  })}
                </div>
              </div>
            )
          }
          case 'historical_comparison': {
            const comps = section.comparisons || []
            const baseTs = section.baseline_timestamp
            if (!comps.length) {
              return <div style={{ fontSize: '0.75rem', color: C.textMuted }}>No previous baseline available yet.</div>
            }
            const SEV_COLOR = {
              positive: C.success,
              warning:  C.danger,
              neutral:  C.textSec,
            }
            const SEV_BG = {
              positive: C.successSoft,
              warning:  C.dangerSoft,
              neutral:  C.borderAlt,
            }
            const CHANGE_ICON = { increase: '↑', decrease: '↓', stable: '→' }
            return (
              <div>
                {baseTs && (
                  <div style={{ fontSize: '0.65rem', color: C.textMuted, marginBottom: '10px', fontFamily: MONO }}>
                    Baseline: {baseTs.slice(0, 19).replace('T', ' ')} UTC
                  </div>
                )}
                <div style={{ display: 'flex', flexDirection: 'column', gap: '1px', borderRadius: '8px', overflow: 'hidden', border: `1px solid ${C.border}` }}>
                  {/* Header row */}
                  <div style={{ display: 'grid', gridTemplateColumns: '1fr 80px 80px 80px', gap: '0', background: C.bg, padding: '6px 10px' }}>
                    {['Metric', 'Current', 'Previous', 'Change'].map((h, i) => (
                      <div key={i} style={{ fontSize: '0.58rem', fontWeight: '700', color: C.textMuted, textTransform: 'uppercase', letterSpacing: '0.07em', textAlign: i === 0 ? 'left' : 'right' }}>{h}</div>
                    ))}
                  </div>
                  {comps.map((c, j) => {
                    try {
                      const sevColor = SEV_COLOR[c.severity] || SEV_COLOR.neutral
                      const sevBg    = SEV_BG[c.severity]    || SEV_BG.neutral
                      const icon     = CHANGE_ICON[c.change_type] || '→'
                      const fmtVal = (v) => v == null ? '—' : (typeof v === 'number' ? v.toLocaleString() : String(v))
                      const fmtChg = (v) => {
                        if (v == null) return '—'
                        const n = Number(v)
                        if (!isFinite(n) || n === 0) return '0'
                        return (n > 0 ? '+' : '') + n.toLocaleString()
                      }
                      return (
                        <div key={j} style={{ display: 'grid', gridTemplateColumns: '1fr 80px 80px 80px', gap: '0', background: j % 2 === 0 ? C.surface : C.bg, padding: '7px 10px', borderTop: `1px solid ${C.border}` }}>
                          <div>
                            <div style={{ display: 'flex', alignItems: 'center', gap: '6px', flexWrap: 'wrap' }}>
                              <span style={{ display: 'inline-flex', alignItems: 'center', background: sevBg, color: sevColor, borderRadius: '4px', padding: '1px 5px', fontSize: '0.58rem', fontWeight: '700', letterSpacing: '0.06em', flexShrink: 0 }}>
                                {icon} {(c.severity || 'neutral').toUpperCase()}
                              </span>
                              <span style={{ fontSize: '0.74rem', fontWeight: '600', color: C.text }}>{c.metric || '—'}</span>
                            </div>
                            {c.description && (
                              <div style={{ fontSize: '0.67rem', color: C.textMuted, marginTop: '3px', lineHeight: 1.4 }}>{c.description}</div>
                            )}
                          </div>
                          <div style={{ textAlign: 'right', fontSize: '0.78rem', fontWeight: '700', color: C.text, alignSelf: 'center' }}>{fmtVal(c.current_value)}</div>
                          <div style={{ textAlign: 'right', fontSize: '0.74rem', color: C.textSec, alignSelf: 'center' }}>{fmtVal(c.previous_value)}</div>
                          <div style={{ textAlign: 'right', fontSize: '0.74rem', fontWeight: '600', color: sevColor, alignSelf: 'center' }}>{fmtChg(c.change)}</div>
                        </div>
                      )
                    } catch (_) { return null }
                  })}
                </div>
              </div>
            )
          }
          case 'predictive_readiness': {
            const prScore  = section.readiness_score
            const prLevel  = section.readiness_level || 'low'
            const signals  = section.signals   || []
            const nexts    = section.next_steps || []
            const PR_LEVEL = {
              high:   { color: C.success, bg: C.successSoft, label: 'HIGH'   },
              medium: { color: C.warn,    bg: C.warnSoft,    label: 'MEDIUM' },
              low:    { color: C.danger,  bg: C.dangerSoft,  label: 'LOW'    },
            }
            const PR_STATUS = {
              ready:   { color: C.success, bg: C.successSoft, symbol: '✓', label: 'READY'   },
              partial: { color: C.warn,    bg: C.warnSoft,    symbol: '~', label: 'PARTIAL' },
              missing: { color: C.danger,  bg: C.dangerSoft,  symbol: '✗', label: 'MISSING' },
            }
            const lv = PR_LEVEL[prLevel] || PR_LEVEL.low
            return (
              <div>
                <div style={{ display: 'flex', alignItems: 'center', gap: '12px', marginBottom: '12px' }}>
                  <div style={{ display: 'flex', flexDirection: 'column', alignItems: 'center', background: lv.bg, border: `1px solid ${lv.color}30`, borderRadius: '10px', padding: '8px 14px', minWidth: '64px' }}>
                    <span style={{ fontSize: '1.8rem', fontWeight: '800', color: lv.color, lineHeight: 1 }}>
                      {prScore != null ? prScore : '—'}
                    </span>
                    <span style={{ fontSize: '0.55rem', color: lv.color, fontWeight: '700', letterSpacing: '0.07em', textTransform: 'uppercase', marginTop: '3px' }}>/ 100</span>
                  </div>
                  <div>
                    <div style={{ display: 'inline-flex', alignItems: 'center', gap: '5px', background: lv.bg, color: lv.color, border: `1px solid ${lv.color}30`, borderRadius: '20px', padding: '3px 10px', fontSize: '0.7rem', fontWeight: '600', letterSpacing: '0.04em' }}>
                      <div style={{ width: '5px', height: '5px', borderRadius: '50%', background: lv.color, flexShrink: 0 }} />
                      {lv.label} READINESS
                    </div>
                    <div style={{ fontSize: '0.68rem', color: C.textMuted, marginTop: '5px' }}>
                      {signals.length} signal{signals.length !== 1 ? 's' : ''} evaluated
                    </div>
                  </div>
                </div>
                {signals.length > 0 && (
                  <div style={{ display: 'flex', flexDirection: 'column', gap: '6px', marginBottom: '12px' }}>
                    {signals.map((sig, j) => {
                      try {
                        const st = PR_STATUS[sig.status] || PR_STATUS.missing
                        return (
                          <div key={j} style={{ background: C.bg, border: `1px solid ${C.border}`, borderLeft: `3px solid ${st.color}`, borderRadius: '8px', padding: '8px 11px' }}>
                            <div style={{ display: 'flex', alignItems: 'center', gap: '8px', marginBottom: '3px', flexWrap: 'wrap' }}>
                              <span style={{ display: 'inline-flex', alignItems: 'center', background: st.bg, color: st.color, borderRadius: '4px', padding: '1px 6px', fontSize: '0.6rem', fontWeight: '700', letterSpacing: '0.06em', flexShrink: 0 }}>
                                {st.symbol} {st.label}
                              </span>
                              <span style={{ fontSize: '0.76rem', fontWeight: '600', color: C.text }}>{sig.name || '—'}</span>
                            </div>
                            {sig.description && (
                              <div style={{ fontSize: '0.72rem', color: C.textSec, lineHeight: 1.5, marginBottom: '2px' }}>{sig.description}</div>
                            )}
                            {sig.evidence && (
                              <div style={{ fontSize: '0.67rem', color: C.textMuted, fontFamily: MONO, lineHeight: 1.5 }}>{sig.evidence}</div>
                            )}
                          </div>
                        )
                      } catch (_) { return null }
                    })}
                  </div>
                )}
                {nexts.length > 0 && (
                  <div>
                    <div style={{ fontSize: '0.62rem', fontWeight: '700', color: C.accent, textTransform: 'uppercase', letterSpacing: '0.07em', marginBottom: '5px' }}>Next Steps</div>
                    <div style={{ display: 'flex', flexDirection: 'column', gap: '4px' }}>
                      {nexts.map((step, j) => (
                        <div key={j} style={{ display: 'flex', alignItems: 'flex-start', gap: '7px', fontSize: '0.74rem', color: C.text, lineHeight: 1.55 }}>
                          <span style={{ color: C.accent, fontWeight: '700', flexShrink: 0 }}>→</span>
                          <span>{typeof step === 'string' ? step : JSON.stringify(step)}</span>
                        </div>
                      ))}
                    </div>
                  </div>
                )}
                {!signals.length && !nexts.length && (
                  <div style={{ fontSize: '0.75rem', color: C.textMuted }}>Not assessed.</div>
                )}
              </div>
            )
          }
          case 'trend': {
            const trends = section.trends || []
            if (!trends.length) {
              return <div style={{ fontSize: '0.75rem', color: C.textMuted }}>No trend data available.</div>
            }
            const DIR = {
              up:       { color: C.success, bg: C.successSoft, symbol: '↑', label: 'UP'       },
              down:     { color: C.danger,  bg: C.dangerSoft,  symbol: '↓', label: 'DOWN'     },
              stable:   { color: C.accent,  bg: C.accentSoft,  symbol: '→', label: 'STABLE'   },
              volatile: { color: C.warn,    bg: C.warnSoft,    symbol: '↕', label: 'VOLATILE' },
            }
            const STR = {
              high:   { color: C.danger,  bg: C.dangerSoft  },
              medium: { color: C.warn,    bg: C.warnSoft    },
              low:    { color: C.textSec, bg: C.borderAlt   },
            }
            const CAT_LABEL = {
              time_series:  'Time Series',
              distribution: 'Distribution',
              completeness: 'Completeness',
              growth:       'Growth',
              decline:      'Decline',
            }
            return (
              <div style={{ display: 'flex', flexDirection: 'column', gap: '8px' }}>
                {trends.map((t, j) => {
                  try {
                    const d = DIR[t.direction] || DIR.stable
                    const s = STR[t.strength]  || STR.low
                    return (
                      <div key={j} style={{ background: C.bg, border: `1px solid ${d.color}22`, borderLeft: `3px solid ${d.color}`, borderRadius: '9px', padding: '10px 13px' }}>
                        <div style={{ display: 'flex', alignItems: 'center', gap: '8px', marginBottom: '5px', flexWrap: 'wrap' }}>
                          <span style={{ display: 'inline-flex', alignItems: 'center', gap: '4px', background: d.bg, color: d.color, borderRadius: '4px', padding: '1px 7px', fontSize: '0.6rem', fontWeight: '700', letterSpacing: '0.07em', flexShrink: 0 }}>
                            {d.symbol} {d.label}
                          </span>
                          <span style={{ display: 'inline-flex', alignItems: 'center', background: s.bg, color: s.color, borderRadius: '4px', padding: '1px 6px', fontSize: '0.6rem', fontWeight: '700', letterSpacing: '0.07em', flexShrink: 0 }}>
                            {(t.strength || 'low').toUpperCase()}
                          </span>
                          <span style={{ fontSize: '0.62rem', color: C.textMuted, background: C.borderAlt, borderRadius: '4px', padding: '1px 6px', fontWeight: '500' }}>
                            {CAT_LABEL[t.category] || t.category || 'Unknown'}
                          </span>
                          <span style={{ fontSize: '0.78rem', fontWeight: '600', color: C.text }}>
                            {t.title || '—'}
                          </span>
                        </div>
                        {t.description && (
                          <div style={{ fontSize: '0.74rem', color: C.textSec, lineHeight: 1.55, marginBottom: '4px' }}>{t.description}</div>
                        )}
                        {t.evidence && (
                          <div style={{ fontSize: '0.69rem', color: C.textMuted, fontFamily: MONO, lineHeight: 1.5 }}>{t.evidence}</div>
                        )}
                      </div>
                    )
                  } catch (_) {
                    return null
                  }
                })}
              </div>
            )
          }
          case 'anomaly': {
            const anomalies = section.anomalies || []
            if (!anomalies.length) {
              return <div style={{ fontSize: '0.75rem', color: C.textMuted }}>No anomalies detected.</div>
            }
            const SEV = {
              high:   { color: C.danger,  bg: C.dangerSoft,  label: 'HIGH'   },
              medium: { color: C.warn,    bg: C.warnSoft,    label: 'MEDIUM' },
              low:    { color: C.success, bg: C.successSoft, label: 'LOW'    },
            }
            const CAT_LABEL = {
              missing_data: 'Missing Data',
              distribution: 'Distribution',
              sample_size:  'Sample Size',
              schema:       'Schema',
              trend:        'Trend',
              quality:      'Quality',
            }
            return (
              <div style={{ display: 'flex', flexDirection: 'column', gap: '8px' }}>
                {anomalies.map((a, j) => {
                  try {
                    const s = SEV[a.severity] || SEV.low
                    return (
                      <div key={j} style={{ background: C.bg, border: `1px solid ${s.color}28`, borderLeft: `3px solid ${s.color}`, borderRadius: '9px', padding: '10px 13px' }}>
                        <div style={{ display: 'flex', alignItems: 'center', gap: '8px', marginBottom: '5px', flexWrap: 'wrap' }}>
                          <span style={{ display: 'inline-flex', alignItems: 'center', background: s.bg, color: s.color, borderRadius: '4px', padding: '1px 6px', fontSize: '0.6rem', fontWeight: '700', letterSpacing: '0.07em', flexShrink: 0 }}>
                            {s.label}
                          </span>
                          <span style={{ fontSize: '0.62rem', color: C.textMuted, background: C.borderAlt, borderRadius: '4px', padding: '1px 6px', fontWeight: '500' }}>
                            {CAT_LABEL[a.category] || a.category || 'Unknown'}
                          </span>
                          <span style={{ fontSize: '0.78rem', fontWeight: '600', color: C.text }}>
                            {a.title || '—'}
                          </span>
                        </div>
                        {a.description && (
                          <div style={{ fontSize: '0.74rem', color: C.textSec, lineHeight: 1.55, marginBottom: '4px' }}>{a.description}</div>
                        )}
                        {a.evidence && (
                          <div style={{ fontSize: '0.69rem', color: C.textMuted, fontFamily: MONO, lineHeight: 1.5 }}>{a.evidence}</div>
                        )}
                      </div>
                    )
                  } catch (_) {
                    return null
                  }
                })}
              </div>
            )
          }
          case 'ai_dashboard': {
            const { most_important_insight, highest_risk, recommended_action, watchlist } = section
            const Cell = ({ label, color, children }) => (
              <div style={{ background: `${color}0c`, border: `1px solid ${color}28`, borderRadius: '9px', padding: '10px 13px' }}>
                <div style={{ fontSize: '0.58rem', fontWeight: '700', color, textTransform: 'uppercase', letterSpacing: '0.07em', marginBottom: '5px' }}>{label}</div>
                <div style={{ fontSize: '0.76rem', color: C.text, lineHeight: 1.6 }}>{children}</div>
              </div>
            )
            return (
              <div style={{ display: 'flex', flexDirection: 'column', gap: '8px' }}>
                {most_important_insight && (
                  <Cell label="Most Important Insight" color={C.accent}>{most_important_insight}</Cell>
                )}
                {highest_risk && (
                  <Cell label="Highest Risk" color={C.danger}>{highest_risk}</Cell>
                )}
                {recommended_action && (
                  <Cell label="Recommended Action" color={C.success}>{recommended_action}</Cell>
                )}
                {(watchlist || []).length > 0 && (
                  <div style={{ background: `${C.warn}0c`, border: `1px solid ${C.warn}28`, borderRadius: '9px', padding: '10px 13px' }}>
                    <div style={{ fontSize: '0.58rem', fontWeight: '700', color: C.warn, textTransform: 'uppercase', letterSpacing: '0.07em', marginBottom: '6px' }}>Watchlist</div>
                    {watchlist.map((item, j) => (
                      <div key={j} style={{ display: 'flex', alignItems: 'flex-start', gap: '7px', marginBottom: '4px' }}>
                        <span style={{ color: C.warn, fontWeight: '700', flexShrink: 0, fontSize: '0.7rem', marginTop: '1px' }}>◉</span>
                        <span style={{ fontSize: '0.74rem', color: C.text, lineHeight: 1.5 }}>{item}</span>
                      </div>
                    ))}
                  </div>
                )}
              </div>
            )
          }
          case 'insight_priority': {
            const insights = section.insights || []
            if (!insights.length) return <div style={{ fontSize: '0.75rem', color: C.textMuted }}>No prioritized insights found.</div>
            const SEV = {
              high:   { color: C.danger,  label: 'HIGH'   },
              medium: { color: C.warn,    label: 'MEDIUM' },
              low:    { color: C.success, label: 'LOW'    },
            }
            return (
              <div style={{ display: 'flex', flexDirection: 'column', gap: '8px' }}>
                {insights.map((ins, j) => {
                  const s = SEV[ins.severity] || SEV.low
                  return (
                    <div key={j} style={{ background: `${s.color}08`, border: `1px solid ${s.color}28`, borderLeft: `3px solid ${s.color}`, borderRadius: '8px', padding: '10px 13px' }}>
                      <div style={{ display: 'flex', alignItems: 'center', gap: '8px', marginBottom: '5px', flexWrap: 'wrap' }}>
                        <span style={{ background: `${s.color}20`, color: s.color, borderRadius: '4px', padding: '1px 6px', fontSize: '0.6rem', fontWeight: '700', letterSpacing: '0.07em', flexShrink: 0 }}>{s.label}</span>
                        <span style={{ fontSize: '0.8rem', fontWeight: '600', color: C.text }}>{ins.title}</span>
                        {ins.confidence && (
                          <span style={{ fontSize: '0.6rem', color: C.textMuted, marginLeft: 'auto' }}>{ins.confidence} confidence</span>
                        )}
                      </div>
                      {ins.evidence && (
                        <div style={{ fontSize: '0.72rem', color: C.textSec, lineHeight: 1.5, marginBottom: ins.recommended_action ? '5px' : 0 }}>{ins.evidence}</div>
                      )}
                      {ins.recommended_action && (
                        <div style={{ fontSize: '0.7rem', color: C.success, display: 'flex', alignItems: 'center', gap: '5px' }}>
                          <span style={{ fontWeight: '700' }}>→</span> {ins.recommended_action}
                        </div>
                      )}
                    </div>
                  )
                })}
              </div>
            )
          }
          case 'drilldown_table': {
            const rows = section.rows || []
            const cols = section.columns || (rows[0] ? Object.keys(rows[0]) : [])
            if (!cols.length || !rows.length) return <div style={{ fontSize: '0.75rem', color: C.textMuted }}>No table data available.</div>
            return (
              <div style={{ overflowX: 'auto' }}>
                <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: '0.73rem', fontFamily: 'inherit' }}>
                  <thead>
                    <tr>
                      {cols.map((col, ci) => (
                        <th key={ci} style={{ padding: '6px 10px', textAlign: 'left', borderBottom: `2px solid ${C.border}`, color: C.textMuted, fontWeight: '600', fontSize: '0.6rem', textTransform: 'uppercase', letterSpacing: '0.06em', whiteSpace: 'nowrap' }}>{col}</th>
                      ))}
                    </tr>
                  </thead>
                  <tbody>
                    {rows.map((row, ri) => (
                      <tr key={ri} style={{ borderBottom: `1px solid ${C.border}` }}
                        onMouseEnter={e => { e.currentTarget.style.background = C.accentSoft }}
                        onMouseLeave={e => { e.currentTarget.style.background = 'transparent' }}>
                        {cols.map((col, ci) => (
                          <td key={ci} style={{ padding: '6px 10px', color: ci === 0 ? C.text : C.textSec, whiteSpace: 'nowrap' }}>
                            {row[col] != null ? String(row[col]) : '—'}
                          </td>
                        ))}
                      </tr>
                    ))}
                  </tbody>
                </table>
                {section.note && <div style={{ marginTop: '8px', fontSize: '0.65rem', color: C.textMuted }}>{section.note}</div>}
              </div>
            )
          }
          case 'ai_findings': {
            const items = section.items || []
            if (!items.length) return <div style={{ fontSize: '0.75rem', color: C.textMuted }}>No findings available.</div>
            return (
              <div style={{ display: 'flex', flexDirection: 'column', gap: '6px' }}>
                {items.map((item, j) => (
                  <div key={j} style={{ display: 'flex', alignItems: 'flex-start', gap: '9px', background: `${C.accent}0a`, border: `1px solid ${C.accent}22`, borderRadius: '8px', padding: '9px 13px' }}>
                    <span style={{ color: C.accent, fontWeight: '700', flexShrink: 0, fontSize: '0.78rem', marginTop: '1px' }}>◆</span>
                    <span style={{ fontSize: '0.76rem', color: C.text, lineHeight: 1.6 }}>{typeof item === 'string' ? item : JSON.stringify(item)}</span>
                  </div>
                ))}
              </div>
            )
          }
          case 'ai_insights': {
            const items = section.items || []
            if (!items.length) return <div style={{ fontSize: '0.75rem', color: C.textMuted }}>No insights available.</div>
            return (
              <div style={{ display: 'flex', flexDirection: 'column', gap: '7px' }}>
                {items.map((item, j) => {
                  const text = typeof item === 'string' ? item : JSON.stringify(item)
                  const isAnomaly = text.toLowerCase().startsWith('anomaly')
                  const isTrend   = text.toLowerCase().startsWith('trend')
                  const color     = isAnomaly ? C.danger : isTrend ? C.success : C.accent
                  const label     = isAnomaly ? '⚠ Anomaly Insight' : isTrend ? '↗ Trend Insight' : '● Insight'
                  const body      = text.replace(/^(anomaly insight|trend insight):\s*/i, '')
                  return (
                    <div key={j} style={{ background: `${color}08`, border: `1px solid ${color}28`, borderLeft: `3px solid ${color}`, borderRadius: '8px', padding: '9px 13px' }}>
                      <div style={{ fontSize: '0.58rem', fontWeight: '700', color, textTransform: 'uppercase', letterSpacing: '0.07em', marginBottom: '4px' }}>{label}</div>
                      <div style={{ fontSize: '0.76rem', color: C.text, lineHeight: 1.6 }}>{body}</div>
                    </div>
                  )
                })}
              </div>
            )
          }
          case 'ai_recommendations': {
            const items = section.items || []
            if (!items.length) return <div style={{ fontSize: '0.75rem', color: C.textMuted }}>No AI recommendations available.</div>
            return (
              <div style={{ display: 'flex', flexDirection: 'column', gap: '7px' }}>
                {items.map((item, j) => {
                  const text   = typeof item === 'string' ? item : JSON.stringify(item)
                  const isRisk = text.toLowerCase().startsWith('key risk')
                  const color  = isRisk ? C.warn : C.success
                  const label  = isRisk ? 'KEY RISK' : 'RECOMMENDATION'
                  const body   = text.replace(/^key risk:\s*/i, '')
                  return (
                    <div key={j} style={{ background: `${color}08`, border: `1px solid ${color}28`, borderLeft: `3px solid ${color}`, borderRadius: '8px', padding: '9px 13px' }}>
                      <div style={{ fontSize: '0.58rem', fontWeight: '700', color, textTransform: 'uppercase', letterSpacing: '0.07em', marginBottom: '4px' }}>{label}</div>
                      <div style={{ fontSize: '0.76rem', color: C.text, lineHeight: 1.6 }}>{body}</div>
                    </div>
                  )
                })}
              </div>
            )
          }
          case 'text':
          default:
            return (
              <div style={{ display: 'flex', flexDirection: 'column', gap: '6px' }}>
                {(section.items || []).map((item, j) => (
                  <div key={j} style={{ display: 'flex', alignItems: 'flex-start', gap: '8px', fontSize: '0.76rem', color: C.text, lineHeight: 1.6 }}>
                    <span style={{ color: C.accent, flexShrink: 0, fontWeight: '700' }}>→</span>
                    <span>{typeof item === 'string' ? item : JSON.stringify(item)}</span>
                  </div>
                ))}
              </div>
            )
        }
      })()}
    </div>
  )
}

// ─── Report generator ──────────────────────────────────────────────────────────
function buildReport(summary) {
  const fmt = (n, dec = 2) =>
    n == null ? '—' : Number(n).toLocaleString(undefined, { maximumFractionDigits: dec })

  const sections = []

  // Overview
  sections.push({
    heading: 'Overview',
    items: [
      `"${summary.filename}" contains ${summary.row_count.toLocaleString()} rows and ${summary.column_count} columns.`,
    ],
  })

  // Numeric Insights
  const numEntries = Object.entries(summary.numeric_profile).filter(([, s]) => s.mean != null)
  if (numEntries.length > 0) {
    const items = []
    const byMean = [...numEntries].sort((a, b) => b[1].mean - a[1].mean)
    const [highMeanCol, highMeanStats] = byMean[0]
    items.push(`${highMeanCol} has the highest average value at ${fmt(highMeanStats.mean)}.`)
    if (highMeanStats.min != null && highMeanStats.max != null) {
      items.push(`${highMeanCol} ranges from ${fmt(highMeanStats.min)} to ${fmt(highMeanStats.max)}.`)
    }
    const bySum = [...numEntries].filter(([, s]) => s.sum != null).sort((a, b) => b[1].sum - a[1].sum)
    if (bySum.length > 0) {
      const [highSumCol, highSumStats] = bySum[0]
      items.push(`${highSumCol} has the highest total at ${fmt(highSumStats.sum)}.`)
    }
    if (byMean.length > 1) {
      const [lowMeanCol, lowMeanStats] = byMean[byMean.length - 1]
      items.push(`${lowMeanCol} has the lowest average value at ${fmt(lowMeanStats.mean)}.`)
    }
    sections.push({ heading: 'Numeric Insights', items })
  }

  // Missing Data
  const missingEntries = Object.entries(summary.missing_values).filter(([, c]) => c > 0)
  const missingItems = []
  if (missingEntries.length === 0) {
    missingItems.push('No missing values were detected across all columns.')
  } else {
    missingEntries.slice(0, 5).forEach(([col, count]) => {
      const pct = ((count / summary.row_count) * 100).toFixed(1)
      missingItems.push(`${col} has ${count.toLocaleString()} missing values (${pct}% of rows).`)
    })
    if (missingEntries.length > 5) {
      missingItems.push(`...and ${missingEntries.length - 5} more columns with missing values.`)
    }
  }
  sections.push({ heading: 'Missing Data', items: missingItems })

  // Category Observations
  const catEntries = Object.entries(summary.categorical_profile).filter(([, e]) => e.length > 0)
  const catItems = catEntries.slice(0, 6).map(([col, entries]) => {
    const top = entries[0]
    return `${col} is most commonly "${top.value}" (${top.count.toLocaleString()} rows).`
  })
  if (catItems.length > 0) {
    sections.push({ heading: 'Top Category Observations', items: catItems })
  }

  return sections
}

// ─── Dashboard view ────────────────────────────────────────────────────────────
function DashboardView({ token, user, onLogout, onSessionExpired, theme, setTheme }) {
  const resolvedTheme = theme === 'system'
    ? (window.matchMedia?.('(prefers-color-scheme: dark)').matches ? 'dark' : 'light')
    : (theme || 'dark')
  // Shadow module-level C and S with theme-aware versions
  const C = resolvedTheme === 'light' ? C_LIGHT : C_DARK
  const S = makeS(C)

  const [activeNav,       setActiveNav]       = useState('ai-workspace')
  const [profileMenuOpen, setProfileMenuOpen] = useState(false)
  const [usage,        setUsage]        = useState(null)
  const [usageLoading,   setUsageLoading]   = useState(false)
  const [history,        setHistory]        = useState([])
  const [historyLoading, setHistoryLoading] = useState(false)
  const [datasetFile,    setDatasetFile]    = useState(null)
  const [datasetLoading, setDatasetLoading] = useState(false)
  const [datasetError,   setDatasetError]   = useState(null)
  const [datasetSummary,      setDatasetSummary]      = useState(null)
  const [report,              setReport]              = useState(null)
  const [datasetList,         setDatasetList]         = useState([])
  const [datasetListLoading,  setDatasetListLoading]  = useState(false)
  const [selectedDatasetId,   setSelectedDatasetId]   = useState(null)
  const [datasetExplicit,     setDatasetExplicit]     = useState(false)
  const [scheduledList,       setScheduledList]       = useState([])
  const [scheduledLoading,    setScheduledLoading]    = useState(false)
  const [scheduleInput,       setScheduleInput]       = useState('')
  const [scheduleFreq,        setScheduleFreq]        = useState('daily')
  const [scheduleCreating,    setScheduleCreating]    = useState(false)
  const [scheduleError,       setScheduleError]       = useState(null)
  const [scheduleSuccess,     setScheduleSuccess]     = useState(null)
  const [schedulePauseLoading,  setSchedulePauseLoading]  = useState(new Set())
  const [schedRuns,             setSchedRuns]             = useState([])
  const [schedRunsLoading,      setSchedRunsLoading]      = useState(false)
  const [schedRunsError,        setSchedRunsError]        = useState(null)
  const [schedExpandedId,       setSchedExpandedId]       = useState(null)
  const [schedRunHistories,     setSchedRunHistories]     = useState({})
  const [schedRunHistLoading,   setSchedRunHistLoading]   = useState(new Set())
  const [schedRunNowLoading,    setSchedRunNowLoading]    = useState(new Set())
  const [scheduleHealth,       setScheduleHealth]       = useState([])
  const [scheduleHealthLoading, setScheduleHealthLoading] = useState(false)
  const [workflowList,        setWorkflowList]        = useState([])
  const [workflowListLoading, setWorkflowListLoading] = useState(false)
  const [wfNameColW,          setWfNameColW]          = useState(220)
  const [wfSaveName,          setWfSaveName]          = useState('')
  const [wfSaveIntent,        setWfSaveIntent]        = useState('')
  const [wfSaving,            setWfSaving]            = useState(false)
  const [wfSaveError,         setWfSaveError]         = useState(null)
  const [wfSaveSuccess,       setWfSaveSuccess]       = useState(null)
  const [wfRunResult,         setWfRunResult]         = useState(null)
  const [wfRunningId,         setWfRunningId]         = useState(null)
  const [wfRunError,          setWfRunError]          = useState(null)
  const [recList,             setRecList]             = useState([])
  const [recLoading,          setRecLoading]          = useState(false)
  const [templates,           setTemplates]           = useState([])
  const [templatesLoading,    setTemplatesLoading]    = useState(false)
  const [insights,            setInsights]            = useState([])
  const [insightsLoading,     setInsightsLoading]     = useState(false)
  const [execActionLoading,   setExecActionLoading]   = useState(new Set())
  const [historyMsg,          setHistoryMsg]          = useState(null)
  const [historyMsgType,      setHistoryMsgType]      = useState('success')
  const [hoveredHistRow,      setHoveredHistRow]      = useState(null)
  const [historySearch,       setHistorySearch]       = useState('')
  const [explainLoading,      setExplainLoading]      = useState(new Set())
  const [explainData,         setExplainData]         = useState({})
  const [builderSteps,        setBuilderSteps]        = useState([])
  const [builderName,         setBuilderName]         = useState('')
  const [builderSaving,       setBuilderSaving]       = useState(false)
  const [builderError,        setBuilderError]        = useState(null)
  const [builderSuccess,      setBuilderSuccess]      = useState(null)
  const [composerMode,        setComposerMode]        = useState('workflow')
  const [activeWorkspaceId,    setActiveWorkspaceId]    = useState(null)
  const [workspaceList,        setWorkspaceList]        = useState([])
  const [workspaceListLoading, setWorkspaceListLoading] = useState(false)
  const [workspaceRunning,     setWorkspaceRunning]     = useState(false)
  const [dsModal,             setDsModal]             = useState(null)
  const [dsRenaming,          setDsRenaming]          = useState(null)
  const [dsRenameVal,         setDsRenameVal]         = useState('')
  const [dsRenameSaving,      setDsRenameSaving]      = useState(false)
  const [dsToast,             setDsToast]             = useState(null)
  const [dsSearch,     setDsSearch]     = useState('')
  const [dsTypeFilter, setDsTypeFilter] = useState('all')
  const [dsSortBy,     setDsSortBy]     = useState('newest')
  const [dsOpenMenu,   setDsOpenMenu]   = useState(null)
  const [dsPickerOpen, setDsPickerOpen] = useState(false)
  const [dsDragOver,   setDsDragOver]   = useState(false)
  const [reportList,            setReportList]            = useState([])
  const [reportListLoading,     setReportListLoading]     = useState(false)
  const [reportListError,       setReportListError]       = useState(null)
  const [selectedReportId,      setSelectedReportId]      = useState(null)
  const [selectedReportData,    setSelectedReportData]    = useState(null)
  const [selectedReportLoading, setSelectedReportLoading] = useState(false)
  const [reportViewMode,        setReportViewMode]        = useState('list')
  const [reportEmailInput,      setReportEmailInput]      = useState('')
  const [reportEmailStatus,     setReportEmailStatus]     = useState(null)
  const [reportEmailSending,    setReportEmailSending]    = useState(false)
  const [notifications,  setNotifications]  = useState([])
  const [notifOpen,      setNotifOpen]      = useState(false)
  const [notifLoading,   setNotifLoading]   = useState(false)
  const [notifError,     setNotifError]     = useState(null)
  const prevSummaryIdRef = useRef(null)
  const dsFileInputRef        = useRef(null)

  const is401 = err => err?.message?.startsWith('401:')

  useEffect(() => {
    if (!dsToast) return
    const t = setTimeout(() => setDsToast(null), 3000)
    return () => clearTimeout(t)
  }, [dsToast])

  // Keep prevSummaryIdRef in sync with the currently loaded summary's dataset ID.
  // This ref is used by the effect below to skip redundant fetches (e.g. after upload).
  useEffect(() => {
    prevSummaryIdRef.current = datasetSummary?.dataset_id ?? null
  }, [datasetSummary])

  // Whenever selectedDatasetId changes, fetch the full dataset profile and
  // display it in the summary panel. Skips the fetch if the summary is already
  // for the correct dataset (e.g. freshly uploaded — preserves sample_rows).
  useEffect(() => {
    if (!selectedDatasetId) { setDatasetSummary(null); setReport(null); return }
    if (prevSummaryIdRef.current === selectedDatasetId) return
    getDatasetById(selectedDatasetId, token)
      .then(data => { setDatasetSummary(data.data); setReport(null) })
      .catch(err => { if (is401(err)) onSessionExpired() })
  }, [selectedDatasetId, token]) // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    setUsageLoading(true)
    getUsage(token)
      .then(data => setUsage(data))
      .catch(err => { if (is401(err)) onSessionExpired() })
      .finally(() => setUsageLoading(false))
  }, [token])

  function refreshHistory() {
    setHistoryLoading(true)
    getMyData(token)
      .then(data => setHistory(data?.data?.execution_history ?? []))
      .catch(err => { if (is401(err)) onSessionExpired() })
      .finally(() => setHistoryLoading(false))
  }

  function refreshWorkspaces() {
    setWorkspaceListLoading(true)
    getWorkspaces(token)
      .then(data => setWorkspaceList(data?.data ?? []))
      .catch(err => { if (is401(err)) onSessionExpired() })
      .finally(() => setWorkspaceListLoading(false))
  }

  useEffect(() => { refreshHistory() }, [token])

  const statValues = {
    'Tasks Run':            usage?.data?.total_events                ?? '—',
    'Successful Workflows': usage?.data?.by_event_type?.interpret    ?? '—',
    'Workflow Runs':        usage?.data?.by_event_type?.workflow_run ?? '—',
  }

  function refreshWorkflows() {
    setWorkflowListLoading(true)
    getWorkflows(token)
      .then(data => setWorkflowList(data?.data ?? []))
      .catch(err => { if (is401(err)) onSessionExpired() })
      .finally(() => setWorkflowListLoading(false))
  }

  useEffect(() => { refreshWorkflows() }, [token])

  async function handleSaveDraft() {
    if (!activeWorkspaceId) throw new Error('No active workspace to save as draft.')
    const result = await createWorkflowDraftFromWorkspace(activeWorkspaceId, token)
    refreshWorkflows()
    return result
  }

  async function handleSaveMultiStepWorkflow() {
    if (!builderName.trim()) { setBuilderError('Enter a workflow name.'); return }
    if (builderSteps.length === 0) { setBuilderError('Add at least one step.'); return }
    setBuilderError(null)
    setBuilderSuccess(null)
    setBuilderSaving(true)
    try {
      await createMultiStepWorkflow(builderName.trim(), builderSteps, token)
      setBuilderName('')
      setBuilderSteps([])
      setBuilderSuccess('Multi-step workflow saved.')
      refreshWorkflows()
    } catch (err) {
      if (is401(err)) { onSessionExpired(); return }
      setBuilderError(err.message.replace(/^\d+:\s*/, ''))
    } finally {
      setBuilderSaving(false)
    }
  }

  async function handleSaveWorkflow() {
    if (!wfSaveName.trim()) { setWfSaveError('Enter a workflow name.'); return }
    if (!wfSaveIntent.trim()) { setWfSaveError('Enter a task description to save.'); return }
    setWfSaveError(null)
    setWfSaveSuccess(null)
    setWfSaving(true)
    try {
      await saveWorkflow(wfSaveName.trim(), wfSaveIntent.trim(), token)
      setWfSaveName('')
      setWfSaveIntent('')
      setWfSaveSuccess('Workflow saved.')
      refreshWorkflows()
    } catch (err) {
      if (is401(err)) { onSessionExpired(); return }
      setWfSaveError(err.message.replace(/^\d+:\s*/, ''))
    } finally {
      setWfSaving(false)
    }
  }

  async function handleRunWorkflow(wf) {
    if (!wf?.id) {
      console.error('[handleRunWorkflow] workflow missing ID', wf)
      setWfRunError('Cannot run workflow: workflow ID is missing.')
      return
    }
    const isMultiStep = Array.isArray(wf.definition?.workflow_steps)
    const intent = wf.definition?.intent || wf.name
    setWfRunningId(wf.id)
    setWfRunError(null)
    setWfRunResult(null)
    try {
      const data = await runWorkflowById(wf.id, token)
      setWfRunResult(data.data)
      getUsage(token).then(d => setUsage(d)).catch(() => {})
      getMyData(token).then(d => setHistory(d?.data?.execution_history ?? [])).catch(() => {})
    } catch (err) {
      if (is401(err)) { onSessionExpired(); return }
      setWfRunError(err.message.replace(/^\d+:\s*/, ''))
    } finally {
      setWfRunningId(null)
    }
  }

  function startWfColResize(e) {
    e.preventDefault()
    const startX = e.clientX
    const startW = wfNameColW
    const onMove = (ev) => setWfNameColW(Math.max(80, startW + ev.clientX - startX))
    const onUp = () => { document.removeEventListener('mousemove', onMove); document.removeEventListener('mouseup', onUp) }
    document.addEventListener('mousemove', onMove)
    document.addEventListener('mouseup', onUp)
  }

  async function handleDeleteWorkflow(id) {
    try {
      await deleteWorkflow(id, token)
      if (wfRunResult) setWfRunResult(null)
      refreshWorkflows()
    } catch (err) {
      if (is401(err)) onSessionExpired()
    }
  }

  function refreshScheduled() {
    setScheduledLoading(true)
    getScheduledWorkflows(token)
      .then(data => setScheduledList(data?.data ?? []))
      .catch(err => { if (is401(err)) onSessionExpired() })
      .finally(() => setScheduledLoading(false))
  }

  useEffect(() => { refreshScheduled() }, [token])

  function refreshScheduleHealth() {
    setScheduleHealthLoading(true)
    getScheduleHealth(token)
      .then(data => setScheduleHealth(data?.data ?? []))
      .catch(err => { if (is401(err)) onSessionExpired() })
      .finally(() => setScheduleHealthLoading(false))
  }

  useEffect(() => { refreshScheduleHealth() }, [token])

  function refreshTemplates() {
    setTemplatesLoading(true)
    getWorkflowTemplates(token)
      .then(data => setTemplates(data?.data ?? []))
      .catch(err => { if (is401(err)) onSessionExpired() })
      .finally(() => setTemplatesLoading(false))
  }

  useEffect(() => { refreshTemplates() }, [token])

  function handleUseTemplate() {
    setActiveNav('ai-workspace')
  }

  async function handleCreateSchedule() {
    if (!scheduleInput.trim()) { setScheduleError('Enter a task description with a frequency.'); return }
    setScheduleError(null)
    setScheduleSuccess(null)
    setScheduleCreating(true)
    try {
      await createScheduledWorkflow(scheduleInput, token, selectedDatasetId)
      setScheduleInput('')
      setScheduleSuccess('Schedule saved.')
      refreshScheduled()
    } catch (err) {
      if (is401(err)) { onSessionExpired(); return }
      setScheduleError(err.message.replace(/^\d+:\s*/, ''))
    } finally {
      setScheduleCreating(false)
    }
  }

  async function handleDeleteSchedule(id) {
    try {
      await deleteScheduledWorkflow(id, token)
      refreshScheduled()
    } catch (err) {
      if (is401(err)) onSessionExpired()
    }
  }

  async function handlePauseSchedule(id) {
    setSchedulePauseLoading(s => new Set(s).add(id))
    try {
      await pauseScheduledWorkflow(id, token)
      refreshScheduled()
    } catch (err) {
      if (is401(err)) onSessionExpired()
    } finally {
      setSchedulePauseLoading(s => { const n = new Set(s); n.delete(id); return n })
    }
  }

  async function handleResumeSchedule(id) {
    setSchedulePauseLoading(s => new Set(s).add(id))
    try {
      await resumeScheduledWorkflow(id, token)
      refreshScheduled()
    } catch (err) {
      if (is401(err)) onSessionExpired()
    } finally {
      setSchedulePauseLoading(s => { const n = new Set(s); n.delete(id); return n })
    }
  }

  async function handleRunNow(id) {
    setSchedRunNowLoading(s => new Set(s).add(id))
    try {
      await runScheduleNow(id, token)
      refreshScheduled()
      setSchedRunHistories(prev => { const n = { ...prev }; delete n[id]; return n })
      refreshNotifications()
    } catch (err) {
      if (is401(err)) onSessionExpired()
    } finally {
      setSchedRunNowLoading(s => { const n = new Set(s); n.delete(id); return n })
    }
  }

  async function handleExpandSchedule(id) {
    if (schedExpandedId === id) { setSchedExpandedId(null); return }
    setSchedExpandedId(id)
    if (!schedRunHistories[id]) {
      setSchedRunHistLoading(s => new Set(s).add(id))
      try {
        const d = await getScheduleRunHistory(id, token)
        setSchedRunHistories(prev => ({ ...prev, [id]: d?.data ?? [] }))
      } catch (err) {
        if (is401(err)) onSessionExpired()
        else setSchedRunHistories(prev => ({ ...prev, [id]: [] }))
      } finally {
        setSchedRunHistLoading(s => { const n = new Set(s); n.delete(id); return n })
      }
    }
  }

  function refreshSchedRuns() {
    setSchedRunsLoading(true)
    setSchedRunsError(null)
    getScheduleRuns(token)
      .then(d => setSchedRuns(d?.data ?? []))
      .catch(err => { if (is401(err)) onSessionExpired(); else setSchedRunsError('Could not load run history.') })
      .finally(() => setSchedRunsLoading(false))
  }

  useEffect(() => { if (activeNav === 'sched-activity') refreshSchedRuns() }, [activeNav, token]) // eslint-disable-line react-hooks/exhaustive-deps

  function refreshRecommendations() {
    setRecLoading(true)
    getRecommendations(token)
      .then(data => setRecList(data?.data ?? []))
      .catch(err => { if (is401(err)) onSessionExpired() })
      .finally(() => setRecLoading(false))
  }

  useEffect(() => { refreshRecommendations() }, [token])

  function refreshInsights() {
    setInsightsLoading(true)
    getInsights(token)
      .then(data => setInsights(data?.data ?? []))
      .catch(err => { if (is401(err)) onSessionExpired() })
      .finally(() => setInsightsLoading(false))
  }

  useEffect(() => { refreshInsights() }, [token])

  async function handleRetry(id) {
    setExecActionLoading(s => new Set(s).add(id))
    setHistoryMsg(null)
    try {
      await retryExecution(id, token)
      setHistoryMsg('Retry dispatched successfully. New execution added to history.')
      setHistoryMsgType('success')
      refreshHistory()
    } catch (err) {
      if (is401(err)) { onSessionExpired(); return }
      setHistoryMsg(err.message.replace(/^\d+:\s*/, ''))
      setHistoryMsgType('error')
    } finally {
      setExecActionLoading(s => { const n = new Set(s); n.delete(id); return n })
    }
  }

  async function handleRerun(id) {
    setExecActionLoading(s => new Set(s).add(id))
    setHistoryMsg(null)
    try {
      await rerunExecution(id, token)
      setHistoryMsg('Re-run dispatched successfully. New execution added to history.')
      setHistoryMsgType('success')
      refreshHistory()
    } catch (err) {
      if (is401(err)) { onSessionExpired(); return }
      setHistoryMsg(err.message.replace(/^\d+:\s*/, ''))
      setHistoryMsgType('error')
    } finally {
      setExecActionLoading(s => { const n = new Set(s); n.delete(id); return n })
    }
  }

  async function handleExplain(contextType, contextId) {
    const key = `${contextType}:${contextId}`
    if (explainData[key]) {
      setExplainData(prev => { const n = {...prev}; delete n[key]; return n })
      return
    }
    setExplainLoading(s => new Set(s).add(key))
    try {
      const res = await explainContext({ context_type: contextType, context_id: contextId }, token)
      setExplainData(prev => ({...prev, [key]: res?.data}))
    } catch (err) {
      if (is401(err)) onSessionExpired()
    } finally {
      setExplainLoading(s => { const n = new Set(s); n.delete(key); return n })
    }
  }

  async function handleRecSave(rec) {
    try {
      await saveWorkflow(rec.intent.slice(0, 60).trim(), rec.intent, token)
      refreshWorkflows()
    } catch (err) {
      if (is401(err)) onSessionExpired()
    }
  }

  function handleRecSchedule(rec) {
    setScheduleInput(rec.intent)
    setActiveNav('scheduled')
  }

  function refreshDatasets() {
    setDatasetListLoading(true)
    getDatasets(token)
      .then(data => {
        const list = data?.data ?? []
        setDatasetList(list)
      })
      .catch(err => { if (is401(err)) onSessionExpired() })
      .finally(() => setDatasetListLoading(false))
  }

  useEffect(() => { refreshDatasets() }, [token])

  function refreshReports() {
    setReportListLoading(true)
    setReportListError(null)
    getReports(token)
      .then(d => { setReportList(d?.data ?? []); setReportListError(null) })
      .catch(err => {
        if (is401(err)) {
          onSessionExpired()
        } else {
          console.warn('[refreshReports] failed:', err)
          setReportListError('Could not load saved reports. Please refresh or try again.')
        }
      })
      .finally(() => setReportListLoading(false))
  }

  useEffect(() => { if (activeNav === 'reports' || activeNav === 'operations' || activeNav === 'overview') refreshReports() }, [activeNav, token]) // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => { if (activeNav !== 'reports') setReportViewMode('list') }, [activeNav]) // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => { if (activeNav === 'workspaces') refreshWorkspaces() }, [activeNav, token]) // eslint-disable-line react-hooks/exhaustive-deps

  function refreshNotifications() {
    setNotifLoading(true)
    setNotifError(null)
    getNotifications(token)
      .then(d => setNotifications(d?.data ?? []))
      .catch(err => { if (is401(err)) onSessionExpired(); else setNotifError('Could not load notifications.') })
      .finally(() => setNotifLoading(false))
  }

  useEffect(() => { refreshNotifications() }, [token]) // eslint-disable-line react-hooks/exhaustive-deps

  async function handleMarkNotifRead(id) {
    try {
      await markNotificationRead(id, token)
      setNotifications(prev => prev.map(n => n.id === id ? { ...n, read: 1 } : n))
    } catch (err) {
      if (is401(err)) onSessionExpired()
    }
  }

  async function handleDeleteNotif(id) {
    try {
      await deleteNotification(id, token)
      setNotifications(prev => prev.filter(n => n.id !== id))
    } catch (err) {
      if (is401(err)) onSessionExpired()
    }
  }

  async function handleSelectReport(id) {
    setSelectedReportId(id)
    setSelectedReportData(null)
    setSelectedReportLoading(true)
    setReportEmailInput(''); setReportEmailStatus(null); setReportEmailSending(false)
    setReportViewMode('detail')
    try {
      const d = await getReportById(id, token)
      setSelectedReportData(d?.data ?? null)
    } catch (err) {
      if (is401(err)) onSessionExpired()
    } finally {
      setSelectedReportLoading(false)
    }
  }

  async function handleEmailReport(reportId) {
    const to = reportEmailInput.trim()
    if (!to) { setReportEmailStatus({ ok: false, msg: 'Please enter a recipient email address.' }); return }
    setReportEmailSending(true)
    setReportEmailStatus(null)
    try {
      const d = await emailReport(reportId, to, token)
      setReportEmailStatus({ ok: true, msg: d.data.message })
    } catch (err) {
      if (is401(err)) { onSessionExpired(); return }
      setReportEmailStatus({ ok: false, msg: err.message.replace(/^\d+:\s*/, '') })
    } finally {
      setReportEmailSending(false)
    }
  }

  function handleOpenSavedReport(reportId) {
    setActiveNav('reports')
    setReportViewMode('detail')
    setSelectedReportId(reportId)
    setSelectedReportData(null)
    setSelectedReportLoading(true)
    getReportById(reportId, token)
      .then(d => setSelectedReportData(d?.data ?? null))
      .catch(err => { if (is401(err)) onSessionExpired() })
      .finally(() => setSelectedReportLoading(false))
    refreshReports()
  }

  async function handleDeleteReport(id) {
    try {
      await deleteReport(id, token)
      setReportList(prev => prev.filter(r => r.id !== id))
      if (selectedReportId === id) { setSelectedReportId(null); setSelectedReportData(null) }
    } catch (err) {
      if (is401(err)) onSessionExpired()
    }
  }

  function handleReopenWorkspace(ws) {
    setActiveWorkspaceId(ws.id)
    if (ws.dataset_id != null) { setSelectedDatasetId(ws.dataset_id); setDatasetExplicit(false) }
    setActiveNav('ai-workspace')
  }

  async function handleDatasetUpload() {
    if (!datasetFile) { setDatasetError('Please select a CSV file.'); return }
    setDatasetError(null)
    setDatasetSummary(null)
    setDatasetLoading(true)
    try {
      const data = await uploadDataset(datasetFile, token)
      setDatasetSummary(data.data)
      setSelectedDatasetId(data.data.dataset_id)
      setDatasetExplicit(false)
      refreshDatasets()
    } catch (err) {
      if (is401(err)) { onSessionExpired(); return }
      setDatasetError(err.message.replace(/^\d+:\s*/, ''))
    } finally {
      setDatasetLoading(false)
    }
  }

  async function handleComposerAttach(file) {
    if (!file) return
    setDatasetError(null)
    setDatasetLoading(true)
    try {
      const data = await uploadDataset(file, token)
      setDatasetSummary(data.data)
      setSelectedDatasetId(data.data.dataset_id)
      setDatasetExplicit(false)
      refreshDatasets()
    } catch (err) {
      if (is401(err)) { onSessionExpired(); return }
      setDatasetError(err.message.replace(/^\d+:\s*/, ''))
    } finally {
      setDatasetLoading(false)
    }
  }

  function handleDeleteDataset(id, name) {
    setDsModal({ id, name })
  }

  async function handleConfirmDeleteDataset() {
    const { id } = dsModal
    setDsModal(null)
    try {
      await deleteDataset(id, token)
      const wasActive = selectedDatasetId === id
      const remaining = datasetList.filter(d => d.id !== id)
      setDatasetList(remaining)
      if (wasActive) {
        const nextId = remaining.length > 0 ? remaining[0].id : null
        setSelectedDatasetId(nextId)
        setDatasetExplicit(false)
        setReport(null)
        if (!nextId) setDatasetSummary(null)
      }
      setDsToast({ msg: 'Dataset deleted.', ok: true })
    } catch (err) {
      if (is401(err)) { onSessionExpired(); return }
      setDsToast({ msg: 'Failed to delete dataset.', ok: false })
    }
  }

  async function handleRenameDataset(id) {
    const name = dsRenameVal.trim()
    if (!name) return
    setDsRenameSaving(true)
    try {
      await renameDataset(id, name, token)
      setDatasetList(prev => prev.map(d => d.id === id ? { ...d, filename: name } : d))
      setDatasetSummary(prev => prev?.dataset_id === id ? { ...prev, filename: name } : prev)
      setDsRenaming(null)
      setDsRenameVal('')
      setDsToast({ msg: 'Dataset renamed.', ok: true })
    } catch (err) {
      if (is401(err)) { onSessionExpired(); return }
      setDsToast({ msg: 'Failed to rename dataset.', ok: false })
    } finally {
      setDsRenameSaving(false)
    }
  }

  return (
    <div className="ts-dashboard" style={{ minHeight: '100vh', background: C.bg, fontFamily: FONT, color: C.text, display: 'flex' }}>
      <style>{`
        .ts-dashboard, .ts-dashboard * {
          transition: background-color 0.18s ease, border-color 0.18s ease, color 0.12s ease !important;
        }
        .ts-dashboard *[style*="transition"] { transition: inherit !important; }
        .ts-composer-input::placeholder { font-size: 0.72rem; opacity: 0.6; }
      `}</style>

      {dsModal && (
        <ConfirmModal
          C={C} S={S}
          title="Delete Dataset"
          body={`Are you sure you want to delete "${dsModal.name}"? This cannot be undone.`}
          confirmLabel="Delete"
          onConfirm={handleConfirmDeleteDataset}
          onCancel={() => setDsModal(null)}
        />
      )}
      {dsToast && (
        <div style={{
          position: 'fixed', bottom: '28px', right: '28px', zIndex: 201,
          background: C.surface,
          border: `1px solid ${dsToast.ok ? C.success + '50' : C.danger + '40'}`,
          borderRadius: '10px', padding: '12px 20px',
          display: 'flex', alignItems: 'center', gap: '10px',
          fontSize: '0.84rem', color: dsToast.ok ? C.success : C.danger,
          boxShadow: '0 8px 32px rgba(0,0,0,0.4)', fontFamily: FONT,
        }}>
          <span style={{ fontWeight: '700' }}>{dsToast.ok ? '✓' : '✕'}</span>
          {dsToast.msg}
        </div>
      )}

      <aside style={{
        position: 'fixed', top: 0, left: 0,
        width: `${SIDEBAR_W}px`, height: '100vh',
        background: C.sidebar, borderRight: `1px solid rgba(37,48,96,0.45)`,
        display: 'flex', flexDirection: 'column', zIndex: 100,
      }}>
        <div style={{
          height: `${HEADER_H}px`, display: 'flex', alignItems: 'center',
          gap: '10px', padding: '0 18px', borderBottom: `1px solid rgba(30,43,82,0.45)`, flexShrink: 0,
        }}>
          <img
            src="/toolsmith-logo-transparent.png"
            alt="ToolSmithAI"
            style={{ width: '38px', height: '38px', objectFit: 'contain', flexShrink: 0 }}
          />
          <span style={{ fontWeight: '700', fontSize: '0.92rem', letterSpacing: '-0.2px' }}>ToolSmithAI</span>
        </div>

        <nav style={{ flex: 1, padding: '12px 10px', display: 'flex', flexDirection: 'column', gap: '2px', overflowY: 'hidden' }}>
          {NAV_ITEMS.map(({ id, label, icon }) => {
            const active = activeNav === id
            return (
              <button key={id} onClick={() => setActiveNav(id)} style={{
                display: 'flex', alignItems: 'center', gap: '10px',
                width: '100%', textAlign: 'left',
                padding: '9px 12px', borderRadius: '8px', border: 'none',
                background: active ? C.accentSoft : 'transparent',
                color: active ? C.accent : C.textSec,
                fontSize: '0.75rem', fontWeight: active ? '600' : '400',
                cursor: 'pointer', fontFamily: FONT, letterSpacing: '0.01em',
                transition: 'background 0.12s, color 0.12s',
              }}
              onMouseEnter={e => { if (!active) { e.currentTarget.style.background = C.borderAlt; e.currentTarget.style.color = C.text } }}
              onMouseLeave={e => { if (!active) { e.currentTarget.style.background = 'transparent'; e.currentTarget.style.color = C.textSec } }}>
                <span style={{ flexShrink: 0, display: 'flex' }}>{icon}</span>
                {label}
              </button>
            )
          })}
        </nav>

        <div style={{ padding: '12px 10px', borderTop: `1px solid rgba(30,43,82,0.45)`, flexShrink: 0 }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: '10px', padding: '8px 8px', borderRadius: '8px', cursor: 'pointer' }}
            onMouseEnter={e => e.currentTarget.style.background = C.borderAlt}
            onMouseLeave={e => e.currentTarget.style.background = 'transparent'}>
            <div style={{ width: '30px', height: '30px', borderRadius: '50%', background: 'linear-gradient(135deg, #6366f1, #8b5cf6)', display: 'flex', alignItems: 'center', justifyContent: 'center', fontSize: '0.78rem', fontWeight: '700', color: '#fff', flexShrink: 0 }}>
              {(user?.name || user?.email || 'U')[0].toUpperCase()}
            </div>
            <div style={{ flex: 1, minWidth: 0 }}>
              <div style={{ fontSize: '0.78rem', fontWeight: '600', color: C.text, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                {user?.name || 'User'}
              </div>
              <div style={{ fontSize: '0.66rem', color: C.textMuted, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                {user?.email || 'Local build · v0.6'}
              </div>
            </div>
          </div>
        </div>
      </aside>

      <div style={{ marginLeft: `${SIDEBAR_W}px`, flex: 1, display: 'flex', flexDirection: 'column', minHeight: '100vh' }}>

        <header style={{
          position: 'sticky', top: 0, height: `${HEADER_H}px`,
          background: C.bg, borderBottom: 'none',
          display: 'flex', alignItems: 'center', justifyContent: 'space-between',
          padding: '0 28px', zIndex: 50, flexShrink: 0,
        }}>
          {/* Back to Reports — shown in header when viewing a report detail */}
          {activeNav === 'reports' && reportViewMode === 'detail' ? (
            <button
              onClick={() => { setReportViewMode('list'); setSelectedReportId(null); setSelectedReportData(null) }}
              style={{ display: 'inline-flex', alignItems: 'center', gap: '6px', background: 'none', border: 'none', color: C.textSec, cursor: 'pointer', fontFamily: FONT, fontSize: '0.76rem', fontWeight: '500', padding: '0', letterSpacing: '0.01em' }}
              onMouseEnter={e => { e.currentTarget.style.color = C.accent }}
              onMouseLeave={e => { e.currentTarget.style.color = C.textSec }}>
              <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round"><line x1="19" y1="12" x2="5" y2="12"/><polyline points="12 19 5 12 12 5"/></svg>
              Back to Reports
            </button>
          ) : <div />}
          {/* Right icon cluster */}
          <div style={{ display: 'flex', alignItems: 'center', gap: '2px' }}>
            {/* Bell */}
            <div style={{ position: 'relative' }}>
              {notifOpen && (
                <div style={{ position: 'fixed', inset: 0, zIndex: 199 }} onClick={() => setNotifOpen(false)} />
              )}
              <div
                onClick={() => setNotifOpen(o => !o)}
                style={{ position: 'relative', width: '36px', height: '36px', display: 'flex', alignItems: 'center', justifyContent: 'center', borderRadius: '8px', cursor: 'pointer', color: notifOpen ? C.accent : C.textSec, background: notifOpen ? C.accentSoft : 'transparent' }}
                onMouseEnter={e => { if (!notifOpen) e.currentTarget.style.background = C.borderAlt }}
                onMouseLeave={e => { if (!notifOpen) e.currentTarget.style.background = 'transparent' }}
              >
                <svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.9" strokeLinecap="round" strokeLinejoin="round">
                  <path d="M18 8A6 6 0 0 0 6 8c0 7-3 9-3 9h18s-3-2-3-9"/>
                  <path d="M13.73 21a2 2 0 0 1-3.46 0"/>
                </svg>
                {notifications.filter(n => !n.read).length > 0 && (
                  <div style={{ position: 'absolute', top: '7px', right: '7px', width: '7px', height: '7px', borderRadius: '50%', background: C.danger, border: `1.5px solid ${C.bg}`, pointerEvents: 'none' }} />
                )}
              </div>
              {notifOpen && (
                <div style={{ position: 'absolute', top: '44px', right: 0, width: '320px', maxHeight: '400px', overflowY: 'auto', background: C.surface, border: `1px solid ${C.border}`, borderRadius: '10px', zIndex: 200, boxShadow: '0 8px 32px rgba(0,0,0,0.28)' }}>
                  <div style={{ padding: '12px 14px 8px', borderBottom: `1px solid ${C.border}`, display: 'flex', alignItems: 'center', justifyContent: 'space-between', position: 'sticky', top: 0, background: C.surface }}>
                    <span style={{ fontSize: '0.8rem', fontWeight: '600', color: C.text }}>Notifications</span>
                    {notifications.filter(n => !n.read).length > 0 && (
                      <span style={{ fontSize: '0.68rem', color: C.textMuted, background: C.borderAlt, borderRadius: '10px', padding: '1px 7px' }}>
                        {notifications.filter(n => !n.read).length} unread
                      </span>
                    )}
                  </div>
                  {notifLoading ? (
                    <div style={{ padding: '24px', textAlign: 'center', fontSize: '0.8rem', color: C.textMuted }}>Loading…</div>
                  ) : notifError ? (
                    <div style={{ padding: '24px', textAlign: 'center', fontSize: '0.8rem', color: C.danger }}>{notifError}</div>
                  ) : notifications.length === 0 ? (
                    <div style={{ padding: '32px 20px', textAlign: 'center', fontSize: '0.82rem', color: C.textMuted }}>No notifications yet.</div>
                  ) : (
                    notifications.map(n => (
                      <div key={n.id} style={{ padding: '10px 14px', borderBottom: `1px solid ${C.border}`, background: n.read ? 'transparent' : C.accentSoft }}>
                        <div style={{ display: 'flex', alignItems: 'flex-start', justifyContent: 'space-between', gap: '8px', marginBottom: '3px' }}>
                          <span style={{ fontSize: '0.82rem', fontWeight: n.read ? '400' : '600', color: C.text, flex: 1, lineHeight: '1.35' }}>{n.title}</span>
                          <div style={{ display: 'flex', gap: '2px', flexShrink: 0, marginTop: '1px' }}>
                            {!n.read && (
                              <button onClick={() => handleMarkNotifRead(n.id)} title="Mark as read" style={{ background: 'none', border: 'none', cursor: 'pointer', color: C.textMuted, fontSize: '0.72rem', padding: '1px 3px', fontFamily: FONT, lineHeight: 1 }}>✓</button>
                            )}
                            <button onClick={() => handleDeleteNotif(n.id)} title="Dismiss" style={{ background: 'none', border: 'none', cursor: 'pointer', color: C.textMuted, fontSize: '0.9rem', padding: '0 3px', lineHeight: 1 }}>×</button>
                          </div>
                        </div>
                        <div style={{ fontSize: '0.76rem', color: C.textSec, lineHeight: '1.4', marginBottom: '3px' }}>{n.message}</div>
                        <div style={{ fontSize: '0.68rem', color: C.textMuted }}>{_relTime(n.created_at)}</div>
                      </div>
                    ))
                  )}
                </div>
              )}
            </div>
            {/* Help */}
            <div style={{ width: '36px', height: '36px', borderRadius: '8px', display: 'flex', alignItems: 'center', justifyContent: 'center', cursor: 'pointer', color: C.textSec }}
              onMouseEnter={e => e.currentTarget.style.background = C.borderAlt}
              onMouseLeave={e => e.currentTarget.style.background = 'transparent'}>
              <svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.9" strokeLinecap="round" strokeLinejoin="round">
                <circle cx="12" cy="12" r="10"/>
                <path d="M9.09 9a3 3 0 0 1 5.83 1c0 2-3 3-3 3"/>
                <circle cx="12" cy="17" r="0.5" fill="currentColor"/>
              </svg>
            </div>
            {/* Theme toggle */}
            <div
              title={resolvedTheme === 'dark' ? 'Switch to light mode' : 'Switch to dark mode'}
              onClick={() => setTheme(resolvedTheme === 'dark' ? 'light' : 'dark')}
              style={{ width: '36px', height: '36px', borderRadius: '8px', display: 'flex', alignItems: 'center', justifyContent: 'center', cursor: 'pointer', color: C.textSec }}
              onMouseEnter={e => e.currentTarget.style.background = C.borderAlt}
              onMouseLeave={e => e.currentTarget.style.background = 'transparent'}
            >
              {resolvedTheme === 'dark' ? (
                <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.9" strokeLinecap="round" strokeLinejoin="round">
                  <circle cx="12" cy="12" r="4"/>
                  <path d="M12 2v2M12 20v2M4.93 4.93l1.41 1.41M17.66 17.66l1.41 1.41M2 12h2M20 12h2M4.93 19.07l1.41-1.41M17.66 6.34l1.41-1.41"/>
                </svg>
              ) : (
                <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.9" strokeLinecap="round" strokeLinejoin="round">
                  <path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z"/>
                </svg>
              )}
            </div>
            {/* Profile dropdown */}
            <div style={{ position: 'relative', marginLeft: '4px' }}>
              {profileMenuOpen && (
                <div
                  style={{ position: 'fixed', inset: 0, zIndex: 199 }}
                  onClick={() => setProfileMenuOpen(false)}
                />
              )}
              <button
                onClick={() => setProfileMenuOpen(o => !o)}
                style={{
                  display: 'flex', alignItems: 'center', gap: '5px',
                  padding: '3px 6px 3px 3px', borderRadius: '20px',
                  border: `1px solid ${C.border}`,
                  background: profileMenuOpen ? C.borderAlt : 'transparent',
                  cursor: 'pointer', transition: 'background 0.12s',
                }}
                onMouseEnter={e => e.currentTarget.style.background = C.borderAlt}
                onMouseLeave={e => { if (!profileMenuOpen) e.currentTarget.style.background = 'transparent' }}
              >
                <div style={{ width: '26px', height: '26px', borderRadius: '50%', background: 'linear-gradient(135deg, #6366f1, #8b5cf6)', display: 'flex', alignItems: 'center', justifyContent: 'center', fontSize: '0.72rem', fontWeight: '700', color: '#fff', flexShrink: 0 }}>
                  {(user?.name || user?.email || 'U')[0].toUpperCase()}
                </div>
                <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round"
                  style={{ color: C.textMuted, flexShrink: 0, transform: profileMenuOpen ? 'rotate(180deg)' : 'rotate(0deg)', transition: 'transform 0.15s ease' }}>
                  <path d="m6 9 6 6 6-6"/>
                </svg>
              </button>

              {profileMenuOpen && (
                <div style={{
                  position: 'absolute', top: 'calc(100% + 8px)', right: 0, zIndex: 200,
                  width: '200px',
                  background: C.surface,
                  border: `1px solid ${C.border}`,
                  borderRadius: '12px',
                  boxShadow: resolvedTheme === 'dark'
                    ? '0 8px 32px rgba(0,0,0,0.5), 0 2px 8px rgba(0,0,0,0.3)'
                    : '0 8px 32px rgba(0,0,0,0.12), 0 2px 8px rgba(0,0,0,0.06)',
                  overflow: 'hidden',
                  animation: 'tsDropdown 0.14s ease',
                }}>
                  <style>{`@keyframes tsDropdown { from { opacity:0; transform:translateY(-6px) scale(0.97); } to { opacity:1; transform:translateY(0) scale(1); } }`}</style>
                  {/* User info header */}
                  <div style={{ padding: '12px 14px', borderBottom: `1px solid ${C.border}` }}>
                    <div style={{ fontSize: '0.78rem', fontWeight: '600', color: C.text, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                      {user?.name || 'User'}
                    </div>
                    <div style={{ fontSize: '0.67rem', color: C.textMuted, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', marginTop: '2px' }}>
                      {user?.email || ''}
                    </div>
                  </div>
                  {/* Menu items */}
                  <div style={{ padding: '6px' }}>
                    <button
                      onClick={() => { setActiveNav('settings'); setProfileMenuOpen(false) }}
                      style={{ display: 'flex', alignItems: 'center', gap: '10px', width: '100%', textAlign: 'left', background: 'transparent', border: 'none', borderRadius: '7px', padding: '8px 10px', fontSize: '0.78rem', color: C.textSec, cursor: 'pointer', fontFamily: FONT, transition: 'background 0.1s, color 0.1s' }}
                      onMouseEnter={e => { e.currentTarget.style.background = C.borderAlt; e.currentTarget.style.color = C.text }}
                      onMouseLeave={e => { e.currentTarget.style.background = 'transparent'; e.currentTarget.style.color = C.textSec }}
                    >
                      <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2"/><circle cx="12" cy="7" r="4"/></svg>
                      Profile
                    </button>
                    <div style={{ height: '1px', background: C.border, margin: '4px 0' }} />
                    <button
                      onClick={() => { setProfileMenuOpen(false); onLogout() }}
                      style={{ display: 'flex', alignItems: 'center', gap: '10px', width: '100%', textAlign: 'left', background: 'transparent', border: 'none', borderRadius: '7px', padding: '8px 10px', fontSize: '0.78rem', color: C.danger, cursor: 'pointer', fontFamily: FONT, transition: 'background 0.1s' }}
                      onMouseEnter={e => e.currentTarget.style.background = C.dangerSoft}
                      onMouseLeave={e => e.currentTarget.style.background = 'transparent'}
                    >
                      <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4"/><polyline points="16 17 21 12 16 7"/><line x1="21" y1="12" x2="9" y2="12"/></svg>
                      Log out
                    </button>
                  </div>
                </div>
              )}
            </div>
          </div>
        </header>

        <main style={{ flex: 1, padding: activeNav === 'reports' && reportViewMode === 'detail' ? '0' : '0px 12px 36px 35px', boxSizing: 'border-box' }}>
          <div style={{ width: '100%' }}>

            {/* ── AI Workspace ─────────────────────────────────────── */}
            {activeNav === 'ai-workspace' && (
              <ErrorBoundary C={C}>
                <Suspense fallback={<LazyFallback />}>
                  <AIWorkspace
                    C={C}
                    S={S}
                    token={token}
                    onSessionExpired={onSessionExpired}
                    user={user}
                    datasetList={datasetList}
                    selectedDatasetId={selectedDatasetId}
                    setSelectedDatasetId={setSelectedDatasetId}
                    datasetExplicit={datasetExplicit}
                    setDatasetExplicit={setDatasetExplicit}
                    setActiveNav={setActiveNav}
                    onOpenReport={handleOpenSavedReport}
                    onExportReport={(id, fmt) => exportReport(id, token, fmt).catch(() => {})}
                    onUploadDataset={handleComposerAttach}
                    onExecutionComplete={() => {
                      refreshNotifications()
                      refreshScheduled()
                      refreshHistory()
                      refreshReports()
                    }}
                    contextStats={{
                      workflowCount: workflowList.length,
                      nextScheduledAt: scheduledList
                        .filter(s => s.enabled && s.next_run_at)
                        .sort((a, b) => new Date(a.next_run_at) - new Date(b.next_run_at))[0]?.next_run_at ?? null,
                      recentExecution: history[0] ?? null,
                      alertCount: notifications.filter(n => !n.read).length,
                      suggestedAction: recList[0]?.intent ?? null,
                    }}
                  />
                </Suspense>
              </ErrorBoundary>
            )}

            {/* ── Engine Lab ───────────────────────────────────────── */}
            {activeNav === 'engine' && (
              <ErrorBoundary C={C}>
                <Suspense fallback={<LazyFallback />}>
                  <EngineWorkspace C={C} token={token} />
                </Suspense>
              </ErrorBoundary>
            )}

            {/* ── Overview ─────────────────────────────────────────── */}
            {activeNav === 'overview' && (() => {
              const activeWfCount    = workflowList.length
              const schedActiveCount = scheduledList.filter(s => s.enabled).length
              const unreadAlerts     = notifications.filter(n => !n.read)
              const failedExecs      = history.filter(h => h.status === 'failed' || h.status === 'error')

              const nextScheduled = scheduledList
                .filter(s => s.enabled && s.next_run_at)
                .sort((a, b) => new Date(a.next_run_at) - new Date(b.next_run_at))[0]

              function fmtNextRun(ts) {
                if (!ts) return 'None'
                const diff = new Date(ts) - Date.now()
                if (diff < 0) return 'Overdue'
                const h = Math.floor(diff / 3600000)
                if (h < 1) return '< 1h'
                if (h < 24) return `${h}h`
                return `${Math.floor(h / 24)}d`
              }

              const KPI_CARDS = [
                { label: 'Active Workflows',    value: activeWfCount,           sub: 'saved',       color: '#a78bfa', action: () => setActiveNav('workflows') },
                { label: 'Scheduled Runs',      value: schedActiveCount,        sub: 'enabled',     color: '#38bdf8', action: () => setActiveNav('scheduled') },
                { label: 'Alerts',              value: unreadAlerts.length,     sub: unreadAlerts.length > 0 ? 'unread' : 'all clear', color: unreadAlerts.length > 0 ? '#f59e0b' : '#10b981', action: null },
                { label: 'Recent Reports',      value: reportList.length,       sub: 'saved',       color: '#10b981', action: () => setActiveNav('reports') },
                { label: 'Failed Executions',   value: failedExecs.length,      sub: 'in history',  color: failedExecs.length > 0 ? '#f87171' : '#10b981', action: () => setActiveNav('history') },
                { label: 'AI Usage',            value: usage?.data?.total_events ?? '—', sub: 'total events', color: '#60a5fa', action: () => setActiveNav('usage') },
              ]

              const QUICK_ACTIONS = [
                { label: 'Upload Dataset',  color: '#10b981', action: () => setActiveNav('datasets'),    icon: <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="17 8 12 3 7 8"/><line x1="12" y1="3" x2="12" y2="15"/></svg> },
                { label: 'Create Workflow', color: '#8b5cf6', action: () => setActiveNav('ai-workspace'), icon: <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><circle cx="18" cy="5" r="3"/><circle cx="6" cy="12" r="3"/><circle cx="18" cy="19" r="3"/><line x1="8.59" y1="13.51" x2="15.42" y2="17.49"/><line x1="15.41" y1="6.51" x2="8.59" y2="10.49"/></svg> },
                { label: 'Generate Report', color: '#6366f1', action: () => setActiveNav('ai-workspace'), icon: <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/></svg> },
                { label: 'Monitor KPI',    color: '#f59e0b', action: () => setActiveNav('operations'),   icon: <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><polyline points="22 12 18 12 15 21 9 3 6 12 2 12"/></svg> },
                { label: 'Schedule Run',   color: '#38bdf8', action: () => setActiveNav('scheduled'),    icon: <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><rect x="3" y="4" width="18" height="18" rx="2"/><line x1="16" y1="2" x2="16" y2="6"/><line x1="8" y1="2" x2="8" y2="6"/><line x1="3" y1="10" x2="21" y2="10"/></svg> },
              ]

              return <>
                {/* ── KPI Strip ── */}
                <div style={{ display: 'grid', gridTemplateColumns: 'repeat(6, 1fr)', gap: '8px', marginBottom: '14px' }}>
                  {KPI_CARDS.map(card => (
                    <div key={card.label}
                      onClick={card.action || undefined}
                      style={{ background: C.surface, border: `1px solid ${C.border}`, borderRadius: '12px', padding: '14px 14px 12px', cursor: card.action ? 'pointer' : 'default', transition: card.action ? 'border-color 0.14s, transform 0.14s' : 'none' }}
                      onMouseEnter={e => { if (card.action) { e.currentTarget.style.borderColor = card.color; e.currentTarget.style.transform = 'translateY(-1px)' } }}
                      onMouseLeave={e => { if (card.action) { e.currentTarget.style.borderColor = C.border; e.currentTarget.style.transform = 'translateY(0)' } }}>
                      <div style={{ fontSize: '0.57rem', color: C.textMuted, fontWeight: '700', textTransform: 'uppercase', letterSpacing: '0.09em', marginBottom: '6px' }}>{card.label}</div>
                      <div style={{ fontSize: '1.5rem', fontWeight: '800', color: card.color, letterSpacing: '-1px', lineHeight: 1, marginBottom: '3px' }}>{card.value}</div>
                      <div style={{ fontSize: '0.62rem', color: C.textMuted }}>{card.sub}</div>
                    </div>
                  ))}
                </div>

                {/* ── Row 1: Recent Executions + Scheduled Automations ── */}
                <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '10px', marginBottom: '10px' }}>
                  {/* Recent Executions */}
                  <div style={S.card}>
                    <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: '14px' }}>
                      <h3 style={{ margin: 0, fontSize: '0.75rem', fontWeight: '600', color: C.textSec }}>Recent Executions</h3>
                      <button onClick={() => setActiveNav('history')} style={{ background: 'none', border: 'none', color: C.accent, fontSize: '0.72rem', cursor: 'pointer', fontFamily: FONT }}>View all</button>
                    </div>
                    {historyLoading ? (
                      <div style={{ padding: '16px 0', textAlign: 'center', color: C.textMuted, fontSize: '0.8rem' }}>Loading…</div>
                    ) : history.length === 0 ? (
                      <div style={{ padding: '16px 0', textAlign: 'center', color: C.textMuted, fontSize: '0.8rem', lineHeight: 1.6 }}>No executions yet.<br />Run a workflow in AI Workspace.</div>
                    ) : (
                      <div style={{ display: 'flex', flexDirection: 'column', gap: '6px' }}>
                        {history.slice(0, 5).map(row => {
                          const isOk  = row.status === 'success' || row.status === 'completed'
                          const isFail = row.status === 'failed'
                          const sc = isOk ? '#10b981' : isFail ? '#f87171' : '#f59e0b'
                          const slabel = isOk ? 'Completed' : isFail ? 'Failed' : 'Running'
                          return (
                            <div key={row.id} style={{ display: 'flex', alignItems: 'center', gap: '10px', padding: '9px 11px', background: C.bg, border: `1px solid ${C.border}`, borderRadius: '9px' }}>
                              <span style={{ width: '7px', height: '7px', borderRadius: '50%', background: sc, flexShrink: 0 }} />
                              <div style={{ flex: 1, minWidth: 0 }}>
                                <div style={{ fontSize: '0.74rem', color: C.textSec, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{row.summary || row.intent || '—'}</div>
                                <div style={{ fontSize: '0.63rem', color: C.textMuted, marginTop: '1px' }}>{row.started_at ? new Date(row.started_at).toLocaleString(undefined, { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' }) : '—'}</div>
                              </div>
                              <span style={{ fontSize: '0.62rem', fontWeight: '600', color: sc, background: sc + '18', borderRadius: '4px', padding: '2px 7px', flexShrink: 0 }}>{slabel}</span>
                            </div>
                          )
                        })}
                      </div>
                    )}
                  </div>

                  {/* Scheduled Automations */}
                  <div style={S.card}>
                    <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: '14px' }}>
                      <h3 style={{ margin: 0, fontSize: '0.75rem', fontWeight: '600', color: C.textSec }}>Scheduled Automations</h3>
                      <button onClick={() => setActiveNav('scheduled')} style={{ background: 'none', border: 'none', color: C.accent, fontSize: '0.72rem', cursor: 'pointer', fontFamily: FONT }}>View all</button>
                    </div>
                    {scheduledLoading ? (
                      <div style={{ padding: '16px 0', textAlign: 'center', color: C.textMuted, fontSize: '0.8rem' }}>Loading…</div>
                    ) : scheduledList.length === 0 ? (
                      <div style={{ padding: '16px 0', textAlign: 'center', color: C.textMuted, fontSize: '0.8rem', lineHeight: 1.6 }}>No scheduled automations.<br /><button onClick={() => setActiveNav('scheduled')} style={{ background: 'none', border: 'none', color: C.accent, cursor: 'pointer', fontFamily: FONT, fontSize: '0.8rem', padding: 0 }}>Set one up</button></div>
                    ) : (
                      <div style={{ display: 'flex', flexDirection: 'column', gap: '6px' }}>
                        {scheduledList.slice(0, 5).map(sw => {
                          const enabled = sw.enabled
                          const sc = enabled ? '#10b981' : '#9ca3af'
                          return (
                            <div key={sw.id} style={{ display: 'flex', alignItems: 'center', gap: '10px', padding: '9px 11px', background: C.bg, border: `1px solid ${C.border}`, borderRadius: '9px' }}>
                              <span style={{ width: '7px', height: '7px', borderRadius: '50%', background: sc, flexShrink: 0 }} />
                              <div style={{ flex: 1, minWidth: 0 }}>
                                <div style={{ fontSize: '0.74rem', color: C.textSec, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{sw.name || sw.intent || '—'}</div>
                                <div style={{ fontSize: '0.63rem', color: C.textMuted, marginTop: '1px' }}>{sw.next_run_at ? `Next: ${new Date(sw.next_run_at).toLocaleString(undefined, { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' })}` : 'No next run set'}</div>
                              </div>
                              <span style={{ fontSize: '0.62rem', fontWeight: '600', color: sc, background: sc + '18', borderRadius: '4px', padding: '2px 7px', flexShrink: 0 }}>{enabled ? 'Active' : 'Paused'}</span>
                            </div>
                          )
                        })}
                      </div>
                    )}
                  </div>
                </div>

                {/* ── Row 2: Recent Reports + Recommended Actions ── */}
                <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '10px', marginBottom: '12px' }}>
                  {/* Recent Reports */}
                  <div style={S.card}>
                    <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: '14px' }}>
                      <h3 style={{ margin: 0, fontSize: '0.75rem', fontWeight: '600', color: C.textSec }}>Recent Reports</h3>
                      <button onClick={() => setActiveNav('reports')} style={{ background: 'none', border: 'none', color: C.accent, fontSize: '0.72rem', cursor: 'pointer', fontFamily: FONT }}>View all</button>
                    </div>
                    {reportListLoading ? (
                      <div style={{ padding: '16px 0', textAlign: 'center', color: C.textMuted, fontSize: '0.8rem' }}>Loading…</div>
                    ) : reportList.length === 0 ? (
                      <div style={{ padding: '16px 0', textAlign: 'center', color: C.textMuted, fontSize: '0.8rem', lineHeight: 1.6 }}>No reports yet.<br />Generate a report in AI Workspace.</div>
                    ) : (
                      <div style={{ display: 'flex', flexDirection: 'column', gap: '6px' }}>
                        {reportList.slice(0, 4).map(r => {
                          const isEmail = r.task_type === 'email_dataset_report'
                          const tc = isEmail ? '#06b6d4' : C.accent
                          return (
                            <div key={r.id}
                              onClick={() => { setSelectedReportId(r.id); setActiveNav('reports') }}
                              style={{ display: 'flex', alignItems: 'center', gap: '10px', padding: '9px 11px', background: C.bg, border: `1px solid ${C.border}`, borderRadius: '9px', cursor: 'pointer', transition: 'background 0.1s' }}
                              onMouseEnter={e => e.currentTarget.style.background = C.borderAlt}
                              onMouseLeave={e => e.currentTarget.style.background = C.bg}>
                              <div style={{ width: '28px', height: '28px', borderRadius: '7px', background: tc + '18', border: `1px solid ${tc}30`, display: 'flex', alignItems: 'center', justifyContent: 'center', flexShrink: 0 }}>
                                <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke={tc} strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/></svg>
                              </div>
                              <div style={{ flex: 1, minWidth: 0 }}>
                                <div style={{ fontSize: '0.74rem', color: C.textSec, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{r.title || r.dataset_filename || 'Report'}</div>
                                <div style={{ fontSize: '0.63rem', color: C.textMuted, marginTop: '1px' }}>{r.created_at ? new Date(r.created_at).toLocaleString(undefined, { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' }) : '—'}</div>
                              </div>
                              <span style={{ fontSize: '0.6rem', color: tc, background: tc + '18', borderRadius: '4px', padding: '2px 7px', flexShrink: 0, fontWeight: '600' }}>{isEmail ? 'Email' : 'Dataset'}</span>
                            </div>
                          )
                        })}
                      </div>
                    )}
                  </div>

                  {/* Recommended Actions */}
                  <div style={S.card}>
                    <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: '14px' }}>
                      <h3 style={{ margin: 0, fontSize: '0.75rem', fontWeight: '600', color: C.textSec }}>Recommended Actions</h3>
                      <button onClick={refreshRecommendations} style={{ background: 'none', border: 'none', color: C.textMuted, fontSize: '0.68rem', cursor: 'pointer', fontFamily: FONT, display: 'flex', alignItems: 'center', gap: '4px' }}>
                        <svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round"><polyline points="23 4 23 10 17 10"/><path d="M20.49 15a9 9 0 1 1-2.12-9.36L23 10"/></svg>Refresh
                      </button>
                    </div>
                    {recLoading ? (
                      <div style={{ padding: '16px 0', textAlign: 'center', color: C.textMuted, fontSize: '0.8rem' }}>Loading…</div>
                    ) : recList.length === 0 ? (
                      <div style={{ padding: '16px 0', textAlign: 'center', color: C.textMuted, fontSize: '0.8rem', lineHeight: 1.6 }}>No recommendations yet.<br />Run the same task twice to see suggestions.</div>
                    ) : (
                      <div style={{ display: 'flex', flexDirection: 'column', gap: '6px' }}>
                        {recList.slice(0, 4).map((rec, i) => {
                          const pal = [C.accent, C.warn, C.success, '#a78bfa'][i % 4]
                          return (
                            <div key={i} style={{ display: 'flex', alignItems: 'flex-start', gap: '10px', padding: '9px 11px', background: C.bg, border: `1px solid ${C.border}`, borderLeft: `3px solid ${pal}`, borderRadius: '9px' }}>
                              <div style={{ flex: 1, minWidth: 0 }}>
                                <div style={{ fontSize: '0.74rem', color: C.textSec, fontWeight: '500', marginBottom: '2px', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{rec.intent}</div>
                                <div style={{ fontSize: '0.63rem', color: C.textMuted, lineHeight: 1.4 }}>{rec.suggestion}</div>
                              </div>
                              <button onClick={() => setActiveNav('ai-workspace')}
                                style={{ flexShrink: 0, background: pal + '18', border: `1px solid ${pal}30`, borderRadius: '6px', padding: '4px 9px', fontSize: '0.65rem', color: pal, cursor: 'pointer', fontFamily: FONT, fontWeight: '600', whiteSpace: 'nowrap' }}>
                                Run
                              </button>
                            </div>
                          )
                        })}
                      </div>
                    )}
                  </div>
                </div>

                {/* ── Quick Actions ── */}
                <div style={{ display: 'flex', gap: '8px', flexWrap: 'wrap' }}>
                  {QUICK_ACTIONS.map(qa => (
                    <button key={qa.label} onClick={qa.action}
                      style={{ display: 'flex', alignItems: 'center', gap: '7px', background: C.surface, border: `1px solid ${C.border}`, borderRadius: '10px', padding: '9px 16px', fontSize: '0.76rem', color: C.textSec, cursor: 'pointer', fontFamily: FONT, fontWeight: '500', transition: 'border-color 0.14s, color 0.14s' }}
                      onMouseEnter={e => { e.currentTarget.style.borderColor = qa.color; e.currentTarget.style.color = qa.color }}
                      onMouseLeave={e => { e.currentTarget.style.borderColor = C.border; e.currentTarget.style.color = C.textSec }}>
                      <span style={{ color: qa.color }}>{qa.icon}</span>
                      {qa.label}
                    </button>
                  ))}
                </div>
              </>
            })()}

            {/* ── Saved Workflows ──────────────────────────────────── */}
            {activeNav === 'workflows' && <>
              <div style={{ marginBottom: '18px' }}>
                <h2 style={{ margin: '0 0 4px', fontSize: '1.4rem', fontWeight: '700', color: C.text, letterSpacing: '-0.4px' }}>Workflows</h2>
                <p style={{ margin: 0, color: C.textMuted, fontSize: '0.78rem' }}>Save named workflows and re-run them from the dashboard at any time.</p>
              </div>

              {/* ── Composer mode tabs — admin only ──────────────────── */}
              {user?.role === 'admin' && (
                <div style={{ display: 'flex', gap: '6px', marginBottom: '18px' }}>
                  {[{ key: 'workflow', label: 'Workflow Composer' }, { key: 'tool', label: 'Dynamic Tool Composer' }].map(({ key, label }) => {
                    const active = composerMode === key
                    return (
                      <button key={key} onClick={() => setComposerMode(key)} style={{ padding: '6px 16px', borderRadius: '20px', fontSize: '0.75rem', cursor: 'pointer', fontFamily: FONT, fontWeight: active ? '600' : '400', border: `1px solid ${active ? C.accent : C.border}`, background: active ? C.accentSoft : 'transparent', color: active ? C.accent : C.textSec, transition: 'border-color 0.12s, background 0.12s, color 0.12s' }}>
                        {label}
                      </button>
                    )
                  })}
                </div>
              )}

              {/* ── Workflow Composer ─────────────────────────────────── */}
              {composerMode === 'workflow' && <>

              {/* Workflow Templates */}
              <div style={{ ...S.card, marginBottom: '18px' }}>
                <h3 style={{ margin: '0 0 14px', fontSize: '0.75rem', fontWeight: '500', color: C.textSec, letterSpacing: '0.01em' }}>Workflow Templates</h3>
                {templatesLoading ? (
                  <div style={{ padding: '20px 0', textAlign: 'center', color: C.textMuted, fontSize: '0.82rem' }}>Loading...</div>
                ) : templates.length === 0 ? (
                  <div style={{ padding: '20px 0', textAlign: 'center', color: C.textMuted, fontSize: '0.82rem' }}>No templates available.</div>
                ) : (
                  <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(230px, 1fr))', gap: '12px' }}>
                    {templates.map(t => (
                      <div key={t.id} style={{ background: C.bg, border: `1px solid ${C.border}`, borderRadius: '10px', padding: '14px 16px', display: 'flex', flexDirection: 'column', gap: '8px' }}>
                        <div style={{ display: 'flex', alignItems: 'center', gap: '6px', flexWrap: 'wrap' }}>
                          <div style={S.badge(C.accent, C.accentSoft)}>{t.category}</div>
                          {t.frequency && <span style={{ fontSize: '0.67rem', color: C.textMuted, fontWeight: '500', letterSpacing: '0.03em' }}>{t.frequency}</span>}
                        </div>
                        <div style={{ fontSize: '0.75rem', fontWeight: '500', color: C.textSec, lineHeight: 1.3 }}>{t.name}</div>
                        <div style={{ fontSize: '0.7rem', color: C.textMuted, lineHeight: 1.55, flex: 1 }}>{t.description}</div>
                        <button
                          onClick={() => handleUseTemplate(t)}
                          style={{ ...S.btnPrimary, padding: '5px 12px', fontSize: '0.75rem', alignSelf: 'flex-start', marginTop: '2px' }}
                        >
                          Use Template →
                        </button>
                      </div>
                    ))}
                  </div>
                )}
              </div>

              {/* Multi-step workflow builder */}
              <div style={{ ...S.card, marginBottom: '18px' }}>
                <div style={{ display: 'flex', alignItems: 'center', gap: '10px', marginBottom: '16px' }}>
                  <h3 style={{ margin: 0, fontSize: '0.75rem', fontWeight: '500', color: C.textSec, letterSpacing: '0.01em' }}>Multi-Step Workflow Builder</h3>
                  <div style={S.badge(C.accent, C.accentSoft)}>New</div>
                </div>
                <p style={{ margin: '0 0 14px', color: C.textMuted, fontSize: '0.75rem', lineHeight: 1.6 }}>
                  Build a workflow with ordered steps. Each step runs sequentially — if one fails, execution stops.
                </p>
                <label style={S.label}>Workflow name</label>
                <input
                  type="text"
                  placeholder="e.g. Full dataset pipeline"
                  value={builderName}
                  onChange={e => { setBuilderName(e.target.value); setBuilderError(null); setBuilderSuccess(null) }}
                  style={{ ...S.input, marginBottom: '14px' }}
                />
                <label style={S.label}>Steps (max 10)</label>
                <WorkflowStepBuilder steps={builderSteps} onStepsChange={setBuilderSteps} C={C} S={S} />
                <button
                  onClick={handleSaveMultiStepWorkflow}
                  disabled={builderSaving || !builderName.trim() || builderSteps.length === 0}
                  style={{ ...S.btnPrimary, marginTop: '14px', opacity: (builderSaving || !builderName.trim() || builderSteps.length === 0) ? 0.6 : 1 }}
                >
                  {builderSaving ? 'Saving...' : 'Save Multi-Step Workflow'}
                </button>
                {builderError && (
                  <div style={{ marginTop: '12px', background: C.dangerSoft, border: `1px solid ${C.danger}40`, borderRadius: '8px', padding: '10px 14px', fontSize: '0.82rem', color: C.danger }}>
                    {builderError}
                  </div>
                )}
                {builderSuccess && (
                  <div style={{ marginTop: '12px', background: C.successSoft, border: `1px solid ${C.success}40`, borderRadius: '8px', padding: '10px 14px', fontSize: '0.82rem', color: C.success }}>
                    {builderSuccess}
                  </div>
                )}
              </div>

              {/* Save form */}
              <div style={{ ...S.card, marginBottom: '18px' }}>
                <div style={{ display: 'flex', alignItems: 'center', gap: '10px', marginBottom: '16px' }}>
                  <h3 style={{ margin: 0, fontSize: '0.75rem', fontWeight: '500', color: C.textSec, letterSpacing: '0.01em' }}>Save Single-Intent Workflow</h3>
                </div>
                <p style={{ margin: '0 0 14px', color: C.textMuted, fontSize: '0.75rem', lineHeight: 1.6 }}>
                  Save a single natural language intent as a reusable workflow. Run it again any time from the dashboard.
                </p>
                <label style={S.label}>Workflow name</label>
                <input
                  type="text"
                  placeholder="e.g. Daily logistics report"
                  value={wfSaveName}
                  onChange={e => { setWfSaveName(e.target.value); setWfSaveError(null); setWfSaveSuccess(null) }}
                  style={{ ...S.input, marginBottom: '14px' }}
                />
                <label style={S.label}>Task description</label>
                <textarea
                  rows={3}
                  placeholder="e.g. generate report from uploaded dataset"
                  value={wfSaveIntent}
                  onChange={e => { setWfSaveIntent(e.target.value); setWfSaveError(null); setWfSaveSuccess(null) }}
                  style={{ ...S.textarea, fontFamily: FONT, marginBottom: '14px' }}
                />
                <button
                  onClick={handleSaveWorkflow}
                  disabled={wfSaving || !wfSaveName.trim() || !wfSaveIntent.trim()}
                  style={{ ...S.btnPrimary, opacity: (wfSaving || !wfSaveName.trim() || !wfSaveIntent.trim()) ? 0.6 : 1 }}
                >
                  {wfSaving ? 'Saving...' : 'Save Workflow'}
                </button>
                {wfSaveError && (
                  <div style={{ marginTop: '12px', background: C.dangerSoft, border: `1px solid ${C.danger}40`, borderRadius: '8px', padding: '10px 14px', fontSize: '0.82rem', color: C.danger }}>
                    {wfSaveError}
                  </div>
                )}
                {wfSaveSuccess && (
                  <div style={{ marginTop: '12px', background: C.successSoft, border: `1px solid ${C.success}40`, borderRadius: '8px', padding: '10px 14px', fontSize: '0.82rem', color: C.success }}>
                    {wfSaveSuccess}
                  </div>
                )}
              </div>

              </>}

              {/* ── Dynamic Tool Composer ─────────────────────────────── */}
              {composerMode === 'tool' && user?.role === 'admin' && (
                <Suspense fallback={<LazyFallback />}>
                  <DynamicToolComposer C={C} S={S} token={token} onSessionExpired={onSessionExpired} />
                </Suspense>
              )}

              {/* Run result */}
              {(wfRunResult || wfRunError) && (
                <div style={{ ...S.card, marginBottom: '18px' }}>
                  <h3 style={{ margin: '0 0 12px', fontSize: '0.75rem', fontWeight: '500', color: C.textSec, letterSpacing: '0.01em' }}>Run Result</h3>
                  {wfRunError
                    ? <p style={{ margin: 0, fontSize: '0.82rem', color: C.danger }}>{wfRunError}</p>
                    : <ErrorBoundary C={C}><Suspense fallback={<LazyFallback />}><WorkflowResult result={wfRunResult} C={C} S={S} onOpenReport={handleOpenSavedReport} onExportReport={(id, fmt) => exportReport(id, token, fmt).catch(() => {})} SectionRenderer={ReportSection} /></Suspense></ErrorBoundary>}
                </div>
              )}

              {/* Workflow list */}
              <div style={S.card}>
                <h3 style={{ margin: '0 0 14px', fontSize: '0.75rem', fontWeight: '500', color: C.textSec, letterSpacing: '0.01em' }}>Saved Workflows</h3>
                {workflowListLoading ? (
                  <div style={{ fontSize: '0.82rem', color: C.textSec, textAlign: 'center', padding: '16px 0' }}>Loading...</div>
                ) : workflowList.length === 0 ? (
                  <div style={{ fontSize: '0.82rem', color: C.textMuted, textAlign: 'center', padding: '16px 0' }}>
                    No saved workflows yet. Use the form above to create one.
                  </div>
                ) : (
                  <div>
                    <div style={{ display: 'grid', gridTemplateColumns: `${wfNameColW}px 1fr 64px`, borderBottom: `1px solid ${C.border}`, paddingBottom: '8px', marginBottom: '4px' }}>
                      {[{ resizable: true, label: 'Name' }, { resizable: false, label: 'Intent' }, { resizable: false, label: 'Actions' }].map(({ resizable, label }) => (
                        <div key={label} style={{ padding: '0 8px', fontSize: '0.67rem', color: C.textSec, fontWeight: '600', textTransform: 'uppercase', letterSpacing: '0.07em', position: 'relative', userSelect: 'none' }}>
                          {label}
                          {resizable && (
                            <div
                              onMouseDown={startWfColResize}
                              style={{ position: 'absolute', right: 0, top: '50%', transform: 'translateY(-50%)', width: '8px', height: '16px', cursor: 'col-resize', display: 'flex', alignItems: 'center', justifyContent: 'center' }}
                            >
                              <div style={{ width: '2px', height: '12px', background: C.borderAlt, borderRadius: '1px' }} />
                            </div>
                          )}
                        </div>
                      ))}
                    </div>
                    {workflowList.map((wf, idx) => {
                      const isMultiStep = Array.isArray(wf.definition?.workflow_steps)
                      const intent = isMultiStep
                        ? `${wf.definition.workflow_steps.length} steps: ${wf.definition.workflow_steps.map(s => s.label).join(' → ')}`
                        : (wf.definition?.intent || '—')
                      const isRunning = wfRunningId === wf.id
                      return (
                        <div key={wf.id} style={{ display: 'grid', gridTemplateColumns: `${wfNameColW}px 1fr 64px`, padding: '9px 0', borderBottom: idx < workflowList.length - 1 ? `1px solid ${C.border}` : 'none', alignItems: 'center' }}>
                          <div style={{ padding: '0 8px', display: 'flex', alignItems: 'center', gap: '6px', overflow: 'hidden' }}>
                            <span style={{ fontSize: '0.75rem', fontWeight: '400', color: C.textSec, lineHeight: 1.4, wordBreak: 'break-word' }}>
                              {wf.name}
                            </span>
                            {isMultiStep && <div style={S.badge(C.accent, C.accentSoft)}>Multi</div>}
                          </div>
                          <div style={{ padding: '0 8px', fontSize: '0.78rem', color: C.textSec, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }} title={intent}>
                            {intent}
                          </div>
                          <div style={{ padding: '0 8px', display: 'flex', gap: '6px' }}>
                            <button
                              onClick={() => handleRunWorkflow(wf)}
                              disabled={isRunning}
                              title="Run"
                              style={{ width: '28px', height: '28px', display: 'flex', alignItems: 'center', justifyContent: 'center', background: C.successSoft, border: `1px solid ${C.success}30`, borderRadius: '7px', cursor: isRunning ? 'default' : 'pointer', color: C.success, opacity: isRunning ? 0.5 : 1, flexShrink: 0 }}
                            >
                              {isRunning
                                ? <div style={{ width: '10px', height: '10px', borderRadius: '50%', border: `2px solid ${C.success}40`, borderTopColor: C.success, animation: 'spin 0.75s linear infinite' }} />
                                : <svg width="10" height="10" viewBox="0 0 24 24" fill="currentColor"><polygon points="5 3 19 12 5 21 5 3"/></svg>}
                            </button>
                            <button
                              onClick={() => handleDeleteWorkflow(wf.id)}
                              title="Delete"
                              style={{ width: '28px', height: '28px', display: 'flex', alignItems: 'center', justifyContent: 'center', background: C.dangerSoft, border: `1px solid ${C.danger}30`, borderRadius: '7px', cursor: 'pointer', color: C.danger, flexShrink: 0 }}
                            >
                              <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><polyline points="3 6 5 6 21 6"/><path d="M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6"/><path d="M10 11v6"/><path d="M14 11v6"/><path d="M9 6V4a1 1 0 0 1 1-1h4a1 1 0 0 1 1 1v2"/></svg>
                            </button>
                          </div>
                        </div>
                      )
                    })}
                  </div>
                )}
              </div>
            </>}

            {/* ── Datasets ─────────────────────────────────────────── */}
            {activeNav === 'datasets' && (() => {
              const dsTypeStyle = (type) => DS_TYPE_STYLE[type] || { color: C.textSec, bg: C.borderAlt }

              const filteredDs = datasetList
                .filter(ds => {
                  const q = dsSearch.toLowerCase()
                  return (!q || ds.filename.toLowerCase().includes(q)) &&
                    (dsTypeFilter === 'all' || getFileType(ds.filename) === dsTypeFilter)
                })
                .sort((a, b) => {
                  if (dsSortBy === 'newest') return new Date(b.uploaded_at) - new Date(a.uploaded_at)
                  if (dsSortBy === 'oldest') return new Date(a.uploaded_at) - new Date(b.uploaded_at)
                  if (dsSortBy === 'name') return a.filename.localeCompare(b.filename)
                  if (dsSortBy === 'rows') return b.row_count - a.row_count
                  return 0
                })

              const totalRows = datasetList.reduce((s, d) => s + (d.row_count || 0), 0)
              const totalCols = datasetList.reduce((s, d) => s + (d.column_count || 0), 0)
              const recentUploads = [...datasetList]
                .sort((a, b) => new Date(b.uploaded_at) - new Date(a.uploaded_at))
                .slice(0, 5)

              return <>
                {dsOpenMenu && <div style={{ position: 'fixed', inset: 0, zIndex: 40 }} onClick={() => setDsOpenMenu(null)} />}

                {/* Page header */}
                <div style={{ display: 'flex', alignItems: 'flex-start', justifyContent: 'space-between', marginBottom: '12px' }}>
                  <div>
                    <h2 style={{ margin: '0 0 4px', fontSize: '1.4rem', fontWeight: '700', color: C.text, letterSpacing: '-0.4px' }}>Datasets</h2>
                    <p style={{ margin: 0, color: C.textMuted, fontSize: '0.75rem' }}>Upload, inspect, and manage your data sources.</p>
                  </div>
                  <div style={{ display: 'flex', gap: '10px', alignItems: 'center', paddingTop: '4px', flexShrink: 0 }}>
                    <button
                      onClick={() => dsFileInputRef.current && dsFileInputRef.current.click()}
                      style={{ ...S.btnPrimary, background: 'linear-gradient(135deg, #6366f1 0%, #8b5cf6 100%)', boxShadow: '0 0 16px #6366f124', padding: '7px 16px', fontSize: '0.78rem' }}
                    >
                      + New Dataset
                    </button>
                  </div>
                </div>

                {/* Two-column layout */}
                <div style={{ display: 'grid', gridTemplateColumns: '1fr 272px', gap: '24px', alignItems: 'start' }}>

                  {/* Left column */}
                  <div style={{ display: 'flex', flexDirection: 'column', gap: '20px', minWidth: 0 }}>

                    {/* Upload dropzone */}
                    <div
                      onDragOver={e => { e.preventDefault(); setDsDragOver(true) }}
                      onDragLeave={e => { if (!e.currentTarget.contains(e.relatedTarget)) setDsDragOver(false) }}
                      onDrop={e => {
                        e.preventDefault(); setDsDragOver(false)
                        const file = e.dataTransfer.files[0]
                        if (file) { setDatasetFile(file); setDatasetSummary(null); setDatasetError(null); setReport(null) }
                      }}
                      style={{
                        background: dsDragOver ? '#6366f108' : C.surface,
                        border: `2px dashed ${dsDragOver ? '#818cf8' : C.accent}`,
                        borderRadius: '14px', padding: '28px',
                        transition: 'border-color 0.18s, background 0.18s',
                        boxShadow: dsDragOver ? `0 0 0 4px ${C.accent}14` : 'none',
                      }}
                    >
                      <input
                        ref={dsFileInputRef}
                        type="file"
                        accept=".csv,.xlsx,.xls"
                        style={{ display: 'none' }}
                        onChange={e => { setDatasetFile(e.target.files[0] || null); setDatasetSummary(null); setDatasetError(null); setReport(null) }}
                      />
                      <div style={{ display: 'flex', gap: '24px', alignItems: 'stretch' }}>
                        {/* Drop zone — cloud icon + upload button centered, matching screenshot */}
                        <div
                          onClick={() => dsFileInputRef.current && dsFileInputRef.current.click()}
                          style={{
                            flex: 1, display: 'flex', flexDirection: 'column', alignItems: 'center',
                            justifyContent: 'center', cursor: 'pointer', padding: '28px 20px',
                            borderRadius: '10px', border: `1px solid ${C.border}`, background: C.bg,
                            gap: '12px', minHeight: '170px',
                          }}
                        >
                          {/* Blue cloud-upload SVG */}
                          <svg width="58" height="50" viewBox="0 0 58 50" fill="none" xmlns="http://www.w3.org/2000/svg">
                            <path d="M43 23c-.3-8.8-7.5-15.8-16.3-15.8C19.8 7.2 14 12.2 12.3 18.8 7.4 19.7 4 24 4 29.2 4 35 8.7 40 14.8 40H43c5.2 0 9.5-4.2 9.5-9.5 0-4.8-3.6-8.9-8.5-9.5z" fill="#6366f1" fillOpacity="0.14" stroke="#6366f1" strokeWidth="1.9" strokeLinejoin="round"/>
                            <line x1="29" y1="44" x2="29" y2="25" stroke="#6366f1" strokeWidth="2.6" strokeLinecap="round"/>
                            <polyline points="21,33 29,24 37,33" fill="none" stroke="#6366f1" strokeWidth="2.6" strokeLinecap="round" strokeLinejoin="round"/>
                          </svg>
                          <div style={{ textAlign: 'center' }}>
                            <div style={{ fontSize: '0.78rem', fontWeight: '500', color: C.textSec, marginBottom: '2px' }}>Drag & drop your file here</div>
                          </div>
                          <div style={{ fontSize: '0.75rem', color: C.textMuted, lineHeight: 1 }}>or</div>
                          <button
                            onClick={e => { e.stopPropagation(); handleDatasetUpload() }}
                            disabled={datasetLoading || !datasetFile}
                            style={{ ...S.btnPrimary, background: 'linear-gradient(135deg, #6366f1 0%, #8b5cf6 100%)', boxShadow: (datasetLoading || !datasetFile) ? 'none' : '0 0 14px #6366f140', opacity: (datasetLoading || !datasetFile) ? 0.72 : 1, padding: '9px 28px', fontSize: '0.86rem' }}
                          >
                            {datasetLoading ? 'Uploading…' : 'Upload Dataset'}
                          </button>
                          {datasetFile ? (
                            <div style={{ background: C.accentSoft, border: `1px solid ${C.accent}40`, borderRadius: '6px', padding: '4px 12px', fontSize: '0.74rem', color: C.accent, fontFamily: MONO, maxWidth: '220px', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                              {datasetFile.name}
                            </div>
                          ) : (
                            <div style={{ fontSize: '0.69rem', color: C.textMuted }}>Supports files up to 100MB</div>
                          )}
                        </div>
                        {/* Supported formats list only */}
                        <div style={{ width: '176px', flexShrink: 0, display: 'flex', flexDirection: 'column', justifyContent: 'center' }}>
                          <div style={{ fontSize: '0.62rem', color: C.textSec, fontWeight: '700', textTransform: 'uppercase', letterSpacing: '0.08em', marginBottom: '12px' }}>Supported Formats</div>
                          <div style={{ display: 'flex', flexDirection: 'column', gap: '10px' }}>
                            {[
                              { label: 'CSV',   desc: 'Comma Separated Values',    color: '#10b981' },
                              { label: 'Excel', desc: 'Microsoft Excel Files',      color: '#34d399' },
                              { label: 'JSON',  desc: 'JavaScript Object Notation', color: '#8b5cf6' },
                              { label: 'SQL',   desc: 'SQL Database Dump',          color: '#f59e0b' },
                            ].map(f => (
                              <div key={f.label} style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
                                <div style={{ width: '30px', height: '20px', background: f.color + '22', border: `1px solid ${f.color}40`, borderRadius: '4px', display: 'flex', alignItems: 'center', justifyContent: 'center', fontSize: '0.57rem', fontWeight: '700', color: f.color, flexShrink: 0 }}>{f.label}</div>
                                <div style={{ fontSize: '0.71rem', color: C.textSec, lineHeight: 1.3 }}>{f.desc}</div>
                              </div>
                            ))}
                          </div>
                        </div>
                      </div>
                      {datasetError && (
                        <div style={{ marginTop: '14px', background: C.dangerSoft, border: `1px solid ${C.danger}40`, borderRadius: '8px', padding: '10px 14px', fontSize: '0.82rem', color: C.danger }}>
                          {datasetError}
                        </div>
                      )}
                    </div>

                    {/* Dataset table card */}
                    <div style={{ ...S.card, padding: 0, overflow: 'visible' }}>
                      {/* Toolbar */}
                      <div style={{ padding: '20px 24px 16px', borderBottom: `1px solid ${C.border}` }}>
                        <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: '14px' }}>
                          <h3 style={{ margin: 0, fontSize: '0.75rem', fontWeight: '500', color: C.textSec, letterSpacing: '0.01em', display: 'flex', alignItems: 'center', gap: '8px' }}>
                            My Datasets
                            {datasetList.length > 0 && (
                              <span style={{ background: C.accentSoft, color: C.accent, borderRadius: '20px', padding: '1px 8px', fontSize: '0.69rem', fontWeight: '700' }}>
                                {datasetList.length}
                              </span>
                            )}
                          </h3>
                          <button onClick={refreshDatasets} style={{ background: 'none', border: 'none', color: C.textMuted, fontSize: '0.68rem', fontWeight: '400', cursor: 'pointer', fontFamily: FONT, display: 'flex', alignItems: 'center', gap: '5px' }}>
                            <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round"><polyline points="23 4 23 10 17 10"/><path d="M20.49 15a9 9 0 1 1-2.12-9.36L23 10"/></svg>Refresh
                          </button>
                        </div>
                        <div style={{ display: 'flex', gap: '10px', flexWrap: 'wrap' }}>
                          <div style={{ flex: 1, minWidth: '150px', position: 'relative' }}>
                            <span style={{ position: 'absolute', left: '10px', top: '50%', transform: 'translateY(-50%)', color: C.textMuted, pointerEvents: 'none', display: 'flex' }}><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><circle cx="11" cy="11" r="8"/><path d="m21 21-4.35-4.35"/></svg></span>
                            <input type="text" placeholder="Search datasets…" value={dsSearch} onChange={e => setDsSearch(e.target.value)}
                              style={{ ...S.input, fontFamily: FONT, paddingLeft: '30px', fontSize: '0.82rem' }} />
                          </div>
                          <select value={dsTypeFilter} onChange={e => setDsTypeFilter(e.target.value)}
                            style={{ ...S.input, width: 'auto', cursor: 'pointer', fontFamily: FONT, fontSize: '0.82rem' }}>
                            <option value="all">All Types</option>
                            <option value="CSV">CSV</option>
                            <option value="Excel">Excel</option>
                            <option value="JSON">JSON</option>
                            <option value="SQL">SQL</option>
                          </select>
                          <select value={dsSortBy} onChange={e => setDsSortBy(e.target.value)}
                            style={{ ...S.input, width: 'auto', cursor: 'pointer', fontFamily: FONT, fontSize: '0.82rem' }}>
                            <option value="newest">Sort: Newest</option>
                            <option value="oldest">Sort: Oldest</option>
                            <option value="name">Sort: Name A–Z</option>
                            <option value="rows">Sort: Most Rows</option>
                          </select>
                        </div>
                      </div>

                      {/* Table body */}
                      {datasetListLoading ? (
                        <div style={{ padding: '40px', textAlign: 'center', color: C.textMuted, fontSize: '0.78rem' }}>Loading datasets…</div>
                      ) : filteredDs.length === 0 ? (
                        <div style={{ padding: '48px 24px', textAlign: 'center' }}>
                          <div style={{ marginBottom: '12px', opacity: 0.3, display: 'flex', justifyContent: 'center', color: C.textSec }}><svg width="36" height="36" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round"><path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"/></svg></div>
                          <div style={{ fontSize: '0.78rem', color: C.textSec, marginBottom: '4px' }}>
                            {datasetList.length === 0 ? 'No datasets uploaded yet.' : 'No datasets match your filter.'}
                          </div>
                          <div style={{ fontSize: '0.78rem', color: C.textMuted }}>
                            {datasetList.length === 0 ? 'Upload a CSV or Excel file above to get started.' : 'Try adjusting your search or filter.'}
                          </div>
                        </div>
                      ) : (
                        <>
                          <div style={{ display: 'grid', gridTemplateColumns: 'minmax(170px, 2.2fr) 72px 70px 52px minmax(120px, 1fr) 84px 96px', padding: '8px 24px', borderBottom: `1px solid ${C.border}` }}>
                            {['Dataset Name', 'Type', 'Rows', 'Cols', 'Uploaded', 'Status', 'Actions'].map(col => (
                              <div key={col} style={{ fontSize: '0.62rem', color: C.textSec, fontWeight: '700', textTransform: 'uppercase', letterSpacing: '0.08em' }}>{col}</div>
                            ))}
                          </div>
                          {filteredDs.map((ds, idx) => {
                            const active   = ds.id === selectedDatasetId
                            const renaming = dsRenaming === ds.id
                            const type     = getFileType(ds.filename)
                            const tStyle   = dsTypeStyle(type)
                            const menuOpen = dsOpenMenu === ds.id
                            return (
                              <div key={ds.id}
                                onClick={() => { if (!renaming) { setSelectedDatasetId(ds.id); setReport(null) } }}
                                style={{
                                  display: 'grid',
                                  gridTemplateColumns: 'minmax(170px, 2.2fr) 72px 70px 52px minmax(120px, 1fr) 84px 96px',
                                  padding: '11px 24px',
                                  borderBottom: idx < filteredDs.length - 1 ? `1px solid ${C.border}` : 'none',
                                  alignItems: 'center',
                                  background: active ? `${C.accent}09` : 'transparent',
                                  transition: 'background 0.15s',
                                  cursor: renaming ? 'default' : 'pointer',
                                }}>
                                {/* Name */}
                                <div style={{ overflow: 'hidden', paddingRight: '8px' }}>
                                  {renaming ? (
                                    <input autoFocus value={dsRenameVal} onChange={e => setDsRenameVal(e.target.value)} onClick={e => e.stopPropagation()}
                                      onKeyDown={e => { if (e.key === 'Enter') handleRenameDataset(ds.id); if (e.key === 'Escape') { setDsRenaming(null); setDsRenameVal('') } }}
                                      style={{ ...S.input, fontSize: '0.79rem', padding: '4px 9px' }} />
                                  ) : (
                                    <span style={{ fontSize: '0.75rem', color: C.textSec, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', display: 'block', fontFamily: MONO }} title={ds.filename}>{ds.filename}</span>
                                  )}
                                </div>
                                {/* Type badge */}
                                <div>
                                  <span style={{ display: 'inline-flex', background: tStyle.bg, color: tStyle.color, border: `1px solid ${tStyle.color}40`, borderRadius: '5px', padding: '2px 8px', fontSize: '0.64rem', fontWeight: '700', letterSpacing: '0.04em' }}>{type}</span>
                                </div>
                                {/* Rows */}
                                <div style={{ fontSize: '0.73rem', color: C.textMuted, fontFamily: MONO }}>{(ds.row_count || 0).toLocaleString()}</div>
                                {/* Cols */}
                                <div style={{ fontSize: '0.73rem', color: C.textMuted, fontFamily: MONO }}>{ds.column_count}</div>
                                {/* Uploaded */}
                                <div style={{ fontSize: '0.7rem', color: C.textMuted, whiteSpace: 'nowrap' }}>
                                  {new Date(ds.uploaded_at).toLocaleString(undefined, { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' })}
                                </div>
                                {/* Status */}
                                <div>
                                  {active ? (
                                    <span style={{ display: 'inline-flex', alignItems: 'center', gap: '5px', background: '#10b9811a', color: '#10b981', border: '1px solid #10b98140', borderRadius: '20px', padding: '3px 10px', fontSize: '0.66rem', fontWeight: '700' }}>
                                      <span style={{ width: '5px', height: '5px', borderRadius: '50%', background: '#10b981', boxShadow: '0 0 5px #10b981', display: 'inline-block', flexShrink: 0 }} />
                                      Active
                                    </span>
                                  ) : (
                                    <span style={{ display: 'inline-flex', alignItems: 'center', background: C.borderAlt, color: C.textMuted, borderRadius: '20px', padding: '3px 10px', fontSize: '0.66rem', fontWeight: '600' }}>
                                      Idle
                                    </span>
                                  )}
                                </div>
                                {/* Actions */}
                                <div style={{ display: 'flex', gap: '2px', alignItems: 'center', position: 'relative' }}>
                                  {renaming ? (
                                    <>
                                      <button onClick={() => handleRenameDataset(ds.id)} disabled={dsRenameSaving || !dsRenameVal.trim()}
                                        style={{ ...S.btnPrimary, padding: '3px 8px', fontSize: '0.71rem', opacity: (dsRenameSaving || !dsRenameVal.trim()) ? 0.5 : 1 }}>
                                        {dsRenameSaving ? '…' : 'Save'}
                                      </button>
                                      <button onClick={() => { setDsRenaming(null); setDsRenameVal('') }}
                                        style={{ background: 'transparent', border: `1px solid ${C.border}`, borderRadius: '5px', padding: '3px 7px', fontSize: '0.71rem', color: C.textSec, cursor: 'pointer', fontFamily: FONT }}>
                                        &#x2715;
                                      </button>
                                    </>
                                  ) : (
                                    <>
                                      <button onClick={() => { setSelectedDatasetId(ds.id); setReport(null) }} title="Preview / select"
                                        style={{ background: active ? C.accentSoft : 'transparent', border: 'none', padding: '5px 6px', borderRadius: '6px', cursor: 'pointer', color: active ? C.accent : C.textSec, display: 'flex', alignItems: 'center', justifyContent: 'center' }}>
                                        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"/><circle cx="12" cy="12" r="3"/></svg>
                                      </button>
                                      <button onClick={() => { setSelectedDatasetId(ds.id); setReport(null) }} title="Open analytics"
                                        style={{ background: 'transparent', border: 'none', padding: '5px 6px', borderRadius: '6px', cursor: 'pointer', color: C.textSec, display: 'flex', alignItems: 'center', justifyContent: 'center' }}>
                                        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><line x1="18" y1="20" x2="18" y2="10"/><line x1="12" y1="20" x2="12" y2="4"/><line x1="6" y1="20" x2="6" y2="14"/></svg>
                                      </button>
                                      <div style={{ position: 'relative' }}>
                                        <button onClick={e => { e.stopPropagation(); setDsOpenMenu(menuOpen ? null : ds.id) }}
                                          style={{ background: 'transparent', border: 'none', padding: '5px 7px', borderRadius: '6px', cursor: 'pointer', color: C.textSec, display: 'flex', alignItems: 'center', justifyContent: 'center' }}>
                                          <svg width="3" height="15" viewBox="0 0 3 15"><circle cx="1.5" cy="1.5" r="1.5" fill="currentColor"/><circle cx="1.5" cy="7.5" r="1.5" fill="currentColor"/><circle cx="1.5" cy="13.5" r="1.5" fill="currentColor"/></svg>
                                        </button>
                                        {menuOpen && (
                                          <div style={{ position: 'absolute', right: 0, top: '110%', background: '#13151f', border: `1px solid ${C.borderAlt}`, borderRadius: '10px', boxShadow: '0 8px 32px #000b', zIndex: 50, minWidth: '160px', overflow: 'hidden' }}>
                                            <button onClick={() => { setDsRenaming(ds.id); setDsRenameVal(ds.filename); setDsOpenMenu(null) }}
                                              style={{ display: 'flex', alignItems: 'center', gap: '9px', width: '100%', textAlign: 'left', background: 'transparent', border: 'none', padding: '9px 14px', fontSize: '0.75rem', color: C.textSec, cursor: 'pointer', fontFamily: FONT }}>
                                              <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M11 4H4a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7"/><path d="M18.5 2.5a2.121 2.121 0 0 1 3 3L12 15l-4 1 1-4 9.5-9.5z"/></svg>
                                              Rename
                                            </button>
                                            {!active && (
                                              <button onClick={() => { setSelectedDatasetId(ds.id); setReport(null); setDsOpenMenu(null) }}
                                                style={{ display: 'flex', alignItems: 'center', gap: '9px', width: '100%', textAlign: 'left', background: 'transparent', border: 'none', borderTop: `1px solid ${C.border}`, padding: '9px 14px', fontSize: '0.75rem', color: C.textSec, cursor: 'pointer', fontFamily: FONT }}>
                                                <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round"><polyline points="20 6 9 17 4 12"/></svg>
                                                Set Active
                                              </button>
                                            )}
                                            <button onClick={() => { handleDeleteDataset(ds.id, ds.filename); setDsOpenMenu(null) }}
                                              style={{ display: 'flex', alignItems: 'center', gap: '9px', width: '100%', textAlign: 'left', background: 'transparent', border: 'none', borderTop: `1px solid ${C.border}`, padding: '9px 14px', fontSize: '0.81rem', color: C.danger, cursor: 'pointer', fontFamily: FONT }}>
                                              <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><polyline points="3 6 5 6 21 6"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a1 1 0 0 1 1-1h4a1 1 0 0 1 1 1v2"/></svg>
                                              Delete
                                            </button>
                                          </div>
                                        )}
                                      </div>
                                    </>
                                  )}
                                </div>
                              </div>
                            )
                          })}
                        </>
                      )}
                    </div>

                    {/* Dataset Intelligence Center */}
                    {datasetSummary && (
                      <Suspense fallback={<LazyFallback />}>
                        <DatasetIntelligence
                          ds={datasetSummary}
                          C={C}
                          S={S}
                          onGenerateReport={() => setReport(buildReport(datasetSummary))}
                          hasReport={Boolean(report)}
                          report={report}
                        />
                      </Suspense>
                    )}

                    {/* LEGACY BLOCK — kept for reference, unreachable when datasetSummary is set */}
                    {false && datasetSummary && <>
                      <div style={{ ...S.card, border: `1px solid ${C.accent}22` }}>
                        <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: '20px' }}>
                          <h3 style={{ margin: 0, fontSize: '0.92rem', fontWeight: '600' }}>Dataset Summary</h3>
                          <span style={{ fontSize: '0.74rem', color: C.textMuted, fontFamily: MONO }}>{datasetSummary.filename}</span>
                        </div>
                        <div style={{ display: 'grid', gridTemplateColumns: 'repeat(4, 1fr)', gap: '12px', marginBottom: '20px' }}>
                          {[
                            { label: 'Rows',         value: datasetSummary.row_count.toLocaleString() },
                            { label: 'Columns',      value: datasetSummary.column_count },
                            { label: 'Numeric Cols', value: datasetSummary.numeric_columns.length },
                            { label: 'Cat. Cols',    value: datasetSummary.column_count - datasetSummary.numeric_columns.length },
                          ].map(({ label, value }) => (
                            <div key={label} style={{ background: C.bg, border: `1px solid ${C.border}`, borderRadius: '10px', padding: '14px 16px' }}>
                              <div style={{ fontSize: '0.62rem', color: C.textSec, fontWeight: '700', textTransform: 'uppercase', letterSpacing: '0.08em', marginBottom: '8px' }}>{label}</div>
                              <div style={{ fontSize: '1.3rem', fontWeight: '700', color: C.accent, fontFamily: MONO, letterSpacing: '-0.03em' }}>{value}</div>
                            </div>
                          ))}
                        </div>
                        <div style={{ marginBottom: '20px' }}>
                          <div style={{ fontSize: '0.62rem', color: C.textSec, fontWeight: '700', textTransform: 'uppercase', letterSpacing: '0.08em', marginBottom: '10px' }}>All Columns</div>
                          <div style={{ display: 'flex', flexWrap: 'wrap', gap: '6px' }}>
                            {datasetSummary.columns.map(col => {
                              const isNum = datasetSummary.numeric_columns.includes(col)
                              return (
                                <span key={col} style={{ background: isNum ? C.accentSoft : C.bg, border: `1px solid ${isNum ? C.accent + '50' : C.border}`, borderRadius: '6px', padding: '3px 10px', fontSize: '0.74rem', color: isNum ? C.accent : C.textSec, fontFamily: MONO }}>
                                  {col}
                                </span>
                              )
                            })}
                          </div>
                          {datasetSummary.numeric_columns.length > 0 && (
                            <div style={{ marginTop: '8px', fontSize: '0.68rem', color: C.textMuted }}>
                              <span style={{ display: 'inline-flex', alignItems: 'center', gap: '5px' }}><span style={{ display: 'inline-block', width: '8px', height: '8px', borderRadius: '2px', background: C.accent, flexShrink: 0 }}></span>Highlighted = numeric column</span>
                            </div>
                          )}
                        </div>
                        {(datasetSummary.sample_rows || []).length > 0 && (
                          <div>
                            <div style={{ fontSize: '0.62rem', color: C.textSec, fontWeight: '700', textTransform: 'uppercase', letterSpacing: '0.08em', marginBottom: '10px' }}>
                              Sample Rows (first {(datasetSummary.sample_rows || []).length})
                            </div>
                            <div style={{ overflowX: 'auto', borderRadius: '8px', border: `1px solid ${C.border}` }}>
                              <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: '0.77rem', fontFamily: MONO }}>
                                <thead>
                                  <tr style={{ background: C.bg }}>
                                    {datasetSummary.columns.map(col => (
                                      <th key={col} style={{ padding: '9px 14px', textAlign: 'left', color: C.textSec, fontWeight: '600', borderBottom: `1px solid ${C.border}`, whiteSpace: 'nowrap' }}>{col}</th>
                                    ))}
                                  </tr>
                                </thead>
                                <tbody>
                                  {(datasetSummary.sample_rows || []).map((row, i) => (
                                    <tr key={i} style={{ background: i % 2 === 1 ? '#ffffff03' : 'transparent' }}>
                                      {datasetSummary.columns.map(col => (
                                        <td key={col} style={{ padding: '8px 14px', color: row[col] === '' || row[col] == null ? C.textMuted : C.text, borderBottom: i < (datasetSummary.sample_rows || []).length - 1 ? `1px solid ${C.border}` : 'none', whiteSpace: 'nowrap' }}>
                                          {row[col] === '' || row[col] == null ? '—' : String(row[col])}
                                        </td>
                                      ))}
                                    </tr>
                                  ))}
                                </tbody>
                              </table>
                            </div>
                          </div>
                        )}
                      </div>

                      <div style={S.card}>
                        <h3 style={{ margin: '0 0 20px', fontSize: '0.92rem', fontWeight: '600' }}>Analysis</h3>
                        {Object.keys(datasetSummary.numeric_profile).length > 0 && (
                          <div style={{ marginBottom: '24px' }}>
                            <div style={{ fontSize: '0.62rem', color: C.textSec, fontWeight: '700', textTransform: 'uppercase', letterSpacing: '0.08em', marginBottom: '10px' }}>Numeric Profiles</div>
                            <div style={{ overflowX: 'auto', borderRadius: '8px', border: `1px solid ${C.border}` }}>
                              <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: '0.78rem', fontFamily: MONO }}>
                                <thead>
                                  <tr style={{ background: C.bg }}>
                                    {['Column', 'Min', 'Max', 'Mean', 'Sum'].map(h => (
                                      <th key={h} style={{ padding: '9px 14px', textAlign: h === 'Column' ? 'left' : 'right', color: C.textSec, fontWeight: '600', borderBottom: `1px solid ${C.border}`, whiteSpace: 'nowrap' }}>{h}</th>
                                    ))}
                                  </tr>
                                </thead>
                                <tbody>
                                  {Object.entries(datasetSummary.numeric_profile).map(([col, stats], i, arr) => (
                                    <tr key={col} style={{ background: i % 2 === 1 ? '#ffffff03' : 'transparent' }}>
                                      <td style={{ padding: '8px 14px', color: C.accent, fontWeight: '600', borderBottom: i < arr.length - 1 ? `1px solid ${C.border}` : 'none' }}>{col}</td>
                                      {['min', 'max', 'mean', 'sum'].map(k => (
                                        <td key={k} style={{ padding: '8px 14px', color: C.text, textAlign: 'right', borderBottom: i < arr.length - 1 ? `1px solid ${C.border}` : 'none', whiteSpace: 'nowrap' }}>
                                          {stats[k] == null ? <span style={{ color: C.textMuted }}>—</span> : stats[k].toLocaleString(undefined, { maximumFractionDigits: 4 })}
                                        </td>
                                      ))}
                                    </tr>
                                  ))}
                                </tbody>
                              </table>
                            </div>
                          </div>
                        )}
                        <div style={{ marginBottom: Object.keys(datasetSummary.categorical_profile).length > 0 ? '24px' : 0 }}>
                          <div style={{ fontSize: '0.62rem', color: C.textSec, fontWeight: '700', textTransform: 'uppercase', letterSpacing: '0.08em', marginBottom: '10px' }}>Missing Values</div>
                          {Object.values(datasetSummary.missing_values).every(v => v === 0) ? (
                            <div style={{ display: 'inline-flex', alignItems: 'center', gap: '7px', background: '#10b9811a', border: '1px solid #10b98140', borderRadius: '8px', padding: '8px 14px', fontSize: '0.82rem', color: '#10b981' }}>
                              <span>&#x2713;</span> No missing values detected.
                            </div>
                          ) : (
                            <div style={{ display: 'flex', flexWrap: 'wrap', gap: '8px' }}>
                              {Object.entries(datasetSummary.missing_values).filter(([, count]) => count > 0).map(([col, count]) => (
                                <div key={col} style={{ background: C.warnSoft, border: `1px solid ${C.warn}40`, borderRadius: '6px', padding: '4px 12px', fontSize: '0.75rem', fontFamily: MONO }}>
                                  <span style={{ color: C.textSec }}>{col}</span>
                                  <span style={{ color: C.warn, fontWeight: '700', marginLeft: '6px' }}>{count}</span>
                                </div>
                              ))}
                            </div>
                          )}
                        </div>
                        {Object.keys(datasetSummary.categorical_profile).length > 0 && (
                          <div>
                            <div style={{ fontSize: '0.62rem', color: C.textSec, fontWeight: '700', textTransform: 'uppercase', letterSpacing: '0.08em', marginBottom: '12px' }}>Top Values by Category</div>
                            <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(210px, 1fr))', gap: '12px' }}>
                              {Object.entries(datasetSummary.categorical_profile).map(([col, entries]) => (
                                <div key={col} style={{ background: C.bg, border: `1px solid ${C.border}`, borderRadius: '10px', overflow: 'hidden' }}>
                                  <div style={{ padding: '9px 13px', borderBottom: `1px solid ${C.border}`, fontSize: '0.77rem', color: C.accent, fontWeight: '600', fontFamily: MONO }}>{col}</div>
                                  {entries.map(({ value, count }, i) => (
                                    <div key={i} style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', padding: '6px 13px', borderBottom: i < entries.length - 1 ? `1px solid ${C.border}` : 'none' }}>
                                      <span style={{ fontSize: '0.75rem', color: C.text, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', maxWidth: '130px' }}>{value}</span>
                                      <span style={{ fontSize: '0.71rem', color: C.textSec, fontWeight: '600', fontFamily: MONO, marginLeft: '8px', flexShrink: 0 }}>{count.toLocaleString()}</span>
                                    </div>
                                  ))}
                                </div>
                              ))}
                            </div>
                          </div>
                        )}
                        <div style={{ borderTop: `1px solid ${C.border}`, marginTop: '20px', paddingTop: '20px' }}>
                          <button onClick={() => setReport(buildReport(datasetSummary))}
                            style={{ ...S.btnPrimary, background: 'linear-gradient(135deg, #6366f1 0%, #8b5cf6 100%)', boxShadow: '0 0 14px #6366f128' }}>
                            {report ? 'Regenerate Report' : 'Generate Report'}
                          </button>
                        </div>
                      </div>

                      {report && (
                        <div style={S.card}>
                          <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: '20px' }}>
                            <h3 style={{ margin: 0, fontSize: '0.92rem', fontWeight: '600' }}>Dataset Report</h3>
                            <div style={S.badge(C.success, C.successSoft)}><div style={S.dot(C.success)} />Generated</div>
                          </div>
                          {report.map((section, i) => (
                            <div key={i} style={{ marginBottom: i < report.length - 1 ? '20px' : 0 }}>
                              <div style={{ fontSize: '0.62rem', color: C.textSec, fontWeight: '700', textTransform: 'uppercase', letterSpacing: '0.08em', marginBottom: '10px' }}>{section.heading}</div>
                              <div style={{ display: 'flex', flexDirection: 'column', gap: '8px' }}>
                                {section.items.map((item, j) => (
                                  <div key={j} style={{ display: 'flex', alignItems: 'flex-start', gap: '10px', fontSize: '0.84rem', color: C.text, lineHeight: 1.65 }}>
                                    <span style={{ color: C.accent, flexShrink: 0, fontWeight: '700' }}>&rarr;</span>
                                    <span>{item}</span>
                                  </div>
                                ))}
                              </div>
                            </div>
                          ))}
                        </div>
                      )}
                    </>}

                  </div>

                  {/* Right sidebar */}
                  <div style={{ display: 'flex', flexDirection: 'column', gap: '16px' }}>

                    {/* Quick Stats */}
                    <div style={S.card}>
                      <h3 style={{ margin: '0 0 14px', fontSize: '0.72rem', fontWeight: '700', color: C.textSec, textTransform: 'uppercase', letterSpacing: '0.08em' }}>Quick Stats</h3>
                      <div style={{ display: 'flex', flexDirection: 'column', gap: '10px' }}>
                        {[
                          { label: 'Total Datasets', value: datasetList.length, color: C.accent,   icon: <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><ellipse cx="12" cy="5" rx="9" ry="3"/><path d="M21 12c0 1.66-4 3-9 3s-9-1.34-9-3"/><path d="M3 5v14c0 1.66 4 3 9 3s9-1.34 9-3V5"/></svg> },
                          { label: 'Total Rows',     value: totalRows.toLocaleString(), color: '#10b981', icon: <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><line x1="8" y1="6" x2="21" y2="6"/><line x1="8" y1="12" x2="21" y2="12"/><line x1="8" y1="18" x2="21" y2="18"/><line x1="3" y1="6" x2="3.01" y2="6"/><line x1="3" y1="12" x2="3.01" y2="12"/><line x1="3" y1="18" x2="3.01" y2="18"/></svg> },
                          { label: 'Total Columns',  value: totalCols, color: '#8b5cf6', icon: <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><rect x="3" y="3" width="7" height="18"/><rect x="14" y="3" width="7" height="18"/></svg> },
                        ].map(stat => (
                          <div key={stat.label} style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', background: C.bg, border: `1px solid ${C.border}`, borderRadius: '10px', padding: '11px 14px' }}>
                            <div style={{ display: 'flex', alignItems: 'center', gap: '10px' }}>
                              <div style={{ width: '28px', height: '28px', borderRadius: '7px', background: stat.color + '18', display: 'flex', alignItems: 'center', justifyContent: 'center', color: stat.color }}>
                                {stat.icon}
                              </div>
                              <div style={{ fontSize: '0.75rem', color: C.textSec }}>{stat.label}</div>
                            </div>
                            <div style={{ fontSize: '1.05rem', fontWeight: '700', color: stat.color, fontFamily: MONO }}>{stat.value}</div>
                          </div>
                        ))}
                      </div>
                    </div>

                    {/* Recent Uploads */}
                    <div style={S.card}>
                      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: '14px' }}>
                        <h3 style={{ margin: 0, fontSize: '0.72rem', fontWeight: '700', color: C.textSec, textTransform: 'uppercase', letterSpacing: '0.08em' }}>Recent Uploads</h3>
                        {datasetList.length > 5 && (
                          <span style={{ fontSize: '0.72rem', color: C.accent, cursor: 'pointer' }}>View all</span>
                        )}
                      </div>
                      {recentUploads.length === 0 ? (
                        <div style={{ fontSize: '0.78rem', color: C.textMuted, textAlign: 'center', padding: '16px 0' }}>No datasets yet</div>
                      ) : (
                        <div style={{ display: 'flex', flexDirection: 'column', gap: '12px' }}>
                          {recentUploads.map(ds => {
                            const type   = getFileType(ds.filename)
                            const tStyle = dsTypeStyle(type)
                            return (
                              <div key={ds.id} style={{ display: 'flex', alignItems: 'center', gap: '10px' }}>
                                <div style={{ width: '30px', height: '26px', borderRadius: '6px', background: tStyle.bg, border: `1px solid ${tStyle.color}40`, display: 'flex', alignItems: 'center', justifyContent: 'center', fontSize: '0.58rem', fontWeight: '700', color: tStyle.color, flexShrink: 0 }}>
                                  {type.slice(0, 3)}
                                </div>
                                <div style={{ overflow: 'hidden', flex: 1 }}>
                                  <div style={{ fontSize: '0.78rem', color: C.text, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', fontFamily: MONO }}>{ds.filename}</div>
                                  <div style={{ fontSize: '0.68rem', color: C.textMuted }}>{fmtRelTime(ds.uploaded_at)}</div>
                                </div>
                              </div>
                            )
                          })}
                        </div>
                      )}
                    </div>

                  </div>

                </div>
              </>
            })()}

            {/* ── Scheduled ────────────────────────────────────────── */}
            {activeNav === 'scheduled' && (() => {
              const activeCount = scheduledList.filter(s => s.enabled).length
              const pausedCount = scheduledList.filter(s => !s.enabled).length
              const totalRuns   = scheduledList.reduce((s, sw) => s + (sw.run_count || 0), 0)
              const issueCount  = scheduleHealth.filter(sh => ['Missed','Delayed'].includes(sh.health)).length
              const SCHED_STATS = [
                { label: 'Active Schedules', value: scheduledLoading ? '…' : activeCount, sub: `${scheduledList.length} total configured`, accent: '#10b981',
                  icon: <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round"><circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 16 14"/></svg> },
                { label: 'Total Runs', value: scheduledLoading ? '…' : totalRuns, sub: 'Across all schedules', accent: '#3b82f6',
                  icon: <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round"><polygon points="5 3 19 12 5 21 5 3"/></svg> },
                { label: 'Issues Detected', value: scheduledLoading ? '…' : issueCount, sub: 'Missed or delayed', accent: '#f59e0b',
                  icon: <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round"><path d="M10.29 3.86L1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"/><line x1="12" y1="9" x2="12" y2="13"/><line x1="12" y1="17" x2="12.01" y2="17"/></svg> },
                { label: 'Paused', value: scheduledLoading ? '…' : pausedCount, sub: 'Awaiting resume', accent: '#64748b',
                  icon: <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round"><rect x="6" y="4" width="4" height="16"/><rect x="14" y="4" width="4" height="16"/></svg> },
              ]
              const activeDs = datasetList.find(d => d.id === selectedDatasetId)
              const COLS = 'minmax(180px, 2fr) 120px 150px 150px 100px 148px'
              return <>
                <div style={{ marginBottom: '16px' }}>
                  <h2 style={{ margin: '0 0 4px', fontSize: '1.4rem', fontWeight: '700', color: C.text, letterSpacing: '-0.4px' }}>Scheduled</h2>
                  <p style={{ margin: 0, color: C.textMuted, fontSize: '0.75rem' }}>Workflows saved to run automatically at a recurring interval.</p>
                </div>

                {/* ── Create Schedule ── */}
                <div style={{ ...S.card, marginBottom: '18px' }}>
                  <h3 style={{ margin: '0 0 16px', fontSize: '0.75rem', fontWeight: '500', color: C.textSec, letterSpacing: '0.01em' }}>Create Schedule</h3>
                  <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '28px' }}>
                    {/* Left */}
                    <div style={{ display: 'flex', flexDirection: 'column', gap: '0' }}>
                      <label style={{ ...S.label, fontSize: '0.62rem' }}>Task Description (must include a frequency)</label>
                      <textarea
                        rows={4}
                        placeholder="Example: email me a daily dataset report"
                        value={scheduleInput}
                        onChange={e => { setScheduleInput(e.target.value); setScheduleError(null); setScheduleSuccess(null) }}
                        style={{ ...S.textarea, fontFamily: FONT, fontSize: '0.76rem', marginBottom: '8px' }}
                      />
                      <div style={{ fontSize: '0.68rem', color: C.textMuted, marginBottom: '18px', lineHeight: 1.6 }}>
                        Try: "generate a daily dataset report" · "email me a weekly dataset report on Monday"
                        {selectedDatasetId && activeDs && <span style={{ color: C.accent }}> · {activeDs.filename} active</span>}
                      </div>
                      <button
                        onClick={handleCreateSchedule}
                        disabled={scheduleCreating || !scheduleInput.trim()}
                        style={{ ...S.btnPrimary, alignSelf: 'flex-start', background: 'linear-gradient(135deg, #6366f1 0%, #8b5cf6 100%)', boxShadow: scheduleCreating ? 'none' : '0 0 18px #6366f125', opacity: (scheduleCreating || !scheduleInput.trim()) ? 0.6 : 1 }}
                      >
                        {scheduleCreating ? 'Saving…' : 'Save Schedule'}
                      </button>
                      {scheduleError && (
                        <div style={{ marginTop: '12px', background: C.dangerSoft, border: `1px solid ${C.danger}40`, borderRadius: '8px', padding: '10px 14px', fontSize: '0.79rem', color: C.danger }}>{scheduleError}</div>
                      )}
                      {scheduleSuccess && (
                        <div style={{ marginTop: '12px', background: C.successSoft, border: `1px solid ${C.success}40`, borderRadius: '8px', padding: '10px 14px', fontSize: '0.79rem', color: C.success }}>{scheduleSuccess}</div>
                      )}
                    </div>
                    {/* Right */}
                    <div style={{ display: 'flex', flexDirection: 'column', gap: '16px' }}>
                      {/* Dataset */}
                      <div>
                        <label style={{ ...S.label, fontSize: '0.62rem' }}>Dataset</label>
                        <div style={{ position: 'relative' }}>
                          <select
                            value={selectedDatasetId ?? ''}
                            onChange={e => setSelectedDatasetId(e.target.value ? Number(e.target.value) : null)}
                            style={{ width: '100%', padding: '9px 32px 9px 12px', background: C.bg, border: `1px solid ${C.border}`, borderRadius: '8px', color: selectedDatasetId ? C.text : C.textMuted, fontSize: '0.79rem', fontFamily: FONT, cursor: 'pointer', appearance: 'none', outline: 'none' }}
                          >
                            <option value=''>No dataset selected</option>
                            {datasetList.map(ds => (
                              <option key={ds.id} value={ds.id}>
                                {ds.filename} · {(ds.row_count || 0).toLocaleString()} rows
                              </option>
                            ))}
                          </select>
                          <div style={{ position: 'absolute', right: '10px', top: '50%', transform: 'translateY(-50%)', pointerEvents: 'none', color: C.textMuted }}>
                            <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round"><path d="m6 9 6 6 6-6"/></svg>
                          </div>
                        </div>
                      </div>
                      {/* Info box */}
                      <div style={{ padding: '12px 14px', background: `${C.accent}08`, border: `1px solid ${C.accent}20`, borderRadius: '8px' }}>
                        <div style={{ fontSize: '0.67rem', color: C.accent, fontWeight: '600', textTransform: 'uppercase', letterSpacing: '0.06em', marginBottom: '5px' }}>How it works</div>
                        <p style={{ margin: 0, fontSize: '0.72rem', color: C.textMuted, lineHeight: 1.6 }}>
                          Write a natural language task that includes a frequency word (daily, weekly, monthly). The AI interpreter will parse your intent and schedule it automatically.
                        </p>
                      </div>
                    </div>
                  </div>
                </div>

                {/* ── Schedule Health stat cards ── */}
                <div style={{ display: 'grid', gridTemplateColumns: 'repeat(4, 1fr)', gap: '14px', marginBottom: '18px' }}>
                  {SCHED_STATS.map(({ label, value, sub, accent, icon }) => (
                    <div key={label} style={{ ...S.card, padding: '18px 20px', display: 'flex', alignItems: 'center', gap: '14px' }}>
                      <div style={{ width: 44, height: 44, borderRadius: '12px', background: accent + '18', display: 'flex', alignItems: 'center', justifyContent: 'center', flexShrink: 0, color: accent }}>
                        {icon}
                      </div>
                      <div style={{ minWidth: 0 }}>
                        <div style={{ fontSize: '0.6rem', color: C.textMuted, fontWeight: '500', textTransform: 'uppercase', letterSpacing: '0.07em', marginBottom: '4px' }}>{label}</div>
                        <div style={{ fontSize: '1.25rem', fontWeight: '600', color: C.text, letterSpacing: '-0.3px', lineHeight: 1 }}>{value}</div>
                        <div style={{ fontSize: '0.64rem', color: C.textMuted, marginTop: '3px' }}>{sub}</div>
                      </div>
                    </div>
                  ))}
                </div>

                {/* ── Your Schedules table ── */}
                <div style={S.card}>
                  <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: '18px' }}>
                    <div>
                      <h3 style={{ margin: 0, fontSize: '0.75rem', fontWeight: '500', color: C.textSec, letterSpacing: '0.01em' }}>Your Schedules</h3>
                    </div>
                  </div>
                  {scheduledLoading ? (
                    <div style={{ padding: '40px 0', textAlign: 'center', color: C.textMuted, fontSize: '0.82rem' }}>Loading…</div>
                  ) : scheduledList.length === 0 ? (
                    <div style={{ padding: '40px 0', textAlign: 'center', color: C.textMuted, fontSize: '0.82rem', lineHeight: 1.7 }}>
                      No scheduled workflows yet.<br />Create one above to get started.
                    </div>
                  ) : (
                    <>
                      {/* Column headers */}
                      <div style={{ display: 'grid', gridTemplateColumns: COLS, borderBottom: `1px solid ${C.border}`, paddingBottom: '8px', marginBottom: '2px' }}>
                        {['Task', 'Frequency', 'Next Run', 'Last Run', 'Status', 'Actions'].map(col => (
                          <div key={col} style={{ padding: '0 12px', fontSize: '0.63rem', color: C.textSec, fontWeight: '600', textTransform: 'uppercase', letterSpacing: '0.07em' }}>{col}</div>
                        ))}
                      </div>
                      {scheduledList.map((sw, idx) => {
                        const notLast = idx < scheduledList.length - 1
                        const failed  = sw.last_status === 'failed'
                        const stColor = sw.enabled ? C.success : C.warn
                        const stBg    = sw.enabled ? C.successSoft : C.warnSoft
                        const isLoading = schedulePauseLoading.has(sw.id)
                        const isExpanded = schedExpandedId === sw.id
                        const isHistLoading = schedRunHistLoading.has(sw.id)
                        const isRunNow = schedRunNowLoading.has(sw.id)
                        const runs = schedRunHistories[sw.id] || []
                        return (
                          <div key={sw.id} style={{ borderRadius: '4px' }}>
                            <div style={{ display: 'grid', gridTemplateColumns: COLS, padding: '10px 0', borderBottom: (!failed || !sw.last_error) && !isExpanded && notLast ? `1px solid ${C.border}` : 'none', alignItems: 'center' }}>
                              {/* Task */}
                              <div style={{ padding: '0 12px', overflow: 'hidden', display: 'flex', alignItems: 'center', gap: '6px' }}>
                                <button onClick={() => handleExpandSchedule(sw.id)} title={isExpanded ? 'Collapse' : 'Expand run history'} style={{ background: 'none', border: 'none', cursor: 'pointer', color: C.textMuted, padding: '2px', display: 'flex', alignItems: 'center', flexShrink: 0 }}>
                                  {isExpanded
                                    ? <svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round"><path d="m18 15-6-6-6 6"/></svg>
                                    : <svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round"><path d="m6 9 6 6 6-6"/></svg>}
                                </button>
                                <div style={{ overflow: 'hidden' }}>
                                  <div style={{ fontSize: '0.75rem', fontWeight: '400', color: C.textSec, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }} title={sw.input_text}>{sw.input_text}</div>
                                  {sw.run_count > 0 && <div style={{ fontSize: '0.64rem', color: C.textMuted, marginTop: '1px' }}>{sw.run_count} run{sw.run_count !== 1 ? 's' : ''}</div>}
                                </div>
                              </div>
                              {/* Frequency — prefer human_label when available */}
                              <div style={{ padding: '0 12px', fontSize: '0.72rem', color: C.textMuted, textTransform: sw.human_label ? 'none' : 'capitalize' }}>
                                {sw.human_label || ((sw.frequency || '') + (sw.day_of_week ? ` on ${sw.day_of_week}` : ''))}
                              </div>
                              {/* Next Run */}
                              <div style={{ padding: '0 12px', fontSize: '0.7rem', color: C.textMuted, whiteSpace: 'nowrap' }}>
                                {sw.next_run_at ? new Date(sw.next_run_at).toLocaleString(undefined, { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' }) : '—'}
                              </div>
                              {/* Last Run */}
                              <div style={{ padding: '0 12px', fontSize: '0.7rem', color: C.textMuted, whiteSpace: 'nowrap' }}>
                                {sw.last_run_at ? new Date(sw.last_run_at).toLocaleString(undefined, { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' }) : '—'}
                              </div>
                              {/* Status */}
                              <div style={{ padding: '0 12px' }}>
                                <span style={{ display: 'inline-flex', alignItems: 'center', gap: '5px', background: stBg, color: stColor, border: `1px solid ${stColor}28`, borderRadius: '20px', padding: '3px 9px', fontSize: '0.67rem', fontWeight: '600', letterSpacing: '0.03em', whiteSpace: 'nowrap' }}>
                                  <span style={{ width: 5, height: 5, borderRadius: '50%', background: stColor, display: 'inline-block' }}/>
                                  {sw.enabled ? 'Active' : 'Paused'}
                                </span>
                              </div>
                              {/* Actions */}
                              <div style={{ padding: '0 12px', display: 'flex', alignItems: 'center', gap: '4px' }}>
                                {/* Pause / Resume */}
                                <button
                                  onClick={() => sw.enabled ? handlePauseSchedule(sw.id) : handleResumeSchedule(sw.id)}
                                  disabled={isLoading}
                                  title={sw.enabled ? 'Pause' : 'Resume'}
                                  style={{ width: 30, height: 30, borderRadius: '7px', border: `1px solid ${C.border}`, background: 'transparent', color: sw.enabled ? C.warn : C.success, cursor: isLoading ? 'not-allowed' : 'pointer', display: 'flex', alignItems: 'center', justifyContent: 'center', opacity: isLoading ? 0.5 : 1, transition: 'background 0.12s' }}
                                  onMouseEnter={e => e.currentTarget.style.background = C.borderAlt}
                                  onMouseLeave={e => e.currentTarget.style.background = 'transparent'}
                                >
                                  {sw.enabled
                                    ? <svg width="11" height="11" viewBox="0 0 24 24" fill="currentColor"><rect x="6" y="4" width="4" height="16"/><rect x="14" y="4" width="4" height="16"/></svg>
                                    : <svg width="11" height="11" viewBox="0 0 24 24" fill="currentColor"><polygon points="5 3 19 12 5 21 5 3"/></svg>}
                                </button>
                                {/* Run Now */}
                                <button
                                  onClick={() => handleRunNow(sw.id)}
                                  disabled={isRunNow}
                                  title="Run Now"
                                  style={{ width: 30, height: 30, borderRadius: '7px', border: `1px solid ${C.success}30`, background: isRunNow ? C.successSoft : 'transparent', color: C.success, cursor: isRunNow ? 'not-allowed' : 'pointer', display: 'flex', alignItems: 'center', justifyContent: 'center', opacity: isRunNow ? 0.6 : 1, transition: 'background 0.12s' }}
                                  onMouseEnter={e => { if (!isRunNow) e.currentTarget.style.background = C.successSoft }}
                                  onMouseLeave={e => { if (!isRunNow) e.currentTarget.style.background = 'transparent' }}
                                >
                                  {isRunNow
                                    ? <div style={{ width: '9px', height: '9px', borderRadius: '50%', border: `2px solid ${C.success}40`, borderTopColor: C.success, animation: 'spin 0.75s linear infinite' }} />
                                    : <svg width="10" height="10" viewBox="0 0 24 24" fill="currentColor"><polygon points="5 3 19 12 5 21 5 3"/></svg>}
                                </button>
                                {/* Delete */}
                                <button
                                  onClick={() => handleDeleteSchedule(sw.id)}
                                  title="Delete"
                                  style={{ width: 30, height: 30, borderRadius: '7px', border: `1px solid ${C.border}`, background: 'transparent', color: C.danger, cursor: 'pointer', display: 'flex', alignItems: 'center', justifyContent: 'center', transition: 'background 0.12s' }}
                                  onMouseEnter={e => e.currentTarget.style.background = C.dangerSoft}
                                  onMouseLeave={e => e.currentTarget.style.background = 'transparent'}
                                >
                                  <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><polyline points="3 6 5 6 21 6"/><path d="M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6"/><path d="M10 11v6"/><path d="M14 11v6"/><path d="M9 6V4h6v2"/></svg>
                                </button>
                              </div>
                            </div>
                            {failed && sw.last_error && (
                              <div style={{ padding: '0 12px 8px', borderBottom: isExpanded || notLast ? `1px solid ${C.border}` : 'none' }}>
                                <span style={{ fontSize: '0.69rem', color: C.danger, opacity: 0.85 }}>↳ {sw.last_error}</span>
                              </div>
                            )}
                            {isExpanded && (
                              <div style={{ padding: '10px 14px 14px', borderBottom: notLast ? `1px solid ${C.border}` : 'none', background: C.bg }}>
                                <div style={{ fontSize: '0.62rem', fontWeight: '600', color: C.textMuted, textTransform: 'uppercase', letterSpacing: '0.07em', marginBottom: '8px' }}>Run History</div>
                                {isHistLoading ? (
                                  <div style={{ fontSize: '0.78rem', color: C.textMuted }}>Loading…</div>
                                ) : runs.length === 0 ? (
                                  <div style={{ fontSize: '0.78rem', color: C.textMuted }}>No runs recorded yet.</div>
                                ) : (
                                  <div style={{ display: 'flex', flexDirection: 'column', gap: '5px' }}>
                                    {runs.slice(0, 8).map(run => (
                                      <div key={run.id} style={{ display: 'flex', alignItems: 'center', gap: '10px', fontSize: '0.72rem', flexWrap: 'wrap' }}>
                                        <span style={{ minWidth: '70px', fontWeight: '600', color: run.status === 'completed' ? C.success : run.status === 'failed' ? C.danger : C.warn }}>{run.status}</span>
                                        <span style={{ color: C.textMuted, minWidth: '72px' }}>{_relTime(run.started_at)}</span>
                                        <span style={{ color: C.textMuted, minWidth: '46px' }}>{_fmtDuration(run.duration_ms)}</span>
                                        {run.trigger_type === 'manual' && <span style={{ color: C.accent, fontSize: '0.65rem', background: C.accentSoft, borderRadius: '4px', padding: '0 5px' }}>manual</span>}
                                        {run.related_report_id && (
                                          <button onClick={() => { setSelectedReportId(run.related_report_id); setActiveNav('reports') }} style={{ background: 'none', border: 'none', cursor: 'pointer', color: C.accent, fontSize: '0.7rem', padding: 0, textDecoration: 'underline', fontFamily: FONT }}>View Report</button>
                                        )}
                                        {run.status === 'failed' && run.error_message && (
                                          <span style={{ color: C.danger, fontSize: '0.68rem', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', maxWidth: '260px' }} title={run.error_message}>{run.error_message}</span>
                                        )}
                                      </div>
                                    ))}
                                  </div>
                                )}
                              </div>
                            )}
                          </div>
                        )
                      })}
                    </>
                  )}
                </div>
              </>
            })()}

            {/* ── Scheduler Activity ───────────────────────────────── */}
            {activeNav === 'sched-activity' && (() => {
              const RUN_COLS = 'minmax(180px, 2fr) 80px 90px 150px 70px minmax(120px, 1fr)'
              const statusColor = s => s === 'completed' ? C.success : s === 'failed' ? C.danger : C.warn
              const statusBg    = s => s === 'completed' ? C.successSoft : s === 'failed' ? C.dangerSoft : C.warnSoft
              return <>
                <div style={{ marginBottom: '16px', display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
                  <div>
                    <h2 style={{ margin: '0 0 4px', fontSize: '1.4rem', fontWeight: '700', color: C.text, letterSpacing: '-0.4px' }}>Scheduler Activity</h2>
                    <p style={{ margin: 0, color: C.textMuted, fontSize: '0.75rem' }}>All recent scheduled workflow runs, newest first.</p>
                  </div>
                  <button onClick={refreshSchedRuns} style={{ background: 'none', border: 'none', color: C.textMuted, fontSize: '0.68rem', cursor: 'pointer', fontFamily: FONT, display: 'flex', alignItems: 'center', gap: '5px' }}>
                    <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><polyline points="23 4 23 10 17 10"/><path d="M20.49 15a9 9 0 1 1-.28-4.5"/></svg>
                    Refresh
                  </button>
                </div>
                <div style={S.card}>
                  {schedRunsLoading ? (
                    <div style={{ padding: '40px 0', textAlign: 'center', color: C.textMuted, fontSize: '0.82rem' }}>Loading…</div>
                  ) : schedRunsError ? (
                    <div style={{ padding: '40px 0', textAlign: 'center', color: C.danger, fontSize: '0.82rem' }}>{schedRunsError}</div>
                  ) : schedRuns.length === 0 ? (
                    <div style={{ padding: '40px 0', textAlign: 'center', color: C.textMuted, fontSize: '0.82rem', lineHeight: 1.7 }}>
                      No scheduled runs yet.<br />Runs will appear here after your first scheduled execution.
                    </div>
                  ) : (
                    <>
                      <div style={{ display: 'grid', gridTemplateColumns: RUN_COLS, borderBottom: `1px solid ${C.border}`, paddingBottom: '8px', marginBottom: '2px' }}>
                        {['Task', 'Trigger', 'Status', 'Started', 'Duration', 'Details'].map(col => (
                          <div key={col} style={{ padding: '0 10px', fontSize: '0.63rem', color: C.textSec, fontWeight: '600', textTransform: 'uppercase', letterSpacing: '0.07em' }}>{col}</div>
                        ))}
                      </div>
                      {schedRuns.map((run, idx) => (
                        <div key={run.id} style={{ display: 'grid', gridTemplateColumns: RUN_COLS, padding: '9px 0', borderBottom: idx < schedRuns.length - 1 ? `1px solid ${C.border}` : 'none', alignItems: 'center' }}>
                          <div style={{ padding: '0 10px', overflow: 'hidden' }}>
                            <div style={{ fontSize: '0.75rem', color: C.textSec, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }} title={run.schedule_input_text || '—'}>{run.schedule_input_text || '—'}</div>
                            {run.schedule_frequency && <div style={{ fontSize: '0.63rem', color: C.textMuted, textTransform: 'capitalize', marginTop: '1px' }}>{run.schedule_frequency}</div>}
                          </div>
                          <div style={{ padding: '0 10px' }}>
                            <span style={{ fontSize: '0.67rem', background: run.trigger_type === 'manual' ? C.accentSoft : C.borderAlt, color: run.trigger_type === 'manual' ? C.accent : C.textMuted, borderRadius: '4px', padding: '2px 6px', fontWeight: '500' }}>
                              {run.trigger_type || 'scheduled'}
                            </span>
                          </div>
                          <div style={{ padding: '0 10px' }}>
                            <span style={{ display: 'inline-flex', alignItems: 'center', gap: '4px', background: statusBg(run.status), color: statusColor(run.status), border: `1px solid ${statusColor(run.status)}28`, borderRadius: '20px', padding: '2px 8px', fontSize: '0.67rem', fontWeight: '600' }}>
                              <span style={{ width: 4, height: 4, borderRadius: '50%', background: statusColor(run.status), display: 'inline-block' }}/>
                              {run.status}
                            </span>
                          </div>
                          <div style={{ padding: '0 10px', fontSize: '0.7rem', color: C.textMuted, whiteSpace: 'nowrap' }}>
                            {run.started_at ? new Date(run.started_at).toLocaleString(undefined, { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' }) : '—'}
                          </div>
                          <div style={{ padding: '0 10px', fontSize: '0.7rem', color: C.textMuted }}>
                            {_fmtDuration(run.duration_ms)}
                          </div>
                          <div style={{ padding: '0 10px', fontSize: '0.72rem', overflow: 'hidden' }}>
                            {run.status === 'failed' && run.error_message ? (
                              <span style={{ color: C.danger, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', display: 'block' }} title={run.error_message}>{run.error_message}</span>
                            ) : run.related_report_id ? (
                              <button onClick={() => { setSelectedReportId(run.related_report_id); setActiveNav('reports') }} style={{ background: 'none', border: 'none', cursor: 'pointer', color: C.accent, fontSize: '0.7rem', padding: 0, textDecoration: 'underline', fontFamily: FONT }}>View Report</button>
                            ) : (
                              <span style={{ color: C.textMuted }}>—</span>
                            )}
                          </div>
                        </div>
                      ))}
                    </>
                  )}
                </div>
              </>
            })()}

            {/* ── History ──────────────────────────────────────────── */}
            {activeNav === 'history' && (() => {
              const q = historySearch.toLowerCase()
              const filteredRows = q
                ? history.filter(r =>
                    fmtHistorySummary(r).toLowerCase().includes(q) ||
                    (r.intent || '').toLowerCase().includes(q) ||
                    (r.dataset_name || '').toLowerCase().includes(q) ||
                    (r.task_type || '').toLowerCase().includes(q)
                  )
                : history
              const total     = history.length
              const completed = history.filter(r => ['completed','success'].includes(r.status)).length
              const failed    = history.filter(r => r.status === 'failed').length
              const totalMs   = history.reduce((s, r) => s + (r.duration_ms || 0), 0)
              const hh = Math.floor(totalMs / 3600000)
              const mm = Math.floor((totalMs % 3600000) / 60000)
              const timeSaved = totalMs === 0 ? '—' : hh > 0 ? `${hh}h ${mm}m` : mm > 0 ? `${mm}m` : '<1m'
              const successPct = total > 0 ? `${Math.round((completed / total) * 100)}% success rate` : 'No data yet'
              const failPct    = total > 0 ? `${Math.round((failed / total) * 100)}% failure rate`    : 'No data yet'
              const HIST_STATS = [
                { label: 'Total Executions', value: total || '—', sub: 'All time',              accent: '#3b82f6',
                  icon: <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/><line x1="16" y1="13" x2="8" y2="13"/><line x1="16" y1="17" x2="8" y2="17"/><polyline points="10 9 9 9 8 9"/></svg> },
                { label: 'Completed',        value: completed || '—', sub: successPct,          accent: '#10b981',
                  icon: <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round"><path d="M22 11.08V12a10 10 0 1 1-5.93-9.14"/><polyline points="22 4 12 14.01 9 11.01"/></svg> },
                { label: 'Failed',           value: failed    || '—', sub: failPct,             accent: '#ef4444',
                  icon: <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round"><circle cx="12" cy="12" r="10"/><line x1="15" y1="9" x2="9" y2="15"/><line x1="9" y1="9" x2="15" y2="15"/></svg> },
                { label: 'Total Time Saved', value: timeSaved,         sub: 'Across all runs',  accent: '#8b5cf6',
                  icon: <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round"><circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 16 14"/></svg> },
              ]
              const COLS = 'minmax(170px, 1.5fr) minmax(130px, 1fr) 108px 78px 148px minmax(120px, 1fr) 96px'
              return <>
                {/* ── Page header ── */}
                <div style={{ marginBottom: '16px' }}>
                  <h2 style={{ margin: '0 0 4px', fontSize: '1.4rem', fontWeight: '700', color: C.text, letterSpacing: '-0.4px' }}>History</h2>
                  <p style={{ margin: 0, color: C.textMuted, fontSize: '0.75rem' }}>View, manage and rerun all your workflow executions.</p>
                </div>
                {/* ── Stat cards ── */}
                <div style={{ display: 'grid', gridTemplateColumns: 'repeat(4, 1fr)', gap: '14px', marginBottom: '22px' }}>
                  {HIST_STATS.map(({ label, value, sub, accent, icon }) => (
                    <div key={label} style={{ ...S.card, padding: '18px 20px', display: 'flex', alignItems: 'center', gap: '14px' }}>
                      <div style={{ width: 44, height: 44, borderRadius: '12px', background: accent + '18', display: 'flex', alignItems: 'center', justifyContent: 'center', flexShrink: 0, color: accent }}>
                        {icon}
                      </div>
                      <div style={{ minWidth: 0 }}>
                        <div style={{ fontSize: '0.6rem', color: C.textMuted, fontWeight: '500', textTransform: 'uppercase', letterSpacing: '0.07em', marginBottom: '4px' }}>{label}</div>
                        <div style={{ fontSize: '1.25rem', fontWeight: '600', color: C.text, letterSpacing: '-0.3px', lineHeight: 1 }}>{value}</div>
                        <div style={{ fontSize: '0.64rem', color: C.textMuted, marginTop: '3px' }}>{sub}</div>
                      </div>
                    </div>
                  ))}
                </div>
                {/* ── Table card ── */}
                <div style={S.card}>
                  <div style={{ display: 'flex', alignItems: 'flex-start', justifyContent: 'space-between', marginBottom: historyMsg ? '10px' : '20px' }}>
                    <div>
                      <h3 style={{ margin: '0 0 4px', fontSize: '0.75rem', fontWeight: '500', color: C.textSec, letterSpacing: '0.01em' }}>Recent Activity</h3>
                      <p style={{ margin: 0, color: C.textMuted, fontSize: '0.7rem' }}>Your latest workflow executions</p>
                    </div>
                  </div>
                  {historyMsg && (
                    <div style={{ marginBottom: '14px', background: historyMsgType === 'error' ? C.dangerSoft : C.successSoft, border: `1px solid ${historyMsgType === 'error' ? C.danger : C.success}40`, borderRadius: '8px', padding: '8px 14px', fontSize: '0.8rem', color: historyMsgType === 'error' ? C.danger : C.success }}>
                      {historyMsg}
                    </div>
                  )}
                  {/* Column headers */}
                  <div style={{ display: 'grid', gridTemplateColumns: COLS, borderBottom: `1px solid ${C.border}`, paddingBottom: '8px', marginBottom: '2px' }}>
                    {[
                      { label: 'Workflow' },
                      { label: 'Dataset', icon: <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round"><ellipse cx="12" cy="5" rx="9" ry="3"/><path d="M21 12c0 1.66-4 3-9 3s-9-1.34-9-3"/><path d="M3 5v14c0 1.66 4 3 9 3s9-1.34 9-3V5"/></svg> },
                      { label: 'Status' },
                      { label: 'Duration' },
                      { label: 'Started' },
                      { label: 'Result' },
                      { label: 'Actions' },
                    ].map(({ label, icon }) => (
                      <div key={label} style={{ padding: '0 12px', fontSize: '0.63rem', color: C.textSec, fontWeight: '600', textTransform: 'uppercase', letterSpacing: '0.07em', display: 'flex', alignItems: 'center', gap: '4px' }}>
                        {icon && <span style={{ opacity: 0.7 }}>{icon}</span>}
                        {label}
                      </div>
                    ))}
                  </div>
                  {/* Rows */}
                  {historyLoading ? (
                    <div style={{ padding: '52px 12px', textAlign: 'center', color: C.textMuted, fontSize: '0.82rem' }}>Loading executions…</div>
                  ) : filteredRows.length === 0 ? (
                    <div style={{ padding: '52px 12px', textAlign: 'center', color: C.textMuted, fontSize: '0.82rem' }}>
                      {history.length === 0 ? 'No executions yet. Run a workflow to see your history here.' : 'No results match your search.'}
                    </div>
                  ) : filteredRows.map((row, idx) => {
                    const isOk   = ['completed','success'].includes(row.status)
                    const isFail = row.status === 'failed'
                    const sc     = isOk ? C.success : isFail ? C.danger : C.warn
                    const scBg   = isOk ? C.successSoft : isFail ? C.dangerSoft : C.warnSoft
                    const meta      = getHistMeta(row.task_type)
                    const notLast   = idx < filteredRows.length - 1
                    const isHovered = hoveredHistRow === row.id
                    const summary   = fmtHistorySummary(row)
                    const duration  = fmtHistoryDuration(row)
                    return (
                      <div key={row.id} onMouseEnter={() => setHoveredHistRow(row.id)} onMouseLeave={() => setHoveredHistRow(null)}
                        style={{ borderRadius: '5px', background: isHovered ? `${C.accent}07` : 'transparent', transition: 'background 0.12s' }}>
                        <div style={{ display: 'grid', gridTemplateColumns: COLS, padding: '10px 0', borderBottom: notLast ? `1px solid ${C.border}` : 'none', alignItems: 'center' }}>
                          {/* Workflow */}
                          <div style={{ padding: '0 12px', display: 'flex', alignItems: 'center', gap: '10px', overflow: 'hidden' }}>
                            <div style={{ width: 34, height: 34, borderRadius: '9px', background: meta.bg, display: 'flex', alignItems: 'center', justifyContent: 'center', flexShrink: 0, color: meta.color }}>
                              {meta.icon}
                            </div>
                            <div style={{ overflow: 'hidden' }}>
                              <div style={{ fontSize: '0.75rem', fontWeight: '400', color: C.textSec, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }} title={row.intent || summary}>{summary}</div>
                              <div style={{ fontSize: '0.68rem', color: C.textMuted, marginTop: '1px' }}>{meta.label}</div>
                            </div>
                          </div>
                          {/* Dataset */}
                          <div style={{ padding: '0 12px', overflow: 'hidden' }}>
                            {row.dataset_name ? (
                              <div style={{ display: 'flex', alignItems: 'center', gap: '7px', overflow: 'hidden' }}>
                                <div style={{ width: 26, height: 26, borderRadius: '6px', background: '#10b98114', display: 'flex', alignItems: 'center', justifyContent: 'center', flexShrink: 0, color: '#10b981' }}>
                                  <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><rect x="3" y="3" width="7" height="7"/><rect x="14" y="3" width="7" height="7"/><rect x="14" y="14" width="7" height="7"/><rect x="3" y="14" width="7" height="7"/></svg>
                                </div>
                                <div style={{ overflow: 'hidden' }}>
                                  <div style={{ fontSize: '0.73rem', fontWeight: '400', color: C.textSec, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{row.dataset_name}</div>
                                  {row.dataset_row_count != null && <div style={{ fontSize: '0.67rem', color: C.textMuted, marginTop: '1px' }}>{row.dataset_row_count.toLocaleString()} rows</div>}
                                </div>
                              </div>
                            ) : (
                              <span style={{ fontSize: '0.75rem', color: C.textMuted }}>—</span>
                            )}
                          </div>
                          {/* Status */}
                          <div style={{ padding: '0 12px' }}>
                            <span style={{ display: 'inline-flex', alignItems: 'center', gap: '5px', background: scBg, color: sc, border: `1px solid ${sc}28`, borderRadius: '20px', padding: '3px 9px', fontSize: '0.67rem', fontWeight: '600', letterSpacing: '0.03em', whiteSpace: 'nowrap' }}>
                              {isOk
                                ? <svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="3" strokeLinecap="round" strokeLinejoin="round"><polyline points="20 6 9 17 4 12"/></svg>
                                : isFail
                                ? <svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="3" strokeLinecap="round" strokeLinejoin="round"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>
                                : <svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round" style={{ animation: 'spin 1s linear infinite' }}><path d="M21 12a9 9 0 1 1-6.219-8.56"/></svg>}
                              {row.status_label || row.status}
                            </span>
                          </div>
                          {/* Duration */}
                          <div style={{ padding: '0 12px', fontSize: '0.72rem', fontWeight: '400', color: duration === 'Instant' ? C.success : C.textMuted, fontFamily: MONO }}>
                            {duration}
                          </div>
                          {/* Started */}
                          <div style={{ padding: '0 12px' }}>
                            {row.started_at ? <>
                              <div style={{ fontSize: '0.72rem', color: C.textSec, whiteSpace: 'nowrap' }}>{new Date(row.started_at).toLocaleString(undefined, { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' })}</div>
                              <div style={{ fontSize: '0.67rem', color: C.textMuted, marginTop: '1px' }}>{fmtRelTime(row.started_at)}</div>
                            </> : <span style={{ color: C.textMuted, fontSize: '0.75rem' }}>—</span>}
                          </div>
                          {/* Result */}
                          <div style={{ padding: '0 12px', overflow: 'hidden' }}>
                            {isFail ? (
                              <div>
                                <div style={{ fontSize: '0.72rem', fontWeight: '400', color: C.danger }}>Execution failed</div>
                                {row.error_message && <div style={{ fontSize: '0.67rem', color: C.danger, opacity: 0.7, marginTop: '1px', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{row.error_message.slice(0, 48)}{row.error_message.length > 48 ? '…' : ''}</div>}
                              </div>
                            ) : isOk ? (
                              <div style={{ fontSize: '0.72rem', color: C.textMuted, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }} title={row.summary}>{row.summary || 'Completed successfully'}</div>
                            ) : (
                              <div style={{ fontSize: '0.77rem', color: C.warn }}>In progress…</div>
                            )}
                          </div>
                          {/* Actions */}
                          <div style={{ padding: '0 12px' }}>
                            {(isOk || isFail) && (
                              <button onClick={() => isFail ? handleRetry(row.id) : handleRerun(row.id)} disabled={execActionLoading.has(row.id)}
                                style={{ background: isFail ? C.dangerSoft : C.accentSoft, border: `1px solid ${isFail ? C.danger : C.accent}40`, color: isFail ? C.danger : C.accent, borderRadius: '6px', padding: '4px 11px', fontSize: '0.71rem', cursor: execActionLoading.has(row.id) ? 'not-allowed' : 'pointer', fontFamily: FONT, fontWeight: '600', opacity: execActionLoading.has(row.id) ? 0.5 : 1, whiteSpace: 'nowrap', transition: 'opacity 0.12s' }}>
                                {execActionLoading.has(row.id) ? '…' : isFail ? 'Retry' : 'Re-run'}
                              </button>
                            )}
                          </div>
                        </div>
                      </div>
                    )
                  })}
                </div>
              </>
            })()}

            {/* ── Reports ──────────────────────────────────────────── */}
            {activeNav === 'reports' && (() => {
              const typeLabel = t => t === 'email_dataset_report' ? 'Emailed Report' : 'Dataset Report'
              const typeColor = t => t === 'email_dataset_report' ? '#06b6d4' : C.accent

              if (reportViewMode === 'detail') {
                const r = reportList.find(x => x.id === selectedReportId)
                return <>
                  {selectedReportLoading ? (
                    <div style={{ padding: '48px 32px', textAlign: 'center', color: C.textMuted, fontSize: '0.82rem' }}>Loading report…</div>
                  ) : selectedReportData?.content?.sections?.length > 0 ? (
                    <ErrorBoundary C={C}>
                      <Suspense fallback={<LazyFallback />}>
                        <ReportWorkspace
                          sections={selectedReportData.content.sections}
                          reportMeta={r}
                          C={C}
                          onExport={fmt => exportReport(selectedReportId, token, fmt)}
                          onEmail={to => emailReport(selectedReportId, to, token)}
                          SectionRenderer={ReportSection}
                          token={token}
                          reportPlan={selectedReportData.content?.report_plan ?? null}
                          onBack={() => { setReportViewMode('list'); setSelectedReportId(null); setSelectedReportData(null) }}
                        />
                      </Suspense>
                    </ErrorBoundary>
                  ) : (
                    <div style={{ fontSize: '0.78rem', color: C.textMuted }}>No report content available.</div>
                  )}
                </>
              }

              return <>
                <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: '16px' }}>
                  <div>
                    <h2 style={{ margin: '0 0 4px', fontSize: '1.4rem', fontWeight: '700', color: C.text, letterSpacing: '-0.4px' }}>Reports</h2>
                    <p style={{ margin: 0, color: C.textMuted, fontSize: '0.75rem' }}>Reports saved automatically after every dataset analysis or email workflow.</p>
                  </div>
                  <button onClick={refreshReports} style={{ background: 'none', border: 'none', color: C.textMuted, fontSize: '0.68rem', fontWeight: '400', cursor: 'pointer', fontFamily: FONT, display: 'flex', alignItems: 'center', gap: '5px' }}>
                    <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round"><polyline points="23 4 23 10 17 10"/><path d="M20.49 15a9 9 0 1 1-2.12-9.36L23 10"/></svg>Refresh
                  </button>
                </div>

                <div style={S.card}>
                  {reportListLoading ? (
                    <div style={{ padding: '40px', textAlign: 'center', color: C.textMuted, fontSize: '0.82rem' }}>Loading…</div>
                  ) : reportListError ? (
                    <div style={{ padding: '32px 20px', textAlign: 'center' }}>
                      <div style={{ fontSize: '0.82rem', color: C.danger, fontWeight: '500', marginBottom: '8px' }}>{reportListError}</div>
                      <button onClick={refreshReports} style={{ background: 'none', border: `1px solid ${C.border}`, borderRadius: '7px', padding: '5px 14px', fontSize: '0.72rem', color: C.textSec, cursor: 'pointer', fontFamily: FONT }}>Retry</button>
                    </div>
                  ) : reportList.length === 0 ? (
                    <div style={{ padding: '48px 20px', textAlign: 'center' }}>
                      <svg width="32" height="32" viewBox="0 0 24 24" fill="none" stroke={C.textMuted} strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" style={{ marginBottom: '12px', opacity: 0.5 }}><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/><line x1="16" y1="13" x2="8" y2="13"/><line x1="16" y1="17" x2="8" y2="17"/></svg>
                      <div style={{ fontSize: '0.82rem', color: C.textSec, fontWeight: '500', marginBottom: '4px' }}>No saved reports yet.</div>
                      <div style={{ fontSize: '0.73rem', color: C.textMuted }}>Generate a dataset report from the Overview tab to see it here.</div>
                    </div>
                  ) : (
                    <>
                      {/* Column headers */}
                      <div style={{ display: 'grid', gridTemplateColumns: 'minmax(200px, 1fr) 130px minmax(100px, 140px) 110px 36px', padding: '8px 20px', borderBottom: `1px solid ${C.border}` }}>
                        {['Report', 'Type', 'Dataset', 'Created', ''].map(h => (
                          <div key={h} style={{ fontSize: '0.62rem', color: C.textSec, fontWeight: '700', textTransform: 'uppercase', letterSpacing: '0.08em' }}>{h}</div>
                        ))}
                      </div>

                      {reportList.map((r, idx) => {
                        const tc = typeColor(r.task_type)
                        return (
                          <div key={r.id}
                            onClick={() => handleSelectReport(r.id)}
                            style={{ display: 'grid', gridTemplateColumns: 'minmax(200px, 1fr) 130px minmax(100px, 140px) 110px 36px', alignItems: 'center', padding: '12px 20px', cursor: 'pointer', background: 'transparent', borderBottom: idx < reportList.length - 1 ? `1px solid ${C.border}` : 'none', transition: 'background 0.1s' }}
                            onMouseEnter={e => { e.currentTarget.style.background = C.borderAlt }}
                            onMouseLeave={e => { e.currentTarget.style.background = 'transparent' }}
                          >
                            {/* Title + summary */}
                            <div style={{ minWidth: 0, paddingRight: '12px' }}>
                              <div style={{ fontSize: '0.78rem', fontWeight: '500', color: C.text, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{r.title}</div>
                              {r.summary_text && <div style={{ fontSize: '0.65rem', color: C.textMuted, marginTop: '2px', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{r.summary_text}</div>}
                            </div>
                            {/* Type badge */}
                            <div>
                              <span style={{ display: 'inline-flex', alignItems: 'center', gap: '4px', background: tc + '18', color: tc, border: `1px solid ${tc}40`, borderRadius: '20px', padding: '2px 8px', fontSize: '0.64rem', fontWeight: '500', whiteSpace: 'nowrap' }}>
                                <span style={{ width: '5px', height: '5px', borderRadius: '50%', background: tc, display: 'inline-block', flexShrink: 0 }}/>
                                {typeLabel(r.task_type)}
                              </span>
                            </div>
                            {/* Dataset */}
                            <div style={{ fontSize: '0.73rem', color: r.dataset_filename ? C.textSec : C.textMuted, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{r.dataset_filename || '—'}</div>
                            {/* Created */}
                            <div style={{ fontSize: '0.72rem', color: C.textMuted }}>{fmtRelTime(r.created_at)}</div>
                            {/* Delete */}
                            <button
                              onClick={e => { e.stopPropagation(); handleDeleteReport(r.id) }}
                              style={{ background: 'transparent', border: 'none', color: C.textMuted, cursor: 'pointer', padding: '4px', borderRadius: '5px', display: 'flex', alignItems: 'center', justifyContent: 'center' }}
                              onMouseEnter={e => { e.currentTarget.style.color = C.danger; e.currentTarget.style.background = C.dangerSoft }}
                              onMouseLeave={e => { e.currentTarget.style.color = C.textMuted; e.currentTarget.style.background = 'transparent' }}
                            >
                              <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><polyline points="3 6 5 6 21 6"/><path d="M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6"/><path d="M10 11v6"/><path d="M14 11v6"/><path d="M9 6V4h6v2"/></svg>
                            </button>
                          </div>
                        )
                      })}
                    </>
                  )}
                </div>
              </>
            })()}

            {/* ── Usage ────────────────────────────────────────────── */}
            {activeNav === 'usage' && <>
              <div style={{ marginBottom: '16px' }}>
                <h2 style={{ margin: '0 0 4px', fontSize: '1.4rem', fontWeight: '700', color: C.text, letterSpacing: '-0.4px' }}>Usage</h2>
                <p style={{ margin: 0, color: C.textMuted, fontSize: '0.75rem' }}>Track task usage, workflow success rates, and system health.</p>
              </div>
              <div style={{ display: 'grid', gridTemplateColumns: 'repeat(4, 1fr)', gap: '14px', marginBottom: '22px' }}>
                {STAT_CARDS.map(({ label, value, accent }) => (
                  <div key={label} style={{ ...S.card, padding: '20px', position: 'relative', overflow: 'hidden' }}>
                    <div style={{ position: 'absolute', top: 0, left: 0, right: 0, height: '2px', background: accent, opacity: 0.7 }} />
                    <div style={{ fontSize: '0.6rem', color: C.textMuted, fontWeight: '500', textTransform: 'uppercase', letterSpacing: '0.07em', marginBottom: '10px' }}>{label}</div>
                    <div style={{ fontSize: '1.25rem', fontWeight: '600', color: accent, letterSpacing: '-0.3px', lineHeight: 1 }}>{usageLoading ? '…' : (statValues[label] ?? value)}</div>
                  </div>
                ))}
              </div>
              {/* Workflow Health */}
              <div style={{ ...S.card, marginBottom: '18px' }}>
                <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: '18px' }}>
                  <h3 style={{ margin: 0, fontSize: '0.75rem', fontWeight: '500', color: C.textSec, letterSpacing: '0.01em' }}>Workflow Health</h3>
                  <button onClick={refreshInsights} style={{ background: 'transparent', border: `1px solid ${C.border}`, borderRadius: '6px', padding: '4px 10px', cursor: 'pointer', color: C.textSec, fontSize: '0.72rem', fontFamily: FONT }}>Refresh</button>
                </div>
                {insightsLoading ? (
                  <div style={{ padding: '24px 0', textAlign: 'center', color: C.textMuted, fontSize: '0.82rem' }}>Loading...</div>
                ) : insights.length === 0 ? (
                  <div style={{ padding: '24px 0', textAlign: 'center', color: C.textMuted, fontSize: '0.82rem', lineHeight: 1.7 }}>
                    No workflow health data yet. Run named workflows at least 3 times to see health scores here.
                  </div>
                ) : (
                  <>
                    <div style={{ display: 'grid', gridTemplateColumns: 'minmax(140px, 1fr) 90px 80px 80px 150px', borderBottom: `1px solid ${C.border}`, paddingBottom: '8px', marginBottom: '4px' }}>
                      {['Workflow', 'Health', 'Success', 'Avg Time', 'Last Run'].map(col => (
                        <div key={col} style={{ padding: '0 8px', fontSize: '0.67rem', color: C.textSec, fontWeight: '600', textTransform: 'uppercase', letterSpacing: '0.07em' }}>{col}</div>
                      ))}
                    </div>
                    {insights.map((wf, idx) => {
                      const hColor = wf.health === 'Healthy' ? C.success : wf.health === 'Critical' ? C.danger : C.warn
                      const hBg    = wf.health === 'Healthy' ? C.successSoft : wf.health === 'Critical' ? C.dangerSoft : C.warnSoft
                      const notLast = idx < insights.length - 1
                      const avgDur = wf.avg_duration_ms == null ? '—'
                        : wf.avg_duration_ms < 1000 ? `${wf.avg_duration_ms}ms`
                        : `${(wf.avg_duration_ms / 1000).toFixed(1)}s`
                      return (
                        <div key={wf.workflow_id}>
                          <div style={{ display: 'grid', gridTemplateColumns: 'minmax(140px, 1fr) 90px 80px 80px 150px', padding: '9px 0', borderBottom: notLast ? `1px solid ${C.border}` : 'none', alignItems: 'center' }}>
                            <div style={{ padding: '0 8px', fontSize: '0.75rem', color: C.textSec, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }} title={wf.workflow_name}>
                              {wf.workflow_name}
                            </div>
                            <div style={{ padding: '0 8px' }}>
                              <div style={S.badge(hColor, hBg)}>
                                <div style={S.dot(hColor)} />
                                {wf.health}
                              </div>
                            </div>
                            <div style={{ padding: '0 8px', fontSize: '0.73rem', color: C.textMuted }}>
                              {Math.round(wf.success_rate * 100)}%
                            </div>
                            <div style={{ padding: '0 8px', fontSize: '0.72rem', color: C.textMuted, fontFamily: MONO }}>
                              {avgDur}
                            </div>
                            <div style={{ padding: '0 8px', fontSize: '0.7rem', color: C.textMuted, whiteSpace: 'nowrap' }}>
                              {wf.last_run ? new Date(wf.last_run).toLocaleString() : '—'}
                            </div>
                          </div>
                          <div style={{ padding: '0 8px 8px', borderBottom: !explainData[`workflow_health:${wf.workflow_id}`] && notLast ? `1px solid ${C.border}` : 'none', display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: '8px' }}>
                            <span style={{ fontSize: '0.7rem', color: wf.health === 'Critical' ? C.danger : wf.health === 'Warning' ? C.warn : C.textMuted }}>
                              {wf.recommendation}
                            </span>
                            <button
                              onClick={() => handleExplain('workflow_health', wf.workflow_id)}
                              disabled={explainLoading.has(`workflow_health:${wf.workflow_id}`)}
                              style={{ flexShrink: 0, background: 'transparent', border: `1px solid ${C.border}`, borderRadius: '5px', padding: '2px 8px', fontSize: '0.65rem', cursor: explainLoading.has(`workflow_health:${wf.workflow_id}`) ? 'not-allowed' : 'pointer', color: C.textMuted, fontFamily: FONT, opacity: explainLoading.has(`workflow_health:${wf.workflow_id}`) ? 0.6 : 1 }}
                            >
                              {explainLoading.has(`workflow_health:${wf.workflow_id}`) ? '…' : explainData[`workflow_health:${wf.workflow_id}`] ? 'Hide' : 'Explain'}
                            </button>
                          </div>
                          {explainData[`workflow_health:${wf.workflow_id}`] && (
                            <div style={{ margin: '2px 0 10px', padding: '10px 12px', background: C.accentSoft, border: `1px solid ${C.accent}25`, borderRadius: '8px', borderBottom: notLast ? `1px solid ${C.border}` : 'none' }}>
                              <div style={{ fontSize: '0.65rem', color: C.accent, fontWeight: '700', textTransform: 'uppercase', letterSpacing: '0.06em', marginBottom: '5px' }}>
                                {explainData[`workflow_health:${wf.workflow_id}`].source === 'ai' ? 'AI Explanation' : 'Explanation'}
                              </div>
                              <p style={{ margin: '0 0 6px', fontSize: '0.75rem', color: C.textSec, lineHeight: 1.6 }}>
                                {explainData[`workflow_health:${wf.workflow_id}`].explanation}
                              </p>
                              {explainData[`workflow_health:${wf.workflow_id}`].recommended_actions?.length > 0 && (
                                <div style={{ display: 'flex', flexDirection: 'column', gap: '3px' }}>
                                  {explainData[`workflow_health:${wf.workflow_id}`].recommended_actions.map((a, i) => (
                                    <div key={i} style={{ fontSize: '0.72rem', color: C.textSec }}>• {a}</div>
                                  ))}
                                </div>
                              )}
                            </div>
                          )}
                        </div>
                      )
                    })}
                  </>
                )}
              </div>

              <div style={S.card}>
                <h3 style={{ margin: '0 0 14px', fontSize: '0.75rem', fontWeight: '500', color: C.textSec, letterSpacing: '0.01em' }}>Recent Usage Events</h3>
                <div style={{ display: 'grid', gridTemplateColumns: '2fr 1fr 200px', borderBottom: `1px solid ${C.border}`, paddingBottom: '8px' }}>
                  {['Event Type', 'Source', 'Time'].map(col => (
                    <div key={col} style={{ padding: '0 10px', fontSize: '0.67rem', color: C.textSec, fontWeight: '600', textTransform: 'uppercase', letterSpacing: '0.07em' }}>{col}</div>
                  ))}
                </div>
                {usageLoading ? (
                  <div style={{ padding: '36px 10px', textAlign: 'center', color: C.textMuted, fontSize: '0.82rem' }}>Loading...</div>
                ) : !usage?.data?.recent_events?.length ? (
                  <div style={{ padding: '36px 10px', textAlign: 'center', color: C.textMuted, fontSize: '0.82rem' }}>No usage events recorded yet.</div>
                ) : usage.data.recent_events.map(ev => (
                  <div key={ev.id} style={{ display: 'grid', gridTemplateColumns: '2fr 1fr 200px', borderBottom: `1px solid ${C.border}`, padding: '10px 0', alignItems: 'center' }}>
                    <div style={{ padding: '0 10px', fontSize: '0.75rem', color: C.textSec }}>{ev.event_type || '—'}</div>
                    <div style={{ padding: '0 10px', fontSize: '0.75rem', color: C.textMuted }}>{ev.source || '—'}</div>
                    <div style={{ padding: '0 10px', fontSize: '0.7rem', color: C.textMuted, whiteSpace: 'nowrap' }}>
                      {ev.created_at ? new Date(ev.created_at).toLocaleString() : '—'}
                    </div>
                  </div>
                ))}
              </div>
            </>}

            {/* ── Settings ─────────────────────────────────────────── */}
            {activeNav === 'settings' && <>
              <div style={{ marginBottom: '16px' }}>
                <h2 style={{ margin: '0 0 4px', fontSize: '1.4rem', fontWeight: '700', color: C.text, letterSpacing: '-0.4px' }}>Settings</h2>
                <p style={{ margin: 0, color: C.textMuted, fontSize: '0.75rem' }}>Local environment configuration and demo account details.</p>
              </div>

              {/* Theme selector */}
              <div style={{ ...S.card, marginBottom: '14px' }}>
                <h3 style={{ margin: '0 0 14px', fontSize: '0.75rem', fontWeight: '500', color: C.textSec, letterSpacing: '0.01em' }}>Appearance</h3>
                <div style={{ display: 'flex', gap: '10px' }}>
                  {[
                    { id: 'light',  label: 'Light',  icon: <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.93 4.93l1.41 1.41M17.66 17.66l1.41 1.41M2 12h2M20 12h2M4.93 19.07l1.41-1.41M17.66 6.34l1.41-1.41"/></svg> },
                    { id: 'dark',   label: 'Dark',   icon: <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z"/></svg> },
                    { id: 'system', label: 'System', icon: <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><rect x="2" y="3" width="20" height="14" rx="2"/><path d="M8 21h8M12 17v4"/></svg> },
                  ].map(({ id, label, icon }) => {
                    const active = theme === id
                    return (
                      <button
                        key={id}
                        onClick={() => setTheme(id)}
                        style={{
                          flex: 1, display: 'flex', flexDirection: 'column', alignItems: 'center', gap: '8px',
                          padding: '14px 10px', borderRadius: '10px', cursor: 'pointer', fontFamily: FONT,
                          border: `1px solid ${active ? C.accent : C.border}`,
                          background: active ? C.accentSoft : C.bg,
                          color: active ? C.accent : C.textSec,
                          transition: 'border-color 0.15s, background 0.15s, color 0.15s',
                        }}
                        onMouseEnter={e => { if (!active) { e.currentTarget.style.borderColor = C.borderAlt; e.currentTarget.style.background = C.borderAlt } }}
                        onMouseLeave={e => { if (!active) { e.currentTarget.style.borderColor = C.border; e.currentTarget.style.background = C.bg } }}
                      >
                        {icon}
                        <span style={{ fontSize: '0.73rem', fontWeight: active ? '600' : '400' }}>{label}</span>
                        {active && <span style={{ width: '5px', height: '5px', borderRadius: '50%', background: C.accent, display: 'block' }} />}
                      </button>
                    )
                  })}
                </div>
                <p style={{ margin: '12px 0 0', fontSize: '0.68rem', color: C.textMuted, lineHeight: 1.5 }}>
                  System follows your OS preference. Saved automatically.
                </p>
              </div>

              <div style={S.card}>
                <h3 style={{ margin: '0 0 14px', fontSize: '0.75rem', fontWeight: '500', color: C.textSec, letterSpacing: '0.01em' }}>Account</h3>
                {[
                  { label: 'Name',      value: user?.name  || '—' },
                  { label: 'Email',     value: user?.email || '—' },
                  { label: 'Role',      value: user?.role  || '—' },
                  { label: 'Workspace', value: 'Default Workspace' },
                ].map(({ label, value }) => (
                  <div key={label} style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', padding: '10px 0', borderBottom: `1px solid ${C.border}` }}>
                    <span style={{ fontSize: '0.75rem', color: C.textMuted }}>{label}</span>
                    <span style={{ fontSize: '0.75rem', color: C.textSec, fontWeight: '400' }}>{value}</span>
                  </div>
                ))}
              </div>
            </>}

            {/* ── Workspaces ───────────────────────────────────────── */}
            {activeNav === 'workspaces' && (
              <Suspense fallback={<LazyFallback />}>
                <WorkspaceHistory
                  workspaces={workspaceList}
                  loading={workspaceListLoading}
                  onReopen={handleReopenWorkspace}
                  runningWorkspaceId={workspaceRunning ? activeWorkspaceId : null}
                  C={C}
                />
              </Suspense>
            )}

            {/* ── Operations Center ─────────────────────────────── */}
            {activeNav === 'operations' && (
              <Suspense fallback={<LazyFallback />}>
                <OperationsCenter
                  history={history}
                  historyLoading={historyLoading}
                  scheduledList={scheduledList}
                  scheduledLoading={scheduledLoading}
                  notifications={notifications}
                  reportList={reportList}
                  workflowList={workflowList}
                  execActionLoading={execActionLoading}
                  schedRunNowLoading={schedRunNowLoading}
                  onRetry={handleRetry}
                  onRerun={handleRerun}
                  onRunNow={handleRunNow}
                  onNavigate={setActiveNav}
                  C={C}
                />
              </Suspense>
            )}

          </div>
        </main>
      </div>
    </div>
  )
}

// ─── Root ──────────────────────────────────────────────────────────────────────
function App() {
  const [token,          setToken]          = useState(() => localStorage.getItem('ts_token') || null)
  const [user,           setUser]           = useState(() => {
    try { return JSON.parse(localStorage.getItem('ts_user')) } catch { return null }
  })
  const [sessionExpired, setSessionExpired] = useState(false)
  const [theme,          setThemeState]     = useState(() => localStorage.getItem('ts_theme') || 'dark')

  function handleThemeChange(t) {
    setThemeState(t)
    localStorage.setItem('ts_theme', t)
  }

  function handleSignIn(newToken, newUser) {
    localStorage.setItem('ts_token', newToken)
    localStorage.setItem('ts_user', JSON.stringify(newUser))
    setToken(newToken)
    setUser(newUser)
    setSessionExpired(false)
  }

  function handleLogout() {
    localStorage.removeItem('ts_token')
    localStorage.removeItem('ts_user')
    setToken(null)
    setUser(null)
  }

  function handleSessionExpired() {
    handleLogout()
    setSessionExpired(true)
  }

  if (!token) {
    return <LoginView onSignIn={handleSignIn} sessionExpired={sessionExpired} />
  }

  if (user?.role === 'admin') {
    return (
      <Suspense fallback={<div style={{ minHeight: '100vh', background: '#09090f', display: 'flex', alignItems: 'center', justifyContent: 'center', color: '#40475e', fontSize: '0.8rem', fontFamily: "system-ui, sans-serif" }}>Loading…</div>}>
        <AdminDashboard token={token} user={user} onLogout={handleLogout} onSessionExpired={handleSessionExpired} theme={theme} setTheme={handleThemeChange} />
      </Suspense>
    )
  }

  return <DashboardView token={token} user={user} onLogout={handleLogout} onSessionExpired={handleSessionExpired} theme={theme} setTheme={handleThemeChange} />
}

export default App
