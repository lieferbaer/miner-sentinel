import api from '@/lib/api'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import TargetRangeCard from '../TargetRangeCard'

vi.mock('@/lib/api', () => ({
  default: {
    get: vi.fn(),
    post: vi.fn(),
    interceptors: {
      request: { use: vi.fn() },
      response: { use: vi.fn() },
    },
  },
}))

const automation = (over = {}) => ({
  enabled: true, target_low: 82, target_high: 85, state: { phase: 'normal', zb: [82, 85] }, ...over,
})

const tap = (user, ...degrees) => degrees.reduce(
  (p, t) => p.then(() => user.click(screen.getByRole('button', { name: String(t) }))), Promise.resolve())

describe('TargetRangeCard', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('shows the stored range, Übernehmen is disabled while unchanged', async () => {
    const user = userEvent.setup()
    render(<TargetRangeCard automation={automation()} onReload={vi.fn()} />)
    expect(screen.getByText('82–85 °C')).toBeInTheDocument()
    expect(screen.getByText('· 4 Grad')).toBeInTheDocument()
    expect(screen.queryByText('wird übernommen')).not.toBeInTheDocument()
    expect(screen.getAllByRole('button', { pressed: true }).map((b) => b.textContent)).toEqual(['82', '83', '84', '85'])
    const save = screen.getByRole('button', { name: 'Übernehmen' })
    expect(save).toBeDisabled()
    // the same range chosen again stays unchanged
    await tap(user, 82, 85)
    expect(save).toBeDisabled()
    // first tap only: waits for the other end
    await tap(user, 80)
    expect(screen.getByText('Jetzt das andere Ende antippen.')).toBeInTheDocument()
    expect(save).toBeDisabled()
  })

  it('shows title, the allowed 77–87 °C and 11 tiles in one row (Nutzer 06.11.)', () => {
    render(<TargetRangeCard automation={automation()} onReload={vi.fn()} />)
    expect(screen.getByText('Zieltemperatur')).toBeInTheDocument()
    expect(screen.getByText(/Erlaubt 77–87 °C, 4 bis 5 Grad \(beide Grenzen mitgezählt\)\./)).toBeInTheDocument()
    expect(screen.queryByText(/Zielbereich/)).not.toBeInTheDocument()
    const tiles = screen.getAllByRole('button').filter((b) => /^\d+$/.test(b.textContent))
    expect(tiles.map((b) => Number(b.textContent))).toEqual([77, 78, 79, 80, 81, 82, 83, 84, 85, 86, 87])
    expect(tiles[0].parentElement).toHaveClass('grid-cols-11')
    expect(tiles[0].parentElement.className).not.toMatch(/sm:grid-cols/)
  })

  it('cuts a wide selection to 5 Grad from the first tap', async () => {
    const user = userEvent.setup()
    render(<TargetRangeCard automation={automation()} onReload={vi.fn()} />)
    await tap(user, 77, 87)
    expect(screen.getByText('Höchstens 5 Grad – gekürzt auf 77–81 °C.')).toBeInTheDocument()
    await tap(user, 87, 80)
    expect(screen.getByText('Höchstens 5 Grad – gekürzt auf 83–87 °C.')).toBeInTheDocument()
    expect(screen.getAllByRole('button', { pressed: true }).map((b) => b.textContent)).toEqual(['83', '84', '85', '86', '87'])
    // 5 and 4 Grad stay as chosen (both edges counted: 81–85 = 5 Grad, 81–84 = 4 Grad)
    await tap(user, 81, 85)
    expect(screen.queryByText(/Höchstens|Mindestens/)).not.toBeInTheDocument()
    expect(screen.getAllByRole('button', { pressed: true }).map((b) => b.textContent)).toEqual(['81', '82', '83', '84', '85'])
    await tap(user, 84, 81)
    expect(screen.queryByText(/Höchstens|Mindestens/)).not.toBeInTheDocument()
    expect(screen.getAllByRole('button', { pressed: true }).map((b) => b.textContent)).toEqual(['81', '82', '83', '84'])
  })

  it('extends a narrow selection to 4 Grad upwards, shifted down at the top edge', async () => {
    const user = userEvent.setup()
    api.post.mockResolvedValue({ data: automation() })
    render(<TargetRangeCard automation={automation()} onReload={vi.fn()} />)
    await tap(user, 80, 82)
    expect(screen.getByText('Mindestens 4 Grad – ergänzt auf 80–83 °C.')).toBeInTheDocument()
    await tap(user, 79, 79)
    expect(screen.getByText('Mindestens 4 Grad – ergänzt auf 79–82 °C.')).toBeInTheDocument()
    await tap(user, 86, 87)
    expect(screen.getByText('Mindestens 4 Grad – ergänzt auf 84–87 °C.')).toBeInTheDocument()
    await tap(user, 85, 85)
    expect(screen.getByText('Mindestens 4 Grad – ergänzt auf 84–87 °C.')).toBeInTheDocument()
    await tap(user, 77, 77)
    expect(screen.getByText('Mindestens 4 Grad – ergänzt auf 77–80 °C.')).toBeInTheDocument()
    expect(screen.getAllByRole('button', { pressed: true }).map((b) => b.textContent)).toEqual(['77', '78', '79', '80'])
    await user.click(screen.getByRole('button', { name: 'Übernehmen' }))
    expect(api.post).toHaveBeenCalledWith('/api/fans/automation/', { action: 'target', low: 77, high: 80 })
  })

  it('posts the chosen range, reloads and shows the new range', async () => {
    const user = userEvent.setup()
    const onReload = vi.fn()
    api.post.mockResolvedValue({ data: automation({ target_low: 81, target_high: 84 }) })
    render(<TargetRangeCard automation={automation()} onReload={onReload} />)
    await tap(user, 84, 81)
    const save = screen.getByRole('button', { name: 'Übernehmen' })
    expect(save).toBeEnabled()
    await user.click(save)
    expect(api.post).toHaveBeenCalledWith('/api/fans/automation/', { action: 'target', low: 81, high: 84 })
    await waitFor(() => expect(onReload).toHaveBeenCalled())
    expect(screen.getByText('81–84 °C')).toBeInTheDocument()
    // the data-service still regulates 82–85 °C
    expect(screen.getByText('wird übernommen')).toBeInTheDocument()
    expect(save).toBeDisabled()
  })

  it('without a server answer: Zieltemperatur nicht gespeichert', async () => {
    const user = userEvent.setup()
    api.post.mockRejectedValue(new Error('Network Error'))
    render(<TargetRangeCard automation={automation()} onReload={vi.fn()} />)
    await tap(user, 80, 83)
    await user.click(screen.getByRole('button', { name: 'Übernehmen' }))
    expect(await screen.findByText('Zieltemperatur nicht gespeichert.')).toBeInTheDocument()
  })

  it('shows the error of the server', async () => {
    const user = userEvent.setup()
    api.post.mockRejectedValue({ response: { data: { error: 'Zieltemperatur 77–87 °C mit 4 bis 5 Grad wählen.' } } })
    render(<TargetRangeCard automation={automation({ enabled: false })} onReload={vi.fn()} />)
    await tap(user, 83, 86)
    await user.click(screen.getByRole('button', { name: 'Übernehmen' }))
    expect(await screen.findByText('Zieltemperatur 77–87 °C mit 4 bis 5 Grad wählen.')).toBeInTheDocument()
    expect(screen.getByText('82–85 °C')).toBeInTheDocument()
  })

  it('wird übernommen only while the automation is on and state.zb differs', () => {
    const { rerender } = render(<TargetRangeCard automation={automation({ state: { zb: [79, 84] } })} onReload={vi.fn()} />)
    expect(screen.getByText('wird übernommen')).toBeInTheDocument()
    rerender(<TargetRangeCard automation={automation({ enabled: false, state: { zb: [79, 84] } })} onReload={vi.fn()} />)
    expect(screen.queryByText('wird übernommen')).not.toBeInTheDocument()
  })
})
