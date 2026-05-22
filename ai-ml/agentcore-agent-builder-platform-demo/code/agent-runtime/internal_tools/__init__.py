"""internalTools type 시스템. Spec Section 5.5."""

from typing import Any
from strands import tool as strands_tool
from .agent_invoke_handler import _request_context as _request_context
from .dynamodb_handler import (
    create_dynamodb_query,
    create_dynamodb_get,
    create_dynamodb_put,
)


HANDLER_MAP = {
    "dynamodb_query": create_dynamodb_query,
    "dynamodb_get": create_dynamodb_get,
    "dynamodb_put": create_dynamodb_put,
}


def create_internal_tool(tool_config: dict, dynamodb_resource: Any):
    tool_type = tool_config.get("type")
    handler_factory = HANDLER_MAP.get(tool_type)
    if not handler_factory:
        if tool_type == "agent_invoke":
            from .agent_invoke_handler import create_agent_invoke

            raw_fn = create_agent_invoke(tool_config, dynamodb_resource)
            return _wrap_as_strands_tool(raw_fn, tool_config)
        if tool_type == "scoped_agent_invoke":
            from .agent_invoke_handler import create_scoped_agent_invoke

            raw_fn = create_scoped_agent_invoke(tool_config, dynamodb_resource)
            return _wrap_as_strands_tool(raw_fn, tool_config)
        if tool_type == "python_function":
            return _create_python_function_tool(tool_config)
        raise ValueError(f"Unknown internalTool type: {tool_type}")
    raw_fn = handler_factory(tool_config, dynamodb_resource)
    return _wrap_as_strands_tool(raw_fn, tool_config)


def _wrap_as_strands_tool(fn, tool_config: dict):
    """Plain function을 Strands @tool로 래핑하여 ToolRegistry에서 인식되게 한다."""
    name = tool_config.get("name", fn.__name__)
    description = tool_config.get("description", fn.__doc__ or "")
    return strands_tool(fn, name=name, description=description)


def _create_python_function_tool(tool_config: dict):
    import importlib

    module_name = tool_config.get("module", tool_config["name"])
    mod = importlib.import_module(module_name)
    fn = getattr(mod, tool_config["name"])
    return _wrap_as_strands_tool(fn, tool_config)
