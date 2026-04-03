interface ProgressBarProps {
  percent: number;
  label?: string;
  color?: string;
  height?: number;
}

export function ProgressBar({ percent, label, color = '#2563eb', height = 12 }: ProgressBarProps) {
  const clamped = Math.min(100, Math.max(0, percent));
  return (
    <div style={{ marginBottom: 8 }}>
      {label && (
        <div style={{ display: 'flex', justifyContent: 'space-between', marginBottom: 4, fontSize: 13 }}>
          <span>{label}</span>
          <span>{clamped.toFixed(1)}%</span>
        </div>
      )}
      <div
        style={{
          background: '#e5e7eb',
          borderRadius: height,
          height,
          overflow: 'hidden',
        }}
      >
        <div
          style={{
            width: `${clamped}%`,
            height: '100%',
            background: color,
            borderRadius: height,
            transition: 'width 0.4s ease',
          }}
        />
      </div>
    </div>
  );
}
