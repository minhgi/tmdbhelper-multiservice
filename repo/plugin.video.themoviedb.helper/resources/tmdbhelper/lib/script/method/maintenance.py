# Module: default
# Author: jurialmunkey
# License: GPL v.3 https://www.gnu.org/copyleft/gpl.html


def clean_old_databases():
    """ Once-off routine to delete old unused database versions to avoid wasting disk space """
    from tmdbhelper.lib.files.futils import delete_folder
    from tmdbhelper.lib.addon.plugin import get_setting
    for f in ['database', 'database_v2', 'database_v3', 'database_v4', 'database_v5']:
        delete_folder(f, force=True, check_exists=True)
    save_path = get_setting('image_location', 'str')
    for f in ['blur', 'crop', 'desaturate', 'colors']:
        delete_folder(f, force=True, check_exists=True)
        if not save_path:
            continue
        delete_folder(f'{save_path}{f}/', force=True, check_exists=True, join_addon_data=False)


def recache_kodidb(notification=True):
    from tmdbhelper.lib.addon.plugin import ADDONPATH
    from tmdbhelper.lib.api.kodi.rpc import KodiLibrary
    from tmdbhelper.lib.addon.logger import TimerFunc
    from xbmcgui import Dialog
    with TimerFunc('KodiLibrary sync took', inline=True):
        KodiLibrary('movie', cache_refresh=True)
        KodiLibrary('tvshow', cache_refresh=True)
    if not notification:
        return
    Dialog().notification('TMDbHelper', 'Kodi Library cached to memory', icon=f'{ADDONPATH}/icon.png')


def delete_cache(delete_cache, **kwargs):
    from xbmcgui import Dialog
    from tmdbhelper.lib.items.builder import ItemBuilder
    from tmdbhelper.lib.api.fanarttv.api import FanartTV
    from tmdbhelper.lib.api.trakt.api import TraktAPI
    from tmdbhelper.lib.api.tmdb.api import TMDb
    from tmdbhelper.lib.api.omdb.api import OMDb
    from tmdbhelper.lib.addon.plugin import get_localized
    from tmdbhelper.lib.addon.dialog import BusyDialog
    d = {
        'TMDb': lambda: TMDb(),
        'Trakt': lambda: TraktAPI(),
        'FanartTV': lambda: FanartTV(),
        'OMDb': lambda: OMDb(),
        'Item Details': lambda: ItemBuilder()}
    if delete_cache == 'select':
        m = [i for i in d]
        x = Dialog().contextmenu([get_localized(32387).format(i) for i in m])
        if x == -1:
            return
        delete_cache = m[x]
    z = d.get(delete_cache)
    if not z:
        return
    if not Dialog().yesno(get_localized(32387).format(delete_cache), get_localized(32388).format(delete_cache)):
        return
    with BusyDialog():
        z()._cache.ret_cache()._do_delete()
    Dialog().ok(get_localized(32387).format(delete_cache), get_localized(32389))


REGEX_REUSELANGUAGEINVOKER = r'(<reuselanguageinvoker>)(true|false)(</reuselanguageinvoker>)'


def get_reuselanguageinvoker_path():
    import os
    import xbmcvfs
    from tmdbhelper.lib.addon.plugin import ADDONPATH
    return os.path.join(xbmcvfs.translatePath(ADDONPATH), 'addon.xml')


def set_reuselanguageinvoker(enable):
    """ Patch the reuselanguageinvoker tag in addon.xml
    Returns True if the file was changed, False if it already matched, None if it could not be read or written
    """
    import os
    import re
    path = get_reuselanguageinvoker_path()
    try:
        with open(path, 'r', encoding='utf-8', newline='') as f:
            text = f.read()
        match = re.search(REGEX_REUSELANGUAGEINVOKER, text)
        if not match:
            return None
        value = 'true' if enable else 'false'
        if match.group(2) == value:
            return False
        text = f'{text[:match.start(2)]}{value}{text[match.end(2):]}'
        tmp = f'{path}.tmp'
        with open(tmp, 'w', encoding='utf-8', newline='') as f:
            f.write(text)
        os.replace(tmp, path)
    except (OSError, UnicodeError):
        if os.path.exists(f'{path}.tmp'):
            try:
                os.remove(f'{path}.tmp')
            except OSError:
                pass
        return None
    return True


def toggle_reuselanguageinvoker():
    """ Settings button: flip the option in addon.xml and record it in the status setting
    Kodi reads the option from addon.xml at startup only (a profile reload does not apply it), so the user restarts Kodi
    """
    import re
    from xbmcgui import Dialog
    from tmdbhelper.lib.addon.logger import kodi_log
    from tmdbhelper.lib.addon.plugin import ADDONNAME, get_localized, set_setting
    try:
        with open(get_reuselanguageinvoker_path(), 'r', encoding='utf-8', newline='') as f:
            match = re.search(REGEX_REUSELANGUAGEINVOKER, f.read())
    except (OSError, UnicodeError):
        match = None
    if not match:
        Dialog().ok(ADDONNAME, get_localized(32737))
        return
    enable = match.group(2) != 'true'  # The file is the truth - flip what it says now
    if set_reuselanguageinvoker(enable) is None:
        Dialog().ok(ADDONNAME, get_localized(32737))
        return
    set_setting('reuselanguageinvoker_status', 'Enabled' if enable else 'Disabled', 'str')
    kodi_log(f'Re-Use Language Invoker switched {"on" if enable else "off"}', 1)
    Dialog().ok(ADDONNAME, get_localized(32736).format('ON' if enable else 'OFF'))


def sync_reuselanguageinvoker():
    """ Service start: make addon.xml match the status setting
    A new install of the add-on puts the shipped value (off) back, so an enabled choice is restored here
    No dialog and no profile reload (unattended) - a changed file takes effect at the next Kodi start
    """
    from tmdbhelper.lib.addon.logger import kodi_log
    from tmdbhelper.lib.addon.plugin import get_setting
    enable = get_setting('reuselanguageinvoker_status', 'str') == 'Enabled'
    result = set_reuselanguageinvoker(enable)
    if result is None:
        kodi_log('Re-Use Language Invoker: addon.xml could not be read or written', 1)
    elif result:
        kodi_log(f'Re-Use Language Invoker: addon.xml set to {"on" if enable else "off"} to match the setting (applies at the next Kodi start)', 1)
