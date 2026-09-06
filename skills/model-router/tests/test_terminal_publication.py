"""Terminal authority stays conservative across writes, cancellation, and crashes."""
import copy
import json
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest
from test_audit_evidence import good_receipt
from test_dispatch import SCRIPT, write_fake
import dispatch_agent as da


@pytest.mark.parametrize('state', list(da.EXIT_BY_STATE))
@pytest.mark.parametrize('command', ['status', 'cancel'])
@pytest.mark.parametrize('claim_kind', ['regular', 'dangling_symlink'])
def test_every_terminal_with_a_claim_is_unpublished(tmp_path, state, command, claim_kind):
    receipt = good_receipt(tmp_path)
    receipt['result']['state'] = state
    root = tmp_path / 'receipts'
    (root/'t1.json').write_text(json.dumps(receipt))
    if claim_kind == 'regular':
        (root/'t1.claim').touch()
    else:
        (root/'t1.claim').symlink_to(root/'missing')
    result = subprocess.run([sys.executable,str(SCRIPT),command,'--receipt-dir',str(root),
                             '--attempt-id','t1'],capture_output=True,text=True,timeout=5)
    assert result.returncode == (5 if state == 'TERMINATION_UNCONFIRMED' else 8)
    assert not result.stdout.strip()


def test_publication_failure_retains_claim_and_never_returns_success(tmp_path, monkeypatch):
    receipt = good_receipt(tmp_path)
    root = tmp_path/'receipts'; claim=root/'t1.claim';claim.touch()
    def broken(*args):
        raise OSError('disk full')
    monkeypatch.setattr(da, 'write_receipt', broken)
    assert da._commit_terminal(root,receipt,claim) == 8
    assert claim.exists()


def test_identity_mismatch_cannot_be_overwritten_as_a_valid_publication(tmp_path):
    receipt=good_receipt(tmp_path);root=tmp_path/'receipts';claim=root/'t1.claim';claim.touch()
    other=copy.deepcopy(receipt);other['prompt_sha256']='f'*64
    (root/'t1.json').write_text(json.dumps(other))
    assert da._commit_terminal(root,receipt,claim) == 8
    assert claim.exists()
    assert json.loads((root/'t1.json').read_text()) == other


def test_terminal_reconcile_and_replace_are_one_locked_transaction(tmp_path, monkeypatch):
    receipt=good_receipt(tmp_path);root=tmp_path/'receipts';claim=root/'t1.claim';claim.touch()
    unconfirmed=copy.deepcopy(receipt)
    unconfirmed['result'].update(state='TERMINATION_UNCONFIRMED',termination_confirmed=False)
    entered=threading.Event();release=threading.Event();second_done=threading.Event()
    active=copy.deepcopy(receipt)
    active['result'].update(state='RUNNING',termination_confirmed=None,cancel_requested_at=da._utcnow())
    (root/'t1.json').write_text(json.dumps(active))
    original=da.write_receipt
    def paused(directory,value):
        if threading.current_thread().name=='first':
            entered.set();assert release.wait(3)
        original(directory,value)
    monkeypatch.setattr(da,'write_receipt',paused)
    a=threading.Thread(name='first',target=lambda:da._commit_terminal(root,receipt,claim))
    def second():
        da._commit_terminal(root,unconfirmed,claim);second_done.set()
    b=threading.Thread(target=second)
    a.start();assert entered.wait(3);b.start()
    try:
        assert not second_done.wait(.1), 'another terminal writer bypassed reconciliation lock'
    finally:
        release.set();a.join(4);b.join(4)
    assert not a.is_alive() and not b.is_alive()
    assert json.loads((root/'t1.json').read_text())['result']['state']=='TERMINATION_UNCONFIRMED'


def test_cancel_intent_survives_requester_exit_before_signaling(tmp_path):
    fake=write_fake(tmp_path,'sleep.py','import time\ntime.sleep(30)')
    root=tmp_path/'receipts'
    supervisor=subprocess.Popen([sys.executable,str(SCRIPT),'run','--attempt-id','t1',
        '--receipt-dir',str(root),'--seat','worker','--deadline-seconds','10','--grace-seconds','.1',
        '--',sys.executable,str(fake)],stdout=subprocess.PIPE,stderr=subprocess.PIPE)
    try:
        deadline=time.monotonic()+4
        while time.monotonic()<deadline:
            try:
                receipt=json.loads((root/'t1.json').read_text())
                if receipt['result']['state']=='RUNNING':break
            except (FileNotFoundError,json.JSONDecodeError):pass
            time.sleep(.01)
        else:pytest.fail('supervisor did not enter RUNNING')
        # A requester publishes intent then exits without signaling anything.
        code='import sys,json,os;from pathlib import Path;import dispatch_agent as d;r=Path(sys.argv[1]);d._request_cancellation(r,d.read_receipt(r,"t1"));os._exit(0)'
        result=subprocess.run([sys.executable,'-c',code,str(root)],env={**__import__('os').environ,
            'PYTHONPATH':str(SCRIPT.parent)},capture_output=True,text=True,timeout=3)
        assert result.returncode==0,result.stderr
        assert supervisor.wait(timeout=4)==7
        final=json.loads((root/'t1.json').read_text())
        assert final['result']['state']=='CANCELLED'
        assert final['result']['termination_confirmed'] is True
    finally:
        if supervisor.poll() is None:
            subprocess.run([sys.executable,str(SCRIPT),'cancel','--receipt-dir',str(root),'--attempt-id','t1','--grace-seconds','.1'],capture_output=True,timeout=4)
            supervisor.wait(timeout=4)


def test_failed_running_write_still_publishes_cleanup_from_owned_starting_identity(tmp_path, monkeypatch):
    original=da.write_receipt
    def fail_running(root,receipt):
        if receipt['result']['state']=='RUNNING':raise OSError('running write failed')
        original(root,receipt)
    monkeypatch.setattr(da,'write_receipt',fail_running)
    fake=write_fake(tmp_path,'child.py','import time\ntime.sleep(20)')
    root=tmp_path/'receipts'
    rc=da.main(['run','--attempt-id','t1','--receipt-dir',str(root),'--seat','worker',
                '--deadline-seconds','5','--grace-seconds','.1','--',sys.executable,str(fake)])
    assert rc==9
    final=json.loads((root/'t1.json').read_text())
    assert final['result']['state']=='CANCELLED'
    assert final['result']['termination_confirmed'] is True
    assert not (root/'t1.claim').exists()


def test_failed_claim_release_is_not_published(tmp_path, monkeypatch):
    receipt=good_receipt(tmp_path);root=tmp_path/'receipts';claim=root/'t1.claim';claim.touch()
    unlink=Path.unlink
    def fail(path,*args,**kwargs):
        if path==claim:raise OSError('claim release failed')
        return unlink(path,*args,**kwargs)
    monkeypatch.setattr(Path,'unlink',fail)
    assert da._commit_terminal(root,receipt,claim)==8
    assert claim.exists()


def test_lock_acquisition_has_a_finite_publication_deadline(tmp_path):
    import fcntl
    import os
    receipt=good_receipt(tmp_path);root=tmp_path/'receipts';claim=root/'t1.claim';claim.touch()
    lock=root/'t1.lock';before=lock.stat().st_ino
    fd=os.open(lock,os.O_RDWR)
    fcntl.flock(fd,fcntl.LOCK_EX)
    try:
        start=time.monotonic()
        assert da._commit_terminal(root,receipt,claim)==8
        assert time.monotonic()-start < 3
        assert claim.exists()
    finally:
        fcntl.flock(fd,fcntl.LOCK_UN);os.close(fd)
    assert lock.stat().st_ino==before


def test_stale_cancel_snapshot_cannot_replace_a_published_success(tmp_path, monkeypatch):
    final=good_receipt(tmp_path);root=tmp_path/'receipts'
    active=copy.deepcopy(final);active['result'].update(state='RUNNING',termination_confirmed=None)
    (root/'t1.json').write_text(json.dumps(active));(root/'t1.claim').touch()
    def completed_before_liveness(_pid):
        (root/'t1.json').write_text(json.dumps(final));(root/'t1.claim').unlink()
        return False
    monkeypatch.setattr(da,'_pid_alive',completed_before_liveness)
    assert da.main(['cancel','--receipt-dir',str(root),'--attempt-id','t1'])==0
    assert json.loads((root/'t1.json').read_text())==final


def test_cancel_rechecks_unpublished_terminal_under_the_lock(tmp_path):
    receipt=good_receipt(tmp_path);root=tmp_path/'receipts';(root/'t1.claim').touch()
    stale=copy.deepcopy(receipt);stale['result']['state']='RUNNING'
    with pytest.raises(ValueError,match='publication'):
        da._request_cancellation(root,stale)


def test_unknown_native_state_cannot_be_reconciled_into_success(tmp_path):
    receipt=good_receipt(tmp_path);root=tmp_path/'receipts';claim=root/'t1.claim';claim.touch()
    unknown=copy.deepcopy(receipt);unknown['result']['state']='NOT_A_STATE'
    (root/'t1.json').write_text(json.dumps(unknown))
    assert da._commit_terminal(root,receipt,claim)==8
    assert claim.exists()
    assert json.loads((root/'t1.json').read_text())==unknown


def test_publication_failure_cannot_hide_unconfirmed_termination(tmp_path, monkeypatch):
    receipt=good_receipt(tmp_path);root=tmp_path/'receipts';claim=root/'t1.claim';claim.touch()
    receipt['result'].update(state='TERMINATION_UNCONFIRMED',termination_confirmed=False)
    monkeypatch.setattr(da,'write_receipt',lambda *_: (_ for _ in ()).throw(OSError('disk full')))
    assert da._commit_terminal(root,receipt,claim)==5
    assert claim.exists()


def test_post_spawn_crash_and_publication_failure_preserve_unconfirmed_exit(tmp_path, monkeypatch):
    original_write=da.write_receipt;original_terminate=da.terminate_group
    def write(root,receipt):
        if receipt['result']['state'] not in ('STARTING','RUNNING'):raise OSError('disk full')
        original_write(root,receipt)
    def unconfirmed(*args,**kwargs):
        original_terminate(*args,**kwargs)
        return False
    monkeypatch.setattr(da,'write_receipt',write)
    monkeypatch.setattr(da,'terminate_group',unconfirmed)
    monkeypatch.setattr(da,'_validate_output',lambda *_: (_ for _ in ()).throw(RuntimeError('grading crash')))
    fake=write_fake(tmp_path,'ok.py','print("verdict: PASS")')
    root=tmp_path/'receipts'
    assert da.main(['run','--attempt-id','t1','--receipt-dir',str(root),'--seat','worker',
        '--deadline-seconds','5','--grace-seconds','.1','--',sys.executable,str(fake)])==5
    assert (root/'t1.claim').exists()


def test_cancel_snapshot_rechecks_new_unconfirmed_publication(tmp_path, monkeypatch):
    receipt=good_receipt(tmp_path);root=tmp_path/'receipts'
    active=copy.deepcopy(receipt);active['result'].update(state='RUNNING',termination_confirmed=None)
    (root/'t1.json').write_text(json.dumps(active));(root/'t1.claim').touch()
    def became_unconfirmed(_pid):
        receipt['result'].update(state='TERMINATION_UNCONFIRMED',termination_confirmed=False)
        (root/'t1.json').write_text(json.dumps(receipt))
        return True
    monkeypatch.setattr(da,'_pid_alive',became_unconfirmed)
    assert da.main(['cancel','--receipt-dir',str(root),'--attempt-id','t1'])==5


@pytest.mark.parametrize('confirmation', [None, True, 'false', 0])
def test_malformed_unconfirmed_state_never_becomes_success(tmp_path, confirmation):
    receipt=good_receipt(tmp_path);root=tmp_path/'receipts';claim=root/'t1.claim';claim.touch()
    disk=copy.deepcopy(receipt);disk['result'].update(state='TERMINATION_UNCONFIRMED',termination_confirmed=confirmation)
    (root/'t1.json').write_text(json.dumps(disk))
    assert da._commit_terminal(root,receipt,claim)==5
    assert claim.exists()


@pytest.mark.parametrize('confirmation', [None, False, 'true', 1])
def test_malformed_cancelled_state_is_not_ignored(tmp_path, confirmation):
    receipt=good_receipt(tmp_path);root=tmp_path/'receipts';claim=root/'t1.claim';claim.touch()
    disk=copy.deepcopy(receipt);disk['result'].update(state='CANCELLED',termination_confirmed=confirmation)
    (root/'t1.json').write_text(json.dumps(disk))
    assert da._commit_terminal(root,receipt,claim) in (5,8)
    assert claim.exists()
