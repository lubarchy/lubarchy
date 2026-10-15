#!/usr/bin/python3
"""LUBARCHY trusted qualification deployment checks and launcher (M1-K; builder only).

    trusted-deployment.py verify    --root DIR --commit SHA --tree SHA
    trusted-deployment.py imports   --root DIR --commit SHA --tree SHA --hostile DIR
    trusted-deployment.py run-apply --root DIR --commit SHA --tree SHA
    trusted-deployment.py run-recover --root DIR --commit SHA --tree SHA
    trusted-deployment.py run-gc-plan --root DIR --commit SHA --tree SHA
    trusted-deployment.py run-gc      --root DIR --commit SHA --tree SHA

A privileged process must never execute LUBARCHY code from a tree an
unprivileged user can write. For qualification, root creates an ephemeral
deployment of the exact reviewed commit with system tools only (no LUBARCHY
code involved):

    DIR = /root/lubarchy-m1k/<full-commit>/
    DIR/repo.git      bare clone of a verified bundle (git bundle verify, git fsck --full --strict)
    DIR/manifest.txt  `git ls-tree -r --full-tree <commit>` of that repository
    DIR/tree/         `git archive <commit>` extracted by root (umask 022)

The security boundary is that root-performed, content-addressed deployment
into root-owned, unprivileged-nonwritable paths BEFORE any root execution.
This tool runs from inside that deployment and records evidence of it; a
self-check inside attacker-writable code would prove nothing, so it is never
run from the developer checkout. It is NOT the product installation
mechanism: production will use package/installer-owned system paths.

verify     asserts: every ancestor of DIR up to / and DIR itself is a real
           directory (no symlink), owned by root and not group/other
           writable; repo.git, manifest.txt and tree/ likewise; every entry of
           tree/ is a regular file or directory owned by root, not
           group/other writable; the tree's file set equals the manifest
           exactly (no untracked file); every file's Git blob hash and
           executable bit match; the repository resolves --commit to --tree
           and its ls-tree equals manifest.txt. Prints JSON evidence.
imports    runs the interpreter exactly as the privileged launch does
           (/usr/bin/env -i /usr/bin/python3 -I -B, cwd = --hostile, with
           PYTHONPATH and a decoy package_apply.py/resolver.py in the hostile
           directory) and requires every LUBARCHY module to load from tree/.
run-apply / run-recover / run-gc-plan / run-gc
           verifies, then execs the privileged entrypoint of tree/ (`apply`,
           `recover`, `gc-plan` or `gc`)
           with an empty environment, `python3 -I -B` (no bytecode written
           into the deployment) and cwd /.
"""

import hashlib
import json
import os
import re
import stat
import subprocess
import sys

GIT = "/usr/bin/git"
ENV = "/usr/bin/env"
PYTHON = "/usr/bin/python3"
ENTRYPOINT = "hardware/apply-packages.py"
EXECUTOR_MODULES = ("package_apply", "package_staging", "package_transaction", "source_activation", "resolver",
                    "probe")
HEX40 = re.compile(r"^[0-9a-f]{40}$")
LS_TREE = re.compile(r"^(100644|100755) blob ([0-9a-f]{40})\t(\S(?:.*\S)?)$")
GIT_ENV = {"PATH": "/usr/bin:/bin", "LC_ALL": "C", "HOME": "/nonexistent", "GIT_CONFIG_NOSYSTEM": "1",
           "GIT_CONFIG_GLOBAL": "/dev/null"}


class DeploymentError(Exception):
    pass


def _fail(msg):
    raise DeploymentError(msg)


def blob_sha1(data):
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


def parse_manifest(text):
    out = {}
    for line in text.splitlines():
        m = LS_TREE.match(line)
        if not m:
            _fail("manifest line is not a regular-file ls-tree entry (symlinks/submodules are not deployable)")
        mode, sha, path = m.groups()
        if path.startswith("/") or ".." in path.split("/") or path in out:
            _fail(f"manifest path {path!r} is unsafe or duplicated")
        out[path] = (mode, sha)
    if not out:
        _fail("empty manifest")
    return out


def check_dir(path, owner_uid):
    st = os.lstat(path)
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
        _fail(f"{path} is not a real directory")
    if st.st_uid != owner_uid:
        _fail(f"{path} is not owned by uid {owner_uid}")
    if st.st_mode & 0o022:
        _fail(f"{path} is group/other writable")


def check_ancestors(root, owner_uid, stop="/"):
    """Every directory from `stop` down to `root` (inclusive)."""
    root = os.path.normpath(root)
    stop = os.path.normpath(stop)
    if not os.path.isabs(root) or not (root == stop or root.startswith(stop.rstrip("/") + "/")):
        _fail("deployment root must be an absolute path below the trust anchor")
    cur, chain = stop, [stop]
    for part in [p for p in root[len(stop):].split("/") if p]:
        cur = os.path.join(cur, part)
        chain.append(cur)
    for d in chain:
        check_dir(d, owner_uid)
    return chain


def verify_tree(root, manifest, owner_uid, stop="/"):
    """Ownership, modes, symlinks, exact file set and content of root/tree."""
    chain = check_ancestors(root, owner_uid, stop)
    tree = os.path.join(root, "tree")
    check_dir(tree, owner_uid)
    for name in ("manifest.txt",):
        st = os.lstat(os.path.join(root, name))
        if not stat.S_ISREG(st.st_mode) or st.st_uid != owner_uid or st.st_mode & 0o022:
            _fail(f"{name} must be a root-owned regular file, not group/other writable")
    found = {}
    for dirpath, dirnames, filenames in os.walk(tree, followlinks=False):
        for d in list(dirnames):
            check_dir(os.path.join(dirpath, d), owner_uid)
        for f in filenames:
            p = os.path.join(dirpath, f)
            st = os.lstat(p)
            if stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode):
                _fail(f"{os.path.relpath(p, tree)} is not a regular file")
            if st.st_uid != owner_uid or st.st_mode & 0o022:
                _fail(f"{os.path.relpath(p, tree)} has a wrong owner or is group/other writable")
            fd = os.open(p, os.O_RDONLY | os.O_NOFOLLOW)
            try:
                data = b""
                while True:
                    b = os.read(fd, 1 << 20)
                    if not b:
                        break
                    data += b
            finally:
                os.close(fd)
            found[os.path.relpath(p, tree)] = (bool(st.st_mode & 0o100), blob_sha1(data))
    extra = sorted(set(found) - set(manifest))
    missing = sorted(set(manifest) - set(found))
    if extra:
        _fail(f"untracked file(s) in the deployment: {', '.join(extra[:5])}")
    if missing:
        _fail(f"missing file(s) in the deployment: {', '.join(missing[:5])}")
    for path, (mode, sha) in manifest.items():
        executable, actual = found[path]
        if actual != sha:
            _fail(f"{path}: content differs from the reviewed commit")
        if executable != (mode == "100755"):
            _fail(f"{path}: executable bit differs from the reviewed commit")
    return {"ancestors": chain, "files": len(found),
            "content_digest": hashlib.sha256("".join(f"{m} {s} {p}\n" for p, (m, s) in
                                                     sorted(manifest.items())).encode()).hexdigest()}


def git(repo, *args):
    r = subprocess.run([GIT, f"--git-dir={repo}", *args], env=dict(GIT_ENV), shell=False, stdin=subprocess.DEVNULL,
                       capture_output=True, timeout=600, check=False)
    if r.returncode != 0:
        _fail(f"git {args[0]} failed")
    return r.stdout.decode("utf-8")


def verify_git(root, commit, tree_sha, manifest_text, owner_uid):
    if not HEX40.match(commit or "") or not HEX40.match(tree_sha or ""):
        _fail("commit and tree must be full 40-hex SHA-1 object names")
    repo = os.path.join(root, "repo.git")
    check_dir(repo, owner_uid)
    if git(repo, "rev-parse", "--verify", f"{commit}^{{commit}}").strip() != commit:
        _fail("repository does not contain the reviewed commit")
    if git(repo, "rev-parse", "--verify", f"{commit}^{{tree}}").strip() != tree_sha:
        _fail("reviewed commit does not have the expected tree")
    if git(repo, "ls-tree", "-r", "--full-tree", commit) != manifest_text:
        _fail("manifest.txt is not the ls-tree of the reviewed commit")


def verify(root, commit, tree_sha, owner_uid=0, stop="/"):
    root = os.path.normpath(root)
    fd = os.open(os.path.join(root, "manifest.txt"), os.O_RDONLY | os.O_NOFOLLOW)
    try:
        manifest_text = os.read(fd, 1 << 22).decode("utf-8")
    finally:
        os.close(fd)
    manifest = parse_manifest(manifest_text)
    evidence = verify_tree(root, manifest, owner_uid, stop)
    verify_git(root, commit, tree_sha, manifest_text, owner_uid)
    evidence.update(commit=commit, tree=tree_sha, deployment=root, owner_uid=owner_uid,
                    entrypoint=os.path.join(root, "tree", ENTRYPOINT))
    return evidence


def privileged_argv(root, operation="apply"):
    """The privileged launch: empty environment, isolated interpreter, the
    deployment's entrypoint. Never a path outside the verified tree."""
    if operation not in ("apply", "recover", "gc-plan", "gc"):
        _fail("unknown privileged operation")
    entry = os.path.join(os.path.normpath(root), "tree", ENTRYPOINT)
    return [ENV, "-i", PYTHON, "-I", "-B", entry, operation]


IMPORT_PROBE = (
    "import json, os, sys\n"
    "hw = os.path.dirname(os.path.realpath(sys.argv[1]))\n"
    "sys.path.insert(0, hw)\n"            # exactly what hardware/apply-packages.py does
    "mods = {}\n"
    "for m in sys.argv[2:]:\n"
    "    mods[m] = os.path.realpath(__import__(m).__file__)\n"
    "print(json.dumps({'modules': mods, 'sys_path': sys.path, 'flags': {'isolated': sys.flags.isolated,"
    " 'ignore_environment': sys.flags.ignore_environment, 'no_user_site': sys.flags.no_user_site,"
    " 'dont_write_bytecode': sys.flags.dont_write_bytecode}}))\n")


def check_imports(root, hostile):
    """Run the interpreter as the privileged launch does, from a hostile
    directory with PYTHONPATH pointing at decoys; every module must load from tree/."""
    tree = os.path.realpath(os.path.join(root, "tree"))
    entry = os.path.join(tree, ENTRYPOINT)
    for decoy in EXECUTOR_MODULES:
        with open(os.path.join(hostile, decoy + ".py"), "w", encoding="utf-8") as f:
            f.write("raise SystemExit('decoy imported')\n")
    argv = [ENV, "-i", f"PYTHONPATH={hostile}", f"PYTHONHOME={hostile}", f"PYTHONSTARTUP={hostile}/x.py",
            PYTHON, "-I", "-B", "-c", IMPORT_PROBE, entry, *EXECUTOR_MODULES]
    r = subprocess.run(argv, cwd=hostile, shell=False, stdin=subprocess.DEVNULL, capture_output=True, timeout=120,
                       check=False)
    if r.returncode != 0:
        _fail("import probe failed (a decoy may have been imported)")
    out = json.loads(r.stdout)
    for m, path in out["modules"].items():
        if not path.startswith(tree + "/"):
            _fail(f"{m} loaded from outside the trusted deployment")
    if any(p in ("", ".", hostile) or p.startswith(hostile + "/") for p in out["sys_path"]):
        _fail("hostile or current directory on sys.path")
    if not all(out["flags"][k] for k in ("isolated", "ignore_environment", "no_user_site", "dont_write_bytecode")):
        _fail("interpreter is not isolated")
    return out


def main(argv):
    cmds = {"verify": 6, "imports": 8, "run-apply": 6, "run-recover": 6, "run-gc-plan": 6, "run-gc": 6}
    if len(argv) < 1 or argv[0] not in cmds or len(argv) != cmds[argv[0]] + 1:
        sys.stderr.write(__doc__)
        return 64
    opts = dict(zip(argv[1::2], argv[2::2]))
    if set(opts) - {"--root", "--commit", "--tree", "--hostile"} or not {"--root", "--commit", "--tree"} <= set(opts):
        sys.stderr.write(__doc__)
        return 64
    try:
        evidence = verify(opts["--root"], opts["--commit"], opts["--tree"])
        if argv[0] == "verify":
            print(json.dumps(evidence, indent=2, sort_keys=True))
            return 0
        if argv[0] == "imports":
            print(json.dumps(check_imports(opts["--root"], opts["--hostile"]), indent=2, sort_keys=True))
            return 0
        if os.geteuid() != 0:
            _fail("run-apply/run-recover/run-gc-plan/run-gc must be started as root")
        launch = privileged_argv(opts["--root"], argv[0][len("run-"):])
        if launch[5] != evidence["entrypoint"]:
            _fail("launch path is not the verified entrypoint")
        os.chdir("/")
        os.execv(launch[0], launch)
    except (DeploymentError, OSError, ValueError) as e:
        sys.stderr.write(f"trusted-deployment: error: {e}\n")
        return 1
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
