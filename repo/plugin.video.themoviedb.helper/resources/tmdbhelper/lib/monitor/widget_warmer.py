from tmdbhelper.lib.addon.thread import SafeThread

WARMER_POLL_TIME = 15  # seconds between checks
WARMER_START_DELAY = 60  # let Kodi, the skin and the first syncs settle (this pass is also the startup preload)
WARMER_QUIET_TIME = 45  # seconds after the last invalidation before rebuilding: the skin re-requests its widgets first
WARMER_MAX_ATTEMPTS = 2  # per widget per cycle: building can itself trigger a sync that invalidates the caches again
WARMER_BACKOFF_FAILED = 1800  # seconds a widget is left alone after a build that raised or came back empty
WARMER_BACKOFF_BUSY = 600  # ... after the caches kept being invalidated while it was building
WARMER_PAUSE = 2  # seconds between two builds
WARM_NEXT_PROPERTY = 'TMDbHelper.Widgets.WarmNext'
WARM_NEXT_MEMORY = 200


class WidgetWarmer(SafeThread):
    """Service thread. Rebuilds, in the background, the widget lists the user opened lately so that opening one is a
    cache hit, and fetches the next page of the list being read. All of it runs here, never inside a plugin call
    (plugin calls are short-lived interpreters), and pauses while a video plays or Kodi shuts down."""

    def __init__(self, parent):
        SafeThread.__init__(self)
        self.exit = False
        self.monitor = parent.update_monitor
        self._attempts = {}
        self._backoff = {}
        self._built = 0
        self._warmed_next = []

    @property
    def _should_stop(self):
        return self.exit or self.monitor.abortRequested()

    def _paused(self):
        from tmdbhelper.lib.addon.plugin import get_setting
        from xbmc import Player
        if not get_setting('widget_prewarm'):
            return True
        return Player().isPlayingVideo()

    @staticmethod
    def _build(paramstring):
        """The same build a plugin call does for this paramstring, without a Kodi directory (handle -1)."""
        from tmdbhelper.lib.items.router import Router
        from tmdbhelper.lib.items.routes import get_container
        router = Router(-1, paramstring)
        container = get_container(router.params.get('info'))(-1, router.paramstring, **router.params)
        container.get_tmdb_id()
        items = container.get_directory(items_only=True)
        return items, container

    def _warm_next(self):
        import xbmcgui
        home = xbmcgui.Window(10000)
        paramstring = home.getProperty(WARM_NEXT_PROPERTY)
        if not paramstring:
            return
        home.clearProperty(WARM_NEXT_PROPERTY)
        if paramstring in self._warmed_next:
            return
        self._warmed_next = (self._warmed_next + [paramstring])[-WARM_NEXT_MEMORY:]
        self._build(paramstring)  # fills the item caches; the page itself is built again when the user opens it

    def _bump_skins(self):
        from tmdbhelper.lib.script.method.kodi_utils import reload_widgets
        reload_widgets(invalidate=False)  # skins re-request their widgets; LastInvalidated stays, so no loop

    def _warm_widgets(self):
        import time
        from tmdbhelper.lib.items.widget_cache import read_index, needs_warm, save_cache, forget, get_last_invalidated, INDEX_MAX_AGE
        from tmdbhelper.lib.addon.logger import kodi_log
        last = get_last_invalidated()
        if last and time.time() - last < WARMER_QUIET_TIME:
            return
        now = time.time()
        due = [
            (key, entry) for key, entry in (read_index() or {}).items()
            if now - entry.get('used', 0) < INDEX_MAX_AGE
            and self._attempts.get(key, 0) < WARMER_MAX_ATTEMPTS
            and self._backoff.get(key, 0) < now
            and needs_warm(key, entry)]
        if not due:
            if self._built:  # this cycle is complete: tell the skins once
                self._bump_skins()
            self._built = 0
            self._attempts = {}
            return
        key, entry = max(due, key=lambda i: i[1].get('used', 0))
        self._attempts[key] = self._attempts.get(key, 0) + 1
        paramstring = entry.get('ps')
        if not paramstring:
            return
        try:
            items, container = self._build(paramstring)
        except Exception as exc:
            self._backoff[key] = now + WARMER_BACKOFF_FAILED
            kodi_log(f'WidgetWarmer: build failed for {key}: {exc}', 1)
            return
        if get_last_invalidated() != last or self._should_stop:
            if self._attempts[key] >= WARMER_MAX_ATTEMPTS:
                self._backoff[key] = now + WARMER_BACKOFF_BUSY  # invalidated while building twice: leave it for a while
            return  # the data changed while building: this result may be older than the change
        if not items:
            self._backoff[key] = now + WARMER_BACKOFF_FAILED
            forget(key)  # empty now: do not keep serving the old items
            return
        save_cache(paramstring, items, container.plugin_category, container.container_content, touch=False)
        self._built += 1
        kodi_log(f'WidgetWarmer: rebuilt {key}', 2)

    def _tick(self):
        if self._paused():
            return
        self._warm_next()
        if self._should_stop:
            return
        self._warm_widgets()

    def run(self):
        from tmdbhelper.lib.addon.logger import kodi_log
        from tmdbhelper.lib.items.widget_cache import cleanup_tmp
        cleanup_tmp()
        self.monitor.waitForAbort(WARMER_START_DELAY)
        while not self._should_stop:
            try:
                self._tick()
            except Exception as exc:
                kodi_log(f'WidgetWarmer error: {exc}', 1)
            self.monitor.waitForAbort(WARMER_PAUSE if self._built and not self._should_stop else WARMER_POLL_TIME)
