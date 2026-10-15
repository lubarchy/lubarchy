"""LUBARCHY post-install hardware validation (M1-M).

Answers one question: did the qualified hardware-enablement plan actually
result in a healthy, usable system?

    fresh probe (inventory v1) + production rules -> plan v1 (the existing
    resolver; no second rules engine)
    + the validation profile (which properties each classification and
      package rule implies, derived from committed evidence)
    + normalized runtime evidence (sysfs bindings, kernel log, package and
      system health, root storage, boot identity)
    -> lubarchy.hardware.validation-result/v1

Validation is separate from detection, resolution, installation and
recovery. It is read-only: it installs, configures, loads, binds, writes and
repairs nothing, and it never changes hardware policy. Every
hardware-specific check is attributed to an inventory device identity.

Statuses (docs/hardware-validation.md):

  PASS        the property was observed and matched the expectation
  FAIL        the property was observable and did not match
  LIMITED     the environment cannot fully verify the property
  NOT_TESTED  the check was intentionally not executed

Overall status is deterministic: FAIL if any critical check fails; otherwise
LIMITED if any check is not PASS (an advisory FAIL, LIMITED or NOT_TESTED);
otherwise PASS. LIMITED and NOT_TESTED are never promoted to PASS, and a
virtual machine never proves physical CPU microcode, radio or GPU function.

This module evaluates; hardware/validate-hardware.py is the CLI. The
evaluator is a pure function of (inventory, rules, runtime evidence,
optional previous result) so it is tested on synthetic evidence; the
collector (RealRuntime) is the only part that reads the running system.
"""

import fnmatch
import hashlib
import os
import re
import stat
import subprocess

import resolver

RESULT_SCHEMA_ID = "lubarchy.hardware.validation-result/v1"
RESULT_SCHEMA_FILE = "validation-result-v1.schema.json"
PROFILE_ID = "lubarchy.hardware.validation-profile/v1"

STATUSES = ("PASS", "FAIL", "LIMITED", "NOT_TESTED")
CRITICALITIES = ("critical", "advisory")
OVERALL_EXIT = {"PASS": 0, "FAIL": 1, "LIMITED": 2, "NOT_TESTED": 2}

# Machine-readable reason codes. "ok" accompanies PASS only.
REASONS = (
    "ok", "device_missing", "device_missing_after_reboot", "device_identity_changed",
    "expected_driver_not_bound", "driver_mismatch", "required_firmware_load_failed", "optional_firmware_unavailable",
    "firmware_requirement_unclassified", "expected_interface_missing",
    "expected_wireless_interface_missing", "expected_drm_device_missing", "expected_controller_missing",
    "package_missing", "package_version_mismatch", "dpkg_unhealthy", "apt_unhealthy", "service_failed",
    "system_critical_unit_failed", "system_not_running", "system_starting", "observation_unavailable",
    "kernel_log_unavailable", "virtualized_cpu", "container_platform", "reboot_required",
    "microcode_load_failed", "microcode_state_unreported", "nvidia_not_qualified", "prerequisite_failed",
    "root_not_mounted", "root_read_only", "root_block_device_missing", "filesystem_errors",
    # limitation-only codes (never a check reason)
    "apply_journal_unreadable", "physical_function_not_exercised",
)

# ---------------------------------------------------------------------------
# Validation profile: which properties the existing plan implies. It adds no
# matching of its own; it is keyed by the plan's classifications and package
# rule ids. The firmware-family kernel modules are those of the committed,
# qualified firmware evidence (tools/hwe/firmware-mappings.json, the same
# input that generates the firmware rules); gate 13 requires them to match
# and requires every production classification and package rule to be
# covered, so a new rule cannot silently go unvalidated.

PROFILE = {
    "id": PROFILE_ID,
    "classifications": {
        "bluetooth.controller": ["present", "driver", "bluetooth"],
        "cpu.amd": [],
        "cpu.intel": [],
        "gpu.amd": ["present", "driver", "drm"],
        "gpu.intel": ["present", "driver", "drm"],
        "gpu.nvidia": ["present", "unqualified_driver"],
        "network.ethernet": ["present", "driver", "interface"],
        "network.other": ["present"],
        "network.wifi": ["present", "driver", "wireless"],
        "platform.bare-metal": [],
        "platform.container": [],
        "platform.vm": [],
        "storage.nvme": ["present", "driver"],
        "storage.sata": ["present", "driver"],
    },
    "firmware_rules": {
        "package.firmware.amd-graphics": ["amdgpu", "radeon"],
        "package.firmware.atheros-bluetooth": ["ath3k"],
        "package.firmware.atheros-wifi": ["ar5523", "ath10k_pci", "ath6kl_usb", "wil6210"],
        "package.firmware.bluez-bcm203x": ["bcm203x"],
        "package.firmware.brcm80211": ["brcmfmac"],
        "package.firmware.intel-graphics": ["i915", "xe"],
        "package.firmware.iwlwifi": ["iwl3945", "iwl4965", "iwlwifi"],
        "package.firmware.mediatek-wifi": [
            "mt7601u", "mt7615e", "mt7663u", "mt76x0e", "mt76x0u", "mt76x2e", "mt76x2u", "mt7915e", "mt7921e",
            "mt7921u", "mt7925e", "mt7925u", "mt7996e", "rt2800pci", "rt2800usb", "rt61pci", "rt73usb"],
        "package.firmware.realtek-wifi": [
            "rtl8188ee", "rtl8192ce", "rtl8192cu", "rtl8192de", "rtl8192ee", "rtl8192se", "rtl8723be", "rtl8821ae",
            "rtl8xxxu", "rtw88_8723de", "rtw88_8723du", "rtw88_8821ce", "rtw88_8821cu", "rtw88_8822be",
            "rtw88_8822bu", "rtw88_8822ce", "rtw88_8822cu", "rtw89_8851be", "rtw89_8852ae", "rtw89_8852be",
            "rtw89_8852bte", "rtw89_8852ce", "rtw89_8922ae"],
    },
    # Firmware requirement policy (M1-M): per firmware family, which requested
    # firmware files are known REQUIRED for the validated function or known
    # OPTIONAL, each with its source-controlled evidence reference. Anything not
    # listed is UNKNOWN. No requiredness is qualified yet, so every family is
    # empty: an attributed firmware request failure is LIMITED
    # (firmware_requirement_unclassified), never FAIL and never PASS, until
    # real-hardware qualification adds evidence-backed entries here.
    "firmware_requirements": {
        "package.firmware.amd-graphics": [],
        "package.firmware.atheros-bluetooth": [],
        "package.firmware.atheros-wifi": [],
        "package.firmware.bluez-bcm203x": [],
        "package.firmware.brcm80211": [],
        "package.firmware.intel-graphics": [],
        "package.firmware.iwlwifi": [],
        "package.firmware.mediatek-wifi": [],
        "package.firmware.realtek-wifi": [],
    },
    "microcode_rules": {
        "package.cpu.amd64-microcode": "AuthenticAMD",
        "package.cpu.intel-microcode": "GenuineIntel",
    },
    # A failed unit here makes the system critically unhealthy; any other
    # failed unit is reported separately as an advisory failure.
    "critical_units": [
        "basic.target", "dbus.service", "local-fs.target", "multi-user.target", "sysinit.target",
        "systemd-journald.service", "systemd-logind.service", "systemd-udevd.service",
    ],
}


def profile_digest():
    """Digest of the whole profile, including the firmware requirement policy."""
    return resolver.digest(PROFILE)


FIRMWARE_REQUIREMENTS = ("required", "optional")


def firmware_requirement(rules, file):
    """'required', 'optional' or 'unknown' for one attributed firmware request
    failure of a device matched by the given firmware-family rules. Only the
    source-controlled profile decides; an unattributable file name is unknown."""
    found = set()
    for rid in rules:
        for entry in PROFILE["firmware_requirements"].get(rid, []):
            if file is not None and fnmatch.fnmatchcase(file, entry["file"]):
                found.add(entry["requirement"])
    if len(found) > 1:
        raise ValidationError("contract_failure", "firmware requirement policy is ambiguous for a file")
    return found.pop() if found else "unknown"


class ValidationError(Exception):
    """Validation cannot produce a trustworthy result. Messages never carry
    observed values."""

    def __init__(self, code, message):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


# ---------------------------------------------------------------------------
# Normalized runtime evidence (the evaluator's only view of the system)

DEVICE_ID = re.compile(r"^(pci:[0-9a-f]{4}:[0-9a-f]{2}:[0-9a-f]{2}\.[0-7]|usb:[1-9][0-9]{0,2}-[1-9][0-9]{0,2}"
                       r"(\.[1-9][0-9]{0,2}){0,6})$")
NAME = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
MODULE = re.compile(r"^[a-z0-9_]{1,64}$")
HEX = re.compile(r"^[0-9a-f]{2,6}$")
DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
FIRMWARE_FILE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+/-]{0,127}$")
UNIT = re.compile(r"^[A-Za-z0-9:_.@*-]{1,128}\.(service|socket|mount|target|device|swap|path|timer|scope|slice|"
                  r"automount)$")
SYSTEM_STATES = ("initializing", "starting", "running", "degraded", "maintenance", "stopping", "offline",
                 "unknown")
IDENTITY_FIELDS = {"pci": ("vendor_id", "device_id", "subsystem_vendor_id", "subsystem_device_id", "class_id"),
                   "usb": ("vendor_id", "product_id")}
MAX_LIST = 4096


def _is_str(v, pattern=None):
    return isinstance(v, str) and (pattern is None or pattern.match(v) is not None)


def _is_int(v, lo=0):
    return isinstance(v, int) and not isinstance(v, bool) and v >= lo


def check_runtime(ev):
    """Strict shape check of normalized runtime evidence; raises on anything
    malformed, duplicated or ambiguous (it is never partially trusted)."""
    def bad(where):
        raise ValidationError("malformed_evidence", f"runtime evidence {where} is malformed")
    if not isinstance(ev, dict) or set(ev) != {"privilege", "boot", "devices", "kernel_log", "system", "packages",
                                               "storage", "apply_journal"}:
        bad("document")
    if ev["privilege"] not in ("root", "unprivileged"):
        bad("privilege")
    boot = ev["boot"]
    if not isinstance(boot, dict) or set(boot) != {"boot_id_digest", "boot_time"} or \
            not _is_str(boot["boot_id_digest"], DIGEST) or not _is_int(boot["boot_time"]):
        bad("boot")
    devices = ev["devices"]
    if not isinstance(devices, dict) or len(devices) > MAX_LIST:
        bad("devices")
    for did, d in devices.items():
        if not _is_str(did, DEVICE_ID) or not isinstance(d, dict) or \
                set(d) != {"present", "identity", "driver", "modules", "net", "drm", "bluetooth"}:
            bad("device")
        if not isinstance(d["present"], bool) or not isinstance(d["identity"], dict):
            bad("device")
        bus = did.split(":", 1)[0]
        if d["present"] and set(d["identity"]) - set(IDENTITY_FIELDS[bus]):
            bad("device identity")
        if any(not _is_str(v, HEX) for v in d["identity"].values()):
            bad("device identity")
        if d["driver"] is not None and not _is_str(d["driver"], NAME):
            bad("device driver")
        if not isinstance(d["modules"], list) or any(not _is_str(m, MODULE) for m in d["modules"]) or \
                d["modules"] != sorted(set(d["modules"])):
            bad("device modules")
        if not isinstance(d["net"], list) or any(not isinstance(n, dict) or set(n) != {"wireless"} or
                                                 not isinstance(n["wireless"], bool) for n in d["net"]):
            bad("device net")
        if not _is_int(d["drm"]) or not _is_int(d["bluetooth"]):
            bad("device counts")
    kl = ev["kernel_log"]
    if not isinstance(kl, dict) or set(kl) != {"available", "firmware_failures", "microcode"} or \
            not isinstance(kl["available"], bool):
        bad("kernel_log")
    if not isinstance(kl["firmware_failures"], list) or len(kl["firmware_failures"]) > MAX_LIST:
        bad("firmware_failures")
    for f in kl["firmware_failures"]:
        if not isinstance(f, dict) or set(f) != {"device", "file"} or \
                not (f["device"] is None or _is_str(f["device"], DEVICE_ID)) or \
                not (f["file"] is None or _is_str(f["file"], FIRMWARE_FILE)):
            bad("firmware failure")
    mc = kl["microcode"]
    if not isinstance(mc, dict) or set(mc) != {"loader_lines", "errors"} or not _is_int(mc["loader_lines"]) or \
            not _is_int(mc["errors"]):
        bad("microcode")
    if not kl["available"] and (kl["firmware_failures"] or mc["loader_lines"] or mc["errors"]):
        bad("kernel_log availability")
    system = ev["system"]
    if not isinstance(system, dict) or set(system) != {"state", "failed_units"} or \
            system["state"] not in SYSTEM_STATES or not isinstance(system["failed_units"], list) or \
            any(not _is_str(u, UNIT) for u in system["failed_units"]) or \
            system["failed_units"] != sorted(set(system["failed_units"])):
        bad("system")
    pk = ev["packages"]
    if not isinstance(pk, dict) or set(pk) != {"installed", "audit_clean", "check", "install_times"}:
        bad("packages")
    if not isinstance(pk["installed"], list) or not isinstance(pk["audit_clean"], bool) or \
            pk["check"] not in ("ok", "failed", "unavailable") or not isinstance(pk["install_times"], dict):
        bad("packages")
    keys = set()
    for e in pk["installed"]:
        if not isinstance(e, dict) or set(e) != {"package", "architecture", "version", "status", "automatic"} or \
                not isinstance(e["automatic"], bool) or not all(_is_str(e[k]) for k in
                                                                ("package", "architecture", "version", "status")):
            bad("installed package")
        if (e["package"], e["architecture"]) in keys:
            bad("installed package (duplicate)")
        keys.add((e["package"], e["architecture"]))
    if any(not _is_str(k, NAME) or not _is_int(v) for k, v in pk["install_times"].items()):
        bad("install_times")
    st = ev["storage"]
    if not isinstance(st, dict) or set(st) != {"mounted", "fstype", "read_only", "block_visible", "device",
                                               "fs_errors"}:
        bad("storage")
    if not isinstance(st["mounted"], bool) or not isinstance(st["read_only"], bool) or \
            not isinstance(st["block_visible"], bool) or not (st["fstype"] is None or _is_str(st["fstype"], NAME)) \
            or not (st["device"] is None or _is_str(st["device"], DEVICE_ID)) or \
            not (st["fs_errors"] is None or _is_int(st["fs_errors"])):
        bad("storage")
    aj = ev["apply_journal"]
    if aj is not None:
        if not isinstance(aj, dict) or set(aj) != {"attempt_id", "transaction_digest", "packages"} or \
                not _is_str(aj["attempt_id"], re.compile(r"^[0-9a-f]{16}$")) or \
                not _is_str(aj["transaction_digest"], DIGEST) or not isinstance(aj["packages"], dict) or \
                any(not _is_str(k, NAME) or not _is_str(v) for k, v in aj["packages"].items()):
            bad("apply_journal")
    return ev


# ---------------------------------------------------------------------------
# Evaluation (pure)

def installed_state_digest(installed):
    """The same installed-state identity the package transaction binds
    (package, architecture, version, status, automatic mark)."""
    rows = sorted(({k: e[k] for k in ("package", "architecture", "version", "status", "automatic")}
                   for e in installed), key=lambda e: (e["package"], e["architecture"]))
    return resolver.digest(rows)


def identity_of(device):
    return {k: device[k] for k in IDENTITY_FIELDS[device["bus"]] if k in device}


def _check(cid, cls, target, identity, criticality, status, reason, expected, observed):
    if status not in STATUSES or criticality not in CRITICALITIES or reason not in REASONS:
        raise ValidationError("contract_failure", f"check {cid} has an invalid status, criticality or reason")
    if (status == "PASS") != (reason == "ok"):
        raise ValidationError("contract_failure", f"check {cid}: PASS and reason 'ok' must coincide")
    return {"id": cid, "class": cls, "target": target, "identity": identity, "criticality": criticality,
            "status": status, "reason": reason, "expected": expected, "observed": observed}


def _plan_targets(plan):
    """target -> {"classifications": set, "rules": set} from the existing plan."""
    out = {}
    for c in plan["classifications"]:
        for t in c["targets"]:
            out.setdefault(t, {"classifications": set(), "rules": set()})["classifications"].add(c["name"])
    for r in plan["matched_rules"]:
        for t in r["targets"]:
            out.setdefault(t, {"classifications": set(), "rules": set()})["rules"].add(r["id"])
    return out


def _bound_modules(rt):
    return set(rt["modules"]) | ({rt["driver"].replace("-", "_")} if rt["driver"] else set())


def boot_relation(plan, ev):
    """post_reboot if every plan package is installed and the running boot started
    after the last of them was unpacked; same_boot if not; not_applicable without
    installed plan packages. Used only to decide what is authoritative."""
    names = [p["name"] for p in plan["packages"] if p["disposition"] == "require"]
    times = [ev["packages"]["install_times"].get(n) for n in names]
    if not names or any(t is None for t in times):
        return "not_applicable"
    return "post_reboot" if ev["boot"]["boot_time"] > max(times) else "same_boot"


def evaluate(inventory, rules, ev, previous=None):
    """inventory (fresh, from the trusted probe) + production rules + normalized
    runtime evidence [+ a previous result for continuity] -> validation result v1."""
    check_runtime(ev)
    plan = resolver.check_plan(resolver.resolve(inventory, rules))
    if plan["status"] not in ("resolved", "partial") or plan["conflicts"]:
        raise ValidationError("plan_rejected", f"plan status {plan['status']} cannot be validated")
    inv = resolver.normalize_inventory(inventory)
    devices = {d["id"]: d for d in inv["devices"]}
    for did in devices:
        if did not in ev["devices"]:
            raise ValidationError("malformed_evidence", "runtime evidence does not cover every inventory device")
    virt = inv["system"]["virtualization"]["kind"]
    relation = boot_relation(plan, ev)
    checks = []
    kl = ev["kernel_log"]

    # -- package state (from the plan's package requirements) --------------
    installed = {}
    for e in ev["packages"]["installed"]:
        installed.setdefault(e["package"], []).append(e)
    journal = ev["apply_journal"]
    for p in plan["packages"]:
        if p["disposition"] != "require":
            continue
        name = p["name"]
        rows = [e for e in installed.get(name, []) if e["status"] == "install ok installed"]
        expected = {"status": "install ok installed"}
        bound_version = (journal or {}).get("packages", {}).get(name)
        if bound_version is not None:
            expected["version"] = bound_version
        if not rows:
            status, reason = "FAIL", "package_missing"
            observed = {"status": installed[name][0]["status"] if name in installed else "not-installed"}
        elif bound_version is not None and all(e["version"] != bound_version for e in rows):
            status, reason, observed = "FAIL", "package_version_mismatch", {"version": rows[0]["version"]}
        else:
            status, reason, observed = "PASS", "ok", {"version": rows[0]["version"],
                                                      "version_bound": bound_version is not None}
        checks.append(_check(f"package.installed:{name}", "package", name, None, "critical", status, reason,
                             expected, observed))
    pkg_ok = {c["target"]: c["status"] == "PASS" for c in checks}

    # -- CPU microcode (package rule -> application) ------------------------
    targets = _plan_targets(plan)
    cpu_rules = sorted(set(targets.get("cpu", {}).get("rules", set())) & set(PROFILE["microcode_rules"]))
    for rid in cpu_rules:
        vendor = PROFILE["microcode_rules"][rid]
        pkg = next(p["name"] for r in rules if r["id"] == rid for p in r["effects"]["packages"])
        observed = {"vendor": inv["cpu"]["vendor"], "revision": inv["cpu"].get("microcode_revision"),
                    "virtualization": virt, "boot_relation": relation}
        if inv["cpu"]["vendor"] != vendor:
            raise ValidationError("contract_failure", "microcode rule matched a CPU of another vendor")
        if virt == "vm":
            status, reason = "LIMITED", "virtualized_cpu"
        elif virt == "container":
            status, reason = "LIMITED", "container_platform"
        elif not pkg_ok.get(pkg):
            status, reason = "NOT_TESTED", "prerequisite_failed"
        elif relation != "post_reboot":
            status, reason = "LIMITED", "reboot_required"
        elif not kl["available"]:
            status, reason = "LIMITED", "kernel_log_unavailable"
        elif kl["microcode"]["errors"]:
            status, reason = "FAIL", "microcode_load_failed"
        elif kl["microcode"]["loader_lines"] and inv["cpu"].get("microcode_revision"):
            status, reason = "PASS", "ok"
        else:
            status, reason = "LIMITED", "microcode_state_unreported"
        checks.append(_check(f"cpu.microcode:{pkg}", "cpu", "cpu", {"vendor": vendor}, "critical", status, reason,
                             {"package": pkg, "loader": "applied at boot, no loader error"}, observed))

    # -- devices (classifications and firmware rules of the plan) -----------
    for did in sorted(t for t in targets if t in devices):
        dev, rt = devices[did], ev["devices"][did]
        cls = targets[did]["classifications"]
        kinds = set()
        for c in cls:
            if c not in PROFILE["classifications"]:
                raise ValidationError("contract_failure", "plan classification has no validation profile")
            kinds.update(PROFILE["classifications"][c])
        fw_rules = sorted(targets[did]["rules"] & set(PROFILE["firmware_rules"]))
        expected_modules = sorted({m for r in fw_rules for m in PROFILE["firmware_rules"][r]})
        if fw_rules:
            kinds.update({"present", "driver", "firmware"})
        if not kinds:
            continue
        ident = identity_of(dev)
        klass = ("gpu" if any(c.startswith("gpu.") for c in cls) else "wifi" if "network.wifi" in cls
                 else "bluetooth" if "bluetooth.controller" in cls else "network" if any(
                     c.startswith("network.") for c in cls) else "storage" if any(
                     c.startswith("storage.") for c in cls) else "device")

        present = rt["present"] and rt["identity"] == ident
        if not rt["present"]:
            p_status, p_reason = "FAIL", "device_missing"
        elif rt["identity"] != ident:
            p_status, p_reason = "FAIL", "device_identity_changed"
        else:
            p_status, p_reason = "PASS", "ok"
        checks.append(_check(f"device.present:{did}", klass, did, ident, "critical", p_status, p_reason,
                             {"present": True, "identity_matches": True},
                             {"present": rt["present"], "identity_matches": present}))
        bound = _bound_modules(rt) if present else set()

        if "unqualified_driver" in kinds:
            checks.append(_check(f"device.driver:{did}", klass, did, ident, "critical", "NOT_TESTED",
                                 "nvidia_not_qualified", {"driver": "not qualified in this release"},
                                 {"driver": rt["driver"] if present else None}))
            continue
        driver_ok = False
        if "driver" in kinds:
            expected = {"modules": expected_modules} if expected_modules else {"driver": "any kernel driver"}
            observed = {"driver": rt["driver"], "modules": rt["modules"]} if present else {"driver": None}
            if not present:
                d_status, d_reason = "NOT_TESTED", "prerequisite_failed"
            elif not bound:
                d_status, d_reason = "FAIL", "expected_driver_not_bound"
            elif expected_modules and not bound & set(expected_modules):
                d_status, d_reason = "FAIL", "driver_mismatch"
            else:
                d_status, d_reason, driver_ok = "PASS", "ok", True
            checks.append(_check(f"device.driver:{did}", klass, did, ident, "critical", d_status, d_reason,
                                 expected, observed))

        if "firmware" in kinds:
            # A firmware request failure is evidence, not proof of broken hardware: FAIL only
            # for a known-required file; known-optional and unclassified failures are LIMITED.
            # Independently observable failures (device, driver, interface, DRM, controller)
            # are separate checks and are never masked by this one.
            files = sorted({f["file"] for f in kl["firmware_failures"] if f["device"] == did},
                           key=lambda f: (f is None, f or ""))
            classes = {f: firmware_requirement(fw_rules, f) for f in files}
            by_req = {r: sorted(f or "unnamed" for f, c in classes.items() if c == r)
                      for r in ("required", "optional", "unknown")}
            observed = {"kernel_log": kl["available"], "failures": len(files),
                        "required_failed": by_req["required"][:8], "optional_failed": by_req["optional"][:8],
                        "unclassified_failed": by_req["unknown"][:8]}
            if not kl["available"]:
                f_status, f_reason = "LIMITED", "kernel_log_unavailable"
            elif by_req["required"]:
                f_status, f_reason = "FAIL", "required_firmware_load_failed"
            elif by_req["unknown"]:
                f_status, f_reason = "LIMITED", "firmware_requirement_unclassified"
            elif by_req["optional"]:
                f_status, f_reason = "LIMITED", "optional_firmware_unavailable"
            elif not driver_ok:
                f_status, f_reason = "NOT_TESTED", "prerequisite_failed"
            else:
                f_status, f_reason = "PASS", "ok"
            checks.append(_check(f"device.firmware:{did}", klass, did, ident, "critical", f_status, f_reason,
                                 {"required_firmware_load_failures": 0, "rules": fw_rules}, observed))

        for kind, cid, reason, count in (
                ("drm", "gpu.drm", "expected_drm_device_missing", rt["drm"]),
                ("interface", "net.interface", "expected_interface_missing", len(rt["net"])),
                ("wireless", "net.wireless", "expected_wireless_interface_missing",
                 sum(1 for n in rt["net"] if n["wireless"])),
                ("bluetooth", "bluetooth.controller", "expected_controller_missing", rt["bluetooth"])):
            if kind not in kinds:
                continue
            if not driver_ok:
                s, r = "NOT_TESTED", "prerequisite_failed"
            elif count:
                s, r = "PASS", "ok"
            else:
                s, r = "FAIL", reason
            checks.append(_check(f"{cid}:{did}", klass, did, ident, "critical", s, r, {"count": ">=1"},
                                 {"count": count if present else 0}))

    # -- continuity with a previous result (device disappeared, boot changed) --
    previous_info = None
    if previous is not None:
        check_result(previous)
        if previous["profile"] != {"id": PROFILE_ID, "digest": profile_digest()}:
            raise ValidationError("stale_previous", "previous result was produced by another validation profile")
        boot_changed = previous["bindings"]["boot"]["boot_id_digest"] != ev["boot"]["boot_id_digest"]
        for c in previous["checks"]:
            if not c["id"].startswith("device.present:") or c["status"] != "PASS":
                continue
            did = c["target"]
            rt = ev["devices"].get(did)
            still = rt is not None and rt["present"] and rt["identity"] == c["identity"]
            reason = "ok" if still else ("device_missing_after_reboot" if boot_changed else "device_missing")
            checks.append(_check(f"device.continuity:{did}", c["class"], did, c["identity"], "critical",
                                 "PASS" if still else "FAIL", reason, {"present": True, "identity_matches": True},
                                 {"present": bool(still)}))
        previous_info = {"result_digest": resolver.digest(previous), "boot_changed": boot_changed}

    # -- storage (read-only) -----------------------------------------------
    st = ev["storage"]
    if not st["mounted"]:
        s, r = "FAIL", "root_not_mounted"
    elif st["read_only"]:
        s, r = "FAIL", "root_read_only"
    elif not st["block_visible"]:
        s, r = "FAIL", "root_block_device_missing"
    else:
        s, r = "PASS", "ok"
    checks.append(_check("storage.root", "storage", "root", None, "critical", s, r,
                         {"mounted": True, "read_write": True, "block_device": "visible"},
                         {"fstype": st["fstype"], "read_only": st["read_only"], "block_visible": st["block_visible"],
                          "device": st["device"] if st["device"] in devices else None}))
    if st["fstype"] in ("ext2", "ext3", "ext4") and st["mounted"]:
        if st["fs_errors"] is None:
            s, r = "LIMITED", "observation_unavailable"
        else:
            s, r = ("FAIL", "filesystem_errors") if st["fs_errors"] else ("PASS", "ok")
        checks.append(_check("storage.root_errors", "storage", "root", None, "advisory", s, r,
                             {"errors": 0}, {"errors": st["fs_errors"]}))

    # -- system and package-manager health -----------------------------------
    state = ev["system"]["state"]
    if state in ("running", "degraded"):
        s, r = "PASS", "ok"
    elif state in ("initializing", "starting"):
        s, r = "LIMITED", "system_starting"
    else:
        s, r = "FAIL", "system_not_running"
    checks.append(_check("system.state", "system", "system", None, "critical", s, r,
                         {"state": "running or degraded (failed units are checked separately)"}, {"state": state}))
    failed = ev["system"]["failed_units"]
    crit = [u for u in failed if u in PROFILE["critical_units"]]
    other = [u for u in failed if u not in PROFILE["critical_units"]]
    checks.append(_check("system.units.critical", "system", "system", None, "critical",
                         "FAIL" if crit else "PASS", "system_critical_unit_failed" if crit else "ok",
                         {"failed": 0}, {"failed": len(crit), "units": crit[:16]}))
    checks.append(_check("system.units.other", "system", "system", None, "advisory",
                         "FAIL" if other else "PASS", "service_failed" if other else "ok",
                         {"failed": 0}, {"failed": len(other), "units": other[:16]}))
    pk = ev["packages"]
    checks.append(_check("packages.dpkg_audit", "package-manager", "dpkg", None, "critical",
                         "PASS" if pk["audit_clean"] else "FAIL", "ok" if pk["audit_clean"] else "dpkg_unhealthy",
                         {"audit": "clean"}, {"audit_clean": pk["audit_clean"]}))
    s, r = {"ok": ("PASS", "ok"), "failed": ("FAIL", "apt_unhealthy"),
            "unavailable": ("LIMITED", "observation_unavailable")}[pk["check"]]
    checks.append(_check("packages.apt_check", "package-manager", "apt", None, "critical", s, r,
                         {"apt_check": "ok"}, {"apt_check": pk["check"]}))

    checks.sort(key=lambda c: c["id"])
    ids = [c["id"] for c in checks]
    if len(ids) != len(set(ids)):
        raise ValidationError("contract_failure", "duplicate check id")
    limitations = sorted({(c["reason"], c["status"]) for c in checks if c["status"] in ("LIMITED", "NOT_TESTED")})
    limitation_list = [{"code": code, "status": s, "checks": sum(1 for c in checks if c["reason"] == code)}
                       for code, s in limitations]
    if ev["privilege"] == "unprivileged" and journal is None:
        limitation_list.append({"code": "apply_journal_unreadable", "status": "LIMITED", "checks": 0})
    limitation_list.append({"code": "physical_function_not_exercised", "status": "NOT_TESTED", "checks": 0})
    limitation_list.sort(key=lambda x: x["code"])
    result = {
        "schema": RESULT_SCHEMA_ID,
        "profile": {"id": PROFILE_ID, "digest": profile_digest()},
        "mode": "revalidation" if previous is not None else "current",
        "privilege": ev["privilege"],
        "platform": {"architecture": inv["system"]["architecture"], "virtualization": virt,
                     "boot_firmware": inv["system"]["boot_firmware"],
                     "kernel_release": inv["system"]["kernel_release"]},
        "bindings": {
            "inventory_digest": plan["inventory_digest"],
            "rule_set_digest": plan["rule_set_digest"],
            "plan_digest": resolver.digest(plan),
            "plan_status": plan["status"],
            "installed_state_digest": installed_state_digest(ev["packages"]["installed"]),
            "package_transaction": None if journal is None else {
                "attempt_id": journal["attempt_id"], "transaction_digest": journal["transaction_digest"]},
            "boot": {"boot_id_digest": ev["boot"]["boot_id_digest"], "relation": relation},
            "previous": previous_info,
        },
        "checks": checks,
        "limitations": limitation_list,
        "unvalidated_targets": sorted(u["target"] for u in plan["unresolved"]),
        "summary": summarize(checks),
        "status": aggregate(checks),
    }
    return check_result(result)


def aggregate(checks):
    """FAIL if any critical check fails; else LIMITED if any check is not PASS;
    else PASS. LIMITED/NOT_TESTED are never promoted to PASS."""
    if any(c["status"] == "FAIL" and c["criticality"] == "critical" for c in checks):
        return "FAIL"
    if any(c["status"] != "PASS" for c in checks):
        return "LIMITED"
    return "PASS"


def summarize(checks):
    out = {s: sum(1 for c in checks if c["status"] == s) for s in STATUSES}
    out["critical_failures"] = sum(1 for c in checks if c["status"] == "FAIL" and c["criticality"] == "critical")
    out["advisory_failures"] = sum(1 for c in checks if c["status"] == "FAIL" and c["criticality"] == "advisory")
    return out


def check_result(doc):
    """Schema + semantic self-check of a result (also used for inputs)."""
    import json
    import jsonschema
    with open(os.path.join(resolver.SCHEMA_DIR, RESULT_SCHEMA_FILE), encoding="utf-8") as f:
        schema = json.load(f)
    if any(True for _ in jsonschema.Draft202012Validator(schema).iter_errors(doc)):
        raise ValidationError("malformed_result", "document does not satisfy validation-result v1")
    if doc["status"] != aggregate(doc["checks"]) or doc["summary"] != summarize(doc["checks"]):
        raise ValidationError("malformed_result", "result status or summary is inconsistent with its checks")
    if [c["id"] for c in doc["checks"]] != sorted({c["id"] for c in doc["checks"]}):
        raise ValidationError("malformed_result", "checks are not unique and canonically ordered")
    if any((c["status"] == "PASS") != (c["reason"] == "ok") for c in doc["checks"]):
        raise ValidationError("malformed_result", "PASS and reason 'ok' must coincide")
    resolver.canonical_json(doc)
    return doc


def current_bindings(inventory, rules, ev):
    check_runtime(ev)
    plan = resolver.check_plan(resolver.resolve(inventory, rules))
    return {"inventory_digest": plan.get("inventory_digest"), "rule_set_digest": plan["rule_set_digest"],
            "plan_digest": resolver.digest(plan), "installed_state_digest": installed_state_digest(
                ev["packages"]["installed"]), "boot_id_digest": ev["boot"]["boot_id_digest"],
            "profile_digest": profile_digest()}


def freshness(result, inventory, rules, ev):
    """Is a stored result still current? Any change of the bound inventory, rule set,
    plan, installed state, boot or profile makes it stale; it is never reused."""
    check_result(result)
    now = current_bindings(inventory, rules, ev)
    b = result["bindings"]
    was = {"inventory_digest": b["inventory_digest"], "rule_set_digest": b["rule_set_digest"],
           "plan_digest": b["plan_digest"], "installed_state_digest": b["installed_state_digest"],
           "boot_id_digest": b["boot"]["boot_id_digest"], "profile_digest": result["profile"]["digest"]}
    changed = sorted(k for k in now if now[k] != was[k])
    return {"current": not changed, "changed": changed, "result_status": result["status"]}


# ---------------------------------------------------------------------------
# Runtime evidence collection (the only part that reads the running system)

SYSTEMCTL = "/usr/bin/systemctl"
JOURNALCTL = "/usr/bin/journalctl"
DPKG = "/usr/bin/dpkg"
DPKG_QUERY = "/usr/bin/dpkg-query"
APT_GET = "/usr/bin/apt-get"
APT_MARK = "/usr/bin/apt-mark"
COMMAND_ENV = {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LC_ALL": "C", "LANG": "C", "LANGUAGE": "C",
               "SYSTEMD_PAGER": "", "SYSTEMD_COLORS": "0"}
COMMAND_TIMEOUT_SECONDS = 120
MAX_OUTPUT = 32 * 1024 * 1024
COMMANDS = (
    (SYSTEMCTL, "is-system-running"),
    (SYSTEMCTL, "list-units", "--failed", "--plain", "--no-legend", "--no-pager", "--full"),
    (JOURNALCTL, "--dmesg", "--boot=0", "--output=cat", "--no-pager", "--quiet"),
    (DPKG, "--audit"),
    (APT_GET, "check", "--quiet"),
    (DPKG_QUERY, "-W", "-f", "${Package}\\t${Architecture}\\t${Version}\\t${Status}\\n"),
    (APT_MARK, "showauto"),
)

FW_PATTERNS = (
    # "<driver> <bus-id>: Direct firmware load for <file> failed with error -2"
    re.compile(r"^(?P<drv>[A-Za-z0-9_.-]+) (?P<dev>[0-9a-f:.\-]+): Direct firmware load for (?P<file>\S+) failed "
               r"with error -?[0-9]+$"),
    re.compile(r"^(?P<drv>[A-Za-z0-9_.-]+) (?P<dev>[0-9a-f:.\-]+): firmware: failed to load (?P<file>\S+) "
               r"\(-?[0-9]+\)$"),
    re.compile(r"^(?P<drv>[A-Za-z0-9_.-]+) (?P<dev>[0-9a-f:.\-]+): .*(?:failed to load firmware|no suitable "
               r"firmware found|firmware load failed|Failed to load firmware)(?P<file>)"),
    re.compile(r"^Bluetooth: (?P<dev>hci[0-9]+): .*(?:[Ff]ailed to load|[Ff]irmware.*(?:not found|failed))"
               r".*?(?P<file>)$"),
)
MICROCODE_LOADER = re.compile(r"^microcode: (?:Current revision|Updated early|updated early|Microcode Update "
                              r"Driver|sig=)")
MICROCODE_ERROR = re.compile(r"^microcode: .*(?:fail|[Ee]rror|mismatch|[Ii]nvalid)")


def check_command(argv):
    if tuple(argv) not in COMMANDS:
        raise ValidationError("contract_failure", "command is not an allowlisted read-only validation command")


def run_command(argv):
    """Production runner: fixed argv only, literal environment, shell=False, no stdin."""
    check_command(argv)
    try:
        r = subprocess.run(list(argv), env=dict(COMMAND_ENV), shell=False, stdin=subprocess.DEVNULL,
                           capture_output=True, timeout=COMMAND_TIMEOUT_SECONDS, check=False)
    except FileNotFoundError:
        return None, b""
    except (OSError, subprocess.TimeoutExpired):
        return None, b""
    return r.returncode, r.stdout[:MAX_OUTPUT] + r.stderr[:4096]


def parse_kernel_log(text, token_to_id):
    """Kernel log -> availability, attributed firmware failures and microcode loader
    facts. Raw lines are never kept; only the device id and a bounded file name."""
    lines = text.splitlines()
    available = any(line.startswith("Linux version ") for line in lines)
    if not available:
        return {"available": False, "firmware_failures": [], "microcode": {"loader_lines": 0, "errors": 0}}
    failures = set()
    loader = errors = 0
    for line in lines:
        if line.startswith("microcode: "):
            if MICROCODE_ERROR.match(line):
                errors += 1
            elif MICROCODE_LOADER.match(line):
                loader += 1
            continue
        for pattern in FW_PATTERNS:
            m = pattern.match(line)
            if m:
                f = m.group("file") or None
                if f is not None and not FIRMWARE_FILE.match(f):
                    f = None
                failures.add((token_to_id(m.group("dev")), f))
                break
    return {"available": True,
            "firmware_failures": [{"device": d, "file": f} for d, f in sorted(failures, key=lambda x: (
                x[0] or "", x[1] or ""))][:MAX_LIST],
            "microcode": {"loader_lines": loader, "errors": errors}}


class RealRuntime:
    """Reads the running system. Roots and runner are injection points for
    the synthetic collector tests; the CLI uses the real ones. Nothing is
    written, loaded, bound or changed."""

    def __init__(self, sys_root="/sys", proc_root="/proc", root="/", runner=run_command, euid=os.geteuid):
        self.sys = os.path.realpath(sys_root)
        self.proc = os.path.realpath(proc_root)
        self.root = os.path.realpath(root)
        self.runner = runner
        self.euid = euid

    def _run(self, argv):
        check_command(argv)
        return self.runner(list(argv))

    def _read(self, path, limit=65536):
        try:
            with open(path, "rb") as f:
                data = f.read(limit + 1)
        except OSError:
            return None
        if len(data) > limit:
            return None
        return data.decode("ascii", "replace").strip()

    def _link(self, path):
        try:
            return os.path.basename(os.readlink(path))
        except OSError:
            return None

    def _device_dirs(self, inventory):
        out = {}
        for d in inventory["devices"]:
            bus = "pci" if d["bus"] == "pci" else "usb"
            out[d["id"]] = os.path.join(self.sys, "bus", bus, "devices", d["locator"])
        return out

    def _owner(self, real, by_real):
        """Nearest inventory device at or above a sysfs path (runtime evidence only)."""
        path = real
        devices_root = os.path.join(self.sys, "devices")
        while path.startswith(devices_root + os.sep):
            if path in by_real:
                return by_real[path]
            path = os.path.dirname(path)
        return None

    def collect(self, inventory, plan_packages):
        dirs = self._device_dirs(inventory)
        by_real, devices = {}, {}
        for d in inventory["devices"]:
            path = dirs[d["id"]]
            present = os.path.isdir(path)
            real = os.path.realpath(path) if present else None
            if real is not None:
                if real in by_real:
                    raise ValidationError("malformed_evidence", "two inventory devices share one sysfs device")
                by_real[real] = d["id"]
            ident = {}
            if present:
                if d["bus"] == "pci":
                    for field, attr, width in (("vendor_id", "vendor", 4), ("device_id", "device", 4),
                                               ("subsystem_vendor_id", "subsystem_vendor", 4),
                                               ("subsystem_device_id", "subsystem_device", 4),
                                               ("class_id", "class", 6)):
                        v = self._read(os.path.join(path, attr))
                        if v is not None and re.fullmatch(r"0x[0-9a-fA-F]{%d}" % width, v):
                            ident[field] = v[2:].lower()
                else:
                    for field, attr in (("vendor_id", "idVendor"), ("product_id", "idProduct")):
                        v = self._read(os.path.join(path, attr))
                        if v is not None and re.fullmatch(r"[0-9a-fA-F]{4}", v):
                            ident[field] = v.lower()
            driver = self._link(os.path.join(path, "driver")) if present else None
            modules = set()
            if present:
                m = self._link(os.path.join(path, "driver", "module"))
                if m:
                    modules.add(m.replace("-", "_"))
                if d["bus"] == "usb":
                    for child in sorted(os.listdir(path)):
                        if re.fullmatch(re.escape(d["locator"]) + r":[0-9]+\.[0-9]+", child):
                            m = self._link(os.path.join(path, child, "driver", "module"))
                            if m:
                                modules.add(m.replace("-", "_"))
                            dn = self._link(os.path.join(path, child, "driver"))
                            if dn:
                                modules.add(dn.replace("-", "_"))
            devices[d["id"]] = {"present": present, "identity": ident,
                                "driver": driver if driver and NAME.match(driver) else None,
                                "modules": sorted(m for m in modules if MODULE.match(m)),
                                "net": [], "drm": 0, "bluetooth": 0}
        hci = {}
        for cls, key in (("net", "net"), ("drm", "drm"), ("bluetooth", "bluetooth")):
            base = os.path.join(self.sys, "class", cls)
            try:
                names = sorted(os.listdir(base))
            except OSError:
                names = []
            for n in names:
                if cls == "drm" and not re.fullmatch(r"card[0-9]+", n):
                    continue
                if cls == "bluetooth" and not re.fullmatch(r"hci[0-9]+", n):
                    continue
                dev = os.path.join(base, n, "device")
                if not os.path.exists(dev):
                    continue                    # virtual interfaces have no device
                owner = self._owner(os.path.realpath(dev), by_real)
                if owner is None:
                    continue
                if key == "net":
                    wireless = os.path.isdir(os.path.join(base, n, "wireless")) or \
                        os.path.exists(os.path.join(base, n, "phy80211"))
                    devices[owner]["net"].append({"wireless": wireless})
                else:
                    devices[owner][key] += 1
                    if key == "bluetooth":
                        hci[n] = owner
        for d in devices.values():
            d["net"].sort(key=lambda n: n["wireless"])

        def token_to_id(tok):
            if re.fullmatch(r"[0-9a-f]{4}:[0-9a-f]{2}:[0-9a-f]{2}\.[0-7]", tok):
                did = "pci:" + tok
            elif tok in hci:
                return hci[tok]
            else:
                did = "usb:" + tok.split(":", 1)[0]
            return did if did in devices else None

        rc, out = self._run(COMMANDS[2])
        kernel_log = parse_kernel_log(out.decode("utf-8", "replace") if rc == 0 else "", token_to_id)

        rc, out = self._run(COMMANDS[0])
        state = out.decode("ascii", "replace").strip().splitlines()[:1]
        state = state[0] if state and state[0] in SYSTEM_STATES else "unknown"
        rc, out = self._run(COMMANDS[1])
        failed = set()
        for line in out.decode("utf-8", "replace").splitlines() if rc == 0 else []:
            unit = line.split()[0] if line.split() else ""
            unit = re.sub(r"@[^.]+\.", "@*.", unit)          # no instance names (user ids, devices)
            if UNIT.match(unit):
                failed.add(unit)

        rc, out = self._run(COMMANDS[3])
        audit_clean = rc == 0 and not out.strip()
        rc, out = self._run(COMMANDS[4])
        text = out.decode("utf-8", "replace")
        if rc == 0:
            check = "ok"
        elif rc is None or "Permission denied" in text or "are you root" in text:
            check = "unavailable"
        else:
            check = "failed"
        rc, out = self._run(COMMANDS[5])
        if rc != 0:
            raise ValidationError("observation_failed", "dpkg-query could not list installed packages")
        rows = []
        for line in out.decode("utf-8", "replace").splitlines():
            parts = line.split("\t")
            if len(parts) != 4 or parts[3].split()[-1:] == ["not-installed"]:
                continue
            rows.append({"package": parts[0], "architecture": parts[1], "version": parts[2], "status": parts[3]})
        rc, out = self._run(COMMANDS[6])
        auto = set(out.decode("utf-8", "replace").split()) if rc == 0 else set()
        native = inventory["system"]["architecture"]
        for e in rows:
            e["automatic"] = e["package"] in auto or f"{e['package']}:{e['architecture']}" in auto
        times = {}
        for name in plan_packages:
            for fn in (f"{name}.list", f"{name}:{native}.list"):
                try:
                    times[name] = int(os.stat(os.path.join(self.root, "var/lib/dpkg/info", fn)).st_mtime)
                    break
                except OSError:
                    continue

        storage = self._root_storage(by_real)
        boot_id = self._read(os.path.join(self.proc, "sys/kernel/random/boot_id"))
        btime = 0
        stat_text = self._read(os.path.join(self.proc, "stat"), limit=4 << 20) or ""
        for line in stat_text.splitlines():
            if line.startswith("btime "):
                btime = int(line.split()[1])
        if not boot_id or not btime:
            raise ValidationError("observation_failed", "boot identity is not observable")
        ev = {
            "privilege": "root" if self.euid() == 0 else "unprivileged",
            "boot": {"boot_id_digest": "sha256:" + hashlib.sha256(boot_id.encode()).hexdigest(), "boot_time": btime},
            "devices": devices,
            "kernel_log": kernel_log,
            "system": {"state": state, "failed_units": sorted(failed)},
            "packages": {"installed": sorted(rows, key=lambda e: (e["package"], e["architecture"])),
                         "audit_clean": audit_clean, "check": check, "install_times": times},
            "storage": storage,
            "apply_journal": self._apply_journal() if self.euid() == 0 else None,
        }
        return check_runtime(ev)

    def _root_storage(self, by_real):
        st = {"mounted": False, "fstype": None, "read_only": False, "block_visible": False, "device": None,
              "fs_errors": None}
        text = self._read(os.path.join(self.proc, "self/mountinfo"), limit=4 << 20) or ""
        for line in text.splitlines():
            left, sep, right = line.partition(" - ")
            f = left.split()
            if not sep or len(f) < 6 or f[4] != "/":
                continue
            fstype = right.split()[0] if right.split() else None
            st["mounted"] = True
            st["fstype"] = fstype if fstype and NAME.match(fstype) else None
            st["read_only"] = "ro" in f[5].split(",")
            majmin = f[2]
            block = os.path.join(self.sys, "dev", "block", majmin)
            if re.fullmatch(r"[0-9]+:[0-9]+", majmin) and os.path.exists(block):
                st["block_visible"] = True
                real = os.path.realpath(block)
                st["device"] = self._owner(real, by_real)
                if fstype in ("ext2", "ext3", "ext4"):
                    v = self._read(os.path.join(self.sys, "fs", "ext4", os.path.basename(real), "errors_count"))
                    st["fs_errors"] = int(v) if v is not None and v.isdigit() else None
        return st

    def _apply_journal(self):
        """Root only: the latest complete LUBARCHY apply attempt (exact versions)."""
        import json
        base = os.path.join(self.root, "var/lib/lubarchy/package-transactions")
        try:
            names = sorted(os.listdir(base))
        except OSError:
            return None
        best = None
        for n in names:
            p = os.path.join(base, n, "journal.json")
            try:
                st = os.lstat(p)
                if not stat.S_ISREG(st.st_mode):
                    continue
                with open(p, "rb") as f:
                    doc = json.loads(f.read(1 << 20))
            except (OSError, ValueError):
                continue
            if doc.get("phase") != "complete" or doc.get("result") != "complete" or not doc.get("post_state"):
                continue
            key = (doc["phases"][-1]["at"], n)
            if best is None or key > best[0]:
                best = (key, doc)
        if best is None:
            return None
        doc = best[1]
        return {"attempt_id": doc["attempt_id"], "transaction_digest": doc["digests"]["transaction_digest"],
                "packages": {p["package"]: p["version"] for p in doc["post_state"]["packages"]}}
