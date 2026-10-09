#!/bin/bash
# LUBARCHY repository lint: checks tracked paths, file modes, shell syntax
# and obvious secret material. It inspects repository state, not prose, so
# documentation may still name things the project prohibits.
set -euo pipefail

REPO_ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
cd "${REPO_ROOT}"
git rev-parse --verify --quiet 'HEAD^{commit}' > /dev/null || {
	echo "FAIL: not a Git checkout with a commit" >&2
	exit 1
}

failures=0
fail() {
	echo "FAIL: $*" >&2
	failures=$((failures + 1))
}
ok() { echo "ok: $*"; }

mapfile -t TRACKED < <(git ls-files)

# 1. Whitespace errors in tracked content (working tree against the empty tree).
EMPTY_TREE=$(git hash-object -t tree /dev/null)
if ! git -c core.whitespace=blank-at-eol,blank-at-eof,space-before-tab diff --check "${EMPTY_TREE}" -- > /dev/null; then
	git diff --check "${EMPTY_TREE}" -- >&2 || true
	fail "whitespace errors in tracked files"
else
	ok "whitespace"
fi

# 2-6. Generated images, build state and live-build defaults must not be tracked.
bad=$(printf '%s\n' "${TRACKED[@]}" | grep -E \
	-e '\.(iso|img|qcow2|raw|vmdk|vdi|squashfs)$' \
	-e '^build/live/(build\.log|chroot\.files|chroot\.packages\..*|binary\.modified_timestamps|lubarchy-m0-.*)$' \
	-e '^build/live/(\.build|cache|chroot|binary|source)/' \
	-e '^build/live/config/(binary|bootstrap|chroot|common|source)$' \
	-e '^build/live/config/package-lists/live\.list\.chroot$' \
	-e '(^|/)(SHA256SUMS|SHA512SUMS)$' || true)
if [ -n "${bad}" ]; then
	while IFS= read -r line; do echo "  ${line}" >&2; done <<< "${bad}"
	fail "generated images, build state or live-build defaults are tracked"
else
	ok "no generated images, build state or live-build defaults tracked"
fi
links=$(git ls-files -s | awk '$1 == "120000" {print $4}')
if [ -n "${links}" ]; then
	while IFS= read -r line; do echo "  ${line}" >&2; done <<< "${links}"
	fail "tracked symlinks (live-build default hook links must not be committed)"
else
	ok "no tracked symlinks"
fi

# 7-8. Shell scripts: executable iff they have a shebang; syntax by dialect.
scripts=0
for f in "${TRACKED[@]}"; do
	[ -f "${f}" ] || continue
	first=$(head -c 64 -- "${f}" | head -n 1)
	mode=$(git ls-files -s -- "${f}" | awk '{print $1}')
	dialect=""
	case "${first}" in
	'#!/bin/sh'*) dialect="sh" ;;
	'#!/bin/bash'* | '#!/usr/bin/env bash'*) dialect="bash" ;;
	'#!'*) dialect="other" ;;
	'# shellcheck shell=sh'*) dialect="sh-sourced" ;;
	esac
	case "${dialect}" in
	sh | bash | other)
		[ "${mode}" = 100755 ] || fail "${f}: has a shebang but mode ${mode}"
		;;
	*)
		[ "${mode}" != 100755 ] || fail "${f}: executable without a shebang"
		;;
	esac
	case "${dialect}" in
	sh | sh-sourced) sh -n "${f}" || fail "${f}: sh syntax error"; scripts=$((scripts + 1)) ;;
	bash) bash -n "${f}" || fail "${f}: bash syntax error"; scripts=$((scripts + 1)) ;;
	esac
done
ok "shell modes and syntax checked (${scripts} scripts)"

# 9. Private keys and obvious secrets.
key_names=$(printf '%s\n' "${TRACKED[@]}" | grep -E \
	-e '(^|/)id_(rsa|dsa|ecdsa|ed25519)$' \
	-e '\.(p12|pfx|jks|keystore)$' \
	-e '(^|/)\.env$' || true)
[ -z "${key_names}" ] || fail "private key or environment files tracked: ${key_names}"
key_header='-----BEGIN [A-Z ]*PRIVATE'' KEY-----'
token_re='(ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{36}|github_pat_[A-Za-z0-9_]{40,}|AKIA[0-9A-Z]{16}'
secret_hits=$(git grep -lE -e "${key_header}" -e "${token_re}" -- . || true)
[ -z "${secret_hits}" ] || fail "secret material in: ${secret_hits}"
if [ -z "${key_names}${secret_hits}" ]; then
	ok "no private keys or tokens tracked"
fi

# 10. Legacy (pre-Debian generation) build artifacts.
legacy=$(printf '%s\n' "${TRACKED[@]}" | grep -iE \
	-e '(^|/)(archiso|airootfs|releng)(/|$)' \
	-e '(^|/)(PKGBUILD|profiledef\.sh|pacman\.conf|mkinitcpio\.conf|packages\.x86_64)$' || true)
if [ -n "${legacy}" ]; then
	fail "legacy artifacts tracked: ${legacy}"
else
	ok "no legacy-generation build artifacts"
fi

if [ "${failures}" -gt 0 ]; then
	echo "FAIL: repository lint (${failures} problem(s))" >&2
	exit 1
fi
echo "PASS: repository lint"
