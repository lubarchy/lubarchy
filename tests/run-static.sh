#!/bin/bash
# LUBARCHY M0 static quality gates, in order:
#   1. tests/repo-lint.sh
#   2. ShellCheck on every project shell script
#   3. tests/live-build-config.sh
# Stops at the first failing gate. Never builds an image or touches libvirt;
# needs git, shellcheck and live-build but not root.
set -euo pipefail

REPO_ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
cd "${REPO_ROOT}"

run_gate() {
	local name="$1"
	shift
	echo "=== ${name}"
	if "$@"; then
		echo "GATE PASS: ${name}"
	else
		echo "GATE FAIL: ${name}" >&2
		exit 1
	fi
}

shellcheck_scripts() {
	command -v shellcheck > /dev/null || {
		echo "FAIL: shellcheck is not installed" >&2
		return 1
	}
	local f first scripts=()
	while IFS= read -r f; do
		first=$(head -n 1 -- "${f}")
		case "${first}" in
		'#!/bin/sh'* | '#!/bin/bash'* | '#!/usr/bin/env bash'* | '# shellcheck shell=sh'*)
			scripts+=("${f}")
			;;
		esac
	done < <(git ls-files)
	[ "${#scripts[@]}" -gt 0 ] || {
		echo "FAIL: no shell scripts found" >&2
		return 1
	}
	shellcheck --version | sed -n 's/^version: /shellcheck /p'
	printf '  %s\n' "${scripts[@]}"
	# Checked explicitly: set -e does not apply inside the caller's if.
	if ! shellcheck --external-sources --severity=style "${scripts[@]}"; then
		echo "FAIL: shellcheck findings" >&2
		return 1
	fi
	echo "ok: shellcheck clean (${#scripts[@]} scripts)"
}

run_gate "repository lint" tests/repo-lint.sh
run_gate "shellcheck" shellcheck_scripts
run_gate "live-build configuration" tests/live-build-config.sh
echo "PASS: all static gates"
