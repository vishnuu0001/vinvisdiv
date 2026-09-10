// ---------------------------------------------------------------------------
// Author: Vishnuu A
// Scope: Dashboard — frontend/src/pages (ServiceNowLogin.jsx)
// Date: 2026-06-03
// ---------------------------------------------------------------------------
import React, { useState, useEffect } from 'react'
import { useNavigate } from 'react-router-dom'
import { Server, Lock, User, AlertCircle, CheckCircle, Loader } from 'lucide-react'
import { connect, syncData, getConfig } from '../api'
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

// Function: ServiceNowLogin
export default function ServiceNowLogin() {
  const navigate = useNavigate()
  const { setConnected, setSynced, setLastSynced, setRecordCounts } = useDashboard()

  const [url, setUrl] = useState(DEMO_SERVICENOW_CONNECTION.url)
  const [username, setUsername] = useState(DEMO_SERVICENOW_CONNECTION.username)
  const [password, setPassword] = useState('')
  const [passwordEncrypted, setPasswordEncrypted] = useState(false)
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(false)
  const [success, setSuccess] = useState(false)

  // Load config on mount
  useEffect(() => {
    getConfig()
      .then((res) => {
        const { encrypted_password: encryptedPassword } = res.data
        setUrl(DEMO_SERVICENOW_CONNECTION.url)
        setUsername(DEMO_SERVICENOW_CONNECTION.username)
        if (encryptedPassword) {
          setPassword(encryptedPassword)
          setPasswordEncrypted(true)
        }
      })
      .catch(() => {})
  }, [])

  // Function: handleLogin
  async function handleLogin() {
    if (!url.trim() || !username.trim() || !password.trim()) {
      setError('Please fill in all fields.')
      return
    }

    setError('')
    setLoading(true)

    try {
      // Connect to ServiceNow
      await connect({
        url: DEMO_SERVICENOW_CONNECTION.url,
        username: DEMO_SERVICENOW_CONNECTION.username,
        password: passwordEncrypted ? '' : password,
        encrypted_password: passwordEncrypted ? password : undefined,
        verify_ssl: false,
      })

      // Sync data
      const res = await syncData({
        url: DEMO_SERVICENOW_CONNECTION.url,
        username: DEMO_SERVICENOW_CONNECTION.username,
        password: passwordEncrypted ? '' : password,
        encrypted_password: passwordEncrypted ? password : undefined,
        verify_ssl: false,
      })

      if (res.data.record_counts) {
        setRecordCounts(res.data.record_counts)
      }

      setConnected(true)
      setSynced(true)
      setLastSynced(new Date().toISOString())
      setSuccess(true)

      // Redirect to dashboard after 1 second
      setTimeout(() => {
        navigate('/dashboard')
      }, 1000)
    } catch (err) {
      setError(extractApiError(err, 'Login failed. Please check your credentials.'))
    } finally {
      setLoading(false)
    }
  }

  return (
    <div className="min-h-screen bg-gradient-to-br from-navy-950 via-navy-900 to-navy-800 flex items-center justify-center p-4">
      <div className="w-full max-w-md rounded-2xl border border-slate-700 bg-slate-900/90 p-8 shadow-2xl">
        {/* Header */}
        <div className="mb-8 text-center">
          <div className="mb-4 flex justify-center">
            <div className="rounded-xl bg-cyan-600/20 p-3">
              <Server size={32} className="text-cyan-400" />
            </div>
          </div>
          <p className="text-xs uppercase tracking-[0.2em] text-cyan-300 mb-2">ServiceNow Integration</p>
          <h1 className="text-3xl font-bold text-white">Dashboard Login</h1>
          <p className="mt-2 text-sm text-slate-400">Connect to ServiceNow to access operational dashboards</p>
        </div>

        {/* Form */}
        <form
          onSubmit={(e) => {
            e.preventDefault()
            handleLogin()
          }}
          className="space-y-5"
        >
          {/* URL */}
          <div>
            <label className="block text-xs font-semibold text-slate-300 mb-2">Instance URL</label>
            <input
              type="url"
              value={url}
              readOnly
              placeholder="https://dev393867.service-now.com"
              className="w-full px-4 py-2.5 rounded-lg bg-slate-800 border border-slate-600 text-white placeholder-slate-500 focus:border-cyan-500 focus:outline-none transition-colors"
            />
          </div>

          {/* Username */}
          <div>
            <label className="block text-xs font-semibold text-slate-300 mb-2 flex items-center gap-2">
              <User size={14} />
              Username
            </label>
            <input
              type="text"
              value={username}
              readOnly
              placeholder="admin"
              className="w-full px-4 py-2.5 rounded-lg bg-slate-800 border border-slate-600 text-white placeholder-slate-500 focus:border-cyan-500 focus:outline-none transition-colors"
            />
          </div>

          {/* Password */}
          <div>
            <label className="block text-xs font-semibold text-slate-300 mb-2 flex items-center gap-2">
              <Lock size={14} />
              {passwordEncrypted ? 'Encrypted Password' : 'Password'}
            </label>
            <input
              type="password"
              value={password}
              onChange={(e) => {
                setPassword(e.target.value)
                setPasswordEncrypted(false)
              }}
              placeholder="••••••••"
              className="w-full px-4 py-2.5 rounded-lg bg-slate-800 border border-slate-600 text-white placeholder-slate-500 focus:border-cyan-500 focus:outline-none transition-colors"
            />
          </div>

          {/* Error */}
          {error && (
            <div className="flex items-start gap-3 p-4 rounded-lg bg-rose-900/30 border border-rose-700/50">
              <AlertCircle size={18} className="text-rose-400 flex-shrink-0 mt-0.5" />
              <p className="text-sm text-rose-200">{error}</p>
            </div>
          )}

          {/* Success */}
          {success && (
            <div className="flex items-start gap-3 p-4 rounded-lg bg-emerald-900/30 border border-emerald-700/50">
              <CheckCircle size={18} className="text-emerald-400 flex-shrink-0 mt-0.5" />
              <p className="text-sm text-emerald-200">Login successful! Redirecting...</p>
            </div>
          )}

          {/* Login Button */}
          <button
            type="submit"
            disabled={loading || success}
            className="w-full mt-8 px-4 py-3 rounded-lg bg-gradient-to-r from-cyan-600 to-blue-600 hover:from-cyan-500 hover:to-blue-500 disabled:from-slate-600 disabled:to-slate-600 text-white font-semibold flex items-center justify-center gap-2 transition-all shadow-lg hover:shadow-xl"
          >
            {loading || success ? (
              <>
                <Loader size={18} className="animate-spin" />
                {success ? 'Redirecting...' : 'Connecting...'}
              </>
            ) : (
              <>
                <Server size={18} />
                Login to ServiceNow
              </>
            )}
          </button>
        </form>

        {/* Footer */}
        <p className="mt-6 text-center text-xs text-slate-500">
          Secure connection • Credentials verified by ServiceNow
        </p>
      </div>
    </div>
  )
}
