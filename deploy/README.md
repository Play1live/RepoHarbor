# RepoHarbor v1.0.0 — Docker Compose, Portainer, and NAS deployment

This guide covers RepoHarbor **v1.0.0**, the first stable release following successful NAS/Portainer acceptance of v0.9.2. Automated Docker testing covers Linux/amd64. Validate each new NAS/Portainer deployment on its target host. Adapt the fictional repository, credential, and path examples to your environment.

## 1. Requirements and host storage

- Linux Docker host, Docker Compose v2, and BuildKit to build the image locally. The Portainer instructions assume a **standalone Docker endpoint**, not Swarm.
- Python 3.12, Git, and CA certificates are included in the container. A host-side offline restore requires a Git installation on the restore host.
- The container runs as **UID/GID 10001:10001** without added Linux capabilities. The default deployment has no TCP listener or Docker healthcheck. Automated image checks cover Linux/amd64; verify other architectures separately.
- A pre-existing, persistent backup bind mount backed by a trusted local filesystem supporting `flock`, atomic rename, and `fsync`. SMB/NFS do not have guaranteed consistency for an active RepoHarbor backup root.

**Verify the intended storage device before creating directories.** The example below assumes that `/srv/repoharbor` itself is a dedicated, mounted filesystem:

```sh
mountpoint -q /srv/repoharbor
findmnt --mountpoint /srv/repoharbor -o SOURCE,TARGET,FSTYPE,OPTIONS
df -h /srv/repoharbor
```

Compare the actual device and mount options with the intended storage. If the filesystem is mounted somewhere else, adapt the commands and all bind paths. `findmnt -T` by itself can show only the host's root filesystem; a Docker bind mount cannot prove that the correct NAS volume is mounted behind the host path.

On the verified filesystem, prepare dedicated directories with appropriate ownership:

```sh
sudo install -d -o 10001 -g 10001 -m 0750 /srv/repoharbor/backups
sudo install -d -o 10001 -g 10001 -m 0700 /srv/repoharbor/secrets
sudo -u '#10001' -g '#10001' test -w /srv/repoharbor/backups
```

Check the target NAS's ACL and UID/GID mapping as well. Do not recursively change permissions or initialize over existing backups to fix a startup error. RepoHarbor writes its mirrors and metadata in `/backups` but only reads its secret files. The Compose example sets `create_host_path: false` so Docker will not silently create missing host paths.

## 2. Build the image and prepare the stack

Run Compose commands from the **repository root**:

```sh
docker compose -f deploy/compose.example.yaml build
docker run --rm repoharbor:local-test --version
```

The version command should print `1.0.0`. `repoharbor:local-test` is a local, mutable **test image**. The build needs access to the pinned Python packages and Debian package sources. If an authorized TLS-intercepting build proxy requires a custom CA, the Dockerfile supports a temporary BuildKit secret:

```sh
docker build --secret id=build_ca,src=/path/to/trusted-ca.pem -t repoharbor:local-test .
```

Keep certificate validation enabled. Start with [`compose.example.yaml`](compose.example.yaml) and adapt the host paths, sources, schedule, and budgets. Keep its `restart: unless-stopped`, `stop_grace_period: 5m`, non-root user, and security settings. The example leaves application ports unpublished. The optional dashboard instructions below show how to publish its TLS listener on host loopback. The example omits Docker init for NAS compatibility. RepoHarbor handles signals and Git child processes directly.

## 3. Configure sources and credentials

The configuration is embedded as multiline YAML in the **`REPOHARBOR_CONFIG`** environment variable. The local timezone is controlled separately with `TZ`. The configuration is validated at startup and does **not** hot-reload; restart/redeploy the service to apply changes.

The supplied Compose example defaults to `REPOHARBOR_TOKEN_SOURCE: file`, an empty credentials list, and a **fictional public source URL** (`code.example.org`). The read-only secrets directory must exist even when no token file is needed. Replace the placeholder source with a real permitted repository or set `manual_git_urls: []` to start with no backup sources.

### Recommended: file-based credentials

For a fictional private repository, replace the **value** of `REPOHARBOR_CONFIG` with the following YAML. Keep `REPOHARBOR_TOKEN_SOURCE: file` in the Compose environment and retain a suitable `TZ` setting:

```yaml
schema_version: 1
schedule: "0 3 * * *"
credentials: [example_primary]
manual_git_urls:
  - alias: example-demo
    url: https://code.example.org/team/demo.git
    web_url: https://code.example.org/team/demo
    credential: example_primary
storage:
  global_gib: 50
  default_repo_gib: 2
  overrides:
    example-demo: 4
  during_transfer: finish
  min_free_gib: 2
recovery:
  automatic_repair: true
  max_repair_attempts_per_incident: 3
features:
  manual_run: false
  restore_helper: false
```

The service expects the token in `/run/secrets/repoharbor/example_primary`, corresponding here to the fictional host path `/srv/repoharbor/secrets/example_primary`. The file must contain **only the token**, not a URL or YAML configuration:

```sh
sudoedit /srv/repoharbor/secrets/example_primary
sudo chown 10001:10001 /srv/repoharbor/secrets/example_primary
sudo chmod 0400 /srv/repoharbor/secrets/example_primary
```

Keep the secrets bind mount read-only. Do not commit, print, screenshot, or log token values. The application removes trailing CR/LF when loading a secret file; a missing, unreadable, empty, oversized, or non-private credential causes validation to fail. Use a regular file with mode 0400 or 0600 owned by the service UID. Root ownership is also accepted when the service can read the file. For Docker Secret drivers that publish broad modes, use a private file in the protected host bind mount. Keep NAS ACLs private. Restart the service after token rotation.

### Alternative: environment-based credentials

Replace the **entire `environment` block** of the service with this example and **remove the secrets bind mount**. Keep the backup mount and the other service options:

```yaml
environment:
  TZ: UTC
  REPOHARBOR_TOKEN_SOURCE: env
  REPOHARBOR_TOKEN_EXAMPLE_PRIMARY: ${REPOHARBOR_TOKEN_EXAMPLE_PRIMARY:?Provide the token securely}
  REPOHARBOR_CONFIG: |
    schema_version: 1
    schedule: "0 3 * * *"
    credentials: [example_primary]
    manual_git_urls:
      - alias: example-demo
        url: https://code.example.org/team/demo.git
        credential: example_primary
    storage:
      global_gib: 50
      default_repo_gib: 2
      overrides: {}
      during_transfer: finish
      min_free_gib: 2
    recovery:
      automatic_repair: true
      max_repair_attempts_per_incident: 3
    features:
      manual_run: false
      restore_helper: false
```

Supply the interpolated variable through a protected local mechanism or the Portainer stack environment; do not insert real values in tracked YAML. Environment secrets are visible to authorized Docker/Portainer administrators. RepoHarbor explicitly supports **one credential mode per container**. A configured secret present in both the active environment and the opposite file mode causes a validation error; remove the old values when switching modes.

Multiple named credentials are supported. Each configured source may reference one credential name; for example, `example_primary` maps to `REPOHARBOR_TOKEN_EXAMPLE_PRIMARY`. If a private Git server uses its own CA, mount a trusted certificate bundle read-only and set `SSL_CERT_FILE` to its path. TLS verification must remain enabled. See the [configuration reference](../docs/CONFIGURATION.md) for normalization rules, supported fields, and defaults.

### GitHub and GitLab alongside manual sources

Use the [combined configuration example](../docs/CONFIGURATION.md#combined-github-and-gitlab-example) as the value of `REPOHARBOR_CONFIG`. It configures GitHub.com, two tokens for GitLab.com, a Self-Managed GitLab instance at `gitlab.example.org:8443`, and a manual HTTPS source. Retain the Compose mounts, non-root user, restart policy, and credential mode.

In file mode, provision `example_github`, `example_gitlab`, `example_gitlab_team`, and `example_self_managed` as separate protected files using the procedure above. In env mode, supply `REPOHARBOR_TOKEN_EXAMPLE_GITHUB`, `REPOHARBOR_TOKEN_EXAMPLE_GITLAB`, `REPOHARBOR_TOKEN_EXAMPLE_GITLAB_TEAM`, and `REPOHARBOR_TOKEN_EXAMPLE_SELF_MANAGED` securely and omit the secrets mount. Declare only credential names used by your adapted configuration.

GitHub classic PATs need sufficient repository read access; fine-grained PATs need access to each selected repository plus metadata and contents read permissions. Organization approval or SSO may also be required. Restricted fine-grained tokens do not discover repositories outside their grants, even with `mode: all`. Multiple provider entries reference these existing named credentials; the first in-scope configured provider determines Git authentication.

GitLab requires a human user's personal access token with `read_api` for discovery and `read_repository` for Git-over-HTTPS. The user's membership and project permissions must allow the intended operations. Group and project tokens and bot accounts are unsupported. Use separate credential names per authority. A Self-Managed instance must serve the API at `/api/v4` and advertise HTTPS project URLs matching its configured lowercase DNS or IPv4 authority and port. For a private CA, mount a trusted CA bundle read-only and set `SSL_CERT_FILE` inside the container. Keep TLS verification enabled.

The default selection is `selected` with an empty list. Choose repositories explicitly or deliberately opt into `all`/`all_except`; configure visibility, forks and archived repositories as needed. Include `internal` explicitly to select internal GitLab projects. Explicit exclusion and property/scope filters always take precedence. Changes take effect after restart and apply to future scheduled and manual runs.

GitHub mirrors live under `/backups/github/github.com/<repository-id>/`. GitLab.com uses `/backups/gitlab/gitlab.com/<project-id>/`. Self-Managed GitLab uses `/backups/gitlab/<host>/<project-id>/`, or `<host>~<port>` for an explicit port. Manual paths remain under `/backups/manual/<alias>/`. For an offline GitHub restore, use the local mirror:

```sh
git clone --no-hardlinks /path/to/backups/github/github.com/654321/repository.git ./restored-example
```

IDs here are fictional. Read the actual ID and path from your metadata. Renames and transfers retain that ID directory. Excluded, unavailable and conflicted mirrors remain stored and count against storage limits.

Inspect `data.json.discovery` when GitHub sources do not run. `GITHUB_UNAUTHORIZED` indicates an invalid/expired token; `GITHUB_FORBIDDEN` indicates insufficient permission; rate-limit, API/network and invalid-page failures pause the entire GitHub part while independent manual sources continue. Rate-limit retry times persist across restart; startup itself does not trigger new discovery. Current GitHub selection must be confirmed before remote repair, so `repair_deferred: DISCOVERY_REQUIRED` can persist until successful discovery in a later backup run. These states do not prove remote deletion. API visibility and Git access are separate: `source.discovery_status` and `source.last_seen_at` describe API observation, while `source.status` retains the last confirmed Git transport result. A successful discovery cannot clear a Git failure; new or skipped repositories are not reported as Git-accessible until a successful Git query or operation.

Manual/API identity collisions block automatic assignment (`manual_identity_conflict`). An unresolved manual GitHub address pauses discovery, and a new manual alias for a known provider mirror is withheld (`PROVIDER_IDENTITY_CONFLICT`). Resolve the source configuration without deleting or transferring backup data. Details and limitations are in the [identity rules](../docs/CONFIGURATION.md#stable-paths-and-manual-conflicts).

For GitLab, inspect `data.json.provider_discovery["gitlab:<authority>"]` after a scheduled run. Check that the authority's `completed_at` and the project's `source.last_seen_at` match the scheduled run you are reviewing. The project should report `source.discovery_status: available`. For each expected project, also check that `repository.git/` exists and that `metadaten.json` shows `backup.status` of `updated` or `up_to_date` with a recent successful `last_checked_at`. Test a local restore to verify the mirror. GitLab metadata may have an unknown `default_branch` when the API omits it. The Git result still determines source availability.

| GitLab diagnostic | Check |
|---|---|
| `GITLAB_UNAUTHORIZED` or `GITLAB_FORBIDDEN` | Check token validity, expiry, API scope, and project/group access. A 403 identified as throttling is reported as a rate limit. |
| `GITLAB_MANUAL_IDENTITY_UNRESOLVED` | Check that a manual URL on the affected authority is discoverable through at least one configured token for that authority. |
| `GITLAB_RATE_LIMIT` | Inspect `retry_at` for the authority. Discovery can resume on a scheduled tick after that time. Cooldowns survive restarts and temporary provider removal. |
| GitLab API response, network, pagination, or identity failure | Check TLS trust, authority/port, canonical GitLab URLs, and API compatibility. Incomplete discovery withholds the affected authority's Git work. |
| `GIT_FAILED` after complete discovery | Check `read_repository` and the first in-scope token's project permissions. RepoHarbor records the Git failure without trying another token. |

## 4. Scheduling, limits, and status

`0 3 * * *` means 03:00 every day in `TZ`. Cron uses five fields with numbers, ranges, lists, and steps. Following Vixie cron, when either day field begins with `*`, including `*/1`, `*/2`, or a wildcard-leading list, **both** day fields must match. For example, `0 3 */2 * 1` matches Mondays with an odd month day. If neither day field begins with `*`, matching uses **OR**, including explicit full ranges such as `1-31` and `0-7`. See the [configuration reference](../docs/CONFIGURATION.md#scheduling-and-storage-behavior). Nonexistent DST wall-clock minutes do not run; the repeated minute is run at most once. Missed ticks after downtime are **not** replayed; a scheduled tick during an active run is skipped and recorded durably.

Storage values are in GiB (`1 GiB = 1,073,741,824 bytes`). `null` disables an individual soft size budget and `0` prevents starting new operations. Per-repository overrides use manual aliases or stable GitHub/GitLab keys. The global budget includes all data in the backup root, including excluded repositories, metadata, journals, partial clones, and quarantine. A per-repository budget counts its entire source directory.

**These are soft budgets, not filesystem quotas.** With `during_transfer: finish` (the default), a running transfer may exceed the configured size budget; `abort` can interrupt it when a limit is detected. The physical free-space reserve takes priority in either mode. Git stop signals are polled frequently, while capacity checks run at most every five seconds. Real disk exhaustion is still possible without host-side quotas. Storage is measured before and after operations and rechecked on the next regular run; valid backups are never deleted automatically to reclaim capacity.

| Backup state | Meaning and operator action |
|---|---|
| `updated` / `up_to_date` | Successful mirror change or successful check with no changes. |
| `global_limit_paused` / `repo_limit_reached` / `FREE_SPACE_RESERVE` | A new operation was skipped because of capacity; check usage and wait for the next scheduled run. |
| `repair_paused` with `repair_deferred` | A needed repair is waiting for storage capacity or complete discovery. This pause does not consume a repair failure attempt. |
| `interrupted` | An operation was interrupted or has not been proven complete; recovery inspects it. |
| `repair_failed` | Automatic repair failed or could not proceed; preserve the data and investigate. |
| `manual_attention_required` (metadata field) | An unknown partial clone, occupied quarantine, or conflict blocks further downloads until explicitly resolved. |

Selection, remote availability, and integrity are separate metadata dimensions. `last_checked_at` is only advanced by a successful remote check; `last_updated_at` only by a proven successful changed mirror. A skipped or failed run must not be reported as a successful backup.

### Enable periodic integrity checks

Add these settings inside the existing `REPOHARBOR_CONFIG` YAML. Merge `integrity_check` into the existing `features` mapping and apply the updated Compose or Portainer stack.

```yaml
features:
  integrity_check: true
integrity:
  schedule: "0 4 * * 0"
  include_excluded: true
```

The feature defaults to disabled. When enabled, the example checks existing mirrors every Sunday at 04:00 in `TZ`, independently of the backup cron. Set `include_excluded: false` to skip excluded mirrors. The existing service container performs the checks without additional ports or containers.

A full Git check reads the object database and can use substantial CPU, memory, and NAS I/O on large mirrors. Repositories are checked sequentially. Choose a quiet window that leaves room for the entire run. Backups and checks share a lock, and overlapping scheduled runs are skipped. Manual backup requests can return exit `6` (`BUSY`) while a check runs.

Use `repoharbor status` or `repoharbor status --json` to see the next check, current progress, previous result, and counts. Use `repoharbor repositories show example-demo` to inspect the mirror's current integrity status, last periodic result, and diagnostic code. After a restart, a previously running check is recorded as interrupted, and the service waits for the next future tick. A check that did not finish leaves the repository's previous completed result in place.

## 5. Validate, start, and inspect

```sh
docker compose -f deploy/compose.example.yaml config --quiet
docker compose -f deploy/compose.example.yaml run --rm --no-deps repoharbor config validate
```

`docker compose config --quiet` avoids printing expanded secret values. `repoharbor config validate` checks configuration and declared credentials **without contacting remotes or changing the backup state**. Because the Compose service declares bind mounts, the configured host paths must still exist for the `compose run` command. Neither validation step proves the expected NAS device is mounted or that a real backup can be restored.

After confirming host mount, storage permissions, and credential settings, start the service:

```sh
docker compose -f deploy/compose.example.yaml up -d --no-build
docker compose -f deploy/compose.example.yaml logs --tail 100 repoharbor
```

The first start safely initializes missing state in an empty root, inventories existing data, performs necessary crash reconciliation, and then waits for **future** cron ticks. Startup recovery can reconcile provider mirrors locally. Remote repairs for discovered repositories require fresh, complete discovery in a subsequent backup run. Missed backups and periodic checks are skipped. Startup still performs needed recovery with periodic verification disabled. To verify a backup, check its recorded timestamps and test an offline restore.

### Inspect status and run manual backups

Run the CLI under the service's container user (UID 10001). Compose selects the running service by name and inherits that user.

```sh
docker compose -f deploy/compose.example.yaml exec -T repoharbor repoharbor status
docker compose -f deploy/compose.example.yaml exec -T repoharbor repoharbor status --json
docker compose -f deploy/compose.example.yaml exec -T repoharbor repoharbor repositories list --json
docker compose -f deploy/compose.example.yaml exec -T repoharbor repoharbor repositories show example-demo --json
```

Use the ID shown by `repositories list`. For a configured manual source, this is its alias. For a discovered repository, use its stable key, for example `gitlab:gitlab.com:234567`. The read-only commands also work with manual backups disabled. `status` shows service reachability, current and recent runs, the next cron time, recorded storage usage and limits, and provider discovery. The periodic integrity section shows its independent schedule, state, and counts. Use repository details to check selection, Git access, backup timestamps, the most recent completed integrity finding, and repair state.

To enable manual backups, edit the existing `features` block **inside the multiline `REPOHARBOR_CONFIG`**.

```yaml
features:
  manual_run: true
```

Apply the edited configuration with the Compose `up -d --no-build` command above or update and redeploy the Portainer stack. Enabling the flag leaves the cron schedule in place and startup does not trigger a backup. Choose one of the following commands.

```sh
docker compose -f deploy/compose.example.yaml exec -T repoharbor repoharbor backup run --all
docker compose -f deploy/compose.example.yaml exec -T repoharbor repoharbor backup run --repository example-demo
```

On the Docker host managed by Portainer, run `docker ps` to find the container name. Substitute it for the fictional name below. Keep the container's configured user.

```sh
container=example-repoharbor-1
docker exec "$container" repoharbor status --json
docker exec "$container" repoharbor repositories list
docker exec "$container" repoharbor repositories show example-demo
docker exec "$container" repoharbor backup run --all
docker exec "$container" repoharbor backup run --repository example-demo
```

The backup command waits for the result. Exit `0` confirms a successful operation or a successful check with no changes. Exit `4` means the feature is disabled, `5` means the service could not be reached or the connection was lost, and `6` means a backup, retry, or integrity check already holds the shared lock. Requests do not queue. A disconnected CLI leaves the outcome unconfirmed for that client. Check status and repository timestamps before retrying.

Exit `8` can mean that discovery failed or is in cooldown even if an unaffected source succeeded in a full run. Discovered repositories on the affected authority require fresh discovery. An established eligible manual mirror can continue, while an unproven or new same-authority mirror waits. For exit `9`, check repository backup, error, and repair states and the storage limits. See the [full exit-code table](../docs/CONFIGURATION.md#cli-commands-and-exit-codes).

### Enable the web dashboard

The same container serves the dashboard when enabled. Plan the browser URL first. The example below uses `https://backup.example.org` through a TLS reverse proxy on the Docker host. Obtain a browser-trusted certificate for that URL and a backend PEM certificate and key trusted by the proxy. The proxy can verify a dedicated backend name that matches the backend certificate.

On the verified host filesystem, create a separate directory for dashboard secrets. The example uses a new, generated hexadecimal password and never places it in the stack YAML.

```sh
sudo install -d -o 10001 -g 10001 -m 0700 /srv/repoharbor/web-secrets
sudo -u '#10001' -g '#10001' sh -c \
  'umask 077; set -C; openssl rand -hex 32 > /srv/repoharbor/web-secrets/password'
sudo install -o 10001 -g 10001 -m 0400 /path/to/backend-chain.pem \
  /srv/repoharbor/web-secrets/tls.crt
sudo install -o 10001 -g 10001 -m 0400 /path/to/backend-key.pem \
  /srv/repoharbor/web-secrets/tls.key
```

Merge these options into the existing service. Keep its backup and Git credential mounts. Inside `REPOHARBOR_CONFIG`, edit the existing `features` and `web` mappings and retain the other settings.

```yaml
services:
  repoharbor:
    ports:
      - "127.0.0.1:8080:8080"
    environment:
      REPOHARBOR_WEB_PASSWORD_FILE: /run/secrets/dashboard/password
      REPOHARBOR_WEB_CERT_FILE: /run/secrets/dashboard/tls.crt
      REPOHARBOR_WEB_KEY_FILE: /run/secrets/dashboard/tls.key
      REPOHARBOR_CONFIG: |
        schema_version: 1
        features:
          web_ui: true
          manual_run: false
        web:
          bind: 0.0.0.0
          port: 8080
          origin: https://backup.example.org
    volumes:
      - type: bind
        source: /srv/repoharbor/web-secrets
        target: /run/secrets/dashboard
        read_only: true
        bind:
          create_host_path: false
```

The bind address is inside the container. Publishing on host loopback keeps the host proxy's backend port off the LAN. For a containerized proxy, use a dedicated shared Docker network, connect to RepoHarbor on its private container port, and omit the host port publication. Restrict the browser endpoint to a trusted management network or VPN.

Configure the proxy to preserve the browser's `Host` and `Origin` headers, verify the backend certificate, disable caching for dashboard requests, and limit connection counts and header/body read times. Set a response timeout appropriate for backups or use CLI for lengthy runs. After an interrupted browser request, inspect status before retrying. The service retains ownership of any accepted operation. The [NGINX proxy module reference](https://nginx.org/en/docs/http/ngx_http_proxy_module.html) documents upstream TLS verification and request forwarding.

For direct access on the Docker host without a proxy, set `web.origin` to `https://127.0.0.1:8080`, keep the host loopback mapping, and use a browser-trusted certificate with the loopback IP in its subject alternative names. Open the exact configured origin and enter the administrator password. Set `features.manual_run: true` to enable backups from the browser and CLI, as well as integrity checks from the dashboard.

Select **Check integrity** in a repository's details or **Check all repositories** beside **Backup all repositories**. Confirm the scope and review the progress and result on the Repositories page. Checks are local and read-only, including eligible excluded mirrors. A check can continue after the browser disconnects. If the service is busy, wait for the current operation to finish before submitting another request. A service restart records an unfinished check as interrupted.

In Portainer, create the files on the Docker endpoint host, use the same read-only bind mount and loopback publication or private proxy network, then redeploy the existing stack. For startup diagnostics, check the container log for `web.ready` or `web.unavailable` when extended logging is active. Without extended logging, look for `HTTPS dashboard ready` or `Web dashboard could not open securely`. Confirm the bind/port, exact browser origin, certificate chain, and that UID 10001 can read all three files. Sessions expire after 15 minutes and after a restart.

By default the sign-in limit applies to the TCP peer, including a reverse proxy. To distinguish clients behind a dedicated proxy, set `web.trusted_proxy` to its exact backend-facing IPv4 address and limit access to the backend listener to that proxy. Configure the proxy to **overwrite** `X-Real-IP` with the verified client address and apply per-client limits to `POST /api/login`. RepoHarbor ignores `Forwarded` and `X-Forwarded-For`. Custom images must provide OpenSSL-backed scrypt through Python's `hashlib`. The supplied Docker image verifies scrypt during dashboard startup.

### Enable automatic retries

Add the following to your existing `REPOHARBOR_CONFIG` YAML. Merge `auto_retry` into your existing `features` mapping.

```yaml
features:
  auto_retry: true
retry:
  max_retries: 3
  initial_delay_seconds: 300
  max_delay_seconds: 3600
```

Redeploy against the existing persistent backup mount. The settings allow three additional attempts per repository after eligible temporary Git failures. The manual command returns its original result, and the service handles pending retries independently of `features.manual_run`. Use `repoharbor status --json` for the global pending count and `repoharbor repositories show <repository>` for the next attempt, outcome, or blocking reason. A retry uses the same capacity, selection, and recovery safeguards as backups. A provider cooldown can delay its next attempt. See [automatic backup retries](../docs/CONFIGURATION.md#automatic-backup-retries).

On the target NAS and Portainer endpoint, use a disposable repository to simulate a temporary HTTPS outage. Observe the original failure and pending retry, then recreate the container with the same bind mount. Restore access and confirm that one retry completes with the expected update count. Verify that a storage restriction or excluded source blocks remote Git and that an offline restore succeeds from the resulting mirror.

### Configure log retention

The Compose example uses Docker's `json-file` driver with `max-size: "10m"` (10 MiB per file) and `max-file: "5"` (five files total, including the active one). This is about 50 MiB of Docker-log storage per container, plus record and filesystem overhead. Both basic and extended console output remain available through `docker compose logs` and Portainer. Docker applies these settings when it creates the container.

For structured console events, add `extended_logging: true` under the existing `features` mapping in `REPOHARBOR_CONFIG`. For persistent application logs, also set `logging.file.enabled: true` and mount the directory that directly contains `logging.file.path`. The default path is `/logs/repoharbor.log`.

On the Docker host, provision a **separate** directory owned by the container UID/GID. With the default identity, a host administrator can use

```sh
install -d -m 0700 -o 10001 -g 10001 /srv/repoharbor/logs
```

Add the bind mount to the service's existing `volumes` list in Compose or Portainer. Keep the existing backup and secret mounts.

```yaml
- type: bind
  source: /srv/repoharbor/logs
  target: /logs
  bind:
    create_host_path: false
```

The application defaults allow one active file and five archives at 10 MiB each (about 60 MiB), separate from the Docker log budget. If logs and backups share a physical device, provision space for both and the backup free-space reserve. On a NAS, verify that effective permissions and locking allow the UID to create, append, and rename logs, and that only authorized operators can read the files. If offline restore uses the same logging settings, pass the same log mount to the restore container after stopping the service. Use the console-only configuration for a restore without that mount. [Formats, levels, and limits](../docs/CONFIGURATION.md#extended-logging)

## 6. Portainer and controlled updates

The image must exist on the **same Docker endpoint managed by Portainer**. Do not assume the web-based stack editor builds the image. Build it on the endpoint first, then paste or import the Compose definition in Portainer and **remove the `build:` block**, leaving `image: repoharbor:local-test`. Bind paths refer to the Docker host, not the browser computer. No registry publication or automatic image availability is assumed.

If the image is built on another trusted machine, an operator may transfer it explicitly:

```sh
docker image save -o /tmp/repoharbor-local-test.tar repoharbor:local-test
# Transfer the archive securely to the destination Docker host.
docker image load -i /tmp/repoharbor-local-test.tar
```

These commands **do not publish** the image. Configure Portainer environment variables and read-only secrets, inspect the entire stack, and deploy only after reviewing the target endpoint. Specific NAS or Portainer UI versions have not been production-validated.

Before updating a deployment, verify the current backup state and an offline restore. Stop the service, build/import the intentionally chosen image, and recreate it with the **same persistent mounts**:

```sh
docker compose -f deploy/compose.example.yaml stop --timeout 300 repoharbor
docker compose -f deploy/compose.example.yaml up -d --no-build --force-recreate
```

SIGTERM prevents new Git work and stops the active Git process; unfinished work is reconciled on restart. The mutable `local-test` tag is not a reproducible release identifier. Before rolling back, verify that the older version understands the existing provider metadata and journal references. An upgrade from v0.9.2 to v1.0.0 keeps schema 1 for configuration, state, metadata, and the journal. Existing retry state carries forward. Extended logging is opt-in and the example's Docker retention settings take effect after recreation. Review pending work and reconcile interrupted operations before changing versions. Do not delete or reinitialize `data.json`, repository metadata, mirrors, or the lock file in an attempt to bypass an error.

## 7. Quarantine and troubleshooting

Only an initial clone with **durable proof of Git completion** and a fresh integrity check may become a valid mirror. A known interrupted initial clone can be journaled and moved to one preserved `quarantine/initial-clone` slot; a new clone can begin during a later eligible backup run. Quarantined data is never automatically rotated or deleted.

For `manual_attention_required`, **stop the service before touching backup data**. Inspect metadata and the global journal and preserve both the partial and any existing quarantine. An operator may deliberately move conflicting content to a **unique, non-overwriting** archive directory under `<backup-root>/operator-archive/`, outside `manual/`; these archived bytes still count toward the global budget. Never overwrite a valid mirror, remove the lock file as a supposed unlock operation, or fabricate successful timestamps. If a pending quarantine slot has gone missing, restore those recorded bytes from a preserved archive or leave the case blocked. See [SECURITY.md](../docs/SECURITY.md) for the safety rules.

| Symptom | Safe checks |
|---|---|
| Invalid configuration | Check YAML indentation, `schema_version: 1`, supported fields, aliases, and credential names. Feature flags and `integrity.include_excluded` must be booleans. Verify cron expressions, retry and logging bounds, and the canonical HTTPS origin if the dashboard is enabled. |
| Dashboard unavailable or sign-in fails | Check the published port and exact browser origin. Check certificate trust and certificate names, proxy header forwarding, key and password file ownership and permissions, and the dashboard startup event in the container log. For sign-in throttling, wait one minute before trying again. |
| `LOG_MOUNT_MISSING` / `LOG_MOUNT_UNSAFE` | Check the actual log bind-mount target, the dedicated host directory, UID/GID, and ACLs. |
| `LOG_FILE_UNSAFE` / `LOG_FILE_UNAVAILABLE` | Check for mode `0600`, matching UID, single-link regular files, sufficient access and space, and a single writer. |
| `logging.file_failed` | Check the log mount's free space, filesystem health, and permissions. Console logging continues. Restart to resume file logging after correcting the host issue. |
| Git access denied / token expired | Source permission, credential mode, token rotation; never put credentials into a remote URL or debug output. |
| TLS error | Server certificate and trusted CA bundle; never disable verification. |
| Mount / `EACCES` | Actual host filesystem, ACLs, UID/GID 10001, read-only secret bind; do not switch to a privileged container. |
| `ENOSPC` or a budget pause | Free space and every preserved directory, including quarantine and operator archives; do not mass-delete mirrors. |
| Process lock | Stop the other running instance. After a forced stop, wait for any surviving Git process to exit. An existing lock file alone does not indicate a held lock. |
| `corrupt` or `repair_failed` | Preserve the data. A later eligible backup can attempt bounded repair if configured. If the required objects are available from a verified copy, follow the offline procedure below. |
| `uncheckable` | Use the periodic error code to check the mount, read access, Git timeout, or repository layout. Keep the existing mirror and metadata while investigating. |
| Invalid JSON | Preserve the journal and repository data for inspection. |

### Verify an offline mirror repair

1. Stop RepoHarbor and any other writer. For Compose, use `docker compose -f deploy/compose.example.yaml stop --timeout 300 repoharbor`. In Portainer, stop the RepoHarbor service. Wait for surviving Git children to exit before touching the backup root.
2. Preserve the affected mirror, its metadata, the central journal, and any partial or quarantine data before changing files. Verify the target directory and repair only the confirmed damage using objects from a trusted, independently verified source. Work on a separate copy first if the repair is uncertain. Keep the previous data available for review.
3. Restart RepoHarbor after validating the repaired mirror. Leave the journal and metadata for RepoHarbor to reconcile. Wait for the next enabled periodic tick and compare `integrity_status` with the latest `periodic_integrity` result in `repositories show example-demo --json`.
4. A full healthy recheck can clear obsolete damage-related repair blocks. An unresolved operation, manual-attention marker, or partial or pending quarantine still needs its own resolution. Excluded sources remain excluded. Backup success timestamps change only through the normal backup flow.

See the [security and recovery rules](../docs/SECURITY.md#integrity-checks-and-bounded-repair) for the conditions governing a return to healthy status.

## 8. Offline restoration and acceptance

### Restore with the RepoHarbor CLI

Use the assistant when the backup root contains a readable schema-1 `data.json`, an existing regular `.repoharbor.lock`, the repository's `metadaten.json`, and a valid `repository.git`. An unfinished backup operation for the selected mirror blocks the assistant. Stop the backup service so the restore process can obtain the exclusive root lock.

For Compose, stop the service from the repository root. For Portainer, stop the running stack's RepoHarbor service and run the one-off commands below on the Docker endpoint that holds the image and mounts.

```sh
docker compose -f deploy/compose.example.yaml stop --timeout 300 repoharbor
```

Use the existing backup directory as a read-only bind mount. Prepare a separate writable export directory on the verified host filesystem. For a new directory at the fictional path below, grant access to UID/GID `10001:10001`.

```sh
sudo install -d -o 10001 -g 10001 -m 0750 /srv/repoharbor/exports
```

Run the one-off container with the local image. The commands use the image entrypoint and its default UID/GID `10001:10001`, a read-only container filesystem, and no network. The minimal configuration enables restore and declares no credentials. `REPOHARBOR_TOKEN_SOURCE` selects a configuration mode and requires no token here. The container needs no ports, elevated privileges, secrets mount, or Docker socket.

```sh
docker run --rm --network none --read-only --cap-drop ALL \
  --security-opt no-new-privileges=true \
  --mount type=bind,src=/srv/repoharbor/backups,dst=/backups,readonly \
  --mount type=bind,src=/srv/repoharbor/exports,dst=/exports \
  -e REPOHARBOR_TOKEN_SOURCE=file \
  -e 'REPOHARBOR_CONFIG=schema_version: 1
features: {restore_helper: true}' \
  repoharbor:local-test restore clone \
  --repository example-demo --destination /exports/restored-demo
```

Add `--branch development` to the clone command to check out that branch if it is present in the mirror. Use a new destination for each run. For provider repositories, use a stable key such as `github:github.com:654321`, `gitlab:gitlab.com:234567`, or `gitlab:gitlab.example.org:8443:345678`. Excluded, removed, and unreachable sources can be restored from valid local backups.

### Export and use a Git bundle

```sh
docker run --rm --network none --read-only --cap-drop ALL \
  --security-opt no-new-privileges=true \
  --mount type=bind,src=/srv/repoharbor/backups,dst=/backups,readonly \
  --mount type=bind,src=/srv/repoharbor/exports,dst=/exports \
  -e REPOHARBOR_TOKEN_SOURCE=file \
  -e 'REPOHARBOR_CONFIG=schema_version: 1
features: {restore_helper: true}' \
  repoharbor:local-test restore bundle \
  --repository example-demo --output /exports/example-demo.bundle
```

Both commands need an existing, writable target parent outside the backup root and configured secrets directory. Existing files and directories are refused, even if empty. The output filesystem must support private staging, symlinks, atomic no-replace rename, and synchronization. Allow space for the staging bundle, its verification import, and the working tree for a clone. The final working directory has mode `0700` and the bundle has mode `0600`. Use an account with permission to read the output.

A bundle contains Git refs and the objects needed to reconstruct their history. To create a working tree from it, run Git on a host that can read the bundle. The first example uses the bundle's default checkout when available. Use the branch example instead when selecting an existing branch explicitly. The mirror clone preserves all exported refs in a new bare repository. A bundle records symbolic refs as resolved object IDs.

```sh
git clone /srv/repoharbor/exports/example-demo.bundle ./restored-from-bundle
git clone --branch development /srv/repoharbor/exports/example-demo.bundle ./restored-development
git clone --mirror /srv/repoharbor/exports/example-demo.bundle ./restored-mirror.git
```

RepoHarbor verifies the bundle by comparing refs and importing into an empty Git repository before publication. A bundle requires at least one exportable ref or a valid detached `HEAD`. An empty mirror can still be restored as an empty Git repository.

### Recover directly with Git

Use direct Git recovery when the RepoHarbor journal or metadata is missing or invalid but an intact mirror is available. Stop any writer and verify the source is a complete mirror before cloning. Direct Git recovery bypasses the assistant's catalog checks, exclusive root lock, path checks, and atomic publication.

With Git installed on the restore host and read permission on a verified local mirror, clone into a **new destination directory**:

```sh
git clone --no-hardlinks \
  /srv/repoharbor/backups/manual/example-demo/repository.git \
  ./restored-example
```

For a GitLab mirror, substitute its local project ID path. This example uses the Self-Managed authority with a non-default port:

```sh
git clone --no-hardlinks \
  /srv/repoharbor/backups/gitlab/gitlab.example.org~8443/345678/repository.git \
  ./restored-gitlab-demo
```

Direct Git recovery needs only the intact local mirror and Git. Git project files and ordinary Git history are available if the mirror is valid and the default branch exists. For a different existing branch, use `git clone -b <branch> --no-hardlinks ...`. An empty repository or missing default branch will not automatically produce a checked-out working tree. Never modify the mirror to perform a restore.

A mirror follows remote branch/tag deletion and force-pushes. It does **not** provide historical snapshots or preserve Git LFS payloads, separate submodule repositories, issues, pull/merge requests, other hosting metadata, or unpushed local changes. Neither restore workflow nor bundle export retrieves those separately. Plan separate protection for any of those requirements.

**Before production use**, accept the actual host mount, filesystem behavior, user permissions, chosen authentication mode, a real scheduled backup, recorded timestamps, container recreation, and an offline restore on the target NAS. Automated local tests and green CI are not a substitute for that acceptance test.


### Upgrade to v1.0.0

Stop the v0.9.2 instance and recreate it with the v1.0.0 image. Reuse the existing
configuration, backup bind mount, and credential setup. Existing schema-1 data,
repository IDs, aliases and excluded mirrors need no initialization or data migration.
Review Git secret permissions and use one credential name per HTTPS authority. For a
mirror whose local Git settings are rejected, preserve the original and review a copy
offline before attempting another backup.

The backup root must be persistent. Linux containers reject tmpfs and ramfs at that path.
To detect a wrong bind mount, optionally create a regular `.repoharbor-storage-id` file on
the intended volume and set `REPOHARBOR_STORAGE_ID` to its ASCII contents, excluding the
final newline. Use the same setting for restore if you want it to verify the volume. A
missing or mismatched configured marker stops the operation. Independently verify the
actual host device when deploying without a marker.

A full run with missing included sources does not advance its successful timestamp.
Verification of unchanged mirrors can take longer. Review the coverage summary and
verify both an updated mirror and an offline restore on the target storage.
