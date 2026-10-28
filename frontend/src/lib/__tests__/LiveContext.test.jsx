import api from '@/lib/api'
import { act, render } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { LIVE_REFRESH_MS, LIVE_TIMEOUT_MS, LiveProvider } from '../LiveContext'

vi.mock('@/lib/api', () => ({
  default: { get: vi.fn() },
}))

describe('LiveProvider', () => {
  beforeEach(() => {
    vi.useFakeTimers()
    vi.clearAllMocks()
  })
  afterEach(() => {
    vi.useRealTimers()
  })

  it('asks with a time limit and keeps polling after a request without an answer', async () => {
    // Nutzer 28.10.: after a server update the page showed "0 / 3 online" until it was reloaded
    api.get.mockRejectedValueOnce(new Error('timeout of 15000ms exceeded')).mockResolvedValue({ data: { devices: [] } })
    render(<LiveProvider><div /></LiveProvider>)
    expect(api.get).toHaveBeenCalledWith('/api/live/', { timeout: LIVE_TIMEOUT_MS })
    await act(async () => {
      await vi.advanceTimersByTimeAsync(LIVE_REFRESH_MS)
    })
    expect(api.get).toHaveBeenCalledTimes(2)
  })
})
