package cli

import (
	"testing"

	"github.com/stretchr/testify/assert"
)

// TestEmbedCmd_HasBackfillAndWorkerSubcommands is a structural test that
// verifies the embed parent command exposes exactly two subcommands:
// backfill (Task 5) and worker (Task 6 wires the RunE; the command itself
// must exist from Task 5 so this guard passes). Prevents accidental
// removal during refactoring.
func TestEmbedCmd_HasBackfillAndWorkerSubcommands(t *testing.T) {
	subs := embedCmd.Commands()
	assert.Equal(t, 2, len(subs), "embed has backfill + worker subcommands")

	names := []string{subs[0].Use, subs[1].Use}
	assert.Contains(t, names, "backfill")
	assert.Contains(t, names, "worker")
}
