"""AniList id -> TMDb id table for the Anime lists, kept on disk.

Order for each AniList entry: the saved table, then the offline Fribb index (api/anilist/fribb.py, no network), then the
ARM service (arm.haglund.dev, one batch request) for the entries the index has no TMDb id for, then the caller's own title
search. An id from Fribb or ARM is only used after TMDb itself confirms it: the id has to exist on TMDb, be
Japanese-language, and (when it is not the kind AniList's format implies, TV vs movie) have a matching year. Fribb names the
kind of each id itself (its `tv` / `movie` key), so only that kind is tried; ARM's `media` field is no type check, so for
ARM ids both kinds are tried. Anything the sources or the check do not settle falls back to the title search, so with no
index and ARM down the lists behave as before.

Entries are saved by AniList id: Fribb and ARM answers 90 days, title-search answers 14 days (so a later Fribb/ARM answer
can replace them). "Not found" is never saved (a failed TMDb request looks the same, and would hide a show for days). The
TMDb season (and, from Fribb, the episode offset) is saved with the entry (not shown anywhere yet). Several lists can
build at once (separate plugin processes), so a save merges into whatever is on disk instead of overwriting it.
"""
import json
import os
import threading
import time

from tmdbhelper.lib.addon.logger import kodi_log

ARM_URL = 'https://arm.haglund.dev/api/v2/ids'
ARM_BATCH = 100                  # the service accepts at most 100 ids per request
ARM_TIMEOUT = 10
CACHE_FILE = 'special://profile/addon_data/plugin.video.themoviedb.helper/anime_idmap_cache.json'
TTL = {'fribb': 90 * 24 * 3600, 'arm': 90 * 24 * 3600, 'title': 14 * 24 * 3600}
YEAR_TOLERANCE = 1               # AniList start year vs TMDb release year, when the id only exists as the other kind


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


def _save(new, now):
    """Merge new entries into the table on disk (re-read just before writing; the newer entry per id wins), drop expired ones."""
    path = _cache_path()
    if not path:
        return
    table = _read()
    for key, entry in new.items():
        if _float((table.get(key) or {}).get('ts')) <= entry['ts']:
            table[key] = entry
    keep = {k: v for k, v in table.items() if isinstance(v, dict) and now - _float(v.get('ts')) < TTL.get(v.get('k'), 0)}
    tmp = f'{path}.{os.getpid()}.{threading.get_ident()}.tmp'
    try:
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(keep, f)
        os.replace(tmp, path)
    except OSError as exc:
        kodi_log(f'Anime ID map: cache write failed: {type(exc).__name__}', 1)
        try:   # on Windows os.replace fails while another process has the table open: do not leave the temp file behind
            os.remove(tmp)
        except OSError:
            pass


def _float(value, default=0.0):
    try:
        return float(value)
    except (ValueError, TypeError):
        return default


def _first_int(value):
    """ARM gives one id, but be safe with a list or 'a,b' string: the first whole number found, else None."""
    if isinstance(value, (list, tuple)):
        value = value[0] if value else None
    try:
        return int(str(value).split(',')[0])
    except (ValueError, TypeError):
        return None


def _saved(table, anilist_id, now):
    """The still-valid saved answer for an AniList id: (found, (mediatype, tmdb id) or None)."""
    entry = table.get(str(anilist_id))
    if not isinstance(entry, dict) or now - _float(entry.get('ts')) >= TTL.get(entry.get('k'), 0):
        return False, None
    try:
        return True, (entry['t'], int(entry['id']))
    except (KeyError, ValueError, TypeError):
        return False, None


def _arm_lookup(ids):
    """({anilist id: ARM record}, reachable) for the ids, 100 per request. A request that fails makes reachable False."""
    import requests
    found, reachable = {}, True
    for start in range(0, len(ids), ARM_BATCH):
        chunk = ids[start:start + ARM_BATCH]
        try:
            response = requests.post(
                ARM_URL, timeout=ARM_TIMEOUT, headers={'User-Agent': 'TMDbHelper'}, json=[{'anilist': i} for i in chunk])
            data = response.json() if response.status_code == 200 else None
        except Exception as exc:
            kodi_log(f'Anime ID map: ARM request failed: {type(exc).__name__}', 1)
            return found, False
        if not isinstance(data, list) or len(data) != len(chunk):
            kodi_log(f'Anime ID map: ARM answered HTTP {getattr(response, "status_code", "?")} / unexpected body', 1)
            reachable = False
            continue
        found.update({i: rec for i, rec in zip(chunk, data) if isinstance(rec, dict)})
    return found, reachable


def _year(details):
    return str(details.get('first_air_date') or details.get('release_date') or '')[:4]


def _confirm(tmdb_api, media, record):
    """((mediatype, tmdb id), season) when TMDb confirms ARM's id for this AniList entry, else None."""
    tmdb_id = _first_int(record.get('themoviedb'))
    if not tmdb_id:
        return None
    movie_first = media.get('format') == 'MOVIE' or record.get('media') == 'MOVIE'
    for position, tmdb_type in enumerate(('movie', 'tv') if movie_first else ('tv', 'movie')):
        details = tmdb_api.get_details_request(tmdb_type, tmdb_id)
        if not details or _first_int(details.get('id')) != tmdb_id:
            continue   # not an id of this kind: try the other kind
        if details.get('original_language') != 'ja':
            return None
        if position:   # only exists as the other kind: the same number could be an unrelated title, so the year has to agree too
            try:
                if abs(int(_year(details)) - int((media.get('startDate') or {}).get('year'))) > YEAR_TOLERANCE:
                    return None
            except (ValueError, TypeError):
                return None
        season = record.get('themoviedb-season')
        return ('movie' if tmdb_type == 'movie' else 'show', tmdb_id), (season if isinstance(season, int) else None)
    return None


def _confirm_fribb(tmdb_api, media, record):
    """((mediatype, tmdb id), season, offset) when TMDb confirms a Fribb id for this AniList entry, else None.

    Only the kind Fribb names for the id is tried (never the other number space). A kind that is not the one AniList's
    format implies (a film filed under a show id, or an OVA under a movie id) needs the year to agree as well."""
    kind = record['kind']
    movie_format = media.get('format') == 'MOVIE'
    for tmdb_id in record['ids']:
        details = tmdb_api.get_details_request(kind, tmdb_id)
        if not details or _first_int(details.get('id')) != tmdb_id or details.get('original_language') != 'ja':
            continue
        if (kind == 'movie') != movie_format:
            try:
                if abs(int(_year(details)) - int((media.get('startDate') or {}).get('year'))) > YEAR_TOLERANCE:
                    continue
            except (ValueError, TypeError):
                continue
        tv = kind == 'tv'
        return ('show' if tv else 'movie', tmdb_id), (record.get('season') if tv else None), (record.get('offset') if tv else None)
    return None


def resolve(tmdb_api, media_list, title_search):
    """TMDb match for each AniList entry of media_list, same order: (mediatype, tmdb id) or None.

    title_search(media) is the caller's own lookup, used for what the table, the Fribb index and ARM do not settle."""
    from tmdbhelper.lib.addon.thread import ParallelThread
    now = time.time()
    table = _read()
    results = [None] * len(media_list)
    todo = []
    for index, media in enumerate(media_list):
        found, match = _saved(table, media.get('id'), now)
        if found:
            results[index] = match
        else:
            todo.append(index)
    if not todo:
        kodi_log(f'Anime ID map: {len(media_list)} entries, all from the saved table', 1)
        return results

    try:
        from tmdbhelper.lib.api.anilist.fribb import records
        known = records([media_list[i].get('id') for i in todo])   # offline index; empty until the service has built it
    except Exception as exc:
        kodi_log(f'Anime ID map: Fribb index unusable: {type(exc).__name__}', 1)
        known = {}
    sources, fresh, seasons, offsets = {}, {}, {}, {}
    with_fribb = [i for i in todo if media_list[i].get('id') in known]
    with ParallelThread(with_fribb, lambda i: _confirm_fribb(tmdb_api, media_list[i], known[media_list[i].get('id')])) as pt:
        confirmed = pt.queue
    for index, outcome in zip(with_fribb, confirmed):
        if outcome:
            results[index], seasons[index], offsets[index] = outcome
            sources[index] = 'fribb'

    # ARM is built from the same file (refreshed daily, the index weekly): ask it only about what the index has no TMDb id for
    ask_arm = list(dict.fromkeys(media_list[i].get('id') for i in todo if media_list[i].get('id') not in known))
    arm, reachable = _arm_lookup(ask_arm) if ask_arm else ({}, True)
    with_arm = [i for i in todo if media_list[i].get('id') in arm]
    with ParallelThread(with_arm, lambda i: _confirm(tmdb_api, media_list[i], arm[media_list[i].get('id')])) as pt:
        confirmed = pt.queue
    for index, outcome in zip(with_arm, confirmed):
        if outcome:
            results[index], seasons[index] = outcome
            sources[index] = 'arm'

    rest = [i for i in todo if results[i] is None]
    with ParallelThread(rest, lambda i: title_search(media_list[i])) as pt:
        searched = pt.queue
    for index, match in zip(rest, searched):
        if match:
            results[index] = match
            sources[index] = 'title'

    for index in todo:
        if results[index]:
            entry = {'k': sources[index], 't': results[index][0], 'id': results[index][1], 'ts': now}
            if seasons.get(index) is not None:
                entry['s'] = seasons[index]
            if offsets.get(index) is not None:
                entry['o'] = offsets[index]
            fresh[str(media_list[index].get('id'))] = entry
    if fresh:
        _save(fresh, now)
    kodi_log(
        f'Anime ID map: {len(media_list)} entries: {len(media_list) - len(todo)} saved, '
        f'{sum(1 for s in sources.values() if s == "fribb")} Fribb, {sum(1 for s in sources.values() if s == "arm")} ARM, '
        f'{sum(1 for s in sources.values() if s == "title")} title search, '
        f'{sum(1 for i in todo if results[i] is None)} not found{"" if reachable else " (ARM unreachable)"}', 1)
    return results
