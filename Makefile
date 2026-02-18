PREFIX ?= /usr/local
BINDIR ?= $(PREFIX)/bin
SYSTEMD_DIR ?= $(HOME)/.config/systemd/user

WIDGETS = logitech-battery-keyboard logitech-battery-mouse logitech-battery-headset
DAEMONS = logitech-hidpp-monitor logitech-headset-monitor
TOOLS = tools/hidpp-battery tools/hidpp-battery-debug tools/headset-battery-probe
SERVICES = systemd/logitech-hidpp-monitor.service systemd/logitech-headset-monitor.service

install:
	$(foreach f,$(WIDGETS) $(DAEMONS),install -Dm755 $(f) $(DESTDIR)$(BINDIR)/$(notdir $(f));)

install-tools:
	$(foreach f,$(TOOLS),install -Dm755 $(f) $(DESTDIR)$(BINDIR)/$(notdir $(f));)

install-systemd:
	install -d $(SYSTEMD_DIR)
	$(foreach f,$(SERVICES),install -m644 $(f) $(SYSTEMD_DIR)/$(notdir $(f));)
	sed -i 's|ExecStart=.*|ExecStart=$(BINDIR)/logitech-hidpp-monitor|' $(SYSTEMD_DIR)/logitech-hidpp-monitor.service
	sed -i 's|ExecStart=.*|ExecStart=$(BINDIR)/logitech-headset-monitor|' $(SYSTEMD_DIR)/logitech-headset-monitor.service
	systemctl --user daemon-reload
	systemctl --user enable logitech-hidpp-monitor.service logitech-headset-monitor.service

install-all: install install-tools install-systemd

uninstall:
	$(foreach f,$(WIDGETS) $(DAEMONS),rm -f $(DESTDIR)$(BINDIR)/$(notdir $(f));)

uninstall-tools:
	$(foreach f,$(TOOLS),rm -f $(DESTDIR)$(BINDIR)/$(notdir $(f));)

uninstall-systemd:
	systemctl --user disable logitech-hidpp-monitor.service logitech-headset-monitor.service || true
	rm -f $(SYSTEMD_DIR)/logitech-hidpp-monitor.service $(SYSTEMD_DIR)/logitech-headset-monitor.service
	systemctl --user daemon-reload

uninstall-all: uninstall uninstall-tools uninstall-systemd

.PHONY: install install-tools install-systemd install-all uninstall uninstall-tools uninstall-systemd uninstall-all
