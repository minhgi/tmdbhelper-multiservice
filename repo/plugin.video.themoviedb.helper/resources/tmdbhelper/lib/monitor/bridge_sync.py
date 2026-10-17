"""
Bidirectional Trakt ↔ MDBList ↔ Simkl library reconciliation (bridge sync).

A port of Seren's snapshot-delta bridge (Seren modules/bridgeSync.py).

Complements the on-action mirror in cross_sync.py:
  - on-action mirror: real-time, fires on Kodi context menu actions (including removes)
  - bridge sync: catches additions made outside Kodi (web clients, apps)

Strategy - snapshot-delta, not a raw current-state diff:
  Each (domain, service, media) keeps a persisted snapshot of the items already accounted for
  (addon_data/bridge_snapshot_v1.json). Every run:  newly = current - snapshot, and only the
  newly added items are pushed to the services that are missing them. An item enters the next
  snapshot only once it is confirmed present everywhere (already there, or the push succeeded);
  a failed push leaves it out so it is retried. Items that vanish from a service drop out of its
  snapshot, so a later genuine re-add is recognised as new.
  This is what stops a removal on service A (made outside Kodi) from being undone by service B's
  old copy: B's copy is old news, not a new addition.

  A service whose read failed (or whose Trakt data is not fully synced yet) is skipped for the
  cycle - as source and as target - and its snapshot is left untouched. A failed read is never
  treated as "empty".

Domains (Seren's scope, widened to Simkl at the user's request - Seren itself keeps Simkl to movies):
  Watched movies:   Trakt <-> MDBList <-> Simkl.
  Watched episodes: Trakt <-> MDBList <-> Simkl (Simkl's shows bucket only - its anime bucket uses
                    different episode numbering and is left out). Whole shows are never pushed - a
                    show-level mark means "every episode".
  Watchlist:        Trakt <-> MDBList <-> Simkl plan-to-watch. An item Simkl already has in another
                    status (completed, watching, hold, dropped, not interested) is never overwritten.
  Collection:       Trakt <-> MDBList only (Simkl has no collection endpoint).

Simkl is read only when /sync/activities says it changed since the last cycle (Simkl asks apps not to
poll its lists unconditionally); otherwise the views saved from the last cycle are used. Simkl writes
are spaced 21 s apart (its per-user write lock) and capped per cycle. Episodes Simkl only holds as
synthetic rows (completed shows without per-episode records) are never sent to Trakt history.
  Progress (resume points): Trakt -> MDBList and Trakt -> Simkl only, resent every run.
  Ratings: on-action mirror only.

Trakt history pushes (watched movies / episodes) add a new play on every POST, so they carry
the source's real watch date and go out at most _HISTORY_CAP items per cycle.

Auth gate: Trakt must be authorized plus at least one of MDBList or Simkl.
Loop safety: no Item* classes instantiated here. Direct API + DB only.
"""

import json
import os
import re

from tmdbhelper.lib.addon.logger import kodi_log
from tmdbhelper.lib.addon.plugin import ADDONDATA, get_setting

_LAST_RUN_PROP = 'TMDbHelper.BridgeSync.LastRun'
_RUNNING_PROP = 'TMDbHelper.BridgeSync.Running'
_DEFAULT_INTERVAL_MINUTES = 180   # same cadence as Seren's bridge
_BATCH_SIZE = 100     # max items per Trakt / Simkl call
_MDB_CHUNK = 50       # max items per MDBList call (Seren's live-tested chunk size)
_SIMKL_BASE = 'https://api.simkl.com'
# Simkl names the Dropped status per app: this addon's token reads back 'notinteresting', a Seren-app token 'dropped'
# (live-probed 2026-10-14). Both mean the user dropped it.
_SIMKL_DROPPED = ('dropped', 'notinteresting')
_SNAPSHOT_FILE = 'bridge_snapshot_v1.json'   # addon_data: what has been reconciled. Do NOT delete it once the bridge has run (copies it pushed would look new)
_HISTORY_CAP = 50     # max Trakt history pushes (each one is a new play) per domain per cycle
_MDB_EPISODE_CAP = 250   # max MDBList episode pushes per cycle (API-call budget while the first runs converge)
_SIMKL_CAP = 300      # max Simkl pushes per domain per cycle (<= 3 calls: Simkl locks writes for 20 s per user)
_SIMKL_LOCK_SECONDS = 21
_TARGET_NAMES = {'mdblist': 'MDBList', 'simkl': 'Simkl', 'trakt': 'Trakt'}
_STAMP_RE = re.compile(r'^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}')


class BridgeSync:

    def __init__(self, trakt_api, mdblist_api, simkl_api=None):
        self.trakt = trakt_api
        self.mdb = mdblist_api
        self.simkl = simkl_api
        self._snapshots = None     # {label: {item_key: tmdb_id}} loaded lazily from _SNAPSHOT_FILE
        self._snapshots_ok = True  # False when the snapshot file can't be read/written: no pushes
        self._meta = {}            # persisted beside the snapshots: Simkl's last-seen activity stamp + its saved views
        self._simkl = None         # this run's Simkl views (see _simkl_prepare); None = Simkl unreadable
        self._simkl_prepared = False
        self._simkl_loaded = False    # True when this run re-read Simkl (not the saved views)
        self._simkl_last_write = 0.0
        self._stamps = {}          # {(media, item_key): newest last_watched_at seen on MDBList / Simkl}
        self._mdb_watched_cache = None   # one MDBList /sync/watched read per run (movies + episodes)
        self._pushed = {}          # {service: items confirmed pushed to it this run}, for the finishing notification

    # ── Per-service auth checks ────────────────────────────────────────────────

    @property
    def _mdb_ok(self):
        return bool(self.mdb and self.mdb.user_token.value)

    @property
    def _simkl_ok(self):
        return bool(self.simkl and self.simkl.user_token.value)

    def _is_authorized(self):
        """Trakt must be authorized, plus at least one other service."""
        if not self.trakt.authorization:
            return False
        return self._mdb_ok or self._simkl_ok

    # ── Interval gate ──────────────────────────────────────────────────────────

    def should_run(self):
        import time
        from jurialmunkey.window import get_property
        last = float(get_property(_LAST_RUN_PROP) or 0)
        minutes = get_setting('cross_sync_interval', 'int') or _DEFAULT_INTERVAL_MINUTES
        return (time.time() - last) >= (minutes * 60)

    def _mark_done(self):
        import time
        from jurialmunkey.window import get_property
        get_property(_LAST_RUN_PROP, set_property=str(time.time()))

    # ── Entry point ────────────────────────────────────────────────────────────

    def run(self, force=False):
        from jurialmunkey.window import get_property
        if get_property(_RUNNING_PROP):
            return
        if not force and not self.should_run():
            return
        if not self._is_authorized():
            kodi_log('BridgeSync: skipped — Trakt or all secondary services not authorized', 2)
            return
        get_property(_RUNNING_PROP, set_property='true')
        self._mdb_watched_cache = None
        self._simkl_prepared = False
        self._pushed = {}
        ui = self._ui_open()
        try:
            kodi_log('BridgeSync: starting reconciliation', 1)
            steps = ((self._sync_watched_movies, 'Watched movies'), (self._sync_watched_episodes, 'Watched episodes'),
                     (self._sync_watchlist, 'Watchlist'), (self._sync_collection, 'Collection'),
                     (self._sync_progress, 'Resume points'))
            for n, (fn, label) in enumerate(steps):
                self._ui_update(ui, n * 100 // len(steps), label)
                try:
                    fn()
                except Exception as exc:
                    kodi_log(f'BridgeSync: {fn.__name__} error: {exc}', 2)
            self._simkl_finish()
            self._mark_done()
            kodi_log('BridgeSync: reconciliation complete', 1)
            self._ui_close(ui)
            ui = None
            self._ui_summary()
        finally:
            self._ui_close(ui)
            get_property(_RUNNING_PROP, clear_property=True)

    # ── Notifications ──────────────────────────────────────────────────────────
    # Like the Trakt / MDBList / Simkl syncs they follow the 'sync progress notifications' setting, and none is shown
    # while a video is playing (the sync itself is unaffected). Failures here are logged and never stop the sync.

    @staticmethod
    def _video_playing():
        try:
            from xbmc import Player
            return Player().isPlayingVideo()
        except Exception:
            return False

    def _ui_open(self):
        """Background progress bar for the run, or None (setting off, video playing, or no dialog)."""
        if self._video_playing():
            return None
        try:
            from tmdbhelper.lib.addon.dialog import DialogProgressSyncBG
            ui = DialogProgressSyncBG()
            ui.heading = 'Cross-Service Sync'
            ui.create()     # does nothing when the setting is off
            return ui
        except Exception as exc:
            kodi_log(f'BridgeSync: progress dialog error: {exc}', 2)
            return None

    @staticmethod
    def _ui_update(ui, progress, message):
        if ui is None:
            return
        try:
            ui.update_or_create(progress, message)
        except Exception as exc:
            kodi_log(f'BridgeSync: progress dialog update error: {exc}', 2)

    @staticmethod
    def _ui_close(ui):
        if ui is None:
            return
        try:
            ui.close()
        except Exception as exc:
            kodi_log(f'BridgeSync: progress dialog close error: {exc}', 2)

    def _ui_summary(self):
        """One toast at the end of a run that actually changed something on a service (nothing for a quiet run)."""
        total = sum(self._pushed.values())
        if not total or not get_setting('sync_notifications') or self._video_playing():
            return
        try:
            from xbmcgui import Dialog
            from tmdbhelper.lib.addon.plugin import ADDONPATH
            parts = ', '.join(f'{_TARGET_NAMES.get(t, t)} {n}' for t, n in sorted(self._pushed.items()))
            Dialog().notification('Cross-Service Sync', f'{total} item{"s" if total != 1 else ""} synced ({parts})',
                                  icon=f'{ADDONPATH}/icon.png', time=5000)
        except Exception as exc:
            kodi_log(f'BridgeSync: summary notification error: {exc}', 2)

    # ── Trakt local DB readers (DB is kept fresh by CronJob) ───────────────────
    # Every reader returns None on failure (never an empty dict that would look like "Trakt has nothing").

    def _trakt_rows(self, keys, item_type, extra_condition):
        try:
            cache = self.trakt.trakt_syncdata.cache
            rows = cache.get_list_values(
                keys=keys,
                values=(item_type,),
                conditions=f'item_type=? AND tmdb_id IS NOT NULL AND {extra_condition}')
            return list(rows or [])
        except Exception as exc:
            kodi_log(f'BridgeSync: trakt rows({item_type},{extra_condition}) error: {exc}', 2)
            return None

    def _trakt_ids(self, item_type, clause_key):
        """{tmdb_id_str: tmdb_id} from the Trakt DB where item_type=type AND clause_key IS NOT NULL, or None."""
        rows = self._trakt_rows(('tmdb_id',), item_type, f'{clause_key} IS NOT NULL')
        if rows is None:
            return None
        return {str(r[0]): int(r[0]) for r in rows if r and r[0]}

    def _trakt_episode_keys(self):
        """{'show:season:number': show_tmdb_id} for every watched episode in the Trakt DB, or None.
        Specials (season 0) are left out on both sides: the Trakt sync does not store them, so
        they would all look missing from Trakt."""
        rows = self._trakt_rows(
            ('tmdb_id', 'season_number', 'episode_number'), 'episode',
            'last_watched_at IS NOT NULL AND season_number > 0 AND episode_number IS NOT NULL')
        if rows is None:
            return None
        return {f'{r[0]}:{r[1]}:{r[2]}': int(r[0]) for r in rows if r and r[0]}

    def _trakt_progress_rows(self):
        """Return (movie_rows, episode_rows) from Trakt DB for progress sync."""
        movies, episodes = [], []
        try:
            cache = self.trakt.trakt_syncdata.cache
            movies = cache.get_list_values(
                keys=('tmdb_id', 'playback_progress'),
                values=('movie',),
                conditions='item_type=? AND playback_progress IS NOT NULL AND tmdb_id IS NOT NULL') or []
            episodes = cache.get_list_values(
                keys=('tmdb_id', 'playback_progress', 'season_number', 'episode_number'),
                values=('episode',),
                conditions=(
                    'item_type=? AND playback_progress IS NOT NULL'
                    ' AND tmdb_id IS NOT NULL'
                    ' AND season_number IS NOT NULL AND episode_number IS NOT NULL')) or []
        except Exception as exc:
            kodi_log(f'BridgeSync: trakt_progress_rows error: {exc}', 2)
        return movies, episodes

    # ── MDBList API readers ────────────────────────────────────────────────────

    def _mdb_get(self, *path_parts, **params):
        """GET from MDBList API with Bearer token. Returns parsed JSON or None."""
        try:
            url = self.mdb.get_request_url(*path_parts, **params)
            resp = self.mdb.get_api_request(url, headers=self.mdb.auth_headers)
            if resp is None:
                return None
            return resp.json()
        except Exception as exc:
            kodi_log(f'BridgeSync: mdb_get{path_parts} error: {exc}', 2)
            return None

    def _mdb_get_paginated(self, *path_parts, max_pages=50):
        """Walk a shared-cursor MDBList sync endpoint (single offset/limit window
        spanning movies+shows+seasons+episodes combined — confirmed via MDBList's
        API spec) to completion via has_more, merging each page's arrays.

        Endpoints like /sync/watched, /sync/collection and /watchlist/items
        default to limit=1000 with no offset if called unparameterised — on an
        episode-heavy account, movies can sort outside that first page and be
        silently missed. Bounded to max_pages (50 * 1000 = 50,000 items) as a
        safety cap against a malformed has_more loop.

        Returns None if any page fails or the page budget runs out: a failed read must
        not look like an empty MDBList. A successful read of an empty account is a real {}.
        """
        combined = {}
        offset = 0
        for _ in range(max_pages):
            page = self._mdb_get(*path_parts, offset=offset, limit=1000)
            if not isinstance(page, dict):
                return None
            for key in ('movies', 'shows', 'seasons', 'episodes'):
                if page.get(key):
                    combined.setdefault(key, []).extend(page[key])
            if not (page.get('pagination') or {}).get('has_more'):
                return combined
            offset += 1000
        return None

    def _mdb_watched_raw(self):
        """GET /sync/watched to completion, once per run. None if the read failed."""
        if self._mdb_watched_cache is None:
            self._mdb_watched_cache = self._mdb_get_paginated('sync', 'watched')
        return self._mdb_watched_cache

    def _mdb_watched_movies(self):
        """{tmdb_id_str: tmdb_id} of watched movies on MDBList, or None if the read failed.
        The newest last_watched_at of each is remembered for the push to Trakt / Simkl."""
        raw = self._mdb_watched_raw()
        if raw is None:
            return None
        result = {}
        for item in (raw.get('movies') or []):
            tmdb_id = ((item.get('movie') or {}).get('ids') or {}).get('tmdb')
            if tmdb_id:
                result[str(tmdb_id)] = int(tmdb_id)
                self._note_stamp('movie', str(tmdb_id), item.get('last_watched_at'))
        return result

    def _mdb_watched_episodes(self):
        """{'show:season:number': show_tmdb_id} of watched episodes on MDBList, or None if the read failed.
        Whole-show / whole-season marks have no episode rows and so do not appear here."""
        raw = self._mdb_watched_raw()
        if raw is None:
            return None
        result = {}
        for item in (raw.get('episodes') or []):
            ep = item.get('episode') or {}
            show = ep.get('show') if isinstance(ep.get('show'), dict) else {}
            tmdb_id = (show.get('ids') or {}).get('tmdb')
            season, number = ep.get('season'), ep.get('number')
            if not tmdb_id or not season or number is None:    # season 0 (specials) is not bridged
                continue
            key = f'{tmdb_id}:{season}:{number}'
            result[key] = int(tmdb_id)
            self._note_stamp('episode', key, item.get('last_watched_at'))
        return result

    def _mdb_list_ids(self, *path_parts):
        """GET list endpoint → {'movie:<id>' / 'show:<id>': tmdb_id}, or None if the read failed.
        Handles both flat list format and dict with movies/shows/items keys
        (both nested Trakt-style and flat direct-id format).
        sync/collection and watchlist/items share the same shared-cursor pagination contract
        as sync/watched, so this also walks to completion — see _mdb_get_paginated.
        """
        raw = self._mdb_get_paginated(*path_parts)
        if raw is None:
            return None
        result = {}
        if not isinstance(raw, dict):
            return result

        def add(media, tmdb_id):
            if tmdb_id:
                result[f'{media}:{tmdb_id}'] = int(tmdb_id)

        for item in (raw.get('movies') or []):
            add('movie', item.get('id') or ((item.get('movie') or {}).get('ids') or {}).get('tmdb'))
        for item in (raw.get('shows') or []):
            add('show', item.get('id') or ((item.get('show') or {}).get('ids') or {}).get('tmdb'))
        for item in (raw.get('items') or []):
            mediatype = str(item.get('mediatype') or item.get('type') or '')
            add('movie' if 'movie' in mediatype else 'show', item.get('id') or item.get('tmdb'))
        return result

    # ── Simkl API readers ──────────────────────────────────────────────────────

    def _simkl_get_ok(self, path):
        """Returns (ok, data): ok is False for a failed request; HTTP 200 with a null / empty body is
        an empty bucket (Simkl answers that way for a bucket with nothing in it)."""
        try:
            sep = '&' if '?' in path else '?'
            url = f'{_SIMKL_BASE}{path}{sep}client_id={self.simkl.client_id}'
            resp = self.simkl.get_api_request(url, headers=self.simkl.auth_headers)
            if resp is None:
                return False, None
            try:
                return True, resp.json()
            except (ValueError, AttributeError):
                return (not (getattr(resp, 'text', None) or '').strip()), None
        except Exception as exc:
            kodi_log(f'BridgeSync: simkl_get({path}) error: {exc}', 2)
            return False, None

    # Simkl views: {'movies': {tmdb: [status, last_watched_at]}, 'shows': {tmdb: [status, last_watched_at]},
    #   'anime': [tmdb of anime shows], 'ep_yes': {'show:season:number': show tmdb} (every episode Simkl counts as
    #   watched, including synthetic rows for completed shows), 'ep_real': {key: watched_at} (episodes the user
    #   actually recorded, the only ones that may go to Trakt history)}. Shows = the shows bucket plus the anime
    # bucket (statuses only: anime episode numbering differs, so anime episodes are not bridged).

    def _simkl_activity(self):
        """Simkl's top-level activity stamp (GET /sync/activities, cheap), or None if the request failed."""
        ok, data = self._simkl_get_ok('/sync/activities')
        stamp = (data or {}).get('all') if ok and isinstance(data, dict) else None
        return stamp if isinstance(stamp, str) and stamp else None

    def _simkl_load(self):
        """Read Simkl's library (4 requests). None if any request failed - never a partial view."""
        reads = {}
        for name, path in (
                ('movies', '/sync/all-items/movies/'),
                ('yes', '/sync/all-items/shows/?extended=full&episode_watched_at=yes&include_all_episodes=yes'),
                ('real', '/sync/all-items/shows/?extended=full&episode_watched_at=yes&include_all_episodes=original'),
                ('anime', '/sync/all-items/anime/')):
            ok, data = self._simkl_get_ok(path)
            if not ok or not (data is None or isinstance(data, dict)):
                kodi_log(f'BridgeSync: Simkl {path.split("?")[0]} read failed - skipping Simkl this cycle', 1)
                return None
            reads[name] = data or {}
        views = {'movies': {}, 'shows': {}, 'anime': [], 'ep_yes': {}, 'ep_real': {}}
        for item in reads['movies'].get('movies') or []:
            tmdb_id = ((item.get('movie') or {}).get('ids') or {}).get('tmdb')
            if tmdb_id:
                views['movies'][str(tmdb_id)] = [item.get('status'), item.get('last_watched_at')]
        for item in reads['anime'].get('anime') or []:
            media = item.get('show') or item.get('movie') or item
            tmdb_id = (media.get('ids') or {}).get('tmdb')
            if not tmdb_id:
                continue
            if item.get('anime_type') == 'movie' or 'movie' in item:
                views['movies'][str(tmdb_id)] = [item.get('status'), item.get('last_watched_at')]
            else:
                views['shows'][str(tmdb_id)] = [item.get('status'), item.get('last_watched_at')]
                views['anime'].append(str(tmdb_id))
        for name, target in (('yes', 'ep_yes'), ('real', 'ep_real')):
            for item in reads[name].get('shows') or []:
                tmdb_id = ((item.get('show') or {}).get('ids') or {}).get('tmdb')
                if not tmdb_id:
                    continue
                if name == 'yes':
                    views['shows'][str(tmdb_id)] = [item.get('status'), item.get('last_watched_at')]
                for season in item.get('seasons') or []:
                    if not season.get('number'):           # season 0 (specials) is not bridged
                        continue
                    for episode in season.get('episodes') or []:
                        if episode.get('number') is None:
                            continue
                        key = f'{tmdb_id}:{season["number"]}:{episode["number"]}'
                        views[target][key] = str(tmdb_id) if name == 'yes' else (episode.get('watched_at') or '')
        return views

    def _simkl_prepare(self):
        """Fill self._simkl for this run: the saved views when Simkl's activity stamp has not moved since the
        last cycle, else a fresh read. Simkl asks apps to check /sync/activities before reading lists."""
        self._simkl_prepared = True
        self._simkl = None
        self._simkl_loaded = False
        if not self._simkl_ok:
            return
        self._snapshot_data()
        stamp = self._simkl_activity()
        if stamp is None:
            kodi_log('BridgeSync: Simkl activity check failed - skipping Simkl this cycle', 1)
            return
        saved = self._meta.get('simkl')
        if saved and self._meta.get('simkl_activity') == stamp:
            self._simkl = saved
            return
        views = self._simkl_load()
        if views is not None:
            self._simkl, self._simkl_loaded = views, True

    def _simkl_state(self):
        if not self._simkl_prepared:
            self._simkl_prepare()
        return self._simkl

    def _simkl_finish(self):
        """End of a run: remember Simkl's views and (after our own writes) its activity stamp so the next
        cycle can skip the reads when nothing moved."""
        if not self._simkl_prepared or self._simkl is None or not self._snapshots_ok:
            return
        stamp = self._simkl_activity()
        if stamp is None:
            self._meta.pop('simkl_activity', None)      # unknown: re-read next cycle
        else:
            self._meta['simkl_activity'] = stamp
        self._meta['simkl'] = self._simkl
        self._snapshot_save()

    def _simkl_watched_movies(self):
        """{tmdb_id_str: tmdb_id} of completed Simkl movies, or None if Simkl is unreadable."""
        views = self._simkl_state()
        if views is None:
            return None
        result = {}
        for tmdb_id, (status, stamp) in views['movies'].items():
            if status == 'completed':
                result[tmdb_id] = int(tmdb_id)
                self._note_stamp('movie', tmdb_id, stamp)
        return result

    def _simkl_watched_episodes(self):
        """{'show:season:number': show_tmdb_id} Simkl counts as watched (shows bucket), or None."""
        views = self._simkl_state()
        if views is None:
            return None
        for key, stamp in views['ep_real'].items():
            self._note_stamp('episode', key, stamp)
        return {k: int(v) for k, v in views['ep_yes'].items()}

    def _simkl_set(self):
        """{'movie:<id>' / 'show:<id>': id} Simkl has on plan-to-watch, or None."""
        views = self._simkl_state()
        if views is None:
            return None
        result = {}
        for kind, bucket in (('movie', 'movies'), ('show', 'shows')):
            for tmdb_id, (status, _) in views[bucket].items():
                if status == 'plantowatch':
                    result[f'{kind}:{tmdb_id}'] = int(tmdb_id)
        return result

    def _simkl_status(self, key):
        """Simkl's status for a 'movie:<id>' / 'show:<id>' key, or None when it is not in the library."""
        views = self._simkl_state() or {}
        kind, _, tmdb_id = key.partition(':')
        entry = (views.get('movies' if kind == 'movie' else 'shows') or {}).get(tmdb_id)
        return entry[0] if entry else None

    # ── Readiness: the Trakt side of every diff comes from the local Trakt DB ──
    # Before 6.0.94 that DB held only page 1 of each paginated Trakt list (100 watched movies of
    # 417). The Trakt sync writes a `<method>.paged_v1` marker once it has read a list in full;
    # until it exists the Trakt side is treated as unreadable (skipped this cycle).

    def _trakt_refresh(self, media, columns):
        """Bring the local Trakt data for `media` up to date before it is read. It otherwise only
        refreshes when a Trakt list is opened, so without this a watch made elsewhere (Seren scrobbles to
        every service) would look "missing from Trakt" and be posted to its history as a second play.
        The sync is gated on Trakt's last-activities, so an unchanged account costs one small request.
        True only when every route is current afterwards; a failed sync means Trakt is skipped this cycle."""
        try:
            from jurialmunkey.modimp import importmodule
            syncdata = self.trakt.trakt_syncdata
            fresh = True
            for route in {j for j in (syncdata.routes.get(c) for c in columns) if j}:
                datatype = importmodule(*route)(syncdata, media)
                datatype.sync()
                if datatype.is_expired:
                    fresh = False
        except Exception as exc:
            kodi_log(f'BridgeSync: Trakt refresh({media},{columns}) error: {exc}', 2)
            return False
        if not fresh:
            kodi_log(f'BridgeSync: Trakt {media} data could not be refreshed - skipping Trakt this cycle', 1)
        return fresh

    def _trakt_ready(self, media, method):
        try:
            ready = bool(self.trakt.trakt_syncdata.cache.get_activity(media, f'{method}.paged_v1'))
        except Exception as exc:
            kodi_log(f'BridgeSync: trakt readiness check({media},{method}) error: {exc}', 2)
            return False
        if not ready:
            kodi_log(f'BridgeSync: Trakt {method} {media}s not fully synced yet (paginated refetch pending) - skipping Trakt this cycle', 1)
        return ready

    def _trakt_episodes_ready(self):
        """The Trakt episode rows are rebuilt show by show from /progress/watched; the rebuild marker is
        'ok' only when every show was fetched ('partial:<retry>' = some shows have no episode rows yet)."""
        if not self._trakt_ready('show', 'watched'):
            return False
        try:
            state = self.trakt.trakt_syncdata.cache.get_activity('show', 'watched_progress_v1')
        except Exception as exc:
            kodi_log(f'BridgeSync: trakt episode readiness check error: {exc}', 2)
            return False
        if state != 'ok':
            kodi_log('BridgeSync: Trakt watched episodes not fully rebuilt yet - skipping Trakt episodes this cycle', 1)
            return False
        return True

    @staticmethod
    def _resp_ok(resp):
        """A service answered with success. Trakt's post_response hands back the raw response even for
        a 4xx/5xx, so a response object alone is not proof (None = no answer at all)."""
        return resp is not None and getattr(resp, 'ok', True) is not False

    # ── Snapshots (see module docstring) ───────────────────────────────────────

    def _snapshot_path(self):
        import xbmcvfs
        return xbmcvfs.translatePath(ADDONDATA + _SNAPSHOT_FILE)

    def _snapshot_data(self):
        if self._snapshots is not None:
            return self._snapshots
        self._snapshots = {}
        try:
            with open(self._snapshot_path(), encoding='utf-8') as f:
                raw = json.load(f) or {}
            self._snapshots = {k: {str(i): int(t) for i, t in v.items()} for k, v in (raw.get('snapshots') or {}).items()}
            self._meta = raw.get('meta') or {}
        except FileNotFoundError:
            pass
        except Exception as exc:
            self._snapshots_ok = False
            kodi_log(f'BridgeSync: snapshot file unreadable ({type(exc).__name__}) - no pushes until it is fixed or deleted', 2)
        return self._snapshots

    def _snapshot_save(self):
        try:
            path = self._snapshot_path()
            with open(path + '.tmp', 'w', encoding='utf-8') as f:
                json.dump({'v': 2, 'snapshots': self._snapshots, 'meta': self._meta}, f)
            os.replace(path + '.tmp', path)
            return True
        except Exception as exc:
            self._snapshots_ok = False
            kodi_log(f'BridgeSync: snapshot not saved ({type(exc).__name__}) - no further pushes this cycle', 2)
            return False

    def _reconcile(self, domain, media, current, push_fn, caps=None, skip=None):
        """current: {service: {item_key: tmdb_id} or None (read failed / not ready)}.
        push_fn(items, source, target) -> (succeeded, given_up), both subsets of items.keys():
        succeeded = confirmed present on the target now; given_up = permanently unresolvable there
        (e.g. a tmdb id the target does not know) - counted as reconciled so it is not retried forever.
        Anything in neither set stays unresolved and is retried next cycle.
        caps: {target: max items pushed to that target per cycle, across all sources}; the rest stay unresolved.
        skip(missing, source, target) -> keys that must not go to that target (counted as reconciled, and
        taken out before the cap so they cannot use its budget).
        Mutates `current` in place so a push earlier in this run is visible to later sources. An item this run
        pushed to a service is accounted for on that service's side too (its snapshot, and its `newly`) - it
        did not originate there, so it is never sent onward from there (that would launder an item past the
        filters applied to its real source)."""
        snapshots = self._snapshot_data()
        if not self._snapshots_ok:
            return
        services = list(current)
        budget = dict(caps or {})
        pushed = {}                            # {service: {item_key: id}} put there by this run
        for source in services:
            cur = current[source]
            if cur is None:
                continue                       # unreadable: snapshot untouched, nothing sourced from it
            label = f'{domain}|{source}|{media}'
            snap = snapshots.get(label, {})
            newly = {k: v for k, v in cur.items() if k not in snap and k not in pushed.get(source, {})}
            carried = {k: v for k, v in snap.items() if k in cur}
            unresolved = set()
            for target in services:
                if target == source or not newly:
                    continue
                if current[target] is None:    # cannot tell what the target has: retry later
                    unresolved.update(newly)
                    continue
                missing = {k: v for k, v in newly.items() if k not in current[target]}
                if missing and skip:
                    skipped = set(skip(missing, source, target))
                    missing = {k: v for k, v in missing.items() if k not in skipped}
                if not missing:
                    continue
                if target in budget:
                    cap = budget[target]
                    if len(missing) > cap:
                        kodi_log(f'BridgeSync: {label} →{_TARGET_NAMES[target]}: {len(missing)} new, sending {cap} this cycle', 1)
                        keep = dict(sorted(missing.items())[:cap])
                        unresolved.update(set(missing) - set(keep))
                        missing = keep
                    budget[target] = cap - len(missing)
                    if not missing:
                        continue
                try:
                    succeeded, given_up = push_fn(missing, source, target)
                except Exception as exc:
                    succeeded, given_up = set(), set()
                    kodi_log(f'BridgeSync: {label} →{_TARGET_NAMES[target]} push error: {exc}', 2)
                if succeeded:
                    current[target].update({k: missing[k] for k in succeeded})
                    pushed.setdefault(target, {}).update({k: missing[k] for k in succeeded})
                    self._pushed[target] = self._pushed.get(target, 0) + len(succeeded)
                    kodi_log(f'BridgeSync →{_TARGET_NAMES[target]}: {len(succeeded)} {media} ({domain})', 1)
                if given_up:
                    kodi_log(f'BridgeSync: {label} →{_TARGET_NAMES[target]}: {len(given_up)} not accepted, giving up on them', 1)
                unresolved.update(set(missing) - set(succeeded) - set(given_up))
            snapshots[label] = {**carried, **{k: v for k, v in newly.items() if k not in unresolved}}
        for target, items in pushed.items():
            label = f'{domain}|{target}|{media}'
            snapshots[label] = {**snapshots.get(label, {}), **{k: v for k, v in items.items() if k in current[target]}}
        self._snapshot_save()

    # ── Push helpers ───────────────────────────────────────────────────────────

    @staticmethod
    def _chunks(items, size):
        keys = sorted(items)
        for i in range(0, len(keys), size):
            yield {k: items[k] for k in keys[i:i + size]}

    def _note_stamp(self, media, item_key, stamp):
        """Remember the newest valid ISO last_watched_at seen for an item (used as watched_at on Trakt / Simkl)."""
        if not item_key or not isinstance(stamp, str) or not _STAMP_RE.match(stamp):
            return
        key = (media, str(item_key))
        if stamp > self._stamps.get(key, ''):
            self._stamps[key] = stamp

    @staticmethod
    def _count(data, media):
        """Total of added + existing + updated for one media key in a push response."""
        total = 0
        for part in ('added', 'existing', 'updated'):
            value = ((data or {}).get(part) or {})
            value = value.get(media, 0) if isinstance(value, dict) else 0
            total += len(value) if isinstance(value, list) else (value if isinstance(value, int) else 0)
        return total

    @staticmethod
    def _not_found_ids(data, media):
        """Itemized tmdb ids from response['not_found'][media] ([{'ids': {'tmdb': N}}]); empty when
        the service only reports a count."""
        entries = ((data or {}).get('not_found') or {}).get(media)
        found = set()
        if isinstance(entries, list):
            for entry in entries:
                if isinstance(entry, dict):
                    tmdb_id = (entry.get('ids') or {}).get('tmdb') or entry.get('tmdb')
                    if tmdb_id:
                        found.add(str(tmdb_id))
        return found

    @staticmethod
    def _not_found_episode_keys(data):
        """'show:season:number' of every episode in response['not_found']['episodes'] - MDBList itemizes unmatched
        episodes as [{'season': S, 'number': N, 'show': {'ids': {'tmdb': T}}}] (live-captured 2026-10-14). Empty when
        the service only reports a count, in which case nothing can be attributed and the chunk is retried."""
        keys = set()
        entries = ((data or {}).get('not_found') or {}).get('episodes')
        if isinstance(entries, list):
            for entry in entries:
                if isinstance(entry, dict):
                    tmdb_id = ((entry.get('show') or {}).get('ids') or {}).get('tmdb')
                    season, number = entry.get('season'), entry.get('number')
                    if tmdb_id and season is not None and number is not None:
                        keys.add(f'{tmdb_id}:{season}:{number}')
        return keys

    @staticmethod
    def _not_found_count(data, media):
        value = ((data or {}).get('not_found') or {}).get(media)
        return len(value) if isinstance(value, list) else (value if isinstance(value, int) else 0)

    @staticmethod
    def _result_text(data):
        """Counts only, from a push response ({'added': {'movies': 3}, 'not_found': {'movies': [..]}})."""
        parts = []
        for key in ('added', 'existing', 'updated', 'not_found'):
            value = data.get(key) if isinstance(data, dict) else None
            if isinstance(value, dict):
                counts = {k: (len(v) if isinstance(v, list) else v) for k, v in value.items()
                          if isinstance(v, (int, list)) and v}
                if counts:
                    parts.append(f'{key}={counts}')
        return ' '.join(parts)

    def _wait(self, seconds):
        try:
            import xbmc
            xbmc.Monitor().waitForAbort(seconds)
        except Exception:
            import time
            time.sleep(seconds)

    def _simkl_pace(self):
        """Simkl locks writes per user for 20 s (a second write inside it is answered 400 rate_limit)."""
        import time
        if self._simkl_last_write:
            remaining = _SIMKL_LOCK_SECONDS - (time.monotonic() - self._simkl_last_write)
            if remaining > 0:
                self._wait(remaining)

    def _post(self, target, path, payload):
        """POST payload to a service. Returns (ok, data); ok False = no usable answer (an HTTP error,
        nothing assumed applied)."""
        if target == 'simkl':
            self._simkl_pace()
        try:
            if target == 'trakt':
                resp = self.trakt.post_response(*path, postdata=payload)
            elif target == 'mdblist':
                resp = self.mdb.get_api_request(
                    self.mdb.get_request_url(*path), postdata=payload, method='json', headers=self.mdb.auth_headers)
            else:
                url = f'{_SIMKL_BASE}/{"/".join(path)}?client_id={self.simkl.client_id}'
                resp = self.simkl.get_api_request(url, postdata=payload, method='json', headers=self.simkl.auth_headers)
        except Exception as exc:
            kodi_log(f'BridgeSync: {_TARGET_NAMES[target]} {"/".join(path)} error: {exc}', 2)
            return False, {}
        if target == 'simkl':
            import time
            self._simkl_last_write = time.monotonic()
        if not self._resp_ok(resp):
            kodi_log(f'BridgeSync: {_TARGET_NAMES[target]} {"/".join(path)} push failed '
                     f'(HTTP {getattr(resp, "status_code", "no answer")}) - retry next cycle', 2)
            return False, {}
        try:
            data = resp.json()
        except Exception:
            data = {}
        data = data if isinstance(data, dict) else {}
        text = self._result_text(data)
        if text:
            kodi_log(f'BridgeSync: {_TARGET_NAMES[target]} {"/".join(path)} -> {text}', 1)
        return True, data

    def _push_watched_movies(self, items, source, target):
        """items {tmdb_id_str: tmdb_id}. Trakt history logs a new play per item, so a chunk whose result
        does not add up is given up on rather than retried; MDBList / Simkl are set-membership
        endpoints and are simply retried."""
        succeeded, given_up = set(), set()
        for chunk in self._chunks(items, _MDB_CHUNK if target == 'mdblist' else _BATCH_SIZE):
            entries = []
            for key, tmdb_id in sorted(chunk.items()):
                entry = {'ids': {'tmdb': int(tmdb_id)}}
                stamp = self._stamps.get(('movie', key))
                if stamp and target in ('trakt', 'simkl'):
                    entry['watched_at'] = stamp
                entries.append(entry)
            ok, data = self._post(target, ('sync', 'watched') if target == 'mdblist' else ('sync', 'history'),
                                  {'movies': entries})
            if not ok:
                break
            rejected = self._not_found_ids(data, 'movies')
            good = {k for k, v in chunk.items() if str(v) not in rejected}
            given_up |= set(chunk) - good
            if self._count(data, 'movies') >= len(good):
                succeeded |= good
            elif target in ('trakt', 'simkl'):
                kodi_log(f'BridgeSync: {_TARGET_NAMES[target]} history confirmed fewer movies than sent ({len(good)}) - not retrying', 2)
                given_up |= good
        return succeeded, given_up

    def _skip_episodes(self, missing, source, target):
        """Episodes that must not go to `target`: anime shows to Simkl (different numbering), and from
        Simkl to Trakt anything the user did not actually record there (synthetic rows, approximate dates)."""
        views = self._simkl or {}
        if target == 'simkl':
            anime = set(views.get('anime') or [])
            return {k for k, v in missing.items()
                    if str(v) in anime or (views.get('shows') or {}).get(str(v), [None])[0] in _SIMKL_DROPPED}
        if source == 'simkl' and target == 'trakt':
            return {k for k in missing if k not in (views.get('ep_real') or {})}
        return set()

    def _skip_movies(self, missing, source, target):
        """A watched mark moves a Simkl movie out of its status: leave a movie the user dropped alone."""
        if target != 'simkl':
            return set()
        movies = (self._simkl or {}).get('movies') or {}
        return {k for k in missing if (movies.get(k) or [None])[0] in _SIMKL_DROPPED}

    def _skip_watchlist(self, missing, source, target):
        """Plan-to-watch adds move an item's status: never overwrite one Simkl has in another status."""
        if target != 'simkl':
            return set()
        blocked = {k for k in missing if self._simkl_status(k) not in (None, 'plantowatch')}
        if blocked:
            kodi_log(f'BridgeSync: {len(blocked)} watchlist item(s) already in another Simkl status - left alone', 1)
        return blocked

    def _push_watched_episodes(self, items, source, target):
        """items {'show:season:number': show_tmdb_id} -> one shows[] entry per episode (Seren's payload).
        Simkl: anime shows are not bridged (different numbering). From Simkl to Trakt only episodes the user
        actually recorded on Simkl go out - the rest are synthetic rows with approximate dates."""
        succeeded, given_up = set(), set()
        for chunk in self._chunks(items, _MDB_CHUNK if target == 'mdblist' else _BATCH_SIZE):
            shows = []
            for key, show_id in sorted(chunk.items()):
                _, season, number = key.split(':')
                episode = {'number': int(number)}
                stamp = self._stamps.get(('episode', key))
                if stamp and target in ('trakt', 'simkl'):
                    episode['watched_at'] = stamp
                shows.append({'ids': {'tmdb': int(show_id)}, 'seasons': [{'number': int(season), 'episodes': [episode]}]})
            ok, data = self._post(target, ('sync', 'watched') if target == 'mdblist' else ('sync', 'history'),
                                  {'shows': shows})
            if not ok:
                break
            if target == 'simkl':
                rejected = self._not_found_ids(data, 'shows')
                sent = {k for k, v in chunk.items() if str(v) not in rejected}
            elif target == 'mdblist':
                # MDBList itemizes the episodes it cannot match: those are given up (never retried), the rest count normally
                rejected = self._not_found_episode_keys(data)
                sent = {k for k in chunk if k not in rejected}
            else:
                sent = set(chunk)
            given_up |= set(chunk) - sent
            if self._count(data, 'episodes') >= len(sent):
                succeeded |= sent
            elif target in ('trakt', 'simkl'):
                kodi_log(f'BridgeSync: {_TARGET_NAMES[target]} history confirmed fewer episodes than sent ({len(sent)}) - not retrying', 2)
                given_up |= sent
        return succeeded, given_up

    def _push_set(self, domain, items, source, target):
        """Watchlist / collection push. items {'movie:<id>' / 'show:<id>': tmdb_id}."""
        succeeded, given_up = set(), set()
        is_watchlist = domain == 'watchlist'
        path = ('sync', domain) if target == 'trakt' else (('watchlist', 'items', 'add') if is_watchlist else ('sync', 'collection'))
        if target == 'simkl':
            path = ('sync', 'add-to-list')       # moves an item's status; items in another status are skipped beforehand
        for chunk in self._chunks(items, _MDB_CHUNK if target == 'mdblist' else _BATCH_SIZE):
            payload = {}
            for key, tmdb_id in sorted(chunk.items()):
                kind = 'movies' if key.startswith('movie:') else 'shows'
                if target == 'simkl':
                    entry = {'ids': {'tmdb': int(tmdb_id)}, 'to': 'plantowatch'}
                else:
                    entry = {'tmdb': int(tmdb_id)} if (target == 'mdblist' and is_watchlist) else {'ids': {'tmdb': int(tmdb_id)}}
                payload.setdefault(kind, []).append(entry)
            ok, data = self._post(target, path, payload)
            if not ok:
                break
            if target == 'simkl':
                rejected = {(m, i) for m in ('movies', 'shows') for i in self._not_found_ids(data, m)}
                sent = set()
                for key, tmdb_id in chunk.items():
                    kind = 'movies' if key.startswith('movie:') else 'shows'
                    (given_up if (kind, str(tmdb_id)) in rejected else sent).add(key)
                if sum(self._count(data, m) for m in ('movies', 'shows')) >= len(sent):
                    succeeded |= sent
                else:
                    kodi_log(f'BridgeSync: Simkl confirmed fewer watchlist items than sent ({len(sent)}) - not retrying', 2)
                    given_up |= sent
                continue
            if target == 'trakt':
                rejected = {(m, i) for m in ('movies', 'shows') for i in self._not_found_ids(data, m)}
                for key, tmdb_id in chunk.items():
                    kind = 'movies' if key.startswith('movie:') else 'shows'
                    (given_up if (kind, str(tmdb_id)) in rejected else succeeded).add(key)
                continue
            landed = sum(self._count(data, m) for m in ('movies', 'shows'))
            if landed >= len(chunk):
                succeeded |= set(chunk)
            elif is_watchlist and sum(self._not_found_count(data, m) for m in ('movies', 'shows')):
                # MDBList only counts rejections: read the watchlist back to see which items landed
                readback = self._mdb_list_ids('watchlist', 'items')
                if readback is not None:
                    for key in chunk:
                        (succeeded if key in readback else given_up).add(key)
        return succeeded, given_up

    # ── Sync domains ───────────────────────────────────────────────────────────

    def _current(self, trakt, mdb=None, simkl=None):
        """Assemble {service: dict or None} for the services that are signed in; `mdb` / `simkl` are
        zero-argument readers."""
        current = {'trakt': trakt}
        if mdb and self._mdb_ok:
            current['mdblist'] = mdb()
            if current['mdblist'] is None:
                kodi_log('BridgeSync: MDBList read failed - skipping MDBList this cycle', 1)
        if simkl and self._simkl_ok:
            current['simkl'] = simkl()
            if current['simkl'] is None:
                kodi_log('BridgeSync: Simkl read failed - skipping Simkl this cycle', 1)
        return current

    def _sync_watched_movies(self):
        """Trakt ↔ MDBList ↔ Simkl, newly watched movies only (see module docstring)."""
        fresh = self._trakt_refresh('movie', ('last_watched_at',))
        trakt = self._trakt_ids('movie', 'last_watched_at') if fresh and self._trakt_ready('movie', 'watched') else None
        current = self._current(trakt, self._mdb_watched_movies, self._simkl_watched_movies)
        self._reconcile('watched', 'movie', current, self._push_watched_movies,
                        caps={'trakt': _HISTORY_CAP, 'simkl': _SIMKL_CAP}, skip=self._skip_movies)
        self._simkl_record_movies(current.get('simkl'))

    def _sync_watched_episodes(self):
        """Trakt ↔ MDBList ↔ Simkl. No show-level pushes: a whole-show mark means every episode."""
        if not (self._mdb_ok or self._simkl_ok):
            return
        fresh = self._trakt_refresh('show', ('last_watched_at',))
        trakt = self._trakt_episode_keys() if fresh and self._trakt_episodes_ready() else None
        current = self._current(trakt, self._mdb_watched_episodes, self._simkl_watched_episodes)
        self._reconcile('watched', 'episode', current, self._push_watched_episodes,
                        caps={'trakt': _HISTORY_CAP, 'mdblist': _MDB_EPISODE_CAP, 'simkl': _SIMKL_CAP},
                        skip=self._skip_episodes)
        if self._simkl is not None and current.get('simkl') is not None:
            self._simkl['ep_yes'].update({k: str(v) for k, v in current['simkl'].items()})

    def _simkl_record_movies(self, current_simkl):
        """Keep the saved Simkl views in step with what this cycle pushed to Simkl."""
        if self._simkl is None or current_simkl is None:
            return
        for tmdb_id in current_simkl:
            entry = self._simkl['movies'].get(tmdb_id)
            if not entry or entry[0] != 'completed':
                self._simkl['movies'][tmdb_id] = ['completed', self._stamps.get(('movie', tmdb_id)) or '']

    def _trakt_set(self, clause_key, method):
        """Watchlist / collection from the Trakt DB as {'movie:<id>' / 'show:<id>': id}; None until both
        media types are fully synced or if the DB cannot be read."""
        if not (self._trakt_refresh('movie', (clause_key,)) and self._trakt_refresh('show', (clause_key,))):
            return None
        if not (self._trakt_ready('movie', method) and self._trakt_ready('show', method)):
            return None
        movies = self._trakt_ids('movie', clause_key)
        shows = self._trakt_ids('show', clause_key)
        if movies is None or shows is None:
            return None
        return {**{f'movie:{k}': v for k, v in movies.items()}, **{f'show:{k}': v for k, v in shows.items()}}

    def _sync_watchlist(self):
        """Trakt ↔ MDBList ↔ Simkl plan-to-watch."""
        if not (self._mdb_ok or self._simkl_ok):
            return
        current = self._current(self._trakt_set('watchlist_listed_at', 'watchlist'),
                                lambda: self._mdb_list_ids('watchlist', 'items'), self._simkl_set)
        self._reconcile('watchlist', 'movie_or_show', current, lambda i, s, t: self._push_set('watchlist', i, s, t),
                        caps={'simkl': _SIMKL_CAP}, skip=self._skip_watchlist)
        if self._simkl is not None and current.get('simkl') is not None:
            for key in current['simkl']:
                kind, _, tmdb_id = key.partition(':')
                bucket = self._simkl['movies' if kind == 'movie' else 'shows']
                bucket.setdefault(tmdb_id, ['plantowatch', ''])

    def _sync_collection(self):
        """Trakt ↔ MDBList only — Simkl has no collection endpoint."""
        if not self._mdb_ok:
            return
        current = self._current(self._trakt_set('collection_last_collected_at', 'collection'),
                                lambda: self._mdb_list_ids('sync', 'collection'))
        self._reconcile('collection', 'movie_or_show', current, lambda i, s, t: self._push_set('collection', i, s, t))

    def _sync_progress(self):
        """Push Trakt resume points to MDBList and Simkl.
        Reverse direction not possible: Trakt /sync/playback is read-only.
        """
        if not self._mdb_ok and not self._simkl_ok:
            return
        movie_rows, ep_rows = self._trakt_progress_rows()

        if self._mdb_ok:
            for row in movie_rows:
                try:
                    tmdb_id, progress = int(row[0]), float(row[1])
                    url = self.mdb.get_request_url('scrobble', 'pause')
                    self.mdb.get_api_request(
                        url,
                        postdata={'movie': {'ids': {'tmdb': tmdb_id}}, 'progress': progress},
                        method='json', headers=self.mdb.auth_headers)
                except Exception:
                    pass
            for row in ep_rows:
                try:
                    tmdb_id, progress, season, episode = int(row[0]), float(row[1]), int(row[2]), int(row[3])
                    url = self.mdb.get_request_url('scrobble', 'pause')
                    self.mdb.get_api_request(
                        url,
                        postdata={
                            'show': {'ids': {'tmdb': tmdb_id},
                                     'season': {'number': season, 'episode': {'number': episode}}},
                            'progress': progress},
                        method='json', headers=self.mdb.auth_headers)
                except Exception:
                    pass
            if movie_rows or ep_rows:
                kodi_log(f'BridgeSync →MDBList: {len(movie_rows)} movie + {len(ep_rows)} episode progress', 1)

        if self._simkl_ok:
            if movie_rows or ep_rows:
                self._simkl_pace()      # the bridge's own writes just above may still hold Simkl's per-user lock
            for row in movie_rows:
                try:
                    tmdb_id, progress = int(row[0]), float(row[1])
                    url = f'{_SIMKL_BASE}/scrobble/pause?client_id={self.simkl.client_id}'
                    self.simkl.get_api_request(
                        url,
                        postdata={'progress': progress, 'movie': {'ids': {'tmdb': tmdb_id}}},
                        method='json', headers=self.simkl.auth_headers)
                except Exception:
                    pass
            for row in ep_rows:
                try:
                    tmdb_id, progress, season, episode = int(row[0]), float(row[1]), int(row[2]), int(row[3])
                    url = f'{_SIMKL_BASE}/scrobble/pause?client_id={self.simkl.client_id}'
                    self.simkl.get_api_request(
                        url,
                        postdata={
                            'progress': progress,
                            'show': {'ids': {'tmdb': tmdb_id}},
                            'episode': {'season': season, 'number': episode}},
                        method='json', headers=self.simkl.auth_headers)
                except Exception:
                    pass
            if movie_rows or ep_rows:
                kodi_log(f'BridgeSync →Simkl: {len(movie_rows)} movie + {len(ep_rows)} episode progress', 1)
