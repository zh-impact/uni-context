-- P1: access-direction trust boundary. Grants extend the default
-- visible_scopes for a given (as_scope, project_id) actor.
--
-- A grant row means: "an actor with as_scope identity (optionally
-- scoped to a specific project_id) is additionally granted access to
-- target_scope data."
--
-- Example: project P's agent is granted read access to user data
--   (as_scope='project', project_id='P', target_scope='user', reason='...')
--
-- Semantics: a grant EXTENDS, never narrows. It can only widen the
-- default visible set defined by visible_scopes() in items/models.py.
-- The default set is the unbreakable floor; grants are its superset.
--
-- Design principle: prefer under-granting (return fewer) over
-- over-granting (leak). The grants table must be used conservatively.
--
-- Scope of fields:
--   as_scope      — only 'project' | 'global'. 'user' is the innermost
--                   layer and sees everything by default, so a user grant
--                   is meaningless and never inserted.
--   project_id    — NULL = the grant applies to ALL projects acting with
--                   this as_scope; non-NULL = only that specific project.
--   target_scope  — the scope whose data the actor may now see
--                   ('user' | 'project' | 'global').
--
-- No FK to project: project_id may be NULL (no FK target for NULL), and
-- keeping it loose avoids coupling access policy to project lifecycle.
-- No updated_at / expires_at: P1 keeps grants boolean-existential;
-- time-bounding is a later concern.

CREATE TABLE IF NOT EXISTS access_grant (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    as_scope      TEXT NOT NULL,
    project_id    TEXT,
    target_scope  TEXT NOT NULL,
    reason        TEXT NOT NULL DEFAULT '',
    created_at    INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_grant_lookup
    ON access_grant(as_scope, project_id);

UPDATE schema_meta SET value = '5' WHERE key = 'schema_version';
