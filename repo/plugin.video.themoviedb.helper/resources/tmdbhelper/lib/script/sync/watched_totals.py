"""Keep the local watched totals (show / season `watched_episodes`) in step with a single episode mark.

Marking one episode watched or unwatched used to write only the episode row; the show and season totals the home
widget badge reads stayed as the last periodic sync left them (an unwatch never lowered the badge, and the badge is the
highest of the Trakt / MDBList / Simkl totals). These helpers move the totals by the change the mark made.

MDBList keeps one row per watched episode, so its totals are recounted from those rows (this also repairs a stale total);
Simkl has no per-episode rows for older watches, so its totals move by the change the mark made. A bulk mark (9999)
cannot be moved by one episode and is left alone. Season 0 (specials) counts toward its own season
row only, as the periodic sync does for the show total.
"""
BULK = 9999


def previous_plays(cache, item_id):
    """Plays recorded for this episode row before the mark (0 when no row)."""
    try:
        row = cache.execute_sql('SELECT plays FROM simplecache WHERE id=?', (item_id,), read_only=True).fetchone()
        return int(row[0] or 0) if row else 0
    except Exception:
        return 0


def adjust_totals(cache, tmdb_id, season, delta, ts=None):
    """Add `delta` (+1 / -1 / 0) to the season row and, outside season 0, the show row of a tv show."""
    if not delta:
        return
    tmdb_id, season = int(tmdb_id), int(season)
    targets = [(f'tv.{tmdb_id}.{season}', 'season', season)]
    if season != 0:
        targets.append((f'tv.{tmdb_id}', 'show', None))
    for item_id, item_type, season_number in targets:
        row = cache.execute_sql('SELECT watched_episodes FROM simplecache WHERE id=?', (item_id,), read_only=True).fetchone()
        current = row[0] if row and row[0] is not None else None
        if current is not None and current >= BULK:
            continue
        if delta > 0:
            cache.execute_sql(cache.statement_insert_or_ignore('simplecache', ('id',)), (item_id,))
            cache.execute_sql(
                'UPDATE simplecache SET watched_episodes=?, item_type=?, tmdb_type=?, tmdb_id=?, season_number=? WHERE id=?',
                ((current or 0) + 1, item_type, 'tv', tmdb_id, season_number, item_id))
            if ts and item_type == 'show':
                cache.execute_sql('UPDATE simplecache SET last_watched_at=? WHERE id=?', (ts, item_id))
        elif current:
            cache.execute_sql('UPDATE simplecache SET watched_episodes=? WHERE id=?', (current - 1, item_id))


def recount_totals(cache, tmdb_id, season, ts=None):
    """Set the season row and, outside season 0, the show row to the number of watched episode rows (MDBList)."""
    tmdb_id, season = int(tmdb_id), int(season)
    targets = [(f'tv.{tmdb_id}.{season}', 'season', season,
                'SELECT count(*) FROM simplecache WHERE tmdb_id=? AND item_type=? AND season_number=? AND plays>0',
                (tmdb_id, 'episode', season))]
    if season != 0:
        targets.append((f'tv.{tmdb_id}', 'show', None,
                        'SELECT count(*) FROM simplecache WHERE tmdb_id=? AND item_type=? AND season_number>0 AND plays>0',
                        (tmdb_id, 'episode')))
    for item_id, item_type, season_number, count_sql, count_args in targets:
        row = cache.execute_sql('SELECT watched_episodes FROM simplecache WHERE id=?', (item_id,), read_only=True).fetchone()
        if row and row[0] is not None and row[0] >= BULK:
            continue
        count = cache.execute_sql(count_sql, count_args, read_only=True).fetchone()[0]
        if not row and not count:
            continue
        cache.execute_sql(cache.statement_insert_or_ignore('simplecache', ('id',)), (item_id,))
        cache.execute_sql(
            'UPDATE simplecache SET watched_episodes=?, item_type=?, tmdb_type=?, tmdb_id=?, season_number=? WHERE id=?',
            (count, item_type, 'tv', tmdb_id, season_number, item_id))
        if ts and item_type == 'show':
            cache.execute_sql('UPDATE simplecache SET last_watched_at=? WHERE id=?', (ts, item_id))
