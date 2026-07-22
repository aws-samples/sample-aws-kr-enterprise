"""Gateway/Tool catalog. Spec Section 3.2, 6.5."""

from fastapi import APIRouter, Depends, Request

router = APIRouter(prefix="/api/gateways", tags=["gateways"])


def get_agentcore(request: Request):
    return request.app.state.agentcore_client


@router.get("")
async def list_gateways(ac=Depends(get_agentcore)):
    resp = ac.control_client.list_gateways(maxResults=50)
    gateways = resp.get("items", [])
    total_tools = 0
    result = []
    for gw in gateways:
        tool_count = 0
        try:
            targets = ac.control_client.list_gateway_targets(
                gatewayIdentifier=gw["gatewayId"], maxResults=100
            )
            tool_count = len(targets.get("items", []))
        except Exception:
            pass
        total_tools += tool_count
        result.append(
            {
                "gatewayId": gw["gatewayId"],
                "name": gw.get("name", ""),
                "description": gw.get("description", ""),
                "status": gw.get("status", ""),
                "toolCount": tool_count,
            }
        )
    return {"gateways": result, "totalGateways": len(result), "totalTools": total_tools}


@router.get("/{gateway_id}/tools")
async def get_gateway_tools(gateway_id: str, ac=Depends(get_agentcore)):
    resp = ac.control_client.list_gateway_targets(
        gatewayIdentifier=gateway_id, maxResults=100
    )
    targets = resp.get("items", [])
    tools = [
        {
            "name": t.get("name", ""),
            "description": t.get("description", ""),
            "status": t.get("status", ""),
        }
        for t in targets
    ]
    return {"gatewayId": gateway_id, "tools": tools, "count": len(tools)}
