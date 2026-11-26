# GameLib: deployment and feature plan (v1.1 → v1.4)

Status: **agreed, 2026-11-25.** Decisions:
- subfolders = **group + category**
- **four** built-in statuses
- status conflicts: **last write wins**
- distribution via **GHCR**

See §8.

## 0. Where we are (v1.0.0)

- One container: Flask + SQLite, gunicorn on `:8080`, running as `99:100`.
- Volumes: `/data` (appdata) and `/games` (the share, read-write).
- Folders: one top-level folder per version, e.g. `/games/Starfall Tactics v1.3.1`.
- Database: schema v1. Migrations run on start and back up the DB first. An older image refuses to start on newer data.
- Validation: three sessions are running (security, UI, Unraid deploy). Their findings become **Phase 0**.

## 1. Release and deployment strategy

### 1.1 One release per feature, one migration per release

| Release | Contents | Schema |
|---|---|---|
| **1.0.1** | Phase 0: fixes from the validation reports | v1 (no change) |
| **1.1.0** | Statuses (pending / on hold / completed / abandoned) | v2 |
| **1.2.0** | Subfolders as groups and categories, folder moves | v3 |
| **1.3.0** | Background workers and jobs (scan, import, sizes, uploads, deletes), scheduled folder rescans | v4 |
| **1.4.0** | Plugin API: tokens, scopes, REST `/api/v1` | v5 |
| **1.5.0** | Webhooks (outgoing events) | v6 |

Small releases keep each database migration easy to roll back. Each one is a single `MIGRATIONS` entry, with an automatic backup before it runs. A failed release only costs you the one feature in it.

### 1.2 Build and distribute: GHCR (decided, set up 2026-11-25)

The repository is public on GitHub, and the image is a public GHCR package, `ghcr.io/<owner>/gamelib`. The workflows live in `.github/workflows/`: `ci.yml` and `release.yml`. Each release also attaches a ready-to-use `gamelib.xml` and `docker-compose.yml` with the owner filled in.

| Option | How updates work in Unraid | Effort |
|---|---|---|
| A. Build on the Unraid box (v1.0 flow, kept as fallback) | `git pull && ./scripts/build.sh`, then Edit → Apply. Unraid's update check stays "not available". | none |
| **B. GitHub Container Registry (chosen)** | GitHub Actions runs `docker buildx --platform linux/amd64` on every `v*` tag and pushes `ghcr.io/<you>/gamelib:{1.2.0, 1.2, latest}`. The template Repository becomes `ghcr.io/<you>/gamelib:latest`, Unraid shows **"update ready"**, and an update is one click. | ~1 h setup |

With B you can also pin `:1.2` (patch updates only) or `:1.2.0` (frozen), and roll back by changing the tag.

**Setup for 1.0.1:**
1. `git init`, then push to a GitHub repo. It can be private, but then Unraid needs a registry login (below).
2. Add `.github/workflows/release.yml`. On a pushed tag `v*.*.*` it:
   - runs pytest
   - checks that the tag matches `app/version.py`
   - runs `docker buildx build --platform linux/amd64` (add `linux/arm64` for local testing on a Mac)
   - pushes `ghcr.io/<you>/gamelib:{X.Y.Z, X.Y, latest}` with OCI labels
3. In the template, set `<Repository>ghcr.io/<you>/gamelib:latest</Repository>` and `<Registry>https://github.com/<you>/gamelib/pkgs/container/gamelib</Registry>`. Also add a `<TemplateURL>` that points at the raw `unraid/gamelib.xml` in the repo, so template changes reach Unraid as well.
4. For a **private** package, run `docker login ghcr.io -u <you>` once in the Unraid terminal, using a PAT with `read:packages` only. Unraid keeps the login across reboots via `/root/.docker/config.json`, which persists only if you copy it from `/boot/config` in the `go` file. The README will cover this. A public package needs no login.
5. **Releasing:** bump the version, update `CHANGELOG.md`, commit, then `git tag v1.1.0 && git push --tags`. CI does the rest.

### 1.3 Rollout steps on Unraid (every release)

1. **Read the release notes** (`CHANGELOG.md`). It is new, and lists any breaking changes and new template variables.
2. **Back up** with `docker exec -u 99:100 GameLib python -m app.backup`. The app also does this automatically before a migration.
3. **Update:** in the Docker tab, **Check for updates**, then **Apply update**. Unraid pulls the new `:latest` and recreates the container. (Fallback A: build, then **Edit → Apply**.)
4. **Check that it's healthy.** The container should be *healthy* within 60 s, and the footer should show the new version. Then run a scan and open one game page.
5. **New template variables.** If a release adds any (e.g. `SCAN_DEPTH` in 1.2.0), they appear only after you update the template file in `templates-user/`, or after you add them manually with **Add another Path/Port/Variable**. Releases ship working defaults, so skipping this is safe.
6. **Rollback:** set the previous tag, and restore `backups/*pre-vN.db` if the schema changed (see README).

### 1.4 Networking for plugins

- **Plugins on the same Unraid server:** put GameLib and the plugin containers on a **user-defined Docker network**, created once with `docker network create gamelib-net`. Plugins then call `http://GameLib:8080/api/v1`. Container-name DNS doesn't work on the default `bridge` network.
- **Plugins elsewhere on your LAN:** they call `http://<unraid-ip>:<webui-port>/api/v1`. There is no extra port, because the API shares 8080.
- **No incoming connections from plugins are ever needed.** Webhooks (1.4) are outgoing only, and go only to URLs you register.

## 2. Phase 0: validation fixes → 1.0.1

**Status: implemented in 1.0.1.** See `CHANGELOG.md`. There are 81 tests, and the image is built and smoke-tested. Browser re-check of the UI fixes: all 10 checks pass at desktop and phone size.

These are the confirmed findings from the three validation sessions (security, UI, deploy), with nothing high in security. Every fix gets a regression test. No schema change.

**Must fix**

| # | Area | Finding | Fix |
|---|---|---|---|
| 1 | backups | Routine manual backups rotate away the `pre-vN` backup, which is the only rollback point | Rotate separately by kind; never auto-delete `pre-v*` |
| 2 | security | "Delete files" on a version whose folder became a symlink deletes the *target* folder | Check `islink` on the unresolved path before `rmtree`; refuse symlinks |
| 3 | security | A chunked POST without a session or token is parsed to a temp file before the 403, so it can fill docker.img | Reject before any body parsing; cap chunked bodies; stream only after auth |
| 4 | security | Open redirect after login (`next=/%09/evil`) | Allow only `/path` matching `^/[^/\\\s]`; otherwise go to the index |
| 5 | UI | The "Also delete files" tick survives Cancel | Reset the dialog form on `close` |
| 6 | deploy | `VOLUME` in the Dockerfile hides a missing `/data` mapping, so data can be lost on recreate | Drop `VOLUME`; the entrypoint warns or refuses when `/data` isn't a mount |
| 7 | deploy | CRLF / a lost exec bit after copying from Windows breaks `build.sh` and the entrypoint | `.gitattributes` `eol=lf`, strip `\r` in the Dockerfile, `sh ./scripts/build.sh` in the README |
| 8 | app | Unpacked uploads lose Unix exec bits, so Linux game binaries can't run | Restore the mode bits from `external_attr`, masked to `0o755`/`0o644` |

**Should fix**
- **Security:**
  - The login throttle has a race (fix with a lock around check+record).
  - There are no `X-Frame-Options`/CSP `frame-ancestors` headers.
  - Crafted inputs give a 500 instead of a 400: a file/dir clash, a bad CRC, an over-long name, `count=abc`, a non-ASCII header.
  - Stale temp files are only cleaned at startup (also clean them hourly).
- **UI:**
  - The desktop sidebar stays collapsed after a narrow load.
  - The phone Version field on the Scan page is squashed.
  - The phone Filters bar is stretched.
  - Error toasts never close.
  - Date-style labels show as "v2026-…".
  - Version is not marked required in the form.
  - The list view platform colour is missing.
  - `/api/*` returns HTML errors.
- **Deploy:**
  - `ARG VERSION` busts the pip cache on every build.
  - The README rollback doesn't say to set the tag back to `latest`.
  - The README trigger for `TRUSTED_ORIGINS` (rewritten Host or a non-standard port) is wrong.
  - The missing-password error is buried in a traceback.
  - Migrations are not logged.
  - Compose `build:` passes no version.
  - The PUID/PGID/UMASK rows in the README table are blank.

Full reports, with reproductions and the phone screenshots, are in the validation sessions' notes (`security-`, `ui-` and `deploy-validation-report.md`).

## 3. Statuses → 1.1.0 (schema v2)

**Status: implemented in 1.1.0.** Status-change times use microsecond precision. History rows store the labels, so they stay readable after a status is renamed or deleted.

### Behaviour

- **Exactly one status per game.** New games start as **Pending**.
- **Built-in statuses (exactly these four):**

  | key | label | colour |
  |---|---|---|
  | `pending` | Pending | grey |
  | `on_hold` | On hold | amber |
  | `completed` | Completed | green |
  | `abandoned` | Abandoned | red |

  Built-ins can be renamed and recoloured but not deleted. You can add custom statuses later in **Settings → Statuses**, for example *Playing* or *Wishlist*.
- **Sidebar:** a new **Status** section with counts, combinable with the platform, tag and group filters.
- **Grid:** a small coloured status pip on each card. List view shows a status column.
- **Detail page:** a status selector that changes the status immediately. Each change is recorded in a **status history** (from → to, when, set by whom).
- **Sorting:** new options for status and "status changed".

### Schema v2

```sql
CREATE TABLE statuses (
  id INTEGER PRIMARY KEY, key TEXT NOT NULL UNIQUE, label TEXT NOT NULL,
  color TEXT NOT NULL, sort INTEGER NOT NULL, builtin INTEGER NOT NULL DEFAULT 0);
INSERT INTO statuses(key,label,color,sort,builtin) VALUES
  ('pending','Pending','#8a93a3',10,1), ('on_hold','On hold','#e7b84a',20,1),
  ('completed','Completed','#5cc98a',30,1), ('abandoned','Abandoned','#ef6461',40,1);
ALTER TABLE games ADD COLUMN status_id INTEGER REFERENCES statuses(id);
UPDATE games SET status_id = (SELECT id FROM statuses WHERE key='pending');
ALTER TABLE games ADD COLUMN status_changed_at TEXT;
CREATE TABLE status_history (
  id INTEGER PRIMARY KEY, game_id INTEGER NOT NULL REFERENCES games(id) ON DELETE CASCADE,
  from_id INTEGER, to_id INTEGER NOT NULL, source TEXT NOT NULL, at TEXT NOT NULL);
```

Deleting a custom status moves its games back to Pending.

## 4. Subfolders as groups and categories → 1.2.0 (schema v3)

**Status: implemented in 1.2.0.** On top of the design below:
- The folder layout is cached for 60 s and invalidated on writes, so browsing doesn't wake spun-down disks.
- Folders created as upload destinations are recorded as groups.
- An upload destination deeper than `SCAN_DEPTH` is refused, because Scan could never find the game there.

### Folder layout (decided: group + category)

```
/games/
├── PC/                               ← level 1 = GROUP (a sidebar section)
│   ├── Strategy/                     ← level 2+ = CATEGORY path "Strategy"
│   │   ├── Starfall Tactics v1.2/    ← version folder
│   │   └── Starfall Tactics v1.3.1/
│   └── Cave Explorer v2.0/           ← in group "PC", no category
├── Browser/
│   └── RPG Maker/
│       └── Moonlit Text RPG v1.0/
└── Pixel Racer v0.9/                 ← top level: still works, group "Ungrouped"
```

**Meaning:**
- **Level 1 → Group.** Groups split the library into separate sections, such as PC, Browser or Prototypes. Groups appear as tabs above the grid and in the sidebar, and "All" shows everything.
- **Deeper levels → Category path,** for example `Browser / RPG Maker`. Categories form a tree in the sidebar.
- Group + category is the only layout. There is no `FOLDER_LAYOUT` setting, which keeps the code and the tests simpler.

### Telling group folders from version folders

A folder is treated as a **version folder** when either of these is true:
1. Its name parses as `Title vX` or a similar pattern (see `parse_folder_name`).
2. It has at least one regular file directly inside it, apart from `folder.jpg`, `desktop.ini`, `Thumbs.db`, `.DS_Store` and other hidden files.

Any other folder is a **group or category folder**, and scanning goes into it. Two limits apply:
- `SCAN_DEPTH` (default `3`) stops runaway walks.
- Folders called `.gamelib-*`, `@eaDir` or `#recycle` are always skipped.

If a folder is classified wrongly, the Scan page has a per-folder toggle, **"This is a game" / "This is a group"**. Your choice is saved in a `folder_rules` table and overrides the automatic detection from then on.

### What changes in the app

| Area | Change |
|---|---|
| `versions.folder` | Becomes a relative **path** (`PC/Strategy/Starfall Tactics v1.2`). Existing values stay valid. `resolve_in` already handles nested paths and still blocks escapes. |
| Scan | Walks the tree and groups the results by group and category. Suggested categories come from the path. |
| Game ↔ category | **Many-to-many.** The path-derived category is attached automatically (source `folder`), and you or plugins can add more (source `user` or `plugin:<name>`). |
| Upload | The form gets a **Destination** picker listing the existing groups and categories, plus "new subfolder…". The new folder is created there. |
| Moved folders | When a tracked path disappears and an untracked folder with the same name appears elsewhere, Scan offers **"Relink: moved from X to Y"**. A version's path is never changed silently. |
| Group/category cover | An optional `folder.jpg` inside a group folder is used as its icon. |

### Schema v3

```sql
CREATE TABLE categories (
  id INTEGER PRIMARY KEY, parent_id INTEGER REFERENCES categories(id) ON DELETE CASCADE,
  name TEXT NOT NULL, is_group INTEGER NOT NULL DEFAULT 0, path TEXT UNIQUE,
  UNIQUE(parent_id, name));
CREATE TABLE game_categories (
  game_id INTEGER NOT NULL REFERENCES games(id) ON DELETE CASCADE,
  category_id INTEGER NOT NULL REFERENCES categories(id) ON DELETE CASCADE,
  source TEXT NOT NULL DEFAULT 'user',              -- user | folder | plugin:<name>
  PRIMARY KEY (game_id, category_id));
CREATE TABLE folder_rules (path TEXT PRIMARY KEY, kind TEXT NOT NULL CHECK (kind IN ('game','group')));
ALTER TABLE game_tags ADD COLUMN source TEXT NOT NULL DEFAULT 'user';
```

Template and env: one new advanced variable, `SCAN_DEPTH` (default `3`).

## 5. Background workers and jobs → 1.3.0 (schema v4)

**Why.** The share holds hundreds of GB across hundreds of small games. Right now, Scan, Import, measuring folder sizes, unpacking uploads and deleting folders all run inside the web request. A large share makes the page hang, and a reverse proxy can time out. Every slow share operation becomes a **job** run by **background workers**. A request only validates input, queues the job and returns immediately.

### Workers

- **In-process worker threads.** They run in the single gunicorn worker, so there's no extra container or external queue, in line with the "one container" requirement.
- **Two lanes:**
  - **`share` lane, 1 thread:** anything that reads or writes lots of share data. Running these one at a time keeps an Unraid array from thrashing across several disks.
  - **`light` lane, 2 threads:** quick work, such as layout cache refreshes and syncing categories.
- **A queue in SQLite** (the `jobs` table). Jobs survive a restart, and the UI can show history.
- **Cancellable.** A job checks a cancel flag between folders, between files during unzip, and per directory while measuring.
- **Restart recovery.** On start, jobs left `running` become `interrupted`:
  - Safe kinds are re-queued: `discover`, `rescan` and `measure`.
  - Unsafe kinds are marked failed, and their temp and staging files are cleaned up: `install` and `delete`.

### Job kinds

| Kind | Lane | Started by | What it does |
|---|---|---|---|
| `discover` | share | opening Scan, **Rescan now** | Walk the share, detect platforms, find moved folders. Saves the result so Scan renders it instantly. |
| `import` | share | Scan → Import selected | Create games and versions, measure sizes, pick covers. Commits per game, so a cancel keeps finished games. |
| `measure` | share | add version from folder, Relink, Refresh size | Folder size. The version shows "measuring…" until done. |
| `install` | share | Add game / New version with Upload | Unzip into staging with byte progress, move into place, then create the game or version. The cover is saved in the request beforehand. |
| `delete` | share | Delete with "also delete files" | The DB row is removed in the request. The folder is removed in the background. |
| `rescan` | share | scheduler | See below. |
| `layout` | light | stale cache | Refresh the cached share layout used for page badges and form pickers. Requests never walk the share. |

### Scheduled folder rescans

- **Schedule.** A scheduler thread queues `rescan` at `RESCAN_AT` (default `04:00` local time, so the disks are usually idle) and every `RESCAN_INTERVAL_HOURS` (default `24`, `0` = off). **Rescan now** is on the Scan page.
- **Incremental.** A `scan_cache` table keeps each folder's `mtime`, kind, platform, size and when it was last seen. Unchanged folders are skipped, so with hundreds of games a rescan only touches what changed.
- **What a rescan updates:**
  - new untracked folders, for the Scan badge and list;
  - tracked versions whose folder disappeared, marked **missing**;
  - moved-folder suggestions, for Relink;
  - sizes of folders whose `mtime` changed.
- **Never imports or relinks by itself.** That stays a confirmed action, as agreed.

  An optional `AUTO_IMPORT=1` could import only folders that match an existing game's title. ⚑ decide later.
- **After a rescan,** Scan shows the result straight away, with "last scanned 3 h ago".

### UI

- **Jobs page** (`/jobs`): running and recent jobs with progress bar, current item, elapsed time, Cancel, and the error for failures.
- **Top-bar indicator:** "⏳ 2" while jobs are running, linking to `/jobs`.
- **Job pages poll** `GET /api/jobs/<id>` every second. Without JavaScript they refresh themselves every 2 s. When a job finishes, the page goes on to its result: the game page for an upload, the library for an import, the Scan results for a discovery.
- **Live progress** for Scan and uploads. Unzipping shows bytes done of the total, and import shows "12 / 340 · measuring <folder>".

### Better artwork lookup (icons from .exe files)

Today a cover is only picked up from `cover.*`, `folder.*`, `logo.*` or `poster.*`. Most games have none, so the grid is mostly placeholders. The new lookup tries these sources in order, stopping at the first one found:
1. **Explicit cover images:** `cover.*`, `folder.*`, `logo.*`, `poster.*`, as today.
2. **Engine icons:**
   - RPG Maker: `www/icon/icon.png`, `icon/icon.png`;
   - Ren'Py: `game/gui/window_icon.png`;
   - Unity WebGL: `TemplateData/favicon.ico`;
   - generic: `icon.png` / `icon.ico`.
3. **HTML games:** the `<link rel="icon">` from `index.html`, or `favicon.*`. Paths must stay inside the game folder.
4. **Windows `.exe` icon.** A small built-in PE resource reader, with no new dependency:
   - It reads the first `RT_GROUP_ICON` (the application icon), picks the largest and deepest image, and rebuilds it as `.ico`, or uses the embedded PNG as is.
   - It reads only the needed bytes with seeks, so it's safe on huge executables.
   - It picks the game's exe: names matching the game title win over the largest file. Helpers are skipped: `unins*`, `setup*`, `UnityCrashHandler*`, `vc_redist*`, `dxsetup` and so on.
- **Covers from icons.** An icon is square and small, so it is centred on a 600×800 card coloured from the title. It isn't stretched into a blurry crop. Pixel-art icons up to 64 px are scaled up without smoothing.
- **Where it runs:** on upload, on add from folder, on scan import, and through a new **Find artwork** button on the edit page for existing games. With the 1.3.0 workers it becomes a `light` job, and a **Find artwork for all games without one** bulk action.

### Scan page: fixing detection without page reloads

**Problem (seen in 1.2.0).** Every **this is a group** / **This is a game** click is a full form POST. The page reloads, the share is walked again, and the list comes back collapsed and scrolled to the top. If tens or hundreds of folders are misdetected, fixing them one reload at a time is unusable.

**Fix:**
- **In-place toggles.** The buttons call `POST /api/scan/rules` (JSON) instead of submitting the page. The response holds only the **changed part of the list**:
  - the toggled row;
  - for a new group, its subfolders as new rows;
  - for a new game, the rows that were inside it, which are removed.

  The page patches just that subtree. Scroll position, open panels, ticked boxes and edited titles and versions all stay as they were.
- **Re-check only the affected folder.** The server re-classifies just the toggled folder's subtree, using `scan_cache`, as a `light` lane job. Other folders aren't re-read. The full discovery result is updated in place, so a later reload shows the same state.
- **Bulk actions:**
  - Tick several rows or group folders, then **Mark selected as game** / **Mark selected as group**. This is one request and one rule batch.
  - **Everything inside this folder is a game**, for example `HTML/`. It stores one rule, `games-inside`, on the parent instead of hundreds of per-folder rules, and applies to folders added there later.
  - **Undo** for the last rule batch.
- **Layout for big lists:**
  - The list is grouped by group/category in collapsible sections, which keep their open/closed state in the browser (`localStorage`).
  - A filter box: "show only: misdetected suspects / new / existing games / by group".
  - Counts per section.
- **Suspects first.** The page highlights likely misdetections: a "group" whose children all look like single games, or a "game" that contains several version-like subfolders. **Fix all suspects** applies the suggested rule to all of them in one step, with a preview.
- **Schema.** `folder_rules.kind` gains `'games-inside'`. A changed CHECK constraint can't be altered in place, so the table is recreated in the v4 migration.
- **Tests:**
  - a toggle returns only the affected rows;
  - bulk marking of 200 folders is one request;
  - `games-inside` also applies to new subfolders;
  - Undo;
  - no full share walk on a toggle.

### Schema v4

```sql
CREATE TABLE jobs (
  id TEXT PRIMARY KEY, kind TEXT NOT NULL, lane TEXT NOT NULL,
  state TEXT NOT NULL,            -- queued | running | done | failed | cancelled | interrupted
  params TEXT NOT NULL,           -- JSON
  done INTEGER NOT NULL DEFAULT 0, total INTEGER NOT NULL DEFAULT 0, message TEXT,
  result TEXT, error TEXT, cancel_requested INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL, started_at TEXT, finished_at TEXT);
CREATE INDEX jobs_state ON jobs(state, lane, created_at);
CREATE TABLE scan_cache (
  path TEXT PRIMARY KEY, kind TEXT NOT NULL, mtime REAL NOT NULL,
  platform TEXT, size_bytes INTEGER, last_seen TEXT NOT NULL);
-- folder_rules recreated with kind IN ('game', 'group', 'games-inside')
```

Old finished jobs are pruned, keeping the last 200 or 30 days. `versions.size_bytes = -1` means "measuring".

### Settings (template + compose, advanced)

- **`RESCAN_AT`** (`04:00`) and **`RESCAN_INTERVAL_HOURS`** (`24`, `0` = off): the rescan schedule.
- **`JOB_LIGHT_WORKERS`** (`2`): threads in the light lane. The share lane always has 1.

### Already in the working tree (uncommitted, from the first attempt)

- `app/jobs.py`: an in-memory job runner with progress, cancel and sync mode for tests. It becomes the base for the SQLite-backed queue.
- `app/storage.py`: progress callbacks for unzip and cancel hooks for `folder_size`.

### Tests

- Every job kind in sync mode, plus real threads (start, poll, done).
- Cancel mid-import and mid-unzip: finished games are kept, and staging is cleaned up.
- Restart recovery from a `running` row.
- Scheduler timing with a fake clock.
- Incremental rescan: unchanged folders are not re-measured, and a changed `mtime` is.
- A share with 500 generated folders: requests stay fast, and no request walks the share.

## 5b. Plugin API → 1.4.0 (schema v5)

The goal is that other services, such as an auto-tagger, a playtime tracker or a metadata fetcher, can read the library and assign **tags, categories and status** without touching the database.

### Auth

- **Tokens.** You create API tokens in **Settings → API tokens**. Each token has a name (this becomes the plugin's source label), scopes, an optional expiry, and a record of when it was last used. The token is shown **once**. Only its SHA-256 hash is stored.
- **Requests** send `Authorization: Bearer glb_<40 random chars>`.
- **Cookies are ignored on `/api/v1`.** Browsers never send the token by themselves, so CSRF doesn't apply to the API. The existing Origin and CSRF checks keep protecting the UI routes.
- **Scopes:**

  | scope | allows |
  |---|---|
  | `read` | everything readable |
  | `tags:write` | tags |
  | `categories:write` | categories (not groups) |
  | `status:write` | status |
  | `games:write` | title and description, not files |
  | `statuses:admin` | create or edit custom statuses |

Write access is all-or-nothing for each field type.

- **Plugins never touch files.** No token can upload or delete them.
- **Rate limit:** 120 requests per minute per token, returning `429` with `Retry-After`.

### Endpoints (JSON, documented at `GET /api/v1/openapi.json`)

```
GET    /api/v1/games?status=&tag=&category=&group=&platform=&q=&updated_since=&page=&per_page=
GET    /api/v1/games/{id}                       → game + versions + tags + categories + status
GET    /api/v1/games/lookup?folder=<path>       → find by any version folder
GET    /api/v1/games/lookup?title=<title>
PATCH  /api/v1/games/{id}                       → {"status": "completed",
                                                    "tags": {"add": ["rpg"], "remove": ["todo"]},
                                                    "categories": {"add": ["Favourites"]}}
PUT    /api/v1/games/{id}/tags                  → replace this plugin's own tags only
POST   /api/v1/batch                            → up to 500 PATCH ops, one transaction
GET    /api/v1/statuses | /tags | /categories
POST   /api/v1/statuses                         (statuses:admin)
GET    /api/v1/events?since=<cursor>            → change feed (polling alternative to webhooks)
```

Example response for one game:

```json
{
  "id": 12, "title": "Starfall Tactics", "platform": "windows",
  "status": {"key": "on_hold", "label": "On hold", "source": "plugin:playtime", "changed_at": "2026-11-20T18:02:11Z"},
  "group": "PC", "categories": [{"path": "PC/Strategy", "source": "folder"}],
  "tags": [{"name": "tactics", "source": "user"}, {"name": "unity", "source": "plugin:autotag"}],
  "latest_version": {"id": 31, "label": "1.3.1", "folder": "PC/Strategy/Starfall Tactics v1.3.1",
                     "size_bytes": 2147483648, "added_at": "2026-11-18T10:00:00Z"},
  "updated_at": "2026-11-20T18:02:11Z"
}
```

### Ownership and conflict rules (decided: last write wins)

- **No locking.** Any token with the matching scope can change any game's status, tags and categories at any time, including values you set by hand. The most recent write wins.
- **Source labels.** Because nothing is locked, every assignment records who set it last: `user`, `folder` or `plugin:<token name>`. The game page shows "On hold · set by playtime, 2 h ago".
- **Undo.** Every change goes into the activity log (below), and each entry in **Settings → Activity** has an **Undo** button that restores the old value. Undoing is itself recorded as a change by `user`.
- **Folder categories.** A category derived from the folder path (source `folder`) is re-added on the next scan if a plugin removes it, because the folder is the source of truth for location.
- **Revoking a token** leaves its assignments in place. **Settings → API tokens → "Revert everything this plugin changed"** undoes that token's activity entries, newest first.
- **Concurrent writes.** `PATCH` accepts an optional `If-Unmodified-Since` / `"expected_updated_at"`, so a plugin that cares can detect a change made after it last read the game (`412`). This is optional, never required.

### Audit and safety

- **Activity log.** An `activity` table records every API write: token, game, field, old → new value, and time. It is shown in **Settings → Activity** and in the game page history.
- **Server-side validation.** Tag and category names are limited to 40 characters and a safe character set. Statuses must exist, unless the token has `statuses:admin`.
- **Payload limits.** Request bodies are capped at 1 MB, and each batch at 500 operations.

### Schema v5

```sql
CREATE TABLE api_tokens (
  id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE, token_hash TEXT NOT NULL UNIQUE,
  scopes TEXT NOT NULL, created_at TEXT NOT NULL, expires_at TEXT, last_used_at TEXT, revoked INTEGER NOT NULL DEFAULT 0);
CREATE TABLE activity (
  id INTEGER PRIMARY KEY, at TEXT NOT NULL, source TEXT NOT NULL, game_id INTEGER,
  field TEXT NOT NULL, old TEXT, new TEXT);
ALTER TABLE games ADD COLUMN status_source TEXT NOT NULL DEFAULT 'default';
```

### Deliverables

- **Example plugins.** `examples/plugins/autotag.py` is a stdlib-only script that tags games by detected platform and engine (Unity, RPG Maker, Ren'Py) and marks games with no versions as `pending`. A curl cookbook goes in the README.
- **Optional Python client.** `gamelib_client.py` is a single file with no dependencies.

## 6. Webhooks → 1.5.0 (schema v6)

- **Registering.** In **Settings → Webhooks** you register a URL, a secret, and the events to send.
- **Events:** `game.created`, `game.deleted`, `version.added`, `version.deleted`, `status.changed`, `tags.changed`, `categories.changed`, `scan.completed`.
- **Delivery.** Each event is a `POST` with a JSON body and the header `X-GameLib-Signature: sha256=<HMAC(secret, body)>`. It is sent from a background thread with retries at 1 m, 5 m, 30 m and 2 h, and each attempt is logged with its status code.
- **Destination limits.** Only `http(s)` URLs that you entered. Redirects are not followed. The timeout is 10 s.
- **Fallback.** Plugins that can't receive webhooks can poll `GET /api/v1/events`, which serves the same event stream.

## 7. Testing per release

| Release | Must pass before tagging |
|---|---|
| all | full pytest suite; migration test from **every** previous schema version, with a backup created; container smoke test (healthy, runs as 99:100, `/data` owned 99:100) |
| 1.1.0 | the status filter combined with others; status history; custom status delete → Pending |
| 1.2.0 | nested scan; the depth limit; group/game classification and `folder_rules` override; relinking a moved folder; upload into a subfolder; path escape through nested `..` and symlinked group folders |
| 1.4.0 | token hashing and one-time display; every scope denied without permission; cookie auth ignored on `/api/v1`; last-write-wins with correct source labels; Undo and "revert plugin" from the activity log; optional `412` check; batch is atomic; rate limit |
| 1.5.0 | the signature is verifiable; retry and backoff; no redirects followed; a slow endpoint doesn't block requests |

## 8. Decisions (2026-11-25)

| Topic | Decision |
|---|---|
| Subfolders | Level 1 = **group** (tabs + sidebar section). Deeper levels = **category** tree. Top-level game folders → "Ungrouped". |
| Statuses | Exactly four built-ins: **Pending** (the default for new and scanned games), **On hold**, **Completed**, **Abandoned**. You can add custom ones. |
| Plugin conflicts | **Last write wins**, with source labels, an activity log, Undo, and "revert plugin". |
| Distribution | **GHCR** via GitHub Actions. Unraid shows updates. The local build stays as a fallback. |
