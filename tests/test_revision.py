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
from transit_feed.revision import revision_apply, revision_init, revised_departures

ROOT = Path(__file__).resolve().parents[1]
MODULE = 'transit_feed'


def write_feed(root):
    root.mkdir(parents=True)
    def table(name, header, rows):
        with (root / name).open('w', encoding='utf-8', newline='') as stream:
            writer = csv.writer(stream)
            writer.writerow(header)
            writer.writerows(rows)
    table('stops.txt', ['stop_id', 'stop_name'],
          [['A', 'Alpha'], ['B', 'Beta'], ['C', 'Gamma']])
    table('trips.txt', ['trip_id', 'service_id'],
          [['T', 'WK'], ['U', 'WK'], ['N', 'WK'], ['R', 'WK']])
    table('calendar.txt',
          ['service_id', 'monday', 'tuesday', 'wednesday', 'thursday', 'friday',
           'saturday', 'sunday', 'start_date', 'end_date'],
          [['WK', '1', '1', '1', '1', '1', '1', '1', '18000101', '24001231']])
    table('calendar_dates.txt', ['service_id', 'date', 'exception_type'],
          [['WK', '20260101', '2']])
    table('stop_times.txt',
          ['trip_id', 'arrival_time', 'departure_time', 'stop_id', 'stop_sequence'],
          [['T', '09:00:00', '09:00:00', 'A', 1],
           ['T', '10:00:00', '10:00:00', 'B', 2],
           ['U', '05:00:00', '05:00:00', 'A', 1],
           ['U', '18:00:00', '18:00:00', 'B', 2],
           ['U', '19:00:00', '19:00:00', 'C', 3],
           ['N', '23:50:00', '23:50:00', 'A', 1],
           ['N', '24:10:00', '24:12:00', 'B', 2],
           ['N', '24:25:00', '24:25:00', 'C', 3],
           ['R', '09:00:00', '09:00:00', 'A', 1],
           ['R', '10:00:00', '10:00:00', 'B', 2],
           ['R', '10:30:00', '10:30:00', 'A', 3]])


def put(correction, trip, sequence, start, end, arrival, departure=None):
    return {'op': 'put', 'correction_id': correction, 'trip_id': trip,
            'stop_sequence': sequence, 'from': start, 'to': end,
            'arrival': arrival, 'departure': departure or arrival}


def retract(correction):
    return {'op': 'retract', 'correction_id': correction}


class BaseCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.feed = self.root / 'feed'
        write_feed(self.feed)
        self.base = self.root / 'base.sqlite'
        import_feed(self.feed, self.base)
        self.db = self.root / 'revision.sqlite'
        revision_init(self.base, self.db)

    def apply(self, *changes):
        return revision_apply(self.db, {'changes': list(changes)})

    def departures(self, day='2026-01-05', stop='A', after='00:00:00', known=None):
        return revised_departures(self.db, day, stop, after, known)

    def rows(self, day='2026-01-05', stop='A', after='00:00:00', known=None):
        return self.departures(day, stop, after, known)['departures']

    def row(self, trip, sequence, day='2026-01-05', stop='A', known=None):
        matches = [row for row in self.rows(day, stop, known=known)
                   if row['trip_id'] == trip and row['stop_sequence'] == sequence]
        self.assertEqual(len(matches), 1)
        return matches[0]


class InitTests(BaseCase):
    def test_init_copies_independent_baseline_and_keeps_source(self):
        before = self.base.read_bytes()
        self.assertEqual(self.departures('2026-01-05', 'A')['known_seq'], 0)
        self.assertEqual(self.base.read_bytes(), before)
        # The copy works after the source archive and the source CSV disappear.
        self.base.unlink()
        for path in self.feed.iterdir():
            path.unlink()
        self.feed.rmdir()
        rows = self.rows('2026-01-05', 'A')
        self.assertEqual([row['trip_id'] for row in rows], ['U', 'R', 'T', 'R', 'N'])
        self.assertTrue(all(row['correction_id'] is None for row in rows))

    def test_init_refuses_existing_target_and_preserves_it(self):
        target = self.root / 'occupied.sqlite'
        target.write_bytes(b'old archive')
        with self.assertRaisesRegex(ValueError, 'must not exist'):
            revision_init(self.base, target)
        self.assertEqual(target.read_bytes(), b'old archive')

    def test_init_refuses_symlink_target_and_preserves_it(self):
        target = self.root / 'target.sqlite'
        target.write_bytes(b'old archive')
        link = self.root / 'link.sqlite'
        link.symlink_to(target)
        with self.assertRaisesRegex(ValueError, 'must not exist'):
            revision_init(self.base, link)
        self.assertTrue(link.is_symlink())
        self.assertEqual(target.read_bytes(), b'old archive')

    def test_init_invalid_base_leaves_no_target(self):
        broken = self.root / 'broken.sqlite'
        with sqlite3.connect(broken) as conn:
            conn.execute('CREATE TABLE junk(x)')
        target = self.root / 'new.sqlite'
        with self.assertRaises(ValueError):
            revision_init(broken, target)
        self.assertFalse(target.exists())
        self.assertEqual(sorted(self.root.glob('*.sqlite*')),
                         sorted([self.base, broken, self.db]))

    def test_init_missing_base_leaves_no_target(self):
        target = self.root / 'new.sqlite'
        with self.assertRaises(ValueError):
            revision_init(self.root / 'missing.sqlite', target)
        self.assertFalse(target.exists())

    def test_apply_refuses_base_archive(self):
        with self.assertRaisesRegex(ValueError, 'revision warehouse'):
            revision_apply(self.base, {'changes': [retract('X')]})


class BatchValidationTests(BaseCase):
    def test_strict_top_level_and_entry_shapes(self):
        bad_batches = [
            [],
            {},
            {'changes': []},
            {'changes': [], 'extra': 1},
            {'changes': [{'op': 'retract', 'correction_id': 'X', 'trip_id': 'T'}]},
            {'changes': [{'op': 'put', 'correction_id': 'X', 'trip_id': 'T',
                          'stop_sequence': 1, 'from': '2026-01-01',
                          'to': '2026-01-02', 'arrival': '09:00:00'}]},
            {'changes': [{'op': 'move', 'correction_id': 'X'}]},
            {'changes': [put('X', 'T', 1, '2026-01-01', '2026-01-02', '09:00:00'),
                         put('X', 'T', 1, '2026-01-03', '2026-01-04', '09:00:00')]},
            {'changes': [retract('X'), retract('X')]},
            {'changes': [put(' X', 'T', 1, '2026-01-01', '2026-01-02', '09:00:00')]},
            {'changes': [put('', 'T', 1, '2026-01-01', '2026-01-02', '09:00:00')]},
            {'changes': [put('X', 'T', 1, '2026-1-1', '2026-01-02', '09:00:00')]},
            {'changes': [put('X', 'T', 1, '2026-01-03', '2026-01-02', '09:00:00')]},
            {'changes': [put('X', 'T', 1, '2026-01-01', '2026-01-02', '9:00:00')]},
            {'changes': [put('X', 'T', 0, '2026-01-01', '2026-01-02', '09:00:00')]},
            {'changes': [put('X', 'T', 1, '2026-01-01', '2026-01-02', '09:00:00', '08:00:00')]},
            {'changes': [put('X', 'T', 9, '2026-01-01', '2026-01-02', '09:00:00')]},
            {'changes': [put('X', 'Z', 1, '2026-01-01', '2026-01-02', '09:00:00')]},
        ]
        for batch in bad_batches:
            with self.subTest(batch=batch), self.assertRaises(ValueError):
                revision_apply(self.db, batch)
        self.assertEqual(self.departures()['known_seq'], 0)

    def test_retract_unknown_and_double_retract_rejected(self):
        with self.assertRaises(ValueError):
            self.apply(retract('missing'))
        self.apply(put('X', 'U', 1, '2026-01-01', '2026-01-10', '06:00:00'))
        self.apply(retract('X'))
        with self.assertRaisesRegex(ValueError, 'already retracted'):
            self.apply(retract('X'))

    def test_identity_is_fixed_but_reactivation_allowed(self):
        self.apply(put('X', 'U', 1, '2026-01-01', '2026-01-10', '06:00:00'))
        with self.assertRaisesRegex(ValueError, 'identity'):
            self.apply(put('X', 'U', 2, '2026-01-01', '2026-01-10', '06:00:00'))
        with self.assertRaisesRegex(ValueError, 'identity'):
            self.apply(put('X', 'T', 1, '2026-01-01', '2026-01-10', '10:00:00'))
        self.apply(retract('X'))
        self.apply(put('X', 'U', 1, '2026-01-02', '2026-01-04', '06:30:00'))
        self.assertEqual(self.row('U', 1, '2026-01-03')['correction_id'], 'X')
        with self.assertRaisesRegex(ValueError, 'identity'):
            self.apply(put('X', 'U', 2, '2026-01-02', '2026-01-04', '06:30:00'))

    def test_batch_file_accepts_path_and_strict_json(self):
        path = self.root / 'batch.json'
        path.write_text(json.dumps({'changes': [
            put('X', 'U', 1, '2026-01-01', '2026-01-10', '06:00:00')]}), encoding='utf-8')
        self.assertEqual(revision_apply(self.db, path), {'known_seq': 1})
        duplicate = self.root / 'dup.json'
        duplicate.write_text('{"changes": [], "changes": []}', encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'duplicate JSON key'):
            revision_apply(self.db, duplicate)
        nan = self.root / 'nan.json'
        nan.write_text('{"changes": [{"op": "retract", "correction_id": NaN}]}', encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'nonfinite'):
            revision_apply(self.db, nan)
        self.assertEqual(self.departures()['known_seq'], 1)

    def test_from_equal_to_is_inclusive(self):
        self.apply(put('X', 'U', 1, '2026-01-05', '2026-01-05', '06:00:00'))
        self.apply(put('Y', 'U', 2, '2026-01-05', '2026-01-05', '18:00:00'))
        self.assertIsNone(self.row('U', 1, '2026-01-04')['correction_id'])
        self.assertEqual(self.row('U', 1, '2026-01-05')['correction_id'], 'X')
        self.assertIsNone(self.row('U', 1, '2026-01-06')['correction_id'])

    def test_failed_batch_leaves_no_version_or_gap(self):
        self.apply(put('X', 'U', 1, '2026-01-01', '2026-01-10', '06:00:00'))
        with self.assertRaises(ValueError):
            self.apply(put('A', 'U', 1, '2026-01-01', '2026-01-10', '07:00:00'),
                       put('B', 'U', 2, '2026-01-01', '2026-01-10', '04:00:00'))
        with sqlite3.connect(self.db) as conn:
            self.assertEqual(conn.execute('SELECT known_seq FROM revision_state').fetchone()[0], 1)
            self.assertIsNone(conn.execute(
                "SELECT 1 FROM revision_identity WHERE correction_id IN ('A','B')").fetchone())
        self.assertEqual(self.apply(put('C', 'U', 3, '2026-01-01', '2026-01-10', '20:00:00')),
                         {'known_seq': 2})


class IntegrityTests(BaseCase):
    def test_acceptance_shorten_exposes_baseline_even_on_removed_day(self):
        # D1=2026-01-01 is removed by a calendar exception; D2/D3 run.
        d1, d2, d3 = '2026-01-01', '2026-01-02', '2026-01-03'
        self.apply(put('X', 'T', 1, d1, d3, '08:00:00'))
        self.assertEqual(self.apply(put('W', 'T', 1, d1, d3, '11:00:00'),
                                   put('Y', 'T', 2, d1, d3, '12:00:00')),
                         {'known_seq': 2})
        self.assertEqual(self.departures(d1)['departures'], [])
        # D1 would reveal baseline B 10:00 under W's A 11:00, so reject even
        # though the service does not run that day.
        with self.assertRaisesRegex(ValueError, 'backwards'):
            self.apply(put('Y', 'T', 2, d2, d3, '12:00:00'))
        self.assertEqual(self.departures()['known_seq'], 2)
        self.assertEqual(self.row('T', 2, d2, 'B')['correction_id'], 'Y')
        self.assertEqual(self.row('T', 1, d2, 'A')['correction_id'], 'W')

    def test_acceptance_retract_only_y_rejected_then_both_accepted(self):
        d1, d3 = '2026-01-02', '2026-01-03'
        self.assertEqual(self.apply(put('X', 'T', 1, d1, d3, '11:00:00'),
                                   put('Y', 'T', 2, d1, d3, '12:00:00')),
                         {'known_seq': 1})
        # Retracting only Y exposes baseline B 10:00 under A 11:00.
        with self.assertRaisesRegex(ValueError, 'backwards'):
            self.apply(retract('Y'))
        self.assertEqual(self.departures()['known_seq'], 1)
        self.assertEqual(self.apply(retract('Y'), retract('X')), {'known_seq': 2})
        self.assertIsNone(self.row('T', 1, d1)['correction_id'])
        self.assertIsNone(self.row('T', 2, d1, 'B')['correction_id'])

    def test_whole_trip_checked_even_when_service_not_running(self):
        # 2026-01-01 is removed from the baseline calendar, but still counts.
        self.apply(put('X', 'T', 1, '2026-01-01', '2026-01-03', '08:00:00'))
        self.assertEqual(self.apply(
            put('W', 'T', 1, '2026-01-01', '2026-01-03', '11:00:00'),
            put('Y', 'T', 2, '2026-01-01', '2026-01-03', '12:00:00')), {'known_seq': 2})
        self.assertEqual(self.departures('2026-01-01')['departures'], [])
        # Shortening Y exposes baseline B 10:00 below W's A 11:00 on D1.
        with self.assertRaisesRegex(ValueError, 'backwards'):
            self.apply(put('Y', 'T', 2, '2026-01-02', '2026-01-03', '12:00:00'))
        self.assertEqual(self.departures()['known_seq'], 2)
        self.assertEqual(self.row('T', 2, '2026-01-02', 'B')['correction_id'], 'Y')

    def test_retract_exposes_baseline_and_rejects(self):
        self.apply(put('X', 'T', 1, '2026-01-01', '2026-01-03', '11:00:00'),
                   put('Y', 'T', 2, '2026-01-01', '2026-01-03', '12:00:00'))
        with self.assertRaisesRegex(ValueError, 'backwards'):
            self.apply(retract('Y'))
        self.assertEqual(self.departures()['known_seq'], 1)
        self.assertEqual(self.row('T', 1, '2026-01-02')['correction_id'], 'X')
        self.assertEqual(self.apply(retract('Y'), retract('X')), {'known_seq': 2})
        current = self.row('T', 1, '2026-01-02')
        self.assertEqual(current['departure'], '09:00:00')
        self.assertIsNone(current['correction_id'])

    def test_retract_exposes_another_id_value_and_rejects(self):
        self.apply(put('X', 'U', 1, '2026-01-01', '2026-01-05', '13:00:00'),
                   put('Y', 'U', 2, '2026-01-01', '2026-01-05', '14:00:00'))
        self.apply(put('Z', 'U', 1, '2026-01-01', '2026-01-05', '06:00:00'),
                   put('Y', 'U', 2, '2026-01-01', '2026-01-05', '10:00:00'))
        # Retracting Z would reveal X at 13:00, after Y's 10:00 at stop 2.
        with self.assertRaisesRegex(ValueError, 'backwards'):
            self.apply(retract('Z'))
        self.assertEqual(self.row('U', 1, '2026-01-02')['correction_id'], 'Z')
        self.assertEqual(self.departures()['known_seq'], 2)

    def test_retract_shows_hidden_valid_id(self):
        self.apply(put('X', 'U', 1, '2026-01-01', '2026-01-05', '06:00:00'))
        self.apply(put('Z', 'U', 1, '2026-01-01', '2026-01-05', '07:00:00'))
        self.assertEqual(self.row('U', 1, '2026-01-02')['correction_id'], 'Z')
        self.apply(retract('Z'))
        current = self.row('U', 1, '2026-01-02')
        self.assertEqual((current['departure'], current['correction_id']),
                         ('06:00:00', 'X'))

    def test_same_batch_priority_decides_validation(self):
        # zulu wins the tie and is illegal, so the whole batch is rejected.
        with self.assertRaisesRegex(ValueError, 'backwards'):
            self.apply(put('aaa', 'U', 1, '2026-01-01', '2026-01-02', '06:00:00'),
                       put('zzz', 'U', 1, '2026-01-01', '2026-01-02', '19:00:00'))
        # Reversed input order gives the same winner and the same rejection.
        with self.assertRaisesRegex(ValueError, 'backwards'):
            self.apply(put('zzz', 'U', 1, '2026-01-01', '2026-01-02', '19:00:00'),
                       put('aaa', 'U', 1, '2026-01-01', '2026-01-02', '06:00:00'))
        self.assertEqual(self.departures()['known_seq'], 0)
        # Now make the winner legal: the loser is irrelevant to validation.
        self.apply(put('aaa', 'U', 1, '2026-01-01', '2026-01-02', '19:00:00'),
                   put('zzz', 'U', 1, '2026-01-01', '2026-01-02', '06:00:00'))
        self.assertEqual(self.row('U', 1, '2026-01-02')['correction_id'], 'zzz')

    def test_arrival_after_departure_rejected(self):
        with self.assertRaises(ValueError):
            self.apply(put('X', 'U', 1, '2026-01-01', '2026-01-02',
                           '06:00:00', '05:00:00'))

    def test_retract_never_restores_earlier_version_of_same_id(self):
        self.apply(put('X', 'U', 1, '2026-01-01', '2026-01-10', '06:00:00'))
        self.apply(put('X', 'U', 1, '2026-01-01', '2026-01-10', '06:30:00'))
        self.apply(retract('X'))
        self.assertIsNone(self.row('U', 1, '2026-01-05')['correction_id'])
        self.assertEqual(self.row('U', 1, '2026-01-05', known=1)['departure'], '06:00:00')
        self.assertEqual(self.row('U', 1, '2026-01-05', known=2)['departure'], '06:30:00')
        self.assertIsNone(self.row('U', 1, '2026-01-05', known=3)['correction_id'])

        self.apply(put('X', 'U', 1, '2026-01-01', '2026-01-10', '06:00:00'))
        self.apply(put('X', 'U', 1, '2026-01-05', '2026-01-06', '06:30:00'))
        self.assertIsNone(self.row('U', 1, '2026-01-02')['correction_id'])
        self.assertEqual(self.row('U', 1, '2026-01-05')['correction_id'], 'X')
        self.assertEqual(self.row('U', 1, '2026-01-06')['departure'], '06:30:00')
        self.assertIsNone(self.row('U', 1, '2026-01-07')['correction_id'])
        self.assertEqual(self.row('U', 1, '2026-01-02', known=1)['correction_id'], 'X')


class PriorityTests(BaseCase):
    def test_same_batch_priority_is_lexicographic_and_order_independent(self):
        one = self.root / 'one.sqlite'
        two = self.root / 'two.sqlite'
        revision_init(self.base, one)
        revision_init(self.base, two)
        a = put('alpha', 'U', 1, '2026-01-01', '2026-01-10', '06:00:00')
        z = put('zulu', 'U', 1, '2026-01-01', '2026-01-10', '07:00:00')
        revision_apply(one, {'changes': [a, z]})
        revision_apply(two, {'changes': [z, a]})
        first = revised_departures(one, '2026-01-05', 'A')['departures']
        second = revised_departures(two, '2026-01-05', 'A')['departures']
        self.assertEqual(first, second)
        chosen = [row for row in first if row['trip_id'] == 'U'][0]
        self.assertEqual((chosen['departure'], chosen['correction_id']), ('07:00:00', 'zulu'))

    def test_later_accept_sequence_beats_lexicographic_id(self):
        self.apply(put('zulu', 'U', 1, '2026-01-01', '2026-01-10', '07:00:00'))
        self.apply(put('alpha', 'U', 1, '2026-01-01', '2026-01-10', '06:00:00'))
        chosen = [row for row in self.rows() if row['trip_id'] == 'U'][0]
        self.assertEqual((chosen['departure'], chosen['correction_id']), ('06:00:00', 'alpha'))

    def test_out_of_range_winner_lets_other_id_apply(self):
        self.apply(put('winner', 'U', 1, '2026-01-01', '2026-01-10', '07:00:00'))
        self.apply(put('loser', 'U', 1, '2026-01-01', '2026-01-10', '06:00:00'))
        self.apply(put('winner', 'U', 1, '2026-01-06', '2026-01-10', '07:30:00'))
        # Outside the newer winner's range the older winner must not reappear,
        # so the lower-priority ID that still covers the day takes over.
        chosen = [row for row in self.rows('2026-01-02') if row['trip_id'] == 'U'][0]
        self.assertEqual((chosen['departure'], chosen['correction_id']), ('06:00:00', 'loser'))
        chosen = [row for row in self.rows('2026-01-07') if row['trip_id'] == 'U'][0]
        self.assertEqual((chosen['departure'], chosen['correction_id']), ('07:30:00', 'winner'))

    def test_repeated_stop_visits_are_separate(self):
        rows = [row for row in self.rows('2026-01-05', 'A') if row['trip_id'] == 'R']
        self.assertEqual([(row['stop_sequence'], row['departure']) for row in rows],
                         [(1, '09:00:00'), (3, '10:30:00')])


class HistoryTests(BaseCase):
    def test_old_knowledge_is_reproducible_after_restart(self):
        self.apply(put('X', 'U', 1, '2026-01-01', '2026-01-10', '06:00:00'))
        before = self.departures('2026-01-05', 'A', known=1)
        self.apply(put('X', 'U', 1, '2026-01-01', '2026-01-10', '06:15:00'))
        after = self.departures('2026-01-05', 'A', known=1)
        self.assertEqual(before, after)
        current = self.departures('2026-01-05', 'A')
        self.assertIn('06:15:00', [row['departure'] for row in current['departures']])
        # Simulate a late retract; the historical answer must not change.
        self.apply(retract('X'))
        self.assertEqual(self.departures('2026-01-05', 'A', known=1), before)
        self.assertNotIn('06:00:00',
                         [row['departure'] for row in self.departures('2026-01-05', 'A')['departures']])

    def test_baseline_known_zero_matches_legacy_departures(self):
        from transit_feed.query import departures as legacy
        revised = self.departures('2026-01-05', 'A', known=0)['departures']
        old = legacy(self.db, '2026-01-05', 'A')
        self.assertEqual(
            [(row['trip_id'], row['stop_sequence'], row['departure'], row['service_date'])
             for row in revised],
            [(row['trip_id'], row['stop_sequence'], row['departure'], row['service_date'])
             for row in old])
        self.assertTrue(all(row['correction_id'] is None for row in revised))

    def test_after_filter_inclusive_and_ordered(self):
        self.apply(put('X', 'U', 1, '2026-01-01', '2026-01-10', '06:00:00'))
        rows = self.rows('2026-01-05', 'A', after='06:00:00')
        self.assertEqual(rows[0]['departure'], '06:00:00')
        self.assertEqual(self.rows('2026-01-05', 'A', after='06:00:01')[0]['departure'], '09:00:00')
        self.apply(put('Y', 'R', 3, '2026-01-01', '2026-01-10', '11:00:00'))
        ordered = [(row['departure'], row['trip_id'], row['stop_sequence'])
                   for row in self.rows('2026-01-05', 'A')]
        self.assertEqual(ordered, sorted(ordered))
        self.assertEqual(ordered[:3], [('06:00:00', 'U', 1), ('09:00:00', 'R', 1),
                                       ('09:00:00', 'T', 1)])

    def test_overnight_belongs_to_service_date(self):
        self.apply(put('X', 'N', 2, '2026-01-05', '2026-01-05', '24:12:00'))
        row = self.row('N', 2, '2026-01-05', 'B')
        self.assertEqual((row['departure'], row['service_date'], row['correction_id']),
                         ('24:12:00', '2026-01-05', 'X'))
        self.apply(put('X', 'N', 2, '2026-01-05', '2026-01-05', '24:14:59'))
        self.assertEqual(self.row('N', 2, '2026-01-05', 'B')['departure'], '24:14:59')
        self.apply(put('Z', 'U', 3, '2026-01-05', '2026-01-05', '47:59:59'))
        self.assertEqual(self.row('U', 3, '2026-01-05', 'C')['departure'], '47:59:59')

    def test_century_spanning_range_without_per_day_storage(self):
        self.apply(put('L', 'U', 1, '1900-01-01', '2300-12-31', '06:00:00'))
        with sqlite3.connect(self.db) as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM revision_version').fetchone()[0], 1)
        self.assertEqual(self.row('U', 1, '1900-01-01')['correction_id'], 'L')
        self.assertEqual(self.row('U', 1, '2300-12-31')['correction_id'], 'L')
        self.assertIsNone(self.row('U', 1, '1899-12-31')['correction_id'])
        self.assertIsNone(self.row('U', 1, '2301-01-01')['correction_id'])
        self.assertIsNone(self.row('U', 1, '2300-12-31', known=0)['correction_id'])

    def test_exception_only_running_rule_uses_baseline(self):
        # 2026-01-01 is removed by the calendar exception.
        self.assertEqual(self.rows('2026-01-01', 'A'), [])
        self.assertTrue(self.rows('2026-01-02', 'A'))
        with self.assertRaises(ValueError):
            self.rows('2026-1-5', 'A')


class QueryValidationTests(BaseCase):
    def test_future_knowledge_unknown_stop_and_bad_date(self):
        self.apply(put('X', 'U', 1, '2026-01-01', '2026-01-10', '06:00:00'))
        cases = [dict(known=2), dict(known=-1), dict(stop='Z'), dict(day='2026-01-05T00:00:00')]
        for kwargs in cases:
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                self.departures(**kwargs)
        self.assertEqual(self.departures(known=1)['known_seq'], 1)

    def test_unknown_stop_omitted_knowledge(self):
        with self.assertRaisesRegex(ValueError, 'unknown stop'):
            self.departures('2026-01-05', 'Z')

    def test_empty_result_still_reports_knowledge(self):
        self.apply(put('X', 'U', 1, '2026-01-01', '2026-01-10', '06:00:00'))
        result = self.departures('2026-01-04', 'A')
        self.assertEqual(result['known_seq'], 1)


class CliTests(unittest.TestCase):
    def invoke(self, *args):
        return subprocess.run([sys.executable, '-m', MODULE, *map(str, args)],
                              capture_output=True, text=True, timeout=30,
                              cwd=str(ROOT),
                              env={**os.environ, 'PYTHONPATH': str(ROOT / 'src')})

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.feed = self.root / 'feed'
        write_feed(self.feed)
        self.base = self.root / 'base.sqlite'
        self.db = self.root / 'revision.sqlite'
        import_feed(self.feed, self.base)

    def apply_file(self, payload, path=None):
        path = path or self.root / 'batch.json'
        path.write_text(json.dumps(payload), encoding='utf-8')
        return self.invoke('revision-apply', '--db', self.db, '--file', path)

    def test_cli_lifecycle_and_error_codes(self):
        init = self.invoke('revision-init', '--source-db', self.base, '--db', self.db)
        self.assertEqual(init.returncode, 0, init.stderr)
        self.assertEqual(json.loads(init.stdout), {'known_seq': 0})
        batch = {'changes': [put('X', 'U', 1, '2026-01-01', '2026-01-10', '06:00:00')]}
        applied = self.apply_file(batch)
        self.assertEqual(applied.returncode, 0, applied.stderr)
        self.assertEqual(json.loads(applied.stdout), {'known_seq': 1})
        query = self.invoke('revised-departures', '--db', self.db,
                            '--date', '2026-01-05', '--stop', 'A',
                            '--after', '06:00:00', '--known-seq', '1')
        self.assertEqual(query.returncode, 0, query.stderr)
        result = json.loads(query.stdout)
        self.assertEqual(result['known_seq'], 1)
        self.assertEqual(result['departures'][0]['correction_id'], 'X')
        invalid = self.apply_file({'changes': [retract('missing')]})
        self.assertEqual(invalid.returncode, 2)
        self.assertTrue(invalid.stderr)
        self.assertEqual(invalid.stdout, '')
        future = self.invoke('revised-departures', '--db', self.db,
                             '--date', '2026-01-05', '--stop', 'A', '--known-seq', '2')
        self.assertEqual(future.returncode, 2)
        still = self.invoke('revised-departures', '--db', self.db,
                            '--date', '2026-01-05', '--stop', 'A')
        self.assertEqual(json.loads(still.stdout)['known_seq'], 1)

    def test_query_missing_database_does_not_create(self):
        missing = self.root / 'missing.sqlite'
        result = self.invoke('revised-departures', '--db', missing,
                             '--date', '2026-01-05', '--stop', 'A')
        self.assertEqual(result.returncode, 2)
        self.assertFalse(missing.exists())

    def test_apply_missing_database_does_not_create(self):
        missing = self.root / 'missing.sqlite'
        batch = self.root / 'batch.json'
        batch.write_text(json.dumps({'changes': [
            put('X', 'U', 1, '2026-01-01', '2026-01-10', '06:00:00')]}), encoding='utf-8')
        result = self.invoke('revision-apply', '--db', missing, '--file', batch)
        self.assertEqual(result.returncode, 2)
        self.assertFalse(missing.exists())

    def test_failed_query_leaves_warehouse_unchanged(self):
        self.invoke('revision-init', '--source-db', self.base, '--db', self.db)
        self.apply_file({'changes': [put('X', 'U', 1, '2026-01-01', '2026-01-10', '06:00:00')]})
        digest = hashlib.sha256(self.db.read_bytes()).hexdigest()
        for extra in (['--stop', 'Z'], ['--known-seq', '99'], ['--after', '9:00:00']):
            result = self.invoke('revised-departures', '--db', self.db,
                                 '--date', '2026-01-05', *extra)
            self.assertEqual(result.returncode, 2)
            self.assertEqual(hashlib.sha256(self.db.read_bytes()).hexdigest(), digest)

    def test_query_is_read_only(self):
        self.invoke('revision-init', '--source-db', self.base, '--db', self.db)
        self.apply_file({'changes': [put('X', 'U', 1, '2026-01-01', '2026-01-10', '06:00:00')]})
        digest = hashlib.sha256(self.db.read_bytes()).hexdigest()
        self.invoke('revised-departures', '--db', self.db, '--date', '2026-01-05', '--stop', 'A')
        self.assertEqual(hashlib.sha256(self.db.read_bytes()).hexdigest(), digest)

    def test_existing_init_target_protected(self):
        self.db.write_bytes(b'keep me')
        result = self.invoke('revision-init', '--source-db', self.base, '--db', self.db)
        self.assertEqual(result.returncode, 2)
        self.assertEqual(self.db.read_bytes(), b'keep me')

    def test_failed_init_creates_no_database(self):
        result = self.invoke('revision-init', '--source-db', self.root / 'missing',
                             '--db', self.db)
        self.assertEqual(result.returncode, 2)
        self.assertFalse(self.db.exists())

    def test_legacy_departures_still_works_on_revision_archive(self):
        self.invoke('revision-init', '--source-db', self.base, '--db', self.db)
        legacy = self.invoke('departures', '--db', self.db,
                             '--date', '2026-01-05', '--stop', 'A')
        self.assertEqual(legacy.returncode, 0, legacy.stderr)
        self.assertEqual(json.loads(legacy.stdout)[0]['departure'], '05:00:00')


if __name__ == '__main__':
    unittest.main()
