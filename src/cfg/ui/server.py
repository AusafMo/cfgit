# Copyright 2026 Mohammad Ausaf. Licensed under the Apache License, Version 2.0.
"""Small localhost UI server for cfgit."""
from __future__ import annotations

from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import socket
from typing import Any
from urllib.parse import parse_qs, urlparse
import webbrowser

from cfg.interfaces import actions
from cfg.interfaces.actions import ActionContext


DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765


class CfgUIServer(ThreadingHTTPServer):
    def __init__(
        self,
        server_address: tuple[str, int],
        *,
        config_file: str | None,
        env: str,
        author: str | None,
    ):
        super().__init__(server_address, CfgUIHandler)
        self.config_file = config_file
        self.env = env
        self.author = author

    def server_bind(self) -> None:
        self.socket.bind(self.server_address)
        self.server_address = self.socket.getsockname()
        self.server_name = str(self.server_address[0])
        self.server_port = int(self.server_address[1])


class CfgUIHandler(BaseHTTPRequestHandler):
    server: CfgUIServer

    def log_message(self, fmt: str, *args: Any) -> None:
        return

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path == "/":
            self._send_text(UI_HTML, content_type="text/html; charset=utf-8")
            return
        if parsed.path == "/api/health":
            self._send_json({"ok": True})
            return
        if parsed.path == "/api/schema":
            params = parse_qs(parsed.query)
            self._send_json(self._schema(params))
            return
        if parsed.path == "/api/state":
            params = parse_qs(parsed.query)
            self._send_json(self._state(params))
            return
        self.send_error(HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path != "/api/action":
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        try:
            payload = self._read_json()
            name = str(payload.get("action") or "")
            engine = actions.make_engine(self._ctx(payload))
            result = actions.envelope(_run_action, name, engine, payload)
            self._send_json(result)
        except Exception as exc:
            self._send_json(
                {"status": "error", "code": actions.EXIT_STORAGE, "message": str(exc), "data": None},
                status=HTTPStatus.INTERNAL_SERVER_ERROR,
            )

    def _ctx(self, payload: dict[str, Any] | None = None, params: dict[str, list[str]] | None = None) -> ActionContext:
        payload = payload or {}
        params = params or {}
        return ActionContext(
            config_file=_first(params, "config_file") or payload.get("config_file") or self.server.config_file,
            env=_first(params, "env") or str(payload.get("env") or self.server.env),
            author=_first(params, "author") or payload.get("author") or self.server.author,
        )

    def _schema(self, params: dict[str, list[str]]) -> dict[str, Any]:
        try:
            project = actions.load_config(_first(params, "config_file") or self.server.config_file)
            return {
                "status": "ok",
                "data": {
                    "project": project.name,
                    "config_file": str(project.path),
                    "envs": sorted(project.envs),
                    "collections": [
                        {
                            "name": item.name,
                            "id_field": item.id_field,
                            "live_when": item.live_when,
                        }
                        for item in project.collections
                    ],
                    "connections": actions.to_json(project.connections),
                },
            }
        except Exception as exc:
            return {"status": "error", "message": str(exc), "data": None}

    def _state(self, params: dict[str, list[str]]) -> dict[str, Any]:
        ctx = self._ctx(params=params)
        engine = actions.make_engine(ctx)
        who, _ = actions.whoami(engine)
        rows, code = actions.status(engine)
        return {
            "status": "dirty" if code == actions.EXIT_DIRTY else "ok",
            "code": code,
            "message": "",
            "data": {
                "whoami": actions.to_json(who),
                "status": actions.to_json(rows),
            },
        }

    def _read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        if length > 5_000_000:
            raise ValueError("request body is too large")
        raw = self.rfile.read(length).decode("utf-8") if length else "{}"
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError("request body must be a JSON object")
        return data

    def _send_json(self, value: Any, *, status: HTTPStatus = HTTPStatus.OK) -> None:
        body = json.dumps(actions.to_json(value), indent=2, sort_keys=True).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_text(self, value: str, *, content_type: str) -> None:
        body = value.encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)


def run_ui(
    *,
    config_file: str | None = None,
    env: str = "dev",
    author: str | None = None,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    open_browser: bool = True,
) -> int:
    server = _bind_server(host=host, port=port, config_file=config_file, env=env, author=author)
    actual_host, actual_port = server.server_address
    url = f"http://{actual_host}:{actual_port}/"
    print(f"cfg ui listening on {url}", flush=True)
    if open_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\ncfg ui stopped")
    finally:
        server.server_close()
    return 0


def _bind_server(
    *,
    host: str,
    port: int,
    config_file: str | None,
    env: str,
    author: str | None,
) -> CfgUIServer:
    last_error: OSError | None = None
    for candidate in range(port, port + 50):
        try:
            return CfgUIServer(
                (host, candidate),
                config_file=config_file,
                env=env,
                author=author,
            )
        except OSError as exc:
            last_error = exc
            if exc.errno not in {48, 98, 10048}:
                raise
    raise OSError(f"could not bind {host}:{port}-{port + 49}") from last_error


def _run_action(name: str, engine: Any, payload: dict[str, Any]) -> tuple[Any, int]:
    return actions.run_named_action(name, engine, payload)


def _first(params: dict[str, list[str]], key: str) -> str | None:
    values = params.get(key) or []
    if not values:
        return None
    return values[0] or None


def find_free_port(host: str = DEFAULT_HOST) -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind((host, 0))
        return int(sock.getsockname()[1])


UI_HTML = r"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>cfgit</title>
  <style>
    :root {
      --paper: #f8f9fc;
      --ink: #0c243c;
      --muted: #66708c;
      --line: #cbd2df;
      --hard: #111827;
      --blue: #316aff;
      --orange: #ff8110;
      --red: #ff401c;
      --green: #137a4d;
      --panel: #ffffff;
      --shadow: 4px 4px 0 #0c243c;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      background: var(--paper);
      color: var(--ink);
      font: 14px/1.45 ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      letter-spacing: 0;
    }
    button, input, textarea, select { font: inherit; }
    .shell {
      min-height: 100vh;
      display: grid;
      grid-template-columns: 260px minmax(0, 1fr);
    }
    aside {
      border-right: 2px solid var(--hard);
      background: #fff;
      padding: 18px;
      position: sticky;
      top: 0;
      height: 100vh;
      overflow: auto;
    }
    main {
      min-width: 0;
      padding: 18px;
      display: grid;
      grid-template-columns: minmax(360px, 520px) minmax(0, 1fr);
      gap: 16px;
      align-content: start;
    }
    .brand {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
      margin-bottom: 18px;
    }
    .mark {
      width: 32px;
      height: 32px;
      border: 2px solid var(--hard);
      background: linear-gradient(135deg, var(--blue) 0 58%, var(--orange) 58% 100%);
      box-shadow: 3px 3px 0 var(--hard);
    }
    h1 {
      margin: 0;
      font-size: 20px;
      line-height: 1;
      letter-spacing: 0;
    }
    .meta {
      color: var(--muted);
      font-size: 12px;
      word-break: break-word;
    }
    .nav {
      display: grid;
      gap: 8px;
    }
    .nav button, .primary, .secondary {
      min-height: 36px;
      border: 2px solid var(--hard);
      color: var(--ink);
      background: #fff;
      cursor: pointer;
      text-align: left;
      padding: 8px 10px;
      border-radius: 4px;
    }
    .nav button.active {
      background: var(--blue);
      color: #fff;
      box-shadow: 3px 3px 0 var(--hard);
    }
    .primary {
      background: var(--blue);
      color: #fff;
      text-align: center;
      box-shadow: 3px 3px 0 var(--hard);
    }
    .secondary {
      text-align: center;
      background: #fff;
    }
    .panel {
      background: var(--panel);
      border: 2px solid var(--hard);
      border-radius: 6px;
      box-shadow: var(--shadow);
      padding: 14px;
      min-width: 0;
    }
    .panel h2 {
      margin: 0 0 12px;
      font-size: 15px;
      line-height: 1.2;
      letter-spacing: 0;
    }
    .grid {
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 10px;
    }
    label {
      display: grid;
      gap: 5px;
      min-width: 0;
      color: var(--muted);
      font-size: 12px;
      font-weight: 700;
      text-transform: uppercase;
    }
    input, textarea, select {
      width: 100%;
      min-width: 0;
      border: 1px solid var(--line);
      background: #fbfcff;
      color: var(--ink);
      border-radius: 4px;
      padding: 9px 10px;
      outline: none;
    }
    input:focus, textarea:focus, select:focus {
      border-color: var(--blue);
      box-shadow: 0 0 0 2px rgba(49, 106, 255, 0.15);
    }
    textarea {
      min-height: 210px;
      resize: vertical;
      font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
      font-size: 12px;
    }
    .wide { grid-column: 1 / -1; }
    .checks {
      display: flex;
      gap: 14px;
      align-items: center;
      flex-wrap: wrap;
      color: var(--ink);
      font-size: 13px;
      margin: 4px 0;
    }
    .checks label {
      display: flex;
      align-items: center;
      gap: 7px;
      text-transform: none;
      font-size: 13px;
      color: var(--ink);
    }
    .checks input {
      width: 16px;
      height: 16px;
    }
    .actions {
      display: flex;
      gap: 10px;
      align-items: center;
      flex-wrap: wrap;
      margin-top: 12px;
    }
    .stack {
      display: grid;
      gap: 16px;
      min-width: 0;
    }
    table {
      width: 100%;
      border-collapse: collapse;
      font-size: 13px;
    }
    th, td {
      border-bottom: 1px solid var(--line);
      padding: 8px 6px;
      text-align: left;
      vertical-align: top;
    }
    th {
      color: var(--muted);
      font-size: 11px;
      text-transform: uppercase;
    }
    .pill {
      display: inline-flex;
      align-items: center;
      min-height: 22px;
      border: 1px solid var(--hard);
      border-radius: 999px;
      padding: 2px 8px;
      font-size: 12px;
      background: #fff;
      white-space: nowrap;
    }
    .clean { color: var(--green); }
    .dirty { color: var(--red); }
    pre {
      margin: 0;
      white-space: pre-wrap;
      word-break: break-word;
      font: 12px/1.5 ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
      color: #101828;
    }
    .output {
      max-height: 58vh;
      overflow: auto;
      background: #fbfcff;
      border: 1px solid var(--line);
      border-radius: 4px;
      padding: 10px;
    }
    .split {
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 10px;
    }
    .danger { color: var(--red); }
    .blue { color: var(--blue); }
    .hidden { display: none; }
    @media (max-width: 980px) {
      .shell { grid-template-columns: 1fr; }
      aside { position: relative; height: auto; border-right: 0; border-bottom: 2px solid var(--hard); }
      main { grid-template-columns: 1fr; }
      .grid, .split { grid-template-columns: 1fr; }
      .nav { grid-template-columns: repeat(2, minmax(0, 1fr)); }
    }
  </style>
</head>
<body>
  <div class="shell">
    <aside>
      <div class="brand">
        <div>
          <h1>cfgit</h1>
          <div class="meta" id="identity">connecting</div>
        </div>
        <div class="mark" aria-hidden="true"></div>
      </div>
      <div class="grid">
        <label>Env<input id="env" value="dev" autocomplete="off"></label>
        <label>Author hint<input id="author" autocomplete="off"></label>
        <label class="wide">Config file<input id="configFile" autocomplete="off"></label>
      </div>
      <div class="actions">
        <button class="secondary" id="refresh" type="button">Refresh</button>
      </div>
      <hr>
      <nav class="nav" id="nav"></nav>
    </aside>
    <main>
      <section class="panel">
        <h2 id="operationTitle">Status</h2>
        <form id="operationForm" class="grid"></form>
      </section>
      <div class="stack">
        <section class="panel">
          <h2>Live Records</h2>
          <div class="output" id="statusBox"><pre>loading</pre></div>
        </section>
        <section class="panel">
          <h2>Output</h2>
          <div class="output"><pre id="output">{}</pre></div>
        </section>
      </div>
    </main>
  </div>
  <script>
    const ops = [
      ["status", "Status", [["record", "Record", "text", "agent_configs:agent_planner"]]],
      ["init", "Init", []],
      ["whoami", "Whoami", []],
      ["import", "Import", [["record", "Record", "text", ""], ["all_records", "All records", "check"], ["allow_secret", "Allow secret", "check"], ["message", "Message", "text", "initial import"]]],
      ["diff", "Diff", [["record", "Record", "text", "agent_configs:agent_planner"], ["a", "From", "text", "=HEAD"], ["b", "To", "text", "=live"]]],
      ["impact", "Impact", [["record", "Record", "text", "agent_configs:agent_planner"], ["a", "From", "text", "=HEAD"], ["b", "To", "text", "=live"], ["use_llm", "LLM", "check"], ["provider", "Provider", "text", ""], ["model", "Model", "text", ""]]],
      ["commit", "Commit", [["record", "Record", "text", "agent_configs:agent_planner"], ["allow_secret", "Allow secret", "check"], ["message", "Message", "text", "ui commit"], ["doc", "Document JSON", "json", "{\n  \"config_id\": \"agent_planner\"\n}"]]],
      ["log", "Log", [["record", "Record", "text", "agent_configs:agent_planner"], ["limit", "Limit", "number", "20"]]],
      ["show", "Show", [["record", "Record", "text", "agent_configs:agent_planner"], ["ref", "Ref", "text", "HEAD"]]],
      ["adopt", "Adopt", [["record", "Record", "text", ""], ["all_records", "All drift", "check"], ["allow_secret", "Allow secret", "check"], ["message", "Message", "text", "ui adopt"]]],
      ["restore", "Restore", [["record", "Record", "text", ""], ["ref", "Ref", "text", ""], ["as_of", "As of", "text", ""], ["tag", "Tag", "text", ""], ["dry_run", "Dry run", "check"], ["message", "Message", "text", "ui restore"]]],
      ["tag", "Tag", [["name", "Name", "text", "stable"]]],
      ["fsck", "Fsck", []]
    ];
    const state = { op: "status" };
    const nav = document.getElementById("nav");
    const form = document.getElementById("operationForm");
    const title = document.getElementById("operationTitle");
    const output = document.getElementById("output");
    const statusBox = document.getElementById("statusBox");

    function envPayload() {
      return {
        env: document.getElementById("env").value || "dev",
        author: document.getElementById("author").value || null,
        config_file: document.getElementById("configFile").value || null
      };
    }
    function selectOp(name) {
      state.op = name;
      renderNav();
      renderForm();
    }
    function renderNav() {
      nav.innerHTML = "";
      for (const [name, label] of ops) {
        const button = document.createElement("button");
        button.type = "button";
        button.textContent = label;
        button.className = name === state.op ? "active" : "";
        button.onclick = () => selectOp(name);
        nav.appendChild(button);
      }
    }
    function renderForm() {
      const op = ops.find(item => item[0] === state.op);
      title.textContent = op[1];
      form.innerHTML = "";
      for (const field of op[2]) {
        const [key, label, type, placeholder] = field;
        if (type === "check") {
          const wrap = document.createElement("div");
          wrap.className = "checks wide";
          const item = document.createElement("label");
          const input = document.createElement("input");
          input.type = "checkbox";
          input.name = key;
          item.append(input, document.createTextNode(label));
          wrap.append(item);
          form.appendChild(wrap);
          continue;
        }
        const labelEl = document.createElement("label");
        labelEl.textContent = label;
        if (type === "json") labelEl.className = "wide";
        const input = type === "json" ? document.createElement("textarea") : document.createElement("input");
        input.name = key;
        input.placeholder = placeholder || "";
        if (type !== "json") input.type = type;
        input.value = placeholder || "";
        labelEl.appendChild(input);
        form.appendChild(labelEl);
      }
      const actions = document.createElement("div");
      actions.className = "actions wide";
      const run = document.createElement("button");
      run.className = "primary";
      run.type = "submit";
      run.textContent = "Run";
      actions.appendChild(run);
      form.appendChild(actions);
    }
    async function callAction(action, data) {
      const res = await fetch("/api/action", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ action, ...envPayload(), ...data })
      });
      return await res.json();
    }
    async function refreshState() {
      const params = new URLSearchParams();
      const env = envPayload();
      if (env.env) params.set("env", env.env);
      if (env.author) params.set("author", env.author);
      if (env.config_file) params.set("config_file", env.config_file);
      const schema = await fetch("/api/schema?" + params.toString()).then(r => r.json());
      const stateRes = await fetch("/api/state?" + params.toString()).then(r => r.json());
      if (stateRes.data && stateRes.data.whoami) {
        const who = stateRes.data.whoami;
        const ident = who.identity || {};
        document.getElementById("identity").textContent =
          `${who.env} / ${who.identity_display || who.author} / ${who.identity_mode || ident.mode || "open"}`;
      }
      if (schema.data && !document.getElementById("configFile").value) {
        document.getElementById("configFile").placeholder = schema.data.config_file;
      }
      renderStatus(stateRes);
    }
    function renderStatus(data) {
      if (!data.data || !data.data.status) {
        statusBox.innerHTML = `<pre>${escapeHtml(JSON.stringify(data, null, 2))}</pre>`;
        return;
      }
      const rows = data.data.status;
      let html = "<table><thead><tr><th>Record</th><th>State</th><th>Head</th></tr></thead><tbody>";
      for (const row of rows) {
        const dirty = row.state === "changed_outside_cfgit" || row.state === "new";
        html += `<tr><td>${escapeHtml(row.collection)}:${escapeHtml(row.record_id)}</td><td><span class="pill ${dirty ? "dirty" : "clean"}">${escapeHtml(row.state)}</span></td><td>${row.head_seq ?? ""}</td></tr>`;
      }
      html += "</tbody></table>";
      statusBox.innerHTML = html;
    }
    function escapeHtml(value) {
      return String(value).replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", "\"": "&quot;", "'": "&#39;" }[c]));
    }
    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      const data = {};
      for (const field of new FormData(form).entries()) data[field[0]] = field[1];
      for (const input of form.querySelectorAll("input[type='checkbox']")) data[input.name] = input.checked;
      output.textContent = "running";
      const result = await callAction(state.op, data);
      output.textContent = result.data && result.data.text ? result.data.text : JSON.stringify(result, null, 2);
      await refreshState();
    });
    document.getElementById("refresh").onclick = refreshState;
    renderNav();
    renderForm();
    refreshState().catch(err => { output.textContent = String(err); });
  </script>
</body>
</html>
"""
