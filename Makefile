# WMTM Development Makefile

.PHONY: test test-quick test-verbose benchmark lint clean help

help:  ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "\033[36m%-15s\033[0m %s\n", $$1, $$2}'

test:  ## Run full test suite
	python3 -m pytest -q --tb=short

test-quick:  ## Run tests with no output (fastest)
	python3 -m pytest -q --tb=no

test-verbose:  ## Run tests with full verbosity
	python3 -m pytest -v --tb=long

benchmark:  ## Run WMTM performance benchmarks
	python3 benchmark_wmtm.py

lint:  ## Check for TODO/FIXME/HACK and unused imports
	@echo "=== TODO/FIXME/HACK scan ==="
	@grep -rn 'TODO\|FIXME\|HACK\|XXX\|KLUDGE' --include='*.py' wmtm/ benchmark_wmtm.py iter.py _petta_journal.py test_*.py 2>/dev/null || echo "None found"
	@echo "=== Unused imports scan ==="
	@python3 -c "import ast,os,sys; [print(f'  {f}') for f in os.listdir('.') if f.endswith('.py') and (lambda t: any(isinstance(n,ast.Import) and any(a.name not in open(f).read() for a in n.names) for n in ast.walk(ast.parse(open(f).read()))))(None)]" 2>/dev/null || echo "Scan complete"

clean:  ## Remove __pycache__ and .pyc files
	find . -type d -name '__pycache__' -not -path './.git/*' -exec rm -rf {} + 2>/dev/null
	find . -name '*.pyc' -not -path './.git/*' -delete 2>/dev/null
	@echo "Cleaned __pycache__ and .pyc files"
