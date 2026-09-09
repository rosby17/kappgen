"""Coordinated migration, or read-only readiness check with --check."""
import argparse
import sys
from src.db.session import engine
from src.utils.provider_secret_storage import migrate, preflight


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check', action='store_true', help='Verify readiness without modifying schema or credentials.')
    args = parser.parse_args()
    try:
        with engine.begin() as connection:
            if args.check and connection.dialect.name == 'postgresql':
                from sqlalchemy import text
                connection.execute(text('SET TRANSACTION READ ONLY'))
            count = preflight(connection) if args.check else migrate(connection)
    except Exception:
        # SQL/driver exceptions may retain bound credentials; never print them.
        print('Provider credential check failed. Verify secret configuration, database access and migration state.', file=sys.stderr)
        return 1
    print(f'Provider credentials {"verified" if args.check else "migrated"}: {count}.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
