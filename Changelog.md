# Changelog

## 0.9.0

- Add an opt-in, administrator-authenticated HTTPS dashboard for service status, repositories, recent activity, storage, sources, and read-only configuration.
- Route optional browser-triggered backups through the existing control socket, execution lock, storage limits, and recovery checks.
- Record repository measurement timestamps and measured free disk space without changing configuration or persistence schema 1. Existing metadata remains readable.
- Keep Docker deployments headless by default and document secure Compose, Portainer, and reverse-proxy operation.

## 0.8.0

- Add opt-in structured text and JSON Lines events for service lifecycle, backups, discovery, retries, integrity, recovery, quarantine, and offline restore.
- Add optional private log files with size-based rotation, bounded archives, and a console diagnosis if the file sink fails. The log directory uses a dedicated persistent mount.
- Limit Docker container logs independently in the Compose example. Configuration and persistence remain at schema 1, with existing v0.7.0 state retained.

## 0.7.0

- Add opt-in automatic retries for individual GitHub, GitLab, and manual HTTPS backups after classified temporary Git failures. The default backoff allows three additional attempts after successive waits of 5, 10, and 20 minutes.
- Persist retry plans and outcomes in schema-1 repository metadata and report attempts, deadlines, blocks, and the global pending count in text and JSON status.
- Share the execution lock with scheduled backups, manual backups, and integrity checks. Reconcile prior mutations before retrying and apply discovery, cooldowns, capacity limits, quarantine, and repair safeguards.
- Allow eligible established manual mirrors to retry during discovery failures and defer retries that require discovery after classified temporary API failures. Persist the bounded deferral budget separately from Git attempts.

## 0.6.0

- Add opt-in periodic Git integrity checks with a separate cron schedule for existing mirrors, including excluded repositories by default. Local, read-only checks distinguish corruption from unavailable results and interruptions.
- Persist periodic findings and progress for the status CLI. Share the backup execution lock and record skipped overlaps without replaying missed runs.
- Route confirmed damage into the existing eligibility and recovery rules. A clean check after offline repair can restore healthy status and clear obsolete repair blocks while preserving independent safety restrictions and backup history.

## 0.5.0

- Added an opt-in offline restore assistant for local Git mirrors, including excluded, removed, and unreachable sources with valid backups.
- Added restoration to a separate working repository with optional branch selection and verified Git bundle export for portable offline recovery.
- Added exclusive backup-root locking, mirror and bundle integrity checks, and atomic output publication that preserves the original backup and existing destinations.

## 0.4.0

- Added global status and repository list/detail commands with read-only text and JSON output.
- Added opt-in manual backups for all eligible repositories or one selected repository.
- Added local, UID-verified Unix socket control with a shared run lock for scheduled and manual backups. Existing discovery, storage, journaling, and recovery rules apply.

## 0.3.0

- Added discovery for GitLab.com and compatible Self-Managed GitLab instances, including HTTPS authorities with non-default ports. Multiple personal access tokens and provider entries can cover personal, group, subgroup, and shared projects.
- Extended explicit repository selection, exclusions, and fork/archive/visibility filters to GitLab. GitLab's `internal` visibility is opt-in through the visibility list. The empty `selected` default remains safe.
- Added stable GitLab project identities scoped by authority. Overlapping token results are deduplicated, and renames and namespace transfers keep one mirror and its confirmed address history. Manual identity checks try the next same-authority token only after an API 404 and block ambiguous assignments.
- Added bounded GitLab keyset and offset pagination, strict validation of continuations and clone URLs, configurable request pacing, and persistent cooldowns that survive restarts and temporary provider removal.
- Isolated discovery failures and cooldowns by GitLab authority. GitHub keeps its existing discovery scope, and eligible manual sources keep running. GitLab publishes runnable selections only after complete instance discovery and durable metadata writes.
- Integrated GitLab with mirror backup, offline restore, crash recovery, and storage limits using schema 1. Missing GitLab default branches are recorded as unknown, and API visibility remains independent of Git read access.

## 0.2.0

- Added scheduled GitHub.com repository discovery with multiple classic/fine-grained tokens, ID-based deduplication, owned/organization/collaborator scopes, explicit selection/exclusion and visibility/fork/archive filters.
- Added stable ID paths and rename/transfer address history, conservative manual-source conflict handling, bounded REST pagination and persisted rate-limit cooldowns. Incomplete discovery pauses all GitHub updates and remote repairs while independent manual sources continue.
- Separated API observation from Git transport availability: discovery preserves existing source fields and Git errors, new repositories remain unknown until successful Git access, and API absence changes only the discovery status.

## 0.1.0

- Introduced manually configured HTTPS Git mirror backups, named file/environment credentials, scheduled synchronization without catch-up or overlapping runs, and soft global/per-repository storage budgets with a minimum free-space reserve.
- Added durable JSON journaling and single-writer locking, crash reconciliation, bounded non-destructive repair, and preservation of incomplete initial clones. Storage-blocked repairs are reported as `repair_paused`, separately from exhausted `repair_failed` attempts.
- Added an unprivileged, headless Docker service, a Compose example and deployment/restore documentation, plus Python 3.12/3.13 CI, integration, fault-injection, coverage, and security checks. Provider discovery, a dashboard, historical snapshots, Git LFS payloads, and hosting metadata are outside this development build.
