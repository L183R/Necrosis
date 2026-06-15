import asyncio
import json
import secrets
import time
from pathlib import Path
from typing import Dict, Any

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
import uvicorn

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"

app = FastAPI(title="NECROSIS Python Multiplayer")

# rooms[room][player_id] = {"ws": WebSocket, "state": dict}
rooms: Dict[str, Dict[str, Dict[str, Any]]] = {}


@app.get("/")
async def index():
    return FileResponse(STATIC_DIR / "index.html")


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


async def broadcast_room(room: str):
    players = rooms.get(room, {})
    public_state = {
        pid: entry.get("state", {})
        for pid, entry in players.items()
        if entry.get("state")
    }
    dead = []
    message = json.dumps({"type": "state", "players": public_state}, ensure_ascii=False)
    for pid, entry in list(players.items()):
        ws = entry["ws"]
        try:
            await ws.send_text(message)
        except Exception:
            dead.append(pid)
    for pid in dead:
        players.pop(pid, None)


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()

    player_id = secrets.token_hex(4)
    name = (websocket.query_params.get("name") or "Jugador")[:24]
    room = (websocket.query_params.get("room") or "sala1")[:32]

    rooms.setdefault(room, {})[player_id] = {
        "ws": websocket,
        "state": {
            "id": player_id,
            "name": name,
            "room": room,
            "x": 144,
            "y": 144,
            "w": 28,
            "h": 28,
            "hp": 100,
            "maxHp": 100,
            "facing": 0,
            "floor": 1,
            "missionId": "?",
            "gameOver": False,
            "weapon": None,
            "t": int(time.time() * 1000),
        },
    }

    await websocket.send_text(json.dumps({"type": "hello", "id": player_id}, ensure_ascii=False))
    await broadcast_room(room)

    try:
        while True:
            raw = await websocket.receive_text()
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue

            if msg.get("type") != "player_state":
                continue

            state = rooms.get(room, {}).get(player_id, {}).get("state")
            if not state:
                continue

            # Solo aceptamos los campos necesarios. El cliente no puede pisar otros jugadores.
            for key in (
                "name", "x", "y", "w", "h", "hp", "maxHp", "facing",
                "floor", "missionId", "gameOver", "weapon", "t"
            ):
                if key in msg:
                    state[key] = msg[key]

            state["id"] = player_id
            state["room"] = room
            state["serverT"] = int(time.time() * 1000)
            await broadcast_room(room)

    except WebSocketDisconnect:
        pass
    finally:
        if room in rooms:
            rooms[room].pop(player_id, None)
            leave_msg = json.dumps({"type": "leave", "id": player_id}, ensure_ascii=False)
            for entry in list(rooms[room].values()):
                try:
                    await entry["ws"].send_text(leave_msg)
                except Exception:
                    pass
            if not rooms[room]:
                rooms.pop(room, None)


if __name__ == "__main__":
    uvicorn.run("server:app", host="0.0.0.0", port=8000, reload=False)
