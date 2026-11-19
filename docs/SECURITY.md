# Security, data integrity, and recovery

This document describes the security and recovery guarantees of RepoHarbor `0.4.0`. Production NAS/Portainer acceptance requires validation on the target host.

## Trusted storage boundary

The backup root must be a dedicated, persistent, trusted local filesystem with reliable `flock`, atomic rename, and `fsync` behavior. The container requires `/backups` to be an actual mount; the host operator must **independently confirm that the intended NAS device is mounted**. A Docker bind mount only verifies the container-side path. SMB/NFS consistency is not guaranteed for an active RepoHarbor backup root.

The service refuses symlinks and special files within the backup-root tree. Do not grant untrusted actors concurrent write access to that tree. The data directory includes the central journal, per-repository metadata, mirrors, temporary clones, quarantine, and any operator-preserved archives.

## Local control and status

The service listens on a Linux abstract Unix-domain socket addressed by its effective UID and the resolved backup root. The client and server each verify the peer UID with `SO_PEERCRED`. The endpoint is scoped to the Linux network namespace and leaves no socket file on the backup mount. Use `docker exec` under the service's configured user. Operators with access to that UID in the container can request manual backups when the flag is enabled. The service exposes no TCP control port, web dashboard, or Docker healthcheck.

Manual backups are opt-in. The running service owns the root lock and executes all Git work. Scheduled and manual requests share one run lock, and overlapping requests are skipped or rejected without queueing. Disconnecting the CLI leaves the service in control of accepted work. The client cannot infer a successful result from that disconnection.

Status and repository inspection read the on-disk state and do not trigger discovery, Git operations, or recovery. A service reachability query uses the same local socket. Restrict access to the backup mount and container because status includes repository identity and operational metadata.

## Git transport and credentials

- Production Git operations accept credential-free **HTTPS** URLs only and invoke Git with argument vectors, not shell interpolation.
- Git starts with an isolated environment and disabled system/global configuration, interactive credential prompts, arbitrary credential helpers, hooks, and filesystem monitors. HTTPS certificate checks remain enabled, redirects are disallowed, and other transport protocols are disabled.
- A credential is supplied through a scoped `GIT_ASKPASS` helper environment, **not** through a URL or command-line argument. The container permits one explicit secret source, `env` or `file`.
- Git diagnostics from remote servers are never directly forwarded into application logs or persisted failure messages; fixed internal error codes are used instead.
- Secrets in a child process environment may still be inspected by users with sufficient OS/container privileges. Limit Docker-host access and use protected, read-only secret-file mounts where possible.
- Configured self-hosted HTTPS endpoints remain network-reachable according to the container's network policy; repository operators must restrict trust and outbound access appropriately.

Local Git transports are enabled **only in test fixtures**, not as a production configuration option. Python static analysis blocks medium/high severity findings. The shell-free Git subprocess boundary is still a subprocess and needs continued review as dependencies and Git versions change.

## GitHub API and identity boundary

Discovery uses the official `https://api.github.com` REST endpoint with TLS verification enabled. Tokens are resolved through the same exclusive env/file credential system and sent only in an Authorization header. The API client rejects redirects; pagination is validated against the fixed host/path and expected next page. Git clone targets are reconstructed from validated GitHub owner/name fields and must match the API's canonical GitHub URLs. Remote errors and response bodies are not logged or stored; only fixed error codes are exposed.

Requests have socket/deadline, response-size and page-count bounds. HTTP 401, 403, 404, server failures, malformed responses and primary/secondary rate limits remain distinct from a confirmed deletion. Rate-limit cooldowns are persisted. API visibility is limited by each token's grants; metadata access does not itself prove Git contents access. Successful discovery updates only `source.last_seen_at` and the separate `source.discovery_status`; it preserves the Git `source.status`, diagnostics and source extensions. New repositories retain unknown Git status until actual successful Git access. Absence from complete discovery changes the API status only; incomplete discovery leaves both observations unchanged. Excluded or storage-limited repositories receive no false Git success confirmation. These additive metadata fields retain schema version 1 and use the existing atomic persistence/recovery path. Git uses the first in-scope configured token and the existing isolated Askpass/HTTPS boundary. There is no automatic Git credential fallback.

The registry deduplicates by provider, host and immutable repository ID, retaining confirmed address history. Contradictory IDs/properties or reuse of a retained address by another ID fail closed. Stable numeric directories avoid moving mirrors on rename/transfer. Arbitrary manual sources keep their original identity contracts. URL equality or a newly resolved API ID does not prove ownership of an old manual mirror: conflicts block automatic assignment and never authorize adoption or overwrite.

A failed or incomplete token discovery suspends the entire GitHub part of that run. No incomplete selection is applied and no stale selection permits remote repair. Manual sources on other authorities and established eligible manual mirrors on the affected authority can continue. A new or unproven same-authority mirror waits for successful discovery. Manual address resolution is conservative; unresolved or redirected GitHub addresses pause automatic discovery rather than risk a duplicate. Full rules and the limitation around unknown historical manual aliases are documented in [configuration](CONFIGURATION.md#stable-paths-and-manual-conflicts).

## GitLab API and instance boundary

The GitLab v4 client requires an explicit HTTPS authority before it can send requests. It supplies the token in the `PRIVATE-TOKEN` header. Use a human user's personal access token with `read_api` and sufficient project/group visibility for discovery, plus `read_repository` and project read access for Git. Group and project access tokens and bot accounts are unsupported. Credential references are bound to a provider type and authority. A manual source can reuse a bound credential only at its matching authority. Git uses the first in-scope credential via isolated Askpass with username `oauth2`; a Git failure does not trigger token fallback.

TLS verification applies to the API and Git. For an instance with a private CA, mount a trusted CA bundle read-only and set `SSL_CERT_FILE`. A Self-Managed instance must expose its API at the root `/api/v4` path and return canonical HTTPS project URLs for the same lowercase DNS or IPv4 authority, including any configured non-default port. The API client rejects redirects. RepoHarbor constructs clone and web URLs from the configured authority and validated namespace and project paths and requires exact agreement with the API fields. Git also disables redirects.

Pagination validates the authority, endpoint, fixed parameters, and cursor or page progression. Requests are built locally within page-count, response-size, socket, and discovery deadline bounds. These checks constrain API-supplied destinations. The operator still controls the configured authorities and manual URLs, and the application does not filter resolved addresses or pin DNS resolutions. Configured servers may resolve to private networks. Control configuration, DNS, and outbound network access accordingly. See [API pacing and rate limits](CONFIGURATION.md#api-pacing-and-rate-limits) for the bounds and headers.

Complete discovery on one authority requires all of its configured tokens and manual identity lookups to succeed. API errors, a cooldown, incomplete pagination, and ambiguous identities withhold its Git work and remote repairs. Other GitLab authorities and GitHub remain independent. On the affected authority, only established eligible manual mirrors can continue, under the same checks in full and targeted runs. GitLab stores cooldowns in the central journal, including a zero-quota HTTP 200 cooldown before the response body is read. GitLab hosts retain their stored cooldowns across restart and temporary removal.

A successful API discovery does not guarantee a successful mirror backup. API presence updates `source.discovery_status` and `source.last_seen_at`. Only Git access can confirm Git availability. Git may fail despite API visibility, for example if the token lacks Git read permission. GitLab can omit `default_branch` or return null with limited access. RepoHarbor records it as unknown. The field establishes neither an empty project nor permission to read Git data.

Stable identity includes the provider, authority, and project ID. Conflicting observations or reuse of a confirmed address by a different ID suspend the affected discovery. Manual collisions block automatic assignment and do not authorize adopting a manual mirror. Manual identity queries use only same-authority GitLab credentials, in configuration order, advancing only after HTTP 404. See [manual identity handling](CONFIGURATION.md#stable-paths-and-manual-conflicts).

## Journaling and crash consistency

The single-writer lock prevents two active RepoHarbor instances from mutating the same root. The central `data.json` journal is persisted **before Git mutations**, using temporary-file writes, flush/`fsync`, atomic replacement, and parent-directory synchronization. Repository metadata is written before marking an operation complete. Scheduler overlap events and worker journal transitions use serialized, durable read-modify-write transactions.

Crash recovery reconciles interrupted operations, records proven completion only once, and avoids falsely advancing `last_checked_at` or `last_updated_at`. A successful `git fetch --atomic` protects reference updates, **not the entire object database as one filesystem transaction**. A crashed update may still require a Git integrity check and repair.

An existing valid mirror is **never automatically deleted or replaced by a fresh clone**. Removing a source from configuration preserves its local backup. A remote disappearance or authentication failure does not prove that the repository was deleted.

Discovery records its own state in the existing central `data.json` without replacing the active Git-operation slot. Repository metadata is atomically written, but the catalog is **not** a multi-file atomic filesystem transaction. Runnable GitHub sources and the runnable sources for each GitLab authority are published in memory only after all metadata writes and the completion marker are durable. A crash during catalog publication may leave some metadata updated; startup permits no discovered-source remote work from that partial catalog, and a fresh complete discovery must recompute selection. Missing metadata in a newly created directory fails closed and may require operator review; it is never silently overwritten or re-cloned. Existing Git journals and completion markers continue to govern backup recovery.

## Interrupted initial clones and quarantine

A temporary `repository.git.partial/` becomes a valid `repository.git/` **only** when completion is supported by a durable journal marker and a fresh integrity check. Structural health by itself does not establish that the initial network clone completed.

An identified interrupted first clone may be moved through a journaled rename into **one reserved slot** per source: `quarantine/initial-clone/`. Quarantined bytes remain preserved and count toward both repository and global size budgets. Startup reconciliation does **not** start a new clone; a later eligible scheduled or manual run can retry.

Unknown or unowned partials, excluded sources, a second partial while quarantine is occupied, missing quarantine content, and conflicting paths set persistent `backup.manual_attention_required` and prevent further automatic downloads. The service does not rotate or delete quarantines automatically. Missing/corrupt journal or metadata files fail closed rather than being silently reset. Only schema version `1` is accepted. GitHub and GitLab metadata and the journal's `discovery` and `provider_discovery` entries are additive extensions. Existing manual state needs no migration.

### Operator procedure for `manual_attention_required`

1. **Stop RepoHarbor** before modifying its backup root. Inspect `data.json`, the repository's `metadaten.json`, the partial, and the quarantine without destroying evidence.
2. Preserve any existing valid `repository.git/`. Do not replace it with a partial, delete the journal, or edit successful completion timestamps to bypass recovery.
3. If the partial or occupied quarantine must be moved, archive it **without overwriting** existing files to a unique, operator-chosen path under `<backup-root>/operator-archive/`, outside `manual/`.
4. Keep archives until their content has been reviewed; they still count against the global backup-root budget. Local `git fsck --full` and a separate offline clone from an existing mirror can help with diagnosis, but do not prove an interrupted initial clone completed.
5. If a recorded **pending** quarantine is missing, restore the exact preserved content to that slot before restarting; otherwise leave recovery blocked and investigate manually.
6. Restart the service only after resolving the conflict. Reconciliation clears an obsolete attention marker when safe; a later scheduled or manual run can then attempt backup again if capacity permits.

Do not use broad `rm -rf`, permission resets, or lock-file deletion as a troubleshooting shortcut. File existence does not mean that an OS lock remains held after the process exits.

## Integrity checks and bounded repair

Interrupted or suspect mirrors receive an on-demand local `git fsck --full` integrity check. Unreachable/dangling Git objects alone are not corruption. For confirmed damaged mirrors, repair attempts to refetch **existing ref-tip object IDs** without destination refspecs or implicit ref mappings, then checks integrity again. This is intentionally not a general-purpose Git repair system.

At most three genuine failed repair attempts are counted per incident; a successful repair resets the incident counter. Completed repair operation IDs make success/error counters idempotent after crashes. SIGTERM/SIGINT, interruption and capacity pauses do **not** consume a repair-failure attempt. A needed repair blocked by storage is `repair_paused` with `repair_deferred`, then reevaluated during a later eligible backup run without a service restart. Exhausted genuine attempts stay `repair_failed`, even if space is also low.

Remote repair downloads require an included repository, enabled automatic repair, adequate capacity, and permitted Git access. GitHub and GitLab repairs additionally require a fresh, complete selection for their discovery scope in the current backup run. Boot reconciliation for discovered sources is local. Needed remote repairs wait with `DISCOVERY_REQUIRED`. **Excluded repositories receive no remote repair download**. If objects were deleted upstream or packs are irrecoverable, `repair_failed` can require manual operator action. The service keeps the old refs; it does not automatically replace the mirror.

## Signals and failure handling

SIGTERM/SIGINT stop new Git work, terminate the active Git process group, and escalate to SIGKILL after five seconds if needed. The process reaps its children; a forced SIGKILL cannot execute cleanup, so the next startup must reconcile the durable journal. A surviving Git process retains the backup-root lock until it exits. Fatal persistence errors stop the worker/service, preserving recoverable state rather than proceeding with an unjournaled mutation.

Stored error history preserves total and consecutive counters plus at most the last 20 events, without raw remote stderr or token values. Storage measurements are point-in-time observations, not enforced filesystem quotas. A physical free-space reserve reduces risk but cannot guarantee protection from concurrent writers or sudden disk exhaustion.

## Scope and operational acceptance

A mirror does **not** provide historical snapshots. Deleted remote refs and force-pushes propagate to the mirror. Git LFS data, separate submodule repositories, issues, pull/merge requests, hosting metadata, and never-pushed local objects are not backed up by this version. Notifications are outside its scope.

Follow the [deployment guide](../deploy/README.md) for host-mount validation, non-root permissions, credential setup, updates, and an offline restore. See [configuration](CONFIGURATION.md) for status semantics and [testing](TESTING.md) for automated checks. Actual NAS/Portainer deployment and disaster-recovery acceptance remain separate operator tasks.
