import copy
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('linkbox', Path(__file__).resolve().parents[1] / 'linkbox.py')
lb = importlib.util.module_from_spec(spec); spec.loader.exec_module(lb)


def ss():
    return lb.new_ss(lb.new_base('ss', '落地', 'exit.example.com', 8388, 38388))


def vless(ep):
    return {**lb.new_base('vless', '入口', 'entry.example.com', 443), 'uuid': 'd7116caa-180c-4150-a00a-1d40d911fbb7',
            'private_key': lb.b64(b'a'*32), 'public_key': lb.b64(b'b'*32), 'short_id': '0123456789abcdef',
            'sni': 'www.example.com', 'egress': ep}


class ModelTests(unittest.TestCase):
    def test_token_and_standard_link_roundtrip(self):
        n = ss(); ep = lb.endpoint_from_node(n)
        self.assertEqual(ep, lb.decode_token(lb.encode_token(ep)))
        self.assertEqual(ep, lb.decode_token(lb.share(n)))
        self.assertEqual(38388, ep['port'])
        n['public_host'] = '2001:db8::1'; n['name'] = 'IPv6 节点'
        self.assertEqual(lb.endpoint_from_node(n), lb.decode_token(lb.share(n)))

    def test_token_rejects_injection_unknown_fields_and_invalid_types(self):
        ep = lb.endpoint_from_node(ss())
        for change in ({'port': True}, {'port': 99999}, {'server': 'a; touch /tmp/pwn'}, {'server': 'x\nfoo'},
                       {'method': 'none'}, {'plugin': 'arbitrary'}, {'password': ''}, {'v': True}):
            bad = dict(ep, **change)
            token = lb.TOKEN_PREFIX + lb.b64(json.dumps(bad).encode())
            with self.assertRaises(lb.Error): lb.decode_token(token)
        for token in ('ENC:unsupported', 'LB1.!', 'LB1.' + lb.b64(b'[]'), lb.share(ss())+'?plugin=evil'):
            # The final string puts the query in the fragment; use a proper query separately.
            if '?plugin' in token: continue
            with self.assertRaises(lb.Error): lb.decode_token(token)
        with self.assertRaises(lb.Error): lb.decode_token(lb.share(ss()).split('#')[0]+'?plugin=evil')

    def test_per_node_routing_has_no_direct_fallback(self):
        ep = lb.endpoint_from_node(ss()); a = vless(ep); b = vless(None)
        b['port'] = 444
        conf = lb.make_config({'version': 1, 'nodes': [a, b]})
        self.assertEqual([{'inbound': [a['id']], 'action': 'route', 'outbound': 'exit-'+a['id']}], conf['route']['rules'])
        self.assertEqual('shadowsocks', conf['outbounds'][1]['type'])
        self.assertNotIn('fallback', json.dumps(conf))
        uri = lb.share(a)
        a['egress'] = lb.endpoint_from_node(ss())
        self.assertEqual(uri, lb.share(a))

    def test_duplicate_ports_and_bad_address_rejected(self):
        a, b = ss(), ss()
        with self.assertRaises(lb.Error): lb.make_config({'version': 1, 'nodes': [a,b]})
        for x in ('https://example.com', '[::1]', 'foo:443', 'bad host', '../example'):
            with self.assertRaises(lb.Error): lb.host(x)


class FakeSystem:
    def __init__(self): self.on = False; self.auto = False; self.fail_once = False
    def active(self): return self.on
    def enabled(self): return self.auto
    def enable(self, yes): self.auto = yes
    def start(self):
        if self.fail_once:
            self.fail_once = False; raise lb.Error('simulated restart failure')
        self.on = True
    def stop(self): self.on = False


class TransactionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.system = FakeSystem()
        self.store = lb.Store(self.tmp.name, '/not-used', self.system)
        self.check = patch.object(self.store, 'check').start()
        self.health = patch.object(self.store, 'healthy').start()
        self.bind = patch.object(lb, 'can_bind', return_value=True).start()
    def tearDown(self): patch.stopall(); self.tmp.cleanup()
    def test_atomic_state_and_failure_restores_running_and_autostart(self):
        first = {'version': 1, 'nodes': [ss()]}; self.store.apply(first)
        before = self.store.generation(); self.system.auto = False
        second = copy.deepcopy(first); second['nodes'][0]['port'] = 8389
        self.system.fail_once = True
        with self.assertRaises(lb.Error): self.store.apply(second)
        self.assertEqual(before, self.store.generation()); self.assertEqual(first, self.store.state())
        self.assertTrue(self.system.on); self.assertFalse(self.system.auto)
        self.assertFalse(self.store.journal.exists())
    def test_conflict_restores_previous_and_invalid_config_never_stops(self):
        first = {'version': 1, 'nodes': [ss()]}; self.store.apply(first)
        self.bind.return_value = False
        with self.assertRaises(lb.Error): self.store.apply(first)
        self.assertTrue(self.system.on); self.assertEqual(first,self.store.state())
        self.check.side_effect = lb.Error('bad config')
        with self.assertRaises(lb.Error): self.store.apply(first)
        self.assertTrue(self.system.on)
    def test_remove_last_node_stops_and_disables(self):
        self.store.apply({'version': 1, 'nodes': [ss()]})
        self.store.apply({'version': 1, 'nodes': []})
        self.assertFalse(self.system.on); self.assertFalse(self.system.auto)
    def test_pending_transaction_recovery(self):
        first = {'version': 1, 'nodes': [ss()]}; self.store.apply(first)
        old = self.store.generation()
        self.store.apply({'version': 1, 'nodes': []})
        lb.atomic_json(self.store.journal, {'old': old, 'new': self.store.generation(), 'running': True, 'enabled': True})
        self.assertTrue(self.store.recover()); self.assertEqual(first,self.store.state())
        self.assertTrue(self.system.on)
    def test_invalid_pointer_is_not_followed(self):
        self.store.current.symlink_to('/tmp')
        with self.assertRaises(lb.Error): self.store.state()


if __name__ == '__main__': unittest.main()
