from tmdbhelper.lib.addon.permissions import __access__
from tmdbhelper.lib.api.api_keys.tokenhandler import TokenHandler

if __access__.has_access('internal') or __access__.has_access('simkl'):
    CLIENT_ID = '62a587ec2a82dbed02c6ab48b923d72e775cb1096d2de60d04502413e36ef100'
    USER_TOKEN = TokenHandler('simkl_token', store_as='setting')
else:
    CLIENT_ID = ''
    USER_TOKEN = TokenHandler()
