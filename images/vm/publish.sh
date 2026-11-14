#!/bin/bash
# Build a containerdisk image from a qcow2 and, with --push, push it.
#
# A containerdisk is a scratch image that holds one file, /disk/disk.qcow2.
# A preset uses it as `vm.image: containerdisk:REF`; `up` pulls it with podman
# and copies the disk out (env/freeipa_env/containerdisk.py).
#
# Usage:
#   images/vm/publish.sh [--arch ARCH] DISK.qcow2 REF [--push]
# Example, with the Fedora cloud base image that qemu-prep.sh downloaded:
#   images/vm/publish.sh ~/.cache/freeipa-ci/images/Fedora-Cloud-Base-Generic-45-1.4.x86_64.qcow2 \
#       ghcr.io/OWNER/fedora-cloud-base:45-1.4 --push
# --arch ARCH builds for linux/ARCH (e.g. aarch64, for an arm64 manifest);
# default: the host platform.
# Log in first (`podman login ghcr.io`); this script does not handle
# credentials.
set -euo pipefail

die() { echo "error: $*" >&2; exit 1; }
[ $# -ge 2 ] && [ $# -le 4 ] || die "usage: $0 [--arch ARCH] DISK.qcow2 REF [--push]"
arch=""
if [ "$1" = --arch ]; then
    [ $# -ge 3 ] || die "--arch needs a value"
    arch="$2"; shift 2
fi
disk="$1" ref="$2" push="${3:-}"
[ -z "$push" ] || [ "$push" = --push ] || die "unknown option: $push"
[ -f "$disk" ] || die "no such file: $disk"
qemu-img info "$disk" | grep -q 'file format: qcow2' \
    || die "$disk is not a qcow2 file"

ctx="$(mktemp -d)"
trap 'rm -rf "$ctx"' EXIT
ln "$disk" "$ctx/disk.qcow2" 2>/dev/null || cp --reflink=auto "$disk" "$ctx/disk.qcow2"
sum="$(sha256sum "$disk" | cut -d' ' -f1)"
cat > "$ctx/Containerfile" <<EOF
FROM scratch
LABEL org.opencontainers.image.title="$(basename "$disk")"
LABEL io.github.freeipa-ci.disk-sha256="$sum"
COPY disk.qcow2 /disk/disk.qcow2
EOF
platform_args=""
[ -n "$arch" ] && platform_args="--platform linux/$arch"
podman build -q -t "$ref" $platform_args -f "$ctx/Containerfile" "$ctx" >/dev/null
echo "built $ref (disk sha256 $sum)"
if [ "$push" = --push ]; then
    podman push "$ref"
    echo "pushed $ref"
fi
