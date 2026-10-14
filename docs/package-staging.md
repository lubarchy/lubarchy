# Debian Source Activation and Staged Package Acquisition (M1-J)

M1-J prepares the trusted inputs for a future privileged package mutation:
- a deterministic plan that realises the accepted Debian source policy on an
  installed system;
- a staged payload bundle bound exactly to one qualified package transaction.

**Status:** implemented locally; awaiting CTO review. Nothing here activates a
source, installs a package or changes a package mark. There is no activate
or apply operation.

M1-J makes no new policy decision. D-013 already authorises Debian `main` and
`non-free-firmware` (suites `trixie`, `trixie-updates`, `trixie-security`;
official Debian infrastructure; Debian archive keyring). M1-J only realises
that policy safely. `contrib`, `non-free`, Backports, NVIDIA, a LUBARCHY
repository and every third-party source stay unauthorised.

## Chain

fresh probe → resolver → package transaction (M1-I) → source-state
verification and activation plan → authenticated metadata snapshot → exact
package acquisition → staged bundle verification → final transaction
revalidation → *future privileged apply* (not implemented).

| Piece | Code | Contract |
| --- | --- | --- |
| Source activation | `hardware/source_activation.py`, CLI `hardware/compile-source-activation.py` | `lubarchy.packages.source-activation/v1` ([schema](../hardware/schemas/source-activation-v1.schema.json)) |
| Staging | `hardware/package_staging.py`, CLI `hardware/stage-packages.py` | `lubarchy.packages.staged-bundle/v1` ([schema](../hardware/schemas/staged-bundle-v1.schema.json)) |
| Builder qualification | `tools/hwe/qualify-staging.py` | evidence only |
| Static gate 10 | `tests/hardware-staging.py` | synthetic and offline |

## Debian 13 source topology (builder evidence)

The Debian 13.7 netinst installation on the builder was inspected read-only:
- `/etc/apt/sources.list` uses the traditional one-line format. It has three
  `deb` lines, component `main` only, plain `http`, and no `Signed-By`:
  `deb.debian.org/debian` with `trixie` and `trixie-updates`, and
  `security.debian.org/debian-security` with `trixie-security`. The disabled
  cdrom line and the commented `deb-src` lines are ignored.
- The file belongs to no package (`dpkg -S` finds nothing): it was written by
  the installer and is administrator-owned.
- `/etc/apt/sources.list.d/` is empty. There is no deb822 file.
- Trust comes from `/etc/apt/trusted.gpg.d/*.asc`, installed by
  `debian-archive-keyring` 2025.1. `/usr/share/keyrings/debian-archive-keyring.pgp`
  has SHA-256 `506b815c…2017`, and `.gpg` is a symlink to it.
- Security and updates are separate suites on separate lines.
- APT 3.0.3 offers `apt modernize-sources`. On an isolated copy it renamed
  the foreign file to `sources.list.bak` and could not determine
  `Signed-By`, so it is not used: it would mutate a file LUBARCHY does not
  own.

## Ownership model

LUBARCHY owns exactly one file, `/etc/apt/sources.list.d/lubarchy-debian.sources`,
in deb822 format with mode `0644` and owner `root:root`. Every other source
file is foreign and is never modified. Debian's own deb822 file name,
`debian.sources`, is deliberately not used.

The model was chosen from isolated `apt-get update` runs on copies of the
builder sources (A1–C0 below). `/etc` was never touched.

| Variant | Result |
| --- | --- |
| A1: foreign one-line file kept; owned deb822 adds only `non-free-firmware` for the same URIs, with `Signed-By` | Clean: no warnings. APT merges `Signed-By` per (URI, suite), so the foreign `main` entries are authenticated by the Debian keyring too. |
| A2/A4: foreign file kept; owned deb822 repeats `main` | Rejected: six "Target Packages … is configured multiple times" warnings. |
| A3: complement uses `https` while the foreign file uses `http` | Ambiguous: APT keys archives by host and path regardless of scheme and silently merged the `https` entry into the `http` archive. |
| B: owned complete file only | Clean, but on the builder it would need the foreign file disabled, which is a mutation of a non-owned file. |
| C0: foreign file only | No `Signed-By`, and `intel-microcode` has no candidate. |

Recognised topologies:
- **`complement_foreign_baseline`.** The foreign sources define exactly the
  three authorised suites, with policy components only. The owned file adds
  the missing policy components, in practice `non-free-firmware`, using
  exactly the foreign URIs and `Signed-By` the Debian keyring. This is the
  builder case: the action is `create_owned_source`.
- **`owned_complete`.** There are no foreign Debian sources. The owned file
  defines every suite with `main non-free-firmware` over `https`, the same
  endpoints as `build/live/auto/config`. This is the expected layout for a
  future LUBARCHY-installed system.

Actions:
- `already_satisfied`: the effective state already equals the policy post-state.
- `create_owned_source`: the owned file is absent.
- `replace_owned_source`: only the LUBARCHY-owned file differs from the rendering.

Everything else fails closed. The foreign file is never rewritten.

## Source-activation contract

`lubarchy.packages.source-activation/v1` binds:
- the architecture;
- the accepted policy (D-013 components, archives, suites, schemes, keyring
  path) and the keyring's SHA-256;
- the normalized pre-state and its digest;
- the recognised model;
- the ownership: owned path, mode, owner, foreign paths, and the fact that
  foreign files are never modified;
- the action;
- the owned source: deb822 stanzas, SHA-256 and size of the rendering;
- the effective post-state (six URI/suite/component targets with their
  `Signed-By`) and its digest;
- safety assertions.

It has no timestamps and no caller text. Verification recompiles from fresh
state and requires byte identity, so any source, keyring or owned-file drift
makes the contract stale.

### Pre-state normalization

The pre-state is semantic, not raw bytes:
- each source file's path, class (`foreign` or `owned`) and format;
- its parsed entries: type, URI, suite, sorted components and `Signed-By`;
- for the owned file only, the SHA-256 of its bytes;
- the files APT ignores silently (`Dir::Ignore-Files-Silently`).

Comments and whitespace do not change the digest; components, URIs, suites,
options and files do.

### Fail-closed rules

Source state is rejected when:
- a host or path is not an official Debian archive (third-party mirror,
  NVIDIA, LUBARCHY repository), or the scheme is not `http`/`https`;
- a suite is not authorised (Backports, testing, older releases);
- a component is not authorised (`contrib`, `non-free`);
- a `trusted` option or `Trusted:` field appears;
- a `Signed-By` other than the Debian archive keyring appears, or no
  `Signed-By` would bind a suite;
- the input is `deb-src`, cdrom, an `arch=` or other option, or an
  unsupported deb822 field;
- a file is malformed, not UTF-8, multi-line deb822, a symlink, a FIFO or a
  directory;
- a file in `sources.list.d` has an unrecognised name;
- definitions are duplicated (the same URI/suite/component twice);
- one archive uses mixed URI schemes, or `Signed-By` values conflict within
  a suite;
- the foreign topology covers only part of the suites;
- an owned file exists although none is required.

### Renderer

- **Output:** a fixed header, then one stanza per (URI, component set) with
  fields in the fixed order `Types`, `URIs`, `Suites`, `Components`,
  `Signed-By`.
- **Values:** every value is re-validated against the policy, and caller text
  is never accepted.
- **Determinism:** rendering identical state twice is byte-identical. On the
  builder the rendering is 541 bytes, SHA-256 `3d7f9b8f…d2a6`: two stanzas
  adding `non-free-firmware` with the Debian keyring.
- **Destination:** M1-J renders only into empty, caller-owned workspaces,
  never `/etc`.

### Isolated authenticated metadata

`source_activation.isolated_update()` builds an APT configuration inside a
workspace:
- the foreign source files as byte copies plus the rendered owned file;
- an **empty** `trusted.gpg.d`, so only the `Signed-By` keyring authenticates;
- no preferences, no `apt.conf.d` hooks, private lists, cache and log;
- insecure and weak repositories disallowed.

It then runs `apt-get update` (any `W:`/`E:`/`Err:` fails) and requires every
index target to be a Debian origin, label and codename in policy, `Trusted:
yes`, and signed by the keyring. The targets must equal the contract
post-state.

One exception was observed and is handled narrowly. Debian publishes
`trixie-updates/non-free-firmware` as an **empty** index (size 0, SHA-256 of
empty input, listed in the signed `InRelease`). APT keeps no list file for it
and reports no target. Such a target is accepted only if the authenticated
`InRelease` of that suite lists the index with size 0 and the empty hash.

The repository metadata digest covers, per target:
- identity: URI, suite, component and architecture;
- origin, label and codename, and `Signed-By`;
- the SHA-256 of the `Packages` index and of the `InRelease` file;
- whether the index is empty.

Full Debian metadata is never committed. Evidence keeps only this compact
provenance.

## Staged bundle

### Layout

```
<staging-root>/<transaction-digest-hex>/
  manifest.json          canonical lubarchy.packages.staged-bundle/v1
  archives/<name>_<version>_<arch>.deb
```

- **Location:** the future production root is
  `/var/lib/lubarchy/package-staging/`, root-owned. M1-J never writes it:
  staging writes only into caller-owned workspaces outside every system
  path. The bundle directory name is derived only from the transaction
  digest (64 hex digits), so caller text cannot traverse.
- **Modes:** directories `0755` and payloads `0644`, owned by the staging
  user. Nothing may be group- or other-writable.
- **Finalization:** the bundle is built in a private `0700` temporary
  directory in the same root, payloads are verified, and the manifest is
  written exclusively with `O_EXCL|O_NOFOLLOW` and fsynced. Then the
  directory is renamed atomically to its final name and the root is fsynced.
  A failure removes the temporary directory entry by entry.
- **Immutability:** a finalized bundle is immutable at the application level.
  Verification rejects any change; filesystem immutable flags are not used.

### Manifest

`lubarchy.packages.staged-bundle/v1` binds:
- the transaction digest and the transaction's repository-state digest;
- the source-activation digest and the repository metadata digest;
- the architecture;
- every payload: package, architecture, version, relation, intended mark,
  APT file name, size, SHA-256, and Debian repository identities (suite and
  component);
- the direct targets;
- the layout, the state `finalized` and the safety assertions;
- the `bundle_digest`, computed over the manifest without itself.

It has no timestamps and no absolute paths.

### Acquisition

- **Payload set:** the transaction's complete change set, i.e. every
  `install` operation. `already_satisfied` packages are installed already.
- **Targets:** each payload is acquired with `apt-get --quiet download
  name:arch=version`, built internally, through the isolated configuration.
  There are no caller names, versions, patterns or options.
- **Marks:** download targets select payloads only and never become install
  targets, so they cannot change automatic/manual marks.

### Payload verification

Each payload is opened `O_NOFOLLOW|O_NONBLOCK`, so a FIFO cannot stall
verification, and must be:
1. a regular file;
2. named exactly as APT names the expected package;
3. of the size, and
4. with the SHA-256, from the authenticated index as bound in the
   transaction;
5. through 7. of the `Package`, `Version` and `Architecture` that
   `dpkg-deb --field` reads from the **same opened inode**
   (`/dev/fd/N`), and these must match the operation.

In addition, every expected payload must be present exactly once, with no
duplicate and no extra file.

After finalization, `verify_bundle` also checks the layout, the canonical
manifest bytes, the bundle digest, the directory name and the modes. It
therefore detects payload replacement, truncation, addition or removal,
filename swaps, symlinks, mode changes and manifest edits.

### Architecture semantics

`name:arch=version` blocks foreign architectures. APT 3.0.3 treats the
native qualifier and `:all` interchangeably, so target syntax alone proves
nothing about architecture. The authoritative architecture checks are:
- the verified closure (M1-I);
- the `.deb` metadata read by `dpkg-deb`.

## Freshness and TOCTOU

- **What staging fixes.** Staging ends repository drift after transaction
  approval. The future apply consumes the payloads bound to the transaction
  and never silently substitutes newly fetched versions.
- **What staging does not claim.** Historical Debian snapshots are not
  supported. A bundle stays cryptographically bound to its transaction, but
  it may become obsolete (for example, a newer security update).

**Final revalidation rule** (`package_staging.final_revalidation`).
Immediately before any future apply, the executor recomputes from fresh
state:
- a fresh probe and the source-state check;
- the installed state and refreshed authenticated metadata under policy;
- the package transaction.

The bundle is accepted only if the fresh transaction digest, the
source-activation digest and the repository metadata digest **all** equal
the bundle's bindings. Otherwise the bundle is stale: discard and restage.

- **Conservative by design.** Any metadata change forces a restage, even one
  that leaves the closure unchanged. Accepting a bundle when only unrelated
  metadata changed would be a later CTO policy choice.
- **Combined guarantee.** Staging prevents payload substitution, and fresh
  re-resolution prevents installing a superseded transaction.

## Future offline apply model

The model was proven on the builder with copies of the staged payloads. No
package was installed.

```
/usr/bin/apt-get install --yes --quiet --no-install-recommends --no-remove --no-download \
    -o Dir::Cache::archives=<bundle>/archives/ <direct targets only>
```

`package_staging.offline_apply_argv()` constructs it; M1-J never executes it.

**Offline consumption (APT 3.0.3):**
- With only the direct target, a complete payload cache, `--no-download` and
  the network black-holed through an unreachable proxy, APT resolves the full
  closure from the cache. `--print-uris` lists nothing to fetch ("Need to
  get 0 B").
- A missing dependency payload, or a cache whose version differs from the
  index, fails with exit 100.

**Marks:**
- In simulation, the resolver debug output shows the direct package
  `FromUser=1` and the dependency installed "as Depends", `FromUser=0`.
- In a non-root sandbox, `apt-get install` ran with dpkg replaced by
  `/bin/true` and with private copies of the dpkg status and
  `extended_states`. Nothing was unpacked and the copied status was
  unchanged. APT wrote exactly `Auto-Installed: 1` for the dependency, and
  the direct package stayed manual.
- So the M1-I mark semantics hold offline.

**Integrity finding:** APT does **not** re-hash a pre-existing file in its
archive cache. A same-size corrupted payload was accepted in
`--download-only` and in the sandbox install path. LUBARCHY's bundle
verification (SHA-256, size, `dpkg-deb` identity) is therefore the
authoritative integrity control. The future executor must:
- verify the bundle immediately before apply;
- run APT against a root-owned location that no unprivileged user can
  write.

**What is not used:**
- `dpkg -i`, which bypasses APT's resolver and marks.
- Dependency `.deb` files as explicit install targets, which would make
  them manual.
- A local repository. A signed one would need a new LUBARCHY key; an
  unsigned or `trusted` one would weaken APT authentication. Instead the
  design keeps Debian's authenticated metadata chain: the `Packages`
  indexes signed by the Debian keyring bind each payload's SHA-256, and the
  bundle carries exactly those payloads.

## Filesystem safety

- **Workspaces and staging roots** must be absolute, normalized and
  symlink-free in every component, owned by the caller, not group- or
  other-writable, and outside `/etc`, `/usr`, `/boot`, `/var/lib/apt`,
  `/var/lib/dpkg`, `/var/cache/apt`, `/var/lib/lubarchy`, `/var/log`,
  `/proc`, `/sys`, `/dev` and `/run`.
- **Sensitive files** are opened with `O_NOFOLLOW` (reads also
  `O_NONBLOCK`) and checked with `fstat`. Writes use exclusive creation and
  fsync.
- **Paths** are never built from unvalidated package text: file names must
  match the APT naming pattern.
- **Payloads are opaque.** There is no archive extraction and no maintainer
  script runs.

**Cleanup boundary.** `package_staging.remove_bundle()`:
- removes exactly `<workspace-root>/<transaction hex>`;
- works entry by entry through directory descriptors, unlinking symlinks
  without following them;
- aborts on any unexpected layout;
- refuses the production root and system paths.

Production garbage collection is later work.

## CLIs

| Command | Effect |
| --- | --- |
| `compile-source-activation.py inspect \| compile` | Read `/etc/apt`; print the pre-state or the contract |
| `compile-source-activation.py validate \| verify --contract P` | Contract-only check or fresh recompile |
| `compile-source-activation.py render --contract P --output-dir D` | Render the owned file into an empty workspace |
| `compile-source-activation.py simulate --contract P --workdir D` | Isolated authenticated update; print the metadata evidence |
| `stage-packages.py stage --workspace D` | Full chain inside an empty workspace; print the manifest summary |
| `stage-packages.py verify --workspace D --transaction-digest X` | Verify a finalized bundle |

The CLIs accept no URI, suite, component, keyring, package, version, APT
option or system destination. Test fixture roots are reachable only through
library parameters used by gate 10, never through a CLI.

Exit codes:
- `0`: ok.
- `2`: malformed input.
- `3`: stale.
- `4`: unauthorised source, `trusted`, or missing `Signed-By`.
- `5`: ambiguous ownership or an unsafe path.
- `6`: metadata or payload mismatch.
- `7`: contract or internal failure.
- `64`: usage error.

## Not in M1-J

- privileged source activation;
- the privileged apply executor;
- the transaction journal;
- interruption and failure handling;
- post-install validation;
- rollback integration;
- production garbage collection;
- the NVIDIA ADR.
