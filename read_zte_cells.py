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

def check_crc(data):
    if len(data) < 4:
        return False
    return data[-2:] == crc16(data[:-2])

port = "/dev/ttyUSB0"
ser = serial.Serial(port, baudrate=9600, parity=serial.PARITY_EVEN, bytesize=8, stopbits=1, timeout=0.6)

slaves = [2, 5]

for slave in slaves:
    pdu = struct.pack(">BBHH", slave, 4, 0x0080, 32)
    frame = pdu + crc16(pdu)
    ser.reset_input_buffer()
    ser.reset_output_buffer()
    time.sleep(0.08)
    ser.write(frame)
    expected_bytes = 1 + 1 + 1 + (32 * 2) + 2 # 69 bytes
    resp = ser.read(expected_bytes)
    if len(resp) >= expected_bytes and resp[0] == slave and resp[1] == 4 and check_crc(resp):
        raw = resp[3:67]
        regs = [struct.unpack(">H", raw[i:i+2])[0] for i in range(0, len(raw), 2)]
        pack_v = regs[0] * 0.01
        bus_v = regs[1] * 0.01
        curr = (regs[2] - 65536) if regs[2] > 32767 else regs[2]
        curr_a = curr * 0.01
        cells = [r * 0.001 for r in regs[4:19] if 2000 <= r <= 4300]
        max_v = max(cells) if cells else 0
        min_v = min(cells) if cells else 0
        delta = (max_v - min_v) * 1000 if cells else 0
        soc = regs[25]
        cycles = regs[26]
        temps = [regs[20]*0.1, regs[21]*0.1, regs[22]*0.1]
        
        print(f"\n==========================================")
        print(f"🔋 BATERAI ZTE (Slave ID {slave}):")
        print(f"==========================================")
        print(f"  Tegangan Pack   : {pack_v:.2f} V")
        print(f"  Tegangan Busbar : {bus_v:.2f} V")
        print(f"  Arus Baterai    : {curr_a:.2f} A ({pack_v * curr_a:.1f} W)")
        print(f"  SOC Baterai     : {soc} %")
        print(f"  Siklus (Cycles) : {cycles} kali")
        print(f"  Suhu Sel        : {temps[0]:.1f}°C, {temps[1]:.1f}°C, {temps[2]:.1f}°C")
        print(f"  Jumlah Sel      : {len(cells)} Sel LiFePO4")
        print(f"  Delta Antar Sel : {delta:.1f} mV (Min: {min_v:.3f}V, Max: {max_v:.3f}V)")
        print(f"  Tegangan Sel    :")
        for idx, c in enumerate(cells, 1):
            print(f"    Sel {idx:02d}: {c:.3f} V")
    else:
        print(f"Slave {slave} tidak merespon valid: len={len(resp)}")
    time.sleep(0.1)

ser.close()
