import os
from datetime import datetime, timedelta, timezone
from typing import List, Optional, Dict, Any

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from bson import ObjectId

from database import db, create_document, get_documents

app = FastAPI(title="StepWize API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------- Utilities ----------

def oid(id_str: str) -> ObjectId:
    try:
        return ObjectId(id_str)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid id")


def serialize(doc: Dict[str, Any]):
    if not doc:
        return doc
    d = {**doc}
    _id = d.pop("_id", None)
    if _id is not None:
        d["id"] = str(_id)
    # Convert datetimes
    for k, v in list(d.items()):
        if isinstance(v, datetime):
            d[k] = v.isoformat()
        if isinstance(v, list):
            d[k] = [x.isoformat() if isinstance(x, datetime) else x for x in v]
    return d


# ---------- Health ----------

@app.get("/")
def read_root():
    return {"message": "StepWize backend is running"}


@app.get("/test")
def test_database():
    response = {
        "backend": "✅ Running",
        "database": "❌ Not Available",
        "database_url": "❌ Not Set",
        "database_name": "❌ Not Set",
        "connection_status": "Not Connected",
        "collections": [],
    }
    try:
        if db is not None:
            response["database"] = "✅ Available"
            response["database_url"] = "✅ Set" if os.getenv("DATABASE_URL") else "❌ Not Set"
            response["database_name"] = db.name if hasattr(db, "name") else "✅ Connected"
            response["connection_status"] = "Connected"
            try:
                response["collections"] = db.list_collection_names()
                response["database"] = "✅ Connected & Working"
            except Exception as e:
                response["database"] = f"⚠️ Connected but Error: {str(e)[:80]}"
    except Exception as e:
        response["database"] = f"❌ Error: {str(e)[:80]}"
    return response


# ---------- Auth (Mock OTP + Guest) ----------

class OTPRequest(BaseModel):
    email: str


class OTPVerify(BaseModel):
    email: str
    code: str


class GuestCreate(BaseModel):
    name: Optional[str] = "Guest"


def _session_token() -> str:
    return ObjectId().__str__() + ObjectId().__str__()


@app.post("/auth/otp/request")
def request_otp(payload: OTPRequest):
    code = f"{int(datetime.now().timestamp()) % 1000000:06d}"[-6:]
    expires_at = (datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat()
    # Upsert OTP
    db["otp"].update_one(
        {"email": payload.email},
        {"$set": {"email": payload.email, "code": code, "expiresAt": expires_at, "updated_at": datetime.now(timezone.utc)},
         "$setOnInsert": {"created_at": datetime.now(timezone.utc)}},
        upsert=True,
    )
    # Upsert user shell
    db["user"].update_one(
        {"email": payload.email},
        {"$setOnInsert": {"email": payload.email, "role": "user", "isGuest": False, "created_at": datetime.now(timezone.utc), "updated_at": datetime.now(timezone.utc)}} ,
        upsert=True,
    )
    return {"ok": True, "message": "OTP sent (mock)", "code": code}


@app.post("/auth/otp/verify")
def verify_otp(payload: OTPVerify):
    rec = db["otp"].find_one({"email": payload.email})
    if not rec or rec.get("code") != payload.code:
        raise HTTPException(status_code=400, detail="Invalid code")
    if datetime.fromisoformat(rec["expiresAt"]).replace(tzinfo=None) < datetime.utcnow():
        raise HTTPException(status_code=400, detail="Code expired")

    user = db["user"].find_one({"email": payload.email})
    if not user:
        raise HTTPException(status_code=400, detail="User missing")
    token = _session_token()
    db["session"].insert_one({"userId": str(user["_id"]), "token": token, "createdAt": datetime.now(timezone.utc)})
    return {"ok": True, "token": token, "userId": str(user["_id"]) }


@app.post("/auth/guest")
def guest_login(payload: GuestCreate):
    user = {"name": payload.name or "Guest", "isGuest": True, "role": "user", "created_at": datetime.now(timezone.utc), "updated_at": datetime.now(timezone.utc)}
    uid = db["user"].insert_one(user).inserted_id
    token = _session_token()
    db["session"].insert_one({"userId": str(uid), "token": token, "createdAt": datetime.now(timezone.utc)})
    return {"ok": True, "token": token, "userId": str(uid)}


# ---------- Tasks ----------

class SubTaskModel(BaseModel):
    emoji: str = "✅"
    title: str
    estimatedMinutes: int = Field(5, ge=1, le=480)
    completed: bool = False


class TaskCreate(BaseModel):
    userId: str
    title: str
    description: str = ""
    estimatedMinutes: int = Field(15, ge=1, le=1440)
    emoji: str = "🧠"
    voiceReminder: bool = False
    subtasks: List[SubTaskModel] = []


class TaskUpdate(BaseModel):
    title: Optional[str] = None
    description: Optional[str] = None
    estimatedMinutes: Optional[int] = Field(None, ge=1, le=1440)
    emoji: Optional[str] = None
    completed: Optional[bool] = None
    voiceReminder: Optional[bool] = None
    subtasks: Optional[List[SubTaskModel]] = None


@app.get("/tasks")
def list_tasks(userId: str):
    docs = list(db["task"].find({"userId": userId}).sort("created_at", -1))
    return [serialize(d) for d in docs]


@app.post("/tasks")
def create_task(payload: TaskCreate):
    data = payload.model_dump()
    data.update({"completed": False, "timerSession": {"status": "idle", "startedAt": None, "elapsedSeconds": 0, "currentSubtaskIndex": None}, "aiHistory": [], "aiCursor": -1, "created_at": datetime.now(timezone.utc), "updated_at": datetime.now(timezone.utc)})
    _id = db["task"].insert_one(data).inserted_id
    return serialize(db["task"].find_one({"_id": _id}))


@app.patch("/tasks/{task_id}")
def update_task(task_id: str, payload: TaskUpdate):
    updates = {k: v for k, v in payload.model_dump(exclude_unset=True).items()}
    if not updates:
        return serialize(db["task"].find_one({"_id": oid(task_id)}))
    updates["updated_at"] = datetime.now(timezone.utc)
    db["task"].update_one({"_id": oid(task_id)}, {"$set": updates})
    return serialize(db["task"].find_one({"_id": oid(task_id)}))


@app.delete("/tasks/{task_id}")
def delete_task(task_id: str):
    db["task"].delete_one({"_id": oid(task_id)})
    return {"ok": True}


@app.post("/tasks/{task_id}/complete")
def complete_task(task_id: str, completed: bool = True):
    db["task"].update_one({"_id": oid(task_id)}, {"$set": {"completed": completed, "updated_at": datetime.now(timezone.utc)}})
    return serialize(db["task"].find_one({"_id": oid(task_id)}))


@app.post("/tasks/{task_id}/subtasks/{index}/toggle")
def toggle_subtask(task_id: str, index: int):
    task = db["task"].find_one({"_id": oid(task_id)})
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    subs = task.get("subtasks", [])
    if index < 0 or index >= len(subs):
        raise HTTPException(status_code=400, detail="Invalid subtask index")
    subs[index]["completed"] = not subs[index].get("completed", False)
    db["task"].update_one({"_id": oid(task_id)}, {"$set": {"subtasks": subs, "updated_at": datetime.now(timezone.utc)}})
    return serialize(db["task"].find_one({"_id": oid(task_id)}))


# ---------- Timer Controls ----------

@app.post("/tasks/{task_id}/timer/start")
def timer_start(task_id: str, subtaskIndex: Optional[int] = None):
    now = datetime.now(timezone.utc).isoformat()
    db["task"].update_one({"_id": oid(task_id)}, {"$set": {"timerSession.status": "running", "timerSession.startedAt": now, "timerSession.currentSubtaskIndex": subtaskIndex, "updated_at": datetime.now(timezone.utc)}})
    return serialize(db["task"].find_one({"_id": oid(task_id)}))


@app.post("/tasks/{task_id}/timer/pause")
def timer_pause(task_id: str, elapsedSeconds: int = 0):
    db["task"].update_one({"_id": oid(task_id)}, {"$set": {"timerSession.status": "paused", "timerSession.elapsedSeconds": elapsedSeconds, "updated_at": datetime.now(timezone.utc)}})
    return serialize(db["task"].find_one({"_id": oid(task_id)}))


@app.post("/tasks/{task_id}/timer/stop")
def timer_stop(task_id: str):
    db["task"].update_one({"_id": oid(task_id)}, {"$set": {"timerSession": {"status": "idle", "startedAt": None, "elapsedSeconds": 0, "currentSubtaskIndex": None}, "updated_at": datetime.now(timezone.utc)}})
    return serialize(db["task"].find_one({"_id": oid(task_id)}))


# ---------- AI (Mock) ----------

class AIGeneratePayload(BaseModel):
    title: str
    description: str = ""


@app.post("/ai/subtasks")
def ai_generate_subtasks(payload: AIGeneratePayload):
    title = payload.title.strip()
    desc = payload.description.strip()
    base = [
        {"emoji": "📝", "title": f"Define the goal for '{title}'", "estimatedMinutes": 5, "completed": False},
        {"emoji": "🧩", "title": "Break into 3-5 bite-sized actions", "estimatedMinutes": 10, "completed": False},
        {"emoji": "⏱️", "title": "Estimate time for each action", "estimatedMinutes": 5, "completed": False},
        {"emoji": "🚀", "title": "Do the easiest first for momentum", "estimatedMinutes": 10, "completed": False},
        {"emoji": "✅", "title": "Review and check off what's done", "estimatedMinutes": 5, "completed": False},
    ]
    if desc:
        base.insert(1, {"emoji": "🔍", "title": "Skim any reference/context provided", "estimatedMinutes": 5, "completed": False})
    return {"subtasks": base}


class AIEditPayload(BaseModel):
    taskId: str
    prompt: str


@app.post("/ai/edit")
def ai_edit_task(payload: AIEditPayload):
    task = db["task"].find_one({"_id": oid(payload.taskId)})
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")

    # Save snapshot for undo
    snapshot = {k: task[k] for k in ["title", "description", "estimatedMinutes", "emoji", "subtasks"] if k in task}
    history = task.get("aiHistory", [])
    cursor = task.get("aiCursor", -1)
    new_history = history[: cursor + 1] + [snapshot]

    prompt = payload.prompt.lower()
    updates: Dict[str, Any] = {}
    if "simpler" in prompt or "shorter" in prompt:
        updates["description"] = (task.get("description", "")[:120] + "...") if len(task.get("description", "")) > 120 else task.get("description", "")
    if "more details" in prompt or "expand" in prompt:
        updates["description"] = (task.get("description", "") + "\n\nMore specifics: steps, tools, and a clear finish line.").strip()
    if "prioritize" in prompt:
        subs = task.get("subtasks", [])
        subs = sorted(subs, key=lambda s: s.get("estimatedMinutes", 5))
        updates["subtasks"] = subs
    if "emoji" in prompt:
        updates["emoji"] = "✨"

    if not updates:
        updates["description"] = (task.get("description", "") + "\n\nTuned for clarity.").strip()

    updates["aiHistory"] = new_history
    updates["aiCursor"] = len(new_history) - 1
    updates["updated_at"] = datetime.now(timezone.utc)
    db["task"].update_one({"_id": oid(payload.taskId)}, {"$set": updates})
    return serialize(db["task"].find_one({"_id": oid(payload.taskId)}))


class AIHistoryMove(BaseModel):
    taskId: str


@app.post("/ai/undo")
def ai_undo(payload: AIHistoryMove):
    task = db["task"].find_one({"_id": oid(payload.taskId)})
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    cursor = task.get("aiCursor", -1)
    history = task.get("aiHistory", [])
    if cursor < 0:
        return serialize(task)
    snap = history[cursor]
    db["task"].update_one({"_id": oid(payload.taskId)}, {"$set": {**snap, "aiCursor": cursor - 1, "updated_at": datetime.now(timezone.utc)}})
    return serialize(db["task"].find_one({"_id": oid(payload.taskId)}))


@app.post("/ai/redo")
def ai_redo(payload: AIHistoryMove):
    task = db["task"].find_one({"_id": oid(payload.taskId)})
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    cursor = task.get("aiCursor", -1)
    history = task.get("aiHistory", [])
    if cursor + 1 >= len(history):
        return serialize(task)
    snap = history[cursor + 1]
    db["task"].update_one({"_id": oid(payload.taskId)}, {"$set": {**snap, "aiCursor": cursor + 1, "updated_at": datetime.now(timezone.utc)}})
    return serialize(db["task"].find_one({"_id": oid(payload.taskId)}))


if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
