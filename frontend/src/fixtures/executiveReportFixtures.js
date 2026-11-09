// TEST / DEMO DATA ONLY (Milestone 7 Parts 17/18). These fixtures shape data
// exactly like core.analysis.models.ExecutiveAnalysisReport.
// to_executive_report_data_dict() so ExecutiveReportContent is exercised
// against the real payload contract — but they are never imported by
// production code paths (App.jsx/AIWorkspace.jsx always fetch a live report
// from the API). They exist purely for frontend tests and manual visual
// review of the dashboard.

// The the reference database fixture — representative of the currently-verified live backend
// output (Students=12,000, Programs=40, Amount=$2,000,000.00, Users=41,000,
// Users trend 170 -> 10, -94%). Illustrative figures only.
export function referenceExecutiveReportFixture() {
  return {
    version: 3,
    title: 'Business Analysis — the reference database',
    source_id: 1,
    subtitle: 'Give me a full executive overview of the business',
    generated_at: '2026-08-24T14:32:00Z',
    report_period_label: 'Latest complete analyzed period: August 2026 (from \'Enrollment\').',
    executive_summary: 'the reference database served 12,000 students across 40 programs, with total collected payments of $2,000,000.00. Active user engagement declined sharply this month.',
    business_overview: 'This database covers 4 business areas: Enrollment, Payments, User Engagement, and Programs. Data is active and current.',
    source_coverage: {
      discovered_business_areas: ['Enrollment', 'Payments', 'User Engagement', 'Programs'],
      usable_business_areas: ['Enrollment', 'Payments', 'User Engagement', 'Programs'],
      historical_only_business_areas: [],
      active_business_areas: ['Enrollment', 'Payments', 'User Engagement'],
      unavailable_business_areas: [],
      observed_data_start: '2019-01-04',
      observed_data_end: '2026-08-20',
    },
    kpi_cards: [
      { id: 'task-students', title: 'Total Students', value: 12000, unit: null, previous_value: null, absolute_change: null, percent_change: null, direction: null, comparison_label: null, warning: null },
      { id: 'task-programs', title: 'Total Programs', value: 40, unit: null, previous_value: null, absolute_change: null, percent_change: null, direction: null, comparison_label: null, warning: null },
      { id: 'task-amount', title: 'Total Amount', value: 2000000.00, unit: null, previous_value: null, absolute_change: null, percent_change: null, direction: null, comparison_label: null, warning: null },
      { id: 'task-users', title: 'Active Users', value: 10, unit: null, previous_value: 170, absolute_change: -160, percent_change: -94.1, direction: 'down', comparison_label: 'vs last month', warning: null },
    ],
    charts: [],
    tables: [],
    narrative: null,
    warnings: ['3 columns had partial null coverage.'],
    trace: [], timings_ms: {},
    data_quality_notes: ['3 columns had partial null coverage.', 'Historical records before 2019 are incomplete.'],
    data_quality_overview: 'Of 4 discovered business areas, 4 had usable analytical evidence and 0 could not be analyzed. All areas reflect active, current data. 2 minor data-quality notes were recorded during analysis.',
    technical_diagnostics: [],
    business_areas: [
      {
        section_id: 'area-payments', title: 'Payments', subtitle: '', domain: 'Payments', status: 'SUPPORTED',
        kpis: [{ id: 'task-amount', title: 'Total Amount', value: 2000000.00, formatted_value: null, unit: null, comparison: null, trend_direction: null, evidence_task_ids: ['task-amount'], confidence: 0.95, limitation: null }],
        charts: [], tables: [], findings: [], insights: [], recommendations: [], limitations: [], evidence_refs: [],
      },
      {
        section_id: 'area-user-engagement', title: 'User Engagement', subtitle: '', domain: 'User Engagement', status: 'SUPPORTED',
        kpis: [{ id: 'task-users', title: 'Active Users', value: 10, formatted_value: null, unit: null, comparison: { previous_value: 170, absolute_change: -160, percent_change: -94.1, direction: 'down', comparison_label: 'vs last month' }, trend_direction: 'down', evidence_task_ids: ['task-users'], confidence: 0.9, limitation: null }],
        charts: [], tables: [], findings: [], insights: [], recommendations: [], limitations: [], evidence_refs: [],
      },
    ],
    // Realistic backend shape: core.analysis.section_builder gives this
    // section the underlying DOMAIN's own name/entities as title/subtitle
    // (never "Students & Programs" itself) — the dashboard derives that
    // category label from which typed report field this arrived in (see
    // utils/executiveReport.js CATEGORY_LABELS), not from this text.
    student_program_overview: {
      section_id: 'area-enrollment', title: 'Enrollment', subtitle: 'Student, Program', domain: 'Enrollment', status: 'SUPPORTED',
      kpis: [
        { id: 'task-students', title: 'Total Students', value: 12000, formatted_value: null, unit: null, comparison: null, trend_direction: null, evidence_task_ids: ['task-students'], confidence: 0.95, limitation: null },
        { id: 'task-programs', title: 'Total Programs', value: 40, formatted_value: null, unit: null, comparison: null, trend_direction: null, evidence_task_ids: ['task-programs'], confidence: 0.9, limitation: null },
      ],
      charts: [
        { id: 'enrollment-trend', title: 'Enrollment Trend', chart_type: 'line', x_label: 'Period', y_label: 'Students', labels: ['Previous period', 'This month'], series: [{ name: 'Students', data: [10420, 12000] }], evidence_task_ids: ['task-enrollment-trend'], limitation: null },
      ],
      tables: [
        {
          id: 'students-by-program', title: 'Students by Certificate Name', columns: ['label', 'value'],
          rows: [
            { label: 'Data Analytics', value: 2200 }, { label: 'Cloud Computing', value: 1900 },
            { label: 'Project Management', value: 1400 }, { label: 'Cybersecurity', value: 1000 },
          ],
        },
      ],
      findings: [], insights: [], recommendations: [],
      limitations: ['Students by Certificate Name shows the top 25 of this view; 42 additional group(s) were truncated.'],
      evidence_refs: [],
    },
    financial_performance: {
      section_id: 'area-payments', title: 'Payments', subtitle: 'Payment', domain: 'Payments', status: 'SUPPORTED',
      kpis: [], charts: [],
      tables: [
        { id: 'amount-by-program', title: 'Average Course Fee by Certificate Name', columns: ['label', 'value'], rows: [{ label: 'Data Analytics', value: 1450.0 }, { label: 'Cloud Computing', value: 1275.5 }] },
      ],
      findings: [], insights: [], recommendations: [], limitations: [], evidence_refs: [],
    },
    operations_engagement: null,
    key_changes: ['Active Users decreased from 170 to 10 (-94%) this month vs last month.'],
    positive_developments: [],
    areas_requiring_attention: [
      'Reliable Certificate Renewal-level analysis is not available from the current analytical model.',
    ],
    insights: [
      {
        finding_id: 'f-users-decline', title: 'Active User Engagement Declined Sharply', statement: 'Active Users decreased from 170 to 10 (-94%) this month vs last month, a materially large swing.',
        evidence_task_ids: ['task-users'], evidence_summary: '', importance: 'HIGH', confidence: 0.9,
        limitation: null, domain: 'User Engagement', business_impact: 'ATTENTION_REQUIRED', is_plain_kpi: false,
      },
    ],
    recommendations: [
      {
        recommendation_id: 'r-engagement', recommendation: 'Investigate the drop in active user engagement this month before it affects enrollment follow-through.',
        priority: 'HIGH', based_on_finding_ids: ['f-users-decline'], rationale: 'Active Users fell 94% month-over-month, the largest verified change in this analysis.',
        evidence_summary: '', recommended_next_analysis: 'Break down Active Users by acquisition channel for the same period.',
        confidence: 0.85, limitation: null,
      },
    ],
    suggested_follow_up_questions: ['What is driving the decline in active users this month?', 'Which programs had the strongest enrollment growth?'],
    evidence_provenance: [],
  }
}

// Synthetic fixture for a DIFFERENT business (Milestone 7 Part 17) — proves
// the dashboard renders generically, with no Student/Program/the reference database anywhere
// in this file.
export function genericExecutiveReportFixture() {
  return {
    version: 3,
    title: 'Business Analysis — Northwind Retail',
    source_id: 42,
    subtitle: 'Give me a full executive overview of the business',
    generated_at: '2026-08-20T09:05:00Z',
    report_period_label: 'Latest complete analyzed period: July 2026 (from \'Orders\').',
    executive_summary: 'Northwind Retail served 4,210 customers across 86 products, with total order revenue of $918,204.12.',
    business_overview: 'This database covers 3 business areas: Customers, Products, and Orders.',
    source_coverage: {
      discovered_business_areas: ['Customers', 'Products', 'Orders'],
      usable_business_areas: ['Customers', 'Products', 'Orders'],
      historical_only_business_areas: [],
      active_business_areas: ['Customers', 'Products', 'Orders'],
      unavailable_business_areas: [],
      observed_data_start: '2021-03-01',
      observed_data_end: '2026-07-31',
    },
    kpi_cards: [
      { id: 'task-customers', title: 'Total Customers', value: 4210, unit: null, previous_value: null, absolute_change: null, percent_change: null, direction: null, comparison_label: null, warning: null },
      { id: 'task-products', title: 'Total Products', value: 86, unit: null, previous_value: null, absolute_change: null, percent_change: null, direction: null, comparison_label: null, warning: null },
      { id: 'task-revenue', title: 'Total Order Revenue', value: 918204.12, unit: null, previous_value: 812340.5, absolute_change: 105863.62, percent_change: 13.0, direction: 'up', comparison_label: 'vs previous quarter', warning: null },
    ],
    charts: [],
    tables: [],
    narrative: null,
    warnings: [],
    trace: [], timings_ms: {},
    data_quality_notes: [],
    data_quality_overview: 'Of 3 discovered business areas, 3 had usable analytical evidence and 0 could not be analyzed.',
    technical_diagnostics: [],
    business_areas: [
      {
        section_id: 'area-customers', title: 'Customers', subtitle: 'Customer, Product', domain: 'Customers', status: 'SUPPORTED',
        kpis: [
          { id: 'task-customers', title: 'Total Customers', value: 4210, formatted_value: null, unit: null, comparison: null, trend_direction: null, evidence_task_ids: ['task-customers'], confidence: 0.92, limitation: null },
          { id: 'task-products', title: 'Total Products', value: 86, formatted_value: null, unit: null, comparison: null, trend_direction: null, evidence_task_ids: ['task-products'], confidence: 0.9, limitation: null },
        ],
        charts: [],
        tables: [
          { id: 'customers-by-region', title: 'Customers by Region', columns: ['label', 'value'], rows: [{ label: 'West', value: 1620 }, { label: 'East', value: 1340 }, { label: 'Central', value: 1250 }] },
        ],
        findings: [], insights: [], recommendations: [], limitations: [], evidence_refs: [],
      },
      {
        section_id: 'area-orders', title: 'Orders', subtitle: '', domain: 'Orders', status: 'SUPPORTED',
        kpis: [{ id: 'task-revenue', title: 'Total Order Revenue', value: 918204.12, formatted_value: null, unit: null, comparison: { previous_value: 812340.5, absolute_change: 105863.62, percent_change: 13.0, direction: 'up', comparison_label: 'vs previous quarter' }, trend_direction: 'up', evidence_task_ids: ['task-revenue'], confidence: 0.9, limitation: null }],
        charts: [{ id: 'revenue-trend', title: 'Revenue Trend', chart_type: 'line', x_label: 'Period', y_label: 'Revenue', labels: ['Previous quarter', 'This quarter'], series: [{ name: 'Revenue', data: [812340.5, 918204.12] }], evidence_task_ids: ['task-revenue-trend'], limitation: null }],
        tables: [], findings: [], insights: [], recommendations: [], limitations: [], evidence_refs: [],
      },
    ],
    student_program_overview: null,
    financial_performance: null,
    operations_engagement: null,
    key_changes: ['Total Order Revenue increased from $812,340.50 to $918,204.12 (+13%) vs previous quarter.'],
    positive_developments: ['Total Order Revenue increased from $812,340.50 to $918,204.12 (+13%) vs previous quarter.'],
    areas_requiring_attention: [],
    insights: [
      {
        finding_id: 'f-revenue-growth', title: 'Order Revenue Grew This Quarter', statement: 'Total Order Revenue increased 13% quarter-over-quarter, from $812,340.50 to $918,204.12.',
        evidence_task_ids: ['task-revenue'], evidence_summary: '', importance: 'MEDIUM', confidence: 0.88,
        limitation: null, domain: 'Orders', business_impact: 'POSITIVE_DEVELOPMENT', is_plain_kpi: false,
      },
    ],
    recommendations: [
      {
        recommendation_id: 'r-inventory', recommendation: 'Review inventory levels for top-performing product categories ahead of next quarter.',
        priority: 'MEDIUM', based_on_finding_ids: ['f-revenue-growth'], rationale: 'Revenue growth of 13% may strain current stocking levels for the fastest-moving categories.',
        evidence_summary: '', recommended_next_analysis: 'Break down revenue growth by product category.',
        confidence: 0.7, limitation: null,
      },
    ],
    suggested_follow_up_questions: ['Which product categories grew fastest this quarter?'],
    evidence_provenance: [],
  }
}
