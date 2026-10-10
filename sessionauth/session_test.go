package sessionauth

import (
	"context"
	"crypto/rand"
	"crypto/rsa"
	"strings"
	"testing"
	"time"

	"github.com/clerk/clerk-sdk-go/v2"
	clerkjwt "github.com/clerk/clerk-sdk-go/v2/jwt"
	jose "github.com/go-jose/go-jose/v3"
	"github.com/go-jose/go-jose/v3/jwt"
)

type fixedClock struct{ at time.Time }

func (c fixedClock) Now() time.Time { return c.at }

func TestSignedSessionSecurityContract(t *testing.T) {
	key, err := rsa.GenerateKey(rand.Reader, 2048)
	if err != nil {
		t.Fatal(err)
	}
	other, err := rsa.GenerateKey(rand.Reader, 2048)
	if err != nil {
		t.Fatal(err)
	}
	now := time.Unix(1800000000, 0)
	base := map[string]any{"iss": "https://clerk.example.com", "sub": "user_example", "azp": "https://app.example.com", "exp": now.Add(time.Minute).Unix(), "iat": now.Add(-time.Minute).Unix(), "nbf": now.Add(-time.Minute).Unix()}
	cases := []struct {
		name   string
		mutate func(map[string]any)
		alg    jose.SignatureAlgorithm
		key    *rsa.PrivateKey
		valid  bool
	}{
		{"valid", nil, jose.RS256, key, true},
		{"missing expiration", func(c map[string]any) { delete(c, "exp") }, jose.RS256, key, false},
		{"missing subject", func(c map[string]any) { delete(c, "sub") }, jose.RS256, key, false},
		{"expired", func(c map[string]any) { c["exp"] = now.Add(-6 * time.Second).Unix() }, jose.RS256, key, false},
		{"within skew", func(c map[string]any) { c["exp"] = now.Add(-4 * time.Second).Unix() }, jose.RS256, key, true},
		{"future not before", func(c map[string]any) { c["nbf"] = now.Add(time.Minute).Unix() }, jose.RS256, key, false},
		{"future issued at", func(c map[string]any) { c["iat"] = now.Add(time.Minute).Unix() }, jose.RS256, key, false},
		{"missing party", func(c map[string]any) { delete(c, "azp") }, jose.RS256, key, false},
		{"foreign party", func(c map[string]any) { c["azp"] = "https://foreign.example.com" }, jose.RS256, key, false},
		{"invalid issuer", func(c map[string]any) { c["iss"] = "https://foreign.example.com" }, jose.RS256, key, false},
		{"wrong algorithm", nil, jose.RS512, key, false},
		{"wrong signature", nil, jose.RS256, other, false},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			claims := map[string]any{}
			for k, v := range base {
				claims[k] = v
			}
			if tc.mutate != nil {
				tc.mutate(claims)
			}
			signer, err := jose.NewSigner(jose.SigningKey{Algorithm: tc.alg, Key: tc.key}, (&jose.SignerOptions{}).WithType("JWT").WithHeader("kid", "example"))
			if err != nil {
				t.Fatal(err)
			}
			raw, err := jwt.Signed(signer).Claims(claims).CompactSerialize()
			if err != nil {
				t.Fatal(err)
			}
			verified, err := Verify(context.Background(), &clerkjwt.VerifyParams{Token: raw, JWK: &clerk.JSONWebKey{Key: &key.PublicKey, KeyID: "example", Algorithm: "RS256", Use: "sig"}, Clock: fixedClock{now}, Leeway: 5 * time.Second, AuthorizedPartyHandler: func(p string) bool { return p == "https://app.example.com" }})
			if (err == nil) != tc.valid {
				t.Fatalf("valid=%v error=%v", tc.valid, err)
			}
			if tc.valid && verified.Subject != "user_example" {
				t.Fatal("lost verified identity")
			}
		})
	}
}

func TestRejectsUnboundedOrIncompleteVerificationBeforeNetwork(t *testing.T) {
	for _, params := range []*clerkjwt.VerifyParams{nil, {}, {Token: strings.Repeat("x", 16385)}, {Token: "token", JWK: &clerk.JSONWebKey{}}, {Token: "token", JWK: &clerk.JSONWebKey{}, AuthorizedPartyHandler: func(string) bool { return true }, Leeway: time.Hour}} {
		if _, err := Verify(context.Background(), params); err != ErrInvalidSession {
			t.Fatalf("unexpected incomplete verification: %v", err)
		}
	}
}
