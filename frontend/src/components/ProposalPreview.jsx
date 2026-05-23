const FONT = "'Inter', 'SF Pro Display', system-ui, -apple-system, sans-serif"
const MONO = "'JetBrains Mono', 'Fira Code', 'Cascadia Code', monospace"

const RISK_COLOR = {
  low:    { text: '#10b981', bg: '#10b9811a', border: '#10b98130' },
  medium: { text: '#f59e0b', bg: '#f59e0b1a', border: '#f59e0b30' },
  high:   { text: '#f87171', bg: '#f871711a', border: '#f8717130' },
}

const TYPE_COLOR = {
  workflow:     { text: '#6366f1', bg: '#6366f11a' },
  dynamic_tool: { text: '#8b5cf6', bg: '#8b5cf61a' },
}

function StepCard({ step, C, idx }) {
  const isPrimitive = Boolean(step.primitive_type)
  const label = step.primitive_type || step.step_type || ''
  const purpose = step.purpose || label.replace(/_/g, ' ')
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
        <div style={{ fontSize: '0.76rem', fontWeight: '600', color: C.text, fontFamily: MONO, marginBottom: '2px' }}>
          {label}
        </div>
        <div style={{ fontSize: '0.72rem', color: C.textSec, lineHeight: 1.4 }}>{purpose}</div>
      </div>
      <div style={{
        fontSize: '0.62rem', fontWeight: '600', padding: '2px 7px', borderRadius: '4px', flexShrink: 0,
        background: isPrimitive ? C.accentSoft : C.surface,
        color: isPrimitive ? C.accent : C.textSec,
        border: `1px solid ${isPrimitive ? C.accent + '30' : C.border}`,
      }}>
        {isPrimitive ? 'primitive' : 'step'}
      </div>
    </div>
  )
}

export default function ProposalPreview({ proposal, C, onApprove, onEdit, onClear }) {
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
          <span style={{ fontSize: '0.68rem', color: C.textMuted, fontStyle: 'italic' }}>
            {proposal.source === 'ai_assisted' ? 'AI-assisted' : 'Rule-based'}
          </span>
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
          {proposal.suggested_name && (
            <div style={{ marginTop: '6px', display: 'flex', alignItems: 'center', gap: '6px' }}>
              <span style={{ fontSize: '0.68rem', color: C.textMuted }}>Suggested name:</span>
              <code style={{ fontSize: '0.72rem', color: C.accent, fontFamily: MONO, background: C.accentSoft, padding: '1px 6px', borderRadius: '4px' }}>
                {proposal.suggested_name}
              </code>
            </div>
          )}
        </div>

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

        {/* Execution preview */}
        {proposal.execution_preview && (
          <div>
            <div style={{ fontSize: '0.68rem', fontWeight: '600', color: C.textMuted, textTransform: 'uppercase', letterSpacing: '0.08em', marginBottom: '6px' }}>
              Execution Preview
            </div>
            <div style={{
              padding: '8px 12px', background: C.bg, borderRadius: '8px',
              border: `1px solid ${C.border}`, fontSize: '0.76rem', color: C.textSec, fontFamily: MONO,
            }}>
              {proposal.execution_preview}
            </div>
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
              Required Inputs at Runtime
            </div>
            <div style={{ display: 'flex', flexWrap: 'wrap', gap: '6px' }}>
              {inputs.map(inp => (
                <code key={inp} style={{
                  fontSize: '0.72rem', fontFamily: MONO, padding: '2px 8px', borderRadius: '4px',
                  background: C.borderAlt, color: C.text, border: `1px solid ${C.border}`,
                }}>
                  {inp}
                </code>
              ))}
            </div>
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
        <div style={{
          display: 'flex', alignItems: 'center', gap: '7px',
          padding: '8px 12px', background: C.accentSoft,
          border: `1px solid ${C.accent}25`, borderRadius: '8px',
        }}>
          <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke={C.accent} strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" style={{ flexShrink: 0 }}>
            <rect x="3" y="11" width="18" height="11" rx="2" ry="2"/><path d="M7 11V7a5 5 0 0 1 10 0v4"/>
          </svg>
          <span style={{ fontSize: '0.72rem', color: C.accent }}>
            Admin approval required before execution. This proposal has not been saved or executed.
          </span>
        </div>

        {/* Action row */}
        <div style={{ display: 'flex', alignItems: 'center', gap: '8px', paddingTop: '2px' }}>
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
