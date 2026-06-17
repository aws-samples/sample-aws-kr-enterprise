"""AIOps AgentCore Entrypoint - routes to RCA Graph or Chatbot agent."""
import json
import os
import re

from strands import Agent, tool
from strands.models.bedrock import BedrockModel
from strands.multiagent import GraphBuilder
from bedrock_agentcore.runtime import BedrockAgentCoreApp

from tools import (logs_agent, metrics_agent, infrastructure_agent, knowledge_agent,
                    remediation_agent, query_reports, query_alarms)

MODEL_ID = os.environ.get("MODEL_ID", "global.anthropic.claude-sonnet-4-6")
REGION = os.environ.get("AWS_REGION", "ap-northeast-2")

model = BedrockModel(model_id=MODEL_ID, region_name=REGION)

# ========================================================================
# RCA Graph: Collector → Writer → Reviewer → (loop back or finish)
# ========================================================================

MAX_ITERATIONS = 3

# --- Collector Agent prompt ---
COLLECTOR_PROMPT = """You are an SRE Information Collector. Your job is to gather data about an incident using EVIDENCE-BASED investigation.

## CORE PRINCIPLE: Evidence-Based Investigation
Every tool call MUST have a clear reason derived from prior evidence. Never investigate "just in case."
- Your INITIAL EVIDENCE is the input you receive (alarm details, user question, or incident description)
- Each finding becomes evidence that may justify investigating the next resource
- If you cannot articulate WHY you need to call a tool, DO NOT call it

## CRITICAL: Not Every Alarm Requires Infrastructure Remediation
After investigation, if ALL of the following are true, conclude "NO INFRASTRUCTURE ISSUE FOUND":
- All targets are healthy
- No configuration changes in CloudTrail
- No resource failures, misconfigurations, or missing rules detected by any tool
- The alarm is 4XX-related and all infrastructure is functioning normally
In this case, the root cause is likely CLIENT-SIDE (bad URLs, invalid requests, bots, scanners) — NOT an infrastructure problem.
Do NOT fabricate infrastructure causes (e.g. "worker saturation", "capacity limits") when there is no evidence of infrastructure failure.

## CRITICAL: When Targets Are Unhealthy
If targets are unhealthy, the cause is ALWAYS in the infrastructure — never dismiss it as "application issue" without exhausting all infrastructure checks:
- Network connectivity: Security Group rules (BOTH inbound AND outbound), NACLs, route tables
- Resource state: EC2 running/stopped, RDS available/stopped, ASG desired vs actual count
- Configuration changes: CloudTrail events for ANY modification to related resources
- Pay special attention to WARNING messages from tools — they flag known misconfigurations

## PARALLEL EXECUTION: Call independent tools in the SAME turn
When multiple investigations are justified by the SAME evidence, call them ALL at once in a single turn.
- Example: ALB 4XX alarm → you can call infrastructure_agent (check ALB/target health) AND metrics_agent (check 4XX metric trend) AND logs_agent (check CloudTrail for recent changes) simultaneously, because all three are justified by the same initial alarm evidence.
- Do NOT call tools sequentially when they don't depend on each other's results.
- Only serialize calls when tool B needs the output of tool A (e.g. "infrastructure_agent found unhealthy target i-abc → now call logs_agent for that specific instance").

## Investigation Strategy
1. PARSE initial evidence: Extract resource IDs, metric names, timestamps, error descriptions
2. INITIAL PARALLEL INVESTIGATION: Call all specialist agents whose investigation is directly justified by the initial evidence — in a single turn
3. FOLLOW-UP: Based on combined results, call additional agents ONLY if new evidence points to another resource
4. STOP when you find the specific change that caused the issue OR when you confirm no infrastructure issue exists

## Tool Usage Rules
When calling any tool, you MUST provide a 'reason' parameter explaining:
- WHAT evidence led you to this call
- WHAT specific resource/log/metric you are investigating

## Tools
- logs_agent(query, reason): CloudWatch Logs + CloudTrail + EC2 system logs + AWS Config
- metrics_agent(query, reason): CloudWatch Metrics for specific metrics
- infrastructure_agent(query, reason): Current state of specific AWS resources
- knowledge_agent(query, reason): Runbooks and past incidents from Knowledge Base

## Output Rules
- For each resource investigated, state HEALTHY or UNHEALTHY with evidence
- If an infrastructure change caused the issue, report the EXACT change
- If NO infrastructure issue is found, clearly state that the alarm was likely caused by client-side traffic (e.g. invalid URLs, bots) and NOT by an infrastructure failure
- Do NOT report pre-existing conditions as root causes"""

# --- Writer Agent prompt ---
WRITER_PROMPT = """You are an RCA Report Writer. Write the report in Korean (한국어).
Keep technical terms, product names, and service names in English (e.g. CloudWatch, Security Group, ALB, EC2, RDS).

You will receive raw investigation data from the Collector. Your job is to ANALYZE this data and produce a structured report — do NOT copy raw API responses into the report.

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
      "command": "aws cli WRITE command with real resource IDs",
      "code": "boto3 Python WRITE code with real resource IDs",
      "risk_level": "LOW|MEDIUM|HIGH",
      "status": "PENDING_APPROVAL"
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
- If the root cause is client-side traffic (not infrastructure), recommended_actions SHOULD BE EMPTY — there is nothing to fix on the infrastructure side
- command and code MUST use real resource IDs from the investigation data — NEVER use placeholders
- If the root cause is a configuration change, the action should REVERSE that specific change"""

# --- Reviewer Agent prompt ---
REVIEWER_PROMPT = """You are an RCA Report Quality Reviewer. Evaluate the RCA report against these criteria:

1. ROOT CAUSE: Is it specific, evidence-backed, and directly explains why THIS alarm fired?
   - A valid root cause CAN BE "client-side traffic (invalid URLs, bots)" if all infrastructure is healthy and no config changes were found
   - FAIL if the report fabricates infrastructure causes when evidence shows everything is healthy
2. EVIDENCE: Are there concrete log entries, metric values, or resource states cited?
3. CAUSAL CHAIN ACCURACY:
   - Does each step in the causal chain have evidence? (A→B must have proof that A caused B)
   - FAIL if any step is assumed/guessed without evidence
   - FAIL if the chain skips steps (e.g. "SG changed → DB failed" without explaining the intermediate steps)
   - FAIL if the report attributes an effect to the wrong resource (e.g. saying "EC2 SG" when it was "ALB SG")
   - FAIL if the report blames infrastructure (capacity, workers, NAT) when there is NO evidence of infrastructure failure
   - FAIL if a configuration change is cited as root cause but it produces a DIFFERENT error type than the alarm (e.g. SG outbound block causes 5XX/timeout, NOT 4XX — citing it as 4XX cause is wrong)
   - FAIL if the alarm metric anomaly started BEFORE the cited configuration change — the change cannot cause something that already happened
4. ACTIONS - NECESSITY: Does EVERY recommended action have a direct causal link to the alarm?
   - FAIL if any action fixes something that is NOT broken (verify against collected evidence)
   - FAIL if any action addresses a pre-existing issue unrelated to this alarm
   - FAIL if any action is preventive/optimization rather than remediation
   - If root cause is client-side traffic, recommended_actions SHOULD BE EMPTY — FAIL if actions are included
5. ACTIONS - CORRECTNESS: Do the commands actually reverse/fix the identified root cause?

Respond in this EXACT format:
VERDICT: PASS or FAIL
GAPS: (only if FAIL) Bullet list of specific issues
SCORE: 1-10

Be strict about action necessity. Fewer correct actions is better than many unnecessary ones."""


def needs_revision(state):
    """Check if reviewer says FAIL - loop back to collector."""
    review_result = state.results.get("reviewer")
    if not review_result:
        return False
    text = str(review_result.result).upper()
    return "VERDICT: FAIL" in text or "VERDICT:FAIL" in text


def is_approved(state):
    """Check if reviewer says PASS - proceed to finish."""
    review_result = state.results.get("reviewer")
    if not review_result:
        return True  # default to approved if no result
    text = str(review_result.result).upper()
    return "VERDICT: PASS" in text or "VERDICT:PASS" in text


def build_rca_graph():
    """Build a fresh RCA graph with new Agent instances per invocation."""
    col = Agent(name="collector", model=model, system_prompt=COLLECTOR_PROMPT,
                tools=[logs_agent, metrics_agent, infrastructure_agent, knowledge_agent], callback_handler=None)
    wri = Agent(name="writer", model=model, system_prompt=WRITER_PROMPT, tools=[], callback_handler=None)
    rev = Agent(name="reviewer", model=model, system_prompt=REVIEWER_PROMPT, tools=[], callback_handler=None)

    builder = GraphBuilder()
    builder.add_node(col, "collector")
    builder.add_node(wri, "writer")
    builder.add_node(rev, "reviewer")

    builder.add_edge("collector", "writer")
    builder.add_edge("writer", "reviewer")
    builder.add_edge("reviewer", "collector", condition=needs_revision)

    builder.set_max_node_executions(MAX_ITERATIONS * 3)
    builder.set_execution_timeout(1800)
    builder.reset_on_revisit(True)
    builder.set_entry_point("collector")
    return builder.build()


def run_rca(prompt: str) -> str:
    """Execute the RCA graph and extract the final report."""
    graph = build_rca_graph()
    result = graph(prompt)

    # Extract the writer's output even if graph status is FAILED (e.g. max iterations reached)
    # The writer may have produced a usable report before the reviewer rejected it
    writer_result = result.results.get("writer")
    if writer_result:
        text = str(writer_result.result)
    elif hasattr(result, 'status') and 'FAILED' in str(result.status).upper():
        raise RuntimeError(f"RCA Graph failed: {result.status}")
    else:
        text = str(result)

    # Extract JSON from markdown code fences if present
    if "```json" in text:
        match = re.search(r'```json\s*(\{.*\})\s*```', text, re.DOTALL)
        if match:
            text = match.group(1)
    elif "```" in text:
        match = re.search(r'```\s*(\{.*\})\s*```', text, re.DOTALL)
        if match:
            text = match.group(1)

    # Try to find the outermost JSON object if text has extra content
    if not text.strip().startswith("{"):
        match = re.search(r'\{.*"summary".*\}', text, re.DOTALL)
        if match:
            text = match.group(0)

    return text


# ========================================================================
# RCA as Tool (for Chatbot to invoke)
# ========================================================================

@tool
def rca_agent(query: str) -> str:
    """Generate a structured RCA report for an incident using the multi-agent RCA graph.
    The graph collects information, writes a report, and reviews quality with feedback loops.

    Args:
        query: Incident description including alarm name, time, and context
    """
    try:
        return run_rca(query)
    except Exception as e:
        return f"RCA agent error: {e}"


# ========================================================================
# Chatbot Agent
# ========================================================================

CHATBOT_PROMPT = """You are a friendly AIOps assistant chatbot. Answer in natural Korean (한국어).

Available Tools:
- logs_agent: CloudWatch Logs + CloudTrail 분석
- metrics_agent: CloudWatch Metrics 분석
- infrastructure_agent: AWS 리소스 상태 조사
- knowledge_agent: 런북/운영 이력 검색
- rca_agent: RCA 리포트 생성 (멀티에이전트 그래프로 정보수집→리포트작성→품질검증 수행)
- query_reports: DynamoDB에서 RCA 리포트 이력 조회 (날짜, 알람 이름으로 검색)
- query_alarms: CloudWatch 알람 목록 및 이력 조회

Guidelines:
- 사용자가 RCA를 요청하면 rca_agent tool을 사용하고, 결과를 자연어로 설명
- 사용자가 과거 알람/리포트를 물으면 query_reports 또는 query_alarms 사용
- 기술적 내용도 이해하기 쉽게 설명
- 필요하면 specialist agents를 직접 호출하여 실시간 정보 제공"""

chatbot_agent = Agent(
    model=model,
    system_prompt=CHATBOT_PROMPT,
    tools=[logs_agent, metrics_agent, infrastructure_agent, knowledge_agent,
           rca_agent, query_reports, query_alarms],
    callback_handler=None,
)


# ========================================================================
# Entrypoint
# ========================================================================

EXECUTOR_PROMPT = """You are an Executor Agent. You receive a specific approved remediation action and execute it safely.

You will receive:
- action description
- command (AWS CLI)
- code (boto3 Python)

Execution steps:
1. Use infrastructure_agent to check the CURRENT STATE of the affected resource
2. DRY-RUN FIRST: Execute the command with dry-run validation
   - For remediation_agent tools, verify the parameters match the current resource state
   - Example: before revoking a rule, confirm that exact rule (protocol, port, CIDR/SG) actually exists
   - Example: before authorizing a rule, confirm it doesn't already exist
3. If dry-run/validation fails, you MAY adjust ONLY technical parameters to fix the command:
   - ALLOWED to change: protocol (-1 vs tcp), port range (0 vs 80), CIDR format
   - NEVER change: resource IDs (instance ID, security group ID, target group ARN, etc.)
   - NEVER change: the intent of the action (e.g. don't switch authorize to revoke)
4. Execute the corrected command via remediation_agent
5. Use infrastructure_agent to verify the fix was applied (before/after comparison)

Return the execution result as JSON:
{"action_id": "...", "status": "COMPLETED" or "FAILED", "result": "what was done", "before": "state before", "after": "state after", "adjustments": "any parameter adjustments made and why"}

CRITICAL SAFETY RULES:
- NEVER modify resource identifiers (IDs, ARNs, names)
- NEVER change the action's intent or target
- If you cannot execute safely, return FAILED with explanation"""

# ========================================================================
# Memory Helper
# ========================================================================

MEMORY_ID = os.environ.get("MEMORY_ID", "")

def _get_memory_context(session_id: str) -> str:
    """Retrieve short-term + long-term memory for chatbot context."""
    if not MEMORY_ID:
        return ""
    import boto3
    try:
        client = boto3.client("bedrock-agentcore", region_name=REGION)
        context_parts = []

        # Short-term: recent conversation turns
        try:
            resp = client.list_events(memoryId=MEMORY_ID, sessionId=session_id, actorId="user", maxResults=10)
            events = resp.get("events", [])
            if events:
                turns = []
                for e in events[-6:]:  # last 6 turns
                    for msg in e.get("payload", {}).get("conversationalMessages", []):
                        role = msg.get("role", "")
                        text = msg.get("content", "")
                        turns.append(f"{role}: {text}")
                if turns:
                    context_parts.append("=== Recent Conversation ===\n" + "\n".join(turns))
        except Exception:
            pass

        # Long-term: semantic memory records
        try:
            resp = client.retrieve_memory_records(
                memoryId=MEMORY_ID, namespacePrefix="/",
                query={"text": "user preferences and past interactions"},
                maxResults=5)
            records = resp.get("memoryRecords", [])
            if records:
                facts = [r.get("content", {}).get("text", "") for r in records if r.get("content", {}).get("text")]
                if facts:
                    context_parts.append("=== Long-term Memory ===\n" + "\n".join(facts[:5]))
        except Exception:
            pass

        return "\n\n".join(context_parts)
    except Exception:
        return ""


def _save_to_memory(session_id: str, user_msg: str, assistant_msg: str):
    """Save conversation turn to short-term memory (long-term extracted automatically)."""
    if not MEMORY_ID:
        return
    import boto3
    try:
        client = boto3.client("bedrock-agentcore", region_name=REGION)

        # Ensure session exists
        try:
            client.create_session(memoryId=MEMORY_ID, sessionId=session_id, actorId="user")
        except Exception:
            pass  # session may already exist

        # Write conversation turn
        client.create_event(
            memoryId=MEMORY_ID, sessionId=session_id, actorId="user",
            payload={"conversationalMessages": [
                {"role": "user", "content": user_msg},
                {"role": "assistant", "content": assistant_msg[:2000]},
            ]})
    except Exception:
        pass  # non-critical, don't fail the response


app = BedrockAgentCoreApp()


def _rca_background(task_id, prompt, payload):
    """Background thread: run RCA Graph, update DDB, send notifications."""
    import boto3
    from urllib.request import Request, urlopen
    report_id = payload.get("report_id", "")
    workload_name = payload.get("workload_name", "")
    alarm_data = payload.get("alarm", {})
    notification = payload.get("notification", {})
    created_at = payload.get("created_at", "")

    ddb = boto3.resource("dynamodb", region_name=REGION)
    reports_table = ddb.Table(os.environ.get("REPORTS_TABLE", "aiops-demo-reports"))

    try:
        rca_result = run_rca(prompt)

        # Parse JSON from RCA result
        parsed = rca_result
        for _ in range(3):
            if isinstance(parsed, str):
                if "```json" in parsed:
                    match = re.search(r'```json\s*(\{.*?\})\s*```', parsed, re.DOTALL)
                    if match:
                        parsed = match.group(1)
                try:
                    parsed = json.loads(parsed)
                except (json.JSONDecodeError, TypeError):
                    break
            else:
                break

        if not isinstance(parsed, dict):
            parsed = {"summary": "RCA 분석이 완료되었으나 리포트 형식 변환에 실패했습니다.", "timeline": [], "root_cause": {"description": "리포트 파싱 실패 — 원본 데이터를 확인하세요.", "evidence": [str(parsed)[:300]], "confidence": "LOW"}, "impact": {}, "recommended_actions": []}

        report_data = {
            "report_id": report_id, "created_at": created_at, "workload": workload_name,
            "alarm": alarm_data, **parsed, "status": "PENDING",
        }
        status = "PENDING"
    except Exception as e:
        report_data = {
            "report_id": report_id, "created_at": created_at, "workload": workload_name,
            "alarm": alarm_data, "summary": f"분석 오류: {e}", "timeline": [],
            "root_cause": {"description": str(e), "evidence": [], "confidence": "LOW"},
            "impact": {}, "recommended_actions": [], "status": "FAILED",
        }
        status = "FAILED"

    # Update DDB
    try:
        reports_table.update_item(
            Key={"report_id": report_id},
            UpdateExpression="SET report_data = :rd, #s = :s",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={":rd": report_data, ":s": status},
        )
    except Exception:
        pass

    # Send notifications
    try:
        alarm_name = alarm_data.get("name", "")
        msg = f"AIOps Alert — {alarm_name}\n\n{report_data.get('summary', '')}\n\n📋 리포트 보기: {os.environ.get('WEB_URL', '')}/reports/{report_id}"
        slack_url = notification.get("slack_webhook", "")
        if slack_url:
            urlopen(Request(slack_url, data=json.dumps({"text": msg}).encode(), headers={"Content-Type": "application/json"}), timeout=5)
    except Exception:
        pass

    app.complete_async_task(task_id)


def _executor_background(task_id, prompt, payload):
    """Background thread: execute approved action, update DDB."""
    import boto3
    report_id = payload.get("report_id", "")
    action_id = payload.get("action_id", "")

    ddb = boto3.resource("dynamodb", region_name=REGION)
    reports_table = ddb.Table(os.environ.get("REPORTS_TABLE", "aiops-demo-reports"))

    try:
        executor = Agent(model=model, system_prompt=EXECUTOR_PROMPT,
                         tools=[remediation_agent, infrastructure_agent], callback_handler=None)
        result_text = str(executor(prompt))
        # Check if agent reported failure in its response
        if "FAILED" in result_text.upper() and "COMPLETED" not in result_text.upper():
            exec_status = "FAILED"
        else:
            exec_status = "COMPLETED"
    except Exception as e:
        result_text = f"실행 오류: {e}"
        exec_status = "FAILED"

    # Update DDB
    try:
        item = reports_table.get_item(Key={"report_id": report_id}).get("Item", {})
        rd = item.get("report_data", {})
        for a in rd.get("recommended_actions", []):
            if a.get("action_id") == action_id:
                a["status"] = exec_status
                a["execution_result"] = result_text[:2000]
                break
        all_done = all(a.get("status") in ("COMPLETED", "FAILED", "PENDING_APPROVAL") for a in rd.get("recommended_actions", []))
        report_status = "RESOLVED" if all_done and any(a.get("status") == "COMPLETED" for a in rd.get("recommended_actions", [])) else item.get("status", "APPROVED")
        reports_table.update_item(
            Key={"report_id": report_id},
            UpdateExpression="SET report_data = :rd, #s = :s",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={":rd": rd, ":s": report_status},
        )
    except Exception:
        pass

    app.complete_async_task(task_id)


@app.entrypoint
def invoke(payload):
    prompt = payload.get("prompt", "")
    agent_name = payload.get("agent", "chatbot")
    session_id = payload.get("session_id", "")

    if not prompt:
        return json.dumps({"error": "No prompt provided"})

    if agent_name == "rca":
        # Async: start background task, return immediately
        import threading
        task_id = app.add_async_task("rca_analysis")
        payload["created_at"] = payload.get("created_at", "")
        t = threading.Thread(target=_rca_background, args=(task_id, prompt, payload), daemon=True)
        t.start()
        return json.dumps({"status": "ANALYZING", "report_id": payload.get("report_id", ""), "task_id": task_id})
    elif agent_name == "executor":
        import threading
        task_id = app.add_async_task("executor")
        t = threading.Thread(target=_executor_background, args=(task_id, prompt, payload), daemon=True)
        t.start()
        return json.dumps({"status": "EXECUTING", "report_id": payload.get("report_id", ""), "task_id": task_id})
    else:
        # Chatbot with memory
        memory_ctx = _get_memory_context(session_id) if session_id else ""
        if memory_ctx:
            full_prompt = f"{memory_ctx}\n\n=== Current Question ===\n{prompt}"
        else:
            full_prompt = prompt

        response = chatbot_agent(full_prompt)
        result = str(response)

        # Save to memory (async-safe, non-blocking)
        if session_id:
            _save_to_memory(session_id, prompt, result)

        return result


if __name__ == "__main__":
    app.run()
