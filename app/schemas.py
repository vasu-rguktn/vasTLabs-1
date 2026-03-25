from datetime import datetime
from typing import List, Optional

from pydantic import BaseModel, EmailStr, Field


class GoogleAuthPayload(BaseModel):
    email: EmailStr
    full_name: str
    roll_number: Optional[str] = None


class AuthResponse(BaseModel):
    user_id: int
    email: EmailStr
    role: str


class ExamCreatePayload(BaseModel):
    title: str
    exam_type: str
    duration_minutes: int = Field(gt=0)
    questions_per_student: int = Field(gt=0)
    student_ids: List[int]
    scheduled_start: datetime


class ExamResponse(BaseModel):
    id: int
    title: str
    exam_type: str
    scheduled_start: datetime
    scheduled_end: datetime


class ProctorEventPayload(BaseModel):
    event_type: str


class ResumePayload(BaseModel):
    passcode: str


class SubmitPayload(BaseModel):
    answers: List[dict]
