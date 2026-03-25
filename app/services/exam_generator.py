import random
from collections import defaultdict
from typing import Iterable

from sqlmodel import Session, select

from app.models import Exam, ExamEnrollment, Question, StudentQuestion


def assign_unique_question_sets(session: Session, exam: Exam, student_ids: Iterable[int]) -> None:
    student_list = list(student_ids)
    questions = session.exec(select(Question).where(Question.exam_id == exam.id)).all()
    question_ids = [q.id for q in questions if q.id is not None]

    required = len(student_list) * exam.questions_per_student
    if len(question_ids) < required:
        raise ValueError(
            f"Not enough questions. Need at least {required} for unique allocation without overlaps."
        )

    random.shuffle(question_ids)
    cursor = 0
    set_number = 1

    for student_id in student_list:
        enrollment = ExamEnrollment(exam_id=exam.id, student_id=student_id, set_number=set_number)
        session.add(enrollment)

        slice_end = cursor + exam.questions_per_student
        allocated = question_ids[cursor:slice_end]
        cursor = slice_end

        for qid in allocated:
            session.add(StudentQuestion(exam_id=exam.id, student_id=student_id, question_id=qid))

        set_number += 1


def questions_grouped_by_student(session: Session, exam_id: int) -> dict[int, list[Question]]:
    links = session.exec(select(StudentQuestion).where(StudentQuestion.exam_id == exam_id)).all()
    question_map = {
        q.id: q for q in session.exec(select(Question).where(Question.exam_id == exam_id)).all() if q.id is not None
    }
    grouped = defaultdict(list)

    for link in links:
        grouped[link.student_id].append(question_map[link.question_id])

    return grouped
