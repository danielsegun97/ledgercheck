"""Copy validated ledger history to DuckDB; no SQLite extensions required."""
import argparse
import json
import sqlite3
from contextlib import closing
from pathlib import Path

import duckdb


def snapshot(source, destination, controls):
    source, destination = Path(source).resolve(), Path(destination).resolve()
    if source == destination:
        raise ValueError('Source and warehouse must be different files')
    if not isinstance(controls, dict) or any(
        not isinstance(k, str) or not k or type(v) is not int or not 0 <= v <= 2**63 - 1
        for k, v in controls.items()
    ):
        raise ValueError('Controls must map firm IDs to non-negative signed 64-bit integer cents')
    # Read-only source: a typo must not create an empty operational database.
    with closing(sqlite3.connect(source.as_uri() + '?mode=ro', uri=True)) as ledger:
        rows = ledger.execute('SELECT firm_id,payment_id,version,amount_cents FROM payments').fetchall()
    destination.parent.mkdir(parents=True, exist_ok=True)
    with duckdb.connect(str(destination)) as db:
        db.execute('BEGIN TRANSACTION')
        try:
            db.execute('CREATE SCHEMA IF NOT EXISTS raw')
            db.execute('CREATE OR REPLACE TABLE raw.payments (firm_id VARCHAR, payment_id VARCHAR, version BIGINT, amount_cents BIGINT)')
            db.execute('CREATE OR REPLACE TABLE raw.controls (firm_id VARCHAR PRIMARY KEY, expected_cents BIGINT)')
            if rows:
                db.executemany('INSERT INTO raw.payments VALUES (?,?,?,?)', rows)
            if controls:
                db.executemany('INSERT INTO raw.controls VALUES (?,?)', list(controls.items()))
            db.execute('COMMIT')
        except Exception:
            db.execute('ROLLBACK')
            raise
    return len(rows)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', required=True)
    parser.add_argument('--destination', required=True)
    parser.add_argument('--controls', required=True)
    args = parser.parse_args()
    print(json.dumps({'payment_versions': snapshot(args.source, args.destination,
          json.loads(Path(args.controls).read_text(encoding='utf-8')))}))
