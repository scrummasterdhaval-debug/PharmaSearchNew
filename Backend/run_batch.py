#!/usr/bin/env python
"""Launch manufacturer batch fill job from Backend root."""
import sys
import os

# Add current directory to path so services module is found
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from services.batch_manufacturer_fill import run_batch_fill

if __name__ == '__main__':
    # Run with limit if provided, otherwise unlimited
    limit = None
    test_mode = False

    if len(sys.argv) > 1:
        try:
            limit = int(sys.argv[1])
        except ValueError:
            print(f"Usage: python run_batch.py [limit]")
            sys.exit(1)

    print(f"Starting manufacturer batch fill {'(limit: ' + str(limit) + ')' if limit else '(unlimited)'}")
    stats = run_batch_fill(limit=limit, test_mode=test_mode)
    print(f"\nBatch complete: {stats}")
