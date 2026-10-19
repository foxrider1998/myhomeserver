import serial
import struct
import time

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

ser = serial.Serial("/dev/ttyUSB0", baudrate=9600, parity=serial.PARITY_EVEN, timeout=0.15)

print("=== SCANNING SLAVE ADDRESSES 1 to 16 ===")
found_slaves = []

for slave in range(1, 17):
    # Try FC 04 Read Input Reg 0x0080 count 4
    pdu = struct.pack(">BBHH", slave, 4, 0x0080, 4)
    frame = pdu + crc16(pdu)
    ser.reset_input_buffer()
    ser.write(frame)
    time.sleep(0.06)
    resp = ser.read(32)
    if resp and len(resp) >= 5 and resp[1] == 4:
        v = struct.unpack(">H", resp[3:5])[0] * 0.01
        print(f"🔥 BATTERY FOUND at Slave ID {slave}: Voltage = {v:.2f} V (Raw: {resp.hex(' ')})")
        found_slaves.append(slave)
    elif resp:
        print(f"  Got raw from Slave {slave}: {resp.hex(' ')}")

ser.close()

print(f"\nTotal Baterai Terdeteksi di Bus RS485: {len(found_slaves)} (Slaves: {found_slaves})")
