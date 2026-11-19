class CommonContainerAPIs():
    @property
    def page_length(self):
        return 1

    @property
    def ftv_is_cache_only(self):
        return False

    @property
    def all_awards(self):
        try:
            return self._all_awards
        except AttributeError:
            self._all_awards = self.get_awards_data()
            return self._all_awards

    @property
    def trakt_api(self):
        try:
            return self._trakt_api
        except AttributeError:
            from tmdbhelper.lib.api.trakt.api import TraktAPI
            self._trakt_api = TraktAPI(page_length=self.page_length)
            return self._trakt_api

    @property
    def tmdb_api(self):
        try:
            return self._tmdb_api
        except AttributeError:
            from tmdbhelper.lib.api.tmdb.api import TMDb
            self._tmdb_api = TMDb(page_length=self.page_length)
            return self._tmdb_api

    @property
    def ftv_api(self):
        try:
            return self._ftv_api
        except AttributeError:
            from tmdbhelper.lib.api.fanarttv.api import FanartTV
            self._ftv_api = FanartTV(cache_only=self.ftv_is_cache_only)
            return self._ftv_api

    @property
    def tvdb_art_api(self):
        """ TheTVDB artwork gap-fill source: None when the setting is off. Follows the fanart.tv rule: no network where fanart.tv is cache only. """
        try:
            return self._tvdb_art_api
        except AttributeError:
            from tmdbhelper.lib.addon.plugin import get_setting
            if get_setting('tvdb_artwork'):
                from tmdbhelper.lib.api.tvdb.artwork import TVDbArtwork
                self._tvdb_art_api = TVDbArtwork(cache_only=self.ftv_is_cache_only)
            else:
                self._tvdb_art_api = None
            return self._tvdb_art_api

    @property
    def tvdb_api(self):
        try:
            return self._tvdb_api
        except AttributeError:
            from tmdbhelper.lib.api.tvdb.api import TVDb
            self._tvdb_api = TVDb()
            return self._tvdb_api

    @property
    def omdb_api(self):
        try:
            return self._omdb_api
        except AttributeError:
            from tmdbhelper.lib.api.omdb.api import OMDb
            from tmdbhelper.lib.addon.plugin import get_setting
            self._omdb_api = OMDb() if get_setting('omdb_apikey', 'str') else None
            return self._omdb_api

    @property
    def mdblist_api(self):
        try:
            return self._mdblist_api
        except AttributeError:
            from tmdbhelper.lib.api.mdblist.api import MDbList
            from tmdbhelper.lib.addon.plugin import get_setting
            api_key = get_setting('mdblist_apikey', 'str')
            if not api_key:
                return None  # not remembered: a key typed in while Kodi runs must be picked up (GitHub issue #13)
            self._mdblist_api = MDbList(api_key=api_key)  # the key given here: the module's own copy was read when it was first imported
            return self._mdblist_api

    @property
    def mdblist_scrobble_api(self):
        """ MDBList connection for the scrobbler: the API key OR the signed-in token is enough (mdblist_api needs the key because the ratings lookup is gated on it) """
        api = self.mdblist_api
        if api:
            return api
        try:
            return self._mdblist_token_api
        except AttributeError:
            from tmdbhelper.lib.addon.plugin import get_setting
            if not get_setting('mdblist_token', 'str'):
                return None  # not remembered: signing in while Kodi runs must be picked up
            from tmdbhelper.lib.api.mdblist.api import MDbList
            self._mdblist_token_api = MDbList()
            return self._mdblist_token_api

    @property
    def simkl_api(self):
        try:
            return self._simkl_api
        except AttributeError:
            from tmdbhelper.lib.api.simkl.api import SimklAPI
            from tmdbhelper.lib.addon.plugin import get_setting
            if not get_setting('service_simkl'):
                return None  # not remembered: the running service must pick Simkl up when it is switched on later (GitHub issue #13)
            self._simkl_api = SimklAPI()
            return self._simkl_api
