"""Processes of the krun VM backend (vmbackend.KrunBackend), run as scripts:

    python3 krunproc.py vmm SPEC.json         one microVM under libkrun;
                                              returns when the guest stops
    python3 krunproc.py hub SOCK PIDFILE...   L2 hub for the VMs' private NICs

Separate processes because krun_start_enter() takes its process over.
"""
import ctypes
import json
import os
import select
import signal
import socket
import subprocess
import sys
import time

KRUN_DISK_FORMAT_QCOW2 = 1
KRUN_SYNC_RELAXED = 1
# virtio-net features: libkrun's COMPAT_NET_FEATURES, for the passt NIC. The
# private NIC gets none: the hub forwards frames as they are and nothing
# completes a partial checksum, so TCP between the VMs needs the guest to
# checksum in full (offloads on made the receivers drop segments).
NET_FEATURES = (1 << 0) | (1 << 1) | (1 << 7) | (1 << 10) | (1 << 11) | (1 << 14)


def _check(ret, what):
    if ret < 0:
        sys.exit(f'krun: {what} failed: {os.strerror(-ret)}')


def _mac(text):
    return (ctypes.c_uint8 * 6)(*bytes.fromhex(text.replace(':', '')))


def vmm(spec_path):
    with open(spec_path) as f:
        s = json.load(f)
    krun = ctypes.CDLL('libkrun.so.1')
    # passt serves the ssh NIC; it exits when our end of the socketpair closes
    here, there = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    libc = ctypes.CDLL(None)
    subprocess.Popen(
        ['passt', '-f', '-a', '10.0.2.15', '-n', '24', '-g', '10.0.2.2',
         '-t', f'127.0.0.1/{s["ssh_port"]}:22', '--fd', str(there.fileno())],
        pass_fds=[there.fileno()], stdout=subprocess.DEVNULL,
        stderr=open(s['passt_log'], 'w'),
        preexec_fn=lambda: libc.prctl(1, signal.SIGTERM))  # PR_SET_PDEATHSIG
    there.close()
    ctx = krun.krun_create_ctx()
    _check(ctx, 'create_ctx')
    _check(krun.krun_set_vm_config(ctx, s['cpus'], s['memory']), 'set_vm_config')
    _check(krun.krun_add_disk3(ctx, b'root', s['disk'].encode(),
                               KRUN_DISK_FORMAT_QCOW2, False, False,
                               KRUN_SYNC_RELAXED), 'add_disk root')
    _check(krun.krun_add_disk3(ctx, b'seed', s['seed'].encode(), 0, True,
                               False, KRUN_SYNC_RELAXED), 'add_disk seed')
    _check(krun.krun_add_net_unixstream(ctx, None, here.fileno(),
                                        _mac(s['mac_ssh']), NET_FEATURES, 0),
           'add_net ssh')
    _check(krun.krun_add_net_unixgram(ctx, s['hub'].encode(), -1,
                                      _mac(s['mac_priv']), 0, 0),
           'add_net private')
    _check(krun.krun_set_root_disk_remount(
        ctx, s['root_device'].encode(), s['root_fstype'].encode(),
        s['root_options'].encode()), 'set_root_disk_remount')
    env = [b'KRUN_INIT=/usr/lib/systemd/systemd', b'KRUN_INIT_PID1=1',
           b'HOSTNAME=' + s['name'].encode(), None]
    _check(krun.krun_set_env(ctx, (ctypes.c_char_p * len(env))(*env)), 'set_env')
    _check(krun.krun_set_console_output(ctx, s['console'].encode()),
           'set_console_output')
    _check(krun.krun_start_enter(ctx), 'start_enter')


def _alive(pid):
    try:
        with open(f'/proc/{pid}/cmdline', 'rb') as f:
            return bool(f.read())      # a zombie has an empty cmdline
    except OSError:
        pass          # no /proc (macOS): fall through to ps
    try:
        p = subprocess.run(['ps', '-p', str(pid), '-o', 'stat='],
                           capture_output=True, text=True)
    except OSError:
        return False
    return p.returncode == 0 and not p.stdout.strip().startswith('Z')


def hub(sock_path, pidfiles):
    """Flood each frame to every other peer seen so far. Exit once a VM in
    `pidfiles` has started and none is alive any more, or when none starts
    within 120 s (a failed `up`)."""
    s = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4 << 20)
    s.bind(sock_path)
    peers, started, t0 = set(), False, time.time()
    while True:
        if select.select([s], [], [], 2)[0]:
            frame, src = s.recvfrom(65536)
            if src:
                peers.add(src)
                for p in list(peers - {src}):
                    try:
                        s.sendto(frame, p)
                    except OSError:
                        peers.discard(p)
        pids = []
        for f in pidfiles:
            try:
                with open(f) as pf:
                    pids.append(int(pf.read()))
            except (OSError, ValueError):
                pass
        started = started or bool(pids)
        if (started and not any(map(_alive, pids))) or \
                (not started and time.time() - t0 > 120):
            os.unlink(sock_path)
            return


if __name__ == '__main__':
    if sys.argv[1] == 'vmm':
        vmm(sys.argv[2])
    elif sys.argv[1] == 'hub':
        hub(sys.argv[2], sys.argv[3:])
