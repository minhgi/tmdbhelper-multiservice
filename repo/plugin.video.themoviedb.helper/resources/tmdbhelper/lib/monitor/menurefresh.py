import xbmc
from tmdbhelper.lib.addon.thread import SafeThread

MENU_POLL_TIME = 0.5  # seconds between looks at the page on screen
PLUGIN_ROOT = 'plugin://plugin.video.themoviedb.helper/'
NO_REFRESH_MENUS = ('dir_search', 'dir_multisearch', 'dir_discover')  # prompt for input: never refreshed unasked


class MenuStaleMonitor(SafeThread):
    """Service thread (GitHub issue #13, setting "Keep menu pages for a quick Back"). Kodi may keep a menu page it was slow to build and show that saved
    copy again on Back without asking the add-on. Each menu page carries a stamp of the switches it was built under (addon/menustate.py); when the page
    on screen was built under other switches than the current ones (a service or main-menu row switched, a service signed in or out meanwhile), it is
    refreshed once. A page without a stamp (built before the update, or by another route) is left alone. This is its own thread, not part of the
    poller: the poller sits in a 30 s holding loop for a skin that does not use the service monitor. """

    def __init__(self, parent):
        SafeThread.__init__(self)
        self.exit = False
        self.monitor = parent.update_monitor
        self._last = None  # (path, stamp, current) already refreshed: asked at most once, so a page that keeps its stamp can never loop

    @property
    def _should_stop(self):
        return self.exit or self.monitor.abortRequested()

    def _tick(self):
        from tmdbhelper.lib.addon.menustate import menu_fast_back_enabled, get_menu_state_key, MENU_STATE_PROPERTY
        if not menu_fast_back_enabled():
            return
        if not xbmc.getCondVisibility('Window.IsMedia') or xbmc.getCondVisibility('Container.IsUpdating'):
            return
        path = xbmc.getInfoLabel('Container.FolderPath')
        if not path.startswith(PLUGIN_ROOT) or any(f'info={i}' in path for i in NO_REFRESH_MENUS):
            return
        stamp = xbmc.getInfoLabel(f'Container.Property({MENU_STATE_PROPERTY})')
        if not stamp:
            return
        current = get_menu_state_key()
        if stamp == current:
            return
        mark = (path, stamp, current)
        if mark == self._last:
            return
        if xbmc.getInfoLabel('Container.FolderPath') != path:  # the user moved on while this was checked
            return
        self._last = mark
        from tmdbhelper.lib.addon.logger import kodi_log
        kodi_log(f'MenuStaleMonitor: {path} was built under other menu switches than the current ones, refreshing it', 2)
        xbmc.executebuiltin('Container.Refresh')

    def run(self):
        from tmdbhelper.lib.addon.logger import kodi_log
        while not self._should_stop:
            try:
                self._tick()
            except Exception as exc:
                kodi_log(f'MenuStaleMonitor error: {exc}', 1)
            self.monitor.waitForAbort(MENU_POLL_TIME)
