from tmdbhelper.lib.files.database import DataBase


class SimklSyncDataBase(DataBase):

    _migrated = set()  # db files already migrated by this interpreter: the ALTERs need running once, not per instance

    def migrate_columns(self):
        if self._db_file in self._migrated:
            return
        self._migrated.add(self._db_file)
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
        # Phase C write-through status columns; since 6.0.113 the watched sync also fills them from the account
        'plantowatch_at': {
            'data': 'TEXT',
            'sync': ('tmdbhelper.lib.api.simkl.sync.datatype', 'SimklSyncWatched',)
        },
        'hold_at': {
            'data': 'TEXT',
            'sync': ('tmdbhelper.lib.api.simkl.sync.datatype', 'SimklSyncWatched',)
        },
        'dropped_at': {
            'data': 'TEXT',
            'sync': ('tmdbhelper.lib.api.simkl.sync.datatype', 'SimklSyncWatched',)
        },
        'watching_at': {
            'data': 'TEXT',
            'sync': ('tmdbhelper.lib.api.simkl.sync.datatype', 'SimklSyncWatched',)
        },
        # The user's own Simkl rating (1-10) and when it was set. Both come with the
        # watched sync (user_rating / user_rated_at of every library entry) and are
        # also written through after a rating made here.
        'rating': {
            'data': 'INTEGER',
            'sync': ('tmdbhelper.lib.api.simkl.sync.datatype', 'SimklSyncWatched',)
        },
        'rated_at': {
            'data': 'TEXT',
            'sync': ('tmdbhelper.lib.api.simkl.sync.datatype', 'SimklSyncWatched',)
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
