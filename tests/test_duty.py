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

from transit_feed.duty import duty_check
from transit_feed.feed import import_feed
from transit_feed.revision import revision_init

ROOT = Path(__file__).resolve().parents[1]
MODULE = 'transit_feed'

DUTY_HEADER = ['vehicle_id', 'trip_id', 'start_date', 'end_date']
LINK_HEADER = ['from_stop', 'to_stop', 'seconds']
ALL_DAYS = ('1', '1', '1', '1', '1', '1', '1')


def write_csv(path, header, rows, encoding='utf-8'):
    path = Path(path)
    with path.open('w', encoding=encoding, newline='') as stream:
        writer = csv.writer(stream)
        writer.writerow(header)
        writer.writerows(rows)
    return path


def write_duties(path, rows, header=None, encoding='utf-8'):
    return write_csv(path, header or DUTY_HEADER, rows, encoding)


def write_links(path, rows, header=None, encoding='utf-8'):
    return write_csv(path, header or LINK_HEADER, rows, encoding)


def write_feed(root, stops, trips, calendar, exceptions, times):
    root = Path(root)
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
        write_csv(root / name, header, rows)


class DutyBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def build_base(self, links=None):
        """V1's T1 -> T2 -> T3 chain; T2 is suspended on 2026-01-07."""
        self.feed = self.root / 'feed'
        shutil.copytree(ROOT / 'fixtures' / 'duty' / 'feed', self.feed)
        self.db = self.root / 'duty.sqlite'
        import_feed(self.feed, self.db)
        self.duties = self.root / 'duties.csv'
        self.links = self.root / 'links.csv'
        shutil.copy(ROOT / 'fixtures' / 'duty' / 'duties.csv', self.duties)
        if links is None:
            shutil.copy(ROOT / 'fixtures' / 'duty' / 'links.csv', self.links)
        else:
            write_links(self.links, links)
        return self

    def feed_with(self, name, trips, calendar, exceptions, times,
                  stops=(('A', 'Alpha'), ('B', 'Bravo'), ('C', 'Charlie'),
                         ('D', 'Delta'))):
        database = self.root / (name + '.sqlite')
        write_feed(self.root / name, stops, trips, calendar, exceptions,
                   times)
        import_feed(self.root / name, database)
        self.db = database
        return database

    def check(self, first='2026-01-07', last=None, duties=None, links=None):
        return duty_check(self.db, duties or self.duties, links or self.links,
                          first, last or first)

    def assertShape(self, result):
        self.assertEqual(set(result),
                         {'feasible', 'duplicate_assignments', 'connections'})
        for item in result['connections']:
            self.assertEqual(set(item), {'kind', 'vehicle_id', 'previous',
                                         'next', 'available_seconds',
                                         'required_seconds',
                                         'shortfall_seconds'})
            self.assertEqual(set(item['previous']),
                             {'trip_id', 'service_date', 'start_stop',
                              'end_stop', 'start', 'end'})
            self.assertEqual(set(item['next']), set(item['previous']))
        for item in result['duplicate_assignments']:
            self.assertEqual(set(item), {'kind', 'trip_id', 'service_date',
                                         'vehicle_ids', 'rule_rows'})
        self.assertEqual(result['feasible'],
                         not result['duplicate_assignments']
                         and not result['connections'])


class AcceptanceTests(DutyBase):
    def test_linked_chain_is_feasible_before_suspension(self):
        self.build_base()
        result = self.check('2026-01-06')
        self.assertShape(result)
        self.assertEqual(result, {'feasible': True,
                                  'duplicate_assignments': [],
                                  'connections': []})

    def test_suspended_middle_trip_creates_twenty_minute_gap(self):
        self.build_base()
        result = self.check('2026-01-07')
        self.assertShape(result)
        self.assertFalse(result['feasible'])
        self.assertEqual(result['duplicate_assignments'], [])
        self.assertEqual(len(result['connections']), 1)
        item = result['connections'][0]
        self.assertEqual(item['kind'], 'insufficient_turnaround')
        self.assertEqual(item['vehicle_id'], 'V1')
        self.assertEqual(item['previous']['trip_id'], 'T1')
        self.assertEqual(item['previous']['service_date'], '2026-01-07')
        self.assertEqual(item['previous']['start_stop'], 'A')
        self.assertEqual(item['previous']['end_stop'], 'B')
        self.assertEqual(item['previous']['start'], '09:00:00')
        self.assertEqual(item['previous']['end'], '10:00:00')
        self.assertEqual(item['next']['trip_id'], 'T3')
        self.assertEqual(item['next']['service_date'], '2026-01-07')
        self.assertEqual(item['next']['start_stop'], 'C')
        self.assertEqual(item['next']['end_stop'], 'D')
        self.assertEqual(item['next']['start'], '11:10:00')
        self.assertEqual(item['next']['end'], '12:00:00')
        self.assertEqual(item['available_seconds'], 4200)
        self.assertEqual(item['required_seconds'], 5400)
        self.assertEqual(item['shortfall_seconds'], 1200)
        # A suspended trip is neither a duplicate nor a missing vehicle.
        self.assertNotIn('T2', json.dumps(result))

    def test_suspension_only_changes_eligibility(self):
        self.build_base()
        self.assertTrue(self.check('2026-01-06')['feasible'])
        after = self.check('2026-01-07')
        self.assertFalse(after['feasible'])
        self.assertEqual(after['duplicate_assignments'], [])

    def test_same_vehicle_chain_spans_both_days(self):
        self.build_base()
        result = self.check('2026-01-06', '2026-01-07')
        kinds = [(item['kind'], item['previous']['service_date'],
                  item['previous']['trip_id'], item['next']['service_date'],
                  item['next']['trip_id'])
                 for item in result['connections']]
        self.assertEqual(kinds, [
            ('insufficient_turnaround', '2026-01-07', 'T1', '2026-01-07',
             'T3')])


class CrossServiceDayTests(DutyBase):
    def build_rolling(self):
        self.feed = self.root / 'feed'
        shutil.copytree(ROOT / 'fixtures' / 'duty-rolling' / 'feed',
                        self.feed)
        self.db = self.root / 'rolling.sqlite'
        import_feed(self.feed, self.db)
        self.duties = self.root / 'duties.csv'
        self.links = self.root / 'links.csv'
        shutil.copy(ROOT / 'fixtures' / 'duty-rolling' / 'duties.csv',
                    self.duties)
        shutil.copy(ROOT / 'fixtures' / 'duty-rolling' / 'links.csv',
                    self.links)

    def test_overlap_across_midnight_when_querying_second_day_only(self):
        self.build_rolling()
        result = self.check('2026-01-07')
        self.assertShape(result)
        self.assertFalse(result['feasible'])
        self.assertEqual(len(result['connections']), 1)
        item = result['connections'][0]
        self.assertEqual(item['kind'], 'overlap')
        self.assertEqual(item['vehicle_id'], 'V2')
        self.assertEqual(item['previous'],
                         {'trip_id': 'T4', 'service_date': '2026-01-06',
                          'start_stop': 'A', 'end_stop': 'B',
                          'start': '24:20:00', 'end': '25:00:00'})
        self.assertEqual(item['next'],
                         {'trip_id': 'T5', 'service_date': '2026-01-07',
                          'start_stop': 'B', 'end_stop': 'C',
                          'start': '00:50:00', 'end': '01:30:00'})
        self.assertEqual(item['available_seconds'], -600)
        self.assertIsNone(item['required_seconds'])
        self.assertEqual(item['shortfall_seconds'], 600)

    def test_previous_day_in_range_also_reports(self):
        self.build_rolling()
        result = self.check('2026-01-06')
        self.assertEqual([item['kind'] for item in result['connections']],
                         ['overlap'])
        self.assertEqual(result['connections'][0]['previous']['trip_id'],
                         'T4')

    def test_pair_entirely_outside_range_is_not_reported(self):
        self.build_rolling()
        result = self.check('2026-01-05')
        self.assertEqual(result, {'feasible': True,
                                  'duplicate_assignments': [],
                                  'connections': []})

    def test_row_order_does_not_change_result(self):
        self.build_rolling()
        first = self.check('2026-01-07')
        write_duties(self.duties, [('V2', 'T5', '2026-01-01', '2026-12-31'),
                                   ('V2', 'T4', '2026-01-01', '2026-12-31')])
        self.assertEqual(first, self.check('2026-01-07'))


class RangeContextTests(DutyBase):
    def build_four_days(self):
        self.feed_with(
            'four',
            [('U0', 'WK'), ('U1', 'WK'), ('U2', 'WK'), ('U3', 'WK')],
            [('WK',) + ALL_DAYS + ('20260101', '20260131')],
            [],
            [('U0', '08:00:00', '08:00:00', 'A', 1),
             ('U0', '09:00:00', '09:00:00', 'B', 2),
             ('U1', '08:00:00', '08:00:00', 'A', 1),
             ('U1', '09:00:00', '09:00:00', 'B', 2),
             ('U2', '08:00:00', '08:00:00', 'C', 1),
             ('U2', '09:00:00', '09:00:00', 'A', 2),
             ('U3', '08:00:00', '08:00:00', 'C', 1),
             ('U3', '09:00:00', '09:00:00', 'A', 2)])
        self.duties = write_duties(self.root / 'four.csv', [
            ('V', 'U0', '2026-01-04', '2026-01-04'),
            ('V', 'U1', '2026-01-05', '2026-01-05'),
            ('V', 'U2', '2026-01-06', '2026-01-06'),
            ('V', 'U3', '2026-01-07', '2026-01-07')])
        self.links = write_links(self.root / 'four-links.csv', [])

    def test_out_of_range_neighbours_still_bound_in_range_instances(self):
        self.build_four_days()
        result = self.check('2026-01-06')
        pairs = [(item['previous']['trip_id'], item['next']['trip_id'])
                 for item in result['connections']]
        self.assertEqual(pairs, [('U1', 'U2'), ('U2', 'U3')])
        self.assertEqual({item['kind'] for item in result['connections']},
                         {'missing_link'})
        for item in result['connections']:
            self.assertIsNone(item['required_seconds'])
            self.assertIsNone(item['shortfall_seconds'])
            self.assertEqual(item['available_seconds'], 23 * 3600)

    def test_fully_outside_pairs_are_suppressed(self):
        self.build_four_days()
        result = self.check('2026-01-06', '2026-01-06')
        pairs = [(item['previous']['trip_id'], item['next']['trip_id'])
                 for item in result['connections']]
        self.assertEqual(pairs, [('U1', 'U2'), ('U2', 'U3')])
        result = self.check('2026-01-05', '2026-01-05')
        pairs = [(item['previous']['trip_id'], item['next']['trip_id'])
                 for item in result['connections']]
        self.assertEqual(pairs, [('U0', 'U1'), ('U1', 'U2')])

    def test_distant_predecessor_is_still_the_neighbour(self):
        self.feed_with(
            'distant',
            [('F', 'WK')],
            [('WK',) + ALL_DAYS + ('20260101', '20260331')],
            [],
            [('F', '08:00:00', '08:00:00', 'A', 1),
             ('F', '09:00:00', '09:00:00', 'B', 2)])
        self.duties = write_duties(self.root / 'distant.csv', [
            ('V', 'F', '2026-01-02', '2026-01-02'),
            ('V', 'F', '2026-03-20', '2026-03-20')])
        self.links = write_links(self.root / 'distant-links.csv', [])
        result = self.check('2026-03-20')
        item = result['connections'][0]
        self.assertEqual(item['previous']['service_date'], '2026-01-02')
        self.assertEqual(item['next']['service_date'], '2026-03-20')
        self.assertEqual(item['kind'], 'missing_link')

    def test_47h_clock_interleaves_adjacent_service_days(self):
        # A 47:xx clock on one service day sorts after 00:xx on the next one,
        # so the chain is ordered by absolute time, not by service date.
        self.feed_with(
            'late',
            [('LATE', 'WK'), ('EARLY', 'WK')],
            [('WK',) + ALL_DAYS + ('20260101', '20260131')],
            [],
            [('LATE', '47:00:00', '47:00:00', 'B', 1),
             ('LATE', '47:30:00', '47:30:00', 'C', 2),
             ('EARLY', '00:00:00', '00:00:00', 'C', 1),
             ('EARLY', '00:20:00', '00:20:00', 'A', 2)])
        self.duties = write_duties(self.root / 'late.csv', [
            ('V', 'LATE', '2026-01-06', '2026-01-06'),
            ('V', 'EARLY', '2026-01-07', '2026-01-07')])
        self.links = write_links(self.root / 'late-links.csv', [])
        # EARLY on Jan 7 (00:00) comes before LATE on Jan 6 (47:00) in absolute
        # time, so the reported pair is EARLY -> LATE, not the reverse.
        result = self.check('2026-01-07')
        item = result['connections'][0]
        self.assertEqual(item['previous']['trip_id'], 'EARLY')
        self.assertEqual(item['previous']['service_date'], '2026-01-07')
        self.assertEqual(item['next']['trip_id'], 'LATE')
        self.assertEqual(item['next']['service_date'], '2026-01-06')
        self.assertEqual(item['next']['start'], '47:00:00')
        self.assertEqual(item['kind'], 'missing_link')
        self.assertIsNone(item['required_seconds'])
        self.assertEqual(item['available_seconds'], 82800 - 1200)

    def test_next_day_zero_hour_sorts_before_late_previous_clock(self):
        self.feed_with(
            'mixed',
            [('E', 'WK'), ('N', 'WK')],
            [('WK',) + ALL_DAYS + ('20260101', '20260131')],
            [],
            [('E', '24:20:00', '24:20:00', 'A', 1),
             ('E', '25:00:00', '25:00:00', 'B', 2),
             ('N', '00:50:00', '00:50:00', 'B', 1),
             ('N', '01:30:00', '01:30:00', 'C', 2)])
        self.duties = write_duties(self.root / 'mixed.csv', [
            ('V', 'E', '2026-01-06', '2026-01-06'),
            ('V', 'N', '2026-01-07', '2026-01-07')])
        self.links = write_links(self.root / 'mixed-links.csv', [])
        result = self.check('2026-01-07')
        item = result['connections'][0]
        self.assertEqual(item['kind'], 'overlap')
        self.assertEqual(item['previous']['service_date'], '2026-01-06')
        self.assertEqual(item['next']['service_date'], '2026-01-07')


class DuplicateTests(DutyBase):
    def test_duplicate_instance_is_excluded_from_chains(self):
        self.build_base()
        write_duties(self.duties, [
            ('V1', 'T1', '2026-01-07', '2026-01-07'),
            ('V1', 'T2', '2026-01-07', '2026-01-07'),
            ('V1', 'T3', '2026-01-07', '2026-01-07'),
            ('V9', 'T1', '2026-01-07', '2026-01-07')])
        result = self.check('2026-01-07')
        self.assertShape(result)
        self.assertEqual(result['duplicate_assignments'], [
            {'kind': 'duplicate_assignment', 'trip_id': 'T1',
             'service_date': '2026-01-07', 'vehicle_ids': ['V1', 'V9'],
             'rule_rows': [2, 5]}])
        # T1 is removed, so V1 has T2 -> T3 only; both are same-stop links.
        self.assertEqual(result['connections'], [])
        self.assertFalse(result['feasible'])

    def test_identical_rows_still_duplicate(self):
        self.build_base()
        write_duties(self.duties, [
            ('V1', 'T1', '2026-01-07', '2026-01-07'),
            ('V1', 'T1', '2026-01-07', '2026-01-07')])
        result = self.check('2026-01-07')
        self.assertEqual(result['duplicate_assignments'], [
            {'kind': 'duplicate_assignment', 'trip_id': 'T1',
             'service_date': '2026-01-07', 'vehicle_ids': ['V1'],
             'rule_rows': [2, 3]}])

    def test_duplicate_selects_both_vehicles_and_both_rows(self):
        self.build_base()
        write_duties(self.duties, [
            ('VB', 'T1', '2026-01-01', '2026-01-31'),
            ('VA', 'T1', '2026-01-06', '2026-01-08'),
            ('VA', 'T1', '2026-01-07', '2026-01-07')])
        result = self.check('2026-01-07')
        self.assertEqual(result['duplicate_assignments'], [
            {'kind': 'duplicate_assignment', 'trip_id': 'T1',
             'service_date': '2026-01-07',
             'vehicle_ids': ['VA', 'VB'], 'rule_rows': [2, 3, 4]}])

    def test_duplicate_order_by_service_date_then_trip(self):
        self.build_base()
        write_duties(self.duties, [
            ('V1', 'T3', '2026-01-06', '2026-01-07'),
            ('V1', 'T1', '2026-01-06', '2026-01-07'),
            ('V2', 'T1', '2026-01-06', '2026-01-07'),
            ('V2', 'T3', '2026-01-06', '2026-01-07')])
        result = self.check('2026-01-06', '2026-01-07')
        self.assertEqual(
            [(item['service_date'], item['trip_id'])
             for item in result['duplicate_assignments']],
            [('2026-01-06', 'T1'), ('2026-01-06', 'T3'),
             ('2026-01-07', 'T1'), ('2026-01-07', 'T3')])
        self.assertEqual(result['connections'], [])

    def test_duplicate_outside_range_is_suppressed_but_excluded(self):
        self.build_base()
        write_duties(self.duties, [
            ('V1', 'T1', '2026-01-05', '2026-01-06'),
            ('V1', 'T2', '2026-01-06', '2026-01-06'),
            ('V1', 'T3', '2026-01-05', '2026-01-05'),
            ('V9', 'T3', '2026-01-05', '2026-01-05')])
        result = self.check('2026-01-06')
        self.assertEqual(result['duplicate_assignments'], [])
        # T3 on Jan 5 is excluded even though it is not reported, so V1's
        # previous instance before T1 on Jan 6 is T1 on Jan 5.
        pairs = [(item['kind'], item['previous']['trip_id'],
                  item['previous']['service_date'], item['next']['trip_id'])
                 for item in result['connections']]
        self.assertEqual(pairs, [('missing_link', 'T1', '2026-01-05', 'T1')])
        self.assertEqual(result['connections'][0]['next']['service_date'],
                         '2026-01-06')

    def test_suspended_overlap_is_not_duplicate(self):
        self.build_base()
        write_duties(self.duties, [
            ('V1', 'T2', '2026-01-06', '2026-01-07'),
            ('V2', 'T2', '2026-01-06', '2026-01-07')])
        result = self.check('2026-01-07')
        self.assertEqual(result['duplicate_assignments'], [])
        self.assertTrue(result['feasible'])

    def test_rule_overlap_without_running_service_is_ignored(self):
        self.build_base()
        write_duties(self.duties, [
            ('V1', 'T2', '2026-01-07', '2026-01-08'),
            ('V2', 'T2', '2026-01-07', '2026-01-07')])
        result = self.check('2026-01-07')
        self.assertEqual(result, {'feasible': True,
                                  'duplicate_assignments': [],
                                  'connections': []})

    def test_same_trip_can_change_vehicle_in_disjoint_ranges(self):
        self.build_base()
        write_duties(self.duties, [
            ('V1', 'T1', '2026-01-06', '2026-01-06'),
            ('V2', 'T1', '2026-01-07', '2026-01-07'),
            ('V2', 'T3', '2026-01-07', '2026-01-07')])
        result = self.check('2026-01-07')
        self.assertEqual(result['duplicate_assignments'], [])
        self.assertEqual([(item['vehicle_id'], item['previous']['trip_id'],
                           item['next']['trip_id'])
                          for item in result['connections']],
                         [('V2', 'T1', 'T3')])


class ExceptionTests(DutyBase):
    def test_type_one_exception_adds_running_instance(self):
        self.build_base()
        write_csv(self.feed / 'calendar_dates.txt',
                  ['service_id', 'date', 'exception_type'],
                  [('S2', '20260107', '1')])
        self.feed_with_reimport()
        self.duties = write_duties(self.root / 'added.csv', [
            ('V1', 'T1', '2026-01-07', '2026-01-07'),
            ('V1', 'T2', '2026-01-07', '2026-01-07'),
            ('V1', 'T3', '2026-01-07', '2026-01-07')])
        self.links = write_links(self.root / 'ok-links.csv',
                                 [('B', 'C', '5400')])
        self.assertEqual(self.check('2026-01-07'),
                         {'feasible': True, 'duplicate_assignments': [],
                          'connections': []})

    def test_type_two_exception_removes_without_duplicate(self):
        self.build_base()
        write_csv(self.feed / 'calendar_dates.txt',
                  ['service_id', 'date', 'exception_type'],
                  [('S2', '20260106', '2')])
        self.feed_with_reimport()
        self.duties = write_duties(self.root / 'removed.csv', [
            ('V1', 'T1', '2026-01-06', '2026-01-06'),
            ('V2', 'T2', '2026-01-06', '2026-01-06'),
            ('V1', 'T3', '2026-01-06', '2026-01-06')])
        self.links = write_links(self.root / 'ok-links.csv',
                                 [('B', 'C', '5400')])
        result = self.check('2026-01-06')
        self.assertEqual(result['duplicate_assignments'], [])
        self.assertEqual([item['kind'] for item in result['connections']],
                         ['insufficient_turnaround'])
        self.assertEqual(result['connections'][0]['previous']['trip_id'],
                         'T1')
        self.assertEqual(result['connections'][0]['next']['trip_id'], 'T3')

    def feed_with_reimport(self):
        database = self.root / 'reimported.sqlite'
        import_feed(self.feed, database)
        self.db = database
        return database

    def test_exception_only_service_drives_duties(self):
        self.feed_with(
            'special',
            [('SP', 'ONLY')],
            [],
            [('ONLY', '20260107', '1')],
            [('SP', '08:00:00', '08:00:00', 'A', 1),
             ('SP', '09:00:00', '09:00:00', 'B', 2)])
        self.duties = write_duties(self.root / 'special.csv', [
            ('V', 'SP', '2026-01-01', '2026-01-31')])
        self.links = write_links(self.root / 'special-links.csv', [])
        self.assertEqual(self.check('2026-01-06'),
                         {'feasible': True, 'duplicate_assignments': [],
                          'connections': []})
        # On the added day the trip runs with no partner, so still feasible.
        self.assertEqual(self.check('2026-01-07'),
                         {'feasible': True, 'duplicate_assignments': [],
                          'connections': []})

    def test_addition_outside_weekday_calendar_still_counts(self):
        # Saturday/Sunday are disabled; an exception adds Sunday explicitly.
        self.feed_with(
            'weekend',
            [('T', 'WK'), ('U', 'WK')],
            [('WK', '1', '1', '1', '1', '1', '0', '0', '20260101',
              '20260131')],
            [('WK', '20260110', '1')],
            [('T', '08:00:00', '08:00:00', 'A', 1),
             ('T', '09:00:00', '09:00:00', 'B', 2),
             ('U', '08:30:00', '08:30:00', 'B', 1),
             ('U', '09:30:00', '09:30:00', 'C', 2)])
        self.duties = write_duties(self.root / 'weekend.csv', [
            ('V', 'T', '2026-01-10', '2026-01-10'),
            ('V', 'U', '2026-01-10', '2026-01-10')])
        self.links = write_links(self.root / 'weekend-links.csv', [])
        result = self.check('2026-01-10')
        # 08:30 starts inside T's 08:00..09:00 occupancy.
        self.assertEqual(result['connections'][0]['kind'], 'overlap')
        self.assertEqual(result['connections'][0]['shortfall_seconds'], 1800)
        # On the previous Saturday (Jan 3) no exception adds the service.
        self.assertEqual(self.check('2026-01-03'),
                         {'feasible': True, 'duplicate_assignments': [],
                          'connections': []})


class LinkTests(DutyBase):
    def test_missing_directed_rule_is_not_zero(self):
        self.build_base()
        write_duties(self.duties, [
            ('V', 'T1', '2026-01-07', '2026-01-07'),
            ('V', 'T3', '2026-01-07', '2026-01-07')])
        missing = self.check('2026-01-07',
                             links=write_links(self.root / 'none.csv', []))
        self.assertEqual([(item['kind'],
                           item['available_seconds'],
                           item['required_seconds'],
                           item['shortfall_seconds'])
                          for item in missing['connections']],
                         [('missing_link', 4200, None, None)])

    def test_directed_rule_is_used_and_reverse_is_separate(self):
        self.build_base()
        write_duties(self.duties, [
            ('V', 'T1', '2026-01-07', '2026-01-07'),
            ('V', 'T3', '2026-01-07', '2026-01-07')])
        explicit = self.check('2026-01-07', links=write_links(
            self.root / 'b-c.csv', [('B', 'C', '4201')]))
        self.assertEqual([item['kind'] for item in explicit['connections']],
                         ['insufficient_turnaround'])
        self.assertEqual(explicit['connections'][0]['required_seconds'], 4201)
        self.assertEqual(explicit['connections'][0]['shortfall_seconds'], 1)
        reverse = self.check('2026-01-07', links=write_links(
            self.root / 'c-b.csv', [('C', 'B', '4201')]))
        self.assertEqual([item['kind'] for item in reverse['connections']],
                         ['missing_link'])

    def test_same_stop_pair_uses_zero_default(self):
        self.build_base()
        # T1 ends at B and T2 starts at B: 10 minutes are enough.
        write_duties(self.duties, [
            ('V1', 'T1', '2026-01-07', '2026-01-07'),
            ('V1', 'T2', '2026-01-07', '2026-01-07')])
        write_csv(self.feed / 'calendar_dates.txt',
                  ['service_id', 'date', 'exception_type'],
                  [('S2', '20260107', '1')])
        database = self.root / 'same.sqlite'
        import_feed(self.feed, database)
        self.db = database
        links = write_links(self.root / 'none.csv', [])
        result = self.check('2026-01-07', links=links)
        self.assertEqual(result['connections'], [])
        self.assertTrue(result['feasible'])

    def test_explicit_same_stop_rule_overrides_zero(self):
        self.build_base()
        write_duties(self.duties, [
            ('V1', 'T1', '2026-01-07', '2026-01-07'),
            ('V1', 'T2', '2026-01-07', '2026-01-07')])
        write_csv(self.feed / 'calendar_dates.txt',
                  ['service_id', 'date', 'exception_type'],
                  [('S2', '20260107', '1')])
        database = self.root / 'same2.sqlite'
        import_feed(self.feed, database)
        self.db = database
        links = write_links(self.root / 'b-b.csv', [('B', 'B', '900')])
        result = self.check('2026-01-07', links=links)
        item = result['connections'][0]
        self.assertEqual(item['previous']['trip_id'], 'T1')
        self.assertEqual(item['next']['trip_id'], 'T2')
        self.assertEqual(item['available_seconds'], 600)
        self.assertEqual(item['required_seconds'], 900)
        self.assertEqual(item['shortfall_seconds'], 300)

    def test_exactly_enough_turnaround_is_feasible(self):
        self.build_base()
        write_duties(self.duties, [
            ('V', 'T1', '2026-01-07', '2026-01-07'),
            ('V', 'T3', '2026-01-07', '2026-01-07')])
        exact = self.check('2026-01-07', links=write_links(
            self.root / 'exact.csv', [('B', 'C', '4200')]))
        self.assertEqual(exact['connections'], [])
        self.assertTrue(exact['feasible'])
        short = self.check('2026-01-07', links=write_links(
            self.root / 'short.csv', [('B', 'C', '4201')]))

    def test_occupancy_uses_first_departure_and_last_arrival(self):
        self.feed_with(
            'dwell',
            [('P', 'WK'), ('Q', 'WK')],
            [('WK',) + ALL_DAYS + ('20260101', '20260131')],
            [],
            [('P', '08:00:00', '08:30:00', 'A', 1),
             ('P', '09:30:00', '10:00:00', 'B', 2),
             ('Q', '10:05:00', '10:10:00', 'B', 1),
             ('Q', '10:20:00', '10:20:00', 'B', 2)],
            stops=(('A', 'Alpha'), ('B', 'Bravo')))
        self.duties = write_duties(self.root / 'dwell.csv', [
            ('V', 'P', '2026-01-07', '2026-01-07'),
            ('V', 'Q', '2026-01-07', '2026-01-07')])
        self.links = write_links(self.root / 'dwell-links.csv', [])
        result = self.check('2026-01-07')
        # P occupies 08:30..09:30 and Q 10:05..10:20, so the 5-minute gap is
        # enough even though P's tail dwell lasts 30 minutes.
        self.assertEqual(result['connections'], [])
        self.assertTrue(result['feasible'])

    def test_first_and_last_stop_follow_stop_sequence(self):
        self.feed_with(
            'order',
            [('R', 'WK'), ('S', 'WK')],
            [('WK',) + ALL_DAYS + ('20260101', '20260131')],
            [],
            [('R', '09:30:00', '09:30:00', 'C', 3),
             ('R', '08:00:00', '08:00:00', 'A', 1),
             ('R', '09:00:00', '09:00:00', 'B', 2),
             ('S', '09:40:00', '09:40:00', 'C', 1),
             ('S', '10:00:00', '10:00:00', 'D', 2)])
        self.duties = write_duties(self.root / 'order.csv', [
            ('V', 'R', '2026-01-07', '2026-01-07'),
            ('V', 'S', '2026-01-07', '2026-01-07')])
        self.links = write_links(self.root / 'order-links.csv',
                                 [('C', 'C', '900')])
        result = self.check('2026-01-07')
        item = result['connections'][0]
        self.assertEqual(item['previous']['start_stop'], 'A')
        self.assertEqual(item['previous']['end_stop'], 'C')
        self.assertEqual(item['previous']['start'], '08:00:00')
        self.assertEqual(item['previous']['end'], '09:30:00')
        self.assertEqual(item['available_seconds'], 600)
        self.assertEqual(item['kind'], 'insufficient_turnaround')

    def test_zero_duration_occupancy_is_allowed(self):
        self.feed_with(
            'zero',
            [('Z', 'WK')],
            [('WK',) + ALL_DAYS + ('20260101', '20260131')],
            [],
            [('Z', '08:00:00', '08:00:00', 'A', 1),
             ('Z', '08:00:00', '08:00:00', 'B', 2)],
            stops=(('A', 'Alpha'), ('B', 'Bravo')))
        self.duties = write_duties(self.root / 'zero.csv',
                                   [('V', 'Z', '2026-01-07', '2026-01-07')])
        self.links = write_links(self.root / 'zero-links.csv', [])
        result = self.check('2026-01-07')
        self.assertTrue(result['feasible'])
        self.assertEqual(result['connections'], [])


class SortingTests(DutyBase):
    def test_connections_sort_by_vehicle_then_times(self):
        self.feed_with(
            'sorting',
            [('P1', 'WK'), ('P2', 'WK'), ('Q1', 'WK'), ('Q2', 'WK')],
            [('WK',) + ALL_DAYS + ('20260101', '20260131')],
            [],
            [('P1', '08:00:00', '08:00:00', 'A', 1),
             ('P1', '09:00:00', '09:00:00', 'B', 2),
             ('P2', '09:10:00', '09:10:00', 'C', 1),
             ('P2', '10:00:00', '10:00:00', 'D', 2),
             ('Q1', '08:00:00', '08:00:00', 'A', 1),
             ('Q1', '09:00:00', '09:00:00', 'B', 2),
             ('Q2', '09:10:00', '09:10:00', 'C', 1),
             ('Q2', '10:00:00', '10:00:00', 'D', 2)])
        self.duties = write_duties(self.root / 'sorting.csv', [
            ('VB', 'P1', '2026-01-07', '2026-01-07'),
            ('VB', 'P2', '2026-01-07', '2026-01-07'),
            ('VA', 'Q1', '2026-01-07', '2026-01-07'),
            ('VA', 'Q2', '2026-01-07', '2026-01-07')])
        self.links = write_links(self.root / 'sorting-links.csv',
                                 [('B', 'C', '1200')])
        result = self.check('2026-01-07')
        self.assertEqual([(item['vehicle_id'], item['previous']['trip_id'],
                           item['next']['trip_id'])
                          for item in result['connections']],
                         [('VA', 'Q1', 'Q2'), ('VB', 'P1', 'P2')])

    def test_connections_sort_by_next_then_previous_time(self):
        self.feed_with(
            'sorting2',
            [('S1', 'WK'), ('S2', 'WK')],
            [('WK',) + ALL_DAYS + ('20260101', '20260131')],
            [],
            [('S1', '08:00:00', '08:00:00', 'A', 1),
             ('S1', '08:30:00', '08:30:00', 'B', 2),
             ('S2', '16:00:00', '16:00:00', 'A', 1),
             ('S2', '17:00:00', '17:00:00', 'B', 2)])
        self.duties = write_duties(self.root / 'sorting2.csv', [
            ('V', 'S1', '2026-01-07', '2026-01-07'),
            ('V', 'S2', '2026-01-07', '2026-01-07'),
            ('V', 'S1', '2026-01-06', '2026-01-06')])
        self.links = write_links(self.root / 'sorting2-links.csv', [])
        result = self.check('2026-01-06', '2026-01-07')
        self.assertEqual(
            [(item['previous']['trip_id'], item['previous']['service_date'],
              item['next']['trip_id'], item['next']['service_date'])
             for item in result['connections']],
            [('S1', '2026-01-06', 'S1', '2026-01-07'),
             ('S1', '2026-01-07', 'S2', '2026-01-07')])


class RefreshTests(DutyBase):
    def test_same_path_same_length_links_rewrite_is_reflected(self):
        self.build_base(links=[('B', 'C', '5400'), ('D', 'A', '0000')])
        first = self.check('2026-01-07')
        self.assertEqual(first['connections'][0]['shortfall_seconds'], 1200)
        size = self.links.stat().st_size
        write_links(self.links, [('B', 'C', '3600'), ('D', 'A', '0000')])
        self.assertEqual(self.links.stat().st_size, size)
        second = self.check('2026-01-07')
        self.assertEqual(second['connections'], [])
        self.assertTrue(second['feasible'])

    def test_restoring_exception_rebuilds_chains(self):
        self.build_base()
        self.assertFalse(self.check('2026-01-07')['feasible'])
        write_csv(self.feed / 'calendar_dates.txt',
                  ['service_id', 'date', 'exception_type'], [])
        database = self.root / 'restored.sqlite'
        import_feed(self.feed, database)
        self.db = database
        restored = self.check('2026-01-07')
        self.assertEqual(restored['connections'], [])
        self.assertTrue(restored['feasible'])

    def test_duties_rewrite_on_same_path(self):
        self.build_base()
        write_duties(self.duties, [('V1', 'T1', '2026-01-07', '2026-01-07'),
                                   ('V1', 'T3', '2026-01-07', '2026-01-07')])
        first = self.check('2026-01-07')
        self.assertEqual(first['connections'][0]['kind'],
                         'insufficient_turnaround')
        write_duties(self.duties, [('V1', 'T1', '2026-01-07', '2026-01-07'),
                                   ('V2', 'T3', '2026-01-07', '2026-01-07')])
        second = self.check('2026-01-07')
        self.assertEqual(second['connections'], [])
        self.assertTrue(second['feasible'])

    def test_failed_then_fixed_input_in_same_process(self):
        self.build_base()
        with self.assertRaises(ValueError):
            self.check('2026-01-07', links=write_links(
                self.root / 'bad.csv', [('B', 'C', '-5')]))
        result = self.check('2026-01-07')
        self.assertEqual(result['connections'][0]['kind'],
                         'insufficient_turnaround')


class ValidationTests(DutyBase):
    def setUp(self):
        super().setUp()
        self.build_base()
        self.good_duties = [('V1', 'T1', '2026-01-07', '2026-01-07')]

    def duties_file(self, rows, name='bad-duties.csv'):
        return write_duties(self.root / name, rows)

    def links_file(self, rows, name='bad-links.csv'):
        return write_links(self.root / name, rows)

    def test_empty_header_only_files_are_legal(self):
        result = self.check('2026-01-07',
                            duties=self.duties_file([], 'empty.csv'),
                            links=self.links_file([], 'empty-links.csv'))
        self.assertEqual(result, {'feasible': True,
                                  'duplicate_assignments': [],
                                  'connections': []})

    def test_bad_duty_rows(self):
        cases = {
            'unknown trip': [('V', 'NOPE', '2026-01-07', '2026-01-07')],
            'inverted': [('V', 'T1', '2026-01-07', '2026-01-06')],
            'sloppy date': [('V', 'T1', '2026-1-7', '2026-01-07')],
            'empty vehicle': [('', 'T1', '2026-01-07', '2026-01-07')],
            'padded vehicle': [(' V', 'T1', '2026-01-07', '2026-01-07')],
            'control vehicle': [('V\x01', 'T1', '2026-01-07',
                                 '2026-01-07')],
            'padded trip': [('V', 'T1 ', '2026-01-07', '2026-01-07')],
            'empty date': [('V', 'T1', '', '2026-01-07')],
        }
        for label, rows in cases.items():
            with self.subTest(label=label), self.assertRaises(ValueError):
                self.check('2026-01-07', duties=self.duties_file(rows))

    def test_bad_link_rows(self):
        cases = {
            'unknown from': [('Z', 'C', '1')],
            'unknown to': [('B', 'Z', '1')],
            'negative': [('B', 'C', '-1')],
            'decimal': [('B', 'C', '1.5')],
            'empty': [('B', 'C', '')],
            'too large': [('B', 'C', '86401')],
            'padded': [('B', 'C', ' 1')],
            'plus sign': [('B', 'C', '+1')],
        }
        for label, rows in cases.items():
            with self.subTest(label=label), self.assertRaises(ValueError):
                self.check('2026-01-07', links=self.links_file(rows))

    def test_duplicate_directed_link_rejected(self):
        with self.assertRaisesRegex(ValueError, 'duplicate'):
            self.check('2026-01-07', links=self.links_file(
                [('B', 'C', '1'), ('B', 'C', '2')]))
        # The reverse direction is a different link and is accepted.
        reverse = write_links(self.root / 'reverse.csv',
                              [('B', 'C', '1'), ('C', 'B', '1')])
        result = self.check('2026-01-07', duties=self.duties_file(
            self.good_duties, 'one-duty.csv'), links=reverse)
        self.assertTrue(result['feasible'])
        self.assertEqual(result['connections'], [])

    def test_maximum_link_seconds_accepted(self):
        duties = write_duties(self.root / 'long-gap.csv', [
            ('V', 'T1', '2026-01-07', '2026-01-07'),
            ('V', 'T3', '2026-01-07', '2026-01-07')])
        result = self.check('2026-01-07', duties=duties,
                            links=self.links_file([('B', 'C', '86400')]))
        self.assertEqual(result['connections'][0]['required_seconds'], 86400)
        self.assertEqual(result['connections'][0]['kind'],
                         'insufficient_turnaround')

    def test_wrong_header_and_column_count(self):
        wrong = write_duties(self.root / 'dh.csv', self.good_duties,
                             header=['trip_id', 'vehicle_id', 'start_date',
                                     'end_date'])
        with self.assertRaisesRegex(ValueError, 'columns'):
            self.check('2026-01-07', duties=wrong)
        short = write_duties(self.root / 'short.csv',
                             [('V1', 'T1', '2026-01-07')])
        with self.assertRaisesRegex(ValueError, 'row 2'):
            self.check('2026-01-07', duties=short)
        long = write_duties(self.root / 'long.csv',
                            [('V1', 'T1', '2026-01-07', '2026-01-07', 'x')])
        with self.assertRaisesRegex(ValueError, 'row 2'):
            self.check('2026-01-07', duties=long)
        links_wrong = write_links(self.root / 'lh.csv', [('B', 'C', '1')],
                                  header=['to_stop', 'from_stop', 'seconds'])
        with self.assertRaisesRegex(ValueError, 'columns'):
            self.check('2026-01-07', links=links_wrong)

    def test_bad_encoding(self):
        path = self.root / 'latin.csv'
        path.write_bytes(
            b'vehicle_id,trip_id,start_date,end_date\nV\xe9,T1,2026-01-07,'
            b'2026-01-07\n')
        with self.assertRaisesRegex(ValueError, 'UTF-8'):
            self.check('2026-01-07', duties=path)

    def test_bom_is_accepted(self):
        path = self.root / 'bom.csv'
        with path.open('w', encoding='utf-8-sig', newline='') as stream:
            writer = csv.writer(stream)
            writer.writerow(DUTY_HEADER)
            writer.writerows(self.good_duties)
        self.assertTrue(self.check('2026-01-07', duties=path)['feasible'])
        links = self.root / 'bom-links.csv'
        with links.open('w', encoding='utf-8-sig', newline='') as stream:
            writer = csv.writer(stream)
            writer.writerow(LINK_HEADER)
            writer.writerow(('B', 'C', '5400'))
        self.assertTrue(self.check('2026-01-07', duties=path,
                                   links=links)['feasible'])

    def test_row_limit(self):
        rows = [('V', 'T1', '2026-01-07', '2026-01-07')] * 20001
        with self.assertRaisesRegex(ValueError, '20000'):
            self.check('2026-01-07', duties=self.duties_file(rows))

    def test_missing_files(self):
        with self.assertRaises(ValueError):
            self.check('2026-01-07', duties=self.root / 'absent.csv')
        with self.assertRaises(ValueError):
            self.check('2026-01-07', links=self.root / 'absent.csv')

    def test_invalid_query_dates(self):
        for value in ('2026-1-7', '20260107', '', None,
                      '2026-01-07T00:00:00'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.check(value)
        with self.assertRaises(ValueError):
            self.check('2026-01-08', '2026-01-07')

    def test_missing_corrupt_and_foreign_archives(self):
        with self.assertRaises(ValueError):
            duty_check(self.root / 'absent.sqlite', self.duties, self.links,
                       '2026-01-07', '2026-01-07')
        corrupt = self.root / 'corrupt.sqlite'
        corrupt.write_bytes(b'not a database')
        with self.assertRaises(ValueError):
            duty_check(corrupt, self.duties, self.links,
                       '2026-01-07', '2026-01-07')
        revision = self.root / 'revision.sqlite'
        revision_init(self.db, revision)
        with self.assertRaises(ValueError):
            duty_check(revision, self.duties, self.links,
                       '2026-01-07', '2026-01-07')

    def test_full_validation_happens_for_empty_range(self):
        # The queried range has no instances at all, but a malformed row must
        # still be rejected.
        bad = self.duties_file([('V', 'NOPE', '2020-01-01', '2020-01-01')])
        with self.assertRaises(ValueError):
            self.check('2026-01-07', duties=bad)

    def test_failed_and_successful_checks_write_nothing(self):
        digest = hashlib.sha256(self.db.read_bytes()).hexdigest()
        duties_digest = hashlib.sha256(self.duties.read_bytes()).hexdigest()
        links_digest = hashlib.sha256(self.links.read_bytes()).hexdigest()
        bad = self.links_file([('B', 'Z', '1')], 'write-check-bad.csv')
        bad_digest = hashlib.sha256(bad.read_bytes()).hexdigest()
        before = sorted(p.name for p in self.root.iterdir())
        attempts = [
            lambda: self.check('2026-01-07', links=bad),
            lambda: self.check('2026-1-7'),
            lambda: duty_check(self.root / 'absent.sqlite', self.duties,
                               self.links, '2026-01-07', '2026-01-07'),
        ]
        for attempt in attempts:
            with self.assertRaises(ValueError):
                attempt()
        self.check('2026-01-07')
        self.assertEqual(hashlib.sha256(self.db.read_bytes()).hexdigest(),
                         digest)
        self.assertEqual(hashlib.sha256(self.duties.read_bytes()).hexdigest(),
                         duties_digest)
        self.assertEqual(hashlib.sha256(self.links.read_bytes()).hexdigest(),
                         links_digest)
        self.assertEqual(hashlib.sha256(bad.read_bytes()).hexdigest(),
                         bad_digest)
        self.assertEqual(sorted(p.name for p in self.root.iterdir()), before)


class CliTests(DutyBase):
    def invoke(self, *args):
        return subprocess.run(
            [sys.executable, '-m', MODULE, *map(str, args)],
            capture_output=True, text=True, timeout=30, cwd=str(ROOT),
            env={**os.environ, 'PYTHONPATH': str(ROOT / 'src')})

    def test_conflict_still_exits_zero_with_one_json_document(self):
        self.build_base()
        result = self.invoke('duty-check', '--db', self.db,
                             '--duties', self.duties, '--links', self.links,
                             '--from', '2026-01-07', '--to', '2026-01-07')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, '')
        payload = json.loads(result.stdout)
        self.assertEqual(set(payload), {'feasible', 'duplicate_assignments',
                                        'connections'})
        self.assertFalse(payload['feasible'])
        self.assertEqual(payload['connections'][0]['shortfall_seconds'], 1200)

    def test_cli_matches_python_api_and_round_trips_json(self):
        self.build_base()
        result = self.invoke('duty-check', '--db', self.db,
                             '--duties', self.duties, '--links', self.links,
                             '--from', '2026-01-06', '--to', '2026-01-07')
        self.assertEqual(result.returncode, 0, result.stderr)
        expected = self.check('2026-01-06', '2026-01-07')
        self.assertEqual(json.loads(result.stdout), expected)
        self.assertEqual(json.loads(json.dumps(expected)), expected)

    def test_errors_exit_two_with_empty_stdout(self):
        self.build_base()
        bad = write_duties(self.root / 'bad.csv',
                           [('V', 'T1', '2026-01-07')])
        missing = self.root / 'absent.sqlite'
        cases = [
            ['--db', self.db, '--duties', bad, '--links', self.links,
             '--from', '2026-01-07', '--to', '2026-01-07'],
            ['--db', missing, '--duties', self.duties, '--links', self.links,
             '--from', '2026-01-07', '--to', '2026-01-07'],
            ['--db', self.db, '--duties', self.duties, '--links', self.links,
             '--from', '2026-1-7', '--to', '2026-01-07'],
        ]
        for extra in cases:
            with self.subTest(extra=extra):
                result = self.invoke('duty-check', *extra)
                self.assertEqual(result.returncode, 2)
                self.assertEqual(result.stdout, '')
                self.assertTrue(result.stderr)
        self.assertFalse(missing.exists())


if __name__ == '__main__':
    unittest.main()
