import calendar
import datetime
import xbmc
import xbmcgui
from functools import partial
from tmdbhelper.lib.items.router import Router
from tmdbhelper.lib.addon.dialog import CalendarBusyDialog
from tmdbhelper.lib.addon.logger import kodi_log
from tmdbhelper.lib.addon.plugin import ADDONPATH, get_setting
from tmdbhelper.lib.window.pagecache import PageCache, sync_stamp
from tmdbhelper.lib.window.calendar_menu import run_context_option, sync_snapshot, sync_changed_since

""" Movie Calendar window: a month poster grid in the style of Seren's Movie Calendar.
Opened from the Movies menu row (info=dir_calendar_window); its items are the same ones the day-bucket calendar lists build
(info=trakt_calendar with tmdb_type=movie), asked for one whole month at a time.
Left / Right off the edge of the grid page the month (the < > buttons do too); with the setting "Calendars: Right goes to the scrollbar" on, Right off
the grid's edge first moves to the scrollbar of a month longer than one screen and a second Right pages to the next month (Left off the first column pages back). The Context Menu key on a tile opens the same
options as the regular lists (Trakt / MDBList / Simkl / WeTrakr options with Watched, Unwatched ...) and the month reloads when one of them finishes.
"""

GRID_ID = 1000
PREV_BUTTON_ID = 3001
NEXT_BUTTON_ID = 3002
CLOSE_BUTTON_ID = 2999
SCROLLBAR_ID = 1111
TILES_PER_PAGE = 16  # 8 columns x 2 rows when the page count cannot be read
ACTION_MOVE_LEFT = 1
ACTION_MOVE_RIGHT = 2
ACTION_CONTEXT_MENU = 117
ACTION_SELECT = (7, 100,)
ACTION_CLOSEWINDOW = (9, 10, 92, 216, 247, 257, 275, 61467, 61448,)
OPEN_PROPERTY = 'TMDbHelper.MovieCalendarWindow'  # Home window property: the id of the Movie Calendar window while it is open
MAX_PAGES = 10  # a month holds at most a few hundred titles; stop a runaway paging loop
PREFETCH_FORWARD_DELAY = 3.0  # land on a month, build the next month after this many seconds ...
PREFETCH_BACKWARD_DELAY = 8.0  # ... and the previous month 5 seconds after that


def _today():
    from tmdbhelper.lib.addon.tmdate import get_datetime_today
    return get_datetime_today().date()


def shift_month(year, month, delta):
    index = year * 12 + (month - 1) + delta
    return index // 12, index % 12 + 1


def month_path(year, month, user=None, endpoint=None, page=1, extra=None):
    """ The calendar list path for a whole month, built the way the day-bucket rows build theirs """
    first = datetime.date(year, month, 1)
    path = f'info=trakt_calendar&tmdb_type=movie&startdate={(first - _today()).days}&days={calendar.monthrange(year, month)[1]}'
    if endpoint:
        path = f'{path}&endpoint={endpoint}'
    if user:
        path = f'{path}&user={user}'
    for key, value in (extra or {}).items():  # countries / calendar_filter / genres of the Asian and Anime calendars
        path = f'{path}&{key}={value}'
    return f'{path}&page={page}'


def _premiered(listitem):
    try:
        return listitem.getVideoInfoTag().getPremiered() or ''
    except Exception:
        return ''


def _build_month(key, user, endpoint, extra):
    """ The page cache's build function (a plain function, so the cache holds no window): the movies of month key = (year, month), by release date,
    and each tile's context menu entries [(label, command)] under id(listitem) """
    year, month = key
    listitems, menus = [], {}
    for page in range(1, MAX_PAGES + 1):
        items = Router(-1, month_path(year, month, user, endpoint, page, extra)).get_directory(items_only=True) or []
        items = [i for i in items if i]
        has_next = any(i.next_page for i in items)
        for i in items:
            if i.next_page:
                continue
            listitem = i.get_listitem(offscreen=True)
            menus[id(listitem)] = list(i.context_menu or [])
            listitems.append(listitem)
        if not has_next:
            break
    listitems.sort(key=_premiered)
    return listitems, menus


def _month_neighbours(key):
    return [(PREFETCH_FORWARD_DELAY, shift_month(*key, 1)), (PREFETCH_BACKWARD_DELAY, shift_month(*key, -1))]


class WindowMovieCalendar(xbmcgui.WindowXMLDialog):
    def __init__(self, *args, **kwargs):
        self._user = kwargs.get('user')
        self._endpoint = kwargs.get('endpoint')
        self._extra = {k: kwargs[k] for k in ('countries', 'calendar_filter', 'genres') if kwargs.get(k)}
        self._title = kwargs.get('window_title') or 'Movie Calendar'
        today = _today()
        self._year, self._month = today.year, today.month
        self._initialised = False
        self._grid = None
        self._items = []
        self._menus = {}  # id(ListItem) -> its context menu entries [(label, command)]
        self._prev_focus = None
        self._right_to_scrollbar = bool(get_setting('calendar_right_scrollbar'))  # read when the window opens
        # the built months, and the prefetch of the months around the open one
        self._pages = PageCache(partial(_build_month, user=self._user, endpoint=self._endpoint, extra=self._extra), _month_neighbours, 'Movie calendar', stamp=sync_stamp)

    def onInit(self):
        # Coming back from the info dialog calls onInit again: keep the month and the selected tile
        if self._initialised:
            return
        self._initialised = True
        xbmcgui.Window(10000).setProperty(OPEN_PROPERTY, str(xbmcgui.getCurrentWindowDialogId()))
        self.setProperty('window_title', self._title)
        self._grid = self.getControl(GRID_ID)
        self._show_month()

    def _show_month(self, position=0):
        key = (self._year, self._month)
        self._pages.cancel()  # the months around the one just left are not wanted any more
        with CalendarBusyDialog():  # the dialog appears only when the month takes longer than a moment (not for a month built before or by the prefetch)
            self._populate(key)
        self._focus_grid()
        if self._items and position > 0:
            self._grid.selectItem(min(position, len(self._items) - 1))  # back on the tile the Context Menu was opened on
        self._pages.schedule(key)  # build the next month and then the previous one in the background

    def _populate(self, key):
        self.setProperty('month_year_label', f'{calendar.month_name[self._month]} {self._year}')
        try:
            self._items, self._menus = self._pages.page(key)
        except Exception as exc:  # a failed month must not leave the window dead
            kodi_log(f'Movie calendar: could not load {self._year}-{self._month:02d}: {exc}', 1)
            self._items, self._menus = [], {}
        self._grid.reset()
        self._grid.addItems(self._items)
        if self._items:
            self.clearProperty('no_items')
        else:
            self.setProperty('no_items', 'True')

    def _focus_grid(self):
        # An empty panel cannot take focus: land on the Next button instead
        focus_id = GRID_ID if self._items else NEXT_BUTTON_ID
        self.setFocusId(focus_id)
        self._prev_focus = focus_id  # the next Left / Right press starts from here, not from the button that paged

    def _navigate_month(self, delta):
        self._year, self._month = shift_month(self._year, self._month, delta)
        self._show_month()

    def onClick(self, control_id):
        if control_id == PREV_BUTTON_ID:
            return self._navigate_month(-1)
        if control_id == NEXT_BUTTON_ID:
            return self._navigate_month(1)
        if control_id == CLOSE_BUTTON_ID:
            return self.close()
        if control_id == GRID_ID:
            return self._show_info()

    def onAction(self, action):
        action_id = action.getId()
        focus_id = self.getFocusId()
        previous, self._prev_focus = self._prev_focus, focus_id
        if action_id in ACTION_CLOSEWINDOW:
            return self.close()
        if action_id == ACTION_CONTEXT_MENU and focus_id == GRID_ID:
            return self._context_menu()
        if self._right_to_scrollbar:
            # The grid's onleft / onright point at the buttons, so a Left / Right press that runs off the grid's edge arrives with the focus already on
            # a button. Right off the right edge of a month longer than one screen goes to the scrollbar first and a second Right (on the scrollbar)
            # pages to the next month; Left off the first column pages back; a month that fits one screen has no scrollbar and Right pages at once
            if self._items and previous == GRID_ID and action_id in (ACTION_MOVE_LEFT, ACTION_MOVE_RIGHT) and focus_id in (PREV_BUTTON_ID, NEXT_BUTTON_ID):
                if action_id == ACTION_MOVE_RIGHT and self._longer_than_a_page():
                    return self.setFocusId(SCROLLBAR_ID)
                return self._navigate_month(-1 if action_id == ACTION_MOVE_LEFT else 1)
            if action_id == ACTION_MOVE_RIGHT and focus_id == SCROLLBAR_ID:
                return self._navigate_month(1)
            if not self._items and action_id in (ACTION_MOVE_LEFT, ACTION_MOVE_RIGHT):  # an empty month has no grid
                return self._navigate_month(-1 if action_id == ACTION_MOVE_LEFT else 1)
            return
        # An empty month has no grid to focus, so there Left / Right page the month from whichever button has the focus
        if not self._items and action_id in (ACTION_MOVE_LEFT, ACTION_MOVE_RIGHT):
            return self._navigate_month(-1 if action_id == ACTION_MOVE_LEFT else 1)
        # The grid's onleft / onright point at the Prev / Next buttons, so a Left / Right press that runs off the grid's edge
        # arrives with the focus already on a button while the previous action still had it on the grid: that pages the month
        if previous == GRID_ID and action_id == ACTION_MOVE_LEFT and focus_id == PREV_BUTTON_ID:
            return self._navigate_month(-1)
        if previous == GRID_ID and action_id == ACTION_MOVE_RIGHT and focus_id == NEXT_BUTTON_ID:
            return self._navigate_month(1)

    def _longer_than_a_page(self):
        """ The scrollbar (showonepage=false) exists only for a month of more than one screen """
        try:
            return int(xbmc.getInfoLabel(f'Container({GRID_ID}).NumPages') or 0) > 1
        except ValueError:
            return len(self._items) > TILES_PER_PAGE

    def _context_menu(self):
        """ The options the regular lists give this tile, then a reload of the month so the tile shows the new state """
        position = self._grid.getSelectedPosition()
        if position < 0 or position >= len(self._items):
            return
        if run_context_option(self._menus.get(id(self._items[position])), 'Movie calendar'):
            self._pages.clear()  # the option that just ran may have changed the other months too
            self._show_month(position)

    def _show_info(self):
        position = self._grid.getSelectedPosition()
        if position < 0 or position >= len(self._items):
            return
        snapshot = sync_snapshot()
        xbmcgui.Dialog().info(self._items[position])
        self._pages.clear()  # Play / Watched in the information dialog change what every month shows
        # Its Trakt Manager runs as a script of its own, so this window is not told: when the sync data moved while the dialog was open the month is
        # reloaded, on the same tile (the context menu does the same); a dialog that changed nothing leaves the month as it is (no reload, no busy dialog)
        if sync_changed_since(snapshot):
            self._show_month(position)


def open_movie_calendar(user=None, endpoint=None, window_title=None, countries=None, calendar_filter=None, genres=None):
    home = xbmcgui.Window(10000)
    open_id = home.getProperty(OPEN_PROPERTY)
    if open_id and xbmc.getCondVisibility(f'Window.IsActive({open_id})'):
        # Already open: the Container.Refresh a Trakt / MDBList / Simkl option sends (also the one of the information dialog's Trakt Manager) re-runs the
        # path that opened the window and would stack a second calendar on top of this one (7.5.24; the TV calendar has the same guard)
        kodi_log('Movie calendar: already open, the request to open it again is ignored', 1)
        return
    window = WindowMovieCalendar(
        'script-tmdbhelper-moviecalendar.xml', ADDONPATH, 'default', '1080i',
        user=user, endpoint=endpoint, window_title=window_title, countries=countries, calendar_filter=calendar_filter, genres=genres)
    try:
        window.doModal()
    finally:
        home.clearProperty(OPEN_PROPERTY)
        window._pages.close()  # stop the prefetch before the script ends
    del window
