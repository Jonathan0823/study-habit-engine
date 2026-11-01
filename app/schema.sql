CREATE TABLE IF NOT EXISTS operations (
    operation_id uuid PRIMARY KEY,
    source_key text UNIQUE NOT NULL,
    request_hash text NOT NULL,
    status text NOT NULL CHECK (status IN ('accepted', 'rejected')),
    result jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);

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
    progress_revision bigint NOT NULL DEFAULT 0
);
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
    created_at timestamptz NOT NULL DEFAULT now(),
    consumed_at timestamptz
);

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

CREATE TABLE IF NOT EXISTS deliveries (
    delivery_id uuid PRIMARY KEY,
    logical_key text UNIQUE NOT NULL,
    operation_id uuid NOT NULL REFERENCES operations(operation_id),
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
