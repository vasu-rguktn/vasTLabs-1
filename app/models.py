from datetime import datetime
from typing import Optional

from sqlmodel import Field, SQLModel


class User(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    email: str = Field(index=True, unique=True)
    full_name: str
    role: str = Field(index=True)  # faculty | student
    roll_number: Optional[str] = Field(default=None, index=True)


class Exam(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    exam_type: str  # internal | external | viva
    title: str
    faculty_id: int = Field(foreign_key="user.id")
    duration_minutes: int
    questions_per_student: int
    max_students: int = 120
    scheduled_start: datetime
    scheduled_end: datetime
    resume_passcode: str


class Question(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    exam_id: int = Field(foreign_key="exam.id", index=True)
    question_text: str
    section: Optional[str] = None


class ExamEnrollment(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    exam_id: int = Field(foreign_key="exam.id", index=True)
    student_id: int = Field(foreign_key="user.id", index=True)
    set_number: int
    status: str = "not_started"  # not_started | in_progress | submitted | terminated
    violation_count: int = 0
    resume_count: int = 0


class StudentQuestion(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    exam_id: int = Field(foreign_key="exam.id", index=True)
    student_id: int = Field(foreign_key="user.id", index=True)
    question_id: int = Field(foreign_key="question.id")


class Answer(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    exam_id: int = Field(foreign_key="exam.id", index=True)
    student_id: int = Field(foreign_key="user.id", index=True)
    question_id: int = Field(foreign_key="question.id")
    answer_text: str
    submitted_at: datetime = Field(default_factory=datetime.utcnow)
