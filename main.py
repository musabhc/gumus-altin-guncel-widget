import tkinter as tk
from tkinter import ttk
import yfinance as yf
import threading
import time
import sys
import json
import os
import winreg
import ctypes
from datetime import datetime
from tkinter import messagebox


class TransactionManager:
    def __init__(self, filename="transactions.json"):
        self.filename = os.path.join(os.path.dirname(os.path.abspath(__file__)), filename)
        self.transactions = self.load()

    def load(self):
        if not os.path.exists(self.filename):
            return []
        try:
            with open(self.filename, 'r', encoding='utf-8') as f:
                return json.load(f)
        except:
            return []

    def save(self, transaction):
        self.transactions.append(transaction)
        self.save_all()
        
    def save_all(self):
        with open(self.filename, 'w', encoding='utf-8') as f:
            json.dump(self.transactions, f, indent=4)
            
    def get_summary(self):
        total_investment = sum(t['total_tl'] for t in self.transactions)
        total_gumus = sum(t['amount_g'] for t in self.transactions)
        return total_investment, total_gumus

class AutoStartManager:
    def __init__(self, app_name="PiyasaWidget"):
        self.app_name = app_name
        self.key_path = r"Software\Microsoft\Windows\CurrentVersion\Run"
        
    def is_enabled(self):
        try:
            key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, self.key_path, 0, winreg.KEY_READ)
            winreg.QueryValueEx(key, self.app_name)
            winreg.CloseKey(key)
            return True
        except FileNotFoundError:
            return False
            
    def set_autostart(self, enable=True):
        try:
            key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, self.key_path, 0, winreg.KEY_ALL_ACCESS)
            if enable:
                # Script path logic
                exe_path = sys.executable
                script_path = os.path.abspath(__file__)
                # If running as .py, use pythonw.exe to run it without console
                if exe_path.endswith("python.exe") or exe_path.endswith("pythonw.exe"):
                     cmd = f'"{exe_path.replace("python.exe", "pythonw.exe")}" "{script_path}"'
                else:
                     # Frozen exe (pyinstaller)
                     cmd = f'"{sys.executable}"'
                
                winreg.SetValueEx(key, self.app_name, 0, winreg.REG_SZ, cmd)
            else:
                try:
                    winreg.DeleteValue(key, self.app_name)
                except FileNotFoundError:
                    pass
            winreg.CloseKey(key)
        except Exception as e:
            messagebox.showerror("Hata", f"Kayıt defteri hatası: {e}")

class PortfolioManagerDialog(tk.Toplevel):
    def __init__(self, parent, manager, on_save_callback, current_dollar_rate):
        super().__init__(parent)
        self.manager = manager
        self.on_save_callback = on_save_callback
        self.current_dollar_rate = current_dollar_rate
        self.title("Portföy Yönetimi")
        self.geometry("750x500")
        self.configure(bg="#2d2d2d")
        
        # --- Sol Panel: Liste ---
        left_frame = tk.Frame(self, bg="#2d2d2d", padx=10, pady=10)
        left_frame.pack(side="left", fill="both", expand=True)
        
        tk.Label(left_frame, text="İşlem Geçmişi", bg="#2d2d2d", fg="#cccccc", font=("Segoe UI", 12, "bold")).pack(anchor="w", pady=(0, 10))
        
        # Treeview Stil
        style = ttk.Style()
        style.theme_use("clam")
        style.configure("Treeview", 
                        background="#3d3d3d", 
                        foreground="white", 
                        fieldbackground="#3d3d3d", 
                        borderwidth=0,
                        rowheight=25,
                        font=("Segoe UI", 9))
        
        style.configure("Treeview.Heading", 
                        background="#252526", 
                        foreground="white", 
                        relief="flat",
                        font=("Segoe UI", 9, "bold"))
                        
        style.map("Treeview", background=[('selected', '#007acc')])
        
        # Treeview
        columns = ("date", "amount", "cost", "total", "delete")
        self.tree = ttk.Treeview(left_frame, columns=columns, show="headings", height=15)
        
        self.tree.heading("date", text="Tarih")
        self.tree.heading("amount", text="Miktar (Gr)")
        self.tree.heading("cost", text="Birim Maliyet")
        self.tree.heading("total", text="Toplam")
        self.tree.heading("delete", text="")
        
        self.tree.column("date", width=90, anchor="center")
        self.tree.column("amount", width=90, anchor="center")
        self.tree.column("cost", width=90, anchor="center")
        self.tree.column("total", width=110, anchor="center")
        self.tree.column("delete", width=40, anchor="center")
        
        self.tree.pack(side="left", fill="both", expand=True)
        self.tree.bind("<ButtonRelease-1>", self.on_click)
        
        # Scrollbar
        scrollbar = ttk.Scrollbar(left_frame, orient="vertical", command=self.tree.yview)
        scrollbar.pack(side="right", fill="y")
        self.tree.configure(yscrollcommand=scrollbar.set)
        
        # --- Sağ Panel: Ekleme ---
        right_frame = tk.Frame(self, bg="#333333", padx=20, pady=20) # Biraz daha açık arka plan
        right_frame.pack(side="right", fill="y")
        
        tk.Label(right_frame, text="Yeni İşlem", bg="#333333", fg="white", font=("Segoe UI", 14, "bold")).pack(anchor="w", pady=(0, 20))
        
        style_entry = {"bg": "#454545", "fg": "white", "insertbackground": "white", "relief": "flat", "font": ("Segoe UI", 10)}
        style_label = {"bg": "#333333", "fg": "#cccccc", "font": ("Segoe UI", 9)}
        
        # 1. Tarih
        tk.Label(right_frame, text="Tarih", **style_label).pack(anchor="w")
        self.entry_date = tk.Entry(right_frame, **style_entry)
        self.entry_date.pack(fill="x", ipady=5, pady=(2, 12))
        self.entry_date.insert(0, datetime.now().strftime("%d-%m-%Y"))
        
        # 2. Miktar
        tk.Label(right_frame, text="Miktar (Gram)", **style_label).pack(anchor="w")
        self.entry_amount = tk.Entry(right_frame, **style_entry)
        self.entry_amount.pack(fill="x", ipady=5, pady=(2, 12))
        
        # 3. Para Birimi
        tk.Label(right_frame, text="Para Birimi", **style_label).pack(anchor="w")
        self.var_currency = tk.StringVar(value="TL")
        frame_radio = tk.Frame(right_frame, bg="#333333")
        frame_radio.pack(fill="x", pady=(2, 12))
        
        r1 = tk.Radiobutton(frame_radio, text="TL", variable=self.var_currency, value="TL", bg="#333333", fg="white", selectcolor="#454545", activebackground="#333333", activeforeground="white", command=self.toggle_rate_entry)
        r1.pack(side="left", padx=(0, 10))
        
        r2 = tk.Radiobutton(frame_radio, text="USD", variable=self.var_currency, value="USD", bg="#333333", fg="white", selectcolor="#454545", activebackground="#333333", activeforeground="white", command=self.toggle_rate_entry)
        r2.pack(side="left")
        
        # 4. Kur (Sadece USD seçiliyse görünür)
        self.frame_rate = tk.Frame(right_frame, bg="#333333")
        self.frame_rate.pack(fill="x")
        
        tk.Label(self.frame_rate, text="İşlem Kuru (USD/TL)", **style_label).pack(anchor="w")
        self.entry_rate = tk.Entry(self.frame_rate, **style_entry)
        self.entry_rate.pack(fill="x", ipady=5, pady=(2, 12))
        # Varsayılan olarak güncel kuru yazalım ama kullanıcı değiştirebilsin
        self.entry_rate.insert(0, f"{self.current_dollar_rate:.4f}")
        
        # 5. Toplam Tutar
        tk.Label(right_frame, text="Toplam Tutar", **style_label).pack(anchor="w")
        self.entry_total = tk.Entry(right_frame, **style_entry)
        self.entry_total.pack(fill="x", ipady=5, pady=(2, 12))
        
        # Ekle Butonu
        btn_add = tk.Button(right_frame, text="EKLE", bg="#007acc", fg="white", font=("Segoe UI", 10, "bold"), relief="flat", cursor="hand2", command=self.save)
        btn_add.pack(fill="x", pady=20, ipady=8)
        
        self.toggle_rate_entry() # İlk durum ayarı
        self.load_list()

    def toggle_rate_entry(self):
        if self.var_currency.get() == "USD":
            self.frame_rate.pack(fill="x", before=self.entry_total) # Tekrar göster
        else:
            self.frame_rate.pack_forget()

    def load_list(self):
        for item in self.tree.get_children():
            self.tree.delete(item)
            
        for i, t in enumerate(self.manager.transactions):
            currency = t.get("currency", "TL")
            if currency == "USD":
                total_str = f"${t.get('total_usd', 0):.2f}"
                cost_str = f"${t.get('price_usd', 0):.2f}"
            else:
                total_str = f"₺{t.get('total_tl', 0):.2f}"
                cost_tl = t.get('total_tl', 0) / t.get('amount_g', 1)
                cost_str = f"₺{cost_tl:.2f}"
                
            self.tree.insert("", "end", iid=i, values=(t['date'], f"{t['amount_g']:.2f}", cost_str, total_str, "🗑️"))

    def on_click(self, event):
        region = self.tree.identify("region", event.x, event.y)
        if region == "cell":
            column = self.tree.identify_column(event.x)
            if column == "#5": # Delete column
                item_id = self.tree.identify_row(event.y)
                if item_id:
                    self.delete_transaction(item_id)

    def delete_transaction(self, item_id):
        idx = int(item_id)
        t = self.manager.transactions[idx]
        
        msg = f"{t['date']} tarihindeki {t['amount_g']:.2f}g miktarında yaptığınız alım silinecektir.\nOnaylıyor musunuz?"
        if tk.messagebox.askyesno("Onay", msg, parent=self):
            del self.manager.transactions[idx]
            self.manager.save_all()
            self.load_list()
            self.on_save_callback()

    def save(self):
        try:
            amount = float(self.entry_amount.get().replace(',', '.'))
            total_entered = float(self.entry_total.get().replace(',', '.'))
            currency = self.var_currency.get()
            
            data = {
                "date": self.entry_date.get(),
                "amount_g": amount,
                "currency": currency
            }
            
            if currency == "USD":
                data["total_usd"] = total_entered
                data["price_usd"] = total_entered / amount if amount else 0
                
                # Kullanıcının girdiği kur
                try:
                    user_rate = float(self.entry_rate.get().replace(',', '.'))
                except:
                    user_rate = self.current_dollar_rate # Fallback
                
                # Banka alım maliyeti (TL) = Toplam USD * Alım Kuru
                data["total_tl"] = total_entered * user_rate
            else:
                data["total_tl"] = total_entered
                data["total_usd"] = 0
                data["price_usd"] = 0
            
            self.manager.save(data)
            self.load_list()
            self.on_save_callback()
            
            # Formu temizle
            self.entry_amount.delete(0, 'end')
            self.entry_total.delete(0, 'end')
            
        except ValueError:
            tk.messagebox.showerror("Hata", "Lütfen geçerli sayısal değerler giriniz.", parent=self)

class PiyasaWidget:
    def __init__(self):
        self.root = tk.Tk()
        self.root.title("Market Widget")
        
        # --- AYARLAR ---
        self.bg_color = "#1e1e1e"  # Koyu Gri Arka Plan
        self.text_color = "#00ff41" # Matrix Yeşili
        self.alpha = 0.85          # Saydamlık (0.1 - 1.0)
        self.refresh_rate = 60     # Saniye cinsinden yenileme
        
        # Pencere Ayarları
        self.root.overrideredirect(True) # Çerçevesiz
        self.root.attributes("-alpha", self.alpha)
        # self.root.attributes("-topmost", True) # Her zaman üstte - Widget modunda her zaman üstte olması istenmeyebilir, ama widget mantığı genelde masaüstünde durur. Kullanıcı "arkaplanda" dedi.
        # Kullanıcı "üstte" demedi, "arkaplanda" dedi. Genellikle widgetlar masaüstünde durur (altta).
        # Ancak "Topmost" açık olursa diğer pencerelerin üstünde durur. Kullanıcı bunu istemiyor olabilir.
        # "Programın altta uygulama olarak gözükerek değil" -> Taskbar'da görünmesin.
        
        self.root.attributes("-topmost", True) # Widget olduğu için görünür olmalı, genelde üstte tutulur ama opsiyonel. Varsayılan üstte kalsın.
        
        self.root.configure(bg=self.bg_color)
        
        # Taskbar'dan gizleme (Windows Widget Modu)
        self.make_toolwindow()
        
        # Başlangıç Konumu (Sağ Üst)
        screen_width = self.root.winfo_screenwidth()
        self.root.geometry(f"220x200+{screen_width-250}+50")
        
        # Managers
        self.tm = TransactionManager()
        self.asm = AutoStartManager()
        
        # UI Elemanları
        self.setup_ui()
        
        # Sürükleme Özelliği
        self.root.bind("<Button-1>", self.start_move)
        self.root.bind("<B1-Motion>", self.do_move)
        
        # Sağ Tık Menüsü
        self.menu = tk.Menu(self.root, tearoff=0)
        self.menu.add_command(label="Kapat", command=self.kapat)
        self.root.bind("<Button-3>", self.show_menu)
        
        # İlk Veri Çekme
        self.update_thread = threading.Thread(target=self.veri_dongusu, daemon=True)
        self.update_thread.start()
        
    def setup_ui(self):
        # Stil
        style_font = ("Consolas", 11, "bold")
        
        # Konteyner
        self.frame = tk.Frame(self.root, bg=self.bg_color, padx=10, pady=10)
        self.frame.pack(expand=True, fill="both")
        
        # Etiketler (Değişkenler)
        self.var_gumus_ons = tk.StringVar(value="Gümüş Ons: ...")
        self.var_gumus_tl = tk.StringVar(value="Gümüş TL : ...")
        self.var_altin_tl = tk.StringVar(value="Altın TL : ...")
        
        tk.Label(self.frame, textvariable=self.var_gumus_ons, bg=self.bg_color, fg=self.text_color, font=style_font, anchor="w").pack(fill="x")
        tk.Label(self.frame, textvariable=self.var_gumus_tl, bg=self.bg_color, fg=self.text_color, font=style_font, anchor="w").pack(fill="x")
        tk.Label(self.frame, textvariable=self.var_altin_tl, bg=self.bg_color, fg="#ffd700", font=style_font, anchor="w").pack(fill="x") # Altın sarısı
        
        ttk.Separator(self.frame, orient='horizontal').pack(fill='x', pady=5)
        
        # Portföy
        self.var_portfolio = tk.StringVar(value="Varlık: ...")
        self.var_profit = tk.StringVar(value="Kar: ...")
        
        tk.Label(self.frame, textvariable=self.var_portfolio, bg=self.bg_color, fg="#ffffff", font=("Consolas", 10), anchor="w").pack(fill="x")
        tk.Label(self.frame, textvariable=self.var_profit, bg=self.bg_color, fg="#ffffff", font=("Consolas", 10), anchor="w").pack(fill="x")
        
        # Butonlar Konteyner
        btn_frame = tk.Frame(self.frame, bg=self.bg_color)
        btn_frame.pack(anchor="e", pady=(0, 0))
        
        # Ayarlar Butonu
        btn_settings = tk.Label(btn_frame, text="⚙️", bg=self.bg_color, fg="#555555", font=("Segoe UI", 12), cursor="hand2")
        btn_settings.pack(side="left", padx=(0, 10))
        btn_settings.bind("<Button-1>", self.open_settings)
        
        # Ekle Butonu
        btn_add = tk.Label(btn_frame, text="+", bg=self.bg_color, fg="#555555", font=("Arial", 12, "bold"), cursor="hand2")
        btn_add.pack(side="left")
        btn_add.bind("<Button-1>", lambda e: self.open_add_transaction())
        
        # Son güncelleme saati (küçük)
        self.var_time = tk.StringVar(value="Başlatılıyor...")
        tk.Label(self.frame, textvariable=self.var_time, bg=self.bg_color, fg="#888888", font=("Arial", 7), anchor="e").pack(fill="x", pady=(5,0))

        # --- Ayarlar Menüsü (Dropdown) ---
        self.settings_menu = tk.Menu(self.root, tearoff=0, bg="#2d2d2d", fg="white", activebackground="#007acc", activeforeground="white", font=("Segoe UI", 9))
        self.var_autostart = tk.BooleanVar(value=self.asm.is_enabled())
        self.settings_menu.add_checkbutton(label="Başlangıçta Çalıştır", variable=self.var_autostart, command=self.toggle_autostart)
        # Menüye başka seçenekler de eklenebilir (örn: Kapat)
        self.settings_menu.add_separator()
        self.settings_menu.add_command(label="Çıkış", command=self.kapat)

    def toggle_autostart(self):
        self.asm.set_autostart(self.var_autostart.get())

    def veri_getir(self):
        try:
            # XAGUSD=X hata verdiği için SI=F (Vadeli) geri dönüyoruz.
            tickers = yf.Tickers("SI=F GC=F TRY=X")
            
            # Veri çekme
            def get_price(symbol):
                try:
                    info = tickers.tickers[symbol].info
                    val = info.get("regularMarketPrice") or info.get("previousClose") or info.get("bid") or 0
                    return val
                except:
                    return 0

            ons_gumus = get_price("SI=F")
            ons_altin = get_price("GC=F")
            dolar = get_price("TRY=X")
            self.last_dolar_rate = dolar
            
            # Hesaplamalar
            gram_gumus_tl = (ons_gumus * dolar) / 31.1035
            gram_altin_tl = (ons_altin * dolar) / 31.1035
            
            # UI Güncelleme (Main Thread'e güvenli geçiş için)
            self.root.after(0, lambda: self.guncelle_arayuz(ons_gumus, gram_gumus_tl, gram_altin_tl))
            
        except Exception as e:
            self.root.after(0, lambda: self.var_time.set("Bağlantı Hatası"))

    def guncelle_arayuz(self, ons_g, gram_g, gram_a):
        self.var_gumus_ons.set(f"Gümüş Ons: ${ons_g:.2f}")
        self.var_gumus_tl.set(f"Gümüş TL : ₺{gram_g:.2f}")
        self.var_altin_tl.set(f"Altın TL : ₺{gram_a:.0f}")
        current_time = time.strftime("%H:%M:%S")
        self.var_time.set(f"Son Güncelleme: {current_time}")

        # Portföy Hesapla
        total_inv, total_g = self.tm.get_summary()
        if total_g > 0:
            current_val = total_g * gram_g
            profit_tl = current_val - total_inv
            profit_pct = (profit_tl / total_inv) * 100 if total_inv > 0 else 0
            
            self.var_portfolio.set(f"Varlık: ₺{current_val:,.0f} ({total_g:,.2f} Gr)")
            
            color = "#00ff41" if profit_tl >= 0 else "#ff4444"
            self.var_profit.set(f"Kar: %{profit_pct:.1f} (₺{profit_tl:,.0f})")
            
            # Label rengini güncellemek zor olduğu için var_profit'i tutan label'ı bulup güncelleyebiliriz
            # Ancak basitlik adına şimdilik statik beyaz bırakabiliriz veya frame içindeki label'ları tarayabiliriz.
            # Şimdilik statik.
        else:
             self.var_portfolio.set("Varlık: ₺0")
             self.var_profit.set("Kar: %0.0")

    def open_add_transaction(self):
        # Güncel dolar kurunu bul
        current_dollar = 0
        try:
             # var_gumus_tl'den veya hesaplamadan bulabilirdik ama temiz olsun diye yeniden çekebiliriz
             # veya veri_getir içindeki 'dolar' değişkenini class attribute yapalım.
             # Hızlı çözüm: self.last_dolar_rate ekleyelim.
             pass
        except:
             pass
             
        PortfolioManagerDialog(self.root, self.tm, self.veri_getir, getattr(self, 'last_dolar_rate', 36.0))

    def open_settings(self, event=None):
        # Menüyü butonun olduğu yerde aç
        try:
            x = self.root.winfo_pointerx() # Mouse konumu
            y = self.root.winfo_pointery()
            self.settings_menu.post(x, y)
        except:
            pass
        
    def make_toolwindow(self):
        # Windows API kullanarak pencereyi Taskbar'dan ve Alt-Tab'dan gizleme
        # GWL_EXSTYLE = -20
        # WS_EX_TOOLWINDOW = 0x00000080
        # WS_EX_APPWINDOW = 0x00040000
        
        hwnd = ctypes.windll.user32.GetParent(self.root.winfo_id())
        style = ctypes.windll.user32.GetWindowLongW(hwnd, -20)
        style = style | 0x00000080 # WS_EX_TOOLWINDOW ekle
        style = style & ~0x00040000 # WS_EX_APPWINDOW çıkar (bazı durumlarda default olabilir)
        ctypes.windll.user32.SetWindowLongW(hwnd, -20, style)
        
        # Değişikliğin hemen uygulanması için
        self.root.withdraw()
        self.root.after(10, self.root.deiconify)

    def veri_dongusu(self):
        while True:
            self.veri_getir()
            time.sleep(self.refresh_rate)

    # --- Sürükleme Mantığı ---
    def start_move(self, event):
        self.x = event.x
        self.y = event.y

    def do_move(self, event):
        deltax = event.x - self.x
        deltay = event.y - self.y
        x = self.root.winfo_x() + deltax
        y = self.root.winfo_y() + deltay
        self.root.geometry(f"+{x}+{y}")

    def show_menu(self, event):
        self.menu.post(event.x_root, event.y_root)

    def kapat(self):
        self.root.destroy()
        sys.exit()

    def run(self):
        self.root.mainloop()

if __name__ == "__main__":
    app = PiyasaWidget()
    app.run()