from datetime import datetime, timedelta
import io
import secrets
from html import escape
from pathlib import Path

import pandas as pd
from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, FileResponse, RedirectResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
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
app.mount("/static", StaticFiles(directory="app/static"), name="static")

MAX_RESUMES = 3
TERMINATION_EVENTS = {"tab_switch", "copy_paste", "screen_blur"}
BASE_DIR = Path(__file__).resolve().parent


class RealtimeHub:
    def __init__(self) -> None:
        self.connections: dict[str, list[WebSocket]] = {}

    async def connect(self, channel: str, websocket: WebSocket) -> None:
        await websocket.accept()
        self.connections.setdefault(channel, []).append(websocket)

    def disconnect(self, channel: str, websocket: WebSocket) -> None:
        current = self.connections.get(channel, [])
        if websocket in current:
            current.remove(websocket)
        if not current and channel in self.connections:
            del self.connections[channel]

    async def broadcast(self, channel: str, payload: dict) -> None:
        for ws in self.connections.get(channel, []):
            await ws.send_json(payload)


realtime_hub = RealtimeHub()


def _exam_channel(exam_id: int) -> str:
    return f"exam:{exam_id}"


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
async def track_proctor_event(
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

    response = {
        "status": enrollment.status,
        "violations": enrollment.violation_count,
        "remaining_resume_attempts": max(0, MAX_RESUMES - enrollment.resume_count),
    }
    await realtime_hub.broadcast(
        _exam_channel(exam_id),
        {"type": "proctor_event", "student_id": student_id, "payload": response},
    )
    return response


@app.post("/student/exams/{exam_id}/resume")
async def resume_exam(
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
    response = {"status": enrollment.status, "resume_count": enrollment.resume_count}
    await realtime_hub.broadcast(
        _exam_channel(exam_id),
        {"type": "resume", "student_id": student_id, "payload": response},
    )
    return response


@app.post("/student/exams/{exam_id}/submit")
async def submit_exam(
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
    response = {"message": "Submitted"}
    await realtime_hub.broadcast(
        _exam_channel(exam_id),
        {"type": "submission", "student_id": student_id, "payload": response},
    )
    return response


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


def _faculty_dashboard(session: Session, user_id: int) -> dict:
    exams = session.exec(select(Exam).where(Exam.faculty_id == user_id).order_by(Exam.id.desc())).all()
    return {"exams": exams}


def _student_dashboard(session: Session, user_id: int) -> dict:
    enrollments = session.exec(
        select(ExamEnrollment).where(ExamEnrollment.student_id == user_id).order_by(ExamEnrollment.exam_id.desc())
    ).all()
    exam_ids = [en.exam_id for en in enrollments]
    exams = session.exec(select(Exam).where(Exam.id.in_(exam_ids))).all() if exam_ids else []
    exam_map = {exam.id: exam for exam in exams}

    assigned = session.exec(select(StudentQuestion).where(StudentQuestion.student_id == user_id)).all()
    assigned_by_exam: dict[int, list[StudentQuestion]] = {}
    for sq in assigned:
        assigned_by_exam.setdefault(sq.exam_id, []).append(sq)

    q_ids = [sq.question_id for sq in assigned]
    questions = session.exec(select(Question).where(Question.id.in_(q_ids))).all() if q_ids else []
    question_map = {q.id: q for q in questions}

    return {
        "enrollments": enrollments,
        "exam_map": exam_map,
        "assigned_by_exam": assigned_by_exam,
        "question_map": question_map,
    }


@app.get("/web/state")
def web_state(user_id: int, session: Session = Depends(get_session)):
    user = session.get(User, user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    if user.role == "faculty":
        exams = _faculty_dashboard(session, user_id)["exams"]
        return {
            "user": {"id": user.id, "role": user.role, "email": user.email, "full_name": user.full_name},
            "faculty_exams": [
                {
                    "id": e.id,
                    "title": e.title,
                    "exam_type": e.exam_type,
                    "scheduled_start": e.scheduled_start.isoformat(),
                    "scheduled_end": e.scheduled_end.isoformat(),
                }
                for e in exams
            ],
        }

    data = _student_dashboard(session, user_id)
    enrollments = data["enrollments"]
    exam_map = data["exam_map"]
    assigned_by_exam = data["assigned_by_exam"]
    question_map = data["question_map"]
    return {
        "user": {"id": user.id, "role": user.role, "email": user.email, "full_name": user.full_name},
        "student_exams": [
            {
                "exam_id": en.exam_id,
                "title": exam_map[en.exam_id].title if en.exam_id in exam_map else "Unknown exam",
                "status": en.status,
                "violation_count": en.violation_count,
                "resume_count": en.resume_count,
                "questions": [
                    {
                        "question_id": sq.question_id,
                        "question_text": question_map[sq.question_id].question_text
                        if sq.question_id in question_map
                        else "Unknown question",
                    }
                    for sq in assigned_by_exam.get(en.exam_id, [])
                ],
            }
            for en in enrollments
        ],
    }


@app.websocket("/ws/exams/{exam_id}")
async def exam_realtime_stream(websocket: WebSocket, exam_id: int):
    channel = _exam_channel(exam_id)
    await realtime_hub.connect(channel, websocket)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        realtime_hub.disconnect(channel, websocket)


@app.get("/", response_class=HTMLResponse)
def home():
    return FileResponse(BASE_DIR / "static" / "index.html")


@app.post("/web/login")
def web_login(
    email: str = Form(...),
    full_name: str = Form(...),
    roll_number: str = Form(""),
    session: Session = Depends(get_session),
):
    payload = GoogleAuthPayload(email=email, full_name=full_name, roll_number=roll_number or None)
    auth = google_oauth_callback(payload=payload, session=session)
    return RedirectResponse(url=f"/web/dashboard?user_id={auth.user_id}", status_code=303)


@app.get("/web/dashboard", response_class=HTMLResponse)
def web_dashboard(request: Request, user_id: int, session: Session = Depends(get_session)):
    user = session.get(User, user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    if user.role == "faculty":
        exams = _faculty_dashboard(session, user_id)["exams"]
        exam_cards = []
        for exam in exams:
            exam_cards.append(
                f"""
<article class='subcard'>
<h3>#{exam.id} - {escape(exam.title)} ({escape(exam.exam_type)})</h3>
<p>Start: {exam.scheduled_start} | End: {exam.scheduled_end}</p>
<p><a href='/faculty/exams/{exam.id}/export?faculty_id={user_id}'>Export Submissions CSV</a></p>
<form method='post' action='/web/faculty/exams/{exam.id}/questions' enctype='multipart/form-data' class='form-grid'>
<input type='hidden' name='user_id' value='{user_id}'/>
<label>Student IDs for distribution <input type='text' name='student_ids' placeholder='2,3,4' required/></label>
<label>Question bank CSV/XLSX <input type='file' name='file' required/></label>
<button type='submit'>Upload + Assign Questions</button>
</form></article>
"""
            )
        cards_html = "".join(exam_cards) if exam_cards else "<p>No exams yet.</p>"
        return Response(
            content=f"""
<!doctype html><html><head><meta charset='utf-8'/><title>Faculty Dashboard</title>
<link rel='stylesheet' href='/static/styles.css'/></head><body><main class='container'>
<h1>Faculty Dashboard</h1><p>Welcome {escape(user.full_name)} ({escape(user.email)})</p><a href='/'>Switch User</a>
<section class='card'><h2>Create Exam</h2>
<form method='post' action='/web/faculty/exams' class='form-grid'>
<input type='hidden' name='user_id' value='{user_id}'/>
<label>Title <input type='text' name='title' required/></label>
<label>Type <select name='exam_type'><option value='internal'>Internal</option><option value='external'>External</option><option value='viva'>Viva</option></select></label>
<label>Duration (minutes) <input type='number' min='1' name='duration_minutes' required/></label>
<label>Questions per student <input type='number' min='1' name='questions_per_student' required/></label>
<label>Student IDs (comma separated) <input type='text' name='student_ids' placeholder='2,3,4' required/></label>
<label>Start datetime <input type='datetime-local' name='scheduled_start' required/></label>
<button type='submit'>Create Exam</button></form></section>
<section class='card'><h2>My Exams</h2>{cards_html}</section></main></body></html>
""",
            media_type="text/html",
        )

    student_data = _student_dashboard(session, user_id)
    enrollments = student_data["enrollments"]
    exam_map = student_data["exam_map"]
    assigned_by_exam = student_data["assigned_by_exam"]
    question_map = student_data["question_map"]
    enrollment_cards = []
    for enrollment in enrollments:
        exam = exam_map.get(enrollment.exam_id)
        title = exam.title if exam else "Unknown exam"
        questions_html = []
        for index, sq in enumerate(assigned_by_exam.get(enrollment.exam_id, []), start=1):
            question = question_map.get(sq.question_id)
            q_text = question.question_text if question else "Unknown question"
            questions_html.append(
                f"<label>Q{index}. {escape(q_text)}<textarea name='answer_{sq.question_id}' rows='2' required></textarea></label>"
            )
        enrollment_cards.append(
            f"""
<article class='subcard'><h3>#{enrollment.exam_id} - {escape(title)}</h3>
<p>Status: <strong>{enrollment.status}</strong> | Violations: {enrollment.violation_count} | Resume count: {enrollment.resume_count}</p>
<form method='post' action='/web/student/exams/{enrollment.exam_id}/proctor' class='form-inline'>
<input type='hidden' name='user_id' value='{user_id}'/><select name='event_type'><option value='tab_switch'>tab_switch</option><option value='copy_paste'>copy_paste</option><option value='screen_blur'>screen_blur</option></select><button type='submit'>Send Proctor Event</button></form>
<form method='post' action='/web/student/exams/{enrollment.exam_id}/resume' class='form-inline'>
<input type='hidden' name='user_id' value='{user_id}'/><input type='text' name='passcode' placeholder='Faculty passcode' required/><button type='submit'>Resume Exam</button></form>
<form method='post' action='/web/student/exams/{enrollment.exam_id}/submit' class='form-grid'>
<input type='hidden' name='user_id' value='{user_id}'/>{''.join(questions_html)}<button type='submit'>Submit Exam</button></form></article>
"""
        )
    cards_html = "".join(enrollment_cards) if enrollment_cards else "<p>No assigned exams yet.</p>"
    return Response(
        content=f"""
<!doctype html><html><head><meta charset='utf-8'/><title>Student Dashboard</title>
<link rel='stylesheet' href='/static/styles.css'/></head><body><main class='container'>
<h1>Student Dashboard</h1><p>Welcome {escape(user.full_name)} ({escape(user.email)})</p><a href='/'>Switch User</a>
<section class='card'><h2>Assigned Exams</h2>{cards_html}</section></main></body></html>
""",
        media_type="text/html",
    )


@app.post("/web/faculty/exams")
def web_create_exam(
    user_id: int = Form(...),
    title: str = Form(...),
    exam_type: str = Form(...),
    duration_minutes: int = Form(...),
    questions_per_student: int = Form(...),
    student_ids: str = Form(...),
    scheduled_start: str = Form(...),
    session: Session = Depends(get_session),
):
    payload = ExamCreatePayload(
        title=title,
        exam_type=exam_type,
        duration_minutes=duration_minutes,
        questions_per_student=questions_per_student,
        student_ids=[int(i.strip()) for i in student_ids.split(",") if i.strip()],
        scheduled_start=datetime.fromisoformat(scheduled_start),
    )
    create_exam(payload=payload, faculty_id=user_id, session=session)
    return RedirectResponse(url=f"/web/dashboard?user_id={user_id}", status_code=303)


@app.post("/web/faculty/exams/{exam_id}/questions")
async def web_upload_questions(
    exam_id: int,
    user_id: int = Form(...),
    student_ids: str = Form(...),
    file: UploadFile = File(...),
    session: Session = Depends(get_session),
):
    await upload_questions(
        exam_id=exam_id,
        faculty_id=user_id,
        student_ids=student_ids,
        file=file,
        session=session,
    )
    return RedirectResponse(url=f"/web/dashboard?user_id={user_id}", status_code=303)


@app.post("/web/student/exams/{exam_id}/proctor")
async def web_proctor_event(
    exam_id: int,
    user_id: int = Form(...),
    event_type: str = Form(...),
    session: Session = Depends(get_session),
):
    await track_proctor_event(
        exam_id=exam_id,
        student_id=user_id,
        payload=ProctorEventPayload(event_type=event_type),
        session=session,
    )
    return RedirectResponse(url=f"/web/dashboard?user_id={user_id}", status_code=303)


@app.post("/web/student/exams/{exam_id}/resume")
async def web_resume_exam(
    exam_id: int,
    user_id: int = Form(...),
    passcode: str = Form(...),
    session: Session = Depends(get_session),
):
    await resume_exam(
        exam_id=exam_id,
        student_id=user_id,
        payload=ResumePayload(passcode=passcode),
        session=session,
    )
    return RedirectResponse(url=f"/web/dashboard?user_id={user_id}", status_code=303)


@app.post("/web/student/exams/{exam_id}/submit")
async def web_submit_exam(
    exam_id: int,
    request: Request,
    user_id: int = Form(...),
    session: Session = Depends(get_session),
):
    form = await request.form()

    answers = []
    for key, value in form.multi_items():
        if key.startswith("answer_"):
            question_id = key.replace("answer_", "")
            answers.append({"question_id": int(question_id), "answer_text": value})

    await submit_exam(
        exam_id=exam_id,
        student_id=user_id,
        payload=SubmitPayload(answers=answers),
        session=session,
    )
    return RedirectResponse(url=f"/web/dashboard?user_id={user_id}", status_code=303)
