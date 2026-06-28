const AUTH_HEADERS = (token) => ({
  'Content-Type': 'application/json',
  Authorization: `Bearer ${token}`,
});

async function parseResponse(res) {
  const text = await res.text();
  let data = null;
  if (text) {
    try {
      data = JSON.parse(text);
    } catch {
      throw new Error(`${res.status}: Server returned non-JSON response — ${text.slice(0, 120)}`);
    }
  }
  if (!res.ok) {
    const message = data?.message || data?.detail || text || `HTTP ${res.status}`;
    throw new Error(`${res.status}: ${message}`);
  }
  return data;
}

export async function verifyKey(token) {
  const res = await fetch('/v1/usage', {
    headers: AUTH_HEADERS(token),
  });
  return parseResponse(res);
}

export async function interpretTask(task, token, datasetId = null, recipient = null, selectedSections = null) {
  const body = { input: task };
  if (datasetId != null) body.dataset_id = datasetId;
  if (recipient != null && recipient.trim() !== '') body.recipient = recipient.trim();
  if (selectedSections != null && selectedSections.length > 0) body.selected_sections = selectedSections;
  const res = await fetch('/v1/interpret', {
    method: 'POST',
    headers: AUTH_HEADERS(token),
    body: JSON.stringify(body),
  });
  return parseResponse(res);
}

export async function getDatasets(token) {
  const res = await fetch('/v1/datasets', {
    headers: AUTH_HEADERS(token),
  });
  return parseResponse(res);
}

export async function getDatasetById(id, token) {
  const res = await fetch(`/v1/datasets/${id}`, {
    headers: AUTH_HEADERS(token),
  });
  return parseResponse(res);
}

export async function getMyData(token) {
  const res = await fetch('/v1/me/data', {
    headers: AUTH_HEADERS(token),
  });
  return parseResponse(res);
}

export async function getUsage(token) {
  const res = await fetch('/v1/usage', {
    headers: AUTH_HEADERS(token),
  });
  return parseResponse(res);
}

export async function registerUser({ name, email, password, role = 'user' }) {
  const res = await fetch('/v1/auth/register', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ name, email, password, role }),
  });
  return parseResponse(res);
}

export async function loginUser(email, password) {
  const res = await fetch('/v1/auth/login', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ email, password }),
  });
  return parseResponse(res);
}

export async function verifyEmail(token) {
  const res = await fetch(`/v1/auth/verify-email?token=${encodeURIComponent(token)}`);
  return parseResponse(res);
}

export async function registerAdmin({ name, email, password, invite_token }) {
  const res = await fetch('/v1/auth/register-admin', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ name, email, password, invite_token }),
  });
  return parseResponse(res);
}

export async function changePassword(token, { currentPassword, newPassword, confirmPassword }) {
  const res = await fetch('/v1/auth/change-password', {
    method: 'POST',
    headers: AUTH_HEADERS(token),
    body: JSON.stringify({
      current_password: currentPassword,
      new_password: newPassword,
      confirm_password: confirmPassword,
    }),
  });
  return parseResponse(res);
}

export async function createScheduledWorkflow(inputText, token, datasetId = null, refreshBeforeRun = false) {
  const body = { input_text: inputText };
  if (datasetId != null) body.dataset_id = datasetId;
  if (refreshBeforeRun) body.refresh_before_run = true;
  const res = await fetch('/v1/scheduled-workflows', {
    method: 'POST',
    headers: AUTH_HEADERS(token),
    body: JSON.stringify(body),
  });
  return parseResponse(res);
}

export async function getScheduledWorkflows(token) {
  const res = await fetch('/v1/scheduled-workflows', {
    headers: AUTH_HEADERS(token),
  });
  return parseResponse(res);
}

export async function deleteScheduledWorkflow(id, token) {
  const res = await fetch(`/v1/scheduled-workflows/${id}`, {
    method: 'DELETE',
    headers: AUTH_HEADERS(token),
  });
  return parseResponse(res);
}

export async function pauseScheduledWorkflow(id, token) {
  const res = await fetch(`/v1/scheduled-workflows/${id}/pause`, {
    method: 'PATCH',
    headers: AUTH_HEADERS(token),
  });
  return parseResponse(res);
}

export async function resumeScheduledWorkflow(id, token) {
  const res = await fetch(`/v1/scheduled-workflows/${id}/resume`, {
    method: 'PATCH',
    headers: AUTH_HEADERS(token),
  });
  return parseResponse(res);
}

export async function getWorkflows(token) {
  const res = await fetch('/v1/workflows', {
    headers: AUTH_HEADERS(token),
  });
  return parseResponse(res);
}

export async function saveWorkflow(name, inputText, token) {
  const definition = {
    steps: [{
      step_id: 'run_intent',
      order: 1,
      tool: 'notifier',
      operation: 'send_notification',
      params: { channel: 'in_app', message: inputText.slice(0, 500), priority: 'normal' },
      depends_on: null,
    }],
    intent: inputText,
  };
  const res = await fetch('/v1/workflows', {
    method: 'POST',
    headers: AUTH_HEADERS(token),
    body: JSON.stringify({ name, definition }),
  });
  return parseResponse(res);
}

export async function deleteWorkflow(id, token) {
  const res = await fetch(`/v1/workflows/${id}`, {
    method: 'DELETE',
    headers: AUTH_HEADERS(token),
  });
  return parseResponse(res);
}

export async function deleteDataset(id, token) {
  const res = await fetch(`/v1/datasets/${id}`, {
    method: 'DELETE',
    headers: AUTH_HEADERS(token),
  });
  return parseResponse(res);
}

export async function renameDataset(id, filename, token) {
  const res = await fetch(`/v1/datasets/${id}`, {
    method: 'PATCH',
    headers: AUTH_HEADERS(token),
    body: JSON.stringify({ filename }),
  });
  return parseResponse(res);
}

export async function reprofileDataset(id, token) {
  const res = await fetch(`/v1/datasets/${id}/reprofile`, {
    method: 'POST',
    headers: AUTH_HEADERS(token),
  });
  return parseResponse(res);
}

export async function uploadDataset(file, token) {
  const formData = new FormData();
  formData.append('file', file);
  const res = await fetch('/v1/datasets/upload', {
    method: 'POST',
    headers: { Authorization: `Bearer ${token}` },
    body: formData,
  });
  return parseResponse(res);
}

export async function getRecommendations(token) {
  const res = await fetch('/v1/recommendations', {
    headers: AUTH_HEADERS(token),
  });
  return parseResponse(res);
}

export async function getInsights(token) {
  const res = await fetch('/v1/insights', {
    headers: AUTH_HEADERS(token),
  });
  return parseResponse(res);
}

export async function explainContext(body, token) {
  const res = await fetch('/v1/assistant/explain', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${token}` },
    body: JSON.stringify(body),
  });
  return parseResponse(res);
}

export async function getWorkflowTemplates(token) {
  const res = await fetch('/v1/workflow-templates', {
    headers: AUTH_HEADERS(token),
  });
  return parseResponse(res);
}

export async function getScheduleHealth(token) {
  const res = await fetch('/v1/schedule-health', {
    headers: AUTH_HEADERS(token),
  });
  return parseResponse(res);
}

export async function retryExecution(id, token) {
  const res = await fetch(`/v1/executions/${id}/retry`, {
    method: 'POST',
    headers: AUTH_HEADERS(token),
  });
  return parseResponse(res);
}

export async function rerunExecution(id, token) {
  const res = await fetch(`/v1/executions/${id}/rerun`, {
    method: 'POST',
    headers: AUTH_HEADERS(token),
  });
  return parseResponse(res);
}

export async function createMultiStepWorkflow(name, steps, token) {
  const definition = {
    workflow_steps: steps,
    steps: [],
  };
  const res = await fetch('/v1/workflows', {
    method: 'POST',
    headers: AUTH_HEADERS(token),
    body: JSON.stringify({ name, definition }),
  });
  return parseResponse(res);
}

export async function runWorkflowById(id, token) {
  if (!id) {
    console.error('[runWorkflowById] called with missing workflow ID:', id)
    throw new Error('Cannot execute workflow: workflow ID is missing.')
  }
  const res = await fetch(`/v1/workflows/${id}/run`, {
    method: 'POST',
    headers: AUTH_HEADERS(token),
  });
  return parseResponse(res);
}

export async function dryRunWorkflow(id, token) {
  const res = await fetch(`/v1/workflows/${id}/dry-run`, {
    method: 'POST',
    headers: AUTH_HEADERS(token),
  });
  return parseResponse(res);
}

export async function getDynamicTools(token) {
  const res = await fetch('/v1/tools', { headers: AUTH_HEADERS(token) });
  return parseResponse(res);
}

export async function createDynamicTool(payload, token) {
  const res = await fetch('/v1/tools', {
    method: 'POST',
    headers: AUTH_HEADERS(token),
    body: JSON.stringify(payload),
  });
  return parseResponse(res);
}

export async function updateDynamicTool(id, payload, token) {
  const res = await fetch(`/v1/tools/${id}`, {
    method: 'PATCH',
    headers: AUTH_HEADERS(token),
    body: JSON.stringify(payload),
  });
  return parseResponse(res);
}

export async function approveDynamicTool(id, token) {
  const res = await fetch(`/v1/tools/${id}/approve`, {
    method: 'PATCH',
    headers: AUTH_HEADERS(token),
  });
  return parseResponse(res);
}

export async function getReports(token) {
  const res = await fetch('/v1/reports', {
    headers: AUTH_HEADERS(token),
  });
  return parseResponse(res);
}

export async function getReportById(reportId, token) {
  const res = await fetch(`/v1/reports/${reportId}`, {
    headers: AUTH_HEADERS(token),
  });
  return parseResponse(res);
}

export async function deleteReport(reportId, token) {
  const res = await fetch(`/v1/reports/${reportId}`, {
    method: 'DELETE',
    headers: AUTH_HEADERS(token),
  });
  return parseResponse(res);
}

export async function exportReport(reportId, token, format = 'json') {
  const res = await fetch(`/v1/reports/${reportId}/export?format=${format}`, {
    headers: AUTH_HEADERS(token),
  });
  if (!res.ok) {
    const text = await res.text();
    let msg;
    try { msg = JSON.parse(text)?.message || text; } catch { msg = text; }
    throw new Error(`${res.status}: ${msg}`);
  }
  const blob = await res.blob();
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  const cd = res.headers.get('Content-Disposition') || '';
  const match = cd.match(/filename="([^"]+)"/);
  a.download = match ? match[1] : `report_${reportId}.json`;
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  URL.revokeObjectURL(url);
}

export async function emailReport(reportId, recipientEmail, token) {
  const res = await fetch(`/v1/reports/${reportId}/email`, {
    method: 'POST',
    headers: AUTH_HEADERS(token),
    body: JSON.stringify({ recipient_email: recipientEmail }),
  });
  return parseResponse(res);
}

export async function getNotifications(token) {
  const res = await fetch('/v1/notifications', {
    headers: AUTH_HEADERS(token),
  });
  return parseResponse(res);
}

export async function markNotificationRead(id, token) {
  const res = await fetch(`/v1/notifications/${id}/read`, {
    method: 'POST',
    headers: AUTH_HEADERS(token),
  });
  return parseResponse(res);
}

export async function deleteNotification(id, token) {
  const res = await fetch(`/v1/notifications/${id}`, {
    method: 'DELETE',
    headers: AUTH_HEADERS(token),
  });
  return parseResponse(res);
}

export async function getScheduleRuns(token) {
  const res = await fetch('/v1/schedules/runs', {
    headers: AUTH_HEADERS(token),
  });
  return parseResponse(res);
}

export async function getScheduleRunHistory(scheduleId, token) {
  const res = await fetch(`/v1/schedules/${scheduleId}/runs`, {
    headers: AUTH_HEADERS(token),
  });
  return parseResponse(res);
}

export async function runScheduleNow(scheduleId, token) {
  const res = await fetch(`/v1/schedules/${scheduleId}/run-now`, {
    method: 'POST',
    headers: AUTH_HEADERS(token),
  });
  return parseResponse(res);
}

export async function composeIntent(intent, datasetId, token, saveWorkspace = false) {
  const body = { intent, save_workspace: saveWorkspace };
  if (datasetId != null) body.dataset_id = datasetId;
  const res = await fetch('/v1/tools/compose', {
    method: 'POST',
    headers: AUTH_HEADERS(token),
    body: JSON.stringify(body),
  });
  return parseResponse(res);
}

export async function getWorkspaces(token) {
  const res = await fetch('/v1/workspaces', { headers: AUTH_HEADERS(token) });
  return parseResponse(res);
}

export async function getWorkspaceById(id, token) {
  const res = await fetch(`/v1/workspaces/${id}`, { headers: AUTH_HEADERS(token) });
  return parseResponse(res);
}

export async function attachWorkspaceExecution(id, payload, token) {
  const res = await fetch(`/v1/workspaces/${id}/execution`, {
    method: 'PATCH',
    headers: AUTH_HEADERS(token),
    body: JSON.stringify(payload),
  });
  return parseResponse(res);
}

export async function saveWorkspaceById(id, token) {
  const res = await fetch(`/v1/workspaces/${id}/save`, {
    method: 'PATCH',
    headers: AUTH_HEADERS(token),
  });
  return parseResponse(res);
}

export async function createWorkflowDraftFromWorkspace(workspaceId, token) {
  const res = await fetch(`/v1/workspaces/${workspaceId}/create-workflow-draft`, {
    method: 'POST',
    headers: AUTH_HEADERS(token),
  });
  return parseResponse(res);
}

export async function askReport(reportId, question, token) {
  const res = await fetch(`/v1/reports/${reportId}/ask`, {
    method: 'POST',
    headers: AUTH_HEADERS(token),
    body: JSON.stringify({ question }),
  });
  return parseResponse(res);
}

export async function askDataset(datasetId, question, composerText, token) {
  const res = await fetch(`/v1/datasets/${datasetId}/ask`, {
    method: 'POST',
    headers: AUTH_HEADERS(token),
    body: JSON.stringify({ question, composer_text: composerText ?? null }),
  });
  return parseResponse(res);
}

export async function createAdminInvite(email, token) {
  const res = await fetch('/v1/admin/invites', {
    method: 'POST',
    headers: AUTH_HEADERS(token),
    body: JSON.stringify({ email }),
  });
  return parseResponse(res);
}

// ---------------------------------------------------------------------------
// Dynamic Tool Creation Engine — lifecycle API
// ---------------------------------------------------------------------------

export async function planEngineTool(intent, token, context = null) {
  const body = { intent };
  if (context != null) body.context = context;
  const res = await fetch('/v1/engine/tools/plan', {
    method: 'POST',
    headers: AUTH_HEADERS(token),
    body: JSON.stringify(body),
  });
  return parseResponse(res);
}

export async function saveEngineTool(toolDefinition, token) {
  const res = await fetch('/v1/engine/tools/save', {
    method: 'POST',
    headers: AUTH_HEADERS(token),
    // source="ai_workspace" tells the backend this originated from the
    // "Save as Reusable Workflow" CTA — enabling autonomous auto-approval
    // when ENABLE_AUTO_APPROVE_ENGINE_TOOLS=true is set server-side.
    body: JSON.stringify({ tool_definition: toolDefinition, source: 'ai_workspace' }),
  });
  return parseResponse(res);
}

export async function submitEngineTool(toolId, token) {
  const res = await fetch(`/v1/engine/tools/${toolId}/submit`, {
    method: 'POST',
    headers: AUTH_HEADERS(token),
  });
  return parseResponse(res);
}

export async function approveEngineTool(toolId, token) {
  const res = await fetch(`/v1/engine/tools/${toolId}/approve`, {
    method: 'POST',
    headers: AUTH_HEADERS(token),
  });
  return parseResponse(res);
}

export async function executeEngineTool(toolId, inputs, token) {
  const res = await fetch(`/v1/engine/tools/${toolId}/execute`, {
    method: 'POST',
    headers: AUTH_HEADERS(token),
    body: JSON.stringify({ inputs: inputs || {} }),
  });
  return parseResponse(res);
}

export async function listEngineTools(token) {
  const res = await fetch('/v1/engine/tools', {
    headers: AUTH_HEADERS(token),
  });
  return parseResponse(res);
}

export async function getEngineTool(toolId, token) {
  const res = await fetch(`/v1/engine/tools/${toolId}`, {
    headers: AUTH_HEADERS(token),
  });
  return parseResponse(res);
}

export async function getEngineToolRuns(toolId, token) {
  const res = await fetch(`/v1/engine/tools/${toolId}/runs`, {
    headers: AUTH_HEADERS(token),
  });
  return parseResponse(res);
}

export async function getEngineRun(runId, token) {
  const res = await fetch(`/v1/engine/runs/${runId}`, {
    headers: AUTH_HEADERS(token),
  });
  return parseResponse(res);
}

export async function getAdminExportLogs(token, params = {}) {
  const qs = new URLSearchParams()
  for (const [k, v] of Object.entries(params)) {
    if (v != null && v !== '') qs.set(k, v)
  }
  const res = await fetch(`/v1/admin/export-logs?${qs}`, { headers: AUTH_HEADERS(token) })
  return parseResponse(res)
}

export async function getAdminExportLogSummary(token) {
  const res = await fetch('/v1/admin/export-logs/summary', { headers: AUTH_HEADERS(token) })
  return parseResponse(res)
}

export async function getAdminEmailLogs(token, params = {}) {
  const qs = new URLSearchParams()
  for (const [k, v] of Object.entries(params)) {
    if (v != null && v !== '') qs.set(k, v)
  }
  const res = await fetch(`/v1/admin/email-logs?${qs}`, { headers: AUTH_HEADERS(token) })
  return parseResponse(res)
}

export async function getAdminEmailLogSummary(token) {
  const res = await fetch('/v1/admin/email-logs/summary', { headers: AUTH_HEADERS(token) })
  return parseResponse(res)
}

export async function createDataSource(payload, token) {
  const res = await fetch('/v1/sources', {
    method: 'POST',
    headers: AUTH_HEADERS(token),
    body: JSON.stringify(payload),
  })
  return parseResponse(res)
}

export async function listDataSources(token) {
  const res = await fetch('/v1/sources', {
    headers: AUTH_HEADERS(token),
  })
  return parseResponse(res)
}

export async function testDataSource(id, token) {
  const res = await fetch(`/v1/sources/${id}/test`, {
    method: 'POST',
    headers: AUTH_HEADERS(token),
  })
  return parseResponse(res)
}

export async function deleteDataSource(id, token) {
  const res = await fetch(`/v1/sources/${id}`, {
    method: 'DELETE',
    headers: AUTH_HEADERS(token),
  })
  return parseResponse(res)
}

export async function discoverDataSourceSchema(id, token) {
  const res = await fetch(`/v1/sources/${id}/discover`, {
    method: 'POST',
    headers: AUTH_HEADERS(token),
  })
  return parseResponse(res)
}

export async function getDataSourceSchema(id, token) {
  const res = await fetch(`/v1/sources/${id}/schema`, {
    headers: AUTH_HEADERS(token),
  })
  return parseResponse(res)
}

export async function listDictionaryTables(sourceId, token) {
  const res = await fetch(`/v1/sources/${sourceId}/dictionary`, {
    headers: AUTH_HEADERS(token),
  })
  return parseResponse(res)
}

export async function getDictionaryTable(sourceId, tableFqn, token) {
  const res = await fetch(`/v1/sources/${sourceId}/dictionary/tables/${encodeURIComponent(tableFqn)}`, {
    headers: AUTH_HEADERS(token),
  })
  return parseResponse(res)
}

export async function approveDictionaryTable(sourceId, tableFqn, token) {
  const res = await fetch(
    `/v1/sources/${sourceId}/dictionary/tables/${encodeURIComponent(tableFqn)}/approve`,
    { method: 'POST', headers: AUTH_HEADERS(token) },
  )
  return parseResponse(res)
}

export async function approveDictionaryColumn(sourceId, tableFqn, columnName, token) {
  const res = await fetch(
    `/v1/sources/${sourceId}/dictionary/tables/${encodeURIComponent(tableFqn)}/columns/${encodeURIComponent(columnName)}/approve`,
    { method: 'POST', headers: AUTH_HEADERS(token) },
  )
  return parseResponse(res)
}

// ---------------------------------------------------------------------------
// Data Source — metadata pipeline (profile / dictionary / domains / entities)
// ---------------------------------------------------------------------------

export async function generateDictionaryForSource(sourceId, token) {
  const res = await fetch(`/v1/sources/${sourceId}/dictionary/generate`, {
    method: 'POST',
    headers: AUTH_HEADERS(token),
  })
  return parseResponse(res)
}

export async function getProfile(sourceId, token) {
  const res = await fetch(`/v1/sources/${sourceId}/profile`, {
    headers: AUTH_HEADERS(token),
  })
  return parseResponse(res)
}

export async function generateDomains(sourceId, token) {
  const res = await fetch(`/v1/sources/${sourceId}/domains/generate`, {
    method: 'POST',
    headers: AUTH_HEADERS(token),
  })
  return parseResponse(res)
}

export async function getDomainSummary(sourceId, token) {
  const res = await fetch(`/v1/sources/${sourceId}/domains/summary`, {
    headers: AUTH_HEADERS(token),
  })
  return parseResponse(res)
}

export async function generateEntities(sourceId, token) {
  const res = await fetch(`/v1/sources/${sourceId}/entities/generate`, {
    method: 'POST',
    headers: AUTH_HEADERS(token),
  })
  return parseResponse(res)
}

export async function getEntitySummary(sourceId, token) {
  const res = await fetch(`/v1/sources/${sourceId}/entities/summary`, {
    headers: AUTH_HEADERS(token),
  })
  return parseResponse(res)
}

export async function getMetadataJob(jobId, token) {
  const res = await fetch(`/v1/metadata-jobs/${jobId}`, {
    headers: AUTH_HEADERS(token),
  })
  return parseResponse(res)
}

export async function runMetadataJob(jobId, token) {
  const res = await fetch(`/v1/metadata-jobs/${jobId}/run`, {
    method: 'POST',
    headers: AUTH_HEADERS(token),
  })
  return parseResponse(res)
}

export async function generateDomainRuleSuggestions(sourceId, token) {
  const res = await fetch(`/v1/sources/${sourceId}/domain-rules/suggest`, {
    method: 'POST',
    headers: AUTH_HEADERS(token),
  })
  return parseResponse(res)
}

export async function getDomainRuleSuggestions(sourceId, token) {
  const res = await fetch(`/v1/sources/${sourceId}/domain-rules/suggestions`, {
    headers: AUTH_HEADERS(token),
  })
  return parseResponse(res)
}

export async function getDomainRules(sourceId, token) {
  const res = await fetch(`/v1/sources/${sourceId}/domain-rules`, {
    headers: AUTH_HEADERS(token),
  })
  return parseResponse(res)
}

export async function approveDomainRule(ruleId, token) {
  const res = await fetch(`/v1/domain-rules/${ruleId}/approve`, {
    method: 'POST',
    headers: AUTH_HEADERS(token),
  })
  return parseResponse(res)
}

export async function rejectDomainRule(ruleId, token) {
  const res = await fetch(`/v1/domain-rules/${ruleId}/reject`, {
    method: 'POST',
    headers: AUTH_HEADERS(token),
  })
  return parseResponse(res)
}

export async function generateEntityRuleSuggestions(sourceId, token) {
  const res = await fetch(`/v1/sources/${sourceId}/entity-rules/suggest`, {
    method: 'POST',
    headers: AUTH_HEADERS(token),
  })
  return parseResponse(res)
}

export async function getEntityRuleSuggestions(sourceId, token) {
  const res = await fetch(`/v1/sources/${sourceId}/entity-rules/suggestions`, {
    headers: AUTH_HEADERS(token),
  })
  return parseResponse(res)
}

export async function getEntityRules(sourceId, token) {
  const res = await fetch(`/v1/sources/${sourceId}/entity-rules`, {
    headers: AUTH_HEADERS(token),
  })
  return parseResponse(res)
}

export async function approveEntityRule(ruleId, token) {
  const res = await fetch(`/v1/entity-rules/${ruleId}/approve`, {
    method: 'POST',
    headers: AUTH_HEADERS(token),
  })
  return parseResponse(res)
}

export async function rejectEntityRule(ruleId, token) {
  const res = await fetch(`/v1/entity-rules/${ruleId}/reject`, {
    method: 'POST',
    headers: AUTH_HEADERS(token),
  })
  return parseResponse(res)
}

export async function analyzeDomainRefinements(sourceId, token) {
  const res = await fetch(`/v1/sources/${sourceId}/domain-refinements/analyze`, {
    method: 'POST',
    headers: AUTH_HEADERS(token),
  })
  return parseResponse(res)
}

export async function getDomainRefinements(sourceId, token) {
  const res = await fetch(`/v1/sources/${sourceId}/domain-refinements`, {
    headers: AUTH_HEADERS(token),
  })
  return parseResponse(res)
}

export async function approveDomainRefinement(id, token) {
  const res = await fetch(`/v1/domain-refinements/${id}/approve`, {
    method: 'POST',
    headers: AUTH_HEADERS(token),
  })
  return parseResponse(res)
}

export async function rejectDomainRefinement(id, token) {
  const res = await fetch(`/v1/domain-refinements/${id}/reject`, {
    method: 'POST',
    headers: AUTH_HEADERS(token),
  })
  return parseResponse(res)
}

export async function listDomainAssignments(sourceId, token) {
  const res = await fetch(`/v1/sources/${sourceId}/domains`, {
    headers: AUTH_HEADERS(token),
  })
  return parseResponse(res)
}

export async function listEntityAssignments(sourceId, token) {
  const res = await fetch(`/v1/sources/${sourceId}/entities`, {
    headers: AUTH_HEADERS(token),
  })
  return parseResponse(res)
}

export async function getProfileHistory(sourceId, token) {
  const res = await fetch(`/v1/sources/${sourceId}/profile/history`, {
    headers: AUTH_HEADERS(token),
  })
  return parseResponse(res)
}

export async function getProfileReviewTasks(sourceId, token, params = {}) {
  const qs = new URLSearchParams()
  if (params.limit  != null) qs.set('limit',  params.limit)
  if (params.offset != null) qs.set('offset', params.offset)
  const query = qs.toString()
  const res = await fetch(
    `/v1/sources/${sourceId}/profile/review-tasks${query ? '?' + query : ''}`,
    { headers: AUTH_HEADERS(token) },
  )
  return parseResponse(res)
}

export async function getColumnProfiles(sourceId, token, params = {}) {
  const qs = new URLSearchParams()
  if (params.table_fqn)        qs.set('table_fqn',     params.table_fqn)
  if (params.semantic_type)    qs.set('semantic_type',  params.semantic_type)
  if (params.pii_only)         qs.set('pii_only',       'true')
  if (params.limit  != null)   qs.set('limit',          params.limit)
  if (params.offset != null)   qs.set('offset',         params.offset)
  const query = qs.toString()
  const res = await fetch(
    `/v1/sources/${sourceId}/profile/columns${query ? '?' + query : ''}`,
    { headers: AUTH_HEADERS(token) },
  )
  return parseResponse(res)
}

export async function getTableProfileDetail(sourceId, tableFqn, token) {
  const res = await fetch(
    `/v1/sources/${sourceId}/profile/tables/${encodeURIComponent(tableFqn)}`,
    { headers: AUTH_HEADERS(token) },
  )
  return parseResponse(res)
}

export async function startBatchProfile(sourceId, token) {
  const res = await fetch(`/v1/sources/${sourceId}/profile/batch/start`, {
    method: 'POST',
    headers: AUTH_HEADERS(token),
  })
  return parseResponse(res)
}

export async function continueBatchProfile(sourceId, snapshotId, token) {
  const res = await fetch(
    `/v1/sources/${sourceId}/profile/batch/${snapshotId}/continue`,
    { method: 'POST', headers: AUTH_HEADERS(token) },
  )
  return parseResponse(res)
}

// ---------------------------------------------------------------------------
// Enterprise Metadata Search
// ---------------------------------------------------------------------------

export async function searchMetadata(q, token, params = {}) {
  const qs = new URLSearchParams({ q })
  if (params.limit             != null) qs.set('limit',             params.limit)
  if (params.offset            != null) qs.set('offset',            params.offset)
  if (params.source_id         != null) qs.set('source_id',         params.source_id)
  if (params.asset_type)                qs.set('asset_type',        params.asset_type)
  if (params.schema)                    qs.set('schema',            params.schema)
  if (params.domain)                    qs.set('domain',            params.domain)
  if (params.entity)                    qs.set('entity',            params.entity)
  if (params.semantic_type)             qs.set('semantic_type',     params.semantic_type)
  if (params.pii === true)              qs.set('pii',               'true')
  if (params.dictionary_status)         qs.set('dictionary_status', params.dictionary_status)
  if (params.classification)            qs.set('classification',    params.classification)
  if (params.profile_status)            qs.set('profile_status',    params.profile_status)
  const res = await fetch(`/v1/search?${qs}`, { headers: AUTH_HEADERS(token) })
  return parseResponse(res)
}

export async function getSearchFilters(token) {
  const res = await fetch('/v1/search/filters', { headers: AUTH_HEADERS(token) })
  return parseResponse(res)
}

export async function getSearchSuggestions(q, token, limit = 8) {
  const qs = new URLSearchParams({ q, limit })
  const res = await fetch(`/v1/search/suggestions?${qs}`, { headers: AUTH_HEADERS(token) })
  return parseResponse(res)
}
