#!/usr/bin/env python3
"""
Scrapes stats.swehockey.se (Svenska Ishockeyförbundets officiella statistik) server-side
for SHL and HockeyAllsvenskan: standings, schedule/results, scoring leaders and leading
goalies. Runs in GitHub Actions (no browser CORS restrictions apply here) and writes a
small JSON file that SHL-briefen's frontend reads same-origin — same pattern as TEFA's
FPL sync and Fotbollsbriefen's matches.py.

Why scraping and not a clean API: SHL's real "Open API" requires a clientId/clientSecret
issued by emailing support@shl.se, so it isn't usable for an unattended public sync.
stats.swehockey.se (the Swedish Ice Hockey Association's own stats site, which also
covers HockeyAllsvenskan) is public and has no login wall, but only serves HTML pages —
so this parses the existing tables directly. Parsing looks for header text ("RK"/"Team"
for standings, "Date"/"Game" for schedule) rather than CSS classes, since the visible
labels are less likely to change than internal markup.

Season fallback: early in the season (or in the off-season) the "current" season's
standings table exists but every team shows 0 games played. In that case we walk back
through the season <select> on the page and re-scrape the most recent season that has
at least one played game, so the site never shows an all-zero table when a real, recently
finished season's data is available. This mirrors TEFA's ESPN previous-season fallback.
"""
import json
import re
import sys
import time
from datetime import datetime, timezone
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError

from bs4 import BeautifulSoup

BASE = "https://stats.swehockey.se"
UA = "SHL-briefen-sync/1.0 (+https://github.com/timsor68/SHL-briefen)"
OUT_FILE = "shl-briefen-data.json"

LEAGUES = {
    "shl": {"label": "SHL", "id": 20961},
    "allsvenskan": {"label": "HockeyAllsvenskan", "id": 20962},
}


def fetch(url, retries=3):
    last_err = None
    for attempt in range(retries):
        try:
            req = Request(url, headers={"User-Agent": UA})
            with urlopen(req, timeout=30) as resp:
                return resp.read().decode("utf-8", errors="replace")
        except (URLError, HTTPError) as e:
            last_err = e
            time.sleep(2)
    raise RuntimeError(f"Failed to fetch {url}: {last_err}")


def soupify(html):
    return BeautifulSoup(html, "html.parser")


def header_text_of(table):
    header_cells = table.find_all("th")
    if not header_cells:
        first_row = table.find("tr")
        header_cells = first_row.find_all(["td", "th"]) if first_row else []
    return " | ".join(c.get_text(strip=True) for c in header_cells).lower()


def find_table_by_headers(soup, required_words):
    """Find the <table> whose header row contains all of required_words
    (case-insensitive substring match). Returns the table tag or None."""
    for table in soup.find_all("table"):
        ht = header_text_of(table)
        if all(w.lower() in ht for w in required_words):
            return table
    return None


def find_all_tables_by_headers(soup, required_words):
    out = []
    for table in soup.find_all("table"):
        ht = header_text_of(table)
        if all(w.lower() in ht for w in required_words):
            out.append(table)
    return out


def cell_texts(tr):
    return [c.get_text(" ", strip=True) for c in tr.find_all(["td", "th"])]


def parse_standings(soup):
    table = find_table_by_headers(soup, ["rk", "team", "gp"])
    if not table:
        return []
    rows = []
    for tr in table.find_all("tr"):
        cells = cell_texts(tr)
        if not cells or cells[0].lower() == "rk":
            continue
        if all(c in ("", "-", "—") for c in cells):
            continue
        if len(cells) < 9:
            continue
        rank_digits = re.sub(r"\D", "", cells[0])
        if not rank_digits:
            continue
        team = cells[1].strip()
        if not team or team.lower() == "team":
            continue
        try:
            gp, w, t, l = int(cells[2]), int(cells[3]), int(cells[4]), int(cells[5])
        except ValueError:
            continue
        gf, ga = 0, 0
        m = re.match(r"(\d+):(\d+)", cells[6])
        if m:
            gf, ga = int(m.group(1)), int(m.group(2))
        try:
            pts = int(cells[8])
        except (ValueError, IndexError):
            pts = 0
        # Overtime/shootout win-loss and game-winning-shot columns (present on the real
        # site as extra columns after points: OTW, OTL, GWSW, GWSL) — read defensively
        # since older cached pages or narrower layouts may not have them.
        def _int_cell(idx):
            try:
                return int(re.sub(r"\D", "", cells[idx]) or 0)
            except (ValueError, IndexError):
                return 0
        otw = _int_cell(9)
        otl = _int_cell(10)
        gwsw = _int_cell(11)
        gwsl = _int_cell(12)
        rows.append({
            "rank": int(rank_digits), "team": team, "gp": gp, "w": w, "t": t, "l": l,
            "gf": gf, "ga": ga, "pts": pts,
            "otw": otw, "otl": otl, "gwsw": gwsw, "gwsl": gwsl,
        })
    return rows


# Matches a cell whose *entire* text is "Home Team - Away Team". Anchored (^...$) and
# requires at least one letter on each side so it can't accidentally match a bare score
# cell like "10 - 12" (some periods/overtime scores are 2+ digits too, so digit-count
# alone isn't a safe discriminator — letters are, since every team name has some).
#
# IMPORTANT: the real stats.swehockey.se markup does NOT render exactly one space on
# each side of the dash — BeautifulSoup's get_text(" ", strip=True) ends up producing
# "Frölunda HC  -   Växjö Lakers HC" (two spaces before the dash, three after), most
# likely from extra/empty inline nodes around the separator in the source HTML. An
# earlier version of this regex required exactly one space (\s) and silently failed to
# match on the live site, causing the fallback path below to grab the wrong cell (the
# date/time column) instead — the "kommande matcher visar datum men inte lag" bug.
# \s+ (one or more) is required here, not \s, to match the real page.
TEAM_VS_RE = re.compile(r"^(.{2,45}?)\s+-\s+(.{2,45})$")


def _has_letter(s):
    return any(ch.isalpha() for ch in s)


def parse_games_table(table):
    """Parses one Schedule or Results table.

    stats.swehockey.se's schedule/results tables sometimes carry extra hidden/empty
    columns (varies by league and by season), so the "Team - Team" cell doesn't always
    sit at a fixed column index. Instead of assuming a column position, we scan every
    cell in the row and pick the one that actually matches the "Lag - Lag" pattern
    (with a letter on both sides, to rule out score cells like "3 - 2"). The result
    score and venue are then read relative to *that* cell's position, not a hardcoded
    index, so the parser keeps working even if swehockey adds/removes columns.
    """
    ht = header_text_of(table)
    if "date" not in ht or "game" not in ht:
        return []
    has_result_col = "result" in ht
    games = []
    # stats.swehockey.se only prints a Date cell on the *first* row of each day's game
    # group (a rowspan in the real markup) — continuation rows for the same day have
    # something else entirely in cell[0] (often a leaked time value). Without tracking
    # the last real date seen, every game except the day's first silently disappears,
    # which is exactly the "only one match per date shown" symptom that was reported.
    last_date = None
    for tr in table.find_all("tr"):
        texts = cell_texts(tr)
        if not texts or texts[0].lower() == "date":
            continue
        if all(not c for c in texts):
            continue

        date_match = re.match(r"\d{4}-\d{2}-\d{2}", texts[0])

        game_idx = None
        home, away = "", ""
        for i, t in enumerate(texts[1:], start=1):
            m = TEAM_VS_RE.match(t)
            if m and _has_letter(m.group(1)) and _has_letter(m.group(2)):
                home, away = m.group(1).strip(), m.group(2).strip()
                game_idx = i
                break

        if date_match:
            date_raw = texts[0]
            last_date = date_raw
        elif last_date is not None and game_idx is not None:
            # No date in this row, but we've seen one earlier in the table and this
            # row does contain a genuine "Lag - Lag" match — safe to attribute it to
            # the day's most recently seen date.
            date_raw = last_date
        else:
            # No date of its own, no established date yet, or no recognizable game
            # pattern at all (e.g. an ad/separator row) — can't safely place this row.
            continue

        if game_idx is None:
            # Row has its own date but nothing matched the "Lag - Lag" pattern
            # (unexpected row shape) — fall back to the old column-1 assumption
            # rather than dropping the row.
            game_text = texts[1] if len(texts) > 1 else ""
            parts = re.split(r"\s+-\s+", game_text)
            home = parts[0].strip() if len(parts) > 0 else ""
            away = parts[1].strip() if len(parts) > 1 else game_text
            game_idx = 1

        entry = {"date": date_raw, "home": home, "away": away, "played": False}

        # Result: scan cells after the game cell for a "N - N" score (not anchored to
        # a fixed offset, since the number of columns between game and result varies).
        result_idx = None
        if has_result_col:
            for i in range(game_idx + 1, len(texts)):
                sm = re.match(r"^(\d+)\s*-\s*(\d+)$", texts[i])
                if sm:
                    entry["homeScore"] = int(sm.group(1))
                    entry["awayScore"] = int(sm.group(2))
                    entry["played"] = True
                    result_idx = i
                    break

        # Venue/periods: whatever non-empty text remains after the game (and result,
        # if any) cell. The periods cell (e.g. "(20-15-10)") is distinguished from the
        # venue by containing a digit; venue text normally doesn't.
        start = (result_idx if result_idx is not None else game_idx) + 1
        remaining = [c for c in texts[start:] if c and c not in ("-", "—")]
        if remaining:
            if entry["played"] and re.search(r"\d", remaining[0]) and len(remaining) > 1:
                entry["periods"] = remaining[0].strip("() ")
                entry["venue"] = remaining[-1]
            else:
                entry["venue"] = remaining[-1]
        else:
            entry["venue"] = ""

        games.append(entry)
    return games


def parse_schedule_results(soup):
    games = []
    for table in find_all_tables_by_headers(soup, ["date", "game"]):
        games.extend(parse_games_table(table))
    # de-dupe (Overview page can list the same game in both a "Results" and general table)
    seen = set()
    unique = []
    for g in games:
        key = (g["date"], g["home"], g["away"])
        if key in seen:
            continue
        seen.add(key)
        unique.append(g)
    return unique


def parse_player_stat_table_from_table(table, limit=60):
    """Same row/column extraction as parse_player_stat_table(), but takes an
    already-located <table> tag directly — used when a page has more than one
    matching table (e.g. Powerplay + Penalty Killing on one page) and the caller
    has already picked the right one out of find_all_tables_by_headers()."""
    header_cells = table.find_all("th")
    if not header_cells:
        first_row = table.find("tr")
        header_cells = first_row.find_all(["td", "th"]) if first_row else []
    headers = [c.get_text(strip=True) for c in header_cells]
    rows = []
    for tr in table.find_all("tr"):
        cells = cell_texts(tr)
        if not cells or cells == headers:
            continue
        if cells[0].lower() in ("rk", "#", ""):
            if not re.match(r"^\d", cells[0]):
                continue
        row = {}
        for i, h in enumerate(headers):
            if i < len(cells):
                row[h or f"col{i}"] = cells[i]
        if row:
            rows.append(row)
        if len(rows) >= limit:
            break
    return rows


def parse_player_stat_table(soup, required_words, limit=60):
    table = find_table_by_headers(soup, required_words)
    if not table:
        return []
    return parse_player_stat_table_from_table(table, limit=limit)


# Extra player leaderboard pages beyond Scoring/Goalies (SVS%). All follow the same
# "Rk | No | Name | Team | Pos | GP | ..." template as ScoringLeaders, so the generic
# parse_player_stat_table() works unchanged — only the URL slug and JSON key differ.
# required_words=["team", "gp"] (rather than the ["player"] used for the original two
# calls) because those two words are the ones directly confirmed present in every one
# of these pages' visible column headers.
EXTRA_SKATER_STAT_PAGES = {
    "goals": "GoalScoringLeaders",
    "assists": "AssistLeaders",
    "plusMinus": "PlusMinusLeaders",
    "faceoffs": "FaceOffLeaders",
    "powerplay": "PowerplayLeaders",
    "shorthanded": "ShorthandedLeaders",
    "defensemen": "DefensemenLeaders",
    "penalties": "MostPenPlayers",
}

# Team-level stat pages with a single, flat (non-nested) header row that the generic
# parser can handle correctly. Attendance and (team) Faceoffs pages use two-row nested
# headers (Home/Away/Total groups) that parse_player_stat_table can't align correctly,
# so they're intentionally left out for now rather than shipping garbled columns.
TEAM_FAIR_PLAY_URL = "FairPlay"
TEAM_PP_PK_URL = "PowerplayAndPenaltyKilling"


def resolve_season(league_id):
    """Fetch the Overview page for league_id. If the standings show 0 GP everywhere
    (pre-season / off-season), walk back through the season <select> options and
    re-fetch until a season with at least one played game is found. Returns
    (season_id, season_label, overview_soup)."""
    url = f"{BASE}/ScheduleAndResults/Overview/{league_id}"
    html = fetch(url)
    soup = soupify(html)
    standings = parse_standings(soup)
    any_played = any(r["gp"] > 0 for r in standings)

    season_label = None
    select = soup.find("select", id=re.compile("season", re.I))
    if not select:
        # fall back to any <select> whose options look like "20XX-YY"
        for s in soup.find_all("select"):
            opts = s.find_all("option")
            if opts and re.match(r"^\d{4}-\d{2}$", opts[0].get_text(strip=True)):
                select = s
                break

    if any_played or not select:
        if select:
            cur_opt = select.find("option", selected=True) or select.find("option")
            season_label = cur_opt.get_text(strip=True) if cur_opt else None
        return league_id, season_label, soup

    options = select.find_all("option")
    for opt in options:
        val = opt.get("value")
        if not val or not val.isdigit():
            continue
        candidate_id = int(val)
        if candidate_id == league_id:
            continue
        try:
            cand_html = fetch(f"{BASE}/ScheduleAndResults/Overview/{candidate_id}")
        except RuntimeError:
            continue
        cand_soup = soupify(cand_html)
        cand_standings = parse_standings(cand_soup)
        if any(r["gp"] > 0 for r in cand_standings):
            return candidate_id, opt.get_text(strip=True), cand_soup
    # nothing with games found — return the original (all-zero) season
    cur_opt = select.find("option", selected=True) or select.find("option")
    season_label = cur_opt.get_text(strip=True) if cur_opt else None
    return league_id, season_label, soup


def scrape_league(key, cfg):
    league_id, season_label, overview_soup = resolve_season(cfg["id"])

    standings = parse_standings(overview_soup)
    games = parse_schedule_results(overview_soup)

    # The dedicated Schedule page has a longer game list than the Overview snippet.
    try:
        schedule_html = fetch(f"{BASE}/ScheduleAndResults/Schedule/{league_id}")
        schedule_soup = soupify(schedule_html)
        more_games = parse_schedule_results(schedule_soup)
        if len(more_games) > len(games):
            games = more_games
    except RuntimeError:
        pass

    scoring, goalies = [], []
    try:
        scoring_html = fetch(f"{BASE}/Players/Statistics/ScoringLeaders/{league_id}")
        scoring = parse_player_stat_table(soupify(scoring_html), ["player"], limit=60)
    except RuntimeError:
        pass
    try:
        goalies_html = fetch(f"{BASE}/Players/Statistics/LeadingGoaliesSVS/{league_id}")
        goalies = parse_player_stat_table(soupify(goalies_html), ["player"], limit=40)
    except RuntimeError:
        pass

    # Extra skater leaderboards (goals, assists, +/-, faceoffs, powerplay, shorthanded,
    # defensemen, penalty minutes). Each page is fetched independently and wrapped in its
    # own try/except so one broken/renamed URL doesn't take down the whole sync.
    player_stats = {}
    for key, slug in EXTRA_SKATER_STAT_PAGES.items():
        try:
            html = fetch(f"{BASE}/Players/Statistics/{slug}/{league_id}")
            player_stats[key] = parse_player_stat_table(soupify(html), ["team", "gp"], limit=60)
        except RuntimeError:
            player_stats[key] = []

    goalie_gaa = []
    try:
        gaa_html = fetch(f"{BASE}/Players/Statistics/LeadingGoaliesGAA/{league_id}")
        goalie_gaa = parse_player_stat_table(soupify(gaa_html), ["team", "gaa"], limit=40)
    except RuntimeError:
        pass

    # Team stats: Powerplay-efficiency + Penalty-killing live on the same page as two
    # separate tables (in that order); Fair Play is its own single-table page.
    team_powerplay, team_penalty_kill, team_fair_play = [], [], []
    try:
        pppk_html = fetch(f"{BASE}/Teams/Statistics/{TEAM_PP_PK_URL}/{league_id}")
        pppk_soup = soupify(pppk_html)
        pppk_tables = find_all_tables_by_headers(pppk_soup, ["team", "gp"])
        if len(pppk_tables) >= 1:
            team_powerplay = parse_player_stat_table_from_table(pppk_tables[0], limit=20)
        if len(pppk_tables) >= 2:
            team_penalty_kill = parse_player_stat_table_from_table(pppk_tables[1], limit=20)
    except RuntimeError:
        pass
    try:
        fp_html = fetch(f"{BASE}/Teams/Statistics/{TEAM_FAIR_PLAY_URL}/{league_id}")
        team_fair_play = parse_player_stat_table(soupify(fp_html), ["team", "pavg"], limit=20)
    except RuntimeError:
        pass

    games.sort(key=lambda g: g["date"])
    played = [g for g in games if g.get("played")]
    upcoming = [g for g in games if not g.get("played")]

    return {
        "label": cfg["label"],
        "leagueId": league_id,
        "seasonLabel": season_label,
        "standings": standings,
        "recentResults": played[-30:],
        "upcoming": upcoming[:30],
        "scoringLeaders": scoring,
        "goalieLeaders": goalies,
        "goalieGAA": goalie_gaa,
        "playerStats": player_stats,
        "teamStats": {
            "powerplay": team_powerplay,
            "penaltyKilling": team_penalty_kill,
            "fairPlay": team_fair_play,
        },
    }


def main():
    out = {
        "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "leagues": {},
    }
    had_error = False
    for key, cfg in LEAGUES.items():
        try:
            out["leagues"][key] = scrape_league(key, cfg)
            n = len(out["leagues"][key]["standings"])
            print(f"{cfg['label']}: {n} teams, "
                  f"{len(out['leagues'][key]['recentResults'])} recent results, "
                  f"{len(out['leagues'][key]['upcoming'])} upcoming, "
                  f"season={out['leagues'][key]['seasonLabel']}")
        except Exception as e:
            had_error = True
            print(f"ERROR scraping {cfg['label']}: {e}", file=sys.stderr)

    if not out["leagues"]:
        raise SystemExit("No league data scraped at all — aborting write.")

    with open(OUT_FILE, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, separators=(",", ":"))

    print(f"Wrote {OUT_FILE}")
    if had_error:
        # Non-fatal: partial data is still useful, but flag it in the Action log.
        print("Completed with partial errors — see above.", file=sys.stderr)


if __name__ == "__main__":
    main()
