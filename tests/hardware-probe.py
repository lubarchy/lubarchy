#!/usr/bin/python3
"""LUBARCHY M1-B hardware probe gate (synthetic only).

Exercises hardware/probe.py against synthetic sysfs/procfs trees built in a
private temporary directory, with a fake command runner. It never probes the
host: no real /sys or /proc is read, no command is run, and nothing needs
root, network or hardware. Needs python3 and python3-jsonschema (Debian main)
for the inventory-v1 contract checks.
"""

import sys

sys.dont_write_bytecode = True

import ast  # noqa: E402
import collections  # noqa: E402
import contextlib  # noqa: E402
import importlib.util  # noqa: E402
import io  # noqa: E402
import os  # noqa: E402
import pathlib  # noqa: E402
import shutil  # noqa: E402
import tempfile  # noqa: E402

import hwe_reference as ref  # noqa: E402

TESTS = pathlib.Path(__file__).resolve().parent
PROBE_PATH = ref.REPO_ROOT / "hardware" / "probe.py"


def _load_probe():
    spec = importlib.util.spec_from_file_location("lubarchy_probe", PROBE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


probe = _load_probe()

Uname = collections.namedtuple("Uname", "sysname nodename release version machine")
SB_DISABLED = b"\x06\x00\x00\x00\x00"
SB_ENABLED = b"\x06\x00\x00\x00\x01"


class Failure(Exception):
    pass


def expect(cond, message):
    if not cond:
        raise Failure(message)


def expect_probe_error(code, fn, message):
    try:
        fn()
    except probe.ProbeError as e:
        expect(e.code == code, f"{message}: got {e.code}, want {code}")
        return e
    raise Failure(f"{message}: no ProbeError")


CHECKS = []


def check(fn):
    CHECKS.append(fn)
    return fn


# ---------------------------------------------------------------------------
# Synthetic system


def uname(machine="x86_64", release="6.12.0-test-amd64"):
    return Uname("Linux", "synthetic", release, "#1", machine)


def pci_modalias(ven, dev, sv, sd, cls):
    u = lambda x: x.upper().rjust(8, "0")
    return "pci:v%sd%ssv%ssd%sbc%ssc%si%s" % (u(ven), u(dev), u(sv), u(sd), cls[0:2].upper(),
                                            cls[2:4].upper(), cls[4:6].upper())


def usb_modalias(ven, prod, dc, dsc, dp, ic, isc, ip, num):
    return "usb:v%sp%sd0100dc%sdsc%sdp%sic%sisc%sip%sin%s" % tuple(
        x.upper() for x in (ven, prod, dc, dsc, dp, ic, isc, ip, num))


class Tree:
    """A synthetic /sys + /proc pair laid out like the kernel's."""

    def __init__(self, root):
        self.root = pathlib.Path(root)
        self.sys = self.root / "sys"
        self.proc = self.root / "proc"
        for d in ("devices", "bus/pci/devices", "bus/pci/drivers", "bus/usb/devices",
                  "bus/usb/drivers", "module"):
            (self.sys / d).mkdir(parents=True, exist_ok=True)
        self.proc.mkdir(parents=True, exist_ok=True)

    def cpuinfo(self, blocks):
        text = "\n".join("".join(f"{k}\t: {v}\n" for k, v in b) for b in blocks) + "\n"
        (self.proc / "cpuinfo").write_text(text, encoding="utf-8")

    def efi(self, secure_boot=SB_DISABLED):
        d = self.sys / "firmware" / "efi" / "efivars"
        d.mkdir(parents=True, exist_ok=True)
        if secure_boot is not None:
            (d / probe.SECURE_BOOT_VARIABLE).write_bytes(secure_boot)

    @staticmethod
    def _attrs(d, attrs):
        for k, v in attrs.items():
            (d / k).write_text(v + "\n", encoding="ascii")

    def _driver(self, d, bus, driver, module):
        if driver is None:
            return
        drv = self.sys / "bus" / bus / "drivers" / driver
        drv.mkdir(parents=True, exist_ok=True)
        if module is not None and not (drv / "module").exists():
            (self.sys / "module" / module).mkdir(parents=True, exist_ok=True)
            os.symlink(self.sys / "module" / module, drv / "module")
        os.symlink(drv, d / "driver")

    def pci(self, parent, name, ven, dev, cls, sv=None, sd=None, driver=None, module=None, modalias=True):
        d = pathlib.Path(parent) / name
        d.mkdir(parents=True, exist_ok=True)
        attrs = {"vendor": "0x" + ven, "device": "0x" + dev, "class": "0x" + cls}
        if sv:
            attrs.update(subsystem_vendor="0x" + sv, subsystem_device="0x" + sd)
        if modalias:
            attrs["modalias"] = pci_modalias(ven, dev, sv or "0000", sd or "0000", cls)
        # Attributes the probe must never read.
        attrs.update(config="SECRET-CONFIG", resource="SECRET-RESOURCE", label="SECRET-LABEL")
        self._attrs(d, attrs)
        self._driver(d, "pci", driver, module)
        os.symlink(d, self.sys / "bus" / "pci" / "devices" / name)
        return d

    def pci_root(self, domain="0000", bus="00"):
        d = self.sys / "devices" / f"pci{domain}:{bus}"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def usb_root_hub(self, controller, n):
        d = pathlib.Path(controller) / f"usb{n}"
        d.mkdir()
        self._attrs(d, {"idVendor": "1d6b", "idProduct": "0002", "bDeviceClass": "09",
                        "bDeviceSubClass": "00", "bDeviceProtocol": "01", "bConfigurationValue": "1",
                        "serial": "SECRET-SERIAL-ROOT", "product": "SECRET-PRODUCT"})
        self._driver(d, "usb", "usb", None)
        os.symlink(d, self.sys / "bus" / "usb" / "devices" / f"usb{n}")
        itf = d / f"{n}-0:1.0"
        itf.mkdir()
        self._attrs(itf, {"bInterfaceNumber": "00", "bInterfaceClass": "09", "bInterfaceSubClass": "00",
                          "bInterfaceProtocol": "00",
                          "modalias": usb_modalias("1d6b", "0002", "09", "00", "01", "09", "00", "00", "00")})
        self._driver(itf, "usb", "hub", None)
        os.symlink(itf, self.sys / "bus" / "usb" / "devices" / itf.name)
        return d

    def usb_device(self, parent, name, ven, prod, dc, dsc, dp, config="1", driver="usb"):
        d = pathlib.Path(parent) / name
        d.mkdir()
        attrs = {"idVendor": ven, "idProduct": prod, "bDeviceClass": dc, "bDeviceSubClass": dsc,
                 "bDeviceProtocol": dp, "bConfigurationValue": config,
                 "serial": "SECRET-SERIAL-0001", "manufacturer": "SECRET-MANUFACTURER",
                 "product": "SECRET-PRODUCT"}
        self._attrs(d, attrs)
        (d / "descriptors").write_bytes(b"SECRET-DESCRIPTORS")
        self._driver(d, "usb", driver, None)
        os.symlink(d, self.sys / "bus" / "usb" / "devices" / name)
        self._usb = getattr(self, "_usb", {})
        self._usb[name] = (ven, prod, dc, dsc, dp)
        return d

    def usb_interface(self, device, config, number, cls, sub, proto, driver=None, module=None, modalias=True):
        device = pathlib.Path(device)
        name = f"{device.name}:{config}.{int(number, 16)}"
        d = device / name
        d.mkdir()
        attrs = {"bInterfaceNumber": number, "bInterfaceClass": cls, "bInterfaceSubClass": sub,
                 "bInterfaceProtocol": proto, "interface": "SECRET-INTERFACE-STRING"}
        if modalias:
            ven, prod, dc, dsc, dp = self._usb[device.name]
            attrs["modalias"] = usb_modalias(ven, prod, dc, dsc, dp, cls, sub, proto, number)
        self._attrs(d, attrs)
        self._driver(d, "usb", driver, module)
        os.symlink(d, self.sys / "bus" / "usb" / "devices" / name)
        return d


INTEL_CPU = [("processor", "0"), ("vendor_id", "GenuineIntel"), ("cpu family", "6"), ("model", "186"),
             ("model name", "Synthetic  Intel(R) Probe CPU @ 2.00GHz"), ("stepping", "3"),
             ("microcode", "0x0000411C"), ("flags", "fpu vme de"), ("bogomips", "4000.00")]


def cpu_blocks(n=4, base=INTEL_CPU, **override):
    blocks = []
    for i in range(n):
        block = [(k, str(i) if k == "processor" else v) for k, v in base]
        for key, values in override.items():
            key = key.replace("_", " ") if key != "vendor_id" else key
            block = [(k, values[i] if k == key else v) for k, v in block]
        blocks.append(block)
    return blocks


def workstation(root, order=1):
    """PCI root port -> GPU, xHCI -> root hub -> USB hub -> Bluetooth device."""
    t = Tree(root)
    t.cpuinfo(cpu_blocks())
    t.efi(SB_DISABLED)
    r = t.pci_root()
    steps = [
        lambda: t.pci(r, "0000:00:00.0", "8086", "f0a0", "060000", "8086", "f1a0"),
        lambda: t.pci(r, "0000:00:01.0", "8086", "f0b0", "060400", driver="pcieport", modalias=False),
        lambda: t.pci(r, "0000:00:14.0", "8086", "f008", "0c0330", "8086", "f108", "xhci_hcd", "xhci_pci"),
        lambda: t.pci(r / "0000:00:01.0", "0000:01:00.0", "10DE", "F003", "030000", "10de", "f103",
                      "nouveau", "nouveau"),
        lambda: t.pci(r, "0000:00:1f.3", "8086", "f0c0", "040300", "8086", "f1c0", "snd_hda_intel",
                      "snd-hda-intel"),
    ]
    for step in steps[::order]:
        step()
    hub_root = t.usb_root_hub(r / "0000:00:14.0", 1)
    t.usb_root_hub(r / "0000:00:14.0", 2)
    hub = t.usb_device(hub_root, "1-2", "f0e1", "f0e2", "09", "00", "02", driver="usb")
    t.usb_interface(hub, "1", "00", "09", "00", "00", "hub", None)
    bt = t.usb_device(hub, "1-2.1", "8087", "f009", "e0", "01", "01")
    interfaces = [("01", "e0", "01", "01"), ("00", "e0", "01", "01")]
    for number, cls, sub, proto in interfaces[::order]:
        t.usb_interface(bt, "1", number, cls, sub, proto, "btusb", "btusb")
    # A stale interface of an inactive configuration must be ignored.
    t.usb_interface(bt, "2", "00", "ff", "ff", "ff", None, None)
    # An unconfigured device has no interfaces.
    t.usb_device(hub_root, "1-3", "f0f1", "f0f2", "00", "00", "00", config="", driver="usb")
    return t


class FakeRunner:
    """Emulates the three approved commands. Records every argv."""

    def __init__(self, arch="amd64", container=None, vm="kvm", udev="agree", overrides=None, missing=()):
        self.arch, self.container, self.vm, self.udev = arch, container, vm, udev
        self.overrides = overrides or {}
        self.missing = set(missing)
        self.calls = []

    def __call__(self, argv):
        probe.check_command(argv)
        self.calls.append(tuple(argv))
        exe = argv[0]
        if exe in self.missing:
            raise probe.ProbeError("tool_missing", f"required tool {exe} is not available")
        if exe == probe.DPKG:
            return 0, (self.arch + "\n").encode()
        if exe == probe.DETECT_VIRT:
            value = self.container if argv[1] == "--container" else self.vm
            return (0, (value + "\n").encode()) if value else (1, b"none\n")
        path = argv[-1][len("--path="):]
        if path in self.overrides:
            return self.overrides[path]
        if self.udev == "absent":
            return 0, b"\n"
        modalias = pathlib.Path(path) / "modalias"
        return 0, modalias.read_bytes() if modalias.exists() else b"\n"


def run_probe(t, runner=None, **kw):
    runner = runner or FakeRunner()
    p = probe.Probe(sys_root=t.sys, proc_root=t.proc, runner=runner, uname=kw.get("uname", uname))
    return p.inventory(), p, runner


def by_id(doc):
    return {d["id"]: d for d in doc["devices"]}


@contextlib.contextmanager
def scratch():
    d = tempfile.mkdtemp(prefix="lubarchy-probe-test-")
    try:
        yield pathlib.Path(d)
    finally:
        shutil.rmtree(d)


def standard():
    """Probe the standard workstation tree; return (doc, probe, runner)."""
    with scratch() as d:
        return run_probe(workstation(d))


# ---------------------------------------------------------------------------
# Platform


@check
def amd64_architecture_mapping():
    doc, _, runner = standard()
    expect(doc["system"]["architecture"] == "amd64", "system architecture")
    expect(doc["cpu"]["architecture"] == "x86_64", "cpu architecture")
    expect((probe.DPKG, "--print-architecture") in runner.calls, "dpkg --print-architecture not used")
    expect(doc["system"]["kernel_release"] == "6.12.0-test-amd64", "kernel release from uname")
    return "dpkg amd64 + uname x86_64 -> amd64/x86_64; kernel release from uname"


@check
def unsupported_architecture_fails_closed():
    with scratch() as d:
        t = workstation(d)
        for arch in ("i386", "arm64", "", "amd64 extra"):
            expect_probe_error("unsupported_architecture", lambda a=arch: run_probe(t, FakeRunner(arch=a)),
                               f"dpkg {arch!r}")
        for machine in ("aarch64", "i686"):
            expect_probe_error("unsupported_architecture",
                               lambda m=machine: run_probe(t, uname=lambda: uname(machine=m)), f"uname {machine}")
        expect_probe_error("malformed_platform_fact",
                           lambda: run_probe(t, uname=lambda: uname(release="6.12 custom")), "kernel release")
    return "non-amd64 dpkg or non-x86_64 kernel -> unsupported_architecture"


@check
def required_tool_missing_fails_closed():
    with scratch() as d:
        t = workstation(d)
        for exe in (probe.DPKG, probe.DETECT_VIRT, probe.UDEVADM):
            expect_probe_error("tool_missing", lambda e=exe: run_probe(t, FakeRunner(missing=[e])), exe)
    with scratch() as d:
        missing = str(pathlib.Path(d) / "no-such-tool")
        saved = probe.DPKG
        try:
            probe.DPKG = missing
            expect_probe_error("tool_missing", lambda: probe.run_command([missing, "--print-architecture"]),
                               "production runner with absent executable")
        finally:
            probe.DPKG = saved
    return "absent dpkg/systemd-detect-virt/udevadm -> tool_missing, nothing emitted"


@check
def cpu_parsing():
    doc, _, _ = standard()
    expect(doc["cpu"] == {"architecture": "x86_64", "vendor": "GenuineIntel", "family": 6, "model": 186,
                          "stepping": 3, "model_name": "Synthetic  Intel(R) Probe CPU @ 2.00GHz",
                          "microcode_revision": "0x411c"}, f"cpu {doc['cpu']}")
    return "vendor, family, model, stepping, model name, microcode parsed from allowlisted fields"


@check
def inconsistent_cpu_scalars_are_omitted():
    with scratch() as d:
        t = workstation(d)
        t.cpuinfo(cpu_blocks(model=["186", "186", "187", "186"], microcode=["0x411c", "0x411c", "0x411d", "0x411c"],
                             stepping=["3", "unknown", "3", "3"]))
        doc, _, _ = run_probe(t)
        for field in ("model", "microcode_revision", "stepping"):
            expect(field not in doc["cpu"], f"inconsistent {field} was emitted")
        expect(doc["cpu"]["family"] == 6, "consistent family must stay")
        t.cpuinfo(cpu_blocks(vendor_id=["GenuineIntel", "AuthenticAMD", "GenuineIntel", "GenuineIntel"]))
        expect_probe_error("cpu_unavailable", lambda: run_probe(t), "inconsistent vendor")
        t.cpuinfo([[("processor", "0"), ("model", "1")]])
        expect_probe_error("cpu_unavailable", lambda: run_probe(t), "missing vendor")
        (t.proc / "cpuinfo").unlink()
        expect_probe_error("cpu_unavailable", lambda: run_probe(t), "missing /proc/cpuinfo")
        t.cpuinfo(cpu_blocks(model_name=["Café CPU"] * 4))
        doc, _, _ = run_probe(t)
        expect("model_name" not in doc["cpu"], "non-ASCII model name emitted")
    return "disagreeing model/stepping/microcode omitted; vendor disagreement or absence -> cpu_unavailable"


@check
def microcode_normalization():
    with scratch() as d:
        t = workstation(d)
        for raw, want in (("0x0000411C", "0x411c"), ("0x0", "0x0"), ("0x00000000", "0x0"), ("0xA0000F0", "0xa0000f0")):
            t.cpuinfo(cpu_blocks(microcode=[raw] * 4))
            got = run_probe(t)[0]["cpu"].get("microcode_revision")
            expect(got == want, f"microcode {raw} -> {got}, want {want}")
        for raw in ("411c", "0x", "0xZZ", "0x11112222333344445"):
            t.cpuinfo(cpu_blocks(microcode=[raw] * 4))
            expect("microcode_revision" not in run_probe(t)[0]["cpu"], f"malformed microcode {raw} emitted")
    return "0x + lowercase hex without leading zeros; malformed revisions omitted"


@check
def uefi_firmware_detection():
    doc, _, _ = standard()
    expect(doc["system"]["boot_firmware"] == "uefi", "UEFI not detected")
    return "/sys/firmware/efi present -> uefi"


@check
def bios_firmware_detection():
    with scratch() as d:
        t = workstation(d)
        shutil.rmtree(t.sys / "firmware")
        doc, _, _ = run_probe(t)
        expect(doc["system"]["boot_firmware"] == "bios" and doc["system"]["secure_boot"] == "unknown", "BIOS")
    return "no /sys/firmware/efi -> bios, Secure Boot unknown"


@check
def secure_boot_states():
    with scratch() as d:
        t = workstation(d)
        var = t.sys / "firmware" / "efi" / "efivars" / probe.SECURE_BOOT_VARIABLE
        cases = [(SB_ENABLED, "enabled"), (SB_DISABLED, "disabled"), (b"\x06\x00\x00\x00\x02", "unknown"),
                 (b"\x06\x00\x00\x00", "unknown"), (b"\x06\x00\x00\x00\x01\x00", "unknown"), (b"", "unknown"),
                 (None, "unknown")]
        for data, want in cases:
            if var.exists():
                var.unlink()
            if data is not None:
                var.write_bytes(data)
            got = run_probe(t)[0]["system"]["secure_boot"]
            expect(got == want, f"SecureBoot {data!r} -> {got}, want {want}")
        var.write_bytes(SB_ENABLED)
        var.chmod(0)
        if os.geteuid() != 0:
            got = run_probe(t)[0]["system"]["secure_boot"]
            expect(got == "unknown", f"unreadable variable -> {got}")
        var.chmod(0o644)
        # Other EFI variables are never consulted.
        (var.parent / "SecureBoot-00000000-0000-0000-0000-000000000000").write_bytes(SB_ENABLED)
        var.write_bytes(SB_DISABLED)
        expect(run_probe(t)[0]["system"]["secure_boot"] == "disabled", "a different variable was used")
    return "data byte 1 -> enabled, 0 -> disabled; absent/short/long/other/unreadable -> unknown"


@check
def virtualization_observation():
    with scratch() as d:
        t = workstation(d)
        doc, _, runner = run_probe(t, FakeRunner(vm="kvm"))
        expect(doc["system"]["virtualization"] == {"kind": "vm", "technology": "kvm"}, "vm")
        expect(runner.calls[1:3] == [(probe.DETECT_VIRT, "--container"), (probe.DETECT_VIRT, "--vm")],
               "container must be checked before vm")
        doc, _, _ = run_probe(t, FakeRunner(container="systemd-nspawn", vm="kvm"))
        expect(doc["system"]["virtualization"] == {"kind": "container", "technology": "systemd-nspawn"}, "container")
        doc, _, _ = run_probe(t, FakeRunner(vm=None))
        expect(doc["system"]["virtualization"] == {"kind": "none"}, "bare metal")
        expect_probe_error("malformed_platform_fact", lambda: run_probe(t, FakeRunner(vm="KVM Guest")), "bad id")
    return "--container, then --vm, else none; technology validated"


# ---------------------------------------------------------------------------
# PCI


@check
def pci_enumeration_and_normalization():
    doc, _, _ = standard()
    devs = by_id(doc)
    pci = [i for i in devs if i.startswith("pci:")]
    expect(pci == ["pci:0000:00:00.0", "pci:0000:00:01.0", "pci:0000:00:14.0", "pci:0000:00:1f.3",
                   "pci:0000:01:00.0"], f"pci ids {pci}")
    gpu = devs["pci:0000:01:00.0"]
    expect(gpu["vendor_id"] == "10de" and gpu["device_id"] == "f003", "uppercase sysfs IDs not lowercased")
    expect(gpu["class_id"] == "030000" and gpu["subsystem_vendor_id"] == "10de", "class/subsystem")
    bridge = devs["pci:0000:00:01.0"]
    expect("subsystem_vendor_id" not in bridge and "modalias" not in bridge, "absent facts must stay absent")
    expect(bridge["locator"] == "0000:00:01.0", "locator")
    return "5 PCI functions; IDs 4-digit lowercase, class 6-digit; absent subsystem/modalias stay absent"


@check
def malformed_pci_facts_fail_closed():
    with scratch() as d:
        t = workstation(d)
        dev = t.sys / "devices" / "pci0000:00" / "0000:00:00.0"
        for attr, value in (("vendor", "8086"), ("vendor", "0x80866"), ("class", "0x0300"), ("device", "0xZZZZ")):
            saved = (dev / attr).read_text()
            (dev / attr).write_text(value + "\n")
            expect_probe_error("malformed_sysfs_fact", lambda: run_probe(t), f"{attr}={value}")
            (dev / attr).write_text(saved)
        (dev / "vendor").unlink()
        expect_probe_error("malformed_sysfs_fact", lambda: run_probe(t), "missing vendor")
    with scratch() as d:
        t = workstation(d)
        os.symlink(t.sys / "devices" / "pci0000:00" / "0000:00:00.0", t.sys / "bus" / "pci" / "devices" / "bogus")
        expect_probe_error("malformed_sysfs_fact", lambda: run_probe(t), "non-PCI-address entry")
    with scratch() as d:
        t = workstation(d)
        outside = t.root / "elsewhere"
        outside.mkdir()
        os.symlink(outside, t.sys / "bus" / "pci" / "devices" / "0000:09:00.0")
        expect_probe_error("invalid_topology", lambda: run_probe(t), "device outside /sys/devices")
    return "unprefixed/oversized/short/non-hex/missing IDs, bogus names, out-of-tree links fail closed"


@check
def modalias_preserved_byte_for_byte():
    doc, _, _ = standard()
    gpu = by_id(doc)["pci:0000:01:00.0"]
    expect(gpu["modalias"] == "pci:v000010DEd0000F003sv000010DEsd0000F103bc03sc00i00", f"modalias {gpu['modalias']}")
    with scratch() as d:
        t = workstation(d)
        gpu_dir = t.sys / "devices" / "pci0000:00" / "0000:00:01.0" / "0000:01:00.0"
        (gpu_dir / "modalias").write_text(gpu["modalias"].lower() + "\n")
        expect_probe_error("malformed_sysfs_fact", lambda: run_probe(t), "lowercased modalias accepted")
    return "kernel modalias kept as written (uppercase hex); altered forms rejected"


@check
def driver_and_module_observation():
    doc, _, _ = standard()
    devs = by_id(doc)
    expect(devs["pci:0000:01:00.0"].get("driver") == "nouveau" and devs["pci:0000:01:00.0"].get("modules") == ["nouveau"],
           "driver/module")
    expect(devs["pci:0000:00:14.0"]["driver"] == "xhci_hcd" and devs["pci:0000:00:14.0"]["modules"] == ["xhci_pci"],
           "driver name differs from module name")
    expect(devs["pci:0000:00:1f.3"]["modules"] == ["snd_hda_intel"], "module '-' not normalized to '_'")
    expect(devs["pci:0000:00:01.0"]["driver"] == "pcieport" and "modules" not in devs["pci:0000:00:01.0"],
           "built-in driver has no module")
    expect("driver" not in devs["pci:0000:00:00.0"], "unbound device has no driver")
    return "driver/module from symlink basenames only; built-in and unbound handled"


@check
def pci_bridge_parent_topology():
    doc, _, _ = standard()
    devs = by_id(doc)
    expect(devs["pci:0000:01:00.0"].get("parent") == "pci:0000:00:01.0", "GPU parent must be its root port")
    expect("parent" not in devs["pci:0000:00:01.0"], "root-complex devices have no parent")
    return "PCI function behind a bridge -> parent is the bridge"


# ---------------------------------------------------------------------------
# USB


@check
def usb_root_hubs_excluded():
    doc, _, _ = standard()
    ids = [d["id"] for d in doc["devices"]]
    expect(not any(i.startswith("usb:usb") or ":0:" in i or i.endswith("-0") for i in ids), f"root hub in {ids}")
    expect([i for i in ids if i.startswith("usb:")] == ["usb:1-2", "usb:1-2.1", "usb:1-3"], f"usb ids {ids}")
    return "usb1/usb2 and their interfaces are not inventory devices"


@check
def usb_device_enumeration():
    doc, _, _ = standard()
    bt = by_id(doc)["usb:1-2.1"]
    expect({k: bt[k] for k in ("vendor_id", "product_id", "device_class", "device_subclass", "device_protocol",
                               "driver", "locator")}
           == {"vendor_id": "8087", "product_id": "f009", "device_class": "e0", "device_subclass": "01",
               "device_protocol": "01", "driver": "usb", "locator": "1-2.1"}, f"bt {bt}")
    expect("interfaces" not in by_id(doc)["usb:1-3"], "unconfigured device must have no interfaces")
    with scratch() as d:
        t = workstation(d)
        dev = t.sys / "bus" / "usb" / "devices" / "1-2.1"
        (dev / "idVendor").write_text("0x8087\n")
        expect_probe_error("malformed_sysfs_fact", lambda: run_probe(t), "prefixed idVendor")
    return "3 USB devices with vendor/product/class fields; unconfigured device has no interfaces"


@check
def active_usb_interfaces_only():
    doc, _, _ = standard()
    itfs = by_id(doc)["usb:1-2.1"]["interfaces"]
    expect([i["number"] for i in itfs] == ["00", "01"], f"interfaces {itfs}")
    expect(all(i["class"] == "e0" for i in itfs), "inactive configuration interface was included")
    expect(itfs[0] == {"number": "00", "class": "e0", "subclass": "01", "protocol": "01",
                       "modalias": "usb:v8087pF009d0100dcE0dsc01dp01icE0isc01ip01in00", "driver": "btusb",
                       "modules": ["btusb"]}, f"interface {itfs[0]}")
    hub = by_id(doc)["usb:1-2"]["interfaces"]
    expect(hub == [{"number": "00", "class": "09", "subclass": "00", "protocol": "00", "driver": "hub",
                    "modalias": "usb:vF0E1pF0E2d0100dc09dsc00dp02ic09isc00ip00in00"}], f"hub interface {hub}")
    return "only '<dev>:<bConfigurationValue>.<n>' interfaces, sorted by number, with class values and drivers"


@check
def usb_parent_topology():
    doc, _, _ = standard()
    devs = by_id(doc)
    expect(devs["usb:1-2"]["parent"] == "pci:0000:00:14.0", "USB device under the root hub -> xHCI controller")
    expect(devs["usb:1-2.1"]["parent"] == "usb:1-2", "USB device behind a hub -> the hub")
    expect(devs["usb:1-3"]["parent"] == "pci:0000:00:14.0", "second device under root hub")
    return "xHCI -> USB device (root hub skipped); USB hub -> child device"


@check
def unsupported_usb_topology_fails_closed():
    with scratch() as d:
        t = workstation(d)
        hub = t.sys / "bus" / "usb" / "devices" / "1-2"
        t.usb_device(os.path.realpath(hub), "1-2.1.1.1.1.1.1.1", "f0aa", "f0ab", "00", "00", "00")
        expect_probe_error("unsupported_topology", lambda: run_probe(t), "8-level USB chain")
    return "USB locators beyond inventory v1 -> unsupported_topology (no truncation)"


# ---------------------------------------------------------------------------
# udev


@check
def udev_modalias_agreement():
    doc, p, runner = standard()
    udev = [c for c in runner.calls if c[0] == probe.UDEVADM]
    expect(len(udev) == 4 + 3, f"expected one MODALIAS query per sysfs modalias, got {len(udev)}")
    for c in udev:
        expect(c[:5] == (probe.UDEVADM, "info", "--query=property", "--property=MODALIAS", "--value"), f"argv {c}")
    expect(p.udev_stats == {"queried": 7, "agreed": 7, "absent": 0}, f"stats {p.udev_stats}")
    return "7 device-specific MODALIAS queries, all agreeing with sysfs"


@check
def udev_absent_modalias_is_valid():
    with scratch() as d:
        t = workstation(d)
        doc, p, _ = run_probe(t, FakeRunner(udev="absent"))
        expect(by_id(doc)["pci:0000:01:00.0"]["modalias"].startswith("pci:v000010DE"), "sysfs modalias lost")
        expect(p.udev_stats == {"queried": 7, "agreed": 0, "absent": 7}, f"stats {p.udev_stats}")
        gpu = str(t.sys / "devices" / "pci0000:00" / "0000:00:01.0" / "0000:01:00.0")
        doc, p, _ = run_probe(t, FakeRunner(overrides={gpu: (1, b"")}))
        expect(p.udev_stats["absent"] == 1, "non-zero udevadm exit counted as absent")
    return "udev without MODALIAS (or unknown device) -> sysfs value used"


@check
def udev_sysfs_mismatch_fails_closed():
    with scratch() as d:
        t = workstation(d)
        gpu = str(t.sys / "devices" / "pci0000:00" / "0000:00:01.0" / "0000:01:00.0")
        other = b"pci:v000010DEd0000F004sv000010DEsd0000F103bc03sc00i00\n"
        e = expect_probe_error("udev_sysfs_mismatch", lambda: run_probe(t, FakeRunner(overrides={gpu: (0, other)})),
                               "conflicting MODALIAS")
        expect("F004" not in str(e) and "F003" not in str(e), "error message leaks observed values")
        bt_itf = os.path.realpath(t.sys / "bus" / "usb" / "devices" / "1-2.1:1.0")
        expect_probe_error("udev_sysfs_mismatch",
                           lambda: run_probe(t, FakeRunner(overrides={bt_itf: (0, b"usb:v0000p0000\n")})),
                           "conflicting interface MODALIAS")
    return "udev/sysfs MODALIAS disagreement -> udev_sysfs_mismatch, values not echoed"


@check
def command_builder_cannot_dump_udev():
    path = "/sys/devices/pci0000:00/0000:00:02.0"
    expect(probe.udev_modalias_command(path) == [probe.UDEVADM, "info", "--query=property", "--property=MODALIAS",
                                                 "--value", "--path=" + path], "udev command shape")
    rejected = [
        [probe.UDEVADM, "info", "--export-db"],
        [probe.UDEVADM, "info", "--query=all", "--path=" + path],
        [probe.UDEVADM, "info", "--query=property", "--path=" + path],
        [probe.UDEVADM, "info", "--query=property", "--property=ID_SERIAL", "--value", "--path=" + path],
        [probe.UDEVADM, "info", "--query=property", "--property=MODALIAS", "--path=" + path],
        [probe.UDEVADM, "info", "--attribute-walk", "--path=" + path],
        [probe.UDEVADM, "trigger"],
        [probe.UDEVADM, "test", path],
        [probe.UDEVADM, "control", "--reload"],
        [probe.UDEVADM, "info", "--query=property", "--property=MODALIAS", "--value", "--path=" + path, "--export-db"],
        [probe.UDEVADM, "info", "--query=property", "--property=MODALIAS", "--value", "--path=devices/x"],
        [probe.UDEVADM, "info", "--query=property", "--property=MODALIAS", "--value", "--path=/sys/devices/../class"],
        [probe.UDEVADM, "info", "--query=property", "--property=MODALIAS", "--value", "--path=/sys/class/net/eth0"],
        ["udevadm", "info", "--query=property", "--property=MODALIAS", "--value", "--path=" + path],
        [probe.DPKG, "-l"],
        [probe.DPKG, "--print-architecture", "--foreign"],
        [probe.DETECT_VIRT],
        [probe.DETECT_VIRT, "--list"],
        ["/bin/sh", "-c", "true"],
        "/usr/bin/dpkg --print-architecture",
    ]
    for argv in rejected:
        try:
            probe.check_command(argv)
        except ValueError:
            continue
        raise Failure(f"command accepted: {argv!r}")
    for bad in ("/sys/devices/../class/net", "relative/devices/x", "/sys/class/net/eth0"):
        try:
            probe.udev_modalias_command(bad)
        except ValueError:
            continue
        raise Failure(f"udev_modalias_command accepted {bad!r}")
    try:
        probe.detect_virt_command("list")
    except ValueError:
        pass
    else:
        raise Failure("detect_virt_command accepted 'list'")
    return f"{len(rejected)} non-approved command shapes rejected; only MODALIAS of one /devices path can be queried"


# ---------------------------------------------------------------------------
# Privacy and static safety


FORBIDDEN_ATTRIBUTES = {"serial", "manufacturer", "product", "descriptors", "bos_descriptors", "address",
                        "config", "resource", "rom", "label", "uevent", "wwid", "eui", "nguid", "uuid",
                        "product_uuid", "product_serial", "board_serial", "chassis_serial", "interface"}


@check
def allowlists_exclude_forbidden_attributes():
    expect(probe.PCI_ATTRIBUTES == {"vendor", "device", "subsystem_vendor", "subsystem_device", "class", "modalias"},
           "PCI allowlist changed")
    expect(probe.USB_DEVICE_ATTRIBUTES == {"idVendor", "idProduct", "bDeviceClass", "bDeviceSubClass",
                                           "bDeviceProtocol", "bConfigurationValue"}, "USB device allowlist changed")
    expect(probe.USB_INTERFACE_ATTRIBUTES == {"bInterfaceNumber", "bInterfaceClass", "bInterfaceSubClass",
                                              "bInterfaceProtocol", "modalias"}, "USB interface allowlist changed")
    expect(set(probe.CPUINFO_FIELDS) == {"vendor_id", "cpu family", "model", "stepping", "model name", "microcode"},
           "cpuinfo allowlist changed")
    for allowlist in (probe.PCI_ATTRIBUTES, probe.USB_DEVICE_ATTRIBUTES, probe.USB_INTERFACE_ATTRIBUTES):
        expect(not allowlist & FORBIDDEN_ATTRIBUTES, f"forbidden attribute allowlisted: {allowlist & FORBIDDEN_ATTRIBUTES}")
    p = probe.Probe(sys_root="/nonexistent", proc_root="/nonexistent", runner=FakeRunner(), uname=uname)
    try:
        p._attribute("/nonexistent", "serial", probe.USB_DEVICE_ATTRIBUTES, "x")
    except ValueError:
        pass
    else:
        raise Failure("non-allowlisted attribute read was not refused")
    return "fixed allowlists; serial/manufacturer/product/descriptors/address/config/resource never readable"


@check
def secrets_in_synthetic_tree_never_reach_output():
    with scratch() as d:
        t = workstation(d)
        doc, _, _ = run_probe(t)
        out = probe.canonical_json(doc).decode()
        expect("SECRET" not in out, "a non-allowlisted attribute value reached the inventory")
        expect(str(t.root) not in out and "/devices/" not in out, "a sysfs path reached the inventory")
    return "planted serial/product/manufacturer/descriptors/config/resource values and real paths never emitted"


def _probe_ast():
    return ast.parse(PROBE_PATH.read_text(encoding="utf-8"))


def _dotted(node):
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
    return ".".join(reversed(parts))


@check
def static_safety_of_production_probe():
    tree = _probe_ast()
    imports = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            imports.add(node.module)
    expect(imports == {"json", "os", "re", "subprocess", "sys"}, f"imports {imports}")
    calls = [(n, _dotted(n.func)) for n in ast.walk(tree) if isinstance(n, ast.Call)]
    names = [name for _, name in calls]
    forbidden_calls = {"os.system", "os.popen", "os.execv", "os.execve", "os.execvp", "os.spawnv", "os.fork",
                       "subprocess.Popen", "subprocess.call", "subprocess.check_call", "subprocess.check_output",
                       "subprocess.getoutput", "subprocess.getstatusoutput", "eval", "exec", "compile",
                       "__import__", "os.remove", "os.unlink", "os.rename", "os.replace", "os.mkdir",
                       "os.makedirs", "os.chmod", "os.chown", "os.symlink", "os.truncate", "os.write",
                       "shutil.rmtree", "os.open"}
    expect(not forbidden_calls & set(names), f"forbidden calls: {forbidden_calls & set(names)}")
    runs = [n for n, name in calls if name == "subprocess.run"]
    expect(len(runs) == 1, f"expected exactly one subprocess.run, found {len(runs)}")
    kw = {k.arg: k.value for k in runs[0].keywords}
    expect(isinstance(kw.get("shell"), ast.Constant) and kw["shell"].value is False, "subprocess.run without shell=False")
    expect(isinstance(runs[0].args[0], ast.Name) and runs[0].args[0].id == "argv", "subprocess.run argv not the checked list")
    for n in ast.walk(tree):
        if isinstance(n, ast.keyword) and n.arg == "shell":
            expect(isinstance(n.value, ast.Constant) and n.value.value is False, "shell=True present")
    run_fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "run_command")
    first = run_fn.body[1] if isinstance(run_fn.body[0], ast.Expr) else run_fn.body[0]
    expect(isinstance(first, ast.Expr) and _dotted(first.value.func) == "check_command",
           "run_command must check the command first")
    for n, name in calls:
        if name == "open":
            expect(len(n.args) >= 2 and isinstance(n.args[1], ast.Constant) and n.args[1].value == "rb",
                   "open() without mode 'rb'")
    writes = [name for name in names if name.endswith((".write", ".write_text", ".write_bytes", ".writelines"))]
    expect(sorted(writes) == ["sys.stderr.write", "sys.stderr.write", "sys.stdout.buffer.write"], f"writes {writes}")
    expect(not any(_dotted(n) in ("os.environ", "os.getenv") for n in ast.walk(tree) if isinstance(n, ast.Attribute)),
           "environment is read")
    strings = [n.value for n in ast.walk(tree) if isinstance(n, ast.Constant) and isinstance(n.value, str)]
    executables = {s for s in strings if s.startswith(("/usr/", "/bin/", "/sbin/"))}
    expect(executables == {probe.DPKG, probe.DETECT_VIRT, probe.UDEVADM, "/usr/bin:/bin"}, f"paths {executables}")
    banned = ("machine-id", "hostname", "dmi", "address", "serial", "uuid", "wwid", "wwn", "ssid", "/etc/",
              "export-db", "attribute-walk", "query=all", "trigger", "settle", "control", "bos_descriptors",
              "manufacturer", "class/net", "efivars/*", "socket", "http")
    hits = sorted({b for s in strings for b in banned if b in s.lower()})
    expect(not hits, f"forbidden strings in probe source: {hits}")
    return "stdlib imports only; one subprocess.run(argv, shell=False) after check_command; read-only opens; " \
           "no env, no forbidden paths or udev operations; only dpkg/systemd-detect-virt/udevadm"


@check
def probe_does_not_import_test_code():
    text = PROBE_PATH.read_text(encoding="utf-8")
    expect("hwe_reference" not in text and "import tests" not in text and "sys.path" not in text,
           "production probe references test code")
    mode = os.stat(PROBE_PATH).st_mode
    expect(mode & 0o111 and text.startswith("#!/usr/bin/python3\n"), "probe.py must be executable with /usr/bin/python3")
    return "hardware/probe.py is executable, /usr/bin/python3, independent of tests/hwe_reference.py"


@check
def probe_patterns_match_inventory_schema():
    schema = ref.load_schema(ref.SCHEMA_FILES["inventory"])
    d = schema["$defs"]
    pairs = {
        "kernel_release": d["system"]["properties"]["kernel_release"]["pattern"],
        "cpu_vendor": d["cpu"]["properties"]["vendor"]["pattern"],
        "model_name": d["cpu"]["properties"]["model_name"]["pattern"],
        "microcode_revision": d["cpu"]["properties"]["microcode_revision"]["pattern"],
        "pci_locator": d["pciDevice"]["properties"]["locator"]["pattern"],
        "usb_locator": d["usbDevice"]["properties"]["locator"]["pattern"],
        "pci_modalias": d["pciDevice"]["properties"]["modalias"]["pattern"],
        "usb_interface_modalias": d["usbInterface"]["properties"]["modalias"]["pattern"],
        "driver": d["driver"]["pattern"],
        "module": d["modules"]["items"]["pattern"],
        "virtualization_technology": d["system"]["properties"]["virtualization"]["properties"]["technology"]["pattern"],
    }
    expect(set(pairs) == set(probe.PATTERNS), "pattern set differs")
    for key, pattern in pairs.items():
        expect(probe.PATTERNS[key] == pattern, f"{key}: probe pattern differs from inventory-v1")
    expect(probe.MAX_MODEL_NAME == d["cpu"]["properties"]["model_name"]["maxLength"], "model_name maxLength")
    expect(probe.MAX_TECHNOLOGY == d["system"]["properties"]["virtualization"]["properties"]["technology"]["maxLength"],
           "technology maxLength")
    return f"{len(pairs)} value patterns identical to inventory-v1"


# ---------------------------------------------------------------------------
# Contract and determinism


@check
def output_validates_against_inventory_v1():
    doc, _, _ = standard()
    errs = ref.inventory_errors(doc)
    expect(not errs, f"inventory-v1 errors: {errs}")
    with scratch() as d:
        t = Tree(d)
        t.cpuinfo([[("processor", "0"), ("vendor_id", "SyntheticCPU")]])
        doc2, _, _ = run_probe(t, FakeRunner(vm=None))
        expect(not ref.inventory_errors(doc2) and doc2["devices"] == [], "minimal machine")
    return "workstation and minimal (no devices, BIOS, bare metal) inventories pass schema + semantic validation"


@check
def canonical_output_matches_contract_helper():
    doc, _, _ = standard()
    expect(probe.canonical_json(doc) == ref.canonical_json(ref.normalize_inventory(doc)),
           "probe canonical JSON differs from hwe_reference.canonical_json")
    return "probe bytes == hwe_reference.canonical_json(normalize_inventory(doc))"


@check
def deterministic_ordering():
    doc, _, _ = standard()
    ids = [d["id"] for d in doc["devices"]]
    expect(ids == sorted(ids), "devices not sorted by id")
    for dev in doc["devices"]:
        nums = [i["number"] for i in dev.get("interfaces", [])]
        expect(nums == sorted(set(nums)), f"{dev['id']}: interfaces not sorted/unique")
        for item in [dev] + dev.get("interfaces", []):
            if "modules" in item:
                expect(item["modules"] == sorted(set(item["modules"])), "modules not sorted/unique")
    return "devices by id, interfaces by number, modules sorted and unique"


@check
def equivalent_trees_give_identical_bytes():
    with scratch() as a_dir, scratch() as b_dir:
        a = probe.canonical_json(run_probe(workstation(a_dir / "x", order=1))[0])
        b = probe.canonical_json(run_probe(workstation(b_dir / "deeper" / "y", order=-1))[0])
        a2 = probe.canonical_json(run_probe(workstation(a_dir / "z", order=1))[0])
    expect(a == b == a2, "equivalent synthetic trees produced different output")
    return f"trees built in different orders and locations: identical {len(a)}-byte output ({ref.sha256_digest(a)})"


@check
def cli_contract():
    out, err = io.BytesIO(), io.StringIO()

    class FakeProbe:
        def __init__(self, fail):
            self.fail = fail

        def inventory(self):
            if self.fail:
                raise probe.ProbeError("tool_missing", "required tool /usr/bin/udevadm is not available")
            return {"schema": probe.INVENTORY_SCHEMA_ID, "x": "y"}

    saved = probe.Probe
    stdout = sys.stdout
    try:
        for fail in (False, True):
            out.seek(0), out.truncate()
            err.seek(0), err.truncate()
            probe.Probe = lambda fail=fail: FakeProbe(fail)
            sys.stdout = io.TextIOWrapper(out, encoding="utf-8")
            with contextlib.redirect_stderr(err):
                rc = probe.main([])
            sys.stdout.flush()
            sys.stdout.detach()
            sys.stdout = stdout
            if fail:
                expect(rc == 1 and out.getvalue() == b"", "failure must emit nothing on stdout")
                expect(err.getvalue() == "probe: error: tool_missing: required tool /usr/bin/udevadm is not available\n",
                       f"stderr {err.getvalue()!r}")
            else:
                expect(rc == 0 and err.getvalue() == "", "success must leave stderr empty")
                expect(out.getvalue() == b'{"schema":"lubarchy.hardware.inventory/v1","x":"y"}\n', "stdout bytes")
        with contextlib.redirect_stderr(err):
            expect(probe.main(["--sys-root=/tmp"]) == 2, "CLI must take no options")
    finally:
        probe.Probe = saved
        sys.stdout = stdout
    return "success: one canonical document + newline, empty stderr; failure: empty stdout, one error line, exit 1"


@check
def no_live_system_access_by_this_gate():
    # The suite only constructs Probe with synthetic roots and FakeRunner.
    source = (TESTS / "hardware-probe.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and _dotted(node.func) in ("probe.Probe", "Probe"):
            kws = {k.arg for k in node.keywords}
            expect({"sys_root", "proc_root", "runner"} <= kws, "Probe constructed without synthetic roots and runner")
    return "every Probe in this gate uses synthetic sys/proc roots and a fake runner"


def main():
    sys.stdout.reconfigure(line_buffering=True)
    from importlib.metadata import version
    print(f"python {sys.version.split()[0]}, jsonschema {version('jsonschema')}")
    failures = 0
    for fn in CHECKS:
        try:
            detail = fn()
        except Exception as e:  # noqa: BLE001 - every failure is reported
            failures += 1
            print(f"FAIL: {fn.__name__}: {type(e).__name__}: {e}", file=sys.stderr, flush=True)
            continue
        print(f"ok: {fn.__name__}" + (f": {detail}" if detail else ""))
    if failures:
        print(f"FAIL: hardware probe ({failures} of {len(CHECKS)} checks failed)", file=sys.stderr)
        return 1
    print(f"PASS: hardware probe ({len(CHECKS)} checks)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
