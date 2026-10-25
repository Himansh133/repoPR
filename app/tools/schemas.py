"""Argument models for the tools. The model-facing JSON schemas are generated from these,
so the schema the model sees and the validation the backend runs cannot drift apart."""

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

MAX_PATH_CHARS = 300


class _Args(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SearchCodeArgs(_Args):
    query: str = Field(min_length=1, max_length=200, description="Literal text to search for.")
    path: str | None = Field(
        default=None,
        max_length=MAX_PATH_CHARS,
        description="Optional repo-relative file or folder to limit the search.",
    )


class ReadFileArgs(_Args):
    path: str = Field(
        min_length=1, max_length=MAX_PATH_CHARS, description="Repo-relative path of the file."
    )


TOOL_MODELS: dict[str, type[_Args]] = {
    "search_code": SearchCodeArgs,
    "read_file": ReadFileArgs,
}

TOOL_DESCRIPTIONS = {
    "search_code": "Search the repository for a literal string. Returns file, line and snippet.",
    "read_file": "Read a text file from the repository. Large files are cut off.",
}


def tool_specs(names: list[str]) -> list[dict[str, Any]]:
    """Return OpenAI function-calling specs for the given tool names (KeyError if unknown)."""
    specs = []
    for name in names:
        schema = TOOL_MODELS[name].model_json_schema()
        schema.pop("title", None)
        for prop in schema.get("properties", {}).values():
            prop.pop("title", None)
        specs.append(
            {
                "type": "function",
                "function": {
                    "name": name,
                    "description": TOOL_DESCRIPTIONS[name],
                    "parameters": schema,
                },
            }
        )
    return specs
