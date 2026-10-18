"""Anime TV calendar rows from Simkl's CDN file (api/simkl/calendar_cdn.py), with their TMDb season and episode worked out.

Simkl numbers every anime cour from episode 1 ("E3" of a 12 episode cour) while TMDb files a franchise under one id with its own seasons,
so the number cannot be used as it is. A calendar knows something Next Up does not: the air date of every episode. Per show:

1. the show must be Japanese- or Chinese-language (donghua) animation on TMDb and its TMDb name must match a Simkl title (Simkl's
   TMDb ids are occasionally wrong), else the show is left out;
2. every row Simkl has for the show (about five weeks) is matched to the one TMDb episode (last three seasons) that aired on the same
   day where it is broadcast (Simkl's UTC time + 9 h for Japan; + 8 h or + 9 h for China, whichever places more rows: TMDb files the
   local broadcast date, and not every donghua is filed on China's day);
3. the matches must agree: TMDb position minus Simkl number has to be the same for all of them, or the odd row is not trusted.

A row that passes is shown with TMDb's season and episode (so the watched tick and the episode it opens are right). Any other row of
a show that passed 1 is still listed, as "E3" with no season or episode, and opens the show.
"""
import re
import datetime
import unicodedata

from tmdbhelper.lib.addon.logger import kodi_log

BROADCAST_HOURS = {'ja': (9,), 'zh': (8, 9)}   # original language -> hours ahead of UTC to try for the local broadcast day (TMDb files that day);
                                                # donghua is filed on China's day (+8) for some shows and on Japan's (+9) for others, so both are tried
SEASONS_CHECKED = 3        # the newest TMDb seasons searched for the air date
MIN_NAME_OVERLAP = 6       # characters two titles must share (one inside the other) to count as the same show
RETRY = 'retry'            # match_show's answer when TMDb returned nothing for the show


def _norm(text):
    return re.sub(r'[^a-z0-9]+', '', unicodedata.normalize('NFKD', str(text or '').lower()))


def _date(value):
    try:
        return datetime.date.fromisoformat(str(value)[:10])
    except (ValueError, TypeError):
        return None


def _is_anime(details):
    return details.get('original_language') in BROADCAST_HOURS and any(g.get('id') == 16 for g in details.get('genres') or [])


def same_show(details, rows):
    """Whether TMDb's names for the show match any title Simkl gave it."""
    mine = {_norm(details.get('name')), _norm(details.get('original_name'))} - {''}
    theirs = set()
    for row in rows[:1]:
        theirs = {_norm(t) for t in [row.get('show'), row.get('romaji')] + list(row.get('alt') or [])} - {''}
    if mine & theirs:
        return True
    return any(min(len(a), len(b)) >= MIN_NAME_OVERLAP and (a in b or b in a) for a in mine for b in theirs)


def _tmdb_episodes(tmdb_api, details):
    """[(season, episode, air date or None)] of the newest seasons, in TMDb order."""
    numbers = sorted((s.get('season_number') for s in details.get('seasons') or [] if (s.get('season_number') or 0) > 0), reverse=True)[:SEASONS_CHECKED]
    out = []
    for number in numbers:
        season = tmdb_api.get_request_sc('tv', details.get('id'), 'season', number) or {}
        out += [(number, e.get('episode_number'), _date(e.get('air_date'))) for e in season.get('episodes') or []]
    return sorted(out, key=lambda e: (e[0], e[1] or 0))


def match_show(tmdb_api, rows):
    """{row index: (season, episode)} for the rows of ONE show that can be placed in TMDb, or None when the show is not usable
    (not Japanese anime on TMDb, a different show, or TMDb has no data)."""
    details = tmdb_api.get_request_sc('tv', rows[0]['tmdb']) or {}
    if not details.get('id'):
        return RETRY   # TMDb gave nothing (a busy start-up, a rate limit): ask again before giving the show up
    if not _is_anime(details) or not same_show(details, rows):
        return None
    episodes = _tmdb_episodes(tmdb_api, details)
    position = {(e[0], e[1]): i for i, e in enumerate(episodes)}
    found = {}
    for hours in BROADCAST_HOURS[details.get('original_language')]:   # the shift that places the most rows wins
        attempt = {}
        for index, row in enumerate(rows):
            try:
                local = (datetime.datetime.fromisoformat(row['date'].replace('Z', '+00:00')) + datetime.timedelta(hours=hours)).date()
            except ValueError:
                continue
            hits = [e for e in episodes if e[2] == local]
            if len(hits) == 1:
                attempt[index] = (hits[0][0], hits[0][1], position[(hits[0][0], hits[0][1])] - int(row['episode']))
        if len(attempt) > len(found):
            found = attempt
    if not found:
        return {}
    offsets = {}
    for _, _, offset in found.values():
        offsets[offset] = offsets.get(offset, 0) + 1
    best = max(offsets, key=offsets.get)
    return {i: (s, e) for i, (s, e, offset) in found.items() if offset == best}


def episodes(tmdb_api, startdate, days, today=None, data=None):
    """Trakt-shaped calendar rows for the anime airing in the window (one day wider each side), or [] when there are none."""
    from tmdbhelper.lib.api.simkl.calendar_cdn import _load, window_rows
    data = data or _load('anime')
    if not data:
        return []
    wanted = window_rows(data, startdate, days, today)
    if not wanted:
        return []
    ids = list(dict.fromkeys(r['tmdb'] for r in wanted))
    by_show = {i: [r for r in data['rows'] if r['tmdb'] == i] for i in ids}

    def _one(tmdb_id):
        try:
            return match_show(tmdb_api, by_show[tmdb_id])
        except Exception as exc:
            kodi_log(f'Simkl anime calendar: show {tmdb_id} failed: {type(exc).__name__}: {exc}', 1)
            return None

    from tmdbhelper.lib.addon.thread import ParallelThread
    with ParallelThread(ids, _one) as pt:
        pass
    matches = dict(zip(ids, pt.queue))   # read after the block: leaving it is what waits for the threads
    failed = [i for i in ids if matches.get(i) == RETRY]
    for tmdb_id in failed:   # one more try, one at a time: the first cold call after a Kodi start lost most of its answers (and the result is cached for hours)
        matches[tmdb_id] = _one(tmdb_id)
    still = [i for i in ids if matches.get(i) == RETRY]
    for tmdb_id in still:
        matches[tmdb_id] = None
    if failed:
        kodi_log(f'Simkl anime calendar: TMDb gave nothing for {len(failed)} of {len(ids)} shows, {len(failed) - len(still)} found on the second try', 1)
    if len(still) * 2 > len(ids):   # TMDb is not answering: an empty result is not kept by the cache, so the next open asks again
        return []
    dropped = [by_show[i][0]['show'] for i in ids if matches.get(i) is None]
    if dropped:
        kodi_log(f'Simkl anime calendar: left out {len(dropped)} shows (not Japanese or Chinese animation on TMDb, or a different show): {", ".join(str(d)[:24] for d in dropped[:30])}', 1)
    out, mapped = [], 0
    for tmdb_id in ids:
        match = matches.get(tmdb_id)
        if match is None:
            continue
        for index, row in enumerate(by_show[tmdb_id]):
            if row not in wanted:
                continue
            placed = match.get(index)
            mapped += 1 if placed else 0
            episode = {'title': row['title'], 'runtime': row['runtime']}
            if placed:
                episode['season'], episode['number'] = placed
            else:
                episode['season'], episode['number'], episode['code'] = 0, 0, f'E{row["episode"]}'   # unplaced: shown by its Simkl number, opens the show
            if int(row['episode']) == 1:
                episode['episode_type'] = 'season_premiere'
            out.append({
                'first_aired': row['date'][:19] + '.000Z', 'episode': episode,
                'show': {'title': row['show'], 'year': int(row['year']) if row['year'].isdigit() else None,
                         'ids': {'tmdb': tmdb_id, 'simkl': row['simkl']}, 'genres': []}})
    kodi_log(f'Simkl anime calendar: {len(ids)} shows, {sum(1 for m in matches.values() if m is not None)} usable, {len(out)} episodes ({mapped} placed in TMDb)', 1)
    out.sort(key=lambda r: r['first_aired'])
    return out
