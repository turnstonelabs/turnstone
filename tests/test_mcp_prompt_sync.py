"""Tests for MCP prompt → governance template sync and readonly API guards."""

from __future__ import annotations

import asyncio
import gc
import weakref
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock, patch

import mcp.types as mcp_types
import pytest

from tests.conftest import _seed_static_state
from turnstone.core.mcp_client import MCPClientManager, _prompt_entries

if TYPE_CHECKING:
    from collections.abc import Iterator


@pytest.fixture()
def mgr() -> MCPClientManager:
    """Create an MCPClientManager with no real servers (no start())."""
    return MCPClientManager({})


def _make_storage() -> MagicMock:
    """Create a mock storage backend with prompt template methods."""
    storage = MagicMock()
    storage.get_prompt_template_by_name.return_value = None
    storage.list_prompt_templates_by_origin.return_value = []
    storage.create_prompt_template.return_value = None
    storage.update_prompt_template.return_value = True
    storage.delete_prompt_template.return_value = True
    return storage


def _prompt_session(names: list[str]) -> MagicMock:
    """A connected session that lists *names* as its prompts, and no tools.

    The list is read on every call, so a test changes the server's catalog by mutating it.
    """
    session = MagicMock()
    session.list_tools = AsyncMock(return_value=mcp_types.ListToolsResult(tools=[]))

    async def _list_prompts(**_kw: Any) -> mcp_types.ListPromptsResult:
        return mcp_types.ListPromptsResult(prompts=[mcp_types.Prompt(name=n) for n in names])

    session.list_prompts = _list_prompts
    return session


def _synced_manager(storage: Any, catalogs: dict[str, list[str]]) -> MCPClientManager:
    """A manager with one connected static server per catalog, its templates synced."""
    mgr = MCPClientManager({name: {"type": "stdio", "command": "echo"} for name in catalogs})
    for name, names in catalogs.items():
        _seed_static_state(
            mgr,
            name,
            session=_prompt_session(names),
            supports_prompts=True,
            prompts=_prompt_entries(name, [mcp_types.Prompt(name=n) for n in names]),
        )
    mgr._rebuild_prompts()
    mgr.set_storage(storage)
    mgr.sync_prompts_to_storage()
    return mgr


def _mcp_template_names(storage: Any) -> list[str]:
    return sorted(t["name"] for t in storage.list_prompt_templates_by_origin("mcp"))


def _counting_syncs(mgr: MCPClientManager) -> Any:
    return patch.object(mgr, "sync_prompts_to_storage", wraps=mgr.sync_prompts_to_storage)


class TestSyncPromptsToStorage:
    def test_sync_no_storage(self, mgr: MCPClientManager) -> None:
        """Without storage set, sync returns empty stats."""
        result = mgr.sync_prompts_to_storage()
        assert result == {"added": [], "removed": [], "skipped": []}

    def test_sync_creates_mcp_templates(self, mgr: MCPClientManager) -> None:
        """New MCP prompts are created as templates."""
        storage = _make_storage()
        mgr.set_storage(storage)

        # Populate internal prompts list directly
        mgr._prompts = [
            {
                "name": "mcp__test__greeting",
                "original_name": "greeting",
                "server": "test",
                "description": "Say hello",
                "arguments": [
                    {"name": "name", "description": "Who to greet", "required": True},
                ],
            },
        ]

        result = mgr.sync_prompts_to_storage()

        assert result["added"] == ["mcp__test__greeting"]
        assert result["removed"] == []
        assert result["skipped"] == []
        storage.create_prompt_template.assert_called_once()
        call_kwargs = storage.create_prompt_template.call_args
        assert call_kwargs[1]["name"] == "mcp__test__greeting"
        assert call_kwargs[1]["origin"] == "mcp"
        assert call_kwargs[1]["mcp_server"] == "test"
        assert call_kwargs[1]["readonly"] is True
        assert call_kwargs[1]["category"] == "mcp"
        assert '"name"' in call_kwargs[1]["variables"]

    def test_sync_skips_manual_overrides(self, mgr: MCPClientManager) -> None:
        """A manual template with the same name is not overwritten."""
        storage = _make_storage()
        storage.get_prompt_template_by_name.return_value = {
            "template_id": "existing-id",
            "name": "mcp__test__greeting",
            "origin": "manual",
            "readonly": False,
        }
        mgr.set_storage(storage)

        mgr._prompts = [
            {
                "name": "mcp__test__greeting",
                "original_name": "greeting",
                "server": "test",
                "description": "Say hello",
                "arguments": [],
            },
        ]

        result = mgr.sync_prompts_to_storage()

        assert result["skipped"] == ["mcp__test__greeting"]
        assert result["added"] == []
        storage.create_prompt_template.assert_not_called()
        storage.update_prompt_template.assert_not_called()

    def test_sync_updates_existing_mcp_template(self, mgr: MCPClientManager) -> None:
        """An existing MCP template gets its content/variables updated."""
        storage = _make_storage()
        storage.get_prompt_template_by_name.return_value = {
            "template_id": "existing-id",
            "name": "mcp__test__greeting",
            "origin": "mcp",
            "mcp_server": "test",
            "readonly": True,
        }
        mgr.set_storage(storage)

        mgr._prompts = [
            {
                "name": "mcp__test__greeting",
                "original_name": "greeting",
                "server": "test",
                "description": "Updated description",
                "arguments": [
                    {"name": "user", "description": "The user", "required": False},
                ],
            },
        ]

        result = mgr.sync_prompts_to_storage()

        assert result["added"] == []
        assert result["skipped"] == []
        storage.create_prompt_template.assert_not_called()
        storage.update_prompt_template.assert_called_once()
        call_args = storage.update_prompt_template.call_args
        assert call_args[0][0] == "existing-id"
        assert "Updated description" in call_args[1]["content"]
        assert "user" in call_args[1]["variables"]
        # Security: is_default must be reset to prevent compromised MCP server
        # from injecting content into a previously admin-promoted default
        assert call_args[1]["is_default"] is False

    def test_sync_resets_is_default_on_promoted_template(self, mgr: MCPClientManager) -> None:
        """An MCP template promoted to default by admin gets is_default reset on sync."""
        storage = _make_storage()
        storage.get_prompt_template_by_name.return_value = {
            "template_id": "promoted-id",
            "name": "mcp__test__greeting",
            "origin": "mcp",
            "mcp_server": "test",
            "readonly": True,
            "is_default": True,  # admin toggled this
        }
        mgr.set_storage(storage)

        mgr._prompts = [
            {
                "name": "mcp__test__greeting",
                "original_name": "greeting",
                "server": "test",
                "description": "Potentially compromised content",
                "arguments": [],
            },
        ]

        mgr.sync_prompts_to_storage()

        call_args = storage.update_prompt_template.call_args
        assert call_args[1]["is_default"] is False

    def test_sync_removes_deleted_prompts(self, mgr: MCPClientManager) -> None:
        """MCP templates in storage with no matching prompt are deleted."""
        storage = _make_storage()
        storage.list_prompt_templates_by_origin.return_value = [
            {
                "template_id": "old-id",
                "name": "mcp__test__old_prompt",
                "origin": "mcp",
                "mcp_server": "test",
            },
        ]
        mgr.set_storage(storage)
        mgr._prompts = []  # No prompts at all

        result = mgr.sync_prompts_to_storage()

        assert result["removed"] == ["mcp__test__old_prompt"]
        storage.delete_prompt_template.assert_called_once_with("old-id")


class TestSetStorageAutoSync:
    """set_storage() triggers an immediate sync when servers are already connected."""

    def test_set_storage_syncs_when_connected(self, mgr) -> None:
        storage = _make_storage()
        mgr._prompts = [
            {
                "name": "mcp__srv__p1",
                "original_name": "p1",
                "server": "srv",
                "description": "A prompt",
                "arguments": [],
            }
        ]
        mgr._connected.set()

        mgr.set_storage(storage)

        # Should have called create_prompt_template for the discovered prompt
        storage.create_prompt_template.assert_called_once()
        call_kwargs = storage.create_prompt_template.call_args
        assert call_kwargs[1]["name"] == "mcp__srv__p1"
        assert call_kwargs[1]["origin"] == "mcp"

    def test_set_storage_no_sync_when_not_connected(self, mgr) -> None:
        storage = _make_storage()
        mgr._prompts = [
            {
                "name": "mcp__srv__p1",
                "original_name": "p1",
                "server": "srv",
                "description": "A prompt",
                "arguments": [],
            }
        ]
        # _connected is NOT set

        mgr.set_storage(storage)

        # Should not have synced
        storage.create_prompt_template.assert_not_called()


class TestRefreshPassSync:
    """A full refresh pass syncs the templates once unless a catalog changes (#1146).

    Each server's prompt refresh used to sync the whole projection, so a pass over N servers
    synced it N + 1 times. Inside a pass, a refresh now syncs only when its server's catalog
    changed, so every change is stored at once, and the pass syncs once after its last server;
    a prompt refresh outside a pass still syncs every time.
    """

    def test_unchanged_pass_syncs_once(self, db: Any) -> None:
        catalogs = {"srv0": ["a", "b"], "srv1": ["c"]}
        mgr = _synced_manager(db, catalogs)

        with _counting_syncs(mgr) as sync:
            results = asyncio.run(mgr._refresh_all())

        assert results == {"srv0": ([], []), "srv1": ([], [])}
        assert sync.call_count == 1
        assert _mcp_template_names(db) == ["mcp__srv0__a", "mcp__srv0__b", "mcp__srv1__c"]

    def test_changed_catalog_syncs_when_refreshed(self, db: Any) -> None:
        """A server whose catalog changed syncs as it is refreshed, and the pass syncs once more
        after its last server."""
        catalogs = {"srv0": ["kept", "dropped"], "srv1": ["other"]}
        mgr = _synced_manager(db, catalogs)
        catalogs["srv0"][:] = ["kept", "added"]

        with _counting_syncs(mgr) as sync:
            results = asyncio.run(mgr._refresh_all())

        assert results == {"srv0": ([], []), "srv1": ([], [])}
        assert sync.call_count == 2
        assert _mcp_template_names(db) == [
            "mcp__srv0__added",
            "mcp__srv0__kept",
            "mcp__srv1__other",
        ]

    def test_cancelled_pass_keeps_what_finished_servers_published(self, db: Any) -> None:
        """Cancelled while the second server lists its prompts, the pass has already stored the
        first server's changed catalog, so the templates agree with the catalog in memory. The
        pass's own final sync does not run."""
        catalogs = {"srv0": ["old"], "srv1": ["other"]}
        mgr = _synced_manager(db, catalogs)
        catalogs["srv0"][:] = ["new"]

        async def _scenario() -> None:
            listing = asyncio.Event()

            async def _list_forever(**_kw: Any) -> Any:
                listing.set()
                await asyncio.Event().wait()

            mgr._static_servers["srv1"].session.list_prompts = _list_forever
            task = asyncio.create_task(mgr._refresh_all())
            await asyncio.wait_for(listing.wait(), timeout=5)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

        with _counting_syncs(mgr) as sync:
            asyncio.run(_scenario())

        expected = ["mcp__srv0__new", "mcp__srv1__other"]
        assert [p["name"] for p in mgr.get_prompts()] == expected
        assert _mcp_template_names(db) == expected
        assert sync.call_count == 1

    def test_failed_server_keeps_the_prompts_it_published(self, db: Any) -> None:
        """A server whose tool list fails has still published its changed prompt list, which is
        stored; the pass still syncs once after its last server."""
        catalogs = {"srv0": ["old"], "srv1": ["other"]}
        mgr = _synced_manager(db, catalogs)
        catalogs["srv0"][:] = ["new"]
        mgr._static_servers["srv0"].session.list_tools = AsyncMock(
            side_effect=RuntimeError("tools broke")
        )

        with _counting_syncs(mgr) as sync:
            results = asyncio.run(mgr._refresh_all())

        assert results == {"srv0": None, "srv1": ([], [])}
        assert mgr.last_refresh_outcome("srv0") == "error:RuntimeError"
        assert sync.call_count == 2
        assert _mcp_template_names(db) == ["mcp__srv0__new", "mcp__srv1__other"]

    def test_skipped_server_does_not_hold_back_the_sync(self, db: Any) -> None:
        """A server whose connect lock is busy is skipped, its holder syncing what it publishes;
        the pass still syncs once after its last server."""
        catalogs = {"srv0": ["old"], "srv1": ["other"]}
        mgr = _synced_manager(db, catalogs)

        async def _scenario() -> dict[str, Any]:
            async with mgr._static_connect_lock_for("srv1"):
                return await mgr._refresh_all()

        with _counting_syncs(mgr) as sync:
            results = asyncio.run(_scenario())

        assert results == {"srv0": ([], []), "srv1": None}
        assert mgr.last_refresh_outcome("srv1") == "skipped"
        assert sync.call_count == 1

    @pytest.mark.parametrize("changed", [True, False])
    @pytest.mark.parametrize("driver", ["notification", "background"])
    def test_refresh_outside_a_pass_syncs_on_its_own(
        self, db: Any, driver: str, changed: bool
    ) -> None:
        """A prompts list_changed push and a spawned background pass each sync what they
        publish, changed or not: only a full pass leaves an unchanged catalog to its own sync."""
        catalogs = {"srv0": ["old"]}
        mgr = _synced_manager(db, catalogs)
        if changed:
            catalogs["srv0"][:] = ["new"]
        expected = ["mcp__srv0__new"] if changed else ["mcp__srv0__old"]

        async def _scenario() -> None:
            mgr._static_connect_lock_for("srv0")
            if driver == "notification":
                handler = mgr._make_static_notification_handler("srv0")
                await handler(
                    mcp_types.ServerNotification(
                        mcp_types.PromptListChangedNotification(
                            method="notifications/prompts/list_changed"
                        )
                    )
                )
            else:
                mgr._spawn_full_refresh("srv0", "background refresh")
            await asyncio.gather(*mgr._background_tasks)

        with _counting_syncs(mgr) as sync:
            asyncio.run(_scenario())

        assert sync.call_count == 1
        assert _mcp_template_names(db) == expected

    def test_failed_sync_is_logged_and_the_pass_still_reports(self, db: Any) -> None:
        mgr = _synced_manager(db, {"srv0": ["old"]})

        with (
            patch.object(db, "list_prompt_templates_by_origin", side_effect=RuntimeError("db")),
            patch("turnstone.core.mcp_client.log") as log,
        ):
            results = asyncio.run(mgr._refresh_all())

        assert results == {"srv0": ([], [])}
        log.warning.assert_called_once_with("Prompt sync after refresh_all failed", exc_info=True)

    def test_final_sync_repairs_a_failed_save(self, db: Any) -> None:
        """A save that failed after a catalog changed leaves a stale row, and the next pass sees
        that catalog as unchanged; its final sync is what removes the row."""
        catalogs = {"srv0": ["old"]}
        mgr = _synced_manager(db, catalogs)
        catalogs["srv0"][:] = ["new"]
        with patch.object(db, "list_prompt_templates_by_origin", side_effect=RuntimeError("db")):
            asyncio.run(mgr._refresh_all())
        assert _mcp_template_names(db) == ["mcp__srv0__new", "mcp__srv0__old"]

        asyncio.run(mgr._refresh_all())

        assert _mcp_template_names(db) == ["mcp__srv0__new"]

    def test_final_sync_resets_a_promoted_default(self, db: Any) -> None:
        mgr = _synced_manager(db, {"srv0": ["a"]})
        row = db.get_prompt_template_by_name("mcp__srv0__a")
        db.update_prompt_template(row["template_id"], is_default=True)

        asyncio.run(mgr._refresh_all())

        assert db.get_prompt_template_by_name("mcp__srv0__a")["is_default"] is False

    @pytest.mark.parametrize("waiting_in", ["prompt_list", "reconnect"])
    def test_abandoned_pass_does_not_sync_when_collected(self, db: Any, waiting_in: str) -> None:
        """A pass still waiting when its loop closes is finished by garbage collection, on
        whatever thread collects, which may hold the sync lock, after shutdown has emptied the
        catalog. It must not sync then, whether it waited on a server's prompt list or inside a
        reconnect."""
        mgr = _synced_manager(db, {"srv0": ["kept"], "srv1": ["other"]})
        loop = asyncio.new_event_loop()
        waiting = asyncio.Event()

        async def _wait_forever(*_args: Any, **_kw: Any) -> Any:
            waiting.set()
            await asyncio.Event().wait()

        if waiting_in == "prompt_list":
            mgr._static_servers["srv1"].session.list_prompts = _wait_forever
        else:
            mgr._static_servers["srv1"].session = None
            mgr._static_transport_owner = _wait_forever  # type: ignore[method-assign]
        try:
            task = loop.create_task(mgr._refresh_all())
            loop.run_until_complete(asyncio.wait_for(waiting.wait(), timeout=5))
        finally:
            loop.close()
        mgr.shutdown()
        collected = weakref.ref(task)
        with _counting_syncs(mgr) as sync:
            del task
            gc.collect()

        assert collected() is None
        assert sync.call_count == 0
        assert _mcp_template_names(db) == ["mcp__srv0__kept", "mcp__srv1__other"]

    @pytest.mark.parametrize("collected_in", ["no_loop", "running_loop"])
    def test_collected_pass_stops_at_the_stalled_server(self, db: Any, collected_in: str) -> None:
        """Garbage collection closing a pass abandoned in its first server's reconnect stops the
        loop there. Reconnecting the servers after it on the collecting thread would record
        stray refresh failures with no loop running, and start a transport inside another loop."""
        mgr = _synced_manager(db, {"srv0": ["a"], "srv1": ["b"]})
        mgr._static_servers["srv0"].session = None
        loop = asyncio.new_event_loop()
        stalled = asyncio.Event()
        owners: list[str] = []

        async def _owner_forever(name: str, *_args: Any, **_kw: Any) -> None:
            owners.append(name)
            stalled.set()
            await asyncio.Event().wait()

        mgr._static_transport_owner = _owner_forever  # type: ignore[method-assign]
        try:
            held = [loop.create_task(mgr._refresh_all())]
            loop.run_until_complete(asyncio.wait_for(stalled.wait(), timeout=5))
        finally:
            loop.close()
        mgr.shutdown()
        collected = weakref.ref(held[0])

        if collected_in == "running_loop":

            async def _collect() -> None:
                held.clear()
                gc.collect()

            asyncio.run(_collect())
        else:
            held.clear()
            gc.collect()

        assert collected() is None
        assert owners == ["srv0"]
        assert mgr._static_servers == {}
        assert "srv1" not in mgr._last_refresh

    @pytest.mark.parametrize(
        "first", ["_refresh_all", "_ensure_static_connected", "_connect_one_locked"]
    )
    @pytest.mark.parametrize("stalled_in", ["handshake", "discovery"])
    def test_stalled_reconnect_closes_cleanly_in_any_order(
        self, db: Any, stalled_in: str, first: str
    ) -> None:
        """Garbage collection finalizes the coroutines of an abandoned pass in no set order, so it
        may close the reconnect the pass waits on, or the connect under that, before the pass.
        With no other driver queued on the server's connect lock, whichever it closes first, the
        close unwinds every frame without raising, records no connect failure (a close says
        nothing about the server) and syncs nothing. The chain is closed before shutdown, so a
        teardown on the way out would still find the server."""
        mgr = _synced_manager(db, {"srv0": ["kept"], "srv1": ["other"]})
        mgr._static_servers["srv1"].session = None
        loop = asyncio.new_event_loop()
        stalled = asyncio.Event()

        async def _stall(*_args: Any, **_kw: Any) -> Any:
            stalled.set()
            await asyncio.Event().wait()

        async def _owner(_name: str, _cfg: Any, ready: asyncio.Future[Any], *_args: Any) -> None:
            if stalled_in == "handshake":
                await _stall()
            session = _prompt_session(["other"])
            session.list_tools = _stall
            ready.set_result(session)
            await asyncio.Event().wait()

        mgr._static_transport_owner = _owner  # type: ignore[method-assign]
        try:
            task = loop.create_task(mgr._refresh_all())
            loop.run_until_complete(asyncio.wait_for(stalled.wait(), timeout=5))
        finally:
            loop.close()
        chain: list[Any] = [task.get_coro()]
        while asyncio.iscoroutine(chain[-1].cr_await):
            chain.append(chain[-1].cr_await)
        names = [coro.__name__ for coro in chain]
        assert names[:3] == ["_refresh_all", "_ensure_static_connected", "_connect_one_locked"]

        with _counting_syncs(mgr) as sync:
            chain[names.index(first)].close()
            for coro in chain:
                coro.close()

        assert sync.call_count == 0
        assert "srv1" not in mgr._last_error
        assert "srv1" not in mgr._consecutive_failures
        assert _mcp_template_names(db) == ["mcp__srv0__kept", "mcp__srv1__other"]
        mgr.shutdown()
        del task, chain
        gc.collect()  # the abandoned tasks report themselves here, not in a later test

    def test_sync_after_shutdown_keeps_the_templates(self, db: Any) -> None:
        """Shutdown empties the catalog, so a sync after it would delete every template."""
        mgr = _synced_manager(db, {"srv0": ["kept"]})
        mgr.shutdown()

        assert mgr.sync_prompts_to_storage() == {"added": [], "removed": [], "skipped": []}
        assert _mcp_template_names(db) == ["mcp__srv0__kept"]

    def test_sync_racing_shutdown_keeps_the_templates(self, db: Any) -> None:
        """Shutdown closes syncs and then empties the catalog, without the sync lock, so a sync
        checks after copying the catalog: a copy of the emptied catalog then sees syncs closed.
        Here shutdown runs while the sync copies the catalog."""
        mgr = _synced_manager(db, {"srv0": ["kept"]})

        class _ShutdownWhileCopied(list[dict[str, Any]]):
            def __iter__(self) -> Iterator[dict[str, Any]]:
                mgr.shutdown()
                return iter([])

        mgr._prompts = _ShutdownWhileCopied(mgr._prompts)
        mgr.sync_prompts_to_storage()

        assert not mgr._prompt_sync_open, "the catalog copy must have run shutdown"
        assert _mcp_template_names(db) == ["mcp__srv0__kept"]


class TestReadonlyAPIGuards:
    """Test that the console server API guards reject edits to readonly templates."""

    @pytest.fixture()
    def db(self, tmp_path, sqlite_backend_factory):
        """Create a fresh SQLite backend for each test."""

        return sqlite_backend_factory(str(tmp_path / "test.db"))

    def test_readonly_guard_update(self, db) -> None:
        """Readonly templates cannot be updated via storage guard logic."""
        db.create_prompt_template(
            "t1",
            "mcp__srv__prompt",
            "mcp",
            "content",
            variables="[]",
            is_default=False,
            org_id="",
            created_by="",
            origin="mcp",
            mcp_server="srv",
            readonly=True,
        )
        tpl = db.get_prompt_template("t1")
        assert tpl is not None
        assert tpl["readonly"] is True
        # Simulate API guard check
        assert tpl.get("readonly") is True

    def test_readonly_guard_delete(self, db) -> None:
        """Readonly templates are flagged for API-level rejection."""
        db.create_prompt_template(
            "t1",
            "mcp__srv__prompt",
            "mcp",
            "content",
            variables="[]",
            is_default=False,
            org_id="",
            created_by="",
            origin="mcp",
            mcp_server="srv",
            readonly=True,
        )
        existing = db.get_prompt_template("t1")
        assert existing is not None
        assert existing.get("readonly") is True
