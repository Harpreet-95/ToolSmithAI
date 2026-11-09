// Resolves how a saved report (data.report_service.get_report_by_id's
// `content` payload) should be rendered in the Reports detail view.
//
// Two report shapes are persisted under the same `reports` table:
//   - Executive Reports (task_type "generate_executive_report"): content is
//     `{ executive_report: <ExecutiveReportData.to_dict()> }` — rendered via
//     ExecutiveReportContent, the same component AI Workspace already uses.
//   - Legacy/dataset reports: content is `{ sections: [...], report_plan?: {...} }`
//     — rendered via ReportWorkspace.
//
// Shape is read directly off the persisted content, never guessed from the
// report title, so it stays correct regardless of how the title was worded.
export function resolveReportContentView(content) {
  if (content?.executive_report) {
    return { kind: 'executive', report: content.executive_report }
  }
  if (content?.sections?.length > 0) {
    return { kind: 'legacy', sections: content.sections, reportPlan: content.report_plan ?? null }
  }
  return { kind: 'empty' }
}
