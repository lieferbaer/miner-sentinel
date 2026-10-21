import { ThemeProvider } from '@/components/theme-provider'
import api from '@/lib/api'
import { AuthProvider } from '@/lib/AuthContext'
import { LiveProvider } from '@/lib/LiveContext'
import { TimeRangeProvider } from '@/lib/TimeRangeContext'
import { render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import OverviewDashboard from '../OverviewDashboard'

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

const recent = () => new Date(Date.now() - 5000).toISOString()

const mockDevices = [
  {
    id: 3,
    device_id: 'A15',
    device_name: 'Avalon A15',
    make: 'avalon',
    model: 'A15',
    ip_address: '192.168.178.149',
    is_active: true,
    last_seen_at: recent(),
  },
]

function mockApi({ devicesFail = false, devices = mockDevices } = {}) {
  api.get.mockImplementation((url) => {
    if (url.includes('/api/auth/user/')) {
      return Promise.resolve({ data: { authenticated: true, user: { id: 1, username: 'test' } } })
    }
    if (url === '/api/live/') {
      // the shared live poll of the UI (LiveContext)
      return devicesFail
        ? Promise.reject(new Error('Network error'))
        : Promise.resolve({
            data: {
              devices,
              mining: [{ device_id_str: 'A15', hashrate_ghs: 200000 }],
              hardware: [{ device_id_str: 'A15', power_watts: 3500, temperature_chip_c: 81 }],
              system: { A15: { id: 3, name: 'Avalon A15', work_mode: 1, chip_temp_boards: [80, 81, 79] } },
              throttle: {},
              automation: null,
              calibration: { device_id: null, run: null },
            },
          })
    }
    if (url.includes('/api/devices/system-latest/')) {
      return Promise.resolve({
        data: { A15: { id: 3, name: 'Avalon A15', work_mode: 1, chip_temp_boards: [80, 81, 79] } },
      })
    }
    if (url === '/api/devices/') {
      return devicesFail ? Promise.reject(new Error('Network error')) : Promise.resolve({ data: devices })
    }
    if (url.includes('/api/mining/latest/')) {
      return Promise.resolve({ data: [{ device_id_str: 'A15', hashrate_ghs: 200000 }] })
    }
    if (url.includes('/api/hardware/latest/')) {
      return Promise.resolve({ data: [{ device_id_str: 'A15', power_watts: 3500, temperature_chip_c: 81 }] })
    }
    if (url.includes('/api/activity/')) {
      return Promise.resolve({ data: { results: [], open_count: 0, critical_open_count: 0 } })
    }
    if (url.includes('/api/settings/collector/status/')) {
      return Promise.resolve({ data: { reachable: true, status: 'healthy' } })
    }
    return Promise.resolve({ data: [] })
  })
}

function renderOverview() {
  localStorage.setItem('sessionToken', 'test-token-123')
  return render(
    <MemoryRouter initialEntries={['/']}>
      <ThemeProvider defaultTheme="dark" storageKey="test-theme">
        <AuthProvider>
          <TimeRangeProvider>
            <LiveProvider>
              <OverviewDashboard />
            </LiveProvider>
          </TimeRangeProvider>
        </AuthProvider>
      </ThemeProvider>
    </MemoryRouter>,
  )
}

describe('OverviewDashboard (per-device overview)', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    localStorage.clear()
  })

  afterEach(() => {
    localStorage.clear()
  })

  it('renders a section per device with live values', async () => {
    mockApi()
    renderOverview()
    expect(await screen.findByText('Avalon A15')).toBeInTheDocument()
    expect(screen.queryAllByText(/Total hashrate/i).length).toBeGreaterThan(0)
    expect(screen.getByText('High Performance')).toBeInTheDocument()
    expect(screen.getByText('80 / 81 / 79 °C')).toBeInTheDocument()
  })

  it('shows the empty state without devices', async () => {
    mockApi({ devices: [] })
    renderOverview()
    expect(await screen.findByText(/No devices yet/i)).toBeInTheDocument()
  })

  it('shows the error state when the device list fails', async () => {
    mockApi({ devicesFail: true })
    renderOverview()
    expect(await screen.findByText(/Unable to load overview/i)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /retry/i })).toBeInTheDocument()
  })
})
