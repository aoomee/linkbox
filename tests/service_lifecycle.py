"""Destructive test ONLY for a fresh disposable root CI host, never a user's server."""
import importlib.util
import os
from pathlib import Path
import shutil
import socket
import sys

root=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('linkbox',root/'linkbox.py')
lb=importlib.util.module_from_spec(spec);spec.loader.exec_module(lb)
assert os.environ.get('LINKBOX_DISPOSABLE_CI')=='1', 'Only run on a disposable CI host'
system=lb.System()
assert all(not p.exists() for p in (lb.ROOT,lb.LIB,lb.LAUNCHER,system.unit)), 'Refusing to touch any existing installation'
core=(root/'.test-core/sing-box').resolve()

def free():
    with socket.socket() as s:s.bind(('127.0.0.1',0));return s.getsockname()[1]

try:
    lb.install(core,root/'linkbox.py',system)
    assert lb.LAUNCHER.exists()
    store=lb.Store(system=system)
    node=lb.new_ss(lb.new_base('ss','CI','127.0.0.1',free(),listen='127.0.0.1'))
    state={'version':1,'nodes':[node]}
    store.apply(state)
    assert system.active() and system.enabled()
    assert not lb.can_bind('127.0.0.1',node['port'],'tcp')
    assert not lb.can_bind('127.0.0.1',node['port'],'udp')
    old=store.generation()
    with socket.socket() as occupied:
        occupied.bind(('127.0.0.1',0));occupied.listen()
        second=lb.new_ss(lb.new_base('ss','blocked','127.0.0.1',occupied.getsockname()[1],listen='127.0.0.1'))
        try:store.apply({'version':1,'nodes':[node,second]})
        except lb.Error:pass
        else:raise AssertionError('port collision should fail')
    assert store.generation()==old and system.active()
    store.apply({'version':1,'nodes':[]})
    assert not system.active() and not system.enabled()
    print('PASS: real '+system.manager+' install/start, TCP+UDP listeners, port collision rollback, empty-state stop/disable')
finally:
    if system.active():system.stop()
    system.enable(False)
    system.unit.unlink(missing_ok=True);system.reload()
    lb.LAUNCHER.unlink(missing_ok=True)
    for p in (lb.ROOT,lb.LIB):
        if p.exists():shutil.rmtree(p)
