"""stdio MCP server for one tool group: python -m incident_tools.server <logs|metrics|infrastructure|knowledge|repository|changes|reports|rca>.

Each Kiro agent profile starts only the servers for its own groups, so investigation agents never see the change tool.
"""
import sys

from mcp.server.mcpserver import MCPServer


def groups():
    from . import aws, knowledge, rca, repository
    return {"logs": aws.LOGS, "metrics": aws.METRICS, "infrastructure": aws.INFRASTRUCTURE,
            "knowledge": knowledge.KNOWLEDGE, "repository": repository.REPOSITORY_TOOLS,
            "changes": repository.CHANGE_TOOLS, "reports": aws.REPORTS, "rca": rca.RCA}


def main():
    name = sys.argv[1] if len(sys.argv) > 1 else ""
    tools = groups().get(name)
    if tools is None:
        raise SystemExit(f"unknown tool group: {name}")
    server = MCPServer(f"incident-{name}")
    for function in tools:
        server.tool()(function)
    server.run()


if __name__ == "__main__":
    main()
