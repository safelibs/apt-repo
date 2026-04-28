PYTHON ?= python3
CONFIG ?= repositories.yml
SITE_DIR ?= site
WORK_DIR ?= .work
BASE_URL ?= https://safelibs.org/apt/

.PHONY: test build-site verify-docker clean

test:
	$(PYTHON) -m unittest discover -s tests -v

build-site:
	$(PYTHON) tools/build_site.py --config $(CONFIG) --output $(SITE_DIR) --workspace $(WORK_DIR) --base-url $(BASE_URL)

verify-docker:
	bash scripts/verify-site.sh $(SITE_DIR) $(CONFIG)

clean:
	rm -rf $(SITE_DIR) $(WORK_DIR)
