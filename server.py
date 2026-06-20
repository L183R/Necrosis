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

TRISTARM_FINAL_XP_REWARD = 250_000
TRISTARM_TOTAL_FLOORS = 16
TRISTARM_PORTAL_KILL_REQUIREMENT = 10_000
MANSION_UMBRA_FIRST_CLEAR_SERUM = 25
MANSION_UMBRA_REPEAT_CLEAR_SERUM = 8
MANSION_UMBRA_SAMPLE_SERUM = 2
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


class TristarmPayload(AuthPayload):
    floor: int | None = None
    xp_bank: int | None = None
    enemies_defeated: int | None = None
    boss_id: str | None = None


class MansionUmbraPayload(AuthPayload):
    map_seed: int | None = None
    player_position: Dict[str, Any] | None = None
    keys: list[str] | None = None
    opened_doors: list[str] | None = None
    activated_mechanisms: list[str] | None = None
    serum_found: int | None = None
    escaped: bool | None = None


def _default_tristarm() -> Dict[str, Any]:
    return {
        "unlocked": False,
        "completed": False,
        "completion_count": 0,
        "best_floor": 0,
        "best_xp_bank": 0,
        "active_run": False,
        "current_floor": 1,
        "xp_bank": 0,
        "final_reward_claimed_for_current_run": False,
        "floor_cleared": False,
        "run_id": None,
        "started_at": None,
        "last_result": None,
    }



def _default_mansion_umbra() -> Dict[str, Any]:
    return {
        "unlocked": False, "completed": False, "completion_count": 0, "active_run": False,
        "map_seed": None, "player_position": None, "keys": [], "opened_doors": [],
        "activated_mechanisms": [], "serum_found": 0,
        "subject_zero_state": {"hp": 0, "downed": False, "position": None, "regeneration_end": None},
        "first_clear_reward_claimed": False, "started_at": None, "last_result": None,
        "stats": {"best_time_ms": None, "rooms_explored": 0, "keys_found": 0, "subject_zero_downs": 0, "serum_total_obtained": 0, "special_enemies_defeated": 0},
    }


def _ensure_mansion_umbra(save: Dict[str, Any]) -> Dict[str, Any]:
    mansion = save.get("mansion_umbra") if isinstance(save.get("mansion_umbra"), dict) else {}
    merged = {**_default_mansion_umbra(), **mansion}
    merged["subject_zero_state"] = {**_default_mansion_umbra()["subject_zero_state"], **(mansion.get("subject_zero_state") if isinstance(mansion.get("subject_zero_state"), dict) else {})}
    merged["stats"] = {**_default_mansion_umbra()["stats"], **(mansion.get("stats") if isinstance(mansion.get("stats"), dict) else {})}
    for key in ("completion_count", "serum_found"):
        merged[key] = max(0, int(merged.get(key) or 0))
    for key in ("keys", "opened_doors", "activated_mechanisms"):
        merged[key] = list(dict.fromkeys(merged.get(key) if isinstance(merged.get(key), list) else []))
    merged["unlocked"] = _has_building(save, "portal_estrafalario")
    save["mansion_umbra"] = merged
    save.setdefault("codex", {}) if isinstance(save.get("codex"), dict) else save.update({"codex": {}})
    for group in ("portals", "modes", "enemies"):
        save["codex"].setdefault(group, {})
    return merged

def _has_building(save: Dict[str, Any], building_id: str) -> bool:
    base = save.get("base") if isinstance(save.get("base"), dict) else {}
    return any(isinstance(b, dict) and b.get("buildingId") == building_id for b in base.values())


def _ensure_tristarm(save: Dict[str, Any]) -> Dict[str, Any]:
    tristarm = save.get("tristarm") if isinstance(save.get("tristarm"), dict) else {}
    merged = {**_default_tristarm(), **tristarm}
    for key in ("completion_count", "best_floor", "best_xp_bank", "current_floor", "xp_bank"):
        merged[key] = max(0, int(merged.get(key) or 0))
    if merged["current_floor"] < 1:
        merged["current_floor"] = 1
    merged["current_floor"] = min(TRISTARM_TOTAL_FLOORS, merged["current_floor"])
    merged["unlocked"] = _has_building(save, "portal_estrafalario")
    save["tristarm"] = merged
    codex = save.setdefault("codex", {}) if isinstance(save.get("codex"), dict) else {}
    codex.setdefault("portals", {})
    codex.setdefault("modes", {})
    codex.setdefault("enemies", {})
    save["codex"] = codex
    return merged


def _persist_user_save(user: str, save: Dict[str, Any]) -> Dict[str, Any]:
    accounts = load_accounts()
    accounts[user]["save"] = normalize_account_save(save)
    accounts[user]["updatedAt"] = int(time.time() * 1000)
    write_accounts(accounts)
    return accounts[user]["save"]


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



def normalize_account_save(save: Dict[str, Any]) -> Dict[str, Any]:
    """Server-side compatibility/defaults for old NECROSIS saves."""
    if not isinstance(save, dict):
        return {}
    out = dict(save)
    out["totalEnemyKills"] = max(0, int(out.get("totalEnemyKills") or 0))
    portals = out.get("portals") if isinstance(out.get("portals"), dict) else {}
    portals.setdefault("unlocked", {})
    portals.setdefault("completed", {})
    portals.setdefault("tempXpBank", {"portalId": None, "floor": 0, "xp": 0})
    out["portals"] = portals
    _ensure_tristarm(out)
    _ensure_mansion_umbra(out)
    resources = out.get("resources") if isinstance(out.get("resources"), dict) else {}
    resources["cultureSerum"] = max(0, int(resources.get("cultureSerum") or 0))
    resources["mutagenicSerum"] = max(0, int(resources.get("mutagenicSerum") or 0))
    resources["slugParts"] = max(0, int(resources.get("slugParts") or 0))
    out["resources"] = resources
    out.setdefault("walkerMutations", {"hp": 0, "damage": 0, "speed": 0, "regen": 0, "resistance": 0, "deathExplosion": 0, "behavior": 0})
    out.setdefault("uniqueRewards", {"umbraFirst": False, "steelFrontFirst": False, "tristarmFirst": False})
    slug = out.get("slug") if isinstance(out.get("slug"), dict) else {}
    slug.setdefault("owned", False)
    slug["inventoryLevel"] = max(0, int(slug.get("inventoryLevel") or 0))
    slug["screensLevel"] = max(0, int(slug.get("screensLevel") or 0))
    slug["noiseLevel"] = max(0, int(slug.get("noiseLevel") or 0))
    slug.setdefault("artillery", False)
    out["slug"] = slug
    codex = out.get("codex") if isinstance(out.get("codex"), dict) else {}
    codex.setdefault("portals", {})
    out["codex"] = codex
    return out

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
    return {"ok": True, "user": user, "save": normalize_account_save(account.get("save")) if account.get("save") else None}


@app.post("/api/save")
async def save_game(payload: SavePayload):
    user = _clean_user(payload.user)
    password = _clean_password(payload.password)
    accounts = load_accounts()
    require_account(user, password)
    incoming = normalize_account_save(payload.save)
    current = normalize_account_save(accounts[user].get("save") or {})
    current_tristarm = current.get("tristarm", {})
    if current_tristarm.get("active_run"):
        # Tristarm economy is server-authoritative while a run is active.
        # Regular saves may persist inventory/ammo, but cannot overwrite the bank/floor/reward flags.
        incoming["tristarm"] = current_tristarm
    current_mansion = current.get("mansion_umbra", {})
    if current_mansion.get("active_run"):
        incoming["mansion_umbra"] = current_mansion
    accounts[user]["save"] = normalize_account_save(incoming)
    accounts[user]["updatedAt"] = int(time.time() * 1000)
    write_accounts(accounts)
    return {"ok": True}



def _require_tristarm_access(save: Dict[str, Any]):
    tristarm = _ensure_tristarm(save)
    if not _has_building(save, "portal_estrafalario"):
        raise HTTPException(status_code=403, detail="Tristarm requiere construir el Portal estrafalario (10.000 bajas totales).")
    return tristarm


@app.post("/api/tristarm/start")
async def tristarm_start(payload: TristarmPayload):
    user = _clean_user(payload.user); password = _clean_password(payload.password)
    account = require_account(user, password)
    save = normalize_account_save(account.get("save") or {})
    tristarm = _require_tristarm_access(save)
    if tristarm.get("active_run"):
        raise HTTPException(status_code=409, detail="Ya hay una run de Tristarm activa.")
    tristarm.update({"active_run": True, "current_floor": 1, "xp_bank": 0, "final_reward_claimed_for_current_run": False, "floor_cleared": False, "run_id": secrets.token_hex(8), "started_at": int(time.time()*1000), "last_result": None})
    save["codex"]["portals"]["tristarm"] = "played"
    save["codex"]["modes"]["tristarm"] = "played"
    return {"ok": True, "save": _persist_user_save(user, save)}


@app.post("/api/tristarm/sync")
async def tristarm_sync(payload: TristarmPayload):
    user = _clean_user(payload.user); password = _clean_password(payload.password)
    account = require_account(user, password)
    save = normalize_account_save(account.get("save") or {})
    tristarm = _require_tristarm_access(save)
    if not tristarm.get("active_run"):
        raise HTTPException(status_code=409, detail="No hay una run de Tristarm activa.")
    floor = int(payload.floor or tristarm["current_floor"])
    if floor < 1 or floor > TRISTARM_TOTAL_FLOORS or floor < int(tristarm["current_floor"]):
        raise HTTPException(status_code=400, detail="Piso de Tristarm inválido.")
    tristarm["current_floor"] = floor
    tristarm["xp_bank"] = max(int(tristarm.get("xp_bank") or 0), max(0, int(payload.xp_bank or 0)))
    tristarm["best_floor"] = max(tristarm["best_floor"], floor)
    tristarm["best_xp_bank"] = max(tristarm["best_xp_bank"], tristarm["xp_bank"])
    return {"ok": True, "save": _persist_user_save(user, save)}


@app.post("/api/tristarm/retire")
async def tristarm_retire(payload: TristarmPayload):
    user = _clean_user(payload.user); password = _clean_password(payload.password)
    account = require_account(user, password)
    save = normalize_account_save(account.get("save") or {})
    tristarm = _require_tristarm_access(save)
    if not tristarm.get("active_run"):
        raise HTTPException(status_code=409, detail="No hay una run activa para retirarse.")
    bank = max(int(tristarm.get("xp_bank") or 0), max(0, int(payload.xp_bank or 0)))
    delivered = bank // 2
    save["freeXP"] = max(0, int(save.get("freeXP") or 0)) + delivered
    tristarm.update({"active_run": False, "xp_bank": 0, "floor_cleared": False, "final_reward_claimed_for_current_run": False, "last_result": {"type":"retire","xp_delivered":delivered,"xp_lost":bank-delivered}})
    return {"ok": True, "xp_delivered": delivered, "save": _persist_user_save(user, save)}


@app.post("/api/tristarm/death")
async def tristarm_death(payload: TristarmPayload):
    user = _clean_user(payload.user); password = _clean_password(payload.password)
    account = require_account(user, password)
    save = normalize_account_save(account.get("save") or {})
    tristarm = _ensure_tristarm(save)
    lost = int(tristarm.get("xp_bank") or 0)
    tristarm.update({"active_run": False, "xp_bank": 0, "floor_cleared": False, "final_reward_claimed_for_current_run": False, "last_result": {"type":"death","xp_lost":lost}})
    return {"ok": True, "xp_lost": lost, "save": _persist_user_save(user, save)}


@app.post("/api/tristarm/victory")
async def tristarm_victory(payload: TristarmPayload):
    user = _clean_user(payload.user); password = _clean_password(payload.password)
    account = require_account(user, password)
    save = normalize_account_save(account.get("save") or {})
    tristarm = _require_tristarm_access(save)
    if not tristarm.get("active_run") or int(tristarm.get("current_floor") or 0) != TRISTARM_TOTAL_FLOORS:
        raise HTTPException(status_code=409, detail="La recompensa final requiere una run activa en el piso 16.")
    if tristarm.get("final_reward_claimed_for_current_run"):
        raise HTTPException(status_code=409, detail="La recompensa final de esta run ya fue cobrada.")
    if payload.boss_id != "tristarm_final_demon":
        raise HTTPException(status_code=403, detail="Victoria inválida: falta derrotar al jefe final.")
    bank = max(int(tristarm.get("xp_bank") or 0), max(0, int(payload.xp_bank or 0)))
    total = bank + TRISTARM_FINAL_XP_REWARD
    save["freeXP"] = max(0, int(save.get("freeXP") or 0)) + total
    save.setdefault("portals", {}).setdefault("completed", {})["tristarm"] = True
    save.setdefault("uniqueRewards", {})["tristarmFirst"] = True
    save["codex"]["portals"]["tristarm"] = "defeated"
    save["codex"]["enemies"]["tristarm_final_demon"] = "defeated"
    tristarm.update({"active_run": False, "completed": True, "completion_count": int(tristarm.get("completion_count") or 0)+1, "best_floor": TRISTARM_TOTAL_FLOORS, "best_xp_bank": max(int(tristarm.get("best_xp_bank") or 0), bank), "xp_bank": 0, "floor_cleared": False, "final_reward_claimed_for_current_run": True, "last_result": {"type":"victory","xp_bank":bank,"final_reward":TRISTARM_FINAL_XP_REWARD,"xp_delivered":total}})
    return {"ok": True, "xp_delivered": total, "final_reward": TRISTARM_FINAL_XP_REWARD, "save": _persist_user_save(user, save)}


def _require_mansion_umbra_access(save: Dict[str, Any]):
    mansion = _ensure_mansion_umbra(save)
    if not _has_building(save, "portal_estrafalario"):
        raise HTTPException(status_code=403, detail="Mansión Umbra requiere construir el Portal estrafalario (10.000 bajas totales).")
    return mansion


@app.post("/api/mansion-umbra/start")
async def mansion_umbra_start(payload: MansionUmbraPayload):
    user = _clean_user(payload.user); password = _clean_password(payload.password)
    account = require_account(user, password)
    save = normalize_account_save(account.get("save") or {})
    mansion = _require_mansion_umbra_access(save)
    if mansion.get("active_run"):
        seed = int(mansion.get("map_seed") or payload.map_seed or secrets.randbelow(2_147_483_647))
        mansion["map_seed"] = seed
        return {"ok": True, "resumed": True, "map_seed": seed, "save": _persist_user_save(user, save)}
    seed = int(payload.map_seed or secrets.randbelow(2_147_483_647))
    mansion.update({"active_run": True, "map_seed": seed, "player_position": None, "keys": [], "opened_doors": [], "activated_mechanisms": [], "serum_found": 0, "subject_zero_state": {"hp": 1800, "downed": False, "position": None, "regeneration_end": None}, "started_at": int(time.time()*1000), "last_result": None})
    save["codex"]["portals"]["mansion_umbra"] = "played"
    save["codex"]["modes"]["mansion_umbra"] = "played"
    return {"ok": True, "map_seed": seed, "save": _persist_user_save(user, save)}


@app.post("/api/mansion-umbra/sync")
async def mansion_umbra_sync(payload: MansionUmbraPayload):
    user = _clean_user(payload.user); password = _clean_password(payload.password)
    account = require_account(user, password)
    save = normalize_account_save(account.get("save") or {})
    mansion = _require_mansion_umbra_access(save)
    if not mansion.get("active_run"):
        raise HTTPException(status_code=409, detail="No hay una run de Mansión Umbra activa.")
    allowed_keys = {"rusty_key", "medical_key", "security_key", "laboratory_key"}
    allowed_mechs = {"generator", "security_panel", "decontamination", "lab_terminal", "exit_switch"}
    mansion["keys"] = [k for k in dict.fromkeys(payload.keys or mansion.get("keys", [])) if k in allowed_keys]
    mansion["opened_doors"] = [str(d)[:48] for d in dict.fromkeys(payload.opened_doors or mansion.get("opened_doors", []))]
    mansion["activated_mechanisms"] = [m for m in dict.fromkeys(payload.activated_mechanisms or mansion.get("activated_mechanisms", [])) if m in allowed_mechs]
    mansion["serum_found"] = max(int(mansion.get("serum_found") or 0), max(0, int(payload.serum_found or 0)))
    if payload.player_position and all(k in payload.player_position for k in ("x", "y")):
        x, y = float(payload.player_position["x"]), float(payload.player_position["y"])
        if 0 <= x <= 4096 and 0 <= y <= 4096:
            mansion["player_position"] = {"x": x, "y": y}
    return {"ok": True, "save": _persist_user_save(user, save)}


@app.post("/api/mansion-umbra/abandon")
async def mansion_umbra_abandon(payload: MansionUmbraPayload):
    user = _clean_user(payload.user); password = _clean_password(payload.password)
    account = require_account(user, password)
    save = normalize_account_save(account.get("save") or {})
    mansion = _ensure_mansion_umbra(save)
    mansion.update({"active_run": False, "keys": [], "opened_doors": [], "activated_mechanisms": [], "serum_found": 0, "last_result": {"type": "abandon"}})
    return {"ok": True, "save": _persist_user_save(user, save)}


@app.post("/api/mansion-umbra/victory")
async def mansion_umbra_victory(payload: MansionUmbraPayload):
    user = _clean_user(payload.user); password = _clean_password(payload.password)
    account = require_account(user, password)
    save = normalize_account_save(account.get("save") or {})
    mansion = _require_mansion_umbra_access(save)
    if not mansion.get("active_run"):
        raise HTTPException(status_code=409, detail="No hay una run de Mansión Umbra activa.")
    keys = set(payload.keys or mansion.get("keys", []))
    mechs = set(payload.activated_mechanisms or mansion.get("activated_mechanisms", []))
    required_keys = {"rusty_key", "medical_key", "security_key", "laboratory_key"}
    required_mechs = {"generator", "security_panel", "decontamination", "lab_terminal", "exit_switch"}
    if not required_keys.issubset(keys) or not required_mechs.issubset(mechs) or not payload.escaped:
        raise HTTPException(status_code=403, detail="Victoria inválida: faltan llaves, mecanismos o escape verificado.")
    first = not mansion.get("first_clear_reward_claimed")
    clear_serum = MANSION_UMBRA_FIRST_CLEAR_SERUM if first else MANSION_UMBRA_REPEAT_CLEAR_SERUM
    run_serum = max(int(mansion.get("serum_found") or 0), max(0, int(payload.serum_found or 0)))
    total_serum = clear_serum + run_serum
    resources = save.setdefault("resources", {})
    resources["mutagenicSerum"] = max(0, int(resources.get("mutagenicSerum") or 0)) + total_serum
    mansion["stats"]["serum_total_obtained"] = int(mansion["stats"].get("serum_total_obtained") or 0) + total_serum
    mansion.update({"active_run": False, "completed": True, "completion_count": int(mansion.get("completion_count") or 0)+1, "first_clear_reward_claimed": True, "keys": [], "opened_doors": [], "activated_mechanisms": [], "serum_found": 0, "last_result": {"type": "victory", "clear_serum": clear_serum, "run_serum": run_serum, "serum_delivered": total_serum}})
    save.setdefault("portals", {}).setdefault("completed", {})["mansion_umbra"] = True
    save["codex"]["portals"]["mansion_umbra"] = "defeated"
    save["codex"]["enemies"]["subject_zero"] = "seen"
    return {"ok": True, "serum_delivered": total_serum, "clear_serum": clear_serum, "run_serum": run_serum, "save": _persist_user_save(user, save)}

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
