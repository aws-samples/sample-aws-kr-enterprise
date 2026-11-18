import asyncio
import os
import time
import unittest

from kiro_agent_sdk import (AgentMessage, CLIConnectionError, CLIJSONDecodeError, CLINotFoundError, HookMatcher, KiroAgentOptions, ProtocolError, ReportedCreditLimitError,
                            KiroSDKClient, PermissionResultAllow, ResultMessage, StreamEvent, KiroEvent, TextBlock, ThoughtBlock, ToolCall, query)

FAKE = os.path.join(os.path.dirname(__file__), "fake_kiro.py")

class ClientTests(unittest.IsolatedAsyncioTestCase):

    async def test_subagent_text_is_not_mixed_into_parent_by_default(self):
        options = KiroAgentOptions(cli_path=FAKE, agent_engine='v3', include_partial_messages=True,
                                   include_thoughts=True)
        messages = [message async for message in query(prompt='nested', options=options)]
        result = next(message for message in messages if isinstance(message, ResultMessage))
        self.assertEqual(result.result, 'ROOT done')
        child_text = [block.text for message in messages if isinstance(message, AgentMessage)
                      and message.agent_id == 'child-1'
                      for block in message.content if isinstance(block, TextBlock)]
        self.assertEqual(child_text, [])
        self.assertFalse(any(isinstance(message, StreamEvent) and message.agent_id
                             for message in messages))
        child_tools = [message for message in messages if isinstance(message, ToolCall)
                       and message.agent_id == 'child-1']
        self.assertEqual([tool.status for tool in child_tools], ['in_progress', 'completed'])
        self.assertTrue(all(tool.parent_tool_call_id == 'spawn' for tool in child_tools))
        self.assertEqual(child_tools[0].raw_input, {'value': 1})
        self.assertEqual(child_tools[1].raw_output, {'text': 'child result'})
        self.assertEqual(child_tools[1].raw_input, {'value': 2})

    async def test_subagent_text_can_be_forwarded_with_parent_identity(self):
        options = KiroAgentOptions(cli_path=FAKE, agent_engine='v3', include_partial_messages=True,
                                   include_thoughts=True, forward_subagent_text=True)
        messages = [message async for message in query(prompt='nested', options=options)]
        self.assertEqual(next(m for m in messages if isinstance(m, ResultMessage)).result, 'ROOT done')
        children = [message for message in messages if isinstance(message, AgentMessage)
                    and message.agent_id == 'child-1']
        self.assertTrue(any(any(isinstance(block, TextBlock) and block.text == 'CHILD_TEXT'
                                for block in message.content) for message in children))
        self.assertTrue(any(any(isinstance(block, ThoughtBlock) and block.text == 'CHILD_THOUGHT'
                                for block in message.content) for message in children))
        self.assertTrue(all(message.parent_tool_call_id == 'spawn' for message in children))
        streams = [message for message in messages if isinstance(message, StreamEvent) and message.agent_id]
        self.assertEqual(len(streams), 2)
        self.assertTrue(all(message.parent_tool_call_id == 'spawn' for message in streams))
        with self.assertRaises(ValueError):
            KiroSDKClient(KiroAgentOptions(forward_subagent_text='yes'))

    async def test_late_subagent_text_keeps_parent_across_turns(self):
        options = KiroAgentOptions(cli_path=FAKE, agent_engine='v3', forward_subagent_text=True)
        async with KiroSDKClient(options) as client:
            await client.query('nested')
            _ = [message async for message in client.receive_response()]
            await client.query('nested_late')
            messages = [message async for message in client.receive_response()]
            child = next(message for message in messages if isinstance(message, AgentMessage)
                         and message.agent_id == 'child-1')
            self.assertEqual(child.parent_tool_call_id, 'spawn')
            self.assertEqual(child.content[0].text, 'LATE_CHILD')
            self.assertEqual(messages[-1].result, 'late root')

    async def test_replayed_subagent_keeps_parent_after_resume(self):
        options = KiroAgentOptions(cli_path=FAKE, agent_engine='v3', resume='old', forward_subagent_text=True,
                                   env={'FAKE_REPLAY_SUBAGENT': '1'})
        async with KiroSDKClient(options) as client:
            await client.query('nested_late')
            messages = [message async for message in client.receive_response()]
            child = next(message for message in messages if isinstance(message, AgentMessage)
                         and message.agent_id == 'child-1')
            self.assertEqual(child.parent_tool_call_id, 'spawn')
            self.assertEqual(messages[-1].result, 'late root')

    async def test_reported_credit_limit_blocks_later_turns(self):
        opts = KiroAgentOptions(cli_path=FAKE, agent_engine='v3', max_reported_credits=0.15,
                                env={'FAKE_DUPLICATE_USAGE': '1'})
        async with KiroSDKClient(opts) as client:
            for prompt in ('hello', 'again'):
                await client.query(prompt)
                _ = [message async for message in client.receive_response()]
            self.assertEqual(client.reported_credits, 0.2)
            with self.assertRaises(ReportedCreditLimitError) as raised:
                await client.query('blocked')
            self.assertEqual((raised.exception.reported_credits, raised.exception.limit), (0.2, 0.15))
            await client.disconnect()
            await client.connect()
            with self.assertRaises(ReportedCreditLimitError):
                await client.query('still blocked')

    async def test_missing_or_invalid_credit_usage_blocks_later_turns(self):
        for flag in ('FAKE_NO_USAGE', 'FAKE_BAD_USAGE'):
            with self.subTest(flag=flag):
                options = KiroAgentOptions(cli_path=FAKE, agent_engine='v3', max_reported_credits=1,
                                           env={flag: '1'})
                async with KiroSDKClient(options) as client:
                    await client.query('hello')
                    _ = [message async for message in client.receive_response()]
                    with self.assertRaisesRegex(ReportedCreditLimitError, 'unavailable'):
                        await client.query('blocked')

    async def test_abandoned_turn_does_not_reset_reported_credit_guard(self):
        options = KiroAgentOptions(cli_path=FAKE, agent_engine='v3', max_reported_credits=1)
        async with KiroSDKClient(options) as client:
            await client.query('cancel')
            await client.disconnect()
            await client.connect()
            with self.assertRaisesRegex(ReportedCreditLimitError, 'unavailable'):
                await client.query('blocked')

    async def test_invalid_reported_credit_limit(self):
        for value in (0, -1, True, float('nan'), float('inf'), '1', 10 ** 1000):
            with self.subTest(value=str(value)[:10]), self.assertRaises(ValueError):
                KiroSDKClient(KiroAgentOptions(agent_engine='v3', max_reported_credits=value))
        with self.assertRaises(ValueError):
            KiroSDKClient(KiroAgentOptions(agent_engine='v2', max_reported_credits=1))

    async def test_query_and_multiple_turns(self):
        async with KiroSDKClient(KiroAgentOptions(cli_path=FAKE)) as client:
            self.assertEqual(client.session_id, "test-session")
            for prompt in ("hello", "again"):
                await client.query(prompt)
                messages = [m async for m in client.receive_response()]
                self.assertEqual("".join(m.content[0].text for m in messages if isinstance(m, AgentMessage)), prompt)
                self.assertEqual(sum(isinstance(m, AgentMessage) for m in messages), 1)
                self.assertEqual(messages[-1].result, prompt)
                self.assertEqual(messages[-1].stop_reason, "end_turn")
                self.assertEqual(messages[-1].user_message_id, "user-1")
                self.assertEqual(messages[-1].kiro_usage["promptTurnSummaries"][0]["usage"], 0.1)

    async def test_result_duration_excludes_delayed_consumption(self):
        async with KiroSDKClient(KiroAgentOptions(cli_path=FAKE)) as client:
            start = time.monotonic()
            await client.query("hello")
            await asyncio.wait_for(asyncio.shield(client._turn), 5)
            await asyncio.sleep(0.25)
            messages = [m async for m in client.receive_response()]
            result = messages[-1]
            self.assertGreaterEqual((time.monotonic() - start) * 1000 - result.duration_ms, 180)
            self.assertIsNone(result.model_turns)

    async def test_turn_timeout_stops_work_without_a_receiver(self):
        for invalid in (0, -1, True, float("nan"), float("inf")):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                KiroSDKClient(KiroAgentOptions(turn_timeout=invalid))
        client = KiroSDKClient(KiroAgentOptions(cli_path=FAKE, turn_timeout=0.1))
        await client.connect()
        process = client._process
        await client.query("cancel")
        await asyncio.wait_for(process.wait(), 3)
        with self.assertRaisesRegex(CLIConnectionError, "Timed out waiting for the Kiro turn"):
            _ = [message async for message in client.receive_response()]
        self.assertFalse(client._connected)

    async def test_turn_timeout_allows_completed_turns(self):
        async with KiroSDKClient(KiroAgentOptions(cli_path=FAKE, turn_timeout=3)) as client:
            await client.query("hello")
            messages = [message async for message in client.receive_response()]
            self.assertEqual(messages[-1].result, "hello")

    async def test_kiro_account_powers_and_spec_tasks(self):
        async with KiroSDKClient(KiroAgentOptions(cli_path=FAKE, agent_engine='v3')) as client:
            self.assertTrue((await client.get_kiro_account_usage())['success'])
            self.assertEqual((await client.list_kiro_powers())['powers'], [])
            response = await client.get_kiro_spec_task_statuses(
                tasks_file_path='/workspace/tasks.md', feature_name='example', workspace_paths=['/workspace'])
            self.assertEqual(response['received'], {
                'tasksFilePath': '/workspace/tasks.md', 'featureName': 'example', 'workspacePaths': ['/workspace']})
            with self.assertRaises(ValueError):
                await client.get_kiro_spec_task_statuses(
                    tasks_file_path='/workspace/tasks.md', feature_name='example', workspace_paths=[])
            with self.assertRaises(ValueError):
                await client.get_kiro_spec_task_statuses(
                    tasks_file_path='tasks.md', feature_name='example', workspace_paths=['/workspace'])
        async with KiroSDKClient(KiroAgentOptions(cli_path=FAKE, agent_engine='v2')) as client:
            with self.assertRaises(ProtocolError):
                await client.list_kiro_powers()

    async def test_kiro_context_management(self):
        async with KiroSDKClient(KiroAgentOptions(cli_path=FAKE, agent_engine='v3')) as client:
            self.assertEqual((await client.list_kiro_context())['entries'], [])
            await client.add_kiro_context('first.txt')
            await client.add_kiro_context('second.txt', force=True)
            self.assertEqual([entry['path'] for entry in (await client.list_kiro_context())['entries']],
                             ['first.txt', 'second.txt'])
            self.assertTrue((await client.remove_kiro_context('first.txt'))['removed'])
            self.assertEqual((await client.clear_kiro_context())['cleared'], 1)
            with self.assertRaises(ValueError):
                await client.add_kiro_context('')
            await client.query('cancel')
            with self.assertRaises(CLIConnectionError):
                await client.add_kiro_context('during-turn.txt')
            await client.interrupt()
            _ = [message async for message in client.receive_response()]

    async def test_permission_defaults_to_deny(self):
        messages = [m async for m in query(prompt="permission", options=KiroAgentOptions(cli_path=FAKE))]
        self.assertEqual(messages[-1].result, "no")

    async def test_permission_callback(self):
        async def approve(name, input_data, context):
            self.assertEqual((name, input_data, context.session_id), ("write", {"path": "a"}, "test-session"))
            self.assertEqual(context.tool_call_id, "call-1")
            return PermissionResultAllow()
        messages = [m async for m in query(prompt="permission", options=KiroAgentOptions(cli_path=FAKE, can_use_tool=approve))]
        self.assertEqual(messages[-1].result, "yes")

    async def test_persistent_permission_requires_explicit_choice(self):
        options = KiroAgentOptions(cli_path=FAKE, permission_mode='trustAll')
        messages = [m async for m in query(prompt="permission_always", options=options)]
        self.assertEqual(messages[-1].result, "cancelled")
        async def choose(name, input_data, context):
            return PermissionResultAllow(option_id='persistent')
        options = KiroAgentOptions(cli_path=FAKE, can_use_tool=choose)
        messages = [m async for m in query(prompt="permission_always", options=options)]
        self.assertEqual(messages[-1].result, "persistent")

    async def test_resume_and_model(self):
        async with KiroSDKClient(KiroAgentOptions(cli_path=FAKE, resume="old", model="foo")) as client:
            self.assertEqual(client.session_id, "old")
            await client.set_mode("test")
            await client.set_config_option("model", "bar")
            await client.query("ok")
            self.assertEqual([m async for m in client.receive_response()][-1].result, "ok")

    async def test_continue_most_recent(self):
        async with KiroSDKClient(KiroAgentOptions(cli_path=FAKE, continue_conversation=True)) as client:
            self.assertEqual(client.session_id, 'one')
        async with KiroSDKClient(KiroAgentOptions(cli_path=FAKE, agent_engine='v3', continue_conversation=True)) as client:
            self.assertEqual(client.session_id, 'test-session')
        with self.assertRaises(ValueError):
            KiroSDKClient(KiroAgentOptions(resume='one', continue_conversation=True))

    async def test_explicit_session_id(self):
        session_id = 'sess_12345678-1234-1234-1234-123456789abc'
        options = KiroAgentOptions(cli_path=FAKE, agent_engine='v3', session_id=session_id)
        async with KiroSDKClient(options) as client:
            self.assertEqual(client.session_id, session_id)
            self.assertEqual(client.session_info['received']['_meta']['kiro']['overrideSessionId'], session_id)
        with self.assertRaises(ValueError):
            KiroSDKClient(KiroAgentOptions(agent_engine='v3', session_id='../unsafe'))
        with self.assertRaises(ValueError):
            KiroSDKClient(KiroAgentOptions(agent_engine='v3', session_id=session_id, resume='old'))
        options = KiroAgentOptions(cli_path=FAKE, agent_engine='v3', session_id=session_id,
                                   env={'FAKE_IGNORE_SESSION_ID': '1'})
        with self.assertRaisesRegex(ProtocolError, 'requested session ID'):
            await KiroSDKClient(options).connect()

    async def test_fork_session_option(self):
        opts = KiroAgentOptions(cli_path=FAKE, agent_engine='v3', resume='old',
                                fork_session=True, resume_session_at='message-1')
        async with KiroSDKClient(opts) as client:
            self.assertEqual(client.session_id, 'forked')
            self.assertEqual(client.session_info['received']['_meta']['kiro']['messageId'], 'message-1')
            self.assertTrue(any(event.get('sessionUpdate') == 'agent_message_chunk' for event in client.history))
        with self.assertRaises(ValueError):
            KiroSDKClient(KiroAgentOptions(resume='old', resume_session_at='message-1'))
        with self.assertRaises(ValueError):
            KiroSDKClient(KiroAgentOptions(resume='old', fork_session=True))
        opts = KiroAgentOptions(cli_path=FAKE, agent_engine='v3', resume='old',
                                fork_session=True, env={'FAKE_LOAD_FAIL': '1'})
        with self.assertRaisesRegex(ProtocolError, 'Forked session forked could not be loaded'):
            await KiroSDKClient(opts).connect()

    async def test_missing_cli(self):
        with self.assertRaises(CLIConnectionError) as raised:
            await KiroSDKClient(KiroAgentOptions(cli_path="/missing/kiro")).connect()
        self.assertIsInstance(raised.exception, CLINotFoundError)

    async def test_missing_working_directory(self):
        options = KiroAgentOptions(cli_path=FAKE, cwd='/missing/kiro-sdk-workspace')
        with self.assertRaisesRegex(CLIConnectionError, 'Working directory'):
            await KiroSDKClient(options).connect()

    async def test_invalid_cli_json(self):
        client = KiroSDKClient(KiroAgentOptions(cli_path=FAKE, env={'FAKE_BAD_JSON': '1'}))
        with self.assertRaises(CLIJSONDecodeError) as raised:
            await client.connect()
        self.assertIn('not-json', raised.exception.line)

    async def test_reconnect(self):
        client = KiroSDKClient(KiroAgentOptions(cli_path=FAKE))
        for _ in range(2):
            await client.connect()
            await client.query("ok")
            self.assertEqual([m async for m in client.receive_response()][-1].result, "ok")
            await client.disconnect()

    async def test_invalid_permission_mode(self):
        with self.assertRaises(ValueError):
            KiroSDKClient(KiroAgentOptions(permission_mode="allowEdits"))

    async def test_streaming_input(self):
        async def prompts():
            for text in ("one", "two"):
                yield text
        messages = [m async for m in query(prompt=prompts(), options=KiroAgentOptions(cli_path=FAKE))]
        self.assertEqual([m.result for m in messages if isinstance(m, ResultMessage)], ["one", "two"])

    async def test_client_streaming_input(self):
        async def prompts():
            for text in ("one", "two"):
                yield text
        async with KiroSDKClient(KiroAgentOptions(cli_path=FAKE)) as client:
            await client.query(prompts())
            messages = [m async for m in client.receive_messages()]
            self.assertEqual([m.result for m in messages if isinstance(m, ResultMessage)], ["one", "two"])

    async def test_receive_response_advances_one_stream_turn(self):
        async def prompts():
            for text in ("one", "two"):
                yield text
        async with KiroSDKClient(KiroAgentOptions(cli_path=FAKE)) as client:
            await client.query(prompts())
            first = [m async for m in client.receive_response()]
            second = [m async for m in client.receive_response()]
            self.assertEqual((first[-1].result, second[-1].result), ("one", "two"))
            self.assertEqual([m async for m in client.receive_response()], [])

    async def test_closing_stream_stops_pending_input(self):
        started, closed = asyncio.Event(), asyncio.Event()
        async def prompts():
            try:
                started.set()
                await asyncio.Event().wait()
                yield "never"
            finally:
                closed.set()
        client = KiroSDKClient(KiroAgentOptions(cli_path=FAKE))
        await client.connect()
        await client.query(prompts())
        receiver = asyncio.create_task(client.receive_messages().__anext__())
        await asyncio.wait_for(started.wait(), 2)
        await asyncio.wait_for(client.disconnect(), 5)
        with self.assertRaises(asyncio.CancelledError):
            await receiver
        self.assertTrue(closed.is_set())

    async def test_closing_streamed_responses_disconnects(self):
        closed = asyncio.Event()
        async def prompts():
            try:
                yield "one"
                await asyncio.Event().wait()
            finally:
                closed.set()
        client = KiroSDKClient(KiroAgentOptions(cli_path=FAKE))
        await client.connect()
        await client.query(prompts())
        responses = client.receive_messages()
        async for message in responses:
            if isinstance(message, ResultMessage):
                break
        await responses.aclose()
        self.assertFalse(client._connected)
        self.assertTrue(closed.is_set())

    async def test_streaming_input_rejects_non_prompt_items(self):
        async def prompts():
            yield {"prompt": "one"}
        with self.assertRaises(ValueError):
            _ = [m async for m in query(prompt=prompts(), options=KiroAgentOptions(cli_path=FAKE))]

    async def test_sessions_and_extensions(self):
        async with KiroSDKClient(KiroAgentOptions(cli_path=FAKE, resume="old")) as client:
            self.assertEqual(client.history[0]["content"]["text"], "replayed")
            self.assertEqual((await client.list_sessions())["sessions"][0]["sessionId"], "test-session")
            self.assertEqual((await client.fork_session())["sessionId"], "forked")
            await client.delete_session("forked")
            with self.assertRaises(ValueError):
                await client.delete_session("old")
            self.assertEqual(await client.extension_request("_kiro/test", {}), {"ok": True})

    async def test_tool_result(self):
        messages = [m async for m in query(prompt="tool", options=KiroAgentOptions(cli_path=FAKE))]
        result = [m for m in messages if isinstance(m, ToolCall)][-1]
        self.assertEqual((result.status, result.raw_output), ("completed", {"text": "data"}))
        self.assertTrue(any(isinstance(m, KiroEvent) and m.kind == "tool_call" for m in messages))

    async def test_partial_tool_updates_keep_input_for_post_hooks(self):
        observed = []
        async def on_tool(data, tool_call_id, context):
            observed.append((data, tool_call_id))
            return {}
        options = KiroAgentOptions(cli_path=FAKE, hooks={"postToolUse": [HookMatcher(matcher="read", hooks=[on_tool])]})
        messages = [m async for m in query(prompt="tool", options=options)]
        self.assertEqual(len(observed), 1)
        self.assertEqual(observed[0][0]["tool_name"], "read")
        self.assertEqual(observed[0][0]["tool_input"], {"path": "a"})
        self.assertEqual(observed[0][0]["tool_response"], {"text": "data"})
        self.assertEqual(observed[0][1], "call")
        self.assertEqual([m for m in messages if isinstance(m, ToolCall)][-1].raw_input, {"path": "a"})

    async def test_duplicate_terminal_tool_update_does_not_repeat_hook(self):
        calls = []
        async def on_tool(data, tool_call_id, context):
            calls.append(tool_call_id)
            return {}
        options = KiroAgentOptions(cli_path=FAKE, hooks={"postToolUse": [HookMatcher(hooks=[on_tool])]})
        messages = [m async for m in query(prompt="tool_duplicate", options=options)]
        self.assertEqual(calls, ["call"])
        self.assertEqual(sum(isinstance(m, ToolCall) and m.status == "completed" for m in messages), 1)
        self.assertEqual(sum(isinstance(m, KiroEvent) and m.kind == "tool_call_update" for m in messages), 2)

    async def test_delayed_tool_input_is_reported_once_known(self):
        messages = [m async for m in query(prompt="tool_input_delayed", options=KiroAgentOptions(cli_path=FAKE))]
        started = [m for m in messages if isinstance(m, ToolCall) and m.status not in ("completed", "failed")]
        self.assertEqual(len(started), 1)
        self.assertEqual(started[0].raw_input, {"path": "late"})

    async def test_interleaved_tool_calls_wait_for_each_input(self):
        messages = [m async for m in query(prompt="two_tools_delayed", options=KiroAgentOptions(cli_path=FAKE))]
        started = [m for m in messages if isinstance(m, ToolCall) and m.status not in ("completed", "failed")]
        self.assertEqual([(tool.tool_call_id, tool.raw_input) for tool in started],
                         [("first", {"path": "a"}), ("second", {"path": "b"})])

    async def test_immediate_interrupt(self):
        async with KiroSDKClient(KiroAgentOptions(cli_path=FAKE)) as client:
            await client.query("cancel")
            await client.interrupt()
            messages = [m async for m in client.receive_response()]
            self.assertEqual(messages[-1].stop_reason, "cancelled")

    async def test_v3_steering_during_turn(self):
        async with KiroSDKClient(KiroAgentOptions(cli_path=FAKE, agent_engine="v3")) as client:
            await client.query("steer")
            result = await client.steer("new instruction")
            self.assertTrue(result["queued"])
            messages = [m async for m in client.receive_response()]
            self.assertEqual(messages[-1].result, "new instruction")
            with self.assertRaises(CLIConnectionError):
                await client.steer("too late")
            self.assertTrue((await client.clear_steering())["cleared"])
        async with KiroSDKClient(KiroAgentOptions(cli_path=FAKE, agent_engine="v2")) as client:
            with self.assertRaises(ProtocolError):
                await client.clear_steering()

    async def test_cancelled_receiver_disconnects(self):
        client = KiroSDKClient(KiroAgentOptions(cli_path=FAKE))
        await client.connect()
        await client.query("cancel")
        stream = client.receive_response()
        receiver = asyncio.create_task(stream.__anext__())
        await asyncio.sleep(0)
        receiver.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await receiver
        self.assertFalse(client._connected)

    async def test_runtime_guard_stops_owned_process(self):
        cases = [
            ("guard_mode", KiroAgentOptions(cli_path=FAKE, agent_engine="v3", tools=[]),
             "restricted agent profile"),
            ("guard_sandbox", KiroAgentOptions(cli_path=FAKE, agent_engine="v3", require_sandbox=True),
             "sandbox stopped"),
        ]
        for prompt, options, expected in cases:
            with self.subTest(prompt=prompt):
                async with KiroSDKClient(options) as client:
                    process = client._process
                    if prompt == "guard_mode":
                        client.options.tools = None
                        with self.assertRaises(ProtocolError):
                            await client.set_mode("unrestricted")
                    else:
                        client.options.require_sandbox = False
                        with self.assertRaises(ProtocolError):
                            await client.set_config_option("sandbox", "disabled")
                    await client.query(prompt)
                    await client._send({"jsonrpc": "2.0", "method": "_test/trigger", "params": {}})
                    await asyncio.wait_for(process.wait(), 5)
                    with self.assertRaisesRegex(ProtocolError, expected):
                        _ = [message async for message in client.receive_response()]

    async def test_concurrent_queries_do_not_start_two_turns(self):
        async with KiroSDKClient(KiroAgentOptions(cli_path=FAKE)) as client:
            results = await asyncio.gather(client.query("first"), client.query("second"), return_exceptions=True)
            self.assertEqual(sum(isinstance(result, CLIConnectionError) for result in results), 1)
            messages = [m async for m in client.receive_response()]
            self.assertEqual(messages[-1].result, "first")

    async def test_query_rejects_another_session_id(self):
        async with KiroSDKClient(KiroAgentOptions(cli_path=FAKE)) as client:
            with self.assertRaises(ValueError):
                await client.query("test", session_id="different")

    async def test_partial_events_respect_thought_setting(self):
        options = KiroAgentOptions(cli_path=FAKE, include_partial_messages=True)
        messages = [m async for m in query(prompt="thought", options=options)]
        self.assertEqual(sum(isinstance(m, StreamEvent) for m in messages), 2)
        self.assertFalse(any("private thought" in repr(m) for m in messages))
        options = KiroAgentOptions(cli_path=FAKE, include_partial_messages=True, include_thoughts=True)
        messages = [m async for m in query(prompt="thought", options=options)]
        self.assertTrue(any(isinstance(m, AgentMessage) and isinstance(m.content[0], ThoughtBlock) for m in messages))
        self.assertTrue(any(isinstance(m, StreamEvent) and m.event['sessionUpdate'] == 'agent_thought_chunk' for m in messages))

    async def test_max_buffer_size(self):
        for invalid in (0, True, 1.5):
            with self.assertRaises(ValueError):
                KiroSDKClient(KiroAgentOptions(max_buffer_size=invalid))
        async with KiroSDKClient(KiroAgentOptions(cli_path=FAKE, max_buffer_size=1024)) as client:
            with self.assertRaisesRegex(ProtocolError, 'max_buffer_size'):
                await client.query('a' * 5000)
                _ = [m async for m in client.receive_response()]

    async def test_startup_timeout(self):
        with self.assertRaises(ValueError):
            KiroSDKClient(KiroAgentOptions(startup_timeout=float('nan')))
        client = KiroSDKClient(KiroAgentOptions(cli_path=FAKE, startup_timeout=0.1,
                                                 env={'FAKE_INITIALIZE_HANG': '1'}))
        with self.assertRaisesRegex(CLIConnectionError, 'Timed out'):
            await client.connect()
        self.assertFalse(client._connected)
        client = KiroSDKClient(KiroAgentOptions(cli_path=FAKE, resume='old', load_timeout_ms=100,
                                                 env={'FAKE_LOAD_HANG': '1'}))
        with self.assertRaisesRegex(CLIConnectionError, 'session/load'):
            await client.connect()
        self.assertFalse(client._connected)

    async def test_stderr_callback(self):
        chunks = []
        options = KiroAgentOptions(cli_path=FAKE, env={'FAKE_STDERR': '1'}, stderr=chunks.append)
        async with KiroSDKClient(options) as client:
            await client.query('ok')
            _ = [m async for m in client.receive_response()]
        self.assertIn('fake diagnostic', ''.join(chunks))

if __name__ == "__main__":
    unittest.main()
