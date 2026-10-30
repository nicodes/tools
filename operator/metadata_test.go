package operator

import (
	"strings"
	"testing"
)

func TestDependencyMetadataRejectsAmbiguousYAMLAndParsesScalarUpdates(t *testing.T) {
	valid := "updated-dependencies:\n- dependency-name: example\n  dependency-version: 1.2.3\n  update-type: version-update:semver-patch\n"
	if value, err := Metadata([]byte(valid)); err != nil || len(value["updated-dependencies"].([]any)) != 1 {
		t.Fatal(value, err)
	}
	for _, bad := range []string{
		"", strings.Repeat("x", 65537), "updated-dependencies: null", "updated-dependencies: {}", "other: []",
		valid + "updated-dependencies: []\n", valid + "---\nupdated-dependencies: []\n",
		"updated-dependencies: &x []", "updated-dependencies: [*x]", "updated-dependencies: !!seq []",
		"updated-dependencies:\n- dependency-name: example\n  dependency-name: other\n",
	} {
		if _, err := Metadata([]byte(bad)); err == nil {
			t.Fatalf("unsafe metadata accepted: %q", bad)
		}
	}
}
