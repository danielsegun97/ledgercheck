"""Synthetic payment reconciliation. Python 3.11+, standard library only."""
import argparse
import csv
import hashlib
import json
import re
import sqlite3
from pathlib import Path

FIELDS = ["firm_id", "payment_id", "version", "amount_cents"]


def connect(path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path)
    db.executescript("""
        CREATE TABLE IF NOT EXISTS payments (
            firm_id TEXT NOT NULL, payment_id TEXT NOT NULL,
            version INTEGER NOT NULL CHECK(version > 0),
            amount_cents INTEGER NOT NULL CHECK(amount_cents >= 0),
            PRIMARY KEY(firm_id, payment_id, version)
        );
        CREATE TABLE IF NOT EXISTS batches (
            digest TEXT PRIMARY KEY, filename TEXT NOT NULL,
            accepted INTEGER NOT NULL, rejected INTEGER NOT NULL,
            duplicates INTEGER NOT NULL,
            loaded_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS rejects (
            digest TEXT NOT NULL, line INTEGER NOT NULL,
            reason TEXT NOT NULL, raw_json TEXT NOT NULL,
            PRIMARY KEY(digest, line)
        );
        CREATE VIEW IF NOT EXISTS current_payments AS
            SELECT p.* FROM payments p
            WHERE p.version = (
                SELECT MAX(q.version) FROM payments q
                WHERE q.firm_id=p.firm_id AND q.payment_id=p.payment_id
            );
    """)
    return db


def validate(row):
    if None in row or any(row.get(k) is None for k in FIELDS):
        raise ValueError("Wrong column count")
    if not all(re.fullmatch(r"[A-Za-z0-9_-]{1,64}", row[k]) for k in FIELDS[:2]):
        raise ValueError("Invalid firm or payment identifier")
    if not re.fullmatch(r"[1-9][0-9]*", row["version"]):
        raise ValueError("Version must be a positive integer")
    if not re.fullmatch(r"[0-9]+", row["amount_cents"]):
        raise ValueError("Amount must be non-negative integer cents")
    version, amount = int(row["version"]), int(row["amount_cents"])
    if max(version, amount) > 2**63 - 1:
        raise ValueError("Integer exceeds supported range")
    return row["firm_id"], row["payment_id"], version, amount


def ingest(db, source):
    source = Path(source)
    payload = source.read_bytes()
    digest = hashlib.sha256(payload).hexdigest()
    reader = csv.DictReader(payload.decode("utf-8-sig").splitlines())
    if reader.fieldnames != FIELDS:
        raise ValueError("Schema mismatch: expected " + ",".join(FIELDS))
    accepted = rejected = duplicates = 0
    try:
        # Serialize writers; history, rejects and batch receipt commit together.
        db.execute("BEGIN IMMEDIATE")
        previous = db.execute("SELECT 1 FROM batches WHERE digest=?", (digest,)).fetchone()
        if previous:
            db.commit()
            return {"status": "already_loaded", "accepted": 0, "rejected": 0, "duplicates": 0}
        for line, row in enumerate(reader, start=2):
            try:
                firm, payment, version, amount = validate(row)
            except ValueError as error:
                rejected += 1
                db.execute("INSERT INTO rejects VALUES (?,?,?,?)",
                           (digest, line, str(error), json.dumps(row)))
                continue
            old = db.execute(
                "SELECT amount_cents FROM payments WHERE firm_id=? AND payment_id=? AND version=?",
                (firm, payment, version)).fetchone()
            if old is not None:
                if old[0] != amount:
                    raise ValueError(f"Conflicting amount for {firm}/{payment} version {version}; batch rolled back")
                duplicates += 1
                continue
            db.execute("INSERT INTO payments VALUES (?,?,?,?)", (firm, payment, version, amount))
            accepted += 1
        db.execute("INSERT INTO batches (digest,filename,accepted,rejected,duplicates) VALUES (?,?,?,?,?)",
                   (digest, source.name, accepted, rejected, duplicates))
        db.commit()
    except Exception:
        db.rollback()
        raise
    return {"status": "loaded", "accepted": accepted, "rejected": rejected, "duplicates": duplicates}


def reconcile(db, expected):
    if not isinstance(expected, dict) or any(
        not isinstance(k, str) or type(v) is not int or v < 0 for k, v in expected.items()
    ):
        raise ValueError("Expected totals must map firm IDs to non-negative integer cents")
    # Sum in Python to avoid SQLite's signed 64-bit aggregate overflow.
    actual = {}
    for firm, amount in db.execute("SELECT firm_id,amount_cents FROM current_payments"):
        actual[firm] = actual.get(firm, 0) + amount
    results = []
    for firm in sorted(set(actual) | set(expected)):
        observed, target = actual.get(firm, 0), expected.get(firm)
        results.append({
            "firm_id": firm, "actual_cents": observed, "expected_cents": target,
            "difference_cents": None if target is None else observed - target,
            "status": "unmapped" if target is None else ("matched" if observed == target else "mismatch")
        })
    return results


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default="run/ledger.db")
    sub = parser.add_subparsers(dest="command", required=True)
    load = sub.add_parser("ingest")
    load.add_argument("source")
    report = sub.add_parser("reconcile")
    report.add_argument("expected")
    args = parser.parse_args()
    db = connect(args.db)
    try:
        if args.command == "ingest":
            result = ingest(db, args.source)
            print(json.dumps(result, indent=2))
            return 2 if result["rejected"] else 0
        expected = json.loads(Path(args.expected).read_text(encoding="utf-8"))
        results = reconcile(db, expected)
        print(json.dumps(results, indent=2))
        return 0 if all(r["status"] == "matched" for r in results) else 2
    except (ValueError, OSError, sqlite3.Error) as error:
        print(json.dumps({"error": str(error)}))
        return 1
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
