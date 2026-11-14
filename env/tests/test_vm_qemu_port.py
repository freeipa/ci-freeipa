"""The qemu backend's arch/accel/machine/cpu/firmware port: pure unit tests,
no qemu needed (monkeypatched shutil.which, os.access, sys.platform)."""
import os
import subprocess

import pytest

from freeipa_env import checker
from freeipa_env.envspec import EnvSpec
from freeipa_env.vmbackend import (QemuBackend, VMBackendError, _qemu_pid,
                                   _qemu_pid_ps)

PRESETS = os.path.join(os.path.dirname(__file__), '..', 'presets')


def backend(**cfg):
    return QemuBackend(cfg)


# ------------------------------------------------------------------- _arch()
def test_arch_cfg_wins_and_arm64_is_normalized():
    assert backend(arch='aarch64')._arch() == 'aarch64'
    assert backend(arch='arm64')._arch() == 'aarch64'
    assert backend(arch='x86_64')._arch() == 'x86_64'


def test_arch_defaults_to_the_host_machine(monkeypatch):
    monkeypatch.setattr('platform.machine', lambda: 'arm64')
    assert backend()._arch() == 'aarch64'
    monkeypatch.setattr('platform.machine', lambda: 'x86_64')
    assert backend()._arch() == 'x86_64'


# ------------------------------------------------------------------ _accel()
@pytest.mark.parametrize('accel', ['hvf', 'kvm', 'tcg'])
def test_accel_explicit_values_are_honored(accel):
    assert backend(accel=accel)._accel('aarch64') == accel


def test_accel_unknown_value_is_refused():
    with pytest.raises(VMBackendError, match='unknown accel'):
        backend(accel='whpx')._accel('x86_64')


def test_accel_auto_is_hvf_on_darwin(monkeypatch):
    monkeypatch.setattr('sys.platform', 'darwin')
    monkeypatch.setattr('os.access', lambda p, m: False)
    assert backend()._accel('aarch64') == 'hvf'


def test_accel_auto_is_kvm_with_a_writable_dev_kvm(monkeypatch):
    monkeypatch.setattr('sys.platform', 'linux')
    monkeypatch.setattr('os.access', lambda p, m: p == '/dev/kvm')
    assert backend()._accel('x86_64') == 'kvm'


def test_accel_auto_is_tcg_without_kvm(monkeypatch):
    monkeypatch.setattr('sys.platform', 'linux')
    monkeypatch.setattr('os.access', lambda p, m: False)
    assert backend()._accel('x86_64') == 'tcg'


# ------------------------------------------------------------------- _qemu()
def test_qemu_default_follows_the_arch():
    assert backend(arch='aarch64')._qemu() == 'qemu-system-aarch64'
    assert backend(arch='x86_64')._qemu() == 'qemu-system-x86_64'


def test_qemu_cfg_wins():
    assert backend(arch='aarch64', qemu='my-qemu')._qemu() == 'my-qemu'


# ------------------------------------------------------------------- _cpu()
def test_cpu_host_under_kvm_and_hvf_and_max_under_tcg():
    assert backend()._cpu('kvm') == 'host'
    assert backend()._cpu('hvf') == 'host'
    assert backend()._cpu('tcg') == 'max'


def test_cpu_cfg_wins():
    assert backend(cpu='neoverse-n1')._cpu('hvf') == 'neoverse-n1'


# ----------------------------------------------------------- _machine_args()
def test_machine_args_x86_64_keep_today_q35_shape():
    assert backend()._machine_args('x86_64', 'kvm', 'host') == \
        ['-machine', 'q35,accel=kvm', '-cpu', 'host']


def test_machine_args_aarch64_use_the_virt_machine():
    assert backend()._machine_args('aarch64', 'hvf', 'host') == \
        ['-machine', 'virt', '-accel', 'hvf', '-cpu', 'host']
    assert backend()._machine_args('aarch64', 'tcg', 'max') == \
        ['-machine', 'virt', '-accel', 'tcg', '-cpu', 'max']


# --------------------------------------------------------------- _firmware()
def test_firmware_is_none_on_x86_64():
    assert backend()._firmware('x86_64') is None


def test_firmware_is_found_next_to_the_qemu_binary(monkeypatch, tmp_path):
    exe = tmp_path / 'bin' / 'qemu-system-aarch64'
    exe.parent.mkdir(parents=True)
    exe.write_text('')
    fw = tmp_path / 'share' / 'qemu' / 'edk2-aarch64-code.fd'
    fw.parent.mkdir(parents=True)
    fw.write_text('')
    monkeypatch.setattr('shutil.which', lambda c: str(exe))
    assert backend(arch='aarch64')._firmware('aarch64') == str(fw)


def test_firmware_cfg_wins(monkeypatch, tmp_path):
    mine = tmp_path / 'my.fd'
    mine.write_text('')
    exe = tmp_path / 'bin' / 'qemu-system-aarch64'
    exe.parent.mkdir(parents=True)
    exe.write_text('')
    monkeypatch.setattr('shutil.which', lambda c: str(exe))
    assert backend(arch='aarch64', firmware=str(mine))._firmware('aarch64') \
        == str(mine)


def test_firmware_missing_everywhere_is_an_error(monkeypatch):
    monkeypatch.setattr('shutil.which', lambda c: None)
    with pytest.raises(VMBackendError, match='no aarch64 UEFI firmware'):
        backend(arch='aarch64')._firmware('aarch64')


# -------------------------------------------------------------- _check_host()
def test_check_host_demands_dev_kvm_only_under_kvm(monkeypatch, tmp_path):
    image = tmp_path / 'base.qcow2'
    image.write_text('')
    monkeypatch.setattr('os.access', lambda p, m: False)
    monkeypatch.setattr('shutil.which', lambda c: '/bin/' + c)
    monkeypatch.setattr('subprocess.run',
                        lambda *a, **k: subprocess.CompletedProcess(
                            [], 0, stdout='{"virtual-size": 1}'))
    cfgs = [{'image': str(image), 'disk': '40G'}]
    with pytest.raises(VMBackendError, match='/dev/kvm'):
        backend(accel='kvm')._check_host(cfgs)
    backend(accel='hvf')._check_host(cfgs)      # no kvm requirement
    # the accel-conditional check is the _needs_kvm hook, not an inline test
    assert not QemuBackend({})._needs_kvm() is None


def test_needs_kvm_follows_the_accel():
    assert QemuBackend({'accel': 'kvm'})._needs_kvm() is True
    assert QemuBackend({'accel': 'hvf'})._needs_kvm() is False
    assert QemuBackend({'accel': 'tcg'})._needs_kvm() is False


def test_qemu_system_args_compose_machine_cpu_and_firmware(monkeypatch, tmp_path):
    exe = tmp_path / 'bin' / 'qemu-system-aarch64'
    exe.parent.mkdir(parents=True)
    exe.write_text('')
    fw = tmp_path / 'share' / 'qemu' / 'edk2-aarch64-code.fd'
    fw.parent.mkdir(parents=True)
    fw.write_text('')
    monkeypatch.setattr('shutil.which', lambda c: str(exe))
    args = backend(arch='aarch64', accel='hvf')._qemu_system_args()
    assert args[:6] == ['-machine', 'virt', '-accel', 'hvf', '-cpu', 'host']
    assert args[6:] == ['-drive', f'if=pflash,format=raw,readonly=on,file={fw}']
    # x86_64: today's argv, no firmware drive
    assert backend(arch='x86_64', accel='kvm')._qemu_system_args() == \
        ['-machine', 'q35,accel=kvm', '-cpu', 'host']


def test_krun_rejects_the_qemu_only_keys_and_always_needs_kvm():
    from freeipa_env.vmbackend import KrunBackend
    assert KrunBackend({})._needs_kvm() is True
    for key in ('arch', 'accel', 'cpu', 'machine', 'firmware', 'qemu'):
        with pytest.raises(VMBackendError, match=f'vm.{key}.*not supported'):
            KrunBackend({})._check_krun([{key: 'x'}])


# ------------------------------------------------------------------ _fetch()
def test_fetch_downloads_an_url_image_once(monkeypatch, tmp_path):
    calls = []

    def fake_urlretrieve(url, dest):
        calls.append(url)
        with open(dest, 'w') as f:
            f.write('qcow2')

    monkeypatch.setattr('urllib.request.urlretrieve', fake_urlretrieve)
    monkeypatch.setattr('os.path.expanduser', lambda p: str(tmp_path))
    dest = backend()._fetch('https://example.com/Fedora-Cloud.aarch64.qcow2')
    assert os.path.isfile(dest)
    again = backend()._fetch('https://example.com/Fedora-Cloud.aarch64.qcow2')
    assert again == dest
    assert calls == ['https://example.com/Fedora-Cloud.aarch64.qcow2']


def test_fetch_failure_is_a_backend_error(monkeypatch, tmp_path):
    def boom(url, dest):
        raise OSError('no route to host')

    monkeypatch.setattr('urllib.request.urlretrieve', boom)
    monkeypatch.setattr('os.path.expanduser', lambda p: str(tmp_path))
    with pytest.raises(VMBackendError, match='downloading .* failed'):
        backend()._fetch('https://example.com/x.qcow2')


def test_provision_resolves_an_url_image_to_a_local_file(monkeypatch, tmp_path):
    monkeypatch.setattr('urllib.request.urlretrieve',
                        lambda url, dest: open(dest, 'w').write('qcow2'))
    monkeypatch.setattr('os.path.expanduser', lambda p: str(tmp_path))
    b = backend(image='https://example.com/Fedora.aarch64.qcow2')
    cfgs = b._vm_cfgs(1)
    # provision's two loops: containerdisk.resolve (a no-op for a URL) ...
    from freeipa_env import containerdisk
    for c in cfgs:
        c['image'] = containerdisk.resolve(c['image'])
    # ... must not make the fetch loop skip the download
    for c in cfgs:
        image = c.get('image') or ''
        if image.startswith(('http://', 'https://')):
            fetched = b._fetch(image)
            for d in cfgs:
                if d.get('image') == image:
                    d['image'] = fetched
    assert os.path.isfile(cfgs[0]['image'])
    assert cfgs[0]['image'].endswith('.qcow2')


# ---------------------------------------------------------------- _qemu_pid()
_real_open = open


class _NoProc:
    """A file-opener stand-in: reads real files, refuses /proc paths."""

    def __call__(self, path, *args, **kwargs):
        if str(path).startswith('/proc/'):
            raise OSError('no /proc on macOS')
        return _real_open(path, *args, **kwargs)


def test_qemu_pid_falls_back_to_ps_without_proc(monkeypatch, tmp_path):
    pidfile = tmp_path / 'qemu.pid'
    pidfile.write_text('4242')
    monkeypatch.setattr('builtins.open', _NoProc())
    monkeypatch.setattr(
        'subprocess.run',
        lambda cmd, **k: subprocess.CompletedProcess(
            cmd, 0, stdout='qemu-system-aarch64 -name freeipa-ci-abc123\n'))
    # the /proc read failed: the ps fallback sees the vm id -> the pid
    assert _qemu_pid(str(pidfile), 'freeipa-ci-abc123') == 4242


def test_qemu_pid_ps_fallback_without_the_vm_id_is_none():
    assert _qemu_pid_ps(4242, 'freeipa-ci-other') is None


# ------------------------------------------------------------ preset loading
@pytest.mark.parametrize('preset', ['qemu-smoke.yaml',
                                    'nested-qemu-external.yaml'])
def test_the_multiarch_presets_pass_the_checker(preset):
    spec = EnvSpec.from_file(os.path.join(PRESETS, preset))
    assert checker.check_spec(spec) == []
