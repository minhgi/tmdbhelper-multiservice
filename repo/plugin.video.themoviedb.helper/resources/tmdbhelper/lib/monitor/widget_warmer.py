from tmdbhelper.lib.addon.thread import SafeThread

WARMER_POLL_TIME = 15  # seconds between checks
WARMER_START_DELAY = 60  # let Kodi, the skin and the first syncs settle (this pass is also the startup preload)
WARMER_QUIET_TIME = 45  # seconds after the last invalidation before rebuilding: the skin re-requests its widgets first
WARMER_MAX_ATTEMPTS = 2  # per widget per cycle: building can itself trigger a sync that invalidates the caches again
WARMER_BACKOFF_FAILED = 1800  # seconds a widget is left alone after a build that raised or came back empty
WARMER_BACKOFF_BUSY = 600  # ... after the caches kept being invalidated while it was building
WARMER_PAUSE = 2  # seconds between two builds
WARMER_SEED_IDLE = 30  # seconds without input before the lists of pages the user has not opened yet are built
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
        self._seeding = False
        self._seed_attempts = {}
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
    def _build(paramstring, row=False):
        """The same build a plugin call does for this paramstring, without a Kodi directory (handle -1).
        row=True: a plain list the skin shows as a row, built as a single page whatever window is open now."""
        from tmdbhelper.lib.items.router import Router
        from tmdbhelper.lib.items.routes import get_container
        router = Router(-1, paramstring)
        container = get_container(router.params.get('info'))(-1, router.paramstring, **router.params)
        container.row_build = True if row else None
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
        from tmdbhelper.lib.items.widget_cache import (
            read_index, needs_warm, save_cache, forget, get_last_invalidated, rowcache_enabled, INDEX_MAX_AGE)
        from tmdbhelper.lib.addon.logger import kodi_log
        last = get_last_invalidated()
        if last and time.time() - last < WARMER_QUIET_TIME:
            return True  # busy: nothing else is built meanwhile
        now = time.time()
        rows_on = rowcache_enabled()
        due = [
            (key, entry) for key, entry in (read_index() or {}).items()
            if (rows_on or not entry.get('row'))
            and now - entry.get('used', 0) < INDEX_MAX_AGE
            and self._attempts.get(key, 0) < WARMER_MAX_ATTEMPTS
            and self._backoff.get(key, 0) < now
            and needs_warm(key, entry)]
        if not due:
            if self._built:  # this cycle is complete: tell the skins once
                self._bump_skins()
            self._built = 0
            self._attempts = {}
            return False
        key, entry = max(due, key=lambda i: i[1].get('used', 0))
        self._attempts[key] = self._attempts.get(key, 0) + 1
        paramstring = entry.get('ps')
        if not paramstring:
            return True
        row = bool(entry.get('row'))
        try:
            items, container = self._build(paramstring, row)
        except Exception as exc:
            self._backoff[key] = now + WARMER_BACKOFF_FAILED
            kodi_log(f'WidgetWarmer: build failed for {key}: {exc}', 1)
            return True
        if get_last_invalidated() != last or self._should_stop:
            if self._attempts[key] >= WARMER_MAX_ATTEMPTS:
                self._backoff[key] = now + WARMER_BACKOFF_BUSY  # invalidated while building twice: leave it for a while
            return True  # the data changed while building: this result may be older than the change
        if not items or (row and (container.container_update or container.container_refresh)):
            self._backoff[key] = now + WARMER_BACKOFF_FAILED
            forget(key)  # empty now: do not keep serving the old items
            return True
        save_cache(paramstring, items, container.plugin_category, container.container_content, touch=False, row=row)
        self._built += 1
        kodi_log(f'WidgetWarmer: rebuilt {key}', 2)
        return True

    def _seed_widgets(self):
        """Build, one per call and only while the user is idle, the lists of the skin's pages that have no cache yet
        (never opened, or older than the startup limit), so the first visit to a page is instant. No skin nudge
        afterwards: a page that is not on screen loads from the cache when it is opened."""
        import time
        from tmdbhelper.lib.addon.plugin import get_condvisibility
        from tmdbhelper.lib.addon.logger import kodi_log
        from tmdbhelper.lib.items.widget_cache import (
            seed_enabled, seed_entries, seed_needs_warm, save_cache, get_last_invalidated, rowcache_enabled, _get_cache_key)
        self._seeding = False  # True only right after a seed was built: the next one follows soon, otherwise the normal poll time
        if not seed_enabled() or not get_condvisibility(f'System.IdleTime({WARMER_SEED_IDLE})'):
            return
        last = get_last_invalidated()
        now = time.time()
        rows_on = rowcache_enabled()
        for paramstring, row in seed_entries():
            if row and not rows_on:
                continue  # rows are not served with the setting off
            key = _get_cache_key(paramstring, row)
            if self._seed_attempts.get(key, 0) >= WARMER_MAX_ATTEMPTS or self._backoff.get(key, 0) > now:
                continue
            if not seed_needs_warm(paramstring, row):
                continue
            self._seed_attempts[key] = self._seed_attempts.get(key, 0) + 1
            try:
                items, container = self._build(paramstring, row)
            except Exception as exc:
                self._backoff[key] = now + WARMER_BACKOFF_FAILED
                kodi_log(f'WidgetWarmer: seed build failed for {key}: {exc}', 1)
                return
            if get_last_invalidated() != last or self._should_stop:
                return  # the data changed while building: try again later
            if not items or (row and (container.container_update or container.container_refresh)):
                self._backoff[key] = now + WARMER_BACKOFF_FAILED
                return
            save_cache(paramstring, items, container.plugin_category, container.container_content, touch=False, row=row)
            kodi_log(f'WidgetWarmer: seeded {key}', 2)
            self._seeding = True
            return
        self._seed_attempts = {}  # nothing left to build

    def _tick(self):
        try:
            from tmdbhelper.lib.items.widget_cache import publish_skin_rows
            publish_skin_rows()  # cheap: only rewritten when the skin's settings changed
        except Exception as exc:
            from tmdbhelper.lib.addon.logger import kodi_log
            kodi_log(f'WidgetWarmer: skin rows not published: {exc}', 1)
        if self._paused():
            self._seeding = False
            return
        self._warm_next()
        if self._should_stop:
            return
        if self._warm_widgets():
            self._seeding = False
        elif not self._should_stop:
            self._seed_widgets()

    def run(self):
        from tmdbhelper.lib.addon.logger import kodi_log
        from tmdbhelper.lib.items.widget_cache import cleanup_tmp
        cleanup_tmp()
        try:
            from tmdbhelper.lib.items.widget_cache import publish_skin_rows, read_skin_rows
            if not read_skin_rows():
                publish_skin_rows()  # first run: the plugin calls need it to know which lists are skin rows (later runs reuse the file)
        except Exception as exc:
            kodi_log(f'WidgetWarmer error: {exc}', 1)
        self.monitor.waitForAbort(WARMER_START_DELAY)
        while not self._should_stop:
            try:
                self._tick()
            except Exception as exc:
                kodi_log(f'WidgetWarmer error: {exc}', 1)
            self.monitor.waitForAbort(WARMER_PAUSE if (self._built or self._seeding) and not self._should_stop else WARMER_POLL_TIME)
