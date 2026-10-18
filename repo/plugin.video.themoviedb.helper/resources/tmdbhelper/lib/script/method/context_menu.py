# Module: default
# Author: jurialmunkey
# License: GPL v.3 https://www.gnu.org/copyleft/gpl.html
from tmdbhelper.lib.script.method.decorators import is_in_kwargs, get_tmdb_id


@is_in_kwargs({'tmdb_type': True})
@get_tmdb_id
def related_lists(tmdb_id=None, tmdb_type=None, season=None, episode=None, container_update=True, include_play=False, **kwargs):
    from xbmcgui import Dialog
    from xbmc import getInfoLabel
    from tmdbhelper.lib.items.basedir import get_basedir_details
    from tmdbhelper.lib.addon.plugin import format_folderpath, encode_url, executebuiltin
    from tmdbhelper.lib.items.basedir import is_anime_item
    from tmdbhelper.lib.api.tmdb.api import TMDb
    animation = {v: k for k, v in (TMDb().genres or {}).items()}.get(16)   # TMDb's name for the Animation genre in your language (saved locally)
    anime = is_anime_item(getInfoLabel('ListItem.Property(original_language)'), getInfoLabel('ListItem.Genre'), animation)   # the focused item's own data: no lookup
    items = get_basedir_details(tmdb_type=tmdb_type, tmdb_id=tmdb_id, season=season, episode=episode, include_play=include_play, anime=anime)
    if not items or len(items) <= 1:
        return
    choice = Dialog().contextmenu([i.get('label') for i in items])
    if choice == -1:
        return
    item = items[choice]
    params = item.get('params')
    if not params:
        return
    item['params']['tmdb_id'] = tmdb_id
    item['params']['tmdb_type'] = tmdb_type
    if not container_update:
        return item
    path = format_folderpath(
        path=encode_url(path=item.get('path'), **item.get('params')),
        info=item['params']['info'],
        play='RunPlugin',  # Use RunPlugin to avoid window manager info dialog crash with Browse method
        content='pictures' if item['params']['info'] in ['posters', 'fanart'] else 'videos')
    executebuiltin('Dialog.Close(busydialog)')  # Kill modals because prevents ActivateWindow
    executebuiltin(path)
