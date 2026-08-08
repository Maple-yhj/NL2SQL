BEGIN;

CREATE TABLE IF NOT EXISTS data_agent_conversations (
    tenant_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    domain_id TEXT NOT NULL,
    conversation_id TEXT NOT NULL,
    owner_key TEXT NOT NULL,
    title TEXT NOT NULL DEFAULT '',
    summary TEXT NOT NULL DEFAULT '',
    summary_run_id TEXT,
    status TEXT NOT NULL DEFAULT 'active',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, user_id, domain_id, conversation_id),
    UNIQUE (tenant_id, conversation_id),
    UNIQUE (owner_key),
    CHECK (status IN ('active', 'archived')),
    CHECK (octet_length(summary) <= 16384)
);

CREATE INDEX IF NOT EXISTS idx_data_agent_conversations_owner_updated
    ON data_agent_conversations
       (tenant_id, user_id, domain_id, updated_at DESC);

CREATE TABLE IF NOT EXISTS data_agent_messages (
    message_id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    domain_id TEXT NOT NULL,
    conversation_id TEXT NOT NULL,
    run_id TEXT NOT NULL,
    owner_key TEXT NOT NULL,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    safe_payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    FOREIGN KEY (tenant_id, user_id, domain_id, conversation_id)
        REFERENCES data_agent_conversations
            (tenant_id, user_id, domain_id, conversation_id)
        ON DELETE CASCADE,
    UNIQUE (tenant_id, user_id, domain_id, conversation_id, run_id, role),
    UNIQUE (owner_key, role),
    CHECK (role IN ('user', 'assistant', 'system')),
    CHECK (jsonb_typeof(safe_payload) = 'object'),
    CHECK (octet_length(content) <= 16384)
);

CREATE INDEX IF NOT EXISTS idx_data_agent_messages_owner_created
    ON data_agent_messages
       (tenant_id, user_id, domain_id, conversation_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_data_agent_messages_run
    ON data_agent_messages
       (tenant_id, user_id, domain_id, conversation_id, run_id);

CREATE TABLE IF NOT EXISTS data_agent_artifact_refs (
    artifact_id TEXT NOT NULL,
    tenant_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    domain_id TEXT NOT NULL,
    conversation_id TEXT NOT NULL,
    run_id TEXT NOT NULL,
    owner_key TEXT NOT NULL,
    kind TEXT NOT NULL,
    digest TEXT NOT NULL,
    row_count INTEGER,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY
        (tenant_id, user_id, domain_id, conversation_id, run_id, artifact_id),
    FOREIGN KEY (tenant_id, user_id, domain_id, conversation_id)
        REFERENCES data_agent_conversations
            (tenant_id, user_id, domain_id, conversation_id)
        ON DELETE CASCADE,
    UNIQUE (owner_key, artifact_id),
    CHECK (digest ~ '^[0-9a-f]{64}$'),
    CHECK (row_count IS NULL OR row_count >= 0)
);

CREATE INDEX IF NOT EXISTS idx_data_agent_artifact_refs_owner_run
    ON data_agent_artifact_refs
       (tenant_id, user_id, domain_id, conversation_id, run_id, created_at DESC);

CREATE TABLE IF NOT EXISTS data_agent_memory_proposals (
    proposal_id TEXT PRIMARY KEY,
    candidate_digest TEXT NOT NULL,
    owner_key TEXT NOT NULL,
    tenant_id TEXT NOT NULL,
    scope TEXT NOT NULL,
    user_id TEXT,
    domain_id TEXT,
    conversation_id TEXT,
    run_id TEXT,
    candidate_json JSONB NOT NULL,
    deduplication_key TEXT NOT NULL,
    status TEXT NOT NULL,
    proposed_by TEXT,
    proposed_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL,
    approver_user_id TEXT,
    approver_roles JSONB,
    approval_decision TEXT,
    approval_reason TEXT,
    decided_at TIMESTAMPTZ,
    committed_memory_id TEXT,
    conflict_with JSONB NOT NULL DEFAULT '[]'::jsonb,
    UNIQUE (owner_key, candidate_digest),
    FOREIGN KEY (tenant_id, user_id, domain_id, conversation_id)
        REFERENCES data_agent_conversations
            (tenant_id, user_id, domain_id, conversation_id)
        ON DELETE CASCADE,
    CHECK (scope IN ('working', 'conversation', 'user', 'episodic', 'enterprise')),
    CHECK (status IN (
        'proposed', 'pending_approval', 'approved', 'rejected', 'conflict',
        'policy_rejected', 'committed', 'invalidated'
    )),
    CHECK (jsonb_typeof(candidate_json) = 'object'),
    CHECK (jsonb_typeof(conflict_with) = 'array'),
    CHECK (
        (scope = 'working' AND user_id IS NOT NULL
         AND domain_id IS NOT NULL AND conversation_id IS NOT NULL
         AND run_id IS NOT NULL)
     OR (scope = 'conversation' AND user_id IS NOT NULL
         AND domain_id IS NOT NULL AND conversation_id IS NOT NULL
         AND run_id IS NULL)
     OR (scope = 'user' AND user_id IS NOT NULL
         AND domain_id IS NULL AND conversation_id IS NULL AND run_id IS NULL)
     OR (scope IN ('episodic', 'enterprise') AND domain_id IS NOT NULL
         AND user_id IS NULL AND conversation_id IS NULL AND run_id IS NULL)
    )
);

CREATE INDEX IF NOT EXISTS idx_data_agent_memory_proposals_owner_status
    ON data_agent_memory_proposals
       (owner_key, tenant_id, scope, user_id, domain_id,
        conversation_id, run_id, status);
CREATE INDEX IF NOT EXISTS idx_data_agent_memory_proposals_dedup
    ON data_agent_memory_proposals (owner_key, deduplication_key);

CREATE TABLE IF NOT EXISTS data_agent_memory_records (
    memory_id TEXT PRIMARY KEY,
    proposal_id TEXT NOT NULL,
    owner_key TEXT NOT NULL,
    tenant_id TEXT NOT NULL,
    scope TEXT NOT NULL,
    user_id TEXT,
    domain_id TEXT,
    conversation_id TEXT,
    run_id TEXT,
    content_json JSONB NOT NULL,
    source TEXT NOT NULL,
    evidence_json JSONB,
    trust_level TEXT NOT NULL,
    approval_status TEXT NOT NULL,
    status TEXT NOT NULL,
    sensitivity TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL,
    expires_at TIMESTAMPTZ,
    domain_version TEXT,
    binding_version TEXT,
    schema_fingerprint TEXT,
    deduplication_key TEXT NOT NULL,
    invalidated_at TIMESTAMPTZ,
    invalidation_reason TEXT,
    FOREIGN KEY (proposal_id)
        REFERENCES data_agent_memory_proposals (proposal_id)
        ON DELETE CASCADE,
    FOREIGN KEY (tenant_id, user_id, domain_id, conversation_id)
        REFERENCES data_agent_conversations
            (tenant_id, user_id, domain_id, conversation_id)
        ON DELETE CASCADE,
    UNIQUE (proposal_id),
    CHECK (scope IN ('working', 'conversation', 'user', 'episodic', 'enterprise')),
    CHECK (approval_status IN ('approved', 'committed')),
    CHECK (status IN ('active', 'pending_review', 'invalidated', 'expired')),
    CHECK (trust_level IN ('low', 'medium', 'high', 'verified')),
    CHECK (sensitivity IN ('public', 'internal', 'restricted')),
    CHECK (jsonb_typeof(content_json) = 'object'),
    CHECK (evidence_json IS NULL OR jsonb_typeof(evidence_json) = 'object'),
    CHECK (
        (scope = 'working' AND user_id IS NOT NULL
         AND domain_id IS NOT NULL AND conversation_id IS NOT NULL
         AND run_id IS NOT NULL)
     OR (scope = 'conversation' AND user_id IS NOT NULL
         AND domain_id IS NOT NULL AND conversation_id IS NOT NULL
         AND run_id IS NULL)
     OR (scope = 'user' AND user_id IS NOT NULL
         AND domain_id IS NULL AND conversation_id IS NULL AND run_id IS NULL)
     OR (scope IN ('episodic', 'enterprise') AND domain_id IS NOT NULL
         AND user_id IS NULL AND conversation_id IS NULL AND run_id IS NULL)
    )
);

CREATE INDEX IF NOT EXISTS idx_data_agent_memory_records_recall
    ON data_agent_memory_records
       (owner_key, tenant_id, scope, user_id, domain_id, conversation_id,
        run_id, status, updated_at DESC);
CREATE UNIQUE INDEX IF NOT EXISTS uq_data_agent_memory_records_active_dedup
    ON data_agent_memory_records (owner_key, deduplication_key)
    WHERE status IN ('active', 'pending_review');
CREATE INDEX IF NOT EXISTS idx_data_agent_memory_records_expiry
    ON data_agent_memory_records (expires_at)
    WHERE expires_at IS NOT NULL;

ALTER TABLE data_agent_memory_proposals
    DROP CONSTRAINT IF EXISTS fk_data_agent_proposal_committed_memory;
ALTER TABLE data_agent_memory_proposals
    ADD CONSTRAINT fk_data_agent_proposal_committed_memory
    FOREIGN KEY (committed_memory_id)
    REFERENCES data_agent_memory_records (memory_id)
    ON DELETE SET NULL
    DEFERRABLE INITIALLY DEFERRED;

COMMIT;
