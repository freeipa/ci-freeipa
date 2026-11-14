"""podman provider (B2/B3): environments made of systemd containers.

Faithful to the validated Azure flow
(ipatests/azure/scripts/setup_containers.py + Dockerfiles/docker-compose.yml):
  * dual-stack podman network with built-in DNS
  * systemd as PID 1, booted to multi-user.target before setup
  * per-container /etc/hosts, /etc/hostname, /etc/resolv.conf rewrites
  * controller (master) SSH keypair; public key distributed to all hosts
  * services known to misbehave in containers get a
    ConditionVirtualization=!container override
  * ipa-test-config.yaml mounted at /root/.ipa/ipa-test-config.yaml
"""

import hashlib
import ipaddress
import json
import os
import shlex
import subprocess
import sys
import time

from .envspec import EnvSpec
from .image import channel_tag, is_channel

# Networks without a named subnet get the first free one of these, so two
# environments (or another project's podman network) never collide.
AUTO_V4_SUBNET = '10.89.{}.0/24'
AUTO_V6_SUBNET = '2001:db8:89:{:x}::/64'
RUN_DIR = '/root/freeipa-env-run'
CONFIG_PATH_IN_CONTAINER = '/root/.ipa/ipa-test-config.yaml'
SSH_PRIVKEY_IN_CONTAINER = '/root/.ssh/id_rsa'
SYSTEMD_TIMEOUT_S = 180
IGNORED_SERVICES = ['nis-domainname']


class PodmanError(Exception):
    pass


class PodmanProvider:
    def __init__(self, spec, workdir, tool='podman', seccomp=None,
                 copr=None, ipa_from=None, ipa_packages=None,
                 ipa_add=None):
        self.spec = spec
        self.workdir = workdir
        self._resolved = {}
        self.tool = tool
        self.seccomp = seccomp
        self.copr = copr
        self.ipa_from = ipa_from
        self.ipa_packages = ipa_packages
        self.ipa_add = ipa_add
        self.logdir = os.path.join(workdir, 'logs')
        self.config_path = os.path.join(workdir, 'ipa-test-config.yaml')

    # ------------------------------------------------------------------ util
    def _podman(self, args, log=None, timeout=None, check=True, env=None,
                echo=True):
        cmd = [self.tool] + args
        if log:
            lf = open(os.path.join(self.logdir, log), 'a')
        else:
            lf = sys.stdout
        try:
            if log or echo:
                lf.write(f'+ {shlex.join(cmd)}\n')
                lf.flush()
            proc = subprocess.run(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                timeout=timeout, env={**os.environ, **(env or {})})
        except FileNotFoundError as e:
            raise PodmanError(f'{self.tool} not found: {e}')
        finally:
            if log:
                lf.close()
        out = proc.stdout.decode()
        if echo:
            print(out, file=sys.stderr)
        if check and proc.returncode:
            raise PodmanError(f'command failed ({proc.returncode}): '
                              f'{shlex.join(cmd)}')
        return proc.returncode, out

    def _exec(self, name, cmd, check=True, env=None, timeout=None, log=None):
        args = ['exec', '-i']
        for k, v in (env or {}).items():
            args += ['-e', f'{k}={v}']
        args += [name, '/bin/bash', '-c', cmd]
        return self._podman(args, log=log, timeout=timeout, check=check)

    def _exec_rc(self, name, cmd, **kw):
        rc, _ = self._exec(name, cmd, check=False, **kw)
        return rc

    # --------------------------------------------------------------- images
    def _image_id(self, ref):
        """Local image id for ref, or None if not present."""
        rc, out = self._podman(['image', 'inspect', ref,
                                '--format', '{{.Id}}'],
                               check=False, echo=False)
        return out.strip() if rc == 0 and out.strip() else None

    def ensure_images(self, force=None):
        """Build this spec's channel images locally when they are missing
        (design §3.10, "build the IPA RPMs ourselves").

        The control node already produced an SRPM (``scripts/make-srpms.sh``)
        and named it in the spec's ``build.srpm``; here, on this host, we
        compile it inside the dedicated ``freeipa-ci/build`` image and bake the
        channel ``full`` image from the resulting binary RPMs. Alternatively,
        when the provider has ``ipa_from`` set, the IPA packages are
        installed with dnf from the enabled COPR repos (``'copr'``) or from
        the distribution's own (``'distro'``), with no SRPM. Idempotent:
        channels already present are skipped unless ``force`` (or
        ``build.force`` in the spec) is set, in which case they are rebuilt.
        Explicit (non-channel) image references are not built — they are
        resolved by :meth:`resolve_images`. Returns {ref: (concrete, id)} for
        the channels it touched.
        """
        refs = set()
        for h in self.spec.hosts:
            if h.is_external:
                continue
            ref = self.spec.podman_image(h)
            if is_channel(ref):
                refs.add(ref)
        if not refs:
            return {}
        # No SRPM on the spec: install with dnf (build.sh's default too),
        # from the COPR repos when any is given, else the distribution's
        # own packages.
        ipa_from = self.ipa_from
        if not ipa_from and not (self.spec.build or {}).get('srpm'):
            ipa_from = 'copr' if self.copr else 'distro'
        if force is None:
            force = bool((self.spec.build or {}).get('force', False))
        from .imagemake import ImageBuildError, ensure_channels
        try:
            result = ensure_channels(self.spec, self.workdir, sorted(refs),
                                     tool=self.tool, force=force,
                                     copr=self.copr, ipa_from=ipa_from,
                                     ipa_packages=self.ipa_packages,
                                     ipa_add=self.ipa_add)
        except ImageBuildError as e:
            raise PodmanError(str(e))
        for ref, (concrete, img_id) in result.items():
            print(f'== image ensured: {ref} -> {concrete} ({img_id[:12]})')
        return result

    def resolve_images(self):
        """Resolve the spec's image references to concrete local images.

        A preset reference is either an *abstract build channel*
        (``freeipa-current`` / ``freeipa-next`` / ``freeipa-previous`` —
        mapped to the podman channel tag the build step publishes) or an
        explicit image reference (passed through). The provider, not the
        preset, decides which concrete image a channel means on this
        host. Read-only. Returns {ref: (concrete_tag, image_id)}."""
        resolved = self._resolved
        for h in self.spec.hosts:
            if h.is_external:
                continue
            ref = self.spec.podman_image(h)
            if ref in resolved:
                continue
            imgref = channel_tag(ref)
            img_id = self._image_id(imgref)
            if not img_id:
                if is_channel(ref):
                    raise PodmanError(
                        f'channel {ref} (tag {imgref}) not found on this '
                        f'host; build it with images/build.sh '
                        f'--channel {ref} (or load the saved image '
                        f'tarball) and re-run')
                raise PodmanError(
                    f'image {ref} not found on this host; build it with '
                    f'images/build.sh (or load the saved image tarball) '
                    f'and re-run')
            rc, out = self._podman(['image', 'inspect', img_id,
                                    '--format',
                                    '{{range .RepoTags}}{{.}} {{end}}'],
                                   echo=False)
            tags = out.split()
            # most specific tag on the same image = longest (commit tags
            # like 44-<sha> outrun the channel and rolling dist tags)
            concrete = max(tags, key=len) if tags else imgref
            resolved[ref] = (concrete, img_id)
            print(f'== image: {ref} -> {concrete} ({img_id[:12]})')
        return resolved

    def _run_image(self, h):
        """Concrete image to pass to `podman run` for this host.

        Uses the resolved image (channel/explicit ref -> fully-qualified
        concrete tag), so `podman run` never sees a bare short name like
        `freeipa-current` (which would trigger podman's interactive
        short-name resolution and fail without a TTY). Falls back to the
        raw reference if it was never resolved."""
        ref = self.spec.podman_image(h)
        r = self._resolved.get(ref)
        if r:
            return r[0] or r[1]
        return ref

    # ------------------------------------------------------------- containers
    def _used_subnets(self):
        """{network: podman network name} for every podman network."""
        _rc, out = self._podman(['network', 'ls', '--format', '{{.Name}}'],
                                check=False, echo=False)
        names = out.split()
        if not names:
            return {}
        _rc, out = self._podman(['network', 'inspect'] + names,
                                check=False, echo=False)
        try:
            networks = json.loads(out)
        except ValueError:
            return {}
        used = {}
        for n in networks:
            for s in n.get('subnets') or n.get('Subnets') or []:
                cidr = s.get('subnet') or s.get('Subnet')
                if cidr:
                    used[ipaddress.ip_network(cidr, strict=False)] = (
                        n.get('name') or n.get('Name'))
        return used

    def _choose_subnet(self, named, template, count, used):
        """The subnet to create: `named` if the spec gives one (it must be
        free), else the first free of `template` formatted 0..count-1."""
        def clash(net):
            return next((nm for u, nm in used.items()
                         if u.version == net.version and u.overlaps(net)),
                        None)
        if named:
            other = clash(ipaddress.ip_network(named, strict=False))
            if other:
                raise PodmanError(
                    f'subnet {named} is already used by the podman network '
                    f'{other}; set another subnet_v4/subnet_v6 in the spec')
            return named
        for i in range(count):
            cand = template.format(i)
            if not clash(ipaddress.ip_network(cand)):
                return cand
        raise PodmanError(f'no free subnet left in {template}')

    def _network_args(self):
        used = self._used_subnets()
        args = ['network', 'create']
        args += ['--subnet', self._choose_subnet(
            self.spec.subnet_v4, AUTO_V4_SUBNET, 256, used)]
        if self.spec.ipv6:
            args += ['--subnet', self._choose_subnet(
                self.spec.subnet_v6, AUTO_V6_SUBNET, 256, used)]
        args.append(self.spec.network_name())
        return args

    def _create_network(self):
        rc, _ = self._podman(['network', 'exists', self.spec.network_name()],
                             check=False)
        if rc != 0:
            self._podman(self._network_args(), log='network.log')

    def _container_args(self, h, idx_in_role):
        name = self.spec.container_name(h)
        fqdn = self.spec.host_fqdn(h)
        res = self.spec.resource(h.role)
        args = ['run', '-d',
                '--name', name,
                '--hostname', fqdn,
                '--network', self.spec.network_name(),
                # runc defaults to container=oci; declare the real runtime
                # so /run/systemd/container and the container env var agree
                # (test_ipaplatform/test_tasks.py::test_detect_container).
                '-e', 'container=podman',
                '--cap-add', 'ALL',
                '--cap-drop', 'CAP_SYS_TIME',
                '--security-opt', 'label=disable',
                '--memory', res['memory'],
                '--memory-swap', res['memory-swap']]
        if self.seccomp:
            args += ['--security-opt', f'seccomp={self.seccomp}']
        args += ['-v', f'{self.config_path}:{CONFIG_PATH_IN_CONTAINER}:ro']
        args.append(self._run_image(h))
        return args

    def _run_containers(self):
        for h in self.spec.hosts:
            if h.is_external:
                continue
            self._podman(self._container_args(h, 0), log=f'{h.name}.run.log')

    def _wait_systemd(self):
        for h in self.spec.hosts:
            if h.is_external:
                continue
            name = self.spec.container_name(h)
            deadline = time.time() + SYSTEMD_TIMEOUT_S
            while time.time() < deadline:
                rc = self._exec_rc(name, 'systemctl is-active multi-user.target',
                                   log=f'{h.name}.systemd-wait.log')
                if rc == 0:
                    print(f'== {h.name}: multi-user.target reached')
                    break
                time.sleep(3)
            else:
                raise PodmanError(f'{name}: multi-user.target not reached')

    def _umount(self, name, path):
        self._exec_rc(name, f'umount {path} 2>/dev/null || true')

    def _setup_hosts(self):
        """Per-container /etc/hosts; controller also learns the rest."""
        for h in self.spec.hosts:
            if h.is_external:
                continue
            name = self.spec.container_name(h)
            fqdn = self.spec.host_fqdn(h)
            self._umount(name, '/etc/hosts')
            self._exec(
                name,
                f"echo -e '127.0.0.1 localhost\n::1 localhost\n"
                f"0:0:0:0:0:0:0:1 localhost' > /etc/hosts",
                log=f'{h.name}.setup.log')
        # every container resolves every other host: DNS covers the IPA
        # hosts (Azure kept an extra entry for the controller); external
        # hosts (e.g. AD DCs) get /etc/hosts entries so IPA<->AD name
        # resolution works without AD DNS on the container network
        for h in self.spec.hosts:
            if h.is_external:
                continue
            name = self.spec.container_name(h)
            extra = []
            for o in self.spec.hosts:
                if o.name == h.name:
                    continue
                if o.is_external:
                    extra.append(f'{o.address}\t{o.name}')
                else:
                    extra.append(f'{self._ip(o)}\t{self.spec.host_fqdn(o)}')
            if extra:
                self._exec(name, 'echo -e ' + shlex.quote('\n'.join(extra))
                           + ' >> /etc/hosts', log=f'{h.name}.setup.log')

    def _setup_hostname(self):
        for h in self.spec.hosts:
            if h.is_external:
                continue
            name = self.spec.container_name(h)
            fqdn = self.spec.host_fqdn(h)
            self._umount(name, '/etc/hostname')
            self._exec(name, f"echo -e '{fqdn}' > /etc/hostname"
                             f' && hostnamectl set-hostname {fqdn}',
                       log=f'{h.name}.setup.log')

    def _setup_resolvconf(self):
        # The multihost framework's PlainFileResolver requires
        # /etc/resolv.conf to be a plain regular file with a real first
        # line (an empty or NM/resolved-managed file is rejected), so the
        # master must always get a forwarder; 8.8.8.8 is the framework's
        # own default (env_config.py 'DNSFORWARD').
        fwd = self.spec.dns_forwarder or '8.8.8.8'
        for h in self.spec.hosts:
            if h.is_external:
                continue
            name = self.spec.container_name(h)
            self._umount(name, '/etc/resolv.conf')
            if h.role == 'master':
                ns = fwd
            else:
                # non-masters resolve through the master's DNS
                ns = self._ip(self.spec.master)
            if ns:
                self._exec(name, f"echo -e 'nameserver {ns}' > /etc/resolv.conf"
                              f' && chmod 0644 /etc/resolv.conf',
                           log=f'{h.name}.setup.log')

    def _setup_ssh(self):
        """Controller keypair (PEM RSA, as Azure) + pubkey distribution."""
        cname = self.spec.container_name(self.spec.master)
        self._exec(cname, 'rm -f ' + SSH_PRIVKEY_IN_CONTAINER
                   + ' && ssh-keygen -q -f ' + SSH_PRIVKEY_IN_CONTAINER
                   + ' -t rsa -m PEM -N ""', log='ssh.setup.log')
        _rc, out = self._exec(cname, f'cat {SSH_PRIVKEY_IN_CONTAINER}.pub')
        pubkey = out.strip().splitlines()[0].strip()
        for h in self.spec.hosts:
            if h.is_external:
                continue
            name = self.spec.container_name(h)
            self._exec(
                name,
                'mkdir -p /root/.ssh && chmod 0700 /root/.ssh'
                ' && touch /root/.ssh/authorized_keys && chmod 0600'
                ' /root/.ssh/authorized_keys'
                f' && echo {pubkey} >> /root/.ssh/authorized_keys',
                log=f'{h.name}.setup.log')
        # keep the private key available to the provisioner (framework on
        # the host uses root_ssh_key_filename in the config)
        self._podman(['cp', f'{cname}:{SSH_PRIVKEY_IN_CONTAINER}',
                      os.path.join(self.workdir, 'id_rsa')])
        os.chmod(os.path.join(self.workdir, 'id_rsa'), 0o600)
        self._podman(['cp', f'{cname}:{SSH_PRIVKEY_IN_CONTAINER}.pub',
                      os.path.join(self.workdir, 'id_rsa.pub')])

    def _setup_container_overrides(self):
        for h in self.spec.hosts:
            if h.is_external:
                continue
            name = self.spec.container_name(h)
            for svc in IGNORED_SERVICES:
                d = f'/etc/systemd/system/{svc}.service.d'
                self._exec(
                    name,
                    f'mkdir -p {d} && printf "[Unit]\\n'
                    f'ConditionVirtualization=!container\\n"'
                    f' > {d}/ipa-override.conf',
                    log=f'{h.name}.setup.log')
            self._exec_rc(name, 'systemctl daemon-reload')

    # ------------------------------------------------------------------ config
    def _state(self, name):
        import json
        try:
            _rc, out = self._podman(['inspect', name], check=False,
                                         echo=False)
            return json.loads(out)[0]['State']['Status']
        except Exception:
            return 'not found'

    def _ip(self, h):
        name = self.spec.container_name(h)
        import json
        _rc, out = self._podman(['inspect', name], echo=False)
        net = json.loads(out)[0]['NetworkSettings']['Networks']
        ip = net.get(self.spec.network_name(), {}).get('IPAddress', '')
        if not ip:
            raise PodmanError(f'no IP for {name} on {self.spec.network_name()}')
        return ip

    def write_config(self):
        """Render the multihost config understood by
        ipatests/pytest_ipa/integration/env_config.py (same shape as
        ipatests/azure/templates/ipa-test-config-template.yaml).

        NOTE: pytest_multihost's Host.from_dict rejects unknown keys, so
        the host entries must look exactly like the Azure template:
        external_hostname / name (FQDN) / ip / role -- no 'type' key.
        'type: IPA' belongs on the domain, not the host.

        The framework supports several domains in one config
        (Config.from_dict): one IPA domain plus optional AD / AD_SUBDOMAIN /
        AD_TREEDOMAIN domains for external AD hosts (roles ad,
        ad_subdomain, ad_treedomain -> pytest_ipa WinHost). AD host entries
        take name (AD FQDN) / role / ip only."""
        ad_types = {'ad': 'AD', 'ad_subdomain': 'AD_SUBDOMAIN',
                    'ad_treedomain': 'AD_TREEDOMAIN'}
        domains = []
        ipa_hosts = [h for h in self.spec.hosts
                     if h.role not in ad_types]
        ad_hosts = [h for h in self.spec.hosts if h.role in ad_types]
        domains.append({
            'name': self.spec.domain,
            'type': 'IPA',
            'hosts': [self._host_dict(h) for h in ipa_hosts],
        })
        for role in ('ad', 'ad_subdomain', 'ad_treedomain'):
            group = [h for h in ad_hosts if h.role == role]
            if not group:
                continue
            # AD domain name = FQDN without its first label
            first = group[0].name
            domain_name = first.split('.', 1)[1] if '.' in first else first
            domains.append({
                'name': domain_name,
                'type': ad_types[role],
                'hosts': [{'name': h.name, 'ip': h.address, 'role': h.role}
                          for h in group],
            })
        cfg = {
            'admin_name': self.spec.admin_name,
            'admin_password': self.spec.admin_password,
            'debug': False,
            'dirman_dn': 'cn=Directory Manager',
            'dirman_password': self.spec.dirman_password,
            'domain_level': self.spec.domain_level,
            # the framework's own default is 8.8.8.8; a null forwarder in
            # the YAML would not be picked up the same way
            'dns_forwarder': self.spec.dns_forwarder or '8.8.8.8',
            'root_ssh_key_filename': os.path.join(self.workdir, 'id_rsa'),
            'domains': domains,
        }
        import yaml
        with open(self.config_path, 'w') as f:
            yaml.safe_dump(cfg, f, default_flow_style=False, sort_keys=False)
        print(f'== wrote {self.config_path}')
        return cfg

    def _host_dict(self, h):
        if h.is_external:
            address = h.address
            fqdn = h.name  # external hosts are given as FQDNs
        else:
            address = self._ip(h)
            fqdn = self.spec.host_fqdn(h)
        return {
            'external_hostname': fqdn,
            'name': fqdn,
            'ip': address,
            'role': h.role,
        }

    # ------------------------------------------------------------------ ops
    def up(self):
        os.makedirs(self.logdir, exist_ok=True)
        # build the spec's channel images ourselves (design §3.10) before
        # resolving them; a no-op when the spec has no build.srpm.
        self.ensure_images()
        self.resolve_images()
        # the config file must exist before containers mount it
        open(self.config_path, 'a').close()
        self._create_network()
        self._run_containers()
        self._wait_systemd()
        self._setup_hosts()
        self._setup_hostname()
        self._setup_resolvconf()
        self._setup_ssh()
        self._setup_container_overrides()
        self.write_config()
        self.check_forwarder()
        print(f'== environment {self.spec.name} is up')

    def down(self, collect=True):
        self.collect_logs()
        for h in self.spec.hosts:
            if h.is_external:
                print(f'== {h.name}: external host, leaving in place')
                continue
            name = self.spec.container_name(h)
            self._podman(['stop', '-t', '30', name],
                         log=f'{h.name}.stop.log', check=False)
            self._podman(['rm', name], check=False)
        self._podman(['network', 'rm', self.spec.network_name()], check=False)
        print(f'== environment {self.spec.name} torn down')

    def show(self):
        for h in self.spec.hosts:
            if h.is_external:
                print(f'{h.name:20s} external  {h.address} ({h.role})')
                continue
            name = self.spec.container_name(h)
            try:
                _rc, out = self._podman(['inspect', name], echo=False)
                d = json.loads(out)[0]
                ip = (d['NetworkSettings']['Networks']
                      .get(self.spec.network_name(), {})
                      .get('IPAddress', '?'))
                print(f'{name:20s} {self._state(name):20s} '
                      f'{ip} ({h.role})')
                self._show_provenance(name, d)
            except (PodmanError, KeyError):
                print(f'{name:20s} not found ({h.role})')

    # packages whose exact build decides what a run actually tested
    _PROVENANCE_PACKAGES = ('freeipa-server', 'freeipa-client',
                            'python3-ipatests', 'kuipa', 'kuipa-server',
                            'ahdapa', 'akamu', '389-ds-base', 'pki-server')

    def _show_provenance(self, name, inspected):
        """What the container holds: image labels and installed builds."""
        labels = (inspected.get('Config') or {}).get('Labels') or {}
        for k in sorted(k for k in labels if k.startswith('org.freeipa-ci.')):
            print(f'    {k[len("org.freeipa-ci."):]}: {labels[k]}')
        rc, out = self._podman(
            ['exec', name, 'rpm', '-q', '--qf', '%{NAME}-%{VERSION}-%{RELEASE}\\n']
            + list(self._PROVENANCE_PACKAGES), check=False, echo=False)
        for line in out.splitlines():
            if line and not line.startswith('package '):
                print(f'    {line}')
        if os.path.exists(self._overlay_record()):
            print('    OVERLAY applied: not a packaged run '
                  f'(see {self._overlay_record()})')

    # the daemon log set, identical to Azure CI's collect_logs()
    # (ipatests/azure/scripts/azure-run-base-tests.sh): dirsrv, httpd,
    # ipa*, krb5kdc, pki, samba, bind data, plus the boot journal
    _DAEMON_LOG_TAR = (
        'journalctl -b --no-pager > /tmp/{n}.journal.log &&'
        # daemons in their own journal namespaces (ipacta, ...) are not in `-b`
        ' for ns in $(ls -d /var/log/journal/*.* /run/log/journal/*.*'
        ' 2>/dev/null | sed -E "s/.*\\.//" | sort -u); do'
        ' journalctl --namespace $ns -b --no-pager'
        ' > /tmp/{n}.journal.$ns.log; done &&'
        ' tar --ignore-failed-read -czf /tmp/{n}-logs.tar.gz'
        ' --warning=no-failed-read'
        ' /var/log/dirsrv /var/log/httpd /var/log/ipa*'
        ' /var/log/krb5kdc.log /var/log/pki /var/log/samba'
        ' /var/named/data /tmp/{n}.journal*.log || :')

    def collect_logs(self):
        dest = os.path.join(self.logdir, 'collected')
        os.makedirs(dest, exist_ok=True)
        for h in self.spec.hosts:
            if h.is_external:
                continue
            name = self.spec.container_name(h)
            host_dir = os.path.join(dest, h.name)
            os.makedirs(host_dir, exist_ok=True)
            try:
                rc, out = self._podman(
                    ['exec', name, 'journalctl', '-b', '--no-pager'],
                    check=False, echo=False)
                if rc == 0 and out.strip():
                    jpath = os.path.join(
                        self.logdir, f'collect-{h.name}.journal.log')
                    with open(jpath, 'w') as lf:
                        lf.write(out if out.endswith('\n')
                                 else out + '\n')
                elif rc != 0:
                    print(f'== {h.name}: journal collection skipped '
                          f'(container not running?)')
            except PodmanError:
                pass
            self._collect_journal_namespaces(h, name)
            # daemon logs: Azure-parity tarball
            self._exec_rc(name, self._DAEMON_LOG_TAR.format(n=h.name))
            if self._exec_rc(name, f'[ -s /tmp/{h.name}-logs.tar.gz ]') == 0:
                try:
                    self._podman(
                        ['cp', f'{name}:/tmp/{h.name}-logs.tar.gz',
                         os.path.join(host_dir, f'{h.name}-logs.tar.gz')],
                        check=False, echo=False)
                except PodmanError:
                    pass
            # the workflow tarballs (run-base-tests.sh) are written by the
            # controller host only; the per-test framework tree lives in
            # /root/ipa-env/logs, fetched once after the loop from the master
            # (it is present on the other hosts only as a shared volume).

        master = self.spec.master
        if master and not master.is_external:
            mname = self.spec.container_name(master)
            if self._exec_rc(mname, '[ -d /root/ipa-env ]') == 0:
                self._podman(
                    ['cp', f'{mname}:/root/ipa-env',
                     os.path.join(dest, master.name, 'ipa-env')],
                    check=False, echo=False)
            if self._exec_rc(mname, '[ -e /root/nosetests.xml ]') == 0:
                self._podman(
                    ['cp', f'{mname}:/root/nosetests.xml',
                     os.path.join(dest, master.name, 'nosetests.xml')],
                    check=False, echo=False)

    # ------------------------------------------------------------------ run
    def copy_to_controller(self, local, remote):
        cname = self.spec.container_name(self.spec.master)
        self._podman(['cp', local, f'{cname}:{remote}'])

    def run_in_controller(self, cmd, env=None, log='run.log',
                          timeout=None):
        """Run a command in the master container, streaming output."""
        cname = self.spec.container_name(self.spec.master)
        # -w /root: ipa-run-tests captures $PWD for IPATEST_XUNIT_PATH
        # before chdir-ing into the ipatests package dir
        args = ['exec', '-i', '-w', '/root']
        for k, v in (env or {}).items():
            args += ['-e', f'{k}={v}']
        args += [cname, '/bin/bash', '-euxc', cmd]
        logpath = os.path.join(self.logdir, log)
        with open(logpath, 'wb') as lf:
            lf.write(f'+ {shlex.join(args[1:])}\n'.encode())
            lf.flush()
            proc = subprocess.Popen(
                [self.tool] + args, stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT)
            for line in proc.stdout:
                sys.stdout.buffer.write(line)
                sys.stdout.flush()
                lf.write(line)
            rc = proc.wait()
        return rc

    # ------------------------------------------------- detached test runs
    def start_detached(self, cmd, env=None):
        """Start `cmd` in the master and return at once. Its output goes to
        RUN_DIR/out.log and its exit status to RUN_DIR/rc, which survive the
        controller (a wrapper timeout, a closed terminal, a sleeping host)."""
        cname = self.spec.container_name(self.spec.master)
        state, _rc = self.detached_state()
        if state == 'running':
            raise PodmanError('a detached run is already in progress '
                              '(see `status`)')
        self._exec(cname, f'mkdir -p {RUN_DIR} && '
                          f'rm -f {RUN_DIR}/rc {RUN_DIR}/pid {RUN_DIR}/out.log')
        inner = (f'echo $$ > {RUN_DIR}/pid; '
                 f'/bin/bash -euxc {shlex.quote(cmd)} '
                 f'> {RUN_DIR}/out.log 2>&1; echo $? > {RUN_DIR}/rc')
        args = ['exec', '-d', '-w', '/root']
        for k, v in (env or {}).items():
            args += ['-e', f'{k}={v}']
        args += [cname, '/bin/bash', '-c', inner]
        self._podman(args, echo=False)

    def detached_state(self):
        """('none'|'running'|'lost'|'finished', exit code or None)."""
        cname = self.spec.container_name(self.spec.master)
        rc, out = self._podman(
            ['exec', cname, '/bin/bash', '-c',
             f'if [ -e {RUN_DIR}/rc ]; then echo finished $(cat {RUN_DIR}/rc);'
             f' elif [ -e {RUN_DIR}/pid ] && kill -0 $(cat {RUN_DIR}/pid)'
             f' 2>/dev/null; then echo running;'
             f' elif [ -e {RUN_DIR}/pid ]; then echo lost; else echo none; fi'],
            check=False, echo=False)
        if rc != 0:
            return 'none', None
        parts = out.split()
        if parts and parts[0] == 'finished':
            return 'finished', int(parts[1]) if len(parts) > 1 else None
        return (parts[0] if parts else 'none'), None

    def detached_tail(self, lines):
        cname = self.spec.container_name(self.spec.master)
        _rc, out = self._podman(
            ['exec', cname, 'tail', '-n', str(lines), f'{RUN_DIR}/out.log'],
            check=False, echo=False)
        return out

    def fetch_detached_log(self):
        """Copy the detached run's output next to the other logs."""
        cname = self.spec.container_name(self.spec.master)
        os.makedirs(self.logdir, exist_ok=True)
        self._podman(['cp', f'{cname}:{RUN_DIR}/out.log',
                      os.path.join(self.logdir, 'run.log')],
                     check=False, echo=False)

    # ---------------------------------------------------------- overlay
    def _overlay_record(self):
        return os.path.join(self.workdir, 'overlay.json')

    @staticmethod
    def _sha256(path):
        h = hashlib.sha256()
        with open(path, 'rb') as f:
            for block in iter(lambda: f.read(1 << 20), b''):
                h.update(block)
        return h.hexdigest()

    def overlay(self, pairs, mode=None):
        """Copy local files into every container and verify each by hash.

        `pairs` are (source, destination): a file goes to the destination
        path, a directory's files to the destination directory. The copies
        are recorded in <workdir>/overlay.json so `show` and the summary can
        say the run was not a packaged one."""
        files = []
        for src, dst in pairs:
            src = os.path.abspath(src)
            if os.path.isdir(src):
                for root, _dirs, names in os.walk(src):
                    for n in sorted(names):
                        p = os.path.join(root, n)
                        files.append((p, os.path.join(
                            dst, os.path.relpath(p, src))))
            elif os.path.isfile(src):
                files.append((src, dst))
            else:
                raise PodmanError(f'overlay source {src} does not exist')
        record = []
        for h in self.spec.hosts:
            if h.is_external:
                continue
            name = self.spec.container_name(h)
            for src, dst in files:
                want = self._sha256(src)
                self._podman(
                    ['exec', name, 'mkdir', '-p', os.path.dirname(dst)],
                    echo=False)
                self._podman(['cp', src, f'{name}:{dst}'], echo=False)
                if mode:
                    self._podman(['exec', name, 'chmod', mode, dst],
                                 echo=False)
                _rc, out = self._podman(['exec', name, 'sha256sum', dst],
                                        check=False, echo=False)
                got = out.split()[0] if out.split() else ''
                if got != want:
                    raise PodmanError(
                        f'overlay verification failed on {name}: {dst} is '
                        f'{got or "missing"}, expected {want}')
                record.append({'host': h.name, 'src': src, 'dst': dst,
                               'sha256': want})
                print(f'== {h.name}: {dst} ({want[:12]})')
        previous = []
        if os.path.exists(self._overlay_record()):
            with open(self._overlay_record()) as f:
                previous = json.load(f)
        with open(self._overlay_record(), 'w') as f:
            json.dump(previous + record, f, indent=1)
        return record

    # ------------------------------------------------------ diagnostics
    def _collect_journal_namespaces(self, h, name):
        """Daemons run in their own journal namespaces (ahdapa, akamu, ...):
        `journalctl -b` does not show them, and they die with the container."""
        rc, out = self._podman(
            ['exec', name, '/bin/bash', '-c',
             "ls -d /var/log/journal/*.* /run/log/journal/*.* 2>/dev/null "
             "| sed -E 's/.*\\.//' | sort -u"],
            check=False, echo=False)
        for ns in out.split() if rc == 0 else []:
            rc, jour = self._podman(
                ['exec', name, 'journalctl', '--namespace', ns, '--no-pager'],
                check=False, echo=False)
            if rc == 0 and jour.strip():
                path = os.path.join(
                    self.logdir, f'collect-{h.name}.journal.{ns}.log')
                with open(path, 'w') as lf:
                    lf.write(jour if jour.endswith('\n') else jour + '\n')

    def check_forwarder(self):
        """Warn when the DNS forwarder does not answer: an install with DNS
        fails its forwarder check on a resolver that is only down for the
        moment, which looks like a product failure."""
        fwd = self.spec.dns_forwarder
        master = self.spec.master
        if not fwd or not master or master.is_external:
            return
        name = self.spec.container_name(master)
        probe = (
            "python3 - <<'PY'\n"
            "import sys\n"
            "try:\n"
            "    import dns.resolver as r\n"
            "except ImportError:\n"
            "    sys.exit(3)\n"
            "x = r.Resolver(configure=False)\n"
            f"x.nameservers = ['{fwd}']\n"
            "x.lifetime = 10\n"
            "x.resolve('.', 'SOA')\n"
            "PY")
        for _ in range(2):
            rc, _out = self._podman(['exec', name, '/bin/bash', '-c', probe],
                                    check=False, echo=False)
            if rc in (0, 3):
                return
        print(f'warning: DNS forwarder {fwd} does not answer from {name}; '
              f'installs with --setup-dns will fail their forwarder check. '
              f'Set dns_forwarder to a resolver this host can reach.',
              file=sys.stderr)
