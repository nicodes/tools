package pgbudget

import (
	"context"
	"github.com/jackc/pgx/v5/pgxpool"
	"reflect"
	"testing"
	"time"
)

func TestServingConfigurationPreservesTighterDSNAndRefusesAtomically(t *testing.T) {
	cfg, err := pgxpool.ParseConfig("postgres://fixture:fixture@localhost/fixture?pool_max_conns=2&connect_timeout=1&statement_timeout=0.5s&lock_timeout=250ms")
	if err != nil {
		t.Fatal(err)
	}
	if err := Configure(cfg); err != nil {
		t.Fatal(err)
	}
	if cfg.MaxConns != 2 || cfg.ConnConfig.ConnectTimeout != time.Second || cfg.ConnConfig.RuntimeParams["statement_timeout"] != "500" || cfg.ConnConfig.RuntimeParams["lock_timeout"] != "250" {
		t.Fatal("tighter DSN was widened", cfg)
	}
	cfg.MaxConns = 100
	cfg.ConnConfig.RuntimeParams["lock_timeout"] = "0.1ms"
	before := cfg.Copy()
	if Configure(cfg) == nil {
		t.Fatal("unlimited rounded timeout accepted")
	}
	if cfg.MaxConns != before.MaxConns || cfg.MinConns != before.MinConns || cfg.MinIdleConns != before.MinIdleConns || cfg.ConnConfig.ConnectTimeout != before.ConnConfig.ConnectTimeout || !reflect.DeepEqual(cfg.ConnConfig.RuntimeParams, before.ConnConfig.RuntimeParams) {
		t.Fatal("failed configuration changed caller values")
	}
	cfg.ConnConfig.RuntimeParams["lock_timeout"] = "0"
	cfg.ConnConfig.RuntimeParams["statement_timeout"] = "2min"
	cfg.MinConns = 90
	cfg.MinIdleConns = 80
	cfg.ConnConfig.ConnectTimeout = 0
	if err := Configure(cfg); err != nil {
		t.Fatal(err)
	}
	if cfg.MaxConns != 4 || cfg.MinConns != 4 || cfg.MinIdleConns != 4 || cfg.ConnConfig.ConnectTimeout != QueryTimeout || cfg.ConnConfig.RuntimeParams["statement_timeout"] != "15000" || cfg.ConnConfig.RuntimeParams["lock_timeout"] != "3000" {
		t.Fatal("unbounded config survived")
	}
}

func TestDurationInputAndParentDeadline(t *testing.T) {
	for _, raw := range []string{"-1", "NaN", "Infinity", "0.001ms", "1e9", "1sec"} {
		if _, err := TimeoutMilliseconds(raw, time.Second); err == nil {
			t.Fatalf("accepted invalid timeout %q", raw)
		}
	}
	for raw, want := range map[string]int64{"": 1000, "0": 1000, "2s": 1000, ".5s": 0, "0.25s": 250, "1ms": 1, "0.001s": 1} {
		got, err := TimeoutMilliseconds(raw, time.Second)
		if want == 0 {
			if err == nil {
				t.Fatalf("unexpected supported syntax %q", raw)
			}
			continue
		}
		if err != nil || got != want {
			t.Fatalf("%q: got %d %v want %d", raw, got, err, want)
		}
	}
	parent, cancel := context.WithTimeout(context.Background(), 10*time.Millisecond)
	defer cancel()
	child, stop := Context(parent)
	defer stop()
	p, _ := parent.Deadline()
	c, _ := child.Deadline()
	if c.After(p) {
		t.Fatal("parent deadline widened")
	}
	<-child.Done()
	if child.Err() != context.DeadlineExceeded {
		t.Fatal(child.Err())
	}
}
