# repo-chat

Chat with GitHub repositories without cloning them. File contents are fetched from the GitHub API on demand, decoded in memory, and never written to disk.

## Installation

```bash
git clone https://github.com/MertIren/repo-chat
cd repo-chat
pip install -e .
```

| Variable | Required | Description |
|---|---|---|
| `ANTHROPIC_API_KEY` | Only when using the `claude` CLI backend | Your Anthropic API key |
| `GITHUB_TOKEN` | Recommended | Raises GitHub rate limit from 60 to 5,000 req/hr; required for private repos |

---

## CLI

An interactive REPL that uses a two-stage AI pipeline: the model first picks which files to read, then answers with their contents.

By default it calls the `claude` CLI (Claude Code must be installed). To use a different model or provider, create a `.repo-chat.toml` file in your working directory:

```toml
api_key = "your-api-key"
api_url = "https://api.openai.com/v1"   # any OpenAI-compatible endpoint
model   = "gpt-4o"                       # optional
```

Works with ChatGPT (`https://api.openai.com/v1`) or Gemini (`https://generativelanguage.googleapis.com/v1beta/openai`), or any other OpenAI-compatible API. When the file is absent or empty, the `claude` CLI is used automatically.

```bash
repo-chat https://github.com/owner/repo
repo-chat https://github.com/owner/frontend https://github.com/owner/backend
repo-chat https://github.com/owner/repo/tree/develop
```

| Command | Description |
|---|---|
| `/add <url>` | Add another repo to the session |
| `/clear` | Reset conversation history and file cache |
| `/files` | List files currently in context |
| `/tree [name]` | Print file tree for one or all repos |
| `/exit` | Quit |

---

## MCP server

Exposes structured GitHub browsing tools to Claude Code. The main advantages over Claude's built-in web fetch are **private repo access** (via `GITHUB_TOKEN`) and convenience tools like `search_files` and `compare_files`.

```bash
claude mcp add repo-chat -- repo-chat-mcp
```

| Tool | Description |
|---|---|
| `get_repo_info(owner, repo)` | Repo metadata including default branch |
| `get_file_tree(owner, repo, branch)` | Full recursive file listing |
| `get_file_content(owner, repo, branch, path)` | Single file content |
| `get_multiple_files(owner, repo, branch, paths)` | Batch fetch up to 10 files in parallel |
| `search_files(owner, repo, branch, pattern)` | Glob/substring search over file tree |
| `compare_files(files)` | Fetch the same path across multiple repos |
