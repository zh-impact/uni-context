package sqlite

import (
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"fmt"
	"strconv"
	"strings"
	"time"

	"uni-context/internal/domain"
	"uni-context/internal/port"
)

type ContextRepo struct {
	db *sql.DB
}

func NewContextRepo(db *sql.DB) *ContextRepo {
	return &ContextRepo{db: db}
}

const insertItemSQL = `
INSERT INTO context_item (
    id, scope, kind, source, owner_user_id, project_id, agent_id,
    conversation_id, parent_id, title, summary, content, content_uri,
    content_mime, content_hash, language, tags, source_meta, visibility,
    confidence, word_count, any_embedding, created_at, updated_at, version
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
`

func (r *ContextRepo) Create(ctx context.Context, item domain.ContextItem) error {
	tags, err := json.Marshal(item.Tags)
	if err != nil {
		return fmt.Errorf("marshal tags: %w", err)
	}
	meta, err := json.Marshal(item.SourceMeta)
	if err != nil {
		return fmt.Errorf("marshal source_meta: %w", err)
	}
	_, err = r.db.ExecContext(ctx, insertItemSQL,
		item.ID, string(item.Scope), string(item.Kind), string(item.Source),
		nullable(item.OwnerUserID), nullable(item.ProjectID), nullable(item.AgentID),
		nullable(item.ConversationID), nullable(item.ParentID),
		item.Title, item.Summary, item.Content,
		nullable(item.ContentURI), nullable(item.ContentMIME), nullable(item.ContentHash),
		nullable(item.Language), string(tags), string(meta), string(item.Visibility),
		item.Confidence, item.WordCount, item.AnyEmbedding,
		item.CreatedAt.Unix(), item.UpdatedAt.Unix(), item.Version,
	)
	if err != nil {
		return fmt.Errorf("insert item: %w", err)
	}
	return nil
}

const getItemSQL = `
SELECT id, scope, kind, source, owner_user_id, project_id, agent_id,
       conversation_id, parent_id, title, summary, content, content_uri,
       content_mime, content_hash, language, tags, source_meta, visibility,
       confidence, word_count, any_embedding, created_at, updated_at, version
FROM context_item WHERE id = ?
`

func (r *ContextRepo) Get(ctx context.Context, id string) (domain.ContextItem, error) {
	row := r.db.QueryRowContext(ctx, getItemSQL, id)
	item, err := scanItem(row.Scan)
	if errors.Is(err, sql.ErrNoRows) {
		return domain.ContextItem{}, fmt.Errorf("%w: item %s", domain.ErrNotFound, id)
	}
	return item, err
}

func (r *ContextRepo) Update(ctx context.Context, item domain.ContextItem) error {
	tags, _ := json.Marshal(item.Tags)
	meta, _ := json.Marshal(item.SourceMeta)
	item.Version++
	item.UpdatedAt = item.UpdatedAt.UTC()
	res, err := r.db.ExecContext(ctx, `
        UPDATE context_item SET
            title=?, summary=?, content=?, content_uri=?, content_mime=?,
            content_hash=?, language=?, tags=?, source_meta=?, visibility=?,
            confidence=?, word_count=?, updated_at=?, version=?
        WHERE id=?`,
		item.Title, item.Summary, item.Content,
		nullable(item.ContentURI), nullable(item.ContentMIME), nullable(item.ContentHash),
		nullable(item.Language), string(tags), string(meta), string(item.Visibility),
		item.Confidence, item.WordCount, item.UpdatedAt.Unix(), item.Version, item.ID,
	)
	if err != nil {
		return fmt.Errorf("update item: %w", err)
	}
	n, _ := res.RowsAffected()
	if n == 0 {
		return fmt.Errorf("%w: item %s", domain.ErrNotFound, item.ID)
	}
	return nil
}

func (r *ContextRepo) Delete(ctx context.Context, id string) error {
	res, err := r.db.ExecContext(ctx, `DELETE FROM context_item WHERE id=?`, id)
	if err != nil {
		return fmt.Errorf("delete item: %w", err)
	}
	n, _ := res.RowsAffected()
	if n == 0 {
		return fmt.Errorf("%w: item %s", domain.ErrNotFound, id)
	}
	return nil
}

func (r *ContextRepo) List(ctx context.Context, f port.ItemFilter) ([]domain.ContextItem, string, error) {
	if f.Limit <= 0 || f.Limit > 200 {
		f.Limit = 50
	}

	var (
		where []string
		args  []any
	)
	if len(f.Scopes) > 0 {
		where = append(where, "scope IN ("+placeholders(len(f.Scopes))+")")
		for _, s := range f.Scopes {
			args = append(args, string(s))
		}
	}
	if len(f.Kinds) > 0 {
		where = append(where, "kind IN ("+placeholders(len(f.Kinds))+")")
		for _, k := range f.Kinds {
			args = append(args, string(k))
		}
	}
	if f.OwnerUserID != "" {
		where = append(where, "owner_user_id=?")
		args = append(args, f.OwnerUserID)
	}
	if f.ProjectID != "" {
		where = append(where, "project_id=?")
		args = append(args, f.ProjectID)
	}
	if len(f.Tags) > 0 {
		// OR semantics: an item matches if it carries ANY of the requested
		// tags. Tags are stored as a JSON array, so json_each expands the
		// item's tags into one row per tag and we test membership against
		// the supplied filter set.
		where = append(where, "EXISTS (SELECT 1 FROM json_each(tags) je WHERE je.value IN ("+placeholders(len(f.Tags))+"))")
		for _, t := range f.Tags {
			args = append(args, t)
		}
	}
	if f.Cursor != "" {
		ts, id, err := decodeCursor(f.Cursor)
		if err != nil {
			return nil, "", fmt.Errorf("decode cursor: %w", err)
		}
		where = append(where, "(created_at < ? OR (created_at = ? AND id < ?))")
		args = append(args, ts, ts, id)
	}
	where = append(where, "1=1")

	query := fmt.Sprintf(`
        SELECT id, scope, kind, source, owner_user_id, project_id, agent_id,
               conversation_id, parent_id, title, summary, content, content_uri,
               content_mime, content_hash, language, tags, source_meta, visibility,
               confidence, word_count, any_embedding, created_at, updated_at, version
        FROM context_item
        WHERE %s
        ORDER BY created_at DESC, id DESC
        LIMIT ?`, strings.Join(where, " AND "))
	args = append(args, f.Limit+1) // +1 to detect next page

	rows, err := r.db.QueryContext(ctx, query, args...)
	if err != nil {
		return nil, "", fmt.Errorf("list items: %w", err)
	}
	defer rows.Close()

	var items []domain.ContextItem
	for rows.Next() {
		item, err := scanItem(rows.Scan)
		if err != nil {
			return nil, "", err
		}
		items = append(items, item)
	}
	if err := rows.Err(); err != nil {
		return nil, "", err
	}

	var nextCursor string
	if len(items) > f.Limit {
		items = items[:f.Limit]
		nextCursor = r.NextCursor(items[len(items)-1])
	}
	return items, nextCursor, nil
}

func (r *ContextRepo) NextCursor(item domain.ContextItem) string {
	return encodeCursor(item.CreatedAt.Unix(), item.ID)
}

// --- helpers ---

func nullable(s string) any {
	if s == "" {
		return nil
	}
	return s
}

func placeholders(n int) string {
	return strings.Repeat("?,", n-1) + "?"
}

func timeFromUnix(ts int64) time.Time {
	return time.Unix(ts, 0).UTC()
}

type scanFn func(...any) error

func scanItem(scan scanFn) (domain.ContextItem, error) {
	var (
		item        domain.ContextItem
		scope       string
		kind        string
		source      string
		owner       sql.NullString
		project     sql.NullString
		agent       sql.NullString
		conv        sql.NullString
		parent      sql.NullString
		contentURI  sql.NullString
		contentMIME sql.NullString
		contentHash sql.NullString
		language    sql.NullString
		tags        string
		meta        string
		visibility  string
		createdAt   int64
		updatedAt   int64
	)
	err := scan(
		&item.ID, &scope, &kind, &source, &owner, &project, &agent,
		&conv, &parent, &item.Title, &item.Summary, &item.Content,
		&contentURI, &contentMIME, &contentHash, &language,
		&tags, &meta, &visibility, &item.Confidence, &item.WordCount,
		&item.AnyEmbedding, &createdAt, &updatedAt, &item.Version,
	)
	if err != nil {
		return domain.ContextItem{}, err
	}
	item.Scope = domain.Scope(scope)
	item.Kind = domain.Kind(kind)
	item.Source = domain.Source(source)
	item.OwnerUserID = owner.String
	item.ProjectID = project.String
	item.AgentID = agent.String
	item.ConversationID = conv.String
	item.ParentID = parent.String
	item.ContentURI = contentURI.String
	item.ContentMIME = contentMIME.String
	item.ContentHash = contentHash.String
	item.Language = language.String
	item.Visibility = domain.Visibility(visibility)
	item.CreatedAt = timeFromUnix(createdAt)
	item.UpdatedAt = timeFromUnix(updatedAt)
	if err := json.Unmarshal([]byte(tags), &item.Tags); err != nil {
		return domain.ContextItem{}, fmt.Errorf("unmarshal tags: %w", err)
	}
	if item.Tags == nil {
		item.Tags = []string{}
	}
	if err := json.Unmarshal([]byte(meta), &item.SourceMeta); err != nil {
		return domain.ContextItem{}, fmt.Errorf("unmarshal source_meta: %w", err)
	}
	if item.SourceMeta == nil {
		item.SourceMeta = map[string]any{}
	}
	return item, nil
}

func encodeCursor(ts int64, id string) string {
	// simple "ts:id" base-36 encoded
	return strconv.FormatInt(ts, 36) + ":" + id
}

func decodeCursor(c string) (int64, string, error) {
	parts := strings.SplitN(c, ":", 2)
	if len(parts) != 2 {
		return 0, "", errors.New("malformed cursor")
	}
	ts, err := strconv.ParseInt(parts[0], 36, 64)
	if err != nil {
		return 0, "", err
	}
	return ts, parts[1], nil
}
