"""Platform API OTEL Middleware — Control Plane 요청에 span attribute 추가."""

import time
import logging

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response
from opentelemetry import trace

logger = logging.getLogger(__name__)

CONTROL_PLANE_PATTERNS = [
    "/deploy",
    "/undeploy",
    "/builder/chat",
]


class OtelMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, service_name: str = "platform-api"):
        super().__init__(app)
        self.service_name = service_name

    async def dispatch(self, request: Request, call_next) -> Response:
        current_span = trace.get_current_span()
        path = request.url.path
        method = request.method

        if current_span.is_recording():
            current_span.set_attribute("http.method", method)
            current_span.set_attribute("http.route", path)
            current_span.set_attribute("platform.service", self.service_name)

            is_control_plane = any(p in path for p in CONTROL_PLANE_PATTERNS)
            current_span.set_attribute("platform.control_plane", is_control_plane)

            parts = path.split("/")
            if len(parts) >= 4 and parts[1] == "api" and parts[2] == "agents":
                agent_id = parts[3]
                current_span.set_attribute("agent.id", agent_id)

        start = time.time()
        response = await call_next(request)
        duration_ms = (time.time() - start) * 1000

        if current_span.is_recording():
            current_span.set_attribute("http.status_code", response.status_code)
            current_span.set_attribute("http.duration_ms", round(duration_ms, 1))

        return response
