"""
L+D Turkey Labeler - unified app.py
Includes:
 - Product DB manager (sqlite)
 - On-screen keyboard for touchscreens (toggleable & persistent)
 - Lot# entry (4-digit numeric) printed top-right on labels
 - Basic scale read & printer test functions (serial)
 - Label image generation (Pillow + python-barcode) with UPC-A
"""

import os
import sqlite3
import time
import tempfile
import serial
from tkinter import *
from tkinter import ttk, filedialog, messagebox
from PIL import Image, ImageDraw, ImageFont
from barcode import UPCA
from barcode.writer import ImageWriter

# Constants
DB_FILE = "ld_turkey_labeler.db"
TEMPLATES_FOLDER = "templates"
LABEL_OUTPUT_FOLDER = os.path.join(tempfile.gettempdir(), "ld_turkey_labels")
os.makedirs(LABEL_OUTPUT_FOLDER, exist_ok=True)
os.makedirs(TEMPLATES_FOLDER, exist_ok=True)


# -------------------- Database helpers / init --------------------
def init_db():
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    # Products table
    cur.execute('''
    CREATE TABLE IF NOT EXISTS products (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
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
    # Settings table
    cur.execute('''
    CREATE TABLE IF NOT EXISTS settings (
        key TEXT PRIMARY KEY,
        value TEXT
    )
    ''')
    # Defaults
    cur.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('touch_keyboard', '1')")
    cur.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('scale_port', 'COM2')")
    cur.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('printer_port', 'COM1')")
    conn.commit()
    conn.close()


def get_setting(key, default=""):
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    try:
        cur.execute("SELECT value FROM settings WHERE key=?", (key,))
        row = cur.fetchone()
    except sqlite3.OperationalError:
        # Auto-create settings table if missing, then return default
        cur.execute('CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)')
        cur.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('touch_keyboard', '1')")
        cur.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('scale_port', 'COM2')")
        cur.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('printer_port', 'COM1')")
        conn.commit()
        row = None
    conn.close()
    return row[0] if row else default


def set_setting(key, value):
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute("REPLACE INTO settings (key, value) VALUES (?, ?)", (key, str(value)))
    conn.commit()
    conn.close()


# -------------------- On-Screen Keyboard --------------------
class OnScreenKeyboard(Toplevel):
    def __init__(self, parent_entry, on_close_callback=None):
        super().__init__()
        self.title("On-Screen Keyboard")
        self.transient()
        self.attributes("-topmost", True)
        self.resizable(False, False)
        self.protocol("WM_DELETE_WINDOW", self.close)

        self.parent_entry = parent_entry
        self.on_close_callback = on_close_callback

        # simple layout
        rows = [
            "1234567890",
            "QWERTYUIOP",
            "ASDFGHJKL",
            "ZXCVBNM",
        ]
        padx = 2
        pady = 2
        for r, row in enumerate(rows):
            for c, ch in enumerate(row):
                b = Button(self, text=ch, width=4, height=2,
                           command=lambda ch=ch: self._insert(ch))
                b.grid(row=r, column=c, padx=padx, pady=pady)

        # space, backspace, close
        Button(self, text="Space", width=12, height=2, command=lambda: self._insert(" ")).grid(row=5, column=0, columnspan=3, pady=6)
        Button(self, text="Back", width=8, height=2, command=self._back).grid(row=5, column=3, columnspan=2, pady=6)
        Button(self, text="Close", width=8, height=2, command=self.close).grid(row=5, column=5, columnspan=2, pady=6)

        # When keyboard is created, focus it so clicks don't immediately trigger entry focus
        self.focus_force()

    def _insert(self, char):
        try:
            self.parent_entry.insert(END, char)
        except Exception:
            pass

    def _back(self):
        try:
            s = self.parent_entry.get()
            if s:
                self.parent_entry.delete(len(s) - 1, END)
        except Exception:
            pass

    def close(self):
        # call callback (so caller can mark keyboard as closed and set a short disable timer)
        try:
            if callable(self.on_close_callback):
                self.on_close_callback()
        except Exception:
            pass
        try:
            self.destroy()
        except Exception:
            pass


# -------------------- App Class --------------------
class App:
    def __init__(self, root, touch_keyboard_var: BooleanVar):
        self.root = root
        self.root.title("L+D Turkey Labeler")
        self.touch_keyboard_var = touch_keyboard_var
        # keyboard state tracking to avoid reopen loop
        self.keyboard_window = None
        self.keyboard_disabled_until = 0.0  # timestamp until keyboard opening is suppressed

        # UI variables
        self.selected_product_code = StringVar()
        self.weight_var = StringVar(value="0.000")
        self.lot_var = StringVar(value="")
        self.template_folder = StringVar(value=TEMPLATES_FOLDER)

        # Build GUI
        self.build_gui()

        # load products on startup
        self.reload_products()

    # ---------------- GUI ----------------
    def build_gui(self):
        frm = ttk.Frame(self.root, padding=10)
        frm.pack(fill=BOTH, expand=True)

        # Product list combobox
        ttk.Label(frm, text="Product").grid(column=0, row=0, sticky=W)
        self.product_combo = ttk.Combobox(frm, textvariable=self.selected_product_code, width=40)
        self.product_combo.grid(column=1, row=0, sticky=W, padx=4, pady=2)

        ttk.Button(frm, text="Manage Products", command=self.open_product_manager).grid(column=2, row=0, padx=6)

        # Weight display
        ttk.Label(frm, text="Weight (lb)").grid(column=0, row=1, sticky=W)
        weight_entry = ttk.Entry(frm, textvariable=self.weight_var, width=20, state='readonly')
        weight_entry.grid(column=1, row=1, sticky=W)

        ttk.Button(frm, text="Read Scale", command=self.action_read_scale).grid(column=2, row=1, padx=6)

        # Price per lb display (read from DB on product select)
        ttk.Label(frm, text="Price Per Pound:").grid(column=0, row=2, sticky=W)
        self.price_label = ttk.Label(frm, text="$0.00")
        self.price_label.grid(column=1, row=2, sticky=W)

        # Lot# field (4 digits numeric only)
        ttk.Label(frm, text='Lot #:').grid(column=0, row=3, sticky=W)
        vcmd = (self.root.register(lambda P: P == "" or (P.isdigit() and len(P) <= 4)), "%P")
        lot_entry = ttk.Entry(frm, textvariable=self.lot_var, width=20, validate="key", validatecommand=vcmd)
        lot_entry.grid(column=1, row=3, sticky=W, padx=2, pady=2)
        # attach keyboard to lot entry
        self.attach_keyboard(lot_entry)

        # Buttons
        btn_frame = ttk.Frame(frm)
        btn_frame.grid(column=0, row=4, columnspan=3, pady=10)
        ttk.Button(btn_frame, text="Print Label", command=self.action_print_label).grid(column=0, row=0, padx=6)
        ttk.Button(btn_frame, text="Test Printer", command=self.test_printer).grid(column=1, row=0, padx=6)
        ttk.Button(btn_frame, text="Options", command=self.open_options).grid(column=2, row=0, padx=6)
        ttk.Button(btn_frame, text="Exit", command=self.root.destroy).grid(column=3, row=0, padx=6)

        # Bind product selection -> update price display
        self.product_combo.bind("<<ComboboxSelected>>", lambda e: self.on_product_selected())

    # ---------------- Product management ----------------
    def reload_products(self):
        conn = sqlite3.connect(DB_FILE)
        cur = conn.cursor()
        cur.execute("SELECT product_code, description, price_per_lb FROM products ORDER BY product_code")
        rows = cur.fetchall()
        conn.close()
        product_labels = [f"{r[0]} - {r[1]} (${(r[2] or 0):.2f}/lb)" for r in rows]
        product_codes = [r[0] for r in rows]
        # map label to code in combobox; store mapping
        self._product_mapping = dict(zip(product_labels, product_codes))
        self.product_combo['values'] = product_labels
        # if current selected code exists, set the combobox label
        cur_code = self.selected_product_code.get()
        for label, code in self._product_mapping.items():
            if code == cur_code:
                self.product_combo.set(label)
                break

    def on_product_selected(self):
        sel_label = self.product_combo.get()
        code = self._product_mapping.get(sel_label)
        if not code:
            return
        # set selected_product_code, update price display
        self.selected_product_code.set(code)
        conn = sqlite3.connect(DB_FILE)
        cur = conn.cursor()
        cur.execute("SELECT price_per_lb FROM products WHERE product_code=?", (code,))
        row = cur.fetchone()
        conn.close()
        price = row[0] if row and row[0] is not None else 0.0
        self.price_label.config(text=f"${price:.2f}")

    def open_product_manager(self):
        win = Toplevel(self.root)
        win.title("Manage Products")
        win.geometry("700x420")

        cols = ("code", "description", "price")
        tree = ttk.Treeview(win, columns=cols, show='headings')
        tree.heading('code', text='Code')
        tree.heading('description', text='Description')
        tree.heading('price', text='Price/lb')
        tree.pack(fill=BOTH, expand=True, padx=8, pady=8)

        sb = ttk.Scrollbar(win, orient='vertical', command=tree.yview)
        tree.configure(yscroll=sb.set)
        sb.pack(side=RIGHT, fill=Y)

        btnf = ttk.Frame(win)
        btnf.pack(pady=6)
        ttk.Button(btnf, text='Add', command=lambda: self._product_form(win, tree, 'add'), width=12).grid(row=0, column=0, padx=6)
        ttk.Button(btnf, text='Edit', command=lambda: self._product_form(win, tree, 'edit', tree.item(tree.selection()[0], 'values')[0]) if tree.selection() else None, width=12).grid(row=0, column=1, padx=6)
        ttk.Button(btnf, text='Delete', command=lambda: self._delete_product_tree(tree), width=12).grid(row=0, column=2, padx=6)
        ttk.Button(btnf, text='Close', command=win.destroy, width=12).grid(row=0, column=3, padx=6)

        # populate
        def refresh():
            conn = sqlite3.connect(DB_FILE)
            cur = conn.cursor()
            cur.execute("SELECT product_code, description, price_per_lb FROM products ORDER BY product_code")
            rows = cur.fetchall()
            conn.close()
            for item in tree.get_children():
                tree.delete(item)
            for r in rows:
                tree.insert("", "end", values=r)

        refresh()
        # attach refresh on window close to update main combobox
        win.protocol("WM_DELETE_WINDOW", lambda: (refresh(), self.reload_products(), win.destroy()))

    def _product_form(self, parent, tree, mode='add', code=None):
        win = Toplevel(parent)
        win.title("Product Form")
        win.geometry("420x520")
        fields = {
            'product_code': StringVar(),
            'description': StringVar(),
            'upc': StringVar(),
            'sell_by': StringVar(),
            'tare': StringVar(),
            'label_format': StringVar(),
            'price_per_lb': StringVar(),
            'min_wt': StringVar(),
            'max_wt': StringVar(),
        }
        for k, v in fields.items():
            Label(win, text=k.replace('_', ' ').title()).pack(pady=2)
            e = Entry(win, textvariable=v, font=('Arial', 12))
            e.pack(pady=2, fill='x', padx=8)
            self.attach_keyboard(e)

        if mode == 'edit' and code:
            conn = sqlite3.connect(DB_FILE)
            cur = conn.cursor()
            cur.execute("SELECT product_code, description, upc, sell_by, tare, label_format, price_per_lb, min_wt, max_wt FROM products WHERE product_code=?", (code,))
            row = cur.fetchone()
            conn.close()
            if row:
                keys = list(fields.keys())
                for i, val in enumerate(row):
                    fields[keys[i]].set("" if val is None else str(val))

        def save_action():
            vals = [fields[k].get() for k in fields]
            conn = sqlite3.connect(DB_FILE)
            cur = conn.cursor()
            try:
                if mode == 'add':
                    cur.execute("INSERT INTO products (product_code, description, upc, sell_by, tare, label_format, price_per_lb, min_wt, max_wt) VALUES (?,?,?,?,?,?,?,?,?)", vals)
                else:
                    cur.execute("UPDATE products SET description=?, upc=?, sell_by=?, tare=?, label_format=?, price_per_lb=?, min_wt=?, max_wt=? WHERE product_code=?", (vals[1], vals[2], vals[3], vals[4], vals[5], vals[6] or 0, vals[7] or 0, vals[8] or 0, vals[0]))
                conn.commit()
                messagebox.showinfo("Saved", "Product saved.")
            except sqlite3.IntegrityError as e:
                messagebox.showerror("Error", f"Could not save product: {e}")
            finally:
                conn.close()
                # refresh parent tree
                try:
                    for item in tree.get_children(): tree.delete(item)
                except Exception:
                    pass
                # If parent has reload capability, call
                try:
                    parent.update()
                except Exception:
                    pass
                self.reload_products()
                win.destroy()

        Button(win, text='Save', command=save_action, width=20).pack(pady=8)
        Button(win, text='Cancel', command=win.destroy, width=20).pack(pady=4)

    def _delete_product_tree(self, tree):
        sel = tree.selection()
        if not sel:
            messagebox.showwarning("Select", "Select a product to delete.")
            return
        code = tree.item(sel[0], 'values')[0]
        if messagebox.askyesno("Confirm", f"Delete product {code}?"):
            conn = sqlite3.connect(DB_FILE)
            cur = conn.cursor()
            cur.execute("DELETE FROM products WHERE product_code=?", (code,))
            conn.commit()
            conn.close()
            self.reload_products()
            # refresh tree
            for item in tree.get_children():
                tree.delete(item)
            # repopulate
            cur = sqlite3.connect(DB_FILE).cursor()
            cur.execute("SELECT product_code, description, price_per_lb FROM products ORDER BY product_code")
            rows = cur.fetchall()
            for r in rows:
                tree.insert("", "end", values=r)
            messagebox.showinfo("Deleted", f"Product {code} deleted.")

    # ---------------- Keyboard integration ----------------
    def attach_keyboard(self, entry_widget):
        # Bind FocusIn to open keyboard; checks for toggle and disabled timer
        def handler(event):
            now = time.time()
            if not self.touch_keyboard_var.get():
                return
            if self.keyboard_window is not None and self.keyboard_window.winfo_exists():
                return
            if now < self.keyboard_disabled_until:
                return
            # open keyboard for this entry
            self.open_keyboard_for_entry(entry_widget)

        entry_widget.bind("<FocusIn>", handler)

    def open_keyboard_for_entry(self, entry_widget):
        # create single keyboard instance and provide a callback for when it's closed
        if self.keyboard_window is not None and self.keyboard_window.winfo_exists():
            return
        def on_kb_close():
            # set short cooldown to avoid immediate reopen loops
            self.keyboard_disabled_until = time.time() + 0.4
            self.keyboard_window = None
        try:
            self.keyboard_window = OnScreenKeyboard(entry_widget, on_close_callback=on_kb_close)
            # position keyboard near bottom of screen center
            try:
                self.keyboard_window.geometry("+100+400")
            except Exception:
                pass
        except Exception as e:
            print("Keyboard open error:", e)
            self.keyboard_window = None

    # ---------------- Scale & Printer (basic) ----------------
    def action_read_scale(self):
        val = self.read_scale()
        if isinstance(val, str) and val.startswith("Error"):
            messagebox.showerror("Scale Error", val)
        elif val is None:
            messagebox.showerror("Scale Error", "No data from scale.")
        else:
            # try to parse numeric from raw read
            try:
                # strip non-digit except dot and minus
                filtered = "".join([c for c in str(val) if (c.isdigit() or c in ".-")])
                wt = float(filtered)
                self.weight_var.set(f"{wt:.3f}")
            except Exception:
                # just display raw
                self.weight_var.set(str(val))

    def read_scale(self):
        # Attempt to read one line from the scale serial port (adjust baud as appropriate)
        port = get_setting("scale_port", "COM2")
        try:
            ser = serial.Serial(port, baudrate=9600, timeout=2)
            time.sleep(0.05)
            raw = ser.readline().decode(errors="ignore").strip()
            ser.close()
            return raw if raw else None
        except Exception as e:
            return f"Error: {e}"

    def test_printer(self):
        port = get_setting("printer_port", "COM1")
        try:
            ser = serial.Serial(port, baudrate=38400, timeout=2)
            # simple sample DPL/text (Datamax) - adjust to your Datamax language if needed
            label = 'N\nA50,50,0,4,1,1,N,"L+D Turkey Labeler Test"\nP1\n'
            ser.write(label.encode("utf-8"))
            ser.close()
            messagebox.showinfo("Printer", "Test label sent.")
        except Exception as e:
            messagebox.showerror("Printer Error", f"{e}")

    # ---------------- Label generation & printing ----------------
    def action_print_label(self):
        # find product row by selected code
        code = self.selected_product_code.get()
        if not code:
            messagebox.showwarning("Product", "Select a product first.")
            return
        # fetch product row
        conn = sqlite3.connect(DB_FILE)
        cur = conn.cursor()
        cur.execute("SELECT * FROM products WHERE product_code=?", (code,))
        prod = cur.fetchone()
        conn.close()
        if not prod:
            messagebox.showerror("Product", "Product not found in DB.")
            return

        try:
            weight = float(self.weight_var.get())
        except Exception:
            weight = 0.0
        try:
            price_per_lb = float(prod[7] or 0)
        except Exception:
            price_per_lb = 0.0
        total_price = weight * price_per_lb

        values = {
            'PRODUCT_CODE': prod[1],
            'DESCRIPTION': prod[2],
            'UPC': prod[3],
            'SELL_BY': prod[4],
            'TARE': prod[5],
            'WEIGHT': f"{weight:.3f}",
            'PRICE': f"{total_price:.2f}",
            'PRICE_PER_LB': f"{price_per_lb:.2f}",
            'LOGO_PATH': prod[10],
            'LOT': self.lot_var.get()
        }

        # generate label image (PNG) and save to temp folder
        fn = self.render_label_image(values)
        if fn:
            # optionally: send to printer here. For now show message and open file location
            messagebox.showinfo("Label", f"Label generated: {fn}")
            try:
                # open file location (platform dependent) - we'll not call on headless systems
                if os.name == 'nt':
                    os.startfile(os.path.dirname(fn))
            except Exception:
                pass

            # Also attempt to send a simple test text label to datamax if serial configured:
            # (This is a very basic example — for production you should build real Datamax/DPL commands.)
            # self.test_printer()  # user can use Test Printer separately
        else:
            messagebox.showerror("Label", "Failed to generate label.")

    def render_label_image(self, values, size=(400, 300)):
        """
        Create a label PNG using Pillow and python-barcode (UPC-A).
        Returns path to PNG or None on error.
        """
        try:
            width, height = size
            bg = Image.new("RGB", (width, height), "white")
            draw = ImageDraw.Draw(bg)
            # fonts (fallback to default)
            try:
                fnt = ImageFont.truetype("arial.ttf", 18)
                fnt_bold = ImageFont.truetype("arialbd.ttf", 18)
            except Exception:
                fnt = ImageFont.load_default()
                fnt_bold = ImageFont.load_default()

            # Draw product description
            draw.text((10, 10), values.get("DESCRIPTION", ""), font=fnt_bold, fill="black")

            # Draw weight & price
            draw.text((10, 40), f"Weight: {values.get('WEIGHT')} lb", font=fnt, fill="black")
            draw.text((10, 60), f"Price/lb: ${values.get('PRICE_PER_LB')}", font=fnt, fill="black")
            draw.text((10, 80), f"Total: ${values.get('PRICE')}", font=fnt_bold, fill="black")

            # Draw sell by
            if values.get("SELL_BY"):
                draw.text((10, 100), f"Sell By: {values.get('SELL_BY')}", font=fnt, fill="black")

            # Draw LOT in top-right (anchor NE)
            lot = values.get("LOT")
            if lot:
                # right-aligned, small padding
                try:
                    draw.text((width - 10, 10), f"Lot#: {lot}", font=fnt_bold, fill="black", anchor="rs")
                except Exception:
                    # fallback if anchor unsupported
                    w, h = draw.textsize(f"Lot#: {lot}", font=fnt_bold)
                    draw.text((width - w - 10, 10), f"Lot#: {lot}", font=fnt_bold, fill="black")

            # Create UPC-A barcode image if UPC present
            upc = values.get("UPC") or ""
            barcode_img = None
            if upc and len(upc) in (11, 12):  # python-barcode expects 11 or 12 digits for UPCA (it will compute checksum if 11)
                try:
                    upc_clean = upc if len(upc) == 11 else upc[:-1] if len(upc) == 12 else upc
                    upc_obj = UPCA(upc_clean, writer=ImageWriter())
                    bfile = os.path.join(LABEL_OUTPUT_FOLDER, f"barcode_{upc}.png")
                    upc_obj.write(open(bfile, "wb"), {"module_height": 10.0, "module_width": 0.2, "font_size": 10})
                    barcode_img = Image.open(bfile)
                except Exception:
                    barcode_img = None

            # Paste barcode at bottom-left if created
            if barcode_img:
                # resize barcode to fit
                bw, bh = barcode_img.size
                target_w = min(width - 20, bw)
                if bw > target_w:
                    ratio = target_w / bw
                    barcode_img = barcode_img.resize((int(bw * ratio), int(bh * ratio)))
                bg.paste(barcode_img, (10, height - barcode_img.size[1] - 10))

            # Save file
            out_fn = os.path.join(LABEL_OUTPUT_FOLDER, f"label_{int(time.time())}.png")
            bg.save(out_fn)
            return out_fn
        except Exception as e:
            print("Label render error:", e)
            return None

    # ---------------- Options window ----------------
    def open_options(self):
        win = Toplevel(self.root)
        win.title("Options")
        frm = ttk.Frame(win, padding=10)
        frm.pack(fill=BOTH, expand=True)

        # Template folder selector
        ttk.Label(frm, text="Templates Folder:").grid(column=0, row=0, sticky=W)
        e = ttk.Entry(frm, textvariable=self.template_folder, width=40)
        e.grid(column=1, row=0, sticky=W)
        ttk.Button(frm, text="Browse", command=lambda: self._browse_template_folder(e)).grid(column=2, row=0, padx=6)

        # Scale port
        ttk.Label(frm, text="Scale Port:").grid(column=0, row=1, sticky=W)
        scale_port_var = StringVar(value=get_setting("scale_port", "COM2"))
        e2 = ttk.Entry(frm, textvariable=scale_port_var)
        e2.grid(column=1, row=1, sticky=W)

        # Printer port
        ttk.Label(frm, text="Printer Port:").grid(column=0, row=2, sticky=W)
        printer_port_var = StringVar(value=get_setting("printer_port", "COM1"))
        e3 = ttk.Entry(frm, textvariable=printer_port_var)
        e3.grid(column=1, row=2, sticky=W)

        def save_settings():
            set_setting("scale_port", scale_port_var.get())
            set_setting("printer_port", printer_port_var.get())
            messagebox.showinfo("Saved", "Options saved.")

        ttk.Button(frm, text="Save", command=save_settings).grid(column=0, row=7, columnspan=2, pady=6)

        # Touch keyboard toggle (persisted)
        chk = ttk.Checkbutton(frm, text="Enable Touch Keyboard", variable=self.touch_keyboard_var,
                              command=lambda: set_setting("touch_keyboard", "1" if self.touch_keyboard_var.get() else "0"))
        chk.grid(column=0, row=8, columnspan=2, pady=6)

    def _browse_template_folder(self, entry_widget):
        d = filedialog.askdirectory(initialdir=self.template_folder.get() or ".")
        if d:
            self.template_folder.set(d)

# -------------------- Main --------------------
def main():
    init_db()

    root = Tk()

    # Create BooleanVar bound to the root BEFORE building the app UI so widgets can read it
    touch_keyboard_var = BooleanVar(master=root)
    touch_keyboard_var.set(get_setting("touch_keyboard", "1") == "1")

    app = App(root, touch_keyboard_var)
    root.mainloop()


if __name__ == "__main__":
    main()
