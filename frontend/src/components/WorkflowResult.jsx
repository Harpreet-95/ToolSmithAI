const FONT = "system-ui, -apple-system, 'Segoe UI', sans-serif"
const MONO = "'Cascadia Code', 'Fira Code', 'JetBrains Mono', monospace"

const TASK_TYPE_LABELS = {
  generate_dataset_report: 'Intelligence Report',
  email_dataset_report:    'Report Delivery',
  analyze_dataset:         'Dataset Analysis',
  send_notification:       'Notification',
  set_reminder:            'Reminder Notification',
  multi_step:              'Multi-Step Workflow',
  workflow:                'Workflow',
}

// ─── Multi-step workflow result ───────────────────────────────────────────────
function MultiStepResult({ result, C, S }) {
  const statusColor = (s) => s === 'completed' ? C.success : s === 'failed' ? C.danger : s === 'running' ? C.warn : C.textMuted
  const statusBg    = (s) => s === 'completed' ? C.successSoft : s === 'failed' ? C.dangerSoft : s === 'running' ? C.warnSoft : 'transparent'
  const statusIcon  = (s) => s === 'completed' ? '✓' : s === 'failed' ? '✕' : s === 'running' ? '…' : s === 'skipped' ? '—' : '·'
  const overallOk   = result.status === 'completed'

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: '8px' }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: '8px', marginBottom: '4px' }}>
        <div style={S.badge(overallOk ? C.success : C.danger, overallOk ? C.successSoft : C.dangerSoft)}>
          <div style={S.dot(overallOk ? C.success : C.danger)} />
          {overallOk ? 'Completed' : 'Failed'}
        </div>
        <span style={{ fontSize: '0.75rem', color: C.textMuted }}>
          {(result.workflow_steps || []).length} steps
        </span>
      </div>

      {(result.workflow_steps || []).map((step, i) => (
        <div key={step.step_id} style={{
          background: C.bg, border: `1px solid ${C.border}`, borderRadius: '8px',
          padding: '10px 14px',
        }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
            <span style={{
              fontSize: '0.72rem', fontWeight: '700', color: statusColor(step.status),
              background: statusBg(step.status), borderRadius: '4px',
              padding: '2px 6px', minWidth: '20px', textAlign: 'center',
            }}>{statusIcon(step.status)}</span>
            <span style={{ fontSize: '0.83rem', fontWeight: '600', color: C.text }}>{step.label}</span>
            <div style={{ marginLeft: 'auto', ...S.badge(statusColor(step.status), statusBg(step.status)) }}>
              {{ completed: 'Done', failed: 'Could Not Complete', running: 'Running', skipped: 'Not Needed' }[step.status] ?? step.status}
            </div>
          </div>
          {step.status === 'failed' && result.error && (
            <div style={{ marginTop: '6px', fontSize: '0.76rem', color: C.danger, lineHeight: 1.5 }}>
              {result.error}
            </div>
          )}
        </div>
      ))}

      {result.error && (
        <div style={{
          background: C.dangerSoft, border: `1px solid ${C.danger}40`,
          borderRadius: '8px', padding: '10px 14px', fontSize: '0.8rem', color: C.danger,
        }}>
          {result.error}
        </div>
      )}
    </div>
  )
}

// ─── Shared helpers ───────────────────────────────────────────────────────────
function SecLabel({ text }) {
  return (
    <div style={{ fontSize: '0.63rem', color: '#6b7280', fontWeight: '700', textTransform: 'uppercase', letterSpacing: '0.09em', marginBottom: '7px' }}>
      {text}
    </div>
  )
}

function Card({ children, extra = {}, C }) {
  return (
    <div style={{ background: C.surface, border: `1px solid ${C.border}`, borderRadius: '10px', padding: '13px 15px', ...extra }}>
      {children}
    </div>
  )
}

// ─── Action center (right sidebar for report results) ────────────────────────
function ActionCenter({ result, C, onOpenReport, onExportReport }) {
  const hasSave = result.report_id != null
  const hasFail = !!result.report_save_warning

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: '10px' }}>

      {/* Primary + export actions */}
      <Card C={C}>
        <SecLabel text="Actions" />
        {hasSave && onOpenReport && (
          <button
            onClick={() => onOpenReport(result.report_id)}
            style={{ display: 'flex', alignItems: 'center', justifyContent: 'center', gap: '7px', width: '100%', background: 'linear-gradient(135deg, #6366f1 0%, #8b5cf6 100%)', border: 'none', borderRadius: '8px', padding: '10px 0', fontSize: '0.82rem', fontWeight: '700', color: '#fff', cursor: 'pointer', fontFamily: FONT, letterSpacing: '-0.1px', marginBottom: '8px' }}
            onMouseEnter={e => { e.currentTarget.style.opacity = '0.88' }}
            onMouseLeave={e => { e.currentTarget.style.opacity = '1' }}
          >
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round"><rect x="3" y="3" width="18" height="18" rx="2"/><path d="M3 9h18"/><path d="M9 21V9"/></svg>
            Open Workspace
          </button>
        )}
        <div style={{ display: 'flex', flexDirection: 'column', gap: '5px' }}>
          {hasSave && onOpenReport && (
            <button
              onClick={() => onOpenReport(result.report_id)}
              style={{ display: 'flex', alignItems: 'center', gap: '6px', width: '100%', background: C.accentSoft, border: `1px solid ${C.accent}30`, borderRadius: '7px', padding: '7px 11px', fontSize: '0.74rem', color: C.accent, cursor: 'pointer', fontFamily: FONT, fontWeight: '600' }}
              onMouseEnter={e => { e.currentTarget.style.background = C.accent; e.currentTarget.style.color = '#fff' }}
              onMouseLeave={e => { e.currentTarget.style.background = C.accentSoft; e.currentTarget.style.color = C.accent }}
            >
              <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/></svg>
              Go to Reports
            </button>
          )}
          {hasSave && onExportReport && ['pdf', 'csv', 'json'].map(fmt => (
            <button
              key={fmt}
              onClick={() => onExportReport(result.report_id, fmt)}
              style={{ display: 'flex', alignItems: 'center', gap: '6px', width: '100%', background: 'transparent', border: `1px solid ${C.border}`, borderRadius: '7px', padding: '7px 11px', fontSize: '0.74rem', color: C.textSec, cursor: 'pointer', fontFamily: FONT, fontWeight: '500' }}
              onMouseEnter={e => { e.currentTarget.style.borderColor = C.accent; e.currentTarget.style.color = C.accent }}
              onMouseLeave={e => { e.currentTarget.style.borderColor = C.border; e.currentTarget.style.color = C.textSec }}
            >
              <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/></svg>
              Export {fmt.toUpperCase()}
            </button>
          ))}
          {!hasSave && !hasFail && (
            <div style={{ fontSize: '0.73rem', color: C.textMuted, lineHeight: 1.5, textAlign: 'center', padding: '4px 0' }}>
              Report was not saved — no export available.
            </div>
          )}
        </div>
      </Card>

      {/* Save status */}
      <Card C={C} extra={result.report_id != null
        ? { background: C.successSoft, border: `1px solid ${C.success}40` }
        : hasFail
        ? { background: C.warnSoft, border: `1px solid ${C.warn}40` }
        : {}
      }>
        <SecLabel text="Save Status" />
        {result.report_id != null ? (
          <div>
            <div style={{ display: 'flex', alignItems: 'center', gap: '7px', marginBottom: '2px' }}>
              <div style={{ width: '6px', height: '6px', borderRadius: '50%', background: C.success, flexShrink: 0 }} />
              <span style={{ fontSize: '0.8rem', color: C.success, fontWeight: '600' }}>Saved as report #{result.report_id}</span>
            </div>
            <div style={{ fontSize: '0.7rem', color: C.textSec, marginLeft: '13px' }}>Available in Reports tab.</div>
          </div>
        ) : hasFail ? (
          <div>
            <div style={{ display: 'flex', alignItems: 'center', gap: '7px', marginBottom: '2px' }}>
              <div style={{ width: '6px', height: '6px', borderRadius: '50%', background: C.warn, flexShrink: 0 }} />
              <span style={{ fontSize: '0.8rem', color: C.warn, fontWeight: '600' }}>Generated — not saved</span>
            </div>
            <div style={{ fontSize: '0.7rem', color: C.textSec, marginLeft: '13px', lineHeight: 1.45 }}>{result.report_save_warning}</div>
          </div>
        ) : (
          <div style={{ display: 'flex', alignItems: 'center', gap: '7px' }}>
            <div style={{ width: '6px', height: '6px', borderRadius: '50%', background: C.textMuted, flexShrink: 0 }} />
            <span style={{ fontSize: '0.8rem', color: C.textMuted }}>Not saved as a report.</span>
          </div>
        )}
      </Card>

      {/* Email delivery */}
      {result.email_delivery && (
        <Card C={C} extra={result.email_delivery.sent
          ? { background: C.successSoft, border: `1px solid ${C.success}40` }
          : { background: C.warnSoft, border: `1px solid ${C.warn}40` }
        }>
          <SecLabel text="Email Delivery" />
          {result.email_delivery.sent ? (
            <div style={{ display: 'flex', alignItems: 'center', gap: '7px' }}>
              <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke={C.success} strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><polyline points="20 6 9 17 4 12"/></svg>
              <span style={{ fontSize: '0.78rem', color: C.success }}>Sent to {result.email_delivery.to}</span>
            </div>
          ) : (
            <p style={{ margin: 0, fontSize: '0.78rem', color: C.warn, lineHeight: 1.5 }}>
              {result.email_delivery.reason}
            </p>
          )}
        </Card>
      )}

      {/* Run details */}
      <Card C={C}>
        <SecLabel text="Run Details" />
        <div style={{ display: 'flex', flexDirection: 'column', gap: '5px' }}>
          {result.status && (
            <div style={{ display: 'flex', justifyContent: 'space-between', fontSize: '0.74rem' }}>
              <span style={{ color: C.textMuted }}>Status</span>
              <span style={{ color: C.text, fontWeight: '500' }}>{{ success: 'Done', completed: 'Done', ok: 'Done', failed: 'Could Not Complete' }[result.status] ?? 'Done'}</span>
            </div>
          )}
          {result.started_at && result.finished_at && (
            <div style={{ display: 'flex', justifyContent: 'space-between', fontSize: '0.74rem' }}>
              <span style={{ color: C.textMuted }}>Duration</span>
              <span style={{ color: C.text, fontWeight: '500' }}>
                {((new Date(result.finished_at) - new Date(result.started_at)) / 1000).toFixed(2)}s
              </span>
            </div>
          )}
          {result.started_at && (
            <div style={{ display: 'flex', justifyContent: 'space-between', fontSize: '0.74rem' }}>
              <span style={{ color: C.textMuted }}>Started</span>
              <span style={{ color: C.text, fontFamily: MONO, fontSize: '0.68rem' }}>{new Date(result.started_at).toLocaleTimeString()}</span>
            </div>
          )}
          {result.finished_at && (
            <div style={{ display: 'flex', justifyContent: 'space-between', fontSize: '0.74rem' }}>
              <span style={{ color: C.textMuted }}>Finished</span>
              <span style={{ color: C.text, fontFamily: MONO, fontSize: '0.68rem' }}>{new Date(result.finished_at).toLocaleTimeString()}</span>
            </div>
          )}
          {result.step_results?.length > 0 && (
            <div style={{ display: 'flex', justifyContent: 'space-between', fontSize: '0.74rem' }}>
              <span style={{ color: C.textMuted }}>Steps</span>
              <span style={{ color: C.text, fontWeight: '500' }}>{result.step_results.length}</span>
            </div>
          )}
        </div>
      </Card>

      {/* Compact execution timeline */}
      {(() => {
        const evs = []
        if (result.started_at) evs.push({ label: 'Started', color: C.accent, ts: result.started_at, detail: null })
        if (result.dataset_report?.sections?.length > 0) {
          const n = result.dataset_report.sections.length
          evs.push({ label: 'Report generated', color: C.success, ts: null, detail: `${n} section${n !== 1 ? 's' : ''}` })
        }
        if (result.dataset_report_error) evs.push({ label: 'Report error', color: C.warn, ts: null, detail: result.dataset_report_error })
        if (result.email_delivery?.sent) evs.push({ label: 'Email sent', color: C.success, ts: null, detail: result.email_delivery.to ? `→ ${result.email_delivery.to}` : null })
        if (result.email_delivery && !result.email_delivery.sent) evs.push({ label: 'Email warning', color: C.warn, ts: null, detail: result.email_delivery.reason || null })
        if (result.finished_at) {
          const ok = result.status === 'success' || result.status === 'completed' || result.status === 'ok'
          evs.push({ label: ok ? 'Completed' : 'Failed', color: ok ? C.success : C.danger, ts: result.finished_at, detail: null })
        }
        if (evs.length === 0) return null
        return (
          <Card C={C}>
            <SecLabel text="Activity" />
            <div style={{ position: 'relative', paddingLeft: '18px' }}>
              <div style={{ position: 'absolute', left: '5px', top: '8px', bottom: '8px', width: '1px', background: C.border }} />
              {evs.map((ev, i) => (
                <div key={i} style={{ position: 'relative', paddingBottom: i < evs.length - 1 ? '12px' : '0' }}>
                  <div style={{ position: 'absolute', left: '-14px', top: '4px', width: '7px', height: '7px', borderRadius: '50%', background: ev.color, border: `2px solid ${C.surface}` }} />
                  <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start', gap: '6px' }}>
                    <div style={{ flex: 1, minWidth: 0 }}>
                      <div style={{ fontSize: '0.74rem', fontWeight: '600', color: C.text, lineHeight: 1.3 }}>{ev.label}</div>
                      {ev.detail && <div style={{ fontSize: '0.67rem', color: C.textSec, lineHeight: 1.4, wordBreak: 'break-word', marginTop: '1px' }}>{ev.detail}</div>}
                    </div>
                    {ev.ts && <span style={{ fontSize: '0.63rem', color: C.textMuted, fontFamily: MONO, flexShrink: 0 }}>{new Date(ev.ts).toLocaleTimeString()}</span>}
                  </div>
                </div>
              ))}
            </div>
          </Card>
        )
      })()}
    </div>
  )
}

// ─── Workflow result renderer ─────────────────────────────────────────────────
// SectionRenderer is passed as a prop so ReportSection stays in App.jsx and
// this file has no import dependency on it — keeping the extraction minimal.
export default function WorkflowResult({ result, C, S, onOpenReport, onExportReport, SectionRenderer }) {
  if (result.task_type === 'multi_step' || result.workflow_steps) {
    return <MultiStepResult result={result} C={C} S={S} />
  }

  const isSuccess = result.status === 'success' || result.status === 'completed' || result.status === 'ok'
  const statusColor = isSuccess ? C.success : C.danger
  const statusBg    = isSuccess ? C.successSoft : C.dangerSoft

  const duration = result.started_at && result.finished_at
    ? `${((new Date(result.finished_at) - new Date(result.started_at)) / 1000).toFixed(2)}s`
    : null

  const sectionLabel = (text) => (
    <div style={{ fontSize: '0.66rem', color: C.textMuted, fontWeight: '700', textTransform: 'uppercase', letterSpacing: '0.08em', marginBottom: '8px' }}>
      {text}
    </div>
  )

  const infoCard = (children, extra = {}) => (
    <div style={{ background: C.surface, border: `1px solid ${C.border}`, borderRadius: '10px', padding: '14px 16px', ...extra }}>
      {children}
    </div>
  )

  const hasReport = Boolean(result.dataset_report)

  // ── Header — spans full width for both report and non-report ──────────────
  const header = (
    <div style={{
      background: C.bg,
      border: `1px solid ${C.border}`,
      borderRadius: '12px',
      padding: '16px 18px',
      borderLeft: `4px solid ${isSuccess ? C.success : C.danger}`,
    }}>
      <div style={{ display: 'flex', alignItems: 'flex-start', justifyContent: 'space-between', gap: '12px' }}>
        <div style={{ flex: 1, minWidth: 0 }}>
          <div style={{ fontSize: '0.96rem', fontWeight: '700', color: C.text, letterSpacing: '-0.15px', marginBottom: '6px' }}>
            {hasReport
              ? 'Report Generated'
              : isSuccess ? 'Analysis Complete' : 'Request Failed'}
          </div>
          <div style={{ display: 'flex', alignItems: 'center', flexWrap: 'wrap', gap: '10px' }}>
            {hasReport && (
              <div style={{ display: 'flex', alignItems: 'center', gap: '5px' }}>
                <div style={{ width: '5px', height: '5px', borderRadius: '50%', background: result.report_id != null ? C.success : result.report_save_warning ? C.warn : C.textMuted, flexShrink: 0 }} />
                <span style={{ fontSize: '0.73rem', color: result.report_id != null ? C.success : result.report_save_warning ? C.warn : C.textMuted, fontWeight: '500' }}>
                  {result.report_id != null
                    ? `Saved as report #${result.report_id}`
                    : result.report_save_warning
                    ? 'Generated — not saved'
                    : 'Not saved'}
                </span>
              </div>
            )}
            {duration && <span style={{ fontSize: '0.73rem', color: C.textMuted }}>Duration: {duration}</span>}
            {result.original_input && !hasReport && (
              <span style={{ fontSize: '0.73rem', color: C.textMuted, fontStyle: 'italic', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', maxWidth: '400px' }}>
                "{result.original_input}"
              </span>
            )}
          </div>
        </div>
        <div style={{ ...S.badge(statusColor, statusBg), flexShrink: 0 }}>
          <div style={S.dot(statusColor)} />
          {{ success: 'Done', completed: 'Done', ok: 'Done', failed: 'Could Not Complete' }[result.status] ?? 'Done'}
        </div>
      </div>
    </div>
  )

  // ── Dataset report case: two-column workspace layout ─────────────────────
  if (hasReport) {
    const aiMeta = result._ai_meta
    const hasAIReasoning = aiMeta?.ai_enrichment_used && aiMeta?.reasoning_summary
    const hasAIReport = result.dataset_report?.ai_narrative != null
      || (result.dataset_report?.sections || []).some(s => s.type === 'ai_findings' || s.type === 'ai_insights')

    return (
      <div style={{ display: 'flex', flexDirection: 'column', gap: '14px' }}>
        {header}

        {/* AI Intelligence Status Banner */}
        {(hasAIReasoning || hasAIReport || aiMeta) && (
          <div style={{
            background: hasAIReport || hasAIReasoning ? '#10b9810a' : '#6b72800a',
            border: `1px solid ${hasAIReport || hasAIReasoning ? '#10b98128' : '#6b728020'}`,
            borderRadius: '10px', padding: '11px 16px',
            display: 'flex', flexDirection: 'column', gap: '6px',
          }}>
            <div style={{ display: 'flex', alignItems: 'center', gap: '8px', flexWrap: 'wrap' }}>
              <div style={{ display: 'flex', alignItems: 'center', gap: '5px' }}>
                <span style={{ width: '6px', height: '6px', borderRadius: '50%', background: hasAIReport || hasAIReasoning ? '#10b981' : '#9ca3af', flexShrink: 0, display: 'inline-block' }} />
                <span style={{ fontSize: '0.7rem', fontWeight: '700', color: hasAIReport || hasAIReasoning ? '#10b981' : '#9ca3af', letterSpacing: '0.04em' }}>
                  {hasAIReport || hasAIReasoning ? 'AI Intelligence Active' : 'Standard Report'}
                </span>
              </div>
              {aiMeta?.ai_model_used && (
                <span style={{ fontSize: '0.62rem', color: '#9ca3af', background: '#6b728012', border: '1px solid #6b728020', borderRadius: '4px', padding: '1px 6px' }}>
                  {aiMeta.ai_model_used.replace('gpt-4o-mini','GPT-4o mini').replace('gpt-4o','GPT-4o')}
                </span>
              )}
            </div>
            {hasAIReasoning && (
              <div style={{ fontSize: '0.74rem', color: C.textSec, lineHeight: 1.55 }}>
                {aiMeta.reasoning_summary}
              </div>
            )}
            {(hasAIReport || hasAIReasoning) && (
              <div style={{ fontSize: '0.65rem', color: '#9ca3af', marginTop: '2px' }}>
                AI sections: Executive Summary · Key Findings · Insights · Recommendations
              </div>
            )}
          </div>
        )}

        <div style={{ display: 'flex', gap: '14px', alignItems: 'flex-start' }}>

          {/* ── LEFT: Artifact (report sections) ── */}
          <div style={{ flex: 1, minWidth: 0, display: 'flex', flexDirection: 'column', gap: '10px' }}>

            {/* Preview banner */}
            {result.dataset_report?.sections?.length > 0 && (
              <div style={{ background: C.accentSoft, border: `1px solid ${C.accent}25`, borderRadius: '8px', padding: '10px 14px', display: 'flex', alignItems: 'flex-start', gap: '10px' }}>
                <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke={C.accent} strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round" style={{ flexShrink: 0, marginTop: '1px' }}><circle cx="12" cy="12" r="10"/><line x1="12" y1="8" x2="12" y2="12"/><line x1="12" y1="16" x2="12.01" y2="16"/></svg>
                <div>
                  <div style={{ fontSize: '0.72rem', fontWeight: '700', color: C.accent, marginBottom: '2px' }}>Preview</div>
                  <div style={{ fontSize: '0.72rem', color: C.textSec, lineHeight: 1.5 }}>Quick preview. Open the saved workspace for navigation, role modes, and full exports.</div>
                </div>
              </div>
            )}

            {/* Report sections — the artifact */}
            {result.dataset_report?.sections?.length > 0 && (
              <div style={{ display: 'flex', flexDirection: 'column', gap: '8px' }}>
                {result.dataset_report.sections.map((section, i) => (
                  <SectionRenderer key={i} section={section} C={C} />
                ))}
              </div>
            )}

            {/* Report generation error */}
            {result.dataset_report_error && infoCard(<>
              {sectionLabel('Dataset Report')}
              <p style={{ margin: 0, fontSize: '0.82rem', color: C.warn, lineHeight: 1.6 }}>
                {result.dataset_report_error}
              </p>
            </>, { background: C.warnSoft, border: `1px solid ${C.warn}40` })}
          </div>

          {/* ── RIGHT: Action Center + Metadata ── */}
          <div style={{ width: '252px', flexShrink: 0 }}>
            <ActionCenter result={result} C={C} onOpenReport={onOpenReport} onExportReport={onExportReport} />
          </div>
        </div>
      </div>
    )
  }

  // ── Non-report case: single-column, improved hierarchy ────────────────────
  const aiMeta = result._ai_meta
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: '10px' }}>
      {header}

      {/* AI Status + Reasoning Panel */}
      {aiMeta && infoCard(<>
        {sectionLabel('Analysis')}
        <div style={{ display: 'flex', alignItems: 'center', gap: '8px', marginBottom: aiMeta.reasoning_summary ? '8px' : 0, flexWrap: 'wrap' }}>
          <span style={{ display: 'inline-flex', alignItems: 'center', gap: '5px', fontSize: '0.7rem', fontWeight: '700', padding: '2px 8px', borderRadius: '4px', background: aiMeta.ai_enrichment_used ? '#10b9811a' : '#6b72800d', color: aiMeta.ai_enrichment_used ? '#10b981' : '#9ca3af', border: `1px solid ${aiMeta.ai_enrichment_used ? '#10b98128' : '#6b728020'}` }}>
            <span style={{ width: '5px', height: '5px', borderRadius: '50%', background: aiMeta.ai_enrichment_used ? '#10b981' : '#9ca3af', display: 'inline-block', flexShrink: 0 }} />
            {aiMeta.ai_enrichment_used ? 'AI Active' : 'Standard Analysis'}
          </span>
          {aiMeta.ai_model_used && (
            <span style={{ fontSize: '0.62rem', color: '#9ca3af', background: '#6b728012', border: '1px solid #6b728018', borderRadius: '4px', padding: '1px 6px' }}>
              {aiMeta.ai_model_used.replace('gpt-4o-mini','GPT-4o mini').replace('gpt-4o','GPT-4o')}
            </span>
          )}
          {aiMeta.confidence != null && (
            <span style={{ fontSize: '0.62rem', color: aiMeta.confidence >= 0.85 ? '#10b981' : aiMeta.confidence >= 0.65 ? '#f59e0b' : '#9ca3af', background: 'transparent', border: `1px solid ${aiMeta.confidence >= 0.85 ? '#10b98130' : aiMeta.confidence >= 0.65 ? '#f59e0b30' : '#9ca3af30'}`, borderRadius: '4px', padding: '1px 6px', fontWeight: '600' }}>
              {Math.round(aiMeta.confidence * 100)}% match
            </span>
          )}
        </div>
        {aiMeta.reasoning_summary && (
          <p style={{ margin: 0, fontSize: '0.76rem', color: C.textSec, lineHeight: 1.6 }}>{aiMeta.reasoning_summary}</p>
        )}
      </>)}

      {/* Detected intent — hero text */}
      {result.original_input && infoCard(<>
        {sectionLabel('Your Request')}
        <p style={{ margin: 0, fontSize: '0.84rem', color: C.text, lineHeight: 1.65 }}>{result.original_input}</p>
      </>)}

      {/* Output */}
      {result.output && infoCard(<>
        {sectionLabel('Output')}
        <p style={{ margin: 0, fontSize: '0.82rem', color: C.text, lineHeight: 1.6 }}>{typeof result.output === 'string' ? result.output : JSON.stringify(result.output)}</p>
      </>)}


      {/* Unsupported intent notice */}
      {result.metadata?.unsupported_reason && infoCard(<>
        {sectionLabel('Notice')}
        <p style={{ margin: 0, fontSize: '0.82rem', color: C.warn, lineHeight: 1.6 }}>
          {result.metadata.unsupported_reason}
        </p>
      </>, { background: C.warnSoft, border: `1px solid ${C.warn}40` })}

      {/* Steps */}
      {Array.isArray(result.step_results) && result.step_results.length > 0 && (
        <div>
          {sectionLabel('Steps')}
          <div style={{ display: 'flex', flexDirection: 'column', gap: '7px' }}>
            {result.step_results.map((step, i) => {
              const ok      = step.success !== false && step.status !== 'failed' && step.status !== 'error'
              const sc      = ok ? C.success : C.danger
              const stepDur = step.duration_ms ? `${step.duration_ms}ms` : step.duration ? `${step.duration}s` : null
              const rawOut  = step.output ?? step.result ?? null
              const outText = rawOut ? (typeof rawOut === 'string' ? rawOut : JSON.stringify(rawOut)) : null
              return (
                <div key={i} style={{ background: C.surface, border: `1px solid ${C.border}`, borderRadius: '10px', padding: '11px 14px 11px 18px', position: 'relative', overflow: 'hidden' }}>
                  <div style={{ position: 'absolute', left: 0, top: 0, bottom: 0, width: '3px', background: sc, borderRadius: '10px 0 0 10px' }} />
                  <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: outText ? '6px' : 0 }}>
                    <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
                      <span style={{ fontSize: '0.82rem', fontWeight: '600', color: C.text }}>{step.tool || `Step ${i + 1}`}</span>
                      {step.operation && <span style={{ fontSize: '0.72rem', color: C.textSec }}>· {step.operation}</span>}
                    </div>
                    <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
                      {stepDur && <span style={{ fontSize: '0.67rem', color: C.textMuted }}>{stepDur}</span>}
                      <div style={S.badge(sc, sc + '1a')}><div style={S.dot(sc)} />{ok ? 'Done' : 'Could Not Complete'}</div>
                    </div>
                  </div>
                  {outText && (
                    <p style={{ margin: 0, fontSize: '0.76rem', color: C.textSec, lineHeight: 1.55, whiteSpace: 'pre-wrap', wordBreak: 'break-word' }}>
                      {outText}
                    </p>
                  )}
                </div>
              )
            })}
          </div>
        </div>
      )}

      {/* Workflow Summary */}
      {infoCard(<>
        <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: '10px' }}>
          <div>
            {sectionLabel('Run Summary')}
            {duration && <span style={{ fontSize: '0.77rem', color: C.textSec }}>Duration: {duration}</span>}
          </div>
          <div style={S.badge(statusColor, statusBg)}>
            <div style={S.dot(statusColor)} />
            {result.status || 'unknown'}
          </div>
        </div>
        {(result.step_results?.length > 0 || result.started_at || result.finished_at) && (
          <div style={{ display: 'flex', flexWrap: 'wrap', gap: '14px' }}>
            {result.step_results?.length > 0 && <span style={{ fontSize: '0.77rem', color: C.textSec }}>Steps: <span style={{ color: C.text, fontWeight: '500' }}>{result.step_results.length}</span></span>}
            {result.started_at && <span style={{ fontSize: '0.77rem', color: C.textSec }}>Started: <span style={{ color: C.text }}>{new Date(result.started_at).toLocaleString()}</span></span>}
            {result.finished_at && <span style={{ fontSize: '0.77rem', color: C.textSec }}>Finished: <span style={{ color: C.text }}>{new Date(result.finished_at).toLocaleString()}</span></span>}
          </div>
        )}
      </>)}

      {/* Next Suggested Action */}
      {isSuccess && infoCard(<>
        {sectionLabel('Next Action')}
        <div style={{ fontSize: '0.78rem', color: C.text, lineHeight: 1.6 }}>
          {result.report_id != null
            ? 'Open the saved workspace to explore AI insights, export the report, or schedule it for future runs.'
            : result.task_type === 'send_notification' || result.task_type === 'set_reminder'
            ? 'Your notification has been dispatched. View delivery status in the Timeline above.'
            : 'Review the activity above. Run another request or schedule this for automation.'}
        </div>
      </>)}

      {/* Email Delivery */}
      {result.email_delivery && infoCard(<>
        {sectionLabel('Email Delivery')}
        {result.email_delivery.sent ? (
          <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
            <div style={S.dot(C.success)} />
            <span style={{ fontSize: '0.82rem', color: C.success }}>
              Report sent to {result.email_delivery.to}
            </span>
          </div>
        ) : (
          <p style={{ margin: 0, fontSize: '0.82rem', color: C.warn, lineHeight: 1.6 }}>
            {result.email_delivery.reason}
          </p>
        )}
      </>, result.email_delivery.sent
        ? { background: C.successSoft, border: `1px solid ${C.success}40` }
        : { background: C.warnSoft,   border: `1px solid ${C.warn}40`    }
      )}

      {/* Execution Timeline */}
      {(() => {
        const evs = []
        if (result.started_at) evs.push({ ts: result.started_at, label: 'Started', color: C.accent, badge: 'Start', detail: null })
        if (Array.isArray(result.step_results)) {
          result.step_results.forEach((step, i) => {
            const ok  = step.success !== false && step.status !== 'failed' && step.status !== 'error'
            const dur = step.duration_ms ? `${step.duration_ms}ms` : step.duration ? `${step.duration}s` : null
            evs.push({ ts: null, label: `Step ${i + 1}`, color: ok ? C.success : C.danger, badge: ok ? 'Done' : 'Could Not Complete', detail: dur || null })
          })
        }
        if (result.email_delivery) {
          if (result.email_delivery.sent) {
            evs.push({ ts: null, label: 'Email Sent', color: C.success, badge: 'sent', detail: result.email_delivery.to ? `→ ${result.email_delivery.to}` : null })
          } else {
            evs.push({ ts: null, label: 'Email Warning', color: C.warn, badge: 'warning', detail: result.email_delivery.reason || null })
          }
        }
        if (result.finished_at) {
          evs.push({ ts: result.finished_at, label: isSuccess ? 'Completed' : 'Request Failed', color: isSuccess ? C.success : C.danger, badge: isSuccess ? 'Done' : 'Could Not Complete', detail: !isSuccess && result.error ? result.error : null })
        }
        if (evs.length === 0) return null
        return infoCard(<>
          {sectionLabel('Activity Timeline')}
          <div style={{ position: 'relative', paddingLeft: '22px' }}>
            <div style={{ position: 'absolute', left: '7px', top: '10px', bottom: '10px', width: '1px', background: C.border }} />
            {evs.map((ev, i) => (
              <div key={i} style={{ position: 'relative', paddingBottom: i < evs.length - 1 ? '16px' : '0' }}>
                <div style={{ position: 'absolute', left: '-17px', top: '5px', width: '8px', height: '8px', borderRadius: '50%', background: ev.color, border: `2px solid ${C.surface}`, boxShadow: `0 0 0 1px ${ev.color}40` }} />
                <div style={{ display: 'flex', alignItems: 'flex-start', justifyContent: 'space-between', gap: '12px' }}>
                  <div style={{ display: 'flex', flexDirection: 'column', gap: '2px', minWidth: 0 }}>
                    <span style={{ fontSize: '0.8rem', fontWeight: '600', color: C.text, lineHeight: 1.3 }}>{ev.label}</span>
                    {ev.detail && <span style={{ fontSize: '0.72rem', color: C.textSec, lineHeight: 1.5, wordBreak: 'break-word' }}>{ev.detail}</span>}
                  </div>
                  <div style={{ display: 'flex', alignItems: 'center', gap: '8px', flexShrink: 0 }}>
                    {ev.ts
                      ? <span style={{ fontSize: '0.66rem', color: C.textMuted, fontFamily: MONO }}>{new Date(ev.ts).toLocaleTimeString()}</span>
                      : <span style={{ fontSize: '0.66rem', color: C.textMuted }}>·</span>
                    }
                    <div style={S.badge(ev.color, ev.color + '1a')}><div style={S.dot(ev.color)} />{ev.badge}</div>
                  </div>
                </div>
              </div>
            ))}
          </div>
        </>)
      })()}
    </div>
  )
}
