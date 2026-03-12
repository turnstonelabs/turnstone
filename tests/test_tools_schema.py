"""Tests for turnstone.core.tools — JSON auto-loading and schema validation."""

from turnstone.core.tools import (
    _META,
    AGENT_AUTO_TOOLS,
    AGENT_TOOLS,
    PRIMARY_KEY_MAP,
    TASK_AGENT_TOOLS,
    TASK_AUTO_TOOLS,
    TOOLS,
)


class TestToolsSchema:
    def test_all_tools_have_function_type(self):
        for tool in TOOLS:
            assert tool["type"] == "function", f"Tool missing type='function': {tool}"

    def test_all_tools_have_name(self):
        for tool in TOOLS:
            assert "name" in tool["function"], f"Tool missing name: {tool}"
            assert isinstance(tool["function"]["name"], str)

    def test_all_tools_have_description(self):
        for tool in TOOLS:
            assert "description" in tool["function"], f"Tool missing description: {tool}"
            assert len(tool["function"]["description"]) > 0

    def test_all_tools_have_parameters(self):
        for tool in TOOLS:
            params = tool["function"]["parameters"]
            assert params["type"] == "object"
            assert "properties" in params

    def test_required_fields_exist_in_properties(self):
        for tool in TOOLS:
            func = tool["function"]
            params = func["parameters"]
            required = params.get("required", [])
            properties = params["properties"]
            for field in required:
                assert field in properties, (
                    f"Tool '{func['name']}': required field '{field}' not in properties"
                )

    def test_tool_names_unique(self):
        names = [t["function"]["name"] for t in TOOLS]
        assert len(names) == len(set(names)), f"Duplicate tool names: {names}"

    def test_agent_tools_subset(self):
        tool_names = {t["function"]["name"] for t in TOOLS}
        agent_names = {t["function"]["name"] for t in AGENT_TOOLS}
        assert agent_names.issubset(tool_names), (
            f"AGENT_TOOLS has names not in TOOLS: {agent_names - tool_names}"
        )

    def test_task_agent_tools_subset(self):
        tool_names = {t["function"]["name"] for t in TOOLS}
        task_names = {t["function"]["name"] for t in TASK_AGENT_TOOLS}
        assert task_names.issubset(tool_names), (
            f"TASK_AGENT_TOOLS has names not in TOOLS: {task_names - tool_names}"
        )

    def test_agent_tools_not_empty(self):
        assert len(AGENT_TOOLS) > 0

    def test_task_agent_tools_not_empty(self):
        assert len(TASK_AGENT_TOOLS) > 0


class TestToolsMetadata:
    """Validate the metadata extracted from JSON files."""

    def test_tool_count(self):
        assert len(TOOLS) == 18

    def test_agent_tools_count(self):
        assert len(AGENT_TOOLS) == 9

    def test_task_agent_tools_count(self):
        assert len(TASK_AGENT_TOOLS) == 12

    def test_auto_approve_sets_match(self):
        expected = {
            "read_file",
            "search",
            "math",
            "man",
            "web_fetch",
            "web_search",
            "notify",
        }
        assert expected == AGENT_AUTO_TOOLS
        assert expected == TASK_AUTO_TOOLS

    def test_primary_key_map(self):
        expected = {
            "bash": "command",
            "math": "code",
            "read_file": "path",
            "search": "query",
            "write_file": "content",
            "edit_file": "old_string",
            "man": "page",
            "web_fetch": "url",
            "web_search": "query",
            "task": "prompt",
            "create_plan": "goal",
            "remember": "key",
            "recall": "query",
            "forget": "key",
            "notify": "message",
            "watch": "command",
            "read_resource": "uri",
            "use_prompt": "name",
        }
        assert expected == PRIMARY_KEY_MAP

    def test_no_metadata_in_function_dicts(self):
        """Ensure turnstone metadata keys are stripped from the OpenAI schema."""
        meta_keys = {"agent", "task_agent", "auto_approve", "primary_key"}
        for tool in TOOLS:
            func = tool["function"]
            leaked = meta_keys & set(func)
            assert not leaked, f"Tool '{func['name']}' leaks metadata into function dict: {leaked}"

    def test_meta_has_all_tools(self):
        tool_names = {t["function"]["name"] for t in TOOLS}
        assert set(_META.keys()) == tool_names
