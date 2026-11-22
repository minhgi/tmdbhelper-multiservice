from tmdbhelper.lib.addon.permissions import __access__
from tmdbhelper.lib.api.api_keys.tokenhandler import TokenHandler

# WeTrakr hands its app key (client id) to the developer of each app, and users of an app never need their own: TMDb Helper's own app key
# is built in (BUILTIN_CLIENT_ID, from 7.5.12) so a user only has to authorize. The setting wetrakr_clientid is an optional override for
# someone who wants to use their own WeTrakr developer app. The sign-in uses the device flow, which needs the client id only: it is a
# public client id (WeTrakr's docs: apps users install are proved by the user's approval, not a secret) and a client secret is never used
# or stored. Other add-ons importing this module get empty values (there is no "allow 3rd party access" toggle for WeTrakr).
if __access__.has_access('internal') or __access__.has_access('wetrakr'):
    BUILTIN_CLIENT_ID = '90630f7b0f1ffa5d0b8079a22ebe2d6c'
    CLIENT_ID = TokenHandler('wetrakr_clientid', store_as='setting')
    USER_TOKEN = TokenHandler('wetrakr_token', store_as='setting')
    REFRESH_TOKEN = TokenHandler('wetrakr_refresh_token', store_as='setting')
    TOKEN_EXPIRES = TokenHandler('wetrakr_token_expires', store_as='setting')
else:
    BUILTIN_CLIENT_ID = ''
    CLIENT_ID = TokenHandler()
    USER_TOKEN = TokenHandler()
    REFRESH_TOKEN = TokenHandler()
    TOKEN_EXPIRES = TokenHandler()
