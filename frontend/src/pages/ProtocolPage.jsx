/**
 * Protokoll der Temperatur-Automatik (A15): jede Entscheidung, jeder Befehl und jeder Versuch (automation_log),
 * Reaktionszeiten je Moduswechsel, Taktwert und Lüfterstufe, der Verlauf je Regelzyklus (automation_cycle)
 * und der CSV-Export. Ereignisse laden alle 10 s neu, der Verlauf alle 30 s (Entscheidung 24.10.).
 */
import ChartFrame from '@/components/charts/ChartFrame'
import { badgeVariants } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { Input } from '@/components/ui/input'
import { Select, SelectOption } from '@/components/ui/select'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table'
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs'
import api from '@/lib/api'
import {
  ERROR_KINDS, KIND_LABEL, change, clockChange, fanChange, fmt, has, isError, levelText, modeText, signed, tempText,
} from '@/lib/automationLog'
import { cn } from '@/lib/utils'
import { ChevronLeft, ChevronRight, Download, Loader2 } from 'lucide-react'
import { Fragment, useCallback, useEffect, useMemo, useRef, useState } from 'react'
import {
  CartesianGrid,
  Line,
  LineChart,
  ReferenceArea,
  ReferenceLine,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts'

const EVENTS_REFRESH_MS = 10000
const CYCLES_REFRESH_MS = 30000
const HOUR_MS = 3600 * 1000
const HISTORY_MAX_DAYS = 7    // Verlauf „frei“: longer ranges would load and draw far too many points

const PHASE_LABEL = {
  normal: 'Normalbetrieb',
  korrektur: 'Korrekturphase',
  ausnahme: 'Ausnahme (Lüfter reduziert)',
  messung: 'Takt-Messung',
  wartung: 'Wartung',
  aus: 'Aus',
  start: 'Start',
  warten: 'Warten',
}
// background of the Verlauf per state; Normalbetrieb has none
const PHASE_FILL = {
  korrektur: '#f59e0b',
  ausnahme: '#0ea5e9',
  messung: '#8b5cf6',
  wartung: '#64748b',
}
const PHASE_FILL_OTHER = '#a3a3a3'

const SOURCE_LABEL = { regelung: 'Regelung', nutzer: 'Nutzer' }
const TARGET_LABEL = { 'miner-takt': 'Miner (Takt)', 'miner-modus': 'Miner (Modus)', lueftersteuerung: 'Lüftersteuerung' }
const RESULT_TEXT = { erfuellt: 'erfüllt', abgebrochen: 'abgebrochen', gespeichert: 'gespeichert' }
const RANGES = { '2h': '2 h', heute: 'heute', '24h': '24 h', '7d': '7 Tage', frei: 'frei' }
const RANGE_HOURS = { '2h': 2, '24h': 24, '7d': 168 }

function when(iso) {
  if (!iso) return '—'
  return new Date(iso).toLocaleString('de-DE', { day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit', second: '2-digit' })
}

function errText(err, fallback) {
  return err?.response?.data?.error || err?.response?.data?.detail || fallback
}

function apiText(e) {
  return has(e.api_ms) ? fmt(e.api_ms, 0, ' ms') : '—'
}

function takeoverText(e) {
  if (e.takeover_state === 'gemessen') return fmt(e.takeover_s, 1, ' s')
  return e.takeover_state || '—'
}

/** ok / Fehler for commands; for pure events the result in the details (erfüllt, abgebrochen, gespeichert). */
function resultOf(e) {
  const d = e.details || {}
  const again = e.attempt > 1 ? ' · Versuch ' + e.attempt : ''
  if (e.ok === true) return { text: 'ok', again, tone: 'ok' }
  if (e.ok === false) return { text: 'Fehler', again, tone: 'error' }
  if (d.kein_befehl) return { text: 'kein Befehl nötig', again: '', tone: 'muted' }
  if (d.ergebnis) return { text: RESULT_TEXT[d.ergebnis] || d.ergebnis, again: '', tone: 'muted' }
  return { text: '—', again: '', tone: 'muted' }
}

function Result({ entry }) {
  const r = resultOf(entry)
  if (r.tone === 'muted') return <span className="text-muted-foreground">{r.text}</span>
  // a span (not the div of <Badge>): the phone cards are buttons
  return (
    <span className="whitespace-nowrap">
      <span className={cn(badgeVariants({ variant: r.tone === 'error' ? 'destructive' : 'outline' }), 'px-2 py-0',
        r.tone === 'ok' && 'border-emerald-500/50 text-emerald-600')}>
        {r.text}
      </span>
      {r.again && <span className="text-muted-foreground">{r.again}</span>}
    </span>
  )
}

function validIso(value) {
  if (!value) return null
  const d = new Date(value)
  return Number.isNaN(d.getTime()) ? null : d.toISOString()
}

/** {from, to} as ISO for the chosen range; 'frei' uses the two inputs (local time). */
function rangeParams(range, custom) {
  if (range === 'frei') return { from: validIso(custom.from), to: validIso(custom.to) }
  if (range === 'heute') {
    const d = new Date()
    d.setHours(0, 0, 0, 0)
    return { from: d.toISOString(), to: null }
  }
  return { from: new Date(Date.now() - (RANGE_HOURS[range] || 24) * HOUR_MS).toISOString(), to: null }
}

function clean(params) {
  return Object.fromEntries(Object.entries(params).filter(([, v]) => has(v)))
}

function RangePicker({ options, value, onChange, custom, onCustom }) {
  return (
    <div className="flex flex-wrap items-center gap-1.5 text-xs">
      <span className="text-muted-foreground">Zeitraum</span>
      {options.map((key) => (
        <Button key={key} size="sm" className="h-8 px-3" variant={value === key ? 'default' : 'outline'}
          aria-pressed={value === key} onClick={() => onChange(key)}>
          {RANGES[key]}
        </Button>
      ))}
      {value === 'frei' && (
        <span className="flex w-full flex-wrap items-center gap-1.5 sm:w-auto">
          <label className="flex items-center gap-1">
            von
            <Input type="datetime-local" className="h-8 w-auto px-2 text-xs" value={custom.from}
              onChange={(e) => onCustom({ ...custom, from: e.target.value })} />
          </label>
          <label className="flex items-center gap-1">
            bis
            <Input type="datetime-local" className="h-8 w-auto px-2 text-xs" value={custom.to}
              onChange={(e) => onCustom({ ...custom, to: e.target.value })} />
          </label>
        </span>
      )}
    </div>
  )
}

function useRange(initial) {
  const [range, setRange] = useState(initial)
  const [custom, setCustom] = useState({ from: '', to: '' })
  return { range, setRange, custom, setCustom }
}

// ---------------------------------------------------------------- Ereignisse

function KindFilter({ value, onChange }) {
  const toggle = (kind) => onChange(value.includes(kind) ? value.filter((k) => k !== kind) : [...value, kind])
  const summary = !value.length ? 'alle' : value.length > 3 ? value.length + ' gewählt'
    : value.map((k) => KIND_LABEL[k] || k).join(', ')
  return (
    <details className="rounded-md border border-border/80 px-3 py-2 text-xs">
      <summary className="cursor-pointer select-none">Art: {summary}</summary>
      <div className="mt-2 grid grid-cols-1 gap-1 sm:grid-cols-2 lg:grid-cols-3">
        {Object.entries(KIND_LABEL).map(([kind, label]) => (
          <label key={kind} className="flex items-center gap-1.5">
            <input type="checkbox" checked={value.includes(kind)} onChange={() => toggle(kind)} />
            {label}
          </label>
        ))}
      </div>
      {value.length > 0 && (
        <Button size="sm" variant="secondary" className="mt-2 h-7" onClick={() => onChange([])}>
          Alle Arten
        </Button>
      )}
    </details>
  )
}

const COLUMNS = ['Zeit', 'Art', 'Auslöser', 'Chiptemperatur', 'Takt alt → neu', 'Lüfter alt → neu', 'Modus',
  'Befehl / Antwort', 'Ergebnis', 'API-Zeit', 'Übernahme']
const HEAD = 'h-9 whitespace-nowrap px-2 text-xs sm:px-2 sm:text-xs'
const CELL = 'px-2 py-1.5 text-xs sm:px-2 sm:py-1.5 sm:text-xs'

/** Small notes under the trigger: restarted learning period, error text. */
function TriggerNotes({ entry }) {
  return (
    <>
      {entry.details?.lernperiode_neu_gestartet && (
        <span className="block text-muted-foreground">Lernperiode neu gestartet</span>
      )}
      {entry.error && <span className="line-clamp-2 block text-destructive">{entry.error}</span>}
    </>
  )
}

const hasCommand = (e) => has(e.request) || has(e.endpoint) || has(e.response_short)

/** Command (else endpoint) and the shortened reply of the list; the full reply is in the detail view. */
function CommandText({ entry }) {
  const command = entry.request || entry.endpoint
  return (
    <>
      {command && <span className="line-clamp-2 block break-all font-mono">{command}</span>}
      {entry.response_short && (
        <span className="line-clamp-2 block break-all text-muted-foreground">{entry.response_short}</span>
      )}
    </>
  )
}

function EventTable({ rows, onOpen }) {
  return (
    <Table>
      <TableHeader>
        <TableRow>
          {COLUMNS.map((c) => (
            <TableHead key={c} className={HEAD}>{c}</TableHead>
          ))}
        </TableRow>
      </TableHeader>
      <TableBody>
        {rows.map((e) => {
          const bad = isError(e)
          return (
            <TableRow key={e.id} tabIndex={0}
              className={cn('cursor-pointer', bad && 'bg-destructive/5 text-destructive hover:bg-destructive/10')}
              onClick={() => onOpen(e.id)} onKeyDown={(ev) => ev.key === 'Enter' && onOpen(e.id)}>
              <TableCell className={cn(CELL, 'whitespace-nowrap tabular-nums')}>{when(e.recorded_at)}</TableCell>
              <TableCell className={cn(CELL, 'font-medium')}>{KIND_LABEL[e.kind] || e.kind}</TableCell>
              <TableCell className={cn(CELL, 'min-w-[12rem] max-w-[22rem]')}>
                {e.trigger || '—'}
                <TriggerNotes entry={e} />
              </TableCell>
              <TableCell className={cn(CELL, 'whitespace-nowrap tabular-nums')}>{tempText(e)}</TableCell>
              <TableCell className={cn(CELL, 'whitespace-nowrap tabular-nums')}>{clockChange(e)}</TableCell>
              <TableCell className={cn(CELL, 'whitespace-nowrap tabular-nums')}>{fanChange(e)}</TableCell>
              <TableCell className={cn(CELL, 'whitespace-nowrap')}>{change(e.mode_old, e.mode_new, modeText)}</TableCell>
              <TableCell className={cn(CELL, 'min-w-[10rem] max-w-[16rem]')}>
                {hasCommand(e) ? <CommandText entry={e} /> : '—'}
              </TableCell>
              <TableCell className={CELL}><Result entry={e} /></TableCell>
              <TableCell className={cn(CELL, 'whitespace-nowrap tabular-nums')}>{apiText(e)}</TableCell>
              <TableCell className={cn(CELL, 'whitespace-nowrap tabular-nums')}>{takeoverText(e)}</TableCell>
            </TableRow>
          )
        })}
      </TableBody>
    </Table>
  )
}

/** Phone: one card per entry instead of the wide table. */
function EventCards({ rows, onOpen }) {
  return (
    <div className="space-y-2">
      {rows.map((e) => {
        const bad = isError(e)
        const facts = [
          ['Chip', tempText(e)],
          ['Takt', clockChange(e)],
          ['Lüfter', fanChange(e)],
          ['Modus', change(e.mode_old, e.mode_new, modeText)],
          ['API', apiText(e)],
          ['Übernahme', takeoverText(e)],
        ].filter(([, v]) => v !== '—')
        return (
          <button key={e.id} type="button" onClick={() => onOpen(e.id)}
            className={cn('w-full rounded-md border border-border/80 p-2.5 text-left text-xs',
              bad && 'border-destructive/60 bg-destructive/5')}>
            <span className="flex items-start justify-between gap-2">
              <span className={cn('font-medium', bad && 'text-destructive')}>{KIND_LABEL[e.kind] || e.kind}</span>
              <Result entry={e} />
            </span>
            <span className="block tabular-nums text-muted-foreground">{when(e.recorded_at)}</span>
            {e.trigger && <span className="mt-1 block">{e.trigger}</span>}
            <TriggerNotes entry={e} />
            {facts.length > 0 && (
              <span className="mt-1 block text-muted-foreground">
                {facts.map(([k, v]) => k + ' ' + v).join(' · ')}
              </span>
            )}
            {hasCommand(e) && (
              <span className="mt-1 block">
                <CommandText entry={e} />
              </span>
            )}
          </button>
        )
      })}
    </div>
  )
}

function TipBox({ title, children }) {
  return (
    <div className="max-w-xs rounded-lg border bg-background/95 p-3 text-xs shadow-lg backdrop-blur">
      {title && <div className="mb-1 text-muted-foreground">{title}</div>}
      {children}
    </div>
  )
}

/** Chip temperature before the command and every 30 s after the takeover (up to 10 values): the data-service
 * starts the series when the miner or the fan controller reports the new value, not when the command is sent. */
function ThermalChart({ before, values }) {
  const data = [
    ...(has(before) ? [{ min: 0, t: before }] : []),
    ...(values || []).map((t, i) => ({ min: (i + 1) * 0.5, t })),
  ]
  const label = (min) => (min === 0 ? 'vorher' : fmt(min, 1) + ' min')
  const tip = (min) => (min === 0 ? 'vor dem Befehl' : fmt(min, 1) + ' min nach der Übernahme')
  return (
    <div className="space-y-1">
      <p className="text-xs font-medium">
        Thermischer Verlauf (Chiptemperatur vor dem Befehl, dann alle 30 s nach der Übernahme)
      </p>
      {data.length > 1 && (
        <div className="h-[120px]">
          <ResponsiveContainer width="100%" height="100%">
            <LineChart data={data} margin={{ top: 4, right: 8, bottom: 0, left: 0 }}>
              <CartesianGrid strokeDasharray="3 3" className="stroke-muted/30" />
              <XAxis dataKey="min" type="number" domain={[0, 5]} ticks={[0, 1, 2, 3, 4, 5]}
                tickFormatter={(v) => v + ' min'} className="text-xs" axisLine={false} tickLine={false} />
              <YAxis domain={['dataMin - 1', 'dataMax + 1']} allowDecimals={false} tickFormatter={(v) => v + '°C'}
                width={44} className="text-xs" axisLine={false} tickLine={false} />
              <Tooltip content={({ active, payload }) => (active && payload?.length ? (
                <TipBox title={tip(payload[0].payload.min)}>{fmt(payload[0].value, 0, ' °C')}</TipBox>
              ) : null)} />
              <Line type="linear" dataKey="t" stroke="var(--destructive)" strokeWidth={2} dot={{ r: 3 }}
                isAnimationActive={false} />
            </LineChart>
          </ResponsiveContainer>
        </div>
      )}
      <p className="text-xs text-muted-foreground">
        {data.length ? data.map((p) => label(p.min) + ' ' + fmt(p.t, 0, ' °C')).join(' · ') : 'noch keine Werte'}
      </p>
    </div>
  )
}

function Facts({ rows }) {
  const shown = rows.filter(([, v]) => has(v) && v !== false && v !== '—')
  if (!shown.length) return null
  return (
    <dl className="grid grid-cols-[minmax(7rem,auto)_1fr] gap-x-3 gap-y-1 text-xs">
      {shown.map(([k, v]) => (
        <Fragment key={k}>
          <dt className="text-muted-foreground">{k}</dt>
          <dd className="min-w-0 break-words">{v}</dd>
        </Fragment>
      ))}
    </dl>
  )
}

const ISO_RE = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}/

function detailValue(value) {
  if (value === true) return 'ja'
  if (value === false) return 'nein'
  if (value !== null && typeof value === 'object') return JSON.stringify(value)
  if (typeof value === 'string' && ISO_RE.test(value)) return when(value)
  return String(value ?? '—')
}

const mhzText = (v) => fmt(v, 0, ' MHz')
const listText = (v) => (Array.isArray(v) ? v.join(' / ') : detailValue(v))
const learnedText = (v) => (v && typeof v === 'object'
  ? [mhzText(v.mhz), has(v.temp_avg) ? 'Ø ' + fmt(v.temp_avg, 1, ' °C') : null, v.at ? when(v.at) : null]
    .filter(Boolean).join(' · ')
  : detailValue(v))
const GROUND_TEXT = { standardtakt_fehlt: 'Standardtakt fehlt', takt_unter_100: 'Takt unter 100 MHz' }
const CHECK_TEXT = { rueckkehr: 'Rückkehr', ausnahme: 'Ausnahme' }
// the fixed keys of details (data-service/fan_automation.py): [label, value]; other keys as they are
const DETAILS = {
  quelle: ['Quelle'],
  starttakt: ['Starttakt', mhzText],
  basisstufe: ['Basisstufe'],
  grund: ['Grund', (v) => GROUND_TEXT[v] || v],
  modus: ['Modus', modeText],
  geschaetzt: ['geschätzte Punkte', listText],
  kein_befehl: ['kein Befehl'],
  punkte: ['Punkte', listText],
  schritt: ['Schritt', (v) => signed(v, 0, ' MHz')],
  testschritt: ['Testschritt'],
  meldung: ['Meldung'],
  nicht_erreichbar_s: ['nicht erreichbar', (v) => fmt(v, 0, ' s')],
  von: ['von', when],
  bis: ['bis', (v) => (typeof v === 'number' ? fmt(v, 0, ' °C') : when(v))],   // Heizmodus: Temperatur, sonst Zeitpunkt
  lernperiode_neu_gestartet: ['Lernperiode neu gestartet'],
  ergebnis: ['Ergebnis', (v) => RESULT_TEXT[v] || v],
  seit: ['seit'],
  takt: ['Takt', mhzText],
  luefterstufe: ['Lüfterstufe', levelText],
  luefterstufe_bleibt: ['Lüfterstufe bleibt', levelText],
  temp_mittel: ['Ø Temperatur', (v) => fmt(v, 1, ' °C')],
  anzahl: ['Anzahl'],
  vorher: ['vorher', learnedText],
  ab: ['ab', (v) => fmt(v, 0, ' °C')],
  luefter_plus: ['Lüfter', (v) => 'Basisstufe +' + v],
  takt_minus: ['Taktsenkung', (v) => '−' + mhzText(v)],
  pruefung: ['Prüfung', (v) => CHECK_TEXT[v] || v],
  dauer_s: ['Dauer', (v) => fmt(v, 0, ' s')],
  berechnet: ['berechnet', mhzText],
}

function detailRow([key, value]) {
  const [label, format] = DETAILS[key] || [key.replace(/_/g, ' ')]
  return [label, format && has(value) ? format(value) : detailValue(value)]
}

function EventDetail({ id, onClose }) {
  const [entry, setEntry] = useState(null)
  const [error, setError] = useState(null)

  useEffect(() => {
    let alive = true
    setEntry(null)
    setError(null)
    api.get(`/api/automation/log/${id}/`)
      .then(({ data }) => alive && setEntry(data))
      .catch((err) => alive && setError(errText(err, 'Eintrag nicht geladen.')))
    return () => {
      alive = false
    }
  }, [id])

  const e = entry
  const r = e ? resultOf(e) : null
  const takeover = e && e.takeover_state === 'gemessen'
    ? when(e.takeover_at) + ' · nach ' + fmt(e.takeover_s, 1, ' s') : e?.takeover_state
  return (
    <Dialog open onOpenChange={(open) => !open && onClose()}>
      <DialogContent className="max-w-2xl" onClose={onClose}>
        <DialogHeader>
          <DialogTitle className={cn(e && isError(e) && 'text-destructive')}>
            {e ? KIND_LABEL[e.kind] || e.kind : 'Eintrag'}
          </DialogTitle>
          <DialogDescription>
            {e ? when(e.recorded_at) + ' · ' + (SOURCE_LABEL[e.source] || e.source || '—') : ' '}
          </DialogDescription>
        </DialogHeader>
        {error && <p className="text-sm text-destructive">{error}</p>}
        {!e && !error && (
          <p className="flex items-center gap-2 text-sm text-muted-foreground">
            <Loader2 className="h-4 w-4 animate-spin" /> Lade …
          </p>
        )}
        {e && (
          <div className="space-y-4">
            <Facts rows={[
              ['Auslöser', e.trigger],
              ['Zustand vorher', PHASE_LABEL[e.phase] || e.phase],
              ['Chiptemperatur', tempText(e)],
              ['Basisstufe', change(e.base_old, e.base_new)],
              ['Lüfterstufe', change(e.fan_old, e.fan_new, levelText)],
              ['Takt', clockChange(e)],
              ['Takt laut Miner', has(e.clock_reported) ? fmt(e.clock_reported, 0, ' MHz') : null],
              ['Modus', change(e.mode_old, e.mode_new, modeText)],
              ['Ziel', TARGET_LABEL[e.target] || e.target],
              ['Endpunkt', e.endpoint],
              ['Befehl', e.request && <span className="font-mono">{e.request}</span>],
              ['Status', e.status],
              ['Ergebnis', r.text !== '—' && <Result entry={e} />],
              ['Fehler', e.error && <span className="text-destructive">{e.error}</span>],
            ]} />
            <div className="space-y-1">
              <p className="text-xs font-medium">Zeiten</p>
              <Facts rows={[
                ['Entscheidung', when(e.recorded_at)],
                ['Befehl gesendet', e.api_sent_at && when(e.api_sent_at)],
                ['Antwort erhalten', e.api_reply_at && when(e.api_reply_at)],
                ['API-Zeit', apiText(e)],
                ['Übernahme', takeover],
              ]} />
            </div>
            {e.response && (
              <div className="space-y-1">
                <p className="text-xs font-medium">Antwort</p>
                <pre className="max-h-48 overflow-auto whitespace-pre-wrap break-all rounded-md bg-muted/50 p-2 text-[11px]">
                  {e.response}
                </pre>
              </div>
            )}
            {(has(e.temp_before) || e.thermal?.length > 0) && <ThermalChart before={e.temp_before} values={e.thermal} />}
            {e.details && Object.keys(e.details).length > 0 && (
              <div className="space-y-1">
                <p className="text-xs font-medium">Einzelheiten</p>
                <Facts rows={Object.entries(e.details).map(detailRow)} />
              </div>
            )}
          </div>
        )}
      </DialogContent>
    </Dialog>
  )
}

function EventsTab() {
  const { range, setRange, custom, setCustom } = useRange('24h')
  const [kinds, setKinds] = useState([])
  const [ok, setOk] = useState('')
  const [page, setPage] = useState(1)
  const [data, setData] = useState(null)
  const [error, setError] = useState(null)
  const [selected, setSelected] = useState(null)
  const seq = useRef(0)

  const load = useCallback(async () => {
    const mine = ++seq.current
    const { from, to } = rangeParams(range, custom)
    try {
      const { data: d } = await api.get('/api/automation/log/', {
        params: clean({ page, from, to, kind: kinds.join(','), ok }),
      })
      if (mine !== seq.current) return
      setData(d)
      setError(null)
    } catch (err) {
      if (mine === seq.current) setError(errText(err, 'Protokoll nicht geladen.'))
    }
  }, [page, kinds, ok, range, custom])

  useEffect(() => {
    load()
    const timer = setInterval(load, EVENTS_REFRESH_MS)
    return () => clearInterval(timer)
  }, [load])

  // a new filter starts again on page 1
  const filtered = (set) => (value) => {
    set(value)
    setPage(1)
  }
  const rows = data?.results || []
  const pages = Math.max(data?.pages || 1, 1)
  // the backend clamps the page to the last one (e.g. after old entries were removed)
  const shown = Math.min(data?.page || page, pages)

  return (
    <Card>
      <CardHeader className="pb-2">
        <CardTitle className="text-base font-semibold">Ereignisse und Befehle</CardTitle>
        <CardDescription className="text-xs">
          Neueste zuerst, 100 je Seite, aktualisiert alle 10 s. Rot: fehlgeschlagene Befehle, Fehler, Over Hot, Miner
          antwortet nicht. Ein Klick auf einen Eintrag zeigt alle Einzelheiten.
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-3 pt-0">
        <div className="space-y-2">
          <KindFilter value={kinds} onChange={filtered(setKinds)} />
          <div className="flex flex-wrap items-center gap-3">
            <label className="flex items-center gap-1.5 text-xs">
              <span className="text-muted-foreground">Ergebnis</span>
              <span className="w-28">
                <Select className="h-9 py-1 text-xs" value={ok} onValueChange={filtered(setOk)} aria-label="Ergebnis">
                  <SelectOption value="">alle</SelectOption>
                  <SelectOption value="true">ok</SelectOption>
                  <SelectOption value="false">Fehler</SelectOption>
                  <SelectOption value="null">ohne Befehl</SelectOption>
                </Select>
              </span>
            </label>
            <RangePicker options={['heute', '24h', '7d', 'frei']} value={range} onChange={filtered(setRange)}
              custom={custom} onCustom={filtered(setCustom)} />
          </div>
        </div>
        {error && <p className="text-xs text-destructive">{error}</p>}
        {!data && !error && (
          <p className="flex items-center gap-2 text-sm text-muted-foreground">
            <Loader2 className="h-4 w-4 animate-spin" /> Lade Protokoll …
          </p>
        )}
        {data && rows.length === 0 && <p className="text-sm text-muted-foreground">Keine Einträge in diesem Zeitraum.</p>}
        {rows.length > 0 && (
          <>
            <div className="hidden md:block">
              <EventTable rows={rows} onOpen={setSelected} />
            </div>
            <div className="md:hidden">
              <EventCards rows={rows} onOpen={setSelected} />
            </div>
          </>
        )}
        {data && (
          <div className="flex flex-wrap items-center justify-between gap-2 text-xs">
            <span className="text-muted-foreground">
              {fmt(data.count, 0)} Einträge · Seite {shown} von {pages}
            </span>
            <span className="flex gap-2">
              <Button size="sm" variant="outline" className="h-8" disabled={shown <= 1} onClick={() => setPage(shown - 1)}>
                <ChevronLeft className="mr-1 h-3.5 w-3.5" /> Neuere
              </Button>
              <Button size="sm" variant="outline" className="h-8" disabled={shown >= pages}
                onClick={() => setPage(shown + 1)}>
                Ältere <ChevronRight className="ml-1 h-3.5 w-3.5" />
              </Button>
            </span>
          </div>
        )}
        {selected != null && <EventDetail id={selected} onClose={() => setSelected(null)} />}
      </CardContent>
    </Card>
  )
}

// ---------------------------------------------------------------- Reaktionszeiten

const ms = (v) => fmt(v, 0, ' ms')
const sec = (v) => fmt(v, 1, ' s')
const deg = (v) => signed(v, 1, ' °C')
const API_COLUMNS = [
  { label: 'Anzahl', get: (r) => fmt(r.anzahl, 0) },
  { label: 'API Ø', get: (r) => ms(r.api_ms_mittel) },
  { label: 'API Median', get: (r) => ms(r.api_ms_median) },
  { label: 'Übernahme Ø', get: (r) => sec(r.uebernahme_s_mittel) },
  { label: 'Übernahme Median', get: (r) => sec(r.uebernahme_s_median) },
]
const STATS = [
  {
    key: 'modes',
    title: 'Moduswechsel',
    columns: [
      { label: 'Von → nach', get: (r) => change(r.von, r.nach, modeText) },
      ...API_COLUMNS,
      { label: 'Übernahme min', get: (r) => sec(r.uebernahme_min) },
      { label: 'Übernahme max', get: (r) => sec(r.uebernahme_max) },
    ],
  },
  {
    key: 'clocks',
    title: 'Taktwerte',
    description: 'Δ = Chiptemperatur 1, 3 und 5 min nach der Übernahme gegenüber vor dem Befehl (Mittel).',
    columns: [
      { label: 'Takt', get: (r) => fmt(r.takt, 0, ' MHz') },
      ...API_COLUMNS,
      { label: 'Δ 1 min', get: (r) => deg(r.thermisch_1min) },
      { label: 'Δ 3 min', get: (r) => deg(r.thermisch_3min) },
      { label: 'Δ 5 min', get: (r) => deg(r.thermisch_5min) },
    ],
  },
  {
    key: 'fans',
    title: 'Lüfterstufen',
    columns: [{ label: 'Stufe', get: (r) => levelText(r.stufe) }, ...API_COLUMNS],
  },
]

function StatTable({ columns, rows }) {
  if (!rows?.length) return <p className="text-xs text-muted-foreground">Keine Befehle in diesem Zeitraum.</p>
  return (
    <Table>
      <TableHeader>
        <TableRow>
          {columns.map((c) => (
            <TableHead key={c.label} className={HEAD}>{c.label}</TableHead>
          ))}
        </TableRow>
      </TableHeader>
      <TableBody>
        {rows.map((r, i) => (
          <TableRow key={i}>
            {columns.map((c) => (
              <TableCell key={c.label} className={cn(CELL, 'whitespace-nowrap tabular-nums')}>{c.get(r)}</TableCell>
            ))}
          </TableRow>
        ))}
      </TableBody>
    </Table>
  )
}

function StatsTab() {
  const { range, setRange, custom, setCustom } = useRange('7d')
  const [data, setData] = useState(null)
  const [error, setError] = useState(null)

  useEffect(() => {
    let alive = true
    const { from, to } = rangeParams(range, custom)
    api.get('/api/automation/log/stats/', { params: clean({ from, to }) })
      .then(({ data: d }) => {
        if (!alive) return
        setData(d)
        setError(null)
      })
      .catch((err) => alive && setError(errText(err, 'Auswertung nicht geladen.')))
    return () => {
      alive = false
    }
  }, [range, custom])

  return (
    <div className="space-y-4">
      <div className="space-y-1">
        <RangePicker options={['heute', '24h', '7d', 'frei']} value={range} onChange={setRange} custom={custom}
          onCustom={setCustom} />
        <p className="text-xs text-muted-foreground">
          Nur erfolgreiche Befehle. API = Dauer der Anfrage; Übernahme = bis der Miner bzw. die Lüftersteuerung den
          neuen Wert meldet (nur wo gemessen).
        </p>
        {error && <p className="text-xs text-destructive">{error}</p>}
      </div>
      {!data && !error && (
        <p className="flex items-center gap-2 text-sm text-muted-foreground">
          <Loader2 className="h-4 w-4 animate-spin" /> Lade Auswertung …
        </p>
      )}
      {data && STATS.map((s) => (
        <Card key={s.key}>
          <CardHeader className="pb-2">
            <CardTitle className="text-base font-semibold">{s.title}</CardTitle>
            {s.description && <CardDescription className="text-xs">{s.description}</CardDescription>}
          </CardHeader>
          <CardContent className="pt-0">
            <StatTable columns={s.columns} rows={data[s.key]} />
          </CardContent>
        </Card>
      ))}
    </div>
  )
}

// ---------------------------------------------------------------- Verlauf

function nearest(points, ts) {
  let lo = 0
  let hi = points.length - 1
  if (hi < 0) return null
  while (lo < hi) {
    const mid = (lo + hi) >> 1
    if (points[mid].ts < ts) lo = mid + 1
    else hi = mid
  }
  if (lo > 0 && ts - points[lo - 1].ts < points[lo].ts - ts) lo -= 1
  return points[lo]
}

/** Points with a time stamp, events at their nearest point (for the tooltip), states as background areas. */
function buildSeries(data) {
  const points = (data?.points || [])
    .map((p) => ({ ...p, ts: Date.parse(p.t), events: [] }))
    .filter((p) => !Number.isNaN(p.ts))
    .sort((a, b) => a.ts - b.ts)
  const events = (data?.events || [])
    .map((e) => ({ ...e, ts: Date.parse(e.t) }))
    .filter((e) => !Number.isNaN(e.ts))
  events.forEach((e) => nearest(points, e.ts)?.events.push(e))
  const areas = []
  points.forEach((p, i) => {
    const last = areas[areas.length - 1]
    const end = points[i + 1]?.ts ?? p.ts
    if (last && last.phase === p.phase && last.x2 === p.ts) last.x2 = end
    else areas.push({ phase: p.phase, x1: p.ts, x2: end })
  })
  return { points, events, areas: areas.filter((a) => a.phase && a.phase !== 'normal' && a.x2 > a.x1) }
}

function timeAxis(spanMs) {
  const long = spanMs > 24 * HOUR_MS
  return {
    tick: (v) => new Date(v).toLocaleString('de-DE', long
      ? { day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit' }
      : { hour: '2-digit', minute: '2-digit' }),
    tooltip: (v) => when(new Date(v).toISOString()),
  }
}

function LineSwatch({ color, dashed }) {
  return (
    <svg width="18" height="8" aria-hidden="true">
      <line x1="1" y1="4" x2="17" y2="4" stroke={color} strokeWidth="2" strokeDasharray={dashed ? '4 3' : undefined} />
    </svg>
  )
}

function Legend({ lines }) {
  if (lines.length < 2) return null
  return (
    <span className="flex flex-wrap items-center gap-3 text-xs text-muted-foreground">
      {lines.map((l) => (
        <span key={l.key} className="flex items-center gap-1">
          <LineSwatch color={l.color} dashed={l.dashed} />
          {l.label}
        </span>
      ))}
    </span>
  )
}

function HistoryTooltip({ active, payload, label, axis, lines, withEvents }) {
  if (!active || !payload?.length) return null
  const p = payload[0].payload
  return (
    <TipBox title={has(label) ? axis.tooltip(label) : null}>
      {lines.map((l) => (
        <div key={l.key} className="flex justify-between gap-4">
          <span className="text-muted-foreground">{l.label}</span>
          <span className="font-semibold tabular-nums">{l.format(p[l.key])}</span>
        </div>
      ))}
      <div className="flex justify-between gap-4">
        <span className="text-muted-foreground">Zustand</span>
        <span>{PHASE_LABEL[p.phase] || p.phase || '—'}</span>
      </div>
      {withEvents && p.decision && <p className="mt-1">{p.decision}</p>}
      {withEvents && p.events.map((e) => (
        <p key={e.id} className={cn('mt-1', ERROR_KINDS.has(e.kind) && 'text-destructive')}>
          {KIND_LABEL[e.kind] || e.kind}
          {e.trigger ? ': ' + e.trigger : ''}
        </p>
      ))}
    </TipBox>
  )
}

function HistoryChart({ title, series, axis, lines, yAxis, loading, withEvents = false }) {
  return (
    <ChartFrame title={title} legend={<Legend lines={lines} />} loading={loading} empty={series.points.length < 2}
      emptyTitle="Keine Werte in diesem Zeitraum" emptyDescription="Die Automatik schreibt alle 5 s einen Regelzyklus."
      heightClass="h-[170px] sm:h-[200px]">
      <ResponsiveContainer width="100%" height="100%">
        <LineChart data={series.points} syncId="verlauf" margin={{ top: 4, right: 8, bottom: 0, left: 0 }}>
          <CartesianGrid strokeDasharray="3 3" className="stroke-muted/30" />
          {series.areas.map((a) => (
            <ReferenceArea key={'a' + a.x1} x1={a.x1} x2={a.x2} fill={PHASE_FILL[a.phase] || PHASE_FILL_OTHER}
              fillOpacity={0.15} stroke="none" ifOverflow="hidden" />
          ))}
          {series.events.map((e) => (
            <ReferenceLine key={'e' + e.id} x={e.ts} ifOverflow="hidden" strokeDasharray="2 3"
              stroke={ERROR_KINDS.has(e.kind) ? 'var(--destructive)' : 'var(--muted-foreground)'} strokeOpacity={0.6} />
          ))}
          <XAxis dataKey="ts" type="number" scale="time" domain={['dataMin', 'dataMax']} tickFormatter={axis.tick}
            minTickGap={48} className="text-xs" axisLine={false} tickLine={false} />
          <YAxis {...yAxis} className="text-xs" axisLine={false} tickLine={false} width={44} />
          <Tooltip content={(props) => (
            <HistoryTooltip {...props} axis={axis} lines={lines} withEvents={withEvents} />
          )} />
          {lines.map((l) => (
            <Line key={l.key} type={l.step ? 'stepAfter' : 'linear'} dataKey={l.key} stroke={l.color}
              strokeWidth={2} strokeDasharray={l.dashed ? '4 3' : undefined} dot={false} isAnimationActive={false} />
          ))}
        </LineChart>
      </ResponsiveContainer>
    </ChartFrame>
  )
}

const TEMP_LINES = [
  { key: 'chip_temp', label: 'Chiptemperatur', color: 'var(--destructive)', format: (v) => fmt(v, 0, ' °C') },
]
const CLOCK_LINES = [
  { key: 'clock_set', label: 'gesetzt', color: 'var(--chart-1)', step: true, format: (v) => fmt(v, 0, ' MHz') },
  { key: 'clock_reported', label: 'laut Miner', color: 'var(--chart-3)', dashed: true, format: (v) => fmt(v, 0, ' MHz') },
]
// GHSspd of the miner (TH/s, only error-free results); the miner reports a new value about every 130 s
const HASHRATE_LINES = [
  { key: 'ghsspd_ths', label: 'Hashrate', color: 'var(--chart-2)', step: true, format: (v) => fmt(v, 1, ' TH/s') },
]
const FAN_LINES = [
  { key: 'fan_level', label: 'aktive Stufe', color: 'var(--chart-1)', step: true,
    format: (v) => (has(v) ? levelText(v) : '—') },
  { key: 'base_level', label: 'Basisstufe', color: 'var(--muted-foreground)', step: true, dashed: true,
    format: (v) => (has(v) ? String(v) : '—') },
]

/** Length of a range in ms; without 'von' the backend shows the 2 h before 'bis'. */
function spanMs(from, to) {
  const end = to ? Date.parse(to) : Date.now()
  return from ? end - Date.parse(from) : 2 * HOUR_MS
}

function HistoryTab() {
  const { range, setRange, custom, setCustom } = useRange('2h')
  const [data, setData] = useState(null)
  const [error, setError] = useState(null)
  const seq = useRef(0)
  const running = useRef(null)     // AbortController of the request in flight

  // 'frei': at most 7 days; reload every 30 s only while 'bis' is open or in the future
  const fixed = range === 'frei' ? rangeParams(range, custom) : null
  const tooLong = !!fixed && spanMs(fixed.from, fixed.to) > HISTORY_MAX_DAYS * 24 * HOUR_MS
  const live = !fixed?.to || Date.parse(fixed.to) > Date.now()

  const load = useCallback(async () => {
    running.current?.abort()
    const ctrl = new AbortController()
    running.current = ctrl
    const mine = ++seq.current
    const { from, to } = rangeParams(range, custom)
    try {
      const { data: d } = await api.get('/api/automation/cycles/', { params: clean({ from, to }), signal: ctrl.signal })
      if (mine !== seq.current) return
      setData(d)
      setError(null)
    } catch (err) {
      if (!ctrl.signal.aborted && mine === seq.current) setError(errText(err, 'Verlauf nicht geladen.'))
    } finally {
      if (running.current === ctrl) running.current = null
    }
  }, [range, custom])

  useEffect(() => {
    if (tooLong) return undefined
    load()
    // no new request while the last one is still running
    const timer = live ? setInterval(() => !running.current && load(), CYCLES_REFRESH_MS) : null
    return () => {
      clearInterval(timer)
      running.current?.abort()
    }
  }, [load, tooLong, live])

  const series = useMemo(() => buildSeries(data), [data])
  const pts = series.points
  const axis = timeAxis(pts.length ? pts[pts.length - 1].ts - pts[0].ts : 0)
  const loading = !data && !error
  const phases = [...new Set(series.areas.map((a) => a.phase))]

  return (
    <div className="space-y-3">
      <div className="space-y-1">
        <RangePicker options={['2h', 'heute', '24h', '7d', 'frei']} value={range} onChange={setRange} custom={custom}
          onCustom={setCustom} />
        <p className="text-xs text-muted-foreground">
          {data?.bucket_s > 5 ? 'Je Minute verdichtet' : 'Jeder Regelzyklus (alle 5 s)'}
          {live ? ', aktualisiert alle 30 s.' : '.'} Senkrechte Linien = Ereignisse (rot: Fehler, Over Hot, Miner
          antwortet nicht).
        </p>
        {tooLong && (
          <p className="text-xs text-amber-600">Höchstens {HISTORY_MAX_DAYS} Tage: bitte den Zeitraum kürzer wählen.</p>
        )}
        {error && !tooLong && <p className="text-xs text-destructive">{error}</p>}
      </div>
      {!tooLong && (
        <>
          <HistoryChart title="Chiptemperatur" series={series} axis={axis} lines={TEMP_LINES} loading={loading}
            withEvents
            yAxis={{ domain: ['dataMin - 2', 'dataMax + 2'], allowDecimals: false, tickFormatter: (v) => v + '°C' }} />
          <HistoryChart title="Takt (MHz)" series={series} axis={axis} lines={CLOCK_LINES} loading={loading}
            yAxis={{ domain: ['dataMin - 10', 'dataMax + 10'], allowDecimals: false }} />
          <HistoryChart title="Hashrate (TH/s)" series={series} axis={axis} lines={HASHRATE_LINES} loading={loading}
            yAxis={{ domain: ['dataMin - 5', 'dataMax + 5'], allowDecimals: false, tickFormatter: (v) => Math.round(v) }} />
          <HistoryChart title="Lüfterstufe" series={series} axis={axis} lines={FAN_LINES} loading={loading}
            yAxis={{ domain: [0, 10], ticks: [0, 2, 4, 6, 8, 10], allowDecimals: false }} />
          {phases.length > 0 && (
            <p className="flex flex-wrap items-center gap-3 text-xs text-muted-foreground">
              <span>Hintergrund:</span>
              {phases.map((phase) => (
                <span key={phase} className="flex items-center gap-1">
                  <span className="inline-block h-3 w-3 rounded-sm"
                    style={{ backgroundColor: PHASE_FILL[phase] || PHASE_FILL_OTHER, opacity: 0.5 }} />
                  {PHASE_LABEL[phase] || phase}
                </span>
              ))}
            </p>
          )}
        </>
      )}
    </div>
  )
}

// ---------------------------------------------------------------- CSV-Export

function ExportCard() {
  const { range, setRange, custom, setCustom } = useRange('24h')
  const [table, setTable] = useState('log')
  const [error, setError] = useState(null)

  // a plain link instead of a request: the browser writes the streamed file straight to disk (login by the
  // session cookie, the file name comes from the server)
  const download = () => {
    const { from, to } = rangeParams(range, custom)
    if (from && to && from > to) {
      setError('Ungültiger Zeitraum.')
      return
    }
    setError(null)
    const a = document.createElement('a')
    const query = new URLSearchParams(clean({ table, from, to }))
    a.href = (api.defaults?.baseURL || '') + '/api/automation/export.csv?' + query
    a.download = ''
    a.click()
  }

  return (
    <Card>
      <CardHeader className="pb-2">
        <CardTitle className="text-base font-semibold">CSV-Export</CardTitle>
        <CardDescription className="text-xs">Alle Spalten, Semikolon getrennt.</CardDescription>
      </CardHeader>
      <CardContent className="space-y-2 pt-0">
        <div className="flex flex-wrap items-center gap-3">
          <label className="flex items-center gap-1.5 text-xs">
            <span className="text-muted-foreground">Tabelle</span>
            <span className="w-32">
              <Select className="h-9 py-1 text-xs" value={table} onValueChange={setTable} aria-label="Tabelle">
                <SelectOption value="log">Ereignisse</SelectOption>
                <SelectOption value="cycle">Zyklen</SelectOption>
              </Select>
            </span>
          </label>
          <RangePicker options={['heute', '24h', '7d', 'frei']} value={range} onChange={setRange} custom={custom}
            onCustom={setCustom} />
        </div>
        <Button size="sm" onClick={download}>
          <Download className="mr-1.5 h-3.5 w-3.5" />
          CSV herunterladen
        </Button>
        {error && <p className="text-xs text-destructive">{error}</p>}
      </CardContent>
    </Card>
  )
}

export default function ProtocolPage() {
  return (
    <div className="space-y-4">
      <Tabs defaultValue="ereignisse">
        <TabsList>
          <TabsTrigger value="ereignisse">Ereignisse</TabsTrigger>
          <TabsTrigger value="reaktionszeiten">Reaktionszeiten</TabsTrigger>
          <TabsTrigger value="verlauf">Verlauf</TabsTrigger>
        </TabsList>
        <TabsContent value="ereignisse">
          <EventsTab />
        </TabsContent>
        <TabsContent value="reaktionszeiten">
          <StatsTab />
        </TabsContent>
        <TabsContent value="verlauf">
          <HistoryTab />
        </TabsContent>
      </Tabs>
      <ExportCard />
    </div>
  )
}
