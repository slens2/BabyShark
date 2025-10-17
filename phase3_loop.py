#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import time
import traceback
import phase3_runner

if __name__ == "__main__":
    interval_sec = 60  # dùng 300 khi strict
    while True:
        try:
            phase3_runner.main()
        except Exception:
            traceback.print_exc()
        time.sleep(interval_sec)
