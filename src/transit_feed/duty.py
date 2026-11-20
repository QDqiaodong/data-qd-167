"""Vehicle duty feasibility for an imported timetable archive.

A duty plan and its directed terminal-to-terminal deadhead links live in two
independent CSVs, so the immutable import archive is never rewritten.  Each
duty row assigns a ``trip_id`` instance to a ``vehicle_id`` over an inclusive
service-date range, but only on days when that trip's service actually runs.

Instances are ordered across service days by their absolute clock
(service-day ordinal * 86400 + seconds), so a ``24:xx`` time stays attached to
the service day that owns it and is never relabelled as the next civil day.
Because a timetable clock reaches 47:59:59, instances of neighbouring service
days can interleave, so predecessor/successor relations are resolved on
absolute clocks rather than calendar-day boundaries.

The queried date range only decides which problems are *reported*: the whole
valid plan is read and every vehicle chain is built from all of its instances,
keeping one predecessor and one successor on each side of the reported window.
An instance claimed by two or more duty rows is ambiguous; it is reported once
and removed from every chain rather than assigned to an arbitrary vehicle.
"""
import bisect
import csv
from datetime import date
import os
from pathlib import Path
import re
import sqlite3

from .feed import hhmmss
from .revision import _connect, _validate_baseline

DUTY_COLUMNS = ['vehicle_id', 'trip_id', 'start_date', 'end_date']
LINK_COLUMNS = ['from_stop', 'to_stop', 'seconds']
MAX_BYTES = 8_000_000
MAX_DATA_ROWS = 20_000
MAX_LINK_SECONDS = 86_400
DAY = 86_400


def _path(value, label):
    try:
        raw = os.fspath(value)
    except TypeError:
        raise ValueError(label + ' must be a filesystem path') from None
    if not isinstance(raw, str) or not raw:
        raise ValueError(label + ' must be a non-empty filesystem path')
    return Path(raw)


def _canonical_date(value):
    if not isinstance(value, str):
        raise ValueError('canonical YYYY-MM-DD date required')
    try:
        parsed = date.fromisoformat(value)
    except (TypeError, ValueError):
        raise ValueError('canonical YYYY-MM-DD date required') from None
    if parsed.isoformat() != value:
        raise ValueError('canonical YYYY-MM-DD date required')
    return parsed


def _vehicle(value, number):
    if (not isinstance(value, str) or not value
            or value != value.strip()
            or any(ord(character) < 32 or ord(character) == 127
                   for character in value)):
        raise ValueError('invalid vehicle_id in duties CSV row %d' % number)
    return value


def _link_seconds(value, number):
    if not re.fullmatch(r'[0-9]+', value):
        raise ValueError(
            'seconds must be a decimal non-negative integer in links CSV row %d'
            % number)
    normalised = value.lstrip('0') or '0'
    if len(normalised) > 5 or int(normalised) > MAX_LINK_SECONDS:
        raise ValueError(
            'seconds must not exceed 86400 in links CSV row %d' % number)
    return int(normalised)


def _csv_rows(value, label, columns):
    """Read every row of a strict, bounded UTF-8 CSV data file."""
    source = _path(value, label)
    if not source.is_file():
        raise ValueError(label + ' does not exist: ' + str(source))
    try:
        if source.stat().st_size > MAX_BYTES:
            raise ValueError(label + ' CSV exceeds 8MB')
        with source.open('r', encoding='utf-8-sig', newline='') as stream:
            reader = csv.reader(stream, strict=True)
            header = next(reader, None)
            if header != columns:
                raise ValueError('unexpected ' + label + ' CSV columns')
            rows = []
            for number, raw in enumerate(reader, start=2):
                if len(raw) != len(columns):
                    raise ValueError(
                        'malformed ' + label + ' CSV row %d' % number)
                if len(rows) == MAX_DATA_ROWS:
                    raise ValueError(label + ' CSV exceeds 20000 data rows')
                rows.append((number, raw))
            return rows
    except UnicodeDecodeError:
        raise ValueError(label + ' CSV must be UTF-8') from None
    except csv.Error:
        raise ValueError('malformed ' + label + ' CSV') from None
    except OSError as error:
        raise ValueError('cannot read ' + label + ' CSV: ' +
                         str(error)) from None


def _parse_duties(value, trip_ids):
    rules = []
    for number, raw in _csv_rows(value, 'duties', DUTY_COLUMNS):
        vehicle = _vehicle(raw[0], number)
        trip = raw[1]
        if trip not in trip_ids:
            raise ValueError('unknown trip_id in duties CSV row %d' % number)
        start = _canonical_date(raw[2])
        end = _canonical_date(raw[3])
        if start > end:
            raise ValueError(
                'duties start_date must not be after end_date in row %d'
                % number)
        rules.append({'vehicle_id': vehicle, 'trip_id': trip,
                      'start_ord': start.toordinal(),
                      'end_ord': end.toordinal(), 'row': number})
    return rules


def _parse_links(value, stop_ids):
    links = {}
    for number, raw in _csv_rows(value, 'links', LINK_COLUMNS):
        origin, destination = raw[0], raw[1]
        if origin not in stop_ids:
            raise ValueError('unknown from_stop in links CSV row %d' % number)
        if destination not in stop_ids:
            raise ValueError('unknown to_stop in links CSV row %d' % number)
        seconds = _link_seconds(raw[2], number)
        if (origin, destination) in links:
            raise ValueError(
                'duplicate directed link in links CSV row %d' % number)
        links[(origin, destination)] = seconds
    return links


def _load_archive(conn):
    stop_ids = {row[0] for row in conn.execute('SELECT id FROM stops')}
    trips = dict(conn.execute('SELECT id,service FROM trips'))
    services = {}
    for service, start, end, weekdays in conn.execute(
            'SELECT service,start,end,weekdays FROM calendar'):
        entry = services.setdefault(service, {'calendar': None,
                                              'adds': [], 'removes': set()})
        entry['calendar'] = (date.fromisoformat(start).toordinal(),
                             date.fromisoformat(end).toordinal(), weekdays)
    for service, day, kind in conn.execute(
            'SELECT service,day,kind FROM exceptions'):
        entry = services.setdefault(service, {'calendar': None,
                                              'adds': [], 'removes': set()})
        ordinal = date.fromisoformat(day).toordinal()
        if kind == 1:
            entry['adds'].append(ordinal)
        else:
            entry['removes'].add(ordinal)
    for entry in services.values():
        entry['adds'].sort()
    times = {}
    for trip, stop, sequence, arrival, departure in conn.execute(
            'SELECT trip,stop,seq,arrival,departure FROM times'):
        times.setdefault(trip, []).append((sequence, stop, arrival,
                                          departure))
    for rows in times.values():
        rows.sort(key=lambda row: row[0])
    return stop_ids, trips, services, times


def _calendar_before(start, end, weekdays, limit, low):
    """Latest weekday-eligible day in ``[low, min(limit, end)]``."""
    high = min(limit, end)
    floor = max(low, start)
    if high < floor:
        return None
    for offset in range(7):
        candidate = high - offset
        if candidate < floor:
            return None
        if weekdays[date.fromordinal(candidate).weekday()] == '1':
            return candidate
    return None


def _calendar_after(start, end, weekdays, limit, high):
    """Earliest weekday-eligible day in ``[max(limit, start), high]``."""
    ceiling = min(high, end)
    floor = max(limit, start)
    if floor > ceiling:
        return None
    for offset in range(7):
        candidate = floor + offset
        if candidate > ceiling:
            return None
        if weekdays[date.fromordinal(candidate).weekday()] == '1':
            return candidate
    return None


def _previous_active(entry, limit, low):
    """Latest active service day in ``[low, limit]`` or ``None``."""
    if entry is None or limit < low:
        return None
    best = None
    calendar = entry['calendar']
    if calendar is not None:
        ordinal = _calendar_before(*calendar, limit, low)
        while ordinal is not None and ordinal in entry['removes']:
            ordinal = _calendar_before(*calendar, ordinal - 1, low)
        best = ordinal
    adds = entry['adds']
    position = bisect.bisect_right(adds, limit) - 1
    if position >= 0 and adds[position] >= low:
        candidate = adds[position]
        if best is None or candidate > best:
            best = candidate
    return best


def _next_active(entry, limit, high):
    """Earliest active service day in ``[limit, high]`` or ``None``."""
    if entry is None or limit > high:
        return None
    best = None
    calendar = entry['calendar']
    if calendar is not None:
        ordinal = _calendar_after(*calendar, limit, high)
        while ordinal is not None and ordinal in entry['removes']:
            ordinal = _calendar_after(*calendar, ordinal + 1, high)
        best = ordinal
    adds = entry['adds']
    position = bisect.bisect_left(adds, limit)
    if position < len(adds) and adds[position] <= high:
        candidate = adds[position]
        if best is None or candidate < best:
            best = candidate
    return best


def _active_days(entry, low, high):
    """Sorted unique active service days of one service in ``[low, high]``."""
    if entry is None or low > high:
        return []
    days = set()
    calendar = entry['calendar']
    if calendar is not None:
        start, end, weekdays = calendar
        for ordinal in range(max(low, start), min(high, end) + 1):
            if weekdays[date.fromordinal(ordinal).weekday()] == '1':
                days.add(ordinal)
    adds = entry['adds']
    position = bisect.bisect_left(adds, low)
    while position < len(adds) and adds[position] <= high:
        days.add(adds[position])
        position += 1
    for ordinal in entry['removes']:
        if low <= ordinal <= high:
            days.discard(ordinal)
    return sorted(days)


def _coverage_segments(rules):
    """Split a trip's rule ranges into maximal constant-coverage segments.

    Returns ``(low, high, covering_rules)`` tuples in ascending day order.
    Two or more covering rules mark exactly the days whose trip instance is
    ambiguous; a single covering rule owns the day.
    """
    starts = {}
    ends = {}
    for index, rule in enumerate(rules):
        starts.setdefault(rule['start_ord'], []).append(index)
        ends.setdefault(rule['end_ord'] + 1, []).append(index)
    points = sorted(set(starts) | set(ends))
    active = set()
    segments = []
    for position, point in enumerate(points):
        for index in ends.get(point, ()):
            active.discard(index)
        for index in starts.get(point, ()):
            active.add(index)
        if not active or position + 1 == len(points):
            continue
        segments.append((point, points[position + 1] - 1,
                         tuple(rules[index] for index in sorted(active))))
    return segments


def _boundary_before(entry, segments, limit):
    """Latest unambiguous active day at or before ``limit``."""
    best = None
    for segment_low, segment_high, _ in segments:
        ceiling = min(segment_high, limit)
        if ceiling < segment_low:
            continue
        ordinal = _previous_active(entry, ceiling, segment_low)
        if ordinal is not None and (best is None or ordinal > best):
            best = ordinal
    return best


def _boundary_after(entry, segments, limit):
    """Earliest unambiguous active day at or after ``limit``."""
    best = None
    for segment_low, segment_high, _ in segments:
        floor = max(segment_low, limit)
        if floor > segment_high:
            continue
        ordinal = _next_active(entry, floor, segment_high)
        if ordinal is not None and (best is None or ordinal < best):
            best = ordinal
    return best


def _context_days(entry, segments, low, high):
    """Unambiguous active days needed to judge the ``[low, high]`` window.

    A margin of four service days on each side exceeds the largest possible
    clock skew (47:59:59 is under two days), so every instance that can
    interleave with a reported one is present.  One further unambiguous
    instance on each side preserves the true predecessor and successor across
    an arbitrarily long gap in a trip's schedule; pairs made only of such
    out-of-window instances are never reported.
    """
    floor, ceiling = low - 4, high + 4
    found = set()
    for segment_low, segment_high, _ in segments:
        found.update(_active_days(entry, max(segment_low, floor),
                                  min(segment_high, ceiling)))
    day = _boundary_before(entry, segments, floor - 1)
    if day is not None:
        found.add(day)
    day = _boundary_after(entry, segments, ceiling + 1)
    if day is not None:
        found.add(day)
    return sorted(found)


def _key(trip, start_seconds, end_seconds, ordinal):
    return (ordinal * DAY + start_seconds,
            ordinal * DAY + end_seconds, trip,
            date.fromordinal(ordinal).isoformat())


def duty_check(database, duties_file, links_file, from_date, to_date):
    """Check whether a vehicle duty plan is executable.

    ``from_date`` and ``to_date`` are inclusive canonical service dates that
    decide which problems are *reported*; the complete valid plan still builds
    every vehicle chain.  A ``trip_id``/service-date instance claimed by two or
    more duty rows is reported once and left out of every chain.  The returned
    dictionary is JSON serialisable and contains exactly ``feasible``,
    ``duplicate_assignments`` and ``connections``.
    """
    first_day = _canonical_date(from_date)
    last_day = _canonical_date(to_date)
    if first_day > last_day:
        raise ValueError('from date must not be after to date')
    from_ord, to_ord = first_day.toordinal(), last_day.toordinal()

    conn = _connect(_path(database, 'database'), 'ro')
    try:
        _validate_baseline(conn)
        stop_ids, trips, services, times = _load_archive(conn)
    except sqlite3.Error as error:
        raise ValueError('invalid archive: ' + str(error)) from None
    finally:
        conn.close()

    rules = _parse_duties(duties_file, set(trips))
    links = _parse_links(links_file, stop_ids)

    rules_by_trip = {}
    for rule in rules:
        rules_by_trip.setdefault(rule['trip_id'], []).append(rule)

    # Per trip, split its rule coverage into owned (single-rule) day ranges and
    # ambiguous (multi-rule) ranges.  Owned ranges are grouped per vehicle; the
    # same trip may serve different vehicles in disjoint date ranges.
    owned = {}            # (trip, vehicle) -> list of (low, high, rule)
    duplicates = []
    for trip, trip_rules in rules_by_trip.items():
        entry = services.get(trips[trip])
        for low, high, covering in _coverage_segments(trip_rules):
            if len(covering) > 1:
                days = _active_days(entry, max(low, from_ord),
                                    min(high, to_ord))
                vehicle_ids = sorted({rule['vehicle_id']
                                      for rule in covering})
                rule_rows = sorted(rule['row'] for rule in covering)
                for ordinal in days:
                    duplicates.append({
                        'kind': 'duplicate_assignment',
                        'trip_id': trip,
                        'service_date': date.fromordinal(ordinal).isoformat(),
                        'vehicle_ids': list(vehicle_ids),
                        'rule_rows': list(rule_rows),
                    })
                continue
            rule = covering[0]
            owned.setdefault((trip, rule['vehicle_id']), []).append(
                (low, high, rule))
    duplicates.sort(key=lambda item: (item['service_date'], item['trip_id']))

    by_vehicle = {}
    for (trip, vehicle), segments in owned.items():
        by_vehicle.setdefault(vehicle, []).append(
            (trip, services.get(trips[trip]), segments))

    connections = []
    for vehicle, fleet in by_vehicle.items():
        nodes = {}

        def add_node(trip, rows, ordinal):
            # A trip/day pair is one instance, and owned segments are disjoint,
            # so this key can never collide with a different instance.
            nodes[_key(trip, rows[0][3], rows[-1][2], ordinal)] = (
                trip, ordinal, rows)

        for trip, entry, segments in fleet:
            rows = times[trip]
            for ordinal in _context_days(entry, segments, from_ord, to_ord):
                add_node(trip, rows, ordinal)

        ordered = sorted(nodes)
        for previous_key, next_key in zip(ordered, ordered[1:]):
            previous_trip, previous_day, previous_rows = nodes[previous_key]
            next_trip, next_day, next_rows = nodes[next_key]
            if not (from_ord <= previous_day <= to_ord
                    or from_ord <= next_day <= to_ord):
                continue
            previous = _instance(previous_rows, previous_trip, previous_day)
            following = _instance(next_rows, next_trip, next_day)
            available = following['start_abs'] - previous['end_abs']
            required = None
            shortfall = None
            kind = None
            if available < 0:
                kind = 'overlap'
                shortfall = -available
            else:
                link = (previous['end_stop'], following['start_stop'])
                if link in links:
                    required = links[link]
                elif previous['end_stop'] == following['start_stop']:
                    required = 0
                else:
                    kind = 'missing_link'
                if kind is None and available < required:
                    kind = 'insufficient_turnaround'
                    shortfall = required - available
            if kind is None:
                continue
            item = {
                'kind': kind,
                'vehicle_id': vehicle,
                'previous': _public(previous),
                'next': _public(following),
                'available_seconds': available,
                'required_seconds': required,
                'shortfall_seconds': shortfall,
            }
            key = (vehicle,
                   following['start_abs'], previous['start_abs'],
                   following['trip_id'], previous['trip_id'],
                   following['service_date'], previous['service_date'])
            connections.append((key, item))

    connections.sort(key=lambda pair: pair[0])
    return {'feasible': not duplicates and not connections,
            'duplicate_assignments': duplicates,
            'connections': [item for _, item in connections]}


def _instance(rows, trip, ordinal):
    first, last = rows[0], rows[-1]
    return {'trip_id': trip,
            'service_date': date.fromordinal(ordinal).isoformat(),
            'start_stop': first[1],
            'end_stop': last[1],
            'start': hhmmss(first[3]),
            'end': hhmmss(last[2]),
            'start_abs': ordinal * DAY + first[3],
            'end_abs': ordinal * DAY + last[2]}


def _public(instance):
    return {'trip_id': instance['trip_id'],
            'service_date': instance['service_date'],
            'start_stop': instance['start_stop'],
            'end_stop': instance['end_stop'],
            'start': instance['start'],
            'end': instance['end']}
