from tmdbhelper.lib.addon.thread import SafeThread


CRONJOB_POLL_TIME = 180  # Reduced from 600: LastInvalidated is now the primary widget-refresh signal; CronJob is just the safety net. Polling every 3 min costs ~zero (one cleared window property per poll when nothing changed).


class CronJobMonitor(SafeThread):

    _poll_time = CRONJOB_POLL_TIME

    def __init__(self, parent, update_hour=0):
        SafeThread.__init__(self)
        self.exit = False
        self.update_hour = update_hour
        self.update_monitor = parent.update_monitor

    def _on_startup(self):
        self._do_delete_old_databases()
        self._do_recache_kodidb()
        self._do_trakt_authorization()
        self._do_bridge_sync_startup()

    def _on_poll(self):
        self._do_library_update_check()
        self._do_reset_trakt_lastactivities()
        self._do_trakt_token_refresh_check()
        self._do_cleanup_widget_cache()   # Item 5: delete widget_cache/*.json older than 24h
        self._do_cleanup_image_cache()    # Item 4: prune blur_v2/crop_v2/desaturate_v2 dirs
        self._do_bridge_sync()

    def _do_cleanup_widget_cache(self):
        """Delete widget_cache/*.json files with mtime older than 24 hours. (Item 5)
        Abort-aware: yields between deletes if Kodi is shutting down.
        """
        import os
        import time
        try:
            import xbmcaddon
            import xbmcvfs
            profile = xbmcvfs.translatePath(xbmcaddon.Addon().getAddonInfo('profile'))
            cache_dir = os.path.join(profile, 'widget_cache')
            if not os.path.isdir(cache_dir):
                return
            max_age = 86400  # 24 hours
            now = time.time()
            removed = 0
            for fname in os.listdir(cache_dir):
                if self.update_monitor.abortRequested():
                    return
                if not fname.endswith('.json'):
                    continue
                fpath = os.path.join(cache_dir, fname)
                try:
                    if now - os.path.getmtime(fpath) > max_age:
                        xbmcvfs.delete(fpath)
                        removed += 1
                except OSError:
                    pass
            if removed:
                from tmdbhelper.lib.addon.logger import kodi_log
                kodi_log(f'CronJob: removed {removed} expired widget cache file(s)', 2)
        except Exception:
            pass

    def _do_cleanup_image_cache(self):
        """Prune image cache dirs by mtime (>14 days) then hard-cap at 100MB oldest-first. (Item 4)
        Abort-aware: yields between deletes if Kodi is shutting down so cleanup can't
        delay shutdown beyond a single delete. (Audit fix v6.0.29 — Bug #5.)
        """
        import os
        import time
        _IMAGE_DIRS = ('blur_v2', 'crop_v2', 'desaturate_v2')
        _MAX_AGE = 14 * 86400       # 14 days
        _MAX_BYTES = 100 * 1024 * 1024  # 100 MB
        try:
            import xbmcaddon
            import xbmcvfs
            profile = xbmcvfs.translatePath(xbmcaddon.Addon().getAddonInfo('profile'))
            now = time.time()
            all_files = []
            for dirname in _IMAGE_DIRS:
                if self.update_monitor.abortRequested():
                    return
                dirpath = os.path.join(profile, dirname)
                if not os.path.isdir(dirpath):
                    continue
                for fname in os.listdir(dirpath):
                    if self.update_monitor.abortRequested():
                        return
                    fpath = os.path.join(dirpath, fname)
                    try:
                        st = os.stat(fpath)
                        if now - st.st_mtime > _MAX_AGE:
                            xbmcvfs.delete(fpath)
                        else:
                            all_files.append((st.st_mtime, st.st_size, fpath))
                    except OSError:
                        pass
            # Size cap: prune oldest files until under 100 MB
            all_files.sort(key=lambda x: x[0])
            total = sum(s for _, s, _ in all_files)
            for mtime, size, fpath in all_files:
                if self.update_monitor.abortRequested():
                    return
                if total <= _MAX_BYTES:
                    break
                try:
                    xbmcvfs.delete(fpath)
                    total -= size
                except Exception:
                    pass
        except Exception:
            pass

    @property
    def trakt_api(self):
        try:
            return self._trakt_api
        except AttributeError:
            from tmdbhelper.lib.api.trakt.api import TraktAPI
            self._trakt_api = TraktAPI()
            return self._trakt_api

    @staticmethod
    def _do_delete_old_databases():
        from tmdbhelper.lib.script.method.maintenance import clean_old_databases
        clean_old_databases()

    @staticmethod
    def _do_recache_kodidb():
        from tmdbhelper.lib.script.method.maintenance import recache_kodidb
        recache_kodidb(notification=False)

    def _do_trakt_authorization(self):
        from tmdbhelper.lib.addon.plugin import get_setting
        if not get_setting('service_trakt'):
            return
        from jurialmunkey.parser import boolean
        from jurialmunkey.window import get_property
        self.trakt_api.authorize(confirmation=True)
        self.update_monitor.waitForAbort(1)
        if not boolean(get_property('TraktIsAuth')):
            return
        from tmdbhelper.lib.script.method.trakt import get_stats
        get_stats()

    def _do_reset_trakt_lastactivities(self):
        from tmdbhelper.lib.addon.plugin import get_setting
        if not get_setting('service_trakt'):
            return
        from jurialmunkey.window import get_property
        from tmdbhelper.lib.addon.consts import LASTACTIVITIES_DATA
        get_property(LASTACTIVITIES_DATA, clear_property=True)

    def _do_trakt_token_refresh_check(self):
        """Periodically check if the Trakt token is expired or about to expire and refresh it.
        This ensures long-running Kodi sessions stay authorized with 24-hour tokens."""
        from tmdbhelper.lib.addon.plugin import get_setting
        if not get_setting('service_trakt'):
            return
        try:
            if not self.trakt_api.authorization:
                return
            if not self.trakt_api.is_token_expired():
                return
            from tmdbhelper.lib.addon.logger import kodi_log
            kodi_log('CronJob: Trakt token expired or expiring soon, refreshing', 2)
            if self.trakt_api.refresh_token():
                kodi_log('CronJob: Trakt token refreshed successfully', 1)
            else:
                kodi_log('CronJob: Trakt token refresh failed', 1)
        except Exception:
            pass

    def _do_library_update(self):
        from tmdbhelper.lib.addon.plugin import executebuiltin
        from tmdbhelper.lib.addon.tmdate import get_datetime_now, get_timedelta
        executebuiltin('RunScript(plugin.video.themoviedb.helper,library_autoupdate)')
        executebuiltin(f'Skin.SetString(TMDbHelper.AutoUpdate.LastTime,{get_datetime_now().strftime("%Y-%m-%dT%H:%M:%S")})')
        self.library_update_next += get_timedelta(hours=24)  # Set next update for tomorrow

    def _do_library_update_check(self):
        from jurialmunkey.parser import try_int
        from tmdbhelper.lib.addon.tmdate import convert_timestamp, get_datetime_now, get_timedelta, get_datetime_today, get_datetime_time, get_datetime_combine
        from tmdbhelper.lib.addon.plugin import get_setting, get_infolabel
        if not get_setting('library_autoupdate'):
            return
        self.library_update_next = get_datetime_combine(get_datetime_today(), get_datetime_time(try_int(self.update_hour)))
        self.library_update_last = get_infolabel('Skin.String(TMDbHelper.AutoUpdate.LastTime)')
        self.library_update_last = convert_timestamp(self.library_update_last) if self.library_update_last else None

        # If we've already updated the library today then set a new next update time for tomorrow
        if self.library_update_last and self.library_update_last > self.library_update_next:
            self.library_update_next += get_timedelta(hours=24)

        # If the next update timestamp has elapsed then we should update the library
        if get_datetime_now() > self.library_update_next:
            self._do_library_update()

    def _do_bridge_sync_startup(self):
        """Run bridge sync ~10s after startup in a background thread so Trakt auth
        and initial data sync can settle before we compare the services."""
        from tmdbhelper.lib.addon.plugin import get_setting
        if not get_setting('cross_sync_services'):
            return
        if not get_setting('service_trakt'):
            return
        if not get_setting('service_mdblist') and not get_setting('service_simkl'):
            return

        monitor = self.update_monitor

        def _run():
            monitor.waitForAbort(10)
            if monitor.abortRequested():
                return
            try:
                from tmdbhelper.lib.api.mdblist.api import MDbList
                from tmdbhelper.lib.api.simkl.api import SimklAPI
                from tmdbhelper.lib.monitor.bridge_sync import BridgeSync
                simkl = SimklAPI() if get_setting('service_simkl') else None
                BridgeSync(self.trakt_api, MDbList(), simkl).run(force=True)
            except Exception as exc:
                from tmdbhelper.lib.addon.logger import kodi_log
                kodi_log(f'BridgeSync startup error: {exc}', 2)

        SafeThread(target=_run).start()

    def _do_bridge_sync(self):
        """Periodic bridge sync — honours the 15-minute interval gate in BridgeSync.run()."""
        from tmdbhelper.lib.addon.plugin import get_setting
        if not get_setting('cross_sync_services'):
            return
        if not get_setting('service_trakt'):
            return
        if not get_setting('service_mdblist') and not get_setting('service_simkl'):
            return
        try:
            from tmdbhelper.lib.api.mdblist.api import MDbList
            from tmdbhelper.lib.api.simkl.api import SimklAPI
            from tmdbhelper.lib.monitor.bridge_sync import BridgeSync
            simkl = SimklAPI() if get_setting('service_simkl') else None
            BridgeSync(self.trakt_api, MDbList(), simkl).run(force=False)
        except Exception as exc:
            from tmdbhelper.lib.addon.logger import kodi_log
            kodi_log(f'BridgeSync poll error: {exc}', 2)

    def run(self):
        self._on_startup()

        while not self.update_monitor.abortRequested() and not self.exit:
            self.update_monitor.waitForAbort(self._poll_time)
            self._on_poll()
