""" What decides the rows of the add-on's own menus, as a short text (GitHub issue #13). A menu page is stamped with it when it is built; the
service compares the stamp of the page on screen with the current text (monitor/menurefresh.py). Only 0 and 1: no token or key ever goes in. """

# The switches that show or hide a row of the add-on's own menus (keep in step with SERVICE_MENUS in items/basedir.py)
MENU_SETTINGS = ('service_trakt', 'service_mdblist', 'service_simkl', 'menu_movies', 'menu_tv', 'menu_anime', 'menu_asian')
# Whether each service is signed in (the Sync Tools row needs one): only if the setting is filled in, never what it holds
TOKEN_SETTINGS = ('trakt_token', 'mdblist_token', 'mdblist_apikey', 'simkl_token')
MENU_STATE_PROPERTY = 'MenuState'  # Container.Property(MenuState) on a menu page


def menu_fast_back_enabled():
    from tmdbhelper.lib.addon.plugin import get_setting
    return bool(get_setting('menu_fast_back'))


def get_menu_state_key():
    from tmdbhelper.lib.addon.plugin import get_setting
    switches = ''.join('1' if get_setting(i) else '0' for i in MENU_SETTINGS)
    signed_in = ''.join('1' if get_setting(i, 'str') else '0' for i in TOKEN_SETTINGS)
    return f'{switches}.{signed_in}'
