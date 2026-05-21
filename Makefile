PREFIX ?= /usr/local
BINDIR ?= $(PREFIX)/bin
SYSTEMD_DIR ?= $(HOME)/.config/systemd/user

WIDGETS = logibar-keyboard logibar-mouse logibar-headset
DAEMONS = logibar-hidpp-monitor logibar-headset-monitor
TOOLS = tools/logibar-hidpp-battery tools/logibar-hidpp-debug tools/logibar-headset-probe
SERVICES = systemd/logibar-hidpp-monitor.service systemd/logibar-headset-monitor.service
UDEV_RULE = udev/70-logitech-hidraw.rules
UDEV_DIR ?= /etc/udev/rules.d

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

.PHONY: install install-tools install-systemd install-udev install-all uninstall uninstall-tools uninstall-systemd uninstall-udev uninstall-all
