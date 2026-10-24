# Boîte d'échange Tessbet ↔ Claude

Mode « Claude fait l'analyse » (réglage `analysis_source: claude_task` de l'application).

1. L'application dépose `request-AAAA-MM-JJ.json` : la liste des paris à vérifier (`items`, chacun avec un `id`).
2. Une tâche programmée de Claude fait les recherches web et dépose `answer-AAAA-MM-JJ.json` :

```json
{"answers": {"<id>": {
  "verdict": "confirme | prudence | ecarte",
  "resume": "1 à 2 phrases simples",
  "points": ["3 faits précis au maximum"],
  "sources": ["https://… (liens réellement consultés)"],
  "analyse": "5 à 6 paragraphes courts séparés par une ligne vide : contexte, forme, absents, style/historique, cohérence de la cote, risques et avis prudent"
}}}
```

3. L'application récupère la réponse (toutes les 10 minutes), affiche l'analyse et envoie la notification.

Règles pour Claude : français simple, ne rien inventer, ne jamais ajouter de pari, écarter en cas de doute sérieux, ne promettre aucun gain.
