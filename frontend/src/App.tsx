import {
  Activity,
  AlertCircle,
  AlertTriangle,
  BarChart3,
  Check,
  CheckCircle2,
  ChevronDown,
  Database,
  FileCode2,
  FileSearch,
  Layers3,
  Loader2,
  LogOut,
  Menu,
  MessageSquare,
  MoreHorizontal,
  PanelLeftClose,
  PanelLeftOpen,
  Pencil,
  Plus,
  Search,
  Send,
  ShieldCheck,
  Square,
  SquarePen,
  Table2,
  Trash2,
  User,
  Waypoints,
  X,
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
import { DataSourcePanel } from "./DataSourcePanel";
import type {
  AgentMode,
  ApiConversationMessage,
  ChatMessage,
  ChartSpec,
  Conversation,
  ConversationMessageResponse,
  DataSource,
  DataRow,
  JsonValue,
  LogicalQueryPlan,
  AnySemanticBinding,
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
import {
  buildEvidenceRoute,
  type EvidenceStep,
  type RunStage,
} from "./evidenceRoute";

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
  const [activeRunId, setActiveRunId] = useState("");
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
  const [dataSources, setDataSources] = useState<DataSource[]>([]);
  const [activeDataBinding, setActiveDataBinding] =
    useState<AnySemanticBinding | null>(null);
  const [dataSourcePanelOpen, setDataSourcePanelOpen] = useState(false);
  const [mobileNavigationOpen, setMobileNavigationOpen] = useState(false);
  const [conversationQuery, setConversationQuery] = useState("");
  const [runStage, setRunStage] = useState<RunStage>("idle");
  const [evidenceMessageId, setEvidenceMessageId] = useState("");
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
    setDataSources([]);
    setActiveDataBinding(null);
    setActiveRunId("");
    setRunStage("idle");
    setEvidenceMessageId("");
    setMobileNavigationOpen(false);
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

  const latestAssistant = useMemo(
    () =>
      [...messages]
        .reverse()
        .find(
          (message) =>
            message.role === "assistant" &&
            message.metadata.message_type !== "thinking",
        ) ?? null,
    [messages],
  );
  const evidenceRoute = useMemo(
    () =>
      buildEvidenceRoute({
        hasBinding: Boolean(activeDataBinding),
        runStage,
        latestAssistant,
      }),
    [activeDataBinding, latestAssistant, runStage],
  );
  const filteredConversations = useMemo(() => {
    const query = conversationQuery.trim().toLocaleLowerCase("zh-CN");
    if (!query) {
      return conversations;
    }
    return conversations.filter((conversation) =>
      (conversation.title || "未命名分析")
        .toLocaleLowerCase("zh-CN")
        .includes(query),
    );
  }, [conversationQuery, conversations]);
  const evidenceMessage =
    messages.find((message) => message.id === evidenceMessageId) ?? latestAssistant;

  const loadConversations = useCallback(async () => {
    const payload = await api.listConversations();
    setConversations(payload.items);
    return payload.items;
  }, [api]);

  const loadDataSources = useCallback(async () => {
    const payload = await api.listDataSources();
    setDataSources(payload.items);
    return payload.items;
  }, [api]);

  const loadMessages = useCallback(
    async (conversationId: string) => {
      const payload = await api.listMessages(conversationId);
      setMessages(payload.items.map(toChatMessage));
    },
    [api],
  );

  const loadConversationBinding = useCallback(
    async (conversationId: string) => {
      const payload = await api.getConversationDataSourceBinding(conversationId);
      setActiveDataBinding(payload.binding);
      return payload.binding;
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
        const [items] = await Promise.all([
          loadConversations(),
          loadDataSources(),
        ]);
        if (items[0]) {
          setActiveConversationId(items[0].conversation_id);
          await Promise.all([
            loadMessages(items[0].conversation_id),
            loadConversationBinding(items[0].conversation_id),
          ]);
        }
      }
    } catch (err) {
      handleAuthOrError(err, clearSession, setError);
    } finally {
      setBooting(false);
    }
  }, [
    api,
    clearSession,
    loadConversationBinding,
    loadConversations,
    loadDataSources,
    loadMessages,
  ]);

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
        loadDataSources(),
      ]);
      setHealthOk(Boolean(health.ok));
      setConversations(conversationList.items);
      if (conversationList.items[0]) {
        setActiveConversationId(conversationList.items[0].conversation_id);
        await Promise.all([
          loadMessages(conversationList.items[0].conversation_id),
          loadConversationBinding(conversationList.items[0].conversation_id),
        ]);
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
    setRunStage("idle");
    setEvidenceMessageId("");
    setMobileNavigationOpen(false);
    try {
      await Promise.all([
        loadMessages(conversationId),
        loadConversationBinding(conversationId),
      ]);
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
      setRunStage("idle");
      setEvidenceMessageId("");
      setMobileNavigationOpen(false);
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
    if (!activeDataBinding) {
      setError("请先上传数据集，确认语义绑定并选择该数据源。");
      setDataSourcePanelOpen(true);
      return;
    }

    setInput("");
    setLoading(true);
    setError("");
    setRunStage("started");
    setEvidenceMessageId("");

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

      const response = await api.streamMessage(
        conversationId,
        createSendMessagePayload(question, activeDataBinding, mode),
        (event) => {
          if (event.type === "run_started") {
            setActiveRunId(event.run_id);
            setRunStage("started");
          }
          if (event.type === "progress") {
            setRunStage("pinned");
          }
          if (event.type === "run_completed") {
            setRunStage("complete");
          }
          if (event.type === "run_failed") {
            setRunStage("failed");
          }
        },
      );
      setMessages((items) =>
        items.map((item) =>
          item.id === thinkingMessage.id ? responseToAssistantMessage(response, item.id) : item,
        ),
      );
      const refreshed = await loadConversations();
      setConversations(refreshed);
    } catch (err) {
      setRunStage("failed");
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
      setActiveRunId("");
      setLoading(false);
    }
  }

  async function handleCancelRun() {
    if (!activeRunId) {
      return;
    }
    try {
      await api.cancelRun(activeRunId);
    } catch (err) {
      if (err instanceof ApiError && err.status === 404) {
        return;
      }
      handleAuthOrError(err, clearSession, setError);
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
    <div
      className={`app-shell ${sidebarCollapsed ? "sidebar-collapsed" : ""} ${
        mobileNavigationOpen ? "mobile-navigation-open" : ""
      }`}
    >
      {mobileNavigationOpen && (
        <button
          className="navigation-scrim"
          type="button"
          aria-label="关闭导航"
          onClick={() => setMobileNavigationOpen(false)}
        />
      )}

      <div className="navigation-shell">
        <aside className="utility-rail" aria-label="主导航">
          <button
            className="product-mark"
            type="button"
            aria-label="返回分析工作台"
            title="Data Assistant"
            onClick={() => {
              setDataSourcePanelOpen(false);
              setMobileNavigationOpen(false);
            }}
          >
            <Waypoints size={23} />
          </button>
          <nav className="utility-actions">
            <button
              className={`rail-action ${dataSourcePanelOpen ? "" : "active"}`}
              type="button"
              aria-label="分析对话"
              title="分析对话"
              onClick={() => {
                setDataSourcePanelOpen(false);
                setMobileNavigationOpen(false);
              }}
            >
              <MessageSquare size={20} />
            </button>
            <button
              className={`rail-action ${dataSourcePanelOpen ? "active" : ""}`}
              type="button"
              aria-label="数据源"
              title="数据源"
              onClick={() => {
                setDataSourcePanelOpen(true);
                setMobileNavigationOpen(false);
              }}
            >
              <Database size={20} />
              {dataSources.length > 0 && (
                <span className="rail-badge">{activeDataBinding ? "✓" : dataSources.length}</span>
              )}
            </button>
            <button
              className="rail-action"
              type="button"
              aria-label="查看证据"
              title="查看证据"
              disabled={!latestAssistant}
              onClick={() => {
                if (latestAssistant) {
                  setEvidenceMessageId(latestAssistant.id);
                }
              }}
            >
              <ShieldCheck size={20} />
            </button>
          </nav>
          <div className="utility-spacer" />
          <button
            className="rail-profile"
            type="button"
            aria-label={`${session.user.username} ${session.user.tenant_id}`}
            aria-expanded={accountOpen}
            onClick={() => setAccountOpen((value) => !value)}
          >
            {initials(session.user.username)}
          </button>
          {accountOpen && (
            <div className="account-flyout">
              <div className="account-profile">
                <div className="avatar">{initials(session.user.username)}</div>
                <div>
                  <div className="account-name">{session.user.username}</div>
                  <div className="small muted">{session.user.tenant_id}</div>
                </div>
              </div>
              <div className="account-row active">
                <User size={15} />
                <span>当前账号</span>
              </div>
              <button className="account-row button-row" onClick={() => void handleLogout()}>
                <LogOut size={15} />
                <span>退出登录</span>
              </button>
            </div>
          )}
        </aside>

        <aside className="conversation-sidebar">
          <div className="sidebar-header">
            <div>
              <div className="sidebar-title">Data Assistant</div>
              <div className="sidebar-subtitle">从问题到可信证据</div>
            </div>
            <button
              className="icon-button collapse-button"
              type="button"
              onClick={() => setSidebarCollapsed(true)}
              aria-label="Collapse sidebar"
              title="收起会话栏"
            >
              <PanelLeftClose size={18} />
            </button>
          </div>

          <button
            className={getNewChatButtonClassName(activeConversationId)}
            onClick={handleNewConversation}
            disabled={loading}
          >
            <SquarePen size={17} />
            <span>新建分析</span>
          </button>

          <label className="conversation-search">
            <Search size={16} />
            <span className="sr-only">搜索对话</span>
            <input
              value={conversationQuery}
              onChange={(event) => setConversationQuery(event.target.value)}
              placeholder="搜索分析记录"
            />
            {conversationQuery && (
              <button
                type="button"
                aria-label="清空搜索"
                onClick={() => setConversationQuery("")}
              >
                <X size={14} />
              </button>
            )}
          </label>

          <div className="conversation-list" aria-label="Conversation history">
            <div className="sidebar-section-label">
              <span>最近分析</span>
              <span>{filteredConversations.length}</span>
            </div>
            {filteredConversations.map((conversation) => {
              const title = conversation.title || "未命名分析";
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
                        <span>{title}</span>
                        <small>{formatConversationDate(conversation.updated_at)}</small>
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
                            <span>移至归档</span>
                          </button>
                        </div>
                      )}
                    </>
                  )}
                </div>
              );
            })}
            {!filteredConversations.length && (
              <div className="empty-list">
                {conversationQuery ? "没有匹配的分析记录" : "还没有分析记录"}
              </div>
            )}
          </div>
        </aside>
      </div>

      <main className="content">
        {dataSourcePanelOpen ? (
          <DataSourcePanel
            api={api}
            sources={dataSources}
            selectedBinding={activeDataBinding}
            onRefresh={loadDataSources}
            onBindingSelect={setActiveDataBinding}
            onClose={() => setDataSourcePanelOpen(false)}
          />
        ) : (
          <>
            <header className="chat-topbar workspace-header">
              <div className="workspace-leading">
                <button
                  className="icon-button mobile-menu-button"
                  type="button"
                  aria-label="打开导航"
                  onClick={() => setMobileNavigationOpen(true)}
                >
                  <Menu size={20} />
                </button>
                {sidebarCollapsed && (
                  <button
                    className="icon-button desktop-expand-button"
                    type="button"
                    onClick={() => setSidebarCollapsed(false)}
                    aria-label="Expand sidebar"
                    title="展开会话栏"
                  >
                    <PanelLeftOpen size={19} />
                  </button>
                )}
                <button
                  className={`dataset-context ${activeDataBinding ? "ready" : "missing"}`}
                  type="button"
                  onClick={() => setDataSourcePanelOpen(true)}
                >
                  <Database size={17} />
                  <span>
                    <small>当前数据</small>
                    <strong>
                      {activeDataBinding
                        ? sourceName(dataSources, activeDataBinding.source_id)
                        : "尚未选择数据集"}
                    </strong>
                  </span>
                  <ChevronDown size={15} />
                </button>
              </div>

              <div className="workspace-actions">
                <div
                  className={`service-status ${healthOk ? "online" : "offline"}`}
                  title={healthOk ? "后端服务正常" : "后端服务不可用"}
                >
                  <span />
                  {healthOk ? "服务在线" : "服务异常"}
                </div>
                <div className="mode-control" role="group" aria-label="分析模式">
                  {(
                    [
                      ["plan", "规划", "仅生成分析计划与查询"],
                      ["preview", "预览", "小范围执行并返回预览"],
                      ["execute", "执行", "完整执行并生成回答"],
                    ] as const
                  ).map(([value, label, title]) => (
                    <button
                      key={value}
                      type="button"
                      title={title}
                      aria-pressed={mode === value}
                      className={mode === value ? "active" : ""}
                      disabled={loading}
                      onClick={() => setMode(value)}
                    >
                      {label}
                    </button>
                  ))}
                </div>
              </div>
            </header>

            <EvidenceRail
              steps={evidenceRoute}
              sourceLabel={
                activeDataBinding
                  ? sourceName(dataSources, activeDataBinding.source_id)
                  : "连接数据"
              }
              onDataSource={() => setDataSourcePanelOpen(true)}
              onEvidence={() => {
                if (latestAssistant) {
                  setEvidenceMessageId(latestAssistant.id);
                }
              }}
            />

            {error && (
              <div className="error-banner" role="alert">
                <AlertTriangle size={17} />
                <span>{error}</span>
                {!activeDataBinding && (
                  <button type="button" onClick={() => setDataSourcePanelOpen(true)}>
                    配置数据源
                  </button>
                )}
              </div>
            )}

            <section
              className={`thread ${messages.length ? "" : "empty"}`}
              aria-live="polite"
            >
              <div className="thread-content">
                {booting ? (
                  <div className="loading-state">
                    <Loader2 className="spin" size={22} />
                    正在加载工作区
                  </div>
                ) : (
                  <>
                    {messages.map((message) =>
                      message.role === "user" ? (
                        <UserBubble key={message.id} message={message} />
                      ) : (
                        <AssistantBubble
                          key={message.id}
                          message={message}
                          onShowEvidence={() => setEvidenceMessageId(message.id)}
                        />
                      ),
                    )}
                    {!messages.length && (
                      <div className="empty-thread">
                        <div className="empty-route-mark" aria-hidden="true">
                          <Waypoints size={30} />
                        </div>
                        <h1>
                          {activeDataBinding
                            ? `你好，${session.user.username}。今天想从数据里确认什么？`
                            : "先连接一份数据，再开始分析"}
                        </h1>
                        <p>
                          {activeDataBinding
                            ? "问题会沿着数据范围、查询、证据和回答逐步推进。"
                            : "上传文件并确认语义范围后，每次回答都会带着可核查的证据路径。"}
                        </p>
                        {!activeDataBinding && (
                          <button
                            className="primary-action"
                            type="button"
                            onClick={() => setDataSourcePanelOpen(true)}
                          >
                            <Database size={17} />
                            连接数据源
                          </button>
                        )}
                        {activeDataBinding && (
                          <div className="starter-questions" aria-label="问题示例">
                            {["概括这份数据的主要趋势", "哪些维度最值得进一步分析？"].map(
                              (question) => (
                                <button
                                  key={question}
                                  type="button"
                                  onClick={() => setInput(question)}
                                >
                                  {question}
                                </button>
                              ),
                            )}
                          </div>
                        )}
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
                <button
                  className="composer-context"
                  type="button"
                  onClick={() => setDataSourcePanelOpen(true)}
                  aria-label="选择数据源"
                  title="选择数据源"
                >
                  <Database size={17} />
                  <span>
                    {activeDataBinding
                      ? sourceName(dataSources, activeDataBinding.source_id)
                      : "选择数据"}
                  </span>
                </button>
                <input
                  className="composer-input"
                  value={input}
                  onChange={(event) => setInput(event.target.value)}
                  placeholder={
                    activeDataBinding
                      ? "输入你的业务问题"
                      : "请先连接并激活数据源"
                  }
                  disabled={loading}
                  aria-label="分析问题"
                />
                <button
                  className={`send-button ${loading ? "cancel-run" : ""}`}
                  type={loading ? "button" : "submit"}
                  disabled={loading ? !activeRunId : !input.trim() || !activeDataBinding}
                  onClick={loading ? () => void handleCancelRun() : undefined}
                  aria-label={loading ? "Cancel active run" : "Send message"}
                  title={loading ? "取消当前运行" : "发送问题"}
                >
                  {loading ? <Square size={13} fill="currentColor" /> : <Send size={18} />}
                </button>
              </form>
              <p>回答仅基于当前已激活的数据范围，请结合业务语境判断。</p>
            </div>
          </>
        )}
      </main>

      {evidenceMessageId && evidenceMessage && (
        <EvidenceDrawer
          message={evidenceMessage}
          binding={activeDataBinding}
          onClose={() => setEvidenceMessageId("")}
        />
      )}
    </div>
  );
}

function EvidenceRail({
  steps,
  sourceLabel,
  onDataSource,
  onEvidence,
}: {
  steps: EvidenceStep[];
  sourceLabel: string;
  onDataSource: () => void;
  onEvidence: () => void;
}) {
  return (
    <nav className="evidence-rail" aria-label="分析证据路径">
      {steps.map((step, index) => {
        const actionable = step.id === "dataset" || step.id === "evidence";
        return (
          <div className={`evidence-station ${step.status}`} key={step.id}>
            <button
              type="button"
              disabled={!actionable}
              onClick={
                step.id === "dataset"
                  ? onDataSource
                  : step.id === "evidence"
                    ? onEvidence
                    : undefined
              }
              aria-current={step.status === "active" ? "step" : undefined}
            >
              <span className="station-node" aria-hidden="true">
                {step.status === "complete" ? (
                  <Check size={13} />
                ) : step.status === "error" ? (
                  <AlertCircle size={13} />
                ) : (
                  index + 1
                )}
              </span>
              <span>
                <strong>{step.label}</strong>
                <small>{step.id === "dataset" ? sourceLabel : step.detail}</small>
              </span>
            </button>
          </div>
        );
      })}
    </nav>
  );
}

function EvidenceDrawer({
  message,
  binding,
  onClose,
}: {
  message: ChatMessage;
  binding: AnySemanticBinding | null;
  onClose: () => void;
}) {
  const viewModel = createAssistantViewModel(message);
  const pins = message.metadata.version_pins;
  const relationshipEvidence = getRelationshipEvidence(viewModel.logicalPlan);

  return (
    <>
      <button
        className="evidence-scrim"
        type="button"
        aria-label="关闭证据面板"
        onClick={onClose}
      />
      <aside className="evidence-drawer" aria-label="证据详情">
        <header className="evidence-drawer-header">
          <div>
            <div className="drawer-title">
              <ShieldCheck size={19} />
              <h2>证据路径</h2>
            </div>
            <p>查看本次回答使用的数据范围、查询和运行证据。</p>
          </div>
          <button className="icon-button" type="button" onClick={onClose} aria-label="关闭证据面板">
            <X size={19} />
          </button>
        </header>

        <div className="evidence-drawer-body">
          <section className="evidence-section scope-section">
            <div className="evidence-section-heading">
              <Database size={16} />
              <h3>数据范围</h3>
              <span className={binding ? "status-stamp success" : "status-stamp warning"}>
                {binding ? "已锁定" : "未锁定"}
              </span>
            </div>
            {binding ? (
              <dl className="evidence-facts">
                <div><dt>数据源</dt><dd>{binding.source_id}</dd></div>
                <div><dt>快照</dt><dd>v{binding.source_snapshot_version}</dd></div>
                <div><dt>语义域</dt><dd>{binding.domain_id}</dd></div>
                <div><dt>绑定</dt><dd>v{binding.version}</dd></div>
              </dl>
            ) : (
              <p className="evidence-empty">这条消息没有可用的数据绑定信息。</p>
            )}
          </section>

          {viewModel.logicalPlan && (
            <details className="evidence-section" open>
              <summary>
                <span><Layers3 size={16} />分析计划</span>
                <ChevronDown size={16} />
              </summary>
              <pre>{JSON.stringify(viewModel.logicalPlan, null, 2)}</pre>
            </details>
          )}

          {relationshipEvidence && (
            <section className="evidence-section">
              <div className="evidence-section-heading">
                <Waypoints size={16} />
                <h3>关系路径决策</h3>
              </div>
              <dl className="evidence-facts">
                <div><dt>Route digest</dt><dd>{evidenceText(relationshipEvidence.route_digest)}</dd></div>
                <div><dt>逻辑节点</dt><dd>{evidenceText(relationshipEvidence.logical_node_ids)}</dd></div>
                <div><dt>关联边</dt><dd>{evidenceText(relationshipEvidence.edge_ids)}</dd></div>
                <div><dt>Cardinality</dt><dd>{evidenceText(relationshipEvidence.cardinality_by_node)}</dd></div>
                <div><dt>Fan-out</dt><dd>{evidenceText(relationshipEvidence.fanout_decision)}</dd></div>
              </dl>
            </section>
          )}

          {viewModel.showSqlCard && (
            <details className="evidence-section">
              <summary>
                <span><FileCode2 size={16} />生成的 SQL</span>
                <ChevronDown size={16} />
              </summary>
              <pre>{viewModel.sql}</pre>
            </details>
          )}

          {viewModel.trace.length > 0 && (
            <section className="evidence-section">
              <div className="evidence-section-heading">
                <Activity size={16} />
                <h3>运行节点</h3>
                <span>{viewModel.trace.length}</span>
              </div>
              <ol className="trace-list">
                {viewModel.trace.map((entry, index) => (
                  <li key={`${entry.node}-${index}`}>
                    <span className={entry.error_code ? "error" : "complete"}>
                      {entry.error_code ? <AlertCircle size={13} /> : <Check size={13} />}
                    </span>
                    <div>
                      <strong>{formatTraceNode(entry.node)}</strong>
                      <small>{entry.error_code ?? entry.status}</small>
                    </div>
                  </li>
                ))}
              </ol>
            </section>
          )}

          <section className="evidence-section">
            <div className="evidence-section-heading">
              <Table2 size={16} />
              <h3>结果证据</h3>
            </div>
            <dl className="evidence-facts">
              <div><dt>消息类型</dt><dd>{viewModel.messageType}</dd></div>
              <div><dt>返回行数</dt><dd>{viewModel.rows.length}</dd></div>
              <div><dt>图表</dt><dd>{viewModel.chart ? "已生成" : "未生成"}</dd></div>
              <div><dt>状态</dt><dd>{viewModel.status === "error" ? "失败" : "已验证"}</dd></div>
            </dl>
          </section>

          {pins && (
            <details className="evidence-section">
              <summary>
                <span><Waypoints size={16} />版本锁定</span>
                <ChevronDown size={16} />
              </summary>
              <dl className="version-list">
                <div><dt>运行时</dt><dd>{pins.runtime_version}</dd></div>
                <div><dt>技能</dt><dd>{pins.skill_version}</dd></div>
                <div><dt>执行图</dt><dd>{pins.graph_version}</dd></div>
                <div><dt>Schema</dt><dd>{pins.schema_fingerprint}</dd></div>
              </dl>
            </details>
          )}

          {viewModel.pendingMemoryUpdates.length > 0 && (
            <section className="evidence-section">
              <div className="evidence-section-heading">
                <FileSearch size={16} />
                <h3>待审批记忆</h3>
                <span>{viewModel.pendingMemoryUpdates.length}</span>
              </div>
              <ul className="proposal-list">
                {viewModel.pendingMemoryUpdates.map((proposal, index) => (
                  <li key={`${proposal.scope}-${proposal.source}-${index}`}>
                    <strong>{proposal.scope}</strong>
                    <span>{proposal.source}</span>
                  </li>
                ))}
              </ul>
            </section>
          )}

          {viewModel.error && (
            <section className="evidence-section evidence-error">
              <div className="evidence-section-heading">
                <AlertTriangle size={16} />
                <h3>{friendlyRuntimeError(viewModel.error.code)}</h3>
              </div>
              <p>{viewModel.error.message}</p>
            </section>
          )}
        </div>
      </aside>
    </>
  );
}

function getRelationshipEvidence(plan: LogicalQueryPlan | null): Record<string, JsonValue> | null {
  const evidence = plan?.relationship_evidence;
  return evidence && typeof evidence === "object" && !Array.isArray(evidence)
    ? evidence
    : null;
}

function evidenceText(value: JsonValue | undefined): string {
  if (value === undefined || value === null) return "—";
  return typeof value === "string" ? value : JSON.stringify(value);
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
      <section className="login-story" aria-label="产品介绍">
        <div className="login-brand">
          <div className="mark">
            <Waypoints size={22} />
          </div>
          <div>
            <div className="h2">Data Assistant</div>
            <div className="small">Governed analytics workspace</div>
          </div>
        </div>
        <div className="login-copy">
          <h1>从问题到可信证据</h1>
          <p>
            在明确的数据范围内提问，沿着语义、查询和证据路径得到可以复核的业务回答。
          </p>
        </div>
        <div className="login-route" aria-hidden="true">
          {["数据集", "语义范围", "查询", "证据", "回答"].map((label, index) => (
            <div key={label} className={index < 2 ? "complete" : index === 2 ? "active" : ""}>
              <span>{index < 2 ? <Check size={13} /> : index + 1}</span>
              <small>{label}</small>
            </div>
          ))}
        </div>
        <div className="login-assurances">
          <span><ShieldCheck size={16} />只读执行</span>
          <span><Waypoints size={16} />版本锁定</span>
          <span><FileSearch size={16} />证据可查</span>
        </div>
      </section>

      <form className="login-card" onSubmit={submit}>
        <div className="login-form-heading">
          <h2>登录工作区</h2>
          <p>使用租户账号进入受治理的数据分析环境。</p>
        </div>
        {error && (
          <div className="login-error" role="alert">
            <AlertCircle size={16} />
            {friendlyAuthError(error)}
          </div>
        )}
        <label className="field">
          <span>租户</span>
          <input
            value={tenantId}
            onChange={(event) => setTenantId(event.target.value)}
            autoComplete="organization"
          />
        </label>
        <label className="field">
          <span>用户名</span>
          <input
            value={username}
            onChange={(event) => setUsername(event.target.value)}
            autoComplete="username"
          />
        </label>
        <label className="field">
          <span>密码</span>
          <input
            type="password"
            value={password}
            onChange={(event) => setPassword(event.target.value)}
            autoComplete="current-password"
          />
        </label>
        <button
          className="btn login-button"
          type="submit"
          disabled={loading || !tenantId.trim() || !username.trim() || !password}
        >
          {loading ? <Loader2 className="spin" size={16} /> : null}
          {loading ? "正在验证" : "登录"}
        </button>
        <p className="login-security-note">
          登录状态仅用于当前租户的数据与会话权限。
        </p>
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

function AssistantBubble({
  message,
  onShowEvidence,
}: {
  message: ChatMessage;
  onShowEvidence: () => void;
}) {
  const viewModel = createAssistantViewModel(message);
  const answerText = viewModel.answer || (viewModel.showTable ? "" : viewModel.status);
  const needsClarification = viewModel.messageType === "clarification";
  const hasEvidence = Boolean(
    viewModel.logicalPlan ||
      viewModel.showSqlCard ||
      viewModel.trace.length ||
      viewModel.rows.length ||
      viewModel.pendingMemoryUpdates.length ||
      message.metadata.version_pins,
  );

  return (
    <div className="message ai">
      <div className="bubble assistant-bubble">
        {viewModel.isThinking ? (
          <div className="thinking">
            <div className="assistant-mark"><Waypoints size={15} /></div>
            <div>
              <strong>正在沿证据路径分析</strong>
              <span>已锁定数据范围，正在生成并核验查询。</span>
            </div>
            <Loader2 className="spin" size={17} />
          </div>
        ) : (
          <>
            <div className="assistant-heading">
              <div className="assistant-identity">
                <div className="assistant-mark"><Waypoints size={15} /></div>
                <strong>Data Assistant</strong>
              </div>
              <div
                className={`response-status ${
                  viewModel.status === "error" ? "error" : "complete"
                }`}
              >
                {viewModel.status === "error" || needsClarification ? (
                  <AlertCircle size={14} />
                ) : (
                  <CheckCircle2 size={14} />
                )}
                {viewModel.status === "error"
                  ? "运行未完成"
                  : needsClarification
                    ? "需要澄清"
                    : "查询已完成"}
              </div>
            </div>
            {answerText && <MarkdownAnswer text={answerText} />}
            {viewModel.error && (
              <div className="error-detail" role="alert">
                <AlertTriangle size={17} />
                <div>
                  <strong>{friendlyRuntimeError(viewModel.error.code)}</strong>
                  <span>{viewModel.error.message}</span>
                </div>
              </div>
            )}
            {viewModel.chart && (
              <SafeBarChart chart={viewModel.chart} rows={viewModel.rows} />
            )}
            {viewModel.showTable && (
              <div className="assistant-grid">
                <div className="result-surface">
                  <div className="table-label">
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
            {hasEvidence && (
              <button className="evidence-link" type="button" onClick={onShowEvidence}>
                <ShieldCheck size={15} />
                查看证据路径
                <span>
                  {viewModel.rows.length
                    ? `${viewModel.rows.length} 条结果`
                    : viewModel.trace.length
                      ? `${viewModel.trace.length} 个运行节点`
                      : "可核查"}
                </span>
              </button>
            )}
          </>
        )}
      </div>
    </div>
  );
}

function SafeBarChart({
  chart,
  rows,
}: {
  chart: ChartSpec;
  rows: DataRow[];
}) {
  const points = rows
    .map((row) => ({
      label: String(row[chart.x_field] ?? ""),
      value: finiteNumber(row[chart.y_field]),
    }))
    .filter(
      (point): point is { label: string; value: number } =>
        point.value !== null,
    )
    .slice(0, 30);
  const maximum = Math.max(...points.map((point) => Math.abs(point.value)), 0);
  if (!points.length || maximum === 0) {
    return null;
  }
  return (
    <figure className="safe-chart">
      <figcaption>
        <strong>{chart.title}</strong>
        <span>{chart.y_field}</span>
      </figcaption>
      <div className="safe-chart-bars">
        {points.map((point, index) => (
          <div
            className="safe-chart-row"
            key={`${point.label}-${index}`}
            title={`${point.label}: ${point.value}`}
          >
            <span>{point.label}</span>
            <div>
              <i
                style={{
                  width: `${Math.max(2, (Math.abs(point.value) / maximum) * 100)}%`,
                }}
              />
            </div>
            <strong>{point.value}</strong>
          </div>
        ))}
      </div>
    </figure>
  );
}

function finiteNumber(value: DataRow[string]): number | null {
  if (typeof value !== "number" && typeof value !== "string") {
    return null;
  }
  const parsed = typeof value === "number" ? value : Number(value);
  return Number.isFinite(parsed) ? parsed : null;
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
    return <div className="no-rows">查询没有返回数据行</div>;
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
    content: response.answer || response.error?.message || "本次运行没有返回回答。",
    metadata: {
      contextualized_question: response.contextualized_question,
      logical_plan: response.logical_plan,
      dataset_query_plan: response.dataset_query_plan,
      answer: response.answer,
      message_type: response.message_type,
      rows: response.rows,
      chart: response.chart,
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

function sourceName(sources: DataSource[], sourceId: string): string {
  return sources.find((source) => source.source_id === sourceId)?.name ?? sourceId;
}

function formatConversationDate(value: string): string {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) {
    return "";
  }
  const today = new Date();
  if (date.toDateString() === today.toDateString()) {
    return new Intl.DateTimeFormat("zh-CN", {
      hour: "2-digit",
      minute: "2-digit",
      hour12: false,
    }).format(date);
  }
  return new Intl.DateTimeFormat("zh-CN", {
    month: "2-digit",
    day: "2-digit",
  }).format(date);
}

function friendlyAuthError(error: string): string {
  if (/invalid authentication credentials|unauthorized|401/i.test(error)) {
    return "租户、用户名或密码不正确，请检查后重试。";
  }
  if (/network|fetch|failed to connect/i.test(error)) {
    return "暂时无法连接服务，请检查后端是否已启动。";
  }
  return error;
}

function friendlyRuntimeError(code: string): string {
  const labels: Record<string, string> = {
    INVALID_REQUEST: "请求信息不完整",
    BINDING_STALE: "数据绑定已过期",
    ACCESS_DENIED: "当前账号无权访问这份数据",
    EMPTY_RESULT: "查询没有返回结果",
    COST_EXCEEDED: "查询范围超出安全限制",
    DEADLINE_EXCEEDED: "查询运行超时",
    CANCELLED: "运行已取消",
    SQL_POLICY_VIOLATION: "查询不符合只读策略",
    INTERNAL_ERROR: "运行安全终止",
    REQUEST_FAILED: "请求未能完成",
  };
  return labels[code] ?? "运行未能完成";
}

function formatTraceNode(node: string): string {
  const labels: Record<string, string> = {
    validate_sql: "验证查询",
    plan: "生成计划",
    compile: "编译查询",
    "execute": "执行查询",
    evidence: "核验证据",
    answer: "生成回答",
    persist: "保存会话",
  };
  return labels[node] ?? node.replaceAll("_", " ");
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
