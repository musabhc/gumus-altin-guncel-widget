
import sys
import os
import json
import time
from unittest.mock import MagicMock, patch

print("Starting verification script...")

# Add path
sys.path.append(r"c:\Users\Musa\Desktop\finans")

try:
    print("Importing main...")
    import main
    print("Main imported.")
except Exception as e:
    print(f"Failed to import main: {e}")
    sys.exit(1)

def run_test():
    print("Setting up test...")
    # Pattern to mock Tkinter and Threading
    with patch('tkinter.Tk') as mock_tk, \
         patch('tkinter.StringVar') as mock_stringvar, \
         patch('threading.Thread') as mock_thread, \
         patch('ctypes.windll') as mock_ctypes: # Mock ctypes
        
        # Configure mock Tk instance
        mock_root = mock_tk.return_value
        mock_root.winfo_screenwidth.return_value = 1920 # Return int
        
        print("Initializing widget...")
        with patch.object(main.PiyasaWidget, 'make_toolwindow'): # Mock make_toolwindow to avoid ctypes issues entirely
            widget = main.PiyasaWidget()
        
        # Mock UI
        widget.var_market_status = MagicMock()
        widget.lbl_market_status = MagicMock()
        widget.var_time = MagicMock()
        widget.guncelle_arayuz = MagicMock()
        widget.root = MagicMock()
        
        test_file = os.path.join(os.path.dirname(os.path.abspath(main.__file__)), "market_data.json")
        
        # TEST 1: Save Last Data
        print("Test 1: Save Last Data")
        data = {
            "ons_gumus": 30.5,
            "gram_gumus_tl": 35.0,
            "gram_altin_tl": 2500.0,
            "dolar": 36.5,
            "timestamp": "Test Time"
        }
        widget.save_last_data(data)
        
        if os.path.exists(test_file):
            print("PASS: market_data.json created.")
        else:
            print("FAIL: market_data.json NOT created.")
            return

        # TEST 2: Load Last Data
        print("Test 2: Load Last Data")
        loaded = widget.load_last_data()
        if loaded == data:
            print("PASS: Data loaded correctly.")
        else:
            print(f"FAIL: Loaded data mismatch. Got {loaded}")
            return

        # TEST 3: Market Closed Behavior
        print("Test 3: Market Closed Behavior")
        saved_data = {
            "ons_gumus": 100,
            "gram_gumus_tl": 200,
            "gram_altin_tl": 3000,
            "dolar": 40.0,
            "timestamp": "Closed Time"
        }
        widget.save_last_data(saved_data)
        
        with patch.object(widget, 'is_market_closed', return_value=True):
            widget.veri_getir()
            
            # Check callback
            args, _ = widget.root.after.call_args
            if not args:
                print("FAIL: root.after not called")
                return
                
            callback = args[1] # .after(0, callback)
            callback() # Execute ui update
            
            # Verify calls
            try:
                widget.guncelle_arayuz.assert_called_with(100, 200, 3000)
                print("PASS: guncelle_arayuz called with correct values.")
            except AssertionError as e:
                print(f"FAIL: guncelle_arayuz call mismatch: {e}")
                
            try:
                widget.var_time.set.assert_called_with("Piyasa Kapalı (Son: Closed Time)")
                print("PASS: var_time updated correctly.")
            except AssertionError as e:
                print(f"FAIL: var_time set mismatch: {e}")

if __name__ == "__main__":
    run_test()
