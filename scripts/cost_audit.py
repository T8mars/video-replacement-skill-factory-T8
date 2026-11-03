"""Read-only project RH bookkeeping. No credentials, network or paid submissions."""
from __future__ import annotations
from pathlib import Path
from decimal import Decimal,InvalidOperation
from collections import defaultdict,Counter
import argparse,json,sqlite3

VERSION=1

def number(value):
    if value is None or isinstance(value,bool):return None
    try:
        result=Decimal(str(value))
        return result if result.is_finite() else None
    except (InvalidOperation,TypeError,ValueError):return None

def money(value):return None if value is None else format(value,'f')

def _receipt(row):
    try:
        value=json.loads(row['receipt']) if row['receipt'] else {}
        return value if isinstance(value,dict) else {}
    except (ValueError,TypeError):return {}

def _success(receipt,row):
    return receipt.get('status')=='SUCCESS' or row.get('state') in ('DOWNLOADED','VISUAL_FAILED','RESULTS_READY','DOWNLOAD_FAILED')

def _owner(project,window,jobs=None):
    queue=Path(project)/'queue.json'
    if not queue.exists():return None
    try:
        jobs=jobs if jobs is not None else json.loads(queue.read_text(encoding='utf-8-sig'))['jobs']
        job=next(e for e in jobs if e['window_id']==window)
        contract=job.get('source_contract',{})
        source_range=contract.get('window',{}).get('source_interval')
        if (not isinstance(source_range,list) or len(source_range)!=2 or
                any(type(n)is not int for n in source_range) or not 0<=source_range[0]<source_range[1]):return None
        return (contract.get('timeline_digest'),tuple(source_range))
    except (ValueError,KeyError,TypeError,StopIteration):return None

def audit(project,ledger,account='default',reduction_assumptions=()):
    project=Path(project).resolve();ledger=Path(ledger).resolve()
    if not ledger.is_file():raise ValueError('Existing ledger required; audit never creates a database')
    db=sqlite3.connect(ledger.as_uri()+'?mode=ro',uri=True);db.row_factory=sqlite3.Row
    try:
        columns={r['name'] for r in db.execute('PRAGMA table_info(attempts)')}
        required={'account','project','window','task_id','receipt','state','created'}
        if not required<=columns:raise ValueError('Ledger schema lacks required bookkeeping columns; no migration was performed')
        rows=[dict(r) for r in db.execute('SELECT account,project,window,task_id,receipt,state,created FROM attempts WHERE project=? AND account=? ORDER BY created',
            (str(project),account))]
    except sqlite3.DatabaseError as exc:raise ValueError('Ledger cannot be read as the supported RH bookkeeping schema') from exc
    finally:db.close()
    grouped=defaultdict(list);unknown=[];deferred=0;mismatched=set()
    queue_path=project/'queue.json'
    try:jobs=json.loads(queue_path.read_text(encoding='utf-8-sig')).get('jobs',[]) if queue_path.exists() else []
    except (ValueError,TypeError):jobs=[]
    for row in rows:
        receipt=_receipt(row);usage=receipt.get('usage')
        receipt_id=str(receipt.get('taskId') or '')
        task_id=str(row.get('task_id') or '')
        if not task_id:
            if receipt_id or isinstance(usage,dict) and any(number(v) not in (None,Decimal(0)) for v in usage.values()):
                unknown.append({'candidate':row['window'],'reason':'unreconciled receipt without ledger task ID'})
            elif row['state'] not in ('DEFERRED','PREPARATION_FAILED'):
                unknown.append({'candidate':row['window'],'reason':'submission or receipt remains uncertain'})
            else:deferred+=1
            continue
        if receipt_id and receipt_id!=task_id:
            mismatched.add(task_id)
            unknown.append({'candidate':row['window'],'reason':'receipt task ID conflicts with ledger'})
        grouped[task_id].append((row,receipt))
    known=[];parents={};aggregate_children=defaultdict(set);unknown_tasks=set()
    for task_id,records in grouped.items():
        if task_id in mismatched:unknown_tasks.add(task_id);continue
        candidates=sorted({r['window'] for r,_ in records})
        charges=[]
        for row,receipt in records:
            usage=receipt.get('usage')
            charge=number(usage.get('consumeCoins')) if isinstance(usage,dict) else None
            if charge is not None:charges.append(charge)
            parent=str(receipt.get('parentTaskId') or '')
            if parent and parent not in ('0',task_id):parents[task_id]=parent
            for item in receipt.get('taskUsageList') or []:
                if isinstance(item,dict):
                    child=str(item.get('taskId') or '')
                    if child and child!=task_id:aggregate_children[task_id].add(child)
        if not charges:
            unknown_tasks.add(task_id);unknown.append({'candidate':','.join(candidates),'reason':'missing numeric consumeCoins receipt'});continue
        if len(set(charges))!=1:
            unknown_tasks.add(task_id);unknown.append({'candidate':','.join(candidates),'reason':'conflicting coin receipts for one task'});continue
        row,receipt=max(records,key=lambda item:item[0].get('created') or 0)
        usage=receipt.get('usage') or {}
        known.append({'task_id':task_id,'candidate':row['window'],'coins':charges[0],
            'cash':number(usage.get('consumeMoney')),'third_party_cash':number(usage.get('thirdPartyConsumeMoney')),
            'success':any(_success(rec,r) for r,rec in records),'created':row.get('created') or 0,
            'owner':_owner(project,row['window'],jobs)})
    overlaps={child for child,parent in parents.items() if parent in grouped}
    overlaps|={parents[child] for child in overlaps}
    for parent,children in aggregate_children.items():
        recorded=children & set(grouped)
        if recorded:overlaps.add(parent);overlaps.update(recorded)
    if overlaps:
        unknown_tasks.update(overlaps)
        unknown.append({'candidate':'parent/child overlap','reason':'parent and child tasks both recorded; aggregate billing may overlap'})
    # Do not publish an exact total while any debit or overlap remains uncertain.
    known_nonoverlap=[r for r in known if r['task_id'] not in overlaps]
    subtotal=sum((r['coins'] for r in known_nonoverlap),Decimal(0))
    full=not unknown
    success_groups=defaultdict(list)
    for row in known_nonoverlap:
        if row['success'] and row['owner'] is not None:success_groups[row['owner']].append(row)
    keys=list(success_groups)
    disjoint=(keys and all(key[0] for key in keys) and len({key[0] for key in keys})==1 and
        not any(a[1][0]<b[1][1] and b[1][0]<a[1][1] for i,a in enumerate(keys) for b in keys[i+1:]))
    baseline=(sum((min(records,key=lambda r:r['created'])['coins'] for records in success_groups.values()),Decimal(0))
        if full and disjoint and all(r['owner'] is not None for r in known_nonoverlap if r['success']) else None)
    extra=subtotal-baseline if baseline is not None else None
    scenarios=[]
    for assumption in reduction_assumptions:
        rate=number(assumption)
        if rate is None or not Decimal(0)<=rate<=Decimal(1):raise ValueError('Scenario reductions must be finite fractions between zero and one')
        if extra is not None and extra>=0:
            saved=extra*rate
            scenarios.append({'assumed_extra_attempt_reduction':money(rate),'coins_saved':money(saved),
                'hypothetical_next_total_coins':money(subtotal-saved),'measured_saving':False})
    cash_values=[r['cash'] for r in known_nonoverlap]
    third_values=[r['third_party_cash'] for r in known_nonoverlap]
    return {'schema_version':VERSION,'kind':'rh-cost-audit','coverage':'complete-saved-receipts' if full else 'incomplete-needs-reconciliation',
        'attempt_rows':len(rows),'accepted_unique_tasks':len(grouped),
        'successful_receipted_tasks':sum(r['success'] for r in known_nonoverlap),'unaccepted_deferred_rows':deferred,
        'known_coin_subtotal':money(subtotal),'total_rh_coins':money(subtotal) if full else None,
        'consume_money_subtotal':money(sum((v for v in cash_values if v is not None),Decimal(0))),
        'consume_money_missing_tasks':sum(v is None for v in cash_values),
        'third_party_money_subtotal':money(sum((v for v in third_values if v is not None),Decimal(0))),
        'third_party_money_missing_tasks':sum(v is None for v in third_values),
        'first_success_owner_groups':len(success_groups),'first_success_baseline_coins':money(baseline),
        'extra_attempt_cost_coins':money(extra),'unknown_items':unknown,'scenario_assumptions':scenarios,
        'notes':['Top-level usage.consumeCoins only; taskUsageList is not added again.',
            'Failed tasks with known charges are included; missing receipts are not zero.',
            'First API success is not visual approval. Extra cost includes necessary repair.',
            'No currency conversion. Scenario savings are hypothetical, not measured.',
            'Private requests, credentials, task IDs and signed URLs are excluded.'], 'cloud_calls':0}

def main():
    ap=argparse.ArgumentParser(description=__doc__);ap.add_argument('--project',type=Path,required=True)
    ap.add_argument('--ledger',type=Path,required=True);ap.add_argument('--account',default='default')
    ap.add_argument('--out',type=Path);ap.add_argument('--scenario-reduction',action='append',default=[])
    args=ap.parse_args()
    result=audit(args.project,args.ledger,args.account,args.scenario_reduction)
    rendered=json.dumps(result,ensure_ascii=False,indent=2)+'\n'
    if args.out:
        if args.out.exists():raise ValueError('Preserve previous audits; choose a new output file')
        if args.out.resolve().is_relative_to(Path(__file__).resolve().parent.parent):raise ValueError('Cost audits must stay outside the installed skill')
        args.out.parent.mkdir(parents=True,exist_ok=True);args.out.write_text(rendered,encoding='utf-8')
    print(rendered,end='')

if __name__=='__main__':main()
