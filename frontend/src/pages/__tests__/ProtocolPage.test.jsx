import api from '@/lib/api'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import ProtocolPage from '../ProtocolPage'

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

const clockEntry = {
  id: 2,
  recorded_at: '2026-10-24T19:00:05Z',
  kind: 'takt',
  source: 'regelung',
  trigger: 'Chiptemperatur 86 °C (ab 86 °C): Takt −20 MHz',
  phase: 'korrektur',
  chip_temp: 86,
  tendency: 1,
  base_old: 3,
  base_new: 3,
  fan_old: 4,
  fan_new: 4,
  clock_old: 240,
  clock_new: 220,
  clock_reported: 241,
  mode_old: '2/L4',
  mode_new: '2/L4',
  target: 'miner-takt',
  endpoint: 'cgminer 192.168.178.149:4028 ascset frequency',
  request: 'ascset 0,frequency,212:220:224:228-0-0',
  response_short: 'STATUS=S,When=1,Code=119',
  status: 'set OK',
  ok: true,
  error: '',
  attempt: 1,
  api_ms: 120,
  takeover_s: 12.5,
  takeover_state: 'gemessen',
  temp_before: 86,
  thermal: [85, 84],
  details: { schritt: -20, lernperiode_neu_gestartet: true },
}

const fanError = {
  id: 1,
  recorded_at: '2026-10-24T19:00:00Z',
  kind: 'luefter',
  source: 'regelung',
  trigger: 'Chiptemperatur 86 °C: Basisstufe 3 + 1',
  phase: 'korrektur',
  chip_temp: 86,
  base_old: 3,
  base_new: 3,
  fan_old: 3,
  fan_new: 4,
  ok: false,
  error: 'AC Infinity: Zeitüberschreitung',
  attempt: 2,
  api_ms: 5000,
  takeover_state: 'nicht gemessen',
  thermal: [],
  details: {},
}

function mockApi() {
  api.get.mockImplementation((url) => {
    if (url === '/api/automation/log/') {
      return Promise.resolve({ data: { count: 2, page: 1, pages: 1, results: [clockEntry, fanError] } })
    }
    if (url === '/api/automation/log/2/') {
      return Promise.resolve({ data: { ...clockEntry, response: 'STATUS=S,When=1,Code=119,Msg=ASC 0 set OK|' } })
    }
    if (url === '/api/automation/log/stats/') {
      return Promise.resolve({
        data: {
          modes: [{ von: '2/L4', nach: '2/L3', anzahl: 2, api_ms_mittel: 800, api_ms_median: 790,
            uebernahme_s_mittel: 40, uebernahme_s_median: 40, uebernahme_min: 35, uebernahme_max: 45 }],
          clocks: [{ takt: 220, anzahl: 3, api_ms_mittel: 110, api_ms_median: 100, uebernahme_s_mittel: 12.5,
            uebernahme_s_median: 12, thermisch_1min: -1.5, thermisch_3min: -2, thermisch_5min: null }],
          fans: [],
        },
      })
    }
    if (url === '/api/automation/cycles/') {
      return Promise.resolve({
        data: {
          bucket_s: 5,
          points: [
            { t: '2026-10-24T19:00:00Z', chip_temp: 86, clock_set: 240, clock_reported: 241, fan_level: 4, base_level: 3,
              phase: 'korrektur' },
            { t: '2026-10-24T19:00:05Z', chip_temp: 85, clock_set: 220, clock_reported: 241, fan_level: 4, base_level: 3,
              phase: 'korrektur', decision: 'Takt 220 MHz gesendet' },
          ],
          events: [{ t: '2026-10-24T19:00:05Z', kind: 'takt', id: 2, trigger: 'Takt −20 MHz' }],
        },
      })
    }
    return Promise.reject(new Error('unexpected ' + url))
  })
}

describe('ProtocolPage', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    mockApi()
  })

  it('lists the events in plain German and shows errors', async () => {
    render(<ProtocolPage />)
    // table (desktop) and cards (phone) are both in the DOM in tests
    expect((await screen.findAllByText('Taktbefehl')).length).toBeGreaterThan(0)
    expect(screen.getAllByText('Lüfterbefehl').length).toBeGreaterThan(0)
    expect(screen.getAllByText('AC Infinity: Zeitüberschreitung').length).toBeGreaterThan(0)
    expect(screen.getAllByText('240 → 220 MHz').length).toBeGreaterThan(0)
    expect(screen.getAllByText('Lernperiode neu gestartet').length).toBeGreaterThan(0)
    // command and shortened reply in the list
    expect(screen.getAllByText('ascset 0,frequency,212:220:224:228-0-0').length).toBeGreaterThan(0)
    expect(screen.getAllByText('STATUS=S,When=1,Code=119').length).toBeGreaterThan(0)
    expect(screen.getByText(/2 Einträge · Seite 1 von 1/)).toBeInTheDocument()
    const params = api.get.mock.calls.find(([url]) => url === '/api/automation/log/')[1].params
    expect(params.page).toBe(1)
    expect(params.from).toBeTruthy()
  })

  it('filters by result', async () => {
    const user = userEvent.setup()
    render(<ProtocolPage />)
    await screen.findAllByText('Taktbefehl')
    await user.selectOptions(screen.getByRole('combobox', { name: 'Ergebnis' }), 'false')
    await waitFor(() => {
      const calls = api.get.mock.calls.filter(([url]) => url === '/api/automation/log/')
      expect(calls[calls.length - 1][1].params.ok).toBe('false')
    })
  })

  it('filters pure events (no command)', async () => {
    const user = userEvent.setup()
    render(<ProtocolPage />)
    await screen.findAllByText('Taktbefehl')
    await user.selectOptions(screen.getByRole('combobox', { name: 'Ergebnis' }), 'null')
    await waitFor(() => {
      const calls = api.get.mock.calls.filter(([url]) => url === '/api/automation/log/')
      expect(calls[calls.length - 1][1].params.ok).toBe('null')
    })
  })

  it('filters by several kinds', async () => {
    const user = userEvent.setup()
    render(<ProtocolPage />)
    await screen.findAllByText('Taktbefehl')
    await user.click(screen.getByText(/^Art:/))
    await user.click(screen.getByRole('checkbox', { name: 'Taktbefehl' }))
    await user.click(screen.getByRole('checkbox', { name: 'Lüfterbefehl' }))
    await waitFor(() => {
      const calls = api.get.mock.calls.filter(([url]) => url === '/api/automation/log/')
      expect(calls[calls.length - 1][1].params.kind).toBe('takt,luefter')
    })
  })

  it('downloads the cycles as CSV by a plain link (streamed to disk, not into the page)', async () => {
    const user = userEvent.setup()
    const click = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(function () {
      click.lastHref = this.href
    })
    render(<ProtocolPage />)
    await user.selectOptions(screen.getByRole('combobox', { name: 'Tabelle' }), 'cycle')
    await user.click(screen.getByRole('button', { name: /CSV herunterladen/ }))
    expect(click).toHaveBeenCalled()
    const url = new URL(click.lastHref, 'http://localhost')
    expect(url.pathname).toBe('/api/automation/export.csv')
    expect(url.searchParams.get('table')).toBe('cycle')
    expect(url.searchParams.get('from')).toBeTruthy()
    expect(api.get.mock.calls.some(([u]) => u === '/api/automation/export.csv')).toBe(false)
    click.mockRestore()
  })

  it('opens the detail view with the full answer', async () => {
    const user = userEvent.setup()
    render(<ProtocolPage />)
    // a click on the row (here: its clock cell) opens the entry
    const [cell] = await screen.findAllByText('240 → 220 MHz')
    await user.click(cell)
    expect(await screen.findByText('STATUS=S,When=1,Code=119,Msg=ASC 0 set OK|')).toBeInTheDocument()
    expect(screen.getByText(/Thermischer Verlauf .*nach der Übernahme/)).toBeInTheDocument()
    expect(api.get).toHaveBeenCalledWith('/api/automation/log/2/')
    // details in plain German
    expect(screen.getByText('Schritt')).toBeInTheDocument()
    expect(screen.getByText('−20 MHz')).toBeInTheDocument()
  })

  it('shows the reaction times', async () => {
    const user = userEvent.setup()
    render(<ProtocolPage />)
    await user.click(screen.getByRole('tab', { name: 'Reaktionszeiten' }))
    expect(await screen.findByText('mode 04 → mode 03')).toBeInTheDocument()
    expect(screen.getByText('220 MHz')).toBeInTheDocument()
    expect(screen.getByText('−1,5 °C')).toBeInTheDocument()
  })

  it('shows the history charts', async () => {
    const user = userEvent.setup()
    render(<ProtocolPage />)
    await user.click(screen.getByRole('tab', { name: 'Verlauf' }))
    expect(await screen.findByText('Takt (MHz)')).toBeInTheDocument()
    expect(screen.getByText('Lüfterstufe')).toBeInTheDocument()
    expect(screen.getByText('Korrekturphase')).toBeInTheDocument()
  })

  it('loads no history over more than 7 days', async () => {
    const user = userEvent.setup()
    render(<ProtocolPage />)
    await user.click(screen.getByRole('tab', { name: 'Verlauf' }))
    await screen.findByText('Takt (MHz)')
    const panel = screen.getByRole('tabpanel')
    await user.click(within(panel).getByRole('button', { name: 'frei' }))
    const before = api.get.mock.calls.filter(([url]) => url === '/api/automation/cycles/').length
    fireEvent.change(within(panel).getByLabelText('von'), { target: { value: '2026-09-01T00:00' } })
    fireEvent.change(within(panel).getByLabelText('bis'), { target: { value: '2026-09-20T00:00' } })
    expect(await screen.findByText(/Höchstens 7 Tage/)).toBeInTheDocument()
    expect(screen.queryByText('Takt (MHz)')).not.toBeInTheDocument()
    const calls = api.get.mock.calls.filter(([url]) => url === '/api/automation/cycles/')
    // the change of 'von' alone (open end: up to now) is already too long, no further request
    expect(calls.length).toBe(before)
  })
})
