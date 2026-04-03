interface StatusBadgeProps {
  status: string;
}

const COLORS: Record<string, { bg: string; color: string }> = {
  pending: { bg: '#fef3c7', color: '#92400e' },
  running: { bg: '#dbeafe', color: '#1e40af' },
  paused: { bg: '#fde8d8', color: '#9a3412' },
  stopped: { bg: '#fee2e2', color: '#991b1b' },
  completed: { bg: '#dcfce7', color: '#166534' },
  failed: { bg: '#fee2e2', color: '#991b1b' },
};

export function StatusBadge({ status }: StatusBadgeProps) {
  const style = COLORS[status] ?? { bg: '#f3f4f6', color: '#374151' };
  return (
    <span
      style={{
        padding: '2px 10px',
        borderRadius: 20,
        fontSize: 12,
        fontWeight: 600,
        background: style.bg,
        color: style.color,
        textTransform: 'capitalize',
      }}
    >
      {status}
    </span>
  );
}
