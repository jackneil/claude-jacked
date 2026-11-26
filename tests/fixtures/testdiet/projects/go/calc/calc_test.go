package calc

import (
	"testing"
	"time"
)

func TestPasses(t *testing.T) {
	if Add(1, 1) != 2 {
		t.Fatal("sum")
	}
}

func TestFails(t *testing.T) {
	t.Errorf("sample-failure-message")
}

func TestSkipped(t *testing.T) {
	t.Skip("sample-skip-reason")
}

func TestTable(t *testing.T) {
	for _, value := range []struct {
		name string
		in   int
	}{{"one", 1}, {"two", 2}} {
		t.Run(value.name, func(t *testing.T) {
			if Add(value.in, 0) != value.in {
				t.Fatal("sum")
			}
		})
	}
}

func TestGroup(t *testing.T) {
	t.Run("nested", func(t *testing.T) {
		t.Run("in_nested_group", func(t *testing.T) {})
	})
}

func Test_sleeps_half_a_second(t *testing.T) {
	time.Sleep(500 * time.Millisecond)
}
