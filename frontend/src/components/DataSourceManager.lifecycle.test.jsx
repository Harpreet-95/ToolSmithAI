// Phase 7, Step 6H1, Part 6 — datasource lifecycle controls (disconnect /
// remove credentials / reconnect / permanently delete) wired into the
// frontend. Proves each button calls the correct backend endpoint and
// that disconnect/delete are never presented as equivalent.
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, waitFor, within } from '@testing-library/react'
import DataSourceManager from './DataSourceManager'

vi.mock('../api/client', () => ({
  listDataSources: vi.fn(),
  createDataSource: vi.fn(),
  deleteDataSource: vi.fn(),
  disconnectDataSource: vi.fn(),
  removeDataSourceCredentials: vi.fn(),
  reconnectDataSource: vi.fn(),
  testDataSource: vi.fn(),
  discoverDataSourceSchema: vi.fn(),
  executeDataSourceQuery: vi.fn(),
  explainTable: vi.fn(),
  generateDictionaryForSource: vi.fn(),
  generateDomainRuleSuggestions: vi.fn(),
  generateDomains: vi.fn(),
  generateEntities: vi.fn(),
  generateEntityRuleSuggestions: vi.fn(),
  getDomainRefinements: vi.fn(),
  getDomainRules: vi.fn(),
  getDomainSummary: vi.fn(),
  getDataSourceSchema: vi.fn(),
  getDownstreamLineage: vi.fn(),
  getEntityRules: vi.fn(),
  getEntitySummary: vi.fn(),
  getGovernanceAssignments: vi.fn(),
  getGovernanceAssignmentSummary: vi.fn(),
  getGovernanceBottlenecks: vi.fn(),
  getGovernanceDashboard: vi.fn(),
  getGovernanceExplanation: vi.fn(),
  getGovernancePolicies: vi.fn(),
  getGovernanceRecommendations: vi.fn(),
  getImpactAnalysis: vi.fn(),
  getKnowledgeGraphSummary: vi.fn(),
  getMetadataJob: vi.fn(),
  getProfile: vi.fn(),
  getProfileHistory: vi.fn(),
  getProfileReviewTasks: vi.fn(),
  getRelatedTables: vi.fn(),
  getSemanticSummary: vi.fn(),
  getSemanticTableProfile: vi.fn(),
  getUpstreamLineage: vi.fn(),
  listDictionaryTables: vi.fn(),
  listDomainAssignments: vi.fn(),
  listEntityAssignments: vi.fn(),
  rejectDomainRefinement: vi.fn(),
  rejectDomainRule: vi.fn(),
  rejectEntityRule: vi.fn(),
  runMetadataJob: vi.fn(),
  analyzeDomainRefinements: vi.fn(),
  approveDomainRefinement: vi.fn(),
  approveDomainRule: vi.fn(),
  approveEntityRule: vi.fn(),
  bulkGovernanceApprove: vi.fn(),
  bulkGovernanceDryRun: vi.fn(),
  bulkGovernanceReject: vi.fn(),
}))

import * as api from '../api/client'

vi.mock('../context/profilingJobState', () => ({
  useProfilingJob: () => ({
    jobs: {}, startJob: vi.fn(), cancelJob: vi.fn(),
    registerNotify: vi.fn(), unregisterNotify: vi.fn(),
  }),
}))

const ACTIVE_SOURCE = {
  id: 1, display_name: 'Active DB', source_type: 'mssql', source_status: 'ACTIVE',
  connection_state: 'ACTIVE', is_active: true, last_test_status: 'success',
  last_snapshot_id: null, metadata_job_id: null, config_summary: 'mssql://host/db', metadata: {},
  live_query_enabled: true, created_at: '2026-10-01T12:00:00Z', updated_at: '2026-10-08T12:00:00Z',
  schema_summary: { status: 'READY', schema_count: 4, table_count: 118, view_count: 26, column_count: 1432, discovered_at: '2026-10-08T11:00:00Z' },
}

const DISCONNECTED_SOURCE = {
  id: 2, display_name: 'Disconnected DB', source_type: 'mssql', source_status: 'ACTIVE',
  connection_state: 'DISCONNECTED', is_active: true, last_test_status: 'success',
  last_snapshot_id: null, metadata_job_id: null, config_summary: 'mssql://host2/db2', metadata: {},
}

function setup(sources) {
  api.listDataSources.mockResolvedValue({ data: sources })
  return render(<DataSourceManager token="test-token" />)
}

beforeEach(() => {
  vi.clearAllMocks()
})

describe('DataSourceManager lifecycle controls', () => {
  it('does not expose the retired manual Explore workspace', async () => {
    setup([ACTIVE_SOURCE])
    await waitFor(() => expect(screen.getByText('Active DB')).toBeInTheDocument())
    expect(screen.queryByRole('button', { name: 'Explore' })).not.toBeInTheDocument()
    expect(screen.queryByText('Schema')).not.toBeInTheDocument()
    expect(screen.queryByText('Dictionary')).not.toBeInTheDocument()
    expect(screen.queryByText('Governance')).not.toBeInTheDocument()
    expect(screen.getByText(/profiling.*run automatically/i)).toBeInTheDocument()
  })

  it('opens useful read-only database information by clicking the database row', async () => {
    setup([ACTIVE_SOURCE])
    await waitFor(() => expect(screen.getByText('Active DB')).toBeInTheDocument())

    fireEvent.click(screen.getByRole('button', { name: 'View database summary for Active DB' }))

    const information = screen.getByLabelText('Database information for Active DB')
    expect(information).toBeInTheDocument()
    expect(screen.getByText('Database Summary')).toBeInTheDocument()
    expect(within(information).getByText('mssql://host/db')).toBeInTheDocument()
    expect(within(information).getByText('Live questions')).toBeInTheDocument()
    expect(within(information).getByText('4')).toBeInTheDocument()
    expect(within(information).getByText('118')).toBeInTheDocument()
    expect(within(information).getByText('26')).toBeInTheDocument()
    expect(within(information).getByText('1,432')).toBeInTheDocument()
    expect(within(information).getByText('Ready for questions')).toBeInTheDocument()
    expect(screen.getByText(/credentials and secrets are never displayed/i)).toBeInTheDocument()
    expect(screen.queryByText('Schema')).not.toBeInTheDocument()
    expect(screen.queryByText('Governance')).not.toBeInTheDocument()
  })

  it('keeps the three-dot menu for actions without opening the database summary', async () => {
    setup([ACTIVE_SOURCE])
    await waitFor(() => expect(screen.getByText('Active DB')).toBeInTheDocument())

    fireEvent.click(screen.getByTitle('More actions'))

    expect(await screen.findByText('Test Connection')).toBeInTheDocument()
    expect(screen.queryByText('View Information')).not.toBeInTheDocument()
    expect(screen.queryByText('Database Summary')).not.toBeInTheDocument()
  })

  it('shows a Disconnected badge and never shows Disconnect/Remove Credentials for a disconnected source', async () => {
    setup([DISCONNECTED_SOURCE])
    await waitFor(() => expect(screen.getByText('Disconnected DB')).toBeInTheDocument())
    expect(screen.getByText('Disconnected')).toBeInTheDocument()
  })

  it('Disconnect menu item calls disconnectDataSource with the source id, never deleteDataSource', async () => {
    api.disconnectDataSource.mockResolvedValue({ data: { id: 1, connection_state: 'DISCONNECTED' } })
    setup([ACTIVE_SOURCE])
    await waitFor(() => expect(screen.getByText('Active DB')).toBeInTheDocument())

    fireEvent.click(screen.getByTitle('More actions'))
    fireEvent.click(await screen.findByText('Disconnect'))

    await waitFor(() => expect(api.disconnectDataSource).toHaveBeenCalledWith(1, null, 'test-token'))
    expect(api.deleteDataSource).not.toHaveBeenCalled()
  })

  it('Remove Credentials menu item calls removeDataSourceCredentials, never deleteDataSource', async () => {
    api.removeDataSourceCredentials.mockResolvedValue({ data: { id: 1, connection_state: 'CREDENTIALS_REMOVED' } })
    setup([ACTIVE_SOURCE])
    await waitFor(() => expect(screen.getByText('Active DB')).toBeInTheDocument())

    fireEvent.click(screen.getByTitle('More actions'))
    fireEvent.click(await screen.findByText('Remove Credentials'))

    await waitFor(() => expect(api.removeDataSourceCredentials).toHaveBeenCalledWith(1, null, 'test-token'))
    expect(api.deleteDataSource).not.toHaveBeenCalled()
  })

  it('a disconnected source shows Reconnect instead of Disconnect/Remove Credentials', async () => {
    setup([DISCONNECTED_SOURCE])
    await waitFor(() => expect(screen.getByText('Disconnected DB')).toBeInTheDocument())

    fireEvent.click(screen.getByTitle('More actions'))
    expect(await screen.findByText('Reconnect')).toBeInTheDocument()
    expect(screen.queryByText('Disconnect')).not.toBeInTheDocument()
    expect(screen.queryByText('Remove Credentials')).not.toBeInTheDocument()
  })

  it('Reconnect opens the form in reconnect mode and submits via reconnectDataSource, never createDataSource', async () => {
    api.reconnectDataSource.mockResolvedValue({ data: { id: 2, reconnect_matched_existing_source: true } })
    setup([DISCONNECTED_SOURCE])
    await waitFor(() => expect(screen.getByText('Disconnected DB')).toBeInTheDocument())

    fireEvent.click(screen.getByTitle('More actions'))
    fireEvent.click(await screen.findByText('Reconnect'))

    expect(await screen.findByText('Reconnect "Disconnected DB"')).toBeInTheDocument()
    expect(screen.getByText(/reuses compatible existing intelligence/)).toBeInTheDocument()

    fireEvent.change(screen.getByPlaceholderText('server.domain.com'), { target: { value: 'host2' } })
    fireEvent.change(screen.getByPlaceholderText('database_name'), { target: { value: 'db2' } })
    const usernameInput = screen.getByText('Username').parentElement.querySelector('input')
    fireEvent.change(usernameInput, { target: { value: 'svc' } })
    fireEvent.change(document.querySelector('input[type="password"]'), { target: { value: 'rotated-secret' } })

    const submit = screen.getByRole('button', { name: 'Reconnect' })
    fireEvent.click(submit)

    await waitFor(() => expect(api.reconnectDataSource).toHaveBeenCalled())
    expect(api.createDataSource).not.toHaveBeenCalled()
  })

  it('Permanently Delete requires an explicit second confirmation with destructive wording, and only then calls deleteDataSource', async () => {
    api.deleteDataSource.mockResolvedValue({ data: { id: 1, deleted: true } })
    setup([ACTIVE_SOURCE])
    await waitFor(() => expect(screen.getByText('Active DB')).toBeInTheDocument())

    fireEvent.click(screen.getByTitle('More actions'))
    fireEvent.click(await screen.findByText('Permanently Delete'))

    // First click only arms the confirmation -- must not delete yet.
    expect(api.deleteDataSource).not.toHaveBeenCalled()
    expect(await screen.findByText(/This permanently deletes all metadata.*cannot be undone/)).toBeInTheDocument()
    const confirmButton = screen.getByRole('button', { name: 'Permanently Delete?' })
    expect(confirmButton).toBeInTheDocument()

    fireEvent.click(confirmButton)
    await waitFor(() => expect(api.deleteDataSource).toHaveBeenCalledWith(1, 'test-token'))
  })

  it('Permanently Delete confirmation can be cancelled without calling deleteDataSource', async () => {
    setup([ACTIVE_SOURCE])
    await waitFor(() => expect(screen.getByText('Active DB')).toBeInTheDocument())

    fireEvent.click(screen.getByTitle('More actions'))
    fireEvent.click(await screen.findByText('Permanently Delete'))
    fireEvent.click(screen.getByText('Cancel'))

    expect(api.deleteDataSource).not.toHaveBeenCalled()
    expect(screen.queryByText(/This permanently deletes all metadata/)).not.toBeInTheDocument()
  })
})
