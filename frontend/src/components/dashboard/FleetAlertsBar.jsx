/**
 * Problem- und Highlight-Leiste aus der Original-Übersicht (MinerSentinel 1.2.0),
 * eingebunden über der Einzelübersicht je MinerGerät.
 */
import { Button } from '@/components/ui/button'
import { getEventMeta, isHighlightEvent, isProblemEvent } from '@/lib/activity'
import api from '@/lib/api'
import { AlertTriangle, Award, Bell } from 'lucide-react'
import { useCallback, useEffect, useState } from 'react'
import { useNavigate } from 'react-router-dom'

export default function FleetAlertsBar({ offlineCount = 0 }) {
  const navigate = useNavigate()
  const [openAlerts, setOpenAlerts] = useState([])
  const [openCount, setOpenCount] = useState(0)
  const [criticalCount, setCriticalCount] = useState(0)
  const [recentHighlights, setRecentHighlights] = useState([])
  const [collectorHealth, setCollectorHealth] = useState(null)

  const load = useCallback(async () => {
    const [activityRes, highlightsRes, statusRes] = await Promise.all([
      api.get('/api/activity/', { params: { open: 'true', kind: 'problem', limit: 8 } }).catch(() => ({ data: null })),
      api.get('/api/activity/', { params: { open: 'false', kind: 'highlight', limit: 5 } }).catch(() => ({ data: null })),
      api.get('/api/settings/collector/status/').catch(() => ({ data: null })),
    ])
    const problems = (activityRes.data?.results || []).filter(isProblemEvent)
    setOpenAlerts(problems)
    setOpenCount(activityRes.data?.open_count ?? problems.length)
    setCriticalCount(activityRes.data?.critical_open_count ?? 0)
    setRecentHighlights((highlightsRes.data?.results || []).filter(isHighlightEvent).slice(0, 3))
    setCollectorHealth(statusRes.data)
  }, [])

  useEffect(() => {
    load()
    // faster poll while criticals are open (60 s), else 2 min
    const timer = setInterval(load, criticalCount > 0 ? 60000 : 120000)
    return () => clearInterval(timer)
  }, [load, criticalCount])

  return (
    <>
      {/* Issues needing attention */}
      {(openCount > 0 || offlineCount > 0 || (collectorHealth && collectorHealth.reachable === false)) && (
        <div
          className={`rounded-xl border px-4 py-3 ${
            criticalCount > 0
              ? 'border-red-500/30 bg-red-500/5'
              : 'border-amber-500/30 bg-amber-500/5'
          }`}
        >
          <div className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
            <div className="flex gap-3">
              <AlertTriangle
                className={`mt-0.5 h-5 w-5 shrink-0 ${
                  criticalCount > 0 ? 'text-red-500' : 'text-amber-500'
                }`}
              />
              <div className="space-y-1">
                <p className="text-sm font-medium">
                  {criticalCount > 0
                    ? `${criticalCount} critical · ${openCount} needing attention`
                    : openCount > 0
                      ? `${openCount} issue${openCount === 1 ? '' : 's'} needing attention`
                      : offlineCount > 0
                        ? `${offlineCount} device${offlineCount === 1 ? '' : 's'} offline`
                        : 'Collector unreachable'}
                </p>
                {collectorHealth && collectorHealth.reachable === false && (
                  <p className="text-xs text-muted-foreground">
                    Data collector is not responding — check Docker / Settings → Collector.
                  </p>
                )}
                {collectorHealth?.reachable && collectorHealth.status === 'degraded' && (
                  <p className="text-xs text-muted-foreground">
                    Collector last cycle had errors
                    {collectorHealth.last_error ? `: ${collectorHealth.last_error}` : ''}.
                  </p>
                )}
                <ul className="space-y-1">
                  {openAlerts.slice(0, 5).map((ev) => {
                    const meta = getEventMeta(ev.event_type)
                    return (
                      <li key={ev.id} className="text-xs text-muted-foreground">
                        <span className="font-medium text-foreground/80">{ev.message}</span>
                        <span className="text-muted-foreground/70"> · {meta.label}</span>
                      </li>
                    )
                  })}
                </ul>
              </div>
            </div>
            <div className="flex shrink-0 flex-wrap gap-2">
              <Button size="sm" variant="outline" onClick={() => navigate('/activity')}>
                <Bell className="mr-1.5 h-3.5 w-3.5" />
                Activity
              </Button>
              {offlineCount > 0 && (
                <Button size="sm" variant="outline" onClick={() => navigate('/mining')}>
                  View devices
                </Button>
              )}
            </div>
          </div>
        </div>
      )}

      {/* Recent highlights (records / recoveries) — never framed as problems */}
      {recentHighlights.length > 0 && (
        <div className="rounded-xl border border-emerald-500/25 bg-emerald-500/5 px-4 py-3">
          <div className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
            <div className="flex gap-3">
              <Award className="mt-0.5 h-5 w-5 shrink-0 text-emerald-500" />
              <div className="space-y-1">
                <p className="text-sm font-medium text-emerald-800 dark:text-emerald-300">
                  Recent highlight{recentHighlights.length === 1 ? '' : 's'}
                </p>
                <ul className="space-y-1">
                  {recentHighlights.map((ev) => (
                    <li key={ev.id} className="text-xs text-muted-foreground">
                      <span className="font-medium text-foreground/85">{ev.message}</span>
                    </li>
                  ))}
                </ul>
              </div>
            </div>
            <Button size="sm" variant="outline" onClick={() => navigate('/activity')}>
              <Award className="mr-1.5 h-3.5 w-3.5" />
              Highlights
            </Button>
          </div>
        </div>
      )}
    </>
  )
}
