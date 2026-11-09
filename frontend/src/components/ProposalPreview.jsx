import { useState } from 'react'

const FONT = "'Inter', 'SF Pro Display', system-ui, -apple-system, sans-serif"
const MONO = "'JetBrains Mono', 'Fira Code', 'Cascadia Code', monospace"

const STEP_LABELS = {
  generate_dataset_report: 'Generate intelligence report',
  email_dataset_report:    'Email report',
  analyze_dataset:         'Analyze dataset',
  send_notification:       'Send notification',
}

const RISK_COLOR = {
  low:    { text: '#10b981', bg: '#10b9811a', border: '#10b98130' },
  medium: { text: '#f59e0b', bg: '#f59e0b1a', border: '#f59e0b30' },
  high:   { text: '#f87171', bg: '#f871711a', border: '#f8717130' },
}

const TYPE_COLOR = {
  workflow:     { text: '#6366f1', bg: '#6366f11a' },
  dynamic_tool: { text: '#8b5cf6', bg: '#8b5cf61a' },
}

const INPUT_LABELS = {
  dataset_id: 'Dataset required',
  url:        'Target URL',
  to:         'Recipient email',
  message:    'Notification message',
}

function buildPlanExplanation(proposal) {
  if (proposal.reasoning_summary) return proposal.reasoning_summary
  const steps = proposal.primitives_or_steps || []
  const types = steps.map(s => s.step_type || s.primitive_type || '').filter(Boolean)
  if (types.includes('analyze_dataset') && types.includes('send_notification'))
    return 'This workflow analyzes your dataset for anomalies, then sends an alert notification.'
  if (types.includes('generate_dataset_report') && types.includes('email_dataset_report'))
    return 'This workflow generates an intelligence report from your dataset and delivers it by email.'
  if (types.includes('generate_dataset_report'))
    return 'This plan generates an intelligence report using your selected dataset.'
  if (types.includes('send_notification'))
    return 'This plan sends a notification based on your workflow configuration.'
  if (proposal.proposal_type === 'dynamic_tool')
    return 'This plan configures a custom tool action based on your intent.'
  return null
}

function ConfidenceBadge({ confidence }) {
  if (confidence == null) return null
  const pct = Math.round(confidence * 100)
  const color = confidence >= 0.85 ? '#10b981' : confidence >= 0.65 ? '#f59e0b' : '#9ca3af'
  const bg    = confidence >= 0.85 ? '#10b9811a' : confidence >= 0.65 ? '#f59e0b1a' : '#9ca3af1a'
  const bdr   = confidence >= 0.85 ? '#10b98130' : confidence >= 0.65 ? '#f59e0b30' : '#9ca3af30'
  return (
    <span style={{
      fontSize: '0.65rem', fontWeight: '600', padding: '2px 7px', borderRadius: '4px',
      background: bg, color, border: `1px solid ${bdr}`,
    }}>
      {pct}% match
    </span>
  )
}

function StepCard({ step, C, idx }) {
  const rawId = step.primitive_type || step.step_type || ''
  const friendlyLabel = STEP_LABELS[rawId] || rawId.replace(/_/g, ' ')
  const purpose = step.purpose || ''
  const validated = step.validated !== false

  return (
    <div style={{
      display: 'flex', alignItems: 'flex-start', gap: '10px',
      padding: '10px 14px', background: C.bg,
      border: `1px solid ${validated ? C.border : '#f59e0b40'}`,
      borderRadius: '8px',
    }}>
      <div style={{
        width: '22px', height: '22px', borderRadius: '50%', flexShrink: 0,
        background: C.accentSoft, border: `1px solid ${C.accent}30`,
        display: 'flex', alignItems: 'center', justifyContent: 'center',
        fontSize: '0.65rem', fontWeight: '700', color: C.accent, fontFamily: MONO,
      }}>
        {idx}
      </div>
      <div style={{ flex: 1, minWidth: 0 }}>
        <div style={{ fontSize: '0.76rem', fontWeight: '600', color: C.text, marginBottom: '2px' }}>
          {friendlyLabel}
        </div>
        {purpose && (
          <div style={{ fontSize: '0.72rem', color: C.textSec, lineHeight: 1.4 }}>{purpose}</div>
        )}
      </div>
    </div>
  )
}

export default function ProposalPreview({ proposal, C, onApprove, onEdit, onClear, onSaveDraft, onGoToDatasets }) {
  const [draftSaving, setDraftSaving] = useState(false)
  const [draftMsg,    setDraftMsg]    = useState(null)

  async function handleDraftClick() {
    setDraftSaving(true)
    setDraftMsg(null)
    try {
      await onSaveDraft()
      setDraftMsg({ ok: true, text: 'Workflow draft saved. Open the Workflows tab to review and run it.' })
    } catch (err) {
      setDraftMsg({ ok: false, text: err.message?.replace(/^\d+:\s*/, '') || 'Failed to save draft.' })
    } finally {
      setDraftSaving(false)
    }
  }
  if (!proposal) return null

  const risk = proposal.risk_level || 'low'
  const riskStyle = RISK_COLOR[risk] || RISK_COLOR.low
  const typeStyle = TYPE_COLOR[proposal.proposal_type] || TYPE_COLOR.workflow
  const steps = proposal.primitives_or_steps || []
  const warnings = proposal.warnings || []
  const inputs = proposal.required_inputs || []
  const needsClarification = Boolean(proposal.clarification_required)
  const missingInputs = proposal.missing_inputs || []
  const reportType = proposal.suggested_report_type
  const audience = proposal.audience
  const selectedSections = proposal.selected_sections

  return (
    <div style={{
      marginBottom: '12px',
      background: C.surface,
      border: `1px solid ${C.border}`,
      borderRadius: '14px',
      overflow: 'hidden',
    }}>
      {/* Header bar */}
      <div style={{
        padding: '14px 20px',
        borderBottom: `1px solid ${C.border}`,
        display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: '12px',
        background: C.bg,
      }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: '10px', minWidth: 0 }}>
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke={C.accent} strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" style={{ flexShrink: 0 }}>
            <path d="M9 11l3 3L22 4"/><path d="M21 12v7a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11"/>
          </svg>
          <span style={{ fontSize: '0.72rem', fontWeight: '700', color: C.textSec, textTransform: 'uppercase', letterSpacing: '0.1em' }}>
            Composition Proposal
          </span>
          <span style={{
            fontSize: '0.65rem', fontWeight: '600', padding: '2px 8px', borderRadius: '4px',
            background: typeStyle.bg, color: typeStyle.text,
          }}>
            {proposal.proposal_type === 'dynamic_tool' ? 'Dynamic Tool' : 'Workflow'}
          </span>
          <span style={{
            fontSize: '0.65rem', fontWeight: '600', padding: '2px 8px', borderRadius: '4px',
            background: riskStyle.bg, color: riskStyle.text, border: `1px solid ${riskStyle.border}`,
          }}>
            {risk.toUpperCase()} RISK
          </span>
        </div>
        <div style={{ display: 'flex', alignItems: 'center', gap: '6px', flexShrink: 0 }}>
          <ConfidenceBadge confidence={proposal.confidence} />
          {proposal.ai_enrichment_used ? (
            <span style={{ fontSize: '0.63rem', fontWeight: '700', padding: '2px 8px', borderRadius: '4px', background: '#10b9811a', color: '#10b981', border: '1px solid #10b98130', display: 'flex', alignItems: 'center', gap: '4px', whiteSpace: 'nowrap' }}>
              <span style={{ width: '5px', height: '5px', borderRadius: '50%', background: '#10b981', flexShrink: 0, display: 'inline-block' }} />
              AI Active{proposal.ai_model_used ? ` · ${proposal.ai_model_used.replace('gpt-4o-mini','GPT-4o mini').replace('gpt-4o','GPT-4o')}` : ''}
            </span>
          ) : proposal.ai_enabled ? (
            <span style={{ fontSize: '0.63rem', fontWeight: '600', padding: '2px 8px', borderRadius: '4px', background: '#6b72800d', color: '#9ca3af', border: '1px solid #6b728025', whiteSpace: 'nowrap' }}>
              Standard Plan
            </span>
          ) : (
            <span style={{ fontSize: '0.63rem', fontWeight: '600', padding: '2px 8px', borderRadius: '4px', background: '#6b72800d', color: '#9ca3af', border: '1px solid #6b728025', whiteSpace: 'nowrap' }}>
              Smart Plan
            </span>
          )}
          <button
            onClick={onClear}
            title="Clear proposal"
            style={{ background: 'none', border: 'none', cursor: 'pointer', color: C.textMuted, display: 'flex', alignItems: 'center', padding: '2px' }}
          >
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>
          </button>
        </div>
      </div>

      <div style={{ padding: '18px 20px', display: 'flex', flexDirection: 'column', gap: '16px' }}>

        {/* Interpreted goal */}
        <div>
          <div style={{ fontSize: '0.68rem', fontWeight: '600', color: C.textMuted, textTransform: 'uppercase', letterSpacing: '0.08em', marginBottom: '6px' }}>
            Interpreted Goal
          </div>
          <div style={{ fontSize: '0.84rem', color: C.text, lineHeight: 1.55, fontStyle: 'italic' }}>
            "{proposal.interpreted_goal}"
          </div>
        </div>

        {/* Plan explanation */}
        {(() => {
          const explanation = buildPlanExplanation(proposal)
          return explanation ? (
            <div style={{
              display: 'flex', alignItems: 'flex-start', gap: '8px',
              padding: '9px 13px', background: C.accentSoft,
              border: `1px solid ${C.accent}20`, borderRadius: '8px',
            }}>
              <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke={C.accent} strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" style={{ flexShrink: 0, marginTop: '2px' }}>
                <circle cx="12" cy="12" r="10"/><path d="M12 16v-4"/><path d="M12 8h.01"/>
              </svg>
              <span style={{ fontSize: '0.74rem', color: C.accent, lineHeight: 1.5 }}>{explanation}</span>
            </div>
          ) : null
        })()}

        {/* Fallback transparency notice */}
        {proposal.ai_enabled && !proposal.ai_enrichment_used && (
          <div style={{ display: 'flex', alignItems: 'center', gap: '8px', padding: '8px 12px', background: '#6b72800a', border: '1px solid #6b728020', borderRadius: '8px' }}>
            <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="#9ca3af" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" style={{ flexShrink: 0 }}>
              <circle cx="12" cy="12" r="10"/><path d="M12 8v4"/><path d="M12 16h.01"/>
            </svg>
            <span style={{ fontSize: '0.69rem', color: '#9ca3af', lineHeight: 1.4 }}>
              Plan generated automatically. Enhanced analysis is not available for this request type.
            </span>
          </div>
        )}

        {/* Clarification banner */}
        {needsClarification && (
          <div style={{
            display: 'flex', alignItems: 'flex-start', gap: '10px',
            padding: '11px 14px', background: '#f59e0b0d',
            border: '1px solid #f59e0b40', borderRadius: '10px',
          }}>
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="#f59e0b" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" style={{ flexShrink: 0, marginTop: '1px' }}>
              <circle cx="12" cy="12" r="10"/><path d="M12 8v4"/><path d="M12 16h.01"/>
            </svg>
            <div>
              <div style={{ fontSize: '0.74rem', fontWeight: '700', color: '#f59e0b', marginBottom: '4px' }}>Clarification needed before executing</div>
              {missingInputs.length > 0 && (
                <div style={{ display: 'flex', flexWrap: 'wrap', gap: '5px' }}>
                  {missingInputs.map(m => (
                    <span key={m} style={{
                      fontSize: '0.68rem', fontWeight: '600', padding: '2px 7px', borderRadius: '4px',
                      background: '#f59e0b1a', color: '#f59e0b', border: '1px solid #f59e0b30',
                    }}>
                      {m === 'dataset' ? 'No dataset selected' : m === 'report_type' ? 'Report type unclear' : m}
                    </span>
                  ))}
                </div>
              )}
            </div>
          </div>
        )}

        {/* AI clarification question (from reasoning layer) */}
        {proposal.clarification_question && (
          <div style={{
            display: 'flex', alignItems: 'flex-start', gap: '9px',
            padding: '10px 14px', background: '#6366f10d',
            border: '1px solid #6366f130', borderRadius: '10px',
          }}>
            <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="#6366f1" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" style={{ flexShrink: 0, marginTop: '1px' }}>
              <circle cx="12" cy="12" r="10"/><path d="M9.09 9a3 3 0 0 1 5.83 1c0 2-3 3-3 3"/><line x1="12" y1="17" x2="12.01" y2="17"/>
            </svg>
            <div>
              <div style={{ fontSize: '0.7rem', fontWeight: '700', color: '#6366f1', marginBottom: '3px' }}>
                AI Question
              </div>
              <div style={{ fontSize: '0.74rem', color: '#4f46e5', lineHeight: 1.5 }}>
                {proposal.clarification_question}
              </div>
            </div>
          </div>
        )}

        {/* Report intent metadata */}
        {(reportType || audience || selectedSections) && (
          <div style={{ display: 'flex', flexWrap: 'wrap', gap: '6px', alignItems: 'center' }}>
            {reportType && (
              <span style={{
                fontSize: '0.67rem', fontWeight: '600', padding: '2px 8px', borderRadius: '4px',
                background: '#6366f11a', color: '#6366f1', border: '1px solid #6366f130',
              }}>
                {reportType.replace(/_/g, ' ')}
              </span>
            )}
            {audience && (
              <span style={{
                fontSize: '0.67rem', fontWeight: '600', padding: '2px 8px', borderRadius: '4px',
                background: '#10b9811a', color: '#10b981', border: '1px solid #10b98130',
              }}>
                audience: {audience}
              </span>
            )}
            {selectedSections && selectedSections.length > 0 && (
              <span style={{ fontSize: '0.67rem', color: '#6b7280' }}>
                {selectedSections.length} section{selectedSections.length !== 1 ? 's' : ''} selected
              </span>
            )}
            {!selectedSections && reportType && (
              <span style={{ fontSize: '0.67rem', color: '#6b7280' }}>all sections</span>
            )}
          </div>
        )}

        {/* Steps */}
        {steps.length > 0 && (
          <div>
            <div style={{ fontSize: '0.68rem', fontWeight: '600', color: C.textMuted, textTransform: 'uppercase', letterSpacing: '0.08em', marginBottom: '8px' }}>
              Steps ({steps.length})
            </div>
            <div style={{ display: 'flex', flexDirection: 'column', gap: '6px' }}>
              {steps.map((step, i) => (
                <StepCard key={i} step={step} C={C} idx={step.order || i + 1} />
              ))}
            </div>
          </div>
        )}

        {/* Required inputs */}
        {inputs.length > 0 && (
          <div>
            <div style={{ fontSize: '0.68rem', fontWeight: '600', color: C.textMuted, textTransform: 'uppercase', letterSpacing: '0.08em', marginBottom: '6px' }}>
              Required Inputs
            </div>
            <div style={{ display: 'flex', flexWrap: 'wrap', gap: '6px' }}>
              {inputs.map(inp => (
                <span key={inp} style={{
                  fontSize: '0.72rem', padding: '3px 10px', borderRadius: '6px',
                  background: C.surface, color: C.textSec, border: `1px solid ${C.border}`,
                  fontWeight: '500',
                }}>
                  {INPUT_LABELS[inp] || inp.replace(/_/g, ' ')}
                </span>
              ))}
            </div>
          </div>
        )}

        {/* Dataset required CTA */}
        {inputs.includes('dataset_id') && (
          <div style={{
            display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: '12px',
            padding: '10px 14px', background: '#f59e0b0d',
            border: '1px solid #f59e0b40', borderRadius: '8px',
          }}>
            <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
              <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="#f59e0b" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" style={{ flexShrink: 0 }}>
                <ellipse cx="12" cy="5" rx="9" ry="3"/><path d="M21 12c0 1.66-4 3-9 3s-9-1.34-9-3"/><path d="M3 5v14c0 1.66 4 3 9 3s9-1.34 9-3V5"/>
              </svg>
              <span style={{ fontSize: '0.73rem', color: '#92400e', lineHeight: 1.45 }}>
                This workflow requires a dataset. Make sure one is selected before running.
              </span>
            </div>
            {onGoToDatasets && (
              <button
                onClick={onGoToDatasets}
                style={{
                  background: '#f59e0b', color: '#fff', border: 'none', borderRadius: '6px',
                  padding: '5px 12px', fontSize: '0.72rem', fontWeight: '600',
                  cursor: 'pointer', whiteSpace: 'nowrap', fontFamily: FONT,
                }}
              >
                Go to Datasets
              </button>
            )}
          </div>
        )}

        {/* Warnings */}
        {warnings.length > 0 && (
          <div style={{ display: 'flex', flexDirection: 'column', gap: '6px' }}>
            {warnings.map((w, i) => (
              <div key={i} style={{
                display: 'flex', alignItems: 'flex-start', gap: '8px',
                padding: '9px 12px', background: '#f59e0b0d',
                border: '1px solid #f59e0b30', borderRadius: '8px',
              }}>
                <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="#f59e0b" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" style={{ flexShrink: 0, marginTop: '1px' }}>
                  <path d="m10.29 3.86-8.66 15A1 1 0 0 0 2.5 20.5h19a1 1 0 0 0 .87-1.5l-8.66-15a1 1 0 0 0-1.74 0z"/><line x1="12" y1="9" x2="12" y2="13"/><line x1="12" y1="17" x2="12.01" y2="17"/>
                </svg>
                <span style={{ fontSize: '0.76rem', color: '#f59e0b', lineHeight: 1.45 }}>{w}</span>
              </div>
            ))}
          </div>
        )}

        {/* Approval notice */}
        {(proposal.proposal_type === 'dynamic_tool' || risk === 'high') ? (
          <div style={{
            display: 'flex', alignItems: 'center', gap: '7px',
            padding: '8px 12px', background: '#8b5cf61a',
            border: '1px solid #8b5cf625', borderRadius: '8px',
          }}>
            <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="#8b5cf6" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" style={{ flexShrink: 0 }}>
              <rect x="3" y="11" width="18" height="11" rx="2" ry="2"/><path d="M7 11V7a5 5 0 0 1 10 0v4"/>
            </svg>
            <span style={{ fontSize: '0.72rem', color: '#8b5cf6' }}>
              Approval required before this tool can be activated.
            </span>
          </div>
        ) : (
          <div style={{
            display: 'flex', alignItems: 'center', gap: '7px',
            padding: '8px 12px', background: C.accentSoft,
            border: `1px solid ${C.accent}25`, borderRadius: '8px',
          }}>
            <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke={C.accent} strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" style={{ flexShrink: 0 }}>
              <circle cx="12" cy="12" r="10"/><line x1="12" y1="8" x2="12" y2="12"/><line x1="12" y1="16" x2="12.01" y2="16"/>
            </svg>
            <span style={{ fontSize: '0.72rem', color: C.accent }}>
              Review this plan before running.
            </span>
          </div>
        )}

        {/* Draft save result message */}
        {draftMsg && (
          <div style={{
            display: 'flex', alignItems: 'center', gap: '7px',
            padding: '8px 12px',
            background: draftMsg.ok ? '#10b9811a' : '#f871711a',
            border: `1px solid ${draftMsg.ok ? '#10b98130' : '#f8717130'}`,
            borderRadius: '8px',
          }}>
            <svg width="12" height="12" viewBox="0 0 24 24" fill="none"
              stroke={draftMsg.ok ? '#10b981' : '#f87171'}
              strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"
              style={{ flexShrink: 0 }}>
              {draftMsg.ok
                ? <polyline points="20 6 9 17 4 12"/>
                : <><circle cx="12" cy="12" r="10"/><line x1="12" y1="8" x2="12" y2="12"/><line x1="12" y1="16" x2="12.01" y2="16"/></>}
            </svg>
            <span style={{ fontSize: '0.72rem', color: draftMsg.ok ? '#10b981' : '#f87171' }}>
              {draftMsg.text}
            </span>
          </div>
        )}

        {/* Action row */}
        <div style={{ display: 'flex', alignItems: 'center', gap: '8px', paddingTop: '2px', flexWrap: 'wrap' }}>
          <button
            onClick={needsClarification ? undefined : onApprove}
            disabled={needsClarification}
            title={needsClarification ? 'Resolve clarification items before executing' : undefined}
            style={{
              background: needsClarification
                ? '#6b728040'
                : 'linear-gradient(135deg, #6366f1 0%, #8b5cf6 100%)',
              color: needsClarification ? '#6b7280' : '#fff',
              border: 'none', borderRadius: '8px',
              padding: '8px 18px', fontSize: '0.79rem', fontWeight: '600',
              cursor: needsClarification ? 'not-allowed' : 'pointer', fontFamily: FONT,
              display: 'flex', alignItems: 'center', gap: '6px',
              opacity: needsClarification ? 0.55 : 1,
            }}
          >
            <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round"><polyline points="20 6 9 17 4 12"/></svg>
            Approve &amp; Continue
          </button>

          {/* Save as Workflow Draft — only for workflow proposals with no clarification needed */}
          {onSaveDraft && !needsClarification && (
            <button
              onClick={handleDraftClick}
              disabled={draftSaving || draftMsg?.ok}
              title="Save this plan as a reusable workflow draft without running it now"
              style={{
                background: 'transparent',
                color: draftMsg?.ok ? '#10b981' : '#10b981',
                border: '1px solid #10b98130',
                borderRadius: '8px',
                padding: '8px 16px', fontSize: '0.79rem', fontWeight: '500',
                cursor: (draftSaving || draftMsg?.ok) ? 'default' : 'pointer',
                fontFamily: FONT,
                display: 'flex', alignItems: 'center', gap: '6px',
                opacity: (draftSaving || draftMsg?.ok) ? 0.65 : 1,
              }}
            >
              <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                <path d="M19 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11l5 5v11a2 2 0 0 1-2 2z"/><polyline points="17 21 17 13 7 13 7 21"/><polyline points="7 3 7 8 15 8"/>
              </svg>
              {draftSaving ? 'Saving…' : draftMsg?.ok ? 'Draft Saved' : 'Save as Workflow Draft'}
            </button>
          )}

          <button
            onClick={onEdit}
            style={{
              background: C.surface, color: C.textSec,
              border: `1px solid ${C.border}`, borderRadius: '8px',
              padding: '8px 16px', fontSize: '0.79rem', fontWeight: '500',
              cursor: 'pointer', fontFamily: FONT,
            }}
          >
            Edit Intent
          </button>
          <button
            onClick={onClear}
            style={{
              background: 'transparent', color: C.textMuted,
              border: 'none', borderRadius: '8px',
              padding: '8px 12px', fontSize: '0.79rem',
              cursor: 'pointer', fontFamily: FONT, marginLeft: 'auto',
            }}
          >
            Clear
          </button>
        </div>
      </div>
    </div>
  )
}
