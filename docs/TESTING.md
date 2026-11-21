# Testing, quality gates, and CI

RepoHarbor `0.9.2` is tested with mocked GitHub and GitLab API responses, isolated local Git repositories, authenticated loopback HTTPS servers, crash and recovery tests, and a Linux Docker image. Production NAS/Portainer acceptance requires separate verification on the target system.

## Local prerequisites and commands

Install **Python 3.12+**, **Git**, and the **OpenSSL CLI** (for the synthetic HTTPS integration tests). From the repository root:

```sh
python -m venv .venv
.venv/bin/pip install --require-hashes -r requirements-dev.lock
.venv/bin/pip install --no-build-isolation --no-deps -e .
.venv/bin/ruff check .
.venv/bin/ruff format --check .
.venv/bin/mypy
.venv/bin/pytest --cov --cov-report=xml --cov-report=json --cov-report=term-missing --junitxml=test-results/junit.xml
.venv/bin/python scripts/check_coverage.py coverage.json
.venv/bin/bandit -r src -ll
.venv/bin/pip-audit --local --skip-editable
.venv/bin/pytest tests/fault_injection
```

The version is defined in `pyproject.toml`; the CLI reports the installed package version. `requirements-dev.in` lists direct development requirements and `requirements-dev.txt` pins the resolved development dependencies. The coverage configuration includes the application modules and entry points; do not remove failing code from measurement to meet a threshold.

### Required coverage thresholds

| Metric | Minimum |
|---|---:|
| Total line coverage | **90%** |
| Total branch coverage | **85%** |
| Line coverage for each dashboard module and retry | **90%** |
| Line coverage for **each** critical module (`catalog`, `control`, `credentials`, `engine`, `events`, `git_backup`, `gitlab`, `integrity`, `log_files`, `mounts`, `persistence`, `recovery`, `restore`, `selection`, `storage`) | **95%** |

`scripts/check_coverage.py` enforces these independently using `coverage.json`, including a check that branch-coverage data is present. A green pytest result alone does not replace the separate coverage gate.

## What the suite tests

| Test area | Covered scenarios |
|---|---|
| GitHub discovery | Multiple overlapping/restricted token lists, configured scopes, all selection modes, filter precedence, fork/archive handling, pagination and rate limits, malformed responses, token/network failures, and manual identity conflicts. |
| GitLab configuration and authentication | Provider fields and scope defaults, DNS/IPv4 authorities and ports, credential binding, Private-Token headers, human account requirements, and refusal to send an API request before instance binding. |
| GitLab discovery and selection | Personal, member-group, subgroup and shared projects, keyset/offset pagination, missing/inconsistent page evidence, invalid API fields, missing or null default branches, fork/archive/internal visibility filters, explicit selection and exclusion precedence. |
| GitLab identity and mirrors | Overlapping tokens deduplicate by project ID, renames retain history and mirror paths, ports stay distinct, manual collisions with 404-only API fallback, and local clone, update, and offline restore. Simulated Git read denial leaves the API observation available and preserves an existing mirror. |
| GitLab pacing and recovery | Request pacing and shutdown, quota headers and durable cooldowns, cooldown across temporary provider removal, isolation of token failures by authority alongside GitHub/manual sources, interrupted metadata publication, SIGKILL while reading a throttled response or publishing metadata, and injected persistence errors. |
| Git integration | First mirror clone, no-op checks, default-branch changes, annotated tags, branch deletion/pruning, force-pushes, remote outages, and offline restore. |
| Automatic backup retries | Backoff, limits, repository and authority isolation, established manual mirrors during discovery failures, bounded discovery deferrals, cooldowns, manual CLI return values, shared scheduling, storage and recovery gates, state validation, and text and JSON status. An HTTPS fixture distinguishes a temporary server failure from authentication, permission, missing repository, and rate-limit responses. |
| Retry fault injection | Restart with pending state, an injected metadata write error, SIGTERM and SIGKILL during a retry, and crashes after planning, claiming, entering the operation journal, completing Git, promoting data, or publishing metadata. Tests verify quarantine preservation, journal replay and success counters, including SIGTERM and SIGKILL on both sides of a discovery deferral write. |
| Periodic integrity configuration and scheduler | Boolean opt-in, independence from manual backups and restore, cron validation, timezone and DST behavior, skipped ticks and no backfill, shared exclusivity with backup runs, and durable overlap counts. |
| Periodic Git integrity and selection | Full local checks of manual, GitHub and GitLab mirrors, confirmed corrupt objects, refs and packs, orphan pack/index pairs, empty mirrors, dangling objects, excluded and removed sources, and checks without provider access. |
| Periodic integrity recovery and status | SIGTERM, SIGKILL, timeouts, filesystem errors and restart with partial progress. Offline object restoration followed by a healthy recheck, preservation of repair history and backup metrics, intact independent safeguards, and compatible text and JSON views. |
| Dashboard manual integrity | Authenticated single and bulk checks with the periodic schedule disabled, local healthy and corrupt mirrors, excluded or removed sources, partial and unsupported mirrors, no remote Git or repair, shared-lock overlaps, outcomes after restart, and rejection of invalid IDs, sessions, CSRF tokens, hosts, and origins. |
| Extended logging | Strict opt-in configuration, level filtering, text and JSON Lines, UTC timestamps and monotonic durations, run and operation IDs, event coverage, and fictional secret exclusion at `DEBUG`. |
| Secure log rotation | Bounded and ordered archives, byte limits, append on restart, mount aliases and symlinks, hard links, file ownership and permissions, concurrent emission, and injected short writes, ENOSPC and EIO. File errors preserve successful Git and journal outcomes. |
| Restore configuration and paths | Boolean flags and their independence, protected backup and secrets paths, symbolic links, traversal, and bind-mount aliases. |
| Offline restore and bundle integration | Real Git mirrors for manual, GitHub, GitLab.com, and Self-Managed GitLab IDs. Tests cover excluded and unavailable sources, alternate branch selection, missing default branches, empty mirrors, corrupt objects, and reference preservation. They verify standalone bundles through offline clone and reject truncated or incomplete exports. |
| Restore locking and fault injection | Active backup lock, competing restore, SIGTERM and SIGKILL during staged Git operations, inherited locks, ENOSPC/EIO, and destination races. Tests check safe retry, preservation of the original backup, and the absence of prematurely published output. |
| HTTPS and credentials | Real local Git smart-HTTP over loopback TLS, a temporary trusted certificate, `GIT_ASKPASS` challenges, both `env` and `file` modes, redaction and token rotation behavior. |
| Git/API status separation | Git failures survive API rediscovery/restart; new repositories remain unknown; exclusions, visibility filters and global/repository limits cannot confirm Git success; completed Git fetch restores availability; source extensions and timestamps survive discovery. |
| GitHub persistence | Rename/transfer retains one mirror and counters; exclusion/unavailability preserves data; partial discovery suspends the whole provider; no startup remote repair; publication I/O failures and SIGKILL before/after metadata/Git/journal writes. |
| Recovery and fault injection | SIGTERM/SIGKILL during clone/fetch/repair, journal checkpoints, metadata writes, interrupted/owned/unowned partials, quarantine replay, bounded repair, and idempotency. |
| Storage and scheduler | Global/per-repository budgets, free-space reserve, capacity pause/resume, five-second transfer monitoring, DST behavior, missed ticks, and durable overlap counts. |
| State and security | Corrupt/missing metadata, OS locking, unknown configuration fields, URL validation, secret handling, and error-code redaction. |
| Status and control | Read-only text/JSON inspection, problem counts, invalid metadata, exit codes, token redaction, Unix peer UID checks, and no queued backup requests. |
| Manual backups | Full and targeted runs with real Git, discovery failure and conflict handling, overlap with cron, client disconnects, and SIGTERM/SIGKILL during Git operations. |
| HTTPS dashboard | Sign-in with the existing secret file format, random scrypt salt on restart, unavailable KDF, malformed credentials, per-peer and concurrent sign-in limits, forwarding-header isolation, session expiry, host/origin checks, CSRF, protected status, rejection of traversal and development routes, empty or unreadable state, measured zero values, and shared backup locking. |
| Docker | CLI/version/config validation, required bind mount, non-root execution, headless defaults, opt-in HTTPS startup and shutdown, manual backups through `docker exec`, recreation with retained data, and offline scheduled integrity checks with retained findings. |

Tests use fictional API payloads and credentials, local repositories, and a synthetic loopback TLS service. GitLab tests mock the provider API and exercise local Git mirrors. The suite does not contact a production GitLab instance or establish permissions, API behavior, or connectivity for a deployed instance. An isolated test Git transport may enable local file access. Production backup sources use HTTPS. Offline restore and bundle export use local Git data.

## Docker and Compose smoke tests

Install Docker with Compose v2, Git, and the OpenSSL CLI and use a Linux/amd64 Docker endpoint. From the repository root:

```sh
docker compose -f deploy/compose.example.yaml config --quiet
docker build -t repoharbor:local-test .
python3 tests/docker/smoke.py
```

The Docker smoke test verifies the version and config CLI, the bind mount, the unprivileged user, and the absence of TCP listeners before starting the test Git fixture. It inspects status with `docker exec`, checks that a disabled manual request returns exit `4`, and exercises full and targeted runs against a local HTTPS Git fixture with manual execution enabled. It also verifies the root lock, graceful SIGTERM with and without Docker init, and state retention on container recreation. The restore smoke tests use separate `--network none` containers with a read-only root filesystem, a read-only backup bind mount, and a writable export bind mount. They verify that the running service blocks a restore, then check a restored working tree and a bundle imported in another container with only the export mount. The automatic retry smoke test returns HTTP 503 from the fixture, verifies the original CLI failure, and waits for a successful no-change retry with its existing update count. The periodic integrity smoke test also verifies that a disabled schedule stays idle, an enabled schedule distinguishes intact and damaged mirrors with networking disabled, and a restart retains the findings. It checks that the scan leaves Git mirror contents and backup success metrics intact. The logging smoke test checks the required separate mount, Docker log-driver settings, file rotation, and retention across SIGTERM and SIGKILL. Production NAS filesystems, live provider APIs and Portainer need separate acceptance tests.

For an authorized TLS-intercepting build proxy, the Dockerfile supports an optional BuildKit CA secret:

```sh
docker build --secret id=build_ca,src=/path/to/trusted-ca.pem -t repoharbor:local-test .
```

The trusted CA is made available only during build-time package installation; do not disable TLS verification or add private CA keys to the repository.

The full smoke run also exercises the packaged HTTPS dashboard in a container with a fresh temporary backup filesystem, loopback TLS, and no external network. It confirms the sign-in flow, empty status, absence of the development entry point, asset access, and clean shutdown. To run only this check against an already built image, use:

```sh
python tests/docker/dashboard_smoke.py repoharbor:local-test
```

## GitHub Actions workflow

The [CI workflow](../.github/workflows/ci.yml) runs for pull requests targeting `main`, pushes to `main`, and manual dispatch. Checkout/setup actions use pinned revisions and the workflow has read-only repository permissions. It does not merge pull requests or publish Docker images.

These are the six **required check names** to select in branch protection:

| Check | Purpose |
|---|---|
| `lint-static` | Ruff lint/format verification and strict mypy. |
| `unit-and-integration (3.12)` | Python 3.12 tests and coverage gates. |
| `unit-and-integration (3.13)` | Python 3.13 tests and coverage gates. |
| `fault-injection` | Independent crash, signal, recovery, and journal fault tests. |
| `security` | Dependency audit, Bandit medium/high findings, and pinned Gitleaks secret scanning. |
| `docker-build-smoke` | Compose validation, image build, and Docker smoke test. |

The workflow uploads test/coverage artifacts for review. They are **generated reports**, not product source files and should not be committed. The repository administrator must configure required checks and deletion/force-push protection in GitHub **Settings → Rules → Rulesets**; CI cannot grant itself branch-protection permissions.

### Historical periodic integrity validation

On implementation and repair commit `6c768dc`, the [GitHub Actions run](https://github.com/Play1live/RepoHarbor/actions/runs/44622832849) passed all six CI jobs, including both Python matrix jobs and 104 fault-injection tests. Local runs on the same commit passed 979 tests each on Python 3.12 and 3.13. They measured 98.54% total line coverage, 96.47% total branch coverage, and 97.26% line coverage for `integrity`. Review the CI run for the exact commit being accepted.

### Historical baseline before provider discovery

For the reviewed development commit `d3d8080` on 2026-11-12, the associated [GitHub Actions run](https://github.com/Play1live/RepoHarbor/actions/runs/43288735934) reported **201 passed tests** on each of Python 3.12 and 3.13, **99.30% line coverage**, **98.30% branch coverage**, and 49 passed fault-injection tests. The six CI checks were successful at that commit. Future commits must be validated using their **own** completed CI runs; local and CI coverage values can vary slightly with timing.

### Historical GitHub discovery validation

The implementation commit `0b54760` was validated locally on Python 3.12.14 and 3.13.5 with **359 passing tests on each**, **98.90% line coverage** and **97.56% branch coverage**. All six critical module gates then in force passed (the lowest was `git_backup` at 97.92% lines). This is a local result, not a claim about a future PR/CI run. The existing historical CI baseline above refers to its own earlier commit.

The source-status correction adds 14 regression cases covering actual Git access failure followed by successful API discovery, exclusion/visibility/storage skips, unknown initial Git status, successful local Git fetch, retained source extensions and timestamps, API absence/failure, and restart with schema-1 metadata. The corrected code passed **373 tests** on Python 3.12 before the version/documentation completion, with **98.90% line** and **97.56% branch coverage** and all coverage gates satisfied. The earlier 359-test result above remains a historical snapshot of its named commit; the final PR commit must pass its own CI run.

Use a separate `COVERAGE_FILE` and report paths if running Python versions concurrently, or isolate each interpreter in its own CI job. For example, using the appropriate virtual environment:

```sh
COVERAGE_FILE=/tmp/repoharbor-312.coverage .venv/bin/pytest --cov \
  --cov-report=json:/tmp/repoharbor-312.json --cov-report=term-missing
.venv/bin/python scripts/check_coverage.py /tmp/repoharbor-312.json
```

The GitHub discovery tests use fictional API payloads and real local Git mirrors. They verify deterministic credential association, partial-token failure with independent manual processing, persisted cooldowns, URL/ID conflicts, non-destructive restart recovery, and offline restore after rename. Fault injection includes interrupted discovery metadata publication, interrupted provider clones, and ENOSPC/EIO before metadata creation. No live GitHub token or provider contract test is required by the suite; actual organization permissions, SSO and fine-grained grants still require operator validation.

Ruff, format checks, strict mypy, Bandit at the CI medium/high threshold, dependency audit, Gitleaks, Compose validation, local Docker build and smoke tests also passed during the historical GitHub discovery validation. None establishes real NAS/Portainer acceptance or authorizes image publication.

## Documentation and production acceptance

On the target NAS or Portainer endpoint, validate both the headless default and an isolated dashboard-enabled stack. Use a disposable repository and backup directory. Verify browser and backend certificate validation, loopback or private-network publication, an unauthenticated status denial, successful sign-in, disabled manual actions, and a successful backup after intentionally enabling manual execution. Inspect the six views on desktop and mobile and confirm stored mirrors remain intact after a restart. Keep production backups and credentials out of this acceptance run.

The [root README](../README.md) summarizes the architecture and supported features. The [deployment guide](../deploy/README.md) provides host and Portainer setup plus offline restore, while [SECURITY.md](SECURITY.md) documents the data-preservation rules.

Before production use, an operator must separately verify the actual host NAS mount, filesystem locking and durability, UID/GID and ACL permissions, selected credential mode, a real scheduled backup, recorded timestamps, controlled container restart, and a complete offline restore. Automated validation or an apparently healthy Docker container is **not** evidence of a successful backup.

For deployments that enable file output, verify the dedicated log bind mount, effective UID/GID, ACLs, private files, and directory locking on the real filesystem. Run a disposable backup, confirm the expected console and file events, and test both size-based retention limits. Recreate the container with the same mounts and verify that the active application log is appended. Stop the service and verify the original mirror can still be restored offline.


### Browser and image verification

Install `requirements-browser.lock` with `pip --require-hashes` and install Chromium
with `python -m playwright install chromium`. Run
`REPOHARBOR_BROWSER_TEST=1 pytest tests/integration/test_dashboard.py -k browser`.
The test uses a temporary mirror, locally issued certificate, and fixture credentials.

CI publishes the image ID, CycloneDX SBOM, and Debian and Python advisory report.
The gate fails on known high or critical findings with vendor fixes. Review the
full report for findings without vendor fixes and for lower-severity issues.
CodeQL analyzes Python and JavaScript in its own workflow. Python development and
build artifacts are pinned by hash. The base-image digest is fixed and Debian
packages follow signed Bookworm updates, so different build dates can yield
different package revisions. Review lock and digest updates explicitly.
