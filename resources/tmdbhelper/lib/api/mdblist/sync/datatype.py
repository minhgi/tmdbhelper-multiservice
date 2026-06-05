from tmdbhelper.lib.files.ftools import cached_property
from tmdbhelper.lib.addon.tmdate import set_timestamp, get_timestamp
from tmdbhelper.lib.api.mdblist.sync.activity import MDbListLastActivities
from tmdbhelper.lib.files.locker import mutexlock


def timerlock(func):
    def wrapper(self, *args, **kwargs):
        interval = 3
        propname = f'mdblist.syncdecorators.timerlock.{self.item_type}.{self.method}'
        if get_timestamp(self.window.get_property(propname) or 0, set_int=True):
            return
        self.window.get_property(propname, set_timestamp(interval, set_int=True))
        return func(self, *args, **kwargs)
    return wrapper


def progress_bg(func):
    def wrapper(self, *args, **kwargs):
        from tmdbhelper.lib.addon.dialog import DialogProgressSyncBG
        self.dialog_progress_bg = DialogProgressSyncBG()
        self.dialog_progress_bg.heading = f'MDbList Syncing {self.item_type} {self.method}'
        self.dialog_progress_bg.create()
        data = func(self, *args, **kwargs)
        self.dialog_progress_bg.close()
        return data
    return wrapper


class MDbListDataType:

    sync_kwgs = {}
    lock_name = 'sync_mdblist'
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
    def mdblist_api(self):
        return self._class_instance_syncdata._class_instance_mdblist_api

    def get_response_sync(self, *path_args, **kwargs):
        from tmdbhelper.lib.addon.logger import kodi_log
        path = self.mdblist_api.get_request_url(*path_args, **kwargs)
        response = self.mdblist_api.get_api_request(path, headers=self.mdblist_api.auth_headers)
        if response is None:
            return
        try:
            return response.json()
        except (ValueError, AttributeError):
            return

    @cached_property
    def last_activities(self):
        return MDbListLastActivities(self._class_instance_syncdata)

    def store_last_activity(self):
        # Store the specific key's timestamp so is_expired() detects only changes
        # to THIS activity type. Storing 'all' (the max timestamp) made is_expired
        # always False — 'watched_at' <= 'all' always — so syncs never skipped.
        value = self.last_activities.json.get(self.last_activities_key) or '2000-01-01T00:00:00.000Z'
        self.cache.set_activity(self.item_type, self.method, value)

    def clear_timerlock(self):
        propname = f'mdblist.syncdecorators.timerlock.{self.item_type}.{self.method}'
        self.window.get_property(propname, clear_property=True)

    @property
    def last_activities_key(self):
        raise NotImplementedError

    @property
    def last_activities_keys(self):
        return (self.last_activities_key,)

    def clear_columns(self, keys):
        self.cache.del_column_values(keys=keys, item_type=self.item_type)

    @property
    def is_expired(self):
        timestamp = self.cache.get_activity(self.item_type, self.method)
        return self.last_activities.is_expired(timestamp, keys=self.last_activities_keys)

    @timerlock
    def sync_func(self):
        raise NotImplementedError

    @progress_bg
    def sync_data(self, **kwargs):
        self.dialog_progress_bg.update(20, message='Refreshing Data')
        meta = self.sync_func()
        if meta is None:
            return

        from tmdbhelper.lib.api.mdblist.sync.itemdata import MDbListSyncItem
        item = MDbListSyncItem(self.method, meta, self.keys)

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


class MDbListSyncWatched(MDbListDataType):
    """
    Covers both movies and episodes in one /sync/watched call.
    Uses item_type='all' so the timerlock is shared, preventing a
    redundant API call when both movie and show indicators are requested
    in the same build cycle.
    """

    keys = ('plays', 'last_watched_at', 'watched_episodes',)
    last_activities_key = 'watched_at'
    method = 'watched'

    def __init__(self, class_instance_syncdata, item_type):
        # Normalise to 'all' — one call covers both movies and shows
        super().__init__(class_instance_syncdata, 'all')

    def clear_columns(self, keys):
        for it in ('movie', 'show', 'season', 'episode'):
            self.cache.del_column_values(keys=keys, item_type=it)

    @timerlock
    def sync_func(self):
        from tmdbhelper.lib.addon.logger import TimerFunc
        with TimerFunc('MDbList Sync: watched', inline=True, log_threshold=0.001):
            return self.get_response_sync('sync', 'watched', extended='full')


class MDbListSyncPaused(MDbListDataType):
    """Playback progress from /sync/playback."""

    keys = ('paused_progress', 'paused_at', 'paused_id',)
    last_activities_key = 'paused_at'
    method = 'playback'

    def __init__(self, class_instance_syncdata, item_type):
        super().__init__(class_instance_syncdata, 'all')

    def clear_columns(self, keys):
        for it in ('movie', 'episode'):
            self.cache.del_column_values(keys=keys, item_type=it)

    @timerlock
    def sync_func(self):
        from tmdbhelper.lib.addon.logger import TimerFunc
        with TimerFunc('MDbList Sync: playback', inline=True, log_threshold=0.001):
            return self.get_response_sync('sync', 'playback')


class MDbListSyncWatchlist(MDbListDataType):

    keys = ('watchlist_listed_at',)
    last_activities_key = 'watchlisted_at'
    method = 'watchlist'

    def __init__(self, class_instance_syncdata, item_type):
        super().__init__(class_instance_syncdata, item_type)

    @timerlock
    def sync_func(self):
        from tmdbhelper.lib.addon.logger import TimerFunc
        with TimerFunc(f'MDbList Sync: watchlist {self.item_type}', inline=True, log_threshold=0.001):
            # MDBList watchlist read is /watchlist/items — GET /sync/watchlist returns 405
            return self.get_response_sync('watchlist', 'items')


class MDbListSyncCollection(MDbListDataType):

    keys = ('collection_collected_at',)
    last_activities_key = 'collected_at'
    method = 'collection'

    def __init__(self, class_instance_syncdata, item_type):
        super().__init__(class_instance_syncdata, item_type)

    @timerlock
    def sync_func(self):
        from tmdbhelper.lib.addon.logger import TimerFunc
        with TimerFunc(f'MDbList Sync: collection {self.item_type}', inline=True, log_threshold=0.001):
            return self.get_response_sync('sync', 'collection')
