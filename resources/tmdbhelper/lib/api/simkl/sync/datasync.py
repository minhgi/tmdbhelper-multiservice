from tmdbhelper.lib.files.ftools import cached_property
from tmdbhelper.lib.api.simkl.sync.database import SimklSyncDataBase


class SimklSyncData:

    cache_filename = 'SimklSync.db'

    def __init__(self, class_instance_simkl_api):
        self._class_instance_simkl_api = class_instance_simkl_api

    @cached_property
    def routes(self):
        return {k: v['sync'] for k, v in self.cache.simplecache_columns.items()}

    def reset_lastactivities(self):
        from tmdbhelper.lib.api.simkl.sync.activity import SIMKL_LASTACTIVITIES_DATA
        self.window.get_property(SIMKL_LASTACTIVITIES_DATA, clear_property=True)

    @cached_property
    def cache(self):
        db = SimklSyncDataBase(filename=self.cache_filename)
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
        from jurialmunkey.modimp import importmodule
        for route in set([j for j in (self.routes.get(k) for k in (keys or ())) if j]):
            importmodule(*route)(self, item_type).sync(forced=forced)

    def get_movie_playcount(self, tmdb_id):
        return self.get_value('movie', tmdb_id, key='plays')

    def get_episode_playcount(self, tmdb_id, season, episode):
        return self.get_value('tv', tmdb_id, season, episode, key='plays')

    def get_episode_watchedcount(self, tmdb_id, season=None):
        return self.get_value('tv', tmdb_id, season, key='watched_episodes')
