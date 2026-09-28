import { BrowserRouter, Routes, Route, Link, useLocation } from 'react-router-dom';
import { ArrowRightLeft, History as HistoryIcon, LayoutDashboard, Plus, Zap } from 'lucide-react';
import { Dashboard } from './pages/Dashboard';
import { NewMigrationWizard } from './pages/NewMigrationWizard';
import { MigrationDetail } from './pages/MigrationDetail';
import { History } from './pages/History';
import { MappingsList, MappingEditor } from './pages/MappingEditor';

/* ── Navigation items ──────────────────────────────────────────────────────── */

const NAV_ITEMS = [
  { to: '/', label: 'Dashboard', icon: LayoutDashboard },
  { to: '/new', label: 'New Migration', icon: Plus },
  { to: '/mappings', label: 'Mappings', icon: ArrowRightLeft, matchPrefix: '/mappings' },
  { to: '/history', label: 'History', icon: HistoryIcon },
];

/* ── Sidebar ───────────────────────────────────────────────────────────────── */

function Sidebar() {
  const { pathname } = useLocation();

  const isActive = (item: (typeof NAV_ITEMS)[number]) => {
    if (item.matchPrefix) return pathname.startsWith(item.matchPrefix);
    return pathname === item.to;
  };

  return (
    <aside className="fixed inset-y-0 left-0 z-50 flex w-[240px] flex-col border-r border-sidebar-border bg-sidebar text-sidebar-foreground max-[800px]:w-[76px]">
      <Link to="/" className="flex h-16 items-center gap-3 border-b border-sidebar-border px-5 font-semibold tracking-tight max-[800px]:justify-center max-[800px]:px-0">
        <span className="flex size-8 items-center justify-center rounded-lg bg-sidebar-primary text-sidebar-primary-foreground">
          <Zap className="size-4" />
        </span>
        <span className="max-[800px]:hidden">ITSM Migration</span>
      </Link>

      <nav className="flex flex-1 flex-col gap-1 p-3">
        {NAV_ITEMS.map((item) => {
          const active = isActive(item);
          const Icon = item.icon;
          return (
            <Link
              key={item.to}
              to={item.to}
              title={item.label}
              className={`flex h-10 items-center gap-3 rounded-md px-3 text-sm font-medium transition-colors max-[800px]:justify-center max-[800px]:px-0 ${
                active
                  ? 'bg-sidebar-accent text-sidebar-accent-foreground'
                  : 'text-muted-foreground hover:bg-sidebar-accent hover:text-sidebar-accent-foreground'
              }`}
            >
              <Icon className="size-4 shrink-0" />
              <span className="max-[800px]:hidden">{item.label}</span>
            </Link>
          );
        })}
      </nav>

      <div className="border-t border-sidebar-border p-4 text-xs text-muted-foreground max-[800px]:hidden">
        ServiceNow → Salesforce
      </div>
    </aside>
  );
}

/* ── App ───────────────────────────────────────────────────────────────────── */

function App() {
  return (
    <BrowserRouter>
      <div className="app-shell">
        <Sidebar />
        <main className="app-main">
          <Routes>
            <Route path="/" element={<Dashboard />} />
            <Route path="/new" element={<NewMigrationWizard />} />
            <Route path="/mappings" element={<MappingsList />} />
            <Route path="/mappings/new" element={<MappingEditor />} />
            <Route path="/mappings/edit/:name" element={<MappingEditor />} />
            <Route path="/migrations/:id" element={<MigrationDetail />} />
            <Route path="/history" element={<History />} />
          </Routes>
        </main>
      </div>
    </BrowserRouter>
  );
}

export default App;

