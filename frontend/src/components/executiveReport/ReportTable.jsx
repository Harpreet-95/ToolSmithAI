import { fmtPlainValue } from './shared'

// Shared ReportTable renderer (ReportTable.id/title/columns/rows dict shape
// — core.reporting.report_models.ReportTable.to_dict()). Used by both the
// top-level report.tables list and any entity-section table (e.g. "Students
// by Program").
export default function ReportTable({ table, C, compact = false }) {
  if (!table) return null
  return (
    <div style={{ background: C.card, border: `1px solid ${C.border}`, borderRadius: '10px', padding: compact ? '12px 14px' : '14px 16px' }}>
      <div style={{ fontSize: '0.78rem', fontWeight: '700', color: C.text, marginBottom: '10px' }}>{table.title}</div>
      <div style={{ overflowX: 'auto' }}>
        <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: '0.72rem' }}>
          <thead>
            <tr>
              {table.columns.map(col => (
                <th key={col} style={{ textAlign: 'left', padding: '6px 10px', color: C.textMuted, borderBottom: `1px solid ${C.border}`, fontWeight: '600', whiteSpace: 'nowrap' }}>{col}</th>
              ))}
            </tr>
          </thead>
          <tbody>
            {table.rows.map((row, i) => (
              <tr key={i}>
                {table.columns.map(col => (
                  <td key={col} style={{ padding: '6px 10px', borderBottom: `1px solid ${C.borderAlt}`, color: C.textSec }}>
                    {typeof row[col] === 'string' && !/^-?\d+(\.\d+)?%?$/.test(row[col]) ? row[col] : fmtPlainValue(row[col])}
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  )
}
