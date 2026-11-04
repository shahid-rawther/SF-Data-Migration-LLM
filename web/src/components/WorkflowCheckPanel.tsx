import type { WorkflowReport } from '../api/client';

const STATUS_LABEL: Record<string, string> = {
  pass: 'Pass',
  fail: 'Failed',
  warning: 'Warning',
  skipped: 'Skipped',
};

export function WorkflowCheckPanel({ report }: { report: WorkflowReport }) {
  return (
    <div style={{ marginBottom: 20 }}>
      <div style={{
        padding: 14, borderRadius: 8, marginBottom: 16,
        background: '#fafafa', border: '1px solid #d4d4d4',
      }}>
        <div style={{ fontWeight: 700, fontSize: 16 }}>
          Salesforce workflow: {report.ok ? 'Ready' : 'Blocked'}
        </div>
        <div style={{ fontSize: 13, color: '#404040', marginTop: 4 }}>{report.summary}</div>
      </div>

      {report.objects.map((obj) => (
        <div key={obj.mapping} style={{ border: '1px solid #e5e5e5', borderRadius: 8, marginBottom: 10, overflow: 'hidden' }}>
          <div style={{ padding: '10px 14px', background: '#fafafa' }}>
            <strong>{obj.mapping}</strong>
            <span style={{ fontSize: 12, color: '#737373', marginLeft: 8 }}>{obj.salesforce_object}</span>
            <div style={{ fontSize: 12, color: '#404040', marginTop: 4 }}>{obj.summary}</div>
          </div>
          <div>
            {obj.stages.map((stage) => (
              <div key={stage.stage} style={{ padding: '8px 14px', borderTop: '1px solid #f0f0f0' }}>
                <div style={{ display: 'flex', justifyContent: 'space-between', gap: 12, alignItems: 'baseline' }}>
                  <span style={{ fontSize: 13, fontWeight: 600 }}>{stage.label}</span>
                  <span style={{ fontSize: 12, color: '#404040' }}>{STATUS_LABEL[stage.status] ?? stage.status}</span>
                </div>
                {stage.findings.map((finding, index) => (
                  <div key={`${finding.name}-${index}`} style={{ fontSize: 12, color: '#525252', marginTop: 4, lineHeight: 1.45 }}>
                    {finding.detail}
                  </div>
                ))}
              </div>
            ))}
          </div>
        </div>
      ))}
    </div>
  );
}
