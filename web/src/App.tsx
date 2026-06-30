import { BrowserRouter, Routes, Route, Link } from 'react-router-dom';
import { Dashboard } from './pages/Dashboard';
import { NewMigrationWizard } from './pages/NewMigrationWizard';
import { MigrationDetail } from './pages/MigrationDetail';
import { History } from './pages/History';
import { MappingsList, MappingEditor } from './pages/MappingEditor';

function NavBar() {
  return (
    <nav
      style={{
        background: '#1e293b',
        padding: '0 24px',
        display: 'flex',
        alignItems: 'center',
        height: 52,
        gap: 24,
        boxShadow: '0 1px 4px rgba(0,0,0,0.15)',
      }}
    >
      <Link
        to="/"
        style={{ color: '#fff', textDecoration: 'none', fontWeight: 700, fontSize: 16 }}
      >
        ⚡ ITSM Migration
      </Link>
      <Link to="/" style={navLink}>Dashboard</Link>
      <Link to="/new" style={navLink}>New Migration</Link>
      <Link to="/mappings" style={navLink}>Mappings</Link>
      <Link to="/history" style={navLink}>History</Link>
    </nav>
  );
}

const navLink: React.CSSProperties = {
  color: '#94a3b8',
  textDecoration: 'none',
  fontSize: 14,
};

function App() {
  return (
    <BrowserRouter>
      <div style={{ minHeight: '100vh', background: '#f8fafc' }}>
        <NavBar />
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
    </BrowserRouter>
  );
}

export default App;

