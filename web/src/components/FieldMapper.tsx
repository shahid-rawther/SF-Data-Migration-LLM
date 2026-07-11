import { useState, useEffect, useRef, useCallback } from 'react';
import { createPortal } from 'react-dom';
import { api } from '../api/client';
import type { MappingFieldDef, SourceFieldInfo, DestinationFieldInfo, TransformDef } from '../api/client';

const CREATE_NEW_SENTINEL = '__create_new__';

function SearchableSelect({
  value,
  options,
  placeholder = '-- select --',
  onChange,
}: {
  value: string;
  options: { value: string; label: string }[];
  placeholder?: string;
  onChange: (value: string) => void;
}) {
  const [open, setOpen] = useState(false);
  const [search, setSearch] = useState('');
  const triggerRef = useRef<HTMLDivElement>(null);
  const dropdownRef = useRef<HTMLDivElement>(null);
  const [pos, setPos] = useState<{ top: number; left: number; width: number }>({
    top: 0,
    left: 0,
    width: 200,
  });

  const updatePosition = useCallback(() => {
    if (!triggerRef.current) return;
    const rect = triggerRef.current.getBoundingClientRect();
    setPos({
      top: rect.bottom + 4,
      left: rect.left,
      width: Math.max(rect.width, 280),
    });
  }, []);

  useEffect(() => {
    if (!open) return;
    updatePosition();
    window.addEventListener('scroll', updatePosition, true);
    window.addEventListener('resize', updatePosition);
    return () => {
      window.removeEventListener('scroll', updatePosition, true);
      window.removeEventListener('resize', updatePosition);
    };
  }, [open, updatePosition]);

  useEffect(() => {
    const handler = (e: MouseEvent) => {
      const target = e.target as Node;
      if (
        triggerRef.current?.contains(target) ||
        dropdownRef.current?.contains(target)
      )
        return;
      setOpen(false);
    };
    document.addEventListener('mousedown', handler);
    return () => document.removeEventListener('mousedown', handler);
  }, []);

  const filtered = search
    ? options.filter((o) => o.label.toLowerCase().includes(search.toLowerCase()))
    : options;

  const selectedLabel = options.find((o) => o.value === value)?.label;

  const dropdown = open
    ? createPortal(
        <div
          ref={dropdownRef}
          style={{
            position: 'fixed',
            top: pos.top,
            left: pos.left,
            width: pos.width,
            zIndex: 9999,
            background: '#fff',
            border: '1px solid #e2e8f0',
            borderRadius: 10,
            boxShadow: '0 8px 30px rgba(0,0,0,0.16), 0 2px 8px rgba(0,0,0,0.08)',
            display: 'flex',
            flexDirection: 'column',
            overflow: 'hidden',
            animation: 'fadeInScale 0.15s ease-out',
          }}
        >
          <div style={{ padding: '8px 10px', borderBottom: '1px solid #f1f5f9', background: '#fafbfc' }}>
            <div style={{ position: 'relative' }}>
              <span
                style={{
                  position: 'absolute',
                  left: 10,
                  top: '50%',
                  transform: 'translateY(-50%)',
                  color: '#94a3b8',
                  fontSize: 13,
                  pointerEvents: 'none',
                }}
              >
                &#128269;
              </span>
              <input
                autoFocus
                value={search}
                onChange={(e) => setSearch(e.target.value)}
                placeholder="Type to search..."
                style={{
                  width: '100%',
                  padding: '8px 10px 8px 32px',
                  border: '1px solid #e2e8f0',
                  borderRadius: 6,
                  outline: 'none',
                  fontSize: 13,
                  background: '#fff',
                  boxSizing: 'border-box',
                  transition: 'border-color 0.15s',
                }}
                onFocus={(e) => (e.currentTarget.style.borderColor = '#93c5fd')}
                onBlur={(e) => (e.currentTarget.style.borderColor = '#e2e8f0')}
              />
            </div>
          </div>
          <div
            style={{
              overflowY: 'auto',
              maxHeight: 260,
              padding: '4px 0',
            }}
          >
            {filtered.length === 0 && (
              <div style={{ padding: '16px', color: '#94a3b8', fontSize: 13, textAlign: 'center' }}>
                No matching fields
              </div>
            )}
            {filtered.map((o) => {
              const isSelected = o.value === value;
              return (
                <div
                  key={o.value}
                  onClick={() => {
                    onChange(o.value);
                    setOpen(false);
                    setSearch('');
                  }}
                  style={{
                    padding: '7px 12px',
                    cursor: 'pointer',
                    fontSize: 13,
                    lineHeight: '1.4',
                    background: isSelected ? '#eff6ff' : '#fff',
                    fontWeight: isSelected ? 600 : 400,
                    color: isSelected ? '#2563eb' : '#1e293b',
                    borderLeft: isSelected ? '3px solid #2563eb' : '3px solid transparent',
                    transition: 'background 0.1s, border-color 0.1s',
                  }}
                  onMouseEnter={(e) => {
                    if (!isSelected) e.currentTarget.style.background = '#f8fafc';
                  }}
                  onMouseLeave={(e) => {
                    if (!isSelected) e.currentTarget.style.background = '#fff';
                  }}
                >
                  {o.label}
                </div>
              );
            })}
          </div>
          <div
            style={{
              padding: '6px 12px',
              borderTop: '1px solid #f1f5f9',
              fontSize: 11,
              color: '#94a3b8',
              background: '#fafbfc',
            }}
          >
            {filtered.length} of {options.length} fields
          </div>
        </div>,
        document.body,
      )
    : null;

  return (
    <div style={{ width: '100%', minWidth: 120 }}>
      <div
        ref={triggerRef}
        onClick={() => {
          if (!open) updatePosition();
          setOpen(!open);
          setSearch('');
        }}
        style={{
          ...selectStyle,
          cursor: 'pointer',
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'space-between',
          overflow: 'hidden',
          whiteSpace: 'nowrap',
          textOverflow: 'ellipsis',
          borderColor: open ? '#93c5fd' : '#d1d5db',
          boxShadow: open ? '0 0 0 2px rgba(59,130,246,0.15)' : 'none',
          transition: 'border-color 0.15s, box-shadow 0.15s',
        }}
        title={selectedLabel || placeholder}
      >
        <span
          style={{
            overflow: 'hidden',
            textOverflow: 'ellipsis',
            color: value ? '#1e293b' : '#94a3b8',
          }}
        >
          {selectedLabel || placeholder}
        </span>
        <span
          style={{
            fontSize: 10,
            marginLeft: 6,
            flexShrink: 0,
            color: '#94a3b8',
            transform: open ? 'rotate(180deg)' : 'rotate(0)',
            transition: 'transform 0.2s',
          }}
        >
          &#9662;
        </span>
      </div>
      {dropdown}
    </div>
  );
}

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
          <SearchableSelect
            value={sourceValue}
            placeholder="-- select source field --"
            onChange={(v) => onUpdate({ source_field: v })}
            options={[
              ...(sourceNotInList ? [{ value: sourceValue, label: sourceValue }] : []),
              ...sourceFields.map((sf) => ({
                value: sf.name,
                label: `${sf.label} (${sf.name.includes('.') ? sf.name.split('.').pop() : sf.name})`,
              })),
            ]}
          />
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
          <SearchableSelect
            value={field.destination_field}
            placeholder="-- select destination field --"
            onChange={handleDestChange}
            options={[
              ...(destNotInList
                ? [{ value: field.destination_field, label: field.destination_field }]
                : []),
              ...destFields
                .filter((df) => df.createable)
                .map((df) => ({
                  value: df.name,
                  label: `${df.label} (${df.name}) [${df.type}]`,
                })),
              { value: CREATE_NEW_SENTINEL, label: '+ Create custom field...' },
            ]}
          />
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
