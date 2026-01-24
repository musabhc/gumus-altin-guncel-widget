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
import requests
import webbrowser
import subprocess
from datetime import datetime
from tkinter import messagebox, filedialog

# Configuration
GITHUB_REPO = "musabhc/gumus-altin-guncel-widget"

try:
    import _version
    VERSION = _version.__version__
except ImportError:
    VERSION = "0.0.0-dev"


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

class UpdateManager:
    def __init__(self, current_version, repo_name):
        self.current_version = current_version
        self.repo_name = repo_name
        self.api_url = f"https://api.github.com/repos/{repo_name}/releases/latest"
        
    def check_for_updates(self):
        try:
            response = requests.get(self.api_url)
            if response.status_code == 200:
                data = response.json()
                latest_tag = data.get("tag_name", "").replace("v", "")
                download_url = ""
                
                # Varlıklar içinde .exe ara (Setup öncelikli)
                for asset in data.get("assets", []):
                    if asset["name"].endswith("Setup.exe"):
                        download_url = asset["browser_download_url"]
                        break
                    elif asset["name"].endswith(".exe"):
                        download_url = asset["browser_download_url"]
                
                if latest_tag > self.current_version and download_url:
                    return True, latest_tag, download_url
            return False, None, None
        except Exception as e:
            print(f"Update Check Error: {e}")
            return False, None, None

    def update_application(self, download_url):
        try:
            # İndirme işlemi
            temp_path = os.path.join(os.environ["TEMP"], "PiyasaWidget_Update.exe")
            response = requests.get(download_url, stream=True)
            with open(temp_path, 'wb') as f:
                for chunk in response.iter_content(chunk_size=8192):
                    f.write(chunk)
            
            # Installer'ı çalıştır ve uygulamayı kapat
            subprocess.Popen([temp_path, "/SILENT"]) # Silent kurulum deneyebiliriz veya normal
            return True
        except Exception as e:
            messagebox.showerror("Hata", f"Güncelleme hatası: {e}")
            return False

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
        try:
            self.root.iconbitmap("icon.ico")
        except:
             pass
        
        # Taskbar'dan gizleme (Windows Widget Modu)
        self.make_toolwindow()
        
        # Başlangıç Konumu (Sağ Üst)
        screen_width = self.root.winfo_screenwidth()
        self.root.geometry(f"260x300+{screen_width-290}+50")
        
        # Managers
        self.tm = TransactionManager()
        self.asm = AutoStartManager()
        self.um = UpdateManager(VERSION, GITHUB_REPO)
        
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
        # --- Premium V2 Stil Tanımları ---
        self.font_header = ("Segoe UI", 9)
        self.font_label = ("Segoe UI Semibold", 9) 
        self.font_value = ("Segoe UI", 11)
        self.font_portfolio = ("Segoe UI", 20, "bold") # Büyük Varlık (Reduced to 20)
        self.font_profit = ("Segoe UI", 10)
        
        # Renk Paleti (Ultra Dark)
        self.bg_color = "#0f0f0f" # Deep Black override
        self.root.configure(bg=self.bg_color)
        
        self.color_card = "#141414"
        self.color_text_main = "#ffffff"
        self.color_text_dim = "#666666"
        self.color_accent = "#2196f3"
        self.color_success = "#2ecc71" # Emerald
        self.color_danger = "#e74c3c" # Alizarin
        self.color_gold = "#d4af37" # Rich Gold
        
        # Ana Konteyner
        self.frame = tk.Frame(self.root, bg=self.bg_color, padx=15, pady=15)
        self.frame.pack(fill="both", expand=True)
        
        # 1. ÜST HEADER (Durum Noktası + Saat)
        header_frame = tk.Frame(self.frame, bg=self.bg_color)
        header_frame.pack(fill="x", pady=(0, 15))
        
        self.var_market_status = tk.StringVar(value="•") 
        self.lbl_market_status = tk.Label(header_frame, textvariable=self.var_market_status, bg=self.bg_color, fg=self.color_text_dim, font=("Arial", 14), anchor="w") # Nokta için Arial
        self.lbl_market_status.pack(side="left")
        
        self.var_time = tk.StringVar(value="--:--")
        tk.Label(header_frame, textvariable=self.var_time, bg=self.bg_color, fg=self.color_text_dim, font=self.font_header, anchor="e").pack(side="right", pady=4) # Hizalama düzeltmesi

        # 2. PORTFÖY (Merkezi, Büyük)
        portfolio_frame = tk.Frame(self.frame, bg=self.bg_color)
        portfolio_frame.pack(fill="x", pady=(0, 20))
        
        tk.Label(portfolio_frame, text="TOPLAM VARLIK", bg=self.bg_color, fg=self.color_text_dim, font=("Segoe UI", 8), anchor="w").pack(fill="x")
        
        self.var_portfolio = tk.StringVar(value="₺...")
        tk.Label(portfolio_frame, textvariable=self.var_portfolio, bg=self.bg_color, fg=self.color_text_main, font=self.font_portfolio, anchor="w").pack(fill="x")
        
        self.var_profit = tk.StringVar(value="...")
        self.lbl_profit = tk.Label(portfolio_frame, textvariable=self.var_profit, bg=self.bg_color, fg=self.color_text_dim, font=self.font_profit, anchor="w")
        self.lbl_profit.pack(fill="x")

        # 3. PİYASA LİSTESİ
        self.create_price_row("Gümüş ONS", "$...", "var_gumus_ons", self.color_text_main)
        self.create_price_row("Gümüş TL", "₺...", "var_gumus_tl", self.color_text_main)
        self.create_price_row("Altın TL", "₺...", "var_altin_tl", self.color_gold)

        # 4. FOOTER (Gizli Butonlar)
        footer_frame = tk.Frame(self.frame, bg=self.bg_color)
        footer_frame.pack(side="bottom", fill="x", pady=(10, 0))
        
        def create_icon_btn(parent, text, command):
            lbl = tk.Label(parent, text=text, bg=self.bg_color, fg="#333333", font=("Segoe UI Emoji", 10), cursor="hand2")
            lbl.pack(side="right", padx=(10, 0))
            lbl.bind("<Button-1>", lambda e: command())
            lbl.bind("<Enter>", lambda e: lbl.config(fg="#888888"))
            lbl.bind("<Leave>", lambda e: lbl.config(fg="#333333"))
            return lbl

        create_icon_btn(footer_frame, "⚙️", self.open_settings)
        create_icon_btn(footer_frame, "📥", self.import_transactions)
        create_icon_btn(footer_frame, "➕", self.open_add_transaction)
        create_icon_btn(footer_frame, "🔄", self.veri_getir)

    def create_price_row(self, label_text, initial_value, var_name, color):
        row = tk.Frame(self.frame, bg=self.bg_color)
        row.pack(fill="x", pady=4)
        
        tk.Label(row, text=label_text, bg=self.bg_color, fg=self.color_text_dim, font=self.font_label, anchor="w").pack(side="left")
        
        var = tk.StringVar(value=initial_value)
        setattr(self, var_name, var)
        tk.Label(row, textvariable=var, bg=self.bg_color, fg=color, font=self.font_value, anchor="e").pack(side="right")

    def toggle_autostart(self):
        self.asm.set_autostart(self.var_autostart.get())
        
    def check_updates(self):
        has_update, new_version, url = self.um.check_for_updates()
        if has_update:
            if messagebox.askyesno("Güncelleme Mevcut", f"Yeni sürüm bulundu: v{new_version}\nŞimdi indirilip kurulsun mu?"):
                self.var_time.set("Güncelleme indiriliyor...")
                self.root.update()
                if self.um.update_application(url):
                    self.root.destroy()
                    sys.exit()
        else:
            messagebox.showinfo("Güncelleme", "Uygulama güncel!")

    def is_market_closed(self):
        """
        Piyasa Kapalı mı kontrolü (Türkiye saati varsayımıyla):
        Kapanış: Cumartesi 01:00
        Açılış: Pazartesi 02:00
        """
        now = datetime.now()
        weekday = now.weekday() # 0: Pzt, 6: Paz
        hour = now.hour
        
        # Cumartesi (5)
        if weekday == 5:
            return hour >= 1
        # Pazar (6)
        if weekday == 6:
            return True
        # Pazartesi (0)
        if weekday == 0:
            return hour < 2
            
        return False

    def save_last_data(self, data):
        try:
            filepath = os.path.join(os.path.dirname(os.path.abspath(__file__)), "market_data.json")
            with open(filepath, 'w', encoding='utf-8') as f:
                json.dump(data, f, indent=4)
        except Exception as e:
            print(f"Veri kaydetme hatası: {e}")

    def load_last_data(self):
        try:
            filepath = os.path.join(os.path.dirname(os.path.abspath(__file__)), "market_data.json")
            if os.path.exists(filepath):
                with open(filepath, 'r', encoding='utf-8') as f:
                    return json.load(f)
        except Exception as e:
            print(f"Veri okuma hatası: {e}")
        return None

    def veri_getir(self):
        try:
            # Piyasa kontrolü
            if self.is_market_closed():
                def set_closed_ui():
                    self.var_market_status.set("•")
                    self.lbl_market_status.config(fg=self.color_danger) # Kırmızı nokta
                    
                    # Kayıtlı son veriyi yükle
                    last_data = self.load_last_data()
                    if last_data:
                        self.guncelle_arayuz(
                            last_data.get("ons_gumus", 0),
                            last_data.get("gram_gumus_tl", 0),
                            last_data.get("gram_altin_tl", 0)
                        )
                        # Dolar kurunu da güncelle, portföy hesaplamaları için gerekebilir
                        self.last_dolar_rate = last_data.get("dolar", 36.0)
                        
                        last_time = last_data.get("timestamp", "")
                        self.var_time.set(f"Piyasa Kapalı (Son: {last_time})")
                    else:
                        self.var_time.set(f"Uyku ({time.strftime('%H:%M')})")
                
                self.root.after(0, set_closed_ui)
                return # API isteği atma

            def set_open_ui():
                self.var_market_status.set("•")
                self.lbl_market_status.config(fg=self.color_success) # Yeşil nokta
            
            self.root.after(0, set_open_ui)

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
            
            # Verileri kaydet
            market_data = {
                "ons_gumus": ons_gumus,
                "gram_gumus_tl": gram_gumus_tl,
                "gram_altin_tl": gram_altin_tl,
                "dolar": dolar,
                "timestamp": time.strftime("%d.%m %H:%M")
            }
            self.save_last_data(market_data)
            
            # UI Güncelleme (Main Thread'e güvenli geçiş için)
            self.root.after(0, lambda: self.guncelle_arayuz(ons_gumus, gram_gumus_tl, gram_altin_tl))
            
        except Exception as e:
            self.root.after(0, lambda: self.var_time.set("Bağlantı Hatası"))

    def guncelle_arayuz(self, ons_g, gram_g, gram_a):
        self.var_gumus_ons.set(f"${ons_g:.2f}")
        self.var_gumus_tl.set(f"₺{gram_g:.2f}")
        self.var_altin_tl.set(f"₺{gram_a:.0f}")
        current_time = time.strftime("%H:%M:%S")
        self.var_time.set(f"Son Güncelleme: {current_time}")

        # Portföy Hesapla
        total_inv, total_g = self.tm.get_summary()
        if total_g > 0:
            current_val = total_g * gram_g
            profit_tl = current_val - total_inv
            profit_pct = (profit_tl / total_inv) * 100 if total_inv > 0 else 0
            
            self.var_portfolio.set(f"₺{current_val:,.0f}")
            
            sign = "+" if profit_tl >= 0 else ""
            self.var_profit.set(f"{sign}%{profit_pct:.1f} ({sign}₺{profit_tl:,.0f})")
            
            color = self.color_success if profit_tl >= 0 else self.color_danger
            self.lbl_profit.config(fg=color)
        else:
             self.var_portfolio.set("₺0")
             self.var_profit.set("%0.0 (₺0)")
             self.lbl_profit.config(fg=self.color_text_dim)

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

    def import_transactions(self):
        filename = filedialog.askopenfilename(title="İçe Aktarılacak Dosyayı Seç", filetypes=[("JSON Files", "*.json")])
        if filename:
            try:
                with open(filename, 'r', encoding='utf-8') as f:
                    new_data = json.load(f)
                
                if isinstance(new_data, list):
                    # Basit doğrulama: İlk öğe beklenen anahtarlara sahip mi?
                    if new_data and ("amount_g" in new_data[0] or "total_tl" in new_data[0]):
                        count = 0
                        for item in new_data:
                             self.tm.save(item) # Tek tek eklersek sürekli save çağırır, transaction manager'a bulk add eklemek daha iyi ama bu da çalışır.
                             # Daha iyisi: self.tm.transactions.extend(new_data); self.tm.save_all()
                             count += 1
                        
                        # Hepsini tek seferde kaydetmek daha performanslı olurdu ama tm.save tek tek ekleyip save ediyor.
                        # Şimdilik sorun değil.
                        
                        messagebox.showinfo("Başarılı", f"{len(new_data)} adet işlem başarıyla içeri aktarıldı.")
                    else:
                        messagebox.showwarning("Uyarı", "Dosya formatı uyumsuz görünüyor veya boş.")
                else:
                    messagebox.showerror("Hata", "JSON formatı geçersiz (Liste olmalı).")
            except Exception as e:
                messagebox.showerror("Hata", f"İçe aktarma hatası: {e}")
            finally:
                # Arayüzü güncelle
                self.veri_getir()

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