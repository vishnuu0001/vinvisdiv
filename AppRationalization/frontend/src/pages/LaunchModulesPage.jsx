// ---------------------------------------------------------------------------
// Author: Vishnuu A
// Scope: AppRationalization — frontend/src/pages (LaunchModulesPage.jsx)
// Date: 2026-09-04
// ---------------------------------------------------------------------------
import React, { useCallback, useEffect, useMemo, useState } from 'react';
import { useLocation, useNavigate } from 'react-router-dom';
import {
  AppWindow, BarChart3, Bot, Car, ChevronDown, CircleHelp, Code2,
  FlaskConical, Gauge, GitBranch, Grid2X2, Home, LayoutGrid,
  LayoutPanelTop, List, LogOut, Maximize2, Network, PanelLeftClose,
  PanelLeftOpen, RotateCw, ScanSearch, Search, Settings, ShieldCheck,
  Star, TrendingUp, Truck, X, Zap,
} from 'lucide-react';

import { useAuth } from '../context/AuthContext';
import { fetchApplications } from '../services/authApi';

const MODULES = [
  { key: 'APP_RATIONALIZATION', name: 'App Rationalization', group: 'Portfolio & Analysis', chip: 'Portfolio decisions', icon: BarChart3, color: '#3b82f6', url: () => process.env.REACT_APP_APP_RATIONALIZATION_URL || '/app-rationalization', description: 'Inventory, capability mapping, correlation, and rationalization decisions.' },
  { key: 'CODE_ANALYSIS', name: 'Code Analysis', group: 'Portfolio & Analysis', chip: 'Code intelligence', icon: Code2, color: '#8b5cf6', url: () => process.env.REACT_APP_CODE_ANALYSIS_URL || 'http://localhost:5173/ca/', description: 'Repository health, technical debt, cloud maturity, and engineering reports.' },
  { key: 'INFRA_SCAN', name: 'Infra Rationalization', group: 'Portfolio & Analysis', chip: 'Infrastructure scanner', icon: ScanSearch, color: '#06b6d4', url: () => process.env.REACT_APP_INFRA_SCAN_URL || 'http://localhost:5174/infra/', description: 'Infrastructure discovery, assessment, and cloud migration classification.' },
  { key: 'MODERNIZATION', name: 'Modernization Studio', group: 'Modernization & AI', chip: 'AI modernization', icon: Zap, color: '#6366f1', url: () => process.env.REACT_APP_MODERNIZATION_URL || 'http://localhost:5175/', description: 'AI-assisted legacy analysis, stack migration, and modernization workflows.' },
  { key: 'NOVASTRA_ITSM', name: 'Novastra-ITSM', group: 'Operations', chip: 'Knowledge graph', icon: Network, color: '#10b981', url: () => process.env.REACT_APP_NOVASTRA_ITSM_URL || 'http://localhost:5177/novastra-itsm/ticket-analysis', description: 'Ticket analysis, knowledge graph queries, and operational insights.' },
  { key: 'DASHBOARD', name: 'Operations Dashboard', group: 'Operations', chip: 'Digital cockpit', icon: Gauge, color: '#0ea5e9', url: () => process.env.REACT_APP_DASHBOARD_URL || 'http://localhost:5178/dash/connect?autostart=1', description: 'Operational monitoring, KPIs, analytics, and executive reporting.' },
  { key: 'SSDLC_PROCESS_ASSESSMENT', name: 'SSDLC Assessment', group: 'Operations', chip: 'SSDLC governance', icon: ShieldCheck, color: '#14b8a6', url: () => process.env.REACT_APP_SSDLC_PROCESS_ASSESSMENT_URL || 'http://localhost:5182/ssdlc/', description: 'Maturity scoring, evidence capture, recommendations, and governance.' },
  { key: 'LAB_ROBOT', name: 'Lab Robot', group: 'Operations', chip: 'Lab automation', icon: FlaskConical, color: '#22c55e', url: () => process.env.REACT_APP_LAB_ROBOT_URL || 'http://localhost:7000/', description: 'Virtual rack management, chemical tracking, and lab workflows.' },
  { key: 'OPPORTUNITY_TRACKER', name: 'Opportunity Tracker', group: 'ATM Pipeline', chip: "FY'27 pipeline", icon: TrendingUp, color: '#f59e0b', url: () => process.env.REACT_APP_OPPORTUNITY_TRACKER_URL || 'http://localhost:5183/ot/', description: 'Pipeline opportunities, commercial values, and consolidated reporting.' },
  { key: 'AI_REMAN_CORE', name: 'AI Reman Core', group: 'Modernization & AI', chip: 'AI inspection', icon: Bot, color: '#ec4899', url: () => process.env.REACT_APP_AI_REMAN_CORE_URL || 'http://localhost:5184/', description: 'Image-driven core inspection, confidence scoring, and warranty estimates.' },
  { key: 'AI_VEHICLE_LOAN', name: 'AI Vehicle Loan', group: 'Operations', chip: 'Vehicle lending', icon: Car, color: '#f97316', url: () => process.env.REACT_APP_AI_VEHICLE_LOAN_URL || 'http://localhost:5185/', description: 'Financing simulation, credit scoring, lender matching, and analysis.' },
  { key: 'MICROSITE_DATA_ANALYSIS', name: 'Data Analysis Studio', group: 'Portfolio & Analysis', chip: 'Tower consolidation', icon: LayoutPanelTop, color: '#0ea5e9', url: () => process.env.REACT_APP_MICROSITE_DATA_ANALYSIS_URL || 'http://localhost:5187/mda/', description: 'Consolidation scenarios, transformation intelligence, and roadmaps.' },
  { key: 'SUPPLY_CHAIN_DISRUPTION_MANAGER', name: 'Supply Chain Manager', group: 'Operations', chip: 'Supply chain', icon: Truck, color: '#eab308', url: () => process.env.REACT_APP_SUPPLY_CHAIN_DISRUPTION_MANAGER_URL || 'http://localhost:5188/', description: 'Signal ingestion, blast-radius analysis, and agentic incident response.' },
  { key: 'TRACEFORGE', name: 'TraceForge', group: 'Modernization & AI', chip: 'SDLC artifact factory', icon: GitBranch, color: '#a855f7', url: () => process.env.REACT_APP_TRACEFORGE_URL || 'http://localhost:5186/tf/', description: 'Requirements, test design, scripts, and end-to-end traceability.' },
];

const GROUP_ORDER = ['Portfolio & Analysis', 'Modernization & AI', 'Operations', 'ATM Pipeline'];

// These modules remain available for direct access, but are intentionally not
// advertised in the unified launcher.
const HIDDEN_MODULE_KEYS = new Set([
  'MICROSITE_DATA_ANALYSIS',
  'AI_REMAN_CORE',
  'SSDLC_PROCESS_ASSESSMENT',
  'LAB_ROBOT',
  'AI_VEHICLE_LOAN',
  'OPPORTUNITY_TRACKER',
]);
const LAUNCHER_MODULES = MODULES.filter((module) => !HIDDEN_MODULE_KEYS.has(module.key));
const LAUNCHER_GROUPS = GROUP_ORDER.filter((group) => LAUNCHER_MODULES.some((module) => module.group === group));

const withAuthHash = (url, token) => {
  if (!token) return url;
  const cleanUrl = url.split('#')[0];
  return `${cleanUrl}#authToken=${encodeURIComponent(token)}`;
};

const LaunchModulesPage = () => {
  const navigate = useNavigate();
  const location = useLocation();
  const { user, token, hasAccess, logout } = useAuth();
  const [applications, setApplications] = useState(LAUNCHER_MODULES);
  const [query, setQuery] = useState('');
  const [category, setCategory] = useState('All');
  const [view, setView] = useState('grid');
  const [favorites, setFavorites] = useState(() => {
    try { return JSON.parse(localStorage.getItem('stratiq:favorites') || '[]'); } catch { return []; }
  });
  const [favoritesOnly, setFavoritesOnly] = useState(false);
  const [railOpen, setRailOpen] = useState(true);
  const [frameKey, setFrameKey] = useState(0);
  const [frameLoading, setFrameLoading] = useState(false);

  const requestedKey = useMemo(() => new URLSearchParams(location.search).get('app'), [location.search]);
  const activeApp = useMemo(
    () => applications.find((app) => app.key === requestedKey && hasAccess(app.key)) || null,
    [applications, requestedKey, hasAccess]
  );

  const loadApplications = useCallback(() => fetchApplications()
    .then((response) => {
      const appKeys = new Set((response?.applications || []).map((app) => app.key));
      setApplications(LAUNCHER_MODULES.filter((module) => appKeys.has(module.key)));
    })
    .catch(() => setApplications(LAUNCHER_MODULES)), []);

  useEffect(() => { loadApplications(); }, [loadApplications]);
  useEffect(() => { if (requestedKey && !activeApp) navigate('/launch-modules', { replace: true }); }, [requestedKey, activeApp, navigate]);

  const filteredApplications = useMemo(() => {
    const term = query.trim().toLowerCase();
    return applications.filter((app) => {
      const textMatch = !term || `${app.name} ${app.chip} ${app.description} ${app.group}`.toLowerCase().includes(term);
      const categoryMatch = category === 'All' || app.group === category;
      const favoriteMatch = !favoritesOnly || favorites.includes(app.key);
      return textMatch && categoryMatch && favoriteMatch;
    });
  }, [applications, query, category, favoritesOnly, favorites]);

  const groupedModules = useMemo(() => LAUNCHER_GROUPS
    .map((group) => ({ group, apps: filteredApplications.filter((app) => app.group === group) }))
    .filter(({ apps }) => apps.length), [filteredApplications]);

  const openModule = (app) => {
    if (!hasAccess(app.key)) return;
    setFrameLoading(true);
    navigate(`/launch-modules?app=${encodeURIComponent(app.key)}`);
  };

  const closeModule = () => navigate('/launch-modules');
  const toggleFavorite = (event, appKey) => {
    event.stopPropagation();
    const next = favorites.includes(appKey) ? favorites.filter((key) => key !== appKey) : [...favorites, appKey];
    setFavorites(next);
    localStorage.setItem('stratiq:favorites', JSON.stringify(next));
  };

  const onLogout = async () => {
    await logout();
    navigate('/login', { replace: true });
  };

  const initials = (user?.username || 'U').slice(0, 2).toUpperCase();

  return (
    <div className="launcher-shell">
      <header className="launcher-topbar">
        <button type="button" className="launcher-waffle" aria-label="Open app menu"><Grid2X2 size={21} /></button>
        <button type="button" className="launcher-brand" onClick={closeModule}>
          <span className="launcher-brand-mark"><LayoutPanelTop size={17} /></span>
          <span>Strat-Aqorynth</span>
        </button>
        {activeApp && <span className="launcher-active-title">/ {activeApp.name}</span>}
        <div className="launcher-topbar-spacer" />
        {user?.role === 'admin' && <button type="button" className="launcher-icon-button" onClick={() => navigate('/admin')} title="Admin console"><ShieldCheck size={18} /></button>}
        <button type="button" className="launcher-icon-button" title="Settings"><Settings size={18} /></button>
        <button type="button" className="launcher-icon-button" title="Help"><CircleHelp size={18} /></button>
        <button type="button" className="launcher-avatar" title={`${user?.username || 'Account'} · Sign out`} onClick={onLogout}>{initials}</button>
      </header>

      <div className={`launcher-layout ${railOpen ? '' : 'rail-collapsed'}`}>
        <aside className="launcher-rail">
          <button type="button" className="launcher-rail-toggle" onClick={() => setRailOpen((open) => !open)} aria-label={railOpen ? 'Collapse navigation' : 'Expand navigation'}>
            {railOpen ? <PanelLeftClose size={19} /> : <PanelLeftOpen size={19} />}
          </button>
          <button type="button" className={`launcher-nav-link ${!activeApp && !favoritesOnly ? 'active' : ''}`} onClick={() => { setFavoritesOnly(false); closeModule(); }}>
            <Home size={19} /><span>Applications</span>
          </button>
          <button type="button" className={`launcher-nav-link ${favoritesOnly && !activeApp ? 'active' : ''}`} onClick={() => { setFavoritesOnly(true); closeModule(); }}>
            <Star size={19} /><span>Favorites</span>
          </button>
          <div className="launcher-rail-apps">
            {applications.slice(0, 8).map((app) => {
              const Icon = app.icon;
              return <button key={app.key} type="button" className={`launcher-nav-link ${activeApp?.key === app.key ? 'active' : ''}`} onClick={() => openModule(app)} title={app.name}><Icon size={18} /><span>{app.name}</span></button>;
            })}
          </div>
          <button type="button" className="launcher-nav-link launcher-signout" onClick={onLogout}><LogOut size={18} /><span>Sign out</span></button>
        </aside>

        {activeApp ? (
          <main className="module-workspace">
            <div className="module-commandbar">
              <button type="button" className="module-back-button" onClick={closeModule}><LayoutGrid size={17} /><span>All applications</span></button>
              <span className="module-command-divider" />
              <span className="module-command-icon" style={{ background: activeApp.color }}><activeApp.icon size={16} /></span>
              <strong>{activeApp.name}</strong>
              <span className="module-command-spacer" />
              <button type="button" className="module-command-button" onClick={() => { setFrameLoading(true); setFrameKey((key) => key + 1); }} title="Reload application"><RotateCw size={16} /> Reload</button>
              <button type="button" className="module-command-button" onClick={() => document.querySelector('.module-frame')?.requestFullscreen?.()} title="Full screen"><Maximize2 size={16} /> Full screen</button>
              <button type="button" className="module-close-button" onClick={closeModule} title="Close application"><X size={19} /></button>
            </div>
            <div className="module-frame-wrap">
              {frameLoading && <div className="module-frame-loader"><span /><p>Opening {activeApp.name}…</p></div>}
              <iframe
                key={`${activeApp.key}-${frameKey}`}
                className="module-frame"
                src={withAuthHash(activeApp.url(), token)}
                title={activeApp.name}
                onLoad={() => setFrameLoading(false)}
                allow="clipboard-read; clipboard-write; fullscreen; camera; microphone"
              />
            </div>
          </main>
        ) : (
          <main className="launcher-content">
            <div className="launcher-page-head">
              <div>
                <p className="launcher-eyebrow">Unified workspace</p>
                <h1>{favoritesOnly ? 'Favorites' : 'Applications'}</h1>
                <p>Launch your authorized tools without leaving the Strat-Aqorynth workspace.</p>
              </div>
              <label className="launcher-search"><Search size={18} /><input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="Search applications" /></label>
            </div>

            <div className="launcher-toolbar">
              <div className="launcher-filter-row">
                {['All', ...LAUNCHER_GROUPS].map((item) => <button key={item} type="button" className={category === item ? 'selected' : ''} onClick={() => setCategory(item)}>{item}</button>)}
              </div>
              <span className="launcher-result-count">{filteredApplications.length} applications</span>
              <div className="launcher-view-toggle">
                <button type="button" className={view === 'list' ? 'active' : ''} onClick={() => setView('list')} aria-label="List view"><List size={19} /></button>
                <button type="button" className={view === 'grid' ? 'active' : ''} onClick={() => setView('grid')} aria-label="Grid view"><Grid2X2 size={19} /></button>
              </div>
            </div>

            {groupedModules.length ? groupedModules.map(({ group, apps }) => (
              <section className="launcher-group" key={group}>
                <h2><ChevronDown size={18} /> {group}</h2>
                <div className={`launcher-card-grid ${view === 'list' ? 'list-view' : ''}`}>
                  {apps.map((app) => {
                    const Icon = app.icon;
                    const isFavorite = favorites.includes(app.key);
                    return (
                      <article key={app.key} className="launcher-card" onClick={() => openModule(app)} onKeyDown={(event) => event.key === 'Enter' && openModule(app)} role="button" tabIndex={0}>
                        <div className="launcher-card-art" style={{ '--card-accent': app.color }}>
                          <span className="launcher-card-orbit orbit-one" />
                          <span className="launcher-card-orbit orbit-two" />
                          <span className="launcher-card-art-icon"><Icon size={44} strokeWidth={1.35} /></span>
                          <span className="launcher-card-chip">{app.chip}</span>
                          <button type="button" className={`launcher-favorite ${isFavorite ? 'selected' : ''}`} onClick={(event) => toggleFavorite(event, app.key)} aria-label={`${isFavorite ? 'Remove' : 'Add'} ${app.name} ${isFavorite ? 'from' : 'to'} favorites`}><Star size={19} fill={isFavorite ? 'currentColor' : 'none'} /></button>
                        </div>
                        <div className="launcher-card-body">
                          <div className="launcher-card-heading"><span className="launcher-card-small-icon" style={{ background: app.color }}><Icon size={17} /></span><h3>{app.name}</h3></div>
                          <p>{app.description}</p>
                          <div className="launcher-card-footer"><span className="launcher-status-dot" /> Ready to launch <span>Open in workspace <AppWindow size={14} /></span></div>
                        </div>
                      </article>
                    );
                  })}
                </div>
              </section>
            )) : <div className="launcher-empty"><Search size={28} /><h2>No applications found</h2><p>Try a different search or category.</p></div>}
          </main>
        )}
      </div>
    </div>
  );
};

export default LaunchModulesPage;
