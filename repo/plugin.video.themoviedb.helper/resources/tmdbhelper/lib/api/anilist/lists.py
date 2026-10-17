"""Anime browse lists from AniList - the same source and the same queries as Otaku Testing's lists (its
"browser.api" is AniList) - opened inside TMDb Helper.

AniList has no TMDb ids. Each AniList title is searched on TMDb (English, then romaji, then Japanese title; Japanese
animation only) with TMDb Helper's own cached search, and the matches are cached for 12 hours. A title TMDb does not
list under any of those names is left out, and several AniList entries of one series collapse into one row (the
series page), the same limits the Simkl anime views have.

Lists (Otaku's names and sorts): Airing Last / This / Next Season, Trending and Popular for Last Year / This Year /
Last Season / This Season / All Time, Top 100 (score) and Genres & Tags (genre presets plus Multi Select).
"""
import json
import re
import time

from tmdbhelper.lib.items.container import ContainerDirectory
from tmdbhelper.lib.addon.plugin import ADDONPATH, PLUGINPATH
from tmdbhelper.lib.addon.logger import kodi_log

ANILIST_URL = 'https://graphql.anilist.co'
ANILIST_QUERY = (
    'query ($page: Int=1, $perpage: Int=50, $type: MediaType, $format: MediaFormat, $isAdult: Boolean=false,'
    ' $season: MediaSeason,'
    ' $year: String, $genres: [String], $tags: [String], $sort: [MediaSort]=[POPULARITY_DESC, SCORE_DESC]) {'
    ' Page(page: $page, perPage: $perpage) { pageInfo { hasNextPage }'
    ' media(type: $type, format: $format, genre_in: $genres, tag_in: $tags, season: $season, startDate_like: $year, sort: $sort,'
    ' isAdult: $isAdult) { id format title { romaji english native } startDate { year } } } }')
ANILIST_COLLECTIONS = 'query { genres: GenreCollection tags: MediaTagCollection { name isAdult } }'
SEASON_NAMES = ('WINTER', 'SPRING', 'SUMMER', 'FALL')
SEASON_LABELS = {'last': 'Airing Last Season', 'this': 'Airing This Season', 'next': 'Airing Next Season'}
PERIODS = (
    ('last_year', 'Trending Last Year'), ('this_year', 'Trending This Year'),
    ('last_season', 'Trending Last Season'), ('this_season', 'Trending This Season'),
    ('all_time', 'All Time Trending'))
POPULAR_PERIODS = (
    ('last_year', 'Popular Last Year'), ('this_year', 'Popular This Year'),
    ('last_season', 'Popular Last Season'), ('this_season', 'Popular This Season'),
    ('all_time', 'All Time Popular'))
# Otaku's genre presets (Hentai left out: adult). Otaku asks AniList for 'Shoujo' as a genre, which AniList does not
# have (0 hits); it is a tag, so that row asks for the tag.
GENRE_PRESETS = (
    ('Action', 'genres'), ('Adventure', 'genres'), ('Comedy', 'genres'), ('Drama', 'genres'), ('Ecchi', 'genres'),
    ('Fantasy', 'genres'), ('Horror', 'genres'), ('Shoujo', 'tags'), ('Mecha', 'genres'), ('Music', 'genres'),
    ('Mystery', 'genres'), ('Psychological', 'genres'), ('Romance', 'genres'), ('Sci-Fi', 'genres'),
    ('Slice of Life', 'genres'), ('Sports', 'genres'), ('Supernatural', 'genres'), ('Thriller', 'genres'))
SKIP_FORMATS = ('MUSIC', 'SPECIAL')
# Anime Movies reuse every list below with anime_format=MOVIE in the params: AniList is then asked for movies only
FORMAT_PARAM = 'anime_format'
MOVIE_FORMAT = 'MOVIE'
PAGES = 2
PERPAGE = 50
CACHE_SECONDS = 12 * 3600
CACHE_KEEP_SECONDS = 3 * 24 * 3600
CACHE_FILE = 'special://profile/addon_data/plugin.video.themoviedb.helper/anime_anilist_cache.json'
LIST_SEPARATOR = '|'


def _num(value, default=0):
    try:
        return float(value)
    except (ValueError, TypeError):
        return default


def _season_of(period, today=None):
    """(year, quarter 0-3) of the season before (last), containing (this) or after (next) today. Quarters are
    Winter Jan-Mar, Spring Apr-Jun, Summer Jul-Sep, Fall Oct-Dec - the cut Otaku's AniList lists use."""
    today = today or time.localtime()
    year, quarter = today.tm_year, (today.tm_mon - 1) // 3
    quarter += {'last': -1, 'next': 1}.get(period, 0)
    if quarter < 0:
        quarter, year = quarter + 4, year - 1
    elif quarter > 3:
        quarter, year = quarter - 4, year + 1
    return year, quarter


def _season_variables(period, today=None):
    year, quarter = _season_of(period, today)
    return {'season': SEASON_NAMES[quarter], 'year': f'{year}%'}


def list_variables(kind, period=None, genres=(), tags=(), today=None):
    """AniList query variables for one list - the same filters and sorts Otaku uses."""
    today = today or time.localtime()
    variables = {'type': 'ANIME'}
    if kind == 'season':
        variables.update(_season_variables(period, today))
        variables['sort'] = 'TRENDING_DESC' if period == 'last' else 'POPULARITY_DESC'
    elif kind in ('trending', 'popular'):
        variables['sort'] = 'TRENDING_DESC' if kind == 'trending' else 'POPULARITY_DESC'
        if period == 'last_year':
            variables['year'] = f'{today.tm_year - 1}%'
        elif period == 'this_year':
            variables['year'] = f'{today.tm_year}%'
        elif period in ('last_season', 'this_season'):
            variables.update(_season_variables(period.split('_')[0], today))
    elif kind == 'top100':
        variables['sort'] = 'SCORE_DESC'
    elif kind == 'genre':
        variables['sort'] = 'POPULARITY_DESC'
        if genres:
            variables['genres'] = list(genres)
        if tags:
            variables['tags'] = list(tags)
    return variables


# ---- AniList titles -> TMDb ids ---------------------------------------------------------------------------------

_SEASON_WORDS = re.compile(
    r'(?i)[\s:\-]*(?:\b\d+(?:st|nd|rd|th)\s+season\b|\bseason\s+\d+\b|\b(?:second|third|fourth|fifth|final|last)\s+season\b'
    r'|\bpart\s+\d+\b|\bcour\s+\d+\b)')


def _clean_title(title):
    """Drop 'Season 3' / '2nd Season' / 'Part 2' style words so a sequel is searched under its series name."""
    return re.sub(r'\s+', ' ', _SEASON_WORDS.sub('', title or '')).strip(' :-')


def _title_queries(title):
    """Search strings for one AniList title, best first: English then romaji, each as is, without a trailing number
    and cut before a colon; then the Japanese title."""
    title = title or {}
    queries = []
    for raw in (title.get('english'), title.get('romaji')):
        cleaned = _clean_title(raw)
        for query in (cleaned, re.sub(r'\s+\d+$', '', cleaned), cleaned.split(':')[0].strip()):
            if len(query) >= 3 and query not in queries:
                queries.append(query)
    native = (title.get('native') or '').strip()
    if len(native) >= 2 and native not in queries:
        queries.append(native)
    return queries


def _match_tmdb(tmdb_api, media):
    """(mediatype, tmdb id) of the Japanese animation TMDb finds for an AniList entry, or None."""
    tmdb_type = 'movie' if media.get('format') == 'MOVIE' else 'tv'
    for query in _title_queries(media.get('title')):
        for result in tmdb_api.get_tmdb_id(tmdb_type, query=query, raw_data=True) or []:
            if result.get('original_language') == 'ja' and 16 in (result.get('genre_ids') or []) and result.get('id'):
                return ('movie' if tmdb_type == 'movie' else 'show'), int(result['id'])
    return None


def _post(requests, query, variables=None):
    response = requests.post(
        ANILIST_URL, timeout=30, headers={'User-Agent': 'TMDbHelper'}, json={'query': query, 'variables': variables or {}})
    if response.status_code != 200:
        kodi_log(f'Anime AniList: HTTP {response.status_code}', 1)
        return None
    return (response.json() or {}).get('data')


def _fetch_media(variables):
    """AniList titles of a list (PAGES x PERPAGE, in AniList order), or None when AniList cannot be reached."""
    import requests
    media = []
    for page in range(1, PAGES + 1):
        try:
            data = (_post(requests, ANILIST_QUERY, dict(variables, page=page, perpage=PERPAGE)) or {}).get('Page') or {}
        except Exception as exc:
            kodi_log(f'Anime AniList: request failed: {type(exc).__name__}', 1)
            return media or None
        media += [m for m in data.get('media') or [] if m.get('format') not in SKIP_FORMATS]
        if not (data.get('pageInfo') or {}).get('hasNextPage'):
            break
    return media


def _cache_path():
    try:
        import xbmcvfs
        return xbmcvfs.translatePath(CACHE_FILE)
    except ImportError:
        return None


def _read_cache():
    try:
        with open(_cache_path(), 'r', encoding='utf-8') as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError, TypeError):
        return {}


def _write_cache(cache, key, rows):
    path = _cache_path()
    if not path:
        return
    now = time.time()
    cache = {k: v for k, v in cache.items() if now - _num((v or {}).get('fetched')) < CACHE_KEEP_SECONDS}
    cache[key] = {'fetched': now, 'rows': rows}
    try:
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(cache, f)
    except OSError as exc:
        kodi_log(f'Anime AniList: cache write failed: {type(exc).__name__}', 1)


def anilist_rows(tmdb_api, variables):
    """Rows (id / mediatype / title / release_year) for one list: from the 12 h cache, else AniList + TMDb search."""
    key = json.dumps(variables, sort_keys=True)
    cache = _read_cache()
    entry = cache.get(key) or {}
    if entry.get('rows') and time.time() - _num(entry.get('fetched')) < CACHE_SECONDS:
        return entry['rows']
    media = _fetch_media(variables)
    if not media:
        return entry.get('rows') or []   # stale beats nothing
    from tmdbhelper.lib.addon.thread import ParallelThread
    with ParallelThread(media, lambda m: _match_tmdb(tmdb_api, m)) as pt:
        matches = pt.queue
    seen, rows = set(), []
    for m, match in zip(media, matches):
        if not match or match in seen:
            continue
        seen.add(match)
        title = m.get('title') or {}
        rows.append({'id': match[1], 'mediatype': match[0], 'title': title.get('english') or title.get('romaji'),
                     'release_year': (m.get('startDate') or {}).get('year')})
    kodi_log(f'Anime AniList {key}: {len(rows)} TMDb rows from {len(media)} AniList titles', 1)
    if rows:
        _write_cache(cache, key, rows)
    return rows


def _split(value):
    return tuple(v for v in (value or '').split(LIST_SEPARATOR) if v)


def _folder(label, params, icon):
    return {
        'label': label, 'params': dict(params, tmdb_type='both'), 'path': PLUGINPATH,
        'art': {'landscape': f'{ADDONPATH}/fanart.jpg', 'icon': f'{ADDONPATH}/resources/icons/themoviedb/{icon}'}}


# ---- list classes -----------------------------------------------------------------------------------------------

class _ListAnilist(ContainerDirectory):
    """Base: a paged list of the rows anilist_rows() finds for _variables()."""

    category = 'Anime'

    def _variables(self, **kwargs):
        raise NotImplementedError

    def _label(self, **kwargs):
        return self.category

    def _empty(self):
        # An empty list still has to close the directory, or Kodi waits on it
        from xbmcplugin import endOfDirectory
        endOfDirectory(self.handle)

    def get_items(self, page=None, **kwargs):
        movies = kwargs.get(FORMAT_PARAM) == MOVIE_FORMAT
        self.plugin_category = ('Anime Movies: ' if movies else '') + self._label(**kwargs)
        variables = self._variables(**kwargs)
        if movies and variables is not None:
            variables = dict(variables, format=MOVIE_FORMAT)
        rows = anilist_rows(self.tmdb_api, variables) if variables is not None else []
        if not rows:
            return self._empty()
        # Not self.simkl_api.get_paginated(): that object only exists while the Simkl service toggle is on
        from tmdbhelper.lib.items.itemlist import ItemListPagination
        response = ItemListPagination({'items': rows}, page=page or 1)
        self.tmdb_cache_only = False
        self.set_mixed_content(response.paginated_items_dict)
        self.library = 'video'
        return response.items + response.next_page


class ListAnimeSeason(_ListAnilist):
    """Airing Last / This / Next Season."""

    def _label(self, period=None, **kwargs):
        return SEASON_LABELS.get(period, self.category)

    def _variables(self, period=None, **kwargs):
        return list_variables('season', period if period in SEASON_LABELS else 'this')


class ListAnimeTrending(_ListAnilist):
    def _label(self, period=None, **kwargs):
        return dict(PERIODS).get(period, 'Trending')

    def _variables(self, period=None, **kwargs):
        return list_variables('trending', period)


class ListAnimePopular(_ListAnilist):
    def _label(self, period=None, **kwargs):
        return dict(POPULAR_PERIODS).get(period, 'Popular')

    def _variables(self, period=None, **kwargs):
        return list_variables('popular', period)


class ListAnimeTop100(_ListAnilist):
    def _label(self, **kwargs):
        return 'Top 100'

    def _variables(self, **kwargs):
        return list_variables('top100')


class ListAnimeGenre(_ListAnilist):
    """One genre / tag preset, or the picks of Multi Select (genres / tags joined with '|')."""

    def _label(self, genres=None, tags=None, **kwargs):
        return ', '.join(_split(genres) + _split(tags)) or 'Genres & Tags'

    def _variables(self, genres=None, tags=None, **kwargs):
        return list_variables('genre', genres=_split(genres), tags=_split(tags)) if (genres or tags) else None


class ListAnimeGenrePick(ListAnimeGenre):
    """Multi Select...: ask for genres and tags once, then page through the result (the picks ride in the params)."""

    def get_items(self, page=None, picked=None, genres=None, tags=None, **kwargs):
        if not picked:
            import requests
            from xbmcgui import Dialog
            try:
                data = _post(requests, ANILIST_COLLECTIONS) or {}
            except Exception as exc:
                kodi_log(f'Anime AniList: genre list failed: {type(exc).__name__}', 1)
                data = {}
            genre_names = [g for g in data.get('genres') or [] if g != 'Hentai']
            tag_names = [t['name'] for t in data.get('tags') or [] if not t.get('isAdult')]
            chosen = Dialog().multiselect('Multi Select...', genre_names + tag_names) or []
            genres = LIST_SEPARATOR.join(genre_names[i] for i in chosen if i < len(genre_names))
            tags = LIST_SEPARATOR.join(tag_names[i - len(genre_names)] for i in chosen if i >= len(genre_names))
            if not chosen:
                return self._empty()
            # next-page links are built from parent_params (a copy taken at init), so the picks go there as well
            for params in (self.params, self.parent_params):
                params.update({'picked': 'true', 'genres': genres, 'tags': tags})
        return super().get_items(page=page, genres=genres, tags=tags, **kwargs)


class ListAnimeGenres(ContainerDirectory):
    """Genres & Tags - Multi Select... then Otaku's genre presets."""

    def get_items(self, **kwargs):
        movies = kwargs.get(FORMAT_PARAM) == MOVIE_FORMAT
        extra = {FORMAT_PARAM: MOVIE_FORMAT} if movies else {}   # the existing Anime rows keep their exact params
        self.plugin_category = ('Anime Movies: ' if movies else '') + 'Genres & Tags'
        rows = [_folder('Multi Select...', dict({'info': 'anime_genre_pick'}, **extra), 'genre.png')]
        rows += [_folder(name, dict({'info': 'anime_genre', kind: name}, **extra), 'genre.png') for name, kind in GENRE_PRESETS]
        return rows
