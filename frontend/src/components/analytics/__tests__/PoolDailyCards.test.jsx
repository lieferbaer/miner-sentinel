import api from '@/lib/api'
import { render, screen, waitFor } from '@testing-library/react'
import { cloneElement } from 'react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import PoolDailyCards from '../PoolDailyCards'

// jsdom has no layout: give the chart a fixed size so the axes are drawn
vi.mock('recharts', async (importOriginal) => {
  const original = await importOriginal()
  return {
    ...original,
    ResponsiveContainer: ({ children }) => cloneElement(children, { width: 800, height: 240 }),
  }
})

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

const TITLE = 'Balance und Pool-Hashrate je Tag (160 Tage)'

const days = (fill) =>
  Array.from({ length: 160 }, (_, i) => {
    const d = new Date(Date.UTC(2026, 5, 2 + i))
    return { day: d.toISOString().slice(0, 10), ...fill(i) }
  })

const payload = (accounts) => ({ days: 160, start: '2026-06-02', end: '2026-11-08', accounts })

describe('PoolDailyCards', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('loads the fixed 160-day endpoint (no time range) and renders one card per account', async () => {
    api.get.mockResolvedValue({
      data: payload([
        {
          pool_type: 'f2pool',
          account: 'onr',
          series: days((i) => ({
            balance_btc: i > 150 ? 0.001 * (i - 150) : null,
            hashrate_ghs: i > 150 ? 200000 : null,
          })),
        },
      ]),
    })
    render(<PoolDailyCards />)
    expect(await screen.findByText(TITLE)).toBeInTheDocument()
    expect(api.get).toHaveBeenCalledWith('/api/analytics/pool-daily/')
    expect(screen.getByText(/onr · f2pool/)).toBeInTheDocument()
    expect(screen.queryByText('Keine Daten in den letzten 160 Tagen')).not.toBeInTheDocument()
    // the x axis always spans the full 160 days, ticks dd.MM.
    const ticks = Array.from(document.querySelectorAll('.recharts-xAxis .recharts-cartesian-axis-tick-value'))
      .map((node) => node.textContent)
    expect(ticks[0]).toBe('02.06.')
    expect(ticks[ticks.length - 1]).toBe('08.11.')
    expect(document.querySelectorAll('.recharts-line').length).toBe(1)
  })

  it('shows the empty text for an account without values', async () => {
    api.get.mockResolvedValue({
      data: payload([
        { pool_type: 'f2pool', account: 'onr', series: days(() => ({ balance_btc: null, hashrate_ghs: null })) },
      ]),
    })
    render(<PoolDailyCards />)
    expect(await screen.findByText('Keine Daten in den letzten 160 Tagen')).toBeInTheDocument()
    expect(screen.getByText(TITLE)).toBeInTheDocument()
  })

  it('shows one card with the empty text when there is no pool account', async () => {
    api.get.mockResolvedValue({ data: payload([]) })
    render(<PoolDailyCards />)
    expect(await screen.findByText('Keine Daten in den letzten 160 Tagen')).toBeInTheDocument()
    expect(screen.getAllByText(TITLE)).toHaveLength(1)
  })

  it('refreshes every 5 minutes on its own', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    try {
      api.get.mockResolvedValue({ data: payload([]) })
      render(<PoolDailyCards />)
      await waitFor(() => expect(api.get).toHaveBeenCalledTimes(1))
      await vi.advanceTimersByTimeAsync(5 * 60 * 1000)
      await waitFor(() => expect(api.get).toHaveBeenCalledTimes(2))
    } finally {
      vi.useRealTimers()
    }
  })
})
