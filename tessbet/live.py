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
    "tennis": ["tennis/atp", "tennis/wta"],
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
    """Matchs de la veille, du jour et du lendemain (ESPN compte les jours à l'heure américaine).

    Le foot accepte une période en une seule demande ; le basket la refuse (erreur 400) : un jour à la fois."""
    d0, d1 = day - dt.timedelta(days=1), day + dt.timedelta(days=1)
    if slug.startswith("tennis/"):
        return tennis_events(slug, day)
    ranges = [f"{d:%Y%m%d}" for d in (day, d0, d1)]  # ESPN refuse désormais les périodes : un jour à la fois
    events, seen = [], set()
    for rng in ranges:
        data, _ = engine.http_json(f"{ESPN}/{slug}/scoreboard?dates={rng}", {"User-Agent": "Mozilla/5.0 (Tessbet)"}, 15)
        for ev in data.get("events", []):
            if ev.get("id") not in seen:
                seen.add(ev.get("id"))
                events.append(ev)
    return events


def tennis_events(slug, day):
    """Tennis : ESPN range les matchs par tournoi ; on les remet à plat, joueur = « équipe »."""
    data, _ = engine.http_json(f"{ESPN}/{slug}/scoreboard?dates={day:%Y%m%d}", {"User-Agent": "Mozilla/5.0 (Tessbet)"}, 15)
    out = []
    for tour in data.get("events", []):
        for g in tour.get("groupings", []):
            if "Doubles" in ((g.get("grouping") or {}).get("displayName") or ""):
                continue
            for c in g.get("competitions", []):
                comps = []
                for x in c.get("competitors", []):
                    a = x.get("athlete") or {}
                    name = a.get("displayName") or ""
                    comps.append(dict(x, team={"id": a.get("id") or x.get("id"), "displayName": name, "name": name, "shortDisplayName": a.get("shortName") or name}))
                if len(comps) == 2 and all(t["team"]["displayName"] for t in comps):
                    out.append({"id": c.get("id"), "competitions": [dict(c, competitors=comps, tennis=True)]})
    out.sort(key=lambda e: e["competitions"][0].get("date") or "", reverse=True)  # le match le plus récent d'abord
    return out


# Noms qui diffèrent entre les sites de cotes et ESPN
ALIASES = {
    "inter milan": "internazionale", "inter": "internazionale", "ac milan": "milan", "psg": "paris saint-germain",
    "paris saint germain": "paris saint-germain", "athletic bilbao": "athletic club", "atletico madrid": "atlético madrid",
    "bayern munich": "bayern munchen", "bayer leverkusen": "bayer 04 leverkusen", "borussia monchengladbach": "monchengladbach",
    "wolverhampton wanderers": "wolves", "tottenham hotspur": "tottenham", "manchester united": "man united",
    "brighton and hove albion": "brighton", "nottingham forest": "nottm forest", "sevilla": "sevilla fc",
    "la clippers": "la clippers", "los angeles clippers": "la clippers", "los angeles lakers": "los angeles lakers",
}


def _same(a, b):
    a2, b2 = engine.unaccent(a or "").lower().strip(), engine.unaccent(b or "").lower().strip()
    a2, b2 = ALIASES.get(a2, a2), ALIASES.get(b2, b2)
    return engine.team_match(a2, b2) or engine.team_match(a or "", b or "")


def _names(x):
    t = x.get("team") or {}
    return [t.get("displayName", ""), t.get("name", ""), t.get("shortDisplayName", ""), t.get("location", "")]


def find_event(events, home, away, kickoff=None):
    """Retrouve le match chez ESPN. Les deux noms d'abord ; sinon un seul nom + l'heure du coup d'envoi (noms différents d'un site à l'autre)."""
    try:
        k = dt.datetime.fromisoformat(kickoff.replace("Z", "+00:00")).timestamp() if kickoff else None
    except ValueError:
        k = None
    partial = []
    for ev in events:
        comp = (ev.get("competitions") or [{}])[0]
        teams = comp.get("competitors") or []
        if len(teams) != 2:
            continue
        for i, t in enumerate(teams):
            other = teams[1 - i]
            h_ok = any(_same(home, n) for n in _names(t) if n)
            a_ok = any(_same(away, n) for n in _names(other) if n)
            if h_ok and a_ok:
                return ev, comp, i
            if h_ok or a_ok:
                partial.append((ev, comp, i))
    if k:
        def start(c):
            try:
                return dt.datetime.fromisoformat((c.get("date") or "").replace("Z", "+00:00")).timestamp()
            except ValueError:
                return None
        partial = [p for p in partial if start(p[1]) is not None and abs(start(p[1]) - k) <= 90 * 60]
    if len({id(p[1]) for p in partial}) == 1:
        return partial[0]
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
    clock = st.get("displayClock", "")
    if comp.get("tennis"):  # tennis : score = sets gagnés, « horloge » = jeux de chaque set
        mine, other = teams[home_idx].get("linescores") or [], teams[1 - home_idx].get("linescores") or []
        def won(x, y):  # set gagné : indiqué par ESPN, ou score de set terminé (6 jeux avec 2 d'écart, ou 7)
            gx, gy = int(x.get("value", 0) or 0), int(y.get("value", 0) or 0)
            return bool(x.get("winner")) or (gx >= 6 and gx - gy >= 2) or (gx == 7 and gy >= 5)
        h = sum(1 for x, y in zip(mine, other) if won(x, y))
        a = sum(1 for x, y in zip(mine, other) if won(y, x))
        games = [f"{int(x.get('value', 0))}-{int(y.get('value', 0))}" for x, y in zip(mine, other)]
        if typ.get("state") == "post" and h == a:  # abandon : le vainqueur est indiqué à part
            if teams[home_idx].get("winner"):
                h += 1
            elif teams[1 - home_idx].get("winner"):
                a += 1
        clock = (f"set {len(games)} : {games[-1]}" + (f" ({' '.join(games[:-1])})" if len(games) > 1 else "")) if typ.get("state") == "in" and games \
            else " ".join(games) + (" (abandon)" if typ.get("name") == "STATUS_RETIRED" else "")
    return {"state": typ.get("state", "pre"), "name": typ.get("name", ""), "clock": clock,
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
            out.append({"id": f"{b['id']}:{i}", "date": (leg.get("kickoff") or b.get("date") or "")[:10], "match": leg["match"], "kickoff": leg.get("kickoff"),
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
        g = re.match(r"set \d+ : (\d+)-(\d+)", lv.get("clock") or "")  # tennis : sets à égalité, on regarde le set en cours
        if g:
            gh, ga = int(g.group(1)), int(g.group(2))
            gm, go = (gh, ga) if side != "away" else (ga, gh)
            if gm != go:
                return {"code": "winning" if gm > go else "losing",
                        "text": "Mène dans le set en cours" if gm > go else "Menée dans le set en cours"}
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
            ev, comp, idx = find_event(cache[(slug, bday)], home, away, bet.get("kickoff"))
            if ev:
                found = (slug, ev, comp, idx)
                break
        first = "score" not in st
        if not found:
            st["miss"] = st.get("miss", 0) + 1
            try:
                k = dt.datetime.fromisoformat(bet["kickoff"].replace("Z", "+00:00")).timestamp()
            except Exception:
                k = None
            near = k is not None and k - 900 <= now <= k + 3 * 3600  # autour du match : on réessaie souvent
            st["next"] = now + (90 if near else 600 if st["miss"] < 40 else 3600)
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
        st["live"] = {"score": cur["score"], "clock": cur["clock"], "state": cur["state"], "start": cur.get("start")}
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


# ---------------------------------------------------------------- résultats des pronos partagés sur Telegram
def tg_send(text):
    import urllib.request
    token = engine.load_env().get("TELEGRAM_TOKEN", "")
    chat = (_read("telegram.json", {}).get("chat") or {}).get("id")
    if not token or not chat:
        return False
    req = urllib.request.Request(f"https://api.telegram.org/bot{token}/sendMessage", method="POST",
                                 data=json.dumps({"chat_id": chat, "text": text, "parse_mode": "HTML"}).encode("utf-8"),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return r.status == 200


_tg_next = {}


def tg_results(now=None):
    """À la fin de chaque match proposé et partagé, le bot annonce s'il est gagné ou perdu, puis le bilan du jour."""
    now = now or time.time()
    t = _read("telegram.json", {})
    shared = t.get("shared") or []
    if not shared:
        return 0
    sent, cache = 0, {}
    ball = {"football": "⚽", "basketball": "🏀", "tennis": "🎾"}
    for x in shared:
        if x.get("reported") or now < _tg_next.get(x["id"], 0):
            continue
        try:
            kick = dt.datetime.fromisoformat(x["kickoff"].replace("Z", "+00:00")).timestamp()
        except Exception:
            kick = 0
        if kick and now < kick + 80 * 60:  # pas avant la fin probable du match
            _tg_next[x["id"]] = kick + 80 * 60
            continue
        home, away = split_match(x["match"])
        slugs = [LEAGUE_SLUGS[x["league_key"]]] if x.get("league_key") in LEAGUE_SLUGS else SPORT_SLUGS.get(x["sport"], [])
        day = dt.date.fromisoformat((x.get("kickoff") or x["date"])[:10])
        found = None
        for slug in slugs:
            try:
                if (slug, day) not in cache:
                    cache[(slug, day)] = fetch_events(slug, day)
            except Exception:
                continue
            ev, comp, idx = find_event(cache[(slug, day)], home, away, x.get("kickoff"))
            if ev:
                found = read_state(comp, idx)
                break
        if not found or found["state"] != "post":
            _tg_next[x["id"]] = now + 600
            continue
        res = final_result(pick_side({"pick": x["pick"], "pick_team": x.get("pick_team")}, home, away), found["score"])
        if res is None:
            x["reported"] = "unknown"
            continue
        sc = f"{found['score'][0]}-{found['score'][1]}" + (f" ({found['clock']})" if x["sport"] == "tennis" and found.get("clock") else "")
        head = ("✅ <b>GAGNÉ</b>" if res == "win" else "❌ <b>PERDU</b>") + (" 🤔 <i>(moins sûr)</i>" if x.get("tier") == "moins_sur" else "")
        try:
            tg_send(f"{head} {ball.get(x['sport'], '')} {_esc(home)} {sc} {_esc(away)}\n👉 {_esc(x['pick'])} @ {engine.fr(x['odds'], 2)}"
                    + (" (bonne affaire)" if x.get("tier") == "value" else ""))
        except Exception as e:
            print("telegram :", str(e)[:100], file=sys.stderr)
            _tg_next[x["id"]] = now + 600
            continue
        x["reported"] = res
        x["score"] = sc
        sent += 1
        day_picks = [y for y in shared if y["date"] == x["date"]]
        if all(y.get("reported") for y in day_picks) and x["date"] not in t.get("bilans", []):
            def count(group):
                return sum(y["reported"] == "win" for y in group), sum(y["reported"] in ("win", "lose") for y in group)
            pronos = [y for y in day_picks if y.get("tier") != "moins_sur"]
            others = [y for y in day_picks if y.get("tier") == "moins_sur"]
            lines = [f"📊 <b>Bilan Tessbet du {x['date'][8:10]}/{x['date'][5:7]}</b>"]
            for label, group in (("Pronos", pronos), ("Moins sûrs", others)):
                if not group:
                    continue
                w, k = count(group)
                lines.append(f"\n<b>{label} : {w}/{k}</b>")
                for y in group:
                    mark = {"win": "✅", "lose": "❌"}.get(y["reported"], "❔")
                    lines.append(f"{mark} {_esc(y['match'])} – {_esc(y['pick'])} @ {engine.fr(y['odds'], 2)}" + (f" ({_esc(y['score'])})" if y.get("score") else ""))
            try:
                tg_send("\n".join(lines))
                t.setdefault("bilans", []).append(x["date"])
                t["bilans"] = t["bilans"][-14:]
            except Exception:
                pass
    if sent or any(x.get("reported") == "unknown" for x in shared):
        engine.write_json(_path("telegram.json"), t)
    return sent


def _esc(s):
    return str(s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def board(bets, state, now=None):
    """Les matchs de mes paris en cours, pour la tuile « En direct » de l'aperçu Home Assistant."""
    rows = []
    for b in bets:
        if b.get("result") != "pending":
            continue
        legs = b.get("legs") or [b]
        combo = f"Combiné de {len(legs)} matchs" if b.get("legs") else ""
        for i, leg in enumerate(legs):
            lv = state.get(f"{b['id']}:{i}" if b.get("legs") else b["id"], {}).get("live") or {}
            home, away = split_match(leg.get("match"))
            st = bet_status(leg, lv) if lv else None
            res = leg.get("result", "pending") if b.get("legs") else "pending"
            rows.append({
                "sport": leg.get("sport") or b.get("sport") or "football", "home": home or leg.get("match", ""), "away": away or "",
                "pick": leg.get("pick", ""), "combo": combo, "odds": leg.get("odds"),
                "score": lv.get("score"), "clock": lv.get("clock", ""), "state": lv.get("state", "pre"),
                "start": lv.get("start") or leg.get("kickoff") or "", "result": res,
                "status": {"win": "Gagné", "lose": "Perdu", "void": "Annulé"}.get(res) or (st or {}).get("text", ""),
                "code": {"win": "won", "lose": "lost", "void": "void"}.get(res) or (st or {}).get("code", ""),
            })
    order = {"in": 0, "pre": 1, "post": 2}
    rows.sort(key=lambda r: (order.get(r["state"], 1), r["start"] or "9"))
    return rows


_last_pub = {"sig": None, "t": 0}


def publish(now=None):
    """Met à jour le capteur sensor.tessbet_direct dans Home Assistant (seulement si quelque chose a changé)."""
    now = now or time.time()
    rows = board(_read("bets.json", {}).get("bets", []), _read("live.json", {}))
    sig = json.dumps(rows, sort_keys=True)
    if sig == _last_pub["sig"] and now - _last_pub["t"] < 600:
        return False
    env = engine.load_env()
    base, token = env.get("HA_URL", "").rstrip("/"), env.get("HA_TOKEN", "")
    if not base or not token:
        return False
    engine.ha_post(f"{base}/api/states/sensor.tessbet_direct", token, {
        "state": len(rows),
        "attributes": {"friendly_name": "Tessbet en direct", "icon": "mdi:scoreboard-outline", "unit_of_measurement": "matchs",
                       "live": sum(r["state"] == "in" for r in rows), "matches": rows,
                       "updated": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}})
    _last_pub.update(sig=sig, t=now)
    return True


def run_loop():
    while True:
        wait = 120
        try:
            if enabled():
                state = tick()
                wait = 30 if any(s.get("live", {}).get("state") == "in" for s in state.values()) else 60
        except Exception as e:  # le suivi ne doit jamais s'arrêter
            print("suivi direct :", e, file=sys.stderr)
        try:
            publish()
        except Exception as e:
            print("tuile en direct :", str(e)[:100], file=sys.stderr)
        try:
            tg_results()
        except Exception as e:
            print("résultats Telegram :", str(e)[:100], file=sys.stderr)
        time.sleep(wait)
