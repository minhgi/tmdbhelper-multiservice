class WeTrakrSyncItem:
    """ Turns what WeTrakr's compact lists returned into the {item_id: [values]} dict DataBase.set_many_values() writes: the base values of the sync
    (in the order of its keys), then item_type, tmdb_type, tmdb_id, season_number, episode_number. TMDb ids and TMDb numbering throughout: WeTrakr
    resolves every title by TMDb id, so no numbering check is needed (unlike Simkl's TVDB-numbered episodes). """

    ADDITIONAL_KEYS = ('item_type', 'tmdb_type', 'tmdb_id', 'season_number', 'episode_number')

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
    def _row(base_values, item_type, tmdb_id, season=None, episode=None):
        return [*base_values, item_type, 'movie' if item_type == 'movie' else 'tv', tmdb_id, season, episode]

    def _parse_watched(self, meta):
        """ base values: (plays, last_watched_at, watched_episodes). A movie / episode row carries plays and when; a season row the number of its
        watched episodes; a show row the number of watched episodes of its seasons above 0 (specials are left out of the show total, as Trakt does) """
        data = {}
        for row in (meta.get('movies') or []):
            tmdb_id = self._movie_tmdb(row)
            if tmdb_id:
                data[f'movie.{tmdb_id}'] = self._row([self._int(row.get('plays')) or 1, row.get('last_watched_at'), None], 'movie', tmdb_id)
        seasons, shows = {}, {}
        for row in (meta.get('episodes') or []):
            tmdb_id = self._show_tmdb(row)
            season, number = self._int(row.get('season_number')), self._int(row.get('number'))
            if not tmdb_id or season is None or number is None:
                continue
            item_id = f'tv.{tmdb_id}.{season}.{number}'
            if item_id in data:
                continue  # one row per watched episode; a repeated one is counted once
            data[item_id] = self._row([self._int(row.get('plays')) or 1, row.get('last_watched_at'), None], 'episode', tmdb_id, season, number)
            seasons[(tmdb_id, season)] = seasons.get((tmdb_id, season), 0) + 1
            if season > 0:
                shows[tmdb_id] = shows.get(tmdb_id, 0) + 1
        for (tmdb_id, season), count in seasons.items():
            data[f'tv.{tmdb_id}.{season}'] = self._row([None, None, count], 'season', tmdb_id, season)
        for tmdb_id, count in shows.items():
            data[f'tv.{tmdb_id}'] = self._row([None, None, count], 'show', tmdb_id)
        return data

    def _parse_playback(self, meta):
        """ base values: (paused_progress, paused_at), from the PAUSED rows of the playing list. A row whose status is 'playing' is not a resume
        point: its percentage is moved along by WeTrakr's clock. A pause at 0 % is only a started player. """
        data = {}
        for row in (meta.get('movies') or []):
            tmdb_id, progress = self._movie_tmdb(row), self._progress(row)
            if tmdb_id and progress:
                data[f'movie.{tmdb_id}'] = self._row([progress, row.get('tracked_at')], 'movie', tmdb_id)
        for row in (meta.get('episodes') or []):
            tmdb_id, progress = self._show_tmdb(row), self._progress(row)
            season, number = self._int(row.get('season_number')), self._int(row.get('number'))
            if tmdb_id and progress and season is not None and number is not None:
                data[f'tv.{tmdb_id}.{season}.{number}'] = self._row([progress, row.get('tracked_at')], 'episode', tmdb_id, season, number)
        return data

    def _parse_ratings(self, meta):
        """ base values: (rating, rated_at), from GET /sync/ratings/all. The rows differ by type (live-probed 2026-11-21):
        a movie or show row has the title's TMDb id in ids; a season row has the season number in `number` and the show in show_ids (its own ids.tmdb is
        the SEASON's TMDb id); an episode row has season_number and number and the show in show_ids (its ids.tmdb is the episode's own). So a season or
        episode is keyed by the show's id, never by its own. A rating is 0-10 with one decimal (8.5); anything else is left out. """
        data = {}
        for row in (meta.get('ratings') or []):
            rating = self._rating(row.get('rating'))
            if not rating:
                continue
            values = [rating, row.get('rated_at')]
            kind = row.get('type')
            if kind == 'movie':
                tmdb_id = self._movie_tmdb(row)
                if tmdb_id:
                    data[f'movie.{tmdb_id}'] = self._row(values, 'movie', tmdb_id)
            elif kind == 'show':
                tmdb_id = self._movie_tmdb(row)  # a show row carries the show's own ids
                if tmdb_id:
                    data[f'tv.{tmdb_id}'] = self._row(values, 'show', tmdb_id)
            elif kind == 'season':
                tmdb_id, season = self._show_tmdb(row), self._int(row.get('number'))
                if tmdb_id and season is not None:
                    data[f'tv.{tmdb_id}.{season}'] = self._row(values, 'season', tmdb_id, season)
            elif kind == 'episode':
                tmdb_id, season, number = self._show_tmdb(row), self._int(row.get('season_number')), self._int(row.get('number'))
                if tmdb_id and season is not None and number is not None:
                    data[f'tv.{tmdb_id}.{season}.{number}'] = self._row(values, 'episode', tmdb_id, season, number)
        return data

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
