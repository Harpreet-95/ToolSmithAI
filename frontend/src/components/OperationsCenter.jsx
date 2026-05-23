import { useState } from 'react'

const FONT = "system-ui, -apple-system, 'Segoe UI', sans-serif"
const MONO = "'Cascadia Code', 'Fira Code', 'JetBrains Mono', monospace"

// ─── Helpers ──────────────────────────────────────────────────────────────────
function relTime(ts) {
  if (!ts) return '—'
  const diff = Date.now() - new Date(ts).getTime()
  const m = Math.floor(diff / 60000)
  if (m < 1) return 'just now'
  if (m < 60) return `${m}m ago`
  const h = Math.floor(m / 60)
  if (h < 24) return `${h}h ago`
  const d = Math.floor(h / 24)
  if (d < 30) return `${d}d ago`
  return new Date(ts).toLocaleDateString()
}

function fmtDur(ms) {
  if (ms == null) return '—'
  if (ms < 1000) return `${ms}ms`
  if (ms < 60000) return `${(ms / 1000).toFixed(1)}s`
  return `${Math.floor(ms / 60000)}m ${Math.round((ms % 60000) / 1000)}s`
}

function isSuccess(row) {
  const s = (row.status || '').toLowerCase()
  return s === 'success' || s === 'completed'
}

function isFailed(row) {
  const s = (row.status || '').toLowerCase()
  return s === 'failed' || s === 'error'
}

function healthStatus(rate) {
  if (rate == null) return 'healthy'
  if (rate >= 90) return 'healthy'
  if (rate >= 60) return 'warning'
  return 'failed'
}

// ─── Status Pill ──────────────────────────────────────────────────────────────
const STATUS_MAP = {
  healthy: { label: 'Healthy', color: '#10b981', bg: '#10b9811a' },
  warning: { label: 'Warning', color: '#f59e0b', bg: '#f59e0b1a' },
  failed:  { label: 'Failed',  color: '#f87171', bg: '#f871711a' },
}

function StatusPill({ status }) {
  const s = STATUS_MAP[status] || STATUS_MAP.healthy
  return (
    <span style={{
      display: 'inline-flex', alignItems: 'center', gap: '4px',
      fontSize: '0.62rem', fontWeight: '700', letterSpacing: '0.04em',
      padding: '2px 7px', borderRadius: '4px',
      background: s.bg, color: s.color,
    }}>
      <span style={{ width: '5px', height: '5px', borderRadius: '50%', background: s.color, display: 'inline-block' }} />
      {s.label}
    </span>
  )
}

// ─── Health Summary Bar ───────────────────────────────────────────────────────
function HealthSummary({ history, C }) {
  const total = history.length
  const succeeded = history.filter(isSuccess).length
  const failed = history.filter(isFailed).length
  const durations = history.filter(h => h.duration_ms != null).map(h => h.duration_ms)
  const avgDur = durations.length > 0
    ? Math.round(durations.reduce((a, b) => a + b, 0) / durations.length)
    : null
  const rate = total > 0 ? Math.round((succeeded / total) * 100) : null
  const hs = healthStatus(rate)

  const stats = [
    {
      label: 'Total Runs', value: total,
      sub: 'all time', color: C.text,
    },
    {
      label: 'Succeeded', value: succeeded,
      sub: rate != null ? `${rate}% success rate` : 'no data',
      color: '#10b981',
    },
    {
      label: 'Failed', value: failed,
      sub: failed > 0 ? 'needs attention' : 'all clear',
      color: failed > 0 ? '#f87171' : '#10b981',
    },
    {
      label: 'Avg Duration', value: avgDur != null ? fmtDur(avgDur) : '—',
      sub: 'per execution', color: C.textSec,
    },
  ]

  return (
    <div>
      <div style={{ display: 'flex', alignItems: 'center', gap: '10px', marginBottom: '8px' }}>
        <StatusPill status={hs} />
        <span style={{ fontSize: '0.68rem', color: C.textMuted }}>
          Overall system health · {total} executions on record
        </span>
      </div>
      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(4, 1fr)', gap: '10px', marginBottom: '14px' }}>
        {stats.map(s => (
          <div key={s.label} style={{
            background: C.surface, border: `1px solid ${C.border}`,
            borderRadius: '10px', padding: '14px 16px',
          }}>
            <div style={{ fontSize: '0.63rem', color: C.textMuted, fontWeight: '600', letterSpacing: '0.07em', textTransform: 'uppercase', marginBottom: '6px' }}>
              {s.label}
            </div>
            <div style={{ fontSize: '1.55rem', fontWeight: '700', color: s.color, lineHeight: 1, marginBottom: '4px' }}>
              {s.value}
            </div>
            <div style={{ fontSize: '0.65rem', color: C.textMuted }}>{s.sub}</div>
          </div>
        ))}
      </div>
    </div>
  )
}

// ─── Throughput Chart (7-day SVG) ─────────────────────────────────────────────
function ThroughputChart({ history, C }) {
  const DAYS = 7
  const now = Date.now()
  const windowStart = now - DAYS * 86400000

  const buckets = Array.from({ length: DAYS }, (_, i) => {
    const ts = now - (DAYS - 1 - i) * 86400000
    return {
      label: new Date(ts).toLocaleDateString('en', { weekday: 'short' }),
      success: 0,
      failed: 0,
      other: 0,
    }
  })

  history.forEach(row => {
    const ts = new Date(row.started_at || row.finished_at || '').getTime()
    if (!ts || ts < windowStart) return
    const idx = Math.min(DAYS - 1, Math.floor((ts - windowStart) / 86400000))
    if (idx < 0) return
    if (isSuccess(row)) buckets[idx].success++
    else if (isFailed(row)) buckets[idx].failed++
    else buckets[idx].other++
  })

  const maxVal = Math.max(...buckets.map(b => b.success + b.failed + b.other), 1)
  const BAR_H = 60
  const BAR_W = 30
  const GAP = 12
  const totalW = DAYS * (BAR_W + GAP)

  return (
    <div style={{ background: C.surface, border: `1px solid ${C.border}`, borderRadius: '10px', padding: '14px 16px', marginBottom: '14px' }}>
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: '12px' }}>
        <div>
          <div style={{ fontSize: '0.72rem', fontWeight: '600', color: C.text }}>Execution Throughput</div>
          <div style={{ fontSize: '0.65rem', color: C.textMuted }}>Last 7 days · daily breakdown</div>
        </div>
        <div style={{ display: 'flex', gap: '14px' }}>
          {[{ label: 'Success', color: '#10b981' }, { label: 'Failed', color: '#f87171' }].map(l => (
            <span key={l.label} style={{ display: 'flex', alignItems: 'center', gap: '5px', fontSize: '0.65rem', color: C.textMuted }}>
              <span style={{ width: '8px', height: '8px', borderRadius: '2px', background: l.color, display: 'inline-block' }} />
              {l.label}
            </span>
          ))}
        </div>
      </div>
      <svg width="100%" height={BAR_H + 20} viewBox={`0 0 ${totalW} ${BAR_H + 20}`} preserveAspectRatio="xMidYMid meet" style={{ display: 'block' }}>
        {buckets.map((b, i) => {
          const x = i * (BAR_W + GAP)
          const total = b.success + b.failed + b.other
          const sH = Math.round((b.success / maxVal) * BAR_H)
          const fH = Math.round((b.failed / maxVal) * BAR_H)
          const topH = sH + fH

          return (
            <g key={i}>
              {/* Base bar (success — green) */}
              {sH > 0 && (
                <rect x={x} y={BAR_H - sH} width={BAR_W} height={sH} rx="3" fill="#10b98144" />
              )}
              {/* Failure overlay (red, sits on top) */}
              {fH > 0 && (
                <rect x={x} y={BAR_H - topH} width={BAR_W} height={fH} rx="3" fill="#f8717166" />
              )}
              {/* Empty state indicator */}
              {total === 0 && (
                <rect x={x} y={BAR_H - 2} width={BAR_W} height={2} rx="1" fill={C.border} />
              )}
              {/* Count label */}
              {total > 0 && (
                <text x={x + BAR_W / 2} y={BAR_H - topH - 3} textAnchor="middle" fontSize="8" fill={C.textMuted} fontFamily={FONT}>
                  {total}
                </text>
              )}
              {/* Day label */}
              <text x={x + BAR_W / 2} y={BAR_H + 14} textAnchor="middle" fontSize="8" fill={C.textMuted} fontFamily={FONT}>
                {b.label}
              </text>
            </g>
          )
        })}
      </svg>
    </div>
  )
}

// ─── Workflow Health Panel ────────────────────────────────────────────────────
function WorkflowHealthPanel({ workflowList, history, C }) {
  const statsById = {}
  history.forEach(row => {
    const wid = row.workflow_id
    if (!wid) return
    if (!statsById[wid]) statsById[wid] = { success: 0, failed: 0, last: null, durations: [] }
    const e = statsById[wid]
    if (isSuccess(row)) e.success++
    else if (isFailed(row)) e.failed++
    if (row.started_at && (!e.last || row.started_at > e.last)) e.last = row.started_at
    if (row.duration_ms != null) e.durations.push(row.duration_ms)
  })

  const rows = workflowList
    .map(wf => ({ ...wf, _stats: statsById[wf.id] }))
    .filter(wf => wf._stats)
    .slice(0, 6)

  if (rows.length === 0) return null

  return (
    <div style={{ background: C.surface, border: `1px solid ${C.border}`, borderRadius: '10px', padding: '14px 16px', marginBottom: '14px' }}>
      <div style={{ fontSize: '0.72rem', fontWeight: '600', color: C.text, marginBottom: '3px' }}>Workflow Health</div>
      <div style={{ fontSize: '0.65rem', color: C.textMuted, marginBottom: '12px' }}>Per-workflow execution summary</div>
      <div style={{ display: 'flex', flexDirection: 'column', gap: '6px' }}>
        {rows.map(wf => {
          const s = wf._stats
          const total = s.success + s.failed
          const rate = total > 0 ? Math.round((s.success / total) * 100) : 100
          const avgDur = s.durations.length > 0
            ? Math.round(s.durations.reduce((a, b) => a + b, 0) / s.durations.length)
            : null
          const hs = healthStatus(total > 0 ? rate : null)

          return (
            <div key={wf.id} style={{ display: 'flex', alignItems: 'center', gap: '10px', padding: '7px 10px', background: C.bg, border: `1px solid ${C.border}`, borderRadius: '7px' }}>
              <StatusPill status={hs} />
              <div style={{ flex: 1, minWidth: 0, fontSize: '0.74rem', fontWeight: '500', color: C.text, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                {wf.name}
              </div>
              <div style={{ display: 'flex', gap: '14px', flexShrink: 0 }}>
                <span style={{ fontSize: '0.65rem', color: '#10b981' }}>{s.success}✓</span>
                <span style={{ fontSize: '0.65rem', color: s.failed > 0 ? '#f87171' : C.textMuted }}>{s.failed}✗</span>
                {avgDur != null && <span style={{ fontSize: '0.65rem', color: C.textMuted }}>{fmtDur(avgDur)}</span>}
                <span style={{ fontSize: '0.65rem', color: C.textMuted }}>{relTime(s.last)}</span>
              </div>
            </div>
          )
        })}
      </div>
    </div>
  )
}

// ─── Failure Inbox ────────────────────────────────────────────────────────────
function FailureInbox({ history, execActionLoading, onRetry, onRerun, C }) {
  const failures = history.filter(isFailed).slice(0, 8)

  return (
    <div style={{ background: C.surface, border: `1px solid ${C.border}`, borderRadius: '10px', padding: '14px 16px', marginBottom: '12px' }}>
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: '12px' }}>
        <div>
          <div style={{ fontSize: '0.72rem', fontWeight: '600', color: C.text }}>Failure Inbox</div>
          <div style={{ fontSize: '0.65rem', color: C.textMuted }}>
            {failures.length} recent failure{failures.length !== 1 ? 's' : ''} · most recent first
          </div>
        </div>
        {failures.length > 0 && <StatusPill status="failed" />}
      </div>

      {failures.length === 0 ? (
        <div style={{ textAlign: 'center', padding: '28px 0', color: C.textMuted }}>
          <div style={{ fontSize: '1.6rem', marginBottom: '6px', color: '#10b981' }}>✓</div>
          <div style={{ fontSize: '0.8rem', fontWeight: '500', color: '#10b981', marginBottom: '2px' }}>No failures</div>
          <div style={{ fontSize: '0.68rem', color: C.textMuted }}>All recent executions succeeded.</div>
        </div>
      ) : (
        <div style={{ display: 'flex', flexDirection: 'column', gap: '8px' }}>
          {failures.map(row => {
            const busy = execActionLoading.has(row.id)
            return (
              <div key={row.id || row.plan_id} style={{ background: C.bg, border: `1px solid #f8717118`, borderRadius: '8px', padding: '10px 12px' }}>
                <div style={{ display: 'flex', alignItems: 'flex-start', gap: '8px', marginBottom: row.error_message ? '6px' : '0' }}>
                  <span style={{ width: '6px', height: '6px', borderRadius: '50%', background: '#f87171', marginTop: '5px', flexShrink: 0 }} />
                  <div style={{ flex: 1, minWidth: 0 }}>
                    <div style={{ fontSize: '0.78rem', fontWeight: '600', color: C.text, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                      {row.intent || row.task_type || 'Unknown task'}
                    </div>
                    <div style={{ fontSize: '0.64rem', color: C.textMuted, marginTop: '2px' }}>
                      {relTime(row.started_at)} · {row.task_type || '—'} · {row.trigger_source || '—'}
                    </div>
                  </div>
                  <div style={{ display: 'flex', gap: '5px', flexShrink: 0 }}>
                    <button
                      onClick={() => onRetry(row.id)}
                      disabled={busy}
                      style={{ fontSize: '0.64rem', padding: '3px 8px', border: `1px solid ${C.border}`, borderRadius: '5px', background: C.surface, color: C.textSec, cursor: busy ? 'default' : 'pointer', fontFamily: FONT }}
                    >
                      {busy ? '…' : 'Retry'}
                    </button>
                    <button
                      onClick={() => onRerun(row.id)}
                      disabled={busy}
                      style={{ fontSize: '0.64rem', padding: '3px 8px', border: `1px solid ${C.border}`, borderRadius: '5px', background: C.surface, color: C.textSec, cursor: busy ? 'default' : 'pointer', fontFamily: FONT }}
                    >
                      Rerun
                    </button>
                  </div>
                </div>
                {row.error_message && (
                  <div style={{ fontSize: '0.65rem', color: '#f87171', background: '#f871710c', border: '1px solid #f8717118', borderRadius: '4px', padding: '4px 8px', fontFamily: MONO, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                    {row.error_message.length > 130 ? row.error_message.slice(0, 129) + '…' : row.error_message}
                  </div>
                )}
              </div>
            )
          })}
        </div>
      )}
    </div>
  )
}

// ─── Scheduler Activity Panel ─────────────────────────────────────────────────
function SchedulerPanel({ scheduledList, schedRunNowLoading, onRunNow, onNavigate, C }) {
  const active = scheduledList.filter(s => s.enabled)
  const paused = scheduledList.filter(s => !s.enabled)
  const overallStatus = scheduledList.length === 0
    ? 'warning'
    : paused.length === scheduledList.length
    ? 'warning'
    : active.some(s => s.last_status === 'error' || s.last_status === 'failed')
    ? 'failed'
    : 'healthy'

  return (
    <div style={{ background: C.surface, border: `1px solid ${C.border}`, borderRadius: '10px', padding: '14px 16px', marginBottom: '12px' }}>
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: '12px' }}>
        <div>
          <div style={{ fontSize: '0.72rem', fontWeight: '600', color: C.text }}>Scheduler Activity</div>
          <div style={{ fontSize: '0.65rem', color: C.textMuted }}>
            {active.length} active · {paused.length} paused
          </div>
        </div>
        <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
          <StatusPill status={overallStatus} />
          <button
            onClick={() => onNavigate('scheduled')}
            style={{ fontSize: '0.65rem', color: '#6366f1', background: 'none', border: 'none', cursor: 'pointer', fontFamily: FONT }}
          >
            Manage →
          </button>
        </div>
      </div>

      {scheduledList.length === 0 ? (
        <div style={{ textAlign: 'center', padding: '18px 0', color: C.textMuted, fontSize: '0.78rem' }}>
          No schedules configured.
        </div>
      ) : (
        <div style={{ display: 'flex', flexDirection: 'column', gap: '7px' }}>
          {scheduledList.slice(0, 6).map(s => {
            const nextRun = s.next_run_at ? new Date(s.next_run_at) : null
            const overdue = nextRun && nextRun < new Date() && s.enabled
            const lastBad = s.last_status === 'failed' || s.last_status === 'error'
            const dotColor = !s.enabled ? '#6b7280' : lastBad ? '#f87171' : overdue ? '#f59e0b' : '#10b981'

            return (
              <div key={s.id} style={{ display: 'flex', alignItems: 'center', gap: '8px', padding: '8px 10px', background: C.bg, border: `1px solid ${lastBad ? '#f8717120' : C.border}`, borderRadius: '7px' }}>
                <span style={{ width: '6px', height: '6px', borderRadius: '50%', background: dotColor, flexShrink: 0 }} />
                <div style={{ flex: 1, minWidth: 0 }}>
                  <div style={{ fontSize: '0.73rem', fontWeight: '500', color: C.text, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                    {s.input_text?.length > 44 ? s.input_text.slice(0, 43) + '…' : s.input_text}
                  </div>
                  <div style={{ fontSize: '0.62rem', color: overdue ? '#f59e0b' : C.textMuted, marginTop: '2px' }}>
                    {s.frequency}
                    {nextRun && <span> · Next: {relTime(s.next_run_at)}{overdue ? ' (overdue)' : ''}</span>}
                    {s.last_status && (
                      <span style={{ marginLeft: '8px', color: lastBad ? '#f87171' : '#10b981', fontWeight: '500' }}>
                        Last: {s.last_status}
                      </span>
                    )}
                    {s.run_count > 0 && <span style={{ marginLeft: '8px' }}>{s.run_count} runs</span>}
                  </div>
                </div>
                <button
                  onClick={() => onRunNow(s.id)}
                  disabled={schedRunNowLoading.has(s.id) || !s.enabled}
                  style={{
                    fontSize: '0.62rem', padding: '3px 8px', borderRadius: '5px',
                    border: `1px solid ${C.border}`, background: C.surface,
                    color: s.enabled ? '#6366f1' : C.textMuted,
                    cursor: s.enabled && !schedRunNowLoading.has(s.id) ? 'pointer' : 'default',
                    fontFamily: FONT, flexShrink: 0,
                  }}
                >
                  {schedRunNowLoading.has(s.id) ? '…' : 'Run now'}
                </button>
              </div>
            )
          })}
        </div>
      )}
    </div>
  )
}

// ─── Drift Alert Center ───────────────────────────────────────────────────────
function DriftAlerts({ reportList, onNavigate, C }) {
  const STALE_DAYS = 7
  const now = Date.now()

  const candidates = reportList
    .filter(r => r.task_type === 'generate_dataset_report' || r.task_type === 'email_dataset_report')
    .map(r => {
      const ageDays = (now - new Date(r.created_at).getTime()) / 86400000
      return { ...r, ageDays, stale: ageDays > STALE_DAYS }
    })
    .sort((a, b) => b.ageDays - a.ageDays)
    .slice(0, 6)

  const staleCount = candidates.filter(r => r.stale).length
  const driftStatus = staleCount > 2 ? 'failed' : staleCount > 0 ? 'warning' : candidates.length > 0 ? 'healthy' : 'warning'

  return (
    <div style={{ background: C.surface, border: `1px solid ${C.border}`, borderRadius: '10px', padding: '14px 16px' }}>
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: '12px' }}>
        <div>
          <div style={{ fontSize: '0.72rem', fontWeight: '600', color: C.text }}>Drift Alert Center</div>
          <div style={{ fontSize: '0.65rem', color: C.textMuted }}>
            Reports &gt;{STALE_DAYS}d old flagged as stale · data drift likely
          </div>
        </div>
        <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
          {candidates.length > 0 && <StatusPill status={driftStatus} />}
          <button
            onClick={() => onNavigate('reports')}
            style={{ fontSize: '0.65rem', color: '#6366f1', background: 'none', border: 'none', cursor: 'pointer', fontFamily: FONT }}
          >
            Reports →
          </button>
        </div>
      </div>

      {candidates.length === 0 ? (
        <div style={{ textAlign: 'center', padding: '18px 0', color: C.textMuted, fontSize: '0.78rem' }}>
          No dataset reports found. Generate a report first.
        </div>
      ) : (
        <div style={{ display: 'flex', flexDirection: 'column', gap: '7px' }}>
          {candidates.map(r => (
            <div key={r.id} style={{ display: 'flex', alignItems: 'center', gap: '8px', padding: '8px 10px', background: C.bg, border: `1px solid ${r.stale ? '#f59e0b20' : C.border}`, borderRadius: '7px' }}>
              <span style={{ width: '6px', height: '6px', borderRadius: '50%', background: r.stale ? '#f59e0b' : '#10b981', flexShrink: 0 }} />
              <div style={{ flex: 1, minWidth: 0 }}>
                <div style={{ fontSize: '0.73rem', fontWeight: '500', color: C.text, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                  {r.title}
                </div>
                <div style={{ fontSize: '0.62rem', color: C.textMuted, marginTop: '2px' }}>
                  Generated {relTime(r.created_at)}
                  {r.stale && (
                    <span style={{ marginLeft: '6px', color: '#f59e0b', fontWeight: '600' }}>
                      · Stale — re-run recommended
                    </span>
                  )}
                </div>
              </div>
              {r.stale && (
                <span style={{ fontSize: '0.6rem', fontWeight: '700', color: '#f59e0b', background: '#f59e0b1a', padding: '2px 6px', borderRadius: '4px', flexShrink: 0 }}>
                  {Math.floor(r.ageDays)}d old
                </span>
              )}
            </div>
          ))}
        </div>
      )}
    </div>
  )
}

// ─── Notification Feed ────────────────────────────────────────────────────────
function NotificationFeed({ notifications, C }) {
  const [showAll, setShowAll] = useState(false)
  const sorted = [...notifications].sort((a, b) => new Date(b.created_at) - new Date(a.created_at))
  const unread = sorted.filter(n => !n.read).length
  const visible = showAll ? sorted.slice(0, 12) : sorted.slice(0, 5)

  const typeColor = { success: '#10b981', error: '#f87171', warning: '#f59e0b', info: '#6366f1', report: '#8b5cf6' }

  return (
    <div style={{ background: C.surface, border: `1px solid ${C.border}`, borderRadius: '10px', padding: '14px 16px' }}>
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: '12px' }}>
        <div>
          <div style={{ fontSize: '0.72rem', fontWeight: '600', color: C.text }}>Notification Activity</div>
          <div style={{ fontSize: '0.65rem', color: C.textMuted }}>{unread} unread · {notifications.length} total</div>
        </div>
        {unread > 0 && (
          <span style={{ background: '#6366f11a', color: '#6366f1', fontSize: '0.62rem', fontWeight: '700', padding: '2px 7px', borderRadius: '10px' }}>
            {unread} new
          </span>
        )}
      </div>

      {sorted.length === 0 ? (
        <div style={{ textAlign: 'center', padding: '18px 0', color: C.textMuted, fontSize: '0.78rem' }}>
          No notifications yet.
        </div>
      ) : (
        <>
          <div style={{ display: 'flex', flexDirection: 'column', gap: '6px' }}>
            {visible.map(n => (
              <div key={n.id} style={{ display: 'flex', gap: '8px', padding: '7px 10px', background: C.bg, border: `1px solid ${n.read ? C.border : '#6366f118'}`, borderRadius: '7px', opacity: n.read ? 0.8 : 1 }}>
                <span style={{ width: '6px', height: '6px', borderRadius: '50%', background: typeColor[n.type] || typeColor.info, marginTop: '5px', flexShrink: 0 }} />
                <div style={{ flex: 1, minWidth: 0 }}>
                  <div style={{ fontSize: '0.73rem', fontWeight: n.read ? '400' : '600', color: C.text, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                    {n.title}
                  </div>
                  {n.message && n.message !== n.title && (
                    <div style={{ fontSize: '0.63rem', color: C.textMuted, marginTop: '1px', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                      {n.message}
                    </div>
                  )}
                  <div style={{ fontSize: '0.61rem', color: C.textMuted, marginTop: '2px' }}>{relTime(n.created_at)}</div>
                </div>
              </div>
            ))}
          </div>
          {sorted.length > 5 && (
            <button
              onClick={() => setShowAll(v => !v)}
              style={{ marginTop: '10px', width: '100%', background: 'none', border: `1px solid ${C.border}`, borderRadius: '6px', padding: '6px', fontSize: '0.68rem', color: C.textMuted, cursor: 'pointer', fontFamily: FONT }}
            >
              {showAll ? 'Show less' : `Show ${sorted.length - 5} more`}
            </button>
          )}
        </>
      )}
    </div>
  )
}

// ─── Main ─────────────────────────────────────────────────────────────────────
export default function OperationsCenter({
  history,
  historyLoading,
  scheduledList,
  scheduledLoading,
  notifications,
  reportList,
  workflowList,
  execActionLoading,
  schedRunNowLoading,
  onRetry,
  onRerun,
  onRunNow,
  onNavigate,
  C,
}) {
  const isLoading = historyLoading || scheduledLoading

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: '0' }}>

      {/* Header */}
      <div style={{ display: 'flex', alignItems: 'flex-start', justifyContent: 'space-between', marginBottom: '18px' }}>
        <div>
          <h2 style={{ margin: '0 0 3px', fontSize: '1.35rem', fontWeight: '700', color: C.text, letterSpacing: '-0.4px' }}>
            Operations Center
          </h2>
          <p style={{ margin: 0, color: C.textMuted, fontSize: '0.74rem' }}>
            Real-time health · failure triage · scheduler control · drift monitoring
          </p>
        </div>
        {isLoading && (
          <span style={{ fontSize: '0.68rem', color: C.textMuted, marginTop: '6px' }}>Refreshing…</span>
        )}
      </div>

      {/* Health summary */}
      <HealthSummary history={history} C={C} />

      {/* Throughput chart */}
      <ThroughputChart history={history} C={C} />

      {/* Workflow health (only if there are workflows with run history) */}
      <WorkflowHealthPanel workflowList={workflowList} history={history} C={C} />

      {/* Two-column: failures + drift  |  scheduler + notifications */}
      <div style={{ display: 'grid', gridTemplateColumns: '1fr 360px', gap: '12px', alignItems: 'start' }}>
        <div>
          <FailureInbox
            history={history}
            execActionLoading={execActionLoading}
            onRetry={onRetry}
            onRerun={onRerun}
            C={C}
          />
          <DriftAlerts
            reportList={reportList}
            onNavigate={onNavigate}
            C={C}
          />
        </div>
        <div>
          <SchedulerPanel
            scheduledList={scheduledList}
            schedRunNowLoading={schedRunNowLoading}
            onRunNow={onRunNow}
            onNavigate={onNavigate}
            C={C}
          />
          <NotificationFeed
            notifications={notifications}
            C={C}
          />
        </div>
      </div>

    </div>
  )
}
