"""Asian Drama country Trending: dramas first aired in the last 90 days, ranked by a combined rating.

Candidates come from TMDb Discover (one country, Drama genre, animation / reality / talk left out, first aired in the
last 90 days). Ratings come from one batched MDBList request per chunk: MDBList returns the IMDb, TMDb and Trakt
ratings of each show already on a 0-100 scale, and the combined score is the mean of the ones that have enough votes.
Shows without a usable rating go last, in TMDb popularity order. If MDBList cannot be reached the list falls back to
TMDb's own vote average and is cached only briefly, so the next open tries MDBList again. Lists are cached for 12 h.

country=ALL (the main-menu Trending) merges the five countries' candidates, drops duplicates (co-productions) and ranks
them together by the same score.

Most Watched (This Week / This Month) for a country whose Trakt list is mostly anime (Japan) is read by ListAsianMostWatched:
Trakt's own list only reads the first page, so it scans the first TRAKT_PAGES x 100 shows of Trakt's country list and keeps
the live-action dramas, in Trakt's order (most watchers first).

Scoped search (Search row of the Anime and Asian Cinema & TV hubs): TMDb's own search/multi, then only the movies and TV shows
that belong to the hub are kept - Anime: Japanese-language animation; Asian: Chinese / Cantonese / Korean / Japanese / Thai
language without animation, reality or talk (the same split the menus use).

Asian Movies reuse all of this with media='movie': TMDb Discover movies (Animation and Documentary left out) released in
the last 90 days, MDBList's movie endpoint, and Trakt's movies/watched lists. Movie cache keys carry a "movie2|" prefix.
Movies differ from dramas in three ways: concert films (Music as the only genre) are left out, a rating source needs
MOVIE_MIN_VOTES votes (films with a handful of votes topped the list at 5), and Most Watched reads MOVIE_TRAKT_PAGES pages.
"""
import json
import time
import datetime

from tmdbhelper.lib.items.container import ContainerDirectory
from tmdbhelper.lib.addon.plugin import get_setting
from tmdbhelper.lib.addon.logger import kodi_log

COUNTRY_NAMES = {'KR': 'Korean', 'CN': 'Chinese', 'JP': 'Japanese', 'TH': 'Thai', 'TW': 'Taiwanese'}
ALL = 'ALL'                 # the combined list of all five countries
WINDOW_DAYS = 90
MAX_PAGES = 8                # 20 shows a page; Chinese had about 140 new dramas in 90 days
NOT_DRAMA = '16,10764,10767'  # TMDb TV genre ids: Animation, Reality, Talk
RATING_SOURCES = ('imdb', 'tmdb', 'trakt')
MIN_VOTES = 5                # a source with fewer votes than this is ignored for that show
BATCH = 200
MDBLIST_URL = 'https://api.mdblist.com/tmdb/show'
CACHE_SECONDS = 12 * 3600
FALLBACK_CACHE_SECONDS = 15 * 60
CACHE_KEEP_SECONDS = 3 * 86400
CACHE_FILE = 'special://profile/addon_data/plugin.video.themoviedb.helper/asian_trending_cache.json'
TRAKT_URL = 'https://api.trakt.tv/shows/watched/{period}'
TRAKT_PAGES = 8              # 100 shows a page; Japan: about 45 live-action dramas in the first 800
TRAKT_SKIP_GENRES = {'animation', 'anime', 'donghua', 'reality', 'talk-show', 'game-show', 'talk', 'news', 'documentary'}
MOST_WATCHED_LABELS = {'weekly': 'This Week', 'monthly': 'This Month'}
NOT_MOVIE = '16,99'          # TMDb movie genre ids: Animation, Documentary
MOVIE_SKIP_GENRES = {'animation', 'anime', 'documentary'}
MOVIE_PREFIX = 'movie2|'     # cache key prefix of the movie lists (drama keys stay as they were); 'movie|' rows predate the vote floor
MOVIE_MIN_VOTES = 50         # movies: a source with fewer votes than this is ignored (1-20 vote films topped the list at 5)
MOVIE_TRAKT_PAGES = 4        # movies: Trakt pages scanned for Most Watched (later pages are films with a handful of watchers)
MUSIC_GENRE = 10402          # TMDb genre id; a film whose only genre is Music is a concert film
TRAKT_MUSIC = ['music']
SEARCH_PAGES = 5             # search/multi pages scanned (20 results each) for matches
SEARCH_LANGUAGES = {'anime': ('ja',), 'asian': ('zh', 'cn', 'ko', 'ja', 'th')}   # TMDb files Hong Kong films as 'cn'
SEARCH_LABELS = {'anime': 'Anime', 'asian': 'Asian Cinema & TV'}
SEARCH_NOT_ASIAN = {16, 10764, 10767}   # Animation, Reality, Talk
SEARCH_NOT_ASIAN_MOVIE = {16, 99}        # Animation, Documentary (what the Asian Movies menu leaves out)
SEARCH_SHORT = {'anime': 'Anime', 'asian': 'Asian'}
SEARCH_MEDIA_WORDS = {'movie': 'Movies', 'tv': 'TV Shows'}


def _num(value, default=0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _media_kw(media):
    return {'media': media} if media != 'tv' else {}   # the drama calls keep their original signature


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


def _write_cache(cache, key, rows, degraded):
    path = _cache_path()
    if not path:
        return
    now = time.time()
    cache = {k: v for k, v in cache.items() if now - _num((v or {}).get('fetched')) < CACHE_KEEP_SECONDS}
    cache[key] = {'fetched': now, 'rows': rows, 'degraded': degraded}
    try:
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(cache, f)
    except OSError as exc:
        kodi_log(f'Asian Trending: cache write failed: {type(exc).__name__}', 1)


def window(today=None):
    """(first day, last day) of the 90 day release window as YYYY-MM-DD strings."""
    today = today or datetime.date.today()
    return (today - datetime.timedelta(days=WINDOW_DAYS)).isoformat(), today.isoformat()


def candidates(tmdb_api, country, today=None, media='tv'):
    """TMDb Discover results (dicts, popularity order) for one country's new dramas (or movies with media='movie')."""
    start, end = window(today)
    found = []
    for page in range(1, MAX_PAGES + 1):
        if media == 'movie':
            response = tmdb_api.get_request_sc(
                'discover/movie', with_origin_country=country, without_genres=NOT_MOVIE, sort_by='popularity.desc',
                page=page, **{'primary_release_date.gte': start, 'primary_release_date.lte': end})
        else:
            response = tmdb_api.get_request_sc(
                'discover/tv', with_origin_country=country, with_genres='18', without_genres=NOT_DRAMA,
                sort_by='popularity.desc', page=page, **{'first_air_date.gte': start, 'first_air_date.lte': end})
        if not isinstance(response, dict):
            break
        found += [
            r for r in response.get('results') or []
            if r.get('id') and not (media == 'movie' and list(r.get('genre_ids') or []) == [MUSIC_GENRE])]
        if page >= (response.get('total_pages') or 0):
            break
    return found


def all_candidates(tmdb_api, today=None, media='tv'):
    """The five countries' candidates in one list: no duplicates, most popular first (the order unscored shows keep)."""
    seen, found = set(), []
    for country in COUNTRY_NAMES:
        for show in candidates(tmdb_api, country, today, **_media_kw(media)):
            if show['id'] not in seen:
                seen.add(show['id'])
                found.append(show)
    return sorted(found, key=lambda show: -_num(show.get('popularity')))


def combined_score(item, min_votes=MIN_VOTES):
    """Mean of the usable 0-100 ratings (IMDb / TMDb / Trakt, each with enough votes) of an MDBList item, else None."""
    values = [
        r['score'] for r in item.get('ratings') or []
        if r.get('source') in RATING_SOURCES and r.get('score') is not None and _num(r.get('votes')) >= min_votes]
    return sum(values) / len(values) if values else None


def fetch_scores(tmdb_ids, post=None, media='tv'):
    """{tmdb id: combined score or None} from MDBList, or None when MDBList cannot be used (no key, HTTP error)."""
    url = MDBLIST_URL.replace('/show', '/movie') if media == 'movie' else MDBLIST_URL
    key = get_setting('mdblist_apikey', 'str')
    if not key:
        kodi_log('Asian Trending: no MDBList API key set', 1)
        return None
    if post is None:
        import requests
        post = requests.post
    scores = {}
    for i in range(0, len(tmdb_ids), BATCH):
        try:
            response = post(
                f'{url}?apikey={key}', json={'ids': tmdb_ids[i:i + BATCH]}, timeout=40,
                headers={'User-Agent': 'TMDbHelper', 'Accept': 'application/json'})   # MDBList refuses default agents
            if response.status_code != 200:
                kodi_log(f'Asian Trending: MDBList HTTP {response.status_code}', 1)
                return None
            data = response.json()
        except Exception as exc:
            kodi_log(f'Asian Trending: MDBList failed: {type(exc).__name__}', 1)
            return None
        for item in data if isinstance(data, list) else []:
            tmdb_id = (item.get('ids') or {}).get('tmdb')
            if tmdb_id:
                scores[int(tmdb_id)] = combined_score(item, MOVIE_MIN_VOTES if media == 'movie' else MIN_VOTES)
    return scores


def rank(found, scores, media='tv'):
    """Rows (id / mediatype / title / release_year) best score first; unscored shows keep popularity order at the end.
    With scores=None (MDBList unavailable) TMDb's own vote average ranks the shows that have a few votes."""
    def sort_key(indexed):
        index, show = indexed
        if scores is None:
            voted = _num(show.get('vote_count')) >= 3
            return (0 if voted else 1, -_num(show.get('vote_average')) if voted else 0, index)
        score = scores.get(show['id'])
        return (0 if score is not None else 1, -score if score is not None else 0, index)

    rows = []
    for _, show in sorted(enumerate(found), key=sort_key):
        if media == 'movie':
            rows.append({
                'id': show['id'], 'mediatype': 'movie', 'title': show.get('title') or show.get('original_title'),
                'release_year': int((show.get('release_date') or '0000')[:4]) or None})
            continue
        rows.append({
            'id': show['id'], 'mediatype': 'show', 'title': show.get('name') or show.get('original_name'),
            'release_year': int((show.get('first_air_date') or '0000')[:4]) or None})
    return rows


def trending_rows(tmdb_api, country, today=None, media='tv'):
    """Rows of one country's Trending list: from the cache, else TMDb candidates ranked by MDBList's combined rating."""
    start, _ = window(today)
    prefix = MOVIE_PREFIX if media == 'movie' else ''
    key = f'{prefix}{country}|{start}'
    cache = _read_cache()
    entry = cache.get(key) or {}
    ttl = FALLBACK_CACHE_SECONDS if entry.get('degraded') else CACHE_SECONDS
    if entry.get('rows') and time.time() - _num(entry.get('fetched')) < ttl:
        return entry['rows']
    found = (
        all_candidates(tmdb_api, today, media) if country == ALL
        else candidates(tmdb_api, country, today, **_media_kw(media)))
    if not found:
        return entry.get('rows') or []   # stale beats nothing
    scores = fetch_scores([s['id'] for s in found], **_media_kw(media))
    rows = rank(found, scores, **_media_kw(media))
    scored = sum(1 for v in (scores or {}).values() if v is not None)
    kodi_log(
        f'Asian {"Movie " if media == "movie" else ""}Trending {country}: {len(found)} candidates, {scored} scored'
        f'{"" if scores is not None else " (MDBList unavailable, TMDb votes used)"}', 1)
    _write_cache(cache, key, rows, degraded=scores is None)
    return rows


def trakt_watched(country, period, post=None, media='tv'):
    """Live-action drama rows (movie rows with media='movie') from the first TRAKT_PAGES x 100 of Trakt's most-watched list
    for one country (Trakt order), or None when Trakt cannot be reached."""
    from tmdbhelper.lib.api.api_keys.trakt import CLIENT_ID
    movie = media == 'movie'
    url = TRAKT_URL.replace('/shows/', '/movies/') if movie else TRAKT_URL
    skip_genres = MOVIE_SKIP_GENRES if movie else TRAKT_SKIP_GENRES
    if post is None:
        import requests
        post = requests.get
    headers = {'trakt-api-version': '2', 'trakt-api-key': CLIENT_ID, 'User-Agent': 'TMDbHelper'}
    rows, seen = [], set()
    for page in range(1, (MOVIE_TRAKT_PAGES if movie else TRAKT_PAGES) + 1):
        try:
            response = post(
                url.format(period=period), headers=headers, timeout=30,
                params={'countries': country.lower(), 'limit': 100, 'page': page, 'extended': 'full'})
            if response.status_code != 200:
                kodi_log(f'Asian Most Watched: Trakt HTTP {response.status_code}', 1)
                return rows or None
            data = response.json()
        except Exception as exc:
            kodi_log(f'Asian Most Watched: Trakt failed: {type(exc).__name__}', 1)
            return rows or None
        if not data:
            break
        for item in data:
            show = item.get('movie' if movie else 'show') or {}
            tmdb_id = (show.get('ids') or {}).get('tmdb')
            if not tmdb_id or tmdb_id in seen or skip_genres & set(show.get('genres') or []):
                continue
            if movie and list(show.get('genres') or []) == TRAKT_MUSIC:
                continue   # concert film
            seen.add(tmdb_id)
            rows.append({
                'id': tmdb_id, 'mediatype': 'movie' if movie else 'show', 'title': show.get('title'),
                'release_year': show.get('year')})
    return rows


def most_watched_rows(country, period, media='tv'):
    """Rows of one country's Most Watched list, from the 12 h cache, else Trakt (a stale list beats an empty one)."""
    prefix = MOVIE_PREFIX if media == 'movie' else ''
    key = f'{prefix}watched|{country}|{period}'
    cache = _read_cache()
    entry = cache.get(key) or {}
    if entry.get('rows') and time.time() - _num(entry.get('fetched')) < CACHE_SECONDS:
        return entry['rows']
    rows = trakt_watched(country, period, media=media)
    if not rows:
        return entry.get('rows') or []
    kodi_log(
        f'Asian Most Watched {country} {period}: {len(rows)} '
        f'{"live-action movies" if media == "movie" else "live-action dramas"}', 1)
    _write_cache(cache, key, rows, degraded=False)
    return rows


def scoped_search_rows(tmdb_api, query, scope, media=None):
    """(rows, results fetched): the movies / TV shows of TMDb's search/multi for query that belong to scope ('anime' or
    'asian'), in TMDb's relevance order. With media='movie' or 'tv' (the Movies / TV Shows submenus) only that kind is
    searched (search/movie or search/tv) and the filter is the one the submenu's own lists use: Asian movies leave out
    Animation and Documentary, Asian TV shows need Drama and leave out Animation, Reality and Talk."""
    from urllib.parse import quote_plus
    languages = SEARCH_LANGUAGES[scope]
    rows, seen, fetched = [], set(), 0
    for page in range(1, SEARCH_PAGES + 1):
        # the request layer does not encode parameter values itself, so the query is encoded here (as TMDb Helper's own search does)
        response = tmdb_api.get_request_sc('search', media or 'multi', query=quote_plus(query), page=page)
        if not isinstance(response, dict):
            break
        results = response.get('results') or []
        fetched += len(results)
        for item in results:
            kind = media or item.get('media_type')   # search/movie and search/tv results carry no media_type
            if kind not in ('movie', 'tv') or not item.get('id') or item.get('original_language') not in languages:
                continue
            genres = set(item.get('genre_ids') or [])
            if scope == 'anime':
                if 16 not in genres:
                    continue
            elif media == 'movie':
                if genres & SEARCH_NOT_ASIAN_MOVIE:
                    continue
            elif media == 'tv':
                if 18 not in genres or genres & SEARCH_NOT_ASIAN:
                    continue
            elif genres & SEARCH_NOT_ASIAN:
                continue
            if (kind, item['id']) in seen:
                continue
            seen.add((kind, item['id']))
            date = item.get('release_date') if kind == 'movie' else item.get('first_air_date')
            rows.append({
                'id': item['id'], 'mediatype': 'movie' if kind == 'movie' else 'show',
                'title': item.get('title') or item.get('name') or item.get('original_title') or item.get('original_name'),
                'release_year': int((date or '0000')[:4]) or None})
        if page >= (response.get('total_pages') or 0):
            break
    return rows, fetched


class ListAsianTrending(ContainerDirectory):
    """Trending for one country (country=KR|CN|JP|TH|TW) or all five together (country=ALL)."""
    MEDIA = 'tv'

    def _category(self, country, suffix):
        if self.MEDIA == 'movie':
            return f'{COUNTRY_NAMES.get(country, "Asian")} Movie {suffix}'
        return f'{COUNTRY_NAMES.get(country, "Asian Drama")} {suffix}'

    def _empty(self):
        # An empty list still has to close the directory, or Kodi waits on it
        from xbmcplugin import endOfDirectory
        endOfDirectory(self.handle)

    def _paged(self, rows, page):
        if not rows:
            return self._empty()
        from tmdbhelper.lib.items.itemlist import ItemListPagination
        response = ItemListPagination({'items': rows}, page=page or 1)
        self.tmdb_cache_only = False
        self.set_mixed_content(response.paginated_items_dict)
        self.library = 'video'
        return response.items + response.next_page

    def get_items(self, country=None, page=None, **kwargs):
        country = (country or '').upper()
        if country not in COUNTRY_NAMES and country != ALL:
            return self._empty()
        self.plugin_category = self._category(country, 'Trending')
        rows = trending_rows(self.tmdb_api, country, media=self.MEDIA)
        return self._paged(rows, page)


class ListAsianMostWatched(ListAsianTrending):
    """Most Watched This Week / This Month for one country (country=JP, period=weekly|monthly)."""

    def get_items(self, country=None, period=None, page=None, **kwargs):
        country = (country or '').upper()
        if country not in COUNTRY_NAMES or period not in MOST_WATCHED_LABELS:
            return self._empty()
        self.plugin_category = self._category(country, f'Most Watched {MOST_WATCHED_LABELS[period]}')
        rows = most_watched_rows(country, period, media=self.MEDIA)
        return self._paged(rows, page)


class ListAsianMovieTrending(ListAsianTrending):
    """Movie Trending: new Asian movies of one country (or all five, country=ALL) ranked by the combined rating."""
    MEDIA = 'movie'


class ListAsianMovieMostWatched(ListAsianMostWatched):
    """Movie Most Watched This Week / This Month for one country (Japan: anime films filtered out of a deep Trakt scan)."""
    MEDIA = 'movie'


class ListScopedSearch(ListAsianTrending):
    """Search row of the Anime / Asian Cinema & TV hubs (search_scope=anime|asian): asks for a query, then lists the matches."""

    def get_items(self, search_scope=None, search_media=None, query=None, update_listing=False, page=None, **kwargs):
        if search_scope not in SEARCH_LANGUAGES:
            return self._empty()
        media = search_media if search_media in SEARCH_MEDIA_WORDS else None
        original_query = query
        if not query:
            from tmdbhelper.lib.addon.consts import PARAM_WIDGETS_RELOAD_FORCED
            if kwargs.get('reload') == PARAM_WIDGETS_RELOAD_FORCED:
                return
            from xbmcgui import Dialog, INPUT_ALPHANUM
            from tmdbhelper.lib.addon.plugin import get_localized
            query = Dialog().input(get_localized(32044), type=INPUT_ALPHANUM)
            if not query:
                return
        rows, fetched = scoped_search_rows(self.tmdb_api, query, search_scope, media)
        kodi_log(f'Scoped search {search_scope}{" " + media if media else ""} {query!r}: kept {len(rows)} of {fetched}', 1)
        if not original_query:
            # Same as TMDb Helper's own search: re-write the path with the query so Back does not ask again and next-page links keep it
            from urllib.parse import urlencode
            from tmdbhelper.lib.addon.plugin import PLUGINPATH
            params = dict(kwargs, info='scoped_search', search_scope=search_scope, tmdb_type='both', page=page,
                          query=query, update_listing='True')
            if media:
                params['search_media'] = media
            self.container_update = f'{PLUGINPATH}?{urlencode(params)}'
            self.parent_params = params
        self.update_listing = True if update_listing else False
        label = f'{SEARCH_SHORT[search_scope]} {SEARCH_MEDIA_WORDS[media]}' if media else SEARCH_LABELS[search_scope]
        self.plugin_category = f'Search {label} - {query}'
        return self._paged(rows, page)
