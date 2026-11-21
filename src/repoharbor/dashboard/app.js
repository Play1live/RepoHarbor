"use strict";

const paths = {
  overview: "M3 3h7v7H3z M14 3h7v7h-7z M3 14h7v7H3z M14 14h7v7h-7z",
  repositories: "M12 3 2.5 8l9.5 5 9.5-5L12 3z M3 12l9 5 9-5 M3 16l9 5 9-5",
  activities: "M3 12a9 9 0 1 0 3-6.7L3 8m0-5v5h5m4-1v5l3 2",
  storage: "M4 8V6c0-2 16-2 16 0v12c0 3-16 3-16 0V8m0-2c0 3 16 3 16 0M4 12c0 3 16 3 16 0",
  sources: "M7 7v10m10-10c0 5-10 3-10 10M9 5a2 2 0 1 1-4 0 2 2 0 0 1 4 0zm0 14a2 2 0 1 1-4 0 2 2 0 0 1 4 0zM19 5a2 2 0 1 1-4 0 2 2 0 0 1 4 0z",
  settings: "M10 3h4l.6 2.5 2 .9 2.3-1.3 2 3.4-1.8 1.9v2.2l1.8 1.9-2 3.4-2.3-1.3-2 .9L14 21h-4l-.6-2.5-2-.9-2.3 1.3-2-3.4 1.8-1.9v-2.2L3.1 8.5l2-3.4 2.3 1.3 2-.9z M15 12a3 3 0 1 1-6 0 3 3 0 0 1 6 0z",
  check: "m5 12 4 4L19 6", warning: "M12 3 2 20h20L12 3z M12 9v4m0 3v.2",
  error: "M12 8v5m0 3v.2M21 12a9 9 0 1 1-18 0 9 9 0 0 1 18 0z",
  pulse: "M2 12h5l3-7 4 14 3-7h5",
  retry: "M20 7a8 8 0 0 0-14-1L3 9m0-5v5h5m-4 8a8 8 0 0 0 14 1l3-3m0 5v-5h-5",
  calendar: "M4 5h16v15H4z M8 3v4m8-4v4M4 10h16",
  shield: "M12 3 4 6v6c0 4 4 7 8 9 4-2 8-5 8-9V6l-8-3zm-4 9 3 3 5-5",
  clock: "M21 12a9 9 0 1 1-18 0 9 9 0 0 1 18 0zM12 7v5l3 2",
  arrow: "m9 5 7 7-7 7", close: "m6 6 12 12M18 6 6 18", menu: "M4 6h16M4 12h16M4 18h16",
  lock: "M5 11h14v10H5z M8 11V7a4 4 0 0 1 8 0v4", play: "m7 4 14 8-14 8z",
  link: "M14 4h6v6m0-6-9 9m6 0v7H4V7h7", logs: "M5 3h10l4 4v14H5z M9 11h6m-6 4h6",
};

const pages = {
  "/": ["overview", "Overview", "Monitor backups, service health, and recent operations."],
  "/repositories": ["repositories", "Repositories", "Backup status, selection, and integrity for all known repositories."],
  "/activities": ["activities", "Activity", "Operations and results from the running service."],
  "/storage": ["storage", "Storage", "Backup usage, storage limits, and free disk space."],
  "/sources": ["sources", "Sources", "Configured Git sources, repository selection, and discovery status."],
  "/settings": ["settings", "Settings", "Current configuration and service settings."],
};
const statuses = {
  unknown: ["Unknown"], idle: ["Ready", "success", "check"],
  running: ["Running", "info", "pulse"], pending: ["Pending", "info", "clock"],
  success: ["Successful", "success", "check"], succeeded: ["Successful", "success", "check"],
  completed: ["Completed", "success", "check"], healthy: ["Healthy", "success", "check"],
  ok: ["OK", "success", "check"], passed: ["Passed", "success", "check"],
  updated: ["Backed up", "success", "check"], up_to_date: ["Up to date", "success", "check"],
  included: ["Included", "success", "check"], excluded: ["Excluded"],
  warning: ["Warning", "warning", "warning"], issues: ["Issues", "warning", "warning"],
  interrupted: ["Interrupted", "warning", "warning"], uncheckable: ["Unable to check", "warning", "warning"],
  suspect: ["Suspect", "warning", "warning"], repair_paused: ["Repair paused", "warning", "warning"],
  repairing: ["Repairing", "warning", "retry"], cooldown: ["In cooldown", "warning", "clock"],
  deferred: ["Deferred", "warning", "clock"], exhausted: ["Retries exhausted", "warning", "warning"],
  error: ["Error", "error", "error"], failed: ["Failed", "error", "error"],
  corrupt: ["Corrupt", "error", "error"], repair_failed: ["Repair failed", "error", "error"],
  unreachable: ["Unreachable", "error", "error"], busy: ["Busy", "info", "pulse"],
  available: ["Available", "success", "check"], missing: ["Missing", "warning", "warning"],
  global_limit_paused: ["Global limit reached", "warning", "warning"],
  repo_limit_reached: ["Repository limit reached", "warning", "warning"],
  FREE_SPACE_RESERVE: ["Free space below minimum", "warning", "warning"],
  disabled: ["Disabled"], not_required: ["Not required"], never: ["Never run"],
  no_candidates: ["No eligible repositories"],
  unchecked: ["Not checked"], unavailable: ["Unavailable", "warning", "warning"],
  initializing: ["Initialising", "info", "pulse"],
};
const eventTypes = {
  backup: "Backup", discovery: "Discovery", retry: "Retry",
  integrity: "Integrity", recovery: "Recovery", repair: "Repair", restore: "Restore",
};
const eventTitles = {
  "backup.check_started": "Repository check started", "backup.clone_started": "Creating mirror",
  "backup.completed": "Backup completed", "backup.run_completed": "Backup run completed",
  "backup.run_failed": "Backup run failed", "backup.run_started": "Backup run started",
  "backup.storage_blocked": "Storage limit reached", "backup.up_to_date": "Mirror already up to date",
  "backup.update_started": "Updating mirror", "discovery.provider_completed": "Source discovery completed",
  "discovery.provider_started": "Source discovery started", "discovery.rate_limited": "Rate limit reached",
  "integrity.mirror_checked": "Mirror checked", "integrity.mirror_damaged": "Corrupt mirror detected",
  "recovery.manual_attention": "Manual review required", "recovery.quarantine_preserved": "Quarantined data preserved",
  "restore.bundle_created": "Bundle created", "restore.bundle_verified": "Bundle verified",
  "retry.cooldown_deferred": "Retry deferred during cooldown",
  "retry.discovery_deferred": "Retry deferred pending discovery", "retry.scheduled": "Retry scheduled",
};
const actionTitles = {
  started: "started", completed: "completed", failed: "failed", succeeded: "succeeded",
  blocked: "blocked", deferred: "deferred", schedule_skipped: "skipped",
  source_skipped: "skipped", limit_reached: "limit reached", cooldown: "in cooldown",
  cooldown_set: "cooldown set", incomplete_operation: "incomplete operation",
  mirror_damaged: "corrupt mirror", quarantine_started: "quarantine started",
};
const providers = {github: "GitHub", gitlab: "GitLab", manual: "Manual HTTPS URLs"};
const visibility = {public: "Public", private: "Private", internal: "Internal"};
const reasons = {explicit_exclusion: "Explicit exclusion", visibility_filter: "Visibility filter",
  fork_filter: "Fork filter", archived_filter: "Archive filter", not_selected: "Not selected"};
const locale = document.documentElement.lang;
const nf = new Intl.NumberFormat(locale, {maximumFractionDigits: 1});
const unknown = "Unknown";
const main = document.querySelector("#main");
const nav = document.querySelector("#nav");
const toolbar = document.querySelector("#toolbar");
const menu = document.querySelector("#menu-toggle");
const sidebar = document.querySelector("#sidebar");
const state = {data: null, online: false, login: false, loading: true, sending: false, notice: "", event: null, problem: null};
const reposFilter = {q: "", provider: "", selection: "", backup: "", errors: "", integrity: "", sort: "name", desc: false, page: 0};
const eventsFilter = {type: "", status: "", repository: "", page: 0};
let refreshId = 0;
let signature = "";
let noticeTimer;

function el(tag, attrs = {}, ...children) {
  const element = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (value == null || value === false) continue;
    if (key.startsWith("on")) element.addEventListener(key.slice(2), value);
    else element.setAttribute(key, value === true ? "" : value);
  }
  for (const child of children.flat(Infinity)) {
    if (child != null) element.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return element;
}

function svg(tag, attrs) {
  const element = document.createElementNS("http://www.w3.org/2000/svg", tag);
  for (const [key, value] of Object.entries(attrs)) element.setAttribute(key, value);
  return element;
}

function icon(key) {
  const image = svg("svg", {viewBox: "0 0 24 24", class: "icon", fill: "none",
    stroke: "currentColor", "stroke-width": 1.65, "stroke-linejoin": "round",
    "stroke-linecap": "round", "aria-hidden": "true"});
  image.append(svg("path", {d: paths[key] || paths.error}));
  return image;
}

const route = () => location.pathname.startsWith("/repositories/") ? "/repositories" : location.pathname;
const label = key => (statuses[key] || statuses.unknown)[0];
const tone = key => statuses[key]?.[1] || "";
const count = value => value == null ? unknown : nf.format(value);
const enabled = value => value ? "Enabled" : "Disabled";
const yes = value => value ? "Yes" : "No";
const providerName = key => providers[key] || unknown;
const eventType = operation => operation.split(".")[0];
const repoLabel = repo => repo?.name || repo?.id || unknown;
const findRepo = id => state.data?.repositories.find(repo => repo.id === id);
const targetLabel = id => repoLabel(findRepo(id) || {id});
const repoPath = id => "/repositories/" + encodeURIComponent(id);
const repoListPath = () => "/repositories" + (reposFilter.errors ? "?errors=" + reposFilter.errors : "");

function eventTitle(operation) {
  const [type, action] = operation.split(".");
  return eventTitles[operation] || [eventTypes[type], actionTitles[action]].filter(Boolean).join(" ");
}

function bytes(value) {
  if (value == null || !Number.isFinite(value) || value < 0) return unknown;
  const units = ["B", "KiB", "MiB", "GiB", "TiB", "PiB"];
  const unit = value === 0 ? 0 : Math.min(units.length - 1, Math.floor(Math.log(value) / Math.log(1024)));
  return count(value / 1024 ** unit) + " " + units[unit];
}

const limit = value => value == null ? "No limit" : bytes(value);

function date(value) {
  const stamp = Date.parse(value);
  if (!Number.isFinite(stamp)) return unknown;
  const options = {dateStyle: "medium", timeStyle: "short", timeZone: state.data?.service.timezone || "UTC"};
  try { return new Intl.DateTimeFormat(locale, options).format(stamp); }
  catch (error) {
    if (!(error instanceof RangeError)) throw error;
    return new Intl.DateTimeFormat(locale, {...options, timeZone: "UTC"}).format(stamp);
  }
}

const time = value => value ? el("time", {datetime: value}, date(value)) : unknown;

function duration(value) {
  if (!Number.isFinite(value) || value < 0) return unknown;
  const seconds = Math.floor(value / 1000);
  if (seconds >= 3600) return Math.floor(seconds / 3600) + " h " + Math.floor(seconds % 3600 / 60) + " min";
  return (seconds >= 60 ? Math.floor(seconds / 60) + " min " : "") + seconds % 60 + " sec";
}

function badge(key, title = label(key), className = "") {
  const glyph = statuses[key]?.[2];
  return el("span", {class: ["badge", tone(key), className].filter(Boolean).join(" ")},
    glyph ? icon(glyph) : null, title);
}

const tile = (glyph, color = "") => el("span", {class: "tile " + color}, icon(glyph));
const notice = (message, color = "") => el("p", {class: "notice " + color}, message);
const empty = message => el("p", {class: "empty"}, message);
const body = (...content) => el("div", {class: "panel-body"}, content);
const metrics = (...cards) => el("div", {class: "metrics"}, cards);
const stack = (...sections) => el("div", {class: "stack"}, sections);
const grid = (...sections) => el("div", {class: "dashboard-grid"}, sections);

function destination(path) {
  const url = new URL(path, location.origin);
  return url.pathname + url.search;
}

function go(path) {
  history.pushState({}, "", destination(path));
  routeChanged();
}

function link(path, attrs, ...content) {
  return el("a", {...attrs, href: destination(path), onclick: event => {
    if (event.button !== 0 || event.ctrlKey || event.metaKey || event.altKey || event.shiftKey) return;
    event.preventDefault();
    go(path);
  }}, content);
}

const shortcut = (path, text) => link(path, {class: "subtle-link"}, text, icon("arrow"));

function button(title, glyph, callback, options = {}) {
  return el("button", {type: "button", onclick: callback, id: options.id,
    class: options.className || (options.only ? "icon-button" : "button"), disabled: options.disabled,
    title: options.only ? title : null, "aria-label": options.only ? title : null},
    glyph ? icon(glyph) : null, options.only ? null : title);
}

function fields(pairs) {
  return el("dl", {class: "fields"}, pairs.map(([name, value]) =>
    el("div", {}, el("dt", {}, name), el("dd", {}, value ?? unknown))));
}

function panel(title, glyph, content, action = null, kind = "") {
  return el("section", {class: "panel " + kind},
    el("header", {class: "panel-head"}, icon(glyph), el("h2", {}, title), action), content);
}

function metric(title, value, detail, glyph, color = "", path = null, isDate = false) {
  const content = [tile(glyph, color), el("div", {}, el("span", {class: "metric-label"}, title),
    el("strong", {class: "metric-value tone-" + color + (isDate ? " date" : "")}, value),
    detail ? el("small", {class: "muted"}, detail) : null)];
  return path ? link(path, {class: "metric"}, content) : el("div", {class: "metric"}, content);
}

function serviceKey() {
  if (state.loading || state.login || state.problem === "status") return "unknown";
  if (!state.online || state.data?.service.service_status === "unreachable") return "unreachable";
  return state.data?.service.run_status === "running" ? "busy" : "idle";
}

function meter(value, maximum, title, mini = false) {
  return value == null || maximum == null || maximum <= 0 ? null : el("progress",
    {value: Math.min(value, maximum), max: maximum, class: "meter" + (mini ? " mini" : ""), "aria-label": title});
}

const usage = (value, max) => value != null && max > 0 ? count(value * 100 / max) + " %" : null;
const sourceProblem = source => Boolean(source.code || ["cooldown", "failed", "error"].includes(source.status));

function distribution(repos) {
  const buckets = [["Backed up", "#6ae1a0", 0], ["Warning", "#ffc775", 0],
    ["Error", "#ff8b94", 0], ["Excluded", "#8395b0", 0], ["Unknown", "#4b6281", 0]];
  for (const repo of repos) {
    const status = tone(repo.backup_status);
    const index = repo.selection_status === "excluded" ? 3 : repo.has_error ? 2
      : status === "warning" ? 1 : status === "success" ? 0 : 4;
    buckets[index][2]++;
  }
  const image = svg("svg", {viewBox: "0 0 120 120", "aria-hidden": "true"});
  const circle = {cx: 60, cy: 60, r: 50, fill: "none", "stroke-width": 12};
  image.append(svg("circle", {...circle, stroke: "#283a54"}));
  let offset = 0;
  const legend = buckets.map(([name, color, total]) => {
    const ratio = repos.length ? 100 * total / repos.length : 0;
    if (ratio) image.append(svg("circle", {...circle, pathLength: 100, stroke: color,
      "stroke-dasharray": ratio + " " + (100 - ratio), "stroke-dashoffset": -offset}));
    offset += ratio;
    const swatch = svg("svg", {width: 10, height: 10, "aria-hidden": "true"});
    swatch.append(svg("rect", {width: 10, height: 10, rx: 2, fill: color}));
    return el("div", {}, swatch, name, el("strong", {}, count(total)));
  });
  return el("div", {class: "chart-layout"}, el("div", {class: "ring"}, image,
    el("div", {class: "ring-center"}, el("strong", {}, count(repos.length)), el("small", {class: "muted"}, "Readable entries"))),
    el("div", {class: "legend"}, legend));
}

function overview(data) {
  const {service: s, repositories, activities, sources} = data;
  const r = s.repositories;
  const p = s.periodic_integrity;
  const storage = s.storage;
  const manual = s.manual_integrity;
  const task = s.run_status === "running" && (manual?.state === "running"
    ? {...manual, trigger: "manual_integrity"} : p.state === "running"
      ? {started_at: p.started_at, repository: p.repository, trigger: "periodic"} : s.active_run || s.active_retry);
  const taskTitle = task ? ({scheduled: "Scheduled backup", manual: "Manual backup",
    periodic: "Integrity check", manual_integrity: "Manual integrity check", retry: "Automatic retry"})[task.trigger] || "Operation in progress"
    : ({busy: "Operation in progress", idle: "No active task"})[serviceKey()] || "Task status unknown";
  const recent = activities.slice(0, 5);
  return [
    metrics(
      metric("Known repositories", count(r.known), count(r.included) + " included · " + count(r.excluded) + " excluded",
        "repositories", "", "/repositories"),
      metric("Repositories with errors", count(r.with_errors), s.retry.enabled
        ? count(s.retry.pending) + (s.retry.pending === 1 ? " pending retry" : " pending retries") : "Automatic retries disabled",
        "warning", r.with_errors > 0 ? "error" : "", "/repositories?errors=yes"),
      metric("Last successful full backup", data.state_available && !s.last_successful_run_at
        ? "None recorded" : date(s.last_successful_run_at), null, "check",
        s.last_successful_run_at ? "success" : "", null, true),
      metric("Next scheduled backup", date(s.next_scheduled_at), s.timezone, "calendar", "", null, true)),
    grid(
      panel("Active task", "pulse", body(
        el("div", {class: "task"}, tile("pulse"), el("div", {}, el("h3", {}, taskTitle),
          task?.repository ? link(repoPath(task.repository), {}, targetLabel(task.repository))
            : el("small", {class: "muted"}, serviceKey() === "busy" ? "Current repository unknown" : label(serviceKey())))),
        task ? fields([["Started at", time(task.started_at)], ["Elapsed time", el("span",
          {"data-start": task.started_at}, duration(Date.now() - Date.parse(task.started_at)))]])
          : s.last_backup_state === "interrupted" ? badge("interrupted", "Last backup interrupted") : null)),
      panel("Repository status", "repositories", body(distribution(repositories))),
      panel("Recent activity", "activities", body(recent.length ? el("ul", {class: "events"},
        recent.map(event => el("li", {}, tile(statuses[event.status]?.[2] || "activities", tone(event.status)),
          el("div", {}, el("strong", {}, eventTitle(event.operation)), el("small", {class: "muted"},
            [event.repository ? targetLabel(event.repository) : event.host, label(event.status), event.code].filter(Boolean).join(" · "))),
          time(event.at)))) : empty("No events are available for the running service.")), shortcut("/activities", "View all activity")),
      panel("System overview", "shield", body(fields([
        ["Backup storage used", shortcut("/storage", bytes(storage.last_measured_bytes))],
        ["Storage status", shortcut("/storage", label(storage.status))],
        ["Integrity check", p.enabled ? badge(["interrupted", "running"].includes(p.state)
          ? p.state : p.last_result || (data.state_available && p.state === "idle" ? "never" : "unknown")) : badge("disabled")],
        ["Next integrity check", p.enabled ? time(p.next_scheduled_at) : "Disabled"],
        ["Last integrity check", p.enabled ? time(p.last_finished_run?.at || p.last_completed_at) : "Disabled"],
        ["Last backup result", s.last_backup_result ? label(s.last_backup_result) : "—"],
        ["Unavailable backup targets", s.backup_coverage ? count(s.backup_coverage.unavailable.length) : "—"],
        ["Sources with issues", shortcut("/sources?problems=yes", count(sources.filter(sourceProblem).length))],
      ])))),
  ];
}

function selectField(title, id, choices, selected, change) {
  return el("label", {}, title, el("select", {id, onchange: event => change(event.target.value)},
    choices.map(([key, text]) => el("option", {value: key, selected: key === selected}, text))));
}

function searchField(title, id, query, input) {
  return el("label", {}, title, el("input", {id, type: "search", value: query, placeholder: "Search…",
    oninput: event => input(event.target.value)}));
}

const filterBar = (...children) => el("div", {class: "panel filters"}, children);

function table(columns, rows, title) {
  return el("div", {class: "table-scroll", role: "region", tabindex: "0", "aria-label": title + " (scroll horizontally)"},
    el("table", {}, el("thead", {}, el("tr", {}, columns.map(column =>
      el("th", {scope: "col", "aria-sort": column.sort}, column.title ?? column)))), el("tbody", {}, rows)));
}

const row = (cells, selected = false) => el("tr", {class: selected ? "selected" : null}, cells.map(cell => el("td", {}, cell)));

function pagination(length, page, size, change) {
  const max = Math.max(0, Math.ceil(length / size) - 1);
  return el("div", {class: "pagination"}, el("small", {}, length
    ? count(page * size + 1) + "–" + count(Math.min(length, (page + 1) * size)) + " of " + count(length) : "0 entries"),
  max ? el("div", {},
    button("Previous", null, () => change(page - 1), {disabled: page <= 0, id: "prev", className: "button small"}),
    el("small", {}, page + 1 + " / " + (max + 1)),
    button("Next", null, () => change(page + 1), {disabled: page >= max, id: "next", className: "button small"})) : null);
}

function choices(items, field, format) {
  const values = [...new Set(items.map(item => item[field]).filter(Boolean))];
  return [["", "All"], ...values.map(key => [key, format(key)]).sort((a, b) => a[1].localeCompare(b[1], locale))];
}

function swap(id, build) {
  const root = document.getElementById(id);
  if (!root) return;
  const focus = document.activeElement?.id;
  const horizontal = root.querySelector(".table-scroll")?.scrollLeft;
  root.replaceChildren(...build());
  if (horizontal) root.querySelector(".table-scroll")?.scrollTo(horizontal, 0);
  if (focus) document.getElementById(focus)?.focus({preventScroll: true});
}

function retryBadge(retry) {
  return retry.discovery_deferred ? badge("deferred") : retry.pending ? badge("pending", "Scheduled")
    : retry.limit_reached ? badge("exhausted") : retry.status === "idle" ? "—" : badge(retry.status);
}

function repoRows(data) {
  const f = reposFilter;
  const q = f.q.trim().toLocaleLowerCase(locale);
  const repos = data.repositories.filter(repo => (
    (!q || [repo.name, repo.owner, repo.host, repo.id].join(" ").toLocaleLowerCase(locale).includes(q)) &&
    (!f.provider || f.provider === repo.provider) && (!f.selection || f.selection === repo.selection_status) &&
    (!f.backup || f.backup === repo.backup_status) && (!f.integrity || f.integrity === repo.integrity_status) &&
    (!f.errors || repo.has_error === (f.errors === "yes"))));
  const value = repo => f.sort === "name" ? repoLabel(repo) : repo[f.sort];
  repos.sort((left, right) => {
    const a = value(left);
    const b = value(right);
    if (a == null || b == null) return (a == null) - (b == null);
    return (f.desc ? -1 : 1) * (typeof a === "number" ? a - b : String(a).localeCompare(String(b), locale));
  });
  const columns = [
    ["Repository", "name"], ["Provider / Host"], ["Selection"], ["Backup status"], ["Last check"],
    ["Last update", "last_updated_at"], ["Size", "size_bytes"], ["Integrity"], ["Retry"], ["Error code"],
  ].map(([title, key]) => ({
    title: key ? button(title + (f.sort === key ? (f.desc ? " ↓" : " ↑") : ""), null, () => {
      f.desc = f.sort === key ? !f.desc : key !== "name";
      f.sort = key;
      f.page = 0;
      swap("repo-list", () => repoRows(data));
    }, {className: "sort", id: "sort-" + key}) : title,
    sort: key === f.sort ? (f.desc ? "descending" : "ascending") : null,
  }));
  const size = 12;
  f.page = Math.min(f.page, Math.max(0, Math.ceil(repos.length / size) - 1));
  const rows = repos.slice(f.page * size, (f.page + 1) * size).map(repo => row([
    el("div", {}, link(repoPath(repo.id), {id: "repo-" + repo.id, class: "table-link"}, repoLabel(repo)),
    repo.owner ? el("div", {class: "muted"}, repo.owner + "/" + repoLabel(repo)) : null),
    el("div", {}, providerName(repo.provider), el("div", {class: "muted"}, repo.host)),
    badge(repo.selection_status), badge(repo.backup_status), time(repo.last_checked_at), time(repo.last_updated_at),
    bytes(repo.size_bytes), badge(repo.integrity_status),
    data.settings.retry.enabled ? retryBadge(repo.retry) : "—",
    repo.last_error_code ? el("code", {}, repo.last_error_code) : "—",
  ]));
  return [
    repos.length ? table(columns, rows, "Repository table") : empty("No repositories match your filters."),
    pagination(repos.length, f.page, size, page => { f.page = page; swap("repo-list", () => repoRows(data)); }),
  ];
}

function externalLink(value) {
  if (!value) return null;
  try {
    const url = new URL(value);
    if (url.protocol === "https:" && !url.username && !url.password && !url.search && !url.hash) {
      return el("a", {href: url.href, rel: "noopener noreferrer", class: "button"}, icon("link"), "Open repository");
    }
  } catch (error) { if (!(error instanceof TypeError)) throw error; }
  return null;
}

function repoDetail(repo, data) {
  const close = link(repoListPath(), {class: "icon-button", title: "Close details", "aria-label": "Close details"}, icon("close"));
  if (!repo) return panel("Repository unavailable", "repositories", body(
    el("p", {}, "No readable data is available for this repository.")), close, "detail");
  const integrity = repo.integrity;
  const periodic = repo.periodic_integrity;
  const manual = repo.manual_integrity;
  const retry = repo.retry;
  const errors = repo.errors.recent.filter(Boolean).slice().sort((a, b) => String(b.at).localeCompare(String(a.at)));
  const external = externalLink(repo.web_url);
  const canRun = data.settings.manual_run && data.csrf && repo.selection_status === "included";
  const canCheck = data.settings.manual_run && data.csrf && repo.can_check_integrity;
  const section = (title, ...content) => el("section", {}, el("h3", {}, title), content);
  return panel(repoLabel(repo), "repositories", body(
    el("div", {class: "repository-details"},
    section("Repository information", fields([["ID", el("code", {}, repo.id)], ["Provider", providerName(repo.provider)], ["Host", repo.host],
      ["Selection", badge(repo.selection_status)], ["Selection reason", reasons[repo.selection_reason] || "—"],
      ["Source status", badge(repo.source_status)],
      ["Discovery status", badge(repo.provider === "manual" ? "not_required" : repo.discovery_status)],
      ["Last seen", time(repo.discovery_last_seen_at)],
      ["Visibility", visibility[repo.visibility] || unknown]])),
    section("Backup and storage", fields([["Status", badge(repo.backup_status)], ["Last check", time(repo.last_checked_at)],
      ["Last attempt", time(repo.last_attempt_at)], ["Last update", time(repo.last_updated_at)],
      ["Measured size", bytes(repo.size_bytes)], ["Limit", limit(repo.limit_bytes)],
      ["Storage status", badge(repo.storage_status)]])),
    section("Integrity and repair", fields([["Integrity", badge(repo.integrity_status)], ["Last recorded engine check", time(integrity.last_checked_at)],
      ["Last verified healthy", time(repo.last_verified_healthy_at)],
      ["Restore compatibility", repo.restore_compatibility ? label(repo.restore_compatibility) : unknown],
      ["Periodic check result", badge(periodic.last_result)], ["Last periodic check", time(periodic.last_completed_at)],
      ["Last manual check", time(manual.last_completed_at)], ["Manual check result", badge(manual.last_result)],
      ["Manual check error", manual.error_code || "—"],
      ["Repair attempts", count(integrity.repair_attempts)], ["Successful repairs", count(integrity.successful_repairs)],
      ["Last repair", time(integrity.last_repair_at)], ["Result", badge(integrity.last_repair_result)],
      ["Action required", integrity.repair_deferred || integrity.manual_attention_required ? "Review needed" : "—"]])),
    section("Errors and retries", fields([["Last error code", repo.last_error_code || "—"], ["Recorded errors", count(repo.errors.total)],
      ["Retry", data.settings.retry.enabled ? retryBadge(retry) : badge("disabled")],
      ["Automatic attempts", count(retry.attempts) + " / " + count(retry.max_retries)],
      ["Next attempt", retry.pending ? time(retry.next_attempt_at) : "—"],
      ["Discovery deferrals", count(retry.discovery_deferrals) + " / " + count(retry.max_discovery_deferrals)],
      ["Reason for deferral", retry.block_reason || "—"]]),
    errors.length ? fields(errors.map(entry => [date(entry.at), el("code", {}, entry.code || unknown)]))
      : el("p", {class: "muted"}, "No further error details are available."))),
    canRun || canCheck || external ? el("div", {class: "detail-actions"},
      canRun ? backupButton(repo.id, repoLabel(repo)) : null,
      canCheck ? checkButton(repo.id, repoLabel(repo)) : null, external) : null),
  close, "detail");
}

function repositories(data) {
  const f = reposFilter;
  const check = data.service.manual_integrity;
  const progress = check && integrityProgress(check);
  if (location.pathname.startsWith("/repositories/")) {
    let selected;
    try { selected = decodeURIComponent(location.pathname.slice("/repositories/".length)); }
    catch (error) { if (!(error instanceof URIError)) throw error; selected = ""; }
    return [check && (!check.target || check.target === selected) ? progress : null,
      repoDetail(findRepo(selected), data)];
  }
  const update = key => value => {
    f[key] = value;
    f.page = 0;
    if (key === "errors") {
      const url = new URL(location.href);
      if (value) url.searchParams.set("errors", value);
      else url.searchParams.delete("errors");
      history.replaceState({}, "", url);
    }
    swap("repo-list", () => repoRows(data));
  };
  const list = stack(
    filterBar(searchField("Search repositories", "repo-query", f.q, update("q")),
      selectField("Provider", "repo-provider", choices(data.repositories, "provider", providerName), f.provider, update("provider")),
      selectField("Selection", "repo-selection", [["", "All"], ["included", "Included"], ["excluded", "Excluded"]], f.selection, update("selection")),
      selectField("Backup status", "repo-backup", choices(data.repositories, "backup_status", label), f.backup, update("backup")),
      selectField("Error", "repo-errors", [["", "All"], ["yes", "With errors"], ["no", "Without errors"]], f.errors, update("errors")),
      selectField("Integrity", "repo-integrity", choices(data.repositories, "integrity_status", label), f.integrity, update("integrity")),
      button("Reset filters", "close", () => {
        Object.assign(f, {q: "", provider: "", selection: "", backup: "", errors: "", integrity: "", page: 0});
        update("errors")("");
        render();
        document.getElementById("repo-query")?.focus();
      }, {only: true})),
    panel("Repository list", "repositories", el("div", {id: "repo-list"}, repoRows(data))));
  const repos = data.repositories;
  return [
    progress,
    metrics(
      metric("Known repositories", count(data.service.repositories.known), null, "repositories"),
      metric("Backed up", count(repos.filter(repo => ["updated", "up_to_date"].includes(repo.backup_status)
        && repo.selection_status === "included").length), "Included repositories", "check", "success"),
      metric("With storage warnings", count(repos.filter(repo => tone(repo.storage_status) === "warning").length),
        null, "warning", "warning", "/storage"),
      metric("With errors", count(data.service.repositories.with_errors), null, "error", "error", "/repositories?errors=yes")),
    list,
  ];
}

function integrityProgress(check) {
  const scope = check.target ? targetLabel(check.target) : "All local mirrors";
  const result = check.state === "completed" ? check.last_result : check.state;
  const current = check.repository ? " · Checking " + targetLabel(check.repository) : "";
  return panel("Manual integrity check", "shield", body(
    el("p", {role: "status"}, scope + current + " ", badge(result)),
    el("p", {class: "muted"}, [
      count(check.checked) + " checked", count(check.healthy) + " healthy",
      count(check.corrupt) + " corrupt", count(check.skipped) + " skipped",
      count(check.uncheckable) + " unable to check",
    ].join(" · "))));
}

function eventId(event) {
  return [event.at, event.operation, event.repository, event.correlation, event.host].join("|");
}

function eventRows(data) {
  const f = eventsFilter;
  const events = data.activities.filter(event =>
    (!f.type || eventType(event.operation) === f.type) && (!f.status || event.status === f.status)
    && (!f.repository || event.repository === f.repository));
  const size = 10;
  f.page = Math.min(f.page, Math.max(0, Math.ceil(events.length / size) - 1));
  const rows = events.slice(f.page * size, (f.page + 1) * size).map(event => row([
    time(event.at),
    el("button", {type: "button", class: "table-action", id: "event-" + eventId(event), onclick: () => {
      state.event = eventId(event);
      render();
      document.getElementById("activity-detail")?.focus();
    }}, eventTitle(event.operation)),
    event.repository ? link(repoPath(event.repository), {}, targetLabel(event.repository)) : event.host || "—",
    badge(event.status), event.code ? el("code", {}, event.code) : "—",
  ], state.event === eventId(event)));
  return [rows.length ? table(["Time ↓", "Operation", "Repository / Source", "Result", "Error code"], rows, "Activity table")
    : empty("No activity matches your filters."),
  pagination(events.length, f.page, size, page => { f.page = page; swap("activity-list", () => eventRows(data)); })];
}

function eventDetail(event) {
  return panel(eventTypes[eventType(event.operation)] || "Operation", "activities", body(
    el("h3", {id: "activity-detail", tabindex: "-1"}, eventTitle(event.operation)),
    fields([["Time", time(event.at)], ["Result", badge(event.status)],
      ["Repository", event.repository ? link(repoPath(event.repository), {}, targetLabel(event.repository)) : "—"],
      ...(event.trigger === "manual" ? [["Trigger", "Manual integrity check"]] : []),
      ["Source", event.host || "—"], ["Duration", duration(event.duration_ms)],
      ["Error code", event.code ? el("code", {}, event.code) : "—"],
      ["Correlation ID", event.correlation ? el("code", {}, event.correlation) : "—"]])),
  button("Close details", "close", () => {
    const id = state.event;
    state.event = null;
    render();
    document.getElementById("event-" + id)?.focus();
  }, {only: true}), "detail");
}

function activities(data) {
  const f = eventsFilter;
  const change = key => value => {
    f[key] = value;
    f.page = 0;
    swap("activity-list", () => eventRows(data));
  };
  const selected = data.activities.find(event => eventId(event) === state.event);
  const list = stack(filterBar(
    selectField("Event type", "event-type", [["", "All"], ...Object.entries(eventTypes)], f.type, change("type")),
    selectField("Result", "event-status", choices(data.activities, "status", label), f.status, change("status")),
    selectField("Repository", "event-repository", choices(data.activities, "repository", targetLabel), f.repository, change("repository"))),
  panel("Events", "activities", el("div", {id: "activity-list"}, eventRows(data))));
  return [
    notice("The service keeps up to 250 events in memory since startup. Offline restores are available through the CLI."),
    selected ? el("div", {class: "split"}, list, eventDetail(selected)) : list,
  ];
}

function storage(data) {
  const s = data.service.storage;
  const config = data.settings.storage;
  const measured = data.repositories.filter(repo => repo.size_bytes != null).sort((a, b) => b.size_bytes - a.size_bytes);
  const warning = tone(s.status) === "warning";
  const rows = measured.map(repo => row([
    link(repoPath(repo.id), {class: "table-link"}, repoLabel(repo)), providerName(repo.provider),
    badge(repo.selection_status), bytes(repo.size_bytes), limit(repo.limit_bytes),
    el("div", {}, usage(repo.size_bytes, repo.limit_bytes) || "—",
      meter(repo.size_bytes, repo.limit_bytes, "Storage usage for " + repoLabel(repo), true)),
    badge(repo.storage_status), time(repo.size_measured_at),
  ]));
  return [
    metrics(metric(s.estimated ? "Estimated backup storage used" : "Backup storage used", s.last_measured_bytes == null && data.state_available ? "Not measured" : bytes(s.last_measured_bytes),
      s.last_measured_at ? "Last measured: " + date(s.last_measured_at) : null, "storage"),
      metric("Global limit", limit(config.global_limit), "Backup directory", "repositories"),
      metric("Free disk space", bytes(s.free_bytes), s.free_bytes == null ? "No recorded measurement" : "At the last measurement", "storage"),
      metric("Minimum free space", bytes(config.reserve), "Configured for the file system", "shield", warning ? "warning" : "")),
    warning ? notice(label(s.status) + ". Backups may be paused or skipped.", "warning") : null,
    stack(panel("Global storage usage", "storage", body(
      el("div", {class: "meter-caption"}, el("strong", {}, bytes(s.last_measured_bytes)),
        el("span", {class: "muted"}, usage(s.last_measured_bytes, s.limit_bytes) || "Usage percentage unavailable")),
      meter(s.last_measured_bytes, s.limit_bytes, "Global backup storage usage"),
      fields([["Status at last measurement", badge(s.status)], ["Measured at", time(s.last_measured_at)],
        ["Space remaining under limit", s.last_measured_bytes != null && s.limit_bytes != null
          ? bytes(Math.max(0, s.limit_bytes - s.last_measured_bytes)) : unknown]]))),
    panel("Repositories by storage usage", "repositories", rows.length
      ? table(["Repository", "Provider", "Selection", "Size", "Limit", "Usage", "Storage status", "Measured at"],
        rows, "Repository storage ranking") : empty("No repository sizes have been measured yet."))),
  ];
}

function sources(data) {
  const onlyProblems = new URLSearchParams(location.search).get("problems") === "yes";
  const all = data.sources;
  const visible = onlyProblems ? all.filter(sourceProblem) : all;
  return [
    metrics(metric("Configured sources", count(all.length), "Grouped by provider and host", "sources"),
      metric("Successful discoveries", count(all.filter(source => ["success", "completed"].includes(source.status) && !sourceProblem(source)).length),
        "Sources with completed discovery", "check", "success"),
      metric("Sources with issues", count(all.filter(sourceProblem).length), null, "warning", "warning", "/sources?problems=yes"),
      metric("Active cooldowns", count(all.filter(source => Date.parse(source.retry_at) > Date.now()).length), null, "clock")),
    onlyProblems ? link("/sources", {class: "button small"}, "Show all sources") : null,
    stack(visible.length ? visible.map(source => {
      const manual = source.provider === "manual";
      const status = Date.parse(source.retry_at) > Date.now() ? "cooldown" : source.status;
      return el("article", {class: "panel source source-grid", "data-status": sourceProblem(source) ? "warning" : null},
        el("div", {}, tile(manual ? "sources" : "repositories"), el("h3", {}, providerName(source.provider)),
          el("p", {class: "muted"}, source.host)),
        el("section", {}, el("h3", {}, "Discovery"), badge(status),
          fields([["Last started", manual ? "—" : time(source.started_at)], ["Last completed", manual ? "—" : time(source.completed_at)],
            ["Cooldown until", source.retry_at ? time(source.retry_at) : "—"],
            ["Error code", source.code ? el("code", {}, source.code) : "—"]])),
        el("section", {}, el("h3", {}, "Repositories"),
          fields(manual ? [["Configured", count(source.configured)], ["Selection", "Explicit list"]]
            : [["In local catalogue", count(source.known)], ["Included", count(source.included)],
              ["Mode", source.mode === "all" ? "All matching" : "Selection list"]])),
        el("section", {}, el("h3", {}, "Visibility and filters"),
          manual ? el("p", {class: "muted"}, "No provider filters") :
            el("div", {}, el("div", {class: "chips"}, source.visibility.map(value => badge("unknown", visibility[value] || unknown))),
              fields([["Include forks", yes(source.forks)], ["Include archived repositories", yes(source.archived)]]))));
    }) : panel("Sources", "sources", empty(onlyProblems ? "No sources with issues." : "No Git sources configured."))),
  ];
}

function settings(data) {
  const s = data.settings;
  const log = s.logging;
  return [
    notice("Read-only. Change settings in Docker or Portainer using REPOHARBOR_CONFIG."),
    grid(
      panel("Backup schedule", "calendar", body(fields([
        ["Cron", el("code", {}, s.schedule)], ["Time zone", s.timezone],
        ["Next backup", time(data.service.next_scheduled_at)]]))),
      panel("Retries", "retry", body(fields([
        ["Automatic retries", enabled(s.retry.enabled)], ["Maximum attempts", count(s.retry.attempts)],
        ["Initial delay", duration(s.retry.initial_delay * 1000)], ["Maximum delay", duration(s.retry.max_delay * 1000)],
        ["Discovery deferrals", count(s.retry.max_deferrals)]]))),
      panel("Storage limits", "storage", body(fields([
        ["Global limit", limit(s.storage.global_limit)], ["Default repository limit", limit(s.storage.repo_limit)],
        ["Minimum free space", bytes(s.storage.reserve)], ["If the limit is exceeded during transfer",
          s.storage.during_transfer === "abort" ? "Abort transfer" : "Finish transfer"],
        ...Object.entries(s.storage.overrides).map(([id, value]) => [targetLabel(id), limit(value)])]))),
      panel("Integrity and repair", "shield", body(fields([
        ["Periodic check", enabled(s.integrity.enabled)], ["Cron", el("code", {}, s.integrity.schedule)],
        ["Check excluded repositories", yes(s.integrity.include_excluded)],
        ["Automatic repair", enabled(s.integrity.repair)], ["Attempts per incident", count(s.integrity.attempts)]]))),
      panel("Logging", "logs", body(fields([
        ["Extended logging", enabled(log.enabled)], ["Level", log.level], ["Format", log.format === "json" ? "JSON" : "Text"],
        ["File logging", enabled(log.file)], ["Rotation", log.file ? count(log.file_size) + " MiB · " + count(log.archives) + " archives" : "Disabled"]]))),
      panel("Web dashboard", "overview", body(fields([
        ["Web interface", enabled(s.web.enabled)], ["Bind address", s.web.bind], ["Port", count(s.web.port)],
        ["Access", "Administrator session over HTTPS"],
        ["Configuration", "Read-only"]]))),
      panel("Optional features", "settings", body(fields([
        ["Manual backup", enabled(s.manual_run)], ["Restore helper (CLI)", enabled(s.restore)],
        ["Automatic retry", enabled(s.retry.enabled)], ["Periodic integrity check", enabled(s.integrity.enabled)],
        ["Extended logging", enabled(log.enabled)], ["Web dashboard", enabled(s.web.enabled)]])))),
  ];
}

function setMenu(open) {
  sidebar.classList.toggle("open", open);
  menu.setAttribute("aria-expanded", String(open));
  menu.setAttribute("aria-label", open ? "Close menu" : "Open menu");
  menu.replaceChildren(icon(open ? "close" : "menu"));
}

function updateChrome() {
  const current = route();
  const page = pages[current] || pages["/"];
  document.title = page[1] + " · RepoHarbor";
  document.querySelector("#breadcrumb").textContent = page[1];
  nav.replaceChildren(...Object.entries(pages).map(([path, [glyph, title]]) =>
    link(path, {id: "nav-" + glyph, "aria-current": current === path ? "page" : null}, icon(glyph), title)));
  document.querySelector(".brand").setAttribute("href", destination("/"));
  const text = {idle: "Service online", busy: "Service busy", unreachable: "Service unavailable", unknown: "Service status unknown"};
  toolbar.replaceChildren(...[
    state.login || state.loading ? null : badge(serviceKey(), text[serviceKey()], "service-indicator"),
    button("Refresh status", "retry", () => refresh(true), {only: true, id: "refresh"}),
    state.data?.csrf ? button("Sign out", "lock", logout, {id: "logout"}) : null,
  ].filter(Boolean));
}

const builders = {"/": overview, "/repositories": repositories, "/activities": activities,
  "/storage": storage, "/sources": sources, "/settings": settings};

function render() {
  const active = document.activeElement;
  const focus = active?.id;
  const selection = active?.type === "search" ? [active.selectionStart, active.selectionEnd] : null;
  const scroll = Array.from(main.querySelectorAll(".table-scroll"), element => element.scrollLeft);
  updateChrome();
  const page = pages[route()] || pages["/"];
  const data = state.data;
  if (state.login) {
    main.replaceChildren(login());
  } else {
    const canRun = data?.settings.manual_run && data.csrf && location.pathname === "/repositories";
    const heading = el("header", {class: "heading"}, el("div", {},
      el("h1", {}, page[1]), el("p", {}, page[2])),
      canRun ? el("div", {class: "repository-actions"}, backupButton(null), checkButton(null)) : null);
    const content = data ? (builders[route()] || overview)(data) : [];
    const unreadable = data?.service.repositories.unreadable;
    const problem = state.problem === "status" ? "Current status data is unavailable." : "The dashboard is offline.";
    main.replaceChildren(heading,
      actionNotice() ?? el("span", {hidden: true}),
      state.loading ? notice("Loading dashboard…") : el("span", {hidden: true}),
      !state.online && !state.loading ? notice(problem + (data ? " Showing the last loaded data."
        : " Refresh to try again."), "error") : el("span", {hidden: true}),
      data && !data.state_available ? notice("The global backup state could not be read.", "warning") : el("span", {hidden: true}),
      unreadable > 0 ? notice(count(unreadable) + " repositories have unreadable metadata.", "warning") : el("span", {hidden: true}),
      ...content.filter(Boolean));
  }
  main.querySelectorAll(".table-scroll").forEach((element, index) => {
    if (scroll[index]) element.scrollLeft = scroll[index];
  });
  if (focus) {
    const replacement = document.getElementById(focus);
    replacement?.focus({preventScroll: true});
    if (replacement && selection) replacement.setSelectionRange(...selection);
  }
}

function routeChanged() {
  const params = new URLSearchParams(location.search);
  if (route() === "/repositories" && !location.pathname.startsWith("/repositories/")) {
    const next = ["yes", "no"].includes(params.get("errors")) ? params.get("errors") : "";
    if (reposFilter.errors !== next) {
      reposFilter.errors = next;
      reposFilter.page = 0;
    }
  }
  setMenu(false);
  render();
  const detail = main.querySelector(".detail .panel-head h2");
  if (detail && location.pathname.startsWith("/repositories/")) {
    detail.tabIndex = -1;
    window.scrollTo(0, 0);
    detail.focus({preventScroll: true});
  } else {
    window.scrollTo(0, 0);
    main.focus({preventScroll: true});
  }
}

function actionNotice() {
  return state.notice ? el("p", {id: "action-notice", class: "notice", role: "status"}, state.notice) : null;
}

function setNotice(message, temporary = false) {
  clearTimeout(noticeTimer);
  state.notice = message;
  if (temporary) {
    noticeTimer = setTimeout(() => {
      state.notice = "";
      document.getElementById("action-notice")?.remove();
    }, 5000);
  }
}

function login() {
  const input = el("input", {type: "password", autocomplete: "current-password",
    id: "password", required: true, maxlength: 128});
  return el("div", {class: "login"}, panel("Sign in to RepoHarbor", "lock", body(
    actionNotice(),
    el("form", {onsubmit: async event => {
      event.preventDefault();
      const submit = event.currentTarget.querySelector("button");
      submit.disabled = true;
      try {
        await post("/api/login", {password: input.value});
        setNotice("");
        await refresh(true);
      } catch (error) {
        input.value = "";
        setNotice(error.message, true);
        render();
        document.getElementById("password")?.focus();
      } finally { submit.disabled = false; }
    }}, el("label", {for: "password"}, "Administrator password"), input,
    el("button", {type: "submit", class: "button primary"}, "Sign in")))));
}

async function post(path, data) {
  const headers = {"Content-Type": "application/json"};
  if (state.data?.csrf) headers["X-CSRF-Token"] = state.data.csrf;
  let response;
  try {
    response = await fetch(path, {method: "POST", headers, body: JSON.stringify(data)});
  } catch {
    throw new Error("The connection to the dashboard was lost.");
  }
  const payload = await response.json().catch(() => null);
  if (!response.ok || !payload) throw new Error(payload?.error || "Request failed.");
  return payload;
}

async function logout() {
  try {
    await post("/api/logout", {});
    state.data = null;
    setNotice("");
  } catch (error) {
    setNotice(error.message, true);
    render();
  }
  await refresh(true);
}

function backupButton(id, name) {
  return operationButton(id, "backup", id ? "Back up repository" : "Backup all repositories",
    "play", id ? "Start a backup for “" + name + "” now?" : "Start a backup for all included repositories now?",
    "Backup requested. Waiting for the result.");
}

function checkButton(id, name) {
  return operationButton(id, "integrity", id ? "Check integrity" : "Check all repositories",
    "shield", id ? "Check the local mirror for “" + name + "”?" : "Check all eligible local mirrors, including excluded repositories?",
    "Integrity check requested. Waiting for the result.");
}

function operationButton(id, action, title, glyph, confirmation, waiting) {
  return button(title, glyph, async () => {
    if (!window.confirm(confirmation)) return;
    state.sending = true;
    setNotice(waiting);
    render();
    try {
      const response = await post("/api/" + action, {repository: id});
      setNotice(response.message, true);
    } catch (error) { setNotice(error.message, true); }
    finally {
      state.sending = false;
      render();
      await refresh(true);
    }
  }, {className: action === "backup" ? "button primary" : "button", id: action + (id ? "-repo" : "-all"),
    disabled: state.sending || !state.online || serviceKey() === "busy"});
}

async function refresh(force = false) {
  const current = ++refreshId;
  let problem = "offline";
  try {
    const response = await fetch("/api/dashboard",
      {signal: AbortSignal.timeout(6000)});
    if (current !== refreshId) return;
    problem = "status";
    if (response.status === 401) {
      if (!state.login || force) {
        state.data = null;
        state.login = true;
        state.online = true;
        state.loading = false;
        state.problem = null;
        signature = "";
        render();
      }
      return;
    }
    if (!response.ok) throw new Error("Dashboard unavailable");
    const data = await response.json();
    if (current !== refreshId) return;
    const key = JSON.stringify({...data, at: undefined});
    const updated = signature !== key || !state.online || force;
    state.data = data;
    state.loading = false;
    state.login = false;
    state.online = true;
    state.problem = null;
    if (updated) {
      signature = key;
      render();
    }
    if (force) document.getElementById("announcement").textContent = "Dashboard refreshed.";
  } catch {
    if (current !== refreshId) return;
    const changed = state.online || state.loading || force || state.problem !== problem;
    state.loading = false;
    state.online = false;
    state.problem = problem;
    if (changed && !state.login) render();
  }
}

menu.addEventListener("click", () => {
  const open = menu.getAttribute("aria-expanded") !== "true";
  setMenu(open);
  if (open) nav.querySelector("[aria-current]")?.focus();
});
document.addEventListener("keydown", event => {
  if (event.key === "Escape" && sidebar.classList.contains("open")) {
    setMenu(false);
    menu.focus();
  }
});
main.addEventListener("click", () => setMenu(false));
window.addEventListener("popstate", routeChanged);
document.addEventListener("visibilitychange", () => { if (!document.hidden) refresh(); });
setInterval(() => { if (!document.hidden) refresh(); }, 8000);
setInterval(() => {
  if (document.hidden || !state.online || serviceKey() !== "busy") return;
  document.querySelectorAll("[data-start]").forEach(element => {
    element.textContent = duration(Date.now() - Date.parse(element.dataset.start));
  });
}, 1000);
setMenu(false);
routeChanged();
refresh();
