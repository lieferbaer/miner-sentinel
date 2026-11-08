/**
 * Zieltemperatur der Temperatur-Automatik (Nutzer 27.10., Variante D „Skala antippen“; bis v2.2.6 „Zielbereich“):
 * erst ein Ende, dann das andere Ende antippen, „Übernehmen“ speichert; der data-service übernimmt die
 * Zieltemperatur (ZTU–ZTO) in seiner nächsten Runde. Erlaubt 77–87 °C, 4 bis 5 Grad (Nutzer 06.11.).
 */
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import api from '@/lib/api'
import { useLive } from '@/lib/LiveContext'
import { Loader2, Target } from 'lucide-react'
import { useEffect, useState } from 'react'

const MIN = 77
const MAX = 87
const WIDTH_MIN = 4   // Grad, beide Grenzen mitgezählt: 81–84 = 4 Grad, 81–85 = 5 Grad (Nutzer 06.11.)
const WIDTH_MAX = 5
const DEGREES = Array.from({ length: MAX - MIN + 1 }, (_, i) => MIN + i)

const width = (lo, hi) => hi - lo + 1

/** The stored Zieltemperatur [ZTU, ZTO] of the automation (fixed 79–84 °C before 27.10.). */
export function targetOf(auto) {
  const lo = auto?.target_low
  const hi = auto?.target_high
  return Number.isInteger(lo) && Number.isInteger(hi) ? [lo, hi] : [79, 84]
}

// second tap: the range between both taps; wider than 5 Grad: cut to 5 from the first tap; narrower than 4 Grad:
// extended to 4 upwards, shifted down at the top edge (Nutzer 06.11.)
function pick(first, t) {
  let lo = Math.min(first, t)
  let hi = Math.max(first, t)
  if (width(lo, hi) > WIDTH_MAX) {
    lo = first <= t ? first : first - WIDTH_MAX + 1
    hi = lo + WIDTH_MAX - 1
    return [lo, hi, 'Höchstens ' + WIDTH_MAX + ' Grad – gekürzt auf ' + lo + '–' + hi + ' °C.']
  }
  if (width(lo, hi) < WIDTH_MIN) {
    hi = Math.min(lo + WIDTH_MIN - 1, MAX)
    lo = hi - WIDTH_MIN + 1
    return [lo, hi, 'Mindestens ' + WIDTH_MIN + ' Grad – ergänzt auf ' + lo + '–' + hi + ' °C.']
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
  // the data-service has not yet taken over the stored Zieltemperatur (state.zb)
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
      setError(err?.response?.data?.error || 'Zieltemperatur nicht gespeichert.')
    } finally {
      setBusy(false)
    }
  }

  return (
    <Card>
      <CardHeader className="pb-2">
        <CardTitle className="flex items-center gap-2 text-base font-semibold">
          <Target className="h-4 w-4 text-muted-foreground" />
          Zieltemperatur
        </CardTitle>
        <CardDescription className="text-xs">
          Bereich, in dem der Takt den heißesten Chip der A15 hält (ZTU bis ZTO). Erst ein Ende, dann das andere Ende
          antippen. Erlaubt 77–87 °C, 4 bis 5 Grad (beide Grenzen mitgezählt).
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
        {/* 11 tiles in one row on all widths (Nutzer 06.11.) */}
        <div className="grid grid-cols-11 gap-1">
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
