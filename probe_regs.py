import serial
import time
import struct

def crc16(data):
    crc = 0xFFFF
    for pos in data:
        crc ^= pos
        for _ in range(8):
            if (crc & 1) != 0:
                crc = (crc >> 1) ^ 0xA001
            else:
                crc >>= 1
    return struct.pack("<H", crc)

port = "/dev/ttyUSB0"
ser = serial.Serial(port, baudrate=9600, parity=serial.PARITY_EVEN, bytesize=8, stopbits=1, timeout=0.25)

print("=== DEEP REGISTER PROBE (Baud 9600, Parity EVEN) ===")

test_slaves = [0, 1, 2, 3, 16, 255]
# Common starting registers across ZTE / Shoto / Narada / Pace BMS models:
test_addresses = [
    0x0000, 0x0001, 0x0002, 0x0010, 0x0020, 0x0022, 0x0080, 0x0081,
    0x0100, 0x0200, 0x0300, 0x0400, 0x0500, 0x1000, 0x2000, 0x3000, 0x0064
]

successes = []

for slave in test_slaves:
    print(f"\nChecking Slave ID: {slave}...")
    for reg in test_addresses:
        for fc in [3, 4]:
            pdu = struct.pack(">BBHH", slave, fc, reg, 1)
            frame = pdu + crc16(pdu)
            ser.reset_input_buffer()
            ser.write(frame)
            time.sleep(0.06)
            resp = ser.read(64)
            if resp:
                # If error response (fc + 0x80)
                if len(resp) >= 3 and resp[1] == (fc + 0x80):
                    err_code = resp[2]
                    err_name = {1: "Illegal Function", 2: "Illegal Address", 3: "Illegal Value", 4: "Device Failure"}.get(err_code, str(err_code))
                    # print(f"  Slave {slave} FC {fc} Reg 0x{reg:04X} -> Exception: {err_name}")
                elif len(resp) >= 5 and resp[1] == fc:
                    byte_cnt = resp[2]
                    val = struct.unpack(">H", resp[3:5])[0] if byte_cnt >= 2 else 0
                    print(f"  🎯 SUCCESS! Slave {slave} FC {fc} Reg 0x{reg:04X} -> Value: {val} (raw: {resp.hex(' ')})")
                    successes.append((slave, fc, reg, val))

ser.close()

if successes:
    print(f"\nTotal valid registers found: {len(successes)}")
else:
    print("\nMencoba blok pembacaan (count 4, 8, 16)...")
    ser = serial.Serial(port, baudrate=9600, parity=serial.PARITY_EVEN, bytesize=8, stopbits=1, timeout=0.3)
    block_starts = [0x0000, 0x0001, 0x0010, 0x0020, 0x0022, 0x0080, 0x0100, 0x0200, 0x1000]
    for slave in [0, 1, 2]:
        for reg in block_starts:
            for cnt in [4, 8, 16]:
                for fc in [3, 4]:
                    pdu = struct.pack(">BBHH", slave, fc, reg, cnt)
                    frame = pdu + crc16(pdu)
                    ser.reset_input_buffer()
                    ser.write(frame)
                    time.sleep(0.08)
                    resp = ser.read(128)
                    if resp and len(resp) >= 5 and resp[1] == fc:
                        print(f"  🎯 BLOCK SUCCESS! Slave {slave} FC {fc} Reg 0x{reg:04X} Count {cnt} -> Raw: {resp.hex(' ')}")
    ser.close()
