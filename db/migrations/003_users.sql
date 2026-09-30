-- Local user accounts. Authentication is a signed JWT in an HttpOnly cookie;
-- this table holds the credential material that the JWT proves the bearer
-- once knew. Sessions are stateless: revocation today means rotating the
-- secret key (which invalidates everyone). When we need per-user revocation,
-- add a `revoked_token_jti` or `sessions` table later.

CREATE TABLE IF NOT EXISTS users (
    id             uuid         PRIMARY KEY DEFAULT gen_random_uuid(),
    username       text         NOT NULL UNIQUE,
    password_hash  text         NOT NULL,
    -- Roles for future RBAC. v1 only checks "is logged in". MVP roles:
    --   admin    — full access (CRUD cameras, users, settings)
    --   operator — read all, ack events, no admin
    --   viewer   — read live + events only
    role           text         NOT NULL DEFAULT 'admin'
                                CHECK (role IN ('admin', 'operator', 'viewer')),
    created_at     timestamptz  NOT NULL DEFAULT now(),
    updated_at     timestamptz  NOT NULL DEFAULT now(),
    last_login_at  timestamptz
);

DROP TRIGGER IF EXISTS users_touch ON users;
CREATE TRIGGER users_touch
    BEFORE UPDATE ON users
    FOR EACH ROW EXECUTE FUNCTION touch_updated_at();
