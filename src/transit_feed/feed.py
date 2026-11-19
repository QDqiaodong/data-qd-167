"""Import an explicitly documented subset of GTFS-shaped CSV tables."""
import csv
from datetime import date
from pathlib import Path
import re
import sqlite3
from .common import text

SCHEMA = '''CREATE TABLE stops(id TEXT PRIMARY KEY,name TEXT NOT NULL);
CREATE TABLE trips(id TEXT PRIMARY KEY,service TEXT NOT NULL);
CREATE TABLE calendar(service TEXT PRIMARY KEY,start TEXT,end TEXT,weekdays TEXT);
CREATE TABLE exceptions(service TEXT,day TEXT,kind INTEGER,PRIMARY KEY(service,day));
CREATE TABLE times(trip TEXT,stop TEXT,seq INTEGER,arrival INTEGER,departure INTEGER,
 PRIMARY KEY(trip,seq),FOREIGN KEY(trip) REFERENCES trips(id),FOREIGN KEY(stop) REFERENCES stops(id));
CREATE INDEX departures ON times(stop,departure);'''


def table(root, name, columns):
    path = Path(root) / name
    if path.stat().st_size > 8_000_000:
        raise ValueError('CSV exceeds 8MB')
    with path.open(encoding='utf-8-sig', newline='') as stream:
        reader = csv.DictReader(stream)
        if reader.fieldnames != columns.split():
            raise ValueError('unexpected CSV columns: ' + name)
        rows = list(reader)
    if len(rows) > 20000 or any(None in row or None in row.values() for row in rows):
        raise ValueError('CSV row limit or malformed row')
    return rows


def feed_date(value):
    if not re.fullmatch(r'\d{8}', value):
        raise ValueError('YYYYMMDD required')
    return date(int(value[:4]), int(value[4:6]), int(value[6:])).isoformat()


def seconds(value):
    if not re.fullmatch(r'[0-4][0-9]:[0-5][0-9]:[0-5][0-9]', value):
        raise ValueError('time must be 00:00:00..47:59:59')
    h, m, s = map(int, value.split(':'))
    if h > 47:
        raise ValueError('time past 47:59:59')
    return h * 3600 + m * 60 + s


def hhmmss(value):
    h, remainder = divmod(value, 3600)
    m, s = divmod(remainder, 60)
    return f'{h:02}:{m:02}:{s:02}'


def import_feed(source, database):
    conn = sqlite3.connect(database)
    try:
        conn.execute('PRAGMA foreign_keys=ON')
        if conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchone():
            raise ValueError('destination database must be empty')
        conn.executescript('BEGIN;\n' + SCHEMA)
        stops = table(source, 'stops.txt', 'stop_id stop_name')
        conn.executemany('INSERT INTO stops VALUES (?,?)', [(text(r['stop_id']), text(r['stop_name'])) for r in stops])
        services = set()
        for r in table(source, 'calendar.txt', 'service_id monday tuesday wednesday thursday friday saturday sunday start_date end_date'):
            service = text(r['service_id'])
            days = ''.join(r[k] for k in ['monday','tuesday','wednesday','thursday','friday','saturday','sunday'])
            if len(days) != 7 or set(days) - {'0','1'}:
                raise ValueError('weekday flags must be 0 or 1')
            start, end = feed_date(r['start_date']), feed_date(r['end_date'])
            if start > end:
                raise ValueError('inverted service range')
            conn.execute('INSERT INTO calendar VALUES (?,?,?,?)', (service,start,end,days))
            services.add(service)
        for r in table(source, 'calendar_dates.txt', 'service_id date exception_type'):
            service = text(r['service_id'])
            if r['exception_type'] not in ('1','2'):
                raise ValueError('exception type must be 1 or 2')
            conn.execute('INSERT INTO exceptions VALUES (?,?,?)', (service, feed_date(r['date']), int(r['exception_type'])))
            services.add(service)
        for r in table(source, 'trips.txt', 'trip_id service_id'):
            if r['service_id'] not in services:
                raise ValueError('unknown service')
            conn.execute('INSERT INTO trips VALUES (?,?)', (text(r['trip_id']), r['service_id']))
        by_trip = {}
        for r in table(source, 'stop_times.txt', 'trip_id arrival_time departure_time stop_id stop_sequence'):
            if not re.fullmatch(r'[1-9][0-9]{0,5}', r['stop_sequence']):
                raise ValueError('positive stop sequence required')
            arrival, departure = seconds(r['arrival_time']), seconds(r['departure_time'])
            if departure < arrival:
                raise ValueError('departure before arrival')
            row = (text(r['trip_id']), text(r['stop_id']), int(r['stop_sequence']), arrival, departure)
            by_trip.setdefault(row[0], []).append(row)
            conn.execute('INSERT INTO times VALUES (?,?,?,?,?)', row)
        trips = {r[0] for r in conn.execute('SELECT id FROM trips')}
        if not stops or not trips or trips != set(by_trip):
            raise ValueError('nonempty stops/trips and times for every trip required')
        for rows in by_trip.values():
            rows.sort(key=lambda r: r[2])
            if len(rows) < 2 or any(a[4] > b[3] for a,b in zip(rows,rows[1:])):
                raise ValueError('trip must have two stops and monotone times')
        conn.commit()
        return {'stops':len(stops),'trips':len(trips),'stop_times':sum(map(len,by_trip.values()))}
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
