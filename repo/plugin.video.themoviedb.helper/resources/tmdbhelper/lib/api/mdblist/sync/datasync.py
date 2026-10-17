from tmdbhelper.lib.files.ftools import cached_property
from tmdbhelper.lib.api.mdblist.sync.database import MDbListSyncDataBase

SYNC_RECHECK = 5  # seconds a data route is trusted as up to date after it was checked (see sync())


class MDbListSyncData:

    cache_filename = 'MDbListSync.db'

    def __init__(self, class_instance_mdblist_api):
        self._class_instance_mdblist_api = class_instance_mdblist_api

    @cached_property
    def routes(self):
        return {k: v['sync'] for k, v in self.cache.simplecache_columns.items()}

    def reset_lastactivities(self):
        from tmdbhelper.lib.api.mdblist.sync.activity import MDBLIST_LASTACTIVITIES_DATA
        self.window.get_property(MDBLIST_LASTACTIVITIES_DATA, clear_property=True)
        self.__dict__.pop('_sync_checked', None)

    def clear_all(self):
        """ Forget the signed-in account's synced data (every row and every activity stamp) so a different account never
        inherits it. The tables are emptied rather than the file deleted: the file's 'initialised' window flag would
        stop a deleted file being recreated. Returns True if both tables were emptied. """
        from tmdbhelper.lib.addon.logger import kodi_log
        done = all(self.cache.execute_sql(f'DELETE FROM {table}') is not None for table in ('simplecache', 'lactivities'))
        self.reset_lastactivities()
        kodi_log(f'{self.cache_filename}: cleared on logout' if done else f'{self.cache_filename}: could not be cleared on logout', 1)
        return done

    @cached_property
    def cache(self):
        db = MDbListSyncDataBase(filename=self.cache_filename)
        db.migrate_columns()
        return db

    @cached_property
    def window(self):
        from jurialmunkey.window import WindowPropertySetter
        return WindowPropertySetter()

    @staticmethod
    def get_name(tmdb_type, tmdb_id, season=None, episode=None):
        name = f'{tmdb_type}.{tmdb_id}'
        if season is None:
            return name
        name = f'{name}.{season}'
        if episode is None:
            return name
        return f'{name}.{episode}'

    def get_values(self, tmdb_type, tmdb_id, season=None, episode=None, keys=None):
        if tmdb_type not in ('tv', 'movie'):
            return
        self.sync('show' if tmdb_type == 'tv' else 'movie', keys)
        return self.cache.get_values(item_id=self.get_name(tmdb_type, tmdb_id, season, episode), keys=keys)

    def get_value(self, tmdb_type, tmdb_id, season=None, episode=None, key=None):
        data = self.get_values(tmdb_type, tmdb_id, season, episode, keys=(key,))
        return data[0] if data else None

    def sync(self, item_type, keys, forced=False):
        """ Make sure the synced data is up to date for these keys. Every value read calls this, so a route that was checked
        in the last SYNC_RECHECK seconds is not checked again (each check takes a cross-process lock and reads the stored
        activity): a list of 20 items used to do ~100 checks. forced=True and reset_lastactivities() always bypass it. """
        from jurialmunkey.modimp import importmodule
        from time import monotonic
        checked = self.__dict__.setdefault('_sync_checked', {})
        for route in set([j for j in (self.routes.get(k) for k in (keys or ())) if j]):
            stamp = (route, item_type)
            if not forced and monotonic() - checked.get(stamp, -SYNC_RECHECK) < SYNC_RECHECK:
                continue
            importmodule(*route)(self, item_type).sync(forced=forced)
            checked[stamp] = monotonic()

    # ------------------------------------------------------------------
    # Query interface — mirrors TraktPlayData's expected methods
    # ------------------------------------------------------------------

    def get_movie_playcount(self, tmdb_id):
        return self.get_value('movie', tmdb_id, key='plays')

    def get_movie_playprogress(self, tmdb_id):
        return self.get_value('movie', tmdb_id, key='paused_progress')

    def get_episode_playcount(self, tmdb_id, season, episode):
        return self.get_value('tv', tmdb_id, season, episode, key='plays')

    def get_episode_playprogress(self, tmdb_id, season, episode):
        return self.get_value('tv', tmdb_id, season, episode, key='paused_progress')

    def get_episode_watchedcount(self, tmdb_id, season=None):
        return self.get_value('tv', tmdb_id, season, key='watched_episodes')
