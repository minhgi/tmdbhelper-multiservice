# Which episode numbers mean the same episode on TMDb and on Simkl (7.5.48). Pure: no add-on imports. Seren carries the same file.
# Counting a season's episodes cannot see a shift: where Simkl splits a two-part episode (Star Trek: Voyager's pilot), every later number
# points at another episode while the two counts differ by only one. So each number is compared by its title and air date.
# Rows are {number: (title, 'YYYY-MM-DD...')}; the TMDb titles must be the English ones, Simkl's are.
import datetime as _dt
import re
import unicodedata

_GENERIC = re.compile(r'^(episode|ep|chapter|part|tba|tbd)?\d*$')   # "Episode 5", "12", "TBA": says nothing about which episode it is
_MARK = re.compile(r'\s*\((\d+|part \w+)\)\s*$')                    # a trailing part marker: "(2)", "(Part 2)"
_FIRST = re.compile(r'\s*\((1|part 1|part i|part one)\)\s*$')


def _text(title):
    return unicodedata.normalize('NFKD', str(title or '')).lower().replace('&', 'and')


def _plain(text):
    key = ''.join(c for c in text if c.isalnum())
    return '' if _GENERIC.match(key) else key


def _title_keys(rows):
    """{number: comparable title}. A part marker is dropped only while that leaves the title unlike every other one in the list:
    otherwise the two halves of a two-parter would look like the same episode."""
    full = {n: _text(v[0]) for n, v in rows.items()}
    cut = {n: _plain(_MARK.sub('', t)) for n, t in full.items()}
    seen = {}
    for key in cut.values():
        seen[key] = seen.get(key, 0) + 1
    return {n: cut[n] if seen[cut[n]] == 1 else _plain(full[n]) for n in rows}


def _index(values):
    """{value: set of numbers holding it}, empty values left out"""
    found = {}
    for number, value in values.items():
        if value:
            found.setdefault(value, set()).add(number)
    return found


def _day(value):
    try:
        return _dt.date.fromisoformat(str(value or '')[:10]).toordinal()
    except ValueError:
        return None


def same_numbers(tmdb, simkl):
    """The episode numbers (of one season) that are the same episode on both sides. A number on one side only is never one."""
    both = set(tmdb) & set(simkl)
    tt, st = _title_keys(tmdb), _title_keys(simkl)
    t_titles, s_titles = _index(tt), _index(st)
    td, sd = {n: _day(v[1]) for n, v in tmdb.items()}, {n: _day(v[1]) for n, v in simkl.items()}
    t_days, s_days = _index(td), _index(sd)

    def near(n):
        return td.get(n) is not None and sd.get(n) is not None and abs(td[n] - sd[n]) <= 1

    def alone(n):      # the dates match, and neither list shows another episode within a day of the other side's date
        if not near(n):
            return False
        return not any(days.get(d + step, set()) - {n} for days, d in ((t_days, sd[n]), (s_days, td[n])) for step in (-1, 0, 1))

    def titled_elsewhere(n):      # either title belongs to another number of the other list: the lists are shifted here
        if tt[n] == st[n]:
            return False
        return bool(s_titles.get(tt[n], set()) - {n}) or bool(t_titles.get(st[n], set()) - {n})

    same = {n for n in both if tt[n] and tt[n] == st[n]}
    # one side split an episode in two: its first half sits in the slot of the whole one
    for n in both - same:
        for whole_keys, whole, split in ((tt, tmdb, simkl), (st, simkl, tmdb)):
            part = _text(split[n][0])
            first = _FIRST.sub('', part)
            if whole_keys[n] and first != part and _plain(first) == whole_keys[n] and not _MARK.search(_text(whole[n][0])):
                same.add(n)
    shifted = {n for n in both - same if titled_elsewhere(n)}
    contradicted = bool(shifted) or any(tt[n] and st[n] and tt[n] != st[n] and not near(n) for n in both - same)
    aligned = set(tmdb) == set(simkl) and not contradicted      # same numbers on both sides and nothing speaks against them
    same |= {n for n in both - same - shifted if aligned or alone(n)}
    return same


def tmdb_rows(season):
    """rows from TMDb's tv/<id>/season/<n> answer"""
    return {int(e['episode_number']): (e.get('name'), e.get('air_date')) for e in season.get('episodes') or [] if e.get('episode_number') is not None}


def simkl_rows(episodes, season):
    """rows of one season from Simkl's tv/episodes/<id>?extended=full answer"""
    return {int(e['episode']): (e.get('title'), e.get('date')) for e in episodes
            if (e.get('type') or 'episode') == 'episode' and e.get('episode') is not None and e.get('season') is not None and int(e['season']) == int(season)}


def season_lists(tmdb_id, seasons, simkl_get, tmdb_get):
    """{season: (TMDb rows, Simkl rows)}, or None when a request failed. simkl_get(kind, value) -> the JSON list or None, kind 'search'
    (value = TMDb id) or 'episodes' (value = Simkl id). tmdb_get(season or None) -> the show's / the season's JSON, or None.
    A show Simkl does not know gives empty lists: nothing of it is the same episode."""
    details = tmdb_get(None)
    if not details or details.get('seasons') is None:
        return None
    found = simkl_get('search', int(tmdb_id))
    if not isinstance(found, list):      # null is not 'no such show': Simkl also answers null where it has no answer
        return None
    simkl_id = (((found[0] or {}).get('ids') or {}).get('simkl')) if found else None
    if not simkl_id:
        return {s: ({}, {}) for s in seasons}
    episodes = simkl_get('episodes', simkl_id)
    if not isinstance(episodes, list):
        return None
    on_tmdb = {int(x['season_number']) for x in details['seasons'] if x.get('season_number') is not None}
    lists = {}
    for season in seasons:
        theirs, ours = simkl_rows(episodes, season), {}
        if season in on_tmdb and theirs:
            answer = tmdb_get(season)
            if not answer or answer.get('episodes') is None:
                return None
            ours = tmdb_rows(answer)
        lists[season] = (ours, theirs)
    return lists
