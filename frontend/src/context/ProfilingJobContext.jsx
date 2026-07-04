import { createContext, useContext, useRef, useState } from 'react'
import {
  startBatchProfile,
  continueBatchProfile,
  cancelBatchProfile,
  getActiveBatchProfile,
  getProfileReviewTasks,
} from '../api/client'

const ProfilingJobContext = createContext(null)

// ─── Provider ─────────────────────────────────────────────────────────────────
// Owns the entire lifecycle of a profiling job:
//   • polling loop (continues even when user navigates away)
//   • recovery after browser refresh
//   • cancel support
//   • completion tracking
//   • duplicate-job prevention
//
// DataSourceManager only STARTS a job — it delegates everything else here.

export function ProfilingJobProvider({ token, children }) {
  // { [sourceId]: { loading, snapshotId, progress, total, statistical, structural,
  //                  error, profileMode, startedAt, recovered } }
  const [jobs, setJobs] = useState({})

  // { [sourceId]: { ok, mode, sourceName, tables, statistical, structural,
  //                  duration, reviewCount, completedAt, error } | null }
  const [lastCompleted, setLastCompleted] = useState({})

  const [jobCenterOpen, setJobCenterOpen] = useState(true)

  // Incremented on each completion — triggers ColumnProfileExplorer refresh
  const [profileRefreshKey, setProfileRefreshKey] = useState({})

  // Mutable flags — same pattern as the original DataSourceManager refs
  const cancelRequestedRef = useRef({})  // { [id]: true }
  const recoveredJobRef    = useRef({})  // { [id]: true }

  // DataSourceManager's local notify() registered while it is mounted
  const notifyRef = useRef(null)

  // DataSourceManager registers post-completion callbacks (loadProfile + loadProfileHistory)
  const completionCallbacksRef = useRef({})  // { [id]: () => void }

  // ── Registration API used by DataSourceManager ──────────────────────────────

  function registerNotify(fn)    { notifyRef.current = fn }
  function unregisterNotify()    { notifyRef.current = null }

  function registerCompletionCallback(id, fn)  { completionCallbacksRef.current[id] = fn }
  function unregisterCompletionCallback(id)    { delete completionCallbacksRef.current[id] }

  // ── Internal helpers ────────────────────────────────────────────────────────

  function _callCompletion(id) {
    completionCallbacksRef.current[id]?.()
  }

  // ── Core polling loop (shared between startProfiling + recoverActiveJob) ────

  async function _runPollingLoop(id, snapshotId, initialProgress, totalTables) {
    let isComplete      = false
    let lastProgress    = initialProgress
    let lastStatistical = 0
    let lastStructural  = 0

    while (!isComplete && !cancelRequestedRef.current[id]) {
      // eslint-disable-next-line no-await-in-loop
      await new Promise(r => setTimeout(r, 50))
      if (cancelRequestedRef.current[id]) break

      // eslint-disable-next-line no-await-in-loop
      const contResp = await continueBatchProfile(id, snapshotId, token)
      const cont = contResp?.data
      isComplete      = cont?.is_complete ?? false
      const cancelled = cont?.status === 'CANCELLED'
      lastProgress    = cont?.completed_tables             ?? lastProgress
      lastStatistical = cont?.statistical_tables_completed ?? lastStatistical
      lastStructural  = cont?.structural_tables_completed  ?? lastStructural

      if (cancelled) {
        setJobs(s => ({ ...s, [id]: { ...(s[id] ?? {}), loading: false, error: 'Profiling cancelled.' } }))
        setLastCompleted(s => ({ ...s, [id]: { ok: false, error: 'Profiling cancelled.' } }))
        recoveredJobRef.current[id] = false
        notifyRef.current?.('Profiling cancelled.')
        _callCompletion(id)
        return { outcome: 'cancelled' }
      }

      setJobs(s => ({
        ...s,
        [id]: {
          ...s[id],
          loading:     !isComplete,
          progress:    lastProgress,
          total:       cont?.total_tables ?? totalTables,
          statistical: lastStatistical,
          structural:  lastStructural,
        },
      }))
    }

    if (cancelRequestedRef.current[id]) return { outcome: 'cancelled' }

    return { outcome: 'complete', lastProgress, lastStatistical, lastStructural }
  }

  // ── Public actions ──────────────────────────────────────────────────────────

  async function startProfiling(src, profileMode = 'FULL', max_tables = 0) {
    const id = src.id

    // Prevent duplicate jobs — the button is already disabled, but belt-and-suspenders
    if (jobs[id]?.loading) return

    cancelRequestedRef.current[id] = false
    recoveredJobRef.current[id]    = false
    setLastCompleted(s => ({ ...s, [id]: null }))

    const startedAt = Date.now()
    setJobs(s => ({
      ...s,
      [id]: {
        loading: true, snapshotId: null, progress: 0, total: 0,
        statistical: 0, structural: 0, error: null,
        profileMode, startedAt,
        sourceName: src.display_name ?? `Source #${id}`,
      },
    }))
    setJobCenterOpen(true)

    try {
      const startResp  = await startBatchProfile(id, token, { mode: profileMode, max_tables })
      const snap       = startResp?.data
      const snapshotId = snap?.profiling_snapshot_id
      const total      = snap?.total_tables ?? 0

      setJobs(s => ({ ...s, [id]: { ...s[id], snapshotId, total } }))

      // Handle degenerate case where total === 0 or already complete
      const alreadyDone = total === 0 || (snap?.next_table_index != null && snap.next_table_index >= total)

      const { outcome, lastProgress = 0, lastStatistical = 0, lastStructural = 0 } =
        alreadyDone
          ? { outcome: 'complete', lastProgress: total, lastStatistical: 0, lastStructural: 0 }
          : await _runPollingLoop(id, snapshotId, 0, total)

      if (outcome === 'cancelled') return 'cancelled'

      const duration = Math.round((Date.now() - startedAt) / 1000)
      let reviewCount = null
      try {
        const revResp = await getProfileReviewTasks(id, token, { limit: 1, offset: 0 })
        reviewCount = revResp?.data?.total ?? null
      } catch { /* best-effort */ }

      setProfileRefreshKey(s => ({ ...s, [id]: (s[id] ?? 0) + 1 }))
      setLastCompleted(s => ({
        ...s,
        [id]: {
          ok: true, mode: profileMode,
          sourceName:  src.display_name ?? `Source #${id}`,
          tables:      total,
          statistical: lastStatistical,
          structural:  lastStructural,
          duration, reviewCount,
          completedAt: new Date().toISOString(),
        },
      }))
      const msg = reviewCount > 0
        ? `Profiling completed. ${reviewCount} review tasks generated.`
        : 'Profiling completed.'
      notifyRef.current?.(msg)
      _callCompletion(id)
      return 'completed'
    } catch (e) {
      const msg = e?.message ?? 'Data profiling failed.'
      setJobs(s => ({ ...s, [id]: { ...(s[id] ?? {}), loading: false, error: msg } }))
      setLastCompleted(s => ({ ...s, [id]: { ok: false, error: msg } }))
      notifyRef.current?.(msg, false)
      _callCompletion(id)
      return 'error'
    }
  }

  // Called by DataSourceManager on source-select to restore any job that survived
  // a page refresh (job running in the DB but not in React state).
  async function recoverActiveJob(id, sourceName) {
    if (jobs[id]?.loading) return  // already tracked in this context session

    try {
      const resp   = await getActiveBatchProfile(id, token)
      const active = resp?.data
      if (!active) return

      recoveredJobRef.current[id]    = true
      cancelRequestedRef.current[id] = false
      setJobCenterOpen(true)

      const snapshotId = active.profiling_snapshot_id
      const total      = active.total_tables ?? 0

      setJobs(s => ({
        ...s,
        [id]: {
          loading:     true,
          snapshotId,
          progress:    active.completed_tables ?? 0,
          total,
          statistical: 0,
          structural:  0,
          error:       null,
          profileMode: active.mode ?? 'FULL',
          startedAt:   active.started_at ? new Date(active.started_at).getTime() : Date.now(),
          recovered:   true,
          sourceName:  sourceName ?? `Source #${id}`,
        },
      }))

      const { outcome, lastProgress = 0, lastStatistical = 0, lastStructural = 0 } =
        await _runPollingLoop(id, snapshotId, active.completed_tables ?? 0, total)

      if (outcome === 'cancelled') return

      recoveredJobRef.current[id] = false

      const duration = active.started_at
        ? Math.round((Date.now() - new Date(active.started_at).getTime()) / 1000)
        : null

      let reviewCount = null
      try {
        const revResp = await getProfileReviewTasks(id, token, { limit: 1, offset: 0 })
        reviewCount = revResp?.data?.total ?? null
      } catch { /* best-effort */ }

      setProfileRefreshKey(s => ({ ...s, [id]: (s[id] ?? 0) + 1 }))
      setLastCompleted(s => ({
        ...s,
        [id]: {
          ok: true, mode: active.mode ?? 'FULL',
          sourceName:  sourceName ?? `Source #${id}`,
          tables:      total,
          statistical: lastStatistical,
          structural:  lastStructural,
          duration, reviewCount,
          completedAt: new Date().toISOString(),
        },
      }))
      const msg = reviewCount > 0
        ? `Profiling completed. ${reviewCount} review tasks generated.`
        : 'Profiling completed.'
      notifyRef.current?.(msg)
      _callCompletion(id)
    } catch { /* silently ignore — user can start a fresh job */ }
  }

  async function cancelJob(sourceId) {
    const snapId = jobs[sourceId]?.snapshotId
    cancelRequestedRef.current[sourceId] = true

    if (snapId) {
      try { await cancelBatchProfile(sourceId, snapId, token) } catch { /* best-effort */ }
    }

    setJobs(s => ({ ...s, [sourceId]: { ...(s[sourceId] ?? {}), loading: false, error: 'Profiling cancelled.' } }))
    setLastCompleted(s => ({ ...s, [sourceId]: { ok: false, error: 'Profiling cancelled.' } }))
    recoveredJobRef.current[sourceId] = false
    notifyRef.current?.('Profiling cancelled.')
    _callCompletion(sourceId)
  }

  function dismissJob(sourceId) {
    setLastCompleted(s => ({ ...s, [sourceId]: null }))
    setJobs(s => {
      const cur = s[sourceId]
      if (!cur || cur.loading) return s
      return { ...s, [sourceId]: { ...cur, error: null } }
    })
  }

  // The single job to display in the floating center:
  //   Running job takes priority; otherwise the most recent terminal state.
  const activeJobId =
    Object.keys(jobs).find(id => jobs[id]?.loading) ??
    Object.keys(lastCompleted).find(id => lastCompleted[id] != null) ??
    null

  const value = {
    jobs,
    lastCompleted,
    jobCenterOpen, setJobCenterOpen,
    profileRefreshKey,
    activeJobId,
    cancelRequestedRef,
    recoveredJobRef,
    startProfiling,
    recoverActiveJob,
    cancelJob,
    dismissJob,
    registerNotify, unregisterNotify,
    registerCompletionCallback, unregisterCompletionCallback,
  }

  return (
    <ProfilingJobContext.Provider value={value}>
      {children}
    </ProfilingJobContext.Provider>
  )
}

export function useProfilingJob() {
  const ctx = useContext(ProfilingJobContext)
  if (!ctx) throw new Error('useProfilingJob must be used inside ProfilingJobProvider')
  return ctx
}
