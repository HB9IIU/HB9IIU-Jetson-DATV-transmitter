# Empty on purpose - its only job is to mark this directory as pytest's
# rootdir, so pytest prepends it to sys.path and tests/ can import this
# repo's flat top-level modules (pluto_signal_stability, pa_relay,
# pa_relay_gpio, ...) without needing them installed as a package.
