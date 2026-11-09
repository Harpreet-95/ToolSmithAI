# ToolSmithAI frontend

The ToolSmithAI web interface is a React 19 application built with Vite. It
provides the AI Workspace, unified database and uploaded-dataset management,
history, reports, scheduling, exports, clarification flows, and visual answers.

## Requirements

- Node.js 20 or newer
- The ToolSmithAI FastAPI backend running at `http://127.0.0.1:8000`

## Install and run

```powershell
npm ci
npm run dev
```

Open `http://localhost:5173`. The Vite development server proxies `/v1` API
requests to the local backend.

## Quality checks

```powershell
npm test
npm run lint
npm run build
```

The frontend never contains production passwords or API keys. Authentication
uses the backend's JWT login flow, and runtime configuration belongs in the
repository-root `.env` file rather than frontend source code.
