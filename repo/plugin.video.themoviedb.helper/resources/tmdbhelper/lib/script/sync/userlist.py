from tmdbhelper.lib.files.ftools import cached_property
from tmdbhelper.lib.script.sync.item import ItemSync
from tmdbhelper.lib.script.sync.mdblist_sync import ItemMDbListSyncBase
from tmdbhelper.lib.addon.plugin import get_infolabel, get_localized, executebuiltin
from tmdbhelper.lib.addon.dialog import BusyDialog
from xbmcgui import Dialog


class ItemMDbList(ItemMDbListSyncBase):
    """ Add an item to / remove it from one of the user's static MDBList lists.

    Built on the MDBList sync base (own authorisation gate, no Trakt lookups). Reads the list id from the container
    properties when opened inside an own static list ('Remove'), otherwise asks - with a 'new list' entry so the
    picker is useful before any static list exists. """
    preconfigured = True

    @cached_property
    def remove(self):
        return self.get_remove()

    def get_remove(self):
        if get_infolabel("ListItem.Property(param.info)") != 'mdblist_userlist':
            return False
        if get_infolabel("ListItem.Property(param.dynamic)"):
            return False
        if get_infolabel("ListItem.Property(param.mdblist_owner)") != 'true':  # Only the user's own lists ('Your lists') can be edited
            return False
        return True

    def get_name(self):
        return get_localized(32519) if self.remove else get_localized(32514)

    @cached_property
    def list_id(self):
        return self.get_list_id()

    def get_list_id(self):
        if self.remove:
            return get_infolabel("ListItem.Property(param.list_id)")
        with BusyDialog():
            lists = self.mdblist_api.get_static_lists()
        if lists is None:  # No API credentials
            Dialog().ok('MDbList', f'{get_localized(32516)}\n{get_localized(32517)}')
            return
        names = [i.get('name', '') for i in lists] + [get_localized(32299)]
        x = Dialog().select(get_localized(32133), names)
        if x == -1:
            return
        if x == len(lists):
            return create_mdblist_list(self.mdblist_api)
        return lists[x].get('id')

    def get_sync_response(self):
        if not self.list_id:
            return
        return self.call_api('lists', self.list_id, 'items', 'remove' if self.remove else 'add')

    @cached_property
    def is_successful_sync(self):
        """ A 2xx answer that found nothing to change ('not_found' only) is not a success. """
        if self.sync_response is None:
            return False
        try:
            data = self.sync_response.json()
        except ValueError:
            return True
        if not isinstance(data, dict):
            return True
        changed = ('removed',) if self.remove else ('added', 'existing')
        if not any(k in data for k in changed):
            return True
        return any(_count(data.get(k)) for k in changed)

    def sync(self):
        if not self.list_id:
            return
        self.display_dialog()
        self.refresh_containers()


class ItemMDbListManage(ItemMDbListSyncBase):
    """ Edit (name + privacy) or delete one of the user's static MDBList lists. Shown whenever MDBList is usable -
    no request at menu-build time; an empty list of lists is reported when the item is chosen. """
    preconfigured = True

    def get_name(self):
        return get_localized(32594)

    def sync(self):
        api = self.mdblist_api
        with BusyDialog():
            lists = api.get_static_lists()
        if lists is None:  # No API credentials
            Dialog().ok('MDbList', f'{get_localized(32516)}\n{get_localized(32517)}')
            return
        if not lists:  # No static lists
            Dialog().ok('MDbList', get_localized(32518))
            return
        x = Dialog().select(get_localized(32594), [i.get('name', '') for i in lists])
        if x == -1:
            return
        mdblist = lists[x]
        y = Dialog().select(mdblist.get('name', ''), [get_localized(32595), get_localized(32596)])
        if y == 0:
            return edit_mdblist_list(api, mdblist)
        if y == 1:
            return delete_mdblist_list(api, mdblist)


def _count(value):
    """ MDBList answers with per-type counts ({'movies': 1, 'shows': 0}). """
    if isinstance(value, dict):
        return sum(i for i in value.values() if isinstance(i, int))
    return value if isinstance(value, int) else 0


def _ok(response):
    return response is not None and response.status_code in (200, 201, 204)


def _response_id(response):
    try:
        return response.json().get('id')
    except (ValueError, AttributeError):
        return


def _ask_private(default=True):
    """ True = private, False = public, None = cancelled. """
    x = Dialog().select(get_localized(32592), [get_localized(32590), get_localized(32591)], preselect=0 if default else 1)
    return None if x == -1 else x == 0


def create_mdblist_list(api):
    """ Ask for a name and privacy, create the list, return its id (None if cancelled or refused). """
    name = Dialog().input(get_localized(32356))
    if not name:
        return
    private = _ask_private()
    if private is None:
        return
    with BusyDialog():
        response = api.create_list(name, private)
    list_id = _response_id(response) if _ok(response) else None
    if list_id:
        return list_id
    # 403 = the account's list limit; anything else is a plain failure
    Dialog().ok('MDbList', get_localized(32593 if response is not None and response.status_code == 403 else 32597))


def edit_mdblist_list(api, mdblist):
    """ Rename and/or change privacy in one request, sending only what changed. """
    old_name = mdblist.get('name', '')
    name = Dialog().input(get_localized(32356), defaultt=old_name)
    if not name:
        return
    fields = {'name': name} if name != old_name else {}
    old_private = mdblist.get('private')
    private = _ask_private(default=True if old_private is None else bool(old_private))
    if private is None:
        return
    if old_private is None or private != bool(old_private):
        fields['private'] = private
    if not fields:
        return
    with BusyDialog():
        response = api.update_list(mdblist.get('id'), **fields)
    if _ok(response):
        return _done(get_localized(32598).format(name))
    Dialog().ok('MDbList', get_localized(32597))


def delete_mdblist_list(api, mdblist):
    name = mdblist.get('name', '')
    if not Dialog().yesno('MDbList', get_localized(32599).format(name)):
        return
    with BusyDialog():
        response = api.delete_list(mdblist.get('id'))
    if _ok(response):
        return _done(get_localized(32600).format(name))
    Dialog().ok('MDbList', get_localized(32597))


def _done(message):
    from tmdbhelper.lib.script.method.kodi_utils import reload_widgets
    Dialog().notification('MDbList', message)
    executebuiltin('Container.Refresh')
    reload_widgets()


class ItemUserList(ItemSync):
    preconfigured = True
    trakt_sync_url = 'items'

    @property
    def userlist(self):
        try:
            return self._userlist
        except AttributeError:
            self._userlist = self.get_userlist()
            return self._userlist

    @userlist.setter
    def userlist(self, value):
        self._userlist = value

    @property
    def userlist_slug(self):
        try:
            return self._userlist_slug
        except AttributeError:
            self._userlist_slug = self.get_userlist_slug()
            return self._userlist_slug

    @userlist_slug.setter
    def userlist_slug(self, value):
        self._userlist_slug = value

    @property
    def userlist_user(self):
        try:
            return self._userlist_user
        except AttributeError:
            self._userlist_user = self.get_userlist_user()
            return self._userlist_user

    @userlist_user.setter
    def userlist_user(self, value):
        self._userlist_user = value

    """
    methods
    """

    def add_list(self):
        """ Create a new Trakt list and returns tuple of list and user slug """
        name = Dialog().input(get_localized(32356))
        if not name:
            return
        response = self.trakt_api.post_response('users/me/lists', postdata={'name': name})
        if not response or not response.json():
            return
        return (
            response.json().get('ids', {}).get('slug'),
            response.json().get('user', {}).get('ids', {}).get('slug'))

    def add_to_library(self, tmdb_type, tmdb_id, list_user_slug_tuple=None, confirm=True):
        """ Add item to library
        Pass optional slug tuple (list, user) to check if in monitored lists
        """
        from tmdbhelper.lib.update.userlist import get_monitor_userlists
        from tmdbhelper.lib.update.library import add_to_library
        if list_user_slug_tuple and list_user_slug_tuple not in get_monitor_userlists():
            return
        if confirm and not Dialog().yesno(get_localized(20444), get_localized(32362)):
            return
        add_to_library(tmdb_type, tmdb_id=tmdb_id)

    """
    overrides
    """

    def get_remove(self):
        if get_infolabel("ListItem.Property(param.owner)") == 'true':
            return True
        return False

    def get_name(self):
        return get_localized(32355) if self.remove else get_localized(32298)

    def get_userlist(self):
        """ Get an existing Trakt list and returns tuple of list and user slug """
        if self.remove:
            return (
                get_infolabel("ListItem.Property(param.list_slug)"),
                get_infolabel("ListItem.Property(param.user_slug)"))
        with BusyDialog():
            list_sync = self.trakt_api.get_list_of_lists('users/me/lists') or []
            list_sync.append({'label': get_localized(32299)})
        x = Dialog().contextmenu([i.get('label') for i in list_sync])
        if x == -1:
            return
        if list_sync[x].get('label') == get_localized(32299):
            return self.add_list()
        return (
            list_sync[x].get('params', {}).get('list_slug'),
            list_sync[x].get('params', {}).get('user_slug'))

    def get_userlist_slug(self):
        if not self.userlist:
            return
        return self.userlist[0]

    def get_userlist_user(self):
        if not self.userlist:
            return
        return self.userlist[1]

    def get_post_response_args(self):
        return ('users', self.userlist_user, 'lists', self.userlist_slug, self.method, )

    def sync(self):
        """ Entry point """
        self.reset_lastactivities()
        if self.is_successful_sync:
            self.add_to_library(self.tmdb_type, self.tmdb_id, list_user_slug_tuple=(self.userlist_slug, self.userlist_user, ))
        self.display_dialog()
        self.refresh_containers()
