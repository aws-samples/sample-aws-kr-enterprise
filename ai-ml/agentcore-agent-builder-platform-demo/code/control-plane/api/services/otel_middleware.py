"""Platform API OTEL Middleware — Control Plane 요청에 span attribute 추가.

The control-plane API only depends on opentelemetry-api, which ships a no-op
default TracerProvider: with no SDK provider installed the middleware would
never create a recording span, so every attribute write below is dead code and
platform-api never appears in the trace/service map. `setup_tracing()` installs
an SDK TracerProvider (when the SDK is available) and the middleware now starts
its OWN span per request instead of relying on an ambient span that never
exists.
"""

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

# A proxy tracer bound to the global provider at span-creation time, so it
# picks up the SDK provider installed later by setup_tracing().
_tracer = trace.get_tracer("platform-api")


def setup_tracing(service_name: str = "platform-api") -> bool:
    """Install an OTEL SDK TracerProvider so middleware spans are recorded and
    exported. Returns True if a provider was installed, False otherwise (in
    which case OtelMiddleware degrades to a cheap pass-through).

    An OTLP exporter is only wired up when OTEL_EXPORTER_OTLP_ENDPOINT is set
    (by an ADOT collector / sidecar). Without a collector, a BatchSpanProcessor
    pointed at the default localhost:4318 endpoint retries endlessly and floods
    the logs with connection-refused errors and delays shutdown — so we skip
    installing the provider entirely when no endpoint is configured.
    """
    import os

    endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", "").strip()
    if not endpoint:
        logger.info(
            "OTEL_EXPORTER_OTLP_ENDPOINT not set — no OTLP collector; skipping "
            "TracerProvider install (OtelMiddleware runs as a pass-through). "
            "Set the endpoint (ADOT sidecar) to enable control-plane tracing."
        )
        return False

    try:
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
            OTLPSpanExporter,
        )
    except ImportError:
        logger.warning(
            "OTEL SDK/exporter not installed — control-plane spans will NOT be "
            "recorded. Add opentelemetry-sdk and opentelemetry-exporter-otlp to "
            "requirements.txt to enable platform-api tracing."
        )
        return False

    provider = TracerProvider(
        resource=Resource.create({"service.name": service_name})
    )
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
    trace.set_tracer_provider(provider)
    logger.info(
        "OTEL TracerProvider installed for %s (endpoint=%s)", service_name, endpoint
    )
    return True


class OtelMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, service_name: str = "platform-api"):
        super().__init__(app)
        self.service_name = service_name

    async def dispatch(self, request: Request, call_next) -> Response:
        path = request.url.path
        method = request.method

        with _tracer.start_as_current_span(f"{method} {path}") as current_span:
            recording = current_span.is_recording()
            if recording:
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

            if recording:
                current_span.set_attribute("http.status_code", response.status_code)
                current_span.set_attribute("http.duration_ms", round(duration_ms, 1))

            return response
