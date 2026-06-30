import { useState, useEffect } from 'react';
import { api } from '../api/client';
import type { MappingFieldDef, SourceFieldInfo, DestinationFieldInfo, TransformDef } from '../api/client';

interface Props {
  fields: MappingFieldDef[];
  sourceFields: SourceFieldInfo[];
  destFields: DestinationFieldInfo[];
  onChange: (fields: MappingFieldDef[]) => void;
}

export function FieldMapper({ fields, sourceFields, destFields, onChange }: Props) {
  const [transforms, setTransforms] = useState<TransformDef[]>([]);

  useEffect(() => {
    api.transforms.list().then(setTransforms).catch(() => {});
  }, []);
  const update = (idx: number, patch: Partial<MappingFieldDef>) => {
    const next = fields.map((f, i) => (i === idx ? { ...f, ...patch } : f));
    onChange(next);
  };

  const remove = (idx: number) => onChange(fields.filter((_, i) => i !== idx));

  const add = () =>
    onChange([...fields, { source_field: '', destination_field: '' }]);

  return (
    <div>
      <datalist id="source-fields">
        {sourceFields.map((sf) => (
          <option key={sf.name} value={sf.name}>
            {sf.label}
          </option>
        ))}
      </datalist>
      <datalist id="dest-fields">
        {destFields.map((df) => (
          <option key={df.name} value={df.name}>
            {df.label} ({df.type})
          </option>
        ))}
      </datalist>
      <div style={{ overflowX: 'auto', border: '1px solid #e5e7eb', borderRadius: 8 }}>
        <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 13 }}>
          <thead>
            <tr style={{ background: '#f8fafc', textAlign: 'left' }}>
              <th style={th}>Source field</th>
              <th style={th}>Destination field</th>
              <th style={th}>Transform</th>
              <th style={th}>Params (JSON)</th>
              <th style={th}>Conf.</th>
              <th style={th}></th>
            </tr>
          </thead>
          <tbody>
            {fields.length === 0 && (
              <tr>
                <td colSpan={6} style={{ ...td, textAlign: 'center', color: '#9ca3af' }}>
                  No field mappings yet. Use “Auto-suggest” or add rows manually.
                </td>
              </tr>
            )}
            {fields.map((f, idx) => (
              <FieldRow
                key={idx}
                field={f}
                transforms={transforms}
                onUpdate={(patch) => update(idx, patch)}
                onRemove={() => remove(idx)}
              />
            ))}
          </tbody>
        </table>
      </div>
      <button type="button" onClick={add} style={addBtn}>
        + Add field mapping
      </button>
    </div>
  );
}

function FieldRow({
  field,
  transforms,
  onUpdate,
  onRemove,
}: {
  field: MappingFieldDef;
  transforms: TransformDef[];
  onUpdate: (patch: Partial<MappingFieldDef>) => void;
  onRemove: () => void;
}) {
  const sourceValue = Array.isArray(field.source_field)
    ? field.source_field.join(', ')
    : field.source_field ?? '';

  const [paramsText, setParamsText] = useState(
    field.transformation_params ? JSON.stringify(field.transformation_params) : '',
  );
  const [paramsError, setParamsError] = useState(false);

  const commitParams = (text: string) => {
    setParamsText(text);
    if (!text.trim()) {
      setParamsError(false);
      onUpdate({ transformation_params: undefined });
      return;
    }
    try {
      const parsed = JSON.parse(text);
      setParamsError(false);
      onUpdate({ transformation_params: parsed });
    } catch {
      setParamsError(true);
    }
  };

  const conf = field.confidence;
  const confColor =
    conf === undefined ? '#9ca3af' : conf >= 0.9 ? '#16a34a' : conf >= 0.7 ? '#d97706' : '#dc2626';

  return (
    <tr style={{ borderTop: '1px solid #f1f5f9' }}>
      <td style={td}>
        <input
          list="source-fields"
          value={sourceValue}
          placeholder="table.field"
          onChange={(e) => {
            const v = e.target.value;
            onUpdate({ source_field: v.includes(',') ? v.split(',').map((s) => s.trim()) : v });
          }}
          style={input}
        />
      </td>
      <td style={td}>
        <input
          list="dest-fields"
          value={field.destination_field}
          placeholder="Salesforce field"
          onChange={(e) => onUpdate({ destination_field: e.target.value })}
          style={input}
        />
      </td>
      <td style={td}>
        <select
          value={field.transformation_function ?? ''}
          onChange={(e) => {
            const fn = e.target.value || undefined;
            const td = transforms.find((t) => t.name === fn);
            const defaultParams = td
              ? Object.fromEntries(Object.entries(td.params_schema).map(([k, v]) => [k, v.default]))
              : undefined;
            onUpdate({ transformation_function: fn, transformation_params: fn ? defaultParams : undefined });
            if (fn && defaultParams) {
              setParamsText(JSON.stringify(defaultParams));
              setParamsError(false);
            } else {
              setParamsText('');
              setParamsError(false);
            }
          }}
          style={input}
          title={transforms.find((t) => t.name === field.transformation_function)?.description ?? ''}
        >
          <option value="">(none)</option>
          {transforms.map((t) => (
            <option key={t.name} value={t.name}>
              {t.label}
            </option>
          ))}
          {field.transformation_function &&
            !transforms.some((t) => t.name === field.transformation_function) && (
              <option value={field.transformation_function}>{field.transformation_function}</option>
            )}
        </select>
      </td>
      <td style={td}>
        <input
          value={paramsText}
          placeholder='{"default":""}'
          onChange={(e) => commitParams(e.target.value)}
          style={{ ...input, borderColor: paramsError ? '#dc2626' : '#d1d5db' }}
        />
      </td>
      <td style={{ ...td, color: confColor, fontWeight: 600, whiteSpace: 'nowrap' }}>
        {conf === undefined ? '—' : `${Math.round(conf * 100)}%`}
      </td>
      <td style={td}>
        <button type="button" onClick={onRemove} style={removeBtn} title="Remove">
          ✕
        </button>
      </td>
    </tr>
  );
}

const th: React.CSSProperties = { padding: '8px 10px', fontWeight: 600, color: '#475569', fontSize: 12 };
const td: React.CSSProperties = { padding: '6px 10px', verticalAlign: 'middle' };
const input: React.CSSProperties = {
  width: '100%',
  minWidth: 120,
  padding: '6px 8px',
  border: '1px solid #d1d5db',
  borderRadius: 6,
  fontSize: 13,
  boxSizing: 'border-box',
};
const addBtn: React.CSSProperties = {
  marginTop: 10,
  padding: '8px 14px',
  background: '#eff6ff',
  color: '#2563eb',
  border: '1px solid #bfdbfe',
  borderRadius: 8,
  cursor: 'pointer',
  fontSize: 13,
  fontWeight: 600,
};
const removeBtn: React.CSSProperties = {
  background: '#fef2f2',
  color: '#dc2626',
  border: '1px solid #fecaca',
  borderRadius: 6,
  cursor: 'pointer',
  padding: '4px 8px',
  fontSize: 12,
};
