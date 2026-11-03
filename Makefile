.PHONY: check install lint unit test build vuln integration clean

check: install lint unit build vuln integration

test: unit integration

install:
	rustup component add --toolchain 1.94.0 rustfmt clippy
	cargo fetch --locked

lint:
	bash scripts/lint.sh

unit:
	cargo test --workspace --all-targets --locked
	cargo build --locked -p tools-operator
	bash tests/publish-inputs.test.sh
	bash tests/setup-godot-inputs.test.sh
	python3 -m unittest discover -s scripts -p 'test_*.py' -v
	python3 -m unittest discover -s tests -p 'test_*.py' -v
	python3 -m unittest discover -s crates/operator/internal/operator -p 'test_*.py' -v

build:
	cargo build --workspace --locked

vuln:
	bash scripts/vuln.sh

integration:
	TOOLS_TEST_POSTGRES=1 python3 -m unittest discover -s tests -p test_postgres_recovery.py -v

clean:
	python3 helpers/clean.py --path .artifacts/rust-target --path .artifacts/native-operator --path dist
