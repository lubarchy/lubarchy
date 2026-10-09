#!/bin/bash
# LUBARCHY M0 automated serial live-payload smoke test.
#
# Boots the kernel and initrd taken from an ISO directly under QEMU/KVM, with
# the same ISO attached read-only as the live medium, and watches the serial
# console until the live system reaches userspace and a ttyS0 login prompt.
#
# Direct kernel loading only makes serial output deterministic. This test is
# NOT evidence for the UEFI/GRUB boot path; that is covered by the separate
# UEFI optical boot test (see docs/testing.md).
#
# Usage:
#   tests/iso-boot-smoke.sh --iso FILE --sha256 HEX --kernel FILE --initrd FILE \
#       [--grub-cfg FILE] [--log FILE] [--timeout SECONDS]
#
# --kernel/--initrd/--grub-cfg must be extracted read-only from the same ISO.
# --grub-cfg supplies the default Live entry's kernel parameters; without it
# "boot=live components" is used.
set -euo pipefail

TIMEOUT=180
LOG=""
GRUB_CFG=""
ISO="" SHA256="" KERNEL="" INITRD=""

usage() {
	sed -n '/^# Usage:/,/^# --grub-cfg/p' "$0" | sed 's/^# \{0,1\}//' >&2
	exit 2
}

fail() {
	echo "FAIL: $*" >&2
	exit 1
}

while [ $# -gt 0 ]; do
	case "$1" in
	--iso) ISO="$2"; shift 2 ;;
	--sha256) SHA256="$2"; shift 2 ;;
	--kernel) KERNEL="$2"; shift 2 ;;
	--initrd) INITRD="$2"; shift 2 ;;
	--grub-cfg) GRUB_CFG="$2"; shift 2 ;;
	--log) LOG="$2"; shift 2 ;;
	--timeout) TIMEOUT="$2"; shift 2 ;;
	*) usage ;;
	esac
done
if [ -z "${ISO}" ] || [ -z "${SHA256}" ] || [ -z "${KERNEL}" ] || [ -z "${INITRD}" ]; then
	usage
fi
[[ "${TIMEOUT}" =~ ^[0-9]+$ ]] || fail "--timeout must be a number of seconds"

if [ ! -r /dev/kvm ] || [ ! -w /dev/kvm ]; then
	fail "KVM is not available (/dev/kvm)"
fi
QEMU=$(command -v qemu-system-x86_64) || fail "qemu-system-x86_64 not found"

if [ ! -f "${ISO}" ] || [ -L "${ISO}" ]; then
	fail "ISO is not a regular file: ${ISO}"
fi
[ ! -w "${ISO}" ] || fail "ISO must be a read-only input (remove write permission): ${ISO}"
for f in "${KERNEL}" "${INITRD}" ${GRUB_CFG:+"${GRUB_CFG}"}; do
	[ -f "${f}" ] || fail "missing input: ${f}"
done

actual=$(sha256sum -- "${ISO}" | cut -d' ' -f1)
[ "${actual}" = "${SHA256}" ] || fail "ISO SHA-256 mismatch: ${actual}"
echo "ok: ISO SHA-256 ${actual}"

# Kernel parameters of the default Live entry, as GRUB would pass them when
# booting the ISO from optical media (${iso_path} is unset there).
if [ -n "${GRUB_CFG}" ]; then
	params=$(awk '/^menuentry "Live system \(amd64\)"/{f=1} f && $1=="linux"{for(i=3;i<=NF;i++) printf "%s ", $i; exit}' "${GRUB_CFG}")
	[ -n "${params}" ] || fail "default Live entry not found in ${GRUB_CFG}"
	# The literal GRUB variable reference is removed, not expanded.
	# shellcheck disable=SC2016
	params=${params//'${iso_path}'/}
else
	params="boot=live components"
fi
params="${params% }"
case " ${params} " in *" boot=live "*) ;; *) fail "kernel parameters lack boot=live" ;; esac
# Test instrumentation only.
CMDLINE="${params} console=ttyS0,115200n8 live-config.noautologin systemd.show_status=yes"
echo "ok: kernel command line: ${CMDLINE}"

WORK=$(mktemp -d)
QEMU_PID=""
cleanup() {
	if [ -n "${QEMU_PID}" ] && kill -0 "${QEMU_PID}" 2>/dev/null; then
		kill "${QEMU_PID}" 2>/dev/null || true
		wait "${QEMU_PID}" 2>/dev/null || true
	fi
	rm -rf -- "${WORK}"
}
trap cleanup EXIT

SERIAL="${WORK}/serial.log"
: > "${SERIAL}"

# No disk, no network, ISO attached read-only as a CD-ROM.
"${QEMU}" \
	-nodefaults -no-user-config \
	-machine q35,accel=kvm -cpu host -smp 2 -m 2048 \
	-display none -monitor none -no-reboot \
	-nic none \
	-drive "file=${ISO},format=raw,media=cdrom,readonly=on,if=ide" \
	-kernel "${KERNEL}" -initrd "${INITRD}" -append "${CMDLINE}" \
	-chardev "file,id=serial0,path=${SERIAL}" -serial chardev:serial0 \
	> "${WORK}/qemu.out" 2>&1 &
QEMU_PID=$!
START=$(date +%s)

FATAL='Kernel panic|not syncing|Unable to find a medium containing a live file system|Begin: Mounting root file system.*failed|^\(initramfs\)|SQUASHFS error|You are in emergency mode|Entering emergency mode'
# Userspace: systemd banner from the live root, multi-user target, ttyS0 login.
MARK_ROOT='Welcome to .*Debian GNU/Linux 13'
MARK_TARGET='Reached target .*(multi-user\.target|Multi-User System)'
MARK_LOGIN='login: *$'

result=""
while :; do
	elapsed=$(($(date +%s) - START))
	text=$(tr -d '\r' < "${SERIAL}" | sed 's/\x1b\[[0-9;?]*[A-Za-z]//g')
	if printf '%s\n' "${text}" | grep -qE "${FATAL}"; then
		result="fatal"
		break
	fi
	if printf '%s\n' "${text}" | grep -qE "${MARK_ROOT}" &&
		printf '%s\n' "${text}" | grep -qE "${MARK_TARGET}" &&
		printf '%s\n' "${text}" | grep -qE "${MARK_LOGIN}"; then
		result="pass"
		break
	fi
	if ! kill -0 "${QEMU_PID}" 2>/dev/null; then
		result="exited"
		break
	fi
	if [ "${elapsed}" -ge "${TIMEOUT}" ]; then
		result="timeout"
		break
	fi
	sleep 1
done
elapsed=$(($(date +%s) - START))

if [ -n "${LOG}" ]; then
	cp -- "${SERIAL}" "${LOG}"
fi

case "${result}" in
pass)
	printf '%s\n' "${text}" | grep -m1 -E "${MARK_ROOT}" | sed 's/^/ok: /'
	printf '%s\n' "${text}" | grep -m1 -E "${MARK_TARGET}" | sed 's/^ */ok: /'
	printf '%s\n' "${text}" | grep -E '^Debian GNU/Linux .* ttyS0' | tail -1 | sed 's/^/ok: /'
	printf '%s\n' "${text}" | grep -E "${MARK_LOGIN}" | tail -1 | sed 's/^/ok: /'
	echo "PASS: live payload reached userspace and a ttyS0 login prompt in ${elapsed}s"
	;;
fatal)
	printf '%s\n' "${text}" | grep -m3 -E "${FATAL}" >&2
	fail "fatal boot marker after ${elapsed}s"
	;;
exited)
	tail -n 20 "${WORK}/qemu.out" >&2
	fail "QEMU exited before userspace was reached (${elapsed}s)"
	;;
*)
	printf '%s\n' "${text}" | tail -n 15 >&2
	fail "no login prompt within ${TIMEOUT}s"
	;;
esac
