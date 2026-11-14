import os
import shutil
import socket
import subprocess
import tempfile
import threading
import uuid

import pytest

from freeipa_env import krunproc
from freeipa_env.vmbackend import KrunBackend, VMBackendError


def _client(dir_, name, hub):
    s = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    s.bind(os.path.join(dir_, name))
    s.connect(hub)
    return s


def _received(s, want):
    s.settimeout(1)
    got = []
    try:
        while want not in got:
            got.append(s.recv(100))
    except socket.timeout:
        pass
    return got


def test_the_hub_floods_each_frame_to_the_other_peers_and_exits_when_the_vms_are_gone(tmp_path):
    # darwin's sun_path is 104 bytes and pytest's numbered per-test dirs can
    # exceed it: the sockets live in a short throwaway dir instead
    short = os.path.join(tempfile.gettempdir(),
                         f'ci-krun-hub-{uuid.uuid4().hex[:8]}')
    os.mkdir(short)
    vm = subprocess.Popen(['sleep', '30'])
    (tmp_path / 'vm.pid').write_text(str(vm.pid))
    hub = os.path.join(short, 'hub.sock')
    t = threading.Thread(target=krunproc.hub, args=(hub, [str(tmp_path / 'vm.pid')]),
                         daemon=True)
    t.start()
    try:
        for _ in range(100):
            if os.path.exists(hub):
                break
            threading.Event().wait(0.05)
        a, b, c = (_client(short, n, hub) for n in 'abc')
        b.send(b'hello')
        c.send(b'hello')
        a.send(b'frame-from-A')
        assert b'frame-from-A' in _received(b, b'frame-from-A')
        assert b'frame-from-A' in _received(c, b'frame-from-A')
        a.settimeout(0.3)
        with pytest.raises(socket.timeout):
            a.recv(100)
    finally:
        vm.kill()
        vm.wait()
    t.join(10)
    assert not t.is_alive()
    shutil.rmtree(short, ignore_errors=True)


@pytest.mark.skipif(not (shutil.which('mkfs.vfat') and shutil.which('mcopy')),
                    reason='needs dosfstools and mtools')
def test_the_seed_is_a_fat_image_with_the_files_cloud_init_reads(tmp_path):
    net = {'name': 'vm1', 'ip': '192.168.100.10', 'mac_ssh': '52:54:00:aa:00:01',
           'mac_priv': '52:54:00:bb:00:01',
           'hosts': [{'name': 'vm1', 'ip': '192.168.100.10'}]}
    KrunBackend({'backend': 'krun'})._seed(
        str(tmp_path), 'freeipa-ci-x', ['ssh-ed25519 AAAA test'], net)
    img = str(tmp_path / 'seed.img')

    def mtool(*args):
        return subprocess.run(args, check=True, capture_output=True, text=True).stdout

    user = mtool('mcopy', '-i', img, '::user-data', '-')
    for want in ('ssh-ed25519 AAAA test', '/etc/sysctl.d/50-pid-max.conf',
                 'systemd-sysctl.service'):
        assert want in user
    netcfg = mtool('mcopy', '-i', img, '::network-config', '-')
    assert '52:54:00:aa:00:01' in netcfg and '52:54:00:bb:00:01' in netcfg
    assert 'CIDATA' in mtool('mdir', '-i', img)


def test_unsupported_options_and_limits_are_refused_before_a_vm_starts():
    b = KrunBackend({'backend': 'krun'})
    with pytest.raises(VMBackendError, match='gpu'):
        b._check_krun([{'gpu': 'virgl'}])
    with pytest.raises(VMBackendError, match='usb'):
        b._check_krun([{'usb': ['1d6b:0002']}])
    with pytest.raises(VMBackendError, match='tpm'):
        b._check_krun([{'tpm': True}])
    with pytest.raises(VMBackendError, match=r'1\.\.16'):
        b._check_krun([{'cpus': 17}])
    long = KrunBackend({'backend': 'krun', 'state_dir': '/tmp/' + 'x' * 90})
    with pytest.raises(VMBackendError, match='too long'):
        long._check_krun([{}])
    b._check_krun([{'cpus': 16}])
