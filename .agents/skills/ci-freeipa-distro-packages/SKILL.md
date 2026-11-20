---
name: ci-freeipa-distro-packages
description: Run FreeIPA test suites against the FreeIPA packages a Fedora release already ships (no worktree, no local build, no COPR repository) with ci-freeipa, locally in podman containers or on Testing Farm, and report results with their provenance.
---

# Test the FreeIPA packages Fedora ships with ci-freeipa

Use this when the request is to test FreeIPA "as in Fedora N", "the packages already in Fedora", or a base/gating suite against a release, and nothing has to be built. For a FreeIPA worktree or packages from a COPR repository use [ci-freeipa-worktree](../ci-freeipa-worktree/SKILL.md) instead. Read `README.md` ("Test the packages Fedora ships") before starting; the tools evolve.
ci-freeipa can be a checkout or an installed package (`python3 -m pip install git+https://github.com/freeipa/ci-freeipa`; README "Install"): the installed `freeipa-env` command works from any cwd and ships the preset catalog, `scripts/` and `images/` inside itself. Where the examples below say `./env/freeipa-env` and `bash images/build.sh`, an install uses just `freeipa-env` and `<CI_ROOT>/images/build.sh` (`CI_ROOT` = `python3 -c "from freeipa_env import paths; print(paths.CI_ROOT)"`). The podman/qemu/ssh/rsync tools are not Python packages; install them separately as the README documents.

This is ci-freeipa's default lane: when no SRPM, RPM directory or COPR repository is named, the image bake runs `dnf install` of every IPA subpackage, including `python3-ipatests`, from the release's `fedora` and `updates` repositories. The tests therefore come from the same release as the packages. `updates-testing` is not enabled. `--ipa-from-distro` states the lane explicitly and refuses `--copr` and `--srpm`. Adding `--copr OWNER/PROJECT` to any command here switches to the COPR's builds (same flow, also on Testing Farm); the result is then no longer the distribution's and `show` must print `source: copr`.

## 1. Scope

- Fix the Fedora release (`dist`: a number or `rawhide`) and the suites. Presets are in `env/presets/` (`azure/` has one file per Azure job, `prci/` the migrated PRCI jobs), queues in `queues/`. The "base" suite is `presets/azure/base.yaml` (unit tests and `test_dns_plugin.py`) and `presets/base-xmlrpc.yaml`; `queues/azure.yaml` is base plus gating.
- Choose where it runs: local podman for one or two presets and for debugging, Testing Farm for a whole queue or when the host must stay free.
- Preserve existing images, containers and networks. `podman images` shows what the host holds: `freeipa-ci/full:current` is usually someone's worktree or COPR build.

## 2. Local run

Bake a dedicated image. Do not reuse the `freeipa-ci` tag: a channel image that is already present is never rebuilt, so `up` on a host holding `freeipa-ci/full:current` runs the tests against that image without any error, and `ensure --build` would replace it. The command is run from the checkout root; from an installed package it is `<CI_ROOT>/images/build.sh` (the script is cwd-independent, and `ensure` bakes missing channel images through it).

```sh
bash images/build.sh --dist 45 --tag freeipa-distro
```

This produces `freeipa-distro/full:45` (about two minutes with a warm base). It is a snapshot of the repositories; run the command again to pick up newer updates. Add packages the default list lacks with `--add-ipa-package SPEC`.

Write an env file outside the repository, or copy a preset and change `dist:` and `image:` (catalog presets say `dist: 44` and `image: freeipa-current`):

```yaml
name: distro45-base
provider: podman
domain: ipa.test
dist: 45
image: freeipa-distro/full:45
resources:
  master: {memory: 3500m, memory-swap: 4500m}
hosts:
  - {role: master, name: master1}
run:
  mode: base
  setup_dns: true
  setup_kra: true
  forwarder: 8.8.8.8
  tests: [test_cmdline, test_custodia, test_install, test_ipaclient,
          test_ipalib, test_ipaplatform, test_ipapython, test_ipaserver,
          test_ipatests_plugins, test_xmlrpc/test_dns_plugin.py]
  ignore: [test_ipaserver/test_migratepw.py]
  args: ['-k', 'not test_dns_soa']
```

Give the master 3500m: the Azure default of 1800m gets the Dogtag JVM OOM-killed during the CA install. Leave the subnets out and `up` picks free ones. Then, with the same env file and workdir for every command:

```sh
./env/freeipa-env check ENV.yaml
./env/freeipa-env up    ENV.yaml --workdir WORKDIR
./env/freeipa-env show  ENV.yaml --workdir WORKDIR   # must print "source: distro"
./env/freeipa-env run   ENV.yaml --workdir WORKDIR   # --detach, then status / wait, for long suites
./env/freeipa-env summary ENV.yaml --workdir WORKDIR
./env/freeipa-env down  ENV.yaml --workdir WORKDIR
```

Before trusting a result, check `show`: the image label must be `source: distro`, with no `copr` label, and the `freeipa-server` build must be the release's (`.fcN`). A different source means the env resolved another image.

Multihost and long suites behave as described in the worktree skill: integration classes install and uninstall their own topology, state leaks between classes in one run (`reset` gives fresh containers), and `run --detach` survives the calling process.

## 3. Testing Farm run

Nothing but this repository is involved: the request runs its own plan `tmt/plans/distro.fmf`, and the guest bakes the channel images for its own release, so the compose selects the Fedora under test.

```sh
./env/freeipa-env queue run queues/azure.yaml --runner testing-farm \
    --tf-compose Fedora-45 [--jobs azure-base] [--dry-run]
```

- `TESTING_FARM_API_TOKEN` must be set (or pass `--tf-token`). Never print it.
- The guest clones this repository at `--tf-repo-url`/`--tf-ref`, by default this checkout's `origin` and current branch. That ref must be pushed and must contain `tmt/plans/distro.fmf`; local uncommitted changes do not reach the guest. Pushing is the user's decision: ask first. From an installed package there is no checkout to default from (`_data` has no git origin): pass `--ipa-from-distro --tf-repo-url URL --tf-ref REF` explicitly, and the queue file's absolute path into `freeipa_env/_data/queues/` (or a queue of your own). Without `--ipa-from-distro`, the explicit `--tf-repo-url` selects the `freeipa-env` plan (the build-a-repo lane) instead of the distro plan; with no flags at all the request fails with "has no origin remote".
- `--dry-run` prints the request; check `"name": "/tmt/plans/distro"`, the compose and `FREEIPA_IPA_FROM_DISTRO`.
- `--jobs NAME` filters the queue by job key; `--job-timeout` is per preset.
- Collect with `./env/freeipa-env tf-logs REQUEST_ID --html` (no token needed); one workdir and one `results.html` per preset.

Do not pass `--freeipa-src` or `--tf-repo-url` here: either one means "build that FreeIPA repo" (to use another clone of ci-freeipa, add `--ipa-from-distro`). A queue on a host runner (`--runner local` or `USER@HOST`) bakes missing channel images the same way, but for dist 44 only. For another release bake the channel image first with `images/build.sh --dist N`; that overwrites the host's `freeipa-ci/full:current`, so only do it on a host where that is acceptable.

## 3b. Two releases in one run (nested VMs)

When hosts must run different Fedora releases, use the nested qemu provider. Each host is one VM. `presets/nested-qemu-mixed.yaml` puts `master1` on F44 and every other host on F45. `vm.guest_packages: [ipa-distro]` installs each VM's own release packages, including `firewalld`.

```sh
freeipa-env blend presets/prci/nightly-latest-testing/customized_ds_config_install.yaml \
    presets/nested-qemu-mixed.yaml --name mixed-cds
freeipa-env up  ~/.cache/freeipa-ci/generated/mixed-cds.yaml
freeipa-env run ~/.cache/freeipa-ci/generated/mixed-cds.yaml
```
`blend` resolves catalog names (`presets/...`) against the checkout's `env/` or the installed package's `_data/env/`, so the same command works from both; `up`/`run` need the generated file's real path (`~/.cache/freeipa-ci/generated/`), which is also where an installed package writes it.

- Check each VM's release before trusting a result: `freeipa-server-*.fc44` on the master, `.fc45` on the replica. A mismatch means the image is wrong.
- The job deadline of the calling tool can cut a run short; run the test without one.
- Setup details (framework SSH key, `framework_key` settings, `/etc/hosts`) are in the README under "Framework SSH key" and "Example: master on Fedora 44, replica on Fedora 45".

## 4. Diagnose and report

- A failure here is a finding about the distribution, not about a patch: before reporting it, rule out the environment (DNS forwarder warning from `up`, memory limits, leaked state from an earlier class). Use `freeipa-env logs ENV.yaml --test PART --file ipaserver-install.log --host master1` for one installer log.
- Report the Fedora release, the bake time and the installed builds from `show` (`freeipa-server`, `389-ds-base`, and the rest it prints), the suites with pass/fail/skip counts, each failure with its cause, and the artifact paths (`WORKDIR/nosetests.xml`, `WORKDIR/logs`, or the TF request id and artifacts URL).
- Tear down only what this task created: `down` for the env, and remove the dedicated image when it is no longer wanted (`podman rmi freeipa-distro/full:45 freeipa-distro/full:current freeipa-distro/base:45`). `images/prune.sh` removes dangling layers left by rebakes.
