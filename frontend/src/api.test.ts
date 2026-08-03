import { afterEach, describe, expect, it, vi } from "vitest";
import { ApiClient, ApiError } from "./api";
import { responseToAssistantMessage } from "./App";
import agentResponseFixture from "./generated/agent-response.fixture.json";
import type { AgentResponse, Conversation, StoredSession } from "./types";
import { createAssistantViewModel } from "./viewModel";

describe("ApiClient", () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("patches conversation title and archived state", async () => {
    const updatedConversation = conversation("conv 1", "Renamed", true);
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue({
      ok: true,
      status: 200,
      json: async () => updatedConversation,
    } as Response);
    const api = new ApiClient({
      getSession: () => session(),
      setSession: vi.fn(),
      clearSession: vi.fn(),
    });

    const result = await api.updateConversation("conv 1", {
      title: "Renamed",
      archived: true,
    });

    expect(result).toEqual(updatedConversation);
    expect(fetchMock).toHaveBeenCalledTimes(1);
    const [path, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(path).toBe("/api/conversations/conv%201");
    expect(init.method).toBe("PATCH");
    expect(JSON.parse(String(init.body))).toEqual({
      title: "Renamed",
      archived: true,
    });
    expect((init.headers as Headers).get("Authorization")).toBe("Bearer access-token");
  });

  it("uploads file datasources as multipart without forcing a JSON content type", async () => {
    const datasource = {
      source_id: "orders",
      name: "Orders",
      kind: "csv",
      status: "ready",
      active_snapshot_version: 1,
      options: { dialect: "duckdb" },
      created_at: "2026-07-26T00:00:00Z",
      updated_at: "2026-07-26T00:00:00Z",
    } as const;
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue({
      ok: true,
      status: 201,
      json: async () => datasource,
    } as Response);
    const api = createApi();
    const file = new File(["order_id,amount\nA-1,10\n"], "orders.csv", {
      type: "text/csv",
    });

    await expect(api.uploadFileDataSource("Orders", [file], "orders")).resolves.toEqual(
      datasource,
    );

    const [path, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(path).toBe("/api/data-sources/files");
    expect(init.method).toBe("POST");
    expect(init.body).toBeInstanceOf(FormData);
    expect((init.headers as Headers).has("Content-Type")).toBe(false);
    expect((init.headers as Headers).get("Authorization")).toBe("Bearer access-token");
  });

  it("deletes a datasource with an encoded identifier", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue({
      ok: true,
      status: 200,
      json: async () => ({ source_id: "orders 2026", deleted: true }),
    } as Response);
    const api = createApi();

    await expect(api.deleteDataSource("orders 2026")).resolves.toEqual({
      source_id: "orders 2026",
      deleted: true,
    });

    const [path, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(path).toBe("/api/data-sources/orders%202026");
    expect(init.method).toBe("DELETE");
  });

  it("creates and activates a semantic binding with encoded source identifiers", async () => {
    const draft = {
      binding_id: "orders-binding-1",
      tenant_id: "tenant-a",
      source_id: "orders 2026",
      source_snapshot_version: 1,
      domain_id: "dataset.orders",
      version: 1,
      status: "draft",
      mappings: [
        {
          logical_ref: "dataset.Orders.amount",
          physical_relation: "public.orders",
          physical_column: "amount",
        },
      ],
      created_at: "2026-07-26T00:00:00Z",
      updated_at: "2026-07-26T00:00:00Z",
    } as const;
    const fetchMock = vi
      .spyOn(globalThis, "fetch")
      .mockResolvedValueOnce({
        ok: true,
        status: 201,
        json: async () => draft,
      } as Response)
      .mockResolvedValueOnce({
        ok: true,
        status: 200,
        json: async () => ({ ...draft, status: "active" }),
      } as Response);
    const api = createApi();

    const created = await api.createDataSourceBinding("orders 2026", {
      domain_id: draft.domain_id,
      mappings: [...draft.mappings],
    });
    await api.activateDataSourceBinding("orders 2026", created.binding_id);

    const [createPath, createInit] = fetchMock.mock.calls[0] as [
      string,
      RequestInit,
    ];
    const [activatePath, activateInit] = fetchMock.mock.calls[1] as [
      string,
      RequestInit,
    ];
    expect(createPath).toBe("/api/data-sources/orders%202026/bindings");
    expect(createInit.method).toBe("POST");
    expect(JSON.parse(String(createInit.body))).toEqual({
      domain_id: "dataset.orders",
      mappings: draft.mappings,
    });
    expect(activatePath).toBe(
      "/api/data-sources/orders%202026/bindings/orders-binding-1/activate",
    );
    expect(activateInit.method).toBe("POST");
  });

  it("consumes typed SSE events and can cancel the active run", async () => {
    const failure = agentFailure();
    const started = {
      type: "run_started",
      run_id: "run 1",
      sequence: 0,
      data: {
        kind: "run_started",
        mode: "execute",
        enterprise_id: "olist",
        domain_id: "commerce",
      },
      response: null,
    };
    const terminal = {
      type: "run_failed",
      run_id: "run 1",
      sequence: 1,
      data: { kind: "run_failed", error_code: "CANCELLED" },
      response: failure,
    };
    const stream = [
      `id: 0\nevent: run_started\ndata: ${JSON.stringify(started)}\n\n`,
      `id: 1\nevent: run_failed\ndata: ${JSON.stringify(terminal)}\n\n`,
    ].join("");
    const fetchMock = vi
      .spyOn(globalThis, "fetch")
      .mockResolvedValueOnce(
        new Response(stream, {
          status: 200,
          headers: { "Content-Type": "text/event-stream; charset=utf-8" },
        }),
      )
      .mockResolvedValueOnce({
        ok: true,
        status: 200,
        json: async () => ({ run_id: "run 1", cancelled: true }),
      } as Response);
    const events: string[] = [];
    const api = createApi();

    const response = await api.streamMessage(
      "conv 1",
      payload(),
      (event) => events.push(event.type),
    );
    const cancelled = await api.cancelRun("run 1");

    expect(response).toEqual(failure);
    expect(events).toEqual(["run_started", "run_failed"]);
    expect(cancelled).toEqual({ run_id: "run 1", cancelled: true });
    expect(fetchMock.mock.calls[0][0]).toBe(
      "/api/conversations/conv%201/messages/stream",
    );
    expect(fetchMock.mock.calls[1][0]).toBe("/api/runs/run%201/cancel");
  });

  it("accepts the snake-case logical plan emitted by the live SSE runtime", async () => {
    const success = {
      ...agentFailure(),
      ok: true,
      answer: "电子产品金额最高，为 1920。",
      error: null,
      message_type: "text",
      chart: null,
      rows: [{ category: "电子产品", total_amount: 1920 }],
      logical_plan: snakeCaseKeys(agentFailure().logical_plan),
    } as AgentResponse;
    const started = {
      type: "run_started",
      run_id: "dataset-run-1",
      sequence: 0,
      data: {
        kind: "run_started",
        mode: "execute",
        enterprise_id: "user-dataset",
        domain_id: "dataset.sales",
      },
      response: null,
    };
    const terminal = {
      type: "run_completed",
      run_id: "dataset-run-1",
      sequence: 1,
      data: { kind: "run_completed" },
      response: success,
    };
    const stream = [
      `id: 0\nevent: run_started\ndata: ${JSON.stringify(started)}\n\n`,
      `id: 1\nevent: run_completed\ndata: ${JSON.stringify(terminal)}\n\n`,
    ].join("");
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(stream, {
        status: 200,
        headers: { "Content-Type": "text/event-stream; charset=utf-8" },
      }),
    );
    const events: string[] = [];

    await expect(
      createApi().streamMessage("conv-1", payload(), (event) =>
        events.push(event.type),
      ),
    ).resolves.toEqual(success);
    expect(events).toEqual(["run_started", "run_completed"]);
  });

  it.each([422, 403, 500])(
    "returns a complete typed AgentResponse for HTTP %i Runtime failures",
    async (status) => {
    const failure = agentFailure();
    vi.spyOn(globalThis, "fetch").mockResolvedValue({
      ok: false,
      status,
      statusText: "Runtime Error",
      json: async () => failure,
    } as unknown as Response);
    const api = new ApiClient({
      getSession: () => session(),
      setSession: vi.fn(),
      clearSession: vi.fn(),
    });

    const response = await api.sendMessage("conv-1", {
      question: "show gmv",
      enterprise_id: "olist",
      domain_id: "commerce",
      mode: "execute",
      requested_output: "answer",
      include_trace: true,
    });
    const assistantMessage = responseToAssistantMessage(response, "assistant-error");
    const viewModel = createAssistantViewModel(assistantMessage);

    expect(response).toEqual(failure);
    expect(assistantMessage).toEqual({
      id: "assistant-error",
      role: "assistant",
      content: failure.error?.message,
      metadata: {
        contextualized_question: failure.contextualized_question,
        logical_plan: failure.logical_plan,
        dataset_query_plan: failure.dataset_query_plan,
        answer: failure.answer,
        message_type: failure.message_type,
        rows: failure.rows,
        chart: failure.chart,
        ok: failure.ok,
        error: failure.error,
        trace: failure.trace,
        sql: failure.sql,
        pending_memory_updates: failure.pending_memory_updates,
        version_pins: failure.version_pins,
      },
    });
    expect(viewModel.answer).toBe(failure.error?.message);
    expect(viewModel.rows).toEqual(failure.rows);
    expect(viewModel.error).toEqual(failure.error);
    expect(viewModel.logicalPlan).toEqual(failure.logical_plan);
    expect(viewModel.sql).toBe(failure.sql);
    expect(viewModel.trace).toEqual(failure.trace);
    expect(viewModel.pendingMemoryUpdates).toEqual(failure.pending_memory_updates);
    expect(viewModel.status).toBe("error");
    expect(viewModel.messageType).toBe("chart");
    },
  );

  it("rejects plans and error codes that backend AgentResponse rejects", async () => {
    const invalid = {
      ...agentFailure(),
      logical_plan: { garbage: true },
      error: {
        code: "NOT_A_BACKEND_ERROR",
        message: "safe",
        retryable: false,
      },
    };
    vi.spyOn(globalThis, "fetch").mockResolvedValue({
      ok: false,
      status: 500,
      statusText: "Internal Server Error",
      json: async () => invalid,
    } as unknown as Response);
    const api = createApi();

    await expect(api.sendMessage("conv-1", payload())).rejects.toBeInstanceOf(
      ApiError,
    );
  });

  it("accepts a backend-valid nonblank message_type outside legacy UI enums", async () => {
    const failure: AgentResponse = {
      ...agentFailure(),
      message_type: "chart",
    };
    vi.spyOn(globalThis, "fetch").mockResolvedValue({
      ok: false,
      status: 500,
      statusText: "Internal Server Error",
      json: async () => failure,
    } as unknown as Response);
    const api = createApi();

    await expect(api.sendMessage("conv-1", payload())).resolves.toEqual(failure);
  });

  it("rejects incomplete, extra-key, invalid-enum, and non-finite nested plans", async () => {
    const validPlan = agentFailure().logical_plan as Record<string, unknown>;
    const invalidPlans = [
      Object.fromEntries(Object.entries(validPlan).filter(([key]) => key !== "resultShape")),
      { ...validPlan, context: { ...(validPlan.context as object), unexpected: true } },
      { ...validPlan, analysisType: "aggregate" },
      {
        ...validPlan,
        filters: [
          { ref: "commerce.gmv", operator: "eq", value: Number.POSITIVE_INFINITY },
        ],
      },
    ];
    const fetchMock = vi.spyOn(globalThis, "fetch");
    for (const logicalPlan of invalidPlans) {
      fetchMock.mockResolvedValueOnce({
        ok: false,
        status: 500,
        statusText: "Internal Server Error",
        json: async () => ({ ...agentFailure(), logical_plan: logicalPlan }),
      } as unknown as Response);
    }
    const api = createApi();

    for (const _ of invalidPlans) {
      await expect(api.sendMessage("conv-1", payload())).rejects.toBeInstanceOf(
        ApiError,
      );
    }
  });

  it("preserves typed Runtime failures after refreshing a 401 response", async () => {
    const failure = agentFailure();
    const refreshedSession = { ...session(), access_token: "refreshed-access-token" };
    let currentSession = session();
    const setSession = vi.fn((nextSession: StoredSession) => {
      currentSession = nextSession;
    });
    const fetchMock = vi.spyOn(globalThis, "fetch");
    fetchMock.mockResolvedValueOnce({
      ok: false,
      status: 401,
      statusText: "Unauthorized",
    } as Response);
    fetchMock.mockResolvedValueOnce({
      ok: true,
      status: 200,
      json: async () => refreshedSession,
    } as Response);
    fetchMock.mockResolvedValueOnce({
      ok: false,
      status: 500,
      statusText: "Internal Server Error",
      json: async () => failure,
    } as unknown as Response);
    const api = new ApiClient({
      getSession: () => currentSession,
      setSession,
      clearSession: vi.fn(),
    });

    const response = await api.sendMessage("conv-1", {
      question: "show gmv",
      enterprise_id: "olist",
      domain_id: "commerce",
      mode: "execute",
      requested_output: "answer",
      include_trace: true,
    });

    expect(response).toEqual(failure);
    expect(setSession).toHaveBeenCalledWith(refreshedSession);
    expect(fetchMock).toHaveBeenCalledTimes(3);
    const [, retriedInit] = fetchMock.mock.calls[2] as [string, RequestInit];
    expect((retriedInit.headers as Headers).get("Authorization")).toBe(
      "Bearer refreshed-access-token",
    );
  });

  it("still throws ApiError for unknown JSON and HTML non-2xx responses", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch");
    fetchMock.mockResolvedValueOnce({
      ok: false,
      status: 500,
      statusText: "Internal Server Error",
      json: async () => ({ detail: "Unknown failure" }),
    } as Response);
    const api = new ApiClient({
      getSession: () => session(),
      setSession: vi.fn(),
      clearSession: vi.fn(),
    });

    await expect(
      api.sendMessage("conv-1", {
        question: "show gmv",
        enterprise_id: "olist",
        domain_id: "commerce",
        mode: "execute",
        requested_output: "answer",
        include_trace: false,
      }),
    ).rejects.toBeInstanceOf(ApiError);

    fetchMock.mockResolvedValueOnce({
      ok: false,
      status: 502,
      statusText: "Bad Gateway",
      json: async () => {
        throw new SyntaxError("HTML response");
      },
    } as unknown as Response);
    await expect(
      api.sendMessage("conv-1", {
        question: "show gmv",
        enterprise_id: "olist",
        domain_id: "commerce",
        mode: "execute",
        requested_output: "answer",
        include_trace: false,
      }),
    ).rejects.toMatchObject({ status: 502, message: "Bad Gateway" });
  });

  it("rejects a malformed near-contract Runtime response", async () => {
    const malformed = {
      ...agentFailure(),
      error: {
        code: "INTERNAL_ERROR",
        message: "The governed run failed safely.",
        retryable: "no",
      },
    };
    vi.spyOn(globalThis, "fetch").mockResolvedValue({
      ok: false,
      status: 500,
      statusText: "Internal Server Error",
      json: async () => malformed,
    } as unknown as Response);
    const api = new ApiClient({
      getSession: () => session(),
      setSession: vi.fn(),
      clearSession: vi.fn(),
    });

    await expect(
      api.sendMessage("conv-1", {
        question: "show gmv",
        enterprise_id: "olist",
        domain_id: "commerce",
        mode: "execute",
        requested_output: "answer",
        include_trace: false,
      }),
    ).rejects.toMatchObject({
      status: 500,
      message: "The governed run failed safely.",
    });
  });
});

function agentFailure(): AgentResponse {
  return structuredClone(agentResponseFixture) as AgentResponse;
}

function snakeCaseKeys(value: unknown): unknown {
  if (Array.isArray(value)) {
    return value.map(snakeCaseKeys);
  }
  if (typeof value !== "object" || value === null) {
    return value;
  }
  return Object.fromEntries(
    Object.entries(value).map(([key, nested]) => [
      key.replace(/[A-Z]/g, (letter) => `_${letter.toLowerCase()}`),
      snakeCaseKeys(nested),
    ]),
  );
}

function createApi(): ApiClient {
  return new ApiClient({
    getSession: () => session(),
    setSession: vi.fn(),
    clearSession: vi.fn(),
  });
}

function payload() {
  return {
    question: "show gmv",
    enterprise_id: "olist",
    domain_id: "commerce",
    mode: "execute" as const,
    requested_output: "answer",
    include_trace: false,
  };
}

function conversation(
  conversationId: string,
  title: string,
  archived = false,
): Conversation {
  return {
    tenant_id: "demo",
    domain_id: "commerce",
    conversation_id: conversationId,
    user_id: "user-1",
    title,
    archived,
    created_at: "2026-06-29T00:00:00Z",
    updated_at: "2026-06-29T00:00:00Z",
  };
}

function session(): StoredSession {
  return {
    access_token: "access-token",
    refresh_token: "refresh-token",
    expires_in: 3600,
    token_type: "bearer",
    user: {
      tenant_id: "demo",
      user_id: "user-1",
      username: "yehj",
      roles: ["user"],
    },
  };
}
