"""
Tool-Assisted LLM Analysis

Provides a multi-turn tool-calling conversation loop and context classes
that let the LLM pull diffs, file contents, and summaries on demand
instead of receiving everything in a single truncated prompt.

Used for major transitions (where truncation would lose meaningful changes)
and overview generation (where concatenating all narratives hits context limits).
"""

import json
import zipfile
import os
import tempfile
from typing import Callable

from snapshot_diff import SnapshotDiff, FileDiff, get_snapshot_files, _find_root_dir, _is_binary, DEFAULT_BINARY_EXTENSIONS
from utils.ai_client import BaseAIClient, AIMessage, AIResponse, ToolCall, AnthropicClient, OpenAIClient, make_api_call_with_retry
from utils.config import get_config


# ---------------------------------------------------------------------------
# Tool-calling conversation loop
# ---------------------------------------------------------------------------

def run_tool_conversation(
    ai_client: BaseAIClient,
    system_message: str,
    cached_context: list[str],
    initial_query: str,
    tools: list[dict],
    tool_handlers: dict[str, Callable],
    max_turns: int = 25,
    max_tokens: int = 4000,
) -> str:
    """
    Run a multi-turn conversation where the LLM can call tools.

    Builds API messages directly since BaseAIClient.create_message is
    single-turn only and cannot handle the multi-turn message alternation
    required by tool-calling conversations.

    Supports both Anthropic and OpenAI clients.

    Args:
        ai_client: BaseAIClient instance (Anthropic or OpenAI)
        system_message: System prompt
        cached_context: List of strings for cached/stable context blocks
        initial_query: The first user message (instructions + question)
        tools: Tool definitions in Anthropic's schema format (input_schema).
            Automatically converted for OpenAI.
        tool_handlers: Dict mapping tool name -> callable(**input) -> value
        max_turns: Safety limit on conversation rounds
        max_tokens: Max response tokens per turn

    Returns:
        The LLM's final text response (accumulated across turns)
    """
    if max_tokens <= 0:
        config = get_config()
        model_cfg = config.get('models', {}).get(ai_client.model, {})
        max_tokens = model_cfg.get('max_tokens', 8000)

    if isinstance(ai_client, AnthropicClient):
        return _run_anthropic(
            ai_client, system_message, cached_context, initial_query,
            tools, tool_handlers, max_turns, max_tokens,
        )
    elif isinstance(ai_client, OpenAIClient):
        return _run_openai(
            ai_client, system_message, cached_context, initial_query,
            tools, tool_handlers, max_turns, max_tokens,
        )
    else:
        raise NotImplementedError(
            f"Tool-assisted analysis not supported for {type(ai_client).__name__}. "
            "Supported: AnthropicClient, OpenAIClient."
        )


# ---------------------------------------------------------------------------
# Anthropic implementation
# ---------------------------------------------------------------------------

def _run_anthropic(
    ai_client: AnthropicClient,
    system_message: str,
    cached_context: list[str],
    initial_query: str,
    tools: list[dict],
    tool_handlers: dict[str, Callable],
    max_turns: int,
    max_tokens: int,
) -> str:
    # Build the first user message with cached context blocks
    first_user_content = []
    MAX_CACHE_BLOCKS = 4
    cache_blocks_added = 0
    for block in cached_context:
        entry = {"type": "text", "text": block}
        if len(block) > 4500 and cache_blocks_added < MAX_CACHE_BLOCKS:
            entry["cache_control"] = {"type": "ephemeral"}
            cache_blocks_added += 1
        first_user_content.append(entry)
    first_user_content.append({"type": "text", "text": initial_query})

    messages = [{"role": "user", "content": first_user_content}]
    accumulated_text = []

    for turn in range(max_turns):
        def _make_api_call(msgs=messages):
            return ai_client.client.messages.create(
                model=ai_client.model,
                max_tokens=max_tokens,
                system=system_message,
                tools=tools,
                messages=msgs,
            )

        response = make_api_call_with_retry(_make_api_call)

        # Extract text and tool calls from response
        text_parts = []
        tool_calls = []
        for block in response.content:
            if block.type == 'text':
                text_parts.append(block.text)
            elif block.type == 'tool_use':
                tool_calls.append(block)

        text = ''.join(text_parts)
        if text:
            accumulated_text.append(text)

        if not tool_calls:
            break

        # Add assistant response to conversation
        assistant_content = []
        if text:
            assistant_content.append({"type": "text", "text": text})
        for tc in tool_calls:
            assistant_content.append({
                "type": "tool_use",
                "id": tc.id,
                "name": tc.name,
                "input": tc.input,
            })
        messages.append({"role": "assistant", "content": assistant_content})

        # Execute tools and build result message
        tool_result_content = _execute_tools(tool_calls, tool_handlers, "anthropic")
        messages.append({"role": "user", "content": tool_result_content})

        tool_names = ', '.join(tc.name for tc in tool_calls)
        print(f"    [turn {turn + 1}] called: {tool_names}", flush=True)

    return '\n'.join(accumulated_text)


# ---------------------------------------------------------------------------
# OpenAI implementation
# ---------------------------------------------------------------------------

def _run_openai(
    ai_client: OpenAIClient,
    system_message: str,
    cached_context: list[str],
    initial_query: str,
    tools: list[dict],
    tool_handlers: dict[str, Callable],
    max_turns: int,
    max_tokens: int,
) -> str:
    # Convert tools from Anthropic schema to OpenAI schema
    openai_tools = []
    for tool in tools:
        openai_tools.append({
            "type": "function",
            "function": {
                "name": tool["name"],
                "description": tool["description"],
                "parameters": tool["input_schema"],
            },
        })

    # Build initial messages
    messages = [
        {"role": "system", "content": system_message},
    ]
    # Cached context as additional system messages
    for block in cached_context:
        messages.append({"role": "system", "content": block})
    messages.append({"role": "user", "content": initial_query})

    accumulated_text = []

    for turn in range(max_turns):
        def _make_api_call(msgs=messages):
            return ai_client.client.chat.completions.create(
                model=ai_client.model,
                max_completion_tokens=max_tokens,
                tools=openai_tools,
                messages=msgs,
            )

        response = make_api_call_with_retry(_make_api_call)
        choice = response.choices[0]
        message = choice.message

        if message.content:
            accumulated_text.append(message.content)

        if not message.tool_calls:
            break

        # Add the assistant message (with tool calls) to conversation
        messages.append({
            "role": "assistant",
            "content": message.content or "",
            "tool_calls": [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {
                        "name": tc.function.name,
                        "arguments": tc.function.arguments,
                    },
                }
                for tc in message.tool_calls
            ],
        })

        # Execute tools and add each result as a separate tool message
        for tc in message.tool_calls:
            tool_name = tc.function.name
            try:
                tool_input = json.loads(tc.function.arguments)
            except json.JSONDecodeError:
                tool_input = {}

            handler = tool_handlers.get(tool_name)
            if handler:
                try:
                    result = handler(**tool_input)
                    result_str = json.dumps(result) if not isinstance(result, str) else result
                except Exception as e:
                    result_str = f"Error: {e}"
            else:
                result_str = f"Unknown tool: {tool_name}"

            messages.append({
                "role": "tool",
                "tool_call_id": tc.id,
                "content": result_str,
            })

        tool_names = ', '.join(tc.function.name for tc in message.tool_calls)
        print(f"    [turn {turn + 1}] called: {tool_names}", flush=True)

    return '\n'.join(accumulated_text)


# ---------------------------------------------------------------------------
# Shared tool execution helper
# ---------------------------------------------------------------------------

def _execute_tools(tool_calls, tool_handlers, platform):
    """Execute tool calls and return results in the appropriate format."""
    results = []
    for tc in tool_calls:
        # Get name and input based on platform
        if platform == "anthropic":
            name, input_data, call_id = tc.name, tc.input, tc.id
        else:
            name, call_id = tc.function.name, tc.id
            try:
                input_data = json.loads(tc.function.arguments)
            except json.JSONDecodeError:
                input_data = {}

        handler = tool_handlers.get(name)
        if handler:
            try:
                result = handler(**input_data)
                result_str = json.dumps(result) if not isinstance(result, str) else result
                if platform == "anthropic":
                    results.append({
                        "type": "tool_result",
                        "tool_use_id": call_id,
                        "content": result_str,
                    })
            except Exception as e:
                if platform == "anthropic":
                    results.append({
                        "type": "tool_result",
                        "tool_use_id": call_id,
                        "content": f"Error: {e}",
                        "is_error": True,
                    })
        else:
            if platform == "anthropic":
                results.append({
                    "type": "tool_result",
                    "tool_use_id": call_id,
                    "content": f"Unknown tool: {name}",
                    "is_error": True,
                })
    return results


# ---------------------------------------------------------------------------
# Tool definitions (JSON schema format for the Anthropic API)
# ---------------------------------------------------------------------------

SNAPSHOT_TOOLS = [
    {
        "name": "get_change_summary",
        "description": (
            "Get a high-level statistical summary of this transition: "
            "counts of files added, removed, modified, moved, and total diff lines."
        ),
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
    {
        "name": "list_files_added",
        "description": "List all file paths that were added in this transition.",
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
    {
        "name": "list_files_removed",
        "description": "List all file paths that were removed in this transition.",
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
    {
        "name": "list_files_moved",
        "description": "List all files that were moved/renamed, showing old and new paths.",
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
    {
        "name": "list_files_modified",
        "description": (
            "List all modified file paths with the number of diff lines for each. "
            "Use this to decide which files to inspect in detail."
        ),
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
    {
        "name": "get_diff",
        "description": (
            "Get the full unified diff for a specific modified file. "
            "No truncation is applied; you see the complete diff."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "file_path": {
                    "type": "string",
                    "description": "The relative file path (as shown in list_files_modified).",
                },
            },
            "required": ["file_path"],
        },
    },
    {
        "name": "get_file_content",
        "description": (
            "Read the full content of a file from either the old or new snapshot. "
            "Useful for understanding context around a diff, or reading newly added files."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "snapshot": {
                    "type": "string",
                    "enum": ["old", "new"],
                    "description": "Which snapshot to read from.",
                },
                "file_path": {
                    "type": "string",
                    "description": "The relative file path to read.",
                },
            },
            "required": ["snapshot", "file_path"],
        },
    },
    {
        "name": "get_status_docs",
        "description": (
            "Get the content of developer status/documentation files (STATUS.md, "
            "CHANGELOG.md, TODO.md, etc.) from the new snapshot, plus their diffs "
            "if they were modified."
        ),
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
    {
        "name": "list_all_files",
        "description": "Get the complete file listing for either the old or new snapshot.",
        "input_schema": {
            "type": "object",
            "properties": {
                "snapshot": {
                    "type": "string",
                    "enum": ["old", "new"],
                    "description": "Which snapshot's file listing to return.",
                },
            },
            "required": ["snapshot"],
        },
    },
]


OVERVIEW_TOOLS = [
    {
        "name": "get_transition_summary",
        "description": (
            "Get the analysis narrative for a specific transition by its index. "
            "Use the transition list provided in the initial context to choose indices."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "index": {
                    "type": "integer",
                    "description": "The transition index (0-based, from the transition list).",
                },
            },
            "required": ["index"],
        },
    },
    {
        "name": "get_transition_range",
        "description": (
            "Get the analysis narratives for a range of transitions. "
            "More efficient than calling get_transition_summary repeatedly."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "start": {
                    "type": "integer",
                    "description": "Start index (inclusive, 0-based).",
                },
                "end": {
                    "type": "integer",
                    "description": "End index (inclusive, 0-based).",
                },
            },
            "required": ["start", "end"],
        },
    },
]


# ---------------------------------------------------------------------------
# SnapshotContext: tool handlers backed by precomputed snapshot data
# ---------------------------------------------------------------------------

class SnapshotContext:
    """
    Provides tool handlers for a single transition, backed by precomputed
    SnapshotDiff data and on-demand zip extraction for file contents.
    """

    def __init__(self, diff: SnapshotDiff, old_zip_path: str, new_zip_path: str,
                 binary_extensions: list[str] | None = None):
        self.diff = diff
        self.old_zip_path = old_zip_path
        self.new_zip_path = new_zip_path
        self.binary_ext = (
            set(binary_extensions) if binary_extensions
            else DEFAULT_BINARY_EXTENSIONS
        )

        # Index diffs by path for O(1) lookup
        self._diff_index = {fd.path: fd for fd in diff.modified}

        # Lazy-loaded file contents from zips
        self._old_contents: dict[str, str] | None = None
        self._new_contents: dict[str, str] | None = None

    def _load_snapshot_contents(self, snapshot: str) -> dict[str, str]:
        """Load file contents from a zip on demand."""
        if snapshot == "old":
            if self._old_contents is None:
                _, self._old_contents = get_snapshot_files(
                    self.old_zip_path, list(self.binary_ext)
                )
            return self._old_contents
        else:
            if self._new_contents is None:
                _, self._new_contents = get_snapshot_files(
                    self.new_zip_path, list(self.binary_ext)
                )
            return self._new_contents

    # -- Tool handler methods --

    def get_change_summary(self) -> dict:
        return {
            "files_added": len(self.diff.added),
            "files_removed": len(self.diff.removed),
            "files_modified": len(self.diff.modified),
            "files_moved": len(self.diff.moved),
            "files_unchanged": len(self.diff.unchanged),
            "total_diff_lines": self.diff.total_diff_lines,
            "total_lines_in_new_snapshot": self.diff.total_lines_in_new,
        }

    def list_files_added(self) -> list[str]:
        return self.diff.added

    def list_files_removed(self) -> list[str]:
        return self.diff.removed

    def list_files_moved(self) -> list[dict]:
        return [{"old_path": old, "new_path": new} for old, new in self.diff.moved]

    def list_files_modified(self) -> list[dict]:
        return [
            {"path": fd.path, "diff_lines": fd.diff_line_count}
            for fd in self.diff.modified
        ]

    def get_diff(self, file_path: str) -> str:
        fd = self._diff_index.get(file_path)
        if fd:
            return fd.diff_text
        return f"No diff found for '{file_path}'. Use list_files_modified to see available paths."

    def get_file_content(self, snapshot: str, file_path: str) -> str:
        contents = self._load_snapshot_contents(snapshot)
        if file_path in contents:
            return contents[file_path]
        return f"File '{file_path}' not found in {snapshot} snapshot."

    def get_status_docs(self) -> dict:
        result = {}
        if self.diff.status_docs:
            result["status_docs"] = {
                path: content for path, content in self.diff.status_docs.items()
            }
        if self.diff.status_doc_diffs:
            result["status_doc_diffs"] = {
                fd.path: fd.diff_text for fd in self.diff.status_doc_diffs
            }
        if not result:
            result["message"] = "No status/documentation files found in this transition."
        return result

    def list_all_files(self, snapshot: str) -> list[str]:
        if snapshot == "old":
            return self.diff.old_file_listing
        return self.diff.new_file_listing

    def get_tool_handlers(self) -> dict[str, Callable]:
        """Return a dict mapping tool names to handler callables."""
        return {
            "get_change_summary": lambda: self.get_change_summary(),
            "list_files_added": lambda: self.list_files_added(),
            "list_files_removed": lambda: self.list_files_removed(),
            "list_files_moved": lambda: self.list_files_moved(),
            "list_files_modified": lambda: self.list_files_modified(),
            "get_diff": lambda file_path: self.get_diff(file_path),
            "get_file_content": lambda snapshot, file_path: self.get_file_content(snapshot, file_path),
            "get_status_docs": lambda: self.get_status_docs(),
            "list_all_files": lambda snapshot: self.list_all_files(snapshot),
        }


# ---------------------------------------------------------------------------
# OverviewContext: tool handlers for overview generation
# ---------------------------------------------------------------------------

class OverviewContext:
    """
    Provides tool handlers for overview generation, backed by the list
    of completed analysis results.
    """

    def __init__(self, results: list, snapshot_labels: list[str]):
        """
        Args:
            results: List of AnalysisResult objects (or dicts with .narrative, .tier, .snapshot_labels)
            snapshot_labels: All snapshot labels in order
        """
        self.results = results
        self.snapshot_labels = snapshot_labels

    def get_transition_summary(self, index: int) -> dict:
        if 0 <= index < len(self.results):
            r = self.results[index]
            return {
                "index": index,
                "tier": r.tier,
                "snapshot_labels": r.snapshot_labels,
                "narrative": r.narrative,
            }
        return {"error": f"Index {index} out of range (0-{len(self.results) - 1})"}

    def get_transition_range(self, start: int, end: int) -> list[dict]:
        results = []
        for i in range(max(0, start), min(end + 1, len(self.results))):
            r = self.results[i]
            results.append({
                "index": i,
                "tier": r.tier,
                "snapshot_labels": r.snapshot_labels,
                "narrative": r.narrative,
            })
        return results

    def get_tool_handlers(self) -> dict[str, Callable]:
        return {
            "get_transition_summary": lambda index: self.get_transition_summary(index),
            "get_transition_range": lambda start, end: self.get_transition_range(start, end),
        }
