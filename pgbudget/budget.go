// Package pgbudget owns conservative serving database defaults shared by callers.
// Product query/result semantics remain in the product. Migration and offline
// maintenance credentials are deliberately outside this serving-pool contract.
package pgbudget

import (
	"context"
	"fmt"
	"github.com/jackc/pgx/v5/pgxpool"
	"math"
	"regexp"
	"strconv"
	"strings"
	"time"
)

const (
	MaxConnections    int32 = 4
	QueryTimeout            = 15 * time.Second
	LockTimeout             = 3 * time.Second
	MaxCollectionRows       = 1000
	MaxResultBytes          = 4 << 20
	MaxInputBytes           = 1 << 20
)

// Context bounds the whole serving operation, including pool waits, and inherits
// a tighter parent deadline. Callers must defer the returned cancellation.
func Context(parent context.Context) (context.Context, context.CancelFunc) {
	return context.WithTimeout(parent, QueryTimeout)
}

// Configure caps serving resources while preserving every valid tighter DSN
// setting. It refuses unsupported/sub-millisecond timeouts rather than making
// a tighter setting unlimited. A refusal leaves the caller's config unchanged.
func Configure(cfg *pgxpool.Config) error {
	if cfg == nil || cfg.ConnConfig == nil || cfg.MaxConns <= 0 {
		return fmt.Errorf("invalid serving pool configuration")
	}
	timeouts := map[string]string{}
	for name, ceiling := range map[string]time.Duration{"statement_timeout": QueryTimeout, "lock_timeout": LockTimeout} {
		raw := strings.TrimSpace(cfg.ConnConfig.RuntimeParams[name])
		milliseconds, err := TimeoutMilliseconds(raw, ceiling)
		if err != nil {
			return fmt.Errorf("invalid database timeout setting %s", name)
		}
		timeouts[name] = strconv.FormatInt(milliseconds, 10)
	}
	if cfg.MaxConns > MaxConnections {
		cfg.MaxConns = MaxConnections
	}
	if cfg.MinIdleConns > cfg.MaxConns {
		cfg.MinIdleConns = cfg.MaxConns
	}
	if cfg.MinConns > cfg.MaxConns {
		cfg.MinConns = cfg.MaxConns
	}
	if cfg.ConnConfig.ConnectTimeout <= 0 || cfg.ConnConfig.ConnectTimeout > QueryTimeout {
		cfg.ConnConfig.ConnectTimeout = QueryTimeout
	}
	if cfg.ConnConfig.RuntimeParams == nil {
		cfg.ConnConfig.RuntimeParams = map[string]string{}
	}
	for name, value := range timeouts {
		cfg.ConnConfig.RuntimeParams[name] = value
	}
	return nil
}

var postgresDuration = regexp.MustCompile(`^([0-9]+(?:\.[0-9]+)?)\s*(ms|s|min|h|d)?$`)

// TimeoutMilliseconds returns a finite timeout no greater than the ceiling.
func TimeoutMilliseconds(raw string, ceiling time.Duration) (int64, error) {
	if ceiling < time.Millisecond {
		return 0, fmt.Errorf("invalid timeout ceiling")
	}
	if raw == "" {
		return ceiling.Milliseconds(), nil
	}
	match := postgresDuration.FindStringSubmatch(raw)
	if match == nil {
		return 0, fmt.Errorf("unsupported timeout")
	}
	value, err := strconv.ParseFloat(match[1], 64)
	if err != nil || math.IsInf(value, 0) || math.IsNaN(value) {
		return 0, fmt.Errorf("invalid timeout")
	}
	multiplier := map[string]float64{"": 1, "ms": 1, "s": 1000, "min": 60000, "h": 3600000, "d": 86400000}[match[2]]
	ms := value * multiplier
	if ms == 0 || ms > float64(ceiling.Milliseconds()) {
		return ceiling.Milliseconds(), nil
	}
	// PostgreSQL rounds integral timeout units; refuse sub-millisecond values
	// rather than converting a tighter setting to its unlimited value (zero).
	if ms < 1 {
		return 0, fmt.Errorf("timeout below supported resolution")
	}
	return int64(ms), nil
}
