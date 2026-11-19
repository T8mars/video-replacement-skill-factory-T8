"""Read-only settlement for a production run and its explicitly included repair runs."""
import argparse
from collections import defaultdict
from decimal import Decimal
from fractions import Fraction
import json
from pathlib import Path
import sqlite3

from cost_audit import money, number
import media
from rh_runner import default_private


def audit(projects, ledger, account='default'):
    projects = sorted({str(Path(p).resolve()) for p in projects})
    groups = defaultdict(list)
    pending = []
    db = sqlite3.connect(Path(ledger).resolve().as_uri() + '?mode=ro', uri=True)
    db.row_factory = sqlite3.Row
    try:
        for project in projects:
            for row in db.execute('SELECT * FROM attempts WHERE account=? AND project=?',
                                  ('zhenzhen:' + account, project)):
                if row['task_id']:
                    groups[row['task_id']].append(dict(row))
                elif row['state'] in ('SUBMITTING','SUBMISSION_UNKNOWN'):
                    pending.append(row['window'] + ': submission uncertain')
    finally:
        db.close()
    amounts = defaultdict(Decimal)
    seconds = defaultdict(Fraction)
    generated = Fraction(0)
    for task, rows in groups.items():
        settlements, outputs = set(), set()
        successful = False
        terminal_states = set()
        window = rows[0]['window']
        for row in rows:
            receipt = json.loads(row['receipt'] or '{}')
            if receipt.get('taskId') and str(receipt['taskId']) != task:
                pending.append(window + ': receipt ID conflict')
                continue
            if receipt.get('status') not in ('SUCCESS','FAILED'):
                continue
            terminal_states.add(receipt['status'])
            successful |= receipt['status'] == 'SUCCESS'
            usage = receipt.get('usage') or {}
            amount = number(usage.get('amount'))
            if amount is not None and isinstance(usage.get('currency'), str) and usage['currency'].strip():
                settlements.add((usage['currency'], amount))
            if row['output'] and Path(row['output']).is_file():
                outputs.add(Path(row['output']))
        if len(terminal_states) != 1 or len(settlements) != 1:
            pending.append(window + ': missing or conflicting final settlement')
        else:
            currency, amount = next(iter(settlements))
            amounts[currency] += amount
        if successful:
            if not outputs:
                pending.append(window + ': successful output unavailable')
                continue
            try:
                times = set()
                for path in outputs:
                    video = media.inspect(path)['video']
                    times.add(Fraction(video['end_time']) - Fraction(video['start_time']))
                if len(times) != 1 or min(times) <= 0:
                    raise ValueError('conflicting output clocks')
            except (ValueError, RuntimeError, OSError):
                pending.append(window + ': output duration unverified')
                continue
            duration = times.pop()
            generated += duration
            if len(settlements) == 1:
                seconds[currency] += duration
    complete = bool(groups) and not pending
    rates = {cur: money(value * Decimal(seconds[cur].denominator) / Decimal(seconds[cur].numerator))
             for cur, value in amounts.items() if seconds[cur]} if complete and generated else None
    return {'kind':'zhenzhen-cost-audit', 'accepted_unique_tasks':len(groups),
            'known_amounts':{k:money(v) for k,v in amounts.items()}, 'settled':complete,
            'measured_generated_seconds':str(generated),
            'seconds_by_currency':{k:str(v) for k,v in seconds.items()},
            'actual_cost_per_generated_second':rates, 'pending':pending}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--project', action='append', required=True)
    parser.add_argument('--ledger', type=Path, default=default_private()/'zhenzhen.sqlite')
    parser.add_argument('--account', default='default')
    args = parser.parse_args()
    print(json.dumps(audit(args.project, args.ledger, args.account), indent=2, ensure_ascii=False))
