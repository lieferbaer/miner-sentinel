/**
 * Throttle — lower an Avalon miner's hashrate (and noise) to a target TH/s.
 * MinerSentinel sets the PLL clock, then regulates with the measured 15-min
 * hashrate. Reboots and work-mode changes on the miner lift the throttle;
 * it is not re-applied automatically.
 * A15: no throttle since 24.10. (the Temperatur-Automatik sets the clock) - only the clock status and the mode
 * buttons.
 */
import MetricCard from '@/components/metrics/MetricCard'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Input } from '@/components/ui/input'
import api from '@/lib/api'
import { useLive } from '@/lib/LiveContext'
import { Gauge, Loader2 } from 'lucide-react'
import { useEffect, useState } from 'react'
const EVENT_VISIBLE_MS = 30 * 60 * 1000

function fmt(value, digits = 1) {
  if (value === null || value === undefined || Number.isNaN(value)) return '—'
  return Number(value).toLocaleString('de-DE', { minimumFractionDigits: digits, maximumFractionDigits: digits })
}

function clock(iso) {
  if (!iso) return ''
  const d = new Date(iso)
  return d.toLocaleString('de-DE', { day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit' })
}

const STATUS_TEXT = {
  adjusting: 'stellt ein …',
  regulating: 'stellt ein …',
  reached: 'Ziel erreicht',
  settled: 'Ziel erreicht',
  limit: 'Grenze erreicht',
}

function current(active, state) {
  return active.estimate_ths ?? state.hashrate_ths
}

/** The card shows what the throttle controller last read from the miner (every 15 s). */
function staleText(state) {
  return state.stale ? 'Keine aktuellen Daten vom Miner (Stand ' + clock(state.seen_at) + ')' : null
}

function liftedEvent(state) {
  const ev = state?.last_event
  if (!ev || ev.type === 'reset' || !ev.at) return null
  if (Date.now() - new Date(ev.at).getTime() > EVENT_VISIBLE_MS) return null
  return ev
}

function maxText(state) {
  return state.max_ths ? fmt(state.max_ths) + ' TH/s (gemessen)' : 'noch nicht gemessen'
}

/** A15: clock status and mode buttons only. */
function isA15(state) {
  return state.family === 'a15'
}

/** What the badge / hero card say: relative to the current mode, based on the real clock. */
function throttleView(state) {
  const active = state.active
  if (isA15(state)) {
    // the miner's value instead of "ungedrosselt"; set value and stock clock beside it
    const set = state.set_mhz != null
    return {
      tone: set ? 'warn' : 'ok',
      badge: fmt(state.current_avg_mhz, 0) + ' MHz laut Miner' +
        (set ? ' · gesetzt ' + fmt(state.set_mhz, 0) + ' MHz (' + state.set_by + ')' : ''),
      value: fmt(state.current_avg_mhz, 0) + ' MHz',
      sub: 'laut Miner · gesetzt ' + (set ? fmt(state.set_mhz, 0) + ' MHz (' + state.set_by + ')' : '—') +
        ' · Standard ' + fmt(state.stock_avg_mhz, 0) + ' MHz',
    }
  }
  if (active) {
    const pct = state.max_ths ? Math.round((active.target_ths / state.max_ths) * 100) : null
    return {
      tone: 'warn',
      badge:
        'gedrosselt · ' + fmt(active.target_ths) + ' von ' + fmt(state.max_ths) + ' TH/s' +
        (pct ? ' (' + pct + ' %)' : '') + ' · ' + state.mode_label,
      value: fmt(active.target_ths) + ' TH/s',
      sub: (pct ? pct + ' % von ' : '') + state.mode_label + ' · aktuell ≈ ' + fmt(current(active, state)) + ' TH/s · ' +
        (STATUS_TEXT[active.status] || active.status),
    }
  }
  return {
    tone: 'ok',
    badge: 'ungedrosselt · ' + state.mode_label,
    value: 'Ungedrosselt',
    sub: state.mode_label + (isA15(state) ? ' · Standardtakt ' + fmt(state.max_mhz, 0) + ' MHz' : ' · max. ' + maxText(state)),
  }
}

/** Shared throttle state for one device (status card + control card), from the live poll of the UI. */
export function useThrottle(deviceId, enabled) {
  const live = useLive()
  const fromLive = enabled ? live.data?.throttle?.[deviceId] || null : null
  const [state, setState] = useState(fromLive)
  // every live answer replaces a value set locally after a command
  useEffect(() => setState(fromLive), [fromLive])
  const loadError = live.error ? 'Status nicht aktualisiert' : null
  return { state, setState, loadError, reload: live.refresh }
}

/** Hero card: throttled or not, relative to the current mode. */
export function ThrottleStatusCard({ throttle }) {
  const state = throttle?.state
  if (!state || !state.supported) return null
  const view = throttleView(state)
  const lifted = !state.active && !isA15(state) && liftedEvent(state)
  return (
    <MetricCard
      variant="hero"
      layout="icon-left"
      icon={Gauge}
      label={isA15(state) ? 'Takt' : 'Drosselung'}
      value={view.value}
      subtitle={staleText(state) || (lifted ? 'Aufgehoben ' + clock(lifted.at) + ': ' + lifted.detail : view.sub)}
      tone={view.tone === 'warn' || lifted || state.stale ? 'warning' : undefined}
    />
  )
}

/** Known modes in the same order as the mode buttons; one row each. */
function ModesTable({ state }) {
  const all = state.modes || []
  const best = (label) => all.find((m) => m.label === label) || null
  const labels = state.mode_options ? state.mode_options.map((o) => o.label) : [...new Set(all.map((m) => m.label))]
  if (!labels.length) return null
  const status = (m) => (m?.max_ths && m?.power_w ? 'gemessen' : m?.max_ths ? 'teilweise gemessen' : 'nicht gemessen')
  return (
    <div className="overflow-x-auto pt-1">
      <table className="w-full text-xs">
        <thead className="text-muted-foreground">
          <tr className="text-left">
            <th className="py-1 pr-2 font-medium">Modus</th>
            <th className="py-1 pr-2 font-medium">max. TH/s</th>
            <th className="py-1 pr-2 font-medium">Leistung</th>
            <th className="py-1 pr-2 font-medium">J/TH</th>
            <th className="py-1 font-medium">Status</th>
          </tr>
        </thead>
        <tbody>
          {labels.map((label) => {
            const m = best(label)
            const isCur = label === state.mode_label
            return (
              <tr key={label} className={isCur ? 'font-semibold' : ''}>
                <td className="py-0.5 pr-2">{label}{isCur ? ' (aktiv)' : ''}</td>
                <td className="py-0.5 pr-2">{m?.max_ths ? fmt(m.max_ths) : '—'}</td>
                <td className="py-0.5 pr-2">{m?.max_ths && m?.power_w ? fmt(m.power_w, 0) + ' W' : '—'}</td>
                <td className="py-0.5 pr-2">{m?.max_ths && m?.efficiency_j_th ? fmt(m.efficiency_j_th) : '—'}</td>
                <td className="py-0.5 text-muted-foreground">{status(m)}</td>
              </tr>
            )
          })}
        </tbody>
      </table>
    </div>
  )
}

/** Work-mode buttons (Nano 3S, Avalon Q, A15 power level). */
function ModeSwitch({ deviceId, state, ip, onChanged }) {
  const [busy, setBusy] = useState(false)
  const [msg, setMsg] = useState(null)
  if (!state.mode_options) return null
  const change = async (opt) => {
    setBusy(true)
    setMsg(null)
    try {
      await api.post(`/api/devices/${deviceId}/workmode/`, { mode: opt.value })
      setMsg({ tone: 'ok', text: 'Modus „' + opt.label + '“ gesetzt. Der Miner stellt in einigen Sekunden um.' })
      setTimeout(onChanged, 20000)
    } catch (error) {
      setMsg({ tone: 'error', text: error?.response?.data?.error || 'Umschalten fehlgeschlagen.' })
    } finally {
      setBusy(false)
    }
  }
  return (
    <div className="space-y-1">
      <div className="flex flex-wrap items-center gap-2">
        <span className="text-xs text-muted-foreground">Modus:</span>
        {state.mode_options.map((opt) => {
          const current = opt.value === state.current_workmode
          return (
            <Button
              key={opt.value}
              type="button"
              size="sm"
              variant={current ? 'default' : 'secondary'}
              disabled={busy || current || state.calibration_active}
              onClick={() => change(opt)}
            >
              {opt.label}
              {current ? ' (aktiv)' : ''}
            </Button>
          )
        })}
      </div>
      {msg && <p className={'text-xs ' + (msg.tone === 'ok' ? 'text-emerald-600' : 'text-destructive')}>{msg.text}</p>}
    </div>
  )
}

/** Control card: enter a target TH/s or lift the throttle (A15: status and mode buttons only). */
export default function ThrottleCard({ deviceId, throttle, ip }) {
  const { state, setState, loadError, reload } = throttle
  const [target, setTarget] = useState('')
  const [busy, setBusy] = useState(false)
  const [message, setMessage] = useState(null)

  const apply = async (body) => {
    setBusy(true)
    setMessage(null)
    try {
      const { data } = await api.post(`/api/devices/${deviceId}/throttle/`, body)
      setState(data)
      setTarget('')
      setMessage({
        tone: 'ok',
        text: body.reset
          ? 'Drosselung aufgehoben.'
          : 'Ziel ' + fmt(body.target_ths) + ' TH/s gesetzt. Der Miner ist in 1–3 Minuten dort.',
      })
    } catch (error) {
      setMessage({ tone: 'error', text: error?.response?.data?.error || 'Setzen fehlgeschlagen.' })
    } finally {
      setBusy(false)
    }
  }

  if (!state || !state.supported) return null

  const active = state.active
  const a15 = isA15(state)
  const lifted = !a15 && liftedEvent(state)
  const view = throttleView(state)
  const min = state.min_ths
  const max = state.max_ths
  const raw = String(target).trim()
  const targetNumber = parseFloat(raw.replace(',', '.'))
  const inRange = Number.isFinite(targetNumber) && min != null && max != null && targetNumber >= min && targetNumber <= max
  const hint = raw && !inRange ? 'Bitte einen Wert zwischen ' + fmt(min) + ' und ' + fmt(max) + ' TH/s eingeben.' : null

  return (
    <Card className="col-span-2">
      <CardHeader className="pb-2">
        <CardTitle className="flex flex-wrap items-center gap-2 text-base font-semibold">
          <Gauge className="h-4 w-4 text-muted-foreground" />
          {a15 ? 'Takt und Modus' : 'Drosselung einstellen'}
          <span
            className={
              'rounded px-1.5 py-0.5 text-xs font-medium ' +
              (view.tone === 'warn' ? 'bg-amber-500/15 text-amber-600' : 'bg-emerald-500/15 text-emerald-600')
            }
          >
            {view.badge}
          </span>
        </CardTitle>
        {!a15 && (
          <CardDescription className="text-xs">
            Modus {state.mode_label} · max. {maxText(state)} · Takt {fmt(state.current_avg_mhz, 0)} von{' '}
            {fmt(state.stock_avg_mhz, 0)} MHz
            {active ? ' · aktuell ≈ ' + fmt(current(active, state)) + ' TH/s · ' + (STATUS_TEXT[active.status] || active.status) : ''}
          </CardDescription>
        )}
      </CardHeader>
      <CardContent className="space-y-2 pt-0">
        {state.stale && <p className="text-xs text-destructive">{staleText(state)}</p>}
        {!a15 && state.calibration_active && !state.calibration_paused && (
          <p className="text-xs text-amber-600">Takt-Messung läuft – Drosseln ist gesperrt (Menü „Takt-Tabellen“).</p>
        )}
        {lifted && !active && (
          <p className="text-xs text-amber-600">
            Drosselung auf{' '}
            {lifted.clock_mhz != null ? fmt(lifted.clock_mhz, 0) + ' MHz' : fmt(lifted.target_ths) + ' TH/s'} aufgehoben
            am {clock(lifted.at)}: {lifted.detail}.
          </p>
        )}
        {!a15 && (
          <form
            className="flex flex-wrap items-center gap-2"
            onSubmit={(e) => {
              e.preventDefault()
              if (inRange && !busy) apply({ target_ths: targetNumber })
            }}
          >
            <Input
              className="h-9 w-28"
              inputMode="decimal"
              placeholder={max ? fmt(min) + '–' + fmt(max) : '—'}
              value={target}
              onChange={(e) => setTarget(e.target.value)}
            />
            <span className="text-sm text-muted-foreground">TH/s</span>
            <Button type="submit" size="sm"
              disabled={busy || !inRange || !max || (state.calibration_active && !state.calibration_paused)}>
              {busy ? <Loader2 className="h-4 w-4 animate-spin" /> : 'Setzen'}
            </Button>
            <Button
              type="button"
              size="sm"
              variant="secondary"
              disabled={busy || !active}
              onClick={() => apply({ reset: true })}
            >
              Drosselung aufheben
            </Button>
          </form>
        )}
        {hint && <p className="text-xs text-amber-600">{hint}</p>}
        {!a15 && !active && !max && (
          <p className="text-xs text-amber-600">
            Dieser Modus ist noch nicht gemessen. Drosseln ist möglich, sobald das Gerät 15 Minuten ungedrosselt in
            diesem Modus gelaufen ist.
          </p>
        )}
        {active && active.status === 'reached' && (
          <p className="text-xs text-emerald-600">
            Ziel erreicht. Die Hashrate-Karte (5-Minuten-Durchschnitt) und f2pool ziehen in einigen Minuten nach.
          </p>
        )}
        {message && (
          <p className={'text-xs ' + (message.tone === 'ok' ? 'text-emerald-600' : 'text-destructive')}>
            {message.text}
          </p>
        )}
        {loadError && <p className="text-xs text-destructive">Status nicht aktualisiert: {loadError}</p>}
        <ModeSwitch
          deviceId={deviceId}
          state={state}
          ip={ip}
          onChanged={reload}
        />
        <ModesTable state={state} />
        {!a15 && (
          <p className="text-xs text-muted-foreground">
            {max ? 'Erlaubt ' + fmt(min) + '–' + fmt(max) + ' TH/s (20–100 % des gemessenen Maximums im aktuellen Modus). ' : ''}
            Neustart oder Moduswechsel heben eine Drosselung auf.
          </p>
        )}
      </CardContent>
    </Card>
  )
}
