"""Errors surfaced by the SDK."""

class KiroSDKError(Exception):
    pass

class CLIConnectionError(KiroSDKError):
    pass

class CLINotFoundError(CLIConnectionError):
    def __init__(self, message="Kiro CLI not found", cli_path=None):
        super().__init__(f"{message}: {cli_path}" if cli_path else message)

class ProtocolError(KiroSDKError):
    pass

class PromptBlockedError(ProtocolError):
    """A prompt was blocked before it was sent to Kiro."""
    def __init__(self, reasons, session_id, warnings=()):
        self.reasons = tuple(reasons)
        self.reason = self.reasons[0] if self.reasons else None
        self.session_id = session_id
        self.warnings = tuple(warnings)
        super().__init__("Prompt blocked by a promptSubmit hook")

class McpInputDeniedError(ProtocolError):
    """An application explicitly declined an SDK-mediated MCP tool call."""
    def __init__(self, reason="MCP tool call denied"):
        if not isinstance(reason, str) or len(reason) > 65536 or "\0" in reason:
            raise ValueError("Invalid MCP input denial reason")
        self.reason = reason
        super().__init__("MCP tool call denied")

class JSONRPCError(ProtocolError):
    """A structured JSON-RPC error returned by the Kiro server."""
    def __init__(self, error):
        self.error = error
        self.code = error.get("code")
        self.data = error.get("data")
        super().__init__(str(error))

class SessionArchiveError(KiroSDKError):
    pass

class StoreForkError(KiroSDKError):
    """A fork was created but could not be fully written to the session store."""
    def __init__(self, fork_session_id):
        self.fork_session_id = fork_session_id
        super().__init__(f"Could not store forked session: {fork_session_id}")

class ReportedCreditLimitError(KiroSDKError):
    """The reported-credit limit was reached or usage is unknown."""
    def __init__(self, message, reported_credits, limit):
        self.reported_credits = reported_credits
        self.limit = limit
        super().__init__(message)

class CheckpointRecoveryError(KiroSDKError):
    """A conversation fork was created, but checkpoint restoration did not succeed."""
    def __init__(self, fork_session_id, checkpoint_result=None):
        self.fork_session_id = fork_session_id
        self.checkpoint_result = checkpoint_result
        super().__init__(f"Checkpoint restoration failed; conversation fork: {fork_session_id}")

class ProcessError(KiroSDKError):
    def __init__(self, message, exit_code=None, stderr=None):
        self.exit_code = exit_code
        self.stderr = stderr
        if exit_code is not None:
            message = f"{message} (exit code: {exit_code})"
        if stderr:
            message = f"{message}\nError output: {stderr}"
        super().__init__(message)

class ResultError(ProcessError):
    """A result could not be accepted; available details are retained in ``data``."""
    def __init__(self, message, data=None, exit_code=None):
        data = data if isinstance(data, dict) else {}
        self.data = data
        self.kind = data.get("kind") if isinstance(data.get("kind"), str) else None
        raw_errors = data.get("errors")
        if isinstance(raw_errors, str):
            raw_errors = [raw_errors]
        self.errors = ([item.strip() for item in raw_errors if isinstance(item, str) and item.strip()]
                       if isinstance(raw_errors, list) else [])
        self.result = data.get("result") if isinstance(data.get("result"), str) else None
        session = data.get("session_id")
        self.session_id = session if isinstance(session, str) else None
        super().__init__(message, exit_code=exit_code)

class CLIJSONDecodeError(ProtocolError):
    def __init__(self, line, original_error):
        self.line = line
        self.original_error = original_error
        super().__init__("Invalid JSON from Kiro CLI")
