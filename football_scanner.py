"""
Football Match Tip Scanner
---------------------------
Pulls upcoming fixtures from football-data.org (v4), scores them using
form + goals data, and pushes flagged matches to Telegram as stats-based
"leans" (NOT guaranteed tips - see disclaimer in alert).

Deployment: Railway persistent worker (Procfile: worker: python football_scanner.py)

Env vars required:
    FOOTBALL_DATA_TOKEN -> your football-data.org X-Auth-Token
    TELEGRAM_BOT_TOKEN
    TELEGRAM_CHAT_ID     (defaults to Ralph's known chat id below if unset)

Follows Ralph's standard architecture:
    - closed-only / pre-match data (no live in-play signals)
    - file-backed cooldown to avoid duplicate alerts (cooldown_football.json)
    - HTML parse mode Telegram alerts
    - WAT (UTC+1) timestamps
    - startup ping to confirm deployment
    - logging format matches other bots (%(levelname)-8s)

NOTE on rate limits: football-data.org free tier = 10 requests/minute,
no daily cap. This script paces requests with a REQUEST_DELAY_SECONDS gap
to stay safely under that ceiling.

NOTE on endpoint choice: the free tier does NOT support the global
/v4/matches endpoint filtered across multiple competitions (paid-tier
only, returns 403 Restricted Resource). This scanner instead calls
/v4/competitions/{code}/matches once per competition, which the free
tier does allow.
"""

import os
import json
import time
import logging
import requests
from datetime import datetime, timedelta, timezone

# ---------------------------------------------------------------------------
# CONFIG
# ---------------------------------------------------------------------------

FOOTBALL_DATA_TOKEN = os.environ.get("FOOTBALL_DATA_TOKEN", "")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "388117501")

API_BASE = "https://api.football-data.org/v4"

COOLDOWN_FILE = "cooldown_football.json"
COOLDOWN_HOURS = 12               # don't re-alert same fixture within this window
SCAN_INTERVAL_SECONDS = 60 * 60   # scan once per hour
LOOKAHEAD_HOURS = 30              # only consider fixtures kicking off within this window
LAST_N_MATCHES = 5                # form sample size per team
REQUEST_DELAY_SECONDS = 6.5       # free tier = 10 req/min, so pace requests ~6.5s apart

# Competitions to scan - codes per football-data.org free tier
# PL=Premier League, PD=La Liga, SA=Serie A, BL1=Bundesliga, FL1=Ligue 1, CL=Champions League
# (Argentina's Liga Profesional is NOT on the free tier - dropped)
COMPETITION_CODES = ["PL", "PD", "SA", "BL1", "FL1", "CL"]

WAT = timezone(timedelta(hours=1))

# Scoring thresholds
MIN_CONFIDENCE_SCORE = 6   # out of 9 - only alert on PRIME-tier matches

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(message)s",
)
log = logging.getLogger("football_scanner")

HEADERS = {
    "X-Auth-Token": FOOTBALL_DATA_TOKEN,
}


# ---------------------------------------------------------------------------
# COOLDOWN (file-backed, same pattern as your other scanners)
# ---------------------------------------------------------------------------

def load_cooldown():
    if os.path.exists(COOLDOWN_FILE):
        try:
            with open(COOLDOWN_FILE, "r") as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError):
            return {}
    return {}


def save_cooldown(data):
    with open(COOLDOWN_FILE, "w") as f:
        json.dump(data, f)


def in_cooldown(cooldown, match_id):
    ts = cooldown.get(str(match_id))
    if not ts:
        return False
    last = datetime.fromisoformat(ts)
    return datetime.now(timezone.utc) - last < timedelta(hours=COOLDOWN_HOURS)


def mark_cooldown(cooldown, match_id):
    cooldown[str(match_id)] = datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# FOOTBALL-DATA.ORG CALLS
# ---------------------------------------------------------------------------

def api_get(endpoint, params=None):
    url = f"{API_BASE}/{endpoint}"
    try:
        resp = requests.get(url, headers=HEADERS, params=params or {}, timeout=15)
        if resp.status_code == 429:
            log.warning("Hit rate limit (429) - backing off 60s")
            time.sleep(60)
            resp = requests.get(url, headers=HEADERS, params=params or {}, timeout=15)
        resp.raise_for_status()
        return resp.json()
    except requests.RequestException as e:
        log.error(f"API request failed [{endpoint}]: {e}")
        return None
    finally:
        time.sleep(REQUEST_DELAY_SECONDS)


def get_upcoming_matches():
    """
    Fetch scheduled matches within the lookahead window, one competition at a time.

    NOTE: the free tier does NOT allow the global /v4/matches endpoint filtered
    across multiple competitions at once (that's a paid-tier feature - it
    returns 403 Restricted Resource). The free-tier-safe approach is to call
    /v4/competitions/{code}/matches separately per competition.
    """
    now = datetime.now(timezone.utc)
    date_from = now.strftime("%Y-%m-%d")
    date_to = (now + timedelta(hours=LOOKAHEAD_HOURS)).strftime("%Y-%m-%d")
    cutoff = now + timedelta(hours=LOOKAHEAD_HOURS)

    all_matches = []
    for code in COMPETITION_CODES:
        data = api_get(f"competitions/{code}/matches", {
            "dateFrom": date_from,
            "dateTo": date_to,
            "status": "SCHEDULED",
        })
        if not data or not data.get("matches"):
            continue

        for m in data["matches"]:
            try:
                kickoff = datetime.fromisoformat(m["utcDate"].replace("Z", "+00:00"))
            except (KeyError, ValueError):
                continue
            if now <= kickoff <= cutoff:
                all_matches.append(m)

    return all_matches


def get_team_recent_stats(team_id):
    """Pull last N finished matches for a team and compute goals for/against + form points."""
    data = api_get(f"teams/{team_id}/matches", {
        "status": "FINISHED",
        "limit": LAST_N_MATCHES,
    })
    if not data or not data.get("matches"):
        return None

    goals_for, goals_against, points = 0, 0, 0
    matches_counted = 0

    for m in data["matches"]:
        home_id = m["homeTeam"]["id"]
        away_id = m["awayTeam"]["id"]
        full_time = m.get("score", {}).get("fullTime", {})
        gh = full_time.get("home")
        ga = full_time.get("away")
        if gh is None or ga is None:
            continue  # not finished / walkover

        matches_counted += 1
        if team_id == home_id:
            goals_for += gh
            goals_against += ga
            if gh > ga:
                points += 3
            elif gh == ga:
                points += 1
        else:
            goals_for += ga
            goals_against += gh
            if ga > gh:
                points += 3
            elif gh == ga:
                points += 1

    if matches_counted == 0:
        return None

    return {
        "avg_goals_for": goals_for / matches_counted,
        "avg_goals_against": goals_against / matches_counted,
        "form_points": points,
        "matches_counted": matches_counted,
    }


# ---------------------------------------------------------------------------
# SCORING LOGIC
# ---------------------------------------------------------------------------

def score_fixture(home_stats, away_stats):
    """
    Produces a 0-9 confidence score plus tip leans, based purely on recent
    scoring/conceding rates and form points. This is a statistical summary,
    not a prediction guarantee.
    """
    score = 0
    tips = []

    combined_avg_goals = home_stats["avg_goals_for"] + away_stats["avg_goals_for"]

    # --- Over/Under 2.5 lean ---
    if combined_avg_goals >= 3.0:
        tips.append(("Over 2.5 Goals", "lean"))
        score += 2
    elif combined_avg_goals <= 1.8:
        tips.append(("Under 2.5 Goals", "lean"))
        score += 2

    # --- BTTS lean ---
    home_scores_often = home_stats["avg_goals_for"] >= 1.2
    away_scores_often = away_stats["avg_goals_for"] >= 1.2
    home_leaky = home_stats["avg_goals_against"] >= 1.2
    away_leaky = away_stats["avg_goals_against"] >= 1.2

    if home_scores_often and away_scores_often and (home_leaky or away_leaky):
        tips.append(("BTTS - Yes", "lean"))
        score += 2
    elif (not home_scores_often or not away_scores_often) and not (home_leaky and away_leaky):
        tips.append(("BTTS - No", "lean"))
        score += 1

    # --- Form differential (possible match-winner lean) ---
    form_diff = home_stats["form_points"] - away_stats["form_points"]
    if form_diff >= 6:
        tips.append(("Home form edge", "lean"))
        score += 2
    elif form_diff <= -6:
        tips.append(("Away form edge", "lean"))
        score += 2
    else:
        score += 1  # closely matched form, small credit for data completeness

    # --- Data completeness bonus ---
    if home_stats["matches_counted"] >= 4 and away_stats["matches_counted"] >= 4:
        score += 2

    return min(score, 9), tips


# ---------------------------------------------------------------------------
# TELEGRAM
# ---------------------------------------------------------------------------

def send_telegram_alert(match, home_stats, away_stats, score, tips):
    if not TELEGRAM_BOT_TOKEN:
        log.warning("TELEGRAM_BOT_TOKEN not set - skipping alert send")
        return

    home = match["homeTeam"]["name"]
    away = match["awayTeam"]["name"]
    competition = match["competition"]["name"]
    kickoff_utc = datetime.fromisoformat(match["utcDate"].replace("Z", "+00:00"))
    kickoff_wat = kickoff_utc.astimezone(WAT)

    tip_lines = "\n".join([f"  • {label}" for label, _ in tips]) if tips else "  • No strong lean"

    message = (
        f"<b>[FOOTBALL SCAN] PRIME {score}/9</b>\n\n"
        f"<b>{home} vs {away}</b>\n"
        f"{competition}\n"
        f"Kickoff: {kickoff_wat.strftime('%a %d %b, %H:%M')} WAT\n\n"
        f"<b>Stats leans:</b>\n{tip_lines}\n\n"
        f"Home form (last {home_stats['matches_counted']}): "
        f"{home_stats['avg_goals_for']:.1f} GF / {home_stats['avg_goals_against']:.1f} GA, "
        f"{home_stats['form_points']} pts\n"
        f"Away form (last {away_stats['matches_counted']}): "
        f"{away_stats['avg_goals_for']:.1f} GF / {away_stats['avg_goals_against']:.1f} GA, "
        f"{away_stats['form_points']} pts\n\n"
        f"<i>Stats-based lean only, not a guaranteed outcome. Football has high "
        f"variance - size any bets accordingly.</i>"
    )

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "HTML"}
    try:
        resp = requests.post(url, json=payload, timeout=10)
        resp.raise_for_status()
        log.info(f"Alert sent: {home} vs {away} ({score}/9)")
    except requests.RequestException as e:
        log.error(f"Telegram send failed: {e}")


def send_startup_ping():
    if not TELEGRAM_BOT_TOKEN:
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": "<b>[FOOTBALL SCAN]</b> Scanner deployed and running on Railway.",
        "parse_mode": "HTML",
    }
    try:
        requests.post(url, json=payload, timeout=10)
    except requests.RequestException:
        pass


# ---------------------------------------------------------------------------
# MAIN SCAN LOOP
# ---------------------------------------------------------------------------

def run_scan(cooldown):
    log.info("Starting scan cycle...")
    matches = get_upcoming_matches()
    log.info(f"Found {len(matches)} upcoming matches in window")

    for m in matches:
        match_id = m["id"]
        if in_cooldown(cooldown, match_id):
            continue

        home_id = m["homeTeam"]["id"]
        away_id = m["awayTeam"]["id"]

        home_stats = get_team_recent_stats(home_id)
        away_stats = get_team_recent_stats(away_id)

        if not home_stats or not away_stats:
            continue

        score, tips = score_fixture(home_stats, away_stats)

        if score >= MIN_CONFIDENCE_SCORE:
            send_telegram_alert(m, home_stats, away_stats, score, tips)
            mark_cooldown(cooldown, match_id)
            save_cooldown(cooldown)

    log.info("Scan cycle complete.")


def main():
    if not FOOTBALL_DATA_TOKEN:
        log.error("FOOTBALL_DATA_TOKEN not set. Exiting.")
        return

    log.info("Football Match Tip Scanner starting up...")
    send_startup_ping()
    cooldown = load_cooldown()

    while True:
        try:
            run_scan(cooldown)
        except Exception as e:
            log.error(f"Unhandled error in scan cycle: {e}")
        time.sleep(SCAN_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
