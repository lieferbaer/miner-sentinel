/**
 * Zielbereich der Temperatur-Automatik (Nutzer 27.10., Variante D „Skala antippen“): erst ein Ende, dann das andere
 * Ende antippen, „Übernehmen“ speichert; der data-service übernimmt den Bereich in seiner nächsten Runde.
 */
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import api from '@/lib/api'
import { useLive } from '@/lib/LiveContext'
import { Loader2, Target } from 'lucide-react'
import { useEffect, useState } from 'react'

const MIN = 76
const MAX = 92
const WIDTH_MIN = 2   // Grad, beide Grenzen mitgezählt: 82–83 = 2 Grad, 82–85 = 4 Grad
const WIDTH_MAX = 4
const DEGREES = Array.from({ length: MAX - MIN + 1 }, (_, i) => MIN + i)

const width = (lo, hi) => hi - lo + 1

/** The stored range [L, U] of the automation (fixed 79–84 °C before 27.10.). */
export function targetOf(auto) {
  const lo = auto?.target_low
  const hi = auto?.target_high
  return Number.isInteger(lo) && Number.isInteger(hi) ? [lo, hi] : [79, 84]
}

// second tap: the range between both taps; wider than 4 Grad: cut to 4 from the first tap; one tile: 2 Grad
function pick(first, t) {
  let lo = Math.min(first, t)
  let hi = Math.max(first, t)
  if (width(lo, hi) > WIDTH_MAX) {
    ;[lo, hi] = first <= t ? [first, first + WIDTH_MAX - 1] : [first - WIDTH_MAX + 1, first]
    return [lo, hi, 'Höchstens 4 Grad – gekürzt auf ' + lo + '–' + hi + ' °C.']
  }
  if (width(lo, hi) < WIDTH_MIN) {
    ;[lo, hi] = lo < MAX ? [lo, lo + 1] : [lo - 1, lo]
    return [lo, hi, 'Mindestens 2 Grad – ergänzt auf ' + lo + '–' + hi + ' °C.']
  }
  return [lo, hi, null]
}

export default function TargetRangeCard({ automation, onReload }) {
  // same source as the automation panel: the live poll (every 5 s), until then the page data
  const live = useLive()
  const current = live.data?.automation || automation
  const [auto, setAuto] = useState(current)
  useEffect(() => setAuto(current), [current])
  const [lo, hi] = targetOf(auto)
  const [sel, setSel] = useState(null)        // chosen range; null = the stored one
  const [first, setFirst] = useState(null)    // first tap, waiting for the other end
  const [note, setNote] = useState(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState(null)
  const [selLo, selHi] = sel || [lo, hi]
  const changed = selLo !== lo || selHi !== hi
  // the data-service has not yet taken over the stored range (state.zb)
  const zb = auto?.state?.zb
  const taking = auto?.enabled && !(Array.isArray(zb) && zb[0] === lo && zb[1] === hi)

  const tap = (t) => {
    setError(null)
    if (first === null) {
      setFirst(t)
      setNote('Jetzt das andere Ende antippen.')
      return
    }
    const [a, b, text] = pick(first, t)
    setFirst(null)
    setNote(text)
    setSel([a, b])
  }

  const save = async () => {
    setBusy(true)
    setError(null)
    try {
      const { data } = await api.post('/api/fans/automation/', { action: 'target', low: selLo, high: selHi })
      setAuto(data)
      setSel(null)
      setNote(null)
      onReload()
      live.refresh()
    } catch (err) {
      setError(err?.response?.data?.error || 'Zielbereich nicht gespeichert.')
    } finally {
      setBusy(false)
    }
  }

  return (
    <Card>
      <CardHeader className="pb-2">
        <CardTitle className="flex items-center gap-2 text-base font-semibold">
          <Target className="h-4 w-4 text-muted-foreground" />
          Zielbereich
        </CardTitle>
        <CardDescription className="text-xs">
          Bereich, in dem der Takt den heißesten Chip der A15 hält. Erst ein Ende, dann das andere Ende antippen.
          Erlaubt 76–92 °C, 2 bis 4 Grad (beide Grenzen mitgezählt).
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-2 pt-0 text-xs">
        <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1">
          <p className="text-2xl font-semibold tabular-nums text-emerald-600">
            {lo + '–' + hi + ' °C'}
            <span className="ml-2 text-sm font-normal text-muted-foreground">{'· ' + width(lo, hi) + ' Grad'}</span>
          </p>
          {taking && <span className="text-amber-600">wird übernommen</span>}
        </div>
        <div className="grid grid-cols-9 gap-1 sm:grid-cols-[repeat(17,minmax(0,1fr))]">
          {DEGREES.map((t) => {
            const inside = first === null && t >= selLo && t <= selHi
            return (
              <button key={t} type="button" disabled={busy} aria-pressed={t === first || inside}
                onClick={() => tap(t)}
                className={'h-9 rounded text-xs font-medium tabular-nums transition-colors disabled:opacity-50 ' +
                  (t === first ? 'bg-emerald-600 text-white ring-2 ring-emerald-300'
                    : inside ? 'bg-emerald-500 text-white' : 'bg-muted hover:bg-muted/70')}>
                {t}
              </button>
            )
          })}
        </div>
        {note && <p className="text-muted-foreground">{note}</p>}
        <div className="flex flex-wrap items-center gap-2">
          <Button size="sm" className="h-9 w-full sm:h-8 sm:w-auto" disabled={!changed || first !== null || busy}
            onClick={save}>
            {busy && <Loader2 className="mr-1.5 h-3.5 w-3.5 animate-spin" />}
            Übernehmen
          </Button>
          {error && <span className="text-destructive">{error}</span>}
        </div>
      </CardContent>
    </Card>
  )
}
