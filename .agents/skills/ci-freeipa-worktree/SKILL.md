---
name: ci-freeipa-worktree
description: Investigate or develop FreeIPA changes using ci-freeipa to build the current worktree, provision IPA hosts, run integration suites, diagnose failures, and retain evidence.
---

# Test a FreeIPA worktree with ci-freeipa

Use this when a request involves investigating, developing, or validating FreeIPA changes against a real master, replica, client, or other multihost topology. ci-freeipa is a separate checkout from the FreeIPA worktree. Read its `README.md`, the relevant `env/presets/` examples, and the selected FreeIPA tests before choosing a workflow; the tools and image contents evolve.
ci-freeipa itself is used as a checkout (the form in these examples) or as an installed package: `python3 -m pip install git+https://github.com/freeipa/ci-freeipa` — or `pip install .` from a checkout — installs a `freeipa-env` command that works from any cwd (README "Install"). An installed package carries the preset catalog, `scripts/` and `images/` inside itself, under `freeipa_env/_data`; `python3 -c 'from freeipa_env import paths; print(paths.CI_ROOT)'` prints that tree root. `up`/`run`/`queue run` take real file paths in both modes; only `blend` also resolves catalog names (`presets/...`).

To test the unmodified packages a Fedora release ships (no worktree, no COPR repository), use [ci-freeipa-distro-packages](../ci-freeipa-distro-packages/SKILL.md) instead.

## 1. Scope the experiment

- Identify the exact FreeIPA worktree and branch, including tracked modifications and untracked files, submodules, dependencies, and tests. Do not assume a published branch or existing image contains local changes. Preserve user changes and existing containers, images, networks, and workdirs.
- Pick one or two relevant suites or test classes. Inspect their `IntegrationTest` subclasses: `num_replicas`, `num_clients`, `topology`, and `install`/`uninstall` determine actual host requirements and whether fixtures install and destroy the realm. A negative test may need its own client. Do not preinstall hosts if the chosen suite owns their installation.
- Choose a unique environment name, workdir, and unoccupied IPv4/IPv6 subnets. Size master and replica for the workload; Dogtag can exhaust small container limits. Choose a distro supported by the selected image and dependencies. Test credentials and log files are sensitive: use disposable credentials and restrict workdir access.
- For bugs, reproduce with the real integration path before editing when practical. Identify all affected code, tests, configuration, packaging, and service permissions; fix the root cause, then rerun the relevant path.

## 2. Put the current code in the image

**Preferred: build the worktree's RPMs.** This covers installed paths, dependencies, systemd/D-Bus configuration, and packaging. The workflow is `scripts/make-srpms.sh <source>` followed by `images/build.sh --srpm <file-or-directory>`. `images/build.sh` options are documented in its header; it does not implement `--help`.

The SRPM helper initializes submodules, generates build files, and runs `git checkout po/*.po`. Never run it blindly on a user's worktree: that checkout can discard translation changes. Use an independent staging checkout with a separate Git directory, copy the complete current tree including needed untracked files, and preserve/inspect modified submodules and ignored build inputs explicitly. Verify that the resulting SRPM contains the actual changed files, particularly new tests and files needed by packaging; copying an untracked file alone does not guarantee it is included in a distribution tarball. Do not claim the worktree was tested until installed contents match it.

Build to a **unique image repository**, not the user's existing channel tags. `images/build.sh` is cwd-independent, so only its path differs between modes: `./images/build.sh ...` from the ci-freeipa checkout root, or `<CI_ROOT>/images/build.sh ...` when ci-freeipa is an installed package (`CI_ROOT` = `python3 -c "from freeipa_env import paths; print(paths.CI_ROOT)"`). For example, after producing the verified SRPM:

```sh
./images/build.sh --srpm /absolute/path/to/srpms --dist 45 \
  --tag freeipa-ci-worktree --channel current
```

Use `image: freeipa-ci-worktree/full:current` in the env YAML. Adjust the distro and, when required, use the documented `--copr`, `--pin`, or `--builddep` options for the branch's actual dependencies; do not assume a particular COPR or package version. A normal `freeipa-current` image is reused when present, so `up` alone is not proof of freshness. Avoid retagging or deleting shared images to force a build.

**Packages already built in COPR** (no local build): bake into a unique tag, naming the packages the default list lacks with `--add-ipa-package` (it extends the default list; `--ipa-packages` would replace it):

```sh
bash images/build.sh --dist 45 --ipa-from-copr --copr OWNER/PROJECT --channel current \
  --tag my-worktree-ci --add-ipa-package kuipa --add-ipa-package kuipa-server
```

Use `image: my-worktree-ci/full:current` in the env YAML. A rebuild re-points the tag and leaves the old image dangling; `images/prune.sh` removes those and stale buildah working containers. To inspect an image, use `podman run --rm --entrypoint rpm <image> -q ...`: the image entrypoint is systemd init, so a plain `podman run <image> sh -c ...` hangs. Before relying on the image, check that it holds the intended builds: `freeipa-env show` prints the image labels (source, COPR, package list, build time) and the installed `freeipa-server`, `kuipa`, `ahdapa`, `akamu`, `389-ds-base`, and `pki-server` builds for each container.

**Alternative: explicit overlay**, for focused investigation when a full build is unavailable or disproportionate. First compare the image's FreeIPA revision with the worktree baseline, then include both committed differences and tracked/untracked local changes relevant to the experiment. Copy to the installed locations on every affected host: Python modules, test modules, generated config, executable helpers, policies, and other runtime files. Reload or restart only the services needed for the copied configuration. Check the installed content and test discovery before running. An overlay does **not** validate the RPM build, dependency declarations, file ownership, SELinux labels, or all deployment behavior; report that limit. Use a full RPM build for packaging, native code, install scripts, or security-label changes.

Use `freeipa-env overlay <env.yaml> SRC:/DST ...` to copy files; it verifies each copy by SHA-256 on every host and records them, after which `show` and the run summary flag the run as an overlay run. Sources must be final files, not `*.in` templates. Do not copy with `podman cp` or `podman exec` by hand: a copy that silently did nothing (a heredoc without `-i`) costs a whole run.

## 3. Provision and run

Create a dedicated env YAML using the repository's documented schema and the host counts the selected tests require. Example for a replication suite (replace the image, distro, subnets and test selection):

```yaml
name: worktree-replication
provider: podman
domain: ipa.test
dist: 45
image: freeipa-ci-worktree/full:current
subnet_v4: 10.239.180.0/24
subnet_v6: 2001:db8:efb4::/64
resources:
  master: {memory: 3500m, memory-swap: 4500m}
  replica: {memory: 3500m, memory-swap: 4500m}
hosts:
  - {role: master, name: master1}
  - {role: replica, name: replica1}
  - {role: client, name: client1}
run:
  mode: integration
  tests: [test_integration/test_simple_replication.py]
```

Validate the preset with `env/freeipa-env check <env.yaml>` and check actual subnet availability before `up`. From the ci-freeipa root, use the **same** env and workdir for each command:

```sh
./env/freeipa-env up /absolute/path/env.yaml --workdir /absolute/path/workdir
./env/freeipa-env run /absolute/path/env.yaml --workdir /absolute/path/workdir
```

Choose the tests without editing the file with `run --tests file.py::Class` (repeatable). Backup, restore and replica classes take 30 minutes or more, and a controller timeout or a sleeping host ends the command while the tests keep running in the master, so start long runs with `run --detach`, follow them with `status`, and collect with `wait`, which fetches `nosetests.xml` and prints one line per test with the cause of each failure. Several classes in one run share the containers and leaked state carries over; use `reset` (fresh containers, old logs kept as `logs.<timestamp>`) when an independent result is needed. `up` warns when the DNS forwarder does not answer: an install failing the forwarder check on a transient resolver outage is not a product failure; check it before diagnosing. `list` and `prune` show and remove leftover environments.

In integration mode, `run` invokes the installed `ipa-run-tests`; the test framework, not merely container startup, sets up the services specified by the test class. Ensure new test modules are installed or explicitly overlaid where the controller can collect them. In base mode, the CI workflow installs the master and runs the specified base suite. Inspect actual test results and exit status; collection alone, skipped tests, or containers reaching `multi-user.target` are not passing integration tests. Exercise changed behavior with a live smoke scenario in addition to focused tests, including failures and secret-handling boundaries where relevant.

## 4. Diagnose, preserve, and hand off

- Keep the run output, xUnit (`<workdir>/nosetests.xml` when fetched), HTML/per-test logs on the controller (`/root/ipa-env/logs`), and installer/service logs. Use `freeipa-env logs` and `report` as documented; `down` collects logs before removing the provisioned environment. Do not expose enrollment strings, OTPs, keys, or credentials in reports.
- For service failures, inspect the relevant install log and `systemctl status`/`journalctl`; systemd log namespaces can require `journalctl --namespace=<name>`. Check DNS, permissions, actual image revision and installed files before attributing failure to code. Test teardown can leave service state behind; reinstalling into the same containers may fail on stale encrypted state. Use newly provisioned disposable containers rather than deleting unknown data or hiding the error.
- Many integration suites uninstall their topology. If the user requested a retained usable environment, create or restore it after the tests, verify master/replica services and an enrolled client's authentication, and say whether each host is configured or merely running. Keep test results from the original run distinct from any subsequently recreated environment. Report DNS warnings and any services not checked.
- Otherwise, tear down only the environment created for this task using `env/freeipa-env down <env.yaml> --workdir <workdir>`. Never destroy an existing or user-requested persistent environment. Give exact artifact paths, suite counts and failures, source/image provenance, what was actually exercised, and any overlay or packaging limitations.
- Report the provenance of every result: image tag, installed package builds from `show`, and whether `overlay` was used. A result from an overlay run is not a packaged result.
- To read one installer log for one test and host, use `freeipa-env logs <env.yaml> --test <part of the test name> --file ipaserver-install.log --host replica1` instead of walking the collected tree. Per-daemon journal namespaces (ahdapa, akamu, ...) are collected as `collect-<host>.journal.<namespace>.log`; the plain boot journal does not contain them.
