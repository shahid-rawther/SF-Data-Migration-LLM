import { Progress } from './ui/progress';

interface ProgressBarProps {
  percent: number;
  label?: string;
  color?: string;
  height?: number;
}

export function ProgressBar({ percent, label, color = '#171717', height = 12 }: ProgressBarProps) {
  const clamped = Math.min(100, Math.max(0, percent));
  return (
    <div className="mb-2">
      {label && (
        <div className="mb-1.5 flex justify-between text-xs">
          <span className="font-medium text-foreground">{label}</span>
          <span className="tabular-nums text-muted-foreground">{clamped.toFixed(1)}%</span>
        </div>
      )}
      <Progress
        value={clamped}
        className="bg-muted"
        style={{ height }}
        indicatorStyle={{ backgroundColor: color }}
      />
    </div>
  );
}
