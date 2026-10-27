# -*- coding: utf-8 -*-
"""PC2 - HMI tu xa cho PC1.py qua Cloudflare HTTPS.
Khong can cai thu vien Python ngoai; chay: py -3.14 PC2.py
PC1 va PC2 cung doc mot trang thai, bo dem va lich su tu PC1.
"""
import json
import ipaddress
import math
import queue
import threading
import time
import tkinter as tk
import urllib.error
import urllib.request
from datetime import datetime
from tkinter import filedialog, messagebox, ttk
from urllib.parse import urlparse

BG, PANEL, PANEL2 = '#081221', '#14253A', '#1C334B'
TEXT, MUTED, BLUE = '#F1F6FC', '#A1B7C9', '#56BCF2'
GREEN, RED, YELLOW = '#3CDBAA', '#F07482', '#EFC66C'


class CommandRejected(Exception):
    pass


class Remote(threading.Thread):
    def __init__(self, outbox):
        super().__init__(daemon=True)
        self.outbox = outbox
        self.commands = queue.Queue()
        self.active = threading.Event()
        self.stopped = threading.Event()
        self.base = ''
        self.token = ''
        self.last_err = ''

    def connect(self, url, token):
        url = url.strip().rstrip('/')
        parsed = urlparse(url)
        if parsed.scheme not in ('http', 'https') or not parsed.hostname:
            raise ValueError('Nhap URL http(s) hop le')
        if parsed.scheme != 'https':
            try:
                private_host = ipaddress.ip_address(parsed.hostname).is_private
            except ValueError:
                private_host = parsed.hostname == 'localhost'
            if not private_host:
                raise ValueError('HTTP chi cho LAN rieng; tu xa Internet phai dung HTTPS Cloudflare')
        if not token.strip():
            raise ValueError('Chua nhap Access Token tu PC1')
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError('URL khong nen co tai khoan, query hoac fragment')
        self.base = url
        self.token = token.strip()
        while True:
            try:
                self.commands.get_nowait()
            except queue.Empty:
                break
        self.active.set()
        self.last_err = ''

    def disconnect(self):
        self.active.clear()
        while True:
            try:
                self.commands.get_nowait()
            except queue.Empty:
                break

    def enqueue(self, action, **fields):
        if self.active.is_set():
            self.commands.put(dict(action=action, **fields))
        else:
            self.outbox.put(('error', 'Chua ket noi PC1'))

    def request(self, data=None, path='/api'):
        if path == '/api':
            body = json.dumps(data, ensure_ascii=False, allow_nan=False).encode('utf-8')
            req = urllib.request.Request(self.base + '/api', data=body, method='POST',
                     headers={'X-API-Token':self.token, 'Content-Type':'application/json'})
        else:
            req = urllib.request.Request(self.base + path,
                     headers={'X-API-Token':self.token})
        try:
            with urllib.request.urlopen(req, timeout=6.0) as response:
                raw = response.read(2_000_001)
                if len(raw) > 2_000_000:
                    raise RuntimeError('Du lieu phan hoi qua dai')
                if path == '/csv':
                    return raw
                output = json.loads(raw.decode('utf-8'))
                if not output.get('ok'):
                    raise RuntimeError(output.get('error','PC1 tu choi lenh'))
                return output
        except urllib.error.HTTPError as e:
            try:
                data = json.loads(e.read().decode('utf-8'))
                raise CommandRejected(data.get('error',str(e))) from e
            except (UnicodeError, ValueError):
                raise RuntimeError(f'HTTP {e.code}: kiem tra Token/URL/Cloudflare') from e

    def run(self):
        while not self.stopped.is_set():
            if not self.active.is_set():
                self.stopped.wait(0.1)
                continue
            task = None
            try:
                try:
                    task = self.commands.get_nowait()
                except queue.Empty:
                    task = {'action':'status'}
                if task.get('action') == '_csv':
                    self.outbox.put(('csv', self.request(path='/csv')))
                else:
                    result = self.request(task)
                    self.outbox.put(('state', result['state']))
                    if task['action'] != 'status':
                        self.outbox.put(('done',task['action']))
                self.last_err = ''
            except CommandRejected as e:
                self.outbox.put(('error', 'PC1 từ chối: ' + str(e)))
                self.last_err = ''
            except Exception as e:
                detail = str(e)
                if detail != self.last_err or (task is not None and task.get('action') != 'status'):
                    self.outbox.put(('error', detail))
                self.last_err = detail
                self.outbox.put(('offline', None))
            self.stopped.wait(0.65)


class HMI:
    def __init__(self, root):
        self.root = root
        self.root.title('PC2 | PHÂN LOẠI BĂNG TẢI - CLOUDFLARE')
        self.root.geometry('1260x860')
        self.root.minsize(950,700)
        self.root.configure(bg=BG)
        self.mailbox = queue.Queue()
        self.remote = Remote(self.mailbox)
        self.remote.start()
        self.connected = False
        self.snapshot = {}
        self.last_logs = ''
        self.last_history = ''
        self.last_config = None
        self.dirty = False
        self.disconnect_when_off = False
        self.url = tk.StringVar(value='https://xxxx.trycloudflare.com')
        self.token = tk.StringVar()
        self.variables = {k:tk.StringVar(value=str(v)) for k,v in dict(
            mode='Thủ công',main_group='Cao',d0=200,threshold=20,
            presence=5,delay=0.8,pulse=0.45,target=0).items()}
        self.widgets()
        self.root.protocol('WM_DELETE_WINDOW',self.close)
        self.root.after(150,self.update_loop)

    def label(self, parent, text, size=10, color=TEXT, bold=False):
        return tk.Label(parent,text=text,bg=parent.cget('bg'),fg=color,
                 font=('Segoe UI',size,'bold' if bold else 'normal'))

    def button(self, parent, text, action, color=PANEL2):
        return tk.Button(parent,text=text,command=action,bg=color,fg='#041522' if color in (GREEN,BLUE,YELLOW) else TEXT,
                         bd=0,font=('Segoe UI',9,'bold'),pady=10,padx=9,activebackground='#376487')

    def widgets(self):
        head=tk.Frame(self.root,bg=BG)
        head.pack(fill='x',padx=20,pady=16)
        self.label(head,'PC2 · GIÁM SÁT & PHÂN LOẠI SẢN PHẨM',19,GREEN,True).pack(side='left')
        self.label(head,'Cloudflare HTTPS → PC1 → Modbus RTU',10,BLUE).pack(side='right')
        content=tk.Frame(self.root,bg=BG)
        content.pack(fill='both',expand=True,padx=20,pady=(0,12))
        sidebar=tk.Frame(content,bg=PANEL,width=340)
        sidebar.pack(side='left',fill='y',padx=(0,13))
        sidebar.pack_propagate(False)
        wrap=tk.Canvas(sidebar,bg=PANEL,highlightthickness=0)
        scroll=ttk.Scrollbar(sidebar,command=wrap.yview)
        wrap.configure(yscrollcommand=scroll.set)
        scroll.pack(side='right',fill='y');wrap.pack(side='left',fill='both',expand=True)
        inner=tk.Frame(wrap,bg=PANEL)
        anchor=wrap.create_window((0,0),window=inner,anchor='nw')
        inner.bind('<Configure>',lambda _:wrap.configure(scrollregion=wrap.bbox('all')))
        wrap.bind('<Configure>',lambda e:wrap.itemconfigure(anchor,width=e.width))
        def wheel(e):wrap.yview_scroll(-int(e.delta/120),'units')
        wrap.bind('<Enter>',lambda _:wrap.bind_all('<MouseWheel>',wheel))
        wrap.bind('<Leave>',lambda _:wrap.unbind_all('<MouseWheel>'))
        self.section(inner,'01 · KẾT NỐI LAN / CLOUDFLARE')
        self.row_entry(inner,'HTTPS Cloudflare / HTTP IP LAN',self.url)
        self.row_entry(inner,'Access Token',self.token,secret=True)
        actions=tk.Frame(inner,bg=PANEL);actions.pack(fill='x',padx=12,pady=6)
        self.button(actions,'KẾT NỐI',self.connect,GREEN).pack(side='left',fill='x',expand=True,padx=3)
        self.button(actions,'DỪNG & NGẮT',self.disconnect,RED).pack(side='left',fill='x',expand=True,padx=3)
        self.button(inner,'NHẬN QUYỀN PC2',lambda:self.send('claim'),BLUE).pack(fill='x',padx=15,pady=5)
        self.section(inner,'02 · VẬN HÀNH')
        self.button(inner,'▶ CHẠY BĂNG TẢI',lambda:self.send('motor',value=True),GREEN).pack(fill='x',padx=15,pady=4)
        self.button(inner,'■ DỪNG BĂNG TẢI',lambda:self.send('stop'),RED).pack(fill='x',padx=15,pady=4)
        self.button(inner,'↪ GẠT SERVO 1 LẦN',lambda:self.send('servo_pulse'),BLUE).pack(fill='x',padx=15,pady=4)
        self.button(inner,'BẬT ĐỌC CẢM BIẾN',lambda:self.send('sensor',value=True)).pack(fill='x',padx=15,pady=4)
        self.button(inner,'DỪNG ĐỌC CẢM BIẾN',lambda:self.send('sensor',value=False)).pack(fill='x',padx=15,pady=4)
        self.section(inner,'03 · TÙY CHỈNH')
        desc=dict(mode='Chế độ',main_group='Nhóm chính đi thẳng',d0='D0 - băng trống (mm)',
                  threshold='Ngưỡng Cao (mm)',presence='Ngưỡng có vật (mm)',
                  delay='Độ trễ gạt (s) - KHÔNG GIỚI HẠN',pulse='Thời gian kích K2 (s)',target='Số SP chính (0 = ∞)')
        for key, val in self.variables.items():
            self.label(inner,desc[key],9,MUTED).pack(anchor='w',padx=17,pady=(7,1))
            if key in ('mode','main_group'):
                cb=ttk.Combobox(inner,textvariable=val,values=('Thủ công','Tự động') if key=='mode' else ('Cao','Thấp'),state='readonly')
                cb.pack(fill='x',padx=17)
                cb.bind('<<ComboboxSelected>>',lambda _:self.mark_dirty())
            else:
                entry=tk.Entry(inner,textvariable=val,bg=PANEL2,fg=TEXT,insertbackground=TEXT,relief='flat')
                entry.pack(fill='x',padx=17,ipady=5)
                entry.bind('<KeyRelease>',lambda _:self.mark_dirty())
        self.button(inner,'ÁP DỤNG CẤU HÌNH',self.apply,GREEN).pack(fill='x',padx=15,pady=(15,4))
        self.button(inner,'LẤY D0 TỪ CẢM BIẾN',lambda:self.send('calibrate')).pack(fill='x',padx=15,pady=4)
        self.button(inner,'XÓA BỘ ĐẾM',lambda:self.send('reset')).pack(fill='x',padx=15,pady=4)
        self.button(inner,'XÁC NHẬN OFF / XÓA LỖI',lambda:self.send('ack_fault')).pack(fill='x',padx=15,pady=4)
        self.button(inner,'XUẤT CSV',lambda:self.remote.enqueue('_csv')).pack(fill='x',padx=15,pady=(4,20))
        main=tk.Frame(content,bg=BG)
        main.pack(side='left',fill='both',expand=True)
        self.conn=tk.StringVar(value='CHƯA KẾT NỐI PC1')
        tk.Label(main,textvariable=self.conn,bg=PANEL,fg=GREEN,font=('Segoe UI',11,'bold'),anchor='w',padx=15,pady=11).pack(fill='x',pady=(0,12))
        metric_wrap=tk.Frame(main,bg=BG)
        metric_wrap.pack(fill='x')
        self.metricvars={name:tk.StringVar(value='--') for name in ('distance','height','motor','group','total','high','low','main')}
        titles=[('distance','KHOẢNG CÁCH WT53R'),('height','CHIỀU CAO'),('motor','BĂNG TẢI / K1'),('group','PHÂN LOẠI'),
                ('total','TỔNG SẢN PHẨM'),('high','SP CAO'),('low','SP THẤP'),('main','NHÓM CHÍNH')]
        for n,(name,title) in enumerate(titles):
            r,c=divmod(n,4)
            box=tk.Frame(metric_wrap,bg=PANEL)
            box.grid(row=r,column=c,sticky='nsew',padx=4,pady=4)
            metric_wrap.grid_columnconfigure(c,weight=1)
            self.label(box,title,9,MUTED,True).pack(anchor='w',padx=11,pady=(13,5))
            tk.Label(box,textvariable=self.metricvars[name],bg=PANEL,fg=GREEN if name in ('motor','main') else BLUE,
                     font=('Segoe UI',17,'bold'),anchor='w').pack(fill='x',padx=11,pady=(0,15))
        section=tk.Frame(main,bg=PANEL)
        section.pack(fill='both',expand=True,padx=4,pady=12)
        self.label(section,'LỊCH SỬ SẢN PHẨM (dữ liệu chung lưu tại PC1)',11,BLUE,True).pack(anchor='w',padx=12,pady=9)
        cols=('time','height','group','route')
        self.tree=ttk.Treeview(section,columns=cols,show='headings',height=12)
        for col,title,width in [('time','THỜI GIAN',170),('height','CAO (mm)',90),('group','NHÓM',75),('route','PHÂN LUỒNG',150)]:
            self.tree.heading(col,text=title)
            self.tree.column(col,width=width,anchor='center')
        self.tree.pack(fill='both',expand=True,padx=12,pady=(0,9))
        logs=tk.Frame(main,bg=PANEL)
        logs.pack(fill='both',expand=True,padx=4,pady=(0,4))
        self.label(logs,'NHẬT KÝ / CẢNH BÁO',11,BLUE,True).pack(anchor='w',padx=12,pady=8)
        self.logbox=tk.Text(logs,bg='#0D1B2C',fg=MUTED,font=('Consolas',9),height=8,relief='flat',state='disabled')
        self.logbox.pack(fill='both',expand=True,padx=12,pady=(0,11))

    def section(self,parent,title):
        self.label(parent,title,10,BLUE,True).pack(anchor='w',padx=14,pady=(18,8))

    def row_entry(self,parent,title,var,secret=False):
        self.label(parent,title,9,MUTED).pack(anchor='w',padx=15,pady=(6,2))
        tk.Entry(parent,textvariable=var,show='●' if secret else '',bg=PANEL2,fg=TEXT,insertbackground=TEXT,relief='flat').pack(fill='x',padx=15,ipady=6)

    def mark_dirty(self):
        self.dirty=True

    def connect(self):
        try:
            self.remote.connect(self.url.get(),self.token.get())
        except ValueError as e:
            messagebox.showerror('URL / Token',str(e));return
        self.conn.set('ĐANG KẾT NỐI...')

    def disconnect(self):
        if self.remote.active.is_set():
            self.disconnect_when_off = True
            self.remote.enqueue('stop')
            # Xep lenh DUNG. Phai dam bao phan hoi OFF truoc khi dong ket noi;
            # khong duoc coi thao tac gui lenh la da dung motor.
            messagebox.showinfo('An toan','Đã xếp lệnh dừng. Chờ PC1 xác nhận Relay 1 OFF trước khi ngắt hoặc đóng ứng dụng.')
        else:
            self.remote.disconnect()
            self.connected=False
            self.conn.set('ĐÃ NGẮT KẾT NỐI')

    def send(self,action,**data):
        self.remote.enqueue(action,**data)

    def apply(self):
        try:
            body={k:(v.get() if k in ('mode','main_group') else (int(v.get()) if k=='target' else float(v.get().replace(',','.')))) for k,v in self.variables.items()}
            for k in ('d0','threshold','presence','delay','pulse'):
                if not math.isfinite(body[k]):raise ValueError(k+' can la so huu han')
            if body['delay']<0:raise ValueError('Tre gat khong am')
        except ValueError as e:
            messagebox.showerror('Sai thong so',str(e));return
        self.send('configure',**body)
        self.dirty=False

    def render(self,s):
        self.connected=True
        self.snapshot=s
        self.conn.set(f"KẾT NỐI PC1 | {s.get('mode')} | Quyền: {s.get('owner')} | {'COM mở' if s.get('serial_open') else 'COM chưa mở'} | Lỗi: {s.get('fault') or s.get('sensor_error') or 'Không'}")
        cfg=s.get('settings',{})
        if not self.dirty and cfg and cfg!=self.last_config:
            for k,v in cfg.items():
                if k in self.variables:self.variables[k].set(str(v))
            self.last_config=dict(cfg)
        d=s.get('distance_mm')
        h=max(0,cfg.get('d0',0)-d) if type(d) in (int,float) else None
        self.metricvars['distance'].set('--' if d is None else f'{d:.1f} mm')
        self.metricvars['height'].set('--' if h is None else f'{h:.1f} mm')
        motor=s.get('motor_on')
        self.metricvars['motor'].set('CHẠY' if motor is True else 'DỪNG' if motor is False else 'KHÔNG RÕ')
        counts=s.get('counts',{})
        for key in ('total','high','low'):
            self.metricvars[key].set(str(counts.get(key,0)))
        self.metricvars['main'].set(f"{counts.get('main',0)} / {cfg.get('target') or '∞'}")
        hist=s.get('history',[])
        self.metricvars['group'].set(hist[-1]['group'].upper() if hist else 'CHƯA CÓ')
        sign=json.dumps(hist,ensure_ascii=False)
        if sign != self.last_history:
            self.last_history=sign
            for iid in self.tree.get_children():self.tree.delete(iid)
            for r in reversed(hist):
                self.tree.insert('', 'end', values=(r['time'],r['height'],r['group'],r['route']))
        logs=s.get('logs',[])
        sign=json.dumps(logs,ensure_ascii=False)
        if sign != self.last_logs:
            self.last_logs=sign
            self.logbox.configure(state='normal')
            self.logbox.delete('1.0','end')
            self.logbox.insert('end','\n'.join(f"[{r['time']}] {r['text']}" for r in logs))
            self.logbox.see('end')
            self.logbox.configure(state='disabled')

    def save_csv(self,raw):
        path=filedialog.asksaveasfilename(defaultextension='.csv',initialfile='phan_loai_chung_PC1_PC2.csv',filetypes=[('CSV','*.csv')])
        if path:
            try:
                with open(path,'wb') as f:f.write(raw)
                messagebox.showinfo('CSV','Đã lưu '+path)
            except OSError as e:
                messagebox.showerror('Lỗi lưu',str(e))

    def update_loop(self):
        for _ in range(35):
            try:kind,data=self.mailbox.get_nowait()
            except queue.Empty:break
            if kind=='state':self.render(data)
            elif kind=='offline':
                self.connected=False
                self.conn.set('PC1 KHÔNG PHẢN HỒI - kiểm tra Internet / Cloudflare')
            elif kind=='done':
                if data=='configure':self.dirty=False
                if data=='stop':
                    s=self.snapshot
                    if s.get('motor_on') is False and s.get('servo_on') is False:
                        if self.disconnect_when_off:
                            self.disconnect_when_off = False
                            self.remote.disconnect()
                            self.connected = False
                            self.conn.set('Đã xác nhận OFF và ngắt kết nối.')
                        else:
                            self.conn.set('Đã xác nhận OFF.')
            elif kind=='csv':self.save_csv(data)
            elif kind=='error':
                if data.startswith('HTTP') or 'Token' in data:self.conn.set('LỖI: '+data)
                self.append_error(data)
        self.root.after(170,self.update_loop)

    def append_error(self,text):
        self.logbox.configure(state='normal')
        self.logbox.insert('end','\n[LỖI KẾT NỐI] '+text)
        self.logbox.see('end')
        self.logbox.configure(state='disabled')

    def close(self):
        if self.connected:
            if not messagebox.askyesno('Thoát PC2','Bạn đã dừng băng tải và xác nhận motor OFF chưa?\nĐóng PC2 khi motor chạy sẽ kích hoạt watchdog trên PC1 nhưng không thay thế công tắc dừng vật lý.'):
                return
        self.remote.disconnect()
        self.remote.stopped.set()
        self.root.destroy()


def main():
    root=tk.Tk()
    HMI(root)
    root.mainloop()


if __name__=='__main__':
    main()
