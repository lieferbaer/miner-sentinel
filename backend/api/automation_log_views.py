"""Protokoll der Temperatur-Automatik (Tabellen automation_log und automation_cycle).

Written by the data-service (data-service/automation_journal.py, source 'regelung') and by fan_views.py
(user events, source 'nutzer'); kept 360 days. Only the database is read here.
  GET automation/log/            entries, newest first, 100 per page (filters kind, ok, from, to)
  GET automation/log/<id>/       one entry in full (response, thermal, details)
  GET automation/log/stats/      per mode switch, clock value and fan level: count, API time, takeover time
  GET automation/cycles/         regulation rounds (over 6 h: one point per minute) and the events
  GET automation/export.csv      CSV (table=log|cycle; semicolon, UTF-8 with BOM)
"""
import csv
import json
import statistics
from datetime import datetime, timedelta, timezone as dt_timezone

from django.db.models.functions import Substr
from django.http import StreamingHttpResponse
from django.utils import timezone
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from .models import AutomationCycle, AutomationLog
from .time_window import _parse_iso_datetime, parse_int_param

KEEP_DAYS = 360               # as automation_journal.KEEP_DAYS: nothing older is kept
PAGE_SIZE = 100
RESPONSE_SHORT = 200          # list: the reply shortened to this many characters
CYCLES_DEFAULT_H = 2          # cycles without 'from': the last 2 h
CYCLES_RAW_H = 6              # longer: one point per minute
CYCLE_S = 5                   # one regulation round every 5 s (raw data)
BUCKET_S = 60
EVENTS_MAX = 500
EVENTS_HIDDEN = ('lernen_start', 'stabilitaet_start', 'stabilitaet_ende')
EXPORT_MAX_DAYS = 400
THERMAL_AT = ((1, 'thermisch_1min'), (5, 'thermisch_3min'), (9, 'thermisch_5min'))   # thermal: every 30 s

LOG_FIELDS = [f.name for f in AutomationLog._meta.concrete_fields]
LIST_FIELDS = [f for f in LOG_FIELDS if f != 'response']


class BadParam(ValueError):
    pass


def _when(params, key):
    raw = params.get(key)
    if raw in (None, ''):
        return None
    # an unencoded '+' of the UTC offset arrives as a space
    try:
        dt = _parse_iso_datetime(raw) or _parse_iso_datetime(str(raw).strip().replace(' ', '+'))
    except ValueError:           # well formed but no valid date (month 13, 30 February)
        dt = None
    if dt is None:
        raise BadParam('Ungültige Zeitangabe (%s).' % key)
    return dt


def _period(params, default_hours=None):
    """(from, to): 'to' defaults to now, 'from' to `default_hours` before 'to' (else no limit);
    never earlier than KEEP_DAYS back."""
    now = timezone.now()
    end = _when(params, 'to') or now
    start = _when(params, 'from')
    if start is None and default_hours:
        start = end - timedelta(hours=default_hours)
    oldest = now - timedelta(days=KEEP_DAYS)
    if start is None or start < oldest:
        start = oldest
    if end < start:
        raise BadParam('Ungültiger Zeitraum.')
    return start, end


def _bad(exc):
    return Response({'error': str(exc)}, status=status.HTTP_400_BAD_REQUEST)


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def log_list(request):
    """?kind=a,b&ok=true|false|null&from=ISO&to=ISO&page=1 -> {count, page, pages, results}."""
    params = request.query_params
    try:
        start, end = _period(params)
    except BadParam as exc:
        return _bad(exc)
    qs = AutomationLog.objects.filter(recorded_at__gte=start, recorded_at__lte=end)
    kinds = [k.strip() for k in (params.get('kind') or '').split(',') if k.strip()]
    if kinds:
        qs = qs.filter(kind__in=kinds)
    ok = (params.get('ok') or '').lower()
    if ok == 'true':
        qs = qs.filter(ok=True)
    elif ok == 'false':
        qs = qs.filter(ok=False)
    elif ok == 'null':
        qs = qs.filter(ok__isnull=True)          # events only (no command)
    elif ok:
        return _bad('ok: true, false oder null.')
    count = qs.count()
    pages = max(1, -(-count // PAGE_SIZE))
    page = parse_int_param(params.get('page'), 1, min_value=1, max_value=pages)
    first = (page - 1) * PAGE_SIZE
    results = list(qs.order_by('-recorded_at', '-id')
                   .annotate(response_short=Substr('response', 1, RESPONSE_SHORT))
                   .values(*LIST_FIELDS, 'response_short')[first:first + PAGE_SIZE])
    return Response({'count': count, 'page': page, 'pages': pages, 'results': results})


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def log_detail(request, pk):
    entry = AutomationLog.objects.filter(pk=pk).values(*LOG_FIELDS).first()
    if not entry:
        return Response({'error': 'Eintrag nicht gefunden.'}, status=status.HTTP_404_NOT_FOUND)
    return Response(entry)


def _mean_median(values):
    vals = [v for v in values if v is not None]
    if not vals:
        return None, None
    return round(statistics.fmean(vals), 1), round(float(statistics.median(vals)), 1)


def _times(rows):
    """Count, API time and takeover time (only measured takeovers) of a group of commands."""
    api = _mean_median(r['api_ms'] for r in rows)
    took = [r['takeover_s'] for r in rows if r['takeover_state'] == 'gemessen' and r['takeover_s'] is not None]
    over = _mean_median(took)
    return {'anzahl': len(rows), 'api_ms_mittel': api[0], 'api_ms_median': api[1],
            'uebernahme_s_mittel': over[0], 'uebernahme_s_median': over[1]}, took


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def log_stats(request):
    """Successful commands (ok=true) per mode switch, set clock and fan level."""
    try:
        start, end = _period(request.query_params)
    except BadParam as exc:
        return _bad(exc)
    rows = (AutomationLog.objects.filter(recorded_at__gte=start, recorded_at__lte=end, ok=True,
                                         kind__in=('modus', 'takt', 'luefter'))
            .order_by().values('kind', 'mode_old', 'mode_new', 'clock_new', 'fan_new', 'api_ms', 'takeover_s',
                               'takeover_state', 'temp_before', 'thermal'))
    modes, clocks, fans = {}, {}, {}
    for r in rows:
        if r['kind'] == 'modus':
            modes.setdefault((r['mode_old'], r['mode_new']), []).append(r)
        elif r['kind'] == 'takt' and r['clock_new'] is not None:
            clocks.setdefault(int(round(r['clock_new'])), []).append(r)
        elif r['kind'] == 'luefter' and r['fan_new'] is not None:
            fans.setdefault(r['fan_new'], []).append(r)

    out_modes = []
    for (old, new), group in sorted(modes.items()):
        times, took = _times(group)
        out_modes.append(dict({'von': old, 'nach': new}, **times, uebernahme_min=min(took) if took else None,
                              uebernahme_max=max(took) if took else None))
    out_clocks = []
    for clock, group in sorted(clocks.items()):
        entry = dict({'takt': clock}, **_times(group)[0])
        for i, name in THERMAL_AT:
            diffs = [r['thermal'][i] - r['temp_before'] for r in group
                     if r['temp_before'] is not None and isinstance(r['thermal'], list) and len(r['thermal']) > i
                     and r['thermal'][i] is not None]
            entry[name] = round(statistics.fmean(diffs), 1) if diffs else None
        out_clocks.append(entry)
    out_fans = [dict({'stufe': level}, **_times(group)[0]) for level, group in sorted(fans.items())]
    return Response({'modes': out_modes, 'clocks': out_clocks, 'fans': out_fans})


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def cycles(request):
    """?from&to (default: the last 2 h) -> {bucket_s, points, events}. Up to 6 h the raw rounds, longer one point
    per minute (mean of the temperatures, clocks and hashrate, last value of phase and levels)."""
    try:
        start, end = _period(request.query_params, default_hours=CYCLES_DEFAULT_H)
    except BadParam as exc:
        return _bad(exc)
    rows = (AutomationCycle.objects.filter(recorded_at__gte=start, recorded_at__lte=end).order_by('recorded_at', 'id')
            .values_list('recorded_at', 'chip_temp', 'clock_set', 'clock_reported', 'ghsspd_ths', 'fan_level', 'base_level',
                         'phase', 'decision').iterator(chunk_size=2000))
    raw = end - start <= timedelta(hours=CYCLES_RAW_H)
    points = []
    if raw:
        for at, temp, clock, reported, ths, fan, base, phase, decision in rows:
            point = {'t': at, 'chip_temp': temp, 'clock_set': clock, 'clock_reported': reported, 'ghsspd_ths': ths,
                     'fan_level': fan, 'base_level': base, 'phase': phase}
            if decision:
                point['decision'] = decision
            points.append(point)
    else:
        bucket = None
        for row in rows:
            key = int(row[0].timestamp()) // BUCKET_S
            if bucket is None or bucket['key'] != key:
                if bucket:
                    points.append(_bucket_point(bucket))
                bucket = {'key': key, 'sums': {}, 'last': {}}
            for name, value in zip(('chip_temp', 'clock_set', 'clock_reported', 'ghsspd_ths'), row[1:5]):
                if value is not None:
                    total = bucket['sums'].setdefault(name, [0.0, 0])
                    total[0] += value
                    total[1] += 1
            for name, value in zip(('fan_level', 'base_level', 'phase', 'decision'), row[5:9]):
                if value not in (None, ''):
                    bucket['last'][name] = value
        if bucket:
            points.append(_bucket_point(bucket))
    events = list(AutomationLog.objects.filter(recorded_at__gte=start, recorded_at__lte=end)
                  .exclude(kind__in=EVENTS_HIDDEN).order_by('-recorded_at', '-id')
                  .values_list('recorded_at', 'kind', 'id', 'trigger')[:EVENTS_MAX])
    return Response({'bucket_s': CYCLE_S if raw else BUCKET_S, 'points': points,
                     'events': [{'t': at, 'kind': kind, 'id': pk, 'trigger': trigger}
                                for at, kind, pk, trigger in reversed(events)]})


def _bucket_point(bucket):
    at = datetime.fromtimestamp(bucket['key'] * BUCKET_S, tz=dt_timezone.utc)
    point = {'t': at}
    for name in ('chip_temp', 'clock_set', 'clock_reported', 'ghsspd_ths'):
        total = bucket['sums'].get(name)
        point[name] = round(total[0] / total[1], 1) if total else None
    for name in ('fan_level', 'base_level', 'phase'):
        point[name] = bucket['last'].get(name)
    if bucket['last'].get('decision'):
        point['decision'] = bucket['last']['decision']
    return point


class _Echo:
    """csv.writer target that hands every line back (streamed response)."""

    def write(self, value):
        return value


def _cell(value):
    if value is None:
        return ''
    if isinstance(value, bool):
        return 'true' if value else 'false'
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    return value


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def export_csv(request):
    """?table=log|cycle&from&to -> all columns as CSV, at most 400 days."""
    params = request.query_params
    table = params.get('table') or 'log'
    if table not in ('log', 'cycle'):
        return _bad('table: log oder cycle.')
    try:
        start, end = _period(params)
    except BadParam as exc:
        return _bad(exc)
    if end - start > timedelta(days=EXPORT_MAX_DAYS):
        return _bad('Höchstens %d Tage.' % EXPORT_MAX_DAYS)
    model = AutomationLog if table == 'log' else AutomationCycle
    fields = model._meta.concrete_fields
    rows = (model.objects.filter(recorded_at__gte=start, recorded_at__lte=end).order_by('recorded_at', 'id')
            .values_list(*[f.attname for f in fields]).iterator(chunk_size=2000))
    writer = csv.writer(_Echo(), delimiter=';')

    def lines():
        yield '﻿' + writer.writerow([f.column for f in fields])
        for row in rows:
            yield writer.writerow([_cell(v) for v in row])

    name = 'automation_%s_%s-%s.csv' % (table, timezone.localtime(start).strftime('%Y%m%d'),
                                        timezone.localtime(end).strftime('%Y%m%d'))
    resp = StreamingHttpResponse(lines(), content_type='text/csv; charset=utf-8')
    resp['Content-Disposition'] = 'attachment; filename="%s"' % name
    return resp
