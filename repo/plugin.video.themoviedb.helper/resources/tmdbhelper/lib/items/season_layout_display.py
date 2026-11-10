from jurialmunkey.parser import try_int
from tmdbhelper.lib.addon.plugin import get_localized


class _EpisodeKey:
    """What the watched-indicator lookups read of an episode list item."""
    def __init__(self, tmdb_id, season, episode):
        self.infolabels = {'mediatype': 'episode', 'season': season, 'episode': episode}
        self.unique_ids = {'tvshow.tmdb': tmdb_id}


class SeasonLayoutDisplay:
    """Shows a show's season layout (api/tmdb/season_layout.py) in a list. An item is finished with its REAL TMDb season and episode
    (watched marks, Kodi library details, play path) and only then are the displayed season number, episode number, season name,
    label and season count changed, so nothing but what is drawn differs from TMDb's own numbering.

    A seasons / episodes list of the show sets `self.layout` itself. Any other list (Next Up, In Progress, calendars, search ...)
    only uses a layout already on file, so it never waits on TMDb; the exceptions that may ask TMDb are the Up Next list of one
    show and a show with a lone long season (the only shows that can have a layout), which is what makes the show's own page right.
    """
    layout = None

    def make_item(self, li):
        keep_label = li.infoproperties.get('no_label_formatting') if li else None
        li = super().make_item(li)
        if not li:
            return li
        self._stamp_season_path(li)
        layout = self.layout or self._layout_of(li)
        if layout and self._show_layout_numbers(li, layout, keep_label) is False:
            return
        return li

    def _stamp_season_path(self, li):
        """Marking something watched refreshes the open list and sets the widget reload stamp. A skin that shows a season's episodes
        beside the season list (Bingie) loads them from the season item's path, so a path that carries the stamp makes it load them
        again; without it the episodes kept their old watched marks until the window was reopened."""
        if self.params.get('info') != 'seasons' or li.infolabels.get('mediatype') != 'season':
            return
        from jurialmunkey.window import get_property
        stamp = get_property('Widgets.Reload')
        if stamp:
            li.params['lreload'] = stamp

    @property
    def layout_enabled(self):
        try:
            return self._layout_enabled
        except AttributeError:
            from tmdbhelper.lib.api.tmdb import season_layout
            self._layout_enabled = season_layout.is_enabled()
            return self._layout_enabled

    def _layout_of(self, li):
        """The layout of the show an item belongs to, or None. No TMDb request unless the item is a lone-long-season show or this is
        the Up Next list of that show."""
        mediatype = li.infolabels.get('mediatype')
        if mediatype == 'tvshow':
            tmdb_id = li.unique_ids.get('tmdb')
        elif mediatype in ('episode', 'season'):
            tmdb_id = li.unique_ids.get('tvshow.tmdb')
        else:
            return None
        if not tmdb_id or not self.layout_enabled:
            return None
        from tmdbhelper.lib.api.tmdb import season_layout
        known, layout = season_layout.cached_layout(tmdb_id)
        if known:
            return layout
        if mediatype == 'tvshow' and try_int(li.infolabels.get('season')) == 1 and try_int(li.infolabels.get('episode')) >= season_layout.MIN_EPISODES:
            return season_layout.get_layout(self.tmdb_api, tmdb_id)
        if self.params.get('info') == 'trakt_upnext' and str(self.params.get('tmdb_id')) == str(tmdb_id):
            return season_layout.get_layout(self.tmdb_api, tmdb_id)
        return None

    def _watched_count(self, tmdb_id, season, source=None):
        """Episodes of a layout season that count as watched: the highest of Trakt, MDBList and Simkl per episode, as the list does,
        or only those of `source` ('trakt', 'mdblist' or 'simkl') when a list opened from an In Progress row decides what to hide."""
        playdata = {'trakt': self.trakt_playdata, 'mdblist': self.mdblist_playdata, 'simkl': self.simkl_playdata}
        source = None if source == 'simkl' else source  # Simkl has no episode rows of its own: highest of all services
        count = 0
        for real_season, real_episode in season['episodes']:
            episode = _EpisodeKey(tmdb_id, real_season, real_episode)
            if source:
                watched = playdata[source].get_playcount(episode) or 0
            else:
                watched = max(self.trakt_playdata.get_playcount(episode) or 0, self.mdblist_playdata.get_playcount(episode) or 0,
                              self.simkl_playdata.get_playcount(episode) or 0)
            if watched > 0:
                count += 1
        return count

    @staticmethod
    def _own_artwork(li, number):
        """A layout season has no artwork of its own at TMDb, so it shows real season 1's until one is chosen for it with Select artwork
        (kept in season_layout_art.json); Manage Artwork then carries the layout season, so a pick changes this season only."""
        from tmdbhelper.lib.api.tmdb import season_layout
        li.art.update(season_layout.layout_art(li.unique_ids.get('tvshow.tmdb'), number))
        li.context_menu = [
            (name, f'{cmd[:-1]},lseason={number})') if 'manage_artwork' in cmd and 'lseason=' not in cmd and cmd.endswith(')') else (name, cmd)
            for name, cmd in li.context_menu]

    def _show_layout_numbers(self, li, layout, keep_label):
        from tmdbhelper.lib.api.tmdb import season_layout
        mediatype = li.infolabels.get('mediatype')
        if mediatype == 'tvshow':
            count = len(layout['seasons'])
            li.infolabels['season'] = li.infoproperties['totalseasons'] = count
        elif mediatype == 'season' and li.params.get('lseason'):
            season = season_layout.find_season(layout, li.params['lseason'])
            if not season:
                return
            li.infolabels['season'] = season['number']
            li.infolabels['episode'] = len(season['episodes'])
            li.infolabels['title'] = li.label = season['name'] or f"{get_localized(20373)} {season['number']}"
            li.set_playcount(self._watched_count(li.unique_ids.get('tvshow.tmdb'), season))  # counts, progress, playcount of THIS season
            self._own_artwork(li, season['number'])
            if self.hide_watched:
                if self.hide_source:  # opened from an In Progress row: that service's own watched episodes decide
                    total = len(season['episodes'])
                    watched = bool(total) and self._watched_count(li.unique_ids.get('tvshow.tmdb'), season, self.hide_source) >= total
                else:
                    watched = li.infolabels.get('playcount')
                if watched and self.hide_source:
                    li.hidden_by_source = True  # dropped in make_items, unless that would leave nothing to open
                elif watched:
                    return False  # fully watched layout season: hidden like a watched season
        elif mediatype == 'episode':
            position = season_layout.episode_position(layout, li.infolabels.get('season'), li.infolabels.get('episode'))
            if not position:
                return
            li.infolabels['season'], li.infolabels['episode'] = position
            if not keep_label:
                li.set_episode_label()
