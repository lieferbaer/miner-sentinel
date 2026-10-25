/**
 * Lüfter (AC Infinity, Testmenü): Controller und Ports anzeigen, Einstellungen sichern/zurückschreiben und
 * testen, welcher Schreibweg die Lüfterstufe am echten Gerät wirklich ändert.
 */
import MetricCard from '@/components/metrics/MetricCard'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import api from '@/lib/api'
import { useLive } from '@/lib/LiveContext'
import { Activity, Fan, Gauge, Loader2, RefreshCw, Thermometer } from 'lucide-react'
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

// state of the regulation (fan_automation.state.phase, Regelung vom 24.10.)
const PHASE = {
  normal: { value: 'Normalbetrieb', sub: 'Takt regelt 80–84 °C' },
  korrektur: { value: 'Korrekturphase', sub: 'Lüfter erhöht, bis 80–84 °C erreicht sind' },
  ausnahme: { value: 'Ausnahme', sub: 'Lüfter reduziert · 440 MHz und unter 70 °C' },
  messung: { value: 'Takt-Messung', sub: 'Taktregelung pausiert, Lüfter nur ab 85 °C' },
  wartung: { value: 'Wartung', sub: 'A15 aus · Lüfter aus · endet beim Einschalten' },
  aus: { value: 'Aus', sub: '' },
}
// commands, window and learning period are only current while the regulation runs on valid readings
const REGULATING = new Set(['normal', 'korrektur', 'ausnahme'])
const MODE_LABEL = { '2/L4': 'mode 04', '2/L3': 'mode 03', '2/L2': 'mode 02', '2/L1': 'mode 01', '2/L0': 'mode 00',
  '1/-': 'High Performance' }
// the regulation in short (plan of 24.10.)
const LOGIC_TEXT =
  'Der Lüfter läuft fest auf der gewählten Basisstufe, der Takt hält den heißesten Chip bei 80–84 °C. Ab 85 °C ' +
  'Lüfter +1 bzw. +2 und Takt runter (Korrekturphase); zurück zur Basisstufe nach 1 min in 80–84 °C oder sobald ' +
  'unter 80 °C. Bei 440 MHz und 3 min unter 70 °C Lüfter −1 (Ausnahme).'
const START_TEXT =
  'Start (Stufenwahl, Einschalten, Neustart, nach Wartung oder Takt-Messung): Lernwert der Basisstufe, sonst ' +
  'Stufe 1 = 100 MHz … Stufe 7 = 160 MHz. Lernwert: 15 min stabil in 80–84 °C bei unverändertem Takt und Lüfter.'

function clockTime(iso, seconds = false) {
  return new Date(iso).toLocaleTimeString('de-DE', { hour: '2-digit', minute: '2-digit', ...(seconds ? { second: '2-digit' } : {}) })
}

function tendencyText(d) {
  if (d === null || d === undefined) return null
  if (d > 0) return 'steigend (+' + d + ' °C)'
  if (d < 0) return 'fallend (−' + Math.abs(d) + ' °C)'
  return 'gleich'
}

/** Temperatur-Automatik (A15): status from the live poll of the UI (every 5 s, same values as the overview). */
function useAutomation(initial) {
  const live = useLive()
  const current = live.data?.automation || initial
  const [auto, setAuto] = useState(current)
  useEffect(() => setAuto(current), [current])
  return [auto, setAuto]
}

/** Learned values per fan level: clock at which the chip temperature stayed 15 min within 80-84 °C. */
function LearnedTable({ learned, level }) {
  // only the per-level values of the regulation since 24.10. (older values per mode have no 'mhz' here)
  const rows = Object.entries(learned || {})
    .filter(([, v]) => v && typeof v.mhz === 'number')
    .sort((a, b) => Number(a[0]) - Number(b[0]))
  if (!rows.length) {
    return <p className="text-muted-foreground">Lernwerte: noch keine (nach 15 min stabil bei 80–84 °C).</p>
  }
  return (
    <div className="pt-1">
      <p className="font-medium">Lernwerte</p>
      <table className="text-xs">
        <thead className="text-muted-foreground">
          <tr className="text-left">
            <th className="pr-3 font-medium">Stufe</th>
            <th className="pr-3 font-medium">Takt</th>
            <th className="pr-3 font-medium">Ø Temperatur</th>
            <th className="pr-3 font-medium">Anzahl</th>
            <th className="font-medium">Zeit</th>
          </tr>
        </thead>
        <tbody>
          {rows.map(([lv, v]) => (
            <tr key={lv} className={Number(lv) === level ? 'font-semibold' : ''}>
              <td className="pr-3">{lv}</td>
              <td className="pr-3 tabular-nums">{fmt(v.mhz, 0, ' MHz')}</td>
              <td className="pr-3 tabular-nums">{fmt(v.temp_avg, 1, ' °C')}</td>
              <td className="pr-3 tabular-nums">{v.count ?? '—'}</td>
              <td className="text-muted-foreground">{when(v.at)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

/** Status of the Temperatur-Automatik as cards like the overview; the small line never repeats the big value. */
function AutomationCards({ auto, st }) {
  const live = useLive()
  const sys = Object.values(live.data?.system || {}).find((d) => d && d.id === auto.device?.id) || {}
  const errorText = sys.error_rate_pct != null ? ' · Fehlerrate ' + fmt(sys.error_rate_pct, 2, ' %') : ''
  // during a Takt-Messung the run sets the clock: then the same value as the throttle card
  const throttle = live.data?.throttle?.[auto.device?.id] || {}
  const measuring = st.phase === 'messung'
  const setText = measuring
    ? (throttle.set_mhz != null ? fmt(throttle.set_mhz, 0, ' MHz') + ' (Takt-Messung)' : '—')
    : fmt(st.clock_set, 0, ' MHz')
  const tmax = auto.now?.tmax
  const boards = (auto.now?.boards || []).filter((b) => b != null)
  const others = boards.length > 1 ? [...boards].sort((a, b) => b - a).slice(1) : []
  const tempTone = tmax == null ? 'default' : tmax >= 88 ? 'danger' : tmax >= 85 ? 'warning' : 'default'
  const tendency = tendencyText(st.tendency)
  // the level MinerSentinel set last; the controller confirms it only after > 5 s (read back after 10 s)
  const base = auto.level
  const running = st.fan_set ?? st.fan_read
  const confirmed = st.fan_read === st.fan_set ||
    (st.fan_read_at && st.fan_set_at && Date.parse(st.fan_read_at) >= Date.parse(st.fan_set_at) + 10000)
  const fanSub = [
    running === base ? '= Basisstufe' : 'Basisstufe ' + base,
    st.phase === 'korrektur' && running > base ? 'Korrektur +' + (running - base) : null,
    st.phase === 'ausnahme' ? 'Ausnahme' + (running != null && running < base ? ' −' + (base - running) : '') : null,
    // after the Ausnahme the fan stays on the reduced level (plan, step 5); judged by the target: fan_set lags
    // behind until the controller has taken a new base level
    st.phase === 'normal' && st.fan_target != null && st.fan_target < base
      ? 'reduziert nach Ausnahme −' + (base - st.fan_target) : null,
    st.fan_target != null && st.fan_target !== st.fan_set ? 'wird Stufe ' + st.fan_target : null,
    confirmed ? null : 'Rückmeldung folgt',
  ].filter(Boolean).join(' · ')
  const phase = PHASE[st.phase] || { value: '—', sub: '' }
  const pending = st.pending
    ? (st.pending.kind === 'modus' ? 'Modus ' + String(st.pending.value).padStart(2, '0') : 'Takt ' + st.pending.value + ' MHz') +
      ' wird übernommen' + (st.pending.attempt > 1 ? ' (Versuch ' + st.pending.attempt + ')' : '')
    : null
  const windowOpen = st.window_until && Date.parse(st.window_until) > Date.now()
  // Wartung, Takt-Messung, Over Hot, miner silent: the data-service leaves these fields as they were
  const regulating = REGULATING.has(st.phase) && !st.offline && !st.over_hot
  const statusSub = !regulating ? phase.sub : [
    pending,
    windowOpen ? 'Beobachtungsfenster bis ' + clockTime(st.window_until, true) : null,
    st.learn_since ? 'lernt seit ' + clockTime(st.learn_since) : null,
  ].filter(Boolean).join(' · ') || phase.sub
  return (
    <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 xl:grid-cols-4">
      <MetricCard variant="hero" layout="icon-left" icon={Thermometer} label="Heißester Chip" tone={tempTone}
        value={fmt(tmax, 0, ' °C')}
        subtitle={[others.length ? 'weitere Boards ' + others.join(' / ') : null,
          st.t != null ? 'Regelwert ' + fmt(st.t, 0, ' °C') : null, tendency ? 'Tendenz ' + tendency : null,
          'Ziel 80–84 °C'].filter(Boolean).join(' · ')} />
      <MetricCard variant="hero" layout="icon-left" icon={Gauge} label="Takt laut Miner"
        value={fmt(st.clock_now, 0, ' MHz')}
        subtitle={'gesetzt ' + setText + (st.mode_key ? ' · ' + (MODE_LABEL[st.mode_key] || st.mode_key) : '') +
          ' · Standard ' + fmt(st.stock_mhz, 0, ' MHz') + errorText} />
      <MetricCard variant="hero" layout="icon-left" icon={Fan} label="Lüfter läuft auf"
        tone={st.phase === 'korrektur' ? 'warning' : 'default'}
        value={st.phase === 'wartung' ? 'Aus' : running == null ? '—' : 'Stufe ' + running}
        subtitle={st.phase === 'wartung' ? 'Wartung · danach wieder Stufe ' + (auto.maintenance?.fan_before ?? base)
          : fanSub} />
      <MetricCard variant="hero" layout="icon-left" icon={Activity} label="Status"
        tone={st.phase === 'normal' ? 'default' : 'warning'} value={phase.value}
        subtitle={statusSub} />
    </div>
  )
}

function AutomationPanel({ auto, here, port, level, locked, onCommand, busy }) {
  const [confirmOff, setConfirmOff] = useState(false)
  if (!auto) return null
  const st = auto.state || {}
  if (!auto.enabled) {
    return (
      <div className="space-y-2 rounded-md border border-border/80 p-3">
        <p className="text-sm font-medium">Temperatur-Automatik · {auto.device?.name}</p>
        <p className="text-xs text-muted-foreground">{LOGIC_TEXT}</p>
        <p className="text-xs text-muted-foreground">{START_TEXT}</p>
        <Button size="sm" disabled={locked || busy} onClick={() => onCommand({ action: 'enable', port, level })}>
          {busy && <Loader2 className="mr-1.5 h-3.5 w-3.5 animate-spin" />}
          Einschalten (Port {port}, Basisstufe {level})
        </Button>
      </div>
    )
  }
  return (
    <div className="space-y-1.5 rounded-md border border-emerald-500/60 bg-emerald-500/5 p-3 text-xs">
      <div className="flex flex-wrap items-center gap-2">
        <p className="w-full text-sm font-medium sm:w-auto">Temperatur-Automatik an · {auto.device?.name}</p>
        {/* phone: buttons share the full width, the question gets its own line */}
        <span className="flex w-full flex-wrap gap-2 sm:ml-auto sm:w-auto">
          {auto.maintenance_at ? (
            <Button size="sm" variant="outline" className="h-9 flex-1 sm:h-8 sm:flex-none" disabled={busy}
              onClick={() => onCommand({ action: 'maintenance_end' })}>
              Wartung beenden
            </Button>
          ) : (
            <Button size="sm" variant="outline" className="h-9 flex-1 sm:h-8 sm:flex-none" disabled={busy}
              onClick={() => onCommand({ action: 'maintenance_start' })}>
              Wartung starten (A15 aus)
            </Button>
          )}
          {confirmOff ? (
            <>
              <span className="basis-full self-center sm:basis-auto">
                Automatik wirklich beenden? Der Takt bleibt, wie er ist.
              </span>
              <Button size="sm" variant="destructive" className="h-9 flex-1 sm:h-8 sm:flex-none" disabled={busy}
                onClick={() => { setConfirmOff(false); onCommand({ action: 'disable' }) }}>
                Ja
              </Button>
              <Button size="sm" variant="secondary" className="h-9 flex-1 sm:h-8 sm:flex-none"
                onClick={() => setConfirmOff(false)}>
                Nein
              </Button>
            </>
          ) : (
            <Button size="sm" variant="outline" className="h-9 flex-1 sm:h-8 sm:flex-none" disabled={busy}
              onClick={() => setConfirmOff(true)}>
              Automatik beenden
            </Button>
          )}
        </span>
      </div>
      {!here && <p className="text-amber-600">Steuert Port {auto.port} dieses Controllers.</p>}
      {auto.maintenance_at && st.phase !== 'wartung' && (
        <p className="text-amber-600">
          Wartung angemeldet: Der Lüfter geht aus, sobald der A15 ausgeschaltet ist. Beim Einschalten geht er wieder
          auf die Stufe von vorher.
        </p>
      )}
      <AutomationCards auto={auto} st={st} />
      <LearnedTable learned={auto.learned} level={auto.level} />
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
          {autoHere && auto.state?.phase === 'messung' && (
            <p className="text-xs text-amber-600">
              Takt-Messung läuft: Die Taktregelung pausiert, der Lüfter reagiert nur ab 85 °C (jetzt Stufe{' '}
              {auto.state.fan_set ?? '—'}). Markiert ist die Basisstufe {auto.level}.
            </p>
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
