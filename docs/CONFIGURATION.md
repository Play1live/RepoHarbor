# Configuration reference (schema 1)

RepoHarbor `0.9.0` supports discovery on GitHub.com, GitLab.com, and compatible Self-Managed GitLab instances, alongside manual HTTPS Git remotes. The web dashboard is optional. GitHub Enterprise discovery and SSH are outside this version's scope.

The canonical service configuration is a YAML document supplied through **`REPOHARBOR_CONFIG`**. The image also accepts `TZ`, `REPOHARBOR_TOKEN_SOURCE`, `REPOHARBOR_BACKUP_ROOT`, and `REPOHARBOR_SECRETS_DIR`. Unknown YAML keys are rejected. The schema requires the integer `schema_version: 1`.

For installation, host mounts, and Portainer instructions, see the [deployment guide](../deploy/README.md). For the supported source and storage behavior, refer to the actual [`deploy/compose.example.yaml`](../deploy/compose.example.yaml) and application source.

## Configuration schema

| Key | Supported value and default |
|---|---|
| `schema_version` | Required integer `1`. Other versions are rejected. |
| `schedule` | Five-field cron expression; default `0 3 * * *`. |
| `credentials` | List of unique lowercase credential identifiers; default `[]`. |
| `providers` | List of GitHub and GitLab provider entries; default `[]`. See below. |
| `selection` | Shared GitHub and GitLab selection and filter settings; safe default `selected` with no selected repositories. |
| `discovery.request_delay_ms` | Integer from `0` to `60000`. Default `1000`. See [API pacing and rate limits](#api-pacing-and-rate-limits). |
| `manual_git_urls` | List of manual HTTPS Git sources; default `[]`. Each requires `alias` and `url`; `web_url` and `credential` are optional. |
| `storage.global_gib` | Soft global size budget in GiB, or `null` for unlimited (default). |
| `storage.default_repo_gib` | Soft budget per repository in GiB, or `null` for unlimited (default). |
| `storage.overrides` | Mapping from configured manual aliases or stable keys such as `github:github.com:123456` or `gitlab:gitlab.example.org:8443:345678` to replacement per-repository GiB budgets; default `{}`. |
| `storage.during_transfer` | `finish` (default) or `abort`. |
| `storage.min_free_gib` | Minimum physical free-space reserve; default `2`. Cannot be `null`. |
| `recovery.automatic_repair` | Boolean; default `true`. |
| `recovery.max_repair_attempts_per_incident` | Integer from `1` through `3`; default `3`. |
| `features` | `manual_run: true` enables CLI-triggered and authenticated browser-triggered backups. `restore_helper: true` enables offline restore and bundle export. `integrity_check: true` enables periodic local mirror checks. `auto_retry: true` enables automatic backup retries. `extended_logging: true` enables structured events. `web_ui: true` enables the authenticated HTTPS dashboard. Each is an independent boolean and defaults to `false`. |
| `web.bind` | IPv4 address inside the container. Default `127.0.0.1`. Use a non-loopback address only when the deployment restricts access to this listener. |
| `web.port` | HTTPS listen port inside the container, an integer from 1024 to 65535. Default `8080`. |
| `web.origin` | Browser-facing canonical HTTPS origin. Required with `features.web_ui: true`. Use a lowercase host, an explicit port only for non-default browser ports, and no path, trailing slash, or credentials. |
| `retry.max_retries` | Integer from `1` through `10`. Default `3` additional attempts per failure cycle. |
| `retry.initial_delay_seconds` | Positive integer delay for the first retry. Default `300`. |
| `retry.max_delay_seconds` | Integer from `1` through `86400`, at least the initial delay. Default `3600`. |
| `integrity.schedule` | Independent five-field cron expression. Default `0 4 * * 0` (Sunday at 04:00 in `TZ`). |
| `integrity.include_excluded` | Boolean. Default `true`. Set to `false` to skip excluded mirrors. |
| `logging.level` | Minimum event severity. One of `DEBUG`, `INFO`, `WARNING`, `ERROR`, or `CRITICAL`. Default `INFO`. |
| `logging.format` | `text` for readable key/value lines or `json` for JSON Lines. Default `text`. |
| `logging.file.enabled` | Boolean. Default `false`. Requires `features.extended_logging: true` when enabled. |
| `logging.file.path` | Absolute path to a `.log` file immediately inside a dedicated mount. Default `/logs/repoharbor.log`. |
| `logging.file.max_size_mib` | Integer from 1 through 100. Default 10 MiB. |
| `logging.file.backup_count` | Integer from 1 through 20. Default 5 archives in addition to the active file. |

An example configuration with invented sources and values:

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
  integrity_check: false
  auto_retry: false
  extended_logging: false
retry:
  max_retries: 3
  initial_delay_seconds: 300
  max_delay_seconds: 3600
```

`REPOHARBOR_CONFIG` can be provided in a Docker Compose multiline environment entry as in the [example stack](../deploy/compose.example.yaml). Use `repoharbor config validate` to validate the configuration and referenced credentials; this command does **not** download from a remote or create backup state.

## Environment and credential modes

| Variable | Behavior |
|---|---|
| `REPOHARBOR_TOKEN_SOURCE` | **Required:** exactly `env` or `file`, even if no credentials are configured. |
| `TZ` | IANA timezone for cron; default `UTC`. Example: `Etc/UTC`. |
| `REPOHARBOR_BACKUP_ROOT` | Absolute backup-root path; default `/backups`. |
| `REPOHARBOR_SECRETS_DIR` | Absolute secret-file directory; default `/run/secrets/repoharbor`. |
| `SSL_CERT_FILE` | Optional path to a trusted custom CA bundle for private HTTPS servers; TLS verification remains active. |

For a configured credential named `example_primary`:

- In **`env` mode**, the token is read from `REPOHARBOR_TOKEN_EXAMPLE_PRIMARY`.
- In **`file` mode**, the token is read from the regular UTF-8 file `/run/secrets/repoharbor/example_primary`, mounted read-only. Its trailing CR/LF is stripped.

A configured secret found in both the chosen and opposite modes is rejected. Credential names use lowercase letters, digits, `_`, and `-`, and must be unique. Because `-` and `_` map to the same environment variable name, ambiguous names are rejected. Empty or malformed credential values are rejected. Tokens are never intended to be embedded in URLs or persisted in metadata. Credential rotation requires a restart.

## Source identity, selection, and scope

Each `manual_git_urls` entry needs a stable `alias` (for example `example-demo`) and a credential-free `https://` Git URL. The optional `web_url` describes the repository web page; it does not change the Git transport URL.

Source URLs are normalized conservatively: scheme/host case, default HTTPS port, and trailing slashes are normalized; path case and the `.git` suffix remain significant. Query parameters, URL-embedded username/password, unsupported protocols, and suspicious paths are rejected.

Identical normalized URLs are deduplicated using the **first** alias, provided credentials do not conflict. Aliases identify persistent directories under `manual/<alias>`. Reassigning an existing alias to a different URL, or its URL to another alias, fails closed. Removing a source from the configuration marks the existing repository as excluded; it does **not** delete the mirror. Restoring the same alias/URL makes it eligible for subsequent backup runs.

Manual aliases and provider IDs identify separate storage paths. Existing manual sources retain their original aliases and mirrors.

## Discovery providers

Every provider requires a unique lowercase `id`, a `type`, and a `credential` declared in the top-level list. The name identifies the configured entry. Credentials can be reused across entries for the same provider type and authority (host and optional port). A manual HTTPS source can reference a provider-bound credential only when its clone URL has the same authority. Every provider scope flag defaults to `true` and accepts YAML booleans.

| Field | GitHub | GitLab |
|---|---|---|
| `type` | `github` | `gitlab` |
| `host` | `github.com` only (default) | `gitlab.com` by default, or a supported Self-Managed authority |
| `include_owned` | Repositories owned by the authenticated login | Projects in the authenticated user's personal namespace |
| `include_organizations` | Other repositories owned by organizations | GitHub only |
| `include_collaborators` | Remaining repositories owned by another user | GitHub only |
| `include_groups` | GitLab only | Projects belonging to a discovered member group or its subgroups |
| `include_shared` | GitLab only | Remaining accessible projects, including projects shared into groups |

Provider-specific keys are rejected on the other provider type. Providers for each authority are processed in configuration order. Each repository is in scope if at least one entry sees it in scope. The first such entry supplies the Git credential for that run. Git access failures are recorded without trying another token.

### GitHub discovery

Each token queries `/user` and paginated `/user/repos` with owner, collaborator and organization-member affiliations. An organization-owned repository follows `include_organizations`, including when access was granted through collaboration. Classic and fine-grained personal access tokens (PATs) work within their API and Git permissions. Fine-grained tokens need metadata and contents read access for each granted repository. Organization approval or SSO may be required. Contradictory observations for one ID suspend the entire GitHub discovery.

### GitLab discovery

GitLab.com uses the built-in `gitlab.com` authority. For Self-Managed GitLab, use a lowercase dotted DNS name or IPv4 address with an optional non-default HTTPS port, for example `gitlab.example.org:8443`. Ports must be decimal, from 1 to 65535, without leading zeros. Omit port 443. Use only the authority in `host`. Schemes, credentials, URL paths (including installations under a subpath), queries, fragments, IPv6 literals, single-label names, uppercase hosts, and trailing dots are unsupported. The GitLab provider rejects the exact authorities `github.com` and `api.github.com`. The API must be served at `/api/v4` and canonical HTTPS project URLs must match the configured authority, including its port. Self-Managed instances must provide the API fields and pagination behavior described here and a certificate trusted by the container.

Use a human user's personal access token with `read_api` for discovery and `read_repository` for Git-over-HTTPS, plus sufficient project access for both operations. The API uses `PRIVATE-TOKEN`. Git uses the same token through isolated Askpass with the username `oauth2`. Group and project access tokens and bot accounts are unsupported. Multiple tokens can cover different grants on one instance. Use separate credential names for independent authorities, including when the same DNS name is configured on different ports.

For each token, discovery queries `/user`, member projects (`/projects?membership=true`), owned projects (`/projects?owned=true`), and member groups (`/groups?all_available=false`). Each group is queried with `include_subgroups=true` and with `with_shared=false` and `with_shared=true` to distinguish group-owned projects from shared access. Personal namespace ownership takes precedence over group membership and shared access. The scopes filter this set of observations. RepoHarbor does not search all public projects.

### Combined GitHub and GitLab example

This complete example uses fictional accounts, project IDs, and credential names. Keep credentials outside the YAML.

```yaml
schema_version: 1
schedule: "0 3 * * *"
credentials: [example_github, example_gitlab, example_gitlab_team, example_self_managed]
providers:
  - id: example_github_access
    type: github
    host: github.com
    credential: example_github
    include_owned: true
    include_organizations: true
    include_collaborators: false
  - id: example_gitlab_access
    type: gitlab
    host: gitlab.com
    credential: example_gitlab
    include_owned: true
    include_groups: false
    include_shared: false
  - id: example_gitlab_team_access
    type: gitlab
    host: gitlab.com
    credential: example_gitlab_team
    include_owned: false
    include_groups: true
    include_shared: true
  - id: example_self_managed_access
    type: gitlab
    host: gitlab.example.org:8443
    credential: example_self_managed
selection:
  mode: selected
  visibility: [public, private, internal]
  include_forks: false
  include_archived: false
  selected:
    - github:github.com:654321
    - gitlab:gitlab.com:234567
    - gitlab:gitlab.example.org:8443:345678
  excluded:
    - gitlab.example.org:8443/example-orchard/example-lab/temporary-demo
discovery:
  request_delay_ms: 1000
manual_git_urls:
  - alias: example-external
    url: https://code.example.org/team/external.git
storage:
  default_repo_gib: 2
  global_gib: 50
  overrides:
    gitlab:gitlab.example.org:8443:345678: 4
  during_transfer: finish
  min_free_gib: 2
```

## Repository selection

| Selection field | Values and default |
| --- | --- |
| `mode` | `all`, `selected` (default), or `all_except`. |
| `visibility` | Nonempty list using `public`, `internal`, and/or `private`. Default `[public, private]`. Add `internal` explicitly for GitLab internal projects. |
| `include_forks` | Boolean, default `false`. Forks have independent IDs. |
| `include_archived` | Boolean, default `false`. |
| `selected` | Selector list, default `[]`; relevant for `selected` mode. |
| `excluded` | Selector list, default `[]`; enforced in **every** mode. |

Selectors accept stable keys such as `github:github.com:123456`, `gitlab:gitlab.com:234567`, and `gitlab:gitlab.example.org:8443:345678`. Canonical addresses such as `github.com/example-orchard/demo` and `gitlab.example.org:8443/example-orchard/example-lab/demo` may include `https://` and a final `.git` suffix. Stable keys are recommended. Address selectors require a lowercase authority and match the repository path case-insensitively. Previous confirmed addresses remain associated with an existing ID so name-based selection/exclusion follows a rename. Reuse of a known address by another ID blocks discovery as an identity conflict; it does not overwrite the old backup.

Explicit exclusions always apply. Visibility, fork and archive filters apply before explicit selection can permit a repository; provider scopes also constrain the result. Thus explicitly selecting a private repository does not override `visibility: [public]`. `all` and `all_except` both include all in-scope, property-matching repositories except explicit exclusions; `selected` additionally requires a selector match. These provider filters do not infer visibility for arbitrary manual HTTPS sources.

Selection is recomputed during scheduled and manual runs. New matching repositories become eligible; excluded repositories receive no Git updates or remote repair downloads. A repository absent from a **complete** discovery has `source.discovery_status: unavailable` and receives no Git access, without claiming deletion or deleting its mirror. Its last confirmed Git transport status is preserved. Explicit filters can still exclude a known unavailable repository. A later successful rediscovery reuses the same backup.

GitHub publishes a selection only after all of its configured tokens complete discovery. GitLab does the same separately for each authority. A failed token, malformed response, incomplete pagination, network failure, or uncertain identity withholds the affected selection for that run, including remote repair. Independent GitLab authorities, GitHub, and eligible manual sources retain their own selection. Fatal persistence failures stop the service.

## API pacing and rate limits

`discovery.request_delay_ms` sets a minimum gap from one API request's completion to the next request's start within a discovery client. This applies across tokens, listing pages, and manual identity API lookups. GitHub uses one client for its run and GitLab uses one per authority. A value of `0` disables the configured gap. Pacing counts toward the discovery deadline and can make large inventories exceed it.

| Behavior | GitHub | GitLab |
|---|---|---|
| Listing pages | Up to 1,000 repository pages per token, with up to 100 repositories per page | Up to 1,000 listing pages per authority per run across tokens and endpoints, with up to 100 entries per page |
| Pagination | Offset pages for `/user/repos` | ID keyset for `/projects`, offset for groups and group projects |
| Quota and cooldown headers | `X-RateLimit-Remaining`, `X-RateLimit-Reset`, `Retry-After` | `RateLimit-Remaining`, `RateLimit-Reset`, `RateLimit-ResetTime`, `Retry-After` |
| Journal record | `data.json.discovery` | `data.json.provider_discovery["gitlab:<authority>"]` |

Both clients validate pagination against the expected HTTPS authority, endpoint, and query and construct the next request locally. GitHub accepts continuations that omit the affiliation parameter and still sends the configured affiliations on its next request. A full page without continuation headers triggers a locally constructed next-page request to verify completion. GitLab checks increasing IDs and consistent offset metadata. Both use a 4 MiB response limit, a socket timeout of up to 15 seconds, and a 120-second discovery deadline per client checked between requests and body reads. Exceeding a bound fails discovery.

HTTP 429 and rate-limit responses detected on HTTP 403 trigger a cooldown. A successful HTTP 200 with a zero remaining-quota header also sets one. `Retry-After` accepts seconds or an HTTP date. Reset headers use Unix timestamps, with GitLab also accepting an HTTP date in `RateLimit-ResetTime`. The latest applicable time wins. The fallback is 60 seconds when no usable time is available, and a zero-quota success without a usable reset uses at least that fallback. Server cooldowns have no one-day cap. GitLab persists them before reading an HTTP 200 response body and retains them across restart or temporary provider removal. GitHub saves the cooldown as part of the discovery outcome. The journal records `retry_at` as Unix epoch seconds. Discovery can resume after the stored time on a later scheduled or manual run, or when an already pending Git retry is eligible. The scheduler continues to process other eligible work during the cooldown.

### API observation and Git source status

`source.status` records Git transport availability (`unknown`, `available`, or `unavailable`), independently of API visibility. A newly discovered repository starts with `unknown`; only a successful Git remote query or completed Git operation confirms `available`. A Git access failure remains visible after successful API rediscovery. Exclusion, property filters and storage limits do not confirm Git access or advance backup check/update timestamps.

`source.discovery_status` is an optional, additive field describing presence (`available`) or absence (`unavailable`) in the last complete API discovery. `source.last_seen_at` records the last successful API observation and is updated only when the repository is observed. Discovery preserves other source fields, diagnostics and timestamps. On incomplete discovery neither the previous API observation nor Git status is republished as current; inspect `data.json.discovery` or the GitLab authority's `provider_discovery` entry for failure or staleness. None of these fields guarantees current network reachability.

No schema bump or migration is required: existing schema-1 metadata without `discovery_status` loads unchanged and gains that field at the next complete discovery. For GitLab, a missing or null API `default_branch` is stored as unknown. When supplied, it must be a string of at most 255 characters without ASCII control characters below U+0020. An unknown branch gives no evidence about Git read access or whether the project is empty. A later successful Git query establishes fresh Git availability.

### Stable paths and manual conflicts

Discovery deduplicates by provider, authority, and numeric ID. For example, GitLab key `gitlab:gitlab.example.org:8443:345678` uses `gitlab/gitlab.example.org~8443/345678/`; `gitlab:gitlab.com:234567` uses `gitlab/gitlab.com/234567/`. GitHub continues to use `github/github.com/<repository-id>/`. A rename or namespace transfer on the same instance retains the same ID path. Repository names, current URLs, and historical address aliases are stored in `metadaten.json`. Name-based selectors continue to match confirmed historical aliases. Reuse of an alias by another ID or contradictory observations for one ID suspend discovery for the affected instance. Git fetch uses the current discovered URL explicitly. The mirror's original Git remote configuration can still contain the original credential-free URL.

A current or historical provider address matching a manual source, or an API-resolved matching ID, excludes the automatic source with `manual_identity_conflict`. Existing manual mirrors retain their paths and update behavior. A new manual source is also withheld if its address already belongs to an existing provider mirror (`PROVIDER_IDENTITY_CONFLICT`). RepoHarbor does not adopt or migrate manual mirrors. Unknown historical manual aliases cannot be resolved reliably.

When discovery is enabled for the matching authority, RepoHarbor resolves manual addresses using that authority's API credentials. GitLab performs these lookups after all configured entries for that authority complete discovery. It uses distinct credential names in configuration order, advances only on HTTP 404, and stops at the first successful lookup. Exhausting those tokens yields `GITLAB_MANUAL_IDENTITY_UNRESOLVED`. Other errors (including 401, 403, rate limits, redirects, invalid API data, and network failures) stop the lookup and withhold the affected automatic selection. GitHub likewise pauses its automatic selection if a manual address cannot be resolved safely. During a discovery failure or cooldown, manual sources on other authorities and established manual mirrors on the affected authority can still run if otherwise eligible. An established manual mirror requires a local mirror directory and a recorded successful update. A new or unproven same-authority mirror waits for successful discovery in both full and targeted runs.

Configuration and persisted state still use schema version `1`. GitLab adds provider metadata and the optional `provider_discovery` journal map. Existing manual and GitHub state keeps its identity and paths. Check state compatibility before rolling back to a version without GitLab support. Storage overrides use manual aliases or stable provider keys from repository metadata.

## Scheduling and storage behavior

Cron uses five fields (`minute hour day-of-month month day-of-week`) and accepts stars, values, lists, ranges, and positive steps. Sunday is `0` or `7`. Following Vixie cron, if either day field begins with `*` (including `*/1`, `*/2`, or a list beginning with a wildcard), day-of-month and day-of-week must **both** match. Thus `0 3 */1 * 1` runs on Mondays, like `0 3 * * 1`; `0 3 1 * */1` runs on the first of the month, like `0 3 1 * *`. Larger steps still filter their day field: `0 3 */2 * 1` runs only on Mondays with an odd month day. If neither day field begins with `*`, they use traditional **OR** semantics, including explicit full ranges such as `1-31` or `0-7`. An IANA timezone determines local firing times. Missing DST minutes are skipped. A repeated local minute fires at most once. Missed execution times during downtime are not replayed, and backup or integrity ticks that overlap active work are skipped without a queue.

Budgets use binary GiB (`1 GiB = 1,073,741,824 bytes`); fractional inputs are truncated to whole bytes. `0` means no new transfer may start. An override replaces the default per-repository budget; it does not add to it. Global usage includes **all** backup-root data, including excluded sources, journals, metadata, partials, and quarantines.

With the default `during_transfer: finish`, size budgets are evaluated before/after operations, but a running transfer is allowed to exceed them. The physical reserve may interrupt the transfer. With `abort`, configured size budgets are also checked during transfer. Stop signals are polled approximately every 100 milliseconds; capacity is checked at most once every five seconds during Git operations. All measurements are point-in-time, **not hard quotas**, and eligibility is checked afresh during subsequent backup runs. No existing backup is automatically deleted to free space.

## Periodic integrity checks

To enable the check, merge these settings into the existing service YAML.

```yaml
features:
  integrity_check: true
integrity:
  schedule: "0 4 * * 0"
  include_excluded: true
```

The check is disabled by default. The two fields under `integrity` are optional and the example shows their defaults. Cron expressions, unknown keys and non-boolean flag values are validated even when verification is disabled. The check cron is independent of the backup cron and uses the same IANA `TZ` timezone, Vixie day matching, and daylight-saving behavior. Startup waits for a future cron tick. The existing recovery checks still run when needed with this feature disabled.

RepoHarbor checks existing, complete mirrors from its local catalog one at a time, including manual HTTPS, GitHub and GitLab sources. This works for retained mirrors when an upstream repository becomes unavailable or a source is removed from configuration. With `include_excluded: false`, it skips excluded repositories and applies current local selection filters for discovered repositories. Partial first clones and pending quarantines are skipped. The check uses structure and pack checks plus `git fsck --full`. It reads local Git data without discovery, remote requests or changes to objects and refs. A clean result establishes local Git integrity within the scope of the check. Upstream reachability and previously deleted content are outside that scope.

Backups, retries, and integrity runs share a lock. Overlapping scheduled runs are skipped and counted for the corresponding schedule. The scheduler advances to the next cron opportunity. A manual backup can return `BUSY` (exit `6`) during a check or retry. See [integrity checks and bounded repair](SECURITY.md#integrity-checks-and-bounded-repair) for how a later eligible backup can handle recorded damage.

## Automatic backup retries

Automatic retries are disabled by default. Enable them by setting `features.auto_retry: true`. The optional `retry` mapping is validated even while the feature is disabled. Booleans, strings, and fractional numbers are rejected for integer settings.

A scheduled or manual backup failure can schedule one retry for the affected repository. Eligible errors include identified HTTPS connection and transfer failures, HTTP 500, 502, 503 and 504, and classified Git timeouts. Retryability requires this specific evidence. Generic `GIT_FAILED`, `source.status: unavailable`, unknown diagnostics, credentials, permissions, missing repositories, TLS certificate failures, and intentional interruption cannot schedule a retry. The manual CLI returns the original attempt's result. Automatic retries can run with `features.manual_run: false`.

The delay doubles after each failed retry until it reaches `retry.max_delay_seconds`. With defaults, retries are due after 5 minutes, another 10 minutes, and another 20 minutes. Subsequent scheduled or manual failures leave an already pending deadline in place. A success from any trigger, including a successful no-change check, clears the pending deadline and resets the cycle counter while preserving the backup and error history. An exhausted cycle remains exhausted until a successful backup.

Retries run individually when the shared execution lock is free, after backup and integrity cron ticks have been evaluated. An active operation keeps the lock until it completes. A cron tick that arrives during a retry follows the existing overlap rule, and a manual request during a retry returns exit `6`. Retry times are independent of cron and never backfill skipped cron ticks.

Before remote Git starts, RepoHarbor rechecks selection, storage and recovery safeguards. Provider repositories and new or unproven manual sources on a configured provider authority require fresh, complete discovery. After a discovery failure, an established manual source can proceed when the backup engine recognizes the configured identity, a successful update is on record, the local mirror passes an offline integrity check, and no conflict or protection rule blocks it. An active provider cooldown also applies to these retries.

A pending Git retry can be deferred after an identified temporary API failure such as a socket timeout, a connection loss, temporary DNS resolution failure, or HTTP 500, 502, 503, or 504. Authentication, permissions, certificate errors, malformed API data, incomplete pagination, and ambiguous failures block work that requires discovery. A discovery check that triggers a rate limit can also defer a retry. Existing cooldowns are honored before discovery or Git starts and can extend the next check beyond the configured backoff cap.

A repository can accumulate up to three discovery deferrals per Git retry cycle. The deferral waits start at `retry.initial_delay_seconds` and double up to `retry.max_delay_seconds`. With defaults, they are 5, 10, and 20 minutes. Waiting for an already active cooldown leaves the deferral counter alone. A discovery check that still requires a deferral after the budget is used blocks the retry with `DISCOVERY_LIMIT_REACHED`. Permanent discovery failures use `DISCOVERY_FAILED` when discovery is required. A successful ordinary backup clears either block and resets the cycle. Discovery deferrals and their deadlines survive restarts and leave the Git attempt count unchanged. Other authorities and independent manual sources can proceed.

The optional `retry` object in `metadaten.json` follows schema 1. Older metadata requires no migration. The existing operation journal records `trigger: retry` for retry clones and fetches. After local startup recovery, overdue work is considered one repository at a time. A durably claimed attempt whose outcome cannot be proven after interruption remains counted and blocked. A proven Git completion is applied once. Disabling the feature pauses pending retries. A later successful ordinary backup can still clear a saved retry state.

`status` reports `retry.enabled` and the global `retry.pending` count. The count is unknown when enabled and part of the catalog is unreadable. `repositories list` and `repositories show` report the per-repository retry status, whether it is pending, the current cycle count, configured limit, next time, last attempt time and result, safe reason and error codes, and whether the Git limit has been reached. Discovery deferrals also expose `discovery_deferred`, `next_discovery_check_at`, `discovery_deferrals`, and `max_discovery_deferrals`. `block_reason` distinguishes a temporary discovery wait, a provider cooldown, and terminal discovery blocks. Text and JSON expose the same fields. Timestamps in the CLI are UTC.

## Extended logging

Enable structured events independently of the other optional features. Leave file output disabled for console-only operation without a log mount.

```yaml
features:
  extended_logging: true
logging:
  level: INFO
  format: text
  file:
    enabled: false
    path: /logs/repoharbor.log
    max_size_mib: 10
    backup_count: 5
```

The service writes events to stderr, where Docker captures them. Application output from commands such as `status --json` remains on stdout. With the feature disabled, the service uses its original console messages. The optional mapping is validated in both modes. Unknown keys, incorrect types, booleans for integer settings, and file output without extended logging are configuration errors. `config validate` checks settings and credentials. The service checks the actual log mount and file permissions at startup, and offline restore checks them after taking its normal backup lock.

Every event carries a UTC ISO 8601 timestamp, a level, and a stable event name. Where available, it also carries a repository alias or stable ID, a run ID, a journal operation ID, and a monotonic duration in milliseconds. For example, with JSON output a single line can read

```json
{"timestamp":"2026-01-01T12:00:00.000Z","level":"INFO","event":"backup.completed","repository":"example-demo","run_id":"0e511d66e2be4bea98b9b6af39a5512a","duration_ms":1250,"result":"success"}
```

Event namespaces cover `service.*`, `backup.*`, `discovery.*`, `retry.*`, `integrity.*`, `recovery.*`, `repair.*`, and `restore.*`. Some per-mirror details, such as healthy integrity results, use `DEBUG`. Treat timestamps and IDs as operational metadata. Confirm backup and recovery results through the status CLI and schema-1 state files.

File paths use slash-separated components of up to 128 ASCII characters, starting with a letter, digit, underscore, or hyphen. The remaining characters may also include dots. The full path is limited to 1,024 characters. Reserve the selected file and its numbered suffixes for RepoHarbor. The service appends to the existing active file and rotates it before an entry would push it over the size limit. Rotation shifts archives with `.1` as the newest. Reducing the archive count removes surplus managed archives at startup. Previously larger files age out through rotation. Choose a fresh filename when changing formats if a parser needs an entire file to use a single format. [Bind-mount setup and retention](../deploy/README.md#configure-log-retention)

## Web dashboard

The dashboard uses the service's current configuration, persisted backup data, and structured events. It keeps up to 250 recent events in process memory independently of extended file logging. Restarting the service clears that history. The dashboard has no log-file browser, historical storage charts, or offline-restore controls. Storage values and measurement dates can be absent until a measurement is recorded.

Set `features.web_ui: true` and supply `web.origin` to enable the listener. The dashboard reads these absolute container paths at startup.

| Environment variable | Default path | File |
|---|---|---|
| `REPOHARBOR_WEB_PASSWORD_FILE` | `/run/secrets/repoharbor-web-password` | A private, regular file with a generated URL-safe administrator password of 43–128 characters. |
| `REPOHARBOR_WEB_CERT_FILE` | `/run/secrets/repoharbor-web-cert` | TLS certificate chain in PEM format. |
| `REPOHARBOR_WEB_KEY_FILE` | `/run/secrets/repoharbor-web-key` | A private, regular PEM key without a passphrase. |

The service UID needs read access. The password and private key must have no group or other permission bits and must not be symlinks. Mount them read-only and restart to apply changes. Keep the existing password file format. The dashboard requires a Python/OpenSSL build with scrypt support and derives a new in-memory verifier at startup. If the service cannot load the files, initialise scrypt, or bind the HTTPS listener, the dashboard remains unavailable and the backup service continues. The CLI `config validate` checks the YAML and Git credential settings. Listener startup verifies the web files, scrypt support, and bind address.

The optional browser backup controls require the independent `features.manual_run` flag. All settings displayed in the browser remain read-only. See the [deployment guide](../deploy/README.md#enable-the-web-dashboard).

## CLI commands and exit codes

Use the service configuration for status and backup commands. For container use, run them as the service UID with [Docker or Compose exec](../deploy/README.md#inspect-status-and-run-manual-backups).

```sh
repoharbor status
repoharbor status --json
repoharbor repositories list
repoharbor repositories list --json
repoharbor repositories show example-demo
repoharbor repositories show example-demo --json
repoharbor repositories show gitlab:gitlab.com:234567 --json
repoharbor backup run --all
repoharbor backup run --repository example-demo
repoharbor backup run --repository gitlab:gitlab.com:234567
```

`show` and `--repository` accept a configured manual alias or a stable provider key from `repositories list`. Addresses and URLs used in selection configuration are invalid CLI selectors. `backup run` requires exactly one of `--all` and `--repository`. It requires `features.manual_run: true` and a reachable service. The flag leaves the cron schedule in place. Scheduled and manual backups, automatic retries, and periodic integrity checks share the run lock. When manual backups are enabled, a current operation causes a new manual request to return exit `6`.

The inspection commands are read-only and work with manual backups disabled. In text output, missing values are shown as `unknown`; JSON uses `null`. A reachable service only confirms that the control socket answered. The global view includes the most recent started and completed runs, the last successful full run, the next cron time in the configured timezone, provider discovery and cooldowns, and the last recorded storage measurement. A single-repository success updates that repository's metadata without advancing the last successful full-run timestamp.

The repository list shows identity, selection, Git source availability, backup status, last check and update, recorded size, and the last error code. Details include the selection reason, API observation, error counters and recent events, integrity and repair state, storage limit, and the relative mirror path. `repositories.with_errors` counts a repository once if it has consecutive errors, a failed/interrupted backup, a corrupt or suspect mirror, a running, paused, or failed repair, pending quarantine, a manual-attention marker, or a last periodic result of `uncheckable`. An exclusion alone contributes no error. Unreadable metadata is counted separately and can make the included, excluded, and error totals unknown.

The status commands never start a check or repair. For `status --json`, all integrity fields in the table belong to the `periodic_integrity` object.

| Command | JSON fields |
|---|---|
| `status --json` | `enabled` and `next_scheduled_at` (null when disabled). |
| `status --json` | `state` (`idle`, `running`, `completed`, or `interrupted`) and `current_repository` (the catalog ID under inspection, or null). |
| `status --json` | `last_started_at`, `last_completed_at`, and `last_result` (`healthy`, `issues`, or `no_candidates` after a completed run). The counters `checked`, `healthy`, `corrupt`, `skipped`, and `uncheckable` track the most recent run or its progress. `checked` counts the `healthy` and `corrupt` findings. `skipped_overlaps` and `last_overlap_at` record schedule collisions. |
| `repositories list --json` and `repositories show … --json` | `integrity_status` is the current metadata status. `periodic_integrity` contains `last_started_at`, `last_completed_at`, `last_result`, `damaged_at`, and `error_code` for that repository. |
| `repositories show … --json` | The `integrity` object includes `status`, `last_checked_at`, `last_check_kind` (`periodic`, `recovery`, or `repair` when known), and repair details. The top-level `last_checked_at` is a backup timestamp. |

A repository's last periodic result is `healthy` after a complete clean probe, `corrupt` when Git damage is detected, or `uncheckable` when a reliable result is unavailable, for example with a missing established mirror, unsupported layout, filesystem error, or Git timeout. Harmless dangling objects do not constitute corruption. A run interrupted by service shutdown has state `interrupted`. An unfinished repository check leaves its previous completion and result intact. A clean periodic finding can coexist with a blocked current status while a journal or another safety restriction remains unresolved.

| Exit | Meaning |
|---:|---|
| `0` | Inspection, backup, restore, or bundle export completed successfully. An up-to-date check is a success. |
| `1` | Configuration, credential validation, startup, or filesystem error reported by the CLI. |
| `2` | Invalid arguments or selector. |
| `3` | Unknown repository in the catalog. |
| `4` | The requested optional feature is disabled. |
| `5` | Service unreachable or connection lost. The client cannot confirm the backup result. |
| `6` | A backup, retry, or integrity check is already in progress, or the backup-root lock is held for another operation. |
| `7` | No eligible backup target or the target is excluded. |
| `8` | Discovery failed or is in cooldown. A full run may have completed some unaffected backups. |
| `9` | Backup failed, was interrupted, or could not complete, for example because of a storage or recovery block. |
| `10` | State is missing, invalid, or incomplete. Status and list may still return partial output. |
| `11` | Restore or bundle export failed, for example because the local state or mirror is invalid, the mirror has no exportable ref or valid detached HEAD, Git failed, or the operation was interrupted. |
| `12` | Restore filesystem or destination operation failed, for example because the target exists or a path operation or write failed. |

Use stderr for the CLI diagnostic and the read-only views for stored backup or discovery diagnostics. Stored error events expose codes and timestamps. The raw Git/API responses and credentials are kept out of that output.

## Offline restore and bundle export

Run the assistant with the service stopped and `features.restore_helper: true`. It opens the existing backup root read-only and requires the exclusive backup-root lock, a valid schema-1 journal, matching repository metadata, and an intact bare mirror. The configuration must specify a supported `REPOHARBOR_TOKEN_SOURCE`. The restore process needs neither credential values nor provider access. The [deployment guide](../deploy/README.md#8-offline-restoration-and-acceptance) gives a complete minimal Docker configuration.

```sh
repoharbor restore clone --repository example-demo --destination /exports/restored-demo
repoharbor restore clone --repository example-demo --destination /exports/restored-demo --branch development
repoharbor restore bundle --repository example-demo --output /exports/example-demo.bundle
```

Use either clone command for that destination. `--repository` accepts a manual alias such as `example-demo`, a GitHub key such as `github:github.com:654321`, or a GitLab key such as `gitlab:gitlab.com:234567`. A Self-Managed authority can include its port, for example `gitlab:gitlab.example.org:8443:345678`. The assistant looks up the on-disk catalog even if the source is excluded, absent from current configuration, or unreachable. URLs and selection addresses are invalid selectors.

`restore clone` creates an independent working repository with the mirror's reachable Git history and available refs, including branches and tags. It checks out the mirror's current branch or detached commit unless `--branch` selects another existing branch. If the branch pointed to by `HEAD` is missing, the assistant chooses the first available local branch in lexicographic order. With no branch to check out and no detached commit, the working tree stays empty. An empty mirror can be cloned as an empty repository.

`restore bundle` exports refs and their necessary objects, verifies their identities, and imports them into an empty repository to check that the bundle is self-contained. A bundle file holds Git objects and refs. Use Git to create a working tree from that file. A bundle requires at least one exportable ref or a valid detached `HEAD`. See [bundle import examples](../deploy/README.md#export-and-use-a-git-bundle).

Both commands require a new output path with an existing writable parent outside the backup root and the configured secrets directory. They reject corrupt Git objects, absent or invalid required metadata, unfinished operations for that mirror, access through unsafe paths, and existing destinations. Use the [direct Git procedure](../deploy/README.md#recover-directly-with-git) when intact mirror data is available but the RepoHarbor catalog is incomplete. The [security guide](SECURITY.md#offline-restore-safety) covers interruption and filesystem errors.

## Runtime state and recovery

- `data.json` is the central, durable run/operation journal and scheduler state. Its `discovery` section records GitHub discovery, and `provider_discovery` records GitLab discovery by authority. These records track running, completed, or failed discovery and any cooldown separately from the Git operation journal. The optional `integrity` section records the periodic run state, counters, and overlap history.
- Each source directory's `metadaten.json` contains repository identity and selection, source availability, backup status, timestamps, storage measurements, errors, and integrity data. It is **outside** the Git mirror. Its optional `integrity.periodic` record includes the last start, completed finding and error code, the last recorded damage time, and `repair_pending`.
- Each source directory's `repository.git/` contains the valid bare mirror when available. A partial first clone can exist as `repository.git.partial/`; an interrupted partial may be preserved in `quarantine/initial-clone/`.

| State | Meaning |
|---|---|
| `updated` / `up_to_date` | Successful changed mirror or successful no-op remote check. |
| `global_limit_paused`, `repo_limit_reached`, `FREE_SPACE_RESERVE` | A new operation was withheld by a capacity condition. |
| `repair_paused` + `repair_deferred` | Repair deferred for storage or discovery, without consuming a failed attempt. A later eligible run reevaluates it. |
| `pending` | Backup work is pending. This can also follow a clean periodic check that cleared an obsolete repair status. |
| `repair_failed` | Genuine exhausted repair attempts or an unrecoverable/unavailable repair condition. |
| `interrupted` | Operation was interrupted or not proven complete. |
| `manual_attention_required` | Data-preservation conflict, unknown partial, or occupied/missing quarantine requiring explicit operator action. |

`last_attempt_at`, `last_checked_at`, and `last_updated_at` have different meanings; skipped or failed operations must not create a false successful check or update. Repairs do not themselves imply a new Git reference update. An exhausted genuine repair does not become `repair_paused` simply because free space is also low.

For controlled recovery and operator actions, see [SECURITY.md](SECURITY.md). The [deployment guide](../deploy/README.md#8-offline-restoration-and-acceptance) covers the assistant and direct Git recovery from an intact mirror. Both restore workflows and bundles use the Git data in the mirror. Git LFS payloads, separate submodule repositories, issues, pull/merge requests, hosting metadata, and unpushed local changes require separate backups. See the [changelog](../Changelog.md) for version history. Production NAS acceptance remains an operator task.
