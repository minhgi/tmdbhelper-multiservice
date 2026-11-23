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
        # WeTrakr's own id of the title (movie rows) or of the show (show, season and episode rows): the journal names an episode by it (7.5.20).
        # Never cleared by a rewrite of a sync's columns; a row written before 7.5.20 has none until the next full read.
        'wt_id': {'data': 'INTEGER', 'sync': None},
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

    # -- incremental sync helpers (7.5.20): plain SQL on the simplecache table --------------------------------------------------

    def select_rows(self, query, data=()):
        """ The rows of a read-only query, or None when it failed """
        result = self.execute_sql(query, data, read_only=True)
        return result.fetchall() if result else None

    def show_tmdb_of(self, wt_id):
        """ The TMDb id of the show whose WeTrakr id is wt_id, from any stored row of it (episode, season or show); None when no stored row knows it """
        rows = self.select_rows(
            "SELECT tmdb_id FROM simplecache WHERE wt_id=? AND item_type IN ('episode', 'season', 'show') AND tmdb_id IS NOT NULL LIMIT 1", (wt_id,))
        return rows[0][0] if rows else None

    def clear_values(self, item_ids, keys):
        """ Set these columns to NULL on these rows (set_many_values cannot: it keeps the old value when given NULL). False when it failed. """
        item_ids = list(item_ids)
        if not item_ids:
            return True
        query = 'UPDATE simplecache SET {} WHERE id=?'.format(', '.join(f'{k}=NULL' for k in keys))
        return self.execute_sql(query, [(i,) for i in item_ids]) is not None

    def watched_counts(self, show_tmdb_ids):
        """ From the stored episode rows: ({(show_tmdb, season): watched episodes}, {show_tmdb: watched episodes of the seasons above 0}) for these shows """
        seasons, shows = {}, {}
        for tmdb_id in show_tmdb_ids:
            rows = self.select_rows(
                "SELECT season_number, COUNT(*) FROM simplecache WHERE item_type='episode' AND tmdb_id=? AND plays IS NOT NULL AND plays>0 GROUP BY season_number",
                (tmdb_id,))
            if rows is None:
                return None
            for season, count in rows:
                seasons[(tmdb_id, season)] = count
                if season is not None and season > 0:
                    shows[tmdb_id] = shows.get(tmdb_id, 0) + count
        return seasons, shows
