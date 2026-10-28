import api from '@/lib/api'
import { render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
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

  it('no status for a hot pulse of an old state (Lüfterstöße oben entfallen, Nutzer 28.10.)', async () => {
    mockApi(automation({ stoss: { n: 1, step: 'an', at: '2026-10-27T10:00:00Z', level: 4 } }))
    renderPage()
    expect(await screen.findByText('Normalbetrieb')).toBeInTheDocument()
    expect(screen.queryByText(/Lüfterstoß/)).not.toBeInTheDocument()
  })

  it('no pulse status without regulation (Takt-Messung)', async () => {
    mockApi(automation({ phase: 'messung', stoss: { n: 1, step: 'an', at: '2026-10-28T10:00:00Z', level: 2, dir: 'down' } }))
    renderPage()
    expect(await screen.findByText('Takt-Messung')).toBeInTheDocument()
    expect(screen.queryByText(/Lüfterstoß/)).not.toBeInTheDocument()
  })

  it('logic text: fan +1 at once, clock only if still above U after 60 s, from U+4 at once', async () => {
    mockApi({ ...automation({ phase: 'aus' }), enabled: false })
    renderPage()
    expect(await screen.findByText(new RegExp('Bei 86–88 °C Lüfter \\+1 sofort, den Takt erst, wenn die Temperatur ' +
      'nach 60 s noch über 85 °C liegt \\(vorher zurück: keine Senkung\\); ab 89 °C sofort nach Tabelle\\.'))).toBeInTheDocument()
    expect(screen.queryByText(/Basisstufe \+1 für 6 s/)).not.toBeInTheDocument()
  })
})

describe('FansPage Lüfterstöße unten', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('shows the cold pulse as status (Basisstufe −1 for 6 s, then back), not as reduced after an Ausnahme', async () => {
    mockApi(automation({ t: 81, fan_target: 2, fan_set: 2, fan_read: 2,
      stoss: { n: 1, step: 'an', at: '2026-10-28T10:00:00Z', level: 2, dir: 'down' } }))
    const { unmount } = renderPage()
    expect(await screen.findByText('Lüfterstoß 1/2')).toBeInTheDocument()
    expect(screen.getByText('Lüfter Stufe 2 für 6 s')).toBeInTheDocument()
    expect(screen.getByText('Basisstufe 3 · Lüfterstoß 1/2')).toBeInTheDocument()
    expect(screen.queryByText(/reduziert nach Ausnahme/)).not.toBeInTheDocument()
    unmount()

    mockApi(automation({ t: 81, fan_target: 3, fan_set: 3, fan_read: 3,
      stoss: { n: 2, step: 'aus', at: '2026-10-28T10:00:20Z', level: 3, dir: 'down' } }))
    renderPage()
    expect(await screen.findByText('Lüfterstoß 2/2')).toBeInTheDocument()
    expect(screen.getByText('zurück auf Basisstufe')).toBeInTheDocument()
  })
})

describe('FansPage Obergrenzen', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('shows the Obergrenze per level, also for a level without a learned value', async () => {
    const learned = { 3: { mhz: 320, temp_avg: 83.2, at: '2026-10-28T09:00:00Z', count: 2 } }
    mockApi({ ...automation({ caps: { 5: 395, 3: 325 } }), learned })
    renderPage()
    const table = (await screen.findByRole('columnheader', { name: 'Obergrenze' })).closest('table')
    const rows = within(table).getAllByRole('row').slice(1).map((r) => within(r).getAllByRole('cell').map((c) => c.textContent))
    expect(rows).toEqual([
      ['3', '82–85 °C', '320 MHz', '83,2 °C', '—', '—', '2', expect.any(String), '325 MHz'],
      ['5', '—', '—', '—', '—', '—', '—', '—', '395 MHz'],
    ])
  })

  it('also with the automation off: table with the Obergrenze and the button', async () => {
    const user = userEvent.setup()
    const auto = { ...automation({ phase: 'aus', caps: { 3: 325 } }), enabled: false }
    mockApi(auto)
    api.post.mockResolvedValue({ data: auto })
    renderPage()
    const table = (await screen.findByRole('columnheader', { name: 'Obergrenze' })).closest('table')
    const rows = within(table).getAllByRole('row').slice(1).map((r) => within(r).getAllByRole('cell').map((c) => c.textContent))
    expect(rows).toEqual([['3', '—', '—', '—', '—', '—', '—', '—', '325 MHz']])
    await user.click(screen.getByRole('button', { name: 'Obergrenzen löschen' }))
    await user.click(screen.getByRole('button', { name: 'Ja' }))
    expect(api.post).toHaveBeenCalledWith('/api/fans/automation/', expect.objectContaining({ action: 'caps_clear' }))
  })

  it('automation off without Obergrenzen: no table and no button', async () => {
    const learned = { 3: { mhz: 320, temp_avg: 83.2, at: '2026-10-28T09:00:00Z', count: 2 } }
    mockApi({ ...automation({ phase: 'aus', caps: {} }), enabled: false, learned })
    renderPage()
    expect(await screen.findByRole('button', { name: /Einschalten/ })).toBeInTheDocument()
    expect(screen.queryByRole('columnheader', { name: 'Gesetzter Takt (Ø)' })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Obergrenzen löschen' })).not.toBeInTheDocument()
  })
})

describe('FansPage Lernwerte', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('Bereich right after Stufe (range of the learned values = state.zb), Takt as Gesetzter Takt (Ø)', async () => {
    const learned = { 2: { mhz: 275, temp_avg: 82, ths_avg: 133.4, j_th: 16.9, at: '2026-10-28T12:39:51Z', count: 13 } }
    // Zielbereich 83–86 gewählt, der data-service hat noch 82–85: die Lernwerte gehören zu 82–85
    mockApi({ ...automation({ zb: [82, 85] }), target_low: 83, target_high: 86, learned })
    renderPage()
    const table = (await screen.findByRole('columnheader', { name: 'Bereich' })).closest('table')
    const heads = within(table).getAllByRole('columnheader').map((c) => c.textContent)
    expect(heads).toEqual(['Stufe', 'Bereich', 'Gesetzter Takt (Ø)', 'Ø Temperatur', 'Ø Hashrate', 'Effizienz', 'Anzahl',
      'Zeit'])
    const cells = within(within(table).getAllByRole('row')[1]).getAllByRole('cell').map((c) => c.textContent)
    expect(cells.slice(0, 7)).toEqual(['2', '82–85 °C', '275 MHz', '82,0 °C', '133,4 TH/s', '16,9 J/TH', '13'])
  })

  it('mobile: one entry per level instead of the wide table', async () => {
    const learned = { 2: { mhz: 260, temp_avg: 81.9, ths_avg: 131.6, j_th: 17.1, at: '2026-10-28T14:37:57Z', count: 21 } }
    mockApi({ ...automation({ zb: [81, 84], caps: { 2: 275, 4: 380 } }), learned })
    renderPage()
    const first = (await screen.findByText(/^Stufe 2 · 81–84 °C$/)).closest('li')
    expect(first.closest('ul')).toHaveClass('sm:hidden')
    // label above value, two per line
    const pairs = (li) => [...li.querySelectorAll('.grid > div')].map((d) => [...d.children].map((c) => c.textContent))
    expect(pairs(first)).toEqual([['Gesetzter Takt (Ø)', '260 MHz'], ['Ø Temperatur', '81,9 °C'],
      ['Ø Hashrate', '131,6 TH/s'], ['Effizienz', '17,1 J/TH'], ['Anzahl', '21'], ['Obergrenze', '275 MHz']])
    const second = within(first.closest('ul')).getByText(/^Stufe 4$/).closest('li')
    expect(pairs(second)).toEqual([['Obergrenze', '380 MHz']])
  })

  it('Bereich unknown without state.zb', async () => {
    const learned = { 3: { mhz: 320, temp_avg: 83.2, at: '2026-10-28T09:00:00Z', count: 2 } }
    mockApi({ ...automation({ zb: null }), learned })
    renderPage()
    const table = (await screen.findByRole('columnheader', { name: 'Bereich' })).closest('table')
    const cells = within(within(table).getAllByRole('row')[1]).getAllByRole('cell').map((c) => c.textContent)
    expect(cells.slice(0, 3)).toEqual(['3', '—', '320 MHz'])
  })

  it('no column and no button without Obergrenzen', async () => {
    mockApi(automation({ caps: {} }))
    renderPage()
    expect(await screen.findByText(/Lernwerte: noch keine/)).toBeInTheDocument()
    expect(screen.queryByText('Obergrenze')).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Obergrenzen löschen' })).not.toBeInTheDocument()
  })

  it('Obergrenzen löschen asks first, then posts caps_clear to the automation', async () => {
    const user = userEvent.setup()
    const auto = automation({ caps: { 3: 325 } })
    mockApi(auto)
    api.post.mockResolvedValue({ data: auto })
    renderPage()
    await user.click(await screen.findByRole('button', { name: 'Obergrenzen löschen' }))
    expect(screen.getByText('Obergrenzen wirklich löschen?')).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Nein' }))
    expect(api.post).not.toHaveBeenCalled()
    await user.click(screen.getByRole('button', { name: 'Obergrenzen löschen' }))
    await user.click(screen.getByRole('button', { name: 'Ja' }))
    expect(api.post).toHaveBeenCalledTimes(1)
    expect(api.post).toHaveBeenCalledWith('/api/fans/automation/', expect.objectContaining({ action: 'caps_clear' }))
    expect(await screen.findByRole('button', { name: 'Obergrenzen löschen' })).toBeInTheDocument()
  })
})
