// ---------------------------------------------------------------------------
// Author: Vishnuu A
// Scope: Dashboard — frontend/src/components (ConnectionPanel.jsx)
// Date: 2025-10-29
// ---------------------------------------------------------------------------
import React, { useState, useEffect } from 'react'
import { useNavigate } from 'react-router-dom'
import {
  Server,
  Link,
  Unlink,
  RefreshCw,
  Database,
  CheckCircle,
  XCircle,
  AlertCircle,
  ArrowRight,
  Eye,
  EyeOff,
  Activity,
} from 'lucide-react'
import { connect, syncData, disconnect, getConfig } from '../api'
import { useDashboard } from '../context/DashboardContext'

const DEMO_SERVICENOW_CONNECTION = Object.freeze({
  url: 'https://dev274740.service-now.com',
  username: 'admin',
})

// Function: extractApiError
function extractApiError(err, fallback) {
  const detail = err?.response?.data?.detail
  if (typeof detail === 'string' && detail.trim()) return detail
  if (detail && typeof detail === 'object') {
    if (typeof detail.message === 'string' && detail.message.trim()) return detail.message
    try {
      return JSON.stringify(detail)
    } catch {
      // no-op
    }
  }
  if (typeof err?.response?.data === 'string' && err.response.data.trim()) return err.response.data
  return err?.message || fallback
}

// Function: TopBar
function TopBar({ statusColor, statusBg, statusText, StatusIcon }) {
  return (
    <div className="az-topbar">
      <div className="az-logo-mark">
        <Activity size={15} />
      </div>
      <div className="flex-1 min-w-0">
        <p className="az-topbar-title">Digital Operations Cockpit</p>
        <p className="az-topbar-eyebrow" style={{ textTransform: 'none', letterSpacing: 0 }}>AI-Powered ITSM Intelligence</p>
      </div>
      <div className={`az-tag-badge ${statusBg}`} style={{ color: statusColor }}>
        <StatusIcon className="w-3.5 h-3.5" style={{ marginRight: 4 }} />
        {statusText}
      </div>
    </div>
  )
}

// Function: HeaderCard
function HeaderCard({ connected, connectionExpiresAt }) {
  return (
    <div className="az-panel">
      <div className="flex items-start justify-between gap-4 flex-wrap">
        <div className="flex items-center gap-3">
          <div className="az-panel-icon">
            <Server className="w-5 h-5" />
          </div>
          <div>
            <h2 className="text-lg font-semibold text-slate-900">ServiceNow / JIRA Connection</h2>
            <p className="text-slate-500 text-sm mt-0.5">
              Connection retained for 5 minutes unless disconnected
              {connected && connectionExpiresAt
                ? ` — expires ${new Date(connectionExpiresAt).toLocaleTimeString()}`
                : ''}
            </p>
          </div>
        </div>
        <div className="text-right text-xs text-slate-500 max-w-xs leading-relaxed rounded-sm px-3 py-2 border" style={{ background: '#fff4ce', borderColor: '#f5d78c' }}>
          <AlertCircle className="w-3.5 h-3.5 inline mr-1" style={{ color: '#ca5010' }} />
          Sync is optional while vector DB is fresh (&lt;12h). Older than 12h requires a fresh sync.
        </div>
      </div>
    </div>
  )
}

// Function: ConnectionForm
function ConnectionForm({
  provider, setProvider, authType, setAuthType, url, user,
  password, setPassword, passwordEncrypted, showPassword, setShowPassword,
  connecting, syncing, connected, handleConnect, handleDisconnect,
}) {
  let connectButtonLabel = 'Connect'
  if (connecting) connectButtonLabel = 'Connecting...'
  else if (syncing) connectButtonLabel = 'Loading data...'

  return (
    <div className="az-panel space-y-5">
      <h3 className="text-xs font-semibold text-slate-500 uppercase tracking-wider flex items-center gap-2">
        <Link className="w-4 h-4" style={{ color: '#0078d4' }} />
        Connection Settings
      </h3>

      <div className="grid grid-cols-3 gap-4">
        <div className="space-y-1.5">
          <label htmlFor="conn-provider" className="text-xs font-medium text-slate-500 uppercase tracking-wide">Provider</label>
          <select
            id="conn-provider"
            value={provider}
            onChange={(e) => setProvider(e.target.value)}
            className="az-field cursor-pointer"
          >
            <option value="ServiceNow">ServiceNow</option>
            <option value="JIRA">JIRA</option>
          </select>
        </div>

        <div className="space-y-1.5">
          <label htmlFor="conn-auth-type" className="text-xs font-medium text-slate-500 uppercase tracking-wide">Auth Type</label>
          <select
            id="conn-auth-type"
            value={authType}
            onChange={(e) => setAuthType(e.target.value)}
            className="az-field cursor-pointer"
          >
            <option value="basic">Basic (username + password/token)</option>
            <option value="oauth2">OAuth2</option>
          </select>
        </div>

        <div className="space-y-1.5">
          <label htmlFor="conn-instance-url" className="text-xs font-medium text-slate-500 uppercase tracking-wide">Instance Base URL</label>
          <input
            id="conn-instance-url"
            type="text"
            value={url}
            readOnly
            placeholder="https://your-instance.service-now.com"
            className="az-field"
          />
        </div>
      </div>

      <div className="grid grid-cols-2 gap-4">
        <div className="space-y-1.5">
          <label className="text-xs font-medium text-slate-500 uppercase tracking-wide">
            {provider === 'JIRA' ? 'Email' : 'Username'}
          </label>
          <input
            type="text"
            value={user}
            readOnly
            placeholder={provider === 'JIRA' ? 'you@company.com' : 'admin'}
            className="az-field"
          />
        </div>

        <div className="space-y-1.5">
          <label className="text-xs font-medium text-slate-500 uppercase tracking-wide">
            {passwordEncrypted ? 'Encrypted Password / API Token' : (authType === 'basic' ? 'Password / API Token' : 'OAuth2 Token')}
          </label>
          <div className="relative">
            <input
              type={showPassword ? 'text' : 'password'}
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              placeholder="••••••••"
              className="az-field pr-10"
            />
            <button
              type="button"
              onClick={() => setShowPassword(!showPassword)}
              className="absolute right-3 top-1/2 -translate-y-1/2 text-slate-400 hover:text-slate-600 transition-colors"
            >
              {showPassword ? <EyeOff className="w-4 h-4" /> : <Eye className="w-4 h-4" />}
            </button>
          </div>
        </div>
      </div>

      <div className="flex items-center justify-end gap-3 pt-3" style={{ borderTop: '1px solid #edebe9' }}>
        <button onClick={handleConnect} disabled={connecting || syncing} className="az-btn-primary">
          {(connecting || syncing) ? (
            <RefreshCw className="w-4 h-4 animate-spin" />
          ) : (
            <Link className="w-4 h-4" />
          )}
          {connectButtonLabel}
        </button>

        <button
          onClick={handleDisconnect}
          disabled={!connected}
          className="az-btn-secondary"
          style={{ color: '#a4262c', borderColor: '#d9a8ac' }}
        >
          <Unlink className="w-4 h-4" />
          Disconnect
        </button>
      </div>
    </div>
  )
}

// Function: StatusAlerts
function StatusAlerts({ syncing, syncProgress, error, success }) {
  return (
    <>
      {syncing && syncProgress && (
        <div className="az-panel px-5 py-3 flex items-center gap-3" style={{ background: '#eff6fc', borderColor: '#c7e0f4' }}>
          <RefreshCw className="w-4 h-4 animate-spin shrink-0" style={{ color: '#0078d4' }} />
          <p className="text-sm" style={{ color: '#0078d4' }}>{syncProgress}</p>
        </div>
      )}

      {error && (
        <div className="az-panel px-5 py-3 flex items-center gap-3" style={{ background: '#fdf3f4', borderColor: '#d9a8ac' }}>
          <XCircle className="w-4 h-4 shrink-0" style={{ color: '#a4262c' }} />
          <p className="text-sm" style={{ color: '#a4262c' }}>{error}</p>
        </div>
      )}

      {success && (
        <div className="az-panel px-5 py-3 flex items-center gap-3" style={{ background: '#dff6dd', borderColor: '#9fd89c' }}>
          <CheckCircle className="w-4 h-4 shrink-0" style={{ color: '#107c10' }} />
          <p className="text-sm" style={{ color: '#107c10' }}>{success}</p>
        </div>
      )}
    </>
  )
}

// Function: RecordCountsPanel
function RecordCountsPanel({ recordEntries }) {
  if (recordEntries.length === 0) return null
  return (
    <div className="az-panel">
      <h3 className="text-sm font-semibold text-slate-700 mb-4 flex items-center gap-2">
        <Database className="w-4 h-4" style={{ color: '#0078d4' }} />
        Synced Records
      </h3>
      <div className="grid grid-cols-3 gap-3">
        {recordEntries.map(([key, val]) => (
          <div key={key} className="az-stat-card text-center">
            <p className="az-stat-value" style={{ marginTop: 0 }}>{typeof val === 'number' ? val.toLocaleString() : val}</p>
            <p className="az-stat-label mt-1 capitalize">{key.replaceAll('_', ' ')}</p>
          </div>
        ))}
      </div>
    </div>
  )
}

// Function: DashboardCTA
function DashboardCTA({ connected, synced, handleGoToDashboard }) {
  if (!(connected && synced)) return null
  return (
    <div className="az-panel flex items-center justify-between flex-wrap gap-3" style={{ background: '#eff6fc', borderColor: '#c7e0f4' }}>
      <div>
        <p className="text-slate-900 font-semibold">Ready to explore your data</p>
        <p className="text-slate-500 text-sm mt-0.5">All tickets synced. Open the dashboard to see insights.</p>
      </div>
      <button onClick={handleGoToDashboard} className="az-btn-primary">
        Go to Dashboard
        <ArrowRight className="w-4 h-4" />
      </button>
    </div>
  )
}

// Function: ConnectionPanel
export default function ConnectionPanel() {
  const navigate = useNavigate()
  const {
    connected,
    setConnected,
    synced,
    setSynced,
    setLastSynced,
    recordCounts,
    setRecordCounts,
    syncing,
    setSyncing,
    connecting,
    setConnecting,
    instanceUrl,
    username,
    connectionExpiresAt,
    refreshStatus,
  } = useDashboard()

  const [provider, setProvider] = useState('ServiceNow')
  const [authType, setAuthType] = useState('basic')
  const [url, setUrl] = useState(DEMO_SERVICENOW_CONNECTION.url)
  const [user, setUser] = useState(DEMO_SERVICENOW_CONNECTION.username)
  const [password, setPassword] = useState('')
  const [passwordEncrypted, setPasswordEncrypted] = useState(false)
  const [showPassword, setShowPassword] = useState(false)
  const [error, setError] = useState('')
  const [success, setSuccess] = useState('')
  const [syncProgress, setSyncProgress] = useState('')

  useEffect(() => {
    getConfig()
      .then((res) => {
        const { encrypted_password: encryptedPassword } = res.data
        setUrl(DEMO_SERVICENOW_CONNECTION.url)
        setUser(DEMO_SERVICENOW_CONNECTION.username)
        if (encryptedPassword) {
          setPassword(encryptedPassword)
          setPasswordEncrypted(true)
        }
      })
      .catch(() => {})
  }, [])

  useEffect(() => {
    setUrl(DEMO_SERVICENOW_CONNECTION.url)
    setUser(DEMO_SERVICENOW_CONNECTION.username)
  }, [instanceUrl, username])

  function buildCredentials() {
    return {
      url: DEMO_SERVICENOW_CONNECTION.url,
      username: DEMO_SERVICENOW_CONNECTION.username,
      password: passwordEncrypted ? '' : password,
      encrypted_password: passwordEncrypted ? password : undefined,
      verify_ssl: false,
    }
  }

  const statusColor = connected ? '#107c10' : '#a4262c'
  const statusBg = connected
    ? 'bg-[#dff6dd] border border-[#9fd89c]'
    : 'bg-[#fdf3f4] border border-[#d9a8ac]'
  const statusText = connected ? 'Connected' : 'Disconnected'
  const StatusIcon = connected ? CheckCircle : XCircle

  // Function: handleConnect
  async function handleConnect() {
    if (!url.trim() || !user.trim() || !password.trim()) {
      setError('Please fill in Instance URL, Username, and Password.')
      return
    }
    setError('')
    setSuccess('')
    setConnecting(true)
    try {
      await connect(buildCredentials())
      await refreshStatus()
      // Auto-sync immediately after connecting so dashboard data is ready
      setSyncing(true)
      setSyncProgress('Connected — loading data from ServiceNow...')
      try {
        const res = await syncData(buildCredentials())
        const data = res.data
        if (data.record_counts) setRecordCounts(data.record_counts)
        setSynced(true)
        setLastSynced(new Date().toISOString())
        await refreshStatus()
        navigate('/dashboard')
      } catch (syncErr) {
        setError(extractApiError(syncErr, 'Sync failed after connect.'))
      } finally {
        setSyncProgress('')
        setSyncing(false)
      }
    } catch (e) {
      setError(extractApiError(e, 'Connection failed.'))
    } finally {
      setConnecting(false)
    }
  }

  // Function: handleDisconnect
  async function handleDisconnect() {
    setError('')
    setSuccess('')
    try {
      await disconnect()
      setConnected(false)
      setSynced(false)
      setRecordCounts({})
      setSuccess('Disconnected.')
      await refreshStatus()
    } catch (e) {
      setError(extractApiError(e, 'Disconnect failed.'))
    }
  }

  // Function: handleGoToDashboard
  function handleGoToDashboard() {
    navigate('/dashboard')
  }

  const recordEntries = Object.entries(recordCounts)

  return (
    <div className="min-h-screen flex flex-col" style={{ background: '#faf9f8' }}>
      <TopBar statusColor={statusColor} statusBg={statusBg} statusText={statusText} StatusIcon={StatusIcon} />

      <div className="flex-1 flex items-start justify-center px-4 py-10">
        <div className="w-full max-w-3xl space-y-6">
          <HeaderCard connected={connected} connectionExpiresAt={connectionExpiresAt} />

          <ConnectionForm
            provider={provider}
            setProvider={setProvider}
            authType={authType}
            setAuthType={setAuthType}
            url={url}
            user={user}
            password={password}
            setPassword={(value) => {
              setPassword(value)
              setPasswordEncrypted(false)
            }}
            passwordEncrypted={passwordEncrypted}
            showPassword={showPassword}
            setShowPassword={setShowPassword}
            connecting={connecting}
            syncing={syncing}
            connected={connected}
            handleConnect={handleConnect}
            handleDisconnect={handleDisconnect}
          />

          <StatusAlerts syncing={syncing} syncProgress={syncProgress} error={error} success={success} />

          <RecordCountsPanel recordEntries={recordEntries} />

          <DashboardCTA connected={connected} synced={synced} handleGoToDashboard={handleGoToDashboard} />
        </div>
      </div>
    </div>
  )
}
