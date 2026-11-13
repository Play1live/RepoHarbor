# Changelog

## 0.1.0 — Development build (not released)

- Added GitHub.com REST discovery alongside manual HTTPS mirrors: multiple named tokens, stable repository IDs, deduplication, owned/organization/collaborator scopes, explicit selection/exclusion and visibility/fork/archive filters. Renames and transfers preserve the same ID directory; incomplete discovery pauses all GitHub work and manual identity conflicts block automatic adoption.
- Preserved durable JSON journaling, single-writer locking, sequential cron scheduling without backfill, storage budgets, bounded non-destructive recovery and incomplete-clone quarantine. GitHub remote repair requires a fresh complete selection; existing manual paths, metadata and journals remain compatible.
- Expanded Python 3.12/3.13 integration, fault-injection, coverage and security checks, plus Docker smoke testing without init. Updated configuration, security, deployment and restore guidance, including NAS-compatible init omission and Vixie cron wildcard/step semantics. GitLab, Enterprise/SSH, the dashboard, historical snapshots, LFS payloads and hosting metadata remain outside this development build.
