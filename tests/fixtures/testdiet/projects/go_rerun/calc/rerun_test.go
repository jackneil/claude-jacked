package calc

import (
	"os"
	"testing"
)

func TestPasses(t *testing.T) {}

// TestFailsThenPasses fails on the first attempt and passes on the second.
// The attempt counter is a file in the package folder. The capture script
// runs the test in a new temporary folder, so the first attempt always fails.
func TestFailsThenPasses(t *testing.T) {
	const marker = "first-attempt.marker"
	if _, err := os.Stat(marker); err != nil {
		if err := os.WriteFile(marker, []byte("1"), 0o600); err != nil {
			t.Fatal(err)
		}
		t.Fatal("sample-first-attempt-failure")
	}
}
