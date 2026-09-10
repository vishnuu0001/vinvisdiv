// ---------------------------------------------------------------------------
// Author: Vishnuu A
// Scope: Novastra-ITSM — frontend/src/services (api.js)
// Date: 2026-03-15
// ---------------------------------------------------------------------------
import axios from 'axios'

// Dev: falls back to '/api' so vite proxy (/api → localhost:8086) still works.
// Prod: VITE_NOVASTRA_ITSM_API_URL=/api/novastra-itsm → IIS rewrites /api/novastra-itsm/* → localhost:8086/api/*
const _apiBase = import.meta.env.VITE_NOVASTRA_ITSM_API_URL ||
  (import.meta.env.DEV ? '/api' : '/api/novastra-itsm')
export const apiBase = _apiBase
const api = axios.create({ baseURL: _apiBase })
// Same constant/default as App.jsx — Novastra-ITSM has no login page of its own (see
// pages/LoginPage.jsx), so an expired/invalid token sends the user to the
// CENTRAL portal's real login, not a Novastra-ITSM-branded one.
const PORTAL_LOGIN_URL = import.meta.env.VITE_PORTAL_LOGIN_URL || '/login'
const PORTAL_SSO_MARKER_KEY = 'novastra_portal_sso_in_progress'

// Separate instance for LLM/agent calls — no timeout (Ollama can take 60-120s)
const agentApi = axios.create({ baseURL: _apiBase, timeout: 0 })

/** Called by AuthContext whenever the token changes. */
// Function: setAuthToken
export function setAuthToken(token) {
  const header = token ? `Bearer ${token}` : null
  if (header) {
    api.defaults.headers.common['Authorization'] = header
    agentApi.defaults.headers.common['Authorization'] = header
  } else {
    delete api.defaults.headers.common['Authorization']
    delete agentApi.defaults.headers.common['Authorization']
  }
  // Re-hydrate on page load from localStorage
  const stored = localStorage.getItem('novastra_itsm_auth')
  if (!token && stored) {
    try {
      const t = JSON.parse(stored).token
      if (t) {
        api.defaults.headers.common['Authorization'] = `Bearer ${t}`
        agentApi.defaults.headers.common['Authorization'] = `Bearer ${t}`
      }
    } catch {}
  }
}

// Initialise from localStorage on module load
setAuthToken(null)

// Auto-clear stale/expired tokens on 401 responses
api.interceptors.response.use(
  (response) => response,
  (error) => {
    if (error.response?.status === 401 && !error.config?.skipAuthRedirect) {
      // Clear the stored token so the user is prompted to log in again
      localStorage.removeItem('novastra_itsm_auth')
      delete api.defaults.headers.common['Authorization']
      delete agentApi.defaults.headers.common['Authorization']
      // A portal handoff can overlap requests carrying a stale local token.
      // The AuthProvider owns navigation while #authToken is present; redirecting
      // here would abandon a successful exchange and bounce back to the launcher.
      // Function: portalSsoInProgress
      const portalSsoInProgress = (() => {
        try {
          const hash = window.location.hash || ''
          return sessionStorage.getItem(PORTAL_SSO_MARKER_KEY) === 'true' ||
            (hash.startsWith('#') &&
              new URLSearchParams(hash.slice(1)).has('authToken'))
        } catch {
          return false
        }
      })()
      // Only redirect if not already headed there and no SSO handoff is active.
      if (!portalSsoInProgress && window.location.pathname !== PORTAL_LOGIN_URL) {
        window.location.href = PORTAL_LOGIN_URL
      }
    }
    return Promise.reject(error)
  }
)

// ── Agent ───────────────────────────────────────────────────
// Function: queryAgent
export const queryAgent = (data) => agentApi.post('/agent/query', data)

// Function: queryJobPollDelayMs
const queryJobPollDelayMs = (attempt, retryAfterMs) => {
  if (Number.isFinite(retryAfterMs) && retryAfterMs > 0) {
    return Math.min(3200, Math.max(350, retryAfterMs))
  }
  if (attempt < 4) return 350
  if (attempt < 12) return 650
  if (attempt < 36) return 1100
  if (attempt < 90) return 1600
  return 2200
}

// Function: featureJobPollDelayMs
const featureJobPollDelayMs = (attempt, retryAfterMs) => {
  if (Number.isFinite(retryAfterMs) && retryAfterMs > 0) {
    return Math.min(3600, Math.max(400, retryAfterMs))
  }
  if (attempt < 4) return 400
  if (attempt < 16) return 700
  if (attempt < 60) return 1200
  if (attempt < 150) return 1700
  return 2300
}

// Function: queryAgentAsync
export const queryAgentAsync = async (data) => {
  const submit = await agentApi.post('/agent/query-async', data)
  const { status, job_id, result } = submit.data || {}
  if (status === 'done' && result) {
    return { data: result }
  }
  if (!job_id) {
    throw new Error('Async query job was not created')
  }

  const MAX_POLLS = 300 // lower request volume while preserving long-running support
  for (let i = 0; i < MAX_POLLS; i++) {
    const polled = await agentApi.get(`/agent/query-job/${job_id}`)
    const body = polled.data || {}
    if (body.status === 'done' && body.result) {
      return { data: body.result }
    }
    if (body.status === 'error') {
      throw new Error(body.detail || 'Async query failed')
    }
    if (body.status === 'pending') {
      const waitMs = queryJobPollDelayMs(i, body.retry_after_ms)
      await new Promise((resolve) => setTimeout(resolve, waitMs))
    }
  }
  throw new Error('Async query timed out')
}
// Function: pollSyntheticTicket
export const pollSyntheticTicket = (data) => agentApi.post('/agent/poll-ticket', data)
// Function: queryAgentWithFile
export const queryAgentWithFile = (formData) =>
  agentApi.post('/agent/query-with-attachment', formData, {
    headers: { 'Content-Type': 'multipart/form-data' },
  })
// Function: historyListSessions
export const historyListSessions = (limit = 100) => api.get(`/chat-history/sessions?limit=${limit}`)
// Function: historyCreateSession
export const historyCreateSession = (data) => api.post('/chat-history/sessions', data)
// Function: historyUpdateSession
export const historyUpdateSession = (sessionId, data) => api.put(`/chat-history/sessions/${sessionId}`, data)
// Function: historyDeleteSession
export const historyDeleteSession = (sessionId) => api.delete(`/chat-history/sessions/${sessionId}`)

// ── ServiceNow ───────────────────────────────────────────────
// Function: snFetchResolve
export const snFetchResolve = (data) => api.post('/servicenow/fetch-and-resolve', data)
// Function: snManualResolve
export const snManualResolve = (data) => api.post('/servicenow/manual-resolve', data)
// Function: snScreenshotResolve
export const snScreenshotResolve = (formData) =>
  api.post('/servicenow/screenshot-resolve', formData, {
    headers: { 'Content-Type': 'multipart/form-data' },
  })
// Function: snUpdateTicket
export const snUpdateTicket = (data) => api.post('/servicenow/update-ticket', data)
// Function: snConnectionDefaults
// Connection defaults include a short-lived encrypted password envelope. The
// plaintext password and the encryption key remain backend-only.
export const snConnectionDefaults = () => api.get('/servicenow/connection-defaults')
// Function: snTestConnection
// A 401 from this endpoint describes the supplied ServiceNow credentials, not the
// user's StratIQ session. Keep the portal auth interceptor from logging the user out.
export const snTestConnection = (data) =>
  api.post('/servicenow/test-connection', data, { skipAuthRedirect: true })
// Function: snSyncStatus
export const snSyncStatus = (maxAgeHours = 168) => api.get(`/servicenow/sync-status?max_age_hours=${maxAgeHours}`)
// Function: snOneTimeSync
export const snOneTimeSync = (data) => api.post('/servicenow/one-time-sync', data)
// Function: snSyncJobStatus
export const snSyncJobStatus = (jobId) => api.get(`/servicenow/sync-job/${jobId}`)

// ── Dashboard ────────────────────────────────────────────────
// Function: getDashboardIncidents
export const getDashboardIncidents = (params) => 
  api.get('/dashboard/incidents', { params })
// Function: getDashboardIncident
export const getDashboardIncident = (incidentNumber) => 
  api.get(`/dashboard/incidents/${incidentNumber}`)
// Function: getDashboardStats
export const getDashboardStats = () => 
  api.get('/dashboard/stats')
// Function: getDashboardFilterOptions
export const getDashboardFilterOptions = () =>
  api.get('/dashboard/filter-options')
// Function: updateDashboardIncident
export const updateDashboardIncident = (incidentNumber, data) =>
  api.put(`/dashboard/incidents/${incidentNumber}`, data)

// ── Admin ────────────────────────────────────────────────────
// Function: adminIndex
export const adminIndex = (data, secret) =>
  api.post('/admin/index', data, { headers: { 'x-admin-secret': secret } })
// Function: adminStats
export const adminStats = (secret) =>
  api.get('/admin/stats', { headers: { 'x-admin-secret': secret } })
// Function: adminListDocs
export const adminListDocs = (secret) =>
  api.get('/admin/documents', { headers: { 'x-admin-secret': secret } })
// Function: adminUpload
export const adminUpload = (formData, secret) =>
  api.post('/admin/upload', formData, {
    headers: { 'Content-Type': 'multipart/form-data', 'x-admin-secret': secret },
  })
// Function: adminDeleteDoc
export const adminDeleteDoc = (data, secret) =>
  api.delete('/admin/document', { data, headers: { 'x-admin-secret': secret } })

// ── Feedback ─────────────────────────────────────────────────
// Function: submitFeedback
export const submitFeedback = (data) => api.post('/feedback', data)
// Function: getFeedbackAll
export const getFeedbackAll = () => api.get('/feedback/all')
// Function: getFeedbackStats
export const getFeedbackStats = () => api.get('/feedback/stats')

// ── Settings ─────────────────────────────────────────────────
// Function: getSettings
export const getSettings = () => api.get('/settings')
// Function: updateSettings
export const updateSettings = (data) => api.post('/settings', data)

// ── Data Sources ─────────────────────────────────────────────
// Function: dsListSources
export const dsListSources  = ()         => api.get('/datasources')
// Function: dsGetTypes
export const dsGetTypes     = ()         => api.get('/datasources/types')
// Function: dsAddSource
export const dsAddSource    = (body)     => api.post('/datasources', body)
// Function: dsDeleteSource
export const dsDeleteSource = (id)       => api.delete(`/datasources/${id}`)
// Function: dsSyncSource
export const dsSyncSource   = (id)       => api.post(`/datasources/${id}/sync`)
// Function: dsProcessFiles
export const dsProcessFiles = (formData) =>
  api.post('/datasources/process-files', formData, { timeout: 0 })

// ── Ticket Analysis ─────────────────────────────────────────
// Function: ticketAnalyzeExcel
export const ticketAnalyzeExcel = (formData) =>
  api.post('/ticket-analysis/analyze-excel', formData, {
    timeout: 0,
    headers: { 'Content-Type': 'multipart/form-data' },
  })

// Function: automationJobPollDelayMs
const automationJobPollDelayMs = (attempt, retryAfterMs) => {
  if (Number.isFinite(retryAfterMs) && retryAfterMs > 0) {
    return Math.min(3200, Math.max(300, retryAfterMs))
  }
  if (attempt < 4) return 300
  if (attempt < 12) return 600
  if (attempt < 36) return 1100
  return 1600
}

// Function: ticketPredictAutomation
export const ticketPredictAutomation = async (analysisPayload) => {
  const submit = await api.post('/ticket-analysis/predict-automation-async', analysisPayload, { timeout: 0 })
  const { status, job_id, result } = submit.data || {}
  if (status === 'done' && result) {
    return { data: result }
  }
  if (!job_id) {
    throw new Error('Automation prediction job was not created')
  }

  const MAX_POLLS = 480
  for (let i = 0; i < MAX_POLLS; i++) {
    const polled = await api.get(`/ticket-analysis/predict-automation-job/${job_id}`)
    const body = polled.data || {}
    if (body.status === 'done' && body.result) {
      return { data: body.result }
    }
    if (body.status === 'error') {
      throw new Error(body.detail || 'Automation prediction failed')
    }
    if (body.status === 'pending') {
      await new Promise((resolve) => setTimeout(resolve, automationJobPollDelayMs(i, body.retry_after_ms)))
    }
  }

  throw new Error('Automation prediction timed out')
}

// ── Incident Workbench ──────────────────────────────────────
// Function: iwRunFeature
export const iwRunFeature = (data) =>
  api.post('/incident-workbench/run-feature', data, { timeout: 0 })

// Function: iwRunFeatureAsync
export const iwRunFeatureAsync = async (data) => {
  let submit
  try {
    submit = await api.post('/incident-workbench/run-feature-async', data)
  } catch (err) {
    const status = err?.response?.status
    // Backward compatibility: older backends may not expose async feature endpoints yet.
    if (status === 404 || status === 405) {
      return api.post('/incident-workbench/run-feature', data, { timeout: 0 })
    }
    throw err
  }
  const { status, job_id, result } = submit.data || {}
  if (status === 'done' && result) {
    return { data: result }
  }
  if (!job_id) {
    throw new Error('Feature job was not created')
  }

  const MAX_POLLS = 600 // lower request volume while preserving long-running support
  for (let i = 0; i < MAX_POLLS; i++) {
    const polled = await api.get(`/incident-workbench/feature-job/${job_id}`)
    const body = polled.data || {}
    if (body.status === 'done' && body.result) {
      return { data: body.result }
    }
    if (body.status === 'error') {
      throw new Error(body.detail || 'Feature execution failed')
    }
    if (body.status === 'pending') {
      const waitMs = featureJobPollDelayMs(i, body.retry_after_ms)
      await new Promise((resolve) => setTimeout(resolve, waitMs))
    }
  }

  throw new Error('Feature execution timed out')
}

// ── Knowledge Graph ──────────────────────────────────────────
// Function: kgGetGraph
export const kgGetGraph  = () => api.get('/knowledge-graph')
// Function: kgGetStats
export const kgGetStats  = () => api.get('/knowledge-graph/stats')

// ── Modern Search API ───────────────────────────────────────
// Function: semanticSearch
export const semanticSearch = (data) => agentApi.post('/search/semantic', data)
// Function: answerWithSearch
export const answerWithSearch = (data) => agentApi.post('/search/answer', data)

export default api

