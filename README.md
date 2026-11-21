<div align="center">

# ⚓ RepoHarbor

**A self-hosted Git mirror backup service for Linux, Docker, and NAS storage.**

![Python](https://img.shields.io/badge/Python-3.12%2B-3776AB?logo=python&logoColor=white)
![Git](https://img.shields.io/badge/Git-Mirror-F05032?logo=git&logoColor=white)
![Docker](https://img.shields.io/badge/Docker-Compose-2496ED?logo=docker&logoColor=white)
![Version](https://img.shields.io/badge/Version-1.0.0-64748B)
[![CI](https://github.com/Play1live/RepoHarbor/actions/workflows/ci.yml/badge.svg)](https://github.com/Play1live/RepoHarbor/actions/workflows/ci.yml)

</div>

RepoHarbor discovers repositories on GitHub.com, GitLab.com, and compatible Self-Managed GitLab instances and backs them up alongside manual HTTPS Git sources as local bare mirrors. It runs unattended in a non-root Docker container, with scheduled synchronization, persistent state, storage limits, and crash recovery.

## Features

- **Provider discovery:** Configure multiple named tokens and provider entries. Discover GitHub organization and collaborator repositories and GitLab personal, group, subgroup, and shared projects. Select by ID or address and filter by visibility, fork status, and archive status.
- **Git mirror backups:** Complete bare mirrors with branch and tag synchronization, pruning, and offline restoration using Git.
- **Offline restore and bundle export:** Opt in to restore a local mirror as a working repository or export a verified Git bundle.
- **Periodic integrity checks:** Opt in to local, read-only verification of existing mirrors on a separate schedule. Results appear in the status CLI.
- **Automatic retries.** Opt in to retry individual repositories after identified temporary Git failures, with persistent exponential backoff.
- **Extended logging and rotation:** Opt in to structured text or JSON events and optional rotating files on a dedicated log mount. The Compose example limits Docker logs independently.
- **Flexible scheduling:** Five-field cron expressions, IANA timezones, daylight-saving handling, and no missed-run backfill or overlapping runs.
- **Status CLI and manual backups:** Inspect service and repository state in text or JSON. Opt in to full or single-repository backups through the running service.
- **Storage controls:** Soft global and per-repository limits, per-repository overrides, and a minimum free-space reserve.
- **Safe recovery:** Durable operation journals, integrity checks after interruptions, bounded non-destructive repair, and preservation of incomplete clones for inspection.
- **Credential isolation:** Named credentials from either environment variables or protected files, without embedding tokens in remote URLs.
- **Optional HTTPS dashboard.** View real backup, repository, activity, storage, discovery, and configuration state. An administrator can also trigger backups when manual execution is enabled.
- **Headless Docker defaults.** Non-root execution with the dashboard disabled and no published application ports.

## Getting started

Build the image locally from the repository root and verify the CLI:

```sh
docker build -t repoharbor:local-test .
docker run --rm repoharbor:local-test --version
docker run --rm \
  -e REPOHARBOR_TOKEN_SOURCE=env \
  -e 'REPOHARBOR_CONFIG=schema_version: 1' \
  repoharbor:local-test config validate
```

The image tag `repoharbor:local-test` is local and mutable. To run the service, prepare a dedicated persistent backup mount, configure your sources and credentials, and follow the [deployment guide](deploy/README.md). The [Compose example](deploy/compose.example.yaml) contains fictional paths and a fictional Git server.

### Inspect and run backups

For a running Compose service, use the CLI inside the container.

```sh
docker compose -f deploy/compose.example.yaml exec -T repoharbor repoharbor status --json
docker compose -f deploy/compose.example.yaml exec -T repoharbor repoharbor repositories list
```

Read-only commands work with the default `features.manual_run: false`. To enable manual backups, set the flag to `true` in `REPOHARBOR_CONFIG` and redeploy. Use `repoharbor backup run --all` or `repoharbor backup run --repository example-demo` inside the running container. The [deployment guide](deploy/README.md#inspect-status-and-run-manual-backups) covers Docker and Portainer commands. The [configuration reference](docs/CONFIGURATION.md#cli-commands-and-exit-codes) lists selectors, status fields, and exit codes.

### Web dashboard

Enable `features.web_ui` to use the browser dashboard. It requires an HTTPS origin, TLS certificate and private key, and a generated administrator password mounted as a private file. Settings are read-only. With `features.manual_run` enabled, administrators can start backups, select **Check integrity** on a repository's detail page, or select **Check all repositories** on the list. Manual checks inspect local mirrors and are available even when periodic checks are disabled. See the [deployment guide](deploy/README.md#enable-the-web-dashboard) and [security model](docs/SECURITY.md#web-dashboard).

### Automatic first-time setup

**No initialization mode or one-time environment flag is required.** On startup, RepoHarbor checks its persistent backup directory:

- On a new, empty backup root, it automatically creates its state files and repository metadata as needed.
- On subsequent starts, it loads existing state and performs recovery checks when necessary.
- It then waits for future backup and enabled integrity schedules. When automatic retries are enabled, it can also resume pending repository retries after startup recovery.

The backup root must already exist as a writable, persistent mount. If the state journal is missing from a non-empty backup root, RepoHarbor stops rather than overwriting data. Docker init is intentionally omitted from the Compose file for NAS compatibility. The application handles signals and Git child processes itself; no RepoHarbor initialization switch is needed.

## Backup and recovery

| Source | Backup directory |
|---|---|
| Manual HTTPS | `/backups/manual/<alias>/` |
| GitHub.com | `/backups/github/github.com/<repository-id>/` |
| GitLab.com | `/backups/gitlab/gitlab.com/<project-id>/` |
| Self-Managed GitLab | `/backups/gitlab/<host>/<project-id>/`, with an explicit port encoded as `<host>~<port>` |

Each source stores its bare mirror in `repository.git/` and its metadata in `metadaten.json`. Discovery deduplicates overlapping token results by provider, authority, and numeric ID. Renames and transfers within an authority retain the ID path. The central `/backups/data.json` tracks operations, discovery, and scheduler state, including periodic integrity runs.

### Enable automatic retries

Set `features.auto_retry: true` in `REPOHARBOR_CONFIG`. By default, a temporary Git failure allows up to three additional attempts for the affected repository, after successive waits of 5, 10, and 20 minutes. An earlier successful backup cancels the pending retry. Inspect the global pending count with `repoharbor status` and the repository schedule and outcome with `repoharbor repositories show <repository>`. See the [configuration reference](docs/CONFIGURATION.md#automatic-backup-retries).

### Schedule periodic integrity checks

Set `features.integrity_check: true` in `REPOHARBOR_CONFIG`. The default schedule is Sundays at 04:00 in `TZ`. Checks run one mirror at a time, cover excluded mirrors by default, and use local Git data without fetching. They share the execution lock with backups. See the [configuration reference](docs/CONFIGURATION.md#periodic-integrity-checks) for options and the [deployment guide](deploy/README.md#enable-periodic-integrity-checks) for status and recovery procedures.

### Enable extended logging

Set `features.extended_logging: true` for events covering backups, discovery, retries, integrity, recovery, and offline restore. The console defaults to text at `INFO`. Set `logging.format: json` for one event per line. File output is a separate opt-in and uses a dedicated persistent bind mount. Existing v0.9.2 deployments can update in place with their current backup and secret mounts and schema-1 state. [Logging configuration](docs/CONFIGURATION.md#extended-logging) · [Docker and NAS setup](deploy/README.md#configure-log-retention)

### Restore or export a mirror

Stop the backup service and run the CLI with `features.restore_helper: true`. The assistant reads the local mirror and catalog without network access or provider credentials.

```sh
repoharbor restore clone --repository example-demo --destination /exports/restored-demo
repoharbor restore bundle --repository example-demo --output /exports/example-demo.bundle
```

The clone includes Git history, branches, tags, and available refs. Add `--branch development` to check out that existing branch. A bundle is a portable Git file that you can clone offline. Repositories that were excluded, removed from the backup configuration, or are unreachable upstream can still be restored from a valid local backup. See [offline restoration](deploy/README.md#8-offline-restoration-and-acceptance) for the read-only Docker workflow, bundle import, and manual Git recovery.

A full backup reports missing included repositories separately. An unchanged mirror is verified before it can report success. See the [upgrade guidance](deploy/README.md#upgrade-to-v100).

**Mirrors are not historical snapshots.** Synchronization reflects deleted refs and force-pushes; Restore and bundle export contain the Git data retained by the mirror. Git LFS payloads, separate submodule repositories, issues, pull/merge requests, other hosting metadata, and unpushed local changes require separate backups. RepoHarbor has no notification feature. Excluded repositories remain stored locally. Interrupted or damaged backups may require manual intervention; existing mirrors are never silently replaced with fresh clones.

Discovery reevaluates selection during scheduled and manual runs. The default is `selected` with an empty selection. Incomplete discovery pauses GitHub or the affected GitLab authority for the run. Independent authorities and eligible manual sources can continue. An established manual mirror on the affected authority can still be eligible. A first clone on that authority waits for successful discovery. Existing mirrors are retained, and manual identity conflicts block automatic assignment. See the [combined configuration example](docs/CONFIGURATION.md#combined-github-and-gitlab-example).

## Documentation

| Topic | Guide |
|---|---|
| Docker Compose, Portainer, NAS setup, updates, and restore | [Deployment](deploy/README.md) |
| GitHub and GitLab discovery, selection, HTTPS sources, credentials, cron, and limits | [Configuration](docs/CONFIGURATION.md) |
| Data integrity, recovery, quarantine, and security | [Security](docs/SECURITY.md) |
| Development setup, test suites, coverage, and CI | [Testing](docs/TESTING.md) |
| Development history | [Changelog](Changelog.md) |

## Development and deployment notes

RepoHarbor v1.0.0 is the first stable release and follows the successful NAS/Portainer acceptance of v0.9.2. RepoHarbor follows Semantic Versioning. PATCH releases provide corrections, MINOR releases add compatible features, and MAJOR releases can introduce incompatible changes. Configuration and persistence continue to use `schema_version: 1`. The changelog records version history. GitHub Releases and tags show publication history.

Local development uses Python 3.12+, Git, and OpenSSL for HTTPS integration tests. CI tests Python 3.12 and 3.13 and includes fault injection, Chromium, CodeQL, image scanning, and Docker smoke tests. See the [testing guide](docs/TESTING.md) for commands and coverage requirements.

Before relying on a backup, verify the host storage mount, permissions, a completed scheduled run, and an **offline restore** on the target system. Automated CI results do not replace NAS/Portainer acceptance testing.
