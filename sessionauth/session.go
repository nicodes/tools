// Package sessionauth defines the mandatory session verification contract.
// Callers own issuer and origin policy, bounded key retrieval and error mapping.
package sessionauth

import (
	"context"
	"errors"
	"time"

	"github.com/clerk/clerk-sdk-go/v2"
	"github.com/clerk/clerk-sdk-go/v2/jwt"
)

var ErrInvalidSession = errors.New("missing or invalid session claims")

// Verify preserves the SDK's signature, algorithm, time and authorized-party
// checks, then requires claims that the SDK otherwise treats as optional.
// Issuer pinning remains caller-owned because tenant policy is not universal.
func Verify(ctx context.Context, params *jwt.VerifyParams) (*clerk.SessionClaims, error) {
	if params == nil || params.JWK == nil || params.AuthorizedPartyHandler == nil || len(params.Token) == 0 || len(params.Token) > 16384 || params.Leeway < 0 || params.Leeway > 5*time.Second {
		return nil, ErrInvalidSession
	}
	claims, err := jwt.Verify(ctx, params)
	if err != nil {
		return nil, err
	}
	if claims.Subject == "" || claims.Expiry == nil {
		return nil, ErrInvalidSession
	}
	return claims, nil
}
