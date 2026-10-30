// tools-operator manages the optional protected engineering operator.
package main

import (
	"context"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"os"
	"os/signal"
	"syscall"

	"github.com/nicodes/tools/operator"
	bundle "github.com/nicodes/tools/operator/internal/operator"
)

var version = "dev"

func run(ctx context.Context, args []string, input io.Reader, output io.Writer) error {
	if len(args) == 0 {
		return errors.New("usage: tools-operator plan|apply|status|recover|export|metadata|version")
	}
	if args[0] == "version" && len(args) == 1 {
		_, err := fmt.Fprintln(output, version)
		return err
	}
	if args[0] == "metadata" {
		if len(args) != 1 {
			return errors.New("metadata accepts only one bounded YAML document on stdin")
		}
		body, err := io.ReadAll(io.LimitReader(input, 65537))
		if err != nil {
			return err
		}
		value, err := operator.Metadata(body)
		if err != nil {
			return err
		}
		return json.NewEncoder(output).Encode(value)
	}
	fs := flag.NewFlagSet(args[0], flag.ContinueOnError)
	fs.SetOutput(os.Stderr)
	expected := fs.String("expected-plan", "", "reviewed installation plan SHA256; apply only")
	directory := fs.String("directory", "", "new absolute private export directory; export only")
	if err := fs.Parse(args[1:]); err != nil {
		return err
	}
	if fs.NArg() != 0 {
		return errors.New("unexpected positional inputs")
	}
	if args[0] == "export" {
		if *expected != "" {
			return errors.New("export accepts no expected plan")
		}
		if err := bundle.Export(*directory); err != nil {
			return err
		}
		files, err := bundle.Plan()
		if err != nil {
			return err
		}
		return json.NewEncoder(output).Encode(map[string]any{"version": 1, "directory": *directory, "files": files})
	}
	if *directory != "" || args[0] != "apply" && *expected != "" {
		return errors.New("only export accepts a directory; only apply accepts an expected plan")
	}
	return bundle.Lifecycle(ctx, args[0], *expected, output, os.Stderr)
}

func main() {
	ctx, cancel := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer cancel()
	if err := run(ctx, os.Args[1:], os.Stdin, os.Stdout); err != nil {
		fmt.Fprintln(os.Stderr, "error:", err)
		os.Exit(1)
	}
}
