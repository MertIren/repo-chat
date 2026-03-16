import asyncio
import json
import re
import sys
from dataclasses import dataclass, field

import httpx
from rich import box
from rich.console import Console
from rich.markdown import Markdown
from rich.table import Table

from .config import Config, load_config
from .github_client import GitHubClient, RateLimitError
from .url_parser import RepoSpec, parse_github_url

console = Console()

# ---------------------------------------------------------------------------
# Claude CLI backend (default)
# ---------------------------------------------------------------------------

_CLAUDE_CMD = ["claude", "--no-session-persistence", "--tools", ""]


async def _call_claude(system: str, prompt: str) -> str:
    """One-shot Claude CLI call, returns full text response."""
    proc = await asyncio.create_subprocess_exec(
        *_CLAUDE_CMD, "-p", prompt, "--system-prompt", system,
        "--output-format", "text",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(stderr.decode().strip() or "claude CLI failed")
    return stdout.decode().strip()


async def _stream_claude(system: str, prompt: str):
    """Async generator that yields text chunks as they stream from Claude CLI."""
    proc = await asyncio.create_subprocess_exec(
        *_CLAUDE_CMD, "-p", prompt, "--system-prompt", system,
        "--output-format", "stream-json", "--verbose", "--include-partial-messages",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    prev_len = 0
    async for raw in proc.stdout:
        line = raw.decode().strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("type") == "assistant":
            for block in event.get("message", {}).get("content", []):
                if block.get("type") == "text":
                    full = block["text"]
                    if len(full) > prev_len:
                        yield full[prev_len:]
                        prev_len = len(full)
    await proc.wait()


# ---------------------------------------------------------------------------
# Direct API backend (any OpenAI-compatible endpoint)
# ---------------------------------------------------------------------------

def _api_messages(system: str, prompt: str) -> list[dict]:
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": prompt},
    ]


async def _call_api(client: httpx.AsyncClient, cfg: Config, system: str, prompt: str) -> str:
    """One-shot call to an OpenAI-compatible /chat/completions endpoint."""
    body: dict = {"messages": _api_messages(system, prompt), "stream": False}
    if cfg.model:
        body["model"] = cfg.model
    resp = await client.post(
        f"{cfg.api_url}/chat/completions",
        json=body,
        headers={"Authorization": f"Bearer {cfg.api_key}"},
        timeout=60.0,
    )
    resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"]


async def _stream_api(client: httpx.AsyncClient, cfg: Config, system: str, prompt: str):
    """Async generator that streams from an OpenAI-compatible SSE endpoint."""
    body: dict = {"messages": _api_messages(system, prompt), "stream": True}
    if cfg.model:
        body["model"] = cfg.model
    async with client.stream(
        "POST",
        f"{cfg.api_url}/chat/completions",
        json=body,
        headers={"Authorization": f"Bearer {cfg.api_key}"},
        timeout=None,
    ) as resp:
        resp.raise_for_status()
        async for line in resp.aiter_lines():
            if not line.startswith("data: "):
                continue
            payload = line[6:]
            if payload == "[DONE]":
                break
            try:
                chunk = json.loads(payload)
                content = chunk["choices"][0]["delta"].get("content", "")
                if content:
                    yield content
            except (json.JSONDecodeError, KeyError, IndexError):
                continue


@dataclass
class RepoChatSession:
    repos: list[RepoSpec] = field(default_factory=list)
    file_trees: dict[str, list[str]] = field(default_factory=dict)  # "owner/repo@branch" -> paths
    fetched_files: dict[tuple[str, str, str, str], str] = field(default_factory=dict)
    conversation: list[dict] = field(default_factory=list)


class RepoChatCLI:
    def __init__(self, initial_urls: list[str], cfg: Config | None = None) -> None:
        self.initial_urls = initial_urls
        self.cfg = cfg or Config()
        self.gh = GitHubClient()
        self.session = RepoChatSession()
        self._api: httpx.AsyncClient | None = (
            httpx.AsyncClient() if self.cfg.use_direct_api else None
        )

    # ------------------------------------------------------------------
    # Repo management
    # ------------------------------------------------------------------

    async def _load_repo(self, url: str) -> RepoSpec:
        spec = parse_github_url(url)
        if not spec.branch:
            info = await self.gh.get_repo_info(spec.owner, spec.repo)
            spec.branch = info["default_branch"]
        files = await self.gh.get_file_tree(spec.owner, spec.repo, spec.branch)
        tree_key = f"{spec.owner}/{spec.repo}@{spec.branch}"
        self.session.file_trees[tree_key] = files
        self.session.repos.append(spec)
        return spec

    # ------------------------------------------------------------------
    # Context builders
    # ------------------------------------------------------------------

    def _trees_context(self) -> str:
        parts = []
        for spec in self.session.repos:
            key = f"{spec.owner}/{spec.repo}@{spec.branch}"
            files = self.session.file_trees.get(key, [])
            parts.append(
                f"Repository: {spec.owner}/{spec.repo} (branch: {spec.branch})\n"
                + "\n".join(files)
            )
        return "\n\n".join(parts)

    def _files_context(self) -> str:
        if not self.session.fetched_files:
            return ""
        by_repo: dict[str, list[tuple[str, str]]] = {}
        for (owner, repo, branch, path), content in self.session.fetched_files.items():
            k = f"{owner}/{repo}@{branch}"
            by_repo.setdefault(k, []).append((path, content))
        parts = []
        for repo_key, entries in by_repo.items():
            parts.append(f"=== Files from {repo_key} ===")
            for path, content in entries:
                parts.append(f"--- {path} ---\n{content}")
        return "\n\n".join(parts)

    # ------------------------------------------------------------------
    # Backend dispatch helpers
    # ------------------------------------------------------------------

    async def _call(self, system: str, prompt: str) -> str:
        if self.cfg.use_direct_api:
            return await _call_api(self._api, self.cfg, system, prompt)
        return await _call_claude(system, prompt)

    async def _stream(self, system: str, prompt: str):
        if self.cfg.use_direct_api:
            async for chunk in _stream_api(self._api, self.cfg, system, prompt):
                yield chunk
        else:
            async for chunk in _stream_claude(system, prompt):
                yield chunk

    # ------------------------------------------------------------------
    # AI calls
    # ------------------------------------------------------------------

    async def _select_files(self, question: str) -> list[dict]:
        trees = self._trees_context()
        fetched = self._files_context()

        system = (
            "You are a file selector. Given file trees from one or more GitHub repositories "
            "and a user question, choose 0–8 files that are most relevant to answer the question.\n\n"
            "Return ONLY valid JSON in this exact format with no explanation:\n"
            '{"files": [{"owner": "...", "repo": "...", "branch": "...", "path": "..."}]}\n\n'
            "If no files are needed (e.g. the question is about tree structure), return: "
            '{"files": []}'
        )
        user_msg = f"## File Trees\n{trees}\n\n"
        if fetched:
            user_msg += f"## Already Fetched Files (reuse these if sufficient)\n{fetched}\n\n"
        user_msg += f"## Question\n{question}"

        text = await self._call(system, user_msg)
        # Strip markdown code fences if present
        m = re.search(r"```(?:json)?\s*(.*?)\s*```", text, re.DOTALL)
        if m:
            text = m.group(1)
        try:
            return json.loads(text).get("files", [])
        except Exception:
            return []

    async def _fetch_selected(self, file_specs: list[dict]) -> int:
        to_fetch = [
            s for s in file_specs
            if (s["owner"], s["repo"], s["branch"], s["path"]) not in self.session.fetched_files
        ]
        if not to_fetch:
            return 0
        tasks = [
            self.gh.get_file_content(s["owner"], s["repo"], s["branch"], s["path"])
            for s in to_fetch
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        fetched_count = 0
        for spec, result in zip(to_fetch, results):
            if not isinstance(result, Exception):
                key = (spec["owner"], spec["repo"], spec["branch"], spec["path"])
                self.session.fetched_files[key] = result
                fetched_count += 1
        return fetched_count

    async def _answer(self, question: str) -> str:
        trees = self._trees_context()
        fetched = self._files_context()

        system = f"You are a helpful assistant that answers questions about GitHub repositories.\n\n## File Trees\n{trees}"
        if fetched:
            system += f"\n\n## File Contents\n{fetched}"

        # Embed prior conversation turns so the stateless CLI has context
        if self.session.conversation:
            history = "\n\n".join(
                f"{'User' if m['role'] == 'user' else 'Assistant'}: {m['content']}"
                for m in self.session.conversation
            )
            system += f"\n\n## Conversation so far\n{history}\n\nContinue the conversation by responding to the latest User message."

        self.session.conversation.append({"role": "user", "content": question})

        full_response = ""
        console.print("[dim]...[/dim]", end="\r")
        async for chunk in self._stream(system, question):
            full_response += chunk
        console.print(" " * 10, end="\r")  # clear the "..." line
        console.rule(style="dim")
        console.print(Markdown(full_response, justify="left"))
        console.rule(style="dim")

        self.session.conversation.append({"role": "assistant", "content": full_response})
        return full_response

    # ------------------------------------------------------------------
    # Context summary
    # ------------------------------------------------------------------

    def _show_context_summary(self) -> None:
        by_repo: dict[str, int] = {}
        for (owner, repo, _, _) in self.session.fetched_files:
            k = f"{owner}/{repo}"
            by_repo[k] = by_repo.get(k, 0) + 1
        total_chars = sum(len(c) for c in self.session.fetched_files.values())
        approx_tokens = total_chars // 4
        if not by_repo and not self.session.fetched_files:
            return
        parts = [f"{repo}: {n} file{'s' if n != 1 else ''}" for repo, n in by_repo.items()]
        parts.append(f"~{approx_tokens:,} tokens")
        console.print(f"\n[dim italic]  \u21b3 {' \u00b7 '.join(parts)}[/dim italic]\n")

    # ------------------------------------------------------------------
    # Commands
    # ------------------------------------------------------------------

    async def _handle_command(self, cmd: str) -> bool:
        """Return False to exit."""
        parts = cmd.strip().split(None, 1)
        name = parts[0].lower()
        arg = parts[1].strip() if len(parts) > 1 else ""

        if name == "/exit":
            console.print("[dim]Goodbye![/dim]")
            return False

        elif name == "/add":
            if not arg:
                console.print("[yellow]Usage: /add <github-url>[/yellow]")
            else:
                try:
                    with console.status(f"[dim]Loading {arg}...[/dim]"):
                        spec = await self._load_repo(arg)
                    key = f"{spec.owner}/{spec.repo}@{spec.branch}"
                    n = len(self.session.file_trees.get(key, []))
                    console.print(f"[green]Added[/green] {spec.owner}/{spec.repo} ({n} files, branch: {spec.branch})")
                except Exception as e:
                    console.print(f"[red]Error:[/red] {e}")

        elif name == "/clear":
            self.session.conversation.clear()
            self.session.fetched_files.clear()
            console.print("[green]Conversation and file cache cleared.[/green]")

        elif name == "/files":
            if not self.session.fetched_files:
                console.print("[dim]No files in context.[/dim]")
            else:
                by_repo: dict[str, list[str]] = {}
                for (owner, repo, branch, path) in self.session.fetched_files:
                    k = f"{owner}/{repo}@{branch}"
                    by_repo.setdefault(k, []).append(path)
                for repo_key, paths in by_repo.items():
                    owner_repo, _, branch = repo_key.partition("@")
                    console.print(f"[blue]{owner_repo}[/blue][dim]@{branch}[/dim]")
                    sorted_paths = sorted(paths)
                    for i, p in enumerate(sorted_paths):
                        connector = "\u2514\u2500" if i == len(sorted_paths) - 1 else "\u251c\u2500"
                        console.print(f"  [dim]{connector}[/dim] {p}")

        elif name == "/tree":
            target = arg.lower() if arg else None
            for spec in self.session.repos:
                short = spec.short_name()
                if target and target not in short.lower():
                    continue
                key = f"{spec.owner}/{spec.repo}@{spec.branch}"
                files = self.session.file_trees.get(key, [])
                console.print(f"\n[blue]{short}[/blue][dim]@{spec.branch}[/dim] [dim]({len(files)} files)[/dim]")
                for i, f in enumerate(files):
                    connector = "\u2514\u2500" if i == len(files) - 1 else "\u251c\u2500"
                    console.print(f"  [dim]{connector}[/dim] {f}")

        else:
            console.print(f"[yellow]Unknown command:[/yellow] {name}  (try /add, /clear, /files, /tree, /exit)")

        return True

    # ------------------------------------------------------------------
    # Startup
    # ------------------------------------------------------------------

    async def _startup(self) -> None:
        console.rule("[bold]repo-chat[/bold]", style="blue")
        console.print("[dim]  Ask questions about GitHub repositories without cloning them[/dim]")
        if self.cfg.use_direct_api:
            model_label = f"  model: {self.cfg.model}" if self.cfg.model else ""
            console.print(f"[dim]  backend: {self.cfg.api_url}{model_label}[/dim]")
        else:
            console.print("[dim]  backend: claude (CLI)[/dim]")
        console.print()

        errors: dict[str, str] = {}
        with console.status(f"[dim]Fetching {len(self.initial_urls)} repo(s)...[/dim]"):
            async def _load(url: str) -> tuple[str, str | None]:
                try:
                    await self._load_repo(url)
                    return url, None
                except Exception as e:
                    return url, str(e)

            results = await asyncio.gather(*[_load(u) for u in self.initial_urls])

        for url, err in results:
            if err:
                errors[url] = err

        if self.session.repos:
            table = Table(
                show_header=True,
                header_style="blue",
                box=box.ROUNDED,
                border_style="dim",
            )
            table.add_column("Repository")
            table.add_column("Branch")
            table.add_column("Files", justify="right")
            for spec in self.session.repos:
                key = f"{spec.owner}/{spec.repo}@{spec.branch}"
                n = len(self.session.file_trees.get(key, []))
                table.add_row(spec.short_name(), spec.branch or "?", str(n))
            console.print(table)

        for url, err in errors.items():
            console.print(f"[red]Error loading {url}:[/red] {err}")

        if not self.session.repos:
            console.print("[red]No repos loaded. Exiting.[/red]")
            sys.exit(1)

        console.print(
            "\n[dim]  /add <url>    add a repository\n"
            "  /clear        reset conversation and file cache\n"
            "  /files        list files in context\n"
            "  /tree <repo>  show repository file tree\n"
            "  /exit         quit[/dim]\n",
            highlight=False,
        )

    # ------------------------------------------------------------------
    # REPL
    # ------------------------------------------------------------------

    def _prompt(self) -> str:
        return ", ".join(r.short_name() for r in self.session.repos)

    async def run(self) -> None:
        await self._startup()

        # Enable readline history if available
        try:
            import readline  # noqa: F401
        except ImportError:
            pass

        try:
            while True:
                try:
                    console.print(f"[blue]{self._prompt()}[/blue] [bold]\u276f[/bold] ", end="")
                    user_input = input()
                except (EOFError, KeyboardInterrupt):
                    console.print("\n[dim]Goodbye![/dim]")
                    break

                user_input = user_input.strip()
                if not user_input:
                    continue

                if user_input.startswith("/"):
                    if not await self._handle_command(user_input):
                        break
                    continue

                try:
                    # Stage 1 — decide which files to fetch
                    with console.status("[dim]Deciding which files to read...[/dim]"):
                        file_specs = await self._select_files(user_input)

                    # Stage 2 — fetch new files
                    if file_specs:
                        new = [
                            s for s in file_specs
                            if (s["owner"], s["repo"], s["branch"], s["path"])
                            not in self.session.fetched_files
                        ]
                        if new:
                            repo_count = len({(s["owner"], s["repo"]) for s in new})
                            with console.status(
                                f"[dim]Fetching {len(new)} file(s) across {repo_count} repo(s)...[/dim]"
                            ):
                                await self._fetch_selected(file_specs)

                    # Stage 3 — stream the answer
                    await self._answer(user_input)
                    self._show_context_summary()

                except (KeyboardInterrupt, asyncio.CancelledError):
                    console.print("\n[dim]Cancelled.[/dim]\n")
                except RateLimitError as e:
                    console.print(f"\n[red]Rate limit:[/red] {e}")
                except Exception as e:
                    console.print(f"\n[red]Error:[/red] {e}")
        finally:
            await self.gh.aclose()
            if self._api:
                await self._api.aclose()


def main() -> None:
    if len(sys.argv) < 2 or sys.argv[1] in ("-h", "--help"):
        console.print(
            "Usage: [bold]repo-chat[/bold] <github-url> [<github-url> ...]\n\n"
            "Example:\n"
            "  repo-chat https://github.com/anthropics/anthropic-sdk-python\n"
            "  repo-chat https://github.com/owner/repo1 https://github.com/owner/repo2",
            highlight=False,
        )
        sys.exit(0 if "--help" in sys.argv or "-h" in sys.argv else 1)

    try:
        asyncio.run(RepoChatCLI(sys.argv[1:], load_config()).run())
    except KeyboardInterrupt:
        pass
