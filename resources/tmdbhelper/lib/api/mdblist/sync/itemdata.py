from tmdbhelper.lib.files.ftools import cached_property
from collections import defaultdict

# Show/season rows for a whole-show / whole-season mark carry no per-episode count, so they
# get the same watched_episodes sentinel the context-menu writes use (script/sync/
# mdblist_sync.py): get_playcount() takes min(sentinel, aired episodes) = fully watched.
_BULK_WATCHED_EPISODES = 9999


class MDbListSyncItem:
    """
    Parses raw MDBList API responses into the {item_id: [values]} dict that
    SyncData.set_many_values() expects.  One instance per sync method.
    """

    _additional_keys = (
        'item_type', 'tmdb_type', 'tmdb_id', 'season_number', 'episode_number',
    )

    def __init__(self, method, meta, keys):
        self._method = method   # 'watched', 'playback', 'watchlist', 'collection'
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
        if self._method == 'watchlist':
            return self._parse_watchlist(self._meta)
        if self._method == 'collection':
            return self._parse_collection(self._meta)
        return {}

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _row(base_values, item_type, tmdb_type, tmdb_id,
             season_number=None, episode_number=None):
        """Build the values list matching self.keys order (base_keys + additional_keys)."""
        return [*base_values, item_type, tmdb_type, tmdb_id, season_number, episode_number]

    # ------------------------------------------------------------------
    # /sync/watched parser
    # Response shape (Umbrella reference):
    #   movies:   [{"movie": {"ids": {"tmdb": N}, ...}, "last_watched_at": "..."}]
    #   episodes: [{"episode": {"season": N, "number": N,
    #                           "show": {"ids": {"tmdb": N}}},
    #               "last_watched_at": "..."}]
    # ------------------------------------------------------------------

    def _parse_watched(self, meta):
        if not meta:
            return {}
        data = {}

        # Movies
        for item in (meta.get('movies') or []):
            tmdb_id = (item.get('movie') or {}).get('ids', {}).get('tmdb')
            if not tmdb_id:
                continue
            tmdb_id = int(tmdb_id)
            last_watched_at = item.get('last_watched_at') or item.get('watched_at')
            item_id = f'movie.{tmdb_id}'
            # base_keys = ('plays', 'last_watched_at', 'watched_episodes')
            data[item_id] = self._row(
                [1, last_watched_at, None],
                'movie', 'movie', tmdb_id)

        # Episodes — aggregate into show/season/episode rows
        # show_data[tmdb_id] = {'last_watched_at': max_ts, episodes: {(s,e): ts}}
        show_data = defaultdict(lambda: {'last_watched_at': None, 'seasons': defaultdict(list)})
        for item in (meta.get('episodes') or []):
            ep = item.get('episode') or {}
            show_ids = ep.get('show', {}).get('ids', {}) if isinstance(ep.get('show'), dict) else {}
            tmdb_id = show_ids.get('tmdb')
            if not tmdb_id:
                continue
            tmdb_id = int(tmdb_id)
            season = ep.get('season')
            episode = ep.get('number')
            if season is None or episode is None:
                continue
            last_watched_at = item.get('last_watched_at') or item.get('watched_at')

            # Episode row
            ep_id = f'tv.{tmdb_id}.{season}.{episode}'
            data[ep_id] = self._row(
                [1, last_watched_at, None],
                'episode', 'tv', tmdb_id, season, episode)

            # Accumulate for aggregation
            sd = show_data[tmdb_id]
            if not sd['last_watched_at'] or (last_watched_at and last_watched_at > sd['last_watched_at']):
                sd['last_watched_at'] = last_watched_at
            sd['seasons'][season].append(episode)

        # Write aggregated season and show rows (individually-watched episodes)
        for tmdb_id, sd in show_data.items():
            total_watched = 0
            for season, episodes in sd['seasons'].items():
                season_watched = len(set(episodes))
                total_watched += season_watched
                season_id = f'tv.{tmdb_id}.{season}'
                data[season_id] = self._row(
                    [None, None, season_watched],
                    'season', 'tv', tmdb_id, season)

            show_id = f'tv.{tmdb_id}'
            data[show_id] = self._row(
                [None, sd['last_watched_at'], total_watched],
                'show', 'tv', tmdb_id)

        # Bulk marks. MDBList stores a whole-season / whole-show mark as one entry in the
        # 'seasons' / 'shows' buckets with no per-episode rows. A mark only fills in a show
        # or season that has NO individually-watched episodes: where individual rows exist
        # they stay the truth, because the cross-service bridge pushes every started Trakt
        # show to MDBList as a whole-show mark, so a mark alone does not mean the show is
        # finished (5 of the 89 marked shows on the test account were still in progress).
        # A season mark only credits its own season row - never the show total, or one
        # marked season would make the whole show look watched. Episode rows under a bulk
        # mark stay unwritten.
        tracked_seasons = {}
        for tmdb_id, sd in show_data.items():
            tracked_seasons[tmdb_id] = {n for n in map(self._to_int, sd['seasons']) if n is not None}

        for item in (meta.get('seasons') or []):
            season = item.get('season') or {}
            tmdb_id = self._to_int(((season.get('show') or {}).get('ids') or {}).get('tmdb'))
            number = self._to_int(season.get('number'))
            if tmdb_id is None or number is None:
                continue
            if number in tracked_seasons.get(tmdb_id, ()):
                continue
            data[f'tv.{tmdb_id}.{number}'] = self._row(
                [None, None, _BULK_WATCHED_EPISODES], 'season', 'tv', tmdb_id, number)

        for item in (meta.get('shows') or []):
            tmdb_id = self._to_int(((item.get('show') or {}).get('ids') or {}).get('tmdb'))
            if tmdb_id is None or tmdb_id in show_data:
                continue
            data[f'tv.{tmdb_id}'] = self._row(
                [None, item.get('last_watched_at') or item.get('watched_at'), _BULK_WATCHED_EPISODES],
                'show', 'tv', tmdb_id)

        return data

    @staticmethod
    def _to_int(value):
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    # ------------------------------------------------------------------
    # /sync/playback parser
    # Response shape (Umbrella reference):
    #   [{"id": N, "type": "movie"|"episode", "progress": 75.5,
    #     "paused_at": "...",
    #     "movie": {"ids": {"tmdb": N}},   (if type==movie)
    #     "show": {"ids": {"tmdb": N}},    (if type==episode)
    #     "episode": {"season": N, "number": N}}]  (if type==episode)
    # ------------------------------------------------------------------

    def _parse_playback(self, meta):
        if not meta:
            return {}
        data = {}
        for item in (meta if isinstance(meta, list) else []):
            resume_id = item.get('id')
            progress = item.get('progress')
            paused_at = item.get('paused_at')
            if progress is None:
                continue
            progress_int = int(round(float(progress)))

            if item.get('type') == 'episode':
                show_ids = (item.get('show') or {}).get('ids', {})
                tmdb_id = show_ids.get('tmdb')
                if not tmdb_id:
                    continue
                tmdb_id = int(tmdb_id)
                ep = item.get('episode') or {}
                season = ep.get('season')
                episode = ep.get('number')
                if season is None or episode is None:
                    continue
                item_id = f'tv.{tmdb_id}.{season}.{episode}'
                # base_keys = ('paused_progress', 'paused_at', 'paused_id')
                data[item_id] = self._row(
                    [progress_int, paused_at, resume_id],
                    'episode', 'tv', tmdb_id, season, episode)
            else:
                movie_ids = (item.get('movie') or {}).get('ids', {})
                tmdb_id = movie_ids.get('tmdb')
                if not tmdb_id:
                    continue
                tmdb_id = int(tmdb_id)
                item_id = f'movie.{tmdb_id}'
                data[item_id] = self._row(
                    [progress_int, paused_at, resume_id],
                    'movie', 'movie', tmdb_id)
        return data

    # ------------------------------------------------------------------
    # /sync/watchlist parser
    # Response shape: {"movies": [...], "shows": [...]}
    # /watchlist/items returns flat format: each item is {id, mediatype, title, ...}
    # where 'id' is the tmdb_id directly (confirmed v6.0.54 from POV reference).
    # Nested format fallback retained for forward-compatibility.
    # ------------------------------------------------------------------

    def _parse_watchlist(self, meta):
        if not meta:
            return {}
        data = {}
        for media_type, tmdb_type, id_key in (
                ('movies', 'movie', 'movie'),
                ('shows', 'tv', 'show')):
            for item in (meta.get(media_type) or []):
                # Flat format: item['id'] is the tmdb_id; nested format fallback.
                tmdb_id = item.get('id') or (item.get(id_key) or {}).get('ids', {}).get('tmdb')
                if not tmdb_id:
                    continue
                tmdb_id = int(tmdb_id)
                # Timestamp may not exist in flat format; sentinel signals "in watchlist".
                # Same '2000-01-01' fallback used by store_last_activity() in datatype.py.
                listed_at = (item.get('watchlist_at') or item.get('listed_at')
                             or item.get('list_at') or '2000-01-01T00:00:00.000Z')
                item_id = f'{tmdb_type}.{tmdb_id}'
                # base_keys = ('watchlist_listed_at',)
                data[item_id] = self._row(
                    [listed_at],
                    tmdb_type if tmdb_type == 'movie' else 'show',
                    tmdb_type, tmdb_id)
        return data

    # ------------------------------------------------------------------
    # /sync/collection parser
    # Response shape similar to watchlist
    # ------------------------------------------------------------------

    def _parse_collection(self, meta):
        if not meta:
            return {}
        data = {}
        for media_type, tmdb_type, id_key in (
                ('movies', 'movie', 'movie'),
                ('shows', 'tv', 'show')):
            for item in (meta.get(media_type) or []):
                ids = (item.get(id_key) or {}).get('ids', {})
                tmdb_id = ids.get('tmdb')
                if not tmdb_id:
                    continue
                tmdb_id = int(tmdb_id)
                collected_at = item.get('collected_at') or item.get('listed_at')
                item_id = f'{tmdb_type}.{tmdb_id}'
                # base_keys = ('collection_collected_at',)
                data[item_id] = self._row(
                    [collected_at],
                    tmdb_type if tmdb_type == 'movie' else 'show',
                    tmdb_type, tmdb_id)
        return data
