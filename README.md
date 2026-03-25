# Proctored Exam Platform (Faculty + Student Dashboards)

This repository provides a production-ready **baseline architecture** for a proctored exam platform that supports:

- Google OAuth role routing (faculty vs students).
- Faculty-only exam creation for **Internal / External / Viva**.
- CSV/Excel question-bank upload.
- Randomized and non-overlapping question assignment (unique set per student).
- 120 concurrent students per exam.
- Proctoring events for tab switching/copy-paste violations.
- Resume-by-passcode with max 3 attempts.
- Single-file export of all student answers with:
  - Name
  - Roll Number
  - Set Number
  - Questions
  - Submitted Answers

## Tech Stack (Open Source + Feasible)

- **Backend:** FastAPI + SQLModel + SQLite (swap to PostgreSQL in production)
- **Data processing:** pandas + openpyxl
- **Auth integration point:** Google OAuth callback endpoint scaffolded
- **Deployment:** Docker/Kubernetes compatible (containerization can be added next)

## Access Rules

- Faculty dashboard access only for allowlisted personal email:
  - `vasuch9959@gmail.com`
- Student dashboard access only for organization mail pattern:
  - `N2XXXXXX@rguktn.ac.in` (validated via regex)

## API Overview

1. `POST /auth/google/callback`
   - Resolves user role by email.

2. `POST /faculty/exams?faculty_id=<id>`
   - Creates exam metadata, duration window, and secure resume passcode.

3. `POST /faculty/exams/{exam_id}/questions?faculty_id=<id>&student_ids=1,2,3`
   - Upload CSV/XLSX question bank with at least `question_text` column.
   - Auto-distributes unique question sets (`questions_per_student` each).

4. `POST /student/exams/{exam_id}/proctor-event?student_id=<id>`
   - Receives events: `tab_switch`, `copy_paste`, `screen_blur`.
   - Escalates violation count; can terminate exam on repeated abuse.

5. `POST /student/exams/{exam_id}/resume?student_id=<id>`
   - Requires faculty passcode.
   - Maximum 3 resume grants.

6. `POST /student/exams/{exam_id}/submit?student_id=<id>`
   - Persists answers, enforces assigned-question-only submission.

7. `GET /faculty/exams/{exam_id}/export?faculty_id=<id>`
   - Downloads single CSV of all submissions.

## Unique Set Distribution Logic

If faculty uploads 600 questions for a section of 60 students and each needs 3 questions,
then required questions are `60 x 3 = 180`, so the allocation succeeds with no overlap.

The service currently enforces **no repeated question across students** per exam allocation.

## Run Locally

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --reload
```

Open docs: `http://127.0.0.1:8000/docs`

## Recommended Next Implementation Steps

- Add full JWT session handling after OAuth verification.
- Add frontend dashboards (React/Next.js).
- Add websocket proctor stream + snapshots.
- Add Redis queue for scalable auto-submission timers.
- Move SQLite to PostgreSQL for multi-instance deployments.
