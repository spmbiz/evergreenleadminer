#!/usr/bin/env python3
"""Combined deterministic regression gate for GWS autonomous certification."""
import gws_v5_selftest_core as legacy
import gws_v54_regression_test as hardening

if __name__ == '__main__':
    legacy.main()
    hardening.main()
    print('GWS_COMBINED_REGRESSION_OK')
