---
name: rca-report
description: Korean RCA report JSON format for incidents, with evidence and recommended-change rules. Use when writing or reviewing an RCA report.
---
# RCA report format

Return ONLY valid JSON (no markdown, no code fences, no explanation):
{
  "summary": "1-2 sentence root cause summary in Korean",
  "timeline": [{"time": "ISO8601", "event": "description in Korean"}],
  "root_cause": {"description": "detailed explanation in Korean", "evidence": ["specific evidence items"], "confidence": "HIGH|MEDIUM|LOW"},
  "impact": {"affected_resources": ["resource IDs"], "service_impact": "description in Korean"},
  "recommended_actions": [
    {
      "action_id": "1",
      "priority": 1,
      "description": "Human-readable description in Korean",
      "change": "The code change in the workload repository: file, construct or setting, old value and new value (Korean)",
      "risk_level": "LOW|MEDIUM|HIGH"
    }
  ]
}

CRITICAL FORMAT RULES:
- Output MUST be a single valid JSON object. No markdown, no code fences, no text before or after.
- summary and root_cause.description MUST be human-readable Korean sentences, NOT raw JSON/API data.
- If the collected data is insufficient, still produce the JSON structure with what you have and set confidence to LOW.

CRITICAL ANALYSIS RULES:
- EVERY CLAIM MUST BE BACKED BY EVIDENCE from the collected data. Never assume or guess.
- Trace the causal chain ONE STEP AT A TIME: A caused B, B caused C. Each step must have evidence.
- Clearly identify WHICH resource owns the changed configuration (e.g. "ALB Security Group" not just "Security Group")
- Do NOT infer effects beyond what the evidence shows.
- CAUSATION VERIFICATION: When you find a configuration change, verify it ACTUALLY explains THIS alarm's symptom:
  - Does this change produce the specific error type in the alarm? (e.g. SG outbound block → 5XX/timeout, NOT 4XX)
  - Did the alarm metric anomaly start AFTER the change? Check timestamps. If the anomaly started BEFORE the change, the change is NOT the cause.
  - If a change exists but does NOT match the alarm symptom, it is a coincidence — do NOT use it as root cause.
- If the Collector found NO infrastructure issue (all healthy, no config changes), the root cause should state that this is likely client-side traffic (invalid URLs, bots, scanners) and NOT an infrastructure problem. Set confidence to HIGH in this case — "no infrastructure issue" IS a valid and confident conclusion.

CRITICAL rules for recommended_actions:
- MINIMUM NECESSARY ACTIONS ONLY: Only include actions that directly fix the root cause of THIS alarm
- DO NOT fix pre-existing issues unrelated to this alarm's root cause
- DO NOT add preventive measures, monitoring improvements, or optimizations
- If the root cause is client-side traffic (not infrastructure), recommended_actions SHOULD BE EMPTY: there is nothing to fix
- Every fix is a code change in the workload repository, delivered as a pull request and deployed by the pipeline.
  Never recommend AWS CLI write commands or console changes.
- If a deployed commit caused the incident, the action restores exactly the values that commit changed. Name the
  commit ID, the file and the old and new values in change.
- If the root cause is a change made outside the pipeline (the deployed state differs from main), the action is to run
  the pipeline again so the deployed state matches main; say so in change.
- Use real resource IDs, commit IDs and file paths from the investigation data. NEVER use placeholders.
