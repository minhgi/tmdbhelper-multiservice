class WeTrakrSyncItem:
    """ Turns what WeTrakr's compact lists returned into the {item_id: [values]} dict DataBase.set_many_values() writes: the base values of the sync
    (in the order of its keys), then item_type, tmdb_type, tmdb_id, season_number, episode_number. TMDb ids and TMDb numbering throughout: WeTrakr
    resolves every title by TMDb id, so no numbering check is needed (unlike Simkl's TVDB-numbered episodes). """

    ADDITIONAL_KEYS = ('item_type', 'tmdb_type', 'tmdb_id', 'season_number', 'episode_number', 'wt_id')

    def __init__(self, method, meta, keys):
        self._method = method
        self._meta = meta
        self._base_keys = keys

    @property
    def base_table_keys(self):
        return self._base_keys

    @property
    def table_keys(self):
        return (*self._base_keys, *self.ADDITIONAL_KEYS)

    @property
    def data(self):
        if self._method == 'watched':
            return self._parse_watched(self._meta)
        if self._method == 'playback':
            return self._parse_playback(self._meta)
        if self._method == 'ratings':
            return self._parse_ratings(self._meta)
        return {}

    @staticmethod
    def _int(value):
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    @classmethod
    def _show_tmdb(cls, row):
        return cls._int((row.get('show_ids') or {}).get('tmdb'))

    @classmethod
    def _movie_tmdb(cls, row):
        return cls._int((row.get('ids') or {}).get('tmdb'))

    @staticmethod
    def _row(base_values, item_type, tmdb_id, season=None, episode=None, wt_id=None):
        return [*base_values, item_type, 'movie' if item_type == 'movie' else 'tv', tmdb_id, season, episode, wt_id]

    @classmethod
    def _movie_wt(cls, row):
        """ WeTrakr's own id of a movie row (the row's id) """
        return cls._int(row.get('id'))

    @classmethod
    def _show_wt(cls, row):
        """ WeTrakr's own id of the SHOW an episode / season row belongs to (the journal names an episode by it, not by the show's TMDb id) """
        return cls._int(row.get('show_id'))

    def _parse_watched(self, meta):
        """ base values: (plays, last_watched_at, watched_episodes). A movie / episode row carries plays and when; a season row the number of its
        watched episodes; a show row the number of watched episodes of its seasons above 0 (specials are left out of the show total, as Trakt does) """
        data = {}
        for row in (meta.get('movies') or []):
            tmdb_id = self._movie_tmdb(row)
            if tmdb_id:
                data[f'movie.{tmdb_id}'] = self._row([self._int(row.get('plays')) or 1, row.get('last_watched_at'), None], 'movie', tmdb_id, wt_id=self._movie_wt(row))
        seasons, shows = {}, {}
        for row in (meta.get('episodes') or []):
            tmdb_id = self._show_tmdb(row)
            season, number = self._int(row.get('season_number')), self._int(row.get('number'))
            if not tmdb_id or season is None or number is None:
                continue
            item_id = f'tv.{tmdb_id}.{season}.{number}'
            if item_id in data:
                continue  # one row per watched episode; a repeated one is counted once
            wt_id = self._show_wt(row)
            data[item_id] = self._row([self._int(row.get('plays')) or 1, row.get('last_watched_at'), None], 'episode', tmdb_id, season, number, wt_id)
            seasons[(tmdb_id, season)] = (seasons.get((tmdb_id, season), (0, None))[0] + 1, wt_id)
            if season > 0:
                shows[tmdb_id] = (shows.get(tmdb_id, (0, None))[0] + 1, wt_id)
        for (tmdb_id, season), (count, wt_id) in seasons.items():
            data[f'tv.{tmdb_id}.{season}'] = self._row([None, None, count], 'season', tmdb_id, season, wt_id=wt_id)
        for tmdb_id, (count, wt_id) in shows.items():
            data[f'tv.{tmdb_id}'] = self._row([None, None, count], 'show', tmdb_id, wt_id=wt_id)
        return data

    def delta_watched(self):
        """ The rows of an incremental read of the watched lists (GET ... from_date): only the movie and episode rows, as the full read writes them.
        The season and show counts are NOT worked out here (a delta holds only some of the episodes): the caller recounts them from the stored
        episode rows. Returns (rows {item_id: values}, episode keys {item_id: (show_tmdb, season, number)}) """
        data, keys = {}, {}
        for row in (self._meta.get('movies') or []):
            tmdb_id = self._movie_tmdb(row)
            if tmdb_id:
                data[f'movie.{tmdb_id}'] = self._row([self._int(row.get('plays')) or 1, row.get('last_watched_at'), None], 'movie', tmdb_id, wt_id=self._movie_wt(row))
        for row in (self._meta.get('episodes') or []):
            tmdb_id = self._show_tmdb(row)
            season, number = self._int(row.get('season_number')), self._int(row.get('number'))
            if not tmdb_id or season is None or number is None:
                continue
            item_id = f'tv.{tmdb_id}.{season}.{number}'
            if item_id in data:
                continue
            data[item_id] = self._row([self._int(row.get('plays')) or 1, row.get('last_watched_at'), None], 'episode', tmdb_id, season, number, self._show_wt(row))
            keys[item_id] = (tmdb_id, season, number)
        return data, keys

    def _parse_playback(self, meta):
        """ base values: (paused_progress, paused_at), from the PAUSED rows of the playing list. A row whose status is 'playing' is not a resume
        point: its percentage is moved along by WeTrakr's clock. A pause at 0 % is only a started player. """
        data = {}
        for row in (meta.get('movies') or []):
            tmdb_id, progress = self._movie_tmdb(row), self._progress(row)
            if tmdb_id and progress:
                data[f'movie.{tmdb_id}'] = self._row([progress, row.get('tracked_at')], 'movie', tmdb_id, wt_id=self._movie_wt(row))
        for row in (meta.get('episodes') or []):
            tmdb_id, progress = self._show_tmdb(row), self._progress(row)
            season, number = self._int(row.get('season_number')), self._int(row.get('number'))
            if tmdb_id and progress and season is not None and number is not None:
                data[f'tv.{tmdb_id}.{season}.{number}'] = self._row([progress, row.get('tracked_at')], 'episode', tmdb_id, season, number, self._show_wt(row))
        return data

    def _parse_ratings(self, meta):
        """ base values: (rating, rated_at), from GET /sync/ratings/all. The rows differ by type (live-probed 2026-11-21):
        a movie or show row has the title's TMDb id in ids; a season row has the season number in `number` and the show in show_ids (its own ids.tmdb is
        the SEASON's TMDb id); an episode row has season_number and number and the show in show_ids (its ids.tmdb is the episode's own). So a season or
        episode is keyed by the show's id, never by its own. A rating is 0-10 with one decimal (8.5); anything else is left out. """
        data = {}
        for row in (meta.get('ratings') or []):
            rating = self._rating(row.get('rating'))
            target = self._rating_target(row)
            if rating and target:
                item_id, item_type, tmdb_id, season, number, wt_id = target
                data[item_id] = self._row([rating, row.get('rated_at')], item_type, tmdb_id, season, number, wt_id)
        return data

    def _rating_target(self, row):
        """ Which stored row a ratings row is about: (item_id, item_type, tmdb_id, season, number, wt_id), or None when it cannot be told. A removed
        row of an incremental read (status 'removed', no rating) keeps the same ids, so the same key finds the row to clear. """
        kind = row.get('type')
        if kind == 'movie':
            tmdb_id = self._movie_tmdb(row)
            return (f'movie.{tmdb_id}', 'movie', tmdb_id, None, None, self._movie_wt(row)) if tmdb_id else None
        if kind == 'show':
            tmdb_id = self._movie_tmdb(row)  # a show row carries the show's own ids
            return (f'tv.{tmdb_id}', 'show', tmdb_id, None, None, self._movie_wt(row)) if tmdb_id else None
        if kind == 'season':
            tmdb_id, season = self._show_tmdb(row), self._int(row.get('number'))
            return (f'tv.{tmdb_id}.{season}', 'season', tmdb_id, season, None, self._show_wt(row)) if tmdb_id and season is not None else None
        if kind == 'episode':
            tmdb_id, season, number = self._show_tmdb(row), self._int(row.get('season_number')), self._int(row.get('number'))
            if tmdb_id and season is not None and number is not None:
                return (f'tv.{tmdb_id}.{season}.{number}', 'episode', tmdb_id, season, number, self._show_wt(row))
        return None

    def delta_ratings(self):
        """ An incremental read of the ratings (GET /sync/ratings/all?from_date=): (rows to write {item_id: values}, item ids whose rating was removed).
        A removed row has status 'removed' and no rating (live-probed 2026-11-23). """
        rows, removed = {}, []
        for row in (self._meta.get('ratings') or []):
            target = self._rating_target(row)
            if not target:
                continue
            if row.get('status') == 'removed':
                removed.append(target[0])
                continue
            rating = self._rating(row.get('rating'))
            if rating:
                item_id, item_type, tmdb_id, season, number, wt_id = target
                rows[item_id] = self._row([rating, row.get('rated_at')], item_type, tmdb_id, season, number, wt_id)
        return rows, removed

    @staticmethod
    def _rating(value):
        try:
            rating = float(value)
        except (TypeError, ValueError):
            return None
        return rating if 0 < rating <= 10 else None

    @staticmethod
    def _progress(row):
        if row.get('status') != 'paused':
            return None
        try:
            progress = int(round(float(row.get('progress_percent'))))
        except (TypeError, ValueError):
            return None
        return progress if progress > 0 else None
