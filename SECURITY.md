# Security

*ToolSmithAI was built by Harpreet Kaur.*

## Reporting a concern

This is a portfolio and case-study repository, not a supported product. If you find a security problem, please contact Harpreet Kaur directly through the channel you used to receive this repository rather than opening a public issue. Do not post credentials, tokens or private data anywhere.

## What the application does to protect data

- **Read-only SQL only.** Model-generated SQL is parsed into a T-SQL syntax tree and must be a single read-only statement that references known tables, columns and joins. Credential-like columns are rejected. Destructive requests are refused.
- **Independent database permissions.** Use a dedicated SQL Server login with read-only access to only the schemas and views you need. Application validation and database permissions should each prevent writes on their own.
- **Encrypted connection details.** Stored connection configuration is encrypted with a key you supply (`ENCRYPTION_KEY`).
- **Strong configuration required.** The backend refuses to start with a missing or weak `JWT_SECRET`.
- **Integrity-protected follow-up state.** Conversation state returned to the client is signed and rejected if altered.
- **Cache hygiene.** The validated-plan cache stores structural plans and SQL, not result rows, credentials, prompts or vectors. Plans are scoped by user and source. SQL may contain filter literals, so protect the local metadata database like other application data.
- **Upload limits.** Dataset uploads have a configurable size limit and accept only CSV and Excel files.
- **Generic error messages.** Unexpected server errors return generic messages rather than internal details.
- **Optional email is off by default.**

## Handling secrets

- Copy `.env.example` to `.env` and replace every placeholder. **Never commit `.env`.**
- This repository contains no credentials, private databases, uploaded files, exports or logs. Do not add any.
- Generated local data (SQLite metadata databases, logs, uploads, exports, backups) is ignored by Git.

## Known limitations

This is a pilot-stage prototype. Before serving critical or regulated workloads it needs managed secrets and key rotation, SSO and a tenant-isolation review, managed metadata storage with backups, centralized monitoring and alerting, load and model-quality evaluation, and data-governance approval for each connected source.
