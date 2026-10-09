#!/bin/bash
# LUBARCHY M0 live-build configuration gate.
#
# Generates the live-build configuration from build/live/auto/config,
# validates it with the installed live-build and asserts the M0 contract.
# It never builds an image: any attempt to run "lb build" aborts the test.
# Run it inside the Debian builder; it needs live-build but not root.
set -euo pipefail

REPO_ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
LIVE_DIR="${REPO_ROOT}/build/live"

fail() {
	echo "FAIL: $*" >&2
	exit 1
}

[ -d "${LIVE_DIR}/auto" ] || fail "missing ${LIVE_DIR}/auto"
REAL_LB=$(command -v lb) || fail "live-build (lb) is not installed"

for f in config clean; do
	sh -n "${LIVE_DIR}/auto/${f}" || fail "syntax error in auto/${f}"
done
bash -n "${LIVE_DIR}/auto/build" || fail "syntax error in auto/build"
sh -n "${LIVE_DIR}/auto/build-environment" || fail "syntax error in auto/build-environment"
echo "ok: auto script syntax"

# Deterministic build epoch (auto/build-environment).
for f in config build; do
	grep -qxF '. ./auto/build-environment' "${LIVE_DIR}/auto/${f}" ||
		fail "auto/${f} does not source auto/build-environment"
done
if grep -nwE 'date' "${LIVE_DIR}"/auto/*; then
	fail "build entry points must not read the wall-clock time"
fi
HEAD_EPOCH=$(git -C "${REPO_ROOT}" show -s --format=%ct HEAD) ||
	fail "not a Git checkout; the deterministic build epoch needs the HEAD commit"
epoch_of() {
	(cd "${LIVE_DIR}" && sh -c '. ./auto/build-environment > /dev/null && printf "%s" "${SOURCE_DATE_EPOCH}"')
}
derived=$(unset SOURCE_DATE_EPOCH; epoch_of) || fail "auto/build-environment failed"
[ "${derived}" = "${HEAD_EPOCH}" ] ||
	fail "derived SOURCE_DATE_EPOCH ${derived} != HEAD commit time ${HEAD_EPOCH}"
echo "ok: SOURCE_DATE_EPOCH defaults to the HEAD commit time (${HEAD_EPOCH})"
explicit=$(SOURCE_DATE_EPOCH=1234567890 epoch_of) || fail "valid explicit SOURCE_DATE_EPOCH rejected"
[ "${explicit}" = "1234567890" ] || fail "explicit SOURCE_DATE_EPOCH not honoured (${explicit})"
echo "ok: explicit SOURCE_DATE_EPOCH honoured"
for bad in "" "abc" "-1" "12a" "1.5" " 1"; do
	if (SOURCE_DATE_EPOCH="${bad}" epoch_of) > /dev/null 2>&1; then
		fail "invalid SOURCE_DATE_EPOCH '${bad}' was accepted"
	fi
done
echo "ok: invalid SOURCE_DATE_EPOCH values rejected"

for f in binary bootstrap chroot common source; do
	[ ! -e "${LIVE_DIR}/config/${f}" ] ||
		fail "generated config/${f} already exists; run auto/clean first"
done

WORK_DIR=$(mktemp -d)
BEFORE="${WORK_DIR}/before.list"
(cd "${LIVE_DIR}" && find . -mindepth 1 | LC_ALL=C sort) > "${BEFORE}"

cleanup() {
	local after="${WORK_DIR}/after.list"
	if [ -d "${LIVE_DIR}" ]; then
		(cd "${LIVE_DIR}" && find . -mindepth 1 | LC_ALL=C sort) > "${after}"
		# Remove only paths created by this run, deepest first.
		LC_ALL=C comm -13 "${BEFORE}" "${after}" | LC_ALL=C sort -r |
			while IFS= read -r p; do
				rm -rf -- "${LIVE_DIR:?}/${p#./}"
			done
	fi
	rm -rf -- "${WORK_DIR}"
}
trap cleanup EXIT

# Guard: pass every lb subcommand through except "build".
mkdir -p "${WORK_DIR}/bin"
cat > "${WORK_DIR}/bin/lb" <<EOF
#!/bin/sh
if [ "\${1:-}" = build ]; then
	echo "FAIL: lb build is not permitted in the configuration gate" >&2
	exit 99
fi
exec "${REAL_LB}" "\$@"
EOF
chmod 0755 "${WORK_DIR}/bin/lb"
export PATH="${WORK_DIR}/bin:${PATH}"

cd "${LIVE_DIR}"

./auto/config > "${WORK_DIR}/config.log" 2>&1 ||
	{ cat "${WORK_DIR}/config.log" >&2; fail "auto/config failed"; }
echo "ok: auto/config generated the configuration"

lb config noauto --validate > "${WORK_DIR}/validate.log" 2>&1 ||
	{ cat "${WORK_DIR}/validate.log" >&2; fail "lb config --validate failed"; }
if grep -qE '^(E|W):' "${WORK_DIR}/validate.log"; then
	cat "${WORK_DIR}/validate.log" >&2
	fail "lb config --validate reported problems"
fi
echo "ok: lb config --validate"

DUMP="${WORK_DIR}/dump.txt"
lb config noauto --dump > "${DUMP}" 2>&1 || fail "lb config --dump failed"
grep -m1 '^This is live-build version' "${DUMP}" || true

assert_var() {
	local name="$1" expected="$2" lines value
	lines=$(grep -E "^config/[a-z]+: ${name}=" "${DUMP}" || true)
	[ -n "${lines}" ] || fail "${name} not present in lb config --dump"
	[ "$(printf '%s\n' "${lines}" | wc -l)" -eq 1 ] || fail "${name} defined more than once"
	value=${lines#*=\"}
	value=${value%\"}
	[ "${value}" = "${expected}" ] || fail "${name}=\"${value}\", expected \"${expected}\""
	echo "ok: ${name}=\"${value}\""
}

DEBIAN_MIRROR="https://deb.debian.org/debian/"
SECURITY_MIRROR="https://security.debian.org/debian-security/"

assert_var LB_MODE "debian"
assert_var LB_DISTRIBUTION "trixie"
assert_var LB_DISTRIBUTION_CHROOT "trixie"
assert_var LB_DISTRIBUTION_BINARY "trixie"
assert_var LB_ARCHITECTURE "amd64"
assert_var LB_SYSTEM "live"
assert_var LB_IMAGE_TYPE "iso-hybrid"
assert_var LB_ARCHIVE_AREAS "main"
assert_var LB_PARENT_ARCHIVE_AREAS "main"
assert_var LB_BACKPORTS "false"
assert_var LB_PROPOSED_UPDATES "false"
assert_var LB_SECURITY "true"
assert_var LB_UPDATES "true"
assert_var LB_APT_SECURE "true"
assert_var LB_APT_SOURCE_ARCHIVES "false"
assert_var LB_DEBIAN_INSTALLER "none"
assert_var LB_FIRMWARE_BINARY "false"
assert_var LB_FIRMWARE_CHROOT "false"
assert_var LB_SOURCE "false"
assert_var LB_BOOTLOADERS "grub-pc grub-efi"
assert_var LB_BOOTLOADER_BIOS "grub-pc"
assert_var LB_BOOTLOADER_EFI "grub-efi"
assert_var LB_UEFI_SECURE_BOOT "disable"
assert_var LB_MEMTEST "none"
assert_var LB_LINUX_FLAVOURS_WITH_ARCH "amd64"
assert_var LB_INTERACTIVE "false"
assert_var LB_UTC_TIME "true"
assert_var LB_ZSYNC "false"
assert_var LB_IMAGE_NAME "lubarchy-m0"
assert_var LB_ISO_APPLICATION "LUBARCHY M0"
assert_var LB_ISO_PUBLISHER "LUBARCHY; https://lubarchy.com"
assert_var LB_ISO_VOLUME "LUBARCHY_M0"
assert_var LB_MIRROR_BOOTSTRAP "${DEBIAN_MIRROR}"
assert_var LB_MIRROR_CHROOT "${DEBIAN_MIRROR}"
assert_var LB_MIRROR_CHROOT_SECURITY "${SECURITY_MIRROR}"
assert_var LB_MIRROR_BINARY "${DEBIAN_MIRROR}"
assert_var LB_MIRROR_BINARY_SECURITY "${SECURITY_MIRROR}"
assert_var LB_PARENT_MIRROR_BOOTSTRAP "${DEBIAN_MIRROR}"
assert_var LB_PARENT_MIRROR_CHROOT "${DEBIAN_MIRROR}"
assert_var LB_PARENT_MIRROR_CHROOT_SECURITY "${SECURITY_MIRROR}"
assert_var LB_PARENT_MIRROR_BINARY "${DEBIAN_MIRROR}"
assert_var LB_PARENT_MIRROR_BINARY_SECURITY "${SECURITY_MIRROR}"

if grep -E '^config/[a-z]+: LB_[A-Z_]*ARCHIVE_AREAS=' "${DUMP}" |
	grep -qE 'contrib|non-free'; then
	fail "contrib/non-free archive area configured"
fi
echo "ok: no contrib/non-free archive areas"

# Reproducibility: APT's regenerable binary caches embed wall-clock time.
# live-build's binary_rootfs passes config/rootfs/excludes to mksquashfs
# (-wildcards -ef) for SquashFS images built in a chroot, so they are left out
# of the live root at SquashFS creation time. Nothing else may be excluded.
assert_var LB_CHROOT_FILESYSTEM "squashfs"
assert_var LB_BUILD_WITH_CHROOT "true"
EXCLUDES="${LIVE_DIR}/config/rootfs/excludes"
if [ ! -f "${EXCLUDES}" ] || [ -L "${EXCLUDES}" ]; then
	fail "config/rootfs/excludes must be a regular file"
fi
git -C "${REPO_ROOT}" ls-files --error-unmatch -- "${EXCLUDES}" > /dev/null 2>&1 ||
	fail "config/rootfs/excludes is not tracked"
expected_excludes=$'var/cache/apt/pkgcache.bin\nvar/cache/apt/srcpkgcache.bin'
[ "$(cat "${EXCLUDES}")" = "${expected_excludes}" ] ||
	fail "config/rootfs/excludes must list exactly var/cache/apt/pkgcache.bin and var/cache/apt/srcpkgcache.bin"
if grep -qE '[][*?]|^\.\.\.|^/' "${EXCLUDES}"; then
	fail "config/rootfs/excludes must not use wildcards, '...' or absolute paths"
fi
if grep -qE 'var/lib/apt|var/cache/apt/archives|etc/apt' "${EXCLUDES}"; then
	fail "config/rootfs/excludes must not exclude APT lists, archives or configuration"
fi
if [ -d "${LIVE_DIR}/config/hooks" ] &&
	git -C "${REPO_ROOT}" grep -lE 'pkgcache|srcpkgcache' -- "${LIVE_DIR}/config/hooks" > /dev/null 2>&1; then
	fail "APT cache handling belongs in config/rootfs/excludes, not in a chroot hook"
fi
echo "ok: config/rootfs/excludes = APT binary caches only (pkgcache.bin, srcpkgcache.bin)"

# Project GRUB menu override: the installed live-build template plus only the
# menu timeout lines.
GRUB_CFG="${LIVE_DIR}/config/bootloaders/grub-pc/config.cfg"
GRUB_TEMPLATE="/usr/share/live/build/bootloaders/grub-pc/config.cfg"
[ -f "${GRUB_CFG}" ] || fail "missing ${GRUB_CFG#"${REPO_ROOT}"/}"
for line in "set default=0" "set timeout_style=menu" "set timeout=5"; do
	[ "$(grep -cxF "${line}" "${GRUB_CFG}")" -eq 1 ] || fail "GRUB config.cfg must contain '${line}' exactly once"
done
if [ -f "${GRUB_TEMPLATE}" ]; then
	grep -vxE 'set timeout_style=menu|set timeout=5' "${GRUB_CFG}" | cmp -s - "${GRUB_TEMPLATE}" ||
		fail "GRUB config.cfg differs from the installed template beyond the timeout lines"
	echo "ok: GRUB config.cfg = installed template + 5-second menu timeout"
else
	fail "installed GRUB template not found: ${GRUB_TEMPLATE}"
fi

for d in chroot binary cache; do
	[ ! -e "${LIVE_DIR}/${d}" ] || fail "build output ${d} exists"
done
if find "${LIVE_DIR}" -maxdepth 1 -name '*.iso' | grep -q .; then
	fail "an ISO image exists in ${LIVE_DIR}"
fi
echo "ok: no build was run and no image exists"

echo "PASS: live-build configuration gate"
