import type { CredentialsSchema } from '../api/client';

interface CredentialFormProps {
  schema: CredentialsSchema;
  values: Record<string, string>;
  onChange: (values: Record<string, string>) => void;
  disabled?: boolean;
}

export function CredentialForm({ schema, values, onChange, disabled }: CredentialFormProps) {
  const set = (key: string, val: string) => onChange({ ...values, [key]: val });

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 14 }}>
      {Object.entries(schema.properties).map(([key, field]) => {
        const isPassword = field.format === 'password';
        return (
          <div key={key}>
            <label style={{ fontSize: 13, fontWeight: 500, display: 'block', marginBottom: 4 }}>
              {field.title}
              {schema.required.includes(key) && <span style={{ color: '#ef4444' }}> *</span>}
            </label>
            {field.description && (
              <p style={{ fontSize: 12, color: '#6b7280', margin: '0 0 4px' }}>{field.description}</p>
            )}
            <input
              type={isPassword ? 'password' : 'text'}
              value={values[key] ?? ''}
              onChange={(e) => set(key, e.target.value)}
              disabled={disabled}
              placeholder={field.description ?? field.title}
              style={{
                width: '100%',
                padding: '8px 10px',
                border: '1px solid #d1d5db',
                borderRadius: 6,
                fontSize: 14,
                boxSizing: 'border-box',
              }}
            />
          </div>
        );
      })}
    </div>
  );
}
