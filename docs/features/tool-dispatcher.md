# Tool schemas and dispatcher

## What it does
- `app/tools/schemas.py`: Pydantic models for tool arguments (`SearchCodeArgs`, `ReadFileArgs`) and `tool_specs(names)`, which builds the OpenAI function-calling specs sent to the model. The specs come from the models, so the schema and the validation are the same thing.
- `app/tools/dispatcher.py`: `Dispatcher(ws, allowed_tools, limits).dispatch(ToolCall)` returns `ToolResult(tool_call_id, name, ok, content)`.

## How to use
```python
d = Dispatcher(Workspace(repo_path))
result = d.dispatch(ToolCall(id="1", name="read_file", arguments='{"path": "shop/cart.py"}'))
d.files_read   # {"shop/cart.py"}
```

## Checks, in order
1. Tool name is on the allowlist.
2. Arguments are at most 4 KB, valid JSON, and a JSON object.
3. Arguments pass the Pydantic model (required fields, types, lengths, no extra fields).
4. Limits: at most 10 distinct files read per investigation (re-reading the same file is free); results capped at 8,000 characters.
5. Workspace rules (path safety, binary, missing file).

Any failure returns `ok=False` and a short `Error: ...` message the model can use to correct itself. `dispatch` does not raise for bad model input.

## Design notes
- Successful output is wrapped in `<untrusted_repo_content tool=... path=...>`. A closing or opening tag inside file text is escaped, so a file cannot break out of the wrapper.
- Validation messages show the field and the rule, not the submitted value, so a huge input is not echoed back.
- Error text never contains absolute paths.
- `files_read` records normalized repo-relative paths. The agent uses it to check that a plan only cites files that were read.
- Only `search_code` and `read_file` exist so far. Other tools are added in their chunks.

## Limits
Reading a file counts toward the 10-file limit only if it succeeds.

## How it is tested
`tests/test_dispatcher.py`, with both search backends: valid calls, unknown and non-allowed tool, malformed JSON, non-object JSON, oversized arguments, missing/extra/wrong-type fields, shell metacharacters and flags in a query, path traversal, missing and binary files, file limit, re-read, output cap, wrapper break-out, injection text.
