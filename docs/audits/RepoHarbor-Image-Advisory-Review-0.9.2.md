# RepoHarbor 0.9.2 runtime image advisory review

## Scope and disposition

This assessment covers the supported non-root RepoHarbor Docker/NAS deployment, with all Linux capabilities dropped, no-new-privileges, and the documented backup and secret mounts. It evaluates reachable application behavior. The raw Trivy results continue to record all Debian and Python package findings at their original severities.

| Evidence | Original image | Reviewed runtime |
| --- | --- | --- |
| Distinct High/Critical advisory IDs | 44 | 38 |
| High/Critical package/advisory pairs | 120 | 114 |
| High/Critical pairs with a scanner-listed package fix | 0 | 0 |
| CycloneDX SBOM components | 126 | 125 |

The earlier removal of unused SQLite code accounts for the six fewer scanner entries. Five represented affected code removed from the image, and one required an FTS5 feature absent from the original SQLite release. The transport follow-up retains the pinned Bookworm base and package versions. It changes Git process behavior, so the package findings remain visible. [Original CI scan and SBOM](https://github.com/Play1live/RepoHarbor/actions/runs/44937961536/artifacts/14720980680) · [Complete historical assessment](https://github.com/Play1live/RepoHarbor/blob/d158dedf05d957375b5429c50bed40eeb8e1914f/docs/audits/RepoHarbor-Image-Advisory-Review-0.9.2.md) · [Prior runtime scan and SBOM](https://github.com/Play1live/RepoHarbor/actions/runs/44945621043/artifacts/14723646320)

Of the 38 remaining scanner IDs, 34 are outside the supported application paths and four are addressed by the Git HTTPS boundary. The review identified no open advisory with a plausible unguarded path in the supported deployment. These conclusions apply to the application and its documented deployment, while other applications, tools, or runtime roles added to the image can expose affected package code.

## Findings outside the supported runtime paths

The initial 32 scope exclusions were rechecked and all 32 are excluded from the active risk list.

- **15 require an absent component.** The image inventory has no MiniZip code or standalone curl utility, Kerberos KDC or administrative server, Kerberos administration RPC runtime, or systemd-homed. The loaded zlib library does not export the affected MiniZip writer. The application starts directly as the configured UID, performs reads over Git HTTPS and exposes neither a Kerberos service nor a system user authentication endpoint. The Docker smoke checks the absence of the Kerberos servers, GSS-RPC library and systemd-homed.
- **17 involve code with no supported invocation.** Git probes, mirror clone/fetch and offline restore use an explicit command set. Checkout does not merge, and the mirror and restore paths do not recurse into submodules. Git bundle operations use Git object packs with no fast import or export. The linked LDAP and Expat code has no reachable parser path here. Git transport is HTTPS, Git's WebDAV push client is unused, and provider responses enter the JSON parser. Python also bundles Expat, but the application does not process XML. The service does not invoke PAM sessions, terminal capability tools, GNU gzip, mount or block probing, ACL editing, the zlib gzip-file writer, or Perl archive extraction. Docker establishes the host bind mounts before the service starts. The non-root and capability-free deployment is part of this assessment.

The Kerberos client libraries remain linked through Git's libcurl. Two additional advisories were excluded after checking the exact vendor prerequisites.

- **CVE-2026-119079** requires a KDC that receives a crafted unauthenticated AS-REQ or TGS-REQ over TCP. RepoHarbor accepts neither request type. It has no KDC executable, package, listener or dispatch path for those requests. The client-side library dependency does not supply a KDC. [Debian advisory](https://security-tracker.debian.org/tracker/CVE-2026-119079) · [MIT Kerberos advisory](https://web.mit.edu/kerberos/advisories/MITKRB5-SA-2026-001.txt)
- **CVE-2026-71199** concerns authorization based on CAMMAC indicators in Kerberos service tickets. The dashboard uses its own password and session verification. No supported role issues service tickets, accepts them as a Kerberos service or grants access based on CAMMAC indicators. An external Kerberos-enabled forge remains responsible for its own KDC and service authorization. [Debian advisory](https://security-tracker.debian.org/tracker/CVE-2026-71199) · [MIT Kerberos advisory](https://web.mit.edu/kerberos/advisories/MITKRB5-SA-2026-001.txt)

## Git HTTPS advisories with verified safeguards

Git's HTTP helper uses Debian libcurl. For discovery, backup and repair, RepoHarbor accepts explicit credential-free HTTPS URLs. Git starts with an allowlisted environment, no system or global Git configuration, disabled redirects and full certificate validation. `SSL_CERT_FILE` remains supported for a trusted custom CA. Each remote operation has its own Git process. In a managed mirror, fetch and repair enforce a local configuration allowlist before an explicit URL and refspec are used. Offline restore keeps its separate local-only transport boundary. [Git process and mirror policy](../../src/repoharbor/git_backup.py) · [Environment and trust](../../src/repoharbor/credentials.py) · [URL validation](../../src/repoharbor/config.py)

The follow-up closes an implicit configuration path. Git could previously discover a repository in the invoking directory and read its transport settings during clone or remote enumeration. The two repository-independent commands now disable discovery of the invoking directory's Git configuration. Other Git operations retain their explicit repository context. The local TLS regression failed on the previous revision when the server observed a fixture Host header from the invoking repository. With the fix it receives the expected authority, and authenticated enumeration and mirror clone still succeed.

| Advisory | Trigger in libcurl | Verified RepoHarbor boundary |
| --- | --- | --- |
| [CVE-2026-6276](https://curl.se/docs/CVE-2026-6276.html) | Reusing an easy handle for a request after clearing a custom Host header can use stale cookie host information. | Supported configuration exposes neither custom Host headers nor Git cookie files. Invoking-directory and parent environment config cannot inject them. A managed mirror rejects extra headers, cookie files and cookie saving. The local HTTPS endpoint checks the expected Host and absence of outbound cookies. |
| [CVE-2026-8286](https://curl.se/docs/CVE-2026-8286.html) | A STARTTLS transfer can reuse a connection whose TLS settings do not match. | HTTPS starts inside TLS. Direct requests using alternate network protocols, redirects to another authority or plaintext HTTP, and mirror attempts to enable alternate transports or disable TLS verification are rejected. The local HTTPS test verifies rejection without the fixture CA and success with it. |
| [CVE-2026-8458](https://curl.se/docs/CVE-2026-8458.html) | Negotiate can reuse a connection to the same server despite different target service names. | Git may encounter a Negotiate challenge. RepoHarbor exposes no service-name or delegation override, filters inherited Kerberos cache, config and keytab variables and provisions no Kerberos material. The mirror rejects corresponding configuration. Separate subprocesses keep independent connection pools. Two local HTTPS authorities require distinct fixture credentials and independently challenge each operation. |
| [CVE-2026-8927](https://curl.se/docs/CVE-2026-8927.html) | Sequential requests on a reused handle switch between environment-selected proxies after Digest proxy authentication. | Proxy variables are excluded from the Git child environment and mirror proxy and proxy-auth settings are rejected. Invoking-directory configuration cannot opt Git into a proxy. Local TLS with inherited fixture proxy settings verifies direct connectivity and absence of proxy authorization. |

The curl code remains installed. These guards address its documented prerequisites in RepoHarbor. A shell, a separate application, a privileged deployment, a configured Kerberos environment, or unsupported changes to Git or transport policy require reassessment. An operating system package update should still be applied when a suitable fix is available.

## Verification and release

The regression exercises a working local Git HTTPS server with synthetic PAT-style credentials, independent authorities, a private test CA, denied redirects and denied alternate protocols. Tests use local fixtures and inspect accepted or rejected configuration, request routing, and credential isolation. The supported deployment requires no new configuration or backup migration. The pinned scanner, full raw output, available-fix gate, package inventory and CycloneDX SBOM remain part of CI.

The supported Docker/NAS deployment can proceed to isolated acceptance testing. Verify the target mount, non-root identity, capability restrictions and CA trust on the NAS, then run a backup, restart and offline restore. Runtime controls do not extend to extra services or commands run by operators inside the image.
