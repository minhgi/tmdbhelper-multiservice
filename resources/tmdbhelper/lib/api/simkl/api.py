from tmdbhelper.lib.api.request import RequestAPI
from tmdbhelper.lib.api.api_keys.simkl import CLIENT_ID, USER_TOKEN
from tmdbhelper.lib.addon.plugin import ADDONPATH
from tmdbhelper.lib.addon.logger import kodi_log

BASE_URL = 'https://api.simkl.com'
_AUTH_PIN_URL = f'{BASE_URL}/oauth/pin'


class SimklAPI(RequestAPI):

    client_id = CLIENT_ID
    user_token = USER_TOKEN

    def __init__(self):
        super(SimklAPI, self).__init__(
            req_api_key=f'client_id={self.client_id}',
            req_api_name='Simkl',
            req_api_url=BASE_URL)

    @property
    def authorization(self):
        return self.user_token.value or ''

    @property
    def auth_headers(self):
        token = self.user_token.value
        if not token:
            return {}
        return {
            'Authorization': f'Bearer {token}',
            'simkl-api-key': self.client_id,
        }

    def login(self, force=True):
        if not force:
            return
        from xbmcgui import Dialog, DialogProgress
        import xbmc

        response = self.get_api_request_json(
            f'{_AUTH_PIN_URL}?client_id={self.client_id}&redirect=https://simkl.com')
        if not response or not response.get('user_code'):
            Dialog().ok('Simkl', 'Authorization request failed.')
            return

        user_code = response['user_code']
        expires_in = int(response.get('expires_in', 300))
        interval = int(response.get('interval', 5))

        progress = 0
        auth_dialog = DialogProgress()
        auth_dialog.create(
            'Simkl',
            f'Visit [B]simkl.com/pin[/B]\nCode: [B]{user_code}[/B]')

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
                f'{_AUTH_PIN_URL}/{user_code}?client_id={self.client_id}')
            if result and result.get('result') == 'OK' and result.get('access_token'):
                access_token = result['access_token']
                break

        auth_dialog.close()

        if not access_token:
            Dialog().ok('Simkl', 'Authorization failed or timed out.')
            return

        self.user_token.value = access_token
        kodi_log('Simkl authenticated successfully', 1)
        Dialog().notification('TMDbHelper', 'Simkl authorized', icon=f'{ADDONPATH}/icon.png')

    def logout(self):
        from xbmcgui import Dialog
        if not Dialog().yesno('Simkl', 'Logout of Simkl account?'):
            return
        self.user_token.value = ''
        kodi_log('Simkl logged out', 1)
        Dialog().ok('Simkl', 'Simkl account logged out.')

    @property
    def simkl_syncdata(self):
        try:
            return self._simkl_syncdata
        except AttributeError:
            from tmdbhelper.lib.api.simkl.sync.datasync import SimklSyncData
            self._simkl_syncdata = SimklSyncData(self)
            return self._simkl_syncdata

    def get_paginated(self, response, page=1, limit=None, permitted_types=None, trakt_style=False):
        from tmdbhelper.lib.items.itemlist import ItemListPagination
        if isinstance(response, dict):
            response = {k: v for k, v in response.items() if isinstance(v, list)}
        return ItemListPagination(response or {}, page=page, limit=limit,
                                  permitted_types=permitted_types, trakt_style=trakt_style)

    def get_account_info(self):
        if not self.user_token.value:
            return None
        return self.get_api_request_json(
            f'{BASE_URL}/users/settings',
            headers=self.auth_headers)
