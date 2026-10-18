"""MyAnimeList and AniList ranks and scores for anime, as rating properties for the skins (monitor/common.py get_all_ratings).

MDBList already sends the MyAnimeList score and vote count (myanimelist_rating / myanimelist_votes); this adds what it does not have:
  myanimelist_rank, myanimelist_popularity   from Tenrai (api.tenrai.org, the Jikan-compatible MyAnimeList mirror Seren uses)
  anilist_rating (0-10), anilist_rank (all-time "highest rated"), anilist_popularity (all-time "most popular")   from AniList

Per item: the TMDb id is turned into a MyAnimeList and an AniList id with the ARM service (GET /api/v2/themoviedb?id=), which
answers with one record per season / cour / movie, so a choice has to be made (pick()): a movie takes its MOVIE record, a show page
takes the first season's entry (the lowest MyAnimeList id of the lowest season), a season or episode page takes the entry filed under
that season, else the first season's. Only Japanese and Chinese language titles are looked up (callers gate on original_language).

Everything is saved on disk (ids 90 days, "not anime" 14 days, scores 12 hours) and merged on save, since the monitor service and
plugin processes write at the same time. A lookup runs in its own thread and an info page waits for it at most WAIT seconds: a cold
lookup that is slower finishes in the background and is served from the file next time. A source that fails is left alone for 15
minutes (AniList allows about 30 requests a minute; a 429 pauses it for a minute).
"""
import json
import os
import threading
import time

from tmdbhelper.lib.addon.logger import kodi_log

ARM_URL = 'https://arm.haglund.dev/api/v2/themoviedb'
TENRAI_URL = 'https://api.tenrai.org/v1/anime/{}'
ANILIST_URL = 'https://graphql.anilist.co'
ANILIST_QUERY = 'query($id:Int){Media(id:$id,type:ANIME){averageScore popularity rankings{rank type allTime}}}'
CACHE_FILE = 'special://profile/addon_data/plugin.video.themoviedb.helper/anime_scores_cache.json'
LANGUAGES = ('ja', 'zh')
TIMEOUT = 3
WAIT = 2.5
IDS_TTL = 90 * 24 * 3600
NONE_TTL = 14 * 24 * 3600
SCORES_TTL = 12 * 3600
FAIL_TTL = 15 * 60
RATE_TTL = 60
SHOW_FORMATS = ('TV', 'ONA')       # ARM media values that count as the show (not OVA, SPECIAL, MOVIE)
_lock = threading.Lock()


def _cache_path():
    try:
        import xbmcvfs
        return xbmcvfs.translatePath(CACHE_FILE)
    except ImportError:
        return None


def _read():
    try:
        with open(_cache_path(), 'r', encoding='utf-8') as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError, TypeError):
        return {}


def _valid(entry, now):
    try:
        return isinstance(entry, dict) and now - float(entry['ts']) < float(entry['ttl'])
    except (KeyError, ValueError, TypeError):
        return False


def _save(new, now):
    """Merge entries into the file on disk (re-read just before writing; the newer entry per key wins) and drop expired ones."""
    path = _cache_path()
    if not path or not new:
        return
    with _lock:
        table = _read()
        for key, entry in new.items():
            if not _valid(table.get(key), now) or float(table[key]['ts']) <= entry['ts']:
                table[key] = entry
        keep = {k: v for k, v in table.items() if _valid(v, now)}
        tmp = f'{path}.{os.getpid()}.{threading.get_ident()}.tmp'
        try:
            with open(tmp, 'w', encoding='utf-8') as f:
                json.dump(keep, f)
            os.replace(tmp, path)
        except OSError as exc:
            kodi_log(f'Anime scores: cache write failed: {type(exc).__name__}', 1)
            try:
                os.remove(tmp)
            except OSError:
                pass


def _int(value):
    try:
        return int(value)
    except (ValueError, TypeError):
        return None


def compact(records):
    """The parts of ARM's records the choice needs: [{'m': MyAnimeList id, 'a': AniList id, 's': TMDb season, 'f': media}]."""
    out = []
    for r in records if isinstance(records, list) else []:
        if isinstance(r, dict) and (_int(r.get('myanimelist')) or _int(r.get('anilist'))):
            out.append({'m': _int(r.get('myanimelist')), 'a': _int(r.get('anilist')), 's': _int(r.get('themoviedb-season')), 'f': r.get('media')})
    return out


def pick(records, tmdb_type, season=None):
    """(MyAnimeList id, AniList id) of the record this page is about, or None. TMDb gives movies and shows ids from one number
    space, so the record's media decides which kind of page it can belong to."""
    if tmdb_type == 'movie':
        group = [r for r in records if r.get('f') == 'MOVIE']
    else:
        shows = [r for r in records if r.get('f') in SHOW_FORMATS and (r.get('s') or 0) > 0]
        group = [r for r in shows if r.get('s') == _int(season)] if _int(season) else []
        if not group and shows:
            first = min(r['s'] for r in shows)
            group = [r for r in shows if r['s'] == first]
    if not group:
        return None
    best = min(group, key=lambda r: (r.get('m') or 10 ** 9, r.get('a') or 10 ** 9))
    return best.get('m'), best.get('a')


def _get(url, **kwargs):
    import requests
    return requests.get(url, timeout=TIMEOUT, headers={'User-Agent': 'TMDbHelper'}, **kwargs)


def _arm(tmdb_id):
    """ARM's records for a TMDb id (a list, empty for a title that is not anime), or None when ARM could not be reached."""
    response = _get(ARM_URL, params={'id': tmdb_id})
    if response.status_code != 200:
        return None
    data = response.json()
    return compact(data) if isinstance(data, list) else None


def _tenrai(mal_id):
    """{'rank', 'popularity'} from Tenrai ({} when MyAnimeList does not have them), or None when Tenrai could not be reached."""
    response = _get(TENRAI_URL.format(mal_id))
    if response.status_code == 404:
        return {}
    if response.status_code != 200:
        return None
    data = (response.json() or {}).get('data') or {}
    out = {}
    if _int(data.get('rank')):
        out['myanimelist_rank'] = str(_int(data['rank']))
    if _int(data.get('popularity')):
        out['myanimelist_popularity'] = str(_int(data['popularity']))
    return out


def _anilist(anilist_id):
    """{'rating', 'rank', 'popularity'} from AniList, or None when it could not be reached (429 raises RateLimited)."""
    import requests
    response = requests.post(ANILIST_URL, json={'query': ANILIST_QUERY, 'variables': {'id': anilist_id}}, timeout=TIMEOUT, headers={'User-Agent': 'TMDbHelper'})
    if response.status_code == 429:
        raise RateLimited()
    if response.status_code != 200:
        return None
    media = ((response.json() or {}).get('data') or {}).get('Media') or {}
    out = {}
    if _int(media.get('averageScore')):
        out['anilist_rating'] = f'{_int(media["averageScore"]) / 10:.1f}'
    ranks = {}
    for r in media.get('rankings') or []:
        if isinstance(r, dict) and r.get('allTime') and _int(r.get('rank')):
            ranks[r.get('type')] = min(ranks.get(r.get('type')) or 10 ** 9, _int(r['rank']))
    if ranks.get('RATED'):
        out['anilist_rank'] = str(ranks['RATED'])
    if ranks.get('POPULAR'):
        out['anilist_popularity'] = str(ranks['POPULAR'])
    return out


class RateLimited(Exception):
    pass


def _fetch(source, func, key, arg, table, new, now):
    """One cached request: the saved answer, else the request (saved on success, the source set aside for a while on failure).
    None when there is no answer."""
    entry = table.get(key)
    if _valid(entry, now):
        return entry.get('v')
    wait = table.get(f'wait:{source}')
    if _valid(wait, now):
        return None
    try:
        value = func(arg)
    except RateLimited:
        new[f'wait:{source}'] = {'ts': now, 'ttl': RATE_TTL, 'v': 0}
        return None
    except Exception as exc:
        kodi_log(f'Anime scores: {source} failed: {type(exc).__name__}', 1)
        value = None
    if value is None:
        new[f'wait:{source}'] = {'ts': now, 'ttl': FAIL_TTL, 'v': 0}
        return None
    ttl = SCORES_TTL
    if source == 'arm':
        ttl = IDS_TTL if value else NONE_TTL
    new[key] = {'ts': now, 'ttl': ttl, 'v': value}
    return value


def _properties(tmdb_type, tmdb_id, season):
    now = time.time()
    table = _read()
    new = {}
    records = _fetch('arm', _arm, f'arm:{tmdb_id}', tmdb_id, table, new, now)
    chosen = pick(records, tmdb_type, season) if records else None
    results = {}
    if chosen:
        mal_id, anilist_id = chosen
        jobs = []
        if mal_id:
            jobs.append(('tenrai', _tenrai, f'tenrai:{mal_id}', mal_id))
        if anilist_id:
            jobs.append(('anilist', _anilist, f'anilist:{anilist_id}', anilist_id))

        def _run(job):
            results[job[0]] = _fetch(*job[:4], table, new, now)

        threads = [threading.Thread(target=_run, args=(job,)) for job in jobs]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
    _save(new, now)
    out = {}
    for value in results.values():
        out.update(value or {})
    return out


def source_ids(tmdb_type, tmdb_id, season=None):
    """(MyAnimeList id, AniList id) of the record a movie / show / season page is about (ARM, saved like the scores), or None."""
    if tmdb_type not in ('movie', 'tv') or not _int(tmdb_id):
        return None
    now = time.time()
    new = {}
    try:
        records = _fetch('arm', _arm, f'arm:{_int(tmdb_id)}', _int(tmdb_id), _read(), new, now)
    finally:
        _save(new, now)
    return pick(records, tmdb_type, season) if records else None


def lookup(tmdb_type, tmdb_id, season=None, wait=WAIT):
    """Rating properties for a TMDb movie / show (season: the season or episode page's season number). {} when there is nothing
    or the answer is not ready within `wait` seconds (it is then saved for the next time)."""
    if tmdb_type not in ('movie', 'tv') or not _int(tmdb_id):
        return {}
    result = {}

    def _work():
        try:
            result.update(_properties(tmdb_type, _int(tmdb_id), season))
        except Exception as exc:
            kodi_log(f'Anime scores: lookup failed: {type(exc).__name__}: {exc}', 1)

    worker = threading.Thread(target=_work, daemon=True)
    worker.start()
    worker.join(wait)
    return dict(result) if not worker.is_alive() else {}
