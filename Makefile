.PHONY: check install lint unit build vuln

check: install lint unit build vuln

install:
	go -C sessionauth mod download
	go -C pgbudget mod download

lint:
	bash scripts/lint.sh

unit:
	python3 -m unittest discover -s scripts -p 'test_*.py' -v
	python3 -m unittest discover -s tests -p 'test_*.py' -v
	go -C sessionauth vet ./...
	go -C sessionauth test -race -count=1 ./...
	go -C pgbudget vet ./...
	go -C pgbudget test -race -count=1 ./...

build:
	go -C sessionauth build ./...
	go -C pgbudget build ./...

vuln:
	bash scripts/vuln.sh
