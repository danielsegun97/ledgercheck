"""End-to-end dbt checks using temporary databases; run from the repository root."""
import json
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import duckdb
from pipeline import connect, ingest, reconcile
from warehouse import snapshot


def verify(condition, message):
    if not condition:
        raise AssertionError(message)


def main():
    executable = Path(sys.executable).parent / ('dbt.exe' if os.name == 'nt' else 'dbt')
    # Use the ignored project folder: Windows app temp paths can contain '#',
    # which older dbt adapters parse as a URL fragment.
    (ROOT / 'run').mkdir(exist_ok=True)
    with TemporaryDirectory(prefix='ledgercheck-', dir=ROOT / 'run') as folder:
        tmp = Path(folder)
        source, target = tmp / 'ledger.db', tmp / 'warehouse.duckdb'
        env = dict(os.environ, LEDGERCHECK_WAREHOUSE=target.as_posix(),
                   DBT_SEND_ANONYMOUS_USAGE_STATS='false')

        def build(expect_failure=False):
            result = subprocess.run([
                str(executable), 'build', '--project-dir', str(ROOT / 'dbt'),
                '--profiles-dir', str(ROOT / 'dbt'), '--target-path', str(tmp / 'target'),
                '--log-path', str(tmp / 'logs'), '--no-use-colors'
            ], env=env, cwd=ROOT, capture_output=True, text=True)
            print(result.stdout)
            if result.stderr:
                print(result.stderr, file=sys.stderr)
            verify(result.returncode == (1 if expect_failure else 0), 'Unexpected dbt exit code')
            results = json.loads((tmp / 'target' / 'run_results.json').read_text())['results']
            failures = [r for r in results if r['status'] not in ('success', 'pass')]
            if expect_failure:
                verify(len(failures) == 1 and failures[0]['unique_id'].startswith(
                    'test.ledgercheck.reconciliation_matches') and failures[0]['status'] == 'fail',
                    'Only the reconciliation test should fail')
            else:
                verify(not failures, 'Unexpected failing or skipped nodes')

        def report():
            with duckdb.connect(str(target), read_only=True) as db:
                return db.execute('SELECT * FROM reconciliation ORDER BY firm_id').fetchall()

        expected = json.loads((ROOT / 'data/expected.json').read_text())
        ledger = connect(source)
        try:
            ingest(ledger, ROOT / 'data/payments.csv')
            snapshot(source, target, expected)
            build(expect_failure=True)
            verify(report() == [('firm-a',12500,13000,-500,'mismatch'),
                                ('firm-b',7000,11000,-4000,'mismatch')], 'Initial discrepancies differ')

            ingest(ledger, ROOT / 'data/corrections.csv')
            snapshot(source, target, expected)
            build()
            corrected = report()
            python_rows = reconcile(ledger, expected)
            verify(corrected == [tuple(r[k] for k in (
                'firm_id','actual_cents','expected_cents','difference_cents','status'))
                for r in python_rows], 'dbt and Python disagree after correction')
            with duckdb.connect(str(target), read_only=True) as db:
                verify(db.execute('SELECT count(*) FROM raw.payments').fetchone()[0] == 5,
                       'History must retain five versions')
                verify(db.execute('SELECT count(*) FROM current_payments').fetchone()[0] == 4,
                       'Current model must contain four payments')

            verify(ingest(ledger, ROOT / 'data/corrections.csv')['status'] == 'already_loaded',
                   'Repeated correction should be a no-op')
            late = tmp / 'late.csv'
            late.write_text('firm_id,payment_id,version,amount_cents\nfirm-a,p002,1,2500\n')
            ingest(ledger, late)
            snapshot(source, target, expected)
            build()
            verify(report() == corrected, 'Rerun or old arrival changed current totals')

            # Presence matters even when the expected total is zero.
            snapshot(source, target, {'firm-a':13000, 'missing':0})
            build(expect_failure=True)
            verify(report() == [('firm-a',13000,13000,0,'matched'),
                                ('firm-b',11000,None,None,'unmapped'),
                                ('missing',0,0,0,'missing_payments')], 'Missing firm handling differs')
            try:
                snapshot(source, target, {'firm-a':True})
            except ValueError:
                pass
            else:
                raise AssertionError('Boolean controls must be rejected')
            verify(report()[-1][-1] == 'missing_payments', 'Invalid controls changed snapshot')
        finally:
            ledger.close()
    print('PASS: initial discrepancies, corrections, history, reruns, late versions, missing firms and invalid controls')


if __name__ == '__main__':
    main()
