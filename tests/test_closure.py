import csv
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

from transit_feed.closure import stop_board
from transit_feed.feed import import_feed

ROOT = Path(__file__).resolve().parents[1]
MODULE = 'transit_feed'

CLOSURE_HEADER = ['closure_id', 'stop_id', 'from', 'to', 'reason']


def write_closures(path, rows, header=None, encoding='utf-8'):
    path = Path(path)
    with path.open('w', encoding=encoding, newline='') as stream:
        writer = csv.writer(stream)
        writer.writerow(header or CLOSURE_HEADER)
        writer.writerows(rows)
    return path


def write_feed(root, stops, trips, calendar, exceptions, times):
    root.mkdir(parents=True, exist_ok=True)
    tables = {
        'stops.txt': (['stop_id', 'stop_name'], stops),
        'trips.txt': (['trip_id', 'service_id'], trips),
        'calendar.txt': (['service_id', 'monday', 'tuesday', 'wednesday',
                          'thursday', 'friday', 'saturday', 'sunday',
                          'start_date', 'end_date'], calendar),
        'calendar_dates.txt': (['service_id', 'date', 'exception_type'],
                               exceptions),
        'stop_times.txt': (['trip_id', 'arrival_time', 'departure_time',
                            'stop_id', 'stop_sequence'], times),
    }
    for name, (header, rows) in tables.items():
        with (root / name).open('w', encoding='utf-8', newline='') as stream:
            writer = csv.writer(stream)
            writer.writerow(header)
            writer.writerows(rows)


class BoardTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.feed = self.root / 'feed'
        shutil.copytree(ROOT / 'fixtures/feed', self.feed)
        self.db = self.root / 'feed.sqlite'
        import_feed(self.feed, self.db)
        self.closures = write_closures(
            self.root / 'closures.csv',
            [('C1', 'B', '2026-01-06', '2026-01-07', 'Track works'),
             ('C2', 'B', '2026-01-07', '2026-01-08', 'Staff shortage')])

    def board(self, day, stop='B', after='00:00:00', closures=None):
        return stop_board(self.db, closures or self.closures, day, stop, after)

    def test_acceptance_overlapping_closures_across_days(self):
        first = self.board('2026-01-06')
        self.assertEqual(first['closure_ids'], ['C1'])
        self.assertEqual(first['closures'],
                         [{'closure_id': 'C1', 'reason': 'Track works'}])
        self.assertTrue(first['closed'])
        self.assertEqual([(row['trip_id'], row['stop_sequence'],
                           row['departure'], row['serviceable'],
                           row['closure_ids']) for row in first['departures']],
                         [('T1', 2, '24:12:00', False, ['C1'])])

        overlap = self.board('2026-01-07')
        self.assertEqual(overlap['closure_ids'], ['C1', 'C2'])
        self.assertEqual(overlap['closures'],
                         [{'closure_id': 'C1', 'reason': 'Track works'},
                          {'closure_id': 'C2', 'reason': 'Staff shortage'}])
        self.assertEqual(overlap['departures'][0]['closure_ids'], ['C1', 'C2'])

        second = self.board('2026-01-08')
        self.assertEqual(second['closure_ids'], ['C2'])
        self.assertEqual(second['closures'],
                         [{'closure_id': 'C2', 'reason': 'Staff shortage'}])

        reopened = self.board('2026-01-09')
        self.assertFalse(reopened['closed'])
        self.assertEqual(reopened['closure_ids'], [])
        self.assertEqual(reopened['closures'], [])
        self.assertEqual([(row['trip_id'], row['departure'], row['serviceable'],
                           row['closure_ids']) for row in reopened['departures']],
                         [('T1', '24:12:00', True, [])])

    def test_closing_one_stop_keeps_same_trip_elsewhere(self):
        before = self.board('2026-01-07', 'A')['departures']
        closed_b = self.board('2026-01-07', 'B')['departures']
        after = self.board('2026-01-07', 'A')['departures']
        self.assertEqual([(row['trip_id'], row['departure']) for row in before],
                         [('T1', '23:50:00')])
        self.assertEqual(before, after)
        self.assertFalse(closed_b[0]['serviceable'])
        self.assertEqual(len(closed_b), 1)

    def test_closed_day_without_departures_still_closed(self):
        # Sunday 2026-01-04: no weekday service, but the closure is reported.
        closures = write_closures(
            self.root / 'weekend.csv',
            [('C9', 'B', '2026-01-03', '2026-01-04', 'Weekend closure')])
        result = self.board('2026-01-04', closures=closures)
        self.assertTrue(result['closed'])
        self.assertEqual(result['closure_ids'], ['C9'])
        self.assertEqual(result['closures'],
                         [{'closure_id': 'C9', 'reason': 'Weekend closure'}])
        self.assertEqual(result['departures'], [])

    def test_calendar_exception_decides_visits_before_closure(self):
        # Jan 5 removes WK and adds SPECIAL; T2 at B is planned and closed.
        closures = write_closures(
            self.root / 'special.csv',
            [('C1', 'B', '2026-01-05', '2026-01-05', 'Holiday works')])
        result = self.board('2026-01-05', closures=closures)
        self.assertEqual([(row['trip_id'], row['departure'], row['serviceable'])
                          for row in result['departures']],
                         [('T2', '08:25:00', False)])
        # Jan 4 has no service at all, so there is nothing to mark.
        self.assertEqual(self.board('2026-01-04', closures=closures)['departures'],
                         [])

    def test_after_is_inclusive_and_keeps_24h_day(self):
        rows = self.board('2026-01-06', after='24:12:00')['departures']
        self.assertEqual([(row['trip_id'], row['departure']) for row in rows],
                         [('T1', '24:12:00')])
        self.assertEqual(self.board('2026-01-06', after='24:12:01')['departures'],
                         [])
        self.assertEqual(rows[0]['service_date'], '2026-01-06')

    def test_closure_range_endpoints_are_inclusive(self):
        closures = write_closures(
            self.root / 'endpoints.csv',
            [('E', 'B', '2026-01-06', '2026-01-06', 'One day')])
        self.assertEqual(self.board('2026-01-05', closures=closures)['closure_ids'], [])
        self.assertEqual(self.board('2026-01-06', closures=closures)['closure_ids'], ['E'])
        self.assertEqual(self.board('2026-01-07', closures=closures)['closure_ids'], [])

    def test_multi_year_range_is_not_expanded(self):
        long_range = write_closures(
            self.root / 'long.csv',
            [('L', 'B', '2020-02-29', '2035-12-31', 'Long rebuild')])
        result = self.board('2026-01-07', closures=long_range)
        self.assertEqual(result['closure_ids'], ['L'])
        self.assertTrue(result['closed'])
        self.assertEqual(self.board('2036-01-01', closures=long_range)['closure_ids'], [])

    def test_codepoint_order_of_overlapping_ids(self):
        closures = write_closures(
            self.root / 'order.csv',
            [('b', 'B', '2026-01-07', '2026-01-07', 'lower'),
             ('A', 'B', '2026-01-07', '2026-01-07', 'upper'),
             ('c', 'B', '2026-01-07', '2026-01-07', 'last')])
        result = self.board('2026-01-07', closures=closures)
        self.assertEqual(result['closure_ids'], ['A', 'b', 'c'])
        self.assertEqual([row['closure_id'] for row in result['closures']],
                         ['A', 'b', 'c'])
        self.assertEqual(result['departures'][0]['closure_ids'], ['A', 'b', 'c'])

    def test_closure_file_update_is_seen_by_next_query(self):
        self.assertEqual(self.board('2026-01-09')['closed'], False)
        write_closures(
            self.root / 'closures.csv',
            [('C3', 'B', '2026-01-09', '2026-01-09', 'New closure')])
        self.assertEqual(self.board('2026-01-09')['closure_ids'], ['C3'])
        self.assertTrue(self.board('2026-01-09')['closed'])

    def test_other_stops_closure_does_not_affect_board(self):
        closures = write_closures(
            self.root / 'other.csv',
            [('C1', 'A', '2026-01-06', '2026-01-08', 'Elsewhere')])
        result = self.board('2026-01-07', closures=closures)
        self.assertFalse(result['closed'])
        self.assertEqual(result['closure_ids'], [])
        self.assertTrue(result['departures'][0]['serviceable'])

    def test_output_shape(self):
        result = self.board('2026-01-06')
        self.assertEqual(set(result), {'service_date', 'stop_id', 'closed',
                                       'closure_ids', 'closures', 'departures'})
        self.assertEqual(set(result['departures'][0]),
                         {'trip_id', 'stop_sequence', 'departure',
                          'service_date', 'serviceable', 'closure_ids'})


class RepeatedVisitTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        feed = self.root / 'feed'
        write_feed(
            feed,
            [('A', 'Alpha'), ('B', 'Loop'), ('C', 'End')],
            [('L', 'WK')],
            [('WK', '1', '1', '1', '1', '1', '1', '1', '20260101', '20261231')],
            [],
            [('L', '08:00:00', '08:00:00', 'A', 1),
             ('L', '08:10:00', '08:11:00', 'B', 2),
             ('L', '08:20:00', '08:21:00', 'C', 3),
             ('L', '08:30:00', '08:31:00', 'B', 4),
             ('L', '08:40:00', '08:40:00', 'C', 5)])
        self.db = self.root / 'loop.sqlite'
        import_feed(feed, self.db)
        self.closures = write_closures(
            self.root / 'closures.csv',
            [('C1', 'B', '2026-01-06', '2026-01-06', 'Loop closed')])

    def test_repeated_visits_remain_separate_records(self):
        result = stop_board(self.db, self.closures, '2026-01-06', 'B')
        self.assertEqual([(row['trip_id'], row['stop_sequence'], row['departure'],
                           row['serviceable']) for row in result['departures']],
                         [('L', 2, '08:11:00', False),
                          ('L', 4, '08:31:00', False)])
        other = stop_board(self.db, self.closures, '2026-01-06', 'C')
        self.assertEqual([(row['stop_sequence'], row['serviceable'])
                          for row in other['departures']],
                         [(3, True), (5, True)])


class ValidationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.feed = self.root / 'feed'
        shutil.copytree(ROOT / 'fixtures/feed', self.feed)
        self.db = self.root / 'feed.sqlite'
        import_feed(self.feed, self.db)
        self.good = [('C1', 'B', '2026-01-06', '2026-01-07', 'Works')]

    def query(self, path, day='2026-01-07', stop='B'):
        return stop_board(self.db, path, day, stop)

    def test_bad_encoding_rejected(self):
        path = self.root / 'latin.csv'
        path.write_bytes(
            'closure_id,stop_id,from,to,reason\nC1,B,2026-01-06,2026-01-07,caf\xe9\n'
            .encode('latin-1'))
        with self.assertRaisesRegex(ValueError, 'UTF-8'):
            self.query(path)

    def test_wrong_header_rejected(self):
        path = write_closures(self.root / 'header.csv', self.good,
                              header=['stop_id', 'closure_id', 'from', 'to',
                                      'reason'])
        with self.assertRaisesRegex(ValueError, 'columns'):
            self.query(path)

    def test_missing_and_extra_columns_rejected(self):
        short = write_closures(self.root / 'short.csv',
                               [('C1', 'B', '2026-01-06', '2026-01-07')])
        with self.assertRaisesRegex(ValueError, 'malformed'):
            self.query(short)
        long = write_closures(
            self.root / 'long.csv',
            [('C1', 'B', '2026-01-06', '2026-01-07', 'Works', 'extra')])
        with self.assertRaisesRegex(ValueError, 'malformed'):
            self.query(long)

    def test_duplicate_id_rejected_even_with_different_dates(self):
        path = write_closures(
            self.root / 'dupe.csv',
            [('C1', 'B', '2026-01-06', '2026-01-06', 'One'),
             ('C1', 'B', '2026-01-07', '2026-01-07', 'Two')])
        with self.assertRaisesRegex(ValueError, 'duplicate closure_id'):
            self.query(path)

    def test_unknown_stop_rejected(self):
        path = write_closures(
            self.root / 'unknown.csv',
            [('C1', 'Z', '2026-01-06', '2026-01-07', 'Works')])
        with self.assertRaisesRegex(ValueError, 'unknown stop'):
            self.query(path)

    def test_noncanonical_and_inverted_dates_rejected(self):
        cases = {
            'slashes.csv': [('C1', 'B', '2026/01/06', '2026-01-07', 'Works')],
            'padded.csv': [('C1', 'B', '2026-1-6', '2026-01-07', 'Works')],
            'inverted.csv': [('C1', 'B', '2026-01-07', '2026-01-06', 'Works')],
            'empty.csv': [('C1', 'B', '', '2026-01-07', 'Works')],
        }
        for name, rows in cases.items():
            with self.subTest(name=name):
                with self.assertRaises(ValueError):
                    self.query(write_closures(self.root / name, rows))

    def test_empty_identifier_or_reason_rejected(self):
        cases = {
            'empty_id.csv': [('', 'B', '2026-01-06', '2026-01-07', 'Works')],
            'padded_id.csv': [(' C1', 'B', '2026-01-06', '2026-01-07', 'Works')],
            'empty_stop.csv': [('C1', '', '2026-01-06', '2026-01-07', 'Works')],
            'empty_reason.csv': [('C1', 'B', '2026-01-06', '2026-01-07', '')],
            'blank_reason.csv': [('C1', 'B', '2026-01-06', '2026-01-07', '   ')],
        }
        for name, rows in cases.items():
            with self.subTest(name=name):
                with self.assertRaises(ValueError):
                    self.query(write_closures(self.root / name, rows))

    def test_missing_file_and_missing_database(self):
        with self.assertRaises(ValueError):
            self.query(self.root / 'absent.csv')
        with self.assertRaises(ValueError):
            stop_board(self.root / 'absent.sqlite',
                       write_closures(self.root / 'ok.csv', self.good),
                       '2026-01-07', 'B')

    def test_corrupt_database_rejected(self):
        broken = self.root / 'broken.sqlite'
        broken.write_bytes(b'not a database')
        with self.assertRaises(ValueError):
            stop_board(broken, write_closures(self.root / 'ok.csv', self.good),
                       '2026-01-07', 'B')

    def test_invalid_day_stop_and_after(self):
        path = write_closures(self.root / 'ok.csv', self.good)
        for day in ('2026-1-7', '20260107', '2026-01-07T00:00:00', '', None):
            with self.subTest(day=day), self.assertRaises(ValueError):
                stop_board(self.db, path, day, 'B')
        for after in ('9:00:00', '24:60:00', '48:00:00', '00:00', 0, None):
            with self.subTest(after=after), self.assertRaises(ValueError):
                stop_board(self.db, path, '2026-01-07', 'B', after)
        with self.assertRaisesRegex(ValueError, 'unknown stop'):
            stop_board(self.db, path, '2026-01-07', 'Z')

    def test_failed_queries_do_not_change_archive_or_closure_file(self):
        path = write_closures(self.root / 'ok.csv', self.good)
        digest = hashlib.sha256(self.db.read_bytes()).hexdigest()
        closures_digest = hashlib.sha256(path.read_bytes()).hexdigest()
        bad = write_closures(
            self.root / 'bad.csv',
            [('C1', 'B', '2026-01-06', '2026-01-07', 'Works'),
             ('C1', 'B', '2026-01-06', '2026-01-07', 'Duplicate')])
        attempts = [
            lambda: stop_board(self.db, bad, '2026-01-07', 'B'),
            lambda: stop_board(self.db, path, '2026-1-7', 'B'),
            lambda: stop_board(self.db, path, '2026-01-07', 'Z'),
            lambda: stop_board(self.db, path, '2026-01-07', 'B', '9:00:00'),
        ]
        for attempt in attempts:
            with self.assertRaises(ValueError):
                attempt()
        self.assertEqual(hashlib.sha256(self.db.read_bytes()).hexdigest(), digest)
        self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(),
                         closures_digest)


class CliTests(unittest.TestCase):
    def invoke(self, *args):
        return subprocess.run(
            [sys.executable, '-m', MODULE, *map(str, args)],
            capture_output=True, text=True, timeout=30, cwd=str(ROOT),
            env={**os.environ, 'PYTHONPATH': str(ROOT / 'src')})

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.db = self.root / 'feed.sqlite'
        result = self.invoke('import', '--source', ROOT / 'fixtures/feed',
                             '--db', self.db)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.closures = write_closures(
            self.root / 'closures.csv',
            [('C1', 'B', '2026-01-06', '2026-01-07', 'Track works'),
             ('C2', 'B', '2026-01-07', '2026-01-08', 'Staff shortage')])

    def test_lifecycle_json(self):
        result = self.invoke('stop-board', '--db', self.db,
                             '--closures', self.closures,
                             '--date', '2026-01-07', '--stop', 'B')
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertTrue(payload['closed'])
        self.assertEqual(payload['closure_ids'], ['C1', 'C2'])
        self.assertEqual([(row['trip_id'], row['departure'],
                           row['serviceable']) for row in payload['departures']],
                         [('T1', '24:12:00', False)])
        after = self.invoke('stop-board', '--db', self.db,
                            '--closures', self.closures,
                            '--date', '2026-01-06', '--stop', 'B',
                            '--after', '24:12:00')
        self.assertEqual(after.returncode, 0, after.stderr)
        self.assertEqual(json.loads(after.stdout)['departures'][0]['departure'],
                         '24:12:00')

    def test_query_errors_exit_two_and_leave_archive(self):
        digest = hashlib.sha256(self.db.read_bytes()).hexdigest()
        bad = write_closures(self.root / 'bad.csv',
                             [('C1', 'Z', '2026-01-06', '2026-01-07', 'Works')])
        cases = [
            ['--db', self.db, '--closures', self.closures,
             '--date', '2026-1-7', '--stop', 'B'],
            ['--db', self.db, '--closures', self.closures,
             '--date', '2026-01-07', '--stop', 'Z'],
            ['--db', self.db, '--closures', self.closures,
             '--date', '2026-01-07', '--stop', 'B', '--after', '9:00:00'],
            ['--db', self.db, '--closures', bad,
             '--date', '2026-01-07', '--stop', 'B'],
            ['--db', self.db, '--closures', self.root / 'missing.csv',
             '--date', '2026-01-07', '--stop', 'B'],
        ]
        for extra in cases:
            with self.subTest(extra=extra):
                result = self.invoke('stop-board', *extra)
                self.assertEqual(result.returncode, 2)
                self.assertTrue(result.stderr)
                self.assertEqual(result.stdout, '')
        self.assertEqual(hashlib.sha256(self.db.read_bytes()).hexdigest(), digest)

    def test_missing_database_is_not_created(self):
        missing = self.root / 'absent.sqlite'
        result = self.invoke('stop-board', '--db', missing,
                             '--closures', self.closures,
                             '--date', '2026-01-07', '--stop', 'B')
        self.assertEqual(result.returncode, 2)
        self.assertFalse(missing.exists())


if __name__ == '__main__':
    unittest.main()
