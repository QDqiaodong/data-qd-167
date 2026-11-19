"""Service-day query: times above 24h remain attached to their service date."""
from datetime import date
from pathlib import Path
import sqlite3
from .feed import seconds, hhmmss


def departures(database, service_date, stop, after='00:00:00'):
    day = date.fromisoformat(service_date)
    if day.isoformat() != service_date:
        raise ValueError('canonical ISO service date required')
    cutoff = seconds(after)
    with sqlite3.connect(Path(database).resolve().as_uri() + '?mode=ro', uri=True) as conn:
        if conn.execute('SELECT id FROM stops WHERE id=?',(stop,)).fetchone() is None:
            raise ValueError('unknown stop')
        active = {r[0] for r in conn.execute('SELECT service FROM calendar WHERE start<=? AND end>=? AND substr(weekdays,?,1)=?',(service_date,service_date,day.weekday()+1,'1'))}
        for service,kind in conn.execute('SELECT service,kind FROM exceptions WHERE day=?',(service_date,)):
            if kind == 1:
                active.add(service)
            else:
                active.discard(service)
        result = []
        for trip,service,seq,time in conn.execute('SELECT t.trip,p.service,t.seq,t.departure FROM times t JOIN trips p ON p.id=t.trip WHERE t.stop=? AND t.departure>=? ORDER BY t.departure,t.trip,t.seq',(stop,cutoff)):
            if service in active:
                result.append({'trip_id':trip,'stop_sequence':seq,'departure':hhmmss(time),'service_date':service_date})
        return result
