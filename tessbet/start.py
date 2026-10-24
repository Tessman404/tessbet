"""Démarrage dans Home Assistant : lit les réglages de l'extension puis lance Tessbet."""
import json, os

try:
    with open("/data/options.json", encoding="utf-8") as f:
        o = json.load(f)
except OSError:
    o = {}

os.environ["TESSBET_DATA"] = "/data/tessbet"
os.environ["ODDS_API_KEY"] = o.get("odds_api_key", "")
os.environ["API_FOOTBALL_KEY"] = o.get("api_football_key", "")
os.environ["ANTHROPIC_API_KEY"] = o.get("anthropic_api_key", "")
os.environ["GITHUB_TOKEN"] = o.get("github_token", "")
os.environ["ROUTINE_ID"] = (o.get("routine_id") or "").strip()
os.environ["ROUTINE_TOKEN"] = (o.get("routine_token") or "").strip()
os.environ["TESSBET_SOURCE"] = o.get("analysis_source", "api")
os.environ["TESSBET_MODE"] = o.get("mode", "live")
if os.environ.get("SUPERVISOR_TOKEN"):  # accès interne à Home Assistant, sans jeton à créer
    os.environ["HA_URL"] = "http://supervisor/core"
    os.environ["HA_TOKEN"] = os.environ["SUPERVISOR_TOKEN"]
if o.get("notify_service"):  # pris en compte par engine.load_config
    os.environ["TESSBET_NOTIFY"] = o["notify_service"]
os.makedirs("/data/tessbet", exist_ok=True)

import server
server.main()
