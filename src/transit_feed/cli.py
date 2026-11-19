import argparse
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
from .feed import import_feed
from .query import departures


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
        else:
            result = departures(args.db, args.date, args.stop, args.after)
    except (OSError, ValueError, TypeError, sqlite3.Error) as error:
        print(str(error), file=sys.stderr)
        return 2
    finally:
        if temporary:
            Path(temporary).unlink(missing_ok=True)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0
