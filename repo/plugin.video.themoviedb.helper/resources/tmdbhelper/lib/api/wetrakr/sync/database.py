from tmdbhelper.lib.files.database import DataBase

WATCHED_ROUTE = ('tmdbhelper.lib.api.wetrakr.sync.datatype', 'WeTrakrSyncWatched',)
PAUSED_ROUTE = ('tmdbhelper.lib.api.wetrakr.sync.datatype', 'WeTrakrSyncPaused',)
RATINGS_ROUTE = ('tmdbhelper.lib.api.wetrakr.sync.datatype', 'WeTrakrSyncRatings',)


class WeTrakrSyncDataBase(DataBase):

    _migrated = set()  # db files already migrated by this interpreter: the ALTERs need running once, not per instance

    def migrate_columns(self):
        if self._db_file in self._migrated:
            return
        self._migrated.add(self._db_file)
        # Only add the columns the table does not have yet (see SimklSyncDataBase.migrate_columns for why the column list is read first)
        result = self.execute_sql('PRAGMA table_info(simplecache)', ())
        rows = result.fetchall() if result else None
        present = {row[1] for row in rows} if rows else set()
        for col_name, col_def in self.simplecache_columns.items():
            if col_name in present:
                continue
            try:
                self.execute_sql(f'ALTER TABLE simplecache ADD COLUMN {col_name} {col_def["data"]}', ())
            except Exception:
                pass

    # Rows are keyed by TMDb id: movie.<id>, tv.<id> (show), tv.<id>.<season>, tv.<id>.<season>.<episode>
    simplecache_columns = {
        'item_type': {'data': 'TEXT', 'sync': None},
        'tmdb_type': {'data': 'TEXT', 'sync': None},
        'tmdb_id': {'data': 'INTEGER', 'sync': None},
        'season_number': {'data': 'INTEGER', 'sync': None},
        'episode_number': {'data': 'INTEGER', 'sync': None},
        # watched: plays of a movie / episode, and how many episodes of a show (seasons above 0) or season are watched
        'plays': {'data': 'INTEGER DEFAULT 0', 'sync': WATCHED_ROUTE},
        'last_watched_at': {'data': 'TEXT', 'sync': WATCHED_ROUTE},
        'watched_episodes': {'data': 'INTEGER', 'sync': WATCHED_ROUTE},
        # resume point: the paused rows of WeTrakr's "playing" list (percent and when it was paused)
        'paused_progress': {'data': 'INTEGER', 'sync': PAUSED_ROUTE},
        'paused_at': {'data': 'TEXT', 'sync': PAUSED_ROUTE},
        # the account's own rating (0-10, one decimal: 8.5 is stored) and when it was set; rows of the same keys as the watched rows
        'rating': {'data': 'REAL', 'sync': RATINGS_ROUTE},
        'rated_at': {'data': 'TEXT', 'sync': RATINGS_ROUTE},
    }
