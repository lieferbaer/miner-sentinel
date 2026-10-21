/**
 * Shown on every page while a miner reports "Over Hot" (its hash boards are switched off).
 * Offers a restart that is only sent after the user confirms it.
 */
import { Button } from '@/components/ui/button'
import api from '@/lib/api'
import { useLive } from '@/lib/LiveContext'
import { AlertTriangle } from 'lucide-react'
import { useState } from 'react'

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
  const { data } = useLive()
  const devices = Object.values(data?.system || {}).filter(
    (d) => d && d.id && /over\s*hot/i.test(String(d.system_status || '')),
  )
  if (!devices.length) return null
  return (
    <div className="mb-4 space-y-2">
      {devices.map((d) => (
        <OverHotDevice key={d.id} device={d} />
      ))}
    </div>
  )
}
