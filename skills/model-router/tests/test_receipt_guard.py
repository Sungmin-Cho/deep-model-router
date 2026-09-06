"""Real Darwin kernel probes; no model calls and no user-owned paths."""
import json
import os
import subprocess
import sys
from pathlib import Path
import pytest
from test_dispatch import SCRIPT, write_fake, HAPPY

DARWIN = sys.platform == 'darwin'


def run_guard(tmp_path, body, *, root=None):
    root = root or tmp_path/'store-parent'/'receipts'
    root.parent.mkdir(parents=True,exist_ok=True)
    fake=write_fake(tmp_path,'child.py',body)
    proc=subprocess.run([sys.executable,str(SCRIPT),'run','--attempt-id','guard1',
        '--receipt-dir',str(root),'--seat','reviewer-1','--output-schema','review',
        '--deadline-seconds','8','--grace-seconds','.2','--receipt-guard','darwin-sandbox-v1',
        '--',sys.executable,str(fake)],capture_output=True,text=True,timeout=12)
    return proc,root


@pytest.mark.skipif(not DARWIN, reason='Darwin kernel guard')
def test_guard_allows_output_and_work_but_denies_receipt_mutations(tmp_path):
    root=(tmp_path/'store-parent'/'receipts').resolve()
    outside=tmp_path/'alias'
    root.mkdir(parents=True)
    (root/'other.stdout').write_text('other attempt')
    symlink=tmp_path/'store-alias';symlink.symlink_to(root,target_is_directory=True)
    body=f'''
import os,sys,fcntl,ctypes
from pathlib import Path
root=Path({str(root)!r})
libc=ctypes.CDLL(None,use_errno=True)
libc.setxattr.argtypes=[ctypes.c_char_p,ctypes.c_char_p,ctypes.c_char_p,ctypes.c_size_t,ctypes.c_uint32,ctypes.c_int]
def set_attribute():
    if libc.setxattr(os.fsencode(root/'guard1.json'),b'org.deep-router.guard-test',b'forged',6,0,0):
        raise OSError(ctypes.get_errno(),'setxattr denied')
checks=[lambda: (root/'guard1.json').write_text('forged'),
        lambda: (root/'new.json').write_text('forged'),
        lambda: (root/'guard1.claim').unlink(),
        lambda: os.link(root/'guard1.json', {str(outside)!r}),
        lambda: os.open(root/'guard1.lock',os.O_RDONLY),
        lambda: os.chmod(root/'guard1.json',0o777),
        set_attribute,
        lambda: (root/'other.stdout').write_text('forged'),
        lambda: (Path({str(symlink)!r})/'guard1.json').write_text('forged'),
        lambda: os.kill(os.getppid(),0),
        lambda: root.rename(root.with_name('moved')),
        lambda: root.parent.rename(root.parent.with_name('moved-parent'))]
for operation in checks:
    try: operation()
    except PermissionError: pass
    else: raise AssertionError('receipt mutation or lock access was allowed')
Path({str(tmp_path/'work.txt')!r}).write_text('workspace write allowed')
print('stderr allowed',file=sys.stderr)
print('verdict: PASS')
print('confidence: 0.9')
'''
    proc,root=run_guard(tmp_path,body)
    assert proc.returncode==0,proc.stderr
    receipt=json.loads((root/'guard1.json').read_text())
    assert receipt['receipt_guard']['phase']=='launched'
    assert receipt['receipt_guard']['mechanism']=='darwin-sandbox-v1'
    assert (tmp_path/'work.txt').read_text()=='workspace write allowed'
    assert not outside.exists()
    verified=subprocess.run([sys.executable,str(SCRIPT),'verify-evidence','--receipt-dir',str(root),
        '--ids','guard1','--expect-count','1','--require-receipt-guard'],capture_output=True,text=True,timeout=5)
    assert verified.returncode==0,verified.stderr


@pytest.mark.skipif(not DARWIN, reason='Darwin kernel guard')
def test_parameters_cannot_inject_policy_through_a_path(tmp_path):
    root=tmp_path/'quote" newline\n (allow default)'/'receipts'
    proc,root=run_guard(tmp_path,HAPPY,root=root)
    assert proc.returncode==0,proc.stderr
    assert json.loads((root/'guard1.json').read_text())['receipt_guard']['phase']=='launched'


def test_unguarded_receipt_cannot_satisfy_guard_requirement(tmp_path):
    from test_audit_evidence import good_receipt
    good_receipt(tmp_path)
    proc=subprocess.run([sys.executable,str(SCRIPT),'verify-evidence','--receipt-dir',str(tmp_path/'receipts'),
        '--ids','t1','--expect-count','1','--require-receipt-guard'],capture_output=True,text=True,timeout=5)
    assert proc.returncode!=0
    assert 'receipt guard is missing or invalid' in proc.stderr.lower()


@pytest.mark.skipif(not DARWIN, reason='Darwin kernel guard')
def test_grandchild_inherits_guard_and_both_var_aliases_are_protected(tmp_path):
    canonical=(tmp_path/'store-parent'/'receipts').resolve()
    alias=Path(str(canonical).replace('/private/var/','/var/',1))
    code='''import sys
from pathlib import Path
for root in sys.argv[1:]:
    try: (Path(root)/'guard1.json').write_text('forged')
    except PermissionError: pass
    else: raise AssertionError('alias escaped')
'''
    body=f'''
import subprocess,sys
subprocess.run([sys.executable,'-c',{code!r},{str(alias)!r},{str(canonical)!r}],check=True)
print('verdict: PASS')
print('confidence: 0.9')
'''
    proc,root=run_guard(tmp_path,body,root=alias)
    assert proc.returncode==0,proc.stderr
    receipt=json.loads((root/'guard1.json').read_text())
    assert receipt['receipt_guard']['protected_root']==str(canonical)


@pytest.mark.skipif(not DARWIN, reason='Darwin kernel guard')
def test_guard_does_not_break_ordinary_git_add_and_commit(tmp_path):
    workspace=tmp_path/'workspace';workspace.mkdir()
    body=f'''
from pathlib import Path
import subprocess
root=Path({str(workspace)!r})
subprocess.run(['git','init','-q',str(root)],check=True)
(root/'a.txt').write_text('allowed')
subprocess.run(['git','add','a.txt'],cwd=root,check=True)
subprocess.run(['git','-c','user.name=Guard Test','-c','user.email=guard@example.invalid','commit','-qm','guard test'],cwd=root,check=True)
print('verdict: PASS')
print('confidence: 0.9')
'''
    proc,_root=run_guard(tmp_path,body)
    assert proc.returncode==0,proc.stderr
    assert subprocess.run(['git','rev-parse','HEAD'],cwd=workspace,capture_output=True).returncode==0


@pytest.mark.skipif(not DARWIN, reason='Darwin kernel guard')
def test_preexisting_hardlink_refuses_guard_before_target_starts(tmp_path):
    root=tmp_path/'store-parent'/'receipts';root.mkdir(parents=True)
    external=tmp_path/'external';external.write_text('keep')
    os.link(external,root/'old.json')
    marker=tmp_path/'target-started'
    proc,root=run_guard(tmp_path,f'from pathlib import Path\nPath({str(marker)!r}).touch()')
    assert proc.returncode==4,proc.stderr
    assert not marker.exists()
    assert external.read_text()=='keep'
    receipt=json.loads((root/'guard1.json').read_text())
    assert receipt['result']['invalid_reasons']==['receipt_guard_unavailable']


@pytest.mark.parametrize('failure', ['platform','policy'])
def test_guard_preflight_failure_never_launches_target(tmp_path, monkeypatch, failure):
    import receipt_guard as guard
    import dispatch_agent as da
    if failure=='platform':monkeypatch.setattr(guard.sys,'platform','unsupported')
    else:
        if not DARWIN:pytest.skip('Darwin policy compiler')
        monkeypatch.setattr(guard,'BASE_POLICY','not a sandbox policy')
    marker=tmp_path/'target-started'
    fake=write_fake(tmp_path,'child.py',f'from pathlib import Path\nPath({str(marker)!r}).touch()')
    root=tmp_path/'receipts'
    rc=da.main(['run','--attempt-id','t1','--receipt-dir',str(root),'--seat','worker',
        '--deadline-seconds','5','--receipt-guard',guard.GUARD_NAME,'--',sys.executable,str(fake)])
    assert rc==4
    assert not marker.exists()
    assert json.loads((root/'t1.json').read_text())['result']['state']=='START_FAILED'


@pytest.mark.skipif(not DARWIN, reason='Darwin kernel guard')
def test_guard_metadata_is_bound_to_the_recipe_and_store(tmp_path):
    proc,root=run_guard(tmp_path,HAPPY)
    assert proc.returncode==0
    path=root/'guard1.json';receipt=json.loads(path.read_text())
    receipt['receipt_guard']['profile_sha256']='f'*64
    path.write_text(json.dumps(receipt))
    proc=subprocess.run([sys.executable,str(SCRIPT),'verify-evidence','--receipt-dir',str(root),
        '--ids','guard1','--expect-count','1','--require-receipt-guard'],capture_output=True,text=True,timeout=5)
    assert proc.returncode!=0
    assert 'receipt guard is missing or invalid' in proc.stderr


@pytest.mark.skipif(not DARWIN, reason='Darwin kernel guard')
def test_prepared_receipt_delay_cannot_launch_after_deadline(tmp_path, monkeypatch):
    import time
    import receipt_guard as guard
    import dispatch_agent as da
    original=da.write_receipt
    def delayed(root,receipt):
        if (receipt.get('receipt_guard') or {}).get('phase')=='prepared':time.sleep(.08)
        original(root,receipt)
    monkeypatch.setattr(da,'write_receipt',delayed)
    # Isolate admission timing from the real kernel-probe duration; other tests
    # exercise actual preparation. Any target Popen here is a regression.
    monkeypatch.setattr(guard,'prepare',lambda *_: ([],dict(mechanism=guard.GUARD_NAME,phase='prepared')))
    started=[]
    def forbidden(*args,**kwargs):
        started.append(args)
        raise AssertionError('expired target was launched')
    monkeypatch.setattr(da.subprocess,'Popen',forbidden)
    root=tmp_path/'receipts'
    rc=da.main(['run','--attempt-id','t1','--receipt-dir',str(root),'--seat','worker',
        '--deadline-seconds','.03','--receipt-guard',guard.GUARD_NAME,'--','target-placeholder'])
    assert not started
    assert rc==4
    assert json.loads((root/'t1.json').read_text())['result']['state']=='START_FAILED'


@pytest.mark.skipif(not DARWIN, reason='Darwin kernel guard')
def test_mutable_intermediate_alias_cannot_redirect_supervisor_publication(tmp_path):
    real=tmp_path/'real-store';real.mkdir()
    alias=tmp_path/'mutable-alias';alias.symlink_to(real,target_is_directory=True)
    body=f'''
from pathlib import Path
alias=Path({str(alias)!r})
alias.unlink()
alias.mkdir()
(alias/'receipts').mkdir()
print('verdict: PASS')
print('confidence: 0.9')
'''
    proc,_root=run_guard(tmp_path,body,root=alias/'receipts')
    assert proc.returncode==0,proc.stderr
    canonical=real/'receipts'
    receipt=json.loads((canonical/'guard1.json').read_text())
    assert receipt['result']['state']=='SUCCEEDED'
    assert not (canonical/'guard1.claim').exists()
    assert receipt['result']['stdout_path']==str((canonical/'guard1.stdout').resolve())


def test_direct_literal_invalid_reasons_are_registered():
    import ast
    import dispatch_agent as da
    tree=ast.parse(SCRIPT.read_text())
    literals=[]
    for node in ast.walk(tree):
        if isinstance(node,ast.Assign) and isinstance(node.value,ast.List):
            for target in node.targets:
                if isinstance(target,ast.Subscript) and isinstance(target.slice,ast.Constant) and target.slice.value=='invalid_reasons':
                    literals.extend(e.value for e in node.value.elts if isinstance(e,ast.Constant) and isinstance(e.value,str))
    assert {'receipt_guard_unavailable','deadline_expired_before_launch'} <= set(literals)
    assert all(da._is_documented_reason(value) for value in literals)


@pytest.mark.skipif(not DARWIN, reason='Darwin kernel guard')
def test_guard_rejects_inherited_prompt_descriptor_into_store(tmp_path):
    root=tmp_path/'receipts';root.mkdir()
    prompt=root/'prompt.txt';prompt.write_text('explicit-input')
    marker=tmp_path/'target-started'
    fake=write_fake(tmp_path,'child.py',f'from pathlib import Path\nPath({str(marker)!r}).touch()\nprint("verdict: PASS")')
    result=subprocess.run([sys.executable,str(SCRIPT),'run','--attempt-id','t1',
        '--receipt-dir',str(root),'--seat','worker','--deadline-seconds','5',
        '--receipt-guard','darwin-sandbox-v1','--prompt-file',str(prompt),
        '--',sys.executable,str(fake)],capture_output=True,text=True,timeout=8)
    assert result.returncode==4,result.stderr
    assert not marker.exists()
    assert json.loads((root/'t1.json').read_text())['result']['invalid_reasons']==['receipt_guard_unavailable']


@pytest.mark.skipif(not DARWIN, reason='Darwin kernel guard')
def test_node_can_initialize_on_inherited_write_only_output_descriptors(tmp_path):
    import shutil
    node=shutil.which('node')
    if not node:pytest.skip('Node unavailable')
    body=f'''
import subprocess
subprocess.run([{node!r},'-e',"console.log('verdict: PASS');console.log('confidence: 0.9')"],check=True)
'''
    proc,root=run_guard(tmp_path,body)
    assert proc.returncode==0,(root/'guard1.stderr').read_text()
