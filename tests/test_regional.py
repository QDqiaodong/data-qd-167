import csv
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest

from transit_feed.feed import import_feed
from transit_feed.regional import regional_departures, regional_init
from transit_feed.revision import revision_init

ROOT = Path(__file__).resolve().parents[1]
MODULE = 'transit_feed'

CALENDAR = ['service_id', 'monday', 'tuesday', 'wednesday', 'thursday', 'friday',
            'saturday', 'sunday', 'start_date', 'end_date']
EXCEPTIONS = ['service_id', 'date', 'exception_type']
TIMES = ['trip_id', 'arrival_time', 'departure_time', 'stop_id', 'stop_sequence']
MAPPING = ['feed_key', 'stop_id', 'interchange_id']


def write_feed(root, stops, trips, calendar, exceptions, times):
    root.mkdir(parents=True, exist_ok=True)

    def table(name, header, rows):
        with (root / name).open('w', encoding='utf-8', newline='') as stream:
            writer = csv.writer(stream)
            writer.writerow(header)
            writer.writerows(rows)

    table('stops.txt', ['stop_id', 'stop_name'], stops)
    table('trips.txt', ['trip_id', 'service_id'], trips)
    table('calendar.txt', CALENDAR, calendar)
    table('calendar_dates.txt', EXCEPTIONS, exceptions)
    table('stop_times.txt', TIMES, times)


def write_mapping(path, rows):
    with Path(path).open('w', encoding='utf-8', newline='') as stream:
        writer = csv.writer(stream)
        writer.writerow(MAPPING)
        writer.writerows(rows)
    return Path(path)


def builder(root):
    """Publisher A and B sharing local names S/T/WK but with own calendars."""
    a = root / 'feed_a'
    write_feed(
        a,
        [('S', 'Alpha South'), ('P', 'Alpha Park'), ('U', 'Alpha Depot')],
        [('T', 'WK'), ('R', 'WK'), ('N', 'WK')],
        [('WK', '1', '1', '1', '1', '1', '1', '1', '20260101', '20261231')],
        [('WK', '20260105', '2')],
        [('T', '08:30:00', '08:30:00', 'S', 1),
         ('T', '08:45:00', '08:45:00', 'P', 2),
         ('R', '10:00:00', '10:00:00', 'S', 1),
         ('R', '10:10:00', '10:10:00', 'P', 2),
         ('R', '10:20:00', '10:20:00', 'S', 3),
         ('N', '23:55:00', '23:55:00', 'P', 1),
         ('N', '24:12:00', '24:12:00', 'S', 2)])

    b = root / 'feed_b'
    write_feed(
        b,
        [('S', 'Beta South'), ('Q', 'Beta Quay')],
        [('T', 'WK')],
        [('WK', '1', '1', '1', '1', '1', '1', '1', '20260101', '20260104')],
        [('WK', '20260105', '1')],
        [('T', '09:00:00', '09:00:00', 'S', 1),
         ('T', '09:20:00', '09:20:00', 'Q', 2)])

    return a, b


class BaseCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.feed_a, self.feed_b = builder(self.root)
        self.a_db = self.root / 'a.sqlite'
        self.b_db = self.root / 'b.sqlite'
        import_feed(self.feed_a, self.a_db)
        import_feed(self.feed_b, self.b_db)
        self.mapping = self.standard_mapping()
        self.db = self.root / 'regional.sqlite'

    def standard_mapping(self):
        return write_mapping(self.root / 'map.csv',
                             [('A', 'S', 'X'), ('A', 'P', 'Y'), ('B', 'S', 'X')])

    def init(self, mapping=None, db=None, feeds=None):
        return regional_init(
            feeds if feeds is not None else {'A': self.a_db, 'B': self.b_db},
            mapping or self.mapping,
            db or self.db)

    def query(self, day='2026-01-02', interchange='X', after='00:00:00'):
        return regional_departures(self.db, day, interchange, after)

    def rows(self, day='2026-01-02', interchange='X', after='00:00:00'):
        return self.query(day, interchange, after)['departures']

    def triple(self, row):
        return (row['feed_key'], row['trip_id'], row['stop_sequence'])


class InitTests(BaseCase):
    def test_feed_days_are_evaluated_per_publisher(self):
        self.init()
        # A removes WK on Jan 5; B adds WK on the same day.  Only B's T runs.
        rows = self.rows('2026-01-05', 'X')
        self.assertEqual([self.triple(row) for row in rows], [('B', 'T', 1)])
        self.assertEqual(rows[0]['departure'], '09:00:00')
        # On an ordinary day both publishers run their identically named T.
        names = [(row['feed_key'], row['trip_id']) for row in self.rows('2026-01-02', 'X')]
        self.assertIn(('A', 'T'), names)
        self.assertIn(('B', 'T'), names)

    def test_merging_service_calendars_would_be_wrong(self):
        # Sanity check for the fixture: A is explicitly removed and B added.
        self.init()
        rows = self.rows('2026-01-05', 'X')
        self.assertFalse(any(row['feed_key'] == 'A' for row in rows))
        self.assertEqual(len([row for row in rows if row['feed_key'] == 'B']), 1)

    def test_init_result_and_copy_independence(self):
        before = (self.a_db.read_bytes(), self.b_db.read_bytes(),
                  self.mapping.read_bytes())
        result = self.init()
        self.assertEqual(result, {'feeds': 2, 'interchanges': 2, 'mappings': 3})
        self.assertEqual((self.a_db.read_bytes(), self.b_db.read_bytes(),
                          self.mapping.read_bytes()), before)
        # Sources and the mapping may vanish; the warehouse still answers.
        self.a_db.unlink()
        self.b_db.unlink()
        self.mapping.unlink()
        self.assertEqual([self.triple(row) for row in self.rows('2026-01-05', 'X')],
                         [('B', 'T', 1)])

    def test_repeated_stop_visits_of_one_trip_are_separate(self):
        self.init()
        visits = [(row['trip_id'], row['stop_sequence'], row['departure'])
                  for row in self.rows('2026-01-02', 'X') if row['trip_id'] == 'R']
        self.assertEqual(visits, [('R', 1, '10:00:00'), ('R', 3, '10:20:00')])

    def test_two_stops_of_one_publisher_share_an_interchange(self):
        mapping = write_mapping(self.root / 'same_source.csv',
                                [('A', 'S', 'M'), ('A', 'P', 'M'),
                                 ('B', 'S', 'M')])
        self.init(mapping)
        rows = self.rows('2026-01-02', 'M')
        self.assertEqual(
            [(row['feed_key'], row['trip_id'], row['stop_sequence']) for row in rows],
            [('A', 'T', 1), ('A', 'T', 2), ('B', 'T', 1), ('A', 'R', 1),
             ('A', 'R', 2), ('A', 'R', 3), ('A', 'N', 1), ('A', 'N', 2)])

    def test_unmapped_stops_are_excluded(self):
        self.init()
        y = [(row['trip_id'], row['stop_sequence']) for row in self.rows('2026-01-02', 'Y')]
        self.assertEqual(y, [('T', 2), ('R', 2), ('N', 1)])

    def test_identically_named_stops_stay_with_their_publisher(self):
        mapping = write_mapping(self.root / 'split.csv',
                                [('A', 'S', 'ONLY_A'), ('B', 'S', 'ONLY_B')])
        self.init(mapping)
        only_a = self.rows('2026-01-02', 'ONLY_A')
        only_b = self.rows('2026-01-02', 'ONLY_B')
        self.assertTrue(only_a and all(row['feed_key'] == 'A' for row in only_a))
        self.assertTrue(only_b and all(row['feed_key'] == 'B' for row in only_b))

    def test_unknown_stop_mapping_rejects_everything(self):
        mapping = write_mapping(self.root / 'bad_stop.csv',
                                [('A', 'S', 'X'), ('B', 'Z', 'X')])
        target = self.root / 'out.sqlite'
        with self.assertRaisesRegex(ValueError, 'unknown stop'):
            self.init(mapping, target)
        self.assertFalse(target.exists())
        self.assertEqual(list(self.root.glob('out.sqlite*')), [])

    def test_unknown_feed_mapping_rejects_everything(self):
        mapping = write_mapping(self.root / 'bad_feed.csv', [('C', 'S', 'X')])
        with self.assertRaisesRegex(ValueError, 'unknown feed'):
            self.init(mapping)
        self.assertFalse(self.db.exists())

    def test_duplicate_and_malformed_mappings_rejected(self):
        cases = {
            'duplicate.csv': [('A', 'S', 'X'), ('A', 'S', 'Y')],
            'empty_field.csv': [('A', '', 'X')],
            'padded.csv': [(' A', 'S', 'X')],
            'extra_column.csv': [('A', 'S', 'X', 'junk')],
        }
        for name, rows in cases.items():
            with self.subTest(name=name):
                mapping = write_mapping(self.root / name, rows)
                with self.assertRaises(ValueError):
                    self.init(mapping)
                self.assertFalse(self.db.exists())
                self.assertEqual(list(self.root.glob('regional.sqlite*')), [])

    def test_bad_mapping_header_rejected(self):
        path = self.root / 'header.csv'
        path.write_text('feed_key,interchange_id,stop_id\nA,X,S\n', encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'columns'):
            self.init(path)
        self.assertFalse(self.db.exists())

    def test_missing_or_non_utf8_mapping_rejected(self):
        with self.assertRaises(ValueError):
            self.init(self.root / 'missing.csv')
        path = self.root / 'latin.csv'
        path.write_bytes(b'feed_key,stop_id,interchange_id\nA,S,\xff\n')
        with self.assertRaises(ValueError):
            self.init(path)
        self.assertFalse(self.db.exists())

    def test_feed_arguments_are_validated(self):
        invalid = [
            {'A': self.a_db},
            {},
            {'A': self.a_db, 'B': self.b_db, ' C': self.b_db},
            {'A': self.a_db, 'B': str(self.root / 'missing.sqlite')},
            {'A': self.a_db, 'A2': self.a_db},
        ]
        for feeds in invalid:
            with self.subTest(feeds=feeds), self.assertRaises(ValueError):
                self.init(feeds=feeds)
            self.assertFalse(self.db.exists())

    def test_nine_feeds_rejected(self):
        feeds = {}
        for index in range(9):
            path = self.root / f'copy{index}.sqlite'
            shutil.copyfile(self.a_db, path)
            feeds[f'K{index}'] = path
        with self.assertRaises(ValueError):
            self.init(feeds=feeds)
        self.assertFalse(self.db.exists())

    def test_feeds_may_be_key_equals_path_sequence(self):
        self.init(feeds=['A=' + str(self.a_db), 'B=' + str(self.b_db)])
        self.assertEqual([self.triple(row) for row in self.rows('2026-01-05', 'X')],
                         [('B', 'T', 1)])

    def test_existing_target_is_preserved(self):
        self.db.write_bytes(b'old archive')
        with self.assertRaisesRegex(ValueError, 'must not exist'):
            self.init()
        self.assertEqual(self.db.read_bytes(), b'old archive')

    def test_symlink_target_is_preserved(self):
        real = self.root / 'real.sqlite'
        real.write_bytes(b'old archive')
        self.db.symlink_to(real)
        with self.assertRaisesRegex(ValueError, 'must not exist'):
            self.init()
        self.assertTrue(self.db.is_symlink())
        self.assertEqual(real.read_bytes(), b'old archive')

    def test_corrupt_feed_leaves_no_target_or_temporary(self):
        broken = self.root / 'broken.sqlite'
        broken.write_bytes(b'not a database')
        with self.assertRaises(ValueError):
            self.init(feeds={'A': self.a_db, 'B': broken})
        self.assertFalse(self.db.exists())
        self.assertEqual(sorted(self.root.glob('*.sqlite*')),
                         sorted([self.a_db, self.b_db, broken]))

    def test_revision_archive_is_not_a_baseline_feed(self):
        revision = self.root / 'revision.sqlite'
        revision_init(self.a_db, revision)
        with self.assertRaises(ValueError):
            self.init(feeds={'A': revision, 'B': self.b_db})
        self.assertFalse(self.db.exists())

    def test_failure_keeps_no_created_parent_directory(self):
        parent = self.root / 'new' / 'nested'
        target = parent / 'regional.sqlite'
        mapping = write_mapping(self.root / 'bad_for_parent.csv',
                                [('A', 'Z', 'X')])
        with self.assertRaises(ValueError):
            self.init(mapping, target)
        self.assertFalse((self.root / 'new').exists())


class QueryTests(BaseCase):
    def setUp(self):
        super().setUp()
        self.init()

    def test_output_shape_and_identity_fields(self):
        result = self.query('2026-01-05', 'X')
        self.assertEqual(set(result), {'service_date', 'interchange_id', 'departures'})
        self.assertEqual((result['service_date'], result['interchange_id']),
                         ('2026-01-05', 'X'))
        self.assertEqual(set(result['departures'][0]),
                         {'feed_key', 'trip_id', 'stop_sequence', 'departure',
                          'service_date'})

    def test_stable_ordering_by_departure_feed_trip_sequence(self):
        rows = self.rows('2026-01-02', 'X')
        ordered = [(row['departure'], row['feed_key'], row['trip_id'],
                    row['stop_sequence']) for row in rows]
        self.assertEqual(ordered, [
            ('08:30:00', 'A', 'T', 1),
            ('09:00:00', 'B', 'T', 1),
            ('10:00:00', 'A', 'R', 1),
            ('10:20:00', 'A', 'R', 3),
            ('24:12:00', 'A', 'N', 2),
        ])

    def test_equal_departures_tie_break_on_feed_key(self):
        z = self.root / 'feed_z'
        write_feed(z,
                   [('S', 'Zulu South'), ('P', 'Zulu Park')],
                   [('T', 'WK')],
                   [('WK', '1', '1', '1', '1', '1', '1', '1',
                     '20260101', '20261231')],
                   [],
                   [('T', '09:00:00', '09:00:00', 'S', 1),
                    ('T', '09:10:00', '09:10:00', 'P', 2)])
        z_db = self.root / 'z.sqlite'
        import_feed(z, z_db)
        # Both publishers name the trip T and leave at 09:00:00.
        target = self.root / 'ties.sqlite'
        mapping = write_mapping(self.root / 'ties.csv',
                                [('B', 'S', 'X'), ('Z', 'S', 'X')])
        regional_init({'B': self.b_db, 'Z': z_db}, mapping, target)
        rows = regional_departures(target, '2026-01-02', 'X')['departures']
        self.assertEqual([(row['feed_key'], row['departure']) for row in rows],
                         [('B', '09:00:00'), ('Z', '09:00:00')])

    def test_overnight_and_inclusive_after(self):
        rows = self.rows('2026-01-02', 'X', '24:12:00')
        self.assertEqual([(row['trip_id'], row['departure']) for row in rows],
                         [('N', '24:12:00')])
        self.assertEqual(rows[0]['service_date'], '2026-01-02')
        self.assertEqual(self.rows('2026-01-02', 'X', '24:12:01'), [])
        self.assertEqual(self.rows('2026-01-05', 'X', '09:00:00')[0]['feed_key'], 'B')

    def test_known_interchange_without_service_is_empty(self):
        # Y is registered but publisher A removes all service on Jan 5.
        self.assertEqual(self.query('2026-01-05', 'Y'),
                         {'service_date': '2026-01-05',
                          'interchange_id': 'Y', 'departures': []})

    def test_mapped_stop_without_departures_is_empty(self):
        # U has no stop_times at all; mapping it creates an empty interchange.
        mapping = write_mapping(self.root / 'empty.csv',
                                [('A', 'S', 'X'), ('A', 'U', 'EMPTY')])
        target = self.root / 'empty.sqlite'
        self.init(mapping, target)
        self.assertEqual(
            regional_departures(target, '2026-01-02', 'EMPTY')['departures'], [])

    def test_unknown_interchange_is_an_error(self):
        with self.assertRaisesRegex(ValueError, 'unknown interchange'):
            self.query(interchange='NOWHERE')

    def test_invalid_date_and_after_rejected(self):
        for day in ('2026-1-2', '2026-01-02T00:00:00', '', 20260102, None):
            with self.subTest(day=day), self.assertRaises(ValueError):
                self.query(day=day)
        for after in ('9:00:00', '24:60:00', '48:00:00', '00:00', 0, None):
            with self.subTest(after=after), self.assertRaises(ValueError):
                self.query(after=after)

    def test_query_does_not_modify_the_warehouse(self):
        digest = hashlib.sha256(self.db.read_bytes()).hexdigest()
        for kwargs in ({'interchange': 'NOWHERE'}, {'day': 'nope'},
                       {'after': '9:00:00'}):
            with self.assertRaises(ValueError):
                self.query(**kwargs)
        self.assertEqual(hashlib.sha256(self.db.read_bytes()).hexdigest(), digest)

    def test_missing_database_is_not_created(self):
        missing = self.root / 'absent.sqlite'
        with self.assertRaises(ValueError):
            regional_departures(missing, '2026-01-02', 'X')
        self.assertFalse(missing.exists())

    def test_corrupt_regional_warehouse_rejected(self):
        broken = self.root / 'broken.sqlite'
        broken.write_bytes(b'not a warehouse')
        with self.assertRaises(ValueError):
            regional_departures(broken, '2026-01-02', 'X')

    def test_baseline_archive_is_not_a_regional_warehouse(self):
        with self.assertRaises(ValueError):
            regional_departures(self.a_db, '2026-01-02', 'X')


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
        self.feed_a, self.feed_b = builder(self.root)
        self.a_db = self.root / 'a.sqlite'
        self.b_db = self.root / 'b.sqlite'
        import_feed(self.feed_a, self.a_db)
        import_feed(self.feed_b, self.b_db)
        self.mapping = write_mapping(self.root / 'map.csv',
                                     [('A', 'S', 'X'), ('A', 'P', 'Y'),
                                      ('B', 'S', 'X')])
        self.db = self.root / 'regional.sqlite'

    def init(self, db=None):
        return self.invoke('regional-init', '--feed', 'A=' + str(self.a_db),
                           '--feed', 'B=' + str(self.b_db),
                           '--interchanges', self.mapping,
                           '--db', db or self.db)

    def test_lifecycle_and_json(self):
        created = self.init()
        self.assertEqual(created.returncode, 0, created.stderr)
        self.assertTrue(self.db.exists())
        query = self.invoke('regional-departures', '--db', self.db,
                            '--date', '2026-01-05', '--interchange', 'X')
        self.assertEqual(query.returncode, 0, query.stderr)
        payload = json.loads(query.stdout)
        self.assertEqual(payload['service_date'], '2026-01-05')
        self.assertEqual(payload['interchange_id'], 'X')
        self.assertEqual([(row['feed_key'], row['trip_id'], row['stop_sequence'],
                           row['departure']) for row in payload['departures']],
                         [('B', 'T', 1, '09:00:00')])
        after = self.invoke('regional-departures', '--db', self.db,
                            '--date', '2026-01-02', '--interchange', 'X',
                            '--after', '24:12:00')
        self.assertEqual(json.loads(after.stdout)['departures'][0]['departure'],
                         '24:12:00')

    def test_existing_target_preserved(self):
        self.db.write_bytes(b'keep')
        result = self.init()
        self.assertEqual(result.returncode, 2)
        self.assertTrue(result.stderr)
        self.assertEqual(result.stdout, '')
        self.assertEqual(self.db.read_bytes(), b'keep')

    def test_symlink_target_preserved(self):
        real = self.root / 'real.sqlite'
        real.write_bytes(b'keep')
        self.db.symlink_to(real)
        self.assertEqual(self.init().returncode, 2)
        self.assertTrue(self.db.is_symlink())
        self.assertEqual(real.read_bytes(), b'keep')

    def test_failed_init_leaves_no_database(self):
        broken = self.root / 'broken.sqlite'
        broken.write_bytes(b'not a database')
        result = self.invoke('regional-init', '--feed', 'A=' + str(self.a_db),
                             '--feed', 'B=' + str(broken),
                             '--interchanges', self.mapping, '--db', self.db)
        self.assertEqual(result.returncode, 2)
        self.assertFalse(self.db.exists())
        self.assertEqual(list(self.root.glob('regional.sqlite*')), [])

    def test_query_errors_exit_two(self):
        self.assertEqual(self.init().returncode, 0)
        digest = hashlib.sha256(self.db.read_bytes()).hexdigest()
        cases = [
            ['--date', '2026-01-02', '--interchange', 'NOWHERE'],
            ['--date', '2026-1-2', '--interchange', 'X'],
            ['--date', '2026-01-02', '--interchange', 'X', '--after', '9:00:00'],
        ]
        for extra in cases:
            with self.subTest(extra=extra):
                result = self.invoke('regional-departures', '--db', self.db, *extra)
                self.assertEqual(result.returncode, 2)
                self.assertTrue(result.stderr)
                self.assertEqual(result.stdout, '')
        self.assertEqual(hashlib.sha256(self.db.read_bytes()).hexdigest(), digest)

    def test_missing_database_query_does_not_create(self):
        missing = self.root / 'absent.sqlite'
        result = self.invoke('regional-departures', '--db', missing,
                             '--date', '2026-01-02', '--interchange', 'X')
        self.assertEqual(result.returncode, 2)
        self.assertFalse(missing.exists())

    def test_feed_argument_requires_key(self):
        result = self.invoke('regional-init', '--feed', str(self.a_db),
                             '--feed', 'B=' + str(self.b_db),
                             '--interchanges', self.mapping, '--db', self.db)
        self.assertEqual(result.returncode, 2)
        self.assertFalse(self.db.exists())


if __name__ == '__main__':
    unittest.main()
