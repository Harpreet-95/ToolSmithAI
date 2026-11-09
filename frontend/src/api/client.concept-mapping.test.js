// Phase 3, Step 3 — promoteConceptMapping's confirmReplace/canonicalTerm
// extension and its 409 remembered_term_conflict handling.
import { describe, it, expect, vi, afterEach } from 'vitest'
import { promoteConceptMapping } from './client'

function mockFetchOnce(status, body) {
  const fetchMock = vi.fn().mockResolvedValue({
    ok: status >= 200 && status < 300,
    status,
    text: async () => JSON.stringify(body),
  })
  vi.stubGlobal('fetch', fetchMock)
  return fetchMock
}

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('promoteConceptMapping', () => {
  it('sends confirm_replace: false and canonical_term: null by default', async () => {
    const fetchMock = mockFetchOnce(200, { status: 'success', data: { created: true, mapping_id: 1 } })
    await promoteConceptMapping(1, { conceptTerm: 'clients', tableFqn: 'dbo.Clients' }, 'tok')

    const [, options] = fetchMock.mock.calls[0]
    const body = JSON.parse(options.body)
    expect(body).toEqual({
      concept_term: 'clients', table_fqn: 'dbo.Clients', column_name: null,
      canonical_term: null, confirm_replace: false,
    })
  })

  it('a normal (non-conflicting) remembered choice still succeeds', async () => {
    mockFetchOnce(200, { status: 'success', data: { created: true, mapping_id: 1 } })
    const result = await promoteConceptMapping(
      1, { conceptTerm: 'clients', tableFqn: 'dbo.Clients', canonicalTerm: 'customer' }, 'tok',
    )
    expect(result).toEqual({ status: 'success', data: { created: true, mapping_id: 1 } })
  })

  it('returns a 409 remembered_term_conflict body as data instead of throwing', async () => {
    mockFetchOnce(409, {
      status: 'conflict', reason: 'remembered_term_conflict', conflict: true,
      source_id: 1, synonym_term: 'client',
      existing_canonical_term: 'customer', requested_canonical_term: 'organization',
    })
    const result = await promoteConceptMapping(
      1, { conceptTerm: 'clients', tableFqn: 'dbo.Organizations', canonicalTerm: 'organization' }, 'tok',
    )
    expect(result.reason).toBe('remembered_term_conflict')
    expect(result.existing_canonical_term).toBe('customer')
    expect(result.requested_canonical_term).toBe('organization')
  })

  it('sends confirm_replace: true when replacing', async () => {
    const fetchMock = mockFetchOnce(200, { status: 'success', data: { created: false, replaced: true } })
    await promoteConceptMapping(
      1,
      { conceptTerm: 'clients', tableFqn: 'dbo.Organizations', canonicalTerm: 'organization', confirmReplace: true },
      'tok',
    )
    const [, options] = fetchMock.mock.calls[0]
    const body = JSON.parse(options.body)
    expect(body.confirm_replace).toBe(true)
    expect(body.canonical_term).toBe('organization')
  })

  it('still throws for a genuine (non-conflict) error status', async () => {
    mockFetchOnce(500, { status: 'error', message: 'Failed to record concept mapping.' })
    await expect(
      promoteConceptMapping(1, { conceptTerm: 'clients', tableFqn: 'dbo.Clients' }, 'tok'),
    ).rejects.toThrow(/Failed to record concept mapping/)
  })

  it('a 409 with a different reason still throws (not swallowed as a conflict)', async () => {
    mockFetchOnce(409, { status: 'error', message: 'Some other conflict.' })
    await expect(
      promoteConceptMapping(1, { conceptTerm: 'clients', tableFqn: 'dbo.Clients' }, 'tok'),
    ).rejects.toThrow(/Some other conflict/)
  })
})
