# Configuration reference (schema 1)

RepoHarbor `0.3.0` supports discovery on GitHub.com, GitLab.com, and compatible Self-Managed GitLab instances, alongside manual HTTPS Git remotes. GitHub Enterprise discovery, SSH, and the web dashboard are outside this version's scope.

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
| `features` | Recognizes `web_ui`, `integrity_check`, `auto_retry`, `manual_run`, `restore_helper`, and `extended_logging`; every specified value must be `false` in this build. |

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
  web_ui: false
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

Identical normalized URLs are deduplicated using the **first** alias, provided credentials do not conflict. Aliases identify persistent directories under `manual/<alias>`. Reassigning an existing alias to a different URL, or its URL to another alias, fails closed. Removing a source from the configuration marks the existing repository as excluded; it does **not** delete the mirror. Restoring the same alias/URL makes it eligible again on the next scheduled run.

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

Selection is recomputed at each future scheduled run. New matching repositories become eligible; excluded repositories receive no Git updates or remote repair downloads. A repository absent from a **complete** discovery has `source.discovery_status: unavailable` and receives no Git access, without claiming deletion or deleting its mirror. Its last confirmed Git transport status is preserved. Explicit filters can still exclude a known unavailable repository. A later successful rediscovery reuses the same backup.

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

HTTP 429 and rate-limit responses detected on HTTP 403 trigger a cooldown. A successful HTTP 200 with a zero remaining-quota header also sets one. `Retry-After` accepts seconds or an HTTP date. Reset headers use Unix timestamps, with GitLab also accepting an HTTP date in `RateLimit-ResetTime`. The latest applicable time wins. The fallback is 60 seconds when no usable time is available, and a zero-quota success without a usable reset uses at least that fallback. Server cooldowns have no one-day cap. GitLab persists them before reading an HTTP 200 response body and retains them across restart or temporary provider removal. GitHub saves the cooldown as part of the discovery outcome. The journal records `retry_at` as Unix epoch seconds. Both resume on a future scheduled tick after the stored time, without waiting out the cooldown in the scheduler or automatically retrying in the same run.

### API observation and Git source status

`source.status` records Git transport availability (`unknown`, `available`, or `unavailable`), independently of API visibility. A newly discovered repository starts with `unknown`; only a successful Git remote query or completed Git operation confirms `available`. A Git access failure remains visible after successful API rediscovery. Exclusion, property filters and storage limits do not confirm Git access or advance backup check/update timestamps.

`source.discovery_status` is an optional, additive field describing presence (`available`) or absence (`unavailable`) in the last complete API discovery. `source.last_seen_at` records the last successful API observation and is updated only when the repository is observed. Discovery preserves other source fields, diagnostics and timestamps. On incomplete discovery neither the previous API observation nor Git status is republished as current; inspect `data.json.discovery` or the GitLab authority's `provider_discovery` entry for failure or staleness. None of these fields guarantees current network reachability.

No schema bump or migration is required: existing schema-1 metadata without `discovery_status` loads unchanged and gains that field at the next complete discovery. For GitLab, a missing or null API `default_branch` is stored as unknown. When supplied, it must be a string of at most 255 characters without ASCII control characters below U+0020. An unknown branch gives no evidence about Git read access or whether the project is empty. A later successful Git query establishes fresh Git availability.

### Stable paths and manual conflicts

Discovery deduplicates by provider, authority, and numeric ID. For example, GitLab key `gitlab:gitlab.example.org:8443:345678` uses `gitlab/gitlab.example.org~8443/345678/`; `gitlab:gitlab.com:234567` uses `gitlab/gitlab.com/234567/`. GitHub continues to use `github/github.com/<repository-id>/`. A rename or namespace transfer on the same instance retains the same ID path. Repository names, current URLs, and historical address aliases are stored in `metadaten.json`. Name-based selectors continue to match confirmed historical aliases. Reuse of an alias by another ID or contradictory observations for one ID suspend discovery for the affected instance. Git fetch uses the current discovered URL explicitly. The mirror's original Git remote configuration can still contain the original credential-free URL.

A current or historical provider address matching a manual source, or an API-resolved matching ID, excludes the automatic source with `manual_identity_conflict`. Existing manual mirrors retain their paths and update behavior. A new manual source is also withheld if its address already belongs to an existing provider mirror (`PROVIDER_IDENTITY_CONFLICT`). RepoHarbor does not adopt or migrate manual mirrors. Unknown historical manual aliases cannot be resolved reliably.

When discovery is enabled for the matching authority, RepoHarbor resolves manual addresses using that authority's API credentials. GitLab performs these lookups after all configured entries for that authority complete discovery. It uses distinct credential names in configuration order, advances only on HTTP 404, and stops at the first successful lookup. Exhausting those tokens yields `GITLAB_MANUAL_IDENTITY_UNRESOLVED`. Other errors (including 401, 403, rate limits, redirects, invalid API data, and network failures) stop the lookup and withhold the affected automatic selection. GitHub likewise pauses its automatic selection if a manual address cannot be resolved safely. Manual processing remains independent.

Configuration and persisted state still use schema version `1`. GitLab adds provider metadata and the optional `provider_discovery` journal map. Existing manual and GitHub state keeps its identity and paths. Check state compatibility before rolling back to a version without GitLab support. Storage overrides use manual aliases or stable provider keys from repository metadata.

## Scheduling and storage behavior

Cron uses five fields (`minute hour day-of-month month day-of-week`) and accepts stars, values, lists, ranges, and positive steps. Sunday is `0` or `7`. Following Vixie cron, if either day field begins with `*` (including `*/1`, `*/2`, or a list beginning with a wildcard), day-of-month and day-of-week must **both** match. Thus `0 3 */1 * 1` runs on Mondays, like `0 3 * * 1`; `0 3 1 * */1` runs on the first of the month, like `0 3 1 * *`. Larger steps still filter their day field: `0 3 */2 * 1` runs only on Mondays with an odd month day. If neither day field begins with `*`, they use traditional **OR** semantics, including explicit full ranges such as `1-31` or `0-7`. An IANA timezone determines local firing times. Missing DST minutes are skipped; a repeated local minute fires at most once. Missed execution times during downtime are not replayed, and ticks during an active backup are skipped without a queue.

Budgets use binary GiB (`1 GiB = 1,073,741,824 bytes`); fractional inputs are truncated to whole bytes. `0` means no new transfer may start. An override replaces the default per-repository budget; it does not add to it. Global usage includes **all** backup-root data, including excluded sources, journals, metadata, partials, and quarantines.

With the default `during_transfer: finish`, size budgets are evaluated before/after operations, but a running transfer is allowed to exceed them. The physical reserve may interrupt the transfer. With `abort`, configured size budgets are also checked during transfer. Stop signals are polled approximately every 100 milliseconds; capacity is checked at most once every five seconds during Git operations. All measurements are point-in-time, **not hard quotas**, and eligibility is checked afresh on future scheduled runs. No existing backup is automatically deleted to free space.

## Runtime state and recovery

- `data.json` is the central, durable run/operation journal and scheduler state. Its `discovery` section records GitHub discovery, and `provider_discovery` records GitLab discovery by authority. These records track running, completed, or failed discovery and any cooldown separately from the Git operation journal.
- Each source directory's `metadaten.json` contains repository identity and selection, source availability, backup status, timestamps, storage measurements, errors, and integrity data. It is **outside** the Git mirror.
- Each source directory's `repository.git/` contains the valid bare mirror when available. A partial first clone can exist as `repository.git.partial/`; an interrupted partial may be preserved in `quarantine/initial-clone/`.

| State | Meaning |
|---|---|
| `updated` / `up_to_date` | Successful changed mirror or successful no-op remote check. |
| `global_limit_paused`, `repo_limit_reached`, `FREE_SPACE_RESERVE` | A new operation was withheld by a capacity condition. |
| `repair_paused` + `repair_deferred` | Repair deferred for storage, without consuming a failed attempt; reevaluated at the next regular run. |
| `repair_failed` | Genuine exhausted repair attempts or an unrecoverable/unavailable repair condition. |
| `interrupted` | Operation was interrupted or not proven complete. |
| `manual_attention_required` | Data-preservation conflict, unknown partial, or occupied/missing quarantine requiring explicit operator action. |

`last_attempt_at`, `last_checked_at`, and `last_updated_at` have different meanings; skipped or failed operations must not create a false successful check or update. Repairs do not themselves imply a new Git reference update. An exhausted genuine repair does not become `repair_paused` simply because free space is also low.

For controlled recovery and operator actions, read [SECURITY.md](SECURITY.md). An offline restore from an intact mirror is performed with Git:

```sh
git clone --no-hardlinks /backups/manual/example-demo/repository.git ./restored-example
```

This restores regular Git objects and refs, not LFS payloads, separate submodules, issues, or hosting metadata. See the [deployment guide](../deploy/README.md) and [changelog](../Changelog.md). Production NAS acceptance remains an operator task.
