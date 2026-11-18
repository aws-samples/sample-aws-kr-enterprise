"""Incident agents on Kiro: RCA loop (Collector, Writer, Reviewer), the Fixer that opens a pull request, and the Chatbot."""
import json
import logging
import os
import re
import time
from datetime import datetime, timezone

import boto3
from boto3.dynamodb.conditions import Key
from incident_tools import repository
from kiro_agent_sdk import KiroSDKClient

import kiro_runtime
from kiro_runtime import ask

logger = logging.getLogger("incident.agents")
REGION = kiro_runtime.REGION
MAX_ITERATIONS = 3

# ========================================================================
# RCA loop: Collector → Writer → Reviewer → (loop back or finish)
# ========================================================================


def _verdict(review):
    """Reviewer verdict as "VERDICT: PASS" or a JSON "verdict" field.

    A missing or unreadable verdict fails, and so do conflicting ones (for example an example PASS and an actual FAIL).
    """
    verdicts = set(re.findall(r'VERDICT"?\s*[:=]\s*"?(PASS|FAIL)', review.upper()))
    return "PASS" if verdicts == {"PASS"} else "FAIL"


def _node_input(task, source, output):
    return f"{task}\n\n=== Output from {source} ===\n{output}"


async def run_rca(prompt):
    """Each node gets the original task plus the previous node's output, on a fresh Kiro session per visit.

    Returns the report text and whether the reviewer passed it within MAX_ITERATIONS.
    """
    review, report, passed = None, None, False
    for iteration in range(1, MAX_ITERATIONS + 1):
        collector_input = prompt if review is None else _node_input(prompt, "reviewer", review)
        collected = await ask("collector", collector_input)
        report = await ask("writer", _node_input(prompt, "collector", collected))
        review = await ask("reviewer", _node_input(prompt, "writer", report))
        logger.info("RCA iteration %s verdict %s", iteration, _verdict(review))
        if _verdict(review) == "PASS":
            passed = True
            break
    return _extract_json(report), passed


def _extract_json(text):
    if "```json" in text:
        match = re.search(r'```json\s*(\{.*\})\s*```', text, re.DOTALL)
        if match:
            text = match.group(1)
    elif "```" in text:
        match = re.search(r'```\s*(\{.*\})\s*```', text, re.DOTALL)
        if match:
            text = match.group(1)
    if not text.strip().startswith("{"):
        match = re.search(r'\{.*"summary".*\}', text, re.DOTALL)
        if match:
            text = match.group(0)
    return text


# Fields the Writer may set (the rca-report skill). Everything else in a report, such as its ID, alarm, workload,
# status and pull request, is written by the platform, so model output for them is dropped.
REPORT_FIELDS = ("summary", "timeline", "root_cause", "impact", "recommended_actions")


def parse_report(rca_result):
    parsed = rca_result
    for _ in range(3):
        if isinstance(parsed, str):
            try:
                parsed = json.loads(parsed)
            except (json.JSONDecodeError, TypeError):
                break
        else:
            break
    if not isinstance(parsed, dict):
        parsed = {"summary": "RCA 분석이 완료되었으나 리포트 형식 변환에 실패했습니다.", "timeline": [],
                  "root_cause": {"description": "리포트 파싱 실패 — 원본 데이터를 확인하세요.",
                                 "evidence": [str(parsed)[:300]], "confidence": "LOW"},
                  "impact": {}, "recommended_actions": []}
    return {key: parsed[key] for key in REPORT_FIELDS if key in parsed}


# ========================================================================
# Fixer: the fix as a pull request to the workload repository
# ========================================================================

async def run_fixer(prompt, report_id, fix_dir):
    """Fixer turn for one reviewed report. repository.read_fix_result, not the model's text, tells what it opened."""
    return await ask("fixer", prompt, extra_env={repository.FIX_DIR_ENV: str(fix_dir),
                                                 repository.REPORT_ID_ENV: report_id})


# ========================================================================
# Chatbot with memory
# ========================================================================
# Short-term: the Kiro session of this runtime session keeps the whole conversation while the microVM lives.
#   The last 6 turns are also kept in DynamoDB so a new microVM for the same chat session can continue.
# Long-term: Kiro memories. They are persisted in DynamoDB and restored into Kiro when a microVM starts.
#   A memory expires MEMORY_DAYS after its last change; expired items are skipped before DynamoDB TTL removes them.

MEMORY_TABLE = os.environ.get("MEMORY_TABLE", "")
MEMORY_DAYS = 30
_chat = {"session_id": None, "client": None, "memories": {}}


def _memory_table():
    return boto3.resource("dynamodb", region_name=REGION).Table(MEMORY_TABLE)


def _recent_turns(session_id):
    if not MEMORY_TABLE:
        return []
    item = _memory_table().get_item(Key={"pk": f"session#{session_id}", "sk": "turns"}).get("Item") or {}
    if int(item.get("expires_at", 0)) <= time.time():
        return []
    return item.get("turns", [])[-6:]


def _save_turn(session_id, user_msg, assistant_msg):
    if not MEMORY_TABLE:
        return
    turns = _recent_turns(session_id) + [{"user": user_msg, "assistant": assistant_msg[:2000],
                                         "at": datetime.now(timezone.utc).isoformat()}]
    _memory_table().put_item(Item={"pk": f"session#{session_id}", "sk": "turns", "turns": turns[-6:],
                                   "expires_at": int(time.time()) + 30 * 86400})


async def _restore_memories(client):
    """Restore unexpired memories and return {title: content} of what Kiro now holds."""
    restored = {}
    if not MEMORY_TABLE:
        return restored
    items = _memory_table().query(KeyConditionExpression=Key("pk").eq("memory#user"))
    for item in items.get("Items", []):
        if int(item.get("expires_at", 0)) <= time.time():
            continue
        try:
            await client.create_kiro_memory(memory_type=item.get("memory_type", "SEMANTIC"), scopes=["Global"],
                                            title=item["title"], summary=item.get("summary", item["title"]),
                                            content=item["content"])
            restored[item["title"]] = item["content"]
        except Exception:
            logger.warning("could not restore memory %s", item.get("title"))
    return restored


async def _persist_memories(client):
    if not MEMORY_TABLE:
        return
    try:
        page = await client.list_kiro_memories(limit=50, scopes=["Global"])
    except Exception:
        return
    table, known = _memory_table(), _chat["memories"]
    for memory in page.get("memories", []):
        title = memory.get("title")
        if not title or not memory.get("content") or known.get(title) == memory["content"]:
            continue  # unchanged memories keep their original expiry
        table.put_item(Item={"pk": "memory#user", "sk": title, "title": title,
                             "summary": memory.get("summary") or title, "content": memory["content"],
                             "memory_type": memory.get("memoryType") or memory.get("type") or "SEMANTIC",
                             "updated_at": datetime.now(timezone.utc).isoformat(),
                             "expires_at": int(time.time()) + MEMORY_DAYS * 86400})
        known[title] = memory["content"]


async def _chat_client(session_id):
    if _chat["client"] is not None and _chat["session_id"] == session_id:
        return _chat["client"], False
    if _chat["client"] is not None:
        await _chat["client"].disconnect()
    kiro_runtime.prepare()
    client = KiroSDKClient(kiro_runtime.options("chatbot"))
    await client.connect()
    memories = await _restore_memories(client)
    _chat.update(session_id=session_id, client=client, memories=memories)
    return client, True


async def run_chat(session_id, prompt):
    client, fresh = await _chat_client(session_id or "default")
    full_prompt = prompt
    if fresh and session_id:
        turns = _recent_turns(session_id)
        if turns:
            history = "\n".join(f"user: {t['user']}\nassistant: {t['assistant']}" for t in turns)
            full_prompt = f"=== Recent Conversation ===\n{history}\n\n=== Current Question ===\n{prompt}"
    await client.query(full_prompt)
    result = await kiro_runtime.response(client)
    if session_id:
        _save_turn(session_id, prompt, result)
    await _persist_memories(client)
    return result


async def prewarm_chat(session_id):
    await _chat_client(session_id or "default")
