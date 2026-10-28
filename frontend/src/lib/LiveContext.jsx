/**
 * One live poll for the whole UI: GET /api/live/ every 5 s while the app is open.
 * Overview, Over-Hot banner, throttle card, fan automation and the Takt-Tabellen run status all read
 * from here, so every page shows the same values at the same time.
 */
import api from '@/lib/api'
import { createContext, useCallback, useContext, useEffect, useRef, useState } from 'react'

export const LIVE_REFRESH_MS = 5000
// a request without an answer (e.g. while the server is updated) must not stop the poll for good (Nutzer 28.10.)
export const LIVE_TIMEOUT_MS = 15000

const LiveContext = createContext(null)

export function LiveProvider({ children }) {
  const [state, setState] = useState({ data: null, error: null, loadedAt: null })
  const busy = useRef(false)

  const refresh = useCallback(async () => {
    if (busy.current) return
    busy.current = true
    try {
      const { data } = await api.get('/api/live/', { timeout: LIVE_TIMEOUT_MS })
      setState({ data, error: null, loadedAt: Date.now() })
    } catch (error) {
      setState((prev) => ({ ...prev, error }))
    } finally {
      busy.current = false
    }
  }, [])

  useEffect(() => {
    refresh()
    const timer = setInterval(refresh, LIVE_REFRESH_MS)
    return () => clearInterval(timer)
  }, [refresh])

  return <LiveContext.Provider value={{ ...state, refresh }}>{children}</LiveContext.Provider>
}

/** { data, error, loadedAt, refresh }; data is null until the first answer. */
export function useLive() {
  return useContext(LiveContext) || { data: null, error: null, loadedAt: null, refresh: () => {} }
}
