import {
  AlertTriangle,
  ChevronDown,
  Database,
  Loader2,
  LogOut,
  Plus,
  Send,
  Table2,
  User,
} from "lucide-react";
import { FormEvent, useCallback, useEffect, useMemo, useRef, useState } from "react";
import { ApiClient, ApiError } from "./api";
import type {
  ApiConversationMessage,
  ChatMessage,
  Conversation,
  ConversationMessageResponse,
  DataRow,
  StoredSession,
} from "./types";
import { createAssistantViewModel, formatCellValue } from "./viewModel";

const SESSION_KEY = "nl2sql.session";
const REQUEST_DEFAULTS = {
  timeout_ms: 10_000,
  max_limit: 1_000,
  max_validation_attempts: 2,
  memory_history_limit: 8,
};

export function App() {
  const [session, setSessionState] = useState<StoredSession | null>(() => readSession());
  const sessionRef = useRef<StoredSession | null>(session);
  const [healthOk, setHealthOk] = useState(false);
  const [conversations, setConversations] = useState<Conversation[]>([]);
  const [activeConversationId, setActiveConversationId] = useState("");
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [execute, setExecute] = useState(false);
  const [input, setInput] = useState("");
  const [loading, setLoading] = useState(false);
  const [booting, setBooting] = useState(Boolean(session));
  const [error, setError] = useState("");
  const [accountOpen, setAccountOpen] = useState(false);

  const commitSession = useCallback((nextSession: StoredSession | null) => {
    sessionRef.current = nextSession;
    setSessionState(nextSession);
    if (nextSession) {
      localStorage.setItem(SESSION_KEY, JSON.stringify(nextSession));
    } else {
      localStorage.removeItem(SESSION_KEY);
    }
  }, []);

  const clearSession = useCallback(() => {
    commitSession(null);
    setConversations([]);
    setActiveConversationId("");
    setMessages([]);
  }, [commitSession]);

  const api = useMemo(
    () =>
      new ApiClient({
        getSession: () => sessionRef.current,
        setSession: commitSession,
        clearSession,
      }),
    [clearSession, commitSession],
  );

  const loadConversations = useCallback(async () => {
    const payload = await api.listConversations();
    setConversations(payload.items);
    return payload.items;
  }, [api]);

  const loadMessages = useCallback(
    async (conversationId: string) => {
      const payload = await api.listMessages(conversationId);
      setMessages(payload.items.map(toChatMessage));
    },
    [api],
  );

  const bootstrap = useCallback(async () => {
    setBooting(true);
    setError("");
    try {
      const [health] = await Promise.all([api.health(), sessionRef.current ? api.me() : null]);
      setHealthOk(Boolean(health.ok));
      if (sessionRef.current) {
        const items = await loadConversations();
        if (items[0]) {
          setActiveConversationId(items[0].conversation_id);
          await loadMessages(items[0].conversation_id);
        }
      }
    } catch (err) {
      handleAuthOrError(err, clearSession, setError);
    } finally {
      setBooting(false);
    }
  }, [api, clearSession, loadConversations, loadMessages]);

  useEffect(() => {
    void bootstrap();
  }, [bootstrap]);

  async function handleLogin(payload: { tenant_id: string; username: string; password: string }) {
    setLoading(true);
    setError("");
    try {
      const nextSession = await api.login(payload);
      commitSession(nextSession);
      const [health, conversationList] = await Promise.all([
        api.health(),
        api.listConversations(),
      ]);
      setHealthOk(Boolean(health.ok));
      setConversations(conversationList.items);
      if (conversationList.items[0]) {
        setActiveConversationId(conversationList.items[0].conversation_id);
        await loadMessages(conversationList.items[0].conversation_id);
      }
    } catch (err) {
      handleAuthOrError(err, clearSession, setError);
    } finally {
      setLoading(false);
    }
  }

  async function handleSelectConversation(conversationId: string) {
    setActiveConversationId(conversationId);
    setError("");
    try {
      await loadMessages(conversationId);
    } catch (err) {
      handleAuthOrError(err, clearSession, setError);
    }
  }

  async function handleNewConversation() {
    setLoading(true);
    setError("");
    try {
      const conversation = await api.createConversation("New analysis");
      setConversations((items) => [conversation, ...items]);
      setActiveConversationId(conversation.conversation_id);
      setMessages([]);
    } catch (err) {
      handleAuthOrError(err, clearSession, setError);
    } finally {
      setLoading(false);
    }
  }

  async function handleSend() {
    const question = input.trim();
    if (!question || loading) {
      return;
    }

    setInput("");
    setLoading(true);
    setError("");

    const userMessage: ChatMessage = {
      id: `local-user-${Date.now()}`,
      role: "user",
      content: question,
      metadata: {},
    };
    setMessages((items) => [...items, userMessage]);

    try {
      let conversationId = activeConversationId;
      if (!conversationId) {
        const conversation = await api.createConversation(question.slice(0, 54));
        conversationId = conversation.conversation_id;
        setActiveConversationId(conversationId);
        setConversations((items) => [conversation, ...items]);
      }

      const response = await api.sendMessage(conversationId, {
        question,
        execute,
        ...REQUEST_DEFAULTS,
      });
      setMessages((items) => [...items, responseToAssistantMessage(response)]);
      const refreshed = await loadConversations();
      setConversations(refreshed);
    } catch (err) {
      handleAuthOrError(err, clearSession, setError);
      setMessages((items) => [
        ...items,
        {
          id: `local-assistant-error-${Date.now()}`,
          role: "assistant",
          content: "Request failed.",
          metadata: {
            ok: false,
            error: err instanceof Error ? err.message : "Request failed",
            rows: [],
            trace: [],
          },
        },
      ]);
    } finally {
      setLoading(false);
    }
  }

  async function handleLogout() {
    const refreshToken = sessionRef.current?.refresh_token;
    clearSession();
    if (refreshToken) {
      try {
        await api.logout(refreshToken);
      } catch {
        return;
      }
    }
  }

  if (!session) {
    return <LoginView loading={loading} error={error} onLogin={handleLogin} />;
  }

  return (
    <div className="app-shell">
      <aside className="rail">
        <div className="brand">
          <div className="mark">AI</div>
          <div>
            <div className="h2">Data Assistant</div>
            <div className="small muted">Conversational BI</div>
          </div>
        </div>

        <button className="new-chat" onClick={handleNewConversation} disabled={loading}>
          <Plus size={16} />
          New chat
        </button>

        <div className="conversation-list">
          {conversations.map((conversation) => (
            <button
              key={conversation.conversation_id}
              className={`prompt-card ${
                conversation.conversation_id === activeConversationId ? "active" : ""
              }`}
              onClick={() => void handleSelectConversation(conversation.conversation_id)}
            >
              {conversation.title || "Untitled analysis"}
            </button>
          ))}
          {!conversations.length && <div className="empty-list">No conversations</div>}
        </div>

        <div className="context-card card">
          <div className="label">Session context</div>
          <div className="context-strong">tenant_id {session.user.tenant_id}</div>
          <div className="muted small">user_id {session.user.user_id}</div>
        </div>
      </aside>

      <main className="content">
        <header className="chat-head card">
          <div>
            <div className="h2">Ask your warehouse in plain English</div>
            <div className="small muted">Memory-aware NL2SQL conversation</div>
          </div>
          <div className="head-actions">
            <span className={`pill ${healthOk ? "success" : "warning"}`}>
              {healthOk ? "health ok" : "health down"}
            </span>
            <label className="switch-pill">
              <input
                type="checkbox"
                checked={execute}
                onChange={(event) => setExecute(event.target.checked)}
              />
              <span>{execute ? "execute on" : "execute off"}</span>
            </label>
            <div className="account-wrap">
              <button className="user-menu" onClick={() => setAccountOpen((value) => !value)}>
                <div className="avatar">{initials(session.user.username)}</div>
                <div className="user-meta">
                  <strong>{session.user.username}</strong>
                  <span className="small muted">Profile & settings</span>
                </div>
                <ChevronDown size={16} />
              </button>
              {accountOpen && (
                <div className="account-flyout">
                  <div className="account-profile">
                    <div className="avatar">{initials(session.user.username)}</div>
                    <div>
                      <div className="account-name">{session.user.username}</div>
                      <div className="small muted">
                        {session.user.user_id} / {session.user.tenant_id}
                      </div>
                    </div>
                  </div>
                  <div className="account-row active">
                    <User size={15} />
                    <span>Personal profile</span>
                  </div>
                  <button className="account-row button-row" onClick={() => void handleLogout()}>
                    <LogOut size={15} />
                    <span>Sign out</span>
                  </button>
                </div>
              )}
            </div>
          </div>
        </header>

        {error && (
          <div className="error-banner">
            <AlertTriangle size={16} />
            {error}
          </div>
        )}

        <section className="thread card" aria-live="polite">
          {booting ? (
            <div className="loading-state">
              <Loader2 className="spin" size={20} />
              Loading
            </div>
          ) : (
            <>
              {messages.map((message) =>
                message.role === "user" ? (
                  <UserBubble key={message.id} message={message} />
                ) : (
                  <AssistantBubble key={message.id} message={message} />
                ),
              )}
              {!messages.length && (
                <div className="empty-thread">
                  <Database size={22} />
                  <span>Start with a warehouse question.</span>
                </div>
              )}
            </>
          )}
        </section>

        <form
          className="composer card"
          onSubmit={(event) => {
            event.preventDefault();
            void handleSend();
          }}
        >
          <input
            className="composer-input"
            value={input}
            onChange={(event) => setInput(event.target.value)}
            placeholder="Ask a follow-up question..."
            disabled={loading}
          />
          <span className="pill amber">memory {REQUEST_DEFAULTS.memory_history_limit}</span>
          <span className="pill indigo">max_limit {REQUEST_DEFAULTS.max_limit}</span>
          <button className="btn" type="submit" disabled={loading || !input.trim()}>
            {loading ? <Loader2 className="spin" size={16} /> : <Send size={16} />}
            Send
          </button>
        </form>
      </main>
    </div>
  );
}

function LoginView({
  loading,
  error,
  onLogin,
}: {
  loading: boolean;
  error: string;
  onLogin: (payload: { tenant_id: string; username: string; password: string }) => Promise<void>;
}) {
  const [tenantId, setTenantId] = useState("demo");
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");

  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    void onLogin({ tenant_id: tenantId, username, password });
  }

  return (
    <main className="login-screen">
      <form className="login-card card" onSubmit={submit}>
        <div className="brand login-brand">
          <div className="mark">AI</div>
          <div>
            <div className="h2">Data Assistant</div>
            <div className="small muted">NL2SQL workspace</div>
          </div>
        </div>
        {error && <div className="login-error">{error}</div>}
        <label className="field">
          <span>Tenant</span>
          <input value={tenantId} onChange={(event) => setTenantId(event.target.value)} />
        </label>
        <label className="field">
          <span>Username</span>
          <input value={username} onChange={(event) => setUsername(event.target.value)} />
        </label>
        <label className="field">
          <span>Password</span>
          <input
            type="password"
            value={password}
            onChange={(event) => setPassword(event.target.value)}
          />
        </label>
        <button className="btn login-button" type="submit" disabled={loading}>
          {loading ? <Loader2 className="spin" size={16} /> : null}
          Sign in
        </button>
      </form>
    </main>
  );
}

function UserBubble({ message }: { message: ChatMessage }) {
  return (
    <div className="message user">
      <div className="avatar">U</div>
      <div className="bubble">{message.content}</div>
    </div>
  );
}

function AssistantBubble({ message }: { message: ChatMessage }) {
  const viewModel = createAssistantViewModel(message);

  return (
    <div className="message ai">
      <div className="avatar">AI</div>
      <div className="bubble assistant-bubble">
        <strong>Answer</strong>
        <p>{viewModel.answer || viewModel.status}</p>
        <div className="assistant-grid">
          <div className="mini-card">
            <div className="label table-label">
              <Table2 size={14} />
              Rows
            </div>
            <RowsTable rows={viewModel.rows} />
          </div>
        </div>
        <div className="pill-row">
          <span className="pill neutral">{viewModel.intentLabel}</span>
          <span className="pill neutral">
            trace {viewModel.trace.length ? "collapsed" : "empty"}
          </span>
          <span className={`pill ${viewModel.status === "error" ? "danger" : "success"}`}>
            {viewModel.status}
          </span>
        </div>
      </div>
    </div>
  );
}

function RowsTable({ rows }: { rows: DataRow[] }) {
  if (!rows.length) {
    return <div className="no-rows">No rows returned</div>;
  }

  const columns = Array.from(new Set(rows.flatMap((row) => Object.keys(row))));

  return (
    <table>
      <thead>
        <tr>
          {columns.map((column) => (
            <th key={column}>{column}</th>
          ))}
        </tr>
      </thead>
      <tbody>
        {rows.slice(0, 20).map((row, rowIndex) => (
          <tr key={rowIndex}>
            {columns.map((column) => (
              <td key={column}>{formatCellValue(row[column])}</td>
            ))}
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function responseToAssistantMessage(response: ConversationMessageResponse): ChatMessage {
  return {
    id: `assistant-${response.conversation_id}-${Date.now()}`,
    role: "assistant",
    content: response.answer || response.error || "No answer returned.",
    metadata: {
      answer: response.answer,
      rows: response.rows,
      ok: response.ok,
      error: response.error,
      trace: response.trace,
      intent: response.intent,
      sql: response.sql,
    },
  };
}

function toChatMessage(message: ApiConversationMessage, index: number): ChatMessage {
  return {
    ...message,
    id: `${message.role}-${index}-${message.content.slice(0, 16)}`,
  };
}

function readSession(): StoredSession | null {
  const raw = localStorage.getItem(SESSION_KEY);
  if (!raw) {
    return null;
  }
  try {
    return JSON.parse(raw) as StoredSession;
  } catch {
    localStorage.removeItem(SESSION_KEY);
    return null;
  }
}

function handleAuthOrError(
  err: unknown,
  clearSession: () => void,
  setError: (message: string) => void,
) {
  if (err instanceof ApiError && err.status === 401) {
    clearSession();
  }
  setError(err instanceof Error ? err.message : "Request failed");
}

function initials(username: string): string {
  return username
    .split(/\s+/)
    .filter(Boolean)
    .slice(0, 2)
    .map((part) => part[0]?.toUpperCase())
    .join("") || "U";
}
