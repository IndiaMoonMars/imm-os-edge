# hwsim — fake hardware for running the real drivers without sensors

Stand-ins for the hardware libraries the drivers import (RPi.bme280, smbus2, the
Adafruit SCD4x / TSL2561 / TCA9548A / ADS1x15 / extended-bus modules, pi-ina219, pyserial).
Put this directory first on `PYTHONPATH` and the **unmodified** drivers run on a laptop
and publish plausible readings through the real publisher, TLS, broker and pipeline:

```bash
PYTHONPATH=tools/hwsim IMM_NODE_ID=node-rpi-01 IMM_ZONE=zone_a O2_CAL_MV=42.5 \
    python sensor_drivers/bme280_driver.py --mode stdout
```

This is different from `simulator/` (which fakes whole nodes on the MCC): hwsim exercises
the edge driver code itself. Used by the end-to-end test and for demos. Never install it on
a node: on real hardware the real libraries must be imported. `tools/bringup.py` prints a
FAKE HARDWARE warning when hwsim is on the path.

`HWSIM_MQ7_PERIOD` (default 10 s) shortens the MQ-7's 150 s cycle for demos.
