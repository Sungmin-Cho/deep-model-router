"""Measured outcomes and price quotes must not manufacture missing evidence."""
import copy
import json
import sys
from pathlib import Path
import pytest
sys.path.insert(0,str(Path(__file__).resolve().parent.parent/'scripts'))
import evaluate_models as ev


def stream(usage=None, text=None):
    usage=usage if usage is not None else dict(input_tokens=1000,cached_input_tokens=200,cache_write_input_tokens=100,output_tokens=50,reasoning_output_tokens=20)
    rows=[dict(type='thread.started',thread_id='thread-1'),dict(type='turn.started'),
          dict(type='item.completed',item=dict(id='item-1',type='agent_message',text=text or '{"answers":[]}')),
          dict(type='turn.completed',usage=usage)]
    return '\n'.join(json.dumps(x) for x in rows).encode()


def price(**over):
    return dict(input=4,output=20,cached_input=.4,cache_write=5,verified_on='2026-09-06',
                max_age_days=30,source='https://developers.openai.com/api/docs/pricing',**over)


def test_native_usage_is_not_double_counted():
    result=ev.parse_codex_jsonl(stream())
    assert result['usage']['output_tokens']==50
    quote=ev.quote_cost(result['usage'],price(),'2026-09-06')
    assert quote['usd']==pytest.approx((700*4+200*.4+100*5+50*20)/1e6)


@pytest.mark.parametrize('value',[True,-1,1.5,'100',None])
def test_invalid_usage_never_becomes_zero_cost(value):
    usage=json.loads(stream().decode().splitlines()[-1])['usage'];usage['input_tokens']=value
    with pytest.raises(ValueError):ev.parse_codex_jsonl(stream(usage))


@pytest.mark.parametrize('change',['missing_complete','duplicate_complete','after_complete','tool_use','error','zero_usage','bad_cache','bad_reasoning'])
def test_incomplete_or_contradictory_stream_is_not_success(change):
    rows=[json.loads(x) for x in stream().decode().splitlines()]
    if change=='missing_complete':rows.pop()
    elif change=='duplicate_complete':rows.append(copy.deepcopy(rows[-1]))
    elif change=='after_complete':rows.append(dict(type='turn.started'))
    elif change=='tool_use':rows.insert(2,dict(type='item.completed',item=dict(id='tool',type='command_execution')))
    elif change=='error':rows.insert(2,dict(type='error',message='failed'))
    elif change=='zero_usage':rows[-1]['usage']={k:0 for k in rows[-1]['usage']}
    elif change=='bad_cache':rows[-1]['usage']['cached_input_tokens']=1001
    else:rows[-1]['usage']['reasoning_output_tokens']=51
    with pytest.raises(ValueError):ev.parse_codex_jsonl('\n'.join(json.dumps(x) for x in rows).encode())


def test_duplicate_json_keys_are_rejected():
    with pytest.raises(ValueError):ev.parse_codex_jsonl(stream()+b'\n{"type":"error","type":"turn.completed"}')


@pytest.mark.parametrize('as_of',['2026-09-05','2026-10-07'])
def test_future_or_stale_price_has_no_quote(as_of):
    quote=ev.quote_cost(ev.parse_codex_jsonl(stream())['usage'],price(),as_of)
    assert quote['usd'] is None


def test_cache_write_usage_is_required_not_assumed_zero():
    rows=[json.loads(x) for x in stream().decode().splitlines()];del rows[-1]['usage']['cache_write_input_tokens']
    with pytest.raises(ValueError):ev.parse_codex_jsonl('\n'.join(json.dumps(x) for x in rows).encode())


def test_long_context_rates_apply_to_the_whole_request_at_exclusive_boundary():
    p=price(long_context=dict(above_input_tokens=272000,boundary='exclusive',input=8,output=30,cached_input=.8,cache_write=10))
    usage=dict(input_tokens=272000,cached_input_tokens=0,cache_write_input_tokens=0,output_tokens=1,reasoning_output_tokens=0)
    assert ev.quote_cost(usage,p,'2026-09-06')['usd']==pytest.approx((272000*4+20)/1e6)
    usage['input_tokens']+=1
    assert ev.quote_cost(usage,p,'2026-09-06')['usd'] is None
    assert ev.quote_cost(usage,p,'2026-09-06',single_request=True)['usd']==pytest.approx((272001*8+30)/1e6)


def test_grading_is_exact_and_does_not_accept_model_self_ratings():
    task=dict(id='x',answer_order='set',expected={'a':['A','B'],'b':[]})
    result=ev.grade(task,json.dumps(dict(answers=[dict(id='a',answer=['B','A']),dict(id='b',answer=[])])))
    assert result==dict(passed=True,correct=2,total=2)
    with pytest.raises(ValueError):ev.grade(task,'{"answers":[{"id":"a","answer":["A","B"]}],"passed":true}')
    with pytest.raises(ValueError):ev.grade(task,'{"answers":[{"id":"a","answer":[]},{"id":"a","answer":[]}]}')


def test_small_diagnostic_suite_cannot_authorize_policy_calibration():
    records=[dict(model_key='m',task_id='a',quality=dict(passed=True,correct=1,total=1),elapsed_ms=1,quote=dict(usd=.1)) for _ in range(10)]
    report=ev.summarize(records)
    assert report['policy_change_admissible'] is False
    assert report['models']['m']['task_successes']==10


@pytest.mark.parametrize('exit_code',[4,6,7,5,8])
def test_no_usable_measurements_cannot_exit_success(tmp_path,monkeypatch,exit_code):
    from types import SimpleNamespace
    monkeypatch.setattr(ev.sys,'platform','darwin')
    monkeypatch.setattr(ev.subprocess,'run',lambda *a,**k:SimpleNamespace(stdout='test-cli'))
    calls=[]
    class Failed:
        returncode=exit_code
        def __init__(self,argv,**kw):calls.append(argv)
        def communicate(self):return b'',b''
    monkeypatch.setattr(ev.subprocess,'Popen',Failed)
    args=SimpleNamespace(models='openai_worker_fast',repetitions=1,effort='LOW',deadline=1,output_dir=str(tmp_path/'run'))
    result=ev.run(args)
    assert result!=0
    report=json.loads((tmp_path/'run/summary.json').read_text())
    assert report['run_complete'] is False
    assert report['models']['openai_worker_fast']['task_successes']==0
    if exit_code in (5,8):
        assert result==exit_code
        assert len(calls)==1


def test_duplicate_answer_values_are_quality_failures_not_ungraded_output():
    task=dict(expected={'a':['A']},answer_order='set')
    assert ev.grade(task,'{"answers":[{"id":"a","answer":["A","A"]}]}')==dict(passed=False,correct=0,total=1)


def test_finite_rates_cannot_overflow_into_an_infinite_quote():
    usage=ev.parse_codex_jsonl(stream())['usage'];usage['input_tokens']=2**63-1
    p=price();p['input']=1e308
    assert ev.quote_cost(usage,p,'2026-09-06')['usd'] is None


@pytest.mark.skipif(sys.platform!='darwin',reason='real guard integration')
def test_collector_protects_manifest_and_measurements_as_well_as_receipts(tmp_path,monkeypatch):
    import os
    from types import SimpleNamespace
    from datetime import datetime,timezone
    cfg=copy.deepcopy(ev.route_task.default_config())
    cfg['models']['openai_worker_fast']['price_per_mtok']['verified_on']=datetime.now(timezone.utc).date().isoformat()
    monkeypatch.setattr(ev.route_task,'default_config',lambda:cfg)
    cases=json.loads(ev.CASES.read_text());gold={t['id']:t['expected'] for t in cases}
    bindir=tmp_path/'bin';bindir.mkdir();stub=bindir/'codex'
    stub.write_text('''#!/usr/bin/env python3
import sys,json,os
from pathlib import Path
if '--version' in sys.argv:
    print('fixture-codex');sys.exit(0)
try: (Path(os.environ['EVAL_FIXTURE_ROOT'])/'manifest.json').write_text('forged')
except PermissionError: pass
else: sys.exit(30)
task=json.loads(sys.stdin.read().splitlines()[1])
gold='''+repr(gold)+'''
answer=json.dumps({'answers':[{'id':k,'answer':v} for k,v in gold[task['id']].items()]})
for row in [{'type':'thread.started','thread_id':'fixture'}, {'type':'turn.started'},
 {'type':'item.completed','item':{'id':'1','type':'agent_message','text':answer}},
 {'type':'turn.completed','usage':{'input_tokens':1000,'cached_input_tokens':0,'cache_write_input_tokens':0,'output_tokens':100,'reasoning_output_tokens':0}}]:
 print(json.dumps(row))
''')
    stub.chmod(0o700)
    monkeypatch.setenv('PATH',str(bindir)+os.pathsep+os.environ['PATH'])
    root=tmp_path/'run';monkeypatch.setenv('EVAL_FIXTURE_ROOT',str(root))
    assert ev.run(SimpleNamespace(models='openai_worker_fast',repetitions=1,effort='LOW',deadline=5,output_dir=str(root)))==0
    manifest=json.loads((root/'manifest.json').read_text())
    assert manifest['host_cli_version']=='fixture-codex'
    assert manifest['storage_layout']=='flat-guard-root-v1'
    assert (root/'collector-source.py').is_file()
    assert len(list((root/'measurements').glob('*.json')))==3
    assert json.loads((root/'summary.json').read_text())['valid_measurements']==3


@pytest.mark.parametrize('terminal,expected',[(5,5),(7,130)])
def test_interruption_preserves_supervisor_termination_outcome(tmp_path,monkeypatch,terminal,expected):
    from types import SimpleNamespace
    monkeypatch.setattr(ev.sys,'platform','darwin')
    monkeypatch.setattr(ev.subprocess,'run',lambda *a,**k:SimpleNamespace(stdout='fixture-cli',returncode=7))
    class Interrupted:
        returncode=None
        calls=0
        def __init__(self,*a,**k):pass
        def communicate(self):
            self.calls+=1
            if self.calls==1:raise KeyboardInterrupt
            self.returncode=terminal
            return b'',b''
    monkeypatch.setattr(ev.subprocess,'Popen',Interrupted)
    args=SimpleNamespace(models='openai_worker_fast',repetitions=1,effort='LOW',deadline=1,output_dir=str(tmp_path/'run'))
    try:result=ev.run(args)
    except KeyboardInterrupt:pytest.fail('interruption discarded the known termination outcome')
    assert result==expected
    assert json.loads((tmp_path/'run/summary.json').read_text())['run_complete'] is False


@pytest.mark.parametrize('proof',['false','true',1,[],{}])
def test_per_request_price_evidence_is_an_exact_boolean(proof):
    p=price(long_context=dict(above_input_tokens=272000,boundary='exclusive',input=8,output=30,cached_input=.8,cache_write=10))
    usage=dict(input_tokens=272001,cached_input_tokens=0,cache_write_input_tokens=0,output_tokens=1,reasoning_output_tokens=0)
    assert ev.quote_cost(usage,p,'2026-09-06',single_request=proof)['usd'] is None


@pytest.mark.parametrize('terminal',[5,8])
@pytest.mark.parametrize('surface',['row','summary'])
def test_collector_write_failure_preserves_known_terminal_hold(tmp_path,monkeypatch,terminal,surface):
    from types import SimpleNamespace
    monkeypatch.setattr(ev.sys,'platform','darwin')
    monkeypatch.setattr(ev.subprocess,'run',lambda *a,**k:SimpleNamespace(stdout='fixture-cli'))
    class Failed:
        returncode=terminal
        def __init__(self,*a,**k):pass
        def communicate(self):return b'',b''
    monkeypatch.setattr(ev.subprocess,'Popen',Failed)
    original=ev._write
    def broken(path,value):
        if (surface=='row' and path.parent.name=='measurements') or (surface=='summary' and path.name=='summary.json'):
            raise OSError('collector disk full')
        original(path,value)
    monkeypatch.setattr(ev,'_write',broken)
    assert ev.main(['run','--models','openai_worker_fast','--repetitions','1','--deadline','1','--output-dir',str(tmp_path/'run')])==terminal


@pytest.mark.parametrize('terminal',[5,8])
@pytest.mark.parametrize('publication_error',[False,True])
def test_broken_diagnostic_descriptors_cannot_erase_terminal_hold(tmp_path,monkeypatch,terminal,publication_error):
    from types import SimpleNamespace
    monkeypatch.setattr(ev.sys,'platform','darwin')
    monkeypatch.setattr(ev.subprocess,'run',lambda *a,**k:SimpleNamespace(stdout='fixture-cli'))
    class Failed:
        returncode=terminal
        def __init__(self,*a,**k):pass
        def communicate(self):return b'',b''
    monkeypatch.setattr(ev.subprocess,'Popen',Failed)
    def broken(*a,**k):raise BrokenPipeError('closed diagnostic descriptor')
    monkeypatch.setattr(ev,'print',broken,raising=False)
    monkeypatch.setattr(ev.os,'write',broken)
    if publication_error:
        original=ev._write
        def failed_write(path,value):
            if path.parent.name=='measurements':raise OSError('disk full')
            original(path,value)
        monkeypatch.setattr(ev,'_write',failed_write)
    try:result=ev.main(['run','--models','openai_worker_fast','--repetitions','1','--output-dir',str(tmp_path/'run')])
    except OSError:pytest.fail('diagnostic write erased terminal status')
    assert result==terminal
