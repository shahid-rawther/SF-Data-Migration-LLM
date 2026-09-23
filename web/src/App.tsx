import { BrowserRouter, Routes, Route, Link, useLocation } from 'react-router-dom';
import { Dashboard } from './pages/Dashboard';
import { NewMigrationWizard } from './pages/NewMigrationWizard';
import { MigrationDetail } from './pages/MigrationDetail';
import { History } from './pages/History';
import { MappingsList, MappingEditor } from './pages/MappingEditor';

/* ── Navigation items ──────────────────────────────────────────────────────── */

const NAV_ITEMS: { to: string; label: string; icon: string; matchPrefix?: string }[] = [
  { to: '/',         label: 'Dashboard',     icon: '📊' },
  { to: '/new',      label: 'New Migration', icon: '🚀' },
  { to: '/mappings', label: 'Mappings',      icon: '🔗', matchPrefix: '/mappings' },
  { to: '/history',  label: 'History',        icon: '📜' },
];

/* ── Sidebar ───────────────────────────────────────────────────────────────── */

function Sidebar() {
  const { pathname } = useLocation();

  const isActive = (item: (typeof NAV_ITEMS)[number]) => {
    if (item.matchPrefix) return pathname.startsWith(item.matchPrefix);
    return pathname === item.to;
  };

  return (
    <aside style={sidebarStyle}>
      {/* Brand */}
      <Link to="/" style={brandStyle}>
        <span style={{ fontSize: 20 }}>⚡</span>
        <span>ITSM Migration</span>
      </Link>

      {/* Divider */}
      <div style={{ height: 1, background: '#334155', margin: '4px 12px 12px' }} />

      {/* Links */}
      <nav style={{ display: 'flex', flexDirection: 'column', gap: 2, flex: 1 }}>
        {NAV_ITEMS.map((item) => {
          const active = isActive(item);
          return (
            <Link key={item.to} to={item.to} style={navItemStyle(active)}>
              <span style={{ fontSize: 16, width: 22, textAlign: 'center' }}>{item.icon}</span>
              <span>{item.label}</span>
            </Link>
          );
        })}
      </nav>

      {/* Footer */}
      <div style={sidebarFooter}>
        <div style={{ fontSize: 11, color: '#64748b' }}>ServiceNow → Salesforce</div>
      </div>
    </aside>
  );
}

/* ── Styles ────────────────────────────────────────────────────────────────── */

const SIDEBAR_WIDTH = 220;

const sidebarStyle: React.CSSProperties = {
  position: 'fixed',
  top: 0,
  left: 0,
  bottom: 0,
  width: SIDEBAR_WIDTH,
  background: '#1e293b',
  display: 'flex',
  flexDirection: 'column',
  zIndex: 50,
  overflowY: 'auto',
  boxShadow: '2px 0 8px rgba(0,0,0,0.12)',
};

const brandStyle: React.CSSProperties = {
  display: 'flex',
  alignItems: 'center',
  gap: 10,
  padding: '20px 16px 12px',
  color: '#fff',
  textDecoration: 'none',
  fontWeight: 700,
  fontSize: 15,
  letterSpacing: '-0.01em',
};

const navItemStyle = (active: boolean): React.CSSProperties => ({
  display: 'flex',
  alignItems: 'center',
  gap: 10,
  padding: '10px 16px',
  margin: '0 8px',
  borderRadius: 8,
  color: active ? '#fff' : '#94a3b8',
  background: active ? '#334155' : 'transparent',
  textDecoration: 'none',
  fontSize: 13.5,
  fontWeight: active ? 600 : 400,
  transition: 'background 0.15s, color 0.15s',
});

const sidebarFooter: React.CSSProperties = {
  padding: '12px 16px',
  borderTop: '1px solid #334155',
  marginTop: 'auto',
};

const mainContent: React.CSSProperties = {
  marginLeft: SIDEBAR_WIDTH,
  minHeight: '100vh',
  background: '#f8fafc',
  flex: 1,
  width: `calc(100% - ${SIDEBAR_WIDTH}px)`,
};

/* ── App ───────────────────────────────────────────────────────────────────── */

function App() {
  return (
    <BrowserRouter>
      <div style={{ display: 'flex', minHeight: '100vh' }}>
        <Sidebar />
        <div style={mainContent}>
          <Routes>
            <Route path="/" element={<Dashboard />} />
            <Route path="/new" element={<NewMigrationWizard />} />
            <Route path="/mappings" element={<MappingsList />} />
            <Route path="/mappings/new" element={<MappingEditor />} />
            <Route path="/mappings/edit/:name" element={<MappingEditor />} />
            <Route path="/migrations/:id" element={<MigrationDetail />} />
            <Route path="/history" element={<History />} />
          </Routes>
        </div>
      </div>
    </BrowserRouter>
  );
}

export default App;

