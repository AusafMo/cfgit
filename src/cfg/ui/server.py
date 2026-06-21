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
<html lang="en" data-theme="dark">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>cfgit</title>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@500;600;700&family=Inter:wght@400;500;600&family=JetBrains+Mono:wght@400;500;600&display=swap" rel="stylesheet">
  <style>
    /* ============ cfgit · git-meets-Compass · two themes ============ */
    :root{
      --disp:"Space Grotesk",ui-sans-serif,system-ui,sans-serif;
      --body:"Inter",ui-sans-serif,system-ui,-apple-system,sans-serif;
      --mono:"JetBrains Mono",ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;
    }
    /* DARK: deep slate, never pure black; calm on the eyes */
    [data-theme="dark"]{
      --bg:#10151c; --chrome:#141b24; --panel:#18212c; --panel2:#1e2935; --raise:#24303d;
      --edge:#27323f; --edge2:#33414f;
      --ink:#e8edf2; --dim:#9aa6b2; --faint:#67727e;
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
      --ink:#24272b; --dim:#5f6670; --faint:#8b9099;
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

    .app{display:grid;grid-template-rows:auto 1fr;height:100vh;min-height:0}

    /* ---- top bar ---- */
    .top{display:flex;align-items:center;gap:16px;padding:0 18px;height:54px;
      background:var(--chrome);border-bottom:1px solid var(--edge)}
    .brand{display:flex;align-items:baseline;gap:2px;font-family:var(--disp);font-weight:700;font-size:18px;letter-spacing:-.01em}
    .brand .dot{color:var(--blue)}
    .who{display:flex;align-items:center;gap:9px;font-size:12.5px;color:var(--dim)}
    .who .ava{width:22px;height:22px;border-radius:6px;display:grid;place-items:center;font-family:var(--mono);
      font-size:10px;font-weight:600;color:#fff;background:linear-gradient(135deg,var(--blue),var(--blue2))}
    .who b{color:var(--ink);font-weight:600}
    .chip{font-family:var(--mono);font-size:10.5px;letter-spacing:.06em;text-transform:uppercase;
      padding:2px 9px;border-radius:999px;border:1px solid var(--edge2);color:var(--dim)}
    .chip.open{color:var(--moss);border-color:var(--moss-bg);background:var(--moss-bg)}
    .top .sp{flex:1}
    .seg{display:flex;background:var(--panel);border:1px solid var(--edge2);border-radius:8px;padding:2px;gap:2px}
    .seg button{border:0;background:transparent;color:var(--dim);padding:4px 9px;border-radius:6px;font-size:12px;cursor:pointer;line-height:1}
    .seg button.on{background:var(--raise);color:var(--ink)}
    .envpick{background:var(--panel);border:1px solid var(--edge2);border-radius:8px;color:var(--ink);
      padding:6px 9px;font-size:12.5px;font-family:var(--mono)}
    .ghost{background:transparent;border:1px solid var(--edge2);border-radius:8px;color:var(--dim);
      padding:6px 11px;font-size:12.5px;cursor:pointer}
    .ghost:hover{color:var(--ink);border-color:var(--blue)}

    /* ---- 3 columns ---- */
    .cols{display:grid;grid-template-columns:300px 320px 1fr;min-height:0}
    .pane{min-height:0;display:flex;flex-direction:column;border-right:1px solid var(--edge);overflow:hidden;background:var(--bg)}
    .pane:last-child{border-right:0}
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
    .dhead .t{font-size:12.5px;color:var(--dim)}
    .dhead .t b{color:var(--ink);font-family:var(--mono)}
    .dhead .sp{flex:1}
    .btn{border:1px solid var(--edge2);border-radius:8px;padding:6px 13px;font-size:12.5px;cursor:pointer;
      background:var(--panel);color:var(--ink);font-weight:500}
    .btn:hover{border-color:var(--blue)}
    .btn.go{background:var(--blue);border-color:var(--blue);color:#fff}
    .btn.go:hover{background:var(--blue2)}
    .btn.warn{color:var(--amber);border-color:var(--amber-bg)}
    .btn:disabled{opacity:.45;cursor:default}
    .paperwrap{flex:1;min-height:0;overflow:auto;background:var(--bg);padding:16px}
    .paper{background:var(--paper);color:var(--paper-ink);border:1px solid var(--paper-edge);border-radius:10px;
      box-shadow:var(--shadow);overflow:hidden;font-family:var(--mono);font-size:12.5px}
    .paper-h{display:grid;grid-template-columns:1fr 1fr;border-bottom:1px solid var(--paper-edge)}
    .paper-h>div{padding:9px 16px;font-size:10.5px;letter-spacing:.07em;text-transform:uppercase;color:var(--paper-dim);
      display:flex;align-items:center;gap:7px}
    .paper-h .r{border-left:1px solid var(--paper-edge)}
    .paper-h .swatch{width:8px;height:8px;border-radius:2px}
    .paper-h .l .swatch{background:var(--paper-del-ink)} .paper-h .r .swatch{background:var(--paper-add-ink)}
    .frow{border-bottom:1px solid var(--paper-edge)}
    .frow:last-child{border-bottom:0}
    .fname{padding:5px 16px;font-size:11px;color:var(--paper-dim);background:var(--paper-gutter);
      border-bottom:1px solid var(--paper-edge);letter-spacing:.02em}
    .fpair{display:grid;grid-template-columns:1fr 1fr}
    .fside{padding:8px 16px;white-space:pre-wrap;word-break:break-word;min-height:34px;line-height:1.55}
    .fside.r{border-left:1px solid var(--paper-edge)}
    .fside.del{background:var(--paper-del);color:var(--paper-del-ink)}
    .fside.add{background:var(--paper-add);color:var(--paper-add-ink)}
    .fside.void{background:repeating-linear-gradient(45deg,transparent,transparent 7px,rgba(0,0,0,.025) 7px,rgba(0,0,0,.025) 14px)}
    /* line-aligned split diff for long multi-line strings (git split view) */
    .splitcol{display:grid;grid-template-columns:1fr 1fr}
    .scol{min-width:0}
    .scol.r{border-left:1px solid var(--paper-edge)}
    .dl{display:grid;grid-template-columns:34px 1fr;line-height:1.5;font-size:12px;border-bottom:1px solid rgba(0,0,0,.035)}
    .dl .gut{text-align:right;padding:3px 8px 3px 0;color:var(--paper-dim);user-select:none;font-size:10.5px;
      border-right:1px solid var(--paper-edge);background:var(--paper-gutter)}
    .dl .ln{padding:3px 12px;white-space:pre-wrap;word-break:break-word}
    .dl.del{background:var(--paper-del)} .dl.del .ln{color:var(--paper-del-ink)} .dl.del .gut{background:#f3d9d3;color:#b56a5c}
    .dl.del .sign{color:var(--paper-del-ink)}
    .dl.add{background:var(--paper-add)} .dl.add .ln{color:var(--paper-add-ink)} .dl.add .gut{background:#d9ead2;color:#5f8a64}
    .dl.add .sign{color:var(--paper-add-ink)}
    .dl.ctx .ln{color:var(--paper-dim)}
    .dl.blank{background:repeating-linear-gradient(45deg,transparent,transparent 7px,rgba(0,0,0,.022) 7px,rgba(0,0,0,.022) 14px)}
    .dl.blank .gut{background:transparent;border-right-color:transparent}
    .dl .sign{display:inline-block;width:10px;color:var(--paper-dim)}
    .fold{padding:4px 16px;text-align:center;color:var(--paper-dim);font-size:11px;background:var(--paper-gutter);
      border-bottom:1px solid var(--paper-edge);cursor:pointer}
    .fold:hover{color:var(--paper-ink)}
    .nodiff{padding:34px 16px;color:var(--paper-dim);text-align:center;font-family:var(--body);font-size:13px}
    /* impact / system-overview panel (dark, sits above the paper diff) */
    .impact{margin:0 0 16px;background:var(--panel);border:1px solid var(--edge2);border-radius:12px;overflow:hidden}
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

    .mbg{position:fixed;inset:0;background:rgba(8,11,16,.6);backdrop-filter:blur(2px);display:none;
      align-items:center;justify-content:center;z-index:50}
    .mbg.show{display:flex}
    .modal{background:var(--panel);border:1px solid var(--edge2);border-radius:14px;width:min(540px,92vw);
      box-shadow:var(--shadow);overflow:hidden}
    .modal h3{margin:0;padding:16px 18px;font-family:var(--disp);font-weight:600;font-size:15px;border-bottom:1px solid var(--edge)}
    .modal .b{padding:16px 18px;display:flex;flex-direction:column;gap:12px}
    .modal .desc{color:var(--dim);font-size:13px;line-height:1.55}
    .modal .desc b{color:var(--ink);font-family:var(--mono);font-size:12.5px}
    .modal label{font-size:11px;letter-spacing:.04em;text-transform:uppercase;color:var(--faint);font-weight:600}
    .modal input{width:100%;background:var(--bg);border:1px solid var(--edge2);border-radius:8px;padding:9px 11px;font-family:var(--mono);font-size:12.5px}
    .modal input:focus{outline:none;border-color:var(--blue)}
    .modal .f{display:flex;justify-content:flex-end;gap:9px;padding:14px 18px;border-top:1px solid var(--edge)}

    @media (max-width:1080px){ .cols{grid-template-columns:240px 280px 1fr} }
    @media (max-width:840px){ .cols{grid-template-columns:1fr;grid-auto-rows:minmax(220px,auto)} .pane{border-right:0;border-bottom:1px solid var(--edge)} }
    @media (prefers-reduced-motion:reduce){ *{animation:none!important;transition:none!important} }
  </style>
</head>
<body>
  <div class="app">
    <header class="top">
      <div class="brand">cfg<span class="dot">·</span>it</div>
      <div class="who" id="who"><span class="ava" id="ava">·</span><span id="whoTxt">connecting…</span></div>
      <span class="chip open" id="mode"></span>
      <div class="sp"></div>
      <select class="envpick" id="env" title="environment"><option>dev</option></select>
      <div class="seg" id="theme"><button data-th="dark" class="on">Dark</button><button data-th="light">Light</button></div>
      <button class="ghost" id="refresh" type="button">Refresh</button>
      <input id="configFile" style="display:none">
    </header>
    <div class="cols">
      <!-- LEFT: collection tree -->
      <section class="pane">
        <div class="find"><input id="find" placeholder="find a record…" autocomplete="off" spellcheck="false"></div>
        <div class="filterbar" id="filters"></div>
        <div class="scroll" id="tree"><div class="spin">loading…</div></div>
      </section>
      <!-- MIDDLE: history graph -->
      <section class="pane">
        <div class="ph"><span class="lab">History</span><span class="sp"></span><span class="ct" id="histCt"></span></div>
        <div class="scroll" id="hist"><div class="ghost-pane"><div class="big">No record selected</div>Pick a record on the left to walk its history.</div></div>
      </section>
      <!-- RIGHT: paper diff -->
      <section class="pane">
        <div class="dhead"><span class="t" id="dTitle">Diff</span><span class="sp"></span><span id="dActs"></span></div>
        <div class="paperwrap" id="diff"><div class="ghost-pane">A version or a record's drift will render here, recorded against live.</div></div>
      </section>
    </div>
  </div>
  <div class="toast" id="toast"></div>
  <div class="mbg" id="mbg"><div class="modal" id="modal"></div></div>

<script>
const S={records:[],filter:"all",q:"",sel:null,hist:[],who:null,open:{}};
const $=id=>document.getElementById(id);
const esc=v=>String(v==null?"":v).replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const fmt=v=>typeof v==="object"?JSON.stringify(v):String(v);
const dcls=s=>s==="clean"?"clean":s==="new"?"new":"drift";
const isDrift=s=>s!=="clean"&&s!=="new";

function env(){return{env:$("env").value||"dev",config_file:$("configFile").value||null};}
function qs(){const p=new URLSearchParams(),e=env();if(e.env)p.set("env",e.env);if(e.config_file)p.set("config_file",e.config_file);return p.toString();}
async function api(action,data){const r=await fetch("/api/action",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({action,...env(),...data})});return r.json();}
function toast(msg,bad){const t=$("toast");t.innerHTML=`<span class="${bad?"bad":"ok"}">${bad?"✕":"✓"}</span>${esc(msg)}`;t.className="toast show"+(bad?" err":"");clearTimeout(t._t);t._t=setTimeout(()=>t.className="toast",2400);}
function initials(s){s=String(s||"");const at=s.indexOf("@");const h=at>0?s.slice(0,at):s;return (h.replace(/[^a-zA-Z0-9]/g,"").slice(0,2)||"··").toLowerCase();}

/* theme */
function setTheme(t){document.documentElement.dataset.theme=t;try{localStorage.setItem("cfgit-theme",t)}catch(e){}
  $("theme").querySelectorAll("button").forEach(b=>b.classList.toggle("on",b.dataset.th===t));}
$("theme").querySelectorAll("button").forEach(b=>b.onclick=()=>setTheme(b.dataset.th));
try{const sv=localStorage.getItem("cfgit-theme");if(sv)setTheme(sv);}catch(e){}

async function loadState(){
  const st=await fetch("/api/state?"+qs()).then(r=>r.json()).catch(e=>({error:String(e)}));
  if(st.data&&st.data.whoami){const w=st.data.whoami;S.who=w;const id=w.identity||{};
    const disp=w.identity_display||w.author||"";
    $("whoTxt").innerHTML=`<b>${esc(disp)}</b> · ${esc(w.env||"dev")}`;
    $("ava").textContent=initials(w.author||disp);
    $("mode").textContent=w.identity_mode||id.mode||"open";}
  // populate env options from schema
  const sc=await fetch("/api/schema?"+qs()).then(r=>r.json()).catch(()=>null);
  if(sc&&sc.data&&Array.isArray(sc.data.envs)&&sc.data.envs.length){
    const cur=$("env").value; $("env").innerHTML=sc.data.envs.map(e=>`<option ${e===cur?"selected":""}>${esc(e)}</option>`).join("");}
  S.records=(st.data&&st.data.status)?st.data.status:[];
  // default: open every collection that has drift, else open first
  const colls=[...new Set(S.records.map(r=>r.collection))];
  if(Object.keys(S.open).length===0){colls.forEach(c=>{S.open[c]=S.records.some(r=>r.collection===c&&isDrift(r.state));});
    if(!Object.values(S.open).some(Boolean)&&colls[0])S.open[colls[0]]=true;}
  renderFilters();renderTree();
}

function counts(){const c={all:S.records.length,drift:0,clean:0,new:0};
  for(const r of S.records){if(r.state==="clean")c.clean++;else if(r.state==="new")c.new++;else c.drift++;}return c;}
function renderFilters(){const c=counts();
  const d=[["all","All",c.all],["drift","Drift",c.drift],["clean","Clean",c.clean],["new","New",c.new]];
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
        const right=r.state==="clean"?`<span class="rt">@${r.head_seq??""}</span>`
          :r.state==="new"?`<span class="tag new">new</span>`:`<span class="tag drift">drift</span>`;
        const slash=r.record_id.lastIndexOf("/");
        const nmHtml=slash>=0
          ? `<span class="pre">${esc(r.record_id.slice(0,slash+1))}</span><span class="leaf">${esc(r.record_id.slice(slash+1))}</span>`
          : `<span class="leaf">${esc(r.record_id)}</span>`;
        return `<div class="doc ${sel}" data-k="${esc(key)}" title="${esc(r.record_id)}">
          <span class="st ${dcls(r.state)}"></span>
          <span class="nm">${nmHtml}</span>${right}</div>`;}).join("")}</div></div>`;
  }
  el.innerHTML=html;
  el.querySelectorAll(".coll-h").forEach(h=>h.onclick=()=>{const c=h.parentElement.dataset.c;S.open[c]=!S.open[c];renderTree();});
  el.querySelectorAll(".doc").forEach(d=>d.onclick=e=>{e.stopPropagation();selectRecord(d.dataset.k);});
}

async function selectRecord(key){
  S.sel=key;renderTree();
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
  $("dActs").innerHTML=`<button class="btn" id="aImpact">Impact</button> <button class="btn go" id="aAdopt">Adopt</button> <button class="btn warn" id="aRestore">Restore @${rec.head_seq??""}</button>`;
  $("diff").innerHTML=`<div class="spin">computing diff…</div>`;
  const res=await api("diff",{record:S.sel,a:"=HEAD",b:"=live"});
  renderDiff(res,"recorded","live",S.diffCtx.empty);
  $("aImpact").onclick=()=>showImpact();
  $("aAdopt").onclick=()=>openAdopt(rec);
  $("aRestore").onclick=()=>openRestore(rec,"@"+(rec.head_seq??""));
}
async function showImpact(){
  if(!S.diffCtx)return;
  const btn=$("aImpact"); if(btn){btn.disabled=true;btn.textContent="Analyzing…";}
  const r=await api("impact",{record:S.sel,a:S.diffCtx.a,b:S.diffCtx.b,use_llm:true});
  if(btn){btn.disabled=false;btn.textContent="Impact";}
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
    const asList=v=>Array.isArray(v)?`<ul class="llmul">${v.map(x=>`<li>${esc(typeof x==="object"?JSON.stringify(x):x)}</li>`).join("")}</ul>`:esc(v);
    if(ov.summary)parts.push(`<div class="body">${esc(ov.summary)}</div>`);
    if(ov.behavior_change)parts.push(`<div class="lk"><span class="lkk">behavior</span>${asList(ov.behavior_change)}</div>`);
    if(ov.blast_radius)parts.push(`<div class="lk"><span class="lkk">blast radius</span>${asList(ov.blast_radius)}</div>`);
    if(ov.unknowns&&(Array.isArray(ov.unknowns)?ov.unknowns.length:true))parts.push(`<div class="lk"><span class="lkk">unknowns</span>${asList(ov.unknowns)}</div>`);
    const fallback=(!parts.length&&(llm.text||llm.narration))?`<div class="body">${esc(llm.text||llm.narration)}</div>`:"";
    llmHtml=`<div class="llm"><div class="who">${esc(llm.provider||"llm")} · ${esc(llm.model||"")} narration</div>${parts.join("")||fallback||`<div class="off">no narration returned</div>`}</div>`;
  } else {
    llmHtml=`<div class="llm"><div class="who">LLM narration</div><div class="off">off — enable <span class="mono">[connections]</span> in .cfg.toml for a written explanation of what this change does to the system. Everything above is computed locally, no data leaves your machine.</div></div>`;
  }
  const panel=`<div class="impact">
    <div class="ih"><span class="tt">System impact</span><span class="sp"></span><span class="risk ${risk}">${esc(risk)} risk</span></div>
    <div class="ib">
      <div class="sum">${esc(d.summary||"")}</div>
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
  if(parent)acts.push(`<button class="btn" id="aImpact">Impact</button>`);
  if(!isHead)acts.push(`<button class="btn warn" id="aRestore">Restore this version</button>`);
  $("dActs").innerHTML=acts.join(" ");
  $("diff").innerHTML=`<div class="spin">loading…</div>`;
  if(parent){S.diffCtx={a:"@"+parent.seq,b:"@"+e.seq,left:"@"+parent.seq,right:"@"+e.seq};
    const res=await api("diff",{record:S.sel,a:"@"+parent.seq,b:"@"+e.seq});
    renderDiff(res,"@"+parent.seq,"@"+e.seq,"No field-level change from the parent version.");}
  else{S.diffCtx=null;const res=await api("show",{record:S.sel,ref:"@"+e.seq});renderDoc(res);}
  const ib=$("aImpact");if(ib)ib.onclick=()=>showImpact();
  const rb=$("aRestore");if(rb)rb.onclick=()=>openRestore(S.records.find(r=>r.collection+":"+r.record_id===S.sel),"@"+e.seq);
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
// collapse long runs of unchanged context to "… N unchanged lines …"
function foldContext(ops,pad){
  pad=pad==null?3:pad; const out=[]; let i=0;
  const changedAt=ops.map(o=>o.t!=="ctx");
  const keep=new Array(ops.length).fill(false);
  for(let k=0;k<ops.length;k++) if(changedAt[k]) for(let d=-pad;d<=pad;d++){const idx=k+d;if(idx>=0&&idx<ops.length)keep[idx]=true;}
  while(i<ops.length){
    if(keep[i]){out.push(ops[i]);i++;}
    else{let j=i;while(j<ops.length&&!keep[j])j++;out.push({t:"fold",n:j-i});i=j;}
  }
  return out;
}
function splitDiffHtml(before,after){
  const ops=foldContext(lineDiff(before,after));
  let L="",R="";let ln=0,rn=0;
  for(const o of ops){
    if(o.t==="fold"){const f=`<div class="dl blank"><div class="gut"></div><div class="ln" style="text-align:center;color:var(--paper-dim)">⋯ ${o.n} unchanged ⋯</div></div>`;
      L+=f;R+=f;ln+=o.n;rn+=o.n;continue;}
    if(o.t==="ctx"){ln++;rn++;
      L+=`<div class="dl ctx"><div class="gut">${ln}</div><div class="ln"><span class="sign"> </span>${esc(o.l)}</div></div>`;
      R+=`<div class="dl ctx"><div class="gut">${rn}</div><div class="ln"><span class="sign"> </span>${esc(o.r)}</div></div>`;}
    else if(o.t==="del"){ln++;
      L+=`<div class="dl del"><div class="gut">${ln}</div><div class="ln"><span class="sign">−</span>${esc(o.l)}</div></div>`;
      R+=`<div class="dl blank"><div class="gut"></div><div class="ln"></div></div>`;}
    else{rn++;
      L+=`<div class="dl blank"><div class="gut"></div><div class="ln"></div></div>`;
      R+=`<div class="dl add"><div class="gut">${rn}</div><div class="ln"><span class="sign">+</span>${esc(o.r)}</div></div>`;}
  }
  return `<div class="splitcol"><div class="scol">${L}</div><div class="scol r">${R}</div></div>`;
}
function isLongText(v){return typeof v==="string"&&(v.length>120||v.indexOf("\n")>=0);}
function renderDiff(res,leftLabel,rightLabel,emptyMsg){
  const ch=changesFrom(res);
  if(!ch){const txt=res&&res.data&&res.data.text?res.data.text:JSON.stringify(res,null,2);
    $("diff").innerHTML=`<div class="paper doconly"><div class="docbody">${esc(txt)}</div></div>`;return;}
  if(!ch.length){$("diff").innerHTML=`<div class="paper"><div class="nodiff">${esc(emptyMsg||"No changes.")}</div></div>`;return;}
  let rows="";
  for(const c of ch){const f=c.path||c.field||c.key||"";
    let before=("before"in c)?c.before:c.old, after=("after"in c)?c.after:c.new;
    const op=c.op||((before==null)?"add":(after==null)?"remove":"change");
    // long multi-line text on both sides -> git-style line-aligned split
    if(op==="change"&&(isLongText(before)||isLongText(after))){
      rows+=`<div class="frow"><div class="fname">${esc(f)}</div>${splitDiffHtml(before,after)}</div>`;
      continue;
    }
    const lcls=op==="add"?"void":"del", rcls=op==="remove"?"void":"add";
    const lval=op==="add"?"":esc(fmt(before)), rval=op==="remove"?"":esc(fmt(after));
    rows+=`<div class="frow"><div class="fname">${esc(f)}</div><div class="fpair">
      <div class="fside ${lcls}">${lval}</div><div class="fside r ${rcls}">${rval}</div></div></div>`;}
  $("diff").innerHTML=`<div class="paper">
    <div class="paper-h"><div class="l"><span class="swatch"></span>${esc(leftLabel)}</div><div class="r"><span class="swatch"></span>${esc(rightLabel)}</div></div>
    ${rows}</div>`;
}
function renderDoc(res){const d=res&&res.data?res.data:res;const doc=d&&d.doc?d.doc:(d&&d.text?d.text:d);
  const txt=(typeof doc==="string")?doc:JSON.stringify(doc,null,2);
  $("diff").innerHTML=`<div class="paper doconly"><div class="paper-h"><div>document</div></div><div class="docbody">${esc(txt)}</div></div>`;}

/* modals */
function modal(html){$("modal").innerHTML=html;$("mbg").classList.add("show");}
function closeModal(){$("mbg").classList.remove("show");}
$("mbg").addEventListener("click",e=>{if(e.target===$("mbg"))closeModal();});
function openAdopt(rec){modal(`<h3>Adopt out-of-band change</h3><div class="b">
  <div class="desc">Fold the current live value of <b>${esc(rec.record_id)}</b> into history as a new version, with attribution. The drift becomes a recorded commit.</div>
  <div><label>Reason</label><input id="mMsg" value="adopt out-of-band edit" autocomplete="off"></div></div>
  <div class="f"><button class="btn" onclick="closeModal()">Cancel</button><button class="btn go" id="mGo">Adopt</button></div>`);
  $("mGo").onclick=async()=>{$("mGo").disabled=true;const r=await api("adopt",{record:S.sel,message:$("mMsg").value||"adopt"});after(r,"Adopted");};}
function openRestore(rec,ref){modal(`<h3>Restore ${esc(ref)}</h3><div class="b">
  <div class="desc">Re-apply the <b>${esc(ref)}</b> version of <b>${esc(rec?rec.record_id:S.sel)}</b> as a new version on top. Nothing is lost — restore is non-destructive.</div>
  <div><label>Reason</label><input id="mMsg" value="restore ${esc(ref)}" autocomplete="off"></div></div>
  <div class="f"><button class="btn" onclick="closeModal()">Cancel</button><button class="btn warn" id="mGo">Restore</button></div>`);
  $("mGo").onclick=async()=>{$("mGo").disabled=true;const r=await api("restore",{record:S.sel,ref:ref,message:$("mMsg").value||("restore "+ref)});after(r,"Restored");};}
async function after(res,verb){closeModal();
  const ok=res&&(res.status==="ok"||(res.data&&(res.data.oid||res.data.seq)));
  toast(ok?verb:(res&&res.message?res.message:verb+" failed"),!ok);
  const keep=S.sel;await loadState();if(keep)selectRecord(keep);}

/* wiring */
$("find").addEventListener("input",e=>{S.q=e.target.value;renderTree();});
$("refresh").onclick=()=>{const keep=S.sel;loadState().then(()=>{if(keep)selectRecord(keep);});};
$("env").addEventListener("change",()=>{S.sel=null;S.open={};loadState();});
loadState();
</script>
</body>
</html>
"""
