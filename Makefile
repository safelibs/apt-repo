PYTHON ?= python3
CONFIG ?= repositories.yml
SITE_DIR ?= site
WORK_DIR ?= .work
BASE_URL ?= https://safelibs.github.io/apt/
PORTS_ROOT ?= ..

.PHONY: test build-site verify-docker clean generate-port-ci check-port-ci

test:
	$(PYTHON) -m unittest discover -s tests -v

build-site:
	$(PYTHON) tools/build_site.py --config $(CONFIG) --output $(SITE_DIR) --workspace $(WORK_DIR) --base-url $(BASE_URL)

verify-docker:
	bash scripts/verify-site.sh $(SITE_DIR) $(CONFIG)

generate-port-ci:
	$(PYTHON) tools/generate_port_ci.py --config $(CONFIG) --ports-root $(PORTS_ROOT)

check-port-ci:
	$(PYTHON) tools/generate_port_ci.py --config $(CONFIG) --ports-root $(PORTS_ROOT) --dry-run

clean:
	rm -rf $(SITE_DIR) $(WORK_DIR)
