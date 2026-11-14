#!/bin/bash
# build.sh (A3) — build the freeipa-ci base and full images.
#
# Usage:
#   images/build.sh --rpms /path/to/rpms --sha 962ac6d0e [options]
#   images/build.sh --srpm /path/to/dist/srpms [options]
#   images/build.sh --ipa-from-copr --copr owner/project [options]
#   images/build.sh [--ipa-from-distro] [options]
#
# Options:
#   --rpms DIR        Directory containing IPA dev-build RPMs (host-arch or
#                     --distarch + noarch, any layout).
#   --srpm PATH       Either a path to a single freeipa-*.src.rpm or a
#                     directory containing them (e.g. dist/srpms from
#                     `make srpms`). Builds the IPA binary RPMs ourselves
#                     inside the dedicated freeipa-ci/build image (base + all
#                     BuildRequires), then bakes the full image from them.
#                     Mutually exclusive with --rpms.
#   --builddep        With --srpm: run `dnf -y builddep <srpm>` inside the
#                     build image before rpmbuild, installing any
#                     BuildRequires the baked image lacks. For building a
#                     spec that is itself under test (a dist-git PR whose
#                     BuildRequires may differ from the image's list); a
#                     no-op when the image already covers the spec.
#   --sha SHORT_SHA   Git SHA of the IPA build to select from --rpms
#                     (matches the 'git<sha>' version fragment). Omit to
#                     pick the newest per package.
#   --dist N          Fedora dist tag, default 44 (image tags:
#                     <tag>/full:<dist>-<sha> and <tag>/full:<dist>). The
#                     base image is built from fedora-toolbox:<dist>;
#                     `rawhide` is accepted (rolling toolbox tag) and is
#                     what the dist-git lanes pass when the guest is rawhide.
#   --channel NAME    Build channel to publish, default current. Tags the
#                     built image as <tag>/full:<channel> (the abstract
#                     channel presets reference via `image: freeipa-<channel>`).
#                     One of: current, next, previous.
#   --tag NAME        Image repo name, default freeipa-ci.
#   --exclude GLOB    Skip files matching GLOB (repeatable). Debuginfo and
#                     debugsource are excluded by default.
#   --pin SPECS       'NVR ...' specs installed before the IPA RPMs
#                     (repo pinning, see full/Dockerfile).
#   --copr OWNER/PROJECT
#                     Additional COPR repository to enable before installing
#                     anything (repeatable). Each becomes a dnf repo pointing
#                     at copr.fedorainstantcloud.org for the active distro, so
#                     the full image (IPA RPM + dependency installs) and the
#                     the SRPM build image (BuildRequires installs) can pull
#                     non-distro packages. This customizes channel *creation*
#                     without touching any preset (channel *use* stays the
#                     abstract `image: freeipa-<channel>` reference).
#   --ipa-from-copr   Install the IPA packages from the enabled COPR
#                     repositories instead of building them from a local
#                     SRPM/RPM set. Requires at least one --copr repo. No
#                     --rpms/--srpm is used: the full image is baked with
#                     `dnf -y install <IPA packages>` against the enabled
#                     COPR repos (IPA itself and its dependencies all come
#                     from COPR), so this lane never runs rpmbuild. The
#                     freshness model is unchanged: the channel image is
#                     only built if its tag is absent (the caller decides
#                     whether to invoke build.sh at all).
#   --ipa-from-distro Install the IPA packages the distribution itself ships
#                     (the `fedora`/`updates` repos of --dist): the same
#                     `dnf -y install <IPA packages>` bake as
#                     --ipa-from-copr, with no COPR repo, no SRPM and no
#                     rpmbuild. For testing a Fedora release as released.
#                     This is the DEFAULT when none of --rpms, --srpm and
#                     --copr is given (--copr alone means --ipa-from-copr).
#                     Excludes all three: with a COPR
#                     enabled dnf would prefer its builds and the image
#                     would no longer be the distribution's.
#   --ipa-packages SPECS
#                     Space-separated dnf specs to install in
#                     --ipa-from-copr/--ipa-from-distro mode. The default is
#                     every IPA subpackage (the full
#                     server incl. freeipa-server-dns -- the `run` lane
#                     installs with --setup-dns -- plus the client, the
#                     python3-* bits and python3-ipatests, the package the
#                     `run` lane executes); the run lane needs all of it,
#                     and a bare `freeipa-server` would leave out e.g. the
#                     integrated-DNS subpackage. Override to bake a narrower
#                     image.
#   --add-ipa-package SPEC
#                     Extra dnf spec added to that default list (repeatable),
#                     so the list never has to be copied.
#   --distarch ARCH   Target architecture, e.g. aarch64; passed to the
#                     container tool as --platform. Default: none (build for
#                     the host platform).
#
# Example (validation host):
#   images/build.sh --rpms /root/rpmbuild/RPMS --sha 962ac6d0e \
#       --exclude freeipa-server-trust-ad --exclude freeipa-client-samba
#
# Build the IPA RPMs ourselves from a `make srpms` SRPM (control node ships
# the SRPM to the runner; the runner bakes the channel image from it):
#   images/build.sh --srpm /root/freeipa/dist/srpms --channel current
set -euo pipefail

RPMS=
RPMS_OUT=
BUILD_ONLY=0
SRPM=
SHA=
DIST=44
TAG=freeipa-ci
CHANNEL=current
EXCLUDES=()
EXCLUDES+=('*debuginfo')
EXCLUDES+=('*debugsource')
PINS=
COPR_REPOS=
IPA_FROM=    # copr | distro: install the IPA with dnf instead of local RPMs
BUILDEP=0
ADD_IPA_PACKAGES=
# Every IPA subpackage (from freeipa.spec.in, minus the source-only
# `freeipa` meta, which has no binary RPM in COPR): the full server
# (incl. freeipa-server-dns, required by the --setup-dns install), the
# client, the python3-* bits and python3-ipatests. Resolved entirely from
# the enabled COPR repos (noarch + arch), so this never under-resolves.
IPA_PACKAGES="freeipa-server freeipa-server-common freeipa-server-dns \
    freeipa-server-encrypted-dns freeipa-server-trust-ad freeipa-client \
    freeipa-client-common freeipa-client-encrypted-dns freeipa-client-samba \
    freeipa-client-epn freeipa-common freeipa-python-compat freeipa-selinux \
    freeipa-selinux-nfast freeipa-selinux-luna python3-ipaserver \
    python3-ipaclient python3-ipalib python3-ipatests"
TOOL=podman
DISTARCH=
while [[ $# -gt 0 ]]; do
    case "$1" in
        --rpms) RPMS="$2"; shift 2 ;;
        --rpms-out) RPMS_OUT="$2"; shift 2 ;;
        --build-only) BUILD_ONLY=1; shift ;;
        --srpm) SRPM="$2"; shift 2 ;;
        --sha) SHA="$2"; shift 2 ;;
        --dist) DIST="$2"; shift 2 ;;
        --channel) CHANNEL="$2"; shift 2 ;;
        --tag) TAG="$2"; shift 2 ;;
        --exclude) EXCLUDES+=("$2"); shift 2 ;;
        --pin) PINS="$2"; shift 2 ;;
        --copr) COPR_REPOS="${COPR_REPOS:+$COPR_REPOS }$2"; shift 2 ;;
        --ipa-from-copr|--ipa-from-distro)
            [[ -z "$IPA_FROM" || "$IPA_FROM" == "${1#--ipa-from-}" ]] || {
                echo "--ipa-from-copr and --ipa-from-distro are mutually exclusive" >&2
                exit 2; }
            IPA_FROM="${1#--ipa-from-}"; shift ;;
        --builddep) BUILDEP=1; shift ;;
        --ipa-packages) IPA_PACKAGES="$2"; shift 2 ;;
        --add-ipa-package) ADD_IPA_PACKAGES="${ADD_IPA_PACKAGES:+$ADD_IPA_PACKAGES }$2"; shift 2 ;;
        --tool) TOOL="$2"; shift 2 ;;
        --distarch) DISTARCH="$2"; shift 2 ;;
        *) echo "unknown option: $1" >&2; exit 2 ;;
    esac
done

# No local source named: install with dnf, from the COPR repos when any is
# given, else the distribution's own packages.
if [[ -z "$IPA_FROM$RPMS$SRPM" ]]; then
    IPA_FROM=distro
    [[ -n "$COPR_REPOS" ]] && IPA_FROM=copr
fi
if [[ -n "$IPA_FROM" && ( -n "$RPMS" || -n "$SRPM" ) ]]; then
    echo "--ipa-from-$IPA_FROM is mutually exclusive with --rpms/--srpm" >&2
    exit 2
fi
if [[ -n "$RPMS" && -n "$SRPM" ]]; then
    echo "--rpms and --srpm are mutually exclusive" >&2; exit 2
fi
if [[ -n "$ADD_IPA_PACKAGES" && -z "$IPA_FROM" ]]; then
    echo "--add-ipa-package only applies with --ipa-from-copr/--ipa-from-distro" >&2; exit 2
fi
# Extra specs extend the default list, so a caller never has to copy it.
[[ -n "$ADD_IPA_PACKAGES" ]] && IPA_PACKAGES="$IPA_PACKAGES $ADD_IPA_PACKAGES"
case "$IPA_FROM" in
    copr)
        [[ -n "$COPR_REPOS" ]] || {
            echo "--ipa-from-copr requires at least one --copr repo" >&2; exit 2; } ;;
    distro)
        [[ -z "$COPR_REPOS" ]] || {
            echo "--ipa-from-distro excludes --copr (use --ipa-from-copr)" >&2; exit 2; } ;;
esac
command -v "$TOOL" >/dev/null || { echo "$TOOL not found" >&2; exit 2; }

# Target architecture: empty = the host platform (no --platform flag).
PLATFORM_ARGS=()
[[ -n "$DISTARCH" ]] && PLATFORM_ARGS=(--platform "linux/$DISTARCH")

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
WORK="$(mktemp -d /tmp/freeipa-ci-build.XXXXXX)"
trap 'rm -rf "$WORK"' EXIT

BASE_IMAGE="$TAG/base:$DIST"
FULL_IMAGE="$TAG/full:$DIST"
SHA_TAG=""
[[ -n "$SHA" ]] && SHA_TAG="$DIST-$SHA"

if [[ "${BASE:-0}" != "1" ]]; then
    echo "==> Building base image $BASE_IMAGE"
    "$TOOL" build -f "$SCRIPT_DIR/base/Dockerfile" -t "$BASE_IMAGE" \
        ${PLATFORM_ARGS[@]+"${PLATFORM_ARGS[@]}"} \
        --build-arg "DIST=$DIST" "$SCRIPT_DIR/base"
fi

# --- select the IPA RPM set (newest per package, scoped to --sha) ----------
# Delivered-RPM path only; --srpm builds the binary RPMs itself (below).
if [[ -n "$RPMS" ]]; then
mkdir -p "$WORK/rpms"
EXCLUDES_NEWLINE="$(printf '%s\n' "${EXCLUDES[@]}")"
export EXCLUDES_NEWLINE
python3 - "$RPMS" "$SHA" "$WORK/rpms" <<'PYEOF'
"""Select RPMs: newest version per package, scoped to --sha.

An rpm filename is <name>-<version>-<release>.<arch>.rpm where the
package name may itself contain dashes; the version field starts with a
digit, so the earliest dash-before-a-digit splits name from version.
"""
import fnmatch, os, re, shutil, sys

rpms_dir, sha, out_dir = sys.argv[1], sys.argv[2], sys.argv[3]
excludes = [l for l in os.environ['EXCLUDES_NEWLINE'].splitlines() if l]

NAME_VER_RE = re.compile(r'^(.*?)-(\d.*)\.[a-z0-9_]+\.rpm$')

def ver_key(fname):
    m = NAME_VER_RE.match(fname)
    if not m:
        return []
    return [int(x) if x.isdigit() else x
            for x in re.split(r'[.+\-]', m.group(2))]

candidates = {}
for root, _dirs, files in os.walk(rpms_dir):
    for fname in files:
        m = NAME_VER_RE.match(fname)
        if not m:
            continue
        pkg = m.group(1)
        if any(fnmatch.fnmatch(pkg, pat) or fnmatch.fnmatch(fname, pat)
               for pat in excludes):
            continue
        if sha and f'git{sha}' not in fname and f'git{sha[:7]}' not in fname:
            continue
        cur = candidates.get(pkg)
        if cur is None or ver_key(fname) > ver_key(cur[0]):
            candidates[pkg] = (fname, os.path.join(root, fname))

for pkg in sorted(candidates):
    fname, path = candidates[pkg]
    shutil.copy2(path, os.path.join(out_dir, fname))
    print(f'{pkg}: {fname}', file=sys.stderr)
print(f'selected {len(candidates)} packages', file=sys.stderr)
PYEOF

fi

# --- "build the IPA RPMs ourselves" lane: SRPM -> binary RPMs ---------------
# Instead of consuming RPMs delivered by a build farm, compile the SRPM (from
# `make srpms`) inside the dedicated build image, then let the full-image build
# below install the freshly built RPMs. Build and test share one distro repo
# snapshot per build, so a delivered-RPM dependency (e.g. samba) cannot drift
# out of resolution in the test image.
if [[ -n "$SRPM" ]]; then
    case "$SRPM" in
        *.src.rpm) SRPM_FILE="$SRPM" ;;
        *)
            [[ -d "$SRPM" ]] || { echo "--srpm: not a file or directory: $SRPM" >&2; exit 2; }
            SRPM_FILE="$(find "$SRPM" -name '*.src.rpm' | sort | tail -n 1)"
            [[ -n "$SRPM_FILE" ]] || { echo "--srpm: no .src.rpm found under $SRPM" >&2; exit 2; }
            ;;
    esac
    SRPM_DIR="$(dirname "$SRPM_FILE")"
    echo "==> Building IPA binary RPMs from $(basename "$SRPM_FILE")"

    # Dedicated build image (base + BuildRequires), baked once, not per build.
    BUILD_IMAGE="$TAG/build:$DIST"
    need_build=0
    [[ "${BASE:-0}" != "1" ]] && need_build=1
    if (( need_build )) && [[ -z "$COPR_REPOS" ]] && \
        "$TOOL" image inspect "$BUILD_IMAGE" >/dev/null 2>&1; then
        need_build=0
    fi
    if (( need_build )); then
        echo "==> Building build image $BUILD_IMAGE${COPR_REPOS:+ (COPR: $COPR_REPOS)}"
        BARGS=(build -f "$SCRIPT_DIR/build/Dockerfile" -t "$BUILD_IMAGE" \
               --build-arg "BASE=$BASE_IMAGE" \
               ${PLATFORM_ARGS[@]+"${PLATFORM_ARGS[@]}"})
        [[ -n "$COPR_REPOS" ]] && BARGS+=(--build-arg "COPR_REPOS=$COPR_REPOS")
        "$TOOL" "${BARGS[@]}"
    fi

    TOPDIR="$WORK/rpmbuild"
    mkdir -p "$TOPDIR"/{BUILD,BUILDROOT,RPMS,SOURCES,SPECS,SRPMS}
    # The base (hence build) image's entrypoint is systemd: without an
    # override the rpmbuild command becomes systemd's arguments and the
    # container boots sshd/avahi/logind instead of running the build.
    # --rebuild unpacks the SRPM (spec -> SPECS/, sources -> SOURCES/) into
    # _topdir and builds the binary RPMs from it. --nocheck skips the spec's
    # %check (the full `make check` unit-test suite) -- CI runs tests in its
    # own `run` lane, so the build lane must not pay for it on a small runner.
    BUILD_CMD="rpmbuild --define '_topdir /root/rpmbuild' --nocheck --rebuild /root/build/$(basename "$SRPM_FILE")"
    if [[ "$BUILDEP" == "1" ]]; then
        # The build image carries the BuildRequires of the upstream spec,
        # baked once. A spec under test (a dist-git PR) may add or rename
        # some; builddep tops up the container from the distro repos so the
        # package builds exactly as Fedora's build system would build it.
        echo "==> --builddep: topping up BuildRequires in $BUILD_IMAGE"
        BUILD_CMD="set -e; dnf -y builddep /root/build/$(basename "$SRPM_FILE"); $BUILD_CMD"
    fi
    "$TOOL" run --rm \
        ${PLATFORM_ARGS[@]+"${PLATFORM_ARGS[@]}"} \
        --entrypoint /bin/bash \
        -v "$SRPM_DIR":/root/build:z \
        -v "$TOPDIR":/root/rpmbuild:z \
        "$BUILD_IMAGE" \
        -c "$BUILD_CMD"

    mkdir -p "$WORK/rpms"
    find "$TOPDIR/RPMS" -name '*.rpm' \
        ! -name '*debuginfo*' ! -name '*debugsource*' \
        -exec cp {} "$WORK/rpms/" \;
    # Fail fast if rpmbuild produced nothing (e.g. the container misbehaved);
    # otherwise the full-image build dies on an empty rpms/ with a confusing
    # "Failed to access RPM /root/rpms/*.rpm" glob error.
    [[ -n "$(ls -A "$WORK/rpms")" ]] || {
        echo "build.sh --srpm: rpmbuild produced no RPMs in $TOPDIR/RPMS" >&2
        exit 1; }
fi

# --- install IPA with dnf (no local RPMs / SRPM) ----------------------------
# --ipa-from-copr / --ipa-from-distro: the full image installs the IPA
# packages with dnf (the Dockerfile's IPA_DNF_SPECS branch) from the enabled
# COPR repos or from the distribution's own, so there is nothing to
# select or build locally. Provide an empty rpms/ so the Dockerfile's
# `COPY rpms/` still has a source; the install branch ignores its contents.
if [[ -n "$IPA_FROM" ]]; then
    if [[ "$IPA_FROM" == copr ]]; then
        echo "==> Installing IPA from COPR repos: $COPR_REPOS"
    else
        echo "==> Installing IPA from the Fedora $DIST distribution repos"
    fi
    echo "==> IPA dnf specs: $IPA_PACKAGES"
    mkdir -p "$WORK/rpms"
fi

ls "$WORK/rpms"

# the RPMs for something other than the full image (the qemu VMs of a
# nested preset install them: vm.rpms)
if [[ -n "$RPMS_OUT" ]]; then
    mkdir -p "$RPMS_OUT"
    cp "$WORK"/rpms/*.rpm "$RPMS_OUT"/
    echo "==> RPMs copied to $RPMS_OUT"
fi
[[ "$BUILD_ONLY" == "1" ]] && exit 0

# --- build the full image ----------------------------------------------------
echo "==> Building full image $FULL_IMAGE"
cp "$SCRIPT_DIR/full/Dockerfile" "$WORK/"
ARGS=(build -f "$WORK/Dockerfile" -t "$FULL_IMAGE" \
      ${PLATFORM_ARGS[@]+"${PLATFORM_ARGS[@]}"} \
      --build-arg "BASE=$BASE_IMAGE" "$WORK")
# Provenance of what `freeipa-env show` reports next to the installed builds.
ARGS+=(--label "org.freeipa-ci.built=$(date -u +%Y-%m-%dT%H:%M:%SZ)"
       --label "org.freeipa-ci.dist=$DIST"
       --label "org.freeipa-ci.source=${IPA_FROM:-${SRPM:+srpm}${RPMS:+rpms}}")
[[ -n "$DISTARCH" ]] && ARGS+=(--label "org.freeipa-ci.arch=$DISTARCH")
[[ -n "$COPR_REPOS" ]] && ARGS+=(--label "org.freeipa-ci.copr=$COPR_REPOS")
[[ -n "$IPA_FROM" ]] && ARGS+=(--label "org.freeipa-ci.ipa-packages=$IPA_PACKAGES")
[[ -n "$PINS" ]] && ARGS+=(--build-arg "PIN_SPECS=$PINS")
[[ -n "$COPR_REPOS" ]] && ARGS+=(--build-arg "COPR_REPOS=$COPR_REPOS")
if [[ -n "$IPA_FROM" ]]; then
    ARGS+=(--build-arg "IPA_DNF_SPECS=$IPA_PACKAGES")
fi
"$TOOL" "${ARGS[@]}"

# Image tag contract:
#   <tag>/full:<dist>-<sha>  immutable per-build tag (provenance)
#   <tag>/full:<dist>        rolling dist pointer, re-pointed by every build
#   <tag>/full:<channel>     build channel (current/next/previous) — the
#                            abstract reference presets use. Presets name a
#                            channel (`image: freeipa-<channel>`), never a
#                            concrete build; the podman provider resolves the
#                            channel tag to the concrete image at up time
#                            (freeipa-env resolve).
CHANNEL_TAG="$TAG/full:$CHANNEL"
"$TOOL" tag "$FULL_IMAGE" "$CHANNEL_TAG"
echo "==> Tagged channel $CHANNEL_TAG (= $FULL_IMAGE)"
if [[ -n "$SHA_TAG" ]]; then
    "$TOOL" tag "$FULL_IMAGE" "$TAG/full:$SHA_TAG"
    echo "==> Tagged $TAG/full:$SHA_TAG (provenance)"
fi

echo "==> Done: $CHANNEL_TAG (channel ${SHA_TAG:+and $TAG/full:$SHA_TAG, rolling $FULL_IMAGE})"
