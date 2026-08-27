PREFIX ?= /usr/local
BINDIR ?= $(PREFIX)/bin
SYSTEMD_DIR ?= $(HOME)/.config/systemd/user
PYTHON ?= python3

WIDGETS = logibar-status logibar-keyboard logibar-mouse logibar-headset
DAEMONS = logibar-hidpp-monitor logibar-headset-monitor
TOOLS = tools/logibar-hidpp-battery tools/logibar-hidpp-debug tools/logibar-headset-probe
SERVICES = systemd/logibar-hidpp-monitor.service systemd/logibar-headset-monitor.service
UDEV_RULE = udev/70-logitech-hidraw.rules
UDEV_DIR ?= /etc/udev/rules.d
OMARCHY_PLUGIN_DIR ?= $(HOME)/.config/omarchy/plugins

test:
	$(PYTHON) tests/test_hidpp_monitor.py
	bash tests/test_status.sh
	bash tests/test_legacy.sh
	bash tests/test_hardening.sh

install:
	$(foreach f,$(WIDGETS) $(DAEMONS),install -Dm755 $(f) $(DESTDIR)$(BINDIR)/$(notdir $(f));)

install-tools:
	$(foreach f,$(TOOLS),install -Dm755 $(f) $(DESTDIR)$(BINDIR)/$(notdir $(f));)

install-systemd:
	install -d $(SYSTEMD_DIR)
	$(foreach f,$(SERVICES),install -m644 $(f) $(SYSTEMD_DIR)/$(notdir $(f));)
	sed -i 's|ExecStart=.*|ExecStart=$(BINDIR)/logibar-hidpp-monitor|' $(SYSTEMD_DIR)/logibar-hidpp-monitor.service
	sed -i 's|ExecStart=.*|ExecStart=$(BINDIR)/logibar-headset-monitor|' $(SYSTEMD_DIR)/logibar-headset-monitor.service
	systemctl --user daemon-reload
	systemctl --user enable logibar-hidpp-monitor.service logibar-headset-monitor.service

install-udev:
	install -Dm644 $(UDEV_RULE) $(DESTDIR)$(UDEV_DIR)/$(notdir $(UDEV_RULE))
ifeq ($(strip $(DESTDIR)),)
	udevadm control --reload-rules
	udevadm trigger --action=change --subsystem-match=hidraw
endif

install-all: install install-tools install-systemd

# Symlink (not copy) so repo edits land in the plugin dir; reload the shell
# to pick them up. The plugin runs logibar-status, which reads the daemons'
# state files: both must be installed (make install install-systemd).
install-omarchy:
	@command -v logibar-status >/dev/null 2>&1 || echo "warning: logibar-status not found on PATH — the widget shows an error until it is installed (make install)"
	@command -v logibar-hidpp-monitor >/dev/null 2>&1 || echo "warning: logibar daemons not found on PATH — the widget stays empty until they are installed and running (make install install-systemd)"
	install -d "$(OMARCHY_PLUGIN_DIR)"
	ln -sfT "$(abspath .)" "$(OMARCHY_PLUGIN_DIR)/mryll.logibar"
	@echo 'Linked $(OMARCHY_PLUGIN_DIR)/mryll.logibar'
	@echo 'Now add { "id": "mryll.logibar" } to a bar.layout section in ~/.config/omarchy/shell.json'

uninstall:
	$(foreach f,$(WIDGETS) $(DAEMONS),rm -f $(DESTDIR)$(BINDIR)/$(notdir $(f));)

uninstall-tools:
	$(foreach f,$(TOOLS),rm -f $(DESTDIR)$(BINDIR)/$(notdir $(f));)

uninstall-systemd:
	systemctl --user disable logibar-hidpp-monitor.service logibar-headset-monitor.service || true
	rm -f $(SYSTEMD_DIR)/logibar-hidpp-monitor.service $(SYSTEMD_DIR)/logibar-headset-monitor.service
	systemctl --user daemon-reload

uninstall-udev:
	rm -f $(DESTDIR)$(UDEV_DIR)/$(notdir $(UDEV_RULE))
	rm -f $(DESTDIR)$(UDEV_DIR)/99-logitech-hidraw.rules
ifeq ($(strip $(DESTDIR)),)
	udevadm control --reload-rules
	udevadm trigger --action=change --subsystem-match=hidraw
endif

uninstall-all: uninstall uninstall-tools uninstall-systemd

uninstall-omarchy:
	rm -f "$(OMARCHY_PLUGIN_DIR)/mryll.logibar"

.PHONY: test install install-tools install-systemd install-udev install-all install-omarchy uninstall uninstall-tools uninstall-systemd uninstall-udev uninstall-all uninstall-omarchy
