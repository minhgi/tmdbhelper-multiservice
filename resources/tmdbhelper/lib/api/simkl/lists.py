from tmdbhelper.lib.items.container import ContainerDirectory

BASE_URL = 'https://api.simkl.com'


def _simkl_get(simkl_api, path):
    url = f'{BASE_URL}{path}?client_id={simkl_api.client_id}'
    response = simkl_api.get_api_request(url, headers=simkl_api.auth_headers)
    if response is None:
        return None
    try:
        return response.json()
    except Exception:
        return None


def _normalize_plantowatch(movies_raw, shows_raw):
    result = {}
    movies = [
        {'id': int(ids['tmdb']), 'mediatype': 'movie',
         'title': (i.get('movie') or {}).get('title'),
         'release_year': (i.get('movie') or {}).get('year')}
        for i in (movies_raw or [])
        if (ids := (i.get('movie') or {}).get('ids', {})) and ids.get('tmdb')
    ]
    if movies:
        result['movies'] = movies
    shows = [
        {'id': int(ids['tmdb']), 'mediatype': 'show',
         'title': (i.get('show') or {}).get('title'),
         'release_year': (i.get('show') or {}).get('year')}
        for i in (shows_raw or [])
        if (ids := (i.get('show') or {}).get('ids', {})) and ids.get('tmdb')
    ]
    if shows:
        result['shows'] = shows
    return result


def _normalize_history(movies_raw, shows_raw):
    result = {}
    movies = sorted(
        [{'id': int(ids['tmdb']), 'mediatype': 'movie', '_ts': i.get('last_watched_at') or '',
          'title': (i.get('movie') or {}).get('title'),
          'release_year': (i.get('movie') or {}).get('year')}
         for i in (movies_raw or [])
         if (ids := (i.get('movie') or {}).get('ids', {})) and ids.get('tmdb')],
        key=lambda x: x['_ts'], reverse=True)
    if movies:
        result['movies'] = [{k: v for k, v in m.items() if k != '_ts'} for m in movies]
    shows = sorted(
        [{'id': int(ids['tmdb']), 'mediatype': 'show', '_ts': i.get('last_watched_at') or '',
          'title': (i.get('show') or {}).get('title'),
          'release_year': (i.get('show') or {}).get('year')}
         for i in (shows_raw or [])
         if (ids := (i.get('show') or {}).get('ids', {})) and ids.get('tmdb')],
        key=lambda x: x['_ts'], reverse=True)
    if shows:
        result['shows'] = [{k: v for k, v in s.items() if k != '_ts'} for s in shows]
    return result


def _normalize_inprogress(items_list):
    result = {}
    seen_shows = set()
    for item in sorted(items_list or [], key=lambda x: x.get('paused_at') or '', reverse=True):
        item_type = item.get('type')
        if item_type == 'movie':
            movie = item.get('movie') or {}
            tmdb_id = movie.get('ids', {}).get('tmdb')
            if tmdb_id:
                result.setdefault('movies', []).append({
                    'id': int(tmdb_id), 'mediatype': 'movie',
                    'title': movie.get('title'), 'release_year': movie.get('year')})
        elif item_type == 'episode':
            show = item.get('show') or {}
            tmdb_id = show.get('ids', {}).get('tmdb')
            if tmdb_id and tmdb_id not in seen_shows:
                seen_shows.add(tmdb_id)
                result.setdefault('shows', []).append({
                    'id': int(tmdb_id), 'mediatype': 'show',
                    'title': show.get('title'), 'release_year': show.get('year')})
    return result


class ListPlanToWatch(ContainerDirectory):
    """Simkl plan-to-watch — GET /sync/all-items/movies/plantowatch + /sync/all-items/shows/plantowatch."""

    def get_items(self, tmdb_type=None, page=None, **kwargs):
        movies_raw = shows_raw = None
        if tmdb_type != 'tv':
            r = _simkl_get(self.simkl_api, '/sync/all-items/movies/plantowatch')
            movies_raw = (r or {}).get('movies')
        if tmdb_type != 'movie':
            r = _simkl_get(self.simkl_api, '/sync/all-items/shows/plantowatch')
            shows_raw = (r or {}).get('shows')
        normalized = _normalize_plantowatch(movies_raw, shows_raw)
        if not normalized:
            return
        response = self.simkl_api.get_paginated(normalized, page=page or 1)
        self.tmdb_cache_only = False
        self.set_mixed_content(response.paginated_items_dict)
        self.library = 'video'
        return response.items + response.next_page


class ListHistory(ContainerDirectory):
    """Simkl completed history — GET /sync/all-items/movies/completed + /sync/all-items/shows/completed.
    Sorted most-recent-first by last_watched_at.
    tmdb_type='movie' → movies only; tmdb_type='tv' → shows only; else → both.
    """

    def get_items(self, tmdb_type=None, page=None, **kwargs):
        movies_raw = shows_raw = None
        if tmdb_type != 'tv':
            r = _simkl_get(self.simkl_api, '/sync/all-items/movies/completed')
            movies_raw = (r or {}).get('movies')
        if tmdb_type != 'movie':
            r = _simkl_get(self.simkl_api, '/sync/all-items/shows/completed')
            shows_raw = (r or {}).get('shows')
        normalized = _normalize_history(movies_raw, shows_raw)
        if not normalized:
            return
        if tmdb_type == 'movie':
            normalized = {k: v for k, v in normalized.items() if k == 'movies'}
        elif tmdb_type == 'tv':
            normalized = {k: v for k, v in normalized.items() if k == 'shows'}
        if not normalized:
            return
        response = self.simkl_api.get_paginated(normalized, page=page or 1)
        self.tmdb_cache_only = False
        self.set_mixed_content(response.paginated_items_dict)
        self.library = 'video'
        return response.items + response.next_page


class ListInProgress(ContainerDirectory):
    """Simkl in-progress — GET /sync/playback.
    Movies + shows (episode-level deduplicated to show-level). Sorted most-recent-first by paused_at.
    """

    def get_items(self, tmdb_type=None, page=None, **kwargs):
        raw = _simkl_get(self.simkl_api, '/sync/playback')
        normalized = _normalize_inprogress(raw if isinstance(raw, list) else [])
        if tmdb_type == 'movie':
            normalized = {k: v for k, v in normalized.items() if k == 'movies'}
        elif tmdb_type == 'tv':
            normalized = {k: v for k, v in normalized.items() if k == 'shows'}
        if not normalized:
            return
        response = self.simkl_api.get_paginated(normalized, page=page or 1)
        self.tmdb_cache_only = False
        self.set_mixed_content(response.paginated_items_dict)
        self.library = 'video'
        return response.items + response.next_page
