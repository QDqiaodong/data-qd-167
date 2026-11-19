from pathlib import Path
import shutil
import sqlite3
import tempfile
import unittest
from transit_feed.feed import import_feed, seconds
from transit_feed.query import departures

ROOT=Path(__file__).resolve().parents[1]
class FeedTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.source=self.root/'feed';shutil.copytree(ROOT/'fixtures/feed',self.source)
        self.db=self.root/'feed.sqlite'
    def test_counts_and_overnight_query(self):
        self.assertEqual(import_feed(self.source,self.db),{'stops':3,'trips':2,'stop_times':5})
        rows=departures(self.db,'2026-01-06','B')
        self.assertEqual([(r['trip_id'],r['departure']) for r in rows],[('T1','24:12:00')])
    def test_exception_override(self):
        import_feed(self.source,self.db)
        self.assertEqual([r['trip_id'] for r in departures(self.db,'2026-01-05','B')],['T2'])
    def test_weekend_no_service(self):
        import_feed(self.source,self.db);self.assertEqual(departures(self.db,'2026-01-04','B'),[])
    def test_after_inclusive(self):
        import_feed(self.source,self.db)
        self.assertEqual(len(departures(self.db,'2026-01-06','B','24:12:00')),1)
        self.assertEqual(departures(self.db,'2026-01-06','B','24:12:01'),[])
    def test_unknown_stop_rejected(self):
        import_feed(self.source,self.db)
        with self.assertRaisesRegex(ValueError,'unknown stop'):departures(self.db,'2026-01-06','Z')
    def test_bad_foreign_key_rolls_back(self):
        p=self.source/'stop_times.txt';p.write_text(p.read_text().replace(',B,2',',Z,2'))
        with self.assertRaises(sqlite3.IntegrityError):import_feed(self.source,self.db)
        with sqlite3.connect(self.db) as conn:self.assertEqual(conn.execute("select name from sqlite_master where type='table'").fetchall(),[])
    def test_existing_database_refused(self):
        import_feed(self.source,self.db)
        with self.assertRaises(ValueError):import_feed(self.source,self.db)
        self.assertEqual(len(departures(self.db,'2026-01-06','B')),1)
    def test_nonmonotone_times_rejected(self):
        p=self.source/'stop_times.txt';p.write_text(p.read_text().replace('24:10:00,24:12:00','22:10:00,22:12:00'))
        with self.assertRaisesRegex(ValueError,'monotone'):import_feed(self.source,self.db)
    def test_duplicate_exception_rejected(self):
        p=self.source/'calendar_dates.txt';p.write_text(p.read_text()+'WK,20260105,1\n')
        with self.assertRaises(sqlite3.IntegrityError):import_feed(self.source,self.db)
    def test_time_boundaries(self):
        self.assertEqual(seconds('47:59:59'),172799)
        for raw in ('48:00:00','12:60:00','9:00:00','-1:00:00'):
            with self.subTest(raw=raw),self.assertRaises(ValueError):seconds(raw)
