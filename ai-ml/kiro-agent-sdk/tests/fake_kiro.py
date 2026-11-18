#!/usr/bin/env python3
"""Small ACP fixture, not a Kiro implementation."""
import json
import os
import sys
import time

if len(sys.argv) > 1 and sys.argv[1] == "--version":
    print(os.environ.get("FAKE_VERSION", "kiro-cli 2.35.1"))
    sys.exit(0)

if len(sys.argv) > 1 and sys.argv[1] == "chat":
    if "--list-sessions" in sys.argv:
        if os.environ.get("FAKE_LIST_SESSIONS_HANG"):
            time.sleep(60)
        print(json.dumps([{"cwd": "/tmp", "sessions": [{"sessionId": "one", "source": "v2"}]}]))
    elif "--list-models" in sys.argv:
        if os.environ.get("FAKE_LIST_MODELS_HANG"):
            time.sleep(60)
        model = {"model_id": "example"}
        if os.environ.get("FAKE_REPORT_CWD"):
            model.update({"processCwd": os.getcwd(), "processPwd": os.environ.get("PWD")})
        default = os.environ.get("FAKE_DEFAULT_MODEL")
        result = {"models": [model]}
        if default is not None:
            result["default_model"] = default
            if default != "example" and os.environ.get("FAKE_UNLISTED_DEFAULT") != "1":
                result["models"].append({"model_id": default})
        print(json.dumps(result))
    sys.exit(0)

if os.environ.get("FAKE_STDERR"):
    print("fake diagnostic", file=sys.stderr, flush=True)
if os.environ.get("FAKE_AUTH_STDERR_SECRET"):
    print(os.environ["FAKE_AUTH_STDERR_SECRET"], file=sys.stderr, flush=True)


def send(message):
    print(json.dumps(message), flush=True)

elicitation_request_correlation = None

def read_interaction_reply():
    while True:
        message = json.loads(sys.stdin.readline())
        if (os.environ.get("FAKE_LOG_INTERACTION_REPLIES") and os.environ.get("FAKE_LOG_REQUESTS") and
                "method" not in message):
            with open(os.environ["FAKE_LOG_REQUESTS"], "a", encoding="utf-8") as output:
                output.write(json.dumps(message) + "\n")
        if message.get("method") == "session/cancel" and os.environ.get("FAKE_IGNORE_SESSION_CANCEL_WHILE_WAITING"):
            continue
        if message.get("method") == "_kiro/test/cancel-elicitation" and elicitation_request_correlation is not None:
            send({"jsonrpc": "2.0", "method": "_kiro/sdk/cancelHook", "params": elicitation_request_correlation})
            send({"jsonrpc": "2.0", "id": message["id"], "result": {"cancelled": True}})
            continue
        if message.get("method") != "_session/steer":
            return message
        if os.environ.get("FAKE_LOG_REQUESTS"):
            with open(os.environ["FAKE_LOG_REQUESTS"], "a", encoding="utf-8") as output:
                output.write(json.dumps(message) + "\n")
        send({"jsonrpc": "2.0", "id": message["id"],
              "result": {"queued": not bool(os.environ.get("FAKE_NOTIFICATION_STEER_FAIL"))}})

connection_request_policy = {}
connection_request_count = 0
roots_tickets = {}
session_title = ["Fake session"]
roots_ticket_count = 0

def mcp_connection_request(phase, overrides=None):
    global connection_request_count
    connection_request_count += 1
    params = {"protocol": 1, "connectionId": connection_request_policy.get("id", "not-enabled"),
        "serverConnectionId": "server-connection-1", "callId": "request-" + str(connection_request_count),
        "serverName": "policy", "method": "roots/list", "params": {}, "phase": phase}
    params.update(overrides or {})
    if os.environ.get("FAKE_MCP_CONNECTION_CANCEL_FIRST"):
        send({"jsonrpc": "2.0", "method": "_kiro/sdk/mcp/cancelConnectionRequest",
              "params": {name: params[name] for name in ("connectionId", "serverConnectionId", "callId")}})
    send({"jsonrpc": "2.0", "id": "connection-" + str(connection_request_count),
          "method": "_kiro/sdk/mcp/connectionRequest", "params": params})
    response = read_interaction_reply()
    if os.environ.get("FAKE_MCP_CONNECTION_LOG"):
        with open(os.environ["FAKE_MCP_CONNECTION_LOG"], "a", encoding="utf-8") as output:
            output.write(json.dumps(response) + "\n")
    return response

def require_client_auth(ident, *, failure_reply=True, early_result=None):
    send({"jsonrpc": "2.0", "id": "client-auth", "method": "_kiro/auth/getAccessToken", "params": {}})
    if early_result is not None:
        send({"jsonrpc": "2.0", "id": ident, "result": early_result})
    reply = read_interaction_reply()
    result = reply.get("result")
    valid = (isinstance(result, dict) and result.get("accessToken") == os.environ.get("FAKE_EXPECT_TOKEN") and
             isinstance(result.get("expiresAt"), str))
    if os.environ.get("FAKE_EXPECT_AUTH_METHOD"):
        valid = valid and result.get("authMethod") == os.environ["FAKE_EXPECT_AUTH_METHOD"]
    if os.environ.get("FAKE_EXPECT_AUTH_PROVIDER"):
        valid = valid and result.get("provider") == os.environ["FAKE_EXPECT_AUTH_PROVIDER"]
    if os.environ.get("FAKE_AUTH_STATUS"):
        with open(os.environ["FAKE_AUTH_STATUS"], "a", encoding="utf-8") as output:
            output.write(json.dumps({"valid": valid, "has_profile": bool(result and result.get("profileArn")),
                                     "flag": "--auth-method" in sys.argv and
                                     sys.argv[sys.argv.index("--auth-method") + 1] == "client",
                                     "api_key_absent": not bool(os.environ.get("KIRO_API_KEY"))}) + "\n")
    if not valid and failure_reply:
        send({"jsonrpc": "2.0", "id": ident, "error": {
            "code": -32603, "message": os.environ.get("FAKE_AUTH_ERROR_SECRET", "authentication failed")}})
    return valid

mode = None
policy_presets = []
thinking_value = 'on'
effort_value = 'high'
model_value = 'auto'
context_files = []
for line in sys.stdin:
    req = json.loads(line)
    if os.environ.get("FAKE_LOG_REQUESTS"):
        with open(os.environ["FAKE_LOG_REQUESTS"], "a", encoding="utf-8") as output:
            output.write(json.dumps(req) + "\n")
    method = req.get("method")
    ident = req.get("id")
    params = req.get("params", {})
    if method == "_kiro/mcp/toggle" and "id" not in req:
        continue
    if method == "initialize":
        connection_request_policy = params.get("clientCapabilities", {}).get("_meta", {}).get(
            "kiro", {}).get("sdkElicitation", {}).get("serverRequests", {}).get("connection", {})
        if os.environ.get("FAKE_CLIENT_AUTH_INIT") and not require_client_auth(ident):
            continue
        if os.environ.get("FAKE_INITIALIZE_HANG"):
            continue
        if os.environ.get("FAKE_BAD_JSON"):
            print(os.environ.get("FAKE_BAD_JSON_SECRET", "not-json"), flush=True)
            continue
        result = {"protocolVersion": 1, "agentCapabilities": {"loadSession": True, "mcpCapabilities": {"http": True, "sse": False},
            "sessionCapabilities": {"list": {}, "fork": {"_meta": {"kiro": {"messageId": True}}}, "delete": {}, "close": {}},
            "_meta": {"kiro": {"checkpoints": True, "extensionMethods": ["_kiro/session/compact", "_kiro/session/history", "_kiro/session/export"]}}}}
        if os.environ.get("FAKE_NATIVE_ACP") or os.environ.get("FAKE_RUNTIME_POLICIES"):
            result["agentCapabilities"]["sdkNativeHooks"] = {"protocol": 1,
                "sourceSha256": "bf79b2b0fa7ae68653abdf117b4a4dbf064fec6fa7ca49500115f3f2f67581b1"}
        if os.environ.get("FAKE_RUNTIME_POLICIES"):
            result["agentCapabilities"]["sdkRuntimePolicies"] = 7
            if not os.environ.get("FAKE_NO_MCP_BOOTSTRAP"):
                result["agentCapabilities"]["sdkMcpBootstrap"] = {"protocol": 1, "deferred": True}
            if not os.environ.get("FAKE_NO_ELICITATION_POLICY"):
                result["agentCapabilities"]["sdkElicitation"] = params.get(
                    "clientCapabilities", {}).get("_meta", {}).get("kiro", {}).get("sdkElicitation")
            if not os.environ.get("FAKE_NO_SESSION_POLICIES"):
                result["agentCapabilities"]["sdkSessionPolicies"] = params.get(
                    "clientCapabilities", {}).get("_meta", {}).get("kiro", {}).get("sdkSessionPolicies")
        if not os.environ.get("FAKE_NO_CODE_INTELLIGENCE"):
            result["agentCapabilities"]["_meta"]["kiro"]["extensionMethods"].append("_kiro/codeIntelligence")
        if os.environ.get("FAKE_PROMPT_CAPABILITIES"):
            result["agentCapabilities"]["promptCapabilities"] = {"image": True, "embeddedContext": True}
        if os.environ.get("FAKE_NO_FORK"):
            result["agentCapabilities"]["sessionCapabilities"].pop("fork")
        if os.environ.get("FAKE_MCP_CONNECTION_INITIAL"):
            mcp_connection_request("initializing")
    elif method == "_kiro/test/mcp-connection":
        result = {"response": mcp_connection_request(params.get("phase", "idle"), params.get("overrides"))}
    elif method == "_kiro/sdk/mcp/rootsChanged":
        action = params.get("action")
        if os.environ.get("FAKE_ROOTS_NOTIFICATION_LOG"):
            with open(os.environ["FAKE_ROOTS_NOTIFICATION_LOG"], "a", encoding="utf-8") as output:
                output.write(json.dumps(params) + "\n")
        if action == "prepare":
            roots_ticket_count += 1
            ticket = "roots-ticket-" + str(roots_ticket_count)
            ids = ["server-connection-1", "server-connection-2"]
            roots_tickets[ticket] = ids
            result = {"protocol": 1, "ticket": ticket, "connectionIds": ids}
        elif action == "commit":
            ids = roots_tickets.pop(params["ticket"])
            statuses = ["sent", os.environ.get("FAKE_ROOTS_NOTIFICATION_SECOND", "sent")]
            result = {"protocol": 1, "ticket": params["ticket"],
                      "results": [{"connectionId": key, "status": status} for key, status in zip(ids, statuses)]}
            if os.environ.get("FAKE_ROOTS_NOTIFICATION_BAD_REPORT"):
                result["results"][0]["connectionId"] = "foreign"
        else:
            roots_tickets.pop(params.get("ticket"), None)
            result = {"protocol": 1, "cancelled": True}
    elif method == "session/new":
        policy_presets = params.get("_meta", {}).get("kiro", {}).get("policyPreset", [])
        default_mode = sys.argv[sys.argv.index("--agent") + 1] if "--agent" in sys.argv else "vibe"
        mode = os.environ.get("FAKE_NEW_MODE", params.get("_meta", {}).get("kiro", {}).get("modeId", default_mode))
        model_value = params.get("_meta", {}).get("kiro", {}).get("modelId", "auto")
        if os.environ.get("FAKE_NEW_MODEL_IGNORED"):
            model_value = "auto"
        session_id = params.get("_meta", {}).get("kiro", {}).get(
            "overrideSessionId", os.environ.get("FAKE_DEFAULT_SESSION_ID", "test-session"))
        if os.environ.get("FAKE_IGNORE_SESSION_ID"):
            session_id = "test-session"
        if "FAKE_STARTUP_COMMANDS" in os.environ:
            send({"jsonrpc": "2.0", "method": "session/update", "params": {
                "sessionId": session_id, "update": {"sessionUpdate": "available_commands_update",
                                                     "availableCommands": json.loads(os.environ["FAKE_STARTUP_COMMANDS"])}}})
        result = {"sessionId": session_id, "received": params, "argv": sys.argv, "modes": {"currentModeId": mode},
                  "configOptions": [{"id": "model", "currentValue": model_value}]}
        if os.environ.get("FAKE_DUPLICATE_NEW_MODEL_CONFIG"):
            result["configOptions"].append({"id": "model", "currentValue": "other"})
        if os.environ.get("FAKE_NULL_NEW_CONFIG"):
            result["configOptions"] = None
        if os.environ.get("FAKE_REPORT_CWD"):
            result.update({"processCwd": os.getcwd(), "processPwd": os.environ.get("PWD")})
        if os.environ.get("FAKE_MODEL_OPTIONS"):
            result["configOptions"][0]["options"] = [{"value": "auto"}, {"value": "model-one"},
                                                       {"value": "model-two"}]
        if os.environ.get("FAKE_NO_MODE"):
            result["modes"] = {}
    elif method == "session/load":
        policy_presets = params.get("_meta", {}).get("kiro", {}).get("policyPreset", [])
        if os.environ.get("FAKE_LOAD_HANG"):
            continue
        if os.environ.get("FAKE_LOAD_FAIL"):
            send({"jsonrpc": "2.0", "id": ident, "error": {"code": -32603, "message": "load failed"}})
            continue
        if os.environ.get("FAKE_REPLAY_SUBAGENT"):
            send({"jsonrpc": "2.0", "method": "session/update", "params": {"sessionId": params["sessionId"],
                 "update": {"sessionUpdate": "tool_call", "toolCallId": "spawn", "title": "Sub-agent",
                            "_meta": {"kiro": {"kind": "agent-subtask", "agentSubtaskId": "child-1"}}}}})
        send({"jsonrpc": "2.0", "method": "session/update", "params": {"sessionId": params["sessionId"], "update": {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "replayed"}}}})
        result = {"modes": {"currentModeId": os.environ.get("FAKE_LOAD_MODE", mode or "vibe")}}
    elif method == "_session/steer/clear":
        if os.environ.get("FAKE_STEER_CLEAR_FAIL"):
            send({"jsonrpc": "2.0", "id": ident, "error": {"code": -32601, "message": "unsupported"}})
            continue
        result = {"cleared": True, "messageIds": []}
    elif method == "session/prompt":
        if (os.environ.get("FAKE_RUNTIME_BACKGROUND_AGENT") and
                params.get("_meta", {}).get("kiro", {}).get("sdkRuntime", {}).get("backgroundAgents")):
            request = {"protocol": 2, "sessionId": params["sessionId"], "callId": "background-call",
                "requestId": params["_meta"]["kiro"]["sdkRuntime"]["requestId"],
                "agentName": params["_meta"]["kiro"]["sdkRuntime"]["backgroundAgents"][0],
                "prompt": os.environ.get("FAKE_BACKGROUND_CHILD_PROMPT", "background child"), "modelId": "model-one",
                "execution": {"id": "main-execution", "depth": 0}}
            request["context"] = ({"source": "parent-execution-text",
                "messages": [{"role": "user", "text": "Parent requirement"}],
                "omitted_messages": 1, "omitted_entries": 2}
                if params["_meta"]["kiro"]["sdkRuntime"]["backgroundContext"] == "parent-text" else None)
            if os.environ.get("FAKE_BACKGROUND_MISSING_CONTEXT"):
                request.pop("context")
            if os.environ.get("FAKE_BACKGROUND_STALE"):
                request["requestId"] = "stale"
            send({"jsonrpc": "2.0", "id": "background-start", "method": "_kiro/sdk/backgroundAgent", "params": request})
            reply = read_interaction_reply()
            if "error" in reply and reply["error"].get("code") != -32000:
                send({"jsonrpc": "2.0", "id": ident, "error": {"code": -32603, "message": "Background dispatch failed"}})
                continue
        if os.environ.get("FAKE_RUNTIME_SDK_POSTTOOL"):
            posttool = {"protocol": 1, "sessionId": params["sessionId"], "callId": "post-correlation",
                "requestId": params.get("_meta", {}).get("kiro", {}).get("sdkRuntime", {}).get("requestId"),
                "execution": {"id": "main-execution", "depth": 0},
                "tool": {"id": "real-tool-id", "name": "shell", "input": {"command": "probe"},
                         "output": "original", "success": not bool(os.environ.get("FAKE_RUNTIME_SDK_POSTTOOL_FAILURE"))}}
            if os.environ.get("FAKE_RUNTIME_SDK_POSTTOOL_CHILD"):
                posttool["execution"] = {"id": "child-execution", "depth": 1,
                                         "parentId": "main-execution", "agentType": "worker"}
            if os.environ.get("FAKE_RUNTIME_SDK_POSTTOOL_STALE"):
                posttool["requestId"] = "stale"
            if os.environ.get("FAKE_RUNTIME_SDK_POSTTOOL_EARLY_CANCEL"):
                send({"jsonrpc": "2.0", "method": "_kiro/sdk/cancelHook", "params": {
                    key: posttool[key] for key in ("sessionId", "requestId", "callId")}})
            send({"jsonrpc": "2.0", "id": "runtime-sdk-posttool", "method": "_kiro/sdk/postTool",
                  "params": posttool})
            hook_reply = read_interaction_reply()
            if "error" in hook_reply:
                send({"jsonrpc": "2.0", "id": ident, "error": {"code": -32603, "message": "SDK post-tool failed"}})
                continue
        if os.environ.get("FAKE_RUNTIME_SUBAGENT_STOP"):
            child_stop = {"protocol": 1, "sessionId": params["sessionId"],
                "callId": "runtime-stop-correlation",
                "requestId": params.get("_meta", {}).get("kiro", {}).get("sdkRuntime", {}).get("requestId"),
                "execution": {"id": "child-execution", "depth": 1,
                              "parentId": "main-execution", "agentType": "research"},
                "stopHookActive": False, "lastAssistantMessage": "child result"}
            if os.environ.get("FAKE_RUNTIME_SUBAGENT_STOP_STALE"):
                child_stop["requestId"] = "expired"
            send({"jsonrpc": "2.0", "id": "runtime-subagent-stop", "method": "_kiro/sdk/subagentStop",
                  "params": child_stop})
            hook_reply = read_interaction_reply()
            if "error" in hook_reply:
                send({"jsonrpc": "2.0", "id": ident, "error": {
                    "code": -32603, "message": "SDK SubagentStop failed"}})
                continue
        if os.environ.get("FAKE_RUNTIME_TOOL_BATCH"):
            batch = {"protocol": 1, "sessionId": params["sessionId"], "callId": "batch-correlation",
                "requestId": params.get("_meta", {}).get("kiro", {}).get("sdkRuntime", {}).get("requestId"),
                "execution": {"id": "main-execution", "depth": 0},
                "batch": {"messageId": "model-message", "tools": [
                    {"tool_call_id": "tool-one", "tool_name": "read", "success": True},
                    {"tool_call_id": "tool-two", "tool_name": "shell", "success": False}]}}
            if os.environ.get("FAKE_RUNTIME_TOOL_BATCH_STALE"):
                batch["requestId"] = "expired"
            if os.environ.get("FAKE_RUNTIME_TOOL_BATCH_DUPLICATE"):
                batch["batch"]["tools"][1]["tool_call_id"] = "tool-one"
            send({"jsonrpc": "2.0", "id": "runtime-batch", "method": "_kiro/sdk/postToolBatch", "params": batch})
            reply = read_interaction_reply()
            if "error" in reply:
                send({"jsonrpc": "2.0", "id": ident, "error": {"code": -32603, "message": "Batch hook unavailable"}})
                continue
        if os.environ.get("FAKE_RUNTIME_SUBAGENT_START"):
            child_start = {"protocol": 1, "sessionId": params["sessionId"],
                "callId": "runtime-start-correlation",
                "requestId": params.get("_meta", {}).get("kiro", {}).get("sdkRuntime", {}).get("requestId"),
                "execution": {"id": "child-execution", "depth": 1,
                              "parentId": "main-execution", "agentType": "research"}}
            if os.environ.get("FAKE_RUNTIME_SUBAGENT_START_STALE"):
                child_start["requestId"] = "expired"
            if os.environ.get("FAKE_RUNTIME_SUBAGENT_START_EARLY_CANCEL"):
                send({"jsonrpc": "2.0", "method": "_kiro/sdk/cancelHook", "params": {
                    key: child_start[key] for key in ("sessionId", "requestId", "callId")}})
            send({"jsonrpc": "2.0", "id": "runtime-subagent-start", "method": "_kiro/sdk/subagentStart",
                  "params": child_start})
            if os.environ.get("FAKE_RUNTIME_SUBAGENT_START_CANCEL"):
                send({"jsonrpc": "2.0", "method": "_kiro/sdk/cancelHook", "params": {
                    key: child_start[key] for key in ("sessionId", "requestId", "callId")}})
            hook_reply = read_interaction_reply()
            child_cancelled = (os.environ.get("FAKE_RUNTIME_SUBAGENT_START_CANCEL") or
                               os.environ.get("FAKE_RUNTIME_SUBAGENT_START_EARLY_CANCEL"))
            if child_cancelled and hook_reply.get("error", {}).get("code") != -32800:
                send({"jsonrpc": "2.0", "id": ident, "error": {
                    "code": -32603, "message": "Cancelled child received a late or invalid approval"}})
                continue
            if "error" in hook_reply and child_cancelled:
                # A scoped child fault is an outer tool result, not a failed
                # parent session/prompt RPC. Make this fixture event one-shot.
                for flag in ("FAKE_RUNTIME_SUBAGENT_START", "FAKE_RUNTIME_SUBAGENT_START_CANCEL",
                             "FAKE_RUNTIME_SUBAGENT_START_EARLY_CANCEL"):
                    os.environ.pop(flag, None)
            elif "error" in hook_reply:
                send({"jsonrpc": "2.0", "id": ident, "error": {
                    "code": -32603, "message": "SDK SubagentStart failed"}})
                continue
        if os.environ.get("FAKE_RUNTIME_PRECOMPACT"):
            precompact = {"protocol": 2, "sessionId": params["sessionId"], "trigger": "auto", "reason": "context",
                          "execution": {"id": "main-execution", "depth": 0},
                          "requestId": params.get("_meta", {}).get("kiro", {}).get("sdkRuntime", {}).get("requestId")}
            if os.environ.get("FAKE_RUNTIME_PRECOMPACT_CHILD"):
                precompact["execution"] = {"id": "child-execution", "depth": 1,
                                           "parentId": "main-execution", "agentType": "research"}
                precompact["reason"] = "model-handoff"
            if os.environ.get("FAKE_RUNTIME_PRECOMPACT_STALE"):
                precompact["requestId"] = "stale"
            send({"jsonrpc": "2.0", "id": "runtime-precompact", "method": "_kiro/sdk/preCompact",
                  "params": precompact})
            hook_reply = read_interaction_reply()
            if "error" in hook_reply or hook_reply.get("result", {}).get("allow") is not True:
                send({"jsonrpc": "2.0", "id": ident, "error": {
                    "code": -32603, "message": "SDK PreCompact rejected compaction"}})
                continue
        if os.environ.get("FAKE_NATIVE_ACP_PROMPT"):
            profile = os.path.join(os.environ["KIRO_HOME"], "agents", str(mode) + ".json")
            hook = json.load(open(profile, encoding="utf-8"))["hooks"][0]
            send({"jsonrpc": "2.0", "id": "native-acp-prompt-hook", "method": "_kiro/sdk/hooks/callback",
                  "params": {"protocol": 1, "command": hook["action"]["command"],
                             "hookId": f"{mode}#hook-0", "sessionId": params["sessionId"], "timeoutMs": 2000,
                             "input": {"session_id": params["sessionId"], "hook_event_name": hook["trigger"],
                                       "tool_name": "shell", "tool_input": {"command": "probe"}}}})
            hook_reply = read_interaction_reply()
            if "error" in hook_reply or hook_reply.get("result", {}).get("exitCode") != 0:
                send({"jsonrpc": "2.0", "id": ident, "result": {"stopReason": "refusal"}})
                continue
        if os.environ.get("FAKE_CLIENT_AUTH_TURN") and not require_client_auth(ident):
            continue
        text = params["prompt"][0]["text"]
        if "FAKE_MODEL_HOOK_DECISION" in os.environ:
            text = os.environ["FAKE_MODEL_HOOK_DECISION"]
        if text == "process_uid_probe":
            text = str(os.geteuid())
        elif text == "env_probe":
            text = os.environ.get("TEST_KIRO_ENV", "<missing>")
        elif text == "env_auth_probe":
            text = "auth-present" if os.environ.get("KIRO_API_KEY") else "auth-absent"
        if text == "barrier_output" and os.environ.get("FAKE_OUTPUT_BARRIER_DIR"):
            directory = os.environ["FAKE_OUTPUT_BARRIER_DIR"]
            send({"jsonrpc": "2.0", "method": "session/update", "params": {
                "sessionId": params["sessionId"], "update": {"sessionUpdate": "agent_message_chunk",
                "content": {"type": "text", "text": "EARLY"}}}})
            with open(os.path.join(directory, "started"), "w", encoding="utf-8") as marker:
                marker.write("started")
            deadline = time.monotonic() + 20
            while not os.path.exists(os.path.join(directory, "release")) and time.monotonic() < deadline:
                time.sleep(0.01)
            text = "PRIVATE-LATE"
        if text == "plan_mode_drift":
            send({"jsonrpc": "2.0", "method": "session/update", "params": {"sessionId": params["sessionId"],
                "update": {"sessionUpdate": "current_mode_update", "currentModeId": "other"}}})
        if text == "unexpected_plan":
            send({"jsonrpc": "2.0", "method": "session/update", "params": {"sessionId": params["sessionId"],
                "update": {"sessionUpdate": "current_mode_update", "currentModeId": "plan"}}})
        if text != "cancel":
            send({"jsonrpc": "2.0", "method": "session/update", "params": {"sessionId": params["sessionId"], "update": {"sessionUpdate": "session_info_update", "_meta": {"kiro": {"kind": "user_message_id_assigned", "userMessageId": "user-1"}}}}})
        if text == "thought":
            send({"jsonrpc": "2.0", "method": "session/update", "params": {"sessionId": params["sessionId"], "update": {"sessionUpdate": "agent_thought_chunk", "content": {"type": "text", "text": "private thought"}}}})
        if text in ("tool_failure", "tool_failure_steer"):
            send({"jsonrpc": "2.0", "method": "session/update", "params": {"sessionId": params["sessionId"],
                "update": {"sessionUpdate": "tool_call", "toolCallId": "failed-1", "title": "probe",
                           "status": "in_progress", "rawInput": {"value": "test"}}}})
            send({"jsonrpc": "2.0", "method": "session/update", "params": {"sessionId": params["sessionId"],
                "update": {"sessionUpdate": "tool_call_update", "toolCallId": "failed-1",
                           "status": "failed", "rawOutput": "SIMULATED_ERROR"}}})
            if text == "tool_failure_steer":
                steering = json.loads(sys.stdin.readline())
                if steering.get("method") != "_session/steer":
                    raise RuntimeError("Expected a failure-hook steering request")
                send({"jsonrpc": "2.0", "id": steering["id"], "result": {"queued": True}})
                text = steering["params"]["message"]
        if text == "posttool_steer":
            send({"jsonrpc": "2.0", "method": "session/update", "params": {"sessionId": params["sessionId"],
                "update": {"sessionUpdate": "tool_call", "toolCallId": "post-1", "title": "probe",
                           "status": "in_progress", "rawInput": {}}}})
            send({"jsonrpc": "2.0", "method": "session/update", "params": {"sessionId": params["sessionId"],
                "update": {"sessionUpdate": "tool_call_update", "toolCallId": "post-1",
                           "status": "completed", "rawOutput": "ready"}}})
            steering = json.loads(sys.stdin.readline())
            if steering.get("method") != "_session/steer":
                raise RuntimeError("Expected a post-tool steering request")
            send({"jsonrpc": "2.0", "id": steering["id"], "result": {"queued": True, "messageId": "post-1"}})
            text = steering["params"]["message"]
        if text == "two_messages":
            for message_id, chunk in (("first-message", "FIRST"), ("second-message", "SECOND")):
                send({"jsonrpc": "2.0", "method": "session/update", "params": {
                    "sessionId": params["sessionId"], "update": {"sessionUpdate": "agent_message_chunk",
                    "messageId": message_id, "content": {"type": "text", "text": chunk}}}})
            text = ""
        if text == "nested":
            child = {"kiro": {"agentSubtaskId": "child-1"}}
            spawn = {"kiro": {"kind": "agent-subtask", "agentSubtaskId": "child-1"}}
            updates = [
                {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "ROOT "}},
                {"sessionUpdate": "tool_call", "toolCallId": "spawn", "title": "Sub-agent", "status": "in_progress",
                 "rawInput": {"name": "child-type", "prompt": "test"}, "_meta": spawn},
                {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "CHILD_TEXT"}, "_meta": child},
                {"sessionUpdate": "agent_thought_chunk", "content": {"type": "text", "text": "CHILD_THOUGHT"}, "_meta": child},
                {"sessionUpdate": "tool_call", "toolCallId": "child-tool", "title": "Child tool", "_meta": child},
                {"sessionUpdate": "tool_call_update", "toolCallId": "child-tool", "status": "in_progress",
                 "rawInput": {"value": 1}},
                {"sessionUpdate": "tool_call_update", "toolCallId": "child-tool", "status": "completed",
                 "rawInput": {"value": 2}, "rawOutput": {"text": "child result"}},
                {"sessionUpdate": "tool_call_update", "toolCallId": "spawn", "status": "completed",
                 "rawOutput": "child finished", "_meta": spawn},
                {"sessionUpdate": "tool_call_update", "toolCallId": "spawn", "status": "completed",
                 "rawOutput": "child finished", "_meta": spawn},
            ]
            for update in updates:
                send({"jsonrpc": "2.0", "method": "session/update",
                      "params": {"sessionId": params["sessionId"], "update": update}})
            text = "done"
        if text == "nested_late":
            send({"jsonrpc": "2.0", "method": "session/update", "params": {"sessionId": params["sessionId"],
                 "update": {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "LATE_CHILD"},
                            "_meta": {"kiro": {"agentSubtaskId": "child-1"}}}}})
            text = "late root"
        if text == "hookevents":
            def hook_update(ident, operation, status):
                send({"jsonrpc": "2.0", "method": "session/update", "params": {"sessionId": params["sessionId"],
                     "update": {"sessionUpdate": "session_info_update", "_meta": {"kiro": {"kind": "hook_update",
                     "hook": {"hookId": ident, "operationId": operation, "status": status}}}}}})
            hook_update("hook-1", "operation-1", "running")
            hook_update("hook-1", "operation-1", "completed")
            send({"jsonrpc": "2.0", "method": "_kiro/hooks/didChange",
                  "params": {"sessionId": params["sessionId"], "hooks": [
                      {"id": "hook-2", "_meta": {"trigger": "Stop"}}]}})
            hook_update("hook-2", "operation-2", "completed")
            hook_update("unknown", "operation-3", "awaiting_approval")
            text = "done"
        if text == "disable_native_hook":
            send({"jsonrpc": "2.0", "method": "_kiro/hooks/didChange",
                  "params": {"sessionId": params["sessionId"], "hooks": []}})
            sys.stdin.readline()
            continue
        if text == "valid_native_hook":
            kiro_home = os.environ.get("KIRO_HOME") or os.path.join(os.environ.get("HOME", ""), ".kiro")
            profile = os.path.join(kiro_home, "agents", str(mode) + ".json")
            hooks = [{"id": "hook-" + hook["name"], "name": hook["name"], "action": hook["action"],
                      "_meta": {"enabled": True, "trigger": hook["trigger"]}}
                     for hook in json.load(open(profile, encoding="utf-8")).get("hooks", [])]
            send({"jsonrpc": "2.0", "method": "_kiro/hooks/didChange",
                  "params": {"sessionId": params["sessionId"], "hooks": hooks}})
            text = "valid"
        if text in ("guard_mode", "guard_sandbox", "guard_preset_plan", "guard_mapped"):
            trigger = json.loads(sys.stdin.readline())
            if trigger.get("method") != "_test/trigger":
                raise RuntimeError("Expected a guard trigger")
            if text in ("guard_mode", "guard_preset_plan", "guard_mapped"):
                send({"jsonrpc": "2.0", "method": "session/update", "params": {"sessionId": params["sessionId"],
                    "update": {"sessionUpdate": "current_mode_update",
                               "currentModeId": ("plan" if text == "guard_preset_plan" else
                                                 "worker" if text == "guard_mapped" else "unrestricted")}}})
            else:
                send({"jsonrpc": "2.0", "method": "_kiro/sandbox/status", "params": {
                    "sessionId": params["sessionId"], "status": "unavailable"}})
            sys.stdin.readline()
            continue
        if text == "agent_observed_wait":
            send({"jsonrpc": "2.0", "method": "session/update", "params": {
                "sessionId": params["sessionId"], "update": {"sessionUpdate": "tool_call",
                "toolCallId": "spawn", "rawInput": {"name": "child-type"},
                "_meta": {"kiro": {"kind": "agent-subtask", "agentSubtaskId": "child-1"}}}}})
            text = "cancel"
        if text == "stop_observed":
            for event in json.loads(os.environ.get("FAKE_STOP_UPDATES", "[]")):
                send({"jsonrpc": "2.0", "method": "session/update", "params": {
                    "sessionId": event.get("sessionId", params["sessionId"]), "update": event["update"]}})
            text = "cancel"
        if text == "cancel":
            cancel = json.loads(sys.stdin.readline())
            while cancel.get("method") == "_kiro/session/rename":
                if os.environ.get("FAKE_LOG_REQUESTS"):
                    with open(os.environ["FAKE_LOG_REQUESTS"], "a", encoding="utf-8") as output:
                        output.write(json.dumps(cancel) + "\n")
                send({"jsonrpc": "2.0", "id": cancel["id"],
                      "result": {"success": not bool(os.environ.get("FAKE_RENAME_FAIL"))}})
                cancel = json.loads(sys.stdin.readline())
            if cancel.get("method") != "session/cancel":
                raise RuntimeError("Expected a cancel notification")
            send({"jsonrpc": "2.0", "id": ident, "result": {"stopReason": "cancelled"}})
            continue
        if text == "steer":
            steering = json.loads(sys.stdin.readline())
            if steering.get("method") != "_session/steer":
                raise RuntimeError("Expected a steering request")
            send({"jsonrpc": "2.0", "id": steering["id"], "result": {"queued": True, "messageId": "steer-1"}})
            text = steering["params"]["message"]
        if text in ("user_input", "mcp_elicitation"):
            interaction_method = "_kiro/userInput" if text == "user_input" else "_kiro/mcp/elicitation"
            interaction = ({"question": "Color?", "options": [{"title": "purple"}]}
                           if text == "user_input" else
                           {"elicitation": {"mode": "form", "message": "Count?",
                                            "requestedSchema": {"type": "object"}}})
            interaction_params = {"toolCallId": "interaction-1", **interaction}
            if os.environ.get("FAKE_ELICITATION_RUNTIME"):
                interaction_params["sdkElicitation"] = {
                    "protocol": 2, "callId": "elicitation-" + str(ident),
                    "requestId": params.get("_meta", {}).get("kiro", {}).get("sdkRuntime", {}).get("requestId"),
                    "serverName": os.environ.get("FAKE_ELICITATION_SERVER", "notion"),
                    "inputRequired": os.environ.get("FAKE_ELICITATION_EMBEDDED") == "1"}
                if os.environ.get("FAKE_ELICITATION_STALE_REQUEST"):
                    interaction_params["sdkElicitation"]["requestId"] = "stale"
            if not os.environ.get("FAKE_INTERACTION_NO_SESSION"):
                interaction_params["sessionId"] = os.environ.get("FAKE_INTERACTION_SESSION", params["sessionId"])
            if text == "mcp_elicitation" and os.environ.get("FAKE_ELICITATION_CANCEL_FIRST"):
                metadata = interaction_params["sdkElicitation"]
                send({"jsonrpc": "2.0", "method": "_kiro/sdk/cancelHook", "params": {
                    "sessionId": params["sessionId"], "requestId": metadata["requestId"], "callId": metadata["callId"]}})
            if text == "mcp_elicitation" and os.environ.get("FAKE_ELICITATION_RUNTIME"):
                metadata = interaction_params["sdkElicitation"]
                elicitation_request_correlation = {"sessionId": params["sessionId"],
                    "requestId": metadata["requestId"], "callId": metadata["callId"]}
            if text == "mcp_elicitation" and os.environ.get("FAKE_ELICITATION_BAD_PROTOCOL"):
                interaction_params["sdkElicitation"]["protocol"] = 1
            send({"jsonrpc": "2.0", "id": 98, "method": interaction_method,
                  "params": interaction_params})
            response = read_interaction_reply()
            text = json.dumps(response.get("result") or response.get("error"))
        if text in ("mcp_roots", "mcp_sampling"):
            sampling = text == "mcp_sampling"
            server_request = {
                "protocol": 1, "sessionId": params["sessionId"],
                "requestId": params.get("_meta", {}).get("kiro", {}).get("sdkRuntime", {}).get("requestId"),
                "callId": "mcp-request-1", "toolCallId": "operation-1",
                "serverName": os.environ.get("FAKE_MCP_REQUEST_SERVER", "policy"),
                "method": "sampling/createMessage" if sampling else "roots/list",
                "params": {"messages": [{"role": "user", "content": {"type": "text", "text": "Review this"}}],
                           "maxTokens": 64, "includeContext": "none"} if sampling else {},
                "inputRequired": os.environ.get("FAKE_MCP_REQUEST_EMBEDDED") == "1"}
            if sampling and os.environ.get("FAKE_MCP_SAMPLING_TOOLS"):
                server_request["params"]["tools"] = [{
                    "name": "lookup", "inputSchema": {"type": "object", "properties": {"key": {"type": "string"}}}}]
                server_request["params"]["toolChoice"] = {"mode": "required"}
            if sampling and os.environ.get("FAKE_MCP_SAMPLING_MEDIA"):
                import base64
                server_request["params"]["messages"][0]["content"] = {
                    "type": "image", "mimeType": "image/png",
                    "data": base64.b64encode(b"synthetic-media" * 6000).decode()}
            if os.environ.get("FAKE_MCP_REQUEST_STALE"):
                server_request["requestId"] = "stale"
            if os.environ.get("FAKE_MCP_REQUEST_NO_SESSION"):
                server_request.pop("sessionId")
            send({"jsonrpc": "2.0", "id": 97, "method": "_kiro/sdk/mcp/request", "params": server_request})
            if os.environ.get("FAKE_MCP_REQUEST_CANCEL"):
                send({"jsonrpc": "2.0", "method": "_kiro/sdk/cancelHook", "params": {
                    "sessionId": params["sessionId"], "requestId": server_request["requestId"],
                    "callId": server_request["callId"]}})
            response = read_interaction_reply()
            text = json.dumps(response.get("result") or response.get("error"))
        if text == "permission_abandoned":
            send({"jsonrpc": "2.0", "id": 99, "method": "session/request_permission",
                  "params": {"sessionId": params["sessionId"],
                             "toolCall": {"toolCallId": "call-1", "title": "write", "rawInput": {"path": "a"}},
                             "options": [{"kind": "allow_once", "optionId": "yes"},
                                         {"kind": "reject_once", "optionId": "no"}]}})
            trigger = json.loads(sys.stdin.readline())
            if trigger.get("method") != "_kiro/testFinish":
                raise RuntimeError("Expected a test finish request")
            send({"jsonrpc": "2.0", "id": trigger["id"], "result": {"success": True}})
            text = "finished"
        if text in ("tool", "tool_duplicate"):
            for update in [
                {"sessionUpdate": "tool_call", "toolCallId": "call", "title": "read", "rawInput": {"path": "a"}},
                {"sessionUpdate": "tool_call_update", "toolCallId": "call", "status": "completed", "rawOutput": {"text": "data"}},
            ] + ([{"sessionUpdate": "tool_call_update", "toolCallId": "call", "status": "completed"}] if text == "tool_duplicate" else []):
                send({"jsonrpc": "2.0", "method": "session/update", "params": {"sessionId": params["sessionId"], "update": update}})
        if text == "tool_input_delayed":
            for update in [
                {"sessionUpdate": "tool_call", "toolCallId": "call", "title": "read"},
                {"sessionUpdate": "tool_call_update", "toolCallId": "call", "status": "in_progress", "rawInput": {"path": "late"}},
                {"sessionUpdate": "tool_call_update", "toolCallId": "call", "status": "completed", "rawOutput": {"text": "data"}},
            ]:
                send({"jsonrpc": "2.0", "method": "session/update", "params": {"sessionId": params["sessionId"], "update": update}})
        if text == "two_tools_delayed":
            for update in [
                {"sessionUpdate": "tool_call", "toolCallId": "first", "title": "read"},
                {"sessionUpdate": "tool_call", "toolCallId": "second", "title": "read"},
                {"sessionUpdate": "tool_call_update", "toolCallId": "first", "status": "in_progress", "rawInput": {"path": "a"}},
                {"sessionUpdate": "tool_call_update", "toolCallId": "second", "status": "in_progress", "rawInput": {"path": "b"}},
                {"sessionUpdate": "tool_call_update", "toolCallId": "first", "status": "completed"},
                {"sessionUpdate": "tool_call_update", "toolCallId": "second", "status": "completed"},
            ]:
                send({"jsonrpc": "2.0", "method": "session/update", "params": {"sessionId": params["sessionId"], "update": update}})
        if text in ("permission", "permission_always", "permission_consent", "permission_shell_edit", "permission_shell_sparse"):
            choices = [{"kind": "allow_once", "optionId": "yes"}, {"kind": "reject_once", "optionId": "no"}]
            if text == "permission_always":
                choices = [{"kind": "allow_always", "optionId": "persistent"}]
            metadata = {}
            if text in ("permission_consent", "permission_shell_edit", "permission_shell_sparse"):
                choices.extend([{"kind": "allow_always", "optionId": "always-yes"},
                                {"kind": "reject_always", "optionId": "always-no"}])
                capability = "shell" if text in ("permission_shell_edit", "permission_shell_sparse") else "fs_write"
                capability = os.environ.get("FAKE_CONSENT_CAPABILITY", capability)
                metadata = {"_meta": {"kiro": {"consent": {"capability": capability,
                           "resource": os.environ.get("FAKE_CONSENT_RESOURCE", "/tmp/test/a"), "workspaceRoot": "/tmp/test",
                           "persistableConsent": os.environ.get("FAKE_CONSENT_DISABLED") != "1"}}}}
                if "FAKE_CONSENT_TRIGGER" in os.environ:
                    metadata["_meta"]["kiro"]["consent"]["triggeringResource"] = os.environ["FAKE_CONSENT_TRIGGER"]
            tool = ({"toolCallId": "call-1", "title": "shell",
                     "rawInput": {} if text == "permission_shell_sparse" else {"command": "printf BEFORE", "timeout": 5}}
                    if text in ("permission_shell_edit", "permission_shell_sparse") else
                    {"toolCallId": "call-1", "title": "write", "rawInput": {"path": "a"}})
            if os.environ.get("FAKE_PERMISSION_MISSING_INPUT") == "1":
                tool.pop("rawInput", None)
            if os.environ.get("FAKE_PERMISSION_NO_TOOL_ID") == "1":
                tool.pop("toolCallId", None)
            if "FAKE_PERMISSION_RAW_INPUT_JSON" in os.environ:
                tool["rawInput"] = json.loads(os.environ["FAKE_PERMISSION_RAW_INPUT_JSON"])
            if "FAKE_PERMISSION_OPTIONS_JSON" in os.environ:
                choices = json.loads(os.environ["FAKE_PERMISSION_OPTIONS_JSON"])
            if os.environ.get("FAKE_PERMISSION_TOOL_META"):
                tool["_meta"] = json.loads(os.environ["FAKE_PERMISSION_TOOL_META"])
            if os.environ.get("FAKE_PERMISSION_AGENT_TYPE"):
                child_id = tool.get("_meta", {}).get("kiro", {}).get("agentSubtaskId")
                send({"jsonrpc": "2.0", "method": "session/update", "params": {
                    "sessionId": params["sessionId"], "update": {"sessionUpdate": "tool_call",
                    "toolCallId": "fake-spawn", "rawInput": {"name": os.environ["FAKE_PERMISSION_AGENT_TYPE"]},
                    "_meta": {"kiro": {"kind": "agent-subtask", "agentSubtaskId": child_id}}}}})
            send({"jsonrpc": "2.0", "id": 99, "method": "session/request_permission",
                  "params": {"sessionId": params["sessionId"], "toolCall": tool, "options": choices, **metadata}})
            response = read_interaction_reply()
            if text in ("permission_consent", "permission_shell_edit", "permission_shell_sparse"):
                text = json.dumps(response.get("result") or response.get("error"))
            else:
                text = response["result"]["outcome"].get("optionId", "cancelled")
        for chunk in [text[:1], text[1:]]:
            send({"jsonrpc": "2.0", "method": "session/update", "params": {"sessionId": params["sessionId"], "update": {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": chunk}}}})
        if not os.environ.get("FAKE_NO_USAGE"):
            usage = float('nan') if os.environ.get("FAKE_BAD_USAGE") else 0.1
            update = {"jsonrpc": "2.0", "method": "session/update", "params": {"sessionId": params["sessionId"],
                      "update": {"sessionUpdate": "session_info_update", "_meta": {"kiro": {
                          "kind": "turn_completion", "turnId": "turn-1",
                          "promptTurnSummaries": [{"unit": "credit", "usage": usage}]}}}}}
            send(update)
            if os.environ.get("FAKE_DUPLICATE_USAGE"):
                send(update)
        result = {"stopReason": os.environ.get("FAKE_STOP_REASON", "end_turn")}
    elif method in ("session/set_model", "session/set_mode", "session/set_config_option", "session/close"):
        if (method == "session/set_config_option" and params.get("configId") == "thinking" and
                params.get("value") == os.environ.get("FAKE_THINKING_SET_HANG_FOR")):
            continue
        if (method == "session/set_config_option" and params.get("configId") == "effortLevel" and
                params.get("value") == os.environ.get("FAKE_EFFORT_SET_FAIL_FOR")):
            send({"jsonrpc": "2.0", "id": ident, "error": {"code": -32603, "message": "effort selection failed"}})
            continue
        if (method == "session/set_config_option" and params.get("configId") == "model" and
                params.get("value") == os.environ.get("FAKE_MODEL_SET_HANG_FOR")):
            continue
        if (method == "session/set_config_option" and params.get("configId") == "model"
                and (os.environ.get("FAKE_MODEL_SET_FAIL") or
                     params.get("value") == os.environ.get("FAKE_MODEL_SET_FAIL_FOR"))):
            send({"jsonrpc": "2.0", "id": ident, "error": {"code": -32603, "message": "model selection failed"}})
            continue
        if method == "session/set_mode":
            requested_mode = params["modeId"]
            if requested_mode == os.environ.get("FAKE_MODE_SET_HANG_FOR"):
                continue
            if requested_mode == os.environ.get("FAKE_MODE_SET_FAIL_FOR"):
                send({"jsonrpc": "2.0", "id": ident, "error": {"code": -32603, "message": "mode selection failed"}})
                continue
            if requested_mode == os.environ.get("FAKE_MODE_SET_REPORT_OTHER_FOR"):
                # Kiro answers the request but reports that the session stayed in its previous mode.
                send({"jsonrpc": "2.0", "method": "session/update", "params": {"sessionId": params["sessionId"], "update": {"sessionUpdate": "current_mode_update", "currentModeId": mode}}})
            elif requested_mode != os.environ.get("FAKE_MODE_SET_IGNORE_FOR"):
                mode = requested_mode
                send({"jsonrpc": "2.0", "method": "session/update", "params": {"sessionId": params["sessionId"], "update": {"sessionUpdate": "current_mode_update", "currentModeId": requested_mode}}})
        if method == "session/set_config_option" and params.get("configId") == "thinking":
            thinking_value = params["value"]
            if os.environ.get("FAKE_THINKING_CHANGES_MODEL"):
                model_value = "changed-model"
        if method == "session/set_config_option" and params.get("configId") == "effortLevel":
            effort_value = ("low" if params["value"] == os.environ.get("FAKE_EFFORT_IGNORED_FOR")
                            else params["value"])
            if os.environ.get("FAKE_EFFORT_CHANGES_MODEL"):
                model_value = "changed-model"
        if method == "session/set_config_option" and params.get("configId") == "model":
            model_value = params["value"]
        result = {"configOptions": [{"id": "thinking", "currentValue": thinking_value},
                                    {"id": "effortLevel", "currentValue": effort_value},
                                    {"id": "model", "currentValue": model_value}]} if method == "session/set_config_option" else {}
        if (method == "session/set_config_option" and params.get("configId") == "model" and
                os.environ.get("FAKE_DUPLICATE_SET_MODEL_CONFIG")):
            result["configOptions"].append({"id": "model", "currentValue": "other"})
        if method == "session/set_config_option" and params.get("configId") == "effortLevel":
            if os.environ.get("FAKE_DUPLICATE_EFFORT_CONFIG"):
                result["configOptions"].append({"id": "effortLevel", "currentValue": "other"})
            if os.environ.get("FAKE_DUPLICATE_MODEL_CONFIG"):
                result["configOptions"].append({"id": "model", "currentValue": "other"})
    elif method == "session/list":
        if os.environ.get("FAKE_ACP_LIST_HANG"):
            continue
        if os.environ.get("FAKE_CLIENT_AUTH_LIST") and not require_client_auth(ident):
            continue
        result = {"sessions": [] if os.environ.get("FAKE_EMPTY_SESSIONS") else [{"sessionId": "test-session", "title": session_title[0],
                                       "updatedAt": "2026-11-13T05:00:00.000Z"}]}
    elif method == "session/fork":
        result = {"sessionId": "test-session" if os.environ.get("FAKE_FORK_SAME") else "forked", "received": params}
        if not os.environ.get("FAKE_RUNTIME_NO_FORK_RECEIPT"):
            result["_meta"] = {"kiro": {"sdkResumeDropsTurn": params.get("_meta", {}).get("kiro", {}).get("sdkResumeDropsTurn")}}
    elif method == "_kiro/sdk/forkPoint":
        result = {"protocol": 1, "sessionId": params["sessionId"], "userMessageId": params["userMessageId"],
                  "messageId": "prior-boundary"}
    elif method == "session/delete":
        result = {}
    elif method == "_kiro/test":
        result = {"ok": True}
    elif method == "_kiro/hooks/list":
        kiro_home = os.environ.get("KIRO_HOME") or os.path.join(os.environ.get("HOME", ""), ".kiro")
        profile = os.path.join(kiro_home, "agents", str(mode) + ".json")
        hooks = []
        if os.path.isfile(profile) and not os.environ.get("FAKE_NO_HOOK"):
            for index, hook in enumerate(json.load(open(profile, encoding="utf-8")).get("hooks", [])):
                hooks.append({"id": f"{mode}#hook-{index}" if os.environ.get("FAKE_NATIVE_ACP") else "hook-" + hook["name"],
                              "name": hook["name"], "action": hook["action"],
                              "_meta": {"enabled": True, "trigger": hook["trigger"]}})
        if os.environ.get("FAKE_HOOK_EVENTS"):
            hooks.append({"id": "hook-1", "name": "example", "action": {"type": "command"},
                          "_meta": {"enabled": True, "trigger": "PreToolUse"}})
        if params.get("trigger"):
            hooks = [hook for hook in hooks if hook.get("_meta", {}).get("trigger") == params["trigger"]]
        if not params.get("includeDisabled", False):
            hooks = [hook for hook in hooks if hook.get("_meta", {}).get("enabled") is not False]
        if os.environ.get("FAKE_HOOK_DISABLE_AT_START"):
            send({"jsonrpc": "2.0", "method": "_kiro/hooks/didChange",
                  "params": {"sessionId": params["sessionId"], "hooks": []}})
        if os.environ.get("FAKE_HOOK_VALID_AT_START"):
            send({"jsonrpc": "2.0", "method": "_kiro/hooks/didChange",
                  "params": {"sessionId": params["sessionId"], "hooks": hooks}})
        result = {"hooks": [None]} if os.environ.get("FAKE_INVALID_HOOK_LIST") else {"hooks": hooks}
    elif method == "_kiro/test/native-hook":
        profile = os.path.join(os.environ["KIRO_HOME"], "agents", str(mode) + ".json")
        hooks = json.load(open(profile, encoding="utf-8"))["hooks"]
        index = next(i for i, hook in enumerate(hooks) if hook["trigger"] == params["event"])
        payload = {"protocol": 1, "command": hooks[index]["action"]["command"],
                   "hookId": f"{mode}#hook-{index}", "sessionId": params["sessionId"],
                   "timeoutMs": 2000, "input": {"session_id": params["sessionId"],
                       "hook_event_name": params["event"], "tool_name": "shell", "tool_input": {"command": "probe"}}}
        payload.update(params.get("override", {}))
        send({"jsonrpc": "2.0", "id": "native-acp-hook", "method": "_kiro/sdk/hooks/callback", "params": payload})
        if params.get("cancel"):
            send({"jsonrpc": "2.0", "method": "$/cancel_request", "params": {"id": "native-acp-hook"}})
        result = read_interaction_reply()
    elif method == "_kiro/test/native-hook-flood":
        # Send many native hook callbacks at once, then collect every reply in any order.
        profile = os.path.join(os.environ["KIRO_HOME"], "agents", str(mode) + ".json")
        hooks = json.load(open(profile, encoding="utf-8"))["hooks"]
        index = next(i for i, hook in enumerate(hooks) if hook["trigger"] == "PreToolUse")
        for number in range(params["count"]):
            send({"jsonrpc": "2.0", "id": f"native-flood-{number}", "method": "_kiro/sdk/hooks/callback",
                  "params": {"protocol": 1, "command": hooks[index]["action"]["command"],
                             "hookId": f"{mode}#hook-{index}", "sessionId": params["sessionId"],
                             "timeoutMs": params.get("timeoutMs", 2000), "input": {
                                 "session_id": params["sessionId"], "hook_event_name": "PreToolUse",
                                 "tool_name": "shell", "tool_input": {"command": f"probe-{number}"}}}})
        replies, started = {}, time.monotonic()
        while len(replies) < params["count"]:
            reply = read_interaction_reply()
            if str(reply.get("id", "")).startswith("native-flood-"):
                replies[reply["id"]] = {"error": "error" in reply, "elapsed": time.monotonic() - started}
        result = {"replies": replies}
    elif method == "_kiro/permissions/list":
        kiro_home = os.environ.get("KIRO_HOME") or os.path.join(os.environ.get("HOME", ""), ".kiro")
        profile = os.path.join(kiro_home, "agents", str(mode) + ".json")
        rules = json.load(open(profile, encoding="utf-8")).get("permissions", {}).get("rules", []) if os.path.isfile(profile) and not os.environ.get("FAKE_NO_POLICY") else []
        result = {"rules": [{**rule, "scope": "agent"} for rule in rules]}
        if not os.environ.get("FAKE_IGNORE_PRESETS"):
            result["rules"].extend({"scope": "session", "effect": "allow", "capability": "fs_read",
                                    "source": "preset:" + preset} for preset in policy_presets)
    elif method == "_kiro/permissions/explain":
        result = {"capability": params["capability"], "resource": params["resource"], "effect": "deny"}
    elif method == "_kiro/account/getUsage":
        result = {"success": True, "data": {"usageBreakdowns": []}}
    elif method == "_kiro/powers/list":
        result = {"powers": [], "errors": []}
    elif method == "_kiro/spec/getTaskStatuses":
        result = {"tasks": [], "received": params}
    elif method == "_kiro/codeIntelligence":
        if os.environ.get("FAKE_CODE_INIT_HANG") and params.get("subcommand") == "init":
            continue
        result = ({"success": False} if os.environ.get("FAKE_CODE_DISABLED") else
                  {} if os.environ.get("FAKE_CODE_INVALID_RESPONSE") and params.get("subcommand") == "init" else
                  {"success": True, "subcommand": params["subcommand"]})
    elif method == "_kiro/session/context":
        command = params["subcommand"]
        if command == "add":
            context_files.append(params["path"])
            result = {"success": True}
        elif command == "remove":
            removed = params["path"] in context_files
            if removed:
                context_files.remove(params["path"])
            result = {"success": True, "removed": removed}
        elif command == "clear":
            cleared = len(context_files)
            context_files.clear()
            result = {"success": True, "cleared": cleared}
        else:
            result = {"success": True, "entries": [{"path": path} for path in context_files]}
    elif method in ("_kiro/session/rename", "_kiro/session/compact"):
        if os.environ.get("FAKE_CONTROL_HANG") and method == "_kiro/session/rename":
            continue
        if os.environ.get("FAKE_COMPACT_HANG") and method == "_kiro/session/compact":
            continue
        result = {"success": not ((method == "_kiro/session/rename" and os.environ.get("FAKE_RENAME_FAIL")) or
                                  (method == "_kiro/session/compact" and os.environ.get("FAKE_COMPACT_FAIL")))}
        if method == "_kiro/session/rename" and result["success"]:
            session_title[0] = params.get("title", session_title[0])
    elif method == "_kiro/checkpoint/revertMultiple":
        if os.environ.get("FAKE_CHECKPOINT_HANG"):
            continue
        result = ({"success": False, "message": "restore failed"} if os.environ.get("FAKE_CHECKPOINT_FAILURE") else
                  {"success": True, "affectedFiles": ["example.txt"], "totalFiles": 1})
    elif method == "_kiro/session/history":
        result = {"updates": [], "hasMore": False}
    elif method == "_kiro/session/export":
        result = {"success": True, "filePath": "/tmp/export.zip"}
    elif method == "_kiro/sandbox/applyConfig":
        status = "unavailable" if os.environ.get("FAKE_SANDBOX_UNAVAILABLE") else "active"
        if not os.environ.get("FAKE_SANDBOX_NO_STATUS"):
            send({"jsonrpc": "2.0", "method": "_kiro/sandbox/status", "params": {"status": status}})
        result = {}
    elif method == "_kiro/mcp/resetServer":
        result = {"success": True}
    elif method == "_kiro/mcp/getResource":
        result = {"contents": [{"uri": params["uri"], "text": "resource"}]}
    elif method == "_kiro/mcp/getPrompt":
        result = {"messages": [{"role": "user", "content": {"type": "text", "text": params["promptName"]}}]}
    else:
        if ident is None:
            continue
        send({"jsonrpc": "2.0", "id": ident, "error": {"code": -32601, "message": "unknown"}})
        continue
    if method == "session/prompt" and os.environ.get("FAKE_RUNTIME_POLICIES") and not os.environ.get("FAKE_RUNTIME_NO_RECEIPT"):
        policy = params.get("_meta", {}).get("kiro", {}).get("sdkRuntime")
        if policy is not None:
            requested = int(os.environ.get("FAKE_RUNTIME_MODEL_TURNS", "1"))
            count = min(policy["maxTurns"] or requested, requested)
            usage_report = None
            if policy["usageMode"] == "reported":
                usage_report = {"protocol": 1, "scope": "main-stream-attempts", "attempts": 1,
                    "truncated": False, "records": [{"index": 1, "modelId": "model-one", "status": "completed",
                    "httpAttempts": 1, "invalid": False, "tokens": {
                        "inputTokens": 100, "outputTokens": 20, "cacheReadInputTokens": 40, "cacheWriteInputTokens": 0}}]}
                if os.environ.get("FAKE_USAGE_MISSING_CACHE"):
                    usage_report["records"][0]["tokens"]["cacheWriteInputTokens"] = None
                if os.environ.get("FAKE_USAGE_BAD_RECEIPT"):
                    usage_report["records"][0]["tokens"]["inputTokens"] = True
            result["_meta"] = {"kiro": {"sdkRuntime": {"protocol": 7, "requestId": policy["requestId"],
                "usageMode": policy["usageMode"], "modelUsage": usage_report,
                "literal": policy["literal"], "modelTurns": count,
                "fallbackModel": policy["fallbackModel"], "fallbackApplied": False,
                "thinkingDisabled": policy["thinkingDisabled"],
                "agentEfforts": policy["agentEfforts"],
                "agentMaxTurns": policy["agentMaxTurns"],
                "inheritAgents": policy["inheritAgents"],
                "foregroundAgents": policy["foregroundAgents"],
                "backgroundAgents": policy["backgroundAgents"],
                "backgroundContext": policy["backgroundContext"],
                "backgroundResultIds": [json.loads(record)["task_id"] for record in policy["backgroundResults"]],
                "backgroundResultsApplied": True,
                "subagentStopLimit": policy["subagentStopLimit"],
                "maxTurnsReached": bool(policy["maxTurns"] and requested > policy["maxTurns"])}}}
            if os.environ.get("FAKE_BACKGROUND_RESULT_BAD_RECEIPT") and policy["backgroundResults"]:
                result["_meta"]["kiro"]["sdkRuntime"]["backgroundResultsApplied"] = False
            if os.environ.get("FAKE_USAGE_CANCEL_RECEIPT") and policy["usageMode"] == "reported":
                result["stopReason"] = "cancelled"
                if os.environ.get("FAKE_CANCEL_RESULT_NOT_APPLIED"):
                    result["_meta"]["kiro"]["sdkRuntime"]["backgroundResultsApplied"] = False
                if os.environ.get("FAKE_CANCEL_RECEIPT_STALE"):
                    result["_meta"]["kiro"]["sdkRuntime"]["requestId"] = "stale"
                if os.environ.get("FAKE_CANCEL_RECEIPT_MISSING"):
                    result.pop("_meta", None)
    if ((method == "initialize" and os.environ.get("FAKE_EARLY_INIT_RESPONSE")) or
            (method == "session/list" and os.environ.get("FAKE_EARLY_LIST_RESPONSE"))):
        require_client_auth(ident, failure_reply=False, early_result=result)
    else:
        send({"jsonrpc": "2.0", "id": ident, "result": result})
    if method == "session/prompt" and os.environ.get("FAKE_CLIENT_AUTH_IDLE"):
        time.sleep(0.2)
        require_client_auth(ident, failure_reply=False)
