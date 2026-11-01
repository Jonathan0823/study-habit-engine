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
