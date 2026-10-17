from tmdbhelper.lib.files.ftools import cached_property
from tmdbhelper.lib.addon.tmdate import set_timestamp, get_timestamp
from tmdbhelper.lib.api.simkl.sync.activity import SimklLastActivities
from tmdbhelper.lib.files.locker import mutexlock

BASE_URL = 'https://api.simkl.com'


def timerlock(func):
    def wrapper(self, *args, **kwargs):
        interval = 3
        propname = f'simkl.syncdecorators.timerlock.{self.item_type}.{self.method}'
        if get_timestamp(self.window.get_property(propname) or 0, set_int=True):
            return
        self.window.get_property(propname, set_timestamp(interval, set_int=True))
        return func(self, *args, **kwargs)
    return wrapper


def progress_bg(func):
    def wrapper(self, *args, **kwargs):
        from tmdbhelper.lib.addon.dialog import DialogProgressSyncBG
        self.dialog_progress_bg = DialogProgressSyncBG()
        self.dialog_progress_bg.heading = f'Simkl Syncing {self.item_type} {self.method}'
        self.dialog_progress_bg.create()
        data = func(self, *args, **kwargs)
        self.dialog_progress_bg.close()
        return data
    return wrapper


class SimklDataType:

    sync_kwgs = {}
    lock_name = 'sync_simkl'
    key_prefix = None

    def __init__(self, class_instance_syncdata, item_type):
        self._class_instance_syncdata = class_instance_syncdata
        self._item_type = item_type

    @property
    def mutex_lockname(self):
        return f'{self.cache._db_file}.{self.lock_name}.{self.item_type}.{self.method}.lockfile'

    @property
    def cache(self):
        return self._class_instance_syncdata.cache

    @property
    def window(self):
        return self._class_instance_syncdata.window

    @property
    def item_type(self):
        return self._item_type

    @property
    def simkl_api(self):
        return self._class_instance_syncdata._class_instance_simkl_api

    def post_response_sync(self, url):
        # Use method='post' — postdata={} is falsy and won't trigger POST in reqapi
        response = self.simkl_api.get_api_request(
            url, method='post', headers=self.simkl_api.auth_headers)
        if response is None:
            return None
        try:
            return response.json()
        except (ValueError, AttributeError):
            return None

    def post_response_sync_ok(self, url):
        """Like post_response_sync but tells a failed request from an empty bucket.

        Returns (ok, data). ok is False when there was no response or an HTTP error.
        ok is True with data None for an empty bucket: Simkl answers HTTP 200 with a
        literal null (or empty) body when a bucket has nothing in it.
        """
        response = self.simkl_api.get_api_request(
            url, method='post', headers=self.simkl_api.auth_headers)
        if response is None:
            return False, None
        try:
            return True, response.json()
        except (ValueError, AttributeError):
            return (not (getattr(response, 'text', None) or '').strip()), None

    @cached_property
    def last_activities(self):
        return SimklLastActivities(self._class_instance_syncdata)

    def store_last_activity(self):
        value = self.last_activities.get_max_watched_timestamp() or '2000-01-01T00:00:00.000Z'
        self.cache.set_activity(self.item_type, self.method, value)

    def clear_timerlock(self):
        propname = f'simkl.syncdecorators.timerlock.{self.item_type}.{self.method}'
        self.window.get_property(propname, clear_property=True)

    @property
    def last_activities_key(self):
        raise NotImplementedError

    def clear_columns(self, keys):
        self.cache.del_column_values(keys=keys, item_type=self.item_type)

    @property
    def is_expired(self):
        timestamp = self.cache.get_activity(self.item_type, self.method)
        return self.last_activities.is_expired(timestamp)

    @timerlock
    def sync_func(self):
        raise NotImplementedError

    @progress_bg
    def sync_data(self, **kwargs):
        self.dialog_progress_bg.update(20, message='Refreshing Data')
        meta = self.sync_func()
        if meta is None:
            return

        from tmdbhelper.lib.api.simkl.sync.itemdata import SimklSyncItem
        item = SimklSyncItem(self.method, meta, self.keys)

        self.dialog_progress_bg.update(40, message='Cleaning Data')
        self.clear_columns(item.base_table_keys)

        if not meta:
            return True

        self.dialog_progress_bg.update(60, message='Configuring Data')
        data = item.data

        self.dialog_progress_bg.update(80, message='Updating Data')
        self.cache.set_many_values(keys=item.table_keys, data=data)

        return (item.table_keys, data,)

    @mutexlock
    def sync(self, forced=False):
        if not forced and not self.is_expired:
            return
        if forced:
            self.clear_timerlock()
        result = self.sync_data()
        if result is not None:
            self.store_last_activity()


class SimklSyncPaused(SimklDataType):
    """Resume progress from GET /sync/playback.

    Movies only — Simkl returns TVDB episode numbers for episode entries
    which diverge from TMDb numbering for anime.  Parsing episode rows would
    overwrite the locally-correct TMDb-numbered rows the scrobbler writes,
    actively degrading episode resume for anime.  Episode support deferred
    until _simkl_resolve_episode (external_ids + airdate fallback) is built.

    Expiry: no dedicated 'paused_at' field in /sync/activities.  Store a
    local UTC timestamp and re-sync after 180 s rather than on every browse.
    """

    keys = ('paused_progress', 'paused_at', 'paused_id',)
    last_activities_key = 'playback'
    method = 'playback'

    def __init__(self, class_instance_syncdata, item_type):
        super().__init__(class_instance_syncdata, 'all')

    def clear_columns(self, keys):
        # Clear movie rows only — episode paused rows are scrobbler-owned
        # (TMDb-numbered) and must not be wiped by a server sync that returns
        # TVDB-numbered episode data.
        self.cache.del_column_values(keys=keys, item_type='movie')

    def store_last_activity(self):
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.000Z')
        self.cache.set_activity(self.item_type, self.method, now)

    @property
    def is_expired(self):
        from datetime import datetime, timezone
        stored = self.cache.get_activity(self.item_type, self.method)
        if not stored:
            return True
        try:
            stored_dt = datetime.fromisoformat(stored.replace('Z', '+00:00'))
            return (datetime.now(timezone.utc) - stored_dt).total_seconds() > 180
        except Exception:
            return True

    @timerlock
    def sync_func(self):
        from tmdbhelper.lib.addon.logger import TimerFunc
        with TimerFunc('Simkl Sync: playback', inline=True, log_threshold=0.001):
            url = f'{BASE_URL}/sync/playback?client_id={self.simkl_api.client_id}'
            response = self.simkl_api.get_api_request(url, headers=self.simkl_api.auth_headers)
            if response is None:
                return None
            try:
                return response.json()
            except (ValueError, AttributeError):
                return None


class SimklSyncWatched(SimklDataType):
    """
    Covers both movies and shows in two POST calls.
    Uses item_type='all' so the timerlock is shared — prevents redundant
    API calls when both movie and show indicators are requested together.
    """

    keys = ('plays', 'last_watched_at', 'watched_episodes', 'rating', 'rated_at',
            'plantowatch_at', 'hold_at', 'dropped_at', 'watching_at',)
    last_activities_key = 'all'
    method = 'watched'
    # Marks a sync that also read the account's list statuses (plan-to-watch / hold / dropped / watching).
    # Syncs stamped before that existed are stale for the status flags, so without the marker the first
    # sync after an upgrade would wait for the next change on the account. v2: 6.0.115 reads anime rows and
    # the watched_episodes_count of completed shows, again stale until a re-sync.
    status_marker = 'watched.status_v2'

    @property
    def is_expired(self):
        if not self.cache.get_activity(self.item_type, self.status_marker):
            return True
        return super().is_expired

    def store_last_activity(self):
        super().store_last_activity()
        self.cache.set_activity(self.item_type, self.status_marker, '1')

    def __init__(self, class_instance_syncdata, item_type):
        super().__init__(class_instance_syncdata, 'all')

    def clear_columns(self, keys):
        for it in ('movie', 'show', 'season',):
            self.cache.del_column_values(keys=keys, item_type=it)

    @timerlock
    def sync_func(self):
        from tmdbhelper.lib.addon.logger import TimerFunc, kodi_log
        with TimerFunc('Simkl Sync: watched', inline=True, log_threshold=0.001):
            # Every status, not just 'completed': the list status of each movie (plan-to-watch, hold, dropped)
            # is read from the same rows; only 'completed' ones count as watched below.
            movies_ok, movies_data = self.post_response_sync_ok(
                f'{BASE_URL}/sync/all-items/movies/?client_id={self.simkl_api.client_id}')
            shows_ok, shows_data = self.post_response_sync_ok(
                f'{BASE_URL}/sync/all-items/shows/?extended=full&client_id={self.simkl_api.client_id}')
            # Simkl buckets anime separately from movies/shows entirely (confirmed
            # via API docs — no mixed lists), so the two calls above never see
            # anime titles. The exact per-entry shape under the top-level "anime"
            # key is NOT directly confirmed for this endpoint: Simkl's detail-doc
            # prose implies anime nests under a "movie"/"show" key like regular
            # entries, but the concrete examples elsewhere in the docs (history
            # writes, search results) show anime objects flat (ids/anime_type
            # directly on the entry). Handle both shapes rather than assume one.
            anime_completed_ok, anime_completed_data = self.post_response_sync_ok(
                f'{BASE_URL}/sync/all-items/anime/completed?client_id={self.simkl_api.client_id}')
            anime_all_ok, anime_all_data = self.post_response_sync_ok(
                f'{BASE_URL}/sync/all-items/anime/?extended=full&client_id={self.simkl_api.client_id}')

        # sync_data() clears every watched flag and rewrites only what was fetched, so
        # a request that failed outright (an empty bucket is not a failure) would wipe
        # that part of the library with nothing to restore it. Skip the whole sync.
        if not (movies_ok and shows_ok and anime_completed_ok and anime_all_ok):
            kodi_log('Simkl Sync: watched - a request failed, keeping local data', 1)
            return None

        movie_rows = (movies_data or {}).get('movies') or []
        movies = [row for row in movie_rows if row.get('status') in (None, 'completed')]
        shows = (shows_data or {}).get('shows') or []
        # Read before the anime entries are appended to `shows` below (it is the same list object)
        from tmdbhelper.lib.api.simkl.sync.itemdata import SimklSyncItem
        statuses = SimklSyncItem.collect_statuses(
            [row for row in movie_rows if row.get('status') not in (None, 'completed')],
            shows, (anime_all_data or {}).get('anime') or [])

        # Anime entries keep anime_type at the top level and the ids under 'show' (live shape, 2026-10-14);
        # a flat entry (ids and anime_type together) is still accepted.
        # Movie-type anime → completed-only, matching the movies treatment above.
        for entry in (anime_completed_data or {}).get('anime') or []:
            movie_obj = entry.get('movie') or entry.get('show') or entry
            if (entry.get('anime_type') or movie_obj.get('anime_type')) == 'movie':
                movies.append({
                    'movie': movie_obj, 'last_watched_at': entry.get('last_watched_at'),
                    'user_rating': entry.get('user_rating'), 'user_rated_at': entry.get('user_rated_at')})
        # Everything else (tv/ova/ona/special/music video) → all-statuses,
        # matching the shows treatment above (watched_episodes needs full
        # status coverage, not just completed).
        for entry in (anime_all_data or {}).get('anime') or []:
            show_obj = entry.get('show') or entry
            anime_type = entry.get('anime_type') or show_obj.get('anime_type')
            if anime_type and anime_type != 'movie':
                shows.append({
                    'show': show_obj,
                    'anime_type': anime_type,
                    'seasons': entry.get('seasons') or [],
                    'watched_episodes_count': entry.get('watched_episodes_count'),
                    'last_watched_at': entry.get('last_watched_at'),
                    'user_rating': entry.get('user_rating'),
                    'user_rated_at': entry.get('user_rated_at'),
                })

        kodi_log(f'Simkl Sync: {len(movies)} movies, {len(shows)} shows, {len(statuses)} on a status list', 1)

        if not movies and not shows and not statuses:
            return None
        return {'movies': movies, 'shows': shows, 'statuses': statuses}
