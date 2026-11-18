"""rca_agent tool for the chatbot: runs the RCA loop in the agent app process, which owns the Kiro authentication."""
import json
import os
import urllib.request


def rca_agent(query: str) -> str:
    """Generate a structured RCA report for an incident using the Collector → Writer → Reviewer loop.
    The loop collects information, writes a report, and reviews quality with feedback loops.

    Args:
        query: Incident description including alarm name, time, and context
    """
    if not os.environ.get("INCIDENT_INTERNAL_URL"):
        return "rca_agent runs in the deployed AgentCore Runtime; in Kiro desktop, ask the collector agent instead."
    request = urllib.request.Request(os.environ["INCIDENT_INTERNAL_URL"] + "/rca", data=json.dumps({"query": query}).encode(),
                                     headers={"Content-Type": "application/json",
                                              "Authorization": "Bearer " + os.environ["INCIDENT_INTERNAL_TOKEN"]})
    try:
        with urllib.request.urlopen(request, timeout=1800) as reply:
            return reply.read().decode()
    except Exception as e:
        return f"RCA agent error: {e}"


RCA = [rca_agent]
