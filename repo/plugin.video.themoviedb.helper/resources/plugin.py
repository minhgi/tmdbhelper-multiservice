# Module: default
# Author: jurialmunkey
# License: GPL v.3 https://www.gnu.org/copyleft/gpl.html
if __name__ == '__main__':
    import sys
    try:
        # Kodi puts every add-on library folder ahead of the standard library. On Android those folders are on slow external storage
        # and a fresh interpreter lists each one on its first import, so look in the standard library and our own folders first
        import os
        std = os.path.normcase(os.path.normpath(os.path.dirname(os.__file__)))
        zipdir = os.path.dirname(std)

        def in_std(p):
            n = os.path.normcase(os.path.normpath(p))
            if 'packages' in os.path.basename(n):
                return False  # site-packages / dist-packages hold third-party code that add-on libraries must be able to override
            return n == std or n.startswith(std + os.sep) or (n.endswith('.zip') and os.path.dirname(n) == zipdir)

        front = [p for p in sys.path if in_std(p)]
        need = [p for p in sys.path if p not in front and ('themoviedb.helper' in p or 'script.module.jurialmunkey' in p or 'script.module.infotagger' in p)]
        sys.path[:] = front + need + [p for p in sys.path if p not in front and p not in need]
    except Exception:
        pass
    from tmdbhelper.lib.items.router import Router
    Router(int(sys.argv[1]), sys.argv[2][1:]).run()
