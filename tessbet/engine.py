#!/usr/bin/env python3
"""Moteur d'analyse des paris du jour.

Étapes : récupérer matchs + cotes -> estimer la vraie probabilité de chaque issue
(consensus des bookmakers, marge retirée) -> garder 0 à 3 paris solides -> écrire data/selections.json.

Python standard uniquement (aucune installation nécessaire).
"""
import datetime as dt
import hashlib
import json
import os
import random
import re
import sys
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from statistics import pstdev

BASE = os.path.dirname(os.path.abspath(__file__))
DATA = os.environ.get("TESSBET_DATA") or os.path.join(BASE, "data")
ODDS_URL = "https://api.the-odds-api.com/v4"
AF_URL = "https://v3.football.api-sports.io"

AF_LEAGUES = {
    "soccer_france_ligue_one": 61, "soccer_france_ligue_two": 62, "soccer_epl": 39,
    "soccer_spain_la_liga": 140, "soccer_germany_bundesliga": 78, "soccer_italy_serie_a": 135,
    "soccer_uefa_champs_league": 2, "soccer_uefa_europa_league": 3,
    "soccer_netherlands_eredivisie": 88, "soccer_portugal_primeira_liga": 94,
}

DISCLAIMER = ("Aucun pari n'est sûr. Même une probabilité de 75 % veut dire qu'on perd 1 fois sur 4. "
              "Ne mise que ce que tu peux perdre. Aide : Joueurs Info Service, 09 74 75 13 13.")


# ---------------------------------------------------------------- utilitaires
def load_env():
    env = {}
    path = os.path.join(BASE, ".env")
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    env[k.strip()] = v.strip()
    for k in ("ODDS_API_KEY", "API_FOOTBALL_KEY", "ANTHROPIC_API_KEY", "HA_URL", "HA_TOKEN"):
        if os.environ.get(k):
            env[k] = os.environ[k]
    return env


def load_config():
    with open(os.path.join(BASE, "config.json"), encoding="utf-8") as f:
        cfg = json.load(f)
    if os.environ.get("TESSBET_MODE") in ("demo", "live"):
        cfg["mode"] = os.environ["TESSBET_MODE"]
    if os.environ.get("TESSBET_NOTIFY"):
        cfg.setdefault("notify", {})["service"] = os.environ["TESSBET_NOTIFY"]
    return cfg


def write_json(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def _last_sunday(year, month):
    d = dt.datetime(year, month, 31, 1, tzinfo=dt.timezone.utc)
    return d - dt.timedelta(days=(d.weekday() + 1) % 7)


def to_paris(d_utc):
    try:
        from zoneinfo import ZoneInfo
        return d_utc.astimezone(ZoneInfo("Europe/Paris"))
    except Exception:  # pas de base de fuseaux : règle de l'heure d'été européenne
        summer = _last_sunday(d_utc.year, 3) <= d_utc < _last_sunday(d_utc.year, 10)
        return d_utc.astimezone(dt.timezone(dt.timedelta(hours=2 if summer else 1)))


def parse_iso(s):
    return dt.datetime.fromisoformat(s.replace("Z", "+00:00"))


def http_json(url, headers=None, timeout=25):
    req = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8")), dict(r.headers)


# ---------------------------------------------------------------- maths
def normalize(odds):
    """Cotes d'un bookmaker -> probabilités sans la marge (somme = 1)."""
    raw = [1.0 / o for o in odds]
    s = sum(raw)
    return [r / s for r in raw]


def kelly_stake_pct(prob, odds):
    """Mise conseillée en % de la bankroll : quart de Kelly, bornée entre 0,5 % et 2 %."""
    if odds <= 1:
        return 0.005
    kelly = (prob * odds - 1) / (odds - 1)
    return min(0.02, max(0.005, 0.25 * kelly))


def unaccent(s):
    return "".join(c for c in unicodedata.normalize("NFD", s) if unicodedata.category(c) != "Mn")


_STOP = {"fc", "cf", "afc", "sc", "ac", "ssc", "cd", "ud", "sd", "de", "la", "le", "1", "04", "05"}


def team_tokens(name):
    return {t for t in re.findall(r"[a-z0-9]+", unaccent(name).lower()) if t not in _STOP}


def team_match(a, b):
    ta, tb = team_tokens(a), team_tokens(b)
    if not ta or not tb:
        return False
    return ta <= tb or tb <= ta or len(ta & tb) / len(ta | tb) >= 0.5


# ---------------------------------------------------------------- analyse
def analyse_event(ev, cfg):
    """Retourne un candidat par issue (hors nul si exclu) avec probabilité estimée et meilleure cote."""
    sharp = cfg.get("sharp_books", {})
    playable = cfg.get("playable_books", [])
    names = [ev["home"], ev["away"]] + (["Draw"] if ev["sport"] == "football" else [])
    books = [b for b in ev["bookmakers"] if all(n in b["outcomes"] for n in names)]
    if len(books) < 2:
        return []
    probs = {n: [] for n in names}
    weights = []
    for b in books:
        p = normalize([b["outcomes"][n] for n in names])
        for n, v in zip(names, p):
            probs[n].append(v)
        weights.append(sharp.get(b["key"], 1))
    wsum = sum(weights)
    out = []
    for n in names:
        if n == "Draw" and cfg["selection"].get("exclude_draw", True):
            continue
        fair = sum(w * v for w, v in zip(weights, probs[n])) / wsum
        disp = pstdev(probs[n]) if len(probs[n]) > 1 else 0.0
        agree = 0
        for i in range(len(books)):
            if max(names, key=lambda m: probs[m][i]) == n:
                agree += 1
        offers = [(b["outcomes"][n], b["title"], b["key"]) for b in books]
        play = [o for o in offers if any(s in o[2] for s in playable)]
        pool = play if play else offers
        best = max(pool, key=lambda o: o[0])
        out.append({
            "event_id": ev["id"], "sport": ev["sport"], "league": ev["league"],
            "league_key": ev.get("league_key", ""), "commence": ev["commence"],
            "home": ev["home"], "away": ev["away"], "pick": n,
            "prob": fair, "dispersion": disp, "n_books": len(books),
            "agree": agree, "odds": best[0], "bookmaker": best[1],
            "playable": bool(play), "edge": fair * best[0] - 1,
        })
    return out


def finalize(c):
    """Calcule avantage, niveau de confiance, mise conseillée et explications."""
    c["edge"] = c["prob"] * c["odds"] - 1
    pts = 0
    pts += 2 if c["prob"] >= 0.75 else 1 if c["prob"] >= 0.68 else 0
    pts += 1 if c["n_books"] >= 6 else 0
    pts += 1 if c["dispersion"] <= 0.015 else 0
    pts += 1 if c["agree"] == c["n_books"] else 0
    pts += 1 if c.get("af_agrees") is True else 0
    c["confidence_score"] = pts
    c["confidence"] = "Élevée" if pts >= 4 else "Bonne" if pts >= 2 else "Moyenne"
    c["stake_pct"] = round(kelly_stake_pct(c["prob"], c["odds"]), 4)
    c["kind"] = "valeur" if c["edge"] > 0.005 else "favori"
    return c


def fr(x, decimals):
    """Nombre à la française : virgule décimale."""
    return f"{x:.{decimals}f}".replace(".", ",")


def explain(c):
    r = [f"Probabilité estimée : {round(c['prob'] * 100)} % (marge des bookmakers retirée)."]
    if c["agree"] == c["n_books"]:
        r.append(f"Les {c['n_books']} bookmakers analysés donnent le même favori.")
    else:
        r.append(f"{c['agree']} bookmakers sur {c['n_books']} donnent ce favori.")
    if c["dispersion"] <= 0.015:
        r.append("Les bookmakers sont très proches sur cette probabilité.")
    r.append(f"Meilleure cote : {fr(c['odds'], 2)} chez {c['bookmaker']}.")
    if c["kind"] == "valeur":
        r.append(f"Bonne affaire : cette cote est au-dessus de la valeur estimée "
                 f"(avantage {fr(c['edge'] * 100, 1)} %).")
    else:
        r.append(f"Pas de bonne affaire : la marge du bookmaker coûte environ {abs(c['edge']) * 100:.0f} % de la mise "
                 "en moyenne. C'est un favori très probable, mais sur la durée ce type de pari perd un peu.")
    if c.get("af_prob") is not None:
        r.append(("Les statistiques de forme (API-Football) vont dans le même sens : "
                  if c.get("af_agrees") else "Statistiques de forme (API-Football) : ")
                 + f"{round(c['af_prob'] * 100)} %.")
    if not c["playable"]:
        r.append("À vérifier : aucun bookmaker français trouvé dans les données reçues.")
    return r


def pick_label(c):
    return "Match nul" if c["pick"] == "Draw" else f"Victoire de {c['pick']}"


def select(cands, cfg, limit=None):
    s = cfg["selection"]
    limit = limit or s["max_picks"]
    ok = [c for c in cands
          if c.get("af_agrees") is not False and c["n_books"] >= s["min_books"]
          and c["prob"] >= s["min_prob"] and c["odds"] >= s["min_odds"] and c["edge"] >= s["min_edge"]]
    ok.sort(key=lambda c: (c["confidence_score"], c["prob"] - 2 * c["dispersion"] + 0.5 * max(c["edge"], 0)),
            reverse=True)
    picked, seen = [], set()
    for c in ok:
        if c["event_id"] not in seen:
            picked.append(c)
            seen.add(c["event_id"])
        if len(picked) >= limit:
            break
    return picked


# ---------------------------------------------------------------- Revue par Claude (recherche web)
ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
VERDICTS = ("confirme", "prudence", "ecarte")


def review_key(c):
    return f"{c['event_id']}|{c['pick']}"


def anthropic_post(payload, key):
    req = urllib.request.Request(
        ANTHROPIC_URL, data=json.dumps(payload).encode("utf-8"), method="POST",
        headers={"x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=240) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:150]
        raise RuntimeError(f"erreur {e.code} de l'API Claude : {detail}")


def build_review_prompt(pool, today):
    lines = []
    for c in pool:
        kick = to_paris(parse_iso(c["commence"])).strftime("%H:%M")
        lines.append(f"- id={review_key(c)} | {c['sport']} | {c['league']} | {c['home']} contre {c['away']} | "
                     f"pari : {pick_label(c)} | cote {c['odds']:.2f} | probabilité estimée d'après les cotes "
                     f"{round(c['prob'] * 100)} % | coup d'envoi {kick} (heure de Paris)")
    return f"""Nous sommes le {today.isoformat()}. Tu es l'analyste de Tessbet, une appli personnelle de paris sportifs.
Un moteur a présélectionné les paris ci-dessous d'après les cotes des bookmakers. Ton travail : vérifier sur le web
s'il existe une raison concrète de se méfier de chacun, puis rendre un verdict.

Paris à vérifier :
{chr(10).join(lines)}

Pour chaque pari, cherche des informations récentes (aujourd'hui et les derniers jours) :
- football : blessés et suspendus, compositions probables, rotation (matchs de coupe rapprochés), enjeu du match, météo
- basket : absents et repos de joueurs (surtout NBA), match à domicile ou à l'extérieur la veille, enjeu
- tennis : forme physique, blessure ou abandon récent, surface, fatigue après un long match, doute sur la participation
Regarde aussi si le match est maintenu.

Règles strictes :
- Tu ne proposes JAMAIS un nouveau pari. Tu ne peux que confirmer, recommander la prudence ou écarter ceux de la liste.
- Tu n'inventes rien. Si tu ne trouves pas d'information fiable et récente, ne dis pas "confirme" : réponds "prudence"
  et dis que l'information est introuvable.
- "ecarte" = raison claire et sourcée (joueur clé absent, doute sur la participation, équipe remaniée, match douteux).
- "confirme" = tu as cherché, rien d'inquiétant n'apparaît, et la situation sportive est cohérente avec le pari.
- Aucun pari n'est certain : ne promets jamais de gain et n'écris pas "sûr" ni "garanti".
- Écris en français simple, sans jargon, phrases courtes.

Réponds UNIQUEMENT avec un objet JSON, sans texte autour, de cette forme :
{{"verdicts": [{{"id": "<l'id exact>", "verdict": "confirme|prudence|ecarte",
  "resume": "1 ou 2 phrases claires", "points": ["fait vérifié 1", "fait vérifié 2"], "sources": ["https://..."]}}]}}"""


def _collect_urls(content, seen):
    for b in content:
        if b.get("type") == "web_search_tool_result" and isinstance(b.get("content"), list):
            seen.update(x["url"] for x in b["content"] if isinstance(x, dict) and x.get("url"))
        for cit in b.get("citations") or []:
            if isinstance(cit, dict) and cit.get("url"):
                seen.add(cit["url"])


def parse_review(text, pool, seen_urls):
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        raise RuntimeError("réponse de Claude illisible")
    data = json.loads(text[start:end + 1])
    wanted = {review_key(c) for c in pool}
    out = {}
    for v in data.get("verdicts", []):
        if not isinstance(v, dict) or v.get("id") not in wanted or v.get("verdict") not in VERDICTS:
            continue
        points = [str(p)[:220] for p in (v.get("points") or []) if p][:4]
        sources = [u for u in (v.get("sources") or []) if isinstance(u, str) and u.startswith("http") and u in seen_urls][:3]
        out[v["id"]] = {"verdict": v["verdict"], "resume": str(v.get("resume", ""))[:420],
                        "points": points, "sources": sources}
    return out


def claude_review(pool, cfg, env, today):
    """Demande à Claude (avec recherche web) de vérifier chaque pari. Retourne {clé: verdict}."""
    key = env.get("ANTHROPIC_API_KEY")
    if not key:
        raise RuntimeError("clé ANTHROPIC_API_KEY manquante dans .env")
    cl = cfg["claude"]
    cache_path = os.path.join(DATA, f"review_{today.isoformat()}.json")
    cache = {}
    if os.path.exists(cache_path):
        with open(cache_path, encoding="utf-8") as f:
            cache = json.load(f)
    todo = [c for c in pool if review_key(c) not in cache]
    if todo:
        messages = [{"role": "user", "content": build_review_prompt(todo, today)}]
        payload = {"model": cl["model"], "max_tokens": cl["max_tokens"], "messages": messages,
                   "tools": [{"type": "web_search_20250305", "name": "web_search", "max_uses": cl["max_searches"],
                              "user_location": {"type": "approximate", "country": "FR", "timezone": "Europe/Paris"}}]}
        seen, text = set(), ""
        for _ in range(4):  # "pause_turn" : Claude demande à poursuivre sa recherche
            resp = anthropic_post(payload, key)
            _collect_urls(resp.get("content", []), seen)
            text = "".join(b.get("text", "") for b in resp.get("content", []) if b.get("type") == "text")
            if resp.get("stop_reason") != "pause_turn":
                break
            messages.append({"role": "assistant", "content": resp["content"]})
        cache.update(parse_review(text, todo, seen))
        write_json(cache_path, cache)
    return {k: v for k, v in cache.items() if k in {review_key(c) for c in pool}}


def demo_review(pool):
    """Verdicts fictifs pour le mode démo (aucun appel à Claude)."""
    canned = {
        "Aigles de Lune": ("confirme", "Aucune absence annoncée côté favori, l'adversaire joue la veille.",
                           ["Effectif complet (exemple fictif)", "Adversaire en déplacement après un match la veille (exemple fictif)"]),
        "Real Montagne": ("prudence", "Le favori pourrait faire tourner son équipe avant un gros match.",
                          ["Match important prévu dans 3 jours (exemple fictif)", "Composition pas encore connue (exemple fictif)"]),
        "FC Océan": ("confirme", "Pas de blessé important, équipe en forme à domicile.",
                     ["4 victoires sur les 5 derniers matchs à domicile (exemple fictif)"]),
        "A. Verdier": ("ecarte", "Doute sur sa forme physique après une gêne signalée hier.",
                       ["Entraînement écourté (exemple fictif)"]),
    }
    out = {}
    for c in pool:
        v, resume, pts = canned.get(c["pick"], ("confirme", "Rien d'inquiétant trouvé (exemple fictif).", []))
        out[review_key(c)] = {"verdict": v, "resume": resume, "points": pts, "sources": [], "demo": True}
    return out


def review_and_pick(cands, cfg, env, today, mode, warnings):
    """Présélection -> vérification par Claude -> choix final. Claude peut écarter, jamais ajouter."""
    cl = cfg.get("claude", {})
    pool = select(cands, cfg, limit=cl.get("max_review", 6))
    info = {"reviewed": len(pool), "dropped": 0, "unavailable": False}
    if not pool:
        return [], info
    verdicts = None
    if mode == "demo":
        verdicts = demo_review(pool)
    elif cl.get("enabled", True):
        try:
            verdicts = claude_review(pool, cfg, env, today)
        except Exception as e:
            warnings.append(f"Vérification par Claude impossible : {str(e)[:140]}")
    if verdicts is None:
        info["unavailable"] = True
        if cl.get("required", True):
            return [], info
        return pool[:cfg["selection"]["max_picks"]], info
    final = []
    for c in pool:
        v = verdicts.get(review_key(c))
        if not v or v["verdict"] == "ecarte":
            info["dropped"] += 1
            continue
        c["claude"] = v
        if v["verdict"] == "prudence":
            c["confidence"] = "Moyenne"
        final.append(c)
    final.sort(key=lambda c: c["claude"]["verdict"] != "confirme")  # tri stable : les « confirmé » d'abord
    return final[:cfg["selection"]["max_picks"]], info


# ---------------------------------------------------------------- API-Football (forme / avis statistique)
def af_get(path, params, key):
    url = f"{AF_URL}{path}?{urllib.parse.urlencode(params)}"
    data, _ = http_json(url, {"x-apisports-key": key})
    if data.get("errors"):
        raise RuntimeError(str(data["errors"])[:150])
    return data.get("response", [])


def enrich_football(cands, cfg, env, today, warnings):
    key = env.get("API_FOOTBALL_KEY")
    af = cfg.get("api_football", {})
    if not key or not af.get("enabled"):
        return
    season = today.year if today.month >= 7 else today.year - 1
    cache, calls = {}, 0
    for c in cands:
        lid = AF_LEAGUES.get(c["league_key"])
        if c["sport"] != "football" or not lid:
            continue
        try:
            if lid not in cache:
                if calls >= af["max_calls"]:
                    break
                cache[lid] = af_get("/fixtures", {"date": today.isoformat(), "league": lid, "season": season}, key)
                calls += 1
            fx = next((f for f in cache[lid]
                       if team_match(f["teams"]["home"]["name"], c["home"])
                       and team_match(f["teams"]["away"]["name"], c["away"])), None)
            if not fx or calls >= af["max_calls"]:
                continue
            pred = af_get("/predictions", {"fixture": fx["fixture"]["id"]}, key)
            calls += 1
            pct = pred[0]["predictions"]["percent"]
            m = {k: int(str(v).strip("%")) / 100 for k, v in pct.items()}
            side = "home" if c["pick"] == c["home"] else "away"
            c["af_prob"] = m[side]
            c["af_agrees"] = max(m, key=m.get) == side
            w = af.get("blend_weight", 0.2)
            c["prob"] = (1 - w) * c["prob"] + w * m[side]
        except Exception as e:  # la forme est un bonus : on n'arrête jamais l'analyse pour ça
            warnings.append(f"Forme indisponible pour {c['home']} - {c['away']} ({str(e)[:60]})")


# ---------------------------------------------------------------- récupération des cotes
def sport_of(key):
    return "football" if key.startswith("soccer") else "basketball" if key.startswith("basketball") else "tennis"


def convert_event(e, key, title):
    books = []
    for b in e.get("bookmakers", []):
        for m in b.get("markets", []):
            if m["key"] == "h2h":
                books.append({"key": b["key"], "title": b["title"],
                              "outcomes": {o["name"]: o["price"] for o in m["outcomes"]}})
    return {"id": e["id"], "sport": sport_of(key), "league": title, "league_key": key,
            "commence": e["commence_time"], "home": e["home_team"], "away": e["away_team"], "bookmakers": books}


def fetch_live_events(cfg, env, now, warnings):
    key = env.get("ODDS_API_KEY")
    if not key:
        raise RuntimeError("Clé ODDS_API_KEY manquante dans le fichier .env")
    sports, _ = http_json(f"{ODDS_URL}/sports/?apiKey={key}")
    active = {s["key"]: s["title"] for s in sports if s.get("active")}
    caps = cfg["max_calls_per_sport"]
    keys = [k for k in cfg["leagues"]["football"] if k in active][:caps["football"]]
    keys += [k for k in cfg["leagues"]["basketball"] if k in active][:caps["basketball"]]
    keys += [k for k in active if k.startswith("tennis_")][:caps["tennis"]]
    paris_now = to_paris(now)
    end = (paris_now + dt.timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    fmt = "%Y-%m-%dT%H:%M:%SZ"
    events, quota = [], None
    for k in keys:
        cache_path = os.path.join(DATA, f"cache_{k}.json")
        raw = None
        if os.path.exists(cache_path):
            age = now.timestamp() - os.path.getmtime(cache_path)
            if age < cfg["cache_hours"] * 3600:
                with open(cache_path, encoding="utf-8") as f:
                    raw = json.load(f)
        if raw is None:
            q = urllib.parse.urlencode({
                "apiKey": key, "regions": cfg["regions"], "markets": "h2h", "oddsFormat": "decimal",
                "dateFormat": "iso", "commenceTimeFrom": now.strftime(fmt),
                "commenceTimeTo": end.astimezone(dt.timezone.utc).strftime(fmt)})
            try:
                raw, headers = http_json(f"{ODDS_URL}/sports/{k}/odds?{q}")
                quota = headers.get("x-requests-remaining", headers.get("X-Requests-Remaining", quota))
                write_json(cache_path, raw)
            except Exception as e:
                warnings.append(f"{k} : {str(e)[:80]}")
                continue
        events += [convert_event(e, k, active[k]) for e in raw]
    return events, quota


# ---------------------------------------------------------------- mode démo
def demo_events(now):
    today = to_paris(now).date()
    rnd = random.Random(int(hashlib.md5(today.isoformat().encode()).hexdigest()[:8], 16))
    books = [("pinnacle", "Pinnacle", 0.025), ("betclic_fr", "Betclic", 0.06), ("winamax_fr", "Winamax", 0.055),
             ("unibet_fr", "Unibet", 0.06), ("bet365", "Bet365", 0.05), ("bwin", "Bwin", 0.065),
             ("williamhill", "William Hill", 0.07), ("marathonbet", "Marathonbet", 0.045)]
    fixtures = [
        ("football", "Ligue démo", "FC Océan", "AS Rivage", (0.68, 0.19, 0.13), 19),
        ("football", "Ligue démo", "Olympique Falaise", "SC Bruyère", (0.44, 0.28, 0.28), 21),
        ("football", "Coupe démo", "Real Montagne", "CF Plaine", (0.74, 0.16, 0.10), 21),
        ("football", "Ligue démo", "US Marais", "FC Vallon", (0.36, 0.30, 0.34), 17),
        ("basketball", "Basket démo", "Aigles de Lune", "Loups du Nord", (0.78, 0.22), 20),
        ("basketball", "Basket démo", "Titans Est", "Comètes Ouest", (0.52, 0.48), 22),
        ("tennis", "Tournoi démo", "A. Verdier", "L. Castel", (0.71, 0.29), 14),
        ("tennis", "Tournoi démo", "M. Orsini", "J. Keller", (0.55, 0.45), 16),
    ]
    boosts = {0: "winamax_fr", 4: "betclic_fr"}  # une cote un peu généreuse sur 2 matchs (« bonne affaire »)
    events = []
    for i, (sport, league, home, away, tp, hour) in enumerate(fixtures):
        names = [home, away] + (["Draw"] if sport == "football" else [])
        bl = []
        for key, title, margin in books:
            p = [max(0.02, x + rnd.uniform(-0.018, 0.018)) for x in tp]
            s = sum(p)
            odds = [round(1 / (x / s * (1 + margin)), 2) for x in p]
            if boosts.get(i) == key:
                odds[0] = round(odds[0] * 1.09, 2)
            bl.append({"key": key, "title": title, "outcomes": dict(zip(names, odds))})
        commence = to_paris(dt.datetime(today.year, today.month, today.day, 12, tzinfo=dt.timezone.utc)).replace(
            hour=hour, minute=0).astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        events.append({"id": f"demo{i}", "sport": sport, "league": league, "league_key": "demo",
                       "commence": commence, "home": home, "away": away, "bookmakers": bl})
    return events


# ---------------------------------------------------------------- notification (téléphone uniquement)
def ha_post(url, token, payload):
    req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"), method="POST",
                                 headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json",
                                          "User-Agent": "Mozilla/5.0 (Tessbet)", "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"Home Assistant a répondu {e.code}")


def notification_text(result):
    sel = result["selections"]
    if not sel:
        return "Tessbet : pas de pari aujourd'hui", result["message"]
    lines = []
    for s in sel:
        verdict = "Claude confirme" if s["claude"] and s["claude"]["verdict"] == "confirme" else "prudence"
        if not s["claude"]:
            verdict = "non vérifié"
        lines.append(f"{s['kickoff_local']} {s['match']} : {s['pick_label']} @ {fr(s['odds'], 2)} "
                     f"({round(s['prob'] * 100)} %, {verdict})")
    title = f"Tessbet : {len(sel)} pari{'s' if len(sel) > 1 else ''} du jour"
    return title, "\n".join(lines)


def notify_phone(result, cfg, env, force=False):
    """Envoie les paris du jour sur le téléphone, et nulle part ailleurs. Retourne un court statut."""
    n = cfg.get("notify", {})
    if not n.get("enabled", False):
        return "notification désactivée"
    if result["mode"] == "demo" and not n.get("in_demo", False) and not force:
        return "pas de notification en mode démo"
    service = n.get("service", "")
    if not service.startswith("mobile_app_"):
        raise RuntimeError("le service de notification doit être un téléphone (mobile_app_...)")
    base, token = env.get("HA_URL", "").rstrip("/"), env.get("HA_TOKEN", "")
    if not base or not token:
        raise RuntimeError("HA_URL ou HA_TOKEN manquant dans .env")
    if not result["selections"] and not n.get("when_empty", True) and not force:
        return "rien à signaler"
    title, message = notification_text(result)
    state_path = os.path.join(DATA, "notified.json")
    digest = hashlib.md5((result["date"] + message).encode()).hexdigest()
    if not force and os.path.exists(state_path):
        with open(state_path, encoding="utf-8") as f:
            if json.load(f).get("digest") == digest:
                return "déjà notifié"
    data = {"tag": "tessbet"}
    if n.get("app_url"):
        data["url"] = n["app_url"]
    ha_post(f"{base}/api/services/notify/{service}", token, {"title": title, "message": message, "data": data})
    write_json(state_path, {"digest": digest, "date": result["date"]})
    return "notification envoyée sur le téléphone"


# ---------------------------------------------------------------- orchestration
def build_message(n, analysed, info, cfg):
    if info["unavailable"] and cfg.get("claude", {}).get("required", True):
        return "Claude n'a pas pu vérifier les paris aujourd'hui : par sécurité, Tessbet n'en propose aucun."
    dropped = info["dropped"]
    tail = f" Claude en a écarté {dropped}." if dropped else ""
    if n == 0:
        return "Aucun pari assez solide aujourd'hui." + tail + " Ne pas parier est aussi une bonne décision."
    if n < cfg["selection"]["max_picks"]:
        return f"Seulement {n} pari{'s' if n > 1 else ''} passe{'nt' if n > 1 else ''} les critères et la vérification de Claude." + tail
    return f"{n} paris retenus sur {analysed} matchs analysés, vérifiés par Claude." + tail


def run(mode=None):
    cfg, env = load_config(), load_env()
    mode = mode or cfg.get("mode", "demo")
    now = dt.datetime.now(dt.timezone.utc)
    today = to_paris(now).date()
    warnings, quota = [], None

    if mode == "demo":
        events = demo_events(now)
    else:
        events, quota = fetch_live_events(cfg, env, now, warnings)
        events = [e for e in events
                  if now + dt.timedelta(minutes=10) <= parse_iso(e["commence"])
                  and to_paris(parse_iso(e["commence"])).date() == today]

    cands = []
    for ev in events:
        cands += analyse_event(ev, cfg)
    cands = [finalize(c) for c in cands]
    shortlist = sorted(cands, key=lambda c: c["prob"], reverse=True)[:12]
    shortlist = [c for c in shortlist if c["prob"] >= cfg["selection"]["min_prob"] - 0.05]
    if mode != "demo":
        enrich_football(shortlist, cfg, env, today, warnings)  # modifie les candidats en place
        cands = [finalize(c) for c in cands]
    picks, info = review_and_pick(cands, cfg, env, today, mode, warnings)

    selections = []
    for c in picks:
        kick = to_paris(parse_iso(c["commence"]))
        selections.append({
            "id": hashlib.md5(f"{c['event_id']}{c['pick']}".encode()).hexdigest()[:10],
            "sport": c["sport"], "league": c["league"], "kickoff": c["commence"],
            "kickoff_local": kick.strftime("%H:%M"), "match": f"{c['home']} – {c['away']}",
            "pick": c["pick"], "pick_label": pick_label(c), "odds": round(c["odds"], 2),
            "bookmaker": c["bookmaker"], "prob": round(c["prob"], 4), "edge": round(c["edge"], 4),
            "kind": c["kind"], "confidence": c["confidence"], "confidence_score": c["confidence_score"],
            "stake_pct": c["stake_pct"], "n_books": c["n_books"], "reasons": explain(c),
            "claude": c.get("claude")})

    analysed = len({c["event_id"] for c in cands})
    msg = build_message(len(selections), analysed, info, cfg)

    result = {"generated_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"), "date": today.isoformat(), "mode": mode,
              "matches_analysed": analysed, "selections": selections, "claude_dropped": info["dropped"],
              "message": msg, "warnings": warnings, "quota_odds_api_remaining": quota,
              "disclaimer": DISCLAIMER}
    write_json(os.path.join(DATA, "selections.json"), result)
    if mode != "demo":  # on garde chaque jour pour pouvoir revoir les paris donnés
        write_json(os.path.join(DATA, "history", f"{today.isoformat()}.json"), result)
    try:
        result["notification"] = notify_phone(result, cfg, env)
    except Exception as e:  # une notification ratée ne doit jamais bloquer l'analyse
        result["notification"] = f"notification impossible : {str(e)[:100]}"
    return result


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "notify-test":
        cfg, env = load_config(), load_env()
        n = cfg["notify"]
        print("Envoi d'un message de test vers :", n.get("service"))
        if not n.get("service", "").startswith("mobile_app_"):
            sys.exit("Le service doit être un téléphone (mobile_app_...).")
        ha_post(f"{env['HA_URL'].rstrip('/')}/api/services/notify/{n['service']}", env["HA_TOKEN"],
                {"title": "Tessbet", "message": "Test réussi : Tessbet est relié à ton téléphone.",
                 "data": {"tag": "tessbet-test"}})
        print("Message de test envoyé.")
        sys.exit(0)
    mode_arg = sys.argv[1] if len(sys.argv) > 1 else None
    res = run(mode_arg)
    print(f"[{res['mode']}] {res['message']}")
    for s in res["selections"]:
        print(f"  {s['kickoff_local']} {s['match']} -> {s['pick_label']} @ {s['odds']} "
              f"({round(s['prob'] * 100)} %, {s['confidence']})")
    for w in res["warnings"]:
        print("  ! " + w)
    print("  notification :", res.get("notification", "aucune"))
