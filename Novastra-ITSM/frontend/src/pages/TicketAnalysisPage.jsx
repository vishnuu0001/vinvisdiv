// ---------------------------------------------------------------------------
// Author: Vishnuu A
// Scope: Novastra-ITSM — frontend/src/pages (TicketAnalysisPage.jsx)
// Date: 2026-07-01
// ---------------------------------------------------------------------------
import { useState, useEffect, useRef } from 'react'
import { useLocation } from 'react-router-dom'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import toast from 'react-hot-toast'
import { CheckCircle2, Database, Loader2, Send, Server, Unplug } from 'lucide-react'

import { iwRunFeatureAsync, pollSyntheticTicket, queryAgentAsync, snConnectionDefaults, snOneTimeSync, snSyncJobStatus, snSyncStatus, snTestConnection } from '../services/api.js'
import EmbeddedDashboard from '../components/EmbeddedDashboard.jsx'
import SourceBadges from '../components/SourceBadges.jsx'
import ConfidenceBadge from '../components/ConfidenceBadge.jsx'
import { useSettings } from '../hooks/useSettings.js'
import { useChatContext } from '../contexts/ChatContext.jsx'

const FEATURE_TABS = [
  { id: 'classify_incident', label: 'Classify Incident' },
  { id: 'anomaly_analysis', label: 'Anomaly Analysis' },
  { id: 'event_correlation', label: 'Event Correlation' },
  { id: 'incident_summarize', label: 'Incident Summarize' },
  { id: 'similar_incidents', label: 'Similar Incidents' },
  { id: 'remediation_playbook', label: 'Remediation Playbook' },
  { id: 'script_generator', label: 'Script Generator' },
]

const FEATURE_PROMPTS = {
  classify_incident: 'Classify each ticket into category, subcategory, and severity rationale.',
  anomaly_analysis: 'Identify anomalies across the tickets: unusual patterns, outliers, and data gaps.',
  event_correlation: 'Correlate events across tickets and explain likely relationships and clusters.',
  incident_summarize: 'Summarize incidents with issue, impact, status, and owner/action items.',
  similar_incidents: 'Find similar incidents for these tickets and explain the similarity basis.',
  remediation_playbook: 'Create a remediation playbook with validation and rollback steps.',
  script_generator: 'Generate safe PowerShell and Bash script templates to support remediation.',
}

const MAX_CHAT_HISTORY_TURNS = 6
const MAX_CHAT_HISTORY_CHARS = 1200
const MAX_FEATURE_TRANSCRIPT_MESSAGES = 8
const MAX_FEATURE_TRANSCRIPT_CHARS = 1800
// Large historical imports can require hours of local embedding work. The job
// continues server-side, and the UI must not report a false timeout after 12m.
const SYNC_STATUS_MAX_POLLS = 7200
const SYNC_STATUS_POLL_DELAY_MS = 3000
const DEMO_SERVICENOW_CONNECTION = {
  base_url: 'https://dev274740.service-now.com',
  username: 'admin',
  password: 'zs8PWmCQ$v2+',
}

// Function: getChatAvailability
const getChatAvailability = (canChat, isConnected) => {
  if (canChat) return 'enabled'
  if (isConnected) return 'sync_required'
  return 'disabled'
}

const CHAT_BADGE_LABELS = {
  enabled: 'Chat Enabled',
  sync_required: 'Sync Required',
  disabled: 'Chat Disabled',
}

const CHAT_PLACEHOLDER_LABELS = {
  enabled: 'Ask about synced incidents...',
  sync_required: 'Sync required when last refresh > 168h',
  disabled: 'Reconnect ServiceNow to enable chat',
}

const CHAT_EMPTY_STATE_LABELS = {
  enabled: 'Start chatting: e.g. "Show all critical access incidents resolved with workaround."',
  sync_required: 'Chat requires vector DB refresh within the last 168 hours. Run One-time Sync if stale.',
  disabled: 'Chat is disabled while ServiceNow is disconnected. Reconnect to enable support chat.',
}

// Function: formatApiError
const formatApiError = (err, fallback = 'Request failed') => {
  const detail = err?.response?.data?.detail
  if (typeof detail === 'string' && detail.trim()) {
    return detail
  }
  if (Array.isArray(detail)) {
    const messages = detail
      .map((entry) => {
        if (typeof entry === 'string') return entry
        if (entry && typeof entry === 'object') return entry.msg || JSON.stringify(entry)
        return ''
      })
      .filter(Boolean)
    if (messages.length > 0) {
      return messages.join('; ')
    }
  }
  if (detail && typeof detail === 'object') {
    if (typeof detail.msg === 'string' && detail.msg.trim()) return detail.msg
    try {
      return JSON.stringify(detail)
    } catch {
      return fallback
    }
  }
  return err?.message || fallback
}

// Function: removeSuggestedSolution
const removeSuggestedSolution = (text) => {
  const lines = String(text || '').split(/\r?\n/)
  const cleaned = []
  let skippingContinuation = false

  for (const line of lines) {
    const trimmed = line.trim()
    const isHeaderLike = /^\s*[A-Za-z][A-Za-z\s/()-]*\s*:\s*/.test(line)
    const startsSuggested = /^\s*suggested\s*solution\s*:/i.test(trimmed)

    if (startsSuggested) {
      skippingContinuation = true
      continue
    }

    if (skippingContinuation) {
      if (!trimmed) {
        continue
      }
      if (isHeaderLike) {
        skippingContinuation = false
      } else {
        continue
      }
    }

    cleaned.push(line)
  }

  return cleaned.join('\n').replace(/\n{3,}/g, '\n\n').trim()
}

// Function: compactMessageContent
const compactMessageContent = (value, maxChars = MAX_CHAT_HISTORY_CHARS) => {
  const text = String(value || '').trim()
  if (!text) return ''
  return text.length <= maxChars ? text : `${text.slice(0, maxChars)}...`
}

// Function: sleep
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms))

// Function: buildRecentChatHistory
const buildRecentChatHistory = (messages) => (
  (messages || [])
    .slice(-MAX_CHAT_HISTORY_TURNS)
    .map((message) => ({
      role: message.role,
      content: compactMessageContent(message.content),
    }))
    .filter((message) => message.content)
)

// Function: buildFeatureTranscript
const buildFeatureTranscript = (messages, tickets) => {
  const ticketSet = new Set((tickets || []).map((ticket) => ticket.toUpperCase()))
  const relevant = (messages || []).filter((message) => {
    const content = String(message.content || '')
    if (!content.trim()) return false
    if (ticketSet.size === 0) return true
    const upper = content.toUpperCase()
    return [...ticketSet].some((ticket) => upper.includes(ticket))
  })

  const source = relevant.length > 0 ? relevant : (messages || [])
  const lines = source
    .slice(-MAX_FEATURE_TRANSCRIPT_MESSAGES)
    .map((message) => `${message.role === 'human' ? 'User' : 'Assistant'}: ${compactMessageContent(message.content, 420)}`)

  const transcript = lines.join('\n')
  if (transcript.length <= MAX_FEATURE_TRANSCRIPT_CHARS) {
    return transcript
  }
  return `${transcript.slice(0, MAX_FEATURE_TRANSCRIPT_CHARS)}...`
}

// Function: SyncStatusBanner
function SyncStatusBanner({ syncState, syncGate }) {
  return (
    <>
      {syncState && (
        <div className="mt-3 rounded-md border p-2 text-xs" style={{ borderColor: '#9fd89c', background: '#dff6dd', color: '#107c10' }}>
          Synced tickets: {syncState.tickets_fetched} | Indexed chunks: {syncState.chunks_indexed}
        </div>
      )}
      {syncGate && (
        <div
          className="mt-2 rounded-md border p-2 text-xs"
          style={
            syncGate.can_chat
              ? { borderColor: '#c7e0f4', background: '#eff6fc', color: '#0078d4' }
              : { borderColor: '#f5d78c', background: '#fff4ce', color: '#ca5010' }
          }
        >
          {syncGate.can_chat
            ? `Vector DB fresh. Last sync: ${syncGate.last_sync_at || 'N/A'} (${syncGate.hours_since_sync ?? 'N/A'}h ago).`
            : (syncGate.reason || 'Sync required to enable chat.')}
        </div>
      )}
    </>
  )
}

// Function: ChatMessageBubble
function ChatMessageBubble({ message }) {
  return (
    <div
      className="rounded-md border p-2 text-xs"
      style={
        message.role === 'human'
          ? { borderColor: '#c7e0f4', background: '#eff6fc', color: '#242424' }
          : { borderColor: '#c8c6c4', background: '#ffffff', color: '#242424' }
      }
    >
      <p className="mb-1 flex items-center gap-2 text-[10px] uppercase tracking-wide text-slate-400">
        <span>{message.role === 'human' ? 'You' : 'Assistant'}</span>
        {message.role === 'assistant' && (
          <span className="rounded-full border px-1.5 py-0.5 text-[9px] font-semibold" style={{ borderColor: '#c7e0f4', background: '#eff6fc', color: '#0078d4' }}>
            Mode: {(message.mode || 'chat').toUpperCase()}
          </span>
        )}
      </p>
      {message.role === 'human' ? (
        <div className="whitespace-pre-wrap break-words text-left leading-7" style={{ color: '#242424' }}>
          {message.content}
        </div>
      ) : (
        <div className="answer-prose-dark">
          <ReactMarkdown remarkPlugins={[remarkGfm]}>{message.content}</ReactMarkdown>
        </div>
      )}
      {message.role === 'assistant' && (
        <div className="mt-2 flex flex-wrap gap-2">
          <ConfidenceBadge value={message.confidence || 0} contextUsed={Boolean(message.context_used)} />
          {message.sources?.length > 0 && <SourceBadges sources={message.sources} />}
        </div>
      )}
    </div>
  )
}

// Function: ChatMessagesPanel
function ChatMessagesPanel({ messages, chatAvailability, bottomRef }) {
  return (
    <div className="mb-3 max-h-56 space-y-2 overflow-y-auto rounded border p-2" style={{ borderColor: '#edebe9', background: '#ffffff' }}>
      {messages.length === 0 ? (
        <p className="text-xs text-slate-400">{CHAT_EMPTY_STATE_LABELS[chatAvailability]}</p>
      ) : (
        messages.map((msg) => <ChatMessageBubble key={msg.id} message={msg} />)
      )}
      <div ref={bottomRef} />
    </div>
  )
}

// Function: FeatureResultPanel
function FeatureResultPanel({ featureResult }) {
  if (!featureResult) {
    return (
      <div className="rounded-sm border border-[#edebe9] bg-[#faf9f8] p-3 text-xs text-slate-400">
        Start chat with incident IDs (for example, INC0012345), then run a feature tab here.
      </div>
    )
  }

  return (
    <div className="space-y-3 rounded-sm border border-[#edebe9] bg-[#faf9f8] p-3">
      <p className="text-xs" style={{ color: '#0078d4' }}>
        Tickets in scope: {featureResult.tickets.join(', ')}
      </p>
      <div className="inline-flex w-fit rounded-full border px-2 py-0.5 text-[10px] font-semibold uppercase tracking-wide" style={{ borderColor: '#9fd89c', background: '#dff6dd', color: '#107c10' }}>
        Mode: {(featureResult.mode || 'analysis').toUpperCase()}
      </div>
      <div className="answer-prose rounded border border-slate-700 bg-slate-900/50 p-3 text-slate-100 [&_h1]:text-slate-100 [&_h2]:text-slate-100 [&_h3]:text-slate-100 [&_p]:text-slate-200 [&_ul]:text-slate-200 [&_ol]:text-slate-200 [&_strong]:text-slate-100 [&_blockquote]:text-slate-300 [&_code]:text-slate-900">
        <ReactMarkdown remarkPlugins={[remarkGfm]}>{featureResult.answer}</ReactMarkdown>
      </div>
      <div className="flex flex-wrap gap-2">
        <ConfidenceBadge value={featureResult.confidence || 0} contextUsed={Boolean(featureResult.context_used)} />
        {featureResult.sources?.length > 0 && <SourceBadges sources={featureResult.sources} />}
      </div>
    </div>
  )
}

// Function: TicketAnalysisPage
export default function TicketAnalysisPage() {
  const { settings } = useSettings()
  const { createSession, updateSessionMessages } = useChatContext()
  const location = useLocation()

  const [conn, setConn] = useState({
    provider: 'ServiceNow',
    auth_type: 'basic',
    base_url: DEMO_SERVICENOW_CONNECTION.base_url,
    username: DEMO_SERVICENOW_CONNECTION.username,
    password: DEMO_SERVICENOW_CONNECTION.password,
    client_id: '',
    client_secret: '',
  })

  const [testing, setTesting] = useState(false)
  const [syncing, setSyncing] = useState(false)
  const [connectionState, setConnectionState] = useState('disconnected')
  const [syncState, setSyncState] = useState(null)
  const [syncGate, setSyncGate] = useState(null)
  const [serverCredentials, setServerCredentials] = useState({ basic: false, oauth: false })
  const [useServerCredentials, setUseServerCredentials] = useState(false)

  const [chatMessages, setChatMessages] = useState([])
  const [supportSessionId, setSupportSessionId] = useState(null)
  const [chatInput, setChatInput] = useState('')
  const [chatLoading, setChatLoading] = useState(false)
  const [pollingTicket, setPollingTicket] = useState(false)

  const [activeFeature, setActiveFeature] = useState('classify_incident')
  const [featureLoading, setFeatureLoading] = useState(false)
  const [featureResult, setFeatureResult] = useState(null)

  const chatBottomRef = useRef(null)
  const chatInputRef = useRef(null)
  const chatRequestInFlightRef = useRef(false)
  const featureRequestInFlightRef = useRef(false)
  const pollingTicketInFlightRef = useRef(false)

  useEffect(() => {
    chatBottomRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [chatMessages])

  useEffect(() => {
    const el = chatInputRef.current
    if (!el) return
    el.style.height = 'auto'
    el.style.height = `${Math.min(el.scrollHeight, 220)}px`
  }, [chatInput])

  const provider = settings?.llm_provider || 'ollama'
  const isConnected = connectionState === 'connected'
  const canChat = isConnected && Boolean(syncGate?.can_chat)
  const chatAvailability = getChatAvailability(canChat, isConnected)

  // Function: refreshSyncGate
  const refreshSyncGate = async () => {
    try {
      const { data } = await snSyncStatus(168)
      setSyncGate(data)
    } catch {
      setSyncGate(null)
    }
  }

  useEffect(() => {
    refreshSyncGate()
  }, [])

  // Prefill the connection form from the backend's own .env config (base URL,
  // username, OAuth client ID) so it doesn't need to be typed in every time.
  // Passwords/secrets are never sent here -- leaving those fields blank still
  // works, since the backend falls back to its own configured secret on submit.
  useEffect(() => {
    (async () => {
      try {
        const { data } = await snConnectionDefaults()
        const hasBasic = Boolean(data.has_password_configured)
        const hasOauth = hasBasic && Boolean(data.has_client_secret_configured)
        setServerCredentials({ basic: hasBasic, oauth: hasOauth })
        setUseServerCredentials(false)
        setConn((p) => ({
          ...p,
          base_url: p.base_url || data.base_url || '',
          username: p.username || data.username || '',
          client_id: p.client_id || data.client_id || '',
        }))
      } catch {
        // No server-side defaults configured (or endpoint unavailable on an
        // older deployed backend) -- fall back to manual entry, unchanged.
      }
    })()
  }, [])

  const buildConnectionPayload = () => {
    const serverAuthAvailable = conn.auth_type === 'oauth'
      ? serverCredentials.oauth
      : serverCredentials.basic

    // Never let password-manager autofill override verified server secrets.
    // When server credentials are selected, omitting every credential field
    // makes the backend resolve the complete matching set from its own config.
    if (useServerCredentials && serverAuthAvailable) {
      return { auth_type: conn.auth_type }
    }

    return {
      auth_type: conn.auth_type,
      base_url: conn.base_url || undefined,
      username: conn.username || undefined,
      password: conn.password || undefined,
      client_id: conn.auth_type === 'oauth' ? conn.client_id || undefined : undefined,
      client_secret: conn.auth_type === 'oauth' ? conn.client_secret || undefined : undefined,
    }
  }

  // Handle preloaded incident from Dashboard
  useEffect(() => {
    if (location.state?.preloadedMessage) {
      const { preloadedMessage, incidentNumber } = location.state
      setChatInput(preloadedMessage)
      toast.success(`Loaded incident ${incidentNumber || ''} from Dashboard`)
      // Clear the location state to prevent re-loading on refresh
      window.history.replaceState({}, document.title)
    }
  }, [location.state])

  // Function: extractTicketsFromChat
  const extractTicketsFromChat = () => {
    const text = chatMessages.map((m) => m.content || '').join('\n')
    const matches = text.match(/\bINC\d+\b/gi) || []
    return [...new Set(matches.map((m) => m.toUpperCase()))]
  }

  // Function: onTestConnection
  const onTestConnection = async () => {
    setTesting(true)
    try {
      const payload = buildConnectionPayload()
      const { data } = await snTestConnection(payload)
      setConnectionState('connected')
      toast.success(data.message || 'ServiceNow connection verified.')
    } catch (err) {
      setConnectionState('disconnected')
      toast.error(formatApiError(err, 'Connection failed'))
    } finally {
      setTesting(false)
    }
  }

  // Function: onOneTimeSync
  const onOneTimeSync = async () => {
    setSyncing(true)
    setSyncState(null)
    try {
      const payload = {
        ...buildConnectionPayload(),
        query: 'active=true^ORactive=false',
        // Import workbooks can contain tens of thousands of historical tickets.
        // The backend paginates and LanceDB upserts by incident, so request the
        // complete incident baseline instead of silently stopping at 5,000.
        limit: 100000,
        batch_size: 500,
      }
      const { data: jobData } = await snOneTimeSync(payload)
      const jobId = jobData.job_id

      let result = null
      for (let attempt = 0; attempt < SYNC_STATUS_MAX_POLLS; attempt++) {
        const { data: job } = await snSyncJobStatus(jobId)
        if (job.status === 'done') {
          result = job.result || {}
          break
        }
        if (job.status === 'error') {
          throw new Error(job.error || 'Sync failed')
        }
        await sleep(SYNC_STATUS_POLL_DELAY_MS)
      }
      if (!result) {
        throw new Error('Sync status polling timed out')
      }

      setSyncState(result)
      setConnectionState('connected')
      await refreshSyncGate()
      toast.success(`Synced ${result.tickets_fetched || 0} tickets into vector DB.`)
    } catch (err) {
      toast.error(formatApiError(err, 'Sync failed'))
    } finally {
      setSyncing(false)
    }
  }

  // Function: onSendChat
  const onSendChat = async () => {
    const q = chatInput.trim()
    if (!q || chatLoading || chatRequestInFlightRef.current) return
    if (!canChat) {
      toast.error('Run one-time sync first, then use chat.')
      return
    }

    chatRequestInFlightRef.current = true
    setChatLoading(true)

    let sessionId = supportSessionId
    const userMsg = { id: Date.now(), role: 'human', content: q }
    const nextMessages = [...chatMessages, userMsg]

    try {
      if (!sessionId) {
        sessionId = await createSession(`Support: ${q}`, provider, 'support_sync')
        setSupportSessionId(sessionId)
      }

      const history = buildRecentChatHistory(chatMessages)

      setChatMessages(nextMessages)
      setChatInput('')

      const { data } = await queryAgentAsync({
        question: q,
        chat_history: history,
        llm_provider: provider,
      })

      const finalMessages = [
        ...nextMessages,
        {
          id: Date.now() + 1,
          role: 'assistant',
          content: data.answer || 'No response generated.',
          sources: data.sources || [],
          confidence: data.confidence,
          context_used: data.context_used,
          mode: data.mode || 'chat',
        },
      ]
      setChatMessages(finalMessages)
      updateSessionMessages(sessionId, finalMessages, `Support: ${q}`).catch(() => {})
    } catch (err) {
      const detail = formatApiError(err, 'Chat failed')
      toast.error(detail)
      const failedMessages = [
        ...nextMessages,
        {
          id: Date.now() + 1,
          role: 'assistant',
          content: `Could not answer from RAG context. Error: ${detail}`,
          sources: [],
          confidence: 0,
          context_used: false,
          mode: 'chat',
        },
      ]
      setChatMessages(failedMessages)
      updateSessionMessages(sessionId, failedMessages, `Support: ${q}`).catch(() => {})
    } finally {
      chatRequestInFlightRef.current = false
      setChatLoading(false)
    }
  }

  // Function: onClearSupportChat
  const onClearSupportChat = async () => {
    setChatMessages([])
    setSupportSessionId(null)
    setChatInput('')
  }

  // Function: onPollTicket
  const onPollTicket = async () => {
    if (pollingTicketInFlightRef.current) return
    if (!canChat) {
      toast.error('Reconnect and sync first, then poll a synthetic ticket.')
      return
    }

    pollingTicketInFlightRef.current = true
    setPollingTicket(true)
    try {
      const { data } = await pollSyntheticTicket({ llm_provider: provider })
      const generated = removeSuggestedSolution(data?.ticket_text || '')
      if (!generated) {
        toast.error('Synthetic ticket was empty. Please retry.')
        return
      }
      setChatInput(generated)
      toast.success('Synthetic ticket generated by Ollama and pasted into the input box.')
    } catch (err) {
      toast.error(formatApiError(err, 'Could not generate synthetic ticket'))
    } finally {
      pollingTicketInFlightRef.current = false
      setPollingTicket(false)
    }
  }

  // Function: onRunFeatureFromChat
  const onRunFeatureFromChat = async () => {
    if (featureRequestInFlightRef.current || featureLoading) return
    if (!canChat) {
      toast.error('Run one-time sync first.')
      return
    }

    const tickets = extractTicketsFromChat()
    if (tickets.length === 0) {
      toast.error('Use chat with at least one incident number like INC12345, then run this action.')
      return
    }

    const transcript = buildFeatureTranscript(chatMessages, tickets)

    const latestUserMessage = [...chatMessages]
      .reverse()
      .find((m) => m.role === 'human' && m.content?.trim())
    const shortDescription = compactMessageContent(
      latestUserMessage?.content || FEATURE_PROMPTS[activeFeature] || `Feature ${activeFeature} for ${tickets.join(', ')}`,
      240,
    )
    const description = transcript || `Analyze incidents ${tickets.join(', ')} using indexed ServiceNow vector context.`

    featureRequestInFlightRef.current = true
    setFeatureLoading(true)
    setFeatureResult(null)
    try {
      const { data } = await iwRunFeatureAsync({
        feature: activeFeature,
        incident_number: tickets[0],
        short_description: shortDescription,
        description,
        priority: null,
        category: null,
        additional_context: `Target tickets from chat: ${tickets.join(', ')}`,
        llm_provider: provider,
      })
      setFeatureResult({
        feature: activeFeature,
        tickets,
        answer: data.answer || 'No response generated.',
        sources: data.sources || [],
        confidence: data.confidence || 0,
        context_used: Boolean(data.context_used),
        mode: data.mode || 'analysis',
        similar_incidents: data.similar_incidents || [],
      })
    } catch (err) {
      toast.error(formatApiError(err, 'Feature execution failed'))
    } finally {
      featureRequestInFlightRef.current = false
      setFeatureLoading(false)
    }
  }

  // Function: onDisconnect
  const onDisconnect = async () => {
    setConnectionState('disconnected')
    setConn((prev) => ({ ...prev, username: '', password: '' }))
    setChatMessages([])
    setChatInput('')
    setFeatureResult(null)
    await refreshSyncGate()
    toast.success('Disconnected from ServiceNow session.')
  }

  // Function: handleDashboardSelectIncident
  const handleDashboardSelectIncident = (incident) => {
    const chatMessage = `Ask about synced incidents: ${incident.number}\n\nIncident Number: ${incident.number}\nShort Description: ${incident.short_description}\nDescription: ${incident.description}\nCategory: ${incident.category || 'N/A'}\nState: ${incident.state || 'N/A'}\nPriority: ${incident.priority || 'N/A'}\nAssigned To: ${incident.assigned_to || 'N/A'}`
    setChatInput(chatMessage)
    toast.success(`Loaded incident ${incident.number} into chat input`)
  }

  return (
    <div className="flex h-full flex-col overflow-hidden bg-[#faf9f8] text-slate-900">
      <div className="space-y-4 overflow-y-auto p-4 md:p-6">
        <section className="rounded-sm border border-[#edebe9] bg-white p-4">
          <div className="mb-3 flex flex-wrap items-center justify-between gap-3">
            <div className="flex items-center gap-2 text-sm">
              <Server size={15} style={{ color: '#0078d4' }} />
              <span>ServiceNow / JIRA Connection (One-time)</span>
              <span
                className="rounded-full px-2 py-0.5 text-[10px] font-semibold"
                style={connectionState === 'connected' ? { background: '#dff6dd', color: '#107c10' } : { background: '#fdf3f4', color: '#a4262c' }}
              >
                {connectionState === 'connected' ? 'Connected - SERVICENOW' : 'Disconnected'}
              </span>
            </div>
            <div className="text-xs text-slate-400">Sync is optional while vector DB is fresh (&lt;168h). Older than 168h requires sync.</div>
          </div>

          {(serverCredentials.basic || serverCredentials.oauth) && (
            <label className="mb-3 flex items-center gap-2 text-xs text-slate-600">
              <input
                type="checkbox"
                checked={useServerCredentials}
                onChange={(e) => setUseServerCredentials(e.target.checked)}
              />
              Use preconfigured server credentials
            </label>
          )}

          <div className="grid gap-3 md:grid-cols-3">
            <div>
              <label className="mb-1 block text-[10px] uppercase tracking-wide text-slate-400">Provider</label>
              <select
                className="w-full rounded-md border border-[#c8c6c4] bg-white text-slate-900 px-3 py-2 text-sm"
                value={conn.provider}
                onChange={(e) => setConn((p) => ({ ...p, provider: e.target.value }))}
              >
                <option>ServiceNow</option>
              </select>
            </div>
            <div>
              <label className="mb-1 block text-[10px] uppercase tracking-wide text-slate-400">Auth Type</label>
              <select
                className="w-full rounded-md border border-[#c8c6c4] bg-white text-slate-900 px-3 py-2 text-sm"
                value={conn.auth_type}
                onChange={(e) => setConn((p) => ({ ...p, auth_type: e.target.value }))}
              >
                <option value="basic">Basic (username + password/token)</option>
                <option value="oauth">OAuth (password grant + Client ID/Secret)</option>
              </select>
            </div>
            <div>
              <label className="mb-1 block text-[10px] uppercase tracking-wide text-slate-400">Instance Base URL</label>
              <input
                className="w-full rounded-md border border-[#c8c6c4] bg-white text-slate-900 px-3 py-2 text-sm"
                placeholder="https://dev12345.service-now.com"
                value={conn.base_url}
                onChange={(e) => setConn((p) => ({ ...p, base_url: e.target.value }))}
              />
            </div>
            <div>
              <label className="mb-1 block text-[10px] uppercase tracking-wide text-slate-400">Username / Email</label>
              <input
                className="w-full rounded-md border border-[#c8c6c4] bg-white text-slate-900 px-3 py-2 text-sm"
                placeholder="admin"
                value={conn.username}
                onChange={(e) => setConn((p) => ({ ...p, username: e.target.value }))}
              />
            </div>
            <div className={conn.auth_type === 'oauth' ? '' : 'md:col-span-2'}>
              <label className="mb-1 block text-[10px] uppercase tracking-wide text-slate-400">Password / API Token</label>
              <input
                type="password"
                autoComplete="new-password"
                disabled={useServerCredentials && (conn.auth_type === 'oauth' ? serverCredentials.oauth : serverCredentials.basic)}
                className="w-full rounded-md border border-[#c8c6c4] bg-white text-slate-900 px-3 py-2 text-sm"
                placeholder="Leave blank to use the server-configured password"
                value={conn.password}
                onChange={(e) => setConn((p) => ({ ...p, password: e.target.value }))}
              />
            </div>
            {conn.auth_type === 'oauth' && (
              <>
                <div>
                  <label className="mb-1 block text-[10px] uppercase tracking-wide text-slate-400">OAuth Client ID</label>
                  <input
                    className="w-full rounded-md border border-[#c8c6c4] bg-white text-slate-900 px-3 py-2 text-sm"
                    placeholder="From System OAuth &gt; Application Registry"
                    value={conn.client_id}
                    onChange={(e) => setConn((p) => ({ ...p, client_id: e.target.value }))}
                  />
                </div>
                <div className="md:col-span-2">
                  <label className="mb-1 block text-[10px] uppercase tracking-wide text-slate-400">OAuth Client Secret</label>
                  <input
                    type="password"
                    autoComplete="new-password"
                    disabled={useServerCredentials && serverCredentials.oauth}
                    className="w-full rounded-md border border-[#c8c6c4] bg-white text-slate-900 px-3 py-2 text-sm"
                    placeholder="Leave blank to use the server-configured client secret"
                    value={conn.client_secret}
                    onChange={(e) => setConn((p) => ({ ...p, client_secret: e.target.value }))}
                  />
                </div>
              </>
            )}
          </div>

          <div className="mt-4 flex flex-wrap items-center justify-end gap-2">
            <button
              type="button"
              onClick={onTestConnection}
              disabled={testing}
              className="inline-flex items-center gap-1 rounded-md border px-3 py-1.5 text-xs font-medium disabled:opacity-50"
              style={{ borderColor: '#9fd89c', background: '#dff6dd', color: '#107c10' }}
            >
              {testing ? <Loader2 size={14} className="animate-spin" /> : <CheckCircle2 size={14} />}
              {connectionState === 'connected' ? 'Reconnect' : 'Connect'}
            </button>
            <button
              type="button"
              onClick={onOneTimeSync}
              disabled={syncing}
              className="inline-flex items-center gap-1 rounded-md border px-3 py-1.5 text-xs font-medium disabled:opacity-50"
              style={{ borderColor: '#c7e0f4', background: '#eff6fc', color: '#0078d4' }}
            >
              {syncing ? <Loader2 size={14} className="animate-spin" /> : <Database size={14} />}
              One-time Sync Tickets
            </button>
            <button
              type="button"
              onClick={onDisconnect}
              className="inline-flex items-center gap-1 rounded-md border border-[#c8c6c4] bg-white px-3 py-1.5 text-xs font-medium text-slate-700"
            >
              <Unplug size={14} /> Disconnect
            </button>
          </div>

          <SyncStatusBanner syncState={syncState} syncGate={syncGate} />
        </section>

        <EmbeddedDashboard 
          onSelectIncident={handleDashboardSelectIncident}
          isConnected={isConnected}
        />

        <section className="support-chat-window rounded-sm border border-[#edebe9] bg-white p-4">
          <div className="mb-3 flex items-center justify-between gap-3">
            <div>
              <p className="text-sm font-semibold text-slate-900">Support Chat (Post Sync)</p>
              <p className="text-xs text-slate-400">
                Ask questions against the synced ServiceNow incidents in vector DB.
              </p>
            </div>
            <span
              className="rounded-full px-2 py-0.5 text-[10px] font-semibold"
              style={canChat ? { background: '#dff6dd', color: '#107c10' } : { background: '#fff4ce', color: '#ca5010' }}
            >
              {CHAT_BADGE_LABELS[chatAvailability]}
            </span>
          </div>

          <div className="rounded-sm border border-[#edebe9] bg-[#faf9f8] p-3">
            <ChatMessagesPanel messages={chatMessages} chatAvailability={chatAvailability} bottomRef={chatBottomRef} />

            <div className="flex gap-2">
              <textarea
                ref={chatInputRef}
                rows={1}
                className="w-full resize-y overflow-y-auto rounded-md border border-[#c8c6c4] bg-white text-slate-900 px-3 py-2 text-sm leading-relaxed [overflow-wrap:anywhere]"
                placeholder={CHAT_PLACEHOLDER_LABELS[chatAvailability]}
                value={chatInput}
                onChange={(e) => setChatInput(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === 'Enter' && !e.shiftKey) {
                    e.preventDefault()
                    onSendChat()
                  }
                }}
                disabled={!canChat || chatLoading || pollingTicket}
              />
              <button
                onClick={onSendChat}
                disabled={!canChat || chatLoading || pollingTicket || !chatInput.trim()}
                className="inline-flex items-center gap-1 rounded-md bg-[#0078d4] px-3 py-2 text-xs font-semibold text-white hover:bg-[#005a9e] disabled:cursor-not-allowed disabled:opacity-50"
              >
                {chatLoading ? <Loader2 size={13} className="animate-spin" /> : <Send size={13} />}
                Send
              </button>
              <button
                onClick={onClearSupportChat}
                className="inline-flex items-center gap-1 rounded-md border border-[#c8c6c4] px-3 py-2 text-xs text-slate-700"
              >
                Clear
              </button>
            </div>
          </div>
        </section>

        <section className="rounded-sm border border-[#edebe9] bg-white">
          <div className="flex flex-wrap gap-1 border-b border-[#edebe9] p-2">
            {FEATURE_TABS.map((tab) => (
              <button
                key={tab.id}
                onClick={() => { setActiveFeature(tab.id); setFeatureResult(null); }}
                className={`rounded-md px-3 py-2 text-xs font-medium transition ${
                  activeFeature === tab.id
                    ? 'bg-[#eff6fc] text-[#0078d4]'
                    : 'text-slate-600 hover:bg-slate-100'
                }`}
              >
                {tab.label}
              </button>
            ))}
          </div>

          <div className="space-y-3 p-4">
            <div className="flex flex-wrap items-center justify-between gap-2">
              <p className="text-xs text-slate-400">
                Runs on ticket numbers detected from chat and grounded vector DB results.
              </p>
              <button
                onClick={onRunFeatureFromChat}
                disabled={featureLoading || !canChat}
                className="inline-flex items-center gap-2 rounded-md bg-[#0078d4] px-3 py-2 text-xs font-semibold text-white hover:bg-[#005a9e] disabled:cursor-not-allowed disabled:opacity-50"
              >
                {featureLoading ? <Loader2 size={13} className="animate-spin" /> : null}
                Run {FEATURE_TABS.find((f) => f.id === activeFeature)?.label}
              </button>
            </div>

            <FeatureResultPanel featureResult={featureResult} />
          </div>
        </section>
      </div>
    </div>
  )
}
