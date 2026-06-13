import { useState, useEffect, lazy, Suspense } from 'react'
import ChartSection from './ChartSection'
import {
  getMyData, getScheduledWorkflows, getReports, getWorkflows,
  getNotifications, markNotificationRead, deleteNotification,
  retryExecution, rerunExecution, runScheduleNow, getUsage, getDynamicTools,
  createAdminInvite,
  getAdminExportLogs,
  getAdminExportLogSummary,
  getAdminEmailLogs,
  getAdminEmailLogSummary,
} from '../api/client'

const DynamicToolComposer = lazy(() => import('./DynamicToolComposer'))
const OperationsCenter    = lazy(() => import('./OperationsCenter'))

const FONT      = "system-ui, -apple-system, 'Segoe UI', sans-serif"
const SIDEBAR_W = 216
const HEADER_H  = 56

const C_DARK = {
  bg: '#09090f', sidebar: '#0c0d15', surface: '#101320',
  border: '#1b1f35', borderAlt: '#232840',
  accent: '#6366f1', accentSoft: '#6366f11a',
  text: '#eef0f8', textSec: '#8890a8', textMuted: '#40475e',
  success: '#10b981', successSoft: '#10b9811a',
  warn: '#f59e0b', warnSoft: '#f59e0b1a',
  danger: '#f87171', dangerSoft: '#f871711a',
}
const C_LIGHT = {
  bg: '#f8f8fb', sidebar: '#f0f0f6', surface: '#ffffff',
  border: '#e2e2ec', borderAlt: '#d0d0de',
  accent: '#6366f1', accentSoft: '#6366f112',
  text: '#111118', textSec: '#5c5c72', textMuted: '#9898b0',
  success: '#059669', successSoft: '#05966912',
  warn: '#d97706', warnSoft: '#d9770612',
  danger: '#dc2626', dangerSoft: '#dc262612',
}

function makeS(C) {
  return {
    card: { background: C.surface, border: `1px solid ${C.border}`, borderRadius: '10px', padding: '14px 16px' },
    input: { width: '100%', boxSizing: 'border-box', background: C.bg, border: `1px solid ${C.border}`, borderRadius: '8px', color: C.text, fontSize: '0.88rem', padding: '10px 14px', outline: 'none', fontFamily: FONT },
    btnPrimary: { background: C.accent, color: '#fff', border: 'none', borderRadius: '8px', padding: '10px 22px', fontSize: '0.88rem', fontWeight: '600', cursor: 'pointer', fontFamily: FONT },
    label: { display: 'block', fontSize: '0.7rem', color: C.textSec, fontWeight: '600', marginBottom: '7px', letterSpacing: '0.06em', textTransform: 'uppercase' },
    badge: (color, bg) => ({ display: 'inline-flex', alignItems: 'center', gap: '5px', background: bg, color, border: `1px solid ${color}30`, borderRadius: '20px', padding: '3px 10px', fontSize: '0.7rem', fontWeight: '600', letterSpacing: '0.04em', fontFamily: FONT }),
    dot: (color) => ({ width: '5px', height: '5px', borderRadius: '50%', background: color, flexShrink: 0 }),
  }
}

function relTime(iso) {
  if (!iso) return ''
  const s = Math.floor((Date.now() - new Date(iso).getTime()) / 1000)
  if (s < 60) return 'just now'
  const m = Math.floor(s / 60); if (m < 60) return `${m}m ago`
  const h = Math.floor(m / 60); if (h < 24) return `${h}h ago`
  return `${Math.floor(h / 24)}d ago`
}

function fmtDate(iso) {
  if (!iso) return '—'
  return new Date(iso).toLocaleString(undefined, { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' })
}

function LazyFallback() {
  return (
    <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'center', padding: '40px 0', color: '#40475e', fontSize: '0.8rem' }}>
      Loading…
    </div>
  )
}

// ─── Admin nav items ──────────────────────────────────────────────────────────
const ADMIN_NAV = [
  {
    id: 'overview', label: 'Admin Overview',
    icon: <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><rect x="3" y="3" width="7" height="7"/><rect x="14" y="3" width="7" height="7"/><rect x="14" y="14" width="7" height="7"/><rect x="3" y="14" width="7" height="7"/></svg>,
  },
  {
    id: 'tools', label: 'Tool Approvals',
    icon: <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M14.7 6.3a1 1 0 0 0 0 1.4l1.6 1.6a1 1 0 0 0 1.4 0l3.77-3.77a6 6 0 0 1-7.94 7.94l-6.91 6.91a2.12 2.12 0 0 1-3-3l6.91-6.91a6 6 0 0 1 7.94-7.94l-3.76 3.76z"/></svg>,
  },
  {
    id: 'operations', label: 'Ops Center',
    icon: <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><circle cx="12" cy="12" r="2"/><path d="M16.24 7.76a6 6 0 0 1 0 8.49m-8.48-.01a6 6 0 0 1 0-8.49m11.31-2.82a10 10 0 0 1 0 14.14m-14.14 0a10 10 0 0 1 0-14.14"/></svg>,
  },
  {
    id: 'system-health', label: 'System Health',
    icon: <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><polyline points="22 12 18 12 15 21 9 3 6 12 2 12"/></svg>,
  },
  {
    id: 'invites', label: 'Invite Management',
    icon: <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/><line x1="19" y1="8" x2="19" y2="14"/><line x1="22" y1="11" x2="16" y2="11"/></svg>,
  },
  {
    id: 'users', label: 'User Management',
    icon: <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M17 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M23 21v-2a4 4 0 0 0-3-3.87"/><path d="M16 3.13a4 4 0 0 1 0 7.75"/></svg>,
  },
  {
    id: 'audit', label: 'Audit & Governance',
    icon: <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/><path d="m9 12 2 2 4-4"/></svg>,
  },
  {
    id: 'export-activity', label: 'Export Activity',
    icon: <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/></svg>,
  },
  {
    id: 'email-activity', label: 'Email Activity',
    icon: <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M4 4h16c1.1 0 2 .9 2 2v12c0 1.1-.9 2-2 2H4c-1.1 0-2-.9-2-2V6c0-1.1.9-2 2-2z"/><polyline points="22,6 12,13 2,6"/></svg>,
  },
]

// ─── Section heading helper ───────────────────────────────────────────────────
function SectionHeading({ C, title, subtitle }) {
  return (
    <div style={{ marginBottom: '20px' }}>
      <h2 style={{ margin: '0 0 4px', fontSize: '1.3rem', fontWeight: '700', color: C.text, letterSpacing: '-0.4px' }}>{title}</h2>
      {subtitle && <p style={{ margin: 0, color: C.textMuted, fontSize: '0.74rem' }}>{subtitle}</p>}
    </div>
  )
}

// ─── Admin overview panel ─────────────────────────────────────────────────────
function AdminOverviewPanel({ C, history, scheduledList, workflowList, toolList, usage, notifications }) {
  const stats = [
    { label: 'Total Executions', value: history.length, color: C.accent, bg: C.accentSoft },
    { label: 'Active Schedules', value: scheduledList.filter(s => s.enabled).length, color: C.success, bg: C.successSoft },
    { label: 'Saved Workflows', value: workflowList.length, color: C.warn, bg: C.warnSoft },
    { label: 'Registered Tools', value: toolList.length, color: '#8b5cf6', bg: '#8b5cf61a' },
  ]

  const unread = notifications.filter(n => !n.read).length
  const successCount = history.filter(h => h.status === 'success').length
  const successRate = history.length > 0 ? Math.round(successCount / history.length * 100) : null

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: '20px' }}>
      <SectionHeading C={C} title="Admin Overview" subtitle="Platform health, tool governance, and system activity" />

      {unread > 0 && (
        <div style={{ background: C.accentSoft, border: `1px solid ${C.accent}30`, borderRadius: '8px', padding: '10px 14px', fontSize: '0.8rem', color: C.accent, display: 'flex', alignItems: 'center', gap: '8px' }}>
          <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M18 8A6 6 0 0 0 6 8c0 7-3 9-3 9h18s-3-2-3-9"/><path d="M13.73 21a2 2 0 0 1-3.46 0"/></svg>
          {unread} unread notification{unread !== 1 ? 's' : ''}
        </div>
      )}

      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(190px, 1fr))', gap: '12px' }}>
        {stats.map(({ label, value, color, bg }) => (
          <div key={label} style={{ background: C.surface, border: `1px solid ${C.border}`, borderRadius: '10px', padding: '16px 18px' }}>
            <div style={{ fontSize: '0.65rem', fontWeight: '700', color: C.textMuted, letterSpacing: '0.08em', textTransform: 'uppercase', marginBottom: '8px' }}>{label}</div>
            <div style={{ fontSize: '1.6rem', fontWeight: '700', color, letterSpacing: '-0.5px' }}>{value}</div>
          </div>
        ))}
      </div>

      {/* System health summary strip */}
      {history.length > 0 && (
        <div style={{ background: C.surface, border: `1px solid ${C.border}`, borderRadius: '10px', padding: '14px 18px', display: 'flex', alignItems: 'center', gap: '28px', flexWrap: 'wrap' }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: '7px' }}>
            <div style={{ width: '8px', height: '8px', borderRadius: '50%', background: C.success }} />
            <span style={{ fontSize: '0.78rem', color: C.textSec }}>API</span>
            <span style={{ fontSize: '0.78rem', fontWeight: '600', color: C.success }}>Operational</span>
          </div>
          {successRate !== null && (
            <div style={{ display: 'flex', alignItems: 'center', gap: '7px' }}>
              <div style={{ width: '8px', height: '8px', borderRadius: '50%', background: successRate >= 80 ? C.success : successRate >= 50 ? C.warn : C.danger }} />
              <span style={{ fontSize: '0.78rem', color: C.textSec }}>Success rate</span>
              <span style={{ fontSize: '0.78rem', fontWeight: '600', color: C.text }}>{successRate}%</span>
            </div>
          )}
          <div style={{ display: 'flex', alignItems: 'center', gap: '7px' }}>
            <div style={{ width: '8px', height: '8px', borderRadius: '50%', background: scheduledList.length > 0 ? C.success : C.textMuted }} />
            <span style={{ fontSize: '0.78rem', color: C.textSec }}>Schedules</span>
            <span style={{ fontSize: '0.78rem', fontWeight: '600', color: C.text }}>{scheduledList.filter(s => s.enabled).length}/{scheduledList.length} active</span>
          </div>
        </div>
      )}

      <div style={{ background: C.surface, border: `1px solid ${C.border}`, borderRadius: '10px', padding: '16px 18px' }}>
        <div style={{ fontSize: '0.72rem', fontWeight: '700', color: C.textSec, letterSpacing: '0.06em', textTransform: 'uppercase', marginBottom: '12px' }}>Recent Executions</div>
        {history.length === 0 ? (
          <div style={{ fontSize: '0.8rem', color: C.textMuted, textAlign: 'center', padding: '20px 0' }}>No executions yet.</div>
        ) : (
          history.slice(0, 6).map(row => (
            <div key={row.id} style={{ display: 'flex', alignItems: 'center', gap: '10px', padding: '8px 0', borderBottom: `1px solid ${C.border}` }}>
              <div style={{ width: '7px', height: '7px', borderRadius: '50%', background: row.status === 'success' ? C.success : C.danger, flexShrink: 0 }} />
              <div style={{ flex: 1, minWidth: 0 }}>
                <div style={{ fontSize: '0.8rem', color: C.text, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                  {(row.intent || row.task_type || 'Execution').slice(0, 55)}
                </div>
                <div style={{ fontSize: '0.68rem', color: C.textMuted }}>{relTime(row.started_at)}</div>
              </div>
              <div style={{ fontSize: '0.68rem', fontWeight: '600', color: row.status === 'success' ? C.success : C.danger, flexShrink: 0 }}>
                {row.status}
              </div>
            </div>
          ))
        )}
      </div>
    </div>
  )
}

// ─── Invite Management panel ──────────────────────────────────────────────────
function InviteManagementPanel({ C, token, onSessionExpired }) {
  const [email,         setEmail]         = useState('')
  const [loading,       setLoading]       = useState(false)
  const [result,        setResult]        = useState(null)
  const [err,           setErr]           = useState(null)
  const [copied,        setCopied]        = useState(false)
  const [inviteHistory, setInviteHistory] = useState([])

  async function handleCreate(e) {
    e.preventDefault()
    const trimmed = email.trim()
    if (!trimmed) return
    setLoading(true); setErr(null); setResult(null)
    try {
      const res = await createAdminInvite(trimmed, token)
      const invite = res?.data ?? res
      setResult(invite)
      setInviteHistory(prev => [{ ...invite, createdAt: new Date().toISOString() }, ...prev].slice(0, 10))
      setEmail('')
    } catch (ex) {
      if (ex?.message?.startsWith('401')) { onSessionExpired(); return }
      setErr(ex.message || 'Failed to create invite')
    } finally {
      setLoading(false)
    }
  }

  function handleCopy() {
    if (!result?.invite_token) return
    navigator.clipboard.writeText(result.invite_token).then(() => {
      setCopied(true)
      setTimeout(() => setCopied(false), 2000)
    })
  }

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: '20px' }}>
      <SectionHeading C={C} title="Invite Management" subtitle="Create single-use admin invite tokens. Each token expires in 72 hours." />

      {/* Create form */}
      <div style={{ background: C.surface, border: `1px solid ${C.border}`, borderRadius: '10px', padding: '20px' }}>
        <div style={{ fontSize: '0.75rem', fontWeight: '700', color: C.textSec, letterSpacing: '0.06em', textTransform: 'uppercase', marginBottom: '14px' }}>Create Admin Invite</div>
        <form onSubmit={handleCreate} style={{ display: 'flex', gap: '10px', alignItems: 'flex-end', flexWrap: 'wrap' }}>
          <div style={{ flex: 1, minWidth: '220px' }}>
            <label style={{ display: 'block', fontSize: '0.7rem', color: C.textSec, fontWeight: '600', marginBottom: '6px', letterSpacing: '0.05em', textTransform: 'uppercase' }}>
              Recipient Email
            </label>
            <input
              type="email"
              placeholder="admin@example.com"
              value={email}
              onChange={e => setEmail(e.target.value)}
              required
              style={{ width: '100%', boxSizing: 'border-box', background: C.bg, border: `1px solid ${C.border}`, borderRadius: '8px', color: C.text, fontSize: '0.88rem', padding: '10px 14px', outline: 'none', fontFamily: FONT }}
            />
          </div>
          <button
            type="submit"
            disabled={loading || !email.trim()}
            style={{ background: loading ? C.borderAlt : C.accent, color: loading ? C.textMuted : '#fff', border: 'none', borderRadius: '8px', padding: '10px 22px', fontSize: '0.88rem', fontWeight: '600', cursor: loading ? 'not-allowed' : 'pointer', fontFamily: FONT, flexShrink: 0 }}
          >
            {loading ? 'Creating…' : 'Create Invite'}
          </button>
        </form>

        {err && (
          <div style={{ marginTop: '12px', background: C.dangerSoft, border: `1px solid ${C.danger}30`, borderRadius: '8px', padding: '10px 14px', fontSize: '0.8rem', color: C.danger }}>
            {err}
          </div>
        )}

        {result && (
          <div style={{ marginTop: '16px', background: C.successSoft, border: `1px solid ${C.success}30`, borderRadius: '10px', padding: '16px' }}>
            <div style={{ display: 'flex', alignItems: 'center', gap: '8px', marginBottom: '10px' }}>
              <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke={C.success} strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round"><polyline points="20 6 9 17 4 12"/></svg>
              <span style={{ fontSize: '0.8rem', fontWeight: '600', color: C.success }}>Invite created for {result.email}</span>
            </div>
            <div style={{ fontSize: '0.7rem', color: C.textSec, marginBottom: '6px', letterSpacing: '0.05em', textTransform: 'uppercase', fontWeight: '600' }}>Invite Token (copy once — not stored)</div>
            <div style={{ display: 'flex', gap: '8px', alignItems: 'center' }}>
              <code style={{ flex: 1, minWidth: 0, display: 'block', background: C.bg, border: `1px solid ${C.border}`, borderRadius: '7px', padding: '9px 12px', fontSize: '0.78rem', color: C.text, fontFamily: 'monospace', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', wordBreak: 'break-all' }}>
                {result.invite_token}
              </code>
              <button
                onClick={handleCopy}
                style={{ flexShrink: 0, background: copied ? C.successSoft : C.accentSoft, border: `1px solid ${copied ? C.success : C.accent}30`, borderRadius: '7px', padding: '9px 14px', fontSize: '0.78rem', fontWeight: '600', color: copied ? C.success : C.accent, cursor: 'pointer', fontFamily: FONT }}
              >
                {copied ? 'Copied!' : 'Copy'}
              </button>
            </div>
            <div style={{ marginTop: '8px', fontSize: '0.72rem', color: C.textSec }}>
              Expires: <strong style={{ color: C.text }}>{fmtDate(result.expires_at)}</strong> · Single-use · Hash stored (token not persisted)
            </div>
          </div>
        )}
      </div>

      {/* Info box */}
      <div style={{ background: C.warnSoft, border: `1px solid ${C.warn}25`, borderRadius: '8px', padding: '12px 16px', display: 'flex', gap: '10px' }}>
        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke={C.warn} strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" style={{ flexShrink: 0, marginTop: '1px' }}><circle cx="12" cy="12" r="10"/><line x1="12" y1="8" x2="12" y2="12"/><line x1="12" y1="16" x2="12.01" y2="16"/></svg>
        <div style={{ fontSize: '0.78rem', color: C.textSec, lineHeight: 1.55 }}>
          Tokens expire in <strong style={{ color: C.text }}>72 hours</strong> and are <strong style={{ color: C.text }}>single-use</strong>. Only the SHA-256 hash is stored — the raw token shown here is the only copy. Share it securely. Recipients register via <code style={{ fontFamily: 'monospace', fontSize: '0.74rem' }}>/v1/auth/register-admin</code>.
        </div>
      </div>

      {/* Session history */}
      {inviteHistory.length > 0 && (
        <div style={{ background: C.surface, border: `1px solid ${C.border}`, borderRadius: '10px', padding: '16px 18px' }}>
          <div style={{ fontSize: '0.72rem', fontWeight: '700', color: C.textSec, letterSpacing: '0.06em', textTransform: 'uppercase', marginBottom: '12px' }}>This Session — Invites Created</div>
          {inviteHistory.map((inv, i) => (
            <div key={i} style={{ display: 'flex', alignItems: 'center', gap: '10px', padding: '8px 0', borderBottom: `1px solid ${C.border}` }}>
              <div style={{ width: '7px', height: '7px', borderRadius: '50%', background: C.success, flexShrink: 0 }} />
              <div style={{ flex: 1, minWidth: 0 }}>
                <div style={{ fontSize: '0.8rem', color: C.text }}>{inv.email}</div>
                <div style={{ fontSize: '0.68rem', color: C.textMuted }}>Expires {fmtDate(inv.expires_at)}</div>
              </div>
              <div style={{ fontSize: '0.68rem', color: C.textMuted, flexShrink: 0 }}>{relTime(inv.createdAt)}</div>
            </div>
          ))}
        </div>
      )}
    </div>
  )
}

// ─── Audit & Governance panel ─────────────────────────────────────────────────
function AuditGovernancePanel({ C, history, toolList, reportList }) {
  const approved   = toolList.filter(t => t.approved || t.status === 'approved')
  const pending    = toolList.filter(t => !t.approved && t.status !== 'approved')
  const approvalRate = toolList.length > 0 ? Math.round(approved.length / toolList.length * 100) : null

  const completedReports = reportList.filter(r => r.status === 'completed' || !r.status)
  const complianceRate = reportList.length > 0 ? Math.round(completedReports.length / reportList.length * 100) : null

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: '20px' }}>
      <SectionHeading C={C} title="Audit & Governance" subtitle="Execution audit log, tool governance, and compliance overview" />

      {/* Governance stats row */}
      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(180px, 1fr))', gap: '12px' }}>
        {[
          { label: 'Tools Approved', value: approved.length, color: C.success, bg: C.successSoft },
          { label: 'Pending Approval', value: pending.length, color: C.warn, bg: C.warnSoft },
          { label: 'Approval Rate', value: approvalRate !== null ? `${approvalRate}%` : '—', color: C.accent, bg: C.accentSoft },
          { label: 'Report Completion', value: complianceRate !== null ? `${complianceRate}%` : '—', color: '#8b5cf6', bg: '#8b5cf61a' },
        ].map(({ label, value, color, bg }) => (
          <div key={label} style={{ background: C.surface, border: `1px solid ${C.border}`, borderRadius: '10px', padding: '14px 16px' }}>
            <div style={{ fontSize: '0.64rem', fontWeight: '700', color: C.textMuted, letterSpacing: '0.08em', textTransform: 'uppercase', marginBottom: '7px' }}>{label}</div>
            <div style={{ fontSize: '1.45rem', fontWeight: '700', color, letterSpacing: '-0.4px' }}>{value}</div>
          </div>
        ))}
      </div>

      {/* Audit log */}
      <div style={{ background: C.surface, border: `1px solid ${C.border}`, borderRadius: '10px', padding: '16px 18px' }}>
        <div style={{ fontSize: '0.72rem', fontWeight: '700', color: C.textSec, letterSpacing: '0.06em', textTransform: 'uppercase', marginBottom: '12px' }}>Audit Log — Recent Executions</div>
        {history.length === 0 ? (
          <div style={{ fontSize: '0.8rem', color: C.textMuted, textAlign: 'center', padding: '24px 0' }}>No execution records available.</div>
        ) : (
          history.slice(0, 20).map(row => (
            <div key={row.id} style={{ display: 'flex', alignItems: 'flex-start', gap: '10px', padding: '8px 0', borderBottom: `1px solid ${C.border}` }}>
              <div style={{ width: '7px', height: '7px', borderRadius: '50%', background: row.status === 'success' ? C.success : C.danger, flexShrink: 0, marginTop: '5px' }} />
              <div style={{ flex: 1, minWidth: 0 }}>
                <div style={{ fontSize: '0.79rem', color: C.text, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                  {(row.intent || row.task_type || `Execution #${row.id}`).slice(0, 70)}
                </div>
                <div style={{ fontSize: '0.67rem', color: C.textMuted, marginTop: '1px' }}>{fmtDate(row.started_at)}</div>
              </div>
              <span style={{ fontSize: '0.68rem', fontWeight: '600', color: row.status === 'success' ? C.success : C.danger, flexShrink: 0 }}>
                {row.status}
              </span>
            </div>
          ))
        )}
      </div>

      {/* AI governance — approval history */}
      <div style={{ background: C.surface, border: `1px solid ${C.border}`, borderRadius: '10px', padding: '16px 18px' }}>
        <div style={{ fontSize: '0.72rem', fontWeight: '700', color: C.textSec, letterSpacing: '0.06em', textTransform: 'uppercase', marginBottom: '12px' }}>AI Governance — Approved Tools</div>
        {approved.length === 0 ? (
          <div style={{ fontSize: '0.8rem', color: C.textMuted, textAlign: 'center', padding: '20px 0' }}>No approved tools yet. Use Tool Approvals to review pending tools.</div>
        ) : (
          approved.slice(0, 10).map(tool => (
            <div key={tool.id} style={{ display: 'flex', alignItems: 'center', gap: '10px', padding: '8px 0', borderBottom: `1px solid ${C.border}` }}>
              <div style={{ width: '7px', height: '7px', borderRadius: '50%', background: C.success, flexShrink: 0 }} />
              <div style={{ flex: 1, minWidth: 0 }}>
                <div style={{ fontSize: '0.8rem', color: C.text, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{tool.name}</div>
                {tool.description && <div style={{ fontSize: '0.67rem', color: C.textMuted, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{tool.description}</div>}
              </div>
              <span style={{ fontSize: '0.68rem', fontWeight: '600', color: C.success, flexShrink: 0 }}>approved</span>
            </div>
          ))
        )}
        {pending.length > 0 && (
          <div style={{ marginTop: '10px', fontSize: '0.75rem', color: C.warn, display: 'flex', alignItems: 'center', gap: '6px' }}>
            <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round"><circle cx="12" cy="12" r="10"/><line x1="12" y1="8" x2="12" y2="12"/><line x1="12" y1="16" x2="12.01" y2="16"/></svg>
            {pending.length} tool{pending.length !== 1 ? 's' : ''} pending review
          </div>
        )}
      </div>

      {/* Compliance status */}
      <div style={{ background: C.surface, border: `1px solid ${C.border}`, borderRadius: '10px', padding: '16px 18px' }}>
        <div style={{ fontSize: '0.72rem', fontWeight: '700', color: C.textSec, letterSpacing: '0.06em', textTransform: 'uppercase', marginBottom: '12px' }}>Compliance Status</div>
        <div style={{ display: 'flex', flexDirection: 'column', gap: '10px' }}>
          {[
            { label: 'Total reports generated', value: reportList.length },
            { label: 'Reports completed', value: completedReports.length },
            { label: 'Completion rate', value: complianceRate !== null ? `${complianceRate}%` : '—' },
            { label: 'Tools with approval', value: `${approved.length} / ${toolList.length}` },
          ].map(({ label, value }) => (
            <div key={label} style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', fontSize: '0.8rem' }}>
              <span style={{ color: C.textSec }}>{label}</span>
              <span style={{ fontWeight: '600', color: C.text }}>{value}</span>
            </div>
          ))}
        </div>
      </div>
    </div>
  )
}

// ─── System Health panel ──────────────────────────────────────────────────────
function SystemHealthPanel({ C, history, scheduledList, reportList }) {
  const total       = history.length
  const successCount = history.filter(h => h.status === 'success').length
  const failedCount  = history.filter(h => h.status !== 'success').length
  const successRate  = total > 0 ? Math.round(successCount / total * 100) : null

  const activeSchedules = scheduledList.filter(s => s.enabled).length
  const totalSchedules  = scheduledList.length

  const staleMs    = 7 * 24 * 60 * 60 * 1000
  const staleCount = reportList.filter(r => r.created_at && (Date.now() - new Date(r.created_at).getTime()) > staleMs).length

  const metrics = [
    {
      label: 'API Status',
      value: 'Operational',
      sub: 'All endpoints responding',
      color: C.success,
      dot: C.success,
    },
    {
      label: 'Execution Success Rate',
      value: successRate !== null ? `${successRate}%` : '—',
      sub: total > 0 ? `${successCount} success / ${failedCount} failed` : 'No executions recorded',
      color: successRate === null ? C.textMuted : successRate >= 80 ? C.success : successRate >= 50 ? C.warn : C.danger,
      dot: successRate === null ? C.textMuted : successRate >= 80 ? C.success : successRate >= 50 ? C.warn : C.danger,
    },
    {
      label: 'Scheduler Status',
      value: totalSchedules === 0 ? 'No schedules' : `${activeSchedules}/${totalSchedules} active`,
      sub: totalSchedules === 0 ? 'No workflows scheduled yet' : activeSchedules === totalSchedules ? 'All schedules running' : `${totalSchedules - activeSchedules} paused`,
      color: totalSchedules === 0 ? C.textMuted : activeSchedules > 0 ? C.success : C.warn,
      dot: totalSchedules === 0 ? C.textMuted : activeSchedules > 0 ? C.success : C.warn,
    },
    {
      label: 'Failed Executions',
      value: failedCount,
      sub: failedCount === 0 ? 'No failures recorded' : `${failedCount} execution${failedCount !== 1 ? 's' : ''} did not succeed`,
      color: failedCount === 0 ? C.success : failedCount < 5 ? C.warn : C.danger,
      dot: failedCount === 0 ? C.success : failedCount < 5 ? C.warn : C.danger,
    },
    {
      label: 'Stale Reports (>7d)',
      value: staleCount,
      sub: staleCount === 0 ? 'All reports are recent' : `${staleCount} report${staleCount !== 1 ? 's' : ''} older than 7 days`,
      color: staleCount === 0 ? C.success : C.warn,
      dot: staleCount === 0 ? C.success : C.warn,
    },
  ]

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: '20px' }}>
      <SectionHeading C={C} title="System Health" subtitle="Real-time status indicators derived from platform activity" />

      <div style={{ display: 'flex', flexDirection: 'column', gap: '10px' }}>
        {metrics.map(({ label, value, sub, color, dot }) => (
          <div key={label} style={{ background: C.surface, border: `1px solid ${C.border}`, borderRadius: '10px', padding: '16px 20px', display: 'flex', alignItems: 'center', gap: '16px' }}>
            <div style={{ width: '10px', height: '10px', borderRadius: '50%', background: dot, flexShrink: 0 }} />
            <div style={{ flex: 1 }}>
              <div style={{ fontSize: '0.75rem', fontWeight: '600', color: C.textSec, marginBottom: '2px' }}>{label}</div>
              <div style={{ fontSize: '0.78rem', color: C.textMuted }}>{sub}</div>
            </div>
            <div style={{ fontSize: '1.05rem', fontWeight: '700', color, flexShrink: 0, letterSpacing: '-0.2px' }}>{value}</div>
          </div>
        ))}
      </div>

      {/* Snapshot summary */}
      <div style={{ background: C.bg, border: `1px solid ${C.border}`, borderRadius: '10px', padding: '14px 18px' }}>
        <div style={{ fontSize: '0.7rem', color: C.textMuted }}>
          Metrics are computed from execution history and live schedule state. Refresh the page to update.
        </div>
      </div>
    </div>
  )
}

// ─── User Management placeholder panel ───────────────────────────────────────
function UserManagementPanel({ C }) {
  const sections = [
    {
      title: 'User Accounts',
      description: 'Browse registered users, view account details, suspend or reactivate accounts, and audit login history.',
      icon: <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round"><path d="M17 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/></svg>,
    },
    {
      title: 'Role Management',
      description: 'Assign and revoke roles (admin / user). Promote users via invite tokens or direct role assignment.',
      icon: <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round"><path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/></svg>,
    },
    {
      title: 'RBAC Configuration',
      description: 'Define permission sets per role, configure resource-level access policies, and audit permission changes.',
      icon: <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round"><rect x="3" y="11" width="18" height="11" rx="2" ry="2"/><path d="M7 11V7a5 5 0 0 1 10 0v4"/></svg>,
    },
  ]

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: '20px' }}>
      <SectionHeading C={C} title="User Management" subtitle="User accounts, role assignments, and access control configuration" />

      <div style={{ background: C.warnSoft, border: `1px solid ${C.warn}25`, borderRadius: '8px', padding: '12px 16px', display: 'flex', gap: '10px', alignItems: 'flex-start' }}>
        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke={C.warn} strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" style={{ flexShrink: 0, marginTop: '2px' }}><circle cx="12" cy="12" r="10"/><line x1="12" y1="8" x2="12" y2="12"/><line x1="12" y1="16" x2="12.01" y2="16"/></svg>
        <div style={{ fontSize: '0.78rem', color: C.textSec, lineHeight: 1.55 }}>
          <strong style={{ color: C.text }}>Coming next.</strong> Admin invites are available now via the Invite Management panel. Full user management UI is planned for the next release.
        </div>
      </div>

      <div style={{ display: 'flex', flexDirection: 'column', gap: '12px' }}>
        {sections.map(({ title, description, icon }) => (
          <div key={title} style={{ background: C.surface, border: `1px solid ${C.border}`, borderRadius: '10px', padding: '18px 20px', display: 'flex', gap: '16px', alignItems: 'flex-start', opacity: 0.65 }}>
            <div style={{ width: '40px', height: '40px', borderRadius: '10px', background: C.accentSoft, border: `1px solid ${C.accent}20`, display: 'flex', alignItems: 'center', justifyContent: 'center', color: C.accent, flexShrink: 0 }}>
              {icon}
            </div>
            <div style={{ flex: 1 }}>
              <div style={{ fontSize: '0.88rem', fontWeight: '600', color: C.text, marginBottom: '5px', display: 'flex', alignItems: 'center', gap: '8px' }}>
                {title}
                <span style={{ display: 'inline-flex', alignItems: 'center', background: C.borderAlt, border: `1px solid ${C.border}`, borderRadius: '20px', padding: '1px 9px', fontSize: '0.63rem', fontWeight: '700', color: C.textMuted, letterSpacing: '0.06em', textTransform: 'uppercase' }}>
                  Coming Next
                </span>
              </div>
              <div style={{ fontSize: '0.78rem', color: C.textSec, lineHeight: 1.55 }}>{description}</div>
            </div>
          </div>
        ))}
      </div>
    </div>
  )
}

// ─── Export Activity panel ────────────────────────────────────────────────────
function ExportActivityPanel({ C, token, onSessionExpired }) {
  const PAGE_SIZE = 25
  const S = makeS(C)

  const [summary,        setSummary]        = useState(null)
  const [summaryLoading, setSummaryLoading] = useState(true)
  const [summaryErr,     setSummaryErr]     = useState(null)
  const [logs,           setLogs]           = useState([])
  const [logsLoading,    setLogsLoading]    = useState(true)
  const [logsErr,        setLogsErr]        = useState(null)
  const [filterFormat,   setFilterFormat]   = useState('')
  const [filterStatus,   setFilterStatus]   = useState('')
  const [page,           setPage]           = useState(0)
  const [total,          setTotal]          = useState(0)

  const is401 = err => err?.message?.startsWith('401')

  useEffect(() => {
    let cancelled = false

    setSummaryLoading(true)
    setSummaryErr(null)
    getAdminExportLogSummary(token)
      .then(res => { if (!cancelled) setSummary(res?.data ?? null) })
      .catch(err => {
        if (is401(err)) { onSessionExpired(); return }
        if (!cancelled) setSummaryErr(err.message || 'Failed to load summary')
      })
      .finally(() => { if (!cancelled) setSummaryLoading(false) })

    setLogsLoading(true)
    setLogsErr(null)
    const params = { limit: PAGE_SIZE, offset: page * PAGE_SIZE }
    if (filterFormat) params.export_format = filterFormat
    if (filterStatus) params.status = filterStatus
    getAdminExportLogs(token, params)
      .then(res => {
        if (!cancelled) {
          setLogs(res?.data ?? [])
          setTotal(res?.total ?? 0)
        }
      })
      .catch(err => {
        if (is401(err)) { onSessionExpired(); return }
        if (!cancelled) setLogsErr(err.message || 'Failed to load export logs')
      })
      .finally(() => { if (!cancelled) setLogsLoading(false) })

    return () => { cancelled = true }
  }, [token, filterFormat, filterStatus, page]) // eslint-disable-line react-hooks/exhaustive-deps

  function handleFormatChange(e) { setFilterFormat(e.target.value); setPage(0) }
  function handleStatusChange(e) { setFilterStatus(e.target.value); setPage(0) }

  const byFormat  = summary?.exports_by_format ?? {}
  const hasChart  = Object.keys(byFormat).length > 0
  const chartData = {
    chart_type: 'bar',
    title: 'Exports by Format',
    labels: Object.keys(byFormat),
    series: [{ name: 'Exports', data: Object.values(byFormat) }],
  }

  const mostUsed = Object.keys(byFormat).length > 0
    ? Object.entries(byFormat).sort((a, b) => b[1] - a[1])[0][0].toUpperCase()
    : '—'

  const from    = total === 0 ? 0 : page * PAGE_SIZE + 1
  const to      = Math.min((page + 1) * PAGE_SIZE, total)
  const hasPrev = page > 0
  const hasNext = to < total

  function fmtSize(bytes) {
    if (bytes == null) return '—'
    if (bytes < 1024) return `${bytes} B`
    if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`
    return `${(bytes / (1024 * 1024)).toFixed(1)} MB`
  }

  const FORMAT_COLORS = {
    pdf:  { color: '#ef4444', bg: '#ef44441a' },
    xlsx: { color: '#10b981', bg: '#10b9811a' },
    csv:  { color: '#f59e0b', bg: '#f59e0b1a' },
    json: { color: '#6366f1', bg: '#6366f11a' },
  }
  const fmtBadge  = fmt => { const f = (fmt || '').toLowerCase(); const c = FORMAT_COLORS[f] ?? { color: C.textSec, bg: C.borderAlt }; return S.badge(c.color, c.bg) }
  const statBadge = st  => st === 'success' ? S.badge(C.success, C.successSoft) : S.badge(C.danger, C.dangerSoft)

  const kpis = [
    { label: 'Total Exports',    value: summaryLoading ? '…' : (summary?.total_exports   ?? '—'), color: C.accent  },
    { label: 'Success Rate',     value: summaryLoading ? '…' : (summary ? `${summary.success_rate}%` : '—'), color: C.success },
    { label: 'Failed Exports',   value: summaryLoading ? '…' : (summary?.failed_exports  ?? '—'), color: C.danger  },
    { label: 'Most Used Format', value: summaryLoading ? '…' : mostUsed,                          color: '#8b5cf6' },
  ]

  const selStyle = {
    background: C.bg, border: `1px solid ${C.border}`, borderRadius: '8px',
    color: C.text, fontSize: '0.82rem', padding: '8px 12px',
    outline: 'none', fontFamily: FONT, cursor: 'pointer',
  }

  const COLS = '1.8fr 1fr 0.7fr 1.8fr 0.65fr 0.7fr'

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: '20px' }}>
      <SectionHeading C={C} title="Export Activity" subtitle="Admin view of all user export events — format distribution, volume, and governance" />

      {summaryErr && (
        <div style={{ background: C.dangerSoft, border: `1px solid ${C.danger}30`, borderRadius: '8px', padding: '10px 14px', fontSize: '0.8rem', color: C.danger }}>
          {summaryErr}
        </div>
      )}

      {/* KPI cards */}
      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(190px, 1fr))', gap: '12px' }}>
        {kpis.map(({ label, value, color }) => (
          <div key={label} style={{ background: C.surface, border: `1px solid ${C.border}`, borderRadius: '10px', padding: '16px 18px' }}>
            <div style={{ fontSize: '0.65rem', fontWeight: '700', color: C.textMuted, letterSpacing: '0.08em', textTransform: 'uppercase', marginBottom: '8px' }}>{label}</div>
            <div style={{ fontSize: '1.6rem', fontWeight: '700', color, letterSpacing: '-0.5px' }}>{value}</div>
          </div>
        ))}
      </div>

      {/* Format distribution chart */}
      {!summaryLoading && hasChart && (
        <div style={{ background: C.surface, border: `1px solid ${C.border}`, borderRadius: '10px', padding: '16px 18px' }}>
          <div style={{ fontSize: '0.72rem', fontWeight: '700', color: C.textSec, letterSpacing: '0.06em', textTransform: 'uppercase', marginBottom: '14px' }}>Export Volume by Format</div>
          <ChartSection chart={chartData} C={C} />
        </div>
      )}

      {/* Filters */}
      <div style={{ display: 'flex', gap: '14px', flexWrap: 'wrap', alignItems: 'flex-end' }}>
        <div>
          <div style={{ fontSize: '0.67rem', fontWeight: '700', color: C.textMuted, letterSpacing: '0.06em', textTransform: 'uppercase', marginBottom: '5px' }}>Format</div>
          <select value={filterFormat} onChange={handleFormatChange} style={selStyle}>
            <option value="">All</option>
            <option value="pdf">PDF</option>
            <option value="xlsx">XLSX</option>
            <option value="csv">CSV</option>
            <option value="json">JSON</option>
          </select>
        </div>
        <div>
          <div style={{ fontSize: '0.67rem', fontWeight: '700', color: C.textMuted, letterSpacing: '0.06em', textTransform: 'uppercase', marginBottom: '5px' }}>Status</div>
          <select value={filterStatus} onChange={handleStatusChange} style={selStyle}>
            <option value="">All</option>
            <option value="success">Success</option>
            <option value="failed">Failed</option>
          </select>
        </div>
      </div>

      {/* Logs table */}
      <div style={{ background: C.surface, border: `1px solid ${C.border}`, borderRadius: '10px', padding: '16px 18px' }}>
        {/* Header row */}
        <div style={{ display: 'grid', gridTemplateColumns: COLS, gap: '8px', paddingBottom: '8px', borderBottom: `1px solid ${C.border}` }}>
          {['Timestamp', 'User', 'Format', 'Filename', 'Size', 'Status'].map(col => (
            <div key={col} style={{ fontSize: '0.64rem', fontWeight: '700', color: C.textMuted, letterSpacing: '0.07em', textTransform: 'uppercase' }}>{col}</div>
          ))}
        </div>

        {logsErr && (
          <div style={{ marginTop: '12px', background: C.dangerSoft, border: `1px solid ${C.danger}30`, borderRadius: '8px', padding: '10px 14px', fontSize: '0.8rem', color: C.danger }}>
            {logsErr}
          </div>
        )}

        {logsLoading && !logsErr && (
          <div style={{ textAlign: 'center', padding: '28px 0', fontSize: '0.8rem', color: C.textMuted }}>Loading export logs…</div>
        )}

        {!logsLoading && !logsErr && logs.length === 0 && (
          <div style={{ textAlign: 'center', padding: '28px 0', fontSize: '0.8rem', color: C.textMuted }}>No export records match the current filters.</div>
        )}

        {!logsLoading && !logsErr && logs.map(row => (
          <div key={row.id} style={{ display: 'grid', gridTemplateColumns: COLS, gap: '8px', padding: '9px 0', borderBottom: `1px solid ${C.border}`, alignItems: 'center' }}>
            <div style={{ fontSize: '0.75rem', color: C.textSec }}>{fmtDate(row.created_at)}</div>
            <div style={{ fontSize: '0.75rem', color: C.text, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }} title={row.user_id ?? undefined}>
              {(row.user_id || '—').slice(0, 18)}{(row.user_id?.length ?? 0) > 18 ? '…' : ''}
            </div>
            <div><span style={fmtBadge(row.export_format)}>{(row.export_format || '?').toUpperCase()}</span></div>
            <div style={{ fontSize: '0.75rem', color: C.textSec, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }} title={row.filename ?? undefined}>
              {row.filename ?? '—'}
            </div>
            <div style={{ fontSize: '0.75rem', color: C.textSec }}>{fmtSize(row.file_size_bytes)}</div>
            <div><span style={statBadge(row.status)}>{row.status || '?'}</span></div>
          </div>
        ))}
      </div>

      {/* Pagination */}
      {total > 0 && (
        <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', flexWrap: 'wrap', gap: '10px' }}>
          <span style={{ fontSize: '0.78rem', color: C.textSec }}>Showing {from}–{to} of {total.toLocaleString()}</span>
          <div style={{ display: 'flex', gap: '8px' }}>
            <button
              onClick={() => setPage(p => p - 1)}
              disabled={!hasPrev}
              style={{ background: C.surface, border: `1px solid ${C.border}`, borderRadius: '7px', padding: '6px 14px', fontSize: '0.78rem', color: hasPrev ? C.text : C.textMuted, cursor: hasPrev ? 'pointer' : 'not-allowed', fontFamily: FONT }}
            >
              Previous
            </button>
            <button
              onClick={() => setPage(p => p + 1)}
              disabled={!hasNext}
              style={{ background: C.surface, border: `1px solid ${C.border}`, borderRadius: '7px', padding: '6px 14px', fontSize: '0.78rem', color: hasNext ? C.text : C.textMuted, cursor: hasNext ? 'pointer' : 'not-allowed', fontFamily: FONT }}
            >
              Next
            </button>
          </div>
        </div>
      )}
    </div>
  )
}

// ─── Email Activity panel ─────────────────────────────────────────────────────
function EmailActivityPanel({ C, token, onSessionExpired }) {
  const PAGE_SIZE = 25
  const S = makeS(C)

  const [summary,        setSummary]        = useState(null)
  const [summaryLoading, setSummaryLoading] = useState(true)
  const [summaryErr,     setSummaryErr]     = useState(null)
  const [logs,           setLogs]           = useState([])
  const [logsLoading,    setLogsLoading]    = useState(true)
  const [logsErr,        setLogsErr]        = useState(null)
  const [filterType,     setFilterType]     = useState('')
  const [filterStatus,   setFilterStatus]   = useState('')
  const [page,           setPage]           = useState(0)
  const [total,          setTotal]          = useState(0)

  const is401 = err => err?.message?.startsWith('401')

  useEffect(() => {
    let cancelled = false

    setSummaryLoading(true)
    setSummaryErr(null)
    getAdminEmailLogSummary(token)
      .then(res => { if (!cancelled) setSummary(res?.data ?? null) })
      .catch(err => {
        if (is401(err)) { onSessionExpired(); return }
        if (!cancelled) setSummaryErr(err.message || 'Failed to load summary')
      })
      .finally(() => { if (!cancelled) setSummaryLoading(false) })

    setLogsLoading(true)
    setLogsErr(null)
    const params = { limit: PAGE_SIZE, offset: page * PAGE_SIZE }
    if (filterType) params.email_type = filterType
    if (filterStatus) params.status = filterStatus
    getAdminEmailLogs(token, params)
      .then(res => {
        if (!cancelled) {
          setLogs(res?.data ?? [])
          setTotal(res?.total ?? 0)
        }
      })
      .catch(err => {
        if (is401(err)) { onSessionExpired(); return }
        if (!cancelled) setLogsErr(err.message || 'Failed to load email logs')
      })
      .finally(() => { if (!cancelled) setLogsLoading(false) })

    return () => { cancelled = true }
  }, [token, filterType, filterStatus, page]) // eslint-disable-line react-hooks/exhaustive-deps

  function handleTypeChange(e)   { setFilterType(e.target.value);   setPage(0) }
  function handleStatusChange(e) { setFilterStatus(e.target.value); setPage(0) }

  const byType   = summary?.emails_by_type ?? {}
  const hasChart = Object.keys(byType).length > 0
  const chartData = {
    chart_type: 'bar',
    title: 'Emails by Type',
    labels: Object.keys(byType),
    series: [{ name: 'Emails', data: Object.values(byType) }],
  }

  const from    = total === 0 ? 0 : page * PAGE_SIZE + 1
  const to      = Math.min((page + 1) * PAGE_SIZE, total)
  const hasPrev = page > 0
  const hasNext = to < total

  const TYPE_COLORS = {
    report:       { color: C.accent,  bg: C.accentSoft  },
    verification: { color: C.success, bg: C.successSoft },
  }
  const STATUS_COLORS = {
    sent:      { color: C.success, bg: C.successSoft },
    failed:    { color: C.danger,  bg: C.dangerSoft  },
    simulated: { color: C.accent,  bg: C.accentSoft  },
    pending:   { color: C.warn,    bg: C.warnSoft    },
  }
  const typeBadge = t  => { const c = TYPE_COLORS[t]    ?? { color: C.textSec, bg: C.borderAlt }; return S.badge(c.color, c.bg) }
  const statBadge = st => { const c = STATUS_COLORS[st] ?? { color: C.textSec, bg: C.borderAlt }; return S.badge(c.color, c.bg) }

  const kpis = [
    { label: 'Total Emails',     value: summaryLoading ? '…' : (summary?.total_emails     ?? '—'), color: C.accent  },
    { label: 'Success Rate',     value: summaryLoading ? '…' : (summary ? `${summary.success_rate}%` : '—'), color: C.success },
    { label: 'Failed Emails',    value: summaryLoading ? '…' : (summary?.failed_emails    ?? '—'), color: C.danger  },
    { label: 'Simulated Emails', value: summaryLoading ? '…' : (summary?.simulated_emails ?? '—'), color: C.warn    },
  ]

  const selStyle = {
    background: C.bg, border: `1px solid ${C.border}`, borderRadius: '8px',
    color: C.text, fontSize: '0.82rem', padding: '8px 12px',
    outline: 'none', fontFamily: FONT, cursor: 'pointer',
  }

  const COLS = '1.5fr 1fr 0.7fr 1.8fr 2fr 0.75fr 0.7fr'

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: '20px' }}>
      <SectionHeading C={C} title="Email Activity" subtitle="Admin view of all email delivery events — type distribution, status, and governance" />

      {summaryErr && (
        <div style={{ background: C.dangerSoft, border: `1px solid ${C.danger}30`, borderRadius: '8px', padding: '10px 14px', fontSize: '0.8rem', color: C.danger }}>
          {summaryErr}
        </div>
      )}

      {/* KPI cards */}
      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(190px, 1fr))', gap: '12px' }}>
        {kpis.map(({ label, value, color }) => (
          <div key={label} style={{ background: C.surface, border: `1px solid ${C.border}`, borderRadius: '10px', padding: '16px 18px' }}>
            <div style={{ fontSize: '0.65rem', fontWeight: '700', color: C.textMuted, letterSpacing: '0.08em', textTransform: 'uppercase', marginBottom: '8px' }}>{label}</div>
            <div style={{ fontSize: '1.6rem', fontWeight: '700', color, letterSpacing: '-0.5px' }}>{value}</div>
          </div>
        ))}
      </div>

      {/* Type distribution chart */}
      {!summaryLoading && hasChart && (
        <div style={{ background: C.surface, border: `1px solid ${C.border}`, borderRadius: '10px', padding: '16px 18px' }}>
          <div style={{ fontSize: '0.72rem', fontWeight: '700', color: C.textSec, letterSpacing: '0.06em', textTransform: 'uppercase', marginBottom: '14px' }}>Email Volume by Type</div>
          <ChartSection chart={chartData} C={C} />
        </div>
      )}

      {/* Filters */}
      <div style={{ display: 'flex', gap: '14px', flexWrap: 'wrap', alignItems: 'flex-end' }}>
        <div>
          <div style={{ fontSize: '0.67rem', fontWeight: '700', color: C.textMuted, letterSpacing: '0.06em', textTransform: 'uppercase', marginBottom: '5px' }}>Email Type</div>
          <select value={filterType} onChange={handleTypeChange} style={selStyle}>
            <option value="">All</option>
            <option value="report">Report</option>
            <option value="verification">Verification</option>
          </select>
        </div>
        <div>
          <div style={{ fontSize: '0.67rem', fontWeight: '700', color: C.textMuted, letterSpacing: '0.06em', textTransform: 'uppercase', marginBottom: '5px' }}>Status</div>
          <select value={filterStatus} onChange={handleStatusChange} style={selStyle}>
            <option value="">All</option>
            <option value="sent">Sent</option>
            <option value="simulated">Simulated</option>
            <option value="failed">Failed</option>
            <option value="pending">Pending</option>
          </select>
        </div>
      </div>

      {/* Logs table */}
      <div style={{ background: C.surface, border: `1px solid ${C.border}`, borderRadius: '10px', padding: '16px 18px' }}>
        {/* Header row */}
        <div style={{ display: 'grid', gridTemplateColumns: COLS, gap: '8px', paddingBottom: '8px', borderBottom: `1px solid ${C.border}` }}>
          {['Timestamp', 'User', 'Report ID', 'Recipient', 'Subject', 'Type', 'Status'].map(col => (
            <div key={col} style={{ fontSize: '0.64rem', fontWeight: '700', color: C.textMuted, letterSpacing: '0.07em', textTransform: 'uppercase' }}>{col}</div>
          ))}
        </div>

        {logsErr && (
          <div style={{ marginTop: '12px', background: C.dangerSoft, border: `1px solid ${C.danger}30`, borderRadius: '8px', padding: '10px 14px', fontSize: '0.8rem', color: C.danger }}>
            {logsErr}
          </div>
        )}

        {logsLoading && !logsErr && (
          <div style={{ textAlign: 'center', padding: '28px 0', fontSize: '0.8rem', color: C.textMuted }}>Loading email logs…</div>
        )}

        {!logsLoading && !logsErr && logs.length === 0 && (
          <div style={{ textAlign: 'center', padding: '28px 0', fontSize: '0.8rem', color: C.textMuted }}>No email records match the current filters.</div>
        )}

        {!logsLoading && !logsErr && logs.map(row => (
          <div key={row.id} style={{ display: 'grid', gridTemplateColumns: COLS, gap: '8px', padding: '9px 0', borderBottom: `1px solid ${C.border}`, alignItems: 'center' }}>
            <div style={{ fontSize: '0.75rem', color: C.textSec }}>{fmtDate(row.created_at)}</div>
            <div style={{ fontSize: '0.75rem', color: C.text, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }} title={row.user_id ?? undefined}>
              {(row.user_id || '—').slice(0, 18)}{(row.user_id?.length ?? 0) > 18 ? '…' : ''}
            </div>
            <div style={{ fontSize: '0.75rem', color: C.textSec }}>
              {row.report_id != null ? `#${row.report_id}` : '—'}
            </div>
            <div style={{ fontSize: '0.75rem', color: C.textSec, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }} title={row.recipient_email ?? undefined}>
              {row.recipient_email ?? '—'}
            </div>
            <div style={{ fontSize: '0.75rem', color: C.textSec, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }} title={row.subject ?? undefined}>
              {row.subject ?? '—'}
            </div>
            <div><span style={typeBadge(row.email_type)}>{row.email_type || '?'}</span></div>
            <div><span style={statBadge(row.status)}>{row.status || '?'}</span></div>
          </div>
        ))}
      </div>

      {/* Pagination */}
      {total > 0 && (
        <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', flexWrap: 'wrap', gap: '10px' }}>
          <span style={{ fontSize: '0.78rem', color: C.textSec }}>Showing {from}–{to} of {total.toLocaleString()}</span>
          <div style={{ display: 'flex', gap: '8px' }}>
            <button
              onClick={() => setPage(p => p - 1)}
              disabled={!hasPrev}
              style={{ background: C.surface, border: `1px solid ${C.border}`, borderRadius: '7px', padding: '6px 14px', fontSize: '0.78rem', color: hasPrev ? C.text : C.textMuted, cursor: hasPrev ? 'pointer' : 'not-allowed', fontFamily: FONT }}
            >
              Previous
            </button>
            <button
              onClick={() => setPage(p => p + 1)}
              disabled={!hasNext}
              style={{ background: C.surface, border: `1px solid ${C.border}`, borderRadius: '7px', padding: '6px 14px', fontSize: '0.78rem', color: hasNext ? C.text : C.textMuted, cursor: hasNext ? 'pointer' : 'not-allowed', fontFamily: FONT }}
            >
              Next
            </button>
          </div>
        </div>
      )}
    </div>
  )
}

// ─── Notification dropdown ────────────────────────────────────────────────────
function NotifDropdown({ C, notifications, loading, onMarkRead, onDelete }) {
  const unread = notifications.filter(n => !n.read).length
  return (
    <div style={{ position: 'absolute', top: '44px', right: 0, width: '310px', maxHeight: '380px', overflowY: 'auto', background: C.surface, border: `1px solid ${C.border}`, borderRadius: '10px', zIndex: 200, boxShadow: '0 8px 32px rgba(0,0,0,0.28)' }}>
      <div style={{ padding: '10px 14px 8px', borderBottom: `1px solid ${C.border}`, display: 'flex', alignItems: 'center', justifyContent: 'space-between', position: 'sticky', top: 0, background: C.surface }}>
        <span style={{ fontSize: '0.8rem', fontWeight: '600', color: C.text }}>Notifications</span>
        {unread > 0 && <span style={{ fontSize: '0.68rem', color: C.textMuted, background: C.borderAlt, borderRadius: '10px', padding: '1px 7px' }}>{unread} unread</span>}
      </div>
      {loading ? (
        <div style={{ padding: '24px', textAlign: 'center', fontSize: '0.8rem', color: C.textMuted }}>Loading…</div>
      ) : notifications.length === 0 ? (
        <div style={{ padding: '28px 20px', textAlign: 'center', fontSize: '0.82rem', color: C.textMuted }}>No notifications.</div>
      ) : notifications.map(n => (
        <div key={n.id} style={{ padding: '9px 14px', borderBottom: `1px solid ${C.border}`, background: n.read ? 'transparent' : C.accentSoft }}>
          <div style={{ display: 'flex', alignItems: 'flex-start', justifyContent: 'space-between', gap: '8px', marginBottom: '2px' }}>
            <span style={{ fontSize: '0.8rem', fontWeight: n.read ? '400' : '600', color: C.text, flex: 1, lineHeight: 1.35 }}>{n.title}</span>
            <div style={{ display: 'flex', gap: '2px', flexShrink: 0, marginTop: '1px' }}>
              {!n.read && <button onClick={() => onMarkRead(n.id)} style={{ background: 'none', border: 'none', cursor: 'pointer', color: C.textMuted, fontSize: '0.72rem', padding: '1px 3px', fontFamily: FONT }}>✓</button>}
              <button onClick={() => onDelete(n.id)} style={{ background: 'none', border: 'none', cursor: 'pointer', color: C.textMuted, fontSize: '0.9rem', padding: '0 3px', lineHeight: 1 }}>×</button>
            </div>
          </div>
          <div style={{ fontSize: '0.73rem', color: C.textSec, lineHeight: 1.4, marginBottom: '2px' }}>{n.message}</div>
          <div style={{ fontSize: '0.66rem', color: C.textMuted }}>{relTime(n.created_at)}</div>
        </div>
      ))}
    </div>
  )
}

// ─── AdminDashboard ───────────────────────────────────────────────────────────
export default function AdminDashboard({ token, user, onLogout, onSessionExpired, theme, setTheme }) {
  const resolvedTheme = theme === 'system'
    ? (window.matchMedia?.('(prefers-color-scheme: dark)').matches ? 'dark' : 'light')
    : (theme || 'dark')
  const C = resolvedTheme === 'light' ? C_LIGHT : C_DARK
  const S = makeS(C)

  const [activeNav,          setActiveNav]          = useState('overview')
  const [notifications,      setNotifications]      = useState([])
  const [notifLoading,       setNotifLoading]       = useState(false)
  const [notifOpen,          setNotifOpen]          = useState(false)
  const [history,            setHistory]            = useState([])
  const [historyLoading,     setHistoryLoading]     = useState(false)
  const [scheduledList,      setScheduledList]      = useState([])
  const [scheduledLoading,   setScheduledLoading]   = useState(false)
  const [reportList,         setReportList]         = useState([])
  const [workflowList,       setWorkflowList]       = useState([])
  const [toolList,           setToolList]           = useState([])
  const [usage,              setUsage]              = useState(null)
  const [execActionLoading,  setExecActionLoading]  = useState(new Set())
  const [schedRunNowLoading, setSchedRunNowLoading] = useState(new Set())

  const is401 = err => err?.message?.startsWith('401')

  useEffect(() => {
    setHistoryLoading(true)
    getMyData(token)
      .then(d => setHistory(d?.data?.execution_history ?? []))
      .catch(err => { if (is401(err)) onSessionExpired() })
      .finally(() => setHistoryLoading(false))

    setScheduledLoading(true)
    getScheduledWorkflows(token)
      .then(d => setScheduledList(d?.data ?? []))
      .catch(err => { if (is401(err)) onSessionExpired() })
      .finally(() => setScheduledLoading(false))

    getReports(token)
      .then(d => setReportList(d?.data ?? []))
      .catch(err => { if (is401(err)) onSessionExpired() })

    getWorkflows(token)
      .then(d => setWorkflowList(d?.data ?? []))
      .catch(err => { if (is401(err)) onSessionExpired() })

    setNotifLoading(true)
    getNotifications(token)
      .then(d => setNotifications(d?.data ?? []))
      .catch(err => { if (is401(err)) onSessionExpired() })
      .finally(() => setNotifLoading(false))

    getUsage(token)
      .then(d => setUsage(d))
      .catch(() => {})

    getDynamicTools(token)
      .then(d => setToolList(d?.data ?? []))
      .catch(() => {})
  }, [token]) // eslint-disable-line react-hooks/exhaustive-deps

  async function handleMarkNotifRead(id) {
    try {
      await markNotificationRead(id, token)
      setNotifications(prev => prev.map(n => n.id === id ? { ...n, read: 1 } : n))
    } catch (err) { if (is401(err)) onSessionExpired() }
  }

  async function handleDeleteNotif(id) {
    try {
      await deleteNotification(id, token)
      setNotifications(prev => prev.filter(n => n.id !== id))
    } catch (err) { if (is401(err)) onSessionExpired() }
  }

  async function handleRetry(id) {
    setExecActionLoading(s => new Set(s).add(id))
    try {
      await retryExecution(id, token)
      const d = await getMyData(token)
      setHistory(d?.data?.execution_history ?? [])
    } catch (err) { if (is401(err)) onSessionExpired() }
    finally { setExecActionLoading(s => { const n = new Set(s); n.delete(id); return n }) }
  }

  async function handleRerun(id) {
    setExecActionLoading(s => new Set(s).add(id))
    try {
      await rerunExecution(id, token)
      const d = await getMyData(token)
      setHistory(d?.data?.execution_history ?? [])
    } catch (err) { if (is401(err)) onSessionExpired() }
    finally { setExecActionLoading(s => { const n = new Set(s); n.delete(id); return n }) }
  }

  async function handleRunNow(id) {
    setSchedRunNowLoading(s => new Set(s).add(id))
    try {
      await runScheduleNow(id, token)
      const d = await getScheduledWorkflows(token)
      setScheduledList(d?.data ?? [])
    } catch (err) { if (is401(err)) onSessionExpired() }
    finally { setSchedRunNowLoading(s => { const n = new Set(s); n.delete(id); return n }) }
  }

  const unreadCount = notifications.filter(n => !n.read).length

  return (
    <div style={{ minHeight: '100vh', background: C.bg, fontFamily: FONT, color: C.text, display: 'flex' }}>
      <style>{`
        .adm-nav-btn { transition: background 0.12s, color 0.12s; }
        .adm-icon-btn { transition: background 0.12s; }
      `}</style>

      {/* ─── Sidebar ─────────────────────────────────────────────────────── */}
      <aside style={{ position: 'fixed', top: 0, left: 0, width: `${SIDEBAR_W}px`, height: '100vh', background: C.sidebar, borderRight: `1px solid ${C.border}`, display: 'flex', flexDirection: 'column', zIndex: 100 }}>
        {/* Logo */}
        <div style={{ height: `${HEADER_H}px`, display: 'flex', alignItems: 'center', gap: '10px', padding: '0 18px', borderBottom: `1px solid ${C.border}`, flexShrink: 0 }}>
          <img src="/toolsmith-logo-transparent.png" alt="ToolSmithAI" style={{ width: '38px', height: '38px', objectFit: 'contain', flexShrink: 0 }} />
          <span style={{ fontWeight: '700', fontSize: '0.92rem', letterSpacing: '-0.2px' }}>ToolSmithAI</span>
        </div>

        {/* Admin badge */}
        <div style={{ padding: '10px 12px 2px', flexShrink: 0 }}>
          <div style={{ background: '#6366f115', border: '1px solid #6366f135', borderRadius: '6px', padding: '4px 0', fontSize: '0.63rem', fontWeight: '700', color: '#6366f1', letterSpacing: '0.09em', textTransform: 'uppercase', textAlign: 'center' }}>
            Admin Console
          </div>
        </div>

        {/* Nav */}
        <nav style={{ flex: 1, padding: '8px 10px', display: 'flex', flexDirection: 'column', gap: '2px', overflowY: 'auto' }}>
          {ADMIN_NAV.map(({ id, label, icon }) => {
            const active = activeNav === id
            return (
              <button key={id} className="adm-nav-btn" onClick={() => setActiveNav(id)} style={{
                display: 'flex', alignItems: 'center', gap: '10px',
                width: '100%', textAlign: 'left',
                padding: '9px 12px', borderRadius: '8px', border: 'none',
                background: active ? C.accentSoft : 'transparent',
                color: active ? C.accent : C.textSec,
                fontSize: '0.855rem', fontWeight: active ? '600' : '400',
                cursor: 'pointer', fontFamily: FONT, letterSpacing: '0.01em',
              }}
              onMouseEnter={e => { if (!active) { e.currentTarget.style.background = C.borderAlt; e.currentTarget.style.color = C.text } }}
              onMouseLeave={e => { if (!active) { e.currentTarget.style.background = 'transparent'; e.currentTarget.style.color = C.textSec } }}>
                <span style={{ flexShrink: 0, display: 'flex', opacity: active ? 1 : 0.7 }}>{icon}</span>
                {label}
              </button>
            )
          })}
        </nav>

        {/* User card + logout */}
        <div style={{ padding: '10px 10px', borderTop: `1px solid ${C.border}`, flexShrink: 0 }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: '9px', padding: '7px 8px', borderRadius: '8px' }}>
            <div style={{ width: '30px', height: '30px', borderRadius: '50%', background: 'linear-gradient(135deg, #6366f1, #8b5cf6)', display: 'flex', alignItems: 'center', justifyContent: 'center', fontSize: '0.78rem', fontWeight: '700', color: '#fff', flexShrink: 0 }}>
              {(user?.name || user?.email || 'A')[0].toUpperCase()}
            </div>
            <div style={{ flex: 1, minWidth: 0 }}>
              <div style={{ fontSize: '0.78rem', fontWeight: '600', color: C.text, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{user?.name || 'Admin'}</div>
              <div style={{ fontSize: '0.64rem', color: C.textMuted, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{user?.email}</div>
            </div>
          </div>
          <button onClick={onLogout} style={{ width: '100%', marginTop: '5px', background: 'transparent', border: `1px solid ${C.border}`, borderRadius: '7px', padding: '7px 12px', fontSize: '0.78rem', color: C.textSec, cursor: 'pointer', fontFamily: FONT, fontWeight: '500' }}
            onMouseEnter={e => e.currentTarget.style.background = C.borderAlt}
            onMouseLeave={e => e.currentTarget.style.background = 'transparent'}>
            Sign out
          </button>
        </div>
      </aside>

      {/* ─── Main area ───────────────────────────────────────────────────── */}
      <div style={{ marginLeft: `${SIDEBAR_W}px`, flex: 1, display: 'flex', flexDirection: 'column', minHeight: '100vh' }}>

        {/* Header */}
        <header style={{ position: 'sticky', top: 0, height: `${HEADER_H}px`, background: C.bg, borderBottom: `1px solid ${C.border}`, display: 'flex', alignItems: 'center', justifyContent: 'space-between', padding: '0 28px', zIndex: 50, flexShrink: 0 }}>
          <div style={{ fontSize: '0.95rem', fontWeight: '700', color: C.text, letterSpacing: '-0.2px' }}>
            {ADMIN_NAV.find(n => n.id === activeNav)?.label ?? ''}
          </div>

          <div style={{ display: 'flex', alignItems: 'center', gap: '2px' }}>
            {/* Notifications bell */}
            <div style={{ position: 'relative' }}>
              {notifOpen && <div style={{ position: 'fixed', inset: 0, zIndex: 199 }} onClick={() => setNotifOpen(false)} />}
              <div
                onClick={() => setNotifOpen(o => !o)}
                className="adm-icon-btn"
                style={{ position: 'relative', width: '36px', height: '36px', display: 'flex', alignItems: 'center', justifyContent: 'center', borderRadius: '8px', cursor: 'pointer', color: notifOpen ? C.accent : C.textSec, background: notifOpen ? C.accentSoft : 'transparent' }}
                onMouseEnter={e => { if (!notifOpen) e.currentTarget.style.background = C.borderAlt }}
                onMouseLeave={e => { if (!notifOpen) e.currentTarget.style.background = 'transparent' }}
              >
                <svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.9" strokeLinecap="round" strokeLinejoin="round"><path d="M18 8A6 6 0 0 0 6 8c0 7-3 9-3 9h18s-3-2-3-9"/><path d="M13.73 21a2 2 0 0 1-3.46 0"/></svg>
                {unreadCount > 0 && <div style={{ position: 'absolute', top: '7px', right: '7px', width: '7px', height: '7px', borderRadius: '50%', background: C.danger, border: `1.5px solid ${C.bg}` }} />}
              </div>
              {notifOpen && (
                <NotifDropdown
                  C={C}
                  notifications={notifications}
                  loading={notifLoading}
                  onMarkRead={handleMarkNotifRead}
                  onDelete={handleDeleteNotif}
                />
              )}
            </div>

            {/* Theme toggle */}
            <div
              title={resolvedTheme === 'dark' ? 'Switch to light mode' : 'Switch to dark mode'}
              onClick={() => setTheme(resolvedTheme === 'dark' ? 'light' : 'dark')}
              className="adm-icon-btn"
              style={{ width: '36px', height: '36px', borderRadius: '8px', display: 'flex', alignItems: 'center', justifyContent: 'center', cursor: 'pointer', color: C.textSec }}
              onMouseEnter={e => e.currentTarget.style.background = C.borderAlt}
              onMouseLeave={e => e.currentTarget.style.background = 'transparent'}
            >
              {resolvedTheme === 'dark' ? (
                <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.9" strokeLinecap="round" strokeLinejoin="round"><circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.93 4.93l1.41 1.41M17.66 17.66l1.41 1.41M2 12h2M20 12h2M4.93 19.07l1.41-1.41M17.66 6.34l1.41-1.41"/></svg>
              ) : (
                <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.9" strokeLinecap="round" strokeLinejoin="round"><path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z"/></svg>
              )}
            </div>
          </div>
        </header>

        {/* Content */}
        <main style={{ flex: 1, padding: '28px', overflowY: 'auto' }}>
          {activeNav === 'overview' && (
            <AdminOverviewPanel
              C={C}
              history={history}
              scheduledList={scheduledList}
              workflowList={workflowList}
              toolList={toolList}
              usage={usage}
              notifications={notifications}
            />
          )}

          {activeNav === 'tools' && (
            <Suspense fallback={<LazyFallback />}>
              <DynamicToolComposer C={C} S={S} token={token} onSessionExpired={onSessionExpired} />
            </Suspense>
          )}

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

          {activeNav === 'system-health' && (
            <SystemHealthPanel
              C={C}
              history={history}
              scheduledList={scheduledList}
              reportList={reportList}
            />
          )}

          {activeNav === 'invites' && (
            <InviteManagementPanel
              C={C}
              token={token}
              onSessionExpired={onSessionExpired}
            />
          )}

          {activeNav === 'users' && (
            <UserManagementPanel C={C} />
          )}

          {activeNav === 'audit' && (
            <AuditGovernancePanel
              C={C}
              history={history}
              toolList={toolList}
              reportList={reportList}
            />
          )}

          {activeNav === 'export-activity' && (
            <ExportActivityPanel
              C={C}
              token={token}
              onSessionExpired={onSessionExpired}
            />
          )}

          {activeNav === 'email-activity' && (
            <EmailActivityPanel
              C={C}
              token={token}
              onSessionExpired={onSessionExpired}
            />
          )}
        </main>
      </div>
    </div>
  )
}
