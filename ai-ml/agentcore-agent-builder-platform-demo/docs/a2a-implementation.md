# A2A (Agent-to-Agent) Implementation Notes

## Disclaimer

> **Important:** The A2A (Agent-to-Agent) delegation pattern implemented in this sample is **not** the [Google A2A Protocol](https://github.com/google-a2a/A2A). While it shares the conceptual goal of inter-agent communication, the implementation details, message format, and protocol semantics differ significantly.

| Aspect | Google A2A Protocol | This Implementation |
|--------|-------------------|---------------------|
| Transport | HTTP + JSON-RPC 2.0 | AWS Bedrock AgentCore `InvokeAgent` API |
| Discovery | Agent Cards (`.well-known/agent.json`) | DynamoDB Agent Registry (internal lookup) |
| Message Format | A2A Task/Message/Part schema | Free-form prompt string + structured response |
| Streaming | SSE with A2A event types | AgentCore response stream + Side-Channel (DynamoDB) |
| Authentication | OAuth 2.0 / API Key | AWS IAM (SigV4) |
| State Management | Task lifecycle (submitted/working/completed) | Session-based (sessionId in DynamoDB) |

## What This Implementation Does

This sample implements **agent delegation** — one agent can invoke another agent as a tool call during its execution. The pattern:

1. **Parent Agent** receives a user request
2. During reasoning, the LLM decides to delegate a subtask to a **Child Agent**
3. The delegation is executed via `bedrock-agentcore:InvokeAgent` API
4. Results flow back to the Parent Agent as tool output
5. Side-Channel events track the delegation lifecycle (start/progress/end)

## Implementation Details

### Core File: `code/agent-runtime/internal_tools/agent_invoke_handler.py`

```python
# Simplified delegation flow
def tool_fn(agent_id, prompt, session_id, caller, delegation_depth):
    client = boto3.client("bedrock-agentcore")
    response = client.invoke_agent(
        agentId=agent_id,
        sessionId=session_id,
        prompt=prompt,
    )
    # Stream response, emit Side-Channel events
    return aggregated_result
```

### Key Design Decisions

1. **Delegation Depth Limit**: Maximum 3 levels to prevent infinite recursion
2. **Session Propagation**: Parent session ID passed to child for trace correlation
3. **Side-Channel Events**: `a2a_delegation` events emitted for UI real-time tracking
4. **Lazy Client Initialization**: boto3 client cached per agent instance

## Known Limitations

The delegation entries in the Agent Registry carry `timeout` and `scope` fields (see `scripts/seed-dynamodb.sh`), but these are **stored/displayed only — they are not enforced at runtime**. The `delegations → scoped_agent_invoke` conversion in `code/agent-runtime/agent_runner.py` reads only `targetAgent` and `purpose`; the other fields are dropped.

- **Per-delegation `timeout` is not enforced.** The seed value (e.g. 60/90s) is ignored; the only effective bound is the boto3 client `read_timeout` (900s) in `agent_invoke_handler.py`.
- **Per-delegation `scope` (tool allow-list) is not enforced.** The delegated child agent runs with its full toolset, not the subset listed in `scope`.
- Treat both fields as declarative metadata for the demo UI. Enforcing them would require passing `timeout`/`scope` through to the invoke handler (e.g. a per-call `read_timeout` and a tool filter on the child).

## How to Implement A2A on AgentCore (Reference)

### Option 1: AgentCore Native (This Sample)

Use `InvokeAgent` API as a Strands SDK tool:

```python
from strands import tool

@tool
def delegate_to_agent(agent_id: str, prompt: str) -> str:
    """Delegate a subtask to another AgentCore agent."""
    client = boto3.client("bedrock-agentcore")
    response = client.invoke_agent(
        agentId=agent_id,
        sessionId=current_session_id,
        prompt=prompt,
    )
    return parse_response(response)
```

**Pros:** Simple, uses IAM auth, native AgentCore observability (traces span both agents)
**Cons:** Tightly coupled to AgentCore, no standardized discovery

### Option 2: Google A2A Protocol on AgentCore

To implement the actual Google A2A Protocol on AgentCore:

1. **Agent Card**: Deploy a `/a2a/agent.json` endpoint on each agent (via FastAPI in the container)
2. **JSON-RPC Handler**: Add A2A task handler alongside the Strands agent
3. **Discovery**: Use AgentCore Agent Registry as the backing store for Agent Cards
4. **Transport**: Each agent exposes HTTP endpoints; use MCP Gateway or direct invocation

```python
# Conceptual: A2A-compliant agent on AgentCore
from fastapi import FastAPI

app = FastAPI()

@app.get("/.well-known/agent.json")
def agent_card():
    return {
        "name": "incident-rca-agent",
        "description": "Root cause analysis for cloud incidents",
        "capabilities": ["streaming", "tools"],
        "endpoint": "https://agent-endpoint/a2a",
    }

@app.post("/a2a")
def handle_a2a_request(request: A2ARequest):
    # JSON-RPC 2.0 dispatch
    ...
```

**Pros:** Standard protocol, interoperable with non-AWS agents
**Cons:** Additional infrastructure (HTTP endpoints per agent), more complex auth

### Option 3: Hybrid Approach (Recommended for Production)

- **Internal delegation**: Use AgentCore `InvokeAgent` (fast, native, traced)
- **External interop**: Expose A2A Protocol endpoints for cross-platform agents
- **Registry**: AgentCore Agent Registry stores both internal IDs and A2A Agent Cards

## References

- [Google A2A Protocol Specification](https://github.com/google-a2a/A2A)
- [AWS Bedrock AgentCore Documentation](https://docs.aws.amazon.com/bedrock/latest/userguide/agentcore.html)
- [Strands SDK — Custom Tools](https://github.com/strands-agents/sdk-python)
- [AgentCore Multi-Agent Collaboration](https://docs.aws.amazon.com/bedrock/latest/userguide/agentcore-multi-agent.html)
