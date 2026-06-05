# TMDb Helper — Enhanced Fork

**Current version:** v6.0.81 &nbsp;|&nbsp; **Base addon:** [plugin.video.themoviedb.helper](https://github.com/jurialmunkey/plugin.video.themoviedb.helper) &nbsp;|&nbsp; **Skin:** Titan Bingie Mod v2.2.3

> A heavily extended fork of TMDb Helper focused on reliable widget refresh, MDBList integration, Simkl integration, and three-way cross-service sync (Trakt ↔ MDBList ↔ Simkl).

---

## Table of Contents

- [Features](#features)
- [Changelog](#changelog)
- [Known Limitations](#known-limitations)
- [Architecture Notes](#architecture-notes)

---

## Features

### Widget Reliability
- Instant widget refresh after every Trakt/MDBList/Simkl state change (mark watched, rate, scrobble, etc.)
- Disk-based widget directory cache — survives spurious skin unloads (e.g. after YouTube trailers) without refetching
- CronJob poll interval reduced to 180s; startup cache serves instantly from disk
- Hub widget badges update in real time via `Skin.SetString()` — no navigation required

### MDBList Service (v6.0.31–6.0.80)
- **Auth:** OAuth2 device-code flow (separate from API key; Bearer token required for all user endpoints)
- **Watched indicators:** Local `MDbListSync.db` unioned with Trakt; delta sync via `/sync/last_activities`
- **Context menu:** Mark watched/unwatched, watchlist, collection, rating (movies/shows/seasons/episodes)
- **Scrobbler:** `POST /scrobble/pause` on pause; `POST /sync/watched` at ≥75% completion
- **Menu widgets:** Watchlist, Recently Watched Movies, Recently Watched TV Shows, Recently Watched Combined, In-Progress Movies, In-Progress TV Shows, In-Progress Combined
- **Recently Watched Movies fix:** MDBList's `GET /sync/watched` is episode-only (movies array always empty — confirmed by probing 1,500 items). Movie history reads from local `MDbListSync.db` instead, populated by the scrobbler and context menu writes
- **Service isolation:** MDBList personal views show only MDBList badge data (not cross-service union) to prevent badge inflation from other services

### Simkl Service (v6.0.69–6.0.78)
- **Auth:** PIN OAuth2 using `script.simkl`'s published client ID — no separate app registration
- **Watched indicators:** Local `SimklSync.db` unioned with Trakt + MDBList
- **Context menu:** Mark watched/unwatched, plan-to-watch, hold, drop (`notinteresting`)
- **Scrobbler:** `POST /scrobble/pause` on pause; `POST /sync/history` at ≥75%; `DELETE /sync/playback/{id}` clears server resume point
- **Show mark-watched:** Uses `POST /sync/add-to-list {"to":"completed"}` (correct show-level endpoint)
- **Menu widgets:** Plan to Watch, Recently Watched, In-Progress (Movies / TV Shows / Combined)

### Cross-Service Sync (v6.0.66–6.0.78)
- **On-action mirror:** Any context menu action on one service is silently mirrored to all other enabled services
- **Bridge sync:** Full three-way library reconciliation at startup and every 15 minutes — catches changes made on websites or other apps outside Kodi

| Action | Trakt↔MDBList | Trakt↔Simkl | MDBList↔Simkl |
|--------|:---:|:---:|:---:|
| Watched / Unwatched | ✅ | ✅ | ✅ |
| Watchlist | ✅ | ✅ add-only | ✅ add-only |
| Rating | ✅ | — | — |
| Collection | ✅ | — | — |
| Progress (resume) | Trakt→MDBList | Trakt→Simkl | — |

### Other Enhancements
- **Awards DB:** `awards.json` (2.4MB) migrated to SQLite — O(1) lookups, ~zero RAM
- **Anime enrichment:** Widget episode titles/plots/thumbs overlaid from Seren's `animeCache.db` (zero API calls)
- **Asian drama aliases:** CJK + romanized title variants passed to Seren for better source matching
- **Install players from zip:** Settings → Players → Install players now accepts local `.zip` files
- **Parallel ratings fetch:** 6 sequential API calls → parallel `SafeThread`; 250ms–1.5s → 50–300ms
- **Settings backup/restore:** Timestamped zip of settings, DBs, widget cache, players
- **Package size:** ~3.1MB savings (stripped GIMP source, minimal `dummy.mp4`)

---

## Changelog

### v6.0.81 — Simkl Cross-Device Movie Resume Sync

`SimklSyncPaused` reads movie resume positions directly from `GET /sync/playback` and stores them in `SimklSync.db`. Previously `paused_progress` was only written by the local scrobbler — movies paused on another device (another Kodi box, another Simkl-enabled app) would not show a resume bar on home screen widgets.

**Design decisions:**
- **Movies only** — Simkl returns TVDB episode numbers for episode entries. Writing those to `SimklSync.db` (which is keyed by TMDb numbers) would miss on `set_playprogress()` lookup or hit the wrong episode for anime. Episode resume is scrobbler-owned (correct TMDb numbers) and untouched. Episode support deferred until `_simkl_resolve_episode` is built.
- **Clear movies-only** — `clear_columns()` wipes only `item_type='movie'` rows so scrobbler-written episode rows are preserved across syncs.
- **180s expiry** — Simkl's `/sync/activities` has no `paused_at` field. A local UTC timestamp is stored after each sync and the data is considered stale after 180 seconds (matching CronJob interval).
- **`_pauseplayprogress` gate** — Playback sync is independent of watched indicators. `pre_sync()` now checks `_pauseplayprogress` separately so users who disable watched badges still get resume bars, and vice versa.

| File | Change |
|------|--------|
| `lib/api/simkl/sync/database.py` | `paused_progress`/`paused_at`/`paused_id` wired to `SimklSyncPaused` |
| `lib/api/simkl/sync/datatype.py` | New `SimklSyncPaused` class — `GET /sync/playback`, 180s local-timestamp expiry, movie-only `clear_columns` |
| `lib/api/simkl/sync/itemdata.py` | `_parse_playback()` — movies only, episode entries explicitly skipped with comment |
| `lib/items/simkl.py` | `pre_sync()` restructured — watched indicators and play-progress gated independently |

---

### v6.0.80 — MDBList Recently Watched Combined Fix
**Root cause:** The "Combined" view (`tmdb_type='both'`) was fetching shows from the MDBList API (correct) but had no source for movies — MDBList's `GET /sync/watched` never returns movies.

**Fix:**
- Combined view now merges movies from `MDbListSync.db` + shows from MDBList API, sorted by `last_watched_at` descending
- `_normalize_watched()` now preserves `last_watched_at` in show items (needed for cross-type sort)
- `container.py` `make_item()` now uses service-specific playcount for MDBList and Simkl personal views instead of cross-service `max()` — prevents Trakt/other service badge inflation inside MDBList and Simkl menus

| File | Change |
|------|--------|
| `lib/api/mdblist/lists.py` | Combined path merges `MDbListSync.db` movies + MDBList API shows, sorted by date |
| `lib/items/container.py` | Service-isolated badge routing for `mdblist_*` and `simkl_*` personal info views |

---

### v6.0.79 — MDBList Recently Watched Movies Fix
**Root cause (confirmed by live API probe):** `GET /sync/watched` returns only TV episode history. Movies are structurally absent regardless of pagination depth (1,500 items probed, zero movies). `GET /sync/watched/movies` → 404. `?type=movie` is silently ignored.

**Fix:** Movie recently-watched reads from `MDbListSync.db` (populated by scrobbler and context menu writes via `INSERT OR IGNORE` + `UPDATE`) instead of calling the MDBList API.

**Note:** Bridge sync's `_mdb_watched_ids()` has the same limitation — always gets 0 movies → re-pushes the full Trakt movie library to MDBList every 15 minutes. Fix deferred to v6.0.81.

| File | Change |
|------|--------|
| `lib/api/mdblist/lists.py` | Movie history reads from `MDbListSync.db`; TV show history unchanged (MDBList API) |

---

### v6.0.78 — Simkl Phase F: Cross-Service Sync

**Build 1 — On-Action Mirror**

All context menu sync actions fan out to all enabled services. Loop safety: mirror functions call APIs directly without instantiating `Item*` sync classes (which would echo back infinitely).

**Build 2 — Bridge Sync (three-way)**

`BridgeSync` extended from Trakt↔MDBList to Trakt↔MDBList↔Simkl:
- Auth gate: Trakt + at least one of MDBList or Simkl
- Watched / watchlist: three-way additive union
- Progress: Trakt → MDBList + Trakt → Simkl (MDBList/Simkl → Trakt impossible — Trakt's `/sync/playback` is read-only)
- Collection: Trakt ↔ MDBList only (Simkl has no collection endpoint)

**Confirmed working (2026-06-03):**
```
BridgeSync →MDBList: 295 movies, 87 shows (watched)
BridgeSync →Simkl:   311 movies, 86 shows (watched)
BridgeSync →Trakt:   12 movies (watched)
BridgeSync →Simkl:   4 movies + 4 shows (plantowatch)
BridgeSync: reconciliation complete
```

| File | Change |
|------|--------|
| `lib/script/sync/cross_sync.py` | Simkl mirror helpers added |
| `lib/script/sync/basic.py` | `ItemWatched/Unwatched/Watchlist` extended to Simkl |
| `lib/script/sync/simkl_sync.py` | `_cross_sync()` overrides |
| `lib/script/sync/mdblist_sync.py` | MDBList items extended to Simkl |
| `lib/monitor/bridge_sync.py` | Full three-way rewrite |
| `lib/monitor/cronjob.py` | Bridge gate updated |

---

### v6.0.77 — Simkl Phase E: Menu Widgets

Three home-screen widget entries under Settings → Browse → Simkl:

| Widget | `info=` | Endpoints |
|--------|---------|-----------|
| Plan to Watch | `simkl_plantowatch` | GET `/sync/all-items/movies/plantowatch` + `/sync/all-items/shows/plantowatch` |
| Recently Watched | `simkl_history` | GET `/sync/all-items/movies/completed` + `/sync/all-items/shows/completed`; sorted newest-first |
| In-Progress | `simkl_upnext` | GET `/sync/playback`; sorted by `paused_at` desc; episode entries deduplicated to show-level |

| File | Change |
|------|--------|
| **New: `lib/api/simkl/lists.py`** | `ListPlanToWatch`, `ListHistory`, `ListInProgress` |
| `lib/api/simkl/api.py` | `get_paginated()` method |
| `lib/addon/consts.py` | Simkl routes in `ROUTE_NOID` |
| `lib/items/basedir.py` | `_get_basedir_simkl()` + `dir_simkl` dispatch |

---

### v6.0.76 — Simkl Phase D: Scrobbler

`SimklScrobbler` runs alongside Trakt and MDBList scrobblers for every playback.

| Event | Behaviour |
|-------|-----------|
| Start | `started=True` — no API call (Simkl has no `/scrobble/start`) |
| Pause | `POST /scrobble/pause`; writes `paused_progress`/`paused_at`/`paused_id` to `SimklSync.db` |
| Stop ≥75% | `POST /sync/history`; writes `plays=1`/`last_watched_at`; `DELETE /sync/playback/{id}` |
| Stop <75% | `POST /scrobble/pause` — server-only save |

**Confirmed working:** Interstellar stopped at 89.65% → `SimklSync.db`: `plays=1`, `last_watched_at=2026-06-03T01:10:13Z`, pause columns cleared.

---

### v6.0.71–6.0.75 — Simkl Phase C: Context Menu Writes

Five actions in "Simkl options" submenu: mark watched, mark unwatched, plan-to-watch, hold, drop. Movie + show + season scope.

**API notes (confirmed by live probe):**
- TMDb IDs accepted by both `/sync/history` and `/sync/add-to-list` — `not_found` arrays empty
- Shows use `POST /sync/add-to-list {"to":"completed"}` (not `/sync/history`)
- Drop uses `"notinteresting"` not `"dropped"` — confirmed by 400 response

| Version | Fix |
|---------|-----|
| 6.0.72 | Drop payload: `"notinteresting"` not `"dropped"`. Plan-to-watch/hold upgraded to full toggles via `POST /sync/history/remove` |
| 6.0.73 | Show watched toggle: `watched_episodes=9999` sentinel makes badge appear immediately; next sync overwrites with real count |
| 6.0.74 | Same `9999` sentinel applied to MDBList show toggle |
| 6.0.75 | MDBList show toggle reads `last_watched_at` not `plays` (show-level `plays` is always 0) |

---

### v6.0.69–6.0.70 — Simkl Phases A–B: Auth + Watched Indicators

**Phase A:** PIN OAuth2 using `script.simkl`'s published client ID. `SimklAPI` with Bearer + `simkl-api-key` headers. Settings → Accounts → Simkl group.

**Phase B:** `SimklSync.db` with `plays`, `last_watched_at`, `watched_episodes` columns. Syncs via `POST /sync/all-items/movies/completed` + `POST /sync/all-items/shows/?extended=full`. Watched badges unioned with Trakt and MDBList. Activities track `movies.completed`, `tv_shows.all`, `anime.all` separately.

**Confirmed working:** 96 movies + 9 shows synced in 3.3s; watched badges visible on home screen.

---

### v6.0.66–6.0.68 — Cross-Service Sync: Trakt ↔ MDBList

**v6.0.66–6.0.67 — On-Action Mirror:** Context menu actions mirror silently to the other service. Local DB writes follow confirmed POSTs for watchlist/collection/rating columns (no periodic read-sync for these).

**v6.0.68 — Bridge Sync:** Additive union at startup + every 15 minutes.

| Domain | Direction | Notes |
|--------|-----------|-------|
| Watched | ↔ Both | |
| Watchlist | ↔ Both | |
| Collection | ↔ Both | |
| Progress | Trakt → MDBList | Trakt `/sync/playback` is read-only |
| Ratings | On-action only | No MDBList GET ratings endpoint |

**Confirmed working (2026-06-01):** 292 movies + 85 shows → MDBList; 3 movies → Trakt; 25-second full reconciliation at startup.

---

### v6.0.31–6.0.65 — MDBList Service Phases A–E

All phases complete.

| Phase | Versions | Feature |
|-------|----------|---------|
| A | 6.0.31 | OAuth2 device-code auth + settings |
| B | 6.0.32 | `MDbListSync.db` + watched indicators (union with Trakt) |
| C | 6.0.33–6.0.56 | Context menu: watched, watchlist, collection, rating |
| D | 6.0.49–6.0.60 | `MDbListScrobbler`: pause saves resume; completion marks watched |
| E | 6.0.51–6.0.65 | Menu widgets: Watchlist, Recently Watched, In-Progress |

**Key fixes during development:**

| Version | Fix |
|---------|-----|
| 6.0.34–6.0.35 | Toggle fix: `force_refresh_sync` failed silently (mutex race); replaced with direct DB write |
| 6.0.37 | `plays` column: `INTEGER DEFAULT 0` makes `ifnull()` a no-op; switched to plain `UPDATE SET plays=?` |
| 6.0.42 | `is_successful_sync` was False (silent API POST failure); `force=True` bypasses guard |
| 6.0.44 | Hub widget refresh: `_reload_bingie_hub_widgets()` updates 88 slot paths via `Skin.SetString()` |
| 6.0.57 | Delta sync: was storing `last_activities['all']` (max) instead of specific key timestamp |
| 6.0.58 | `get_paginated()` strips non-list values before iterating — prevented `'str' object has no attribute 'get'` crash |
| 6.0.65 | Watchlist flat-format fix: `item['id']` not nested `item['movie']['ids']['tmdb']` |

---

### v6.0.26–6.0.30 — Performance & Reliability

| Version | Change |
|---------|--------|
| 6.0.26 | Anime episode enrichment: widget titles/plots/thumbs from Seren's `animeCache.db` (zero API calls) |
| 6.0.27 | CronJob 600s → 180s; image cache eviction (14-day TTL, 100MB cap); widget cache 24h TTL; settings backup/restore |
| 6.0.28–6.0.29 | Security: `restore_settings()` path traversal fix (Windows vectors); dead code cleanup; logout clears all auth state |
| 6.0.30 | Startup hang fix: `is_cache_valid()` returned `False` on `LastInvalidated == 0.0` every restart (~15s hang). Now serves from disk cache if file is < 6h old |

---

### v6.0.12–6.0.25 — Foundation Fixes

#### v6.0.12 — Widget Refresh Root Cause Fixes

Nine bugs fixed. Bug 4 was the primary visible cause.

| # | Severity | Bug |
|---|----------|-----|
| 1 | High | `store_last_activity()` ran before `sync_data()` — stamped complete before fetch finished |
| 2 | High | Scrobble completion never triggered `Container.Refresh` or `Widgets.Reload` |
| 3 | High | `timerlock` blocked forced re-sync even when `forced=True` |
| **4** | **Critical** | **Skin reads `widgetreload-movies` but addon only set `TMDbHelper.Widgets.Reload` — cache-bust never fired** |
| 5 | High | `refresh_containers()` override on `ItemRating` was `pass` — silently killed all rating refreshes |
| 6 | High | `ItemUserList.sync()` missing `reset_lastactivities()` — stale timestamps blocked re-fetch |
| 7 | Low | `like_list()` only refreshed on unlike, not like |
| 8 | Medium | `refresh_listing()` early-returned for add operations |
| 9 | Low | Logout didn't clear `LASTACTIVITIES_DATA` or `TraktIsAuth` |

`reload_widgets()` in `kodi_utils.py` now sets `TMDbHelper.Widgets.Reload`, `widgetreload`, `widgetreload2`, `widgetreload-movies`, `widgetreload-tvshows` — compatible with Titan/Aura/Arctic-family skins. All refresh paths funnel through this single function.

#### v6.0.13–6.0.19 — Widget Cache

Disk-based JSON cache for widget directory listings. Served instantly on re-requests (e.g. after YouTube trailer fullscreen). Invalidated by `LastInvalidated` window property set on every `reload_widgets()` call. Cache key strips `reload=` so the same widget maps to one file.

#### v6.0.17 — Awards SQLite Migration

`awards.json` (2.4MB, loaded/parsed on first browse) migrated to `awards.db` (SQLite, O(1) indexed lookups, ~zero RAM).

#### v6.0.20 — Source Audit Bug Fixes

| # | Bug |
|---|-----|
| 1 | `i.get('created_at')[:10]` crashes on None |
| 2 | `now_value / max_value` ZeroDivisionError on empty sync list |
| 3 | `progress_now / progress_max` ZeroDivisionError on empty seed |
| 4 | `ZipExtFile` handle never closed — resource leak |
| 5 | Mutable default args (`[]`/`{}`) shared across calls in 4 files |
| 6 | `response.json()` without try/except — crashes on Trakt 5xx HTML |

#### v6.0.23–6.0.25 — Other

| Version | Change |
|---------|--------|
| 6.0.23 | Install players from local `.zip` file (Settings → Players → Install) |
| 6.0.24–6.0.25 | Asian drama aliases: CJK + romanized variants fetched from TMDb and written to `Window(Home).Property('TMDbHelper.AsianDramaAliases')` for Seren title-matching |

---

## Known Limitations

| Limitation | Detail |
|-----------|--------|
| **MDBList movie history from server** | `GET /sync/watched` never returns movies regardless of pagination. Movies are pushed to MDBList via POST but cannot be read back. Widget reads from local `MDbListSync.db` instead — reflects movies watched in Kodi only, not changes made on mdblist.com |
| **Bridge movie re-push (v6.0.81)** | `_mdb_watched_ids()` reads from the same broken endpoint → always diffs all ~400 movies → pushes them to MDBList every 15 min. Harmless but wasteful; fix pending |
| **Bridge sync removes** | Additive only. Unwatching on trakt.tv/mdblist.com directly is not propagated by the bridge. Context menu unwatches ARE mirrored in real time |
| **Progress sync direction** | Trakt → MDBList/Simkl only. Writing resume points back to Trakt requires an active scrobble session — Trakt's `/sync/playback` is read-only. MDBList and Simkl **movie** progress reads from server (cross-device) are implemented; Simkl **episode** progress is scrobbler-local only |
| **Ratings bridge** | No MDBList GET ratings endpoint. Ratings only sync via on-action mirror (context menu) |
| **Simkl episode numbering** | Scrobbler and context menu use TMDb episode numbers. For anime where TVDB and TMDb numbering diverge, episode marks may land on the wrong episode. Full TVDB resolution (external_ids + airdate fallback) is not yet implemented |
| **Hub widget static URLs** | Hub slot refresh only works when the slot's path string contains a `reload=` token. Fully static paths update on next navigation |
| **Simkl `notinteresting` status removal** | `_parse_watched()` omits episode-level rows due to TVDB/TMDb mismatch — episode dots in show progress are not tracked in `SimklSync.db` |

---

## Architecture Notes

**Fix in TMDb Helper, not the skin:** Titan Bingie Mod receives regular skin updates. Changes made to skin files would not survive. TMDb Helper sets the same properties the skin already reads (`widgetreload-movies` etc.).

**Widget cache is disk-based, not memory:** Each widget request spawns a separate `plugin.py` process. Window properties are used for cross-process invalidation (`LastInvalidated`); the filesystem is the cross-process data store.

**MDBList local DB is sparse:** `MDbListSync.db` contains only rows for items the user has interacted with in Kodi (browsed, scrobbled, or used context menu on). It is not a full mirror of the MDBList account. The bridge sync reads MDBList's live state from the API to get the complete picture.

**Direct DB writes for toggles:** MDBList's `is_expired()` always returns `False` after the first sync (it compares any key timestamp against `last_activities['all']` which is always equal or larger). Periodic re-sync never fires for individual keys. All toggle state is written directly to `MDbListSync.db` after each API POST so context menu labels reflect the new state immediately.

**`reset_lastactivities()` intentionally omitted for MDBList watched:** Calling it triggers `MDbListSyncWatched.sync()` on the next `Container.Refresh`, which calls `clear_columns()` and wipes the `plays=1` that was just written.

**Cross-sync loop safety:** Mirror functions in `cross_sync.py` call API endpoints directly and never instantiate `Item*` sync classes. Instantiating `ItemMDbListWatched.sync()` from inside a Trakt sync would call `mirror_watched_to_trakt()` again and echo forever.

**Hub widget refresh via `Skin.SetString()`:** Hub containers source their content from `$INFO[Skin.String(key.path)]`. When `Skin.SetString()` updates the value, Kodi's info manager marks the dependency stale and `CDirectoryProvider` re-fetches immediately. The `reload=` token baked into the path string is what makes this possible.
