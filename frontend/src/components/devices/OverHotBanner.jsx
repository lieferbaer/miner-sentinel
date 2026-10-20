/**
 * Shown on every page while a miner reports "Over Hot" (its hash boards are switched off).
 * Offers a restart that is only sent after the user confirms it.
 */
import { Button } from '@/components/ui/button'
import api from '@/lib/api'
import { AlertTriangle } from 'lucide-react'
import { useCallback, useEffect, useState } from 'react'

const REFRESH_MS = 15000

function OverHotDevice({ device }) {
  const [confirm, setConfirm] = useState(false)
  const [busy, setBusy] = useState(false)
  const [msg, setMsg] = useState(null)

  const restart = async () => {
    setBusy(true)
    setMsg(null)
    try {
      await api.post(`/api/devices/${device.id}/restart/`)
      setMsg({ tone: 'ok', text: 'Neustart gesendet. Der Miner braucht einige Minuten.' })
      setConfirm(false)
    } catch (err) {
      setMsg({ tone: 'error', text: err?.response?.data?.error || 'Neustart fehlgeschlagen.' })
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="flex flex-wrap items-center gap-3 rounded-md border border-destructive/50 bg-destructive/10 px-3 py-2">
      <AlertTriangle className="h-4 w-4 shrink-0 text-destructive" />
      <p className="text-sm">
        <span className="font-semibold">{device.name}</span> meldet „Over Hot“. Die Hashboards sind abgeschaltet.
      </p>
      {!confirm ? (
        <Button size="sm" variant="destructive" disabled={busy} onClick={() => setConfirm(true)}>
          Neustart
        </Button>
      ) : (
        <span className="flex items-center gap-2">
          <span className="text-sm">Wirklich neu starten?</span>
          <Button size="sm" variant="destructive" disabled={busy} onClick={restart}>
            Ja, neu starten
          </Button>
          <Button size="sm" variant="secondary" disabled={busy} onClick={() => setConfirm(false)}>
            Nein
          </Button>
        </span>
      )}
      {msg && <span className={'text-xs ' + (msg.tone === 'ok' ? 'text-emerald-600' : 'text-destructive')}>{msg.text}</span>}
    </div>
  )
}

export default function OverHotBanner() {
  const [devices, setDevices] = useState([])

  const load = useCallback(async () => {
    try {
      const { data } = await api.get('/api/devices/system-latest/')
      setDevices(
        Object.values(data || {}).filter((d) => d && d.id && /over\s*hot/i.test(String(d.system_status || ''))),
      )
    } catch {
      /* keep the last state; the next poll retries */
    }
  }, [])

  useEffect(() => {
    load()
    const timer = setInterval(load, REFRESH_MS)
    return () => clearInterval(timer)
  }, [load])

  if (!devices.length) return null
  return (
    <div className="mb-4 space-y-2">
      {devices.map((d) => (
        <OverHotDevice key={d.id} device={d} />
      ))}
    </div>
  )
}
