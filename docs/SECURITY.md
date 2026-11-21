# Security, data integrity, and recovery

This document describes the security and recovery guarantees of RepoHarbor `0.9.1`. Production NAS/Portainer acceptance requires validation on the target host.

## Trusted storage boundary

The backup root must be a dedicated, persistent, trusted local filesystem with reliable `flock`, atomic rename, and `fsync` behavior. The container requires `/backups` to be an actual mount; the host operator must **independently confirm that the intended NAS device is mounted**. A Docker bind mount only verifies the container-side path. SMB/NFS consistency is not guaranteed for an active RepoHarbor backup root.

The service refuses symlinks and special files within the backup-root tree. Do not grant untrusted actors concurrent write access to that tree. The data directory includes the central journal, per-repository metadata, mirrors, temporary clones, quarantine, and any operator-preserved archives.

## Local control and status

The service listens on a Linux abstract Unix-domain socket addressed by its effective UID and the resolved backup root. The client and server each verify the peer UID with `SO_PEERCRED`. The endpoint is scoped to the Linux network namespace and leaves no socket file on the backup mount. Use `docker exec` under the service's configured user. Operators with access to that UID in the container can request manual backups when the flag is enabled. The optional HTTPS dashboard provides a separate authenticated entry point. The default deployment has no TCP listener.

Manual backups are opt-in. The running service owns the root lock and executes all Git work. Scheduled and manual requests share one run lock, and overlapping requests are skipped or rejected without queueing. Disconnecting the CLI leaves the service in control of accepted work. The client cannot infer a successful result from that disconnection.

Status and repository inspection read the on-disk state and do not trigger discovery, Git operations, or recovery. A service reachability query uses the same local socket. Restrict access to the backup mount and container because status includes repository identity and operational metadata.

## Web dashboard

The dashboard requires TLS 1.2 or later and a generated administrator password. HTTPS serves the sign-in shell and a fixed set of application assets before authentication. Every operational status response requires an administrator session. Sessions expire after 15 minutes, are cleared on restart, and use a host-only cookie with `Secure`, `HttpOnly`, and `SameSite=Strict`.

At startup RepoHarbor derives a 32-byte verifier with OpenSSL-backed scrypt and a fresh, random 16-byte salt. It keeps the verifier and salt in memory and compares login candidates with `hmac.compare_digest`. The work factors are `N=32768`, `r=8`, and `p=3`, the OWASP 32 MiB scrypt profile. OpenSSL has a 40 MiB limit per derivation, including its workspace overhead. A missing or unavailable KDF fails closed without a weaker substitute. See the [OWASP password storage guidance](https://cheatsheetseries.owasp.org/cheatsheets/Password_Storage_Cheat_Sheet.html#scrypt).

The server allows five failures per TCP peer address in a rolling 60-second window and tracks at most 256 peer addresses with failures. It discards expired entries on subsequent sign-in attempts. Only one password derivation can run at once. Concurrent attempts and attempts from untracked peers when the table is full receive HTTP 429 without starting another derivation. Existing sessions use a separate lock. A reverse proxy or shared NAT can make several clients share one limit. The server ignores `Forwarded` and `X-Forwarded-For` when identifying peers. Treat the dashboard as a single-administrator interface on a trusted management network or behind a VPN.

The server checks the exact configured host and checks the browser origin on every POST. Backup, manual integrity, and sign-out requests also require a session-bound CSRF token. The backup and integrity endpoints recheck `features.manual_run` and route accepted operations through the existing UID-checked control socket. Scheduled jobs, retries, and manual requests share the service execution lock. If the browser disconnects after acceptance, the service still owns the operation and its outcome.

Manual integrity checks resolve validated repository IDs against the local catalog. They use the same structure, pack/index, and full Git checks as scheduled verification. Git probes disallow transport and lazy fetching. Manual findings update metadata outside the mirror and leave object contents, references, and automated repair decisions intact.

The dashboard has fixed asset routes, allowlisted status and event fields, and no arbitrary file access or export endpoints. Repository identifiers are length-bounded and their provider, authority and numeric ID are validated before use. Repository text is rendered as text and links are validated as credential-free HTTPS URLs. Git credentials, host file paths, raw responses, logs, and exception traces are excluded from web responses. The response policy blocks framing and third-party resources and disables caching of application data.

Keep the TLS key and dashboard password in read-only mounts that the service UID can read. Use a distinct dashboard password, restrict access to the published endpoint, and validate certificates throughout the network path. With a reverse proxy, preserve the browser-facing `Host` and `Origin`, verify backend TLS, and restrict the backend listener to the proxy. See the [deployment guide](../deploy/README.md#enable-the-web-dashboard).

## Git transport and credentials

- Network backup and repair operations accept credential-free **HTTPS** URLs only and invoke Git with argument vectors, not shell interpolation.
- Git starts with an isolated environment and disabled system/global configuration, interactive credential prompts, arbitrary credential helpers, hooks, and filesystem monitors. For network backups and repairs, HTTPS certificate checks remain enabled, redirects are disallowed, and other transport protocols are disabled.
- A credential is supplied through a scoped `GIT_ASKPASS` helper environment, **not** through a URL or command-line argument. The container permits one explicit secret source, `env` or `file`.
- Git diagnostics from remote servers are never directly forwarded into application logs or persisted failure messages; fixed internal error codes are used instead.
- Secrets in a child process environment may still be inspected by users with sufficient OS/container privileges. Limit Docker-host access and use protected, read-only secret-file mounts where possible.
- Configured self-hosted HTTPS endpoints remain network-reachable according to the container's network policy; repository operators must restrict trust and outbound access appropriately.

Backup configuration accepts only HTTPS Git sources. The restore assistant enables local file transport only for importing its own staged bundle. Isolated test fixtures can also enable local Git transport. Python static analysis blocks medium/high severity findings. The shell-free Git subprocess boundary is still a subprocess and needs continued review as dependencies and Git versions change.

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

An identified interrupted first clone may be moved through a journaled rename into **one reserved slot** per source: `quarantine/initial-clone/`. Quarantined bytes remain preserved and count toward both repository and global size budgets. Startup reconciliation handles existing local data. A later eligible scheduled, manual, or automatic retry can start a clone.

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

Recovery checks interrupted or suspect mirrors locally with `git fsck --full`. Unreachable/dangling Git objects alone are not corruption. For confirmed damaged mirrors, repair attempts to refetch **existing ref-tip object IDs** without destination refspecs or implicit ref mappings, then checks integrity again. This is intentionally not a general-purpose Git repair system.

At most three genuine failed repair attempts are counted per incident; a successful repair resets the incident counter. Completed repair operation IDs make success/error counters idempotent after crashes. SIGTERM/SIGINT, interruption and capacity pauses do **not** consume a repair-failure attempt. A needed repair blocked by storage is `repair_paused` with `repair_deferred`, then reevaluated during a later eligible backup run without a service restart. Exhausted genuine attempts stay `repair_failed`, even if space is also low.

Remote repair downloads require an included repository, enabled automatic repair, adequate capacity, and permitted Git access. GitHub and GitLab repairs additionally require a fresh, complete selection for their discovery scope in the current backup run. Boot reconciliation for discovered sources is local. Needed remote repairs wait with `DISCOVERY_REQUIRED`. **Excluded repositories receive no remote repair download**. If objects were deleted upstream or packs are irrecoverable, `repair_failed` can require manual operator action. The service keeps the old refs; it does not automatically replace the mirror.

### Periodic local verification

With `features.integrity_check: true`, RepoHarbor inspects existing mirrors sequentially from its on-disk catalog. It shares the service run lock with backups and does not run alongside their recovery or repair work. Startup recovery precedes scheduled work. The backup-root lock also excludes another service or an offline restore.

Periodic Git probes run without provider credentials, network transports, lazy fetching, or optional Git locks. The Git environment isolates global and system configuration, credential helpers, hooks, and filesystem-monitor integration. Checks read the bare repository structure, verify pack/index pairs, and run `git fsck --full --no-dangling` without changing refs or objects. They refuse shallow or externally linked stores, promisor packs, and configurations that can redirect lookup or override the check. The backup root remains a trusted storage boundary.

Each Git probe has a time limit. Output is streamed in bounded chunks. RepoHarbor retains a bounded amount of configuration or structure output and stores only classified findings and error codes. Missing or unsupported mirrors, permission or other filesystem failures, and timeouts are `uncheckable`. Confirmed object, pack or structure damage is `corrupt`. Dangling objects alone do not indicate damage.

Periodic checks do not initiate repair or discovery. A new confirmed damage finding can block the mirror and make it eligible for the existing bounded repair flow during a later authorized backup. Selection, upstream authorization, capacity, and fresh provider discovery still govern that flow.

### Return to healthy status after offline repair

Stop RepoHarbor and preserve the existing data before [repairing a mirror offline](../deploy/README.md#verify-an-offline-mirror-repair). A subsequent successful full check can set the current integrity status to `healthy`, including for an excluded mirror. It clears a resolved incident's `repair_pending`, resets its active attempt count, and clears obsolete repair-only blockers. A former `repair_failed` or `repair_paused` backup status can become `pending` or retain a recorded capacity restriction. Exclusion still applies.

Open journal operations, unfinished repairs, partial clones, pending quarantine, manual-attention markers and unrelated safety conditions retain their independent meaning. A clean Git finding does not reconcile an operation or authorize a backup. The check retains the damage timestamp, total and recent error history, and previous repair results. It clears a consecutive error count only when the retained events show that the entire streak was repair-related. Backup timestamps, successful update counters, and the count of successful automatic repairs are unaffected.

## Signals and failure handling

SIGTERM/SIGINT stop new Git work, terminate the active Git process group, and escalate to SIGKILL after five seconds if needed. The process reaps its children; a forced SIGKILL cannot execute cleanup, so the next startup must reconcile the durable journal. A surviving Git process retains the backup-root lock until it exits. Fatal persistence errors stop the worker/service and preserve the last durable state.

Periodic runs record their start and progress in `data.json` and store per-mirror findings in `metadaten.json` through atomic file updates. A stop during an unfinished check retains its previous completed finding and timestamp. A completed repository finding can survive a crash before the run counters are written. After a forced stop, startup marks an unfinished periodic run as `interrupted`. The next check starts at a future scheduled tick.

Stored error history preserves total and consecutive counters plus at most the last 20 events, without raw remote stderr or token values. Storage measurements are point-in-time observations, not enforced filesystem quotas. A physical free-space reserve reduces risk but cannot guarantee protection from concurrent writers or sudden disk exhaustion.

## Automatic retry safety

A retry uses the existing backup engine, execution lock, storage measurements, and durable operation journal. The classifier consumes bounded Git diagnostics and emits fixed codes. Raw stderr is kept out of logs and repository metadata. RepoHarbor suppresses Git's internal HTTP 429 retry loop for classified backup commands. Authorization, TLS verification, credential isolation, and provider API pacing continue to apply. An active provider cooldown prevents retry Git traffic on that authority. Failed or incomplete discovery also prevents retry Git traffic for provider repositories and unproven manual sources. Established manual mirrors can use their configured identity and successful backup history to continue after a discovery failure, subject to a fresh offline integrity check and the existing selection and identity checks.

An interrupted fetch must pass local recovery before another backup can mutate the mirror. If integrity is uncertain, the journal is preserved and backup mutations wait for reconciliation. Confirmed corruption remains subject to the existing bounded repair policy. Automatic retries do not launch a remote repair or reset repair attempts. An identified partial clone can use the existing journaled quarantine slot. Unowned partials, Git lock files, occupied quarantine slots with a new partial, and manual-attention markers block retry execution.

RepoHarbor writes the retry claim before Git starts. Startup recovery applies proven completions once and preserves incomplete data. An attempt interrupted by SIGTERM or left without proof of completion by SIGKILL consumes its claim and is marked interrupted. A pending retry with a durable temporary failure can still run after startup under the current eligibility rules. Discovery deferrals are persisted separately from Git claims. Each repository has a bounded deferral budget, and the service reapplies eligibility before each new check.

## Offline restore safety

Stop the backup service before running `restore clone` or `restore bundle`. The assistant needs the existing regular `.repoharbor.lock`, valid `data.json` and repository metadata, and a complete bare mirror. It takes the same exclusive, non-blocking backup-root lock as the service, using a read-only file descriptor. A running service, another restore, or a surviving Git child holding that lock causes the command to exit with code `6`. Leave the lock file in place and wait for the holder to exit.

Both commands read the original backup without network access, credentials, discovery, synchronization, recovery, or repair. An unfinished operation for the selected mirror, required metadata that cannot be validated, or a corrupt Git object causes the command to stop. RepoHarbor validates the mirror's structure and checks its objects with `git fsck --full`. It refuses shallow or externally linked stores and Git lock files. Bundle verification checks the export against the mirror's refs, imports it into an empty Git repository, and checks the imported objects.

Use trusted backup and output filesystems. The assistant checks the target's parent directories for symlinks and rejects traversal, targets inside the backup root or configured secrets directory, and bind aliases of those directories. It stages output in a private `.repoharbor-restore-incomplete-*` directory under the destination parent and uses atomic no-replace rename to publish the verified result. The destination filesystem must support the required operations, including symlink creation and directory synchronization. An existing destination remains intact.

SIGTERM and SIGINT stop Git work and allow staging cleanup. A forced SIGKILL can leave a private staging directory. A surviving Git process retains the root lock until it exits. Treat the staging directory as incomplete and wait for all related processes to exit before removing it. You can retry with a new output path. ENOSPC and other write failures are reported as errors. For a late failure after the atomic rename, such as a directory synchronization error, a complete result may already be present. Check the final path and verify the result before retrying.

Use the [manual Git procedure](../deploy/README.md#recover-directly-with-git) if the RepoHarbor state cannot be validated but you have an independently verified, intact mirror. The operator must keep writers stopped and choose a safe separate destination for that procedure.

## Extended logging and log storage

Event fields contain approved identifiers, counts, outcomes, and internal error codes. This also applies at `DEBUG`. Provider diagnostics, Git output, exception text, credential names and values, secret-file contents, and repository contents stay out of extended events. Repository IDs, aliases, provider authorities, and operational timing may still be confidential. Limit Docker-log access and persistent-log access to authorized operators.

File logging requires a dedicated writable Linux mount disjoint from the backup root and secret directory. RepoHarbor rejects paths through symlinks and mount aliases of protected storage. It checks that active and archived logs are private, service-owned regular files with mode `0600`, and rejects hard links and independently mounted log files. Use a log directory owned by the service UID with mode `0700`. Keep the host mount source and its parents under trusted administrative control. The directory lock allows one log writer at a time. Only the active file and its configured numbered archive namespace are managed.

Invalid file setup causes a safe startup diagnosis. If the file sink fails during operation, RepoHarbor emits one `logging.file_failed` event on stderr and continues with console output until restart. State remains in `data.json`, `metadaten.json`, and the operations journal. Logs provide diagnostic context and carry no durability guarantee. Use the journal and validated mirrors to establish the outcome of interrupted Git work. [Deployment and troubleshooting](../deploy/README.md#configure-log-retention)

## Scope and operational acceptance

A mirror does **not** provide historical snapshots. Deleted remote refs and force-pushes propagate to the mirror. Git LFS data, separate submodule repositories, issues, pull/merge requests, hosting metadata, and never-pushed local objects are not backed up by this version. Notifications are outside its scope.

Follow the [deployment guide](../deploy/README.md) for host-mount validation, non-root permissions, credential setup, updates, and an offline restore. See [configuration](CONFIGURATION.md) for status semantics and [testing](TESTING.md) for automated checks. Actual NAS/Portainer deployment and disaster-recovery acceptance remain separate operator tasks.
