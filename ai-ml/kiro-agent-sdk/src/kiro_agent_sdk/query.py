"""One-shot query interface."""
from __future__ import annotations
import copy
from typing import Any, AsyncIterator, Awaitable, Callable, Optional
from .client import KiroSDKClient, _receive_queued_messages
from .errors import CLIConnectionError, ResultError
from .types import KiroAgentOptions, ResultMessage, ToolCall
from .transport import Transport

async def query(*, prompt: Any, options: Optional[KiroAgentOptions] = None,
                transport: Optional[Transport] = None) -> AsyncIterator[Any]:
    async with KiroSDKClient(options, transport=transport) as client:
        await client.query(prompt)
        async for message in _receive_queued_messages(client):
            yield message


async def query_with_structured_output_retry(
    *, prompt: Any, options: KiroAgentOptions,
    approve_retry: Callable[[ResultError, bool, int], Awaitable[bool]],
    max_retries: int = 1,
    retry_prompt: str = ("Return the previous answer as valid JSON matching the requested schema. "
                         "Do not repeat the original task or call tools."),
) -> AsyncIterator[Any]:
    """Resume after invalid structured output only when the caller approves.

    The callback receives the validation error, whether any tool calls were
    observed in that attempt's streamed messages, and the next attempt number.
    An absence of observed calls does not prove that the turn had no side effects.
    """
    if not isinstance(prompt, (str, list)):
        raise TypeError("Structured-output retry requires a replayable prompt")
    if not isinstance(options, KiroAgentOptions) or options.output_format is None:
        raise ValueError("Structured-output retry requires options with output_format")
    if not callable(approve_retry):
        raise ValueError("approve_retry must be an asynchronous callback")
    if isinstance(max_retries, bool) or not isinstance(max_retries, int) or not 0 <= max_retries <= 3:
        raise ValueError("max_retries must be an integer between 0 and 3")
    if not isinstance(retry_prompt, str) or not retry_prompt.strip():
        raise ValueError("retry_prompt must be a nonempty string")
    if max_retries and options.max_reported_credits is not None and options.reported_credit_ledger is None:
        raise ValueError("Structured-output retry with a credit limit requires a shared credit ledger")
    if max_retries and options.fallback_model not in (None, ""):
        raise ValueError("Structured-output retry cannot be combined with startup fallback")
    local_options = copy.copy(options)
    local_options.output_format = copy.deepcopy(options.output_format)
    next_prompt = copy.deepcopy(prompt)
    retry_count = 0
    while True:
        observed_tools = False
        user_attempt_started = False
        try:
            async with KiroSDKClient(local_options) as client:
                if client._initial_prompt_active:
                    initial_result = None
                    async for message in client.receive_response():
                        if isinstance(message, ToolCall):
                            observed_tools = True
                        if isinstance(message, ResultMessage):
                            initial_result = message
                        yield message
                    if initial_result is None or initial_result.is_error:
                        raise CLIConnectionError("Initial agent prompt failed; user prompt was not sent")
                await client.query(next_prompt)
                user_attempt_started = True
                async for message in _receive_queued_messages(client):
                    if isinstance(message, ToolCall):
                        observed_tools = True
                    yield message
            return
        except ResultError as exc:
            if (not user_attempt_started or exc.kind != "invalid_structured_output" or retry_count >= max_retries or
                    not exc.session_id or "\0" in exc.session_id):
                raise
            resume_id = exc.session_id
            if await approve_retry(exc, observed_tools, retry_count + 1) is not True:
                raise
            local_options.resume = resume_id
            local_options.session_id = None
            local_options.continue_conversation = False
            local_options.fork_session = False
            local_options.resume_session_at = None
            local_options.resume_from_store = False
            local_options.session_store_source_directory = None
            next_prompt = retry_prompt
            retry_count += 1


async def query_with_fallback(
    *, prompt: Any, fallback_model: str,
    approve_retry: Callable[[ResultMessage, bool], Awaitable[bool]],
    options: Optional[KiroAgentOptions] = None, transport: Optional[Transport] = None,
) -> AsyncIterator[Any]:
    """Retry one reported failure only after an explicit caller decision.

    The callback receives the failed result and whether a tool call was
    observed. Unobserved calls and external side effects remain possible.
    """
    if not isinstance(prompt, (str, list)):
        raise TypeError("Fallback requires a replayable string or content-block list")
    if not isinstance(fallback_model, str) or not fallback_model.strip() or "\0" in fallback_model:
        raise ValueError("fallback_model must be a nonempty Kiro model ID")
    if not callable(approve_retry):
        raise ValueError("approve_retry must be an asynchronous callback")
    original_prompt = copy.deepcopy(prompt)
    # set_model mutates the options object; keep the caller's model unchanged.
    local_options = copy.copy(options) if options is not None else KiroAgentOptions()
    async with KiroSDKClient(local_options, transport=transport) as client:
        await client.query(copy.deepcopy(original_prompt))
        last_result = None
        observed_tools = False
        async for message in _receive_queued_messages(client):
            if isinstance(message, ToolCall):
                observed_tools = True
            if isinstance(message, ResultMessage):
                last_result = copy.deepcopy(message)
            yield message
        reported_status = last_result.kiro_usage if last_result is not None else None
        if (last_result is None or not last_result.is_error or last_result.stop_reason == "cancelled" or
                (isinstance(reported_status, dict) and reported_status.get("status") == "aborted") or
                fallback_model == client._session_model):
            return
        if await approve_retry(last_result, observed_tools) is not True:
            return
        await client.set_model(fallback_model)
        await client.query(copy.deepcopy(original_prompt))
        async for message in _receive_queued_messages(client):
            yield message
