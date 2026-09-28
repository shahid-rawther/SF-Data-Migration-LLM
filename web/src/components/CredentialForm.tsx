import type { CredentialsSchema } from '../api/client';
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

  return (
    <div className="flex flex-col gap-4">
      {Object.entries(schema.properties).map(([key, field]) => {
        const isPassword = field.format === 'password';
        return (
          <div key={key} className="grid gap-2">
            <Label htmlFor={`credential-${key}`}>
              {field.title}
              {schema.required.includes(key) && <span className="text-destructive"> *</span>}
            </Label>
            {field.description && (
              <p className="m-0 text-xs text-muted-foreground">{field.description}</p>
            )}
            <Input
              id={`credential-${key}`}
              type={isPassword ? 'password' : 'text'}
              value={values[key] ?? ''}
              onChange={(e) => set(key, e.target.value)}
              disabled={disabled}
              placeholder={field.description ?? field.title}
            />
          </div>
        );
      })}
    </div>
  );
}
