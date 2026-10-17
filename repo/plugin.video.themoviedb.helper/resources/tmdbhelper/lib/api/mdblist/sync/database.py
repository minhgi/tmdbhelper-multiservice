from tmdbhelper.lib.files.database import DataBase


class MDbListSyncDataBase(DataBase):

    def migrate_columns(self):
        """Add any columns missing from the on-disk table (schema migration).

        CREATE TABLE IF NOT EXISTS does not alter existing tables, so new columns
        added in later versions must be applied via ALTER TABLE ADD COLUMN.
        SQLite < 3.37 has no ADD COLUMN IF NOT EXISTS — duplicate-column errors
        are caught and ignored.  Uses execute_sql (the same commit path as all
        other DB writes) rather than the connection object from
        create_database_execute, which is not reliably committed.
        """
        for col_name, col_def in self.simplecache_columns.items():
            try:
                self.execute_sql(
                    f'ALTER TABLE simplecache ADD COLUMN {col_name} {col_def["data"]}', ())
            except Exception:
                pass  # column already present

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
            'sync': ('tmdbhelper.lib.api.mdblist.sync.datatype', 'MDbListSyncWatched', )
        },
        'last_watched_at': {
            'data': 'TEXT',
            'sync': ('tmdbhelper.lib.api.mdblist.sync.datatype', 'MDbListSyncWatched', )
        },
        'watched_episodes': {
            'data': 'INTEGER',
            'sync': ('tmdbhelper.lib.api.mdblist.sync.datatype', 'MDbListSyncWatched', )
        },
        'paused_progress': {
            'data': 'INTEGER',
            'sync': ('tmdbhelper.lib.api.mdblist.sync.datatype', 'MDbListSyncPaused', )
        },
        'paused_at': {
            'data': 'TEXT',
            'sync': ('tmdbhelper.lib.api.mdblist.sync.datatype', 'MDbListSyncPaused', )
        },
        'paused_id': {
            'data': 'INTEGER',
            'sync': ('tmdbhelper.lib.api.mdblist.sync.datatype', 'MDbListSyncPaused', )
        },
        'watchlist_listed_at': {
            'data': 'TEXT',
            'sync': ('tmdbhelper.lib.api.mdblist.sync.datatype', 'MDbListSyncWatchlist', )
        },
        'collection_collected_at': {
            'data': 'TEXT',
            'sync': ('tmdbhelper.lib.api.mdblist.sync.datatype', 'MDbListSyncCollection', )
        },
        'rating': {
            'data': 'INTEGER',
            'sync': None
        },
        'rated_at': {
            'data': 'TEXT',
            'sync': None
        },
    }
