CREATE TABLE IF NOT EXISTS operations (
    operation_id uuid PRIMARY KEY,
    source_key text UNIQUE NOT NULL,
    request_hash text NOT NULL,
    status text NOT NULL CHECK (status IN ('accepted', 'rejected', 'completed')),
    result jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);
ALTER TABLE operations DROP CONSTRAINT IF EXISTS operations_status_check;
ALTER TABLE operations ADD CONSTRAINT operations_status_check
    CHECK (status IN ('accepted', 'rejected', 'completed'));

CREATE TABLE IF NOT EXISTS learning_plan_proposals (
    proposal_id uuid PRIMARY KEY,
    operation_id uuid NOT NULL UNIQUE REFERENCES operations(operation_id),
    goal text NOT NULL CHECK (length(btrim(goal)) BETWEEN 1 AND 2000),
    learner_context jsonb NOT NULL,
    content jsonb NOT NULL,
    content_hash text NOT NULL,
    status text NOT NULL DEFAULT 'pending_review'
        CHECK (status IN ('pending_review','approved','rejected')),
    created_at timestamptz NOT NULL DEFAULT now(),
    decided_at timestamptz
);

CREATE OR REPLACE FUNCTION prevent_learning_plan_proposal_content_mutation() RETURNS trigger AS $$
BEGIN
    IF NEW.proposal_id IS DISTINCT FROM OLD.proposal_id
       OR NEW.operation_id IS DISTINCT FROM OLD.operation_id
       OR NEW.goal IS DISTINCT FROM OLD.goal
       OR NEW.learner_context IS DISTINCT FROM OLD.learner_context
       OR NEW.content IS DISTINCT FROM OLD.content
       OR NEW.content_hash IS DISTINCT FROM OLD.content_hash THEN
        RAISE EXCEPTION 'learning plan proposal content is immutable';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
DROP TRIGGER IF EXISTS learning_plan_proposal_content_immutable ON learning_plan_proposals;
CREATE TRIGGER learning_plan_proposal_content_immutable
    BEFORE UPDATE ON learning_plan_proposals
    FOR EACH ROW EXECUTE FUNCTION prevent_learning_plan_proposal_content_mutation();

CREATE TABLE IF NOT EXISTS telegram_ingestion (
    bot_id bigint NOT NULL,
    update_id bigint NOT NULL,
    operation_id uuid NOT NULL UNIQUE REFERENCES operations(operation_id),
    request_hash text NOT NULL,
    PRIMARY KEY (bot_id, update_id)
);

CREATE TABLE IF NOT EXISTS telegram_cursor (
    bot_id bigint PRIMARY KEY,
    next_offset bigint NOT NULL DEFAULT 0,
    poller_owner uuid,
    fence bigint NOT NULL DEFAULT 0,
    lease_until timestamptz
);

CREATE TABLE IF NOT EXISTS engine_state (
    singleton boolean PRIMARY KEY DEFAULT true CHECK (singleton),
    active_curriculum_id uuid,
    state_revision bigint NOT NULL DEFAULT 0,
    progress_revision bigint NOT NULL DEFAULT 0,
    recovery_epoch uuid NOT NULL DEFAULT gen_random_uuid()
);
ALTER TABLE engine_state ADD COLUMN IF NOT EXISTS recovery_epoch uuid NOT NULL DEFAULT gen_random_uuid();
-- Upgrade legacy numeric epochs to opaque UUIDs. Existing confirmations are
-- invalidated because their numeric epoch cannot match the fresh engine epoch.
DO $$
BEGIN
    IF (SELECT atttypid FROM pg_attribute
        WHERE attrelid='engine_state'::regclass AND attname='recovery_epoch') <> 'uuid'::regtype THEN
        ALTER TABLE engine_state ALTER COLUMN recovery_epoch DROP DEFAULT;
        ALTER TABLE engine_state ALTER COLUMN recovery_epoch TYPE uuid USING gen_random_uuid();
        ALTER TABLE engine_state ALTER COLUMN recovery_epoch SET DEFAULT gen_random_uuid();
    END IF;
END $$;
INSERT INTO engine_state(singleton) VALUES (true) ON CONFLICT DO NOTHING;

CREATE TABLE IF NOT EXISTS curriculum_versions (
    curriculum_id uuid PRIMARY KEY,
    status text NOT NULL CHECK (status IN ('draft', 'approved')),
    content_hash text NOT NULL,
    content jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    approved_at timestamptz
);

CREATE TABLE IF NOT EXISTS curriculum_previews (
    token text PRIMARY KEY,
    curriculum_id uuid NOT NULL REFERENCES curriculum_versions(curriculum_id),
    content_hash text NOT NULL,
    state_revision bigint NOT NULL,
    progress_revision bigint NOT NULL,
    queue_snapshot jsonb NOT NULL DEFAULT '[]'::jsonb,
    mapping jsonb NOT NULL DEFAULT '[]'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    consumed_at timestamptz
);
ALTER TABLE curriculum_previews ADD COLUMN IF NOT EXISTS queue_snapshot jsonb NOT NULL DEFAULT '[]'::jsonb;
ALTER TABLE curriculum_previews ADD COLUMN IF NOT EXISTS mapping jsonb NOT NULL DEFAULT '[]'::jsonb;

CREATE OR REPLACE FUNCTION prevent_curriculum_content_mutation() RETURNS trigger AS $$
BEGIN
    IF TG_OP = 'DELETE' OR NEW.curriculum_id IS DISTINCT FROM OLD.curriculum_id
       OR NEW.content_hash IS DISTINCT FROM OLD.content_hash
       OR NEW.content IS DISTINCT FROM OLD.content THEN
        RAISE EXCEPTION 'imported curriculum content is immutable';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
DROP TRIGGER IF EXISTS curriculum_content_immutable ON curriculum_versions;
CREATE TRIGGER curriculum_content_immutable
    BEFORE UPDATE OR DELETE ON curriculum_versions
    FOR EACH ROW EXECUTE FUNCTION prevent_curriculum_content_mutation();

CREATE TABLE IF NOT EXISTS admin_activation_operations (
    idempotency_key uuid PRIMARY KEY,
    token text NOT NULL UNIQUE REFERENCES curriculum_previews(token),
    request_hash text NOT NULL,
    result jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS admin_idempotency_operations (
    actor_hash text NOT NULL,
    idempotency_key uuid NOT NULL,
    request_method text NOT NULL,
    request_hash text NOT NULL,
    result jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (actor_hash, idempotency_key)
);

CREATE TABLE IF NOT EXISTS study_days (
    study_date date PRIMARY KEY,
    budget_minutes integer NOT NULL DEFAULT 60 CHECK (budget_minutes BETWEEN 0 AND 90),
    queue_revision bigint NOT NULL DEFAULT 0,
    state text NOT NULL DEFAULT 'absent' CHECK (state IN ('absent','valid','replan_required')),
    current_plan_id uuid,
    requested_generation bigint NOT NULL DEFAULT 0,
    routine_suppressed boolean NOT NULL DEFAULT false
);
ALTER TABLE study_days ADD COLUMN IF NOT EXISTS routine_suppressed boolean NOT NULL DEFAULT false;

CREATE TABLE IF NOT EXISTS action_confirmations (
    token text PRIMARY KEY CHECK (octet_length(token) <= 48),
    actor_id bigint NOT NULL,
    chat_id bigint NOT NULL,
    recovery_epoch uuid NOT NULL,
    action text NOT NULL CHECK (action IN ('topic_complete','topic_reopen','block_done','progress_add','progress_edit','progress_reopen')),
    target text NOT NULL,
    study_date date NOT NULL,
    state_revision bigint NOT NULL,
    queue_revision bigint NOT NULL,
    planned_minutes integer,
    expires_at timestamptz NOT NULL,
    consumed_at timestamptz,
    result_code text,
    result_text text,
    context jsonb NOT NULL DEFAULT '{}'::jsonb
);
ALTER TABLE action_confirmations ADD COLUMN IF NOT EXISTS context jsonb NOT NULL DEFAULT '{}'::jsonb;
ALTER TABLE action_confirmations DROP CONSTRAINT IF EXISTS action_confirmations_action_check;
ALTER TABLE action_confirmations ADD CONSTRAINT action_confirmations_action_check
    CHECK (action IN ('topic_complete','topic_reopen','block_done','progress_add','progress_edit','progress_reopen'));
-- Legacy bigint confirmation epochs are replaced with UUIDs during upgrade;
-- old tokens cannot match the new engine epoch.
DO $$
BEGIN
    IF (SELECT atttypid FROM pg_attribute
        WHERE attrelid='action_confirmations'::regclass AND attname='recovery_epoch') <> 'uuid'::regtype THEN
        ALTER TABLE action_confirmations ALTER COLUMN recovery_epoch TYPE uuid USING gen_random_uuid();
    END IF;
END $$;

CREATE TABLE IF NOT EXISTS topic_progress (
    topic_id text PRIMARY KEY,
    completed boolean NOT NULL DEFAULT false,
    study_minutes integer NOT NULL DEFAULT 0 CHECK (study_minutes >= 0)
);

CREATE TABLE IF NOT EXISTS topic_progress_audit (
    audit_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    operation_id uuid NOT NULL REFERENCES operations(operation_id),
    topic_id text NOT NULL,
    before_state jsonb NOT NULL,
    after_state jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE OR REPLACE FUNCTION prevent_topic_progress_audit_mutation() RETURNS trigger AS $$
BEGIN RAISE EXCEPTION 'topic progress audit history is immutable'; END;
$$ LANGUAGE plpgsql;
DROP TRIGGER IF EXISTS topic_progress_audit_immutable ON topic_progress_audit;
CREATE TRIGGER topic_progress_audit_immutable BEFORE UPDATE OR DELETE ON topic_progress_audit
    FOR EACH ROW EXECUTE FUNCTION prevent_topic_progress_audit_mutation();

CREATE TABLE IF NOT EXISTS study_sessions (
    session_id uuid PRIMARY KEY,
    topic_id text NOT NULL,
    study_date date NOT NULL,
    actual_minutes integer NOT NULL CHECK (actual_minutes BETWEEN 0 AND 1440)
);

CREATE TABLE IF NOT EXISTS study_session_audit (
    audit_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    operation_id uuid NOT NULL REFERENCES operations(operation_id),
    session_id uuid NOT NULL,
    action text NOT NULL CHECK (action IN ('add','correct')),
    before_state jsonb,
    after_state jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE OR REPLACE FUNCTION prevent_study_session_audit_mutation() RETURNS trigger AS $$
BEGIN RAISE EXCEPTION 'study session audit history is immutable'; END;
$$ LANGUAGE plpgsql;
DROP TRIGGER IF EXISTS study_session_audit_immutable ON study_session_audit;
CREATE TRIGGER study_session_audit_immutable BEFORE UPDATE OR DELETE ON study_session_audit
    FOR EACH ROW EXECUTE FUNCTION prevent_study_session_audit_mutation();

CREATE TABLE IF NOT EXISTS plans (
    plan_id uuid PRIMARY KEY,
    study_date date NOT NULL REFERENCES study_days(study_date),
    curriculum_id uuid NOT NULL REFERENCES curriculum_versions(curriculum_id),
    generation bigint NOT NULL,
    state_revision bigint NOT NULL,
    queue_revision bigint NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (study_date, generation)
);

CREATE TABLE IF NOT EXISTS calendar_reminders (
    study_date date PRIMARY KEY REFERENCES study_days(study_date),
    plan_id uuid NOT NULL REFERENCES plans(plan_id),
    event_id text NOT NULL,
    topics jsonb NOT NULL CHECK (jsonb_typeof(topics)='array'),
    payload_hash text NOT NULL,
    synced_payload_hash text,
    state text NOT NULL DEFAULT 'pending'
        CHECK (state IN ('pending','synced','cancelled','suppressed','auth_required','failed')),
    ever_synced boolean NOT NULL DEFAULT false,
    first_attempt_at timestamptz,
    retry_until timestamptz,
    next_retry_at timestamptz,
    next_check_at timestamptz,
    attempts integer NOT NULL DEFAULT 0,
    last_error_code text,
    auth_file_mtime double precision,
    updated_at timestamptz NOT NULL DEFAULT now()
);

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'study_days_current_plan_id_fkey'
    ) THEN
        ALTER TABLE study_days
            ADD CONSTRAINT study_days_current_plan_id_fkey
            FOREIGN KEY (current_plan_id) REFERENCES plans(plan_id);
    END IF;
END $$;

CREATE TABLE IF NOT EXISTS study_blocks (
    block_id uuid PRIMARY KEY,
    plan_id uuid NOT NULL REFERENCES plans(plan_id),
    position integer NOT NULL,
    topic_id text NOT NULL,
    planned_minutes integer NOT NULL CHECK (planned_minutes BETWEEN 15 AND 45),
    status text NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','done','skipped','cancelled','expired')),
    UNIQUE (plan_id, position)
);

ALTER TABLE study_sessions ADD COLUMN IF NOT EXISTS block_id uuid REFERENCES study_blocks(block_id);
CREATE UNIQUE INDEX IF NOT EXISTS study_sessions_block_id_unique ON study_sessions(block_id) WHERE block_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS planning_jobs (
    job_id uuid PRIMARY KEY,
    operation_id uuid NOT NULL REFERENCES operations(operation_id),
    study_date date NOT NULL REFERENCES study_days(study_date),
    generation bigint NOT NULL,
    status text NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','running','completed','failed','expired')),
    state_revision bigint,
    curriculum_id uuid,
    lease_owner uuid,
    fence bigint NOT NULL DEFAULT 0,
    lease_until timestamptz,
    result jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (operation_id)
);
CREATE INDEX IF NOT EXISTS planning_jobs_ready ON planning_jobs(status, created_at);
CREATE UNIQUE INDEX IF NOT EXISTS planning_jobs_one_active_per_day
    ON planning_jobs(study_date) WHERE status IN ('pending','running');

CREATE TABLE IF NOT EXISTS planning_job_operations (
    operation_id uuid PRIMARY KEY REFERENCES operations(operation_id),
    job_id uuid NOT NULL REFERENCES planning_jobs(job_id),
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS planning_job_operations_job ON planning_job_operations(job_id);

CREATE TABLE IF NOT EXISTS ai_attempts (
    job_id uuid PRIMARY KEY REFERENCES planning_jobs(job_id),
    study_date date NOT NULL,
    month_start date NOT NULL,
    provider text NOT NULL,
    model text NOT NULL,
    pricing_revision text NOT NULL,
    reserved_usd numeric(12,6) NOT NULL CHECK (reserved_usd >= 0),
    actual_usd numeric(12,6) CHECK (actual_usd >= 0),
    status text NOT NULL CHECK (status IN ('dispatched','finished','uncertain')),
    ranking jsonb,
    error_code text,
    created_at timestamptz NOT NULL DEFAULT now(),
    finished_at timestamptz
);
CREATE INDEX IF NOT EXISTS ai_attempts_daily_limit ON ai_attempts(study_date);
CREATE INDEX IF NOT EXISTS ai_attempts_monthly_budget ON ai_attempts(month_start);

CREATE TABLE IF NOT EXISTS deliveries (
    delivery_id uuid PRIMARY KEY,
    logical_key text UNIQUE NOT NULL,
    operation_id uuid NOT NULL REFERENCES operations(operation_id),
    study_date date REFERENCES study_days(study_date),
    kind text NOT NULL,
    payload jsonb NOT NULL,
    status text NOT NULL CHECK (status IN ('pending', 'leased', 'sent', 'failed')) DEFAULT 'pending',
    attempts integer NOT NULL DEFAULT 0,
    fence bigint NOT NULL DEFAULT 0,
    lease_until timestamptz,
    next_attempt_at timestamptz NOT NULL DEFAULT now(),
    expires_at timestamptz NOT NULL DEFAULT now() + interval '1 hour',
    telegram_receipt text,
    created_at timestamptz NOT NULL DEFAULT now()
);
ALTER TABLE deliveries ADD COLUMN IF NOT EXISTS study_date date REFERENCES study_days(study_date);
CREATE INDEX IF NOT EXISTS deliveries_study_date ON deliveries(study_date,status);
