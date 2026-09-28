import { Badge } from './ui/badge';

interface StatusBadgeProps {
  status: string;
}

const VARIANTS = {
  pending: 'warning',
  running: 'info',
  paused: 'warning',
  stopped: 'destructive',
  completed: 'success',
  completed_with_errors: 'warning',
  failed: 'destructive',
} as const;

// The badge itself is monochrome, so the dot carries the distinction: filled
// and animated while work is happening, inverted on the dark finished states.
const DOTS: Record<string, string> = {
  pending: 'bg-muted-foreground/60',
  running: 'bg-foreground animate-pulse',
  paused: 'bg-muted-foreground',
  stopped: 'bg-white/80',
  completed: 'bg-background',
  completed_with_errors: 'bg-foreground',
  failed: 'bg-white/80',
};

export function StatusBadge({ status }: StatusBadgeProps) {
  const variant = VARIANTS[status as keyof typeof VARIANTS] ?? 'secondary';
  return (
    <Badge variant={variant} className="gap-1.5 capitalize">
      <span className={`size-1.5 rounded-full ${DOTS[status] ?? 'bg-muted-foreground'}`} />
      {status}
    </Badge>
  );
}
