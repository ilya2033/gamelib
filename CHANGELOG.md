# Changelog

## Unreleased (1.4.0)

Database schema v5: on first start, a `pre-v5` backup is written to `/data/backups`, then the database is migrated.

- **Missing folders.** Rescans now mark versions whose folder is gone (skipped when the share looks unmounted). ⚠ in the library, a *Missing folders* filter, and *Tools → Missing folders* to relink a moved folder or forget a deleted one.
- **Export / import.** The whole library as one JSON file, and a merge-import that matches games by version folder, then by title.
- **Personal fields.** 1–5 star rating, favourite, hours played and private notes per game, with filters and sorting.
- **Saved views.** Save any set of library filters under a name in the sidebar.
- **Bulk edit.** Select games in the library and set status, rating or favourite, or add/remove tags and categories in one go.
- **SteamGridDB covers.** Optional API key (Settings → Integrations or `STEAMGRIDDB_API_KEY`). Pick box art per game, or fetch covers for every game without one. Only on demand.
- **Tools page** (🧰 in the top bar): duplicates with merge, old versions with bulk removal, storage by status, group and platform, plus the items above.
- **Unraid Apps preparation:** MIT license, a 512 px PNG icon, a fuller template overview and the new optional SteamGridDB variable.

## 1.3.1 (2026-11-27)

- **Artwork rescan for the whole library**, on demand only (*Jobs → Find artwork in game folders*). Scopes: games without a cover; also refresh covers that were found automatically; or every game, including uploaded covers (needs a confirmation tick). A game keeps its current cover when nothing is found. It never runs on a schedule, and an interrupted run isn't resumed after a restart.
- Covers found automatically are now stored as `auto-*.webp`, so a refresh can tell them from uploaded ones. Covers found by 1.3.0 have no prefix and count as uploaded.
- Artwork job v2 (adds `scope`); queued v1 jobs are upgraded to "without a cover".
- **Docs: updating on Unraid.** The README no longer suggests re-downloading `gamelib.xml` on an existing install. That replaces `my-GameLib.xml`, where Unraid keeps your saved settings, and resets the password, ports and paths to defaults. Add new variables with **Edit → Add another Path, Port, Variable** instead.

## 1.3.0 (2026-11-26)

- **Artwork lookup:** games without a cover image now get artwork automatically from the game folder. Sources, in order:
  - a cover image (`cover.*`, `folder.*`, `logo.*`, `poster.*`);
  - an engine icon (RPG Maker, Ren'Py, Unity WebGL, `icon.*`);
  - an HTML game's `<link rel="icon">` / `favicon.*`;
  - the **application icon inside the game's `.exe`**, read by a built-in PE reader with no new dependency.

  Installers, crash handlers and redistributables are skipped when picking the exe.
- **Icon covers:** icons are rendered centred on a cover card in the game's colour, so they aren't stretched. Small pixel-art icons stay crisp.
- **Find artwork:** a new button on the edit page looks again for an existing game.
- **Background workers and jobs.** **Database schema v3 → v4**, backed up first as usual. Nothing slow runs in a web request any more:
  - **What runs as a job:** opening Scan runs a background discovery. Import, unpacking uploads (with progress in bytes), measuring folder sizes ("measuring…" until done), deleting folders and bulk artwork lookup are all jobs.
  - **Two lanes:** share-heavy jobs run one at a time; a light lane has 2 threads.
  - **The queue:** jobs are kept in SQLite. On a restart, safe jobs are re-queued and interrupted uploads are cleaned up.
  - **UI:** a Jobs page, a top-bar badge, live progress and Cancel.
- **Job versioning.** Each job stores its handler version and the GameLib version that queued it. Changed handlers bump their version and register a params upgrade. Jobs from an older version are upgraded step by step, or failed with a clear message. Jobs from a newer version (after a rollback) are refused.
- **Scheduled, incremental rescans.** `RESCAN_AT` (default `04:00`) and `RESCAN_INTERVAL_HOURS` (default `24`, `0` = off). Platform and size results are cached by folder modified time, so unchanged folders aren't re-read. **Rescan now** is on the Scan page.
- **New folder rule `games-inside`:** every folder inside a given folder is a game, including folders added there later.
- **Scan page fixes detection in place, without reloading.**
  - **this is a group**, **This is a game**, **All inside are games** and **Reset detection** save the rule through a JSON API. A small background job re-checks only that folder's subtree, and just those rows are swapped in.
  - Scroll position, open sections, ticks and edited titles and versions are kept. The saved scan result is patched too, so a reload shows the same thing.
  - **Bulk:** tick group folders and mark them all at once, in one request and one Undo step.
  - **Undo last change** reverts the last rule batch.
  - **Big lists:** rows are grouped into collapsible sections per group, which remember whether they're open. There's a text filter and a "Suspects only" filter, with counts.
  - **Suspects:** a "game" folder holding several game-like subfolders is flagged **probably a group**. **Fix all suspects** marks them all, after asking.
  - Import rows are keyed by folder, so rows can come and go freely. The old numbered form still works.
  - Without JavaScript, the buttons fall back to normal form posts.
- **Fixes found by the 300-game test:**
  - Jobs no longer keep a database write open while reading the share, which used to block other writes.
  - Progress and cancel requests no longer wait on the database.
  - An upload cancelled before it started now removes its temporary file.

## 1.2.0 (2026-11-26)

Subfolders become groups and categories. **Database schema v2 → v3.** The app backs up the database to `/data/backups/gamelib-…-pre-v3.db` before migrating. To roll back to 1.1.x, restore that file (see README, "Rolling back").

**Groups and categories**
- Version folders can now live in subfolders of the share, up to `SCAN_DEPTH` levels deep (default 3, a new advanced template setting).
- The first folder level is the **group**, shown as tabs above the grid, with an optional `folder.jpg` icon. Deeper levels are **categories**, shown as a tree in the sidebar. Filtering by a category includes its subcategories.
- A game's group and category follow its latest version's location and update automatically.
- You can add your own **categories** (for example Favourites) when adding or editing a game.

**Scan**
- Walks nested folders. A folder with a version in its name, or with files directly inside, is a game. Otherwise it is a group or category.
- If a folder is detected wrongly, use **this is a group** / **This is a game**. The choice is remembered.
- NAS metadata folders (`@eaDir`, `#recycle`) and symlinks are skipped.
- **Moved folders:** when a tracked folder disappears and a folder with the same name appears elsewhere, Scan offers **Relink**, keeping history, notes and status.

**Upload**
- A new **Destination** picker lets you choose the top level or any group/category folder, plus an optional new subfolder.
- New versions default to the folder of the game's latest version.
- Destinations through a symlink, or deeper than `SCAN_DEPTH`, are refused.

**Publishing**
- The repository and image are public, so Unraid pulls `ghcr.io/<owner>/gamelib` without a registry login.
- Each GitHub Release has a ready-to-use `gamelib.xml` (Unraid template) and `docker-compose.yml` attached, with the owner filled in. The repository copies use `__OWNER__` / `__REPO__` placeholders.
- The template's TemplateURL points at the latest release.

**Other**
- The folder layout is cached for 60 s, so browsing doesn't keep waking spun-down disks. Scan always reads fresh.
- The example compose timezone defaults to `Etc/UTC`.

## 1.1.0 (2026-11-25)

Adds game statuses. **Database schema v1 → v2.** The app backs up the database to `/data/backups/gamelib-…-pre-v2.db` before migrating. To roll back to 1.0.x, restore that file (see README, "Rolling back").

- **Statuses:** every game has one status. The four built-ins are **Pending**, **On hold**, **Completed** and **Abandoned**. New, uploaded and scanned games start as Pending, and existing games are set to Pending by the migration.
- **Changing a status:** use the selector on the game page. You can also pick one when adding a game.
- **Status history:** each game page shows its status history (from → to, when, and by whom).
- **Library:** the sidebar has a **Status** filter with counts, which combines with the platform, tag and search filters. Cards show a coloured status dot, and list view shows the status name.
- **Sorting:** new sort options **Status** (in status order) and **Status changed**.
- **Settings → Statuses:**
  - rename and recolour any status, and change the order;
  - add custom statuses (for example Playing);
  - delete custom ones, which moves their games back to Pending and keeps the history readable.

  Built-ins can't be deleted. Messages use the current name of the default status, so they stay correct if you rename Pending.
- Status history shows the time (UTC) as well as the date.

## 1.0.1 (2026-11-25)

Fixes from the security, UI and deployment validation. No database change, so updating from 1.0.0 needs no migration.

**Security**
- "Delete files" no longer follows a version folder that was replaced by a symlink.
- Write requests are checked (origin, size, login) before any body is read. Chunked bodies are refused, so the temp disk can't be filled without logging in.
- Fixed an open redirect after login (`next=/%09/…`).
- The login throttle can no longer be raced with parallel attempts.
- Added anti-framing and `nosniff` headers.
- Crafted zips, scan forms, headers and folder names now give a clean 400 instead of a 500.
- Stale temp uploads are cleaned hourly, not only at startup.

**Data safety**
- Pre-migration backups are rotated separately from manual ones (5 vs 10), so routine backups can't delete the rollback point.
- Uploaded zips keep their execute bits, so Linux game binaries stay runnable.

**UI**
- The delete dialog resets "also delete files" on Cancel.
- The desktop sidebar no longer stays collapsed after a narrow load or a rotation.
- Fixed the phone Scan row layout and the stretched Filters bar.
- Error messages can be dismissed and fade after 10 s.
- Date-style version labels no longer show as "v2026-…".
- Version is marked required when needed.
- The list view shows platform colours.
- `/api/*` errors are JSON, and an expired session is explained in the uploader.

**Deployment**
- The container refuses to start when `/data` isn't mapped, which avoids silent data loss. It warns when `/games` isn't mapped.
- A missing password gives a short, clear message.
- The Dockerfile no longer declares `VOLUME`. It strips CRLF from scripts and keeps the pip layer cached across version bumps.
- Added `.gitattributes`. The compose file no longer builds and documents `MAX_UNCOMPRESSED_GB`.
- README: corrected the `TRUSTED_ORIGINS` guidance, rollback steps (reset the tag to `latest`), the harmless pull error on Apply, and the backup rotation.

## 1.0.0 (2026-11-25)

First release.
