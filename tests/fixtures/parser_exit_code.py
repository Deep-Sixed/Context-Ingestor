"""
Fake parser: reads the desired exit code from STELE_TEST_EXIT_CODE and exits with it.
Used to prove that arbitrary exit codes are faithfully captured by the runner.
"""
import os
import sys

code = int(os.environ.get("STELE_TEST_EXIT_CODE", "0"))
print(f"exiting with code {code}", flush=True)
sys.exit(code)
