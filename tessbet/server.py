#!/usr/bin/env python3
"""Serveur de l'appli : sert la page web + l'API (paris du jour, suivi des mises, capteurs Home Assistant).

Lancer :  python3 server.py      puis ouvrir  http://<adresse>:8099
Python standard uniquement.
"""
import datetime as dt
import json
import os
import subprocess
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import engine

BASE = engine.BASE
DATA = engine.DATA
SEL_PATH = os.path.join(DATA, "selections.json")
BETS_PATH = os.path.join(DATA, "bets.json")
LOCK = threading.Lock()


def now_utc():
    return dt.datetime.now(dt.timezone.utc)


def today_paris():
    return engine.to_paris(now_utc()).date().isoformat()


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


def bet_profit(b):
    if b["result"] == "win":
        return round(b["stake"] * (b["odds"] - 1), 2)
    if b["result"] == "lose":
        return -b["stake"]
    return 0.0


BOOK_NAMES = {"winamax": "Winamax", "unibet": "Unibet", "betclic": "Betclic", "parionssport": "Parions Sport",
              "zebet": "Zebet", "pmu": "PMU", "netbet": "NetBet", "bwin": "Bwin", "vbet": "Vbet"}


def returned(b, result=None):
    """Argent rendu par le site pour ce pari selon son résultat (la mise est retirée du compte au moment du pari)."""
    r = result or b["result"]
    return b["stake"] * b["odds"] if r == "win" else b["stake"] if r == "void" else 0.0


def wallet_list(d):
    w = d.get("wallets") or {}
    return [{"key": k, "name": BOOK_NAMES.get(k, k), "balance": round(w[k], 2)} for k in w]


def compute_stats(d):
    cfg = engine.load_config()
    settled = [b for b in d["bets"] if b["result"] in ("win", "lose", "void")]
    profit = round(sum(bet_profit(b) for b in settled), 2)
    pending = round(sum(b["stake"] for b in d["bets"] if b["result"] == "pending"), 2)
    played = [b for b in settled if b["result"] in ("win", "lose")]
    staked_played = sum(b["stake"] for b in played)
    wallets = wallet_list(d)
    if wallets:  # l'argent réel sur les comptes des bookmakers (+ ce qui est en jeu) fait foi
        bankroll = round(sum(w["balance"] for w in wallets) + pending, 2)
    else:
        bankroll = round(d["bankroll_start"] + profit, 2)
    today = today_paris()
    staked_today = round(sum(b["stake"] for b in d["bets"] if b["date"] == today), 2)
    cap = round(cfg.get("daily_cap_pct", 0.05) * bankroll, 2)
    return {
        "bankroll_start": d["bankroll_start"], "bankroll": bankroll, "profit": profit, "pending": pending,
        "roi": round(profit / staked_played, 4) if staked_played else None,
        "win_rate": round(sum(1 for b in played if b["result"] == "win") / len(played), 4) if played else None,
        "n_bets": len(d["bets"]), "n_settled": len(played),
        "staked_today": staked_today, "daily_cap": cap, "daily_left": round(max(0, cap - staked_today), 2),
        "mode": cfg.get("mode", "demo"), "wallets": wallets,
        "other_books": [{"key": k, "name": n} for k, n in BOOK_NAMES.items() if k not in (d.get("wallets") or {})],
    }


def add_bet(body):
    try:
        odds, stake = float(body.get("odds")), float(body.get("stake"))
    except (TypeError, ValueError):
        return 400, {"error": "Cote ou mise invalide."}
    match, pick = str(body.get("match", ""))[:120], str(body.get("pick", ""))[:120]
    if not match or not pick or odds <= 1 or stake <= 0:
        return 400, {"error": "Il manque une information (match, pari, cote ou mise)."}
    book = str(body.get("book", "")).strip().lower()
    with LOCK:
        d = load_bets()
        st = compute_stats(d)
        wallets = d.get("wallets") or {}
        if book and book in wallets:
            if stake > wallets[book] + 1e-9:
                return 400, {"error": f"Il n'y a que {wallets[book]:.2f} € sur ton compte {BOOK_NAMES.get(book, book)}."}
        else:
            book = ""
            if stake > st["bankroll"] - st["pending"]:
                return 400, {"error": "Mise supérieure à l'argent disponible."}
        if st["staked_today"] + stake > st["daily_cap"] and not body.get("force"):
            return 409, {"error": f"Cette mise dépasse ta limite du jour ({st['daily_cap']:.2f} €). "
                                  "Tu l'as fixée pour te protéger.", "needs_force": True}
        bet = {"id": uuid.uuid4().hex[:8], "created": now_utc().strftime("%Y-%m-%dT%H:%M:%SZ"),
               "date": today_paris(), "match": match, "pick": pick, "odds": round(odds, 2),
               "stake": round(stake, 2), "sport": str(body.get("sport", ""))[:20], "result": "pending"}
        if book:
            bet["book"] = book
            wallets[book] = round(wallets[book] - bet["stake"], 2)
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
                w = d.get("wallets") or {}
                if b.get("book") in w:  # le compte suit : gain rendu, mise rendue si annulé
                    w[b["book"]] = round(w[b["book"]] + returned(b, result) - returned(b), 2)
                b["result"] = result
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
        w = d.get("wallets") or {}
        if gone and gone[0].get("book") in w:  # comme si le pari n'avait jamais existé
            w[gone[0]["book"]] = round(w[gone[0]["book"]] + gone[0]["stake"] - returned(gone[0]), 2)
        engine.write_json(BETS_PATH, d)
        return 200, {"ok": True}


def set_wallet(body):
    key = str(body.get("key", "")).strip().lower()
    if key not in BOOK_NAMES:
        return 400, {"error": "Bookmaker inconnu."}
    try:
        v = float(body.get("balance"))
    except (TypeError, ValueError):
        return 400, {"error": "Montant invalide."}
    if v < 0:
        return 400, {"error": "Le solde ne peut pas être négatif."}
    with LOCK:
        d = load_bets()
        d.setdefault("wallets", {})[key] = round(v, 2)
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
def get_today():
    d = read_json(SEL_PATH, None)
    if not d:
        return {"selections": [], "message": "Pas encore d'analyse. Appuie sur « Actualiser ».",
                "date": today_paris(), "mode": engine.load_config().get("mode", "demo"),
                "warnings": [], "disclaimer": engine.DISCLAIMER}
    return d


def run_analysis(force=False):
    cfg = engine.load_config()
    d = read_json(SEL_PATH, None)
    if d and not force and cfg.get("mode") != "demo":
        age = now_utc().timestamp() - engine.parse_iso(d["generated_at"]).timestamp()
        if age < 1800 and d.get("date") == today_paris() and d.get("mode") == cfg.get("mode"):
            return dict(d, throttled=True)
    p = subprocess.run([sys.executable, os.path.join(BASE, "engine.py")], capture_output=True, text=True, timeout=420,
                       env=dict(os.environ, TESSBET_NO_NOTIFY="1"))
    if p.returncode != 0:
        err = (p.stderr.strip().splitlines() or ["erreur inconnue"])[-1][:200]
        return {"error": f"L'analyse a échoué : {err}"}
    return get_today()


SETTINGS_PATH = os.path.join(DATA, "settings.json")


def get_settings():
    return {"auto": bool(read_json(SETTINGS_PATH, {}).get("auto", True))}


def set_settings(body):
    s = get_settings()
    if "auto" in body:
        s["auto"] = bool(body["auto"])
    engine.write_json(SETTINGS_PATH, s)
    return 200, s


def get_history(limit=60):
    folder = os.path.join(DATA, "history")
    days = []
    if os.path.isdir(folder):
        for name in sorted(os.listdir(folder), reverse=True)[:limit]:
            d = read_json(os.path.join(folder, name), None)
            if d:
                days.append(d)
    return {"days": days}


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
    """Lance l'analyse chaque jour à l'heure choisie (et au démarrage si rien n'existe pour aujourd'hui)."""
    last_run, tries, last_try = None, {}, 0
    while True:
        try:
            cfg = engine.load_config()
            if not get_settings()["auto"]:
                time.sleep(60)
                continue
            d = read_json(SEL_PATH, None)
            today = today_paris()
            hour = engine.to_paris(now_utc()).hour
            stale = not d or d.get("date") != today or d.get("mode") != cfg.get("mode")
            ready = hour >= cfg.get("auto_run_hour", 9) or cfg.get("mode") == "demo"
            if stale and ready and last_run != today:
                last_run = today  # un seul essai par jour : on ne martèle pas les API en cas d'erreur
                run_analysis(force=True)
            d = read_json(SEL_PATH, None)
            if (d and cfg.get("mode") == "live" and d.get("date") == today and tries.get(today, 0) < 6
                    and (not d.get("notification") or str(d["notification"]).startswith(("notification impossible", "pas encore")))
                    and time.time() - last_try > 300):
                last_try, tries[today] = time.time(), tries.get(today, 0) + 1  # HA peut ne pas être prêt au démarrage
                engine.notify_current()
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
                return self._send(200, get_today())
            if path == "/api/stats":
                return self._send(200, compute_stats(load_bets()))
            if path == "/api/bets":
                return self._send(200, {"bets": list(reversed(load_bets()["bets"]))})
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
                res = run_analysis(force=bool(body.get("force")))
                return self._send(500 if "error" in res else 200, res)
            if path == "/api/bets":
                return self._send(*add_bet(body))
            if path == "/api/settings":
                return self._send(*set_settings(body))
            if path == "/api/notify":
                st = engine.notify_current(force=True)
                return self._send(500 if st.startswith("notification impossible") else 200, {"status": st} if not st.startswith("notification impossible") else {"error": st})
            if path == "/api/bankroll":
                return self._send(*set_bankroll(body))
            if path == "/api/wallets":
                return self._send(*set_wallet(body))
            if path.startswith("/api/bets/") and path.endswith("/result"):
                return self._send(*set_result(path.split("/")[3], body.get("result")))
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
    print(f"Paris du jour : http://0.0.0.0:{port}  (mode {cfg.get('mode')})")
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()


if __name__ == "__main__":
    main()
