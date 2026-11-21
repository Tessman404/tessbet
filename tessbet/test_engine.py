import os
"""Tests des calculs. Lancer :  python3 -m unittest -v test_engine"""
import datetime as dt
import unittest

import engine


def make_event(prob, margin=0.05, n_books=6, sport="tennis", boost=1.0):
    names = ["A", "B"] if sport != "football" else ["A", "B", "Draw"]
    books = []
    for i in range(n_books):
        odds = [round(1 / (p * (1 + margin)), 2) for p in prob]
        if i == 0:
            odds[0] = round(odds[0] * boost, 2)
        books.append({"key": "winamax_fr" if i == 0 else f"book{i}", "title": "Winamax" if i == 0 else f"Book{i}", "outcomes": dict(zip(names, odds))})
    return {"id": "e1", "sport": sport, "league": "L", "league_key": "x", "commence": "2026-10-24T18:00:00Z",
            "home": "A", "away": "B", "bookmakers": books}


class TestMaths(unittest.TestCase):
    def test_normalize_sums_to_one(self):
        p = engine.normalize([1.5, 2.6])
        self.assertAlmostEqual(sum(p), 1.0)
        self.assertGreater(p[0], p[1])

    def test_margin_removed(self):
        # cotes avec 5 % de marge sur 70/30 -> on doit retrouver ~70/30
        p = engine.normalize([1 / (0.7 * 1.05), 1 / (0.3 * 1.05)])
        self.assertAlmostEqual(p[0], 0.7, places=3)

    def test_stake_bounds(self):
        self.assertEqual(engine.kelly_stake_pct(0.5, 1.5), 0.005)   # pas d'avantage -> plancher
        self.assertEqual(engine.kelly_stake_pct(0.9, 3.0), 0.02)    # gros avantage -> plafond
        self.assertEqual(engine.kelly_stake_pct(0.7, 1.0), 0.005)   # cote invalide -> plancher

    def test_team_match(self):
        self.assertTrue(engine.team_match("Olympique de Marseille", "Marseille"))
        self.assertTrue(engine.team_match("Paris Saint-Germain", "Paris Saint Germain"))
        self.assertFalse(engine.team_match("Manchester United", "Manchester City"))
        self.assertFalse(engine.team_match("", "Lyon"))


class TestAnalyse(unittest.TestCase):
    def setUp(self):
        self.cfg = engine.load_config()

    def test_fair_probability_recovered(self):
        cands = engine.analyse_event(make_event((0.7, 0.3)), self.cfg)
        a = next(c for c in cands if c["pick"] == "A")
        self.assertAlmostEqual(a["prob"], 0.7, delta=0.01)
        self.assertEqual(a["agree"], a["n_books"])

    def test_boosted_odds_give_positive_edge(self):
        c = engine.finalize(next(c for c in engine.analyse_event(make_event((0.7, 0.3), boost=1.12), self.cfg)
                                 if c["pick"] == "A"))
        self.assertGreater(c["edge"], 0.02)
        self.assertEqual(c["kind"], "valeur")

    def test_plain_odds_are_favori(self):
        c = engine.finalize(next(c for c in engine.analyse_event(make_event((0.7, 0.3)), self.cfg) if c["pick"] == "A"))
        self.assertLess(c["edge"], 0)
        self.assertEqual(c["kind"], "favori")

    def test_draw_excluded_by_default(self):
        cands = engine.analyse_event(make_event((0.5, 0.25, 0.25), sport="football"), self.cfg)
        self.assertNotIn("Draw", [c["pick"] for c in cands])

    def test_too_few_books_not_selected(self):
        cands = [engine.finalize(c) for c in engine.analyse_event(make_event((0.8, 0.2), n_books=3, boost=1.2), self.cfg)]
        self.assertEqual(engine.select(cands, self.cfg), [])

    def test_never_more_than_three_and_one_per_match(self):
        cands = []
        for i in range(6):
            ev = make_event((0.8, 0.2), boost=1.1)
            ev["id"] = f"e{i}"
            cands += [engine.finalize(c) for c in engine.analyse_event(ev, self.cfg)]
        picks = engine.select(cands, self.cfg)
        self.assertLessEqual(len(picks), 3)
        self.assertEqual(len({p["event_id"] for p in picks}), len(picks))

    def test_disagreeing_form_data_blocks_pick(self):
        c = engine.finalize(next(c for c in engine.analyse_event(make_event((0.8, 0.2), boost=1.1), self.cfg)
                                 if c["pick"] == "A"))
        c["af_agrees"] = False
        self.assertEqual(engine.select([c], self.cfg), [])

    def test_no_pick_below_probability_threshold(self):
        cands = [engine.finalize(c) for c in engine.analyse_event(make_event((0.55, 0.45), boost=1.2), self.cfg)]
        self.assertEqual(engine.select(cands, self.cfg), [])


def fake_resp(text, urls=(), stop="end_turn"):
    content = [{"type": "web_search_tool_result",
                "content": [{"type": "web_search_result", "url": u} for u in urls]},
               {"type": "text", "text": text}]
    return {"content": content, "stop_reason": stop}


class TestClaudeReview(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.cfg = engine.load_config()
        self.today = dt.date(2026, 10, 24)
        self.tmp = tempfile.mkdtemp()
        self._old = (engine.DATA, engine.anthropic_post)
        engine.DATA = self.tmp
        cands = []
        for i in range(3):
            ev = make_event((0.8, 0.2), boost=1.1)
            ev["id"] = f"e{i}"
            cands += [engine.finalize(c) for c in engine.analyse_event(ev, self.cfg) if c["pick"] == "A"]
        self.cands = cands
        self.env = {"ANTHROPIC_API_KEY": "test"}

    def tearDown(self):
        engine.DATA, engine.anthropic_post = self._old

    def verdict_json(self, items):
        import json
        return json.dumps({"verdicts": [dict(id=i, verdict=v, resume="ok", points=["p"], sources=s) for i, v, s in items]})

    def test_missing_key_blocks_everything_when_required(self):
        picks, info = engine.review_and_pick(self.cands, self.cfg, {}, self.today, "live", [])
        self.assertEqual(picks, [])
        self.assertTrue(info["unavailable"])

    def test_missing_key_allows_unverified_when_not_required(self):
        cfg = dict(self.cfg, claude=dict(self.cfg["claude"], required=False))
        picks, info = engine.review_and_pick(self.cands, cfg, {}, self.today, "live", [])
        self.assertEqual(len(picks), 3)
        self.assertTrue(all("claude" not in p for p in picks))

    def test_api_error_blocks_when_required(self):
        def boom(payload, key):
            raise RuntimeError("erreur 529")
        engine.anthropic_post = boom
        warnings = []
        picks, info = engine.review_and_pick(self.cands, self.cfg, self.env, self.today, "live", warnings)
        self.assertEqual(picks, [])
        self.assertTrue(warnings)

    def test_ecarte_removed_and_prudence_downgraded(self):
        txt = self.verdict_json([("e0|A", "confirme", ["https://ok.fr/a"]), ("e1|A", "ecarte", []),
                                 ("e2|A", "prudence", [])])
        engine.anthropic_post = lambda p, k: fake_resp(txt, ["https://ok.fr/a"])
        picks, info = engine.review_and_pick(self.cands, self.cfg, self.env, self.today, "live", [])
        self.assertEqual([p["event_id"] for p in picks], ["e0", "e2"])
        self.assertEqual(picks[1]["confidence"], "Moyenne")
        self.assertEqual(info["dropped"], 1)

    def test_no_verdict_means_not_proposed(self):
        txt = self.verdict_json([("e0|A", "confirme", [])])
        engine.anthropic_post = lambda p, k: fake_resp(txt)
        picks, _ = engine.review_and_pick(self.cands, self.cfg, self.env, self.today, "live", [])
        self.assertEqual([p["event_id"] for p in picks], ["e0"])

    def test_claude_cannot_add_unknown_bet(self):
        txt = self.verdict_json([("zzz|Inconnu", "confirme", []), ("e0|A", "confirme", [])])
        engine.anthropic_post = lambda p, k: fake_resp(txt)
        picks, _ = engine.review_and_pick(self.cands, self.cfg, self.env, self.today, "live", [])
        self.assertEqual([p["event_id"] for p in picks], ["e0"])

    def test_invented_sources_are_dropped(self):
        txt = self.verdict_json([("e0|A", "confirme", ["https://vrai.fr/x", "https://invente.fr/y"])])
        engine.anthropic_post = lambda p, k: fake_resp(txt, ["https://vrai.fr/x"])
        picks, _ = engine.review_and_pick(self.cands, self.cfg, self.env, self.today, "live", [])
        self.assertEqual(picks[0]["claude"]["sources"], ["https://vrai.fr/x"])

    def test_json_inside_code_fence_is_parsed(self):
        txt = "```json\n" + self.verdict_json([("e0|A", "confirme", [])]) + "\n```"
        engine.anthropic_post = lambda p, k: fake_resp(txt)
        picks, _ = engine.review_and_pick(self.cands, self.cfg, self.env, self.today, "live", [])
        self.assertEqual(len(picks), 1)

    def test_pause_turn_continues_and_cache_avoids_second_call(self):
        calls = []
        txt = self.verdict_json([(f"e{i}|A", "confirme", []) for i in range(3)])

        def fake(payload, key):
            calls.append(1)
            return fake_resp("", [], "pause_turn") if len(calls) == 1 else fake_resp(txt)
        engine.anthropic_post = fake
        engine.review_and_pick(self.cands, self.cfg, self.env, self.today, "live", [])
        self.assertEqual(len(calls), 5)  # 2 pour la vérification (pause_turn) + 1 analyse écrite par pari
        picks, _ = engine.review_and_pick(self.cands, self.cfg, self.env, self.today, "live", [])
        self.assertEqual(len(calls), 5)  # relance du jour : tout vient du cache
        self.assertEqual(len(picks), 3)
        self.assertTrue(all(p["claude"].get("analyse") for p in picks))

    def test_written_analysis_failure_keeps_the_pick(self):
        txt = self.verdict_json([("e0|A", "confirme", [])])
        n = []

        def fake(payload, key):
            n.append(1)
            if len(n) > 1:
                raise RuntimeError("panne")
            return fake_resp(txt)
        engine.anthropic_post = fake
        warnings = []
        picks, _ = engine.review_and_pick(self.cands, self.cfg, self.env, self.today, "live", warnings)
        self.assertEqual(len(picks), 1)
        self.assertFalse(picks[0]["claude"].get("analyse"))
        self.assertTrue(any("Analyse écrite impossible" in w for w in warnings))

    def test_garbage_answer_is_an_error_not_a_pick(self):
        engine.anthropic_post = lambda p, k: fake_resp("désolé, je ne peux pas")
        picks, info = engine.review_and_pick(self.cands, self.cfg, self.env, self.today, "live", [])
        self.assertEqual(picks, [])
        self.assertTrue(info["unavailable"])


class TestNotify(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.cfg = engine.load_config()
        self.env = {"HA_URL": "https://ha.example/", "HA_TOKEN": "t"}
        self.sent = []
        self._old = (engine.DATA, engine.ha_post)
        engine.DATA = tempfile.mkdtemp()
        engine.ha_post = lambda url, token, payload: self.sent.append((url, payload))
        self.result = {"mode": "live", "date": "2026-10-24", "message": "ok", "selections": [{
            "kickoff_local": "20:00", "match": "A – B", "pick_label": "Victoire de A", "odds": 1.3,
            "prob": 0.78, "claude": {"verdict": "confirme"}}]}

    def tearDown(self):
        engine.DATA, engine.ha_post = self._old

    def test_goes_to_the_phone_only(self):
        self.assertIn("envoyée", engine.notify_phone(self.result, self.cfg, self.env))
        url, payload = self.sent[0]
        self.assertEqual(url, "https://ha.example/api/services/notify/mobile_app_iphone_de_thomas")
        self.assertIn("Victoire de A @ 1,30", payload["message"])
        self.assertEqual(len(self.sent), 1)

    def test_refuses_non_phone_service(self):
        cfg = dict(self.cfg, notify=dict(self.cfg["notify"], service="persistent_notification"))
        with self.assertRaises(RuntimeError):
            engine.notify_phone(self.result, cfg, self.env)
        self.assertEqual(self.sent, [])

    def test_no_notification_in_demo(self):
        res = dict(self.result, mode="demo")
        self.assertIn("démo", engine.notify_phone(res, self.cfg, self.env))
        self.assertEqual(self.sent, [])

    def test_same_analysis_notified_once(self):
        engine.notify_phone(self.result, self.cfg, self.env)
        self.assertEqual(engine.notify_phone(self.result, self.cfg, self.env), "déjà notifié")
        self.assertEqual(len(self.sent), 1)

    def test_missing_token_is_an_error(self):
        with self.assertRaises(RuntimeError):
            engine.notify_phone(self.result, self.cfg, {"HA_URL": "https://ha.example"})

    def test_empty_day_still_tells_you(self):
        res = dict(self.result, selections=[], message="Aucun pari assez solide aujourd'hui.")
        engine.notify_phone(res, self.cfg, self.env)
        self.assertIn("pas de pari", self.sent[0][1]["title"])


class TestRoutineLaunch(unittest.TestCase):
    """Réveil de la tâche Claude depuis l'appli (déclencheur API), sans jamais appeler le vrai service."""

    class FakeResp:
        def __init__(self, body): self._b = body
        def read(self): return self._b
        def __enter__(self): return self
        def __exit__(self, *a): return False

    def setUp(self):
        import tempfile, os
        self.today = dt.date(2026, 10, 25)
        self.tmp = tempfile.mkdtemp()
        self._old = (engine.DATA, engine.urllib.request.urlopen)
        engine.DATA = self.tmp
        os.environ.pop("TESSBET_REFIRE", None)
        self.env = {"ROUTINE_ID": "trig_TEST", "ROUTINE_TOKEN": "sk-ant-oat01-secret"}
        self.calls = []

    def tearDown(self):
        import os
        engine.DATA, engine.urllib.request.urlopen = self._old
        os.environ.pop("TESSBET_REFIRE", None)

    def ok(self, req, timeout=0):
        self.calls.append(req)
        return self.FakeResp(b'{"type":"routine_fire","claude_code_session_id":"s1","claude_code_session_url":"https://claude.ai/code/s1"}')

    def test_not_configured_is_manual(self):
        engine.urllib.request.urlopen = self.ok
        self.assertEqual(engine.routine_launch(self.today, {})["status"], "manual")
        self.assertEqual(engine.routine_launch(self.today, {"ROUTINE_ID": "x"})["status"], "manual")
        self.assertEqual(self.calls, [])

    def test_fire_once_per_day_and_request_format(self):
        engine.urllib.request.urlopen = self.ok
        r = engine.routine_launch(self.today, self.env)
        self.assertEqual(r["status"], "fired")
        self.assertEqual(r["session_url"], "https://claude.ai/code/s1")
        req = self.calls[0]
        self.assertTrue(req.full_url.endswith("/v1/claude_code/routines/trig_TEST/fire"))
        self.assertEqual(req.get_method(), "POST")
        self.assertEqual(req.get_header("Authorization"), "Bearer sk-ant-oat01-secret")
        self.assertEqual(req.get_header("Anthropic-version"), "2026-10-21")
        self.assertIn("2026-10-25", req.data.decode())
        self.assertEqual(engine.routine_launch(self.today, self.env)["status"], "already")
        self.assertEqual(len(self.calls), 1)  # pas de deuxième lancement le même jour

    def test_refire_forces_a_new_launch(self):
        import os
        engine.urllib.request.urlopen = self.ok
        engine.routine_launch(self.today, self.env)
        os.environ["TESSBET_REFIRE"] = "1"
        self.assertEqual(engine.routine_launch(self.today, self.env)["status"], "fired")
        self.assertEqual(len(self.calls), 2)

    def test_http_error_is_reported_not_raised_and_not_remembered(self):
        import urllib.error, io
        def boom(req, timeout=0):
            raise urllib.error.HTTPError(req.full_url, 401, "no", {}, io.BytesIO(b"{}"))
        engine.urllib.request.urlopen = boom
        r = engine.routine_launch(self.today, self.env)
        self.assertEqual(r["status"], "error")
        self.assertIn("jeton", r["error"])
        self.assertNotIn("secret", str(r))
        engine.urllib.request.urlopen = self.ok  # le lendemain d'une erreur on peut réessayer
        self.assertEqual(engine.routine_launch(self.today, self.env)["status"], "fired")

    def test_network_failure_is_reported(self):
        def down(req, timeout=0):
            raise OSError("unreachable")
        engine.urllib.request.urlopen = down
        self.assertEqual(engine.routine_launch(self.today, self.env)["status"], "error")

    def test_routine_test_wakes_claude_without_marking_the_day(self):
        import os
        engine.urllib.request.urlopen = self.ok
        r = engine.routine_test(self.env)
        self.assertEqual(r["status"], "fired")
        self.assertIn("TEST DE LIAISON", self.calls[0].data.decode())
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "fire_2026-10-25.json")))  # le vrai lancement du jour reste possible
        self.assertEqual(engine.routine_launch(self.today, self.env)["status"], "fired")
        self.assertEqual(engine.routine_test({})["status"], "manual")

    def test_free_plan_gives_injuries_and_skips_predictions_quietly(self):
        calls = []
        old = engine.af_get
        def fake(path, params, key):
            calls.append(path)
            if path == "/fixtures":
                return [{"fixture": {"id": i}, "teams": {"home": {"name": "A"}, "away": {"name": "B"}}} for i in (100,)]
            if path == "/injuries":
                return [{"fixture": {"id": 100}, "player": {"name": "Joueur X", "reason": "Blessure"}, "team": {"name": "A"}}]
            raise RuntimeError("{'plan': 'Free plans do not have access to this endpoint'}")
        engine.af_get = fake
        try:
            cfg = engine.load_config()
            cfg["api_football"]["enabled"] = True
            cands = []
            for i in range(3):
                ev = make_event((0.7, 0.2, 0.1), boost=1.1, sport="football")
                ev["id"], ev["league_key"] = f"e{i}", "soccer_epl"
                cands += [engine.finalize(c) for c in engine.analyse_event(ev, cfg) if c["pick"] == "A"]
            warnings = []
            engine.enrich_football(cands, cfg, {"API_FOOTBALL_KEY": "k"}, self.today, warnings)
        finally:
            engine.af_get = old
        self.assertEqual(warnings, [])
        self.assertEqual(calls, ["/fixtures", "/injuries", "/predictions"])  # une seule prédiction tentée
        self.assertEqual(cands[0]["injuries"], ["Joueur X (A, Blessure)"])
        self.assertIn("Joueur X", engine.build_review_prompt(cands[:1], self.today))

    def test_review_deposits_request_then_wakes_claude(self):
        order = []
        old = (engine.task_fetch_answer, engine.task_request, engine.routine_launch)
        try:
            engine.task_fetch_answer = lambda today, env, key=None: False
            engine.task_request = lambda pool, today, env, key=None: order.append("request")
            engine.routine_launch = lambda today, env, key=None: (order.append("fire"), {"status": "fired"})[1]
            cfg = engine.load_config()
            cfg["claude"]["source"] = "claude_task"
            ev = make_event((0.8, 0.2), boost=1.1)
            pool = [engine.finalize(c) for c in engine.analyse_event(ev, cfg) if c["pick"] == "A"]
            warnings = []
            picks, info = engine.review_and_pick(pool, cfg, self.env, self.today, "live", warnings)
        finally:
            engine.task_fetch_answer, engine.task_request, engine.routine_launch = old
        self.assertEqual(order, ["request", "fire"])
        self.assertTrue(info["waiting"])
        self.assertEqual(info["fire"]["status"], "fired")
        self.assertEqual(picks, [])


class TestTomorrow(unittest.TestCase):
    """Analyse de demain : un fichier par jour, matchs du bon jour seulement, jamais de mélange."""

    def setUp(self):
        import tempfile
        self.tmp = tempfile.mkdtemp()
        self._old = (engine.DATA, engine.load_config, engine.fetch_live_events, engine.enrich_football)
        engine.DATA = self.tmp

    def tearDown(self):
        engine.DATA, engine.load_config, engine.fetch_live_events, engine.enrich_football = self._old

    def test_demo_tomorrow_has_its_own_file_and_does_not_touch_today(self):
        import os
        today = engine.paris_date()
        res = engine.run("demo", 1)
        self.assertEqual(res["date"], (today + dt.timedelta(days=1)).isoformat())
        self.assertEqual(res["offset"], 1)
        self.assertTrue(os.path.exists(engine.result_path("demo", res["date"])))
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "selections.json")))  # « la dernière » reste celle d'aujourd'hui
        res0 = engine.run("demo", 0)
        self.assertEqual(res0["date"], today.isoformat())
        self.assertTrue(os.path.exists(os.path.join(self.tmp, "selections.json")))
        self.assertTrue(os.path.exists(engine.result_path("demo", today.isoformat())))

    def test_live_keeps_only_the_requested_day(self):
        now = dt.datetime.now(dt.timezone.utc)
        today = engine.paris_date()
        at = lambda d, h: engine.to_paris(now).replace(year=d.year, month=d.month, day=d.day, hour=h, minute=0, second=0, microsecond=0)\
            .astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        tomorrow, after = today + dt.timedelta(days=1), today + dt.timedelta(days=2)
        evs = []
        for i, (d, h) in enumerate([(today, 23), (tomorrow, 15), (after, 15)]):
            ev = make_event((0.8, 0.2), boost=1.1)
            ev["id"], ev["commence"] = f"e{i}", at(d, h)
            evs.append(ev)
        cfg = engine.load_config()
        cfg = dict(cfg, claude=dict(cfg["claude"], enabled=False, required=False), api_football=dict(cfg["api_football"], enabled=False))
        engine.load_config = lambda: cfg
        engine.fetch_live_events = lambda c, e, n, w: (evs, "400")
        engine.enrich_football = lambda *a, **k: None
        res = engine.run("live", 1)
        self.assertEqual(res["date"], tomorrow.isoformat())
        self.assertEqual(res["matches_analysed"], 1)  # ni le match d'aujourd'hui ni celui d'après-demain
        self.assertEqual([s["kickoff_local"] for s in res["selections"]], ["15:00"])
        import os
        self.assertTrue(os.path.exists(engine.result_path("live", tomorrow.isoformat())))
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "selections.json")))

    def test_notification_says_tomorrow(self):
        sel = [{"kickoff_local": "15:00", "match": "A – B", "pick_label": "Victoire de A", "odds": 1.3, "prob": 0.8,
                "claude": {"verdict": "confirme"}}]
        title, _ = engine.notification_text({"selections": sel, "message": "x", "offset": 1})
        self.assertIn("demain", title)
        title, _ = engine.notification_text({"selections": [], "message": "x", "offset": 1})
        self.assertIn("demain", title)
        title, _ = engine.notification_text({"selections": sel, "message": "x"})
        self.assertIn("du jour", title)

    def test_prompts_know_the_real_date_and_that_matches_are_tomorrow(self):
        today = dt.date(2026, 10, 25)
        self.assertTrue(engine._when(today, today).startswith("Nous sommes le 2026-10-25."))
        t = engine._when(dt.date(2026, 10, 26), today)
        self.assertIn("Nous sommes le 2026-10-25.", t)
        self.assertIn("demain, le 2026-10-26", t)

    def test_fire_message_mentions_tomorrow_only_for_tomorrow(self):
        calls = []
        old = engine._routine_post
        engine._routine_post = lambda env, text: (calls.append(text), {"status": "fired", "session_url": "u", "fired_at": "t"})[1]
        try:
            engine.routine_launch(engine.paris_date(), {"ROUTINE_ID": "x", "ROUTINE_TOKEN": "y"})
            engine.routine_launch(engine.paris_date() + dt.timedelta(days=1), {"ROUTINE_ID": "x", "ROUTINE_TOKEN": "y"})
        finally:
            engine._routine_post = old
        self.assertNotIn("demain", calls[0])
        self.assertIn("ceux de demain", calls[1])
        import os
        self.assertEqual(len([f for f in os.listdir(self.tmp) if f.startswith("fire_")]), 2)  # un réveil par jour, indépendants

    def _pool(self, cfg, n=2):
        pool = []
        for i in range(n):
            ev = make_event((0.8, 0.2), boost=1.1)
            ev["id"] = f"e{i}"
            pool += [engine.finalize(c) for c in engine.analyse_event(ev, cfg) if c["pick"] == "A"]
        return pool

    def test_new_picks_after_answer_get_a_complement_request(self):
        old = (engine.task_fetch_answer, engine.task_request, engine.routine_launch)
        asked = []
        try:
            cfg = engine.load_config()
            cfg["claude"]["source"] = "claude_task"
            pool = self._pool(cfg)
            day = dt.date(2026, 10, 26)
            engine.write_json(engine._review_cache_file(day), {"e0|A": {"verdict": "confirme", "resume": "ok", "points": [], "sources": []}})

            def fetch(d, env, key=None):  # seule la première demande a une réponse (elle ne couvre pas e1)
                if key == d.isoformat():
                    engine.write_json(engine._rounds_path(d), sorted(engine.rounds_done(d) | {key}))
                    return True
                return False
            engine.task_fetch_answer = fetch
            engine.task_request = lambda p, d, env, key=None: asked.append((key, [c["event_id"] for c in p]))
            engine.routine_launch = lambda d, env, key=None: {"status": "fired", "key": key}
            picks, info = engine.review_and_pick(pool, cfg, {}, day, "live", [])
        finally:
            engine.task_fetch_answer, engine.task_request, engine.routine_launch = old
        self.assertTrue(info.get("waiting"))
        self.assertEqual(asked, [("2026-10-26-r2", ["e1"])])  # complément avec le seul nouveau match
        self.assertEqual(info["fire"]["key"], "2026-10-26-r2")

    def test_after_last_complement_new_picks_are_dropped(self):
        old = (engine.task_fetch_answer, engine.task_request)
        try:
            cfg = engine.load_config()
            cfg["claude"]["source"] = "claude_task"
            pool = self._pool(cfg)
            day = dt.date(2026, 10, 26)
            engine.write_json(engine._review_cache_file(day), {"e0|A": {"verdict": "confirme", "resume": "ok", "points": [], "sources": []}})
            engine.write_json(engine._rounds_path(day), engine.round_keys(day))
            engine.task_fetch_answer = lambda *a, **k: False
            engine.task_request = lambda *a, **k: self.fail("plus de demande")
            warnings = []
            picks, info = engine.review_and_pick(pool, cfg, {}, day, "live", warnings)
        finally:
            engine.task_fetch_answer, engine.task_request = old
        self.assertFalse(info.get("waiting"))
        self.assertEqual([p["event_id"] for p in picks], ["e0"])
        self.assertTrue(any("pas encore vérifié" in w for w in warnings))
        self.assertEqual([x["unverified"] for x in info["dropped_items"]], [True])  # en attente, pas « écarté »
        self.assertEqual(info["unverified"], 1)

    def test_recheck_asks_only_unverified(self):
        old = (engine.task_fetch_answer, engine.task_request, engine.routine_launch)
        asked = []
        try:
            cfg = engine.load_config()
            cfg["claude"]["source"] = "claude_task"
            pool = self._pool(cfg)
            day = dt.date(2026, 10, 26)
            engine.write_json(engine._review_cache_file(day), {"e0|A": {"verdict": "confirme", "resume": "ok", "points": [], "sources": []}})
            engine.write_json(engine._rounds_path(day), engine.round_keys(day))
            engine.task_fetch_answer = lambda *a, **k: False
            engine.task_request = lambda p, d, env, key=None: asked.append((key, [c["event_id"] for c in p]))
            engine.routine_launch = lambda d, env, key=None: {"status": "fired", "key": key}
            os.environ["TESSBET_RECHECK"] = "1"
            picks, info = engine.review_and_pick(pool, cfg, {}, day, "live", [])
        finally:
            os.environ.pop("TESSBET_RECHECK", None)
            engine.task_fetch_answer, engine.task_request, engine.routine_launch = old
        self.assertTrue(info.get("waiting"))
        self.assertEqual(asked, [("2026-10-26-r4", ["e1"])])
        self.assertIn("2026-10-26-r4", engine.rounds_asked(day))


class TestNote(unittest.TestCase):
    def test_scale(self):
        base = {"n_books": 8, "agree": 8, "dispersion": 0.01}
        self.assertEqual(engine.note_of(dict(base, prob=0.86, claude={"verdict": "confirme"})), 5)
        self.assertEqual(engine.note_of(dict(base, prob=0.67, claude={"verdict": "confirme"})), 3)
        self.assertLess(engine.note_of(dict(base, prob=0.62)), engine.MIN_NOTE)
        self.assertEqual(engine.note_of(dict(base, prob=0.40, tier="outsider", claude={"verdict": "confirme"})), 2)


class TestNews(unittest.TestCase):
    def test_new_picks_are_marked(self):
        import tempfile
        old = engine.DATA
        engine.DATA = tempfile.mkdtemp()
        try:
            r1 = {"mode": "live", "date": "2026-10-27", "selections": [{"match": "A – B", "pick_label": "Victoire de A"}], "waiting": False}
            engine.mark_news(r1); engine.write_json(engine.result_path("live", "2026-10-27"), r1)
            self.assertIsNone(r1["news"])
            r2 = {"mode": "live", "date": "2026-10-27", "waiting": False, "selections": [
                {"match": "A – B", "pick_label": "Victoire de A"}, {"match": "C – D", "pick_label": "Victoire de C"}]}
            engine.mark_news(r2)
        finally:
            engine.DATA = old
        self.assertEqual(r2["news"], 1)
        self.assertEqual([x["new"] for x in r2["selections"]], [False, True])


class TestCombo(unittest.TestCase):
    def leg(self, i, prob, odds_w, odds_u=None):
        c = {"event_id": f"e{i}", "pick": f"T{i}", "home": f"T{i}", "away": f"U{i}", "sport": "football", "league": "L",
             "league_key": "soccer_epl", "commence": f"2026-10-26T1{i}:00:00Z", "prob": prob, "odds": odds_w,
             "offers": [{"key": "winamax_fr", "odds": odds_w}] + ([{"key": "unibet_fr", "odds": odds_u}] if odds_u else []),
             "claude": {"verdict": "confirme", "resume": "ok"}}
        return c

    def test_best_combo_on_one_site_within_limits(self):
        cfg = engine.load_config()
        legs = [self.leg(0, 0.72, 1.35), self.leg(1, 0.70, 1.40), self.leg(2, 0.68, 1.45), self.leg(3, 0.66, 1.50), self.leg(4, 0.62, 1.60)]
        cb = engine.build_combo(legs, cfg)
        self.assertIsNotNone(cb)
        self.assertEqual(cb["site"], "winamax")
        self.assertIn(len(cb["legs"]), (3, 4))
        self.assertTrue(3.0 <= cb["total_odds"] <= 8.0)
        self.assertGreaterEqual(cb["prob"], 0.15)
        self.assertEqual(len({l["match"] for l in cb["legs"]}), len(cb["legs"]))

    def test_no_combo_when_odds_too_small(self):
        cfg = engine.load_config()
        legs = [self.leg(i, 0.85, 1.15) for i in range(4)]  # 1,15^4 = 1,75 : pas assez
        self.assertIsNone(engine.build_combo(legs, cfg))

    def test_combo_legs_reviewed_but_not_shown_as_picks(self):
        cfg = engine.load_config()
        cfg["claude"]["source"] = "api"
        pool = []
        for i in range(4):
            ev = make_event((0.66, 0.34), boost=1.0)
            ev["id"] = f"e{i}"
            pool += [engine.finalize(c) for c in engine.analyse_event(ev, cfg) if c["pick"] == "A"]
        legs = engine.select_combo_legs(pool, cfg)
        self.assertTrue(all(c["tier"] == "combo" for c in legs))


class TestWhyNoPick(unittest.TestCase):
    """« Pourquoi aucun pari ? » : les presque-paris et ce qui les bloque."""

    def setUp(self):
        self.cfg = engine.load_config()

    def cands(self, probs, **kw):
        out = []
        for i, p in enumerate(probs):
            ev = make_event((p, 1 - p), **kw)
            ev["id"] = f"e{i}"
            out += [engine.finalize(c) for c in engine.analyse_event(ev, self.cfg) if c["pick"] == "A"]
        return out

    def test_low_probability_is_explained(self):
        rows = engine.near_misses(self.cands([0.55]), self.cfg)
        self.assertEqual(len(rows), 1)
        self.assertTrue(any("de chances" in r and "62 %" in r for r in rows[0]["reasons"]))
        self.assertEqual(rows[0]["pick_label"], "Victoire de A")

    def test_selected_bets_are_not_listed(self):
        self.assertEqual(engine.near_misses(self.cands([0.8], boost=1.1), self.cfg), [])

    def test_closest_first_and_limited_to_three(self):
        rows = engine.near_misses(self.cands([0.50, 0.58, 0.45, 0.60, 0.40]), self.cfg)
        self.assertEqual(len(rows), 3)
        self.assertEqual([round(r["prob"], 2) for r in rows], [0.60, 0.58, 0.50])  # les plus proches du seuil d'abord

    def test_few_books_and_low_odds_are_explained(self):
        why = engine.blockers(dict(self.cands([0.8], boost=1.1)[0], n_books=3, odds=1.1), self.cfg)
        self.assertTrue(any("bookmaker" in w for w in why))
        self.assertTrue(any("cote de 1,10" in w for w in why))

    def test_result_carries_near_and_claude_dropped(self):
        import tempfile
        old = engine.DATA
        engine.DATA = tempfile.mkdtemp()
        try:
            res = engine.run("demo", 0)
        finally:
            engine.DATA = old
        self.assertIn("near", res)
        self.assertIn("claude_dropped_items", res)
        self.assertTrue(all(it["resume"] for it in res["claude_dropped_items"]))


class TestTime(unittest.TestCase):
    def test_paris_summer_and_winter(self):
        self.assertEqual(engine.to_paris(dt.datetime(2026, 10, 24, 12, tzinfo=dt.timezone.utc)).hour, 14)
        self.assertEqual(engine.to_paris(dt.datetime(2026, 12, 1, 12, tzinfo=dt.timezone.utc)).hour, 13)


class TestValue(unittest.TestCase):
    def cand(self, **kw):
        c = {"event_id": "e1", "playable": True, "n_books": 6, "prob": 0.48, "odds": 2.4, "dispersion": 0.01, "sport": "football",
             "home": "A", "away": "B", "pick": "A", "league": "L", "commence": "2026-10-25T18:00:00Z"}
        c.update(kw)
        c["edge"] = c["prob"] * c["odds"] - 1
        return c

    def test_value_bet_found_with_small_stake(self):
        cfg = engine.load_config()
        out = engine.select_value([self.cand()], cfg)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["tier"], "value")
        self.assertLessEqual(out[0]["stake_pct"], 0.01)

    def test_value_bet_rejected_when_odds_fair_or_too_risky(self):
        cfg = engine.load_config()
        self.assertEqual(engine.select_value([self.cand(odds=2.05)], cfg), [])          # cote juste : pas d'affaire
        self.assertEqual(engine.select_value([self.cand(prob=0.30, odds=4.0)], cfg), [])  # trop risqué
        self.assertEqual(engine.select_value([self.cand(playable=False)], cfg), [])       # pas jouable en France
        self.assertEqual(engine.select_value([self.cand()], cfg, exclude={"e1"}), [])     # déjà dans les paris fiables


class TestOutsider(unittest.TestCase):
    def cand(self, **kw):
        c = {"event_id": "e1", "playable": True, "n_books": 6, "prob": 0.25, "odds": 4.6, "dispersion": 0.02, "sport": "football",
             "home": "A", "away": "B", "pick": "B", "league": "L", "commence": "2026-10-25T18:00:00Z"}
        c.update(kw)
        c["edge"] = c["prob"] * c["odds"] - 1
        return c

    def test_outsider_with_value_is_kept(self):
        out = engine.select_outsider([self.cand()], engine.load_config())
        self.assertEqual([c["tier"] for c in out], ["outsider"])

    def test_outsider_rejected(self):
        cfg = engine.load_config()
        self.assertEqual(engine.select_outsider([self.cand(odds=4.0)], cfg), [])            # cote juste (25 % -> 4,00)
        self.assertEqual(engine.select_outsider([self.cand(prob=0.10, odds=12)], cfg), [])  # loto
        self.assertEqual(engine.select_outsider([self.cand(pick="Draw")], cfg), [])          # victoire seulement
        self.assertEqual(engine.select_outsider([self.cand()], cfg, exclude={"e1"}), [])     # pas contre un pari déjà retenu

    def test_prompt_marks_outsider(self):
        c = self.cand(tier="outsider")
        self.assertIn("OUTSIDER", engine.build_review_prompt([c], engine.dt.date(2026, 10, 25)))


class TestNightWindow(unittest.TestCase):
    def test_day_runs_until_7am_next_morning(self):
        lo, hi = engine.day_window(dt.date(2026, 10, 26))
        self.assertEqual((lo.hour, hi.day, hi.hour), (6, 27, 6))      # 7 h à Paris = 6 h UTC (heure d'hiver)
        nba = dt.datetime(2026, 10, 27, 0, 10, tzinfo=dt.timezone.utc)  # 1 h 10 à Paris, la nuit
        self.assertTrue(lo <= nba < hi)
        self.assertEqual(engine.journee(nba), dt.date(2026, 10, 26))
        self.assertEqual(engine.journee(dt.datetime(2026, 10, 27, 8, tzinfo=dt.timezone.utc)), dt.date(2026, 10, 27))


if __name__ == "__main__":
    unittest.main()


class TestLearning(unittest.TestCase):
    def test_calibration_lowers_overconfident_sport(self):
        import tempfile
        old = engine.DATA
        engine.DATA = tempfile.mkdtemp()
        try:
            shared = [{"date": "2026-11-01", "match": f"H{i} – A{i}", "sport": "hockey", "prob": 0.75, "note": 4,
                       "reported": "win" if i < 10 else "lose"} for i in range(20)]
            shared += [{"date": "2026-11-01", "match": f"T{i} – U{i}", "sport": "tennis", "prob": 0.8, "note": 4,
                        "reported": "win" if i < 4 else "lose"} for i in range(5)]
            engine.write_json(os.path.join(engine.DATA, "telegram.json"), {"shared": shared})
            cal = engine.calibrate()
            h = cal["sports"]["hockey"]
            self.assertEqual((h["n"], h["real_pct"], h["expected_pct"]), (20, 50, 75))
            self.assertTrue(h["active"]); self.assertLess(h["ratio"], 0.95)
            self.assertFalse(cal["sports"]["tennis"]["active"])  # trop peu de résultats : pas de correction
            self.assertLess(engine.calibrated("hockey", 0.75), 0.70)
            self.assertEqual(engine.calibrated("tennis", 0.8), 0.8)
        finally:
            engine.DATA = old
            engine._cal.update(mtime=None, data={})


class TestWeeklyReview(unittest.TestCase):
    def test_review_tightens_then_relaxes(self):
        import tempfile
        old = engine.DATA
        engine.DATA = tempfile.mkdtemp()
        engine._wk.update(mtime=None, data={})
        cfg = {"selection": {"min_prob": 0.62, "sport_min_prob": {"hockey": 0.70}}}
        try:
            day = dt.date(2026, 11, 22)
            bad = [{"date": "2026-11-18", "match": f"H{i} – A{i}", "sport": "hockey", "tier": "fiable",
                    "reported": "win" if i < 4 else "lose"} for i in range(8)]
            bad += [{"date": "2026-11-18", "match": f"N{i} – B{i}", "sport": "basketball", "tier": "moins_sur",
                     "reported": "win" if i < 7 else "lose"} for i in range(10)]
            r = engine.weekly_review(day, bad, cfg)
            self.assertEqual(r["week"]["fiable"], [4, 8])
            self.assertEqual(engine.min_prob_for({"sport": "hockey"}, cfg["selection"]), 0.72)
            self.assertEqual(engine.min_prob_for({"sport": "tennis"}, cfg["selection"]), 0.62)
            self.assertEqual(engine.min_note_for("basketball"), 4)
            self.assertEqual(engine.min_note_for("tennis"), 3)
            good = [dict(x, reported="win") for x in bad]
            engine.weekly_review(day, good, cfg)
            self.assertEqual(engine.min_prob_for({"sport": "hockey"}, cfg["selection"]), 0.70)
            self.assertEqual(engine.min_note_for("basketball"), 3)
        finally:
            engine.DATA = old
            engine._wk.update(mtime=None, data={})


class TestScript(unittest.TestCase):
    def test_engine_runs_as_a_script(self):
        """L'analyse est lancée en tant que programme : tout doit être défini avant le lancement."""
        import subprocess, sys, tempfile
        env = dict(os.environ, TESSBET_MODE="demo", TESSBET_DATA=tempfile.mkdtemp(), TESSBET_NO_NOTIFY="1")
        r = subprocess.run([sys.executable, os.path.join(os.path.dirname(engine.__file__), "engine.py")], env=env,
                           capture_output=True, text=True, timeout=120)
        self.assertEqual(r.returncode, 0, r.stderr[-500:])


class TestFreebet(unittest.TestCase):
    def test_combo_reaches_target_with_best_chances(self):
        old = engine.cached_events
        start = (engine.now_utc() + dt.timedelta(hours=3)).strftime("%Y-%m-%dT%H:%M:%SZ")
        evs = []
        for i, pa in enumerate([0.66, 0.6, 0.75, 0.56, 0.63, 0.68, 0.55]):
            ev = make_event([pa, 1 - pa])
            ev.update(id=f"e{i}", commence=start, home=f"A{i}", away=f"B{i}", sport="basketball")
            for b in ev["bookmakers"]:
                b["outcomes"] = {f"A{i}": b["outcomes"]["A"], f"B{i}": b["outcomes"]["B"]}
            evs.append(ev)
        engine.cached_events = lambda cfg: (evs, None)
        try:
            r = engine.freebet_combos()
        finally:
            engine.cached_events = old
        self.assertTrue(r["combos"])
        c = r["combos"][0]
        self.assertGreaterEqual(c["total_odds"], 10)
        self.assertGreater(c["prob"], 0.05)
