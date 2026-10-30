import re
from tmdbhelper.lib.items.container import ContainerDirectory
from tmdbhelper.lib.addon.plugin import get_localized
from tmdbhelper.lib.addon.logger import kodi_log

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
         'release_year': (i.get('movie') or {}).get('year'),
         'added_at': i.get('added_to_watchlist_at')}
        for i in (movies_raw or [])
        if (ids := (i.get('movie') or {}).get('ids', {})) and ids.get('tmdb')
    ]
    if movies:
        result['movies'] = movies
    shows = [
        {'id': int(ids['tmdb']), 'mediatype': 'show',
         'title': (i.get('show') or {}).get('title'),
         'release_year': (i.get('show') or {}).get('year'),
         'added_at': i.get('added_to_watchlist_at')}
        for i in (shows_raw or [])
        if (ids := (i.get('show') or {}).get('ids', {})) and ids.get('tmdb')
    ]
    if shows:
        result['shows'] = shows
    return result


def _resolve_anime(tmdb_api, anime_raw):
    """anime_raw with a TMDb id filled in for every entry Simkl has none for (its anime records often carry only MAL /
    AniList / AniDB / Kitsu ids), found by title the way the Anime menus match AniList titles: the Japanese animation
    TMDb returns for that title. Entries that still cannot be matched stay as they were (and are skipped later)."""
    if not anime_raw or tmdb_api is None:
        return anime_raw
    todo = []
    for n, i in enumerate(anime_raw):
        entry = (i or {}).get('show') or (i or {}).get('movie') or {}
        try:
            int((entry.get('ids') or {}).get('tmdb'))
        except (ValueError, TypeError):
            if entry.get('title'):
                todo.append(n)
    if not todo:
        return anime_raw

    def _match(n):
        i = anime_raw[n]
        title = ((i.get('show') or i.get('movie')) or {}).get('title')
        from tmdbhelper.lib.api.anilist.lists import _match_tmdb
        try:
            return _match_tmdb(tmdb_api, {'format': 'MOVIE' if i.get('anime_type') == 'movie' else 'TV',
                                          'title': {'english': title, 'romaji': title}})
        except Exception:
            return None

    from tmdbhelper.lib.addon.thread import ParallelThread
    with ParallelThread(todo, _match) as pt:
        matches = pt.queue
    out = list(anime_raw)
    for n, match in zip(todo, matches):
        if not match:
            continue
        i = dict(out[n])
        key = 'show' if i.get('show') else 'movie'
        i[key] = dict(i[key], ids=dict(i[key].get('ids') or {}, tmdb=match[1]))
        out[n] = i
    return out


def _normalize_anime(anime_raw, with_added=False):
    """Anime rows -> (movies, shows) in the _normalize_plantowatch shape.

    Simkl files every anime in its own bucket and keeps the ids under entry['show'] - an anime film
    included, told apart only by the top-level anime_type == 'movie' (live-probed 2026-10-14). Rows with no
    tmdb id cannot be opened in TMDb Helper and are skipped.
    """
    movies, shows = [], []
    for i in anime_raw or []:
        show = (i or {}).get('show') or (i or {}).get('movie') or {}
        tmdb_id = (show.get('ids') or {}).get('tmdb')
        try:
            tmdb_id = int(tmdb_id)
        except (ValueError, TypeError):
            continue
        is_movie = i.get('anime_type') == 'movie'
        row = {
            'id': tmdb_id, 'mediatype': 'movie' if is_movie else 'show',
            'title': show.get('title'), 'release_year': show.get('year')}
        if with_added:  # Plan to Watch only: its regular rows carry the date too, so the anime rows are not left at the end of Date added
            row['added_at'] = i.get('added_to_watchlist_at')
        (movies if is_movie else shows).append(row)
    return movies, shows


def _merge_anime(normalized, anime_raw, tmdb_type=None, with_added=False):
    """Append the anime rows of one status to a _normalize_plantowatch() result (in place, and returned).

    tmdb_type 'tv' takes the anime shows only and 'movie' the anime films only. A tmdb id already listed
    (as a regular row or by an earlier anime row) is not added twice.
    """
    anime_movies, anime_shows = _normalize_anime(anime_raw, with_added)
    for key, extra in (('movies', anime_movies if tmdb_type != 'tv' else []),
                       ('shows', anime_shows if tmdb_type != 'movie' else [])):
        rows = normalized.get(key, [])
        seen = {i['id'] for i in rows}
        for i in extra:
            if i['id'] not in seen:
                seen.add(i['id'])
                rows.append(i)
        if rows:
            normalized[key] = rows
    return normalized


def _first_per_id(rows):
    """Keep the first row of each tmdb id (rows are sorted newest first, so the newest survives)."""
    seen = set()
    out = []
    for r in rows:
        if r['id'] not in seen:
            seen.add(r['id'])
            out.append(r)
    return out


def _anime_as_raw(anime_raw):
    """Anime rows -> (movies_raw, shows_raw) in the plain movies / shows entry shape (ids sit under 'show')."""
    movies_raw, shows_raw = [], []
    for i in anime_raw or []:
        entry = (i or {}).get('show') or (i or {}).get('movie')
        if not entry:
            continue
        try:
            int((entry.get('ids') or {}).get('tmdb'))
        except (ValueError, TypeError):
            continue   # no usable tmdb id (the movie / show normalizers need an int)
        if i.get('anime_type') == 'movie':
            movies_raw.append({'movie': entry, 'last_watched_at': i.get('last_watched_at')})
        else:
            shows_raw.append({'show': entry, 'last_watched_at': i.get('last_watched_at')})
    return movies_raw, shows_raw


def _normalize_history(movies_raw, shows_raw, anime_raw=None):
    """Completed rows, newest watched first. anime_raw (Simkl's separate anime bucket) is folded into the
    movies / shows by last_watched_at; a tmdb id already listed is not repeated."""
    if anime_raw:
        anime_movies, anime_shows = _anime_as_raw(anime_raw)
        movies_raw = list(movies_raw or []) + anime_movies
        shows_raw = list(shows_raw or []) + anime_shows
    result = {}
    movies = sorted(
        [{'id': int(ids['tmdb']), 'mediatype': 'movie', '_ts': i.get('last_watched_at') or '',
          'title': (i.get('movie') or {}).get('title'),
          'release_year': (i.get('movie') or {}).get('year')}
         for i in (movies_raw or [])
         if (ids := (i.get('movie') or {}).get('ids', {})) and ids.get('tmdb')],
        key=lambda x: x['_ts'], reverse=True)
    movies = _first_per_id(movies)
    if movies:
        result['movies'] = [{k: v for k, v in m.items() if k != '_ts'} for m in movies]
    shows = sorted(
        [{'id': int(ids['tmdb']), 'mediatype': 'show', '_ts': i.get('last_watched_at') or '',
          'title': (i.get('show') or {}).get('title'),
          'release_year': (i.get('show') or {}).get('year')}
         for i in (shows_raw or [])
         if (ids := (i.get('show') or {}).get('ids', {})) and ids.get('tmdb')],
        key=lambda x: x['_ts'], reverse=True)
    shows = _first_per_id(shows)
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


_NEXT_TO_WATCH = re.compile(r'^S(\d+)E(\d+)$')


_ANIME_NEXT = re.compile(r'^E(\d+)$')


def _int(value):
    try:
        return int(value)
    except (ValueError, TypeError):
        return None


def _normalize_nextup(shows_raw, anime_raw=None):
    """GET /sync/all-items/shows/watching -> [{id, title, season, episode}] newest watched show first.

    next_to_watch is Simkl's own "S01E04" text; it is null once a show is caught up, and it is skipped
    when missing or malformed.

    anime_raw (GET /sync/all-items/anime/watching) is merged in by last_watched_at. Simkl writes an anime's
    next episode as a bare "E12" counted inside that cour (a cour is always its season 1), so those rows come
    back with season None, the cour's episode total and its AniList id; get_items maps them onto a TMDb id,
    season and episode or drops them. A row needs a TMDb id or an AniList id on Simkl (id is None without the
    former). Anime films are left out.
    """
    rows = []
    for item in anime_raw or []:
        if (item or {}).get('anime_type') == 'movie':
            continue
        show = item.get('show') or {}
        ids = show.get('ids') or {}
        match = _ANIME_NEXT.match(str(item.get('next_to_watch') or ''))
        tmdb_id, anilist_id, total = _int(ids.get('tmdb')), _int(ids.get('anilist')), _int(item.get('total_episodes_count')) or 0
        if not match or total <= 0 or (tmdb_id is None and anilist_id is None):
            continue
        rows.append({
            '_ts': item.get('last_watched_at') or '', 'id': tmdb_id, 'anilist': anilist_id, 'title': show.get('title'),
            'season': None, 'episode': int(match.group(1)), 'total': total})
    for item in shows_raw or []:
        show = item.get('show') or {}
        tmdb_id = (show.get('ids') or {}).get('tmdb')
        match = _NEXT_TO_WATCH.match(str(item.get('next_to_watch') or ''))
        if not tmdb_id or not match:
            continue
        try:
            tmdb_id = int(tmdb_id)
        except (ValueError, TypeError):
            continue
        rows.append({
            '_ts': item.get('last_watched_at') or '', 'id': tmdb_id, 'title': show.get('title'),
            'season': int(match.group(1)), 'episode': int(match.group(2))})
    rows.sort(key=lambda r: r['_ts'], reverse=True)
    return [{k: v for k, v in r.items() if k != '_ts'} for r in rows]


def _anime_tmdb_season(details, total):
    """TMDb season number for a Simkl anime cour of `total` episodes, or None when it cannot be told safely.

    Simkl numbers every cour from episode 1 of season 1, while TMDb files the same show under the franchise id
    with its own seasons. Only a season whose episode_count equals the cour's total is trusted, and only when
    exactly one season does - otherwise the episode could be a different one on TMDb, so the row is left out.
    """
    seasons = [s.get('season_number') for s in details.get('seasons') or []
               if (s.get('season_number') or 0) > 0 and s.get('episode_count') == total]
    return seasons[0] if len(seasons) == 1 else None


def _anime_next_targets(tmdb_api, rows, use_anilist=True):
    """For each anime Next Up row (season None) the (tmdb id, season, episode, how) to show, or None to leave it out.

    The TMDb id is Simkl's, or the one the AniList id table finds for the row's AniList id (api/anilist/idmap.py); when the
    two disagree the one whose TMDb episode list has the cour starting on its AniList start date wins (a TMDb language or
    genre check cannot tell, Simkl's wrong ids are Japanese animation too). The season is then the one season of the cour's
    length (_anime_tmdb_season, first choice) or else the cour's first episode is found by its AniList start date against
    TMDb's air dates (api/anilist/cours.py), which also covers cours inside a longer TMDb season. Without AniList data the
    behaviour is as before: Simkl's id and the season rule only (use_anilist=False asks for exactly that)."""
    from tmdbhelper.lib.api.anilist import cours
    from tmdbhelper.lib.api.anilist.idmap import resolve
    from tmdbhelper.lib.api.anilist.lists import _match_tmdb
    from tmdbhelper.lib.addon.thread import ParallelThread

    media = cours.fetch_media([r.get('anilist') for r in rows]) if use_anilist else {}
    with_media = [r for r in rows if r.get('anilist') in media]
    table = {}
    if with_media:
        found = resolve(tmdb_api, [media[r['anilist']] for r in with_media], lambda m: _match_tmdb(tmdb_api, m))
        table = {r['anilist']: match[1] for r, match in zip(with_media, found) if match and match[0] == 'show'}

    def _anchor(tmdb_id, details, start, n):
        found = cours.anchor_episode(cours.tmdb_episodes(tmdb_api, details), start, n) if start else None
        return (tmdb_id, found[0], found[1], 'air date') if found else None

    def _one(row):
        try:
            start = cours.start_date(media.get(row.get('anilist')))
            simkl_id, table_id, n = row.get('id'), table.get(row.get('anilist')), row['episode']
            tried_anchor = False
            if simkl_id and table_id and simkl_id != table_id and start:
                for tmdb_id in (table_id, simkl_id):
                    target = _anchor(tmdb_id, tmdb_api.get_request_sc('tv', tmdb_id) or {}, start, n)
                    if target:
                        return target
                tmdb_id, tried_anchor = simkl_id, True   # neither id starts on the date: Simkl's id and the season rule, as before
            else:
                tmdb_id = simkl_id or table_id
            if not tmdb_id:
                return None
            details = tmdb_api.get_request_sc('tv', tmdb_id) or {}
            season = _anime_tmdb_season(details, row.get('total'))
            if season is not None:
                return tmdb_id, season, n, 'season length'
            return None if tried_anchor else _anchor(tmdb_id, details, start, n)
        except Exception as exc:
            kodi_log(f'Simkl Next Up: anime row {row.get("title")!r} failed: {type(exc).__name__}: {exc}', 1)
            return None

    with ParallelThread(rows, _one) as pt:
        return pt.queue


def _exists_in_tmdb(details, season, episode):
    """True when TMDb's own numbering has this aired episode (Simkl numbers episodes the TVDB way).

    details is TMDb's tv/{id} response: the season must exist, the number must not exceed that season's
    episode_count, and the episode must not come after last_episode_to_air.
    """
    for s in details.get('seasons') or []:
        if s.get('season_number') == season:
            if not 0 < episode <= (s.get('episode_count') or 0):
                return False
            break
    else:
        return False
    last = details.get('last_episode_to_air') or {}
    if last.get('season_number') is not None and last.get('episode_number') is not None:
        return (season, episode) <= (last['season_number'], last['episode_number'])
    return False


class ListPlanToWatch(ContainerDirectory):
    """Simkl plan-to-watch — GET /sync/all-items/movies/plantowatch + /sync/all-items/shows/plantowatch
    + /sync/all-items/anime/plantowatch (Simkl's anime bucket)."""

    def get_items(self, tmdb_type=None, page=None, **kwargs):
        movies_raw = shows_raw = None
        if tmdb_type != 'tv':
            r = _simkl_get(self.simkl_api, '/sync/all-items/movies/plantowatch')
            movies_raw = (r or {}).get('movies')
        if tmdb_type != 'movie':
            r = _simkl_get(self.simkl_api, '/sync/all-items/shows/plantowatch')
            shows_raw = (r or {}).get('shows')
        normalized = _normalize_plantowatch(movies_raw, shows_raw)
        r = _simkl_get(self.simkl_api, '/sync/all-items/anime/plantowatch')
        _merge_anime(normalized, _resolve_anime(self.tmdb_api, (r or {}).get('anime')), tmdb_type, with_added=True)
        if not normalized:
            return
        response = self.simkl_api.get_paginated(normalized, page=page or 1)
        self.tmdb_cache_only = False
        self.set_mixed_content(response.paginated_items_dict)
        self.library = 'video'
        return response.items + response.next_page


class ListHistory(ContainerDirectory):
    """Simkl completed history — GET /sync/all-items/movies/completed + /sync/all-items/shows/completed
    + /sync/all-items/anime/completed (Simkl's anime bucket, folded in by last_watched_at).
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
        r = _simkl_get(self.simkl_api, '/sync/all-items/anime/completed')
        normalized = _normalize_history(movies_raw, shows_raw, _resolve_anime(self.tmdb_api, (r or {}).get('anime')))
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


class ListDropped(ContainerDirectory):
    """Simkl dropped — GET /sync/all-items/{movies|shows|anime}/notinteresting.

    The word for "dropped" is per login (live-probed 2026-10-14): TMDb Helper's token files it under
    'notinteresting'; asking its token for '/dropped' is not an error but returns the WHOLE library
    (every status), so this list used to show every movie and show. Rows are also checked by status.
    """

    DROPPED = ('notinteresting', 'dropped')

    @classmethod
    def _only_dropped(cls, rows):
        return [i for i in rows or [] if (i or {}).get('status') in cls.DROPPED]

    def get_items(self, tmdb_type=None, page=None, **kwargs):
        movies_raw = shows_raw = None
        if tmdb_type != 'tv':
            r = _simkl_get(self.simkl_api, '/sync/all-items/movies/notinteresting')
            movies_raw = self._only_dropped((r or {}).get('movies'))
        if tmdb_type != 'movie':
            r = _simkl_get(self.simkl_api, '/sync/all-items/shows/notinteresting')
            shows_raw = self._only_dropped((r or {}).get('shows'))
        # Anime has a bucket of its own; an anime film counts as a movie, anything else as a show.
        r = _simkl_get(self.simkl_api, '/sync/all-items/anime/notinteresting')
        anime_raw = _resolve_anime(self.tmdb_api, self._only_dropped((r or {}).get('anime')))
        # _normalize_plantowatch is a generic id/title/year extractor with no
        # plantowatch-specific behaviour — reused here for the dropped bucket.
        normalized = _normalize_plantowatch(movies_raw, shows_raw)
        _merge_anime(normalized, anime_raw, tmdb_type)
        if not normalized:
            return
        response = self.simkl_api.get_paginated(normalized, page=page or 1)
        self.tmdb_cache_only = False
        self.set_mixed_content(response.paginated_items_dict)
        self.library = 'video'
        return response.items + response.next_page


class ListWatching(ContainerDirectory):
    """Simkl watching — GET /sync/all-items/shows/watching + /sync/all-items/anime/watching.
    Shows only — Simkl movies have no 'watching' status (binary watched/not); an anime film is left out.
    """

    def get_items(self, tmdb_type=None, page=None, **kwargs):
        r = _simkl_get(self.simkl_api, '/sync/all-items/shows/watching')
        shows_raw = (r or {}).get('shows')
        normalized = _normalize_plantowatch(None, shows_raw)
        r = _simkl_get(self.simkl_api, '/sync/all-items/anime/watching')
        _merge_anime(normalized, _resolve_anime(self.tmdb_api, (r or {}).get('anime')), 'tv')
        if not normalized:
            return
        response = self.simkl_api.get_paginated(normalized, page=page or 1)
        self.tmdb_cache_only = False
        self.set_mixed_content(response.paginated_items_dict)
        self.library = 'video'
        return response.items + response.next_page


class ListHold(ContainerDirectory):
    """Simkl on hold — GET /sync/all-items/shows/hold + /sync/all-items/anime/hold.
    Shows only — Simkl movies have no 'hold' status; an anime film is left out.
    """

    def get_items(self, tmdb_type=None, page=None, **kwargs):
        r = _simkl_get(self.simkl_api, '/sync/all-items/shows/hold')
        shows_raw = (r or {}).get('shows')
        normalized = _normalize_plantowatch(None, shows_raw)
        r = _simkl_get(self.simkl_api, '/sync/all-items/anime/hold')
        _merge_anime(normalized, _resolve_anime(self.tmdb_api, (r or {}).get('anime')), 'tv')
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
    anime_only='true' / asian_only='true' keep only anime / Asian titles (Anime / Asian Progress menus); other_only='true' keeps
    everything else (Movies and TV Shows menu).
    """

    def _keep_scope(self, normalized, scope):
        from tmdbhelper.lib.api.mdblist.lists import _in_scope
        kept = {}
        for key, rows in normalized.items():
            tmdb_type = 'movie' if key == 'movies' else 'tv'
            rows = [r for r in rows if _in_scope(self.tmdb_api.get_request_sc(tmdb_type, r['id']) or {}, scope, tmdb_type)]
            if rows:
                kept[key] = rows
        return kept

    def get_items(self, tmdb_type=None, page=None, anime_only=None, asian_only=None, other_only=None, **kwargs):
        from tmdbhelper.lib.api.mdblist.lists import _get_scope
        raw = _simkl_get(self.simkl_api, '/sync/playback')
        normalized = _normalize_inprogress(raw if isinstance(raw, list) else [])
        if tmdb_type == 'movie':
            normalized = {k: v for k, v in normalized.items() if k == 'movies'}
        elif tmdb_type == 'tv':
            normalized = {k: v for k, v in normalized.items() if k == 'shows'}
        scope = _get_scope(anime_only, asian_only, other_only)
        if normalized and scope:
            normalized = self._keep_scope(normalized, scope)
        if not normalized:
            return
        response = self.simkl_api.get_paginated(normalized, page=page or 1)
        self.tmdb_cache_only = False
        self.set_mixed_content(response.paginated_items_dict)
        self.library = 'video'
        return response.items + response.next_page


class ListNextUp(ContainerDirectory):
    """Simkl Next Up: one row per show Simkl lists as 'watching', each showing that show's next episode
    to watch (Simkl's next_to_watch). The most recently watched show comes first. GET /sync/all-items/shows/watching
    + /sync/all-items/anime/watching (anime shows only; an anime's "E12" is mapped to a TMDb season, see
    _anime_tmdb_season).
    Shows that are caught up (next_to_watch null) are left out, and so is any row whose episode TMDb does not
    have or has not aired (a numbering difference between Simkl's TVDB order and TMDb's).
    """

    def get_items(self, tmdb_type=None, page=None, anime_only=None, asian_only=None, other_only=None, **kwargs):
        from tmdbhelper.lib.api.mdblist.lists import _get_scope, _in_scope
        scope = _get_scope(anime_only, asian_only, other_only)
        r = _simkl_get(self.simkl_api, '/sync/all-items/shows/watching')
        ra = _simkl_get(self.simkl_api, '/sync/all-items/anime/watching')
        rows = _normalize_nextup(
            r.get('shows') if isinstance(r, dict) else None, ra.get('anime') if isinstance(ra, dict) else None)
        anime_rows = [row for row in rows if row['season'] is None]
        if anime_rows:  # anime: Simkl's cour numbering -> a TMDb id, season and episode
            # only the unfiltered and the Anime Next Up keep anime rows: no AniList / ARM work for the Movies and TV Shows / Asian menus
            targets = _anime_next_targets(self.tmdb_api, anime_rows, use_anilist=scope in (None, 'anime'))
            ways = {}
            for row, target in zip(anime_rows, targets):
                row.pop('total', None)
                if target:
                    row['id'], row['season'], row['episode'] = target[:3]
                    ways[target[3]] = ways.get(target[3], 0) + 1
            kodi_log(f'Simkl Next Up anime: {len(anime_rows)} rows, mapped {sum(ways.values())} ({ways})', 1)
        episodes = []
        dropped = 0
        for row in rows:
            if row['season'] is None:  # an anime row that could not be mapped
                dropped += 1
                continue
            details = self.tmdb_api.get_request_sc('tv', row['id']) or {}
            if scope and not _in_scope(details, scope, 'tv'):
                continue
            if not _exists_in_tmdb(details, row['season'], row['episode']):
                dropped += 1
                continue
            episodes.append({
                'id': row['id'], 'mediatype': 'episode', 'season': row['season'], 'episode': row['episode'],
                'title': row['title'] or details.get('name')})
        if dropped:
            kodi_log(f'Simkl Next Up: {dropped} of {len(rows)} next episodes not in TMDb numbering or not aired yet - left out', 1)
        if not episodes:
            return
        response = self.simkl_api.get_paginated({'episodes': episodes}, page=page or 1)
        self.tmdb_cache_only = False
        self.set_mixed_content(response.paginated_items_dict)
        self.library = 'video'
        self.plugin_category = get_localized(32572)
        return response.items + response.next_page
