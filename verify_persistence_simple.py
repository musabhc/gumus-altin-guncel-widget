import unittest

import verify_persistence


if __name__ == "__main__":
    suite = unittest.defaultTestLoader.loadTestsFromModule(verify_persistence)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    raise SystemExit(0 if result.wasSuccessful() else 1)
