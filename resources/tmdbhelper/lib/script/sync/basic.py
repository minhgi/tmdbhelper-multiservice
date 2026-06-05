from tmdbhelper.lib.script.sync.item import ItemSync


class ItemWatched(ItemSync):
    trakt_sync_url = 'history'
    allow_seasons = True
    allow_episodes = True
    preconfigured = True
    localized_name = 16103
    remove = False
    trakt_sync_key = 'last_watched_at'

    def get_self(self):
        # super() handles service_trakt gate + is_allowed_type; returns self or None
        if not super().get_self():
            return None
        # Toggle: hide "Mark as watched" when item is already watched
        if self.is_sync:
            return None
        return self

    def _cross_sync(self):
        from tmdbhelper.lib.script.sync.cross_sync import mirror_watched_to_mdblist, mirror_watched_to_simkl
        mirror_watched_to_mdblist(self.tmdb_type, self.tmdb_id, self.season, self.episode, remove=False)
        mirror_watched_to_simkl(self.tmdb_type, self.tmdb_id, self.season, self.episode, remove=False)


class ItemUnwatched(ItemSync):
    trakt_sync_url = 'history'
    allow_seasons = True
    allow_episodes = True
    preconfigured = True
    localized_name = 16104
    remove = True
    trakt_sync_key = 'last_watched_at'

    def get_self(self):
        # super() handles service_trakt gate + is_allowed_type; returns self or None
        if not super().get_self():
            return None
        # Toggle: hide "Mark as unwatched" when item has not been watched
        if not self.is_sync:
            return None
        return self

    def _clear_playback_progress(self):
        """Also clear Trakt playback/resume progress when marking as unwatched.
        Without this, the item stays in 'In Progress' widgets despite being unwatched
        because the scrobble bookmark at sync/playback/{id} is a separate API endpoint."""
        if not self.is_successful_sync:
            return
        try:
            if self.trakt_type == 'movie':
                playback_id = self.trakt_syncdata.get_movie_playprogress_id(self.tmdb_id)
            elif self.season is not None and self.episode is not None:
                playback_id = self.trakt_syncdata.get_episode_playprogress_id(self.tmdb_id, self.season, self.episode)
            else:
                playback_id = None
            if playback_id:
                from tmdbhelper.lib.addon.dialog import BusyDialog
                with BusyDialog():
                    self.trakt_api.delete_response('sync', 'playback', playback_id)
        except Exception:
            pass

    def _cross_sync(self):
        from tmdbhelper.lib.script.sync.cross_sync import mirror_watched_to_mdblist, mirror_watched_to_simkl
        mirror_watched_to_mdblist(self.tmdb_type, self.tmdb_id, self.season, self.episode, remove=True)
        mirror_watched_to_simkl(self.tmdb_type, self.tmdb_id, self.season, self.episode, remove=True)

    def sync(self):
        self._clear_playback_progress()
        self.reset_lastactivities()
        self.display_dialog()
        self.refresh_containers()
        if self.is_successful_sync:
            self._cross_sync()


class ItemWatchlist(ItemSync):
    localized_name_add = 32291
    localized_name_rem = 32292
    trakt_sync_key = 'watchlist_listed_at'
    trakt_sync_url = 'watchlist'

    def _cross_sync(self):
        from tmdbhelper.lib.script.sync.cross_sync import mirror_watchlist_to_mdblist, mirror_watchlist_to_simkl
        mirror_watchlist_to_mdblist(self.tmdb_type, self.tmdb_id, self.remove)
        if not self.remove:
            mirror_watchlist_to_simkl(self.tmdb_type, self.tmdb_id)


class ItemCollection(ItemSync):
    localized_name_add = 32289
    localized_name_rem = 32290
    allow_episodes = True
    trakt_sync_key = 'collection_last_collected_at'
    trakt_sync_url = 'collection'

    def _cross_sync(self):
        from tmdbhelper.lib.script.sync.cross_sync import mirror_collection_to_mdblist
        mirror_collection_to_mdblist(self.tmdb_type, self.tmdb_id, self.season, self.episode, self.remove)


class ItemFavorites(ItemSync):
    localized_name_add = 32490
    localized_name_rem = 32491
    trakt_sync_key = 'favorites_listed_at'
    trakt_sync_url = 'favorites'
