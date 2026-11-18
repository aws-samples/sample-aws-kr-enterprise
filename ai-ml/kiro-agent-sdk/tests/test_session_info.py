import unittest
from unittest.mock import AsyncMock, patch

from kiro_agent_sdk import (
    InMemorySessionStore,
    SessionInfo,
    get_session_info,
    get_session_info_from_store,
    list_sessions,
    list_sessions_from_store,
    project_key_for_directory,
    rename_session_via_store,
)


class SessionInfoTests(unittest.IsolatedAsyncioTestCase):
    def test_attributes_read_the_keys_kiro_reports(self):
        listed = SessionInfo({"sessionId": "s", "cwd": "/work", "title": "Write Release Notes",
                              "updatedAt": "2026-10-29T16:20:59.007Z", "_meta": {"kiro": {"modeId": "writer"}}})
        self.assertEqual((listed.session_id, listed.title, listed.cwd, listed.agent),
                         ("s", "Write Release Notes", "/work", "writer"))
        self.assertEqual(listed.updated_at, 1793290859007)
        archived = SessionInfo({"id": "a", "title": "T", "createdAt": "2026-10-29T16:20:59.007Z",
                                "lastModifiedAt": "2026-10-29T16:21:00.007Z", "agentMode": "kiro_default",
                                "modelId": "auto", "status": "idle"})
        self.assertEqual((archived.session_id, archived.agent, archived.model, archived.status),
                         ("a", "kiro_default", "auto", "idle"))
        self.assertEqual((archived.created_at, archived.updated_at), (1793290859007, 1793290860007))
        self.assertEqual(SessionInfo({"mtime": 5}).updated_at, 5)

    async def test_cli_listing_keeps_the_raw_record(self):
        raw = {"sessionId": "sess_test", "source": "v3", "title": "A session", "status": "idle",
               "updatedAt": "2026-10-29T16:20:59.007Z"}
        with patch("kiro_agent_sdk.sessions._list_cli", new=AsyncMock(return_value=[{"sessions": [raw]}])):
            info = (await list_sessions())[0]
            fetched = await get_session_info("sess_test")
        self.assertIsInstance(info, SessionInfo)
        self.assertEqual(info, raw)
        self.assertEqual((info.title, info.source, info.status), ("A session", "v3", "idle"))
        self.assertEqual(fetched, info)
        self.assertIsNone(info.cwd)
        self.assertIsNone(info.created_at)

    async def test_store_reports_the_kiro_metadata_it_recorded(self):
        store = InMemorySessionStore()
        key = {"project_key": project_key_for_directory("."), "session_id": "store-session"}
        await store.append(key, [{"type": "prompt", "data": [{"type": "text", "text": "First prompt"}]}])
        bare = await get_session_info_from_store(store, "store-session", directory=".")
        self.assertEqual(bare.session_id, "store-session")
        self.assertIsNone(bare.title)
        await store.append(key, [{"type": "session_info", "data": {
            "sessionId": "store-session", "title": "Kiro title", "cwd": "/work",
            "updatedAt": "2026-10-29T16:20:59.007Z"}}])
        await rename_session_via_store(store, "store-session", " Store title ", directory=".")
        info = (await list_sessions_from_store(store, directory="."))[0]
        fetched = await get_session_info_from_store(store, "store-session", directory=".")
        self.assertEqual((info.session_id, info.title, info.cwd), ("store-session", "Store title", "/work"))
        self.assertEqual(info.updated_at, 1793290859007)
        self.assertEqual(fetched, info)

    def test_unreported_or_invalid_metadata_is_not_invented(self):
        info = SessionInfo({"sessionId": "s", "updatedAt": "not a timestamp", "title": ""})
        self.assertIsNone(info.title)
        self.assertIsNone(info.updated_at)
        self.assertIsNone(info.agent)
        self.assertIsNone(SessionInfo({"updatedAt": "2026-10-29T16:20:59"}).updated_at)
        self.assertIsNone(SessionInfo({"mtime": True}).updated_at)
        self.assertEqual(SessionInfo({"updatedAt": "1969-12-31T23:59:59.999Z"}).updated_at, -1)
        self.assertEqual(SessionInfo({"updatedAt": "1970-01-01T01:00:00.001+01:00"}).updated_at, 1)


if __name__ == "__main__":
    unittest.main()
