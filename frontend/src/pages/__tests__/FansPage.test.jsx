import api from '@/lib/api'
import { render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import FansPage from '../FansPage'

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

// Temperatur-Automatik on, Zielbereich 82–85 °C, Basisstufe 3
const automation = (state) => ({
  enabled: true, dev_id: 'ac1', port: 1, level: 3, device: { id: 3, name: 'A15' },
  target_low: 82, target_high: 85, now: { tmax: 86, boards: [86] }, learned: {},
  state: { phase: 'normal', t: 86, fan_target: 4, fan_set: 4, fan_read: 4, zb: [82, 85], ...state },
})

function mockApi(auto) {
  api.get.mockImplementation((url) => {
    if (url === '/api/fans/status/') {
      return Promise.resolve({
        data: {
          configured: true, session: { test: false }, backups: [], automation: auto,
          devices: [{ dev_id: 'ac1', name: 'Controller', online: true, ports: [{ port: 1, speak: 4 }] }],
        },
      })
    }
    if (url === '/api/fans/credentials/') return Promise.resolve({ data: { email: '', password_set: true } })
    return Promise.resolve({ data: { results: [] } })
  })
}

const renderPage = () => render(<MemoryRouter><FansPage /></MemoryRouter>)

describe('FansPage Lüfterstoß', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('shows the pulse as status: first the level for 6 s, then back to the base level', async () => {
    mockApi(automation({ stoss: { n: 1, step: 'an', at: '2026-10-27T10:00:00Z', level: 4 } }))
    const { unmount } = renderPage()
    expect(await screen.findByText('Lüfterstoß 1/2')).toBeInTheDocument()
    expect(screen.getByText('Lüfter Stufe 4 für 6 s')).toBeInTheDocument()
    expect(screen.getByText('Basisstufe 3 · Lüfterstoß 1/2')).toBeInTheDocument()
    unmount()

    mockApi(automation({ fan_target: 3, stoss: { n: 2, step: 'aus', at: '2026-10-27T10:00:20Z', level: 4 } }))
    renderPage()
    expect(await screen.findByText('Lüfterstoß 2/2')).toBeInTheDocument()
    expect(screen.getByText('zurück auf Basisstufe')).toBeInTheDocument()
  })

  it('no pulse status without regulation (Takt-Messung)', async () => {
    mockApi(automation({ phase: 'messung', stoss: { n: 1, step: 'an', at: '2026-10-27T10:00:00Z', level: 4 } }))
    renderPage()
    expect(await screen.findByText('Takt-Messung')).toBeInTheDocument()
    expect(screen.queryByText(/Lüfterstoß/)).not.toBeInTheDocument()
  })
})
