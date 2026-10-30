.PHONY: check install lint unit build vuln integration

check: install lint unit build vuln

install:
	go -C sessionauth mod download
	go -C pgbudget mod download
	go -C operator mod download

lint:
	bash scripts/lint.sh

unit:
	bash tests/publish-inputs.test.sh
	bash tests/setup-godot-inputs.test.sh
	python3 -m unittest discover -s scripts -p 'test_*.py' -v
	python3 -m unittest discover -s tests -p 'test_*.py' -v
	python3 -m unittest discover -s operator/internal/operator -p 'test_*.py' -v
	go -C operator test -race -count=1 ./...
	go -C sessionauth vet ./...
	go -C sessionauth test -race -count=1 ./...
	go -C pgbudget vet ./...
	go -C pgbudget test -race -count=1 ./...

build:
	go -C sessionauth build ./...
	go -C pgbudget build ./...
	go -C operator build ./...

vuln:
	bash scripts/vuln.sh

integration:
	TOOLS_TEST_POSTGRES=1 python3 -m unittest discover -s tests -p test_postgres_recovery.py -v
