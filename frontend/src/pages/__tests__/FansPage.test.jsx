import api from '@/lib/api'
import { LiveProvider } from '@/lib/LiveContext'
import { render, screen, within } from '@testing-library/react'
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

// Temperatur-Automatik on, Zieltemperatur 82–85 °C, Basisstufe 3
const automation = (state) => ({
  enabled: true, dev_id: 'ac1', port: 1, level: 3, device: { id: 3, name: 'A15' },
  target_low: 82, target_high: 85, now: { tmax: 86, boards: [86] }, learned: {},
  state: { phase: 'basismodus', t: 86, fan_target: 4, fan_set: 4, fan_read: 4, zb: [82, 85], ...state },
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

// the metric card of a label and its tone (colour of the icon)
const card = (label) => screen.getByText(label).closest('.rounded-xl')
const toneOf = (label) => {
  const icon = card(label).querySelector('[class*="text-status-"], .text-primary')
  return icon.className.includes('text-status-critical-fg') ? 'danger'
    : icon.className.includes('text-status-warning-fg') ? 'warning' : 'default'
}
const inMin = (min) => new Date(Date.now() + min * 60000).toISOString()
const hhmm = (iso) => new Date(iso).toLocaleTimeString('de-DE', { hour: '2-digit', minute: '2-digit' })

describe('FansPage fan modes (v2.2.7, Nutzer 06.11.)', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('Basismodus: status, fan on the Basisstufe, nothing running', async () => {
    mockApi(automation({ t: 84, fan_target: 3, fan_set: 3, fan_read: 3 }))
    renderPage()
    expect(await screen.findByText('Basismodus')).toBeInTheDocument()
    expect(screen.getByText('Takt regelt 82–85 °C')).toBeInTheDocument()
    expect(screen.getByText('= Basisstufe')).toBeInTheDocument()
    expect(toneOf('Status')).toBe('default')
    expect(toneOf('Lüfter läuft auf')).toBe('default')
  })

  it('Kühlmodus: fan relative to the Basisstufe, warning, ends at ZTO−4', async () => {
    mockApi(automation({ phase: 'kuehlmodus', t: 88, cool: { since: inMin(-1), plus: 1 } }))
    const { unmount } = renderPage()
    expect(await screen.findByText('Kühlmodus')).toBeInTheDocument()
    expect(screen.getByText('Basisstufe 3 · Kühlmodus +1')).toBeInTheDocument()
    expect(screen.getByText('endet bei 81 °C oder kälter')).toBeInTheDocument()
    expect(toneOf('Status')).toBe('warning')
    expect(toneOf('Lüfter läuft auf')).toBe('warning')
    unmount()

    // +2, but the controller does not yet report the new level
    mockApi(automation({ phase: 'kuehlmodus', t: 93, fan_target: 5, fan_set: 5, fan_read: 4,
      fan_set_at: inMin(0), fan_read_at: inMin(0), cool: { since: inMin(-2), plus: 2 } }))
    renderPage()
    expect(await screen.findByText('Basisstufe 3 · Kühlmodus +2 · Rückmeldung folgt')).toBeInTheDocument()
  })

  it('Kühlmodus at level 10 (Basisstufe 10): no plus shown', async () => {
    mockApi({ ...automation({ phase: 'kuehlmodus', fan_target: 10, fan_set: 10, fan_read: 10,
      cool: { since: inMin(-1), plus: 1 } }), level: 10 })
    renderPage()
    expect(await screen.findByText('= Basisstufe · Kühlmodus')).toBeInTheDocument()
  })

  it('Heizmodus: fan below the Basisstufe, calm tone, ends from ZTU−4', async () => {
    mockApi(automation({ phase: 'heizmodus', t: 68, fan_target: 1, fan_set: 1, fan_read: 1,
      heat: { since: inMin(-3) } }))
    const { unmount } = renderPage()
    expect(await screen.findByText('Heizmodus')).toBeInTheDocument()
    expect(screen.getByText('Basisstufe 3 · Heizmodus −2')).toBeInTheDocument()
    expect(screen.getByText('endet ab 78 °C')).toBeInTheDocument()
    expect(toneOf('Status')).toBe('default')
    expect(toneOf('Lüfter läuft auf')).toBe('default')
    unmount()

    // the next step is on its way to the controller
    mockApi(automation({ phase: 'heizmodus', t: 68, fan_target: 1, fan_set: 2, fan_read: 2,
      heat: { since: inMin(-3) } }))
    renderPage()
    expect(await screen.findByText('Basisstufe 3 · Heizmodus −1 · wird Stufe 1')).toBeInTheDocument()
  })

  it('Heizmodus at Basisstufe 1: no minus shown', async () => {
    mockApi({ ...automation({ phase: 'heizmodus', t: 60, fan_target: 1, fan_set: 1, fan_read: 1,
      heat: { since: inMin(-3) } }), level: 1 })
    renderPage()
    expect(await screen.findByText('= Basisstufe · Heizmodus')).toBeInTheDocument()
  })

  it('old phases and states are not shown any more (Korrekturphase, Ausnahme, Takt-Messung, Lüfterstoß)', async () => {
    for (const phase of ['normal', 'korrektur', 'ausnahme', 'messung']) {
      mockApi(automation({ phase, heat: null, stoss: { n: 1, step: 'an', at: inMin(0), level: 2, dir: 'down' },
        window_until: inMin(1), calm: { until: inMin(5) } }))
      const view = renderPage()
      expect(await screen.findByText('Status')).toBeInTheDocument()
      expect(within(card('Status')).getByText('—')).toBeInTheDocument()
      for (const old of [/Normalbetrieb/, /Korrektur/, /Ausnahme/, /Takt-Messung/, /Lüfterstoß/, /Beobachtungsfenster/,
        /Taktsperre/]) {
        expect(screen.queryByText(old)).not.toBeInTheDocument()
      }
      view.unmount()
    }
  })
})

describe('FansPage status: what runs (Nutzer 06.11.)', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('lists command, Startpause, the latest lock with its reasons, the excursion over ZTO and the learning', async () => {
    const hold = inMin(8)
    const learn = inMin(-4)
    // ZTU 80: Startpause until 75 °C (ZTU−5); erhoehung ends before senkung, kuehlmodus has already ended
    mockApi({ ...automation({
      pending: { kind: 'takt', value: 300, attempt: 1 }, start_hold: { since: inMin(-1), until: hold },
      locks: { erhoehung: inMin(2), heizmodus: null, kuehlmodus: inMin(-1), senkung: inMin(4) },
      hot: { band: 2, since: inMin(-1), band_at: inMin(0), next_cut_at: inMin(2), dwell_s: 30, minus1: 15 },
      learn_since: learn, zb: [80, 85],
    }), target_low: 80, target_high: 85 })
    renderPage()
    expect(await screen.findByText('Takt 300 MHz wird übernommen · keine Takterhöhung bis 75 °C, spätestens ' +
      hhmm(hold) + ' · keine Takterhöhung bis ' + hhmm(inMin(4)) + ' (nach Erhöhung, nach Senkung) · ' +
      'über ZTO: Bereich 2 · lernt seit ' + hhmm(learn))).toBeInTheDocument()
  })

  it('one lock: its end and its reason', async () => {
    for (const [key, reason] of [['erhoehung', 'nach Erhöhung'], ['heizmodus', 'nach Heizmodus'],
      ['kuehlmodus', 'nach Kühlmodus'], ['senkung', 'nach Senkung']]) {
      const until = inMin(3)
      mockApi(automation({ locks: { [key]: until } }))
      const view = renderPage()
      expect(await screen.findByText('keine Takterhöhung bis ' + hhmm(until) + ' (' + reason + ')')).toBeInTheDocument()
      view.unmount()
    }
  })

  it('expired or ended locks and an expired Startpause are not shown', async () => {
    mockApi(automation({ locks: { erhoehung: inMin(-1), heizmodus: null, senkung: inMin(-0.5) },
      start_hold: { since: inMin(-11), until: inMin(-1) }, hot: null }))
    renderPage()
    expect(await screen.findByText('Takt regelt 82–85 °C')).toBeInTheDocument()
    expect(screen.queryByText(/keine Takterhöhung/)).not.toBeInTheDocument()
    expect(screen.queryByText(/über ZTO/)).not.toBeInTheDocument()
  })

  it('miner silent or Over Hot: nothing of the regulation, only the state', async () => {
    for (const extra of [{ offline: true }, { over_hot: true }]) {
      mockApi(automation({ ...extra, locks: { senkung: inMin(4) }, hot: { band: 1 }, learn_since: inMin(-1) }))
      const view = renderPage()
      expect(await screen.findByText('Takt regelt 82–85 °C')).toBeInTheDocument()
      expect(screen.queryByText(/keine Takterhöhung|über ZTO|lernt seit/)).not.toBeInTheDocument()
      view.unmount()
    }
  })

  it('Neustart instead of the mode while the A15 restarts', async () => {
    mockApi(automation({ offline: 'neustart' }))
    renderPage()
    expect(await screen.findByText('Neustart')).toBeInTheDocument()
    expect(screen.getByText('A15 startet neu')).toBeInTheDocument()
  })
})

describe('FansPage hottest chip', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('Zieltemperatur in the subtitle; warning from ZTO+1, danger from ZTO+4', async () => {
    for (const [tmax, tone] of [[85, 'default'], [86, 'warning'], [88, 'warning'], [89, 'danger'], [97, 'danger']]) {
      mockApi({ ...automation({ t: tmax }), now: { tmax, boards: [tmax] } })
      const view = renderPage()
      expect(await screen.findByText('Regelwert ' + tmax + ' °C · Zieltemperatur 82–85 °C')).toBeInTheDocument()
      expect(toneOf('Heißester Chip')).toBe(tone)
      view.unmount()
    }
  })

  it('Takt laut Miner: the set clock of the automation', async () => {
    mockApi(automation({ clock_now: 296, clock_set: 300, mode_key: '2/L4', stock_mhz: 420 }))
    renderPage()
    expect(await screen.findByText('gesetzt 300 MHz · mode 04 · Standard 420 MHz')).toBeInTheDocument()
  })
})

describe('FansPage rule texts', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  // ZTU 80, ZTO 85: the numbers of the examples in the rules of 06.11.
  const off = () => ({ ...automation({ phase: 'aus', zb: [80, 85] }), enabled: false, target_low: 80, target_high: 85 })
  const ruleText = async () => (await screen.findByText(/^Der Takt hält den heißesten Chip/)).textContent

  it('fan modes with the current numbers', async () => {
    mockApi(off())
    renderPage()
    const text = await ruleText()
    expect(text).toContain('in der Zieltemperatur 80–85 °C (ZTU 80 °C, ZTO 85 °C); dort keine Taktänderung.')
    expect(text).toContain('Kühlmodus: ab 88 °C Basisstufe +1, ab 93 °C +2 (höchstens Stufe 10; das Plus geht bis ' +
      'zum Ende nicht zurück); endet bei 81 °C oder kälter, zurück auf die Basisstufe.')
    expect(text).toContain('Heizmodus: bei 69 °C oder kälter Lüfter −1, sobald die Lüfterstufe seit über 1 min ' +
      'unverändert ist, so jede Minute weiter (mindestens Stufe 1); bei 70–75 °C bleibt die Stufe; endet ab 76 °C, ' +
      'zurück auf die Basisstufe. Nach dem Ende von Heiz- oder Kühlmodus 3 min keine Takterhöhung.')
  })

  it('clock increase, reduction, Startpause, clock check and error rate with the current numbers', async () => {
    mockApi(off())
    renderPage()
    const text = await ruleText()
    expect(text).toContain('Unter 80 °C sofort Takt +5 MHz bei 77–79 °C, +10 bei 75–76 °C, +15 bei 70–74 °C, ' +
      '+20 bei 60–69 °C, +40 bei 59 °C oder kälter (höchstens 440 MHz); danach keine Takterhöhung für 3 min ' +
      '(+5, +10) bzw. 2 min (+15, +20, +40). Diese Sperren und die nach Heiz- oder Kühlmodus enden früher ab 84 °C.')
    expect(text).toContain('Über 85 °C Takt runter: Bereich 1 (86–88 °C) −10 / −5 MHz bei einer ' +
      'ZeitImZieltemperatur davor (ununterbrochen in 80–85 °C) bis 5 min / über 5 min, Bereich 2 ' +
      '(89–91 °C) −20 MHz, Bereich 3 (92–94 °C) −20 MHz, Bereich 4 (ab 95 °C) −50 MHz; mindestens 100 MHz.')
    expect(text).toContain('Nach jeder Senkung 5 min keine Takterhöhung.')
    expect(text).toContain('Nach Neustart, Wartung oder Over Hot keine Takterhöhung bis 75 °C, höchstens 10 min ' +
      '(Startpause).')
    expect(text).toContain('15 min ununterbrochen um mehr als 15 MHz')
    expect(text).toContain('Fehlerrate 30 min ununterbrochen über 10 %: automatischer Neustart der A15 (höchstens ' +
      'einer je 6 h).')
  })

  it('start and learning: no cap, Fehlerrate unter 5 %', async () => {
    mockApi(off())
    renderPage()
    const start = (await screen.findByText(/^Start \(Stufenwahl/)).textContent
    expect(start).toContain('Heiz- und Kühlmodus enden ohne Sperre, Lüfter auf die Basisstufe')
    expect(start).toContain('Lernwert: 15 min stabil in 80–85 °C bei unveränderter Lüfterstufe, nur bei einer ' +
      'Fehlerrate unter 5 % (Mittel der 15 min).')
  })

  it('none of the removed rules', async () => {
    mockApi(off())
    renderPage()
    await ruleText()
    for (const old of [/Obergrenze/, /Lüfterstoß/, /Lüfterstöße/, /Korrektur/, /Ausnahme/, /Ertrags/, /Takt-Messung/,
      /Zielbereich/, /60 s/, /Taktsperre/, /4 %/]) {
      expect(screen.queryByText(old)).not.toBeInTheDocument()
    }
  })
})

describe('FansPage removed features', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('no Temperatur-Test, also with an old state or request', async () => {
    mockApi({ ...automation({ test: { step: 1, steps: 2, clock: 330, phase: 'messen' } }), temp_test_at: inMin(-1) })
    renderPage()
    expect(await screen.findByText('Basismodus')).toBeInTheDocument()
    expect(screen.queryByText(/Temperatur-Test/)).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /Test abbrechen/ })).not.toBeInTheDocument()
  })

  it('no Takt-Messung notice under the level buttons', async () => {
    mockApi(automation({ phase: 'messung' }))
    renderPage()
    expect(await screen.findByText('Status')).toBeInTheDocument()
    expect(screen.queryByText(/Takt-Messung/)).not.toBeInTheDocument()
  })

  it('no Obergrenzen: neither column nor entry nor button, also with old caps in the state', async () => {
    const learned = { 3: { mhz: 320, temp_avg: 83.2, at: '2026-10-28T09:00:00Z', count: 2 } }
    mockApi({ ...automation({ caps: { 5: 395, 3: 325 } }), learned })
    renderPage()
    const table = (await screen.findByRole('columnheader', { name: 'Zieltemperatur' })).closest('table')
    const rows = within(table).getAllByRole('row').slice(1).map((r) => within(r).getAllByRole('cell').map((c) => c.textContent))
    // a level with only an old Obergrenze gets no row
    expect(rows).toEqual([['3', '82–85 °C', '320 MHz', '83,2 °C', '—', '—', '2', expect.any(String)]])
    expect(screen.queryByText(/Obergrenze/)).not.toBeInTheDocument()
    expect(screen.queryByText(/325 MHz|395 MHz/)).not.toBeInTheDocument()
  })

  it('automation off: no table of learned values and no button, also with old caps', async () => {
    const learned = { 3: { mhz: 320, temp_avg: 83.2, at: '2026-10-28T09:00:00Z', count: 2 } }
    mockApi({ ...automation({ phase: 'aus', caps: { 3: 325 } }), enabled: false, learned })
    renderPage()
    expect(await screen.findByRole('button', { name: /Einschalten/ })).toBeInTheDocument()
    expect(screen.queryByRole('columnheader', { name: 'Gesetzter Takt (Ø)' })).not.toBeInTheDocument()
    expect(screen.queryByText(/Obergrenze/)).not.toBeInTheDocument()
  })
})

describe('FansPage Letzte Einträge', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('loads the fan modes and shows Kühlmodus / Kühlmodus beendet as fan entries', async () => {
    mockApi(automation({}))
    const status = api.get.getMockImplementation()
    api.get.mockImplementation((url, config) => {
      if (url === '/api/automation/log/') {
        return Promise.resolve({
          data: {
            results: [
              { id: 2, kind: 'kuehlmodus_ende', recorded_at: '2026-11-06T10:07:00Z', chip_temp: 81, fan_old: 4, fan_new: 3,
                trigger: 'Chiptemperatur 81 °C' },
              { id: 1, kind: 'kuehlmodus', recorded_at: '2026-11-06T10:00:00Z', chip_temp: 88, fan_old: 3, fan_new: 4,
                trigger: 'Chiptemperatur 88 °C' },
            ],
          },
        })
      }
      return status(url, config)
    })
    renderPage()
    const ended = await screen.findAllByText('Kühlmodus beendet')
    expect(ended[0]).toHaveClass('text-amber-600')
    expect(screen.getAllByText('Kühlmodus').some((el) => el.classList.contains('text-amber-600'))).toBe(true)
    expect(screen.getAllByText('Stufe 3 → 4').length).toBeGreaterThan(0)
    expect(screen.getAllByText('Stufe 4 → 3').length).toBeGreaterThan(0)
    const kinds = api.get.mock.calls.find(([url]) => url === '/api/automation/log/')[1].params.kind.split(',')
    expect(kinds).toEqual(expect.arrayContaining(['heizmodus', 'heizmodus_ende', 'kuehlmodus', 'kuehlmodus_ende',
      'zielbereich']))
  })
})

describe('FansPage Lernwerte', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('Zieltemperatur right after Stufe (the one of the learned values = state.zb), Takt as Gesetzter Takt (Ø)', async () => {
    const learned = { 2: { mhz: 275, temp_avg: 82, ths_avg: 133.4, j_th: 16.9, at: '2026-10-28T12:39:51Z', count: 13 } }
    // Zieltemperatur 83–86 gewählt, der data-service hat noch 82–85: die Lernwerte gehören zu 82–85
    mockApi({ ...automation({ zb: [82, 85] }), target_low: 83, target_high: 86, learned })
    renderPage()
    const table = (await screen.findByRole('columnheader', { name: 'Zieltemperatur' })).closest('table')
    const heads = within(table).getAllByRole('columnheader').map((c) => c.textContent)
    expect(heads).toEqual(['Stufe', 'Zieltemperatur', 'Gesetzter Takt (Ø)', 'Ø Temperatur', 'Ø Hashrate', 'Effizienz',
      'Anzahl', 'Zeit'])
    const cells = within(within(table).getAllByRole('row')[1]).getAllByRole('cell').map((c) => c.textContent)
    expect(cells.slice(0, 7)).toEqual(['2', '82–85 °C', '275 MHz', '82,0 °C', '133,4 TH/s', '16,9 J/TH', '13'])
  })

  it('mobile: one entry per level instead of the wide table', async () => {
    const learned = { 2: { mhz: 260, temp_avg: 81.9, ths_avg: 131.6, j_th: 17.1, at: '2026-10-28T14:37:57Z', count: 21 } }
    mockApi({ ...automation({ zb: [81, 84] }), learned })
    renderPage()
    const first = (await screen.findByText(/^Stufe 2 · 81–84 °C$/)).closest('li')
    expect(first.closest('ul')).toHaveClass('sm:hidden')
    // label above value, two per line
    const pairs = (li) => [...li.querySelectorAll('.grid > div')].map((d) => [...d.children].map((c) => c.textContent))
    expect(pairs(first)).toEqual([['Gesetzter Takt (Ø)', '260 MHz'], ['Ø Temperatur', '81,9 °C'],
      ['Ø Hashrate', '131,6 TH/s'], ['Effizienz', '17,1 J/TH'], ['Anzahl', '21']])
    expect(within(first.closest('ul')).getAllByRole('listitem')).toHaveLength(1)
  })

  it('Zieltemperatur unknown without state.zb', async () => {
    const learned = { 3: { mhz: 320, temp_avg: 83.2, at: '2026-10-28T09:00:00Z', count: 2 } }
    mockApi({ ...automation({ zb: null }), learned })
    renderPage()
    const table = (await screen.findByRole('columnheader', { name: 'Zieltemperatur' })).closest('table')
    const cells = within(within(table).getAllByRole('row')[1]).getAllByRole('cell').map((c) => c.textContent)
    expect(cells.slice(0, 3)).toEqual(['3', '—', '320 MHz'])
  })

  it('no learned values yet: 15 min stable in the Zieltemperatur with an error rate below 5 %', async () => {
    mockApi(automation({}))
    renderPage()
    expect(await screen.findByText('Lernwerte: noch keine (nach 15 min stabil bei 82–85 °C und einer Fehlerrate unter 5 %).'))
      .toBeInTheDocument()
  })

  // the page with the shared live poll: A15 fan speed 1520 rpm (another device 4500), room 21,5 °C at the controller
  function renderWithLive(auto) {
    mockApi(auto)
    const status = api.get.getMockImplementation()
    api.get.mockImplementation((url) => {
      if (url === '/api/live/') {
        return Promise.resolve({
          data: {
            devices: [{ id: 3, device_id: 'A15' }], mining: [], system: {}, throttle: {}, automation: auto,
            hardware: [{ device_id_str: 'OTHER', fan_speed_rpm: 4500 }, { device_id_str: 'A15', fan_speed_rpm: 1520 }],
          },
        })
      }
      if (url === '/api/fans/status/') {
        return status(url).then((res) => ({ data: { ...res.data, devices: [{ ...res.data.devices[0], temperature_c: 21.5 }] } }))
      }
      return status(url)
    })
    return render(<LiveProvider><MemoryRouter><FansPage /></MemoryRouter></LiveProvider>)
  }

  it('Fan speed of the A15 (live poll) after the room temperature in the fan card (Nutzer 06.11.)', async () => {
    renderWithLive(automation({}))
    expect(await screen.findByText('Basisstufe 3 · Raum 21,5 °C · Fan speed 1.520 rpm')).toBeInTheDocument()
  })

  it('Fan speed unbekannt instead of the old value in the Wartung (Nutzer 06.11.)', async () => {
    renderWithLive({ ...automation({ phase: 'wartung' }), maintenance: { fan_before: 3 } })
    expect(await screen.findByText('Wartung · danach wieder Stufe 3 · Raum 21,5 °C · Fan speed unbekannt'))
      .toBeInTheDocument()
    expect(screen.queryByText(/1\.520 rpm/)).not.toBeInTheDocument()
  })

  it('Fan speed unbekannt while the A15 does not answer or restarts (Nutzer 06.11.)', async () => {
    for (const offline of [true, 'neustart']) {
      const view = renderWithLive(automation({ offline }))
      expect(await screen.findByText('Basisstufe 3 · Raum 21,5 °C · Fan speed unbekannt')).toBeInTheDocument()
      expect(screen.queryByText(/1\.520 rpm/)).not.toBeInTheDocument()
      view.unmount()
    }
  })

  it('no Fan speed without a live value', async () => {
    mockApi(automation({}))
    renderPage()
    expect(await screen.findByText('Basismodus')).toBeInTheDocument()
    expect(screen.queryByText(/Fan speed/)).not.toBeInTheDocument()
  })
})
