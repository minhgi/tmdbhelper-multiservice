from tmdbhelper.lib.addon.permissions import __access__
from tmdbhelper.lib.addon.plugin import get_setting
from tmdbhelper.lib.api.api_keys.tokenhandler import TokenHandler

if __access__.has_access('internal') or __access__.has_access('mdblist'):
    API_KEY = get_setting('mdblist_apikey', 'str')
    CLIENT_ID = 'Jfx43IWpbKcEOoRdnjgZ00eBpcKCRM4mVHALZSc4'
    USER_TOKEN = TokenHandler('mdblist_token', store_as='setting')
else:
    API_KEY = ''
    CLIENT_ID = ''
    USER_TOKEN = TokenHandler()
