import csv
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest

from transit_feed.feed import import_feed
from transit_feed.revision import revision_init
from transit_feed.regional import regional_init
from transit_feed.booking import (booking_cancel, booking_create, booking_init,
                                  booking_show)

ROOT = Path(__file__).resolve().parents[1]
MODULE = 'transit_feed'


def write_feed(root, stops, trips, services, exceptions, times):
    root.mkdir(parents=True, exist_ok=True)

    def table(name, header, rows):
        with (root / name).open('w', encoding='utf-8', newline='') as stream:
            writer = csv.writer(stream)
            writer.writerow(header)
            writer.writerows(rows)

    table('stops.txt', ['stop_id', 'stop_name'],
          [[stop, stop] for stop in stops])
    table('trips.txt', ['trip_id', 'service_id'],
          [[trip, service] for trip, service in trips])
    table('calendar.txt',
          ['service_id', 'monday', 'tuesday', 'wednesday', 'thursday',
           'friday', 'saturday', 'sunday', 'start_date', 'end_date'],
          [[service, '1', '1', '1', '1', '1', '1', '1', '18000101',
            '24001231'] for service in services])
    table('calendar_dates.txt', ['service_id', 'date', 'exception_type'],
          exceptions)
    table('stop_times.txt',
          ['trip_id', 'arrival_time', 'departure_time', 'stop_id',
           'stop_sequence'], times)


def make_feed(root, stops, trips, times, exceptions=(), services=('WK',)):
    write_feed(
        root, stops,
        [(trip, 'WK') for trip in trips],
        list(services), list(exceptions), times)


def write_capacities(path, rows, encoding='utf-8'):
    with Path(path).open('w', encoding=encoding, newline='') as stream:
        writer = csv.writer(stream)
        writer.writerow(['trip_id', 'seats'])
        writer.writerows(rows)


def request(booking_id, origin, destination, **overrides):
    value = {
        'booking_id': booking_id,
        'origin': origin,
        'destination': destination,
        'service_date': '2026-11-20',
        'earliest': '08:00:00',
        'latest_arrival': '11:00:00',
        'party_size': 2,
        'max_transfers': 1,
        'min_transfer_seconds': 300,
    }
    value.update(overrides)
    return value


class AcceptanceTests(unittest.TestCase):
    """The linked P/Q/R/S/W scenario from the contract."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        feed = root / 'feed'
        times = [
            ['T', '09:00:00', '09:00:00', 'A', 1],
            ['T', '09:30:00', '09:30:00', 'B', 2],
            ['T', '10:00:00', '10:00:00', 'C', 3],
            ['U', '09:35:00', '09:35:00', 'B', 1],
            ['U', '10:10:00', '10:10:00', 'D', 2],
            ['V', '09:05:00', '09:05:00', 'A', 1],
            ['V', '10:30:00', '10:30:00', 'D', 2],
        ]
        make_feed(feed, ['A', 'B', 'C', 'D'], ['T', 'U', 'V'], times)
        self.base = root / 'base.sqlite'
        import_feed(feed, self.base)
        capacities = root / 'cap.csv'
        write_capacities(capacities, [['T', 2], ['U', 2], ['V', 2]])
        self.db = root / 'booking.sqlite'
        self.assertEqual(booking_init(self.base, capacities, self.db),
                         {'trips': 3, 'bookings': 0})

    def create(self, booking_id, origin, destination):
        return booking_create(self.db, request(booking_id, origin, destination))

    def test_full_linked_scenario(self):
        p = self.create('P', 'B', 'C')
        self.assertTrue(p['accepted'])
        self.assertEqual(p['arrival'], '10:00:00')
        self.assertEqual([(l['trip_id'], l['board_sequence'],
                          l['alight_sequence']) for l in p['legs']],
                         [('T', 2, 3)])

        q = self.create('Q', 'A', 'D')
        self.assertTrue(q['accepted'])
        self.assertEqual(q['arrival'], '10:10:00')
        self.assertEqual([l['trip_id'] for l in q['legs']], ['T', 'U'])

        r = self.create('R', 'A', 'D')
        self.assertTrue(r['accepted'])
        self.assertEqual(r['arrival'], '10:30:00')
        self.assertEqual([l['trip_id'] for l in r['legs']], ['V'])

        self.assertEqual(booking_cancel(self.db, 'Q'),
                         {'booking_id': 'Q', 'cancelled': True})

        s = self.create('S', 'A', 'C')
        self.assertFalse(s['accepted'])
        self.assertIsNone(s['arrival'])
        self.assertEqual(s['legs'], [])

        w = self.create('W', 'A', 'D')
        self.assertTrue(w['accepted'])
        self.assertEqual(w['arrival'], '10:10:00')
        self.assertEqual([l['trip_id'] for l in w['legs']], ['T', 'U'])

        # Replaying the cancelled Q keeps its original 10:10 receipt.
        replay = self.create('Q', 'A', 'D')
        self.assertEqual(replay, q)
        self.assertEqual(booking_show(self.db, 'Q')['status'], 'cancelled')

        # Cancelling Q again frees nothing belonging to W.
        self.assertEqual(booking_cancel(self.db, 'Q'),
                         {'booking_id': 'Q', 'cancelled': True})
        z = self.create('Z', 'A', 'D')
        self.assertFalse(z['accepted'])

    def test_receipt_leg_shape(self):
        p = self.create('P', 'B', 'C')
        self.assertEqual(set(p), {'booking_id', 'accepted', 'party_size',
                                  'service_date', 'arrival', 'legs'})
        leg = p['legs'][0]
        self.assertEqual(set(leg), {'trip_id', 'board_sequence',
                                    'alight_sequence', 'from_stop', 'to_stop',
                                    'departure', 'arrival'})
        self.assertEqual(leg['from_stop'], 'B')
        self.assertEqual(leg['to_stop'], 'C')
        self.assertEqual(leg['departure'], '09:30:00')

    def test_failure_leaves_no_identity_and_is_retryable(self):
        self.create('P', 'B', 'C')
        first = self.create('S', 'A', 'C')
        self.assertFalse(first['accepted'])
        with self.assertRaises(ValueError):
            booking_show(self.db, 'S')
        # A different, feasible request under the same new ID is allowed.
        later = booking_create(self.db, request('S', 'A', 'B',
                                                latest_arrival='11:00:00'))
        self.assertTrue(later['accepted'])

    def test_replay_is_byte_for_byte_receipt(self):
        q = self.create('Q', 'A', 'D')
        self.assertEqual(booking_create(self.db, request('Q', 'A', 'D')), q)
        booking_cancel(self.db, 'Q')
        self.assertEqual(booking_create(self.db, request('Q', 'A', 'D')), q)

    def test_replay_ignores_json_key_order(self):
        ordered = request('Q', 'A', 'D')
        first = booking_create(self.db, ordered)
        shuffled = {key: ordered[key] for key in reversed(list(ordered))}
        self.assertEqual(booking_create(self.db, shuffled), first)

    def test_identity_conflict_after_cancel_still_errors(self):
        self.create('Q', 'A', 'D')
        booking_cancel(self.db, 'Q')
        changed = request('Q', 'A', 'C')
        with self.assertRaises(ValueError):
            booking_create(self.db, changed)

    def test_request_validation_precedes_identity_lookup(self):
        self.create('Q', 'A', 'D')
        bad = request('Q', 'A', 'D', party_size=True)
        with self.assertRaises(ValueError):
            booking_create(self.db, bad)

    def test_show_unknown_and_cancel_unknown_error(self):
        with self.assertRaises(ValueError):
            booking_show(self.db, 'NOPE')
        with self.assertRaises(ValueError):
            booking_cancel(self.db, 'NOPE')

    def test_double_cancel_releases_once(self):
        self.create('P', 'B', 'C')
        booking_cancel(self.db, 'P')
        booking_cancel(self.db, 'P')
        with sqlite3.connect(self.db) as conn:
            used = conn.execute('SELECT COALESCE(SUM(used),0) '
                                'FROM booking_occupancy').fetchone()[0]
        self.assertEqual(used, 0)
        self.assertEqual(booking_show(self.db, 'P')['status'], 'cancelled')


class RoutingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def build(self, stops, trips, times, capacities, exceptions=()):
        feed = self.root / 'feed'
        make_feed(feed, stops, trips, times, exceptions)
        self.base = self.root / 'base.sqlite'
        import_feed(feed, self.base)
        cap = self.root / 'cap.csv'
        write_capacities(cap, [[trip, seats] for trip, seats in capacities])
        self.db = self.root / 'booking.sqlite'
        booking_init(self.base, cap, self.db)

    def occupancy(self, trip, left, right, day='2026-11-20'):
        with sqlite3.connect(self.db) as conn:
            row = conn.execute(
                'SELECT used FROM booking_occupancy WHERE trip_id=? '
                'AND service_date=? AND from_seq=? AND to_seq=?',
                (trip, day, left, right)).fetchone()
        return None if row is None else row[0]

    def test_transfer_limit_keeps_later_direct_prefix(self):
        # The early way to X needs an extra transfer at M (AM then MX), so
        # under max_transfers=1 its prefix already exhausts the leg budget.
        # MX2 reaches X later on a single direct A->X leg and can continue.
        times = [
            ['AM', '08:30:00', '08:30:00', 'A', 1],
            ['AM', '08:40:00', '08:40:00', 'M', 2],
            ['MX', '08:45:00', '08:45:00', 'M', 1],
            ['MX', '08:55:00', '08:55:00', 'X', 2],
            ['MX2', '09:00:00', '09:00:00', 'A', 1],
            ['MX2', '09:40:00', '09:40:00', 'X', 2],
            ['XZ', '10:00:00', '10:00:00', 'X', 1],
            ['XZ', '10:30:00', '10:30:00', 'Z', 2],
        ]
        self.build(['A', 'M', 'X', 'Z'], ['AM', 'MX', 'MX2', 'XZ'], times,
                   [('AM', 5), ('MX', 5), ('MX2', 5), ('XZ', 5)])
        result = booking_create(self.db, request('B1', 'A', 'Z',
                                                 max_transfers=1,
                                                 min_transfer_seconds=300))
        self.assertTrue(result['accepted'])
        self.assertEqual([l['trip_id'] for l in result['legs']],
                         ['MX2', 'XZ'])
        self.assertEqual(result['arrival'], '10:30:00')
        # After MX2/XZ are taken, only the longer AM+MX prefix can complete;
        # it needs two transfers, so the same request capped at one fails.
        # Capacities of 4 leave no third seat on the direct chain (B1+B2=4).
        feed2 = self.root / 'feed2'
        make_feed(feed2, ['A', 'M', 'X', 'Z'],
                  ['AM', 'MX', 'MX2', 'XZ'], times)
        base2 = self.root / 'base2.sqlite'
        import_feed(feed2, base2)
        cap2 = self.root / 'cap2.csv'
        write_capacities(cap2, [['AM', 5], ['MX', 5], ['MX2', 4],
                                ['XZ', 9]])
        db2 = self.root / 'wb2.sqlite'
        booking_init(base2, cap2, db2)
        first = booking_create(db2, request('B1', 'A', 'Z',
                                            max_transfers=1,
                                            min_transfer_seconds=300))
        self.assertEqual([l['trip_id'] for l in first['legs']],
                         ['MX2', 'XZ'])
        second = booking_create(db2, request('B2', 'A', 'Z',
                                             max_transfers=2,
                                             min_transfer_seconds=300))
        self.assertEqual([l['trip_id'] for l in second['legs']],
                         ['MX2', 'XZ'])
        only_long = booking_create(db2, request(
            'B3', 'A', 'Z', max_transfers=2, min_transfer_seconds=300))
        self.assertTrue(only_long['accepted'])
        self.assertEqual([l['trip_id'] for l in only_long['legs']],
                         ['AM', 'MX', 'XZ'])
        capped = booking_create(db2, request(
            'B4', 'A', 'Z', max_transfers=1, min_transfer_seconds=300))
        self.assertFalse(capped['accepted'])

    def test_zero_transfer_direct_only(self):
        times = [
            ['D1', '09:00:00', '09:00:00', 'A', 1],
            ['D1', '09:30:00', '09:30:00', 'X', 2],
            ['D1', '10:00:00', '10:00:00', 'Z', 3],
            ['L1', '09:05:00', '09:05:00', 'A', 1],
            ['L1', '09:20:00', '09:20:00', 'X', 2],
            ['L2', '09:25:00', '09:25:00', 'X', 1],
            ['L2', '09:45:00', '09:45:00', 'Z', 2],
        ]
        self.build(['A', 'X', 'Z'], ['D1', 'L1', 'L2'], times,
                   [('D1', 5), ('L1', 5), ('L2', 5)])
        direct = booking_create(self.db, request('D', 'A', 'Z',
                                                 max_transfers=0))
        self.assertEqual([l['trip_id'] for l in direct['legs']], ['D1'])
        self.assertEqual(direct['arrival'], '10:00:00')

    def test_repeated_physical_stop_keeps_sequence_identity(self):
        times = [
            ['LOOP', '09:00:00', '09:00:00', 'A', 1],
            ['LOOP', '09:30:00', '09:30:00', 'X', 2],
            ['LOOP', '10:00:00', '10:00:00', 'A', 3],
            ['LOOP', '10:30:00', '10:30:00', 'B', 4],
        ]
        self.build(['A', 'X', 'B'], ['LOOP'], times, [('LOOP', 5)])
        result = booking_create(self.db, request('L', 'A', 'B'))
        self.assertTrue(result['accepted'])
        leg = result['legs'][0]
        self.assertEqual((leg['board_sequence'], leg['alight_sequence']),
                         (1, 4))
        self.assertEqual((leg['from_stop'], leg['to_stop']), ('A', 'B'))
        # Boarding at the second A visit (seq 3) is a distinct leg choice.
        second = booking_create(
            self.db, request('L2', 'A', 'B', earliest='10:00:00'))
        self.assertEqual((second['legs'][0]['board_sequence'],
                          second['legs'][0]['alight_sequence']), (3, 4))

    def test_non_contiguous_sequences_are_not_indices(self):
        times = [
            ['GAP', '09:00:00', '09:00:00', 'A', 10],
            ['GAP', '09:30:00', '09:30:00', 'X', 25],
            ['GAP', '10:00:00', '10:00:00', 'B', 40],
        ]
        self.build(['A', 'X', 'B'], ['GAP'], times, [('GAP', 5)])
        result = booking_create(self.db, request('G', 'A', 'B'))
        self.assertEqual((result['legs'][0]['board_sequence'],
                          result['legs'][0]['alight_sequence']), (10, 40))
        self.assertEqual(self.occupancy('GAP', 10, 25), 2)
        self.assertEqual(self.occupancy('GAP', 25, 40), 2)

    def test_front_and_back_half_reuse_then_capacity_blocks(self):
        times = [
            ['T', '09:00:00', '09:00:00', 'A', 1],
            ['T', '09:30:00', '09:30:00', 'B', 2],
            ['T', '10:00:00', '10:00:00', 'C', 3],
        ]
        self.build(['A', 'B', 'C'], ['T'], times, [('T', 2)])
        back = booking_create(self.db, request('BACK', 'B', 'C'))
        self.assertTrue(back['accepted'])
        front = booking_create(self.db, request('FRONT', 'A', 'B'))
        self.assertTrue(front['accepted'])
        self.assertEqual(self.occupancy('T', 1, 2), 2)
        self.assertEqual(self.occupancy('T', 2, 3), 2)
        # A third group cannot fit either segment; whole-leg trip also fails.
        self.assertFalse(booking_create(
            self.db, request('X1', 'A', 'B'))['accepted'])
        self.assertFalse(booking_create(
            self.db, request('X2', 'B', 'C'))['accepted'])
        self.assertFalse(booking_create(
            self.db, request('X3', 'A', 'C'))['accepted'])

    def test_party_is_never_split_across_routes(self):
        times = [
            ['T1', '09:00:00', '09:00:00', 'A', 1],
            ['T1', '09:30:00', '09:30:00', 'B', 2],
            ['T2', '09:40:00', '09:40:00', 'B', 1],
            ['T2', '10:10:00', '10:10:00', 'C', 2],
        ]
        self.build(['A', 'B', 'C'], ['T1', 'T2'], times,
                   [('T1', 3), ('T2', 1)])
        result = booking_create(self.db, request('BIG', 'A', 'C',
                                                 party_size=2))
        self.assertFalse(result['accepted'])

    def test_exact_capacity_boundary_accepted(self):
        times = [
            ['T', '09:00:00', '09:00:00', 'A', 1],
            ['T', '09:30:00', '09:30:00', 'B', 2],
        ]
        self.build(['A', 'B'], ['T'], times, [('T', 3)])
        ok = booking_create(self.db, request('FULL', 'A', 'B', party_size=3))
        self.assertTrue(ok['accepted'])
        blocked = booking_create(
            self.db, request('ONE', 'A', 'B', party_size=1))
        self.assertFalse(blocked['accepted'])

    def test_zero_seat_trip_never_bookable(self):
        times = [
            ['T', '09:00:00', '09:00:00', 'A', 1],
            ['T', '09:30:00', '09:30:00', 'B', 2],
        ]
        self.build(['A', 'B'], ['T'], times, [('T', 0)])
        self.assertFalse(booking_create(
            self.db, request('Z', 'A', 'B', party_size=1))['accepted'])

    def test_inclusive_time_and_transfer_boundaries(self):
        times = [
            ['T1', '09:00:00', '09:00:00', 'A', 1],
            ['T1', '09:30:00', '09:30:00', 'X', 2],
            ['T2', '09:35:00', '09:35:00', 'X', 1],
            ['T2', '10:00:00', '10:00:00', 'B', 2],
        ]
        self.build(['A', 'X', 'B'], ['T1', 'T2'], times,
                   [('T1', 5), ('T2', 5)])
        ok = booking_create(self.db, request(
            'EDGE', 'A', 'B', earliest='09:00:00',
            latest_arrival='10:00:00', min_transfer_seconds=300))
        self.assertTrue(ok['accepted'])
        # One extra required second breaks the transfer; one less arrival
        # second breaks the latest_arrival boundary.
        self.assertFalse(booking_create(self.db, request(
            'TIGHT', 'A', 'B', min_transfer_seconds=301))['accepted'])
        self.assertFalse(booking_create(self.db, request(
            'LATE', 'A', 'B', latest_arrival='09:59:59'))['accepted'])

    def test_tie_break_lexicographic_then_leg_count(self):
        # Both direct trips arrive at the same time; trip id order decides.
        times = [
            ['AAA', '09:00:00', '09:00:00', 'A', 1],
            ['AAA', '10:00:00', '10:00:00', 'B', 2],
            ['BBB', '09:00:00', '09:00:00', 'A', 1],
            ['BBB', '10:00:00', '10:00:00', 'B', 2],
        ]
        self.build(['A', 'B'], ['AAA', 'BBB'], times,
                   [('AAA', 5), ('BBB', 5)])
        result = booking_create(self.db, request('TIE', 'A', 'B'))
        self.assertEqual(result['legs'][0]['trip_id'], 'AAA')

        # Fewer legs wins even when the longer journey arrives just as early.
        times2 = [
            ['DIR', '09:00:00', '09:00:00', 'A', 1],
            ['DIR', '10:00:00', '10:00:00', 'B', 2],
            ['S1', '09:00:00', '09:00:00', 'A', 1],
            ['S1', '09:30:00', '09:30:00', 'X', 2],
            ['S2', '09:35:00', '09:35:00', 'X', 1],
            ['S2', '10:00:00', '10:00:00', 'B', 2],
        ]
        root2 = self.root / 'feed2'
        make_feed(root2, ['A', 'X', 'B'], ['DIR', 'S1', 'S2'], times2)
        base2 = self.root / 'base2.sqlite'
        import_feed(root2, base2)
        cap2 = self.root / 'cap2.csv'
        write_capacities(cap2, [['DIR', 5], ['S1', 5], ['S2', 5]])
        db2 = self.root / 'wb2.sqlite'
        booking_init(base2, cap2, db2)
        result = booking_create(db2, request('FEW', 'A', 'B'))
        self.assertEqual([l['trip_id'] for l in result['legs']], ['DIR'])

    def test_capacity_reroutes_to_second_best_journey(self):
        times = [
            ['FAST', '09:00:00', '09:00:00', 'A', 1],
            ['FAST', '09:45:00', '09:45:00', 'B', 2],
            ['SLOW', '09:00:00', '09:00:00', 'A', 1],
            ['SLOW', '10:30:00', '10:30:00', 'B', 2],
        ]
        self.build(['A', 'B'], ['FAST', 'SLOW'], times,
                   [('FAST', 2), ('SLOW', 5)])
        first = booking_create(self.db, request('F', 'A', 'B'))
        self.assertEqual(first['legs'][0]['trip_id'], 'FAST')
        second = booking_create(self.db, request('S', 'A', 'B'))
        self.assertEqual(second['legs'][0]['trip_id'], 'SLOW')
        self.assertEqual(second['arrival'], '10:30:00')

    def test_same_trip_different_service_dates_are_isolated(self):
        times = [
            ['T', '09:00:00', '09:00:00', 'A', 1],
            ['T', '09:30:00', '09:30:00', 'B', 2],
        ]
        self.build(['A', 'B'], ['T'], times, [('T', 2)])
        day1 = booking_create(self.db, request(
            'D1', 'A', 'B', service_date='2026-11-20'))
        day2 = booking_create(self.db, request(
            'D2', 'A', 'B', service_date='2026-11-21'))
        self.assertTrue(day1['accepted'])
        self.assertTrue(day2['accepted'])
        self.assertEqual(self.occupancy('T', 1, 2, '2026-11-20'), 2)
        self.assertEqual(self.occupancy('T', 1, 2, '2026-11-21'), 2)
        third = booking_create(self.db, request(
            'D3', 'A', 'B', service_date='2026-11-21'))
        self.assertFalse(third['accepted'])

    def test_calendar_add_and_remove_exceptions(self):
        times = [
            ['T', '09:00:00', '09:00:00', 'A', 1],
            ['T', '09:30:00', '09:30:00', 'B', 2],
            ['E', '09:00:00', '09:00:00', 'A', 1],
            ['E', '09:30:00', '09:30:00', 'B', 2],
        ]
        exceptions = [
            ['WK', '20261120', '2'],
            ['EXTRA', '20261120', '1'],
        ]
        feed = self.root / 'feed'
        write_feed(feed, ['A', 'B'], [['T', 'WK'], ['E', 'EXTRA']],
                   ['WK', 'EXTRA'], exceptions, times)
        self.base = self.root / 'base.sqlite'
        import_feed(feed, self.base)
        cap = self.root / 'cap.csv'
        write_capacities(cap, [['T', 5], ['E', 5]])
        self.db = self.root / 'booking.sqlite'
        booking_init(self.base, cap, self.db)
        result = booking_create(self.db, request('X', 'A', 'B'))
        self.assertTrue(result['accepted'])
        self.assertEqual(result['legs'][0]['trip_id'], 'E')

    def test_after_midnight_time_belongs_to_service_day(self):
        times = [
            ['NIGHT', '24:30:00', '24:30:00', 'A', 1],
            ['NIGHT', '25:00:00', '25:00:00', 'B', 2],
        ]
        self.build(['A', 'B'], ['NIGHT'], times, [('NIGHT', 5)])
        result = booking_create(self.db, request(
            'N', 'A', 'B', earliest='00:00:00',
            latest_arrival='47:59:59', min_transfer_seconds=0))
        self.assertTrue(result['accepted'])
        self.assertEqual(result['arrival'], '25:00:00')
        self.assertEqual(result['legs'][0]['departure'], '24:30:00')

    def test_same_trip_cannot_be_used_twice_in_one_journey(self):
        # A trip looping back through the transfer stop cannot be reboarded.
        times = [
            ['LOOP', '09:00:00', '09:00:00', 'A', 1],
            ['LOOP', '09:20:00', '09:20:00', 'X', 2],
            ['LOOP', '09:40:00', '09:40:00', 'A', 3],
            ['LOOP', '10:00:00', '10:00:00', 'B', 4],
            ['OTHER', '09:25:00', '09:25:00', 'X', 1],
            ['OTHER', '09:55:00', '09:55:00', 'B', 2],
        ]
        self.build(['A', 'X', 'B'], ['LOOP', 'OTHER'], times,
                   [('LOOP', 5), ('OTHER', 5)])
        result = booking_create(self.db, request('R', 'A', 'B',
                                                 min_transfer_seconds=0))
        self.assertEqual([l['trip_id'] for l in result['legs']],
                         ['LOOP', 'OTHER'])

    def test_lexicographic_tie_with_different_trip_order(self):
        # Both two-leg journeys use trips {X1, X2}, arrive at Z at the same
        # time and meet at the same intermediate stop X in a different trip
        # order because trips revisit physical stops. The smaller leg tuple
        # (X1 first) must win regardless of enumeration order.
        times = [
            ['X1', '09:00:00', '09:00:00', 'A', 1],
            ['X1', '09:20:00', '09:20:00', 'X', 2],
            ['X1', '09:40:00', '09:40:00', 'A', 3],
            ['X2', '09:25:00', '09:25:00', 'X', 1],
            ['X2', '09:45:00', '09:45:00', 'A', 2],
            ['X2', '10:05:00', '10:05:00', 'X', 3],
            ['Y1', '10:15:00', '10:15:00', 'X', 1],
            ['Y1', '10:30:00', '10:30:00', 'Z', 2],
        ]
        feed = self.root / 'feedord'
        make_feed(feed, ['A', 'X', 'Z'], ['X1', 'X2', 'Y1'], times)
        base = self.root / 'baseord.sqlite'
        import_feed(feed, base)
        cap = self.root / 'capord.csv'
        write_capacities(cap, [['X1', 5], ['X2', 5], ['Y1', 5]])
        db = self.root / 'wbord.sqlite'
        booking_init(base, cap, db)
        result = booking_create(db, request(
            'ORD', 'A', 'Z', min_transfer_seconds=300))
        self.assertTrue(result['accepted'])
        self.assertEqual([l['trip_id'] for l in result['legs']],
                         ['X1', 'Y1'])

    def test_cancel_frees_segments_for_new_id_only(self):
        times = [
            ['T', '09:00:00', '09:00:00', 'A', 1],
            ['T', '09:30:00', '09:30:00', 'B', 2],
        ]
        self.build(['A', 'B'], ['T'], times, [('T', 2)])
        booking_create(self.db, request('OLD', 'A', 'B'))
        self.assertFalse(booking_create(
            self.db, request('WAIT', 'A', 'B'))['accepted'])
        booking_cancel(self.db, 'OLD')
        self.assertTrue(booking_create(
            self.db, request('NEW', 'A', 'B'))['accepted'])
        # OLD only replays; it never re-seats.
        replay = booking_create(self.db, request('OLD', 'A', 'B'))
        self.assertTrue(replay['accepted'])
        self.assertEqual(booking_show(self.db, 'OLD')['status'], 'cancelled')
        self.assertFalse(booking_create(
            self.db, request('THIRD', 'A', 'B'))['accepted'])


class RequestValidationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        feed = root / 'feed'
        make_feed(feed, ['A', 'B'], ['T'], [
            ['T', '09:00:00', '09:00:00', 'A', 1],
            ['T', '09:30:00', '09:30:00', 'B', 2],
        ])
        base = root / 'base.sqlite'
        import_feed(feed, base)
        cap = root / 'cap.csv'
        write_capacities(cap, [['T', 2]])
        self.db = root / 'booking.sqlite'
        booking_init(base, cap, self.db)
        self.good = request('OK', 'A', 'B')

    def assert_rejected(self, value):
        with self.assertRaises(ValueError):
            booking_create(self.db, value)

    def test_field_set_must_be_exact(self):
        missing = dict(self.good)
        del missing['party_size']
        self.assert_rejected(missing)
        extra = dict(self.good)
        extra['note'] = 'x'
        self.assert_rejected(extra)
        self.assert_rejected([])
        self.assert_rejected(None)

    def test_booking_id_rules(self):
        for bad in ['', ' x', 'x ', 'a\tb', 'a\x7fb', 'a\x01b']:
            self.assert_rejected(request(bad, 'A', 'B'))

    def test_stop_rules(self):
        self.assert_rejected(request('X', 'A', 'A'))
        self.assert_rejected(request('X', 'A', 'ZZZ'))
        self.assert_rejected(request('X', 1, 'B'))

    def test_numeric_fields_reject_bools_floats_and_strings(self):
        for field, low, high in [('party_size', 1, 10000),
                                 ('max_transfers', 0, 3),
                                 ('min_transfer_seconds', 0, 3600)]:
            for bad in (True, False, 1.0, '2', None):
                value = dict(self.good)
                value[field] = bad
                self.assert_rejected(value)
            below = dict(self.good)
            below[field] = low - 1
            self.assert_rejected(below)
            above = dict(self.good)
            above[field] = high + 1
            self.assert_rejected(above)

    def test_dates_and_times(self):
        self.assert_rejected(request('X', 'A', 'B', service_date='2026-1-20'))
        self.assert_rejected(request('X', 'A', 'B', service_date='20261120'))
        self.assert_rejected(request('X', 'A', 'B', earliest='9:00:00'))
        self.assert_rejected(request('X', 'A', 'B', earliest='48:00:00'))
        self.assert_rejected(request('X', 'A', 'B',
                                     earliest='10:00:00',
                                     latest_arrival='09:00:00'))


class InitValidationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.feed = self.root / 'feed'
        make_feed(self.feed, ['A', 'B'], ['T1', 'T2'], [
            ['T1', '09:00:00', '09:00:00', 'A', 1],
            ['T1', '09:30:00', '09:30:00', 'B', 2],
            ['T2', '10:00:00', '10:00:00', 'A', 1],
            ['T2', '10:30:00', '10:30:00', 'B', 2],
        ])
        self.base = self.root / 'base.sqlite'
        import_feed(self.feed, self.base)
        self.cap = self.root / 'cap.csv'
        write_capacities(self.cap, [['T1', 2], ['T2', '0007']])

    def test_bom_capacity_file_and_leading_zeros_accepted(self):
        target = self.root / 'workbench.sqlite'
        result = booking_init(self.base, self.cap, target)
        self.assertEqual(result, {'trips': 2, 'bookings': 0})

    def test_reject_revision_regional_and_booking_warehouses(self):
        revised = self.root / 'revised.sqlite'
        revision_init(self.base, revised)
        with self.assertRaises(ValueError):
            booking_init(revised, self.cap, self.root / 'x1.sqlite')

        # A regional warehouse needs a second distinct feed and a mapping.
        feed2 = self.root / 'feed2'
        make_feed(feed2, ['A', 'B'], ['T1'], [
            ['T1', '09:00:00', '09:00:00', 'A', 1],
            ['T1', '09:30:00', '09:30:00', 'B', 2],
        ])
        base2 = self.root / 'base2.sqlite'
        import_feed(feed2, base2)
        mapping = self.root / 'map.csv'
        mapping.write_text(
            'feed_key,stop_id,interchange_id\n'
            'F1,A,X\nF1,B,X\nF2,A,X\nF2,B,X\n', encoding='utf-8')
        regional = self.root / 'regional.sqlite'
        regional_init([('F1', self.base), ('F2', base2)], mapping, regional)
        with self.assertRaises(ValueError):
            booking_init(regional, self.cap, self.root / 'x2.sqlite')

        workbench = self.root / 'wb.sqlite'
        booking_init(self.base, self.cap, workbench)
        with self.assertRaises(ValueError):
            booking_init(workbench, self.cap, self.root / 'x3.sqlite')

    def test_capacity_csv_must_be_strict(self):
        cases = {
            'header.csv': 'id,seats\nT1,2\nT2,2\n',
            'missing.csv': 'trip_id,seats\nT1,2\n',
            'duplicate.csv': 'trip_id,seats\nT1,2\nT1,3\nT2,2\n',
            'unknown.csv': 'trip_id,seats\nT1,2\nT2,2\nNOPE,1\n',
            'negative.csv': 'trip_id,seats\nT1,-1\nT2,2\n',
            'float.csv': 'trip_id,seats\nT1,1.5\nT2,2\n',
            'big.csv': 'trip_id,seats\nT1,10001\nT2,2\n',
            'wide.csv': 'trip_id,seats,extra\nT1,2,1\nT2,2,1\n',
            'empty.csv': 'trip_id,seats\n',
        }
        for name, content in cases.items():
            path = self.root / name
            path.write_text(content, encoding='utf-8')
            with self.assertRaises(ValueError):
                booking_init(self.base, path, self.root / ('out-' + name))
            self.assertFalse((self.root / ('out-' + name)).exists())

    def test_non_utf8_capacity_file_rejected(self):
        path = self.root / 'latin.csv'
        path.write_bytes('trip_id,seats\nT1,2\nT2,\xff\n'.encode('latin-1'))
        with self.assertRaises(ValueError):
            booking_init(self.base, path, self.root / 'out.sqlite')

    def test_existing_target_and_symlink_preserved(self):
        marker = self.root / 'marker'
        marker.write_bytes(b'keep')
        with self.assertRaises(ValueError):
            booking_init(self.base, self.cap, marker)
        self.assertEqual(marker.read_bytes(), b'keep')

        target = self.root / 'target'
        target.write_bytes(b'original')
        link = self.root / 'link'
        link.symlink_to(target)
        with self.assertRaises(ValueError):
            booking_init(self.base, self.cap, link)
        self.assertEqual(target.read_bytes(), b'original')

    def test_workbench_independent_of_source_files(self):
        target = self.root / 'independent.sqlite'
        booking_init(self.base, self.cap, target)
        os.remove(self.base)
        os.remove(self.cap)
        receipt = booking_create(target, request('LONE', 'A', 'B'))
        self.assertTrue(receipt['accepted'])

    def test_missing_database_is_never_created(self):
        missing = self.root / 'nope' / 'wb.sqlite'
        with self.assertRaises((ValueError, OSError)):
            booking_init(self.root / 'missing-base.sqlite', self.cap, missing)
        self.assertFalse(missing.exists())


class CliTests(unittest.TestCase):
    def invoke(self, *args):
        env = dict(os.environ)
        env['PYTHONPATH'] = str(ROOT / 'src')
        return subprocess.run(
            [sys.executable, '-m', MODULE, *map(str, args)],
            capture_output=True, text=True, timeout=15, env=env)

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        feed = root / 'feed'
        make_feed(feed, ['A', 'B'], ['T'], [
            ['T', '09:00:00', '09:00:00', 'A', 1],
            ['T', '09:30:00', '09:30:00', 'B', 2],
        ])
        self.base = root / 'base.sqlite'
        import_feed(feed, self.base)
        self.cap = root / 'cap.csv'
        write_capacities(self.cap, [['T', 2]])
        self.db = root / 'wb.sqlite'
        self.request_file = root / 'request.json'

    def test_cli_lifecycle(self):
        init = self.invoke('booking-init', '--source-db', self.base,
                           '--capacities', self.cap, '--db', self.db)
        self.assertEqual(init.returncode, 0, init.stderr)
        self.assertEqual(json.loads(init.stdout),
                         {'trips': 1, 'bookings': 0})

        self.request_file.write_text(json.dumps(request('C1', 'A', 'B')),
                                     encoding='utf-8')
        create = self.invoke('booking-create', '--db', self.db,
                             '--file', self.request_file)
        self.assertEqual(create.returncode, 0, create.stderr)
        receipt = json.loads(create.stdout)
        self.assertTrue(receipt['accepted'])
        self.assertEqual(create.stdout.count('\n'), 1)

        show = self.invoke('booking-show', '--db', self.db,
                           '--booking', 'C1')
        self.assertEqual(show.returncode, 0, show.stderr)
        self.assertEqual(json.loads(show.stdout)['status'], 'active')

        cancel = self.invoke('booking-cancel', '--db', self.db,
                             '--booking', 'C1')
        self.assertEqual(json.loads(cancel.stdout),
                         {'booking_id': 'C1', 'cancelled': True})

        bad = self.invoke('booking-show', '--db', self.db,
                          '--booking', 'UNKNOWN')
        self.assertEqual(bad.returncode, 2)
        self.assertEqual(bad.stdout, '')
        self.assertIn('unknown booking_id', bad.stderr)

    def test_duplicate_json_keys_rejected(self):
        self.invoke('booking-init', '--source-db', self.base,
                    '--capacities', self.cap, '--db', self.db)
        good = request('C1', 'A', 'B')
        raw = json.dumps(good)[:-1] + ',"party_size":3}'
        self.request_file.write_text(raw, encoding='utf-8')
        result = self.invoke('booking-create', '--db', self.db,
                             '--file', self.request_file)
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, '')

    def test_state_survives_new_process(self):
        self.invoke('booking-init', '--source-db', self.base,
                    '--capacities', self.cap, '--db', self.db)
        self.request_file.write_text(json.dumps(request('C1', 'A', 'B')),
                                     encoding='utf-8')
        self.invoke('booking-create', '--db', self.db,
                    '--file', self.request_file)
        # A second process sees the committed identity, occupancy and replay.
        duplicate = self.invoke('booking-create', '--db', self.db,
                                '--file', self.request_file)
        self.assertEqual(duplicate.returncode, 0, duplicate.stderr)
        self.assertTrue(json.loads(duplicate.stdout)['accepted'])
        other = Path(self.tmp.name) / 'other.json'
        other.write_text(json.dumps(request('C2', 'A', 'B')),
                         encoding='utf-8')
        blocked = self.invoke('booking-create', '--db', self.db,
                              '--file', other)
        self.assertEqual(blocked.returncode, 0, blocked.stderr)
        self.assertFalse(json.loads(blocked.stdout)['accepted'])


if __name__ == '__main__':
    unittest.main()
