"""Nested provider (design doc §3.9): get a VM, run the env in it.

Composes an outer VM backend (``vmbackend.VMBackend`` — the "get a VM"
cloud-API half) with an inner provider (default ``podman`` — the "run the
env in the VM" half).  The inner environment is the *same* EnvSpec with
``provider`` set to the inner type and the ``vm:`` block stripped; the
nested provider materializes it, ships it to the provisioned VM, and
drives the inner provider there by re-invoking ``freeipa-env up/run/down
<inner-spec> --workdir <remote>`` over ssh.  The whole validated podman
flow runs unchanged — just on a VM acquired through the API.

Inner provider ``external`` places each host on its own VM, named after the
host.  A ``vm.vms`` entry named after a host overrides that VM's settings (e.g.
``cpus``).  The
external provider attaches to every VM over its forwarded ssh port, and the
hosts reach each other over the backend's private network (``ip``).  In
this mode ``up``/``run``/``down`` run from the control node and nothing is
shipped to the VMs.

The qemu/krun VM memory is each host's ``resources`` memory; a ``memory`` in the vm block is an error here.

The nested provider runs on the *control node* (a CI machine that has the
cloud API creds + ssh).  ``up`` provisions its own VM(s) and persists the
handles to ``<workdir>/nested-state.json`` so that a later, separate
``freeipa-env down`` invocation knows what to tear down.
"""

import json
import os
import sys
import shlex
import shutil

import yaml

from . import guest_settings, paths, samba_ad
from .envspec import EnvSpec
from .external_provider import ExternalProvider
from .vmbackend import (VMHandle, make_backend, rsync_from, rsync_to,
                        ssh_run)


class NestedProvider:
    def __init__(self, spec, workdir, args=None):
        self.spec = spec
        self.workdir = workdir
        self.args = args
        self.inner_type = (spec.inner or 'podman').strip()
        self.backend = make_backend(self._vm_cfg())
        self.logdir = os.path.join(workdir, 'logs')
        self._state_path = os.path.join(workdir, 'nested-state.json')
        self._vms = []

    # ------------------------------------------------------------- vm config
    def _cfg(self, key, default=None):
        return (self.spec.vm or {}).get(key, default)

    def _vm_cfg(self):
        """Backend config.  Inner external sizes each qemu/krun VM from the memory
        of its host's `resources` (the test requirement), so a `memory` in the
        vm block would contradict it and is rejected."""
        cfg = dict(self.spec.vm or {})
        if self.inner_type != 'external' or cfg.get('backend') not in ('qemu', 'krun'):
            return cfg
        over = {v.get('name'): dict(v) for v in cfg.get('vms') or []}
        if 'memory' in cfg or any('memory' in v for v in over.values()):
            raise NestedError(
                "inner external sizes each VM from its host's resources; "
                "remove `memory` from the vm block")
        extra = sorted(str(n) for n in set(over) - {h.name for h in self.spec.hosts})
        if extra:
            raise NestedError(f'vm.vms {extra} match no host; a VM is named after its host')
        vms = []
        for h in self.spec.hosts:
            mem = self.spec.resource(h.role)['memory']
            if not mem.endswith('m'):
                raise NestedError(f'host {h.name}: memory {mem!r} must be in MiB')
            vms.append({**over.get(h.name, {}), 'name': h.name, 'memory': int(mem[:-1])})
        cfg['vms'] = vms
        return cfg

    def _remote_ci(self):
        return self._cfg('remote_ci', '~/freeipa-ci/ci')

    def _remote_env(self):
        return os.path.join(self._remote_ci(), 'env')

    def _remote_workdir(self):
        return self._cfg('remote_workdir',
                         f'~/ciwork/{self.spec.name}')

    @staticmethod
    def _rpath(p):
        """Remote path for the shell: keep a leading ~/ expandable."""
        if p.startswith('~/'):
            return '~/' + shlex.quote(p[2:])
        return shlex.quote(p)

    # -------------------------------------------------------------- inner spec
    def _inner_doc(self):
        """The inner EnvSpec dict: same spec, inner provider, no vm block."""
        d = {k: v for k, v in self.spec.raw.items()}
        d['provider'] = self.inner_type
        d.pop('vm', None)
        d.pop('inner', None)
        return d

    def _inner_path(self):
        return os.path.join(self.workdir, 'inner-env.yaml')

    def _write_inner_file(self, doc=None):
        os.makedirs(self.workdir, exist_ok=True)
        path = self._inner_path()
        with open(path, 'w') as f:
            yaml.safe_dump(doc or self._inner_doc(), f,
                           default_flow_style=False, sort_keys=False)
        return path

    # ------------------------------------------------------------------ state
    def _load_state(self):
        if not os.path.exists(self._state_path):
            return None
        with open(self._state_path) as f:
            return json.load(f)

    def _save_state(self, remote_workdir):
        state = {
            'vms': [v.to_dict() for v in self._vms],
            'remote_workdir': remote_workdir,
            'inner': self.inner_type,
            'name': self.spec.name,
        }
        with open(self._state_path, 'w') as f:
            json.dump(state, f, indent=2)

    def _clear_state(self):
        if os.path.exists(self._state_path):
            os.remove(self._state_path)

    def _primary(self):
        return self._vms[0]

    def _inner_abs(self, state):
        return os.path.join(state['remote_workdir'], 'inner-env.yaml')

    def _load_vms(self):
        if self._vms:
            return
        state = self._load_state()
        if not state or not state.get('vms'):
            raise NestedError(
                'no provisioned VMs recorded (did `up` succeed? state file '
                f'{self._state_path} is missing)')
        self._vms = [VMHandle.from_dict(d) for d in state['vms']]
        self._state = state

    # ------------------------------------------------------ external placement
    @staticmethod
    def _vm_name(vm, i):
        return (vm.meta.get('private') or {}).get('name') or f'vm{i + 1}'

    @staticmethod
    def _vm_ip(vm):
        return (vm.meta.get('private') or {}).get('ip') or vm.host

    def _place(self):
        """host name -> index into self._vms: each host runs on the VM named after it."""
        names = [self._vm_name(v, i) for i, v in enumerate(self._vms)]
        place = {}
        for h in self.spec.hosts:
            if h.name not in names:
                raise NestedError(f'host {h.name}: no VM named after it (VMs: {names})')
            place[h.name] = names.index(h.name)
        return place

    def _external_doc(self, place):
        """Inner external spec: each host is its VM's forwarded ssh port;
        `ip` is the address the other hosts use (private network)."""
        d = self._inner_doc()
        d['provider'] = 'external'
        hosts = []
        for raw in self.spec.raw['hosts']:
            vm = self._vms[place[raw['name']]]
            h = dict(raw)
            domain = (self.spec.ad_domain if raw['role'] == 'ad'
                      else self.spec.domain)
            h.update(name=f"{raw['name']}.{domain}",
                     address=vm.host, port=vm.port, ip=self._vm_ip(vm))
            hosts.append(h)
        d['hosts'] = hosts
        return d

    def _provision_ad(self, ext, h):
        """Make the VM of an `ad` host a Samba AD domain controller"""
        if h.os != 'samba':
            raise NestedError(
                f'host {h.name}: a nested `ad` host is provisioned here, '
                f'which only knows `os: samba`')
        print(f'== {h.name}: provisioning a Samba AD DC for '
              f'{self.spec.ad_domain}')
        script = samba_ad.script(
            self.spec.ad_domain, self.spec.ad_domain.split('.')[0].upper(),
            h.ip, self.spec.dns_forwarder or '8.8.8.8')
        ext._ssh(h, 'bash -ec ' + shlex.quote(script), timeout=1200)

    def _external(self):
        """ExternalProvider over the inner spec, driven from the control node."""
        spec = EnvSpec.from_file(self._inner_path())
        return ExternalProvider(spec, os.path.join(self.workdir, 'inner'),
                                ssh_key=self._vms[0].key)

    # --------------------------------------------------------------- remote op
    def _remote_cmd(self, state, verb):
        env = self._remote_env()
        return (f'cd {self._rpath(env)} && ./freeipa-env {verb} '
                f'{self._rpath(self._inner_abs(state))} '
                f'--workdir {self._rpath(state["remote_workdir"])}')

    def _ssh_stream(self, vm, cmd, logname, timeout=None):
        os.makedirs(self.logdir, exist_ok=True)
        logpath = os.path.join(self.logdir, logname)
        print(f'== vm {vm.target}: {cmd}')
        return ssh_run(vm, cmd, stream=True, log_path=logpath,
                       timeout=timeout)[0]

    # ---------------------------------------------------------------- bootstrap
    def _bootstrap(self, vm):
        """Ship the ci-freeipa tree + inner spec to the VM, ensure the image."""
        remote_ci = self._remote_ci()
        print(f'== vm {vm.target}: rsync ci-freeipa tree '
              f'({paths.CI_ROOT} -> {remote_ci})')
        for src, rel in paths.ship_sources():
            dst = remote_ci if not rel else f'{remote_ci}/{rel}'
            rsync_to(vm, src.rstrip('/') + '/', dst + '/')
        state_rwd = self._remote_workdir()
        ssh_run(vm, f'mkdir -p {self._rpath(state_rwd)}', check=True)
        inner_local = os.path.join(self.workdir, 'inner-env.yaml')
        rsync_to(vm, inner_local,
                 os.path.join(state_rwd, 'inner-env.yaml'))
        # ensure the image the inner env needs is present on the VM
        ref = self._cfg('image_ref')
        if ref:
            rc, _ = ssh_run(vm,
                            f'podman image inspect {shlex.quote(ref)} '
                            f'--format {{.Id}}', check=False)
            if rc != 0:
                print(f'== vm {vm.target}: image {ref} missing, pulling')
                ssh_run(vm, f'podman pull {shlex.quote(ref)}',
                        check=True, log_path=os.path.join(
                            self.logdir, 'image-pull.log'))
        print(f'== vm {vm.target}: bootstrap complete')

    # ------------------------------------------------------------------ up/down
    def up(self):
        state = self._load_state()
        if state and state.get('vms'):
            # a second `up` would overwrite the state file and orphan these
            ids = ', '.join(v['id'] for v in state['vms'])
            raise NestedError(
                f'{self.spec.name} already has VMs ({ids}); run '
                f'`freeipa-env down` on it first (`freeipa-env vms` lists '
                f'the running ones)')
        if self.inner_type == 'external':
            return self._up_external()
        count = int(self._cfg('count', 1) or 1)
        timeout = int(self._cfg('ready_timeout', 600))
        os.makedirs(self.logdir, exist_ok=True)
        print(f'== nested provider: provisioning {count} VM(s) via '
              f'{self.backend.describe()}')
        self._vms = self.backend.provision(count)
        for v in self._vms:
            self.backend.wait_ready(v, timeout=timeout)
        state_rwd = self._remote_workdir()
        self._save_state(state_rwd)
        inner = self._write_inner_file()
        print(f'== inner spec (provider: {self.inner_type}): {inner}')
        if self.inner_type == 'podman' and count > 1:
            print(f'== note: the podman inner env runs on the first VM; '
                  f'{count - 1} extra VM(s) are provisioned and released '
                  f'with it')
        primary = self._primary()
        self._bootstrap(primary)
        cmd = self._remote_cmd({'remote_workdir': state_rwd}, 'up')
        rc = self._ssh_stream(primary, cmd, 'nested-up.log')
        if rc != 0:
            raise NestedError(
                f'inner `freeipa-env up` failed on {primary.target} '
                f'({rc}); VM(s) left provisioned — run `freeipa-env down` '
                f'to release them (or keep for triage)')
        print(f'== nested environment {self.spec.name} is up on '
              f'{primary.target}')

    def _up_external(self):
        count = len(self.spec.hosts)
        if self._cfg('count') not in (None, count):
            raise NestedError(
                f'vm.count is {self._cfg("count")} but there are {count} '
                f'hosts; inner external runs one VM per host')
        timeout = int(self._cfg('ready_timeout', 600))
        os.makedirs(self.logdir, exist_ok=True)
        print(f'== nested provider: provisioning {count} VM(s) via '
              f'{self.backend.describe()}')
        self._vms = self.backend.provision(count)
        for v in self._vms:
            self.backend.wait_ready(v, timeout=timeout)
        self._save_state(self._remote_workdir())
        place = self._place()
        self._write_inner_file(self._external_doc(place))
        print(f'== inner spec (provider: external): {self._inner_path()}')
        ext = self._external()
        # ipa-server-install takes the primary name from the first /etc/hosts
        # line for the IP: drop cloud-init's bare "ip short" line first
        for h in ext.spec.hosts:
            short = h.name.split('.')[0]
            ext._ssh(h, f"hostnamectl set-hostname {h.name} && "
                        f"sed -i '/^{h.ip} /d' /etc/hosts && "
                        f"echo '{h.ip} {h.name} {short}' >> /etc/hosts")
        # bootc hosts keep /usr read-only; the overlay lets dnf install into it
        # (discarded on reboot)
        if self._cfg('bootc'):
            for h in ext.spec.hosts:
                ext._ssh(h, 'bootc usroverlay', timeout=600)
        ipa_hosts = [h for h in ext.spec.hosts if not h.is_ad]
        try:
            done = guest_settings.apply(
                self.spec.vm or {}, ipa_hosts,
                lambda h, cmd, t: ext._ssh(h, cmd, timeout=t),
                ext._scp)
        except ValueError as e:
            raise NestedError(str(e)) from e
        print(f'== guest setup on {len(ipa_hosts)} VM(s): '
              + '; '.join(f'{n}: {s}' for n, s in done.items()))
        for h in (h for h in ext.spec.hosts if h.is_ad):
            self._provision_ad(ext, h)
        ext.up()
        print(f'== nested environment {self.spec.name} is attached on '
              f'{count} VM(s)')

    def down(self, collect=True):
        self._load_vms()
        if self.inner_type == 'external':
            ok = self._down_external(collect)
        else:
            ok = self._down_podman(collect)
        for v in self._vms:
            try:
                self.backend.terminate(v)
            except Exception as e:          # noqa: BLE001
                print(f'== terminate {v.target} failed: {e}')
        self._clear_state()
        self._vms = []
        print(f'== nested environment {self.spec.name} torn down')
        if not ok:
            raise NestedError('inner down reported a failure (see above)')

    def _down_external(self, collect):
        if not collect:
            return True
        try:
            self._external().down(collect=True)
            # the inner external tree lives under inner/logs; `logs` reads ours
            src = os.path.join(self.workdir, 'inner', 'logs')
            shutil.copytree(src, self.logdir, dirs_exist_ok=True)
            return True
        except Exception as e:              # noqa: BLE001
            print(f'== external log collection failed: {e}')
            return False

    def _down_podman(self, collect):
        state = self._state
        primary = self._primary()
        ok = True
        if collect:
            rc = self._ssh_stream(
                primary, self._remote_cmd(state, 'down'), 'nested-down.log')
            if rc != 0:
                print(f'== inner `freeipa-env down` on {primary.target} '
                      f'failed ({rc}); continuing to release VMs')
                ok = False
            # fetch the remote workdir (logs, xunit, collected) back
            try:
                os.makedirs(self.logdir, exist_ok=True)
                rsync_from(primary, state['remote_workdir'] + '/',
                           self.workdir)
                self._fetch_xunit(state)
            except Exception as e:          # noqa: BLE001
                print(f'== artifact fetch-back failed: {e}')
        return ok

    def _fetch_xunit(self, state):
        # collect the inner env's xunit (if any) to <workdir>/logs/nosetests.xml
        candidates = []
        collected = os.path.join(self.workdir, 'logs', 'collected')
        if os.path.isdir(collected):
            for d in os.listdir(collected):
                p = os.path.join(collected, d, 'nosetests.xml')
                if os.path.isfile(p):
                    candidates.append(p)
        src = max(candidates, key=os.path.getmtime) if candidates else None
        if src:
            dst = os.path.join(self.logdir, 'nosetests.xml')
            import shutil
            shutil.copyfile(src, dst)
            print(f'== xunit: {src} -> {dst}')

    # -------------------------------------------------------------------- run
    def copy_to_controller(self, local, remote):
        if self.inner_type == 'external':
            self._load_vms()
            return self._external().copy_to_controller(local, remote)
        # the podman inner provider does its own copy at run time
        pass

    def clear_xunit(self):
        if self.inner_type == 'external':
            self._load_vms()
            self._external().clear_xunit()
        # the podman inner provider clears its own report at run time

    def fetch_xunit(self, workdir):
        """The run's report on the VM, to <workdir>/nosetests.xml."""
        self._load_vms()
        if self.inner_type == 'external':
            return self._external().fetch_xunit(workdir)
        try:
            rsync_from(self._primary(),
                       f'{self._state["remote_workdir"]}/nosetests.xml',
                       os.path.join(workdir, 'nosetests.xml'))
        except Exception as e:              # noqa: BLE001
            print(f'== no xunit report fetched: {e}', file=sys.stderr)
            return False
        return True

    def run_in_controller(self, cmd, env=None, log='run.log', timeout=None):
        """Drive the inner `freeipa-env run` on the primary VM.

        ``cmd``/``env`` from the CLI are intentionally ignored: the inner
        provider recomputes the identical test selection from the inner
        spec (which mirrors this spec, including any --run override).
        In external mode the test command is run on the master host instead,
        so the CLI's ``cmd``/``env`` are passed through."""
        self._load_vms()
        if self.inner_type == 'external':
            return self._external().run_in_controller(
                cmd, env=env, log=log, timeout=timeout)
        state = self._state
        primary = self._primary()
        rc = self._ssh_stream(primary,
                              self._remote_cmd(state, 'run'),
                              'nested-run.log', timeout=timeout)
        return rc

    # ------------------------------------------------------------------- misc
    def resolve_images(self):
        # the VM does not exist yet at resolve time; the inner provider
        # resolves the image on the VM during `up`. Read-only, returns {}.
        print(f'== nested provider: image resolution deferred to the '
              f'provisioned VM at `up` time (backend: '
              f'{self.backend.describe()})')
        return {}

    def ensure_images(self, force=None):
        # the VM does not exist yet; the inner provider builds the channel
        # image on the VM during `up` (mirrors resolve_images deferral).
        print(f'== nested provider: image build deferred to the provisioned '
              f'VM at `up` time (backend: {self.backend.describe()})')
        return {}

    def collect_logs(self):
        # logs are fetched back by down(); nothing local to collect
        pass

    def show(self):
        if not self._vms:
            state = self._load_state()
            if state and state.get('vms'):
                self._vms = [VMHandle.from_dict(d) for d in state['vms']]
        if not self._vms:
            print('== no nested environment is up')
            return
        if self.inner_type == 'external':
            for i, v in enumerate(self._vms):
                print(f'vm[{i}] {v.target} id={v.id or "-"} '
                      f'[{self.backend.describe()}]')
            self._external().show()
            return
        for i, v in enumerate(self._vms):
            tag = ' (primary)' if i == 0 else ''
            print(f'vm[{i}] {v.target} id={v.id or "-"}{tag} '
                  f'[{self.backend.describe()}]')
        state = self._load_state()
        if state:
            self._ssh_stream(self._primary(),
                             self._remote_cmd(state, 'show'),
                             'nested-show.log')


class NestedError(Exception):
    pass
