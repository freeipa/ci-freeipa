import hashlib
import os
import shutil
import socket
import subprocess
import time
import uuid

import pytest

from freeipa_env import container as container_mode
from freeipa_env import containerdisk as cd

ROOT = os.path.join(os.path.dirname(__file__), '..', '..')
PUBLISH = os.path.join(ROOT, 'images', 'vm', 'publish.sh')
needs_podman = pytest.mark.skipif(
    not (shutil.which('podman') and shutil.which('qemu-img')
         and not (container_mode.in_container()
                  and subprocess.run(
                      ['podman', 'info'], capture_output=True
                  ).returncode != 0)),
    reason='a working podman and qemu-img needed (a control node that is '
           'a container has no podman; see freeipa_env.container)')
# the multi-arch tests pull through a real registry: podman resolves a
# manifest list only over the docker transport (a containers-storage: pull
# of a list does not pick a member, and podman 6 dropped the other
# transports)
needs_registry = pytest.mark.skipif(
    not (shutil.which('podman') and shutil.which('qemu-img')),
    reason='podman and qemu-img needed')


def sha(path):
    with open(path, 'rb') as f:
        return hashlib.sha256(f.read()).hexdigest()


@pytest.fixture
def cache(tmp_path, monkeypatch):
    monkeypatch.setattr(cd, 'CACHE', str(tmp_path / 'cache'))
    tags = []
    yield tags
    for t in tags:
        subprocess.run(['podman', 'rmi', '-f', t], capture_output=True)


def local_ref(tags):
    ref = f'localhost/containerdisk-test-{uuid.uuid4().hex[:8]}:1'
    tags.append(ref)
    return ref


def test_a_plain_path_is_left_alone():
    assert cd.resolve('/x/disk.qcow2') == '/x/disk.qcow2'


@needs_podman
def test_a_published_disk_comes_back_byte_for_byte_and_is_cached(
        tmp_path, cache):
    disk = tmp_path / 'disk.qcow2'
    subprocess.run(['qemu-img', 'create', '-q', '-f', 'qcow2', str(disk),
                    '8M'], check=True)
    ref = local_ref(cache)
    subprocess.run([PUBLISH, str(disk), ref], check=True,
                   capture_output=True)
    first = cd.resolve(f'containerdisk:containers-storage:{ref}')
    assert sha(first) == sha(disk)
    stamp = os.stat(first).st_mtime_ns
    again = cd.resolve(f'containerdisk:containers-storage:{ref}')
    assert again == first and os.stat(again).st_mtime_ns == stamp


@needs_podman
def test_an_image_whose_disk_is_not_a_qcow2_is_rejected(tmp_path, cache):
    (tmp_path / 'disk.qcow2').write_text('not a disk')
    (tmp_path / 'Containerfile').write_text(
        'FROM scratch\nCOPY disk.qcow2 /disk/disk.qcow2\n')
    ref = local_ref(cache)
    subprocess.run(['podman', 'build', '-q', '-t', ref, str(tmp_path)],
                   check=True, capture_output=True)
    with pytest.raises(cd.ContainerDiskError, match='not a qcow2'):
        cd.resolve(f'containerdisk:containers-storage:{ref}')
    assert not os.listdir(cd.CACHE)       # no partial file is left


@needs_podman
def test_an_image_without_the_disk_says_so(tmp_path, cache):
    (tmp_path / 'other').write_text('x')
    (tmp_path / 'Containerfile').write_text(
        'FROM scratch\nCOPY other /other\n')
    ref = local_ref(cache)
    subprocess.run(['podman', 'build', '-q', '-t', ref, str(tmp_path)],
                   check=True, capture_output=True)
    with pytest.raises(cd.ContainerDiskError, match='has no /disk/disk.qcow2'):
        cd.resolve(f'containerdisk:containers-storage:{ref}')


@pytest.fixture
def registry():
    """A throwaway local docker registry on a free loopback port.

    Plain HTTP: the podman calls of the tests that use it carry
    --tls-verify=false themselves (the insecure_pull fixture adds it to the
    pulls that resolve() makes).
    """
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        port = s.getsockname()[1]
    cid = 'containerdisk-test-registry'
    subprocess.run(['podman', 'rm', '-f', cid], capture_output=True)
    subprocess.run(['podman', 'run', '-d', '--name', cid,
                    '-p', f'127.0.0.1:{port}:5000',
                    'docker.io/library/registry:2'], capture_output=True,
                   check=True)
    for _ in range(30):
        if subprocess.run(['curl', '-fsS', f'http://127.0.0.1:{port}/v2/'],
                          capture_output=True).returncode == 0:
            break
        time.sleep(1)
    else:
        subprocess.run(['podman', 'rm', '-f', cid], capture_output=True)
        pytest.fail('the test registry did not come up')
    yield f'127.0.0.1:{port}'
    subprocess.run(['podman', 'rm', '-f', '-t', '0', cid],
                   capture_output=True)


@pytest.fixture
def insecure_pull(monkeypatch):
    """Let resolve() pull from the plain-HTTP test registry."""
    real = cd._podman

    def podman(args):
        if args[:1] == ['pull']:
            args = ['pull', '--tls-verify=false'] + args[1:]
        return real(args)

    monkeypatch.setattr(cd, '_podman', podman)


def _containerdisk_ctx(tmp_path, disk, name):
    ctx = tmp_path / name
    ctx.mkdir()
    shutil.copy(disk, ctx / 'disk.qcow2')
    (ctx / 'Containerfile').write_text(
        'FROM scratch\nCOPY disk.qcow2 /disk/disk.qcow2\n')
    return str(ctx)


@needs_registry
def test_a_multi_arch_tag_yields_the_disk_of_the_asked_arch(
        tmp_path, cache, registry, insecure_pull):
    # a distinct content per arch, so the pulled disk proves the member
    x86 = tmp_path / 'x86.qcow2'
    arm = tmp_path / 'arm.qcow2'
    subprocess.run(['qemu-img', 'create', '-q', '-f', 'qcow2', str(x86),
                    '8M'], check=True)
    subprocess.run(['qemu-img', 'create', '-q', '-f', 'qcow2', str(arm),
                    '16M'], check=True)
    ref = f'{registry}/multiarch-{uuid.uuid4().hex[:8]}:1'
    for arch, d, plat in (('x86_64', x86, 'linux/amd64'),
                          ('aarch64', arm, 'linux/arm64')):
        subprocess.run(
            ['podman', 'build', '-q', '--platform', plat,
             '-t', f'{ref}-{arch}',
             _containerdisk_ctx(tmp_path, d, f'c-{arch}')],
            check=True, capture_output=True)
        subprocess.run(['podman', 'push', '-q', '--tls-verify=false',
                        f'{ref}-{arch}'], check=True, capture_output=True)
        cache.append(f'{ref}-{arch}')
    subprocess.run(['podman', 'manifest', 'create', ref], check=True,
                   capture_output=True)
    for arch in ('x86_64', 'aarch64'):
        subprocess.run(['podman', 'manifest', 'add', '--tls-verify=false',
                        ref, f'{ref}-{arch}'], check=True,
                       capture_output=True)
    subprocess.run(['podman', 'manifest', 'push', '--tls-verify=false',
                    ref], check=True, capture_output=True)
    cache.append(ref)

    got_x86 = cd.resolve(f'containerdisk:{ref}', 'x86_64')
    got_arm = cd.resolve(f'containerdisk:{ref}', 'aarch64')
    assert got_x86 != got_arm                 # two members, two cache files
    assert sha(got_x86) == sha(x86)
    assert sha(got_arm) == sha(arm)


@needs_registry
def test_a_tag_without_the_asked_member_is_refused_not_silently_wrong(
        tmp_path, cache, registry, insecure_pull):
    # amd64-only, asked for aarch64: the pull succeeds and returns the
    # amd64 image; resolve must fail instead of handing over a disk that
    # would not boot
    disk = tmp_path / 'x86.qcow2'
    subprocess.run(['qemu-img', 'create', '-q', '-f', 'qcow2', str(disk),
                    '8M'], check=True)
    ref = f'{registry}/amd64only-{uuid.uuid4().hex[:8]}:1'
    subprocess.run(['podman', 'build', '-q', '--platform', 'linux/amd64',
                    '-t', ref, _containerdisk_ctx(tmp_path, disk, 'c')],
                   check=True, capture_output=True)
    subprocess.run(['podman', 'push', '-q', '--tls-verify=false', ref],
                   check=True, capture_output=True)
    cache.append(ref)

    with pytest.raises(cd.ContainerDiskError, match=r'not arm64'):
        cd.resolve(f'containerdisk:{ref}', 'aarch64')
