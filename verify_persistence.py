
import unittest
import json
import os
import time
from unittest.mock import MagicMock, patch
import sys

# Add the directory containing main.py to path
sys.path.append(r"c:\Users\Musa\Desktop\finans")

import main

class TestMarketPersistence(unittest.TestCase):
    def setUp(self):
        # Prevent Tkinter execution
        self.patcher = patch('tkinter.Tk')
        self.mock_tk = self.patcher.start()
        
        self.widget = main.PiyasaWidget()
        # Mock UI elements that might be accessed
        self.widget.var_market_status = MagicMock()
        self.widget.lbl_market_status = MagicMock()
        self.widget.var_time = MagicMock()
        self.widget.guncelle_arayuz = MagicMock()
        self.widget.root = MagicMock()
        
        self.test_file = os.path.join(os.path.dirname(os.path.abspath(main.__file__)), "market_data.json")

    def tearDown(self):
        self.patcher.stop()
        # Clean up test file if it exists (optional, or keep to inspect)
        # if os.path.exists(self.test_file):
        #     os.remove(self.test_file)

    def test_save_and_load(self):
        data = {
            "ons_gumus": 30.5,
            "gram_gumus_tl": 35.0,
            "gram_altin_tl": 2500.0,
            "dolar": 36.5,
            "timestamp": "Test Time"
        }
        
        self.widget.save_last_data(data)
        
        self.assertTrue(os.path.exists(self.test_file), "market_data.json should be created")
        
        loaded = self.widget.load_last_data()
        self.assertEqual(loaded, data)

    def test_market_closed_behavior(self):
        # 1. Create a dummy saved file
        saved_data = {
            "ons_gumus": 100,
            "gram_gumus_tl": 200,
            "gram_altin_tl": 3000,
            "dolar": 40.0,
            "timestamp": "Closed Time"
        }
        self.widget.save_last_data(saved_data)
        
        # 2. Mock is_market_closed to return True
        with patch.object(self.widget, 'is_market_closed', return_value=True):
            # 3. Call veri_getir
            self.widget.veri_getir()
            
            # 4. Verify that root.after was called with a function that sets the UI
            # veri_getir calls root.after(0, set_closed_ui)
            # We need to execute the callback passed to root.after
            
            args, _ = self.widget.root.after.call_args
            callback = args[1]
            callback() # Execute set_closed_ui
            
            # 5. Verify UI update calls
            self.widget.guncelle_arayuz.assert_called_with(100, 200, 3000)
            self.widget.var_time.set.assert_called_with("Piyasa Kapalı (Son: Closed Time)")

if __name__ == '__main__':
    unittest.main()
