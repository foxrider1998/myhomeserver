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

def send_cmd(cmd):
    ser.reset_input_buffer()
    ser.write(build_command(cmd))
    time.sleep(0.2)
    resp = ser.read_until(b"\r")
    is_ack = b"ACK" in resp
    print(f"[{'OK' if is_ack else 'FAIL'}] {cmd:15} -> {resp}")
    return is_ack

print("=== APPLYING OPTIMIZED SETTINGS FOR 2x 550Wp LONGI + 2x 15S USED LIFEPO4 ===")
# 1. Bulk Voltage 53.8V
send_cmd("PCVV53.8")

# 2. Float Voltage 52.5V
send_cmd("PBFT52.5")

# 3. Cut-off Voltage 44.0V (Protects used cells from over-discharge)
send_cmd("PSDV44.0")

# 4. Back to Grid 47.0V (Hands over to PLN safely at ~25% SOC)
send_cmd("PBCV47.0")

# 5. Back to Battery 52.0V
send_cmd("PBDV52.0")

# 6. Max Total Charge Current 40A (Gentle for used 200Ah bank)
send_cmd("MNCHGC040")

# 7. Max Utility/PLN Charge Current 10A (Save PLN electricity bill)
send_cmd("MUCHGC010")

# 8. Charger Priority: Solar First (PCP01)
send_cmd("PCP01")

# 9. Output Source Priority: SBU (POP02) or SUB (POP01). Let's set SBU (Solar-Battery-Utility)
send_cmd("POP02")

print("\n--- Verifying QPIRI ---")
ser.reset_input_buffer()
ser.write(build_command("QPIRI"))
time.sleep(0.2)
q = ser.read_until(b"\r")
print("New QPIRI:", q)

ser.close()
