package operator

import (
	"bytes"
	"errors"
	"go.yaml.in/yaml/v3"
	"io"
)

func Metadata(body []byte) (map[string]any, error) {
	if len(body) == 0 || len(body) > 65536 {
		return nil, errors.New("dependency metadata must contain at most 64 KiB")
	}
	var document yaml.Node
	decoder := yaml.NewDecoder(bytes.NewReader(body))
	if err := decoder.Decode(&document); err != nil {
		return nil, errors.New("invalid dependency metadata YAML")
	}
	if decoder.Decode(new(yaml.Node)) != io.EOF {
		return nil, errors.New("dependency metadata must contain one document")
	}
	nodes := 0
	var visit func(*yaml.Node, int) error
	visit = func(node *yaml.Node, depth int) error {
		nodes++
		if nodes > 4096 || depth > 16 || node.Kind == yaml.AliasNode || node.Anchor != "" || node.Style&yaml.TaggedStyle != 0 {
			return errors.New("unsupported dependency metadata indirection or nesting")
		}
		if node.Kind == yaml.MappingNode {
			seen := map[string]bool{}
			for i := 0; i < len(node.Content); i += 2 {
				key := node.Content[i]
				if key.Kind != yaml.ScalarNode || key.Tag != "!!str" || key.Value == "<<" || seen[key.Value] {
					return errors.New("duplicate or invalid dependency metadata key")
				}
				seen[key.Value] = true
			}
		}
		for _, child := range node.Content {
			if err := visit(child, depth+1); err != nil {
				return err
			}
		}
		return nil
	}
	if err := visit(&document, 0); err != nil {
		return nil, err
	}
	var value map[string]any
	if document.Decode(&value) != nil || len(value) != 1 || value["updated-dependencies"] == nil {
		return nil, errors.New("unexpected dependency metadata fields")
	}
	if _, ok := value["updated-dependencies"].([]any); !ok {
		return nil, errors.New("dependency updates must be an array")
	}
	return value, nil
}
