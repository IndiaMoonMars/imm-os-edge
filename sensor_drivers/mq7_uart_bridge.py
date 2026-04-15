#!/usr/bin/env python3
"""
IMM-OS MQ-7 UART Bridge (CO Sensor)
Reads UART stream from STM32 which processes the MQ-7 analog signal.
Outputs JSON to stdout.
"""

import serial
import json
import time
import sys

# Default serial port for RPi GPIO UART
SERIAL_PORT = "/dev/serial0"
BAUD_RATE = 115200

def main():
    try:
        ser = serial.Serial(SERIAL_PORT, BAUD_RATE, timeout=2.0)
        ser.flushInput()
    except Exception as e:
        print(json.dumps({"error": f"Failed to open UART {SERIAL_PORT}: {e}"}), file=sys.stderr)
        sys.exit(1)

    while True:
        try:
            if ser.in_waiting > 0:
                line = ser.readline().decode('utf-8').strip()
                
                # Assume STM32 sends comma-separated values or simple float: "CO_PPM: 3.5" or just "3.5"
                # We'll try to parse it as float for this phase
                if line:
                    co_ppm = float(line.split(':')[-1].strip())
                    
                    payload = {
                        "sensor": "mq7",
                        "co_ppm": round(co_ppm, 2),
                        "timestamp": int(time.time()),
                    }
                    print(json.dumps(payload), flush=True)
                    
        except ValueError:
            print(json.dumps({"error": f"Invalid data from STM32: {line}"}), file=sys.stderr)
        except Exception as e:
            print(json.dumps({"error": f"UART read error: {str(e)}"}), file=sys.stderr)

        time.sleep(1.0) # Rate limit UART reading to 1Hz based on requirements

if __name__ == '__main__':
    main()
