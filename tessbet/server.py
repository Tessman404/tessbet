#!/usr/bin/env python3
"""Serveur de l'appli : sert la page web + l'API (paris du jour, suivi des mises, capteurs Home Assistant).

Lancer :  python3 server.py      puis ouvrir  http://<adresse>:8099
Python standard uniquement.
"""
import datetime as dt
import json
import math
import os
import subprocess
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import engine
import live

BASE = engine.BASE
DATA = engine.DATA
SEL_PATH = os.path.join(DATA, "selections.json")
BETS_PATH = os.path.join(DATA, "bets.json")
LOCK = threading.Lock()


def now_utc():
    return dt.datetime.now(dt.timezone.utc)


def today_paris():
    return engine.to_paris(now_utc()).date().isoformat()


def day_date(day):
    """« today » ou « tomorrow » -> date ISO à Paris."""
    base = engine.to_paris(now_utc()).date()
    return (base + dt.timedelta(days=1 if day == "tomorrow" else 0)).isoformat()


def read_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


# ---------------------------------------------------------------- suivi des mises
def load_bets():
    cfg = engine.load_config()
    d = read_json(BETS_PATH, None)
    if not d:
        d = {"bankroll_start": float(cfg.get("bankroll_start", 100)), "bets": []}
    return d


def eff_odds(b):
    """Cote réellement payée : dans un combiné, un match annulé compte pour 1 (le site recalcule la cote sans lui)."""
    void = [l["odds"] for l in b.get("legs") or [] if l.get("result") == "void"]
    return max(1.0, round(b["odds"] / math.prod(void), 2)) if void else b["odds"]


def combo_result(legs):
    """Résultat d'un combiné d'après ses matchs : perdu dès qu'un match est perdu, gagné quand tous sont validés."""
    rs = [l.get("result", "pending") for l in legs]
    if "lose" in rs:
        return "lose"
    if "pending" in rs:
        return "pending"
    return "void" if all(r == "void" for r in rs) else "win"


def boost_of(b):
    """Boost du site en % (ex. Winamax +10 %) : il augmente le gain net d'un pari gagné."""
    try:
        return max(0.0, min(200.0, float(b.get("boost") or 0)))
    except (TypeError, ValueError):
        return 0.0


def win_profit(b):
    """Gain net si le pari est gagné : mise × (cote − 1), augmenté du boost."""
    return round(b["stake"] * (eff_odds(b) - 1) * (1 + boost_of(b) / 100), 2)


def clean_boost(v):
    try:
        v = float(str(v).replace(",", ".")) if v not in (None, "") else 0.0
    except ValueError:
        return None
    return round(v, 2) if 0 <= v <= 200 else None


def bet_profit(b):
    if b["result"] == "win":
        return win_profit(b)
    if b["result"] == "lose":
        return -b["stake"]
    return 0.0


BOOK_NAMES = {"winamax": "Winamax", "unibet": "Unibet", "betclic": "Betclic", "parionssport": "Parions Sport",
              "zebet": "Zebet", "pmu": "PMU", "netbet": "NetBet", "bwin": "Bwin", "vbet": "Vbet"}


def returned(b, result=None):
    """Argent rendu par le site pour ce pari selon son résultat (la mise est retirée du compte au moment du pari)."""
    r = result or b["result"]
    return b["stake"] + win_profit(b) if r == "win" else b["stake"] if r == "void" else 0.0


def get_cap_setting():
    c = read_json(SETTINGS_PATH, {}).get("cap")
    return float(c) if isinstance(c, (int, float)) and c > 0 else None


def _ts(s):
    try:
        return dt.datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def book_balances(bets, moves=()):
    """Solde de chaque site, sans compter deux fois l'argent rejoué.

    On rejoue les mouvements dans l'ordre : une mise prend d'abord l'argent déjà sur le site (gains compris) ;
    s'il en manque, c'est un dépôt. Un pari terminé rend son argent environ 3 h après son dernier coup d'envoi."""
    events = []
    for b in bets:
        t0 = _ts(b.get("created")) or 0
        events.append((t0, 0, b))
        if b["result"] in ("win", "lose", "void"):
            kicks = [_ts(x.get("kickoff")) for x in (b.get("legs") or [b])]
            kicks = [k for k in kicks if k]
            settle = max(kicks + [t0]) + 3 * 3600
            if _ts(b.get("settled_at")):  # l'heure où le résultat a été enregistré, quand on la connaît
                settle = min(settle, _ts(b["settled_at"]))
            events.append((settle, 1, b))
    for m in moves:
        events.append((_ts(m.get("created")) or 0, 2, m))
    books = {}

    def book(k):
        return books.setdefault(k, {"key": k, "name": BOOK_NAMES.get(k, "Non précisé"), "n": 0, "staked": 0.0, "profit": 0.0,
                                    "won": 0, "deposit": 0.0, "added": 0.0, "withdrawn": 0.0, "cash": 0.0, "pending": 0.0})

    def take(x, amount):  # sortir de l'argent du site ; s'il en manque, il y avait forcément été déposé avant
        if x["cash"] < amount:
            x["deposit"] = round(x["deposit"] + amount - x["cash"], 2)
            x["cash"] = 0.0
        else:
            x["cash"] = round(x["cash"] - amount, 2)

    for t, kind, b in sorted(events, key=lambda e: (e[0], e[1])):
        if kind == 2:  # mouvement d'argent : dépôt, retrait ou transfert entre deux sites
            amt = b["amount"]
            if b["type"] in ("withdraw", "transfer"):
                x = book(b["from"])
                take(x, amt)
                x["withdrawn"] = round(x["withdrawn"] + amt, 2)
            if b["type"] in ("deposit", "transfer"):
                x = book(b["to"])
                x["cash"] = round(x["cash"] + amt, 2)
                x["added"] = round(x["added"] + amt, 2)
            continue
        k = b.get("book") or ""
        x = book(k)
        if kind == 0:
            x["n"] += 1
            x["staked"] = round(x["staked"] + b["stake"], 2)
            take(x, b["stake"])
            if b["result"] == "pending":
                x["pending"] = round(x["pending"] + b["stake"], 2)
        else:
            x["cash"] = round(x["cash"] + returned(b), 2)
            x["profit"] = round(x["profit"] + bet_profit(b), 2)
            x["won"] += b["result"] == "win"
    for x in books.values():
        x["balance"] = x.pop("cash")  # argent disponible sur le site (hors mises en cours)
    return books


def compute_stats(d):
    cfg = engine.load_config()
    bets = sorted(d["bets"], key=lambda b: b.get("created", ""))
    settled = [b for b in bets if b["result"] in ("win", "lose", "void")]
    profit = round(sum(bet_profit(b) for b in settled), 2)
    pending = round(sum(b["stake"] for b in bets if b["result"] == "pending"), 2)
    played = [b for b in settled if b["result"] in ("win", "lose")]
    staked_played = sum(b["stake"] for b in played)
    moves = sorted(d.get("moves", []), key=lambda m: m.get("created", ""))
    added = round(sum(m["amount"] for m in moves if m["type"] == "deposit"), 2)      # argent ajouté depuis la banque
    withdrawn = round(sum(m["amount"] for m in moves if m["type"] == "withdraw"), 2)  # argent retiré vers la banque
    bankroll = round(d["bankroll_start"] + added - withdrawn + profit, 2)
    today = today_paris()
    staked_today = round(sum(b["stake"] for b in bets if b["date"] == today), 2)
    manual = get_cap_setting()
    cap = round(manual if manual else cfg.get("daily_cap_pct", 0.05) * bankroll, 2)
    # évolution : argent total après chaque pari terminé, et résultat par jour
    run, series, by_day = d["bankroll_start"], [], {}
    timeline = [(b.get("created", ""), "bet", b) for b in settled] + [(m.get("created", ""), "move", m) for m in moves if m["type"] != "transfer"]
    for _, kind, b in sorted(timeline, key=lambda x: x[0]):
        if kind == "move":
            delta = b["amount"] if b["type"] == "deposit" else -b["amount"]
            run = round(run + delta, 2)
            series.append({"date": b["created"][:10], "label": ("Dépôt " if delta > 0 else "Retrait ") + BOOK_NAMES.get(b.get("to") or b.get("from"), ""),
                           "profit": 0.0, "move": delta, "bankroll": run})
            continue
        p = bet_profit(b)
        run = round(run + p, 2)
        series.append({"date": b["date"], "label": b["pick"], "profit": p, "bankroll": run})
        day = by_day.setdefault(b["date"], {"date": b["date"], "n": 0, "staked": 0.0, "profit": 0.0})
        day["n"] += 1; day["staked"] = round(day["staked"] + b["stake"], 2); day["profit"] = round(day["profit"] + p, 2)
    # répartition par bookmaker : argent déposé (estimé), en jeu, gagné ou perdu, et solde sur le site
    books = book_balances(bets, moves)
    by_book = sorted(books.values(), key=lambda x: -x["balance"])
    unplaced = round(d["bankroll_start"] - sum(x["deposit"] for x in by_book), 2)
    return {
        "unplaced": unplaced, "added": added, "withdrawn": withdrawn, "invested": round(d["bankroll_start"] + added, 2),
        "moves": [dict(m, from_name=BOOK_NAMES.get(m.get("from"), ""), to_name=BOOK_NAMES.get(m.get("to"), "")) for m in reversed(moves)],
        "bankroll_start": d["bankroll_start"], "bankroll": bankroll, "profit": profit, "pending": pending,
        "roi": round(profit / staked_played, 4) if staked_played else None,
        "win_rate": round(sum(1 for b in played if b["result"] == "win") / len(played), 4) if played else None,
        "n_bets": len(bets), "n_settled": len(played),
        "staked_today": staked_today, "daily_cap": cap, "daily_left": round(max(0, cap - staked_today), 2),
        "cap_manual": manual, "mode": cfg.get("mode", "demo"),
        "series": series, "by_day": sorted(by_day.values(), key=lambda x: x["date"], reverse=True), "by_book": by_book,
        "books": [{"key": k, "name": n} for k, n in BOOK_NAMES.items()],
    }


def clean_leg(raw):
    """Un match d'un combiné (ou d'un pari simple), nettoyé ; None s'il est inutilisable."""
    if not isinstance(raw, dict):
        return None
    match, pick = str(raw.get("match", "")).strip()[:120], str(raw.get("pick", "")).strip()[:120]
    try:
        odds = float(raw.get("odds"))
    except (TypeError, ValueError):
        return None
    if not match or not pick or not 1 < odds < 1000:
        return None
    leg = {"match": match, "pick": pick, "odds": round(odds, 2), "result": "pending"}
    if raw.get("sport"):
        leg["sport"] = str(raw["sport"])[:20]
    for k in ("league_key", "pick_team", "kickoff"):
        if raw.get(k):
            leg[k] = str(raw[k])[:80]
    return leg


MAX_LEGS = 12


def add_bet(body):
    """Enregistre un pari simple, ou un combiné si « legs » est donné (2 à 12 matchs, une seule cote et une seule mise)."""
    try:
        stake = float(body.get("stake"))
    except (TypeError, ValueError):
        return 400, {"error": "Mise invalide."}
    extra = {}
    if body.get("legs") is not None:
        legs = [clean_leg(x) for x in body["legs"]] if isinstance(body["legs"], list) else []
        if not 2 <= len(legs) <= MAX_LEGS or any(l is None for l in legs):
            return 400, {"error": f"Un combiné compte de 2 à {MAX_LEGS} matchs, chacun avec son match, son pari et sa cote."}
        product = round(math.prod(l["odds"] for l in legs), 2)
        try:
            odds = float(body.get("odds") or product)  # la cote du site peut différer légèrement du produit
        except (TypeError, ValueError):
            return 400, {"error": "Cote du combiné invalide."}
        match, pick = f"Combiné ({len(legs)} matchs)", " + ".join(l["pick"] for l in legs)[:120]
        extra = {"legs": legs, "sport": "combine"}
    else:
        try:
            odds = float(body.get("odds"))
        except (TypeError, ValueError):
            return 400, {"error": "Cote ou mise invalide."}
        match, pick = str(body.get("match", ""))[:120], str(body.get("pick", ""))[:120]
        extra = {"sport": str(body.get("sport", ""))[:20]}
        for k in ("league_key", "pick_team", "kickoff"):
            if body.get(k):
                extra[k] = str(body[k])[:80]
    if not match or not pick or odds <= 1 or stake <= 0:
        return 400, {"error": "Il manque une information (match, pari, cote ou mise)."}
    book = str(body.get("book", "")).strip().lower()
    book = book if book in BOOK_NAMES else ""
    boost = clean_boost(body.get("boost"))
    if boost is None:
        return 400, {"error": "Boost invalide (entre 0 et 200 %)."}
    if boost:
        extra["boost"] = boost
    with LOCK:
        d = load_bets()
        st = compute_stats(d)
        if stake > st["bankroll"] - st["pending"] + 1e-9:
            return 400, {"error": "Mise supérieure à l'argent disponible."}
        if st["staked_today"] + stake > st["daily_cap"] and not body.get("force"):
            return 409, {"error": f"Cette mise dépasse ta limite du jour ({st['daily_cap']:.2f} €). "
                                  "Tu l'as fixée pour te protéger.", "needs_force": True}
        bet = {"id": uuid.uuid4().hex[:8], "created": now_utc().strftime("%Y-%m-%dT%H:%M:%SZ"),
               "date": today_paris(), "match": match, "pick": pick, "odds": round(odds, 2),
               "stake": round(stake, 2), "result": "pending", **extra}
        if book:
            bet["book"] = book
        d["bets"].append(bet)
        engine.write_json(BETS_PATH, d)
        return 200, {"bet": bet}


def set_result(bet_id, result):
    if result not in ("win", "lose", "void", "pending"):
        return 400, {"error": "Résultat inconnu."}
    with LOCK:
        d = load_bets()
        for b in d["bets"]:
            if b["id"] == bet_id:
                b["result"] = result
                b["settled_at"] = now_utc().strftime("%Y-%m-%dT%H:%M:%SZ")
                for leg in b.get("legs") or []:  # résultat global d'un combiné : les matchs pas encore tranchés suivent
                    if result == "pending" or (leg.get("result", "pending") == "pending" and result in ("win", "void")):
                        leg["result"] = result
                engine.write_json(BETS_PATH, d)
                return 200, {"bet": b}
    return 404, {"error": "Pari introuvable."}


def set_leg_result(bet_id, idx, result):
    """Résultat d'un seul match d'un combiné ; le résultat du combiné s'en déduit."""
    if result not in ("win", "lose", "void", "pending"):
        return 400, {"error": "Résultat inconnu."}
    with LOCK:
        d = load_bets()
        for b in d["bets"]:
            if b["id"] != bet_id:
                continue
            legs = b.get("legs") or []
            if not 0 <= idx < len(legs):
                return 404, {"error": "Match introuvable dans ce combiné."}
            legs[idx]["result"] = result
            b["result"] = combo_result(legs)
            b["settled_at"] = now_utc().strftime("%Y-%m-%dT%H:%M:%SZ")
            engine.write_json(BETS_PATH, d)
            return 200, {"bet": b}
    return 404, {"error": "Pari introuvable."}


def update_bet(bet_id, body):
    """Corrige la cote et/ou la mise d'un pari ; le compte du bookmaker suit."""
    with LOCK:
        d = load_bets()
        for b in d["bets"]:
            if b["id"] != bet_id:
                continue
            try:
                odds = float(body.get("odds", b["odds"]))
                stake = float(body.get("stake", b["stake"]))
            except (TypeError, ValueError):
                return 400, {"error": "Cote ou mise invalide."}
            if odds <= 1 or stake <= 0:
                return 400, {"error": "La cote doit dépasser 1 et la mise être positive."}
            if "boost" in body:
                boost = clean_boost(body["boost"])
                if boost is None:
                    return 400, {"error": "Boost invalide (entre 0 et 200 %)."}
                if boost:
                    b["boost"] = boost
                else:
                    b.pop("boost", None)
            b.update(odds=round(odds, 2), stake=round(stake, 2))
            if "book" in body:
                k = str(body["book"]).strip().lower()
                if k in BOOK_NAMES:
                    b["book"] = k
                else:
                    b.pop("book", None)
            engine.write_json(BETS_PATH, d)
            return 200, {"bet": b}
    return 404, {"error": "Pari introuvable."}


def delete_bet(bet_id):
    with LOCK:
        d = load_bets()
        n = len(d["bets"])
        gone = [b for b in d["bets"] if b["id"] == bet_id]
        d["bets"] = [b for b in d["bets"] if b["id"] != bet_id]
        if len(d["bets"]) == n:
            return 404, {"error": "Pari introuvable."}
        engine.write_json(BETS_PATH, d)
        return 200, {"ok": True}


def add_move(body):
    """Retrait vers la banque, dépôt depuis la banque, ou transfert d'un site à un autre."""
    typ = body.get("type")
    if typ not in ("deposit", "withdraw", "transfer"):
        return 400, {"error": "Choisis : retrait, dépôt ou transfert."}
    try:
        amount = round(float(str(body.get("amount")).replace(",", ".")), 2)
    except (TypeError, ValueError):
        return 400, {"error": "Montant invalide."}
    if amount <= 0:
        return 400, {"error": "Le montant doit être positif."}
    src, dst = str(body.get("from") or "").lower(), str(body.get("to") or "").lower()
    if typ in ("withdraw", "transfer") and src not in BOOK_NAMES:
        return 400, {"error": "Choisis le site d'où part l'argent."}
    if typ in ("deposit", "transfer") and dst not in BOOK_NAMES:
        return 400, {"error": "Choisis le site où arrive l'argent."}
    if typ == "transfer" and src == dst:
        return 400, {"error": "Choisis deux sites différents."}
    with LOCK:
        d = load_bets()
        if typ != "deposit" and not body.get("force"):
            have = book_balances(d["bets"], d.get("moves", [])).get(src, {}).get("balance", 0.0)
            if amount > have + 1e-9:
                return 409, {"error": f"D'après Tessbet, il n'y a que {have:.2f} € disponibles sur {BOOK_NAMES[src]}.".replace(".", ",", 1),
                             "needs_force": True}
        m = {"id": uuid.uuid4().hex[:8], "created": now_utc().strftime("%Y-%m-%dT%H:%M:%SZ"), "type": typ, "amount": amount}
        if typ != "deposit":
            m["from"] = src
        if typ != "withdraw":
            m["to"] = dst
        d.setdefault("moves", []).append(m)
        engine.write_json(BETS_PATH, d)
        return 200, {"move": m}


def delete_move(move_id):
    with LOCK:
        d = load_bets()
        n = len(d.get("moves", []))
        d["moves"] = [m for m in d.get("moves", []) if m["id"] != move_id]
        if len(d["moves"]) == n:
            return 404, {"error": "Mouvement introuvable."}
        engine.write_json(BETS_PATH, d)
        return 200, {"ok": True}


def set_bankroll(body):
    try:
        v = float(body.get("bankroll_start"))
    except (TypeError, ValueError):
        return 400, {"error": "Montant invalide."}
    if v <= 0:
        return 400, {"error": "Le montant doit être positif."}
    with LOCK:
        d = load_bets()
        d["bankroll_start"] = round(v, 2)
        engine.write_json(BETS_PATH, d)
    return 200, {"ok": True}


# ---------------------------------------------------------------- analyse
def read_result(date_iso):
    """L'analyse d'un jour précis (aujourd'hui ou demain), ou None si elle n'a pas été faite."""
    cfg = engine.load_config()
    d = read_json(engine.result_path(cfg.get("mode", "demo"), date_iso), None)
    last = read_json(SEL_PATH, None)  # « la dernière » : même contenu pour aujourd'hui, mais plus complète sur les anciennes données
    if last and last.get("date") == date_iso and last.get("mode") == cfg.get("mode") \
            and (not d or last.get("generated_at", "") >= d.get("generated_at", "")):
        d = last
    return d


def get_today(day="today"):
    date = day_date(day)
    d = read_result(date)
    if not d:
        return {"selections": [], "message": "Pas encore d'analyse. Appuie sur « Lancer l'analyse ».",
                "date": date, "mode": engine.load_config().get("mode", "demo"),
                "warnings": [], "disclaimer": engine.DISCLAIMER}
    return d


def run_analysis(force=False, source=None, refire=False, day="today"):
    cfg = engine.load_config()
    d = read_result(day_date(day))
    if d and not force and cfg.get("mode") != "demo":
        age = now_utc().timestamp() - engine.parse_iso(d["generated_at"]).timestamp()
        if age < 1800 and d.get("mode") == cfg.get("mode"):
            return dict(d, throttled=True)
    p = subprocess.run([sys.executable, os.path.join(BASE, "engine.py")], capture_output=True, text=True, timeout=420,
                       env=dict(os.environ, TESSBET_NO_NOTIFY="1", **({"TESSBET_SOURCE": source} if source else {}),
                                **({"TESSBET_REFIRE": "1"} if refire else {}),
                                **({"TESSBET_DAY": "tomorrow"} if day == "tomorrow" else {})))
    if p.returncode != 0:
        err = (p.stderr.strip().splitlines() or ["erreur inconnue"])[-1][:200]
        return {"error": f"L'analyse a échoué : {err}"}
    return get_today(day)


SETTINGS_PATH = os.path.join(DATA, "settings.json")


def get_settings():
    s = read_json(SETTINGS_PATH, {})
    hour = s.get("hour")
    if not isinstance(hour, int) or not 0 <= hour <= 23:
        hour = int(engine.load_config().get("auto_run_hour", 9))
    return {"auto": bool(s.get("auto", True)), "live": bool(s.get("live", True)), "hour": hour,
            "horizon": 48 if s.get("horizon") == 48 else 24,  # 24 h = aujourd'hui, 48 h = aujourd'hui + demain
            "cap": get_cap_setting(), "asked": s.get("asked"),
            "source": engine.load_config().get("claude", {}).get("source", "api"),
            "routine": bool(os.environ.get("ROUTINE_ID") and os.environ.get("ROUTINE_TOKEN"))}


def set_settings(body):
    s = get_settings()
    if "auto" in body:
        s["auto"] = bool(body["auto"])
    if "live" in body:
        s["live"] = bool(body["live"])
    if "horizon" in body:
        if body["horizon"] not in (24, 48):
            return 400, {"error": "Choisis 24 h ou 48 h."}
        s["horizon"] = body["horizon"]
    if "hour" in body:
        try:
            h = int(body["hour"])
        except (TypeError, ValueError):
            return 400, {"error": "Heure invalide."}
        if not 5 <= h <= 14:
            return 400, {"error": "Choisis une heure entre 5 h et 14 h."}
        s["hour"] = h
    if "cap" in body:
        raw = body["cap"]
        if raw in (None, "", 0):
            s["cap"] = None
        else:
            try:
                c = float(raw)
            except (TypeError, ValueError):
                return 400, {"error": "Montant invalide."}
            if c <= 0:
                return 400, {"error": "La limite doit être positive."}
            s["cap"] = round(c, 2)
    engine.write_json(SETTINGS_PATH, s)
    return 200, s


def get_history(limit=60):
    folder = os.path.join(DATA, "history")
    days = []
    if os.path.isdir(folder):
        today = today_paris()
        for name in [n for n in sorted(os.listdir(folder), reverse=True) if n[:10] <= today][:limit]:  # pas les jours à venir
            d = read_json(os.path.join(folder, name), None)
            if d:
                days.append(d)
    return {"days": days}


def bets_payload():
    """Les paris avec leur état en direct ; un combiné porte l'état de chacun de ses matchs."""
    lvs = read_json(os.path.join(DATA, "live.json"), {})
    out = []
    for b in reversed(load_bets()["bets"]):
        if b.get("legs"):
            legs = []
            for i, leg in enumerate(b["legs"]):
                lv = lvs.get(f"{b['id']}:{i}", {}).get("live")
                one = dict(leg, live=lv)
                one["status"] = live.bet_status({"match": leg["match"], "pick": leg["pick"], "pick_team": leg.get("pick_team")}, lv)
                legs.append(one)
            out.append(dict(b, legs=legs, status=live.combo_status(legs), eff_odds=eff_odds(b), win_profit=win_profit(b)))
        else:
            lv = lvs.get(b["id"], {}).get("live")
            out.append(dict(b, live=lv, status=live.bet_status(b, lv), eff_odds=eff_odds(b), win_profit=win_profit(b)))
    return out


def official_picks():
    """Les paris proposés par Tessbet pour aujourd'hui et demain : (match, issue) -> vrai."""
    out = set()
    for day in ("today", "tomorrow"):
        d = read_result(day_date(day)) or {}
        for sel in d.get("selections", []):
            out.add((sel.get("match"), sel.get("pick_label")))
    return out


def refresh_cost(cfg):
    """Crédits d'API qu'une mise à jour des cotes devrait coûter : un par championnat déjà relevé."""
    tag = engine._odds_tag(cfg)
    n = len([f for f in os.listdir(DATA) if f.startswith("cache_") and f.endswith(f"_{tag}.json")]) if os.path.isdir(DATA) else 0
    return n or 7


def matches_payload():
    """Tous les matchs d'aujourd'hui et de demain avec leurs cotes, tirés de la mémoire (sans crédit)."""
    cfg, now = engine.load_config(), now_utc()
    if cfg.get("mode") == "demo":
        events = engine.demo_events(now) + engine.demo_events(now, engine.paris_date(now) + dt.timedelta(days=1))
        newest = now.timestamp()
    else:
        events, newest = engine.cached_events(cfg)
    rows = engine.match_list(events, cfg, now)
    official = official_picks()
    for r in rows:
        for o in r["outcomes"]:
            o["official"] = (f"{r['home']} – {r['away']}", o["label"]) in official
    leagues = len({r["league_key"] or r["league"] for r in rows})
    return {"matches": rows, "mode": cfg.get("mode"), "updated_at": dt.datetime.fromtimestamp(newest, dt.timezone.utc).isoformat() if newest else None,
            "refresh_cost": refresh_cost(cfg) if cfg.get("mode") == "live" else 0, "leagues": leagues}


def refresh_matches(body):
    """Met à jour les cotes de tous les matchs (utilise des crédits de l'API de cotes : demande une confirmation)."""
    cfg = engine.load_config()
    if cfg.get("mode") != "live":
        return 400, {"error": "Le mode démo n'utilise pas de vraies cotes."}
    if not body.get("confirm"):
        return 400, {"error": "La mise à jour des cotes coûte des crédits : confirmation nécessaire."}
    warnings = []
    try:
        _, quota = engine.fetch_live_events(cfg, engine.load_env(), now_utc(), warnings, force=True)
    except Exception as e:
        return 400, {"error": "Mise à jour impossible : " + str(e)[:120] + "."}
    out = matches_payload()
    out.update(quota=quota, warnings=warnings)
    return 200, out


def ha_payload():
    d = get_today()
    picks = [f"{s['match']} : {s['pick_label']} @ {s['odds']:.2f}".replace(".", ",") + f" ({round(s['prob'] * 100)} %)"
             for s in d["selections"]]
    st = compute_stats(load_bets())
    return {"date": d.get("date"), "count": len(picks), "picks": picks, "message": d.get("message", ""),
            "summary": "\n".join(f"{i + 1}. {p}" for i, p in enumerate(picks)) or d.get("message", ""),
            "bankroll": st["bankroll"], "profit": st["profit"], "daily_left": st["daily_left"],
            "mode": d.get("mode")}


def scheduler():
    """Rappel du matin, réponse de Claude à récupérer, notification : pour aujourd'hui et pour demain."""
    last_run, tries, last_try, last_wait = None, {}, {}, {}
    while True:
        try:
            cfg = engine.load_config()
            if not get_settings()["auto"]:
                time.sleep(60)
                continue
            today = today_paris()
            hour = engine.to_paris(now_utc()).hour
            stale = not read_result(today)
            ready = hour >= get_settings()["hour"] or cfg.get("mode") == "demo"
            if stale and ready and last_run != today and cfg.get("mode") == "live":
                last_run = today  # une seule demande par jour ; l'analyse ne part jamais sans ton accord
                if get_settings().get("asked") != today:
                    try:
                        engine.send_phone("Tessbet : analyse du jour",
                                          "Veux-tu lancer l'analyse du jour ? Ouvre Tessbet et confirme (Claude ou API).", "tessbet-ask")
                    except Exception as ex:
                        print("rappel :", str(ex)[:100], file=sys.stderr)
                    s2 = read_json(SETTINGS_PATH, {})
                    s2["asked"] = today
                    engine.write_json(SETTINGS_PATH, s2)
            for offset, day in ((0, "today"), (1, "tomorrow")):
                date = day_date(day)
                d = read_result(date)
                if d and d.get("waiting") and time.time() - last_wait.get(date, 0) > 180:
                    last_wait[date] = time.time()  # mode « tâche Claude » : on regarde si la réponse est arrivée
                    if engine.task_fetch_answer(dt.date.fromisoformat(date), engine.load_env()):
                        run_analysis(force=True, day=day)
                    d = read_result(date)
                if (d and not d.get("waiting") and cfg.get("mode") == "live" and tries.get(date, 0) < 6
                        and (not d.get("notification") or str(d["notification"]).startswith(("notification impossible", "pas encore")))
                        and time.time() - last_try.get(date, 0) > 300):
                    last_try[date], tries[date] = time.time(), tries.get(date, 0) + 1  # HA peut ne pas être prêt au démarrage
                    engine.notify_current(offset=offset)
        except Exception as e:  # le planificateur ne doit jamais s'arrêter
            print("planificateur :", e, file=sys.stderr)
        time.sleep(60)


# ---------------------------------------------------------------- HTTP
class Handler(BaseHTTPRequestHandler):
    server_version = "ParisSportifs/1.0"

    def log_message(self, fmt, *args):
        pass

    def _send(self, code, obj, ctype="application/json; charset=utf-8"):
        body = obj if isinstance(obj, bytes) else json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n > 10_000:
            return {}
        try:
            return json.loads(self.rfile.read(n) or b"{}")
        except ValueError:
            return {}

    def _route(self, method):
        path = self.path.split("?")[0].rstrip("/") or "/"
        if method == "GET":
            if path == "/":
                with open(os.path.join(BASE, "web", "index.html"), "rb") as f:
                    return self._send(200, f.read(), "text/html; charset=utf-8")
            if path == "/api/today":
                q = parse_qs(urlparse(self.path).query)
                return self._send(200, get_today("tomorrow" if q.get("day", [""])[0] == "tomorrow" else "today"))
            if path == "/api/stats":
                return self._send(200, compute_stats(load_bets()))
            if path == "/api/bets":
                return self._send(200, {"bets": bets_payload()})
            if path == "/api/matches":
                return self._send(200, matches_payload())
            if path == "/api/settings":
                return self._send(200, get_settings())
            if path == "/api/bookmakers":  # diagnostic : quels bookmakers l'API renvoie vraiment
                keys = set()
                for name in os.listdir(DATA) if os.path.isdir(DATA) else []:
                    if name.startswith("cache_"):
                        for ev in read_json(os.path.join(DATA, name), []) or []:
                            keys.update(b.get("key") for b in ev.get("bookmakers", []))
                return self._send(200, {"keys": sorted(k for k in keys if k)})
            if path == "/api/history":
                return self._send(200, get_history())
            if path == "/api/ha":
                return self._send(200, ha_payload())
        elif method == "POST":
            body = self._body()
            if path == "/api/refresh":
                src = body.get("source")
                if src not in ("api", "claude_task"):
                    return self._send(400, {"error": "Choisis d'abord comment lancer l'analyse (Claude ou API)."})
                if src == "api" and not body.get("confirm"):
                    return self._send(400, {"error": "L'analyse par l'API coûte des crédits : confirmation nécessaire."})
                res = run_analysis(force=bool(body.get("force")), source=src, refire=bool(body.get("refire")) and src == "claude_task",
                                   day="tomorrow" if body.get("day") == "tomorrow" else "today")
                return self._send(400 if "error" in res else 200, res)  # pas de 5xx : les proxys remplacent le message
            if path == "/api/bets":
                return self._send(*add_bet(body))
            if path == "/api/matches/refresh":
                return self._send(*refresh_matches(body))
            if path == "/api/settings":
                return self._send(*set_settings(body))
            if path == "/api/routine/test":
                res = engine.routine_test(engine.load_env())
                if res["status"] == "manual":
                    return self._send(400, {"error": "Le jeton de la tâche Claude n'est pas réglé dans les réglages de l'application."})
                if res["status"] == "error":
                    return self._send(400, {"error": "Claude n'a pas pu être réveillé : " + res["error"] + "."})  # pas de 5xx : les proxys remplacent le message
                return self._send(200, res)
            if path == "/api/notify":
                st = engine.notify_current(force=True)
                return self._send(500 if st.startswith("notification impossible") else 200, {"status": st} if not st.startswith("notification impossible") else {"error": st})
            if path == "/api/moves":
                return self._send(*add_move(body))
            if path == "/api/bankroll":
                return self._send(*set_bankroll(body))
            if path.startswith("/api/bets/") and path.count("/") == 5 and path.split("/")[4] == "legs" and path.split("/")[5].isdigit():
                return self._send(*set_leg_result(path.split("/")[3], int(path.split("/")[5]), body.get("result")))
            if path.startswith("/api/bets/") and path.count("/") == 3:
                return self._send(*update_bet(path.split("/")[3], body))
            if path.startswith("/api/bets/") and path.endswith("/result"):
                return self._send(*set_result(path.split("/")[3], body.get("result")))
        elif method == "DELETE" and path.startswith("/api/moves/"):
            return self._send(*delete_move(path.split("/")[3]))
        elif method == "DELETE" and path.startswith("/api/bets/"):
            return self._send(*delete_bet(path.split("/")[3]))
        self._send(404, {"error": "Introuvable."})

    def do_GET(self):
        self._route("GET")

    def do_POST(self):
        self._route("POST")

    def do_DELETE(self):
        self._route("DELETE")


def main():
    cfg = engine.load_config()
    port = int(os.environ.get("PORT", cfg.get("port", 8099)))
    threading.Thread(target=scheduler, daemon=True).start()
    import live  # suivi des buts en direct
    threading.Thread(target=live.run_loop, daemon=True).start()
    print(f"Paris du jour : http://0.0.0.0:{port}  (mode {cfg.get('mode')})")
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()


if __name__ == "__main__":
    main()
