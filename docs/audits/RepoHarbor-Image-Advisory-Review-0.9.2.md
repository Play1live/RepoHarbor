# RepoHarbor 0.9.2 runtime image advisory review

## A. Summary

| Evidence | CI baseline | Rebuilt runtime |
| --- | --- | --- |
| Source commit | `1cb9259e333efd16f12d227b24fdb1b4a8d9da4c` | `5efbda4fbf866b62cdcbcd583b1d5188cd569cad` |
| Docker image config ID | `sha256:e8bdb53abb78dcbe21196df340514e6c8067d8572a4749abbe224b92bc08c7cf` | `sha256:a7af5cdf1dd16599fcc8294ff8e94a291355e293a3d69b77f41caebf0de93f97` |
| Scan timestamp, UTC | `2026-11-21T18:54:08.477887944Z` | `2026-11-21T20:08:24.242385839Z` |
| Critical package/advisory pairs | 2 | 1 |
| High package/advisory pairs | 118 | 113 |
| Distinct High/Critical advisory IDs | 44 | 38 |
| Distinct binary packages flagged | 35 | 34 |
| High/Critical pairs with a scanner-listed package fix | 0 | 0 |
| CycloneDX components | 126 | 125 |

Both builds use `python:3.12.14-slim-bookworm@sha256:392307d22300de8b5986851a12d9176dfc0fc073e65bf6523ebd7dcbeb23564e`. Both scans use Trivy 0.70.0, pinned at `sha256:be1190afcb28352bfddc4ddeb71470835d16462af68d310f9f4bca710961a41e`. The local repeat used the v2 database updated on 2026-11-21 at 13:02 UTC. Both inspected runtimes are Debian 12 (Bookworm), linux/amd64. The image IDs identify image configurations. The base digest identifies the upstream image index. [Original CI image and scan](https://github.com/Play1live/RepoHarbor/actions/runs/44937961536/artifacts/14720980680)

The review covered all 44 distinct IDs and all 120 original package/advisory pairs. **5 fixed**, **16 not affected**, **23 mitigated**, **0 verified already patched**, **0 open with an established application path**, and **0 needing a separate applicability determination**. Five fixes remove affected SQLite code from the runtime, and a sixth SQLite finding concerns functionality missing in 3.40.1. The review identified zero remotely reachable vulnerable paths in the supported RepoHarbor data flow. Mitigated entries can still affect software added to the image or deployments that bypass the supported controls.

### Evidence and scope

The baseline comes from the exact CI image artifact, including its scanner output, OS/Python SBOM and image ID. Binary linkage, program presence and build flags were inspected in a local build of the same source and package set. The updated image was built and scanned again, and its package inventory was compared with the baseline. Signed Bookworm main, updates and security repositories had no applicable binary upgrades at review time. Signed Bookworm backports offered curl 8.14.1-2+deb13u2~bpo13+1, which is within the affected upstream ranges. Its Debian patch list did not identify a fix for the five reviewed curl CVEs. The matched Debian packaging archives were downloaded from the official archive and checked against the signed source index. [Debian security archive](https://deb.debian.org/debian-security/) · [Bookworm backports](https://backports.debian.org/)

A nominal upstream version is only one input. For example, Debian records that the PAM backport was not applied correctly in the shipped build. Debian also carries Expat memory-accounting backports into 2.5.0. The Python interpreter separately includes statically linked Expat 2.8.3. The official advisories, repository input validation, exact Git command options and runtime linkage all inform the findings below. The package scan showed no High/Critical rows for OpenSSL or Python distributions. The runtime reported CPython 3.12.14, OpenSSL 3.0.22 and zlib 1.2.13. The absence of a scanner row is insufficient evidence for embedded C libraries. [PAM build note](https://security-tracker.debian.org/tracker/CVE-2025-6020) · [Expat status](https://security-tracker.debian.org/tracker/CVE-2026-113351)

The Compose deployment starts as UID/GID 10001, drops all capabilities and sets no-new-privileges. Docker configures bind mounts from the host. The raw Docker image sets the UID/GID and relies on the operator to supply the remaining runtime controls. Dashboard TLS is optional, with no ports published by the example deployment. Git runs as a child process with HTTPS-only network policies, system/global Git configuration disabled, managed mirror configuration, no inherited proxies, validated certificates and explicit custom-CA support. GitHub and GitLab APIs use Python urllib and bounded JSON rather than the Git libcurl handle. [Deployment controls](../../deploy/compose.example.yaml) · [Git environment](../../src/repoharbor/credentials.py) · [Git commands](../../src/repoharbor/git_backup.py) · [Restore commands](../../src/repoharbor/restore.py)

## B. Complete advisory table

Package names are grouped inside a row when one CVE maps to multiple installed binaries. Every original package/CVE pair is included. Version cells refer to the baseline unless marked removed. “Unlisted” is Trivy's Bookworm fix field. An upstream or newer release can still have a fix, as documented individually below.

| Advisory | Bookworm runtime package(s) | Installed version(s) | Trivy | Runtime applicability | Bookworm fix in scan | Final status |
| --- | --- | --- | --- | --- | --- | --- |
| [CVE-2023-2953](#cve-2023-2953) | `libldap-2.5-0` | `2.5.13+dfsg-5` | HIGH | Code or package retained with an exposure control | Unlisted | **MITIGATED** |
| [CVE-2023-45853](#cve-2023-45853) | `zlib1g` | `1:1.2.13.dfsg-1` | CRITICAL | Required component or code absent | Unlisted | **NOT_AFFECTED** |
| [CVE-2025-6020](#cve-2025-6020) | `libpam-modules`, `libpam-modules-bin`, `libpam-runtime`, `libpam0g` | `1.5.2-6+deb12u2` | HIGH | Code or package retained with an exposure control | Unlisted | **MITIGATED** |
| [CVE-2025-69720](#cve-2025-69720) | `libncursesw6`, `libtinfo6`, `ncurses-base`, `ncurses-bin` | `6.4-4` | HIGH | Code or package retained with an exposure control | Unlisted | **MITIGATED** |
| [CVE-2025-7458](#cve-2025-7458) | `libsqlite3-0` | `3.40.1-2+deb12u2` | CRITICAL | Affected code removed | Unlisted | **FIXED** |
| [CVE-2026-105437](#cve-2026-105437) | `git`, `git-man` | `1:2.39.5-0+deb12u3` | HIGH | Code or package retained with an exposure control | Unlisted | **MITIGATED** |
| [CVE-2026-107364](#cve-2026-107364) | `libgssapi-krb5-2`, `libk5crypto3`, `libkrb5-3`, `libkrb5support0` | `1.20.1-2+deb12u5` | HIGH | Required component or code absent | Unlisted | **NOT_AFFECTED** |
| [CVE-2026-111760](#cve-2026-111760) | `git`, `git-man` | `1:2.39.5-0+deb12u3` | HIGH | Code or package retained with an exposure control | Unlisted | **MITIGATED** |
| [CVE-2026-112315](#cve-2026-112315) | `libgssapi-krb5-2`, `libk5crypto3`, `libkrb5-3`, `libkrb5support0` | `1.20.1-2+deb12u5` | HIGH | Required component or code absent | Unlisted | **NOT_AFFECTED** |
| [CVE-2026-112316](#cve-2026-112316) | `libgssapi-krb5-2`, `libk5crypto3`, `libkrb5-3`, `libkrb5support0` | `1.20.1-2+deb12u5` | HIGH | Required component or code absent | Unlisted | **NOT_AFFECTED** |
| [CVE-2026-112317](#cve-2026-112317) | `libgssapi-krb5-2`, `libk5crypto3`, `libkrb5-3`, `libkrb5support0` | `1.20.1-2+deb12u5` | HIGH | Required component or code absent | Unlisted | **NOT_AFFECTED** |
| [CVE-2026-112318](#cve-2026-112318) | `libgssapi-krb5-2`, `libk5crypto3`, `libkrb5-3`, `libkrb5support0` | `1.20.1-2+deb12u5` | HIGH | Required component or code absent | Unlisted | **NOT_AFFECTED** |
| [CVE-2026-112319](#cve-2026-112319) | `libgssapi-krb5-2`, `libk5crypto3`, `libkrb5-3`, `libkrb5support0` | `1.20.1-2+deb12u5` | HIGH | Required component or code absent | Unlisted | **NOT_AFFECTED** |
| [CVE-2026-112320](#cve-2026-112320) | `libgssapi-krb5-2`, `libk5crypto3`, `libkrb5-3`, `libkrb5support0` | `1.20.1-2+deb12u5` | HIGH | Required component or code absent | Unlisted | **NOT_AFFECTED** |
| [CVE-2026-112321](#cve-2026-112321) | `libgssapi-krb5-2`, `libk5crypto3`, `libkrb5-3`, `libkrb5support0` | `1.20.1-2+deb12u5` | HIGH | Required component or code absent | Unlisted | **NOT_AFFECTED** |
| [CVE-2026-112322](#cve-2026-112322) | `libgssapi-krb5-2`, `libk5crypto3`, `libkrb5-3`, `libkrb5support0` | `1.20.1-2+deb12u5` | HIGH | Required component or code absent | Unlisted | **NOT_AFFECTED** |
| [CVE-2026-112323](#cve-2026-112323) | `libgssapi-krb5-2`, `libk5crypto3`, `libkrb5-3`, `libkrb5support0` | `1.20.1-2+deb12u5` | HIGH | Required component or code absent | Unlisted | **NOT_AFFECTED** |
| [CVE-2026-113351](#cve-2026-113351) | `libexpat1` | `2.5.0-1+deb12u5` | HIGH | Code or package retained with an exposure control | Unlisted | **MITIGATED** |
| [CVE-2026-114466](#cve-2026-114466) | `git`, `git-man` | `1:2.39.5-0+deb12u3` | HIGH | Code or package retained with an exposure control | Unlisted | **MITIGATED** |
| [CVE-2026-115506](#cve-2026-115506) | `libexpat1` | `2.5.0-1+deb12u5` | HIGH | Code or package retained with an exposure control | Unlisted | **MITIGATED** |
| [CVE-2026-117610](#cve-2026-117610) | `libexpat1` | `2.5.0-1+deb12u5` | HIGH | Code or package retained with an exposure control | Unlisted | **MITIGATED** |
| [CVE-2026-11822](#cve-2026-11822) | `libsqlite3-0` | `3.40.1-2+deb12u2` | HIGH | Affected code removed | Unlisted | **FIXED** |
| [CVE-2026-11824](#cve-2026-11824) | `libsqlite3-0` | `3.40.1-2+deb12u2` | HIGH | Affected code removed | Unlisted | **FIXED** |
| [CVE-2026-118370](#cve-2026-118370) | `libexpat1` | `2.5.0-1+deb12u5` | HIGH | Code or package retained with an exposure control | Unlisted | **MITIGATED** |
| [CVE-2026-118380](#cve-2026-118380) | `libgssapi-krb5-2`, `libk5crypto3`, `libkrb5-3`, `libkrb5support0` | `1.20.1-2+deb12u5` | HIGH | Required component or code absent | Unlisted | **NOT_AFFECTED** |
| [CVE-2026-119079](#cve-2026-119079) | `libgssapi-krb5-2`, `libk5crypto3`, `libkrb5-3`, `libkrb5support0` | `1.20.1-2+deb12u5` | HIGH | Code or package retained with an exposure control | Unlisted | **MITIGATED** |
| [CVE-2026-119373](#cve-2026-119373) | `libsqlite3-0` | `3.40.1-2+deb12u2` | HIGH | Affected code removed | Unlisted | **FIXED** |
| [CVE-2026-119374](#cve-2026-119374) | `libsqlite3-0` | `3.40.1-2+deb12u2` | HIGH | Required component or code absent | Unlisted | **NOT_AFFECTED** |
| [CVE-2026-119379](#cve-2026-119379) | `libsqlite3-0` | `3.40.1-2+deb12u2` | HIGH | Affected code removed | Unlisted | **FIXED** |
| [CVE-2026-12064](#cve-2026-12064) | `libcurl3-gnutls` | `7.88.1-10+deb12u15` | HIGH | Required component or code absent | Unlisted | **NOT_AFFECTED** |
| [CVE-2026-16742](#cve-2026-16742) | `libsystemd0`, `libudev1` | `252.39-1~deb12u2` | HIGH | Required component or code absent | Unlisted | **NOT_AFFECTED** |
| [CVE-2026-41992](#cve-2026-41992) | `gzip` | `1.12-1` | HIGH | Code or package retained with an exposure control | Unlisted | **MITIGATED** |
| [CVE-2026-53613](#cve-2026-53613) | `bsdutils`, `libblkid1`, `libmount1`, `libsmartcols1`, `libuuid1`, `mount`, `util-linux`, `util-linux-extra` | `1:2.38.1-5+deb12u3`, `2.38.1-5+deb12u3` | HIGH | Code or package retained with an exposure control | Unlisted | **MITIGATED** |
| [CVE-2026-53615](#cve-2026-53615) | `bsdutils`, `libblkid1`, `libmount1`, `libsmartcols1`, `libuuid1`, `mount`, `util-linux`, `util-linux-extra` | `1:2.38.1-5+deb12u3`, `2.38.1-5+deb12u3` | HIGH | Code or package retained with an exposure control | Unlisted | **MITIGATED** |
| [CVE-2026-54369](#cve-2026-54369) | `libacl1` | `2.3.1-3` | HIGH | Code or package retained with an exposure control | Unlisted | **MITIGATED** |
| [CVE-2026-6276](#cve-2026-6276) | `libcurl3-gnutls` | `7.88.1-10+deb12u15` | HIGH | Code or package retained with an exposure control | Unlisted | **MITIGATED** |
| [CVE-2026-71199](#cve-2026-71199) | `libgssapi-krb5-2`, `libk5crypto3`, `libkrb5-3`, `libkrb5support0` | `1.20.1-2+deb12u5` | HIGH | Code or package retained with an exposure control | Unlisted | **MITIGATED** |
| [CVE-2026-71200](#cve-2026-71200) | `libgssapi-krb5-2`, `libk5crypto3`, `libkrb5-3`, `libkrb5support0` | `1.20.1-2+deb12u5` | HIGH | Required component or code absent | Unlisted | **NOT_AFFECTED** |
| [CVE-2026-78410](#cve-2026-78410) | `bsdutils`, `libblkid1`, `libmount1`, `libsmartcols1`, `libuuid1`, `mount`, `util-linux`, `util-linux-extra` | `1:2.38.1-5+deb12u3`, `2.38.1-5+deb12u3` | HIGH | Code or package retained with an exposure control | Unlisted | **MITIGATED** |
| [CVE-2026-8286](#cve-2026-8286) | `libcurl3-gnutls` | `7.88.1-10+deb12u15` | HIGH | Code or package retained with an exposure control | Unlisted | **MITIGATED** |
| [CVE-2026-8458](#cve-2026-8458) | `libcurl3-gnutls` | `7.88.1-10+deb12u15` | HIGH | Code or package retained with an exposure control | Unlisted | **MITIGATED** |
| [CVE-2026-85091](#cve-2026-85091) | `zlib1g` | `1:1.2.13.dfsg-1` | HIGH | Code or package retained with an exposure control | Unlisted | **MITIGATED** |
| [CVE-2026-8927](#cve-2026-8927) | `libcurl3-gnutls` | `7.88.1-10+deb12u15` | HIGH | Code or package retained with an exposure control | Unlisted | **MITIGATED** |
| [CVE-2026-9538](#cve-2026-9538) | `libperl5.36`, `perl`, `perl-base`, `perl-modules-5.36` | `5.36.0-7+deb12u4` | HIGH | Code or package retained with an exposure control | Unlisted | **MITIGATED** |

## C. Individual technical assessments

The Debian source package named in each entry was compared with the exact installed Bookworm revision. A missing named patch was insufficient to claim a fix or prove exploitability. Vendor scores are cited when available. Numeric severity in the scanner and the impact in this deployment can differ. Where the code is present, the notes distinguish the triggering component and the reason the supported application does not use that path. Checks use package inventory, source/linkage inspection and benign functional tests.

### CVE-2023-2953

**MITIGATED** · Trivy HIGH (affected) · Vendor assessment No base score verified in the cited vendor record · Debian Bookworm `openldap` `2.5.13+dfsg-5`.

LDAP BER allocation can dereference a null pointer on malformed input. Debian explicitly lists the installed Bookworm OpenLDAP 2.5 release as vulnerable, with no DSA. Git HTTPS links the LDAP library through curl, but URL validation and protocol controls exclude LDAP and LDAPS, and the image has no LDAP daemon or client entry point. Evidence: Debian tracker, Git HTTPS link map, executable inventory and URL validation tests. [Debian Tracker](https://security-tracker.debian.org/tracker/CVE-2023-2953) · [redhat CVE record](https://www.cve.org/CVERecord?id=CVE-2023-2953) · [Exact Debian packaging source](https://deb.debian.org/debian/pool/main/o/openldap/openldap_2.5.13+dfsg-5.debian.tar.xz)

### CVE-2023-45853

**NOT_AFFECTED** · Trivy CRITICAL (will_not_fix) · Vendor assessment No base score verified in the cited vendor record · Debian Bookworm `zlib` `1:1.2.13.dfsg-1`.

The overflow belongs to the contributed MiniZip ZIP writer, fixed upstream in MiniZip from zlib 1.3.1. The installed zlib package contains no MiniZip utility or library and libz does not export the affected MiniZip function. Python has no pyminizip dependency. The Debian source-package match stays visible in Trivy. Evidence: dpkg files, symbol and filesystem checks, both scans. [Debian Tracker](https://security-tracker.debian.org/tracker/CVE-2023-45853) · [mitre CVE record](https://www.cve.org/CVERecord?id=CVE-2023-45853) · [Exact Debian packaging source](https://deb.debian.org/debian/pool/main/z/zlib/zlib_1.2.13.dfsg-1.debian.tar.xz)

### CVE-2025-6020

**MITIGATED** · Trivy HIGH (affected) · Vendor assessment HIGH 7.8 · Debian Bookworm `pam` `1.5.2-6+deb12u2`.

The local namespace privilege issue requires a PAM session that uses pam_namespace on user-controlled paths. The installed Bookworm revision 1.5.2-6+deb12u2 includes a source backport and claims a fix in its changelog, but Debian explicitly reports an incomplete fix because the patch was not correctly applied during the build. The module remains installed and affected. RepoHarbor does not invoke PAM and supported Compose uses a non-root UID, no-new-privileges and no Linux capabilities. Evidence: Debian build note, checksummed source, module inventory and Compose review. [Debian Tracker](https://security-tracker.debian.org/tracker/CVE-2025-6020) · [redhat CVE record](https://www.cve.org/CVERecord?id=CVE-2025-6020) · [Linux-PAM upstream advisory](https://github.com/linux-pam/linux-pam/security/advisories/GHSA-f9p8-gjr4-j9gx) · [Exact Debian packaging source](https://deb.debian.org/debian/pool/main/p/pam/pam_1.5.2-6+deb12u2.debian.tar.xz)

### CVE-2025-69720

**MITIGATED** · Trivy HIGH (affected) · Vendor assessment HIGH 7.3 · Debian Bookworm `ncurses` `6.4-4`.

A crafted terminfo entry reaches analyze_string via the infocmp program. The CNA range covers ncurses 6.4 and identifies 6.5-20251213 as fixed. infocmp is installed. The daemon does not invoke it, expose a terminal capability importer or accept terminfo data through Git or the dashboard. Independent container shell commands can still invoke it. Evidence: executable inventory and process call-site review. [Debian Tracker](https://security-tracker.debian.org/tracker/CVE-2025-69720) · [mitre CVE record](https://www.cve.org/CVERecord?id=CVE-2025-69720) · [Exact Debian packaging source](https://deb.debian.org/debian/pool/main/n/ncurses/ncurses_6.4-4.debian.tar.xz)

### CVE-2025-7458

**FIXED** · Trivy CRITICAL (affected) · Vendor assessment MEDIUM 6.9 · Debian Bookworm `sqlite3` `3.40.1-2+deb12u2`.

The vulnerable SQLite expression planner requires attacker-controlled SQL. The Google CNA range covers 3.40.1 and names 3.41.2 as fixed. The baseline Python extension links this library, but RepoHarbor has no SQL query or database input interface. The rebuild removes libsqlite3-0 and the optional _sqlite3 extension. Evidence: SQLite source and baseline linkage, removal smoke check and post-build scan. [Debian Tracker](https://security-tracker.debian.org/tracker/CVE-2025-7458) · [Vendor advisory or fix](https://sqlite.org/forum/forumpost/16ce2bb7a639e29b) · [Google CVE record](https://www.cve.org/CVERecord?id=CVE-2025-7458) · [Exact Debian packaging source](https://deb.debian.org/debian/pool/main/s/sqlite3/sqlite3_3.40.1-2+deb12u2.debian.tar.xz)

### CVE-2026-105437

**MITIGATED** · Trivy HIGH (affected) · Vendor assessment HIGH 7.5 · Debian Bookworm `git` `1:2.39.5-0+deb12u3`.

A three-way content merge can corrupt heap memory on untrusted repository data. Debian Git 2.39.5 is within the GitLab CNA range and no named Bookworm backport was found. HTTPS clone/fetch transfer objects and refs into bare mirrors. The service and offline restore never merge branches or files. Evidence: Git/restore command review and actual mirror clone/fetch and restore smoke. [Debian Tracker](https://security-tracker.debian.org/tracker/CVE-2026-105437) · [Vendor advisory or fix](https://gitlab.com/gitlab-org/git/-/work_items/814) · [GitLab CVE record](https://www.cve.org/CVERecord?id=CVE-2026-105437) · [Exact Debian packaging source](https://deb.debian.org/debian/pool/main/g/git/git_2.39.5-0+deb12u3.debian.tar.xz)

### CVE-2026-107364

**NOT_AFFECTED** · Trivy HIGH (affected) · Vendor assessment Important · Debian Bookworm `krb5` `1.20.1-2+deb12u5`.

The vendor feed describes a remote denial of service in the Kerberos KDC request decoder. A listening KDC is required. The image has Kerberos client libraries through libcurl, but no krb5kdc program or KDC service. MIT and Red Hat confirm the server prerequisite. Evidence: MIT and Red Hat advisories, package inventory, executable checks and deployed listeners. [Debian Tracker](https://security-tracker.debian.org/tracker/CVE-2026-107364) · [MIT Security Advisory 2026-001](https://web.mit.edu/kerberos/advisories/MITKRB5-SA-2026-001.txt) · [Red Hat advisory](https://access.redhat.com/security/cve/CVE-2026-107364) · [Exact Debian packaging source](https://deb.debian.org/debian/pool/main/k/krb5/krb5_1.20.1-2+deb12u5.debian.tar.xz)

### CVE-2026-111760

**MITIGATED** · Trivy HIGH (affected) · Vendor assessment HIGH 8 · Debian Bookworm `git` `1:2.39.5-0+deb12u3`.

The GitHub advisory requires a recursive submodule clone of attacker-controlled repository data. The upstream affected range covers this Git release. The network clone uses --mirror, Git skips submodule checkout for bare clones, fetch explicitly disables recursion and local restore clones explicitly disable submodules. The subprocess blocks unmanaged configuration. Evidence: v2.39.5 clone source, application arguments and mirror/restore smoke. [Debian Tracker](https://security-tracker.debian.org/tracker/CVE-2026-111760) · [Vendor advisory or fix](https://github.com/git/git/security/advisories/GHSA-vp2w-7xxj-jvxr) · [GitHub_M CVE record](https://www.cve.org/CVERecord?id=CVE-2026-111760) · [Exact Debian packaging source](https://deb.debian.org/debian/pool/main/g/git/git_2.39.5-0+deb12u3.debian.tar.xz)

### CVE-2026-112315

**NOT_AFFECTED** · Trivy HIGH (affected) · Vendor assessment MEDIUM 5.3 · Debian Bookworm `krb5` `1.20.1-2+deb12u5`.

An unauthenticated RPCSEC_GSS decode error can exhaust memory in kadmind. The image contains neither kadmind nor the Kerberos administration RPC runtime. Git uses HTTPS and the dashboard exposes no administration RPC service. Evidence: Red Hat CNA, image package and executable inventory. [Debian Tracker](https://security-tracker.debian.org/tracker/CVE-2026-112315) · [redhat CVE record](https://www.cve.org/CVERecord?id=CVE-2026-112315) · [MIT Security Advisory 2026-001](https://web.mit.edu/kerberos/advisories/MITKRB5-SA-2026-001.txt) · [Exact Debian packaging source](https://deb.debian.org/debian/pool/main/k/krb5/krb5_1.20.1-2+deb12u5.debian.tar.xz)

### CVE-2026-112316

**NOT_AFFECTED** · Trivy HIGH (affected) · Vendor assessment MEDIUM 6.5 · Debian Bookworm `krb5` `1.20.1-2+deb12u5`.

An authenticated Kerberos administration request can leak memory during typed-data decoding in kadmind. No kadmind or administration RPC runtime is installed. Evidence: Red Hat CNA, image package and executable inventory. [Debian Tracker](https://security-tracker.debian.org/tracker/CVE-2026-112316) · [redhat CVE record](https://www.cve.org/CVERecord?id=CVE-2026-112316) · [MIT Security Advisory 2026-001](https://web.mit.edu/kerberos/advisories/MITKRB5-SA-2026-001.txt) · [Exact Debian packaging source](https://deb.debian.org/debian/pool/main/k/krb5/krb5_1.20.1-2+deb12u5.debian.tar.xz)

### CVE-2026-112317

**NOT_AFFECTED** · Trivy HIGH (affected) · Vendor assessment MEDIUM 5.3 · Debian Bookworm `krb5` `1.20.1-2+deb12u5`.

A KDC key-database decryption error can leak memory on an unauthenticated request. The KDC daemon, database service and associated server-side request path are absent. Evidence: Red Hat CNA and image inventory. [Debian Tracker](https://security-tracker.debian.org/tracker/CVE-2026-112317) · [redhat CVE record](https://www.cve.org/CVERecord?id=CVE-2026-112317) · [MIT Security Advisory 2026-001](https://web.mit.edu/kerberos/advisories/MITKRB5-SA-2026-001.txt) · [Exact Debian packaging source](https://deb.debian.org/debian/pool/main/k/krb5/krb5_1.20.1-2+deb12u5.debian.tar.xz)

### CVE-2026-112318

**NOT_AFFECTED** · Trivy HIGH (affected) · Vendor assessment MEDIUM 6.5 · Debian Bookworm `krb5` `1.20.1-2+deb12u5`.

An authenticated policy request can crash kadmind on a null policy name. The required administrator daemon and its RPC runtime are absent. Evidence: Red Hat CNA and image inventory. [Debian Tracker](https://security-tracker.debian.org/tracker/CVE-2026-112318) · [redhat CVE record](https://www.cve.org/CVERecord?id=CVE-2026-112318) · [MIT Security Advisory 2026-001](https://web.mit.edu/kerberos/advisories/MITKRB5-SA-2026-001.txt) · [Exact Debian packaging source](https://deb.debian.org/debian/pool/main/k/krb5/krb5_1.20.1-2+deb12u5.debian.tar.xz)

### CVE-2026-112319

**NOT_AFFECTED** · Trivy HIGH (affected) · Vendor assessment MEDIUM 6.5 · Debian Bookworm `krb5` `1.20.1-2+deb12u5`.

An authenticated administration RPC can induce unbounded XDR allocation in kadmind. The required daemon and administration RPC runtime are absent. Evidence: Red Hat CNA and image inventory. [Debian Tracker](https://security-tracker.debian.org/tracker/CVE-2026-112319) · [redhat CVE record](https://www.cve.org/CVERecord?id=CVE-2026-112319) · [MIT Security Advisory 2026-001](https://web.mit.edu/kerberos/advisories/MITKRB5-SA-2026-001.txt) · [Exact Debian packaging source](https://deb.debian.org/debian/pool/main/k/krb5/krb5_1.20.1-2+deb12u5.debian.tar.xz)

### CVE-2026-112320

**NOT_AFFECTED** · Trivy HIGH (affected) · Vendor assessment HIGH 7.5 · Debian Bookworm `krb5` `1.20.1-2+deb12u5`.

Switching RPC authentication flavors can leak memory in kadmind without authentication. The daemon and RPC service are absent. Normal Git HTTP authentication does not instantiate Kerberos administration RPC. Evidence: Red Hat CNA and image inventory. [Debian Tracker](https://security-tracker.debian.org/tracker/CVE-2026-112320) · [redhat CVE record](https://www.cve.org/CVERecord?id=CVE-2026-112320) · [MIT Security Advisory 2026-001](https://web.mit.edu/kerberos/advisories/MITKRB5-SA-2026-001.txt) · [Exact Debian packaging source](https://deb.debian.org/debian/pool/main/k/krb5/krb5_1.20.1-2+deb12u5.debian.tar.xz)

### CVE-2026-112321

**NOT_AFFECTED** · Trivy HIGH (affected) · Vendor assessment HIGH 7.5 · Debian Bookworm `krb5` `1.20.1-2+deb12u5`.

AUTH_GSSAPI_INIT error handling can leak allocations in the kadmind GSS-RPC server. No such server is installed. Evidence: Red Hat CNA and image inventory. [Debian Tracker](https://security-tracker.debian.org/tracker/CVE-2026-112321) · [redhat CVE record](https://www.cve.org/CVERecord?id=CVE-2026-112321) · [MIT Security Advisory 2026-001](https://web.mit.edu/kerberos/advisories/MITKRB5-SA-2026-001.txt) · [Exact Debian packaging source](https://deb.debian.org/debian/pool/main/k/krb5/krb5_1.20.1-2+deb12u5.debian.tar.xz)

### CVE-2026-112322

**NOT_AFFECTED** · Trivy HIGH (affected) · Vendor assessment HIGH 7.5 · Debian Bookworm `krb5` `1.20.1-2+deb12u5`.

Unauthenticated client-state cleanup can exhaust CPU on the kadmind RPC server. The required kadmin daemon and RPC server support are absent. Evidence: Red Hat CNA and image inventory. [Debian Tracker](https://security-tracker.debian.org/tracker/CVE-2026-112322) · [redhat CVE record](https://www.cve.org/CVERecord?id=CVE-2026-112322) · [MIT Security Advisory 2026-001](https://web.mit.edu/kerberos/advisories/MITKRB5-SA-2026-001.txt) · [Exact Debian packaging source](https://deb.debian.org/debian/pool/main/k/krb5/krb5_1.20.1-2+deb12u5.debian.tar.xz)

### CVE-2026-112323

**NOT_AFFECTED** · Trivy HIGH (affected) · Vendor assessment HIGH 7.5 · Debian Bookworm `krb5` `1.20.1-2+deb12u5`.

Faulty AUTH_GSSAPI_INIT requests can exhaust memory in kadmind. The required daemon and RPC server support are absent. Evidence: Red Hat CNA and image inventory. [Debian Tracker](https://security-tracker.debian.org/tracker/CVE-2026-112323) · [redhat CVE record](https://www.cve.org/CVERecord?id=CVE-2026-112323) · [MIT Security Advisory 2026-001](https://web.mit.edu/kerberos/advisories/MITKRB5-SA-2026-001.txt) · [Exact Debian packaging source](https://deb.debian.org/debian/pool/main/k/krb5/krb5_1.20.1-2+deb12u5.debian.tar.xz)

### CVE-2026-113351

**MITIGATED** · Trivy HIGH (fix_deferred) · Vendor assessment HIGH 8.2, MEDIUM 5.9 · Debian Bookworm `expat` `2.5.0-1+deb12u5`.

The parser-reset accounting defect covers Expat 2.7.2 through 2.9.0, while Bookworm reports its nominal 2.5.0 package as vulnerable. Treat the Debian build as affected pending a feature-level mapping of its patch set. Python's separate statically linked pyexpat reports 2.8.3 and is within range. Activation requires processing XML on a reused reset parser. RepoHarbor uses JSON for providers and SafeLoader for YAML. Neither the dashboard nor mirror/restore pipeline passes remote data to Expat. Evidence: runtime version and linkage, source review, provider and mirror tests. [Debian Tracker](https://security-tracker.debian.org/tracker/CVE-2026-113351) · [VulnCheck CVE record](https://www.cve.org/CVERecord?id=CVE-2026-113351) · [Exact Debian packaging source](https://deb.debian.org/debian-security/pool/updates/main/e/expat/expat_2.5.0-1+deb12u5.debian.tar.xz)

### CVE-2026-114466

**MITIGATED** · Trivy HIGH (affected) · Vendor assessment MEDIUM 4.4 · Debian Bookworm `git` `1:2.39.5-0+deb12u3`.

Git fast-import and fast-export process a separate import/export format with file-access features. The installed Git is within the reported range. RepoHarbor never runs either command. Offline bundle creation, verify and fetch use Git's pack/bundle format. Evidence: Git backup/restore call sites and bundle smoke. [Debian Tracker](https://security-tracker.debian.org/tracker/CVE-2026-114466) · [Vendor advisory or fix](https://gitlab.com/gitlab-org/git/-/work_items/812) · [GitLab CVE record](https://www.cve.org/CVERecord?id=CVE-2026-114466) · [Exact Debian packaging source](https://deb.debian.org/debian/pool/main/g/git/git_2.39.5-0+deb12u3.debian.tar.xz)

### CVE-2026-115506

**MITIGATED** · Trivy HIGH (fix_deferred) · Vendor assessment HIGH 8.7, HIGH 7.5 · Debian Bookworm `expat` `2.5.0-1+deb12u5`.

The Expat entity amplification check can allow excessive CPU use when parsing XML with a DTD. The published range includes the installed Debian Expat and CPython-bundled Expat. No provider, dashboard or restore API accepts parsed XML. git-http-push links Expat for WebDAV, but RepoHarbor only runs Git reads and does not use WebDAV push. Evidence: linkage, upstream Git HTTP helper source and provider/restore tests. [Debian Tracker](https://security-tracker.debian.org/tracker/CVE-2026-115506) · [VulnCheck CVE record](https://www.cve.org/CVERecord?id=CVE-2026-115506) · [Exact Debian packaging source](https://deb.debian.org/debian-security/pool/updates/main/e/expat/expat_2.5.0-1+deb12u5.debian.tar.xz)

### CVE-2026-117610

**MITIGATED** · Trivy HIGH (affected) · Vendor assessment HIGH 8.2, MEDIUM 5.9 · Debian Bookworm `expat` `2.5.0-1+deb12u5`.

The affected Expat external-entity parser can crash on an empty non-final chunk. The function exists in the Debian upstream baseline and the published range covers both XML engines in the image. RepoHarbor does not create external XML entity parsers or push using Git WebDAV. Evidence: upstream Expat code, runtime linkage and Git/provider call sites. [Debian Tracker](https://security-tracker.debian.org/tracker/CVE-2026-117610) · [VulnCheck CVE record](https://www.cve.org/CVERecord?id=CVE-2026-117610) · [Exact Debian packaging source](https://deb.debian.org/debian-security/pool/updates/main/e/expat/expat_2.5.0-1+deb12u5.debian.tar.xz)

### CVE-2026-11822

**FIXED** · Trivy HIGH (fix_deferred) · Vendor assessment MEDIUM 5.5, MEDIUM 6.8 · Debian Bookworm `sqlite3` `3.40.1-2+deb12u2`.

Crafted FTS5 index pages may trigger an out-of-bounds read during a MATCH query. The baseline build enabled FTS5 and includes fts5LeafSeek. The CNA names SQLite 3.53.2 as fixed. RepoHarbor never accepts or queries SQLite files. The rebuilt image removes the library and Python extension. Evidence: compile options, source, smoke and subsequent scan. [Debian Tracker](https://security-tracker.debian.org/tracker/CVE-2026-11822) · [Vendor advisory or fix](https://sqlite.org/src/info/87b653731171) · [VulnCheck CVE record](https://www.cve.org/CVERecord?id=CVE-2026-11822) · [Exact Debian packaging source](https://deb.debian.org/debian/pool/main/s/sqlite3/sqlite3_3.40.1-2+deb12u2.debian.tar.xz)

### CVE-2026-11824

**FIXED** · Trivy HIGH (fix_deferred) · Vendor assessment HIGH 8.5, HIGH 7.8 · Debian Bookworm `sqlite3` `3.40.1-2+deb12u2`.

A malformed FTS5 continuation page can drive a heap overflow in query processing. The baseline build enabled FTS5 and includes fts5ChunkIterate. SQLite 3.53.2 is the CNA fix boundary. The rebuilt image removes both the SQLite library and extension. Evidence: compile options, source, smoke and subsequent scan. [Debian Tracker](https://security-tracker.debian.org/tracker/CVE-2026-11824) · [Vendor advisory or fix](https://sqlite.org/src/info/061febcf41ca) · [VulnCheck CVE record](https://www.cve.org/CVERecord?id=CVE-2026-11824) · [Exact Debian packaging source](https://deb.debian.org/debian/pool/main/s/sqlite3/sqlite3_3.40.1-2+deb12u2.debian.tar.xz)

### CVE-2026-118370

**MITIGATED** · Trivy HIGH (affected) · Vendor assessment HIGH 8.2, MEDIUM 5.9 · Debian Bookworm `expat` `2.5.0-1+deb12u5`.

Expat start-tag attribute accounting can overflow on extremely large XML input. The published range includes both Expat versions in the image. RepoHarbor has no XML input path and bounds provider responses before JSON parsing. Git WebDAV push is outside the application's command set. Evidence: version/linkage and parser call-site review. [Debian Tracker](https://security-tracker.debian.org/tracker/CVE-2026-118370) · [VulnCheck CVE record](https://www.cve.org/CVERecord?id=CVE-2026-118370) · [Exact Debian packaging source](https://deb.debian.org/debian-security/pool/updates/main/e/expat/expat_2.5.0-1+deb12u5.debian.tar.xz)

### CVE-2026-118380

**NOT_AFFECTED** · Trivy HIGH (affected) · Vendor assessment Important · Debian Bookworm `krb5` `1.20.1-2+deb12u5`.

The vendor feed describes an active network attack against Kerberos RPCSEC_GSS services. It requires this RPC transport, distinct from HTTP Negotiate. The image has neither the GSS-RPC runtime nor the corresponding kadmin components. The MIT advisory identifies libgssrpc and kadmind. Evidence: MIT and Red Hat advisories and package inventory. [Debian Tracker](https://security-tracker.debian.org/tracker/CVE-2026-118380) · [MIT Security Advisory 2026-001](https://web.mit.edu/kerberos/advisories/MITKRB5-SA-2026-001.txt) · [Red Hat advisory](https://access.redhat.com/security/cve/CVE-2026-118380) · [Exact Debian packaging source](https://deb.debian.org/debian/pool/main/k/krb5/krb5_1.20.1-2+deb12u5.debian.tar.xz)

### CVE-2026-119079

**MITIGATED** · Trivy HIGH (affected) · Vendor assessment HIGH 7.5 · Debian Bookworm `krb5` `1.20.1-2+deb12u5`.

Kerberos ASN.1 sequence decoding can consume quadratic CPU time. The common libkrb5 decoder is installed. The documented network trigger targets unauthenticated KDC requests, and the image runs no KDC or Kerberos service. Git HTTPS may receive a Negotiate challenge, so this review does not equate an absent server with absent client code. Credential and process isolation limit the path. Evidence: CNA, installed client linkage, no server and HTTPS/credentials tests. [Debian Tracker](https://security-tracker.debian.org/tracker/CVE-2026-119079) · [redhat CVE record](https://www.cve.org/CVERecord?id=CVE-2026-119079) · [MIT Security Advisory 2026-001](https://web.mit.edu/kerberos/advisories/MITKRB5-SA-2026-001.txt) · [Exact Debian packaging source](https://deb.debian.org/debian/pool/main/k/krb5/krb5_1.20.1-2+deb12u5.debian.tar.xz)

### CVE-2026-119373

**FIXED** · Trivy HIGH (affected) · Vendor assessment HIGH 8.5, HIGH 7.8 · Debian Bookworm `sqlite3` `3.40.1-2+deb12u2`.

The vulnerable SQLite FTS5 output path merges prefix posting lists. The baseline source has fts5MergeRowidLists and the runtime enabled FTS5. The CNA names 3.54.0 as fixed. RepoHarbor has no SQLite input path. The library and extension were removed. Evidence: source and SQLite build flags plus smoke and subsequent scan. [Debian Tracker](https://security-tracker.debian.org/tracker/CVE-2026-119373) · [Vendor advisory or fix](https://sqlite.org/bugs/info/2026-08-04T08:49:27Z) · [VulnCheck CVE record](https://www.cve.org/CVERecord?id=CVE-2026-119373) · [Exact Debian packaging source](https://deb.debian.org/debian/pool/main/s/sqlite3/sqlite3_3.40.1-2+deb12u2.debian.tar.xz)

### CVE-2026-119374

**NOT_AFFECTED** · Trivy HIGH (affected) · Vendor assessment HIGH 8.5, HIGH 7.8 · Debian Bookworm `sqlite3` `3.40.1-2+deb12u2`.

The CNA requires the FTS5 secure-delete code path. SQLite added this feature in 3.42.0. The installed 3.40.1 source has neither the option nor fts5DoSecureDelete, and the Bookworm patch set does not add it. SQLite has also been removed from the rebuilt image. Evidence: SQLite FTS5 documentation, release source and checksummed Debian source. [Debian Tracker](https://security-tracker.debian.org/tracker/CVE-2026-119374) · [Vendor advisory or fix](https://sqlite.org/src/info/9cd4f9e920) · [VulnCheck CVE record](https://www.cve.org/CVERecord?id=CVE-2026-119374) · [FTS5 secure-delete version boundary](https://sqlite.org/fts5.html#the_secure_delete_configuration_option) · [Exact Debian packaging source](https://deb.debian.org/debian/pool/main/s/sqlite3/sqlite3_3.40.1-2+deb12u2.debian.tar.xz)

### CVE-2026-119379

**FIXED** · Trivy HIGH (affected) · Vendor assessment HIGH 8.5, HIGH 7.8 · Debian Bookworm `sqlite3` `3.40.1-2+deb12u2`.

Applying crafted SQLite session changesets can overflow an allocation. The baseline library enabled the session extension and includes sessionUpdateFind. The CNA names 3.54.0 as fixed. Python does not expose a RepoHarbor changeset ingestion path. Removal also excludes the library's C entry point. Evidence: compile options, source, smoke and subsequent scan. [Debian Tracker](https://security-tracker.debian.org/tracker/CVE-2026-119379) · [Vendor advisory or fix](https://sqlite.org/bugs/info/2026-08-16T10:15:25Z) · [VulnCheck CVE record](https://www.cve.org/CVERecord?id=CVE-2026-119379) · [Exact Debian packaging source](https://deb.debian.org/debian/pool/main/s/sqlite3/sqlite3_3.40.1-2+deb12u2.debian.tar.xz)

### CVE-2026-12064

**NOT_AFFECTED** · Trivy HIGH (fix_deferred) · Vendor assessment No base score verified in the cited vendor record · Debian Bookworm `curl` `7.88.1-10+deb12u15`.

The curl advisory requires the standalone curl tool's handling of a schemeless default SFTP/SCP URL and SSH host verification options. The image installs libcurl for Git but has no curl executable. RepoHarbor accepts only explicit HTTPS URLs for remote Git and prevents redirects. Evidence: curl CNA, executable inventory, URL and transport checks. [Debian Tracker](https://security-tracker.debian.org/tracker/CVE-2026-12064) · [Vendor advisory or fix](https://curl.se/docs/CVE-2026-12064.html) · [curl CVE record](https://www.cve.org/CVERecord?id=CVE-2026-12064) · [Exact Debian packaging source](https://deb.debian.org/debian/pool/main/c/curl/curl_7.88.1-10+deb12u15.debian.tar.xz)

### CVE-2026-16742

**NOT_AFFECTED** · Trivy HIGH (fix_deferred) · Vendor assessment MEDIUM 6.7 · Debian Bookworm `systemd` `252.39-1~deb12u2`.

The advisory concerns privilege escalation for a local account managed by systemd-homed. libsystemd and libudev are present, but no systemd-homed daemon or homed session is installed or started. The application starts directly as UID 10001. Evidence: systemd CNA, installed package/executable inventory and Compose configuration. [Debian Tracker](https://security-tracker.debian.org/tracker/CVE-2026-16742) · [systemd CVE record](https://www.cve.org/CVERecord?id=CVE-2026-16742) · [Exact Debian packaging source](https://deb.debian.org/debian/pool/main/s/systemd/systemd_252.39-1~deb12u2.debian.tar.xz)

### CVE-2026-41992

**MITIGATED** · Trivy HIGH (fix_deferred) · Vendor assessment MEDIUM 6.9 · Debian Bookworm `gzip` `1.12-1`.

GNU gzip's LZH and LZW decoders can mishandle shared state during a multi-input invocation. The gzip command is present and no named Bookworm fix was found. No application operation shells out to gzip or accepts LZH/LZW files for decompression. Git and Python use zlib interfaces independently. Evidence: upstream fix references, executable inventory and runtime command review. [Debian Tracker](https://security-tracker.debian.org/tracker/CVE-2026-41992) · [CERT-PL CVE record](https://www.cve.org/CVERecord?id=CVE-2026-41992) · [Exact Debian packaging source](https://deb.debian.org/debian/pool/main/g/gzip/gzip_1.12-1.debian.tar.xz)

### CVE-2026-53613

**MITIGATED** · Trivy HIGH (affected) · Vendor assessment HIGH 7 · Debian Bookworm `util-linux` `1:2.38.1-5+deb12u3, 2.38.1-5+deb12u3`.

Restricted non-root mount needs an authorized fstab mount with a mutable target ancestor and usable privilege elevation. The image contains setuid mount, but its fstab has no user mounts. The supported Compose deployment sets no-new-privileges and drops all capabilities. Docker establishes the backup bind mount outside the application. Evidence: fstab, executable mode, Compose and mount validation. [Debian Tracker](https://security-tracker.debian.org/tracker/CVE-2026-53613) · [GitHub_M CVE record](https://www.cve.org/CVERecord?id=CVE-2026-53613) · [Exact Debian packaging source](https://deb.debian.org/debian/pool/main/u/util-linux/util-linux_2.38.1-5+deb12u3.debian.tar.xz)

### CVE-2026-53615

**MITIGATED** · Trivy HIGH (affected) · Vendor assessment HIGH 8 · Debian Bookworm `util-linux` `1:2.38.1-5+deb12u3, 2.38.1-5+deb12u3`.

The DOS partition parser in libblkid can give incorrect results on crafted extended-partition metadata. Debian has libblkid and blkid, but RepoHarbor checks mounted filesystems and does not probe untrusted block devices. The supported deployment passes no devices and grants no mount capability. Evidence: CNA fixed version 2.41.4, tool inventory and Compose inspection. [Debian Tracker](https://security-tracker.debian.org/tracker/CVE-2026-53615) · [GitHub_M CVE record](https://www.cve.org/CVERecord?id=CVE-2026-53615) · [Exact Debian packaging source](https://deb.debian.org/debian/pool/main/u/util-linux/util-linux_2.38.1-5+deb12u3.debian.tar.xz)

### CVE-2026-54369

**MITIGATED** · Trivy HIGH (fix_deferred) · Vendor assessment HIGH 8.4, HIGH 7.1 · Debian Bookworm `acl` `2.3.1-3`.

The installed libacl pathname APIs follow path components in a way that can matter to a privileged caller. RepoHarbor neither invokes ACL editing utilities nor acts as a privileged ACL service. The supported container has no capabilities and no-new-privileges. Evidence: libacl CNA range below 2.4.0 and application filesystem/Compose review. [Debian Tracker](https://security-tracker.debian.org/tracker/CVE-2026-54369) · [VulnCheck CVE record](https://www.cve.org/CVERecord?id=CVE-2026-54369) · [Exact Debian packaging source](https://deb.debian.org/debian/pool/main/a/acl/acl_2.3.1-3.debian.tar.xz)

### CVE-2026-6276

**MITIGATED** · Trivy HIGH (affected) · Vendor assessment Low · Debian Bookworm `curl` `7.88.1-10+deb12u15`.

libcurl can reuse cookies for the wrong host when the same easy handle moves from a custom Host header to its default. Git's HTTP helper is linked and can reuse handles. RepoHarbor does not enable a cookie file or custom Host header, rejects local overrides and redirects, and starts a per-command helper for an explicit HTTPS remote. The Bookworm and backports versions fall within the affected ranges without an identified named backport. Evidence: curl CNA, installed Git HTTP source, managed configuration and TLS tests. [Debian Tracker](https://security-tracker.debian.org/tracker/CVE-2026-6276) · [Vendor advisory or fix](https://curl.se/docs/CVE-2026-6276.html) · [curl CVE record](https://www.cve.org/CVERecord?id=CVE-2026-6276) · [Exact Debian packaging source](https://deb.debian.org/debian/pool/main/c/curl/curl_7.88.1-10+deb12u15.debian.tar.xz)

### CVE-2026-71199

**MITIGATED** · Trivy HIGH (affected) · Vendor assessment Important · Debian Bookworm `krb5` `1.20.1-2+deb12u5`.

MIT identifies this as an authorization bypass involving Kerberos service tickets and CAMMAC authentication-indicator enforcement. Debian lists the shipped libkrb5 build as vulnerable and MIT fixes it in 1.22.3. Client libraries remain linked, but the service does not accept Kerberos tickets, issue them, or enforce their authorization indicators. The deployment provisions no Kerberos caches or keytabs. An external forge that itself relies on Kerberos authorization must patch its own realm and service. Evidence: MIT advisory, Debian tracker, link map and credential/API review. [Debian Tracker](https://security-tracker.debian.org/tracker/CVE-2026-71199) · [MIT Security Advisory 2026-001](https://web.mit.edu/kerberos/advisories/MITKRB5-SA-2026-001.txt) · [Red Hat advisory](https://access.redhat.com/security/cve/CVE-2026-71199) · [Exact Debian packaging source](https://deb.debian.org/debian/pool/main/k/krb5/krb5_1.20.1-2+deb12u5.debian.tar.xz)

### CVE-2026-71200

**NOT_AFFECTED** · Trivy HIGH (affected) · Vendor assessment Important · Debian Bookworm `krb5` `1.20.1-2+deb12u5`.

The vendor record concerns unauthenticated requests to a KDC configured for PKINIT. No KDC process or PKINIT server configuration exists in the image. Evidence: MIT and Red Hat advisories and package and executable inventory. [Debian Tracker](https://security-tracker.debian.org/tracker/CVE-2026-71200) · [MIT Security Advisory 2026-001](https://web.mit.edu/kerberos/advisories/MITKRB5-SA-2026-001.txt) · [Red Hat advisory](https://access.redhat.com/security/cve/CVE-2026-71200) · [Exact Debian packaging source](https://deb.debian.org/debian/pool/main/k/krb5/krb5_1.20.1-2+deb12u5.debian.tar.xz)

### CVE-2026-78410

**MITIGATED** · Trivy HIGH (affected) · Vendor assessment HIGH 7.8 · Debian Bookworm `util-linux` `1:2.38.1-5+deb12u3, 2.38.1-5+deb12u3`.

Restricted bind mounts can resolve a changed source and apply ownership or mode from a privileged fstab rule. The setuid mount executable is installed. RepoHarbor does not call mount, the image has no enabling fstab entry, and supported Compose drops capabilities and sets no-new-privileges. Evidence: image fstab and mode, Compose configuration and mount tests. [Debian Tracker](https://security-tracker.debian.org/tracker/CVE-2026-78410) · [redhat CVE record](https://www.cve.org/CVERecord?id=CVE-2026-78410) · [Exact Debian packaging source](https://deb.debian.org/debian/pool/main/u/util-linux/util-linux_2.38.1-5+deb12u3.debian.tar.xz)

### CVE-2026-8286

**MITIGATED** · Trivy HIGH (fix_deferred) · Vendor assessment No base score verified in the cited vendor record · Debian Bookworm `curl` `7.88.1-10+deb12u15`.

libcurl can reuse a connection with different TLS parameters during STARTTLS. Git uses direct HTTPS with certificate validation and a fixed CA configuration per command. It never requests an upgrade through a STARTTLS protocol. Treat the library as affected outside this usage. Evidence: curl CNA, Git transport config and custom-CA HTTPS smoke. [Debian Tracker](https://security-tracker.debian.org/tracker/CVE-2026-8286) · [Vendor advisory or fix](https://curl.se/docs/CVE-2026-8286.html) · [curl CVE record](https://www.cve.org/CVERecord?id=CVE-2026-8286) · [Exact Debian packaging source](https://deb.debian.org/debian/pool/main/c/curl/curl_7.88.1-10+deb12u15.debian.tar.xz)

### CVE-2026-8458

**MITIGATED** · Trivy HIGH (fix_deferred) · Vendor assessment Low · Debian Bookworm `curl` `7.88.1-10+deb12u15`.

The libcurl Negotiate reuse issue requires the same server and different authentication service names in a connection pool. HTTPS can offer Negotiate, but RepoHarbor passes neither a custom Negotiate service name nor a reusable pool across repositories. Git 2.39.5's HTTP helper does not set CURLOPT_SERVICE_NAME. No credential cache or keytab is provided in the supported configuration. Evidence: upstream Git HTTP source and credential/HTTPS integration tests. [Debian Tracker](https://security-tracker.debian.org/tracker/CVE-2026-8458) · [Vendor advisory or fix](https://curl.se/docs/CVE-2026-8458.html) · [curl CVE record](https://www.cve.org/CVERecord?id=CVE-2026-8458) · [Exact Debian packaging source](https://deb.debian.org/debian/pool/main/c/curl/curl_7.88.1-10+deb12u15.debian.tar.xz)

### CVE-2026-85091

**MITIGATED** · Trivy HIGH (affected) · Vendor assessment HIGH 8.3, HIGH 7.4 · Debian Bookworm `zlib` `1:1.2.13.dfsg-1`.

The zlib defect occurs in the gz-file write API after an earlier write failure. The baseline 1.2.13 requires the legacy error-clear sequence described in the CNA. Git pack processing and Python's zlib module use stream compression, not the gz-file writer, and the app does not recover or reuse a gz write handle after an I/O error. Keep Debian zlib for supported Git and Python operations. Evidence: upstream zlib CNA, link/runtime version and mirror/bundle tests. [Debian Tracker](https://security-tracker.debian.org/tracker/CVE-2026-85091) · [VulnCheck CVE record](https://www.cve.org/CVERecord?id=CVE-2026-85091) · [Exact Debian packaging source](https://deb.debian.org/debian/pool/main/z/zlib/zlib_1.2.13.dfsg-1.debian.tar.xz)

### CVE-2026-8927

**MITIGATED** · Trivy HIGH (fix_deferred) · Vendor assessment No base score verified in the cited vendor record · Debian Bookworm `curl` `7.88.1-10+deb12u15`.

The bug needs sequential requests on one easy handle, different proxies selected from the environment and Digest authentication to the first proxy. The Git subprocess drops proxy variables, isolates config, rejects proxy overrides and operates without redirects. Python provider HTTPS uses urllib rather than this libcurl handle. Evidence: curl CNA, Git environment and provider/HTTPS tests. [Debian Tracker](https://security-tracker.debian.org/tracker/CVE-2026-8927) · [Vendor advisory or fix](https://curl.se/docs/CVE-2026-8927.html) · [curl CVE record](https://www.cve.org/CVERecord?id=CVE-2026-8927) · [Exact Debian packaging source](https://deb.debian.org/debian/pool/main/c/curl/curl_7.88.1-10+deb12u15.debian.tar.xz)

### CVE-2026-9538

**MITIGATED** · Trivy HIGH (fix_deferred) · Vendor assessment No base score verified in the cited vendor record · Debian Bookworm `perl` `5.36.0-7+deb12u4`.

Perl Archive::Tar before 3.10 can exhaust memory reading an untrusted tar header. The image ships Archive::Tar 2.40 through Perl. RepoHarbor never extracts tar files or calls Archive::Tar, and Git bundles and mirrors do not use it. Evidence: module version and application process/restore review. [Debian Tracker](https://security-tracker.debian.org/tracker/CVE-2026-9538) · [CPANSec CVE record](https://www.cve.org/CVERecord?id=CVE-2026-9538) · [Exact Debian packaging source](https://deb.debian.org/debian-security/pool/updates/main/p/perl/perl_5.36.0-7+deb12u4.debian.tar.xz)

## D. Changes and verification

- Removed Debian `libsqlite3-0` `3.40.1-2+deb12u2` and CPython's optional `_sqlite3` binary module. RepoHarbor stores JSON and uses PyYAML as its runtime dependency. Neither app nor dependency requires a SQLite DB or SQL parser. The old build had both FTS5 and session support and the extension linked the system library. Using `dpkg --purge` fails the build if any future package requires SQLite, avoiding cascading removal. The image's standard Python interpreter is still present. The standalone SQLite API is no longer available in this application image.
- Kept all other package versions, the Debian Bookworm base and digest, CA setup, Git, Python, non-root user and container isolation. Removal is architecture-neutral. The actual runtime test was linux/amd64. Docker image layer accounting makes the total local size effectively unchanged, because the base layer remains part of the image history.
- Added a Docker smoke assertion for both the shared library and the Python extension. Image CI publishes an exact package inventory, Trivy version, this assessment, the image ID, CycloneDX SBOM, and raw scan. Each High/Critical package entry appears in the job summary. The gate still fails on any High/Critical result with a scanner-listed fixed version. A green gate alone does not assess entries without a listed fix. This review provides the applicability assessment for the recorded scan.
- Local Python 3.12 passed 1,323 tests including Chromium, with 97.04% line coverage and 93.94% branch coverage. Python 3.13 passed 1,322 tests, with the browser case handled separately, 97.00% line coverage and 93.94% branch coverage. Coverage gates passed on both versions. The packaged Docker smoke passed, including real HTTPS Git with the fixture CA, manual backups and retry, periodic integrity, offline clone and bundle restore, dashboard, headless startup, stop and restart. The unit, integration and fault-injection cases exercise provider discovery, credential isolation, scheduling, normal clone/fetch, recovery, repair, quarantine and limits. Ruff, Mypy, Bandit at the CI threshold, pip-audit and Compose validation were run without changing thresholds or enabling a scanner ignore list.

## E. Residual risk

1. Keep the documented Compose controls enabled, including no-new-privileges and no capabilities. Local mount, ACL and PAM code remains installed. A privileged or multi-user adaptation of this image changes the applicability of those findings. The Bookworm PAM build in particular needs a verified vendor fix before enabling an affected PAM namespace service.
2. Keep Git on explicit HTTPS with the managed Git configuration and without cookie, proxy, custom Host header or cross-service Negotiate configuration. Git/libcurl/Kerberos remain runtime dependencies. Continue following signed Debian updates. A Git client talking to an external forge cannot compensate for an unpatched Kerberos realm or SPNEGO service operated by the forge.
3. Adding an XML parser entry point, an LDAP transport, archive extraction, submodule checkout, live Git merges, a Kerberos service, or additional host devices requires renewed applicability review. Expat, zlib, Perl and the Debian utilities remain present for the current image and its dependencies. A new Python build should recheck bundled Expat separately from Debian libexpat.

## F. Before/after scan comparison

The original CI artifact contains 118 High and 2 Critical package/advisory pairs. The rebuilt image has 113 High and 1 Critical. The six removed rows are CVE-2025-7458, CVE-2026-11822, CVE-2026-11824, CVE-2026-119373, CVE-2026-119374 and CVE-2026-119379. Every retained High/Critical pair keeps its original version, rating and scanner fix field. The second scan introduced no additional High/Critical pairs. The remaining Critical scanner entry is CVE-2023-45853, for MiniZip code absent from the runtime. The full raw report and all ratings remain visible in CI. [Baseline artifact](https://github.com/Play1live/RepoHarbor/actions/runs/44937961536/artifacts/14720980680)

## G. Release assessment

The tested runtime is suitable for the final isolated NAS test under the supported non-root and capability-free deployment. The audited High/Critical entries do not establish a blocking exposure through supported RepoHarbor functions. Release should follow a successful target-NAS backup, restart and offline restore using the deployment controls above, plus successful CI on the exact PR head. Production storage, network and permissions were outside this fixture-based review. Reassess newly flagged entries and removed controls when promoting a rebuild.
