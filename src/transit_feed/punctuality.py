"""Actual-arrival punctuality assessment against planned stop visits.

A punctuality ledger is an independent SQLite database created by
:func:`punctuality_init`.  A report aligns one vehicle's observed arrival
events with the planned stop visits of one ``(service_date, trip_id)``
instance.  The alignment is a global sequence alignment: it maximises the
number of pairs, then minimises the total absolute deviation, then keeps the
lexicographically smallest pair list, so a greedy nearest-time match can
never be produced.

Every report stores the historical facts needed for later human review
(observations, effective planned visits, tolerances and the knowledge
sequence) inside the ledger, so a review can be recomputed after the source
schedule archive and the original observation CSV have been moved or removed.
A report, its request identity and its review facts are committed in one
SQLite transaction.
"""
import csv
from datetime import date
import hashlib
import io
import json
import os
from pathlib import Path
import re
import sqlite3
import tempfile
from array import array

from .common import read_json
from .feed import hhmmss, seconds

BASE_TABLES = {'stops', 'trips', 'calendar', 'exceptions', 'times'}
REVISION_TABLES = BASE_TABLES | {
    'revision_state', 'revision_identity', 'revision_version'}
LEDGER_TABLES = {'punctuality_meta', 'punctuality_report'}
OBSERVATION_COLUMNS = ['event_id', 'trip_id', 'service_date', 'stop_id',
                       'observed_arrival']
MAX_BYTES = 8_000_000
MAX_DATA_ROWS = 20_000
MAX_TOLERANCE = 86_400
MAX_SEQUENCE = 10 ** 9

LEDGER_SCHEMA = '''
CREATE TABLE punctuality_meta(
 key TEXT PRIMARY KEY,
 value TEXT NOT NULL
);
CREATE TABLE punctuality_report(
 report_id TEXT PRIMARY KEY,
 kind TEXT NOT NULL CHECK(kind IN ('initial','review')),
 review_of TEXT,
 request_json TEXT NOT NULL,
 report_json TEXT NOT NULL,
 facts_json TEXT NOT NULL
);
'''


def _path(value, label):
    try:
        raw = os.fspath(value)
    except TypeError:
        raise ValueError(label + ' must be a filesystem path') from None
    if not isinstance(raw, str) or not raw:
        raise ValueError(label + ' must be a non-empty filesystem path')
    return Path(raw)


def _absolute(value):
    """Absolute path with normalised separators and dot segments.

    Symlinks are deliberately not resolved and the target need not exist, so
    request identities stay comparable even after the referenced file moves.
    """
    return os.path.abspath(os.fspath(value))


def _same_file(left, right):
    try:
        return os.path.samefile(left, right)
    except OSError:
        return os.path.normcase(os.path.abspath(os.fspath(left))) == \
            os.path.normcase(os.path.abspath(os.fspath(right)))


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


def _strict_integer(value, low, high, label):
    if type(value) is not int or not low <= value <= high:
        raise ValueError(label + ' must be an integer from %d to %d'
                         % (low, high))
    return value


def _report_id(value):
    if (not isinstance(value, str) or not value or value != value.strip()
            or any(ord(c) < 32 or ord(c) == 127 for c in value)):
        raise ValueError('report_id must be a nonempty string without '
                         'leading/trailing whitespace or control characters')
    return value


def _identifier(value, label, number):
    if (not isinstance(value, str) or not value or value != value.strip()
            or any(ord(c) < 32 or ord(c) == 127 for c in value)):
        raise ValueError('invalid %s in observations CSV row %d'
                         % (label, number))
    return value


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


def punctuality_init(report_database):
    """Create an empty, independent punctuality report ledger."""
    destination = _path(report_database, 'report database')
    if destination.exists() or destination.is_symlink():
        raise ValueError('destination database must not exist')
    temporary = None
    created = []
    published = False
    try:
        created = _make_parent(destination.parent)
        fd, temporary = tempfile.mkstemp(dir=destination.parent,
                                         suffix='.sqlite')
        os.close(fd)
        conn = sqlite3.connect(temporary, isolation_level=None)
        try:
            conn.executescript('BEGIN;\n' + LEDGER_SCHEMA)
            conn.execute('INSERT INTO punctuality_meta VALUES (?,?)',
                         ('schema_version', '1'))
            if conn.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                raise ValueError('punctuality ledger failed integrity check')
            conn.commit()
        except BaseException:
            try:
                conn.rollback()
            except sqlite3.Error:
                pass
            raise
        finally:
            conn.close()
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
    return {'reports': 0}


def _open_ledger(database, mode):
    path = _path(database, 'report database')
    if not path.is_file():
        raise ValueError('database does not exist: ' + str(database))
    try:
        conn = sqlite3.connect(path.resolve().as_uri() + '?mode=' + mode,
                               uri=True, isolation_level=None)
    except sqlite3.Error as error:
        raise ValueError('invalid punctuality ledger: ' + str(error)) from None
    return conn


def _validate_ledger(conn):
    tables = {row[0] for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' "
        "AND name NOT LIKE 'sqlite_%'")}
    if tables != LEDGER_TABLES:
        raise ValueError('not a punctuality ledger')
    if conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type IN ('view','trigger')"
    ).fetchone():
        raise ValueError('views or triggers are not supported')
    marker = conn.execute(
        'SELECT value FROM punctuality_meta WHERE key=?',
        ('schema_version',)).fetchone()
    if marker is None or marker[0] != '1':
        raise ValueError('invalid punctuality ledger marker')
    if conn.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
        raise ValueError('integrity check failed')


def _read_observations(observations_csv, first_day, last_day):
    """Fully validate the strict observation CSV and return its raw bytes."""
    source = _path(observations_csv, 'observations CSV')
    if not source.is_file():
        raise ValueError('observations CSV does not exist: ' + str(source))
    if source.stat().st_size > MAX_BYTES:
        raise ValueError('observations CSV exceeds 8MB')
    try:
        raw = source.read_bytes()
    except OSError as error:
        raise ValueError('cannot read observations CSV: ' + str(error)) \
            from None
    if len(raw) > MAX_BYTES:
        raise ValueError('observations CSV exceeds 8MB')
    digest = hashlib.sha256(raw).hexdigest()
    rows = []
    seen_events = set()
    try:
        text = raw.decode('utf-8-sig')
        reader = csv.reader(io.StringIO(text), strict=True)
        header = next(reader, None)
        if header != OBSERVATION_COLUMNS:
            raise ValueError('unexpected observations CSV columns')
        for number, fields in enumerate(reader, start=2):
            if len(fields) != len(OBSERVATION_COLUMNS):
                raise ValueError(
                    'malformed observations CSV row %d' % number)
            event_id, trip_id, service_text, stop_id, arrival_text = fields
            _identifier(event_id, 'event_id', number)
            _identifier(trip_id, 'trip_id', number)
            _identifier(stop_id, 'stop_id', number)
            if not service_text or not arrival_text:
                raise ValueError(
                    'empty field in observations CSV row %d' % number)
            if event_id in seen_events:
                raise ValueError(
                    'duplicate event_id in observations CSV: ' + event_id)
            seen_events.add(event_id)
            day = _canonical_date(service_text)
            if not (first_day <= day <= last_day):
                raise ValueError(
                    'service_date outside the queried range in row %d'
                    % number)
            if not re.fullmatch(r'[0-4][0-9]:[0-5][0-9]:[0-5][0-9]',
                                arrival_text):
                raise ValueError(
                    'observed_arrival must be 00:00:00..47:59:59 in row %d'
                    % number)
            observed = seconds(arrival_text)
            rows.append({'event_id': event_id, 'trip_id': trip_id,
                         'service_date': service_text, 'stop_id': stop_id,
                         'observed_arrival': observed})
            if len(rows) > MAX_DATA_ROWS:
                raise ValueError('observations CSV exceeds 20000 data rows')
    except UnicodeDecodeError:
        raise ValueError('observations CSV must be UTF-8') from None
    except csv.Error:
        raise ValueError('malformed observations CSV') from None
    return digest, rows


def _open_schedule(schedule_database):
    path = _path(schedule_database, 'schedule database')
    if not path.is_file():
        raise ValueError('database does not exist: ' + str(schedule_database))
    try:
        conn = sqlite3.connect(path.resolve().as_uri() + '?mode=ro',
                               uri=True, isolation_level=None)
        conn.execute('PRAGMA foreign_keys=ON')
    except sqlite3.Error as error:
        raise ValueError('invalid schedule archive: ' + str(error)) from None
    tables = {row[0] for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' "
        "AND name NOT LIKE 'sqlite_%'")}
    if tables == BASE_TABLES:
        kind = 'baseline'
    elif tables == REVISION_TABLES:
        kind = 'revision'
    else:
        conn.close()
        raise ValueError('schedule database must be an import or revision '
                         'archive')
    if conn.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
        conn.close()
        raise ValueError('schedule archive failed integrity check')
    return conn, kind


def _current_revision_seq(conn):
    rows = conn.execute('SELECT known_seq FROM revision_state').fetchall()
    if len(rows) != 1 or type(rows[0][0]) is not int or rows[0][0] < 0:
        raise ValueError('invalid revision state')
    return rows[0][0]


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
            raise ValueError('invalid schedule archive: bad exception kind')
    return active


def _revision_overrides(conn, sequence, service_date):
    """Effective arrival overrides for one day at knowledge ``sequence``."""
    rows = conn.execute('''
        SELECT i.trip_id, i.stop_sequence, v.accept_seq, i.correction_id,
               v.from_day, v.to_day, v.arrival
        FROM revision_identity i JOIN revision_version v
          ON v.correction_id = i.correction_id
        WHERE v.accept_seq <= ?
          AND v.accept_seq = (SELECT MAX(w.accept_seq) FROM revision_version w
                              WHERE w.correction_id = i.correction_id
                                AND w.accept_seq <= ?)
          AND v.kind = 'put'
          AND v.from_day <= ? AND v.to_day >= ?''',
        (sequence, sequence, service_date, service_date)).fetchall()
    overrides = {}
    for trip, stop_sequence, accept_seq, correction_id, _, _, arrival in rows:
        key = (trip, stop_sequence)
        priority = (accept_seq, correction_id)
        old = overrides.get(key)
        if old is None or priority > old[0]:
            overrides[key] = (priority, arrival)
    return {key: value[1] for key, value in overrides.items()}


def _build_facts(conn, kind, sequence, observations, trip_ids):
    """Collect effective planned visits for every reported trip instance."""
    stop_ids = {row[0] for row in conn.execute('SELECT id FROM stops')}
    archive_trips = dict(conn.execute('SELECT id,service FROM trips'))
    unknown_trips = sorted(trip_ids - set(archive_trips))
    if unknown_trips:
        raise ValueError('unknown trip_id in observations CSV: '
                         + ', '.join(unknown_trips[:10]))
    unknown_stops = sorted({row['stop_id'] for row in observations}
                           - stop_ids)
    if unknown_stops:
        raise ValueError('unknown stop_id in observations CSV: '
                         + ', '.join(unknown_stops[:10]))

    baseline = {}
    for trip in trip_ids:
        rows = conn.execute(
            'SELECT seq,stop,arrival FROM times WHERE trip=? ORDER BY seq',
            (trip,)).fetchall()
        baseline[trip] = rows

    grouped = {}
    for row in observations:
        grouped.setdefault((row['service_date'], row['trip_id']), []).append(
            row)

    facts = []
    override_cache = {}
    for (service_date, trip), events in sorted(grouped.items()):
        day = date.fromisoformat(service_date)
        active_services = _active_services(conn, service_date,
                                           day.weekday() + 1)
        service_active = archive_trips[trip] in active_services
        visits = []
        if service_active:
            if kind == 'revision':
                overrides = override_cache.get(service_date)
                if overrides is None:
                    overrides = _revision_overrides(conn, sequence,
                                                    service_date)
                    override_cache[service_date] = overrides
            else:
                overrides = {}
            for stop_sequence, stop_id, arrival in baseline[trip]:
                effective = overrides.get((trip, stop_sequence), arrival)
                visits.append({'stop_sequence': stop_sequence,
                               'stop_id': stop_id,
                               'arrival': effective})
        facts.append({'service_date': service_date, 'trip_id': trip,
                      'service_active': service_active,
                      'observations': sorted(
                          ({'event_id': event['event_id'],
                            'stop_id': event['stop_id'],
                            'observed_arrival': event['observed_arrival']}
                           for event in events),
                          key=lambda item: (item['observed_arrival'],
                                            item['event_id'])),
                      'visits': visits})
    return facts


def _optimal_pairs(observations, visits, fixed=()):
    """Globally best order-preserving one-to-one pairing.

    ``observations`` and ``visits`` are already ordered lists of dicts.
    ``fixed`` holds zero or more ``(observation_index, visit_index)`` pairs
    that must appear in the result; callers guarantee they are order-strict,
    stop-compatible and unique.  The ranking is: most pairs, then smallest
    total absolute deviation, then lexicographically smallest
    ``(event_id, stop_sequence)`` list in observation order.
    """
    fixed = sorted(fixed)
    if fixed:
        results = []
        obs_low = visit_low = 0
        for obs_index, visit_index in fixed:
            for local_obs, local_visit in _optimal_pairs(
                    observations[obs_low:obs_index],
                    visits[visit_low:visit_index]):
                results.append((obs_low + local_obs,
                                visit_low + local_visit))
            results.append((obs_index, visit_index))
            obs_low = obs_index + 1
            visit_low = visit_index + 1
        for local_obs, local_visit in _optimal_pairs(
                observations[obs_low:], visits[visit_low:]):
            results.append((obs_low + local_obs, visit_low + local_visit))
        return results

    m, n = len(observations), len(visits)
    # Compact numeric tables keep the O(m*n) dynamic program affordable even
    # for long trips: counts fit in 32 bits and total deviation in 64 bits.
    counts = [array('i', [0]) * (n + 1) for _ in range(m + 1)]
    costs = [array('q', [0]) * (n + 1) for _ in range(m + 1)]
    for i in range(m - 1, -1, -1):
        event = observations[i]
        for j in range(n - 1, -1, -1):
            visit = visits[j]
            best_count = max(counts[i + 1][j], counts[i][j + 1])
            best_cost = None
            for candidate_count, candidate_cost in (
                    (counts[i + 1][j], costs[i + 1][j]),
                    (counts[i][j + 1], costs[i][j + 1])):
                if candidate_count == best_count and (
                        best_cost is None or candidate_cost < best_cost):
                    best_cost = candidate_cost
            if event['stop_id'] == visit['stop_id']:
                paired_count = 1 + counts[i + 1][j + 1]
                paired_cost = abs(event['observed_arrival']
                                  - visit['arrival']) + costs[i + 1][j + 1]
                if paired_count > best_count:
                    best_count, best_cost = paired_count, paired_cost
                elif paired_count == best_count and paired_cost < best_cost:
                    best_cost = paired_cost
            counts[i][j] = best_count
            costs[i][j] = best_cost

    # Reconstruct the lexicographically smallest optimal pair list with an
    # explicit memo stack, so no call depth depends on the input size.
    memo = {}
    stack = [(0, 0)]
    while stack:
        i, j = stack[-1]
        if (i, j) in memo:
            stack.pop()
            continue
        if i == m or j == n or counts[i][j] == 0:
            memo[(i, j)] = ()
            stack.pop()
            continue
        target_count, target_cost = counts[i][j], costs[i][j]
        dependencies = []
        if counts[i + 1][j] == target_count \
                and costs[i + 1][j] == target_cost:
            dependencies.append(('skip_event', (i + 1, j)))
        if counts[i][j + 1] == target_count \
                and costs[i][j + 1] == target_cost:
            dependencies.append(('skip_visit', (i, j + 1)))
        event = observations[i]
        visit = visits[j]
        if event['stop_id'] == visit['stop_id']:
            paired_count = 1 + counts[i + 1][j + 1]
            paired_cost = abs(event['observed_arrival'] - visit['arrival']) \
                + costs[i + 1][j + 1]
            if paired_count == target_count and paired_cost == target_cost:
                dependencies.append(('pair', (i + 1, j + 1)))
        pending = [state for _, state in dependencies
                   if state not in memo]
        if pending:
            stack.extend(pending)
            continue
        options = []
        for action, state in dependencies:
            suffix = memo[state]
            if action == 'pair':
                option = ((event['event_id'], visit['stop_sequence']),) \
                    + suffix
            else:
                option = suffix
            options.append(option)
        memo[(i, j)] = min(options)
        stack.pop()

    event_index = {event['event_id']: index
                   for index, event in enumerate(observations)}
    visit_index = {visit['stop_sequence']: index
                   for index, visit in enumerate(visits)}
    return [(event_index[event_id], visit_index[stop_sequence])
            for event_id, stop_sequence in memo[(0, 0)]]


def _classify(deviation, early_tolerance, late_tolerance):
    if deviation < -early_tolerance:
        return 'early'
    if deviation > late_tolerance:
        return 'late'
    return 'on_time'


def _build_trip(fact, pairs, early_tolerance, late_tolerance, confirmed):
    observations = fact['observations']
    visits = fact['visits']
    matched_events = set()
    matched_visits = set()
    matched = []
    for obs_index, visit_index in pairs:
        event = observations[obs_index]
        visit = visits[visit_index]
        deviation = event['observed_arrival'] - visit['arrival']
        matched.append({
            'event_id': event['event_id'],
            'stop_sequence': visit['stop_sequence'],
            'stop_id': visit['stop_id'],
            'scheduled_arrival': hhmmss(visit['arrival']),
            'observed_arrival': hhmmss(event['observed_arrival']),
            'deviation_seconds': deviation,
            'status': _classify(deviation, early_tolerance, late_tolerance),
            'confirmed': (event['event_id'], visit['stop_sequence'])
                         in confirmed,
        })
        matched_events.add(obs_index)
        matched_visits.add(visit_index)
    matched.sort(key=lambda item: item['stop_sequence'])
    missed = [{'stop_sequence': visit['stop_sequence'],
               'stop_id': visit['stop_id'],
               'scheduled_arrival': hhmmss(visit['arrival'])}
              for index, visit in enumerate(visits)
              if index not in matched_visits]
    unmatched = [{'event_id': event['event_id'],
                  'stop_id': event['stop_id'],
                  'observed_arrival': hhmmss(event['observed_arrival'])}
                 for index, event in enumerate(observations)
                 if index not in matched_events]
    unmatched.sort(key=lambda item: (item['observed_arrival'],
                                     item['event_id']))
    return {
        'service_date': fact['service_date'],
        'trip_id': fact['trip_id'],
        'service_active': fact['service_active'],
        'matched': matched,
        'missed': missed,
        'unmatched_observations': unmatched,
    }


def _summarize(trips):
    summary = {'scheduled_visits': 0, 'matched_visits': 0,
               'on_time': 0, 'early': 0, 'late': 0,
               'missed_visits': 0, 'unmatched_observations': 0}
    for trip in trips:
        summary['scheduled_visits'] += len(trip['matched']) \
            + len(trip['missed'])
        summary['matched_visits'] += len(trip['matched'])
        summary['missed_visits'] += len(trip['missed'])
        summary['unmatched_observations'] += \
            len(trip['unmatched_observations'])
        for match in trip['matched']:
            summary[match['status']] += 1
    return summary


def _report_from_facts(facts, early_tolerance, late_tolerance,
                       fixed_by_trip, confirmed_pairs, report_id, review_of,
                       known_seq):
    trips = []
    for fact in facts:
        key = (fact['service_date'], fact['trip_id'])
        fixed = fixed_by_trip.get(key, ())
        if fixed:
            pairs = _optimal_pairs(fact['observations'], fact['visits'],
                                   fixed)
        else:
            pairs = _optimal_pairs(fact['observations'], fact['visits'])
        trips.append(_build_trip(fact, pairs, early_tolerance,
                                 late_tolerance, confirmed_pairs))
    trips.sort(key=lambda trip: (trip['service_date'], trip['trip_id']))
    return {'report_id': report_id, 'review_of': review_of,
            'known_seq': known_seq, 'summary': _summarize(trips),
            'trips': trips}


def _canonical_json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      allow_nan=False)


def punctuality_report(schedule_database, report_database, report_id,
                       observations_csv, from_date, to_date, known_seq=None,
                       early_tolerance=60, late_tolerance=300):
    """Generate and store one initial punctuality report."""
    report_id = _report_id(report_id)
    first_day = _canonical_date(from_date)
    last_day = _canonical_date(to_date)
    if first_day > last_day:
        raise ValueError('from date must not be after to date')
    early = _strict_integer(early_tolerance, 0, MAX_TOLERANCE,
                            'early_tolerance')
    late = _strict_integer(late_tolerance, 0, MAX_TOLERANCE,
                           'late_tolerance')
    explicit_seq = known_seq is not None
    if explicit_seq:
        known_seq = _strict_integer(known_seq, 0, MAX_SEQUENCE, 'known_seq')

    ledger_path = _path(report_database, 'report database')
    schedule_path = _path(schedule_database, 'schedule database')
    csv_path = _path(observations_csv, 'observations CSV')
    if _same_file(ledger_path, schedule_path):
        raise ValueError('schedule database must not be the report ledger')
    if _same_file(ledger_path, csv_path):
        raise ValueError('observations CSV must not be the report ledger')

    digest, observations = _read_observations(csv_path, first_day, last_day)
    schedule_identity = _absolute(schedule_path)
    csv_identity = _absolute(csv_path)
    identity = _initial_identity(
        schedule_identity, csv_identity, digest, from_date, to_date,
        early, late, explicit_seq,
        known_seq if explicit_seq else None)
    identity_text = _canonical_json(identity)

    ledger = _open_ledger(ledger_path, 'rw')
    try:
        _validate_ledger(ledger)
        # Read-only lookups and schedule processing all finish before the
        # write transaction begins; the transaction only guards the identity
        # check and the single insert.
        existing = ledger.execute(
            'SELECT request_json,report_json FROM punctuality_report '
            'WHERE report_id=?', (report_id,)).fetchone()
        if existing is not None:
            # Replay: the schedule archive is deliberately never reopened.
            if existing[0] != identity_text:
                raise ValueError(
                    'report_id is already bound to a different request')
            return json.loads(existing[1])

        schedule, kind = _open_schedule(schedule_path)
        try:
            if kind == 'baseline':
                if explicit_seq and known_seq != 0:
                    raise ValueError(
                        'baseline archives only support known_seq 0')
                sequence = 0
            else:
                current = _current_revision_seq(schedule)
                if explicit_seq:
                    if known_seq > current:
                        raise ValueError(
                            'known_seq must be from 0 to the current '
                            'knowledge sequence')
                    sequence = known_seq
                else:
                    sequence = current
            trip_ids = {row['trip_id'] for row in observations}
            facts = _build_facts(schedule, kind, sequence,
                                 observations, trip_ids)
        finally:
            schedule.close()

        report = _report_from_facts(facts, early, late, {}, set(),
                                    report_id, None, sequence)
        stored_facts = {'known_seq': sequence,
                        'early_tolerance': early,
                        'late_tolerance': late,
                        'trips': facts}
        ledger.execute('BEGIN IMMEDIATE')
        try:
            # Re-check under the write lock so two callers cannot claim the
            # same id with different requests.
            raced = ledger.execute(
                'SELECT request_json,report_json FROM punctuality_report '
                'WHERE report_id=?', (report_id,)).fetchone()
            if raced is not None:
                if raced[0] != identity_text:
                    raise ValueError(
                        'report_id is already bound to a different request')
                ledger.commit()
                return json.loads(raced[1])
            ledger.execute(
                'INSERT INTO punctuality_report VALUES (?,?,?,?,?,?)',
                (report_id, 'initial', None, identity_text,
                 _canonical_json(report), _canonical_json(stored_facts)))
            ledger.commit()
            return report
        except BaseException:
            ledger.rollback()
            raise
    finally:
        ledger.close()


def _initial_identity(schedule_identity, csv_identity, digest, from_date,
                      to_date, early, late, explicit_seq, seq_value):
    identity = {
        'method': 'punctuality-report',
        'schedule_database': schedule_identity,
        'observations_csv': csv_identity,
        'observations_sha256': digest,
        'from_date': from_date,
        'to_date': to_date,
        'early_tolerance': early,
        'late_tolerance': late,
        'known_seq_explicit': explicit_seq,
        'known_seq': seq_value,
    }
    return identity


def punctuality_show(report_database, report_id):
    """Read one saved report body without writing anything."""
    report_id = _report_id(report_id)
    conn = _open_ledger(_path(report_database, 'report database'), 'ro')
    try:
        _validate_ledger(conn)
        row = conn.execute(
            'SELECT report_json FROM punctuality_report WHERE report_id=?',
            (report_id,)).fetchone()
        if row is None:
            raise ValueError('unknown report_id: ' + report_id)
        return json.loads(row[0])
    finally:
        conn.close()


def _parse_decisions(decisions_file, facts):
    if isinstance(decisions_file, (str, bytes, os.PathLike)):
        payload = read_json(decisions_file)
    else:
        raise ValueError('decisions must be a JSON file path')
    if not isinstance(payload, dict) or set(payload) != {'confirmations'}:
        raise ValueError('decisions file must contain exactly confirmations')
    raw = payload['confirmations']
    if not isinstance(raw, list) or not raw:
        raise ValueError('confirmations must be a nonempty list')

    event_location = {}
    for fact in facts:
        for event in fact['observations']:
            event_location[event['event_id']] = fact
    trip_visits = {}
    for fact in facts:
        trip_visits[(fact['service_date'], fact['trip_id'])] = {
            visit['stop_sequence']: visit for visit in fact['visits']}

    pairs = []
    seen_pairs = set()
    seen_events = set()
    seen_visits = {}
    for item in raw:
        if not isinstance(item, dict) or set(item) != {'event_id',
                                                       'stop_sequence'}:
            raise ValueError('each confirmation must have exactly event_id '
                             'and stop_sequence')
        event_id = item['event_id']
        stop_sequence = item['stop_sequence']
        if not isinstance(event_id, str) or not event_id:
            raise ValueError('confirmation event_id must be a nonempty string')
        if type(stop_sequence) is not int or not 1 <= stop_sequence <= MAX_SEQUENCE:
            raise ValueError('confirmation stop_sequence must be a positive '
                             'integer')
        if (event_id, stop_sequence) in seen_pairs:
            raise ValueError('duplicate confirmation: ' + event_id)
        seen_pairs.add((event_id, stop_sequence))
        if event_id in seen_events:
            raise ValueError('event reused by confirmations: ' + event_id)
        if event_id not in event_location:
            raise ValueError('unknown event in confirmations: ' + event_id)
        fact = event_location[event_id]
        key = (fact['service_date'], fact['trip_id'])
        if not fact['service_active']:
            raise ValueError('confirmation refers to an inactive trip: '
                             + event_id)
        visit = trip_visits[key].get(stop_sequence)
        if visit is None:
            raise ValueError('confirmation refers to an unknown stop '
                             'sequence: ' + event_id)
        event_stop = next(event['stop_id']
                          for event in fact['observations']
                          if event['event_id'] == event_id)
        if visit['stop_id'] != event_stop:
            raise ValueError('confirmation stop_id mismatch: ' + event_id)
        if stop_sequence in seen_visits.get(key, ()):
            raise ValueError('planned visit reused by confirmations: '
                             + event_id)
        seen_events.add(event_id)
        seen_visits.setdefault(key, set()).add(stop_sequence)
        pairs.append({'fact': fact, 'event_id': event_id,
                      'stop_sequence': stop_sequence})

    fixed_by_trip = {}
    for entry in pairs:
        fact = entry['fact']
        key = (fact['service_date'], fact['trip_id'])
        obs_order = {event['event_id']: index
                     for index, event in enumerate(fact['observations'])}
        visit_order = {visit['stop_sequence']: index
                       for index, visit in enumerate(fact['visits'])}
        fixed = sorted(
            ((obs_order[item['event_id']],
              visit_order[item['stop_sequence']])
             for item in pairs if item['fact'] is fact))
        for (left_obs, left_visit), (right_obs, right_visit) in zip(
                fixed, fixed[1:]):
            if not (left_obs < right_obs and left_visit < right_visit):
                raise ValueError('confirmations invert the relative sequence '
                                 'order')
        fixed_by_trip[key] = fixed
    confirmation_set = sorted((entry['event_id'], entry['stop_sequence'])
                              for entry in pairs)
    return fixed_by_trip, confirmation_set


def punctuality_revise(report_database, base_report_id, report_id,
                       decisions_file):
    """Generate a reviewed report from human confirmation decisions."""
    base_report_id = _report_id(base_report_id)
    report_id = _report_id(report_id)
    if base_report_id == report_id:
        raise ValueError('review report_id must differ from the base report')
    ledger_path = _path(report_database, 'report database')
    decisions_path = _path(decisions_file, 'decisions file')
    if _same_file(ledger_path, decisions_path):
        raise ValueError('decisions file must not be the report ledger')

    ledger = _open_ledger(ledger_path, 'rw')
    try:
        _validate_ledger(ledger)
        # All file IO and request validation happens before the write
        # transaction begins, so a bad decisions file never holds a lock.
        base = ledger.execute(
            'SELECT kind,request_json,report_json,facts_json '
            'FROM punctuality_report WHERE report_id=?',
            (base_report_id,)).fetchone()
        if base is None:
            raise ValueError('unknown base report_id: ' + base_report_id)
        if base[0] != 'initial':
            raise ValueError('reviews can only be based on an initial '
                             'report')
        stored_facts = json.loads(base[3])
        fixed_by_trip, confirmation_set = _parse_decisions(
            decisions_path, stored_facts['trips'])

        identity = {
            'method': 'punctuality-revise',
            'base_report_id': base_report_id,
            'confirmations': confirmation_set,
        }
        identity_text = _canonical_json(identity)
        confirmed_pairs = set(confirmation_set)
        report = _report_from_facts(
            stored_facts['trips'], stored_facts['early_tolerance'],
            stored_facts['late_tolerance'], fixed_by_trip,
            confirmed_pairs, report_id, base_report_id,
            stored_facts['known_seq'])

        ledger.execute('BEGIN IMMEDIATE')
        try:
            existing = ledger.execute(
                'SELECT request_json,report_json FROM punctuality_report '
                'WHERE report_id=?', (report_id,)).fetchone()
            if existing is not None:
                if existing[0] != identity_text:
                    raise ValueError(
                        'report_id is already bound to a different request')
                ledger.commit()
                return json.loads(existing[1])
            ledger.execute(
                'INSERT INTO punctuality_report VALUES (?,?,?,?,?,?)',
                (report_id, 'review', base_report_id, identity_text,
                 _canonical_json(report), base[3]))
            ledger.commit()
            return report
        except BaseException:
            ledger.rollback()
            raise
    finally:
        ledger.close()
