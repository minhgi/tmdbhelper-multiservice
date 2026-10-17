# Module: default
# Author: jurialmunkey
# License: GPL v.3 https://www.gnu.org/copyleft/gpl.html


def _reload_bingie_hub_widgets(timestamp):
    """Cycle the reload= token in Titan Bingie Mod hub widget path skin strings.
    Changing Skin.String(key) causes CDirectoryProvider to re-evaluate <content>
    and re-fetch the widget when the hub window is next rendered — so the badge
    appears as soon as the user returns from the context menu, without navigating away."""
    import re
    import xbmc
    _HUB_NAMES = (
        'movies', 'tvshows', 'new', 'mycustom', 'mylist', 'mycustomlist',
        'pvr', 'categories', 'something', 'category2', 'myhub',
    )
    _SLOT_IDS = ('510', '520', '530', '540', '550', '560', '570', '580')
    for hub in _HUB_NAMES:
        for slot in _SLOT_IDS:
            key = f'bingiehub-{hub}-{slot}.path'
            current = xbmc.getInfoLabel(f'Skin.String({key})')
            if not current or 'plugin.video.themoviedb.helper' not in current:
                continue
            new_url = re.sub(r'&reload=[^&]*', '', current)
            xbmc.executebuiltin(f'Skin.SetString({key},{new_url}&reload={timestamp})')


def reload_widgets(soft=False, invalidate=True):
    """soft=True: a background sync changed data (the widget cache may be served stale while the service rebuilds it).
    invalidate=False: only nudge the skins to re-request (used by the widget warmer once its caches are fresh)."""
    """Set widget reload properties for both TMDbHelper and common skin property names.
    TMDbHelper uses TMDbHelper.Widgets.Reload (via jurialmunkey prefix).
    Many skins use their own property names (e.g. widgetreload-movies, widgetreload).
    Setting both ensures widgets actually re-fetch when Trakt data changes."""
    from tmdbhelper.lib.addon.tmdate import set_timestamp
    from jurialmunkey.window import get_property
    import xbmcgui
    import time
    timestamp = f'{set_timestamp(0, True)}'
    get_property('Widgets.Reload', set_property=timestamp)
    home = xbmcgui.Window(10000)
    home.setProperty('widgetreload-movies', timestamp)
    home.setProperty('widgetreload-tvshows', timestamp)
    home.setProperty('widgetreload', timestamp)
    home.setProperty('widgetreload2', timestamp)
    # Invalidate widget directory cache so next request runs full pipeline
    if invalidate:
        home.setProperty('TMDbHelper.Widgets.LastInvalidated', str(time.time()))
        if not soft:
            home.setProperty('TMDbHelper.Widgets.HardInvalidated', str(time.time()))
    _reload_bingie_hub_widgets(timestamp)


def container_refresh():
    from tmdbhelper.lib.addon.plugin import executebuiltin
    executebuiltin('Container.Refresh')
    reload_widgets()


def split_value(split_value, separator=None, **kwargs):
    """ Split string values and output to window properties """
    from jurialmunkey.window import get_property
    if not split_value:
        return
    v = f'{split_value}'
    s = separator or ' / '
    p = kwargs.get("property") or "TMDbHelper.Split"
    for x, i in enumerate(v.split(s)):
        get_property(f'{p}.{x}', set_property=i, prefix=-1)


def kodi_setting(kodi_setting, **kwargs):
    """ Get Kodi setting value and output to window property """
    from tmdbhelper.lib.api.kodi.rpc import get_jsonrpc
    from jurialmunkey.window import get_property
    method = "Settings.GetSettingValue"
    params = {"setting": kodi_setting}
    response = get_jsonrpc(method, params)
    get_property(
        name=kwargs.get('property') or 'KodiSetting',
        set_property=f'{response.get("result", {}).get("value", "")}')


def do_wikipedia_gui(wikipedia, tmdb_type=None, **kwargs):
    """ Reroutes to wikipedia script: Included for legacy purposes """
    from xbmc import executebuiltin
    from tmdbhelper.lib.addon.plugin import get_language
    language = get_language()[:2]
    cmd = f'script.wikipedia,wikipedia={wikipedia},xml_file=script-tmdbhelper-wikipedia.xml'
    if tmdb_type:
        cmd = f'{cmd},tmdb_type={tmdb_type}'
    if language:
        cmd = f'{cmd},language={language}'
    executebuiltin(f'RunScript({cmd})')
