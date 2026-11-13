# Configuration reference (schema 1)

RepoHarbor `0.1.0` is an **unreleased development build**. It supports GitHub.com REST discovery and manually configured HTTPS Git remotes. GitLab, GitHub Enterprise, SSH, dashboard access, and additional optional features are not yet implemented.

The canonical service configuration is a YAML document supplied through **`REPOHARBOR_CONFIG`**. The image also accepts `TZ`, `REPOHARBOR_TOKEN_SOURCE`, `REPOHARBOR_BACKUP_ROOT`, and `REPOHARBOR_SECRETS_DIR`. Unknown YAML keys are rejected, rather than silently ignored. The schema requires the integer `schema_version: 1`.

For installation, host mounts, and Portainer instructions, see the [deployment guide](../deploy/README.md). For the supported source and storage behavior, refer to the actual [`deploy/compose.example.yaml`](../deploy/compose.example.yaml) and application source.

## Configuration schema

| Key | Supported value and default |
|---|---|
| `schema_version` | Required integer `1`. Other versions are rejected. |
| `schedule` | Five-field cron expression; default `0 3 * * *`. |
| `credentials` | List of unique lowercase credential identifiers; default `[]`. |
| `providers` | List of GitHub.com provider entries; default `[]`. See below. |
| `selection` | GitHub selection/filter settings; safe default `selected` with no selected repositories. |
| `manual_git_urls` | List of manual HTTPS Git sources; default `[]`. Each requires `alias` and `url`; `web_url` and `credential` are optional. |
| `storage.global_gib` | Soft global size budget in GiB, or `null` for unlimited (default). |
| `storage.default_repo_gib` | Soft budget per repository in GiB, or `null` for unlimited (default). |
| `storage.overrides` | Mapping from configured manual aliases or stable keys such as `github:github.com:123456` to replacement per-repository GiB budgets; default `{}`. |
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

The current manual alias is not a provider ID. GitHub discovery uses separate stable provider identities, as described below. Existing manual paths and identities are retained; they are never automatically migrated to GitHub IDs.

## GitHub discovery and selection

A complete example using invented accounts, repositories, IDs and credential names:

```yaml
schema_version: 1
schedule: "0 3 * * *"
credentials: [example_personal, example_team]
providers:
  - id: example_personal_access
    type: github
    host: github.com
    credential: example_personal
    include_owned: true
    include_organizations: false
    include_collaborators: true
  - id: example_team_access
    type: github
    host: github.com
    credential: example_team
    include_owned: false
    include_organizations: true
    include_collaborators: false
selection:
  mode: all_except
  visibility: [public, private]
  include_forks: false
  include_archived: false
  selected: []
  excluded:
    - github.com/example-orchard/temporary-demo
    - github:github.com:123456
manual_git_urls:
  - alias: example-external
    url: https://code.example.org/team/external.git
storage:
  default_repo_gib: 2
  global_gib: 50
  overrides:
    github:github.com:654321: 4
  during_transfer: finish
  min_free_gib: 2
```

Provider `id` must be a unique lowercase identifier; `credential` references the existing top-level credential list. `type` must be `github`; `host` defaults to `github.com` and other hosts are rejected. Scope flags default to `true` and require actual YAML booleans. Providers are processed in configured order. The first in-scope observation determines the Git credential; credentials and repository identities are never interchangeable.

Each token queries the authenticated `/user` and paginated `/user/repos` endpoints with owner, collaborator and organization-member affiliations. Classic and fine-grained PATs work within their actual API and Git permissions. Fine-grained tokens discover only repositories granted to that token; they do not automatically gain access to new repositories. Each token must support both metadata discovery and Git contents read access for its chosen repositories. Organization approval/SSO requirements may restrict access. Automatic Git credential fallback is not implemented: a Git permission failure is recorded without retrying an unrelated token.

Scopes are a union of three disjoint categories: repositories owned by the authenticated login, other repositories owned by an organization, and remaining repositories owned by another user with collaborator access. An organization-owned repository therefore follows `include_organizations`, even when access was granted through collaboration. Repository observations across providers are deduplicated by `github` + `github.com` + numeric ID. A repository is in scope when at least one configured provider observes it in scope. Contradictory observations for one ID fail the entire GitHub discovery rather than choosing an uncertain identity/property snapshot.

| Selection field | Values and default |
| --- | --- |
| `mode` | `all`, `selected` (default), or `all_except`. |
| `visibility` | Nonempty list containing `public`, `private`, or both (default). |
| `include_forks` | Boolean, default `false`. Forks have independent IDs. |
| `include_archived` | Boolean, default `false`. |
| `selected` | Selector list, default `[]`; relevant for `selected` mode. |
| `excluded` | Selector list, default `[]`; enforced in **every** mode. |

Selectors accept a stable key (`github:github.com:123456`) or a canonical address (`github.com/example-orchard/demo`); credential-free HTTPS addresses and a final `.git` suffix are also accepted. Stable keys are recommended. Address matching is case-insensitive. Previous confirmed addresses remain associated with an existing ID so name-based selection/exclusion follows a rename. Reuse of a known address by another ID blocks discovery as an identity conflict; it does not overwrite the old backup.

Explicit exclusions always apply. Visibility, fork and archive filters apply before explicit selection can permit a repository; provider scopes also constrain the result. Thus explicitly selecting a private repository does not override `visibility: [public]`. `all` and `all_except` both include all in-scope, property-matching repositories except explicit exclusions; `selected` additionally requires a selector match. These provider filters do not infer visibility for arbitrary manual HTTPS sources.

Selection is recomputed at each future scheduled run. New matching repositories become eligible; excluded repositories receive no Git updates or remote repair downloads. A repository absent from a **complete** discovery is marked unavailable and receives no Git access, without claiming deletion or deleting its mirror. Explicit filters can still exclude a known unavailable repository. A later successful rediscovery reuses the same backup.

Any failed token, incomplete pagination, malformed response, network failure or uncertain identity pauses the **entire GitHub part** of that run. No partial global selection is published and no old selection authorizes GitHub remote repair. Independent manual sources continue. A diagnostic is recorded in `data.json.discovery`; rate limits defer further discovery until the persisted retry time and a future scheduled tick. Retry-After/reset are honored with a minimum 60-second cooldown, capped at 24 hours. Other failures are retried at the next normal tick; no backfill or immediate automatic retry loop is added.

Discovery permits at most 1,000 pages per token, 100 repositories per page, 4 MiB per response, a 15-second socket timeout, and a shared 120-second discovery deadline checked between reads/requests. Exceeding a bound is an incomplete discovery, never a successful truncated catalog. Redirects and pagination links to other hosts/endpoints are rejected.

### Stable paths and manual conflicts

Discovered data uses `github/github.com/<numeric-id>/`, with name, owner, canonical URLs and address history in `metadaten.json`. Renames/transfers update those attributes and retain the directory. This deliberately avoids filesystem rename transactions and collisions. Git updates use the current discovered URL explicitly; a mirror's original Git remote configuration may still contain its original credential-free clone URL.

No manual mirror is automatically adopted or migrated. Matching a manual address, its resolved current API ID, or a retained provider address produces `manual_identity_conflict` for the automatic source. Manual API resolution is used only to block overlaps, never as proof that historical mirror contents belong to that ID. If a manual GitHub address cannot be safely resolved (including a redirect or insufficient token visibility), GitHub discovery pauses; manual processing remains independent. Unknown historical manual URL aliases cannot be proven automatically. Avoid parallel configuration of historical aliases whose identity is uncertain.

An existing manual mirror retains its alias, path and update behavior. A newly configured manual source without a mirror is also withheld when its address already belongs to an existing provider mirror; logs report `PROVIDER_IDENTITY_CONFLICT`. The existing provider mirror is preserved and its conflicting automatic assignment is blocked. Resolve such configuration conflicts explicitly; do not edit IDs or move mirrors to force adoption.

Schema version remains `1`, with additive provider metadata and discovery fields. Existing manual metadata/journals load unchanged. Older application builds do not support the new GitHub catalog or its journal references; rollback requires an operator-reviewed plan, not an automatic rewrite. No data migration is performed.

The specification's conceptual name-address storage overrides must be expressed as the stable provider key shown above (read the ID from repository metadata), while existing manual overrides keep their aliases. This is an explicit configuration adaptation; mirrors and stored IDs must not be renamed to implement it. GitLab entries from the conceptual full-product example remain unsupported in this development build.

## Scheduling and storage behavior

Cron uses five fields (`minute hour day-of-month month day-of-week`) and accepts stars, values, lists, ranges, and positive steps. Sunday is `0` or `7`. Following Vixie cron, if either day field begins with `*` (including `*/1`, `*/2`, or a list beginning with a wildcard), day-of-month and day-of-week must **both** match. Thus `0 3 */1 * 1` runs on Mondays, like `0 3 * * 1`; `0 3 1 * */1` runs on the first of the month, like `0 3 1 * *`. Larger steps still filter their day field: `0 3 */2 * 1` runs only on Mondays with an odd month day. If neither day field begins with `*`, they use traditional **OR** semantics, including explicit full ranges such as `1-31` or `0-7`. An IANA timezone determines local firing times. Missing DST minutes are skipped; a repeated local minute fires at most once. Missed execution times during downtime are not replayed, and ticks during an active backup are skipped without a queue.

Budgets use binary GiB (`1 GiB = 1,073,741,824 bytes`); fractional inputs are truncated to whole bytes. `0` means no new transfer may start. An override replaces the default per-repository budget; it does not add to it. Global usage includes **all** backup-root data, including excluded sources, journals, metadata, partials, and quarantines.

With the default `during_transfer: finish`, size budgets are evaluated before/after operations, but a running transfer is allowed to exceed them. The physical reserve may interrupt the transfer. With `abort`, configured size budgets are also checked during transfer. Stop signals are polled approximately every 100 milliseconds; capacity is checked at most once every five seconds during Git operations. All measurements are point-in-time, **not hard quotas**, and eligibility is checked afresh on future scheduled runs. No existing backup is automatically deleted to free space.

## Runtime state and recovery

- `data.json` is the central, durable run/operation journal and scheduler state. Its additive `discovery` section records running/completed/failed discovery and an optional rate-limit retry time; it does not replace an interrupted Git operation.
- `manual/<alias>/metadaten.json` or `github/github.com/<repository-id>/metadaten.json` contains repository identity and selection, source availability, backup status, timestamps, storage measurements, errors, and integrity data. It is **outside** the Git mirror.
- `manual/<alias>/repository.git/` is the valid bare mirror. A partial first clone can exist as `repository.git.partial/`; an interrupted partial may be preserved in `quarantine/initial-clone/`.

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

This restores regular Git objects and refs, not LFS payloads, separate submodules, issues, or hosting metadata. See the [deployment guide](../deploy/README.md) and [changelog](../Changelog.md). No published image or production NAS acceptance is implied.
