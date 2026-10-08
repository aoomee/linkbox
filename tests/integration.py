"""Real local VLESS Reality -> SS -> TCP/UDP tests; needs sing-box + openssl.
Run: python3 tests/integration.py /absolute/path/to/sing-box
"""
import importlib.util
import json
import os
from pathlib import Path
import socket
import ssl
import struct
import subprocess
import sys
import tempfile
import threading
import time

spec = importlib.util.spec_from_file_location('linkbox',Path(__file__).resolve().parents[1]/'linkbox.py')
lb=importlib.util.module_from_spec(spec); spec.loader.exec_module(lb)
core=Path(sys.argv[1]).resolve()


def free_port():
    with socket.socket() as s:
        s.bind(('127.0.0.1',0)); return s.getsockname()[1]


def exact(sock,n):
    out=b''
    while len(out)<n:
        chunk=sock.recv(n-len(out))
        if not chunk: raise OSError('connection closed')
        out+=chunk
    return out


def socks_request(proxy,target,command=1):
    s=socket.create_connection(('127.0.0.1',proxy),timeout=4)
    s.sendall(b'\x05\x01\x00'); assert exact(s,2)==b'\x05\x00'
    s.sendall(bytes([5,command,0,1])+socket.inet_aton('127.0.0.1')+struct.pack('!H',target))
    head=exact(s,4)
    if head[1]!=0: s.close(); raise OSError('SOCKS connection rejected')
    if head[3]==1: addr=socket.inet_ntoa(exact(s,4))
    elif head[3]==4: addr=socket.inet_ntop(socket.AF_INET6,exact(s,16))
    else: addr=exact(s,exact(s,1)[0]).decode()
    number=struct.unpack('!H',exact(s,2))[0]
    return s,(addr,number)


with tempfile.TemporaryDirectory(prefix='linkbox-e2e-') as directory:
    root=Path(directory); processes=[]; files=[]
    subprocess.run(['openssl','req','-x509','-newkey','rsa:2048','-nodes','-keyout',str(root/'key'),
                    '-out',str(root/'cert'),'-subj','/CN=localhost','-days','1'],check=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    context=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER); context.minimum_version=ssl.TLSVersion.TLSv1_3
    context.load_cert_chain(root/'cert',root/'key')
    tls_socket=socket.socket();tls_socket.bind(('127.0.0.1',0));tls_socket.listen()
    tls_port=tls_socket.getsockname()[1]
    def handle_tls(raw):
        try:
            with context.wrap_socket(raw,server_side=True) as s:
                s.settimeout(3);s.recv(4096);s.sendall(b'HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nOK')
        except (OSError,ssl.SSLError): raw.close()
    def tls_server():
        while True:
            try: s,_=tls_socket.accept()
            except OSError: return
            threading.Thread(target=handle_tls,args=(s,),daemon=True).start()
    threading.Thread(target=tls_server,daemon=True).start()
    http_socket=socket.socket();http_socket.bind(('127.0.0.1',0));http_socket.listen();http_port=http_socket.getsockname()[1]
    def http_server():
        while True:
            try: s,_=http_socket.accept()
            except OSError:return
            with s:
                s.settimeout(3)
                try:
                    s.recv(4096);s.sendall(b'HTTP/1.1 200 OK\r\nContent-Length: 15\r\nConnection: close\r\n\r\nLINKBOX-SS-EXIT')
                except OSError: pass
    threading.Thread(target=http_server,daemon=True).start()
    udp_socket=socket.socket(socket.AF_INET,socket.SOCK_DGRAM);udp_socket.bind(('127.0.0.1',0));udp_port=udp_socket.getsockname()[1]
    def udp_server():
        while True:
            try: data,addr=udp_socket.recvfrom(4096);udp_socket.sendto(data,addr)
            except OSError:return
    threading.Thread(target=udp_server,daemon=True).start()
    def start(name,config):
        path=root/(name+'.json');path.write_text(json.dumps(config))
        subprocess.run([str(core),'check','-c',str(path)],check=True,stdout=subprocess.DEVNULL)
        f=(root/(name+'.log')).open('w');files.append(f)
        proc=subprocess.Popen([str(core),'run','-c',str(path)],stdout=f,stderr=f);processes.append(proc)
        time.sleep(.3)
        if proc.poll() is not None:raise RuntimeError((root/(name+'.log')).read_text())
        return proc
    try:
        landing=lb.new_ss(lb.new_base('ss','test-exit','127.0.0.1',free_port(),listen='127.0.0.1'))
        endpoint=lb.decode_token(lb.encode_token(lb.endpoint_from_node(landing)))
        entry=lb.new_vless(lb.new_base('vless','test-entry','127.0.0.1',free_port(),listen='127.0.0.1'),'localhost',endpoint,core)
        landing_config=lb.make_config({'version':1,'nodes':[landing]})
        entry_config=lb.make_config({'version':1,'nodes':[entry]})
        entry_config['inbounds'][0]['tls']['reality']['handshake']={'server':'127.0.0.1','server_port':tls_port}
        proxy_port=free_port()
        client_config={'log':{'level':'warn'},'inbounds':[{'type':'mixed','tag':'socks','listen':'127.0.0.1','listen_port':proxy_port}],
                       'outbounds':[{'type':'vless','tag':'entry','server':'127.0.0.1','server_port':entry['port'],'uuid':entry['uuid'],
                                     'flow':'xtls-rprx-vision','packet_encoding':'xudp',
                                     'tls':{'enabled':True,'server_name':'localhost','utls':{'enabled':True,'fingerprint':'chrome'},
                                            'reality':{'enabled':True,'public_key':entry['public_key'],'short_id':entry['short_id']}}}],
                       'route':{'final':'entry'}}
        exit_proc=start('landing',landing_config);start('entry',entry_config);start('client',client_config)
        s,_=socks_request(proxy_port,http_port)
        with s:
            s.sendall(b'GET / HTTP/1.1\r\nHost: test\r\nConnection: close\r\n\r\n')
            data=b''
            while True:
                chunk=s.recv(4096)
                if not chunk:break
                data+=chunk
            assert b'LINKBOX-SS-EXIT' in data,data
        print('PASS: client VLESS Reality -> entry SS outbound -> landing -> HTTP')
        control,relay=socks_request(proxy_port,0,3)
        with control,socket.socket(socket.AF_INET,socket.SOCK_DGRAM) as u:
            u.settimeout(5)
            u.sendto(b'\0\0\0\x01'+socket.inet_aton('127.0.0.1')+struct.pack('!H',udp_port)+b'UDP-CHAIN-OK',relay)
            received,_=u.recvfrom(4096);assert received.endswith(b'UDP-CHAIN-OK'),received
        print('PASS: UDP over VLESS/XUDP -> SS UDP -> echo')
        exit_proc.terminate();exit_proc.wait(timeout=5)
        try:
            s,_=socks_request(proxy_port,http_port)
            with s:
                s.sendall(b'GET / HTTP/1.1\r\nHost: test\r\n\r\n');data=s.recv(4096)
            assert b'LINKBOX-SS-EXIT' not in data,'unexpected direct fallback'
        except OSError:pass
        print('PASS: SS exit stopped -> request fails, no direct fallback')
    except BaseException:
        for p in root.glob('*.log'):
            print(p.name,p.read_text()[-5000:],file=sys.stderr)
        raise
    finally:
        for p in processes:
            if p.poll() is None:p.terminate()
        for p in processes:
            try:p.wait(timeout=5)
            except subprocess.TimeoutExpired:p.kill();p.wait()
        for f in files:f.close()
        tls_socket.close();http_socket.close();udp_socket.close()
