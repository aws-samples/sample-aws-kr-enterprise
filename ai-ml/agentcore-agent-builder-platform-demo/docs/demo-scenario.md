# Demo Scenario

## Overview

This demo showcases an enterprise AI agent platform that automates cloud operations (AIOps). The platform demonstrates:

1. **Agent Builder** — Natural language agent creation
2. **Agent Playground** — Real-time agent testing with streaming
3. **Automated Incident RCA** — CloudWatch Alarm triggers autonomous investigation
4. **Observability** — End-to-end tracing of agent execution

## Demo Flow (15 minutes)

### Scene 1: Platform Overview (2 min)

1. Open `https://aiops-v2.${DOMAIN_NAME}`
2. Show the Dashboard — agent status grid, active sessions
3. Navigate to Architecture page — explain the component diagram

### Scene 2: Agent Builder (3 min)

1. Navigate to Builder page
2. Describe a new agent in natural language:
   > "Create an EKS cluster health check agent that monitors pod status,
   > node capacity, and reports any failing deployments"
3. Watch the AI generate the agent configuration
4. Review generated: system prompt, tool selection, model choice

### Scene 3: Agent Playground (4 min)

1. Open an existing agent (e.g., "Incident RCA Agent")
2. In the Playground, send:
   > "Check the health of the production EKS cluster and report any issues"
3. Observe:
   - SSE streaming events in real-time
   - Tool calls being made (CloudWatch, EKS MCP tools)
   - Structured response with findings

### Scene 4: Automated Incident Response (4 min)

1. Trigger a test alarm:
   ```bash
   aws cloudwatch set-alarm-state \
     --alarm-name "test-high-cpu" \
     --state-value ALARM \
     --state-reason "Demo trigger"
   ```
2. Show EventBridge delivering the event
3. Navigate to the Incidents view — new incident appears
4. Click into the incident — RCA report auto-generated
5. Show the agent's investigation trace (tools called, reasoning)

### Scene 5: Observability (2 min)

1. Navigate to Traces page
2. Show the waterfall view of the last agent invocation
3. Explain: each span = one tool call or LLM invocation
4. Show latency breakdown and token usage

## Key Talking Points

- **AgentCore managed runtime** — no server management for agents
- **MCP Gateway pattern** — 130+ tools available, extensible via Lambda
- **Security** — VPC endpoints, private subnets, Cognito auth, least-privilege IAM
- **Observability built-in** — every agent invocation traced end-to-end
- **Event-driven automation** — alarms → agents → reports, zero human intervention

## Demo Prerequisites

- Platform fully deployed and running
- At least one agent configured and ACTIVE
- Test CloudWatch alarm created for demo trigger
- Cognito user created for login

## Video

<!-- YouTube link placeholder -->
> Demo video: [Coming Soon]
