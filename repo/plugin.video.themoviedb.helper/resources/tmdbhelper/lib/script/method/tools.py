# Actions of the Tools menu (items/basedir.py _get_basedir_tools): Seren's Sync Tools, Clear Cache Tool, Open Settings Menu,
# Backup and Reset and Changelog, done with TMDb Helper's own functions.
#
# Sync tools work on the three local sync databases (TraktSync_v2.db, MDbListSync.db, SimklSync.db: tables simplecache +
# lactivities). The tables are emptied, never the files (the "initialised" window flag would stop a deleted file being
# recreated). Nothing here signs in, signs out or writes to a service, and the bridge snapshot file is never touched. Clear
# keeps the version markers (see _clear_stamps) so the bridge stays ready; Rebuild empties everything and syncs at once
# (the bridge also re-syncs Trakt itself each cycle if a marker is ever missing).
from tmdbhelper.lib.addon.logger import kodi_log
from tmdbhelper.lib.addon.plugin import ADDON, ADDONDATA, get_setting

TITLE = 'TMDb Helper'
SERVICES = (('Trakt', 'trakt_token'), ('MDBList', 'mdblist_token'), ('Simkl', 'simkl_token'))
BRIDGE_RUNNING_PROP = 'TMDbHelper.BridgeSync.Running'   # window property monitor/bridge_sync.py sets while a cycle runs
LIST_CACHES = ('asian_trending_cache.json', 'anime_anilist_cache.json', 'anime_idmap_cache.json')   # the Asian / Anime list caches + saved AniList->TMDb ids (addon_data)
# Same data types as script/method/trakt.py refresh_trakt_sync offers: (sync keys, item types)
TRAKT_FORCE = (
    (('hidden_at',), ('movie', 'show')),
    (('last_watched_at',), ('movie', 'show', 'episode')),
    (('playback_paused_at',), ('movie', 'episode')),
    (('rated_at',), ('movie', 'show', 'season', 'episode')),
    (('favorites_listed_at',), ('movie', 'show')),
    (('watchlist_listed_at',), ('movie', 'show', 'season', 'episode')),
    (('collection_last_collected_at',), ('movie', 'show')))
# The calls items/mdblist.py and items/simkl.py make when an item is opened: (item type, sync keys)
MDBLIST_FORCE = (('movie', ('plays', 'paused_progress')), ('show', ('plays', 'watched_episodes', 'paused_progress')))
SIMKL_FORCE = (('movie', ('plays', 'paused_progress')), ('show', ('plays', 'watched_episodes')))


def connected_services():
    """Names of the services that have a stored sign-in (local settings only: never builds an API object, never logs in)."""
    return [name for name, setting in SERVICES if get_setting(setting, 'str')]


def _syncdata(name):
    if name == 'Trakt':
        from tmdbhelper.lib.api.trakt.api import TraktAPI
        return TraktAPI().trakt_syncdata
    if name == 'MDBList':
        from tmdbhelper.lib.api.mdblist.api import MDbList
        return MDbList().mdblist_syncdata
    from tmdbhelper.lib.api.simkl.api import SimklAPI
    return SimklAPI().simkl_syncdata


def _clear_stamps(syncdata):
    # lactivities holds the last-sync timestamps AND version markers (paged_v1, watched_progress_v1, status_v1/2: every id
    # ending _v<digits>) that say the stored data is complete. Only the timestamps go, so the next sync re-checks everything
    # without the full refetch / per-show progress rebuild a missing marker forces, and the bridge keeps reading Trakt.
    done = syncdata.cache.execute_sql("DELETE FROM lactivities WHERE id NOT GLOB '*_v[0-9]*'") is not None
    syncdata.reset_lastactivities()
    return done


def _wipe(syncdata):
    done = all(syncdata.cache.execute_sql(f'DELETE FROM {table}') is not None for table in ('simplecache', 'lactivities'))
    syncdata.reset_lastactivities()
    return done


def _force(name, syncdata):
    if name == 'Trakt':
        for keys, item_types in TRAKT_FORCE:
            for item_type in item_types:
                syncdata.sync(item_type, keys, forced=True)
        return True
    for item_type, keys in (MDBLIST_FORCE if name == 'MDBList' else SIMKL_FORCE):
        syncdata.sync(item_type, keys, forced=True)
    return True


def _for_each_service(action, func):
    """Run func(name, syncdata) for every connected service; (names that worked, names that failed). One failing
    service never stops the others."""
    done, failed = [], []
    for name in connected_services():
        try:
            ok = func(name, _syncdata(name))
        except Exception as exc:
            ok = False
            kodi_log(f'Tools {action}: {name} failed: {type(exc).__name__}: {exc}', 2)
        (done if ok else failed).append(name)
        kodi_log(f'Tools {action}: {name} {"done" if ok else "FAILED"}', 1)
    return done, failed


def _report(heading, done, failed):
    from xbmcgui import Dialog
    from tmdbhelper.lib.addon.plugin import ADDONPATH
    text = ', '.join(done) if done else 'nothing to do'
    if failed:
        text = f'{text} - failed: {", ".join(failed)} (see the log)'
    Dialog().notification(TITLE, f'{heading}: {text}', icon=f'{ADDONPATH}/icon.png')


def _refresh_widgets():
    from tmdbhelper.lib.script.method.kodi_utils import reload_widgets
    reload_widgets()


def _bridge_busy():
    from jurialmunkey.window import get_property
    from xbmcgui import Dialog
    if not get_property(BRIDGE_RUNNING_PROP):
        return False
    Dialog().ok(TITLE, 'A library sync between your services is running right now. Try again in a minute or two.')
    return True


def sync_clear():
    """Clear last sync time: forget when each service was last synced; the next sync re-checks everything."""
    from xbmcgui import Dialog
    if not connected_services() or _bridge_busy():
        return
    if not Dialog().yesno(TITLE, 'Clear the last sync time of every connected service?\n\n'
                                 'Nothing is deleted. Each service re-checks everything on its next sync.'):
        return
    done, failed = _for_each_service('clear last sync time', lambda name, syncdata: _clear_stamps(syncdata))
    _refresh_widgets()
    _report('Cleared last sync time', done, failed)


def sync_force():
    """Force a sync: sync every connected service now, ignoring what it says has not changed."""
    from tmdbhelper.lib.addon.dialog import BusyDialog
    if not connected_services() or _bridge_busy():
        return
    with BusyDialog():
        done, failed = _for_each_service('force sync', _force)
    _refresh_widgets()
    _report('Synced', done, failed)


def sync_rebuild():
    """Rebuild Database: empty every connected service's sync tables, then sync again from scratch."""
    from xbmcgui import Dialog
    from tmdbhelper.lib.addon.dialog import BusyDialog
    if not connected_services() or _bridge_busy():
        return
    if not Dialog().yesno(TITLE, 'Remove all synced data and sync it again from scratch?\n\n'
                                 'Nothing on the services changes. Takes minutes and many requests.'):
        return

    def rebuild(name, syncdata):
        if not _wipe(syncdata):
            return False
        return _force(name, syncdata)

    with BusyDialog():
        done, failed = _for_each_service('rebuild database', rebuild)
    _refresh_widgets()
    _report('Rebuilt', done, failed)


def clear_cache():
    from tmdbhelper.lib.script.method.maintenance import delete_cache
    delete_cache('select')


def clear_search_history():
    from xbmcgui import Dialog
    from tmdbhelper.lib.api.tmdb.search import MULTISEARCH_TYPES
    from tmdbhelper.lib.files.hcache import set_search_history
    if not Dialog().yesno(TITLE, 'Clear your search history?'):
        return
    for tmdb_type in MULTISEARCH_TYPES:
        set_search_history(tmdb_type=tmdb_type, clear_cache=True)
    Dialog().notification(TITLE, 'Search history cleared')


def clear_list_caches():
    """Clear the Asian Drama / Asian Movies / Anime lists' saved results (they are rebuilt the next time a list opens)."""
    import os
    import xbmcvfs
    from xbmcgui import Dialog
    if not Dialog().yesno(TITLE, 'Clear the saved results of the Anime and Asian lists, including the saved AniList to TMDb matches? They are rebuilt the next time a list is opened.'):
        return
    base = xbmcvfs.translatePath(ADDONDATA)
    removed = 0
    for filename in LIST_CACHES:
        path = os.path.join(base, filename)
        if os.path.isfile(path):
            try:
                os.remove(path)
                removed += 1
            except OSError as exc:
                kodi_log(f'Tools: could not delete {filename}: {exc}', 2)
    Dialog().notification(TITLE, f'Anime and Asian list cache cleared ({removed} file{"" if removed == 1 else "s"})')


def open_settings():
    ADDON.openSettings()


def backup():
    from tmdbhelper.lib.script.method.backup_restore import backup_settings
    backup_settings()


def restore():
    from tmdbhelper.lib.script.method.backup_restore import restore_settings
    restore_settings()


def changelog():
    from xbmcgui import Dialog
    text = ADDON.getAddonInfo('changelog')
    text = '\n'.join(line.strip() for line in text.splitlines()).strip()
    Dialog().textviewer(f'{TITLE} v{ADDON.getAddonInfo("version")} - Changelog', text or 'No changelog available.')


ACTIONS = {
    'sync_clear': sync_clear,
    'sync_force': sync_force,
    'sync_rebuild': sync_rebuild,
    'clear_cache': clear_cache,
    'clear_search_history': clear_search_history,
    'clear_list_caches': clear_list_caches,
    'open_settings': open_settings,
    'backup': backup,
    'restore': restore,
    'changelog': changelog,
}


def run(action=None):
    func = ACTIONS.get(action)
    if func:
        func()
