"""Where does an anime cour start in TMDb's episode list?

Simkl numbers every anime cour from episode 1 ("E12" = the 12th episode of that cour), while TMDb files a franchise under
one id with its own seasons, sometimes all in season 1. The AniList start date of the cour is the date of its first
episode, so the TMDb episode airing within START_WINDOW days of it is that cour's episode 1, and Simkl's E<n> is the
episode n-1 places after it in TMDb's order. When the offline Fribb index names the TMDb season of the cour and the episodes
before it (anchor_in_season), only that one season has to be fetched, and only an answer that agrees with the air dates is used.

The answer is only used when the episode it lands on has aired and airs about where a weekly show would (start date +
(n-1) weeks, with room for breaks that grows with n); batch releases and irregular shows therefore fail and the row is
left out, as it was before.
"""
import datetime

from tmdbhelper.lib.addon.logger import kodi_log

ANILIST_URL = 'https://graphql.anilist.co'
ANILIST_QUERY = (
    'query ($ids: [Int], $perpage: Int) { Page(perPage: $perpage) { media(id_in: $ids, type: ANIME) {'
    ' id format title { romaji english native } startDate { year month day } } } }')
ANILIST_BATCH = 50
ANILIST_TIMEOUT = 5       # seconds; Next Up opens must not wait long on AniList
START_WINDOW = 10         # days between the AniList start date and the TMDb episode taken as the cour's first
BASE_TOLERANCE = 21       # days the target episode may differ from start + (n-1) weeks ...
PER_EPISODE_TOLERANCE = 3  # ... plus this much for every further episode (breaks, delays)


def fetch_media(anilist_ids):
    """{AniList id: media} for the ids (id, format, title, startDate), 50 per request; {} or a partial result if AniList fails."""
    ids = list(dict.fromkeys(i for i in anilist_ids if i))
    media = {}
    if not ids:
        return media
    import requests
    for start in range(0, len(ids), ANILIST_BATCH):
        chunk = ids[start:start + ANILIST_BATCH]
        try:
            response = requests.post(
                ANILIST_URL, timeout=ANILIST_TIMEOUT, headers={'User-Agent': 'TMDbHelper'},
                json={'query': ANILIST_QUERY, 'variables': {'ids': chunk, 'perpage': ANILIST_BATCH}})
            page = (((response.json() or {}).get('data') or {}).get('Page') or {}) if response.status_code == 200 else {}
        except Exception as exc:
            kodi_log(f'Anime cours: AniList request failed: {type(exc).__name__}', 1)
            return media
        for m in page.get('media') or []:
            media[m['id']] = m
    return media


def _date(value):
    try:
        return datetime.date.fromisoformat(str(value)[:10])
    except (ValueError, TypeError):
        return None


def start_date(media):
    """The cour's AniList start date, or None unless AniList has the full year, month and day."""
    sd = (media or {}).get('startDate') or {}
    try:
        return datetime.date(int(sd['year']), int(sd['month']), int(sd['day']))
    except (KeyError, ValueError, TypeError):
        return None


def tmdb_episodes(tmdb_api, details):
    """[(season, episode, air date or None)] of TMDb seasons 1, 2, ... in order (specials left out)."""
    seasons = sorted(s.get('season_number') for s in details.get('seasons') or [] if (s.get('season_number') or 0) > 0)

    def get_season(number):
        return tmdb_api.get_request_sc('tv', details.get('id'), 'season', number) or {}

    if len(seasons) > 1:  # every season is needed to count the anchor's position, so ask for them side by side: a cold open waits for one request, not one per season
        from tmdbhelper.lib.addon.thread import ParallelThread
        with ParallelThread(seasons, get_season) as pt:
            fetched = pt.queue
        data = [s or {} for s in fetched]
    else:
        data = [get_season(number) for number in seasons]
    episodes = []
    for number, season in zip(seasons, data):
        episodes += [(number, e.get('episode_number'), _date(e.get('air_date'))) for e in season.get('episodes') or []]
    return episodes


def anchor_in_season(tmdb_api, tmdb_id, season, offset, start, n, today=None):
    """(season, episode) of Simkl's cour-episode n using ONE TMDb season, the one the Fribb index lists for the cour with the
    number of TMDb episodes before the cour (offset), or None.

    The index offset is wrong about one time in seven, and the weekly tolerance of anchor_episode would let an off-by-one
    offset through, so the offset only says where to look: the answer counts only when anchor_episode, run on that season
    alone, lands on exactly (season, offset + n), i.e. the cour's first episode is the one airing nearest to its AniList start
    date. Anything else (including a nearer episode in another season, which only the full search sees) returns None and the
    caller does the full search over every season."""
    if not start or n < 1 or not season or season < 1:
        return None
    data = tmdb_api.get_request_sc('tv', tmdb_id, 'season', season) or {}
    episodes = [(season, e.get('episode_number'), _date(e.get('air_date'))) for e in data.get('episodes') or []]
    found = anchor_episode(episodes, start, n, today)
    return found if found == (season, (offset or 0) + n) else None


def anchor_episode(episodes, start, n, today=None):
    """(season, episode) of Simkl's cour-episode n when the cour starts on `start`, or None when it cannot be told safely."""
    if not start or n < 1:
        return None
    near = [(abs((d - start).days), i) for i, (_, _, d) in enumerate(episodes) if d and abs((d - start).days) <= START_WINDOW]
    if not near:
        return None
    target = min(near)[1] + n - 1
    if target >= len(episodes):
        return None
    season, episode, aired = episodes[target]
    if aired is None or aired > (today or datetime.date.today()):
        return None
    expected = start + datetime.timedelta(weeks=n - 1)
    if abs((aired - expected).days) > BASE_TOLERANCE + PER_EPISODE_TOLERANCE * (n - 1):
        return None
    return season, episode
