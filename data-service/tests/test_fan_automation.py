"""Temperatur-Automatik nach dem Plan vom 24.10.2026: Basisstufe fest, Takt regelt 80-84 °C, Protokoll je Zyklus."""
from datetime import datetime, timedelta, timezone

import pytest

import automation_journal
import calibration
import fan_automation as fa
import miner_cache

T0 = datetime(2026, 10, 24, 12, 0, tzinfo=timezone.utc)
STOCK = {'2/L4': [330, 349, 369, 390], '2/L3': [351, 370, 390, 411], '2/L2': [371, 390, 410, 431],
         '2/L1': [391, 410, 430, 451], '2/L0': [411, 430, 450, 471]}
PROFILES = {k: {'points': STOCK[k]} for k in ('2/L4', '2/L3', '2/L0')}     # 01 und 02 kennt nur der Miner


class FakeAC:
    class ACError(Exception):
        pass

    def __init__(self, level=5):
        self.level = level
        self.writes = []
        self.power = []
        self.fail = False

    def speed_of(self, dev_id, port):
        return self.level

    def set_fan_level(self, dev_id, port, level):
        if self.fail:
            raise self.ACError('Cloud nicht erreichbar')
        self.writes.append(level)
        self.level = level
        return {'code': 200}

    def set_fan_power(self, dev_id, port, on, level=None):
        self.power.append(('an' if on else 'aus', level))
        if on and level is not None:
            self.level = level


class Miner:
    """The A15 as the automation sees it: TMax, the four clock points (SF0), the power level, uptime."""

    def __init__(self, tmax=82, level=4, points=None):
        self.tmax = tmax
        self.level = level
        self.points = list(points or STOCK['2/L4'])
        self.clock_sets, self.mode_sets = [], []
        self.elapsed = 1000
        self.status = 'Work: In Work'
        self.offline = False
        self.ignore_clock = False
        self.web_down = False                                    # nur die Einstellungsseite antwortet nicht
        self.never_read = False                                  # ... und wurde seit dem Start nie gelesen
        self.dh = 1.2                                            # Fehlerrate DH in %
        self.ghs = 150000.0                                      # Hashrate GHSspd in GH/s (Wert oder Funktion)

    def read(self, ip, port, fresh=False):
        if self.offline:
            raise OSError('timed out')
        self.elapsed += 5
        conf = {'mode': 2, 'power_level': self.level}
        unknown = self.web_down and self.never_read
        return {'fields': {'TMax': str(self.tmax), 'MTmax': '%s 70 69' % self.tmax}, 'points': list(self.points),
                'mode_key': None if unknown else '2/L%d' % self.level, 'status': self.status, 'elapsed': self.elapsed,
                'conf': None if unknown else dict(conf, _stale=True) if self.web_down else conf,
                'conf_fresh': not self.web_down,
                'web_error': 'A15-Einstellungsseite lesen: timed out' if self.web_down else None,
                'power': 2000.0, 'ghsspd': self.ghs(self) if callable(self.ghs) else self.ghs, 'dh': self.dh}

    def set_frequency(self, ip, port, points):
        self.clock_sets.append(sum(points) / len(points))
        if not self.ignore_clock:
            self.points = list(points)
        return 'STATUS=S,Msg=ASC 0 set OK'

    def set_mode(self, ip, conf, mode, level):
        self.mode_sets.append(level)
        self.level = level
        self.points = list(STOCK['2/L%d' % level])        # der Moduswechsel stellt den Standardtakt ein

    def clock(self):
        return round(sum(self.points) / len(self.points))


@pytest.fixture
def miner(monkeypatch):
    mn = Miner()
    monkeypatch.setattr(calibration, 'read_miner', mn.read)
    monkeypatch.setattr(calibration, 'set_frequency', mn.set_frequency)
    monkeypatch.setattr(calibration, 'a15_set_mode', mn.set_mode)

    def no_network(addr, timeout=None):                      # Neustart ab 4 % (28.10.): nie eine echte Verbindung
        raise AssertionError('Verbindung im Test ohne _connect: %s' % (addr,))
    monkeypatch.setattr(calibration.socket, 'create_connection', no_network)
    return mn


def row(state=None, level=5, enabled=True, cal=False, level_at=None, maintenance_at=None, temp_test_at=None,
        restart_req_at=None, target_low=79, target_high=84, caps_cleared_at=None):
    return {'id': 1, 'device_id': 3, 'enabled': enabled, 'ac_dev_id': 'dev', 'ac_port': 1, 'level': level,
            'level_at': level_at, 'maintenance_at': maintenance_at, 'temp_test_at': temp_test_at, 'state': state or {},
            'restart_req_at': restart_req_at, 'target_low': target_low, 'target_high': target_high,
            'caps_cleared_at': caps_cleared_at,
            'ip_address': '10.0.0.15', 'port': 4028, 'cal_active': cal,
            'connection_config': {'throttle': {'profiles': PROFILES}}}


class Run:
    """Rounds every 5 s with one journal; temperature can follow a function of the clock and fan level."""

    def __init__(self, miner, ac, level=5, state=None, journal=None):
        self.miner, self.ac, self.level = miner, ac, level
        self.st = state or {}
        self.now = T0
        self.j = journal or automation_journal.MemoryJournal()
        self.kw = {}

    def go(self, seconds, temp=None, **kw):
        self.kw.update(kw)
        for _ in range(int(seconds // 5)):
            if temp is not None:
                self.miner.tmax = temp(self) if callable(temp) else temp
            self.st = fa.step(row(self.st, level=self.level, **self.kw), self.now, self.ac, self.j)
            self.now += timedelta(seconds=5)
        return self

    def events(self, kind):
        return [e for e in self.j.events if e['kind'] == kind]


def started(miner, ac, level=5, tmax=82, learned=None, zb=None):
    """Automatik ein (Basisstufe `level`, Zielbereich `zb`, sonst 79–84 °C) and the start clock taken over."""
    r = Run(miner, ac, level)
    if zb:
        r.kw.update(target_low=zb[0], target_high=zb[1])
    if learned:
        # Lernwerte des eingestellten Bereichs (ohne gemerkten Bereich gehörten sie zum Startwert 82–85)
        r.st = {'v': fa.STATE_VERSION, 'seen_at': T0.isoformat(), 'learned': learned, 'level_seen': 'x',
                'zb': list(zb or (79, 84))}
        r.kw['level_at'] = 'klick'
    miner.tmax = tmax
    r.go(10)
    return r


def _pulses(r, temp):
    """Lüfterstöße (Nutzer 27.10.): ab U+1 bis U+3 aus L-U oder darunter zuerst zwei Stöße, kein Takt – bei gleicher
    Temperatur bis zur Runde vor dem Ende (70 s); in der nächsten Runde beginnt die Korrekturphase wie bisher."""
    sets, clock = list(r.miner.clock_sets), r.st.get('clock_set')
    r.go(70, temp=temp)
    ps = r.st['stoss']
    assert ps['n'] == 2 and ps['step'] == 'aus' and not r.st.get('corr')
    assert r.st.get('clock_set') == clock and r.miner.clock_sets == sets
    return r


# ---------------------------------------------------------------- Start (Kapitel 4)

def test_start_without_learned_value_uses_initial_value(miner):
    ac = FakeAC(level=3)
    r = started(miner, ac, level=3)
    assert [round(x) for x in miner.clock_sets] == [330]                 # Stufe 3 = 330 MHz, Modus 04 passt schon
    assert miner.mode_sets == [] and r.st['clock_set'] == 330 and r.st['phase'] == 'normal'
    assert r.events('start')[0]['details']['quelle'] == 'Initialwert'
    assert r.events('takt')[0]['takeover_state'] == 'gemessen'


def test_start_uses_learned_value_of_the_base_level(miner):
    ac = FakeAC(level=3)
    r = started(miner, ac, level=3, learned={'3': {'mhz': 250, 'temp_avg': 82.0, 'at': 'x', 'count': 2}})
    assert r.st['clock_set'] == 250 and miner.clock() == 250
    assert r.events('start')[-1]['details']['quelle'] == 'Lernwert'


def test_start_with_mode_change_sets_mode_first(miner):
    ac = FakeAC(level=4)
    r = started(miner, ac, level=4, learned={'4': {'mhz': 390, 'temp_avg': 82, 'at': 'x', 'count': 1}})
    assert miner.mode_sets == [2]                      # 390 MHz → mode 02 (Tabelle ohne Spielraum)
    assert r.st['stock']['2/L2'] == STOCK['2/L2']      # Standardtakt von mode 02 beim Wechsel gemerkt
    assert miner.clock() == 390
    kinds = [k for k in r.j.kinds() if k in ('modus', 'takt')]
    assert kinds == ['modus', 'takt']


def test_no_clock_command_when_miner_already_runs_the_target(miner):
    ac = FakeAC(level=3)
    miner.points = fa.points_for(STOCK['2/L4'], 330)
    r = started(miner, ac, level=3)
    assert miner.clock_sets == []                      # Zieltakt = Takt laut Miner: kein Befehl
    assert r.events('takt')[0]['details']['kein_befehl']


def test_old_state_starts_without_learned_values(miner):
    ac = FakeAC(level=2)
    old = {'learned': {'2/L4': {'2': {'mhz': 241}}}, 'seen_at': 'x', 'phase': 'normal', 'clock_set': 231}
    r = Run(miner, ac, level=2, state=old).go(10)
    assert r.st['v'] == fa.STATE_VERSION and r.st['learned'] == {}
    assert [round(x) for x in miner.clock_sets] == [250]                 # neue Regelung startet ohne Lernwerte: Stufe 2 = 250


def test_click_on_level_starts_again(miner):
    ac = FakeAC(level=5)
    r = started(miner, ac, level=5)
    r.level = 3
    r.go(10, level_at='neu')
    assert miner.clock() == 330 and r.st['fan_target'] == 3 and ac.level == 3


# ---------------------------------------------------------------- Schritt 1: hohe Chiptemperatur

def test_high_temperature_fan_and_clock_at_once(miner):
    ac = FakeAC(level=5)
    r = started(miner, ac, level=5, learned={'5': {'mhz': 300, 'temp_avg': 82, 'at': 'x', 'count': 1}})
    r.go(5, temp=88)
    assert ac.level == 7 and r.st['phase'] == 'korrektur'        # Basisstufe + 2
    assert miner.clock() == 270                                   # 300 − 30 sofort
    r.go(25, temp=88)
    assert miner.clock() == 270                                   # Beobachtungsfenster 30 s
    r.go(10, temp=88)
    assert miner.clock() == 240                                   # noch über der Schwelle, nicht fallend: erneut
    r.go(40, temp=lambda r: 88 if r.now < T0 + timedelta(seconds=105) else 87)
    assert miner.clock() in (240, 210)                            # fallend → keine weitere Korrektur in diesem Fenster


def test_fan_correction_relative_to_base_not_added(miner):
    ac = FakeAC(level=3)
    r = started(miner, ac, level=3, learned={'3': {'mhz': 300, 'temp_avg': 82, 'at': 'x', 'count': 1}})
    r.go(5, temp=85)
    assert ac.level == 4
    r.go(5, temp=90)
    assert ac.level == 5                                          # Basisstufe + 2, nicht 4 + 2
    r.go(5, temp=94)
    assert ac.level == 5


def test_lower_correction_level_only_after_30_s(miner):
    ac = FakeAC(level=5)
    r = started(miner, ac, level=5, learned={'5': {'mhz': 300, 'temp_avg': 82, 'at': 'x', 'count': 1}})
    r.go(5, temp=88)
    assert ac.level == 7
    r.go(25, temp=87)
    assert ac.level == 7                                          # 87 verlangt + 1: erst nach 30 s
    r.go(10, temp=87)
    assert ac.level == 6


def test_clock_below_100_is_an_error_and_not_sent(miner):
    ac = FakeAC(level=5)
    r = started(miner, ac, level=5, learned={'5': {'mhz': 130, 'temp_avg': 82, 'at': 'x', 'count': 1}})
    sends = len(miner.clock_sets)
    r.go(5, temp=88)                                              # 130 − 30 = 100: erlaubt
    assert miner.clock() == 100
    r.go(35, temp=89)                                             # 100 − 30 = 70: Fehler, nicht senden
    assert len(miner.clock_sets) == sends + 1 and r.st['clock_set'] == 100
    assert r.events('fehler')[0]['details']['grund'] == 'takt_unter_100'


def test_high_temperature_with_mode_change_clock_first_then_mode_then_clock(miner):
    ac = FakeAC(level=5)
    r = started(miner, ac, level=5, learned={'5': {'mhz': 390, 'temp_avg': 82, 'at': 'x', 'count': 1}})
    assert miner.level == 2 and miner.clock() == 390
    miner.clock_sets.clear()
    r.go(15, temp=88)                                             # 390 − 30 = 360 → mode 03 (Spielraum 20: bei 360 runter)
    assert miner.mode_sets[-1] == 3 and miner.level == 3
    assert miner.clock_sets[0] == pytest.approx(360, abs=1) and miner.clock() == 360
    assert len(miner.clock_sets) == 2                             # Takt, Modus, Takt erneut


# ---------------------------------------------------------------- Schritt 2: Korrekturphase und Rückkehr

def test_return_after_one_minute_in_band(miner):
    ac = FakeAC(level=5)
    r = started(miner, ac, level=5, learned={'5': {'mhz': 300, 'temp_avg': 82, 'at': 'x', 'count': 1}})
    _pulses(r, 85)
    r.go(5, temp=85)
    assert ac.level == 6
    r.go(55, temp=83)
    assert ac.level == 6
    r.go(15, temp=83)
    assert ac.level == 5 and r.st['phase'] == 'normal'
    assert r.events('rueckkehr')


def test_return_at_once_below_the_target_range(miner):
    """Nutzer 27.10.: Rückkehrbereich = ganzer Zielbereich (hier 79–84 °C) – sofort erst unter 79 °C (vorher 80 °C)."""
    ac = FakeAC(level=5)
    r = started(miner, ac, level=5, learned={'5': {'mhz': 300, 'temp_avg': 82, 'at': 'x', 'count': 1}})
    _pulses(r, 85)
    r.go(5, temp=85)
    r.go(5, temp=79)
    assert r.st['phase'] == 'korrektur'                           # 79 °C: im Rückkehrbereich, 1 min warten
    r.go(5, temp=78)
    assert r.st['phase'] == 'normal' and r.st['fan_target'] == 5


def test_no_plus_steps_in_correction_phase(miner):
    ac = FakeAC(level=5)
    r = started(miner, ac, level=5, learned={'5': {'mhz': 300, 'temp_avg': 82, 'at': 'x', 'count': 1}})
    _pulses(r, 86)
    r.go(5, temp=86)
    clock = miner.clock()
    r.go(50, temp=82)                                             # in 80-84, noch keine Minute: keine Plus-Schritte
    assert miner.clock() == clock and r.st['phase'] == 'korrektur'


# ---------------------------------------------------------------- Schritt 3: Feintuning (Kapitel 6)

def test_no_clock_change_between_79_and_84(miner):
    """Nutzer 25.10.: zwischen 79 und 84 °C keine Taktänderung – auch nicht bei steigender Tendenz (82 → 83 → 84)."""
    ac = FakeAC(level=5)
    r = started(miner, ac, level=5, learned={'5': {'mhz': 300, 'temp_avg': 82, 'at': 'x', 'count': 1}})
    sets, n = list(miner.clock_sets), len(r.events('takt'))
    for temp in (82, 83, 84, 83, 81, 80, 79, 80, 84):
        r.go(60, temp=temp)
    assert miner.clock_sets == sets and miner.clock() == 300 and len(r.events('takt')) == n

def test_fine_plus_steps_by_temperature(miner):
    """Kleinster Taktschritt 5 MHz (Nutzer 25.10.): unter 79 °C +5 (70–78), +20 (60–69), +40 (unter 60) – nach 60 s
    unter 79 °C (Nutzer 28.10.)."""
    for temp, step in ((79, 0), (78, 5), (76, 5), (72, 5), (65, 20), (55, 40)):
        miner.clock_sets.clear()
        ac = FakeAC(level=5)
        r = started(miner, ac, level=5, tmax=temp, learned={'5': {'mhz': 200, 'temp_avg': 82, 'at': 'x', 'count': 1}})
        r.go(55, temp=temp)
        assert miner.clock() == 200 + step, temp

def test_no_plus_step_while_rising(miner):
    ac = FakeAC(level=5)
    r = started(miner, ac, level=5, tmax=70, learned={'5': {'mhz': 200, 'temp_avg': 82, 'at': 'x', 'count': 1}})
    r.go(40, temp=lambda r: 70 + int((r.now - T0).total_seconds() // 10))
    assert miner.clock() == 200


def test_plus_step_stays_5_after_strong_effect(miner):
    """Die Regel „eine Größe kleiner“ entfällt: kleiner als 5 MHz gibt es nicht mehr."""
    ac = FakeAC(level=5)
    r = started(miner, ac, level=5, tmax=72, learned={'5': {'mhz': 200, 'temp_avg': 82, 'at': 'x', 'count': 1}})
    r.go(55, temp=72)                                             # nach 60 s unter 79 °C (Nutzer 28.10.)
    assert miner.clock() == 205
    r.go(30, temp=lambda r: 74 if r.now < T0 + timedelta(seconds=100) else 73)   # +2 °C nach dem Schritt
    r.go(10, temp=73)
    assert miner.clock() == 210

# ---------------------------------------------------------------- Modus (Kapitel 8)

def test_mode_up_at_361_and_down_only_at_340(miner):
    ac = FakeAC(level=5)
    r = started(miner, ac, level=5, tmax=78, learned={'5': {'mhz': 358, 'temp_avg': 82, 'at': 'x', 'count': 1}})
    assert miner.level == 4
    r.go(120, temp=78)                                           # +5 → 363 → mode 03 zuerst
    assert miner.level == 3 and miner.clock() == 363
    st = r.st
    st['clock_set'] = 352
    assert fa.required_level(341, 3) == 3 and fa.required_level(340, 3) == 4     # Spielraum 20 MHz (Nutzer 26.10.)
    assert fa.required_level(361, 4) == 3 and fa.required_level(360, 4) == 4
    assert fa.required_level(360, 2) == 3 and fa.required_level(361, 2) == 2
    assert fa.required_level(400, 0) == 1 and fa.required_level(401, 0) == 0
    assert fa.table_level(360) == 4 and fa.table_level(361) == 3 and fa.table_level(440) == 0


def test_clock_capped_at_440(miner):
    ac = FakeAC(level=5)
    r = started(miner, ac, level=5, tmax=55, learned={'5': {'mhz': 430, 'temp_avg': 82, 'at': 'x', 'count': 1}})
    r.go(115, temp=55)
    assert r.st['clock_set'] == 440 and miner.level == 0


# ---------------------------------------------------------------- Schritt 5: Ausnahme

def test_exception_lowers_fan_stepwise_after_3_minutes(miner):
    ac = FakeAC(level=5)
    r = started(miner, ac, level=5, tmax=82, learned={'5': {'mhz': 440, 'temp_avg': 82, 'at': 'x', 'count': 1}})
    r.go(170, temp=60)                                           # warm gestartet (kein Heizmodus), dann kühl
    assert ac.level == 5
    r.go(15, temp=60)
    assert ac.level == 4 and r.st['phase'] == 'ausnahme'
    r.go(185, temp=60)
    assert ac.level == 3
    r.go(10, temp=72)                                            # über 70: Ende, Stufe bleibt reduziert
    assert r.st['phase'] == 'normal' and ac.level == 3 and r.events('ausnahme_ende')
    r.go(5, temp=85)                                             # zurück nur über „Hohe Chiptemperaturen“
    assert ac.level == 6


# ---------------------------------------------------------------- Lernen (Kapitel 7)

def test_learns_after_15_minutes_unchanged(miner):
    ac = FakeAC(level=4)
    r = started(miner, ac, level=4, learned={'4': {'mhz': 280, 'temp_avg': 82, 'at': 'x', 'count': 1}})
    r.go(15 * 60 + 30, temp=82)
    lw = r.st['learned']['4']
    assert lw['mhz'] == 280 and lw['count'] == 2 and lw['temp_avg'] == 82.0
    assert lw['ths_avg'] == 150.0                                 # Ø Hashrate der 15 min (Nutzer 28.10.)
    assert lw['j_th'] == 13.3                                     # Effizienz: 2000 W / 150 TH/s
    assert r.events('lernwert')


def test_learned_value_never_above_the_cap(miner):
    """Review 28.10.: die Obergrenze senkt den Takt nicht – ein später gelernter Wert wird trotzdem auf sie begrenzt."""
    ac = FakeAC(level=4)
    r = started(miner, ac, level=4, learned={'4': {'mhz': 280, 'temp_avg': 82, 'at': 'x', 'count': 1}})
    r.st['caps'] = {'4': 270}                                     # Takt bleibt 280
    r.go(15 * 60 + 30, temp=82)
    assert r.st['clock_set'] == 280 and r.st['learned']['4']['mhz'] == 270.0


def test_learning_in_band_mean_clock_is_stored(miner):
    """15 min in 80–84 °C ohne Taktänderung: Lernwert = Mittel des gesetzten Takts."""
    ac = FakeAC(level=4)
    r = started(miner, ac, level=4, learned={'4': {'mhz': 280, 'temp_avg': 82, 'at': 'x', 'count': 1}})
    r.go(300, temp=82)
    r.go(625, temp=83)
    lw = r.st['learned']['4']
    assert lw['count'] == 2 and lw['mhz'] == 280

def test_short_dip_below_79_keeps_learning_long_one_restarts(miner):
    """Lernbereich 79–84 °C (Nutzer 25.10.): 79 °C gehört dazu, Ausflüge darunter bis 60 s brechen nicht ab."""
    ac = FakeAC(level=4)
    r = started(miner, ac, level=4, learned={'4': {'mhz': 280, 'temp_avg': 82, 'at': 'x', 'count': 1}})
    r.go(60, temp=82)
    since = r.st['learn_since']
    r.go(120, temp=79)                                            # 79 °C: im Lernbereich
    r.go(30, temp=78)                                             # 30 s Ausflug: Lernperiode läuft weiter
    r.go(30, temp=82)
    assert r.st['learn_since'] == since
    r.go(70, temp=78)                                             # länger als 60 s: neu
    assert r.st['learn_since'] is None and r.events('lernen_ende')[-1]['details']['ergebnis'] == 'abgebrochen'


def test_fan_change_restarts_learning(miner):
    ac = FakeAC(level=4)
    r = started(miner, ac, level=4, learned={'4': {'mhz': 280, 'temp_avg': 82, 'at': 'x', 'count': 1}})
    r.go(60, temp=82)
    r.go(5, temp=85)                                              # hohe Chiptemperatur → Lüfter + 1
    assert r.st['learn_since'] is None


# ---------------------------------------------------------------- Situationen

def test_firmware_clock_change_is_sent_again(miner):
    ac = FakeAC(level=5)
    r = started(miner, ac, level=5, learned={'5': {'mhz': 250, 'temp_avg': 82, 'at': 'x', 'count': 1}})
    r.go(60, temp=82)
    miner.points = list(STOCK['2/L4'])                           # Miner fährt wieder seinen Standardtakt
    r.go(10, temp=82)
    assert miner.clock() == 250 and r.events('firmware_takt')


def test_restart_of_the_miner_starts_again(miner):
    ac = FakeAC(level=5)
    r = started(miner, ac, level=5, learned={'5': {'mhz': 250, 'temp_avg': 82, 'at': 'x', 'count': 1}})
    r.go(60, temp=82)
    miner.elapsed = 10
    miner.points = list(STOCK['2/L4'])
    r.go(10, temp=82)
    assert r.events('neustart') and r.events('start')[-1]['trigger'] == 'Neustart des Miners'
    assert miner.clock() == 250


def test_restart_caused_by_own_mode_switch_is_not_a_restart(miner):
    ac = FakeAC(level=4)
    r = started(miner, ac, level=4, learned={'4': {'mhz': 390, 'temp_avg': 82, 'at': 'x', 'count': 1}})
    assert not r.events('neustart')


def test_calibration_run_no_clock_fan_only_for_high_temperature(miner):
    ac = FakeAC(level=5)
    r = started(miner, ac, level=5, learned={'5': {'mhz': 250, 'temp_avg': 82, 'at': 'x', 'count': 1}})
    sends = len(miner.clock_sets)
    r.go(60, temp=70, cal=True)
    assert len(miner.clock_sets) == sends and r.st['phase'] != 'korrektur'
    r.go(5, temp=88)
    assert ac.level == 7 and len(miner.clock_sets) == sends      # Lüfter reagiert, kein Takt
    r.go(10, temp=82, cal=False)
    assert r.events('messung_ende') and r.events('start')[-1]['trigger'] == 'nach Takt-Messung'


def test_over_hot_no_decision_then_start(miner):
    ac = FakeAC(level=5)
    r = started(miner, ac, level=5, learned={'5': {'mhz': 250, 'temp_avg': 82, 'at': 'x', 'count': 1}})
    miner.status = 'Over Hot'
    sends = len(miner.clock_sets)
    r.go(30, temp=95)
    assert len(miner.clock_sets) == sends and r.events('over_hot')
    miner.status = 'Work: In Work'
    r.go(10, temp=80)
    assert r.events('start')[-1]['trigger'] == 'nach Over Hot'


def test_miner_offline_fan_unchanged(miner):
    ac = FakeAC(level=5)
    r = started(miner, ac, level=5)
    miner.offline = True
    writes = list(ac.writes)
    r.go(30)
    assert ac.writes == writes and len(r.events('miner_offline')) == 1
    assert r.j.cycles[-1]['decision'] == 'Miner antwortet nicht'


def test_automation_off_sends_no_clock(miner):
    ac = FakeAC(level=5)
    r = started(miner, ac, level=5)
    sends = len(miner.clock_sets)
    r.go(30, enabled=False)
    assert len(miner.clock_sets) == sends and r.st['phase'] == 'aus' and r.st['off']


def test_fan_error_retried_until_it_works(miner):
    ac = FakeAC(level=5)
    r = started(miner, ac, level=5, learned={'5': {'mhz': 250, 'temp_avg': 82, 'at': 'x', 'count': 1}})
    _pulses(r, 86)
    ac.fail = True
    r.go(5, temp=86)
    assert ac.level == 5 and r.st['ac_error']
    r.go(25, temp=86)
    ac.fail = False
    r.go(25, temp=86)
    assert ac.level == 6
    tries = r.events('luefter')
    assert [t['ok'] for t in tries][-2:] == [False, True] or tries[-1]['ok']


def test_maintenance_fan_off_and_back(miner):
    ip = '10.0.0.15'
    ac = FakeAC(level=2)
    r = started(miner, ac, level=2)
    miner_cache.put(ip, 'estats', 'x')
    maint = (r.now - timedelta(seconds=1)).isoformat()
    r.go(5, maintenance_at=maint)
    assert ac.power == []
    miner_cache.clear(ip)
    r.go(10)
    assert ac.power == [('aus', None)] and r.st['phase'] == 'wartung'
    miner_cache.put(ip, 'estats', 'x')
    r.go(5)
    assert ac.power[-1] == ('an', 2) and r.st.get('maint_end') and r.st.get('maint_ended_at')
    miner_cache.clear(ip)


# ---------------------------------------------------------------- Protokoll

def test_every_round_is_recorded(miner):
    ac = FakeAC(level=5)
    r = started(miner, ac, level=5)
    r.go(60, temp=82)
    assert len(r.j.cycles) == 14
    c = r.j.cycles[-1]
    assert c['chip_temp'] == 82 and c['clock_set'] == 400 and c['base_level'] == 5 and c['mode_key'] == '2/L2'


def test_thermal_response_10_values_every_30_s(miner):
    ac = FakeAC(level=5)
    r = started(miner, ac, level=5)
    r.go(310, temp=82)
    takt = r.events('takt')[0]
    assert len(takt['thermal']) == 10 and takt['takeover_state'] == 'gemessen'
    assert takt['api_ms'] is not None and takt['request'].startswith('ascset 0,frequency,')


def test_mode_command_never_logs_pool_credentials(miner):
    ac = FakeAC(level=4)
    r = started(miner, ac, level=4, learned={'4': {'mhz': 390, 'temp_avg': 82, 'at': 'x', 'count': 1}})
    modus = r.events('modus')[0]
    assert 'passwd' not in modus['request'] and modus['request'].startswith('mode=2, power_level=02')


# ---------------------------------------------------------------- Simulation (Schätzmodell, keine Messwerte)

def _sim(level, room=0.0, hours=2.0, start_clock=200):
    """Annahmen: Gleichgewicht 60 °C + 2 °C je 10 MHz über 300 − 10 °C je Lüfterstufe über 5 + Raum (vorher über 200;
    seit dem 28.10. liegt so der Initialwert der Stufe 5, 400 MHz, bei 80 °C), Zeitkonstante 120 s, Messwert ganzzahlig."""
    import math
    mn = Miner(tmax=70, points=fa.points_for(STOCK['2/L4'], start_clock))
    ac = FakeAC(level=level)
    calibration.read_miner, calibration.set_frequency, calibration.a15_set_mode = mn.read, mn.set_frequency, mn.set_mode
    r = Run(mn, ac, level)
    temp = [75.0]
    peak = [0]

    def model(run):
        eq = 60 + 2 * (mn.clock() - 300) / 10 - 10 * (ac.level - 5) + room(run) if callable(room) else \
            60 + 2 * (mn.clock() - 300) / 10 - 10 * (ac.level - 5) + room
        temp[0] += (eq - temp[0]) * (1 - math.exp(-5 / 120))
        peak[0] = max(peak[0], temp[0])
        return int(round(temp[0]))
    r.go(hours * 3600, temp=model)
    return r, peak[0], mn


def test_simulation_reaches_band_and_stays_safe(miner):
    r, peak, mn = _sim(5)
    assert peak < 86
    band = [c for c in r.j.cycles[-360:] if 79 <= (c['chip_temp'] or 0) <= 84]
    assert len(band) > 300                                       # letzte 30 min fast nur im Zielbereich (79–84 °C)
    assert r.st['learned'].get('5')


def test_simulation_room_plus_15_corrects(miner):
    r, peak, mn = _sim(5, room=lambda run: 15 if run.now >= T0 + timedelta(hours=1) else 0)
    assert peak < 88 and r.st['fan_target'] == 5


# ---------------------------------------------------------------- Befunde der Prüfung (25.10.)

def test_start_after_restart_even_with_invalid_temperature(miner):
    ac = FakeAC(level=5)
    r = started(miner, ac, level=5, learned={'5': {'mhz': 250, 'temp_avg': 82, 'at': 'x', 'count': 1}})
    r.go(30, temp=82)
    miner.elapsed = 10
    miner.points = list(STOCK['2/L4'])
    r.go(5, temp=-273)                                            # booting: TMax noch ungültig
    assert r.events('start')[-1]['trigger'] == 'Neustart des Miners' and miner.clock() == 250


def test_alternating_84_85_corrects_at_most_once_per_window(miner):
    ac = FakeAC(level=5)
    r = started(miner, ac, level=5, learned={'5': {'mhz': 300, 'temp_avg': 82, 'at': 'x', 'count': 1}})
    r.go(30, temp=lambda r: 85 if int((r.now - T0).total_seconds() // 5) % 2 else 84)
    assert miner.clock() == 290                                   # nur die erste Korrektur, danach Fenster/Tendenz
    assert len(r.events('luefterstoss')) == 1                     # Stoß beim ersten 85, beim zweiten gleich korrigiert


def test_entering_band_from_below_is_no_minus_step(miner):
    ac = FakeAC(level=5)
    r = started(miner, ac, level=5, tmax=78, learned={'5': {'mhz': 250, 'temp_avg': 82, 'at': 'x', 'count': 1}})
    r.go(115, temp=78)
    assert miner.clock() == 255
    r.go(40, temp=80)
    assert miner.clock() == 255                                   # 78 → 80 ist das Erreichen des Ziels, kein −

def test_phase_messung_during_calibration(miner):
    ac = FakeAC(level=5)
    r = started(miner, ac, level=5)
    r.go(10, temp=82, cal=True)
    assert r.st['phase'] == 'messung' and r.j.cycles[-1]['phase'] == 'messung'


def test_superseded_command_is_not_measured(miner):
    ac = FakeAC(level=5)
    r = started(miner, ac, level=5, learned={'5': {'mhz': 300, 'temp_avg': 82, 'at': 'x', 'count': 1}})
    miner.ignore_clock = True
    r.go(40, temp=82)
    r.go(65, temp=77)                                             # +5 gesendet (nach 60 s unter 79 °C), Miner übernimmt nicht
    r.go(25, temp=77)                                             # nach 20 s erneut gesendet
    takt = [e for e in r.events('takt') if e.get('clock_new') == 305]
    assert len(takt) >= 2 and takt[0]['takeover_state'] == 'nicht gemessen' and takt[-1]['attempt'] == 2


def test_switch_to_new_logic_keeps_maintenance_and_does_not_loop(miner):
    ip = '10.0.0.15'
    ac = FakeAC(level=2)
    maint = (T0 - timedelta(hours=1)).isoformat()
    old = {'maint': {'since': maint, 'seen_off': True, 'fan_off': True, 'fan_before': 2}, 'fan_set': 2,
           'phase': 'wartung', 'learned': {'2/L4': {'2': {'mhz': 241}}}}
    miner_cache.clear(ip)
    r = Run(miner, ac, level=2, state=old)
    r.go(30, maintenance_at=maint)                                # A15 aus
    assert ac.power == [] and r.st['v'] == fa.STATE_VERSION       # Lüfter war schon aus: kein neuer Befehl
    assert len([n for n in r.st['log'] if 'Automatik ein' in n[1]]) == 1
    miner_cache.put(ip, 'estats', 'x')
    r.go(5)
    assert ac.power[-1] == ('an', 2) and r.st.get('maint_end')
    miner_cache.clear(ip)


def test_missing_stock_clock_is_estimated_and_never_blocks(miner):
    ac = FakeAC(level=5)
    miner.level, miner.points = 1, list(STOCK['2/L1'])           # mode 01: kein Profil, kein gemerkter Standardtakt
    r = started(miner, ac, level=5, learned={'5': {'mhz': 410, 'temp_avg': 82, 'at': 'x', 'count': 1}})
    assert miner.clock() == 410
    _pulses(r, 86)
    r.go(5, temp=86)                                              # kurz in 79–84 °C: −10
    assert miner.clock() == 400 and r.events('fehler')[0]['details']['grund'] == 'standardtakt_fehlt'


def test_stock_is_taken_only_after_the_firmware_applied_it(miner, monkeypatch):
    ac = FakeAC(level=4)

    def slow_mode(ip, conf, mode, level):                         # Webseite meldet den Modus, SF0 noch alt
        miner.mode_sets.append(level)
        miner.level = level
    monkeypatch.setattr(calibration, 'a15_set_mode', slow_mode)
    r = started(miner, ac, level=4, learned={'4': {'mhz': 390, 'temp_avg': 82, 'at': 'x', 'count': 1}})
    assert '2/L2' not in (r.st.get('stock') or {}) and miner.clock_sets == []   # wartet auf den Standardtakt
    miner.points = list(STOCK['2/L2'])                            # jetzt hat die Firmware ihn eingestellt
    r.go(10, temp=82)
    assert r.st['stock']['2/L2'] == STOCK['2/L2'] and miner.clock() == 390


def test_fan_read_error_retried_only_after_60_s(miner):
    calls = []
    ac = FakeAC(level=5)

    def broken(dev_id, port):
        calls.append(1)
        raise ac.ACError('Timeout')
    ac.speed_of = broken
    r = started(miner, ac, level=5)
    r.go(55, temp=82)
    assert len(calls) == 1


def test_firmware_ramp_after_a_clock_change_is_no_override(miner, monkeypatch):
    """25.10. live: 183 → 100 MHz, die Firmware meldet erst 152, dann 123 – das ist die Rampe, kein Überschreiben."""
    ac = FakeAC(level=1)
    ramp = []

    def ramping(ip, port, points):
        miner.clock_sets.append(sum(points) / len(points))
        ramp.extend([[p + 52 for p in points], [p + 23 for p in points], list(points)])
        return 'set OK'
    monkeypatch.setattr(calibration, 'set_frequency', ramping)
    orig = miner.read

    def read(ip, port, fresh=False):
        if ramp:
            miner.points = ramp.pop(0)
        return orig(ip, port, fresh)
    monkeypatch.setattr(calibration, 'read_miner', read)
    miner.points = fa.points_for(STOCK['2/L4'], 183)
    r = started(miner, ac, level=1)
    r.go(30, temp=70)
    assert [round(x) for x in miner.clock_sets].count(170) == 1 and not r.events('firmware_takt')   # Stufe 1 = 170



# ---------------------------------------------------------------- Unruhe an der 80-°C-Grenze (25.10.)

def test_calm_5_min_after_a_hot_reduction(miner):
    """Nutzer 25.10.: letzte Taktänderung über 84 °C, dann unter 79 °C → 5 min keine Taktänderung (ab dem ersten
    Unterschreiten), danach wieder Plus-Schritte."""
    ac = FakeAC(level=5)
    r = started(miner, ac, level=5, learned={'5': {'mhz': 300, 'temp_avg': 82, 'at': 'x', 'count': 1}})
    _pulses(r, 85)
    r.go(10, temp=85)
    assert r.st['clock_set'] == 290                               # ab 85 °C −10
    r.go(40, temp=82)
    since = r.now
    r.go(5, temp=78)
    ev = r.events('taktsperre')
    until = since + timedelta(minutes=5)
    assert len(ev) == 1 and ev[0]['trigger'].startswith('Taktsperre 5 min bis ' + until.astimezone(fa.LOCAL_TZ).strftime('%H:%M'))
    assert r.st['calm']['until'] == until.isoformat()
    r.go(5 * 60 - 5, temp=lambda r: 74 if r.now < since + timedelta(minutes=2) else 77)
    assert r.st['clock_set'] == 290                               # 5 min keine Taktänderung, auch bei 74 °C
    r.go(5, temp=77)
    assert r.st['clock_set'] == 295 and not r.st.get('calm')      # danach +5


def test_calm_ends_above_84(miner):
    ac = FakeAC(level=5)
    r = started(miner, ac, level=5, learned={'5': {'mhz': 300, 'temp_avg': 82, 'at': 'x', 'count': 1}})
    _pulses(r, 85)
    r.go(10, temp=85)
    r.go(40, temp=82)
    r.go(60, temp=78)
    assert r.st.get('calm')
    r.go(5, temp=85)                                              # keine 10 min seit den Stößen: gleich korrigiert
    assert not r.st.get('calm') and r.events('taktsperre_ende') and r.st['clock_set'] == 280   # wieder −10


def test_calm_only_once_per_hot_change_and_not_after_a_plus_step(miner):
    ac = FakeAC(level=5)
    r = started(miner, ac, level=5, learned={'5': {'mhz': 300, 'temp_avg': 82, 'at': 'x', 'count': 1}})
    _pulses(r, 85)
    r.go(10, temp=85)
    r.go(40, temp=82)
    r.go(60, temp=78)
    r.go(5 * 60, temp=80)                                         # Sperre läuft im Bereich ab
    assert not r.st.get('calm') and r.st['clock_set'] == 290
    r.go(65, temp=77)                                             # wieder unter 79: keine neue Sperre, +5 (nach 60 s)
    assert len(r.events('taktsperre')) == 1 and r.st['clock_set'] == 295
    r.go(185, temp=77)                                            # 75–78 °C: 3 min nach der letzten Erhöhung
    assert r.st['clock_set'] == 300 and len(r.events('taktsperre')) == 1   # letzte Änderung bei 78 °C: keine Sperre

# ---------------------------------------------------------------- Temperatur-Test (25.10., v2.0.6)

def _test_run(miner, ac):
    """Automatik auf Basisstufe 2 (Lernwert 319,2 MHz), dann Temperatur-Test gestartet."""
    r = started(miner, ac, level=2, learned={'2': {'mhz': 319.2, 'temp_avg': 82, 'at': 'x', 'count': 1}})
    r.kw['temp_test_at'] = r.now.isoformat()
    return r


def _measuring(r, step):
    """Bis Schritt `step` misst (Chiptemperatur 80 °C)."""
    for _ in range(2000):
        tt = r.st.get('test') or {}
        if tt.get('step') == step and tt.get('phase') == 'messen':
            return r
        r.go(5, temp=80)
    raise AssertionError('Schritt %d misst nicht' % step)


def test_temp_test_begins_at_learned_value_and_waits_for_80(miner):
    ac = FakeAC(level=2)
    r = _test_run(miner, ac)
    miner.points = fa.points_for(STOCK['2/L4'], 300)             # Takt steht woanders
    r.go(10, temp=83)
    assert r.st['clock_set'] == 319 and miner.clock() == 319
    assert r.st['test']['step'] == 0 and r.st['test']['phase'] == 'beginn'
    r.go(60, temp=81)
    assert r.st['test']['step'] == 0 and miner.clock() == 319     # über 80 °C: warten, keine Taktänderung
    r.go(5, temp=80)
    assert r.st['test']['step'] == 1 and r.st['clock_set'] == 349
    assert r.events('test_schritt')[0]['trigger'] == 'Temperatur-Test Schritt 1/2: Takt 349 MHz (+30 MHz)'


def test_temp_test_waits_2_min_after_takeover_then_measures_20_min(miner):
    ac = FakeAC(level=2)
    r = _test_run(miner, ac)
    r.go(15, temp=80)                                             # Beginn (Lernwert steht schon), Schritt 1 übernommen
    tt = r.st['test']
    took = r.now - timedelta(seconds=5)
    assert tt['step'] == 1 and tt['phase'] == 'warten'
    assert tt['wait_ends'] == (took + timedelta(minutes=2)).isoformat()
    sets = list(miner.clock_sets)
    r.go(115, temp=80)
    assert r.st['test']['phase'] == 'warten'
    r.go(5, temp=80)
    tt = r.st['test']
    ends = took + timedelta(minutes=22)
    assert tt['phase'] == 'messen' and tt['measure_ends'] == ends.isoformat()
    assert r.events('test_messung')[-1]['trigger'] == ('Temperatur-Test Schritt 1/2: Messung läuft bis '
                                                       + ends.astimezone(fa.LOCAL_TZ).strftime('%H:%M'))
    r.go(20 * 60 - 5, temp=84)                                    # 84 °C: keine Taktregelung im Test
    assert r.st['test']['step'] == 1 and miner.clock_sets == sets
    r.go(5, temp=84)
    assert r.st['test']['step'] == 2 and r.st['clock_set'] == 379


def test_temp_test_second_step_mode_first_then_clock_wait_after_takeover(miner):
    """379 MHz liegt in mode 03: erst Modus, dann Takt (allgemeine Regel); die 2 min beginnen nach der Übernahme des
    Takts nach dem Moduswechsel."""
    ac = FakeAC(level=2)
    r = _measuring(_test_run(miner, ac), 1)
    n = len(r.j.events)
    r.go(20 * 60 + 15, temp=80)
    kinds = [e['kind'] for e in r.j.events[n:] if e['kind'] in ('test_schritt', 'modus', 'takt')]
    assert kinds == ['test_schritt', 'modus', 'takt'] and miner.mode_sets == [3]
    r = _measuring(r, 2)
    takt = [e for e in r.j.events if e['kind'] == 'takt' and e.get('clock_new') == 379][-1]
    assert takt['takeover_state'] == 'gemessen'
    measure_from = datetime.fromisoformat(r.st['test']['measure_from'])
    assert measure_from - takt['takeover_at'] == timedelta(minutes=2)


def test_temp_test_finishes_then_start_with_learned_value(miner):
    ac = FakeAC(level=2)
    r = _measuring(_test_run(miner, ac), 2)
    r.go(20 * 60, temp=82)
    end = r.events('test_ende')[-1]
    assert end['details']['ergebnis'] == 'fertig' and 'test' not in r.st and r.st.get('test_clear')
    assert r.events('start')[-1]['trigger'] == 'nach Temperatur-Test' and r.st['clock_set'] == 319


def test_temp_test_aborts_at_96_then_high_temperature_rules(miner):
    ac = FakeAC(level=2)
    r = _test_run(miner, ac)
    r.go(15, temp=80)
    assert r.st['test']['step'] == 1
    r.go(5, temp=95)
    assert 'test' in r.st and ac.level == 2 and r.st['phase'] == 'normal'   # keine Lüfterkorrektur im Test
    r.go(5, temp=96)
    end = r.events('test_ende')[-1]
    assert end['details']['ergebnis'] == 'abgebrochen' and '96 °C' in end['trigger'] and 'test' not in r.st
    assert r.st['phase'] == 'korrektur' and r.st['fan_target'] == 4 and r.st['clock_set'] == 249


def test_temp_test_aborts_on_restart_of_the_miner(miner):
    ac = FakeAC(level=2)
    r = _test_run(miner, ac)
    r.go(15, temp=80)
    miner.elapsed = 10
    r.go(5, temp=80)
    assert r.events('test_ende')[-1]['details']['grund'] == 'Neustart des Miners' and 'test' not in r.st
    assert r.events('start')[-1]['trigger'] == 'Neustart des Miners' and r.st['clock_set'] == 319


def test_temp_test_aborts_on_new_base_level(miner):
    ac = FakeAC(level=2)
    r = _test_run(miner, ac)
    r.go(15, temp=80)
    r.kw['level_at'] = 'neu'
    r.go(5, temp=80)
    assert r.events('test_ende')[-1]['details']['grund'] == 'Basisstufe gewählt (Stufe 2)' and 'test' not in r.st


def test_temp_test_restart_or_base_level_before_the_test_began_does_not_abort(miner):
    """Review 25.10.: Neustart bzw. Basisstufe kurz vor dem Beginn (z. B. Test gestartet, während die A15 neu startet)
    bricht nicht ab – Start und Test-Beginn setzen denselben Lernwert."""
    ac = FakeAC(level=2)
    r = _test_run(miner, ac)
    miner.elapsed = 10
    r.kw['level_at'] = 'neu'
    r.go(10, temp=82)
    assert not r.events('test_ende') and r.st['test']['step'] == 0 and r.st['clock_set'] == 319


def test_temp_test_abort_reason_wins_over_the_end(miner):
    ac = FakeAC(level=2)
    r = _measuring(_test_run(miner, ac), 2)
    r.go(20 * 60 - 5, temp=82)
    r.go(5, temp=96)                                              # 96 °C genau in der letzten Runde
    end = r.events('test_ende')[-1]
    assert end['details']['ergebnis'] == 'abgebrochen' and not [e for e in r.events('start')
                                                                if e['trigger'] == 'nach Temperatur-Test']


def test_temp_test_no_learning_fan_stays_on_base_level(miner):
    ac = FakeAC(level=2)
    r = _test_run(miner, ac)
    r.go(20 * 60, temp=84)
    assert r.st['learned']['2']['count'] == 1 and not r.events('lernwert')
    assert r.st['test']['step'] == 0 and ac.level == 2


def test_temp_test_ended_by_user(miner):
    ac = FakeAC(level=2)
    r = _test_run(miner, ac)
    r.go(15, temp=80)
    r.go(10, temp=86)
    assert r.st['phase'] == 'normal'
    r.kw['temp_test_at'] = None
    r.go(5, temp=86)
    assert 'test' not in r.st and r.st['stoss']['n'] == 1         # 86 °C ist ohne Test wieder „hoch“: erst Lüfterstöße


def test_temp_test_state_before_v206_begins_new(miner):
    ac = FakeAC(level=2)
    r = _test_run(miner, ac)
    r.st['test'] = {'started': r.kw['temp_test_at'], 'stage': 2, 'stages': 5, 'target': 86, 'band': [85, 87],
                    'reached_at': None, 'stage_ends': None, 'ends': None}
    r.go(10, temp=83)
    assert r.st['test']['step'] == 0 and r.st['test']['phase'] == 'beginn' and not r.st.get('last_error')


def test_no_calm_during_takt_messung(miner):
    """Review 25.10.: während der Takt-Messung ist die Taktregelung pausiert – keine Taktsperre."""
    ac = FakeAC(level=5)
    r = started(miner, ac, level=5, learned={'5': {'mhz': 300, 'temp_avg': 82, 'at': 'x', 'count': 1}})
    _pulses(r, 85)
    r.go(10, temp=85)
    r.go(40, temp=82)
    r.kw['cal'] = True
    r.go(60, temp=72)
    assert not r.events('taktsperre') and not r.st.get('calm')


# ---------------------------------------------------------------- Absenkung nach Aufenthalt (Nutzer 25.10.)

def test_85_after_short_stay_in_band_minus_10(miner):
    ac = FakeAC(level=5)
    r = started(miner, ac, level=5, tmax=75, learned={'5': {'mhz': 300, 'temp_avg': 82, 'at': 'x', 'count': 1}})
    r.go(60, temp=75)                                             # unter 79 °C
    clock = r.st['clock_set']
    r.go(4 * 60, temp=82)                                         # 4 min in 79–84 °C (Grenze 5 min, Nutzer 26.10.)
    _pulses(r, 85)
    r.go(5, temp=85)
    assert r.st['clock_set'] == clock - 10 and ac.level == 6
    takt = r.events('takt')[-1]
    assert takt['trigger'] == 'Chiptemperatur 85 °C nach 4 min in 79–84 °C: Takt −10 MHz'
    assert r.events('hohe_temperatur')[-1]['details']['aufenthalt_s'] == 240


def test_85_and_86_after_long_stay_minus_5_for_the_whole_correction_phase(miner):
    """Aufenthalt ab 5 min → −5; gilt für jede Absenkung bei 85/86 °C bis zum Ende der Korrekturphase, auch nach
    einem kurzen Rückgang auf 84 °C."""
    ac = FakeAC(level=5)
    r = started(miner, ac, level=5, learned={'5': {'mhz': 300, 'temp_avg': 82, 'at': 'x', 'count': 1}})
    r.go(11 * 60, temp=82)
    _pulses(r, 85)
    r.go(5, temp=85)
    assert r.st['clock_set'] == 295                               # −5
    r.go(35, temp=85)
    assert r.st['clock_set'] == 290                               # nicht fallend: wieder −5
    r.go(10, temp=84)                                             # kurz darunter, Korrekturphase läuft weiter
    r.go(30, temp=86)
    assert r.st['clock_set'] == 285 and r.st['phase'] == 'korrektur'   # 86 °C: auch −5 (vorher −20)
    r.go(35, temp=88)
    assert r.st['clock_set'] == 255                               # ab 88 °C Tabelle −30


def test_jump_from_below_79_to_85_counts_as_no_stay(miner):
    ac = FakeAC(level=5)
    r = started(miner, ac, level=5, tmax=75, learned={'5': {'mhz': 300, 'temp_avg': 82, 'at': 'x', 'count': 1}})
    r.go(60, temp=75)
    clock = r.st['clock_set']
    _pulses(r, 85)
    r.go(5, temp=85)
    assert r.st['clock_set'] == clock - 10


def test_new_measurement_after_the_correction_phase(miner):
    ac = FakeAC(level=5)
    r = started(miner, ac, level=5, learned={'5': {'mhz': 300, 'temp_avg': 82, 'at': 'x', 'count': 1}})
    r.go(11 * 60, temp=82)
    _pulses(r, 85)
    r.go(5, temp=85)
    assert r.st['clock_set'] == 295                               # 11 min → −5
    r.go(70, temp=82)                                             # 1 min stabil: Rückkehr, Korrekturphase endet
    assert r.st['phase'] == 'normal'
    r.go(2 * 60, temp=83)
    r.go(5, temp=85)                                              # jetzt nur ~3 min in 79–84 °C → −10 (keine Stöße: < 10 min)
    assert r.st['clock_set'] == 285



# ---------------------------------------------------------------- Startpause (Nutzer 25.10.)

def test_start_hold_after_restart_until_79(miner):
    """25.10. live: nach dem Neustart meldete der Miner 26 °C, die Regelung setzte +40 MHz → 85 °C; danach wurde er
    bei 253 MHz nur ~1 °C/min wärmer. Startpause bis 79 °C, höchstens 10 min (Nutzer 26.10.)."""
    ac = FakeAC(level=5)
    r = started(miner, ac, level=5, learned={'5': {'mhz': 300, 'temp_avg': 82, 'at': 'x', 'count': 1}})
    r.go(30, temp=82)
    miner.elapsed = 10                                            # Neustart
    start = r.now
    r.go(5, temp=26)
    ev = r.events('startpause')
    until = start + timedelta(minutes=10)
    assert len(ev) == 1 and ev[0]['trigger'] == ('Startpause: keine Takterhöhung bis 79 °C, spätestens %s'
                                                  % until.astimezone(fa.LOCAL_TZ).strftime('%H:%M'))
    assert ev[0]['details']['grund'] == 'Neustart des Miners'
    r.go(60, temp=26)
    r.go(6 * 60, temp=77)
    assert r.st['clock_set'] == 300                               # langsam warm: keine Takterhöhung
    r.go(5, temp=79)
    assert not r.st.get('start_hold')                             # 79 °C erreicht: Pause vorbei
    assert r.events('startpause_ende')[-1]['trigger'] == 'Startpause beendet: 79 °C erreicht (nach 7 min)'
    r.go(65, temp=77)
    assert r.st['clock_set'] == 305                               # danach wie immer (nach 60 s unter 79 °C)


def test_start_hold_ends_after_10_min_at_the_latest(miner):
    ac = FakeAC(level=5)
    r = started(miner, ac, level=5, learned={'5': {'mhz': 300, 'temp_avg': 82, 'at': 'x', 'count': 1}})
    r.go(30, temp=82)
    miner.elapsed = 10
    r.go(10 * 60 - 5, temp=70)
    assert r.st['clock_set'] == 300
    r.go(10, temp=70)
    assert r.st['clock_set'] == 305 and not r.st.get('start_hold')
    assert r.events('startpause_ende')[-1]['trigger'] == 'Startpause beendet: 10 min vorbei (Chiptemperatur 70 °C)'


def test_start_hold_keeps_high_temperature_rules(miner):
    ac = FakeAC(level=5)
    r = started(miner, ac, level=5, learned={'5': {'mhz': 300, 'temp_avg': 82, 'at': 'x', 'count': 1}})
    r.go(30, temp=82)
    miner.elapsed = 10
    r.go(30, temp=70)
    _pulses(r, 85)
    r.go(5, temp=85)
    assert r.st['clock_set'] == 290 and r.st['phase'] == 'korrektur'   # Absenkung ab 85 °C gilt weiter


def test_start_hold_after_over_hot(miner):
    ac = FakeAC(level=5)
    r = started(miner, ac, level=5, learned={'5': {'mhz': 300, 'temp_avg': 82, 'at': 'x', 'count': 1}})
    r.go(30, temp=82)
    miner.status = 'Over Hot'
    r.go(10, temp=82)
    miner.status = 'Work: In Work'
    r.go(5, temp=40)
    assert r.events('startpause') and r.events('start')[-1]['trigger'] == 'nach Over Hot'
    r.go(60, temp=40)
    assert r.st['clock_set'] == 300


def test_no_start_hold_when_base_level_is_chosen(miner):
    """Chips warm: Basisstufe gewählt, Automatik ein, nach Takt-Messung – keine Startpause."""
    ac = FakeAC(level=5)
    r = started(miner, ac, level=5, learned={'5': {'mhz': 300, 'temp_avg': 82, 'at': 'x', 'count': 1}})
    r.go(30, temp=82)
    r.kw['level_at'] = 'neu'
    r.go(125, temp=72)
    assert not r.events('startpause') and r.st['clock_set'] > 300


def test_start_hold_also_when_another_start_reason_follows_in_the_same_round(miner):
    """Review 25.10.: Neustart erkannt und in derselben Runde Basisstufe gewählt (z. B. während das MinerGerät
    offline war) – die Chips sind trotzdem kalt: Startpause."""
    ac = FakeAC(level=5)
    r = started(miner, ac, level=5, learned={'5': {'mhz': 300, 'temp_avg': 82, 'at': 'x', 'count': 1}})
    r.go(30, temp=82)
    miner.elapsed = 10
    r.kw['level_at'] = 'neu'
    r.go(5, temp=26)
    assert r.events('start')[-1]['trigger'].startswith('Basisstufe gewählt') and r.events('startpause')
    r.go(60, temp=26)
    assert r.st['clock_set'] == 300


# ---------------------------------------------------------------- Neustart sauber anzeigen (Nutzer 25.10.)

def test_restart_by_button_offline_is_expected(miner):
    ac = FakeAC(level=5)
    r = started(miner, ac, level=5, learned={'5': {'mhz': 300, 'temp_avg': 82, 'at': 'x', 'count': 1}})
    r.go(30, temp=82)
    r.kw['restart_req_at'] = r.now.isoformat()                    # Knopf „A15 neu starten“
    r.go(5, temp=82)
    assert r.st['restart_req']
    miner.offline = True
    r.go(30)
    assert r.st['offline'] == 'neustart' and not r.events('miner_offline')
    assert r.events('neustart_offline')[0]['trigger'] == 'A15 startet neu – nicht erreichbar (erwartet)'
    miner.offline, miner.elapsed = False, 10
    r.go(5, temp=26)
    assert r.events('neustart_online')[0]['trigger'] == 'A15 nach Neustart wieder erreichbar nach 30 s'
    assert r.events('start')[-1]['trigger'] == 'Neustart des Miners' and not r.st.get('restart_req')


def test_restart_another_way_relabels_offline_of_the_last_3_min(miner):
    """Neustart über die Einstellungsseite oder Strom: erst an der Laufzeit erkannt, dann werden „antwortet nicht/
    wieder“ der 3 min davor zu Neustart-Einträgen (nicht mehr rot)."""
    ac = FakeAC(level=5)
    r = started(miner, ac, level=5, learned={'5': {'mhz': 300, 'temp_avg': 82, 'at': 'x', 'count': 1}})
    r.go(30, temp=82)
    miner.offline = True
    r.go(30)
    miner.offline = False
    r.go(15, temp=82)
    assert r.events('miner_offline') and r.events('miner_online')
    miner.elapsed = 10
    r.go(5, temp=26)
    assert not r.events('miner_offline') and not r.events('miner_online')
    assert r.events('neustart_offline')[0]['ok'] is None
    assert r.events('neustart_online')[0]['trigger'] == 'A15 nach Neustart wieder erreichbar nach 30 s'
    miner.offline = True                                          # während der Startpause: Teil des Neustarts
    r.go(20)
    assert len(r.events('neustart_offline')) == 2 and not r.events('miner_offline')


def test_offline_without_restart_stays_an_error(miner):
    ac = FakeAC(level=5)
    r = started(miner, ac, level=5, learned={'5': {'mhz': 300, 'temp_avg': 82, 'at': 'x', 'count': 1}})
    r.go(30, temp=82)
    miner.offline = True
    r.go(30)
    miner.offline = False
    r.go(4 * 60, temp=82)                                         # kein Neustart in den 3 min danach
    miner.elapsed = 10
    r.go(5, temp=26)
    assert r.events('miner_offline') and r.events('miner_offline')[0]['ok'] is False


def test_firmware_resend_waits_60_s_while_the_firmware_ramps(miner, monkeypatch):
    """26.10. live: nach dem Hochfahren setzte die Firmware 360 MHz und senkte dann schrittweise; die Regelung sandte
    in 10 s dreimal neu. Nach einem erneuten Senden frühestens nach 60 s wieder."""
    ac = FakeAC(level=5)
    r = started(miner, ac, level=5, learned={'5': {'mhz': 253, 'temp_avg': 82, 'at': 'x', 'count': 1}})
    r.go(70, temp=82)

    def ramping(ip, port, points):                                # Firmware stellt den Takt nur schrittweise ein
        miner.clock_sets.append(sum(points) / len(points))
        miner.points = [p + 60 for p in points]
        return 'STATUS=S,Msg=ASC 0 set OK'
    monkeypatch.setattr(calibration, 'set_frequency', ramping)
    miner.points = list(STOCK['2/L4'])                            # Firmware setzt den Standardtakt
    r.go(5, temp=82)
    assert len(r.events('firmware_takt')) == 1
    r.go(50, temp=82)
    assert len(r.events('firmware_takt')) == 1                    # Takt laut Miner noch hoch: aber erst nach 60 s
    r.go(15, temp=82)
    assert len(r.events('firmware_takt')) == 2


def test_restart_entries_carry_no_red_error_text(miner):
    """Review 26.10.: die Protokoll-Seite zeigt error immer rot – bei Neustart-Einträgen steht die Meldung in details."""
    ac = FakeAC(level=5)
    r = started(miner, ac, level=5, learned={'5': {'mhz': 300, 'temp_avg': 82, 'at': 'x', 'count': 1}})
    r.go(30, temp=82)
    miner.offline = True
    r.go(30)
    miner.offline, miner.elapsed = False, 10
    r.go(5, temp=26)
    off = r.events('neustart_offline')[0]
    assert not off.get('error') and off['details']['meldung'] == 'timed out'
    miner.offline = True                                          # während der Startpause
    r.go(20)
    off = r.events('neustart_offline')[-1]
    assert not off.get('error') and off['details']['meldung'] == 'timed out'


def test_expected_offline_becomes_an_error_after_5_min(miner):
    """Review 26.10.: Knopf-Neustart, die A15 kommt nicht zurück – nach 5 min wie bisher „Miner antwortet nicht“."""
    ac = FakeAC(level=5)
    r = started(miner, ac, level=5, learned={'5': {'mhz': 300, 'temp_avg': 82, 'at': 'x', 'count': 1}})
    r.go(30, temp=82)
    r.kw['restart_req_at'] = r.now.isoformat()
    r.go(5, temp=82)
    miner.offline = True
    r.go(4 * 60)
    assert r.st['offline'] == 'neustart' and not r.events('miner_offline')
    r.go(60)
    assert r.st['offline'] is True and r.events('miner_offline')[0]['ok'] is False


def test_button_restart_does_not_relabel_entries_before_the_click(miner):
    ac = FakeAC(level=5)
    r = started(miner, ac, level=5, learned={'5': {'mhz': 300, 'temp_avg': 82, 'at': 'x', 'count': 1}})
    r.go(30, temp=82)
    miner.offline = True                                          # Zeitüberschreitung ohne Neustart
    r.go(10)
    miner.offline = False
    r.go(60, temp=82)
    r.kw['restart_req_at'] = r.now.isoformat()                    # dann der Knopf
    r.go(5, temp=82)
    miner.offline = True
    r.go(30)
    miner.offline, miner.elapsed = False, 10
    r.go(5, temp=26)
    assert len(r.events('miner_offline')) == 1 and r.events('miner_offline')[0]['ok'] is False
    assert len(r.events('neustart_offline')) == 1


def test_no_start_hold_after_takt_messung_with_mode_switch_restart(miner):
    """Review 26.10.: Moduswechsel der Takt-Messung setzen die Laufzeit zurück – danach keine Startpause (Chips warm)."""
    ac = FakeAC(level=5)
    r = started(miner, ac, level=5, learned={'5': {'mhz': 300, 'temp_avg': 82, 'at': 'x', 'count': 1}})
    r.go(30, temp=82)
    r.kw['cal'] = True
    r.go(10, temp=83)
    miner.elapsed = 10
    r.go(60, temp=83)
    r.kw['cal'] = False
    r.go(5, temp=75)
    assert r.events('start')[-1]['trigger'] == 'nach Takt-Messung' and not r.events('startpause')


def test_automatik_off_on_does_not_revive_an_old_button_restart(miner):
    ac = FakeAC(level=5)
    r = started(miner, ac, level=5, learned={'5': {'mhz': 300, 'temp_avg': 82, 'at': 'x', 'count': 1}})
    r.go(30, temp=82)
    r.kw['restart_req_at'] = r.now.isoformat()
    r.go(5, temp=82)
    miner.elapsed = 10
    r.go(5, temp=26)
    assert not r.st.get('restart_req')
    r.kw['enabled'] = False
    r.go(10)
    r.kw['enabled'] = True
    r.go(10, temp=40)
    assert not r.st.get('restart_req')


def test_long_outage_pair_is_relabeled_together(miner):
    """Nutzer 26.10.: Strom 6 min aus – „antwortet nicht“ (älter als 3 min) und „wieder“ gemeinsam umbenennen."""
    ac = FakeAC(level=5)
    r = started(miner, ac, level=5, learned={'5': {'mhz': 300, 'temp_avg': 82, 'at': 'x', 'count': 1}})
    r.go(30, temp=82)
    miner.offline = True
    r.go(6 * 60)
    miner.offline, miner.elapsed = False, 10
    r.go(5, temp=26)
    assert not r.events('miner_offline') and not r.events('miner_online')
    assert r.events('neustart_offline')[0]['details']['meldung'] == 'timed out'
    assert r.events('neustart_online')[0]['trigger'] == 'A15 nach Neustart wieder erreichbar nach 360 s'


# ---------------------------------------------------------------- Einstellungsseite der A15 (Nutzer 26.10.)

def test_settings_page_down_regulation_goes_on(miner):
    """26.10. 04:31: 9,5 min antwortete nur die Einstellungsseite nicht – die Automatik war blind. Jetzt regelt sie
    weiter, kein „Miner antwortet nicht“; Eintrag erst nach 2 min, beim Zurückkommen die Dauer."""
    ac = FakeAC(level=5)
    r = started(miner, ac, level=5, learned={'5': {'mhz': 300, 'temp_avg': 82, 'at': 'x', 'count': 1}})
    r.go(30, temp=82)
    miner.web_down = True
    r.go(60, temp=82)
    assert not r.events('einstellungsseite_aus') and not r.events('miner_offline')
    _pulses(r, 85)
    r.go(5, temp=85)
    assert r.st['clock_set'] == 290 and r.st['phase'] == 'korrektur'   # regelt weiter (Absenkung ab 85 °C)
    r.go(60, temp=82)
    ev = r.events('einstellungsseite_aus')
    assert len(ev) == 1 and ev[0]['ok'] is None and ev[0]['trigger'] == ('A15-Einstellungsseite antwortet nicht – '
                                                                       'Mining-Schnittstelle ok')
    miner.web_down = False                                        # (die 60 s davor stecken in den Lüfterstößen)
    r.go(5, temp=82)
    assert r.events('einstellungsseite_an')[0]['trigger'] == 'A15-Einstellungsseite wieder erreichbar nach 3 min'
    assert not r.events('miner_offline')


def test_short_settings_page_drop_no_entry(miner):
    ac = FakeAC(level=5)
    r = started(miner, ac, level=5, learned={'5': {'mhz': 300, 'temp_avg': 82, 'at': 'x', 'count': 1}})
    r.go(30, temp=82)
    miner.web_down = True
    r.go(100, temp=82)
    miner.web_down = False
    r.go(10, temp=82)
    assert not r.events('einstellungsseite_aus') and not r.events('einstellungsseite_an')


def test_mode_switch_waits_for_the_settings_page(miner):
    """Ein Moduswechsel braucht die Pool-Felder der Einstellungsseite: nie mit alten Daten, er wartet."""
    ac = FakeAC(level=5)
    r = started(miner, ac, level=5, tmax=78, learned={'5': {'mhz': 358, 'temp_avg': 82, 'at': 'x', 'count': 1}})
    miner.web_down = True
    r.go(120, temp=78)                                            # +5 → 363 → mode 03 nötig
    assert miner.mode_sets == [] and len(r.events('modus_wartet')) == 1
    assert r.st['clock_set'] == 360 and not r.st['queue']         # Takt bis zum Standardtakt von mode 04
    r.go(30, temp=78)
    assert miner.mode_sets == [] and len(r.events('modus_wartet')) == 1
    miner.web_down = False
    r.go(185, temp=78)                                            # Seite wieder da: nächster Plus-Schritt (nach 3 min) mit Modus
    assert miner.mode_sets == [3] and miner.clock() == r.st['clock_set'] > 360 and r.st.get('mode_wait') is None


def test_waiting_mode_switch_does_not_block_further_reductions(miner):
    """Review 26.10.: wartete der Moduswechsel in der Befehlsfolge, kam ab 85 °C nach dem ersten Schritt keine weitere
    Absenkung mehr (busy). Jetzt wartet er außerhalb, die Absenkungen laufen weiter."""
    ac = FakeAC(level=5)
    miner.level, miner.points = 3, list(STOCK['2/L3'])
    r = started(miner, ac, level=5, learned={'5': {'mhz': 370, 'temp_avg': 82, 'at': 'x', 'count': 1}})
    r.go(30, temp=82)
    miner.web_down = True
    r.go(5, temp=88)                                              # 370 − 30 = 340 → mode 04 nötig
    r.go(5, temp=88)
    assert r.st['clock_set'] == 340 and miner.mode_sets == [] and r.events('modus_wartet')
    r.go(35, temp=88)
    assert r.st['clock_set'] == 310                               # weitere Absenkung trotz wartendem Moduswechsel
    r.go(40, temp=92)
    assert r.st['clock_set'] == 230


def test_second_outage_logs_mode_wait_again(miner):
    ac = FakeAC(level=5)
    r = started(miner, ac, level=5, tmax=78, learned={'5': {'mhz': 358, 'temp_avg': 82, 'at': 'x', 'count': 1}})
    miner.web_down = True
    r.go(115, temp=78)
    assert len(r.events('modus_wartet')) == 1
    miner.web_down = False
    r.go(60, temp=82)
    miner.web_down = True
    r.go(10, temp=82)
    r.go(40, temp=88)                                             # Absenkung über die Modusgrenze: wartet wieder
    r.go(40, temp=78)
    assert len(r.events('modus_wartet')) >= 1


def test_settings_never_read_keeps_the_last_known_mode(miner):
    """Review 26.10.: nach einem Data-Service-Neustart ohne gelesene Einstellungen kein rotes „antwortet nicht“ –
    die Automatik regelt mit dem zuletzt bekannten Modus weiter."""
    ac = FakeAC(level=5)
    r = started(miner, ac, level=5, learned={'5': {'mhz': 300, 'temp_avg': 82, 'at': 'x', 'count': 1}})
    r.go(30, temp=82)
    miner.web_down = miner.never_read = True
    _pulses(r, 85)
    r.go(5, temp=85)
    assert not r.events('miner_offline') and r.st['mode_key'] == '2/L4' and r.st['clock_set'] == 290


def test_waiting_mode_switch_caps_the_clock_at_the_current_mode(miner):
    """Review 26.10.: wartete der Moduswechsel, stieg der gesetzte Takt bis 440 MHz, obwohl der Miner beim Standardtakt
    von mode 04 blieb (Ausnahme, falscher Lernwert, am Ende ein Sprung). Jetzt höchstens der Standardtakt des Modus."""
    ac = FakeAC(level=5)
    r = started(miner, ac, level=5, learned={'5': {'mhz': 350, 'temp_avg': 82, 'at': 'x', 'count': 1}})
    r.go(30, temp=82)
    miner.web_down = True
    r.go(6 * 60, temp=65)
    assert r.st['clock_set'] == 360 and miner.mode_sets == [] and not r.events('ausnahme')
    assert len(r.events('modus_wartet')) == 1
    miner.web_down = False
    r.go(10, temp=65)
    assert r.st['clock_set'] <= 380                               # danach wieder in Schritten, kein Sprung auf 440


# ---------------------------------------------------------------- 3 min zwischen Takterhöhungen bei 75–78 °C (Nutzer 26.10.)

def test_plus_steps_at_75_to_78_wait_3_min(miner):
    ac = FakeAC(level=5)
    r = started(miner, ac, level=5, tmax=76, learned={'5': {'mhz': 250, 'temp_avg': 82, 'at': 'x', 'count': 1}})
    r.go(55, temp=76)
    assert r.st['clock_set'] == 255
    r.go(170, temp=76)
    assert r.st['clock_set'] == 255                               # 3 min nach der letzten Erhöhung noch nicht vorbei
    r.go(15, temp=76)
    assert r.st['clock_set'] == 260


def test_wait_counts_from_any_increase_but_not_below_75(miner):
    """Meine Lesart (freigegeben): die 3 min zählen ab der letzten Takterhöhung, egal bei welcher Temperatur."""
    ac = FakeAC(level=5)
    r = started(miner, ac, level=5, tmax=65, learned={'5': {'mhz': 250, 'temp_avg': 82, 'at': 'x', 'count': 1}})
    r.go(55, temp=65)
    assert r.st['clock_set'] == 270                               # 60–69 °C: +20
    r.go(40, temp=72)
    assert r.st['clock_set'] == 275                               # unter 75 °C: nach dem 30-s-Fenster wie bisher
    r.go(40, temp=76)
    assert r.st['clock_set'] == 275                               # 75–78 °C: erst 3 min nach dem +5
    r.go(150, temp=76)
    assert r.st['clock_set'] == 280


def test_dwell_limit_is_5_min(miner):
    ac = FakeAC(level=5)
    r = started(miner, ac, level=5, tmax=75, learned={'5': {'mhz': 300, 'temp_avg': 82, 'at': 'x', 'count': 1}})
    r.go(60, temp=75)
    clock = r.st['clock_set']
    r.go(5 * 60, temp=82)                                         # 5 min in 79–84 °C
    _pulses(r, 85)
    r.go(5, temp=85)
    assert r.st['clock_set'] == clock - 5


def test_plus_wait_counts_from_the_takeover_and_only_real_increases(miner):
    """Review 26.10.: die 3 min zählen ab der Übernahme (nicht ab der Entscheidung); ein auf den gleichen Takt gekappter
    Schritt (Moduswechsel wartet) ist keine Takterhöhung."""
    ac = FakeAC(level=5)
    r = started(miner, ac, level=5, tmax=76, learned={'5': {'mhz': 360, 'temp_avg': 82, 'at': 'x', 'count': 1}})
    r.go(10, temp=82)
    miner.web_down = True
    r.go(125, temp=76)                                            # 360 → 365 bräuchte mode 03: gekappt auf 360
    assert r.st['clock_set'] == 360 and not r.st.get('last_plus_at')
    miner.web_down = False
    r.go(45, temp=76)                                             # Seite wieder da: der Plus-Schritt kommt gleich
    assert miner.mode_sets == [3] and r.st['clock_set'] == 365 and r.st.get('last_plus_at')


# ---------------------------------------------------------------- Heizmodus beim Start (Nutzer 26.10.)

def test_heat_mode_after_a_cold_start_fan_1_until_79(miner):
    ac = FakeAC(level=3)
    r = started(miner, ac, level=3, learned={'3': {'mhz': 300, 'temp_avg': 82, 'at': 'x', 'count': 1}})
    r.go(30, temp=82)
    assert ac.level == 3
    miner.elapsed = 10                                            # Neustart: Chips kalt
    r.go(10, temp=26)
    assert ac.level == 1 and r.st['heat']
    assert r.events('heizmodus')[-1]['trigger'] == 'Heizmodus: Lüfter Stufe 1 bis 79 °C'
    r.go(120, temp=78)
    assert ac.level == 1
    r.go(15, temp=79)
    assert ac.level == 3 and not r.st['heat']
    assert r.events('heizmodus_ende')[-1]['trigger'] == 'Heizmodus beendet: 79 °C erreicht, Lüfter Stufe 3'


def test_no_heat_mode_when_already_warm(miner):
    ac = FakeAC(level=3)
    r = started(miner, ac, level=3, learned={'3': {'mhz': 300, 'temp_avg': 82, 'at': 'x', 'count': 1}})
    r.kw['level_at'] = 'neu'                                      # Basisstufe gewählt bei 82 °C
    r.go(15, temp=82)
    assert ac.level == 3 and not r.st['heat'] and not r.events('heizmodus')


def test_heat_mode_ends_when_a_temperature_test_begins(miner):
    """Review 26.10.: der Temperatur-Test stellt den Lüfter auf die Basisstufe – damit endet der Heizmodus."""
    ac = FakeAC(level=2)
    r = started(miner, ac, level=2, tmax=40, learned={'2': {'mhz': 319.2, 'temp_avg': 82, 'at': 'x', 'count': 1}})
    assert r.st['heat'] and ac.level == 1
    r.kw['temp_test_at'] = r.now.isoformat()
    r.go(10, temp=60)
    assert r.st['test'] and not r.st['heat'] and ac.level == 2
    ev = r.events('heizmodus_ende')[-1]
    assert ev['trigger'] == 'Heizmodus beendet: Temperatur-Test, Lüfter Stufe 2' and ev['fan_old'] == 1


def test_no_heat_mode_for_a_start_while_a_temperature_test_runs(miner):
    """Review 26.10.: Neustart und Test-Beginn in derselben Runde – der Start schaltet den Heizmodus nicht wieder ein."""
    ac = FakeAC(level=3)
    r = started(miner, ac, level=3, learned={'3': {'mhz': 300, 'temp_avg': 82, 'at': 'x', 'count': 1}})
    r.go(30, temp=82)
    miner.elapsed = 10
    r.kw['temp_test_at'] = r.now.isoformat()
    r.go(5, temp=30)
    assert r.st['test'] and not r.st['heat'] and ac.level == 3
    r.go(300, temp=69)
    assert r.st['test'] and not r.st['heat'] and ac.level == 3


def test_heat_mode_ends_with_a_start_during_takt_messung(miner):
    ac = FakeAC(level=5)
    r = started(miner, ac, level=5, tmax=40, learned={'5': {'mhz': 300, 'temp_avg': 82, 'at': 'x', 'count': 1}})
    assert r.st['heat'] and ac.level == 1
    r.kw['cal'] = True
    r.go(10, temp=60)
    miner.elapsed = 10                                            # Moduswechsel der Messung: Laufzeit zurückgesetzt
    r.go(10, temp=60)
    assert not r.st['heat'] and ac.level == 5
    r.go(30, temp=76)
    assert len(r.events('heizmodus_ende')) == 1


def _heating(miner, ac):
    r = started(miner, ac, level=3, tmax=40, learned={'3': {'mhz': 300, 'temp_avg': 82, 'at': 'x', 'count': 1}})
    assert r.st['heat'] and ac.level == 1
    return r


def test_heat_mode_ends_at_once_when_automation_is_turned_off(miner):
    """Nutzer 26.10.: ohne Regelung kein Lüfter auf Stufe 1 – Automatik aus, Miner antwortet nicht, Over Hot."""
    ac = FakeAC(level=3)
    r = _heating(miner, ac)
    ac.fail = True
    r.go(5, temp=60, enabled=False)
    assert not r.st['heat'] and ac.level == 1 and r.st.get('heat_fan_due')    # Befehl fehlgeschlagen: wiederholen
    ac.fail = False
    r.go(60, temp=60)
    assert ac.level == 3 and not r.st.get('heat_fan_due') and r.st['phase'] == 'aus'
    assert r.events('heizmodus_ende')[-1]['trigger'] == 'Heizmodus beendet: Automatik aus, Lüfter Stufe 3'
    assert r.events('luefter')[-1]['takeover_state'] == 'nicht gemessen' and not r.st['fan_pending']
    writes = list(ac.writes)
    r.go(60, temp=60)
    assert ac.writes == writes                                    # danach kein Lüfterbefehl mehr bei Automatik aus


def test_heat_mode_ends_at_once_when_the_miner_does_not_answer(miner):
    ac = FakeAC(level=3)
    r = _heating(miner, ac)
    miner.offline = True
    r.go(5, temp=60)
    assert not r.st['heat'] and ac.level == 3
    assert r.events('heizmodus_ende')[-1]['trigger'] == 'Heizmodus beendet: Miner antwortet nicht, Lüfter Stufe 3'
    assert r.j.cycles[-1]['decision'] == 'Miner antwortet nicht'


def test_heat_mode_stays_while_the_a15_restarts_by_button(miner):
    ac = FakeAC(level=3)
    r = _heating(miner, ac)
    r.kw['restart_req_at'] = r.now.isoformat()
    miner.offline = True
    r.go(30, temp=60)
    assert r.st['offline'] == 'neustart' and r.st['heat'] and ac.level == 1


def test_heat_mode_ends_at_once_on_over_hot(miner):
    ac = FakeAC(level=3)
    r = _heating(miner, ac)
    miner.status = 'Over Hot'
    r.go(5, temp=60)
    assert not r.st['heat'] and ac.level == 3
    assert r.events('heizmodus_ende')[-1]['trigger'] == 'Heizmodus beendet: Over Hot, Lüfter Stufe 3'


def test_heat_mode_also_when_the_temperature_is_unknown_at_the_start(miner):
    ac = FakeAC(level=4)
    r = started(miner, ac, level=4, learned={'4': {'mhz': 300, 'temp_avg': 82, 'at': 'x', 'count': 1}})
    r.go(30, temp=82)
    miner.elapsed, miner.status = 10, 'Over Hot'                 # Over Hot: keine gültige Temperatur
    r.go(10, temp=82)
    miner.status = 'Work: In Work'
    r.go(5, temp=40)
    assert r.st['heat'] and ac.level == 1
    r.go(30, temp=80)
    assert not r.st['heat'] and ac.level == 4


# ---------------------------------------------------------------- Zielbereich wählbar (Nutzer 27.10.)

LW = {'mhz': 300, 'temp_avg': 82, 'at': 'x', 'count': 1}


def test_high_table_follows_the_upper_edge_capped_at_96():
    assert fa.high_rule(84, 84) is None and fa.high_rule(85, 84) == (85, 1, None)       # 79–84 °C wie bisher
    assert fa.high_rule(88, 84) == (88, 2, 30) and fa.high_rule(94, 84) == (94, 2, 100)
    assert fa.high_rule(85, 85) is None and fa.high_rule(86, 85) == (86, 1, None)
    assert fa.high_rule(87, 85) == (87, 1, None) and fa.high_rule(89, 85) == (89, 2, 30)
    assert fa.high_rule(91, 85) == (91, 2, 50) and fa.high_rule(93, 85) == (93, 2, 80)
    assert fa.high_rule(95, 85) == (95, 2, 100)
    assert fa.high_rule(92, 92) is None and fa.high_rule(93, 92) == (93, 1, None)       # 89–92 °C: Deckel 96 °C
    assert fa.high_rule(95, 92) == (94, 1, None) and fa.high_rule(96, 92) == (96, 2, 100)
    assert fa.high_rule(95, 89) == (95, 2, 50) and fa.high_rule(96, 89) == (96, 2, 100)
    assert fa.high_rule(78, 77) == (78, 1, None) and fa.high_rule(81, 77) == (81, 2, 30)


def test_plus_size_follows_the_lower_edge():
    assert [fa.plus_size(t, 82) for t in (81, 73, 72, 63, 62)] == [5, 5, 20, 20, 40]
    assert [fa.plus_size(t, 76) for t in (75, 67, 66, 57, 56)] == [5, 5, 20, 20, 40]


def test_range_82_85_no_clock_change_inside_learning_until_85(miner):
    ac = FakeAC(level=5)
    r = started(miner, ac, level=5, learned={'5': LW}, zb=(82, 85))
    sets = list(miner.clock_sets)
    for temp in (82, 85, 83, 85, 82):
        r.go(60, temp=temp)
    assert miner.clock_sets == sets and r.st['phase'] == 'normal' and ac.level == 5
    assert r.st['learn_since'] and r.events('lernen_start')[0]['trigger'] == 'Chiptemperatur 82 °C in 82–85 °C'
    _pulses(r, 86)
    r.go(5, temp=86)                                              # ab U+1: hohe Chiptemperatur, Lernen bricht ab
    assert r.st['phase'] == 'korrektur' and ac.level == 6 and r.st['clock_set'] == 295 and not r.st['learn_since']
    assert r.events('takt')[-1]['trigger'] == 'Chiptemperatur 86 °C nach 5 min in 82–85 °C: Takt −5 MHz'


def test_range_82_85_plus_steps(miner):
    """+5 ab L−9 (73 °C), +20 ab L−19 (63 °C), darunter +40."""
    for temp, step in ((82, 0), (81, 5), (73, 5), (72, 20), (63, 20), (62, 40)):
        miner.clock_sets.clear()
        ac = FakeAC(level=5)
        r = started(miner, ac, level=5, tmax=temp, learned={'5': {'mhz': 200, 'temp_avg': 82, 'at': 'x', 'count': 1}},
                    zb=(82, 85))
        r.go(55, temp=temp)
        assert miner.clock() == 200 + step, temp


def test_range_82_85_wait_3_min_from_78(miner):
    ac = FakeAC(level=5)
    r = started(miner, ac, level=5, tmax=78, learned={'5': {'mhz': 250, 'temp_avg': 82, 'at': 'x', 'count': 1}},
                zb=(82, 85))
    r.go(55, temp=78)
    assert r.st['clock_set'] == 255
    r.go(170, temp=78)
    assert r.st['clock_set'] == 255                               # ab L−4 = 78 °C: 3 min nach der letzten Erhöhung
    r.go(15, temp=78)
    assert r.st['clock_set'] == 260
    r.go(40, temp=77)
    assert r.st['clock_set'] == 265                               # unter 78 °C: nach dem 30-s-Fenster


def test_range_82_85_return_band(miner):
    """Rückkehr nach 1 min stabil in 82–85 °C, sofort unter 82 °C."""
    ac = FakeAC(level=5)
    r = started(miner, ac, level=5, learned={'5': LW}, zb=(82, 85))
    _pulses(r, 86)
    r.go(5, temp=86)
    assert ac.level == 6
    r.go(55, temp=85)
    assert ac.level == 6 and r.st['phase'] == 'korrektur'
    assert r.events('stabilitaet_start')[-1]['trigger'] == 'Rückkehr: Chiptemperatur 85 °C in 82–85 °C'
    r.go(15, temp=85)
    assert ac.level == 5 and r.st['phase'] == 'normal'
    assert r.events('rueckkehr')[-1]['trigger'] == '1 min stabil in 82–85 °C'
    r.go(5, temp=86)                                              # keine 10 min seit den Stößen: gleich korrigiert
    r.go(5, temp=81)
    assert r.st['phase'] == 'normal' and r.events('rueckkehr')[-1]['trigger'] == 'Chiptemperatur 81 °C unter 82 °C'


def test_range_89_92_high_from_93_and_96_cap(miner):
    ac = FakeAC(level=5)
    r = started(miner, ac, level=5, tmax=90, learned={'5': LW}, zb=(89, 92))
    r.go(60, temp=92)
    assert r.st['clock_set'] == 300 and r.st['phase'] == 'normal'
    _pulses(r, 93)
    r.go(5, temp=93)
    assert ac.level == 6 and r.st['clock_set'] == 290             # U+1: Lüfter +1, kurzer Aufenthalt −10
    r.go(40, temp=96)
    assert ac.level == 7 and r.st['clock_set'] == 190             # ab 96 °C: Lüfter +2, Takt −100
    assert r.events('takt')[-1]['trigger'].startswith('Chiptemperatur 96 °C (ab 96 °C): Takt −100 MHz')


def test_narrow_range_76_77(miner):
    ac = FakeAC(level=5)
    r = started(miner, ac, level=5, tmax=77, learned={'5': LW}, zb=(76, 77))
    sets = list(miner.clock_sets)
    r.go(60, temp=76)
    r.go(60, temp=77)
    assert miner.clock_sets == sets and r.st['phase'] == 'normal'
    _pulses(r, 78)
    r.go(5, temp=78)                                              # ab 78 °C hoch
    assert ac.level == 6 and r.st['clock_set'] == 290
    r.go(5, temp=77)
    assert r.st['phase'] == 'korrektur'                           # in 76–77 °C: 1 min warten
    r.go(5, temp=75)
    assert r.st['phase'] == 'normal' and ac.level == 5            # unter 76 °C sofort zurück


def test_narrow_range_76_77_plus_steps_and_wait_from_72(miner):
    ac = FakeAC(level=5)
    r = started(miner, ac, level=5, tmax=75, learned={'5': LW}, zb=(76, 77))
    r.go(115, temp=75)
    assert r.st['clock_set'] == 305
    r.go(60, temp=72)
    assert r.st['clock_set'] == 305                               # ab L−4 = 72 °C: 3 min warten
    r.go(5, temp=71)
    assert r.st['clock_set'] == 310


def test_heat_mode_and_start_hold_end_at_the_lower_edge(miner):
    ac = FakeAC(level=3)
    r = started(miner, ac, level=3, learned={'3': LW}, zb=(82, 85))
    r.go(30, temp=84)
    miner.elapsed = 10                                            # Neustart: Chips kalt
    r.go(10, temp=26)
    assert ac.level == 1 and r.st['heat'] and r.st.get('start_hold')
    assert r.events('heizmodus')[-1]['trigger'] == 'Heizmodus: Lüfter Stufe 1 bis 82 °C'
    assert r.events('startpause')[-1]['trigger'].startswith('Startpause: keine Takterhöhung bis 82 °C, spätestens ')
    r.go(120, temp=81)
    assert ac.level == 1 and r.st['heat'] and r.st.get('start_hold') and r.st['clock_set'] == 300
    r.go(15, temp=82)
    assert ac.level == 3 and not r.st['heat'] and not r.st.get('start_hold')
    assert r.events('heizmodus_ende')[-1]['trigger'] == 'Heizmodus beendet: 82 °C erreicht, Lüfter Stufe 3'
    assert r.events('startpause_ende')[-1]['trigger'] == 'Startpause beendet: 82 °C erreicht (nach 2 min)'


def test_exception_below_l_minus_9(miner):
    ac = FakeAC(level=5)
    r = started(miner, ac, level=5, learned={'5': {'mhz': 440, 'temp_avg': 82, 'at': 'x', 'count': 1}}, zb=(82, 85))
    r.go(185, temp=73)
    assert ac.level == 5 and not r.events('ausnahme')             # 73 °C ist nicht unter L−9
    r.go(185, temp=72)
    assert ac.level == 4 and r.st['phase'] == 'ausnahme'
    assert r.events('ausnahme')[-1]['trigger'] == 'Takt 440 MHz, 3 min stabil unter 73 °C'
    r.go(10, temp=74)
    assert r.st['phase'] == 'normal' and r.events('ausnahme_ende')[-1]['trigger'] == 'Chiptemperatur 74 °C über 73 °C'


def test_temp_test_begins_at_l_plus_1(miner):
    ac = FakeAC(level=2)
    r = started(miner, ac, level=2, learned={'2': {'mhz': 319.2, 'temp_avg': 82, 'at': 'x', 'count': 1}}, zb=(82, 85))
    r.kw['temp_test_at'] = r.now.isoformat()
    r.go(70, temp=84)
    assert r.st['test']['step'] == 0 and r.st['test']['phase'] == 'beginn'
    assert any(n[1].endswith('dann warten bis ≤ 83 °C.') for n in r.st['log'])
    r.go(5, temp=83)
    assert r.st['test']['step'] == 1 and r.st['clock_set'] == 349


def test_takt_messung_keeps_the_fixed_protection_with_a_high_range(miner):
    """Takt-Messung: Lüfterschutz wie bisher ab 85 °C, Rückkehr nach 1 min in 80–84 °C – unabhängig vom Zielbereich."""
    ac = FakeAC(level=5)
    r = started(miner, ac, level=5, tmax=90, learned={'5': LW}, zb=(89, 92))
    r.go(10, temp=84, cal=True)
    sends = len(miner.clock_sets)
    assert not r.st.get('corr')
    r.go(5, temp=86)                                              # unter dem Zielbereich, in der Messung aber hoch
    assert r.st['corr'] and ac.level == 6 and len(miner.clock_sets) == sends
    r.go(5, temp=88)
    assert ac.level == 7
    r.go(30, temp=83)
    assert r.st['corr']                                           # 83 °C: im Rückkehrbereich 80–84 °C, nicht sofort
    r.go(35, temp=83)
    assert not r.st['corr'] and ac.level == 5


def test_takt_messung_keeps_the_fixed_protection_with_a_low_range(miner):
    ac = FakeAC(level=5)
    r = started(miner, ac, level=5, tmax=77, learned={'5': LW}, zb=(76, 77))
    r.go(10, temp=77, cal=True)
    r.go(30, temp=84)
    assert not r.st.get('corr') and ac.level == 5                 # ohne Takt-Messung wäre 84 °C hoch
    r.go(5, temp=85)
    assert r.st['corr'] and ac.level == 6
    r.go(5, temp=79)
    assert not r.st['corr']                                       # unter 80 °C sofort zurück


def _without_range(miner, ac, lo, hi, tmax):
    """Zustand vor dem 27.10.: Lernwerte, aber kein gemerkter Bereich."""
    r = Run(miner, ac, 5)
    r.st = {'v': fa.STATE_VERSION, 'seen_at': T0.isoformat(), 'learned': {'5': LW}, 'level_seen': 'x'}
    r.kw.update(level_at='klick', target_low=lo, target_high=hi)
    miner.tmax = tmax
    return r.go(10)


def test_first_round_without_a_range_keeps_the_learned_values(miner):
    """Stand vor dem 27.10. (kein gemerkter Bereich): die Lernwerte gehören zum Startwert 82–85, kein Eintrag."""
    ac = FakeAC(level=5)
    r = _without_range(miner, ac, 82, 85, 83)
    assert r.st['zb'] == [82, 85] and r.st['learned'] == {'5': LW} and 'learned_zb' not in r.st
    assert not [n for n in r.st['log'] if 'Zielbereich' in n[1]]
    assert r.events('start')[-1]['details']['quelle'] == 'Lernwert' and r.st['clock_set'] == 300


def test_first_round_without_a_range_but_another_range_chosen(miner):
    """Review 27.10.: vor der ersten Runde schon ein anderer Bereich gewählt (z. B. bei Automatik aus) – die Lernwerte
    bleiben beim Startwert 82–85; der neue Bereich startet mit dem Lernwert des nächsten Bereichs (Nutzer 28.10.,
    vorher Initialwert)."""
    ac = FakeAC(level=5)
    r = _without_range(miner, ac, 76, 77, 70)
    assert r.st['zb'] == [76, 77] and r.st['learned_zb'] == {'82-85': {'5': LW}} and r.st['learned'] == {}
    assert r.events('start')[-1]['details']['quelle'] == 'Lernwert 82–85' and r.st['clock_set'] == 300


def test_range_change_leaves_the_saved_state_untouched(miner):
    """Review 27.10.: scheitert die Runde nach dem Wechsel, speichert run_fan_automation den alten Zustand – dessen
    Archiv bleibt vollständig."""
    other = {'mhz': 340, 'temp_avg': 84, 'at': 'x', 'count': 1}
    state = {'v': fa.STATE_VERSION, 'zb': [82, 85], 'learned': {'5': other}, 'learned_zb': {'79-84': {'5': LW}}}
    rnd = fa.Round(row(state, level=5, target_low=79, target_high=84), T0, FakeAC(level=5),
                   automation_journal.MemoryJournal())
    rnd.target_range()
    assert rnd.st['learned'] == {'5': LW} and rnd.st['learned_zb'] == {'82-85': {'5': other}}
    assert state['learned_zb'] == {'79-84': {'5': LW}}


def test_range_change_takes_the_new_limits_at_once_without_a_start(miner):
    ac = FakeAC(level=5)
    r = started(miner, ac, level=5, learned={'5': LW})
    r.go(60, temp=83)
    assert r.st['zb'] == [79, 84] and r.st['learn_since']
    sets, starts = list(miner.clock_sets), len(r.events('start'))
    r.kw.update(target_low=82, target_high=85)
    r.go(5, temp=83)
    assert r.st['zb'] == [82, 85] and r.st['learned'] == {} and r.st['learned_zb'] == {'79-84': {'5': LW}}
    assert r.events('lernen_ende')[-1]['details']['grund'] == 'Zielbereich geändert'
    assert r.st['band_in'] == (r.now - timedelta(seconds=5)).isoformat()    # Aufenthalt neu gemessen
    assert any(n[1] == 'Zielbereich 82–85 °C übernommen.' for n in r.st['log'])
    assert len(r.events('start')) == starts and miner.clock_sets == sets and not r.st.get('heat') and ac.level == 5
    r.go(65, temp=80)                                             # 80 °C liegt jetzt unter dem Zielbereich: +5 (nach 60 s)
    assert r.st['clock_set'] == 305


def test_learned_values_are_kept_per_range(miner):
    """Nutzer 27.10.: zurück im alten Bereich gelten wieder seine Lernwerte; ein neuer Bereich beginnt mit dem
    Lernwert des nächsten Bereichs (Nutzer 28.10.; vorher Initialwert) und lernt eigene."""
    ac = FakeAC(level=5)
    r = started(miner, ac, level=5, learned={'5': LW})
    r.go(30, temp=83)
    r.kw.update(target_low=82, target_high=85, level_at='neu')
    r.go(10, temp=83)
    assert r.events('start')[-1]['details']['quelle'] == 'Lernwert 79–84' and r.st['clock_set'] == 300
    assert r.st['learned'] == {}
    r.go(15 * 60 + 30, temp=83)
    assert r.st['learned']['5']['mhz'] == 300 and r.st['learned']['5']['temp_avg'] == 83.0
    r.kw.update(target_low=79, target_high=84)
    r.go(5, temp=83)
    assert r.st['learned'] == {'5': LW} and r.st['learned_zb']['82-85']['5']['temp_avg'] == 83.0
    r.kw['level_at'] = 'neu2'
    r.go(10, temp=83)
    assert r.events('start')[-1]['details']['quelle'] == 'Lernwert' and r.st['clock_set'] == 300


def test_automatik_off_on_keeps_the_range_and_the_learned_values_per_range(miner):
    ac = FakeAC(level=5)
    r = started(miner, ac, level=5, learned={'5': LW})
    r.go(30, temp=83)
    r.kw.update(target_low=82, target_high=85)
    r.go(5, temp=83)
    r.kw['enabled'] = False
    r.go(10)
    r.kw['enabled'] = True
    r.go(10, temp=83)
    assert r.st['zb'] == [82, 85] and r.st['learned_zb'] == {'79-84': {'5': LW}}
    assert len([n for n in r.st['log'] if 'Zielbereich' in n[1]]) == 1
    r.kw.update(target_low=79, target_high=84)
    r.go(5, temp=83)
    assert r.st['learned'] == {'5': LW}


def test_select_reads_the_target_range(monkeypatch):
    seen = []

    class Cur:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def execute(self, sql, args=None):
            seen.append(sql)

        def fetchall(self):
            return []

    class Conn:
        def cursor(self, cursor_factory=None):
            return Cur()

        def close(self):
            pass

    monkeypatch.setenv('MINERSENTINEL_ENV', 'Live')
    monkeypatch.setattr(fa.psycopg2, 'connect', lambda url: Conn())
    fa.run_fan_automation('postgresql://x', ac=FakeAC())
    assert 'f.target_low' in seen[0] and 'f.target_high' in seen[0]


# ---------------------------------------------------------------- Lüfterstöße (Nutzer 27.10.)

ZB = [None, (82, 85)]                                             # Testbereich 79–84 und Startwert des Nutzers 82–85
STOSS_WEITER = 'Lüfterstoß: keine Bestätigung nach 30 s – weiter.'


def _hot(zb, plus=1):
    """U + plus des Bereichs (ohne Angabe 79–84 °C)."""
    return (zb or (79, 84))[1] + plus


@pytest.mark.parametrize('zb', ZB)
def test_pulses_first_then_correction_phase_after_70_s(miner, zb):
    """Ab U+1 zuerst zwei Lüfterstöße: Basisstufe + 1, 6 s ab der Bestätigung (Lüftersteuerung meldet die Stufe, gelesen
    10 s nach dem Befehl), dann zurück; kein Takt. Danach Korrekturphase wie bisher – erste Taktsenkung 70 s nach dem
    ersten Wert über U."""
    ac = FakeAC(level=5)
    r = started(miner, ac, level=5, learned={'5': LW}, zb=zb)
    r.go(30, temp=83)
    hot, start, n = _hot(zb), r.now, len(ac.writes)
    r.go(5, temp=hot)
    assert r.st['stoss'] == {'n': 1, 'step': 'an', 'at': start.isoformat(), 'level': 6}
    assert ac.level == 6 and r.st['phase'] == 'normal' and not r.st['corr'] and not r.st['learn_since']
    ev = r.events('luefterstoss')[-1]
    assert ev['trigger'] == 'Lüfterstoß 1/2: Stufe 6 für 6 s (Chiptemperatur %d °C)' % hot
    assert ev['fan_old'] == 5 and ev['fan_new'] == 6
    r.go(15, temp=hot)
    assert r.st['stoss']['step'] == 'an' and r.st['stoss']['ok'] == (start + timedelta(seconds=10)).isoformat()
    r.go(5, temp=hot)                                             # 6 s nach der Bestätigung: zurück
    assert r.st['stoss'] == {'n': 1, 'step': 'aus', 'at': (start + timedelta(seconds=20)).isoformat(), 'level': 5}
    assert ac.level == 5
    r.go(15, temp=hot)                                            # „aus“ bestätigt: Stoß 2
    assert r.st['stoss']['n'] == 2 and r.st['stoss']['step'] == 'an' and ac.level == 6
    assert r.events('luefterstoss')[-1]['trigger'] == 'Lüfterstoß 2/2: Stufe 6 für 6 s (Chiptemperatur %d °C)' % hot
    r.go(20, temp=hot)
    assert r.st['stoss']['n'] == 2 and r.st['stoss']['step'] == 'aus' and ac.level == 5
    r.go(10, temp=hot)
    assert r.st['stoss'] and not r.st['corr'] and r.st['clock_set'] == 300 and miner.clock() == 300
    r.go(5, temp=hot)                                             # Stoß 2 „aus“ bestätigt: fertig, Korrekturphase
    assert not r.st['stoss'] and r.st['corr'] and ac.level == 6 and r.st['clock_set'] == 290 and miner.clock() == 290
    assert ac.writes[n:] == [6, 5, 6, 5, 6]
    assert r.events('luefterstoss_ende')[-1]['trigger'] == 'Lüfterstöße fertig: %d °C, Takt wird gesenkt' % hot
    takt = r.events('takt')[-1]
    assert takt['recorded_at'] - start == timedelta(seconds=70)
    assert takt['trigger'] == 'Chiptemperatur %d °C nach 0 min in %d–%d °C: Takt −10 MHz' % (hot, *(zb or (79, 84)))
    assert all(e['takeover_state'] == 'gemessen' for e in r.events('luefter')[-5:-1])


@pytest.mark.parametrize('zb', ZB)
@pytest.mark.parametrize('secs, n', [(10, 1), (40, 2)])
def test_pulses_end_back_in_the_target_range_without_clock_change(miner, zb, secs, n):
    """Zurück in L-U während Stoß 1 oder 2: Ende, Lüfter Basisstufe, kein Takt, keine Korrekturphase; danach die
    normalen Regeln (unter L +5)."""
    ac = FakeAC(level=5)
    r = started(miner, ac, level=5, learned={'5': LW}, zb=zb)
    hot, hi = _hot(zb), _hot(zb, 0)
    sets = list(miner.clock_sets)
    r.go(5 + secs, temp=hot)
    assert r.st['stoss']['n'] == n and r.st['stoss']['step'] == 'an' and ac.level == 6
    r.go(5, temp=hi)
    assert not r.st['stoss'] and not r.st['corr'] and r.st['phase'] == 'normal' and ac.level == 5
    assert r.events('luefterstoss_ende')[-1]['trigger'] == 'Lüfterstöße beendet: wieder im Zielbereich (%d °C)' % hi
    r.go(120, temp=hi)
    assert miner.clock_sets == sets and r.st['clock_set'] == 300 and not r.events('hohe_temperatur')
    r.go(65, temp=(zb or (79, 84))[0] - 2)                       # nach 60 s unter L (bei L−1 kämen Lüfterstöße unten)
    assert r.st['clock_set'] == 305 and miner.clock() == 305


@pytest.mark.parametrize('zb', ZB)
def test_pulses_abort_at_u_plus_4_table_at_once(miner, zb):
    ac = FakeAC(level=5)
    r = started(miner, ac, level=5, learned={'5': LW}, zb=zb)
    r.go(30, temp=_hot(zb))
    assert r.st['stoss']['n'] == 1 and r.st['stoss']['step'] == 'aus'
    r.go(5, temp=_hot(zb, 4))
    assert not r.st['stoss'] and r.st['corr'] and ac.level == 7 and r.st['clock_set'] == 270 and miner.clock() == 270
    assert r.events('luefterstoss_ende')[-1]['trigger'] == 'Lüfterstöße abgebrochen: %d °C – Tabelle' % _hot(zb, 4)
    assert r.events('hohe_temperatur')[-1]['details']['takt_minus'] == 30


@pytest.mark.parametrize('zb', ZB)
def test_jump_to_u_plus_4_no_pulses(miner, zb):
    ac = FakeAC(level=5)
    r = started(miner, ac, level=5, learned={'5': LW}, zb=zb)
    r.go(5, temp=_hot(zb, 4))
    assert not r.events('luefterstoss') and not r.st.get('stoss')
    assert r.st['corr'] and ac.level == 7 and r.st['clock_set'] == 270


def test_pulses_up_to_95_with_range_89_92_table_from_96(miner):
    """Deckel 96 °C: bei 89–92 °C sind 93–95 °C die Zeilen ohne Taktwert (Lüfterstöße), ab 96 °C gilt die Tabelle."""
    ac = FakeAC(level=5)
    r = started(miner, ac, level=5, tmax=90, learned={'5': LW}, zb=(89, 92))
    r.go(10, temp=92)
    r.go(5, temp=95)
    assert r.st['stoss'] and r.st['clock_set'] == 300
    r.go(5, temp=96)
    assert not r.st['stoss'] and ac.level == 7 and r.st['clock_set'] == 200


def test_pulses_without_confirmation_go_on_after_30_s(miner):
    """Lüftersteuerung übernimmt nicht (Befehl schlägt fehl): jeder Schritt nach 30 s weiter, dann Korrekturphase."""
    ac = FakeAC(level=5)
    r = started(miner, ac, level=5, learned={'5': LW})
    r.go(30, temp=82)
    ac.fail = True
    start = r.now
    r.go(30, temp=85)
    assert r.st['stoss']['n'] == 1 and r.st['stoss']['step'] == 'an' and ac.level == 5
    r.go(5, temp=85)
    assert r.st['stoss']['step'] == 'aus' and r.st['log'][-1][1] == STOSS_WEITER
    r.go(85, temp=85)
    assert r.st['stoss']['n'] == 2 and r.st['clock_set'] == 300
    r.go(5, temp=85)
    assert not r.st['stoss'] and r.st['corr'] and r.st['clock_set'] == 290 and miner.clock() == 290
    assert r.events('takt')[-1]['recorded_at'] - start == timedelta(seconds=120)
    assert len([x for x in r.st['log'] if x[1] == STOSS_WEITER]) == 4


@pytest.mark.parametrize('zb', ZB)
def test_pending_command_goes_on_when_pulses_start(miner, zb):
    """Review 27.10.: der Stoßbeginn verwirft nichts (sonst liefen gesetzter Takt und Miner auseinander); verworfen wird
    erst beim Eintritt in die Korrekturphase."""
    ac = FakeAC(level=5)
    r = started(miner, ac, level=5, tmax=78, learned={'5': LW}, zb=zb)
    miner.ignore_clock = True
    r.go(115, temp=78)                                            # +5 gesendet, Miner übernimmt nicht: offen
    assert r.st['pending']['value'] == 305
    r.go(5, temp=_hot(zb))
    assert r.st['stoss'] and r.st['pending']['value'] == 305 and r.st['clock_set'] == 305
    r.go(70, temp=_hot(zb))                                       # Stöße fertig, Korrekturphase: Befehl abgelöst, 305 − 10
    assert r.st['corr'] and r.st['clock_set'] == 295


def test_start_in_the_round_of_the_crossing_sends_its_clock(miner):
    """Review 27.10.: Basisstufe gewählt in der Runde 83 → 85 °C – der Starttakt wird trotz Lüfterstößen gesendet."""
    ac = FakeAC(level=5)
    r = started(miner, ac, level=5, learned={'5': LW, '7': dict(LW, mhz=330)})
    r.go(30, temp=83)
    r.level = 7
    r.go(5, temp=85, level_at='neu')
    assert r.st['stoss'] and r.st['clock_set'] == 330
    r.go(15, temp=85)
    assert round(miner.clock()) == 330


@pytest.mark.parametrize('zb', ZB)
def test_new_pulses_only_after_u_minus_1_and_10_min(miner, zb):
    """Nutzer 27.10. (gegen Pendeln an U, „Beides“): neue Stöße erst nach einem Wert ≤ U−1 und höchstens ein Stoßpaar
    je 10 min; sonst gleich die Korrekturphase."""
    ac = FakeAC(level=5)
    r = started(miner, ac, level=5, learned={'5': LW}, zb=zb)
    r.go(50, temp=_hot(zb))                                       # Stoß 2 läuft
    r.go(5, temp=_hot(zb, 0))                                     # U: Stöße enden, kein ≤ U−1
    assert not r.st['stoss'] and not r.st['corr']
    r.go(5, temp=_hot(zb))
    assert r.st['corr'] and not r.st['stoss'] and len(r.events('luefterstoss')) == 2   # gleich korrigiert
    r.go(70, temp=_hot(zb, -1))                                   # U−1: Rückkehr, wieder frei – aber keine 10 min
    assert not r.st['corr'] and r.st['stoss_frei']
    r.go(5, temp=_hot(zb))
    assert r.st['corr'] and len(r.events('luefterstoss')) == 2
    r.go(70, temp=_hot(zb, -1))
    r.go(10 * 60, temp=_hot(zb, -2))                              # 10 min seit dem letzten Stoßpaar
    r.go(5, temp=_hot(zb))
    assert r.st['stoss']['n'] == 1 and len(r.events('luefterstoss')) == 3 and not r.st['corr']


@pytest.mark.parametrize('zb', ZB)
def test_no_pulses_during_takt_messung(miner, zb):
    ac = FakeAC(level=5)
    r = started(miner, ac, level=5, learned={'5': LW}, zb=zb)
    r.go(10, temp=84, cal=True)
    r.go(5, temp=86)                                              # Takt-Messung: Lüfterschutz fest ab 85 °C, sofort
    assert not r.events('luefterstoss') and r.st['corr'] and ac.level == 6


def test_takt_messung_beginning_ends_the_pulses(miner):
    ac = FakeAC(level=5)
    r = started(miner, ac, level=5, learned={'5': LW})
    r.go(10, temp=85)
    assert r.st['stoss'] and ac.level == 6
    r.go(10, temp=84, cal=True)
    assert not r.st['stoss'] and ac.level == 5 and not r.st['corr'] and not r.events('luefterstoss_ende')


def test_no_pulses_during_a_temperature_test_and_its_beginning_ends_them(miner):
    ac = FakeAC(level=2)
    r = started(miner, ac, level=2, learned={'2': {'mhz': 319.2, 'temp_avg': 82, 'at': 'x', 'count': 1}})
    r.go(10, temp=85)
    assert r.st['stoss'] and ac.level == 3
    r.kw['temp_test_at'] = r.now.isoformat()
    r.go(5, temp=85)
    assert not r.st['stoss'] and r.st['test'] and ac.level == 2 and not r.events('luefterstoss_ende')
    r.go(10, temp=83)
    r.go(60, temp=86)
    assert len(r.events('luefterstoss')) == 1 and ac.level == 2 and not r.st['corr']


def test_restart_during_pulses_clears_them(miner):
    ac = FakeAC(level=5)
    r = started(miner, ac, level=5, learned={'5': LW})
    r.go(10, temp=85)
    assert r.st['stoss']
    miner.elapsed = 10                                            # Neustart: Chips kalt
    r.go(5, temp=26)
    assert not r.st['stoss'] and r.st['heat'] and ac.level == 1 and not r.events('luefterstoss_ende')
    assert r.events('start')[-1]['trigger'] == 'Neustart des Miners'


def test_start_during_pulses_no_new_pulses_within_10_min(miner):
    """Basisstufe gewählt während der Stöße bei 85 °C: Stöße enden; neue erst nach ≤ U−1 und 10 min – also gleich die
    Korrekturphase (Starttakt −10)."""
    ac = FakeAC(level=5)
    r = started(miner, ac, level=5, learned={'5': LW})
    r.go(10, temp=85)
    r.level = 3
    r.go(5, temp=85, level_at='neu')
    assert not r.st['stoss'] and r.st['corr'] and ac.level == 4 and r.st['clock_set'] == 320
    assert len(r.events('luefterstoss')) == 1


def test_automatik_off_on_does_not_keep_the_pulses(miner):
    ac = FakeAC(level=5)
    r = started(miner, ac, level=5, learned={'5': LW})
    r.go(10, temp=85)
    r.go(10, enabled=False)
    r.go(5, temp=85, enabled=True)
    # Automatik ein: Stöße von vorher vergessen, auch die 10-min-Sperre – auch beim Start im heißen Zustand erst Stöße
    assert r.st['stoss']['n'] == 1 and r.st['stoss']['at'] == (r.now - timedelta(seconds=5)).isoformat()
    assert not r.st.get('corr') and ac.level == 6 and len(r.events('luefterstoss')) == 2


# ---------------------------------------------------------------- Initialwerte, 60 s unter L, Lüfterstöße unten (Nutzer 28.10.)

def test_initial_values_of_28_10():
    assert fa.START_CLOCK == {1: 170, 2: 250, 3: 330, 4: 380, 5: 400, 6: 420, 7: 440, 8: 440, 9: 440, 10: 440}


@pytest.mark.parametrize('level, mhz, mode', [(1, 170, 4), (4, 380, 3), (5, 400, 2), (7, 440, 0)])
def test_start_without_learned_value_new_initial_values(miner, level, mhz, mode):
    ac = FakeAC(level=level)
    r = started(miner, ac, level=level)
    assert r.st['clock_set'] == mhz and miner.clock() == mhz and miner.level == mode   # Modus folgt dem Takt
    assert r.events('start')[-1]['details'] == {'quelle': 'Initialwert', 'starttakt': mhz, 'basisstufe': level}


def test_plus_step_only_after_60_s_below_l(miner):
    ac = FakeAC(level=5)
    r = started(miner, ac, level=5, learned={'5': LW})
    r.go(30, temp=82)
    since = r.now
    r.go(60, temp=72)                                             # 55 s unter 79 °C: noch kein Plus-Schritt
    assert r.st['clock_set'] == 300 and r.st['below_since'] == since.isoformat()
    r.go(5, temp=72)                                              # 60 s: +5
    assert r.st['clock_set'] == 305 and miner.clock() == 305


def test_a_value_at_l_restarts_the_60_s(miner):
    ac = FakeAC(level=5)
    r = started(miner, ac, level=5, learned={'5': LW})
    r.go(30, temp=82)
    r.go(50, temp=72)
    r.go(5, temp=79)                                              # ein Wert ≥ L setzt zurück
    assert r.st['below_since'] is None
    r.go(60, temp=72)
    assert r.st['clock_set'] == 300
    r.go(5, temp=72)
    assert r.st['clock_set'] == 305


def test_lower_l_restarts_the_60_s(miner):
    """Neuer Zielbereich mit tieferem L: die 60 s zählen unter dem neuen L neu."""
    ac = FakeAC(level=5)
    r = started(miner, ac, level=5, learned={'5': LW}, zb=(82, 85))
    r.go(30, temp=83)
    r.go(50, temp=77)
    r.kw.update(target_low=79, target_high=84)
    r.go(60, temp=77)
    assert r.st['clock_set'] == 300 and not r.st.get('stoss')
    r.go(5, temp=77)
    assert r.st['clock_set'] == 305


def _cold(miner, ac, level=5, learned=None, **kw):
    """Basisstufe `level`, 82 °C, dann 60 s bei 78 °C (L−1 von 79–84): in der nächsten Runde wäre +5 fällig."""
    r = started(miner, ac, level=level, learned=learned or {str(level): LW}, **kw)
    r.go(30, temp=82)
    r.go(60, temp=78)
    assert r.st['clock_set'] == 300 and not r.st.get('stoss')
    return r


def test_cold_pulses_at_l_minus_1_then_plus_5(miner):
    """Nutzer 28.10.: bei genau L−1 statt des Plus-Schritts zuerst zwei Lüfterstöße unten (Basisstufe − 1, 6 s ab der
    Bestätigung, zurück), kein Takt; nach beiden Stößen noch L−1 → +5 in derselben Runde."""
    ac = FakeAC(level=5)
    r = _cold(miner, ac)
    start, n = r.now, len(ac.writes)
    r.go(5, temp=78)
    assert r.st['stoss'] == {'n': 1, 'step': 'an', 'at': start.isoformat(), 'level': 4, 'dir': 'down'}
    assert ac.level == 4 and r.st['clock_set'] == 300 and r.st['phase'] == 'normal'
    ev = r.events('luefterstoss')[-1]
    assert ev['trigger'] == 'Lüfterstoß unten 1/2: Stufe 4 für 6 s (Chiptemperatur 78 °C)'
    assert ev['fan_old'] == 5 and ev['fan_new'] == 4
    r.go(15, temp=78)
    assert r.st['stoss']['step'] == 'an' and r.st['stoss']['ok'] == (start + timedelta(seconds=10)).isoformat()
    r.go(5, temp=78)                                              # 6 s nach der Bestätigung: zurück
    assert r.st['stoss'] == {'n': 1, 'step': 'aus', 'at': (start + timedelta(seconds=20)).isoformat(), 'level': 5,
                             'dir': 'down'}
    r.go(15, temp=78)
    assert r.st['stoss']['n'] == 2 and r.st['stoss']['step'] == 'an' and ac.level == 4
    assert r.events('luefterstoss')[-1]['trigger'] == 'Lüfterstoß unten 2/2: Stufe 4 für 6 s (Chiptemperatur 78 °C)'
    r.go(30, temp=78)
    assert r.st['stoss']['n'] == 2 and r.st['stoss']['step'] == 'aus' and r.st['clock_set'] == 300
    assert miner.clock() == 300
    r.go(5, temp=78)                                              # Stoß 2 „aus“ bestätigt: fertig, +5
    assert not r.st['stoss'] and r.st['clock_set'] == 305 and miner.clock() == 305 and ac.level == 5
    assert ac.writes[n:] == [4, 5, 4, 5]
    assert r.events('luefterstoss_ende')[-1]['trigger'] == 'Lüfterstöße unten fertig: 78 °C, Takt wird erhöht'
    takt = r.events('takt')[-1]
    assert takt['recorded_at'] - start == timedelta(seconds=70) and takt['trigger'] == 'Chiptemperatur 78 °C: +5 MHz'


@pytest.mark.parametrize('secs, n', [(10, 1), (40, 2)])
def test_cold_pulses_end_back_in_the_target_range_without_clock_increase(miner, secs, n):
    ac = FakeAC(level=5)
    r = _cold(miner, ac)
    r.go(5 + secs, temp=78)
    assert r.st['stoss']['n'] == n and r.st['stoss']['step'] == 'an' and ac.level == 4
    r.go(5, temp=79)
    assert not r.st['stoss'] and ac.level == 5 and r.st['clock_set'] == 300
    assert r.events('luefterstoss_ende')[-1]['trigger'] == 'Lüfterstöße unten beendet: wieder im Zielbereich (79 °C)'
    r.go(120, temp=79)
    assert r.st['clock_set'] == 300 and miner.clock() == 300


def test_cold_pulses_abort_at_l_minus_2_plus_step_at_once(miner):
    ac = FakeAC(level=5)
    r = _cold(miner, ac)
    r.go(30, temp=78)
    assert r.st['stoss']['n'] == 1 and r.st['stoss']['step'] == 'aus'
    r.go(5, temp=77)
    assert not r.st['stoss'] and ac.level == 5 and r.st['clock_set'] == 305 and miner.clock() == 305
    assert r.events('luefterstoss_ende')[-1]['trigger'] == 'Lüfterstöße unten abgebrochen: 77 °C – Takterhöhung'


def test_cold_pulses_at_most_once_per_10_min(miner):
    ac = FakeAC(level=5)
    r = _cold(miner, ac)
    first = r.now
    r.go(75, temp=78)                                             # Stoßpaar, dann +5
    assert r.st['clock_set'] == 305 and len(r.events('luefterstoss')) == 2
    r.go(5, temp=80)                                              # L+1: wieder frei – aber keine 10 min
    assert r.st['stoss_unten_frei']
    r.go(185, temp=78)                                            # 60 s unter L, 3 min nach dem +5: gleich +5
    assert r.st['clock_set'] == 310 and len(r.events('luefterstoss')) == 2
    r.go((first + timedelta(minutes=10) - r.now).total_seconds(), temp=78)
    assert len(r.events('luefterstoss')) == 2
    r.go(40, temp=78)                                             # 10 min vorbei: das nächste Plus wieder mit Stößen
    assert len(r.events('luefterstoss')) == 3 and r.st['stoss']['dir'] == 'down'


def test_new_cold_pulses_only_after_a_value_at_l_plus_1(miner):
    ac = FakeAC(level=5)
    r = _cold(miner, ac)
    r.go(15 * 60, temp=78)                                        # über 10 min nur 78 °C: Plus-Schritte ohne Stöße
    assert len(r.events('luefterstoss')) == 2 and r.st['clock_set'] >= 315 and not r.st['stoss_unten_frei']
    clock = r.st['clock_set']
    r.go(5, temp=80)                                              # L+1
    r.go(60, temp=78)
    assert not r.st.get('stoss')
    r.go(185, temp=78)                                            # 60 s unter L: zweites Stoßpaar, dann +5
    pairs = [e for e in r.events('luefterstoss') if e['trigger'].startswith('Lüfterstoß unten 1/2')]
    assert len(pairs) == 2 and r.st['clock_set'] == clock + 5


def test_no_cold_pulses_at_base_level_1(miner):
    ac = FakeAC(level=1)
    r = _cold(miner, ac, level=1)
    r.go(5, temp=78)
    assert r.st['clock_set'] == 305 and not r.events('luefterstoss') and ac.level == 1


def test_no_cold_pulses_in_heat_mode(miner):
    ac = FakeAC(level=3)
    r = started(miner, ac, level=3, tmax=40, learned={'3': LW})
    assert r.st['heat'] and ac.level == 1
    r.go(125, temp=78)
    assert r.st['clock_set'] == 305 and not r.events('luefterstoss') and ac.level == 1


def test_no_cold_pulses_at_the_cap(miner):
    ac = FakeAC(level=5)
    r = started(miner, ac, level=5, learned={'5': LW})
    r.st['caps'] = {'5': 300}
    r.go(30, temp=82)
    r.go(300, temp=78)
    assert r.st['clock_set'] == 300 and not r.events('luefterstoss')


def test_hot_pulses_keep_their_texts_and_state(miner):
    ac = FakeAC(level=5)
    r = started(miner, ac, level=5, learned={'5': LW})
    r.go(5, temp=85)
    assert 'dir' not in r.st['stoss']
    assert r.events('luefterstoss')[-1]['trigger'] == 'Lüfterstoß 1/2: Stufe 6 für 6 s (Chiptemperatur 85 °C)'


# ---------------------------------------------------------------- Fehlerrate: Obergrenze (Nutzer 28.10.: 4 %, vorher 3)

def test_cap_after_5_min_above_4_percent_clock_stays(miner):
    ac = FakeAC(level=5)
    r = started(miner, ac, level=5, learned={'5': LW})
    miner.dh = 4.0
    r.go(10 * 60, temp=82)
    r.go(6 * 60, temp=82)                                         # 4 %: nicht über 4 % – keine Obergrenze
    assert not r.st.get('caps') and not r.events('obergrenze')
    miner.dh = 4.5
    r.go(5 * 60, temp=82)
    assert not r.st.get('caps') and not r.events('obergrenze')
    r.go(5, temp=82)                                              # 5 min lang jeder Wert über 4 %
    assert r.st['caps'] == {'5': 295} and r.st['clock_set'] == 300 and miner.clock() == 300
    ev = r.events('obergrenze')[-1]
    assert ev['trigger'] == ('Fehlerrate 4.5 % seit 5 min über 4 %: Obergrenze Stufe 5 = 295 MHz, Lernwert Stufe 5 auf '
                             '295 MHz begrenzt; kein automatischer Neustart (erst 30 min nach dem Start)')
    assert ev['details']['obergrenze'] == 295 and ev['details']['vorher'] is None
    r.go(5 * 60, temp=82)                                         # weitere 5 min: Neustart noch gesperrt (nach 26 min)
    assert len(r.events('obergrenze')) == 1 and r.st['caps'] == {'5': 295}   # nur niedriger
    assert len([n for n in r.st['log'] if 'kein automatischer Neustart' in n[1]]) == 1   # Grund nur einmal


@pytest.mark.parametrize('low', [4.0, None])
def test_cap_5_min_restart_with_a_low_or_missing_reading(miner, low):
    ac = FakeAC(level=5)
    r = started(miner, ac, level=5, learned={'5': LW})
    r.go(10 * 60, temp=82)
    miner.dh = 4.5
    r.go(250, temp=82)
    miner.dh = low
    r.go(5, temp=82)                                              # ≤ 4 % oder ohne Wert: die 5 min beginnen neu
    miner.dh = 4.5
    r.go(300, temp=82)
    assert not r.st.get('caps')
    r.go(5, temp=82)
    assert r.st['caps'] == {'5': 295}


def test_cap_5_min_restart_when_the_miner_does_not_answer(miner):
    ac = FakeAC(level=5)
    r = started(miner, ac, level=5, learned={'5': LW})
    r.go(10 * 60, temp=82)
    miner.dh = 4.5
    r.go(250, temp=82)
    miner.offline = True
    r.go(5)
    miner.offline = False
    r.go(300, temp=82)
    assert not r.st.get('caps')
    r.go(5, temp=82)
    assert r.st['caps'] == {'5': 295}


def test_cap_not_in_the_first_10_min_after_a_start(miner):
    ac = FakeAC(level=5)
    r = started(miner, ac, level=5, learned={'5': LW})            # Start bei T0
    miner.dh = 4.5
    r.go(15 * 60 - 10, temp=82)
    assert not r.st.get('caps')
    r.go(5, temp=82)                                              # 10 min + 5 min
    assert r.st['caps'] == {'5': 295}


@pytest.mark.parametrize('case', ['messung', 'test', 'wartung', 'overhot', 'offline'])
def test_no_cap_and_no_restart_during_takt_messung_test_wartung_over_hot(miner, monkeypatch, case):
    calls = []
    monkeypatch.setattr(calibration, 'reboot', lambda ip, port: calls.append(ip) or 'ok')
    ac = FakeAC(level=2)
    r = started(miner, ac, level=2, learned={'2': dict(LW, mhz=319.2)})
    r.st['start_at'] = (r.now - timedelta(hours=1)).isoformat()
    miner.dh = 4.5                                                # über 4 %: sonst Obergrenze und Neustart (28.10.)
    if case == 'messung':
        r.kw['cal'] = True
    elif case == 'test':
        r.kw['temp_test_at'] = r.now.isoformat()
    elif case == 'wartung':
        miner_cache.put('10.0.0.15', 'estats', 'x')               # A15 antwortet noch
        r.kw['maintenance_at'] = r.now.isoformat()
    elif case == 'offline':
        miner.offline = True                                      # Miner antwortet nicht
    else:
        miner.status = 'Over Hot'
    r.go(6 * 60, temp=82)
    miner_cache.clear('10.0.0.15')
    assert not r.st.get('caps') and not r.events('obergrenze') and not r.events('neustart_auto') and calls == []


def test_cap_limits_plus_steps(miner):
    ac = FakeAC(level=5)
    r = started(miner, ac, level=5, learned={'5': LW})
    r.st['caps'] = {'5': 303}
    r.go(30, temp=82)
    r.go(125, temp=72)
    assert r.st['clock_set'] == 303 and miner.clock() == 303      # +5 → höchstens die Obergrenze
    assert r.events('takt')[-1]['trigger'] == 'Chiptemperatur 72 °C: +3 MHz'
    r.go(5 * 60, temp=72)
    assert r.st['clock_set'] == 303                               # an der Obergrenze kein Plus-Schritt


def test_cap_of_another_stage_does_not_limit(miner):
    ac = FakeAC(level=5)
    r = started(miner, ac, level=5, learned={'5': LW})
    r.st['caps'] = {'4': 290}
    r.go(30, temp=82)
    r.go(65, temp=72)
    assert r.st['clock_set'] == 305


def _with_state(miner, ac, level, state, zb=(79, 84), tmax=82):
    """Zustand vorgegeben, dann Basisstufe gewählt (Start)."""
    r = Run(miner, ac, level)
    r.st = dict({'v': fa.STATE_VERSION, 'seen_at': T0.isoformat(), 'level_seen': 'x', 'zb': list(zb)}, **state)
    r.kw.update(level_at='klick', target_low=zb[0], target_high=zb[1])
    miner.tmax = tmax
    return r.go(10)


@pytest.mark.parametrize('learned, cap, mhz, quelle', [({'5': dict(LW, mhz=350)}, 320, 320, 'Lernwert'),
                                                        ({}, 380, 380, 'Initialwert'),
                                                        ({'5': LW}, 320, 300, 'Lernwert')])
def test_cap_limits_the_start_clock(miner, learned, cap, mhz, quelle):
    ac = FakeAC(level=5)
    r = _with_state(miner, ac, 5, {'learned': learned, 'caps': {'5': cap}})
    assert r.st['clock_set'] == mhz and miner.clock() == mhz
    assert r.events('start')[-1]['details']['quelle'] == quelle and r.events('start')[-1]['details']['starttakt'] == mhz


def test_caps_stay_on_automatik_off_on_range_change_and_start(miner):
    ac = FakeAC(level=5)
    r = started(miner, ac, level=5, learned={'5': LW})
    at = (T0 - timedelta(hours=1)).isoformat()
    r.st.update(caps={'5': 290, '3': 250}, caps_cleared_seen=at, auto_restart_at=at)
    r.go(10, temp=82)
    r.kw['enabled'] = False
    r.go(10)
    r.kw['enabled'] = True
    r.go(10, temp=82)
    assert r.st['caps'] == {'5': 290, '3': 250} and r.st['clock_set'] == 290   # Start nach Automatik ein: höchstens 290
    assert r.st['caps_cleared_seen'] == at and r.st['auto_restart_at'] == at
    r.kw.update(target_low=82, target_high=85)
    r.go(5, temp=83)
    assert r.st['caps'] == {'5': 290, '3': 250}
    r.kw['level_at'] = 'neu'
    r.go(10, temp=83)
    assert r.st['caps'] == {'5': 290, '3': 250} and r.events('start')[-1]['details']['quelle'] == 'Lernwert 79–84'
    assert r.st['clock_set'] == 290


def test_caps_cleared_by_the_user(miner):
    ac = FakeAC(level=5)
    r = started(miner, ac, level=5, learned={'5': LW})
    r.st['caps'] = {'5': 290, '3': 250}
    r.go(10, temp=82)
    cleared = r.now - timedelta(seconds=2)
    r.kw['caps_cleared_at'] = cleared                             # Eintrag obergrenzen_loeschen (Zeit aus der Datenbank)
    r.go(5, temp=82)
    assert r.st['caps'] == {} and r.st['caps_cleared_seen'] == cleared.isoformat()
    r.st['caps'] = {'5': 280}                                     # eine neue Obergrenze danach bleibt
    r.go(10, temp=82)
    assert r.st['caps'] == {'5': 280}
    r.kw['enabled'] = False
    r.go(10)
    r.kw.update(enabled=True, caps_cleared_at=(r.now - timedelta(seconds=1)).isoformat())   # gelöscht, während aus
    r.go(5, temp=82)
    assert r.st['caps'] == {}


def test_select_reads_caps_cleared_at(monkeypatch):
    seen = []

    class Cur:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def execute(self, sql, args=None):
            seen.append(sql)

        def fetchall(self):
            return []

    class Conn:
        def cursor(self, cursor_factory=None):
            return Cur()

        def close(self):
            pass

    monkeypatch.setenv('MINERSENTINEL_ENV', 'Live')
    monkeypatch.setattr(fa.psycopg2, 'connect', lambda url: Conn())
    fa.run_fan_automation('postgresql://x', ac=FakeAC())
    assert "l.kind = 'obergrenzen_loeschen') AS caps_cleared_at" in seen[0]
    # Automatik aus: ein neuerer Eintrag zum Löschen holt die Zeile trotzdem (Review 28.10.)
    assert ("'obergrenzen_loeschen') > COALESCE((f.state->>'caps_cleared_seen')::timestamptz, "
            "'-infinity'::timestamptz)") in seen[0]


# ---------------------------------------------------------------- Fehlerrate: automatischer Neustart (Nutzer 28.10.: ab 4 %)

class FakeSocket:
    """cgminer-Verbindung: recv liefert die Antworten der Reihe nach (b'' = Ende, eine Ausnahme wird geworfen)."""

    def __init__(self, replies=(b'',), send_error=None):
        self.replies, self.send_error, self.sent = list(replies), send_error, []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def sendall(self, data):
        if self.send_error:
            raise self.send_error
        self.sent.append(data)

    def recv(self, n):
        reply = self.replies.pop(0) if self.replies else b''
        if isinstance(reply, Exception):
            raise reply
        return reply


def _connect(monkeypatch, sock=None, error=None):
    conns = []

    def create_connection(addr, timeout=None):
        conns.append(addr)
        if error:
            raise error
        return sock
    monkeypatch.setattr(calibration.socket, 'create_connection', create_connection)
    return conns


REBOOT_REPLY = b'STATUS=S,When=1,Code=119,Msg=ASC 0 set info: reboot,Description=cgminer 4.11.1|\x00'


def test_reboot_helper_reply_no_reply_and_errors(monkeypatch):
    sock = FakeSocket([REBOOT_REPLY, b''])
    conns = _connect(monkeypatch, sock)
    assert calibration.reboot('10.0.0.15', None) == 'STATUS=S,When=1,Code=119,Msg=ASC 0 set info: reboot,' \
                                                  'Description=cgminer 4.11.1|'
    assert conns == [('10.0.0.15', 4028)] and sock.sent == [b'ascset|0,reboot,0']
    _connect(monkeypatch, FakeSocket([OSError('timed out')]))
    assert calibration.reboot('10.0.0.15', 4028) == calibration.REBOOT_NO_REPLY   # gesendet, keine Antwort
    _connect(monkeypatch, FakeSocket([b'']))
    assert calibration.reboot('10.0.0.15', 4028) == calibration.REBOOT_NO_REPLY
    _connect(monkeypatch, error=OSError('Connection refused'))
    with pytest.raises(OSError):
        calibration.reboot('10.0.0.15', 4028)
    _connect(monkeypatch, FakeSocket(send_error=OSError('Broken pipe')))
    with pytest.raises(OSError):
        calibration.reboot('10.0.0.15', 4028)


def test_auto_restart_after_5_min_above_4_percent_cap_first(miner, monkeypatch):
    """Nutzer 28.10.: 5 min über 4 % beim selben Takt → Obergrenze und sofort der Neustart (vorher erst 5 min ab 10 %)."""
    sock = FakeSocket([REBOOT_REPLY, b''])
    conns = _connect(monkeypatch, sock)
    ac = FakeAC(level=5)
    r = started(miner, ac, level=5, learned={'5': LW})            # Start bei T0
    r.go(25 * 60, temp=82)
    miner.dh = 4.5
    r.go(5 * 60, temp=82)
    assert not r.events('neustart_auto') and not r.st.get('caps') and conns == []
    r.go(5, temp=82)                                              # 5 min über 4 %, 30 min nach dem Start
    at = (r.now - timedelta(seconds=5)).isoformat()
    ev = r.events('neustart_auto')
    assert len(ev) == 1 and ev[0]['trigger'] == 'Automatischer Neustart: Fehlerrate 4.5 % seit 5 min über 4 % (Takt 300 MHz)'
    assert ev[0]['ok'] is True and ev[0]['request'] == 'ascset|0,reboot,0' and ev[0]['response'].startswith('STATUS=S')
    assert conns == [('10.0.0.15', 4028)] and sock.sent == [b'ascset|0,reboot,0']
    assert r.st['auto_restart_at'] == at and r.st['restart_req'] == at
    kinds = [k for k in r.j.kinds() if k in ('obergrenze', 'neustart_auto')]
    assert kinds == ['obergrenze', 'neustart_auto'] and r.st['caps'] == {'5': 295}
    assert r.events('obergrenze')[-1]['trigger'] == ('Fehlerrate 4.5 % seit 5 min über 4 %: Obergrenze Stufe 5 = '
                                                     '295 MHz, Lernwert Stufe 5 auf 295 MHz begrenzt')
    assert r.st['learned']['5']['mhz'] == 295                     # Lernwert 300 auf die Obergrenze begrenzt
    assert r.st['clock_set'] == 300                               # der Takt bleibt
    miner.offline = True                                          # die A15 startet neu: erwartet, kein Fehler
    r.go(30)
    assert r.st['offline'] == 'neustart' and not r.events('miner_offline') and r.events('neustart_offline')
    miner.offline, miner.elapsed, miner.dh = False, 10, 1.0
    r.go(5, temp=26)
    start = r.events('start')[-1]
    assert start['trigger'] == 'Neustart des Miners' and start['details']['starttakt'] == 295   # Lernwert, höchstens 295
    assert r.st['heat'] and r.st.get('start_hold') and not r.st.get('restart_req')


def test_after_the_auto_restart_start_with_a_learned_value_below_the_cap(miner, monkeypatch):
    conns = _connect(monkeypatch, FakeSocket([b'']))
    ac = FakeAC(level=5)
    r = started(miner, ac, level=5, learned={'5': LW})
    r.st['start_at'] = (r.now - timedelta(hours=1)).isoformat()
    r.st['learned'] = {'5': dict(LW, mhz=280)}                    # Lernwert unter der kommenden Obergrenze 295
    miner.dh = 4.5
    r.go(5 * 60 + 5, temp=82)
    assert len(conns) == 1 and r.st['caps'] == {'5': 295} and r.st['learned'] == {'5': dict(LW, mhz=280)}
    assert r.events('obergrenze')[-1]['trigger'] == 'Fehlerrate 4.5 % seit 5 min über 4 %: Obergrenze Stufe 5 = 295 MHz'
    assert r.events('obergrenze')[-1]['details']['lernwerte_vorher'] is None
    miner.offline = True
    r.go(30)
    miner.offline, miner.elapsed, miner.dh = False, 10, 1.0
    r.go(5, temp=26)
    assert r.events('start')[-1]['details']['starttakt'] == 280 and r.st['clock_set'] == 280   # min(Lernwert, Obergrenze)


def test_no_auto_restart_within_30_min_after_a_start_only_the_cap(miner, monkeypatch):
    conns = _connect(monkeypatch, FakeSocket([b'']))
    ac = FakeAC(level=5)
    r = started(miner, ac, level=5, learned={'5': LW})            # Start bei T0
    miner.dh = 4.5
    r.go(15 * 60 - 10, temp=82)
    assert not r.st.get('caps')
    r.go(5, temp=82)                                              # 10 min nach dem Start, dann 5 min über 4 %
    ev = r.events('obergrenze')[-1]
    assert ev['trigger'] == ('Fehlerrate 4.5 % seit 5 min über 4 %: Obergrenze Stufe 5 = 295 MHz, Lernwert Stufe 5 auf '
                             '295 MHz begrenzt; kein automatischer Neustart (erst 30 min nach dem Start)')
    assert ev['details']['neustart_gesperrt'] == 'erst 30 min nach dem Start'
    assert r.st['caps'] == {'5': 295} and not r.events('neustart_auto') and conns == []
    r.go(15 * 60 - 5, temp=82)                                    # nach 20 und 25 min: weiter gesperrt
    assert not r.events('neustart_auto') and conns == [] and len(r.events('obergrenze')) == 1
    assert len([n for n in r.st['log'] if 'kein automatischer Neustart' in n[1]]) == 1
    r.go(5, temp=82)                                              # 30 min nach dem Start: die nächsten 5 min über 4 %
    assert len(r.events('neustart_auto')) == 1 and r.events('neustart_auto')[0]['response'] == calibration.REBOOT_NO_REPLY


def test_auto_restart_at_most_once_per_3_h_only_the_cap(miner, monkeypatch):
    conns = _connect(monkeypatch, FakeSocket([b'']))
    ac = FakeAC(level=5)
    r = started(miner, ac, level=5, learned={'5': LW})
    r.st['start_at'] = (r.now - timedelta(hours=1)).isoformat()
    last = r.now - timedelta(hours=3) + timedelta(minutes=7)      # letzter automatischer Neustart: Sperre noch 7 min
    r.st['auto_restart_at'] = last.isoformat()
    miner.dh = 4.5
    r.go(5 * 60 + 5, temp=82)                                     # 5 min über 4 %: nur die Obergrenze
    hm = last.astimezone(fa.LOCAL_TZ).strftime('%H:%M')
    ev = r.events('obergrenze')[-1]
    assert ev['trigger'].endswith('; kein automatischer Neustart (höchstens einer je 3 h, letzter um %s)' % hm)
    assert r.st['caps'] == {'5': 295} and not r.events('neustart_auto') and conns == []
    r.go(5 * 60 - 5, temp=82)                                     # Sperre nach 7 min vorbei: Neustart erst mit den nächsten 5 min
    assert conns == []
    r.go(5, temp=82)
    assert len(conns) == 1 and len(r.events('neustart_auto')) == 1 and len(r.events('obergrenze')) == 1


def test_blocked_restart_without_a_new_cap_noted_once(miner, monkeypatch):
    """Obergrenze schon da (z. B. Ertrags-Prüfung), Neustart gesperrt: nur ein Hinweis mit dem Grund, einmal je Grund."""
    conns = _connect(monkeypatch, FakeSocket([b'']))
    ac = FakeAC(level=5)
    r = started(miner, ac, level=5, learned={'5': LW})
    r.st['start_at'] = (r.now - timedelta(hours=1)).isoformat()
    last = r.now - timedelta(hours=3) + timedelta(minutes=12)
    r.st.update(caps={'5': 295}, auto_restart_at=last.isoformat())
    miner.dh = 4.5
    r.go(10 * 60 + 5, temp=82)                                    # nach 5 und 10 min über 4 %: gesperrt
    text = ('Fehlerrate 4.5 % seit 5 min über 4 %: kein automatischer Neustart (höchstens einer je 3 h, letzter um '
            + last.astimezone(fa.LOCAL_TZ).strftime('%H:%M') + ').')
    assert [n[1] for n in r.st['log'] if 'kein automatischer Neustart' in n[1]] == [text]
    assert not r.events('obergrenze') and conns == [] and r.st['learned']['5']['mhz'] == 300   # keine neue Obergrenze
    r.go(5 * 60, temp=82)
    assert len(conns) == 1 and len(r.events('neustart_auto')) == 1


def test_auto_restart_failure_retries_after_5_min_and_does_not_count(miner, monkeypatch):
    conns = _connect(monkeypatch, error=OSError('[Errno 61] Connection refused'))
    ac = FakeAC(level=5)
    r = started(miner, ac, level=5, learned={'5': LW})
    r.st['start_at'] = (r.now - timedelta(hours=1)).isoformat()
    miner.dh = 4.5
    r.go(5 * 60 + 5, temp=82)
    assert len(conns) == 1 and not r.events('neustart_auto') and r.st['caps'] == {'5': 295}
    err = [e for e in r.events('fehler') if e['details'].get('grund') == 'neustart_auto'][-1]
    assert err['ok'] is False and 'Connection refused' in err['error']
    assert err['trigger'] == 'Automatischer Neustart: Befehl nicht gesendet'
    assert r.st['log'][-1][1].startswith('Automatischer Neustart fehlgeschlagen: [Errno 61] Connection refused')
    assert not r.st.get('auto_restart_at') and not r.st.get('restart_req')
    r.go(5 * 60 - 5, temp=82)
    assert len(conns) == 1                                        # nächster Versuch frühestens nach 5 min
    sock = FakeSocket([b''])
    _connect(monkeypatch, sock)
    r.go(5, temp=82)                                              # weitere 5 min über 4 %: erneut
    assert len(r.events('neustart_auto')) == 1 and sock.sent == [b'ascset|0,reboot,0'] and r.st['auto_restart_at']
    assert len(r.events('obergrenze')) == 1                       # die Obergrenze nur einmal


def test_no_cap_and_no_restart_at_exactly_4_percent(miner, monkeypatch):
    conns = _connect(monkeypatch, FakeSocket([b'']))
    ac = FakeAC(level=5)
    r = started(miner, ac, level=5, learned={'5': LW})
    r.st['start_at'] = (r.now - timedelta(hours=1)).isoformat()
    miner.dh = 4.0
    r.go(30 * 60, temp=82)                                        # genau 4 %: nicht über 4 %
    assert conns == [] and not r.st.get('caps') and not r.events('neustart_auto')
    miner.dh = 4.5
    r.go(250, temp=82)
    miner.dh = 4.0
    r.go(5, temp=82)                                              # ein Wert von 4 %: die 5 min beginnen neu
    miner.dh = 4.5
    r.go(300, temp=82)
    assert conns == [] and not r.st.get('caps')
    r.go(5, temp=82)
    assert len(conns) == 1 and r.st['caps'] == {'5': 295}


# ---------------------------------------------------------------- Lernen nur bei Fehlerrate unter 4 % (Nutzer 28.10.)

def test_learned_value_discarded_at_mean_error_rate_of_4_percent(miner):
    ac = FakeAC(level=4)
    old = {'mhz': 280, 'temp_avg': 82, 'at': 'x', 'count': 1}
    miner.dh = 4.0
    r = started(miner, ac, level=4, learned={'4': dict(old)})
    r.go(15 * 60 + 30, temp=82)
    assert r.st['learned']['4'] == old and not r.events('lernwert') and not r.st.get('caps')   # 4 %: keine Obergrenze
    end = r.events('lernen_ende')[-1]
    assert end['details']['ergebnis'] == 'verworfen' and end['details']['grund'] == 'Fehlerrate 4.0 % (Grenze 4 %)'
    assert r.st['learn_since'] == (T0 + timedelta(minutes=15)).isoformat()   # die Lernperiode beginnt neu
    miner.dh = 3.9                                                # unter 4 % (vorher galt 3 %)
    r.go(15 * 60, temp=82)
    assert r.st['learned']['4']['count'] == 2 and r.events('lernwert')


# ---------------------------------------------------------------- Lernwert des nächsten Bereichs (Nutzer 28.10.)

A = dict(LW, mhz=360)
B = dict(LW, mhz=280)


@pytest.mark.parametrize('zb, archive, mhz, quelle', [
    ((82, 84), {'76-79': {'5': A}, '85-88': {'5': B}, '89-92': {'5': dict(LW, mhz=260)}}, 280, 'Lernwert 85–88'),
    ((82, 85), {'79-84': {'5': A}, '85-86': {'5': B}}, 360, 'Lernwert 79–84'),          # gleich weit: der kühlere
    ((82, 84), {'76-79': {'5': A}, '85-88': {'3': B}}, 360, 'Lernwert 76–79'),          # ohne die Stufe: der nächste mit ihr
    ((82, 84), {'85-88': {'3': B}}, 400, 'Initialwert'),
])
def test_start_with_the_learned_value_of_the_nearest_range(miner, zb, archive, mhz, quelle):
    ac = FakeAC(level=5)
    r = _with_state(miner, ac, 5, {'learned': {}, 'learned_zb': archive}, zb=zb)
    start = r.events('start')[-1]
    assert start['details']['quelle'] == quelle and r.st['clock_set'] == mhz and miner.clock() == mhz
    assert r.st['learned'] == {}                                  # wird nicht übernommen, nur verwendet


def test_nearest_range_value_is_capped(miner):
    ac = FakeAC(level=5)
    r = _with_state(miner, ac, 5, {'learned': {}, 'learned_zb': {'85-88': {'5': B}}, 'caps': {'5': 270}}, zb=(82, 84))
    assert r.events('start')[-1]['details']['quelle'] == 'Lernwert 85–88' and r.st['clock_set'] == 270


def test_temp_test_begins_with_nearest_range_value_and_the_cap_steps_are_not_limited(miner):
    ac = FakeAC(level=5)
    r = _with_state(miner, ac, 5, {'learned': {}, 'learned_zb': {'85-88': {'5': B}}, 'caps': {'5': 270}}, zb=(82, 84))
    r.kw['temp_test_at'] = r.now.isoformat()
    r.go(10, temp=85)
    assert r.st['test']['clock'] == 270 and r.st['clock_set'] == 270
    assert any(n[1].startswith('Temperatur-Test Beginn: Lernwert 85–88 Stufe 5 (270 MHz)') for n in r.st['log'])
    r.go(5, temp=83)                                              # ≤ L+1: Schritt 1, +30 über die Obergrenze (Test des Nutzers)
    assert r.st['test']['step'] == 1 and r.st['clock_set'] == 300


# ---------------------------------------------------------------- Ertrags-Prüfung (Nutzer 28.10.)

def _ghs(gain, top=300):
    """Hashrate laut Miner: bis `top` MHz 150 TH/s, darüber 150 TH/s + gain."""
    return lambda mn: 150000.0 if mn.clock() <= top else 150000.0 + gain * 1000


def _plus5(miner, ac, gain, learned=300, level=5, warm=15 * 60, dip=None, before=150.0):
    """Lernwert `learned`, Start vor 1 h; `warm` bei 82 °C (15 min: das ganze Fenster vor dem Schritt hat Hashrate),
    dann 74 °C (L−5: +5 ohne Stöße und ohne die 3 min): der +5-Schritt ist übernommen, die Ertrags-Prüfung läuft seit
    der letzten Runde. `dip` = (von, bis) in min vor dem Schritt: Hashrate 10 TH/s tiefer (kurzer Firmware-Einbruch)."""
    r = started(miner, ac, level=level, learned={str(level): dict(LW, mhz=learned)})
    r.st['start_at'] = (r.now - timedelta(hours=1)).isoformat()
    ghs = _ghs(gain, learned)
    step = r.now + timedelta(seconds=warm + 60)                   # Runde des +5-Schritts
    if dip:
        lo, hi = step - timedelta(minutes=dip[0]), step - timedelta(minutes=dip[1])
        miner.ghs = lambda mn: ghs(mn) - (10000.0 if lo <= r.now < hi else 0.0)
    else:
        miner.ghs = ghs
    r.go(warm, temp=82)
    r.go(60, temp=74)
    assert r.st['clock_set'] == learned                           # 55 s unter L: noch kein Plus-Schritt
    r.go(5, temp=74)                                              # Runde `step`, 60 s unter L: +5 in die Folge
    assert r.st['clock_set'] == learned + 5 and not r.st.get('ertrag')
    for _ in range(6):                                            # übernommen (mit Moduswechsel später): Prüfung
        r.go(5, temp=74)
        if r.st.get('ertrag'):
            break
    at = r.now - timedelta(seconds=5)
    assert r.st['ertrag'] == {'at': at.isoformat(), 'from': learned, 'to': learned + 5, 'before': before}
    assert miner.clock() == learned + 5
    return r


@pytest.mark.parametrize('dip, before', [((10, 9), round(150 - 12 * 10 / 181, 2)), ((17, 16), 150.0)])
def test_yield_check_before_is_the_mean_of_the_15_min_before_the_step(miner, dip, before):
    """Nutzer 28.10.: Mittel der 15 min vor dem Schritt (181 Werte) – ein Einbruch 10 min vorher zählt (mit 12 Werten
    à −10 TH/s), einer 17 min vorher nicht, obwohl er noch in der gemerkten Hashrate (20 min) liegt."""
    ac = FakeAC(level=5)
    r = _plus5(miner, ac, gain=1.0, warm=20 * 60, dip=dip, before=before)
    assert min(v for _, v in r.st['ths']) == 140.0


def test_yield_check_loss_goes_back_and_sets_the_cap(miner):
    ac = FakeAC(level=5)
    r = _plus5(miner, ac, gain=-1.2)
    r.go(595, temp=74)                                            # 10 min kein Plus-Schritt
    assert r.st['clock_set'] == 305 and r.st['ertrag'] and not r.events('ertrag')
    r.go(5, temp=74)                                              # 10 min nach der Übernahme
    ev = r.events('ertrag')[-1]
    assert ev['trigger'] == ('Ertrags-Prüfung: +5 MHz (300 → 305 MHz) brachte −1,2 TH/s: zurück auf 300 MHz, '
                             'Obergrenze Stufe 5 = 300 MHz')
    assert ev['details']['ertrag_ths'] == -1.2 and ev['details']['zurueck'] is True
    assert ev['clock_old'] == 305 and ev['clock_new'] == 300
    assert r.st['caps'] == {'5': 300} and r.st['clock_set'] == 300 and r.st['ertrag'] is None
    assert miner.clock() == 300 and r.events('takt')[-1]['trigger'] == 'Ertrags-Prüfung: zurück auf 300 MHz'
    assert any(n[1] == ev['trigger'] + '.' for n in r.st['log'])
    r.go(10 * 60, temp=74)
    assert r.st['clock_set'] == 300 and len(r.events('ertrag')) == 1   # an der Obergrenze kein Plus-Schritt


@pytest.mark.parametrize('gain, text', [(0.2, '+0,2'), (-0.5, '−0,5')])   # −0,5: noch kein Verlust
def test_yield_check_small_gain_sets_the_cap_clock_stays(miner, gain, text):
    ac = FakeAC(level=5)
    r = _plus5(miner, ac, gain=gain)
    r.go(600, temp=74)
    ev = r.events('ertrag')[-1]
    assert ev['trigger'] == ('Ertrags-Prüfung: +5 MHz (300 → 305 MHz) brachte %s TH/s (unter 0,5): '
                             'Obergrenze Stufe 5 = 300 MHz' % text)
    assert ev['details']['zurueck'] is False and ev['details']['obergrenze_vorher'] is None
    assert r.st['caps'] == {'5': 300} and r.st['clock_set'] == 305 and miner.clock() == 305
    r.go(10 * 60, temp=74)
    assert r.st['clock_set'] == 305                               # über der Obergrenze: kein Plus-Schritt


@pytest.mark.parametrize('gain, cap, back', [(0.46, {'5': 300}, False), (-0.54, {'5': 300}, True)])
def test_yield_check_limits_use_the_unrounded_gain(miner, gain, cap, back):
    """Review 28.10.: +0,46 ist unter 0,5 (Obergrenze), −0,54 ist ein Verlust (zurück) – im Text gerundet."""
    ac = FakeAC(level=5)
    r = _plus5(miner, ac, gain=gain)
    r.go(600, temp=74)
    assert r.st['caps'] == cap and (r.st['clock_set'] == 300) is back


def test_yield_check_lowers_only_an_existing_cap(miner):
    ac = FakeAC(level=5)
    r = _plus5(miner, ac, gain=0.0)
    r.st['caps'] = {'5': 320}
    r.go(600, temp=74)
    assert r.st['caps'] == {'5': 300} and r.events('ertrag')[-1]['details']['obergrenze_vorher'] == 320


def test_yield_check_gain_from_0_5_only_a_note(miner):
    ac = FakeAC(level=5)
    r = _plus5(miner, ac, gain=0.5)
    r.go(595, temp=74)
    assert r.st['clock_set'] == 305
    r.go(5, temp=74)                                              # Prüfung, danach in derselben Runde +5
    assert not r.events('ertrag') and not r.st.get('caps') and r.st['ertrag'] is None
    assert any(n[1] == 'Ertrags-Prüfung: +5 MHz (300 → 305 MHz) brachte +0,5 TH/s.' for n in r.st['log'])
    assert r.st['clock_set'] == 310


def test_yield_check_step_back_follows_the_mode_rules(miner):
    """360 → 365 MHz braucht mode 03 (Modus zuerst); zurück auf 360 bleibt mode 03 (Spielraum 20 MHz)."""
    ac = FakeAC(level=5)
    r = _plus5(miner, ac, gain=-2.0, learned=360)
    assert miner.mode_sets == [3]
    r.go(600, temp=74)
    assert r.st['clock_set'] == 360 and r.st['caps'] == {'5': 360}
    r.go(10, temp=74)
    assert miner.clock() == 360 and miner.mode_sets == [3] and miner.level == 3


@pytest.mark.parametrize('temp, cap, size', [(65, None, 20), (55, None, 40), (74, 303, 3)])
def test_no_yield_check_for_20_40_and_shortened_steps(miner, temp, cap, size):
    ac = FakeAC(level=5)
    r = started(miner, ac, level=5, learned={'5': LW})
    r.st['start_at'] = (r.now - timedelta(hours=1)).isoformat()
    if cap:
        r.st['caps'] = {'5': cap}
    miner.ghs = _ghs(-1.2)
    r.go(15 * 60, temp=82)                                        # das ganze Fenster vor dem Schritt hat Hashrate
    r.go(70, temp=temp)
    assert r.st['clock_set'] == 300 + size and miner.clock() == 300 + size and not r.st.get('ertrag')
    r.go(15 * 60, temp=82)
    assert not r.events('ertrag') and r.st.get('caps', {}) == ({'5': cap} if cap else {})


def test_no_yield_check_within_30_min_after_a_start(miner):
    ac = FakeAC(level=5)
    r = started(miner, ac, level=5, learned={'5': LW})            # Start bei T0
    miner.ghs = _ghs(-1.2)
    r.go(15 * 60, temp=82)                                        # Schritt nach gut 16 min: noch in den 30 min
    r.go(70, temp=74)
    assert r.st['clock_set'] == 305 and not r.st.get('ertrag')
    r.go(30, temp=74)                                             # kein 10-min-Halt: nach dem Fenster gleich +5
    assert r.st['clock_set'] == 310
    r.go(15 * 60, temp=82)
    assert not r.events('ertrag') and not r.st.get('caps')


def test_no_yield_check_without_hashrate_before_the_step(miner):
    ac = FakeAC(level=5)
    r = started(miner, ac, level=5, learned={'5': LW})
    r.st['start_at'] = (r.now - timedelta(hours=1)).isoformat()
    miner.ghs = None
    r.go(15 * 60, temp=82)
    r.go(70, temp=74)
    assert r.st['clock_set'] == 305 and not r.st.get('ertrag')
    assert len(r.st['ths']) == 2                                  # nur die zwei Werte des Starts, älter als 15 min
    miner.ghs = _ghs(-1.2)
    r.go(15 * 60, temp=82)
    assert not r.events('ertrag') and not r.st.get('caps')


def test_yield_check_cancelled_without_hashrate_after_the_step(miner):
    ac = FakeAC(level=5)
    r = _plus5(miner, ac, gain=-1.2)
    r.go(5 * 60 - 5, temp=74)
    miner.ghs = 0.0                                               # keine Hashrate in den letzten 5 min
    r.go(5 * 60 + 5, temp=74)                                     # 10 min nach der Übernahme
    assert not r.events('ertrag') and not r.st.get('caps') and r.st['ertrag'] is None
    assert r.st['clock_set'] == 310                               # Prüfung vorbei: Plus-Schritte wieder möglich


def test_yield_check_keeps_20_min_of_hashrate(miner):
    ac = FakeAC(level=5)
    r = _plus5(miner, ac, gain=1.0)
    r.go(20 * 60, temp=82)
    hist = r.st['ths']
    assert len(hist) == 241 and fa._age(hist[0][0], r.now - timedelta(seconds=5)) == 20 * 60
    assert hist[-1] == [(r.now - timedelta(seconds=5)).isoformat(), 151.0]


def test_yield_check_blocks_cold_pulses_for_10_min(miner):
    ac = FakeAC(level=5)
    r = _plus5(miner, ac, gain=2.5)
    r.go(595, temp=78)                                            # L−1: Stöße unten wären fällig – Prüfung läuft
    assert not r.events('luefterstoss') and r.st['clock_set'] == 305 and r.st['ertrag']
    r.go(5, temp=78)                                              # 10 min: Prüfung (+2,5 TH/s), dann die Stöße unten
    assert r.st['ertrag'] is None and r.st['stoss']['dir'] == 'down' and not r.events('ertrag')


def test_yield_check_after_the_plus_5_of_the_cold_pulses(miner):
    ac = FakeAC(level=5)
    r = _cold(miner, ac)
    r.st['start_at'] = (r.now - timedelta(hours=1)).isoformat()
    miner.ghs = _ghs(-1.2)
    r.go(75, temp=78)                                             # Stoßpaar, dann +5
    assert r.st['clock_set'] == 305
    r.go(5, temp=78)
    assert r.st['ertrag']['to'] == 305
    r.go(600, temp=78)
    assert r.events('ertrag')[-1]['details']['zurueck'] is True and r.st['caps'] == {'5': 300}


@pytest.mark.parametrize('case', ['hot', 'start', 'start_same_clock', 'messung', 'test', 'wartung', 'overhot',
                                  'offline', 'aus', 'firmware'])
def test_yield_check_cancelled_by_another_clock_change_or_a_pause(miner, case):
    ac = FakeAC(level=5)
    r = _plus5(miner, ac, gain=-1.2)
    r.go(60, temp=74)
    temp = 74
    if case == 'hot':
        temp = 90                                                 # U+6: Takt −50
    elif case == 'start':
        r.kw['level_at'] = 'neu'
    elif case == 'start_same_clock':
        r.st['learned'] = {'5': dict(LW, mhz=305)}                # Starttakt = jetziger Takt: kein Befehl
        r.kw['level_at'] = 'neu'
    elif case == 'messung':
        r.kw['cal'] = True
    elif case == 'test':
        r.kw['temp_test_at'] = r.now.isoformat()
    elif case == 'wartung':
        miner_cache.put('10.0.0.15', 'estats', 'x')               # A15 antwortet noch
        r.kw['maintenance_at'] = r.now.isoformat()
    elif case == 'overhot':
        miner.status = 'Over Hot'
    elif case == 'offline':
        miner.offline = True
    elif case == 'aus':
        r.kw['enabled'] = False
    else:
        miner.points = fa.points_for(STOCK['2/L4'], 340)          # der Miner ändert den Takt selbst
    r.go(10, temp=temp)
    assert r.st.get('ertrag') is None
    miner_cache.clear('10.0.0.15')
    miner.status, miner.offline = 'Work: In Work', False
    r.kw.update(cal=False, temp_test_at=None, maintenance_at=None, enabled=True)
    r.go(11 * 60, temp=82)
    assert not r.events('ertrag') and not r.st.get('caps')


# ---------------------------------------------------------------- Neue Obergrenze begrenzt die Lernwerte (Nutzer 28.10.)

def test_error_rate_cap_limits_the_learned_values_of_the_stage_in_all_ranges(miner):
    ac = FakeAC(level=5)
    r = started(miner, ac, level=5, learned={'5': LW, '4': dict(LW, mhz=320)})
    archive = {'82-85': {'5': dict(LW, mhz=320), '4': dict(LW, mhz=330)}, '76-79': {'5': dict(LW, mhz=280)}}
    r.st['learned_zb'] = archive
    miner.dh = 4.5
    r.go(15 * 60 - 10, temp=82)
    assert not r.st.get('caps') and r.st['learned']['5'] == LW
    r.go(5, temp=82)                                              # 10 min nach dem Start, dann 5 min über 4 %
    assert r.st['caps'] == {'5': 295}
    assert r.st['learned'] == {'5': dict(LW, mhz=295), '4': dict(LW, mhz=320)}   # andere Stufe bleibt
    assert r.st['learned_zb'] == {'82-85': {'5': dict(LW, mhz=295), '4': dict(LW, mhz=330)},
                                  '76-79': {'5': dict(LW, mhz=280)}}              # 280 liegt schon darunter
    assert archive['82-85']['5']['mhz'] == 320                    # neue dicts: der Zustand davor bleibt unverändert
    ev = r.events('obergrenze')[-1]
    assert ev['trigger'] == ('Fehlerrate 4.5 % seit 5 min über 4 %: Obergrenze Stufe 5 = 295 MHz, Lernwerte Stufe 5 auf '
                             '295 MHz begrenzt (79–84, 82–85); kein automatischer Neustart (erst 30 min nach dem Start)')
    assert ev['details']['lernwerte_vorher'] == {'79-84': 300, '82-85': 320}
    assert any(n[1] == ev['trigger'] + '.' for n in r.st['log'])
    r.kw.update(target_low=82, target_high=85)                    # zurück im Bereich 82–85: begrenzter Lernwert
    r.go(5, temp=83)
    assert r.st['learned'] == {'5': dict(LW, mhz=295), '4': dict(LW, mhz=330)}
    r.kw['level_at'] = 'neu'
    r.go(10, temp=83)
    assert r.events('start')[-1]['details']['starttakt'] == 295


def test_yield_check_cap_limits_the_learned_values_at_or_below_stay(miner):
    ac = FakeAC(level=5)
    r = _plus5(miner, ac, gain=-1.2)
    r.st['learned'] = {'5': dict(LW, mhz=300)}                    # genau die kommende Obergrenze: bleibt
    r.st['learned_zb'] = {'82-85': {'5': dict(LW, mhz=310)}, '76-79': {'5': dict(LW, mhz=290)}}
    r.go(600, temp=74)
    ev = r.events('ertrag')[-1]
    assert ev['trigger'] == ('Ertrags-Prüfung: +5 MHz (300 → 305 MHz) brachte −1,2 TH/s: zurück auf 300 MHz, '
                             'Obergrenze Stufe 5 = 300 MHz, Lernwert Stufe 5 auf 300 MHz begrenzt (82–85)')
    assert ev['details']['lernwerte_vorher'] == {'82-85': 310}
    assert r.st['caps'] == {'5': 300} and r.st['learned'] == {'5': dict(LW, mhz=300)}
    assert r.st['learned_zb'] == {'82-85': {'5': dict(LW, mhz=300)}, '76-79': {'5': dict(LW, mhz=290)}}


def test_yield_check_cap_limits_the_active_learned_value(miner):
    ac = FakeAC(level=5)
    r = _plus5(miner, ac, gain=0.2)
    r.st['learned'] = {'5': dict(LW, mhz=304.6)}
    r.go(600, temp=74)
    assert r.events('ertrag')[-1]['trigger'] == ('Ertrags-Prüfung: +5 MHz (300 → 305 MHz) brachte +0,2 TH/s (unter 0,5): '
                                                 'Obergrenze Stufe 5 = 300 MHz, Lernwert Stufe 5 auf 300 MHz begrenzt')
    assert r.st['learned'] == {'5': dict(LW, mhz=300)} and r.st['clock_set'] == 305


# ---------------------------------------------------------------- Lüfterstöße unten enden ohne Regelung (28.10.)

def test_cold_pulses_end_on_automatik_off_fan_write_retried(miner):
    ac = FakeAC(level=5)
    r = _cold(miner, ac)
    r.go(5, temp=78)
    assert r.st['stoss']['dir'] == 'down' and ac.level == 4
    ac.fail = True
    r.kw['enabled'] = False
    r.go(5)
    assert r.st['stoss'] is None and r.st['fan_target'] == 5 and r.st['heat_fan_due'] and ac.level == 4
    ev = r.events('luefterstoss_ende')[-1]
    assert ev['trigger'] == 'Lüfterstöße unten beendet: Automatik aus' and ev['fan_old'] == 4 and ev['fan_new'] == 5
    ac.fail = False
    r.go(15)
    assert ac.level == 4                                          # erneut frühestens 20 s nach dem Fehler
    r.go(5)
    assert ac.level == 5 and 'heat_fan_due' not in r.st and r.st['fan_pending'] is None
    assert len(r.events('luefterstoss_ende')) == 1


def test_cold_pulses_end_when_the_miner_does_not_answer(miner):
    ac = FakeAC(level=5)
    r = _cold(miner, ac)
    r.go(5, temp=78)
    miner.offline = True
    r.go(5)
    assert r.st['stoss'] is None and ac.level == 5
    assert r.events('luefterstoss_ende')[-1]['trigger'] == 'Lüfterstöße unten beendet: Miner antwortet nicht'


def test_cold_pulses_end_on_over_hot(miner):
    ac = FakeAC(level=5)
    r = _cold(miner, ac)
    r.go(5, temp=78)
    miner.status = 'Over Hot'
    r.go(5, temp=78)
    assert r.st['stoss'] is None and ac.level == 5
    assert r.events('luefterstoss_ende')[-1]['trigger'] == 'Lüfterstöße unten beendet: Over Hot'


# ---------------------------------------------------------------- Update ohne Startzeit; 5 min über 4 % bei einem Takt

def test_state_without_start_time_gets_one_no_cap_and_no_restart_at_once(miner, monkeypatch):
    conns = _connect(monkeypatch, FakeSocket([b'']))
    ac = FakeAC(level=5)
    miner.points = fa.points_for(STOCK['2/L4'], 300)
    old = {'v': fa.STATE_VERSION, 'seen_at': T0.isoformat(), 'level_seen': 'x', 'zb': [79, 84], 'learned': {'5': LW},
           'clock_set': 300, 'fan_target': 5, 'fan_set': 5, 'elapsed': 900}   # Stand vor dem Update: ohne start_at
    r = Run(miner, ac, 5, state=old)
    miner.dh = 4.5
    r.go(5, temp=82)
    assert r.st['start_at'] == T0.isoformat() and not r.events('start')
    r.go(15 * 60 - 5, temp=82)                                    # 10 min, dann 5 min über 4 %
    assert not r.st.get('caps')
    r.go(5, temp=82)
    assert r.st['caps'] == {'5': 295} and conns == []
    r.go(15 * 60 - 5, temp=82)
    assert not r.events('neustart_auto')
    r.go(5, temp=82)                                              # 30 min nach dem Update
    assert len(r.events('neustart_auto')) == 1


def test_cap_window_restarts_when_the_clock_changes(miner):
    ac = FakeAC(level=5)
    r = started(miner, ac, level=5, learned={'5': LW})
    r.go(10 * 60, temp=82)
    miner.dh = 4.5
    r.go(60, temp=82)                                             # 5 min über 4 % beginnen bei 300 MHz
    r.go(65, temp=74)                                             # +5 → 305 MHz
    assert r.st['clock_set'] == 305
    r.go(5, temp=82)
    since = r.now - timedelta(seconds=5)
    assert r.st['dh_cap_since'] == since.isoformat() and r.st['dh_cap_clock'] == 305
    r.go((since + timedelta(minutes=5) - r.now).total_seconds(), temp=82)
    assert not r.st.get('caps')                                   # die 5 min bei 300 MHz zählen nicht
    r.go(5, temp=82)
    assert r.st['caps'] == {'5': 300}
    assert r.st['dh_cap_since'] == (r.now - timedelta(seconds=5)).isoformat()   # nach der Obergrenze neu


def test_cap_window_restarts_when_the_user_clears_the_caps(miner):
    ac = FakeAC(level=5)
    r = started(miner, ac, level=5, learned={'5': LW})
    r.go(10 * 60, temp=82)
    miner.dh = 4.5
    r.go(5 * 60 + 5, temp=82)
    assert r.st['caps'] == {'5': 295}
    r.go(240, temp=82)
    r.kw['caps_cleared_at'] = r.now
    r.go(5, temp=82)                                              # gelöscht: nicht in derselben Runde neu gesetzt
    assert r.st['caps'] == {}
    r.go(5 * 60 - 5, temp=82)
    assert r.st['caps'] == {}                                     # erst nach weiteren vollen 5 min über 4 %
    r.go(5, temp=82)
    assert r.st['caps'] == {'5': 295}


def test_restart_window_restarts_when_the_clock_changes(miner, monkeypatch):
    """Nutzer 28.10.: der Neustart hängt an den 5 min über 4 % beim selben Takt (vorher eigene 5 min ab 10 %, unabhängig
    vom Takt)."""
    conns = _connect(monkeypatch, FakeSocket([b'']))
    ac = FakeAC(level=5)
    r = started(miner, ac, level=5, learned={'5': LW})
    r.st['start_at'] = (r.now - timedelta(hours=1)).isoformat()
    r.st['dh_restart_since'] = r.now.isoformat()                  # Zustand der entfallenen 10-%-Regel
    miner.dh = 4.5
    r.go(60, temp=82)                                             # 5 min über 4 % beginnen bei 300 MHz
    assert 'dh_restart_since' not in r.st
    r.go(65, temp=74)                                             # +5 → 305 MHz
    assert r.st['clock_set'] == 305
    r.go(5, temp=82)
    since = r.now - timedelta(seconds=5)
    assert r.st['dh_cap_since'] == since.isoformat() and r.st['dh_cap_clock'] == 305
    r.go((since + timedelta(minutes=5) - r.now).total_seconds(), temp=82)
    assert conns == [] and not r.st.get('caps')                   # die 5 min bei 300 MHz zählen nicht
    r.go(5, temp=82)
    assert len(conns) == 1 and r.st['caps'] == {'5': 300}
