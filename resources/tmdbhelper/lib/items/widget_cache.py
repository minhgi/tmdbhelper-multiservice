"""
Widget Directory Cache

Caches fully-built widget directory listings so that spurious re-requests
(e.g. after YouTube trailer playback causes skin to unload/reload widgets)
can be served instantly without running the full Trakt sync + TMDb API pipeline.

Cache is invalidated whenever reload_widgets() is called (Trakt sync, mark watched, etc.)
via the TMDbHelper.Widgets.LastInvalidated window property.
"""

import os
import hashlib
import re

import xbmcgui
import xbmcvfs

from tmdbhelper.lib.addon.logger import kodi_log


# Cache lives in addon_data temp directory
_CACHE_DIR = None


def _get_cache_dir():
    global _CACHE_DIR
    if _CACHE_DIR is None:
        import xbmcaddon
        profile = xbmcvfs.translatePath(xbmcaddon.Addon().getAddonInfo('profile'))
        _CACHE_DIR = os.path.join(profile, 'widget_cache')
        os.makedirs(_CACHE_DIR, exist_ok=True)
    return _CACHE_DIR


def _get_settings_salt():
    """Build a salt string from settings that affect widget content.

    When any of these settings change, the cache key changes, forcing a cache
    miss and a fresh build with the correct data. This avoids serving stale
    widget content after a settings toggle (e.g., anime enrichment on/off).
    """
    try:
        from tmdbhelper.lib.addon.plugin import get_setting
        anime = '1' if get_setting('anime_enrichment') else '0'
    except Exception:
        anime = '0'
    return f'ae={anime}'


def _get_cache_key(paramstring):
    """Generate a stable cache key from paramstring, excluding reload= value.

    Incorporates a settings salt so that toggling settings that affect widget
    content (e.g., anime enrichment) produces a different key → cache miss.
    """
    clean = re.sub(r'reload=[^&]*', 'reload=_', paramstring)
    salt = _get_settings_salt()
    return hashlib.md5(f'{clean}|{salt}'.encode('utf-8')).hexdigest()[:16]


def _get_cache_path(cache_key):
    return os.path.join(_get_cache_dir(), f'{cache_key}.json')


def get_last_invalidated():
    """Get the timestamp of the last widget data invalidation."""
    home = xbmcgui.Window(10000)
    val = home.getProperty('TMDbHelper.Widgets.LastInvalidated')
    try:
        return float(val) if val else 0.0
    except (ValueError, TypeError):
        return 0.0


STARTUP_CACHE_MAX_AGE_HOURS = 6


def is_cache_valid(paramstring):
    """Check if a valid widget cache exists for this paramstring.
    Returns False if no cache, if data was invalidated since cache was written,
    or if LastInvalidated was never set this session (fresh Kodi startup).
    On startup (LastInvalidated == 0.0), serves previous session's cache if < 6h old
    to avoid forcing a full Trakt pipeline on every restart."""
    import time
    last_invalidated = get_last_invalidated()
    if last_invalidated == 0.0:
        # Fresh Kodi startup — window properties have been reset.
        # Check if we have a recent cache from a previous session.
        cache_key = _get_cache_key(paramstring)
        cache_path = _get_cache_path(cache_key)
        if os.path.exists(cache_path):
            cache_mtime = os.path.getmtime(cache_path)
            if (time.time() - cache_mtime) / 3600 < STARTUP_CACHE_MAX_AGE_HOURS:
                # Serve previous session's cache immediately.
                # Set LastInvalidated to just before cache_mtime so subsequent
                # HIT checks pass (cache_mtime > cache_mtime-1.0) and a later
                # reload_widgets() call from CronJob correctly invalidates by
                # setting time.time() which will be > cache_mtime.
                home = xbmcgui.Window(10000)
                home.setProperty('TMDbHelper.Widgets.LastInvalidated', str(cache_mtime - 1.0))
                kodi_log(f'WidgetCache: startup HIT ({(time.time()-cache_mtime)/60:.0f}min old)', 2)
                return True
        # No usable cache — initialize baseline and run full pipeline.
        home = xbmcgui.Window(10000)
        home.setProperty('TMDbHelper.Widgets.LastInvalidated', str(time.time()))
        return False
    cache_key = _get_cache_key(paramstring)
    cache_path = _get_cache_path(cache_key)
    if not os.path.exists(cache_path):
        return False
    cache_mtime = os.path.getmtime(cache_path)
    return cache_mtime > last_invalidated


def load_cache(paramstring, handle):
    """Load cached widget items and add them to the Kodi directory listing.
    Returns True if cache was served, False if cache miss."""
    from xbmcplugin import addDirectoryItems, setPluginCategory, setContent, endOfDirectory
    from infotagger.listitem import ListItemInfoTag
    from json import loads as json_loads

    cache_key = _get_cache_key(paramstring)
    cache_path = _get_cache_path(cache_key)

    try:
        with open(cache_path, 'r', encoding='utf-8') as f:
            data = json_loads(f.read())
    except Exception:
        return False

    if not data or not data.get('items'):
        return False

    kodi_items = []
    for ci in data['items']:
        li = xbmcgui.ListItem(label=ci.get('label', ''), label2=ci.get('label2', ''), path=ci.get('url', ''), offscreen=True)
        li.setArt(ci.get('art', {}))
        li.setProperties({str(k): str(v) for k, v in ci.get('infoproperties', {}).items() if v is not None})

        info_tag = ListItemInfoTag(li)
        info_tag.set_info(ci.get('infolabels', {}))
        info_tag.set_unique_ids(ci.get('unique_ids', {}))
        info_tag.set_cast(ci.get('cast', []))
        info_tag.set_stream_details(ci.get('stream_details', {}))

        # set_resume_point expects float values for ResumeTime/TotalTime
        # but save_cache stores all infoproperties as strings
        ip = ci.get('infoproperties', {})
        resume_point = {}
        for rk in ('ResumeTime', 'TotalTime'):
            rv = ip.get(rk)
            if rv is not None:
                try:
                    resume_point[rk] = float(rv)
                except (ValueError, TypeError):
                    pass
        if resume_point:
            info_tag.set_resume_point(resume_point)

        ctx = ci.get('context_menu')
        if ctx:
            li.addContextMenuItems(ctx)

        kodi_items.append((ci.get('url', ''), li, ci.get('is_folder', True)))

    addDirectoryItems(handle, kodi_items)
    setPluginCategory(handle, data.get('plugin_category', ''))
    setContent(handle, data.get('container_content', ''))
    endOfDirectory(handle, updateListing=False)

    kodi_log(f'WidgetCache: HIT for {cache_key} ({len(kodi_items)} items)', 2)
    return True


def save_cache(paramstring, items, plugin_category='', container_content=''):
    """Save built widget items to cache after the full pipeline has run."""
    from json import dumps as json_dumps

    if not items:
        return

    cache_key = _get_cache_key(paramstring)
    cache_path = _get_cache_path(cache_key)

    cached_items = []
    for li in items:
        if not li:
            continue
        try:
            ci = {
                'url': li.get_url(),
                'is_folder': li.is_folder,
                'label': li.label or '',
                'label2': li.label2 or '',
                'infolabels': li.infolabels or {},
                'art': li.set_art_fallbacks() if hasattr(li, 'set_art_fallbacks') else li.art or {},
                'infoproperties': {str(k): str(v) for k, v in (li.infoproperties or {}).items() if v is not None},
                'unique_ids': li.unique_ids or {},
                'cast': li.cast or [],
                'stream_details': li.stream_details or {},
                'context_menu': li.context_menu or [],
            }
            cached_items.append(ci)
        except Exception as exc:
            kodi_log(f'WidgetCache: Failed to serialize item: {exc}', 1)
            continue

    if not cached_items:
        return

    data = {
        'items': cached_items,
        'plugin_category': plugin_category,
        'container_content': container_content,
    }

    try:
        with open(cache_path, 'w', encoding='utf-8') as f:
            f.write(json_dumps(data))
        kodi_log(f'WidgetCache: SAVED {cache_key} ({len(cached_items)} items)', 2)
    except Exception as exc:
        kodi_log(f'WidgetCache: Failed to save: {exc}', 1)


def clear_cache():
    """Clear all cached widget files. Called from settings/maintenance."""
    cache_dir = _get_cache_dir()
    try:
        for f in os.listdir(cache_dir):
            if f.endswith('.json'):
                os.remove(os.path.join(cache_dir, f))
        kodi_log('WidgetCache: Cleared all cached widgets', 2)
    except Exception as exc:
        kodi_log(f'WidgetCache: Failed to clear: {exc}', 1)
