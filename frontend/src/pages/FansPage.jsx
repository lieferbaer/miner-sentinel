/**
 * Lüfter (AC Infinity, Testmenü): Controller und Ports anzeigen, Einstellungen sichern/zurückschreiben und
 * testen, welcher Schreibweg die Lüfterstufe am echten Gerät wirklich ändert. Dazu die Temperatur-Automatik der A15
 * (Regeln v2.2.7, Nutzer 06.11.): Heiz-, Basis- oder Kühlmodus des Lüfters, Takt nach der Zieltemperatur, Lernwerte.
 */
import { formatHashrate, formatNumber } from '@/components/dashboard'
import TargetRangeCard, { targetOf } from '@/components/fans/TargetRangeCard'
import MetricCard from '@/components/metrics/MetricCard'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import api from '@/lib/api'
import { KIND_LABEL, clockChange, fanChange, has, isError, modeText, tempText } from '@/lib/automationLog'
import { useLive } from '@/lib/LiveContext'
import { Activity, Fan, Gauge, Hash, Loader2, RefreshCw, Thermometer } from 'lucide-react'
import { useCallback, useEffect, useState } from 'react'
import { Link } from 'react-router-dom'

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

// state of the regulation (fan_automation.state.phase, rules of v2.2.7, Nutzer 06.11.): exactly one fan mode at a
// time; the numbers follow the Zieltemperatur ZTU–ZTO = lo–hi
function phases(lo, hi) {
  return {
    heizmodus: { value: 'Heizmodus', sub: 'endet ab ' + (lo - 4) + ' °C' },
    basismodus: { value: 'Basismodus', sub: 'Takt regelt ' + lo + '–' + hi + ' °C' },
    kuehlmodus: { value: 'Kühlmodus', sub: 'endet bei ' + (hi - 4) + ' °C oder kälter' },
    wartung: { value: 'Wartung', sub: 'A15 aus · Lüfter aus · endet beim Einschalten' },
    aus: { value: 'Aus', sub: '' },
  }
}
// commands, locks and learning period are only current while the regulation runs on valid readings
const REGULATING = new Set(['heizmodus', 'basismodus', 'kuehlmodus'])
// the status card keeps the calm tone in these modes; Kühlmodus (hot) is a warning like the old Korrekturphase
const CALM_PHASES = new Set(['heizmodus', 'basismodus'])
const MODE_LABEL = { '2/L4': 'mode 04', '2/L3': 'mode 03', '2/L2': 'mode 02', '2/L1': 'mode 01', '2/L0': 'mode 00',
  '1/-': 'High Performance' }
// clock-increase locks of the data-service (state.locks, ISO "until" times) and why they run
const LOCK_REASON = { erhoehung: 'nach Erhöhung', heizmodus: 'nach Heizmodus', kuehlmodus: 'nach Kühlmodus',
  senkung: 'nach Senkung' }

// the rules in short (v2.2.7, Nutzer 06.11.) with the current Zieltemperatur lo–hi (ZTU–ZTO)
function logicText(lo, hi) {
  const range = lo + '–' + hi + ' °C'
  return 'Der Takt hält den heißesten Chip in der Zieltemperatur ' + range + ' (ZTU ' + lo + ' °C, ZTO ' + hi +
    ' °C); dort keine Taktänderung. Der Lüfter ist immer in genau einem Modus. Basismodus: Lüfter auf der gewählten ' +
    'Basisstufe. Kühlmodus: ab ' + (hi + 3) + ' °C Basisstufe +1, ab ' + (hi + 8) + ' °C +2 (höchstens Stufe 10; ' +
    'das Plus geht bis zum Ende nicht zurück); endet bei ' + (hi - 4) + ' °C oder kälter, zurück auf die Basisstufe. ' +
    'Heizmodus: bei ' + (lo - 11) + ' °C oder kälter Lüfter −1, sobald die Lüfterstufe seit über 1 min unverändert ' +
    'ist, so jede Minute weiter (mindestens Stufe 1); bei ' + (lo - 10) + '–' + (lo - 5) + ' °C bleibt die Stufe; ' +
    'endet ab ' + (lo - 4) + ' °C, zurück auf die Basisstufe. Nach dem Ende von Heiz- oder Kühlmodus 3 min keine ' +
    'Takterhöhung. ' +
    'Unter ' + lo + ' °C sofort Takt +5 MHz bei ' + (lo - 3) + '–' + (lo - 1) + ' °C, +15 bei ' + (lo - 5) + '–' +
    (lo - 4) + ' °C, +20 bei ' + (lo - 10) + '–' + (lo - 6) + ' °C, +25 bei ' + (lo - 20) + '–' + (lo - 11) +
    ' °C, +40 bei ' + (lo - 21) + ' °C oder kälter (höchstens 440 MHz); danach keine Takterhöhung für 3 min ' +
    '(+5) bzw. 2 min (+15, +20, +25, +40). Diese Sperren und die nach Heiz- oder Kühlmodus enden früher ab ' +
    (lo + 4) + ' °C. ' +
    // the lock after an increase also ends 3 degrees below the temperature at that increase (Nutzer 09.11.)
    'Die Sperre nach einer Erhöhung endet außerdem, sobald die Chiptemperatur mindestens 3 °C unter der bei der Erhöhung ' +
    'liegt. Keine Takterhöhung, solange ein Befehl noch nicht übernommen ist. ' +
    'Über ' + hi + ' °C Takt runter: Bereich 1 (' + (hi + 1) + '–' + (hi + 3) + ' °C) −10 / −5 MHz bei ' +
    'einer ZeitImZieltemperatur davor (ununterbrochen in ' + range + ') bis 5 min / über 5 min, ' +
    'Bereich 2 (' + (hi + 4) + '–' + (hi + 6) + ' °C) −20 MHz, Bereich 3 (' + (hi + 7) + '–' + (hi + 9) +
    ' °C) −20 MHz, Bereich 4 (ab ' + (hi + 10) + ' °C) −50 MHz; mindestens 100 MHz. Die erste Senkung sofort ' +
    '(lag die letzte unter 3 min zurück: 3 min nach ihr, in einem höheren Bereich als bei ihr sofort), im ' +
    'selben Bereich alle 2 min, beim Anstieg in einen ' +
    'höheren Bereich sofort, beim Rückgang in einen niedrigeren 2 min nach der Ankunft dort. Nach jeder Senkung ' +
    '5 min keine Takterhöhung. ' +
    'Bei Basisstufe 1: nach einer Senkung um −5 MHz 30 min keine Takterhöhung bei ' + (lo - 2) + '–' + (lo - 1) +
    ' °C (ein Start hebt das auf). ' +
    'Nach Neustart, Wartung oder Over Hot keine Takterhöhung bis ' + (lo - 5) + ' °C, höchstens 10 min ' +
    '(Startpause). Weicht der Takt laut Miner 15 min ununterbrochen um mehr als 15 MHz vom gesetzten ab, wird der ' +
    'gesetzte Takt erneut gesendet. Fehlerrate 30 min ununterbrochen über 10 %: automatischer Neustart der A15 ' +
    '(höchstens einer je 6 h).'
}
function startText(lo, hi) {
  return 'Start (Stufenwahl, Einschalten, Neustart, nach Wartung oder Over Hot): Heiz- und Kühlmodus enden ohne ' +
    'Sperre, Lüfter auf die Basisstufe; Starttakt = Lernwert der Basisstufe, sonst der der nächstgelegenen ' +
    'Zieltemperatur, sonst Stufe 1 = 170, 2 = 250, 3 = 330, 4 = 380, 5 = 400, 6 = 420, ab 7 = 440 MHz. ' +
    'Lernwert: 15 min stabil in ' + lo + '–' + hi + ' °C bei unveränderter Lüfterstufe, nur bei einer Fehlerrate ' +
    'unter 5 %, bei Lüfterstufe 1 unter 7 % (Mittel der 15 min).'
}

function clockTime(iso) {
  return new Date(iso).toLocaleTimeString('de-DE', { hour: '2-digit', minute: '2-digit' })
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

// only the per-level values of the regulation since 24.10. (older values per mode have no 'mhz' here)
const perLevel = (learned) =>
  Object.entries(learned || {}).filter(([, v]) => v && typeof v.mhz === 'number')
    .sort(([a], [b]) => Number(a) - Number(b))

/** The learned values of the other Zieltemperaturen ({"81-84": {"<stufe>": …}}), sorted by ZTU, then ZTO. */
function otherEntries(others) {
  return Object.entries(others || {})
    .map(([key, values]) => [key, key.split('-').map(Number), values])
    .filter(([, zb]) => zb.length === 2 && zb.every(Number.isInteger))
    .sort(([, a], [, b]) => a[0] - b[0] || a[1] - b[1])
    .flatMap(([key, zb, values]) =>
      perLevel(values).map(([lv, v]) => ({ lv, v, range: zb[0] + '–' + zb[1] + ' °C', zbKey: key })))
}

/** The learned values grouped by Stufe (Nutzer 09.11.): levels ascending, in each Stufe first the value of the current
 * Zieltemperatur, then (grey) those of the other Zieltemperaturen in their order by ZTU, then ZTO. */
function byLevel(current, other) {
  const levels = [...new Set([...current, ...other].map(({ lv }) => Number(lv)))].sort((a, b) => a - b)
  return levels.map((lv) => ({
    lv,
    rows: [...current.filter((row) => Number(row.lv) === lv).map((row) => ({ ...row, muted: false })),
      ...other.filter((row) => Number(row.lv) === lv).map((row) => ({ ...row, muted: true }))],
  }))
}

// the miner work mode during the learning period (Nutzer 09.11.); old values have none
const modeOf = (v) => (v.mode ? MODE_LABEL[v.mode] || v.mode : '—')

/** Learned values per fan level after 15 min in the Zieltemperatur: the clock set by the regulation (since v2.2.9 the
 * one at the end, before the mean), the mean clock reported by the miner (Nutzer 08.11.; no Obergrenzen any more
 * since v2.2.7, Nutzer 06.11.) and the work mode (Nutzer 09.11.). Grouped by Stufe, in each the value of the current
 * Zieltemperatur first, then grey those of the other Zieltemperaturen (Nutzer 09.11.). Each value can be deleted
 * (Nutzer 08.11.); the data-service deletes it, the live poll (every 5 s) then shows it gone. */
function LearnedTable({ learned, others, zb, level, lo, hi, onCommand, busy, requested, setRequested }) {
  const [confirm, setConfirm] = useState(null)
  // Zieltemperatur (Nutzer 28.10.): the learned values belong to the one the data-service has taken over (state.zb)
  const known = Array.isArray(zb) && zb.length === 2
  const range = known ? zb[0] + '–' + zb[1] + ' °C' : '—'
  // without state.zb the current values cannot be addressed, so no delete button for them
  const current = perLevel(learned).map(([lv, v]) => ({ lv, v, range, zbKey: known ? zb[0] + '-' + zb[1] : null }))
  const other = otherEntries(others)
  const none = (
    <p className="text-muted-foreground">
      Lernwerte: noch keine (nach 15 min stabil bei {lo}–{hi} °C und einer Fehlerrate unter 5 %, bei Lüfterstufe 1
      unter 7 %).
    </p>
  )
  if (!current.length && !other.length) return none
  const groups = byLevel(current, other)
  // bold: the value of the current Zieltemperatur at the Basisstufe; grey: those of the other Zieltemperaturen
  const tone = ({ lv, muted }) => (muted ? 'text-muted-foreground' : Number(lv) === level ? 'font-semibold' : '')
  const item = (label, value) => (
    <div>
      <div className="text-xs font-normal text-muted-foreground">{label}</div>
      <div className="whitespace-nowrap">{value}</div>
    </div>
  )
  // Löschen with the inline question like Zurückschreiben; the request carries the 'at' of the shown value so that a
  // value learned anew in the meantime is not deleted
  const remove = ({ lv, v, zbKey }, size) => {
    if (!zbKey) return null
    const key = zbKey + '/' + lv + '/' + v.at
    if (confirm === key) {
      return (
        <span className="flex flex-wrap items-center gap-2">
          <span className="whitespace-nowrap">Lernwert wirklich löschen?</span>
          <Button size="sm" variant="destructive" className={size} disabled={busy}
            onClick={async () => {
              setConfirm(null)
              setRequested((prev) => [...prev, key])
              // a failed request deletes nothing, so the value can be deleted again
              const ok = await onCommand({ action: 'learned_delete', zb: zbKey, level: Number(lv), at: v.at })
              if (!ok) setRequested((prev) => prev.filter((k) => k !== key))
            }}>
            Ja
          </Button>
          <Button size="sm" variant="secondary" className={size} onClick={() => setConfirm(null)}>
            Nein
          </Button>
        </span>
      )
    }
    return (
      <Button size="sm" variant="outline" className={size} disabled={busy || requested.includes(key)}
        onClick={() => setConfirm(key)}>
        Löschen
      </Button>
    )
  }
  // mobile: per Stufe a small heading, then one entry per Zieltemperatur like the Letzte Einträge (Nutzer 28.10.:
  // Tabelle nicht mobil freundlich; grouped since Nutzer 09.11.)
  const entries = (
    <div className="sm:hidden">
      {groups.map(({ lv, rows: list }) => (
        <div key={lv} className="pt-1.5">
          <p className="font-medium">Stufe {lv}</p>
          <ul className="divide-y divide-border/40">
            {list.map((row) => {
              const { v, range: r } = row
              const action = remove(row, 'h-8 px-3 text-xs')
              return (
                <li key={r} className={['py-1.5', tone(row)].filter(Boolean).join(' ')}>
                  <div className="flex items-baseline justify-between gap-2">
                    <span>{r}</span>
                    <span className="tabular-nums text-muted-foreground">{when(v.at)}</span>
                  </div>
                  <div className="mt-0.5 grid grid-cols-2 gap-x-3 gap-y-1 tabular-nums">
                    {item('Gesetzter Takt', fmt(v.mhz, 0, ' MHz'))}
                    {item('Takt laut Miner (Ø)', fmt(v.mhz_miner, 0, ' MHz'))}
                    {item('Modus', modeOf(v))}
                    {item('Ø Temperatur', fmt(v.temp_avg, 1, ' °C'))}
                    {item('Ø Hashrate', fmt(v.ths_avg, 1, ' TH/s'))}
                    {item('Effizienz', fmt(v.j_th, 1, ' J/TH'))}
                    {item('Anzahl', v.count ?? '—')}
                  </div>
                  {action && <div className="mt-1">{action}</div>}
                </li>
              )
            })}
          </ul>
        </div>
      ))}
    </div>
  )
  // desktop: a line between the Stufe groups (top border on the first row of each further group)
  const rows = groups.flatMap(({ lv, rows: list }, g) => list.map((row, i) => {
    const { v, range: r } = row
    const line = g > 0 && i === 0 ? 'border-t border-border/60' : ''
    return (
      <tr key={r + lv} className={[tone(row), line].filter(Boolean).join(' ')}>
        <td className="pr-3">{lv}</td>
        <td className="whitespace-nowrap pr-3 tabular-nums">{r}</td>
        <td className="pr-3 tabular-nums">{fmt(v.mhz, 0, ' MHz')}</td>
        <td className="pr-3 tabular-nums">{fmt(v.mhz_miner, 0, ' MHz')}</td>
        <td className="whitespace-nowrap pr-3">{modeOf(v)}</td>
        <td className="pr-3 tabular-nums">{fmt(v.temp_avg, 1, ' °C')}</td>
        <td className="whitespace-nowrap pr-3 tabular-nums">{fmt(v.ths_avg, 1, ' TH/s')}</td>
        <td className="whitespace-nowrap pr-3 tabular-nums">{fmt(v.j_th, 1, ' J/TH')}</td>
        <td className="pr-3 tabular-nums">{v.count ?? '—'}</td>
        <td className="pr-3 text-muted-foreground">{when(v.at)}</td>
        <td className="py-0.5">{remove(row, 'h-6 px-2 text-xs')}</td>
      </tr>
    )
  }))
  return (
    <div className="pt-1">
      {current.length ? <p className="font-medium">Lernwerte</p> : none}
      {entries}
      <div className="hidden overflow-x-auto sm:block">
        <table className="text-xs">
          <thead className="text-muted-foreground">
            <tr className="text-left">
              <th className="pr-3 font-medium">Stufe</th>
              <th className="pr-3 font-medium">Zieltemperatur</th>
              <th className="pr-3 font-medium">Gesetzter Takt</th>
              <th className="pr-3 font-medium">Takt laut Miner (Ø)</th>
              <th className="pr-3 font-medium">Modus</th>
              <th className="pr-3 font-medium">Ø Temperatur</th>
              <th className="pr-3 font-medium">Ø Hashrate</th>
              <th className="pr-3 font-medium">Effizienz</th>
              <th className="pr-3 font-medium">Anzahl</th>
              <th className="pr-3 font-medium">Zeit</th>
              <th />
            </tr>
          </thead>
          <tbody>{rows}</tbody>
        </table>
      </div>
    </div>
  )
}

// a lock or pause of the data-service that still runs (ISO "until" in the future)
const runs = (until) => Boolean(until) && Date.parse(until) > Date.now()

// Basisstufe 1 (Nutzer 09.11.): after a −5 MHz reduction (state.cut5_at) no clock increase at ZTU−2–ZTU−1 for 30 min
const CUT5_BLOCK_MS = 30 * 60000

/** What runs right now in the regulation (Nutzer 06.11.): command being taken over, Startpause, the clock-increase
 * locks (latest end with the reasons), the block after −5 at Basisstufe 1 (Nutzer 09.11.), the excursion over ZTO
 * and the learning period. */
function runningItems(st, lo, base) {
  const pending = st.pending
    ? (st.pending.kind === 'modus' ? 'Modus ' + String(st.pending.value).padStart(2, '0') : 'Takt ' + st.pending.value + ' MHz') +
      ' wird übernommen' + (st.pending.attempt > 1 ? ' (Versuch ' + st.pending.attempt + ')' : '')
    : null
  const startHold = st.start_hold && runs(st.start_hold.until)
    ? 'keine Takterhöhung bis ' + (lo - 5) + ' °C, spätestens ' + clockTime(st.start_hold.until) : null
  const locks = Object.keys(LOCK_REASON).filter((key) => runs(st.locks?.[key]))
  const lockEnd = locks.length ? locks.map((key) => st.locks[key]).reduce((a, b) => (Date.parse(b) > Date.parse(a) ? b : a)) : null
  const cut5End = st.cut5_at && base === 1 ? Date.parse(st.cut5_at) + CUT5_BLOCK_MS : NaN
  return [
    pending,
    startHold,
    lockEnd ? 'keine Takterhöhung bis ' + clockTime(lockEnd) + ' (' + locks.map((key) => LOCK_REASON[key]).join(', ') + ')'
      : null,
    cut5End > Date.now() ? 'keine Takterhöhung bei ' + (lo - 2) + '–' + (lo - 1) + ' °C bis ' + clockTime(cut5End) +
      ' (−5 um ' + clockTime(st.cut5_at) + ')' : null,
    st.hot?.band ? 'über ZTO: Bereich ' + st.hot.band : null,
    st.learn_since ? 'lernt seit ' + clockTime(st.learn_since) : null,
  ].filter(Boolean)
}

/** Status of the Temperatur-Automatik as cards like the overview; the small line never repeats the big value. */
function AutomationCards({ auto, st, roomTemp, lo, hi }) {
  const live = useLive()
  const sys = Object.values(live.data?.system || {}).find((d) => d && d.id === auto.device?.id) || {}
  const errorText = sys.error_rate_pct != null ? ' · Fehlerrate ' + fmt(sys.error_rate_pct, 2, ' %') : ''
  // the same hashrate card as on the overview (latest collector value of the A15)
  const deviceKey = (live.data?.devices || []).find((d) => d.id === auto.device?.id)?.device_id
  const mining = (live.data?.mining || []).find((row) => row.device_id_str === deviceKey) || {}
  // Fan speed of the A15 (Fan1, its own fan inputs), the same latest collector value as on the overview (Nutzer 06.11.)
  const fanRpm = (live.data?.hardware || []).find((row) => row.device_id_str === deviceKey)?.fan_speed_rpm
  // Wartung (Miner aus) or the A15 does not answer: the latest value is old, so "unbekannt" (Nutzer 06.11.)
  const fanRpmText = st.phase === 'wartung' || st.offline ? 'unbekannt' : fanRpm != null ? fmt(fanRpm, 0, ' rpm') : null
  const tmax = auto.now?.tmax
  const boards = (auto.now?.boards || []).filter((b) => b != null)
  const others = boards.length > 1 ? [...boards].sort((a, b) => b - a).slice(1) : []
  // colours like the clock reduction (Nutzer 06.11.): warning from ZTO+1 (Bereich 1), danger from ZTO+4 (Bereich 2)
  const tempTone = tmax == null ? 'default' : tmax >= hi + 4 ? 'danger' : tmax >= hi + 1 ? 'warning' : 'default'
  const tendency = tendencyText(st.tendency)
  // the level MinerSentinel set last; the controller confirms it only after > 5 s (read back after 10 s)
  const base = auto.level
  const running = st.fan_set ?? st.fan_read
  const confirmed = st.fan_read === st.fan_set ||
    (st.fan_read_at && st.fan_set_at && Date.parse(st.fan_read_at) >= Date.parse(st.fan_set_at) + 10000)
  // Wartung, Over Hot, miner silent: the data-service leaves these fields as they were
  const regulating = REGULATING.has(st.phase) && !st.offline && !st.over_hot
  // Kühlmodus +n / Heizmodus −n: the running level relative to the Basisstufe (Nutzer 06.11.)
  const delta = running != null && base != null ? running - base : 0
  const fanSub = [
    running === base ? '= Basisstufe' : 'Basisstufe ' + base,
    st.phase === 'kuehlmodus' ? 'Kühlmodus' + (delta > 0 ? ' +' + delta : '') : null,
    st.phase === 'heizmodus' ? 'Heizmodus' + (delta < 0 ? ' −' + -delta : '') : null,
    st.fan_target != null && st.fan_target !== st.fan_set ? 'wird Stufe ' + st.fan_target : null,
    confirmed ? null : 'Rückmeldung folgt',
  ].filter(Boolean).join(' · ')
  const phase = phases(lo, hi)[st.phase] || { value: '—', sub: '' }
  const restarting = Boolean(st.restart_req) || st.offline === 'neustart'
  const statusSub = !regulating ? phase.sub : runningItems(st, lo, base).join(' · ') || phase.sub
  return (
    <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 xl:grid-cols-5">
      <MetricCard variant="hero" layout="icon-left" icon={Hash} label="Hashrate"
        value={formatHashrate(mining.hashrate_ghs || 0)}
        subtitle={formatNumber(mining.hashrate_ghs || 0, 2) + ' GH/s' + errorText} />
      <MetricCard variant="hero" layout="icon-left" icon={Thermometer} label="Heißester Chip" tone={tempTone}
        value={fmt(tmax, 0, ' °C')}
        subtitle={[others.length ? 'weitere Boards ' + others.join(' / ') : null,
          st.t != null ? 'Regelwert ' + fmt(st.t, 0, ' °C') : null, tendency ? 'Tendenz ' + tendency : null,
          'Zieltemperatur ' + lo + '–' + hi + ' °C'].filter(Boolean).join(' · ')} />
      <MetricCard variant="hero" layout="icon-left" icon={Gauge} label="Takt laut Miner"
        value={fmt(st.clock_now, 0, ' MHz')}
        subtitle={'gesetzt ' + fmt(st.clock_set, 0, ' MHz') + (st.mode_key ? ' · ' + (MODE_LABEL[st.mode_key] || st.mode_key) : '') +
          ' · Standard ' + fmt(st.stock_mhz, 0, ' MHz')} />
      <MetricCard variant="hero" layout="icon-left" icon={Fan} label="Lüfter läuft auf"
        tone={st.phase === 'kuehlmodus' ? 'warning' : 'default'}
        value={st.phase === 'wartung' ? 'Aus' : running == null ? '—' : 'Stufe ' + running}
        subtitle={[st.phase === 'wartung' ? 'Wartung · danach wieder Stufe ' + (auto.maintenance?.fan_before ?? base)
          : fanSub, roomTemp != null ? 'Raum ' + fmt(roomTemp, 1, ' °C') : null,
          fanRpmText ? 'Fan speed ' + fanRpmText : null].filter(Boolean).join(' · ')} />
      {/* Neustart (Knopf oder nicht erreichbar während der Startpause): kein Fehler, sondern ein erwarteter Zustand */}
      <MetricCard variant="hero" layout="icon-left" icon={Activity} label="Status"
        tone={CALM_PHASES.has(st.phase) && !restarting ? 'default' : 'warning'}
        value={restarting ? 'Neustart' : phase.value}
        subtitle={restarting ? 'A15 startet neu' + (st.restart_req ? ' · gesendet ' + clockTime(st.restart_req) : '')
          : statusSub} />
    </div>
  )
}

// what the automation and the user did (protocol); the noisy kinds (learning period, stability check) only on the
// Protokoll page; kinds no longer written since v2.2.7 stay so that old entries still show here
const RECENT_KINDS = ['takt', 'modus', 'luefter', 'heizmodus', 'heizmodus_ende', 'kuehlmodus', 'kuehlmodus_ende',
  'hohe_temperatur', 'rueckkehr', 'ausnahme', 'ausnahme_ende', 'lernwert',
  'start', 'neustart', 'over_hot', 'miner_offline', 'miner_online', 'firmware_takt', 'messung_start',
  'messung_ende', 'fehler', 'basisstufe', 'automatik_ein', 'automatik_aus', 'wartung_start', 'wartung_ende',
  'test_start', 'test_stufe', 'test_ziel', 'test_schritt', 'test_messung', 'test_ende', 'neustart_nutzer', 'taktsperre',
  'taktsperre_ende', 'startpause', 'startpause_ende', 'neustart_offline', 'neustart_online',
  'einstellungsseite_aus', 'einstellungsseite_an', 'modus_wartet', 'zielbereich',
  'luefterstoss', 'luefterstoss_ende', 'obergrenze', 'ertrag', 'neustart_auto', 'obergrenzen_loeschen',
  'lernwert_loeschen']
const RECENT_COUNT = 20
const RECENT_REFRESH_MS = 10000
const RECENT_LABEL = { takt: 'Takt', modus: 'Modus', luefter: 'Lüfter', start: 'Start' }
// fan entries (amber, with the fan change): the fan modes of v2.2.7 and the old fan kinds
const FAN_KINDS = new Set(['luefter', 'heizmodus', 'heizmodus_ende', 'kuehlmodus', 'kuehlmodus_ende', 'hohe_temperatur',
  'rueckkehr', 'ausnahme', 'ausnahme_ende', 'luefterstoss'])
const USER_KINDS = new Set(['basisstufe', 'automatik_ein', 'automatik_aus', 'wartung_start', 'wartung_ende',
  'zielbereich', 'obergrenzen_loeschen', 'lernwert_loeschen'])

function recentTone(e) {
  if (isError(e)) return 'text-destructive'
  if (FAN_KINDS.has(e.kind)) return 'text-amber-600'
  if (e.kind === 'lernwert') return 'text-emerald-600'
  if (USER_KINDS.has(e.kind)) return 'text-muted-foreground'
  return 'text-sky-600'
}

/** The change of one entry in a few words (clock, mode, fan level or the learned value). */
function recentChange(e) {
  const d = e.details || {}
  if (e.kind === 'lernwert') return 'Stufe ' + d.luefterstufe + ': ' + fmt(d.takt, 0, ' MHz')
  if (e.kind === 'takt' || e.kind === 'start' || e.kind === 'firmware_takt') {
    const mode = has(e.mode_new) && e.mode_old !== e.mode_new && e.kind !== 'takt' ? ' · ' + modeText(e.mode_new) : ''
    return clockChange(e) + mode
  }
  if (e.kind === 'modus') return (has(e.mode_old) ? modeText(e.mode_old) + ' → ' : '') + modeText(e.mode_new)
  if (FAN_KINDS.has(e.kind) || USER_KINDS.has(e.kind)) return fanChange(e)
  return '—'
}

function recentTime(iso) {
  const d = new Date(iso)
  const time = d.toLocaleTimeString('de-DE', { hour: '2-digit', minute: '2-digit', second: '2-digit' })
  return d.toDateString() === new Date().toDateString() ? time
    : d.toLocaleDateString('de-DE', { day: '2-digit', month: '2-digit' }) + ' ' + time
}

/** The last 20 entries of the protocol: table on larger screens, two-line rows on the phone. */
function RecentEvents() {
  const [rows, setRows] = useState(null)
  const [error, setError] = useState(null)
  useEffect(() => {
    let alive = true
    const load = () => api.get('/api/automation/log/', { params: { kind: RECENT_KINDS.join(','), page: 1 } })
      .then(({ data }) => {
        if (!alive) return
        setRows((data.results || []).slice(0, RECENT_COUNT))
        setError(null)
      })
      .catch(() => alive && setError('Protokoll nicht geladen.'))
    load()
    const timer = setInterval(load, RECENT_REFRESH_MS)
    return () => {
      alive = false
      clearInterval(timer)
    }
  }, [])
  return (
    <div className="space-y-1.5 pt-2">
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <p className="text-sm font-medium">Letzte {RECENT_COUNT} Einträge</p>
        <Link to="/protokoll" className="text-xs text-primary underline-offset-2 hover:underline">
          Alle Einträge im Protokoll
        </Link>
      </div>
      {error && <p className="text-destructive">{error}</p>}
      {rows && rows.length === 0 && <p className="text-muted-foreground">Noch keine Einträge.</p>}
      {rows && rows.length > 0 && (
        <>
          <table className="hidden w-full text-xs sm:table">
            <thead>
              <tr className="border-b border-border/60 text-left text-muted-foreground">
                <th className="py-1 pr-3 font-medium">Zeit</th>
                <th className="py-1 pr-3 font-medium">Ereignis</th>
                <th className="py-1 pr-3 font-medium">Chip</th>
                <th className="py-1 pr-3 font-medium">Änderung</th>
                <th className="py-1 font-medium">Grund</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((e) => (
                <tr key={e.id} className="border-b border-border/30 align-top last:border-0">
                  <td className="whitespace-nowrap py-1 pr-3 tabular-nums text-muted-foreground">{recentTime(e.recorded_at)}</td>
                  <td className={'whitespace-nowrap py-1 pr-3 font-medium ' + recentTone(e)}>
                    {RECENT_LABEL[e.kind] || KIND_LABEL[e.kind] || e.kind}
                  </td>
                  <td className="whitespace-nowrap py-1 pr-3 tabular-nums">{tempText(e)}</td>
                  <td className="whitespace-nowrap py-1 pr-3 tabular-nums">{recentChange(e)}</td>
                  <td className="py-1 text-muted-foreground">
                    {e.trigger || '—'}
                    {e.ok === false && e.error && <span className="block text-destructive">{e.error}</span>}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          <ul className="divide-y divide-border/40 sm:hidden">
            {rows.map((e) => (
              <li key={e.id} className="py-1.5">
                <div className="flex items-baseline justify-between gap-2">
                  <span className={'font-medium ' + recentTone(e)}>{RECENT_LABEL[e.kind] || KIND_LABEL[e.kind] || e.kind}</span>
                  <span className="tabular-nums text-muted-foreground">{recentTime(e.recorded_at)}</span>
                </div>
                <div className="tabular-nums">
                  {recentChange(e)}{has(e.chip_temp) ? ' · ' + tempText(e) : ''}
                </div>
                <div className="text-muted-foreground">{e.trigger || '—'}</div>
                {e.ok === false && e.error && <div className="text-destructive">{e.error}</div>}
              </li>
            ))}
          </ul>
        </>
      )}
    </div>
  )
}

function AutomationPanel({ auto, here, port, level, locked, onCommand, busy, roomTemp }) {
  const [confirmOff, setConfirmOff] = useState(false)
  const [confirmRestart, setConfirmRestart] = useState(false)
  const [restart, setRestart] = useState({ busy: false, msg: null })
  // Lernwerte whose deletion was sent: the button stays disabled until the value disappears (Nutzer 08.11.); kept
  // here so that switching the Automatik on or off does not reset it
  const [requested, setRequested] = useState([])
  if (!auto) return null
  const st = auto.state || {}
  const [lo, hi] = targetOf(auto)
  // A15 neu starten (Nutzer 25.10.): the existing restart of the Over-Hot banner (ascset 0,reboot,0)
  const restartA15 = async () => {
    setConfirmRestart(false)
    setRestart({ busy: true, msg: null })
    try {
      await api.post(`/api/devices/${auto.device.id}/restart/`)
      setRestart({ busy: false, msg: { tone: 'ok', text: 'Neustart gesendet, die A15 braucht einige Minuten.' } })
    } catch (err) {
      setRestart({ busy: false, msg: { tone: 'error', text: errText(err, 'Neustart fehlgeschlagen.') } })
    }
  }
  if (!auto.enabled) {
    return (
      <div className="space-y-2 rounded-md border border-border/80 p-3">
        <p className="text-sm font-medium">Temperatur-Automatik · {auto.device?.name}</p>
        <p className="text-xs text-muted-foreground">{logicText(lo, hi)}</p>
        <p className="text-xs text-muted-foreground">{startText(lo, hi)}</p>
        <Button size="sm" disabled={locked || busy} onClick={() => onCommand({ action: 'enable', port, level })}>
          {busy && <Loader2 className="mr-1.5 h-3.5 w-3.5 animate-spin" />}
          Einschalten (Port {port}, Basisstufe {level})
        </Button>
        {/* the learned values also while the Automatik is off, to look at and delete (Nutzer 08.11.) */}
        <div className="text-xs">
          <LearnedTable learned={auto.learned} others={auto.learned_zb} zb={st.zb} level={auto.level} lo={lo}
            hi={hi} onCommand={onCommand} busy={busy} requested={requested} setRequested={setRequested} />
        </div>
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
            <>
              <Button size="sm" variant="outline" className="h-9 flex-1 sm:h-8 sm:flex-none" disabled={busy}
                onClick={() => onCommand({ action: 'maintenance_start' })}>
                Wartung starten (A15 aus)
              </Button>
              {confirmRestart ? (
                <>
                  <span className="basis-full self-center sm:basis-auto">Wirklich neu starten?</span>
                  <Button size="sm" variant="destructive" className="h-9 flex-1 sm:h-8 sm:flex-none"
                    disabled={restart.busy} onClick={restartA15}>
                    Ja
                  </Button>
                  <Button size="sm" variant="secondary" className="h-9 flex-1 sm:h-8 sm:flex-none"
                    onClick={() => setConfirmRestart(false)}>
                    Nein
                  </Button>
                </>
              ) : (
                <Button size="sm" variant="outline" className="h-9 flex-1 sm:h-8 sm:flex-none" disabled={restart.busy}
                  onClick={() => setConfirmRestart(true)}>
                  {restart.busy && <Loader2 className="mr-1.5 h-3.5 w-3.5 animate-spin" />}
                  A15 neu starten
                </Button>
              )}
            </>
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
      {restart.msg && (
        <p className={restart.msg.tone === 'ok' ? 'text-emerald-600' : 'text-destructive'}>{restart.msg.text}</p>
      )}
      {!here && <p className="text-amber-600">Steuert Port {auto.port} dieses Controllers.</p>}
      {auto.maintenance_at && st.phase !== 'wartung' && (
        <p className="text-amber-600">
          Wartung angemeldet: Der Lüfter geht aus, sobald der A15 ausgeschaltet ist. Beim Einschalten geht er wieder
          auf die Stufe von vorher.
        </p>
      )}
      <AutomationCards auto={auto} st={st} roomTemp={roomTemp} lo={lo} hi={hi} />
      <LearnedTable learned={auto.learned} others={auto.learned_zb} zb={st.zb} level={auto.level} lo={lo}
        hi={hi} onCommand={onCommand} busy={busy} requested={requested} setRequested={setRequested} />
      {st.ac_error && <p className="text-amber-600">AC Infinity: {st.ac_error}</p>}
      {st.last_error && <p className="text-destructive">Fehler: {st.last_error}</p>}
      <RecentEvents />
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
  // true when the server took the command (the Lernwert delete needs to know)
  const autoCommand = async (body) => {
    setBusy('auto')
    setMsg(null)
    try {
      const { data } = await api.post('/api/fans/automation/', { dev_id: dev.dev_id, ...body })
      setAuto(data)
      return true
    } catch (err) {
      setMsg({ tone: 'error', text: errText(err, 'Automatik-Befehl fehlgeschlagen.') })
      return false
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
  // the one open question of the backup list: { file, kind: 'restore' | 'delete' }
  const [confirmBackup, setConfirmBackup] = useState(null)
  const own = backups.filter((b) => b.dev_id === dev.dev_id)

  const backup = async () => {
    setBusy('backup')
    setMsg(null)
    try {
      const { data } = await api.post('/api/fans/backup/', { dev_id: dev.dev_id })
      const ok = Object.entries(data.ports).filter(([, v]) => v.ok).map(([p]) => p)
      setMsg({ tone: 'ok', where: 'backup', text: `Sicherung ${data.file} gespeichert (Ports mit Einstellungen: ${ok.join(', ') || 'keine'}).` })
      onReload()
    } catch (err) {
      setMsg({ tone: 'error', where: 'backup', text: errText(err, 'Sicherung fehlgeschlagen.') })
    } finally {
      setBusy(null)
    }
  }

  const restore = async (file) => {
    setBusy('restore')
    setMsg(null)
    setConfirmBackup(null)
    try {
      const { data } = await api.post('/api/fans/restore/', { file })
      setMsg({
        tone: 'ok',
        where: 'backup',
        text: 'Zurückgeschrieben: ' + data.results.map((r) => `Port ${r.port}: Code ${r.code}${r.msg ? ' ' + r.msg : ''}`).join(' · '),
      })
      onReload()
    } catch (err) {
      setMsg({ tone: 'error', where: 'backup', text: errText(err, 'Zurückschreiben fehlgeschlagen.') })
    } finally {
      setBusy(null)
    }
  }

  // delete one backup (Nutzer 08.11., B): only offered with more than one, the backend keeps the last one anyway
  const removeBackup = async (file) => {
    setBusy('backup_delete')
    setMsg(null)
    setConfirmBackup(null)
    try {
      await api.post('/api/fans/backup/delete/', { file })
      setMsg({ tone: 'ok', where: 'backup', text: 'Sicherung gelöscht.' })
    } catch (err) {
      setMsg({ tone: 'error', where: 'backup', text: errText(err, 'Löschen fehlgeschlagen.') })
    } finally {
      setBusy(null)
    }
    onReload()
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

  // Lüfterseite (Nutzer 09.11.): on top the automation and the level buttons; the AC Infinity account, name with
  // An/Aus, ports and Sicherung at the bottom of the page (CSS order in the page's flex column)
  return (
    <>
      <Card className="order-2">
        <CardContent className="space-y-4 pt-6">
          <AutomationPanel auto={auto} here={autoHere} port={port} level={enableLevel} locked={locked}
            busy={busy === 'auto'} onCommand={autoCommand} roomTemp={dev.temperature_c} />

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
            {busy && busy !== 'backup' && busy !== 'restore' && busy !== 'backup_delete' && (
              <p className="text-xs text-muted-foreground">Befehl gesendet, warte 5 s und lese die Stufe neu …</p>
            )}
            <p className="text-xs text-muted-foreground">
              Zwischen zwei Schreibbefehlen liegen mindestens 1,5 s. Während der Tests kann die Handy-App abgemeldet werden.
            </p>
          </div>

          {msg && msg.where !== 'backup' && (
            <p className={'text-xs ' + (msg.tone === 'ok' ? 'text-emerald-600' : 'text-destructive')}>{msg.text}</p>
          )}

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
      <Card className="order-4">
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
                    {confirmBackup?.file === b.file ? (
                      <>
                        <span>
                          {confirmBackup.kind === 'delete' ? 'Sicherung wirklich löschen?' : 'Wirklich zurückschreiben?'}
                        </span>
                        <Button size="sm" variant="destructive" disabled={!!busy}
                          onClick={() => (confirmBackup.kind === 'delete' ? removeBackup(b.file) : restore(b.file))}>
                          Ja
                        </Button>
                        <Button size="sm" variant="secondary" disabled={!!busy} onClick={() => setConfirmBackup(null)}>
                          Nein
                        </Button>
                      </>
                    ) : (
                      <>
                        <Button size="sm" variant="secondary" disabled={!!busy}
                          onClick={() => setConfirmBackup({ file: b.file, kind: 'restore' })}>
                          Zurückschreiben
                        </Button>
                        {own.length > 1 && (
                          <Button size="sm" variant="secondary" disabled={!!busy}
                            onClick={() => setConfirmBackup({ file: b.file, kind: 'delete' })}>
                            Löschen
                          </Button>
                        )}
                      </>
                    )}
                  </li>
                ))}
              </ul>
            )}
          </div>
          {msg && msg.where === 'backup' && (
            <p className={'text-xs ' + (msg.tone === 'ok' ? 'text-emerald-600' : 'text-destructive')}>{msg.text}</p>
          )}
        </CardContent>
      </Card>
    </>
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
    <div className="flex flex-col gap-4">
      <div className="order-1 flex items-center gap-2">
        <Button size="sm" variant="secondary" disabled={loading} onClick={load}>
          {loading ? <Loader2 className="mr-1.5 h-3.5 w-3.5 animate-spin" /> : <RefreshCw className="mr-1.5 h-3.5 w-3.5" />}
          Neu laden
        </Button>
        {error && <span className="text-xs text-destructive">{error}</span>}
      </div>
      {data?.automation && (
        <div className="order-1">
          <TargetRangeCard automation={data.automation} onReload={load} />
        </div>
      )}
      {data?.configured && data.session?.test && (
        <div className="order-1">
          <TestSession session={data.session} onChanged={load} />
        </div>
      )}
      <div className="order-3 flex flex-col gap-4 empty:hidden">
        {data && <Credentials configured={!!data.configured} onSaved={load} />}
        {data?.configured && data.devices.length === 0 && !(data.session?.test && !data.session.active) && (
          <p className="text-sm text-muted-foreground">Keine Controller im Konto gefunden.</p>
        )}
        {data?.session && !data.session.test && data.session.paused_until && (
          <LiveTakeover until={data.session.paused_until} onDone={load} />
        )}
      </div>
      {data?.devices.map((dev) => (
        <Controller key={dev.dev_id} dev={dev} backups={data.backups || []} automation={data.automation}
          onReload={load} />
      ))}
    </div>
  )
}
