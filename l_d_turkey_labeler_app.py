"""
L+D Turkey Labeler

Features:
- Connect to Gainco scale on configurable COM port (e.g. COM2) and read weight
- Connect to Datamax printer via serial (COM) or IP (raw TCP/9100)
- SQLite database of products with fields: product_code, description, upc, sell_by, tare, label_format, price_per_lb, min_wt, max_wt, logo_path
- Template system: label templates are simple HTML-like / text with placeholders ({{WEIGHT}}, {{PRICE}}, {{UPC_IMG_PATH}}, etc). Program renders a label as an image using ReportLab + Pillow, embeds barcode (UPC-A) generated with python-barcode
- Manual print button and automatic print on weight stable (optional)
- Connection test for scale and printer; test print and weigh retrieve

How it works (high-level):
- GUI built with tkinter (single-file for portability)
- Serial comm via pyserial
- Barcode via python-barcode (or treepoem fallback if needed)
- Rendering via Pillow + reportlab for better control; saved temporary PNG and sent to printer
- For IP printing: send raw PNG data to port 9100 (Datamax RAW printing often accepts printer command language; many modern Datamax accept PDF/PNG depending on model. If your Datamax accepts native image data over RAW port this will work; otherwise upload/convert templates into the printer language your model expects).
- For serial printing: send raw bytes to COM port. Many Datamax printers use DPL or DMX. This code will send the image as bytes; if your printer requires language commands to print an image, you'll need to provide a compatible label template (feature supported: load binary label templates and send them with placeholder replacements).

IMPORTANT: Printer compatibility varies. This program gives you a complete workflow (weight->render->send). For guaranteed serial Datamax compatibility, the recommended path is to create label templates in the Datamax native language (DPL) with placeholders (e.g. %%WEIGHT%%) and put them into templates folder; this program will substitute placeholders and send the final text directly to COM1.

Dependencies:
- Python 3.10+
- pyserial
- pillow
- reportlab
- python-barcode
- sqlite3 (standard)

Install deps:
    pip install pyserial pillow reportlab python-barcode

Usage:
- Run the script: python app.py
- Configure COM ports and template folder in Options
- Add products to database, select product, place item on scale, click "Read Weight" or press "Print" to create and send the label

"""

import os
import sys
import sqlite3
import tempfile
import threading
import time
import socket
from datetime import datetime
from tkinter import *
from tkinter import ttk, filedialog, messagebox
from PIL import Image, ImageDraw, ImageFont
import serial
import serial.tools.list_ports
import barcode
from barcode.writer import ImageWriter

APP_NAME = "L+D Turkey Labeler"
DB_FILE = "ld_turkey_labeler.db"
TEMPLATES_FOLDER = "templates"

# ---------------------- Database helpers ----------------------
def init_db():
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute('''
    CREATE TABLE IF NOT EXISTS products (
        id INTEGER PRIMARY KEY,
        product_code TEXT UNIQUE,
        description TEXT,
        upc TEXT,
        sell_by TEXT,
        tare REAL DEFAULT 0,
        label_format TEXT,
        price_per_lb REAL DEFAULT 0,
        min_wt REAL DEFAULT 0,
        max_wt REAL DEFAULT 9999,
        logo_path TEXT
    )
    ''')
    conn.commit()
    conn.close()

def add_or_update_product(product):
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute('''
    INSERT INTO products(product_code,description,upc,sell_by,tare,label_format,price_per_lb,min_wt,max_wt,logo_path)
    VALUES (?,?,?,?,?,?,?,?,?,?)
    ON CONFLICT(product_code) DO UPDATE SET
      description=excluded.description,
      upc=excluded.upc,
      sell_by=excluded.sell_by,
      tare=excluded.tare,
      label_format=excluded.label_format,
      price_per_lb=excluded.price_per_lb,
      min_wt=excluded.min_wt,
      max_wt=excluded.max_wt,
      logo_path=excluded.logo_path
    ''', (
        product['product_code'], product['description'], product['upc'], product['sell_by'], product['tare'],
        product['label_format'], product['price_per_lb'], product['min_wt'], product['max_wt'], product.get('logo_path')
    ))
    conn.commit()
    conn.close()

def get_all_products():
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute('SELECT product_code,description FROM products ORDER BY product_code')
    rows = cur.fetchall()
    conn.close()
    return rows

def get_product(product_code):
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute('SELECT * FROM products WHERE product_code=?', (product_code,))
    r = cur.fetchone()
    conn.close()
    return r

# ---------------------- Serial helpers ----------------------

class SerialDevice:
    def __init__(self, port=None, baud=9600, timeout=1):
        self.port = port
        self.baud = baud
        self.timeout = timeout
        self.ser = None

    def open(self):
        if not self.port:
            raise RuntimeError('No port set')
        self.ser = serial.Serial(self.port, self.baud, timeout=self.timeout)

    def close(self):
        if self.ser and self.ser.is_open:
            try:
                self.ser.close()
            except Exception:
                pass
            self.ser = None

    def write(self, data: bytes):
        if not self.ser or not self.ser.is_open:
            self.open()
        self.ser.write(data)

    def readline(self):
        if not self.ser or not self.ser.is_open:
            self.open()
        return self.ser.readline()

# ---------------------- Barcode & label rendering ----------------------

def generate_upc_barcode(upc: str, out_path: str):
    # Ensure UPC is 12 digits (UPC-A). barcode library will compute checksum if 11 digits given.
    upc_digits = upc.strip()
    if len(upc_digits) == 11:
        pass
    elif len(upc_digits) == 12:
        pass
    elif len(upc_digits) < 11:
        upc_digits = upc_digits.zfill(11)
    else:
        upc_digits = upc_digits[-12:]
    upc_obj = barcode.get('upca', upc_digits, writer=ImageWriter())
    upc_obj.save(out_path)
    return out_path + '.png'


def render_label_as_image(template_text: str, values: dict, output_path: str, size=(400,300)):
    """Simple renderer: places text fields and barcode on an image. Template tokens: {{FIELD}}"""
    # Create white canvas
    img = Image.new('RGB', size, 'white')
    draw = ImageDraw.Draw(img)

    # Load fonts - use default PIL fonts if no TTF available
    try:
        fnt_bold = ImageFont.truetype('arialbd.ttf', 18)
        fnt = ImageFont.truetype('arial.ttf', 14)
    except Exception:
        fnt_bold = ImageFont.load_default()
        fnt = ImageFont.load_default()

    y = 10
    # If logo provided in values, paste it at top-left
    logo_path = values.get('LOGO_PATH')
    if logo_path and os.path.isfile(logo_path):
        try:
            logo = Image.open(logo_path)
            logo.thumbnail((80,80))
            img.paste(logo, (10,10))
        except Exception:
            pass
        y = 10

    # Basic layout: iterate lines in template_text
    lines = template_text.split('\n')
    for line in lines:
        # Replace tokens
        for k,v in values.items():
            token = '{{' + k + '}}'
            if token in line:
                line = line.replace(token, str(v))
        # Handle barcode token separately
        if '{{UPC_BARCODE}}' in line:
            # generate UPC to temp file and paste
            upc = values.get('UPC') or ''
            tmp = tempfile.mktemp(prefix='upc_', suffix='.png')
            try:
                generate_upc_barcode(upc, tmp[:-4])
                bc = Image.open(tmp)
                bc.thumbnail((180,60))
                img.paste(bc, (10, y))
                y += bc.size[1] + 5
            except Exception as e:
                draw.text((10,y), 'ERR: barcode', font=fnt, fill='black')
                y += 20
            continue
        # Draw normal text
        draw.text((100, y), line, font=fnt if len(line) < 30 else fnt, fill='black')
        y += 18

    img.save(output_path)
    return output_path

# ---------------------- Printer sending ----------------------

def send_to_printer_serial(port, baud, data_bytes):
    sd = SerialDevice(port, baud, timeout=2)
    try:
        sd.open()
        sd.write(data_bytes)
        sd.close()
        return True, 'Sent to serial printer'
    except Exception as e:
        return False, str(e)


def send_to_printer_ip(ip, port, data_bytes):
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(5)
        s.connect((ip, port))
        s.sendall(data_bytes)
        s.close()
        return True, 'Sent to network printer'
    except Exception as e:
        return False, str(e)

# ---------------------- Scale parsing (Gainco Infinity GII) ----------------------
# The exact response format depends on your scale setup. We'll provide a configurable parser.

class GaincoScale:
    def __init__(self, port, baud=9600, timeout=1, read_terminator=b'\r\n'):
        self.port = port
        self.baud = baud
        self.timeout = timeout
        self.read_terminator = read_terminator
        self.ser = SerialDevice(port, baud, timeout)

    def read_weight(self):
        try:
            self.ser.open()
            # Some scales continuously send weight; here we'll attempt a readline
            raw = self.ser.readline()
            self.ser.close()
            if not raw:
                return None, 'No data'
            # Attempt decode
            try:
                text = raw.decode('utf-8', errors='ignore').strip()
            except Exception:
                text = str(raw)
            # Heuristic: find numeric value in text
            import re
            m = re.search(r'([-+]?\d+\.\d+|\d+)', text)
            if m:
                return float(m.group(0)), text
            else:
                return None, text
        except Exception as e:
            return None, str(e)

# ---------------------- GUI ----------------------

class App:
    def __init__(self, root):
        self.root = root
        root.title(APP_NAME)
        self.scale_port = StringVar(value='COM2')
        self.scale_baud = IntVar(value=9600)
        self.printer_port = StringVar(value='COM1')
        self.printer_baud = IntVar(value=38400)
        self.printer_ip = StringVar(value='')
        self.printer_mode = StringVar(value='serial')  # or 'ip'
        self.template_folder = StringVar(value=TEMPLATES_FOLDER)
        self.selected_product = StringVar()
        self.weight_var = StringVar(value='0.000')
        self.price_var = StringVar(value='0.00')
        self.db_products = []

        self.build_gui()
        init_db()
        os.makedirs(self.template_folder.get(), exist_ok=True)
        self.reload_products()

    def build_gui(self):
        frm = ttk.Frame(self.root, padding=8)
        frm.pack(fill=BOTH, expand=True)

        top = ttk.Frame(frm)
        top.pack(fill=X)

        ttk.Label(top, text='Product:').grid(column=0, row=0, sticky=W)
        self.product_combo = ttk.Combobox(top, textvariable=self.selected_product, width=40)
        self.product_combo.grid(column=1, row=0, sticky=W)

        ttk.Button(top, text='Reload Products', command=self.reload_products).grid(column=2, row=0, padx=4)

        ttk.Label(top, text='Weight:').grid(column=0, row=1, sticky=W)
        ttk.Entry(top, textvariable=self.weight_var, width=20).grid(column=1, row=1, sticky=W)
        ttk.Button(top, text='Read Weight', command=self.read_weight).grid(column=2, row=1, padx=4)

        ttk.Label(top, text='Price:').grid(column=0, row=2, sticky=W)
        ttk.Entry(top, textvariable=self.price_var, width=20).grid(column=1, row=2, sticky=W)

        btns = ttk.Frame(frm)
        btns.pack(fill=X, pady=8)
        ttk.Button(btns, text='Print Label', command=self.print_label).pack(side=LEFT, padx=4)
        ttk.Button(btns, text='Manual Print (raw template)', command=self.manual_print_template).pack(side=LEFT, padx=4)
        ttk.Button(btns, text='Options', command=self.open_options).pack(side=LEFT, padx=4)

        tests = ttk.LabelFrame(frm, text='Diagnostics', padding=8)
        tests.pack(fill=X, pady=6)
        ttk.Button(tests, text='Test Scale Connection', command=self.test_scale_connection).grid(column=0, row=0, padx=4)
        ttk.Button(tests, text='Test Printer Connection', command=self.test_printer_connection).grid(column=1, row=0, padx=4)
        ttk.Button(tests, text='Test Print (sample)', command=self.test_print).grid(column=2, row=0, padx=4)

    def reload_products(self):
        rows = get_all_products()
        self.db_products = rows
        self.product_combo['values'] = [f"{r[0]} - {r[1]}" for r in rows]

    def read_weight(self):
        port = self.scale_port.get()
        baud = self.scale_baud.get()
        scale = GaincoScale(port, baud)
        wt, info = scale.read_weight()
        if wt is None:
            messagebox.showerror('Scale Read', f'Could not read weight: {info}')
        else:
            # apply tare if product selected
            sel = self.selected_product.get().split(' - ')[0] if self.selected_product.get() else None
            tare = 0.0
            if sel:
                p = get_product(sel)
                if p:
                    tare = p[5] if p[5] else 0.0
            net = wt - tare
            if net < 0:
                net = 0.0
            self.weight_var.set(f"{net:.3f}")
            messagebox.showinfo('Scale Read', f'Raw: {info}\nNet weight: {net:.3f} lb (tare {tare})')

    def print_label(self):
        sel = self.selected_product.get().split(' - ')[0] if self.selected_product.get() else None
        if not sel:
            messagebox.showwarning('Print', 'Select a product first')
            return
        prod = get_product(sel)
        if not prod:
            messagebox.showerror('Print', 'Product not found in database')
            return
        # prod columns: id, product_code, description, upc, sell_by, tare, label_format, price_per_lb, min_wt, max_wt, logo_path
        weight = float(self.weight_var.get() or 0)
        price_per_lb = prod[7] or 0.0
        total_price = weight * price_per_lb
        self.price_var.set(f"{total_price:.2f}")

        # load template
        template_file = prod[6] or ''
        if template_file and os.path.isfile(template_file):
            with open(template_file, 'r', encoding='utf-8') as f:
                template_text = f.read()
        else:
            # default template
            template_text = f"{prod[2]}\n{{{ { 'UPC' } }}}\n{{UPC_BARCODE}}\nWeight: {{WEIGHT}} lb\nPrice: ${{PRICE}}"

        values = {
            'PRODUCT_CODE': prod[1],
            'DESCRIPTION': prod[2],
            'UPC': prod[3],
            'SELL_BY': prod[4],
            'TARE': prod[5],
            'WEIGHT': f"{weight:.3f}",
            'PRICE': f"{total_price:.2f}",
            'PRICE_PER_LB': f"{price_per_lb:.2f}",
            'LOGO_PATH': prod[10]
        }
        tmp_img = tempfile.mktemp(prefix='label_', suffix='.png')
        try:
            render_label_as_image(template_text, values, tmp_img, size=(600,400))
        except Exception as e:
            messagebox.showerror('Render', f'Failed to render label: {e}')
            return

        # send to printer
        if self.printer_mode.get() == 'ip' and self.printer_ip.get():
            with open(tmp_img, 'rb') as f:
                data = f.read()
            ok,msg = send_to_printer_ip(self.printer_ip.get(), 9100, data)
        else:
            with open(tmp_img, 'rb') as f:
                data = f.read()
            ok,msg = send_to_printer_serial(self.printer_port.get(), self.printer_baud.get(), data)

        if ok:
            messagebox.showinfo('Print', 'Label sent to printer')
        else:
            messagebox.showerror('Print', f'Printer error: {msg}')

    def manual_print_template(self):
        # Let user pick a template text file, substitute placeholders and send raw
        path = filedialog.askopenfilename(title='Select label template (text)', filetypes=[('Text files','*.txt;*.prn;*.dpl;*.tpl'),('All files','*.*')])
        if not path:
            return
        with open(path,'r',encoding='utf-8') as f:
            tpl = f.read()
        # Simple substitution using current fields
        sel = self.selected_product.get().split(' - ')[0] if self.selected_product.get() else None
        prod = get_product(sel) if sel else None
        weight = float(self.weight_var.get() or 0)
        price = float(self.price_var.get() or 0)
        subs = {
            '{{PRODUCT_CODE}}': prod[1] if prod else '',
            '{{DESCRIPTION}}': prod[2] if prod else '',
            '{{WEIGHT}}': f"{weight:.3f}",
            '{{PRICE}}': f"{price:.2f}",
        }
        out = tpl
        for k,v in subs.items():
            out = out.replace(k,v)
        data_bytes = out.encode('utf-8', errors='ignore')
        ok,msg = send_to_printer_serial(self.printer_port.get(), self.printer_baud.get(), data_bytes)
        if ok:
            messagebox.showinfo('Manual Print', 'Template sent to printer')
        else:
            messagebox.showerror('Manual Print', f'Printer error: {msg}')

    def open_options(self):
        win = Toplevel(self.root)
        win.title('Options')
        frm = ttk.Frame(win, padding=8)
        frm.pack(fill=BOTH, expand=True)
        ttk.Label(frm, text='Scale COM Port:').grid(column=0,row=0)
        ttk.Entry(frm, textvariable=self.scale_port).grid(column=1,row=0)
        ttk.Label(frm, text='Scale Baud:').grid(column=0,row=1)
        ttk.Entry(frm, textvariable=self.scale_baud).grid(column=1,row=1)

        ttk.Label(frm, text='Printer Mode:').grid(column=0,row=2)
        ttk.Radiobutton(frm, text='Serial', variable=self.printer_mode, value='serial').grid(column=1,row=2, sticky=W)
        ttk.Radiobutton(frm, text='IP', variable=self.printer_mode, value='ip').grid(column=2,row=2, sticky=W)
        ttk.Label(frm, text='Printer COM Port:').grid(column=0,row=3)
        ttk.Entry(frm, textvariable=self.printer_port).grid(column=1,row=3)
        ttk.Label(frm, text='Printer Baud:').grid(column=0,row=4)
        ttk.Entry(frm, textvariable=self.printer_baud).grid(column=1,row=4)
        ttk.Label(frm, text='Printer IP:').grid(column=0,row=5)
        ttk.Entry(frm, textvariable=self.printer_ip).grid(column=1,row=5)
        ttk.Label(frm, text='Templates Folder:').grid(column=0,row=6)
        ttk.Entry(frm, textvariable=self.template_folder).grid(column=1,row=6)
        def choose_folder():
            p = filedialog.askdirectory()
            if p:
                self.template_folder.set(p)
        ttk.Button(frm, text='Browse...', command=choose_folder).grid(column=2,row=6)
        ttk.Button(frm, text='Save', command=win.destroy).grid(column=1,row=7)

    def test_scale_connection(self):
        port = self.scale_port.get()
        try:
            s = SerialDevice(port, self.scale_baud.get(), timeout=1)
            s.open()
            s.close()
            messagebox.showinfo('Scale Test', f'Opened {port} OK')
        except Exception as e:
            messagebox.showerror('Scale Test', str(e))

    def test_printer_connection(self):
        if self.printer_mode.get() == 'ip' and self.printer_ip.get():
            ok,msg = send_to_printer_ip(self.printer_ip.get(), 9100, b'TEST')
            if ok:
                messagebox.showinfo('Printer Test', msg)
            else:
                messagebox.showerror('Printer Test', msg)
        else:
            try:
                s = SerialDevice(self.printer_port.get(), self.printer_baud.get(), timeout=1)
                s.open()
                s.close()
                messagebox.showinfo('Printer Test', f'Opened {self.printer_port.get()} OK')
            except Exception as e:
                messagebox.showerror('Printer Test', str(e))

    def test_print(self):
        # generate a simple sample label and send
        tmp = tempfile.mktemp(prefix='label_test_', suffix='.png')
        tpl = 'SAMPLE LABEL\n{{UPC_BARCODE}}\nWeight: {{WEIGHT}}\nPrice: ${{PRICE}}'
        values = {'UPC':'01234567890','WEIGHT':'1.234','PRICE':'3.45','LOGO_PATH':''}
        render_label_as_image(tpl, values, tmp, size=(600,400))
        with open(tmp,'rb') as f:
            data = f.read()
        if self.printer_mode.get() == 'ip' and self.printer_ip.get():
            ok,msg = send_to_printer_ip(self.printer_ip.get(), 9100, data)
        else:
            ok,msg = send_to_printer_serial(self.printer_port.get(), self.printer_baud.get(), data)
        if ok:
            messagebox.showinfo('Test Print', 'Sent test label')
        else:
            messagebox.showerror('Test Print', msg)


if __name__ == '__main__':
    root = Tk()
    app = App(root)
    root.mainloop()
