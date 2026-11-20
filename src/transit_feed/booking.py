"""Group booking workbench over an immutable imported timetable archive.

``booking_init`` copies a legal baseline archive (never a revision, regional or
booking warehouse) together with one per-trip seat capacity, producing a fully
independent SQLite workbench.  Seats are managed per adjacent stop-sequence
segment on each ``(trip_id, service_date)`` instance, so one group may reuse the
front half of a trip whose back half is promised to another group.

Every create/cancel mutation is one SQLite transaction: either the whole
booking identity, receipt, legs and occupancy commitments are visible together
or none of them are.  A successful ``booking_id`` is permanently bound to its
request and receipt; replaying it never reroutes or re-seats, even after
cancellation.
"""
import csv
from datetime import date
import json
import os
from pathlib import Path
import re
import sqlite3
import tempfile

from .common import read_json
from .feed import hhmmss, seconds
from .revision import _connect, _tables, _validate_baseline

BOOKING_TABLES = {'booking_capacity', 'booking', 'booking_leg',
                  'booking_occupancy'}
BASE_TABLES = {'stops', 'trips', 'calendar', 'exceptions', 'times'}
REQUEST_FIELDS = ('booking_id', 'origin', 'destination', 'service_date',
                  'earliest', 'latest_arrival', 'party_size',
                  'max_transfers', 'min_transfer_seconds')
MAX_SEATS = 10_000

BOOKING_SCHEMA = '''
CREATE TABLE booking_capacity(
 trip_id TEXT PRIMARY KEY REFERENCES trips(id),
 seats INTEGER NOT NULL CHECK(seats BETWEEN 0 AND 10000)
);
CREATE TABLE booking(
 booking_id TEXT PRIMARY KEY,
 request_json TEXT NOT NULL,
 receipt_json TEXT NOT NULL,
 status TEXT NOT NULL CHECK(status IN ('active','cancelled'))
);
CREATE TABLE booking_leg(
 booking_id TEXT NOT NULL REFERENCES booking(booking_id),
 leg_index INTEGER NOT NULL CHECK(leg_index >= 0),
 trip_id TEXT NOT NULL,
 service_date TEXT NOT NULL,
 board_seq INTEGER NOT NULL,
 alight_seq INTEGER NOT NULL,
 from_stop TEXT NOT NULL,
 to_stop TEXT NOT NULL,
 departure INTEGER NOT NULL,
 arrival INTEGER NOT NULL,
 party_size INTEGER NOT NULL CHECK(party_size BETWEEN 1 AND 10000),
 PRIMARY KEY(booking_id, leg_index)
);
CREATE INDEX booking_leg_trip ON booking_leg(trip_id, service_date);
CREATE TABLE booking_occupancy(
 trip_id TEXT NOT NULL,
 service_date TEXT NOT NULL,
 from_seq INTEGER NOT NULL,
 to_seq INTEGER NOT NULL,
 used INTEGER NOT NULL CHECK(used > 0),
 PRIMARY KEY(trip_id, service_date, from_seq, to_seq)
);
'''

CAPACITY_COLUMNS = ['trip_id', 'seats']


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


def _time(value):
    if not isinstance(value, str):
        raise ValueError('time must be a string 00:00:00..47:59:59')
    return seconds(value)


def _strict_integer(value, low, high, label):
    if type(value) is not int or not low <= value <= high:
        raise ValueError(label + ' must be an integer from %d to %d' % (low, high))
    return value


def _booking_id(value):
    if (not isinstance(value, str) or not value or value != value.strip()
            or any(ord(c) < 32 or ord(c) == 127 for c in value)):
        raise ValueError('booking_id must be a nonempty string without '
                         'leading/trailing whitespace or control characters')
    return value


def _parse_capacities(capacities_csv, trip_ids):
    """Fully validate the strict capacity CSV before any archive is touched."""
    source = _path(capacities_csv, 'capacities CSV')
    if not source.is_file():
        raise ValueError('capacities CSV does not exist: ' + str(source))
    capacities = {}
    try:
        with source.open('r', encoding='utf-8-sig', newline='') as stream:
            reader = csv.reader(stream, strict=True)
            header = next(reader, None)
            if header != CAPACITY_COLUMNS:
                raise ValueError('unexpected capacities CSV columns')
            for number, raw in enumerate(reader, start=2):
                if len(raw) != 2:
                    raise ValueError(
                        'malformed capacities CSV row %d' % number)
                trip_id, seats_text = raw
                if trip_id not in trip_ids:
                    raise ValueError(
                        'unknown trip_id in capacities CSV row %d' % number)
                if trip_id in capacities:
                    raise ValueError(
                        'duplicate trip_id in capacities CSV row %d' % number)
                if not re.fullmatch(r'[0-9]+', seats_text):
                    raise ValueError(
                        'seats must be ASCII decimal digits in capacities '
                        'CSV row %d' % number)
                seats = int(seats_text)
                if seats > MAX_SEATS:
                    raise ValueError(
                        'seats must be between 0 and 10000 in capacities '
                        'CSV row %d' % number)
                capacities[trip_id] = seats
    except UnicodeDecodeError:
        raise ValueError('capacities CSV must be UTF-8') from None
    except csv.Error:
        raise ValueError('malformed capacities CSV') from None
    except OSError as error:
        raise ValueError('cannot read capacities CSV: ' + str(error)) from None
    if set(capacities) != trip_ids:
        missing = sorted(trip_ids - set(capacities))
        raise ValueError('capacities CSV misses trip_id: '
                         + ', '.join(missing[:10]))
    return capacities


def _make_parent(directory):
    missing = []
    candidate = directory
    while not candidate.exists():
        if candidate == candidate.parent:
            break
        missing.append(candidate)
        candidate = candidate.parent
    directory.mkdir(parents=True, exist_ok=True)
    return missing


def booking_init(source_db, capacities_csv, new_db):
    """Copy a baseline archive and its capacities into a new booking workbench."""
    destination = _path(new_db, 'destination database')
    if destination.exists() or destination.is_symlink():
        raise ValueError('destination database must not exist')
    source = _connect(source_db, 'ro')
    try:
        if _tables(source) != BASE_TABLES:
            raise ValueError('source must be an archive produced by import')
        _validate_baseline(source)
        trip_ids = {row[0] for row in source.execute('SELECT id FROM trips')}
        capacities = _parse_capacities(capacities_csv, trip_ids)

        temporary = None
        created = []
        published = False
        try:
            created = _make_parent(destination.parent)
            fd, temporary = tempfile.mkstemp(dir=destination.parent,
                                             suffix='.sqlite')
            os.close(fd)
            target = sqlite3.connect(temporary, isolation_level=None)
            try:
                target.execute('PRAGMA foreign_keys=ON')
                source.backup(target)
                target.executescript('BEGIN;\n' + BOOKING_SCHEMA)
                target.executemany(
                    'INSERT INTO booking_capacity VALUES (?,?)',
                    sorted(capacities.items()))
                if target.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                    raise ValueError('booking workbench failed integrity check')
                if target.execute('PRAGMA foreign_key_check').fetchall():
                    raise ValueError('booking workbench failed foreign key check')
                target.commit()
            except BaseException:
                try:
                    target.rollback()
                except sqlite3.Error:
                    pass
                raise
            finally:
                target.close()
            try:
                # Hard-link publication is atomic and refuses an existing path.
                os.link(temporary, destination)
                published = True
            except FileExistsError:
                raise ValueError('destination database must not exist') from None
        finally:
            if temporary and os.path.exists(temporary):
                os.unlink(temporary)
            if not published:
                for directory in created:
                    try:
                        directory.rmdir()
                    except OSError:
                        break
    finally:
        source.close()
    return {'trips': len(capacities), 'bookings': 0}


def _open_booking(database, mode):
    path = _path(database, 'database')
    if not path.is_file():
        raise ValueError('database does not exist: ' + str(database))
    try:
        conn = sqlite3.connect(path.resolve().as_uri() + '?mode=' + mode,
                               uri=True, isolation_level=None)
        conn.execute('PRAGMA foreign_keys=ON')
    except sqlite3.Error as error:
        raise ValueError('invalid booking workbench: ' + str(error)) from None
    return conn


def _validate_booking_warehouse(conn):
    """Reject anything that is not a complete, consistent booking workbench."""
    try:
        if _tables(conn) != BASE_TABLES | BOOKING_TABLES:
            raise ValueError('not a booking workbench')
        if conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type IN ('view','trigger')"
        ).fetchone():
            raise ValueError('views or triggers are not supported')
        if conn.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
            raise ValueError('integrity check failed')
        if conn.execute('PRAGMA foreign_key_check').fetchall():
            raise ValueError('foreign key check failed')
        expected = {
            'booking_capacity': ['trip_id', 'seats'],
            'booking': ['booking_id', 'request_json', 'receipt_json', 'status'],
            'booking_leg': ['booking_id', 'leg_index', 'trip_id',
                            'service_date', 'board_seq', 'alight_seq',
                            'from_stop', 'to_stop', 'departure', 'arrival',
                            'party_size'],
            'booking_occupancy': ['trip_id', 'service_date', 'from_seq',
                                  'to_seq', 'used'],
        }
        for table, columns in expected.items():
            actual = [row[1] for row in
                      conn.execute('PRAGMA table_info("%s")' % table)]
            if actual != columns:
                raise ValueError('unexpected columns for ' + table)

        capacities = dict(conn.execute(
            'SELECT trip_id,seats FROM booking_capacity'))
        trip_ids = {row[0] for row in conn.execute('SELECT id FROM trips')}
        if set(capacities) != trip_ids:
            raise ValueError('capacity rows must cover every trip exactly once')

        adjacency = set()
        for trip in trip_ids:
            sequences = [row[0] for row in conn.execute(
                'SELECT seq FROM times WHERE trip=? ORDER BY seq', (trip,))]
            for left, right in zip(sequences, sequences[1:]):
                adjacency.add((trip, left, right))

        for trip, day, from_seq, to_seq, used in conn.execute(
                'SELECT trip_id,service_date,from_seq,to_seq,used '
                'FROM booking_occupancy'):
            if (trip, from_seq, to_seq) not in adjacency:
                raise ValueError('occupancy on a non-adjacent segment')
            if used > capacities[trip]:
                raise ValueError('occupancy exceeds trip capacity')

        statuses = dict(conn.execute(
            'SELECT booking_id,status FROM booking'))
        leg_count = dict(conn.execute(
            'SELECT booking_id,COUNT(*) FROM booking_leg GROUP BY booking_id'))
        for booking_id in statuses:
            if leg_count.get(booking_id, 0) < 1:
                raise ValueError('booking without legs: ' + booking_id)
        for booking_id in leg_count:
            if booking_id not in statuses:
                raise ValueError('leg without booking identity')

        recomputed = {}
        for row in conn.execute(
                'SELECT trip_id,service_date,board_seq,alight_seq,party_size '
                'FROM booking_leg l JOIN booking b '
                '  ON b.booking_id=l.booking_id WHERE b.status=\'active\''):
            trip, day, board_seq, alight_seq, party = row
            sequences = [r[0] for r in conn.execute(
                'SELECT seq FROM times WHERE trip=? ORDER BY seq', (trip,))]
            start = sequences.index(board_seq)
            end = sequences.index(alight_seq)
            if not start < end:
                raise ValueError('leg board sequence must precede alight')
            for left, right in zip(sequences[start:end],
                                   sequences[start + 1:end + 1]):
                key = (trip, day, left, right)
                recomputed[key] = recomputed.get(key, 0) + party
        stored = {(trip, day, left, right): used for trip, day, left, right,
                  used in conn.execute(
                      'SELECT trip_id,service_date,from_seq,to_seq,used '
                      'FROM booking_occupancy')}
        if stored != recomputed:
            raise ValueError('occupancy commitments do not match active legs')
    except sqlite3.Error as error:
        raise ValueError('invalid booking workbench: ' + str(error)) from None


def _active_services(conn, service_date, weekday):
    active = {row[0] for row in conn.execute(
        'SELECT service FROM calendar WHERE start<=? AND end>=? '
        'AND substr(weekdays,?,1)=?',
        (service_date, service_date, weekday, '1'))}
    for service, kind in conn.execute(
            'SELECT service,kind FROM exceptions WHERE day=?',
            (service_date,)):
        if kind == 1:
            active.add(service)
        elif kind == 2:
            active.discard(service)
        else:
            raise ValueError('invalid booking workbench: bad exception kind')
    return active


def _validate_request(request, stop_ids):
    if not isinstance(request, dict) or set(request) != set(REQUEST_FIELDS):
        raise ValueError('request fields must be exactly: '
                         + ', '.join(REQUEST_FIELDS))
    booking_id = _booking_id(request['booking_id'])
    origin = request['origin']
    destination = request['destination']
    if not isinstance(origin, str) or origin not in stop_ids:
        raise ValueError('origin must be an existing stop_id')
    if not isinstance(destination, str) or destination not in stop_ids:
        raise ValueError('destination must be an existing stop_id')
    if origin == destination:
        raise ValueError('origin and destination must differ')
    service_date = _canonical_date(request['service_date']).isoformat()
    earliest = _time(request['earliest'])
    latest_arrival = _time(request['latest_arrival'])
    if earliest > latest_arrival:
        raise ValueError('earliest must not be later than latest_arrival')
    party_size = _strict_integer(request['party_size'], 1, MAX_SEATS,
                                 'party_size')
    max_transfers = _strict_integer(request['max_transfers'], 0, 3,
                                    'max_transfers')
    min_transfer_seconds = _strict_integer(
        request['min_transfer_seconds'], 0, 3600, 'min_transfer_seconds')
    return {
        'booking_id': booking_id,
        'origin': origin,
        'destination': destination,
        'service_date': service_date,
        'earliest': earliest,
        'latest_arrival': latest_arrival,
        'party_size': party_size,
        'max_transfers': max_transfers,
        'min_transfer_seconds': min_transfer_seconds,
    }


def _load_day(conn, request):
    """Load every trip instance running on the request's service day."""
    day = date.fromisoformat(request['service_date'])
    active = _active_services(conn, request['service_date'],
                              day.weekday() + 1)
    visits = {}
    for trip, service in conn.execute('SELECT id,service FROM trips'):
        if service not in active:
            continue
        rows = conn.execute(
            'SELECT seq,stop,arrival,departure FROM times WHERE trip=? '
            'ORDER BY seq', (trip,)).fetchall()
        visits[trip] = rows
    at_stop = {}
    for trip, rows in visits.items():
        for index, (sequence, stop, arrival, departure) in enumerate(rows):
            at_stop.setdefault(stop, []).append((departure, trip, index))
    return visits, at_stop


def _leg_feasible(rows, start, end, trip, service_date, party,
                  capacities, occupancy):
    if capacities[trip] < party:
        return False
    for index in range(start, end):
        segment = (trip, service_date, rows[index][0], rows[index + 1][0])
        if occupancy.get(segment, 0) + party > capacities[trip]:
            return False
    return True


def _choose_journey(conn, request):
    """Enumerate every feasible journey and return the contract-best one.

    Labels per stop are keyed by ``(arrival, leg count, used trips)``.  A
    label is dropped only when another label arrives no later, uses no more
    legs and uses a subset of the same trips; equal keys keep the
    lexicographically smallest leg tuple, so the final tie-break can never be
    lost even when two journeys visit the same stop in a different trip order
    (possible because a physical stop may be visited repeatedly).  Retaining
    every non-dominated label also keeps the only prefix that still fits the
    transfer limit, however late it arrives.
    """
    visits, at_stop = _load_day(conn, request)
    capacities = dict(conn.execute('SELECT trip_id,seats FROM booking_capacity'))
    occupancy = {
        (trip, day, left, right): used
        for trip, day, left, right, used in conn.execute(
            'SELECT trip_id,service_date,from_seq,to_seq,used '
            'FROM booking_occupancy')}

    origin = request['origin']
    destination = request['destination']
    party = request['party_size']
    day = request['service_date']
    earliest = request['earliest']
    latest = request['latest_arrival']
    max_legs = request['max_transfers'] + 1
    min_transfer = request['min_transfer_seconds']
    # labels[stop] maps (arrival, leg count, used-set) -> best path tuple.
    labels = {}
    best = None

    def consider(arrival, path):
        nonlocal best
        ranking = (
            arrival,
            len(path),
            tuple((leg[0], leg[1], leg[2]) for leg in path),
        )
        if best is None or ranking < best[0]:
            best = (ranking, list(path))

    def search(stop, arrive, used, path):
        if path:
            leg_count = len(path)
            used_set = frozenset(used)
            key = (arrive, leg_count, used_set)
            stored = labels.setdefault(stop, {})
            for (old_arrive, old_legs, old_used) in stored:
                if (old_arrive <= arrive and old_legs <= leg_count
                        and old_used <= used_set):
                    # A strictly dominating label makes this one redundant.
                    if (old_arrive, old_legs, old_used) != key:
                        return
                    # Same key: keep only the lexicographically smallest
                    # leg tuple, which is exactly the final ranking key.
                    old_path = stored[key]
                    new_tuple = tuple((leg[0], leg[1], leg[2]) for leg in path)
                    old_tuple = tuple((leg[0], leg[1], leg[2])
                                      for leg in old_path)
                    if old_tuple <= new_tuple:
                        return
            stored[key] = list(path)
            if stop == destination:
                consider(arrive, path)
                return
        if len(path) >= max_legs:
            return
        board_floor = earliest if not path else arrive + min_transfer
        for departure, trip, board_index in at_stop.get(stop, ()):
            if trip in used or departure < board_floor or departure > latest:
                continue
            rows = visits[trip]
            for alight_index in range(board_index + 1, len(rows)):
                alight_arrival = rows[alight_index][2]
                if alight_arrival > latest:
                    break
                if not _leg_feasible(rows, board_index, alight_index, trip,
                                     day, party, capacities, occupancy):
                    continue
                leg = (trip,
                       rows[board_index][0], rows[alight_index][0],
                       stop, rows[alight_index][1],
                       departure, alight_arrival)
                search(rows[alight_index][1], alight_arrival,
                       used | {trip}, path + [leg])

    search(origin, earliest, frozenset(), [])
    if best is None:
        return None
    return best[1]


def _receipt_leg(leg, service_date):
    trip, board_seq, alight_seq, from_stop, to_stop, departure, arrival = leg
    return {
        'trip_id': trip,
        'board_sequence': board_seq,
        'alight_sequence': alight_seq,
        'from_stop': from_stop,
        'to_stop': to_stop,
        'departure': hhmmss(departure),
        'arrival': hhmmss(arrival),
    }


def _failure_receipt(request):
    return {
        'booking_id': request['booking_id'],
        'accepted': False,
        'party_size': request['party_size'],
        'service_date': request['service_date'],
        'arrival': None,
        'legs': [],
    }


def booking_create(database, request):
    """Validate, route and atomically commit one group booking request."""
    if isinstance(request, (str, bytes, os.PathLike)):
        request = read_json(request)
    conn = _open_booking(database, 'rw')
    try:
        _validate_booking_warehouse(conn)
        stop_ids = {row[0] for row in conn.execute('SELECT id FROM stops')}
        # Full request validation precedes any identity lookup.
        valid = _validate_request(request, stop_ids)
        conn.execute('BEGIN IMMEDIATE')
        try:
            existing = conn.execute(
                'SELECT request_json,receipt_json FROM booking '
                'WHERE booking_id=?', (valid['booking_id'],)).fetchone()
            if existing is not None:
                stored_request = json.loads(existing[0])
                if stored_request != request:
                    raise ValueError(
                        'booking_id is already bound to a different request')
                # Replay: never reroute, never touch seats, even if cancelled.
                conn.commit()
                return json.loads(existing[1])

            legs = _choose_journey(conn, valid)
            if legs is None:
                conn.commit()
                return _failure_receipt(valid)

            receipt_legs = [_receipt_leg(leg, valid['service_date'])
                            for leg in legs]
            final_arrival = legs[-1][6]
            receipt = {
                'booking_id': valid['booking_id'],
                'accepted': True,
                'party_size': valid['party_size'],
                'service_date': valid['service_date'],
                'arrival': hhmmss(final_arrival),
                'legs': receipt_legs,
            }
            conn.execute(
                'INSERT INTO booking VALUES (?,?,?,?)',
                (valid['booking_id'],
                 json.dumps(request, ensure_ascii=False, sort_keys=True),
                 json.dumps(receipt, ensure_ascii=False, sort_keys=True),
                 'active'))
            for index, leg in enumerate(legs):
                trip, board_seq, alight_seq, from_stop, to_stop, \
                    departure, arrival = leg
                conn.execute(
                    'INSERT INTO booking_leg VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                    (valid['booking_id'], index, trip,
                     valid['service_date'], board_seq, alight_seq,
                     from_stop, to_stop, departure, arrival,
                     valid['party_size']))
                rows = conn.execute(
                    'SELECT seq FROM times WHERE trip=? ORDER BY seq',
                    (trip,)).fetchall()
                sequences = [row[0] for row in rows]
                start = sequences.index(board_seq)
                end = sequences.index(alight_seq)
                for left, right in zip(sequences[start:end],
                                       sequences[start + 1:end + 1]):
                    conn.execute(
                        'INSERT INTO booking_occupancy VALUES (?,?,?,?,?) '
                        'ON CONFLICT(trip_id,service_date,from_seq,to_seq) '
                        'DO UPDATE SET used=used+excluded.used',
                        (trip, valid['service_date'], left, right,
                         valid['party_size']))
                    row = conn.execute(
                        'SELECT o.used,c.seats FROM booking_occupancy o '
                        'JOIN booking_capacity c ON c.trip_id=o.trip_id '
                        'WHERE o.trip_id=? AND o.service_date=? '
                        'AND o.from_seq=? AND o.to_seq=?',
                        (trip, valid['service_date'], left, right)).fetchone()
                    if row[0] > row[1]:
                        raise ValueError('segment capacity exceeded')
            conn.commit()
            return receipt
        except BaseException:
            conn.rollback()
            raise
    finally:
        conn.close()


def booking_cancel(database, booking_id):
    """Release every segment of an active booking exactly once."""
    booking_id = _booking_id(booking_id)
    conn = _open_booking(database, 'rw')
    try:
        _validate_booking_warehouse(conn)
        conn.execute('BEGIN IMMEDIATE')
        try:
            status = conn.execute(
                'SELECT status FROM booking WHERE booking_id=?',
                (booking_id,)).fetchone()
            if status is None:
                raise ValueError('unknown booking_id')
            if status[0] == 'active':
                legs = conn.execute(
                    'SELECT trip_id,service_date,board_seq,alight_seq,'
                    'party_size FROM booking_leg WHERE booking_id=? '
                    'ORDER BY leg_index', (booking_id,)).fetchall()
                for trip, day, board_seq, alight_seq, party in legs:
                    sequences = [row[0] for row in conn.execute(
                        'SELECT seq FROM times WHERE trip=? ORDER BY seq',
                        (trip,))]
                    start = sequences.index(board_seq)
                    end = sequences.index(alight_seq)
                    for left, right in zip(sequences[start:end],
                                           sequences[start + 1:end + 1]):
                        used = conn.execute(
                            'SELECT used FROM booking_occupancy '
                            'WHERE trip_id=? AND service_date=? '
                            'AND from_seq=? AND to_seq=?',
                            (trip, day, left, right)).fetchone()[0]
                        if used < party:
                            raise ValueError('occupancy would turn negative')
                        if used == party:
                            conn.execute(
                                'DELETE FROM booking_occupancy '
                                'WHERE trip_id=? AND service_date=? '
                                'AND from_seq=? AND to_seq=?',
                                (trip, day, left, right))
                        else:
                            conn.execute(
                                'UPDATE booking_occupancy SET used=used-? '
                                'WHERE trip_id=? AND service_date=? '
                                'AND from_seq=? AND to_seq=?',
                                (party, trip, day, left, right))
                conn.execute(
                    'UPDATE booking SET status=? WHERE booking_id=?',
                    ('cancelled', booking_id))
            conn.commit()
            return {'booking_id': booking_id, 'cancelled': True}
        except BaseException:
            conn.rollback()
            raise
    finally:
        conn.close()


def booking_show(database, booking_id):
    """Return the permanent status and original receipt of one booking."""
    booking_id = _booking_id(booking_id)
    conn = _open_booking(database, 'ro')
    try:
        _validate_booking_warehouse(conn)
        row = conn.execute(
            'SELECT status,receipt_json FROM booking WHERE booking_id=?',
            (booking_id,)).fetchone()
        if row is None:
            raise ValueError('unknown booking_id: ' + booking_id)
        return {'booking_id': booking_id, 'status': row[0],
                'receipt': json.loads(row[1])}
    finally:
        conn.close()
