# ToolSmithAI — Manual Test Checklist

**MVP scope only. Do not document unbuilt features as passing.**
Run this checklist after every interpreter or dashboard change.

---

## Prerequisites

- Backend running: `uvicorn api.app:app --reload` (port 8000)
- Frontend running: `npm run dev` inside `frontend/` (port 5173)
- `.env` file present with valid `JWT_SECRET`, `ENCRYPTION_KEY`, `USER_ID_SALT`

---

## 1. Auth Tests

### QA Run — 2026-05-17

| Test ID  | Description             | Result | Notes                                      |
|----------|-------------------------|--------|--------------------------------------------|
| AUTH-01  | Register new user       | ✅ PASS | Redirect to sign-in page worked correctly  |
| AUTH-05  | Login happy path        | ✅ PASS | Login succeeded immediately after register |
| AUTH-10  | Logout clears session   | ✅ PASS | `ts_token` + `ts_user` removed correctly  |

Observed: no frontend errors, no API failures, no session persistence bugs.
Tested by: Harpreet · Environment: localhost (dev)

---

### 1.1 Register
- [x] Navigate to the register screen
- [x] Submit with name, email, password
- [x] Expected: redirected to dashboard immediately (email verification not enforced)
- [x] Expected: `ts_token` and `ts_user` present in `localStorage`
> **AUTH-01 — PASS** · 2026-05-17

### 1.2 Login
- [x] Log out, then log back in with same credentials
- [x] Expected: dashboard loads, usage and history fetched
- [x] Expected: token persists on page refresh
> **AUTH-05 — PASS** · 2026-05-17

### 1.3 Invalid login
- [ ] Submit wrong password
- [ ] Expected: inline error message, no redirect

### 1.4 Session expiry
- [ ] Let the JWT expire (default: 60 min), then attempt to execute a workflow
- [ ] Expected: user is logged out automatically
- [ ] Expected: login screen shows amber banner: "Your session expired. Please log in again."
- [ ] Expected: banner clears after successful re-login

### 1.5 Logout
- [x] Click Logout
- [x] Expected: redirected to login screen, `ts_token` and `ts_user` removed from `localStorage`
> **AUTH-10 — PASS** · 2026-05-17

---

## 2. Dashboard Tests

### 2.1 Overview tab on load
- [ ] Usage stat cards show live counts (Tasks Run, Successful Workflows, Workflow Runs)
- [ ] Recent Activity table shows up to 5 most recent execution history rows
- [ ] Recent Activity shows "No recent activity" message if history is empty

### 2.2 Workflow Composer — empty input
- [ ] Click Execute Workflow with empty textarea
- [ ] Expected: inline error "Please enter a task description." — no API call made

### 2.3 Workflow Composer — short input
- [ ] Type `hi`, click Execute Workflow
- [ ] Expected: inline error "Task description must be at least 5 characters." — no API call made

### 2.4 Execute button state
- [ ] Submit a valid task
- [ ] Expected: button label changes to "Executing..." and is disabled during the request
- [ ] Expected: button returns to "Execute Workflow" after completion

### 2.5 History tab
- [ ] Run at least one workflow, then open History tab
- [ ] Expected: row appears with intent, task type, status badge, started_at timestamp
- [ ] Expected: status badge is green for completed, red for failed

### 2.6 Usage tab
- [ ] Open Usage tab after running workflows
- [ ] Expected: stat cards reflect updated counts
- [ ] Expected: Recent Usage Events table shows event_type, source, and timestamp for each event
- [ ] Expected: "No usage events recorded yet." shown if no events exist

### 2.7 Settings tab
- [ ] Open Settings tab
- [ ] Expected: Account card shows correct name, email, and role from JWT

---

## 3. Interpreter Tests

Backend must be running. All inputs are submitted via the Workflow Composer.

### 3.1 Email task — `email me daily report`

**Input:** `email me daily report`

Expected result panel:
- Status badge: `completed` (green)
- Detected Intent: shows the original input
- Execution Steps: one step — `email_sender · send_email`, status Success
- Step output: `"Email sent successfully (simulated)"`
- Summary: Steps: 1, Started/Finished timestamps visible
- No Notice card shown

Expected history row:
- task_type: `send_email`
- status: `completed`

### 3.2 Report task — `generate a weekly summary`

**Input:** `generate a weekly summary`

Expected result panel:
- Status badge: `completed` (green)
- Detected Intent: shows the original input
- Execution Steps: two steps — `data_fetcher · fetch_report_data` then `email_sender · send_email`, both Success
- Summary: Steps: 2
- No Notice card shown

Expected history row:
- task_type: `generate_report`
- status: `completed`

### 3.3 Reminder task — `remind me monthly`

**Input:** `remind me monthly`

Expected result panel:
- Status badge: `completed` (green)
- Detected Intent: shows the original input
- Execution Steps: one step — `notifier · send_notification`, status Success
- Step output: `"Notification delivered successfully (simulated)"`
- No Notice card shown

Expected history row:
- task_type: `set_reminder`
- status: `completed`

### 3.4 Unknown task — `organize my invoices`

**Input:** `organize my invoices`

Expected result panel:
- Status badge: `completed` (green) — backend returns completed even with no steps
- Detected Intent: shows the original input
- Notice card (amber): "No supported tool intent detected yet."
- No Execution Steps section shown (empty step list)

Expected history row:
- task_type: `unknown`
- status: `completed`

---

## 4. Regression Checklist

Run after every change to the interpreter or dashboard.

### After any interpreter change
- [ ] 3.1 email task still produces `send_email` steps
- [ ] 3.2 report task still produces 2 steps in correct order
- [ ] 3.3 reminder task still produces `set_reminder` step
- [ ] 3.4 unknown task shows Notice card and no steps
- [ ] History row is written for all 4 cases
- [ ] Usage event is recorded for all 4 cases

### After any dashboard change
- [ ] Login and register still work
- [ ] Session expiry banner still shows on 401
- [ ] Execute Workflow validation still blocks empty and short inputs
- [ ] Result panel renders summary, intent, steps for a known task
- [ ] Result panel renders Notice card for unknown task
- [ ] History tab updates after task execution
- [ ] Usage tab updates after task execution
- [ ] Recent Activity on Overview tab updates after task execution

---

## 5. Dataset Tests

### QA Run — 2026-05-17

| Test ID     | Description          | Result  | Notes                                           |
|-------------|----------------------|---------|-------------------------------------------------|
| DATASET-01  | CSV upload           | ✅ PASS  | Parsed correctly, summary rendered              |
| DATASET-02  | XLSX upload          | ✅ PASS  | Parsed correctly via openpyxl                   |
| DATASET-03  | XLS upload           | ✅ PASS  | Parsed correctly via xlrd                       |
| DATASET-04  | Dataset rename       | ✅ PASS  | Inline rename saved and reflected correctly     |
| DATASET-05  | Dataset delete       | ✅ PASS  | Removed from list, selection moved to next      |
| DATASET-06  | Dataset select/active| ✅ PASS  | Row highlights, summary updates on click        |

Observed: dataset list updated after each upload, row/column counts correct, dataset insights updated,
no frontend rendering issues, no backend parsing failures, no auth regressions during upload flow.
Tested by: Harpreet · Environment: localhost (dev)

---

### 5.1 CSV Upload
- [x] Select a `.csv` file and click Upload Dataset
- [x] Expected: upload succeeds with no errors
- [x] Expected: dataset appears in My Datasets list
- [x] Expected: row count and column count displayed correctly
- [x] Expected: Dataset Summary panel renders with column names and sample rows
- [x] Expected: Dataset Insights on Overview tab updates
> **DATASET-01 — PASS** · 2026-05-17

### 5.2 XLSX Upload
- [x] Select a `.xlsx` file and click Upload Dataset
- [x] Expected: upload succeeds, parsed via openpyxl
- [x] Expected: dataset appears in My Datasets list with correct row/column counts
- [x] Expected: Dataset Summary panel renders correctly
> **DATASET-02 — PASS** · 2026-05-17

### 5.3 XLS Upload
- [x] Select a `.xls` file and click Upload Dataset
- [x] Expected: upload succeeds, parsed via xlrd
- [x] Expected: dataset appears in My Datasets list with correct row/column counts
- [x] Expected: Dataset Summary panel renders correctly
> **DATASET-03 — PASS** · 2026-05-17

### 5.4 Dataset rename
- [x] Click ⋮ → Rename on an uploaded dataset
- [x] Expected: inline input appears, Enter saves the new name
- [x] Expected: updated name reflected in the list immediately
> **DATASET-04 — PASS** · 2026-05-17

### 5.5 Dataset delete
- [x] Click ⋮ → Delete on an uploaded dataset
- [x] Expected: confirmation modal appears
- [x] Expected: dataset removed from list after confirmation
- [x] Expected: if deleted dataset was active, selection moves to next available
> **DATASET-05 — PASS** · 2026-05-17

### 5.6 Dataset select / activate
- [x] Click a non-active dataset row
- [x] Expected: row highlights, status changes to Active
- [x] Expected: Dataset Summary panel updates to the selected dataset
> **DATASET-06 — PASS** · 2026-05-17

### 5.7 Empty file upload
- [ ] Attempt to upload a file with 0 data rows (headers only)
- [ ] Expected: backend returns a clear error or empty-state summary — no crash

---

## Known Limitations (not bugs)

- All tool execution is **simulated** — no real emails are sent, no real data is fetched
- Email verification is disabled — users can log in without verifying their email
- JWT expires after 60 minutes with no automatic refresh
- `usage_events` and `tenants` tables must exist in the DB — they are not created by `init_db()` on a fresh install
- Interpreter is keyword-matching only — no LLM integration yet

---

## 6. Workflow Routing & Dataset Context Tests

### QA Run — 2026-05-17

| Test ID  | Description                        | Result  | Notes                                                              |
|----------|------------------------------------|---------|--------------------------------------------------------------------|
| WF-01    | Single workflow execution          | ✅ PASS  | Executed correctly, result panel showed correct steps              |
| WF-02    | Dataset-aware report generation    | ✅ PASS  | Selected dataset context respected; routed to `generate_dataset_report` |
| WF-03    | Email report workflow routing      | ✅ PASS  | Email keywords + dataset context routed to `email_dataset_report`  |

Root cause fixed: `input_handler.py` called `interpret_task(user_input)` without considering the
selected dataset context. Generic report keywords (e.g. "generate report") matched the `generate_report`
rule and produced unrelated `data_fetcher` + `email_sender` steps.

Fix applied: dataset-aware pre-routing intercept added in `core/input/input_handler.py`. When
`dataset_id` is present and input contains a report-related keyword, routing bypasses `interpret_task()`
and calls the dataset-specific plan builders directly.

Observed: no regressions in existing workflow execution; unrelated steps no longer appear when a
dataset is selected.
Tested by: Harpreet · Environment: localhost (dev)

---

### 6.1 Single workflow execution
- [x] Submit a task via the Workflow Composer with no dataset selected
- [x] Expected: correct task type detected, correct steps returned, result panel renders
- [x] Expected: execution history row written with correct `task_type` and `status: completed`
> **WF-01 — PASS** · 2026-05-17

### 6.2 Dataset-aware report generation
- [x] Select an uploaded dataset in the Workflow Composer
- [x] Type `generate report` and click Execute Workflow
- [x] Expected: task routes to `generate_dataset_report` (not generic `generate_report`)
- [x] Expected: no `data_fetcher` or `email_sender` steps in the result panel
- [x] Expected: result ties to the selected dataset
> **WF-02 — PASS** · 2026-05-17

### 6.3 Email report workflow routing
- [x] Select an uploaded dataset in the Workflow Composer
- [x] Type `email report` and click Execute Workflow
- [x] Expected: task routes to `email_dataset_report`
- [x] Expected: no unrelated steps; report is dataset-scoped
> **WF-03 — PASS** · 2026-05-17

### 6.4 Unrelated task with dataset selected (regression)
- [ ] Select an uploaded dataset, then type `remind me daily`
- [ ] Expected: routes to `set_reminder` as normal — dataset context does not pollute unrelated intents

### 6.5 No dataset selected — report keywords use generic routing
- [ ] Ensure no dataset is selected, then type `generate report`
- [ ] Expected: routes to generic `generate_report` (data_fetcher + email_sender) as before

### 6.6 Dataset selected — ambiguous input
- [ ] Select a dataset, then type `run workflow`
- [ ] Expected: no report-hint words matched; routes normally through `interpret_task()`
