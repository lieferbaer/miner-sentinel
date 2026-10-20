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
  set: 'Stufe',
  on: 'An',
  off: 'Aus',
}

// selected fan port per controller, kept when leaving the page
function storedPort(devId) {
  try {
    const value = Number(localStorage.getItem('fans.port.' + devId))
    return value >= 1 && value <= 4 ? value : null
  } catch {
    return null
  }
}

function storePort(devId, port) {
  try {
    localStorage.setItem('fans.port.' + devId, String(port))
  } catch {
    // no browser storage: the port is only kept while the page is open
  }
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

const PHASE = {
  normal: 'regelt über den Takt (Lüfter fest auf der gewählten Stufe)',
  senken: 'Lüfter wird gesenkt: erst Takt runter, bis 80 °C erreicht und 3 min stabil',
  messung: 'Takt-Messung läuft: kein Takt-Eingriff, der Lüfter hält 80 °C',
  aus: 'aus',
}

/** Temperatur-Automatik (A15): status and on/off; refreshed every 15 s without AC Infinity calls. */
function useAutomation(initial) {
  const [auto, setAuto] = useState(initial)
  useEffect(() => setAuto(initial), [initial])
  useEffect(() => {
    if (!auto?.enabled) return undefined
    const timer = setInterval(() => {
      api.get('/api/fans/automation/').then(({ data }) => setAuto(data)).catch(() => {})
    }, 15000)
    return () => clearInterval(timer)
  }, [auto?.enabled])
  return [auto, setAuto]
}

function AutomationPanel({ auto, here, port, level, locked, onCommand, busy }) {
  if (!auto) return null
  const st = auto.state || {}
  if (!auto.enabled) {
    return (
      <div className="space-y-2 rounded-md border border-border/80 p-3">
        <p className="text-sm font-medium">Temperatur-Automatik · {auto.device?.name}</p>
        <p className="text-xs text-muted-foreground">
          Lüfter fest auf der gewählten Stufe, der Takt hält den heißesten Chip bei 80–84 °C. Schutz ab 88 °C (Lüfter
          +1, Takt −30 MHz) und ab 90 °C (Lüfter +2).
        </p>
        <Button size="sm" disabled={locked || busy} onClick={() => onCommand({ action: 'enable', port, level })}>
          {busy && <Loader2 className="mr-1.5 h-3.5 w-3.5 animate-spin" />}
          Einschalten (Port {port}, Stufe {level})
        </Button>
      </div>
    )
  }
  return (
    <div className="space-y-1.5 rounded-md border border-emerald-500/60 bg-emerald-500/5 p-3 text-xs">
      <div className="flex flex-wrap items-center gap-2">
        <p className="text-sm font-medium">Temperatur-Automatik an · {auto.device?.name}</p>
        <Button size="sm" variant="outline" className="ml-auto h-8" disabled={busy}
          onClick={() => onCommand({ action: 'disable' })}>
          Ausschalten
        </Button>
      </div>
      {!here && <p className="text-amber-600">Steuert Port {auto.port} dieses Controllers.</p>}
      <p>Status: {PHASE[st.phase] || '—'}</p>
      {st.throttled && st.phase !== 'messung' && (
        <p className="text-amber-600">Drosselung gesetzt (Messung pausiert): kein Takt-Eingriff, nur Schutz über den Lüfter.</p>
      )}
      <p>
        Heißester Chip (60 s): <span className="font-semibold">{fmt(st.t, 1, ' °C')}</span> · Ziel 80–84 °C
      </p>
      <p>
        Takt: {fmt(st.clock_now, 0, ' MHz')} von {fmt(st.stock_mhz, 0, ' MHz')} (Standardtakt)
        {st.clock_set ? ' · zuletzt gesetzt ' + fmt(st.clock_set, 0, ' MHz') : ''}
      </p>
      <p>
        Lüfter: gewählt Stufe {auto.level} · läuft auf Stufe {st.fan_read ?? '—'}
        {st.phase === 'messung' && st.cal_fan ? ' · Messung: Stufe ' + st.cal_fan : ''}
        {st.boost ? ' · Schutz +' + st.boost : ''}
      </p>
      {st.ac_error && <p className="text-amber-600">AC Infinity: {st.ac_error}</p>}
      {st.last_error && <p className="text-destructive">Fehler: {st.last_error}</p>}
      {auto.log?.length > 0 && (
        <ul className="space-y-0.5 pt-1 text-muted-foreground">
          {auto.log.slice(0, 8).map(([at, text], i) => (
            <li key={i}>
              {when(at)} · {text}
            </li>
          ))}
        </ul>
      )}
    </div>
  )
}

function Controller({ dev, backups, automation, onReload }) {
  const fanPorts = dev.ports.filter((p) => p.port >= 1 && p.port <= 4)
  const [port, setPortState] = useState(() => {
    const stored = storedPort(dev.dev_id)
    if (stored) return stored
    const active = fanPorts.find((p) => p.speak > 0) || fanPorts[0]
    return active ? active.port : 1
  })
  const setPort = (value) => {
    setPortState(value)
    storePort(dev.dev_id, value)
  }
  const [auto, setAuto] = useAutomation(automation)
  const autoOn = auto?.enabled && auto.dev_id === dev.dev_id
  const autoHere = autoOn && auto.port === port
  const autoCommand = async (body) => {
    setBusy('auto')
    setMsg(null)
    try {
      const { data } = await api.post('/api/fans/automation/', { dev_id: dev.dev_id, ...body })
      setAuto(data)
    } catch (err) {
      setMsg({ tone: 'error', text: errText(err, 'Automatik-Befehl fehlgeschlagen.') })
    } finally {
      setBusy(null)
    }
  }
  // the highlighted level is the one the controller reports for the selected port
  const current = dev.ports.find((p) => p.port === port)?.speak
  const [pending, setPending] = useState(null)
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

  const test = async (action, speed = 0) => {
    setBusy(action)
    setPending(action === 'set' ? speed : null)
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
      setPending(null)
    }
  }
  const locked = !!busy || own.length === 0
  const enableLevel = current >= 1 && current <= 10 ? current : 5

  return (
    <Card>
      <CardHeader className="pb-2">
        <CardTitle className="flex flex-wrap items-center gap-2 text-base font-semibold">
          <Fan className="h-4 w-4 text-muted-foreground" />
          {dev.name || 'Controller'}
          <span className="ml-auto flex gap-2">
            {['on', 'off'].map((key) => (
              <Button key={key} size="sm" variant="outline" className="h-8 px-4" disabled={locked || autoOn}
                onClick={() => test(key)}>
                {busy === key && <Loader2 className="mr-1.5 h-3.5 w-3.5 animate-spin" />}
                {ACTIONS[key]}
              </Button>
            ))}
          </span>
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

        <AutomationPanel auto={auto} here={autoHere} port={port} level={enableLevel} locked={locked}
          busy={busy === 'auto'} onCommand={autoCommand} />

        <div className="space-y-2 rounded-md border border-border/80 p-3">
          <div className="flex flex-wrap items-center gap-1.5 text-xs">
            <span className="w-10">Port</span>
            {[1, 2, 3, 4].map((p) => (
              <Button key={p} size="sm" className="h-8 w-9 px-0 tabular-nums"
                variant={port === p ? 'default' : 'outline'} aria-pressed={port === p}
                disabled={!!busy || autoOn} onClick={() => setPort(p)}>
                {p}
              </Button>
            ))}
          </div>
          <div className="flex flex-wrap items-center gap-1.5 text-xs">
            <span className="w-10">Stufe</span>
            {[0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10].map((s) => {
              // automation on: the button chooses the level (the data-service applies it safely)
              const selected = autoHere ? auto.level === s : current === s
              return (
                <Button key={s} size="sm" className="h-8 w-9 px-0 tabular-nums"
                  variant={selected ? 'default' : 'outline'} aria-pressed={selected}
                  disabled={locked || (autoOn && (!autoHere || s === 0))}
                  onClick={() => (autoHere ? autoCommand({ action: 'level', level: s }) : test('set', s))}>
                  {pending === s ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : s}
                </Button>
              )
            })}
          </div>
          {own.length === 0 && (
            <p className="text-xs text-muted-foreground">Erst eine Sicherung erstellen, dann sind die Buttons frei.</p>
          )}
          {busy && busy !== 'backup' && busy !== 'restore' && (
            <p className="text-xs text-muted-foreground">Befehl gesendet, warte 5 s und lese die Stufe neu …</p>
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

const ENDED = {
  manual: 'Test beendet.',
  expired: 'Test nach 5 min automatisch beendet.',
  live: 'Live hat die Anmeldung übernommen – Test beendet.',
}

function TestSession({ session, onChanged }) {
  const [busy, setBusy] = useState(false)
  const [msg, setMsg] = useState(null)
  const [now, setNow] = useState(Date.now())
  const until = session.active && session.until ? new Date(session.until).getTime() : null

  useEffect(() => {
    if (!until) return undefined
    const timer = setInterval(() => setNow(Date.now()), 1000)
    return () => clearInterval(timer)
  }, [until])

  useEffect(() => {
    // the backend ends the session after 5 min; reload then so the page shows it
    if (until && now >= until + 1000) onChanged()
  }, [until, now, onChanged])

  const send = async (action) => {
    setBusy(true)
    setMsg(null)
    try {
      await api.post('/api/fans/session/', { action })
      onChanged()
    } catch (err) {
      setMsg(errText(err, 'Befehl fehlgeschlagen.'))
    } finally {
      setBusy(false)
    }
  }

  const left = until ? Math.max(0, Math.round((until - now) / 1000)) : 0
  return (
    <div className="flex flex-wrap items-center gap-2 rounded-md border border-amber-500/60 bg-amber-500/10 p-3 text-xs">
      <span className="font-semibold">Test-Umgebung</span>
      {session.active ? (
        <>
          <span>
            AC-Infinity-Anmeldung aktiv bis {when(session.until)} (noch {Math.floor(left / 60)}:
            {String(left % 60).padStart(2, '0')}). Danach übernimmt Live beim nächsten Zugriff.
          </span>
          <Button size="sm" variant="secondary" disabled={busy} onClick={() => send('end')}>
            {busy && <Loader2 className="mr-1.5 h-3.5 w-3.5 animate-spin" />}
            Test beenden
          </Button>
        </>
      ) : (
        <>
          <span>
            {ENDED[session.ended] ? ENDED[session.ended] + ' ' : ''}Nicht angemeldet – Live nutzt die Anmeldung.
          </span>
          <Button size="sm" disabled={busy} onClick={() => send('activate')}>
            {busy && <Loader2 className="mr-1.5 h-3.5 w-3.5 animate-spin" />}
            Test aktivieren ({session.minutes || 5} min)
          </Button>
        </>
      )}
      {msg && <span className="text-destructive">{msg}</span>}
    </div>
  )
}

/** Live: another environment uses the AC Infinity session; Live takes it back after 5 min or now. */
function LiveTakeover({ until, onDone }) {
  const [busy, setBusy] = useState(false)
  const [msg, setMsg] = useState(null)
  const take = async () => {
    setBusy(true)
    setMsg(null)
    try {
      await api.post('/api/fans/session/', { action: 'takeover' })
      onDone()
    } catch (err) {
      setMsg(errText(err, 'Übernehmen fehlgeschlagen.'))
    } finally {
      setBusy(false)
    }
  }
  return (
    <div className="flex flex-wrap items-center gap-2 rounded-md border border-amber-500/60 bg-amber-500/10 p-3 text-xs">
      <span>Die AC-Infinity-Anmeldung nutzt gerade eine andere Umgebung. Live übernimmt um {when(until)}.</span>
      <Button size="sm" disabled={busy} onClick={take}>
        {busy && <Loader2 className="mr-1.5 h-3.5 w-3.5 animate-spin" />}
        Live jetzt übernehmen
      </Button>
      {msg && <span className="text-destructive">{msg}</span>}
    </div>
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
      {data?.configured && data.session?.test && <TestSession session={data.session} onChanged={load} />}
      {data && <Credentials configured={!!data.configured} onSaved={load} />}
      {data?.configured && data.devices.length === 0 && !(data.session?.test && !data.session.active) && (
        <p className="text-sm text-muted-foreground">Keine Controller im Konto gefunden.</p>
      )}
      {data?.session && !data.session.test && data.session.paused_until && (
        <LiveTakeover until={data.session.paused_until} onDone={load} />
      )}
      {data?.devices.map((dev) => (
        <Controller key={dev.dev_id} dev={dev} backups={data.backups || []} automation={data.automation}
          onReload={load} />
      ))}
    </div>
  )
}
