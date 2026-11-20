"""Regional warehouse for several independent publisher archives.

Every source archive keeps its own local stop/trip/service identifiers.  The
regional warehouse therefore namespaces all of them by ``feed_key``: a stop,
trip or service named ``S``/``T``/``WK`` in one feed never meets the equally
named object of another feed.  Calendar and exception evaluation is always
performed for one feed at a time.
"""
import csv
from datetime import date
import os
from pathlib import Path
import sqlite3
import tempfile

from .common import text
from .feed import hhmmss, seconds
from .revision import _tables, _validate_baseline

REGIONAL_SCHEMA = '''
CREATE TABLE regional_feed(
 feed_key TEXT PRIMARY KEY
);
CREATE TABLE regional_interchange(
 interchange_id TEXT PRIMARY KEY
);
CREATE TABLE regional_stop(
 feed_key TEXT NOT NULL REFERENCES regional_feed(feed_key),
 stop_id TEXT NOT NULL,
 name TEXT NOT NULL,
 PRIMARY KEY(feed_key, stop_id)
);
CREATE TABLE regional_trip(
 feed_key TEXT NOT NULL REFERENCES regional_feed(feed_key),
 trip_id TEXT NOT NULL,
 service_id TEXT NOT NULL,
 PRIMARY KEY(feed_key, trip_id)
);
CREATE TABLE regional_calendar(
 feed_key TEXT NOT NULL REFERENCES regional_feed(feed_key),
 service_id TEXT NOT NULL,
 start TEXT NOT NULL,
 end TEXT NOT NULL,
 weekdays TEXT NOT NULL,
 PRIMARY KEY(feed_key, service_id)
);
CREATE TABLE regional_exception(
 feed_key TEXT NOT NULL REFERENCES regional_feed(feed_key),
 service_id TEXT NOT NULL,
 day TEXT NOT NULL,
 kind INTEGER NOT NULL CHECK(kind IN (1, 2)),
 PRIMARY KEY(feed_key, service_id, day)
);
CREATE TABLE regional_time(
 feed_key TEXT NOT NULL,
 trip_id TEXT NOT NULL,
 stop_id TEXT NOT NULL,
 seq INTEGER NOT NULL CHECK(seq > 0),
 arrival INTEGER NOT NULL CHECK(arrival BETWEEN 0 AND 172799),
 departure INTEGER NOT NULL CHECK(departure BETWEEN 0 AND 172799),
 PRIMARY KEY(feed_key, trip_id, seq),
 FOREIGN KEY(feed_key, trip_id) REFERENCES regional_trip(feed_key, trip_id),
 FOREIGN KEY(feed_key, stop_id) REFERENCES regional_stop(feed_key, stop_id)
);
CREATE TABLE regional_mapping(
 feed_key TEXT NOT NULL,
 stop_id TEXT NOT NULL,
 interchange_id TEXT NOT NULL REFERENCES regional_interchange(interchange_id),
 PRIMARY KEY(feed_key, stop_id),
 FOREIGN KEY(feed_key, stop_id) REFERENCES regional_stop(feed_key, stop_id)
);
CREATE INDEX regional_mapping_interchange
 ON regional_mapping(interchange_id, feed_key, stop_id);
CREATE INDEX regional_time_stop
 ON regional_time(feed_key, stop_id, departure);
'''

REGIONAL_TABLES = {
    'regional_feed', 'regional_interchange', 'regional_stop', 'regional_trip',
    'regional_calendar', 'regional_exception', 'regional_time',
    'regional_mapping',
}

REGIONAL_COLUMNS = {
    'regional_feed': ['feed_key'],
    'regional_interchange': ['interchange_id'],
    'regional_stop': ['feed_key', 'stop_id', 'name'],
    'regional_trip': ['feed_key', 'trip_id', 'service_id'],
    'regional_calendar': ['feed_key', 'service_id', 'start', 'end', 'weekdays'],
    'regional_exception': ['feed_key', 'service_id', 'day', 'kind'],
    'regional_time': ['feed_key', 'trip_id', 'stop_id', 'seq', 'arrival',
                      'departure'],
    'regional_mapping': ['feed_key', 'stop_id', 'interchange_id'],
}


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


def _normalise_feeds(feeds):
    """Accept a mapping, ``(key, path)`` pairs or ``KEY=PATH`` strings."""
    if isinstance(feeds, dict):
        entries = list(feeds.items())
    elif isinstance(feeds, (str, bytes, os.PathLike)):
        raise ValueError('feeds must be a mapping or a sequence of pairs')
    else:
        try:
            entries = list(feeds)
        except TypeError:
            raise ValueError('feeds must be a mapping or a sequence of pairs') from None

    pairs = []
    for entry in entries:
        if isinstance(entry, str):
            key, separator, raw_path = entry.partition('=')
            if not separator:
                raise ValueError('feed must be KEY=DB')
        elif isinstance(entry, (tuple, list)) and len(entry) == 2:
            key, raw_path = entry
            if not isinstance(key, str):
                raise ValueError('feed key must be a string')
        else:
            raise ValueError('feed must be KEY=DB or a (KEY, DB) pair')
        key = text(key)
        source = _path(raw_path, 'feed database')
        pairs.append((key, source))

    if not 2 <= len(pairs) <= 8:
        raise ValueError('two to eight feeds are required')
    keys = set()
    paths = set()
    for key, source in pairs:
        if key in keys:
            raise ValueError('feed keys must be distinct')
        keys.add(key)
        try:
            real = os.path.realpath(os.fspath(source))
        except OSError:
            real = os.fspath(source)
        if real in paths:
            raise ValueError('feed database paths must be distinct')
        paths.add(real)
    for key, source in pairs:
        if not source.is_file():
            raise ValueError('feed database does not exist: ' + str(source))
    return pairs


def _load_source(source):
    """Read one complete baseline archive without ever writing to it."""
    try:
        conn = sqlite3.connect(source.resolve().as_uri() + '?mode=ro', uri=True)
    except sqlite3.Error as error:
        raise ValueError('invalid feed archive: ' + str(error)) from None
    try:
        conn.execute('PRAGMA foreign_keys=ON')
        # The revision validator accepts exactly the tables produced by import
        # and rejects revision warehouses, views and triggers.
        _validate_baseline(conn)
        stops = conn.execute('SELECT id,name FROM stops ORDER BY id').fetchall()
        trips = conn.execute(
            'SELECT id,service FROM trips ORDER BY id').fetchall()
        calendar = conn.execute(
            'SELECT service,start,end,weekdays FROM calendar ORDER BY service').fetchall()
        exceptions = conn.execute(
            'SELECT service,day,kind FROM exceptions ORDER BY service,day').fetchall()
        times = conn.execute(
            'SELECT trip,stop,seq,arrival,departure FROM times ORDER BY trip,seq').fetchall()
        return {'stops': stops, 'trips': trips, 'calendar': calendar,
                'exceptions': exceptions, 'times': times}
    except sqlite3.Error as error:
        raise ValueError('invalid feed archive: ' + str(error)) from None
    finally:
        conn.close()


def _load_mapping(path, stops_by_feed):
    source = _path(path, 'interchanges CSV')
    rows = []
    seen = set()
    interchanges = set()
    try:
        if not source.is_file():
            raise ValueError('interchanges CSV does not exist: ' + str(source))
        with source.open('r', encoding='utf-8-sig', newline='') as stream:
            reader = csv.reader(stream)
            header = next(reader, None)
            if header != ['feed_key', 'stop_id', 'interchange_id']:
                raise ValueError('unexpected mapping CSV columns')
            for raw in reader:
                if len(raw) != 3:
                    raise ValueError('malformed mapping CSV row')
                feed_key = text(raw[0])
                stop_id = text(raw[1])
                interchange_id = text(raw[2])
                if feed_key not in stops_by_feed:
                    raise ValueError('unknown feed in mapping: ' + feed_key)
                if stop_id not in stops_by_feed[feed_key]:
                    raise ValueError('unknown stop in mapping: ' + stop_id)
                if (feed_key, stop_id) in seen:
                    raise ValueError('duplicate stop mapping')
                seen.add((feed_key, stop_id))
                rows.append((feed_key, stop_id, interchange_id))
                interchanges.add(interchange_id)
    except UnicodeDecodeError:
        raise ValueError('mapping CSV must be UTF-8') from None
    except csv.Error:
        raise ValueError('malformed mapping CSV') from None
    except OSError as error:
        raise ValueError('cannot read mapping CSV: ' + str(error)) from None
    return rows, interchanges


def _make_parent(directory):
    """Create the destination parent and report directories we created."""
    missing = []
    candidate = directory
    while not candidate.exists():
        if candidate == candidate.parent:
            break
        missing.append(candidate)
        candidate = candidate.parent
    directory.mkdir(parents=True, exist_ok=True)
    return missing


def regional_init(feeds, interchanges_csv, new_db):
    """Build an independent regional warehouse from two to eight imports."""
    pairs = _normalise_feeds(feeds)
    destination = _path(new_db, 'destination database')
    if destination.exists() or destination.is_symlink():
        raise ValueError('destination database must not exist')

    loaded = []
    for key, source in pairs:
        loaded.append((key, _load_source(source)))
    stops_by_feed = {key: {stop for stop, _ in data['stops']}
                     for key, data in loaded}
    mapping, interchanges = _load_mapping(interchanges_csv, stops_by_feed)

    temporary = None
    created = []
    published = False
    try:
        created = _make_parent(destination.parent)
        fd, temporary = tempfile.mkstemp(dir=destination.parent, suffix='.sqlite')
        os.close(fd)
        target = sqlite3.connect(temporary, isolation_level=None)
        try:
            target.execute('PRAGMA foreign_keys=ON')
            target.executescript('BEGIN;\n' + REGIONAL_SCHEMA)
            for key, _ in loaded:
                target.execute('INSERT INTO regional_feed VALUES (?)', (key,))
            for interchange_id in sorted(interchanges):
                target.execute('INSERT INTO regional_interchange VALUES (?)',
                               (interchange_id,))
            for key, data in loaded:
                target.executemany(
                    'INSERT INTO regional_stop VALUES (?,?,?)',
                    [(key, stop, name) for stop, name in data['stops']])
                target.executemany(
                    'INSERT INTO regional_trip VALUES (?,?,?)',
                    [(key, trip, service) for trip, service in data['trips']])
                target.executemany(
                    'INSERT INTO regional_calendar VALUES (?,?,?,?,?)',
                    [(key, service, start, end, weekdays)
                     for service, start, end, weekdays in data['calendar']])
                target.executemany(
                    'INSERT INTO regional_exception VALUES (?,?,?,?)',
                    [(key, service, day, kind)
                     for service, day, kind in data['exceptions']])
                target.executemany(
                    'INSERT INTO regional_time VALUES (?,?,?,?,?,?)',
                    [(key, trip, stop, seq, arrival, departure)
                     for trip, stop, seq, arrival, departure in data['times']])
            target.executemany(
                'INSERT INTO regional_mapping VALUES (?,?,?)', mapping)
            if target.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                raise ValueError('regional warehouse failed integrity check')
            if target.execute('PRAGMA foreign_key_check').fetchall():
                raise ValueError('regional warehouse failed foreign key check')
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
            # Hard-link publication is atomic and refuses to replace a target.
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
    return {'feeds': len(loaded), 'interchanges': len(interchanges),
            'mappings': len(mapping)}


def _open_regional(database):
    path = _path(database, 'database')
    if not path.is_file():
        raise ValueError('database does not exist: ' + str(database))
    try:
        conn = sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True,
                               isolation_level=None)
        conn.execute('PRAGMA foreign_keys=ON')
    except sqlite3.Error as error:
        raise ValueError('invalid regional warehouse: ' + str(error)) from None
    return conn


def _validate_regional(conn):
    """Reject anything that is not a complete regional warehouse."""
    try:
        if _tables(conn) != REGIONAL_TABLES:
            raise ValueError('not a regional warehouse')
        if conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type IN ('view','trigger')"
        ).fetchone():
            raise ValueError('views or triggers are not supported')
        if conn.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
            raise ValueError('regional warehouse failed integrity check')
        for table, columns in REGIONAL_COLUMNS.items():
            actual = [row[1] for row in conn.execute(
                'PRAGMA table_info("%s")' % table)]
            if actual != columns:
                raise ValueError('unexpected columns for ' + table)
        if conn.execute('PRAGMA foreign_key_check').fetchall():
            raise ValueError('regional warehouse failed foreign key check')
    except sqlite3.Error as error:
        raise ValueError('invalid regional warehouse: ' + str(error)) from None


def _active_services(conn, feed_key, service_date, weekday):
    """Evaluate one publisher's calendar without borrowing another's rows."""
    active = {row[0] for row in conn.execute(
        'SELECT service_id FROM regional_calendar '
        'WHERE feed_key=? AND start<=? AND end>=? AND substr(weekdays,?,1)=?',
        (feed_key, service_date, service_date, weekday, '1'))}
    for service, kind in conn.execute(
            'SELECT service_id,kind FROM regional_exception '
            'WHERE feed_key=? AND day=?', (feed_key, service_date)):
        if kind == 1:
            active.add(service)
        elif kind == 2:
            active.discard(service)
        else:
            raise ValueError('invalid regional warehouse: bad exception kind')
    return active


def regional_departures(database, service_date, interchange_id, after='00:00:00'):
    """Return publisher-local departures mapped to one regional interchange."""
    day = _canonical_date(service_date)
    cutoff = _time(after)
    interchange = text(interchange_id)
    conn = _open_regional(database)
    try:
        _validate_regional(conn)
        if conn.execute(
                'SELECT 1 FROM regional_interchange WHERE interchange_id=?',
                (interchange,)).fetchone() is None:
            raise ValueError('unknown interchange')
        mappings = conn.execute(
            'SELECT feed_key,stop_id FROM regional_mapping '
            'WHERE interchange_id=? ORDER BY feed_key,stop_id',
            (interchange,)).fetchall()
        active_by_feed = {}
        found = []
        for feed_key, stop_id in mappings:
            if feed_key not in active_by_feed:
                active_by_feed[feed_key] = _active_services(
                    conn, feed_key, service_date, day.weekday() + 1)
            active = active_by_feed[feed_key]
            for trip_id, service, seq, departure in conn.execute(
                    'SELECT t.trip_id,tr.service_id,t.seq,t.departure '
                    'FROM regional_time t '
                    'JOIN regional_trip tr '
                    '  ON tr.feed_key=t.feed_key AND tr.trip_id=t.trip_id '
                    'WHERE t.feed_key=? AND t.stop_id=? AND t.departure>=?',
                    (feed_key, stop_id, cutoff)):
                if service in active:
                    found.append((departure, feed_key, trip_id, seq))
        found.sort(key=lambda row: (row[0], row[1], row[2], row[3]))
        departures = [{'feed_key': feed_key, 'trip_id': trip_id,
                       'stop_sequence': seq, 'departure': hhmmss(departure),
                       'service_date': service_date}
                      for departure, feed_key, trip_id, seq in found]
        return {'service_date': service_date, 'interchange_id': interchange,
                'departures': departures}
    except sqlite3.Error as error:
        raise ValueError('invalid regional warehouse: ' + str(error)) from None
    finally:
        conn.close()
