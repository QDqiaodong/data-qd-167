import argparse
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
from .feed import import_feed
from .query import departures
from .regional import regional_departures, regional_init
from .revision import revision_apply, revision_init, revised_departures


def feed_argument(value):
    key, separator, path = value.partition('=')
    if not separator:
        raise argparse.ArgumentTypeError('--feed must be KEY=DB')
    return key, path


def main():
    parser = argparse.ArgumentParser(description='Local service-day timetable archive')
    commands = parser.add_subparsers(dest='command', required=True)
    load = commands.add_parser('import')
    load.add_argument('--source', type=Path, required=True)
    load.add_argument('--db', type=Path, required=True)
    query = commands.add_parser('departures')
    query.add_argument('--db', type=Path, required=True)
    query.add_argument('--date', required=True)
    query.add_argument('--stop', required=True)
    query.add_argument('--after', default='00:00:00')

    init = commands.add_parser('revision-init')
    init.add_argument('--source-db', type=Path, required=True)
    init.add_argument('--db', type=Path, required=True)

    apply = commands.add_parser('revision-apply')
    apply.add_argument('--db', type=Path, required=True)
    apply.add_argument('--file', type=Path, required=True)

    revised = commands.add_parser('revised-departures')
    revised.add_argument('--db', type=Path, required=True)
    revised.add_argument('--date', required=True)
    revised.add_argument('--stop', required=True)
    revised.add_argument('--after', default='00:00:00')
    revised.add_argument('--known-seq', type=int, default=None)

    regional_load = commands.add_parser('regional-init')
    regional_load.add_argument('--feed', type=feed_argument, action='append',
                               required=True, metavar='KEY=DB')
    regional_load.add_argument('--interchanges', type=Path, required=True)
    regional_load.add_argument('--db', type=Path, required=True)

    regional_query = commands.add_parser('regional-departures')
    regional_query.add_argument('--db', type=Path, required=True)
    regional_query.add_argument('--date', required=True)
    regional_query.add_argument('--interchange', required=True)
    regional_query.add_argument('--after', default='00:00:00')

    args = parser.parse_args()
    temporary = None
    try:
        if args.command == 'import':
            args.db.parent.mkdir(parents=True, exist_ok=True)
            fd, temporary = tempfile.mkstemp(dir=args.db.parent, suffix='.sqlite')
            os.close(fd)
            result = import_feed(args.source, temporary)
            # Hard-link publication is atomic and refuses existing paths.
            os.link(temporary, args.db)
        elif args.command == 'departures':
            result = departures(args.db, args.date, args.stop, args.after)
        elif args.command == 'revision-init':
            result = revision_init(args.source_db, args.db)
        elif args.command == 'revision-apply':
            result = revision_apply(args.db, args.file)
        elif args.command == 'regional-init':
            args.db.parent.mkdir(parents=True, exist_ok=True)
            result = regional_init(args.feed, args.interchanges, args.db)
        elif args.command == 'regional-departures':
            result = regional_departures(args.db, args.date, args.interchange,
                                         args.after)
        else:
            result = revised_departures(args.db, args.date, args.stop,
                                        args.after, args.known_seq)
    except (OSError, ValueError, TypeError, sqlite3.Error) as error:
        print(str(error), file=sys.stderr)
        return 2
    finally:
        if temporary:
            Path(temporary).unlink(missing_ok=True)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0
