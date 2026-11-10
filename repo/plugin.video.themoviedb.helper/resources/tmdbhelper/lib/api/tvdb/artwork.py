""" TheTVDB as an optional, gap-fill source for clearlogo and fanart.

Built to be cheap on slow devices (Android TV):
- never logs in or makes a request unless a lookup is actually needed (no work in __init__)
- short request timeout, and a cool-down (no requests at all, cached answers still served) after any failed login,
  timeout, bad token, HTTP 429 or HTTP 5xx
- every successful answer (including "TVDB has nothing") is cached for weeks, so an item is asked about once
- a failed request is NOT cached as "nothing", so it is retried later (after the cool-down)
- cache_only mode reads the cache and never touches the network
"""
from tmdbhelper.lib.api.request import RequestAPI
from tmdbhelper.lib.addon.tmdate import set_timestamp, get_timestamp
from tmdbhelper.lib.api.api_keys.tvdb import API_KEY, USER_TOKEN
from jurialmunkey.window import get_property
import threading

API_URL = 'https://api4.thetvdb.com/v4'
ARTWORK_URL = 'https://artworks.thetvdb.com'
REQUEST_TIMEOUT = 4  # seconds per request (the shared client uses 15)
FAIL_COOLDOWN = 300  # seconds to leave TVDB alone after a failed login, timeout, bad token, 429 or 5xx
CACHE_DAYS = 30  # artwork answers
CACHE_DAYS_TYPES = 90  # artwork type table
CACHE_VERSION = 'tvdbart.v2'  # v2: digest entries are [image, thumbnail]
THUMB_SUFFIX = '.thumb'  # art key of the smaller TVDB image, e.g. 'fanart.thumb' (items/builder.py picks full or thumb by the artwork quality setting)
LOGIN_FAIL_PROP = 'TVDbArt.LoginFail'
_TOKEN_LOCK = threading.Lock()  # list items are built in parallel threads sharing one client: log in once, not once per item

# Kodi language (first two letters) -> TVDB language codes (TVDB uses 3 letter codes; a few have an extra spelling)
LANGUAGE_CODES = {
    'en': {'eng'}, 'fr': {'fra', 'fre'}, 'de': {'deu', 'ger'}, 'es': {'spa'}, 'it': {'ita'},
    'pt': {'por', 'pt'}, 'nl': {'nld', 'dut'}, 'ja': {'jpn'}, 'ko': {'kor'}, 'zh': {'zho', 'chi', 'zhtw', 'yue'},
    'ru': {'rus'}, 'pl': {'pol'}, 'sv': {'swe'}, 'da': {'dan'}, 'no': {'nor'}, 'nb': {'nor'}, 'fi': {'fin'},
    'tr': {'tur'}, 'cs': {'ces', 'cze'}, 'hu': {'hun'}, 'el': {'ell', 'gre'}, 'he': {'heb'}, 'ar': {'ara'},
    'hi': {'hin'}, 'th': {'tha'}, 'uk': {'ukr'}, 'ro': {'ron', 'rum'}, 'bg': {'bul'}, 'hr': {'hrv'},
    'sk': {'slk', 'slo'}, 'sl': {'slv'}, 'sr': {'srp'}, 'vi': {'vie'}, 'id': {'ind'}, 'ca': {'cat'},
    'lt': {'lit'}, 'lv': {'lav'}, 'et': {'est'}, 'is': {'isl', 'ice'}, 'fa': {'fas', 'per'}, 'ms': {'msa', 'may'},
}


def get_language_codes(language):
    """ 'fr-FR' / 'fr' -> {'fra', 'fre'}; unknown language -> empty set (own-language step is skipped) """
    return LANGUAGE_CODES.get(f'{language or ""}'[:2].lower(), set())


def _full_url(image):
    if not image or not isinstance(image, str):
        return
    if image.lower().endswith('.svg'):  # Kodi cannot use these
        return
    if image.startswith('//'):
        return f'https:{image}'
    if image.startswith('/'):
        return f'{ARTWORK_URL}{image}'
    return image


def _score(record):
    try:
        return float(record.get('score') or 0)
    except (TypeError, ValueError):
        return 0


def resolve_type_ids(types, record_type):
    """ /artwork/types rows -> {'logo': id, 'bg': id} for 'series' or 'movie'.
    Matched on recordType + name, never hard-coded. Missing either one -> {} (the source then adds nothing). """
    ids = {}
    for i in types or []:
        try:
            if f'{i.get("recordType")}'.lower() != record_type:
                continue
            name = f'{i.get("name")}'.replace(' ', '').lower()
            if name == 'clearlogo':
                ids.setdefault('logo', i['id'])
            elif name == 'background':
                ids.setdefault('bg', i['id'])
        except (AttributeError, KeyError, TypeError):
            continue
    return ids if len(ids) == 2 else {}


def digest_artworks(artworks, type_ids):
    """ Reduce a TVDB artwork list to the best (highest score) image per language for logos and backgrounds.
    Language key is '' for artwork without a language. Each entry is [image url, thumbnail url or None].
    The result is what gets cached, so it stays small. """
    out = {'logo': {}, 'bg': {}}
    best = {'logo': {}, 'bg': {}}
    for a in artworks or []:
        try:
            kind = 'logo' if a.get('type') == type_ids['logo'] else 'bg' if a.get('type') == type_ids['bg'] else None
            if not kind:
                continue
            url = _full_url(a.get('image'))
            if not url:
                continue
            lang = f'{a.get("language") or ""}'.lower()
            score = _score(a)
            if lang in best[kind] and best[kind][lang] >= score:
                continue
            best[kind][lang] = score
            out[kind][lang] = [url, _full_url(a.get('thumbnail'))]
        except (AttributeError, KeyError, TypeError):
            continue
    return out


LIST_CACHE_VERSION = 'tvdbart.list.v1'  # the Artwork Manager's full list: its own cache, the gap-fill digest above is untouched
LIST_KINDS = {'poster': 'poster', 'background': 'fanart', 'banner': 'banner', 'clearart': 'clearart', 'clearlogo': 'clearlogo'}  # TVDB type name (spaces removed, lower case) -> Artwork Manager art type. TVDB has nothing like landscape


def resolve_list_type_ids(types, record_type):
    """ /artwork/types rows -> {type id: art type} for 'series' or 'movie' (poster, background, banner, clearart, clearlogo).
    Matched on recordType + name, never hard-coded. Season, episode, actor, icon and other types are left out. """
    ids = {}
    for i in types or []:
        try:
            if f'{i.get("recordType")}'.lower() != record_type:
                continue
            kind = LIST_KINDS.get(f'{i.get("name")}'.replace(' ', '').lower())
            if kind:
                ids[i['id']] = kind
        except (AttributeError, KeyError, TypeError):
            continue
    return ids


def _int(value):
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def slim_artworks(artworks, type_kinds):
    """ TVDB artwork list -> [[art type, image url, thumbnail url or '', language code or '', score, width, height], ...] for the types in type_kinds.
    Only what the Artwork Manager shows is kept, so the cached list stays small (a long-running show has hundreds of records). """
    out = []
    for a in artworks or []:
        try:
            kind = type_kinds.get(a.get('type'))
            if not kind:
                continue
            url = _full_url(a.get('image'))
            if not url:
                continue
            out.append([kind, url, _full_url(a.get('thumbnail')) or '', f'{a.get("language") or ""}'.lower(), _score(a), _int(a.get('width')), _int(a.get('height'))])
        except (AttributeError, KeyError, TypeError):
            continue
    return out


def pick_art(digest, language):
    """ digest (from digest_artworks, cached) + Kodi language -> {'clearlogo': url, 'fanart': url} (keys only when found).
    Logo: own language, then English, then no language. Other languages are skipped.
    Fanart: no language (textless) first, then own language. Backgrounds with text in another language are skipped.
    When TVDB gave a thumbnail, it is added as 'clearlogo.thumb' / 'fanart.thumb' next to the full image. """
    own = get_language_codes(language)
    art = {}

    def put(key, entry):
        if isinstance(entry, str):  # a bare url (no thumbnail)
            entry = [entry]
        art[key] = entry[0]
        if len(entry) > 1 and entry[1]:
            art[f'{key}{THUMB_SUFFIX}'] = entry[1]

    logos = (digest or {}).get('logo') or {}
    for group in (own, {'eng'}, {''}):
        entry = next((logos[k] for k in sorted(group) if logos.get(k)), None)
        if entry:
            put('clearlogo', entry)
            break
    bgs = (digest or {}).get('bg') or {}
    for group in ({''}, own):
        entry = next((bgs[k] for k in sorted(group) if bgs.get(k)), None)
        if entry:
            put('fanart', entry)
            break
    return art


class TVDbArtwork(RequestAPI):
    """ Looks up clearlogo/fanart for one movie (by IMDb id) or one show (by TVDB id). Uses the same token as the TVDb list client. """

    def __init__(self, cache_only=False, cache_refresh=False):
        super(TVDbArtwork, self).__init__(
            req_api_name='TVDb',  # same name as the list client: shared cache file and shared connection back-off
            req_api_url=API_URL,
            timeout=REQUEST_TIMEOUT,
            error_notification=False)
        self.cache_only = cache_only
        self.cache_refresh = cache_refresh
        self._headers_set = False

    # -- auth ----------------------------------------------------------------------------------------------------
    def _in_cooldown(self):
        return bool(get_timestamp(get_property(LOGIN_FAIL_PROP, is_type=float) or 0))

    def _start_cooldown(self):
        get_property(LOGIN_FAIL_PROP, set_timestamp(FAIL_COOLDOWN))

    def _login(self):
        data = self.get_api_request_json(
            self.get_request_url('login'), postdata={'apikey': API_KEY}, method='json')
        try:
            token = data['data']['token'] if data and data.get('status') == 'success' else None
        except (KeyError, TypeError):
            token = None
        if token:
            USER_TOKEN.value = token
        return token

    def _ensure_headers(self):
        """ True when a token is available. Logs in at most once per cool-down period. """
        if self._headers_set:
            return True
        with _TOKEN_LOCK:
            if self._headers_set:
                return True
            token = USER_TOKEN.value  # read inside the lock: another thread may just have logged in
            if not token:
                if self._in_cooldown():
                    return False
                token = self._login()
                if not token:
                    self._start_cooldown()
                    return False
            self.headers = {'Authorization': f'Bearer {token}'}
            self._headers_set = True
            return True

    # -- requests ------------------------------------------------------------------------------------------------
    def _fetch(self, *args, **kwargs):
        """ One GET. Returns (status, data): 'ok' + the data, 'missing' (HTTP 404: TVDB does not know it), or 'fail'.
        Only 'ok' and 'missing' may be cached; 'fail' (timeout, server error, bad token) must be retried later. """
        if get_timestamp(self.req_connect_err) or self._in_cooldown() or not self._ensure_headers():
            return 'fail', None
        response = self.get_simple_api_request(self.get_request_url(*args, **kwargs), headers=self.headers)
        if response is None:  # timeout or connection error: pause TVDB so a slow server cannot stall every item
            self._start_cooldown()
            return 'fail', None
        try:
            code = response.status_code
            if code == 200:
                try:
                    return 'ok', response.json().get('data')
                except (ValueError, AttributeError):
                    return 'fail', None
            if code == 404:
                return 'missing', None
            if code == 401:  # token no longer valid: drop it, one fresh login after the cool-down
                USER_TOKEN.value = ''
                self._headers_set = False
                self._start_cooldown()
            elif code == 429:
                self.connection_error(code)
                self._start_cooldown()
            elif code >= 500:
                self.connection_error(code, wait_time=300 if code == 503 else 60)
                self._start_cooldown()
            return 'fail', None
        finally:
            response.close()

    def _get_type_ids(self, record_type):
        name = f'{CACHE_VERSION}.types.{record_type}'
        ids = self._cache.get_cache(name)
        if ids is not None or self.cache_only:
            return ids or {}
        status, rows = self._fetch('artwork', 'types')
        if status != 'ok' or not isinstance(rows, list):
            return {}
        ids = resolve_type_ids(rows, record_type)
        if ids:  # an unresolved table is not cached, so a changed API gets picked up without waiting
            self._cache.set_cache(ids, name, cache_days=CACHE_DAYS_TYPES)
        return ids

    def _get_digest(self, cache_name, record_type, fetch):
        digest = None if self.cache_refresh else self._cache.get_cache(cache_name)
        if digest is not None or self.cache_only:
            return digest
        type_ids = self._get_type_ids(record_type)
        if not type_ids:
            return
        status, artworks = fetch()
        if status == 'fail':
            return
        digest = digest_artworks(artworks if status == 'ok' else [], type_ids)  # 'missing' caches an empty digest
        self._cache.set_cache(digest, cache_name, cache_days=CACHE_DAYS)
        return digest

    def _fetch_series_artworks(self, tvdb_id):
        status, data = self._fetch('series', tvdb_id, 'artworks')
        return status, (data or {}).get('artworks') if isinstance(data, dict) else None

    def _fetch_movie_artworks(self, imdb_id):
        status, found = self._fetch('search', 'remoteid', imdb_id)
        if status != 'ok':
            return status, None
        movie_id = next((i['movie']['id'] for i in found or [] if isinstance(i, dict) and isinstance(i.get('movie'), dict) and i['movie'].get('id')), None)
        if not movie_id:
            return 'missing', None
        status, data = self._fetch('movies', movie_id, 'extended')
        return status, (data or {}).get('artworks') if isinstance(data, dict) else None

    def get_series_digest(self, tvdb_id):
        return self._get_digest(f'{CACHE_VERSION}.series.{tvdb_id}', 'series', lambda: self._fetch_series_artworks(tvdb_id))

    def get_movie_digest(self, imdb_id):
        return self._get_digest(f'{CACHE_VERSION}.movie.{imdb_id}', 'movie', lambda: self._fetch_movie_artworks(imdb_id))

    def _get_list_type_kinds(self, record_type):
        name = f'{LIST_CACHE_VERSION}.types.{record_type}'
        cached = self._cache.get_cache(name)
        if cached is not None or self.cache_only:
            return {int(k): v for k, v in (cached or {}).items()}
        status, rows = self._fetch('artwork', 'types')
        if status != 'ok' or not isinstance(rows, list):
            return {}
        kinds = resolve_list_type_ids(rows, record_type)
        if kinds:  # an unresolved table is not cached, so a changed API gets picked up without waiting
            self._cache.set_cache({str(k): v for k, v in kinds.items()}, name, cache_days=CACHE_DAYS_TYPES)
        return kinds

    def get_artwork_list(self, tmdb_type, unique_ids):
        """ Every poster, background (fanart), banner, clearart and clearlogo TVDB holds for a movie (by IMDb id) or a show (by TVDB id), for the
        Artwork Manager: [[art type, image, thumbnail, language, score, width, height], ...] (possibly empty), or None when TVDB could not be
        reached / nothing is cached in cache_only mode. Season art is not included: the answer does not say which season a record belongs to. """
        if tmdb_type == 'tv':
            record_type, key, fetch = 'series', (unique_ids or {}).get('tvdb'), self._fetch_series_artworks
        elif tmdb_type == 'movie':
            record_type, key, fetch = 'movie', (unique_ids or {}).get('imdb'), self._fetch_movie_artworks
        else:
            return []
        if not key:
            return []
        name = f'{LIST_CACHE_VERSION}.{record_type}.{key}'
        cached = None if self.cache_refresh else self._cache.get_cache(name)
        if cached is not None or self.cache_only:
            return cached
        type_kinds = self._get_list_type_kinds(record_type)
        if not type_kinds:
            return
        status, artworks = fetch(key)
        if status == 'fail':
            return
        result = slim_artworks(artworks if status == 'ok' else [], type_kinds)  # 'missing' caches an empty list
        self._cache.set_cache(result, name, cache_days=CACHE_DAYS)
        return result

    def get_artwork(self, tmdb_type, unique_ids, language):
        """ Returns {'clearlogo': .., 'fanart': ..} (possibly empty) when TVDB answered (or the cache has it),
        or None when it could not be reached / the answer is not cached yet in cache_only mode. """
        if tmdb_type == 'tv':
            tvdb_id = (unique_ids or {}).get('tvdb')
            digest = self.get_series_digest(tvdb_id) if tvdb_id else {}
        elif tmdb_type == 'movie':
            imdb_id = (unique_ids or {}).get('imdb')
            digest = self.get_movie_digest(imdb_id) if imdb_id else {}
        else:
            digest = {}
        if digest is None:
            return
        return pick_art(digest, language)
