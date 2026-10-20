/**
 * Takt-Tabellen (Avalon A15): measured clock -> hashrate/power tables per work mode.
 * Max-Messung: two clean measurements per selected mode at stock clocks.
 * Messreihe: one mode, stock clock downwards in 10 MHz steps.
 * The runs are executed by the data-service; this page starts/pauses/cancels them and shows the data.
 */
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import api from '@/lib/api'
import { Loader2, SlidersHorizontal, Table } from 'lucide-react'
import { useCallback, useEffect, useMemo, useState } from 'react'

const REFRESH_MS = 15000
const COLS_KEY = 'ms-calibration-columns'

const STATUS_TEXT = {
  running: 'läuft',
  paused: 'pausiert',
  cancelling: 'wird abgebrochen …',
  finishing: 'wird beendet …',
  done: 'fertig',
  cancelled: 'abgebrochen',
}
const PHASE_TEXT = {
  switch: 'schaltet den Modus um',
  wait_mode: 'wartet, bis der Miner den neuen Modus meldet',
  set_clock: 'setzt den Takt',
  measure: 'wartet auf eine saubere Messung (der Miner meldet etwa alle 130 s)',
  restore: 'stellt den Ausgangszustand wieder her',
}

// Time model for the estimates: switching a mode ~60 s. Messreihe: first clean measurement after a
// change: power stable for 180 s plus on average half a GHSspd period (~65 s). Max-Messung: each of the
// two measurements waits 300 s plus on average half a GHSspd period. Measured intervals of a running
// series replace the estimate.
const T_SWITCH = 60
const T_FIRST = 245
const T_MAX = 365
const T_MODE = T_SWITCH + 2 * T_MAX

function stepsFor(stockMhz) {
  return stockMhz ? Math.floor((stockMhz * 0.85) / 10) + 1 : null
}

function fmtDuration(sec) {
  if (sec === null || sec === undefined || Number.isNaN(sec)) return '—'
  const s = Math.max(Math.round(sec), 0)
  const h = Math.floor(s / 3600)
  const m = Math.floor((s % 3600) / 60)
  const r = s % 60
  return h ? h + ':' + String(m).padStart(2, '0') + ':' + String(r).padStart(2, '0') + ' h' : m + ':' + String(r).padStart(2, '0') + ' min'
}

function clockAt(ms) {
  return new Date(ms).toLocaleTimeString('de-DE', { hour: '2-digit', minute: '2-digit' })
}

/** Seconds per point learned from the saved points of this run (needs at least two intervals). */
function learnedPerPoint(points) {
  const times = points.map((p) => Date.parse(p.recorded_at)).filter(Number.isFinite).sort((a, b) => a - b)
  if (times.length < 3) return null
  const gaps = times.slice(1).map((t, i) => (t - times[i]) / 1000)
  return gaps.reduce((a, b) => a + b, 0) / gaps.length
}

/** Estimated remaining seconds of the current mode and of the whole run, as of server time `nowMs`. */
function remainingSeconds(run, nowMs, perPoint, stockMhz) {
  const since = run.changed_at ? Math.max((nowMs - Date.parse(run.changed_at)) / 1000, 0) : 0
  if (run.phase === 'restore' || run.phase === 'done') return { mode: null, total: T_SWITCH }
  if (run.kind === 'max') {
    const after = Math.max(run.modes.length - run.mode_index - 1, 0)
    let cur = T_MODE
    if (run.phase === 'measure') cur = Math.max(T_MAX - since, 30) + ((run.count || 0) === 0 ? T_MAX : 0)
    return { mode: cur, total: cur + after * T_MODE + T_SWITCH }
  }
  const per = perPoint || T_FIRST
  const total = run.steps_total || stepsFor(stockMhz)
  if (!total) return { mode: null, total: null }
  let cur = per
  if (run.phase === 'measure') cur = Math.max(per - since, 30)
  else if (run.phase === 'switch' || run.phase === 'wait_mode') cur = T_SWITCH + per
  const mode = cur + Math.max(total - run.step_index - 1, 0) * per
  return { mode, total: mode + T_SWITCH }
}

/** Ticking countdown towards `eta` (client clock, ms). */
function Countdown({ eta, paused, remaining, label }) {
  const [now, setNow] = useState(Date.now())
  useEffect(() => {
    if (paused) return undefined
    const timer = setInterval(() => setNow(Date.now()), 1000)
    return () => clearInterval(timer)
  }, [paused])
  if (paused) return <span>{label}: ca. {fmtDuration(remaining)} (läuft weiter, sobald fortgesetzt)</span>
  if (!eta) return null
  return (
    <span>
      {label}: ca. <span className="font-semibold tabular-nums">{fmtDuration((eta - now) / 1000)}</span> · fertig ca. um{' '}
      {clockAt(eta)} Uhr (Schätzung)
    </span>
  )
}

function fmt(value, digits = 1) {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return '—'
  return Number(value).toLocaleString('de-DE', { minimumFractionDigits: digits, maximumFractionDigits: digits })
}

function when(iso) {
  if (!iso) return '—'
  return new Date(iso).toLocaleString('de-DE', { day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit' })
}

function fieldText(value) {
  if (value === null || value === undefined || value === '') return '—'
  if (typeof value === 'object') {
    if ('min' in value && 'max' in value) return fmt(value.min, 0) + '–' + fmt(value.max, 0) + ' (Ø ' + fmt(value.avg, 1) + ')'
    return JSON.stringify(value)
  }
  return String(value)
}

/** Standard columns (always shown) and extra columns the filter can add. */
const STANDARD = [
  { key: 'set_mhz', label: 'Takt (gesetzt)', get: (p) => fmt(p.set_mhz, 1) + ' MHz' },
  { key: 'hashrate_ths', label: 'Hashrate', get: (p) => fmt(p.hashrate_ths, 2) + ' TH/s' },
  { key: 'power_w', label: 'Leistung', get: (p) => fmt(p.power_w, 0) + ' W' },
  { key: 'j_th', label: 'J/TH', get: (p) => fmt(p.j_th, 1) },
  { key: 'dh_pct', label: 'Fehlerquote', get: (p) => fmt(p.dh_pct, 2) + ' %' },
  { key: 'chip_temp_c', label: 'Chip-Temp.', get: (p) => fmt(p.chip_temp_c, 0) + ' °C' },
  { key: 'fan_level', label: 'Lüfter (AC)', get: (p) => (p.fan_level == null ? '—' : 'Stufe ' + p.fan_level) },
  {
    key: 'power_spread_pct',
    label: 'Schwankung',
    get: (p) => (p.power_spread_pct == null ? '—' : '±' + fmt(p.power_spread_pct, 1) + ' %'),
  },
]
const BASE_EXTRA = [
  { key: 'miner_mhz', label: 'Takt laut Miner', get: (p) => fmt(p.miner_mhz, 1) + ' MHz' },
  { key: 'set_points', label: 'Takt-Werte gesetzt', get: (p) => (p.set_points || []).join(' / ') || '—' },
  { key: 'miner_points', label: 'Takt-Werte Miner', get: (p) => (p.miner_points || []).join(' / ') || '—' },
  { key: 'recorded_at', label: 'gemessen am', get: (p) => when(p.recorded_at) },
]

function loadColumns() {
  try {
    const raw = window.localStorage.getItem(COLS_KEY)
    return raw ? JSON.parse(raw) : []
  } catch {
    return []
  }
}

function saveColumns(cols) {
  try {
    window.localStorage.setItem(COLS_KEY, JSON.stringify(cols))
  } catch {
    /* storage not available: selection lasts for this visit */
  }
}

function useColumns(points) {
  const [chosen, setChosen] = useState(loadColumns)
  const available = useMemo(() => {
    const keys = new Set()
    for (const p of points) Object.keys(p.fields || {}).forEach((k) => keys.add(k))
    return [
      ...BASE_EXTRA,
      ...[...keys].sort((a, b) => a.localeCompare(b)).map((k) => ({
        key: 'f:' + k,
        label: k,
        get: (p) => fieldText((p.fields || {})[k]),
      })),
    ]
  }, [points])
  const toggle = (key) => {
    const next = chosen.includes(key) ? chosen.filter((k) => k !== key) : [...chosen, key]
    setChosen(next)
    saveColumns(next)
  }
  const extra = available.filter((c) => chosen.includes(c.key))
  return { available, chosen, toggle, columns: [...STANDARD, ...extra] }
}

function ColumnFilter({ available, chosen, toggle }) {
  const [open, setOpen] = useState(false)
  const [query, setQuery] = useState('')
  const shown = available.filter((c) => c.label.toLowerCase().includes(query.toLowerCase()))
  return (
    <div className="space-y-2">
      <Button type="button" size="sm" variant="secondary" onClick={() => setOpen(!open)}>
        <SlidersHorizontal className="mr-1.5 h-4 w-4" />
        Spalten {chosen.length ? '(' + chosen.length + ' zusätzlich)' : ''}
      </Button>
      {open && (
        <div className="rounded-md border border-border/80 p-3">
          <input
            className="mb-2 h-8 w-full rounded-md border border-input bg-background px-2 text-sm"
            placeholder="Wert suchen …"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
          />
          <p className="mb-2 text-xs text-muted-foreground">
            Immer sichtbar: Takt, Hashrate, Leistung, J/TH, Fehlerquote, Chip-Temperatur. Zusätzlich einblenden:
          </p>
          <div className="grid max-h-64 grid-cols-2 gap-x-4 gap-y-1 overflow-y-auto sm:grid-cols-3 lg:grid-cols-4">
            {shown.map((c) => (
              <label key={c.key} className="flex items-center gap-2 text-xs">
                <input type="checkbox" checked={chosen.includes(c.key)} onChange={() => toggle(c.key)} />
                <span className="truncate" title={c.label}>
                  {c.label}
                </span>
              </label>
            ))}
          </div>
        </div>
      )}
    </div>
  )
}

function PointTable({ points, columns, lead }) {
  if (!points.length) return <p className="text-sm text-muted-foreground">Noch keine Messwerte.</p>
  return (
    <div className="overflow-x-auto">
      <table className="w-full text-xs">
        <thead className="text-muted-foreground">
          <tr className="text-left">
            {lead && <th className="whitespace-nowrap py-1.5 pr-3 font-medium">{lead.label}</th>}
            {columns.map((c) => (
              <th key={c.key} className="whitespace-nowrap py-1.5 pr-3 font-medium">
                {c.label}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {points.map((p) => (
            <tr key={p.id} className="border-t border-border/50">
              {lead && <td className="whitespace-nowrap py-1 pr-3 font-medium">{lead.get(p)}</td>}
              {columns.map((c) => (
                <td key={c.key} className="whitespace-nowrap py-1 pr-3 tabular-nums">
                  {c.get(p)}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

function RunStatus({ run, onCommand, busy, timing }) {
  if (!run) return null
  const active = run.active
  const modes = run.modes || []
  const cur = modes[run.mode_index]
  let progress = ''
  if (run.kind === 'max' && cur) {
    progress = cur.label + ' · Messung ' + Math.min((run.count || 0) + 1, 2) + ' von 2'
  } else if (run.kind === 'step' && cur) {
    progress = cur.label + ' · Punkt ' + (run.step_index + 1) + (run.steps_total ? ' von ' + run.steps_total : '') +
      (run.set_mhz ? ' · Takt ' + fmt(run.set_mhz, 0) + ' MHz' : '')
  }
  return (
    <div className="space-y-2 rounded-md border border-border/80 p-3">
      <p className="text-sm font-medium">
        {run.kind === 'max' ? 'Max-Messung' : 'Messreihe'} · {STATUS_TEXT[run.status] || run.status}
        {run.status === 'paused' && run.pause_reason === 'overhot' ? ' (Over Hot)' : ''}
      </p>
      {active && progress && <p className="text-xs">{progress}</p>}
      {active && run.status === 'running' && run.phase && (
        <p className="text-xs text-muted-foreground">
          {run.phase === 'measure'
            ? run.stable
              ? 'Leistung stabil, wartet auf eine saubere Messung (der Miner meldet etwa alle 130 s)'
              : 'wartet, bis die Leistung 3 Minuten stabil ist (±3 %)'
            : PHASE_TEXT[run.phase] || run.phase}
        </p>
      )}
      {active && timing && timing.modeRemaining !== null && cur && (
        <p className="text-sm">
          <Countdown eta={timing.modeEta} paused={run.status !== 'running'} remaining={timing.modeRemaining}
            label={'Restzeit ' + cur.label} />
        </p>
      )}
      {active && timing && timing.remaining !== null && (
        <p className="text-sm">
          <Countdown eta={timing.eta} paused={run.status !== 'running'} remaining={timing.remaining}
            label="Restzeit gesamt" />
        </p>
      )}
      {run.message && <p className="text-xs text-muted-foreground">{run.message}</p>}
      {active && run.last_error && <p className="text-xs text-amber-600">{run.last_error}</p>}
      {run.status === 'paused' && run.pause_reason === 'overhot' && (
        <p className="text-xs text-destructive">
          Der A15 meldet „Over Hot“. Neustart über den Hinweis oben auf der Seite, danach „Fortsetzen“.
        </p>
      )}
      {active && (
        <div className="flex flex-wrap gap-2">
          {run.status === 'running' && (
            <Button size="sm" variant="secondary" disabled={busy} onClick={() => onCommand({ action: 'pause' })}>
              Pausieren
            </Button>
          )}
          {run.status === 'paused' && (
            <Button size="sm" disabled={busy} onClick={() => onCommand({ action: 'resume' })}>
              Fortsetzen (letzten Punkt neu messen)
            </Button>
          )}
          {run.status !== 'cancelling' && (
            <Button size="sm" variant="secondary" disabled={busy} onClick={() => onCommand({ action: 'cancel' })}>
              Abbrechen
            </Button>
          )}
        </div>
      )}
    </div>
  )
}

export default function CalibrationPage() {
  const [device, setDevice] = useState(null)
  const [data, setData] = useState(null)
  const [mode, setMode] = useState('2/L4')
  const [maxModes, setMaxModes] = useState(null)
  const [busy, setBusy] = useState(false)
  const [message, setMessage] = useState(null)
  const [error, setError] = useState(null)

  useEffect(() => {
    api
      .get('/api/devices/')
      .then(({ data: res }) => {
        const list = Array.isArray(res) ? res : res?.results || []
        const a15 = list.find((d) => /a15/i.test(String(d.device_id || '') + ' ' + String(d.name || d.device_name || '')))
        if (a15) setDevice(a15)
        else setError('Kein Avalon A15 gefunden.')
      })
      .catch(() => setError('Geräteliste nicht ladbar.'))
  }, [])

  const load = useCallback(async () => {
    if (!device) return
    try {
      const { data: res } = await api.get(`/api/devices/${device.id}/calibration/`, { params: { mode } })
      setData({ ...res, _loadedAt: Date.now() })
      setError(null)
      setMaxModes((prev) => prev || res.modes.map((m) => m.key))
    } catch (err) {
      setError(err?.response?.data?.error || 'Daten nicht ladbar.')
    }
  }, [device, mode])

  useEffect(() => {
    load()
    const timer = setInterval(load, REFRESH_MS)
    return () => clearInterval(timer)
  }, [load])

  const command = async (body) => {
    setBusy(true)
    setMessage(null)
    try {
      await api.post(`/api/devices/${device.id}/calibration/`, body)
      await load()
    } catch (err) {
      setMessage(err?.response?.data?.error || 'Aktion fehlgeschlagen.')
    } finally {
      setBusy(false)
    }
  }

  const allPoints = useMemo(() => [...(data?.max || []), ...(data?.step || []), ...(data?.manual || [])], [data])
  const timing = useMemo(() => {
    const r = data?.run
    if (!r || !r.active) return null
    const skew = r.server_now ? Date.parse(r.server_now) - data._loadedAt : 0
    const runPoints = r.kind === 'step' && data.step_run && data.step_run.id === r.id ? data.step : []
    const stock = (data.modes.find((m) => m.key === (r.modes[0] || {}).key) || {}).stock_mhz
    const rem = remainingSeconds(r, data._loadedAt + skew, learnedPerPoint(runPoints), stock)
    const at = (sec) => (sec === null ? null : data._loadedAt + sec * 1000)
    return { remaining: rem.total, eta: at(rem.total), modeRemaining: rem.mode, modeEta: at(rem.mode) }
  }, [data])
  const cols = useColumns(allPoints)
  const run = data?.run
  const active = run?.active
  const notes = data?.max_notes || {}

  if (error && !data) return <p className="text-sm text-destructive">{error}</p>
  if (!data) {
    return (
      <p className="flex items-center gap-2 text-sm text-muted-foreground">
        <Loader2 className="h-4 w-4 animate-spin" /> Lade Takt-Tabellen …
      </p>
    )
  }

  const modeLabel = (key) => (data.modes.find((m) => m.key === key) || {}).label || key

  return (
    <div className="space-y-4">
      <Card>
        <CardHeader className="pb-2">
          <CardTitle className="flex items-center gap-2 text-base font-semibold">
            <Table className="h-4 w-4 text-muted-foreground" />
            Takt-Tabellen · {data.device.name}
          </CardTitle>
          <CardDescription className="text-xs">
            Max-Messung: jede Messung frühestens 5 Minuten nach dem Moduswechsel bzw. der vorherigen Messung. Messreihe:
            frühestens 145 s nach einer Taktänderung. Während einer
            Messung ist Drosseln gesperrt; eine laufende Drosselung wird beim Start aufgehoben.
          </CardDescription>
        </CardHeader>
        <CardContent className="space-y-3 pt-0">
          <RunStatus run={run} onCommand={command} busy={busy} timing={timing} />
          {!active && (
            <div className="grid gap-3 md:grid-cols-2">
              <div className="space-y-2 rounded-md border border-border/80 p-3">
                <p className="text-sm font-medium">Max-Messung</p>
                <p className="text-xs text-muted-foreground">
                  Pro Modus umschalten und bei Standardtakt zwei saubere Messungen speichern (Schätzung ca.{' '}
                  {fmtDuration(T_MODE)} pro Modus).
                </p>
                <div className="flex flex-wrap gap-x-4 gap-y-1">
                  {data.modes.map((m) => (
                    <label key={m.key} className="flex items-center gap-1.5 text-xs">
                      <input
                        type="checkbox"
                        checked={(maxModes || []).includes(m.key)}
                        onChange={() =>
                          setMaxModes((prev) =>
                            prev.includes(m.key) ? prev.filter((k) => k !== m.key) : [...prev, m.key],
                          )
                        }
                      />
                      {m.label}
                    </label>
                  ))}
                </div>
                <p className="text-xs">
                  Geschätzte Dauer für {(maxModes || []).length} Modi:{' '}
                  <span className="font-semibold">
                    {(maxModes || []).length ? fmtDuration((maxModes || []).length * T_MODE + T_SWITCH) : '—'}
                  </span>
                </p>
                <Button
                  size="sm"
                  disabled={busy || !(maxModes || []).length}
                  onClick={() => command({ action: 'start_max', modes: maxModes })}
                >
                  Max-Messung starten
                </Button>
              </div>
              <div className="space-y-2 rounded-md border border-border/80 p-3">
                <p className="text-sm font-medium">Messreihe (10-MHz-Schritte)</p>
                <p className="text-xs text-muted-foreground">
                  Vom Standardtakt abwärts bis 15 % davon, eine saubere Messung pro Punkt (Schätzung ca.{' '}
                  {fmtDuration(T_FIRST)} pro Punkt).
                  Unterbrechen jederzeit möglich; beim Fortsetzen wird der letzte gespeicherte Punkt neu gemessen.
                </p>
                <p className="text-xs">Modus: {modeLabel(mode)} (Auswahl unten bei der Tabelle)</p>
                {(() => {
                  const stock = (data.modes.find((m) => m.key === mode) || {}).stock_mhz
                  const n = stepsFor(stock)
                  return (
                    <p className="text-xs">
                      {n ? (
                        <>
                          Standardtakt {fmt(stock, 0)} MHz · {n} Punkte · geschätzte Dauer{' '}
                          <span className="font-semibold">{fmtDuration(T_SWITCH * 2 + n * T_FIRST)}</span>
                        </>
                      ) : (
                        'Standardtakt dieses Modus noch unbekannt, Dauer daher erst nach dem Start schätzbar.'
                      )}
                    </p>
                  )
                })()}
                <Button size="sm" disabled={busy} onClick={() => command({ action: 'start_step', mode })}>
                  Messreihe für {modeLabel(mode)} starten
                </Button>
              </div>
            </div>
          )}
          {message && <p className="text-xs text-destructive">{message}</p>}
          {error && <p className="text-xs text-destructive">Nicht aktualisiert: {error}</p>}
        </CardContent>
      </Card>

      <ColumnFilter available={cols.available} chosen={cols.chosen} toggle={cols.toggle} />

      <Card>
        <CardHeader className="pb-2">
          <CardTitle className="text-base font-semibold">Max-Werte je Modus</CardTitle>
          <CardDescription className="text-xs">Letzte Max-Messung pro Modus, zwei Messungen je Modus.</CardDescription>
        </CardHeader>
        <CardContent className="space-y-2 pt-0">
          <PointTable
            points={data.max}
            columns={cols.columns}
            lead={{ label: 'Modus', get: (p) => p.mode_label + ' · ' + (p.seq + 1) }}
          />
          {Object.keys(notes).length > 0 && (
            <ul className="space-y-0.5 text-xs text-muted-foreground">
              {Object.entries(notes).map(([key, n]) => (
                <li key={key}>
                  {modeLabel(key)}:{' '}
                  {n.error
                    ? n.error
                    : 'Umschalten dauerte ' + n.switch_s + ' s' + (n.restart ? ', der Miner hat dabei neu gestartet' : ', ohne Neustart')}
                </li>
              ))}
            </ul>
          )}
        </CardContent>
      </Card>

      <Card>
        <CardHeader className="pb-2">
          <CardTitle className="flex flex-wrap items-center gap-2 text-base font-semibold">
            Messreihe
            <select
              className="h-8 rounded-md border border-input bg-background px-2 text-sm font-normal"
              value={mode}
              onChange={(e) => setMode(e.target.value)}
            >
              {data.modes.map((m) => (
                <option key={m.key} value={m.key}>
                  {m.label}
                </option>
              ))}
            </select>
          </CardTitle>
          <CardDescription className="text-xs">
            {data.step_run ? 'Messreihe vom ' + when(data.step_run.created_at) : 'Für diesen Modus gibt es noch keine Messreihe.'}
          </CardDescription>
        </CardHeader>
        <CardContent className="pt-0">
          <PointTable points={data.step} columns={cols.columns} />
        </CardContent>
      </Card>

      <Card>
        <CardHeader className="pb-2">
          <CardTitle className="text-base font-semibold">Manuell</CardTitle>
          <CardDescription className="text-xs">
            Messpunkte der Drosselung: gespeichert nach 15 Minuten mit unverändertem Takt (neueste zuerst).
          </CardDescription>
        </CardHeader>
        <CardContent className="pt-0">
          <PointTable
            points={data.manual || []}
            columns={cols.columns}
            lead={{ label: 'Zeit · Modus', get: (p) => when(p.recorded_at) + ' · ' + p.mode_label }}
          />
        </CardContent>
      </Card>
    </div>
  )
}
