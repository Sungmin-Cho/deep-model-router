#!/usr/bin/env python3
"""Opt-in diagnostics with CLI-reported usage and collector-measured attempt wall time.

This does not automatically change routing policy or estimate subscription bills.
"""
from __future__ import annotations
import argparse
from datetime import date, datetime, timezone
from decimal import Decimal
import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
import statistics
import subprocess
import sys
import tempfile
import time

import strict_json
import route_task
import dispatch_agent
import receipt_guard

HERE = Path(__file__).resolve().parent
CASES = HERE.parent / 'evals' / 'diagnostic-cases.json'
USAGE_FIELDS = ('input_tokens','cached_input_tokens','cache_write_input_tokens','output_tokens','reasoning_output_tokens')
MAX_BYTES = 4 * 1024 * 1024


class MeasurementPublicationError(RuntimeError):
    def __init__(self, code, message):
        super().__init__(message)
        self.exit_code=code


def read_regular(path, limit=MAX_BYTES):
    fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
    try:
        before=os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink!=1 or before.st_size>limit:
            raise ValueError('evidence is not a bounded single-linked regular file')
        with os.fdopen(os.dup(fd),'rb') as f:raw=f.read(limit+1)
        after=os.fstat(fd)
        if len(raw)>limit or before.st_size!=len(raw) or after.st_size!=len(raw) or before.st_mtime_ns!=after.st_mtime_ns:
            raise ValueError('evidence changed while reading')
        return raw
    finally:os.close(fd)


def _usage(value):
    if not isinstance(value,dict):raise ValueError('usage missing')
    for key in USAGE_FIELDS:
        number=value.get(key)
        if type(number) is not int or not 0<=number<=2**63-1:raise ValueError('invalid usage '+key)
    if value['cached_input_tokens']+value['cache_write_input_tokens']>value['input_tokens']:
        raise ValueError('cache usage exceeds total input')
    if value['reasoning_output_tokens']>value['output_tokens']:raise ValueError('reasoning exceeds output')
    if not value['input_tokens'] or not value['output_tokens']:raise ValueError('zero counters do not attest a model response')
    return {key:value[key] for key in USAGE_FIELDS}


def parse_codex_jsonl(raw):
    if len(raw)>MAX_BYTES:raise ValueError('oversized event stream')
    events=[strict_json.loads(line) for line in raw.splitlines() if line.strip()]
    phase='initial';text=None;usage=None;thread=None
    for event in events:
        if not isinstance(event,dict):raise ValueError('event is not an object')
        kind=event.get('type')
        if phase=='done':raise ValueError('event after completion')
        if kind=='thread.started' and phase=='initial':
            thread=event.get('thread_id')
            if not isinstance(thread,str) or not thread.strip():raise ValueError('thread identity missing')
            phase='thread'
        elif kind=='turn.started' and phase=='thread':phase='turn'
        elif kind in ('item.started','item.updated','item.completed') and phase=='turn':
            item=event.get('item')
            if not isinstance(item,dict) or item.get('type') not in ('agent_message','reasoning'):
                raise ValueError('tool use or unsupported item in tool-free evaluation')
            if kind=='item.completed' and item['type']=='agent_message':
                text=item.get('text')
                if not isinstance(text,str) or not text.strip():raise ValueError('empty assistant message')
        elif kind=='turn.completed' and phase=='turn':
            usage=_usage(event.get('usage'));phase='done'
        else:raise ValueError('unexpected or failed event')
    if phase!='done' or text is None:raise ValueError('complete model turn missing')
    # This is one fresh turn. CLI totals are not summed across events.
    return dict(text=text,usage=usage,thread_id=thread)


def _day(value):
    if not isinstance(value,str) or not re.fullmatch(r'[0-9]{4}-[0-9]{2}-[0-9]{2}',value):raise ValueError('invalid date')
    return date.fromisoformat(value)


def quote_cost(usage,price,as_of,*,single_request=False):
    try:
        if type(single_request) is not bool:raise ValueError('single-request evidence must be boolean')
        usage=_usage(usage);today=_day(as_of);verified=_day(price['verified_on'])
        age=(today-verified).days;max_age=price.get('max_age_days',30)
        if type(max_age) is not int or not 0<=max_age<=30:raise ValueError('invalid price freshness limit')
        if age<0:raise ValueError('future price verification')
        if age>max_age:raise ValueError('stale price verification')
        if price.get('promotional_through') and today>_day(price['promotional_through']):raise ValueError('promotion needs refresh')
        if not isinstance(price.get('source'),str) or not price['source'].startswith('https://'):raise ValueError('price source missing')
        rates=price;long=price.get('long_context')
        if long:
            threshold=long.get('above_input_tokens');boundary=long.get('boundary')
            if type(threshold) is not int or threshold<0 or boundary not in ('exclusive','inclusive'):raise ValueError('invalid price boundary')
            if usage['input_tokens']>threshold or (boundary=='inclusive' and usage['input_tokens']==threshold):
                if single_request is not True:raise ValueError('aggregate usage cannot establish the per-request context tier')
                rates=long
        numbers={}
        for key in ('input','cached_input','cache_write','output'):
            value=rates.get(key)
            if type(value) not in (int,float) or not math.isfinite(value) or value<0:raise ValueError('missing or invalid rate '+key)
            numbers[key]=Decimal(str(value))
        uncached=usage['input_tokens']-usage['cached_input_tokens']-usage['cache_write_input_tokens']
        total=(uncached*numbers['input']+usage['cached_input_tokens']*numbers['cached_input']+
               usage['cache_write_input_tokens']*numbers['cache_write']+usage['output_tokens']*numbers['output'])/Decimal(1000000)
        amount=float(total)
        if not math.isfinite(amount):raise ValueError('reference quote overflow')
        return dict(usd=amount,basis='standard_api_token_equivalent_not_subscription_charge',verified_on=price['verified_on'],source=price['source'])
    except (KeyError,ValueError,TypeError,OverflowError) as exc:
        return dict(usd=None,basis='unavailable',reason=str(exc))


def grade(task,text):
    if not isinstance(task.get('expected'),dict) or not task['expected']:raise ValueError('empty oracle')
    value=strict_json.loads(text)
    if not isinstance(value,dict) or set(value)!={'answers'} or not isinstance(value['answers'],list):raise ValueError('answer envelope invalid')
    answers={}
    for item in value['answers']:
        if not isinstance(item,dict) or set(item)!={'id','answer'}:raise ValueError('answer row invalid')
        key=item['id'];answer=item['answer']
        if not isinstance(key,str) or key in answers:raise ValueError('duplicate or invalid case id')
        if not isinstance(answer,list) or any(not isinstance(x,str) for x in answer):
            raise ValueError('invalid answer value')
        answers[key]=answer
    if set(answers)!=set(task['expected']):raise ValueError('case ids do not match fixed oracle')
    normalize=sorted if task.get('answer_order')=='set' else list
    correct=sum(normalize(answers[key])==normalize(expected) for key,expected in task['expected'].items())
    return dict(passed=correct==len(answers),correct=correct,total=len(answers))


def summarize(records):
    groups={}
    for row in records:groups.setdefault(row['model_key'],[]).append(row)
    models={}
    for model,rows in groups.items():
        prices=[r.get('quote',{}).get('usd') for r in rows]
        quality=[r['quality'] for r in rows if r.get('quality') is not None]
        models[model]=dict(attempts=len(rows),task_successes=sum(q['passed'] is True for q in quality),
            ungraded=len(rows)-len(quality),correct_cases=sum(q['correct'] for q in quality),
            graded_cases=sum(q['total'] for q in quality),median_elapsed_ms=statistics.median(r['elapsed_ms'] for r in rows),
            standard_api_equivalent_usd=sum(prices) if all(p is not None for p in prices) else None,
            known_partial_api_equivalent_usd=sum(p for p in prices if p is not None),quoted_attempts=sum(p is not None for p in prices))
    return dict(models=models,policy_change_admissible=False,
                scope='fixed diagnostic reasoning/review tasks; repeated cases are not independent generalization evidence',
                calibration_decision='retain routing defaults; representative repository tasks and broader independent quality evidence are required before policy changes')


def _write(path,value):
    data=(json.dumps(value,indent=2,ensure_ascii=True,allow_nan=False)+'\n').encode()
    fd,name=tempfile.mkstemp(prefix='.eval-',dir=path.parent)
    try:
        with os.fdopen(fd,'wb') as f:
            f.write(data);f.flush();os.fsync(f.fileno())
        os.link(name,path)  # atomic creation, refusing any existing destination
    finally:
        os.unlink(name)



def _diagnostic(message,*,error=False):
    # Progress is not authority. Avoid buffered shutdown failures and a stuck
    # progress pipe delaying a known termination hold.
    try:
        fd=(sys.stderr if error else sys.stdout).fileno()
        blocking=os.get_blocking(fd)
        try:
            os.set_blocking(fd,False)
            os.write(fd,(str(message)+'\n').encode('utf-8',errors='replace')[:4096])
        finally:os.set_blocking(fd,blocking)
    except (OSError,ValueError,AttributeError):pass


def _publish_measurement(path,value,terminal):
    try:_write(path,value)
    except (OSError,ValueError) as exc:
        raise MeasurementPublicationError(terminal if terminal in (5,8) else 8,str(exc)) from exc


def run(args):
    if sys.platform!='darwin':raise ValueError('live evaluation currently requires the tested Darwin receipt guard')
    cfg=route_task.default_config();keys=[key.strip() for key in args.models.split(',')]
    if not keys or len(keys)!=len(set(keys)) or len(keys)>4:raise ValueError('choose one to four distinct model registry keys')
    for key in keys:
        if key not in cfg['models'] or cfg['models'][key]['family']!='openai' or not cfg['models'][key].get('dispatchable',True):raise ValueError('unsupported evaluation model')
    if not 1<=args.repetitions<=5:raise ValueError('repetitions must be 1..5')
    version=subprocess.run(['codex','--version'],capture_output=True,text=True,check=True,timeout=10).stdout.strip()
    raw=read_regular(CASES);tasks=strict_json.loads(raw)
    if not isinstance(tasks,list) or not 1<=len(tasks)<=20:raise ValueError('invalid diagnostic task set')
    seen=set()
    for task in tasks:
        if not isinstance(task,dict) or not isinstance(task.get('id'),str) or task['id'] in seen:raise ValueError('invalid or duplicate task id')
        seen.add(task['id'])
        if not isinstance(task.get('instructions'),str) or task.get('answer_order') not in ('set','sequence'):raise ValueError('invalid task contract')
        if not isinstance(task.get('cases'),list) or not task['cases']:raise ValueError('empty task cases')
        ids=[c.get('id') for c in task['cases'] if isinstance(c,dict)]
        if len(ids)!=len(task['cases']) or any(not isinstance(i,str) for i in ids) or len(ids)!=len(set(ids)):raise ValueError('invalid case identities')
        if set(ids)!=set(task.get('expected',{})):raise ValueError('oracle/case mismatch')
        grade(task,json.dumps({'answers':[{'id':k,'answer':v} for k,v in task['expected'].items()]}))
    plan=[]
    for rep in range(args.repetitions):
        for index,task in enumerate(tasks):
            offset=(rep+index)%len(keys);order=keys[offset:]+keys[:offset]
            for key in order:
                attempt=dispatch_agent._validated_attempt_id(f'eval-{rep}-{task["id"]}-{key}')
                plan.append((attempt,key,task))
    root=Path(args.output_dir).resolve();root.mkdir(parents=True,exist_ok=False,mode=0o700)
    # Every authoritative file, not just execution receipts, is under the
    # child-denied store. Prompts/schema are deliberately outside it.
    receipts=root
    measurements=root/'measurements';measurements.mkdir(mode=0o700)
    _write(root/'fixed-oracle.json',tasks)
    collector_source=read_regular(Path(__file__))
    with (root/'collector-source.py').open('xb') as f:f.write(collector_source)
    prices={key:cfg['models'][key]['price_per_mtok'] for key in keys}
    manifest=dict(schema_version=1,created_at=datetime.now(timezone.utc).isoformat(),
        as_of=datetime.now(timezone.utc).date().isoformat(),case_sha256=hashlib.sha256(raw).hexdigest(),
        evaluator_sha256=hashlib.sha256(collector_source).hexdigest(),storage_layout='flat-guard-root-v1',
        policy_sha256=route_task.Policy.of(cfg).content_sha,host_cli_version=version,
        planned_ids=[p[0] for p in plan],models={key:cfg['models'][key]['id'] for key in keys},prices=prices,
        effort=args.effort,identity_basis='requested_model; served identity unavailable',
        latency_basis='collector monotonic supervisor wall time, including launch/guard/CLI/publication overhead; all outcomes included',
        native_usage_source='https://github.com/openai/codex/blob/main/codex-rs/exec/src/exec_events.rs')
    _write(root/'manifest.json',manifest)
    schema=dict(type='object',properties=dict(answers=dict(type='array',items=dict(type='object',properties=dict(id=dict(type='string'),answer=dict(type='array',items=dict(type='string'))),required=['id','answer'],additionalProperties=False))),required=['answers'],additionalProperties=False)
    records=[];interrupted=False
    for attempt,key,task in plan:
        scratch=Path(tempfile.mkdtemp(prefix='dmr-eval-')).resolve();prompt=scratch/'prompt.txt';shape=scratch/'schema.json'
        _write(shape,schema)
        public={k:v for k,v in task.items() if k!='expected'}
        prompt.write_text('Complete this fixed diagnostic task. Use no tools or external files. Return only the requested JSON answers.\n'+json.dumps(public)+'\nResponse: {"answers":[{"id":"case id","answer":["answer strings"]}]}\n')
        model=cfg['models'][key]['id'];native=route_task.Policy.of(cfg).native_effort(model,args.effort)
        cmd=[sys.executable,str(HERE/'dispatch_agent.py'),'run','--attempt-id',attempt,'--receipt-dir',str(receipts),
             '--deadline-seconds',str(args.deadline),'--grace-seconds','3','--seat','worker','--runtime','codex',
             '--model-id',model,'--effort-native',native,'--host-cli-version',version,'--child-cwd',str(scratch),'--prompt-file',str(prompt),
             '--receipt-guard',receipt_guard.GUARD_NAME,'--','codex','exec','--ignore-user-config','--skip-git-repo-check',
             '--ephemeral','--sandbox','read-only','--disable','shell_tool','--disable','unified_exec','--disable','multi_agent',
             '-c','project_doc_max_bytes=0','-c','web_search="disabled"','-c',f'model_reasoning_effort="{native}"',
             '--output-schema',str(shape),'--json','-m',model,'-']
        started=time.monotonic();proc=subprocess.Popen(cmd,stdout=subprocess.PIPE,stderr=subprocess.PIPE,start_new_session=True)
        try:supervisor_out,supervisor_err=proc.communicate()
        except KeyboardInterrupt:
            # Keep the owning supervisor alive; request cancellation and let its
            # own deadline cover any brief STARTING window.
            subprocess.run([sys.executable,str(HERE/'dispatch_agent.py'),'cancel','--attempt-id',attempt,'--receipt-dir',str(receipts),'--grace-seconds','3'],capture_output=True)
            supervisor_out,supervisor_err=proc.communicate();interrupted=True
        elapsed=(time.monotonic()-started)*1000
        row=dict(attempt_id=attempt,model_key=key,model_id=model,task_id=task['id'],elapsed_ms=round(elapsed,3),
                 supervisor_exit=proc.returncode,quality=None,usage=None,quote=dict(usd=None,basis='unavailable'),operational_outcome=None)
        try:
            receipt_raw=read_regular(receipts/f'{attempt}.json');receipt=strict_json.loads(receipt_raw)
            output=read_regular(receipts/f'{attempt}.stdout')
            row['supervisor_state']=receipt['result']['state']
            row['receipt_sha256']=hashlib.sha256(receipt_raw).hexdigest();row['stdout_sha256']=hashlib.sha256(output).hexdigest()
            if proc.returncode!=0 or receipt['result']['state']!='SUCCEEDED' or receipt['result']['termination_confirmed'] is not True:raise ValueError('supervisor did not attest success')
            expected=dict(attempt_id=attempt,model_id=model,effort_native=native,runtime='codex',prompt_sha256=hashlib.sha256(prompt.read_bytes()).hexdigest())
            if any(receipt.get(k)!=v for k,v in expected.items()):raise ValueError('receipt request identity mismatch')
            if dispatch_agent._success_evidence_problems(receipts,receipt,attempt):raise ValueError('receipt is unpublished or inconsistent')
            if receipt['result']['output_sha256']!=row['stdout_sha256'] or not receipt_guard.verified_metadata(receipt['receipt_guard'],receipts,attempt):raise ValueError('source receipt linkage invalid')
            parsed=parse_codex_jsonl(output);row['usage']=parsed['usage']
            row['as_of']=receipt['timing']['started_at'][:10]
            row['quote']=quote_cost(parsed['usage'],prices[key],row['as_of'])
            row['quality']=grade(task,parsed['text'])
        except (ValueError,KeyError,OSError,TypeError) as exc:
            row['operational_outcome']={0:'invalid_output',3:'timeout',4:'launch_failure',5:'termination_unconfirmed',6:'invalid_output',7:'cancelled',8:'publication_failure'}.get(proc.returncode,'unknown')
            row['measurement_error']=str(exc)
        _publish_measurement(measurements/f'{attempt}.json',row,proc.returncode);records.append(row)
        _diagnostic(json.dumps(dict(attempt=attempt,passed=(row['quality'] or {}).get('passed'),elapsed_ms=row['elapsed_ms'],outcome=row['operational_outcome'])))
        if interrupted or proc.returncode in (5,8):break  # no further work past uncertain termination/publication
    result=summarize(records);result['collection_complete']=len(records)==len(plan);result['planned_attempts']=len(plan)
    result['valid_measurements']=sum(r['quality'] is not None and r['usage'] is not None and r['quote']['usd'] is not None for r in records)
    result['run_complete']=result['collection_complete'] and result['valid_measurements']==len(plan)
    _publish_measurement(root/'summary.json',result,records[-1]['supervisor_exit'] if records else None)
    if records and records[-1]['supervisor_exit'] in (5,8):return records[-1]['supervisor_exit']
    if interrupted:return 130
    return 0 if result['run_complete'] else 1


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__);sub=parser.add_subparsers(dest='command',required=True)
    live=sub.add_parser('run',help='explicitly invoke configured models and grade fixed diagnostics')
    live.add_argument('--output-dir',required=True,help='new directory; never overwrite an earlier run')
    live.add_argument('--models',default='openai_worker_fast,openai_worker_balanced,openai_reasoning,openai_frontier')
    live.add_argument('--repetitions',type=int,default=2);live.add_argument('--effort',choices=route_task.default_config()['effort_levels'],default='LOW')
    live.add_argument('--deadline',type=float,default=180)
    args=parser.parse_args(argv)
    if not math.isfinite(args.deadline) or not 0<args.deadline<=600:parser.error('deadline must be finite and in (0,600]')
    try:return run(args)
    except MeasurementPublicationError as exc:_diagnostic(str(exc),error=True);return exc.exit_code
    except (ValueError,OSError) as exc:_diagnostic(str(exc),error=True);return 2


if __name__=='__main__':sys.exit(main())
