from tmdbhelper.lib.files.ftools import cached_property


class SimklSyncItem:
    """
    Parses raw Simkl API responses into the {item_id: [values]} dict that
    SyncData.set_many_values() expects.  One instance per sync method.

    Phase B covers watched only (plays + watched_episodes + last_watched_at).
    Episode-level rows are omitted — Simkl uses TVDB episode numbers which
    may diverge from TMDb numbers. Phase D adds resolution via external_ids.
    """

    _additional_keys = (
        'item_type', 'tmdb_type', 'tmdb_id', 'season_number', 'episode_number',
    )

    def __init__(self, method, meta, keys):
        self._method = method
        self._meta = meta
        self._base_keys = keys

    @property
    def base_keys(self):
        return self._base_keys

    @property
    def additional_keys(self):
        return self._additional_keys

    @property
    def keys(self):
        return (*self.base_keys, *self.additional_keys)

    @property
    def base_table_keys(self):
        return self.base_keys

    @property
    def table_keys(self):
        return self.keys

    @cached_property
    def data(self):
        return self.get_data()

    def get_data(self):
        if self._method == 'watched':
            return self._parse_watched(self._meta)
        if self._method == 'playback':
            return self._parse_playback(self._meta)
        return {}

    @staticmethod
    def _row(base_values, item_type, tmdb_type, tmdb_id,
             season_number=None, episode_number=None):
        return [*base_values, item_type, tmdb_type, tmdb_id, season_number, episode_number]

    def _parse_watched(self, meta):
        if not meta:
            return {}
        data = {}

        # --- Movies ---
        # POST /sync/all-items/movies/completed response:
        # {"movies": [{"movie": {"ids": {"tmdb": "508", ...}}, "last_watched_at": "..."}]}
        for item in (meta.get('movies') or []):
            ids = (item.get('movie') or {}).get('ids', {})
            tmdb_id = ids.get('tmdb')
            if not tmdb_id:
                continue
            try:
                tmdb_id = int(tmdb_id)
            except (ValueError, TypeError):
                continue
            last_watched_at = item.get('last_watched_at')
            item_id = f'movie.{tmdb_id}'
            # base_keys = ('plays', 'last_watched_at', 'watched_episodes')
            data[item_id] = self._row(
                [1, last_watched_at, None],
                'movie', 'movie', tmdb_id)

        # --- Shows ---
        # POST /sync/all-items/shows/?extended=full response:
        # {"shows": [{"show": {"ids": {"tmdb": "2316", ...}},
        #             "status": "watching",
        #             "total_episodes_count": 195,
        #             "not_aired_episodes_count": 0,
        #             "seasons": [{"number": 1, "episodes": [{"number": 1}]}]}]}
        # seasons[].episodes[] = WATCHED episodes (TVDB numbers).
        # We write show-level and season-level watched counts only (no episode rows).
        for item in (meta.get('shows') or []):
            ids = (item.get('show') or {}).get('ids', {})
            tmdb_id = ids.get('tmdb')
            if not tmdb_id:
                continue
            try:
                tmdb_id = int(tmdb_id)
            except (ValueError, TypeError):
                continue

            total_watched = 0
            for season in (item.get('seasons') or []):
                season_num = season.get('number')
                if season_num is None:
                    continue
                season_watched = len(season.get('episodes') or [])
                total_watched += season_watched
                if season_watched == 0:
                    continue
                season_id = f'tv.{tmdb_id}.{season_num}'
                data[season_id] = self._row(
                    [None, None, season_watched],
                    'season', 'tv', tmdb_id, season_num)

            show_id = f'tv.{tmdb_id}'
            data[show_id] = self._row(
                [None, None, total_watched],
                'show', 'tv', tmdb_id)

        return data

    def _parse_playback(self, meta):
        """GET /sync/playback response (movies only).

        Response shape:
          [{"type": "movie"|"episode",
            "movie": {"ids": {"tmdb": "76341"}},   (if movie)
            "show":  {"ids": {"tmdb": "1399"}},    (if episode — skipped, see below)
            "episode": {"season": N, "number": N}, (if episode — skipped)
            "progress": 75.5, "paused_at": "...", "id": N}]

        Episode entries are intentionally skipped: Simkl stores episodes under
        TVDB season/episode numbers, but set_playprogress() looks up by TMDb
        numbers (from li.infolabels).  Writing TVDB-numbered episode rows would
        miss on lookup or hit the wrong episode for anime.  The scrobbler already
        writes locally-correct TMDb-numbered episode rows; those are preserved by
        clearing only movie rows in clear_columns().
        """
        if not meta:
            return {}
        data = {}
        for item in (meta if isinstance(meta, list) else []):
            if item.get('type') != 'movie':
                continue  # episode entries skipped — see docstring
            progress = item.get('progress')
            if progress is None:
                continue
            ids = (item.get('movie') or {}).get('ids', {})
            tmdb_id = ids.get('tmdb')
            if not tmdb_id:
                continue
            try:
                tmdb_id = int(tmdb_id)
            except (ValueError, TypeError):
                continue
            item_id = f'movie.{tmdb_id}'
            # base_keys = ('paused_progress', 'paused_at', 'paused_id')
            data[item_id] = self._row(
                [int(round(float(progress))), item.get('paused_at'), item.get('id')],
                'movie', 'movie', tmdb_id)
        return data
