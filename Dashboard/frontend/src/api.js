// ---------------------------------------------------------------------------
// Author: Vishnuu A
// Scope: Dashboard — frontend/src (api.js)
// Date: 2026-01-20
// ---------------------------------------------------------------------------
import axios from 'axios'

// Dev: falls back to '/api' so vite proxy (/api → localhost:8087) still works.
// Prod: VITE_DASH_API_URL=/api/dashboard → IIS rewrites /api/dashboard/* → localhost:8087/api/*
const _apiBase = import.meta.env.VITE_DASH_API_URL ||
  (import.meta.env.DEV ? '/api' : '/api/dashboard')
const _backendBase = _apiBase.startsWith('http')
  ? _apiBase.replace(/\/api(?:\/dashboard)?\/?$/, '')
  : ''

const AUTH_TOKEN_KEY = 'token'
const PORTAL_SESSION_KEY = 'portal_auth_session'

// Function: getSharedPortalToken
const getSharedPortalToken = () => {
  try {
    const session = JSON.parse(sessionStorage.getItem(PORTAL_SESSION_KEY) || 'null')
    return session?.token || null
  } catch {
    return null
  }
}

// Function: getPortalToken
const getPortalToken = () =>
  // Dashboard is embedded by the same-origin launcher. Prefer its current
  // authenticated session over legacy `token` keys, which can contain an
  // expired token from an earlier login and cause every API call to return 401.
  getSharedPortalToken() ||
  sessionStorage.getItem(AUTH_TOKEN_KEY) ||
  localStorage.getItem(AUTH_TOKEN_KEY)

// Function: setPortalToken
const setPortalToken = (token) => {
  if (!token) return
  sessionStorage.setItem(AUTH_TOKEN_KEY, token)
}

// Consumes the `#authToken=` handoff the central portal's LaunchModulesPage
// appends when launching this module (see withAuthHash in
// AppRationalization/frontend/src/pages/LaunchModulesPage.jsx). TopMenu.jsx
// already reads/clears localStorage['token']; this is what populates it.
const _hash = window.location.hash || ''
const _hashParams = _hash.startsWith('#') ? new URLSearchParams(_hash.slice(1)) : null
const _searchParams = new URLSearchParams(window.location.search || '')
const _handoffToken =
  _hashParams?.get('authToken') || _hashParams?.get('token') ||
  _searchParams.get('authToken') || _searchParams.get('token')
if (_handoffToken) {
  setPortalToken(_handoffToken)
  window.history.replaceState(null, document.title, window.location.pathname + window.location.search)
}

const client = axios.create({
  baseURL: _apiBase,
  timeout: 120000,
  headers: {
    'Content-Type': 'application/json',
  },
})

client.interceptors.request.use((config) => {
  const token = getPortalToken()
  if (token) {
    config.headers = config.headers || {}
    config.headers.Authorization = `Bearer ${token}`
  }
  return config
})

// Add response interceptor for error handling
client.interceptors.response.use(
  (response) => response,
  async (error) => {
    const status = error?.response?.status
    const original = error?.config || {}
    const reqUrl = original.url || ''

    // Some environments proxy /api/* directly to Dashboard backend (8087)
    // without the extra /dashboard segment. If /api/dashboard/* returns 404,
    // retry once against /api/* to avoid hard failure.
    if (
      status === 404 &&
      _apiBase === '/api/dashboard' &&
      typeof reqUrl === 'string' &&
      reqUrl.startsWith('/') &&
      !original.__apiBaseFallbackRetried
    ) {
      const retryConfig = {
        ...original,
        __apiBaseFallbackRetried: true,
        baseURL: '/api',
      }
      return client.request(retryConfig)
    }

    console.error('API Error:', {
      status,
      url: reqUrl,
      detail: error.response?.data || error.message,
    })
    return Promise.reject(error)
  }
)

// ---------------------------------------------------------------------------
// Date param helper — appends start_date / end_date when provided
// ---------------------------------------------------------------------------
// Function: buildDateQS
function buildDateQS(dateRange = {}) {
  const parts = []
  if (dateRange?.startDate) parts.push(`start_date=${encodeURIComponent(dateRange.startDate)}`)
  if (dateRange?.endDate)   parts.push(`end_date=${encodeURIComponent(dateRange.endDate)}`)
  return parts.length ? `&${parts.join('&')}` : ''
}

// ---------------------------------------------------------------------------
// Connection / status
// ---------------------------------------------------------------------------
// Function: getConfig
export const getConfig  = () => client.get('/config')
// Function: getStatus
export const getStatus  = () => client.get('/status')
// Function: connect
export const connect    = (creds) => client.post('/connect', creds)
// Function: syncData
export const syncData   = (creds) => client.post('/sync', creds || undefined)
// Function: disconnect
export const disconnect = () => client.delete('/disconnect')

// ---------------------------------------------------------------------------
// Dashboard data — all accept an optional dateRange object
// ---------------------------------------------------------------------------
// Function: getKPIs
export const getKPIs = (dateRange = {}) =>
  client.get(`/kpis?_=1${buildDateQS(dateRange)}`)

// Function: getMonthlyVolume
export const getMonthlyVolume = (months = 12, dateRange = {}) =>
  client.get(`/monthly-volume?months=${months}${buildDateQS(dateRange)}`)

// Function: getCycleTime
export const getCycleTime = (dateRange = {}) =>
  client.get(`/cycle-time?_=1${buildDateQS(dateRange)}`)

// Function: getIncidents
export const getIncidents = (dateRange = {}) =>
  client.get(`/incidents?_=1${buildDateQS(dateRange)}`)

// Function: getChanges
export const getChanges = (dateRange = {}) =>
  client.get(`/changes?_=1${buildDateQS(dateRange)}`)

// Function: getServiceRequests
export const getServiceRequests = (dateRange = {}) =>
  client.get(`/service-requests?_=1${buildDateQS(dateRange)}`)

// Function: getApplicationHotspots
export const getApplicationHotspots = (topN = 10, dateRange = {}) =>
  client.get(`/application-hotspots?top_n=${topN}${buildDateQS(dateRange)}`)

// Function: getAssignmentGroupHotspots
export const getAssignmentGroupHotspots = (topN = 10, dateRange = {}) =>
  client.get(`/assignment-group-hotspots?top_n=${topN}${buildDateQS(dateRange)}`)

// Function: getAutomationCandidates
export const getAutomationCandidates = (topN = 20, options = {}, dateRange = {}) => {
  const deepAnalysis = options.deepAnalysis ?? true
  const useOllama    = options.useOllama    ?? true
  const llmOnly      = options.llmOnly      ?? false
  const gpuRequired  = options.gpuRequired  ?? false
  return client.get(
    `/automation-candidates?top_n=${topN}&deep_analysis=${deepAnalysis}&use_ollama=${useOllama}&llm_only=${llmOnly}&gpu_required=${gpuRequired}${buildDateQS(dateRange)}`
  )
}

// Function: getInsights
export const getInsights = (options = {}, dateRange = {}) => {
  const quick = options.quick ?? false
  return client.get(`/insights?quick=${quick}${buildDateQS(dateRange)}`)
}

// Function: getRepeatIncidents
export const getRepeatIncidents = (dateRange = {}) =>
  client.get(`/repeat-incidents?_=1${buildDateQS(dateRange)}`)

// Function: getRCAOwnership
export const getRCAOwnership = (dateRange = {}) =>
  client.get(`/rca-ownership?_=1${buildDateQS(dateRange)}`)

// Function: getPreventiveMaintenance
export const getPreventiveMaintenance = (dateRange = {}) =>
  client.get(`/preventive-maintenance?_=1${buildDateQS(dateRange)}`)

// Function: getAdHocVsBau
export const getAdHocVsBau = (dateRange = {}) =>
  client.get(`/adhoc-vs-bau?_=1${buildDateQS(dateRange)}`)

// Function: getSLABreachRisk
export const getSLABreachRisk = (months = 12, dateRange = {}) =>
  client.get(`/sla-breach-risk?months=${months}${buildDateQS(dateRange)}`)

// Function: getTransformationKpis
export const getTransformationKpis = (dateRange = {}) =>
  client.get(`/transformation-kpis?_=1${buildDateQS(dateRange)}`)

// Function: getPeopleCapacity
export const getPeopleCapacity = (dateRange = {}) =>
  client.get(`/people-capacity?_=1${buildDateQS(dateRange)}`)

// ---------------------------------------------------------------------------
// Critical incident routes (not date-filtered)
// ---------------------------------------------------------------------------
// Function: invokeCriticalIncident
export const invokeCriticalIncident = (payload = {}) =>
  client.post('/invoke-critical-incident', payload)

// Function: getCriticalAlerts
export const getCriticalAlerts = () => client.get('/critical-alerts')

// ---------------------------------------------------------------------------
// Chart image helper (server-side PNG — not date-filtered)
// ---------------------------------------------------------------------------
// Function: renderChart
export const renderChart = (name) => `${_backendBase}/render/${name}.png?t=${Date.now()}`

export default client
