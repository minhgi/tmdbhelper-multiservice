"""Asian Drama and Anime TV calendars from Simkl's public CDN (data.simkl.in/calendar/v2/tv.json and anime.json).

The files list every episode airing from yesterday to about five weeks ahead (UTC air times, Simkl numbering) with a metadata
block per show (country, genres, TMDb id, ...). In a one week probe Trakt's calendar held 10 Asian shows and 15 episodes where the
TV file held 105 shows and 193 episodes, with identical air times on the episodes both listed; Trakt's anime calendar held 32 shows
and 41 episodes where the anime file held 131 and 149.

Each file is read once and only the rows the calendars need are kept (Asian dramas; non-movie anime with a TMDb id), in a JSON file
shared by every day page for CACHE_SECONDS. TV rows are returned in the shape of a Trakt calendar row so the existing episode builder is
used unchanged. The anime rows need their TMDb season and episode worked out first (Simkl numbers every cour from 1): see
api/simkl/calendar_anime.py. A window the file does not cover (yesterday and earlier, or more than the five weeks ahead) is left to
Trakt: covers() says so. If Simkl cannot be reached the failure is remembered for RETRY_SECONDS, so the calendars fall back to Trakt at
once instead of waiting on a dead server for every day page.
"""
import json
import time
import datetime

from tmdbhelper.lib.addon.logger import kodi_log

CDN_URL = 'https://data.simkl.in/calendar/v2/{catalog}.json'
CACHE_FILE = 'special://profile/addon_data/plugin.video.themoviedb.helper/simkl_calendar_{catalog}.json'
CACHE_SECONDS = 12 * 3600
RETRY_SECONDS = 15 * 60                            # after a failed download: use the old file (or Trakt) this long before trying again
TIMEOUT = 20
ASIAN_COUNTRIES = ('kr', 'cn', 'jp', 'th', 'tw')   # the five countries of the Asian menus (Hong Kong is not one of them)
REQUIRED_GENRE = 'drama'                           # same rule as the Asian Drama menus
SKIP_GENRES = {'animation', 'anime', 'reality', 'talk show', 'game show', 'documentary', 'news', 'children', 'sport'}
FINALE_TYPES = {3: 'season_finale'}                # Simkl finale_type 3 = Trakt season_finale (seen on the one episode both list); 1 and 2 unknown
ALT_TITLES_KEPT = 12


def _cache_path(catalog='tv'):
    try:
        import xbmcvfs
        return xbmcvfs.translatePath(CACHE_FILE.format(catalog=catalog))
    except ImportError:
        return None


def _read_cache(catalog='tv'):
    try:
        with open(_cache_path(catalog), 'r', encoding='utf-8') as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError, TypeError):
        return {}


def _write_cache(data, catalog='tv'):
    path = _cache_path(catalog)
    if not path:
        return
    try:
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(data, f)
    except OSError as exc:
        kodi_log(f'Simkl calendar: cache write failed: {type(exc).__name__}', 1)


def _file_parts(data):
    """(calendar rows, metadata, first date, last date) of a CDN calendar file, or None for an unexpected file."""
    calendar_rows = data.get('calendar') if isinstance(data, dict) else None
    metadata = data.get('metadata') if isinstance(data, dict) else None
    if not isinstance(calendar_rows, list) or not isinstance(metadata, dict) or not calendar_rows:
        return None
    dates = sorted(r['date'] for r in calendar_rows if isinstance(r, dict) and r.get('date'))
    if not dates:
        return None
    return calendar_rows, metadata, dates[0][:10], dates[-1][:10]


def _parse(data):
    """The Asian drama rows of a CDN TV calendar file plus the dates the whole file covers, or None for an unexpected file."""
    parts = _file_parts(data)
    if not parts:
        return None
    calendar_rows, metadata, first, last = parts
    rows = []
    for row in calendar_rows:
        meta = metadata.get(str(row.get('simkl_id'))) or {}
        country = str(meta.get('country') or '').lower()
        genres = {str(g).lower() for g in meta.get('genres') or []}
        ids = meta.get('ids') or {}
        episode = row.get('episode') or {}
        if country not in ASIAN_COUNTRIES or REQUIRED_GENRE not in genres or genres & SKIP_GENRES:
            continue
        if not ids.get('tmdb') or not row.get('date') or episode.get('episode') is None:
            continue
        rows.append({
            'date': row['date'], 'country': country, 'season': episode.get('season'), 'episode': episode.get('episode'),
            'title': episode.get('title'), 'runtime': episode.get('runtime'), 'finale_type': row.get('finale_type'),
            'show': meta.get('title'), 'year': str(meta.get('release_date') or '')[:4],
            'ids': {k: (int(v) if str(v).isdigit() else v) for k, v in (  # Trakt's ids are numbers (the CDN sends strings), imdb stays text
                ('tmdb', ids.get('tmdb')), ('simkl', ids.get('simkl_id')), ('imdb', ids.get('imdb')), ('tvdb', ids.get('tvdb'))) if v}})
    return {'from': first, 'to': last, 'rows': rows}


def _parse_anime(data):
    """The anime rows (not movies, with a TMDb id) of a CDN anime calendar file plus the dates it covers, or None for an unexpected file.
    Rows carry what calendar_anime needs to check the TMDb match: the show's titles and year."""
    parts = _file_parts(data)
    if not parts:
        return None
    calendar_rows, metadata, first, last = parts
    rows = []
    for row in calendar_rows:
        meta = metadata.get(str(row.get('simkl_id'))) or {}
        ids = meta.get('ids') or {}
        episode = row.get('episode')
        if not isinstance(episode, dict) or episode.get('episode') is None or not row.get('date'):
            continue
        if str(meta.get('anime_type') or '').lower() == 'movie' or not str(ids.get('tmdb') or '').isdigit():
            continue
        rows.append({
            'date': row['date'], 'episode': episode.get('episode'), 'title': episode.get('title'), 'runtime': episode.get('runtime'),
            'simkl': row.get('simkl_id'), 'tmdb': int(ids['tmdb']), 'show': meta.get('title'), 'romaji': meta.get('title_romaji'),
            'alt': [a.get('name') for a in (meta.get('alt_titles') or [])[:ALT_TITLES_KEPT] if isinstance(a, dict) and a.get('name')],
            'year': str(meta.get('release_date') or '')[:4]})
    return {'from': first, 'to': last, 'rows': rows}


def _load(catalog='tv', post=None):
    """The cached subset, else a fresh download. None when Simkl cannot be reached (the caller then uses Trakt)."""
    cache = _read_cache(catalog)
    now = time.time()
    if cache.get('rows') is not None and now - float(cache.get('fetched') or 0) < CACHE_SECONDS:
        return cache
    if now - float(cache.get('failed') or 0) < RETRY_SECONDS:   # a download failed a moment ago: do not wait on it again
        return cache if cache.get('rows') is not None else None
    parsed = None
    try:
        from tmdbhelper.lib.api.api_keys.simkl import CLIENT_ID
        if CLIENT_ID:
            import requests
            response = (post or requests.get)(
                CDN_URL.format(catalog=catalog), params={'client_id': CLIENT_ID, 'app-name': 'tmdbhelper', 'app-version': '1.0'},
                headers={'User-Agent': 'TMDbHelper'}, timeout=TIMEOUT)
            if response.status_code == 200:
                parsed = (_parse_anime if catalog == 'anime' else _parse)(response.json())
            else:
                kodi_log(f'Simkl calendar: HTTP {response.status_code}', 1)
    except Exception as exc:
        kodi_log(f'Simkl calendar: {type(exc).__name__}', 1)
    if not parsed:
        _write_cache(dict(cache, failed=now), catalog)   # keeps an old file, if any, and stops the retries for RETRY_SECONDS
        return cache if cache.get('rows') is not None else None
    parsed['fetched'] = now
    _write_cache(parsed, catalog)
    kodi_log(f'Simkl calendar ({catalog}): {len(parsed["rows"])} episodes, {parsed["from"]} to {parsed["to"]}', 1)
    return parsed


def covers(startdate, days, today=None, data=None, catalog='tv'):
    """Whether the file covers the whole window (startdate days from today, for days days) with a day to spare at both ends
    for the time zone conversion that follows."""
    data = data or _load(catalog)
    if not data:
        return False
    today = today or datetime.date.today()
    first = today + datetime.timedelta(days=int(startdate))
    last = first + datetime.timedelta(days=max(int(days), 1) - 1)
    return (first - datetime.timedelta(days=1)).isoformat() >= data['from'] and (last + datetime.timedelta(days=1)).isoformat() <= data['to']


def window_rows(data, startdate, days, today=None):
    """The rows of a loaded file inside the window, one day wider each side (the caller trims to the local day)."""
    today = today or datetime.date.today()
    first = today + datetime.timedelta(days=int(startdate) - 1)
    last = today + datetime.timedelta(days=int(startdate) + max(int(days), 1))
    return [r for r in data['rows'] if first.isoformat() <= r['date'][:10] <= last.isoformat()]


def episodes(startdate, days, countries=None, today=None, data=None):
    """Trakt-shaped calendar rows of the Asian dramas for the window and countries."""
    data = data or _load()
    if not data:
        return []
    wanted = {c.strip().lower() for c in (countries or '').split(',') if c.strip()} or set(ASIAN_COUNTRIES)
    out = []
    for r in window_rows(data, startdate, days, today):
        if r['country'] not in wanted:
            continue
        if r['finale_type'] in FINALE_TYPES:
            episode_type = FINALE_TYPES[r['finale_type']]
        elif r['episode'] == 1:
            episode_type = 'series_premiere' if r['season'] in (1, None) else 'season_premiere'
        else:
            episode_type = None
        episode = {'season': r['season'], 'number': r['episode'], 'title': r['title'], 'runtime': r['runtime']}
        if episode_type:
            episode['episode_type'] = episode_type
        out.append({
            'first_aired': r['date'][:19] + '.000Z', 'episode': episode,
            'show': {'title': r['show'], 'year': int(r['year']) if r['year'].isdigit() else None, 'ids': r['ids'], 'genres': []}})
    out.sort(key=lambda row: row['first_aired'])
    return out
