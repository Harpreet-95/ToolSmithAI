# ToolSmithAI Testing Guide

Use this guide to verify a fresh clone or a release candidate. Automated tests use isolated fixtures unless a test is explicitly marked as live. Never run live tests against a production database without authorization.

## Prerequisites

- Python 3.12
- Node.js 20 or newer
- Microsoft ODBC Driver for SQL Server when testing SQL Server connections
- A local `.env` created from `.env.example`
- A non-production test datasource for live connection checks

## Install test dependencies

Backend:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements-dev.txt
```

Frontend:

```powershell
Set-Location frontend
npm ci
```

## Automated checks

Run from the repository root:

```powershell
python -m pytest tests
```

Run frontend checks:

```powershell
Set-Location frontend
npm test
npm run build
npm run lint
```

Run dependency security checks:

```powershell
python -m pip_audit -r requirements.txt
Set-Location frontend
npm audit
```

Do not describe a release as fully green unless the backend tests, frontend tests,
frontend build, frontend lint, and dependency audits complete successfully. Record
any intentionally skipped tests and remaining limitations in the release notes.

## Quick check without a database or AI key

With the backend running and an empty `OPENAI_API_KEY`, you can verify the uploaded-dataset path end to end:

- [ ] Register a local test user and sign in.
- [ ] Upload a small CSV file; the row and column counts should match the file.
- [ ] Select it in the AI Workspace and generate a report.
- [ ] Export the report to CSV, Excel and PDF and open each file.
- [ ] Open History and Reports; both should load.
- [ ] Ask a database question with no source selected; the app should ask you to select a source.

Use only non-sensitive sample data. AI-written answers and SQL question answering need an OpenAI key and an enabled SQL Server source.

## Start the application

Backend terminal:

```powershell
python -m uvicorn api.app:app --reload --host 127.0.0.1 --port 8000
```

Frontend terminal:

```powershell
Set-Location frontend
npm run dev
```

Open `http://localhost:5173` and sign in with a test account.

## Manual smoke checklist

### Authentication and navigation

- [ ] Register or sign in with a test account.
- [ ] Refresh the browser and confirm the session remains usable.
- [ ] For a standard user, confirm the sidebar shows AI Workspace, Overview,
      Workflows, Data Sources, Scheduled, History, Reports, and Settings.
- [ ] Confirm administrator-only operational pages do not appear for a standard user.
- [ ] Confirm Data Sources opens the combined dataset/database landing page.
- [ ] Confirm History loads and shows no records older than seven days.
- [ ] Sign out and confirm protected pages are no longer accessible.

### Uploaded datasets

- [ ] Upload one CSV file and one Excel file.
- [ ] Confirm each item shows the correct name, row count, column count, and status.
- [ ] Select an uploaded dataset from the single AI Workspace source selector.
- [ ] Run one dataset analysis and confirm the answer is tied to the selected dataset.
- [ ] Generate a report and verify its content.
- [ ] Export a result in each enabled format.
- [ ] Delete a disposable upload and confirm it disappears.

### Database connections

- [ ] Add or reconnect a non-production database connection.
- [ ] Open the datasource card directly and confirm the summary shows its database, schemas, tables, views, and profiling status.
- [ ] Confirm disconnect, remove credentials, reconnect, and permanent delete are presented as different actions.
- [ ] Confirm permanent delete requires explicit destructive confirmation.
- [ ] Confirm a disconnected source cannot answer questions but retains its metadata and validated plans.

### AI-first SQL answering

Select a prepared SQL datasource and verify:

- [ ] A scalar question returns a current value and an appropriate KPI.
- [ ] A time-series question returns a readable table, chart, and grounded insight when its result shape supports them.
- [ ] A ranked-category question returns ordered data with readable full labels or an accessible full-label fallback.
- [ ] A detail question returns a table without inventing a chart.
- [ ] A genuinely ambiguous question asks for clarification instead of guessing.
- [ ] A follow-up preserves the relevant prior filters and source context.
- [ ] New Query clears the previous conversation context.
- [ ] A destructive request is rejected and no write SQL is executed.
- [ ] Broad results show truncation clearly and do not produce misleading insights.

### Cache and fresh data

- [ ] Ask a supported SQL question once and confirm a cache miss.
- [ ] Ask the identical question again and confirm a cache hit.
- [ ] Confirm a cache hit revalidates the SQL and executes it against the live datasource.
- [ ] Restart the backend and confirm the validated-plan cache still produces a hit.
- [ ] Confirm result rows, credentials, secrets, prompts, and PII are not stored in the durable plan table.

### Export, scheduling, and email

- [ ] Export a small answer to CSV, Excel, and PDF and open each file.
- [ ] Export a result over 40 rows and confirm PDF generation succeeds.
- [ ] Create a disposable scheduled workflow and confirm its next-run state.
- [ ] Keep real email delivery disabled unless an authorized test recipient is configured.
- [ ] When authorized, send only to the approved test recipient and confirm delivery status is recorded.

## Safety checks

- [ ] `.env`, local SQLite databases, logs, uploads, exports, backups, and dependency folders are not tracked by Git.
- [ ] Generated SQL is read-only and passes deterministic validation before execution.
- [ ] Secret-bearing columns are rejected.
- [ ] Application logs do not contain credentials, raw tokens, or unmasked sensitive rows.
- [ ] Cross-user and cross-datasource cache isolation tests pass.
- [ ] Tests do not use the real project metadata database unless explicitly marked and authorized.

## Release evidence

For a handoff or demo, save:

- the Git commit SHA;
- backend and frontend test totals;
- frontend build and lint results;
- the tested browser and viewport;
- the datasource type used for live verification;
- cold and warm response timings;
- any known limitations that remain.

Do not commit credentials, database files, screenshots containing sensitive rows, or live result exports.

### Response-time record

Record measurements from the actual demonstration environment rather than
copying development-session timings:

| Question | Source | Cold time | Warm time | Cache status | SQL executed live |
| --- | --- | ---: | ---: | --- | --- |
| Example question | Test source | ___ s | ___ s | MISS / HIT | Yes / No |
