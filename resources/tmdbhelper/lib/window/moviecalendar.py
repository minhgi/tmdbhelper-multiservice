import calendar
import datetime
import xbmcgui
from tmdbhelper.lib.items.router import Router
from tmdbhelper.lib.addon.dialog import BusyDialog
from tmdbhelper.lib.addon.logger import kodi_log
from tmdbhelper.lib.addon.plugin import ADDONPATH

""" Movie Calendar window: a month poster grid in the style of Seren's Movie Calendar.
Opened from the Movies menu row (info=dir_calendar_window); its items are the same ones the day-bucket calendar lists build
(info=trakt_calendar with tmdb_type=movie), asked for one whole month at a time.
"""

GRID_ID = 1000
PREV_BUTTON_ID = 3001
NEXT_BUTTON_ID = 3002
CLOSE_BUTTON_ID = 2999
ACTION_MOVE_LEFT = 1
ACTION_MOVE_RIGHT = 2
ACTION_SELECT = (7, 100,)
ACTION_CLOSEWINDOW = (9, 10, 92, 216, 247, 257, 275, 61467, 61448,)
MAX_PAGES = 10  # a month holds at most a few hundred titles; stop a runaway paging loop


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
        self._prev_focus = None

    def onInit(self):
        # Coming back from the info dialog calls onInit again: keep the month and the selected tile
        if self._initialised:
            return
        self._initialised = True
        self.setProperty('window_title', self._title)
        self._grid = self.getControl(GRID_ID)
        with BusyDialog():
            self._populate()
        self._focus_grid()

    def _load_month(self):
        listitems = []
        for page in range(1, MAX_PAGES + 1):
            items = Router(-1, month_path(self._year, self._month, self._user, self._endpoint, page, self._extra)).get_directory(items_only=True) or []
            items = [i for i in items if i]
            has_next = any(i.next_page for i in items)
            listitems += [i.get_listitem(offscreen=True) for i in items if not i.next_page]
            if not has_next:
                break
        listitems.sort(key=_premiered)
        return listitems

    def _populate(self):
        self.setProperty('month_year_label', f'{calendar.month_name[self._month]} {self._year}')
        try:
            self._items = self._load_month()
        except Exception as exc:  # a failed month must not leave the window dead
            kodi_log(f'Movie calendar: could not load {self._year}-{self._month:02d}: {exc}', 1)
            self._items = []
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
        with BusyDialog():
            self._populate()
        self._focus_grid()

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
        # An empty month has no grid to focus, so there Left / Right page the month from whichever button has the focus
        if not self._items and action_id in (ACTION_MOVE_LEFT, ACTION_MOVE_RIGHT):
            return self._navigate_month(-1 if action_id == ACTION_MOVE_LEFT else 1)
        # The grid's onleft / onright point at the Prev / Next buttons, so a Left / Right press that runs off the grid's edge
        # arrives with the focus already on a button while the previous action still had it on the grid: that pages the month
        if previous == GRID_ID and action_id == ACTION_MOVE_LEFT and focus_id == PREV_BUTTON_ID:
            return self._navigate_month(-1)
        if previous == GRID_ID and action_id == ACTION_MOVE_RIGHT and focus_id == NEXT_BUTTON_ID:
            return self._navigate_month(1)

    def _show_info(self):
        position = self._grid.getSelectedPosition()
        if position < 0 or position >= len(self._items):
            return
        xbmcgui.Dialog().info(self._items[position])


def open_movie_calendar(user=None, endpoint=None, window_title=None, countries=None, calendar_filter=None, genres=None):
    window = WindowMovieCalendar(
        'script-tmdbhelper-moviecalendar.xml', ADDONPATH, 'default', '1080i',
        user=user, endpoint=endpoint, window_title=window_title, countries=countries, calendar_filter=calendar_filter, genres=genres)
    window.doModal()
    del window
