from tmdbhelper.lib.api.request import RequestAPI
from tmdbhelper.lib.api.api_keys.mdblist import API_KEY, CLIENT_ID, USER_TOKEN
from tmdbhelper.lib.addon.plugin import ADDONPATH
from tmdbhelper.lib.addon.logger import kodi_log
from tmdbhelper.lib.items.itemlist import ItemListPagination, ListPagination


class MDbListPaginationLists(ListPagination):
    """ Paginates and configures items in a list of lists """

    @staticmethod
    def map_item(i):
        item = {}
        item['label'] = i.get('name')
        item['infolabels'] = {'plot': i.get('description'), 'studio': [i.get('user_name')]}
        item['art'] = {'icon': f'{ADDONPATH}/resources/icons/mdblist/mdblist.png'}
        item['params'] = {
            'info': 'mdblist_userlist',
            'list_name': i.get('name'),
            'list_id': i.get('id'),
            'plugin_category': i.get('name')}
        item['unique_ids'] = {
            'mdblist': i.get('id'),
            'slug': i.get('slug'),
            'user': i.get('user_id')}
        if i.get('dynamic'):
            item['params']['dynamic'] = 'true'
        return item

    @property
    def items(self):
        try:
            return self._items
        except AttributeError:
            self.update_pagination()
            return self._items

    def update_pagination(self):
        self._paginated_items = self.get_updated_pagination()
        self._items = [j for j in (self.map_item(i) for i in self.paginated_items.items) if j]


class MDbListRatingMapping():
    ratings_translation = {
        'tomatoes': 'rottentomatoes_rating',
        'tomatoesaudience': 'rottentomatoes_usermeter',
        'popcorn': 'rottentomatoes_usermeter'}

    def __init__(self, meta):
        self.meta = meta

    @property
    def meta_ratings(self):
        try:
            return self.meta['ratings']
        except (KeyError, TypeError):
            return []

    @property
    def ratings(self):
        try:
            return self._ratings
        except AttributeError:
            return self.get_ratings()

    def get_ratings(self):
        ratings = {}
        ratings['mdblist_rating'] = self.meta.get('score')

        for i in self.meta_ratings:
            try:
                name = i['source']
            except KeyError:
                continue
            if i.get('value'):
                ratings[self.ratings_translation.get(name) or f'{name}_rating'] = i['value']
            if i.get('votes'):
                ratings[f'{name}_votes'] = i['votes']

        self._ratings = ratings
        return self._ratings


class MDbList(RequestAPI):

    api_key = API_KEY
    client_id = CLIENT_ID
    user_token = USER_TOKEN

    def __init__(self, api_key=None):
        api_key = api_key or self.api_key

        super(MDbList, self).__init__(
            req_api_key=f'apikey={api_key}',
            req_api_name='MDbList.v2',
            req_api_url='https://api.mdblist.com')  # OLD API = https://mdblist.com/api
        MDbList.api_key = api_key

    @property
    def authorization(self):
        return self.user_token.value or ''

    @property
    def auth_headers(self):
        token = self.user_token.value
        if not token:
            return {}
        return {'Authorization': f'Bearer {token}'}

    @property
    def mdblist_syncdata(self):
        try:
            return self._mdblist_syncdata
        except AttributeError:
            from tmdbhelper.lib.api.mdblist.sync.datasync import MDbListSyncData
            self._mdblist_syncdata = MDbListSyncData(self)
            return self._mdblist_syncdata

    def login(self, force=True):
        if not force:
            return
        from xbmcgui import Dialog, DialogProgress
        import xbmc

        response = self.get_api_request_json(
            'https://api.mdblist.com/oauth/device-authorization/',
            postdata={'client_id': self.client_id, 'scope': 'write'})
        if not response or not response.get('device_code'):
            Dialog().ok('MDBList', 'Authorization request failed.')
            return

        device_code = response['device_code']
        user_code = response['user_code']
        verification_url = response.get('verification_url', 'https://mdblist.com/oauth/device/')
        expires_in = int(response.get('expires_in', 300))
        interval = int(response.get('interval', 5))

        progress = 0
        auth_dialog = DialogProgress()
        auth_dialog.create(
            'MDBList',
            f'Visit [B]{verification_url}[/B]\nCode: [B]{user_code}[/B]')

        access_token = None
        while progress < expires_in:
            if auth_dialog.iscanceled():
                auth_dialog.close()
                return
            xbmc.Monitor().waitForAbort(interval)
            if xbmc.Monitor().abortRequested():
                auth_dialog.close()
                return
            progress += interval
            auth_dialog.update(int(progress * 100 / expires_in))

            result = self.get_api_request_json(
                'https://api.mdblist.com/oauth/token/',
                postdata={
                    'grant_type': 'urn:ietf:params:oauth:grant-type:device_code',
                    'device_code': device_code,
                    'client_id': self.client_id})
            if result and result.get('access_token'):
                access_token = result['access_token']
                break

        auth_dialog.close()

        if not access_token:
            Dialog().ok('MDBList', 'Authorization failed or timed out.')
            return

        self.user_token.value = access_token
        kodi_log('MDBList authenticated successfully', 1)
        Dialog().notification('TMDbHelper', 'MDBList authorized', icon=f'{ADDONPATH}/icon.png')

    def logout(self):
        from xbmcgui import Dialog
        if not Dialog().yesno('MDBList', 'Logout of MDBList account?'):
            return

        token = self.user_token.value
        if token:
            self.get_api_request(
                'https://api.mdblist.com/oauth/revoke_token/',
                postdata={'token': token, 'client_id': self.client_id})

        self.user_token.value = ''
        kodi_log('MDBList logged out', 1)
        Dialog().ok('MDBList', 'MDBList account logged out.')

    def modify_static_list(self, list_id, media_type, media_id, media_provider='tmdb', action='add'):
        item = {f'{media_type}s': [{media_provider: media_id}]}
        path = self.get_request_url('lists', list_id, 'items', action)
        return self.get_api_request(path, postdata=item, method='json')

    def get_details(self, media_type, media_id, media_provider='tmdb'):
        return self.get_request_sc(media_provider, media_type, media_id)  # TODO: Add append_to_response=review ?

    def get_ratings(self, media_type, media_id, media_provider='tmdb'):
        response = self.get_details(media_type, media_id, media_provider=media_provider)
        response = MDbListRatingMapping(response)
        return response.ratings

    def get_list_of_lists(self, path, page=1, limit: int = None):
        response = self.get_request_sc(path, cache_refresh=True if page == 1 else False)
        response = MDbListPaginationLists(response, page=page, limit=limit or 250)
        return response.items if not response.next_page else response.items + response.next_page

    def get_list_of_lists_search(self, query):
        return self.get_list_of_lists(path=f'lists/search?query={query}')

    def get_custom_trakt_style_list(self, list_id):
        path = f'lists/{list_id}/items'
        response = self.get_request_sc(path, cache_refresh=True)
        return self.get_paginated(response, permitted_types=('movie', 'show'), trakt_style=True)

    def get_custom_list(self, list_id, page=1, limit: int = None):
        path = f'lists/{list_id}/items'
        response = self.get_request_sc(path, cache_refresh=True if page == 1 else False)
        response = self.get_paginated(response, page=page, limit=limit)
        return response

    def get_paginated(self, response, page=1, limit: int = None, permitted_types: tuple = None, trakt_style=False):
        # Strip non-list values (e.g. 'pagination' dict) before handing to
        # ItemListPagination — it iterates all dict values and crashes on non-lists.
        if isinstance(response, dict):
            response = {k: v for k, v in response.items() if isinstance(v, list)}
        return ItemListPagination(response or {}, page=page, limit=limit, permitted_types=permitted_types, trakt_style=trakt_style)
