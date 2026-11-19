from tmdbhelper.lib.files.ftools import cached_property


# Simkl's status word -> the local SimklSync.db flag column. Simkl names the Dropped status per app token:
# this addon's token reads back 'notinteresting', a Seren-app token 'dropped' - both are the dropped flag.
STATUS_COLUMN = {
    'plantowatch': 'plantowatch_at',
    'hold': 'hold_at',
    'notinteresting': 'dropped_at',
    'dropped': 'dropped_at',
    'watching': 'watching_at',
}


class SimklSyncItem:
    """
    Parses raw Simkl API responses into the {item_id: [values]} dict that
    SyncData.set_many_values() expects.  One instance per sync method.

    Phase B covers watched only (plays + watched_episodes + last_watched_at), plus the user's
    own rating (user_rating / user_rated_at on every library entry) and the account's list status
    (plantowatch_at / hold_at / dropped_at / watching_at, see collect_statuses).
    Episode-level rows are omitted — Simkl uses TVDB episode numbers which
    may diverge from TMDb numbers. Phase D adds resolution via external_ids.
    """

    _NO_STATUS = (None, None, None, None)

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

    @staticmethod
    def _is_completed_show(item, watched):
        """True for a regular show Simkl lists as completed with every aired episode watched (watched >= total - not aired, in Simkl's
        own counts, both sent with extended=full). Anime entries are never flagged: Simkl numbers each cour on its own."""
        if item.get('anime_type') or item.get('status') != 'completed':
            return False
        try:
            total = int(item.get('total_episodes_count') or 0)
            not_aired = int(item.get('not_aired_episodes_count') or 0)
        except (ValueError, TypeError):
            return False
        return total > 0 and watched >= total - not_aired

    @staticmethod
    def _rating(item):
        """(rating, rated_at) of a library entry: user_rating is 1-10 or null when never rated."""
        try:
            rating = int(item.get('user_rating') or 0) or None
        except (ValueError, TypeError):
            rating = None
        return rating, (item.get('user_rated_at') if rating else None)

    @staticmethod
    def _tmdb_id(obj):
        try:
            return int(((obj or {}).get('ids') or {}).get('tmdb'))
        except (ValueError, TypeError):
            return None

    @classmethod
    def collect_statuses(cls, movie_rows, show_rows, anime_rows):
        """[(kind, tmdb_id, status, added_at)] for every library entry that sits on a plan-to-watch / hold /
        dropped / watching list, from the raw all-items rows (every status) of movies, shows and anime.

        kind is 'movie' or 'show'. 'completed' entries are not a list status and are left out, as are entries
        without a TMDb id (nothing here could look them up). Anime movies are left out as well: the Remove
        action looks a movie up in Simkl's 'movies' bucket only, so a flag taken from the 'anime' bucket would
        offer Remove, find nothing to remove and clear itself - to return on the next sync.
        Anime entries keep anime_type at the top level and the ids under 'show' (live shape, 2026-10-14).
        """
        found = []

        def add(kind, obj, entry):
            status = entry.get('status')
            tmdb_id = cls._tmdb_id(obj)
            if status in STATUS_COLUMN and tmdb_id:
                found.append((kind, tmdb_id, status, entry.get('added_to_watchlist_at') or entry.get('last_watched_at')))

        for entry in (movie_rows or []):
            add('movie', entry.get('movie'), entry)
        for entry in (show_rows or []):
            add('show', entry.get('show'), entry)
        for entry in (anime_rows or []):
            if entry.get('anime_type') != 'movie':
                add('show', entry.get('show') or entry, entry)
        return found

    def _apply_statuses(self, data, statuses):
        """Set the status flag of each (kind, tmdb_id, status, added_at); a status-only item gets a row of its own."""
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.000Z')
        for kind, tmdb_id, status, added_at in statuses:
            movie = kind == 'movie'
            item_id = f'movie.{tmdb_id}' if movie else f'tv.{tmdb_id}'
            row = data.get(item_id)
            if row is None:
                row = data[item_id] = self._row(
                    [None] * len(self._base_keys),
                    'movie' if movie else 'show', 'movie' if movie else 'tv', tmdb_id)
            row[self._base_keys.index(STATUS_COLUMN[status])] = added_at or now

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
            # base_keys = ('plays', 'last_watched_at', 'watched_episodes', 'rating', 'rated_at', *status flags)
            data[item_id] = self._row(
                [1, last_watched_at, None, *self._rating(item), *self._NO_STATUS],
                'movie', 'movie', tmdb_id)

        # --- Shows ---
        # POST /sync/all-items/shows/?extended=full response:
        # {"shows": [{"show": {"ids": {"tmdb": "2316", ...}},
        #             "status": "watching",
        #             "total_episodes_count": 195,
        #             "not_aired_episodes_count": 0,
        #             "seasons": [{"number": 1, "episodes": [{"number": 1}]}]}]}
        # seasons[].episodes[] = WATCHED episodes (TVDB numbers), sent only for entries still in progress
        # (watching / hold). A completed or dropped entry carries just "watched_episodes_count", which then
        # stands in for the show-level count (live check 2026-10-14).
        # We write show-level and season-level watched counts, and (7.0.97) one episode row per watched episode of a
        # regular show: its season numbers already stand in for TMDb's in the season rows, and live checks (2026-11-16,
        # two shows) found Simkl's watched episode numbers identical to Trakt's, so an episode list agrees with its season
        # row. Anime entries (anime_type set) are one cour each and always number it season 1, which says nothing about
        # the TMDb season, so here they get the show-level count only; their episodes are mapped separately, below.
        entry_totals = {}   # tmdb id -> watched count summed over EVERY entry of that id (an anime show can have several cours)
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
                if season_watched == 0 or item.get('anime_type'):
                    continue
                season_id = f'tv.{tmdb_id}.{season_num}'
                data[season_id] = self._row(
                    [None, None, season_watched, None, None, *self._NO_STATUS],
                    'season', 'tv', tmdb_id, season_num)
                for episode in (season.get('episodes') or []):
                    try:
                        episode_num = int(episode.get('number'))
                    except (ValueError, TypeError):
                        continue
                    # plays only: ifnull() in the write keeps the last_watched_at a local mark already has
                    data[f'{season_id}.{episode_num}'] = self._row(
                        [1, None, None, None, None, *self._NO_STATUS],
                        'episode', 'tv', tmdb_id, season_num, episode_num)

            if not total_watched:
                try:
                    total_watched = max(int(item.get('watched_episodes_count') or 0), 0)
                except (ValueError, TypeError):
                    total_watched = 0

            entry_totals[tmdb_id] = entry_totals.get(tmdb_id, 0) + total_watched
            show_id = f'tv.{tmdb_id}'
            # plays = 1 on a show row means "every aired episode of this show is watched": a regular (non-anime) show Simkl lists as
            # completed whose watched count reaches its own aired total. A completed entry sends no episode list, so this is what lets
            # the episode lists show its episodes as watched (items/simkl.py). It is derived from Simkl's counts on every sync, so a show
            # that gains aired episodes (or loses a watched one) stops being flagged by itself.
            data[show_id] = self._row(
                [1 if self._is_completed_show(item, total_watched) else None, None, total_watched, *self._rating(item), *self._NO_STATUS],
                'show', 'tv', tmdb_id)

        # --- Anime, mapped to TMDb numbers by the sync (SimklSyncWatched.map_anime_episodes): one episode row per mapped episode.
        # Season rows (a season's watched count = the episodes mapped into it) only when the mapping accounts for all of the show's
        # count on Simkl: a cour Simkl completed in one go carries a total and no episode list, so a partial season count would be
        # wrong (Apothecary Diaries: 49 watched, one mapped). The show-level count above stays Simkl's own total. ---
        for tmdb_id, seasons in (meta.get('anime_mapped') or {}).items():
            mapped_total = sum(len(set(episodes)) for episodes in seasons.values())
            complete = bool(entry_totals.get(tmdb_id)) and entry_totals[tmdb_id] == mapped_total
            for season_num, episodes in seasons.items():
                season_id = f'tv.{tmdb_id}.{season_num}'
                if complete:
                    data[season_id] = self._row(
                        [None, None, len(set(episodes)), None, None, *self._NO_STATUS],
                        'season', 'tv', tmdb_id, season_num)
                for episode_num in set(episodes):
                    data[f'{season_id}.{episode_num}'] = self._row(
                        [1, None, None, None, None, *self._NO_STATUS],
                        'episode', 'tv', tmdb_id, season_num, episode_num)

        # --- List status (plantowatch / hold / dropped / watching), from collect_statuses ---
        self._apply_statuses(data, meta.get('statuses') or [])

        return data

    def _parse_playback(self, meta):
        """GET /sync/playback response (movies, and the episodes of regular shows).

        Response shape:
          [{"type": "movie"|"episode",
            "movie": {"ids": {"tmdb": "76341"}},   (if movie)
            "show":  {"ids": {"tmdb": "1399"}},    (if a regular show's episode)
            "anime": {"ids": {"tmdb": "..."}},     (if an anime's episode — skipped, see below)
            "episode": {"season": N, "number": N}, (if episode)
            "progress": 75.5, "paused_at": "...", "id": N}]

        Anime episode entries are skipped: Simkl numbers an anime per cour, so the number cannot be matched to a TMDb episode
        (the scrobbler writes the locally-correct TMDb-numbered row for those). A regular show's episode is written under its
        Simkl (TVDB) numbers, which SimklSyncPaused only lets through for a show season whose numbering agrees with TMDb's
        (7.0.109, SimklSyncPaused._gate_episodes). A pause at 0 % is only a started player and is skipped; the newest pause of
        the same episode wins.
        """
        if not meta:
            return {}
        data = {}
        episode_at = {}
        for item in (meta if isinstance(meta, list) else []):
            if item.get('type') == 'episode':
                if not isinstance(item.get('show'), dict):
                    continue  # anime entry — see docstring
                try:
                    progress = float(item.get('progress'))
                    tmdb_id = int(((item.get('show') or {}).get('ids') or {}).get('tmdb'))
                    season, number = int((item.get('episode') or {}).get('season')), int((item.get('episode') or {}).get('number'))
                except (TypeError, ValueError):
                    continue
                if progress <= 0:
                    continue
                item_id = f'tv.{tmdb_id}.{season}.{number}'
                at = str(item.get('paused_at') or '')
                if item_id in episode_at and at <= episode_at[item_id]:
                    continue
                episode_at[item_id] = at
                data[item_id] = self._row(
                    [int(round(progress)), item.get('paused_at'), item.get('id')],
                    'episode', 'tv', tmdb_id, season, number)
                continue
            if item.get('type') != 'movie':
                continue
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
