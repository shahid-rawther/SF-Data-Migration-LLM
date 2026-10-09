import type { CredentialsSchema } from '../api/client';
import { Disclosure } from './Disclosure';
import { Input } from './ui/input';
import { Label } from './ui/label';

interface CredentialFormProps {
  schema: CredentialsSchema;
  values: Record<string, string>;
  onChange: (values: Record<string, string>) => void;
  disabled?: boolean;
}

export function CredentialForm({ schema, values, onChange, disabled }: CredentialFormProps) {
  const set = (key: string, val: string) => onChange({ ...values, [key]: val });
  const entries = Object.entries(schema.properties);
  const required = entries.filter(([key]) => schema.required.includes(key));
  const optional = entries.filter(([key]) => !schema.required.includes(key));

  const renderField = ([key, field]: (typeof entries)[number]) => (
    <div key={key} className="grid gap-1.5">
      <Label htmlFor={`credential-${key}`}>{field.title}</Label>
      <Input
        id={`credential-${key}`}
        type={field.format === 'password' ? 'password' : 'text'}
        value={values[key] ?? ''}
        onChange={(e) => set(key, e.target.value)}
        disabled={disabled}
        placeholder={field.description ?? field.title}
      />
    </div>
  );

  return (
    <div className="flex flex-col gap-3">
      <div className="grid gap-3 sm:grid-cols-2">{required.map(renderField)}</div>
      {optional.length > 0 && (
        <Disclosure
          label="Optional settings"
          hint={optional.map(([, f]) => f.title).join(', ')}
        >
          <div className="grid gap-3 sm:grid-cols-2">{optional.map(renderField)}</div>
        </Disclosure>
      )}
    </div>
  );
}
