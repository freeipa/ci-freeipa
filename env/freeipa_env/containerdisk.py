"""`vm.image: containerdisk:REF`: a VM disk delivered by a container registry.

A containerdisk is a minimal image that holds one file, /disk/disk.qcow2:

    FROM scratch
    COPY disk.qcow2 /disk/disk.qcow2

(`images/vm/publish.sh` builds and pushes one.) Using it needs only podman,
no root and no conversion: the image is pulled, the disk copied out and kept
in the image cache, named after the image id so a moved tag is picked up and
an unchanged one is not copied again. REF is anything podman can pull,
including `containers-storage:...` and `oci-archive:...`.

On a control node that is itself a container (see container.py) podman is
never run: skopeo pulls the image into a temporary OCI directory and the
disk is taken from its one layer. The cache file name is the same either
way (the image id is the config digest podman reports too), so a cache
filled by one path is reused by the other.
"""
import gzip
import json
import os
import platform
import re
import shutil
import subprocess
import tempfile

from . import container as container_mode

PREFIX = 'containerdisk:'
CACHE = os.path.expanduser('~/.cache/freeipa-ci/images/containerdisk')
DISK_IN_IMAGE = '/disk/disk.qcow2'


class ContainerDiskError(Exception):
    pass


def _podman(args):
    try:
        p = subprocess.run(['podman'] + args, capture_output=True, text=True,
                           timeout=3600)
    except FileNotFoundError:
        raise ContainerDiskError('podman not found (needed for a '
                                 'containerdisk: image)')
    if p.returncode:
        raise ContainerDiskError(f'podman {" ".join(args)} failed: '
                                 f'{(p.stderr or p.stdout).strip()[-800:]}')
    return p.stdout.strip()


def _skopeo_pull(ref, arch):
    """(image id, layer path, temp dir) of `ref`, pulled with skopeo.

    Used instead of podman on a control node that is itself a container
    (see container.py): skopeo never touches a container store. The caller
    must remove the temp directory. `arch` selects the member of a
    multi-arch manifest list (the Go name: arm64, not aarch64) and pins the
    OS to linux, or the host's (darwin) would select nothing.
    """
    if not shutil.which('skopeo'):
        raise ContainerDiskError('skopeo not found (needed for a '
                                 'containerdisk: image inside a container)')
    tmp = tempfile.mkdtemp(prefix='ci-freeipa-containerdisk-',
                           dir=container_mode.scratch_dir())
    try:
        env = dict(os.environ)
        # /run/containers/<uid>/auth.json of the outer user is often not
        # readable from inside the container; an empty one is fine for the
        # public registries a containerdisk lives in
        auth = os.path.join(container_mode.scratch_dir(), 'auth.json')
        if 'REGISTRY_AUTH_FILE' not in env and not os.path.isfile(auth):
            with open(auth, 'w') as f:
                f.write('{}\n')
        env.setdefault('REGISTRY_AUTH_FILE', auth)
        p = subprocess.run(
            ['skopeo', 'copy', '--override-arch', _goarch(arch),
             '--override-os', 'linux', f'docker://{ref}', f'dir:{tmp}'],
            capture_output=True, text=True, timeout=3600, check=False,
            env=env)
        if p.returncode:
            raise ContainerDiskError(f'skopeo copy {ref} failed: '
                                      f'{(p.stderr or p.stdout).strip()[-800:]}')
        with open(os.path.join(tmp, 'manifest.json')) as f:
            manifest = json.load(f)
        config = manifest['config']['digest']
        if not config.startswith('sha256:'):
            raise ContainerDiskError(f'{ref}: unexpected config digest '
                                     f'{config}')
        layer = os.path.join(
            tmp, manifest['layers'][0]['digest'].split(':', 1)[1])
        return config[len('sha256:'):], layer, tmp
    except Exception:
        shutil.rmtree(tmp, ignore_errors=True)
        raise


def _skopeo_disk(layer, dest):
    """Copy /disk/disk.qcow2 out of the (gzipped tar) layer into `dest`."""
    try:
        with gzip.open(layer, 'rb') as f:
            p = subprocess.run(['tar', '-xzO', 'disk/disk.qcow2'], stdin=f,
                               stdout=subprocess.PIPE, check=False)
    except (OSError, EOFError) as e:
        raise ContainerDiskError(f'cannot read the image layer: {e}')
    if p.returncode or not p.stdout:
        raise ContainerDiskError(f'the image has no {DISK_IN_IMAGE}')
    with open(dest, 'wb') as f:
        f.write(p.stdout)


def _qemu_img():
    """The qemu-img to run: the fetched wrapper when there is one."""
    wrap = os.path.join(container_mode.tool_bin_dir(), 'qemu-img')
    if os.path.isfile(wrap):
        return wrap
    if shutil.which('qemu-img'):
        return 'qemu-img'
    return None


def _goarch(arch):
    """The Go architecture name registries use (x86_64/amd64, aarch64/
    arm64)."""
    return {'x86_64': 'amd64', 'aarch64': 'arm64'}.get(arch, arch)


def _assert_arch(ref, want):
    """Fail when the pulled image's architecture is not `want`.

    A missing member of a manifest list makes the platform-asked pull fail
    on its own, but a *single-arch* image is returned unchanged (no
    error), so a linux/amd64-only tag asked for arm64 would otherwise hand
    the qemu backend an unbootable disk.
    """
    arch = _podman(['image', 'inspect', '--format', '{{.Architecture}}',
                    ref])
    if arch != _goarch(want):
        raise ContainerDiskError(
            f'{ref}: the image is {arch}, not {_goarch(want)} (no '
            f'{arch} member in the tag)')


def _resolve_podman(ref, arch):
    # a containers-storage: ref names an image already in the local store:
    # there is nothing to pull (podman 6 cannot, podman 4 need not), and
    # the store is addressed without the transport prefix
    local = ref.startswith('containers-storage:')
    store_ref = ref[len('containers-storage:'):] if local else ref
    if not local:
        _podman(['pull', '-q', '--platform', f'linux/{arch}', ref])
    _assert_arch(store_ref, arch)
    image_id = _podman(['image', 'inspect', '--format',
                        '{{.Id}}', store_ref])[:12]
    dest = os.path.join(
        CACHE, f'{re.sub(r"[^A-Za-z0-9._-]+", "_", ref)}-{image_id}.qcow2')
    if os.path.isfile(dest):
        return dest
    os.makedirs(CACHE, exist_ok=True)
    cid = _podman(['create', store_ref, DISK_IN_IMAGE])  # scratch: no cmd runs
    try:
        _podman(['cp', f'{cid}:{DISK_IN_IMAGE}', dest + '.partial'])
    except ContainerDiskError as e:
        raise ContainerDiskError(f'{ref} has no {DISK_IN_IMAGE}: {e}')
    finally:
        subprocess.run(['podman', 'rm', '-f', cid], capture_output=True)
    return dest


def _resolve_skopeo(ref, arch):
    image_id, layer, tmp = _skopeo_pull(ref, arch)
    try:
        dest = os.path.join(
            CACHE, f'{re.sub(r"[^A-Za-z0-9._-]+", "_", ref)}-'
                   f'{image_id[:12]}.qcow2')
        if os.path.isfile(dest):
            return dest
        os.makedirs(CACHE, exist_ok=True)
        _skopeo_disk(layer, dest + '.partial')
        return dest
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def resolve(image, arch=None):
    """The local qcow2 for `image`: a plain path is returned as it is.

    `arch` (a qemu arch such as x86_64 or aarch64) selects the member of a
    multi-arch manifest list; the default is the host platform. A tag that
    has no such member fails with a clear error instead of handing the
    backend a disk of the wrong architecture, which would not boot.
    """
    if not image.startswith(PREFIX):
        return image
    if arch is None:
        arch = platform.machine()
        if arch == 'arm64':
            arch = 'aarch64'
    if container_mode.in_container():
        container_mode.prepare_tools()       # qemu-img, on the wrapper PATH
    ref = image[len(PREFIX):]
    if ref.startswith(('containers-storage:', 'oci-archive:')):
        dest = _resolve_podman(ref, arch)   # local stores: only podman has them
    elif container_mode.in_container():
        dest = _resolve_skopeo(ref, arch)
    else:
        dest = _resolve_podman(ref, arch)
    if os.path.isfile(dest):
        return dest
    tool = _qemu_img()
    if not tool:
        raise ContainerDiskError('qemu-img not found')
    info = subprocess.run([tool, 'info', dest + '.partial'],
                          capture_output=True, text=True, check=False)
    if info.returncode or 'file format: qcow2' not in info.stdout:
        os.unlink(dest + '.partial')
        raise ContainerDiskError(f'{ref}: {DISK_IN_IMAGE} is not a qcow2 file')
    os.replace(dest + '.partial', dest)
    return dest
