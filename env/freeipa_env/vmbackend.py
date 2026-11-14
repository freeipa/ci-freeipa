"""VM acquisition backends for the nested provider (design doc §3.9).

A VMBackend is the "get a VM" half of a nested provider: it turns a config
dict into provisioned VMs and hands back VMHandle objects (ssh targets)
that the nested provider then drives.  The "run the env in the VM" half
lives in nested_provider.py and composes a VMBackend with an inner
provider (default: podman).

Backends:
  * ``ssh``      -- a pool of pre-allocated user@host entries; the
                    "no real API" reference case.
  * ``command``  -- shell out to provision/deprovision scripts; the generic
                    cloud-API integration point (wrap any cloud in 2 scripts).
  * ``openstack``-- a concrete real-API example (openstack CLI); the
                    template for any other cloud.
  * ``qemu``     -- local VMs under qemu on the control node (qcow2 overlay
                    + cloud-init seed; ssh over a forwarded port): x86_64 or
                    aarch64 guests, KVM on Linux, Apple hvf on macOS.
  * ``krun``     -- the same on libkrun microVMs (see KrunBackend).

VMHandle is the currency between a backend and the nested provider: an ssh
target (user@host[:port]) with an optional private key and free-form meta
(the backend's own VM id, used for terminate).
"""

import ctypes
import glob
import json
import os
import platform
import re
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import time
import urllib.request
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import yaml

from . import container as container_mode
from . import containerdisk


class VMBackendError(Exception):
    pass


# --------------------------------------------------------------------- handle
@dataclass
class VMHandle:
    """An ssh-reachable provisioned VM."""
    host: str
    user: str = 'root'
    port: int = 22
    id: Optional[str] = None        # backend-specific id (used by terminate)
    key: Optional[str] = None       # private key path on the control node
    meta: Dict[str, Any] = field(default_factory=dict)

    @property
    def target(self):
        return f'{self.user}@{self.host}'

    @classmethod
    def parse(cls, line: str) -> 'VMHandle':
        """Parse ``user@host``, ``user@host:port``, or a bare ``host``."""
        line = line.strip()
        user, sep, host = line.rpartition('@')
        if not sep:
            user, host = 'root', line
        port = 22
        if host.count(':') == 1 and not host.startswith('['):
            host, _, p = host.rpartition(':')
            if p.isdigit():
                port = int(p)
        return cls(host=host, user=user or 'root', port=port)

    def to_dict(self):
        return {
            'host': self.host, 'user': self.user, 'port': self.port,
            'id': self.id, 'key': self.key, 'meta': self.meta,
        }

    @classmethod
    def from_dict(cls, d):
        return cls(host=d['host'], user=d.get('user', 'root'),
                   port=int(d.get('port', 22)), id=d.get('id'),
                   key=d.get('key'), meta=d.get('meta') or {})


# ----------------------------------------------------------------- ssh helpers
SSH_OPTS = [
    '-o', 'StrictHostKeyChecking=accept-new',
    '-o', 'ConnectTimeout=10',
    '-o', 'BatchMode=yes',
    '-o', 'ServerAliveInterval=30',
    '-o', 'ServerAliveCountMax=4',
]


def _ssh_command(handle: VMHandle):
    """The ssh argv without the target; shared by ssh and rsync."""
    args = ['ssh'] + SSH_OPTS
    if handle.meta.get('ephemeral_hostkey'):
        # per-VM host keys on reused localhost ports: don't pin them
        args += ['-o', 'UserKnownHostsFile=/dev/null', '-o', 'LogLevel=ERROR']
    if handle.port and handle.port != 22:
        args += ['-p', str(handle.port)]
    if handle.key:
        args += ['-i', handle.key]
    return args


def _ssh_prefix(handle: VMHandle):
    return _ssh_command(handle) + [handle.target]


def ssh_run(handle: VMHandle, cmd: str, check: bool = False,
            timeout: Optional[int] = None, stream: bool = False,
            log_path: Optional[str] = None):
    """Run a shell command on the VM. Returns (rc, stdout+stderr).

    When stream=True (and log_path is given) output is teed to the log and
    echoed to stdout line by line; otherwise it is captured and returned."""
    args = _ssh_prefix(handle) + [cmd]
    if stream and log_path:
        with open(log_path, 'wb') as lf:
            lf.write(f'+ {shlex.join(args)}\n'.encode())
            lf.flush()
            proc = subprocess.Popen(args, stdout=subprocess.PIPE,
                                    stderr=subprocess.STDOUT)
            for line in proc.stdout:
                import sys
                sys.stdout.buffer.write(line)
                sys.stdout.flush()
                lf.write(line)
            rc = proc.wait()
            return rc, ''
    proc = subprocess.run(args, stdout=subprocess.PIPE,
                          stderr=subprocess.STDOUT, timeout=timeout)
    out = proc.stdout.decode(errors='replace')
    if log_path:
        with open(log_path, 'a') as lf:
            lf.write(out)
    if check and proc.returncode:
        raise VMBackendError(f'ssh {handle.target} failed '
                             f'({proc.returncode}): {cmd}\n{out[-2000:]}')
    return proc.returncode, out


def _rsync(handle: VMHandle, src: str, dst: str):
    ssh = shlex.join(_ssh_command(handle))
    subprocess.run(['rsync', '-az', '--partial', '-e', ssh, src, dst],
                   check=True)


def rsync_to(handle: VMHandle, local: str, remote: str):
    """rsync local -> VM:remote (remote path need not pre-exist)."""
    _rsync(handle, local, f'{handle.target}:{remote}')


def rsync_from(handle: VMHandle, remote: str, local: str):
    """rsync VM:remote -> local."""
    _rsync(handle, f'{handle.target}:{remote}', local)


# ------------------------------------------------------------------- backends
class VMBackend:
    name = 'base'

    def __init__(self, cfg: Optional[Dict] = None):
        self.cfg = cfg or {}

    def provision(self, count: int = 1) -> List[VMHandle]:
        raise NotImplementedError

    def terminate(self, handle: VMHandle) -> None:
        raise NotImplementedError

    def wait_ready(self, handle: VMHandle, timeout: int = 600,
                   interval: int = 5) -> None:
        """Poll until ssh reaches the VM (or timeout)."""
        deadline = time.time() + timeout
        last = ''
        while time.time() < deadline:
            rc, out = ssh_run(handle, 'true', check=False, timeout=30)
            if rc == 0:
                print(f'== vm {handle.target} is reachable')
                return
            last = out.strip().splitlines()[-1] if out.strip() else ''
            time.sleep(interval)
        raise VMBackendError(
            f'vm {handle.target} not ssh-reachable within {timeout}s'
            + (f' (last: {last})' if last else ''))

    def describe(self) -> str:
        return self.name


class SshBackend(VMBackend):
    """A pool of pre-allocated ``user@host[:port]`` entries.

    Config::

        vm:
          backend: ssh
          hosts: [root@10.0.0.5, root@10.0.0.6]
    """
    name = 'ssh'

    def __init__(self, cfg):
        super().__init__(cfg)
        hosts = self.cfg.get('hosts') or []
        self._pool = [VMHandle.parse(h) if isinstance(h, str)
                      else VMHandle.from_dict(h) for h in hosts]

    def provision(self, count=1):
        if not self._pool:
            raise VMBackendError('ssh backend: no hosts configured')
        if count > len(self._pool):
            raise VMBackendError(
                f'ssh backend: asked for {count} but the pool has '
                f'{len(self._pool)}')
        out, self._pool = self._pool[:count], self._pool[count:]
        return out

    def terminate(self, handle):
        # pre-allocated: leave it in place (it belongs to the pool)
        print(f'== vm {handle.target}: pre-allocated, leaving in place')


class CommandBackend(VMBackend):
    """Provision/deprovision by shelling out to user scripts.

    The generic cloud-API integration point: wrap any cloud in two scripts.

    Config::

        vm:
          backend: command
          provision_cmd: /opt/cloud/get-vms.sh     # $1 = count
          deprovision_cmd: /opt/cloud/drop-vms.sh  # args = handle ids/targets

    ``provision_cmd <count>`` must print one ``user@host[:port]`` per line
    (or a JSON list of {host, user, port, id, key}).  ``deprovision_cmd``
    receives one argument per VM being released (its id if present, else
    its user@host target)."""
    name = 'command'

    def _run_script(self, script: str, argv: List[str]) -> str:
        if not script:
            raise VMBackendError(f'command backend: no {script} configured')
        if isinstance(script, (list, tuple)):
            cmd = [str(x) for x in list(script) + argv]
        else:
            cmd = shlex.split(script) + [str(a) for a in argv]
        try:
            proc = subprocess.run(cmd, stdout=subprocess.PIPE,
                                  stderr=subprocess.STDOUT, timeout=600)
        except FileNotFoundError:
            raise VMBackendError(f'command backend: script not found: '
                                 f'{shlex.join(cmd)}')
        out = proc.stdout.decode(errors='replace')
        if proc.returncode:
            raise VMBackendError(
                f'command backend: {shlex.join(cmd)} failed '
                f'({proc.returncode}):\n{out[-2000:]}')
        return out

    def provision(self, count=1):
        out = self._run_script(self.cfg.get('provision_cmd'), [count])
        handles: List[VMHandle] = []
        # try JSON first (a list of handle dicts), fall back to lines
        s = out.strip()
        if s.startswith('['):
            for i, d in enumerate(json.loads(s)):
                h = VMHandle.from_dict(d)
                h.id = h.id or f'vm{i}'
                handles.append(h)
            return handles
        for line in out.splitlines():
            line = line.strip()
            if not line or line.startswith('#'):
                continue
            handles.append(VMHandle.parse(line))
        if not handles:
            raise VMBackendError(
                'command backend: provision_cmd printed no VM handles')
        return handles

    def terminate(self, handle):
        arg = handle.id or handle.target
        self._run_script(self.cfg.get('deprovision_cmd'), [arg])
        print(f'== vm {handle.target}: deprovisioned via command backend')


class OpenstackBackend(VMBackend):
    """A concrete real cloud-API example, built on the ``openstack`` CLI.

    This is the template for any other cloud: map provision to
    ``server create`` (+ floating IP), terminate to ``server delete``.

    Config::

        vm:
          backend: openstack
          cloud: prod          # `openstack --cloud prod` (or OS_* env)
          image: fedora-44
          flavor: m1.medium
          keypair: freeipa-ci # optional
          network: test-net   # optional fixed network
          floating_pool: public
    """
    name = 'openstack'

    def _os(self, args: List[str], parse_json: bool = True):
        cmd = ['openstack']
        cloud = self.cfg.get('cloud')
        if cloud:
            cmd += ['--cloud', cloud]
        cmd += args
        if parse_json and '--format' not in args:
            cmd += ['--format', 'json']
        try:
            proc = subprocess.run(cmd, stdout=subprocess.PIPE,
                                  stderr=subprocess.STDOUT, timeout=600)
        except FileNotFoundError:
            raise VMBackendError('openstack backend: `openstack` CLI not '
                                 'found on the control node')
        out = proc.stdout.decode(errors='replace')
        if proc.returncode:
            raise VMBackendError(
                f'openstack backend: {shlex.join(cmd)} failed '
                f'({proc.returncode}):\n{out[-2000:]}')
        return json.loads(out) if parse_json else out

    def provision(self, count=1):
        image = self.cfg.get('image')
        flavor = self.cfg.get('flavor')
        if not image or not flavor:
            raise VMBackendError('openstack backend: need `image` and '
                                 '`flavor`')
        handles = []
        for i in range(count):
            name = f'freeipa-ci-{uuid.uuid4().hex[:10]}'
            create = ['server', 'create', '--wait', '--image', image,
                      '--flavor', flavor, '-n', name]
            if self.cfg.get('keypair'):
                create += ['--keypair', self.cfg['keypair']]
            if self.cfg.get('network'):
                create += ['--network', self.cfg['network']]
            self._os(create, parse_json=False)
            self.wait_ready_ip(name)
            ip = self._ip_for(name)
            h = VMHandle(host=ip, user='root', id=name)
            h.meta['openstack'] = name
            handles.append(h)
            print(f'== openstack: provisioned {name} at {ip}')
        return handles

    def wait_ready_ip(self, name):
        # `server create --wait` already blocks until ACTIVE; poll the
        # address just in case the floating/fixed IP attaches late.
        deadline = time.time() + int(self.cfg.get('timeout', 300))
        while time.time() < deadline:
            ip = self._ip_for(name)
            if ip:
                return ip
            time.sleep(5)
        raise VMBackendError(f'openstack backend: no address for {name}')

    def _ip_for(self, name) -> Optional[str]:
        pool = self.cfg.get('floating_pool', 'public')
        # prefer a floating IP, else the first fixed address
        try:
            fips = self._os(['floating', 'ip', 'list', '--server', name,
                             '--format', 'json'])
            if fips:
                return fips[0]['IP']
        except VMBackendError:
            pass
        srv = self._os(['server', 'show', name, '--format', 'json'])
        addrs = srv.get('addresses') or {}
        for addrs_list in addrs.values():
            for a in addrs_list:
                if a.get('addr'):
                    return a['addr']
        return None

    def terminate(self, handle):
        name = handle.id
        if not name:
            print(f'== openstack: no server id for {handle.target}; '
                  f'skipping delete')
            return
        # release floating ip if present
        pool = self.cfg.get('floating_pool', 'public')
        try:
            fips = self._os(['floating', 'ip', 'list', '--server', name,
                             '--format', 'json'])
            for f in fips:
                self._os(['floating', 'ip', 'delete', f['IP']],
                         parse_json=False)
        except VMBackendError:
            pass
        self._os(['server', 'delete', name], parse_json=False)
        print(f'== openstack: deleted {name}')


PRIVATE_PREFIX = '192.168.100'
OVMF_CODE = '/usr/share/edk2/ovmf/OVMF_CODE.fd'


def _free_port(kind=socket.SOCK_STREAM) -> int:
    with socket.socket(socket.AF_INET, kind) as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]


MCAST_GROUP = '230.0.0.1'


def _mcast_works() -> bool:
    """Can local processes exchange UDP multicast on this control node?

    Containers (shared host netns, multicast routed out a real interface,
    no CAP_NET_ADMIN) deliver a sent multicast packet to no local socket,
    not even back to the sender. Detect that once instead of failing later
    with ARP INCOMPLETE between the VMs.
    """
    # Like qemu's mcast netdev: bound to the group and joined on the interface
    # the kernel picks. A socket bound to 127.0.0.1 never receives a datagram
    # sent to the group, so that probe always failed and every env fell back
    # to the TCP chain, which only links two VMs.
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind((MCAST_GROUP, 0))
            port = s.getsockname()[1]
            s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_LOOP, 1)
            s.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP,
                         socket.inet_aton(MCAST_GROUP) +
                         socket.inet_aton('0.0.0.0'))
            s.settimeout(1.0)
            s.sendto(b'ci-freeipa-probe', (MCAST_GROUP, port))
            s.recvfrom(64)
            return True
    except OSError:
        return False


MCAST_OK = _mcast_works()


def _free_vnc_display() -> int:
    """First VNC display n (TCP 5900+n) not already bound on loopback."""
    for n in range(1, 200):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(('127.0.0.1', 5900 + n))
            except OSError:
                continue
            return n
    raise VMBackendError('no free VNC display in 1..199')


def _usb_id(s) -> tuple:
    """'VVVV:PPPP' -> lowercase ('vvvv', 'pppp')."""
    vid, _, pid = str(s).lower().partition(':')
    try:
        if len(vid) == len(pid) == 4 and int(vid + pid, 16) >= 0:
            return vid, pid
    except ValueError:
        pass
    raise VMBackendError(f'qemu backend: usb id {s!r} must be VVVV:PPPP (hex)')


def _usb_devices() -> list:
    """(vid, pid, busnum, devnum) for each USB device in sysfs."""
    out = []
    for d in glob.glob('/sys/bus/usb/devices/*'):
        def rd(f):
            with open(os.path.join(d, f)) as fh:
                return fh.read().strip()
        try:
            out.append((rd('idVendor'), rd('idProduct'),
                        int(rd('busnum')), int(rd('devnum'))))
        except (OSError, ValueError):
            pass  # interface entries have no idVendor
    return out


def _usb_assign(cfgs):
    """Give each VM's `usb: [VVVV:PPPP]` entries a distinct host device (a
    device goes to one VM only). Sets c['usb_devices'] = [(bus, dev)]."""
    free = _usb_devices()
    for c in cfgs:
        c['usb_devices'] = []
        for want in c.get('usb') or []:
            vid, pid = _usb_id(want)
            hit = next((d for d in free if d[0] == vid and d[1] == pid), None)
            if hit is None:
                raise VMBackendError(f'qemu backend: no free USB device {vid}:{pid} '
                                     f'on this host')
            free.remove(hit)
            node = f'/dev/bus/usb/{hit[2]:03d}/{hit[3]:03d}'
            if not os.access(node, os.R_OK | os.W_OK):
                raise VMBackendError(f'qemu backend: USB {vid}:{pid} at {node} is '
                                     f'not read/write for this user')
            c['usb_devices'].append((hit[2], hit[3]))


def _usb_args(c) -> list:
    """xHCI controller plus usb-host devices for the VM's assigned devices."""
    if not c.get('usb_devices'):
        return []
    args = ['-device', 'qemu-xhci,id=xhci']
    for bus, dev in c['usb_devices']:
        args += ['-device', f'usb-host,bus=xhci.0,hostbus={bus},hostaddr={dev}']
    return args


def _size_bytes(text) -> int:
    """A qemu size: bytes, or a number with K, M, G or T (powers of 1024)."""
    m = re.fullmatch(r'(\d+(?:\.\d+)?)\s*([KMGT]?)i?B?', str(text).strip(),
                     re.IGNORECASE)
    if not m:
        raise VMBackendError(f'qemu backend: bad disk size {text!r}')
    return int(float(m.group(1)) * 1024 ** ' KMGT'.index(m.group(2).upper() or ' '))


def _check_disk(image: str, disk) -> None:
    """An overlay smaller than its base image cuts the end of the image off
    (its partition table and filesystem): the VM then fails to boot in ways
    that look like a network problem. Refuse it."""
    p = subprocess.run(['qemu-img', 'info', '--output=json', image],
                       capture_output=True, text=True)
    if p.returncode:
        raise VMBackendError(f'qemu backend: qemu-img info {image} failed: '
                             f'{p.stderr.strip()}')
    virtual = json.loads(p.stdout)['virtual-size']
    if _size_bytes(disk) < virtual:
        raise VMBackendError(
            f'qemu backend: vm.disk {disk} is smaller than the image '
            f'{os.path.basename(image)} ({virtual / 2**30:g} GiB): the disk '
            f'would be cut off. Use at least {virtual / 2**30:g}G.')


def _mac() -> str:
    h = uuid.uuid4().hex[:6]
    return '52:54:00:' + ':'.join(h[i:i + 2] for i in (0, 2, 4))


def _qemu_pid(pidfile: str, vm_id: str) -> Optional[int]:
    """The live qemu pid for this VM, or None. qemu carries ``-name <vm_id>``
    on its cmdline, which guards against a recycled pid."""
    try:
        with open(pidfile) as f:
            pid = int(f.read().strip())
    except (OSError, ValueError):
        return None
    try:
        with open(f'/proc/{pid}/cmdline', 'rb') as f:
            cmd = f.read()
        if vm_id.encode() in cmd:
            return pid
    except OSError:
        pass          # no /proc (macOS): fall through to ps
    return _qemu_pid_ps(pid, vm_id)


def _qemu_pid_ps(pid: int, vm_id: str) -> Optional[int]:
    """macOS and other hosts without /proc: ask ps for the command line."""
    try:
        p = subprocess.run(['ps', '-p', str(pid), '-o', 'command='],
                           capture_output=True, text=True)
    except OSError:
        return None
    return pid if vm_id in p.stdout else None


def live_qemu_vms(state_dir: str = '~/.cache/freeipa-ci/qemu') -> dict:
    """{vm id: pid} of the running qemu VMs under `state_dir`."""
    root = os.path.expanduser(state_dir)
    found = {}
    for vm_id in sorted(os.listdir(root)) if os.path.isdir(root) else []:
        pid = _qemu_pid(os.path.join(root, vm_id, 'qemu.pid'), vm_id)
        if pid:
            found[vm_id] = pid
    return found


def stop_qemu(pid: int, grace: int = 30):
    os.kill(pid, signal.SIGTERM)
    deadline = time.time() + grace
    while time.time() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        time.sleep(0.5)
    os.kill(pid, signal.SIGKILL)


class QemuBackend(VMBackend):
    """Local VMs under qemu on the control node: no cloud API.

    Each VM is a qcow2 overlay on a cloud base image, booted with hardware
    acceleration (KVM on Linux, Apple hvf on macOS) and user-mode NAT
    networking. A cloud-init NoCloud seed authorizes the ssh public key for
    root; ssh reaches the guest on a host port forwarded to its port 22 on
    127.0.0.1. Per-VM state lives in ``<state_dir>/<id>/``.

    Config::

        vm:
          backend: qemu
          image: ~/images/Fedora-Cloud-Base-Generic.x86_64.qcow2  # required
                                        #   (a http(s):// URL is downloaded
                                        #   to ~/.cache/freeipa-ci/images/;
                                        #   a containerdisk:REF is pulled for
                                        #   the arch the VM boots, so one
                                        #   multi-arch tag serves both arches)
          arch: aarch64                # default: host arch (arm64 is
                                        #   normalized to aarch64);
                                        #   qemu-system-<arch> is then used
          accel: hvf                   # default: auto — hvf on macOS, kvm
                                        #   with a writable /dev/kvm, else tcg
          machine: virt                # default: q35 (x86_64) | virt (aarch64)
          cpu: host                    # default: host (kvm/hvf) | max (tcg)
          firmware: /path/QEMU_EFI.fd  # aarch64 only; default: auto-discovered
          dns: [8.8.8.8, 1.1.1.1]     # optional; guest nameservers, bypassing
                                        #   the slirp DNS forwarder 10.0.2.3
          cpus: 4                       # default 4
          memory: 8192                  # MiB, default 8192
          disk: 40G                     # overlay virtual size, default 40G
          ssh_key: ~/.ssh/id_ed25519    # default: every ~/.ssh/{id_ed25519,
                                        #   id_ecdsa,id_rsa}.pub is authorized
          state_dir: ~/.cache/freeipa-ci/qemu   # default
          qemu: qemu-system-aarch64     # default: qemu-system-<arch>
          gpu: virgl                    # optional; virtio-gpu with host OpenGL
          render_node: /dev/dri/renderD128   # default, for gpu: virgl
          usb: [VVVV:PPPP]              # optional, per VM; host USB devices by
                                        #   VVVV:PPPP, one entry per device
          tpm: true                     # optional, per VM; a TPM 2.0 (swtpm) for
                                        #   the VM, its state discarded at down
          vms:                          # optional per-VM overrides, one per VM;
                                        #   vm.count must equal len(vms)
            - {name: master1, cpus: 8, memory: 16384}
            - {name: client1, image: ~/images/other.qcow2, disk: 20G}

    USB passthrough and ``gpu: virgl`` need a Linux host: their host-side
    paths (/sys/bus/usb, /dev/dri/...) do not exist on macOS.

    ``terminate`` kills the qemu process and removes the overlay and seed;
    ``console.log`` is kept for triage.
    """
    name = 'qemu'
    _VM_FILES = ('disk.qcow2', 'seed.iso', 'user-data', 'meta-data', 'qemu.pid',
                 'swtpm.pid', 'swtpm.sock')

    def _state_dir(self):
        return os.path.expanduser(
            self.cfg.get('state_dir', '~/.cache/freeipa-ci/qemu'))

    def _tools(self):
        return [self._qemu(), 'qemu-img']

    def _needs_kvm(self):
        """Whether this backend requires /dev/kvm, whatever the accel says."""
        return self._accel(self._arch()) == 'kvm'

    def _qemu_system_args(self):
        """-machine/-accel/-cpu and the aarch64 UEFI firmware drive."""
        arch = self._arch()
        accel = self._accel(arch)
        fw = self._firmware(arch)
        args = self._machine_args(arch, accel, self._cpu(accel))
        if fw:
            args += ['-drive', f'if=pflash,format=raw,readonly=on,file={fw}']
        return args

    def _arch(self):
        """Normalized target arch: cfg `arch`, else the host's (arm64 →
        aarch64)."""
        arch = self.cfg.get('arch') or platform.machine()
        if arch == 'arm64':
            arch = 'aarch64'
        return arch

    def _accel(self, arch):
        """The accelerator: cfg `accel`, else auto (hvf on macOS, kvm with
        a writable /dev/kvm, tcg otherwise)."""
        accel = self.cfg.get('accel')
        if accel is None:
            if sys.platform == 'darwin':
                return 'hvf'
            if os.access('/dev/kvm', os.W_OK):
                return 'kvm'
            return 'tcg'
        if accel not in ('hvf', 'kvm', 'tcg'):
            raise VMBackendError(f'qemu backend: unknown accel {accel!r} '
                                 f'(known: hvf, kvm, tcg)')
        return accel

    def _qemu(self):
        return self.cfg.get('qemu', f'qemu-system-{self._arch()}')

    def _cpu(self, accel):
        """-cpu host is invalid under tcg on both arches."""
        return self.cfg.get('cpu') or ('host' if accel in ('kvm', 'hvf')
                                       else 'max')

    def _machine_args(self, arch, accel, cpu):
        if arch == 'aarch64':
            return ['-machine', 'virt', '-accel', accel, '-cpu', cpu]
        return ['-machine', f'q35,accel={accel}', '-cpu', cpu]

    def _firmware(self, arch):
        """UEFI firmware for aarch64 guests (None on x86_64: SeaBIOS)."""
        if arch != 'aarch64':
            return None
        candidates = [self.cfg.get('firmware')]
        q = shutil.which(self._qemu())
        if q:
            prefix = os.path.dirname(os.path.dirname(q))
            candidates.append(os.path.join(
                prefix, 'share', 'qemu', 'edk2-aarch64-code.fd'))
        candidates += ['/usr/share/edk2/aarch64/QEMU_EFI.fd',
                       '/usr/share/qemu-efi-aarch64/QEMU_EFI.fd',
                       '/usr/share/qemu/edk2-aarch64-code.fd']
        for c in candidates:
            if c and os.path.isfile(c):
                return c
        raise VMBackendError(
            'qemu backend: no aarch64 UEFI firmware found (set vm.firmware; '
            f'looked in: {[c for c in candidates if c]})')

    def _vm_cfgs(self, count):
        """Per-VM settings: top-level config, overridden by vms[i] if given."""
        base = {k: v for k, v in self.cfg.items() if k != 'vms'}
        over = self.cfg.get('vms')
        if over is None:
            return [dict(base) for _ in range(count)]
        if len(over) != count:
            raise VMBackendError(f'{self.name} backend: vm.vms lists {len(over)} VM(s) '
                                 f'but {count} requested; set vm.count to match')
        return [{**base, **(o or {})} for o in over]

    def _check_host(self, cfgs):
        if container_mode.in_container():
            container_mode.prepare_tools()   # qemu-img, ISO tool on PATH
        for c in cfgs:
            image = c.get('image')
            if not image:
                raise VMBackendError(f'{self.name} backend: `vm.image` (cloud base '
                                     'image) is required, per VM or top-level')
            if not os.path.isfile(os.path.expanduser(image)):
                raise VMBackendError(
                    f'{self.name} backend: base image not found: {image}')
            _check_disk(os.path.expanduser(image), c.get('disk', '40G'))
        if self._needs_kvm() and not os.access('/dev/kvm', os.W_OK):
            raise VMBackendError(f'{self.name} backend: /dev/kvm is not writable '
                                 '(needs KVM and access to /dev/kvm)')
        for tool in self._tools():
            if not shutil.which(tool):
                raise VMBackendError(f'{self.name} backend: {tool} not found')
        if any(c.get('tpm') for c in cfgs):
            if not shutil.which('swtpm'):
                raise VMBackendError(f'{self.name} backend: vm.tpm needs swtpm '
                                     '(dnf install swtpm)')
            if not os.path.isfile(OVMF_CODE):
                raise VMBackendError(f'{self.name} backend: vm.tpm boots with UEFI '
                                     f'and needs {OVMF_CODE} (dnf install edk2-ovmf)')

    def _pubkeys(self):
        """(authorized public key texts, private key for ssh or None)."""
        if self.cfg.get('ssh_key'):
            key = os.path.expanduser(self.cfg['ssh_key'])
            pubs = [key + '.pub']
        else:
            key = None
            pubs = [os.path.expanduser(f'~/.ssh/{n}.pub')
                    for n in ('id_ed25519', 'id_ecdsa', 'id_rsa')]
        pubs = [p for p in pubs if os.path.isfile(p)]
        if not pubs:
            raise VMBackendError(f'{self.name} backend: no ssh public key to authorize '
                                 '(set vm.ssh_key)')
        key = key or pubs[0][:-len('.pub')]     # private half of the first authorized key
        keys = []
        for p in pubs:
            with open(p) as f:
                keys.append(f.read().strip())
        return keys, key

    def _run(self, cmd, cwd=None):
        try:
            proc = subprocess.run(cmd, cwd=cwd, stdout=subprocess.PIPE,
                                  stderr=subprocess.STDOUT, timeout=600)
        except FileNotFoundError:
            raise VMBackendError(f'{self.name} backend: {cmd[0]} not found')
        out = proc.stdout.decode(errors='replace')
        if proc.returncode:
            raise VMBackendError(f'{self.name} backend: {shlex.join(cmd)} failed '
                                 f'({proc.returncode}):\n{out[-2000:]}')

    def _seed(self, vmdir, vm_id, keys, net):
        tool = next((t for t in ('genisoimage', 'mkisofs', 'xorriso')
                     if shutil.which(t)), None)
        if not tool:
            raise VMBackendError('qemu backend: need genisoimage, mkisofs or '
                                 'xorriso to build the cloud-init seed')
        self._seed_files(vmdir, vm_id, keys, net)
        args = ['-output', 'seed.iso', '-volid', 'cidata', '-joliet', '-rock',
                'user-data', 'meta-data', 'network-config']
        if tool == 'xorriso':
            args = ['-as', 'mkisofs'] + args
        self._run([tool] + args, cwd=vmdir)

    def _seed_files(self, vmdir, vm_id, keys, net, files=(), runcmd=()):
        """cloud-init user-data, meta-data and network-config in `vmdir`."""
        user = {'disable_root': False,
                'users': ['default',
                          {'name': 'root', 'ssh_authorized_keys': keys}],
                'write_files': [{'path': '/etc/hosts', 'append': True,
                                 'content': ''.join(f"{h['ip']} {h['name']}\n"
                                                    for h in net['hosts'])},
                                *files]}
        if runcmd:
            user['runcmd'] = list(runcmd)
        # The slirp DNS forwarder (10.0.2.3) uses the host's resolvers; on
        # networks where those do not answer the guest, vm.dns overrides
        # them per VM (default: none, keep the forwarder).
        dnscfg = {}
        if self.cfg.get('dns'):
            dnscfg = {'nameservers': {'addresses': list(self.cfg['dns'])}}
        netcfg = {'version': 2, 'ethernets': {
            'ssh': {'match': {'macaddress': net['mac_ssh']}, 'dhcp4': True,
                    **dnscfg},
            'priv': {'match': {'macaddress': net['mac_priv']},
                     'addresses': [f"{net['ip']}/24"]}}}
        with open(os.path.join(vmdir, 'user-data'), 'w') as f:
            f.write('#cloud-config\n' + yaml.safe_dump(user))
        with open(os.path.join(vmdir, 'meta-data'), 'w') as f:
            f.write(f"instance-id: {vm_id}\nlocal-hostname: {net['name']}\n")
        with open(os.path.join(vmdir, 'network-config'), 'w') as f:
            f.write(yaml.safe_dump(netcfg))


    def _fetch(self, url):
        """A cached local copy of an http(s):// image, downloaded to
        ~/.cache/freeipa-ci/images/<basename>."""
        dest = os.path.join(os.path.expanduser('~/.cache/freeipa-ci/images'),
                            os.path.basename(url))
        if os.path.isfile(dest):
            return dest
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        try:
            urllib.request.urlretrieve(url, dest)
        except Exception as e:
            try:
                os.remove(dest)
            except OSError:
                pass
            raise VMBackendError(f'qemu backend: downloading {url} failed: {e}')
        return dest

    def _display_args(self, c, q):
        """No display, or virtio-gpu with host OpenGL (virgl) rendered on
        the control node's DRM render node via egl-headless. The guest screen
        is also served over VNC + websocket on loopback (q['vnc_websocket'])."""
        gpu = c.get('gpu')
        if not gpu:
            return ['-display', 'none']
        if gpu != 'virgl':
            raise VMBackendError(f'qemu backend: unknown gpu {gpu!r} '
                                 f'(supported: virgl)')
        node = os.path.expanduser(c.get('render_node', '/dev/dri/renderD128'))
        if not os.access(node, os.R_OK | os.W_OK):
            raise VMBackendError(f'qemu backend: gpu virgl needs read/write '
                                 f'access to {node}')
        disp = _free_vnc_display()
        q['vnc_websocket'] = _free_port()
        return ['-device', 'virtio-vga-gl',
                '-display', f'egl-headless,rendernode={node}',
                '-vnc', f'127.0.0.1:{disp},websocket={q["vnc_websocket"]}']

    def _start_swtpm(self, vmdir) -> list:
        """Start a TPM 2.0 emulator for the VM; return the qemu arguments that
        attach it, and UEFI firmware: ``systemd-analyze has-tpm2`` says
        ``partial`` on a BIOS boot (with ``tpm-tis`` too). ``--terminate`` ends
        swtpm when qemu closes the control channel."""
        sock = os.path.join(vmdir, 'swtpm.sock')
        state = os.path.join(vmdir, 'tpm')
        os.makedirs(state)
        self._run(['swtpm', 'socket', '--tpm2', '--tpmstate', f'dir={state}',
                   '--ctrl', f'type=unixio,path={sock}', '--terminate', '--daemon',
                   '--pid', f'file={os.path.join(vmdir, "swtpm.pid")}',
                   '--log', f'file={os.path.join(vmdir, "swtpm.log")}'])
        for _ in range(50):
            if os.path.exists(sock):
                break
            time.sleep(0.1)
        else:
            raise VMBackendError('qemu backend: swtpm did not create its socket')
        return ['-chardev', f'socket,id=chrtpm,path={sock}',
                '-tpmdev', 'emulator,id=tpm0,chardev=chrtpm',
                '-device', 'tpm-crb,tpmdev=tpm0', '-bios', OVMF_CODE]

    def _stop_swtpm(self, vmdir):
        try:
            with open(os.path.join(vmdir, 'swtpm.pid')) as f:
                os.kill(int(f.read().strip()), signal.SIGTERM)
        except (FileNotFoundError, ProcessLookupError, ValueError):
            pass
        shutil.rmtree(os.path.join(vmdir, 'tpm'), ignore_errors=True)

    def _start(self, keys, key, net, seg, c):
        vm_id = f'freeipa-ci-{uuid.uuid4().hex[:10]}'
        vmdir = os.path.join(self._state_dir(), vm_id)
        os.makedirs(vmdir)
        q = {'dir': vmdir, 'pidfile': os.path.join(vmdir, 'qemu.pid'),
             'console': os.path.join(vmdir, 'console.log')}
        disk = os.path.join(vmdir, 'disk.qcow2')
        seed = os.path.join(vmdir, 'seed.iso')
        port = _free_port()
        # private NIC: one L2 segment shared by this run's VMs. Multicast
        # socket when the control node supports it, else a chain of TCP
        # socket netdevs on loopback (containers without multicast support).
        if MCAST_OK:
            priv_netdev = f'socket,id=priv,mcast={MCAST_GROUP}:{seg}'
        else:
            priv_netdev = c['priv_netdev']
        try:
            self._seed(vmdir, vm_id, keys, net)
            self._overlay(c, disk)
            self._run([self._qemu(), '-name', vm_id,
                       *self._qemu_system_args(),
                       '-smp', str(c.get('cpus', 4)),
                       '-m', str(c.get('memory', 8192)),
                       *self._display_args(c, q), *_usb_args(c),
                       *(self._start_swtpm(vmdir) if c.get('tpm') else []),
                       '-daemonize',
                       '-pidfile', q['pidfile'],
                       '-serial', f'file:{q["console"]}',
                       '-drive', f'file={disk},if=virtio,format=qcow2',
                       '-drive', f'file={seed},if=virtio,media=cdrom,readonly=on',
                       # ssh NIC: NAT with a forwarded port on the control node
                       '-netdev', f'user,id=net0,hostfwd=tcp:127.0.0.1:{port}-:22',
                       '-device', f'virtio-net-pci,netdev=net0,mac={net["mac_ssh"]}',
                       # private NIC: the segment shared by this run
                       '-netdev', priv_netdev,
                       '-device', f'virtio-net-pci,netdev=priv,mac={net["mac_priv"]}'])
        except Exception:
            self._stop_swtpm(vmdir)
            shutil.rmtree(vmdir, ignore_errors=True)
            raise
        if 'vnc_websocket' in q:
            print(f'== vm {vm_id}: VNC websocket 127.0.0.1:{q["vnc_websocket"]}')
        return VMHandle(host='127.0.0.1', port=port, id=vm_id, key=key,
                        meta={'ephemeral_hostkey': True, 'qemu': q,
                              'private': net})

    def _overlay(self, c, disk):
        self._run(['qemu-img', 'create', '-q', '-f', 'qcow2', '-F', 'qcow2',
                   '-b', os.path.expanduser(c['image']), disk,
                   str(c.get('disk', '40G'))])

    def _cfgs_with_images(self, count):
        """Per-VM configs with container-disk image references resolved."""
        cfgs = self._vm_cfgs(count)
        pulled = {}
        for c in cfgs:
            if c.get('image'):
                # the arch this VM will boot (per-VM `arch` included), so a
                # multi-arch containerdisk yields its matching member (and
                # a tag without that member fails here, not at boot)
                arch = c.get('arch') or self._arch()
                if arch == 'arm64':
                    arch = 'aarch64'
                key = (c['image'], arch)
                try:
                    if key not in pulled:
                        pulled[key] = containerdisk.resolve(c['image'], arch)
                except containerdisk.ContainerDiskError as e:
                    raise VMBackendError(f'{self.name} backend: {e}')
                c['image'] = pulled[key]
        for c in cfgs:
            image = c.get('image') or ''
            if image.startswith(('http://', 'https://')):
                fetched = self._fetch(image)
                for d in cfgs:
                    if d.get('image') == image:
                        d['image'] = fetched
        return cfgs

    def _nets(self, cfgs):
        names = [c.get('name', f'vm{i + 1}') for i, c in enumerate(cfgs)]
        if len(set(names)) != len(names):
            raise VMBackendError(f'{self.name} backend: duplicate VM names: {names}')
        nets = [{'name': n, 'ip': f'{PRIVATE_PREFIX}.{10 + i}',
                 'mac_ssh': _mac(), 'mac_priv': _mac()}
                for i, n in enumerate(names)]
        for n in nets:
            n['hosts'] = [{'name': m['name'], 'ip': m['ip']} for m in nets]
        return nets

    def provision(self, count=1):
        cfgs = self._cfgs_with_images(count)
        self._check_host(cfgs)
        _usb_assign(cfgs)
        keys, key = self._pubkeys()
        # one private L2 segment per call: runs on the same host stay isolated
        seg = _free_port(socket.SOCK_DGRAM)
        if MCAST_OK:
            for c in cfgs:
                c['priv_netdev'] = f'socket,id=priv,mcast={MCAST_GROUP}:{seg}'
        else:
            print('== qemu backend: multicast unavailable on this host, '
                  'using TCP socket segments for the private NICs')
            # a listening TCP netdev per VM; each later VM connects to the
            # previous one: qemu socket netdevs chain into one L2 segment
            ports = []
            for c in cfgs:
                ports.append(_free_port())
                c['priv_netdev'] = f'socket,id=priv,listen=127.0.0.1:{ports[-1]}'
            for i in range(1, len(cfgs)):
                cfgs[i]['priv_netdev'] = \
                    f'socket,id=priv,connect=127.0.0.1:{ports[i - 1]}'
        nets = self._nets(cfgs)
        handles = []
        try:
            for net, c in zip(nets, cfgs):
                handles.append(self._start(keys, key, net, seg, c))
        except Exception:
            for h in handles:
                try:
                    self.terminate(h)
                except VMBackendError as e:
                    print(f'== {e}')
            raise
        return handles

    def terminate(self, handle):
        q = handle.meta.get('qemu')
        if not q:
            raise VMBackendError(
                f'{self.name} backend: no {self.name} state for {handle.target}')
        pid = _qemu_pid(q['pidfile'], handle.id)
        if pid:
            stop_qemu(pid)
        self._stop_swtpm(q['dir'])
        for f in self._VM_FILES:
            try:
                os.remove(os.path.join(q['dir'], f))
            except FileNotFoundError:
                pass
        print(f'== vm {handle.target}: {self.name} {handle.id} stopped '
              f'(console: {q["console"]})')


KRUNPROC = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'krunproc.py')


class KrunBackend(QemuBackend):
    """Local microVMs under libkrun (KVM) on the control node: no cloud API.

    Same contract as the qemu backend (qcow2 overlay on a cloud image, cloud-init,
    ssh on a forwarded 127.0.0.1 port, private NIC 192.168.100.0/24) but no qemu:
    each VM is a ``krunproc.py vmm`` process (libkrun via ctypes), the ssh NIC is
    served by passt, the private NIC by a ``krunproc.py hub`` process per ``up``.
    Config is the qemu backend's except ``gpu``, ``render_node``, ``usb``,
    ``qemu`` (errors), plus::

        root_device: /dev/vda3        # root partition of the image (Fedora Cloud)
        root_fstype: btrfs
        root_options: subvol=root

    The guest runs libkrunfw's kernel (no modules, no iso9660, NFS or autofs, no
    iptables-legacy, <= 16 vCPUs); a guest reboot ends the VM. Needs libkrun,
    passt, qemu-img, mkfs.vfat and mcopy (dosfstools, mtools) and /dev/kvm.
    """
    name = 'krun'
    _VM_FILES = QemuBackend._VM_FILES + ('seed.img', 'network-config', 'vm.json')
    _UNSUPPORTED = ('gpu', 'render_node', 'usb', 'qemu', 'tpm', 'arch',
                    'accel', 'cpu', 'machine', 'firmware')
    _MAX_VCPUS = 16

    def _needs_kvm(self):
        return True     # libkrun is KVM-only, whatever _accel() would say

    _MAX_DIR = 83   # unix socket paths are 107 bytes; longest: <vmdir>/krun-net-<7 digit pid>-0.sock

    def _tools(self):
        return ['qemu-img', 'passt', 'mkfs.vfat', 'mcopy']

    def _check_krun(self, cfgs):
        for c in cfgs:
            for k in self._UNSUPPORTED:
                if c.get(k):
                    raise VMBackendError(f'krun backend: `vm.{k}` is not supported')
            if not 1 <= int(c.get('cpus', 4)) <= self._MAX_VCPUS:
                raise VMBackendError('krun backend: cpus must be 1..16 (libkrun limit)')
        if len(os.path.join(self._state_dir(), 'freeipa-ci-0123456789')) > self._MAX_DIR:
            raise VMBackendError(
                'krun backend: vm.state_dir is too long for unix socket paths (the '
                f'VM directory path must be at most {self._MAX_DIR} bytes); set a '
                'shorter vm.state_dir')

    def _check_host(self, cfgs):
        super()._check_host(cfgs)
        try:
            ctypes.CDLL('libkrun.so.1')
        except OSError:
            raise VMBackendError('krun backend: libkrun.so.1 not found '
                                 '(Fedora: dnf install libkrun)')
        self._check_krun(cfgs)

    def _seed(self, vmdir, vm_id, keys, net):
        # libkrunfw's kernel has no iso9660 and cloud-init only reads vfat/iso9660;
        # the sysctl override + restart fixes the one unit that otherwise fails at boot
        self._seed_files(
            vmdir, vm_id, keys, net,
            files=[{'path': '/etc/sysctl.d/50-pid-max.conf',
                    'content': '# libkrunfw kernel cannot raise kernel.pid_max\n'}],
            runcmd=[['systemctl', 'restart', 'systemd-sysctl.service']])
        img = os.path.join(vmdir, 'seed.img')
        with open(img, 'wb') as f:
            f.truncate(4 << 20)
        self._run(['mkfs.vfat', '-n', 'CIDATA', img])
        self._run(['mcopy', '-i', img, 'user-data', 'meta-data', 'network-config',
                   '::'], cwd=vmdir)

    def _start_vm(self, vm_id, keys, key, net, c, hub):
        vmdir = os.path.join(self._state_dir(), vm_id)
        os.makedirs(vmdir, exist_ok=True)   # the first VM's dir also holds the hub
        q = {'dir': vmdir, 'pidfile': os.path.join(vmdir, 'qemu.pid'),
             'console': os.path.join(vmdir, 'console.log')}
        disk = os.path.join(vmdir, 'disk.qcow2')
        spec = os.path.join(vmdir, 'vm.json')
        memory = int(c.get('memory', 8192))
        if 3285 <= memory <= 3328:
            # shortcut: libkrun 1.19.7 guests die at boot (no output) when RAM
            # ends in the last ~40 MiB below the 3.25 GiB MMIO gap; step past it
            memory = 3336
        port = _free_port()
        try:
            self._seed(vmdir, vm_id, keys, net)
            self._overlay(c, disk)
            with open(spec, 'w') as f:
                json.dump({
                    'name': net['name'], 'cpus': int(c.get('cpus', 4)),
                    'memory': memory, 'disk': disk,
                    'seed': os.path.join(vmdir, 'seed.img'), 'ssh_port': port,
                    'mac_ssh': net['mac_ssh'], 'mac_priv': net['mac_priv'],
                    'hub': hub, 'console': q['console'],
                    'passt_log': os.path.join(vmdir, 'passt.log'),
                    'root_device': c.get('root_device', '/dev/vda3'),
                    'root_fstype': c.get('root_fstype', 'btrfs'),
                    'root_options': c.get('root_options', 'subvol=root')}, f)
            with open(os.path.join(vmdir, 'vmm.log'), 'w') as log:
                proc = subprocess.Popen(
                    [sys.executable, KRUNPROC, 'vmm', spec], start_new_session=True,
                    stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                    env={**os.environ, 'TMPDIR': vmdir})
            with open(q['pidfile'], 'w') as f:
                f.write(str(proc.pid))
        except Exception:
            shutil.rmtree(vmdir, ignore_errors=True)
            raise
        return VMHandle(host='127.0.0.1', port=port, id=vm_id, key=key,
                        meta={'ephemeral_hostkey': True, 'qemu': q, 'private': net})

    def provision(self, count=1):
        cfgs = self._cfgs_with_images(count)
        self._check_host(cfgs)
        keys, key = self._pubkeys()
        nets = self._nets(cfgs)
        ids = [f'freeipa-ci-{uuid.uuid4().hex[:10]}' for _ in cfgs]
        dirs = [os.path.join(self._state_dir(), i) for i in ids]
        for d in dirs:
            os.makedirs(d)
        hub_sock = os.path.join(dirs[0], 'hub.sock')
        handles, hub = [], None
        try:
            with open(os.path.join(dirs[0], 'hub.log'), 'w') as log:
                hub = subprocess.Popen(
                    [sys.executable, KRUNPROC, 'hub', hub_sock,
                     *[os.path.join(d, 'qemu.pid') for d in dirs]],
                    start_new_session=True, stdin=subprocess.DEVNULL,
                    stdout=log, stderr=subprocess.STDOUT)
            for _ in range(100):
                if os.path.exists(hub_sock):
                    break
                time.sleep(0.05)
            else:
                raise VMBackendError('krun backend: the private-network hub did '
                                     f'not start (see {dirs[0]}/hub.log)')
            for i, (net, c) in enumerate(zip(nets, cfgs)):
                handles.append(self._start_vm(ids[i], keys, key, net, c, hub_sock))
            time.sleep(2)
            for h in handles:
                if _qemu_pid(h.meta['qemu']['pidfile'], h.id) is None:
                    with open(os.path.join(h.meta['qemu']['dir'], 'vmm.log')) as f:
                        tail = f.read()[-2000:]
                    raise VMBackendError(f'krun backend: {h.id} exited at start:\n{tail}')
        except Exception:
            for h in handles:
                try:
                    self.terminate(h)
                except VMBackendError as e:
                    print(f'== {e}')
            if hub:
                hub.terminate()
            for i, d in zip(ids, dirs):
                if i not in [h.id for h in handles]:
                    shutil.rmtree(d, ignore_errors=True)
            raise
        return handles

    def terminate(self, handle):
        super().terminate(handle)
        for s in glob.glob(os.path.join(handle.meta['qemu']['dir'], 'krun-net-*.sock')):
            os.remove(s)


BACKENDS = {
    'ssh': SshBackend,
    'command': CommandBackend,
    'openstack': OpenstackBackend,
    'qemu': QemuBackend,
    'krun': KrunBackend,
}


def make_backend(cfg: Dict) -> VMBackend:
    cfg = cfg or {}
    name = cfg.get('backend')
    if not name:
        raise VMBackendError('vm backend: `vm.backend` is required')
    cls = BACKENDS.get(name)
    if cls is None:
        raise VMBackendError(
            f'unknown vm backend {name!r} (known: {sorted(BACKENDS)})')
    return cls(cfg)
