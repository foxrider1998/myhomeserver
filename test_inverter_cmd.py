import serial
import time

def voltronic_crc(cmd: str) -> bytes:
    crc = 0
    for c in cmd:
        byte = ord(c)
        for _ in range(8):
            bit = (byte >> 7) & 1
            crc_bit = (crc >> 15) & 1
            crc = (crc << 1) & 0xFFFF
            if crc_bit ^ bit:
                crc ^= 0x1021
            byte = (byte << 1) & 0xFF
    crc_high = (crc >> 8) & 0xFF
    crc_low = crc & 0xFF
    if crc_high in (0x0A, 0x0D, 0x28):
        crc_high += 1
    if crc_low in (0x0A, 0x0D, 0x28):
        crc_low += 1
    return bytes([crc_high, crc_low])

def build_command(cmd_str: str) -> bytes:
    return cmd_str.encode("ascii") + voltronic_crc(cmd_str) + b"\r"

ser = serial.Serial("/dev/ttyS0", baudrate=2400, timeout=1.5)

def test_cmd(cmd):
    ser.reset_input_buffer()
    ser.write(build_command(cmd))
    time.sleep(0.2)
    resp = ser.read_until(b"\r")
    print(f"Cmd: {cmd:15} -> {resp}")
    return resp

print("--- Testing Voltronic Voltage Commands ---")
# PCVV: Constant Voltage / Bulk charge voltage
test_cmd("PCVV54.0")
test_cmd("PCVV53.8")

# PBFT: Floating charge voltage
test_cmd("PBFT52.7")
test_cmd("PBFT52.5")

# PSDV: Shut down / Cut-off voltage
test_cmd("PSDV40.0")
test_cmd("PSDV44.0")

# PBCV: Battery re-charge voltage (Back to Grid)
test_cmd("PBCV46.0")
test_cmd("PBCV47.0")

# PBDV: Battery re-discharge voltage (Back to Battery)
test_cmd("PBDV52.0")

ser.close()
