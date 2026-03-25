import re

FACULTY_EMAIL_ALLOWLIST = {"vasuch9959@gmail.com"}
STUDENT_MAIL_PATTERN = re.compile(r"^N2\w+@rguktn\.ac\.in$", re.IGNORECASE)


def resolve_role(email: str) -> str:
    if email.lower() in FACULTY_EMAIL_ALLOWLIST:
        return "faculty"
    if STUDENT_MAIL_PATTERN.match(email):
        return "student"
    raise ValueError("Email not authorized for this platform")
