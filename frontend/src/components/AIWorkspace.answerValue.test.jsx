import { describe, it, expect } from 'vitest'
import { render, screen } from '@testing-library/react'
import { ComposerResultPanel } from './AIWorkspace'
import { vi } from 'vitest'

// Minimal theme object matching the shape consumed by these components
// (see C_LIGHT in App.jsx) — only the keys these components actually read.
const C = {
  bg: '#f8f8fb', surface: '#ffffff', border: '#e2e2ec',
  text: '#111118', textSec: '#5c5c72', textMuted: '#9898b0',
}

function baseResult(enterpriseAnswer) {
  return {
    resolved_intent: { intent_type: 'live_query_answer', confidence: 0.9, keywords_matched: [] },
    services_selected: [], evidence_summary: {}, evidence_package: { evidence: [] },
    status: 'success', warnings: [], errors: [],
    enterprise_answer: enterpriseAnswer,
  }
}

function renderPanel(enterpriseAnswer) {
  render(
    <ComposerResultPanel
      result={baseResult(enterpriseAnswer)} wsInput="how many clients" C={C}
      onBack={vi.fn()} onOpenReport={vi.fn()}
      onResolveClarification={vi.fn()} onCancelClarification={vi.fn()}
      clarificationResubmitted={false}
    />
  )
}

describe('EnterpriseAnswerBlock — Milestone M-25 value rendering', () => {
  it('renders a scalar business-language answer with no result table', () => {
    renderPanel({
      answer: 'There are 2,218 clients.', summary: '2,218 clients.',
      answer_type: 'live_query', confidence: 95,
      actual_value: 2218, business_entity: 'clients', measure: null, aggregation: 'COUNT',
      applied_filters: [], date_context: null, result_preview: [],
      source_tables: ['dbo.Client'], source_columns: [],
      limitations: [], next_actions: [], citations: [],
    })
    expect(screen.getByText('There are 2,218 clients.')).toBeInTheDocument()
    expect(screen.queryByRole('table')).not.toBeInTheDocument()
  })

  it('renders a bounded result preview table for a grouped answer', () => {
    renderPanel({
      answer: 'Clients are grouped below by Region.', summary: 'Grouped by Region.',
      answer_type: 'live_query', confidence: 95,
      actual_value: null, business_entity: 'clients', measure: null, aggregation: 'COUNT',
      applied_filters: [], date_context: null,
      result_preview: [{ Region: 'West', clients: 10 }, { Region: 'East', clients: 5 }],
      source_tables: ['dbo.Client'], source_columns: ['dbo.Client.region'],
      limitations: [], next_actions: [], citations: [],
    })
    expect(screen.getByText('Clients are grouped below by Region.')).toBeInTheDocument()
    expect(screen.getByRole('table')).toBeInTheDocument()
    expect(screen.getByText('West')).toBeInTheDocument()
    expect(screen.getByText('East')).toBeInTheDocument()
  })

  it('renders applied filters and date context as pills', () => {
    renderPanel({
      answer: 'The total payroll is 1,240,550.', summary: 'Total payroll: 1,240,550.',
      answer_type: 'live_query', confidence: 95,
      actual_value: 1240550, business_entity: null, measure: 'payroll', aggregation: 'SUM',
      applied_filters: [{ label: 'Pay Date', operator: 'BETWEEN', value: ['2026-07-01', '2026-07-31'] }],
      date_context: { label: 'this month', start: '2026-07-01', end: '2026-07-31' },
      result_preview: [], source_tables: ['dbo.Payroll'], source_columns: ['dbo.Payroll.amount'],
      limitations: [], next_actions: [], citations: [],
    })
    expect(screen.getByText(/this month/)).toBeInTheDocument()
    expect(screen.getByText(/Pay Date/)).toBeInTheDocument()
  })

  it('hides raw table/column identifiers by default, revealing them only under Technical details', () => {
    renderPanel({
      answer: 'There are 42 clients.', summary: '42 clients.',
      answer_type: 'live_query', confidence: 95,
      actual_value: 42, business_entity: 'clients', measure: null, aggregation: 'COUNT',
      applied_filters: [], date_context: null, result_preview: [],
      source_tables: ['dbo.ADF_Clients'], source_columns: [],
      limitations: [], next_actions: [], citations: [],
    })
    expect(screen.getByText('There are 42 clients.')).toBeInTheDocument()
    // <details> is closed by default — the raw identifier is in the DOM
    // (as any collapsed disclosure content is) but not visible to the user.
    expect(screen.getByText('dbo.ADF_Clients')).not.toBeVisible()
    expect(screen.getByText('Technical details')).toBeInTheDocument()
  })

  it('does not render a Technical details section when no source tables/columns are present', () => {
    renderPanel({
      answer: 'No matching clients were found.', summary: 'No clients found.',
      answer_type: 'live_query', confidence: 95,
      actual_value: null, business_entity: 'clients', measure: null, aggregation: null,
      applied_filters: [], date_context: null, result_preview: [],
      source_tables: [], source_columns: [],
      limitations: [], next_actions: [], citations: [],
    })
    expect(screen.getByText('No matching clients were found.')).toBeInTheDocument()
    expect(screen.queryByText('Technical details')).not.toBeInTheDocument()
  })

  it('renders the live-query execution-failure state, not a zero-result answer', () => {
    // Response shape matches core.answering.explanation_builder._explain_live_query's
    // status != "success" branch, exercised backend-side by
    // tests/test_answering.py::test_execution_failure_not_shown_as_zero_result.
    renderPanel({
      answer: 'The live query did not complete successfully (status: failed). Connection timed out.',
      summary: 'Live query status: failed.',
      answer_type: 'live_query', confidence: 20,
      actual_value: null, business_entity: null, measure: null, aggregation: null,
      applied_filters: [], date_context: null, result_preview: [],
      source_tables: [], source_columns: [],
      limitations: ['Connection timed out.'], next_actions: [], citations: [],
    })
    expect(screen.getByText(/did not complete successfully/i)).toBeInTheDocument()
    expect(screen.getByText('Live query status: failed.')).toBeInTheDocument()
    expect(screen.getByText('20%')).toBeInTheDocument()
    expect(screen.queryByText(/no matching/i)).not.toBeInTheDocument()
    expect(screen.queryByRole('table')).not.toBeInTheDocument()
  })

  it('renders the empty-result state as a successful answer, not an execution error', () => {
    // Response shape matches result_formatter.build_business_answer's "empty"
    // shape (core/answering/result_formatter.py) for a query that executed
    // successfully but matched zero rows.
    renderPanel({
      answer: 'No matching orders were found.', summary: 'No orders found.',
      answer_type: 'live_query', confidence: 95,
      actual_value: null, business_entity: 'orders', measure: null, aggregation: null,
      applied_filters: [], date_context: null, result_preview: [],
      source_tables: ['dbo.Orders'], source_columns: [],
      limitations: [], next_actions: [], citations: [],
    })
    expect(screen.getByText('No matching orders were found.')).toBeInTheDocument()
    expect(screen.getByText('95%')).toBeInTheDocument()
    expect(screen.queryByText(/did not complete successfully/i)).not.toBeInTheDocument()
    expect(screen.queryByRole('table')).not.toBeInTheDocument()
  })

  it('renders a truncation notice when present', () => {
    renderPanel({
      answer: 'The top 10 clients by revenue are shown below. Results were truncated by the configured row limit.',
      summary: 'Top 10 clients.', answer_type: 'live_query', confidence: 95,
      actual_value: null, business_entity: 'clients', measure: 'revenue', aggregation: 'SUM',
      applied_filters: [], date_context: null,
      result_preview: [{ Name: 'Acme', Revenue: 5000 }],
      truncation_notice: 'Only the first 10 row(s) are shown; more matching rows exist.',
      source_tables: ['dbo.Client'], source_columns: [],
      limitations: ['Results were truncated.'], next_actions: [], citations: [],
    })
    expect(screen.getByText(/Only the first 10 row\(s\) are shown/)).toBeInTheDocument()
  })

  it('renders a clarification-resumed final answer through the same EnterpriseAnswerBlock, not a special UI', () => {
    renderPanel({
      answer: 'There are 87 active clients.', summary: '87 active clients.',
      answer_type: 'live_query', confidence: 95, clarification: null,
      actual_value: 87, business_entity: 'active clients', measure: null, aggregation: 'COUNT',
      applied_filters: [], date_context: null, result_preview: [],
      source_tables: ['dbo.ADF_Clients'], source_columns: [],
      limitations: [], next_actions: [], citations: [],
    })
    expect(screen.getByText('Enterprise Answer')).toBeInTheDocument()
    expect(screen.getByText('There are 87 active clients.')).toBeInTheDocument()
    expect(screen.queryByText('Clarification Needed')).not.toBeInTheDocument()
  })
})
