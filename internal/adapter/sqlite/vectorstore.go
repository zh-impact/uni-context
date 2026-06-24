package sqlite

import (
	"context"
	"database/sql"
	"fmt"

	"uni-context/internal/port"

	sqlite_vec "github.com/asg017/sqlite-vec-go-bindings/cgo"
)

type VectorStore struct {
	db *sql.DB
}

func NewVectorStore(db *sql.DB) *VectorStore {
	return &VectorStore{db: db}
}

// vecTableName resolves the embedding_model.vec_table column for a
// given slug. Returns error if the model isn't registered.
const vecTableSQL = `SELECT vec_table FROM embedding_model WHERE slug = ?`

func (s *VectorStore) vecTable(ctx context.Context, model string) (string, error) {
	var name string
	err := s.db.QueryRowContext(ctx, vecTableSQL, model).Scan(&name)
	if err != nil {
		return "", fmt.Errorf("lookup vec table for model %s: %w", model, err)
	}
	return name, nil
}

func (s *VectorStore) Put(ctx context.Context, model, itemID string, vector []float32) error {
	table, err := s.vecTable(ctx, model)
	if err != nil {
		return err
	}
	blob, err := sqlite_vec.SerializeFloat32(vector)
	if err != nil {
		return fmt.Errorf("serialize vector: %w", err)
	}
	// vec0 does NOT support INSERT OR REPLACE on its TEXT PK (it errors
	// with "UNIQUE constraint failed on ... primary key"). Use
	// DELETE + INSERT in a tx for idempotency.
	tx, err := s.db.BeginTx(ctx, nil)
	if err != nil {
		return fmt.Errorf("begin tx: %w", err)
	}
	defer func() { _ = tx.Rollback() }() // safe: no-op after Commit

	if _, err = tx.ExecContext(ctx,
		fmt.Sprintf(`DELETE FROM %s WHERE item_id = ?`, table), itemID); err != nil {
		return fmt.Errorf("delete existing vector: %w", err)
	}
	if _, err = tx.ExecContext(ctx,
		fmt.Sprintf(`INSERT INTO %s (item_id, embedding) VALUES (?, ?)`, table),
		itemID, blob); err != nil {
		return fmt.Errorf("put vector: %w", err)
	}
	if err = tx.Commit(); err != nil {
		return fmt.Errorf("commit put vector: %w", err)
	}
	return nil
}

// Search runs a KNN query with optional scope/kind filter pushed down via JOIN.
// vec0 KNN syntax: `SELECT ... FROM vec_tbl WHERE embedding MATCH ?
// ORDER BY distance LIMIT ?`. We JOIN to context_item to push filters
// down and to fetch nothing else (caller hydrates via repo.Get).
func (s *VectorStore) Search(ctx context.Context, q port.VectorQuery) ([]port.VectorHit, error) {
	table, err := s.vecTable(ctx, q.Model)
	if err != nil {
		return nil, err
	}
	blob, err := sqlite_vec.SerializeFloat32(q.Vector)
	if err != nil {
		return nil, fmt.Errorf("serialize query vector: %w", err)
	}
	if q.Limit <= 0 || q.Limit > 200 {
		q.Limit = 20
	}

	// q.Limit is used directly as the KNN k. The service layer
	// (searchHybrid) is responsible for over-fetch — it passes
	// Limit=limit*3 from the orchestration layer per spec §5.2.
	// VectorStore must NOT multiply again: scope/kind filters are pushed
	// down via JOIN context_item, so there is no post-filter inside this
	// function that would require headroom. (Prior to this fix,
	// fetchN=q.Limit*3 made the effective k = limit*9 at the service
	// layer — 180 KNN rows + 180 repo.Get calls for default limit=20.)
	var (
		filterSQL string
		args      []any
	)
	// vec0 KNN: `... WHERE embedding MATCH ? [AND <filters>] ORDER BY
	// distance LIMIT ?`. The MATCH clause is mandatory — without it vec0
	// does a normal scan and `distance` isn't a valid column, producing
	// "datatype mismatch". We push scope/kind filters into the same WHERE.
	args = append(args, blob)
	if len(q.Scopes) > 0 || len(q.Kinds) > 0 {
		clauses := []string{}
		if len(q.Scopes) > 0 {
			clauses = append(clauses, "ci.scope IN ("+placeholders(len(q.Scopes))+")")
			for _, sc := range q.Scopes {
				args = append(args, sc)
			}
		}
		if len(q.Kinds) > 0 {
			clauses = append(clauses, "ci.kind IN ("+placeholders(len(q.Kinds))+")")
			for _, k := range q.Kinds {
				args = append(args, k)
			}
		}
		filterSQL = " AND " + joinAnd(clauses)
	}
	args = append(args, q.Limit)
	query := fmt.Sprintf(`
        SELECT v.item_id, v.distance
        FROM %s v
        JOIN context_item ci ON ci.id = v.item_id
        WHERE v.embedding MATCH ?%s AND k = ?
        ORDER BY v.distance
    `, table, filterSQL)

	rows, err := s.db.QueryContext(ctx, query, args...)
	if err != nil {
		return nil, fmt.Errorf("vector search: %w", err)
	}
	defer rows.Close()

	var hits []port.VectorHit
	for rows.Next() {
		var h port.VectorHit
		if err := rows.Scan(&h.ID, &h.Distance); err != nil {
			return nil, err
		}
		// Convert distance (lower=better) to score (higher=better).
		// cosine distance ∈ [0, 2]; score = 1 - distance/2 ∈ [0, 1].
		h.Score = 1.0 - h.Distance/2.0
		hits = append(hits, h)
	}
	return hits, rows.Err()
}

func (s *VectorStore) Delete(ctx context.Context, model, itemID string) error {
	table, err := s.vecTable(ctx, model)
	if err != nil {
		return err
	}
	_, err = s.db.ExecContext(ctx,
		fmt.Sprintf(`DELETE FROM %s WHERE item_id = ?`, table), itemID)
	if err != nil {
		return fmt.Errorf("delete vector: %w", err)
	}
	return nil
}

func joinAnd(parts []string) string {
	out := ""
	for i, p := range parts {
		if i > 0 {
			out += " AND "
		}
		out += p
	}
	return out
}
