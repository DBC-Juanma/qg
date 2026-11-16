"""The twin world of a QA stack: browser access to the twins and the director as an MCP tool.

The agent runs in a jail with no shell and no checkout, so the director CLI runs here, outside
it, behind a loopback HTTP MCP endpoint guarded by a per-run bearer token. Fail-closed: no run
file, nothing added.
"""

import json
import re
import secrets
import tempfile
import threading
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from qg import proc, redact

TOOL = "director"
SERVER = "twins"
ALLOWED_TOOL = f"mcp__{SERVER}__{TOOL}"
DIRECTOR_TIMEOUT = 340
MAX_ARGS = 24
MAX_ARG_BYTES = 4000
COMMAND = re.compile(r"[a-z][a-z0-9-]*")
RUN_OPTIONS = ("--port", "--run-file")
HELP = (["-h"], ["--help"])
LOCAL_HOSTS = ("127.0.0.1", "localhost")

TOOL_SPEC = {
    "name": TOOL,
    "description": (
        "Runs the twin world's director CLI for this run (`uv run python -m twins.director <args>`): "  # noqa: E501
        "the other side of every provider (Telegram, Shopify, Stripe, Polar, the mailbox, a bank "
        "feed, an OIDC provider, calendars, Meta login...). Pass the subcommand and its "
        "arguments as "
        '`args`, e.g. ["outbox"], ["say", "telegram", "--as", "Ana", "hello"], '
        '["stripe", "complete-onboarding", "acct_qa_test"]; ["--help"] lists every subcommand and '
        '[<subcommand>, "--help"] its options. The port and token are added for you.'
    ),
    "inputSchema": {
        "type": "object",
        "properties": {"args": {"type": "array", "items": {"type": "string"}}},
        "required": ["args"],
    },
}


@dataclass(frozen=True)
class TwinRun:
    """The twins of one stack: their loopback origin and the backend port that names the run file."""  # noqa: E501

    origin: str
    backend_port: str

    @property
    def browser_origins(self) -> tuple[str, ...]:
        """The origins the QA browser may open for these twins: the loopback origin and every `*.localhost` host on its
        port (each twin shop, UCP merchant and brand site has its own loopback host there)."""
        return (self.origin, f"*.localhost:{urlsplit(self.origin).port}")


def run_file(tmpdir: str | None, backend_port: str) -> Path:
    """Returns the run file the stack's twins write: <tmp>/yedran-twins/<backend port>.json."""
    return Path(tmpdir or tempfile.gettempdir()) / "yedran-twins" / f"{backend_port}.json"


def discover(tmpdir: str | None, backend_port: str) -> TwinRun | None:
    """Returns the stack's twins, or None when there is no run file or its origin is not loopback."""  # noqa: E501
    file = run_file(tmpdir, backend_port)
    if not file.is_file():
        return None
    try:
        url = str(json.loads(file.read_text())["url"])
    except (ValueError, KeyError, TypeError):
        return None
    parts = urlsplit(url)
    if parts.scheme != "http" or parts.hostname not in LOCAL_HOSTS or not parts.port:
        return None
    return TwinRun(f"{parts.scheme}://{parts.netloc}", backend_port)


def check_args(args: Any) -> list[str]:
    """Returns `args` when it is a short list of strings that is the director's help or starts with a plain subcommand name and never names the director's own run options (even abbreviated), else raises."""  # noqa: E501
    if not isinstance(args, list) or not args or len(args) > MAX_ARGS:
        raise ValueError(f"args must be a list of 1 to {MAX_ARGS} strings")
    if not all(isinstance(a, str) and len(a) <= MAX_ARG_BYTES for a in args):
        raise ValueError("every argument must be a short string")
    if args not in HELP and not COMMAND.fullmatch(args[0]):
        raise ValueError(f"{args[0]!r} is not a director command; run [\"--help\"] to list them")
    for a in args[1:]:
        name = a.split("=", 1)[0]
        if len(name) > 2 and name.startswith("--") and any(o.startswith(name) for o in RUN_OPTIONS):
            raise ValueError(f"option {a!r} is not allowed: the run is fixed for you")
    return args


def director_argv(backend_port: str, args: list[str]) -> list[str]:
    """Returns the exact director command line for this run."""
    return ["uv", "run", "python", "-m", "twins.director", "--port", backend_port, *args]


class Endpoint:
    """A loopback HTTP MCP server (stateless JSON-RPC over POST) exposing the director tool."""

    def __init__(
        self, twin: TwinRun, cwd: Path, env: dict[str, str], hidden: tuple[str, ...] = ()
    ) -> None:
        self.twin = twin
        self._hidden = hidden
        self.token = secrets.token_hex(24)
        self._cwd = cwd
        self._env = env
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_: Any) -> None:
                return

            def do_POST(self) -> None:
                if self.headers.get("Authorization") != f"Bearer {outer.token}":
                    self._send(401, {"error": "unauthorized"})
                    return
                size = int(self.headers.get("Content-Length") or 0)
                if size > 1 << 20:
                    self._send(413, {"error": "too large"})
                    return
                try:
                    message = json.loads(self.rfile.read(size))
                except ValueError:
                    self._send(400, {"error": "bad json"})
                    return
                reply = outer.handle(message)
                if reply is None:
                    self.send_response(202)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                else:
                    self._send(200, reply)

            def do_GET(self) -> None:
                self._send(405, {"error": "post only"})

            def _send(self, code: int, body: dict[str, Any]) -> None:
                data = json.dumps(body).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._server.daemon_threads = True
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    @property
    def url(self) -> str:
        """Returns the endpoint's URL."""
        return f"http://127.0.0.1:{self._server.server_address[1]}/mcp"

    def start(self) -> None:
        """Starts serving."""
        self._thread.start()

    def stop(self) -> None:
        """Stops serving."""
        self._server.shutdown()
        self._server.server_close()

    def handle(self, message: dict[str, Any]) -> dict[str, Any] | None:
        """Answers one JSON-RPC message; notifications (no id) get no answer."""
        if "id" not in message:
            return None
        method = message.get("method")
        ident = message["id"]
        if method == "initialize":
            wanted = (message.get("params") or {}).get("protocolVersion") or "2025-03-26"
            result: dict[str, Any] = {
                "protocolVersion": wanted,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": SERVER, "version": "1"},
            }
        elif method == "tools/list":
            result = {"tools": [TOOL_SPEC]}
        elif method == "tools/call":
            result = self._call(message.get("params") or {})
        elif method == "ping":
            result = {}
        else:
            return {
                "jsonrpc": "2.0",
                "id": ident,
                "error": {"code": -32601, "message": f"unknown method {method}"},
            }
        return {"jsonrpc": "2.0", "id": ident, "result": result}

    def _call(self, params: dict[str, Any]) -> dict[str, Any]:
        """Runs the director for a tools/call and returns its output as the tool result."""
        if params.get("name") != TOOL:
            return _text("unknown tool", True)
        try:
            args = check_args((params.get("arguments") or {}).get("args"))
        except ValueError as exc:
            return _text(str(exc), True)
        done = proc.run(
            director_argv(self.twin.backend_port, args),
            cwd=self._cwd,
            env=self._env,
            timeout=DIRECTOR_TIMEOUT,
        )
        out = (done.stdout or "") + (done.stderr or "")
        return _text(redact.clean(out.strip(), self._hidden) or "(no output)", done.returncode != 0)


def _text(text: str, error: bool) -> dict[str, Any]:
    """Returns an MCP tool result carrying `text`."""
    return {"content": [{"type": "text", "text": text}], "isError": error}


def mcp_entry(endpoint: Endpoint) -> dict[str, Any]:
    """Returns the MCP config entry that points the agent at the endpoint with the run's token."""
    return {
        "type": "http",
        "url": endpoint.url,
        "headers": {"Authorization": f"Bearer {endpoint.token}"},
    }


CHARTER_SECTION = """
## Twin world

The providers of this stack are local twins (Telegram, Shopify, Stripe with Connect, a mail
server). Their hosted pages are reachable in the browser at {origin} (the only extra origin). You
have one more tool, `{tool}`: it runs the twins' director for this run, with `args` as the
director's arguments (the port and token are added). Examples: `["outbox"]`,
`["wait", "telegram", "--to", "Ana", "--timeout", "90"]`,
`["say", "telegram", "--as", "Ana", "I want two Starter packs"]`, `["pay", "shopify", "last"]`,
`["stripe", "complete-onboarding", "acct_qa_test"]`, `["pay", "stripe", "last"]`,
`["email", "mailbox", "someone@custdomain.com"]`. The director offers more than these: `labs`,
`polar`, `bank`, `oidc`, `calendar`, `consent`, `meta-login`, `meta-whatsapp-signup` and
`telegram-personal` (the people around a personal Telegram account) among them. `["--help"]`
lists every subcommand of the product's director and `["<subcommand>", "--help"]` its options.
Never assume a provider cannot be driven before you have read that list. The recipes are in the
project notes below. A flow that needs a provider's other side is never `blocked`: drive it with
`{tool}`.
"""


def charter_section(twin: TwinRun) -> str:
    """Returns the charter text that tells the agent about the twins and the director tool."""
    return CHARTER_SECTION.format(origin=twin.origin, tool=ALLOWED_TOOL)
