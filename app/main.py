from datetime import datetime, timedelta
import io
import secrets

import pandas as pd
from fastapi import Depends, FastAPI, File, HTTPException, UploadFile
from fastapi.responses import StreamingResponse
from sqlmodel import Session, select

from app.database import get_session, init_db
from app.models import Answer, Exam, ExamEnrollment, Question, StudentQuestion, User
from app.schemas import (
    AuthResponse,
    ExamCreatePayload,
    ExamResponse,
    GoogleAuthPayload,
    ProctorEventPayload,
    ResumePayload,
    SubmitPayload,
)
from app.security import resolve_role
from app.services.exam_generator import assign_unique_question_sets

app = FastAPI(title="Proctored Exam Platform")

MAX_RESUMES = 3
TERMINATION_EVENTS = {"tab_switch", "copy_paste", "screen_blur"}


@app.on_event("startup")
def startup() -> None:
    init_db()


@app.post("/auth/google/callback", response_model=AuthResponse)
def google_oauth_callback(payload: GoogleAuthPayload, session: Session = Depends(get_session)):
    try:
        role = resolve_role(payload.email)
    except ValueError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc

    user = session.exec(select(User).where(User.email == payload.email)).first()
    if not user:
        user = User(
            email=payload.email,
            full_name=payload.full_name,
            role=role,
            roll_number=payload.roll_number,
        )
        session.add(user)
        session.commit()
        session.refresh(user)

    return AuthResponse(user_id=user.id, email=user.email, role=user.role)


@app.post("/faculty/exams", response_model=ExamResponse)
def create_exam(payload: ExamCreatePayload, faculty_id: int, session: Session = Depends(get_session)):
    faculty = session.get(User, faculty_id)
    if not faculty or faculty.role != "faculty":
        raise HTTPException(status_code=403, detail="Faculty access required")

    if len(payload.student_ids) > 120:
        raise HTTPException(status_code=400, detail="Maximum 120 students per live exam")

    exam = Exam(
        title=payload.title,
        exam_type=payload.exam_type.lower(),
        faculty_id=faculty_id,
        duration_minutes=payload.duration_minutes,
        questions_per_student=payload.questions_per_student,
        scheduled_start=payload.scheduled_start,
        scheduled_end=payload.scheduled_start + timedelta(minutes=payload.duration_minutes),
        resume_passcode=secrets.token_hex(3).upper(),
    )

    session.add(exam)
    session.commit()
    session.refresh(exam)

    return ExamResponse(
        id=exam.id,
        title=exam.title,
        exam_type=exam.exam_type,
        scheduled_start=exam.scheduled_start,
        scheduled_end=exam.scheduled_end,
    )


@app.post("/faculty/exams/{exam_id}/questions")
async def upload_questions(
    exam_id: int,
    faculty_id: int,
    student_ids: str,
    file: UploadFile = File(...),
    session: Session = Depends(get_session),
):
    exam = session.get(Exam, exam_id)
    if not exam or exam.faculty_id != faculty_id:
        raise HTTPException(status_code=403, detail="Exam not found for faculty")

    raw = await file.read()
    if file.filename.endswith(".csv"):
        df = pd.read_csv(io.BytesIO(raw))
    else:
        df = pd.read_excel(io.BytesIO(raw))

    if "question_text" not in df.columns:
        raise HTTPException(status_code=400, detail="File must include question_text column")

    for _, row in df.iterrows():
        session.add(
            Question(
                exam_id=exam_id,
                question_text=str(row["question_text"]),
                section=str(row.get("section", "")) or None,
            )
        )

    session.commit()

    parsed_student_ids = [int(item.strip()) for item in student_ids.split(",") if item.strip()]
    assign_unique_question_sets(session, exam, parsed_student_ids)
    session.commit()

    return {
        "message": "Questions uploaded and unique sets distributed",
        "students": len(parsed_student_ids),
        "passcode": exam.resume_passcode,
    }


@app.post("/student/exams/{exam_id}/proctor-event")
def track_proctor_event(
    exam_id: int,
    student_id: int,
    payload: ProctorEventPayload,
    session: Session = Depends(get_session),
):
    enrollment = session.exec(
        select(ExamEnrollment).where(
            ExamEnrollment.exam_id == exam_id, ExamEnrollment.student_id == student_id
        )
    ).first()
    if not enrollment:
        raise HTTPException(status_code=404, detail="Enrollment not found")

    if payload.event_type in TERMINATION_EVENTS:
        enrollment.violation_count += 1

    if enrollment.violation_count > MAX_RESUMES:
        enrollment.status = "terminated"

    session.add(enrollment)
    session.commit()

    return {
        "status": enrollment.status,
        "violations": enrollment.violation_count,
        "remaining_resume_attempts": max(0, MAX_RESUMES - enrollment.resume_count),
    }


@app.post("/student/exams/{exam_id}/resume")
def resume_exam(
    exam_id: int,
    student_id: int,
    payload: ResumePayload,
    session: Session = Depends(get_session),
):
    exam = session.get(Exam, exam_id)
    enrollment = session.exec(
        select(ExamEnrollment).where(
            ExamEnrollment.exam_id == exam_id, ExamEnrollment.student_id == student_id
        )
    ).first()

    if not exam or not enrollment:
        raise HTTPException(status_code=404, detail="Invalid exam/student mapping")

    if enrollment.resume_count >= MAX_RESUMES:
        enrollment.status = "terminated"
        session.add(enrollment)
        session.commit()
        raise HTTPException(status_code=403, detail="Resume limit exceeded")

    if payload.passcode != exam.resume_passcode:
        raise HTTPException(status_code=400, detail="Invalid passcode")

    enrollment.resume_count += 1
    enrollment.status = "in_progress"
    session.add(enrollment)
    session.commit()
    return {"status": enrollment.status, "resume_count": enrollment.resume_count}


@app.post("/student/exams/{exam_id}/submit")
def submit_exam(
    exam_id: int,
    student_id: int,
    payload: SubmitPayload,
    session: Session = Depends(get_session),
):
    exam = session.get(Exam, exam_id)
    enrollment = session.exec(
        select(ExamEnrollment).where(
            ExamEnrollment.exam_id == exam_id, ExamEnrollment.student_id == student_id
        )
    ).first()

    if not exam or not enrollment:
        raise HTTPException(status_code=404, detail="Enrollment not found")

    if datetime.utcnow() > exam.scheduled_end:
        enrollment.status = "submitted"

    if enrollment.status == "terminated":
        raise HTTPException(status_code=403, detail="Exam terminated due to policy violations")

    assigned_qids = {
        s.question_id
        for s in session.exec(
            select(StudentQuestion).where(
                StudentQuestion.exam_id == exam_id,
                StudentQuestion.student_id == student_id,
            )
        ).all()
    }

    for entry in payload.answers:
        question_id = int(entry["question_id"])
        if question_id not in assigned_qids:
            raise HTTPException(status_code=400, detail=f"Question {question_id} not assigned")
        session.add(
            Answer(
                exam_id=exam_id,
                student_id=student_id,
                question_id=question_id,
                answer_text=str(entry.get("answer_text", "")),
            )
        )

    enrollment.status = "submitted"
    session.add(enrollment)
    session.commit()
    return {"message": "Submitted"}


@app.get("/faculty/exams/{exam_id}/export")
def export_submissions(exam_id: int, faculty_id: int, session: Session = Depends(get_session)):
    exam = session.get(Exam, exam_id)
    if not exam or exam.faculty_id != faculty_id:
        raise HTTPException(status_code=403, detail="Forbidden")

    answers = session.exec(select(Answer).where(Answer.exam_id == exam_id)).all()
    questions = {q.id: q for q in session.exec(select(Question).where(Question.exam_id == exam_id)).all()}

    enrollments = session.exec(select(ExamEnrollment).where(ExamEnrollment.exam_id == exam_id)).all()
    enrollment_map = {e.student_id: e for e in enrollments}

    user_ids = list({a.student_id for a in answers})
    users = session.exec(select(User).where(User.id.in_(user_ids))).all() if user_ids else []
    user_map = {u.id: u for u in users}

    rows = []
    for ans in answers:
        user = user_map.get(ans.student_id)
        enrollment = enrollment_map.get(ans.student_id)
        question = questions.get(ans.question_id)
        rows.append(
            {
                "Name": user.full_name if user else "Unknown",
                "Roll Number": user.roll_number if user else "",
                "Set Number": enrollment.set_number if enrollment else "",
                "Question": question.question_text if question else "",
                "Answer": ans.answer_text,
            }
        )

    output_df = pd.DataFrame(rows)
    stream = io.StringIO()
    output_df.to_csv(stream, index=False)
    stream.seek(0)

    return StreamingResponse(
        iter([stream.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename=exam_{exam_id}_submissions.csv"},
    )
