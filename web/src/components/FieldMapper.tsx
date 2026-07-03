import { useState, useEffect } from 'react';
import { api } from '../api/client';
import type { MappingFieldDef, SourceFieldInfo, DestinationFieldInfo, TransformDef } from '../api/client';

const CREATE_NEW_SENTINEL = '__create_new__';

interface Props {
  fields: MappingFieldDef[];
  sourceFields: SourceFieldInfo[];
  destFields: DestinationFieldInfo[];
  sfObject: string;
  destCreds: Record<string, string>;
  onDestFieldsChange: (fields: DestinationFieldInfo[]) => void;
  onChange: (fields: MappingFieldDef[]) => void;
}

export function FieldMapper({
  fields, sourceFields, destFields, sfObject, destCreds, onDestFieldsChange, onChange,
}: Props) {
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
                  No field mappings yet. Use "Auto-suggest" or add rows manually.
                </td>
              </tr>
            )}
            {fields.map((f, idx) => (
              <FieldRow
                key={idx}
                field={f}
                sourceFields={sourceFields}
                destFields={destFields}
                sfObject={sfObject}
                destCreds={destCreds}
                onDestFieldsChange={onDestFieldsChange}
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
  sourceFields,
  destFields,
  sfObject,
  destCreds,
  onDestFieldsChange,
  transforms,
  onUpdate,
  onRemove,
}: {
  field: MappingFieldDef;
  sourceFields: SourceFieldInfo[];
  destFields: DestinationFieldInfo[];
  sfObject: string;
  destCreds: Record<string, string>;
  onDestFieldsChange: (fields: DestinationFieldInfo[]) => void;
  transforms: TransformDef[];
  onUpdate: (patch: Partial<MappingFieldDef>) => void;
  onRemove: () => void;
}) {
  const isMultiSource = Array.isArray(field.source_field);
  const sourceValue = isMultiSource
    ? (field.source_field as string[]).join(', ')
    : field.source_field ?? '';

  const [paramsText, setParamsText] = useState(
    field.transformation_params ? JSON.stringify(field.transformation_params) : '',
  );
  const [paramsError, setParamsError] = useState(false);

  const [creating, setCreating] = useState(false);
  const [newLabel, setNewLabel] = useState('');
  const [newApiName, setNewApiName] = useState('');
  const [newType, setNewType] = useState('Text');
  const [createBusy, setCreateBusy] = useState(false);
  const [createError, setCreateError] = useState('');

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

  const sourceNotInList = sourceValue && !isMultiSource && !sourceFields.some((sf) => sf.name === sourceValue);
  const destNotInList = field.destination_field && !destFields.some((df) => df.name === field.destination_field);

  const handleDestChange = (value: string) => {
    if (value === CREATE_NEW_SENTINEL) {
      setCreating(true);
      setNewLabel('');
      setNewApiName('');
      setNewType('Text');
      setCreateError('');
    } else {
      onUpdate({ destination_field: value });
    }
  };

  const handleCreateField = async () => {
    if (!newLabel.trim()) return;
    setCreateBusy(true);
    setCreateError('');
    const apiName = newApiName.trim() || newLabel.trim().replace(/\s+/g, '_');
    const finalApiName = apiName.endsWith('__c') ? apiName : apiName + '__c';
    try {
      const res = await api.customFields.create({
        destination_credentials: destCreds,
        salesforce_object: sfObject,
        field_label: newLabel.trim(),
        field_api_name: finalApiName,
        field_type: newType,
        length: newType === 'LongTextArea' ? 32768 : 255,
      });
      if (res.success) {
        const newField: DestinationFieldInfo = {
          name: res.field_api_name,
          label: newLabel.trim(),
          type: newType,
          length: newType === 'LongTextArea' ? 32768 : 255,
          required: false,
          createable: true,
          updateable: true,
          reference_to: [],
          picklist_values: [],
        };
        onDestFieldsChange([...destFields, newField]);
        onUpdate({ destination_field: res.field_api_name });
        setCreating(false);
      } else {
        setCreateError(res.message);
      }
    } catch (e: unknown) {
      setCreateError(e instanceof Error ? e.message : 'Failed to create field');
    } finally {
      setCreateBusy(false);
    }
  };

  return (
    <tr style={{ borderTop: '1px solid #f1f5f9' }}>
      <td style={td}>
        {isMultiSource ? (
          <input
            value={sourceValue}
            placeholder="table.field1, table.field2"
            onChange={(e) => {
              const v = e.target.value;
              onUpdate({ source_field: v.includes(',') ? v.split(',').map((s) => s.trim()) : v });
            }}
            style={inputStyle}
            title="Multi-field source (comma-separated)"
          />
        ) : (
          <select
            value={sourceValue}
            onChange={(e) => onUpdate({ source_field: e.target.value })}
            style={selectStyle}
          >
            <option value="">-- select source field --</option>
            {sourceNotInList && (
              <option value={sourceValue}>{sourceValue}</option>
            )}
            {sourceFields.map((sf) => (
              <option key={sf.name} value={sf.name}>
                {sf.label} ({sf.name.includes('.') ? sf.name.split('.').pop() : sf.name})
              </option>
            ))}
          </select>
        )}
      </td>
      <td style={td}>
        {creating ? (
          <div style={{ display: 'flex', flexDirection: 'column', gap: 6, minWidth: 220 }}>
            <input
              placeholder="Field Label"
              value={newLabel}
              onChange={(e) => {
                setNewLabel(e.target.value);
                setNewApiName(e.target.value.replace(/\s+/g, '_'));
              }}
              style={inputStyle}
              autoFocus
            />
            <input
              placeholder="API Name (auto)"
              value={newApiName}
              onChange={(e) => setNewApiName(e.target.value)}
              style={{ ...inputStyle, fontSize: 11, color: '#6b7280' }}
            />
            <select value={newType} onChange={(e) => setNewType(e.target.value)} style={selectStyle}>
              <option value="Text">Text</option>
              <option value="Number">Number</option>
              <option value="TextArea">TextArea</option>
              <option value="LongTextArea">Long Text Area</option>
              <option value="Checkbox">Checkbox</option>
              <option value="Date">Date</option>
              <option value="DateTime">DateTime</option>
              <option value="Url">URL</option>
              <option value="Email">Email</option>
              <option value="Phone">Phone</option>
            </select>
            {createError && (
              <div style={{ fontSize: 11, color: '#dc2626' }}>{createError}</div>
            )}
            <div style={{ display: 'flex', gap: 4 }}>
              <button
                type="button"
                disabled={createBusy || !newLabel.trim()}
                onClick={handleCreateField}
                style={{ ...createBtn, flex: 1 }}
              >
                {createBusy ? 'Creating...' : 'Create'}
              </button>
              <button
                type="button"
                onClick={() => setCreating(false)}
                style={{ ...cancelBtn, flex: 1 }}
              >
                Cancel
              </button>
            </div>
          </div>
        ) : (
          <select
            value={field.destination_field}
            onChange={(e) => handleDestChange(e.target.value)}
            style={selectStyle}
          >
            <option value="">-- select destination field --</option>
            {destNotInList && (
              <option value={field.destination_field}>{field.destination_field}</option>
            )}
            {destFields.filter((df) => df.createable).map((df) => (
              <option key={df.name} value={df.name}>
                {df.label} ({df.name}) [{df.type}]
              </option>
            ))}
            <option value={CREATE_NEW_SENTINEL}>+ Create custom field...</option>
          </select>
        )}
      </td>
      <td style={td}>
        <select
          value={field.transformation_function ?? ''}
          onChange={(e) => {
            const fn = e.target.value || undefined;
            const tdef = transforms.find((t) => t.name === fn);
            const defaultParams = tdef
              ? Object.fromEntries(Object.entries(tdef.params_schema).map(([k, v]) => [k, v.default]))
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
          style={selectStyle}
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
          style={{ ...inputStyle, borderColor: paramsError ? '#dc2626' : '#d1d5db' }}
        />
      </td>
      <td style={{ ...td, color: confColor, fontWeight: 600, whiteSpace: 'nowrap' }}>
        {conf === undefined ? '\u2014' : `${Math.round(conf * 100)}%`}
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
const selectStyle: React.CSSProperties = {
  width: '100%',
  minWidth: 120,
  padding: '6px 8px',
  border: '1px solid #d1d5db',
  borderRadius: 6,
  fontSize: 13,
  boxSizing: 'border-box',
  background: '#fff',
};
const inputStyle: React.CSSProperties = {
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
const createBtn: React.CSSProperties = {
  padding: '4px 8px',
  background: '#16a34a',
  color: '#fff',
  border: 'none',
  borderRadius: 4,
  cursor: 'pointer',
  fontSize: 11,
  fontWeight: 600,
};
const cancelBtn: React.CSSProperties = {
  padding: '4px 8px',
  background: '#fff',
  color: '#374151',
  border: '1px solid #d1d5db',
  borderRadius: 4,
  cursor: 'pointer',
  fontSize: 11,
};
