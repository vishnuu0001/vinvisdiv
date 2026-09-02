// ---------------------------------------------------------------------------
// Author: Vishnuu A
// Scope: AppRationalization — frontend/src/pages (LaunchModulesPage.jsx)
// Date: 2026-05-27
// ---------------------------------------------------------------------------
import React, { useCallback, useEffect, useMemo, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import {
  ArrowRight,
  BarChart3,
  Bot,
  Car,
  Code2,
  FlaskConical,
  Gauge,
  GitBranch,
  Home,
  LayoutPanelTop,
  LogOut,
  Network,
  ScanSearch,
  Search,
  ShieldCheck,
  TrendingUp,
  Truck,
  Zap,
} from 'lucide-react';

import { useAuth } from '../context/AuthContext';
import { fetchApplications } from '../services/authApi';

const MODULES = [
  {
    key: 'APP_RATIONALIZATION',
    number: '01',
    name: 'App Rationalization',
    group: 'Portfolio & Analysis',
    chip: 'Portfolio Decisions',
    icon: BarChart3,
    url: () => `${window.location.origin}/app-rationalization`,
    description: 'Inventory, correlation, capability mapping, and rationalization decisions.',
  },
  {
    key: 'CODE_ANALYSIS',
    number: '02',
    name: 'Code Analysis',
    group: 'Portfolio & Analysis',
    chip: 'Code Intelligence',
    icon: Code2,
    url: () => process.env.REACT_APP_CODE_ANALYSIS_URL || 'http://localhost:5173/ca/',
    description: 'Repository health scoring, technical debt analysis, cloud maturity, and engineering reports.',
  },
  {
    key: 'INFRA_SCAN',
    number: '03',
    name: 'Infra Rationalization',
    group: 'Portfolio & Analysis',
    chip: 'Infra Scanner',
    icon: ScanSearch,
    url: () => process.env.REACT_APP_INFRA_SCAN_URL || 'http://localhost:5174/infra/',
    description: 'Infrastructure scan, assessment, and cloud migration classification.',
  },
  {
    key: 'MODERNIZATION',
    number: '04',
    name: 'Modernization Studio',
    group: 'Modernization & AI',
    chip: 'Modernization',
    icon: Zap,
    url: () => process.env.REACT_APP_MODERNIZATION_URL || 'http://localhost:5175/',
    description: 'AI-assisted legacy code analysis, stack migration, and modernization workflows.',
  },
  {
    key: 'NOVASTRA_ITSM',
    number: '05',
    name: 'Novastra-ITSM',
    group: 'Operations',
    chip: 'Knowledge Graph',
    icon: Network,
    url: () => process.env.REACT_APP_NOVASTRA_ITSM_URL || 'http://localhost:5177/novastra-itsm/ticket-analysis',
    description: 'Ticket analysis, knowledge graph queries, and operational insight exploration.',
  },
  {
    key: 'DASHBOARD',
    number: '06',
    name: 'Dashboard',
    group: 'Operations',
    chip: 'Digital Cockpit',
    icon: Gauge,
    url: () => process.env.REACT_APP_DASHBOARD_URL || 'http://localhost:5178/dash/connect?autostart=1',
    description: 'Digital operations monitoring, KPI views, analytics, and reporting.',
  },
  {
    key: 'SSDLC_PROCESS_ASSESSMENT',
    number: '07',
    name: 'SSDLC Process Assessment',
    group: 'Operations',
    chip: 'SSDLC Governance',
    icon: ShieldCheck,
    url: () => process.env.REACT_APP_SSDLC_PROCESS_ASSESSMENT_URL || 'http://localhost:5182/ssdlc/',
    description: 'Weighted maturity scoring, evidence capture, recommendations, and dashboard review.',
  },
  {
    key: 'LAB_ROBOT',
    number: '08',
    name: 'Lab Robot',
    group: 'Operations',
    chip: 'Lab Robot',
    icon: FlaskConical,
    url: () => process.env.REACT_APP_LAB_ROBOT_URL || 'http://localhost:7000/',
    description: 'Virtual rack management, barcode tracking, chemical placement, and lab workflows.',
  },
  {
    key: 'OPPORTUNITY_TRACKER',
    number: '09',
    name: 'Opportunity Tracker',
    group: 'ATM Pipeline',
    chip: "FY'27 Pipeline",
    icon: TrendingUp,
    url: () => process.env.REACT_APP_OPPORTUNITY_TRACKER_URL || 'http://localhost:5183/ot/',
    description: 'Pipeline opportunities, commercial values, and consolidated reporting.',
  },
  {
    key: 'AI_REMAN_CORE',
    number: '10',
    name: 'AI Reman Core',
    group: 'Modernization & AI',
    chip: 'AI Inspection',
    icon: Bot,
    url: () => process.env.REACT_APP_AI_REMAN_CORE_URL || 'http://localhost:5184/',
    description: 'Image-driven remanufactured core inspection, confidence scoring, and warranty estimation.',
  },
  {
    key: 'AI_VEHICLE_LOAN',
    number: '11',
    name: 'AI Vehicle Loan',
    group: 'Operations',
    chip: 'Vehicle Loans',
    icon: Car,
    url: () => process.env.REACT_APP_AI_VEHICLE_LOAN_URL || 'http://localhost:5185/',
    description: 'Vehicle financing simulation, credit scoring, lender matching, and loan analysis.',
  },
  {
    key: 'MICROSITE_DATA_ANALYSIS',
    number: '12',
    name: 'Data Analysis Studio',
    group: 'Portfolio & Analysis',
    chip: 'Tower Consolidation',
    icon: LayoutPanelTop,
    url: () => process.env.REACT_APP_MICROSITE_DATA_ANALYSIS_URL || 'http://localhost:5187/mda/',
    description: 'Tower consolidation simulation, transformation intelligence, scenarios, and roadmap planning.',
  },
  {
    key: 'SUPPLY_CHAIN_DISRUPTION_MANAGER',
    number: '13',
    name: 'Supply Chain Disruption Manager',
    group: 'Operations',
    chip: 'Supply Chain',
    icon: Truck,
    url: () => process.env.REACT_APP_SUPPLY_CHAIN_DISRUPTION_MANAGER_URL || 'http://localhost:5188/',
    description: 'Sense-Understand-Act disruption management: signal ingestion, knowledge graph blast radius, and agentic incident response.',
  },
  {
    key: 'TRACEFORGE',
    number: '14',
    name: 'TraceForge',
    group: 'Modernization & AI',
    chip: 'SDLC Artifact Factory',
    icon: GitBranch,
    url: () => process.env.REACT_APP_TRACEFORGE_URL || 'http://localhost:5186/tf/',
    description: 'AI-assisted requirements extraction, BRD authoring, test design, and script generation with a full traceability spine.',
  },
];

// Keep attached modules configured for direct access and easy restoration, but
// do not advertise them on the workspace launcher.
const HIDDEN_MODULE_KEYS = new Set([
  'SSDLC_PROCESS_ASSESSMENT',
  'OPPORTUNITY_TRACKER',
  'AI_REMAN_CORE',
  'AI_VEHICLE_LOAN',
  'MICROSITE_DATA_ANALYSIS',
]);
const VISIBLE_MODULES = MODULES.filter((module) => !HIDDEN_MODULE_KEYS.has(module.key));

const groupOrder = ['Portfolio & Analysis', 'Modernization & AI', 'Operations', 'ATM Pipeline'];
const GROUP_DETAILS = {
  'Portfolio & Analysis': {
    eyebrow: 'Portfolio intelligence',
    title: 'Map the estate, then move with intent.',
    description: 'Start with discovery, prioritization, and rationalization before you launch into transformation work.',
    accent: '#0078d4',
  },
  'Modernization & AI': {
    eyebrow: 'Modernization studio',
    title: 'Transform code, docs, and delivery with AI-native workflows.',
    description: 'Use the modernization and AI modules as a connected workspace for migrations, automation, and artifact generation.',
    accent: '#8764b8',
  },
  Operations: {
    eyebrow: 'Operations cockpit',
    title: 'Run the business with controlled, observable actions.',
    description: 'Use the operational tools for service workflows, governance, lab flows, and live control surfaces.',
    accent: '#107c10',
  },
  'ATM Pipeline': {
    eyebrow: 'Pipeline radar',
    title: 'Track commercial motion and pipeline momentum.',
    description: 'Move from opportunity discovery to execution planning with the same launch surface.',
    accent: '#ca5010',
  },
};
// Function: withAuthHash
const withAuthHash = (url, token) => (token ? `${url}#authToken=${encodeURIComponent(token)}` : url);

const labRobotDesktopUri = (token) => {
  const query = token ? `?token=${encodeURIComponent(token)}` : '';
  return `stratiq-labrobot://open/lab-robot${query}`;
};

// Function: LaunchModulesPage
const LaunchModulesPage = () => {
  const navigate = useNavigate();
  const { user, token, hasAccess, logout } = useAuth();
  const [applications, setApplications] = useState(VISIBLE_MODULES);
  const [query, setQuery] = useState('');

  const loadApplications = useCallback(() => {
    return fetchApplications()
      .then((response) => {
        const appKeys = new Set((response?.applications || []).map((app) => app.key));
        setApplications(VISIBLE_MODULES.filter((module) => appKeys.has(module.key)));
      })
      .catch(() => {
        setApplications(VISIBLE_MODULES);
      });
  }, []);

  useEffect(() => {
    loadApplications();
  }, [loadApplications]);

  const groupedModules = useMemo(
    () =>
      applications.reduce((groups, app) => {
        const group = app.group || 'Operations';
        return { ...groups, [group]: [...(groups[group] || []), app] };
      }, {}),
    [applications]
  );

  const visibleGroups = useMemo(() => {
    const q = query.trim().toLowerCase();
    return groupOrder.filter((group) => {
      const apps = groupedModules[group] || [];
      if (!q) return apps.length > 0;
      return apps.some((app) => app.name.toLowerCase().includes(q) || app.chip.toLowerCase().includes(q));
    });
  }, [groupedModules, query]);

  const matchesQuery = (app) => {
    const q = query.trim().toLowerCase();
    if (!q) return true;
    return app.name.toLowerCase().includes(q) || app.chip.toLowerCase().includes(q);
  };

  // Function: openModule
  const openModule = (app) => {
    if (!hasAccess(app.key)) return;
    if (app.key === 'LAB_ROBOT' && process.env.REACT_APP_LAB_ROBOT_DESKTOP_ENABLED !== 'false') {
      window.location.assign(labRobotDesktopUri(token));
      return;
    }
    window.location.assign(withAuthHash(app.url(), token));
  };

  // Function: onLogout
  const onLogout = async () => {
    await logout();
    navigate('/login', { replace: true });
  };

  const initials = (user?.username || 'U').slice(0, 2).toUpperCase();

  return (
    <div className="az-shell">
      <header className="az-topbar">
        <div className="az-logo-mark">
          <LayoutPanelTop size={15} />
        </div>
        <div className="az-brand">
          <span className="az-brand-name">Strat-Aqorynth</span>
          <span className="az-brand-sub">Workspace launcher</span>
        </div>

        <label className="az-searchbar">
          <Search size={14} />
          <input
            type="text"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="Search modules, chips, and capabilities"
          />
        </label>

        {user?.role === 'admin' && (
          <button type="button" onClick={() => navigate('/admin')} className="az-topbar-btn">
            <ShieldCheck size={13} />
            Admin Console
          </button>
        )}
        <button type="button" onClick={onLogout} className="az-topbar-btn">
          <LogOut size={13} />
          Logout
        </button>
        <div className="az-avatar" title={user?.username || 'Account'}>{initials}</div>
      </header>

      <div className="az-body">
        <nav className="az-nav-rail">
          <button type="button" className="az-nav-item" data-active="true" title="Home">
            <Home size={18} />
          </button>
          {user?.role === 'admin' && (
            <button type="button" className="az-nav-item" title="Admin Console" onClick={() => navigate('/admin')}>
              <ShieldCheck size={18} />
            </button>
          )}
        </nav>

        <main className="az-content">
          {visibleGroups.length === 0 ? (
            <div className="az-empty-state">No modules match "{query}".</div>
          ) : (
            visibleGroups.map((group) => {
              const detail = GROUP_DETAILS[group];
              const apps = (groupedModules[group] || []).filter(matchesQuery);
              return (
                <section key={group} className="az-section">
                  <div className="az-section-head">
                    <div className="az-section-swatch" style={{ background: detail.accent }} />
                    <div>
                      <p className="az-section-eyebrow">{detail.eyebrow}</p>
                      <h3 className="az-section-title">{group}</h3>
                    </div>
                  </div>
                  <p className="az-section-desc">{detail.title} {detail.description}</p>

                  <div className="az-tile-grid">
                    {apps.map((app) => {
                      const canUse = hasAccess(app.key);
                      const Icon = app.icon;
                      return (
                        <button
                          key={app.key}
                          type="button"
                          onClick={() => openModule(app)}
                          disabled={!canUse}
                          className="az-tile"
                        >
                          <div className="az-tile-icon" style={{ background: detail.accent }}>
                            <Icon size={16} />
                          </div>
                          <div className="az-tile-body">
                            <span className="az-tile-chip">{app.chip}</span>
                            <span className="az-tile-name">{app.name}</span>
                          </div>
                          <ArrowRight size={16} className="az-tile-arrow" />
                        </button>
                      );
                    })}
                  </div>
                </section>
              );
            })
          )}
        </main>
      </div>
    </div>
  );
};

export default LaunchModulesPage;
