"""Temperatur-Automatik v2.2.7 (Nutzer 06.11.): Heiz-, Basis- und Kühlmodus des Lüfters, Takterhöhung unter ZTU mit
Sperren, Taktsenkung über ZTO nach Bereichen und ZeitImZieltemperatur, Startpause, Takt laut Miner, Fehlerrate,
Lernen, Protokoll je Zyklus. Zieltemperatur der Tests: 80–85 °C (Beispiel der Freigabe), sonst angegeben."""
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
ZT = (80, 85)                                                              # Zieltemperatur ZTU–ZTO der Tests


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

    def no_network(addr, timeout=None):                      # automatischer Neustart: nie eine echte Verbindung
        raise AssertionError('Verbindung im Test ohne _connect: %s' % (addr,))
    monkeypatch.setattr(calibration.socket, 'create_connection', no_network)
    return mn


def row(state=None, level=5, enabled=True, level_at=None, maintenance_at=None, restart_req_at=None,
        target_low=ZT[0], target_high=ZT[1], learned_del=None):
    return {'id': 1, 'device_id': 3, 'enabled': enabled, 'ac_dev_id': 'dev', 'ac_port': 1, 'level': level,
            'level_at': level_at, 'maintenance_at': maintenance_at, 'state': state or {},
            'restart_req_at': restart_req_at, 'target_low': target_low, 'target_high': target_high,
            'ip_address': '10.0.0.15', 'port': 4028, 'learned_del': learned_del,
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

    @property
    def last(self):
        """Time of the last round."""
        return self.now - timedelta(seconds=5)


LW = {'mhz': 300, 'temp_avg': 82, 'at': 'x', 'count': 1}


def started(miner, ac, level=5, tmax=82, learned=None, zb=None):
    """Automatik ein (Basisstufe `level`, Zieltemperatur `zb`, sonst 80–85 °C) and the start clock taken over: rounds at
    T0 and T0+5 s, r.now = T0+10 s."""
    r = Run(miner, ac, level)
    if zb:
        r.kw.update(target_low=zb[0], target_high=zb[1])
    if learned:
        # Lernwerte der eingestellten Zieltemperatur (ohne gemerkte gehörten sie zum Startwert 82–85)
        r.st = {'v': fa.STATE_VERSION, 'seen_at': T0.isoformat(), 'learned': learned, 'level_seen': 'x',
                'zb': list(zb or ZT)}
        r.kw['level_at'] = 'klick'
    miner.tmax = tmax
    r.go(10)
    return r


def warm(miner, ac=None, level=5, mhz=300, **kw):
    """Basisstufe `level` with the learned value `mhz`, 82 °C since T0 (in the Zieltemperatur), start clock taken over."""
    return started(miner, ac or FakeAC(level=level), level=level, learned={str(level): dict(LW, mhz=mhz)}, **kw)


def hm(dt):
    return dt.astimezone(fa.LOCAL_TZ).strftime('%H:%M')


# ---------------------------------------------------------------- Start

def test_start_without_learned_value_uses_initial_value(miner):
    ac = FakeAC(level=3)
    r = started(miner, ac, level=3)
    assert [round(x) for x in miner.clock_sets] == [330]                 # Stufe 3 = 330 MHz, Modus 04 passt schon
    assert miner.mode_sets == [] and r.st['clock_set'] == 330 and r.st['phase'] == 'basismodus'
    assert r.events('start')[0]['details']['quelle'] == 'Initialwert'
    assert r.events('takt')[0]['takeover_state'] == 'gemessen'
    assert r.st['fan_target'] == 3 and r.st['fan_changed_at'] == T0.isoformat() and not r.st.get('heat')


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


def test_initial_values_of_28_10():
    assert fa.START_CLOCK == {1: 170, 2: 250, 3: 330, 4: 380, 5: 400, 6: 420, 7: 440, 8: 440, 9: 440, 10: 440}
    assert fa.STATE_VERSION == 2                                          # a new version would wipe the learned values


@pytest.mark.parametrize('level, mhz, mode', [(1, 170, 4), (4, 380, 3), (5, 400, 2), (7, 440, 0)])
def test_start_without_learned_value_new_initial_values(miner, level, mhz, mode):
    ac = FakeAC(level=level)
    r = started(miner, ac, level=level)
    assert r.st['clock_set'] == mhz and miner.clock() == mhz and miner.level == mode   # Modus folgt dem Takt
    assert r.events('start')[-1]['details'] == {'quelle': 'Initialwert', 'starttakt': mhz, 'basisstufe': level}


# ---------------------------------------------------------------- Modus

def test_mode_up_at_361_and_down_only_below_335(miner):
    r = warm(miner, mhz=358)
    assert miner.level == 4
    r.go(15, temp=79)                                             # +5 → 363 → mode 03 zuerst
    assert miner.level == 3 and miner.clock() == 363 and miner.mode_sets == [3]
    assert [k for k in r.j.kinds() if k in ('modus', 'takt')][-2:] == ['modus', 'takt']
    assert fa.MODE_DOWN_MARGIN == 25                              # Nutzer 08.11. (vorher 20)
    # abwärts nur kleiner als Obergrenze des tieferen Modus − 25 (Nutzer 08.11.)
    assert fa.required_level(335, 3) == 3 and fa.required_level(334, 3) == 4     # mode 03 → 04 unter 335
    assert fa.required_level(340, 3) == 3 and fa.required_level(339, 3) == 3     # bis v2.2.8 schon unter 340
    assert fa.required_level(355, 2) == 2 and fa.required_level(354, 2) == 3     # mode 02 → 03 unter 355
    assert fa.required_level(375, 1) == 1 and fa.required_level(374, 1) == 2     # mode 01 → 02 unter 375
    assert fa.required_level(395, 0) == 0 and fa.required_level(394, 0) == 1     # mode 00 → 01 unter 395
    assert fa.required_level(330, 0) == 4 and fa.required_level(354, 0) == 3     # mehrere Modi auf einmal
    # aufwärts unverändert ab Obergrenze + 1
    assert fa.required_level(361, 4) == 3 and fa.required_level(360, 4) == 4
    assert fa.required_level(381, 3) == 2 and fa.required_level(401, 2) == 1 and fa.required_level(421, 1) == 0
    assert fa.table_level(360) == 4 and fa.table_level(361) == 3 and fa.table_level(440) == 0   # Start unverändert


def test_clock_at_most_440(miner):
    r = warm(miner, mhz=430)
    assert miner.level == 0
    r.go(5, temp=55)                                              # ZTU −25: +40, höchstens 440
    assert r.st['clock_set'] == 440 and miner.clock() == 440
    assert r.events('takt')[-1]['trigger'].startswith('Chiptemperatur 55 °C (ZTU −25): +10 MHz, keine Takterhöhung')
    n = len(r.events('takt'))
    r.go(300, temp=55)                                            # bei 440 MHz keine Takterhöhung mehr
    assert r.st['clock_set'] == 440 and len(r.events('takt')) == n


# ---------------------------------------------------------------- Takterhöhung unter ZTU (Nutzer 06.11.)

def test_plus_rule_table():
    """Tabelle der Freigabe mit ZTU 80 (Nutzer 09.11.): 77–79 °C +5 (3 min), 75–76 +15 (2 min), 70–74 +20 (2 min),
    60–69 +25 (2 min), bis 59 +40 (2 min)."""
    got = {t: fa.plus_rule(80 - t) for t in (80, 79, 77, 76, 75, 74, 70, 69, 60, 59, 30)}
    assert got == {80: None, 79: (5, 180), 77: (5, 180), 76: (15, 120), 75: (15, 120), 74: (20, 120), 70: (20, 120),
                   69: (25, 120), 60: (25, 120), 59: (40, 120), 30: (40, 120)}


@pytest.mark.parametrize('temp, plus', [(80, 0), (79, 5), (77, 5), (76, 15), (75, 15), (74, 20), (70, 20), (69, 25),
                                        (60, 25), (59, 40)])
def test_increase_at_once_at_the_first_value_below_ztu(miner, temp, plus):
    """Kein 60-s-Warten unter ZTU, keine Tendenzprüfung, kein Beobachtungsfenster: der erste Wert genügt."""
    r = warm(miner, mhz=200)
    r.go(5, temp=temp)
    assert r.st['clock_set'] == 200 + plus and miner.clock() == 200 + plus


def test_increase_text_and_lock_3_min(miner):
    r = warm(miner)
    at = r.now
    r.go(5, temp=77)                                              # ZTU −3: +5, dann 3 min keine Takterhöhung
    until = at + timedelta(minutes=3)
    assert r.st['clock_set'] == 305 and r.st['locks']['erhoehung'] == until.isoformat()
    takt = r.events('takt')[-1]
    assert takt['trigger'] == 'Chiptemperatur 77 °C (ZTU −3): +5 MHz, keine Takterhöhung bis %s' % hm(until)
    assert takt['details']['schritt'] == 5 and takt['details']['sperre_bis'] == until.isoformat()
    r.go(175, temp=77)
    assert r.st['clock_set'] == 305                               # Sperre läuft
    r.go(5, temp=77)                                              # 3 min nach der Erhöhung
    assert r.st['clock_set'] == 310 and miner.clock() == 310


@pytest.mark.parametrize('temp, plus, lock_s', [(76, 15, 120), (72, 20, 120), (65, 25, 120), (50, 40, 120)])
def test_lock_length_by_the_row_of_the_step(miner, temp, plus, lock_s):
    r = warm(miner, mhz=200)
    r.go(5, temp=temp)
    assert r.st['clock_set'] == 200 + plus
    r.go(lock_s - 5, temp=temp)
    assert r.st['clock_set'] == 200 + plus
    r.go(5, temp=temp)
    assert r.st['clock_set'] == min(200 + 2 * plus, 440)


def test_increase_also_while_rising(miner):
    """Die Tendenz wird nur noch angezeigt: auch bei steigender Chiptemperatur gilt die Takterhöhung."""
    r = warm(miner)
    r.go(5, temp=70)                                              # +20, Sperre 2 min
    assert r.st['clock_set'] == 320
    r.go(115, temp=lambda r: 70 + int((r.now - T0).total_seconds() // 40))   # steigt langsam
    assert r.st['clock_set'] == 320 and r.st['tendency'] > 0
    r.go(5, temp=73)
    assert r.st['clock_set'] == 340 and r.st['tendency'] > 0


def test_locks_after_an_increase_heizmodus_kuehlmodus_end_early_above_ztu_plus_3(miner):
    r = warm(miner)
    r.go(5, temp=79)                                              # +5, Sperre 3 min
    r.go(30, temp=83)                                             # ZTU+3: Sperre bleibt
    assert r.st['locks']['erhoehung']
    r.go(5, temp=79)
    assert r.st['clock_set'] == 305
    r.go(5, temp=84)                                              # über ZTU+3: Sperre endet
    assert r.st['locks'] == {'erhoehung': None, 'heizmodus': None, 'kuehlmodus': None, 'senkung': None}
    r.go(5, temp=79)
    assert r.st['clock_set'] == 310                               # gleich wieder +5


def test_lock_after_an_increase_ends_3_degrees_below_the_temperature_at_the_increase(miner):
    """Beispiel Nutzer 09.11. (av ja, aw ja), ZTU 80, Basisstufe 3: 10:00 79 °C +5, Sperre bis 10:03 (Bezug 79);
    10:01 77 °C Sperre läuft; 10:01:30 76 °C Sperre endet, in derselben Runde +15 mit neuer Sperre 2 min (Bezug 76);
    weiter auf 73 °C: Sperre endet, +20."""
    r = warm(miner, FakeAC(level=3), level=3)
    x = r.now
    r.go(5, temp=79)                                              # 10:00: +5, Sperre bis 10:03
    assert r.st['clock_set'] == 305 and r.st['raise_t'] == 79
    assert r.st['locks']['erhoehung'] == (x + timedelta(minutes=3)).isoformat()
    r.go(55, temp=79)
    r.go(5, temp=77)                                              # 10:01: 77 °C, Sperre läuft
    assert r.st['clock_set'] == 305 and r.st['locks']['erhoehung'] == (x + timedelta(minutes=3)).isoformat()
    r.go(25, temp=77)
    r.go(5, temp=76)                                              # 10:01:30: 76 °C = 79 − 3, Sperre endet → +15
    y = r.last
    until = y + timedelta(minutes=2)
    assert r.st['clock_set'] == 320 and miner.clock() == 320 and r.st['raise_t'] == 76
    assert r.st['locks']['erhoehung'] == until.isoformat()
    assert r.events('takt')[-1]['trigger'] == 'Chiptemperatur 76 °C (ZTU −4): +15 MHz, keine Takterhöhung bis %s' % hm(until)
    r.go(5, temp=75)
    r.go(5, temp=74)                                              # 74 °C: noch nicht 3 Grad unter 76
    assert r.st['clock_set'] == 320 and r.st['locks']['erhoehung'] == until.isoformat()
    r.go(5, temp=73)                                              # 73 °C = 76 − 3: Sperre endet → +20
    assert r.st['clock_set'] == 340 and r.st['raise_t'] == 73
    assert r.st['locks']['erhoehung'] == (r.last + timedelta(minutes=2)).isoformat()


def test_lock_after_an_increase_ends_more_than_3_degrees_below(miner):
    r = warm(miner)
    r.go(5, temp=79)                                              # +5, Sperre 3 min (Bezug 79)
    r.go(30, temp=79)
    assert r.st['clock_set'] == 305
    r.go(5, temp=75)                                              # 4 Grad darunter: Sperre endet → +15
    assert r.st['clock_set'] == 320 and r.st['raise_t'] == 75
    assert r.st['locks']['erhoehung'] == (r.last + timedelta(minutes=2)).isoformat()


@pytest.mark.parametrize('key', ['heizmodus', 'kuehlmodus', 'senkung'])
def test_3_degree_drop_does_not_end_the_other_locks(miner, key):
    """Nur die Sperre nach einer Erhöhung endet 3 Grad unter der Temperatur bei der Erhöhung (Nutzer 09.11., aw)."""
    r = warm(miner)
    r.go(5, temp=79)                                              # +5, Bezug 79
    until = (r.now + timedelta(minutes=2)).isoformat()
    r.st['locks'] = dict(r.st['locks'], erhoehung=None, **{key: until})   # statt der Sperre nach der Erhöhung
    r.go(5, temp=76)
    r.go(5, temp=73)
    assert r.st['locks'][key] == until and r.st['clock_set'] == 305 and r.st['raise_t'] == 79


def test_lock_after_a_reduction_not_ended_by_a_3_degree_drop(miner):
    r = warm(miner)
    r.go(5, temp=79)                                              # +5, Bezug 79
    r.go(5, temp=86)                                              # über ZTU+3: Sperre nach der Erhöhung endet; −10
    x, clock = r.last, r.st['clock_set']
    assert clock == 295 and r.st['locks']['senkung'] == (x + timedelta(minutes=5)).isoformat()
    r.go(60, temp=76)                                             # 3 Grad unter 79: die Sperre nach der Senkung bleibt
    assert r.st['locks']['senkung'] == (x + timedelta(minutes=5)).isoformat() and r.st['clock_set'] == clock
    r.go((x + timedelta(minutes=5) - r.now).total_seconds(), temp=76)
    assert r.st['clock_set'] == clock
    r.go(5, temp=76)                                              # 5 min nach der Senkung: +15
    assert r.st['clock_set'] == clock + 15


def test_old_state_without_temperature_at_the_increase_keeps_the_lock(miner):
    r = warm(miner)
    x = r.now
    r.go(5, temp=79)                                              # +5, Sperre bis 3 min danach
    r.st.pop('raise_t')                                           # Zustand von vor dem Update
    r.go(5, temp=76)
    r.go(5, temp=70)
    assert r.st['clock_set'] == 305 and r.st['locks']['erhoehung'] == (x + timedelta(minutes=3)).isoformat()
    r.go((x + timedelta(minutes=3) - r.now).total_seconds(), temp=76)
    assert r.st['clock_set'] == 305
    r.go(5, temp=76)                                              # 3 min nach der Erhöhung: +15
    assert r.st['clock_set'] == 320


@pytest.mark.parametrize('reason', ['Basisstufe gewählt', 'Automatik ein'])
def test_temperature_at_the_increase_survives_a_start(miner, reason):
    r = warm(miner)
    x = r.now
    r.go(5, temp=79)                                              # +5, Sperre 3 min, Bezug 79
    if reason == 'Basisstufe gewählt':
        r.go(5, temp=79, level_at='neu')
    else:
        r.go(5, temp=79, enabled=False)
        r.go(5, temp=79, enabled=True)
    assert r.events('start')[-1]['trigger'].startswith(reason) and r.st['clock_set'] == 300
    r.go(10, temp=78)
    assert r.st['raise_t'] == 79 and r.st['locks']['erhoehung'] == (x + timedelta(minutes=3)).isoformat()
    assert r.st['clock_set'] == 300
    r.go(5, temp=76)                                              # 3 Grad unter 79: Sperre endet → +15
    assert r.st['clock_set'] == 315


def test_no_increase_while_a_command_is_pending(miner):
    """Erst nach der Übernahme des vorigen Befehls."""
    miner.ignore_clock = True
    ac = FakeAC(level=5)
    r = started(miner, ac, level=5, tmax=75, learned={'5': LW})   # Starttakt 300 wird nicht übernommen
    r.go(60, temp=75)
    assert r.st['clock_set'] == 300 and r.st['pending']['value'] == 300 and r.st['pending']['attempt'] >= 2
    miner.ignore_clock = False
    r.go(20, temp=75)                                             # erneut gesendet, übernommen: gleich +15
    assert r.st['clock_set'] == 315 and miner.clock() == 315


def test_superseded_command_is_not_measured(miner):
    r = warm(miner)
    miner.ignore_clock = True
    r.go(5, temp=79)                                              # +5 gesendet, Miner übernimmt nicht
    r.go(25, temp=79)                                             # nach 20 s erneut gesendet
    takt = [e for e in r.events('takt') if e.get('clock_new') == 305]
    assert len(takt) == 2 and takt[0]['takeover_state'] == 'nicht gemessen' and takt[-1]['attempt'] == 2


def test_entering_the_zieltemperatur_from_below_is_no_reduction(miner):
    r = warm(miner)
    r.go(5, temp=79)
    assert miner.clock() == 305
    r.go(240, temp=81)
    assert miner.clock() == 305 and r.st['clock_set'] == 305


def test_no_clock_change_in_the_zieltemperatur(miner):
    """Zwischen ZTU und ZTO (beide eingeschlossen) keine Taktänderung – auch nicht bei steigender Tendenz."""
    r = warm(miner)
    sets, n = list(miner.clock_sets), len(r.events('takt'))
    for temp in (82, 83, 85, 84, 81, 80, 85, 80, 84):
        r.go(60, temp=temp)
    assert miner.clock_sets == sets and miner.clock() == 300 and len(r.events('takt')) == n
    assert r.st['phase'] == 'basismodus' and r.st['fan_target'] == 5


# ---------------------------------------------------------------- Taktsenkung über ZTO (Nutzer 06.11.)

def test_bands_and_band_1_minus_by_the_zeit_im_zieltemperatur():
    assert [fa.band_of(t, 85) for t in (84, 85, 86, 88, 89, 91, 92, 94, 95, 99, 120)] == \
        [None, None, 1, 1, 2, 2, 3, 3, 4, 4, 4]
    assert [fa.minus_band_1(s) for s in (0, 59, 60, 300, 301, 3600)] == [10, 10, 10, 10, 5, 5]   # Nutzer 08.11.


def test_example_of_the_approval_timing_within_an_excursion(miner):
    """10:00 85→86 (3 min in ZT) −10; 10:02 noch 87 −10; 10:03 89 −20; 10:04 zurück auf 88 → 10:06 −10; 10:07 85 →
    keine Senkung."""
    r = warm(miner)                                               # 82 °C seit T0
    r.go(170, temp=85)
    x = r.now                                                     # 10:00 (3 min in 80–85 °C)
    r.go(5, temp=86)
    assert r.st['clock_set'] == 290 and r.st['cut_at'] == x.isoformat()
    assert r.events('takt')[-1]['trigger'] == 'Chiptemperatur 86 °C (Bereich 1, ZeitImZieltemperatur 3 min): Takt −10 MHz'
    assert r.st['hot']['dwell_s'] == 180 and r.st['hot']['minus1'] == 10
    assert r.st['locks']['senkung'] == (x + timedelta(minutes=5)).isoformat()
    r.go(115, temp=87)
    assert r.st['clock_set'] == 290
    r.go(5, temp=87)                                              # 10:02
    assert r.st['clock_set'] == 280
    assert r.events('takt')[-1]['trigger'] == 'Chiptemperatur 87 °C (Bereich 1, ZeitImZieltemperatur 3 min): Takt −10 MHz'
    r.go(55, temp=87)
    r.go(5, temp=89)                                              # 10:03: höherer Bereich sofort
    assert r.st['clock_set'] == 260 and r.events('takt')[-1]['trigger'] == 'Chiptemperatur 89 °C (Bereich 2): Takt −20 MHz'
    r.go(55, temp=89)
    r.go(5, temp=88)                                              # 10:04 zurück in Bereich 1
    assert r.st['clock_set'] == 260 and r.st['hot']['band'] == 1
    assert r.st['hot']['next_cut_at'] == (x + timedelta(minutes=6)).isoformat()
    r.go(115, temp=88)
    assert r.st['clock_set'] == 260
    r.go(5, temp=88)                                              # 10:06
    assert r.st['clock_set'] == 250 and miner.clock() == 250
    r.go(60, temp=85)                                             # 10:07: Ausflug vorbei, keine Senkung
    assert r.st['clock_set'] == 250 and r.st['hot'] is None
    assert r.st['locks']['senkung'] == (x + timedelta(minutes=11)).isoformat()


@pytest.mark.parametrize('stay, minus', [(55, 10), (60, 10), (240, 10), (300, 10), (305, 5)])
def test_band_1_minus_by_the_time_in_the_zieltemperatur(miner, stay, minus):
    """Beispiel der Freigabe: 4 min in 80–85, dann 86 → −10; 0–5 min −10, über 5 min −5 (Nutzer 08.11.)."""
    r = warm(miner)                                               # 82 °C seit T0
    r.go(stay - 10, temp=82)
    r.go(5, temp=86)
    assert r.st['clock_set'] == 300 - minus and r.st['hot']['dwell_s'] == stay
    assert r.events('takt')[-1]['details']['zeit_im_ziel_s'] == stay


def test_directly_from_below_ztu_to_86_minus_10(miner):
    """Direkt 79 → 86 → −10 (ein Wert unter ZTU beendet den Aufenthalt; bis v2.2.7 −15, Nutzer 08.11.)."""
    r = warm(miner)
    r.go(5, temp=79)                                              # +5
    r.go(5, temp=86)
    assert r.st['clock_set'] == 295 and r.st['hot']['dwell_s'] == 0
    assert r.events('takt')[-1]['trigger'] == 'Chiptemperatur 86 °C (Bereich 1, ZeitImZieltemperatur 0 s): Takt −10 MHz'


def test_entry_from_above_starts_the_time_in_the_zieltemperatur(miner):
    r = warm(miner)
    r.go(45, temp=82)
    r.go(5, temp=86)                                              # 55 s in ZT: −10
    assert r.st['clock_set'] == 290
    r.go(240, temp=84)                                            # von oben zurück: 4 min in ZT
    r.go(5, temp=86)                                              # letzte Senkung > 3 min her: sofort −10
    assert r.st['clock_set'] == 280 and r.st['hot']['dwell_s'] == 240


def test_excursion_start_less_than_3_min_after_the_last_reduction_waits_until_3_min(miner):
    r = warm(miner)
    r.go(45, temp=82)
    x = r.now
    r.go(5, temp=86)                                              # −10
    r.go(30, temp=84)                                             # kurz in der Zieltemperatur
    r.go(5, temp=86)                                              # neuer Ausflug 35 s nach der Senkung: warten
    assert r.st['clock_set'] == 290 and r.st['hot']['next_cut_at'] == (x + timedelta(minutes=3)).isoformat()
    r.go(140, temp=86)
    assert r.st['clock_set'] == 290
    r.go(5, temp=86)                                              # 3 min nach der letzten Senkung (30 s in ZT: −10)
    assert r.st['clock_set'] == 280


def test_higher_band_reduces_at_once_even_during_the_3_min_wait(miner):
    r = warm(miner)
    r.go(45, temp=82)
    r.go(5, temp=86)                                              # −10
    r.go(30, temp=84)
    r.go(5, temp=86)                                              # wartet bis 3 min nach der Senkung
    r.go(5, temp=89)                                              # Bereich 2: sofort −20
    assert r.st['clock_set'] == 270


def test_higher_band_after_a_short_dip_reduces_at_once(miner):
    """Nutzer 08.11.: 86 °C −10, 30 s 84 °C, dann direkt 95 °C (Bereich 4) – sofort −50, nicht erst 3 min nach −10."""
    r = warm(miner)
    r.go(45, temp=82)
    r.go(5, temp=86)                                              # −10
    r.go(30, temp=84)                                             # kurz in der Zieltemperatur
    r.go(5, temp=95)                                              # Bereich 4 > Bereich 1 der letzten Senkung
    assert r.st['clock_set'] == 240 and r.st['cut_band'] == 4
    assert r.events('takt')[-1]['trigger'] == 'Chiptemperatur 95 °C (Bereich 4): Takt −50 MHz'


def test_running_excursion_with_the_old_minus_15_reduces_by_10(miner):
    """Nutzer 08.11. (ae): ein beim Update laufender Ausflug mit dem alten Wert −15 senkt ab sofort um −10."""
    r = warm(miner)
    r.go(5, temp=86)                                              # −10
    r.st['hot']['minus1'] = 15                                    # Zustand von v2.2.7
    r.go(120, temp=86)                                            # 2 min später im selben Bereich: −10, nicht −15
    assert r.st['clock_set'] == 280 and r.st['hot']['minus1'] == 10
    assert r.events('takt')[-1]['trigger'].endswith('Takt −10 MHz')


@pytest.mark.parametrize('temp, minus, band', [(88, 10, 1), (91, 20, 2), (94, 20, 3), (96, 20, 3), (97, 50, 4),
                                               (105, 50, 4)])
def test_minus_by_band_relative_to_zto_without_96_cap(miner, temp, minus, band):
    """Zieltemperatur 83–87 °C: Bereich 1 ab 88, 2 ab 91, 3 ab 94, 4 ab 97 °C – kein Deckel bei 96 °C mehr."""
    r = warm(miner, zb=(83, 87), tmax=85)
    r.go(5, temp=temp)                                            # 10 s in ZT: Bereich 1 −10
    assert r.st['clock_set'] == 300 - minus and r.st['hot']['band'] == band


def test_reduction_replaces_a_pending_command(miner):
    """Sicherheit zuerst: eine fällige Senkung löst einen laufenden Befehl ab."""
    r = warm(miner)
    miner.ignore_clock = True
    r.go(5, temp=79)                                              # +5 gesendet, Miner übernimmt nicht
    assert r.st['pending']['value'] == 305
    r.go(5, temp=86)
    assert r.st['clock_set'] == 295 and r.st['pending']['value'] == 295
    assert [e for e in r.events('takt') if e.get('clock_new') == 305][-1]['takeover_state'] == 'nicht gemessen'


def test_lock_after_a_reduction_5_min_never_early(miner):
    r = warm(miner)
    x = r.now
    r.go(5, temp=86)                                              # −10
    assert r.st['clock_set'] == 290
    r.go(60, temp=84)                                             # über ZTU+3: die Sperre nach der Senkung bleibt
    assert r.st['locks']['senkung'] == (x + timedelta(minutes=5)).isoformat()
    r.go(235, temp=79)
    assert r.st['clock_set'] == 290
    r.go(5, temp=79)                                              # 5 min nach der Senkung
    assert r.st['clock_set'] == 295


def test_clock_below_100_is_an_error_and_not_sent(miner):
    r = warm(miner, mhz=110)
    r.go(5, temp=86)                                              # 110 − 10 = 100: erlaubt
    assert miner.clock() == 100
    sends = len(miner.clock_sets)
    r.go(125, temp=86)                                            # nach 2 min: 90 MHz – Fehler, nicht gesendet
    assert len(miner.clock_sets) == sends and r.st['clock_set'] == 100
    errors = [e for e in r.events('fehler') if e['details']['grund'] == 'takt_unter_100']
    assert len(errors) == 1                                       # nicht jede Runde, erst wieder nach 2 min


@pytest.mark.parametrize('start, mode', [(385, 3), (384, 4)])
def test_reduction_with_mode_change_clock_first_then_mode_then_clock(miner, start, mode):
    r = warm(miner, mhz=start)
    assert miner.level == 2 and miner.clock() == start
    miner.clock_sets.clear()
    r.go(15, temp=95)                                             # Bereich 4: −50 → 335 bleibt mode 03, 334 → mode 04
    assert miner.mode_sets[-1] == mode and miner.level == mode    # (04 erst unter 335, Nutzer 08.11.)
    assert miner.clock_sets[0] == pytest.approx(start - 50, abs=1) and miner.clock() == start - 50
    assert len(miner.clock_sets) == 2                             # Takt, Modus, Takt erneut


def test_missing_stock_clock_is_estimated_and_never_blocks(miner):
    ac = FakeAC(level=5)
    miner.level, miner.points = 1, list(STOCK['2/L1'])           # mode 01: kein Profil, kein gemerkter Standardtakt
    r = started(miner, ac, level=5, learned={'5': {'mhz': 410, 'temp_avg': 82, 'at': 'x', 'count': 1}})
    assert miner.clock() == 410
    r.go(5, temp=86)                                              # 10 s in ZT: −10
    assert miner.clock() == 400 and r.events('fehler')[0]['details']['grund'] == 'standardtakt_fehlt'


# ---------------------------------------------------------------- Basisstufe 1: nach −5 MHz (Nutzer 09.11.)

def _cut5(miner, level=1, mhz=200):
    """Basisstufe `level`, 6 min in 80–85 °C, then 86 °C: Bereich 1 −5 MHz at x (10:00 of the example)."""
    ac = FakeAC(level=level)
    r = warm(miner, ac, level=level, mhz=mhz)                     # 82 °C seit T0
    r.go(350, temp=82)
    x = r.now
    r.go(5, temp=86)
    assert r.st['clock_set'] == mhz - 5 and r.st['cut5_at'] == x.isoformat()
    return r, x


def test_base_level_1_no_increase_at_ztu_minus_2_and_1_for_30_min_after_minus_5(miner):
    """Beispiel (ar, as): 10:00 86 °C nach 6 min −5; 10:10 79 °C kein +5; 10:12 77 °C +5 (ZTU −3, hebt die Sperre
    nicht auf); 10:31 79 °C +5. Kein Protokolleintrag für die verhinderte Erhöhung."""
    r, x = _cut5(miner)
    r.go(595, temp=84)                                            # 10:10, Sperre nach der Senkung vorbei
    n, logs = len(r.j.events), list(r.st['log'])
    r.go(5, temp=79)
    assert r.st['clock_set'] == 195 and not any(r.st['locks'].values())
    r.go(5, temp=78)
    assert r.st['clock_set'] == 195 and len(r.j.events) == n      # still: kein Eintrag
    assert r.st['log'] == logs                                    # ... und kein Hinweis
    r.go(110, temp=82)
    r.go(5, temp=77)                                              # 10:12: ZTU −3 wie bisher
    assert r.st['clock_set'] == 200 and r.st['cut5_at'] == x.isoformat()
    r.go(475, temp=82)
    r.go(5, temp=79)                                              # 10:20: Sperre nach der Erhöhung vorbei, Block nicht
    assert r.st['clock_set'] == 200
    r.go(655, temp=82)
    r.go(5, temp=79)                                              # 10:31: Block vorbei
    assert r.st['clock_set'] == 205 and miner.clock() == 205


def test_block_after_minus_5_ends_exactly_after_30_min(miner):
    r, x = _cut5(miner)
    r.go((x + timedelta(seconds=1795) - r.now).total_seconds(), temp=84)
    r.go(5, temp=79)                                              # 29 min 55 s
    assert r.st['clock_set'] == 195
    r.go(5, temp=79)                                              # genau 30 min
    assert r.st['clock_set'] == 200


def test_block_after_minus_5_not_at_base_level_3(miner):
    r, x = _cut5(miner, level=3, mhz=300)
    r.go(595, temp=84)
    r.go(5, temp=79)                                              # 10:10: +5
    assert r.st['clock_set'] == 300 and r.st['cut5_at'] == x.isoformat()


def test_minus_10_does_not_block_at_base_level_1(miner):
    r = warm(miner, FakeAC(level=1), level=1, mhz=200)
    r.go(230, temp=82)                                            # 4 min in 80–85 °C
    r.go(5, temp=86)                                              # −10
    assert r.st['clock_set'] == 190 and not r.st.get('cut5_at')
    r.go(595, temp=84)
    r.go(5, temp=79)
    assert r.st['clock_set'] == 195


@pytest.mark.parametrize('reason', ['Basisstufe gewählt', 'Automatik ein', 'Neustart', 'nach Wartung',
                                    'nach Over Hot'])
def test_every_start_ends_the_block_after_minus_5(miner, reason):
    """Nutzer 09.11. (at nein): ein Start (hier 10:15) hebt den Block auf."""
    r, x = _cut5(miner)
    r.go(895, temp=84)                                            # 10:15
    if reason == 'Basisstufe gewählt':
        r.go(5, temp=79, level_at='neu')
    elif reason == 'Automatik ein':
        r.go(5, temp=79, enabled=False)
        r.go(5, temp=79, enabled=True)
    elif reason == 'Neustart':
        miner.elapsed = 10
        r.go(5, temp=79)
    elif reason == 'nach Wartung':
        ip = '10.0.0.15'
        miner_cache.put(ip, 'estats', 'x')                        # A15 antwortet noch
        r.go(5, temp=79, maintenance_at=r.now.isoformat())
        miner_cache.clear(ip)                                     # A15 aus
        r.go(5, temp=79)
        miner_cache.put(ip, 'estats', 'x')                        # A15 wieder an: Wartung beendet
        r.go(5, temp=79)
        miner_cache.clear(ip)
        r.st.pop('maint_end')                                     # wie die Datenbank-Schleife
        r.kw['maintenance_at'] = None
    else:
        miner.status = 'Over Hot'
        r.go(5, temp=79)
        miner.status = 'Work: In Work'
        r.go(5, temp=79)
    assert r.events('start')[-1]['trigger'].startswith(reason) and r.st['cut5_at'] is None
    assert r.st['clock_set'] == 200                               # Starttakt (Lernwert Stufe 1)
    r.go(10, temp=79)
    assert r.st['clock_set'] == 205


# ---------------------------------------------------------------- Kühlmodus (Nutzer 06.11.)

def test_kuehlmodus_plus_1_then_plus_2_never_back_ends_below_zto_minus_3(miner):
    ac = FakeAC(level=3)
    r = warm(miner, ac, level=3)
    r.go(5, temp=87)                                              # ZTO+2: noch nicht
    assert not r.st.get('cool') and ac.level == 3
    r.go(5, temp=88)                                              # über ZTO+2: Basisstufe +1 sofort
    assert r.st['cool'] == {'since': r.last.isoformat(), 'plus': 1} and ac.level == 4 and r.st['phase'] == 'kuehlmodus'
    ev = r.events('kuehlmodus')[-1]
    assert ev['trigger'] == 'Kühlmodus: Chiptemperatur 88 °C über 87 °C, Lüfter Stufe 4 (Basisstufe 3 +1)'
    assert ev['fan_old'] == 3 and ev['fan_new'] == 4 and ev['phase'] == 'kuehlmodus'
    r.go(5, temp=92)
    assert r.st['cool']['plus'] == 1 and ac.level == 4
    r.go(5, temp=93)                                              # über ZTO+7: +2
    assert r.st['cool']['plus'] == 2 and ac.level == 5 and len(r.events('kuehlmodus')) == 2
    assert r.events('kuehlmodus')[-1]['trigger'] == 'Kühlmodus: Chiptemperatur 93 °C über 92 °C, Lüfter Stufe 5 (Basisstufe 3 +2)'
    r.go(60, temp=88)
    r.go(60, temp=82)                                             # ZTO−3: bleibt (Hysterese), +2 bleibt
    assert r.st['cool']['plus'] == 2 and ac.level == 5 and r.st['phase'] == 'kuehlmodus'
    at = r.now
    r.go(5, temp=81)                                              # unter ZTO−3: Ende, Basisstufe
    until = at + timedelta(minutes=3)
    assert r.st['cool'] is None and ac.level == 3 and r.st['phase'] == 'basismodus'
    assert r.st['locks']['kuehlmodus'] == until.isoformat()
    end = r.events('kuehlmodus_ende')[-1]
    assert end['trigger'] == ('Kühlmodus beendet: Chiptemperatur 81 °C unter 82 °C, Lüfter Stufe 3; keine '
                              'Takterhöhung bis %s' % hm(until))
    assert end['fan_old'] == 5 and end['fan_new'] == 3


@pytest.mark.parametrize('base, temp, fan', [(5, 93, 7), (9, 93, 10), (10, 88, 10)])
def test_kuehlmodus_jump_to_plus_2_and_at_most_level_10(miner, base, temp, fan):
    ac = FakeAC(level=base)
    r = warm(miner, ac, level=base)
    r.go(5, temp=temp)
    assert r.st['cool']['plus'] == (2 if temp > 92 else 1) and ac.level == fan and r.st['fan_target'] == fan
    assert len(r.events('kuehlmodus')) == 1


def test_no_increase_3_min_after_the_kuehlmodus(miner):
    ac = FakeAC(level=3)
    r = warm(miner, ac, level=3)
    r.go(5, temp=88)                                              # Kühlmodus, −10 (Sperre 5 min)
    r.go(360, temp=84)                                            # Kühlmodus bleibt, Sperre nach der Senkung vorbei
    assert r.st['cool'] and not r.st['locks']['senkung']
    clock = r.st['clock_set']
    r.go(5, temp=79)                                              # Ende → 3 min keine Takterhöhung
    assert not r.st['cool'] and r.st['locks']['kuehlmodus'] and r.st['clock_set'] == clock
    r.go(175, temp=79)
    assert r.st['clock_set'] == clock
    r.go(5, temp=79)
    assert r.st['clock_set'] == clock + 5


def test_kuehlmodus_lock_ends_early_above_ztu_plus_3(miner):
    ac = FakeAC(level=3)
    r = warm(miner, ac, level=3)
    r.go(5, temp=88)
    r.go(360, temp=84)
    r.go(5, temp=79)                                              # Ende, Sperre 3 min
    r.go(5, temp=84)                                              # über ZTU+3: Sperre endet
    assert r.st['locks']['kuehlmodus'] is None
    clock = r.st['clock_set']
    r.go(5, temp=79)
    assert r.st['clock_set'] == clock + 5


def test_fan_error_retried_until_it_works(miner):
    ac = FakeAC(level=5)
    r = warm(miner, ac)
    ac.fail = True
    r.go(5, temp=88)                                              # Kühlmodus: Lüfter +1 sofort – schlägt fehl
    assert ac.level == 5 and r.st['ac_error'] and r.st['cool']
    r.go(25, temp=88)
    ac.fail = False
    r.go(25, temp=88)
    assert ac.level == 6
    tries = r.events('luefter')
    assert [t['ok'] for t in tries][-2:] == [False, True] or tries[-1]['ok']


# ---------------------------------------------------------------- Heizmodus (Nutzer 06.11.)

def test_heizmodus_steps_after_more_than_1_min_down_to_1_and_ends_above_ztu_minus_5(miner):
    ac = FakeAC(level=3)
    r = warm(miner, ac, level=3)                                  # Start bei T0: Lüfter gesetzt
    r.go(55, temp=69)                                             # unter ZTU−10, aber erst 60 s seit dem Start
    assert not r.st.get('heat') and ac.level == 3
    r.go(5, temp=69)                                              # mehr als 1 min ohne Änderung: −1, Heizmodus beginnt
    assert r.st['heat'] == {'since': r.last.isoformat()} and ac.level == 2 and r.st['phase'] == 'heizmodus'
    ev = r.events('heizmodus')[-1]
    assert ev['trigger'] == 'Heizmodus: Chiptemperatur 69 °C unter 70 °C, Lüfter Stufe 2 (Basisstufe 3 −1)'
    assert r.events('luefter')[-1]['trigger'] == 'Heizmodus: Chiptemperatur 69 °C unter 70 °C, Lüfter Stufe 3 → 2'
    r.go(60, temp=69)
    assert ac.level == 2
    r.go(5, temp=69)                                              # jede weitere Minute −1
    assert ac.level == 1
    r.go(130, temp=65)                                            # Stufe 1: nicht tiefer
    assert ac.level == 1 and len(r.events('heizmodus')) == 1
    r.go(120, temp=72)                                            # ZTU−10 bis ZTU−5: Stufe bleibt
    r.go(10, temp=75)
    assert ac.level == 1 and r.st['heat']
    at = r.now
    r.go(5, temp=76)                                              # über ZTU−5: Ende, Basisstufe
    until = at + timedelta(minutes=3)
    assert not r.st['heat'] and ac.level == 3 and r.st['phase'] == 'basismodus'
    assert r.st['locks']['heizmodus'] == until.isoformat()
    end = r.events('heizmodus_ende')[-1]
    assert end['trigger'] == ('Heizmodus beendet: Chiptemperatur 76 °C über 75 °C, Lüfter Stufe 3; keine '
                              'Takterhöhung bis %s' % hm(until))
    clock = r.st['clock_set']
    r.go(175, temp=76)                                            # 3 min keine Takterhöhung nach dem Heizmodus
    assert r.st['clock_set'] == clock
    r.go(5, temp=76)                                              # danach +15
    assert r.st['clock_set'] == clock + 15


def test_heizmodus_lock_ends_early_above_ztu_plus_3(miner):
    ac = FakeAC(level=3)
    r = warm(miner, ac, level=3)
    r.go(65, temp=65)
    r.go(5, temp=76)                                              # Ende, Sperre 3 min
    assert r.st['locks']['heizmodus']
    r.go(5, temp=84)                                              # über ZTU+3: Sperre endet
    assert r.st['locks']['heizmodus'] is None


def test_heizmodus_at_base_level_1_without_a_fan_change(miner):
    ac = FakeAC(level=1)
    r = warm(miner, ac, level=1)
    writes = list(ac.writes)
    r.go(65, temp=60)
    assert r.st['heat'] and ac.level == 1 and r.events('heizmodus')
    assert r.events('heizmodus')[-1]['trigger'] == 'Heizmodus: Chiptemperatur 60 °C unter 70 °C, Lüfter Stufe 1 (Basisstufe 1)'
    r.go(5, temp=76)
    assert not r.st['heat'] and r.events('heizmodus_ende') and ac.writes == writes


def test_heizmodus_waits_1_min_after_any_fan_change(miner):
    ac = FakeAC(level=3)
    r = warm(miner, ac, level=3)
    r.go(5, temp=88)                                              # Kühlmodus: Lüfter 4
    r.go(60, temp=84)
    r.go(5, temp=65)                                              # Ende des Kühlmodus: Lüfter 3 (Änderung)
    assert ac.level == 3 and not r.st['cool']
    r.go(60, temp=65)
    assert not r.st.get('heat') and ac.level == 3
    r.go(5, temp=65)
    assert r.st['heat'] and ac.level == 2


def test_heat_and_cool_mode_never_together(miner):
    ac = FakeAC(level=3)
    r = warm(miner, ac, level=3)
    r.go(65, temp=60)
    assert r.st['heat'] and ac.level == 2
    r.go(5, temp=95)                                              # Sprung: Heizmodus endet, Kühlmodus +2
    assert not r.st['heat'] and r.st['cool']['plus'] == 2 and ac.level == 5 and r.st['phase'] == 'kuehlmodus'
    assert r.events('heizmodus_ende') and r.events('kuehlmodus')


def test_start_ends_kuehlmodus_without_a_lock_then_the_rules_apply_anew(miner):
    ac = FakeAC(level=5)
    r = warm(miner, ac)
    r.go(5, temp=93)
    assert r.st['cool']['plus'] == 2 and ac.level == 7
    r.go(5, temp=88, level_at='neu')                              # Basisstufe gewählt
    end = r.events('kuehlmodus_ende')[-1]
    assert end['trigger'] == 'Kühlmodus beendet: Start (Basisstufe gewählt (Stufe 5)), Lüfter Stufe 5'
    assert not r.st['locks']['kuehlmodus']
    assert r.st['cool']['plus'] == 1 and ac.level == 6           # danach neu: 88 °C → +1


def test_start_ends_heizmodus_without_a_lock(miner):
    ac = FakeAC(level=3)
    r = warm(miner, ac, level=3)
    r.go(65, temp=65)
    assert r.st['heat'] and ac.level == 2
    miner.elapsed = 10                                            # Neustart des Miners
    r.go(5, temp=65)
    end = r.events('heizmodus_ende')[-1]
    assert end['trigger'] == 'Heizmodus beendet: Start (Neustart des Miners), Lüfter Stufe 3'
    assert not r.st['heat'] and ac.level == 3 and not r.st['locks']['heizmodus']
    r.go(60, temp=65)                                             # neu: erst nach mehr als 1 min ohne Änderung
    assert not r.st.get('heat')
    r.go(5, temp=65)
    assert r.st['heat'] and ac.level == 2


def test_start_keeps_locks_last_reduction_and_zeit_im_zieltemperatur(miner):
    """Ein Start setzt nur den Ausflug über ZTO zurück; Sperren, letzte Senkung und ZeitImZieltemperatur bleiben."""
    r = warm(miner)
    r.go(230, temp=82)                                            # 4 min in 80–85 °C
    r.go(5, temp=82, level_at='neu')                              # Start: Basisstufe gewählt
    r.go(5, temp=86)                                              # 4 min 5 s in ZT (nicht ab dem Start): −10
    assert r.st['clock_set'] == 290 and r.st['hot']['dwell_s'] == 245
    x, lock = r.last, r.st['locks']['senkung']
    r.go(30, temp=86, level_at='neu2')                            # Start während des Ausflugs
    assert r.events('start')[-1]['trigger'].startswith('Basisstufe gewählt')
    assert r.st['cut_at'] == x.isoformat() and r.st['locks']['senkung'] == lock
    assert r.st['hot']['next_cut_at'] == (x + timedelta(minutes=3)).isoformat()   # neuer Ausflug: 3 min nach der Senkung
    assert r.st['clock_set'] == 300                               # Starttakt (Lernwert), Senkung erst dann
    r.go((x + timedelta(minutes=3) - r.now).total_seconds() + 5, temp=86)
    assert r.st['clock_set'] == 290


def test_automatik_off_on_keeps_locks_and_the_last_reduction(miner):
    r = warm(miner)
    r.go(5, temp=86)                                              # −10, Sperre 5 min
    x, lock = r.last, r.st['locks']['senkung']
    r.go(10, temp=82, enabled=False)
    r.go(5, temp=79, enabled=True)                                # Automatik ein: Start, Sperre läuft weiter
    assert r.events('start')[-1]['trigger'] == 'Automatik ein'
    assert r.st['cut_at'] == x.isoformat() and r.st['locks']['senkung'] == lock and r.st['clock_set'] == 300
    r.go((x + timedelta(minutes=5) - r.now).total_seconds(), temp=79)
    assert r.st['clock_set'] == 300                               # keine Takterhöhung bis 5 min nach der Senkung
    r.go(5, temp=79)
    assert r.st['clock_set'] == 305


def _heating(miner, ac):
    r = warm(miner, ac, level=3)
    r.go(65, temp=60)
    assert r.st['heat'] and ac.level == 2
    return r


def test_heat_mode_ends_at_once_when_automation_is_turned_off(miner):
    """Nutzer 26.10.: ohne Regelung kein Lüfter unter der Basisstufe – Automatik aus, Miner antwortet nicht, Over Hot."""
    ac = FakeAC(level=3)
    r = _heating(miner, ac)
    ac.fail = True
    r.go(5, temp=60, enabled=False)
    assert not r.st['heat'] and ac.level == 2 and r.st.get('heat_fan_due')    # Befehl fehlgeschlagen: wiederholen
    ac.fail = False
    r.go(60, temp=60)
    assert ac.level == 3 and not r.st.get('heat_fan_due') and r.st['phase'] == 'aus'
    assert r.events('heizmodus_ende')[-1]['trigger'].startswith('Heizmodus beendet: Automatik aus, Lüfter Stufe 3')
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
    assert r.events('heizmodus_ende')[-1]['trigger'].startswith('Heizmodus beendet: Miner antwortet nicht, Lüfter Stufe 3')
    assert r.j.cycles[-1]['decision'] == 'Miner antwortet nicht'
    assert r.st['phase'] == 'basismodus' and r.j.cycles[-1]['phase'] == 'basismodus'   # shown mode = ended Heizmodus


def test_kuehlmodus_stays_while_the_miner_does_not_answer(miner):
    ac = FakeAC(level=3)
    r = warm(miner, ac, level=3)
    r.go(5, temp=88)
    miner.offline = True
    r.go(30)
    assert r.st['cool'] == {'since': (T0 + timedelta(seconds=10)).isoformat(), 'plus': 1} and ac.level == 4
    assert r.st['phase'] == 'kuehlmodus' and r.j.cycles[-1]['phase'] == 'kuehlmodus'


def test_heat_mode_stays_while_the_a15_restarts_by_button(miner):
    ac = FakeAC(level=3)
    r = _heating(miner, ac)
    r.kw['restart_req_at'] = r.now.isoformat()
    miner.offline = True
    r.go(30, temp=60)
    assert r.st['offline'] == 'neustart' and r.st['heat'] and ac.level == 2


def test_heat_mode_ends_at_once_on_over_hot(miner):
    ac = FakeAC(level=3)
    r = _heating(miner, ac)
    miner.status = 'Over Hot'
    r.go(5, temp=60)
    assert not r.st['heat'] and ac.level == 3
    assert r.events('heizmodus_ende')[-1]['trigger'].startswith('Heizmodus beendet: Over Hot, Lüfter Stufe 3')


def test_fan_thresholds_follow_the_zieltemperatur(miner):
    """Zieltemperatur 81–84 °C: Kühlmodus über 86 / 91, Ende unter 81; Heizmodus unter 71, Ende über 76."""
    ac = FakeAC(level=5)
    r = warm(miner, ac, zb=(81, 84))
    r.go(5, temp=86)
    assert not r.st.get('cool')
    r.go(5, temp=87)
    assert r.st['cool']['plus'] == 1
    r.go(5, temp=91)
    assert r.st['cool']['plus'] == 1
    r.go(5, temp=92)
    assert r.st['cool']['plus'] == 2
    r.go(5, temp=81)
    assert r.st['cool']
    r.go(5, temp=80)
    assert not r.st['cool']
    r.go(65, temp=71)
    assert not r.st.get('heat')
    r.go(5, temp=70)
    assert r.st['heat']
    r.go(5, temp=76)
    assert r.st['heat']
    r.go(5, temp=77)
    assert not r.st['heat']


# ---------------------------------------------------------------- Lernen

def test_learns_after_15_minutes_unchanged(miner):
    ac = FakeAC(level=4)
    r = started(miner, ac, level=4, learned={'4': {'mhz': 280, 'temp_avg': 82, 'at': 'x', 'count': 1}})
    r.go(15 * 60 + 30, temp=82)
    lw = r.st['learned']['4']
    assert lw['mhz'] == 280 and lw['count'] == 2 and lw['temp_avg'] == 82.0
    assert lw['ths_avg'] == 150.0                                 # Ø Hashrate der 15 min (Nutzer 28.10.)
    assert lw['j_th'] == 13.3                                     # Effizienz: 2000 W / 150 TH/s
    assert r.events('lernwert')


def test_learning_in_the_zieltemperatur_set_clock_at_the_end_is_stored(miner):
    """Nutzer 08.11. (ag C): Lernwert = gesetzter Takt am Ende der 15 min, kein Mittelwert mehr; daneben der mittlere
    Takt laut Miner nur aus Runden ohne offenen Befehl."""
    ac = FakeAC(level=4)
    r = started(miner, ac, level=4, learned={'4': {'mhz': 280, 'temp_avg': 82, 'at': 'x', 'count': 1}})
    since = r.st['learn_since']
    r.go(300, temp=82)
    r.go(30, temp=79)                                             # kurzer Ausflug: +5 → 285, Lernperiode läuft weiter
    assert r.st['learn_since'] == since and r.st['clock_set'] == 285
    r.go(600, temp=83)
    lw = r.st['learned']['4']
    assert lw['count'] == 2 and lw['mhz'] == 285 and isinstance(lw['mhz'], int)   # Mittel wäre ≈ 281
    assert 'learn_clock_sum' not in r.st
    period = [c for c in r.j.cycles if since <= c['recorded_at'].isoformat() <= lw['at']]
    free = [c['clock_reported'] for c in period if not c['pending']]
    assert len(free) < len(period)                                # Runden mit offenem Befehl zählen nicht
    assert lw['mhz_miner'] == round(sum(free) / len(free), 1)
    assert lw['mhz_miner'] != round(sum(c['clock_reported'] for c in period) / len(period), 1)
    ev = r.events('lernwert')[-1]
    assert ev['details']['takt'] == 285 and ev['details']['takt_miner'] == lw['mhz_miner']
    assert any(n[1] == 'Lernwert Stufe 4: 285 MHz (gesetzter Takt), Ø 82.5 °C (2. Mal).' for n in r.st['log'])


def test_learned_value_without_miner_clock_while_a_command_stays_open(miner):
    """Der Miner übernimmt den Takt nie (Befehl bleibt offen): kein Ø Takt laut Miner, das Feld fehlt."""
    ac = FakeAC(level=4)
    miner.ignore_clock = True
    r = started(miner, ac, level=4, learned={'4': {'mhz': 280, 'temp_avg': 82, 'at': 'x', 'count': 1}})
    r.go(15 * 60 + 30, temp=82)
    lw = r.st['learned']['4']
    assert lw['count'] == 2 and lw['mhz'] == 280 and 'mhz_miner' not in lw
    assert r.events('lernwert')[-1]['details']['takt_miner'] is None


def test_learning_period_running_at_the_update_does_not_fail(miner):
    """Lernperiode von vor v2.2.9 (learn_clock_sum, ohne learn_mclk_*): endet normal, Ø Takt laut Miner aus den
    Runden danach."""
    ac = FakeAC(level=4)
    miner.points = fa.points_for(STOCK['2/L4'], 280)
    n = 179
    state = {'v': fa.STATE_VERSION, 'seen_at': T0.isoformat(), 'level_seen': 'x', 'zb': list(ZT), 'elapsed': 900,
             'learned': {'4': dict(LW, mhz=280)}, 'clock_set': 280, 'mode_key': '2/L4', 'stock': {},
             'fan_target': 4, 'fan_set': 4, 'fan_read': 4, 'fan_read_at': T0.isoformat(),
             'learn_since': (T0 - timedelta(seconds=15 * 60)).isoformat(), 'learn_level': 4,
             'learn_sum': 82 * n, 'learn_n': n, 'learn_clock_sum': 280.0 * n, 'learn_dh_sum': 1.2 * n,
             'learn_dh_n': n, 'learn_ths_sum': 150.0 * n, 'learn_ths_n': n, 'learn_w_sum': 2000.0 * n, 'learn_w_n': n}
    r = Run(miner, ac, 4, state=state).go(5, temp=82)
    lw = r.st['learned']['4']
    assert not r.st.get('last_error') and lw['count'] == 2 and lw['mhz'] == 280
    assert lw['mhz_miner'] == round(sum(miner.points) / 4, 1)    # nur die Runde nach dem Update
    assert r.st['learn_mclk_n'] == 0 and r.st['learn_mclk_sum'] == 0.0    # neue Lernperiode


def test_short_dip_below_ztu_keeps_learning_long_one_restarts(miner):
    """Lernbereich ZTU–ZTO: Ausflüge darunter bis 60 s brechen nicht ab (Taktschritte auch nicht)."""
    ac = FakeAC(level=4)
    r = started(miner, ac, level=4, learned={'4': {'mhz': 280, 'temp_avg': 82, 'at': 'x', 'count': 1}})
    r.go(60, temp=82)
    since = r.st['learn_since']
    r.go(120, temp=80)                                            # ZTU: im Lernbereich
    r.go(30, temp=79)                                             # 30 s Ausflug (+5 MHz): Lernperiode läuft weiter
    r.go(30, temp=82)
    assert r.st['learn_since'] == since and r.st['clock_set'] == 285
    r.go(70, temp=79)                                             # länger als 60 s: neu
    assert r.st['learn_since'] is None and r.events('lernen_ende')[-1]['details']['ergebnis'] == 'abgebrochen'


def test_fan_change_restarts_learning(miner):
    ac = FakeAC(level=4)
    r = started(miner, ac, level=4, learned={'4': {'mhz': 280, 'temp_avg': 82, 'at': 'x', 'count': 1}})
    r.go(60, temp=82)
    r.go(5, temp=88)                                              # Kühlmodus → Lüfter + 1
    assert r.st['learn_since'] is None


def test_learned_value_only_at_a_mean_error_rate_below_5_percent(miner):
    ac = FakeAC(level=4)
    old = {'mhz': 280, 'temp_avg': 82, 'at': 'x', 'count': 1}
    miner.dh = 5.0
    r = started(miner, ac, level=4, learned={'4': dict(old)})
    r.go(15 * 60 + 30, temp=82)
    assert r.st['learned']['4'] == old and not r.events('lernwert')
    end = r.events('lernen_ende')[-1]
    assert end['details']['ergebnis'] == 'verworfen' and end['details']['grund'] == 'Fehlerrate 5.0 % (Grenze 5 %)'
    assert r.st['learn_since'] == (T0 + timedelta(minutes=15)).isoformat()   # die Lernperiode beginnt neu
    miner.dh = 4.9                                                # unter 5 % (vorher galt 4 %)
    r.go(15 * 60, temp=82)
    assert r.st['learned']['4']['count'] == 2 and r.events('lernwert')


def test_learned_value_at_fan_level_1_below_7_percent(miner):
    """Nutzer 09.11. (aq): nur bei Lüfterstufe 1 gilt die Grenze 7 %, sonst weiter 5 %."""
    ac = FakeAC(level=1)
    old = {'mhz': 200, 'temp_avg': 82, 'at': 'x', 'count': 1}
    miner.dh = 7.0
    r = started(miner, ac, level=1, learned={'1': dict(old)})
    r.go(15 * 60 + 30, temp=82)
    assert r.st['learned']['1'] == old and not r.events('lernwert')
    end = r.events('lernen_ende')[-1]
    assert end['details']['ergebnis'] == 'verworfen' and end['details']['grund'] == 'Fehlerrate 7.0 % (Grenze 7 %)'
    assert any(n[1] == 'Lernwert Stufe 1 verworfen: Fehlerrate 7.0 % (Grenze 7 %).' for n in r.st['log'])
    miner.dh = 6.9                                                # Stufe 1: unter 7 %
    r.go(15 * 60, temp=82)
    assert r.st['learned']['1']['count'] == 2 and r.events('lernwert')


def test_learned_value_at_fan_level_2_still_below_5_percent(miner):
    ac = FakeAC(level=2)
    old = {'mhz': 250, 'temp_avg': 82, 'at': 'x', 'count': 1}
    miner.dh = 6.0
    r = started(miner, ac, level=2, learned={'2': dict(old)})
    r.go(15 * 60 + 30, temp=82)
    assert r.st['learned']['2'] == old and not r.events('lernwert')
    assert r.events('lernen_ende')[-1]['details']['grund'] == 'Fehlerrate 6.0 % (Grenze 5 %)'


def test_learned_value_at_fan_level_2_with_base_level_1_still_below_5_percent(miner):
    """Die 7 % gelten für die Lüfterstufe 1, nicht für die Basisstufe 1: Kühlmodus +1 (Stufe 2) bei Basisstufe 1."""
    ac = FakeAC(level=1)
    miner.dh = 6.0
    r = warm(miner, ac, level=1, mhz=200)
    r.go(5, temp=88)                                              # Kühlmodus: Basisstufe 1 +1
    r.go(15 * 60 + 30, temp=84)                                   # bleibt (ZTO−3 nicht unterschritten)
    assert r.st['cool'] and r.st['fan_target'] == 2 and '2' not in r.st['learned'] and not r.events('lernwert')
    assert r.events('lernen_ende')[-1]['details']['grund'] == 'Fehlerrate 6.0 % (Grenze 5 %)'
    assert any(n[1] == 'Lernwert Stufe 2 verworfen: Fehlerrate 6.0 % (Grenze 5 %).' for n in r.st['log'])


def test_learned_value_stores_the_mode_of_the_learning_period(miner):
    """Nutzer 09.11. (ap): ein neuer Lernwert speichert den Modus (hier mode 03 für 370 MHz), auch im Protokoll;
    alte Lernwerte bleiben ohne."""
    ac = FakeAC(level=4)
    r = started(miner, ac, level=4, learned={'4': {'mhz': 370, 'temp_avg': 82, 'at': 'x', 'count': 1}, '3': dict(LW)})
    r.go(20 * 60, temp=82)
    assert miner.level == 3
    lw = r.st['learned']['4']
    assert lw['count'] == 2 and lw['mode'] == '2/L3'
    assert r.events('lernwert')[-1]['details']['modus'] == '2/L3'
    assert 'mode' not in r.st['learned']['3']


def test_learned_value_without_a_known_mode_has_no_mode(miner):
    """Einstellungsseite seit dem Start nie gelesen und kein gemerkter Modus: das Feld fehlt."""
    ac = FakeAC(level=4)
    miner.points = fa.points_for(STOCK['2/L4'], 280)
    miner.web_down = miner.never_read = True
    n = 179
    state = {'v': fa.STATE_VERSION, 'seen_at': T0.isoformat(), 'level_seen': 'x', 'zb': list(ZT), 'elapsed': 900,
             'learned': {'4': dict(LW, mhz=280)}, 'clock_set': 280, 'stock': {},
             'fan_target': 4, 'fan_set': 4, 'fan_read': 4, 'fan_read_at': T0.isoformat(),
             'learn_since': (T0 - timedelta(seconds=15 * 60)).isoformat(), 'learn_level': 4,
             'learn_sum': 82 * n, 'learn_n': n, 'learn_mclk_sum': 280.0 * n, 'learn_mclk_n': n, 'learn_dh_sum': 1.2 * n,
             'learn_dh_n': n, 'learn_ths_sum': 150.0 * n, 'learn_ths_n': n, 'learn_w_sum': 2000.0 * n, 'learn_w_n': n}
    r = Run(miner, ac, 4, state=state).go(5, temp=82)
    lw = r.st['learned']['4']
    assert r.st['mode_key'] is None and lw['count'] == 2 and 'mode' not in lw
    assert r.events('lernwert')[-1]['details']['modus'] is None


# ---------------------------------------------------------------- Situationen

def test_restart_of_the_miner_starts_again(miner):
    r = warm(miner, mhz=250)
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


def test_over_hot_no_decision_then_start(miner):
    r = warm(miner, mhz=250)
    miner.status = 'Over Hot'
    sends = len(miner.clock_sets)
    r.go(30, temp=95)
    assert len(miner.clock_sets) == sends and r.events('over_hot') and not r.st.get('cool')
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


def test_start_after_restart_even_with_invalid_temperature(miner):
    r = warm(miner, mhz=250)
    r.go(30, temp=82)
    miner.elapsed = 10
    miner.points = list(STOCK['2/L4'])
    r.go(5, temp=-273)                                            # booting: TMax noch ungültig
    assert r.events('start')[-1]['trigger'] == 'Neustart des Miners' and miner.clock() == 250


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


# ---------------------------------------------------------------- Takt laut Miner weicht ab (Nutzer 06.11.)

def test_reported_clock_16_mhz_off_for_15_min_is_sent_again(miner):
    """Beispiel der Freigabe: gesetzt 300, gemeldet 284 zählt."""
    r = warm(miner)
    r.go(60, temp=82)
    miner.points = [284] * 4
    since = r.now
    r.go(15 * 60, temp=82)
    assert not r.events('firmware_takt') and r.st['fw_dev'] == {'since': since.isoformat(), 'clock': 300}
    r.go(5, temp=82)                                              # 15 min ununterbrochen
    ev = r.events('firmware_takt')
    assert len(ev) == 1 and ev[0]['trigger'] == ('Takt laut Miner 284 MHz, gesetzt 300 MHz: seit 15 min mehr als '
                                                 '15 MHz Abweichung')
    assert miner.clock() == 300 and r.st['fw_dev'] is None
    assert r.events('takt')[-1]['trigger'] == 'Takt laut Miner weicht ab: erneut senden'


@pytest.mark.parametrize('points', [[285] * 4, [315] * 4])
def test_15_mhz_off_does_not_count(miner, points):
    """285 bzw. 315 bei gesetzt 300: genau 15 MHz – zählt nicht."""
    r = warm(miner)
    miner.points = points
    r.go(20 * 60, temp=82)
    assert not r.events('firmware_takt') and r.st.get('fw_dev') is None


def test_a_break_starts_the_15_min_anew(miner):
    r = warm(miner)
    miner.points = [316] * 4                                      # 16 MHz darüber zählt auch
    r.go(10 * 60, temp=82)
    miner.points = [300] * 4                                      # ≤ 15 MHz: die 15 min beginnen neu
    r.go(5, temp=82)
    miner.points = [316] * 4
    r.go(10 * 60, temp=82)
    miner.offline = True                                          # ohne Wert: neu
    r.go(5)
    miner.offline = False
    r.go(15 * 60, temp=82)
    assert not r.events('firmware_takt')
    r.go(5, temp=82)
    assert len(r.events('firmware_takt')) == 1


def test_maintenance_starts_the_15_min_anew_while_the_a15_still_answers(miner):
    """Wartung counts as no value (Nutzer 06.11.) – also while the A15 still answers."""
    ip = '10.0.0.15'
    r = warm(miner)
    miner.points = [284] * 4
    r.go(10 * 60, temp=82)
    assert r.st['fw_dev']['clock'] == 300
    miner_cache.put(ip, 'estats', 'x')
    try:
        r.go(10 * 60, temp=82, maintenance_at=r.now.isoformat())
        assert not r.events('firmware_takt') and r.st['fw_dev'] is None
    finally:
        miner_cache.clear(ip)


def test_changed_set_clock_starts_the_15_min_anew(miner):
    r = warm(miner)
    miner.points = [284] * 4
    r.go(10 * 60, temp=82)
    assert r.st['fw_dev']['clock'] == 300
    miner.ignore_clock = True
    r.go(5, temp=79)                                              # Takterhöhung auf 305: Befehl offen
    r.go(5, temp=82)
    assert r.st['fw_dev'] is None and r.st['pending']['value'] == 305
    miner.points = [283] * 4                                      # gilt als übernommen, weiter mehr als 15 MHz ab
    miner.ignore_clock = False
    since = r.now
    r.go(5, temp=82)
    assert r.st['fw_dev'] == {'since': since.isoformat(), 'clock': 305}   # die 15 min beginnen beim neuen Takt neu
    r.go(15 * 60 - 5, temp=82)
    assert not r.events('firmware_takt')
    r.go(5, temp=82)
    assert len(r.events('firmware_takt')) == 1 and miner.clock() == 305


def test_resend_with_mode_first(miner):
    r = warm(miner, mhz=390)                                      # mode 02
    miner.level, miner.points = 4, list(STOCK['2/L4'])           # der Miner fällt auf mode 04 zurück (360 MHz)
    r.go(15 * 60 + 5, temp=82)
    kinds = [k for k in r.j.kinds() if k in ('firmware_takt', 'modus', 'takt')]
    assert kinds[-2:] == ['firmware_takt', 'modus']               # Modus zuerst ...
    r.go(10, temp=82)
    kinds = [k for k in r.j.kinds() if k in ('firmware_takt', 'modus', 'takt')]
    assert kinds[-3:] == ['firmware_takt', 'modus', 'takt']       # ... dann der Takt
    assert miner.level == 2 and miner.clock() == 390


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
    r.go(30, temp=82)
    assert [round(x) for x in miner.clock_sets].count(170) == 1 and not r.events('firmware_takt')   # Stufe 1 = 170


# ---------------------------------------------------------------- Startpause (bis ZTU−5, Nutzer 06.11.)

def test_start_hold_after_restart_until_ztu_minus_5(miner):
    """25.10. live: nach dem Neustart meldete der Miner 26 °C, die Regelung setzte +40 MHz → 85 °C. Startpause bis
    ZTU−5 (hier 75 °C), höchstens 10 min."""
    r = warm(miner)
    r.go(30, temp=82)
    miner.elapsed = 10                                            # Neustart
    start = r.now
    r.go(5, temp=26)
    ev = r.events('startpause')
    until = start + timedelta(minutes=10)
    assert len(ev) == 1 and ev[0]['trigger'] == 'Startpause: keine Takterhöhung bis 75 °C, spätestens %s' % hm(until)
    assert ev[0]['details']['grund'] == 'Neustart des Miners'
    assert r.st['fan_target'] == 5 and not r.st.get('heat')       # Start: Basisstufe (kein Lüfter 1 mehr)
    r.go(60, temp=26)
    r.go(6 * 60, temp=74)
    assert r.st['clock_set'] == 300                               # langsam warm: keine Takterhöhung
    r.go(5, temp=75)                                              # ZTU−5 erreicht: Pause vorbei, gleich +15
    assert not r.st.get('start_hold')
    assert r.events('startpause_ende')[-1]['trigger'] == 'Startpause beendet: 75 °C erreicht (nach 7 min)'
    assert r.st['clock_set'] == 315


def test_start_hold_ends_after_10_min_at_the_latest(miner):
    r = warm(miner)
    r.go(30, temp=82)
    miner.elapsed = 10
    r.go(10 * 60 - 5, temp=70)
    assert r.st['clock_set'] == 300
    r.go(10, temp=70)                                             # ZTU −10: +20
    assert r.st['clock_set'] == 320 and not r.st.get('start_hold')
    assert r.events('startpause_ende')[-1]['trigger'] == 'Startpause beendet: 10 min vorbei (Chiptemperatur 70 °C)'


def test_start_hold_ends_at_a_jump_above_zto_and_the_reduction_follows(miner):
    r = warm(miner)
    r.go(30, temp=82)
    miner.elapsed = 10
    r.go(30, temp=70)
    assert r.st.get('start_hold')
    r.go(5, temp=86)                                              # über ZTU−5: Pause vorbei; Senkung über ZTO
    assert not r.st.get('start_hold') and r.st['clock_set'] == 290
    assert r.events('startpause_ende')[-1]['trigger'] == 'Startpause beendet: 75 °C erreicht (nach 0 min)'


def test_start_hold_after_over_hot(miner):
    r = warm(miner)
    r.go(30, temp=82)
    miner.status = 'Over Hot'
    r.go(10, temp=82)
    miner.status = 'Work: In Work'
    r.go(5, temp=40)
    assert r.events('startpause') and r.events('start')[-1]['trigger'] == 'nach Over Hot'
    r.go(60, temp=40)
    assert r.st['clock_set'] == 300


def test_no_start_hold_when_base_level_is_chosen(miner):
    """Chips warm: Basisstufe gewählt, Automatik ein – keine Startpause."""
    r = warm(miner)
    r.go(30, temp=82)
    r.kw['level_at'] = 'neu'
    r.go(10, temp=72)                                             # ZTU −8: gleich +20
    assert not r.events('startpause') and r.st['clock_set'] == 320


def test_start_hold_also_when_another_start_reason_follows_in_the_same_round(miner):
    """Review 25.10.: Neustart erkannt und in derselben Runde Basisstufe gewählt – die Chips sind trotzdem kalt."""
    r = warm(miner)
    r.go(30, temp=82)
    miner.elapsed = 10
    r.kw['level_at'] = 'neu'
    r.go(5, temp=26)
    assert r.events('start')[-1]['trigger'].startswith('Basisstufe gewählt') and r.events('startpause')
    r.go(60, temp=26)
    assert r.st['clock_set'] == 300


def test_start_hold_follows_the_zieltemperatur(miner):
    r = warm(miner, zb=(82, 86))
    r.go(30, temp=84)
    miner.elapsed = 10
    r.go(5, temp=26)
    assert r.events('startpause')[-1]['trigger'].startswith('Startpause: keine Takterhöhung bis 77 °C, spätestens ')
    r.go(120, temp=76)
    assert r.st.get('start_hold') and r.st['clock_set'] == 300
    r.go(5, temp=77)                                              # ZTU−5 (82 − 5): +15
    assert not r.st.get('start_hold') and r.st['clock_set'] == 315
    assert r.events('startpause_ende')[-1]['trigger'] == 'Startpause beendet: 77 °C erreicht (nach 2 min)'


# ---------------------------------------------------------------- Neustart sauber anzeigen (Nutzer 25.10.)

def test_restart_by_button_offline_is_expected(miner):
    r = warm(miner)
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
    r = warm(miner)
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
    r = warm(miner)
    r.go(30, temp=82)
    miner.offline = True
    r.go(30)
    miner.offline = False
    r.go(4 * 60, temp=82)                                         # kein Neustart in den 3 min danach
    miner.elapsed = 10
    r.go(5, temp=26)
    assert r.events('miner_offline') and r.events('miner_offline')[0]['ok'] is False


def test_restart_entries_carry_no_red_error_text(miner):
    """Review 26.10.: die Protokoll-Seite zeigt error immer rot – bei Neustart-Einträgen steht die Meldung in details."""
    r = warm(miner)
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
    r = warm(miner)
    r.go(30, temp=82)
    r.kw['restart_req_at'] = r.now.isoformat()
    r.go(5, temp=82)
    miner.offline = True
    r.go(4 * 60)
    assert r.st['offline'] == 'neustart' and not r.events('miner_offline')
    r.go(60)
    assert r.st['offline'] is True and r.events('miner_offline')[0]['ok'] is False


def test_button_restart_does_not_relabel_entries_before_the_click(miner):
    r = warm(miner)
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


def test_automatik_off_on_does_not_revive_an_old_button_restart(miner):
    r = warm(miner)
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
    r = warm(miner)
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
    r = warm(miner)
    r.go(30, temp=82)
    miner.web_down = True
    r.go(60, temp=82)
    assert not r.events('einstellungsseite_aus') and not r.events('miner_offline')
    r.go(5, temp=86)                                              # 100 s in ZT: −10
    assert r.st['clock_set'] == 290                               # regelt weiter
    r.go(120, temp=82)
    ev = r.events('einstellungsseite_aus')
    assert len(ev) == 1 and ev[0]['ok'] is None and ev[0]['trigger'] == ('A15-Einstellungsseite antwortet nicht – '
                                                                       'Mining-Schnittstelle ok')
    miner.web_down = False
    r.go(5, temp=82)
    assert r.events('einstellungsseite_an')[0]['trigger'] == 'A15-Einstellungsseite wieder erreichbar nach 3 min'
    assert not r.events('miner_offline')


def test_short_settings_page_drop_no_entry(miner):
    r = warm(miner)
    r.go(30, temp=82)
    miner.web_down = True
    r.go(100, temp=82)
    miner.web_down = False
    r.go(10, temp=82)
    assert not r.events('einstellungsseite_aus') and not r.events('einstellungsseite_an')


def test_mode_switch_waits_for_the_settings_page(miner):
    """Ein Moduswechsel braucht die Pool-Felder der Einstellungsseite: nie mit alten Daten, er wartet."""
    r = warm(miner, mhz=358)
    miner.web_down = True
    r.go(10, temp=79)                                             # +5 → 363 → mode 03 nötig
    assert miner.mode_sets == [] and len(r.events('modus_wartet')) == 1
    assert r.st['clock_set'] == 360 and not r.st['queue']         # Takt bis zum Standardtakt von mode 04
    r.go(240, temp=79)                                            # mehr geht erst mit dem wartenden Moduswechsel
    assert miner.mode_sets == [] and len(r.events('modus_wartet')) == 1 and r.st['clock_set'] == 360
    miner.web_down = False
    r.go(10, temp=79)                                             # Seite wieder da: der nächste Plus-Schritt mit Modus
    assert miner.mode_sets == [3] and miner.clock() == r.st['clock_set'] == 365 and r.st.get('mode_wait') is None


def test_waiting_mode_switch_does_not_block_further_reductions(miner):
    """Review 26.10.: wartete der Moduswechsel in der Befehlsfolge, kam keine weitere Absenkung mehr (busy). Jetzt wartet
    er außerhalb, die Senkungen laufen weiter."""
    ac = FakeAC(level=5)
    miner.level, miner.points = 3, list(STOCK['2/L3'])
    r = started(miner, ac, level=5, learned={'5': {'mhz': 370, 'temp_avg': 82, 'at': 'x', 'count': 1}})
    r.go(30, temp=82)
    miner.web_down = True
    r.go(10, temp=95)                                             # Bereich 4: 370 − 50 = 320 → mode 04 nötig
    assert r.st['clock_set'] == 320 and miner.mode_sets == [] and r.events('modus_wartet')
    r.go(120, temp=95)
    assert r.st['clock_set'] == 270                               # weitere Senkung trotz wartendem Moduswechsel


def test_settings_never_read_keeps_the_last_known_mode(miner):
    """Review 26.10.: nach einem Data-Service-Neustart ohne gelesene Einstellungen kein rotes „antwortet nicht“ –
    die Automatik regelt mit dem zuletzt bekannten Modus weiter."""
    r = warm(miner)
    r.go(30, temp=82)
    miner.web_down = miner.never_read = True
    r.go(5, temp=86)
    assert not r.events('miner_offline') and r.st['mode_key'] == '2/L4' and r.st['clock_set'] == 290


def test_waiting_mode_switch_caps_the_clock_at_the_current_mode(miner):
    """Review 26.10.: wartete der Moduswechsel, stieg der gesetzte Takt bis 440 MHz, obwohl der Miner beim Standardtakt
    von mode 04 blieb. Jetzt höchstens der Standardtakt des Modus."""
    r = warm(miner, mhz=350)
    r.go(30, temp=82)
    miner.web_down = True
    r.go(6 * 60, temp=65)
    assert r.st['clock_set'] == 360 and miner.mode_sets == []
    assert len(r.events('modus_wartet')) == 1
    miner.web_down = False
    r.go(10, temp=65)
    assert r.st['clock_set'] <= 385                               # danach wieder in Schritten (+25), kein Sprung auf 440


# ---------------------------------------------------------------- Fehlerrate: automatischer Neustart (Nutzer 06.11.)

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


def test_auto_restart_after_30_min_above_10_percent_also_right_after_a_start(miner, monkeypatch):
    """Keine Ausnahmen nach einem Start mehr: 30 min über 10 % ab dem Start → Neustart."""
    sock = FakeSocket([REBOOT_REPLY, b''])
    conns = _connect(monkeypatch, sock)
    miner.dh = 10.5
    r = warm(miner)                                               # Start bei T0, über 10 % ab T0
    r.go(30 * 60 - 10, temp=82)
    assert not r.events('neustart_auto') and conns == [] and r.st['dh_since'] == T0.isoformat()
    r.go(5, temp=82)                                              # 30 min ununterbrochen über 10 %
    at = r.last.isoformat()
    ev = r.events('neustart_auto')
    assert len(ev) == 1 and ev[0]['trigger'] == 'Automatischer Neustart: Fehlerrate 10.5 % seit 30 min über 10 % (Takt 300 MHz)'
    assert ev[0]['ok'] is True and ev[0]['request'] == 'ascset|0,reboot,0' and ev[0]['response'].startswith('STATUS=S')
    assert conns == [('10.0.0.15', 4028)] and sock.sent == [b'ascset|0,reboot,0']
    assert r.st['auto_restart_at'] == at and r.st['restart_req'] == at and r.st['clock_set'] == 300
    assert r.st['dh_since'] is None                               # nach dem Versuch zählen die 30 min neu
    r.go(5, temp=82)
    assert r.st['dh_since'] == r.last.isoformat()
    miner.offline = True                                          # die A15 startet neu: erwartet, kein Fehler
    r.go(30)
    assert r.st['offline'] == 'neustart' and not r.events('miner_offline') and r.events('neustart_offline')
    miner.offline, miner.elapsed, miner.dh = False, 10, 1.0
    r.go(5, temp=26)
    assert r.events('start')[-1]['trigger'] == 'Neustart des Miners' and r.st.get('start_hold')


@pytest.mark.parametrize('low', [10.0, None])
def test_a_value_of_10_percent_or_without_value_starts_the_30_min_anew(miner, monkeypatch, low):
    conns = _connect(monkeypatch, FakeSocket([b'']))
    r = warm(miner)
    miner.dh = 12.0
    r.go(29 * 60, temp=82)
    miner.dh = low
    r.go(5, temp=82)
    miner.dh = 12.0
    r.go(30 * 60, temp=82)
    assert conns == [] and not r.events('neustart_auto')
    r.go(5, temp=82)                                              # 30 min nach dem Wert ≤ 10 % bzw. ohne Wert
    assert len(conns) == 1 and len(r.events('neustart_auto')) == 1


@pytest.mark.parametrize('case', ['wartung', 'overhot', 'offline'])
def test_not_counted_during_wartung_over_hot_and_without_answer(miner, monkeypatch, case):
    calls = []
    monkeypatch.setattr(calibration, 'reboot', lambda ip, port: calls.append(ip) or 'ok')
    r = warm(miner)
    miner.dh = 12.0
    r.go(20 * 60, temp=82)
    if case == 'wartung':
        miner_cache.put('10.0.0.15', 'estats', 'x')               # A15 antwortet noch
        r.kw['maintenance_at'] = r.now.isoformat()
    elif case == 'offline':
        miner.offline = True
    else:
        miner.status = 'Over Hot'
    r.go(15 * 60, temp=82)
    miner_cache.clear('10.0.0.15')
    assert not r.events('neustart_auto') and calls == [] and not r.st.get('dh_since')
    miner.status, miner.offline = 'Work: In Work', False
    r.kw['maintenance_at'] = None
    r.go(20 * 60, temp=82)
    assert calls == []                                            # danach zählen die 30 min neu


def test_at_most_one_auto_restart_per_6_h_one_note(miner, monkeypatch):
    conns = _connect(monkeypatch, FakeSocket([b'']))
    r = warm(miner)
    last = r.now - timedelta(hours=6) + timedelta(minutes=40)     # letzter automatischer Neustart: Sperre noch 40 min
    r.st['auto_restart_at'] = last.isoformat()
    miner.dh = 12.0
    r.go(35 * 60, temp=82)                                        # 30 min über 10 %: gesperrt, ein Hinweis
    text = ('Fehlerrate 12.0 % seit 30 min über 10 %: kein automatischer Neustart (höchstens einer je 6 h, letzter um '
            + hm(last) + ').')
    assert [n[1] for n in r.st['log'] if 'kein automatischer Neustart' in n[1]] == [text]
    assert conns == [] and not r.events('neustart_auto')
    r.go(5 * 60, temp=82)
    assert conns == []                                            # bis genau 6 h nach dem letzten gesperrt
    r.go(5, temp=82)                                              # Sperre vorbei, noch über 10 %: Neustart
    assert len(conns) == 1 and len(r.events('neustart_auto')) == 1
    assert len([n for n in r.st['log'] if 'kein automatischer Neustart' in n[1]]) == 1


def test_only_auto_restarts_of_this_rule_count_for_6_h(miner, monkeypatch):
    conns = _connect(monkeypatch, FakeSocket([b'']))
    r = warm(miner)
    r.kw['restart_req_at'] = r.now.isoformat()                    # Knopf „A15 neu starten“ (Neustart des Nutzers)
    r.go(5, temp=82)
    miner.elapsed = 10                                            # ... und ein Neustart
    r.go(5, temp=82)
    miner.dh = 12.0
    r.go(30 * 60 + 5, temp=82)
    assert len(conns) == 1 and len(r.events('neustart_auto')) == 1


def test_auto_restart_failure_retries_after_5_min_and_does_not_count(miner, monkeypatch):
    conns = _connect(monkeypatch, error=OSError('[Errno 61] Connection refused'))
    r = warm(miner)
    miner.dh = 12.0
    r.go(30 * 60 + 5, temp=82)
    assert len(conns) == 1 and not r.events('neustart_auto')
    err = [e for e in r.events('fehler') if e['details'].get('grund') == 'neustart_auto'][-1]
    assert err['ok'] is False and 'Connection refused' in err['error']
    assert err['trigger'] == 'Automatischer Neustart: Befehl nicht gesendet'
    assert r.st['log'][-1][1].startswith('Automatischer Neustart fehlgeschlagen: [Errno 61] Connection refused')
    assert not r.st.get('auto_restart_at') and not r.st.get('restart_req')
    r.go(5 * 60 - 5, temp=82)
    assert len(conns) == 1                                        # nächster Versuch nach 5 min
    sock = FakeSocket([b''])
    _connect(monkeypatch, sock)
    r.go(5, temp=82)
    assert len(r.events('neustart_auto')) == 1 and sock.sent == [b'ascset|0,reboot,0'] and r.st['auto_restart_at']


def test_auto_restart_independent_of_the_set_clock(miner, monkeypatch):
    conns = _connect(monkeypatch, FakeSocket([b'']))
    r = warm(miner)
    miner.dh = 12.0
    r.go(10 * 60, temp=82)
    r.go(5, temp=79)                                              # Takt +5: die 30 min laufen weiter
    r.go(10 * 60, temp=82)
    r.go(5, temp=82, level_at='neu')                              # Start: auch
    assert r.st['clock_set'] == 300 and conns == []
    r.go(10 * 60 - 10, temp=82)
    assert conns == []
    r.go(5, temp=82)
    assert len(conns) == 1 and len(r.events('neustart_auto')) == 1


# ---------------------------------------------------------------- Zieltemperatur (Nutzer 27.10./06.11.)

def _without_range(miner, ac, lo, hi, tmax):
    """Zustand vor dem 27.10.: Lernwerte, aber keine gemerkte Zieltemperatur."""
    r = Run(miner, ac, 5)
    r.st = {'v': fa.STATE_VERSION, 'seen_at': T0.isoformat(), 'learned': {'5': LW}, 'level_seen': 'x'}
    r.kw.update(level_at='klick', target_low=lo, target_high=hi)
    miner.tmax = tmax
    return r.go(10)


def test_first_round_without_a_range_keeps_the_learned_values(miner):
    """Stand vor dem 27.10. (keine gemerkte Zieltemperatur): die Lernwerte gehören zum Startwert 82–85, kein Eintrag."""
    ac = FakeAC(level=5)
    r = _without_range(miner, ac, 82, 85, 83)
    assert r.st['zb'] == [82, 85] and r.st['learned'] == {'5': LW} and 'learned_zb' not in r.st
    assert not [n for n in r.st['log'] if 'Zieltemperatur' in n[1]]
    assert r.events('start')[-1]['details']['quelle'] == 'Lernwert' and r.st['clock_set'] == 300


def test_first_round_without_a_range_but_another_range_chosen(miner):
    """Review 27.10.: vor der ersten Runde schon eine andere Zieltemperatur gewählt – die Lernwerte bleiben beim Startwert
    82–85; die neue startet mit dem Lernwert der nächsten (Nutzer 28.10.)."""
    ac = FakeAC(level=5)
    r = _without_range(miner, ac, 80, 84, 82)
    assert r.st['zb'] == [80, 84] and r.st['learned_zb'] == {'82-85': {'5': LW}} and r.st['learned'] == {}
    assert r.events('start')[-1]['details']['quelle'] == 'Lernwert 82–85' and r.st['clock_set'] == 300
    assert any(n[1] == 'Zieltemperatur 80–84 °C übernommen.' for n in r.st['log'])


def test_range_change_leaves_the_saved_state_untouched(miner):
    """Review 27.10.: scheitert die Runde nach dem Wechsel, speichert run_fan_automation den alten Zustand – dessen
    Archiv bleibt vollständig."""
    other = {'mhz': 340, 'temp_avg': 84, 'at': 'x', 'count': 1}
    state = {'v': fa.STATE_VERSION, 'zb': [82, 86], 'learned': {'5': other}, 'learned_zb': {'80-85': {'5': LW}}}
    rnd = fa.Round(row(state, level=5, target_low=80, target_high=85), T0, FakeAC(level=5),
                   automation_journal.MemoryJournal())
    rnd.target_range()
    assert rnd.st['learned'] == {'5': LW} and rnd.st['learned_zb'] == {'82-86': {'5': other}}
    assert state['learned_zb'] == {'80-85': {'5': LW}}


def test_range_change_takes_the_new_limits_at_once_without_a_start(miner):
    ac = FakeAC(level=5)
    r = warm(miner, ac)
    r.go(60, temp=83)
    assert r.st['zb'] == [80, 85] and r.st['learn_since']
    sets, starts = list(miner.clock_sets), len(r.events('start'))
    r.kw.update(target_low=82, target_high=86)
    r.go(5, temp=83)
    assert r.st['zb'] == [82, 86] and r.st['learned'] == {} and r.st['learned_zb'] == {'80-85': {'5': LW}}
    assert r.events('lernen_ende')[-1]['details']['grund'] == 'Zieltemperatur geändert'
    assert r.st['zt_since'] == r.last.isoformat()                 # ZeitImZieltemperatur neu gemessen
    assert any(n[1] == 'Zieltemperatur 82–86 °C übernommen.' for n in r.st['log'])
    assert len(r.events('start')) == starts and miner.clock_sets == sets and ac.level == 5
    r.go(5, temp=81)                                              # 81 °C liegt jetzt unter ZTU: gleich +5
    assert r.st['clock_set'] == 305


def test_learned_values_are_kept_per_range(miner):
    """Nutzer 27.10.: zurück in der alten Zieltemperatur gelten wieder ihre Lernwerte; eine neue beginnt mit dem
    Lernwert der nächsten (Nutzer 28.10.) und lernt eigene."""
    r = warm(miner)
    r.go(30, temp=83)
    r.kw.update(target_low=82, target_high=86, level_at='neu')
    r.go(10, temp=83)
    assert r.events('start')[-1]['details']['quelle'] == 'Lernwert 80–85' and r.st['clock_set'] == 300
    assert r.st['learned'] == {}
    r.go(15 * 60 + 30, temp=83)
    assert r.st['learned']['5']['mhz'] == 300 and r.st['learned']['5']['temp_avg'] == 83.0
    r.kw.update(target_low=80, target_high=85)
    r.go(5, temp=83)
    assert r.st['learned'] == {'5': LW} and r.st['learned_zb']['82-86']['5']['temp_avg'] == 83.0
    r.kw['level_at'] = 'neu2'
    r.go(10, temp=83)
    assert r.events('start')[-1]['details']['quelle'] == 'Lernwert' and r.st['clock_set'] == 300


def test_automatik_off_on_keeps_the_range_and_the_learned_values_per_range(miner):
    r = warm(miner)
    r.go(30, temp=83)
    r.kw.update(target_low=82, target_high=86)
    r.go(5, temp=83)
    r.kw['enabled'] = False
    r.go(10)
    r.kw['enabled'] = True
    r.go(10, temp=83)
    assert r.st['zb'] == [82, 86] and r.st['learned_zb'] == {'80-85': {'5': LW}}
    assert len([n for n in r.st['log'] if 'Zieltemperatur' in n[1]]) == 1
    r.kw.update(target_low=80, target_high=85)
    r.go(5, temp=83)
    assert r.st['learned'] == {'5': LW}


A = dict(LW, mhz=360)
B = dict(LW, mhz=280)


def _with_state(miner, ac, level, state, zb=ZT, tmax=82):
    """Zustand vorgegeben, dann Basisstufe gewählt (Start)."""
    r = Run(miner, ac, level)
    r.st = dict({'v': fa.STATE_VERSION, 'seen_at': T0.isoformat(), 'level_seen': 'x', 'zb': list(zb)}, **state)
    r.kw.update(level_at='klick', target_low=zb[0], target_high=zb[1])
    miner.tmax = tmax
    return r.go(10)


@pytest.mark.parametrize('zb, archive, mhz, quelle', [
    ((82, 86), {'77-80': {'5': A}, '84-88': {'5': B}, '87-91': {'5': dict(LW, mhz=260)}}, 280, 'Lernwert 84–88'),
    ((81, 85), {'79-83': {'5': A}, '83-87': {'5': B}}, 360, 'Lernwert 79–83'),          # gleich weit: die kühlere
    ((81, 85), {'77-80': {'5': A}, '84-88': {'3': B}}, 360, 'Lernwert 77–80'),          # ohne die Stufe: die nächste mit ihr
    ((81, 85), {'84-88': {'3': B}}, 400, 'Initialwert'),
])
def test_start_with_the_learned_value_of_the_nearest_range(miner, zb, archive, mhz, quelle):
    ac = FakeAC(level=5)
    r = _with_state(miner, ac, 5, {'learned': {}, 'learned_zb': archive}, zb=zb)
    start = r.events('start')[-1]
    assert start['details']['quelle'] == quelle and r.st['clock_set'] == mhz and miner.clock() == mhz
    assert r.st['learned'] == {}                                  # wird nicht übernommen, nur verwendet


class _Cur:
    def __init__(self, seen):
        self.seen = seen

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, args=None):
        self.seen.append(sql)

    def fetchall(self):
        return []


def _select_sql(monkeypatch):
    seen = []

    class Conn:
        def cursor(self, cursor_factory=None):
            return _Cur(seen)

        def close(self):
            pass

    monkeypatch.setenv('MINERSENTINEL_ENV', 'Live')
    monkeypatch.setattr(fa.psycopg2, 'connect', lambda url: Conn())
    fa.run_fan_automation('postgresql://x', ac=FakeAC())
    return seen[0]


def test_select_reads_the_target_range_and_nothing_of_the_removed_features(monkeypatch):
    sql = _select_sql(monkeypatch)
    assert 'f.target_low' in sql and 'f.target_high' in sql and "'heat_fan_due'" in sql
    for removed in ('cal_active', 'calibration_runs', 'temp_test_at', 'caps_cleared', 'obergrenzen_loeschen'):
        assert removed not in sql


def test_select_reads_the_delete_requests_also_with_the_automatik_off(monkeypatch):
    """Nutzer 08.11.: alle neuen Anfragen „Lernwert löschen“ (nach learned_del_seen) als Liste; die Zeile wird auch bei
    ausgeschalteter Automatik gelesen, solange eine Anfrage offen ist."""
    sql = _select_sql(monkeypatch)
    assert "l.kind = 'lernwert_loeschen'" in sql and "l.source = 'nutzer'" in sql
    assert "(f.state->>'learned_del_seen')::timestamptz" in sql and 'AS learned_del ' in sql
    assert "json_build_object('recorded_at', l.recorded_at, 'details', l.details) ORDER BY l.recorded_at, l.id" in sql
    where = sql.split(' WHERE f.enabled ')[1]
    assert where.count('OR EXISTS (SELECT 1 FROM automation_log l') == 1 and "'lernwert_loeschen'" in where
    assert 'pool_url' not in sql and 'pool_user' not in sql


# ---------------------------------------------------------------- Lernwert löschen (Nutzer 08.11.)

L4 = dict(LW, mhz=280, at='2026-10-24T11:00:00+00:00')
L5 = dict(LW, at='2026-10-24T11:30:00+00:00')


def _req(zt, level, at, when):
    """A request row as the SELECT delivers it (learned_del)."""
    return {'recorded_at': when.isoformat(), 'details': {'zieltemperatur': zt, 'stufe': level, 'at': at}}


def test_delete_a_learned_value_of_the_current_range_learning_goes_on(miner):
    ac = FakeAC(level=5)
    r = started(miner, ac, level=5, learned={'4': L4, '5': L5})
    r.go(60, temp=82)
    since, before = r.st['learn_since'], r.st
    when = r.now - timedelta(seconds=2)
    r.go(5, temp=82, learned_del=[_req('80-85', 5, L5['at'], when)])   # Stufe 5 lernt gerade
    r.kw['learned_del'] = None
    assert r.st['learned'] == {'4': L4} and before['learned'] == {'4': L4, '5': L5}   # neue dicts, nie in place
    assert r.st['learned_del_seen'] == when.isoformat()          # Zeit der Anfrage, nicht jetzt
    assert any(n[1] == 'Lernwert Stufe 5 (80–85 °C) gelöscht (Nutzer).' for n in r.st['log'])
    assert r.st['learn_since'] == since                           # die Lernperiode läuft unverändert weiter (ai ja)
    assert not r.events('lernwert_loeschen') and not r.st.get('last_error')
    r.go(15 * 60, temp=82)
    assert r.st['learned']['5']['count'] == 1 and r.st['learned']['4'] == L4


def test_delete_learned_values_of_other_ranges_two_requests_in_one_round(miner):
    ac = FakeAC(level=5)
    r = started(miner, ac, level=5, learned={'5': L5})
    archive = {'82-86': {'5': L5}, '77-81': {'4': L4, '5': L5}}
    r.st = dict(r.st, learned_zb=archive)
    first, second = r.now - timedelta(seconds=3), r.now - timedelta(seconds=1)
    r.go(5, temp=82, learned_del=[_req('82-86', 5, L5['at'], first), _req('77-81', 4, L4['at'], second)])
    r.kw['learned_del'] = None
    assert r.st['learned_zb'] == {'77-81': {'5': L5}}            # leer gewordener Eintrag 82-86 entfällt
    assert archive == {'82-86': {'5': L5}, '77-81': {'4': L4, '5': L5}}
    assert r.st['learned'] == {'5': L5} and r.st['learned_del_seen'] == second.isoformat()
    log = [n[1] for n in r.st['log']]
    assert 'Lernwert Stufe 5 (82–86 °C) gelöscht (Nutzer).' in log and 'Lernwert Stufe 4 (77–81 °C) gelöscht (Nutzer).' in log


def test_delete_request_for_a_value_learned_again_or_gone_keeps_everything(miner):
    ac = FakeAC(level=5)
    r = started(miner, ac, level=5, learned={'4': L4, '5': L5})
    r.st = dict(r.st, learned_zb={'82-86': {'5': L5}})
    when = r.now - timedelta(seconds=1)
    reqs = [_req('80-85', 4, 'älter', when), _req('80-85', 6, L5['at'], when), _req('82-86', 5, None, when),
            _req('70-74', 5, L5['at'], when), {'recorded_at': when.isoformat(), 'details': {'zieltemperatur': ['x']}}]
    r.go(5, temp=82, learned_del=reqs)
    r.kw['learned_del'] = None
    assert r.st['learned'] == {'4': L4, '5': L5} and r.st['learned_zb'] == {'82-86': {'5': L5}}
    assert not r.st.get('last_error') and r.st['learned_del_seen'] == when.isoformat()
    kept = [n[1] for n in r.st['log'] if 'nicht gelöscht: nicht mehr vorhanden oder neu gelernt.' in n[1]]
    assert len(kept) == 5 and kept[0] == 'Lernwert Stufe 4 (80–85 °C) nicht gelöscht: nicht mehr vorhanden oder neu gelernt.'


def test_delete_request_with_details_not_an_object_does_not_fail_the_round(miner):
    """details is jsonb: a list, string or number must not fail the round (else learned_del_seen never moves on and
    every later round fails the same way)."""
    ac = FakeAC(level=5)
    r = started(miner, ac, level=5, learned={'4': L4, '5': L5})
    when = r.now - timedelta(seconds=1)
    reqs = [{'recorded_at': when.isoformat(), 'details': d} for d in (['x'], 'x', 5)]
    r.go(5, temp=82, learned_del=reqs)
    r.kw['learned_del'] = None
    assert r.st['learned'] == {'4': L4, '5': L5} and not r.st.get('last_error')
    assert r.st['learned_del_seen'] == when.isoformat()
    kept = [n[1] for n in r.st['log'] if 'nicht gelöscht: nicht mehr vorhanden oder neu gelernt.' in n[1]]
    assert len(kept) == 3


def test_delete_marker_survives_automatik_ein(miner):
    ac = FakeAC(level=5)
    r = started(miner, ac, level=5, learned={'4': L4, '5': L5})
    r.go(10, enabled=False)
    when = r.now - timedelta(seconds=1)
    r.go(5, temp=82, enabled=True, learned_del=[_req('80-85', 4, L4['at'], when)])   # löschen und Automatik ein
    r.kw['learned_del'] = None
    assert r.events('start')[-1]['trigger'] == 'Automatik ein'
    assert r.st['learned'] == {'5': L5} and r.st['learned_del_seen'] == when.isoformat()
    r.go(10, temp=82, enabled=False)
    r.go(10, temp=82, enabled=True)
    assert r.st['learned_del_seen'] == when.isoformat() and r.st['learned'] == {'5': L5}


def test_delete_while_the_automatik_is_off_only_deletes(miner):
    """Zeile nur wegen der Anfrage gelesen (Aus-Runde lief schon): nur löschen und speichern – kein AC-Aufruf, kein
    Minerbefehl, kein Lesen des Miners, nur die Aus-Runde im Protokoll wie bei v2.2.6."""
    ac = FakeAC(level=5)
    r = started(miner, ac, level=5, learned={'4': L4, '5': L5})
    r.st = dict(r.st, learned_zb={'82-86': {'5': L5}})
    r.go(10, enabled=False)
    assert r.st['off'] and r.st['phase'] == 'aus'

    def no_call(*args, **kw):
        raise AssertionError('kein AC-Aufruf bei ausgeschalteter Automatik')
    ac.speed_of = ac.set_fan_level = ac.set_fan_power = no_call
    elapsed, sets, modes = miner.elapsed, len(miner.clock_sets), len(miner.mode_sets)
    events, cycles = len(r.j.events), len(r.j.cycles)
    when = r.now - timedelta(seconds=1)
    r.go(5, learned_del=[_req('80-85', 4, L4['at'], when), _req('82-86', 5, L5['at'], when)])
    r.kw['learned_del'] = None
    assert r.st['learned'] == {'5': L5} and r.st['learned_zb'] == {} and r.st['learned_del_seen'] == when.isoformat()
    assert miner.elapsed == elapsed and len(miner.clock_sets) == sets and len(miner.mode_sets) == modes
    assert len(r.j.events) == events and len(r.j.cycles) == cycles + 1 and r.j.cycles[-1]['decision'] == 'Automatik aus'
    assert r.st['off'] and r.st['phase'] == 'aus' and not r.st.get('last_error')


# ---------------------------------------------------------------- Zustand vor v2.2.7 (Nutzer 06.11.)

OLD_STATE = {'v': fa.STATE_VERSION, 'seen_at': T0.isoformat(), 'level_seen': 'x', 'zb': [80, 85],
             'learned': {'5': LW}, 'learned_zb': {'82-85': {'5': B}}, 'auto_restart_at': T0.isoformat(),
             'clock_set': 300, 'fan_target': 6, 'fan_set': 6, 'fan_read': 6, 'elapsed': 900, 'phase': 'korrektur',
             'start_at': T0.isoformat(), 'heat': False, 'caps': {'5': 290}, 'caps_cleared_seen': T0.isoformat(),
             'ertrag': None, 'ths': [[T0.isoformat(), 150.0]], 'corr': True, 'corr_dwell_s': 100, 'corr_wait': None,
             'hot_wait': None, 'exc_active': False, 'calm': None, 'calm_for': None, 'window_until': None,
             'window_active': False, 'below_since': None, 'last_plus_at': None, 'above': True, 'above_at': None,
             'band_in': None, 'band_dwell_s': 100, 'band_since': None, 'cold_since': None, 'down_since': None,
             'cal': False, 'test_clear': True, 'dh_cap_since': None, 'dh_cap_clock': None, 'dh_block': None,
             'stoss': None, 'stoss_unten_at': None, 'stoss_unten_frei': True, 'resent_at': None}


@pytest.mark.parametrize('temp, fan, phase', [(83, 5, 'basismodus'), (88, 6, 'kuehlmodus')])
def test_old_state_keys_go_once_learned_values_stay(miner, temp, fan, phase):
    """Korrekturphase (Lüfter +1) beim Update: Zustand der alten Regeln entfernt, Lüfter zurück auf die Basisstufe –
    bei 88 °C gleich der Kühlmodus; Lernwerte und STATE_VERSION bleiben, kein Start."""
    ac = FakeAC(level=6)
    miner.points = fa.points_for(STOCK['2/L4'], 300)
    r = Run(miner, ac, 5, state=dict(OLD_STATE))
    r.go(5, temp=temp)
    assert not [k for k in fa.OBSOLETE_KEYS if k in r.st] and r.st['heat'] is None
    assert r.st['v'] == fa.STATE_VERSION and r.st['learned'] == {'5': LW} and r.st['learned_zb'] == {'82-85': {'5': B}}
    assert r.st['auto_restart_at'] == T0.isoformat() and r.st['zb'] == [80, 85]
    assert r.st['fan_target'] == fan and ac.level == fan and r.st['phase'] == phase
    assert not r.events('start') and not r.st.get('last_error')
    assert any(n[1] == 'Neue Regeln (Heiz-, Basis-, Kühlmodus): Lüfter Basisstufe 5.' for n in r.st['log'])
    r.go(15, temp=temp)
    assert len([n for n in r.st['log'] if n[1].startswith('Neue Regeln')]) == 1


def test_old_heat_mode_of_the_start_ends(miner):
    ac = FakeAC(level=1)
    miner.points = fa.points_for(STOCK['2/L4'], 300)
    r = Run(miner, ac, 5, state=dict(OLD_STATE, heat=True, corr=False, fan_target=1, fan_set=1, fan_read=1))
    r.go(5, temp=78)
    assert r.st['heat'] is None and ac.level == 5 and r.st['phase'] == 'basismodus'


@pytest.mark.parametrize('old, fan, writes', [
    ({'heat': True}, 1, [5]),                                     # Heizmodus of the start (Stufe 1)
    ({'stoss': {'n': 1, 'step': 'an', 'at': T0.isoformat(), 'level': 4, 'dir': 'down'}}, 4, [5]),   # Lüfterstöße unten
    ({'corr': True}, 6, []),                                      # Korrekturphase (+1): stays while the miner is silent
])
def test_old_fan_below_the_base_level_goes_back_while_the_miner_does_not_answer(miner, old, fan, writes):
    """Update while the miner is silent (Nutzer 26.10.: ohne Regelung kein Lüfter unter der Basisstufe): the fan goes to
    the Basisstufe at once; a raised one stays until the miner answers. The old phase goes."""
    ac = FakeAC(level=fan)
    miner.offline = True
    state = dict(OLD_STATE, corr=False, fan_target=fan, fan_set=fan, fan_read=fan)
    state.update(old)
    r = Run(miner, ac, 5, state=state)
    r.go(60)
    assert ac.writes == writes and r.st['fan_target'] == 5 and not r.st.get('heat_fan_due')
    assert r.st['heat'] is None and r.st['phase'] == 'basismodus'


def test_learned_values_are_not_limited_any_more(miner):
    ac = FakeAC(level=4)
    r = started(miner, ac, level=4, learned={'4': {'mhz': 280, 'temp_avg': 82, 'at': 'x', 'count': 1}})
    r.st['caps'] = {'4': 270}                                     # Obergrenze vor v2.2.7: entfällt
    r.go(15 * 60 + 30, temp=82)
    assert r.st['learned']['4']['mhz'] == 280 and 'caps' not in r.st


# ---------------------------------------------------------------- Protokoll

def test_every_round_is_recorded(miner):
    ac = FakeAC(level=5)
    r = started(miner, ac, level=5)
    r.go(60, temp=82)
    assert len(r.j.cycles) == 14
    c = r.j.cycles[-1]
    assert c['chip_temp'] == 82 and c['clock_set'] == 400 and c['base_level'] == 5 and c['mode_key'] == '2/L2'
    assert c['phase'] == 'basismodus' and 'window_active' not in c


def test_phases_in_the_protocol(miner):
    ac = FakeAC(level=3)
    r = warm(miner, ac, level=3)
    r.go(5, temp=88)
    assert r.j.cycles[-1]['phase'] == 'kuehlmodus' and r.events('kuehlmodus')[-1]['phase'] == 'kuehlmodus'
    r.go(5, temp=81)
    assert r.j.cycles[-1]['phase'] == 'basismodus'
    r.go(65, temp=65)
    assert r.j.cycles[-1]['phase'] == 'heizmodus' and r.events('luefter')[-1]['phase'] == 'heizmodus'
    assert {c['phase'] for c in r.j.cycles} == {'basismodus', 'kuehlmodus', 'heizmodus'}


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
    """Annahmen: Gleichgewicht 60 °C + 2 °C je 10 MHz über 300 − 10 °C je Lüfterstufe über 5 + Raum (so liegt der
    Initialwert der Stufe 5, 400 MHz, bei 80 °C), Zeitkonstante 120 s, Messwert ganzzahlig."""
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


def test_simulation_reaches_the_zieltemperatur_and_stays_safe(miner):
    r, peak, mn = _sim(5)
    assert peak < 88
    band = [c for c in r.j.cycles[-360:] if 80 <= (c['chip_temp'] or 0) <= 85]
    assert len(band) > 300                                       # letzte 30 min fast nur in der Zieltemperatur
    assert r.st['learned'].get('5')


def test_simulation_room_plus_15_corrects(miner):
    r, peak, mn = _sim(5, room=lambda run: 15 if run.now >= T0 + timedelta(hours=1) else 0)
    band = [c for c in r.j.cycles[-360:] if 80 <= (c['chip_temp'] or 0) <= 85]
    assert peak < 90 and len(band) > 300 and r.st['fan_target'] in (5, 6)   # Kühlmodus +1 hält die Temperatur
