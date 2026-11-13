<div align="center">

# ⚓ RepoHarbor

**A self-hosted Git mirror backup service for Linux, Docker, and NAS storage.**

![Python](https://img.shields.io/badge/Python-3.12%2B-3776AB?logo=python&logoColor=white)
![Git](https://img.shields.io/badge/Git-Mirror-F05032?logo=git&logoColor=white)
![Docker](https://img.shields.io/badge/Docker-Compose-2496ED?logo=docker&logoColor=white)
![Version](https://img.shields.io/badge/Development-0.1.0-64748B)
[![CI](https://github.com/Play1live/RepoHarbor/actions/workflows/ci.yml/badge.svg)](https://github.com/Play1live/RepoHarbor/actions/workflows/ci.yml)

</div>

RepoHarbor discovers GitHub repositories and backs them up alongside explicitly configured HTTPS Git repositories as local bare mirrors. It runs unattended in a non-root Docker container, with scheduled synchronization, persistent state, storage limits, and crash recovery.

**Status:** `0.1.0` is an unreleased development build, not a published image or a production-validated NAS release. GitHub.com REST discovery and manual HTTPS sources are supported. GitLab, GitHub Enterprise, SSH, and the web dashboard are not yet implemented.

## Features

- **GitHub discovery:** Multiple named tokens, stable repository IDs, organization/collaborator scopes, visibility/fork/archive filters, and explicit selection or exclusions.
- **Git mirror backups:** Complete bare mirrors with branch and tag synchronization, pruning, and offline restoration using Git.
- **Flexible scheduling:** Five-field cron expressions, IANA timezones, daylight-saving handling, and no missed-run backfill or overlapping runs.
- **Storage controls:** Soft global and per-repository limits, per-repository overrides, and a minimum free-space reserve.
- **Safe recovery:** Durable operation journals, integrity checks after interruptions, bounded non-destructive repair, and preservation of incomplete clones for inspection.
- **Credential isolation:** Named credentials from either environment variables or protected files, without embedding tokens in remote URLs.
- **Headless Docker service:** Non-root execution, no published application port, and no web server or healthcheck in this version.

## Getting started

Build the development image from the repository root and verify the CLI:

```sh
docker build -t repoharbor:local-test .
docker run --rm repoharbor:local-test --version
docker run --rm \
  -e REPOHARBOR_TOKEN_SOURCE=env \
  -e 'REPOHARBOR_CONFIG=schema_version: 1' \
  repoharbor:local-test config validate
```

The image tag `repoharbor:local-test` is local and mutable; it is **not** a published registry image. To run the service, prepare a dedicated persistent backup mount, configure your sources and credentials, and follow the [deployment guide](deploy/README.md). The [Compose example](deploy/compose.example.yaml) contains fictional paths and a fictional Git server.

### Automatic first-time setup

**No initialization mode or one-time environment flag is required.** On startup, RepoHarbor checks its persistent backup directory:

- On a new, empty backup root, it automatically creates its state files and repository metadata as needed.
- On subsequent starts, it loads existing state and performs recovery checks when necessary.
- It then waits for the next scheduled backup; starting the container does **not** immediately download repositories.

The backup root must already exist as a writable, persistent mount. If the state journal is missing from a non-empty backup root, RepoHarbor stops rather than overwriting data. Docker init is intentionally omitted from the Compose file for NAS compatibility. The application handles signals and Git child processes itself; no RepoHarbor initialization switch is needed.

## Backup and recovery

Manual repositories remain under `/backups/manual/<alias>/`; discovered repositories use `/backups/github/github.com/<repository-id>/`. Each contains a `repository.git/` bare mirror and a separate `metadaten.json`. GitHub renames and transfers retain the same ID path. The central `/backups/data.json` tracks operations and scheduler state. For example, a mirror can be restored without contacting its original host:

```sh
git clone --no-hardlinks /path/to/backups/manual/example-demo/repository.git ./restored-demo
```

**Mirrors are not historical snapshots.** Synchronization reflects deleted refs and force-pushes; RepoHarbor does not back up Git LFS payloads, separate submodule contents, issues, pull/merge requests, or unpushed local changes. Excluded repositories remain stored locally. Interrupted or damaged backups may require manual intervention; existing mirrors are never silently replaced with fresh clones.

GitHub discovery is reevaluated at future scheduled runs. Its safe default is `selected` with an empty selection. See the [configuration example](docs/CONFIGURATION.md#github-discovery-and-selection). A failed or incomplete discovery pauses the entire GitHub part of that run, while independent manual sources continue. Existing mirrors are retained. Collisions with manual mirrors block automatic assignment instead of adopting or duplicating data.

## Documentation

| Topic | Guide |
|---|---|
| Docker Compose, Portainer, NAS setup, updates, and restore | [Deployment](deploy/README.md) |
| GitHub discovery, selection, HTTPS sources, credentials, cron, and limits | [Configuration](docs/CONFIGURATION.md) |
| Data integrity, recovery, quarantine, and security | [Security](docs/SECURITY.md) |
| Development setup, test suites, coverage, and CI | [Testing](docs/TESTING.md) |
| Development history | [Changelog](Changelog.md) |

## Development and deployment notes

Local development uses Python 3.12+, Git, and OpenSSL for HTTPS integration tests. CI tests Python 3.12 and 3.13 and includes fault injection, security checks, and Docker smoke tests. See the [testing guide](docs/TESTING.md) for commands and coverage requirements.

Before relying on a backup, verify the host storage mount, permissions, a completed scheduled run, and an **offline restore** on the target system. Automated CI results do not replace NAS/Portainer acceptance testing.
