"""Temporary stop closures combined with the immutable timetable archive.

Closures live in their own CSV so the timetable archive is never rewritten.
Each notification keeps its own identity and its inclusive ``from``/``to``
service-date range; ranges are compared as strings and never expanded into
per-day rows.  A board answer keeps every planned stop visit: a closure only
marks the visits at the closed stop as unserviceable, it never cancels the
same trip's visits at other stops.
"""
import csv
from datetime import date
import os
from pathlib import Path
import sqlite3

from .common import text
from .feed import hhmmss, seconds

CLOSURE_COLUMNS = ['closure_id', 'stop_id', 'from', 'to', 'reason']
MAX_CLOSURE_BYTES = 8_000_000


def _canonical_date(value):
    if not isinstance(value, str):
        raise ValueError('canonical YYYY-MM-DD date required')
    try:
        parsed = date.fromisoformat(value)
    except (TypeError, ValueError):
        raise ValueError('canonical YYYY-MM-DD date required') from None
    if parsed.isoformat() != value:
        raise ValueError('canonical YYYY-MM-DD date required')
    return value


def _path(value, label):
    try:
        raw = os.fspath(value)
    except TypeError:
        raise ValueError(label + ' must be a filesystem path') from None
    if not isinstance(raw, str) or not raw:
        raise ValueError(label + ' must be a non-empty filesystem path')
    return Path(raw)


def _parse_closures(path, stop_ids):
    """Parse and fully validate a closure CSV without touching any archive."""
    source = _path(path, 'closure CSV')
    if not source.is_file():
        raise ValueError('closure CSV does not exist: ' + str(source))
    if source.stat().st_size > MAX_CLOSURE_BYTES:
        raise ValueError('closure CSV exceeds 8MB')

    closures = []
    seen = set()
    try:
        with source.open('r', encoding='utf-8-sig', newline='') as stream:
            reader = csv.reader(stream, strict=True)
            header = next(reader, None)
            if header != CLOSURE_COLUMNS:
                raise ValueError('unexpected closure CSV columns')
            for raw in reader:
                if len(raw) != len(CLOSURE_COLUMNS):
                    raise ValueError('malformed closure CSV row')
                closure_id = text(raw[0])
                stop_id = text(raw[1])
                start = _canonical_date(raw[2])
                end = _canonical_date(raw[3])
                reason = raw[4]
                if start > end:
                    raise ValueError('closure from must not be after to')
                if not reason.strip() or any(ord(character) < 32 for character in reason):
                    raise ValueError('closure reason is required')
                if closure_id in seen:
                    raise ValueError('duplicate closure_id: ' + closure_id)
                seen.add(closure_id)
                if stop_id not in stop_ids:
                    raise ValueError('unknown stop in closure CSV: ' + stop_id)
                closures.append({'closure_id': closure_id, 'stop_id': stop_id,
                                 'from': start, 'to': end, 'reason': reason})
    except UnicodeDecodeError:
        raise ValueError('closure CSV must be UTF-8') from None
    except csv.Error:
        raise ValueError('malformed closure CSV') from None
    except OSError as error:
        raise ValueError('cannot read closure CSV: ' + str(error)) from None
    return closures


def _open(database):
    path = _path(database, 'database')
    if not path.is_file():
        raise ValueError('database does not exist: ' + str(database))
    try:
        conn = sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True,
                               isolation_level=None)
        conn.execute('PRAGMA foreign_keys=ON')
    except sqlite3.Error as error:
        raise ValueError('invalid archive: ' + str(error)) from None
    return conn


def _active_services(conn, service_date, weekday):
    active = {row[0] for row in conn.execute(
        'SELECT service FROM calendar WHERE start<=? AND end>=? '
        'AND substr(weekdays,?,1)=?',
        (service_date, service_date, weekday, '1'))}
    for service, kind in conn.execute(
            'SELECT service,kind FROM exceptions WHERE day=?', (service_date,)):
        if kind == 1:
            active.add(service)
        elif kind == 2:
            active.discard(service)
        else:
            raise ValueError('invalid archive: bad exception kind')
    return active


def stop_board(database, closures_csv, service_date, stop, after='00:00:00'):
    """Return planned departures at ``stop`` with closure serviceability.

    Planned visits follow the archive's calendar and exception rules and keep
    one row per ``(trip_id, stop_sequence)``.  Every closure notification that
    covers the stop and service day is reported by identity, even when the day
    has no planned departures at all.
    """
    service_date = _canonical_date(service_date)
    if not isinstance(after, str):
        raise ValueError('time must be a string 00:00:00..47:59:59')
    cutoff = seconds(after)
    stop = text(stop)
    weekday = date.fromisoformat(service_date).weekday() + 1

    conn = _open(database)
    try:
        if conn.execute('SELECT 1 FROM stops WHERE id=?', (stop,)).fetchone() is None:
            raise ValueError('unknown stop')
        stop_ids = {row[0] for row in conn.execute('SELECT id FROM stops')}
        closures = _parse_closures(closures_csv, stop_ids)
        hits = sorted(
            (row for row in closures
             if row['stop_id'] == stop
             and row['from'] <= service_date <= row['to']),
            key=lambda row: row['closure_id'])
        closure_ids = [row['closure_id'] for row in hits]
        closed = bool(hits)

        active = _active_services(conn, service_date, weekday)
        found = []
        for trip, service, sequence, departure in conn.execute(
                'SELECT t.trip,p.service,t.seq,t.departure '
                'FROM times t JOIN trips p ON p.id=t.trip '
                'WHERE t.stop=? AND t.departure>=?',
                (stop, cutoff)):
            if service in active:
                found.append((departure, trip, sequence))
        found.sort()

        departures = [{'trip_id': trip, 'stop_sequence': sequence,
                       'departure': hhmmss(departure),
                       'service_date': service_date,
                       'serviceable': not closed,
                       'closure_ids': list(closure_ids)}
                      for departure, trip, sequence in found]
        return {'service_date': service_date, 'stop_id': stop,
                'closed': closed, 'closure_ids': closure_ids,
                'closures': [{'closure_id': row['closure_id'],
                              'reason': row['reason']} for row in hits],
                'departures': departures}
    except sqlite3.Error as error:
        raise ValueError('invalid archive: ' + str(error)) from None
    finally:
        conn.close()
