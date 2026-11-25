# Changelog

## Unreleased

- **Public:** the repository and image are public, so Unraid pulls `ghcr.io/<owner>/gamelib` without a registry login.
- **Release files:** each GitHub Release has a ready-to-use `gamelib.xml` (Unraid template) and `docker-compose.yml` attached, with the owner filled in. The repository copies use `__OWNER__` / `__REPO__` placeholders.
- **Template updates:** the template's TemplateURL points at the latest release, so Unraid can pick up template changes.
- **Compose:** the example timezone defaults to `Etc/UTC`.

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
