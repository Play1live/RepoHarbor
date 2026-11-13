# Security, data integrity, and recovery

This document covers RepoHarbor `0.2.0`. This document describes the implemented protections and their limits. Passing local or CI tests does not certify a production NAS/Portainer installation.

## Trusted storage boundary

The backup root must be a dedicated, persistent, trusted local filesystem with reliable `flock`, atomic rename, and `fsync` behavior. The container requires `/backups` to be an actual mount; the host operator must **independently confirm that the intended NAS device is mounted**. A Docker bind mount only verifies the container-side path. SMB/NFS consistency is not guaranteed for an active RepoHarbor backup root.

The service refuses symlinks and special files within the backup-root tree. Do not grant untrusted actors concurrent write access to that tree. The data directory includes the central journal, per-repository metadata, mirrors, temporary clones, quarantine, and any operator-preserved archives.

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

A failed or incomplete token discovery suspends the entire GitHub part of that run. No incomplete selection is applied and no stale selection permits remote repair. Independent manual sources remain eligible. Manual address resolution is conservative; unresolved or redirected GitHub addresses pause automatic discovery rather than risk a duplicate. Full rules and the limitation around unknown historical manual aliases are documented in [configuration](CONFIGURATION.md#stable-paths-and-manual-conflicts).

## Journaling and crash consistency

The single-writer lock prevents two active RepoHarbor instances from mutating the same root. The central `data.json` journal is persisted **before Git mutations**, using temporary-file writes, flush/`fsync`, atomic replacement, and parent-directory synchronization. Repository metadata is written before marking an operation complete. Scheduler overlap events and worker journal transitions use serialized, durable read-modify-write transactions.

Crash recovery reconciles interrupted operations, records proven completion only once, and avoids falsely advancing `last_checked_at` or `last_updated_at`. A successful `git fetch --atomic` protects reference updates, **not the entire object database as one filesystem transaction**. A crashed update may still require a Git integrity check and repair.

An existing valid mirror is **never automatically deleted or replaced by a fresh clone**. Removing a source from configuration preserves its local backup. A remote disappearance or authentication failure does not prove that the repository was deleted.

Discovery records its own state in the existing central `data.json` without replacing the active Git-operation slot. Repository metadata is atomically written, but the catalog is **not** a multi-file atomic filesystem transaction. Runnable GitHub sources are published in memory only after all metadata writes and the completion marker are durable. A crash during catalog publication may leave some metadata updated; startup permits no GitHub remote work from that partial catalog, and a fresh complete discovery must recompute selection. Missing metadata in a newly created directory fails closed and may require operator review; it is never silently overwritten or re-cloned. Existing Git journals and completion markers continue to govern backup recovery.

## Interrupted initial clones and quarantine

A temporary `repository.git.partial/` becomes a valid `repository.git/` **only** when completion is supported by a durable journal marker and a fresh integrity check. Structural health by itself does not establish that the initial network clone completed.

An identified interrupted first clone may be moved through a journaled rename into **one reserved slot** per source: `quarantine/initial-clone/`. Quarantined bytes remain preserved and count toward both repository and global size budgets. Startup reconciliation does **not** start a new clone; retry is permitted no earlier than the next normal cron tick.

Unknown or unowned partials, excluded sources, a second partial while quarantine is occupied, missing quarantine content, and conflicting paths set persistent `backup.manual_attention_required` and prevent further automatic downloads. The service does not rotate or delete quarantines automatically. Missing/corrupt journal or metadata files fail closed rather than being silently reset. Only schema version `1` is accepted. GitHub metadata and the `data.json.discovery` marker are additive extensions; existing manual state is not migrated.

### Operator procedure for `manual_attention_required`

1. **Stop RepoHarbor** before modifying its backup root. Inspect `data.json`, the repository's `metadaten.json`, the partial, and the quarantine without destroying evidence.
2. Preserve any existing valid `repository.git/`. Do not replace it with a partial, delete the journal, or edit successful completion timestamps to bypass recovery.
3. If the partial or occupied quarantine must be moved, archive it **without overwriting** existing files to a unique, operator-chosen path under `<backup-root>/operator-archive/`, outside `manual/`.
4. Keep archives until their content has been reviewed; they still count against the global backup-root budget. Local `git fsck --full` and a separate offline clone from an existing mirror can help with diagnosis, but do not prove an interrupted initial clone completed.
5. If a recorded **pending** quarantine is missing, restore the exact preserved content to that slot before restarting; otherwise leave recovery blocked and investigate manually.
6. Restart the service only after resolving the conflict. Reconciliation clears an obsolete attention marker when safe; the next **future scheduled** run can then attempt backup again if capacity permits.

Do not use broad `rm -rf`, permission resets, or lock-file deletion as a troubleshooting shortcut. File existence does not mean that an OS lock remains held after the process exits.

## Integrity checks and bounded repair

Interrupted or suspect mirrors receive an on-demand local `git fsck --full` integrity check. Unreachable/dangling Git objects alone are not corruption. For confirmed damaged mirrors, repair attempts to refetch **existing ref-tip object IDs** without destination refspecs or implicit ref mappings, then checks integrity again. This is intentionally not a general-purpose Git repair system.

At most three genuine failed repair attempts are counted per incident; a successful repair resets the incident counter. Completed repair operation IDs make success/error counters idempotent after crashes. SIGTERM/SIGINT, interruption and capacity pauses do **not** consume a repair-failure attempt. A needed repair blocked by storage is `repair_paused` with `repair_deferred`, then reevaluated at the next scheduled run without a service restart. Exhausted genuine attempts stay `repair_failed`, even if space is also low.

Remote repair downloads require an included repository, enabled automatic repair, adequate capacity, and permitted Git access. GitHub repair additionally requires a fresh, complete selection in the current scheduled run. Boot reconciliation is local for GitHub sources, and needed remote repairs wait with `DISCOVERY_REQUIRED`. **Excluded repositories receive no remote repair download**. If objects were deleted upstream or packs are irrecoverable, `repair_failed` can require manual operator action. The service keeps the old refs; it does not automatically replace the mirror.

## Signals and failure handling

SIGTERM/SIGINT stop new Git work, terminate the active Git process group, and escalate to SIGKILL after five seconds if needed. The process reaps its children; a forced SIGKILL cannot execute cleanup, so the next startup must reconcile the durable journal. Fatal persistence errors stop the worker/service, preserving recoverable state rather than proceeding with an unjournaled mutation.

Stored error history preserves total and consecutive counters plus at most the last 20 events, without raw remote stderr or token values. Storage measurements are point-in-time observations, not enforced filesystem quotas. A physical free-space reserve reduces risk but cannot guarantee protection from concurrent writers or sudden disk exhaustion.

## Scope and operational acceptance

A mirror does **not** provide historical snapshots. Deleted remote refs and force-pushes propagate to the mirror. Git LFS data, separate submodule repositories, issues, pull/merge requests, hosting metadata, and never-pushed local objects are not backed up by this version.

Follow the [deployment guide](../deploy/README.md) for host-mount validation, non-root permissions, credential setup, updates, and an offline restore. See [configuration](CONFIGURATION.md) for status semantics and [testing](TESTING.md) for automated checks. Actual NAS/Portainer deployment and disaster-recovery acceptance remain separate operator tasks.
