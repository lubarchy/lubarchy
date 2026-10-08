#!/bin/bash
# LUBARCHY M0 hybrid ISO structure check (read-only, no root).
#
# Verifies that an ISO keeps its UEFI El Torito boot image and also carries
# the hybrid disk structures needed when its bytes are used as a removable
# disk: an MBR system area with a boot signature, a GPT, and a partition that
# maps the EFI boot image.
#
# This is a structural check only. It does not prove that any physical USB
# device or firmware boots the image.
#
# Usage: tests/iso-hybrid-structure.sh --iso FILE --sha256 HEX [--report FILE]
set -euo pipefail

ISO="" SHA256="" REPORT=""

fail() {
	echo "FAIL: $*" >&2
	exit 1
}

while [ $# -gt 0 ]; do
	case "$1" in
	--iso) ISO="$2"; shift 2 ;;
	--sha256) SHA256="$2"; shift 2 ;;
	--report) REPORT="$2"; shift 2 ;;
	*) echo "usage: $0 --iso FILE --sha256 HEX [--report FILE]" >&2; exit 2 ;;
	esac
done
[ -n "${ISO}" ] && [ -n "${SHA256}" ] || { echo "usage: $0 --iso FILE --sha256 HEX [--report FILE]" >&2; exit 2; }

command -v xorriso > /dev/null || fail "xorriso not found"
[ -f "${ISO}" ] && [ ! -L "${ISO}" ] || fail "ISO is not a regular file: ${ISO}"

actual=$(sha256sum -- "${ISO}" | cut -d' ' -f1)
[ "${actual}" = "${SHA256}" ] || fail "ISO SHA-256 mismatch: ${actual}"
echo "ok: ISO SHA-256 ${actual}"

# xorriso only reads the image when it is loaded with -indev.
out=$(xorriso -indev "${ISO}" -report_el_torito plain -report_system_area plain 2>&1) ||
	fail "xorriso could not read the image"
if [ -n "${REPORT}" ]; then
	printf '%s\n' "${out}" > "${REPORT}"
fi
field() { printf '%s\n' "${out}" | grep -E "^$1" || true; }

# ISO 9660 readability.
printf '%s\n' "${out}" | grep -qE "^Volume id +: '.+'" || fail "no ISO 9660 volume id"
echo "ok: $(field 'Volume id' | head -1)"

# UEFI El Torito boot image.
efi_n=$(field 'El Torito boot img' | awk '$7=="UEFI"{print $6; exit}')
[ -n "${efi_n}" ] || fail "no UEFI El Torito boot entry"
efi_path=$(field 'El Torito img path' | awk -v n="${efi_n}" '$6==n{print $7; exit}')
[ -n "${efi_path}" ] || fail "UEFI El Torito entry has no image path"
echo "ok: UEFI El Torito entry ${efi_n} -> ${efi_path}"

# MBR system area.
summary=$(field 'System area summary' | sed 's/^System area summary: *//')
[ -n "${summary}" ] || fail "system area is empty (no MBR/GPT)"
echo "ok: system area: ${summary}"
case " ${summary} " in *" MBR "*) ;; *) fail "system area has no MBR" ;; esac
sig=$(od -An -tx1 -j510 -N2 -- "${ISO}" | tr -d ' \n')
[ "${sig}" = "55aa" ] || fail "MBR boot signature is ${sig}, expected 55aa"
echo "ok: MBR boot signature 0x55AA"
mbr_parts=$(field 'MBR partition +:')
[ -n "${mbr_parts}" ] || fail "MBR partition table is empty"
echo "ok: MBR partitions: $(printf '%s\n' "${mbr_parts}" | wc -l)"

# GPT.
case " ${summary} " in *" GPT "*) ;; *) fail "system area has no GPT" ;; esac
[ "$(dd if="${ISO}" bs=1 skip=512 count=8 status=none)" = "EFI PART" ] ||
	fail "no GPT header signature at LBA 1"
field 'GPT disk GUID' | grep -q . || fail "GPT disk GUID missing"
gpt_parts=$(field 'GPT start and size')
[ -n "${gpt_parts}" ] || fail "GPT has no partitions"
echo "ok: GPT header present, partitions: $(printf '%s\n' "${gpt_parts}" | wc -l)"

# A partition (MBR or GPT) must map the UEFI boot image.
mapped=$(printf '%s\n' "${out}" | grep -E "^(MBR|GPT) partition path +: +[0-9]+ +${efi_path}\$" || true)
[ -n "${mapped}" ] || fail "no MBR/GPT partition maps ${efi_path}"
printf '%s\n' "${mapped}" | sed 's/^/ok: EFI image mapped: /'
mbr_efi=$(printf '%s\n' "${mbr_parts}" | awk '$6=="0xef"' || true)
if [ -n "${mbr_efi}" ]; then
	echo "ok: MBR EFI system partition (type 0xef) present"
fi

echo "PASS: hybrid ISO structure (UEFI El Torito + MBR + GPT + EFI image partition)"
echo "note: structural check only; physical USB boot is not established by this test"
