"""external provider (B4): attach to pre-created environments.

Hosts are provisioned elsewhere (VMs, bare metal, an existing AD forest).
The env spec lists them with address:; the provider's contract is the same
multihost config YAML, so the test side is unchanged.

  up    = attach: validate SSH reachability, hostname, dist, IPA state;
          render the config.  It never mutates the hosts.
  down  = collect logs only; nothing is destroyed.

Ports are probed from the provisioner's vantage point; a failure is a
warning (the provisioner may sit off the environment's network) unless
--strict is set.
"""

import os
import shlex
import socket
import subprocess
import sys
import tarfile

CHECK_PORTS = [22, 53, 88, 389, 636]


class ExternalError(Exception):
    pass


class ExternalProvider:
    def __init__(self, spec, workdir, strict=False, ssh_user=None,
                 ssh_key=None):
        self.spec = spec
        self.workdir = workdir
        self.strict = strict
        self.ssh_user = ssh_user or 'root'
        self.ssh_key = ssh_key
        self.logdir = os.path.join(workdir, 'logs')
        os.makedirs(self.logdir, exist_ok=True)
        self.ok = True
        self._write_ssh_config()

    def resolve_images(self):
        """External hosts run their own IPA (dev build, RPMs, ...); image
        references in the preset are ignored by this provider."""
        return {}

    def ensure_images(self, force=None):
        """No-op: external hosts run their own IPA, so there is no local
        channel image to build (design §3.10). Present so callers can invoke
        it uniformly across providers."""
        return {}

    def ssh_config_path(self):
        return os.path.join(self.workdir, 'ssh_config')

    def _write_ssh_config(self):
        """One Host block per host, named by its FQDN, so `ssh <fqdn>` works
        after `Include <path>` in the operator's ~/.ssh/config."""
        lines = []
        for h in self.spec.hosts:
            lines += [f'Host {h.name}',
                      f'  HostName {h.address}',
                      f'  Port {h.port or 22}',
                      f'  User {self.ssh_user}',
                      f'  IdentityFile {self.ssh_key_path()}',
                      '  BatchMode yes',
                      '  ConnectTimeout 10',
                      '  StrictHostKeyChecking accept-new']
            if h.address == '127.0.0.1':
                # shortcut: a loopback address is a forwarded port to a local
                # nested VM, whose host keys are regenerated per run and whose
                # ports get reused; upgrade if loopback hosts gain a stable key.
                lines += ['  UserKnownHostsFile /dev/null', '  LogLevel ERROR']
        with open(self.ssh_config_path(), 'w') as f:
            f.write('\n'.join(lines) + '\n')
        print(f'== ssh config: {self.ssh_config_path()} '
              '(Include it in ~/.ssh/config to use `ssh <host>`)')

    def _ssh_opts(self, h):
        return ['-F', self.ssh_config_path()]

    def _ssh_args(self, h):
        return ['ssh'] + self._ssh_opts(h) + [h.name]

    def _ssh(self, h, cmd, timeout=15, check=True):
        args = self._ssh_args(h) + [cmd]
        log = os.path.join(self.logdir, f'ext-{h.name}-check.log')
        with open(log, 'a') as lf:
            lf.write(f'+ {cmd}\n')
            lf.flush()
            proc = subprocess.run(args, stdout=subprocess.PIPE,
                                  stderr=subprocess.STDOUT, timeout=timeout)
        out = proc.stdout.decode().strip()
        if check and proc.returncode:
            raise ExternalError(f'{h.name}: ssh failed: {out[:500]}')
        return proc.returncode, out

    def _check_host(self, h):
        print(f'== {h.name} ({h.address}) [{h.role}]')
        try:
            rc, out = self._ssh(h,
                                'hostname -f; '
                                'grep PRETTY_NAME /etc/os-release; '
                                'rpm -q freeipa-server freeipa-server-dns '
                                '2>/dev/null || echo "IPA: not installed"; '
                                'ip -4 -o addr show | head -3')
        except (ExternalError, subprocess.TimeoutExpired) as e:
            print(f'   FAIL: {e}')
            self.ok = False
            return
        lines = out.splitlines()
        fqdn = lines[0] if lines else '?'
        expected = f'{h.name}.{self.spec.domain}'
        host_ok = 'OK' if fqdn.endswith(self.spec.domain) else \
            f'MISMATCH (expected suffix .{self.spec.domain})'
        dist = next((l for l in lines if 'PRETTY_NAME' in l), '?')
        ipa = next((l for l in lines if 'IPA:' in l
                    or 'freeipa-server-' in l or 'freeipa-server-common' in l
                    or 'not installed' in l), '?')
        print(f'   fqdn: {fqdn} {host_ok}')
        print(f'   dist: {dist}')
        print(f'   ipa:  {ipa}')
        # port probe from the provisioner
        ports = []
        for p in CHECK_PORTS:
            try:
                with socket.create_connection((h.address, p), timeout=2):
                    ports.append(f'{p}+')
            except OSError:
                ports.append(f'{p}-')
        print(f'   ports: {", ".join(ports)}')
        bad = [p for p in ports if p.endswith('-')]
        if bad and self.strict:
            self.ok = False
            print(f'   FAIL: unreachable ports {bad} (strict)')

    def up(self):
        """Attach: validate every host, then render the shared config."""
        for h in self.spec.hosts:
            if not h.is_external:
                raise ExternalError(
                    f'external provider: host {h.name} has no address:')
            self._check_host(h)
        if not self.ok:
            raise ExternalError('attach validation failed')

        cfg = self._write_config()
        self._hosts_entries()
        self._framework_key()
        self._scp(self.spec.master,
                  os.path.join(self.workdir, 'ipa-test-config.yaml'),
                  '/root/.ipa/ipa-test-config.yaml')
        print(f'== attached to environment {self.spec.name}')
        return cfg

    def _hosts_entries(self):
        """Hosts reach each other by FQDN (the framework ssh's from the
        master to the replica by name); make every host's name resolvable on all hosts.
        The FQDN must be the first name on the IP line: ipa-replica-install
        reverse-resolves the master's IP and compares against it."""
        for t in self.spec.hosts:
            dom = self.spec.ad_domain if t.is_ad else self.spec.domain
            fqdn = t.name if t.name.endswith('.' + dom) \
                else f'{t.name}.{dom}'
            ip, short = t.ip or t.address, fqdn.split('.')[0]
            for h in self.spec.hosts:
                self._ssh(h, f"sed -i '/^{ip} /d; /[[:space:]]{fqdn}\\([[:space:]]\\|$\\)/d' /etc/hosts && "
                             f"echo '{ip} {fqdn} {short}' >> /etc/hosts")

    def _keygen_cmd(self):
        """Guarded ssh-keygen for the framework key. The type is passed to
        `ssh-keygen -t` unchanged, so the host decides what it supports.
        Default ed25519, or rsa under fips."""
        fk = self.spec.framework_key or {}
        kind = fk.get('type', 'rsa' if self.spec.fips else 'ed25519')
        size = f" -b {fk['bits']}" if 'bits' in fk else ''
        return ('{ [ -f /root/.ipa/framework_key ] || '
                f'ssh-keygen -q -t {shlex.quote(kind)}{size} -N "" -f /root/.ipa/framework_key; }}')

    def _framework_key(self):
        """The framework runs on the master and ssh's to every host as root
        with a key that must exist on the master; generate it there and
        authorize its public key on each host."""
        m = self.spec.master
        self._ssh(m, 'mkdir -p /root/.ipa && ' + self._keygen_cmd())
        _, pub = self._ssh(m, 'cat /root/.ipa/framework_key.pub')
        for h in self.spec.hosts:
            self._ssh(h, 'mkdir -p /root/.ssh && chmod 700 /root/.ssh && '
                      f'grep -qxF "{pub}" /root/.ssh/authorized_keys || '
                      f'echo "{pub}" >> /root/.ssh/authorized_keys')

    def _write_config(self):
        import yaml
        hosts, ad_hosts = [], []
        for h in self.spec.hosts:
            entry = {'name': h.name, 'role': h.role,
                     'ip': h.ip or h.address}
            if h.is_ad:
                # host_type: samba makes the harness drive the DC with
                # samba-tool (SambaADHost) instead of PowerShell
                if h.os == 'samba':
                    entry['host_type'] = 'samba'
                ad_hosts.append(entry)
            else:
                hosts.append(entry)
        domains = [{'name': self.spec.domain, 'type': 'IPA', 'hosts': hosts}]
        if ad_hosts:
            domains.append({'name': self.spec.ad_domain, 'type': 'AD',
                            'hosts': ad_hosts})
        cfg = {
            'admin_name': self.spec.admin_name,
            'admin_password': self.spec.admin_password,
            'debug': False,
            'dirman_dn': 'cn=directory manager',
            'dirman_password': self.spec.dirman_password,
            'domain_level': self.spec.domain_level,
            'dns_forwarder': self.spec.dns_forwarder,
            'root_ssh_key_filename': '/root/.ipa/framework_key',
            'domains': domains,
        }
        path = os.path.join(self.workdir, 'ipa-test-config.yaml')
        with open(path, 'w') as f:
            yaml.safe_dump(cfg, f, default_flow_style=False, sort_keys=False)
        print(f'== wrote {path}')
        return cfg

    def ssh_key_path(self):
        """The framework authenticates with this key; the provisioner
        assumes the operator's key already has root access (attach
        validation proved it with the same identity)."""
        return self.ssh_key or os.path.expanduser('~/.ssh/id_rsa')

    # the daemon log set, identical to Azure CI's collect_logs()
    # (ipatests/azure/scripts/azure-run-base-tests.sh)
    _DAEMON_LOG_TAR = (
        'journalctl -b --no-pager > /tmp/freeipa-env-{n}.journal.log; '
        # daemons in their own journal namespaces (ipacta, ...) are not in `-b`
        'for ns in $(ls -d /var/log/journal/*.* /run/log/journal/*.* '
        '2>/dev/null | sed -E "s/.*\\.//" | sort -u); do '
        'journalctl --namespace $ns -b --no-pager '
        '> /tmp/freeipa-env-{n}.journal.$ns.log; done; '
        'tar --ignore-failed-read -czf - --warning=no-failed-read '
        '/var/log/dirsrv /var/log/httpd /var/log/ipa* '
        '/var/log/krb5kdc.log /var/log/pki /var/log/samba '
        '/var/named/data /tmp/freeipa-env-{n}.journal*.log 2>/dev/null')

    def down(self, collect=True):
        if collect:
            dest = os.path.join(self.logdir, 'collected')
            os.makedirs(dest, exist_ok=True)
            for h in self.spec.hosts:
                host_dir = os.path.join(dest, h.name)
                os.makedirs(host_dir, exist_ok=True)
                tarball = os.path.join(host_dir, f'{h.name}-logs.tar.gz')
                tmp = tarball + '.partial'
                try:
                    cmd = self._ssh_args(h) + [
                        '-T', self._DAEMON_LOG_TAR.format(n=h.name)]
                    with open(tmp, 'wb') as f:
                        subprocess.run(cmd, stdout=f,
                                       stderr=subprocess.DEVNULL,
                                       timeout=300)
                    if os.path.getsize(tmp) > 0:
                        os.replace(tmp, tarball)
                    else:
                        os.unlink(tmp)
                        print(f'   {h.name}: no logs collected')
                except (ExternalError, subprocess.TimeoutExpired) as e:
                    print(f'   {h.name}: log collection skipped ({e})')
                    if os.path.exists(tmp):
                        os.unlink(tmp)
            # the framework collects every host's per-test logs into the
            # controller's $IPA_TESTS_LOGSDIR (=/root/ipa-env/logs); fetch
            # that tree once from the master so the `per-test` category and
            # the HTML report can surface it (the daemon tarballs above do
            # not include it).
            ctrl = self.spec.master
            host_dir = os.path.join(dest, ctrl.name)
            os.makedirs(host_dir, exist_ok=True)
            tree = os.path.join(host_dir, 'ipa-env')
            tmp = tree + '.partial'
            try:
                cmd = self._ssh_args(ctrl) + ['-T',
                    'tar --ignore-failed-read -czf - -C /root ipa-env nosetests.xml '
                    '2>/dev/null']
                with open(tmp, 'wb') as f:
                    subprocess.run(cmd, stdout=f,
                                   stderr=subprocess.DEVNULL, timeout=600)
                if os.path.getsize(tmp) > 0:
                    with tarfile.open(tmp, 'r:gz') as tf:
                        try:
                            tf.extractall(host_dir, filter='data')
                        except TypeError:      # older python, no filter kw
                            tf.extractall(host_dir)
                    print(f'== {ctrl.name}: per-test framework logs -> {tree}')
                else:
                    print(f'   {ctrl.name}: no per-test framework logs '
                          f'(/root/ipa-env absent or empty)')
            except (ExternalError, subprocess.TimeoutExpired, OSError,
                    tarfile.TarError) as e:
                print(f'   {ctrl.name}: per-test log fetch skipped ({e})')
            finally:
                if os.path.exists(tmp):
                    os.unlink(tmp)
        print(f'== environment {self.spec.name}: logs collected, '
              f'hosts left running (external)')

    def show(self):
        for h in self.spec.hosts:
            try:
                _rc, out = self._ssh(h, 'hostname -f')
                state = 'up'
            except (ExternalError, subprocess.TimeoutExpired):
                out, state = 'unreachable', 'down'
            print(f'{h.name:20s} external  {h.address:18s} {out} ({h.role})')

    def collect_logs(self):
        self.down()

    # base-mode support: scp the run script to the controller, exec it.
    def copy_to_controller(self, local, remote):
        h = self.spec.master
        self._scp(h, local, remote)

    def _scp(self, h, local, remote):
        args = ['scp'] + self._ssh_opts(h) + [local, f'{h.name}:{remote}']
        subprocess.run(args, check=True)

    # where ipa-run-tests leaves the report on the controller
    _XUNIT = ('/root/ipa-env/logs/nosetests.xml', '/root/nosetests.xml')

    def clear_xunit(self):
        """Drop a report left by an earlier run, so a run that writes none
        is not mistaken for that one."""
        self._ssh(self.spec.master, 'rm -f ' + ' '.join(self._XUNIT),
                  check=False)

    def fetch_xunit(self, workdir):
        """Copy the controller's report to <workdir>/nosetests.xml."""
        probe = ('for p in %s; do [ -f $p ] && { cat $p; break; }; done'
                 % ' '.join(self._XUNIT))
        try:
            _rc, out = self._ssh(self.spec.master, probe, timeout=60)
        except (ExternalError, subprocess.TimeoutExpired) as e:
            out = ''
            print(f'== xunit fetch failed: {e}', file=sys.stderr)
        if not out:
            print('== no xunit report found (tests produced no '
                  'nosetests.xml; expected when the install failed)',
                  file=sys.stderr)
            return False
        dst = os.path.join(workdir, 'nosetests.xml')
        with open(dst, 'w') as f:
            f.write(out + '\n')
        print(f'== xunit report: {dst}')
        return True

    def run_in_controller(self, cmd, env=None, log='run.log',
                          timeout=None):
        h = self.spec.master
        envprefix = ''.join(f'{k}={shlex.quote(v)} ' for k, v in (env or {}).items())
        args = self._ssh_args(h)
        args[1:1] = ['-t']
        args.append(envprefix + cmd)
        logpath = os.path.join(self.logdir, log)
        with open(logpath, 'wb') as lf:
            lf.write(f'+ {envprefix}{cmd}\n'.encode())
            lf.flush()
            proc = subprocess.Popen(args, stdout=subprocess.PIPE,
                                    stderr=subprocess.STDOUT)
            for line in proc.stdout:
                sys.stdout.buffer.write(line)
                sys.stdout.flush()
                lf.write(line)
            return proc.wait()
