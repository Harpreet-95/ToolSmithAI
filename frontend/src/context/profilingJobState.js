import { createContext, useContext } from 'react'

export const ProfilingJobContext = createContext(null)

export function useProfilingJob() {
  const context = useContext(ProfilingJobContext)
  if (!context) throw new Error('useProfilingJob must be used inside ProfilingJobProvider')
  return context
}
