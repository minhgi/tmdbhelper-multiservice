"""Account Info dialogs for Trakt, MDBList and Simkl (Settings > Accounts).

Each dialog is built only from the fields named in its formatter below - nothing else from the
service response is shown or logged. Formatting is kept in pure functions (no Kodi imports) so it
can be tested offline.
"""
import time


# ---------------------------------------------------------------------------
# Pure formatters
# ---------------------------------------------------------------------------

def _count(value):
    try:
        return f'{int(value):,}'
    except (TypeError, ValueError):
        return 'N/A'


def _int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _duration(minutes):
    days, rest = divmod(int(minutes or 0), 1440)
    hours, mins = divmod(rest, 60)
    return f'{days} days {hours:02d} hours {mins:02d} minutes'


def _date(value):
    return str(value or 'N/A')[:19].replace('T', ' ').replace('Z', '')


def format_trakt(settings, stats=None):
    """settings = GET users/settings body; stats = GET users/me/stats body, or None when unavailable
    (a private profile answers HTTP 204 with no body, so the stats rows are simply left out)."""
    user = settings.get('user') or {}
    account = settings.get('account') or {}
    if user.get('vip') and user.get('vip_years'):
        vip = f"Yes ({user['vip_years']} years)"
    else:
        vip = 'Yes' if user.get('vip') else 'No'
    lines = [
        f"Username:  {user.get('username') or 'N/A'}",
        f"Timezone:  {account.get('timezone') or 'N/A'}",
        f"Joined:  {_date(user.get('joined_at'))}",
        f"Private:  {'Yes' if user.get('private') else 'No'}",
        f'VIP:  {vip}',
    ]
    if isinstance(stats, dict) and stats:
        movies = stats.get('movies') or {}
        shows = stats.get('shows') or {}
        episodes = stats.get('episodes') or {}
        lines += [
            f"Ratings given:  {_count((stats.get('ratings') or {}).get('total', 0))}",
            f"Movies:  {_count(movies.get('collected', 0))} collected, {_count(movies.get('watched', 0))} watched "
            f"for {_duration(movies.get('minutes'))}",
            f"Shows:  {_count(shows.get('collected', 0))} collected, {_count(shows.get('watched', 0))} watched",
            f"Episodes:  {_count(episodes.get('watched', 0))} watched for {_duration(episodes.get('minutes'))}",
        ]
    lines.append('Status:  Online')
    return lines


def format_mdblist(status_code, data, headers, now=None):
    """Quota screen from GET /user. Works from the JSON body, and from the X-RateLimit-* / Retry-After
    headers when the body is unavailable (HTTP 429 on an exhausted daily quota).
    Returns (lines, None) on success or (None, message) when no screen can be built."""
    from datetime import datetime
    now = time.time() if now is None else now
    headers = {str(k).lower(): v for k, v in (headers or {}).items()}
    data = data if isinstance(data, dict) else {}
    throttled = status_code == 429
    if status_code == 401:
        return None, 'MDBList: Token rejected (HTTP 401) - re-authorise MDBList from Settings > Accounts.'
    if not (status_code in range(200, 300) or throttled):
        return None, 'MDBList: Could not retrieve account information.'

    api_requests = _int(data.get('api_requests'))
    api_requests_count = _int(data.get('api_requests_count'))
    limit = _int(data.get('rate_limit'))
    if limit is None:
        limit = api_requests
    if limit is None:
        limit = _int(headers.get('x-ratelimit-limit'))
    remaining = _int(data.get('rate_limit_remaining'))
    if remaining is None and api_requests is not None and api_requests_count is not None:
        remaining = max(api_requests - api_requests_count, 0)
    if remaining is None:
        remaining = _int(headers.get('x-ratelimit-remaining'))
    if remaining is None and throttled:
        remaining = 0
    used = api_requests_count
    if used is None and limit is not None and remaining is not None:
        used = max(limit - remaining, 0)
    reset = _int(data.get('rate_limit_reset'))
    if reset is None:
        reset = _int(headers.get('x-ratelimit-reset'))
    retry_after = _int(headers.get('retry-after'))
    if reset is None and retry_after is not None:
        reset = int(now) + retry_after

    reset_str = 'N/A'
    if reset:
        reset_str = datetime.fromtimestamp(reset).strftime('%Y-%m-%d %H:%M')
        minutes = max(int((reset - now) // 60), 0)
        reset_str += f' (in {minutes // 60}h {minutes % 60:02d}m)' if minutes >= 60 else f' (in {minutes}m)'

    if throttled or remaining == 0:
        state = 'Daily limit reached - sync is paused until the reset'
    elif remaining is None:
        state = 'N/A'
    else:
        state = 'OK'

    return [
        f"Username:  {data.get('username') or 'N/A'}",
        f"Plan:  {data.get('plan') or 'N/A'}",
        f'Status:  {state}',
        f'Daily limit:  {_count(limit)}',
        f'Used today:  {_count(used)}',
        f'Remaining:  {_count(remaining)}',
        f'Resets:  {reset_str}',
    ], None


def format_simkl(data):
    """data = GET users/settings body. Shows name, account id/type, time zone and join date only."""
    user = data.get('user') or {}
    account = data.get('account') or {}
    lines = [f"Username:  {user.get('name') or 'N/A'}"]
    if account.get('id'):
        lines.append(f"Account ID:  {account['id']}")
    if account.get('type'):
        lines.append(f"Account type:  {str(account['type']).capitalize()}")
    if account.get('timezone'):
        lines.append(f"Timezone:  {account['timezone']}")
    if user.get('joined_at'):
        lines.append(f"Joined:  {_date(user['joined_at'])}")
    lines.append('Status:  Online')
    return lines


# ---------------------------------------------------------------------------
# Dialogs (Kodi)
# ---------------------------------------------------------------------------

def _show(title, lines):
    from xbmcgui import Dialog
    Dialog().select(title, lines)


def _offline(title, reason):
    _show(title, [f'Status:  Offline ({reason})'])


def trakt_account_info(**kwargs):
    from xbmcgui import Dialog
    from tmdbhelper.lib.addon.logger import kodi_log
    from tmdbhelper.lib.api.trakt.api import TraktAPI

    title = 'TMDb Helper: Trakt Account'
    try:
        api = TraktAPI()  # never opens the login flow without force=True
        if not api.authorization:
            Dialog().ok('TMDb Helper', 'Trakt: Not authorised - authorise from settings.')
            return
        response = api.get_response('users/settings')
        if response is None:
            return _offline(title, 'no response from Trakt')
        if response.status_code != 200:
            return _offline(title, f'HTTP {response.status_code} from Trakt')
        try:
            settings = response.json()
        except ValueError:
            settings = None
        if not isinstance(settings, dict) or not isinstance(settings.get('user'), dict):
            return _offline(title, 'unreadable response from Trakt')
        stats = None
        stats_response = api.get_response('users/me/stats')
        if stats_response is not None and stats_response.status_code == 200:
            try:
                stats = stats_response.json()
            except ValueError:
                stats = None
        _show(title, format_trakt(settings, stats))
    except Exception as exc:
        kodi_log(f'Trakt account info error: {type(exc).__name__}', 1)
        _offline(title, 'Trakt unreachable')


def mdblist_account_info(**kwargs):
    import requests
    from xbmcgui import Dialog
    from tmdbhelper.lib.addon.logger import kodi_log
    from tmdbhelper.lib.api.mdblist.api import MDbList

    title = 'TMDb Helper: MDBList Account'
    try:
        api = MDbList()
        token = api.user_token.value
        if not token and not api.api_key:
            Dialog().ok('TMDb Helper', 'MDBList: Not authorised - authorise from settings.')
            return
        headers = {'User-Agent': 'Mozilla/5.0', 'Accept': 'application/json'}
        params = {}
        if token:
            headers['Authorization'] = f'Bearer {token}'
        else:
            params['apikey'] = api.api_key
        # Direct one-shot request: the shared retry path would turn a 429 (the very case this screen
        # explains) into a stall and hide the X-RateLimit-* headers.
        try:
            response = requests.get('https://api.mdblist.com/user', params=params, headers=headers, timeout=10)
        except Exception as exc:
            # type name only - a requests exception's text can carry the full URL, apikey= included
            kodi_log(f'MDBList account info request failed: {type(exc).__name__}', 1)
            Dialog().ok('TMDb Helper', 'MDBList: Could not retrieve account information.')
            return
        try:
            data = response.json()
        except ValueError:
            data = {}
        lines, message = format_mdblist(response.status_code, data, response.headers)
        if lines is None:
            kodi_log(f'MDBList account info: HTTP {response.status_code}', 1)
            Dialog().ok('TMDb Helper', message)
            return
        _show(title, lines)
    except Exception as exc:
        kodi_log(f'MDBList account info error: {type(exc).__name__}', 1)
        Dialog().ok('TMDb Helper', 'MDBList: Could not retrieve account information.')


def simkl_account_info(**kwargs):
    from xbmcgui import Dialog
    from tmdbhelper.lib.addon.logger import kodi_log
    from tmdbhelper.lib.api.simkl.api import SimklAPI

    title = 'TMDb Helper: Simkl Account'
    try:
        api = SimklAPI()
        if not api.user_token.value:
            Dialog().ok('TMDb Helper', 'Simkl: Not authorised - authorise from settings.')
            return
        data = api.get_account_info()
        if not isinstance(data, dict) or not data:
            return _offline(title, 'no valid response from Simkl')
        if not isinstance(data.get('user'), dict) and not isinstance(data.get('account'), dict):
            # e.g. an error body for a rejected token - never present that as an online account
            return _offline(title, 'unexpected response from Simkl - token may be rejected, re-authorise')
        _show(title, format_simkl(data))
    except Exception as exc:
        kodi_log(f'Simkl account info error: {type(exc).__name__}', 1)
        _offline(title, 'Simkl unreachable')
