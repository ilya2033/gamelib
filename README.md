# GameLib

A small self-hosted game library for an Unraid server, in the style of a Lutris
library: a cover grid of the games sitting on your **games share**, with
platforms, tags, version history, uploads straight into the share and zip
downloads. One Flask app, SQLite, one container, no external services.

- **Library:** cover grid or list, with search, platform and tag filters, and sorting by name, added or updated.
- **Search with autocomplete:** suggestions appear as you type (games with cover and status, plus matching tags, groups and categories). It tolerates typos, initials such as `ff vii`, and missing spaces. Press **/** to focus the search.
- **Groups and categories:** subfolders on the share become groups (tabs) and categories (a sidebar tree), and you can add your own categories.
- **Games:** title, platform (Windows, Linux, macOS, HTML, Unity WebGL, Other), description, tags and a cover/logo.
- **Statuses:** Pending, On hold, Completed and Abandoned, plus any custom ones you add. Each game has one status, with a history of changes, and you can filter and sort by status. Manage them in **Settings → Statuses**.
- **Versions:** one folder on the share per version, each with its own label, added date, notes and size. Old versions are kept.
- **Scan:** finds untracked folders on the share, suggests a title, version and platform for each, and imports only after you confirm.
- **Upload:** a `.zip` or a single `.html` is streamed to disk with a progress bar, then unpacked into a new folder on the share. Multi-GB files work.
- **Play in the browser:** HTML games (HTML5 exports, RPG Maker MV/MZ, Twine, Construct...) run full-window inside GameLib, sandboxed, and **their saves are kept on the server**, so they follow you to every browser and device.
- **Start file:** pick which file starts each version (an `.html` page, `.exe`, `.sh`...), or let GameLib find it.
- **Download:** any version as a zip, built while it streams (nothing is stored twice). A single-file version downloads as the file itself.
- **Edit and delete:** edit details, replace the cover, delete a version or a game. Deleting asks for confirmation and keeps the files unless you tick "also delete files".
- **Works on phone and desktop.**

## How the games share is used

```
/mnt/user/games/                       → mounted at /games
├── PC/                                ← level 1 = GROUP (a tab above the grid)
│   ├── folder.jpg                     ← optional group icon
│   ├── Strategy/                      ← deeper level = CATEGORY ("PC › Strategy")
│   │   ├── Starfall Tactics v1.2/     ← version "1.2" of "Starfall Tactics"
│   │   └── Starfall Tactics v1.3.1/   ← version "1.3.1" of the same game
│   └── Cave Explorer v2.0/            ← group PC, no category
├── Browser/
│   └── RPG Maker/
│       └── Moonlit Text RPG/          ← no version in the name → you type one in Scan
├── Pixel Racer (v0.9)/                ← top level still works: "Ungrouped"
└── .gamelib-staging-…/                ← hidden, used while unpacking an upload
```

- **One folder per version.** Version folders can sit at the top level or inside group/category folders, up to `SCAN_DEPTH` levels deep (default 3).
  - Recognised name patterns: `Name v1.2`, `Name (v1.2)`, `Name [1.2]`, `Name_v1.2`, `Name-1.2.3` and `Name ver 1.2`.
- **Telling games from groups:** a folder is a **version folder** if its name has a version in it, or it directly contains at least one file. `folder.jpg`, `desktop.ini`, `Thumbs.db` and hidden files don't count.
  - Any other folder is a **group** (first level) or **category** (deeper), and Scan looks inside it.
  - If a folder is detected wrongly, use **this is a group** / **This is a game** / **All inside are games** on the Scan page. The list updates in place without reloading, and your choice is remembered.
  - For many folders, tick them in *Group folders* and mark them together, or use **Fix all suspects**. **Undo last change** reverts the last batch.
  - Hidden folders, symlinks, `@eaDir` and `#recycle` are skipped.
- **Groups and categories:**
  - A game's group and category come from where its **latest** version lives, and they update when you add, move or delete versions.
  - You can also give games your own **categories**, such as *Favourites* or *Couch co-op*, when adding or editing them.
  - Both kinds show in the sidebar and can be filtered on.
  - Filtering by a category includes everything below it.
- **Scan:**
  - Groups folders with the same title into one game. A folder whose title matches an existing game becomes a new version of that game.
  - The *added date* of a scanned version is its folder's modified time, and you can edit it.
  - If you move a version folder elsewhere on the share, Scan offers **Relink** under *Moved folders*, which keeps the version's history, notes and the game's status.
- **Upload:** creates `"<Title> v<Version>"` in the **Destination** you pick: the top level, any existing group/category folder, plus an optional new subfolder. New versions default to the folder of the game's latest version.
  - If the zip has a single top-level folder, that folder is flattened.
  - `__MACOSX` and `.DS_Store` entries are skipped.
  - If the folder name already exists, a suffix is added, for example ` (2)`. Nothing is overwritten.
- **Cover:** if you don't upload one, a `cover.*`, `folder.*`, `logo.*` or `poster.*` image (png, jpg, webp) at the top of the game folder is used automatically.
- **Read-only:** apart from uploads and the optional "also delete files" on delete, the app never changes the share. To make it strictly track-only, mount `/games` read-only.
- **Background jobs:** everything slow runs in background workers, so pages never wait for the share. That covers scanning, importing, unpacking uploads, measuring folder sizes, deleting folders, and finding artwork.
  - **Jobs page:** the **⏳ Jobs** page and top-bar badge show progress, and you can **Cancel** a job.
  - **One at a time:** share-heavy jobs run one after another, so an Unraid array isn't thrashed.
  - **Restarts:** jobs survive a container restart. Scans and size checks are re-queued; an interrupted upload is cleaned up and marked failed.
  - **Job versions:** each job records the version of the handler that created it. After an update, older queued jobs are upgraded or clearly failed, never run with params they don't understand.
- **Scheduled rescans:** by default every 24 h at 04:00, the share is rescanned in the background. Scan then opens instantly, with "Last scanned …".
  - It is **incremental**: platform detection and sizes are cached per folder and only redone when a folder's modified time changes.
  - It never imports anything by itself.
- **Spun-down disks:** pages never walk the share. The folder list used by pages is cached and refreshed in the background.
- **Artwork rescan (on demand only):** *Jobs → Find artwork in game folders* looks through the whole library for a cover image, engine icon, favicon or `.exe` icon. Pick the scope: games without a cover, also refresh covers that were found automatically, or every game including covers you uploaded (needs an extra tick). A game keeps its cover when nothing is found. It never runs on a schedule and isn't resumed after a restart.
- **Your own data per game:** a 1–5 star rating, a ♥ favourite, hours played (typed in; GameLib doesn't track playtime) and private notes. Set them on the game page or in *Edit*, filter the library by them, and sort by rating or hours.
- **Saved views:** any combination of filters can be saved under a name in the sidebar, for example "Pending + Windows + no cover".
- **Bulk edit:** **Select** in the library toolbar, tick games (or *All shown*), then set status, rating or favourite, or add/remove a tag or category for all of them at once.
- **Missing folders:** every rescan marks versions whose folder is gone. They show with ⚠ in the library and are listed under *Tools → Missing folders*, where you can **Relink** a moved folder or **Forget** a deleted one (the game, its status and notes stay). If the share looks unmounted (empty or unreadable), nothing is marked.
- **Export / import:** *Tools → Export and import* downloads the whole library as one JSON file (statuses, ratings, favourites, hours, notes, tags, categories and version folders; not covers or files). Importing merges into the current library, matching games by version folder, then by title.
- **SteamGridDB covers (optional):** with a free API key (*Settings → Integrations* or the `STEAMGRIDDB_API_KEY` variable), *Find cover on SteamGridDB* on a game's *Edit* page lets you pick box art, and *Tools* can fetch covers for every game without one. Only runs when you click; game titles are sent to SteamGridDB.
- **Duplicates:** *Tools → Duplicates* lists games with the same title (ignoring case, punctuation, a leading "The" and bracketed parts) and merges two entries into one.
- **Old versions:** *Tools → Old versions* lists every version except each game's newest, biggest first, and removes the ones you pick, optionally deleting their folders. The newest version is never offered.
- **Playing HTML games:** **▶ Play** appears for versions whose start file is an `.html` page. The game opens in a sandboxed frame with a full-screen button. It runs on its own opaque origin, so it can't read GameLib's cookies or pages, and its files are served through a signed link that expires after 1–2 days.
  - **Saves:** the game's `localStorage` is stored in GameLib's database per game and shared by all its versions. That makes saves part of the database backups, independent of the browser, and editable on the game page under **Browser saves** (download, restore from file, delete).
  - **Not saved:** games that save only to IndexedDB (most Unity WebGL and Godot exports) still run, but their progress is lost when the tab closes.
- **Start file:** *Edit version → Start file* lists the pages and programs in the folder. *Automatic* uses `index.html` (also inside `www/`, `html/`, `web/`...), a single page, or the game's `.exe`. The game page shows it next to each version, and artwork lookup uses it.
- **Storage:** *Tools → Storage* shows size by status, group and platform, the largest games and free space on the share.

## Install on Unraid

Each release is published as a public image on GitHub Container Registry,
`ghcr.io/<owner>/gamelib`, tagged with the exact version (`1.1.0`), the minor
version (`1.1`) and `latest`. `<owner>` is the GitHub account that publishes
GameLib: the owner of the repository you got it from. Pulling needs no login.

Every [GitHub Release](../../releases/latest) also has two ready-to-use files
attached, with the owner already filled in: **`gamelib.xml`** (Unraid template)
and **`docker-compose.yml`**. The copies in this repository contain
`__OWNER__` / `__REPO__` placeholders. Use the release files, or replace the
placeholders yourself.

### 1. Add the container template

Download `gamelib.xml` from the latest release straight onto the flash drive
(Unraid terminal: the *>_* icon in the top bar):

```bash
wget -O /boot/config/plugins/dockerMan/templates-user/my-GameLib.xml \
  https://github.com/<owner>/gamelib/releases/latest/download/gamelib.xml
```

Or copy it over SMB to `\\TOWER\flash\config\plugins\dockerMan\templates-user\my-GameLib.xml`.

> **First install only.** After you click **Apply**, Unraid saves your settings
> (password, ports, paths) into this same `my-GameLib.xml`. Downloading the
> template again replaces that file and **resets every setting to its default,
> including an empty password**. For updates, see [Updating](#updating).

### 2. Create the container

1. Go to **Docker → Add Container**.
2. In the **Template** drop-down, pick **GameLib** under "User templates". Repository is already set to `ghcr.io/<owner>/gamelib:latest`.
3. Fill in the settings:

| Setting | Container side | Default (host side) | Notes |
|---|---|---|---|
| WebUI port | `8080/tcp` | `8080` | Change only the host port if 8080 is taken (e.g. `8095`). The WebUI link follows it. |
| App data | `/data` | `/mnt/user/appdata/gamelib` | DB, covers, temp uploads, backups. |
| Games share | `/games` | `/mnt/user/games` | Your games share. `rw` for uploads/deletes, `ro` for track-only. |
| Username | `GAMELIB_USER` | `admin` | |
| Password | `GAMELIB_PASSWORD` | — | **Required.** The container won't start without it. |
| PUID / PGID | `PUID` / `PGID` | `99` / `100` | nobody:users. *(advanced)* |
| UMASK | `UMASK` | `002` | Use `000` if SMB users can't edit uploaded games. *(advanced)* |
| Trusted origins | `TRUSTED_ORIGINS` | empty | Only for a reverse proxy on another hostname. *(advanced)* |
| Secure cookie | `COOKIE_SECURE` | `0` | `1` only when served over HTTPS. *(advanced)* |
| Max unpacked size | `MAX_UNCOMPRESSED_GB` | `500` | Zip-bomb guard. *(advanced)* |
| Rescan time / interval | `RESCAN_AT` / `RESCAN_INTERVAL_HOURS` | `04:00` / `24` | Scheduled background rescan of the share; `0` hours = off. *(advanced)* |
| Light job threads | `JOB_LIGHT_WORKERS` | `2` | Quick background jobs. Heavy share jobs always run one at a time. *(advanced)* |
| Scan depth | `SCAN_DEPTH` | `3` | How many folder levels Scan looks into (1–6). `3` fits *Group / Category / Game v1*. *(advanced)* |
| SteamGridDB API key | `STEAMGRIDDB_API_KEY` | empty | Optional, for box art. Can also be set in *Settings → Integrations*. *(advanced)* |

4. Click **Apply**. Unraid pulls the image, and the container should turn *healthy* within about a minute.

   If **App data** isn't mapped, the container refuses to start rather than storing your library somewhere it would be lost.
5. Open **WebUI**, log in, then click **Scan** to import what's already on the share.

Before using a large upload, make sure **App data** has free space for the whole upload. The file lands in `/data/tmp` first, then gets unpacked onto the share.

#### Alternative: Docker Compose Manager plugin

Use `docker-compose.yml` from the latest release. It has the same settings and
the image already filled in. Edit the password, ports and paths, then *Compose Up*.

#### Publishing your own copy (fork)

1. **Fork the repository.** Push a version tag (see [Releasing a new version](#releasing-a-new-version-if-you-change-the-code)), and the release workflow publishes `ghcr.io/<your-account>/gamelib` along with release files that point at it.
2. **Make the image public.** New GHCR packages start **private**, even for a public repository. Make yours public once under *your profile → Packages → gamelib → Package settings → Change visibility*.
3. **Or keep it private.** Unraid then needs a login:
   - Create a classic token with only `read:packages`.
   - Run `docker login ghcr.io -u <your-account>` on Unraid.
   - Unraid keeps `/root` in memory, so persist the login across reboots: copy `/root/.docker/config.json` to `/boot/config/docker-auth/`, then add this line to `/boot/config/go`:
     ```bash
     mkdir -p /root/.docker && cp /boot/config/docker-auth/config.json /root/.docker/
     ```

#### Building locally (fallback)

Use this if GitHub is unavailable or you want to test unreleased code.

1. Get the source onto the server, then build it:
   ```bash
   git clone https://github.com/<owner>/gamelib.git /mnt/user/appdata/gamelib-src
   cd /mnt/user/appdata/gamelib-src && sh ./scripts/build.sh
   ```
   This tags `gamelib:<version>` and `gamelib:latest`.
2. Set the template's Repository to `gamelib:latest` (with the repository's own `unraid/gamelib.xml`, also replace the `__OWNER__` / `__REPO__` placeholders, or just set Repository). Apply shows a harmless "pull access denied" message for a local image, and Unraid's update check stays *not available*.

Always build on the Unraid box itself: an image built on a Mac (ARM) won't run on an x86 server. To build on another machine anyway, run the following, then `docker save gamelib:X | ssh root@tower docker load`:

```bash
docker buildx build --platform linux/amd64 -t gamelib:X --load .
```

### Reverse proxy (optional: SWAG, Nginx Proxy Manager)

- **Uploads:** allow large bodies and long uploads. For nginx:
  ```nginx
  client_max_body_size 0;
  proxy_request_buffering off;
  proxy_read_timeout 3600s;
  proxy_send_timeout 3600s;
  ```
- **Downloads:** set `proxy_buffering off;`, or keep `X-Accel-Buffering: no`, which the app already sends.
- **Host header:** write requests are only accepted when the browser's `Origin` matches the `Host` the app receives. Set `TRUSTED_ORIGINS` to exactly what the browser's address bar shows, including any port (for example `games.example.com:8443`), in either of these cases:
  - your proxy rewrites the Host header;
  - you use a non-standard HTTPS port. nginx's `$host`, which SWAG and NPM use, drops the port.

  Alternatively, forward the header with `proxy_set_header Host $http_host;`.
- **HTTPS:** with HTTPS, set `COOKIE_SECURE=1`.

## Updating

Releases are published as `ghcr.io/<owner>/gamelib:<version>` (e.g. `1.1.0`),
`:<major.minor>` (e.g. `1.1`, which moves only on patch releases) and `:latest`.

1. **Read the release notes** (the GitHub Release, or `CHANGELOG.md`) for new settings and breaking changes.
   - **Don't re-download the template.** `my-GameLib.xml` holds your saved settings, and replacing it wipes them.
   - New settings always have working defaults, so you can skip them. To set one, click the GameLib icon → **Edit** → **Add another Path, Port, Variable, Label or Device**, choose *Config Type: Variable*, enter the *Key* from the release notes (for example `RESCAN_AT`) and a value, then **Apply**.
   - Before editing the template by hand, keep a copy:
     ```bash
     cp /boot/config/plugins/dockerMan/templates-user/my-GameLib.xml /boot/config/plugins/dockerMan/my-GameLib.xml.bak
     ```
2. **Pull the update.** In the **Docker** tab, click **Check for updates**, then **Apply update** on GameLib. Unraid pulls the new image and recreates the container. Your data lives in the mapped folders, so nothing is lost.
   - If the check shows *not available* even though a release is out, click the GameLib icon and choose **Force update**.
   - If you rolled back earlier, Repository is still pinned to an old tag. Set it back to `ghcr.io/<owner>/gamelib:latest` first.
3. **Migrations.** On start, the app checks the database schema version (`PRAGMA user_version`).
   - If a migration is needed, it **first copies the database to `/data/backups/gamelib-<date>-pre-v<N>.db`**, then migrates. Both steps are logged.
   - The 5 newest pre-migration backups and the 10 newest manual backups are kept, each counted separately. Routine manual backups never remove a pre-migration backup.
4. The footer of every page shows the running version.

To stay on one minor version and get only bug-fix releases automatically, set Repository to `ghcr.io/<owner>/gamelib:1.1`.

### Rolling back

0. Check that a rollback point exists:
   ```bash
   ls /mnt/user/appdata/gamelib/backups/*pre-v*
   ```
   This is only needed if the update changed the schema.
1. Edit the container and set **Repository** to the previous version, for example `ghcr.io/<owner>/gamelib:1.1.0`. Click **Apply**.
2. If the newer version migrated the database, the older image refuses to start and says the data is newer. In that case:
   1. Stop the container.
   2. Restore the `pre-v<N>` backup:
      ```bash
      cp /mnt/user/appdata/gamelib/backups/gamelib-<date>-pre-v<N>.db /mnt/user/appdata/gamelib/gamelib.db
      ```
   3. Start the container again.

   Anything you changed after the upgrade is lost.

When a fixed release is out, set **Repository** back to `ghcr.io/<owner>/gamelib:latest` so normal updates work again.

Every published version is listed on the repository's **Packages** page (`gamelib`). The first one published to GHCR is 1.1.0; 1.0.x existed only as local builds.

### Releasing a new version (if you change the code)

1. Bump `__version__` in `app/version.py`.
2. If the schema changes, **append** a migration to `MIGRATIONS` in `app/db.py`. Never edit existing ones.
3. Add a `CHANGELOG.md` entry and run the tests (`python -m pytest -q`).
4. Commit, tag and push. The tag must match `app/version.py`:
   ```bash
   git commit -am "Release 1.2.0" && git tag -a v1.2.0 -m "GameLib 1.2.0" && git push origin main v1.2.0
   ```
5. GitHub Actions (`.github/workflows/release.yml`) runs the tests, checks the tag, builds amd64 and arm64 images, and pushes `:1.2.0`, `:1.2` and `:latest`. It then creates a GitHub Release with that version's `CHANGELOG.md` section as notes, and attaches `gamelib.xml` and `docker-compose.yml` with the owner filled in. Progress is under the repository's **Actions** tab.
   - A tag like `v1.2.0-rc1` publishes `:1.2.0-rc1` only, without moving `:latest`. Use it to try a release on Unraid first.

## Backup

**What to back up:** `/mnt/user/appdata/gamelib`. It holds the database, covers and the secret key. The games themselves are on your games share, which you back up separately.

- **Recommended: Appdata Backup plugin** (Community Applications). It stops the container, copies `appdata/gamelib` and starts it again. Just include this folder.
- **Manual backup while running.** Writes a consistent copy to `/data/backups/`:
  ```bash
  docker exec -u 99:100 GameLib python -m app.backup
  ```
  The container name is the one shown in the Docker tab, `GameLib` with the template or `gamelib` with compose. Keep `-u 99:100`, otherwise the file is owned by root.
- **Manual backup while stopped:** stop the container, then copy the folder:
  ```bash
  cp -a /mnt/user/appdata/gamelib /mnt/user/backups/gamelib-$(date +%F)
  ```
- **Restore:**
  1. Stop the container.
  2. Put the folder, or just `gamelib.db`, back.
  3. Start the container.
  4. Version folders that no longer exist on the share show as **missing**. Folders that aren't in the restored DB show up in **Scan**.

```
/mnt/user/appdata/gamelib/
├── gamelib.db        SQLite database
├── secret_key        session signing key (keeps you logged in across restarts)
├── covers/           cover images (WebP)
├── backups/          pre-migration (5 kept) + manual (10 kept) DB backups
└── tmp/              uploads in progress (stale ones are cleaned after 24 h, checked hourly)
```

## Security notes

- **Login:** single user, password hashed in memory.
  - Login is throttled to 5 failures per 5 minutes per IP. The limit holds even for parallel attempts. Behind a reverse proxy, all clients share the proxy's IP.
  - The session cookie is `HttpOnly` and `SameSite=Lax`.
  - The page sends anti-framing headers (`X-Frame-Options: DENY`, CSP `frame-ancestors 'none'`).
- **Request bodies:** write requests must have a `Content-Length`, so chunked bodies are refused. The origin, size and login checks run before any request body is read.
- Every write request needs a CSRF token **and** an `Origin` or `Referer` matching the app's own host and port. Cross-origin writes are rejected, including from other ports on the same server.
- Zip uploads are checked before anything is extracted. The following are rejected:
  - absolute paths, drive letters and `..` traversal
  - symlinks
  - archives that would unpack beyond `MAX_UNCOMPRESSED_GB`
- Extraction happens in a hidden staging folder, which is removed on failure.
- **Folder paths:** every folder path is resolved and must stay inside `/games`, and symlinks that point outside are refused. "Delete files" also refuses any path that goes through a symlink. The app only chowns `/data`, never your share.
- **Unpacking:** execute bits stored in a zip made on Unix are kept, so Linux and macOS game binaries stay runnable. Known launchers (`.x86_64`, `.sh`, `.AppImage`…) are made executable even from Windows-made zips.
- Meant for a home LAN. Put it behind a reverse proxy with HTTPS before exposing it to the internet.

## Development

```bash
python3.12 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest -q
```

Run locally:

```bash
DATA_DIR=./dev/data GAMES_DIR=./dev/games GAMELIB_PASSWORD=demo .venv/bin/gunicorn -c gunicorn.conf.py -b 127.0.0.1:8099 'app:create_app()'
```

## Troubleshooting

- **"denied" / "unauthorized" when pulling:** the image is private. Either make the package public (see [Publishing your own copy](#publishing-your-own-copy-fork)) or set up the GHCR login described there.
- **Template still shows `__OWNER__`:** you used the repository's copy of `gamelib.xml`. Download it from the latest release instead, or edit Repository by hand.
- **Container stops right away:** check the logs. There are two likely causes:
  - `GAMELIB_PASSWORD is not set` means you need to fill in Password.
  - `/data is not mapped` means you need to set the App data path.
- **`exec /app/entrypoint.sh: no such file or directory`:** the image was built with an older build that had Windows line endings. Rebuild with `sh ./scripts/build.sh`.
- **"/games is not writable" in the logs:** the share isn't writable for 99:100. Run *Tools → New Permissions* on the share, set UMASK to `000`, or mount `/games` read-only if you only want tracking.
- **Port already in use:** change the host side of *WebUI port* only.
- **403 "Cross-origin request rejected" behind a proxy:** add the proxy hostname to `TRUSTED_ORIGINS`.
- **Upload fails near the end:** `/data` ran out of space, or a reverse proxy limit was hit (see above).
