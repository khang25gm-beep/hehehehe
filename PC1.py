# -*- coding: utf-8 -*-
"""PC1 - Gateway RS485 + API HTTP cho Cloudflare + HMI tai cho.
Chi can PC1.py, PC2.py, va cloudflared.exe co san (dat canh PC1.py).
Chay: py -3.14 PC1.py | Cai neu HARDWARE: py -3.14 -m pip install pyserial
"""
import csv
import io
import json
import math
import os
import queue
import re
import secrets
import struct
import subprocess
import threading
import time
import tkinter as tk
from collections import deque
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

# ======================= CAU HINH PC1 =======================
MODE = "SIMULATION"             # Doi thanh "HARDWARE" sau khi da test
COM_PORT = "COM6"               # USB-SERIAL CH340 theo Device Manager
RELAY_ID = 255
RELAY_BAUD = 9600
MOTOR_COIL = 0x0000              # Xac minh dung dia chi cua relay
SERVO_COIL = 0x0001
SENSOR_ID = 0x50                 # ID 80
SENSOR_BAUD = 115200            # Kiem tra thuc te cua WT53R
SENSOR_REG = 0x0034
STATUS_REG = 0x0035
SERIAL_TIMEOUT = 0.4
SENSOR_INTERVAL = 0.24
HTTP_PORT = 8765
HTTP_BIND = '0.0.0.0'         # Cho phep ket noi LAN va Cloudflare (token bat buoc)
REMOTE_WATCHDOG = 7.0          # Mat heartbeat PC2 -> yeu cau ngat tai
ALLOW_REAL_MOTOR = False        # Chi doi True sau khi test khong tai
ALLOW_REAL_SERVO = False
# ==========================================================

BG, PANEL, FG, MUTED = "#091322", "#152539", "#E7F1FA", "#A0B8CD"
GREEN, RED, CYAN = "#37D7A6", "#F07883", "#5BBFFA"


def crc16(buf):
    crc = 0xFFFF
    for b in buf:
        crc ^= b
        for _ in range(8):
            crc = (crc >> 1) ^ (0xA001 if crc & 1 else 0)
    return struct.pack('<H', crc)


class ModbusBus:
    def __init__(self):
        try:
            import serial
        except ImportError as e:
            raise RuntimeError('Chua cai pyserial: py -3.14 -m pip install pyserial') from e
        self.ser = serial.Serial(COM_PORT, RELAY_BAUD, timeout=SERIAL_TIMEOUT, write_timeout=0.5)

    def read_n(self, n):
        out = bytearray()
        deadline = time.monotonic() + SERIAL_TIMEOUT
        while len(out) < n and time.monotonic() < deadline:
            out.extend(self.ser.read(n - len(out)))
        if len(out) != n:
            raise TimeoutError('Modbus khong phan hoi: kiem tra baudrate, ID, A/B, nguon')
        return bytes(out)

    def exchange(self, slave, fc, register, value, baud):
        if self.ser.baudrate != baud:
            self.ser.baudrate = baud
        time.sleep(max(0.004, 4 * 11 / baud))
        self.ser.reset_input_buffer()
        body = struct.pack('>BBHH', slave, fc, register, value)
        packet = body + crc16(body)
        self.ser.write(packet)
        self.ser.flush()
        head = self.read_n(3)
        if head[1] & 0x80:
            received = head + self.read_n(2)
        elif fc == 3:
            if head[2] != value * 2 or head[2] > 32:
                raise ValueError('So byte thanh ghi sai')
            received = head + self.read_n(head[2] + 2)
        elif fc == 5:
            received = head + self.read_n(5)
        else:
            raise ValueError('Unsupported function code')
        if crc16(received[:-2]) != received[-2:]:
            raise ValueError('CRC sai')
        if received[0] != slave:
            raise ValueError('Sai Slave ID')
        if received[1] & 0x80:
            raise RuntimeError(f'Modbus exception {received[2]}')
        if received[1] != fc:
            raise ValueError('Sai function code')
        if fc == 5 and received != packet:
            raise ValueError('Relay khong xac nhan lenh ghi')
        return received

    def read_sensor(self):
        r1 = self.exchange(SENSOR_ID, 3, SENSOR_REG, 1, SENSOR_BAUD)
        r2 = self.exchange(SENSOR_ID, 3, STATUS_REG, 1, SENSOR_BAUD)
        return struct.unpack('>H', r1[3:5])[0] * 1.0, struct.unpack('>H', r2[3:5])[0]

    def relay(self, coil, on):
        self.exchange(RELAY_ID, 5, coil, 0xFF00 if on else 0, RELAY_BAUD)

    def close(self):
        self.ser.close()


class SimulatedBus:
    def __init__(self):
        self.motor_on = False
        self.servo_on = False
        self.start_time = time.monotonic()

    def relay(self, coil, on):
        if coil == MOTOR_COIL:
            if on and not self.motor_on:
                self.start_time = time.monotonic()
            self.motor_on = on
        elif coil == SERVO_COIL:
            self.servo_on = on
        else:
            raise ValueError('Coil khong ton tai')

    def read_sensor(self):
        if not self.motor_on:
            return 200.0, 0
        t = time.monotonic() - self.start_time
        phase = t % 3.2
        high = int(t // 3.2) % 2 == 0
        if 0.6 <= phase <= 1.5:
            return 200.0 - (36 if high else 12), 0
        return 200.0, 0

    def close(self):
        pass


def default_config():
    return dict(mode='Thủ công', main_group='Cao', d0=200.0,
                threshold=20.0, presence=5.0, delay=0.8,
                pulse=0.45, target=0)


class Controller:
    """Duy nhat mot worker so huu COM; tat ca lenh di qua queue."""
    def __init__(self):
        self.lock = threading.RLock()
        self.tasks = queue.Queue(maxsize=80)
        self.ended = threading.Event()
        self.settings = default_config()
        self.state = dict(mode=MODE, com_port=COM_PORT, serial_open=False,
                          sensor_enabled=True, distance_mm=None, sensor_status=None,
                          sensor_error='Chua doc', timestamp=None, sample_id=0,
                          motor_on=None, servo_on=None, relay_ready=False,
                          output_error='', owner='PC1', fault='',
                          status_text='Dang khoi tao')
        self.counts = dict(total=0, high=0, low=0, main=0)
        self.history = []
        self.logs = deque(maxlen=150)
        self.pending = deque()
        self.last_remote = None
        self.servo_started = None
        self.reset_detection()
        self.last_sensor_seq = -1
        self.bus = None
        self.log('Khoi dong PC1 ' + MODE)

    def log(self, msg):
        with self.lock:
            self.logs.append(dict(time=datetime.now().strftime('%H:%M:%S'), text=str(msg)))

    def reset_detection(self):
        self.present = False
        self.hits = 0
        self.misses = 0
        self.peak = 0.0

    def snapshot(self, concise=True):
        with self.lock:
            s = dict(self.state)
            s['settings'] = dict(self.settings)
            s['counts'] = dict(self.counts)
            s['history'] = [dict(r) for r in (self.history[-80:] if concise else self.history)]
            s['logs'] = list(self.logs)[-60:]
            s['pending_count'] = len(self.pending)
            return s

    def request(self, source, action, data=None, timeout=4.0):
        completed = threading.Event()
        result = {}
        deadline = time.monotonic() + timeout
        try:
            self.tasks.put_nowait((source, action, data or {}, completed, result, deadline))
        except queue.Full:
            return dict(ok=False, error='Hang doi lenh day', state=self.snapshot())
        if not completed.wait(timeout + 0.3):
            return dict(ok=False, error='Timeout: KHONG biet lenh da thuc hien hay chua', state=self.snapshot())
        return dict(ok=result.get('ok', False), error=result.get('error', ''), state=self.snapshot())

    def outputs_off(self):
        errors = []
        for coil, key in ((MOTOR_COIL, 'motor_on'), (SERVO_COIL, 'servo_on')):
            try:
                self.bus.relay(coil, False)
                self.state[key] = False
            except Exception as e:
                self.state[key] = None
                errors.append(f'{key}: {e}')
        self.servo_started = None
        for _, row in self.pending:
            row['route'] = 'Huy gat do dung'
        self.pending.clear()
        self.reset_detection()
        self.state['relay_ready'] = not errors
        self.state['output_error'] = '; '.join(errors)
        if errors:
            self.state['fault'] = 'Khong xac nhan duoc relay OFF'
            self.log('NGUY HIEM: chua xac nhan OFF ' + '; '.join(errors))
        return not errors

    def set_relay(self, coil, on, key):
        if on:
            if self.state['fault'] or not self.state['relay_ready']:
                raise RuntimeError('Relay chua an toan: ' + (self.state['fault'] or 'Chua xac nhan OFF'))
            if MODE == 'HARDWARE' and coil == MOTOR_COIL and not ALLOW_REAL_MOTOR:
                raise RuntimeError('ALLOW_REAL_MOTOR dang False')
            if MODE == 'HARDWARE' and coil == SERVO_COIL and not ALLOW_REAL_SERVO:
                raise RuntimeError('ALLOW_REAL_SERVO dang False')
        try:
            self.bus.relay(coil, on)
        except Exception as e:
            self.state[key] = None
            self.state['relay_ready'] = False
            self.state['fault'] = str(e)
            raise
        self.state[key] = on
        self.log(f'{key} -> {"ON" if on else "OFF"} (da phan hoi Modbus)')

    @staticmethod
    def validate_settings(data):
        if not isinstance(data, dict):
            raise ValueError('Cau hinh phai la object')
        out = {}
        if data.get('mode') not in ('Tự động', 'Thủ công'):
            raise ValueError('Che do khong hop le')
        if data.get('main_group') not in ('Cao', 'Thấp'):
            raise ValueError('Nhom chinh khong hop le')
        out['mode'], out['main_group'] = data['mode'], data['main_group']
        for key in ('d0', 'threshold', 'presence', 'delay', 'pulse'):
            v = data.get(key)
            if isinstance(v, bool) or not isinstance(v, (float, int)) or not math.isfinite(v):
                raise ValueError(key + ': nhap so huu han')
            out[key] = float(v)
        target = data.get('target')
        if isinstance(target, bool) or not isinstance(target, int) or target < 0:
            raise ValueError('target phai la so nguyen >= 0')
        out['target'] = target
        if not (40 <= out['d0'] <= 4000 and 0 < out['presence'] <= out['threshold'] < out['d0']):
            raise ValueError('Can 40<=D0<=4000 va 0<presence<=threshold<D0')
        if out['delay'] < 0:
            raise ValueError('delay khong duoc am (KHONG gioi han tren)')
        if not (0.1 <= out['pulse'] <= 1.5):
            raise ValueError('Thoi gian kich relay 2 trong 0.1–1.5s; day KHONG phai delay')
        return out

    def execute(self, source, action, data):
        if source == 'PC2':
            self.last_remote = time.monotonic()
        if action == 'status':
            return
        if action == 'stop':
            safe = self.outputs_off()
            self.log(source + ' yeu cau DUNG KHAN QUA PHAN MEM')
            if not safe:
                raise RuntimeError('CHUA XAC NHAN DUOC RELAY OFF. Phai dung vat ly va kiem tra tai mo hinh!')
            return
        if action == 'claim':
            if self.state['motor_on'] is not False or self.state['servo_on'] is not False:
                raise RuntimeError('Phai dung motor va servo truoc khi chuyen quyen')
            if self.state['fault']:
                raise RuntimeError('Phai xu ly loi phan cung truoc khi nhan quyen')
            self.state['owner'] = source
            self.log(source + ' da nhan quyen dieu khien')
            return
        if action == 'release':
            if self.state['owner'] != source:
                raise RuntimeError('Ban khong giu quyen')
            if not self.outputs_off():
                raise RuntimeError('Khong the xac nhan relay OFF')
            self.state['owner'] = 'PC1'
            return
        if self.state['owner'] != source:
            raise PermissionError('Quyen dang thuoc ' + self.state['owner'] + '; hay NHAN QUYEN khi cac relay OFF')
        if action == 'ack_fault':
            if not self.outputs_off():
                raise RuntimeError('Relay chua xac nhan OFF, khong the xoa loi')
            self.state['fault'] = ''
            self.log('Da xac nhan relay OFF va xoa khoa loi (khong phai kiem tra vat ly)')
        elif action == 'configure':
            if self.state['motor_on'] is not False or self.state['servo_on'] is not False:
                raise RuntimeError('Dung bang tai va servo truoc khi doi thong so')
            new = self.validate_settings(data)
            if new['main_group'] != self.settings['main_group']:
                self.counts = dict(total=0, high=0, low=0, main=0)
                self.history.clear()
            self.settings = new
            self.pending.clear()
            self.reset_detection()
            self.log(source + ' cap nhat thong so. delay=' + str(new['delay']))
        elif action == 'sensor':
            val = data.get('value')
            if type(val) is not bool:
                raise ValueError('value must be boolean')
            if not val and self.state['motor_on'] and self.settings['mode'] == 'Tự động':
                self.outputs_off()
                raise RuntimeError('Da dung motor: khong the tat cam bien trong Auto')
            self.state['sensor_enabled'] = val
            if not val:
                self.state['distance_mm'] = None
                self.reset_detection()
        elif action == 'motor':
            val = data.get('value')
            if type(val) is not bool:
                raise ValueError('value must be boolean')
            if val:
                if self.settings['mode'] == 'Tự động' and not self.sensor_good():
                    raise RuntimeError('Chua co so do WT53R hop le, moi')
                if self.settings['target'] and self.counts['main'] >= self.settings['target']:
                    raise RuntimeError('Da du muc tieu; can reset hoac tang muc tieu')
                self.set_relay(MOTOR_COIL, True, 'motor_on')
            else:
                if not self.outputs_off():
                    raise RuntimeError('Chua xac nhan motor OFF')
        elif action == 'servo_pulse':
            if self.settings['mode'] != 'Thủ công':
                raise RuntimeError('Gat thu chi o che do Thu cong')
            if self.state['servo_on'] is True:
                raise RuntimeError('Servo dang kich')
            self.set_relay(SERVO_COIL, True, 'servo_on')
            self.servo_started = time.monotonic()
        elif action == 'calibrate':
            if self.state['motor_on'] is not False or not self.sensor_good():
                raise RuntimeError('Dung bang tai va do duoc D0 khi khong co vat')
            new = dict(self.settings)
            new['d0'] = round(self.state['distance_mm'], 1)
            if not (new['threshold'] < new['d0']):
                raise RuntimeError('D0 can lon hon nguong cao')
            self.settings = new
            self.log('Da lay D0 = ' + str(new['d0']))
        elif action == 'reset':
            if self.state['motor_on'] is not False:
                raise RuntimeError('Can dung motor truoc khi reset')
            self.counts = dict(total=0, high=0, low=0, main=0)
            self.history.clear()
            self.pending.clear()
            self.reset_detection()
        else:
            raise ValueError('Lenh khong duoc ho tro')

    def sensor_good(self):
        st = self.state
        return (st['sensor_enabled'] and st['sensor_error'] == '' and
                st['distance_mm'] is not None and st['timestamp'] is not None and
                time.time() - st['timestamp'] < 2)

    def auto_step(self, now):
        if self.state['motor_on'] is not True or self.settings['mode'] != 'Tự động':
            self.reset_detection()
            return
        if not self.sensor_good():
            self.state['fault'] = 'Cam bien loi trong Auto'
            self.log('Auto: cam bien mat du lieu -> dung bang tai')
            self.outputs_off()
            return
        seq = self.state['sample_id']
        if seq != self.last_sensor_seq:
            self.last_sensor_seq = seq
            h = max(0.0, self.settings['d0'] - self.state['distance_mm'])
            if h >= self.settings['presence']:
                self.hits += 1
                self.misses = 0
                self.peak = max(self.peak, h)
                if self.hits >= 2:
                    self.present = True
            elif h <= self.settings['presence'] * 0.45:
                self.misses += 1
                self.hits = 0
                if self.present and self.misses >= 2:
                    self.finish_product(now)
                    self.reset_detection()
                elif not self.present:
                    self.peak = 0
        if self.pending and self.state['motor_on']:
            started, row = self.pending[0]
            if now - started >= self.settings['delay']:
                if self.state['servo_on'] is True:
                    # cho servo hoan thanh, khong nhay qua lenh
                    return
                self.set_relay(SERVO_COIL, True, 'servo_on')
                self.servo_started = time.monotonic()
                row['route'] = 'Da gui lenh gat'
                self.pending.popleft()

    def finish_product(self, now):
        height = round(self.peak, 1)
        group = 'Cao' if height >= self.settings['threshold'] else 'Thấp'
        is_main = group == self.settings['main_group']
        self.counts['total'] += 1
        self.counts['high' if group == 'Cao' else 'low'] += 1
        self.counts['main'] += int(is_main)
        row = dict(id=self.counts['total'], time=datetime.now().isoformat(timespec='seconds'),
                   height=height, group=group, main_group=self.settings['main_group'],
                   route='Di thang' if is_main else 'Cho gat')
        self.history.append(row)
        if not is_main:
            self.pending.append((now, row))
        self.log(f'SP #{row["id"]}: {group} / {height:.1f}mm')
        target = self.settings['target']
        if target and self.counts['main'] >= target:
            self.log('Dat so luong muc tieu -> DUNG bang tai')
            self.outputs_off()

    def run(self):
        last_poll = 0.0
        last_safe_attempt = 0.0
        try:
            self.bus = SimulatedBus() if MODE == 'SIMULATION' else ModbusBus()
            self.state['serial_open'] = True
            self.state['status_text'] = 'Da khoi tao giao tiep'
            self.outputs_off()
            while not self.ended.is_set():
                now = time.monotonic()
                if self.state['owner'] == 'PC2' and (self.last_remote is None or now - self.last_remote > REMOTE_WATCHDOG):
                    self.log('Mat Cloudflare/PC2: STOP ca hai relay')
                    self.outputs_off()
                    self.state['owner'] = 'PC1'
                    self.last_remote = None
                if not self.state['relay_ready'] and now - last_safe_attempt > 2.0:
                    last_safe_attempt = now
                    self.outputs_off()
                if self.servo_started is not None and now - self.servo_started >= self.settings['pulse']:
                    try:
                        self.set_relay(SERVO_COIL, False, 'servo_on')
                        self.servo_started = None
                    except Exception as e:
                        self.log('Loi ngat servo: ' + str(e))
                        self.outputs_off()
                for _ in range(5):
                    try:
                        source, action, data, done, result, deadline = self.tasks.get_nowait()
                    except queue.Empty:
                        break
                    try:
                        if time.monotonic() > deadline:
                            raise TimeoutError('Lenh da het han; khong thuc thi')
                        self.execute(source, action, data)
                        result['ok'] = True
                    except Exception as e:
                        result.update(ok=False, error=str(e))
                        self.log(f'Loi [{source}/{action}]: {e}')
                    finally:
                        done.set()
                if self.state['sensor_enabled'] and time.monotonic() - last_poll >= SENSOR_INTERVAL:
                    last_poll = time.monotonic()
                    try:
                        distance, status = self.bus.read_sensor()
                        if status != 0 or not (40 <= distance <= 4000):
                            raise RuntimeError(f'Cam bien tra ve distance={distance}, status={status}')
                        self.state.update(distance_mm=distance, sensor_status=status, sensor_error='',
                                          timestamp=time.time(), sample_id=self.state['sample_id'] + 1)
                    except Exception as e:
                        self.state.update(distance_mm=None, sensor_status=None, sensor_error=str(e))
                try:
                    self.auto_step(time.monotonic())
                except Exception as e:
                    self.state['fault'] = 'Auto loi: ' + str(e)
                    self.log(self.state['fault'])
                    self.outputs_off()
                self.ended.wait(0.015)
        except Exception as e:
            self.state['serial_open'] = False
            self.state['fault'] = str(e)
            self.state['output_error'] = str(e)
            self.log('Khong mo duoc COM: ' + str(e))
        finally:
            if self.bus is not None:
                self.outputs_off()
                self.bus.close()
            self.state['serial_open'] = False


CONTROL = Controller()
TOKEN = secrets.token_urlsafe(32)  # Chi chia rieng voi nguoi dung PC2


class APIHandler(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def check_auth(self):
        key = self.headers.get('X-API-Token', '')
        return isinstance(key, str) and secrets.compare_digest(key, TOKEN)

    def respond(self, status, body, mime='application/json; charset=utf-8'):
        content = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', mime)
        self.send_header('Content-Length', str(len(content)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.end_headers()
        try:
            self.wfile.write(content)
        except (ConnectionError, BrokenPipeError):
            pass

    def do_GET(self):
        if not self.check_auth():
            self.respond(403, dict(ok=False, error='Token khong dung'))
            return
        if self.path == '/csv':
            snap = CONTROL.snapshot(concise=False)
            f = io.StringIO()
            wr = csv.writer(f)
            wr.writerow(['STT', 'Thời gian', 'Chiều cao (mm)', 'Nhóm', 'Nhóm chính', 'Phân luồng'])
            for r in snap['history']:
                wr.writerow([r['id'], r['time'], r['height'], r['group'], r['main_group'], r['route']])
            data = ('\ufeff' + f.getvalue()).encode('utf-8')
            self.respond(200, data, 'text/csv; charset=utf-8')
        else:
            self.respond(404, dict(ok=False, error='Khong ton tai'))

    def do_POST(self):
        if self.path != '/api':
            self.respond(404, dict(ok=False, error='Khong ton tai'))
            return
        if not self.check_auth():
            self.respond(403, dict(ok=False, error='Token khong dung'))
            return
        try:
            n = int(self.headers.get('Content-Length', '0'))
            if not 2 <= n <= 4096:
                raise ValueError('Yeu cau qua dai/khong hop le')
            data = json.loads(self.rfile.read(n).decode('utf-8'))
            if not isinstance(data, dict):
                raise ValueError('Can JSON object')
            action = data.pop('action', None)
            if action not in ('status', 'stop', 'claim', 'release', 'configure', 'sensor',
                              'motor', 'servo_pulse', 'calibrate', 'reset', 'ack_fault'):
                raise ValueError('Lenh khong hop le')
            answer = CONTROL.request('PC2', action, data)
            self.respond(200 if answer['ok'] else 409, answer)
        except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as e:
            self.respond(400, dict(ok=False, error=str(e)))


class HttpThread(threading.Thread):
    def __init__(self):
        super().__init__(daemon=True)
        self.server = ThreadingHTTPServer((HTTP_BIND, HTTP_PORT), APIHandler)
    def run(self):
        self.server.serve_forever(poll_interval=0.2)
    def close(self):
        self.server.shutdown()
        self.server.server_close()


class PC1Window:
    def __init__(self, root, server):
        self.root = root
        self.server = server
        self.cloud = None
        self.cloud_url = ''
        self.updates = queue.Queue()
        self.last_log = ''
        self.root.title('PC1 | MODBUS RS485 + CLOUDFLARE')
        self.root.geometry('1050x830')
        self.root.configure(bg=BG)
        self.root.protocol('WM_DELETE_WINDOW', self.close)
        self.make_widgets()
        self.root.after(160, self.refresh)

    def label(self, parent, content, size=10, color=FG, bold=False):
        return tk.Label(parent, text=content, font=('Segoe UI', size, 'bold' if bold else 'normal'),
                        fg=color, bg=parent.cget('bg'))

    def make_widgets(self):
        self.label(self.root, 'PC1 • GATEWAY & ĐIỀU KHIỂN TẠI CHỖ', 19, GREEN, True).pack(pady=18)
        self.info = tk.StringVar(value='Khởi động')
        tk.Label(self.root, textvariable=self.info, bg=PANEL, fg=FG, font=('Consolas', 11),
                 justify='left', anchor='w', padx=15, pady=14).pack(fill='x', padx=20)
        strip = tk.Frame(self.root, bg=BG)
        strip.pack(fill='x', padx=20, pady=12)
        self.button(strip, 'CHẠY MOTOR', lambda: self.call('motor', value=True), GREEN).pack(side='left', expand=True, fill='x', padx=3)
        self.button(strip, 'DỪNG MOTOR', lambda: self.call('stop'), RED).pack(side='left', expand=True, fill='x', padx=3)
        self.button(strip, 'GẠT SERVO', lambda: self.call('servo_pulse'), CYAN).pack(side='left', expand=True, fill='x', padx=3)
        strip2 = tk.Frame(self.root, bg=BG)
        strip2.pack(fill='x', padx=20)
        self.button(strip2, 'NHẬN QUYỀN PC1', lambda: self.call('claim')).pack(side='left', expand=True, fill='x', padx=3)
        self.button(strip2, 'BẬT ĐỌC CẢM BIẾN', lambda: self.call('sensor', value=True)).pack(side='left', expand=True, fill='x', padx=3)
        self.button(strip2, 'DỪNG ĐỌC CẢM BIẾN', lambda: self.call('sensor', value=False)).pack(side='left', expand=True, fill='x', padx=3)
        area = tk.LabelFrame(self.root, text='TÙY CHỈNH (Áp dụng khi motor OFF)', fg=CYAN, bg=PANEL, font=('Segoe UI',10,'bold'), padx=12, pady=9)
        area.pack(fill='x', padx=20, pady=12)
        self.vars = {}
        fields = [('mode','Chế độ'),('main_group','Nhóm chính'),('d0','D0 (mm)'),('threshold','Ngưỡng Cao (mm)'),
                  ('presence','Ngưỡng có vật (mm)'),('delay','Trễ gạt (s) KHÔNG GIỚI HẠN'),
                  ('pulse','Kích Relay 2 (s)'),('target','Mục tiêu SP chính')]
        for i,(key,text) in enumerate(fields):
            row, col = divmod(i,2)
            tk.Label(area, text=text, bg=PANEL, fg=MUTED, anchor='w').grid(row=row, column=2*col, sticky='w', padx=5, pady=6)
            var = tk.StringVar(value=str(CONTROL.settings[key]))
            self.vars[key] = var
            if key in ('mode','main_group'):
                options = ('Tự động','Thủ công') if key == 'mode' else ('Cao','Thấp')
                combo = ttk.Combobox(area, textvariable=var, values=options, width=18, state='readonly')
                combo.grid(row=row, column=2*col+1, padx=5, sticky='ew')
            else:
                tk.Entry(area, textvariable=var, width=18, bg='#24394F', fg=FG, insertbackground=FG).grid(row=row,column=2*col+1, padx=5, sticky='ew')
        area.grid_columnconfigure(1, weight=1)
        area.grid_columnconfigure(3, weight=1)
        buttons = tk.Frame(area,bg=PANEL)
        buttons.grid(row=5,column=0,columnspan=4,sticky='ew')
        self.button(buttons,'ÁP DỤNG',self.apply,GREEN).pack(side='left',fill='x',expand=True,padx=3)
        self.button(buttons,'LẤY D0',lambda:self.call('calibrate')).pack(side='left',fill='x',expand=True,padx=3)
        self.button(buttons,'RESET SỐ ĐẾM',lambda:self.call('reset')).pack(side='left',fill='x',expand=True,padx=3)
        self.button(buttons,'XUẤT CSV',self.export_csv).pack(side='left',fill='x',expand=True,padx=3)
        self.button(buttons,'XÁC NHẬN OFF / XÓA LỖI',lambda:self.call('ack_fault')).pack(side='left',fill='x',expand=True,padx=3)
        cloud = tk.LabelFrame(self.root,text='CLOUDFLARE TUNNEL',fg=CYAN,bg=PANEL,padx=12,pady=10)
        cloud.pack(fill='x', padx=20, pady=5)
        bar = tk.Frame(cloud,bg=PANEL)
        bar.pack(fill='x')
        self.button(bar,'MỞ CLOUDFLARE',self.start_cloud,GREEN).pack(side='left',padx=3)
        self.button(bar,'DỪNG TUNNEL',self.stop_cloud,RED).pack(side='left',padx=3)
        self.button(bar,'SAO CHÉP TOKEN',self.copy_token).pack(side='left',padx=3)
        self.button(bar,'SAO CHÉP LINK',self.copy_link).pack(side='left',padx=3)
        self.cloud_text = tk.StringVar(value='Link: CHƯA MỞ (hoặc chạy lệnh cloudflared ở Terminal)')
        tk.Label(cloud,textvariable=self.cloud_text,bg=PANEL,fg=GREEN,anchor='w',font=('Consolas',10)).pack(fill='x',pady=8)
        self.label(cloud,'Token ngẫu nhiên mỗi lần mở PC1. Chỉ chuyển riêng cho PC2; đừng công khai.',9,MUTED).pack(anchor='w')
        logs = tk.LabelFrame(self.root,text='NHẬT KÝ / CẢNH BÁO',fg=CYAN,bg=PANEL,padx=10,pady=7)
        logs.pack(fill='both',expand=True,padx=20,pady=(10,17))
        self.logbox=tk.Text(logs,bg='#0D1C2B',fg=FG,font=('Consolas',9),relief='flat',state='disabled',height=10)
        self.logbox.pack(fill='both',expand=True)

    def button(self,parent,title,fn,color=None):
        return tk.Button(parent,text=title,command=fn,bg=color or '#2A4860',fg='#00181A' if color in (GREEN,CYAN) else FG,
                         activebackground='#3C6680',bd=0,font=('Segoe UI',9,'bold'),padx=10,pady=10)

    def call(self,action,**data):
        def task():
            ans=CONTROL.request('PC1',action,data)
            self.updates.put(('response', ans))
        threading.Thread(target=task,daemon=True).start()

    def apply(self):
        try:
            data={k:(v.get() if k in ('mode','main_group') else (int(v.get()) if k=='target' else float(v.get().replace(',','.')))) for k,v in self.vars.items()}
        except ValueError:
            messagebox.showerror('Giá trị sai','Kiểm tra lại các ô số')
            return
        self.call('configure',**data)

    def export_csv(self):
        rows=CONTROL.snapshot(concise=False)['history']
        if not rows:
            messagebox.showinfo('CSV','Chưa có dữ liệu')
            return
        name=filedialog.asksaveasfilename(defaultextension='.csv',initialfile='phan_loai_PC1.csv')
        if not name:return
        with open(name,'w',encoding='utf-8-sig',newline='') as f:
            writer=csv.writer(f)
            writer.writerow(['STT','Thời gian','Chiều cao (mm)','Nhóm','Nhóm chính','Phân luồng'])
            for r in rows:writer.writerow([r['id'],r['time'],r['height'],r['group'],r['main_group'],r['route']])

    def start_cloud(self):
        if self.cloud and self.cloud.poll() is None:
            messagebox.showinfo('Cloudflare','Tunnel đang chạy')
            return
        exe=Path(__file__).resolve().parent/'cloudflared.exe'
        command=str(exe) if exe.is_file() else 'cloudflared'
        try:
            flags=getattr(subprocess,'CREATE_NO_WINDOW',0)
            self.cloud=subprocess.Popen([command,'tunnel','--url',f'http://127.0.0.1:{HTTP_PORT}'],
                                        stdout=subprocess.PIPE,stderr=subprocess.STDOUT,
                                        text=True,errors='replace',creationflags=flags)
        except OSError as e:
            messagebox.showerror('Cloudflare','Không chạy được cloudflared.exe. Đặt file cạnh PC1.py.\n'+str(e))
            return
        self.cloud_text.set('Đang tạo link Cloudflare...')
        threading.Thread(target=self.read_cloud,daemon=True).start()

    def read_cloud(self):
        try:
            for line in self.cloud.stdout:
                match=re.search(r'https://[a-z0-9-]+\.trycloudflare\.com\b',line)
                if match:
                    self.updates.put(('cloud',match.group(0)))
                if 'ERR' in line or 'error' in line.lower():
                    self.updates.put(('cloudlog',line.strip()[:170]))
        except Exception as e:
            self.updates.put(('cloudlog',str(e)))

    def stop_cloud(self):
        if self.cloud and self.cloud.poll() is None:
            self.cloud.terminate()
            self.cloud_url=''
            self.cloud_text.set('Link: ĐÃ DỪNG')

    def copy_token(self):
        self.root.clipboard_clear();self.root.clipboard_append(TOKEN)
        messagebox.showinfo('Token','Đã sao chép. Chỉ dán vào PC2, không gửi công khai.')

    def copy_link(self):
        if not self.cloud_url:
            messagebox.showinfo('Cloudflare','Chưa nhận link. Kiểm tra Terminal của cloudflared.')
            return
        self.root.clipboard_clear();self.root.clipboard_append(self.cloud_url)

    def refresh(self):
        s=CONTROL.snapshot()
        dist='--' if s['distance_mm'] is None else f"{s['distance_mm']:.1f}mm"
        self.info.set(f"Cổng COM: {COM_PORT} | {MODE} | {'ĐÃ MỞ' if s['serial_open'] else 'CHƯA MỞ'} | HTTP: {HTTP_PORT} (LAN + Cloudflare)\n"
                      f"Quyền điều khiển: {s['owner']} | WT53R: {dist} | Status: {s['sensor_status']} | SP: {s['counts']['total']}\n"
                      f"Relay 1 (motor): {s['motor_on']} | Relay 2 (servo): {s['servo_on']} | Đợi gạt: {s['pending_count']}\n"
                      f"Lỗi: {s['fault'] or s['output_error'] or s['sensor_error'] or 'Không'}")
        sig='\n'.join(f"[{e['time']}] {e['text']}" for e in s['logs'])
        if sig!=self.last_log:
            self.last_log=sig
            self.logbox.configure(state='normal');self.logbox.delete('1.0','end');self.logbox.insert('end',sig)
            self.logbox.see('end');self.logbox.configure(state='disabled')
        try:
            while True:
                event,data=self.updates.get_nowait()
                if event=='response':
                    if not data['ok']: messagebox.showwarning('PC1',data['error'])
                    else:
                        if data['state'] and data['state']['settings']:
                            self.vars['d0'].set(str(data['state']['settings']['d0']))
                elif event=='cloud':
                    self.cloud_url=data
                    self.cloud_text.set('Link: '+data)
                elif event=='cloudlog':
                    CONTROL.log('Cloudflare: '+data)
        except queue.Empty:
            pass
        self.root.after(220,self.refresh)

    def close(self):
        if not messagebox.askyesno('Đóng PC1','Đóng PC1 và yêu cầu tắt motor, servo? Cần công tắc dừng vật lý nếu lệnh thất bại.'):
            return
        CONTROL.request('PC1','stop',timeout=3)
        self.stop_cloud()
        self.server.close()
        CONTROL.ended.set()
        self.root.destroy()


def main():
    if MODE not in ('SIMULATION','HARDWARE'):
        raise SystemExit('MODE phai SIMULATION hoac HARDWARE')
    CONTROL.thread=threading.Thread(target=CONTROL.run,daemon=True)
    CONTROL.thread.start()
    server=HttpThread();server.start()
    root=tk.Tk()
    PC1Window(root,server)
    root.mainloop()


if __name__=='__main__':
    main()
