# TMDb Helper 7.0.6 — Install Instructions

Covers four pieces: the **TMDb Helper** add-on (this fork), its two required script modules **script.module.jurialmunkey** and **script.module.infotagger**, the **players**, and connecting your **Trakt / MDBList / Simkl** accounts.

There are three separate steps, for three kinds of thing:

- **Part 1** — the add-on and its two modules. These are real Kodi add-ons. Install them from the self-hosted repository (recommended, Kodi keeps them updated) or from zip files.
- **Part 2** — the players. These are not add-ons; they are small `.json` files TMDb Helper reads. They install through **TMDb Helper's own Settings → Players** screen.
- **Part 3** — connecting accounts and turning on the features you want. Most sync features are off by default (only Trakt scrobbling is on).

Do Part 1 first, then Part 2, then Part 3.

> **This is a personal fork, not in the official Kodi repository.** It uses the same add-on ID as the stock TMDb Helper (`plugin.video.themoviedb.helper`), so it replaces the stock one. The version numbers are above the other builds' (7.0.6 here; 5.4.16 in Kodi's official repository when checked on 2026-10-17, 6.17.9 in the signde repository and 6.18.0 in the upstream source), so Kodi will not offer to "update" you back to any of them.

---

## Requirements

- **Kodi 20 (Nexus) or newer** (the add-on asks for Kodi's Python API 3.0.1, which belongs to Kodi 20 and later; Kodi 19 is not enough). It is developed and live-tested on **Kodi 22**, portable install.
- **An internet connection.** Lists come from TMDb, Trakt, MDBList, Simkl, TVDb and AniList.
- **A player add-on** if you want to *play* things (for example Seren, Prism, Red Light, Fen Light, Otaku Testing, Umbrella, POV). TMDb Helper finds and lists titles; the player add-on does the playing. Browsing, calendars, progress lists and syncing work without one.
- **Optional accounts:** Trakt, MDBList and Simkl (free accounts work for sign-in), plus optional API keys for OMDb, fanart.tv and TVDb.

---

## Part 1 — TMDb Helper and its two modules

### Option A — Install from the repository (recommended)

The repository serves TMDb Helper 7.0.6 **and** the two script modules it needs, which are not in Kodi's official repository.

1. **Settings → File Manager → Add source**. Enter this address and name it anything (for example "TMDb Helper Multiservice"), then select **OK**:

   `https://minhgi.github.io/tmdbhelper-multiservice/`

2. **Add-ons → Install from zip file** → choose the source you just added → select `repository.tmdbhelper-multiservice-1.0.0.zip`.
3. **Add-ons → Install from repository → TMDb Helper Multiservice → Video add-ons → TMDb Helper** → **Install**. Kodi installs `script.module.jurialmunkey` and `script.module.infotagger` for you as required add-ons, plus the modules that Kodi's own repository supplies (`script.module.requests`, `script.module.pil`, `script.module.addon.signals`, `script.module.beautifulsoup4`). Accept every prompt.

If the source cannot be browsed, use **Install from zip file → Web Location…** (if your Kodi build offers it) with this exact address:

`https://minhgi.github.io/tmdbhelper-multiservice/repository.tmdbhelper-multiservice-1.0.0.zip`

Once the repository add-on is installed, Kodi checks it for new TMDb Helper versions on its own.

### Option B — Install from zip files

Use this if you cannot reach GitHub Pages, or want a specific version. Install the **two modules first**, then the add-on, each through **Add-ons → Install from zip file**:

| Order | Zip | Address |
|---|---|---|
| 1 | `script.module.jurialmunkey-0.2.35.zip` | `https://raw.githubusercontent.com/minhgi/tmdbhelper-multiservice/main/repo/zips/script.module.jurialmunkey/script.module.jurialmunkey-0.2.35.zip` |
| 2 | `script.module.infotagger-0.0.8.zip` | `https://raw.githubusercontent.com/minhgi/tmdbhelper-multiservice/main/repo/zips/script.module.infotagger/script.module.infotagger-0.0.8.zip` |
| 3 | `plugin.video.themoviedb.helper-7.0.6.zip` | `https://raw.githubusercontent.com/minhgi/tmdbhelper-multiservice/main/repo/zips/plugin.video.themoviedb.helper/plugin.video.themoviedb.helper-7.0.6.zip` |

Download each file (or paste the address where your Kodi build accepts a web location) and select it. The add-on will still need the four modules that Kodi's official repository supplies; Kodi fetches those itself when it can. Installing from the repository (Option A) avoids chasing these by hand.

### Replacing an existing TMDb Helper

Installing over the stock TMDb Helper is an upgrade, and your settings are kept. After the install, restart Kodi once so the new service starts cleanly.

### Other repositories that carry TMDb Helper

Some skins point you to another repository with its own TMDb Helper (Arctic Fuse 3 uses the signde repository, version 6.17.9). Kodi installs the highest version it can see, and this fork is numbered 7.0.6, above those, so it is not replaced and automatic updates can stay on. Arctic Fuse 3 3.4.0 asks for TMDb Helper 6.17.9 or newer, which 7.x satisfies. Do not install the other build over this one: it is an older version and has none of this fork's features.

---

## Part 2 — Players

Players tell TMDb Helper how to open a movie or episode in another add-on. The pack in this repository holds the open-players set plus players for Seren, Prism, Red Light, Otaku Testing and others. A player only works if its add-on is installed.

1. **Add-ons → My add-ons → Video add-ons → TMDb Helper → Configure**.
2. Open the **Players** category.
3. Select **Install players → Install from URL** (the row below it, **Update players from URL**, does the same thing).
4. Enter this address and confirm the download prompt:

   `https://raw.githubusercontent.com/minhgi/tmdbhelper-multiservice/main/players/tmdbhelper_players.zip`

5. Kodi asks **Would you like to clear existing players first?** — **Yes** deletes the player files already in TMDb Helper's players folder before installing; **No** keeps them (files with the same name are still overwritten).
6. Kodi shows **Success!** and the number of files extracted (91 on the test install).

TMDb Helper remembers the address, so a later update only needs a confirm.

> **Choose No at the "clear existing players" question if you wrote or edited player files by hand.** Files in the pack with the same name as one of yours are overwritten either way, so keep a copy first (**Tools → Backup and Reset → Backup Settings** includes the player configs).

**From a file instead:** download `tmdbhelper_players.zip` from the `players/` folder of `https://github.com/minhgi/tmdbhelper-multiservice`, then **Install players → Install from zip file** and pick it.

**Set a default (optional):** in the same Players category, **Default player for movies** and **Default player for episodes** choose the player used when you select an item. With none set, TMDb Helper asks each time. **Play automatically using available providers** (on by default) skips the provider question.

---

## Part 3 — Connect accounts and turn features on

Open **Settings → Accounts**. Each service signs in directly from TMDb Helper using an on-screen code; no separate login add-on is needed.

### Step 1 — Trakt (recommended)

1. **Authenticate Trakt account…** — TMDb Helper shows a code and a web address. Open the address on a phone or computer, sign in to Trakt and enter the code.
2. Back in Kodi, the screen closes when Trakt accepts it.
3. Use **Account Info** to check the account.

### Step 2 — MDBList (optional)

1. **Authorize MDBList account…** — follow the on-screen device code the same way.
2. MDBList *lists* also use your API key: enter it in **MDbList API key**.
3. **Account Info** shows the account and your quota.

### Step 3 — Simkl (optional)

1. **Authorize Simkl account…** — TMDb Helper shows a PIN. Visit **simkl.com/pin**, enter the code and wait for the dialog to finish.
2. **Account Info** confirms the login.

### Step 4 — Turn on what you want from each service

These are **off by default**, so a fresh install shows no watched marks and sends nothing back until you enable them. In each service's group:

| Setting (in that service's group) | Trakt default | MDBList default | Simkl default | What it does |
|---|:---:|:---:|:---:|---|
| **Scrobble items played via TMDbHelper** | on | **off** | **off** | Tells the service what you watch when you play through TMDb Helper |
| **Get watched indicators and episode counts (slower)** | **off** | **off** | **off** | Shows watched marks and episode counts on items |
| **Get resume points (experimental) (slower)** | **off** | **off** | **off** | Shows resume points from that service |

Turn on at least **Get watched indicators and episode counts** for every service you use. Turn on **Scrobble** for MDBList / Simkl if you want playback recorded there too.

### Step 5 — Cross-Service Sync (optional)

In **Settings → Accounts → Cross-service sync**:

- **Mirror actions across enabled services** (off by default): marking watched, watchlist, rating or collection on one service is mirrored to the other enabled services. It needs Trakt plus MDBList or Simkl, and only applies to actions taken after you turn it on.
- **Sync Interval (Minutes)** (default 180): how often the full three-way reconciliation runs. A first pass runs about 10 seconds after Kodi starts.

### Step 6 — Optional API keys (Settings → API Keys)

- **TMDb** works out of the box. A **TMDb user token** is only needed for the *TMDb User* menu (your own lists, ratings, watchlist).
- **OMDb API key** adds ratings from IMDb and Rotten Tomatoes.
- **Fanart.tv client key** enables fanart.tv artwork (slower; the *Use fanart.tv for …* switches choose where).
- **TVDb token** is used by the *TVDb* menu.

---

## Quick Reference — Menu Paths

| Goal | Path |
|---|---|
| Add the repository source | Settings → File Manager → Add source → `https://minhgi.github.io/tmdbhelper-multiservice/` |
| Install the repository add-on | Add-ons → Install from zip file → *your source* → `repository.tmdbhelper-multiservice-1.0.0.zip` |
| Install TMDb Helper (and its modules) | Add-ons → Install from repository → TMDb Helper Multiservice → Video add-ons → TMDb Helper |
| Install players | TMDb Helper → Settings → Players → Install players → Install from URL |
| Set default players | TMDb Helper → Settings → Players → Default player for movies / episodes |
| Sign in to Trakt / MDBList / Simkl | TMDb Helper → Settings → Accounts → Authenticate / Authorize … |
| Turn on watched marks | TMDb Helper → Settings → Accounts → *service* → Get watched indicators and episode counts |
| Cross-Service Sync | TMDb Helper → Settings → Accounts → Cross-service sync |
| Back up settings | TMDb Helper → Tools → Backup and Reset → Backup Settings (or Settings → Expert → Backup Settings & Data) |
| Force a sync | TMDb Helper → Tools → Sync Tools → Force a sync (shown only when Trakt, MDBList or Simkl is signed in) |
| Read the changelog | TMDb Helper → Tools → Changelog |

---

## Troubleshooting

| Symptom | What to do |
|---|---|
| **Add source** says "Unable to connect" | The address needs the trailing slash and no typos. If it still fails, use **Install from zip file → Web Location…** with the zip address, or Option B. |
| Install fails on a missing add-on | Install `script.module.jurialmunkey` and `script.module.infotagger` first (Option B, rows 1–2), then TMDb Helper. |
| Lists show no watched marks | Turn on **Get watched indicators and episode counts** for each service (Part 3, Step 4). |
| Nothing syncs between services | Turn on **Mirror actions across enabled services** and sign in to Trakt plus MDBList or Simkl (Part 3, Step 5). |
| A player is missing from the list | Install the player's add-on; install or update the pack (Part 2); check **Configure players**. |
| Need to report a problem | Settings → Expert → turn on **Debug logging**, repeat the problem, then use **Log output of TMDb request** / **Log output of Trakt request** and attach `kodi.log`. |

---

*Source-verified against the 6.0.215 add-on source (`settings.xml`, `strings.po`, `router.py`, `basedir.py`, `bridge_sync.py`, `cronjob.py`, `players.py`, `downloader.py`) and the repository files. Confirmed live: the players pack installs through **Install players → Install from URL** (Kodi reported 91 files extracted), and the repository source address and every zip address above returned HTTP 200 when checked on 2026-10-17 (for 6.0.196; the 7.0.6 zip address works once the repository is pushed and was not checked). Not independently re-tested on a clean Kodi: that Kodi fetches `script.module.jurialmunkey` and `script.module.infotagger` automatically during the repository install (the add-on requires them and the repository carries them).*
