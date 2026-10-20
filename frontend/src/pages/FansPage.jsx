/**
 * Lüfter (AC Infinity, Testmenü): Controller und Ports anzeigen, Einstellungen sichern/zurückschreiben und
 * testen, welcher Schreibweg die Lüfterstufe am echten Gerät wirklich ändert.
 */
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import api from '@/lib/api'
import { Fan, Loader2, RefreshCw } from 'lucide-react'
import { useCallback, useEffect, useState } from 'react'

const ACTIONS = {
  set: 'Stufe setzen',
  on: 'An (atType 2)',
  off: 'Aus (atType 1)',
}

function fmt(value, digits = 1, unit = '') {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return '—'
  return Number(value).toLocaleString('de-DE', { minimumFractionDigits: digits, maximumFractionDigits: digits }) + unit
}

function when(iso) {
  if (!iso) return '—'
  return new Date(iso).toLocaleString('de-DE', { day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit', second: '2-digit' })
}

function errText(err, fallback) {
  return err?.response?.data?.error || fallback
}

function Controller({ dev, backups, onReload }) {
  const fanPorts = dev.ports.filter((p) => p.port >= 1 && p.port <= 4)
  const [port, setPort] = useState(() => {
    const active = fanPorts.find((p) => p.speak > 0) || fanPorts[0]
    return active ? active.port : 1
  })
  const [speed, setSpeed] = useState(5)
  const [busy, setBusy] = useState(null)
  const [msg, setMsg] = useState(null)
  const [results, setResults] = useState([])
  const [confirmRestore, setConfirmRestore] = useState(null)
  const own = backups.filter((b) => b.dev_id === dev.dev_id)

  const backup = async () => {
    setBusy('backup')
    setMsg(null)
    try {
      const { data } = await api.post('/api/fans/backup/', { dev_id: dev.dev_id })
      const ok = Object.entries(data.ports).filter(([, v]) => v.ok).map(([p]) => p)
      setMsg({ tone: 'ok', text: `Sicherung ${data.file} gespeichert (Ports mit Einstellungen: ${ok.join(', ') || 'keine'}).` })
      onReload()
    } catch (err) {
      setMsg({ tone: 'error', text: errText(err, 'Sicherung fehlgeschlagen.') })
    } finally {
      setBusy(null)
    }
  }

  const restore = async (file) => {
    setBusy('restore')
    setMsg(null)
    setConfirmRestore(null)
    try {
      const { data } = await api.post('/api/fans/restore/', { file })
      setMsg({
        tone: 'ok',
        text: 'Zurückgeschrieben: ' + data.results.map((r) => `Port ${r.port}: Code ${r.code}${r.msg ? ' ' + r.msg : ''}`).join(' · '),
      })
      onReload()
    } catch (err) {
      setMsg({ tone: 'error', text: errText(err, 'Zurückschreiben fehlgeschlagen.') })
    } finally {
      setBusy(null)
    }
  }

  const test = async (action) => {
    setBusy(action)
    setMsg(null)
    const started = new Date().toISOString()
    try {
      const { data } = await api.post('/api/fans/test/', { dev_id: dev.dev_id, port, speed, action })
      setResults((prev) => [{ ...data, at: started }, ...prev])
      onReload()
    } catch (err) {
      setResults((prev) => [{ action, at: started, error: errText(err, 'Befehl fehlgeschlagen.') }, ...prev])
    } finally {
      setBusy(null)
    }
  }

  return (
    <Card>
      <CardHeader className="pb-2">
        <CardTitle className="flex items-center gap-2 text-base font-semibold">
          <Fan className="h-4 w-4 text-muted-foreground" />
          {dev.name || 'Controller'} · devType {dev.dev_type ?? '—'}
        </CardTitle>
        <CardDescription className="text-xs">
          {dev.online ? 'online' : 'offline'} · Temperatur {fmt(dev.temperature_c, 1, ' °C')} · Luftfeuchte{' '}
          {fmt(dev.humidity_pct, 1, ' %')} · VPD {fmt(dev.vpd_kpa, 2, ' kPa')}
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-4 pt-0">
        <div className="overflow-x-auto">
          <table className="w-full text-xs">
            <thead>
              <tr className="border-b border-border/60 text-left text-muted-foreground">
                <th className="py-1.5 pr-3 font-medium">Port</th>
                <th className="py-1.5 pr-3 font-medium">Name</th>
                <th className="py-1.5 pr-3 font-medium">Stufe (speak)</th>
                <th className="py-1.5 pr-3 font-medium">curMode</th>
                <th className="py-1.5 pr-3 font-medium">online</th>
              </tr>
            </thead>
            <tbody>
              {dev.ports.map((p) => (
                <tr key={p.port} className="border-b border-border/30">
                  <td className="py-1.5 pr-3">{p.port}</td>
                  <td className="py-1.5 pr-3">{p.name || '—'}</td>
                  <td className="py-1.5 pr-3 font-semibold tabular-nums">{p.speak ?? '—'}</td>
                  <td className="py-1.5 pr-3">{p.cur_mode ?? '—'}</td>
                  <td className="py-1.5 pr-3">{p.online === undefined || p.online === null ? '—' : String(p.online)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>

        <div className="space-y-2 rounded-md border border-border/80 p-3">
          <p className="text-sm font-medium">Sicherung (Einstellungen Port 0–4)</p>
          <Button size="sm" disabled={!!busy} onClick={backup}>
            {busy === 'backup' && <Loader2 className="mr-1.5 h-3.5 w-3.5 animate-spin" />}
            Sicherung erstellen
          </Button>
          {own.length === 0 ? (
            <p className="text-xs text-muted-foreground">Noch keine Sicherung. Die Test-Buttons sind erst danach freigegeben.</p>
          ) : (
            <ul className="space-y-1 text-xs">
              {own.map((b) => (
                <li key={b.file} className="flex flex-wrap items-center gap-2">
                  <span>
                    {when(b.created_at)} · Ports {b.ports.join(', ') || '—'}
                  </span>
                  {confirmRestore === b.file ? (
                    <>
                      <span>Wirklich zurückschreiben?</span>
                      <Button size="sm" variant="destructive" disabled={!!busy} onClick={() => restore(b.file)}>
                        Ja
                      </Button>
                      <Button size="sm" variant="secondary" disabled={!!busy} onClick={() => setConfirmRestore(null)}>
                        Nein
                      </Button>
                    </>
                  ) : (
                    <Button size="sm" variant="secondary" disabled={!!busy} onClick={() => setConfirmRestore(b.file)}>
                      Zurückschreiben
                    </Button>
                  )}
                </li>
              ))}
            </ul>
          )}
        </div>

        <div className="space-y-2 rounded-md border border-border/80 p-3">
          <p className="text-sm font-medium">Test</p>
          <div className="flex flex-wrap items-center gap-3 text-xs">
            <label className="flex items-center gap-1.5">
              Lüfter an Port
              <select className="rounded border border-border bg-background px-2 py-1" value={port}
                onChange={(e) => setPort(Number(e.target.value))} disabled={!!busy}>
                {[1, 2, 3, 4].map((p) => (
                  <option key={p} value={p}>{p}</option>
                ))}
              </select>
            </label>
            <label className="flex items-center gap-1.5">
              Stufe
              <select className="rounded border border-border bg-background px-2 py-1" value={speed}
                onChange={(e) => setSpeed(Number(e.target.value))} disabled={!!busy}>
                {[0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10].map((s) => (
                  <option key={s} value={s}>{s}</option>
                ))}
              </select>
            </label>
          </div>
          <div className="flex flex-wrap gap-2">
            {Object.entries(ACTIONS).map(([key, label]) => (
              <Button key={key} size="sm" variant={key === 'set' ? 'default' : 'secondary'}
                disabled={!!busy || own.length === 0} onClick={() => test(key)}>
                {busy === key && <Loader2 className="mr-1.5 h-3.5 w-3.5 animate-spin" />}
                {label}
              </Button>
            ))}
          </div>
          {busy && busy !== 'backup' && busy !== 'restore' && (
            <p className="text-xs text-muted-foreground">Befehl gesendet, warte 10 s und lese die Stufe neu …</p>
          )}
          <p className="text-xs text-muted-foreground">
            Zwischen zwei Schreibbefehlen liegen mindestens 1,5 s. Während der Tests kann die Handy-App abgemeldet werden.
          </p>
        </div>

        {msg && <p className={'text-xs ' + (msg.tone === 'ok' ? 'text-emerald-600' : 'text-destructive')}>{msg.text}</p>}

        {results.length > 0 && (
          <div className="space-y-2">
            <p className="text-sm font-medium">Ergebnisse</p>
            {results.map((r, i) => (
              <div key={i} className="space-y-1 rounded-md border border-border/60 p-2 text-xs">
                <p className="font-medium">
                  {when(r.at)} · {ACTIONS[r.action] || r.action}
                  {r.speed !== undefined && r.action === 'set' ? ` · Stufe ${r.speed}` : ''}
                </p>
                {r.error ? (
                  <p className="text-destructive">{r.error}</p>
                ) : (
                  <>
                    <p>
                      Übernommen:{' '}
                      <span className={'font-semibold ' + (r.accepted ? 'text-emerald-600' : 'text-destructive')}>
                        {r.accepted ? 'ja' : 'nein'}
                      </span>{' '}
                      · Stufe vorher {r.before ?? '—'} → nachher {r.after ?? '—'} (nach {r.waited_s} s, Port {r.fan_port})
                    </p>
                    <p className="text-muted-foreground">
                      API: Code {String(r.code)}
                      {r.msg ? ' · ' + r.msg : ''} · HTTP {r.http} · geschrieben auf Port {r.write_port}
                    </p>
                    <p className="text-muted-foreground">
                      Geändert:{' '}
                      {Object.entries(r.changes || {})
                        .map(([k, v]) => `${k} ${v.before ?? '—'} → ${v.sent}`)
                        .join(' · ')}
                    </p>
                  </>
                )}
              </div>
            ))}
          </div>
        )}
      </CardContent>
    </Card>
  )
}

function Credentials({ configured, onSaved }) {
  const [open, setOpen] = useState(!configured)
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [passwordSet, setPasswordSet] = useState(false)
  const [busy, setBusy] = useState(false)
  const [msg, setMsg] = useState(null)

  useEffect(() => {
    api.get('/api/fans/credentials/').then(({ data }) => {
      setEmail(data.email || '')
      setPasswordSet(!!data.password_set)
    }).catch(() => {})
  }, [])

  useEffect(() => {
    if (!configured) setOpen(true)
  }, [configured])

  const save = async (e) => {
    e.preventDefault()
    setBusy(true)
    setMsg(null)
    try {
      const { data } = await api.post('/api/fans/credentials/', { email, password })
      setPasswordSet(!!data.password_set)
      setPassword('')
      setMsg({ tone: 'ok', text: 'Gespeichert.' })
      onSaved()
    } catch (err) {
      setMsg({ tone: 'error', text: errText(err, 'Speichern fehlgeschlagen.') })
    } finally {
      setBusy(false)
    }
  }

  if (!open) {
    return (
      <div className="flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
        <span>AC-Infinity-Konto: {email || '—'}</span>
        <Button size="sm" variant="secondary" onClick={() => setOpen(true)}>
          Zugangsdaten bearbeiten
        </Button>
      </div>
    )
  }
  return (
    <Card>
      <CardHeader className="pb-2">
        <CardTitle className="text-base font-semibold">AC-Infinity-Zugangsdaten</CardTitle>
        <CardDescription className="text-xs">
          Dieselben Daten wie in der AC-Infinity-App. Gespeichert nur auf dem Server, das Passwort wird nie angezeigt.
          Die API nutzt höchstens die ersten 25 Zeichen des Passworts.
        </CardDescription>
      </CardHeader>
      <CardContent className="pt-0">
        <form onSubmit={save} className="flex flex-wrap items-end gap-3 text-xs">
          <label className="flex flex-col gap-1">
            E-Mail
            <input type="email" autoComplete="off" className="w-64 rounded border border-border bg-background px-2 py-1.5"
              value={email} onChange={(e) => setEmail(e.target.value)} required />
          </label>
          <label className="flex flex-col gap-1">
            Passwort
            <input type="password" autoComplete="new-password"
              className="w-56 rounded border border-border bg-background px-2 py-1.5" value={password}
              placeholder={passwordSet ? 'leer lassen = unverändert' : ''} onChange={(e) => setPassword(e.target.value)}
              required={!passwordSet} />
          </label>
          <Button size="sm" type="submit" disabled={busy}>
            {busy && <Loader2 className="mr-1.5 h-3.5 w-3.5 animate-spin" />}
            Speichern
          </Button>
          {configured && (
            <Button size="sm" type="button" variant="secondary" disabled={busy} onClick={() => setOpen(false)}>
              Schließen
            </Button>
          )}
          {msg && <span className={msg.tone === 'ok' ? 'text-emerald-600' : 'text-destructive'}>{msg.text}</span>}
        </form>
      </CardContent>
    </Card>
  )
}

export default function FansPage() {
  const [data, setData] = useState(null)
  const [error, setError] = useState(null)
  const [loading, setLoading] = useState(false)

  const load = useCallback(async () => {
    setLoading(true)
    try {
      const { data: d } = await api.get('/api/fans/status/')
      setData(d)
      setError(null)
    } catch (err) {
      setError(errText(err, 'AC Infinity nicht erreichbar.'))
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    load()
  }, [load])

  return (
    <div className="space-y-4">
      <div className="flex items-center gap-2">
        <Button size="sm" variant="secondary" disabled={loading} onClick={load}>
          {loading ? <Loader2 className="mr-1.5 h-3.5 w-3.5 animate-spin" /> : <RefreshCw className="mr-1.5 h-3.5 w-3.5" />}
          Neu laden
        </Button>
        {error && <span className="text-xs text-destructive">{error}</span>}
      </div>
      {data && <Credentials configured={!!data.configured} onSaved={load} />}
      {data?.configured && data.devices.length === 0 && (
        <p className="text-sm text-muted-foreground">Keine Controller im Konto gefunden.</p>
      )}
      {data?.devices.map((dev) => (
        <Controller key={dev.dev_id} dev={dev} backups={data.backups || []} onReload={load} />
      ))}
    </div>
  )
}
