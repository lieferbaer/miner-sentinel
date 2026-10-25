/**
 * Gemeinsame Texte und Formate für das Protokoll der Temperatur-Automatik (Seite „Protokoll“ und die Liste der
 * letzten Ereignisse auf der Lüfter-Seite).
 */
// every kind of automation_log in plain German (fixed list of the contract)
export const KIND_LABEL = {
  basisstufe: 'Basisstufe gewählt',
  automatik_ein: 'Automatik ein',
  automatik_aus: 'Automatik aus',
  wartung_start: 'Wartung gestartet',
  wartung_ende: 'Wartung beendet',
  start: 'Start (Starttakt)',
  takt: 'Taktbefehl',
  modus: 'Modusbefehl',
  luefter: 'Lüfterbefehl',
  hohe_temperatur: 'Hohe Chiptemperatur',
  rueckkehr: 'Rückkehr zur Basisstufe',
  ausnahme: 'Ausnahme: Lüfter −1',
  ausnahme_ende: 'Ausnahme beendet',
  stabilitaet_start: 'Stabilitätsprüfung beginnt',
  stabilitaet_ende: 'Stabilitätsprüfung endet',
  lernen_start: 'Lernperiode beginnt',
  lernen_ende: 'Lernperiode endet',
  lernwert: 'Lernwert gespeichert',
  neustart: 'Neustart des Miners',
  neustart_nutzer: 'Neustart (Nutzer)',
  taktsperre: 'Taktsperre (5 min)',
  taktsperre_ende: 'Taktsperre beendet',
  startpause: 'Startpause (3 min)',
  over_hot: 'Over Hot',
  miner_offline: 'Miner antwortet nicht',
  miner_online: 'Miner antwortet wieder',
  luefter_fehler: 'Lüftersteuerung: Fehler',
  firmware_takt: 'Miner hat den Takt geändert',
  messung_start: 'Takt-Messung beginnt',
  messung_ende: 'Takt-Messung endet',
  test_start: 'Temperatur-Test gestartet',
  test_stufe: 'Temperatur-Test: neue Stufe',
  test_ziel: 'Temperatur-Test: Ziel erreicht',
  test_schritt: 'Temperatur-Test: neuer Schritt',
  test_messung: 'Temperatur-Test: Messung',
  test_ende: 'Temperatur-Test beendet',
  fehler: 'Fehler',
}
// shown in red in both tabs; the list also marks failed commands (ok=false), the Verlauf cannot (its events
// carry no result)
export const ERROR_KINDS = new Set(['fehler', 'over_hot', 'miner_offline', 'luefter_fehler'])

export const MODE_LABEL = { '2/L4': 'mode 04', '2/L3': 'mode 03', '2/L2': 'mode 02', '2/L1': 'mode 01', '2/L0': 'mode 00',
  '1/-': 'High Performance' }

export function fmt(value, digits = 1, unit = '') {
  if (value === null || value === undefined || value === '' || Number.isNaN(Number(value))) return '—'
  return Number(value).toLocaleString('de-DE', { minimumFractionDigits: digits, maximumFractionDigits: digits }) + unit
}

export function signed(value, digits = 0, unit = '') {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return '—'
  return (value > 0 ? '+' : value < 0 ? '−' : '±') + fmt(Math.abs(value), digits, unit)
}

export function has(value) {
  return value !== null && value !== undefined && value !== ''
}

/** "alt → neu"; one value if unchanged, '—' without values. */
export function change(a, b, f = String) {
  if (!has(a) && !has(b)) return '—'
  if (!has(b)) return f(a)
  if (!has(a) || a === b) return f(b)
  return f(a) + ' → ' + f(b)
}

export const levelText = (v) => (Number(v) === 0 ? 'aus' : String(v))
export const modeText = (key) => MODE_LABEL[key] || key

export function clockChange(e) {
  const text = change(e.clock_old, e.clock_new, (v) => fmt(v, 0))
  return text === '—' ? text : text + ' MHz'
}

export function fanChange(e) {
  const fan = change(e.fan_old, e.fan_new, levelText)
  const base = has(e.base_new) && e.base_old !== e.base_new ? 'Basis ' + change(e.base_old, e.base_new) : null
  return [fan !== '—' ? 'Stufe ' + fan : null, base].filter(Boolean).join(' · ') || '—'
}

export function tempText(e) {
  if (!has(e.chip_temp)) return '—'
  return fmt(e.chip_temp, 0, ' °C') + (e.tendency ? ' (' + signed(e.tendency) + ')' : '')
}

/** Shown in red: failed commands and the error kinds (same kinds as in the Verlauf). */
export function isError(e) {
  return e.ok === false || ERROR_KINDS.has(e.kind)
}
