from tmdbhelper.lib.files.ftools import cached_property
from tmdbhelper.lib.addon.tmdate import set_timestamp, get_timestamp
from tmdbhelper.lib.api.wetrakr.sync.activity import WeTrakrLastActivities, NO_ACTIVITY
from tmdbhelper.lib.files.locker import mutexlock


def timerlock(func):
    """ A second sync of the same kind within a few seconds (several widgets building at once) is skipped """
    def wrapper(self, *args, **kwargs):
        interval = 3
        propname = f'wetrakr.syncdecorators.timerlock.{self.method}'
        if get_timestamp(self.window.get_property(propname) or 0, set_int=True):
            return
        self.window.get_property(propname, set_timestamp(interval, set_int=True))
        return func(self, *args, **kwargs)
    return wrapper


class WeTrakrDataType:
    """ One kind of synced data. The stored stamp says which change of the account the stored rows were read at; the data is refetched
    (every page, all or nothing) when WeTrakr reports a newer one. The stamp is read BEFORE the fetch, so a change made while the fetch
    runs triggers another sync instead of being skipped. """

    lock_name = 'sync_wetrakr'
    keys = ()
    method = None
    item_types = ()  # item types whose columns are cleared before a rewrite

    def __init__(self, class_instance_syncdata, item_type):
        self._class_instance_syncdata = class_instance_syncdata
        self._item_type = item_type

    @property
    def mutex_lockname(self):
        return f'{self.cache._db_file}.{self.lock_name}.{self.method}.lockfile'

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
    def wetrakr_api(self):
        return self._class_instance_syncdata._class_instance_wetrakr_api

    @cached_property
    def last_activities(self):
        return WeTrakrLastActivities(self._class_instance_syncdata)

    def current_stamp(self):
        raise NotImplementedError

    @property
    def is_expired(self):
        stored = self.cache.get_activity(self.item_type, self.method)
        if not stored:
            return True
        current = self.current_stamp()
        if current is None:
            return False  # WeTrakr could not be read: keep what is stored
        return current > stored

    def sync_func(self):
        """ The data read from WeTrakr, or None when any request failed (nothing is then changed) """
        raise NotImplementedError

    def clear_timerlock(self):
        self.window.get_property(f'wetrakr.syncdecorators.timerlock.{self.method}', clear_property=True)

    def sync_data(self):
        from tmdbhelper.lib.api.wetrakr.sync.itemdata import WeTrakrSyncItem
        meta = self.sync_func()
        if meta is None:
            return None
        item = WeTrakrSyncItem(self.method, meta, self.keys)
        data = item.data
        for item_type in self.item_types:
            self.cache.del_column_values(keys=item.base_table_keys, item_type=item_type)
        if data:
            self.cache.set_many_values(keys=item.table_keys, data=data)
        return True

    @mutexlock
    def sync(self, forced=False):
        if forced:
            self.clear_timerlock()
        elif not self.is_expired:
            return
        stamp = self.current_stamp() or NO_ACTIVITY  # before the fetch (see the class docstring)
        if self.timerlocked_sync() is not None:
            self.cache.set_activity(self.item_type, self.method, stamp)

    @timerlock
    def timerlocked_sync(self):
        return self.sync_data()


class WeTrakrSyncWatched(WeTrakrDataType):
    """ Watched marks: GET /sync/tracking/watched/movies and /episodes (compact, every page). The show and season counts are worked out from the
    episode rows, so they agree with the marks shown on the episodes. """

    keys = ('plays', 'last_watched_at', 'watched_episodes',)
    method = 'watched'
    item_types = ('movie', 'show', 'season', 'episode',)

    def __init__(self, class_instance_syncdata, item_type):
        super().__init__(class_instance_syncdata, 'all')

    def current_stamp(self):
        return self.last_activities.watched_stamp()

    def sync_func(self):
        from tmdbhelper.lib.addon.logger import kodi_log
        movies_ok, movies = self.wetrakr_api.get_list('/sync/tracking/watched/movies')
        episodes_ok, episodes = self.wetrakr_api.get_list('/sync/tracking/watched/episodes') if movies_ok else (False, [])
        if not (movies_ok and episodes_ok):
            kodi_log('WeTrakr Sync: watched - a request failed, keeping local data', 1)
            return None
        kodi_log(f'WeTrakr Sync: watched - {len(movies)} movies, {len(episodes)} episodes', 1)
        return {'movies': movies, 'episodes': episodes}


class WeTrakrSyncPaused(WeTrakrDataType):
    """ Resume points: the paused rows of GET /sync/tracking/playing/movies and /episodes. Every movie and episode row is rewritten from the read, so a
    pause that was finished (or deleted) on WeTrakr disappears here. """

    keys = ('paused_progress', 'paused_at',)
    method = 'playback'
    item_types = ('movie', 'episode',)

    def __init__(self, class_instance_syncdata, item_type):
        super().__init__(class_instance_syncdata, 'all')

    def current_stamp(self):
        return self.last_activities.playback_stamp()

    def sync_func(self):
        from tmdbhelper.lib.addon.logger import kodi_log
        movies_ok, movies = self.wetrakr_api.get_list('/sync/tracking/playing/movies')
        episodes_ok, episodes = self.wetrakr_api.get_list('/sync/tracking/playing/episodes') if movies_ok else (False, [])
        if not (movies_ok and episodes_ok):
            kodi_log('WeTrakr Sync: playback - a request failed, keeping local data', 1)
            return None
        return {'movies': movies, 'episodes': episodes}


class WeTrakrSyncRatings(WeTrakrDataType):
    """ The account's own ratings: GET /sync/ratings/all (compact, every page) holds movies, shows, seasons and episodes in one list. Every rating
    row is rewritten from the read, so a rating removed on WeTrakr disappears here. The ratings stamp moves on a rating added, changed or removed
    and on nothing else, so a play or a pause never costs a read of the ratings. """

    keys = ('rating', 'rated_at',)
    method = 'ratings'
    item_types = ('movie', 'show', 'season', 'episode',)

    def __init__(self, class_instance_syncdata, item_type):
        super().__init__(class_instance_syncdata, 'all')

    def current_stamp(self):
        return self.last_activities.ratings_stamp()

    def sync_func(self):
        from tmdbhelper.lib.addon.logger import kodi_log
        ok, rows = self.wetrakr_api.get_list('/sync/ratings/all')
        if not ok:
            kodi_log('WeTrakr Sync: ratings - a request failed, keeping local data', 1)
            return None
        kodi_log(f'WeTrakr Sync: ratings - {len(rows)} rated', 1)
        return {'ratings': rows}
