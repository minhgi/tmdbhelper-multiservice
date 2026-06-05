"""
TMDb Helper — Settings Backup / Restore (Item 9)

Mirrors Seren's Session 15 backup_restore.py pattern.

Backs up:
  - settings.xml            (all addon settings incl. Trakt/MDbList tokens)
  - TraktSync_v2.db         (full Trakt sync state)
  - awards.db               (awards cache — 1.9 MB, slow to rebuild)
  - widget_cache/           (all JSON widget cache files)
  - players/                (user-defined player JSON overrides)

Creates a single timestamped zip: tmdbhelper_backup_YYYYMMDD_HHMMSS.zip
Restore extracts back into the addon profile directory.
"""

import datetime
import os
import shutil
import zipfile

import xbmcgui
import xbmcvfs

from tmdbhelper.lib.addon.logger import kodi_log


_ADDON_NAME = 'TMDb Helper'

_BACKUP_FILES = [
    'settings.xml',
    'TraktSync_v2.db',
    'awards.db',
]

_BACKUP_FOLDERS = [
    'widget_cache',
    'players',
]


def _get_profile():
    import xbmcaddon
    return xbmcvfs.translatePath(xbmcaddon.Addon().getAddonInfo('profile'))


def backup_settings():
    """Export settings and data to a user-chosen folder as a timestamped zip."""
    dest_folder = xbmcgui.Dialog().browseSingle(
        0,  # ShowAndGetDirectory
        f'{_ADDON_NAME}: Select Backup Location',
        'files', '', False, False,
    )
    if not dest_folder:
        return

    # Audit fix v6.0.29 — Bug #8: zipfile.ZipFile requires a local filesystem path.
    # Reject network shares (smb://, nfs://, http://) up front with a clear message
    # instead of failing mid-write with a cryptic OSError caught by the broad except.
    dest_folder_local = xbmcvfs.translatePath(dest_folder)
    if '://' in dest_folder_local or not os.path.isdir(dest_folder_local):
        xbmcgui.Dialog().ok(
            _ADDON_NAME,
            'Backup destination must be on the local filesystem.\n'
            'Network shares (SMB, NFS, HTTP) are not supported — pick a local '
            'folder, then copy the resulting zip to your share manually.',
        )
        return

    timestamp = datetime.datetime.now().strftime('%Y%m%d_%H%M%S')
    zip_name = f'tmdbhelper_backup_{timestamp}.zip'
    zip_path = os.path.join(dest_folder_local, zip_name)
    profile = _get_profile()

    try:
        file_count = 0
        with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zf:
            for filename in _BACKUP_FILES:
                filepath = os.path.join(profile, filename)
                if os.path.isfile(filepath):
                    zf.write(filepath, filename)
                    file_count += 1
                    kodi_log(f'backup_restore: backup added {filename}', 2)
            for folder_name in _BACKUP_FOLDERS:
                folder_path = os.path.join(profile, folder_name)
                if not os.path.isdir(folder_path):
                    continue
                for root, _dirs, files in os.walk(folder_path):
                    for f in files:
                        abs_path = os.path.join(root, f)
                        arc_name = os.path.relpath(abs_path, profile)
                        zf.write(abs_path, arc_name)
                        file_count += 1
                kodi_log(f'backup_restore: backup added folder {folder_name}/', 2)

        kodi_log(f'backup_restore: created {zip_path} ({file_count} files)', 1)
        xbmcgui.Dialog().notification(_ADDON_NAME, f'Backup saved — {file_count} file(s)', time=4000)

    except Exception as exc:
        kodi_log(f'backup_restore: backup FAILED — {exc}', 1)
        xbmcgui.Dialog().notification(_ADDON_NAME, 'Backup failed — check log', time=4000)


def restore_settings():
    """Restore settings and data from a previously created backup zip."""
    zip_path = xbmcgui.Dialog().browseSingle(
        1,  # ShowAndGetFile
        f'{_ADDON_NAME}: Select Backup File (.zip)',
        'files', '.zip', False, False,
    )
    if not zip_path:
        return

    zip_path = xbmcvfs.translatePath(zip_path)

    if not os.path.isfile(zip_path) or not zipfile.is_zipfile(zip_path):
        xbmcgui.Dialog().notification(_ADDON_NAME, 'Invalid backup file', time=4000)
        return

    if not xbmcgui.Dialog().yesno(
            _ADDON_NAME,
            'Restore will overwrite current settings and data. Continue?'):
        return

    profile = _get_profile()
    profile_real = os.path.realpath(profile)

    try:
        file_count = 0
        skipped = 0
        with zipfile.ZipFile(zip_path, 'r') as zf:
            for member in zf.namelist():
                # AUDIT v6.0.28: cross-platform safe path check.
                # Normalize separators, reject absolute paths (catches C:\... and \...
                # on Windows that the old startswith('/') check missed), reject any
                # path containing '..' as a component.
                norm = member.replace('\\', '/')
                parts = norm.split('/')
                if (os.path.isabs(member) or norm.startswith('/')
                        or '..' in parts
                        or (len(member) >= 2 and member[1] == ':')):  # Windows drive letter
                    kodi_log(f'backup_restore: rejecting unsafe path "{member}"', 1)
                    skipped += 1
                    continue
                if not _is_known_path(norm):
                    kodi_log(f'backup_restore: skipping unknown path "{member}"', 2)
                    skipped += 1
                    continue
                if norm.endswith('/'):
                    target_dir = os.path.join(profile, *parts)
                    os.makedirs(target_dir, exist_ok=True)
                    continue
                # Belt-and-suspenders: realpath containment check
                dest_path = os.path.join(profile, *parts)
                dest_real = os.path.realpath(dest_path)
                if not dest_real.startswith(profile_real + os.sep) and dest_real != profile_real:
                    kodi_log(f'backup_restore: realpath escape blocked for "{member}"', 1)
                    skipped += 1
                    continue
                os.makedirs(os.path.dirname(dest_path), exist_ok=True)
                with zf.open(member) as src, open(dest_path, 'wb') as dst:
                    shutil.copyfileobj(src, dst)
                file_count += 1
                kodi_log(f'backup_restore: restored {member}', 2)

        kodi_log(f'backup_restore: restore complete ({file_count} files, {skipped} skipped)', 1)
        xbmcgui.Dialog().ok(
            _ADDON_NAME,
            f'Restore complete — {file_count} file(s) restored.\n'
            f'Please restart Kodi for all changes to take effect.',
        )

    except Exception as exc:
        kodi_log(f'backup_restore: restore FAILED — {exc}', 1)
        xbmcgui.Dialog().notification(_ADDON_NAME, 'Restore failed — check log', time=4000)


def _is_known_path(member):
    """Only restore paths belonging to known backup categories.
    Member must be already normalized to forward-slash form by the caller.
    """
    if member in _BACKUP_FILES:
        return True
    for folder in _BACKUP_FOLDERS:
        if member.startswith(f'{folder}/'):
            return True
    return False
