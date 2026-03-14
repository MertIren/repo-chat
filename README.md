# repo-chat

Chat with GitHub repositories without cloning them. Ask questions about architecture, code, cross-repo relationships, and more. File contents are fetched from the GitHub API on demand, decoded in memory, and never written to disk.

## Features

- **CLI** (`repo-chat`): Interactive REPL backed by Claude — load one or more repos, ask questions, get streaming answers
- **MCP server** (`repo-chat-mcp`): Stateless GitHub browsing tools Claude Code can call natively

---

## Installation

```bash
git clone https://github.com/MertIren/repo-chat
cd repo-chat
pip install -e .
```

### Environment variables

| Variable | Required | Description |
|---|---|---|
| `ANTHROPIC_API_KEY` | Yes (CLI only) | Your Anthropic API key |
| `GITHUB_TOKEN` | Recommended | Personal access token — raises rate limit from 60 to 5 000 req/hr |

---

## CLI usage

```bash
# Single repo
repo-chat https://github.com/anthropics/anthropic-sdk-python

# Multiple repos
repo-chat https://github.com/owner/frontend https://github.com/owner/backend

# Specific branch
repo-chat https://github.com/owner/repo/tree/develop
```

### REPL commands

| Command | Description |
|---|---|
| `/add <url>` | Add another repo to the current session |
| `/clear` | Reset conversation history and fetched file cache |
| `/files` | List all files currently in context, grouped by repo |
| `/tree [name]` | Print the file tree for one or all loaded repos |
| `/exit` | Quit |

### Example session

```
[anthropics/anthropic-sdk-python] > How is streaming implemented?
Deciding which files to read...
Fetching 3 files across 1 repo...
Thinking...

Streaming is implemented via the `Stream` and `AsyncStream` classes in
`src/anthropic/_streaming.py`. When you call `.stream()` on the messages
API, it returns a context manager that wraps an SSE (Server-Sent Events)
connection...

Context: anthropics/anthropic-sdk-python: 3 file(s) · ~4,200 tokens
```

---

## MCP server

### Adding to Claude Code

**Option 1 — CLI (recommended):**

```bash
claude mcp add repo-chat -- repo-chat-mcp
```

**Option 2 — Manual config** (`~/.claude.json`):

```json
{
  "mcpServers": {
    "repo-chat": {
      "command": "repo-chat-mcp",
      "env": {
        "GITHUB_TOKEN": "your_token_here"
      }
    }
  }
}
```

### Available tools

| Tool | Description |
|---|---|
| `get_repo_info(owner, repo)` | Repo metadata including default branch |
| `get_file_tree(owner, repo, branch)` | Full recursive file listing |
| `get_file_content(owner, repo, branch, path)` | Single file content |
| `get_multiple_files(owner, repo, branch, paths)` | Batch fetch up to 10 files |
| `search_files(owner, repo, branch, pattern)` | Glob/substring search over file tree |
| `compare_files(files)` | Fetch the same path across multiple repos |

### Example prompts for Claude Code

```
Look at https://github.com/anthropics/anthropic-sdk-python and explain how
tool use is handled end-to-end.

Compare how https://github.com/owner/service-a and https://github.com/owner/service-b
each implement their authentication middleware.

Find all test files in https://github.com/owner/repo and summarize what
the test coverage looks like.
```

---

## How it works

1. **No disk writes** — all file content is fetched from the GitHub API, base64-decoded in memory, and discarded when the session ends
2. **Concurrent fetches** — `asyncio` + `httpx` fetch multiple files in parallel
3. **In-memory cache** — files and trees are cached by `(owner, repo, branch, path)` so repeated reads within a session are free
4. **Two-stage Claude calls** (CLI) — first Claude picks which files to read, then answers with full context
5. **Stateless MCP tools** — repo identity is passed on every call; the server caches results for the process lifetime
