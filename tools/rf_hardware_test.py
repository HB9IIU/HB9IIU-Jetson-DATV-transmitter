"""Raw hardware RF test - bypasses PlutoDVB2/DVB-S2/GStreamer entirely.

Powers up the Pluto's TX local oscillator and steps it through a few
frequencies directly via the AD9361's own IIO attributes (the same
"cf-ad9361-lpc"/"ad9361-phy" hardware interface libiio uses, independent of
any DATV software). No modulation is set up - the AD9361 always leaks a
small amount of its LO frequency straight to the output (a normal
direct-conversion impairment), which is enough to show up as a visible peak
on a spectrum analyzer at the exact tuned frequency. This confirmed the RF
hardware chain is alive earlier, so this script turns that one-off manual
test into something repeatable: watch the peak visibly hop between
frequencies, confirming the whole TX chain (LO, mixer, amplifier, antenna
path) responds correctly to tuning.

Default frequencies step across a 5 MHz span centered on 2.405 GHz, to
match a spectrum analyzer set to center=2.405 GHz, span=5 MHz.

Ctrl+C to stop - always leaves the Pluto muted (TX LO powered down) on exit.
"""

import time

import paramiko

PLUTO_IP = "192.168.0.50"
SSH_USERNAME = "root"
SSH_PASSWORD = "analog"

TX_LO_POWERDOWN_PATH = "/sys/bus/iio/devices/iio:device0/out_altvoltage1_TX_LO_powerdown"
HARDWARE_GAIN_DB = -15
STEP_SECONDS = 4.0

# Hz - a few points across a 5 MHz span centered on 2.405 GHz.
TEST_FREQUENCIES_HZ = [
    2_403_000_000,
    2_404_000_000,
    2_405_000_000,
    2_406_000_000,
    2_407_000_000,
]


def run(ssh, command):
    _stdin, stdout, stderr = ssh.exec_command(command)
    exit_status = stdout.channel.recv_exit_status()
    if exit_status != 0:
        print("  WARNING: command failed ({}): {}".format(exit_status, command))
        print("  stderr: {}".format(stderr.read().decode(errors="replace").strip()))


def set_frequency(ssh, freq_hz):
    run(ssh, "iio_attr -u local: -o -c ad9361-phy altvoltage1 frequency {}".format(freq_hz))


def main():
    print("Connecting to Pluto at {}...".format(PLUTO_IP))
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    ssh.connect(PLUTO_IP, username=SSH_USERNAME, password=SSH_PASSWORD, timeout=8)

    try:
        print("Powering up TX LO and setting gain to {} dB...".format(HARDWARE_GAIN_DB))
        run(ssh, "echo 0 > {}".format(TX_LO_POWERDOWN_PATH))
        run(ssh, "iio_attr -u local: -o -c ad9361-phy voltage0 hardwaregain {}".format(
            HARDWARE_GAIN_DB))

        print("\nStepping through {} frequencies, {}s each. "
              "Watch your spectrum analyzer. Press Ctrl+C to stop.\n".format(
                  len(TEST_FREQUENCIES_HZ), STEP_SECONDS))
        while True:
            for freq_hz in TEST_FREQUENCIES_HZ:
                print("TX LO -> {:.3f} MHz".format(freq_hz / 1e6))
                set_frequency(ssh, freq_hz)
                time.sleep(STEP_SECONDS)
    except KeyboardInterrupt:
        pass
    finally:
        print("\nStopping - powering down TX LO...")
        run(ssh, "echo 1 > {}".format(TX_LO_POWERDOWN_PATH))
        ssh.close()
        print("Stopped. TX LO powered down.")


if __name__ == "__main__":
    main()
