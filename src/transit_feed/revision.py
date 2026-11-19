"""Traceable timetable revisions stored as ranges, never per-day copies.

The baseline archive produced by :mod:`transit_feed.feed` is immutable input.
``revision_init`` makes an independent backup and adds three small tables:
identities, immutable version rows and the current knowledge sequence.  A
successful apply assigns one new sequence number to the whole batch.  Every
version row keeps its date range, so a query for an old sequence number is
evaluated from history instead of from mutable per-day materialisations.
"""
from datetime import date
import os
from pathlib import Path
import sqlite3
import tempfile

from .common import fields, integer, read_json, text
from .feed import hhmmss, seconds

MAX_TIME = 47 * 3600 + 59 * 60 + 59
BASE_TABLES = {'stops', 'trips', 'calendar', 'exceptions', 'times'}
REVISION_TABLES = {'revision_state', 'revision_identity', 'revision_version'}

REVISION_SCHEMA = '''
CREATE TABLE revision_state(known_seq INTEGER NOT NULL CHECK(known_seq >= 0));
CREATE TABLE revision_identity(
 correction_id TEXT PRIMARY KEY,
 trip_id TEXT NOT NULL,
 stop_sequence INTEGER NOT NULL
);
CREATE TABLE revision_version(
 correction_id TEXT NOT NULL,
 accept_seq INTEGER NOT NULL CHECK(accept_seq > 0),
 kind TEXT NOT NULL CHECK(kind IN ('put','retract')),
 from_day TEXT,
 to_day TEXT,
 arrival INTEGER,
 departure INTEGER,
 PRIMARY KEY(correction_id, accept_seq),
 CHECK((kind='put' AND from_day IS NOT NULL AND to_day IS NOT NULL
        AND arrival BETWEEN 0 AND 172799 AND departure BETWEEN 0 AND 172799)
       OR (kind='retract' AND from_day IS NULL AND to_day IS NULL
           AND arrival IS NULL AND departure IS NULL)),
 FOREIGN KEY(correction_id) REFERENCES revision_identity(correction_id)
);
CREATE INDEX revision_identity_position ON revision_identity(trip_id, stop_sequence);
CREATE INDEX revision_version_sequence ON revision_version(accept_seq);
'''


def _connect(database, mode):
    """Open an existing SQLite archive without ever creating it."""
    path = Path(database)
    if not path.is_file():
        raise ValueError('database does not exist: ' + str(database))
    uri = path.resolve().as_uri() + '?mode=' + mode
    conn = sqlite3.connect(uri, uri=True, isolation_level=None)
    conn.execute('PRAGMA foreign_keys=ON')
    return conn


def _canonical_date(value):
    if not isinstance(value, str):
        raise ValueError('canonical YYYY-MM-DD date required')
    try:
        parsed = date.fromisoformat(value)
    except ValueError:
        raise ValueError('canonical YYYY-MM-DD date required') from None
    if parsed.isoformat() != value:
        raise ValueError('canonical YYYY-MM-DD date required')
    return value


def _ordinal(value):
    return date.fromisoformat(value).toordinal()


def _tables(conn):
    return {row[0] for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}


def _check(condition, message):
    if not condition:
        raise ValueError('invalid baseline archive: ' + message)


def _validate_baseline(conn):
    """Reject anything that is not the archive produced by ``import_feed``."""
    _check(conn.execute('PRAGMA integrity_check').fetchone()[0] == 'ok', 'integrity check failed')
    _check(_tables(conn) == BASE_TABLES, 'unexpected table set')
    _check(not conn.execute("SELECT 1 FROM sqlite_master WHERE type IN ('view','trigger')").fetchone(),
           'views or triggers are not supported')

    expected = {
        'stops': ['id', 'name'],
        'trips': ['id', 'service'],
        'calendar': ['service', 'start', 'end', 'weekdays'],
        'exceptions': ['service', 'day', 'kind'],
        'times': ['trip', 'stop', 'seq', 'arrival', 'departure'],
    }
    for table, columns in expected.items():
        actual = [row[1] for row in conn.execute('PRAGMA table_info("%s")' % table)]
        _check(actual == columns, 'unexpected columns for ' + table)

    stops = conn.execute('SELECT id,name FROM stops').fetchall()
    _check(bool(stops), 'stops are required')
    stop_ids = {stop for stop, _ in stops}
    for stop, name in stops:
        try:
            text(stop)
            text(name)
        except ValueError:
            _check(False, 'invalid stop identifier')

    services = set()
    for service, start, end, weekdays in conn.execute(
            'SELECT service,start,end,weekdays FROM calendar'):
        try:
            text(service)
        except ValueError:
            _check(False, 'invalid service identifier')
        _check(isinstance(weekdays, str) and len(weekdays) == 7
               and set(weekdays) <= {'0', '1'}, 'invalid weekday flags')
        try:
            ordered = _canonical_date(start) <= _canonical_date(end)
        except ValueError:
            ordered = False
        _check(ordered, 'invalid calendar date range')
        services.add(service)

    for service, day, kind in conn.execute('SELECT service,day,kind FROM exceptions'):
        try:
            text(service)
            _canonical_date(day)
        except ValueError:
            _check(False, 'invalid exception row')
        _check(type(kind) is int and kind in (1, 2), 'invalid exception kind')
        services.add(service)

    trips = {}
    for trip, service in conn.execute('SELECT id,service FROM trips'):
        try:
            text(trip)
        except ValueError:
            _check(False, 'invalid trip identifier')
        _check(service in services, 'trip refers to an unknown service')
        trips[trip] = service
    _check(bool(trips), 'trips are required')

    seen = set()
    by_trip = {}
    for trip, stop, seq, arrival, departure in conn.execute(
            'SELECT trip,stop,seq,arrival,departure FROM times'):
        _check(trip in trips, 'time refers to an unknown trip')
        _check(stop in stop_ids, 'time refers to an unknown stop')
        _check(type(seq) is int and seq > 0, 'invalid stop sequence')
        _check(type(arrival) is int and 0 <= arrival <= MAX_TIME, 'arrival outside range')
        _check(type(departure) is int and 0 <= departure <= MAX_TIME, 'departure outside range')
        _check(arrival <= departure, 'departure before arrival')
        _check((trip, seq) not in seen, 'duplicate trip stop sequence')
        seen.add((trip, seq))
        by_trip.setdefault(trip, []).append((seq, arrival, departure))

    _check(set(by_trip) == set(trips), 'every trip needs stop times')
    for rows in by_trip.values():
        rows.sort()
        _check(len(rows) >= 2, 'a trip needs at least two stops')
        _check(all(a[2] <= b[1] for a, b in zip(rows, rows[1:])),
               'trip times are not monotone')
    _check(not conn.execute('PRAGMA foreign_key_check').fetchall(), 'foreign key check failed')


def revision_init(source_db, new_db):
    """Copy a legal baseline archive into a new revision warehouse."""
    destination = Path(new_db)
    if destination.exists() or destination.is_symlink():
        raise ValueError('destination database must not exist')
    source = _connect(source_db, 'ro')
    try:
        _validate_baseline(source)
        destination.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(dir=destination.parent, suffix='.sqlite')
        os.close(fd)
        try:
            target = sqlite3.connect(temporary, isolation_level=None)
            try:
                target.execute('PRAGMA foreign_keys=ON')
                source.backup(target)
                target.executescript(REVISION_SCHEMA)
                target.execute('INSERT INTO revision_state VALUES (0)')
                if target.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                    raise ValueError('copied archive failed integrity check')
            finally:
                target.close()
            # Hard-link publication refuses an existing target and leaves BASE alone.
            os.link(temporary, destination)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
    finally:
        source.close()
    return {'known_seq': 0}


def _time(value):
    if not isinstance(value, str):
        raise ValueError('time must be a string 00:00:00..47:59:59')
    return seconds(value)


def _batch_changes(batch):
    if isinstance(batch, (str, bytes, os.PathLike)):
        batch = read_json(batch)
    if not isinstance(batch, dict):
        raise ValueError('batch must be a JSON object')
    if set(batch) != {'changes'}:
        raise ValueError('batch fields must be exactly: changes')
    raw_changes = batch['changes']
    if not isinstance(raw_changes, list) or not raw_changes:
        raise ValueError('changes must be a nonempty list')

    changes = []
    ids = set()
    for raw in raw_changes:
        if not isinstance(raw, dict):
            raise ValueError('change must be an object')
        op = raw.get('op')
        if op == 'put':
            fields(raw, 'op correction_id trip_id stop_sequence from to arrival departure')
            correction = text(raw['correction_id'])
            trip = text(raw['trip_id'])
            sequence = integer(raw['stop_sequence'], 1)
            start = _canonical_date(raw['from'])
            end = _canonical_date(raw['to'])
            if start > end:
                raise ValueError('from must not be after to')
            arrival = _time(raw['arrival'])
            departure = _time(raw['departure'])
            change = {'op': 'put', 'correction_id': correction, 'trip_id': trip,
                      'stop_sequence': sequence, 'from': start, 'to': end,
                      'arrival': arrival, 'departure': departure}
        elif op == 'retract':
            fields(raw, 'op correction_id')
            correction = text(raw['correction_id'])
            change = {'op': 'retract', 'correction_id': correction}
        else:
            raise ValueError('op must be put or retract')
        if correction in ids:
            raise ValueError('correction_id repeated in batch')
        ids.add(correction)
        changes.append(change)
    return changes


def _revision_state(conn):
    tables = _tables(conn)
    if tables != BASE_TABLES | REVISION_TABLES:
        raise ValueError('not a revision warehouse')
    if conn.execute("SELECT 1 FROM sqlite_master WHERE type IN ('view','trigger')").fetchone():
        raise ValueError('views or triggers are not supported')
    rows = conn.execute('SELECT known_seq FROM revision_state').fetchall()
    if len(rows) != 1 or type(rows[0][0]) is not int or rows[0][0] < 0:
        raise ValueError('invalid revision state')
    return rows[0][0]


def _latest_versions(conn):
    rows = conn.execute('''
        SELECT v.correction_id, v.accept_seq, v.kind, v.from_day, v.to_day,
               v.arrival, v.departure
        FROM revision_version v
        WHERE v.accept_seq = (SELECT MAX(w.accept_seq) FROM revision_version w
                              WHERE w.correction_id = v.correction_id)''').fetchall()
    return {row[0]: row[1:] for row in rows}


def _validate_trip(conn, trip):
    """Validate the final timetable for all date segments of one trip.

    Values change only at a revision's start date and at the day after its end
    date.  A sweep over those boundaries therefore covers every calendar date,
    including dates on which the service does not operate.
    """
    baseline_rows = conn.execute(
        'SELECT seq,arrival,departure FROM times WHERE trip=? ORDER BY seq', (trip,)).fetchall()
    if not baseline_rows:
        raise ValueError('unknown trip in revision')
    order = [row[0] for row in baseline_rows]
    position = {seq: index for index, seq in enumerate(order)}
    current = {seq: (arrival, departure) for seq, arrival, departure in baseline_rows}

    # Baseline is legal by the import contract; check it once as a guard.
    for index, seq in enumerate(order):
        arrival, departure = current[seq]
        if not (0 <= arrival <= MAX_TIME and 0 <= departure <= MAX_TIME and arrival <= departure):
            raise ValueError('invalid baseline time in trip ' + trip)
        if index and arrival < current[order[index - 1]][1]:
            raise ValueError('invalid baseline ordering in trip ' + trip)
        if index + 1 < len(order) and departure > current[order[index + 1]][0]:
            raise ValueError('invalid baseline ordering in trip ' + trip)

    corrections = conn.execute('''
        SELECT i.correction_id, i.stop_sequence, v.accept_seq, v.from_day, v.to_day,
               v.arrival, v.departure
        FROM revision_identity i JOIN revision_version v
          ON v.correction_id = i.correction_id
        WHERE i.trip_id = ?
          AND v.accept_seq = (SELECT MAX(w.accept_seq) FROM revision_version w
                              WHERE w.correction_id = i.correction_id)
          AND v.kind = 'put' ''', (trip,)).fetchall()

    events = {}
    for correction, seq, accept_seq, start, end, arrival, departure in corrections:
        if seq not in position:
            raise ValueError('revision refers to an unknown stop sequence')
        value = (accept_seq, correction, arrival, departure)
        events.setdefault(_ordinal(start), []).append((seq, value, True))
        events.setdefault(_ordinal(end) + 1, []).append((seq, value, False))

    active = {seq: {} for seq in order}
    baseline = {seq: (arrival, departure) for seq, arrival, departure in baseline_rows}

    def check_position(seq):
        arrival, departure = current[seq]
        if not (0 <= arrival <= MAX_TIME and 0 <= departure <= MAX_TIME and arrival <= departure):
            raise ValueError('revision makes arrival/departure invalid for trip ' + trip)
        index = position[seq]
        if index and arrival < current[order[index - 1]][1]:
            raise ValueError('revision makes trip times run backwards: ' + trip)
        if index + 1 < len(order) and departure > current[order[index + 1]][0]:
            raise ValueError('revision makes trip times run backwards: ' + trip)

    for boundary in sorted(events):
        touched = set()
        for seq, value, adding in events[boundary]:
            if adding:
                active[seq][(value[0], value[1])] = (value[2], value[3])
            else:
                active[seq].pop((value[0], value[1]), None)
            touched.add(seq)
        for seq in touched:
            if active[seq]:
                best = max(active[seq])
                current[seq] = active[seq][best]
            else:
                current[seq] = baseline[seq]
        # All changes on this date are in place before local checks run.
        for seq in touched:
            check_position(seq)


def revision_apply(database, batch):
    """Accept or reject one batch atomically and return the new sequence."""
    changes = _batch_changes(batch)
    conn = _connect(database, 'rw')
    try:
        conn.execute('BEGIN IMMEDIATE')
        known = _revision_state(conn)
        identities = {row[0]: (row[1], row[2]) for row in conn.execute(
            'SELECT correction_id,trip_id,stop_sequence FROM revision_identity')}
        latest = _latest_versions(conn)
        new_seq = known + 1
        affected = set()

        for change in changes:
            correction = change['correction_id']
            if change['op'] == 'put':
                identity = (change['trip_id'], change['stop_sequence'])
                if correction in identities and identities[correction] != identity:
                    raise ValueError('correction identity cannot change')
                if conn.execute('SELECT 1 FROM times WHERE trip=? AND seq=?',
                                identity).fetchone() is None:
                    raise ValueError('unknown trip or stop sequence')
                affected.add(identity[0])
            else:
                if correction not in identities or correction not in latest:
                    raise ValueError('unknown correction')
                if latest[correction][1] == 'retract':
                    raise ValueError('correction already retracted')
                affected.add(identities[correction][0])

        for change in changes:
            correction = change['correction_id']
            if change['op'] == 'put':
                if correction not in identities:
                    conn.execute('INSERT INTO revision_identity VALUES (?,?,?)',
                                 (correction, change['trip_id'], change['stop_sequence']))
                conn.execute('INSERT INTO revision_version VALUES (?,?,?,?,?,?,?)',
                             (correction, new_seq, 'put', change['from'], change['to'],
                              change['arrival'], change['departure']))
            else:
                conn.execute('INSERT INTO revision_version VALUES (?,?,?,?,?,?,?)',
                             (correction, new_seq, 'retract', None, None, None, None))

        conn.execute('UPDATE revision_state SET known_seq=?', (new_seq,))
        for trip in affected:
            _validate_trip(conn, trip)
        conn.commit()
        return {'known_seq': new_seq}
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _active_services(conn, service_date, weekday):
    active = {row[0] for row in conn.execute(
        'SELECT service FROM calendar WHERE start<=? AND end>=? AND substr(weekdays,?,1)=?',
        (service_date, service_date, weekday, '1'))}
    for service, kind in conn.execute(
            'SELECT service,kind FROM exceptions WHERE day=?', (service_date,)):
        if kind == 1:
            active.add(service)
        else:
            active.discard(service)
    return active


def revised_departures(database, service_date, stop, after='00:00:00', known_seq=None):
    """Return final departures at a stop for one service day and knowledge K."""
    day = date.fromisoformat(service_date)
    if day.isoformat() != service_date:
        raise ValueError('canonical ISO service date required')
    cutoff = _time(after)
    conn = _connect(database, 'ro')
    try:
        current = _revision_state(conn)
        if known_seq is None:
            sequence = current
        else:
            sequence = integer(known_seq, 0, current)
        if conn.execute('SELECT 1 FROM stops WHERE id=?', (stop,)).fetchone() is None:
            raise ValueError('unknown stop')

        # Each correction ID contributes only its latest state at or below K.
        rows = conn.execute('''
            SELECT i.correction_id, i.trip_id, i.stop_sequence,
                   v.accept_seq, v.kind, v.from_day, v.to_day, v.arrival, v.departure
            FROM revision_identity i JOIN revision_version v
              ON v.correction_id = i.correction_id
            WHERE v.accept_seq <= ?
              AND v.accept_seq = (SELECT MAX(w.accept_seq) FROM revision_version w
                                  WHERE w.correction_id = i.correction_id
                                    AND w.accept_seq <= ?)''',
            (sequence, sequence)).fetchall()

        overrides = {}
        for correction, trip, seq, accept_seq, kind, start, end, arrival, departure in rows:
            if kind != 'put' or not (start <= service_date <= end):
                continue
            key = (trip, seq)
            priority = (accept_seq, correction)
            old = overrides.get(key)
            if old is None or priority > old[0]:
                overrides[key] = (priority, departure, correction)

        active = _active_services(conn, service_date, day.weekday() + 1)
        result = []
        for trip, service, seq, arrival, departure in conn.execute('''
                SELECT t.trip,p.service,t.seq,t.arrival,t.departure
                FROM times t JOIN trips p ON p.id=t.trip
                WHERE t.stop=? ORDER BY t.seq''', (stop,)):
            if service not in active:
                continue
            override = overrides.get((trip, seq))
            correction = None
            if override is not None:
                _, departure, correction = override
            if departure < cutoff:
                continue
            result.append({'trip_id': trip, 'stop_sequence': seq,
                           'departure': hhmmss(departure),
                           'service_date': service_date,
                           'correction_id': correction,
                           '_seconds': departure})
        result.sort(key=lambda row: (row['_seconds'], row['trip_id'], row['stop_sequence']))
        for row in result:
            del row['_seconds']
        return {'known_seq': sequence, 'departures': result}
    finally:
        conn.close()
