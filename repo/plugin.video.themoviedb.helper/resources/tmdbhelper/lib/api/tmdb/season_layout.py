"""Seasons the way a viewer counts them, for a show whose TMDb seasons lump several seasons into one.

TMDb files some shows (The Apothecary Diaries, Jujutsu Kaisen, Frieren) as one long season 1 although the show aired in several
seasons / cours. TMDb also has community "episode groups" for many shows. Most groups are other orders (DVD, production, story
arcs, per streaming service), so a group is used ONLY when all of this holds:

* the show has exactly one regular TMDb season with MIN_EPISODES or more episodes (anything else already has seasons to show);
* the group is of type Production or TV (the types TMDb uses for season-style groups);
* the groups that hold regular episodes (not the season 0 specials) are two or more and together hold exactly the regular
  episodes of the show: every episode once, none missing, none extra.

Of several groups that fit, the partition most groups agree on wins, then Production before TV, then TMDb's own order. A show that
fits nothing keeps TMDb's seasons. Only what is DISPLAYED changes: every item keeps the real TMDb season and episode in its path, so
playing, watched marks, scrobbling and Next Up are untouched.
"""
import re

GROUP_TYPES = (6, 7)         # TMDb episode group types: 6 = Production, 7 = TV
MIN_EPISODES = 25            # a lone season at least this long may be several seasons
GENERIC_NAME = re.compile(r'^\s*(season|series|staffel|cour|part|s)?\s*0*(\d{1,2})\s*$', re.IGNORECASE)

CACHE_FILE = 'special://profile/addon_data/plugin.video.themoviedb.helper/season_layouts.json'
CACHE_TTL = 86400            # seconds a layout (or the answer 'none') is trusted before TMDb is asked again

_memo = {}
_file_cache = None


def _cache_path():
    try:
        import xbmcvfs
        return xbmcvfs.translatePath(CACHE_FILE)
    except ImportError:
        return None


def _cache_load():
    global _file_cache
    if _file_cache is None:
        _file_cache = {}
        path = _cache_path()
        try:
            import json
            with open(path, 'r', encoding='utf-8') as f:
                data = json.load(f)
            if isinstance(data, dict):
                _file_cache = data
        except Exception:
            pass
    return _file_cache


def _cache_save(tmdb_id, layout):
    """Remember a layout (None = the show keeps its seasons) for CACHE_TTL. Never raises; a lost write only costs a TMDb request."""
    import time
    try:
        import json
        cache = _cache_load()
        now = time.time()
        cache[str(tmdb_id)] = {'t': now, 'layout': layout}
        for key in [k for k, v in cache.items() if now - (v or {}).get('t', 0) > 7 * CACHE_TTL]:
            del cache[key]
        path = _cache_path()
        if not path:
            return
        import os
        tmp = f'{path}.{os.getpid()}.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(cache, f)
        os.replace(tmp, path)
    except Exception:
        return


ART_FILE = 'special://profile/addon_data/plugin.video.themoviedb.helper/season_layout_art.json'   # artwork chosen per layout season
_art_memo = (None, None)   # (file mtime, content)


def _art_path():
    try:
        import xbmcvfs
        return xbmcvfs.translatePath(ART_FILE)
    except ImportError:
        return None


def _art_read():
    """{tmdb_id: {layout season number: {artwork type: url}}}; {} when nothing is stored or the file cannot be read."""
    global _art_memo
    path = _art_path()
    if not path:
        return {}
    try:
        import json
        import os
        stamp = os.path.getmtime(path)
        if _art_memo[0] == stamp:
            return _art_memo[1]
        with open(path, 'r', encoding='utf-8') as f:
            data = json.load(f)
        data = data if isinstance(data, dict) else {}
        _art_memo = (stamp, data)
        return data
    except Exception:
        return {}


def layout_art(tmdb_id, number):
    """Artwork {type: url} chosen for one layout season of a show (TMDb has none for it), or {}."""
    try:
        art = ((_art_read().get(str(tmdb_id)) or {}).get(str(number)) or {})
        return {k: v for k, v in art.items() if v}
    except Exception:
        return {}


def set_layout_art(tmdb_id, number, art_type, url):
    """Store the artwork chosen for one layout season. True when written. The file is only ever rewritten whole, so a lost write
    costs a choice, never the file."""
    try:
        import json
        import os
        path = _art_path()
        if not path:
            return False
        data = json.loads(json.dumps(_art_read()))
        data.setdefault(str(tmdb_id), {}).setdefault(str(number), {})[art_type] = url
        tmp = f'{path}.{os.getpid()}.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(data, f)
        os.replace(tmp, path)
        return True
    except Exception:
        return False


def cached_layout(tmdb_id):
    """(True, layout or None) when a fresh answer for the show is on file, else (False, None). No network, for lists that must stay fast."""
    import time
    entry = _cache_load().get(str(tmdb_id))
    if not entry or time.time() - entry.get('t', 0) > CACHE_TTL:
        return False, None
    layout = entry.get('layout')
    if layout:
        layout = {'season': layout['season'], 'seasons': [
            {'number': s['number'], 'name': s['name'], 'episodes': [tuple(p) for p in s['episodes']]} for s in layout['seasons']]}
    return True, layout


def season_name(name, number):
    """'Season N' for a plain numbered group name or no name; the group's own name when it says more (e.g. a final season's title)."""
    name = (name or '').strip()
    if not name or GENERIC_NAME.match(name):
        return None  # caller uses the localised "Season N"
    return name


def _regular_total(show):
    """(season number, episode count) of the show's only regular season, or None when TMDb already has several regular seasons."""
    regular = [(s.get('season_number'), s.get('episode_count') or 0) for s in (show or {}).get('seasons') or []
               if (s.get('season_number') or 0) > 0]
    if len(regular) != 1 or regular[0][1] < MIN_EPISODES:
        return None
    return regular[0]


def _partition(group_detail, season, total):
    """[[(season, episode), ...] per regular group, in the group's order] when the group's regular groups hold exactly the show's
    `total` regular episodes of `season`, else None."""
    regular, seen = [], set()
    for group in (group_detail or {}).get('groups') or []:
        episodes = sorted(group.get('episodes') or [], key=lambda e: e.get('order', 0))
        pairs = [(e.get('season_number'), e.get('episode_number')) for e in episodes]
        if not pairs:
            continue  # an empty group (a season announced but not yet filled)
        if all(s == 0 for s, _ in pairs):
            continue  # specials: TMDb season 0 stays as it is
        if any(s != season or not e or e < 1 or e > total for s, e in pairs):
            return None  # mixes specials in, or points outside the show's regular episodes
        if any(p in seen for p in pairs) or len(set(pairs)) != len(pairs):
            return None
        seen.update(pairs)
        regular.append((group.get('name'), pairs))
    if len(regular) < 2 or len(seen) != total:
        return None
    return regular


def choose_layout(show, groups_list, group_details):
    """The layout for a show, or None. `groups_list` = TMDb tv/{id}/episode_groups answer, `group_details` = {group id: answer of
    tv/episode_group/{id}}. Layout: {'season': real season, 'seasons': [{'number': n, 'name': str or None, 'episodes': [(s, e), ...]}]}."""
    lone = _regular_total(show)
    if not lone:
        return None
    season, total = lone
    candidates = []
    for position, group in enumerate((groups_list or {}).get('results') or []):
        if group.get('type') not in GROUP_TYPES:
            continue
        partition = _partition(group_details.get(group.get('id')), season, total)
        if partition:
            candidates.append((position, group.get('type'), partition))
    if not candidates:
        return None
    key = lambda c: tuple(tuple(p) for _, p in c[2])  # noqa: E731 - the episode split, names ignored
    votes = {}
    for c in candidates:
        votes[key(c)] = votes.get(key(c), 0) + 1
    foreign = lambda c: sum(1 for name, _ in c[2] if name and not name.isascii())  # noqa: E731 - names in another script rank last
    best = sorted(candidates, key=lambda c: (-votes[key(c)], foreign(c), 0 if c[1] == 6 else 1, c[0]))[0]
    return {
        'season': season,
        'seasons': [{'number': n, 'name': season_name(name, n), 'episodes': pairs}
                    for n, (name, pairs) in enumerate(best[2], 1)]}


def get_layout(tmdb_api, tmdb_id, show=None):
    """choose_layout for a TMDb show id using the cached TMDb requests; None when the show keeps its seasons. Never raises."""
    key = str(tmdb_id)
    if key in _memo:
        return _memo[key]
    known, layout = cached_layout(tmdb_id)
    if known:
        _memo[key] = layout
        return layout
    layout = None
    try:
        show = show or tmdb_api.get_request_sc(f'tv/{tmdb_id}')
        if _regular_total(show):
            groups_list = tmdb_api.get_request_sc(f'tv/{tmdb_id}/episode_groups') or {}
            details = {}
            for group in groups_list.get('results') or []:
                if group.get('type') in GROUP_TYPES and (group.get('group_count') or 0) >= 2:
                    details[group.get('id')] = tmdb_api.get_request_sc(f'tv/episode_group/{group["id"]}')
            layout = choose_layout(show, groups_list, details)
    except Exception as exc:
        from tmdbhelper.lib.addon.logger import kodi_log
        kodi_log(f'Season layout: {type(exc).__name__} for {tmdb_id}', 1)
        layout = None
    _memo[key] = layout
    _cache_save(tmdb_id, layout)
    return layout


def is_enabled():
    try:
        from tmdbhelper.lib.addon.plugin import get_setting
        return bool(get_setting('season_layout'))
    except Exception:
        return False


def virtual_season_items(items, layout):
    """`items` (season item dicts of get_season_list) with the show's lone regular season replaced by one item per layout season.
    Each copy keeps the real season (the item is still BUILT from TMDb's season) and carries `lseason` in its path params; the
    displayed season number, name and episode count are set when the list item is finished (see ListSeasons)."""
    import copy
    out = []
    for item in items:
        if (item.get('infolabels') or {}).get('season') != layout['season']:
            out.append(item)
            continue
        for season in layout['seasons']:
            virtual = copy.deepcopy(item)
            virtual.setdefault('params', {})['lseason'] = str(season['number'])
            out.append(virtual)
    return out


def layout_episode_items(tmdb_api, tmdb_id, layout_season):
    """Episode item dicts of one layout season, in the layout's order, each still carrying its real TMDb season and episode."""
    wanted = layout_season['episodes']
    by_pair = {}
    for real_season in sorted({s for s, _ in wanted}):
        for item in tmdb_api.get_episode_list(tmdb_id, real_season, get_detailed=True) or []:
            by_pair[(real_season, item['infolabels'].get('episode'))] = item
    return [by_pair[pair] for pair in wanted if pair in by_pair]


def find_season(layout, number):
    try:
        number = int(number)
    except (TypeError, ValueError):
        return None
    for season in (layout or {}).get('seasons') or []:
        if season['number'] == number:
            return season
    return None


def remap_params(params, keep_layout_season=False):
    """Turn a request that carries LAYOUT season/episode numbers (a skin reading ListItem.Season / ListItem.Episode, such as Bingie's
    Refresh details / Select artwork buttons or its episode-info paths) back into the real TMDb numbers, in place. Items made by this
    add-on already carry the real numbers (or `lseason`), so they are left alone. Never raises; returns `params`."""
    try:
        if params.get('lseason') or not is_enabled():
            return params
        if (params.get('tmdb_type') or params.get('type')) != 'tv' or not params.get('tmdb_id'):
            return params
        try:
            season = int(params.get('season'))
        except (TypeError, ValueError):
            return params
        if season < 2:
            return params
        known, layout = cached_layout(params['tmdb_id'])
        if not known:
            from tmdbhelper.lib.api.tmdb.api import TMDb
            layout = get_layout(TMDb(), params['tmdb_id'])
        entry = find_season(layout, season)
        if not entry or season == layout['season']:
            return params
        try:
            episode = int(params.get('episode'))
        except (TypeError, ValueError):
            episode = None
        if episode is not None:
            if not 1 <= episode <= len(entry['episodes']):
                return params
            real_season, real_episode = entry['episodes'][episode - 1]
            params['season'], params['episode'] = str(real_season), str(real_episode)
        else:
            params['season'] = str(layout['season'])
            if params.get('info') == 'episodes':
                params['lseason'] = str(season)
            if keep_layout_season:
                params['layout_season'] = str(season)   # scripts (Select artwork) still need to know which layout season was meant
        from tmdbhelper.lib.addon.logger import kodi_log
        kodi_log(f'Season layout: S{season}' + (f'E{episode}' if episode is not None else '')
                 + f' -> S{params["season"]}' + (f'E{params["episode"]}' if episode is not None else ''), 1)
    except Exception as exc:
        try:
            from tmdbhelper.lib.addon.logger import kodi_log
            kodi_log(f'Season layout remap: {type(exc).__name__}', 1)
        except Exception:
            pass
    return params


def script_layout_season(tmdb_type, tmdb_id, season, kwargs):
    """The layout season a season-level script call (Select artwork) means, as a string, or None for a real TMDb season.
    Explicit when the call carries `layout_season` (a skin's layout season >= 2, see remap_params) or `lseason` (the add-on's own
    context menu on a layout season item). Otherwise a skin's season 1 of a show that has a layout (and the setting on) is the
    layout's first season, whose artwork is kept apart from real season 1's, the fallback of every layout season without its own."""
    try:
        explicit = str(kwargs.get('layout_season') or kwargs.get('lseason') or '')
        if explicit.isdigit():
            return explicit
        if tmdb_type != 'tv' or season in (None, '') or not tmdb_id or not is_enabled():
            return None
        known, layout = cached_layout(tmdb_id)
        if layout and str(season) == str(layout['season']):
            return str(season)
    except Exception:
        pass
    return None


def episode_position(layout, real_season, real_episode):
    """(layout season number, position in it) of a real TMDb episode, or None."""
    try:
        target = (int(real_season), int(real_episode))
    except (TypeError, ValueError):
        return None
    for season in (layout or {}).get('seasons') or []:
        for position, pair in enumerate(season['episodes'], 1):
            if pair == target:
                return season['number'], position
    return None
