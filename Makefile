PKG_NAME     ?= iss-equipment-utilities
PKG_ENG_NAME ?= iss-equipment-utilities-eng
VERSION      ?= 1.1
ARCH         ?= all
BUILD_DIR     = build/$(PKG_NAME)_$(VERSION)-1_$(ARCH)
BUILD_ENG_DIR = build/$(PKG_ENG_NAME)_$(VERSION)-1_$(ARCH)

all: deb

# ==========================================================================
# Clean
# ==========================================================================
clean:
	rm -rf build/
	rm -f *.deb

# ==========================================================================
# Official Release
# Packages: iss_pdu_utility, iss_terminal_utility, iss_daq_utility
# No mock, no simulator, no trial tools.
# ==========================================================================
deb: clean
	mkdir -p $(BUILD_DIR)/DEBIAN
	mkdir -p $(BUILD_DIR)/opt/$(PKG_NAME)
	mkdir -p $(BUILD_DIR)/usr/bin

	# Copy official source files (without bytecode caches or Finder metadata from the build machine)
	cp -r equipment_drivers $(BUILD_DIR)/opt/$(PKG_NAME)/
	find $(BUILD_DIR) -name __pycache__ -prune -exec rm -rf {} +
	find $(BUILD_DIR) -name .DS_Store -delete
	# Remove simulator and the CLIs built on it from official package — engineering only
	rm -f $(BUILD_DIR)/opt/$(PKG_NAME)/equipment_drivers/simulator.py
	rm -f $(BUILD_DIR)/opt/$(PKG_NAME)/equipment_drivers/cli/trial.py
	rm -f $(BUILD_DIR)/opt/$(PKG_NAME)/equipment_drivers/cli/mock_server.py

	cp iss_pdu_utility     $(BUILD_DIR)/opt/$(PKG_NAME)/
	cp iss_terminal_utility $(BUILD_DIR)/opt/$(PKG_NAME)/
	cp iss_daq_utility     $(BUILD_DIR)/opt/$(PKG_NAME)/

	# Symlinks in /usr/bin
	ln -s /opt/$(PKG_NAME)/iss_pdu_utility     $(BUILD_DIR)/usr/bin/iss_pdu_utility
	ln -s /opt/$(PKG_NAME)/iss_terminal_utility $(BUILD_DIR)/usr/bin/iss_terminal_utility
	ln -s /opt/$(PKG_NAME)/iss_daq_utility     $(BUILD_DIR)/usr/bin/iss_daq_utility

	# DEBIAN/control
	echo "Package: $(PKG_NAME)"                                      > $(BUILD_DIR)/DEBIAN/control
	echo "Version: $(VERSION)-1"                                    >> $(BUILD_DIR)/DEBIAN/control
	echo "Architecture: $(ARCH)"                                    >> $(BUILD_DIR)/DEBIAN/control
	echo "Maintainer: Equipment Admin <admin@example.com>"          >> $(BUILD_DIR)/DEBIAN/control
	echo "Description: ISS Equipment Utilities (Official Release)"  >> $(BUILD_DIR)/DEBIAN/control
	echo "Depends: python3 (>= 3.10), python3-requests, python3-paramiko" >> $(BUILD_DIR)/DEBIAN/control

	dpkg-deb --build $(BUILD_DIR)
	mv build/*.deb .
	@echo ""
	@echo ">>> Official package built: $(PKG_NAME)_$(VERSION)-1_$(ARCH).deb"
	@echo "    Includes: iss_pdu_utility, iss_terminal_utility, iss_daq_utility"
	@echo "    Excludes: iss_trial_utility, iss_mock_server, iss_pdu_utility_eng, simulator.py"

# ==========================================================================
# Engineering Release
# Packages everything in official, PLUS:
#   - iss_pdu_utility_eng  (with --mock / --model flags)
#   - iss_trial_utility    (9-phase blackbox mock trial tool)
#   - iss_mock_server      (standalone mock HTTP server)
#   - simulator.py         (in-process mock server used by the above)
# ==========================================================================
deb-engineering: clean
	mkdir -p $(BUILD_ENG_DIR)/DEBIAN
	mkdir -p $(BUILD_ENG_DIR)/opt/$(PKG_ENG_NAME)
	mkdir -p $(BUILD_ENG_DIR)/usr/bin

	# Copy all source files including simulator (without bytecode caches or Finder metadata)
	cp -r equipment_drivers $(BUILD_ENG_DIR)/opt/$(PKG_ENG_NAME)/
	find $(BUILD_ENG_DIR) -name __pycache__ -prune -exec rm -rf {} +
	find $(BUILD_ENG_DIR) -name .DS_Store -delete

	# Official utilities
	cp iss_pdu_utility      $(BUILD_ENG_DIR)/opt/$(PKG_ENG_NAME)/
	cp iss_terminal_utility $(BUILD_ENG_DIR)/opt/$(PKG_ENG_NAME)/
	cp iss_daq_utility      $(BUILD_ENG_DIR)/opt/$(PKG_ENG_NAME)/

	# Engineering-only utilities
	cp iss_pdu_utility_eng  $(BUILD_ENG_DIR)/opt/$(PKG_ENG_NAME)/
	cp iss_trial_utility    $(BUILD_ENG_DIR)/opt/$(PKG_ENG_NAME)/
	cp iss_mock_server      $(BUILD_ENG_DIR)/opt/$(PKG_ENG_NAME)/

	# Symlinks in /usr/bin (official)
	ln -s /opt/$(PKG_ENG_NAME)/iss_pdu_utility      $(BUILD_ENG_DIR)/usr/bin/iss_pdu_utility
	ln -s /opt/$(PKG_ENG_NAME)/iss_terminal_utility  $(BUILD_ENG_DIR)/usr/bin/iss_terminal_utility
	ln -s /opt/$(PKG_ENG_NAME)/iss_daq_utility       $(BUILD_ENG_DIR)/usr/bin/iss_daq_utility
	# Symlinks in /usr/bin (engineering extras)
	ln -s /opt/$(PKG_ENG_NAME)/iss_pdu_utility_eng   $(BUILD_ENG_DIR)/usr/bin/iss_pdu_utility_eng
	ln -s /opt/$(PKG_ENG_NAME)/iss_trial_utility     $(BUILD_ENG_DIR)/usr/bin/iss_trial_utility
	ln -s /opt/$(PKG_ENG_NAME)/iss_mock_server       $(BUILD_ENG_DIR)/usr/bin/iss_mock_server

	# DEBIAN/control
	echo "Package: $(PKG_ENG_NAME)"                                                       > $(BUILD_ENG_DIR)/DEBIAN/control
	echo "Version: $(VERSION)-1"                                                         >> $(BUILD_ENG_DIR)/DEBIAN/control
	echo "Architecture: $(ARCH)"                                                         >> $(BUILD_ENG_DIR)/DEBIAN/control
	echo "Maintainer: Equipment Admin <admin@example.com>"                               >> $(BUILD_ENG_DIR)/DEBIAN/control
	echo "Description: ISS Equipment Utilities (Engineering Release — includes mock/trial tools)" >> $(BUILD_ENG_DIR)/DEBIAN/control
	echo "Depends: python3 (>= 3.10), python3-requests, python3-paramiko"                >> $(BUILD_ENG_DIR)/DEBIAN/control
	echo "Recommends: openssl"                                                           >> $(BUILD_ENG_DIR)/DEBIAN/control

	dpkg-deb --build $(BUILD_ENG_DIR)
	mv build/*.deb .
	@echo ""
	@echo ">>> Engineering package built: $(PKG_ENG_NAME)_$(VERSION)-1_$(ARCH).deb"
	@echo "    Includes all official tools PLUS:"
	@echo "    + iss_pdu_utility_eng  (--mock / --model flags)"
	@echo "    + iss_trial_utility    (9-phase blackbox mock trial)"
	@echo "    + iss_mock_server      (standalone mock HTTP server)"
	@echo "    + simulator.py         (in-process mock engine)"

# ==========================================================================
# Docker helpers (builds inside Ubuntu to produce a valid .deb on macOS)
# ==========================================================================
deb-docker:
	docker run --rm -v $$(pwd):/app -w /app ubuntu:latest \
		bash -c "apt-get update && apt-get install -y make build-essential dpkg-dev && make deb"

deb-engineering-docker:
	docker run --rm -v $$(pwd):/app -w /app ubuntu:latest \
		bash -c "apt-get update && apt-get install -y make build-essential dpkg-dev && make deb-engineering"

# ==========================================================================
# GitHub Release (Official and Engineering .deb)
# ==========================================================================
release: deb deb-engineering
	@echo "Publishing GitHub release for v$(VERSION)..."
	@if ! command -v gh > /dev/null 2>&1; then echo "Error: GitHub CLI (gh) is not installed."; exit 1; fi
	@if gh release view v$(VERSION) > /dev/null 2>&1; then \
		echo "Release v$(VERSION) already exists. Uploading and updating .deb assets..."; \
		gh release upload --clobber v$(VERSION) \
			$(PKG_NAME)_$(VERSION)-1_$(ARCH).deb \
			$(PKG_ENG_NAME)_$(VERSION)-1_$(ARCH).deb; \
	else \
		echo "Creating new GitHub release v$(VERSION)..."; \
		gh release create v$(VERSION) \
			$(PKG_NAME)_$(VERSION)-1_$(ARCH).deb \
			$(PKG_ENG_NAME)_$(VERSION)-1_$(ARCH).deb \
			--title "Release v$(VERSION)" --notes "Automated release of v$(VERSION) (Official & Engineering)"; \
	fi
# ==========================================================================
# Protobuf codegen
# ==========================================================================
proto:
	python3 -m grpc_tools.protoc \
		-I proto \
		--python_out=equipment_drivers/pb \
		--pyi_out=equipment_drivers/pb \
		proto/equipment/v1/equipment.proto
