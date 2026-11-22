import xbmc

# Settings that change the artwork or labels a built widget holds (GitHub issue #9). Widget caches are keyed by them
# (items/widget_cache.py), but a skin only asks for its widgets again when told to, so a change has to nudge the skins.
# The service switches and the watched / resume indicator switches of the three watch services (GitHub issue #13 item 5)
# are NOT in the cache key: a widget built before a service was switched kept its marks (and the skin its rows) until
# Rebuild Database, and a restart served the same cache again (it is reused for 6 h). Listing them here makes a change
# invalidate the widget caches once, so the next build follows the new setting.
WIDGET_SETTINGS = (
    'fanarttv_enfallback', 'fanarttv_secondpref', 'fanarttv_lookup', 'widget_fanarttv_lookup',
    'service_fanarttv_lookup', 'tvdb_artwork', 'force_english_plot_fallback',
    'service_trakt', 'service_mdblist', 'service_simkl',
    'trakt_watchedindicators', 'mdblist_watchedindicators', 'simkl_watchedindicators',
    'trakt_playprogress', 'mdblist_playprogress', 'simkl_playprogress',
    'service_wetrakr', 'wetrakr_watchedindicators', 'wetrakr_playprogress')


PLUGIN_ROOT = 'plugin://plugin.video.themoviedb.helper/'
NO_REFRESH_MENUS = ('dir_search', 'dir_multisearch', 'dir_discover')  # routed outside ListBaseDir (addon/consts.py ROUTE_NOID)


def get_widget_settings_state():
    from tmdbhelper.lib.addon.plugin import get_setting, get_language, get_artwork_quality_key
    # the artwork quality is not a bool (0-3, plus the poster/fanart overrides): its own element, because bool() would not tell 1, 2 and 3 apart.
    # The default level alone (an int) while the overrides add nothing, else 'default.poster.fanart': what the widget caches are keyed by
    return (get_language(),) + tuple(bool(get_setting(i)) for i in WIDGET_SETTINGS) + (get_artwork_quality_key(),)


class UpdateMonitor(xbmc.Monitor):
    """
    Monitors updating Kodi library
    """

    def __init__(self):
        super().__init__()
        try:
            self._widget_settings_state = get_widget_settings_state()
        except Exception:
            self._widget_settings_state = None
        try:
            from tmdbhelper.lib.addon.plugin import get_setting
            self._trakt_on = bool(get_setting('service_trakt'))
        except Exception:
            self._trakt_on = None
        try:
            self._menu_state = self._get_menu_state()
        except Exception:
            self._menu_state = None

    @staticmethod
    def _get_menu_state():
        from tmdbhelper.lib.addon.menustate import get_menu_state_key  # the menu switches and whether each service is signed in (addon/menustate.py)
        return get_menu_state_key()

    def _refresh_menu_if_shown(self):
        """A menu row was switched on or off while one of the add-on's menus is on screen (GitHub issue #13): the list stayed as
        it was built (the Settings row opens the dialog over it), so the new switch showed only after leaving the page. Refresh it."""
        try:
            state = self._get_menu_state()
            if state == self._menu_state:
                return
            self._menu_state = state
            path = xbmc.getInfoLabel('Container.FolderPath')
            if not path.startswith(PLUGIN_ROOT):
                return
            rest = path[len(PLUGIN_ROOT):]
            if 'info=' in rest and 'info=dir_' not in rest:
                return  # a list, not a menu (widgets are handled by reload_widgets)
            if any(f'info={i}' in rest for i in NO_REFRESH_MENUS):
                return  # the search / discover pages are not built from these switches and are not run again unasked
            from tmdbhelper.lib.addon.logger import kodi_log
            kodi_log(f'UpdateMonitor: menu switches changed while {path} is shown, refreshing it', 2)
            xbmc.executebuiltin('Container.Refresh')
        except Exception as exc:
            from tmdbhelper.lib.addon.logger import kodi_log
            kodi_log(f'UpdateMonitor: menu refresh failed: {exc}', 1)

    @staticmethod
    def run_library_tagger():
        from tmdbhelper.lib.addon.thread import SafeThread
        from tmdbhelper.lib.update.tagger import LibraryTagger
        SafeThread(target=LibraryTagger).start()

    def onScanFinished(self, library):
        if library == 'video':
            self.run_library_tagger()

    def _authorize_trakt(self):
        """What the startup job (monitor/cronjob.py _do_trakt_authorization) does, for a Trakt switch turned on while Kodi runs."""
        from jurialmunkey.parser import boolean
        from jurialmunkey.window import get_property
        from tmdbhelper.lib.api.trakt.api import TraktAPI
        TraktAPI().authorize(confirmation=True)
        self.waitForAbort(1)
        if not boolean(get_property('TraktIsAuth')):
            return
        from tmdbhelper.lib.script.method.trakt import get_stats
        get_stats()

    def _check_trakt_switch(self):
        """The Trakt switch was turned on or off while Kodi was running (GitHub issue #13 item 5).
        On: the authorization that sets TraktIsAuth only ran at startup and only with the switch on, so Trakt scrobbling and
        syncing stayed idle until Kodi was restarted; run it now, once per switch-on, off the monitor thread.
        Off: the Trakt scrobbler checks TraktIsAuth and trakt_scrobbling but not the switch, so Trakt kept receiving plays
        until a restart; clear the property so it stops at once."""
        try:
            from tmdbhelper.lib.addon.plugin import get_setting
            on = bool(get_setting('service_trakt'))
            was, self._trakt_on = self._trakt_on, on
            if on and was is False:
                from tmdbhelper.lib.addon.logger import kodi_log
                from tmdbhelper.lib.addon.thread import SafeThread
                kodi_log('UpdateMonitor: Trakt switched on, authorizing it now (no restart needed)', 2)
                SafeThread(target=self._authorize_trakt).start()
            elif was and not on:
                from jurialmunkey.window import get_property
                from tmdbhelper.lib.addon.logger import kodi_log
                get_property('TraktIsAuth', clear_property=True)
                kodi_log('UpdateMonitor: Trakt switched off, scrobbling and syncing to Trakt stop now (no restart needed)', 2)
        except Exception as exc:
            from tmdbhelper.lib.addon.logger import kodi_log
            kodi_log(f'UpdateMonitor: Trakt switch check failed: {exc}', 1)

    def onSettingsChanged(self):
        """Language, artwork or watch-service setting changed: make the skins ask for their widgets again.
        Compared with the last state because the add-on also writes its own settings (tokens, flags) and each write comes here."""
        self._check_trakt_switch()
        self._refresh_menu_if_shown()
        try:
            state = get_widget_settings_state()
            if state == self._widget_settings_state:
                return
            old, self._widget_settings_state = self._widget_settings_state, state
            from tmdbhelper.lib.addon.logger import kodi_log
            kodi_log(f'UpdateMonitor: widget settings changed {old} -> {state}, reloading widgets', 2)  # logged so a hook that never fires is visible
            from tmdbhelper.lib.script.method.kodi_utils import reload_widgets
            reload_widgets(bridge=False)  # a hard invalidation (not a data change of the user's, so no Cross-Service Sync)
        except Exception as exc:
            from tmdbhelper.lib.addon.logger import kodi_log
            kodi_log(f'UpdateMonitor: settings change check failed: {exc}', 1)
