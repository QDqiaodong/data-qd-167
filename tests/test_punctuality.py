import csv
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest

from transit_feed.feed import import_feed
from transit_feed.revision import revision_apply, revision_init
from transit_feed.punctuality import (punctuality_init, punctuality_report,
                                      punctuality_revise, punctuality_show)

ROOT = Path(__file__).resolve().parents[1]
MODULE = 'transit_feed'

OBS_HEADER = ['event_id', 'trip_id', 'service_date', 'stop_id',
              'observed_arrival']


def write_feed(root, stops, trips, calendar, exceptions, stop_times):
    root.mkdir(parents=True, exist_ok=True)

    def table(name, header, rows):
        with (root / name).open('w', encoding='utf-8', newline='') as stream:
            writer = csv.writer(stream)
            writer.writerow(header)
            writer.writerows(rows)

    table('stops.txt', ['stop_id', 'stop_name'], stops)
    table('trips.txt', ['trip_id', 'service_id'], trips)
    table('calendar.txt',
          ['service_id', 'monday', 'tuesday', 'wednesday', 'thursday',
           'friday', 'saturday', 'sunday', 'start_date', 'end_date'],
          calendar)
    table('calendar_dates.txt',
          ['service_id', 'date', 'exception_type'], exceptions)
    table('stop_times.txt',
          ['trip_id', 'arrival_time', 'departure_time', 'stop_id',
           'stop_sequence'], stop_times)


def write_csv(path, rows, header=OBS_HEADER, bom=False):
    with Path(path).open('w', encoding='utf-8', newline='') as stream:
        if bom:
            stream.write('﻿')
        writer = csv.writer(stream)
        writer.writerow(header)
        writer.writerows(rows)


def all_days(service='WK', start='18000101', end='24001231'):
    return [service, '1', '1', '1', '1', '1', '1', '1', start, end]


class PunctualityBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.feed = self.root / 'feed'
        self.base = self.root / 'base.sqlite'
        self.ledger = self.root / 'reports.sqlite'

    def init_ledger(self):
        self.assertEqual(punctuality_init(self.ledger), {'reports': 0})

    def trip(self, report, service_date, trip_id):
        matches = [trip for trip in report['trips']
                   if trip['service_date'] == service_date
                   and trip['trip_id'] == trip_id]
        self.assertEqual(len(matches), 1)
        return matches[0]

    def report(self, report_id, csv_name='obs.csv', **kwargs):
        options = dict(from_date='2026-01-01', to_date='2026-01-31')
        options.update(kwargs)
        return punctuality_report(self.base, self.ledger, report_id,
                                  self.root / csv_name, **options)

    def decisions(self, name, confirmations):
        path = self.root / name
        path.write_text(json.dumps({'confirmations': confirmations}),
                        encoding='utf-8')
        return path


class InitTests(PunctualityBase):
    def build_base(self):
        write_feed(self.feed,
                   [['A', 'Alpha'], ['B', 'Beta']],
                   [['T', 'WK']], [all_days()], [],
                   [['T', '08:00:00', '08:00:00', 'A', 1],
                    ['T', '09:00:00', '09:00:00', 'B', 2]])
        import_feed(self.feed, self.base)

    def test_init_creates_empty_ledger(self):
        self.init_ledger()
        with sqlite3.connect(self.ledger) as conn:
            tables = {row[0] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            self.assertEqual(tables,
                             {'punctuality_meta', 'punctuality_report'})
            self.assertEqual(
                conn.execute('SELECT COUNT(*) FROM punctuality_report')
                .fetchone()[0], 0)

    def test_init_refuses_existing_target_and_symlink(self):
        self.build_base()
        target = self.root / 'occupied'
        target.write_bytes(b'old')
        with self.assertRaisesRegex(ValueError, 'must not exist'):
            punctuality_init(target)
        self.assertEqual(target.read_bytes(), b'old')
        link = self.root / 'link'
        link.symlink_to(target)
        with self.assertRaisesRegex(ValueError, 'must not exist'):
            punctuality_init(link)
        self.assertTrue(link.is_symlink())

    def test_init_failure_leaves_no_target_or_temp_files(self):
        blocker = self.root / 'afile'
        blocker.write_bytes(b'x')
        target = blocker / 'ledger.sqlite'
        with self.assertRaises(OSError):
            punctuality_init(target)
        self.assertFalse(target.exists())
        self.assertEqual(blocker.read_bytes(), b'x')
        self.assertEqual(list(self.root.glob('*.sqlite*')), [])

    def test_foreign_sqlite_and_missing_ledger_rejected(self):
        self.build_base()
        foreign = self.root / 'foreign.sqlite'
        with sqlite3.connect(foreign) as conn:
            conn.execute('CREATE TABLE junk(x)')
        write_csv(self.root / 'obs.csv',
                  [['E1', 'T', '2026-01-05', 'A', '08:00:00']])
        with self.assertRaisesRegex(ValueError, 'punctuality ledger'):
            punctuality_report(self.base, foreign, 'R',
                               self.root / 'obs.csv',
                               '2026-01-01', '2026-01-31')
        missing = self.root / 'missing.sqlite'
        with self.assertRaises(ValueError):
            punctuality_show(missing, 'R')
        self.assertFalse(missing.exists())


class PairingTests(PunctualityBase):
    def build_base(self):
        write_feed(self.feed,
                   [['A', 'Alpha'], ['B', 'Beta'], ['Z', 'Zeta']],
                   [['T', 'WK'], ['U', 'WK']], [all_days()], [],
                   [['T', '08:00:00', '08:00:00', 'A', 10],
                    ['T', '08:05:00', '08:05:00', 'A', 20],
                    ['T', '08:10:00', '08:10:00', 'A', 30],
                    ['T', '08:20:00', '08:20:00', 'B', 40],
                    ['U', '10:00:00', '10:00:00', 'A', 1],
                    ['U', '11:00:00', '11:00:00', 'B', 2]])
        import_feed(self.feed, self.base)
        self.init_ledger()

    def test_repeated_stop_with_missing_middle_event(self):
        self.build_base()
        write_csv(self.root / 'obs.csv', [
            ['E1', 'T', '2026-01-05', 'A', '08:00:00'],
            ['E3', 'T', '2026-01-05', 'A', '08:10:00'],
        ])
        report = self.report('R1')
        trip = self.trip(report, '2026-01-05', 'T')
        self.assertEqual(
            [(m['event_id'], m['stop_sequence']) for m in trip['matched']],
            [('E1', 10), ('E3', 30)])
        self.assertEqual([m['stop_sequence'] for m in trip['missed']],
                         [20, 40])

    def test_same_timestamp_events_keep_stable_event_id_order(self):
        self.build_base()
        # Trip U visits A once (seq 1) then B (seq 2). Two equal-time events
        # at A can only yield one pair at identical cost, so the final tie
        # break picks the lexicographically smaller event_id.
        write_csv(self.root / 'obs.csv', [
            ['E2', 'U', '2026-01-05', 'A', '10:00:00'],
            ['E1', 'U', '2026-01-05', 'A', '10:00:00'],
        ])
        report = self.report('R1')
        trip = self.trip(report, '2026-01-05', 'U')
        self.assertEqual(
            [(m['event_id'], m['stop_sequence']) for m in trip['matched']],
            [('E1', 1)])
        self.assertEqual(
            [u['event_id'] for u in trip['unmatched_observations']], ['E2'])

    def test_global_pairing_differs_from_greedy_nearest_match(self):
        self.build_base()
        # A greedy nearest-time rule pairs E1 with seq 20 (40s away) and can
        # never reach three pairs; the global optimum pairs all three.
        write_csv(self.root / 'obs.csv', [
            ['E1', 'T', '2026-01-05', 'A', '08:00:40'],
            ['E2', 'T', '2026-01-05', 'A', '08:02:30'],
            ['E3', 'T', '2026-01-05', 'A', '08:10:00'],
        ])
        report = self.report('R1')
        trip = self.trip(report, '2026-01-05', 'T')
        self.assertEqual(len(trip['matched']), 3)
        self.assertEqual(
            [(m['event_id'], m['stop_sequence']) for m in trip['matched']],
            [('E1', 10), ('E2', 20), ('E3', 30)])

    def test_event_at_stop_the_trip_never_serves_is_unmatched(self):
        self.build_base()
        write_csv(self.root / 'obs.csv', [
            ['E1', 'U', '2026-01-05', 'Z', '10:00:00'],
            ['E2', 'U', '2026-01-05', 'A', '10:00:00'],
        ])
        report = self.report('R1')
        trip = self.trip(report, '2026-01-05', 'U')
        self.assertEqual(
            [(m['event_id'], m['stop_sequence']) for m in trip['matched']],
            [('E2', 1)])
        self.assertEqual(
            [u['event_id'] for u in trip['unmatched_observations']], ['E1'])
        self.assertEqual([m['stop_sequence'] for m in trip['missed']], [2])

    def test_spec_example_review_realigns_other_events_and_stats(self):
        self.build_base()
        write_csv(self.root / 'obs.csv', [
            ['E1', 'T', '2026-01-05', 'A', '08:04:00'],
            ['E2', 'T', '2026-01-05', 'A', '08:10:00'],
        ])
        initial = self.report('R1', late_tolerance=180)
        trip = self.trip(initial, '2026-01-05', 'T')
        self.assertEqual(
            [(m['event_id'], m['stop_sequence']) for m in trip['matched']],
            [('E1', 20), ('E2', 30)])
        self.assertEqual([m['stop_sequence'] for m in trip['missed']],
                         [10, 40])
        decisions = self.decisions('d.json', [
            {'event_id': 'E2', 'stop_sequence': 20}])
        review = punctuality_revise(self.ledger, 'R1', 'R2', decisions)
        self.assertEqual(review['review_of'], 'R1')
        trip = self.trip(review, '2026-01-05', 'T')
        self.assertEqual(
            [(m['event_id'], m['stop_sequence'], m['status'], m['confirmed'])
             for m in trip['matched']],
            [('E1', 10, 'late', False), ('E2', 20, 'late', True)])
        self.assertEqual([m['stop_sequence'] for m in trip['missed']],
                         [30, 40])
        # Confirmation does not force on_time: both matches are honestly late.
        self.assertEqual(review['summary']['late'], 2)

    def test_trip_without_confirmation_keeps_original_pairing(self):
        self.build_base()
        write_csv(self.root / 'obs.csv', [
            ['E1', 'T', '2026-01-05', 'A', '08:00:00'],
            ['E2', 'T', '2026-01-05', 'A', '08:10:00'],
            ['F1', 'U', '2026-01-05', 'A', '10:00:00'],
        ])
        initial = self.report('R1')
        decisions = self.decisions('d.json', [
            {'event_id': 'E1', 'stop_sequence': 10}])
        review = punctuality_revise(self.ledger, 'R1', 'R2', decisions)
        initial_u = self.trip(initial, '2026-01-05', 'U')
        review_u = self.trip(review, '2026-01-05', 'U')
        self.assertEqual(initial_u['matched'], review_u['matched'])
        self.assertEqual(initial_u['missed'], review_u['missed'])
        self.assertEqual(initial_u['unmatched_observations'],
                         review_u['unmatched_observations'])


class ClassificationTests(PunctualityBase):
    def build_base(self):
        write_feed(self.feed,
                   [['A', 'Alpha'], ['B', 'Beta'], ['C', 'Gamma']],
                   [['T', 'WK']], [all_days()], [],
                   [['T', '08:00:00', '08:00:00', 'A', 1],
                    ['T', '09:00:00', '09:00:00', 'B', 2],
                    ['T', '10:00:00', '10:00:00', 'C', 3]])
        import_feed(self.feed, self.base)
        self.init_ledger()

    def test_early_on_time_late_boundaries_are_inclusive(self):
        self.build_base()
        write_csv(self.root / 'obs.csv', [
            ['EB', 'T', '2026-01-05', 'A', '07:58:59'],   # -61 early
            ['EO', 'T', '2026-01-05', 'B', '08:59:00'],   # -60 on time
            ['LO', 'T', '2026-01-05', 'C', '10:05:00'],   # +300 on time
        ])
        report = self.report('R1')
        statuses = {m['event_id']: m['status']
                    for m in report['trips'][0]['matched']}
        self.assertEqual(statuses,
                         {'EB': 'early', 'EO': 'on_time', 'LO': 'on_time'})
        write_csv(self.root / 'obs2.csv', [
            ['LB', 'T', '2026-01-05', 'A', '08:05:01'],   # +301 late
        ])
        report = self.report('R2', csv_name='obs2.csv')
        self.assertEqual(report['trips'][0]['matched'][0]['status'], 'late')

    def test_zero_tolerances_classify_strictly(self):
        self.build_base()
        write_csv(self.root / 'obs.csv', [
            ['E', 'T', '2026-01-05', 'A', '08:00:00'],
        ])
        report = self.report('R1', early_tolerance=0, late_tolerance=0)
        self.assertEqual(report['trips'][0]['matched'][0]['status'],
                         'on_time')

    def test_tolerance_type_rules_reject_bools_and_strings(self):
        self.build_base()
        write_csv(self.root / 'obs.csv',
                  [['E', 'T', '2026-01-05', 'A', '08:00:00']])
        for kwargs in (dict(early_tolerance=True),
                       dict(early_tolerance='60'),
                       dict(late_tolerance=1.5),
                       dict(known_seq=True),
                       dict(known_seq='0')):
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(ValueError):
                    self.report('X', **kwargs)
        with self.assertRaises(ValueError):
            self.report('X', early_tolerance=-1)
        with self.assertRaises(ValueError):
            self.report('X', late_tolerance=86401)


class ServiceDayTests(PunctualityBase):
    def build_base(self):
        write_feed(self.feed,
                   [['A', 'Alpha'], ['B', 'Beta']],
                   [['T', 'WK'], ['N', 'WK']], [all_days()],
                   [['WK', '20260106', '2']],
                   [['T', '08:00:00', '08:00:00', 'A', 1],
                    ['T', '09:00:00', '09:00:00', 'B', 2],
                    ['N', '24:30:00', '24:30:00', 'A', 1],
                    ['N', '25:00:00', '25:00:00', 'B', 2]])
        import_feed(self.feed, self.base)
        self.init_ledger()

    def test_times_above_24h_stay_on_service_day_clock(self):
        self.build_base()
        write_csv(self.root / 'obs.csv', [
            ['N1', 'N', '2026-01-05', 'A', '24:31:00'],
            ['N2', 'N', '2026-01-05', 'B', '25:02:00'],
        ], bom=True)
        report = self.report('R1')
        trip = self.trip(report, '2026-01-05', 'N')
        self.assertEqual(
            [(m['event_id'], m['observed_arrival'], m['deviation_seconds'])
             for m in trip['matched']],
            [('N1', '24:31:00', 60), ('N2', '25:02:00', 120)])

    def test_events_on_inactive_service_are_kept_unmatched(self):
        self.build_base()
        write_csv(self.root / 'obs.csv', [
            ['X1', 'T', '2026-01-06', 'A', '08:10:00'],
            ['X2', 'T', '2026-01-06', 'B', '09:10:00'],
        ])
        report = self.report('R1')
        trip = self.trip(report, '2026-01-06', 'T')
        self.assertFalse(trip['service_active'])
        self.assertEqual(trip['matched'], [])
        self.assertEqual(trip['missed'], [])
        self.assertEqual(
            sorted(u['event_id'] for u in trip['unmatched_observations']),
            ['X1', 'X2'])
        self.assertEqual(report['summary']['scheduled_visits'], 0)
        self.assertEqual(report['summary']['unmatched_observations'], 2)

    def test_header_only_csv_returns_zero_report(self):
        self.build_base()
        write_csv(self.root / 'empty.csv', [])
        report = punctuality_report(self.base, self.ledger, 'EMPTY',
                                    self.root / 'empty.csv',
                                    '2026-01-01', '2026-01-31')
        self.assertEqual(report, {
            'report_id': 'EMPTY', 'review_of': None, 'known_seq': 0,
            'summary': {'scheduled_visits': 0, 'matched_visits': 0,
                        'on_time': 0, 'early': 0, 'late': 0,
                        'missed_visits': 0, 'unmatched_observations': 0},
            'trips': []})


class RevisionSnapshotTests(PunctualityBase):
    def build_base(self):
        write_feed(self.feed,
                   [['A', 'Alpha'], ['B', 'Beta']],
                   [['T', 'WK']], [all_days()], [],
                   [['T', '08:00:00', '08:00:00', 'A', 1],
                    ['T', '09:00:00', '09:00:00', 'B', 2]])
        import_feed(self.feed, self.base)
        self.revision = self.root / 'revision.sqlite'
        revision_init(self.base, self.revision)
        self.init_ledger()

    def correction(self, arrival):
        return {'changes': [
            {'op': 'put', 'correction_id': 'C1', 'trip_id': 'T',
             'stop_sequence': 2, 'from': '2026-01-01', 'to': '2026-12-31',
             'arrival': arrival, 'departure': arrival}]}

    def test_knowledge_sequence_changes_deviation(self):
        self.build_base()
        write_csv(self.root / 'obs.csv',
                  [['E1', 'T', '2026-01-05', 'B', '08:58:59']])
        at_zero = punctuality_report(
            self.revision, self.ledger, 'K0', self.root / 'obs.csv',
            '2026-01-05', '2026-01-05', known_seq=0)
        revision_apply(self.revision, self.correction('08:59:00'))
        at_one_explicit = punctuality_report(
            self.revision, self.ledger, 'K1', self.root / 'obs.csv',
            '2026-01-05', '2026-01-05', known_seq=1)
        at_one_default = punctuality_report(
            self.revision, self.ledger, 'KD', self.root / 'obs.csv',
            '2026-01-05', '2026-01-05')
        self.assertEqual(at_zero['known_seq'], 0)
        self.assertEqual(at_one_explicit['known_seq'], 1)
        self.assertEqual(at_one_default['known_seq'], 1)
        deviation_zero = at_zero['trips'][0]['matched'][0]['deviation_seconds']
        deviation_one = at_one_explicit['trips'][0]['matched'][0][
            'deviation_seconds']
        self.assertEqual((deviation_zero, deviation_one), (-61, -1))

    def test_explicit_and_omitted_sequence_are_different_identities(self):
        self.build_base()
        revision_apply(self.revision, self.correction('08:59:00'))
        write_csv(self.root / 'obs.csv',
                  [['E1', 'T', '2026-01-05', 'B', '08:58:59']])
        punctuality_report(self.revision, self.ledger, 'SAME',
                           self.root / 'obs.csv',
                           '2026-01-05', '2026-01-05')
        with self.assertRaisesRegex(ValueError, 'different request'):
            punctuality_report(self.revision, self.ledger, 'SAME',
                               self.root / 'obs.csv',
                               '2026-01-05', '2026-01-05', known_seq=1)

    def test_baseline_archive_only_accepts_sequence_zero(self):
        self.build_base()
        write_csv(self.root / 'obs.csv',
                  [['E1', 'T', '2026-01-05', 'A', '08:00:00']])
        with self.assertRaisesRegex(ValueError, 'known_seq 0'):
            punctuality_report(self.base, self.ledger, 'X',
                               self.root / 'obs.csv',
                               '2026-01-05', '2026-01-05', known_seq=1)
        report = punctuality_report(self.base, self.ledger, 'OK',
                                    self.root / 'obs.csv',
                                    '2026-01-05', '2026-01-05', known_seq=0)
        self.assertEqual(report['known_seq'], 0)

    def test_future_sequence_rejected(self):
        self.build_base()
        write_csv(self.root / 'obs.csv',
                  [['E1', 'T', '2026-01-05', 'A', '08:00:00']])
        with self.assertRaisesRegex(ValueError, 'knowledge sequence'):
            punctuality_report(self.revision, self.ledger, 'X',
                               self.root / 'obs.csv',
                               '2026-01-05', '2026-01-05', known_seq=5)


class ValidationTests(PunctualityBase):
    def build_base(self):
        write_feed(self.feed,
                   [['A', 'Alpha'], ['B', 'Beta']],
                   [['T', 'WK']], [all_days()], [],
                   [['T', '08:00:00', '08:00:00', 'A', 1],
                    ['T', '09:00:00', '09:00:00', 'B', 2]])
        import_feed(self.feed, self.base)
        self.init_ledger()

    def reject(self, content, name='bad.csv'):
        path = self.root / name
        path.write_text(content, encoding='utf-8')
        with self.assertRaises(ValueError):
            punctuality_report(self.base, self.ledger, 'BAD-' + name,
                                path, '2026-01-01', '2026-01-31')

    def test_invalid_rows_are_rejected_as_a_whole(self):
        self.build_base()
        header = ','.join(OBS_HEADER) + '\n'
        self.reject('event_id,trip_id,service_date,stop_id\n')
        self.reject(header + 'E1,T,2026-01-05,A\n')
        self.reject(header + 'E1,T,2026-1-05,A,08:00:00\n')
        self.reject(header + 'E1,T,2026-01-05,A,48:00:00\n')
        self.reject(header + 'E1,T,2026-01-05,A,9:00:00\n')
        self.reject(header + 'E1,,2026-01-05,A,08:00:00\n')
        self.reject(header + ' E1,T,2026-01-05,A,08:00:00\n')
        self.reject(header + 'E1,T,2026-01-05,A,08:00:00\n'
                            + 'E1,T,2026-01-05,B,09:00:00\n')
        self.reject(header + 'E1,ZZ,2026-01-05,A,08:00:00\n')
        self.reject(header + 'E1,T,2026-01-05,ZZ,08:00:00\n')

    def test_out_of_range_dates_rejected_even_when_other_rows_valid(self):
        self.build_base()
        self.reject(','.join(OBS_HEADER) + '\n'
                    'E1,T,2026-02-05,A,08:00:00\n')
        with self.assertRaisesRegex(ValueError, 'from date'):
            punctuality_report(self.base, self.ledger, 'X',
                               self.root / 'missing.csv',
                               '2026-01-10', '2026-01-05')

    def test_size_and_row_limits(self):
        self.build_base()
        header = ','.join(OBS_HEADER) + '\n'
        many = self.root / 'many.csv'
        many.write_text(
            header + ''.join(
                'E%d,T,2026-01-05,A,08:00:00\n' % i for i in range(20001)),
            encoding='utf-8')
        with self.assertRaisesRegex(ValueError, '20000'):
            punctuality_report(self.base, self.ledger, 'X', many,
                               '2026-01-01', '2026-01-31')
        big = self.root / 'big.csv'
        with big.open('wb') as stream:
            stream.write(header.encode())
            stream.write(b'x' * 8000001)
        with self.assertRaisesRegex(ValueError, '8MB'):
            punctuality_report(self.base, self.ledger, 'X', big,
                               '2026-01-01', '2026-01-31')

    def test_failure_occupies_no_id_and_keeps_ledger_bytes(self):
        self.build_base()
        write_csv(self.root / 'good.csv',
                  [['E1', 'T', '2026-01-05', 'A', '08:00:00']])
        self.report('KEPT', csv_name='good.csv')
        digest = hashlib.sha256(self.ledger.read_bytes()).hexdigest()
        self.reject(','.join(OBS_HEADER) + '\n'
                    'E1,ZZ,2026-01-05,A,08:00:00\n', name='worse.csv')
        with sqlite3.connect(self.ledger) as conn:
            ids = [row[0] for row in conn.execute(
                'SELECT report_id FROM punctuality_report')]
        self.assertEqual(ids, ['KEPT'])
        self.assertEqual(hashlib.sha256(self.ledger.read_bytes()).hexdigest(),
                         digest)

    def test_unknown_report_id_and_missing_schedule(self):
        self.build_base()
        with self.assertRaisesRegex(ValueError, 'unknown report_id'):
            punctuality_show(self.ledger, 'GHOST')
        write_csv(self.root / 'obs.csv',
                  [['E1', 'T', '2026-01-05', 'A', '08:00:00']])
        with self.assertRaises(ValueError):
            punctuality_report(self.root / 'nope.sqlite', self.ledger, 'X',
                               self.root / 'obs.csv',
                               '2026-01-01', '2026-01-31')


class IdentityAndReplayTests(PunctualityBase):
    def build_base(self):
        write_feed(self.feed,
                   [['A', 'Alpha'], ['B', 'Beta']],
                   [['T', 'WK']], [all_days()], [],
                   [['T', '08:00:00', '08:00:00', 'A', 1],
                    ['T', '09:00:00', '09:00:00', 'B', 2]])
        import_feed(self.feed, self.base)
        self.init_ledger()

    def test_show_and_replay_survive_schedule_move_and_modification(self):
        self.build_base()
        obs = self.root / 'obs.csv'
        write_csv(obs, [['E1', 'T', '2026-01-05', 'A', '08:01:00']])
        original = self.report('R1')
        # Move the schedule away entirely: the original request path no
        # longer exists, but identity comparison never requires its presence.
        self.base.unlink()
        self.assertEqual(punctuality_show(self.ledger, 'R1'), original)
        # Exact replay at the same (now missing) path returns the stored
        # report without reopening the schedule archive.
        self.assertEqual(
            punctuality_report(self.base, self.ledger, 'R1', obs,
                               '2026-01-01', '2026-01-31'),
            original)

    def test_parameter_and_byte_changes_conflict_while_replay_matches(self):
        self.build_base()
        obs = self.root / 'obs.csv'
        write_csv(obs, [['E1', 'T', '2026-01-05', 'A', '08:01:00']])
        original = self.report('R1')
        self.assertEqual(self.report('R1'), original)
        with self.assertRaisesRegex(ValueError, 'different request'):
            self.report('R1', early_tolerance=30)
        with self.assertRaisesRegex(ValueError, 'different request'):
            self.report('R1', to_date='2026-02-01')
        write_csv(obs, [['E1', 'T', '2026-01-05', 'A', '08:01:01']])
        with self.assertRaisesRegex(ValueError, 'different request'):
            self.report('R1')
        # restore the exact original bytes: replay works again
        write_csv(obs, [['E1', 'T', '2026-01-05', 'A', '08:01:00']])
        self.assertEqual(self.report('R1'), original)

    def test_new_id_reads_current_csv_bytes_on_successive_calls(self):
        self.build_base()
        obs = self.root / 'obs.csv'
        write_csv(obs, [['E1', 'T', '2026-01-05', 'A', '08:01:00']])
        first = self.report('ONE')
        write_csv(obs, [['E1', 'T', '2026-01-05', 'A', '08:02:00']])
        second = self.report('TWO')
        self.assertEqual(
            first['trips'][0]['matched'][0]['deviation_seconds'], 60)
        self.assertEqual(
            second['trips'][0]['matched'][0]['deviation_seconds'], 120)
        # the first report is permanently the old bytes' answer
        self.assertEqual(punctuality_show(self.ledger, 'ONE'), first)


class ReviewTests(PunctualityBase):
    def build_base(self):
        write_feed(self.feed,
                   [['A', 'Alpha'], ['B', 'Beta']],
                   [['T', 'WK']], [all_days()], [],
                   [['T', '08:00:00', '08:00:00', 'A', 10],
                    ['T', '08:05:00', '08:05:00', 'A', 20],
                    ['T', '08:10:00', '08:10:00', 'A', 30],
                    ['T', '08:20:00', '08:20:00', 'B', 40]])
        import_feed(self.feed, self.base)
        self.init_ledger()

    def test_review_works_after_both_sources_move_away(self):
        self.build_base()
        obs = self.root / 'obs.csv'
        write_csv(obs, [
            ['E1', 'T', '2026-01-05', 'A', '08:04:00'],
            ['E2', 'T', '2026-01-05', 'A', '08:10:00']])
        initial = self.report('R1', late_tolerance=180)
        decisions = self.decisions('d.json', [
            {'event_id': 'E2', 'stop_sequence': 20}])
        self.base.rename(self.root / 'gone.sqlite')
        obs.rename(self.root / 'gone.csv')
        review = punctuality_revise(self.ledger, 'R1', 'R2', decisions)
        self.assertEqual(
            [(m['event_id'], m['stop_sequence'])
             for m in self.trip(review, '2026-01-05', 'T')['matched']],
            [('E1', 10), ('E2', 20)])
        self.assertEqual(punctuality_show(self.ledger, 'R1'), initial)

    def test_conflicting_confirmations_occupy_no_id(self):
        self.build_base()
        write_csv(self.root / 'obs.csv', [
            ['E1', 'T', '2026-01-05', 'A', '08:04:00'],
            ['E2', 'T', '2026-01-05', 'A', '08:10:00']])
        self.report('R1', late_tolerance=180)
        inverted = self.decisions('inverted.json', [
            {'event_id': 'E1', 'stop_sequence': 30},
            {'event_id': 'E2', 'stop_sequence': 20}])
        digest = hashlib.sha256(self.ledger.read_bytes()).hexdigest()
        with self.assertRaisesRegex(ValueError, 'relative sequence'):
            punctuality_revise(self.ledger, 'R1', 'BAD', inverted)
        with self.assertRaisesRegex(ValueError, 'unknown report_id'):
            punctuality_show(self.ledger, 'BAD')
        self.assertEqual(hashlib.sha256(self.ledger.read_bytes()).hexdigest(),
                         digest)
        # unknown event, wrong stop, inactive trip, reused event/visit
        cases = [
            [{'event_id': 'GHOST', 'stop_sequence': 10}],
            [{'event_id': 'E1', 'stop_sequence': 40}],
            [{'event_id': 'E1', 'stop_sequence': 20},
             {'event_id': 'E1', 'stop_sequence': 30}],
            [{'event_id': 'E1', 'stop_sequence': 20},
             {'event_id': 'E2', 'stop_sequence': 20}],
        ]
        for index, confirmations in enumerate(cases):
            path = self.decisions('bad%d.json' % index, confirmations)
            with self.assertRaises(ValueError):
                punctuality_revise(self.ledger, 'R1',
                                   'BAD%d' % index, path)

    def test_review_replay_is_identical_and_never_rewrites_reports(self):
        self.build_base()
        write_csv(self.root / 'obs.csv', [
            ['E1', 'T', '2026-01-05', 'A', '08:04:00'],
            ['E2', 'T', '2026-01-05', 'A', '08:10:00']])
        self.report('R1', late_tolerance=180)
        decisions = self.decisions('d.json', [
            {'event_id': 'E2', 'stop_sequence': 20}])
        first = punctuality_revise(self.ledger, 'R1', 'RV', decisions)
        # Same confirmation set, different file, key order and array layout.
        reordered = self.root / 'd2.json'
        reordered.write_text(
            '{"confirmations": [{"stop_sequence": 20, "event_id": "E2"}]}',
            encoding='utf-8')
        digest = hashlib.sha256(self.ledger.read_bytes()).hexdigest()
        again = punctuality_revise(self.ledger, 'R1', 'RV', reordered)
        self.assertEqual(again, first)
        self.assertEqual(hashlib.sha256(self.ledger.read_bytes()).hexdigest(),
                         digest)
        # A second independent review of the same initial report is allowed.
        other_decisions = self.decisions('d3.json', [
            {'event_id': 'E1', 'stop_sequence': 10}])
        second = punctuality_revise(self.ledger, 'R1', 'RV2',
                                    other_decisions)
        self.assertEqual(second['review_of'], 'R1')
        # Reviews cannot be stacked on reviews.
        with self.assertRaisesRegex(ValueError, 'initial report'):
            punctuality_revise(self.ledger, 'RV', 'RV3', decisions)

    def test_decisions_file_structure_validation(self):
        self.build_base()
        write_csv(self.root / 'obs.csv',
                  [['E1', 'T', '2026-01-05', 'A', '08:04:00']])
        self.report('R1')
        bad_payloads = [
            {'confirmations': []},
            {'confirmations': [{}]},
            {'confirmations': [{'event_id': 'E1'}]},
            {'confirmations': [{'event_id': 'E1', 'stop_sequence': 10,
                                'extra': 1}]},
            {'confirmations': [{'event_id': 'E1', 'stop_sequence': True}]},
            {'confirmations': [{'event_id': 10, 'stop_sequence': 10}]},
            {'other': []},
        ]
        for index, payload in enumerate(bad_payloads):
            path = self.root / ('p%d.json' % index)
            path.write_text(json.dumps(payload), encoding='utf-8')
            with self.assertRaises(ValueError):
                punctuality_revise(self.ledger, 'R1',
                                   'PB%d' % index, path)
        duplicate_keys = self.root / 'dup.json'
        duplicate_keys.write_text(
            '{"confirmations":[{"event_id":"E1","stop_sequence":10}],'
            '"confirmations":[]}', encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'duplicate JSON key'):
            punctuality_revise(self.ledger, 'R1', 'PDUP', duplicate_keys)


class CliTests(unittest.TestCase):
    def invoke(self, *args):
        return subprocess.run(
            [sys.executable, '-m', MODULE, *map(str, args)],
            capture_output=True, text=True, timeout=60, cwd=str(ROOT),
            env={**os.environ, 'PYTHONPATH': str(ROOT / 'src')})

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.feed = self.root / 'feed'
        self.base = self.root / 'base.sqlite'
        self.ledger = self.root / 'reports.sqlite'
        write_feed(self.feed,
                   [['A', 'Alpha'], ['B', 'Beta']],
                   [['T', 'WK']], [all_days()], [],
                   [['T', '08:00:00', '08:00:00', 'A', 10],
                    ['T', '08:05:00', '08:05:00', 'A', 20],
                    ['T', '08:10:00', '08:10:00', 'A', 30],
                    ['T', '08:20:00', '08:20:00', 'B', 40]])
        import_feed(self.feed, self.base)
        self.obs = self.root / 'obs.csv'
        write_csv(self.obs, [
            ['E1', 'T', '2026-01-05', 'A', '08:04:00'],
            ['E2', 'T', '2026-01-05', 'A', '08:10:00']])

    def test_full_cli_lifecycle(self):
        result = self.invoke('punctuality-init', '--db', self.ledger)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), {'reports': 0})

        result = self.invoke(
            'punctuality-report', '--db', self.base, '--reports',
            self.ledger, '--report', 'R1', '--observations', self.obs,
            '--from', '2026-01-01', '--to', '2026-01-31',
            '--late-tolerance', '180')
        self.assertEqual(result.returncode, 0, result.stderr)
        initial = json.loads(result.stdout)
        self.assertEqual(set(initial),
                         {'report_id', 'review_of', 'known_seq', 'summary',
                          'trips'})
        self.assertIsNone(initial['review_of'])

        result = self.invoke('punctuality-show', '--reports', self.ledger,
                             '--report', 'R1')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), initial)

        decisions = self.root / 'd.json'
        decisions.write_text(json.dumps(
            {'confirmations': [{'event_id': 'E2',
                                'stop_sequence': 20}]}), encoding='utf-8')
        result = self.invoke('punctuality-revise', '--reports', self.ledger,
                             '--base', 'R1', '--report', 'R2',
                             '--decisions', decisions)
        self.assertEqual(result.returncode, 0, result.stderr)
        review = json.loads(result.stdout)
        self.assertEqual(review['review_of'], 'R1')
        confirmed = [m for m in review['trips'][0]['matched']
                     if m['confirmed']]
        self.assertEqual(
            [(m['event_id'], m['stop_sequence']) for m in confirmed],
            [('E2', 20)])

        # Replaying the exact report request returns the same body.
        replay = self.invoke(
            'punctuality-report', '--db', self.base, '--reports',
            self.ledger, '--report', 'R1', '--observations', self.obs,
            '--from', '2026-01-01', '--to', '2026-01-31',
            '--late-tolerance', '180')
        self.assertEqual(json.loads(replay.stdout), initial)

    def test_cli_errors_exit_two_with_empty_stdout(self):
        self.invoke('punctuality-init', '--db', self.ledger)
        missing = self.root / 'missing.sqlite'
        cases = [
            ('punctuality-show', '--reports', missing, '--report', 'X'),
            ('punctuality-show', '--reports', self.ledger, '--report', 'X'),
            ('punctuality-report', '--db', missing, '--reports', self.ledger,
             '--report', 'X', '--observations', self.obs,
             '--from', '2026-01-01', '--to', '2026-01-31'),
        ]
        for args in cases:
            result = self.invoke(*args)
            self.assertEqual(result.returncode, 2, args)
            self.assertEqual(result.stdout, '')
            self.assertTrue(result.stderr)
        self.assertFalse(missing.exists())
        # Non-numeric tolerance is an argparse failure with exit code 2.
        result = self.invoke(
            'punctuality-report', '--db', self.base, '--reports',
            self.ledger, '--report', 'X', '--observations', self.obs,
            '--from', '2026-01-01', '--to', '2026-01-31',
            '--known-seq', '1.5')
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, '')

    def test_cli_init_refuses_existing_target(self):
        self.ledger.write_bytes(b'keep')
        result = self.invoke('punctuality-init', '--db', self.ledger)
        self.assertEqual(result.returncode, 2)
        self.assertEqual(self.ledger.read_bytes(), b'keep')
        link = self.root / 'link'
        link.symlink_to(self.ledger)
        result = self.invoke('punctuality-init', '--db', link)
        self.assertEqual(result.returncode, 2)
        self.assertTrue(link.is_symlink())


if __name__ == '__main__':
    unittest.main()
