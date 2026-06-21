package cli

import (
	"testing"

	"github.com/spf13/cobra"
	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
)

// TestEmbedCmd_HasExpectedSubcommands is a structural test that verifies
// the embed parent command exposes its expected subcommands. Uses
// assert.Contains (not a count) so future tasks adding `switch`/`reembed`
// don't break this test.
func TestEmbedCmd_HasExpectedSubcommands(t *testing.T) {
	subs := embedCmd.Commands()
	names := []string{}
	for _, c := range subs {
		names = append(names, c.Use)
	}
	for _, want := range []string{"backfill", "worker", "model"} {
		assert.Contains(t, names, want, "embed must expose %q subcommand", want)
	}
}

// TestEmbedModelCmd_AddParsesFlags is a structural test that confirms the
// expected flags exist on the `embed model add` subcommand.
func TestEmbedModelCmd_AddParsesFlags(t *testing.T) {
	sub := findSub(embedCmd, "model")
	require.NotNil(t, sub)
	addSub := findSub(sub, "add")
	require.NotNil(t, addSub)

	for _, flag := range []string{"provider", "base-url", "dim", "api-key"} {
		assert.NotNil(t, addSub.Flags().Lookup(flag),
			"add command must expose --%s flag", flag)
	}
}

// findSub returns the direct child of parent whose Name() matches, or nil
// if no such child exists. Uses cobra's Name() (the first token of Use)
// rather than the full Use string, so a command declared as
// `Use: "add <slug>"` is found by findSub(parent, "add").
func findSub(parent *cobra.Command, name string) *cobra.Command {
	for _, c := range parent.Commands() {
		if c.Name() == name {
			return c
		}
	}
	return nil
}
