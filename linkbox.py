#!/usr/bin/env python3
"""LINKBOX: small, independent VLESS Reality / Shadowsocks manager."""
from __future__ import annotations

import base64
import copy
import errno
import fcntl
import getpass
import hashlib
import ipaddress
import json
import os
import platform
from pathlib import Path
import re
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import tarfile
import time
import urllib.parse
import urllib.request
import uuid
from contextlib import contextmanager

VERSION = '1.0.1'
CORE_VERSION = '1.14.2'
METHODS = ('aes-256-gcm', 'chacha20-ietf-poly1305', 'aes-128-gcm')
ROOT = Path('/etc/linkbox')
LIB = Path('/usr/local/lib/linkbox')
CORE = LIB / 'sing-box'
LAUNCHER = Path('/usr/local/bin/lb')
SERVICE = 'linkbox'
TOKEN_PREFIX = 'LB1.'
CORE_HASHES = {
    'amd64-glibc': '5c7bc18461827b28d0e5ee7e89d33b276d3ff7c818531104c8e8d26d85b0656e',
    'arm64-glibc': '87db5c3a96ebad1c44c0be1fe7955db2f76b8c97bbc0ed62173063d675e078cf',
    'armv7-glibc': '550431f271700aa25b266093ac7549568dbc4174d2b36fbfcdc2613504dd1a1a',
    'amd64-musl': '8f6cb4bcf94d2b33c65d52e0d5b142db29a938336f1ff7267f397ac3758fc297',
    'arm64-musl': '675297394f9430cebb72b3c48ba8bce0d6f7c750a9d68a8f7f88c515c8255cd1',
    'armv7-musl': 'ba873d9e1eaff53dd6f678ecc315ac6fcb9d28b32b550e1ecc8227634955b56f',
}


class Error(Exception):
    pass


def text(value, maximum=80):
    if not isinstance(value, str) or not value or len(value) > maximum:
        raise Error('文本为空或过长。')
    if any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise Error('文本不能含控制字符。')
    return value


def host(value):
    value = text(value, 253)
    try:
        return str(ipaddress.ip_address(value))
    except ValueError:
        if ':' in value or '%' in value:
            raise Error('IP 地址格式不正确；不要附带端口，IPv6 不加方括号。')
        value = value.rstrip('.').lower()
        if not value or any(not re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?', x) for x in value.split('.')):
            raise Error('请填写有效 IP 或域名，不带协议和路径。')
        return value


def port(value):
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 65535:
        raise Error('端口应为 1–65535 的整数。')
    return value


def ip_literal(value):
    try:
        return str(ipaddress.ip_address(value))
    except ValueError:
        raise Error('监听地址必须是 IP，例如 0.0.0.0 或 ::。')


def b64(data):
    return base64.urlsafe_b64encode(data).decode().rstrip('=')


def unb64(value):
    try:
        return base64.b64decode(value + '=' * (-len(value) % 4), altchars=b'-_', validate=True)
    except (ValueError, TypeError):
        raise Error('Base64 格式无效。')


def endpoint(value):
    required = {'v', 'protocol', 'name', 'server', 'port', 'method', 'password'}
    if not isinstance(value, dict) or set(value) != required:
        raise Error('Token 字段不正确；只接受 LINKBOX Token 或标准 SS 链接。')
    if type(value['v']) is not int or value['v'] != 1 or value['protocol'] != 'ss':
        raise Error('Token 版本或协议不受支持。')
    if value['method'] not in METHODS:
        raise Error('目前支持 AES-128/256-GCM 和 ChaCha20-IETF-Poly1305。')
    return {**value, 'name': text(value['name']), 'server': host(value['server']),
            'port': port(value['port']), 'password': text(value['password'], 256)}


def encode_token(value):
    return TOKEN_PREFIX + b64(json.dumps(endpoint(value), ensure_ascii=False, separators=(',', ':')).encode())


def decode_token(value):
    if not isinstance(value, str) or len(value) > 8192:
        raise Error('Token 太长或格式不正确。')
    value = value.strip()
    if value.startswith(TOKEN_PREFIX):
        try:
            return endpoint(json.loads(unb64(value[len(TOKEN_PREFIX):])))
        except (ValueError, UnicodeError):
            raise Error('Token 内容不是有效 JSON。')
    if value.startswith('ss://'):
        try:
            u = urllib.parse.urlsplit(value)
            if u.query or u.path not in ('', '/') or not u.hostname or not u.port:
                raise Error('只接受无插件的标准 SS 链接。')
            userinfo = u.netloc.rsplit('@', 1)[0]
            credentials = urllib.parse.unquote(userinfo)
            if ':' not in credentials:
                credentials = unb64(credentials).decode()
            method, password = credentials.split(':', 1)
            return endpoint({'v': 1, 'protocol': 'ss', 'name': urllib.parse.unquote(u.fragment) or 'SS 落地',
                             'server': u.hostname, 'port': u.port, 'method': method, 'password': password})
        except (ValueError, UnicodeError):
            raise Error('SS 链接格式不正确。')
    raise Error('请粘贴 LB1. 开头的 Token 或 ss:// 链接。')


def endpoint_from_node(n):
    if n['kind'] != 'ss':
        raise Error('只有 SS 落地节点可以导出连接 Token。')
    return endpoint({'v': 1, 'protocol': 'ss', 'name': n['name'], 'server': n['public_host'],
                     'port': n['public_port'], 'method': n['method'], 'password': n['password']})


def authority(server, number):
    return f'[{server}]:{number}' if ':' in server else f'{server}:{number}'


def share(n):
    address = authority(n['public_host'], n['public_port'])
    if n['kind'] == 'ss':
        return f"ss://{b64((n['method'] + ':' + n['password']).encode())}@{address}#{urllib.parse.quote(n['name'])}"
    if n['kind'] == 'vless':
        query = urllib.parse.urlencode({'encryption': 'none', 'security': 'reality', 'type': 'tcp',
                                        'flow': 'xtls-rprx-vision', 'sni': n['sni'], 'fp': 'chrome',
                                        'pbk': n['public_key'], 'sid': n['short_id']})
        return f"vless://{n['uuid']}@{address}?{query}#{urllib.parse.quote(n['name'])}"
    return address


def validate_state(state):
    if not isinstance(state, dict) or set(state) != {'version', 'nodes'} or state['version'] != 1:
        raise Error('本地状态版本不正确。')
    if not isinstance(state['nodes'], list) or len(state['nodes']) > 256:
        raise Error('节点列表无效或超过 256 个。')
    ids, ports = set(), set()
    for n in state['nodes']:
        if not isinstance(n, dict):
            raise Error('节点格式错误。')
        common = {'id', 'kind', 'name', 'listen', 'port', 'public_host', 'public_port'}
        extras = {'ss': {'method', 'password'}, 'vless': {'uuid', 'private_key', 'public_key', 'short_id', 'sni', 'egress'},
                  'forward': {'target_host', 'target_port', 'network'}}
        kind = n.get('kind')
        if kind not in extras or set(n) != common | extras[kind]:
            raise Error('节点字段不正确。')
        if not re.fullmatch(r'n-[a-f0-9]{12}', n['id']) or n['id'] in ids:
            raise Error('节点 ID 重复或格式错误。')
        ids.add(n['id'])
        text(n['name']); ip_literal(n['listen']); port(n['port'])
        host(n['public_host']); port(n['public_port'])
        # Avoid ambiguous wildcard / dual-stack collisions between managed nodes.
        if n['port'] in ports:
            raise Error('本机监听端口重复。')
        ports.add(n['port'])
        if kind == 'ss':
            endpoint_from_node(n)
        elif kind == 'vless':
            try:
                uuid.UUID(n['uuid'])
            except (ValueError, AttributeError):
                raise Error('VLESS UUID 无效。')
            if any(len(unb64(n[k])) != 32 for k in ('private_key', 'public_key')):
                raise Error('Reality 密钥长度错误。')
            if not re.fullmatch(r'[a-f0-9]{16}', n['short_id']):
                raise Error('Reality Short ID 格式错误。')
            host(n['sni'])
            if n['egress'] is not None:
                endpoint(n['egress'])
        else:
            host(n['target_host']); port(n['target_port'])
            if n['network'] not in ('tcp', 'udp', 'both'):
                raise Error('转发协议无效。')
    return state


def make_config(state):
    validate_state(state)
    config = {'log': {'level': 'warn', 'timestamp': True},
              'dns': {'servers': [{'type': 'local', 'tag': 'system-dns'}]},
              'inbounds': [], 'outbounds': [{'type': 'direct', 'tag': 'direct'}],
              'route': {'rules': [], 'final': 'direct', 'default_domain_resolver': 'system-dns'}}
    for n in state['nodes']:
        inbound = {'tag': n['id'], 'listen': n['listen'], 'listen_port': n['port']}
        if n['kind'] == 'ss':
            inbound.update(type='shadowsocks', method=n['method'], password=n['password'])
        elif n['kind'] == 'vless':
            inbound.update(type='vless', users=[{'uuid': n['uuid'], 'flow': 'xtls-rprx-vision'}],
                           tls={'enabled': True, 'server_name': n['sni'], 'reality': {
                               'enabled': True, 'handshake': {'server': n['sni'], 'server_port': 443},
                               'private_key': n['private_key'], 'short_id': [n['short_id']]}})
            if n['egress']:
                ep = endpoint(n['egress'])
                tag = 'exit-' + n['id']
                config['outbounds'].append({'type': 'shadowsocks', 'tag': tag, 'server': ep['server'],
                                            'server_port': ep['port'], 'method': ep['method'], 'password': ep['password']})
                # No fallback: a failed SS exit must not silently use this server's IP.
                config['route']['rules'].append({'inbound': [n['id']], 'action': 'route', 'outbound': tag})
        else:
            inbound.update(type='direct', override_address=n['target_host'], override_port=n['target_port'])
            if n['network'] != 'both':
                inbound['network'] = n['network']
        config['inbounds'].append(inbound)
    return config


def run(args, timeout=30, check=True):
    try:
        result = subprocess.run([str(a) for a in args], stdin=subprocess.DEVNULL, capture_output=True,
                                text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise Error(f'无法完成 {Path(str(args[0])).name} 操作：{type(exc).__name__}')
    if check and result.returncode:
        # Avoid echoing config or token values from a failed parser into terminal logs.
        raise Error(f'{Path(str(args[0])).name} 执行失败（退出码 {result.returncode}）。可在“诊断”中查看服务日志。')
    return result


def atomic_bytes(path, data, mode=0o600):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, tmp = tempfile.mkstemp(prefix='.' + path.name + '-', dir=path.parent)
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, 'wb') as f:
            f.write(data); f.flush(); os.fsync(f.fileno())
        os.replace(tmp, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def atomic_json(path, value):
    atomic_bytes(path, (json.dumps(value, ensure_ascii=False, indent=2) + '\n').encode())


class System:
    def __init__(self):
        release = {}
        for line in Path('/etc/os-release').read_text().splitlines():
            if '=' in line:
                k, v = line.split('=', 1); release[k] = v.strip('"\'')
        self.distro = release.get('ID', '')
        if self.distro == 'alpine' and Path('/sbin/openrc-run').exists():
            self.manager = 'openrc'; self.unit = Path('/etc/init.d/linkbox')
        elif self.distro in ('debian', 'ubuntu') and Path('/run/systemd/system').is_dir():
            self.manager = 'systemd'; self.unit = Path('/etc/systemd/system/linkbox.service')
        else:
            raise Error('支持 Debian/Ubuntu + systemd、Alpine + OpenRC；普通容器不适用。')

    def command(self, action):
        return ['systemctl', action, SERVICE] if self.manager == 'systemd' else ['rc-service', SERVICE, action]

    def active(self):
        cmd = ['systemctl', 'is-active', '--quiet', SERVICE] if self.manager == 'systemd' else self.command('status')
        return run(cmd, check=False).returncode == 0

    def enabled(self):
        if self.manager == 'systemd':
            return run(['systemctl', 'is-enabled', '--quiet', SERVICE], check=False).returncode == 0
        return any(line.split() and line.split()[0] == SERVICE for line in run(['rc-update', 'show', 'default']).stdout.splitlines())

    def enable(self, yes):
        if self.manager == 'systemd':
            run(['systemctl', 'enable' if yes else 'disable', SERVICE])
        elif yes:
            run(['rc-update', 'add', SERVICE, 'default'])
        elif self.enabled():
            run(['rc-update', 'del', SERVICE, 'default'])

    def reload(self):
        if self.manager == 'systemd':
            run(['systemctl', 'daemon-reload'])

    def start(self):
        run(self.command('start'))

    def stop(self):
        run(self.command('stop'))

    def logs(self):
        if self.manager == 'systemd':
            return run(['journalctl', '-u', SERVICE, '-n', '12', '--no-pager'], check=False).stdout
        p = Path('/var/log/linkbox.log')
        if not p.exists():
            return '暂无日志。'
        with p.open('rb') as f:
            f.seek(max(0, p.stat().st_size - 12000))
            return b'\n'.join(f.read().splitlines()[-12:]).decode(errors='replace')

    def definition(self):
        if self.manager == 'systemd':
            return '''[Unit]
Description=LINKBOX VLESS and Shadowsocks
Wants=network-online.target
After=network-online.target
StartLimitIntervalSec=0
[Service]
Type=simple
ExecStart=/usr/local/lib/linkbox/sing-box run -c /etc/linkbox/current/config.json
Restart=on-failure
RestartSec=5
TimeoutStopSec=20
UMask=0077
StandardOutput=journal
StandardError=journal
[Install]
WantedBy=multi-user.target
'''
        return '''#!/sbin/openrc-run
description="LINKBOX VLESS and Shadowsocks"
command="/usr/local/lib/linkbox/sing-box"
command_args="run -c /etc/linkbox/current/config.json"
supervisor="supervise-daemon"
respawn_delay=5
respawn_max=0
output_log="/var/log/linkbox.log"
error_log="/var/log/linkbox.log"
depend() {
    need net
    after firewall
}
'''


def bindings(state):
    for n in state['nodes']:
        protocols = ('tcp',) if n['kind'] == 'vless' else ('tcp', 'udp')
        if n['kind'] == 'forward' and n['network'] != 'both':
            protocols = (n['network'],)
        for proto in protocols:
            yield n['listen'], n['port'], proto


def can_bind(address, number, proto):
    family = socket.AF_INET6 if ':' in address else socket.AF_INET
    kind = socket.SOCK_STREAM if proto == 'tcp' else socket.SOCK_DGRAM
    with socket.socket(family, kind) as s:
        if kind == socket.SOCK_STREAM:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind((address, number))
            if kind == socket.SOCK_STREAM:
                s.listen(1)
            return True
        except OSError as exc:
            if exc.errno == errno.EADDRINUSE:
                return False
            raise Error(f'无法监听 {address}:{number}/{proto}：{exc.strerror}')


class Store:
    def __init__(self, root=ROOT, core=CORE, system=None):
        self.root, self.core = Path(root), Path(core)
        self.system = system if system is not None else System()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.versions = self.root / 'versions'; self.versions.mkdir(exist_ok=True, mode=0o700)
        self.current = self.root / 'current'; self.journal = self.root / 'transaction.json'

    def generation(self):
        if not self.current.is_symlink():
            if self.current.exists():
                raise Error('current 不是本工具的配置链接。')
            return None
        dest = os.readlink(self.current)
        if not re.fullmatch(r'versions/[a-f0-9]{16}', dest):
            raise Error('配置链接不在受管理的目录内。')
        return dest

    def state(self):
        gen = self.generation()
        if gen is None:
            return {'version': 1, 'nodes': []}
        return validate_state(json.loads((self.root / gen / 'state.json').read_text()))

    def point(self, gen):
        if gen is None:
            self.current.unlink(missing_ok=True)
            return
        link = self.root / ('.current-' + secrets.token_hex(4))
        try:
            os.symlink(gen, link)
            os.replace(link, self.current)
        finally:
            link.unlink(missing_ok=True)
        fd = os.open(self.root, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    def restore(self, txn):
        if self.system.active():
            self.system.stop()
        self.point(txn['old'])
        self.system.enable(txn['enabled'])
        if txn['running'] and txn['old']:
            self.system.start()
            if not self.system.active():
                raise Error('原配置已恢复，但服务未能恢复，请查看日志。')

    def recover(self):
        if self.journal.exists():
            txn = json.loads(self.journal.read_text())
            if txn.get('old') is not None and not re.fullmatch(r'versions/[a-f0-9]{16}', txn['old']):
                raise Error('恢复记录格式不正确。')
            self.restore(txn)
            self.journal.unlink()
            return True
        return False

    def check(self, path):
        run([self.core, 'check', '-c', path])

    def healthy(self, state):
        for _ in range(30):
            if self.system.active() and all(not can_bind(a, p, t) for a, p, t in bindings(state)):
                return
            time.sleep(0.2)
        raise Error('服务或监听端口未就绪，已尝试恢复旧配置。')

    def apply(self, state):
        config = make_config(state)
        gen = 'versions/' + secrets.token_hex(8)
        folder = self.root / gen; folder.mkdir(mode=0o700)
        atomic_json(folder / 'state.json', state); atomic_json(folder / 'config.json', config)
        try:
            self.check(folder / 'config.json')
        except BaseException:
            shutil.rmtree(folder); raise
        txn = {'old': self.generation(), 'new': gen, 'running': self.system.active(), 'enabled': self.system.enabled()}
        atomic_json(self.journal, txn)
        try:
            if txn['running']:
                self.system.stop()
            for a, p, t in bindings(state):
                if not can_bind(a, p, t):
                    raise Error(f'端口 {p}/{t} 已被其他程序占用。')
            self.point(gen)
            if state['nodes']:
                self.system.start(); self.healthy(state); self.system.enable(True)
            else:
                self.system.enable(False)
            self.journal.unlink()
        except BaseException as error:
            try:
                self.restore(txn); self.journal.unlink(); shutil.rmtree(folder)
            except Exception as rollback_error:
                raise Error('保存失败，自动恢复也未完成；保留恢复记录，下次运行 lb 会重试。') from rollback_error
            raise error
        # Keep the preceding generation for local recovery; no remote backups.
        keep = {gen, txn['old']}
        for old in self.versions.iterdir():
            if 'versions/' + old.name not in keep and re.fullmatch(r'[a-f0-9]{16}', old.name):
                shutil.rmtree(old)


@contextmanager
def locked():
    path = Path('/run/lock/linkbox.lock'); path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a') as f:
        os.chmod(path, 0o600)
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise Error('另一个 LINKBOX 窗口正在操作，请先退出它。')
        yield


def reality_keys(core=CORE):
    result = run([core, 'generate', 'reality-keypair']).stdout
    private = re.search(r'PrivateKey:\s*(\S+)', result)
    public = re.search(r'PublicKey:\s*(\S+)', result)
    if not private or not public:
        raise Error('无法生成 Reality 密钥。')
    return private[1], public[1]


def new_base(kind, name, address, number, public_number=None, listen=None):
    address = host(address)
    return {'id': 'n-' + secrets.token_hex(6), 'kind': kind, 'name': text(name),
            'listen': listen or ('::' if ':' in address else '0.0.0.0'), 'port': port(number),
            'public_host': address, 'public_port': port(public_number or number)}


def new_ss(base):
    return {**base, 'method': METHODS[0], 'password': secrets.token_urlsafe(32)}


def new_vless(base, sni, ep=None, core=CORE):
    private, public = reality_keys(core)
    return {**base, 'uuid': str(uuid.uuid4()), 'private_key': private, 'public_key': public,
            'short_id': secrets.token_hex(8), 'sni': host(sni), 'egress': endpoint(ep) if ep else None}


class UI:
    def __init__(self, store):
        self.store = store
        self.color = sys.stdout.isatty() and not os.environ.get('NO_COLOR')
        self.tty = open('/dev/tty', 'r+')

    def line(self, value=''):
        print('  ' + value)

    def title(self, name):
        print()
        self.line(('\033[36m' if self.color else '') + name + ('\033[0m' if self.color else ''))
        print()

    def ask(self, label, default=None, validate=None, secret=False):
        while True:
            prompt = f'  {label}' + (f' [{default}]' if default is not None else '') + '：'
            if secret:
                value = getpass.getpass(prompt, stream=self.tty)
            else:
                print(prompt, end='', flush=True)
                line = self.tty.readline()
                if not line:
                    raise KeyboardInterrupt
                value = line.strip()
            value = value if value else (str(default) if default is not None else '')
            try:
                return validate(value) if validate else value
            except (Error, ValueError) as exc:
                self.line(str(exc) if isinstance(exc, Error) else '请输入有效数字。')

    def number(self, label, default):
        return self.ask(label, default, lambda s: port(int(s)))

    def pause(self):
        self.ask('回车返回', '')

    def choose_node(self, kinds=None):
        nodes = [n for n in self.store.state()['nodes'] if kinds is None or n['kind'] in kinds]
        if not nodes:
            raise Error('还没有可选节点。')
        for index, n in enumerate(nodes, 1):
            dest = ''
            if n['kind'] == 'vless':
                dest = ' → ' + (n['egress']['name'] if n['egress'] else '本机出口')
            self.line(f"{index}  {n['name']}  · {n['kind'].upper()} :{n['port']}{dest}")
        def selection(s):
            i = int(s)
            if not 1 <= i <= len(nodes):
                raise Error('编号不在列表中。')
            return copy.deepcopy(nodes[i-1])
        return self.ask('选择节点', 1, selection)

    def base(self, kind, default_name, default_port):
        self.line('公网地址填客户端能访问的入口；NAT 可填写其他地区的映射入口。')
        name = self.ask('名称', default_name, text)
        previous = self.store.state()['nodes']
        address = self.ask('公网 IP / 域名', previous[-1]['public_host'] if previous else None, host)
        number = self.number('本机监听端口', default_port)
        external = self.number('公网连接端口', number)
        return new_base(kind, name, address, number, external)

    def save_node(self, node, replace=False):
        state = copy.deepcopy(self.store.state())
        if replace:
            state['nodes'] = [node if n['id'] == node['id'] else n for n in state['nodes']]
        else:
            state['nodes'].append(node)
        self.line('正在检查并应用…')
        self.store.apply(state)
        self.line('✓ 已保存并启用。请确认防火墙和 NAT 映射已放行。')

    def reveal(self, n):
        self.title(n['name'])
        self.line('连接链接')
        print(share(n))
        if n['kind'] == 'ss':
            self.line('落地 Token · 复制到直连机导入（等同于连接密码，请私下保管）')
            print(encode_token(endpoint_from_node(n)))
        elif n['kind'] == 'vless':
            self.line('出口：' + (n['egress']['name'] if n['egress'] else '本机'))
        self.line('本机监听：' + authority(n['listen'], n['port']))

    def create_ss(self):
        self.title('创建 SS 落地')
        n = new_ss(self.base('ss', 'SS 落地', 8388))
        self.save_node(n); self.reveal(n)

    def create_vless(self, chained=False):
        self.title('导入落地 → 创建 VLESS' if chained else '创建 VLESS Reality')
        ep = decode_token(self.ask('落地 Token / SS 链接', secret=True)) if chained else None
        if ep:
            self.line(f"落地：{ep['name']} · {authority(ep['server'], ep['port'])}")
        base = self.base('vless', 'VLESS 入口', 443)
        sni = self.ask('Reality 握手域名', 'www.cloudflare.com', host)
        self.probe_reality(sni)
        n = new_vless(base, sni, ep, self.store.core)
        self.save_node(n); self.reveal(n)

    def probe_reality(self, sni):
        import ssl
        context = ssl.create_default_context()
        context.minimum_version = ssl.TLSVersion.TLSv1_3
        try:
            with socket.create_connection((sni, 443), timeout=8) as raw:
                with context.wrap_socket(raw, server_hostname=sni) as tls:
                    self.line('握手站点可达 · ' + str(tls.version()))
        except (OSError, ssl.SSLError):
            raise Error('握手站点的 TLS 1.3 检查失败，请换一个本机可达、证书有效的域名。')

    def edit(self):
        self.title('修改节点')
        n = self.choose_node()
        self.line('1 名称与连接地址   2 本机端口与监听地址   3 轮换认证')
        if n['kind'] == 'vless':
            self.line('4 Reality 握手域名')
        if n['kind'] == 'forward':
            self.line('4 转发目标')
        pick = self.ask('选择', '1')
        if pick == '1':
            n['name'] = self.ask('名称', n['name'], text)
            n['public_host'] = self.ask('公网 IP / 域名', n['public_host'], host)
            n['public_port'] = self.number('公网连接端口', n['public_port'])
        elif pick == '2':
            n['port'] = self.number('本机监听端口', n['port'])
            n['listen'] = self.ask('监听地址', n['listen'], ip_literal)
        elif pick == '3' and n['kind'] in ('ss', 'vless'):
            self.line('旧链接会失效；SS 的旧 Token 也会失效，需在直连机更新。')
            if self.ask('继续？y/N', 'n').lower() != 'y':
                return
            if n['kind'] == 'ss':
                n['password'] = secrets.token_urlsafe(32)
            else:
                n['uuid'] = str(uuid.uuid4())
                n['private_key'], n['public_key'] = reality_keys(self.store.core)
                n['short_id'] = secrets.token_hex(8)
        elif pick == '4' and n['kind'] == 'vless':
            n['sni'] = self.ask('握手域名', n['sni'], host); self.probe_reality(n['sni'])
        elif pick == '4' and n['kind'] == 'forward':
            n['target_host'] = self.ask('目标 IP / 域名', n['target_host'], host)
            n['target_port'] = self.number('目标端口', n['target_port'])
        else:
            raise Error('没有这个选项。')
        self.save_node(n, True); self.reveal(n)

    def delete(self):
        self.title('删除节点')
        n = self.choose_node()
        if self.ask(f"删除 {n['name']}？y/N", 'n').lower() != 'y':
            return
        state = copy.deepcopy(self.store.state())
        state['nodes'] = [x for x in state['nodes'] if x['id'] != n['id']]
        self.store.apply(state); self.line('✓ 已删除。')

    def switch_exit(self):
        self.title('切换 VLESS 落地')
        n = self.choose_node({'vless'})
        self.line('粘贴新的落地 Token / SS 链接；输入 direct 改成本机出口。')
        value = self.ask('Token', secret=True)
        if value == 'direct':
            if self.ask('确认改用本机出口？y/N', 'n').lower() != 'y':
                return
            n['egress'] = None
        else:
            n['egress'] = decode_token(value)
        self.save_node(n, True)
        self.line('VLESS 链接不变，出口已更新。')

    def forward(self):
        self.title('添加端口转发')
        n = self.base('forward', '端口转发', 10000)
        n['target_host'] = self.ask('目标 IP / 域名', validate=host)
        n['target_port'] = self.number('目标端口', 443)
        def network(value):
            if value not in ('tcp', 'udp', 'both'):
                raise Error('请输入 tcp、udp 或 both。')
            return value
        n['network'] = self.ask('协议 tcp / udp / both', 'both', network)
        self.save_node(n)
        self.line('转发只连接固定目标，不转换代理协议。')

    def diagnostics(self):
        self.title('服务与诊断')
        self.line('服务：' + ('运行中' if self.store.system.active() else '已停止'))
        self.line('1 检查配置   2 重启   3 停止   4 日志   5 落地 TCP 检查')
        pick = self.ask('选择', '1')
        if pick == '1':
            if not self.store.generation():
                raise Error('尚无配置。')
            self.store.check(self.store.current / 'config.json'); self.line('✓ 配置有效。')
        elif pick == '2':
            state = self.store.state()
            if not state['nodes']:
                raise Error('还没有节点。')
            self.store.check(self.store.current / 'config.json')
            if self.store.system.active(): self.store.system.stop()
            self.store.system.start(); self.store.healthy(state); self.line('✓ 已启动。')
        elif pick == '3':
            if self.store.system.active(): self.store.system.stop()
            self.store.system.enable(False); self.line('已停止，并关闭开机自启。')
        elif pick == '4':
            self.line(self.store.system.logs())
        elif pick == '5':
            n = self.choose_node({'vless'})
            if not n['egress']: raise Error('这个节点使用本机出口。')
            ep = n['egress']
            try:
                with socket.create_connection((ep['server'], ep['port']), timeout=5): pass
            except OSError:
                raise Error('落地 TCP 端口不可达。')
            self.line('TCP 端口可达；这不代表 SS 密码正确或端到端已经连通。')
        else:
            raise Error('没有这个选项。')

    def uninstall(self):
        self.title('卸载 LINKBOX')
        if self.ask('删除全部 LINKBOX 节点和密钥？输入 DELETE 确认') != 'DELETE':
            return
        if self.store.system.active(): self.store.system.stop()
        self.store.system.enable(False)
        self.store.system.unit.unlink(missing_ok=True); self.store.system.reload()
        LAUNCHER.unlink(missing_ok=True)
        shutil.rmtree(ROOT); shutil.rmtree(LIB)
        self.line('已卸载。系统依赖及历史服务日志保留。')
        raise SystemExit(0)

    def menu(self):
        while True:
            self.title('LINKBOX  ·  ' + VERSION)
            self.line('1  节点管理')
            self.line('2  进阶功能')
            self.line('0  退出')
            pick = self.ask('选择', '0')
            if pick == '0': return
            if pick == '1':
                self.title('节点管理')
                self.line('1  创建 SS 落地')
                self.line('2  创建 VLESS Reality（本机出口）')
                self.line('3  查看链接 / 导出 Token')
                self.line('4  修改节点')
                self.line('5  删除节点')
                self.line('0  返回')
                actions = {'1': self.create_ss, '2': self.create_vless,
                           '3': lambda: self.reveal(self.choose_node()), '4': self.edit, '5': self.delete}
            elif pick == '2':
                self.title('进阶功能')
                self.line('1  导入落地 → 创建 VLESS')
                self.line('2  切换已有 VLESS 的落地')
                self.line('3  添加端口转发')
                self.line('4  服务与诊断')
                self.line('5  卸载')
                self.line('0  返回')
                actions = {'1': lambda: self.create_vless(True), '2': self.switch_exit,
                           '3': self.forward, '4': self.diagnostics, '5': self.uninstall}
            else:
                continue
            sub = self.ask('选择', '0')
            if sub == '0': continue
            try:
                if sub not in actions: raise Error('没有这个选项。')
                actions[sub]()
            except Error as exc:
                self.line('未完成 · ' + str(exc))
            self.pause()


def download(url, destination):
    """Bounded HTTPS retries, including curl 56 on older distro curl versions."""
    destination = Path(destination)
    attempts = [
        ('自动连接', []),
        ('HTTP/1.1', ['--http1.1']),
        ('HTTP/1.1 · IPv4', ['--http1.1', '-4']),
        ('HTTP/1.1 · IPv6', ['--http1.1', '-6']),
    ]
    failures = []
    for number, (label, flags) in enumerate(attempts, 1):
        destination.unlink(missing_ok=True)
        if number > 1:
            print(f'  下载重试 {number}/{len(attempts)} · {label}', flush=True)
            time.sleep(1)
        args = ['curl', '-q', '-fsSL', '--proto', '=https', '--proto-redir', '=https',
                '--connect-timeout', '15', '--max-time', '180',
                *flags, url, '-o', str(destination)]
        code = None
        try:
            result = subprocess.run(args, stdin=subprocess.DEVNULL, capture_output=True,
                                    text=True, errors='replace', timeout=190)
            code = result.returncode
            if code == 0:
                return
            detail = result.stderr.strip() or '没有返回错误详情'
            # Keep public-download diagnostics readable, without terminal control codes.
            detail = ''.join(c for c in detail if c.isprintable() or c == '\n')[-800:]
            reason = f'curl {code}: {detail}'
        except subprocess.TimeoutExpired:
            reason = '下载超时（190 秒）'
        except OSError as exc:
            destination.unlink(missing_ok=True)
            raise Error(f'无法启动下载工具 curl（{type(exc).__name__}），请检查 curl 是否安装。') from exc
        destination.unlink(missing_ok=True)
        failures.append(f'{label} · {reason}')
        # Certificate/local write errors do not benefit from changing network families.
        if code in (23, 26, 60, 77):
            break
    raise Error('核心下载失败，安装未完成。\n  ' + '\n  '.join(failures) +
                '\n  下载地址：' + url +
                '\n  请检查这台机器到 GitHub 下载站的连接后重新运行安装命令。')


def fetch_core(directory, distro, machine=None):
    architecture = {'x86_64': 'amd64', 'amd64': 'amd64', 'aarch64': 'arm64', 'arm64': 'arm64', 'armv7l': 'armv7'}.get(machine or platform.machine())
    if not architecture:
        raise Error('支持的架构：x86_64、aarch64、armv7l。')
    variant = architecture + ('-musl' if distro == 'alpine' else '-glibc')
    name = f'sing-box-{CORE_VERSION}-linux-{variant}'
    directory = Path(directory)
    archive = directory / 'core.tar.gz'
    url = f'https://github.com/SagerNet/sing-box/releases/download/v{CORE_VERSION}/{name}.tar.gz'
    download(url, archive)
    digest = hashlib.sha256()
    with archive.open('rb') as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b''):
            digest.update(chunk)
    if digest.hexdigest() != CORE_HASHES[variant]:
        archive.unlink(missing_ok=True)
        raise Error('核心 SHA-256 校验失败，未安装。')
    binary = directory / 'sing-box'
    with tarfile.open(archive, 'r:gz') as tar:
        member = tar.getmember(name + '/sing-box')
        if not member.isfile():
            raise Error('核心归档格式不正确。')
        with tar.extractfile(member) as src, binary.open('wb') as dst:
            shutil.copyfileobj(src, dst, 1024 * 1024)
    binary.chmod(0o700)
    return binary


def install(staged_core, staged_manager, system):
    marker = LIB / '.linkbox-owned'
    paths = (ROOT, LIB, LAUNCHER, system.unit)
    if any(p.exists() for p in paths) and not marker.exists():
        raise Error('发现同名文件但没有 LINKBOX 安装标记，未覆盖。')
    output = run([staged_core, 'version']).stdout
    if not re.search(r'^sing-box version ' + re.escape(CORE_VERSION) + r'\s*$', output, re.M):
        raise Error('核心版本核验失败。')
    fresh = not marker.exists()
    if marker.exists():
        store = Store(system=system)
        store.recover()
        if store.generation(): run([staged_core, 'check', '-c', store.current / 'config.json'])
    ROOT.mkdir(parents=True, exist_ok=True, mode=0o700)
    LIB.mkdir(parents=True, exist_ok=True, mode=0o700)
    for p in (ROOT, LIB): os.chmod(p, 0o700)
    files = {CORE: (Path(staged_core).read_bytes(), 0o700), LIB / 'linkbox.py': (Path(staged_manager).read_bytes(), 0o700),
             LAUNCHER: (b'#!/bin/sh\nexec python3 /usr/local/lib/linkbox/linkbox.py "$@"\n', 0o755),
             system.unit: (system.definition().encode(), 0o644 if system.manager == 'systemd' else 0o755),
             marker: (b'LINKBOX 1\n', 0o600)}
    backup = {p: (p.read_bytes(), p.stat().st_mode & 0o777) if p.exists() else None for p in files}
    was_active = system.active()
    try:
        if was_active: system.stop()
        for p, (content, mode) in files.items(): atomic_bytes(p, content, mode)
        system.reload()
        if was_active:
            system.start(); Store(system=system).healthy(Store(system=system).state())
    except BaseException:
        for p, old in backup.items():
            if old is None: p.unlink(missing_ok=True)
            else: atomic_bytes(p, old[0], old[1])
        system.reload()
        if was_active: system.start()
        if fresh:
            for directory in (ROOT, LIB):
                try: directory.rmdir()
                except OSError: pass
        raise


def main():
    if os.geteuid() != 0: raise Error('请以 root 运行。')
    os.umask(0o077)
    system = System()
    def interrupt(_sig, _frame): raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, interrupt)
    with locked():
        if len(sys.argv) == 3 and sys.argv[1] == '--bootstrap':
            print('  正在下载并验证核心…', flush=True)
            binary = fetch_core(Path(sys.argv[2]), system.distro)
            install(binary, Path(__file__), system)
            print('  ✓ 安装完成，以后直接输入 lb。', flush=True)
            return
        if len(sys.argv) == 4 and sys.argv[1] == '--install':
            install(Path(sys.argv[2]), Path(sys.argv[3]), system)
            return
        if not (LIB / '.linkbox-owned').exists(): raise Error('请先运行一键安装命令。')
        store = Store(system=system)
        if store.recover(): print('  已恢复上次未完成的配置。')
        UI(store).menu()


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        print('\n  已退出。')
        sys.exit(130)
    except (Error, OSError, ValueError, KeyError) as exc:
        print('\n  错误 · ' + (str(exc) if isinstance(exc, Error) else '本地文件或系统操作异常，请检查安装与配置。'), file=sys.stderr)
        sys.exit(1)
