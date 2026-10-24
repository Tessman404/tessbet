"""Suivi en direct des paris en cours : notifie sur le téléphone le coup d'envoi, les buts, la mi-temps et le résultat.

Les scores viennent de l'API publique d'ESPN (sans clé). Elle n'est pas officielle : si elle change, le suivi
s'arrête sans gêner le reste de Tessbet.
"""
import datetime as dt
import json
import os
import re
import sys
import time

import engine

ESPN = "https://site.api.espn.com/apis/site/v2/sports"
LEAGUE_SLUGS = {
    "soccer_france_ligue_one": "soccer/fra.1", "soccer_epl": "soccer/eng.1",
    "soccer_spain_la_liga": "soccer/esp.1", "soccer_germany_bundesliga": "soccer/ger.1",
    "soccer_italy_serie_a": "soccer/ita.1", "soccer_uefa_champs_league": "soccer/uefa.champions",
    "soccer_uefa_europa_league": "soccer/uefa.europa", "basketball_nba": "basketball/nba",
}
SPORT_SLUGS = {
    "football": ["soccer/fra.1", "soccer/eng.1", "soccer/esp.1", "soccer/ger.1", "soccer/ita.1",
                 "soccer/uefa.champions", "soccer/uefa.europa"],
    "basketball": ["basketball/nba"],
}


def _path(name):
    return os.path.join(engine.DATA, name)


def _read(name, default):
    try:
        with open(_path(name), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def enabled():
    return bool(_read("settings.json", {}).get("live", True))


def split_match(match):
    parts = re.split(r"\s+[–-]\s+", match or "", maxsplit=1)
    return (parts[0].strip(), parts[1].strip()) if len(parts) == 2 else (None, None)


def pick_team(bet):
    if bet.get("pick_team"):
        return bet["pick_team"]
    label = bet.get("pick", "")
    return None if label.startswith("Match nul") else re.sub(r"^Victoire de ", "", label)


def fetch_events(slug, day):
    """Matchs de la veille, du jour et du lendemain (ESPN compte les jours à l'heure américaine)."""
    d0 = day - dt.timedelta(days=1)
    d1 = day + dt.timedelta(days=1)
    rng = f"{d0:%Y%m%d}-{d1:%Y%m%d}"
    data, _ = engine.http_json(f"{ESPN}/{slug}/scoreboard?dates={rng}", {"User-Agent": "Mozilla/5.0 (Tessbet)"}, 15)
    return data.get("events", [])


def find_event(events, home, away):
    for ev in events:
        comp = (ev.get("competitions") or [{}])[0]
        teams = comp.get("competitors") or []
        if len(teams) != 2:
            continue
        for i, t in enumerate(teams):
            other = teams[1 - i]
            names = lambda x: [x["team"].get("displayName", ""), x["team"].get("name", "")]
            if any(engine.team_match(home, n) for n in names(t)) and any(engine.team_match(away, n) for n in names(other)):
                return ev, comp, i
    return None, None, None


def read_state(comp, home_idx):
    teams = comp["competitors"]
    st = comp.get("status") or {}
    typ = st.get("type") or {}
    def score(t):
        try:
            return int(float(t.get("score", 0)))
        except (TypeError, ValueError):
            return 0
    h, a = score(teams[home_idx]), score(teams[1 - home_idx])
    return {"state": typ.get("state", "pre"), "name": typ.get("name", ""), "clock": st.get("displayClock", ""),
            "score": [h, a], "start": comp.get("date") or comp.get("startDate")}


def last_goal(comp, home_idx):
    plays = [d for d in (comp.get("details") or []) if d.get("scoringPlay")]
    if not plays:
        return ""
    p = plays[-1]
    who = (p.get("athletesInvolved") or [{}])[0].get("shortName", "")
    tid = (p.get("team") or {}).get("id")
    team = ""
    for t in comp["competitors"]:
        if t["team"].get("id") == tid:
            team = t["team"].get("shortDisplayName") or t["team"].get("displayName", "")
    tag = " (csc)" if p.get("ownGoal") else " (pen.)" if p.get("penaltyKick") else ""
    clock = (p.get("clock") or {}).get("displayValue", "")
    return " – ".join(x for x in (clock, f"{who}{tag}".strip(), team) if x)


def pick_side(bet, home, away):
    """'home', 'away' ou 'draw' selon le pari."""
    team = pick_team(bet)
    if team is None:
        return "draw"
    if engine.team_match(team, home):
        return "home"
    if engine.team_match(team, away):
        return "away"
    return None


def pick_status(side, score):
    h, a = score
    if side is None:
        return ""
    if side == "draw":
        return "égalité : ton pari est bon pour l'instant" if h == a else "pas d'égalité pour l'instant"
    mine, other = (h, a) if side == "home" else (a, h)
    return "ton équipe mène" if mine > other else "égalité" if mine == other else "ton équipe est menée"


def final_result(side, score):
    h, a = score
    if side is None:
        return None
    if side == "draw":
        return "win" if h == a else "lose"
    mine, other = (h, a) if side == "home" else (a, h)
    return "win" if mine > other else "lose"


def tracked(bets):
    """Ce qu'il faut suivre : chaque pari simple en cours, et chaque match encore ouvert d'un combiné (identifiant « pari:n°»)."""
    out = []
    for b in bets:
        if b.get("result") != "pending":
            continue
        if not b.get("legs"):
            out.append(b)
            continue
        for i, leg in enumerate(b["legs"]):
            if leg.get("result", "pending") != "pending":
                continue
            out.append({"id": f"{b['id']}:{i}", "date": (leg.get("kickoff") or b.get("date") or "")[:10], "match": leg["match"],
                        "pick": leg["pick"], "pick_team": leg.get("pick_team"), "sport": leg.get("sport") or "football",
                        "league_key": leg.get("league_key"), "result": "pending", "shown": f"{leg['pick']} (combiné)"})
    return out


def combo_status(legs):
    """Où en est un combiné, d'après le résultat ou l'état en direct de chacun de ses matchs."""
    codes = []
    for leg in legs:
        r = leg.get("result", "pending")
        codes.append({"win": "won", "lose": "lost", "void": "void"}.get(r) or (leg.get("status") or {}).get("code"))
    if "lost" in codes:
        return {"code": "lost", "text": "Combiné perdu"}
    if "won" in codes and all(c in ("won", "void") for c in codes):
        return {"code": "won", "text": "Combiné gagné"}
    if "losing" in codes:
        return {"code": "losing", "text": "Un match est mal parti"}
    if "draw" in codes:
        return {"code": "draw", "text": "Un match est à égalité"}
    won = codes.count("won")
    if "winning" in codes:
        return {"code": "winning", "text": "Les matchs en cours vont dans ton sens"}
    if won:
        return {"code": "winning", "text": f"{won} match{'s' if won > 1 else ''} gagné{'s' if won > 1 else ''} sur {len(legs)}"}
    return None


def bet_status(bet, lv):
    """Où en est mon pari : gagne / perd / égalité en cours, ou gagné / perdu si le match est fini."""
    if not lv:
        return None
    home, away = split_match(bet.get("match"))
    if not home:
        return None
    side = pick_side(bet, home, away)
    if side is None:
        return None
    score = lv["score"]
    if lv["state"] == "post":
        r = final_result(side, score)
        return {"code": "won" if r == "win" else "lost", "text": "Gagné" if r == "win" else "Perdu"}
    if lv["state"] != "in":
        return None
    h, a = score
    mine, other = (h, a) if side != "away" else (a, h)
    if side == "draw":
        ok = h == a
        return {"code": "winning" if ok else "losing", "text": "Tu gagnes pour l'instant" if ok else "Tu perds pour l'instant"}
    if mine > other:
        return {"code": "winning", "text": "Tu gagnes pour l'instant"}
    if mine == other:
        return {"code": "draw", "text": "Égalité : pas encore gagné"}
    return {"code": "losing", "text": "Tu perds pour l'instant"}


def messages(bet, home, away, cur, st, sport, first):
    """Liste de (clé, titre, message) à envoyer d'après l'évolution du match."""
    side = pick_side(bet, home, away)
    pick = bet.get("shown") or bet.get("pick", "")
    sc = cur["score"]
    line = f"{home} {sc[0]}-{sc[1]} {away}"
    out = []
    if cur["state"] == "in" and not st.get("started"):
        out.append(("started", "▶️ Coup d'envoi" if not first else "🔴 Match en cours",
                    f"{line}. Ton pari : {pick}."))
    if (sport == "football" and cur["state"] in ("in", "post") and st.get("score") is not None
            and sc != st["score"] and sum(sc) > sum(st["score"]) and not (first and cur["state"] == "post")):
        out.append(("goal", f"⚽ BUT ! {line}", f"{cur.get('goal', '')}. Ton pari « {pick} » : {pick_status(side, sc)}.".replace(" . ", ". ")))
    if cur["name"] == "STATUS_HALFTIME" and not st.get("ht"):
        out.append(("ht", f"⏸️ Mi-temps : {line}", f"Ton pari « {pick} » : {pick_status(side, sc)}."))
    if cur["state"] == "post" and not st.get("final"):
        res = final_result(side, sc)
        verdict = {"win": "semble GAGNÉ ✅", "lose": "semble PERDU ❌"}.get(res, "est terminé")
        out.append(("final", f"🏁 Terminé : {line}",
                    f"Ton pari « {pick} » {verdict}. Confirme dans Tessbet pour mettre ton argent à jour."))
    return out


def tick(now=None):
    now = now or time.time()
    bets = _read("bets.json", {}).get("bets", [])
    state = _read("live.json", {})
    changed = False
    cache = {}
    today = dt.datetime.now(dt.timezone.utc).date()
    for bet in tracked(bets):
        if bet.get("result") != "pending" or bet.get("sport") not in SPORT_SLUGS:
            continue
        try:
            bday = dt.date.fromisoformat(bet.get("date", ""))
        except ValueError:
            bday = today
        if (today - bday).days > 2:
            continue
        home, away = split_match(bet.get("match"))
        if not home:
            continue
        st = state.get(bet["id"], {})
        if st.get("done") or now < st.get("next", 0):
            continue
        slugs = [st["slug"]] if st.get("slug") else (
            [LEAGUE_SLUGS[bet["league_key"]]] if bet.get("league_key") in LEAGUE_SLUGS else SPORT_SLUGS[bet["sport"]])
        found = None
        for slug in slugs:
            try:
                if (slug, bday) not in cache:
                    cache[(slug, bday)] = fetch_events(slug, bday)
            except Exception as e:
                print("suivi direct :", slug, str(e)[:80], file=sys.stderr)
                continue
            ev, comp, idx = find_event(cache[(slug, bday)], home, away)
            if ev:
                found = (slug, ev, comp, idx)
                break
        first = "score" not in st
        if not found:
            st["miss"] = st.get("miss", 0) + 1
            st["next"] = now + (600 if st["miss"] < 40 else 3600)
            state[bet["id"]] = st
            changed = True
            continue
        slug, ev, comp, idx = found
        cur = read_state(comp, idx)
        cur["goal"] = last_goal(comp, idx)
        st["slug"] = slug
        for key, title, message in messages(bet, home, away, cur, st, bet["sport"], first):
            try:
                engine.send_phone(title, message, f"tessbet-{bet['id']}")
            except Exception as e:
                print("suivi direct : notification impossible :", str(e)[:100], file=sys.stderr)
                break
            if key == "goal":
                continue
            st[{"started": "started", "ht": "ht", "final": "final"}[key]] = True
        else:
            st["score"] = cur["score"]
        if first and "score" not in st:
            st["score"] = cur["score"]
        st["live"] = {"score": cur["score"], "clock": cur["clock"], "state": cur["state"]}
        if cur["state"] == "post" and st.get("final"):
            st["done"] = True
        elif cur["state"] == "in":
            st["next"] = now + 45
        else:
            try:
                start = dt.datetime.fromisoformat(cur["start"].replace("Z", "+00:00")).timestamp()
                st["next"] = now + max(60, min(600, start - now - 900))
            except Exception:
                st["next"] = now + 300
        state[bet["id"]] = st
        changed = True
    if changed:
        engine.write_json(_path("live.json"), state)
    return state


def run_loop():
    while True:
        wait = 120
        try:
            if enabled():
                state = tick()
                wait = 30 if any(s.get("live", {}).get("state") == "in" for s in state.values()) else 60
        except Exception as e:  # le suivi ne doit jamais s'arrêter
            print("suivi direct :", e, file=sys.stderr)
        time.sleep(wait)
