from tmdbhelper.lib.items.container import ContainerDirectory
from tmdbhelper.lib.addon.plugin import get_plugin_category, get_localized, PLUGINPATH
from tmdbhelper.lib.addon.consts import MDBLIST_LIST_OF_LISTS


# ---------------------------------------------------------------------------
# Response normalizers — convert API shapes to flat {id, mediatype} dicts
# that ItemListPagination / ItemMappingBasic expect.
# ---------------------------------------------------------------------------

def _normalize_watched(response):
    """GET /sync/watched → {"movies": [...], "shows": [], "episodes": [...]}
    Movies: {"movie": {"title": "...", "year": N, "ids": {"tmdb": N}}, "last_watched_at": "..."}
    Episodes: {"episode": {"show": {"title": "...", "year": N, "ids": {"tmdb": N}}}, "last_watched_at": "..."}
    Show tmdb_id is at item['episode']['show']['ids']['tmdb']. Title/year carried through so
    ItemMappingBasic has a label before TMDb enrichment.
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

    # Aggregate episodes by show — keep latest ts and show metadata
    show_data = {}
    for item in (response.get('episodes') or []):
        ep = item.get('episode') or {}
        show = ep.get('show') or {}
        tmdb_id = show.get('ids', {}).get('tmdb')
        if not tmdb_id:
            continue
        ts = item.get('last_watched_at') or ''
        if tmdb_id not in show_data or ts > show_data[tmdb_id][0]:
            show_data[tmdb_id] = (ts, show)

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

        items = self.mdblist_api.get_list_of_lists(
            path=info_model.get('path', '').format(**kwargs),
            page=page or 1)

        self.library = 'video'
        self.plugin_category = get_plugin_category(info_model)
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


def _get_user_endpoint(mdblist_api, *path_parts):
    """Fetch a user-specific MDBList endpoint with Bearer token auth.

    All three user-data endpoints (/watchlist/items, /sync/watched, /sync/playback)
    require the Bearer token — get_request_sc only sends the API key and returns empty.
    Mirrors MDbListDataType.get_response_sync() used by the Phase B sync code.
    """
    try:
        path = mdblist_api.get_request_url(*path_parts)
        response = mdblist_api.get_api_request(path, headers=mdblist_api.auth_headers)
        if response is None:
            return None
        return response.json()
    except (ValueError, AttributeError, Exception):
        return None


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
    """MDBList watched history.
    tmdb_type='movie' → reads TraktSync_v2.db (MDBList /sync/watched is episode-only;
                         movies are never returned regardless of pagination depth).
    tmdb_type='tv'    → GET /sync/watched episodes, aggregated to show level.
    else              → both (shows only in practice, same API limitation).
    """

    def get_items(self, tmdb_type=None, page=None, **kwargs):
        if tmdb_type == 'movie':
            # MDBList's GET /sync/watched never returns movies (confirmed: 1,500 items probed,
            # 0 movies found).  Read from MDbListSync.db instead — populated by the scrobbler
            # and context menu writes (both do INSERT OR IGNORE + UPDATE with last_watched_at).
            try:
                cache = self.mdblist_api.mdblist_syncdata.cache
                rows = cache.get_list_values(
                    keys=('tmdb_id',),
                    values=('movie',),
                    conditions=(
                        'item_type=? AND last_watched_at IS NOT NULL'
                        ' AND tmdb_id IS NOT NULL'
                        ' ORDER BY last_watched_at DESC LIMIT 20'))
            except Exception:
                rows = None
            if not rows:
                return
            movies = [
                {'id': r[0], 'mediatype': 'movie'}
                for r in rows if r[0]
            ]
            if not movies:
                return
            normalized = {'movies': movies}
        else:
            raw = _get_user_endpoint(self.mdblist_api, 'sync', 'watched')
            normalized = _normalize_watched(raw or {})
            if tmdb_type != 'tv':
                # Combined view: add movies from MDbListSync.db (GET /sync/watched never returns movies)
                try:
                    mdb_rows = self.mdblist_api.mdblist_syncdata.cache.get_list_values(
                        keys=('tmdb_id', 'last_watched_at'),
                        values=('movie',),
                        conditions=(
                            'item_type=? AND last_watched_at IS NOT NULL'
                            ' AND tmdb_id IS NOT NULL'
                            ' ORDER BY last_watched_at DESC LIMIT 50'))
                    movies = [
                        {'id': r[0], 'mediatype': 'movie', 'last_watched_at': r[1]}
                        for r in (mdb_rows or []) if r[0]
                    ]
                except Exception:
                    movies = []
                shows = normalized.get('shows') or []
                combined = sorted(movies + shows,
                                  key=lambda x: x.get('last_watched_at') or '',
                                  reverse=True)
                normalized = {'all': combined} if combined else {}
            else:
                if not normalized:
                    return
                normalized = {k: v for k, v in normalized.items() if k == 'shows'}
            if not normalized:
                return
        response = self.mdblist_api.get_paginated(normalized, page=page or 1)
        self.tmdb_cache_only = False
        self.set_mixed_content(response.paginated_items_dict)
        self.library = 'video'
        return response.items + response.next_page


class ListInProgress(ContainerDirectory):
    """MDBList in-progress — GET /sync/playback.  Movies + shows (deduplicated).
    tmdb_type='movie' → movies only; tmdb_type='tv' → shows only; else → both.
    """

    def get_items(self, tmdb_type=None, page=None, **kwargs):
        raw = _get_user_endpoint(self.mdblist_api, 'sync', 'playback')
        normalized = _normalize_playback(raw if isinstance(raw, list) else [])
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
