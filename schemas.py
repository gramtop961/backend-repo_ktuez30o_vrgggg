"""
Database Schemas for StepWize

Each Pydantic model represents a MongoDB collection (lowercased class name).

Collections:
- user
- task
- session
- otp
"""

from pydantic import BaseModel, Field
from typing import List, Optional, Literal, Dict, Any


class SubTask(BaseModel):
    emoji: str = Field("✅", description="Emoji representing the subtask")
    title: str = Field(..., description="Short title for the subtask")
    estimatedMinutes: int = Field(5, ge=1, le=480)
    completed: bool = Field(False)


class TimerSession(BaseModel):
    status: Literal["idle", "running", "paused"] = "idle"
    startedAt: Optional[str] = None  # ISO timestamp
    elapsedSeconds: int = 0
    currentSubtaskIndex: Optional[int] = None


class Task(BaseModel):
    userId: str
    title: str
    description: str = ""
    estimatedMinutes: int = Field(15, ge=1, le=1440)
    emoji: str = "🧠"
    completed: bool = False
    voiceReminder: bool = False
    subtasks: List[SubTask] = []
    timerSession: TimerSession = TimerSession()
    # Simple AI history for undo/redo
    aiHistory: List[Dict[str, Any]] = []  # list of snapshots
    aiCursor: int = -1


class User(BaseModel):
    email: Optional[str] = None
    name: Optional[str] = None
    image: Optional[str] = None
    role: Literal["user", "admin"] = "user"
    isGuest: bool = False


class Session(BaseModel):
    userId: str
    token: str
    createdAt: Optional[str] = None


class OTP(BaseModel):
    email: str
    code: str
    expiresAt: str
