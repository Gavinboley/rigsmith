"""A stand-in HeadRush, so the device path can be built without hardware.

**This is not the HeadRush protocol.** Nobody has captured that yet. This is
an invented API of roughly the right shape, existing so the parts of rigsmith
that talk to a device can be written, exercised and tested end to end today
rather than waiting on a cable.

That distinction matters enough to keep saying: every response here is
fiction. Model *names* are real - they come from the vendor's published model
list - but their parameter sets are made up, and the endpoint paths are a
guess at what a device like this would plausibly expose. Nothing learned from
this simulator is evidence about the real device.

What it is genuinely good for:

* ``rigsmith discover`` finds it, so discovery is exercisable.
* ``rigsmith probe --dump-app`` mines its bundle, so the endpoint extractor
  is exercisable against something that looks like a real minified app.
* ``rigsmith catalog extract`` reads that bundle, so the catalog pipeline is
  exercisable end to end.
* ``rigsmith protocol record`` can proxy it, so the recorder is exercisable.

And once real traffic *is* captured, :func:`replay_app` serves the recorded
responses instead of the invented ones - at which point the simulator stops
being a guess and becomes a faithful fixture.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..core.errors import Invalid, NotFound, Unavailable

try:
    from fastapi import Body, FastAPI, HTTPException, Request, WebSocket
    from fastapi.responses import HTMLResponse, JSONResponse, Response
    from starlette.websockets import WebSocketDisconnect

    HAVE_WEB = True
except ImportError:  # pragma: no cover - optional dependency
    HAVE_WEB = False
    FastAPI = Body = HTTPException = Request = WebSocket = None  # type: ignore[assignment]
    HTMLResponse = JSONResponse = Response = WebSocketDisconnect = None  # type: ignore[assignment]


def _require_web() -> None:
    if not HAVE_WEB:
        raise Unavailable(
            "the device simulator needs the web stack",
            hint="Install it with:  pip install 'rigsmith[ui]'",
        )


#: Amp and cab names from the vendor's published model list. Real names,
#: invented parameters - enough to make catalog extraction realistic without
#: pretending to know knob layouts nobody has published.
AMP_MODELS = [
    ("59 TWEED DELUXE", "headrush"),
    ("65 BLACK PRINCE", "headrush"),
    ("67 PLEXIGLAS VARI", "headrush"),
    ("82 LEAD 800 100W", "headrush"),
    ("92 TREADPLATE MODERN", "headrush"),
    ("17 TRACE ELLIOT ELF", "headrush"),
    ("25 ON-1 CLEAN", "revalver"),
    ("62 BLUESMAKER", "revalver"),
    ("05 PEAVEY 6505 LEAD", "revalver"),
    ("14 PEAVEY CUSTOM LITE 20", "revalver"),
]

CAB_MODELS = [
    ("1X8 CUSTOM", "headrush"),
    ("1X12 BLACK PANEL LUX", "headrush"),
    ("2X12 AC BLUE", "headrush"),
    ("4X12 65W", "headrush"),
    ("8X10 BLUE LINE", "headrush"),
    ("ANGEL 412 SLANT", "revalver"),
    ("BUDDA 212 OPEN BACK", "revalver"),
]

EFFECT_MODELS = [
    ("TUBE SCREAMER", "overdrive"),
    ("BIG FUZZ", "distortion_fuzz"),
    ("OPTICAL COMP", "compressor"),
    ("ANALOG DELAY", "delay"),
    ("TAPE ECHO", "delay"),
    ("PLATE REVERB", "reverb"),
    ("SPRING REVERB", "reverb"),
    ("CHORUS ENSEMBLE", "chorus"),
    ("PHASE 90", "phaser_flanger"),
    ("CRY WAH", "wah_filter"),
]

AMP_PARAMS = [
    {"name": "gain", "min": 0, "max": 10, "default": 5},
    {"name": "bass", "min": 0, "max": 10, "default": 5},
    {"name": "mid", "min": 0, "max": 10, "default": 5},
    {"name": "treble", "min": 0, "max": 10, "default": 5},
    {"name": "presence", "min": 0, "max": 10, "default": 5},
    {"name": "master", "min": 0, "max": 10, "default": 7},
]

CAB_PARAMS = [
    {"name": "mic", "choices": ["dynamic", "condenser", "ribbon"], "default": "dynamic"},
    {"name": "break_up", "min": 0, "max": 10, "default": 3},
    {"name": "on_axis", "min": 0, "max": 10, "default": 5},
    {"name": "out_gain", "min": -12, "max": 12, "default": 0, "unit": "dB"},
]

EFFECT_PARAMS = [
    {"name": "mix", "min": 0, "max": 1, "default": 0.3},
    {"name": "level", "min": 0, "max": 1, "default": 0.5},
]


def build_catalog() -> list[dict[str, Any]]:
    """The invented model catalog the simulator serves."""
    models: list[dict[str, Any]] = []
    for name, family in AMP_MODELS:
        models.append(
            {
                "id": name.lower().replace(" ", "-"),
                "name": name,
                "category": "amp",
                "family": family,
                "params": AMP_PARAMS,
            }
        )
    for name, family in CAB_MODELS:
        models.append(
            {
                "id": name.lower().replace(" ", "-"),
                "name": name,
                "category": "cab",
                "family": family,
                "params": CAB_PARAMS,
            }
        )
    for name, subcategory in EFFECT_MODELS:
        models.append(
            {
                "id": name.lower().replace(" ", "-"),
                "name": name,
                "category": "effect",
                "subcategory": subcategory,
                "params": EFFECT_PARAMS,
            }
        )
    return models


@dataclass
class FakeRig:
    """A rig as the simulator stores it."""

    id: int
    name: str
    blocks: list[dict[str, Any]] = field(default_factory=list)
    scenes: list[dict[str, Any]] = field(default_factory=list)
    signal_path: str = "straight"

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "signalPath": self.signal_path,
            "blocks": self.blocks,
            "scenes": self.scenes,
        }


@dataclass
class DeviceState:
    """Everything the simulated device knows."""

    firmware: str = "5.1.0"
    model: str = "HeadRush Core"
    rigs: dict[int, FakeRig] = field(default_factory=dict)
    current_rig: int = 1
    cpu: float = 0.07
    next_id: int = 1

    def __post_init__(self) -> None:
        if not self.rigs:
            self.add_rig("Factory Clean")
            self.add_rig("Factory Crunch")

    def add_rig(self, name: str) -> FakeRig:
        rig = FakeRig(id=self.next_id, name=name)
        self.rigs[rig.id] = rig
        self.next_id += 1
        return rig

    def require(self, rig_id: int) -> FakeRig:
        if rig_id not in self.rigs:
            raise KeyError(rig_id)
        return self.rigs[rig_id]

    def recalc_cpu(self) -> None:
        rig = self.rigs.get(self.current_rig)
        blocks = len(rig.blocks) if rig else 0
        self.cpu = round(0.07 + blocks * 0.04, 3)


# --------------------------------------------------------------------------
# the served bundle
# --------------------------------------------------------------------------


def build_bundle_js(catalog: list[dict]) -> str:
    """A minified-looking app bundle that references every endpoint.

    Shaped like a real one deliberately: the endpoints appear at fetch call
    sites, the catalog is an embedded JSON blob, and there is a WebSocket
    constructor - so the extractors get realistic input rather than a tidy
    fixture that would let their weaknesses go unnoticed.
    """
    return (
        'var VERSION="5.1.0";var BASE="/api/v1";\n'
        "const MODEL_CATALOG=" + json.dumps({"models": catalog}) + ";\n"
        'async function loadRigs(){return fetch("/api/v1/rigs").then(r=>r.json())}\n'
        "async function loadRig(id){return fetch(`/api/v1/rigs/${id}`).then(r=>r.json())}\n"
        'async function createRig(b){return fetch("/api/v1/rigs",{method:"POST",body:JSON.stringify(b)})}\n'
        'async function addBlock(id,b){return fetch(`/api/v1/rigs/${id}/blocks`,{method:"POST",body:JSON.stringify(b)})}\n'
        'async function setParam(id,s,b){return fetch(`/api/v1/rigs/${id}/blocks/${s}`,{method:"PATCH",body:JSON.stringify(b)})}\n'
        'async function removeBlock(id,s){return fetch(`/api/v1/rigs/${id}/blocks/${s}`,{method:"DELETE"})}\n'
        'async function reorder(id,b){return fetch(`/api/v1/rigs/${id}/reorder`,{method:"POST",body:JSON.stringify(b)})}\n'
        'async function setScene(id,b){return fetch(`/api/v1/rigs/${id}/scenes`,{method:"POST",body:JSON.stringify(b)})}\n'
        'async function status(){return fetch("/api/v1/status").then(r=>r.json())}\n'
        'async function catalog(){return fetch("/api/v1/models").then(r=>r.json())}\n'
        'var sock=new WebSocket("ws://"+location.host+"/api/v1/ws");\n'
        'sock.onmessage=function(e){var m=JSON.parse(e.data);'
        'if(m.type==="cpu"){render(m)}else if(m.type==="rig_changed"){loadRig(m.id)}};\n'
        'function send(t,p){sock.send(JSON.stringify({type:t,payload:p}))}\n'
        'function selectRig(i){send("select_rig",{id:i})}\n'
        'function pressFootswitch(n){send("footswitch",{number:n})}\n'
    )


BUNDLE_HTML = """<!doctype html>
<html><head><meta charset="utf-8">
<title>HeadRush Remote Editor</title>
<link rel="stylesheet" href="/app.css">
</head><body>
<div id="app">HeadRush Remote Editor</div>
<script src="/app.js"></script>
</body></html>
"""

BUNDLE_CSS = "body{background:#111;color:#eee;font-family:system-ui}\n"


# --------------------------------------------------------------------------
# the app
# --------------------------------------------------------------------------


def build_app(state: DeviceState | None = None):
    """Build the simulated device's ASGI app."""
    _require_web()
    state = state or DeviceState()
    catalog = build_catalog()
    bundle = build_bundle_js(catalog)

    app = FastAPI(title="HeadRush Remote Editor (simulated)", docs_url=None, redoc_url=None)
    app.state.device = state

    # -- the served editor ------------------------------------------------

    @app.get("/", response_class=HTMLResponse)
    def index() -> str:
        return BUNDLE_HTML

    @app.get("/app.js")
    def app_js():
        return Response(content=bundle, media_type="application/javascript")

    @app.get("/app.css")
    def app_css():
        return Response(content=BUNDLE_CSS, media_type="text/css")

    # -- the API ----------------------------------------------------------

    @app.get("/api/v1/status")
    def status() -> dict:
        state.recalc_cpu()
        return {
            "model": state.model,
            "firmware": state.firmware,
            "currentRig": state.current_rig,
            "cpu": state.cpu,
            "simulated": True,
        }

    @app.get("/api/v1/models")
    def models() -> dict:
        return {"models": catalog}

    @app.get("/api/v1/rigs")
    def list_rigs() -> dict:
        return {"rigs": [r.to_dict() for r in state.rigs.values()]}

    @app.get("/api/v1/rigs/{rig_id}")
    def get_rig(rig_id: int) -> dict:
        try:
            return state.require(rig_id).to_dict()
        except KeyError:
            raise HTTPException(404, f"no rig {rig_id}")

    @app.post("/api/v1/rigs")
    def create_rig(payload: dict = Body(...)) -> dict:
        name = payload.get("name")
        if not name:
            raise HTTPException(400, "a rig needs a name")
        return state.add_rig(name).to_dict()

    @app.delete("/api/v1/rigs/{rig_id}")
    def delete_rig(rig_id: int) -> dict:
        if rig_id not in state.rigs:
            raise HTTPException(404, f"no rig {rig_id}")
        del state.rigs[rig_id]
        return {"deleted": rig_id}

    @app.post("/api/v1/rigs/{rig_id}/blocks")
    def add_block(rig_id: int, payload: dict = Body(...)) -> dict:
        try:
            rig = state.require(rig_id)
        except KeyError:
            raise HTTPException(404, f"no rig {rig_id}")

        if len(rig.blocks) >= 14:
            raise HTTPException(409, "all 14 slots are occupied")

        model_id = payload.get("modelId") or payload.get("model_id")
        if not model_id:
            raise HTTPException(400, "modelId is required")

        used = {b["slot"] for b in rig.blocks}
        slot = payload.get("slot") or next(
            (s for s in range(1, 15) if s not in used), None
        )
        if slot in used:
            raise HTTPException(409, f"slot {slot} is occupied")

        block = {
            "slot": slot,
            "modelId": model_id,
            "category": payload.get("category", "effect"),
            "enabled": payload.get("enabled", True),
            "params": payload.get("params", {}),
        }
        rig.blocks.append(block)
        rig.blocks.sort(key=lambda b: b["slot"])
        state.recalc_cpu()
        return block

    @app.patch("/api/v1/rigs/{rig_id}/blocks/{slot}")
    def patch_block(rig_id: int, slot: int, payload: dict = Body(...)) -> dict:
        try:
            rig = state.require(rig_id)
        except KeyError:
            raise HTTPException(404, f"no rig {rig_id}")
        block = next((b for b in rig.blocks if b["slot"] == slot), None)
        if block is None:
            raise HTTPException(404, f"no block in slot {slot}")
        if "enabled" in payload:
            block["enabled"] = bool(payload["enabled"])
        if "params" in payload:
            block["params"].update(payload["params"])
        return block

    @app.delete("/api/v1/rigs/{rig_id}/blocks/{slot}")
    def remove_block(rig_id: int, slot: int) -> dict:
        try:
            rig = state.require(rig_id)
        except KeyError:
            raise HTTPException(404, f"no rig {rig_id}")
        before = len(rig.blocks)
        rig.blocks = [b for b in rig.blocks if b["slot"] != slot]
        if len(rig.blocks) == before:
            raise HTTPException(404, f"no block in slot {slot}")
        state.recalc_cpu()
        return {"removed": slot}

    @app.post("/api/v1/rigs/{rig_id}/reorder")
    def reorder(rig_id: int, payload: dict = Body(...)) -> dict:
        try:
            rig = state.require(rig_id)
        except KeyError:
            raise HTTPException(404, f"no rig {rig_id}")
        order = payload.get("order") or []
        position = {slot: index + 1 for index, slot in enumerate(order)}
        for block in rig.blocks:
            block["slot"] = position.get(block["slot"], block["slot"])
        rig.blocks.sort(key=lambda b: b["slot"])
        return rig.to_dict()

    @app.post("/api/v1/rigs/{rig_id}/scenes")
    def set_scene(rig_id: int, payload: dict = Body(...)) -> dict:
        try:
            rig = state.require(rig_id)
        except KeyError:
            raise HTTPException(404, f"no rig {rig_id}")
        name = payload.get("name")
        if not name:
            raise HTTPException(400, "a scene needs a name")
        rig.scenes = [s for s in rig.scenes if s.get("name") != name]
        rig.scenes.append(
            {
                "name": name,
                "states": payload.get("states", {}),
                "footswitch": payload.get("footswitch"),
            }
        )
        return rig.to_dict()

    @app.websocket("/api/v1/ws")
    async def live(websocket: WebSocket) -> None:
        import asyncio

        await websocket.accept()
        try:
            await websocket.send_text(
                json.dumps({"type": "hello", "firmware": state.firmware, "simulated": True})
            )
            while True:
                try:
                    raw = await asyncio.wait_for(websocket.receive_text(), timeout=2.0)
                except asyncio.TimeoutError:
                    state.recalc_cpu()
                    await websocket.send_text(
                        json.dumps({"type": "cpu", "value": state.cpu})
                    )
                    continue

                try:
                    message = json.loads(raw)
                except json.JSONDecodeError:
                    continue

                kind = message.get("type")
                payload = message.get("payload") or {}
                if kind == "select_rig":
                    state.current_rig = int(payload.get("id", state.current_rig))
                    await websocket.send_text(
                        json.dumps({"type": "rig_changed", "id": state.current_rig})
                    )
                elif kind == "footswitch":
                    await websocket.send_text(
                        json.dumps(
                            {"type": "footswitch", "number": payload.get("number")}
                        )
                    )
        except (WebSocketDisconnect, RuntimeError):
            return

    return app


# --------------------------------------------------------------------------
# replay
# --------------------------------------------------------------------------


def replay_app(recording_path: str | Path):
    """Serve the responses from a real captured session.

    Once ``rigsmith protocol record`` has captured genuine traffic, this
    turns that log into a server. At that point the simulator stops being a
    guess about the device and becomes a faithful fixture of it - which is
    what makes it worth keeping around afterwards.
    """
    _require_web()
    path = Path(recording_path)
    if not path.exists():
        raise NotFound(f"no recording at {path}")

    responses: dict[tuple[str, str], list[dict]] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if entry.get("kind") != "http":
            continue
        key = (entry.get("method", "GET"), entry.get("path", "/"))
        responses.setdefault(key, []).append(entry)

    if not responses:
        raise Invalid(
            f"{path.name} contains no HTTP exchanges to replay",
            hint="Record a session with:  rigsmith protocol record",
        )

    app = FastAPI(title="HeadRush replay", docs_url=None, redoc_url=None)
    counters: dict[tuple[str, str], int] = {}

    @app.api_route(
        "/{path:path}",
        methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"],
    )
    async def serve(request: Request, path: str):
        key = (request.method, "/" + path)
        options = responses.get(key)
        if not options:
            return JSONResponse(
                {
                    "error": "not in the recording",
                    "hint": f"{request.method} /{path} was never captured",
                },
                status_code=404,
            )
        # Cycle through repeats, so a sequence of differing responses to the
        # same endpoint replays in the order it actually happened.
        index = counters.get(key, 0)
        counters[key] = index + 1
        entry = options[min(index, len(options) - 1)]

        body = entry.get("response_body", "")
        content_type = entry.get("headers", {}).get("content-type", "application/json")
        return Response(
            content=body,
            status_code=entry.get("status", 200),
            media_type=content_type.split(";")[0],
        )

    return app


# --------------------------------------------------------------------------
# running it
# --------------------------------------------------------------------------


def run_simulator(
    *,
    host: str = "127.0.0.1",
    port: int = 0,
    recording: str | Path | None = None,
    on_ready=None,
) -> None:
    """Serve the simulated device until interrupted."""
    _require_web()
    import socket

    import uvicorn

    if port == 0:
        with socket.socket() as probe:
            probe.bind((host, 0))
            port = probe.getsockname()[1]

    app = replay_app(recording) if recording else build_app()
    url = f"http://{host}:{port}"
    if on_ready:
        on_ready(url)

    server = uvicorn.Server(
        uvicorn.Config(app, host=host, port=port, log_level="warning")
    )
    try:
        server.run()
    except KeyboardInterrupt:  # pragma: no cover - interactive
        pass
