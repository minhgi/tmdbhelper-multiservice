"""
Bidirectional Trakt ↔ MDBList ↔ Simkl library reconciliation (bridge sync).

Complements the on-action mirror in cross_sync.py:
  - on-action mirror: real-time, fires on Kodi context menu actions (including removes)
  - bridge sync: additive union, catches additions made outside Kodi (web clients, apps)

Strategy:
  Watched, watchlist: additive union across all authorized services.
    Items on one service but not another are pushed to the missing service.
    On-action mirror handles real-time removes.
  Collection: Trakt ↔ MDBList only (Simkl has no collection endpoint).
  Progress (resume points): Trakt → MDBList and Trakt → Simkl only.
    Reverse direction not possible: Trakt /sync/playback is read-only.
  Ratings: on-action mirror only (no MDBList or Simkl GET /sync/ratings endpoint).

Auth gate: Trakt must be authorized plus at least one of MDBList or Simkl.
Loop safety: no Item* classes instantiated here. Direct API + DB only.
"""

from tmdbhelper.lib.addon.logger import kodi_log

_LAST_RUN_PROP = 'TMDbHelper.BridgeSync.LastRun'
_RUNNING_PROP = 'TMDbHelper.BridgeSync.Running'
_MIN_INTERVAL = 900   # 15 minutes minimum between full passes
_BATCH_SIZE = 100     # max tmdb_ids per single API call
_SIMKL_BASE = 'https://api.simkl.com'


class BridgeSync:

    def __init__(self, trakt_api, mdblist_api, simkl_api=None):
        self.trakt = trakt_api
        self.mdb = mdblist_api
        self.simkl = simkl_api

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
        return (time.time() - last) >= _MIN_INTERVAL

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
        try:
            kodi_log('BridgeSync: starting reconciliation', 1)
            for fn in (self._sync_watched, self._sync_progress,
                       self._sync_watchlist, self._sync_collection):
                try:
                    fn()
                except Exception as exc:
                    kodi_log(f'BridgeSync: {fn.__name__} error: {exc}', 2)
            self._mark_done()
            kodi_log('BridgeSync: reconciliation complete', 1)
        finally:
            get_property(_RUNNING_PROP, clear_property=True)

    # ── Trakt local DB readers (DB is kept fresh by CronJob) ───────────────────

    def _trakt_ids(self, item_type, clause_key):
        """Set of tmdb_id strings from Trakt DB where item_type=type AND clause_key IS NOT NULL."""
        try:
            cache = self.trakt.trakt_syncdata.cache
            rows = cache.get_list_values(
                keys=('tmdb_id',),
                values=(item_type,),
                conditions=f'item_type=? AND {clause_key} IS NOT NULL AND tmdb_id IS NOT NULL')
            return {str(r[0]) for r in (rows or []) if r and r[0]}
        except Exception as exc:
            kodi_log(f'BridgeSync: trakt_ids({item_type},{clause_key}) error: {exc}', 2)
            return set()

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

    def _mdb_get(self, *path_parts):
        """GET from MDBList API with Bearer token. Returns parsed JSON or None."""
        try:
            url = self.mdb.get_request_url(*path_parts)
            resp = self.mdb.get_api_request(url, headers=self.mdb.auth_headers)
            if resp is None:
                return None
            return resp.json()
        except Exception as exc:
            kodi_log(f'BridgeSync: mdb_get{path_parts} error: {exc}', 2)
            return None

    def _mdb_watched_ids(self):
        """GET /sync/watched → set of (media_type, tmdb_id_str) tuples.
        Response: {"movies": [{"movie": {"ids": {"tmdb": N}}, ...}],
                   "episodes": [{"episode": {"show": {"ids": {"tmdb": N}}, ...}}]}
        Shows aggregated from episode list (any watched episode = show in set).
        """
        raw = self._mdb_get('sync', 'watched') or {}
        result = set()
        for item in (raw.get('movies') or []):
            tmdb_id = (item.get('movie') or {}).get('ids', {}).get('tmdb')
            if tmdb_id:
                result.add(('movie', str(tmdb_id)))
        for item in (raw.get('episodes') or []):
            ep = item.get('episode') or {}
            tmdb_id = (ep.get('show') or {}).get('ids', {}).get('tmdb')
            if tmdb_id:
                result.add(('show', str(tmdb_id)))
        return result

    def _mdb_list_ids(self, *path_parts):
        """GET list endpoint → set of (media_type, tmdb_id_str) tuples.
        Handles both flat list format and dict with movies/shows/items keys
        (both nested Trakt-style and flat direct-id format).
        """
        raw = self._mdb_get(*path_parts)
        if not raw:
            return set()
        result = set()
        if isinstance(raw, list):
            for item in raw:
                tmdb_id = item.get('id') or item.get('tmdb')
                mediatype = str(item.get('mediatype') or item.get('type') or '')
                if tmdb_id:
                    result.add(('movie' if 'movie' in mediatype else 'show', str(tmdb_id)))
            return result
        if not isinstance(raw, dict):
            return result
        for item in (raw.get('movies') or []):
            tmdb_id = item.get('id') or (item.get('movie') or {}).get('ids', {}).get('tmdb')
            if tmdb_id:
                result.add(('movie', str(tmdb_id)))
        for item in (raw.get('shows') or []):
            tmdb_id = item.get('id') or (item.get('show') or {}).get('ids', {}).get('tmdb')
            if tmdb_id:
                result.add(('show', str(tmdb_id)))
        for item in (raw.get('items') or []):
            tmdb_id = item.get('id') or item.get('tmdb')
            mediatype = str(item.get('mediatype') or item.get('type') or '')
            if tmdb_id:
                result.add(('movie' if 'movie' in mediatype else 'show', str(tmdb_id)))
        return result

    # ── Simkl API readers ──────────────────────────────────────────────────────

    def _simkl_get(self, path):
        """GET from Simkl API. Returns parsed JSON or None."""
        try:
            url = f'{_SIMKL_BASE}{path}?client_id={self.simkl.client_id}'
            resp = self.simkl.get_api_request(url, headers=self.simkl.auth_headers)
            if resp is None:
                return None
            return resp.json()
        except Exception as exc:
            kodi_log(f'BridgeSync: simkl_get({path}) error: {exc}', 2)
            return None

    def _simkl_watched_ids(self):
        """GET /sync/all-items/movies/completed + /shows/completed
        → set of (media_type, tmdb_id_str) tuples.
        Response: {"movies": [{"movie": {"ids": {"tmdb": N}}, ...}]}
                  {"shows":  [{"show":  {"ids": {"tmdb": N}}, ...}]}
        """
        result = set()
        r = self._simkl_get('/sync/all-items/movies/completed')
        for item in (r or {}).get('movies') or []:
            tmdb_id = (item.get('movie') or {}).get('ids', {}).get('tmdb')
            if tmdb_id:
                result.add(('movie', str(tmdb_id)))
        r = self._simkl_get('/sync/all-items/shows/completed')
        for item in (r or {}).get('shows') or []:
            tmdb_id = (item.get('show') or {}).get('ids', {}).get('tmdb')
            if tmdb_id:
                result.add(('show', str(tmdb_id)))
        return result

    def _simkl_watchlist_ids(self):
        """GET /sync/all-items/movies/plantowatch + /shows/plantowatch
        → set of (media_type, tmdb_id_str) tuples.
        """
        result = set()
        r = self._simkl_get('/sync/all-items/movies/plantowatch')
        for item in (r or {}).get('movies') or []:
            tmdb_id = (item.get('movie') or {}).get('ids', {}).get('tmdb')
            if tmdb_id:
                result.add(('movie', str(tmdb_id)))
        r = self._simkl_get('/sync/all-items/shows/plantowatch')
        for item in (r or {}).get('shows') or []:
            tmdb_id = (item.get('show') or {}).get('ids', {}).get('tmdb')
            if tmdb_id:
                result.add(('show', str(tmdb_id)))
        return result

    # ── Batch push helpers ─────────────────────────────────────────────────────

    def _mdb_push_batch(self, media_key, tmdb_ids, *path_parts):
        if not tmdb_ids:
            return
        try:
            url = self.mdb.get_request_url(*path_parts)
            items = [{'tmdb': int(tid)} for tid in tmdb_ids]
            for i in range(0, len(items), _BATCH_SIZE):
                self.mdb.get_api_request(
                    url, postdata={media_key: items[i:i + _BATCH_SIZE]},
                    method='json', headers=self.mdb.auth_headers)
        except Exception as exc:
            kodi_log(f'BridgeSync: mdb_push_batch{path_parts} error: {exc}', 2)

    def _trakt_push_batch(self, media_key, tmdb_ids, *path_parts):
        if not tmdb_ids:
            return
        try:
            items = [{'ids': {'tmdb': int(tid)}} for tid in tmdb_ids]
            for i in range(0, len(items), _BATCH_SIZE):
                self.trakt.post_response(*path_parts, postdata={media_key: items[i:i + _BATCH_SIZE]})
        except Exception as exc:
            kodi_log(f'BridgeSync: trakt_push_batch{path_parts} error: {exc}', 2)

    def _simkl_push_watched_batch(self, media_key, tmdb_ids):
        """Push watched items to Simkl.
        Movies → POST /sync/history with watched_at timestamp.
        Shows → POST /sync/add-to-list with to=completed (Simkl's required pattern for shows).
        """
        if not tmdb_ids:
            return
        try:
            from datetime import datetime, timezone
            ts = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
            if media_key == 'movies':
                items = [{'ids': {'tmdb': int(tid)}, 'watched_at': ts} for tid in tmdb_ids]
                path = '/sync/history'
                payload_key = 'movies'
            else:
                items = [{'ids': {'tmdb': int(tid)}, 'to': 'completed'} for tid in tmdb_ids]
                path = '/sync/add-to-list'
                payload_key = 'shows'
            for i in range(0, len(items), _BATCH_SIZE):
                url = f'{_SIMKL_BASE}{path}?client_id={self.simkl.client_id}'
                self.simkl.get_api_request(
                    url, postdata={payload_key: items[i:i + _BATCH_SIZE]},
                    method='json', headers=self.simkl.auth_headers)
        except Exception as exc:
            kodi_log(f'BridgeSync: simkl_push_watched_batch({media_key}) error: {exc}', 2)

    def _simkl_push_plantowatch_batch(self, media_key, tmdb_ids):
        """Push plan-to-watch items to Simkl via /sync/add-to-list with to=plantowatch."""
        if not tmdb_ids:
            return
        try:
            payload_key = 'movies' if media_key == 'movies' else 'shows'
            items = [{'ids': {'tmdb': int(tid)}, 'to': 'plantowatch'} for tid in tmdb_ids]
            for i in range(0, len(items), _BATCH_SIZE):
                url = f'{_SIMKL_BASE}/sync/add-to-list?client_id={self.simkl.client_id}'
                self.simkl.get_api_request(
                    url, postdata={payload_key: items[i:i + _BATCH_SIZE]},
                    method='json', headers=self.simkl.auth_headers)
        except Exception as exc:
            kodi_log(f'BridgeSync: simkl_push_plantowatch_batch({media_key}) error: {exc}', 2)

    # ── Trakt↔MDBList pairwise diff (used for collection) ─────────────────────

    def _diff_and_push(self, trakt_movies, trakt_shows, mdb_ids, mdb_path, trakt_path):
        """Compute additive diff and push missing items each way (Trakt↔MDBList pair only)."""
        mdb_movies = {tid for mt, tid in mdb_ids if mt == 'movie'}
        mdb_shows = {tid for mt, tid in mdb_ids if mt == 'show'}

        if to_mdb_m := (trakt_movies - mdb_movies):
            self._mdb_push_batch('movies', to_mdb_m, *mdb_path)
            kodi_log(f'BridgeSync →MDBList: {len(to_mdb_m)} movies ({mdb_path[-1]})', 1)
        if to_mdb_s := (trakt_shows - mdb_shows):
            self._mdb_push_batch('shows', to_mdb_s, *mdb_path)
            kodi_log(f'BridgeSync →MDBList: {len(to_mdb_s)} shows ({mdb_path[-1]})', 1)
        if to_trakt_m := (mdb_movies - trakt_movies):
            self._trakt_push_batch('movies', to_trakt_m, *trakt_path)
            kodi_log(f'BridgeSync →Trakt: {len(to_trakt_m)} movies ({trakt_path[-1]})', 1)
        if to_trakt_s := (mdb_shows - trakt_shows):
            self._trakt_push_batch('shows', to_trakt_s, *trakt_path)
            kodi_log(f'BridgeSync →Trakt: {len(to_trakt_s)} shows ({trakt_path[-1]})', 1)

    # ── Sync domains ───────────────────────────────────────────────────────────

    def _sync_watched(self):
        """Three-way additive union for watched state.
        Push to Trakt: anything in MDBList OR Simkl that Trakt doesn't have.
        Push to MDBList/Simkl: anything in Trakt that they're missing.
        """
        trakt_movies = self._trakt_ids('movie', 'last_watched_at')
        trakt_shows = self._trakt_ids('show', 'last_watched_at')

        mdb_movies = mdb_shows = set()
        if self._mdb_ok:
            mdb_ids = self._mdb_watched_ids()
            mdb_movies = {tid for mt, tid in mdb_ids if mt == 'movie'}
            mdb_shows = {tid for mt, tid in mdb_ids if mt == 'show'}

        simkl_movies = simkl_shows = set()
        if self._simkl_ok:
            simkl_ids = self._simkl_watched_ids()
            simkl_movies = {tid for mt, tid in simkl_ids if mt == 'movie'}
            simkl_shows = {tid for mt, tid in simkl_ids if mt == 'show'}

        # Push to MDBList what Trakt has that MDBList doesn't
        if self._mdb_ok:
            if to_mdb_m := (trakt_movies - mdb_movies):
                self._mdb_push_batch('movies', to_mdb_m, 'sync', 'watched')
                kodi_log(f'BridgeSync →MDBList: {len(to_mdb_m)} movies (watched)', 1)
            if to_mdb_s := (trakt_shows - mdb_shows):
                self._mdb_push_batch('shows', to_mdb_s, 'sync', 'watched')
                kodi_log(f'BridgeSync →MDBList: {len(to_mdb_s)} shows (watched)', 1)

        # Push to Simkl what Trakt has that Simkl doesn't
        if self._simkl_ok:
            if to_simkl_m := (trakt_movies - simkl_movies):
                self._simkl_push_watched_batch('movies', to_simkl_m)
                kodi_log(f'BridgeSync →Simkl: {len(to_simkl_m)} movies (watched)', 1)
            if to_simkl_s := (trakt_shows - simkl_shows):
                self._simkl_push_watched_batch('shows', to_simkl_s)
                kodi_log(f'BridgeSync →Simkl: {len(to_simkl_s)} shows (watched)', 1)

        # Push to Trakt anything from any external service it's missing
        all_ext_movies = mdb_movies | simkl_movies
        all_ext_shows = mdb_shows | simkl_shows
        if to_trakt_m := (all_ext_movies - trakt_movies):
            self._trakt_push_batch('movies', to_trakt_m, 'sync', 'history')
            kodi_log(f'BridgeSync →Trakt: {len(to_trakt_m)} movies (watched)', 1)
        if to_trakt_s := (all_ext_shows - trakt_shows):
            self._trakt_push_batch('shows', to_trakt_s, 'sync', 'history')
            kodi_log(f'BridgeSync →Trakt: {len(to_trakt_s)} shows (watched)', 1)

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

    def _sync_watchlist(self):
        """Three-way additive union for watchlist / plan-to-watch.
        Simkl plan-to-watch maps to Trakt/MDBList watchlist.
        """
        trakt_movies = self._trakt_ids('movie', 'watchlist_listed_at')
        trakt_shows = self._trakt_ids('show', 'watchlist_listed_at')

        mdb_movies = mdb_shows = set()
        if self._mdb_ok:
            mdb_ids = self._mdb_list_ids('watchlist', 'items')
            mdb_movies = {tid for mt, tid in mdb_ids if mt == 'movie'}
            mdb_shows = {tid for mt, tid in mdb_ids if mt == 'show'}

        simkl_movies = simkl_shows = set()
        if self._simkl_ok:
            simkl_ids = self._simkl_watchlist_ids()
            simkl_movies = {tid for mt, tid in simkl_ids if mt == 'movie'}
            simkl_shows = {tid for mt, tid in simkl_ids if mt == 'show'}

        if self._mdb_ok:
            if to_mdb_m := (trakt_movies - mdb_movies):
                self._mdb_push_batch('movies', to_mdb_m, 'watchlist', 'items', 'add')
                kodi_log(f'BridgeSync →MDBList: {len(to_mdb_m)} movies (watchlist)', 1)
            if to_mdb_s := (trakt_shows - mdb_shows):
                self._mdb_push_batch('shows', to_mdb_s, 'watchlist', 'items', 'add')
                kodi_log(f'BridgeSync →MDBList: {len(to_mdb_s)} shows (watchlist)', 1)

        if self._simkl_ok:
            if to_simkl_m := (trakt_movies - simkl_movies):
                self._simkl_push_plantowatch_batch('movies', to_simkl_m)
                kodi_log(f'BridgeSync →Simkl: {len(to_simkl_m)} movies (plantowatch)', 1)
            if to_simkl_s := (trakt_shows - simkl_shows):
                self._simkl_push_plantowatch_batch('shows', to_simkl_s)
                kodi_log(f'BridgeSync →Simkl: {len(to_simkl_s)} shows (plantowatch)', 1)

        all_ext_movies = mdb_movies | simkl_movies
        all_ext_shows = mdb_shows | simkl_shows
        if to_trakt_m := (all_ext_movies - trakt_movies):
            self._trakt_push_batch('movies', to_trakt_m, 'sync', 'watchlist')
            kodi_log(f'BridgeSync →Trakt: {len(to_trakt_m)} movies (watchlist)', 1)
        if to_trakt_s := (all_ext_shows - trakt_shows):
            self._trakt_push_batch('shows', to_trakt_s, 'sync', 'watchlist')
            kodi_log(f'BridgeSync →Trakt: {len(to_trakt_s)} shows (watchlist)', 1)

    def _sync_collection(self):
        """Trakt ↔ MDBList only — Simkl has no collection endpoint."""
        if not self._mdb_ok:
            return
        self._diff_and_push(
            trakt_movies=self._trakt_ids('movie', 'collection_last_collected_at'),
            trakt_shows=self._trakt_ids('show', 'collection_last_collected_at'),
            mdb_ids=self._mdb_list_ids('sync', 'collection'),
            mdb_path=('sync', 'collection'),
            trakt_path=('sync', 'collection'))
