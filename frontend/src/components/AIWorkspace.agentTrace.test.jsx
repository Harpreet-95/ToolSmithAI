// Milestone M-32 — Frontend Agent Evidence / Trace rendering.
//
// Proves ComposerResultPanel's new, purely-additive "How this answer was
// prepared" section and agent-status display, using the same render-the-
// exported-subcomponent-directly convention as
// AIWorkspace.clarification.test.jsx.
import { describe, it, expect, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { ComposerResultPanel } from './AIWorkspace'

const C = {
  bg: '#f8f8fb', surface: '#ffffff', border: '#e2e2ec',
  text: '#111118', textSec: '#5c5c72', textMuted: '#9898b0',
}

function baseResult(overrides = {}) {
  return {
    resolved_intent: { intent_type: 'sql_request', confidence: 0.9, keywords_matched: [] },
    services_selected: ['live_query'],
    evidence_summary: {},
    evidence_package: { evidence: [] },
    status: 'success',
    warnings: [],
    errors: [],
    enterprise_answer: {
      answer: 'There are 42 active clients.', summary: 'Query executed successfully.',
      answer_type: 'live_query', confidence: 88, clarification: null,
    },
    ...overrides,
  }
}

function renderPanel(overrides = {}, props = {}) {
  return render(
    <ComposerResultPanel
      result={baseResult(overrides)} wsInput="how many clients" C={C}
      onBack={vi.fn()} onOpenReport={vi.fn()}
      onResolveClarification={vi.fn()} onCancelClarification={vi.fn()}
      clarificationResubmitted={false}
      {...props}
    />
  )
}

const SAMPLE_TRACE = [
  { step: 1, tool: 'resolve_intent', status: 'ok', input_summary: "question='how many clients'", output_summary: 'intent=sql_request confidence=0.90', reason_code: null, duration_ms: 1.2 },
  { step: 2, tool: 'apply_clarification_selection', status: 'ok', input_summary: 'conversation_id=conv-1 terms=1', output_summary: 'prior clarification selection applied', reason_code: null, duration_ms: null },
  { step: 3, tool: 'execute_sql', status: 'ok', input_summary: 'row_limit_applied=1000', output_summary: 'row_count=1 truncated=False', reason_code: null, duration_ms: 12.4 },
]

describe('ComposerResultPanel — agent trace ("How this answer was prepared")', () => {
  it('10. renders friendly labels for known trace tool names', () => {
    renderPanel({ agent_status: 'answered', agent_trace: SAMPLE_TRACE })
    expect(screen.getByText('Understood the question')).toBeInTheDocument()
    expect(screen.getByText('Applied your clarification')).toBeInTheDocument()
    expect(screen.getByText('Ran a read-only query')).toBeInTheDocument()
  })

  it('falls back to a humanized name for an unmapped tool rather than hiding the step', () => {
    renderPanel({
      agent_status: 'answered',
      agent_trace: [{ step: 1, tool: 'some_future_step', status: 'ok', input_summary: 'x', output_summary: 'y' }],
    })
    expect(screen.getByText('Some Future Step')).toBeInTheDocument()
  })

  it('11. never renders raw input_summary/output_summary/reason_code/duration_ms values', () => {
    renderPanel({ agent_status: 'answered', agent_trace: SAMPLE_TRACE })
    expect(screen.queryByText(/how many clients'/)).not.toBeInTheDocument()
    expect(screen.queryByText(/confidence=0\.90/)).not.toBeInTheDocument()
    expect(screen.queryByText(/row_limit_applied/)).not.toBeInTheDocument()
    expect(screen.queryByText(/row_count=1/)).not.toBeInTheDocument()
    expect(screen.queryByText(/conv-1/)).not.toBeInTheDocument()
    expect(screen.queryByText(/12\.4/)).not.toBeInTheDocument()
  })

  it('11b. never renders SQL text, credentials, or connection details even if present on the result', () => {
    renderPanel({
      agent_status: 'answered', agent_trace: SAMPLE_TRACE,
      // Defensive: even if a caller accidentally attached these fields to
      // the result object, ComposerResultPanel must never read/render them.
      generated_sql: 'SELECT * FROM dbo.active_clients WHERE password = \'secret\'',
      connection: { host: 'db.internal', password: 'hunter2' },
    })
    expect(screen.queryByText(/SELECT/)).not.toBeInTheDocument()
    expect(screen.queryByText(/db\.internal/)).not.toBeInTheDocument()
    expect(screen.queryByText(/hunter2/)).not.toBeInTheDocument()
  })

  it('12. the trace section is collapsed by default (a native <details> with no open attribute)', () => {
    renderPanel({ agent_status: 'answered', agent_trace: SAMPLE_TRACE })
    const summary = screen.getByText('How this answer was prepared')
    const details = summary.closest('details')
    expect(details).toBeInTheDocument()
    expect(details).not.toHaveAttribute('open')
  })

  it('expanding the section reveals the mapped steps (still no raw fields)', async () => {
    // Demo cleanup — the trace now lives inside the outer "Technical
    // details" disclosure, so revealing its steps takes two expansions:
    // the outer developer-info section, then the trace's own sub-toggle.
    const user = userEvent.setup()
    renderPanel({ agent_status: 'answered', agent_trace: SAMPLE_TRACE })
    await user.click(screen.getByText('Technical details'))
    await user.click(screen.getByText('How this answer was prepared'))
    expect(screen.getByText('Ran a read-only query')).toBeVisible()
  })

  it('renders nothing for agent_trace when the field is absent (legacy/non-agent response)', () => {
    renderPanel({}) // no agent_status/agent_trace at all
    expect(screen.queryByText('How this answer was prepared')).not.toBeInTheDocument()
  })

  it('15. a response without agent_trace/agent_status still renders the answer and legacy status exactly as before', () => {
    renderPanel({ status: 'success' })
    expect(screen.getByText('Enterprise Answer')).toBeInTheDocument()
    expect(screen.getByText('success')).toBeInTheDocument()
    expect(screen.queryByText('How this answer was prepared')).not.toBeInTheDocument()
  })
})

// Phase 7, Step 6E3E, Part 5 — truthful fast-path labels + plan-cache
// status display. Mirrors the describe block above's own render-the-
// exported-subcomponent-directly convention.
describe('ComposerResultPanel — fast-path trace label + plan cache status', () => {
  const FAST_PATH_TRACE = [
    { step: 1, tool: 'schema_frontier', status: 'ok', duration_ms: 50 },
    { step: 2, tool: 'ai_query_planning', status: 'ok', duration_ms: 1200 },
    { step: 3, tool: 'sql_validation', status: 'ok', duration_ms: 1 },
    { step: 4, tool: 'sql_execution', status: 'ok', duration_ms: 400 },
  ]

  it('labels the fast path\'s combined stage "AI query planning", never "Schema Selection"', async () => {
    const user = userEvent.setup()
    renderPanel({ agent_status: 'answered', agent_trace: FAST_PATH_TRACE })
    await user.click(screen.getByText('Technical details'))
    await user.click(screen.getByText('How this answer was prepared'))
    expect(screen.getByText(/AI query planning/)).toBeInTheDocument()
    expect(screen.queryByText('Selected relevant tables')).not.toBeInTheDocument()
  })

  it('shows "Plan cache: Hit" and an explicit live-execution note on a cache hit', async () => {
    const user = userEvent.setup()
    renderPanel({
      agent_status: 'answered', agent_trace: FAST_PATH_TRACE,
      plan_cache_status: 'HIT',
      plan_cache_note: 'The query plan (intent, schema, and SQL shape) was reused from cache. SQL was revalidated and executed live against current data for this request.',
    })
    await user.click(screen.getByText('Technical details'))
    expect(screen.getByText(/Plan cache: Hit/)).toBeInTheDocument()
    expect(screen.getByText(/executed live/)).toBeInTheDocument()
  })

  it('shows "Plan cache: Miss" on a cache miss', async () => {
    const user = userEvent.setup()
    renderPanel({
      agent_status: 'answered', agent_trace: FAST_PATH_TRACE,
      plan_cache_status: 'MISS',
      plan_cache_note: 'No matching cached query plan was found. One embedding call and one AI query-planning call were made.',
    })
    await user.click(screen.getByText('Technical details'))
    expect(screen.getByText(/Plan cache: Miss/)).toBeInTheDocument()
  })

  it('renders no plan-cache block when plan_cache_status is absent (e.g. a not-enabled or state-reset response)', async () => {
    const user = userEvent.setup()
    renderPanel({ agent_status: 'answered', agent_trace: FAST_PATH_TRACE })
    await user.click(screen.getByText('Technical details'))
    expect(screen.queryByText(/Plan cache:/)).not.toBeInTheDocument()
  })

  it('never implies database RESULTS came from cache on a hit', async () => {
    const user = userEvent.setup()
    renderPanel({
      agent_status: 'answered', agent_trace: FAST_PATH_TRACE,
      plan_cache_status: 'HIT',
      plan_cache_note: 'The query plan (intent, schema, and SQL shape) was reused from cache. SQL was revalidated and executed live against current data for this request.',
    })
    await user.click(screen.getByText('Technical details'))
    expect(screen.queryByText(/results? (came|returned) from cache/i)).not.toBeInTheDocument()
  })
})

describe('ComposerResultPanel — agent_status controlled states', () => {
  it('13/14. renders "Validation Issue" (not a raw red error) for validation_failed', () => {
    renderPanel({
      agent_status: 'validation_failed', agent_trace: [],
      warnings: ['Expected column(s) missing from the execution result: count_id.'],
    })
    expect(screen.getByText('Validation Issue')).toBeInTheDocument()
    expect(screen.getByText(/Expected column\(s\) missing/)).toBeInTheDocument()
  })

  it('14. renders "Governance Blocked" as a controlled state, not a generic failure', () => {
    renderPanel({ agent_status: 'governance_blocked', agent_trace: [] })
    expect(screen.getByText('Governance Blocked')).toBeInTheDocument()
  })

  it('renders "Safely Refused" for a safely_refused agent status', () => {
    renderPanel({ agent_status: 'safely_refused', agent_trace: [] })
    expect(screen.getByText('Safely Refused')).toBeInTheDocument()
  })

  it('renders "Step Limit Reached" for a step_limit_reached agent status', () => {
    renderPanel({ agent_status: 'step_limit_reached', agent_trace: [] })
    expect(screen.getByText('Step Limit Reached')).toBeInTheDocument()
  })

  it('an actual execution_failed still reads as a failure state', () => {
    renderPanel({ agent_status: 'execution_failed', agent_trace: [] })
    expect(screen.getByText('Execution Failed')).toBeInTheDocument()
  })

  it('13. warnings render regardless of agent_status', () => {
    renderPanel({ agent_status: 'answered', warnings: ['not ranked as requested'] })
    expect(screen.getByText('not ranked as requested')).toBeInTheDocument()
  })
})

describe('ComposerResultPanel — inline follow-up box', () => {
  it('shows the follow-up box only when the response carries conversation_state', () => {
    renderPanel(
      { agent_status: 'answered', conversation_state: { conversation_id: 'c1', source_id: 1, user_id: 'u1' } },
      { onAskFollowUp: vi.fn() },
    )
    expect(screen.getByLabelText('Ask a follow-up')).toBeInTheDocument()
  })

  it('hides the follow-up box when there is no conversation_state to continue (e.g. a non-SQL answer)', () => {
    renderPanel({}, { onAskFollowUp: vi.fn() })
    expect(screen.queryByLabelText('Ask a follow-up')).not.toBeInTheDocument()
  })

  it('submits the typed follow-up text via onAskFollowUp and clears the box', async () => {
    const user = userEvent.setup()
    const onAskFollowUp = vi.fn()
    renderPanel(
      { agent_status: 'answered', conversation_state: { conversation_id: 'c1', source_id: 1, user_id: 'u1' } },
      { onAskFollowUp },
    )
    const input = screen.getByLabelText('Ask a follow-up')
    await user.type(input, 'What about last quarter?')
    await user.click(screen.getByRole('button', { name: 'Ask' }))
    expect(onAskFollowUp).toHaveBeenCalledWith('What about last quarter?')
    expect(input).toHaveValue('')
  })

  it('never shows the follow-up box on a clarification card', () => {
    renderPanel(
      {
        conversation_state: { conversation_id: 'c1', source_id: 1, user_id: 'u1' },
        enterprise_answer: {
          answer: 'Which clients table?', summary: 'Clarification needed.',
          answer_type: 'clarification_needed', confidence: 0,
          clarification: { reason: 'ambiguous', options: [], expected_impact: '' },
        },
      },
      { onAskFollowUp: vi.fn() },
    )
    expect(screen.queryByLabelText('Ask a follow-up')).not.toBeInTheDocument()
  })
})

// Phase 7, Step 6F, Part 6 — Technical Details shows the resolved
// standalone question for a genuine follow-up, never for a fresh question,
// and never a row value. Also: truthful badges for the new reset/limit/
// too-long agent statuses this step introduced.
describe('ComposerResultPanel — follow-up resolution display', () => {
  it('shows the resolution type and resolved standalone question for a genuine follow-up', async () => {
    const user = userEvent.setup()
    renderPanel({
      agent_status: 'answered',
      follow_up_resolution: { resolution_type: 'continuation', resolved_standalone_question: 'How many active widgets are there?' },
    })
    await user.click(screen.getByText('Technical details'))
    expect(screen.getByText('Continuation')).toBeInTheDocument()
    expect(screen.getByText(/How many active widgets are there\?/)).toBeInTheDocument()
  })

  it('renders nothing for a fresh question (resolution_type "none")', async () => {
    const user = userEvent.setup()
    renderPanel({
      agent_status: 'answered',
      follow_up_resolution: { resolution_type: 'none', resolved_standalone_question: 'How many widgets are there?' },
    })
    await user.click(screen.getByText('Technical details'))
    expect(screen.queryByText('Follow-up Resolution')).not.toBeInTheDocument()
  })

  it('renders nothing when follow_up_resolution is absent (older multi-call pipeline)', async () => {
    const user = userEvent.setup()
    renderPanel({ agent_status: 'answered' })
    await user.click(screen.getByText('Technical details'))
    expect(screen.queryByText('Follow-up Resolution')).not.toBeInTheDocument()
  })

  it('never renders a row value even if accidentally present elsewhere on the result', async () => {
    const user = userEvent.setup()
    renderPanel({
      agent_status: 'answered',
      follow_up_resolution: { resolution_type: 'continuation', resolved_standalone_question: 'Only active widgets.' },
      rows: [['Acme Corp', 42]], columns: ['name', 'count'],
    })
    await user.click(screen.getByText('Technical details'))
    expect(screen.queryByText(/Acme Corp/)).not.toBeInTheDocument()
  })
})

describe('ComposerResultPanel — reset/limit/too-long agent statuses render a truthful, non-error badge', () => {
  it.each([
    ['state_reset', 'Context Reset'],
    ['clarification_limit_reached', 'Needs a Full Question'],
    ['question_too_long', 'Question Too Long'],
  ])('renders "%s" as "%s", never as a generic error', (agentStatus, label) => {
    renderPanel({ agent_status: agentStatus, enterprise_answer: { answer: 'Please restate your question.', summary: '', answer_type: 'live_query', confidence: 0, clarification: null } })
    expect(screen.getByText(label)).toBeInTheDocument()
  })
})
