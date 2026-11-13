# Changelog

## 0.1.0 — Development build (not released)

- Introduced manually configured HTTPS Git mirror backups, named file/environment credentials, scheduled synchronization without catch-up or overlapping runs, and soft global/per-repository storage budgets with a minimum free-space reserve.
- Added durable JSON journaling and single-writer locking, crash reconciliation, bounded non-destructive repair, and preservation of incomplete initial clones. Storage-blocked repairs are reported as `repair_paused`, separately from exhausted `repair_failed` attempts.
- Added an unprivileged, headless Docker service, a Compose example and deployment/restore documentation, plus Python 3.12/3.13 CI, integration, fault-injection, coverage, and security checks. Provider discovery, a dashboard, historical snapshots, Git LFS payloads, and hosting metadata are outside this development build.
