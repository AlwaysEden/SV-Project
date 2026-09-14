CREATE TABLE devices (
    id VARCHAR(10) PRIMARY KEY,
    url VARCHAR(50) NOT NULL,
    status VARCHAR(10) NOT NULL DEFAULT 'OFFLINE'
        CHECK (status IN ('ONLINE', 'OFFLINE')),
    latest_temperature NUMERIC(4,1),
    latest_humidity NUMERIC(4,1),
    last_heartbeat_time TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ
);

CREATE TABLE telemetry (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    device_id VARCHAR(10) NOT NULL,
    seq INTEGER NOT NULL,
    temperature NUMERIC(4,1) NOT NULL,
    humidity NUMERIC(4,1) NOT NULL,
    ts TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE commands (
    cmd_id VARCHAR(20) PRIMARY KEY,
    device_id VARCHAR(10) NOT NULL,
    source VARCHAR(10) NOT NULL,
    type VARCHAR(20) NOT NULL,
    value VARCHAR(10) NOT NULL,
    status VARCHAR(16) NOT NULL DEFAULT 'PENDING'
        CHECK (status IN ('PENDING', 'ACK', 'NACK', 'FAILED')),
    reason VARCHAR(500),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ
);

CREATE TABLE rules (
    device_id VARCHAR(10) PRIMARY KEY,
    threshold NUMERIC(4,1),
    last_action VARCHAR(3) NOT NULL DEFAULT 'OFF'
        CHECK (last_action IN ('ON', 'OFF')),
    updated_at TIMESTAMPTZ
);

CREATE TABLE event_logs(
    seq BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    device_id VARCHAR(10) NOT NULL,
    type VARCHAR(10) NOT NULL
        CHECK (type IN ('ONLINE', 'OFFLINE')),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_telemetry_device_time
    ON telemetry (device_id, ts);

CREATE OR REPLACE FUNCTION set_updated_at()
RETURNS trigger AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_devices_updated_at
BEFORE UPDATE ON devices
FOR EACH ROW
EXECUTE FUNCTION set_updated_at();

CREATE TRIGGER trg_commands_updated_at
BEFORE UPDATE ON commands
FOR EACH ROW
EXECUTE FUNCTION set_updated_at();

CREATE TRIGGER trg_rules_updated_at
BEFORE UPDATE ON rules
FOR EACH ROW
EXECUTE FUNCTION set_updated_at();
