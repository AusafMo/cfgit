# Copyright 2026 Mohammad Ausaf. Licensed under the Apache License, Version 2.0.
"""Small localhost UI server for cfgit."""
from __future__ import annotations

from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
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
            result = actions.envelope(_run_action, name, engine, payload, record=payload.get("record"))
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
                    "impact": {
                        "provider": os.getenv("CFGIT_UI_IMPACT_PROVIDER")
                        or project.connections.ai_provider,
                        "model": os.getenv("CFGIT_UI_IMPACT_MODEL") or None,
                    },
                },
            }
        except Exception as exc:
            return {"status": "error", "message": str(exc), "data": None}

    def _state(self, params: dict[str, list[str]]) -> dict[str, Any]:
        ctx = self._ctx(params=params)
        engine = actions.make_engine(ctx)
        who, _ = actions.whoami(engine)
        rows, code = actions.status(engine)
        branches: list[dict[str, Any]] = []
        prs: list[dict[str, Any]] = []
        try:
            branches, _ = actions.branch_list(engine)
            prs, _ = actions.pr_list(engine, status="open")
        except Exception:
            branches = []
            prs = []
        recent, _ = actions.recent_history(engine, limit=50)
        return {
            "status": "dirty" if code == actions.EXIT_DIRTY else "ok",
            "code": code,
            "message": "",
            "data": {
                "whoami": actions.to_json(who),
                "status": actions.to_json(rows),
                "recent_history": actions.to_json(recent),
                "branches": actions.to_json(branches),
                "prs": actions.to_json(prs),
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
    allow_port_fallback: bool = True,
) -> int:
    server = _bind_server(
        host=host,
        port=port,
        config_file=config_file,
        env=env,
        author=author,
        allow_port_fallback=allow_port_fallback,
    )
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
    allow_port_fallback: bool = True,
) -> CfgUIServer:
    last_error: OSError | None = None
    candidates = range(port, port + 50) if allow_port_fallback else range(port, port + 1)
    for candidate in candidates:
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
            if not allow_port_fallback:
                raise OSError(f"could not bind {host}:{port}; port is already in use") from exc
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
<html lang="en" data-theme="dark">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>cfgit</title>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@500;600;700&family=Inter:wght@400;500;600&family=JetBrains+Mono:wght@400;500;600&display=swap" rel="stylesheet">
  <style>
    /* ============ cfgit · git workflow, cfgit skin ============ */
    :root{
      --disp:"Space Grotesk",ui-sans-serif,system-ui,sans-serif;
      --body:"Inter",ui-sans-serif,system-ui,-apple-system,sans-serif;
      --mono:"JetBrains Mono",ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;
    }
    /* DARK: deep slate, never pure black; calm on the eyes */
    [data-theme="dark"]{
      --bg:#10151c; --chrome:#141b24; --panel:#18212c; --panel2:#1e2935; --raise:#24303d;
      --edge:#27323f; --edge2:#33414f;
      --ink:#e8edf2; --dim:#9aa6b2; --faint:#67727e; --select-ink:#f7f9fb;
      --blue:#5b8dff; --blue2:#3d6fe0;
      --amber:#e0a445; --amber-bg:rgba(224,164,69,.14);
      --moss:#5bb37a; --moss-bg:rgba(91,179,122,.13);
      --sky:#5bb1ff; --sky-bg:rgba(91,177,255,.12);
      /* paper diff surface (the signature) stays warm even in dark */
      --paper:#f3efe6; --paper-edge:#ddd6c6; --paper-ink:#2b2a25; --paper-dim:#7a7567;
      --paper-del:#fbe7e3; --paper-del-ink:#9a3a2c; --paper-add:#e6f0e3; --paper-add-ink:#2f6a3d;
      --paper-gutter:#ece6da;
      --shadow:0 18px 40px rgba(0,0,0,.45);
    }
    /* LIGHT: warm off-white, soft ink; not glare-white */
    [data-theme="light"]{
      --bg:#eceae3; --chrome:#f3f1ea; --panel:#f7f5ef; --panel2:#efece3; --raise:#e7e3d8;
      --edge:#dcd7ca; --edge2:#cfc9b8;
      --ink:#24272b; --dim:#5f6670; --faint:#8b9099; --select-ink:#181b20;
      --blue:#2f6af0; --blue2:#1f56d8;
      --amber:#b5781f; --amber-bg:rgba(181,120,31,.14);
      --moss:#3f8a59; --moss-bg:rgba(63,138,89,.12);
      --sky:#2575c8; --sky-bg:rgba(37,117,200,.10);
      --paper:#fbfaf5; --paper-edge:#e4dfd2; --paper-ink:#2b2a25; --paper-dim:#857f70;
      --paper-del:#fbe7e3; --paper-del-ink:#9a3a2c; --paper-add:#e6f0e3; --paper-add-ink:#2f6a3d;
      --paper-gutter:#f1ede2;
      --shadow:0 16px 36px rgba(60,56,44,.14);
    }
    *{box-sizing:border-box}
    html,body{height:100%}
    body{margin:0;background:var(--bg);color:var(--ink);font-family:var(--body);font-size:13.5px;line-height:1.5;
      -webkit-font-smoothing:antialiased;text-rendering:optimizeLegibility}
    button,input,select{font:inherit;color:inherit}
    ::selection{background:var(--blue);color:#fff}
    .mono{font-family:var(--mono)}
    *::-webkit-scrollbar{width:10px;height:10px}
    *::-webkit-scrollbar-thumb{background:var(--edge2);border-radius:6px;border:2px solid transparent;background-clip:content-box}
    *::-webkit-scrollbar-thumb:hover{background:var(--faint);background-clip:content-box}

    .app{display:grid;grid-template-rows:auto minmax(0,1fr);height:100vh;min-height:0;max-width:100vw;overflow:hidden}

    /* ---- repository header ---- */
    .top{display:flex;flex-direction:column;min-width:0;max-width:100vw;background:var(--chrome);border-bottom:1px solid var(--edge)}
    .repo-main{display:grid;grid-template-columns:minmax(300px,380px) minmax(0,1fr);align-items:flex-start;gap:16px;min-width:0;padding:12px 18px 9px}
    .repo-left{display:flex;flex-direction:column;gap:8px;min-width:0;flex:1}
    .repo-title{display:flex;align-items:center;gap:8px;min-width:0;flex-wrap:nowrap}
    .repo-ic{width:16px;height:16px;color:var(--blue);flex:0 0 auto}
    .brand{display:flex;align-items:baseline;gap:3px;min-width:0;max-width:520px;overflow:hidden;color:var(--ink);font-family:var(--disp);font-weight:700;font-size:20px;line-height:1.2;white-space:nowrap}
    .brand .dot{color:var(--blue);font-weight:600}
    #projectName{display:block;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
    .repo-scope{font-family:var(--mono);font-size:10.5px;letter-spacing:.06em;text-transform:uppercase;color:var(--dim);border:1px solid var(--edge2);border-radius:999px;padding:1px 8px;line-height:18px}
    .repo-meta{display:flex;align-items:center;gap:10px;min-width:0;flex-wrap:wrap}
    .who{display:flex;align-items:center;gap:9px;font-size:12.5px;color:var(--dim);min-width:0}
    .who .ava{width:22px;height:22px;border-radius:6px;display:grid;place-items:center;font-family:var(--mono);
      font-size:10px;font-weight:600;color:#fff;background:linear-gradient(135deg,var(--blue),var(--blue2))}
    .who b{color:var(--ink);font-weight:600}
    #whoTxt{display:block;max-width:360px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
    .chip{font-family:var(--mono);font-size:10.5px;letter-spacing:.06em;text-transform:uppercase;
      padding:2px 9px;border-radius:999px;border:1px solid var(--edge2);color:var(--dim);line-height:1.4}
    .chip.open{color:var(--moss);border-color:var(--moss-bg);background:var(--moss-bg)}
    .repo-stats{display:flex;align-items:center;gap:8px;min-width:0;color:var(--dim);font-size:12px}
    .repo-stats span{white-space:nowrap}
    .repo-stats b{color:var(--ink);font-weight:600}
    .repo-actions{display:flex;align-items:flex-start;justify-content:flex-end;gap:8px;min-width:0;max-width:100%;flex-wrap:wrap}
    .scopebar,.cmdset{display:flex;align-items:center;gap:7px;min-width:0}
    .scopebar{padding-right:3px}
    .cmdset{padding-left:4px}
    .pick{display:flex;align-items:center;gap:6px;min-width:0}
    .pick span{font-family:var(--mono);font-size:10px;letter-spacing:.07em;text-transform:uppercase;color:var(--faint);white-space:nowrap}
    .selectbox{position:relative;display:inline-flex;align-items:center;min-width:0;z-index:1}
    .selectbox.open{z-index:80}
    .seg{display:flex;background:var(--panel);border:1px solid var(--edge2);border-radius:8px;padding:2px;gap:2px;min-width:0}
    .seg button{border:0;background:transparent;color:var(--dim);padding:4px 9px;border-radius:6px;font-size:12px;cursor:pointer;line-height:1}
    .seg button.on{background:var(--raise);color:var(--ink)}
    .envpick{appearance:none;-webkit-appearance:none;background:linear-gradient(180deg,var(--panel2),var(--panel));border:1px solid var(--edge2);border-radius:8px;color:var(--ink);min-width:0;
      padding:6px 30px 6px 28px;font-size:12.5px;font-family:var(--mono);box-shadow:inset 0 1px 0 rgba(255,255,255,.03)}
    .envpick:hover{border-color:var(--blue);background:var(--panel2)}
    .selectbox .envpick{position:absolute;inset:0;width:100%;height:100%;opacity:0;pointer-events:none}
    .branchpick{width:188px;max-width:188px}
    .envbox{min-width:92px}
    .branchbox{width:188px;max-width:188px}
    .select-trigger{display:grid;grid-template-columns:auto minmax(0,1fr) auto;align-items:center;gap:9px;width:100%;min-height:32px;
      background:linear-gradient(180deg,var(--panel2),var(--panel));border:1px solid var(--edge2);border-radius:8px;color:var(--select-ink);
      padding:6px 10px;font-family:var(--mono);font-size:12.5px;line-height:1.2;text-align:left;cursor:pointer;
      box-shadow:inset 0 1px 0 rgba(255,255,255,.03)}
    .select-trigger:hover,.selectbox.open .select-trigger{border-color:var(--blue);background:var(--panel2)}
    .select-trigger:disabled{opacity:.5;cursor:default}
    .selectbox.branchbox .select-trigger:hover,.selectbox.branchbox.open .select-trigger{border-color:var(--moss)}
    .select-mark{width:7px;height:7px;border-radius:2px;background:var(--blue);box-shadow:0 0 0 3px var(--sky-bg);flex:0 0 auto}
    .branchbox .select-mark{background:var(--moss);box-shadow:0 0 0 3px var(--moss-bg)}
    .select-trigger .select-value{display:block;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;min-width:0;color:var(--select-ink);font-weight:600}
    .select-caret{width:7px;height:7px;border-right:1.5px solid var(--dim);border-bottom:1.5px solid var(--dim);transform:rotate(45deg) translateY(-2px);justify-self:end}
    .selectbox.open .select-caret{transform:rotate(225deg) translate(-1px,-1px);border-color:var(--ink)}
    .select-menu{position:absolute;left:0;top:calc(100% + 6px);z-index:100;min-width:100%;max-width:min(420px,calc(100vw - 24px));max-height:260px;overflow:auto;
      display:none;padding:6px;background:var(--panel2);border:1px solid var(--edge2);border-radius:12px;box-shadow:var(--shadow)}
    .selectbox.open .select-menu{display:flex;flex-direction:column;gap:3px}
    .select-option{display:grid;grid-template-columns:18px minmax(0,1fr);align-items:center;gap:7px;width:100%;border:1px solid transparent;border-radius:8px;
      background:transparent;color:var(--select-ink);padding:8px 9px;font-family:var(--mono);font-size:12.5px;font-weight:600;line-height:1.25;text-align:left;cursor:pointer}
    .select-option:hover,.select-option:focus-visible{outline:none;color:var(--select-ink);background:var(--panel);border-color:var(--edge2)}
    .select-option[aria-selected="true"]{color:var(--select-ink);background:var(--sky-bg);border-color:var(--blue);font-weight:700}
    .branchbox .select-option[aria-selected="true"]{background:var(--moss-bg);border-color:var(--moss)}
    .select-check{color:transparent;font-weight:700}
    .select-option[aria-selected="true"] .select-check{color:var(--blue)}
    .branchbox .select-option[aria-selected="true"] .select-check{color:var(--moss)}
    .select-option .txt{overflow:hidden;text-overflow:ellipsis;white-space:nowrap;color:var(--select-ink)}
    .branch-state{font-family:var(--mono);font-size:10.5px;color:var(--dim);line-height:32px;white-space:nowrap}
    .branch-state.open{color:var(--moss)}
    .ghost{background:transparent;border:1px solid var(--edge2);border-radius:8px;color:var(--dim);min-width:0;
      padding:6px 11px;font-size:12.5px;font-weight:500;cursor:pointer;white-space:nowrap}
    .ghost:hover:not(:disabled){color:var(--ink);border-color:var(--blue);background:var(--panel)}
    .ghost:disabled{opacity:.42;cursor:default}
    .repo-nav{display:flex;align-items:flex-end;gap:6px;min-width:0;padding:0 18px;overflow:auto}
    .repo-tab{display:flex;align-items:center;gap:6px;min-height:38px;padding:0 10px;color:var(--dim);font-size:12.5px;border:0;border-bottom:2px solid transparent;background:transparent;white-space:nowrap;cursor:pointer}
    .repo-tab:hover{color:var(--ink);background:var(--panel)}
    .repo-tab.on{color:var(--ink);font-weight:600;border-bottom-color:var(--blue)}
    .repo-tab .oct{width:16px;height:16px;color:var(--dim)}
    .repo-tab.on .oct{color:var(--blue)}
    .repo-tab .count{min-width:18px;padding:0 6px;border-radius:999px;background:var(--raise);color:var(--dim);font-family:var(--mono);font-size:10.5px;text-align:center}
    button:focus-visible,input:focus-visible,select:focus-visible{outline:2px solid var(--blue);outline-offset:2px}

    /* ---- 3 columns ---- */
    .cols{display:grid;grid-template-columns:minmax(260px,320px) minmax(280px,340px) minmax(0,1fr);min-height:0;min-width:0;max-width:100vw;background:var(--bg)}
    .pane{min-height:0;min-width:0;display:flex;flex-direction:column;border-right:1px solid var(--edge);overflow:hidden;background:var(--bg)}
    .pane:last-child{border-right:0}
    .app.activity-mode .cols{grid-template-columns:minmax(360px,480px) minmax(0,1fr)}
    .app.activity-mode .records-pane{display:none}
    .app.pr-mode .cols{grid-template-columns:minmax(380px,520px) minmax(0,1fr)}
    .app.pr-mode .records-pane{display:none}
    .app.branches-mode .cols{grid-template-columns:minmax(0,1fr)}
    .app.branches-mode .records-pane{display:none}
    .app.branches-mode .diff-pane{display:none}
    .app.branches-mode .history-pane{border-right:0}
    /* Branch/PR workflow buttons belong to their tab: hidden by default (Records/Activity),
       shown only when their tab's screen is active. */
    .cmd-branches,.cmd-pr{display:none}
    .app.branches-mode .cmd-branches{display:inline-flex}
    .app.pr-mode .cmd-pr{display:inline-flex}
    .app.branches-mode .cmdset,.app.pr-mode .cmdset{padding-left:0}
    .ph{display:flex;align-items:center;gap:9px;height:42px;padding:0 14px;flex:0 0 auto;
      border-bottom:1px solid var(--edge);background:var(--chrome)}
    .ph .lab{font-family:var(--disp);font-weight:600;font-size:12px;letter-spacing:.04em;text-transform:uppercase;color:var(--dim)}
    .ph .sp{flex:1}
    .ph .ct{font-family:var(--mono);font-size:11px;color:var(--faint)}
    .scroll{overflow:auto;flex:1;min-height:0}

    /* ---- LEFT: Compass-style collection tree ---- */
    .find{padding:10px 12px;border-bottom:1px solid var(--edge);background:var(--chrome);flex:0 0 auto}
    .find input{width:100%;background:var(--bg);border:1px solid var(--edge2);border-radius:8px;
      padding:7px 11px;font-size:12.5px;font-family:var(--mono)}
    .find input:focus{outline:none;border-color:var(--blue);box-shadow:0 0 0 3px var(--sky-bg)}
    .filterbar{display:flex;gap:6px;padding:9px 12px 5px;flex-wrap:wrap;flex:0 0 auto}
    .fchip{background:transparent;border:1px solid var(--edge2);border-radius:7px;color:var(--dim);
      padding:3px 9px;font-size:11.5px;cursor:pointer;display:flex;align-items:center;gap:6px}
    .fchip.on{background:var(--panel2);color:var(--ink);border-color:var(--blue)}
    .fchip .n{font-family:var(--mono);font-size:10.5px;color:var(--faint)}
    .fchip.on .n{color:var(--blue)}
    .tree{padding:6px 0 14px}
    .coll{user-select:none}
    .coll-h{display:flex;align-items:center;gap:8px;padding:8px 12px;cursor:pointer}
    .coll-h:hover{background:var(--panel)}
    .tw{width:14px;text-align:center;color:var(--faint);font-size:10px;transition:transform .12s;flex:0 0 auto}
    .coll.open .tw{transform:rotate(90deg)}
    .coll-ic{width:15px;height:15px;flex:0 0 auto;color:var(--dim)}
    .coll-nm{flex:1;min-width:0;font-family:var(--mono);font-size:12.5px;font-weight:500;
      overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
    .coll-ct{font-family:var(--mono);font-size:10.5px;color:var(--faint)}
    .coll-warn{width:7px;height:7px;border-radius:50%;background:var(--amber);flex:0 0 auto}
    .docs{display:none}
    .coll.open .docs{display:block}
    .doc{display:flex;align-items:center;gap:9px;padding:6px 12px 6px 30px;cursor:pointer;position:relative}
    .doc::before{content:"";position:absolute;left:18px;top:0;bottom:0;width:1px;background:var(--edge)}
    .doc:hover{background:var(--panel)}
    .doc.sel{background:var(--panel2)}
    .doc.sel::after{content:"";position:absolute;left:0;top:0;bottom:0;width:2px;background:var(--blue)}
    .doc .st{width:7px;height:7px;border-radius:50%;flex:0 0 auto}
    .st.clean{background:var(--moss)} .st.drift{background:var(--amber);box-shadow:0 0 0 3px var(--amber-bg)} .st.new{background:var(--sky)}
    .doc .nm{flex:1;min-width:0;font-family:var(--mono);font-size:12px;display:flex;align-items:baseline;gap:0;
      overflow:hidden;white-space:nowrap}
    .doc .nm .pre{color:var(--faint);flex:0 1 auto;overflow:hidden;text-overflow:ellipsis;min-width:0}
    .doc .nm .leaf{color:var(--dim);flex:0 0 auto}
    .doc.sel .nm .leaf{color:var(--ink)}
    .doc .rt{font-family:var(--mono);font-size:10px;color:var(--faint);flex:0 0 auto}
    .doc.sel .nm{color:var(--ink)}
    /* multi-select "in context" marker (cmd/ctrl-click adds a record to the impact context) */
    .doc.ctx{background:var(--sky-bg)}
    .doc.ctx .ckx{color:var(--sky)}
    .ckx{flex:0 0 auto;width:13px;height:13px;display:grid;place-items:center;font-size:10px;color:transparent}
    .doc:hover .ckx{color:var(--edge2)}
    .doc.ctx:hover .ckx{color:var(--sky)}
    .tag{font-family:var(--mono);font-size:9.5px;letter-spacing:.04em;text-transform:uppercase;
      padding:1px 6px;border-radius:5px}
    .tag.drift{color:var(--amber);background:var(--amber-bg)}
    .tag.new{color:var(--sky);background:var(--sky-bg)}
    .empty{padding:28px 14px;color:var(--faint);font-size:12.5px;text-align:center}

    /* ---- MIDDLE: commit graph + timeline (the signature rail) ---- */
    .ghost-pane{padding:42px 22px;color:var(--faint);font-size:13px;text-align:center;line-height:1.7}
    .ghost-pane .big{font-family:var(--disp);font-size:15px;color:var(--dim);margin-bottom:6px}
    .selhdr{padding:14px;border-bottom:1px solid var(--edge);flex:0 0 auto;background:var(--chrome)}
    .selhdr .nm{font-family:var(--mono);font-size:13px;font-weight:500;word-break:break-all;line-height:1.4}
    .selhdr .meta{margin-top:6px;display:flex;gap:8px;align-items:center;flex-wrap:wrap;font-size:11.5px;color:var(--dim)}
    .rail{padding:8px 0 18px}
    .node{position:relative;padding:10px 14px 10px 40px;cursor:pointer}
    .node:hover{background:var(--panel)}
    .node.sel{background:var(--panel2)}
    .node.sel::before{content:"";position:absolute;left:0;top:0;bottom:0;width:2px;background:var(--blue)}
    /* graph rail: vertical line + node markers */
    .node .line{position:absolute;left:21px;top:0;bottom:0;width:2px;background:var(--edge2)}
    .node:first-child .line{top:20px}
    .node:last-child .line{bottom:calc(100% - 20px)}
    .node .mk{position:absolute;left:15px;top:14px;width:13px;height:13px;border-radius:50%;
      background:var(--panel);border:2px solid var(--moss);z-index:1}
    .node.commit .mk{border-color:var(--blue)}
    .node.restore .mk{border-color:var(--sky)}
    .node.adopt .mk{border-color:var(--moss)}
    .node.importt .mk{border-color:var(--faint)}
    /* drift = open dashed ring in amber, sitting above the committed line */
    .node.live .mk{border:2px dashed var(--amber);background:var(--amber-bg);width:14px;height:14px;left:14.5px;animation:pulse 2.4s ease-in-out infinite}
    @keyframes pulse{0%,100%{box-shadow:0 0 0 0 var(--amber-bg)}50%{box-shadow:0 0 0 5px transparent}}
    .node .msg{font-size:13px;line-height:1.4;margin-bottom:4px}
    .node.live .msg{color:var(--amber);font-weight:500}
    .node .sub{display:flex;gap:8px;align-items:center;flex-wrap:wrap;font-family:var(--mono);font-size:11px;color:var(--faint)}
    .op{font-family:var(--mono);font-size:9.5px;letter-spacing:.05em;text-transform:uppercase;
      padding:1px 6px;border-radius:5px;border:1px solid var(--edge2);color:var(--dim)}
    .op.r-restore{color:var(--sky);border-color:var(--sky-bg)}
    .op.r-adopt{color:var(--moss);border-color:var(--moss-bg)}
    .op.r-commit{color:var(--blue);border-color:var(--sky-bg)}

    /* ---- RIGHT: paper diff (the reading surface) ---- */
    .dhead{display:flex;align-items:center;gap:10px;height:42px;padding:0 16px;flex:0 0 auto;
      border-bottom:1px solid var(--edge);background:var(--chrome)}
    .dhead .t{font-size:12.5px;color:var(--dim);min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
    .dhead .t b{color:var(--ink);font-family:var(--mono)}
    .dhead .sp{flex:1}
    #dActs{display:flex;align-items:center;gap:8px;min-width:0}
    .btn{border:1px solid var(--edge2);border-radius:8px;padding:6px 13px;font-size:12.5px;cursor:pointer;
      background:var(--panel);color:var(--ink);font-weight:500;white-space:nowrap}
    .btn:hover:not(:disabled){border-color:var(--blue);background:var(--panel2)}
    .btn.go{background:var(--blue);border-color:var(--blue);color:#fff}
    .btn.go:hover:not(:disabled){background:var(--blue2)}
    .btn.warn{color:var(--amber);border-color:var(--amber-bg)}
    .btn:disabled{opacity:.45;cursor:default}
    /* padding lives on .paper as margin (not on the scroll box) so the sticky field header
       pins flush to the visible top edge — sticky top:0 references the scroll content box. */
    .paperwrap{flex:1;min-height:0;min-width:0;overflow:auto;background:
      linear-gradient(180deg,rgba(255,255,255,.018),transparent 120px),var(--bg);padding:16px}
    /* no overflow:hidden here — it would clip the sticky field header. Round the top via the
       legend; the bottom rows sit flush (the paper border still reads as rounded). */
    .paper{background:var(--paper);color:var(--paper-ink);border:1px solid var(--paper-edge);border-radius:10px;min-width:0;
      box-shadow:var(--shadow);font-family:var(--mono);font-size:12.5px;margin:0 0 16px}
    .paper-h{border-radius:10px 10px 0 0}
    .paper-h{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1fr);border-bottom:1px solid var(--paper-edge)}
    .paper-h>div{padding:9px 16px;font-size:10.5px;letter-spacing:.07em;text-transform:uppercase;color:var(--paper-dim);
      display:flex;align-items:center;gap:7px}
    .paper-h .r{border-left:1px solid var(--paper-edge)}
    .paper-h .swatch{width:8px;height:8px;border-radius:2px}
    .paper-h .l .swatch{background:var(--paper-del-ink)} .paper-h .r .swatch{background:var(--paper-add-ink)}
    .record-title{display:flex;align-items:center;gap:8px;min-height:32px;
      padding:6px 16px;background:var(--paper-gutter);border-bottom:1px solid var(--paper-edge);
      color:var(--paper-ink);font-size:11px;font-weight:600;letter-spacing:.02em}
    .record-title .op{background:rgba(43,42,37,.06);border-color:var(--paper-edge);color:var(--paper-dim)}
    .frow{border-bottom:1px solid var(--paper-edge)}
    .frow:last-child{border-bottom:0}
    /* the field name is the sticky header for its whole diff: it pins to the top of the
       scroll area and stays visible the entire time you scroll that field's lines.
       its parent .frow is tall (the full field diff), so sticky has room to travel.
       the leading fold's expand control is fused in here, so header + "expand N
       unchanged" are a single pinned bar (not two stacked bars that scroll apart). */
    .fname{position:sticky;top:0;z-index:4;display:flex;align-items:center;gap:12px;min-height:30px;
      padding:5px 16px;font-size:11px;color:var(--paper-dim);background:var(--paper-gutter);
      border-bottom:1px solid var(--paper-edge);box-shadow:0 1px 0 rgba(0,0,0,.04)}
    .fname .fnm{letter-spacing:.04em;text-transform:uppercase;font-weight:600;flex:0 0 auto}
    .fname .fhx{display:flex;align-items:center;gap:6px;margin-left:auto}
    .fname .leadfold{display:flex;align-items:center;gap:6px}
    .fpair{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1fr)}
    .fside{padding:8px 16px;white-space:pre-wrap;word-break:break-word;min-height:34px;line-height:1.55}
    .fside.r{border-left:1px solid var(--paper-edge)}
    .fside.del{background:var(--paper-del);color:var(--paper-del-ink)}
    .fside.add{background:var(--paper-add);color:var(--paper-add-ink)}
    .fside.void{background:repeating-linear-gradient(45deg,transparent,transparent 7px,rgba(0,0,0,.025) 7px,rgba(0,0,0,.025) 14px)}
    /* line-aligned split diff for long multi-line strings (git split view) */
    /* row-aligned split diff: each .drow has a left + right cell; folds are expandable */
    .splitgrid{display:flex;flex-direction:column}
    .drow{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1fr)}
    .dcell{display:grid;grid-template-columns:38px 14px 1fr;align-items:baseline;line-height:1.5;font-size:12px;
      border-bottom:1px solid rgba(0,0,0,.04);min-width:0}
    .dcell.r{border-left:1px solid var(--paper-edge)}
    .dcell .gut{text-align:right;padding:3px 7px 3px 0;color:var(--paper-dim);user-select:none;font-size:10.5px;
      border-right:1px solid var(--paper-edge);background:var(--paper-gutter)}
    .dcell .sign{text-align:center;user-select:none;color:var(--paper-dim)}
    .dcell .tx{padding:3px 10px;white-space:pre-wrap;word-break:break-word}
    .dcell.ctx .tx{color:var(--paper-dim)}
    .dcell.del{background:var(--paper-del)} .dcell.del .tx,.dcell.del .sign{color:var(--paper-del-ink)} .dcell.del .gut{background:#f3d9d3;color:#b56a5c}
    .dcell.add{background:var(--paper-add)} .dcell.add .tx,.dcell.add .sign{color:var(--paper-add-ink)} .dcell.add .gut{background:#d9ead2;color:#5f8a64}
    .dcell.void{background:repeating-linear-gradient(45deg,transparent,transparent 7px,rgba(0,0,0,.022) 7px,rgba(0,0,0,.022) 14px)}
    .foldrow{grid-template-columns:1fr}
    .foldbar{display:flex;align-items:center;justify-content:center;gap:6px;padding:4px 16px;
      background:var(--paper-gutter);border-top:1px solid var(--paper-edge);border-bottom:1px solid var(--paper-edge)}
    .fx{font-family:var(--mono);font-size:10.5px;color:var(--paper-dim);background:var(--paper);
      border:1px solid var(--paper-edge);border-radius:6px;padding:2px 10px;cursor:pointer;line-height:1.5}
    .fx:hover{color:var(--paper-ink);border-color:var(--paper-dim);background:#fff}
    .nodiff{padding:34px 16px;color:var(--paper-dim);text-align:center;font-family:var(--body);font-size:13px}
    /* impact / system-overview panel (dark, sits above the paper diff) */
    .impact{margin:0 0 16px;background:var(--panel);border:1px solid var(--edge2);border-radius:12px;overflow:hidden;box-shadow:0 14px 30px rgba(0,0,0,.18)}
    .impact .ih{display:flex;align-items:center;gap:10px;padding:11px 15px;border-bottom:1px solid var(--edge)}
    .impact .ih .tt{font-family:var(--disp);font-weight:600;font-size:13px}
    .impact .ih .sp{flex:1}
    .risk{font-family:var(--mono);font-size:10.5px;letter-spacing:.05em;text-transform:uppercase;padding:2px 9px;border-radius:999px}
    .risk.low{color:var(--moss);background:var(--moss-bg)} .risk.medium{color:var(--amber);background:var(--amber-bg)}
    .risk.high,.risk.breaking{color:#ff7a6b;background:rgba(248,81,73,.14)}
    .impact .ib{padding:13px 15px;display:flex;flex-direction:column;gap:11px}
    .impact .sum{font-size:13px;line-height:1.55;color:var(--ink)}
    .impact .row{display:flex;gap:8px;flex-wrap:wrap;align-items:center;font-size:12px;color:var(--dim)}
    .impact .row .k{font-family:var(--mono);font-size:10.5px;letter-spacing:.04em;text-transform:uppercase;color:var(--faint);min-width:96px}
    .cat{font-family:var(--mono);font-size:11px;padding:1px 8px;border-radius:6px;border:1px solid var(--edge2);color:var(--dim)}
    .aff{font-family:var(--mono);font-size:11.5px;color:var(--sky)}
    .impact .note{font-size:12px;color:var(--faint);line-height:1.5;border-top:1px solid var(--edge);padding-top:11px}
    .impact .llm{border-top:1px solid var(--edge);padding-top:11px}
    .impact .llm .who{font-family:var(--mono);font-size:10.5px;text-transform:uppercase;letter-spacing:.05em;color:var(--faint);margin-bottom:6px}
    .impact .llm .body{font-size:12.5px;line-height:1.6;color:var(--ink)}
    .impact .llm .lk{font-size:12px;line-height:1.5;color:var(--dim);margin-top:7px}
    .impact .llm .lkk{display:block;font-family:var(--mono);font-size:10px;letter-spacing:.04em;text-transform:uppercase;
      color:var(--faint);margin-bottom:3px}
    .impact .llm .llmul{margin:3px 0 0;padding-left:18px;display:flex;flex-direction:column;gap:3px}
    .impact .llm .llmul li{font-size:12px;line-height:1.45;color:var(--dim)}
    /* inline markdown the LLM emits: bold/italic inherit; code gets a subtle chip */
    .impact .llm strong{color:var(--ink);font-weight:600}
    .impact .llm code{font-family:var(--mono);font-size:.92em;background:var(--chip,rgba(127,127,127,.14));
      padding:1px 5px;border-radius:4px}
    .impact .off{font-size:12px;color:var(--faint)}
    .doconly .paper-h{grid-template-columns:1fr}
    .docbody{padding:14px 16px;white-space:pre-wrap;word-break:break-word;line-height:1.6;max-height:none}

    .spin{padding:30px;color:var(--faint);font-size:13px;text-align:center}
    .toast{position:fixed;bottom:20px;left:50%;transform:translateX(-50%) translateY(8px);background:var(--panel2);
      border:1px solid var(--edge2);border-radius:10px;padding:10px 18px;font-size:13px;z-index:60;
      box-shadow:var(--shadow);opacity:0;transition:all .18s;pointer-events:none;display:flex;align-items:center;gap:9px}
    .toast.show{opacity:1;transform:translateX(-50%) translateY(0)}
    .toast.err{border-color:var(--paper-del-ink)}
    .toast .ok{color:var(--moss)} .toast .bad{color:var(--paper-del-ink)}

    /* self-teaching remedy card: shown when an action result carries `next` */
    .remedy{position:fixed;bottom:20px;right:20px;max-width:440px;background:var(--panel2);
      border:1px solid var(--amber);border-radius:10px;padding:12px 14px;font-size:12.5px;z-index:61;
      box-shadow:var(--shadow);opacity:0;transform:translateY(8px);transition:all .18s;pointer-events:none}
    .remedy.show{opacity:1;transform:translateY(0);pointer-events:auto}
    .remedy .rw{color:var(--ink);margin-bottom:5px}
    .remedy .rr{color:var(--faint);margin-bottom:8px}
    .remedy .rc{display:flex;align-items:center;gap:6px;background:var(--panel);border:1px solid var(--edge2);
      border-radius:6px;padding:4px 8px;margin:4px 0;font-family:ui-monospace,monospace;font-size:11.5px}
    .remedy .rc code{flex:1;color:var(--ink);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
    .remedy .rc .cp{cursor:pointer;color:var(--sky);flex:0 0 auto;border:none;background:none;font-size:11px}
    .remedy .rc .cp:hover{color:var(--ink)}
    .remedy .rx{position:absolute;top:8px;right:10px;cursor:pointer;color:var(--faint);border:none;background:none}
    .remedy .rx:hover{color:var(--ink)}

    .mbg{position:fixed;inset:0;background:rgba(8,11,16,.6);backdrop-filter:blur(2px);display:none;
      align-items:center;justify-content:center;z-index:50}
    .mbg.show{display:flex}
    .modal{background:var(--panel);border:1px solid var(--edge2);border-radius:14px;width:min(620px,92vw);
      box-shadow:var(--shadow);overflow:visible}
    .modal h3{margin:0;padding:16px 18px;font-family:var(--disp);font-weight:600;font-size:15px;border-bottom:1px solid var(--edge)}
    .modal .b{padding:16px 18px;display:flex;flex-direction:column;gap:12px}
    .modal .desc{color:var(--dim);font-size:13px;line-height:1.55}
    .modal .desc b{color:var(--ink);font-family:var(--mono);font-size:12.5px}
    .modal label{font-size:11px;letter-spacing:.04em;text-transform:uppercase;color:var(--faint);font-weight:600}
    .modal input{width:100%;background:var(--bg);border:1px solid var(--edge2);border-radius:8px;padding:9px 11px;font-family:var(--mono);font-size:12.5px}
    .modal textarea{width:100%;min-height:320px;resize:vertical;background:var(--bg);border:1px solid var(--edge2);border-radius:8px;padding:9px 11px;font-family:var(--mono);font-size:12px;color:var(--ink)}
    .modal input:focus,.modal textarea:focus{outline:none;border-color:var(--blue)}
    .modal .f{display:flex;justify-content:flex-end;gap:9px;padding:14px 18px;border-top:1px solid var(--edge)}

    /* ---- PR compare workspace ---- */
    .prwork{padding:12px;display:flex;flex-direction:column;gap:12px;min-width:0;overflow:visible}
    .prbox{position:relative;z-index:1;background:var(--panel);border:1px solid var(--edge2);border-radius:12px;overflow:visible;box-shadow:0 12px 28px rgba(0,0,0,.16)}
    .prbox .hd{display:flex;align-items:center;gap:10px;padding:11px 13px;border-bottom:1px solid var(--edge);background:var(--chrome)}
    .prbox .ttl{font-family:var(--disp);font-size:13px;font-weight:600;color:var(--ink)}
    .prbox .sp{flex:1}
    .prbox .ct{font-family:var(--mono);font-size:11px;color:var(--faint)}
    .prbox .subtle{font-size:12px;color:var(--faint);line-height:1.5}
    .prbox .bd{padding:13px;display:flex;flex-direction:column;gap:12px}
    .prbox.primary{z-index:2;border-color:var(--blue);box-shadow:0 16px 34px rgba(0,0,0,.22)}
    .compare-hero{display:flex;align-items:flex-start;justify-content:space-between;gap:12px;padding:2px 2px 0}
    .compare-hero .title{font-family:var(--disp);font-size:20px;line-height:1.15;font-weight:600;color:var(--ink)}
    .compare-hero .meta{margin-top:4px;font-family:var(--mono);font-size:11.5px;color:var(--faint)}
    .compare-hero .op{margin-top:1px}
    .compare-row{display:grid;grid-template-columns:minmax(0,1fr) 22px minmax(0,1fr);align-items:end;gap:9px}
    .compare-row .arr{font-family:var(--mono);color:var(--faint);text-align:center;padding-bottom:8px}
    .prpick{width:100%;max-width:none}
    .prselect{display:flex;flex-direction:column;gap:5px;min-width:0}
    .prselect .selectbox{width:100%;max-width:none}
    .prselect .cap,.prmsg label{font-family:var(--mono);font-size:10px;letter-spacing:.07em;text-transform:uppercase;color:var(--faint)}
    .branch-pill{display:flex;align-items:center;gap:8px;min-height:32px;border:1px solid var(--edge2);border-radius:8px;
      background:var(--bg);padding:6px 10px;font-family:var(--mono);font-size:12.5px;color:var(--select-ink);font-weight:600;min-width:0}
    .branch-pill:before{content:"";width:7px;height:7px;border-radius:2px;background:var(--moss);box-shadow:0 0 0 3px var(--moss-bg);flex:0 0 auto}
    .pr-status{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:8px}
    .pr-stat{border:1px solid var(--edge);border-radius:10px;background:var(--bg);padding:9px 10px;min-width:0}
    .pr-stat .num{display:block;font-family:var(--disp);font-size:18px;line-height:1.1;color:var(--ink);font-weight:600}
    .pr-stat .cap{display:block;margin-top:2px;font-family:var(--mono);font-size:10px;letter-spacing:.06em;text-transform:uppercase;color:var(--faint)}
    .mergebar{display:flex;align-items:center;gap:9px;min-width:0;border:1px solid var(--edge);border-radius:10px;background:var(--bg);
      padding:9px 10px;font-size:12.5px;color:var(--dim);line-height:1.35}
    .mergebar.ok{border-color:var(--moss);background:var(--moss-bg)}
    .mergebar.warn{border-color:var(--amber);background:var(--amber-bg)}
    .mergebar .check{font-family:var(--mono);font-weight:700;color:var(--moss);flex:0 0 auto}
    .mergebar.warn .check{color:var(--amber)}
    .mergebar b{color:var(--ink);font-weight:600}
    .mergebar span:last-child{min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
    .pr-intent{border:1px solid var(--edge);border-radius:10px;background:var(--bg);padding:11px;display:flex;flex-direction:column;gap:10px}
    .pr-intent.open{border-color:var(--moss);background:var(--moss-bg)}
    .pr-intent .topline{display:flex;align-items:center;gap:8px;min-width:0;font-size:12px;color:var(--dim)}
    .pr-intent .topline b{font-family:var(--mono);font-size:12px;color:var(--ink);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
    .pr-intent .message{font-size:12.5px;color:var(--ink);line-height:1.45}
    .prmeta{display:flex;align-items:center;gap:7px;flex-wrap:wrap;font-size:12px;color:var(--dim)}
    .prmeta b{color:var(--ink);font-weight:600}
    .prmsg{display:flex;flex-direction:column;gap:6px}
    .prmsg input{width:100%;background:var(--bg);border:1px solid var(--edge2);border-radius:8px;padding:8px 10px;font-family:var(--mono);font-size:12px;color:var(--ink)}
    .prmsg input:focus{outline:none;border-color:var(--blue);box-shadow:0 0 0 3px var(--sky-bg)}
    .praction{display:flex;align-items:center;gap:8px;flex-wrap:wrap}
    .prlist{display:flex;flex-direction:column;gap:8px}
    .prcard,.prrec{border:1px solid var(--edge);border-radius:10px;background:var(--bg);padding:10px 11px}
    .prcard{display:flex;flex-direction:column;gap:7px}
    .prcard.on{border-color:var(--moss);background:var(--moss-bg)}
    .prid{display:flex;align-items:center;gap:8px;min-width:0;font-family:var(--mono);font-size:12px;color:var(--ink)}
    .prid b{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
    .prrec{display:grid;grid-template-columns:minmax(0,1fr) auto;gap:8px;align-items:center;cursor:pointer}
    .prrec:hover{border-color:var(--blue);background:var(--panel)}
    .prrec.sel{border-color:var(--blue);background:var(--panel2);box-shadow:inset 2px 0 0 var(--blue)}
    .prrec .name{font-family:var(--mono);font-size:12px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
    .prrec .lines{font-family:var(--mono);font-size:10.5px;color:var(--faint)}
    .prrec .delta{font-family:var(--mono);font-size:10.5px;color:var(--moss);background:var(--moss-bg);border-radius:999px;padding:2px 7px}
    .pr-empty{padding:26px 16px;text-align:center;color:var(--faint);line-height:1.6}
    .branchcard{display:grid;grid-template-columns:minmax(0,1fr) auto;gap:10px;align-items:center;border:1px solid var(--edge);
      border-radius:10px;background:var(--bg);padding:11px;cursor:pointer}
    .branchcard:hover{border-color:var(--blue);background:var(--panel)}
    .branchcard.on{border-color:var(--blue);background:var(--panel2);box-shadow:inset 2px 0 0 var(--blue)}
    .branchcard.default.on{border-color:var(--moss);box-shadow:inset 2px 0 0 var(--moss)}
    .branch-main{display:flex;align-items:center;gap:8px;min-width:0}
    .branch-dot{width:8px;height:8px;border-radius:2px;background:var(--moss);box-shadow:0 0 0 3px var(--moss-bg);flex:0 0 auto}
    .branchcard.draft .branch-dot{background:var(--blue);box-shadow:0 0 0 3px var(--sky-bg)}
    .branch-name{font-family:var(--mono);font-size:12.5px;color:var(--ink);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
    .branch-sub{display:flex;gap:7px;align-items:center;flex-wrap:wrap;margin-top:5px;font-family:var(--mono);font-size:10.5px;color:var(--faint)}
    .branch-actions{display:flex;gap:7px;align-items:center;justify-content:flex-end;flex-wrap:wrap}
    .branch-compare{display:flex;gap:9px;align-items:end;flex-wrap:wrap;min-width:0}
    .branch-compare .prselect{flex:1 1 260px;min-width:220px}
    .branch-compare .arr{flex:0 0 22px;font-family:var(--mono);color:var(--faint);text-align:center;padding-bottom:8px}
    .branch-compare .btn{flex:0 0 auto}
    .branch-table{display:flex;flex-direction:column;border:1px solid var(--edge);border-radius:10px;overflow:hidden;background:var(--bg)}
    .branchrow{display:grid;grid-template-columns:minmax(220px,1.2fr) minmax(170px,.9fr) minmax(120px,.6fr) auto;gap:12px;align-items:center;
      padding:11px 12px;border-bottom:1px solid var(--edge)}
    .branchrow:last-child{border-bottom:0}
    .branchrow:hover{background:var(--panel)}
    .branchrow .muted{font-size:12px;color:var(--faint);min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
    .branchrow .prcell{font-family:var(--mono);font-size:11px;color:var(--dim)}
    .modal .selectbox,.modal .selectbox .envpick{width:100%}

    @media (max-width:1080px){
      .cols{grid-template-columns:minmax(230px,260px) minmax(250px,300px) minmax(0,1fr)}
      #whoTxt{max-width:220px}
      .ghost{padding-inline:9px}
    }
    @media (max-width:960px){
      .app{height:auto;min-height:100vh;overflow-x:hidden}
      .repo-main{display:flex;flex-direction:column;padding:12px;gap:10px}
      .repo-left,.repo-actions{width:100%}
      .repo-actions{justify-content:flex-start}
      .scopebar,.cmdset{flex:1 1 100%;flex-wrap:wrap}
      #whoTxt{max-width:100%}
      .envpick,.seg,.ghost{height:32px}
      .pick{flex:1 1 150px}
      .envpick{max-width:100%;flex:1 1 130px}
      .ghost{flex:1 1 72px;padding:5px 8px}
      .seg{flex:1 1 104px;justify-content:center}
      .repo-nav{padding:0 12px}
      .cols{grid-template-columns:minmax(0,1fr);grid-template-rows:minmax(240px,34vh) minmax(260px,34vh) minmax(360px,auto);min-height:0}
      .app.activity-mode .cols{grid-template-columns:minmax(0,1fr);grid-template-rows:minmax(260px,38vh) minmax(360px,auto)}
      .app.pr-mode .cols{grid-template-columns:minmax(0,1fr);grid-template-rows:minmax(320px,44vh) minmax(420px,auto)}
      .app.branches-mode .cols{grid-template-columns:minmax(0,1fr);grid-template-rows:minmax(520px,auto)}
      .pane{border-right:0;border-bottom:1px solid var(--edge);min-height:0}
      .dhead{height:auto;min-height:42px;flex-wrap:wrap;padding:8px 12px}
      .paperwrap{padding:10px}
      .paper{font-size:12px}
      .impact .row .k{min-width:72px}
    }
    @media (max-width:560px){
      body{font-size:13px}
      .selectbox{width:100%}
      .envpick{width:100%}
      .compare-row{grid-template-columns:1fr;gap:8px}
      .compare-row .arr{display:none}
      .branch-compare{grid-template-columns:1fr}
      .branch-compare .arr{display:none}
      .branchrow{grid-template-columns:1fr;align-items:start}
      .pr-status{grid-template-columns:1fr}
      .filterbar{overflow:auto;flex-wrap:nowrap;padding-bottom:8px}
      .fchip{flex:0 0 auto}
      .node{padding-right:10px}
      .node .sub{font-size:10px;gap:6px}
      .paper-h,.fpair,.drow{grid-template-columns:1fr}
      .paper-h .r,.fside.r,.dcell.r{border-left:0;border-top:1px solid var(--paper-edge)}
      .paper-h>div{padding:8px 12px}
      .fside{padding:8px 12px}
      .dcell{grid-template-columns:30px 14px 1fr}
      .impact .ib{padding:12px}
      .modal textarea{min-height:220px}
    }
    @media (prefers-reduced-motion:reduce){ *{animation:none!important;transition:none!important} }
  </style>
</head>
<body>
  <div class="app">
    <header class="top">
      <div class="repo-main">
        <div class="repo-left">
          <div class="repo-title">
            <svg class="repo-ic" viewBox="0 0 16 16" aria-hidden="true"><path fill="currentColor" d="M2 2.75A.75.75 0 0 1 2.75 2h8.5a.75.75 0 0 1 .75.75v10.5a.75.75 0 0 1-1.13.65L7 11.65 3.13 13.9A.75.75 0 0 1 2 13.25Zm1.5.75v8.44l3.12-1.82a.75.75 0 0 1 .76 0l3.12 1.82V3.5Z"/></svg>
            <div class="brand">cfg<span class="dot">/</span><span id="projectName">cfgit</span></div>
            <span class="repo-scope">local</span>
          </div>
          <div class="repo-meta">
            <div class="who" id="who"><span class="ava" id="ava">·</span><span id="whoTxt">connecting…</span></div>
            <span class="chip open" id="mode"></span>
            <span class="repo-stats" id="repoStats"></span>
          </div>
        </div>
        <div class="repo-actions">
          <div class="scopebar">
            <label class="pick"><span>Env</span><span class="selectbox envbox"><select class="envpick" id="env" title="Environment"><option>dev</option></select></span></label>
            <label class="pick"><span>Branch</span><span class="selectbox branchbox"><select class="envpick branchpick" id="branch" title="Branch"><option>main</option></select></span></label>
            <span class="branch-state" id="branchState"></span>
          </div>
          <div class="cmdset" aria-label="Branch workflow">
            <button class="ghost cmd-branches" id="branchDiff" type="button" title="Compare current branch with main">Compare</button>
            <button class="ghost cmd-branches" id="newBranch" type="button" title="Create a draft branch">New branch</button>
            <button class="ghost cmd-branches" id="draftCommit" type="button" title="Commit selected record to the current branch">Commit draft</button>
            <button class="ghost cmd-pr" id="openPr" type="button" title="Prepare a cfgit PR for the current branch">Prepare PR</button>
            <button class="ghost cmd-pr" id="mergePr" type="button" title="Merge the open cfgit PR">Merge PR</button>
          </div>
          <button class="ghost" id="refresh" type="button" title="Reload state">Refresh</button>
          <div class="seg" id="theme" aria-label="Theme"><button data-th="dark" class="on">Dark</button><button data-th="light">Light</button></div>
        </div>
      </div>
      <div class="repo-nav" aria-label="Repository navigation">
        <button class="repo-tab on" id="navRecordsTab" type="button">
          <svg class="oct" viewBox="0 0 16 16" aria-hidden="true"><path fill="currentColor" d="M2.75 2h10.5a.75.75 0 0 1 .75.75v10.5a.75.75 0 0 1-.75.75H2.75A.75.75 0 0 1 2 13.25V2.75A.75.75 0 0 1 2.75 2ZM3.5 3.5v2h9v-2Zm9 3.5h-9v5.5h9Z"/></svg>
          Records <span class="count" id="navRecords">0</span>
        </button>
        <button class="repo-tab" id="navBranches" type="button">
          <svg class="oct" viewBox="0 0 16 16" aria-hidden="true"><path fill="currentColor" d="M5 3.25a1.25 1.25 0 1 1-2.5 0A1.25 1.25 0 0 1 5 3.25ZM3.75.5a2.75 2.75 0 0 0-.75 5.396v4.208a2.751 2.751 0 1 0 1.5 0V5.896A2.75 2.75 0 0 0 3.75.5Zm0 11a1.25 1.25 0 1 1 0 2.5 1.25 1.25 0 0 1 0-2.5ZM11.5 8.75a.75.75 0 0 0-1.5 0v1.354a2.751 2.751 0 1 0 1.5 0V8.75Zm-.75 2.75a1.25 1.25 0 1 1 0 2.5 1.25 1.25 0 0 1 0-2.5ZM5.75 4.5h2.5A3.25 3.25 0 0 1 11.5 7.75v.25H10v-.25A1.75 1.75 0 0 0 8.25 6h-2.5Z"/></svg>
          Branches <span class="count" id="navBranchesCount">0</span>
        </button>
        <button class="repo-tab" id="navPr" type="button">
          <svg class="oct" viewBox="0 0 16 16" aria-hidden="true"><path fill="currentColor" d="M5 3.25a1.25 1.25 0 1 1-2.5 0 1.25 1.25 0 0 1 2.5 0ZM3.75.5a2.75 2.75 0 0 0-.75 5.396v4.208a2.751 2.751 0 1 0 1.5 0V5.896A2.75 2.75 0 0 0 3.75.5Zm0 11a1.25 1.25 0 1 1 0 2.5 1.25 1.25 0 0 1 0-2.5ZM12 5.896a2.75 2.75 0 1 0-1.5 0V7A2.5 2.5 0 0 1 8 9.5H6v1.5h2A4 4 0 0 0 12 7Zm-1.25-2.646a1.25 1.25 0 1 1 2.5 0 1.25 1.25 0 0 1-2.5 0Z"/></svg>
          Pull requests <span class="count" id="navPrCount">0</span>
        </button>
        <button class="repo-tab" id="navHistoryTab" type="button">
          <svg class="oct" viewBox="0 0 16 16" aria-hidden="true"><path fill="currentColor" d="M1.75 8a6.25 6.25 0 1 1 2.183 4.743.75.75 0 0 1 .976-1.14A4.75 4.75 0 1 0 3.25 8H5.5a.75.75 0 0 1 0 1.5H2.5a.75.75 0 0 1-.75-.75Zm5.5-3.25A.75.75 0 0 1 8 4h.01a.75.75 0 0 1 .75.75v3l2.25 1.35a.75.75 0 0 1-.77 1.286l-2.625-1.575A.75.75 0 0 1 7.25 8.17Z"/></svg>
          Activity <span class="count" id="navHistory">0</span>
        </button>
      </div>
      <input id="configFile" style="display:none">
    </header>
    <div class="cols">
      <!-- LEFT: collection tree -->
      <section class="pane records-pane">
        <div class="ph"><span class="lab">Records</span><span class="sp"></span><span class="ct" id="recordCt"></span></div>
        <div class="find"><input id="find" placeholder="Go to record" autocomplete="off" spellcheck="false"></div>
        <div class="filterbar" id="filters"></div>
        <div class="scroll" id="tree"><div class="spin">loading…</div></div>
      </section>
      <!-- MIDDLE: history graph -->
      <section class="pane history-pane">
        <div class="ph"><span class="lab">History</span><span class="sp"></span><span class="ct" id="histCt"></span></div>
        <div class="scroll" id="hist"><div class="ghost-pane"><div class="big">No record selected</div>Pick a record on the left to walk its history.</div></div>
      </section>
      <!-- RIGHT: paper diff -->
      <section class="pane diff-pane">
        <div class="dhead"><span class="t" id="dTitle">Diff</span><span class="sp"></span><span id="dActs"></span></div>
        <div class="paperwrap" id="diff"><div class="ghost-pane">A version or a record's drift will render here, recorded against live.</div></div>
      </section>
    </div>
  </div>
  <div class="toast" id="toast"></div>
  <div class="remedy" id="remedy"></div>
  <div class="mbg" id="mbg"><div class="modal" id="modal"></div></div>

<script>
const S={records:[],recent:[],branches:[],prs:[],filter:"all",q:"",sel:null,against:new Set(),hist:[],who:null,open:{},impact:{},prBase:"main",prHead:null,prRows:[],prSel:0,branchView:null};
const $=id=>document.getElementById(id);
const esc=v=>String(v==null?"":v).replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
// inline markdown for LLM narration: escape first (XSS-safe), then render the small
// subset the model actually emits — **bold**, *italic*, `code`. **bold** before
// *italic* so the bold markers aren't consumed by the italic rule.
const mdi=v=>esc(v)
  .replace(/\*\*([^*]+)\*\*/g,"<strong>$1</strong>")
  .replace(/(^|[^*])\*([^*\n]+)\*/g,"$1<em>$2</em>")
  .replace(/`([^`]+)`/g,'<code>$1</code>');
const fmt=v=>typeof v==="object"?JSON.stringify(v):String(v);
const dcls=s=>s==="clean"?"clean":s==="new"?"new":"drift";
const isDrift=s=>s!=="clean"&&s!=="new";

function env(){return{env:$("env").value||"dev",branch:$("branch").value||"main",config_file:$("configFile").value||null};}
function qs(){const p=new URLSearchParams(),e=env();if(e.env)p.set("env",e.env);if(e.config_file)p.set("config_file",e.config_file);return p.toString();}
async function api(action,data){const r=await fetch("/api/action",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({action,...env(),...data})});return r.json();}
function toast(msg,bad){const t=$("toast");t.innerHTML=`<span class="${bad?"bad":"ok"}">${bad?"✕":"✓"}</span>${esc(msg)}`;t.className="toast show"+(bad?" err":"");clearTimeout(t._t);t._t=setTimeout(()=>t.className="toast",2400);}
function showRemedy(nx){const r=$("remedy");if(!nx){r.className="remedy";return;}
  const cmds=(nx.commands||[]).map(c=>`<div class="rc"><code title="${esc(c)}">${esc(c)}</code><button class="cp" onclick="navigator.clipboard&&navigator.clipboard.writeText(this.previousElementSibling.textContent);this.textContent='copied'">copy</button></div>`).join("");
  r.innerHTML=`<button class="rx" onclick="$('remedy').className='remedy'">✕</button>`+
    `<div class="rw">${esc(nx.why||"")}</div><div class="rr">→ ${esc(nx.remedy||"")}</div>${cmds}`+
    (nx.docs?`<div class="rr" style="margin-top:6px">Docs: ${esc(nx.docs)}</div>`:"");
  r.className="remedy show";}
function initials(s){s=String(s||"");const at=s.indexOf("@");const h=at>0?s.slice(0,at):s;return (h.replace(/[^a-zA-Z0-9]/g,"").slice(0,2)||"··").toLowerCase();}

/* theme */
function setTheme(t){document.documentElement.dataset.theme=t;try{localStorage.setItem("cfgit-theme",t)}catch(e){}
  $("theme").querySelectorAll("button").forEach(b=>b.classList.toggle("on",b.dataset.th===t));}
$("theme").querySelectorAll("button").forEach(b=>b.onclick=()=>setTheme(b.dataset.th));
try{const sv=localStorage.getItem("cfgit-theme");if(sv)setTheme(sv);}catch(e){}

function directChild(el,cls){return [...el.children].find(c=>c.classList&&c.classList.contains(cls));}
function closeSelectMenus(except){
  document.querySelectorAll(".selectbox.open").forEach(box=>{
    if(box===except)return;
    box.classList.remove("open");
    const btn=directChild(box,"select-trigger");
    if(btn)btn.setAttribute("aria-expanded","false");
  });
}
function optionButtons(menu){return [...menu.querySelectorAll(".select-option")];}
function focusOption(menu,delta){
  const opts=optionButtons(menu);if(!opts.length)return;
  const cur=Math.max(0,opts.indexOf(document.activeElement));
  opts[(cur+delta+opts.length)%opts.length].focus();
}
function enhanceSelect(select){
  const box=select.closest(".selectbox");if(!box)return;
  select.tabIndex=-1;select.setAttribute("aria-hidden","true");
  let trigger=directChild(box,"select-trigger");
  let menu=directChild(box,"select-menu");
  if(!trigger){
    trigger=document.createElement("button");
    trigger.type="button";
    trigger.className="select-trigger";
    trigger.setAttribute("aria-haspopup","listbox");
    trigger.setAttribute("aria-expanded","false");
    box.appendChild(trigger);
  }
  if(!menu){
    menu=document.createElement("div");
    menu.className="select-menu";
    menu.setAttribute("role","listbox");
    box.appendChild(menu);
  }
  const selected=select.options[select.selectedIndex]||select.options[0];
  trigger.disabled=select.disabled;
  trigger.setAttribute("aria-label",select.title||select.id||"Select");
  trigger.innerHTML=`<span class="select-mark"></span><span class="select-value">${esc(selected?selected.textContent:"")}</span><span class="select-caret"></span>`;
  menu.innerHTML=[...select.options].map(o=>`<button type="button" class="select-option" role="option" data-value="${esc(o.value)}" aria-selected="${o.selected?"true":"false"}">
    <span class="select-check">✓</span><span class="txt">${esc(o.textContent)}</span>
  </button>`).join("");
  trigger.onclick=e=>{
    e.stopPropagation();
    const open=box.classList.contains("open");
    closeSelectMenus();
    if(!open&&!trigger.disabled){box.classList.add("open");trigger.setAttribute("aria-expanded","true");}
  };
  trigger.onkeydown=e=>{
    if(["Enter"," ","ArrowDown","ArrowUp"].includes(e.key)){
      e.preventDefault();
      closeSelectMenus(box);box.classList.add("open");trigger.setAttribute("aria-expanded","true");
      const opts=optionButtons(menu);const idx=Math.max(0,opts.findIndex(o=>o.getAttribute("aria-selected")==="true"));
      if(opts[idx])opts[idx].focus();
    } else if(e.key==="Escape"){closeSelectMenus();}
  };
  optionButtons(menu).forEach(opt=>{
    opt.onclick=e=>{
      e.stopPropagation();
      select.value=opt.dataset.value;
      closeSelectMenus();
      select.dispatchEvent(new Event("change",{bubbles:true}));
      enhanceSelect(select);
      trigger.focus();
    };
    opt.onkeydown=e=>{
      if(e.key==="ArrowDown"){e.preventDefault();focusOption(menu,1);}
      else if(e.key==="ArrowUp"){e.preventDefault();focusOption(menu,-1);}
      else if(e.key==="Enter"||e.key===" "){e.preventDefault();opt.click();}
      else if(e.key==="Escape"){e.preventDefault();closeSelectMenus();trigger.focus();}
    };
  });
}
function syncSelectMenus(root=document){
  root.querySelectorAll(".selectbox select").forEach(enhanceSelect);
}
document.addEventListener("click",e=>{if(!e.target.closest(".selectbox"))closeSelectMenus();});
document.addEventListener("keydown",e=>{if(e.key==="Escape")closeSelectMenus();});
syncSelectMenus();

async function loadState(){
  const st=await fetch("/api/state?"+qs()).then(r=>r.json()).catch(e=>({error:String(e)}));
  if(st.data&&st.data.whoami){const w=st.data.whoami;S.who=w;const id=w.identity||{};
    const disp=w.identity_display||w.author||"";
    $("whoTxt").innerHTML=`<b>${esc(disp)}</b> · ${esc(w.env||"dev")}`;
    $("ava").textContent=initials(w.author||disp);
    $("mode").textContent=w.identity_mode||id.mode||"open";
    const mel=$("mode");if(mel){mel.classList.toggle("warn",!!w.open_mode_warning);mel.title=w.open_mode_warning||"";}
    if(w.open_mode_warning&&!S._warnedOpen){S._warnedOpen=true;
      showRemedy({why:"This env writes UNAUDITED.",remedy:w.open_mode_warning,commands:[],docs:"IDENTITY_AND_ATTRIBUTION.md"});}}
  // populate env options from schema
  const sc=await fetch("/api/schema?"+qs()).then(r=>r.json()).catch(()=>null);
  if(sc&&sc.data&&Array.isArray(sc.data.envs)&&sc.data.envs.length){
    const cur=$("env").value; $("env").innerHTML=sc.data.envs.map(e=>`<option ${e===cur?"selected":""}>${esc(e)}</option>`).join("");}
  if(sc&&sc.data&&sc.data.project)$("projectName").textContent=sc.data.project;
  if(sc&&sc.data&&sc.data.impact)S.impact=sc.data.impact;
  S.records=(st.data&&st.data.status)?st.data.status:[];
  S.recent=(st.data&&st.data.recent_history)?st.data.recent_history:[];
  S.branches=(st.data&&st.data.branches)?st.data.branches:[];
  S.prs=(st.data&&st.data.prs)?st.data.prs:[];
  renderRepoSummary();
  renderBranches();
  // default: open every collection that has drift, else open first
  const colls=[...new Set(S.records.map(r=>r.collection))];
  if(Object.keys(S.open).length===0){colls.forEach(c=>{S.open[c]=S.records.some(r=>r.collection===c&&isDrift(r.state));});
    if(!Object.values(S.open).some(Boolean)&&colls[0])S.open[colls[0]]=true;}
  renderFilters();renderTree();
  if(!S.sel&&!document.querySelector(".app")?.classList.contains("pr-mode")&&!document.querySelector(".app")?.classList.contains("branches-mode"))renderRecentHistory();
}

function branchNames(){return [...new Set((S.branches.length?S.branches.map(b=>b.name):["main"]).filter(Boolean))];}
function defaultBranch(){const names=branchNames();return names.includes("main")?"main":(names[0]||"main");}
function draftBranches(){const def=defaultBranch();return branchNames().filter(n=>n!==def);}

function renderBranches(){
  const sel=$("branch"); if(!sel)return;
  const current=sel.value||"main";
  const names=branchNames();
  sel.innerHTML=names.map(n=>`<option ${n===current?"selected":""}>${esc(n)}</option>`).join("");
  if(names.includes(current))sel.value=current; else sel.value=names[0]||"main";
  const onMain=sel.value===defaultBranch();
  const hasOpenPr=S.prs.some(p=>p.head_branch===sel.value&&p.status==="open");
  const branchState=$("branchState");
  if(branchState){
    branchState.textContent=onMain?"runtime snapshot":(hasOpenPr?"PR snapshot":"draft snapshot");
    branchState.classList.toggle("open",hasOpenPr);
  }
  $("draftCommit").disabled=onMain||!S.sel;
  $("branchDiff").disabled=onMain;
  $("openPr").disabled=onMain;
  $("mergePr").disabled=onMain||!hasOpenPr;
  $("draftCommit").title=onMain?"Select a draft branch first":(!S.sel?"Select a record first":"Commit selected record into this branch view");
  $("branchDiff").title=onMain?"Select a draft branch to compare with main":"Compare this branch snapshot with main";
  $("openPr").textContent=hasOpenPr?"View PR":"Prepare PR";
  $("openPr").title=onMain?"Select a draft branch first":(hasOpenPr?"Review the open pull request comparison":"Prepare a pull request comparison; opening it is explicit in the PR tab");
  $("mergePr").title=onMain?"Select a draft branch first":(hasOpenPr?"Merge the open cfgit PR":"No open PR for this branch");
  syncSelectMenus(document.querySelector(".repo-actions"));
}

function counts(){const c={all:S.records.length,drift:0,clean:0,new:0};
  for(const r of S.records){if(r.state==="clean")c.clean++;else if(r.state==="new")c.new++;else c.drift++;}return c;}
function renderRepoSummary(){
  const c=counts();
  const openPrs=S.prs.filter(p=>p.status==="open").length;
  const set=(id,html)=>{const el=$(id);if(el)el.innerHTML=html;};
  set("repoStats",`<span><b>${c.all}</b> records</span><span><b>${c.drift}</b> changed</span><span><b>${openPrs}</b> PRs</span>`);
  set("navRecords",String(c.all));
  set("navBranchesCount",String(branchNames().length));
  set("navPrCount",String(openPrs));
  set("navHistory",String((S.recent||[]).length));
  set("recordCt",`${c.drift} changed`);
}
function setNav(active){
  ["navRecordsTab","navBranches","navPr","navHistoryTab"].forEach(id=>{const el=$(id);if(el)el.classList.toggle("on",id===active);});
  const app=document.querySelector(".app");
  if(app){
    app.classList.toggle("activity-mode",active==="navHistoryTab");
    app.classList.toggle("pr-mode",active==="navPr");
    app.classList.toggle("branches-mode",active==="navBranches");
  }
  const histLab=document.querySelector(".history-pane .ph .lab");
  if(histLab)histLab.textContent=active==="navPr"?"Pull requests":active==="navBranches"?"Branches":"History";
}
function renderFilters(){const c=counts();
  const d=[["all","All",c.all],["drift","Changed",c.drift],["clean","Unchanged",c.clean],["new","New",c.new]];
  $("filters").innerHTML=d.map(([k,l,n])=>`<button class="fchip ${S.filter===k?"on":""}" data-f="${k}">${l}<span class="n">${n}</span></button>`).join("");
  $("filters").querySelectorAll(".fchip").forEach(b=>b.onclick=()=>{S.filter=b.dataset.f;renderTree();});}

function visibleRecords(){let rs=S.records.slice();
  if(S.filter==="drift")rs=rs.filter(r=>isDrift(r.state));
  else if(S.filter==="clean")rs=rs.filter(r=>r.state==="clean");
  else if(S.filter==="new")rs=rs.filter(r=>r.state==="new");
  if(S.q){const q=S.q.toLowerCase();rs=rs.filter(r=>(r.collection+":"+r.record_id).toLowerCase().includes(q));}
  return rs;}

const COLL_IC=`<svg class="coll-ic" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.4"><ellipse cx="8" cy="3.7" rx="5.3" ry="2.1"/><path d="M2.7 3.7v8.6c0 1.16 2.37 2.1 5.3 2.1s5.3-.94 5.3-2.1V3.7"/><path d="M2.7 8c0 1.16 2.37 2.1 5.3 2.1s5.3-.94 5.3-2.1"/></svg>`;
function renderTree(){
  const rs=visibleRecords();const el=$("tree");
  if(!rs.length){el.innerHTML=`<div class="empty">No records match.</div>`;return;}
  const byColl={};for(const r of rs){(byColl[r.collection]=byColl[r.collection]||[]).push(r);}
  let html="";
  for(const coll of Object.keys(byColl).sort()){
    const recs=byColl[coll].sort((a,b)=>{const o={changed_outside_cfgit:0,new:1,clean:2};return (o[a.state]??0)-(o[b.state]??0)||a.record_id.localeCompare(b.record_id);});
    const drifted=recs.filter(r=>isDrift(r.state)).length;
    const open=S.open[coll]!==false&&!!S.open[coll]||S.open[coll]===true;
    html+=`<div class="coll ${S.open[coll]?"open":""}" data-c="${esc(coll)}">
      <div class="coll-h">
        <span class="tw">▶</span>${COLL_IC}
        <span class="coll-nm">${esc(coll)}</span>
        ${drifted?`<span class="coll-warn" title="${drifted} drifted"></span>`:""}
        <span class="coll-ct">${recs.length}</span>
      </div>
      <div class="docs">${recs.map(r=>{
        const key=r.collection+":"+r.record_id;const sel=S.sel===key?"sel":"";
        const ctx=S.against.has(key)?"ctx":"";
        const right=r.state==="clean"?`<span class="rt">@${r.head_seq??""}</span>`
          :r.state==="new"?`<span class="tag new">new</span>`:`<span class="tag drift">drift</span>`;
        const slash=r.record_id.lastIndexOf("/");
        const nmHtml=slash>=0
          ? `<span class="pre">${esc(r.record_id.slice(0,slash+1))}</span><span class="leaf">${esc(r.record_id.slice(slash+1))}</span>`
          : `<span class="leaf">${esc(r.record_id)}</span>`;
        return `<div class="doc ${sel} ${ctx}" data-k="${esc(key)}" title="${esc(r.record_id)}">
          <span class="ckx">${S.against.has(key)?"✓":"+"}</span>
          <span class="st ${dcls(r.state)}"></span>
          <span class="nm">${nmHtml}</span>${right}</div>`;}).join("")}</div></div>`;
  }
  el.innerHTML=html;
  el.querySelectorAll(".coll-h").forEach(h=>h.onclick=()=>{const c=h.parentElement.dataset.c;S.open[c]=!S.open[c];renderTree();});
  el.querySelectorAll(".doc").forEach(d=>d.onclick=e=>{
    e.stopPropagation();
    // the ✓/+ marker, or cmd/ctrl-click, toggles the record into the impact context.
    // a plain click selects it as the primary (drives diff/history).
    const onMarker=e.target.classList&&e.target.classList.contains("ckx");
    if(onMarker||e.metaKey||e.ctrlKey){toggleContext(d.dataset.k);}
    else{selectRecord(d.dataset.k);}
  });
}

function renderRecentHistory(){
  setNav("navHistoryTab");
  const drift=S.records.filter(r=>isDrift(r.state));
  const recent=S.recent||[];
  $("histCt").textContent=recent.length?`${recent.length} recent`:"";
  let h=`<div class="selhdr"><div class="nm">Recent activity</div>
    <div class="meta"><span class="mono" style="color:var(--faint)">all configured records</span>
    ${drift.length?`<span class="tag drift">${drift.length} live drift</span>`:`<span class="tag" style="color:var(--moss);background:var(--moss-bg)">no drift</span>`}</div></div>
    <div class="rail">`;
  if(drift.length){
    for(const r of drift){
      const key=r.collection+":"+r.record_id;
      h+=`<div class="node live recent" data-k="${esc(key)}"><div class="line"></div><div class="mk"></div>
        <div class="msg">${esc(r.record_id)}</div>
        <div class="sub"><span class="op">drift</span><span>${esc(r.collection)}</span><span>live differs from @${esc(r.head_seq??"HEAD")}</span></div></div>`;
    }
  }
  for(const e of recent){
    const key=e.collection+":"+e.record_id;
    const sh=(e.oid||"").slice(0,7);const when=(e.recorded_at||"").replace("T"," ").slice(0,16);const op=e.op||"commit";
    h+=`<div class="node ${opClass(op)} recent" data-k="${esc(key)}" data-seq="${e.seq}"><div class="line"></div><div class="mk"></div>
      <div class="msg">${esc(e.record_id)}</div>
      <div class="sub"><span class="op r-${op}">${esc(op)}</span><span>${esc(e.collection)}</span><span>@${e.seq}</span><span>${esc(sh)}</span><span>${esc(e.author||"")}</span>${when?`<span>${esc(when)}</span>`:""}</div>
      ${e.message?`<div class="sub" style="margin-top:3px">${esc(e.message)}</div>`:""}</div>`;
  }
  if(!drift.length&&!recent.length){
    h+=`<div class="ghost-pane"><div class="big">No recorded activity yet</div>Run an import or commit and the latest changes will appear here.</div>`;
  }
  h+=`</div>`;
  $("hist").innerHTML=h;
  $("dTitle").textContent="Diff";
  $("dActs").innerHTML="";
  $("diff").innerHTML=`<div class="ghost-pane">Select a recent entry or a record to inspect its diff.</div>`;
  $("hist").querySelectorAll(".node.recent").forEach(n=>n.onclick=()=>selectRecord(n.dataset.k));
}

function toggleContext(key){
  if(S.against.has(key))S.against.delete(key); else S.against.add(key);
  // the primary record is implicitly its own diff subject; don't also keep it in `against`
  S.against.delete(S.sel);
  renderTree();
  refreshImpactBtn();
}
function refreshImpactBtn(){
  const b=$("aImpact"); if(!b)return;
  const n=[...S.against].filter(k=>k!==S.sel).length;
  b.textContent=n?`Analyze impact (${n})`:"Analyze impact";
  b.title=n?`Reason this change against ${n} selected record(s)`:"Reason this change against the whole system";
}

async function selectRecord(key){
  setNav("navRecordsTab");
  S.sel=key;renderBranches();renderTree();
  const rec=S.records.find(r=>r.collection+":"+r.record_id===key);
  $("hist").innerHTML=`<div class="spin">loading history…</div>`;
  $("diff").innerHTML=`<div class="spin">…</div>`;$("dActs").innerHTML="";$("dTitle").textContent="Diff";
  const res=await api("log",{record:key,limit:60});
  S.hist=(res&&Array.isArray(res.data))?res.data:(res&&res.data&&Array.isArray(res.data.entries))?res.data.entries:[];
  renderHistory(rec);
  if(rec&&isDrift(rec.state))showDrift(rec);
  else if(S.hist.length)selectNode(S.hist[0]);
  else $("diff").innerHTML=`<div class="ghost-pane">No versions yet. This record is <b>${esc(rec?rec.state:"")}</b>.</div>`;
}

function opClass(op){return op==="restore"?"restore":op==="adopt"?"adopt":op==="import"?"importt":"commit";}
function renderHistory(rec){
  const drift=rec&&isDrift(rec.state);
  $("histCt").textContent=S.hist.length?`${S.hist.length} version${S.hist.length>1?"s":""}`:"";
  let h=`<div class="selhdr"><div class="nm">${esc(rec?rec.record_id:S.sel)}</div>
    <div class="meta"><span class="mono" style="color:var(--faint)">${esc(rec?rec.collection:"")}</span>
    ${rec?`<span class="tag ${rec.state==="clean"?"":dcls(rec.state)}" style="${rec.state==="clean"?"color:var(--moss);background:var(--moss-bg)":""}">${rec.state==="clean"?"clean":rec.state==="new"?"new":"drift"}</span>`:""}</div></div>
    <div class="rail">`;
  if(drift){h+=`<div class="node live" data-live="1"><div class="line"></div><div class="mk"></div>
    <div class="msg">Live now — edited outside cfgit</div>
    <div class="sub"><span class="op">live</span><span>uncommitted change in the database</span></div></div>`;}
  for(const e of S.hist){const sh=(e.oid||"").slice(0,7);const when=(e.recorded_at||"").replace("T"," ").slice(0,16);const op=e.op||"commit";
    h+=`<div class="node ${opClass(op)}" data-seq="${e.seq}"><div class="line"></div><div class="mk"></div>
      <div class="msg">${esc(e.message||"(no message)")}</div>
      <div class="sub"><span class="op r-${op}">${esc(op)}</span><span>@${e.seq}</span><span>${esc(sh)}</span><span>${esc(e.author||"")}</span>${when?`<span>${esc(when)}</span>`:""}</div></div>`;}
  h+=`</div>`;
  $("hist").innerHTML=h;
  $("hist").querySelectorAll(".node").forEach(n=>{
    if(n.dataset.live)n.onclick=()=>showDrift(rec);
    else{const sq=+n.dataset.seq;n.onclick=()=>selectNode(S.hist.find(x=>x.seq===sq));}});
}
function markNode(seq,live){$("hist").querySelectorAll(".node").forEach(n=>{
  n.classList.toggle("sel",live?!!n.dataset.live:(+n.dataset.seq===seq));});}

async function showDrift(rec){
  markNode(null,true);
  S.diffCtx={a:"=HEAD",b:"=live",left:"recorded",right:"live",empty:"No structural difference (it may be in ignored or secret fields)."};
  $("dTitle").innerHTML=`Drift · recorded <b>@${rec.head_seq??""}</b> → live`;
  $("dActs").innerHTML=`<button class="btn" id="aImpact">Analyze impact</button> <button class="btn go" id="aAdopt">Adopt live change</button>`;
  $("diff").innerHTML=`<div class="spin">computing diff…</div>`;
  const res=await api("diff",{record:S.sel,a:"=HEAD",b:"=live"});
  renderDiff(res,"recorded","live",S.diffCtx.empty);
  $("aImpact").onclick=()=>showImpact();
  $("aAdopt").onclick=()=>openAdopt(rec);
  refreshImpactBtn();
}
async function showImpact(){
  if(!S.diffCtx)return;
  const against=[...S.against].filter(k=>k!==S.sel);
  const btn=$("aImpact"); if(btn){btn.disabled=true;btn.textContent="Analyzing…";}
  const payload={record:S.sel,a:S.diffCtx.a,b:S.diffCtx.b,use_llm:true};
  if(S.impact&&S.impact.provider)payload.provider=S.impact.provider;
  if(S.impact&&S.impact.model)payload.model=S.impact.model;
  if(against.length)payload.against=against;
  const r=await api("impact",payload);
  if(btn){btn.disabled=false;}
  refreshImpactBtn();
  const d=r&&r.data?r.data:{};
  const risk=(d.risk_level||"medium").toLowerCase();
  const cats=(d.categories||[]).map(c=>`<span class="cat">${esc(c)}</span>`).join(" ")||`<span class="off">none</span>`;
  const aff=(d.affected_records||[]);
  const affHtml=aff.length?aff.map(a=>`<span class="aff">${esc(typeof a==="string"?a:(a.record_id||JSON.stringify(a)))}</span>`).join(", ")
    :`<span class="off">none found by static scan</span>`;
  const llm=d.llm||{};
  let llmHtml;
  if(llm.enabled){
    const ov=llm.overview||{};
    const parts=[];
    const objLine=x=>{
      const label=x.config_id||x.record_id||x.id||x.name||x.collection||"record";
      const reason=x.reason||x.summary||x.impact||x.note||"";
      return `<strong>${esc(label)}</strong>${reason?` — ${mdi(reason)}`:""}`;
    };
    const asList=v=>Array.isArray(v)?`<ul class="llmul">${v.map(x=>`<li>${x&&typeof x==="object"?objLine(x):mdi(x)}</li>`).join("")}</ul>`:mdi(v);
    if(ov.summary)parts.push(`<div class="body">${mdi(ov.summary)}</div>`);
    if(ov.behavior_change)parts.push(`<div class="lk"><span class="lkk">behavior</span>${asList(ov.behavior_change)}</div>`);
    if(ov.blast_radius)parts.push(`<div class="lk"><span class="lkk">blast radius</span>${asList(ov.blast_radius)}</div>`);
    if(ov.unknowns&&(Array.isArray(ov.unknowns)?ov.unknowns.length:true))parts.push(`<div class="lk"><span class="lkk">unknowns</span>${asList(ov.unknowns)}</div>`);
    const fallback=(!parts.length&&(llm.text||llm.narration))?`<div class="body">${esc(llm.text||llm.narration)}</div>`:"";
    llmHtml=`<div class="llm"><div class="who">${esc(llm.provider||"llm")} · ${esc(llm.model||"")} narration</div>${parts.join("")||fallback||`<div class="off">no narration returned</div>`}</div>`;
  } else {
    llmHtml=`<div class="llm"><div class="who">LLM narration</div><div class="off">off — enable <span class="mono">[connections]</span> in .cfg.toml for a written explanation of what this change does to the system. Everything above is computed locally, no data leaves your machine.</div></div>`;
  }
  const scopedAgainst=[...S.against].filter(k=>k!==S.sel);
  const scopeRow=scopedAgainst.length
    ? `<div class="row"><span class="k">reasoned vs</span>${scopedAgainst.map(k=>`<span class="cat">${esc(k.split(":").pop())}</span>`).join(" ")}</div>`
    : `<div class="row"><span class="k">reasoned vs</span><span class="off">whole system (select records on the left to scope)</span></div>`;
  const panel=`<div class="impact">
    <div class="ih"><span class="tt">System impact</span><span class="sp"></span><span class="risk ${risk}">${esc(risk)} risk</span></div>
    <div class="ib">
      <div class="sum">${esc(d.summary||"")}</div>
      ${scopeRow}
      <div class="row"><span class="k">changed</span>${(d.changed_paths||[]).map(p=>`<span class="cat">${esc(p)}</span>`).join(" ")||`<span class="off">—</span>`}</div>
      <div class="row"><span class="k">categories</span>${cats}</div>
      <div class="row"><span class="k">affects</span>${affHtml}</div>
      ${(d.declared_links_changed&&d.declared_links_changed.length)?`<div class="row"><span class="k">links changed</span>${d.declared_links_changed.map(l=>`<span class="cat">${esc(typeof l==="string"?l:JSON.stringify(l))}</span>`).join(" ")}</div>`:""}
      ${d.rollback_note?`<div class="note">↩ ${esc(d.rollback_note)}</div>`:""}
      ${llmHtml}
    </div></div>`;
  // prepend the panel above the existing paper diff
  const wrap=$("diff");
  const existing=wrap.querySelector(".impact"); if(existing)existing.remove();
  wrap.insertAdjacentHTML("afterbegin",panel);
}
async function selectNode(e){
  if(!e)return;markNode(e.seq,false);
  const isHead=S.hist.length&&e.seq===S.hist[0].seq;
  const idx=S.hist.findIndex(x=>x.seq===e.seq);
  const parent=idx>=0&&idx<S.hist.length-1?S.hist[idx+1]:null;
  $("dTitle").innerHTML=`Version <b>@${e.seq}</b> · ${esc((e.oid||"").slice(0,7))}`;
  const acts=[];
  if(parent)acts.push(`<button class="btn" id="aImpact">Analyze impact</button>`);
  if(!isHead)acts.push(`<button class="btn warn" id="aRestore">Restore version</button>`);
  $("dActs").innerHTML=acts.join(" ");
  $("diff").innerHTML=`<div class="spin">loading…</div>`;
  if(parent){S.diffCtx={a:"@"+parent.seq,b:"@"+e.seq,left:"@"+parent.seq,right:"@"+e.seq};
    const res=await api("diff",{record:S.sel,a:"@"+parent.seq,b:"@"+e.seq});
    renderDiff(res,"@"+parent.seq,"@"+e.seq,"No field-level change from the parent version.");}
  else{S.diffCtx=null;const res=await api("show",{record:S.sel,ref:"@"+e.seq});renderDoc(res);}
  const ib=$("aImpact");if(ib)ib.onclick=()=>showImpact();
  const rb=$("aRestore");if(rb)rb.onclick=()=>openRestore(S.records.find(r=>r.collection+":"+r.record_id===S.sel),"@"+e.seq);
  refreshImpactBtn();
}

function changesFrom(res){if(!res||!res.data)return null;const d=res.data;
  if(Array.isArray(d.changes))return d.changes;if(Array.isArray(d))return d;return null;}
// LCS line diff -> ops array of {t:'ctx'|'del'|'add', l:leftLine|null, r:rightLine|null}
function lineDiff(aStr,bStr){
  const a=String(aStr).split("\n"), b=String(bStr).split("\n");
  const n=a.length,m=b.length;
  // LCS table (lengths capped for safety; these strings are config text, fine)
  const dp=Array.from({length:n+1},()=>new Uint32Array(m+1));
  for(let i=n-1;i>=0;i--)for(let j=m-1;j>=0;j--)
    dp[i][j]=a[i]===b[j]?dp[i+1][j+1]+1:Math.max(dp[i+1][j],dp[i][j+1]);
  const ops=[];let i=0,j=0;
  while(i<n&&j<m){
    if(a[i]===b[j]){ops.push({t:"ctx",l:a[i],r:b[j]});i++;j++;}
    else if(dp[i+1][j]>=dp[i][j+1]){ops.push({t:"del",l:a[i],r:null});i++;}
    else{ops.push({t:"add",l:null,r:b[j]});j++;}
  }
  while(i<n){ops.push({t:"del",l:a[i],r:null});i++;}
  while(j<m){ops.push({t:"add",l:null,r:b[j]});j++;}
  return ops;
}
// number each op with its left/right line number, then collapse long unchanged runs
// into a fold that REMEMBERS its hidden ops so the user can expand them (git-style).
const FOLD_PAD=3, FOLD_STEP=10;   // context kept around changes; lines revealed per expand click
function numberOps(ops){
  let ln=0,rn=0;
  for(const o of ops){
    if(o.t==="ctx"){o.ln=++ln;o.rn=++rn;}
    else if(o.t==="del"){o.ln=++ln;o.rn=null;}
    else{o.ln=null;o.rn=++rn;}
  }
  return ops;
}
function foldContext(ops){
  const out=[];const keep=new Array(ops.length).fill(false);
  for(let k=0;k<ops.length;k++) if(ops[k].t!=="ctx") for(let d=-FOLD_PAD;d<=FOLD_PAD;d++){const idx=k+d;if(idx>=0&&idx<ops.length)keep[idx]=true;}
  let i=0;
  while(i<ops.length){
    if(keep[i]){out.push(ops[i]);i++;}
    else{let j=i;while(j<ops.length&&!keep[j])j++;out.push({t:"fold",hidden:ops.slice(i,j)});i=j;}
  }
  return out;
}
function lineRowHtml(o){
  if(o.t==="ctx")return `<div class="drow"><div class="dcell l ctx"><span class="gut">${o.ln}</span><span class="sign"> </span><span class="tx">${esc(o.l)}</span></div><div class="dcell r ctx"><span class="gut">${o.rn}</span><span class="sign"> </span><span class="tx">${esc(o.r)}</span></div></div>`;
  if(o.t==="del")return `<div class="drow"><div class="dcell l del"><span class="gut">${o.ln}</span><span class="sign">−</span><span class="tx">${esc(o.l)}</span></div><div class="dcell r void"></div></div>`;
  return `<div class="drow"><div class="dcell l void"></div><div class="dcell r add"><span class="gut">${o.rn}</span><span class="sign">+</span><span class="tx">${esc(o.r)}</span></div></div>`;
}
let _foldSeq=0;
const _folds={};   // id -> hidden ops, for expand-in-place
// the expand controls for one fold; `bare` returns just the buttons (for the
// merged sticky header), otherwise they're wrapped in their own scrolling row.
function foldControls(hidden){
  const id="fold"+(++_foldSeq);_folds[id]=hidden;const n=hidden.length;
  const up=`<button class="fx" data-fold="${id}" data-dir="up" title="expand above">↑</button>`;
  const dn=`<button class="fx" data-fold="${id}" data-dir="down" title="expand below">↓</button>`;
  const all=`<button class="fx" data-fold="${id}" data-dir="all">expand ${n} unchanged</button>`;
  return {id, html:`${n>FOLD_STEP*2?up:""}${all}${n>FOLD_STEP*2?dn:""}`};
}
function foldRowHtml(hidden){
  const c=foldControls(hidden);
  return `<div class="drow foldrow" data-foldid="${c.id}"><div class="foldbar">${c.html}</div></div>`;
}
// Returns {lead, body}. If the diff opens on unchanged lines, `lead` holds that
// first fold's expand controls so the caller can fuse them into the sticky field
// header (one bar, and the control stays pinned). `body` is the row grid.
function splitDiffHtml(before,after){
  const ops=foldContext(numberOps(lineDiff(before,after)));
  let lead="";let start=0;
  if(ops.length&&ops[0].t==="fold"){const c=foldControls(ops[0].hidden);lead=`<span class="leadfold" data-foldid="${c.id}">${c.html}</span>`;start=1;}
  const rows=ops.slice(start).map(o=>o.t==="fold"?foldRowHtml(o.hidden):lineRowHtml(o)).join("");
  return {lead, body:`<div class="splitgrid">${rows}</div>`};
}
// expand a fold: replace it (or reveal a chunk and keep a smaller fold)
function expandFold(id,dir){
  const hidden=_folds[id];if(!hidden)return;
  let reveal,rest;
  if(dir==="all"||hidden.length<=FOLD_STEP){reveal=hidden;rest=null;}
  else if(dir==="up"){reveal=hidden.slice(0,FOLD_STEP);rest=hidden.slice(FOLD_STEP);}
  else{reveal=hidden.slice(hidden.length-FOLD_STEP);rest=hidden.slice(0,hidden.length-FOLD_STEP);}
  // leading fold lives in the sticky header: reveal lines at the TOP of the body,
  // and keep any remainder as a fresh leading control in the header (still pinned).
  const lead=document.querySelector(`.leadfold[data-foldid="${id}"]`);
  if(lead){
    const grid=lead.closest(".frow").querySelector(".splitgrid");
    if(grid)grid.insertAdjacentHTML("afterbegin",reveal.map(lineRowHtml).join(""));
    if(rest&&rest.length){const c=foldControls(rest);lead.dataset.foldid=c.id;lead.innerHTML=c.html;}
    else{const wrap=lead.closest(".fhx");if(wrap)wrap.remove();else lead.remove();}
    bindFolds();return;
  }
  const row=document.querySelector(`.foldrow[data-foldid="${id}"]`);
  if(!row)return;
  let html=reveal.map(lineRowHtml).join("");
  // a fold expanded from one side keeps the remaining hidden lines as a new fold on the other side
  if(rest&&rest.length){const restHtml=foldRowHtml(rest);
    html=dir==="up"?html+restHtml:restHtml+html;}
  row.outerHTML=html;
  bindFolds();
}
function bindFolds(){document.querySelectorAll(".fx").forEach(b=>b.onclick=e=>{e.stopPropagation();expandFold(b.dataset.fold,b.dataset.dir);});}
function isLongText(v){return typeof v==="string"&&(v.length>120||v.indexOf("\n")>=0);}
function diffRowsHtml(ch){
  let rows="";
  for(const c of ch){const f=c.path||c.field||c.key||"";
    let before=("before"in c)?c.before:c.old, after=("after"in c)?c.after:c.new;
    const op=c.op||((before==null)?"add":(after==null)?"remove":"change");
    // long multi-line text on both sides -> git-style line-aligned split.
    // the leading fold's expand control rides inside the sticky field-name bar,
    // so the header and the "expand N unchanged" control are one pinned bar.
    if(op==="change"&&(isLongText(before)||isLongText(after))){
      const sp=splitDiffHtml(before,after);
      const lead=sp.lead?`<span class="fhx">${sp.lead}</span>`:"";
      rows+=`<div class="frow"><div class="fname"><span class="fnm">${esc(f)}</span>${lead}</div>${sp.body}</div>`;
      continue;
    }
    const lcls=op==="add"?"void":"del", rcls=op==="remove"?"void":"add";
    const lval=op==="add"?"":esc(fmt(before)), rval=op==="remove"?"":esc(fmt(after));
    rows+=`<div class="frow"><div class="fname">${esc(f)}</div><div class="fpair">
      <div class="fside ${lcls}">${lval}</div><div class="fside r ${rcls}">${rval}</div></div></div>`;}
  return rows;
}
function diffPaperHtml(ch,leftLabel,rightLabel,emptyMsg,recordLabel){
  if(!ch.length)return `<div class="paper"><div class="nodiff">${esc(emptyMsg||"No changes.")}</div></div>`;
  const record=recordLabel?`<div class="record-title"><span class="op">${ch.length} field${ch.length===1?"":"s"}</span>${esc(recordLabel)}</div>`:"";
  return `<div class="paper">
    <div class="paper-h"><div class="l"><span class="swatch"></span>${esc(leftLabel)}</div><div class="r"><span class="swatch"></span>${esc(rightLabel)}</div></div>
    ${record}${diffRowsHtml(ch)}</div>`;
}
function renderDiff(res,leftLabel,rightLabel,emptyMsg){
  const ch=changesFrom(res);
  if(!ch){const txt=res&&res.data&&res.data.text?res.data.text:JSON.stringify(res,null,2);
    $("diff").innerHTML=`<div class="paper doconly"><div class="docbody">${esc(txt)}</div></div>`;return;}
  $("diff").innerHTML=diffPaperHtml(ch,leftLabel,rightLabel,emptyMsg);
  bindFolds();
}
function renderDoc(res){const d=res&&res.data?res.data:res;const doc=d&&d.doc?d.doc:(d&&d.text?d.text:d);
  const txt=(typeof doc==="string")?doc:JSON.stringify(doc,null,2);
  $("diff").innerHTML=`<div class="paper doconly"><div class="paper-h"><div>document</div></div><div class="docbody">${esc(txt)}</div></div>`;}

/* modals */
function modal(html){$("modal").innerHTML=html;$("mbg").classList.add("show");syncSelectMenus($("modal"));}
function closeModal(){$("mbg").classList.remove("show");}
$("mbg").addEventListener("click",e=>{if(e.target===$("mbg"))closeModal();});
function openAdopt(rec){modal(`<h3>Adopt live change</h3><div class="b">
  <div class="desc">Fold the current live value of <b>${esc(rec.record_id)}</b> into history as a new version, with attribution. The drift becomes a recorded commit.</div>
  <div><label>Reason</label><input id="mMsg" value="adopt out-of-band edit" autocomplete="off"></div></div>
  <div class="f"><button class="btn" onclick="closeModal()">Cancel</button><button class="btn go" id="mGo">Adopt live change</button></div>`);
  $("mGo").onclick=async()=>{$("mGo").disabled=true;const r=await api("adopt",{record:S.sel,message:$("mMsg").value||"adopt"});after(r,"Adopted");};}
function openRestore(rec,ref){modal(`<h3>Restore ${esc(ref)}</h3><div class="b">
  <div class="desc">Re-apply the <b>${esc(ref)}</b> version of <b>${esc(rec?rec.record_id:S.sel)}</b> as a new version on top. Nothing is lost — restore is non-destructive.</div>
  <div><label>Reason</label><input id="mMsg" value="restore ${esc(ref)}" autocomplete="off"></div></div>
  <div class="f"><button class="btn" onclick="closeModal()">Cancel</button><button class="btn warn" id="mGo">Restore</button></div>`);
  $("mGo").onclick=async()=>{$("mGo").disabled=true;const r=await api("restore",{record:S.sel,ref:ref,message:$("mMsg").value||("restore "+ref)});after(r,"Restored");};}
function selectedBranch(){return $("branch").value||"main";}
function openCreateBranch(){modal(`<h3>Create branch</h3><div class="b">
  <div class="desc">Create a draft branch. This writes only cfgit branch metadata and does not mutate runtime.</div>
  <div><label>Name</label><input id="mName" value="draft-${Date.now().toString(36)}" autocomplete="off"></div>
  <div><label>Message</label><input id="mMsg" value="create draft branch" autocomplete="off"></div></div>
  <div class="f"><button class="btn" onclick="closeModal()">Cancel</button><button class="btn go" id="mGo">Create</button></div>`);
  $("mGo").onclick=async()=>{$("mGo").disabled=true;const name=$("mName").value;const r=await api("branch_create",{name,from_branch:defaultBranch(),message:$("mMsg").value});afterBranch(r,"Branch created",name);};}
async function openDraftCommit(){
  const br=selectedBranch();
  if(br===defaultBranch()){toast("select a non-main branch",true);return;}
  if(!S.sel){toast("select a record first",true);return;}
  const live=await api("show",{record:S.sel,ref:"live"});
  const doc=live&&live.data&&live.data.doc?live.data.doc:{};
  modal(`<h3>Draft commit to ${esc(br)}</h3><div class="b">
    <div class="desc">Edit the JSON for <b>${esc(S.sel)}</b>. This stores a branch commit only; runtime changes only after PR merge.</div>
    <div><label>Message</label><input id="mMsg" value="draft ${esc(S.sel)}" autocomplete="off"></div>
    <div><label>Document JSON</label><textarea id="mDoc" spellcheck="false">${esc(JSON.stringify(doc,null,2))}</textarea></div></div>
    <div class="f"><button class="btn" onclick="closeModal()">Cancel</button><button class="btn go" id="mGo">Commit Draft</button></div>`);
  $("mGo").onclick=async()=>{
    $("mGo").disabled=true;
    let doc;
    try{doc=JSON.parse($("mDoc").value);}catch(e){toast("invalid JSON: "+e.message,true);$("mGo").disabled=false;return;}
    const r=await api("commit",{record:S.sel,doc,branch:br,message:$("mMsg").value||"draft"});
    afterBranch(r,"Draft committed");
  };
}
async function showBranchDiff(){
  const br=selectedBranch();
  const base=defaultBranch();
  if(br===base){toast("select a non-main branch",true);return;}
  openCompareWorkspace(base,br);
}
function branchRef(name){return S.branches.find(b=>b.name===name)||null;}
function branchBadge(name){
  const def=defaultBranch();
  if(name===def)return `<span class="tag" style="color:var(--moss);background:var(--moss-bg)">default</span>`;
  const pr=prOpenForHead(name);
  return pr?`<span class="tag" style="color:var(--moss);background:var(--moss-bg)">open PR</span>`:`<span class="op">draft</span>`;
}
function openCompareWorkspace(base,head){
  const def=defaultBranch();
  S.prBase=base||def;
  S.prHead=head||draftBranches()[0]||"";
  if(S.prHead)syncTopBranch(S.prHead);
  renderPrWorkspace();
}
function renderBranchesWorkspace(){
  setNav("navBranches");
  const names=branchNames();
  const def=defaultBranch();
  const cur=selectedBranch();
  const drafts=draftBranches();
  const selectedHead=(cur!==def&&names.includes(cur))?cur:(drafts[0]||"");
  $("histCt").textContent=`${names.length} branch${names.length===1?"":"es"}`;
  $("dActs").innerHTML="";
  $("dTitle").textContent="Branches";
  const rows=names.map(name=>{
    const ref=branchRef(name)||{};
    const pr=prOpenForHead(name);
    const isDef=name===def;
    const updated=(ref.updated_at||ref.created_at||"").replace("T"," ").slice(0,16);
    const head=(ref.head_commit_id||"").slice(0,12);
    const model=isDef?"full runtime snapshot":"main + draft overlay";
    return `<div class="branchrow ${isDef?"default":"draft"}" data-branch="${esc(name)}">
      <div class="branch-id">
        <div class="branch-main"><span class="branch-dot"></span><span class="branch-name">${esc(name)}</span>${branchBadge(name)}</div>
        <div class="branch-sub">
          ${head?`<span>${esc(head)}</span>`:""}
          ${ref.author?`<span>${esc(ref.author)}</span>`:""}
          <span>${esc(model)}</span>
        </div>
      </div>
      <div class="muted">${updated?`updated ${esc(updated)}`:"runtime branch"}</div>
      <div class="prcell">${pr?`PR ${esc(pr.id)}`:"-"}</div>
      <div class="branch-actions">
        ${isDef?`<button class="btn" data-branch-new="${esc(name)}">New branch</button>`:`<button class="btn" data-branch-compare="${esc(name)}">${pr?"Review":"Compare"}</button><button class="btn warn" data-branch-delete="${esc(name)}">Delete</button>`}
      </div>
    </div>`;
  }).join("");
  $("hist").innerHTML=`<div class="prwork">
    <div class="prbox primary">
      <div class="hd"><span class="ttl">Compare branches</span><span class="sp"></span><span class="op">preview</span></div>
      <div class="bd">
        ${drafts.length?`<div class="branch-compare">
          <label class="prselect"><span class="cap">target</span><span class="selectbox"><select class="envpick prpick" id="branchCompareBase" title="Target branch">${branchOptions([def],def)}</select></span></label>
          <span class="arr">←</span>
          <label class="prselect"><span class="cap">source</span><span class="selectbox branchbox"><select class="envpick prpick" id="branchCompareHead" title="Source branch">${branchOptions(drafts,selectedHead)}</select></span></label>
          <button class="btn go" id="branchCompareGo" type="button">Compare changes</button>
        </div>`:`<div class="pr-empty">No draft branches yet. Create a branch, then compare it against ${esc(def)}.</div>`}
      </div>
    </div>
    <div class="prbox">
      <div class="hd"><span class="ttl">Branches</span><span class="sp"></span><span class="ct">${names.length}</span></div>
      <div class="bd"><div class="branch-table">${rows}</div></div>
    </div>
  </div>`;
  syncSelectMenus($("hist"));
  const go=$("branchCompareGo");
  if(go)go.onclick=()=>openCompareWorkspace($("branchCompareBase").value,$("branchCompareHead").value);
  $("hist").querySelectorAll(".branchrow").forEach(row=>row.onclick=e=>{
    if(e.target.closest("button"))return;
    syncTopBranch(row.dataset.branch);
  });
  $("hist").querySelectorAll("[data-branch-new]").forEach(b=>b.onclick=e=>{e.stopPropagation();openCreateBranch();});
  $("hist").querySelectorAll("[data-branch-compare]").forEach(b=>b.onclick=e=>{e.stopPropagation();openCompareWorkspace(def,b.dataset.branchCompare);});
  $("hist").querySelectorAll("[data-branch-delete]").forEach(b=>b.onclick=e=>{e.stopPropagation();openDeleteBranch(b.dataset.branchDelete);});
}
function openDeleteBranch(name){
  if(name===defaultBranch()){toast("default branch cannot be deleted",true);return;}
  modal(`<h3>Delete branch</h3><div class="b">
    <div class="desc">Delete draft branch <b>${esc(name)}</b>. Runtime records are not changed, but this branch will leave the branch list.</div>
    <div><label>Type branch name</label><input id="mConfirm" autocomplete="off" spellcheck="false" placeholder="${esc(name)}"></div>
  </div><div class="f"><button class="btn" onclick="closeModal()">Cancel</button><button class="btn warn" id="mGo" disabled>Delete branch</button></div>`);
  const confirm=$("mConfirm"), go=$("mGo");
  confirm.oninput=()=>{go.disabled=confirm.value!==name;};
  go.onclick=async()=>{go.disabled=true;const r=await api("branch_delete",{name});await afterDeleteBranch(r,name);};
  confirm.focus();
}
async function afterDeleteBranch(res,name){closeModal();
  const ok=res&&res.status==="ok";
  toast(ok?"Branch deleted":(res&&res.message?res.message:"Delete failed"),!ok);
  if(ok&&S.branchView===name)S.branchView=defaultBranch();
  if(ok&&S.prHead===name)S.prHead=null;
  await loadState();
  if(inBranchesMode())renderBranchesWorkspace();
}
function prOpenForHead(head){return S.prs.find(p=>p.status==="open"&&p.head_branch===head);}
function branchOptions(names,current){return names.map(n=>`<option value="${esc(n)}" ${n===current?"selected":""}>${esc(n)}</option>`).join("");}
function syncTopBranch(name){
  const sel=$("branch");
  if(sel&&[...sel.options].some(o=>o.value===name)){sel.value=name;renderBranches();}
}
function prRecordName(row){return `${row.collection}:${row.record_id}`;}
function renderPrWorkspace(){
  setNav("navPr");
  const def=defaultBranch();
  const drafts=draftBranches();
  const current=selectedBranch();
  if(!S.prHead||S.prHead===def||!branchNames().includes(S.prHead))S.prHead=(current!==def&&branchNames().includes(current))?current:(drafts[0]||"");
  S.prBase=def;
  S.prRows=[];
  S.prSel=0;
  $("histCt").textContent=`${S.prs.filter(p=>p.status==="open").length} open`;
  $("dActs").innerHTML="";
  if(!drafts.length){
    $("hist").innerHTML=`<div class="prwork">
      <div class="prbox"><div class="hd"><span class="ttl">Compare changes</span></div>
        <div class="bd"><div class="pr-empty">No draft branches yet. Create a branch, commit a draft, then compare the records changed from main.</div>
          <div class="praction"><button class="btn go" id="prCreateBranch">New branch</button></div></div></div></div>`;
    $("dTitle").textContent="Pull requests";
    $("diff").innerHTML=`<div class="paper"><div class="nodiff">Create a draft branch to compare it against ${esc(def)}.</div></div>`;
    $("prCreateBranch").onclick=()=>openCreateBranch();
    return;
  }
  const head=S.prHead||drafts[0];
  const openPr=prOpenForHead(head);
  const allPrs=S.prs.filter(p=>p.status==="open");
  const prIntent=openPr?`<div class="pr-intent open" id="prIntent">
    <div class="topline"><span class="op r-commit">open</span><b>${esc(openPr.message||"review draft")}</b><span>PR ${esc(openPr.id)}</span></div>
    <div class="prmeta"><span>${esc(openPr.base_branch||def)} ← ${esc(openPr.head_branch||head)}</span><span>${(openPr.records||[]).length} records captured</span></div>
    <div class="message">${esc(openPr.message||"review draft")}</div>
    <div class="praction"><button class="btn" id="prReload" type="button">Refresh comparison</button><button class="btn warn" id="prMergeInline" type="button">Merge pull request</button></div>
  </div>`:`<div class="pr-intent" id="prIntent">
    <div class="topline"><span class="op">draft</span><b>No pull request opened yet</b><span>${esc(def)} ← ${esc(head)}</span></div>
    <div class="prmsg"><label>Pull request title</label><input id="prMsg" value="merge ${esc(head)}" autocomplete="off"></div>
    <div class="praction"><button class="btn" id="prReload" type="button">Refresh comparison</button><button class="btn go" id="prOpenInline" type="button">Open pull request</button></div>
  </div>`;
  const otherPrs=allPrs.filter(p=>p.head_branch!==head);
  const prCards=otherPrs.map(p=>`<div class="prcard">
    <div class="prid"><span class="op r-commit">open</span><b>${esc(p.id)}</b></div>
    <div class="subtle">${esc(p.message||"review draft")}</div>
    <div class="prmeta"><span class="mono">${esc(p.base_branch||def)} ← ${esc(p.head_branch||"")}</span><span>${(p.records||[]).length} records</span></div>
    <div class="praction"><button class="btn" data-pr-head="${esc(p.head_branch||"")}">View comparison</button></div>
  </div>`).join("");
  const prInbox=otherPrs.length?`<div class="prbox">
      <div class="hd"><span class="ttl">Other open pull requests</span><span class="sp"></span><span class="ct">${otherPrs.length}</span></div>
      <div class="bd prlist">${prCards}</div>
    </div>`:"";
  $("hist").innerHTML=`<div class="prwork">
    <div class="compare-hero">
      <div><div class="title">Comparing changes</div><div class="meta">${esc(def)} ← ${esc(head)}</div></div>
      <span class="op">${openPr?"pull request":"compare"}</span>
    </div>
    <div class="prbox primary">
      <div class="hd"><span class="ttl">${openPr?"Review pull request":"Compare branch"}</span><span class="sp"></span><span class="op">${openPr?"open":"draft"}</span></div>
      <div class="bd">
        <div class="compare-row">
          <div class="prselect"><span class="cap">target</span><span class="branch-pill">${esc(def)}</span></div>
          <span class="arr">←</span>
          <label class="prselect"><span class="cap">source</span><span class="selectbox branchbox"><select class="envpick prpick" id="prHead" title="Source branch">${branchOptions(drafts,head)}</select></span></label>
        </div>
        <div class="mergebar" id="prMergebar"><span class="check">…</span><b>Checking comparison</b><span>${esc(def)} ← ${esc(head)}</span></div>
        <div class="pr-status" id="prSummary"><div class="spin">computing diff…</div></div>
        ${prIntent}
      </div>
    </div>
    <div class="prbox">
      <div class="hd"><span class="ttl">Changed records</span><span class="sp"></span><span class="ct" id="prRecordCt"></span></div>
      <div class="bd prlist" id="prRecordList"><div class="spin">computing diff…</div></div>
    </div>
    ${prInbox}
  </div>`;
  syncSelectMenus($("hist"));
  $("prHead").onchange=()=>{S.prHead=$("prHead").value;S.prSel=0;syncTopBranch(S.prHead);renderPrWorkspace();};
  $("prReload").onclick=()=>loadPrComparison();
  const openBtn=$("prOpenInline");if(openBtn)openBtn.onclick=()=>openPrFromWorkspace();
  const mergeBtn=$("prMergeInline");if(mergeBtn)mergeBtn.onclick=()=>mergePrFromWorkspace();
  $("hist").querySelectorAll("[data-pr-head]").forEach(b=>b.onclick=()=>{S.prHead=b.dataset.prHead;S.prSel=0;syncTopBranch(S.prHead);renderPrWorkspace();});
  if(openPr&&$("prMsg"))$("prMsg").value=openPr.message||("merge "+head);
  loadPrComparison();
}
async function loadPrComparison(){
  const base=S.prBase||defaultBranch();
  const head=S.prHead;
  if(!head)return;
  const openPr=prOpenForHead(head);
  $("dTitle").innerHTML=`Compare · <b>${esc(base)}..${esc(head)}</b>`;
  $("dActs").innerHTML="";
  $("diff").innerHTML=`<div class="spin">computing branch diff…</div>`;
  const r=await api("branch_diff",{range:`${base}..${head}`});
  if(!r||r.status!=="ok"){
    const msg=r&&r.message?r.message:"Could not compute branch diff.";
    const mb=$("prMergebar");
    if(mb){mb.className="mergebar warn";mb.innerHTML=`<span class="check">!</span><b>Comparison failed</b><span>${esc(msg)}</span>`;}
    $("prSummary").innerHTML=`<span class="tag drift">error</span><span>${esc(msg)}</span>`;
    $("prRecordList").innerHTML=`<div class="pr-empty">${esc(msg)}</div>`;
    $("diff").innerHTML=`<div class="paper"><div class="nodiff">${esc(msg)}</div></div>`;
    return;
  }
  const rows=(r.data&&r.data.records)||[];
  S.prRows=rows;
  const changed=rows.length;
  const changeCount=rows.reduce((n,row)=>n+(row.changes||[]).length,0);
  const mb=$("prMergebar");
  if(mb){
    mb.className="mergebar "+(changed?"ok":"warn");
    const label=openPr?"Pull request open":(changed?"Ready to open pull request":"No changes to open");
    const detail=changed?`${changed} changed record${changed===1?"":"s"} from ${base} to ${head}`:`${head} matches ${base}`;
    mb.innerHTML=`<span class="check">${changed?"✓":"!"}</span><b>${esc(label)}</b><span>${esc(detail)}</span>`;
  }
  $("prSummary").innerHTML=`<div class="pr-stat"><span class="num">${changed}</span><span class="cap">changed records</span></div>
    <div class="pr-stat"><span class="num">${changeCount}</span><span class="cap">field changes</span></div>
    <div class="pr-stat"><span class="num">${openPr?"open":(changed?"ready":"none")}</span><span class="cap">pull request</span></div>`;
  const openBtn=$("prOpenInline"), mergeBtn=$("prMergeInline");
  if(openBtn)openBtn.disabled=!changed||!!openPr;
  if(mergeBtn)mergeBtn.disabled=!openPr;
  renderPrRecordList();
  if(rows.length)renderPrRecordDiff(Math.min(S.prSel||0,rows.length-1));
  else $("diff").innerHTML=`<div class="paper"><div class="nodiff">No draft changes on ${esc(head)} compared with ${esc(base)}.</div></div>`;
}
function renderPrRecordList(){
  const list=$("prRecordList"); if(!list)return;
  const rows=S.prRows||[];
  const ct=$("prRecordCt"); if(ct)ct.textContent=rows.length?`${rows.length}`:"";
  if(!rows.length){list.innerHTML=`<div class="pr-empty">No changed records for this comparison.</div>`;return;}
  list.innerHTML=rows.map((row,i)=>`<div class="prrec ${i===(S.prSel||0)?"sel":""}" data-i="${i}">
    <div><div class="name">${esc(prRecordName(row))}</div><div class="lines">${esc((row.commit_id||"").slice(0,12))}</div></div>
    <span class="delta">${(row.changes||[]).length} fields</span>
  </div>`).join("");
  list.querySelectorAll(".prrec").forEach(r=>r.onclick=()=>renderPrRecordDiff(+r.dataset.i));
}
function renderPrRecordDiff(i){
  const row=(S.prRows||[])[i]; if(!row)return;
  S.prSel=i;renderPrRecordList();
  const base=S.prBase||defaultBranch(), head=S.prHead||"";
  $("dTitle").innerHTML=`PR diff · <b>${esc(base)}..${esc(head)}</b> · ${esc(prRecordName(row))}`;
  renderDiff({data:{changes:row.changes||[]}},base,head,"No field-level draft changes.");
}
async function openPrFromWorkspace(){
  const head=S.prHead;
  if(!head){toast("select a compare branch",true);return;}
  if(prOpenForHead(head)){toast("pull request already open for "+head,true);return;}
  const btn=$("prOpenInline"); if(btn)btn.disabled=true;
  const msg=($("prMsg")&&$("prMsg").value)||("merge "+head);
  const r=await api("pr_create",{base:S.prBase||defaultBranch(),head,message:msg});
  await afterPr(r,"PR opened",head);
}
async function mergePrFromWorkspace(){
  const head=S.prHead;
  const pr=prOpenForHead(head);
  if(!pr){toast("no open PR for this branch",true);return;}
  const btn=$("prMergeInline"); if(btn)btn.disabled=true;
  const msg=($("prMsg")&&$("prMsg").value)||("merge "+head);
  const r=await api("pr_merge",{id:pr.id,message:msg});
  await afterPr(r,"Merged",head);
}
async function afterPr(res,verb,head){closeModal();
  const ok=res&&res.status==="ok";
  toast(ok?verb:(res&&res.message?res.message:verb+" failed"),!ok);
  S.prHead=head||S.prHead;
  await loadState();
  renderPrWorkspace();
}
function openPrModal(){
  const br=selectedBranch();
  if(br===defaultBranch()){toast("select a non-main branch",true);return;}
  S.prHead=br;renderPrWorkspace();
}
function openMergeModal(){
  const br=selectedBranch();
  const prs=S.prs.filter(p=>p.head_branch===br&&p.status==="open");
  if(!prs.length){toast("no open PR for this branch",true);return;}
  modal(`<h3>Merge PR</h3><div class="b">
    <div class="desc">This is the runtime mutation path. cfgit will refuse stale heads and out-of-band drift.</div>
    <div><label>Open PR</label><span class="selectbox branchbox"><select class="envpick" id="mPr">${prs.map(p=>`<option value="${esc(p.id)}">${esc(p.id)} · ${esc(p.message||"")}</option>`).join("")}</select></span></div>
    <div><label>Message</label><input id="mMsg" value="merge ${esc(br)}" autocomplete="off"></div></div>
    <div class="f"><button class="btn" onclick="closeModal()">Cancel</button><button class="btn warn" id="mGo">Merge</button></div>`);
  $("mGo").onclick=async()=>{$("mGo").disabled=true;const r=await api("pr_merge",{id:$("mPr").value,message:$("mMsg").value||("merge "+br)});after(r,"Merged");};
}
function inPrMode(){return document.querySelector(".app")?.classList.contains("pr-mode");}
function inBranchesMode(){return document.querySelector(".app")?.classList.contains("branches-mode");}
async function afterBranch(res,verb,nextBranch){closeModal();
  const ok=res&&res.status==="ok";
  toast(ok?verb:(res&&res.message?res.message:verb+" failed"),!ok);
  showRemedy(res&&res.next);
  await loadState();
  if(ok&&nextBranch&&[...$("branch").options].some(o=>o.value===nextBranch))$("branch").value=nextBranch;
  if(inPrMode()){if(ok&&nextBranch)S.prHead=nextBranch;renderPrWorkspace();return;}
  if(inBranchesMode()){if(ok&&nextBranch)S.branchView=nextBranch;renderBranchesWorkspace();return;}
  renderBranches();
}
async function after(res,verb){closeModal();
  const ok=res&&(res.status==="ok"||(res.data&&(res.data.oid||res.data.seq)));
  toast(ok?verb:(res&&res.message?res.message:verb+" failed"),!ok);
  showRemedy(res&&res.next);
  const keep=S.sel, prMode=inPrMode(), branchesMode=inBranchesMode();await loadState();
  if(prMode){renderPrWorkspace();return;}
  if(branchesMode){renderBranchesWorkspace();return;}
  if(keep)selectRecord(keep);}

/* wiring */
$("find").addEventListener("input",e=>{S.q=e.target.value;renderTree();});
$("refresh").onclick=()=>{const keep=S.sel, prMode=inPrMode(), branchesMode=inBranchesMode();loadState().then(()=>{if(prMode)renderPrWorkspace();else if(branchesMode)renderBranchesWorkspace();else if(keep)selectRecord(keep);});};
$("env").addEventListener("change",()=>{S.sel=null;S.open={};loadState();});
$("branch").addEventListener("change",()=>{renderBranches();const br=selectedBranch();if(inPrMode()){if(br!==defaultBranch())S.prHead=br;renderPrWorkspace();}else if(inBranchesMode()){S.branchView=br;renderBranchesWorkspace();}});
// Workflow buttons live with their tab: clicking one lands on that tab's screen, then acts.
function onBranchesTab(fn){if(!inBranchesMode())renderBranchesWorkspace();fn();}
function onPrTab(fn){if(!inPrMode())renderPrWorkspace();fn();}
$("newBranch").onclick=()=>onBranchesTab(openCreateBranch);
$("draftCommit").onclick=()=>onBranchesTab(openDraftCommit);
$("branchDiff").onclick=()=>onBranchesTab(showBranchDiff);
$("openPr").onclick=()=>onPrTab(openPrModal);
$("mergePr").onclick=()=>onPrTab(openMergeModal);
$("navRecordsTab").onclick=()=>{setNav("navRecordsTab");if(!S.sel){$("histCt").textContent="";$("hist").innerHTML=`<div class="ghost-pane"><div class="big">No record selected</div>Pick a record on the left to walk its history.</div>`;$("dTitle").textContent="Diff";$("dActs").innerHTML="";$("diff").innerHTML=`<div class="ghost-pane">A version or a record's drift will render here, recorded against live.</div>`;}$("find").focus();};
$("navBranches").onclick=()=>renderBranchesWorkspace();
$("navHistoryTab").onclick=()=>{S.sel=null;renderTree();renderRecentHistory();};
$("navPr").onclick=()=>renderPrWorkspace();
loadState();
</script>
</body>
</html>
"""
