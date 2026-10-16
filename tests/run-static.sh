#!/bin/bash
# LUBARCHY static quality gates, in order:
#   1. tests/repo-lint.sh
#   2. ShellCheck on every project shell script
#   3. tests/live-build-config.sh
#   4. tests/hardware-contracts.py (M1 hardware inventory/rule/plan contracts)
#   5. tests/hardware-probe.py (M1 read-only probe, synthetic sysfs only)
#   6. tests/hardware-resolver.py (M1 production resolver and rules, synthetic only)
#   7. tests/hardware-firmware.py (M1 device firmware mappings vs committed kernel alias evidence)
#   8. tests/nvidia-strategy.py (M1-G NVIDIA strategy evidence, M1-H inactive repository containment policy; committed evidence only)
#   9. tests/hardware-transaction.py (M1-I install-only package transaction contract; synthetic only)
#  10. tests/hardware-staging.py (M1-J source activation and staged package bundle; synthetic and offline only)
#  11. tests/hardware-apply.py (M1-K privileged apply executor; fake host, never mutates or needs root)
#  12. tests/hardware-recovery.py (M1-L interrupted transaction recovery; fake host, never mutates or needs root)
#  13. tests/hardware-validation.py (M1-M post-install hardware validation; synthetic evidence and sysfs, read-only)
#  14. tests/hardware-gc.py (M1-N package-state garbage collection; fake host roots only, never the runner's state)
#  15. tests/hardware-orchestration.py (M1-O hardware-enablement orchestration; fake host roots only, never reboots or mutates the runner)
# Stops at the first failing gate. Never builds an image, touches libvirt or
# probes the host's hardware; needs git, shellcheck, live-build, python3 and
# python3-jsonschema but not root.
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
run_gate "hardware contracts" tests/hardware-contracts.py
run_gate "hardware probe" tests/hardware-probe.py
run_gate "hardware resolver" tests/hardware-resolver.py
run_gate "hardware firmware" tests/hardware-firmware.py
run_gate "hardware NVIDIA strategy" tests/nvidia-strategy.py
run_gate "hardware package transaction" tests/hardware-transaction.py
run_gate "hardware package staging" tests/hardware-staging.py
run_gate "hardware package apply" tests/hardware-apply.py
run_gate "hardware package recovery" tests/hardware-recovery.py
run_gate "hardware post-install validation" tests/hardware-validation.py
run_gate "hardware package state garbage collection" tests/hardware-gc.py
run_gate "hardware enablement orchestration" tests/hardware-orchestration.py
echo "PASS: all static gates"
