from tmdbhelper.lib.files.database import DataBase


class SimklSyncDataBase(DataBase):

    def migrate_columns(self):
        for col_name, col_def in self.simplecache_columns.items():
            try:
                self.execute_sql(
                    f'ALTER TABLE simplecache ADD COLUMN {col_name} {col_def["data"]}', ())
            except Exception:
                pass

    simplecache_columns = {
        'item_type': {
            'data': 'TEXT',
            'sync': None
        },
        'tmdb_type': {
            'data': 'TEXT',
            'sync': None
        },
        'tmdb_id': {
            'data': 'INTEGER',
            'sync': None
        },
        'season_number': {
            'data': 'INTEGER',
            'sync': None
        },
        'episode_number': {
            'data': 'INTEGER',
            'sync': None
        },
        'plays': {
            'data': 'INTEGER DEFAULT 0',
            'sync': ('tmdbhelper.lib.api.simkl.sync.datatype', 'SimklSyncWatched',)
        },
        'last_watched_at': {
            'data': 'TEXT',
            'sync': ('tmdbhelper.lib.api.simkl.sync.datatype', 'SimklSyncWatched',)
        },
        'watched_episodes': {
            'data': 'INTEGER',
            'sync': ('tmdbhelper.lib.api.simkl.sync.datatype', 'SimklSyncWatched',)
        },
        # Phase C write-through status columns
        'plantowatch_at': {
            'data': 'TEXT',
            'sync': None
        },
        'hold_at': {
            'data': 'TEXT',
            'sync': None
        },
        'dropped_at': {
            'data': 'TEXT',
            'sync': None
        },
        # Phase D: resume progress synced from GET /sync/playback (movies only;
        # episode rows deferred — Simkl returns TVDB episode numbers which
        # diverge from TMDb for anime, causing wrong-episode lookups).
        'paused_progress': {
            'data': 'INTEGER',
            'sync': ('tmdbhelper.lib.api.simkl.sync.datatype', 'SimklSyncPaused',)
        },
        'paused_at': {
            'data': 'TEXT',
            'sync': ('tmdbhelper.lib.api.simkl.sync.datatype', 'SimklSyncPaused',)
        },
        'paused_id': {
            'data': 'INTEGER',
            'sync': ('tmdbhelper.lib.api.simkl.sync.datatype', 'SimklSyncPaused',)
        },
    }
