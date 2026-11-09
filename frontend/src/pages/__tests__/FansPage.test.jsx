import api from '@/lib/api'
import { LiveProvider } from '@/lib/LiveContext'
import { act, render, screen, waitFor, within } from '@testing-library/react'
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

// Temperatur-Automatik on, Zieltemperatur 82–85 °C, Basisstufe 3
const automation = (state) => ({
  enabled: true, dev_id: 'ac1', port: 1, level: 3, device: { id: 3, name: 'A15' },
  target_low: 82, target_high: 85, now: { tmax: 86, boards: [86] }, learned: {},
  state: { phase: 'basismodus', t: 86, fan_target: 4, fan_set: 4, fan_read: 4, zb: [82, 85], ...state },
})

function mockApi(auto, status = {}) {
  api.get.mockImplementation((url) => {
    if (url === '/api/fans/status/') {
      return Promise.resolve({
        data: {
          configured: true, session: { test: false }, backups: [], automation: auto,
          devices: [{ dev_id: 'ac1', name: 'Controller', online: true, ports: [{ port: 1, speak: 4 }] }],
          ...status,
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

// Lernwerte on the phone (Nutzer 09.11.): per Stufe a heading "Stufe N", then its entries titled by the Zieltemperatur
const mobileGroups = () => screen.getAllByText(/^Stufe \d+$/)
  .filter((p) => p.nextElementSibling?.tagName === 'UL')
  .map((p) => ({ title: p.textContent, list: p.nextElementSibling, entries: [...p.nextElementSibling.children] }))
const mobileEntry = (level, range) => mobileGroups().find((g) => g.title === 'Stufe ' + level)
  ?.entries.find((li) => li.querySelector('span').textContent === range)

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

  // ZTU 80 (Nutzer 09.11.): after −5 at Basisstufe 1 no increase at 78–79 °C for 30 min
  const cut5 = (cutAt, level) => ({ ...automation({ cut5_at: cutAt, zb: [80, 85] }), target_low: 80, target_high: 85,
    level })
  const plus30 = (iso) => new Date(Date.parse(iso) + 30 * 60000).toISOString()

  it('Basisstufe 1: no increase at ZTU−2–ZTU−1 until 30 min after the −5, with its time (Nutzer 09.11.)', async () => {
    const cutAt = inMin(-10)
    mockApi(cut5(cutAt, 1))
    renderPage()
    expect(await screen.findByText('keine Takterhöhung bei 78–79 °C bis ' + hhmm(plus30(cutAt)) + ' (−5 um ' +
      hhmm(cutAt) + ')')).toBeInTheDocument()
  })

  it('the block after −5 after the other locks, before the excursion over ZTO', async () => {
    const cutAt = inMin(-10)
    const senkung = inMin(4)
    mockApi({ ...cut5(cutAt, 1), state: { ...cut5(cutAt, 1).state, locks: { senkung }, hot: { band: 1 } } })
    renderPage()
    expect(await screen.findByText('keine Takterhöhung bis ' + hhmm(senkung) + ' (nach Senkung) · keine ' +
      'Takterhöhung bei 78–79 °C bis ' + hhmm(plus30(cutAt)) + ' (−5 um ' + hhmm(cutAt) + ') · über ZTO: Bereich 1'))
      .toBeInTheDocument()
  })

  it('no block after −5 at another Basisstufe, after 30 min or without cut5_at', async () => {
    for (const [cutAt, level] of [[inMin(-10), 3], [inMin(-10), 2], [inMin(-30.1), 1], [null, 1]]) {
      mockApi(cut5(cutAt, level))
      const view = renderPage()
      expect(await screen.findByText('Takt regelt 80–85 °C')).toBeInTheDocument()
      expect(screen.queryByText(/keine Takterhöhung bei/)).not.toBeInTheDocument()
      view.unmount()
    }
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
    expect(text).toContain('Unter 80 °C sofort Takt +5 MHz bei 77–79 °C, +15 bei 75–76 °C, +20 bei 70–74 °C, ' +
      '+25 bei 60–69 °C, +40 bei 59 °C oder kälter (höchstens 440 MHz); danach keine Takterhöhung für 3 min ' +
      '(+5) bzw. 2 min (+15, +20, +25, +40). Diese Sperren und die nach Heiz- oder Kühlmodus enden früher ab 84 °C. ' +
      'Die Sperre nach einer Erhöhung endet außerdem, sobald die Chiptemperatur mindestens 3 °C unter der bei der Erhöhung ' +
      'liegt. Keine Takterhöhung, solange ein Befehl noch nicht übernommen ist.')
    expect(text).toContain('Über 85 °C Takt runter: Bereich 1 (86–88 °C) −10 / −5 MHz bei einer ' +
      'ZeitImZieltemperatur davor (ununterbrochen in 80–85 °C) bis 5 min / über 5 min, Bereich 2 ' +
      '(89–91 °C) −20 MHz, Bereich 3 (92–94 °C) −20 MHz, Bereich 4 (ab 95 °C) −50 MHz; mindestens 100 MHz.')
    expect(text).toContain('Nach jeder Senkung 5 min keine Takterhöhung. Bei Basisstufe 1: nach einer Senkung um ' +
      '−5 MHz 30 min keine Takterhöhung bei 78–79 °C (ein Start hebt das auf). Nach Neustart')
    expect(text).toContain('Nach Neustart, Wartung oder Over Hot keine Takterhöhung bis 75 °C, höchstens 10 min ' +
      '(Startpause).')
    expect(text).toContain('15 min ununterbrochen um mehr als 15 MHz')
    expect(text).toContain('Fehlerrate 30 min ununterbrochen über 10 %: automatischer Neustart der A15 (höchstens ' +
      'einer je 6 h).')
  })

  it('start and learning: no cap, Fehlerrate unter 5 %, bei Lüfterstufe 1 unter 7 % (Nutzer 09.11.)', async () => {
    mockApi(off())
    renderPage()
    const start = (await screen.findByText(/^Start \(Stufenwahl/)).textContent
    expect(start).toContain('Heiz- und Kühlmodus enden ohne Sperre, Lüfter auf die Basisstufe')
    expect(start).toContain('Lernwert: 15 min stabil in 80–85 °C bei unveränderter Lüfterstufe, nur bei einer ' +
      'Fehlerrate unter 5 %, bei Lüfterstufe 1 unter 7 % (Mittel der 15 min).')
  })

  it('the block after −5 follows the ZTU', async () => {
    mockApi({ ...off(), target_low: 82, target_high: 86 })
    renderPage()
    expect(await ruleText()).toContain('Bei Basisstufe 1: nach einer Senkung um −5 MHz 30 min keine Takterhöhung bei ' +
      '80–81 °C (ein Start hebt das auf).')
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
    expect(rows).toEqual([['3', '82–85 °C', '320 MHz', '—', '—', '83,2 °C', '—', '—', '2', expect.any(String),
      'Löschen']])
    expect(screen.queryByText(/Obergrenze/)).not.toBeInTheDocument()
    expect(screen.queryByText(/325 MHz|395 MHz/)).not.toBeInTheDocument()
  })

  it('automation off: no Obergrenze, also with old caps', async () => {
    const learned = { 3: { mhz: 320, temp_avg: 83.2, at: '2026-10-28T09:00:00Z', count: 2 } }
    mockApi({ ...automation({ phase: 'aus', caps: { 3: 325 } }), enabled: false, learned })
    renderPage()
    expect(await screen.findByRole('button', { name: /Einschalten/ })).toBeInTheDocument()
    expect(screen.queryByText(/Obergrenze/)).not.toBeInTheDocument()
    expect(screen.queryByText(/325 MHz/)).not.toBeInTheDocument()
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

  it('a deleted Lernwert is loaded and shown as a user entry (Nutzer 08.11.)', async () => {
    mockApi(automation({}))
    const status = api.get.getMockImplementation()
    api.get.mockImplementation((url, config) => {
      if (url === '/api/automation/log/') {
        return Promise.resolve({
          data: {
            results: [{ id: 1, kind: 'lernwert_loeschen', recorded_at: '2026-11-08T10:00:00Z', chip_temp: 83,
              trigger: 'Nutzer: Lernwert Stufe 3 (82–85 °C) gelöscht' }],
          },
        })
      }
      return status(url, config)
    })
    renderPage()
    const labels = await screen.findAllByText('Lernwert gelöscht')
    expect(labels[0]).toHaveClass('text-muted-foreground')
    expect(screen.getAllByText('Nutzer: Lernwert Stufe 3 (82–85 °C) gelöscht').length).toBeGreaterThan(0)
    const kinds = api.get.mock.calls.find(([url]) => url === '/api/automation/log/')[1].params.kind.split(',')
    expect(kinds).toContain('lernwert_loeschen')
  })
})

describe('FansPage Lernwerte', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('Zieltemperatur right after Stufe (the one of the learned values = state.zb), Takt as Gesetzter Takt', async () => {
    const learned = { 2: { mhz: 275, temp_avg: 82, ths_avg: 133.4, j_th: 16.9, at: '2026-10-28T12:39:51Z', count: 13 } }
    // Zieltemperatur 83–86 gewählt, der data-service hat noch 82–85: die Lernwerte gehören zu 82–85
    mockApi({ ...automation({ zb: [82, 85] }), target_low: 83, target_high: 86, learned })
    renderPage()
    const table = (await screen.findByRole('columnheader', { name: 'Zieltemperatur' })).closest('table')
    const heads = within(table).getAllByRole('columnheader').map((c) => c.textContent)
    expect(heads).toEqual(['Stufe', 'Zieltemperatur', 'Gesetzter Takt', 'Takt laut Miner (Ø)', 'Modus',
      'Ø Temperatur', 'Ø Hashrate', 'Effizienz', 'Anzahl', 'Zeit', ''])
    const cells = within(within(table).getAllByRole('row')[1]).getAllByRole('cell').map((c) => c.textContent)
    expect(cells.slice(0, 9)).toEqual(['2', '82–85 °C', '275 MHz', '—', '—', '82,0 °C', '133,4 TH/s', '16,9 J/TH',
      '13'])
  })

  it('mobile: one entry per level instead of the wide table', async () => {
    const learned = { 2: { mhz: 260, temp_avg: 81.9, ths_avg: 131.6, j_th: 17.1, at: '2026-10-28T14:37:57Z', count: 21 } }
    mockApi({ ...automation({ zb: [81, 84] }), learned })
    renderPage()
    expect(await screen.findByRole('columnheader', { name: 'Zieltemperatur' })).toBeInTheDocument()
    const first = mobileEntry(2, '81–84 °C')
    expect(first.closest('ul').parentElement.parentElement).toHaveClass('sm:hidden')
    // label above value, two per line
    const pairs = (li) => [...li.querySelectorAll('.grid > div')].map((d) => [...d.children].map((c) => c.textContent))
    expect(pairs(first)).toEqual([['Gesetzter Takt', '260 MHz'], ['Takt laut Miner (Ø)', '—'], ['Modus', '—'],
      ['Ø Temperatur', '81,9 °C'], ['Ø Hashrate', '131,6 TH/s'], ['Effizienz', '17,1 J/TH'], ['Anzahl', '21']])
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

  it('Takt laut Miner (Ø) right after Gesetzter Takt, — for old values without it (Nutzer 08.11.)', async () => {
    const learned = {
      2: { mhz: 275, mhz_miner: 271.6, temp_avg: 82, at: '2026-11-08T12:39:51Z', count: 3 },
      3: { mhz: 320, temp_avg: 83.2, at: '2026-10-28T09:00:00Z', count: 2 },
    }
    const others = { '81-84': { 4: { mhz: 400, mhz_miner: 396.2, temp_avg: 82.5, at: '2026-11-07T10:00:00Z', count: 1 } } }
    mockApi({ ...automation({ zb: [82, 85] }), learned, learned_zb: others })
    renderPage()
    const table = (await screen.findByRole('columnheader', { name: 'Takt laut Miner (Ø)' })).closest('table')
    const rows = within(table).getAllByRole('row').slice(1)
    expect(rows.map((r) => within(r).getAllByRole('cell').slice(2, 4).map((c) => c.textContent))).toEqual([
      ['275 MHz', '272 MHz'], ['320 MHz', '—'], ['400 MHz', '396 MHz']])
    expect(screen.queryByText('Andere Zieltemperaturen')).not.toBeInTheDocument()
    // mobile: after Gesetzter Takt in each entry
    const pairs = (level, range) => [...mobileEntry(level, range).querySelectorAll('.grid > div')]
      .slice(0, 2).map((d) => [...d.children].map((c) => c.textContent))
    expect(pairs(2, '82–85 °C')).toEqual([['Gesetzter Takt', '275 MHz'], ['Takt laut Miner (Ø)', '272 MHz']])
    expect(pairs(3, '82–85 °C')).toEqual([['Gesetzter Takt', '320 MHz'], ['Takt laut Miner (Ø)', '—']])
    expect(pairs(4, '81–84 °C')).toEqual([['Gesetzter Takt', '400 MHz'], ['Takt laut Miner (Ø)', '396 MHz']])
  })

  // current Zieltemperatur 81–85 (Basisstufe 3); the other ones in the order of the keys deliberately unsorted
  const grouped = () => ({
    learned: { 3: { mhz: 360, temp_avg: 81.6, at: '2026-11-08T18:55:39Z', count: 1 } },
    learned_zb: {
      '82-86': { 2: { mhz: 275.4, temp_avg: 82.7, at: '2026-11-08T20:49:00Z', count: 5 } },
      '81-84': { 3: { mhz: 360, temp_avg: 82, at: '2026-11-08T16:11:00Z', count: 2 },
        2: { mhz: 260, temp_avg: 81.9, at: '2026-11-08T12:44:00Z', count: 3 } },
      '82-85': { 4: { mhz: 405, temp_avg: 83, at: '2026-10-27T02:08:00Z', count: 1 } },
    },
  })

  it('grouped by Stufe: current Zieltemperatur first, then the others grey by ZTU then ZTO (Nutzer 09.11.)', async () => {
    mockApi({ ...automation({ zb: [81, 85] }), target_low: 81, target_high: 85, ...grouped() })
    renderPage()
    const table = (await screen.findByRole('columnheader', { name: 'Zieltemperatur' })).closest('table')
    const rows = within(table).getAllByRole('row').slice(1)
    expect(rows.map((r) => within(r).getAllByRole('cell').slice(0, 3).map((c) => c.textContent))).toEqual([
      ['2', '81–84 °C', '260 MHz'], ['2', '82–86 °C', '275 MHz'],
      ['3', '81–85 °C', '360 MHz'], ['3', '81–84 °C', '360 MHz'],
      ['4', '82–85 °C', '405 MHz']])
    // only the current one at the Basisstufe normal and bold, all others grey
    expect(rows[2]).toHaveClass('font-semibold')
    expect(rows[2]).not.toHaveClass('text-muted-foreground')
    expect(rows.filter((r, i) => i !== 2).every((r) => r.classList.contains('text-muted-foreground'))).toBe(true)
    // a line between the Stufe groups: on the first row of each further group
    expect(rows.map((r) => r.classList.contains('border-t'))).toEqual([false, false, true, false, true])
    expect(screen.queryByText('Andere Zieltemperaturen')).not.toBeInTheDocument()
  })

  it('a current value of another level than the Basisstufe is normal, not bold', async () => {
    const { learned_zb } = grouped()
    mockApi({ ...automation({ zb: [81, 85] }), target_low: 81, target_high: 85, learned_zb,
      learned: { 2: { mhz: 280, temp_avg: 82, at: '2026-11-09T08:00:00Z', count: 1 } } })
    renderPage()
    const table = (await screen.findByRole('columnheader', { name: 'Zieltemperatur' })).closest('table')
    const rows = within(table).getAllByRole('row').slice(1)
    expect(rows.map((r) => within(r).getAllByRole('cell')[1].textContent)).toEqual(['81–85 °C', '81–84 °C',
      '82–86 °C', '81–84 °C', '82–85 °C'])
    expect(rows[0]).not.toHaveClass('text-muted-foreground')
    expect(rows[0]).not.toHaveClass('font-semibold')
    expect(rows.slice(1).every((r) => r.classList.contains('text-muted-foreground'))).toBe(true)
  })

  it('mobile: per Stufe a heading, then its entries titled by the Zieltemperatur, the others grey', async () => {
    mockApi({ ...automation({ zb: [81, 85] }), target_low: 81, target_high: 85, ...grouped() })
    renderPage()
    expect(await screen.findByRole('columnheader', { name: 'Zieltemperatur' })).toBeInTheDocument()
    const groups = mobileGroups()
    expect(groups.map((g) => [g.title, g.entries.map((li) => li.querySelector('span').textContent)])).toEqual([
      ['Stufe 2', ['81–84 °C', '82–86 °C']], ['Stufe 3', ['81–85 °C', '81–84 °C']], ['Stufe 4', ['82–85 °C']]])
    const grey = (li) => li.classList.contains('text-muted-foreground')
    expect(groups.map((g) => g.entries.map(grey))).toEqual([[true, true], [false, true], [true]])
    expect(groups[1].entries[0]).toHaveClass('font-semibold')
    expect(groups.every((g) => g.list.closest('.sm\\:hidden'))).toBe(true)
    expect(screen.queryByText(/^Stufe \d+ · /)).not.toBeInTheDocument()
  })

  it('Modus after Takt laut Miner (Ø): label of the work mode, the key if unknown, — for old values (Nutzer 09.11.)',
    async () => {
      const learned = {
        1: { mhz: 170, mode: '2/L4', temp_avg: 80.5, at: '2026-11-09T09:00:00Z', count: 1 },
        2: { mhz: 260, mode: '1/-', temp_avg: 81, at: '2026-11-09T08:00:00Z', count: 1 },
        3: { mhz: 320, mode: '9/X', temp_avg: 83.2, at: '2026-11-09T07:00:00Z', count: 1 },
        4: { mhz: 380, temp_avg: 83, at: '2026-10-28T09:00:00Z', count: 2 },
      }
      mockApi({ ...automation({ zb: [82, 85] }), learned })
      renderPage()
      const table = (await screen.findByRole('columnheader', { name: 'Modus' })).closest('table')
      const heads = within(table).getAllByRole('columnheader').map((c) => c.textContent)
      expect(heads.indexOf('Modus')).toBe(heads.indexOf('Takt laut Miner (Ø)') + 1)
      const rows = within(table).getAllByRole('row').slice(1)
      expect(rows.map((r) => within(r).getAllByRole('cell')[4].textContent)).toEqual(['mode 04', 'High Performance',
        '9/X', '—'])
      // mobile: in each entry after Takt laut Miner (Ø)
      const third = (level) => [...mobileEntry(level, '82–85 °C').querySelectorAll('.grid > div')][2]
      expect([1, 2, 3, 4].map((lv) => [...third(lv).children].map((c) => c.textContent))).toEqual([
        ['Modus', 'mode 04'], ['Modus', 'High Performance'], ['Modus', '9/X'], ['Modus', '—']])
    })

  it('no learned value in the current Zieltemperatur: the note, then only the grey values in their groups',
    async () => {
      const others = { '81-84': { 2: { mhz: 260, temp_avg: 81.9, at: '2026-11-08T12:44:00Z', count: 3 } } }
      mockApi({ ...automation({}), learned_zb: others })
      renderPage()
      expect(await screen.findByText('Lernwerte: noch keine (nach 15 min stabil bei 82–85 °C und einer Fehlerrate ' +
        'unter 5 %, bei Lüfterstufe 1 unter 7 %).')).toBeInTheDocument()
      const table = screen.getByRole('columnheader', { name: 'Zieltemperatur' }).closest('table')
      const rows = within(table).getAllByRole('row').slice(1)
      expect(rows.map((r) => within(r).getAllByRole('cell').slice(0, 3).map((c) => c.textContent))).toEqual([
        ['2', '81–84 °C', '260 MHz']])
      expect(rows[0]).toHaveClass('text-muted-foreground')
      expect(mobileGroups().map((g) => g.title)).toEqual(['Stufe 2'])
      expect(mobileEntry(2, '81–84 °C')).toHaveClass('text-muted-foreground')
    })

  it('no learned values yet: 15 min stable in the Zieltemperatur, error rate below 5 %, at Lüfterstufe 1 below 7 %',
    async () => {
      mockApi(automation({}))
      renderPage()
      expect(await screen.findByText('Lernwerte: noch keine (nach 15 min stabil bei 82–85 °C und einer Fehlerrate ' +
        'unter 5 %, bei Lüfterstufe 1 unter 7 %).')).toBeInTheDocument()
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

describe('FansPage Lernwert löschen (Nutzer 08.11.)', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  const learned = { 3: { mhz: 320, temp_avg: 83.2, at: '2026-10-28T09:00:00Z', count: 2 } }
  const others = { '81-84': { 2: { mhz: 260, temp_avg: 81.9, at: '2026-11-08T12:44:00Z', count: 3 } } }
  const desktop = async () => (await screen.findByRole('columnheader', { name: 'Zieltemperatur' })).closest('table')
  const rowOf = (table, range) => within(table).getAllByRole('row')
    .find((r) => within(r).queryAllByRole('cell')[1]?.textContent === range)

  it('Löschen: question, Nein sends nothing, Ja sends Zieltemperatur, Stufe and at of the value', async () => {
    const user = userEvent.setup()
    const auto = { ...automation({ zb: [82, 85] }), learned }
    mockApi(auto)
    api.post.mockResolvedValue({ data: auto })
    renderPage()
    const table = await desktop()
    expect(within(table).getAllByRole('columnheader').at(-1)).toHaveTextContent(/^$/)
    await user.click(within(table).getByRole('button', { name: 'Löschen' }))
    expect(within(table).getByText('Lernwert wirklich löschen?')).toBeInTheDocument()
    expect(within(table).getByRole('button', { name: 'Ja' })).toHaveClass('bg-destructive')
    expect(within(table).getByRole('button', { name: 'Nein' })).toHaveClass('bg-secondary')
    await user.click(within(table).getByRole('button', { name: 'Nein' }))
    expect(screen.queryByText('Lernwert wirklich löschen?')).not.toBeInTheDocument()
    expect(api.post).not.toHaveBeenCalled()

    await user.click(within(table).getByRole('button', { name: 'Löschen' }))
    await user.click(within(table).getByRole('button', { name: 'Ja' }))
    expect(api.post).toHaveBeenCalledTimes(1)
    expect(api.post).toHaveBeenCalledWith('/api/fans/automation/',
      { dev_id: 'ac1', action: 'learned_delete', zb: '82-85', level: 3, at: '2026-10-28T09:00:00Z' })
    // the answer still has the value (the data-service deletes it): the button stays disabled, also on the phone
    await waitFor(() => expect(screen.getByRole('button', { name: 'Automatik beenden' })).toBeEnabled())
    expect(screen.getAllByRole('button', { name: 'Löschen' })).toHaveLength(2)
    for (const button of screen.getAllByRole('button', { name: 'Löschen' })) expect(button).toBeDisabled()
  })

  it('the value disappears with the live poll (every 5 s) once the data-service has deleted it', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    try {
      const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime })
      const auto = { ...automation({ zb: [82, 85] }), learned }
      let live = auto
      mockApi(auto)
      const status = api.get.getMockImplementation()
      api.get.mockImplementation((url) => {
        if (url === '/api/live/') {
          return Promise.resolve({ data: { devices: [], mining: [], system: {}, hardware: [], automation: live } })
        }
        return status(url)
      })
      api.post.mockResolvedValue({ data: auto })
      render(<LiveProvider><MemoryRouter><FansPage /></MemoryRouter></LiveProvider>)
      const table = await desktop()
      await user.click(within(table).getByRole('button', { name: 'Löschen' }))
      await user.click(within(table).getByRole('button', { name: 'Ja' }))
      await waitFor(() => expect(screen.getByRole('button', { name: 'Automatik beenden' })).toBeEnabled())
      expect(within(table).getByRole('button', { name: 'Löschen' })).toBeDisabled()
      live = { ...auto, learned: {} }
      await act(() => vi.advanceTimersByTimeAsync(5000))
      expect(screen.getByText('Lernwerte: noch keine (nach 15 min stabil bei 82–85 °C und einer Fehlerrate unter 5 %, ' +
        'bei Lüfterstufe 1 unter 7 %).')).toBeInTheDocument()
      expect(screen.queryByRole('button', { name: 'Löschen' })).not.toBeInTheDocument()
    } finally {
      vi.useRealTimers()
    }
  })

  it('without state.zb no button for the current values, the other Zieltemperaturen keep theirs', async () => {
    mockApi({ ...automation({ zb: null }), learned, learned_zb: others })
    renderPage()
    const table = await desktop()
    expect(within(rowOf(table, '—')).queryByRole('button')).not.toBeInTheDocument()
    expect(within(rowOf(table, '81–84 °C')).getByRole('button', { name: 'Löschen' })).toBeInTheDocument()
    expect(within(mobileEntry(3, '—')).queryByRole('button')).not.toBeInTheDocument()
    expect(within(mobileEntry(2, '81–84 °C')).getByRole('button', { name: 'Löschen' })).toBeInTheDocument()
  })

  it('Automatik off: the table below the Einschalten button, Löschen of another Zieltemperatur on the phone', async () => {
    const user = userEvent.setup()
    const auto = { ...automation({ phase: 'aus', zb: [82, 85] }), enabled: false, learned, learned_zb: others }
    mockApi(auto)
    api.post.mockResolvedValue({ data: auto })
    renderPage()
    const enable = await screen.findByRole('button', { name: /Einschalten/ })
    const table = await desktop()
    expect(enable.compareDocumentPosition(table) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
    expect(within(table).getAllByRole('button', { name: 'Löschen' })).toHaveLength(2)
    const entry = mobileEntry(2, '81–84 °C')
    await user.click(within(entry).getByRole('button', { name: 'Löschen' }))
    await user.click(within(entry).getByRole('button', { name: 'Ja' }))
    expect(api.post).toHaveBeenCalledWith('/api/fans/automation/',
      { dev_id: 'ac1', action: 'learned_delete', zb: '81-84', level: 2, at: '2026-11-08T12:44:00Z' })
  })

  it('an error of the server is shown like the other Automatik commands', async () => {
    const user = userEvent.setup()
    mockApi({ ...automation({ zb: [82, 85] }), learned })
    api.post.mockRejectedValue({ response: { status: 409, data: { error: 'Lernwert nicht mehr vorhanden oder neu gelernt.' } } })
    renderPage()
    const table = await desktop()
    await user.click(within(table).getByRole('button', { name: 'Löschen' }))
    await user.click(within(table).getByRole('button', { name: 'Ja' }))
    expect(await screen.findByText('Lernwert nicht mehr vorhanden oder neu gelernt.')).toHaveClass('text-destructive')
  })

  it('a failed request deletes nothing: Löschen is free again', async () => {
    const user = userEvent.setup()
    mockApi({ ...automation({ zb: [82, 85] }), learned })
    api.post.mockRejectedValue({ response: { status: 500, data: { error: 'Lernwert nicht gelöscht.' } } })
    renderPage()
    const table = await desktop()
    await user.click(within(table).getByRole('button', { name: 'Löschen' }))
    await user.click(within(table).getByRole('button', { name: 'Ja' }))
    expect(await screen.findByText('Lernwert nicht gelöscht.')).toHaveClass('text-destructive')
    for (const button of screen.getAllByRole('button', { name: 'Löschen' })) expect(button).toBeEnabled()
  })

  it('switching the Automatik off while the value is not yet deleted keeps Löschen disabled', async () => {
    const user = userEvent.setup()
    const auto = { ...automation({ zb: [82, 85] }), learned }
    mockApi(auto)
    api.post.mockResolvedValueOnce({ data: auto })
      .mockResolvedValueOnce({ data: { ...auto, enabled: false, state: { ...auto.state, phase: 'aus' } } })
    renderPage()
    const table = await desktop()
    await user.click(within(table).getByRole('button', { name: 'Löschen' }))
    await user.click(within(table).getByRole('button', { name: 'Ja' }))
    await waitFor(() => expect(screen.getByRole('button', { name: 'Automatik beenden' })).toBeEnabled())
    await user.click(screen.getByRole('button', { name: 'Automatik beenden' }))
    await user.click(screen.getByRole('button', { name: 'Ja' }))
    expect(await screen.findByRole('button', { name: /Einschalten/ })).toBeInTheDocument()
    expect(screen.getAllByRole('button', { name: 'Löschen' })).toHaveLength(2)
    for (const button of screen.getAllByRole('button', { name: 'Löschen' })) expect(button).toBeDisabled()
  })
})

describe('FansPage Sicherung löschen (Nutzer 08.11., B)', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  const backup = (file, at) => ({ file, dev_id: 'ac1', created_at: at, ports: [0, 1] })
  const one = [backup('backup-20261107-100000-ac1.json', '2026-11-07T10:00:00Z')]
  const two = [...one, backup('backup-20261108-090000-ac1.json', '2026-11-08T09:00:00Z')]
  const list = async () => (await screen.findByText('Sicherung (Einstellungen Port 0–4)')).parentElement.querySelector('ul')

  it('no Löschen with only one backup of the controller (one of another controller does not count)', async () => {
    const foreign = { ...backup('backup-20261108-090000-other.json', '2026-11-08T09:00:00Z'), dev_id: 'other' }
    mockApi(automation({}), { backups: [...one, foreign] })
    renderPage()
    const ul = await list()
    expect(within(ul).getAllByRole('listitem')).toHaveLength(1)
    expect(within(ul).getByRole('button', { name: 'Zurückschreiben' })).toBeInTheDocument()
    expect(within(ul).queryByRole('button', { name: 'Löschen' })).not.toBeInTheDocument()
  })

  it('two backups: Löschen with question, one question at a time, Ja posts the file and reloads', async () => {
    const user = userEvent.setup()
    mockApi(automation({}), { backups: two })
    let answer
    api.post.mockImplementation(() => new Promise((resolve) => { answer = resolve }))
    renderPage()
    const ul = await list()
    const [first, second] = within(ul).getAllByRole('listitem')
    expect(within(ul).getAllByRole('button', { name: 'Löschen' })).toHaveLength(2)
    await user.click(within(first).getByRole('button', { name: 'Löschen' }))
    expect(within(first).getByText('Sicherung wirklich löschen?')).toBeInTheDocument()
    expect(within(first).getByRole('button', { name: 'Ja' })).toHaveClass('bg-destructive')
    expect(within(first).getByRole('button', { name: 'Nein' })).toHaveClass('bg-secondary')
    // the restore question of the other backup closes the delete question
    await user.click(within(second).getByRole('button', { name: 'Zurückschreiben' }))
    expect(screen.queryByText('Sicherung wirklich löschen?')).not.toBeInTheDocument()
    expect(within(second).getByText('Wirklich zurückschreiben?')).toBeInTheDocument()
    await user.click(within(first).getByRole('button', { name: 'Löschen' }))
    expect(screen.queryByText('Wirklich zurückschreiben?')).not.toBeInTheDocument()
    await user.click(within(first).getByRole('button', { name: 'Nein' }))
    expect(api.post).not.toHaveBeenCalled()

    const loads = api.get.mock.calls.filter(([url]) => url === '/api/fans/status/').length
    await user.click(within(first).getByRole('button', { name: 'Löschen' }))
    await user.click(within(first).getByRole('button', { name: 'Ja' }))
    expect(api.post).toHaveBeenCalledTimes(1)
    expect(api.post).toHaveBeenCalledWith('/api/fans/backup/delete/', { file: 'backup-20261107-100000-ac1.json' })
    expect(screen.queryByText(/Befehl gesendet/)).not.toBeInTheDocument()
    await act(async () => answer({ data: { deleted: 'backup-20261107-100000-ac1.json', backups: one } }))
    expect(await screen.findByText('Sicherung gelöscht.')).toHaveClass('text-emerald-600')
    await waitFor(() => expect(api.get.mock.calls.filter(([url]) => url === '/api/fans/status/').length).toBe(loads + 1))
  })

  it('an error of the server is shown', async () => {
    const user = userEvent.setup()
    mockApi(automation({}), { backups: two })
    api.post.mockRejectedValue({ response: { status: 409, data: { error: 'Die letzte Sicherung dieses Controllers bleibt erhalten.' } } })
    renderPage()
    const ul = await list()
    await user.click(within(ul).getAllByRole('button', { name: 'Löschen' })[0])
    await user.click(within(ul).getByRole('button', { name: 'Ja' }))
    expect(await screen.findByText('Die letzte Sicherung dieses Controllers bleibt erhalten.')).toHaveClass('text-destructive')
  })

  it('without a text of the server: Löschen fehlgeschlagen.', async () => {
    const user = userEvent.setup()
    mockApi(automation({}), { backups: two })
    api.post.mockRejectedValue(new Error('Network Error'))
    renderPage()
    const ul = await list()
    await user.click(within(ul).getAllByRole('button', { name: 'Löschen' })[1])
    await user.click(within(ul).getByRole('button', { name: 'Ja' }))
    expect(await screen.findByText('Löschen fehlgeschlagen.')).toBeInTheDocument()
  })
})
