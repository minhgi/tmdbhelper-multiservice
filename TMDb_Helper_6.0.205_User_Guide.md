# TMDb Helper 6.0.205 — Complete Features & Functions Guide

**Version:** 6.0.205 (Enhanced Fork — Personal Build)
**Platform:** Kodi 20 (Nexus) and above; developed and live-tested on Kodi 22
**Base add-on:** [jurialmunkey's TMDb Helper](https://github.com/jurialmunkey/plugin.video.themoviedb.helper)
**License:** GNU GPL v3

---

## Table of Contents

1. [What Is TMDb Helper?](#1-what-is-tmdb-helper)
2. [What's New Since the v6.0.81 README](#2-whats-new-since-the-v6081-readme)
3. [First-Time Setup](#3-first-time-setup)
4. [Navigation & Menus](#4-navigation--menus)
5. [Anime and Asian Cinema & TV](#5-anime-and-asian-cinema--tv)
6. [Calendars](#6-calendars)
7. [Progress Menus (Trakt, MDBList, Simkl)](#7-progress-menus-trakt-mdblist-simkl)
8. [Playing Things — Players](#8-playing-things--players)
9. [Item Context Menu](#9-item-context-menu)
10. [Widgets & Skins](#10-widgets--skins)
11. [Trakt, MDBList and Simkl Integration](#11-trakt-mdblist-and-simkl-integration)
12. [Cross-Service Sync](#12-cross-service-sync)
13. [Kodi Library Integration](#13-kodi-library-integration)
14. [Tools & Maintenance](#14-tools--maintenance)
15. [Settings Reference (Plain Language)](#15-settings-reference-plain-language)
16. [Known Limitations](#16-known-limitations)
17. [Power User Appendix](#17-power-user-appendix)

---

## 1. What Is TMDb Helper?

TMDb Helper is a **browsing and widget add-on for Kodi**. It turns the data on TMDb, Trakt, MDBList, Simkl, TVDb and AniList into movie, TV show, person and list menus, and it can hand any title to a *player* add-on (Seren, Prism, Red Light, Fen Light, Otaku Testing, Umbrella, POV and others) to play it. It does not host or find streams itself.

**Core concept:** Browse a list or calendar → select a title → TMDb Helper asks your chosen player add-on to play it → your watching is recorded to the accounts you connected.

**What this fork adds to the stock add-on:**

- **MDBList and Simkl** next to Trakt: sign-in, watched marks, resume points, scrobbling, context-menu actions and menus.
- **Cross-Service Sync** between Trakt, MDBList and Simkl.
- **Native Anime and Asian Cinema & TV** menus (no Otaku add-on needed).
- **Calendars:** a Seren-style month poster grid for movies and a scrollable one-day show page for TV.
- **Progress menus** for each service, split into everything / movies and TV shows / anime / Asian.
- **Faster, more reliable widgets** (disk cache, background rebuild, instant refresh).
- **Tools menu, Account Info screens, a description under every setting and menu row.**
- **Players pack** hosted in the repository.

**Installing:** see `TMDb_Helper_6.0.205_Install_Instructions.md`. Section 3 below summarises it.

### Requirements

| Requirement | Detail |
|---|---|
| Kodi | 20 (Nexus) or newer (the add-on asks for Python API 3.0.1); tested on Kodi 22 |
| Add-on dependencies | `script.module.jurialmunkey`, `script.module.infotagger` (hosted in the repository), plus `requests`, `pil`, `addon.signals`, `beautifulsoup4` from Kodi's own repository |
| To play titles | A player add-on, and a player file for it (see section 8) |
| Optional | Trakt, MDBList and Simkl accounts; OMDb, fanart.tv and TVDb keys |

---

## 2. What's New Since the v6.0.81 README

The public README stopped at v6.0.81. Everything below arrived in 6.0.82–6.0.205. The complete per-version list is in `changelog.txt`; Kodi also shows it under **Tools → Changelog**.

### Seren parity and sync engine (6.0.82–6.0.121)

- Simkl and MDBList **status-bucket menus** ported from Seren: Watching, On Hold, Dropped, Plan to Watch (Simkl); Dropped Shows, Collection, Liked Lists, User Lists, Next Up (MDBList).
- **Dropped shows** for Trakt and MDBList, hidden from Progress and Next Up.
- **Simkl Next Up, rating and "Set As Watching".** Simkl lists now include anime.
- **MDBList list management:** Add / Remove To List, New List, Manage Lists. Recently Watched Movies now reads from the MDBList server.
- **Reset Resume Position** for MDBList and Simkl.
- **Bridge redesigned** on Seren's snapshot-delta design: shows are never pushed, season 0 is left out, Trakt is refreshed first, a pushed-ledger and per-cycle cap prevent re-pushing, and Trakt sync now reads every page of its endpoints.
- **Account Info** screens for Trakt, MDBList (quota) and Simkl.

### Anime and Asian menus (6.0.121–6.0.143)

- Anime and Asian Cinema & TV became **two hubs**, each with Movies and TV Shows submenus and a scoped search. The Anime menu is native and AniList-backed. See section 5.

### Tools, descriptions and speed (6.0.144–6.0.159)

- **Tools menu** modelled on Seren's: Sync Tools, Clear Cache Tool, Open Settings Menu, Backup and Reset, Changelog.
- A **one-line description under every setting category, setting and menu row.**
- The changelog is also stored in `addon.xml` so Kodi's add-on information screen shows it.
- **Lists open about twice as fast:** the synced Trakt, MDBList and Simkl data is checked once per list instead of once per item (12 test lists: 11.7 s → 6.1 s).

### Widgets and notifications (6.0.149–6.0.157)

- **Background widget rebuild:** the service rebuilds, in the background, the widgets you opened in the last 24 hours (not while a video plays) and prefetches the next page of a list you are reading.
- A **"Nothing here yet"** notice replaces silently returning to the menu when a list is empty.
- Cross-Service Sync shows a progress notification; progress bars are suppressed during playback.
- **Trakt User Lists:** browse any Trakt username's public lists.

### Calendars (6.0.160–6.0.182, 6.0.196–6.0.198, 6.0.202)

- Movie, Asian and Anime calendars; the movie calendar became a Seren-style month poster grid, the TV calendars a scrollable one-day show page. Left / Right now change the day or month even when it is empty. See section 6.
- **The All movie calendars no longer hide films made outside your country** (6.0.197). Trakt's country filter is the country a film was made in, not where it is released, so UK, Canadian or Japanese films releasing in your country were missing. The Asian and Anime calendars keep their own country filters.
- **Selecting a show you do not follow** in a TV calendar now lists the season that is airing there, not always season 1 (6.0.197).
- **Air time and a Today button** (6.0.198): every episode on the TV calendar pages shows its local air time, and the one-day pages have a **Today** button whenever another day is open. Episode times after the next daylight-saving change were one hour off and are now correct.
- **Badges and a watched tick on the TV calendar pages** (6.0.202): an episode that starts a show, opens a season or ends one carries a small badge (**New show**, **Premiere**, **Finale**; also **Mid premiere**, **Mid finale** and **Series finale** where Trakt marks them), and an episode you have already watched gets a green tick and a dimmed row (when watched indicators are switched on for at least one service, see section 3). An episode with no air date shows **No air date** in a show's Unwatched list (it used to show a 1601 date). The calendar pages are kept for 6 hours, so after installing the update the badges appear on pages you have not opened in the last 6 hours. The All TV calendar without a country or genre filter also no longer asks a logged-out user to sign in to Trakt.

### Progress menus (6.0.183–6.0.195)

- **Movies and TV Shows Progress, Anime Progress, Asian Progress** rows in the Trakt, MDBList and Simkl menus, with "All …" names for the unfiltered rows. See section 7.

### Main menu switches (6.0.205)

- **Settings → Navigation → Main menu** has on/off switches for **Show Movies**, **Show TV Shows**, **Show Anime** and **Show Asian Cinema & TV**. Switch one off and its row leaves the add-on's main menu; all four are on by default. The other main-menu rows (People, Search, Discover, the service menus and so on) are not affected.

### Watch services (6.0.203–6.0.204)

- **A watch service that is switched off disappears from view** (6.0.204). With *Enable Trakt*, *Enable MDBList* or *Enable Simkl* off in Settings → Accounts, that service's row leaves the main menu (and the Trakt rows leave the Movies, TV Shows and People menus, which includes the public Trakt lists such as Trending and Popular), its entry leaves Tools → Sync Tools, its settings fold away (the Details context-menu switch, and for Trakt the page multiplier, next-episode sort, genre icons and request log), and its context-menu entry was already hidden. Everything comes back when the service is switched on again.
- **MDBList Watchlist TV shows / Movies** list only their own kind again (6.0.203). MDBList now returns the watchlist in separate movie and show groups, and the list showed both.

### Anime matching and Simkl anime Next Up (6.0.199–6.0.201)

- **A saved AniList-to-TMDb table** (6.0.199). The Anime lists remember which TMDb show or movie each AniList entry is. New matches come from the ARM id service (arm.haglund.dev) and are checked against TMDb before they are used; what ARM cannot settle is found by title search as before, so the lists work as before if ARM cannot be reached. A repeat list build no longer searches again for what is already saved, and a new show is no longer matched to an older series with a similar name (for example the 2026 KERORO instead of the 2014 one). See section 5.
- **Simkl Next Up lists far more anime** (6.0.200). Simkl numbers every anime cour from episode 1 and often has no TMDb id, or a wrong one. The TMDb show now also comes from the saved table, and the episode is placed by the length of the TMDb season or by matching the cour's AniList start date to TMDb's air dates. On the author's account 15 of 16 anime rows were placed (7 before). See section 7.
- **Tools → Clear Cache Tool → Clear Anime and Asian Lists Cache** also clears the saved matches (6.0.199). 6.0.201 makes sure no stray temporary file is left behind when the table cannot be saved. 6.0.202 asks TMDb for a franchise's seasons side by side, so the first Next Up open after the cache is cleared is quicker.

---

## 3. First-Time Setup

The full procedure, with addresses, is in the install instructions. In short:

1. **Install the add-on from the repository.** Settings → File Manager → Add source → `https://minhgi.github.io/tmdbhelper-multiservice/` → install `repository.tmdbhelper-multiservice-1.0.0.zip` from it → Install from repository → TMDb Helper Multiservice → Video add-ons → TMDb Helper.
2. **Install players.** TMDb Helper → Settings → Players → Install players → Install from URL → `https://raw.githubusercontent.com/minhgi/tmdbhelper-multiservice/main/players/tmdbhelper_players.zip`.
3. **Connect accounts** in Settings → Accounts: *Authenticate Trakt account…*, *Authorize MDBList account…*, *Authorize Simkl account…* (each shows an on-screen code or PIN).
4. **Turn on what you want.** *Get watched indicators and episode counts* is **off** for all three services by default, and scrobbling is off for MDBList and Simkl. Switch them on in each service's group.
5. **Optional:** Cross-Service Sync (off by default), API keys (OMDb, fanart.tv, TVDb, TMDb user token).

---

## 4. Navigation & Menus

Open the add-on from **Add-ons → Video add-ons → TMDb Helper**, or put any menu row on your skin's home screen as a widget (section 10).

### Main Menu

| Row | What it holds |
|---|---|
| **Movies** | TMDb movie lists followed by the Trakt movie lists |
| **TV shows** | TMDb TV lists followed by the Trakt TV lists |
| **Anime** | Anime hub: Movies and TV Shows submenus, scoped search |
| **Asian Cinema & TV** | Asian hub: Movies and TV Shows submenus, scoped search |
| **People** | Lists of people (TMDb and Trakt lists applied to people) |
| **Search** | Search movies, TV shows and people |
| **Discover** | Build your own TMDb discover query (genre, year, rating and so on) |
| **Randomised** | Random picks, see below |
| **TheMovieDb** | The TMDb lists for movies and TV shows together |
| **TMDb User** | Your own TMDb recommendations, favourites, rated, watchlist and lists (needs a TMDb user token) |
| **Trakt** | Trakt lists and your Trakt data |
| **TVDb** | TVDb awards and genres (needs a TVDb token) |
| **MDbList** | MDBList lists and your MDBList data |
| **Simkl** | Simkl lists and your Simkl data |
| **Nodes** | Custom nodes, for example lists you saved with *Add/Remove as Custom Node* |
| **Settings** | Opens the add-on settings |
| **Tools** | Sync, cache, backup and changelog tools |

Every row shows a one-line description in Kodi's side panel when highlighted.

### Movies and TV shows menus

Both menus start with the **TMDb rows** and continue with the **Trakt rows**. Which rows appear depends on whether you are in Movies or TV shows.

**TMDb rows:** Popular, Top Rated, Upcoming, Trending Today, Trending This Week, In Theatres (movies), Airing Today / Currently Airing (TV), Next Aired Library, Airing Next Library, Genres, Providers (where to watch), Highest Revenue (movies), Most Voted, **Search** (sits just after Most Voted), and All.

**Trakt rows:** Collection, Favourites, Watchlist (plus Watchlist (Released) and Watchlist (Anticipated)), Your Recently Watched, Dropped Shows, Your Most Watched, Your In-Progress (and its Episodes, Unwatched, **Anime / Asian / Movies and TV Shows Progress** variants), Still to Watch, Your Next Episodes, Recommended For You, Based on Recently Watched, Based on Most Watched, Your Airing This Week, Your Episodes Airing Next, the **calendars** (section 6), Genres, Trending, Popular, Most Played, Most Watched, Anticipated, Top 10 Box Office, Trending Lists, Popular Lists, Liked Lists, Your Lists, Search Lists, **User Lists**.

### Item details submenu

Opening a movie, show, season, episode or person shows a menu of related lists. The rows depend on the item type:

Play · Collection · Seasons / Episodes · Cast · Recommended · Similar · Crew · Posters · Fanart · Keywords · Reviews · Cast in Movies / TV Shows · Crew in Movies / TV Shows · Images · Thumbs · Videos · Episode groups · In Trakt Lists

### Randomised

Random picks generated from: random genre, random keyword, *Based on Recently Watched*, *Based on Most Watched*, random trending, popular, most played, most watched, anticipated, trending lists, popular lists, liked lists and your own lists.

### Trakt menu

The Trakt submenu (shown on its own from the main menu) holds the same Trakt rows listed above, for movies and TV shows together. The personal rows need a Trakt sign-in.

### MDbList menu

| Row | Notes |
|---|---|
| **All In-Progress …** (Movies / TV shows / Combined) | Everything in progress, unfiltered |
| **All Next Up** | Next episode for each show you are watching |
| **Movies and TV Shows Progress** | The same lists with anime and Asian titles left out |
| **Anime Progress** | The same lists, anime only |
| **Asian Progress** | The same lists, Asian only |
| **Watchlist** | Your MDBList watchlist |
| **Your Recently Watched** | Movies from the MDBList server, shows from the MDBList API |
| **Collection** | Your MDBList collection |
| **Dropped Shows** | Shows you dropped (they are hidden from progress) |
| **Your Lists / Liked Lists / Top Lists / Search Lists / User Lists** | MDBList lists |

### Simkl menu

All In-Progress (Movies / TV shows / Combined), All Next Up, Movies and TV Shows Progress, Anime Progress, Asian Progress, Watching, Plan to Watch, Recently Watched, On Hold, Dropped.

### TVDb menu

Awards, and Genres.

### Tools menu

| Row | What it does |
|---|---|
| **Sync Tools** (shown only when Trakt, MDBList or Simkl is signed in) | *Clear last sync time*, *Force a sync*, *Rebuild Database* |
| **Clear Cache Tool** | *Clear Cache*, *Clear Search History*, *Clear Anime and Asian Lists Cache* |
| **Open Settings Menu** | Opens the settings |
| **Backup and Reset** | *Backup Settings*, *Restore Settings* |
| **Changelog** | The version history |

---

## 5. Anime and Asian Cinema & TV

Both hubs have the same shape: **Movies**, **TV Shows** and **Search Movies & TV Shows** (a search limited to that hub's titles).

### Anime

*Anime* means Japanese-language animation. The lists come from AniList and are matched to TMDb, so every row opens a normal TMDb Helper item.

**How the matching works:** each AniList entry is looked up in a table saved on your device (ARM matches are kept up to 90 days, title-search matches 14 days), then in the ARM id service, whose answer is checked against TMDb (the id must exist there and be a Japanese-language title) before it is used, then by title search on TMDb (English, romaji, then the Japanese title). Entries nothing finds, mostly shorts and ONAs that TMDb does not list, are left out and tried again the next time. Several AniList entries of one series (its seasons or cours) show as one row, the series page. In a sample of 193 current-season entries about 94 % were matched.

**Anime TV Shows menu:** Airing Last Season · Airing This Season · Airing Next Season · **Calendar** · Trending · Popular · Top 100 · Genres & Tags · Search

**Anime Movies menu:** Last Season Releases · This Season Releases · Next Season Releases · **Calendar** · Trending · Popular · Top 100 · Genres & Tags · Search

- **Trending:** Trending Last Year, This Year, Last Season, This Season, All Time Trending.
- **Popular:** Popular Last Year, This Year, Last Season, This Season, All Time Popular.
- **Calendar:** the Anime TV Calendar uses Trakt's anime tag and opens the one-day show page. The Anime Movies Calendar covers Japanese anime films and ignores your region setting.

### Asian Cinema & TV

*Asian* means Chinese, Cantonese, Korean, Japanese or Thai language. Movies exclude animation and documentaries; TV shows must carry the Drama genre and exclude animation, reality and talk shows. Five countries are covered: **Chinese, Korean, Japanese, Thai, Taiwanese**.

**Asian Movies menu:** In Theaters · Coming Soon · Calendar · Trending · Most Watched This Week · Most Watched This Month · Popular · Top Rated · By Country · Genres · Search

**Asian Drama (TV) menu:** Airing Now · Airing Next · Calendar · Trending · Most Watched This Week · Most Watched This Month · Popular · Top Rated · By Country · Genres · Search

- **By Country** opens a list for each of the five countries with In Theaters / Airing Now, Calendar, Trending, Most Watched This Week / Month, Popular and Top Rated.
- **Trending** (all countries and per country) ranks recently released titles by the mean of their IMDb, TMDb and Trakt ratings.
- **Most Watched** uses Trakt's lists; Japan reads Trakt's list deeper because it is mostly anime and the live-action titles are kept.
- **Movie genres:** Drama, Romance, Comedy, Action, Thriller, Crime, Mystery, Horror, Fantasy, Sci-Fi, History, War, Family. **Drama genres:** Romance, Historical, Melodrama, Comedy, Crime, Mystery, Action & Adventure, Sci-Fi & Fantasy, Family.

---

## 6. Calendars

### TV calendars — the one-day show page

These open a scrollable window instead of a list: a header row per show with its episodes under it, in the look of your skin's menus (poster and description of the selected row on the left, flat rows on the right).

| Calendar | Window |
|---|---|
| **Your Calendar** | Every show you follow on one page: 14 days back to about 19 days ahead; watched episodes stay in the list, dimmed |
| **Your Calendar Premieres / New** | The same page, premieres only or new shows only |
| **All Calendar** | The page one day at a time for all shows airing that day |
| **All Calendar Premieres / New** | Premieres or new shows, one day at a time |
| **Asian Drama calendars** | All five countries together and one per country, same page |
| **Anime TV Calendar** | Same page, limited to Trakt's anime tag |

- **Left / Right** (or the previous / next day buttons) change the day on a day page, **also when the day has no episodes**.
- Select a **show** for its unwatched episodes (Back returns to the page; for a show you do not follow, the season that is airing is listed); select an **episode** for its information, with Play and the other buttons.
- Each episode shows its **air time** in your local time, in your region's time format.
- On the **one-day pages** (All Calendar, All Premieres / New, the Asian Drama calendars and the Anime TV Calendar) a **Today** button appears at the left whenever a day other than today is open and jumps back to today.
- A small **badge** beside an episode marks a **New show** (series premiere), **Premiere** (season premiere), **Finale** (season finale), and where Trakt gives them **Mid premiere**, **Mid finale** and **Series finale**. Normal episodes have none. Badges come from Trakt's calendar, so they appear on every calendar page but not in a show's Unwatched list.
- A green **tick** and a dimmed row mark an episode you have already watched (on Trakt, MDBList or Simkl). This needs **Get watched indicators and episode counts** switched on for that service. An episode with no air date reads **No air date**.
- Episode labels read `1x02. Title (Show)`; stacked episodes read `1x01-1x03. 3 Episodes (Show)`.
- The Kodi library **Next Aired** calendar (under TheMovieDb) is unchanged.

### Movie calendars — the month poster grid

Each poster shows the **title, release date, a countdown badge** (days until release, or a star once released) and the **rating**.

| Calendar | Shows |
|---|---|
| **Your Calendar** / **Your Calendar DVD** | Movies you follow / physical releases |
| **All Calendar** / **All Calendar DVD** | All movies / physical releases, not limited to your region |
| **Asian Movies Calendar** | All five countries, plus one per country |
| **Anime Movies Calendar** | Japanese anime films |

- **Left / Right at the edge of the grid**, or the previous / next month buttons, change the month. On an **empty month** Left / Right change the month from whichever button has focus.
- **Select** opens the movie's information.
- The *All Calendar* rows show releases whatever country the film was made in; your region setting no longer narrows them.
- A calendar can be put on a skin as a widget path; old saved day-by-day paths still open the list view.

---

## 7. Progress Menus (Trakt, MDBList, Simkl)

Each service has the same family of rows so the same question — *what am I in the middle of?* — has the same answer in all three:

| Row | Lists |
|---|---|
| **All In-Progress …** | Everything, unfiltered (Trakt: *All In-Progress Movies / TV shows / Episodes / Movies (Unwatched) / Episodes (Unwatched)* and *All Next Episodes*) |
| **All Next Up** | Next episode per show |
| **Movies and TV Shows Progress** | The same lists, anime and Asian titles left out |
| **Anime Progress** | The same lists, anime only |
| **Asian Progress** | The same lists, Asian only |

Inside **Anime Progress** and **Asian Progress** the rows keep the plain names (In-Progress Movies, In-Progress TV shows, In-Progress Combined, Next Up; Trakt adds Episodes, the Unwatched lists and Your Next Episodes).

How the split is decided: each title is classified from its TMDb details. **Anime** = Japanese-language animation. **Asian** = the rule in section 5. **Movies and TV Shows** = everything that is neither.

### Simkl Next Up and anime

Simkl writes an anime's next episode as a bare "E12" counted inside its cour (a cour is a Simkl "season 1"), while TMDb files a franchise under one show with its own seasons, and Simkl's own TMDb id for an anime is sometimes missing or wrong. For each anime row TMDb Helper therefore:

1. takes the TMDb show from Simkl, or from the saved AniList table (section 5) when Simkl has none; when the two differ, the one whose TMDb episodes start on the cour's AniList start date is used;
2. uses the one TMDb season whose episode count equals the cour's length, if there is exactly one;
3. otherwise finds the TMDb episode that aired within 10 days of the cour's AniList start date and counts forward from it, accepting the result only if that episode has aired and airs roughly when a weekly show would.

A row that cannot be placed safely, such as a batch release or an irregular show, is left out as before. If AniList cannot be reached, only steps 1 (Simkl's id) and 2 are used. **All Next Up** and **Anime Progress → Next Up** include anime; **Movies and TV Shows Progress → Next Up** leaves them out. An episode you already watched on Trakt or MDBList is hidden by the *Unwatched* view even if Simkl still lists it as next.

---

## 8. Playing Things — Players

A **player** is a small `.json` file describing how to open a movie or episode in another add-on. TMDb Helper reads every file in its players folder. **A player only works if its add-on is installed.**

### Installing players

**Settings → Players → Install players → Install from URL**, then enter `https://raw.githubusercontent.com/minhgi/tmdbhelper-multiservice/main/players/tmdbhelper_players.zip`. Kodi reports how many files were extracted (91 on the test install). Kodi first asks whether to **clear existing players**: Yes deletes the files already in the players folder, No keeps them (same-named files are overwritten either way). The pack includes the open-players set and players for Seren, Prism, Red Light, Otaku Testing and others. **Install from zip file** installs the same pack from a local file; **Update players from URL** repeats the download from the remembered address.

### What happens when you select a title

- *Default select action in addon* (Settings → Navigation) decides whether selecting a movie or episode **plays** it (default) or **browses** its related items.
- With **Default player for movies / episodes** set, that player is used. With none set, you are asked.
- **Play automatically using available providers** (on by default) skips the provider question.
- **Play local Kodi library files:** *Never* offer the local file, *Automatic* (default), or *Manual* (ask).
- **Combine all players for plugin into single entry** (on) lists each add-on's players as one entry; **Use bundled players** (on) also uses the players that ship with TMDb Helper.
- **Configure players** (Settings → Players) opens the player configuration for each installed player.

### Choosing a player for one title

From an item's context menu you can set a **chosen player** for a single movie, show, season or episode; it overrides the default for just that item.

---

## 9. Item Context Menu

Right-click (or long-press) an item to see these TMDb Helper entries. Each can be hidden in **Settings → Other → Context menu**.

| Entry | What it does |
|---|---|
| **Browse lists** | Opens related lists for the item |
| **Trakt options** | Watched / unwatched, watchlist, collection, rating, lists, dropped and more |
| **MDBList options** | Watched / unwatched, watchlist, collection, rating, add or remove from a list, drop show, reset resume position |
| **Simkl options** | Mark watched / unwatched, plan to watch, hold, drop, set as watching, rating |
| **TMDb user options** | Your TMDb favourites, watchlist, rating and lists |
| **Manage artwork** | Choose artwork for the item |
| **Refresh details** | Reload the item's details |
| **Add to library** | Save the item to your Kodi library (section 13) |
| **Add/Remove as Custom Node** | Save a list as a *Nodes* entry |

State-dependent actions show only the option that applies (for example *Mark watched* or *Mark unwatched*, never both), decided from local data without a live API call.

---

## 10. Widgets & Skins

Any menu row can be used as a widget in a skin. TMDb Helper is built to work with skins such as **Titan Bingie Mod** without editing the skin.

- **Instant refresh:** after any Trakt / MDBList / Simkl change (mark watched, rate, scrobble) TMDb Helper sets the reload properties skins already read (`TMDbHelper.Widgets.Reload`, `widgetreload`, `widgetreload2`, `widgetreload-movies`, `widgetreload-tvshows`).
- **Disk widget cache:** a widget that is re-opened (for example after a trailer) is served from disk without refetching.
- **Background rebuild** (Settings → Widgets → *Rebuild widgets in the background*, on by default): widgets you opened in the last 24 hours are rebuilt while idle, so opening one is a cache hit. Right after a restart a widget may briefly show its previous contents and then refresh. A widget that keeps failing is left alone for 30 minutes.
- **Next page / hide watched:** *Next page item* adds a Next Page row to long widget lists; *Hide watched items* hides watched titles.
- **Hub widgets (Titan Bingie Mod):** hub widget paths that contain a `reload=` token are refreshed in place through `Skin.SetString()`, so watched badges update when you return from the context menu.

---

## 11. Trakt, MDBList and Simkl Integration

### What each service gives you

| Feature | Trakt | MDBList | Simkl |
|---|:---:|:---:|:---:|
| Sign-in | on-screen code | on-screen code | PIN at simkl.com/pin |
| Watched marks on items | ✅ | ✅ | ✅ |
| Scrobbling (record what you play) | ✅ | ✅ | ✅ |
| Resume points | ✅ (experimental) | ✅ (experimental) | ✅ (experimental) |
| Watchlist / collection | ✅ | ✅ | plan to watch only |
| Ratings | ✅ | ✅ | ✅ |
| Dropped shows | ✅ | ✅ | ✅ (dropped, on hold) |
| Your lists, liked lists, user lists | ✅ | ✅ | — |
| Next Up | ✅ (Your Next Episodes) | ✅ | ✅ |
| Account Info | ✅ | ✅ (quota) | ✅ |

### Settings to switch on

In **Settings → Accounts**, per service (defaults in the Settings Reference):

- **Enable Trakt / MDBList / Simkl** — master switch for the service.
- **Scrobble items played via TMDbHelper** — playback is recorded.
- **Get watched indicators and episode counts (slower)** — watched marks and counts.
- **Get resume points (experimental) (slower)** — resume bars on items.

Trakt only: **Get episode types (slower)** (premiere / finale labels), **Ignore articles when sorting alphabetically**, **Cache items in custom lists you own for 24 hours**, **Random seed pool for recommendations** (how many recently watched items feed *Because You Watched* lists).

### Service notes

- **MDBList:** watched marks are the local `MDbListSync.db` combined with Trakt. A show's whole-series or whole-season mark only fills in shows and seasons that have no individual episodes.
- **Simkl:** movies paused on another device show a resume bar (read from the Simkl server). Simkl **episode** resume is recorded by TMDb Helper's own scrobbler. Simkl status changes are exclusive: Plan to Watch, On Hold, Dropped and Watching replace one another.
- **Simkl anime:** Simkl's anime records often have no TMDb id, or a wrong one; see "Simkl Next Up and anime" in section 7 for how the id and the episode are found.
- **Dropped shows** are hidden from the Progress and Next Up lists. On Simkl the drop word differs between apps; TMDb Helper handles both names.
- **Logging out** of MDBList or Simkl clears its local sync data.
- **Resetting resume position** (MDBList, Simkl) clears the server resume point as well as the local one.

---

## 12. Cross-Service Sync

Two cooperating mechanisms keep Trakt, MDBList and Simkl in step. **Both are off until you turn on** *Mirror actions across enabled services* (Settings → Accounts → Cross-service sync), and the bridge needs **Trakt plus MDBList or Simkl**.

### On-action mirror

A context-menu action on one service is mirrored silently to every other enabled service. Mirror functions call the other service's API directly and never trigger that service's own sync classes, so actions cannot echo back.

### Bridge sync

A full three-way reconciliation that catches changes made on a website or in another app. It runs about **10 seconds after Kodi starts** and then every **Sync Interval** minutes (default **180**), in a background thread. It follows Seren's snapshot-delta design:

- shows are never pushed, season 0 is left out, Trakt is refreshed first;
- a pushed-ledger and a per-cycle cap avoid re-pushing and large bursts;
- real watch dates are used;
- a notification shows the step it is on and a short message afterwards when items were copied (not shown while a video plays).

| Action | Trakt↔MDBList | Trakt↔Simkl | MDBList↔Simkl |
|---|:---:|:---:|:---:|
| Watched / Unwatched | ✅ | ✅ | ✅ |
| Watchlist | ✅ | ✅ add-only | ✅ add-only |
| Rating | ✅ (on-action only) | — | — |
| Collection | ✅ | — | — |
| Progress (resume) | Trakt→MDBList | Trakt→Simkl | — |

Use **Tools → Sync Tools → Force a sync** to run it now. **Clear last sync time** forgets when each service was last synced but keeps the "data is complete" markers, so the next sync only re-checks. **Rebuild Database** removes all synced data and rebuilds it from scratch.

---

## 13. Kodi Library Integration

Select **Add to library** in an item's context menu to save a movie or show as library files (`.strm` and `.nfo`).

Settings → Library:

- **Movies / TV folders** (default under the add-on's data folder).
- **Update library after adding items** (on): runs a Kodi library update straight away.
- **Auto-update library and add new/missing episodes** (off) with **Auto-update library at time**: a daily update that adds new or missing episodes of shows you added.
- **Auto-update library from Trakt list(s):** items from the Trakt lists you choose are added during the automatic update.
- **Update library now** runs the update immediately.
- **Ignore folder creation checks** and **Use alternative .nfo filename** are compatibility options.

---

## 14. Tools & Maintenance

All under **TMDb Helper → Tools** (rows described in section 4) or in Settings → Expert.

| Task | Where |
|---|---|
| Back up settings, Trakt sync data, awards cache, widget cache and player configs to a zip | Tools → Backup and Reset → **Backup Settings**, or Settings → Expert → *Backup Settings & Data* |
| Restore from a backup (restart Kodi afterwards) | Tools → Backup and Reset → **Restore Settings**, or *Restore Settings & Data* |
| Clear cached TMDb, Trakt, fanart.tv, OMDb and item data | Tools → Clear Cache Tool → **Clear Cache**, or *Delete cache* |
| Clear saved search history | Tools → Clear Cache Tool → **Clear Search History** |
| Clear the saved results of the Anime and Asian lists, and the saved AniList to TMDb matches | Tools → Clear Cache Tool → **Clear Anime and Asian Lists Cache** |
| Rebuild TMDb Helper's copy of your Kodi library details | Settings → Other → **Refresh Cached Kodi Library details** |
| Read the version history | Tools → **Changelog** |
| Debug a problem | Settings → Expert → **Debug logging**, **Log timer reports for directories**, **Log output of TMDb / Trakt request** |

---

## 15. Settings Reference (Plain Language)

All settings are visible at the **Standard** level. Defaults below are the shipped defaults.

### General

| Setting | Default | What it does |
|---|---|---|
| MPAA rating prefix | `Rated` | Text added before the rating (PG → "Rated PG"); cached items need a refresh |
| Language and country | English (US) | Language and country used for TMDb and Trakt lookups |
| Ignore region release filter | off | Lists are not limited to releases in your country |
| Get additional details from the local Kodi library database | off | Adds watched state and local files for items also in your library |
| Get additional details and artwork from TMDb | off | Extra details and artwork; off is faster but shows less |
| TMDb artwork quality | Original | Size of artwork downloaded from TMDb (Original, High, Medium, Low) |

### Navigation

| Setting | Default | What it does |
|---|---|---|
| Default select action in addon | Play | Play, or Browse (open related items) |
| Hide unaired movies / episodes | off | Hide titles not yet released or aired |
| Hide items without airdates | on | Treat undated items as unaired |
| Special season folders | Show All | Show all, or hide Specials, Up Next and / or Episode Groups |
| Flatten seasons | off | List all seasons' episodes together |

### Widgets

| Setting | Default | What it does |
|---|---|---|
| Next page item | off | Next Page row on long widget lists |
| Hide watched items | off | Hide watched titles in widgets |
| Rebuild widgets in the background | on | Background rebuild of widgets used in the last 24 hours |
| Flatten tvshows with multiple episodes airing same day | on | Show them as one item |
| Next aired library calendar links to local tvshow | on | Opens the show in your library |
| Preferred artwork for thumbs | thumbs | Thumbs, fanart or landscape for calendar and Next Episodes thumbnails |
| Next episodes sort method | air date | Air date, recently watched, today's or last week's |
| Next episodes checks calendar | off | More accurate, slower |

### Library

See section 13. Defaults: update after adding **on**, daily auto-update **off**.

### API Keys

OMDb API key, Fanart.tv client key (with *Use fanart.tv for plugin / widget / service monitor artwork*, all off; *Prefer artwork from TMDb first* off; *Fallback to English* on), TVDb token, TMDb user token. Each has **Allow 3rd party plugin access to API key/token** (off by default), which lets a skin or player reuse the key instead of asking you again.

### Accounts

| Group | Settings |
|---|---|
| **Trakt API** | Enable Trakt (on) · Authenticate · Account Info · Logout · Scrobble (**on**) · Get episode types (on) · Get watched indicators (**off**) · Get resume points (**off**) · Random seed pool (5) · Ignore articles (off) · Cache own lists 24 h (on) |
| **MDbList API** | Enable MDBList (on) · Authorize · Account Info · Logout · Scrobble (**off**) · Get watched indicators (**off**) · Get resume points (**off**) · MDbList API key |
| **Simkl API** | Enable Simkl (on) · Authorize · Account Info · Logout · Scrobble (**off**) · Get watched indicators (**off**) · Get resume points (**off**) |
| **Cross-service sync** | Mirror actions across enabled services (**off**) · Sync Interval (Minutes) (**180**) |

### Players

Default player for movies / episodes (none) · Play automatically using available providers (on) · Play local Kodi library files (Automatic; options Never / Automatic / Manual) · Combine all players for plugin into single entry (on) · Use bundled players (on) · Install players · Update players from URL · Configure players.

### Other

| Group | Settings |
|---|---|
| **Details** | Configure provider data allowlist · Container content fallback for service listitems without mediatypes (off) · Readahead caching of service listitems (off) · Refresh cached Kodi Library details |
| **Page multipliers** (higher is slower) | Library windows (2) · TMDb basic lists (1) · Trakt basic lists (1) · Trakt sync lists (1) |
| **Context menu** | Show or hide each entry from section 9 (all on) |
| **Artwork** | Genre icons folder (TMDb) · Genre icon folder (Trakt) · Provider icons folder (TMDb) |
| **Notifications** | Authorisation and sync notifications on startup (on) · Trakt / MDBList / Simkl sync progress notifications (on) · Connection error notifications (on) |
| **Anime** | Anime episode enrichment (Seren) (on) — overlays anime episode titles, plots and thumbnails from Seren's anime cache; needs Seren with anime features |

### Expert

| Group | Settings |
|---|---|
| **Dummy ResolvedURL** | Dummy video duration (1.0 s) · Delay after resolving (1.0 s) · Stop before attempting playback (on) · Restrict to library strm (off) |
| **Player** | Reroute PlayMedia to xbmc.Player() (on) · Recache Kodi Library details when building players (off) |
| **Other** | Use alternative TMDb API url (off; uses api.tmdb.org) · Max concurrent threads (0 = unlimited) · Cache database location · Cache image location · Delete cache · Backup Settings & Data · Restore Settings & Data |
| **Debug** | Log timer reports for directories (off) · Debug logging (off) · Log output of TMDb / Trakt request |

---

## 16. Known Limitations

| Limitation | Detail |
|---|---|
| **Bridge removes** | Additive only. Unwatching directly on trakt.tv, mdblist.com or simkl.com is not propagated. Context-menu unwatches ARE mirrored. |
| **Progress sync direction** | Trakt → MDBList / Simkl only (Trakt's `/sync/playback` is read-only). MDBList and Simkl **movie** progress is read from the server; Simkl **episode** progress is recorded locally by the scrobbler. |
| **Ratings** | No MDBList endpoint to read ratings back; they sync only through the on-action mirror. |
| **Simkl episode numbering** | TMDb episode numbers are used. Other shows whose TVDB and TMDb numbering differ can land on the wrong episode. Anime cours are placed by season length or by start date (section 7); batch releases and irregular anime stay out of Next Up. |
| **Anime matching** | Titles TMDb does not list (mostly shorts and ONAs) do not appear in the Anime lists, and a rare entry can still be matched to the wrong show. Clear Anime and Asian Lists Cache resets the saved matches. |
| **Static hub widget paths** | Hub slot refresh needs a `reload=` token in the path; fully static paths update on the next navigation. |
| **Anime / Asian scoping** | Decided client-side from TMDb language and genres; an unusually tagged title can land in the wrong menu. |
| **Anime calendars** | The TV calendar is limited to titles Trakt tags as anime; the movie calendar is Japan only and ignores the region setting. |
| **Players need their add-ons** | A listed player does nothing if its add-on is not installed. |

---

## 17. Power User Appendix

### Where things live

| Item | Location |
|---|---|
| Add-on data | `special://profile/addon_data/plugin.video.themoviedb.helper/` |
| Players | `…/addon_data/plugin.video.themoviedb.helper/players/` (install extracts the pack's `.json` files here) |
| Default library folders | `…/addon_data/plugin.video.themoviedb.helper/movies/` and `…/tvshows/` |
| Sync databases | `MDbListSync.db`, `SimklSync.db` (local sync data), plus the Trakt sync data |
| Repository source | `https://github.com/minhgi/tmdbhelper-multiservice` (add-on source in `repo/plugin.video.themoviedb.helper/`, generated zips in `repo/zips/`) |

### Plugin routes (for widgets and skin shortcuts)

Menu rows are plugin paths of the form `plugin://plugin.video.themoviedb.helper/?info=<route>&…`. Useful routes: `dir_movie`, `dir_tv`, `dir_anime_hub`, `dir_asian_hub`, `dir_trakt`, `dir_mdblist`, `dir_simkl`, `dir_mdblist_progress` / `dir_simkl_progress` / `dir_trakt_progress` (with `progress_filter=anime|asian|other`), `dir_calendar_window` (movie month grid), `dir_calendar_shows_window` (TV show page), `scoped_search` (with `search_scope=anime|asian`).

Calendar windows are opened with `RunPlugin(...)`, not as a folder, because the plugin opens its own window. Parameters such as `countries=cn,kr,jp,th,tw` must be written with the commas encoded as `%2C` in a plugin URL, because Kodi splits the URL parameter string on literal commas:

`RunPlugin("plugin://plugin.video.themoviedb.helper/?info=dir_calendar_window&user=false&genres=anime&countries=jp&window_title=Anime%20Movies%20Calendar")`

### Dependency note

If a Kodi build resolves TMDb Helper's modules from another repository, the newest version found wins. `script.module.jurialmunkey` 0.2.35 and `script.module.infotagger` 0.0.8 are the versions this repository carries; the add-on asks for 0.2.28 and 0.0.5 or newer.

### Reporting a problem

Turn on **Debug logging** (Settings → Expert), repeat the problem, then attach `kodi.log`. **Log output of TMDb request** and **Log output of Trakt request** write the raw response to the log for a chosen item.

---

*Source-verified against the 6.0.205 add-on source (`basedir.py`, `settings.xml`, `strings.po`, `router.py`, `players.py`, `downloader.py`, `bridge_sync.py`, `cronjob.py`, `context.py`, `changelog.txt`). Live-confirmed: the players install through Install players → Install from URL; the Left / Right keys page empty days and months in the calendar windows; the Anime and Asian calendar windows open from the shortcut links above; the calendar air time, Today button, badges and watched tick; the anime list matching (first build and saved-table build) and Simkl Next Up for anime (All Next Up, Anime Progress and Movies and TV Shows Progress) on one account. The rest is source-verified only and was not each exercised on a live account.*
