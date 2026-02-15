# Lessons Learned

## Never Run API-Consuming Tests Without Permission
During initial development, an end-to-end test was run against the PlaylistExporter project without asking first. This made real API calls to Anthropic's Claude API and incurred charges on the user's account. Any script that invokes the AI client (analyze_project.py, or any test that calls llm_analysis functions) costs real money. Always get explicit approval before running anything that touches paid APIs. This rule is also documented in CLAUDE.md under "Cost Control".

**Safe to run freely**: snapshot_discovery.py, snapshot_diff.py, change_analyzer.py, import checks, and any purely local operations.

**Requires permission**: analyze_project.py (full or drill-down), or any script/test that calls into llm_analysis.py or utils/ai_client.py.
