import asyncio
import hashlib
import json
import secrets
import time
from pathlib import Path
from typing import Dict, Any

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from pydantic import BaseModel
from fastapi.staticfiles import StaticFiles
import uvicorn

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
DATA_FILE = BASE_DIR / "accounts.json"

app = FastAPI(title="NECROSIS Python Multiplayer")

# rooms[room][player_id] = {"ws": WebSocket, "state": dict}
rooms: Dict[str, Dict[str, Dict[str, Any]]] = {}


class AuthPayload(BaseModel):
    user: str
    password: str


class SavePayload(AuthPayload):
    save: Dict[str, Any]


def _clean_user(user: str) -> str:
    user = (user or "").strip()[:24]
    if len(user) < 3:
        raise HTTPException(status_code=400, detail="El usuario debe tener al menos 3 caracteres.")
    return user


def _clean_password(password: str) -> str:
    password = password or ""
    if len(password) < 4:
        raise HTTPException(status_code=400, detail="La contraseña debe tener al menos 4 caracteres.")
    return password


def load_accounts() -> Dict[str, Dict[str, Any]]:
    if not DATA_FILE.exists():
        return {}
    try:
        data = json.loads(DATA_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    return data if isinstance(data, dict) else {}


def write_accounts(accounts: Dict[str, Dict[str, Any]]) -> None:
    DATA_FILE.write_text(json.dumps(accounts, ensure_ascii=False, indent=2), encoding="utf-8")


def password_hash(password: str, salt: str) -> str:
    return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), 120_000).hex()


def make_password_record(password: str) -> Dict[str, str]:
    salt = secrets.token_hex(16)
    return {"salt": salt, "passwordHash": password_hash(password, salt)}


def password_matches(account: Dict[str, Any], password: str) -> bool:
    salt = account.get("salt")
    expected = account.get("passwordHash")
    if salt and expected:
        return secrets.compare_digest(expected, password_hash(password, salt))
    # Compatibility with old development data, if any exists.
    return secrets.compare_digest(account.get("password", ""), password)


def require_account(user: str, password: str) -> Dict[str, Any]:
    accounts = load_accounts()
    account = accounts.get(user)
    if not account or not password_matches(account, password):
        raise HTTPException(status_code=401, detail="Usuario o contraseña incorrectos.")
    return account


@app.post("/api/register")
async def register(payload: AuthPayload):
    user = _clean_user(payload.user)
    password = _clean_password(payload.password)
    accounts = load_accounts()
    if user in accounts:
        raise HTTPException(status_code=409, detail="Ese usuario ya existe. Iniciá sesión.")
    accounts[user] = {**make_password_record(password), "createdAt": int(time.time() * 1000), "save": None}
    write_accounts(accounts)
    return {"ok": True, "user": user, "save": None}


@app.post("/api/login")
async def login(payload: AuthPayload):
    user = _clean_user(payload.user)
    password = _clean_password(payload.password)
    account = require_account(user, password)
    return {"ok": True, "user": user, "save": account.get("save")}


@app.post("/api/save")
async def save_game(payload: SavePayload):
    user = _clean_user(payload.user)
    password = _clean_password(payload.password)
    accounts = load_accounts()
    require_account(user, password)
    accounts[user]["save"] = payload.save
    accounts[user]["updatedAt"] = int(time.time() * 1000)
    write_accounts(accounts)
    return {"ok": True}


@app.post("/api/reset")
async def reset_game(payload: AuthPayload):
    user = _clean_user(payload.user)
    password = _clean_password(payload.password)
    accounts = load_accounts()
    require_account(user, password)
    accounts[user]["save"] = None
    accounts[user]["updatedAt"] = int(time.time() * 1000)
    write_accounts(accounts)
    return {"ok": True, "save": None}


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
