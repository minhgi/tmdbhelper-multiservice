from tmdbhelper.lib.items.container import ContainerDirectory
from tmdbhelper.lib.addon.plugin import get_plugin_category, get_localized, PLUGINPATH
from tmdbhelper.lib.addon.consts import MDBLIST_LIST_OF_LISTS
from tmdbhelper.lib.addon.logger import kodi_log


# ---------------------------------------------------------------------------
# Response normalizers — convert API shapes to flat {id, mediatype} dicts
# that ItemListPagination / ItemMappingBasic expect.
# ---------------------------------------------------------------------------

def _normalize_watched(response):
    """GET /sync/watched → {"movies": [...], "shows": [...], "seasons": [...], "episodes": [...]}
    Movies: {"movie": {"title": "...", "year": N, "ids": {"tmdb": N}}, "last_watched_at": "..."}
    Episodes: {"episode": {"show": {"title": "...", "year": N, "ids": {"tmdb": N}}}, "last_watched_at": "..."}
    Show tmdb_id is at item['episode']['show']['ids']['tmdb']. Title/year carried through so
    ItemMappingBasic has a label before TMDb enrichment.
    Whole-show marks ('shows': {"show": {...}}) and whole-season marks ('seasons':
    {"season": {"show": {...}}}) have no episode rows; their show is listed too, with the
    newest timestamp across all of its entries.
    """
    result = {}

    movies = sorted(
        [{'id': (i.get('movie') or {}).get('ids', {}).get('tmdb'),
          'mediatype': 'movie',
          'title': (i.get('movie') or {}).get('title'),
          'release_year': (i.get('movie') or {}).get('year'),
          'last_watched_at': i.get('last_watched_at')}
         for i in (response.get('movies') or [])
         if (i.get('movie') or {}).get('ids', {}).get('tmdb')],
        key=lambda m: m.get('last_watched_at') or '', reverse=True)
    if movies:
        result['movies'] = movies

    # Aggregate episodes and bulk marks by show — keep latest ts and show metadata
    show_data = {}

    def _add_show(show, item):
        tmdb_id = (show.get('ids') or {}).get('tmdb')
        if not tmdb_id:
            return
        ts = item.get('last_watched_at') or ''
        if tmdb_id not in show_data or ts > show_data[tmdb_id][0]:
            show_data[tmdb_id] = (ts, show)

    for item in (response.get('episodes') or []):
        _add_show((item.get('episode') or {}).get('show') or {}, item)
    for item in (response.get('seasons') or []):
        _add_show((item.get('season') or {}).get('show') or {}, item)
    for item in (response.get('shows') or []):
        _add_show(item.get('show') or {}, item)

    if show_data:
        result['shows'] = [
            {'id': tid, 'mediatype': 'show',
             'title': meta.get('title'), 'release_year': meta.get('year'),
             'last_watched_at': ts}
            for tid, (ts, meta) in sorted(show_data.items(), key=lambda x: x[1][0], reverse=True)
        ]

    return result


def _normalize_playback(items_list):
    """GET /sync/playback → [{"type": "movie"|"episode", "movie"/"show": {"ids": {"tmdb": N}}, ...}]
    Shows are deduplicated — same show can appear once per in-progress episode.
    Title/year carried through so ItemMappingBasic has a label before TMDb enrichment.
    """
    result = {}
    seen_shows = set()
    for item in (items_list or []):
        item_type = item.get('type')
        if item_type == 'movie':
            movie = item.get('movie') or {}
            tmdb_id = movie.get('ids', {}).get('tmdb')
            if tmdb_id:
                result.setdefault('movies', []).append({
                    'id': tmdb_id, 'mediatype': 'movie',
                    'title': movie.get('title'), 'release_year': movie.get('year')})
        elif item_type == 'episode':
            show = item.get('show') or {}
            tmdb_id = show.get('ids', {}).get('tmdb')
            if tmdb_id and tmdb_id not in seen_shows:
                seen_shows.add(tmdb_id)
                result.setdefault('shows', []).append({
                    'id': tmdb_id, 'mediatype': 'show',
                    'title': show.get('title'), 'release_year': show.get('year')})
    return result


class ListLocal(ContainerDirectory):
    def get_items(self, paths, page=None, **kwargs):
        if not paths or not isinstance(paths, list):
            return

        import json
        import xbmcvfs
        import contextlib

        response = None
        filepath = paths[0]

        if filepath.startswith('http'):
            import requests
            response = requests.get(filepath, timeout=10.000)
            response = response.json() if response else None
        else:
            with contextlib.suppress(IOError, json.JSONDecodeError):
                with xbmcvfs.File(filepath, 'r') as file:
                    response = json.load(file)

        if not response:
            return

        response = {"all": response}
        response = self.mdblist_api.get_paginated(response, page=page or 1)

        self.tmdb_cache_only = False
        self.set_mixed_content(response.paginated_items_dict)

        return response.items + response.next_page


class ListLists(ContainerDirectory):
    def get_items(self, info, page=None, **kwargs):
        from xbmcplugin import SORT_METHOD_UNSORTED

        info_model = MDBLIST_LIST_OF_LISTS.get(info)
        path = info_model.get('path', '').format(**kwargs)

        if info_model.get('user_endpoint'):
            items = _get_list_of_lists_user_endpoint(self.mdblist_api, path, page=page or 1)
        else:
            items = self.mdblist_api.get_list_of_lists(path=path, page=page or 1, owner=info == 'mdblist_yourlists')

        self.library = 'video'
        self.plugin_category = get_plugin_category(info_model)
        self.sort_methods = [{'sortMethod': SORT_METHOD_UNSORTED, 'label2Mask': '%U'}]  # Label2 Mask by Studio (i.e. User Name)
        return items


def _get_list_of_lists_user_endpoint(mdblist_api, path, page=1, limit=250):
    """Lists of lists from a user-scoped endpoint (e.g. lists/liked).
    Tries the Bearer-token request first, then the API-key request. The response is an
    envelope {"lists": [...], "pagination": {...}}, so unwrap it before pagination.
    """
    from tmdbhelper.lib.api.mdblist.api import MDbListPaginationLists
    response = _get_user_endpoint(mdblist_api, *path.split('/'))
    if response is None:
        response = mdblist_api.get_request_sc(path, cache_refresh=True if page == 1 else False)
    if isinstance(response, dict):
        response = response.get('lists') or []
    response = MDbListPaginationLists(response, page=page, limit=limit)
    return response.items if not response.next_page else response.items + response.next_page


class ListUserLists(ContainerDirectory):
    """Browse another MDBList user's public lists (GET /lists/user/{username})."""

    def get_items(self, username=None, page=None, **kwargs):
        if not username:
            from xbmcgui import Dialog
            kwargs['username'] = username = Dialog().input(get_localized(32573))
            if not username:
                return
            from tmdbhelper.lib.addon.plugin import encode_url
            self.container_update = f'{encode_url(PLUGINPATH, **kwargs)},replace'
        from urllib.parse import quote
        from xbmcplugin import SORT_METHOD_UNSORTED
        items = self.mdblist_api.get_list_of_lists(
            path=f"lists/user/{quote(username, safe='')}", page=page or 1)
        self.library = 'video'
        self.plugin_category = f'{get_localized(32573)}: {username}'
        self.sort_methods = [{'sortMethod': SORT_METHOD_UNSORTED, 'label2Mask': '%U'}]  # Label2 Mask by Studio (i.e. User Name)
        return items


class ListCustom(ContainerDirectory):
    def get_items(self, list_id, page=None, **kwargs):
        response = self.mdblist_api.get_custom_list(list_id, page=page or 1)

        self.tmdb_cache_only = False
        self.set_mixed_content(response.paginated_items_dict)

        return response.items + response.next_page


class ListCustomSearch(ContainerDirectory):
    def get_items(self, query=None, **kwargs):
        if not query:
            from xbmcgui import Dialog
            kwargs['query'] = query = Dialog().input(get_localized(32044))
            if not kwargs['query']:
                return
            from tmdbhelper.lib.addon.plugin import encode_url
            self.container_update = f'{encode_url(PLUGINPATH, **kwargs)},replace'
        from xbmcplugin import SORT_METHOD_UNSORTED
        items = self.mdblist_api.get_list_of_lists_search(query)
        self.library = 'video'
        self.sort_methods = [{'sortMethod': SORT_METHOD_UNSORTED, 'label2Mask': '%U'}]  # Label2 Mask by Studio (i.e. User Name)
        return items


def _get_user_endpoint(mdblist_api, *path_parts, **params):
    """Fetch a user-specific MDBList endpoint with Bearer token auth.

    All user-data endpoints (/watchlist/items, /sync/watched, /sync/playback,
    /sync/collection, /sync/dropped) require the Bearer token — get_request_sc
    only sends the API key and returns empty.
    Mirrors MDbListDataType.get_response_sync() used by the Phase B sync code.
    """
    try:
        path = mdblist_api.get_request_url(*path_parts, **params)
        response = mdblist_api.get_api_request(path, headers=mdblist_api.auth_headers)
        if response is None:
            return None
        return response.json()
    except (ValueError, AttributeError, Exception):
        return None


def _normalize_collection(response):
    """GET /sync/collection or /sync/dropped → {"movies": [...], "shows": [...]}
    Movies: {"movie": {"title": "...", "year": N, "ids": {"tmdb": N}}, "collected_at": "..."}
    Shows:  {"show": {"title": "...", "year": N, "ids": {"tmdb": N}}, "collected_at"/"dropped_at": "..."}
    Title/year carried through so ItemMappingBasic has a label before TMDb enrichment.
    """
    result = {}

    movies = [
        {'id': (i.get('movie') or {}).get('ids', {}).get('tmdb'),
         'mediatype': 'movie',
         'title': (i.get('movie') or {}).get('title'),
         'release_year': (i.get('movie') or {}).get('year')}
        for i in (response.get('movies') or [])
        if (i.get('movie') or {}).get('ids', {}).get('tmdb')
    ]
    if movies:
        result['movies'] = movies

    shows = [
        {'id': (i.get('show') or {}).get('ids', {}).get('tmdb'),
         'mediatype': 'show',
         'title': (i.get('show') or {}).get('title'),
         'release_year': (i.get('show') or {}).get('year')}
        for i in (response.get('shows') or [])
        if (i.get('show') or {}).get('ids', {}).get('tmdb')
    ]
    if shows:
        result['shows'] = shows

    return result


class ListWatchlist(ContainerDirectory):
    """MDBList watchlist — GET /watchlist/items.
    Returns flat {id, mediatype, title, ...} items — same format as /lists/{id}/items.
    tmdb_type='movie' → movies only; tmdb_type='tv' → shows only; else → both.
    """

    def get_items(self, tmdb_type=None, page=None, **kwargs):
        raw = _get_user_endpoint(self.mdblist_api, 'watchlist', 'items')
        if not raw:
            return
        # Flat list → filter by mediatype if requested, then wrap for paginator
        if isinstance(raw, list):
            if tmdb_type == 'movie':
                raw = [i for i in raw if i.get('mediatype') in ('movie',)]
            elif tmdb_type == 'tv':
                raw = [i for i in raw if i.get('mediatype') in ('show', 'tv')]
            raw = {'items': raw}
        elif not isinstance(raw, dict):
            return
        response = self.mdblist_api.get_paginated(raw, page=page or 1)
        self.tmdb_cache_only = False
        self.set_mixed_content(response.paginated_items_dict)
        self.library = 'video'
        items = response.items + response.next_page
        return items or None


class ListHistory(ContainerDirectory):
    """MDBList watched history (Recently Watched), read from GET /sync/watched.
    tmdb_type='movie' → movies, newest first.
    tmdb_type='tv'    → episodes/season/show marks aggregated to show level, newest first.
    else              → movies and shows interleaved by watch time.
    The server read is uncached: one request per 1,000 entries, every time the list opens.
    """

    def _local_movies(self, limit):
        """[{id, mediatype, last_watched_at}] newest first from MDbListSync.db (the scrobbler and the
        context menu write it). Only the fallback when the server read fails."""
        try:
            rows = self.mdblist_api.mdblist_syncdata.cache.get_list_values(
                keys=('tmdb_id', 'last_watched_at'),
                values=('movie',),
                conditions=(
                    'item_type=? AND last_watched_at IS NOT NULL'
                    ' AND tmdb_id IS NOT NULL'
                    f' ORDER BY last_watched_at DESC LIMIT {int(limit)}'))
        except Exception:
            return []
        return [{'id': r[0], 'mediatype': 'movie', 'last_watched_at': r[1]} for r in (rows or []) if r[0]]

    def get_items(self, tmdb_type=None, page=None, **kwargs):
        # GET /sync/watched is walked to completion: one default page holds 1,000 entries shared across
        # movies/shows/seasons/episodes (the movies of one account sat on page 1 AND page 5), far fewer
        # than a long history. A failed page returns None - never a partial history.
        raw = _get_watched_all(self.mdblist_api)
        normalized = _normalize_watched(raw or {})
        if raw is None:
            # Server unreachable: movies fall back to what this Kodi wrote locally (the old behaviour).
            kodi_log('MDBList history: /sync/watched could not be read - movies come from the local MDbListSync.db', 1)
            local = self._local_movies(20 if tmdb_type == 'movie' else 50)
            if local:
                normalized['movies'] = local
        if tmdb_type == 'movie':
            normalized = {k: v for k, v in normalized.items() if k == 'movies'}
        elif tmdb_type == 'tv':
            normalized = {k: v for k, v in normalized.items() if k == 'shows'}
        else:
            combined = sorted((normalized.get('movies') or []) + (normalized.get('shows') or []),
                              key=lambda x: x.get('last_watched_at') or '', reverse=True)
            normalized = {'all': combined} if combined else {}
        if not normalized:
            return
        response = self.mdblist_api.get_paginated(normalized, page=page or 1)
        self.tmdb_cache_only = False
        self.set_mixed_content(response.paginated_items_dict)
        self.library = 'video'
        return response.items + response.next_page


def _is_anime(details):
    """Anime = Japanese-language animation (the same split the Anime hub's search uses), from a TMDb details dict."""
    return details.get('original_language') == 'ja' and any(g.get('id') == 16 for g in details.get('genres') or [])


def _in_scope(details, scope, tmdb_type):
    """Whether a TMDb details dict belongs to the Anime or Asian Progress menu (scope 'anime' / 'asian'), or to the Movies and TV Shows
    menu (scope 'other': neither anime nor Asian). Asian follows the Asian Cinema & TV hub: Chinese / Cantonese / Korean / Japanese / Thai
    language, movies without Animation or Documentary, TV shows with Drama and without Animation, Reality or Talk."""
    if scope == 'anime':
        return _is_anime(details)
    if scope == 'other':
        return not _is_anime(details) and not _in_scope(details, 'asian', tmdb_type)
    from tmdbhelper.lib.api.asiandrama.lists import SEARCH_LANGUAGES, SEARCH_NOT_ASIAN, SEARCH_NOT_ASIAN_MOVIE
    if details.get('original_language') not in SEARCH_LANGUAGES['asian']:
        return False
    genres = {g.get('id') for g in details.get('genres') or []}
    if tmdb_type == 'movie':
        return not genres & SEARCH_NOT_ASIAN_MOVIE
    return 18 in genres and not genres & SEARCH_NOT_ASIAN


def _get_scope(anime_only=None, asian_only=None, other_only=None):
    return 'anime' if anime_only == 'true' else 'asian' if asian_only == 'true' else 'other' if other_only == 'true' else None


class ListInProgress(ContainerDirectory):
    """MDBList in-progress — GET /sync/playback.  Movies + shows (deduplicated).
    tmdb_type='movie' → movies only; tmdb_type='tv' → shows only; else → both.
    anime_only='true' / asian_only='true' keep only anime / Asian titles (Anime / Asian Progress menus); other_only='true' keeps
    everything else (Movies and TV Shows menu).
    """

    def _keep_scope(self, normalized, scope):
        kept = {}
        for key, rows in normalized.items():
            tmdb_type = 'movie' if key == 'movies' else 'tv'
            rows = [r for r in rows if _in_scope(self.tmdb_api.get_request_sc(tmdb_type, r['id']) or {}, scope, tmdb_type)]
            if rows:
                kept[key] = rows
        return kept

    def get_items(self, tmdb_type=None, page=None, anime_only=None, asian_only=None, other_only=None, **kwargs):
        raw = _get_user_endpoint(self.mdblist_api, 'sync', 'playback')
        normalized = _normalize_playback(raw if isinstance(raw, list) else [])
        if tmdb_type == 'movie':
            normalized = {k: v for k, v in normalized.items() if k == 'movies'}
        elif tmdb_type == 'tv':
            normalized = {k: v for k, v in normalized.items() if k == 'shows'}
        scope = _get_scope(anime_only, asian_only, other_only)
        if normalized and scope:
            normalized = self._keep_scope(normalized, scope)
        if not normalized:
            return
        response = self.mdblist_api.get_paginated(normalized, page=page or 1)
        self.tmdb_cache_only = False
        self.set_mixed_content(response.paginated_items_dict)
        self.library = 'video'
        return response.items + response.next_page


_NEXTUP_RESULT_CAP = 20     # episodes returned
_NEXTUP_SHOW_CAP = 60       # shows examined (each costs one cached TMDb call)
_NEXTUP_SCOPE_CAP = 200   # anime / Asian only: most examined shows are not, so look through more of the history


def _get_watched_all(mdblist_api, page_limit=1000, max_pages=50):
    """Walk GET /sync/watched to completion (movies/shows/seasons/episodes share one
    offset window). Returns None if any page fails or the page budget runs out, so a
    partial read is never mistaken for the whole history.
    """
    combined = {'movies': [], 'shows': [], 'seasons': [], 'episodes': []}
    offset = 0
    for _ in range(max_pages):
        page = _get_user_endpoint(mdblist_api, 'sync', 'watched', offset=offset, limit=page_limit)
        if not isinstance(page, dict):
            return None
        for key in combined:
            combined[key].extend(page.get(key) or [])
        if not (page.get('pagination') or {}).get('has_more'):
            return combined
        offset += page_limit
    return None


def _group_watched_by_show(watched):
    """{show_tmdb_id: {'title', 'last', 'episodes': {(s, n)}, 'bulk_seasons': {s}, 'bulk_show': ts}}.
    'last' is the newest individual-episode watch time (Seren orders by this). Shows with
    only a bulk mark and no individual episode are not listed (Seren parity).
    """
    shows = {}

    def _show(tmdb_id):
        return shows.setdefault(tmdb_id, {
            'title': None, 'last': '', 'episodes': set(), 'bulk_seasons': set(), 'bulk_show': None})

    for item in watched.get('episodes') or []:
        ep = item.get('episode') or {}
        show = ep.get('show') or {}
        tmdb_id = (show.get('ids') or {}).get('tmdb')
        try:
            season, number = int(ep.get('season')), int(ep.get('number'))
        except (TypeError, ValueError):
            continue
        if not tmdb_id:
            continue
        entry = _show(tmdb_id)
        entry['title'] = entry['title'] or show.get('title')
        entry['episodes'].add((season, number))
        entry['last'] = max(entry['last'], item.get('last_watched_at') or '')
    for item in watched.get('seasons') or []:
        season = item.get('season') or {}
        tmdb_id = ((season.get('show') or {}).get('ids') or {}).get('tmdb')
        try:
            season_number = int(season.get('number'))
        except (TypeError, ValueError):
            continue
        if tmdb_id:
            _show(tmdb_id)['bulk_seasons'].add(season_number)
    for item in watched.get('shows') or []:
        tmdb_id = ((item.get('show') or {}).get('ids') or {}).get('tmdb')
        if tmdb_id:
            _show(tmdb_id)['bulk_show'] = item.get('last_watched_at') or ''
    return shows


def _pick_next_episode(seasons, last_aired, watched, bulk_seasons=(), bulk_show=None, get_air_dates=None):
    """First aired, non-special episode that is not watched, in season/number order.

    seasons       [(season_number, episode_count, premiere_date_or_None)] from TMDb show details
    last_aired    (season, episode, air_date) of TMDb's last_episode_to_air, or None
    watched       {(season, number)} individually watched episodes
    bulk_seasons  season numbers marked watched as a whole on MDBList
    bulk_show     last_watched_at of a whole-show mark, or None
    get_air_dates callable(season) -> {episode_number: air_date}; only called for the
                  last aired season, when a whole-show mark has to be dated per episode

    A season mark credits the whole season. A whole-show mark credits what had aired by the
    mark's date: everything aired if the mark is at/after the last aired episode, otherwise
    earlier seasons that premiered by then plus, for the last season, episodes aired by then.
    Returns (season, episode) or None.
    """
    if not last_aired:
        return None
    last_season, last_episode, last_air_date = last_aired
    mark = (bulk_show or '')[:10] or None
    covers_all = bool(mark and last_air_date and mark >= last_air_date[:10])
    last_season_dates = None

    def _credited(season, number, premiere):
        nonlocal last_season_dates
        if season in bulk_seasons:
            return True
        if not mark:
            return False
        if covers_all:
            return True
        if season < last_season:
            return bool(premiere and premiere[:10] <= mark)
        if last_season_dates is None:
            last_season_dates = (get_air_dates(season) if get_air_dates else None) or {}
        air_date = last_season_dates.get(number)
        return bool(air_date and air_date[:10] <= mark)

    for season, count, premiere in sorted(s for s in seasons if s[0]):
        for number in range(1, (count or 0) + 1):
            if (season, number) > (last_season, last_episode):
                return None
            if (season, number) in watched or _credited(season, number, premiere):
                continue
            return season, number
    return None


class ListNextUp(ContainerDirectory):
    """MDBList Next Up: one row per recently watched show (per MDBList's own history), each
    showing that show's first unwatched aired episode. Mirrors Seren's mdblistNextUp.
    Reads the full watched history live (including whole-show / whole-season marks); if any
    page fails the list is empty rather than built from partial history.
    Episodes are matched on TMDb numbering (Seren used Trakt's, which can differ for anime).
    """

    def _get_air_dates_func(self, tmdb_id):
        def _get_air_dates(season):
            response = self.tmdb_api.get_request_sc(f'tv/{tmdb_id}/season/{season}') or {}
            return {e.get('episode_number'): e.get('air_date') for e in response.get('episodes') or []}
        return _get_air_dates

    def get_items(self, tmdb_type=None, page=None, anime_only=None, asian_only=None, other_only=None, **kwargs):
        scope = _get_scope(anime_only, asian_only, other_only)
        watched = _get_watched_all(self.mdblist_api)
        if not watched:
            return
        by_show = _group_watched_by_show(watched)
        recent = sorted(
            ((k, v) for k, v in by_show.items() if v['episodes']),
            key=lambda kv: kv[1]['last'], reverse=True)[:_NEXTUP_SCOPE_CAP if scope else _NEXTUP_SHOW_CAP]

        episodes = []
        for tmdb_id, show in recent:
            if len(episodes) >= _NEXTUP_RESULT_CAP:
                break
            details = self.tmdb_api.get_request_sc('tv', tmdb_id) or {}
            if scope and not _in_scope(details, scope, 'tv'):
                continue
            last = details.get('last_episode_to_air') or {}
            last_aired = None
            if last.get('season_number') is not None and last.get('episode_number') is not None:
                last_aired = (last['season_number'], last['episode_number'], last.get('air_date') or '')
            seasons = [
                (s.get('season_number'), s.get('episode_count'), s.get('air_date'))
                for s in details.get('seasons') or [] if s.get('season_number') is not None]
            nxt = _pick_next_episode(
                seasons, last_aired, show['episodes'], show['bulk_seasons'], show['bulk_show'],
                self._get_air_dates_func(tmdb_id))
            if nxt:
                episodes.append({
                    'id': tmdb_id, 'mediatype': 'episode', 'season': nxt[0], 'episode': nxt[1],
                    'title': show['title'] or details.get('name')})
        if not episodes:
            return

        response = self.mdblist_api.get_paginated({'episodes': episodes}, page=page or 1)
        self.tmdb_cache_only = False
        self.set_mixed_content(response.paginated_items_dict)
        self.library = 'video'
        self.plugin_category = get_localized(32572)
        return response.items + response.next_page


class ListCollection(ContainerDirectory):
    """MDBList collection — GET /sync/collection.
    tmdb_type='movie' → movies only; tmdb_type='tv' → shows only; else → both.
    Single generous fetch (limit=5000, MDBList's documented max) — this is a
    browse menu, not the bridge-sync diff path, so exhaustive offset/limit
    walking isn't needed the way it is for sync correctness (see bridge_sync.py
    and mdblist/sync/datatype.py, which do walk to completion).
    """

    def get_items(self, tmdb_type=None, page=None, **kwargs):
        raw = _get_user_endpoint(self.mdblist_api, 'sync', 'collection', limit=5000)
        normalized = _normalize_collection(raw or {})
        if tmdb_type == 'movie':
            normalized = {k: v for k, v in normalized.items() if k == 'movies'}
        elif tmdb_type == 'tv':
            normalized = {k: v for k, v in normalized.items() if k == 'shows'}
        if not normalized:
            return
        response = self.mdblist_api.get_paginated(normalized, page=page or 1)
        self.tmdb_cache_only = False
        self.set_mixed_content(response.paginated_items_dict)
        self.library = 'video'
        return response.items + response.next_page


class ListDroppedShows(ContainerDirectory):
    """MDBList dropped shows — GET /sync/dropped. Shows only — MDBList has no
    dropped-movies equivalent (confirmed via API spec: "Dropped Shows... Rules:
    Shows only"). Single generous fetch (limit=5000) — browse menu, not the
    bridge-sync diff path.
    """

    def get_items(self, tmdb_type=None, page=None, **kwargs):
        raw = _get_user_endpoint(self.mdblist_api, 'sync', 'dropped', limit=5000)
        normalized = _normalize_collection(raw or {})
        if not normalized:
            return
        response = self.mdblist_api.get_paginated(normalized, page=page or 1)
        self.tmdb_cache_only = False
        self.set_mixed_content(response.paginated_items_dict)
        self.library = 'video'
        from tmdbhelper.lib.script.sync.dropped import with_header, HEADER_MDBLIST_RESTORE
        return with_header(response.items, HEADER_MDBLIST_RESTORE, self.is_widget, page) + response.next_page
