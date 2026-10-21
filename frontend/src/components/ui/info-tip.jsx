/**
 * Small "i" sign with an explanation balloon: shown after DELAY_MS of mouse-over (or at once on tap/focus).
 */
import { Info } from 'lucide-react'
import { useEffect, useRef, useState } from 'react'

const DELAY_MS = 1000

export default function InfoTip({ text, className = '' }) {
  const [open, setOpen] = useState(false)
  const timer = useRef(null)
  const stop = () => {
    clearTimeout(timer.current)
    setOpen(false)
  }
  useEffect(() => () => clearTimeout(timer.current), [])
  return (
    <span
      className={'relative inline-flex align-middle ' + className}
      onMouseEnter={() => {
        clearTimeout(timer.current)
        timer.current = setTimeout(() => setOpen(true), DELAY_MS)
      }}
      onMouseLeave={stop}
    >
      <button
        type="button"
        aria-label="Erklärung"
        className="inline-flex rounded-full text-muted-foreground hover:text-foreground focus:outline-none"
        onClick={(e) => {
          e.stopPropagation()
          setOpen((v) => !v)
        }}
        onBlur={stop}
      >
        <Info className="h-3.5 w-3.5" />
      </button>
      {open && (
        <span
          role="tooltip"
          className="absolute bottom-full left-1/2 z-50 mb-2 w-64 -translate-x-1/2 rounded-md border bg-popover p-2 text-xs font-normal leading-snug text-popover-foreground shadow-md"
        >
          {text}
        </span>
      )}
    </span>
  )
}
