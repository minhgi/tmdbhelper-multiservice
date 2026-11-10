import xbmc

# Settings that change the artwork or labels a built widget holds (GitHub issue #9). Widget caches are keyed by them
# (items/widget_cache.py), but a skin only asks for its widgets again when told to, so a change has to nudge the skins.
WIDGET_SETTINGS = (
    'fanarttv_enfallback', 'fanarttv_secondpref', 'fanarttv_lookup', 'widget_fanarttv_lookup',
    'service_fanarttv_lookup', 'tvdb_artwork')


def get_widget_settings_state():
    from tmdbhelper.lib.addon.plugin import get_setting, get_language
    return (get_language(),) + tuple(bool(get_setting(i)) for i in WIDGET_SETTINGS)


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

    @staticmethod
    def run_library_tagger():
        from tmdbhelper.lib.addon.thread import SafeThread
        from tmdbhelper.lib.update.tagger import LibraryTagger
        SafeThread(target=LibraryTagger).start()

    def onScanFinished(self, library):
        if library == 'video':
            self.run_library_tagger()

    def onSettingsChanged(self):
        """Language or artwork setting changed: make the skins ask for their widgets again.
        Compared with the last state because the add-on also writes its own settings (tokens, flags) and each write comes here."""
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
