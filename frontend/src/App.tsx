import {
  AlertTriangle,
  BookOpen,
  ChevronDown,
  Loader2,
  LogOut,
  MessageSquare,
  MoreHorizontal,
  PanelLeftClose,
  PanelLeftOpen,
  Pencil,
  Plus,
  Search,
  Send,
  SquarePen,
  Table2,
  Trash2,
  User,
} from "lucide-react";
import {
  FormEvent,
  KeyboardEvent,
  MouseEvent,
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import { ApiClient, ApiError } from "./api";
import type {
  AgentMode,
  ApiConversationMessage,
  ChatMessage,
  Conversation,
  ConversationMessageResponse,
  DataRow,
  StoredSession,
} from "./types";
import {
  applyConversationUpdate,
  removeConversationFromList,
  resolveConversationDeletionState,
} from "./conversationState";
import { parseMarkdown, type MarkdownSegment } from "./markdown";
import { getNewChatButtonClassName } from "./navigationState";
import { createSendMessagePayload } from "./requestPayload";
import { paginateRows } from "./tablePagination";
import {
  createAssistantViewModel,
  formatCellValueForColumn,
  formatColumnLabel,
  getColumnClassName,
} from "./viewModel";

const SESSION_KEY = "nl2sql.session";
const SIDEBAR_KEY = "nl2sql.sidebarCollapsed";

export function App() {
  const [session, setSessionState] = useState<StoredSession | null>(() => readSession());
  const sessionRef = useRef<StoredSession | null>(session);
  const [healthOk, setHealthOk] = useState(false);
  const [conversations, setConversations] = useState<Conversation[]>([]);
  const [activeConversationId, setActiveConversationId] = useState("");
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [input, setInput] = useState("");
  const [mode, setMode] = useState<AgentMode>("execute");
  const [loading, setLoading] = useState(false);
  const [booting, setBooting] = useState(Boolean(session));
  const [error, setError] = useState("");
  const [accountOpen, setAccountOpen] = useState(false);
  const [sidebarCollapsed, setSidebarCollapsed] = useState(
    () => localStorage.getItem(SIDEBAR_KEY) === "true",
  );
  const [conversationMenuId, setConversationMenuId] = useState("");
  const [renamingConversationId, setRenamingConversationId] = useState("");
  const [renameDraft, setRenameDraft] = useState("");
  const [conversationActionId, setConversationActionId] = useState("");
  const threadEndRef = useRef<HTMLDivElement | null>(null);
  const skipRenameBlurRef = useRef(false);
  const renameSavingRef = useRef(false);

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

  useEffect(() => {
    localStorage.setItem(SIDEBAR_KEY, sidebarCollapsed ? "true" : "false");
  }, [sidebarCollapsed]);

  useEffect(() => {
    if (!conversationMenuId) {
      return undefined;
    }

    function closeMenu() {
      setConversationMenuId("");
    }

    function closeOnEscape(event: globalThis.KeyboardEvent) {
      if (event.key === "Escape") {
        closeMenu();
      }
    }

    document.addEventListener("click", closeMenu);
    document.addEventListener("keydown", closeOnEscape);
    return () => {
      document.removeEventListener("click", closeMenu);
      document.removeEventListener("keydown", closeOnEscape);
    };
  }, [conversationMenuId]);

  useEffect(() => {
    threadEndRef.current?.scrollIntoView({ behavior: "smooth", block: "end" });
  }, [messages, loading, booting]);

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

  function handleConversationMenuToggle(
    event: MouseEvent<HTMLButtonElement>,
    conversationId: string,
  ) {
    event.stopPropagation();
    setConversationMenuId((current) => (current === conversationId ? "" : conversationId));
  }

  function beginRename(event: MouseEvent<HTMLButtonElement>, conversation: Conversation) {
    event.stopPropagation();
    skipRenameBlurRef.current = false;
    renameSavingRef.current = false;
    setConversationMenuId("");
    setRenamingConversationId(conversation.conversation_id);
    setRenameDraft(conversation.title || "Untitled analysis");
  }

  function cancelRename() {
    skipRenameBlurRef.current = true;
    setRenamingConversationId("");
    setRenameDraft("");
  }

  async function commitRename(conversation: Conversation) {
    if (renameSavingRef.current) {
      return;
    }

    const title = renameDraft.trim();
    if (!title || title === conversation.title) {
      cancelRename();
      return;
    }

    renameSavingRef.current = true;
    setConversationActionId(conversation.conversation_id);
    setError("");
    try {
      const updated = await api.updateConversation(conversation.conversation_id, { title });
      setConversations((items) => applyConversationUpdate(items, updated));
      cancelRename();
    } catch (err) {
      handleAuthOrError(err, clearSession, setError);
    } finally {
      renameSavingRef.current = false;
      setConversationActionId("");
    }
  }

  function handleRenameBlur(conversation: Conversation) {
    if (skipRenameBlurRef.current) {
      skipRenameBlurRef.current = false;
      return;
    }
    void commitRename(conversation);
  }

  function handleRenameKeyDown(
    event: KeyboardEvent<HTMLInputElement>,
    conversation: Conversation,
  ) {
    if (event.key === "Enter") {
      event.preventDefault();
      void commitRename(conversation);
    }
    if (event.key === "Escape") {
      event.preventDefault();
      cancelRename();
    }
  }

  async function handleDeleteConversation(conversationId: string) {
    setConversationMenuId("");
    setConversationActionId(conversationId);
    setError("");
    try {
      await api.updateConversation(conversationId, { archived: true });
      setConversations((items) => removeConversationFromList(items, conversationId));
      const nextState = resolveConversationDeletionState({
        activeConversationId,
        deletedConversationId: conversationId,
        messages,
      });
      setActiveConversationId(nextState.activeConversationId);
      setMessages(nextState.messages);
      if (renamingConversationId === conversationId) {
        cancelRename();
      }
    } catch (err) {
      handleAuthOrError(err, clearSession, setError);
    } finally {
      setConversationActionId("");
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
    const thinkingMessage: ChatMessage = {
      id: `local-assistant-thinking-${Date.now()}`,
      role: "assistant",
      content: "",
      metadata: {
        message_type: "thinking",
      },
    };
    setMessages((items) => [...items, userMessage, thinkingMessage]);

    try {
      let conversationId = activeConversationId;
      if (!conversationId) {
        const conversation = await api.createConversation(question.slice(0, 54));
        conversationId = conversation.conversation_id;
        setActiveConversationId(conversationId);
        setConversations((items) => [conversation, ...items]);
      }

      const response = await api.sendMessage(
        conversationId,
        createSendMessagePayload(question, mode),
      );
      setMessages((items) =>
        items.map((item) =>
          item.id === thinkingMessage.id ? responseToAssistantMessage(response, item.id) : item,
        ),
      );
      const refreshed = await loadConversations();
      setConversations(refreshed);
    } catch (err) {
      handleAuthOrError(err, clearSession, setError);
      setMessages((items) =>
        items.map((item) =>
          item.id === thinkingMessage.id
            ? {
                id: `local-assistant-error-${Date.now()}`,
                role: "assistant",
                content: "Request failed.",
                metadata: {
                  message_type: "error",
                  ok: false,
                  error: {
                    code: "REQUEST_FAILED",
                    message: err instanceof Error ? err.message : "Request failed",
                    retryable: false,
                  },
                  rows: [],
                  trace: [],
                },
              }
            : item,
        ),
      );
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
    <div className={`app-shell ${sidebarCollapsed ? "sidebar-collapsed" : ""}`}>
      <aside className={`rail ${sidebarCollapsed ? "collapsed" : ""}`}>
        <div className="sidebar-header">
          <div className="sidebar-title">Data Assistant</div>
          <button
            className="icon-button"
            type="button"
            onClick={() => setSidebarCollapsed(true)}
            aria-label="Collapse sidebar"
            title="Collapse sidebar"
          >
            <PanelLeftClose size={18} />
          </button>
        </div>

        <nav className="sidebar-nav" aria-label="Primary">
          <button
            className={getNewChatButtonClassName(activeConversationId)}
            onClick={handleNewConversation}
            disabled={loading}
          >
            <SquarePen size={17} />
            <span>新聊天</span>
          </button>
          <button className="sidebar-nav-item" type="button">
            <Search size={17} />
            <span>搜索聊天</span>
          </button>
          <button className="sidebar-nav-item" type="button">
            <BookOpen size={17} />
            <span>文件库</span>
          </button>
        </nav>

        <div className="conversation-list" aria-label="Conversation history">
          <div className="sidebar-section-label">聊天</div>
          {conversations.map((conversation) => {
            const title = conversation.title || "Untitled analysis";
            const isActive = conversation.conversation_id === activeConversationId;
            const isRenaming = conversation.conversation_id === renamingConversationId;
            const isBusy = conversation.conversation_id === conversationActionId;

            return (
              <div
                key={conversation.conversation_id}
                className={`conversation-row ${isActive ? "active" : ""} ${
                  isRenaming ? "renaming" : ""
                }`}
              >
                {isRenaming ? (
                  <div className="conversation-rename">
                    <MessageSquare size={15} />
                    <input
                      value={renameDraft}
                      autoFocus
                      disabled={isBusy}
                      onClick={(event) => event.stopPropagation()}
                      onFocus={(event) => event.currentTarget.select()}
                      onChange={(event) => setRenameDraft(event.target.value)}
                      onBlur={() => handleRenameBlur(conversation)}
                      onKeyDown={(event) => handleRenameKeyDown(event, conversation)}
                      aria-label="Rename conversation"
                    />
                  </div>
                ) : (
                  <>
                    <button
                      className="prompt-card"
                      type="button"
                      onClick={() => void handleSelectConversation(conversation.conversation_id)}
                      title={title}
                    >
                      <MessageSquare size={15} />
                      <span>{title}</span>
                    </button>
                    <button
                      className="conversation-more"
                      type="button"
                      disabled={isBusy}
                      onClick={(event) =>
                        handleConversationMenuToggle(event, conversation.conversation_id)
                      }
                      aria-label={`More actions for ${title}`}
                      aria-expanded={conversationMenuId === conversation.conversation_id}
                    >
                      <MoreHorizontal size={16} />
                    </button>
                    {conversationMenuId === conversation.conversation_id && (
                      <div
                        className="conversation-actions-menu"
                        role="menu"
                        onClick={(event) => event.stopPropagation()}
                      >
                        <button
                          className="conversation-menu-item"
                          type="button"
                          role="menuitem"
                          onClick={(event) => beginRename(event, conversation)}
                        >
                          <Pencil size={15} />
                          <span>重命名</span>
                        </button>
                        <button
                          className="conversation-menu-item danger"
                          type="button"
                          role="menuitem"
                          disabled={isBusy}
                          onClick={(event) => {
                            event.stopPropagation();
                            void handleDeleteConversation(conversation.conversation_id);
                          }}
                        >
                          <Trash2 size={15} />
                          <span>删除</span>
                        </button>
                      </div>
                    )}
                  </>
                )}
              </div>
            );
          })}
          {!conversations.length && <div className="empty-list">暂无聊天</div>}
        </div>

        <div className="rail-footer">
          <button className="user-menu" onClick={() => setAccountOpen((value) => !value)}>
            <div className="avatar">{initials(session.user.username)}</div>
            <div className="user-meta">
              <strong>{session.user.username}</strong>
              <span>{session.user.tenant_id}</span>
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
      </aside>

      <main className="content">
        <header className="chat-topbar">
          {sidebarCollapsed && (
            <button
              className="icon-button"
              type="button"
              onClick={() => setSidebarCollapsed(false)}
              aria-label="Expand sidebar"
              title="Expand sidebar"
            >
              <PanelLeftOpen size={19} />
            </button>
          )}
          <span className={`health-dot ${healthOk ? "online" : "offline"}`} />
        </header>

        {error && (
          <div className="error-banner">
            <AlertTriangle size={16} />
            {error}
          </div>
        )}

        <section className={`thread ${messages.length ? "" : "empty"}`} aria-live="polite">
          <div className="thread-content">
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
                    <span>你好，{session.user.username}。</span>
                  </div>
                )}
                <div ref={threadEndRef} className="thread-end" />
              </>
            )}
          </div>
        </section>

        <div className="composer-shell">
          <form
            className="composer"
            onSubmit={(event) => {
              event.preventDefault();
              void handleSend();
            }}
          >
            <button className="composer-icon" type="button" aria-label="Add context" title="Add context">
              <Plus size={20} />
            </button>
            <select
              className="mode-select"
              value={mode}
              onChange={(event) => setMode(event.target.value as AgentMode)}
              disabled={loading}
              aria-label="Run mode"
            >
              <option value="plan">Plan</option>
              <option value="preview">Preview</option>
              <option value="execute">Execute</option>
            </select>
            <input
              className="composer-input"
              value={input}
              onChange={(event) => setInput(event.target.value)}
              placeholder="有问题，尽管问"
              disabled={loading}
            />
            <button className="send-button" type="submit" disabled={loading || !input.trim()}>
              {loading ? <Loader2 className="spin" size={18} /> : <Send size={18} />}
            </button>
          </form>
        </div>
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
      <div className="bubble">{message.content}</div>
    </div>
  );
}

function AssistantBubble({ message }: { message: ChatMessage }) {
  const viewModel = createAssistantViewModel(message);
  const answerText = viewModel.answer || (viewModel.showTable ? "" : viewModel.status);

  return (
    <div className="message ai">
      <div className="bubble assistant-bubble">
        {viewModel.isThinking ? (
          <div className="thinking">
            <span />
            <span />
            <span />
          </div>
        ) : (
          <>
            {viewModel.showTable && <div className="insight-label">数据洞察</div>}
            {answerText && <MarkdownAnswer text={answerText} />}
            {viewModel.error && (
              <div className="mini-card error-detail">
                <div className="label">{viewModel.error.code}</div>
                <div>{viewModel.error.message}</div>
              </div>
            )}
            {viewModel.logicalPlan && (
              <details className="mini-card artifact-card">
                <summary>Logical plan</summary>
                <pre>{JSON.stringify(viewModel.logicalPlan, null, 2)}</pre>
              </details>
            )}
            {viewModel.showSqlCard && (
              <details className="mini-card artifact-card">
                <summary>SQL</summary>
                <pre>{viewModel.sql}</pre>
              </details>
            )}
            {viewModel.showTable && (
              <div className="assistant-grid">
                <div className="mini-card">
                  <div className="label table-label">
                    <span>
                      <Table2 size={14} />
                      查询结果
                    </span>
                    <span>{viewModel.rows.length} 条</span>
                  </div>
                  <RowsTable rows={viewModel.rows} />
                </div>
              </div>
            )}
            {viewModel.pendingMemoryUpdates.length > 0 && (
              <div className="mini-card artifact-card">
                <div className="label">Pending memory proposals</div>
                <ul>
                  {viewModel.pendingMemoryUpdates.map((proposal, index) => (
                    <li key={`${proposal.scope}-${proposal.source}-${index}`}>
                      {proposal.scope}: {proposal.source}
                    </li>
                  ))}
                </ul>
              </div>
            )}
          </>
        )}
      </div>
    </div>
  );
}

function RowsTable({ rows }: { rows: DataRow[] }) {
  const [page, setPage] = useState(1);
  const paginated = useMemo(() => paginateRows(rows, page), [page, rows]);

  useEffect(() => {
    if (paginated.page !== page) {
      setPage(paginated.page);
    }
  }, [page, paginated.page]);

  if (!rows.length) {
    return <div className="no-rows">No rows returned</div>;
  }

  const columns = Array.from(new Set(rows.flatMap((row) => Object.keys(row))));
  const hasPagination = paginated.totalPages > 1;

  return (
    <>
      <table>
        <thead>
          <tr>
            {columns.map((column) => (
              <th key={column} className={getColumnClassName(column)}>
                {formatColumnLabel(column)}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {paginated.pageRows.map((row, rowIndex) => (
            <tr key={(paginated.page - 1) * paginated.pageSize + rowIndex}>
              {columns.map((column) => (
                <td key={column} className={getColumnClassName(column)}>
                  {formatCellValueForColumn(row[column], column)}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
      {hasPagination && (
        <div className="table-pagination">
          <span>{paginated.totalRows} 条</span>
          <button
            type="button"
            onClick={() => setPage((value) => value - 1)}
            disabled={paginated.page <= 1}
          >
            上一页
          </button>
          <span>
            第 {paginated.page} / {paginated.totalPages} 页
          </span>
          <button
            type="button"
            onClick={() => setPage((value) => value + 1)}
            disabled={paginated.page >= paginated.totalPages}
          >
            下一页
          </button>
        </div>
      )}
    </>
  );
}

function MarkdownAnswer({ text }: { text: string }) {
  const blocks = parseMarkdown(text);
  if (!blocks.length) {
    return <p className="assistant-answer">{text}</p>;
  }

  return (
    <div className="assistant-answer">
      {blocks.map((block, blockIndex) => (
        <p key={blockIndex}>
          {block.segments.map((segment, segmentIndex) => (
            <MarkdownInline key={segmentIndex} segment={segment} />
          ))}
        </p>
      ))}
    </div>
  );
}

function MarkdownInline({ segment }: { segment: MarkdownSegment }) {
  if (segment.kind === "strong") {
    return <strong>{segment.text}</strong>;
  }
  if (segment.kind === "code") {
    return <code>{segment.text}</code>;
  }
  return <>{segment.text}</>;
}

export function responseToAssistantMessage(
  response: ConversationMessageResponse,
  id = `assistant-${response.conversation_id}-${Date.now()}`,
): ChatMessage {
  return {
    id,
    role: "assistant",
    content: response.answer || response.error?.message || "No answer returned.",
    metadata: {
      contextualized_question: response.contextualized_question,
      logical_plan: response.logical_plan,
      answer: response.answer,
      message_type: response.message_type,
      rows: response.rows,
      ok: response.ok,
      error: response.error,
      trace: response.trace,
      sql: response.sql,
      pending_memory_updates: response.pending_memory_updates,
      version_pins: response.version_pins,
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
