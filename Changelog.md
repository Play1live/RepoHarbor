# Changelog

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
