from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from typing import List, Dict, Optional
from pathlib import Path
from datetime import date, timedelta, datetime
import sqlite3
import json

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "schedules.db"

app = FastAPI()
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))


def get_db_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_db_connection()
    cur = conn.cursor()

    cur.execute("""
        CREATE TABLE IF NOT EXISTS schedules (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            schedule_date TEXT NOT NULL,
            time_slot TEXT NOT NULL,
            title TEXT NOT NULL,
            note TEXT NOT NULL DEFAULT '',
            creator TEXT NOT NULL,
            claimed_by TEXT,
            is_done INTEGER NOT NULL DEFAULT 0
        )
    """)

    cur.execute("""
        CREATE INDEX IF NOT EXISTS idx_schedules_date_time
        ON schedules (schedule_date, time_slot)
    """)

    conn.commit()
    conn.close()


@app.on_event("startup")
def on_startup():
    init_db()


def serialize_row(row: sqlite3.Row) -> Dict:
    return {
        "id": row["id"],
        "schedule_date": row["schedule_date"],
        "time_slot": row["time_slot"],
        "title": row["title"],
        "note": row["note"],
        "creator": row["creator"],
        "claimed_by": row["claimed_by"],
        "is_done": bool(row["is_done"]),
    }


def get_start_of_week(target: date) -> date:
    return target - timedelta(days=target.weekday())


def parse_week_start(week_start_str: Optional[str]) -> date:
    if week_start_str:
        parsed = datetime.strptime(week_start_str, "%Y-%m-%d").date()
        return get_start_of_week(parsed)
    return get_start_of_week(date.today())


def get_week_dates(week_start: date) -> List[Dict]:
    day_names = ["월", "화", "수", "목", "금", "토", "일"]
    result = []
    for i in range(7):
        current = week_start + timedelta(days=i)
        result.append({
            "date": current.isoformat(),
            "label": day_names[i],
            "display": f"{current.month}/{current.day}",
        })
    return result


def get_time_slots() -> List[str]:
    return [f"{i}교시" for i in range(1, 9)]


def get_week_schedules(week_dates: List[Dict]) -> List[Dict]:
    date_strings = [d["date"] for d in week_dates]

    conn = get_db_connection()
    cur = conn.cursor()

    placeholders = ",".join(["?"] * len(date_strings))
    cur.execute(f"""
        SELECT id, schedule_date, time_slot, title, note, creator, claimed_by, is_done
        FROM schedules
        WHERE schedule_date IN ({placeholders})
        ORDER BY schedule_date ASC, time_slot ASC, id DESC
    """, date_strings)

    rows = cur.fetchall()
    conn.close()
    return [serialize_row(row) for row in rows]


def get_schedule_by_id(schedule_id: int) -> Optional[Dict]:
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("""
        SELECT id, schedule_date, time_slot, title, note, creator, claimed_by, is_done
        FROM schedules
        WHERE id = ?
    """, (schedule_id,))
    row = cur.fetchone()
    conn.close()
    return serialize_row(row) if row else None


def create_schedule_in_db(schedule_date: str, time_slot: str, title: str, note: str, creator: str) -> Dict:
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO schedules (schedule_date, time_slot, title, note, creator, claimed_by, is_done)
        VALUES (?, ?, ?, ?, ?, NULL, 0)
    """, (schedule_date, time_slot, title, note, creator))
    schedule_id = cur.lastrowid
    conn.commit()
    conn.close()
    return get_schedule_by_id(schedule_id)


def update_schedule_in_db(schedule_id: int, user: str, schedule_date: str, time_slot: str, title: str, note: str) -> Dict:
    conn = get_db_connection()
    cur = conn.cursor()

    cur.execute("""
        SELECT creator
        FROM schedules
        WHERE id = ?
    """, (schedule_id,))
    row = cur.fetchone()

    if row is None:
        conn.close()
        raise ValueError("해당 스케줄이 없습니다.")

    if row["creator"] != user:
        conn.close()
        raise ValueError("등록자만 수정할 수 있습니다.")

    cur.execute("""
        UPDATE schedules
        SET schedule_date = ?, time_slot = ?, title = ?, note = ?
        WHERE id = ?
    """, (schedule_date, time_slot, title, note, schedule_id))

    conn.commit()
    conn.close()
    return get_schedule_by_id(schedule_id)


def delete_schedule_in_db(schedule_id: int, user: str) -> None:
    conn = get_db_connection()
    cur = conn.cursor()

    cur.execute("""
        SELECT creator
        FROM schedules
        WHERE id = ?
    """, (schedule_id,))
    row = cur.fetchone()

    if row is None:
        conn.close()
        raise ValueError("해당 스케줄이 없습니다.")

    if row["creator"] != user:
        conn.close()
        raise ValueError("등록자만 삭제할 수 있습니다.")

    cur.execute("""
        DELETE FROM schedules
        WHERE id = ?
    """, (schedule_id,))

    conn.commit()
    conn.close()


def claim_schedule_in_db(schedule_id: int, user: str) -> Dict:
    conn = get_db_connection()
    cur = conn.cursor()

    cur.execute("""
        SELECT id, claimed_by, is_done
        FROM schedules
        WHERE id = ?
    """, (schedule_id,))
    row = cur.fetchone()

    if row is None:
        conn.close()
        raise ValueError("해당 스케줄이 없습니다.")

    if row["is_done"]:
        conn.close()
        raise ValueError("완료된 스케줄은 선점할 수 없습니다.")

    if row["claimed_by"] is not None:
        conn.close()
        raise ValueError("이미 다른 사람이 선점한 스케줄입니다.")

    cur.execute("""
        UPDATE schedules
        SET claimed_by = ?
        WHERE id = ? AND claimed_by IS NULL AND is_done = 0
    """, (user, schedule_id))

    if cur.rowcount == 0:
        conn.close()
        raise ValueError("이미 다른 사람이 선점했거나 완료된 스케줄입니다.")

    conn.commit()
    conn.close()
    return get_schedule_by_id(schedule_id)


def unclaim_schedule_in_db(schedule_id: int, user: str) -> Dict:
    conn = get_db_connection()
    cur = conn.cursor()

    cur.execute("""
        SELECT id, claimed_by
        FROM schedules
        WHERE id = ?
    """, (schedule_id,))
    row = cur.fetchone()

    if row is None:
        conn.close()
        raise ValueError("해당 스케줄이 없습니다.")

    if row["claimed_by"] != user:
        conn.close()
        raise ValueError("본인이 선점한 스케줄만 취소할 수 있습니다.")

    cur.execute("""
        UPDATE schedules
        SET claimed_by = NULL, is_done = 0
        WHERE id = ? AND claimed_by = ?
    """, (schedule_id, user))

    if cur.rowcount == 0:
        conn.close()
        raise ValueError("선점 취소에 실패했습니다.")

    conn.commit()
    conn.close()
    return get_schedule_by_id(schedule_id)


def complete_schedule_in_db(schedule_id: int, user: str) -> Dict:
    conn = get_db_connection()
    cur = conn.cursor()

    cur.execute("""
        SELECT id, claimed_by
        FROM schedules
        WHERE id = ?
    """, (schedule_id,))
    row = cur.fetchone()

    if row is None:
        conn.close()
        raise ValueError("해당 스케줄이 없습니다.")

    if row["claimed_by"] != user:
        conn.close()
        raise ValueError("선점한 사람만 완료 처리할 수 있습니다.")

    cur.execute("""
        UPDATE schedules
        SET is_done = 1
        WHERE id = ? AND claimed_by = ?
    """, (schedule_id, user))

    if cur.rowcount == 0:
        conn.close()
        raise ValueError("완료 처리에 실패했습니다.")

    conn.commit()
    conn.close()
    return get_schedule_by_id(schedule_id)


class ConnectionManager:
    def __init__(self):
        self.active_connections: List[WebSocket] = []

    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        self.active_connections.append(websocket)

    def disconnect(self, websocket: WebSocket):
        if websocket in self.active_connections:
            self.active_connections.remove(websocket)

    async def broadcast_json(self, payload: dict):
        dead_connections = []
        for connection in self.active_connections:
            try:
                await connection.send_text(json.dumps(payload, ensure_ascii=False))
            except Exception:
                dead_connections.append(connection)

        for connection in dead_connections:
            self.disconnect(connection)


manager = ConnectionManager()


@app.get("/", response_class=HTMLResponse)
async def home(request: Request, week_start: Optional[str] = None):
    base_week_start = parse_week_start(week_start)
    week_dates = get_week_dates(base_week_start)
    time_slots = get_time_slots()

    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={
            "week_dates": week_dates,
            "time_slots": time_slots,
            "week_start": base_week_start.isoformat(),
        }
    )


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await manager.connect(websocket)

    try:
        while True:
            raw = await websocket.receive_text()
            message = json.loads(raw)
            msg_type = message.get("type")

            try:
                if msg_type == "load_week":
                    week_start = parse_week_start(message.get("week_start"))
                    week_dates = get_week_dates(week_start)
                    schedules = get_week_schedules(week_dates)

                    await websocket.send_text(json.dumps({
                        "type": "week_data",
                        "week_start": week_start.isoformat(),
                        "week_dates": week_dates,
                        "time_slots": get_time_slots(),
                        "schedules": schedules,
                    }, ensure_ascii=False))

                elif msg_type == "create_schedule":
                    creator = message.get("creator", "").strip()
                    title = message.get("title", "").strip()
                    note = message.get("note", "").strip()
                    schedule_date = message.get("schedule_date", "").strip()
                    time_slot = message.get("time_slot", "").strip()

                    if not creator or not title or not schedule_date or not time_slot:
                        await websocket.send_text(json.dumps({
                            "type": "error",
                            "message": "이름, 날짜, 교시, 스케줄 제목은 필수입니다."
                        }, ensure_ascii=False))
                        continue

                    datetime.strptime(schedule_date, "%Y-%m-%d")

                    new_item = create_schedule_in_db(
                        schedule_date=schedule_date,
                        time_slot=time_slot,
                        title=title,
                        note=note,
                        creator=creator,
                    )

                    await manager.broadcast_json({
                        "type": "schedule_created",
                        "schedule": new_item
                    })

                elif msg_type == "update_schedule":
                    user = message.get("user", "").strip()
                    schedule_id = int(message.get("id"))
                    schedule_date = message.get("schedule_date", "").strip()
                    time_slot = message.get("time_slot", "").strip()
                    title = message.get("title", "").strip()
                    note = message.get("note", "").strip()

                    if not user or not title or not schedule_date or not time_slot:
                        await websocket.send_text(json.dumps({
                            "type": "error",
                            "message": "이름, 날짜, 교시, 스케줄 제목은 필수입니다."
                        }, ensure_ascii=False))
                        continue

                    datetime.strptime(schedule_date, "%Y-%m-%d")

                    updated_item = update_schedule_in_db(
                        schedule_id=schedule_id,
                        user=user,
                        schedule_date=schedule_date,
                        time_slot=time_slot,
                        title=title,
                        note=note,
                    )

                    await manager.broadcast_json({
                        "type": "schedule_updated",
                        "schedule": updated_item
                    })

                elif msg_type == "delete_schedule":
                    user = message.get("user", "").strip()
                    schedule_id = int(message.get("id"))

                    if not user:
                        await websocket.send_text(json.dumps({
                            "type": "error",
                            "message": "로그인 후 이용해줘."
                        }, ensure_ascii=False))
                        continue

                    deleted_schedule = get_schedule_by_id(schedule_id)
                    delete_schedule_in_db(schedule_id, user)

                    await manager.broadcast_json({
                        "type": "schedule_deleted",
                        "schedule_id": schedule_id,
                        "schedule_date": deleted_schedule["schedule_date"] if deleted_schedule else None
                    })

                elif msg_type == "claim_schedule":
                    schedule_id = int(message.get("id"))
                    user = message.get("user", "").strip()

                    if not user:
                        await websocket.send_text(json.dumps({
                            "type": "error",
                            "message": "로그인 후 이용해줘."
                        }, ensure_ascii=False))
                        continue

                    updated_item = claim_schedule_in_db(schedule_id, user)

                    await manager.broadcast_json({
                        "type": "schedule_updated",
                        "schedule": updated_item
                    })

                elif msg_type == "unclaim_schedule":
                    schedule_id = int(message.get("id"))
                    user = message.get("user", "").strip()

                    if not user:
                        await websocket.send_text(json.dumps({
                            "type": "error",
                            "message": "로그인 후 이용해줘."
                        }, ensure_ascii=False))
                        continue

                    updated_item = unclaim_schedule_in_db(schedule_id, user)

                    await manager.broadcast_json({
                        "type": "schedule_updated",
                        "schedule": updated_item
                    })

                elif msg_type == "complete_schedule":
                    schedule_id = int(message.get("id"))
                    user = message.get("user", "").strip()

                    if not user:
                        await websocket.send_text(json.dumps({
                            "type": "error",
                            "message": "로그인 후 이용해줘."
                        }, ensure_ascii=False))
                        continue

                    updated_item = complete_schedule_in_db(schedule_id, user)

                    await manager.broadcast_json({
                        "type": "schedule_updated",
                        "schedule": updated_item
                    })

            except ValueError as e:
                await websocket.send_text(json.dumps({
                    "type": "error",
                    "message": str(e)
                }, ensure_ascii=False))

    except WebSocketDisconnect:
        manager.disconnect(websocket)