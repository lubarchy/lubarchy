#!/usr/bin/python3
"""LUBARCHY read-only hardware probe (M1-B).

Observes the running Linux system and prints one canonical
lubarchy.hardware.inventory/v1 JSON document to stdout, followed by one
newline. Observation only: it resolves nothing, installs nothing, loads or
binds nothing, causes no udev event, writes no file or sysfs attribute and
needs no root.

Every fact comes from a fixed, allowlisted source:

- /proc/cpuinfo (allowlisted CPU fields only)
- os.uname() (CPU architecture, kernel release)
- /sys/firmware/efi and exactly one EFI variable (SecureBoot)
- /sys/bus/pci/devices and /sys/bus/usb/devices (allowlisted attributes and
  the kernel-managed driver/module links only)
- /usr/bin/dpkg --print-architecture
- /usr/bin/systemd-detect-virt --container | --vm
- /usr/bin/udevadm info, for the MODALIAS property of one device only

Standard library only. Exit status 0 on success with an empty stderr; on a
probe failure nothing is written to stdout, a short error code goes to
stderr and the exit status is 1.
"""

import json
import os
import re
import subprocess
import sys

INVENTORY_SCHEMA_ID = "lubarchy.hardware.inventory/v1"

# The only executables the probe may start, with fixed arguments.
DPKG = "/usr/bin/dpkg"
DETECT_VIRT = "/usr/bin/systemd-detect-virt"
UDEVADM = "/usr/bin/udevadm"
COMMAND_TIMEOUT_SECONDS = 10
COMMAND_ENV = {"LC_ALL": "C", "LANG": "C", "PATH": "/usr/bin:/bin"}

SECURE_BOOT_VARIABLE = "SecureBoot-8be4df61-93ca-11d2-aa0d-00e098032b8c"

# Read allowlists. No other attribute is ever read.
PCI_ATTRIBUTES = frozenset({
    "vendor", "device", "subsystem_vendor", "subsystem_device", "class", "modalias",
})
USB_DEVICE_ATTRIBUTES = frozenset({
    "idVendor", "idProduct", "bDeviceClass", "bDeviceSubClass", "bDeviceProtocol",
    "bConfigurationValue",
})
USB_INTERFACE_ATTRIBUTES = frozenset({
    "bInterfaceNumber", "bInterfaceClass", "bInterfaceSubClass", "bInterfaceProtocol",
    "modalias",
})
# /proc/cpuinfo field -> inventory field. Other fields are discarded unread.
CPUINFO_FIELDS = {
    "vendor_id": "vendor",
    "cpu family": "family",
    "model": "model",
    "stepping": "stepping",
    "model name": "model_name",
    "microcode": "microcode_revision",
}

MAX_ATTRIBUTE_BYTES = 4096
MAX_CPUINFO_BYTES = 8 * 1024 * 1024
MAX_EFI_VARIABLE_BYTES = 64

# Value patterns of inventory v1 (tests/hardware-probe.py checks each against
# hardware/schemas/inventory-v1.schema.json).
PATTERNS = {
    "kernel_release": r"^[0-9A-Za-z][0-9A-Za-z.+_~-]{0,63}$",
    "cpu_vendor": r"^[!-~]{1,12}$",
    "model_name": r"^[ -~]+$",
    "microcode_revision": r"^0x(0|[1-9a-f][0-9a-f]{0,15})$",
    "pci_locator": r"^[0-9a-f]{4}:[0-9a-f]{2}:[0-9a-f]{2}\.[0-7]$",
    "usb_locator": r"^[1-9][0-9]{0,2}-[1-9][0-9]{0,2}(\.[1-9][0-9]{0,2}){0,6}$",
    "pci_modalias": r"^pci:v[0-9A-F]{8}d[0-9A-F]{8}sv[0-9A-F]{8}sd[0-9A-F]{8}bc[0-9A-F]{2}sc[0-9A-F]{2}i[0-9A-F]{2}$",
    "usb_interface_modalias": r"^usb:v[0-9A-F]{4}p[0-9A-F]{4}d[0-9A-F]{4}dc[0-9A-F]{2}dsc[0-9A-F]{2}dp[0-9A-F]{2}ic[0-9A-F]{2}isc[0-9A-F]{2}ip[0-9A-F]{2}in[0-9A-F]{2}$",
    "driver": r"^[A-Za-z0-9_.-]{1,64}$",
    "module": r"^[a-z0-9_]{1,64}$",
    "virtualization_technology": r"^[a-z0-9]+(-[a-z0-9]+)*$",
}
MAX_MODEL_NAME = 128
MAX_TECHNOLOGY = 32
MAX_CPU_INTEGER = 65535

ERROR_CODES = frozenset({
    "unsupported_architecture",
    "tool_missing",
    "tool_failed",
    "cpu_unavailable",
    "malformed_platform_fact",
    "malformed_sysfs_fact",
    "unsupported_topology",
    "duplicate_device_id",
    "invalid_topology",
    "udev_sysfs_mismatch",
    "contract_failure",
})


class ProbeError(Exception):
    """An expected probe failure. Messages never carry observed values."""

    def __init__(self, code, message):
        if code not in ERROR_CODES:
            raise ValueError(f"unknown probe error code {code!r}")
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


def _matches(kind, value):
    return isinstance(value, str) and re.fullmatch(PATTERNS[kind], value) is not None


# ---------------------------------------------------------------------------
# Commands: fixed builders, one checker, one runner.


def dpkg_architecture_command():
    return [DPKG, "--print-architecture"]


def detect_virt_command(kind):
    if kind not in ("container", "vm"):
        raise ValueError("virtualization kind must be 'container' or 'vm'")
    return [DETECT_VIRT, "--" + kind]


def udev_modalias_command(syspath):
    """Query the MODALIAS property of exactly one device. Nothing else."""
    argv = [UDEVADM, "info", "--query=property", "--property=MODALIAS", "--value",
            "--path=" + syspath]
    check_command(argv)
    return argv


def check_command(argv):
    """Reject every command except the three fixed, read-only shapes."""
    if not isinstance(argv, list) or not all(isinstance(a, str) for a in argv):
        raise ValueError("command must be a list of strings")
    if argv == [DPKG, "--print-architecture"]:
        return
    if argv in ([DETECT_VIRT, "--container"], [DETECT_VIRT, "--vm"]):
        return
    if len(argv) == 6 and argv[:5] == [UDEVADM, "info", "--query=property", "--property=MODALIAS", "--value"]:
        path = argv[5][len("--path="):] if argv[5].startswith("--path=") else ""
        if (path.startswith("/") and os.path.normpath(path) == path and "/devices/" in path
                and "\n" not in path and "\0" not in path):
            return
    raise ValueError("command is not one of the approved read-only probe commands")


def run_command(argv):
    """Production runner: the only place the probe starts a process."""
    check_command(argv)
    if not os.access(argv[0], os.X_OK):
        raise ProbeError("tool_missing", f"required tool {argv[0]} is not available")
    try:
        proc = subprocess.run(
            argv,
            shell=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env=dict(COMMAND_ENV),
            timeout=COMMAND_TIMEOUT_SECONDS,
            check=False,
        )
    except subprocess.TimeoutExpired:
        raise ProbeError("tool_failed", f"{argv[0]} timed out") from None
    except OSError:
        raise ProbeError("tool_failed", f"{argv[0]} could not be run") from None
    return proc.returncode, proc.stdout


# ---------------------------------------------------------------------------
# Reading


def _read_bytes(path, limit):
    with open(path, "rb") as f:
        return f.read(limit + 1)


def _hex(text, width, prefixed):
    """Kernel hex value -> fixed-width lowercase hex, or None if malformed."""
    if text is None:
        return None
    if prefixed:
        if not text.startswith("0x"):
            return None
        text = text[2:]
    if re.fullmatch(r"[0-9a-fA-F]{%d}" % width, text) is None:
        return None
    return text.lower()


def canonical_json(value):
    """Canonical JSON (M1-A contract): UTF-8, sorted keys, no whitespace."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                      allow_nan=False).encode("utf-8")


class Probe:
    """Collects inventory v1 from a sysfs/procfs root. The roots, runner and
    uname are injection points for synthetic tests; the CLI always uses
    /sys, /proc, run_command and os.uname."""

    def __init__(self, sys_root="/sys", proc_root="/proc", runner=run_command, uname=os.uname):
        self._sys = os.path.realpath(sys_root)
        self._proc = os.path.realpath(proc_root)
        self._runner = runner
        self._uname = uname
        self.udev_stats = {"queried": 0, "agreed": 0, "absent": 0}

    def _run(self, argv):
        check_command(argv)
        return self._runner(argv)

    # -- attributes -------------------------------------------------------

    def _attribute(self, directory, name, allowed, label):
        """Read one allowlisted sysfs attribute. None if the file is absent."""
        if name not in allowed:
            raise ValueError(f"attribute {name!r} is not allowlisted")
        try:
            data = _read_bytes(os.path.join(directory, name), MAX_ATTRIBUTE_BYTES)
        except FileNotFoundError:
            return None
        except OSError:
            raise ProbeError("malformed_sysfs_fact", f"{label}: attribute {name} unreadable") from None
        if len(data) > MAX_ATTRIBUTE_BYTES:
            raise ProbeError("malformed_sysfs_fact", f"{label}: attribute {name} too large")
        try:
            text = data.decode("ascii")
        except UnicodeDecodeError:
            raise ProbeError("malformed_sysfs_fact", f"{label}: attribute {name} not ASCII") from None
        return text[:-1] if text.endswith("\n") else text

    def _link_name(self, path, label, kind):
        """Basename of a kernel-managed symlink (driver, driver/module)."""
        try:
            target = os.readlink(path)
        except (FileNotFoundError, NotADirectoryError):
            return None
        except OSError:
            # Not a symlink or unreadable: not a driver/module link.
            return None
        name = os.path.basename(target)
        if kind == "module":
            name = name.replace("-", "_")
        if not _matches(kind, name):
            raise ProbeError("malformed_sysfs_fact", f"{label}: {kind} name not representable")
        return name

    def _driver_and_modules(self, device_dir, label):
        out = {}
        driver = self._link_name(os.path.join(device_dir, "driver"), label, "driver")
        if driver is not None:
            out["driver"] = driver
            module = self._link_name(os.path.join(device_dir, "driver", "module"), label, "module")
            if module is not None:
                out["modules"] = [module]
        return out

    def _modalias(self, device_dir, kind, label, allowed):
        value = self._attribute(device_dir, "modalias", allowed, label)
        if value is None:
            return None
        if not _matches(kind, value):
            raise ProbeError("malformed_sysfs_fact", f"{label}: modalias not representable")
        self._udev_agrees(device_dir, value, label)
        return value

    def _udev_agrees(self, device_dir, sysfs_value, label):
        """sysfs is primary; a udev MODALIAS, when present, must be identical."""
        returncode, out = self._run(udev_modalias_command(device_dir))
        self.udev_stats["queried"] += 1
        if returncode != 0:
            self.udev_stats["absent"] += 1
            return
        try:
            value = out.decode("ascii")
        except UnicodeDecodeError:
            raise ProbeError("udev_sysfs_mismatch", f"{label}: udev MODALIAS not ASCII") from None
        value = value[:-1] if value.endswith("\n") else value
        if value == "":
            self.udev_stats["absent"] += 1
            return
        if value != sysfs_value:
            raise ProbeError("udev_sysfs_mismatch", f"{label}: udev and sysfs MODALIAS differ")
        self.udev_stats["agreed"] += 1

    def _real_device_dir(self, link, label):
        real = os.path.realpath(link)
        if not real.startswith(os.path.join(self._sys, "devices") + os.sep):
            raise ProbeError("invalid_topology", f"{label}: device outside the sysfs device tree")
        return real

    def _list(self, *parts):
        try:
            return sorted(os.listdir(os.path.join(self._sys, *parts)))
        except FileNotFoundError:
            return []

    # -- platform ---------------------------------------------------------

    def system(self):
        uname = self._uname()
        if uname.machine != "x86_64":
            raise ProbeError("unsupported_architecture", "CPU architecture is not x86_64")
        returncode, out = self._run(dpkg_architecture_command())
        if returncode != 0:
            raise ProbeError("tool_failed", f"{DPKG} --print-architecture failed")
        if out.decode("ascii", "replace").strip() != "amd64":
            raise ProbeError("unsupported_architecture", "Debian architecture is not amd64")
        if not _matches("kernel_release", uname.release):
            raise ProbeError("malformed_platform_fact", "kernel release not representable")
        efi = os.path.isdir(os.path.join(self._sys, "firmware", "efi"))
        return {
            "architecture": "amd64",
            "kernel_release": uname.release,
            "boot_firmware": "uefi" if efi else "bios",
            "secure_boot": self._secure_boot() if efi else "unknown",
            "virtualization": self._virtualization(),
        }

    def _secure_boot(self):
        """Exactly one EFI variable: 4 attribute bytes, then 1 data byte."""
        path = os.path.join(self._sys, "firmware", "efi", "efivars", SECURE_BOOT_VARIABLE)
        try:
            data = _read_bytes(path, MAX_EFI_VARIABLE_BYTES)
        except OSError:
            return "unknown"
        if len(data) != 5:
            return "unknown"
        return {0: "disabled", 1: "enabled"}.get(data[4], "unknown")

    def _virtualization(self):
        for kind in ("container", "vm"):
            returncode, out = self._run(detect_virt_command(kind))
            value = out.decode("ascii", "replace").strip()
            if returncode == 0 and value and value != "none":
                if not _matches("virtualization_technology", value) or len(value) > MAX_TECHNOLOGY:
                    raise ProbeError("malformed_platform_fact", "virtualization technology not representable")
                return {"kind": kind, "technology": value}
            if returncode == 0 or value != "none":
                raise ProbeError("tool_failed", f"{DETECT_VIRT} --{kind} gave no usable answer")
        return {"kind": "none"}

    # -- CPU --------------------------------------------------------------

    def cpu(self):
        uname = self._uname()
        if uname.machine != "x86_64":
            raise ProbeError("unsupported_architecture", "CPU architecture is not x86_64")
        try:
            data = _read_bytes(os.path.join(self._proc, "cpuinfo"), MAX_CPUINFO_BYTES)
            text = data.decode("utf-8")
        except (OSError, UnicodeDecodeError):
            raise ProbeError("cpu_unavailable", "/proc/cpuinfo unreadable") from None
        if len(data) > MAX_CPUINFO_BYTES:
            raise ProbeError("cpu_unavailable", "/proc/cpuinfo too large")
        blocks = []
        for chunk in re.split(r"\n\s*\n", text):
            block = {}
            for line in chunk.splitlines():
                key, sep, value = line.partition(":")
                key = key.strip()
                if sep and key in CPUINFO_FIELDS:
                    block[CPUINFO_FIELDS[key]] = value.strip()
            if block:
                blocks.append(block)
        vendors = {b.get("vendor") for b in blocks}
        if not blocks or None in vendors:
            raise ProbeError("cpu_unavailable", "CPU vendor not reported")
        if len(vendors) != 1:
            raise ProbeError("cpu_unavailable", "CPU vendor differs between logical CPUs")
        vendor = vendors.pop()
        if not _matches("cpu_vendor", vendor):
            raise ProbeError("cpu_unavailable", "CPU vendor not representable")
        cpu = {"architecture": "x86_64", "vendor": vendor}

        def consistent(field):
            values = {b.get(field) for b in blocks}
            return values.pop() if len(values) == 1 else None

        for field in ("family", "model", "stepping"):
            value = consistent(field)
            if value is not None and re.fullmatch(r"[0-9]{1,5}", value) and int(value) <= MAX_CPU_INTEGER:
                cpu[field] = int(value)
        name = consistent("model_name")
        if name is not None and len(name) <= MAX_MODEL_NAME and _matches("model_name", name):
            cpu["model_name"] = name
        microcode = consistent("microcode_revision")
        if microcode is not None and re.fullmatch(r"0x[0-9a-fA-F]{1,16}", microcode):
            cpu["microcode_revision"] = "0x%x" % int(microcode, 16)
        return cpu

    # -- PCI --------------------------------------------------------------

    def pci_devices(self):
        devices = []
        for name in self._list("bus", "pci", "devices"):
            label = f"pci:{name}"
            if not _matches("pci_locator", name):
                raise ProbeError("malformed_sysfs_fact", "PCI device name not representable")
            real = self._real_device_dir(os.path.join(self._sys, "bus", "pci", "devices", name), label)

            def attr(n, real=real, label=label):
                return self._attribute(real, n, PCI_ATTRIBUTES, label)

            dev = {"id": label, "bus": "pci", "locator": name}
            for field, attribute, width, required in (
                ("vendor_id", "vendor", 4, True),
                ("device_id", "device", 4, True),
                ("subsystem_vendor_id", "subsystem_vendor", 4, False),
                ("subsystem_device_id", "subsystem_device", 4, False),
                ("class_id", "class", 6, True),
            ):
                raw = attr(attribute)
                if raw is None and not required:
                    continue
                value = _hex(raw, width, prefixed=True)
                if value is None:
                    raise ProbeError("malformed_sysfs_fact", f"{label}: {attribute} not representable")
                dev[field] = value
            modalias = self._modalias(real, "pci_modalias", label, PCI_ATTRIBUTES)
            if modalias is not None:
                dev["modalias"] = modalias
            dev.update(self._driver_and_modules(real, label))
            devices.append((real, dev))
        return devices

    # -- USB --------------------------------------------------------------

    def usb_devices(self):
        devices = []
        for name in self._list("bus", "usb", "devices"):
            if ":" in name or re.fullmatch(r"usb[0-9]+", name):
                continue  # interfaces are read per device; root hubs are not devices
            label = f"usb:{name}"
            if not _matches("usb_locator", name):
                raise ProbeError("unsupported_topology", "USB device locator not representable in inventory v1")
            real = self._real_device_dir(os.path.join(self._sys, "bus", "usb", "devices", name), label)

            def attr(n, real=real, label=label):
                return self._attribute(real, n, USB_DEVICE_ATTRIBUTES, label)

            dev = {"id": label, "bus": "usb", "locator": name}
            for field, attribute, width, required in (
                ("vendor_id", "idVendor", 4, True),
                ("product_id", "idProduct", 4, True),
                ("device_class", "bDeviceClass", 2, False),
                ("device_subclass", "bDeviceSubClass", 2, False),
                ("device_protocol", "bDeviceProtocol", 2, False),
            ):
                raw = attr(attribute)
                if raw is None and not required:
                    continue
                value = _hex(raw, width, prefixed=False)
                if value is None:
                    raise ProbeError("malformed_sysfs_fact", f"{label}: {attribute} not representable")
                dev[field] = value
            driver = self._link_name(os.path.join(real, "driver"), label, "driver")
            if driver is not None:
                dev["driver"] = driver
            interfaces = self._usb_interfaces(real, name, attr("bConfigurationValue"), label)
            if interfaces:
                dev["interfaces"] = interfaces
            devices.append((real, dev))
        return devices

    def _usb_interfaces(self, device_dir, name, configuration, label):
        """Interfaces '<name>:<active configuration>.<n>' of this device only."""
        if configuration in (None, ""):
            return []
        if re.fullmatch(r"[1-9][0-9]{0,2}", configuration) is None:
            raise ProbeError("malformed_sysfs_fact", f"{label}: bConfigurationValue not representable")
        pattern = re.escape(f"{name}:{configuration}.") + r"[0-9]{1,3}"
        interfaces = []
        for child in sorted(os.listdir(device_dir)):
            if re.fullmatch(pattern, child) is None:
                continue
            itf_dir = os.path.join(device_dir, child)
            itf_label = f"{label} interface"

            def attr(n, itf_dir=itf_dir, itf_label=itf_label):
                return self._attribute(itf_dir, n, USB_INTERFACE_ATTRIBUTES, itf_label)

            itf = {}
            for field, attribute in (("number", "bInterfaceNumber"), ("class", "bInterfaceClass"),
                                     ("subclass", "bInterfaceSubClass"), ("protocol", "bInterfaceProtocol")):
                value = _hex(attr(attribute), 2, prefixed=False)
                if value is None:
                    raise ProbeError("malformed_sysfs_fact", f"{itf_label}: {attribute} not representable")
                itf[field] = value
            modalias = self._modalias(itf_dir, "usb_interface_modalias", itf_label, USB_INTERFACE_ATTRIBUTES)
            if modalias is not None:
                itf["modalias"] = modalias
            itf.update(self._driver_and_modules(itf_dir, itf_label))
            interfaces.append(itf)
        numbers = [itf["number"] for itf in interfaces]
        if len(numbers) != len(set(numbers)):
            raise ProbeError("duplicate_device_id", f"{label}: duplicate USB interface number")
        return sorted(interfaces, key=lambda itf: itf["number"])

    # -- assembly ---------------------------------------------------------

    def devices(self):
        observed = self.pci_devices() + self.usb_devices()
        by_path = {}
        ids = set()
        for real, dev in observed:
            if dev["id"] in ids or real in by_path:
                raise ProbeError("duplicate_device_id", f"{dev['id']}: observed twice")
            ids.add(dev["id"])
            by_path[real] = dev["id"]
        devices_root = os.path.join(self._sys, "devices")
        for real, dev in observed:
            # Nearest ancestor that is itself an inventory device (root hubs
            # and other kernel devices in between are skipped).
            path = os.path.dirname(real)
            while path.startswith(devices_root + os.sep):
                if path in by_path:
                    dev["parent"] = by_path[path]
                    break
                path = os.path.dirname(path)
            if dev.get("parent") == dev["id"]:
                raise ProbeError("invalid_topology", f"{dev['id']}: device is its own parent")
        return sorted((dev for _, dev in observed), key=lambda dev: dev["id"])

    def inventory(self):
        doc = {
            "schema": INVENTORY_SCHEMA_ID,
            "system": self.system(),
            "cpu": self.cpu(),
            "devices": self.devices(),
        }
        check_inventory(doc)
        return doc


def check_inventory(doc):
    """Final contract self-check (semantic rules of inventory v1)."""
    ids = [dev["id"] for dev in doc["devices"]]
    if len(ids) != len(set(ids)):
        raise ProbeError("duplicate_device_id", "duplicate device id")
    if ids != sorted(ids):
        raise ProbeError("contract_failure", "devices not in canonical order")
    for dev in doc["devices"]:
        if dev["id"] != f"{dev['bus']}:{dev['locator']}":
            raise ProbeError("contract_failure", f"{dev['id']}: id does not match locator")
        parent = dev.get("parent")
        if parent is not None and (parent not in ids or parent == dev["id"]):
            raise ProbeError("invalid_topology", f"{dev['id']}: parent not in inventory")
        numbers = [itf["number"] for itf in dev.get("interfaces", [])]
        if numbers != sorted(set(numbers)):
            raise ProbeError("contract_failure", f"{dev['id']}: interfaces not canonical")
    try:
        canonical_json(doc)
    except (TypeError, ValueError):
        raise ProbeError("contract_failure", "inventory is not canonical JSON") from None


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if argv:
        sys.stderr.write("usage: probe.py (takes no arguments)\n")
        return 2
    try:
        output = canonical_json(Probe().inventory()) + b"\n"
    except ProbeError as e:
        sys.stderr.write(f"probe: error: {e.code}: {e.message}\n")
        return 1
    sys.stdout.buffer.write(output)
    sys.stdout.buffer.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
