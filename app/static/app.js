let currentUserId = null;
let wsByExam = {};

const loginForm = document.getElementById("login-form");
const loginSection = document.getElementById("login-section");
const appSection = document.getElementById("app-section");
const welcome = document.getElementById("welcome");
const roleLine = document.getElementById("role-line");
const dashboard = document.getElementById("dashboard");
const eventsEl = document.getElementById("events");

function addEvent(message) {
  const line = document.createElement("div");
  line.className = "event-line";
  line.textContent = `${new Date().toLocaleTimeString()} - ${message}`;
  eventsEl.prepend(line);
}

function connectExamSocket(examId) {
  if (wsByExam[examId]) return;
  const protocol = window.location.protocol === "https:" ? "wss" : "ws";
  const ws = new WebSocket(`${protocol}://${window.location.host}/ws/exams/${examId}`);
  ws.onopen = () => {
    addEvent(`Connected realtime channel for exam ${examId}`);
    ws.send("subscribe");
  };
  ws.onmessage = (evt) => {
    const data = JSON.parse(evt.data);
    addEvent(`[Exam ${examId}] ${data.type} -> student ${data.student_id}`);
  };
  ws.onclose = () => {
    addEvent(`Disconnected realtime channel for exam ${examId}`);
    delete wsByExam[examId];
  };
  wsByExam[examId] = ws;
}

async function api(path, options = {}) {
  const res = await fetch(path, options);
  if (!res.ok) {
    const err = await res.text();
    throw new Error(err || `HTTP ${res.status}`);
  }
  const contentType = res.headers.get("content-type") || "";
  return contentType.includes("application/json") ? res.json() : res.text();
}

function renderFaculty(data) {
  const exams = data.faculty_exams || [];
  const blocks = exams.map((exam) => `
    <article class="subcard">
      <h3>#${exam.id} - ${exam.title} (${exam.exam_type})</h3>
      <p>${exam.scheduled_start} → ${exam.scheduled_end}</p>
      <p><a href="/faculty/exams/${exam.id}/export?faculty_id=${currentUserId}" target="_blank">Export Submissions CSV</a></p>
      <form class="form-grid" onsubmit="uploadQuestions(event, ${exam.id})">
        <label>Student IDs <input name="student_ids" placeholder="2,3,4" required /></label>
        <label>CSV/XLSX <input type="file" name="file" required /></label>
        <button type="submit">Upload + Assign</button>
      </form>
    </article>
  `).join("");

  dashboard.innerHTML = `
    <section class="card">
      <h3>Create Exam</h3>
      <form class="form-grid" onsubmit="createExam(event)">
        <label>Title <input name="title" required /></label>
        <label>Type
          <select name="exam_type"><option value="internal">internal</option><option value="external">external</option><option value="viva">viva</option></select>
        </label>
        <label>Duration <input name="duration_minutes" type="number" min="1" required /></label>
        <label>Questions per student <input name="questions_per_student" type="number" min="1" required /></label>
        <label>Student IDs <input name="student_ids" placeholder="2,3,4" required /></label>
        <label>Start datetime <input name="scheduled_start" type="datetime-local" required /></label>
        <button type="submit">Create Exam</button>
      </form>
    </section>
    <section class="card"><h3>My Exams</h3>${blocks || "<p>No exams yet.</p>"}</section>
  `;

  exams.forEach((e) => connectExamSocket(e.id));
}

function renderStudent(data) {
  const exams = data.student_exams || [];
  const blocks = exams.map((exam) => {
    const questions = (exam.questions || []).map((q, i) => `
      <label>Q${i + 1}. ${q.question_text}<textarea name="answer_${q.question_id}" rows="2" required></textarea></label>
    `).join("");

    connectExamSocket(exam.exam_id);

    return `
    <article class="subcard">
      <h3>#${exam.exam_id} - ${exam.title}</h3>
      <p>Status: <b>${exam.status}</b> | Violations: ${exam.violation_count} | Resume count: ${exam.resume_count}</p>
      <form class="form-inline" onsubmit="sendProctorEvent(event, ${exam.exam_id})">
        <select name="event_type"><option value="tab_switch">tab_switch</option><option value="copy_paste">copy_paste</option><option value="screen_blur">screen_blur</option></select>
        <button type="submit">Send Proctor Event</button>
      </form>
      <form class="form-inline" onsubmit="resumeExam(event, ${exam.exam_id})">
        <input name="passcode" placeholder="Faculty passcode" required />
        <button type="submit">Resume Exam</button>
      </form>
      <form class="form-grid" onsubmit="submitExam(event, ${exam.exam_id})">
        ${questions}
        <button type="submit">Submit Exam</button>
      </form>
    </article>
  `;
  }).join("");

  dashboard.innerHTML = `<section class="card"><h3>Assigned Exams</h3>${blocks || "<p>No assigned exams.</p>"}</section>`;
}

async function loadState() {
  const data = await api(`/web/state?user_id=${currentUserId}`);
  welcome.textContent = `Welcome ${data.user.full_name} (${data.user.email})`;
  roleLine.textContent = `Role: ${data.user.role}`;

  if (data.user.role === "faculty") renderFaculty(data);
  else renderStudent(data);
}

loginForm.addEventListener("submit", async (e) => {
  e.preventDefault();
  const formData = new FormData(loginForm);
  const body = JSON.stringify({
    email: formData.get("email"),
    full_name: formData.get("full_name"),
    roll_number: formData.get("roll_number") || null,
  });
  const auth = await api("/auth/google/callback", { method: "POST", headers: { "Content-Type": "application/json" }, body });
  currentUserId = auth.user_id;
  loginSection.style.display = "none";
  appSection.style.display = "block";
  addEvent(`Logged in as ${auth.role}`);
  await loadState();
});

async function createExam(e) {
  e.preventDefault();
  const formData = new FormData(e.target);
  const payload = {
    title: formData.get("title"),
    exam_type: formData.get("exam_type"),
    duration_minutes: Number(formData.get("duration_minutes")),
    questions_per_student: Number(formData.get("questions_per_student")),
    student_ids: String(formData.get("student_ids")).split(",").map((x) => Number(x.trim())).filter(Boolean),
    scheduled_start: new Date(formData.get("scheduled_start")).toISOString(),
  };
  await api(`/faculty/exams?faculty_id=${currentUserId}`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) });
  addEvent("Exam created");
  await loadState();
}

async function uploadQuestions(e, examId) {
  e.preventDefault();
  const formData = new FormData(e.target);
  const studentIds = formData.get("student_ids");
  const fileData = new FormData();
  fileData.append("file", formData.get("file"));
  await api(`/faculty/exams/${examId}/questions?faculty_id=${currentUserId}&student_ids=${encodeURIComponent(studentIds)}`, { method: "POST", body: fileData });
  addEvent(`Questions uploaded for exam ${examId}`);
}

async function sendProctorEvent(e, examId) {
  e.preventDefault();
  const formData = new FormData(e.target);
  await api(`/student/exams/${examId}/proctor-event?student_id=${currentUserId}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ event_type: formData.get("event_type") }),
  });
  addEvent(`Proctor event sent for exam ${examId}`);
  await loadState();
}

async function resumeExam(e, examId) {
  e.preventDefault();
  const formData = new FormData(e.target);
  await api(`/student/exams/${examId}/resume?student_id=${currentUserId}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ passcode: formData.get("passcode") }),
  });
  addEvent(`Resume requested for exam ${examId}`);
  await loadState();
}

async function submitExam(e, examId) {
  e.preventDefault();
  const formData = new FormData(e.target);
  const answers = [];
  for (const [k, v] of formData.entries()) {
    if (k.startsWith("answer_")) {
      answers.push({ question_id: Number(k.replace("answer_", "")), answer_text: String(v) });
    }
  }
  await api(`/student/exams/${examId}/submit?student_id=${currentUserId}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ answers }),
  });
  addEvent(`Submitted exam ${examId}`);
  await loadState();
}

window.createExam = createExam;
window.uploadQuestions = uploadQuestions;
window.sendProctorEvent = sendProctorEvent;
window.resumeExam = resumeExam;
window.submitExam = submitExam;
