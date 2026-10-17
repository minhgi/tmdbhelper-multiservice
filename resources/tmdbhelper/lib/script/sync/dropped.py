from tmdbhelper.lib.addon.plugin import get_localized, executebuiltin, PLUGINPATH, ADDONPATH
from tmdbhelper.lib.addon.dialog import BusyDialog
from tmdbhelper.lib.script.sync.item import ItemSync
from xbmcgui import Dialog

""" Dropped shows (Seren backport items N4 / C1).

Trakt:   POST /users/hidden/dropped (drop) and /users/hidden/dropped/remove (restore); list GET /users/hidden/dropped?type=show.
MDBList: POST /sync/dropped (drop) and /sync/dropped/remove; list GET /sync/dropped (any known show can be dropped; not_found = unknown id).
Dropping is one-way from a show's menu (like Seren): restoring is done in bulk from the Dropped Shows list.
The bulk pickers always read the server live (never a cached list).
"""

PAGE_LIMIT = 1000
MAX_PAGES = 50

# Header items shown at the top of a list: (info route, label string, icon)
HEADER_TRAKT_RESTORE = ('trakt_dropped_restore', 32575, 'trakt/watchlist.png')
HEADER_TRAKT_DROP = ('trakt_watchlist_drop', 32576, 'trakt/watchlist.png')
HEADER_MDBLIST_RESTORE = ('mdblist_dropped_restore', 32575, 'mdblist/dropped.png')


def count_of(value):
    """MDBList/Trakt report not_found / updated either as a number or as an itemized list: both mean "how many"."""
    if isinstance(value, bool):
        return 0
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, (list, dict)):
        return len(value)
    return 0


def get_header_item(header):
    """List item for a header action (an info route handled by items/router.py, not a folder)."""
    info, localized, icon = header
    return {
        'label': get_localized(localized),
        'path': PLUGINPATH,
        'params': {'info': info},
        'is_folder': False,
        'infoproperties': {'specialsort': 'top'},
        'art': {
            'landscape': f'{ADDONPATH}/fanart.jpg',
            'icon': f'{ADDONPATH}/resources/icons/{icon}'}}


def with_header(items, header, is_widget, page):
    """Put the header action first on page 1 of a normal (non-widget) listing."""
    if not items or is_widget:
        return items
    try:
        if int(page or 1) != 1:
            return items
    except (TypeError, ValueError):
        return items
    return [get_header_item(header)] + list(items)


def refresh_containers():
    from tmdbhelper.lib.script.method.kodi_utils import reload_widgets
    executebuiltin('Container.Refresh')
    reload_widgets()


def resync_hidden(api):
    """Refresh the local hidden-shows data (it feeds In Progress / Next Up etc.). Trakt does not move the hidden_at activity stamp for a
    drop or restore, so it has to be forced after one of our own changes (changes made elsewhere show up on the next hidden_at change or
    via Refresh Trakt Sync > Hidden)."""
    try:
        from tmdbhelper.lib.api.trakt.sync.datasync import SyncData
        SyncData(api).sync('show', ('hidden_at', ), forced=True)
    except Exception as exc:
        from tmdbhelper.lib.addon.logger import kodi_log
        kodi_log(f'Dropped shows: hidden resync failed - {exc}', 1)


def label_of(show):
    title = show.get('title') or ''
    year = show.get('year')
    return f'{title} ({year})' if year else title


# ---------------------------------------------------------------- Trakt

def trakt_authorized(api):
    if api.authorization:
        return True
    Dialog().ok('Trakt', get_localized(32586))
    return False


def trakt_get_pages(api, *path, **params):
    """Every entry of a paged Trakt list endpoint, or None when any page failed (a failed read is never an empty list)."""
    entries = []
    for page in range(1, MAX_PAGES + 1):
        response = api.get_response(*path, page=page, limit=PAGE_LIMIT, **params)
        if response is None:
            return None
        try:
            data = response.json()
        except (ValueError, AttributeError):
            return None
        if not isinstance(data, list):
            return None
        entries += data
        try:
            pages = int(response.headers.get('x-pagination-page-count') or 1)
        except (TypeError, ValueError, AttributeError):
            pages = 1
        if page >= pages:
            return entries
    return None


def trakt_show_choices(entries):
    """[(trakt id, label)] from entries shaped {"show": {"title", "year", "ids": {"trakt"}}}"""
    choices = []
    for entry in entries:
        show = (entry or {}).get('show') or {}
        trakt_id = (show.get('ids') or {}).get('trakt')
        if trakt_id:
            choices.append((trakt_id, label_of(show)))
    return choices


def trakt_post_shows(api, path, trakt_ids):
    """POST {"shows": [{"ids": {"trakt": id}}]} and report success (2xx and nothing in not_found)."""
    with BusyDialog():
        response = api.post_response(*path, postdata={'shows': [{'ids': {'trakt': i}} for i in trakt_ids]})
    if response is None or response.status_code not in (200, 201, 204):
        return False
    try:
        not_found = (response.json() or {}).get('not_found') or {}
    except (ValueError, AttributeError):
        return True
    return count_of(not_found.get('shows')) == 0


def pick_and_post(api, choices, heading, path, done_string):
    picked = Dialog().multiselect(get_localized(heading), [c[1] for c in choices])
    if not picked:
        return
    ids = [choices[i][0] for i in picked]
    if not trakt_post_shows(api, path, ids):
        Dialog().ok('Trakt', get_localized(32585))
        return
    Dialog().notification('TMDbHelper', get_localized(done_string).format(len(ids)))
    with BusyDialog():
        resync_hidden(api)
    refresh_containers()


def trakt_restore_dropped():
    from tmdbhelper.lib.api.trakt.api import TraktAPI
    api = TraktAPI()
    if not trakt_authorized(api):
        return
    with BusyDialog():
        entries = trakt_get_pages(api, 'users', 'hidden', 'dropped', type='show')
    if entries is None:
        Dialog().ok('Trakt', get_localized(32585))
        return
    choices = trakt_show_choices(entries)
    if not choices:
        Dialog().ok('Trakt', get_localized(32581))
        return
    pick_and_post(api, choices, 32575, ('users', 'hidden', 'dropped', 'remove'), 32583)


def trakt_drop_from_watchlist():
    from tmdbhelper.lib.api.trakt.api import TraktAPI
    api = TraktAPI()
    if not trakt_authorized(api):
        return
    with BusyDialog():
        entries = trakt_get_pages(api, 'users', 'me', 'watchlist', 'shows')
    if entries is None:
        Dialog().ok('Trakt', get_localized(32585))
        return
    choices = trakt_show_choices(entries)
    # a drop does not take the show off the watchlist, so leave out what is already dropped (if that read fails the list is just unfiltered)
    with BusyDialog():
        dropped = trakt_get_pages(api, 'users', 'hidden', 'dropped', type='show')
    already = {c[0] for c in trakt_show_choices(dropped or [])}
    choices = [c for c in choices if c[0] not in already]
    if not choices:
        Dialog().ok('Trakt', get_localized(32582))
        return
    pick_and_post(api, choices, 32576, ('users', 'hidden', 'dropped'), 32584)


class ItemTraktDrop(ItemSync):
    """Drop a show on Trakt (one-way; restore from the Dropped Shows list). Show level only."""
    preconfigured = True
    localized_name = 32577

    def get_self(self):
        if not super().get_self():
            return None
        if self.trakt_type != 'show':
            return None
        return self

    def get_post_response_args(self):
        return ('users', 'hidden', 'dropped')

    def get_post_response_data(self):
        return {'shows': [{'ids': {'tmdb': int(self.tmdb_id)}}]}

    def get_is_successful_sync(self):
        if not super().get_is_successful_sync():
            return False
        try:
            not_found = (self.sync_response.json() or {}).get('not_found') or {}
        except (ValueError, AttributeError):
            return True
        return count_of(not_found.get('shows')) == 0

    def get_dialog_message(self):
        if self.sync_response is not None and self.sync_response.status_code in (200, 201, 204) and not self.is_successful_sync:
            return f'{get_localized(32296)}\n{get_localized(32580)}'.format(self.dialog_header, self.tmdb_type, 'TMDb', self.item_id)
        return super().get_dialog_message()

    def sync(self):
        if self.is_successful_sync:
            with BusyDialog():
                resync_hidden(self.trakt_api)
        self.display_dialog()
        self.refresh_containers()


# ---------------------------------------------------------------- MDBList

def mdblist_get_dropped(mdblist_api):
    """Every dropped show as [(tmdb id, label)], or None when any page failed. Pages by has_more, advancing by items actually served."""
    from tmdbhelper.lib.api.mdblist.lists import _get_user_endpoint
    choices = []
    offset = 0
    for _ in range(MAX_PAGES):
        page = _get_user_endpoint(mdblist_api, 'sync', 'dropped', offset=offset, limit=5000)
        if not isinstance(page, dict):
            return None
        served = 0
        for entry in page.get('shows') or []:
            served += 1
            show = (entry or {}).get('show') or {}
            tmdb_id = (show.get('ids') or {}).get('tmdb')
            if tmdb_id:
                choices.append((tmdb_id, label_of(show)))
        if not (page.get('pagination') or {}).get('has_more'):
            return choices
        if not served:
            return None
        offset += served
    return None


def mdblist_restore_dropped():
    from tmdbhelper.lib.api.mdblist.api import MDbList
    api = MDbList()
    if not (api.user_token.value or api.api_key):
        Dialog().ok('MDBList', get_localized(32586))
        return
    with BusyDialog():
        choices = mdblist_get_dropped(api)
    if choices is None:
        Dialog().ok('MDBList', get_localized(32585))
        return
    if not choices:
        Dialog().ok('MDBList', get_localized(32581))
        return
    picked = Dialog().multiselect(get_localized(32575), [c[1] for c in choices])
    if not picked:
        return
    ids = [choices[i][0] for i in picked]
    with BusyDialog():
        response = api.get_api_request(
            api.get_request_url('sync', 'dropped', 'remove'),
            postdata={'shows': [{'ids': {'tmdb': int(i)}} for i in ids]},
            method='json', headers=api.auth_headers)
    if response is None:
        Dialog().ok('MDBList', get_localized(32585))
        return
    Dialog().notification('TMDbHelper', get_localized(32583).format(len(ids)))
    refresh_containers()


# Router: info routes that run an action instead of building a directory (items/router.py)
ACTIONS = {
    'trakt_dropped_restore': trakt_restore_dropped,
    'trakt_watchlist_drop': trakt_drop_from_watchlist,
    'mdblist_dropped_restore': mdblist_restore_dropped,
}
