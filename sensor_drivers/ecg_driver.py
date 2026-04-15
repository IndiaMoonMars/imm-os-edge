#!/usr/bin/env python3
"""
IMM-OS ECG Driver (AD8232 via ADS1115 ADC)
Samples ECG at 250Hz.
Outputs JSON to stdout.
"""

import time
import json
import sys
import board
import busio
import adafruit_ads1x15.ads1115 as ADS
from adafruit_ads1x15.analog_in import AnalogIn

def main():
    try:
        # Create the I2C bus
        i2c = busio.I2C(board.SCL, board.SDA)

        # Create the ADC object using the I2C bus
        # AD8232 OUT pin connected to ADS1115 channel 0
        ads = ADS.ADS1115(i2c)
        
        # Configure data rate to max 860 SPS to achieve our 250Hz loops without blocking
        ads.data_rate = 860
        
        chan = AnalogIn(ads, ADS.P0)

    except Exception as e:
        print(json.dumps({"error": f"Failed to initialize ADC for ECG: {e}"}), file=sys.stderr)
        sys.exit(1)

    while True:
        try:
            # At 250Hz, we need very tight loops. Standard print() is slow, 
            # but we're piping this to another python process. In production, 
            # ECG should ideally be batched. We'll output individual samples per the requirement.
            
            start_time = time.time()
            
            # Read voltage from ADC
            voltage = chan.voltage
            
            payload = {
                "sensor": "ecg_ad8232",
                "voltage": round(voltage, 4),
                "timestamp": time.time(), # microsecond resolution
            }
            
            print(json.dumps(payload), flush=True)

            # Sleep precisely enough to maintain 250Hz (0.004 seconds per loop)
            elapsed = time.time() - start_time
            sleep_time = max(0, 0.004 - elapsed)
            time.sleep(sleep_time)

        except Exception as e:
            print(json.dumps({"error": f"ECG read error: {str(e)}"}), file=sys.stderr)
            time.sleep(1)

if __name__ == '__main__':
    main()
