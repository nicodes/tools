package main

import (
	"bytes"
	"context"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestMetadataRefusesAmbiguityWithoutRunningProductCode(t *testing.T) {
	var output bytes.Buffer
	if err := run(context.Background(), []string{"metadata"}, strings.NewReader("updated-dependencies: []\nupdated-dependencies: []\n"), &output); err == nil || output.Len() != 0 {
		t.Fatal("ambiguous metadata produced output", err, output.String())
	}
	if err := run(context.Background(), []string{"metadata"}, strings.NewReader("updated-dependencies: []\n"), &output); err != nil || !strings.Contains(output.String(), `"updated-dependencies":[]`) {
		t.Fatal(err, output.String())
	}
}

func TestExportCannotOverwriteReviewedBytes(t *testing.T) {
	directory := filepath.Join(t.TempDir(), "stage")
	var output bytes.Buffer
	if err := run(context.Background(), []string{"export", "--directory", directory}, strings.NewReader(""), &output); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(directory, "reviewed"), []byte("keep"), 0600); err != nil {
		t.Fatal(err)
	}
	if err := run(context.Background(), []string{"export", "--directory", directory}, strings.NewReader(""), &output); err == nil {
		t.Fatal("overwrote an existing stage")
	}
}
