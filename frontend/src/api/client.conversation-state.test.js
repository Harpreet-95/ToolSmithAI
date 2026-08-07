// Milestone M-32 — proves askComposer needs no functional change: it is
// already a pure passthrough (payload -> POST body, response body ->
// return value, unfiltered), so conversation_state on the request and
// conversation_state/agent_status/agent_trace on the response are already
// preserved without any modification to this function.
import { describe, it, expect, vi, afterEach } from 'vitest'
import { askComposer } from './client'

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

describe('askComposer', () => {
  it('sends conversation_state in the request body when included in the payload', async () => {
    const fetchMock = mockFetchOnce(200, { status: 'success' })
    const conversationState = { conversation_id: 'conv-1', source_id: 1, user_id: 'user-1', turn_number: 1 }

    await askComposer('tok', {
      session_id: 'conv-1', message: 'What about last quarter?', selected_data_source: 1,
      conversation_state: conversationState,
    })

    const [, options] = fetchMock.mock.calls[0]
    const body = JSON.parse(options.body)
    expect(body.conversation_state).toEqual(conversationState)
  })

  it('does not send conversation_state when the caller omits it (existing callers unaffected)', async () => {
    const fetchMock = mockFetchOnce(200, { status: 'success' })
    await askComposer('tok', { session_id: 's1', message: 'show me the dictionary', selected_data_source: 1 })

    const [, options] = fetchMock.mock.calls[0]
    const body = JSON.parse(options.body)
    expect(body).not.toHaveProperty('conversation_state')
  })

  it('preserves conversation_state, agent_status, and agent_trace on the response, unfiltered', async () => {
    const responseBody = {
      status: 'success',
      agent_status: 'answered',
      agent_trace: [{ step: 1, tool: 'resolve_intent', status: 'ok', input_summary: 'x', output_summary: 'y' }],
      conversation_state: { conversation_id: 'conv-1', source_id: 1, user_id: 'user-1', turn_number: 2 },
    }
    mockFetchOnce(200, responseBody)

    const result = await askComposer('tok', { session_id: 'conv-1', message: 'q', selected_data_source: 1 })

    expect(result.agent_status).toBe('answered')
    expect(result.agent_trace).toEqual(responseBody.agent_trace)
    expect(result.conversation_state).toEqual(responseBody.conversation_state)
  })

  it('still throws on a genuine error response, same as before', async () => {
    mockFetchOnce(500, { message: 'Internal orchestration failure' })
    await expect(
      askComposer('tok', { session_id: 's1', message: 'q', selected_data_source: 1 }),
    ).rejects.toThrow(/Internal orchestration failure/)
  })
})
