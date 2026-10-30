"""Global list sort order: one setting (list_global_sort) that orders the account lists of Trakt, MDBList and Simkl.

'service' (the default) changes nothing: no lookups, no reordering. Anything else sorts the rows of a list before it is
paged. Movie and show rows only for release / date added; a row without the date (an episode row, a row that carries no
added-date, a title TMDb has no date for) keeps its place at the end of the list, in the order the service gave.
"""
from tmdbhelper.lib.addon.plugin import get_setting

SETTING = 'list_global_sort'

# setting value: (field, direction)
_MODES = {
    'released_desc': ('released', 'desc'),
    'released_asc': ('released', 'asc'),
    'added_desc': ('added', 'desc'),
    'added_asc': ('added', 'asc'),
    'title_asc': ('title', 'asc'),
    'title_desc': ('title', 'desc'),
}


def get_global_sort():
    """(field, direction) for the chosen sort, or None for service order."""
    try:
        return _MODES.get(get_setting(SETTING, 'str'))
    except Exception:
        return None


def get_settings_salt():
    """Widget cache salt: empty for service order, so caches built before the setting existed stay valid."""
    try:
        value = get_setting(SETTING, 'str')
    except Exception:
        return ''
    return value if value in _MODES else ''


def _title_key(row):
    import re
    title = row.get('title') or ''
    if get_setting('trakt_sortignorearticle'):
        title = re.sub(r'(?i)^The ', '', title)
    return title.casefold() or None


def _added_key(row):
    if row.get('mediatype') not in ('movie', 'show'):
        return None
    return row.get('added_at') or None


def _tmdb_date(tmdb_api, row):
    """Release date of a movie, first air date of a show (TMDb details, short cache), or the year when TMDb has no date."""
    tmdb_type = 'movie' if row.get('mediatype') == 'movie' else 'tv'
    try:
        details = tmdb_api.get_request_sc(tmdb_type, row['id']) or {}
    except Exception:
        details = {}
    date = details.get('release_date') if tmdb_type == 'movie' else details.get('first_air_date')
    if date:
        return date[:10]
    year = row.get('release_year')
    return f'{year}-01-01' if year else None


def _released_keys(rows):
    from tmdbhelper.lib.api.tmdb.api import TMDb
    from tmdbhelper.lib.addon.thread import ParallelThread
    tmdb_api = TMDb()
    todo = [i for i, row in enumerate(rows) if row.get('mediatype') in ('movie', 'show') and row.get('id')]
    keys = [None] * len(rows)
    if not todo:
        return keys
    with ParallelThread(todo, lambda i: _tmdb_date(tmdb_api, rows[i])) as pt:
        fetched = pt.queue
    for i, key in zip(todo, fetched):
        keys[i] = key
    return keys


def sort_rows(rows, sort=None):
    """Rows ordered as the global sort says; rows is returned as it came for service order."""
    sort = sort or get_global_sort()
    if not sort or not rows:
        return rows
    field, how = sort
    if field == 'released':
        keys = _released_keys(rows)
    elif field == 'added':
        keys = [_added_key(row) for row in rows]
    else:
        keys = [_title_key(row) for row in rows]
    dated = [(k, row) for k, row in zip(keys, rows) if k]
    if not dated:
        return rows
    undated = [row for k, row in zip(keys, rows) if not k]
    dated.sort(key=lambda kr: kr[0], reverse=(how == 'desc'))  # stable in both directions
    return [row for _, row in dated] + undated


def trakt_sort_args(sync_type=None, item_type=None):
    """(sort_by, sort_how) for a Trakt sync list from the global sort, or (None, None) when it does not apply.
    Episode lists (On Deck, Next Up) keep their own order: Trakt's release date is the show's premiere, and its title the
    episode's name. Date added exists for the watchlist and favourites only."""
    sort = get_global_sort()
    if not sort or item_type == 'episode':
        return None, None
    field, how = sort
    if field == 'released':
        return 'released', how
    if field == 'added':
        return ('added', how) if sync_type in ('watchlist', 'favorites') else (None, None)
    return 'title', how
