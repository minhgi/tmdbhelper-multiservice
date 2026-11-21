import re
import datetime
import xbmc
from functools import partial
import xbmcgui
from tmdbhelper.lib.items.router import Router
from tmdbhelper.lib.addon.dialog import CalendarBusyDialog
from tmdbhelper.lib.addon.logger import kodi_log
from tmdbhelper.lib.addon.plugin import ADDON, ADDONPATH, SHOW_CALENDAR_PROPERTY, WARM_CALENDAR_PROPERTY, get_setting
from tmdbhelper.lib.window.pagecache import PageCache, sync_stamp

""" Your TV Calendar window: every show you follow on one scrollable page. A header row per show, its episodes under it
(aired and not yet aired); Select on a show lists that show's unwatched episodes, Select on an episode opens its information
(with Play and the other buttons); the Context Menu key on a row opens the same options as the regular lists (Trakt / MDBList / Simkl
Options with Watched, Unwatched, Drop ...), and the page reloads when one of them finishes. The episodes are the ones the day-by-day calendar builds (info=trakt_calendar, unstacked) for a
span around today; a show's unwatched list comes from its Up Next list (info=trakt_upnext).
The All TV Calendar (user=false) uses the same page for ONE DAY at a time: Left / Right (or the < > buttons) change the day; with the setting
"All TV Calendar: Right goes to the scrollbar" on, only the buttons do and Right on the list moves to the scrollbar.
With the setting "TV calendars: landscape artwork and episode thumbnails" on, the rows and the large image at the side use the art key row_wide.
"""

LIST_ID = 1000
BACK_BUTTON_ID = 3003
PREV_BUTTON_ID = 3001
NEXT_BUTTON_ID = 3002
TODAY_BUTTON_ID = 3004
SCROLLBAR_ID = 1111
OPEN_PROPERTY = SHOW_CALENDAR_PROPERTY  # Home window property: the id of the calendar window that is open
ROWS_PER_PAGE = 12  # the list is 900 px high, a row 72 px: the scrollbar (showonepage=false) is there only for a longer list
CLOSE_BUTTON_ID = 2999
ACTION_MOVE_LEFT = 1
ACTION_MOVE_RIGHT = 2
ACTION_CONTEXT_MENU = 117
ACTION_CLOSEWINDOW = (9, 10, 92, 216, 247, 257, 275, 61467, 61448,)
DAYS_BACK = 14
DAYS_AHEAD = 30  # 7.0.127: was 18 (one 33-day request); the page now asks for as many windows as it takes, like Seren's My Watch List calendar
DAYS_SPAN = 31  # days one window asks for: Trakt's calendar answers at most 33 days in one request and the list builder adds a day on each side
MAX_PAGES = 10  # stop a runaway paging loop
PREFETCH_FORWARD_DELAY = 3.0  # All calendar: land on a day, build the next day after this many seconds ...
PREFETCH_BACKWARD_DELAY = 8.0  # ... and the previous day 5 seconds after that
PAGE_KEY = 'page'  # the one page of Your TV Calendar / Premieres / New in the page cache (the All calendars use the day offset)

VIEW_SHOWS = 'shows'
VIEW_UNWATCHED = 'unwatched'

# Trakt's episode_type values that get a badge on the row (anything else, "standard" included, gets none). Short, so the badge fits beside the title.
EPISODE_BADGES = {  # episode_type: (string id of this add-on, English text used when the string is missing)
    'series_premiere': (32799, 'New show'),
    'season_premiere': (32800, 'Premiere'),
    'mid_season_premiere': (32801, 'Mid premiere'),
    'mid_season_finale': (32802, 'Mid finale'),
    'season_finale': (32803, 'Finale'),
    'series_finale': (32804, 'Series finale'),
}
MIN_YEAR = 1900  # Kodi writes an unknown date as 1601-01-01 (its zero date); nothing real airs before this


def _today():
    from tmdbhelper.lib.addon.tmdate import get_datetime_today
    return get_datetime_today().date()


def _region_date(date, region='dateshort'):
    """ A date in the user's Kodi regional format ('dateshort' / 'datelong'), day and month names in the Kodi interface language.
    (strftime alone writes English names; Kodi's own strings 11-17 / 41-47 are Monday-Sunday long / short and 21-32 / 51-62 January-December long / short.) """
    names = {
        '%A': xbmc.getLocalizedString(11 + date.weekday()), '%a': xbmc.getLocalizedString(41 + date.weekday()),
        '%B': xbmc.getLocalizedString(21 + date.month - 1), '%b': xbmc.getLocalizedString(51 + date.month - 1)}
    names = {code: name for code, name in names.items() if name}  # a name Kodi does not give: strftime's own (English) name stays
    tokens = {code: f'@@{code[1]}@@' for code in names}  # ASCII placeholders survive strftime; the names go in afterwards
    fmt = xbmc.getRegion(region) or '%Y-%m-%d'
    for code, token in tokens.items():
        fmt = fmt.replace(code, token)
    try:
        text = date.strftime(fmt)
    except (ValueError, TypeError):
        return date.isoformat()
    for code, token in tokens.items():
        text = text.replace(token, names[code])
    return text


def calendar_windows():
    """ (first day offset, number of days) of each request that together cover DAYS_BACK days back to DAYS_AHEAD days ahead, DAYS_SPAN days at most each """
    windows, start = [], -DAYS_BACK
    while start <= DAYS_AHEAD:
        days = min(DAYS_SPAN, DAYS_AHEAD - start + 1)
        windows.append((start, days))
        start += days
    return windows


def calendar_path(endpoint=None, page=1, start=None, days=None):
    """ One window of Your TV Calendar (the first one by default) """
    start = -DAYS_BACK if start is None else start
    path = f'info=trakt_calendar&startdate={start}&days={DAYS_SPAN if days is None else days}&unstacked=true'
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


def _load(path_for_page, menus=None):
    """ Build every page of a list path into Kodi ListItems (the item builders' output, as a plugin directory would show it).
    menus: when given, each ListItem's context menu entries [(label, command)] are stored in it under id(listitem). """
    listitems = []
    for page in range(1, MAX_PAGES + 1):
        items = Router(-1, path_for_page(page)).get_directory(items_only=True) or []
        items = [i for i in items if i]
        has_next = any(i.next_page for i in items)
        for i in items:
            if i.next_page:
                continue
            listitem = i.get_listitem(offscreen=True)
            if menus is not None:
                menus[id(listitem)] = list(i.context_menu or [])
            listitems.append(listitem)
        if not has_next:
            break
    return listitems


def _tag(listitem):
    try:
        return listitem.getVideoInfoTag()
    except Exception:
        return None


def _episode_key(listitem):
    """ Who an episode row is, to drop the ones two neighbouring windows both return (their edges overlap by a day) """
    tag = _tag(listitem)
    return (_show_tmdb_id(listitem), tag.getSeason() if tag else 0, tag.getEpisode() if tag else 0, listitem.getProperty('calendar_code'), listitem.getLabel(), _premiered(listitem))


def _load_windows(endpoint, menus=None):
    """ Every window of Your TV Calendar, each one built page by page like _load does; an episode two windows both return is listed once """
    listitems, seen = [], set()
    for start, days in calendar_windows():
        for listitem in _load(lambda page: calendar_path(endpoint, page, start, days), menus):
            key = _episode_key(listitem)
            if key in seen:
                continue
            seen.add(key)
            listitems.append(listitem)
    return listitems


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


def _string(string_id):
    """ A localized string: Kodi's own below 32000 (Today 33006, Tomorrow 33007, Watched 16102), this add-on's from 32000 """
    return (ADDON.getLocalizedString(string_id) if string_id >= 32000 else xbmc.getLocalizedString(string_id)) or ''


def _status(days, watched):
    """ (state, text) for the right-hand badge of an episode row """
    if watched:
        return 'watched', _string(16102) or 'Watched'
    if days is None:  # no air date known: not "Aired", and not a date to count down to
        return 'unaired', _string(32796) or 'No air date'
    if days < 0:
        return 'aired', _string(32795) or 'Aired'
    if days == 0:
        return 'today', _string(33006) or 'Today'
    if days == 1:
        return 'unaired', _string(33007) or 'Tomorrow'
    return 'unaired', (_string(32797) or 'In {} days').format(days)


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
    code = listitem.getProperty('calendar_code') or f'{season}x{episode:0>2}'   # 'calendar_code': an anime episode not placed in TMDb's numbering, e.g. "E3"
    listitem.setLabel(f'{code}. {title}' if title else code)
    listitem.setProperty('row_type', 'episode')
    listitem.setProperty('air_state', state)
    listitem.setProperty('air_status', text)
    listitem.setProperty('row_date', _region_date(premiered) if premiered else '')
    try:
        rating = tag.getRating() if tag else 0
    except Exception:
        rating = 0
    listitem.setProperty('row_rating', f'{rating:.1f}' if rating and rating > 0 else '')  # shown under the poster of the selected row (7.0.127)
    wide = _first_art([listitem], 'still', 'thumb', 'tvshow.landscape', 'landscape', 'tvshow.fanart', 'fanart', 'tvshow.poster', 'poster')  # the art the landscape setting shows; always set, so a cached page follows the setting
    if wide:
        listitem.setArt({'row_wide': wide})
    badge = EPISODE_BADGES.get(listitem.getProperty('calendar_episode_type'))  # only calendar rows carry it; the Unwatched list has no badges
    if badge:
        listitem.setProperty('row_badge', _string(badge[0]) or badge[1])
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
    art = {'poster': _first_art(episodes, 'tvshow.poster', 'poster'), 'fanart': _first_art(episodes, 'tvshow.fanart', 'fanart'),
           'landscape': _first_art(episodes, 'tvshow.landscape', 'landscape')}
    art['row_wide'] = art['landscape'] or art['fanart'] or art['poster']  # a show: its landscape art, else its fanart
    header.setArt({k: v for k, v in art.items() if v})
    return header


def _show_menu(tmdb_id):
    """ The context menu of a show header row: the options the regular lists give a TV show (Trakt / MDBList / Simkl Options, ...) """
    from types import SimpleNamespace
    from tmdbhelper.lib.items.context import ContextMenu
    show = SimpleNamespace(ftv_id=None, tmdb_id=tmdb_id, tmdb_type='tv', trakt_type='show', season=None, episode=None, infolabels={'mediatype': 'tvshow'})
    return ContextMenu(show).get()


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


def _build_page(load, today):
    """ One page of the calendar as (rows, menus): the rows build_overview makes and the context menu entries of each row by id(ListItem).
    load(menus) builds the list items (and stores their context menus in menus). Runs in the window and, for the All calendar, in the prefetch thread. """
    menus = {}
    items = build_overview(load(menus), today)
    for item in items:
        if item.getProperty('row_type') == 'show' and item.getProperty('show_tmdb_id'):
            menus[id(item)] = _show_menu(item.getProperty('show_tmdb_id'))
    return items, menus


def _build_overview(key, endpoint, extra, day_mode):
    """ The page cache's build function (a plain function, so the cache holds no window): the All calendars build the day `key`, the others their one page """
    if day_mode:
        return _build_page(lambda menus: _load(lambda page: day_path(key, endpoint, page, extra), menus), _today())
    return _build_page(lambda menus: _load_windows(endpoint, menus), _today())


def _day_neighbours(day):
    return [(PREFETCH_FORWARD_DELAY, day + 1), (PREFETCH_BACKWARD_DELAY, day - 1)]


class WindowShowCalendar(xbmcgui.WindowXMLDialog):
    def __init__(self, *args, **kwargs):
        self._title = kwargs.get('window_title') or 'Your TV Calendar'
        self._endpoint = kwargs.get('endpoint')  # premieres / new: the same page for those calendars
        self._day_mode = kwargs.get('user') == 'false'  # the All calendar: one day per page
        self._extra = {k: kwargs[k] for k in ('countries', 'calendar_filter', 'genres') if kwargs.get(k)}
        # the built pages (and, for the All calendars, the prefetch of the days around the open one)
        self._pages = PageCache(partial(_build_overview, endpoint=self._endpoint, extra=self._extra, day_mode=self._day_mode),
                                _day_neighbours if self._day_mode else None, 'Show calendar', stamp=sync_stamp)
        self._day = 0  # days from today
        self._initialised = False
        self._view = VIEW_SHOWS
        self._list = None
        self._items = []
        self._menus = {}  # id(ListItem) -> its context menu entries [(label, command)]
        self._saved_position = 0
        self._right_to_scrollbar = bool(get_setting('calendar_right_scrollbar'))  # read when the window opens
        self._wide_art = bool(get_setting('calendar_wide_art'))
        self._unwatched = None  # (tmdb_id, title, season) of the Unwatched view that is open

    def onInit(self):
        # Coming back from the info dialog calls onInit again: keep the view and the selected row
        if self._initialised:
            return
        self._initialised = True
        xbmcgui.Window(10000).setProperty(OPEN_PROPERTY, str(xbmcgui.getCurrentWindowDialogId()))
        xbmcgui.Window(10000).clearProperty(WARM_CALENDAR_PROPERTY)  # a menu asked the service to warm this page: the window builds it itself now
        self.setProperty('window_title', self._title)
        if self._day_mode:
            self.setProperty('day_mode', 'True')
        if self._wide_art:
            self.setProperty('wide_art', 'True')  # the skin shows the row_wide art instead of the poster
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
            self.setProperty('center_label', _region_date(day, 'datelong') + (f' ({_string(33006) or "Today"})' if not self._day else ''))
            empty_text = 'No episodes on this day'
        else:
            self.setProperty('center_label', f'{_region_date(today - datetime.timedelta(days=DAYS_BACK))} - {_region_date(today + datetime.timedelta(days=DAYS_AHEAD))}')
            empty_text = 'No episodes in your calendar for these dates'
        self._menus = {}
        self._pages.cancel()  # the pages around the one just left are not wanted any more
        key = self._day if self._day_mode else PAGE_KEY
        try:
            with CalendarBusyDialog():  # the dialog appears only when the page takes longer than a moment (not for a page built before or by the prefetch)
                items, self._menus = self._pages.page(key)
        except Exception as exc:  # a failed load must not leave the window dead
            kodi_log(f'Show calendar: could not load the calendar: {exc}', 1)
            items, self._menus = [], {}
        self._set_items(items, empty_text)
        if items and position > 0:
            self._list.selectItem(min(position, len(items) - 1))
        elif not items and self._day_mode:
            self.setFocusId(NEXT_BUTTON_ID)  # an empty day: land on the day buttons, not on Close
        self._pages.schedule(key)  # the All calendars: build the next day and then the previous one in the background

    def _navigate_day(self, delta):
        self._day += delta
        self._show_overview()

    def _show_unwatched(self, tmdb_id, title, season=None, position=0):
        self._view = VIEW_UNWATCHED
        self._unwatched = (tmdb_id, title, season)
        self.setProperty('view_unwatched', 'True')
        self.setProperty('center_label', f'{title} - unwatched')
        today = _today()
        self._menus = {}
        with CalendarBusyDialog():
            try:
                items = [i for i in _load(lambda page: unwatched_path(tmdb_id, page, season), self._menus) if not (_tag(i) and _tag(i).getPlayCount())]
                items = [_episode_row(i, today) for i in items]
            except Exception as exc:
                kodi_log(f'Show calendar: could not load the unwatched episodes of {tmdb_id}: {exc}', 1)
                items = []
        self._set_items(items, 'No unwatched episodes')
        if items and position > 0:
            self._list.selectItem(min(position, len(items) - 1))

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
        if action_id == ACTION_CONTEXT_MENU and self.getFocusId() == LIST_ID:
            return self._context_menu()
        # All calendar: Left / Right on the list is the previous / next day (the list's own left / right point back at it).
        # An empty day has no list to focus, so there Left / Right change the day from whichever button has the focus.
        # With "Right goes to the scrollbar" on this is skipped: Left / Right move the focus as in any other list and the buttons change the day.
        if self._day_mode and not self._right_to_scrollbar and self._view == VIEW_SHOWS and (self.getFocusId() == LIST_ID or not self._items):
            if action_id == ACTION_MOVE_LEFT:
                return self._navigate_day(-1)
            if action_id == ACTION_MOVE_RIGHT:
                return self._navigate_day(1)
        # Everywhere else Right on the list goes to the scrollbar (Left from the scrollbar is wired back to the list in the skin file)
        if action_id == ACTION_MOVE_RIGHT and self.getFocusId() == LIST_ID and len(self._items) > ROWS_PER_PAGE:
            return self.setFocusId(SCROLLBAR_ID)

    def _context_menu(self):
        """ The options the regular lists give this row, then a reload of the page so the row shows the new state (watched tick, gone from Unwatched, ...) """
        position = self._list.getSelectedPosition()
        if position < 0 or position >= len(self._items):
            return
        entries = [entry for entry in self._menus.get(id(self._items[position])) or [] if 'related_lists' not in str(entry[1])]  # Browse lists opens a list in the window under this one: nothing happens here
        if not entries:
            return
        choice = xbmcgui.Dialog().contextmenu([xbmc.getInfoLabel(name) or name for name, _ in entries])  # names are $ADDON[...] / $LOCALIZE[...] strings
        if choice < 0:
            return
        command = str(entries[choice][1])
        script = re.match(r'(?is)^\s*runscript\(\s*plugin\.video\.themoviedb\.helper\s*,(.*)\)\s*$', command)
        if not script:  # Play with ... and the like: nothing on the page changes
            return xbmc.executebuiltin(command)
        # RunScript would start the script in its own thread and return at once, before its dialogs and the sync are done: run it here instead,
        # so the page reloads only after the Trakt / MDBList / Simkl option (Watched, Unwatched, Drop ...) has finished and the row shows the new state
        try:
            from tmdbhelper.lib.script.router import Script
            Script(*[arg.strip() for arg in script.group(1).split(',')]).router()
        except Exception as exc:
            kodi_log(f'Show calendar: the context menu option {command} failed: {exc}', 1)
        self._refresh(position)

    def _refresh(self, position):
        self._pages.clear()  # the option that just ran may have changed rows on other days too
        if self._view == VIEW_UNWATCHED and self._unwatched:
            return self._show_unwatched(*self._unwatched, position=position)
        return self._show_overview(position)

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
        self._pages.clear()  # Play / Watched in the information dialog change what every day shows


def open_show_calendar(window_title=None, endpoint=None, user=None, countries=None, calendar_filter=None, genres=None):
    home = xbmcgui.Window(10000)
    open_id = home.getProperty(OPEN_PROPERTY)
    if open_id and xbmc.getCondVisibility(f'Window.IsActive({open_id})'):
        # Already open: a sync script's Container.Refresh re-runs the path that opened the window (a skin shortcut / favourite that points at it)
        # and would stack a second calendar on top of this one
        return
    window = WindowShowCalendar(
        'script-tmdbhelper-showcalendar.xml', ADDONPATH, 'default', '1080i', window_title=window_title, endpoint=endpoint, user=user,
        countries=countries, calendar_filter=calendar_filter, genres=genres)
    try:
        window.doModal()
    finally:
        home.clearProperty(OPEN_PROPERTY)
        window._pages.close()  # stop the prefetch before the script ends
    del window
