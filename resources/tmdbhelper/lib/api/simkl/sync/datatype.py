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

    keys = ('plays', 'last_watched_at', 'watched_episodes',)
    last_activities_key = 'all'
    method = 'watched'

    def __init__(self, class_instance_syncdata, item_type):
        super().__init__(class_instance_syncdata, 'all')

    def clear_columns(self, keys):
        for it in ('movie', 'show', 'season',):
            self.cache.del_column_values(keys=keys, item_type=it)

    @timerlock
    def sync_func(self):
        from tmdbhelper.lib.addon.logger import TimerFunc, kodi_log
        with TimerFunc('Simkl Sync: watched', inline=True, log_threshold=0.001):
            movies_data = self.post_response_sync(
                f'{BASE_URL}/sync/all-items/movies/completed?client_id={self.simkl_api.client_id}')
            shows_data = self.post_response_sync(
                f'{BASE_URL}/sync/all-items/shows/?extended=full&client_id={self.simkl_api.client_id}')

        movies = (movies_data or {}).get('movies') or []
        shows = (shows_data or {}).get('shows') or []
        kodi_log(f'Simkl Sync: {len(movies)} movies, {len(shows)} shows', 1)

        if not movies and not shows:
            return None
        return {'movies': movies, 'shows': shows}
