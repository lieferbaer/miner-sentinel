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

  it('cuts a wide selection to 4 Grad from the first tap and widens one tile to 2 Grad', async () => {
    const user = userEvent.setup()
    api.post.mockResolvedValue({ data: automation() })
    render(<TargetRangeCard automation={automation()} onReload={vi.fn()} />)
    await tap(user, 80, 90)
    expect(screen.getByText('Höchstens 4 Grad – gekürzt auf 80–83 °C.')).toBeInTheDocument()
    await tap(user, 90, 78)
    expect(screen.getByText('Höchstens 4 Grad – gekürzt auf 87–90 °C.')).toBeInTheDocument()
    await tap(user, 92, 92)
    expect(screen.getByText('Mindestens 2 Grad – ergänzt auf 91–92 °C.')).toBeInTheDocument()
    await tap(user, 79, 79)
    expect(screen.getByText('Mindestens 2 Grad – ergänzt auf 79–80 °C.')).toBeInTheDocument()
    expect(screen.getAllByRole('button', { pressed: true }).map((b) => b.textContent)).toEqual(['79', '80'])
    await user.click(screen.getByRole('button', { name: 'Übernehmen' }))
    expect(api.post).toHaveBeenCalledWith('/api/fans/automation/', { action: 'target', low: 79, high: 80 })
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

  it('shows the error of the server', async () => {
    const user = userEvent.setup()
    api.post.mockRejectedValue({ response: { data: { error: 'Zielbereich 76–92 °C mit 2 bis 4 Grad wählen.' } } })
    render(<TargetRangeCard automation={automation({ enabled: false })} onReload={vi.fn()} />)
    await tap(user, 86, 88)
    await user.click(screen.getByRole('button', { name: 'Übernehmen' }))
    expect(await screen.findByText('Zielbereich 76–92 °C mit 2 bis 4 Grad wählen.')).toBeInTheDocument()
    expect(screen.getByText('82–85 °C')).toBeInTheDocument()
  })

  it('wird übernommen only while the automation is on and state.zb differs', () => {
    const { rerender } = render(<TargetRangeCard automation={automation({ state: { zb: [79, 84] } })} onReload={vi.fn()} />)
    expect(screen.getByText('wird übernommen')).toBeInTheDocument()
    rerender(<TargetRangeCard automation={automation({ enabled: false, state: { zb: [79, 84] } })} onReload={vi.fn()} />)
    expect(screen.queryByText('wird übernommen')).not.toBeInTheDocument()
  })
})
