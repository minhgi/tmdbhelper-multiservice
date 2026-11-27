import time
from threading import RLock
from jurialmunkey.window import get_property
from tmdbhelper.lib.api.api_keys.wetrakr import BUILTIN_CLIENT_ID, CLIENT_ID, USER_TOKEN, REFRESH_TOKEN, TOKEN_EXPIRES
from tmdbhelper.lib.addon.plugin import ADDONPATH
from tmdbhelper.lib.addon.logger import kodi_log

BASE_URL = 'https://api.wetrakr.com'
API_VERSION = '1'
REQUEST_TIMEOUT = 10  # seconds; posts run on Kodi's player callback thread, so a dead connection must not hold it long
REFRESH_MARGIN = 3600  # refresh the access token (valid 7 days) once less than an hour is left
LIST_PAGE_LIMIT = 5000  # rows per compact page (WeTrakr's maximum)
LIST_MAX_PAGES = 40  # backstop for the cursor loop
JOURNAL_PAGE_LIMIT = 1000  # entries per journal page (WeTrakr's maximum)
JOURNAL_MAX_PAGES = 3  # a journal longer than this is not worth reading: the sync falls back to reading the lists in full
DEFAULT_BACKOFF = 60  # seconds without requests after HTTP 429 when the answer carries no Retry-After
MAX_BACKOFF = 86400  # a longer Retry-After is cut to a day (the longest WeTrakr quota)
BLOCKED_UNTIL = 'WeTrakr.BlockedUntil'  # Home window property: epoch seconds until which an HTTP 429 asks every Kodi process for silence
REQUEST_COUNT = 'WeTrakr.RequestCount'  # Home window property: requests sent since Kodi started
SIGNED_OUT_NOTICE = 'WeTrakr.SignedOutNotice'  # Home window property: the "sign in again" notice is shown once per Kodi session

_DEVICE_FAILURES = {
    401: 'WeTrakr did not accept the app key (client ID).',
    404: 'The sign-in code is unknown. Please try again.',
    409: 'This sign-in code was already used. Please try again.',
    410: 'The sign-in code expired before it was approved. Please try again.',
    418: 'Access was denied on the WeTrakr page.',
    423: 'This WeTrakr app key is suspended.',
    429: 'Too many sign-in codes were requested. Please wait 15 minutes and try again.',
}


def _to_int(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


class WeTrakrAPI:
    """ WeTrakr REST client (https://api.wetrakr.com). Written without the shared RequestAPI on purpose: that one logs the full request
    headers (Bearer token and key) on every HTTP error, and WeTrakr's 4xx answers carry what the add-on needs (404 title not found, 401 token
    rejected). Nothing here ever logs a token, the client ID or an OAuth body - only paths and status codes.

    Tokens live in settings and are read again before every use, because the service and the script (settings buttons) are separate
    processes that both refresh: WeTrakr rotates the refresh token on every refresh, so a stale copy must never be sent blindly. """

    def __init__(self):
        self._lock = RLock()
        self._session = None

    # -- stored credentials ------------------------------------------------------------------------------------------------

    @property
    def client_id(self):
        """ The app key sent with every request: the one entered in the settings (an override for a user with their own WeTrakr developer
        app), otherwise TMDb Helper's own, built in. """
        return f'{CLIENT_ID.value or ""}'.strip() or BUILTIN_CLIENT_ID

    def _device_failure(self, status):
        """ The message for a failed sign-in step. A refused app key (401) or a suspended one (423) says what to do: with an override in the settings,
        clear it to use the key built into TMDb Helper; without one, the built-in key itself was refused, which only WeTrakr can fix. """
        message = _DEVICE_FAILURES.get(status)
        if message and status in (401, 423):
            if f'{CLIENT_ID.value or ""}'.strip():
                message += ' Clear the key you entered in the settings to use the one built into TMDb Helper.'
            else:
                message += ' This is the key built into TMDb Helper, so please try again later.'
        return message

    @property
    def access_token(self):
        return f'{USER_TOKEN.value or ""}'.strip()

    @property
    def refresh_token(self):
        return f'{REFRESH_TOKEN.value or ""}'.strip()

    @property
    def is_authorized(self):
        return bool(self.client_id and (self.access_token or self.refresh_token))

    def _clear_tokens(self):
        USER_TOKEN.value = ''
        REFRESH_TOKEN.value = ''
        TOKEN_EXPIRES.value = ''

    def _expiring(self):
        """ True when the access token is known to expire within REFRESH_MARGIN seconds (an unknown expiry, e.g. a token pasted in by hand, counts as fine) """
        expires = _to_int(TOKEN_EXPIRES.value)
        return bool(expires) and expires - time.time() < REFRESH_MARGIN

    def _store_tokens(self, response):
        """ Save the tokens of a 200 answer from /oauth/device/token or /oauth/token/refresh. The refresh answer repeats the new
        refresh token under new_refresh_token as well; either carries the same value. """
        try:
            data = response.json()
        except ValueError:
            return False
        access = f'{data.get("access_token") or ""}'
        refresh = f'{data.get("refresh_token") or data.get("new_refresh_token") or ""}'
        if not access or not refresh:
            return False
        USER_TOKEN.value = access
        REFRESH_TOKEN.value = refresh
        TOKEN_EXPIRES.value = f'{int(time.time()) + _to_int(data.get("expires_in"), 604800)}'
        return True

    # -- requests ----------------------------------------------------------------------------------------------------------

    def _get_session(self):
        if self._session is None:
            import requests
            self._session = requests.Session()
        return self._session

    def _send(self, method, path, body=None, params=None, token=None, backoff=True):
        """ One HTTP request. Returns the Response (any status) or None when nothing could be sent or no answer came
        (network failure, or an HTTP 429 asked for silence a moment ago). backoff=False is for the sign-in polls, where 429 means slow_down. """
        if backoff and self._blocked():
            return None
        import requests
        headers = {
            'Content-Type': 'application/json', 'Accept': 'application/json',
            'wetrakr-api-version': API_VERSION, 'wetrakr-api-key': self.client_id}
        if token:
            headers['Authorization'] = f'Bearer {token}'
        try:
            response = self._get_session().request(
                method, f'{BASE_URL}{path}', json=body, params=params, headers=headers, timeout=REQUEST_TIMEOUT)
        except requests.exceptions.RequestException as exc:
            kodi_log(f'WeTrakr {method} {path}: request failed - {type(exc).__name__}', 1)
            return None
        self._count_request(method, path, response.status_code)
        if backoff and response.status_code == 429:
            wait = min(_to_int(response.headers.get('Retry-After'), DEFAULT_BACKOFF) or DEFAULT_BACKOFF, MAX_BACKOFF)
            get_property(BLOCKED_UNTIL, set_property=f'{time.time() + wait:.0f}')
            kodi_log(f'WeTrakr {method} {path}: HTTP 429, no requests for {wait}s', 1)
        return response

    @staticmethod
    def _blocked():
        """ True while an HTTP 429 asks for silence. Shared by every Kodi process: each change check, widget and sync run makes its own API object,
        and a rejected request still counts against the account. A time further off than a day is ignored (a clock that moved). """
        try:
            until = float(get_property(BLOCKED_UNTIL) or 0)
        except ValueError:
            return False
        return 0 < until - time.time() <= MAX_BACKOFF

    @staticmethod
    def _count_request(method, path, status):
        """ One debug line per request (method, path, status: nothing sensitive) and a running count in a Home window property that every Kodi
        process shares, so a session's request cost can be read back (the free quota is counted per account) """
        total = _to_int(get_property(REQUEST_COUNT)) + 1
        get_property(REQUEST_COUNT, set_property=f'{total}')
        kodi_log(f'WeTrakr {method} {path} -> {status} (request {total} this Kodi session)', 2)

    @staticmethod
    def _json(response):
        if response is None:
            return {}
        try:
            data = response.json()
        except ValueError:
            return {}
        return data if isinstance(data, dict) else {}

    def user_request(self, method, path, body=None, params=None):
        """ Request that needs the signed-in user. The access token is refreshed first when it is about to expire, and once after an HTTP
        401. Returns the Response (the caller reads its status) or None when nothing could be sent. """
        token = self._usable_token()
        if not token:
            return None
        response = self._send(method, path, body, params, token)
        if response is not None and response.status_code == 401:
            if self._refresh(seen_access=token):
                token = self.access_token
                response = self._send(method, path, body, params, token) if token else None
        return response

    @staticmethod
    def _user_id(token):
        """ The numeric user id inside the access token (a JWT: its payload is only decoded, never logged - it also holds the e-mail address).
        '' when there is none. """
        try:
            import base64
            import json
            part = (token or '').split('.')[1]
            return str(json.loads(base64.urlsafe_b64decode(part + '=' * (-len(part) % 4))).get('user_id') or '')
        except Exception:
            return ''

    def get_profile(self):
        """ The public profile of the signed-in user (GET /users/{id}, no token needed), or None. GET /account itself answers 403 NOT_PUBLIC to
        third-party apps, but the access token carries the user id and the profile holds info.username. """
        try:
            user_id = self._user_id(self.access_token)
            if not user_id:
                return None
            response = self._send('GET', f'/users/{user_id}')
            if response is None or response.status_code != 200:
                return None
            data = self._json(response)
            if isinstance(data.get('data'), dict):
                data = data['data']
            return data if isinstance(data.get('info'), dict) else None
        except Exception as exc:
            kodi_log(f'WeTrakr profile lookup failed: {type(exc).__name__}', 1)
            return None

    def get_json(self, path, params=None):
        """ GET of a user endpoint that answers one JSON object (e.g. /sync/last_activities): the dict, or None when it could not be read """
        response = self.user_request('GET', path, params=params)
        if response is None or response.status_code != 200:
            return None
        data = self._json(response)
        return data or None

    def get_list(self, path, extra=None):
        """ Every row of a compact cursor list: (ok, rows). ok is False, and rows empty, when any page failed, the cursor repeated or the page
        cap was hit, or the rows read do not add up to the item count WeTrakr reports: a partial list must never replace stored data.
        extra = more query values for every page (from_date: only the rows changed since then, 7.5.20). """
        rows, seen, after, expected = [], set(), None, None
        for page in range(LIST_MAX_PAGES):
            params = {**(extra or {}), 'compact': 'true', 'limit': LIST_PAGE_LIMIT}
            if after:
                params['after'] = after
            response = self.user_request('GET', path, params=params)
            if response is None or response.status_code != 200:
                return False, []
            try:
                data = response.json()
            except ValueError:
                return False, []
            if not isinstance(data, list):
                return False, []
            rows.extend(data)
            if page == 0:
                expected = _to_int(response.headers.get('X-Pagination-Item-Count'), None)
            after = response.headers.get('X-Pagination-Next')
            if not after:
                break
            if after in seen:
                return False, []
            seen.add(after)
        else:
            return False, []
        if expected is not None and len(rows) != expected:
            kodi_log(f'WeTrakr {path}: read {len(rows)} rows but the list holds {expected}, keeping the stored data', 1)
            return False, []
        return True, rows

    def get_journal(self, from_date, category='watched'):
        """ GET /sync/journal (7.5.20): what changed since from_date, oldest first, read page by page. Returns (state, entries, visible_until):
        state 'ok' (entries is the complete answer; visible_until is the newest moment the journal shows, WeTrakr writes it a few seconds late, so the
        NEXT read must start from it, never from the clock), 'expired' (from_date is older than the journal keeps: do a full read), 'big' (more
        than JOURNAL_MAX_PAGES pages: a full read is cheaper) or 'failed' (nothing could be read; keep what is stored). """
        entries, until, expected = [], None, None
        for page in range(1, JOURNAL_MAX_PAGES + 2):
            response = self.user_request('GET', '/sync/journal', params={'from_date': from_date, 'category': category, 'limit': JOURNAL_PAGE_LIMIT, 'page': page})
            if response is None:
                return 'failed', [], None
            if response.status_code == 400 and self._json(response).get('error') == 'JOURNAL_EXPIRED':
                return 'expired', [], None
            if response.status_code != 200:
                return 'failed', [], None
            data = self._json(response)
            rows, seen_until = data.get('journal'), data.get('visible_until')
            if not isinstance(rows, list) or not isinstance(seen_until, str) or not seen_until:
                return 'failed', [], None
            until = seen_until if until is None else min(until, seen_until)  # the oldest of the answers: nothing newer is certain to be complete
            entries.extend(rows)
            if page == 1:
                expected = _to_int(response.headers.get('X-Pagination-Item-Count'), None)
            if page >= (_to_int(response.headers.get('X-Pagination-Page-Count'), 0) or 0):
                break
            if page >= JOURNAL_MAX_PAGES:
                return 'big', [], None
        if expected is not None and len(entries) != expected:
            kodi_log(f'WeTrakr /sync/journal: read {len(entries)} entries but the journal holds {expected}, keeping the stored data', 1)
            return 'failed', [], None
        return 'ok', entries, until

    def get_first_page(self, path, limit=LIST_PAGE_LIMIT, params=None):
        """ Only the first compact page of a cursor list (newest rows first for the history routes): (ok, rows). For a screen that needs the latest
        rows of a list that can be very long (the watch history), where walking every page would cost many requests. """
        response = self.user_request('GET', path, params={'compact': 'true', 'limit': limit, **(params or {})})
        if response is None or response.status_code != 200:
            return False, []
        try:
            data = response.json()
        except ValueError:
            return False, []
        return (True, data) if isinstance(data, list) else (False, [])

    def get_pages(self, path, params=None, limit=100, max_pages=10):
        """ The rows of a normal (not compact) list, page by page: (ok, rows). Stops at the first short page, or after max_pages pages (the first
        limit x max_pages rows of the list, in the order asked for: that is a complete answer to "the newest N", so ok stays True). A page that
        fails or cannot be read gives (False, rows read so far): the caller must not show those as the list. """
        rows = []
        for page in range(1, max_pages + 1):
            response = self.user_request('GET', path, params={**(params or {}), 'limit': limit, 'page': page})
            if response is None or response.status_code != 200:
                return False, rows
            try:
                data = response.json()
            except ValueError:
                return False, rows
            if not isinstance(data, list):
                return False, rows
            rows.extend(data)
            if len(data) < limit:
                return True, rows
        return True, rows  # page cap reached: the first limit x max_pages rows

    def get_paginated(self, response, page=1, limit=None, permitted_types=None, trakt_style=False):
        from tmdbhelper.lib.items.itemlist import ItemListPagination
        if isinstance(response, dict):
            response = {k: v for k, v in response.items() if isinstance(v, list)}
        return ItemListPagination(response or {}, page=page, limit=limit,
                                  permitted_types=permitted_types, trakt_style=trakt_style, global_sort=not trakt_style)

    @property
    def wetrakr_syncdata(self):
        try:
            return self._wetrakr_syncdata
        except AttributeError:
            from tmdbhelper.lib.api.wetrakr.sync.datasync import WeTrakrSyncData
            self._wetrakr_syncdata = WeTrakrSyncData(self)
            return self._wetrakr_syncdata

    def _forget_synced_data(self):
        """ Nothing of one account may survive into the next: empty the sync tables (login and logout) """
        try:
            self.wetrakr_syncdata.clear_all()
        except Exception as exc:
            kodi_log(f'WeTrakr sync data not cleared - {type(exc).__name__}', 1)

    def scrobble(self, action, payload, params=None):
        """ POST /scrobble/<start|pause|stop> """
        return self.user_request('POST', f'/scrobble/{action}', body=payload, params=params)

    # -- token refresh -----------------------------------------------------------------------------------------------------

    def _usable_token(self):
        if not self.client_id:
            return ''
        token = self.access_token
        if (not token or self._expiring()) and self.refresh_token:
            self._refresh(seen_access=token)
            token = self.access_token
        return token

    def _refresh(self, seen_access):
        """ Trade the stored refresh token for new tokens. seen_access is the access token the caller used (empty if it had none).
        True when a usable access token is stored afterwards. Tokens are only cleared when WeTrakr says the refresh token is dead
        (HTTP 400/401); a network failure or a 429/5xx keeps them for the next try. """
        with self._lock:
            access, refresh = self.access_token, self.refresh_token
            if access and access != seen_access and not self._expiring():
                return True  # another process (service or script) refreshed meanwhile
            if not refresh:
                self._signed_out('no refresh token')
                return False
            response = self._send('POST', '/oauth/token/refresh', {'refresh_token': refresh})
            if response is None:
                return False
            if response.status_code == 200 and self._store_tokens(response):
                return True
            if response.status_code in (400, 401):
                stored = self.refresh_token
                if stored and stored != refresh:  # rotated by another process while this request was on its way: use its value once
                    retry = self._send('POST', '/oauth/token/refresh', {'refresh_token': stored})
                    if retry is not None and retry.status_code == 200 and self._store_tokens(retry):
                        return True
                self._signed_out(f'refresh refused (HTTP {response.status_code})')
            else:
                kodi_log(f'WeTrakr token refresh: HTTP {response.status_code}, tokens kept', 1)
            return False

    def _signed_out(self, reason):
        self._clear_tokens()
        kodi_log(f'WeTrakr signed out locally: {reason}', 1)
        if get_property(SIGNED_OUT_NOTICE):
            return
        get_property(SIGNED_OUT_NOTICE, set_property='1')
        from xbmcgui import Dialog
        Dialog().notification('TMDbHelper', 'WeTrakr sign-in expired - authorize again in the settings', icon=f'{ADDONPATH}/icon.png')

    # -- sign in / out (settings buttons) ----------------------------------------------------------------------------------

    def login(self, force=True):
        """ Device flow: the user enters a code on wetrakr.com/activate. Needs the client ID only, never a client secret. """
        if not force:
            return
        from xbmcgui import Dialog
        from xbmc import Monitor
        from tmdbhelper.lib.api.wetrakr.authdialog import WeTrakrAuthDialog

        get_property(BLOCKED_UNTIL, clear_property=True)  # signing in again (maybe with another app key) lifts a pause

        if not self.client_id:
            Dialog().ok('WeTrakr', 'Enter your WeTrakr app key (client ID) in the settings first.')
            return

        response = self._send('POST', '/oauth/device/code', {'client_id': self.client_id}, backoff=False)
        data = self._json(response)
        if response is None or response.status_code != 200 or not data.get('device_code'):
            status = response.status_code if response is not None else None
            Dialog().ok('WeTrakr', self._device_failure(status) or (
                'WeTrakr could not be reached.' if response is None else 'Authorization request failed.'))
            return

        device_code = data['device_code']
        user_code = data.get('user_code', '')
        url = data.get('verification_url') or 'https://wetrakr.com/activate'
        expires_in = _to_int(data.get('expires_in'), 600)
        interval = max(_to_int(data.get('interval'), 5), 1)

        dialog = WeTrakrAuthDialog(url, user_code)  # QR code + address + code + time left (the text dialog when that cannot be shown)
        dialog.create()
        monitor = Monitor()
        elapsed, approved, failure, canceled = 0, None, '', False
        try:
            while elapsed < expires_in:
                if dialog.iscanceled():
                    canceled = True
                    break
                if monitor.waitForAbort(interval):
                    return
                elapsed += interval
                dialog.update(int(elapsed * 100 / expires_in), expires_in - elapsed)
                reply = self._send(
                    'POST', '/oauth/device/token', {'code': device_code, 'client_id': self.client_id}, backoff=False)
                if reply is None:
                    continue
                if reply.status_code == 200:
                    approved = reply
                    break
                if reply.status_code == 429:  # slow_down: the interval was too short
                    interval += 5
                    continue
                if reply.status_code == 400 and self._json(reply).get('error') == 'authorization_pending':
                    continue
                failure = self._device_failure(reply.status_code) or 'Authorization failed.'
                break
        finally:
            dialog.close()

        if canceled:
            return
        if approved is not None and self._store_tokens(approved):
            self._forget_synced_data()  # a different account must not inherit the last one's marks (the sync decides by stamps, which can be older)
            get_property(SIGNED_OUT_NOTICE, clear_property=True)
            kodi_log('WeTrakr authenticated successfully', 1)
            Dialog().notification('TMDbHelper', 'WeTrakr authorized', icon=f'{ADDONPATH}/icon.png')
            return
        Dialog().ok('WeTrakr', failure or 'Authorization failed or timed out.')

    def logout(self):
        from xbmcgui import Dialog
        if not Dialog().yesno('WeTrakr', 'Logout of WeTrakr account?'):
            return
        access, refresh = self.access_token, self.refresh_token
        revoked = False
        if access and refresh and self.client_id:
            response = self._send('POST', '/oauth/logout', {'refresh_token': refresh}, token=access, backoff=False)
            revoked = response is not None and response.status_code == 200
        self._clear_tokens()
        self._forget_synced_data()
        kodi_log(f'WeTrakr logged out (session revoked at WeTrakr: {"yes" if revoked else "no"})', 1)
        if access and not revoked:
            Dialog().ok('WeTrakr', 'Logged out here. WeTrakr could not be reached to end the session, so it stays valid there until it expires.')
            return
        Dialog().ok('WeTrakr', 'WeTrakr account logged out.')
