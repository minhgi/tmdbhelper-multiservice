import re
import datetime
import xbmcgui
from tmdbhelper.lib.items.router import Router
from tmdbhelper.lib.addon.dialog import BusyDialog
from tmdbhelper.lib.addon.logger import kodi_log
from tmdbhelper.lib.addon.plugin import ADDONPATH

""" Your TV Calendar window: every show you follow on one scrollable page. A header row per show, its episodes under it
(aired and not yet aired); Select on a show lists that show's unwatched episodes, Select on an episode opens its information
(with Play and the other buttons). The episodes are the ones the day-by-day calendar builds (info=trakt_calendar, unstacked) for a
span around today; a show's unwatched list comes from its Up Next list (info=trakt_upnext).
The All TV Calendar (user=false) uses the same page for ONE DAY at a time: Left / Right (or the < > buttons) change the day.
"""

LIST_ID = 1000
BACK_BUTTON_ID = 3003
PREV_BUTTON_ID = 3001
NEXT_BUTTON_ID = 3002
TODAY_BUTTON_ID = 3004
CLOSE_BUTTON_ID = 2999
ACTION_MOVE_LEFT = 1
ACTION_MOVE_RIGHT = 2
ACTION_CLOSEWINDOW = (9, 10, 92, 216, 247, 257, 275, 61467, 61448,)
DAYS_BACK = 14
DAYS_SPAN = 33  # Trakt's calendar allows at most 33 days in one request: 14 back and about 19 ahead
MAX_PAGES = 10  # stop a runaway paging loop

VIEW_SHOWS = 'shows'
VIEW_UNWATCHED = 'unwatched'

# Trakt's episode_type values that get a badge on the row (anything else, "standard" included, gets none). Short, so the badge fits beside the title.
EPISODE_BADGES = {
    'series_premiere': 'New show',
    'season_premiere': 'Premiere',
    'mid_season_premiere': 'Mid premiere',
    'mid_season_finale': 'Mid finale',
    'season_finale': 'Finale',
    'series_finale': 'Series finale',
}
MIN_YEAR = 1900  # Kodi writes an unknown date as 1601-01-01 (its zero date); nothing real airs before this


def _today():
    from tmdbhelper.lib.addon.tmdate import get_datetime_today
    return get_datetime_today().date()


def calendar_path(endpoint=None, page=1):
    path = f'info=trakt_calendar&startdate=-{DAYS_BACK}&days={DAYS_SPAN}&unstacked=true'
    return f'{path}&endpoint={endpoint}&page={page}' if endpoint else f'{path}&page={page}'


def day_path(day_offset, endpoint=None, page=1, extra=None):
    """ The All calendar for one day: day_offset days from today (user=false is the public calendar of every show) """
    path = f'info=trakt_calendar&user=false&startdate={day_offset}&days=1&unstacked=true'
    for key, value in (extra or {}).items():  # countries / calendar_filter / genres of the Asian drama and Anime calendars
        path = f'{path}&{key}={value}'
    return f'{path}&endpoint={endpoint}&page={page}' if endpoint else f'{path}&page={page}'


def unwatched_path(tmdb_id, page=1, season=None):
    """ season: the season to list when the show has no synced Up Next data (a show you do not follow); the list falls back to season 1 without it """
    path = f'info=trakt_upnext&tmdb_type=tv&tmdb_id={tmdb_id}&page={page}'
    return f'{path}&fallback_season={season}' if season else path


def _load(path_for_page):
    """ Build every page of a list path into Kodi ListItems (the item builders' output, as a plugin directory would show it) """
    listitems = []
    for page in range(1, MAX_PAGES + 1):
        items = Router(-1, path_for_page(page)).get_directory(items_only=True) or []
        items = [i for i in items if i]
        has_next = any(i.next_page for i in items)
        listitems += [i.get_listitem(offscreen=True) for i in items if not i.next_page]
        if not has_next:
            break
    return listitems


def _tag(listitem):
    try:
        return listitem.getVideoInfoTag()
    except Exception:
        return None


def _show_tmdb_id(listitem):
    tag = _tag(listitem)
    try:
        value = tag.getUniqueID('tvshow.tmdb') if tag else None
    except Exception:
        value = None
    if value:
        return str(value)
    match = re.search(r'tmdb_id=(\d+)', listitem.getPath() or '')
    return match.group(1) if match else None


def _premiered(listitem):
    """ The air date as a date (None when unknown). The info tag returns it formatted for the user's region, so parse that format """
    tag = _tag(listitem)
    text = (tag.getPremiered() or tag.getFirstAired() or '') if tag else ''
    if not text:
        return None
    try:
        date = datetime.date.fromisoformat(text)
    except ValueError:
        date = None
    if date is None:
        try:
            import xbmc
            date = datetime.datetime.strptime(text, xbmc.getRegion('dateshort')).date()
        except (ValueError, TypeError):
            return None
    return date if date.year >= MIN_YEAR else None


def _days_until(premiered, today):
    return (premiered - today).days if premiered else None


def _status(days, watched):
    """ (state, text) for the right-hand badge of an episode row """
    if watched:
        return 'watched', 'Watched'
    if days is None:  # no air date known: not "Aired", and not a date to count down to
        return 'unaired', 'No air date'
    if days < 0:
        return 'aired', 'Aired'
    if days == 0:
        return 'today', 'Today'
    if days == 1:
        return 'unaired', 'Tomorrow'
    return 'unaired', f'In {days} days'


def _episode_row(listitem, today):
    """ Re-label a built episode item for its row: "1x02. Title" (the calendar label carries the show name, which the header row shows) """
    tag = _tag(listitem)
    season = tag.getSeason() if tag else 0
    episode = tag.getEpisode() if tag else 0
    title = tag.getTitle() if tag else ''
    premiered = _premiered(listitem)
    watched = bool(tag and tag.getPlayCount())
    days = _days_until(premiered, today)
    state, text = _status(days, watched)
    code = f'{season}x{episode:0>2}'
    listitem.setLabel(f'{code}. {title}' if title else code)
    listitem.setProperty('row_type', 'episode')
    listitem.setProperty('air_state', state)
    listitem.setProperty('air_status', text)
    listitem.setProperty('row_date', premiered.strftime('%d %b %Y') if premiered else '')
    badge = EPISODE_BADGES.get(listitem.getProperty('calendar_episode_type'))  # only calendar rows carry it; the Unwatched list has no badges
    if badge:
        listitem.setProperty('row_badge', badge)
    return listitem


def _first_art(listitems, *keys):
    for listitem in listitems:
        for key in keys:
            value = listitem.getArt(key)
            if value:
                return value
    return ''


def _header_row(title, tmdb_id, episodes, today):
    unwatched = [e for e in episodes if e.getProperty('air_state') != 'watched']
    unaired = [e for e in episodes if e.getProperty('air_state') == 'unaired']
    header = xbmcgui.ListItem(label=title, offscreen=True)
    header.setProperty('row_type', 'show')
    header.setProperty('show_tmdb_id', tmdb_id or '')
    seasons = [s for s in (_tag(e).getSeason() if _tag(e) else 0 for e in episodes) if s > 0]
    header.setProperty('show_season', str(max(seasons)) if seasons else '')  # the latest season airing in the calendar: the Unwatched list's fallback
    parts = [f'{len(episodes)} episode{"s" if len(episodes) != 1 else ""}']
    if unaired:
        parts.append(f'{len(unaired)} not aired yet')
    if len(unwatched) - len(unaired):
        parts.append(f'{len(unwatched) - len(unaired)} to watch')
    header.setProperty('show_summary', '  -  '.join(parts))
    art = {'poster': _first_art(episodes, 'tvshow.poster', 'poster'), 'fanart': _first_art(episodes, 'tvshow.fanart', 'fanart')}
    header.setArt({k: v for k, v in art.items() if v})
    return header


def build_overview(listitems, today):
    """ Group the calendar episodes by show: header row, then that show's episodes oldest first. Shows sorted by name. """
    shows = {}
    for listitem in listitems:
        tag = _tag(listitem)
        title = (tag.getTVShowTitle() if tag else '') or listitem.getLabel()
        key = _show_tmdb_id(listitem) or title
        shows.setdefault(key, {'title': title, 'items': []})['items'].append(_episode_row(listitem, today))
    rows = []
    for key, show in sorted(shows.items(), key=lambda kv: kv[1]['title'].lower()):
        episodes = sorted(show['items'], key=lambda e: (_premiered(e) or datetime.date.max, _tag(e).getSeason(), _tag(e).getEpisode()))
        tmdb_id = key if str(key).isdigit() else None
        rows.append(_header_row(show['title'], tmdb_id, episodes, today))
        rows += episodes
    return rows


class WindowShowCalendar(xbmcgui.WindowXMLDialog):
    def __init__(self, *args, **kwargs):
        self._title = kwargs.get('window_title') or 'Your TV Calendar'
        self._endpoint = kwargs.get('endpoint')  # premieres / new: the same page for those calendars
        self._day_mode = kwargs.get('user') == 'false'  # the All calendar: one day per page
        self._extra = {k: kwargs[k] for k in ('countries', 'calendar_filter', 'genres') if kwargs.get(k)}
        self._day = 0  # days from today
        self._initialised = False
        self._view = VIEW_SHOWS
        self._list = None
        self._items = []
        self._saved_position = 0

    def onInit(self):
        # Coming back from the info dialog calls onInit again: keep the view and the selected row
        if self._initialised:
            return
        self._initialised = True
        self.setProperty('window_title', self._title)
        if self._day_mode:
            self.setProperty('day_mode', 'True')
        self._list = self.getControl(LIST_ID)
        self._show_overview()

    def _set_items(self, items, empty_text):
        self._items = items
        self._list.reset()
        self._list.addItems(items)
        if items:
            self.clearProperty('empty_text')
            self.setFocusId(LIST_ID)
        else:
            self.setProperty('empty_text', empty_text)
            self.setFocusId(CLOSE_BUTTON_ID)

    def _show_overview(self, position=0):
        self._view = VIEW_SHOWS
        self.clearProperty('view_unwatched')
        today = _today()
        if self._day_mode:
            day = today + datetime.timedelta(days=self._day)
            if self._day:  # the Today button shows only when another day is open
                self.setProperty('not_today', 'True')
            else:
                self.clearProperty('not_today')
            self.setProperty('center_label', f'{day.strftime("%A %d %B %Y")}' + (' (today)' if not self._day else ''))
            path_for_page, empty_text = (lambda page: day_path(self._day, self._endpoint, page, self._extra)), 'No episodes on this day'
        else:
            self.setProperty('center_label', f'{(today - datetime.timedelta(days=DAYS_BACK)).strftime("%d %b")} - {(today + datetime.timedelta(days=DAYS_SPAN - DAYS_BACK - 1)).strftime("%d %b")}')
            path_for_page, empty_text = (lambda page: calendar_path(self._endpoint, page)), 'No episodes in your calendar for these dates'
        with BusyDialog():
            try:
                items = build_overview(_load(path_for_page), today)
            except Exception as exc:  # a failed load must not leave the window dead
                kodi_log(f'Show calendar: could not load the calendar: {exc}', 1)
                items = []
        self._set_items(items, empty_text)
        if items and 0 < position < len(items):
            self._list.selectItem(position)
        elif not items and self._day_mode:
            self.setFocusId(NEXT_BUTTON_ID)  # an empty day: land on the day buttons, not on Close

    def _navigate_day(self, delta):
        self._day += delta
        self._show_overview()

    def _show_unwatched(self, tmdb_id, title, season=None):
        self._view = VIEW_UNWATCHED
        self.setProperty('view_unwatched', 'True')
        self.setProperty('center_label', f'{title} - unwatched')
        today = _today()
        with BusyDialog():
            try:
                items = [i for i in _load(lambda page: unwatched_path(tmdb_id, page, season)) if not (_tag(i) and _tag(i).getPlayCount())]
                items = [_episode_row(i, today) for i in items]
            except Exception as exc:
                kodi_log(f'Show calendar: could not load the unwatched episodes of {tmdb_id}: {exc}', 1)
                items = []
        self._set_items(items, 'No unwatched episodes')

    def onClick(self, control_id):
        if control_id == PREV_BUTTON_ID:
            return self._navigate_day(-1)
        if control_id == NEXT_BUTTON_ID:
            return self._navigate_day(1)
        if control_id == TODAY_BUTTON_ID:
            return self._navigate_day(-self._day)
        if control_id == CLOSE_BUTTON_ID:
            return self.close()
        if control_id == BACK_BUTTON_ID:
            return self._go_back()
        if control_id == LIST_ID:
            return self._select()

    def onAction(self, action):
        action_id = action.getId()
        if action_id in ACTION_CLOSEWINDOW:
            return self._go_back()
        # All calendar: Left / Right on the list is the previous / next day (the list's own left / right point back at it).
        # An empty day has no list to focus, so there Left / Right change the day from whichever button has the focus.
        if self._day_mode and self._view == VIEW_SHOWS and (self.getFocusId() == LIST_ID or not self._items):
            if action_id == ACTION_MOVE_LEFT:
                return self._navigate_day(-1)
            if action_id == ACTION_MOVE_RIGHT:
                return self._navigate_day(1)

    def _go_back(self):
        if self._view == VIEW_UNWATCHED:
            return self._show_overview(self._saved_position)
        return self.close()

    def _select(self):
        position = self._list.getSelectedPosition()
        if position < 0 or position >= len(self._items):
            return
        item = self._items[position]
        if item.getProperty('row_type') == 'show':
            tmdb_id = item.getProperty('show_tmdb_id')
            if not tmdb_id:
                return
            self._saved_position = position
            return self._show_unwatched(tmdb_id, item.getLabel(), item.getProperty('show_season'))
        xbmcgui.Dialog().info(item)


def open_show_calendar(window_title=None, endpoint=None, user=None, countries=None, calendar_filter=None, genres=None):
    window = WindowShowCalendar(
        'script-tmdbhelper-showcalendar.xml', ADDONPATH, 'default', '1080i', window_title=window_title, endpoint=endpoint, user=user,
        countries=countries, calendar_filter=calendar_filter, genres=genres)
    window.doModal()
    del window
