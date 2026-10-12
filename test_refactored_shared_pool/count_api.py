import re
from collections import defaultdict

log_file = "/home/muhammed.yaashwin/freshworks/ltes/logs/master_pipeline_20260704_143123.log"

# Store total calls per minute slot
minute_totals = defaultdict(int)

# Match lines like:
# 13:03:00 - 13:03:59 : 78 calls
pattern = re.compile(
    r'(\d{2}:\d{2}:\d{2}\s*-\s*\d{2}:\d{2}:\d{2})\s*:\s*(\d+)\s+calls'
)

with open(log_file, "r") as f:
    for line in f:
        match = pattern.search(line)
        if match:
            minute_slot = match.group(1)
            calls = int(match.group(2))
            minute_totals[minute_slot] += calls

print("=" * 60)
print("FINAL PER-MINUTE API CALL TOTALS")
print("=" * 60)

for minute in sorted(minute_totals):
    print(f"{minute} : {minute_totals[minute]} calls")

print("=" * 60)
print(f"Grand Total Calls: {sum(minute_totals.values())}")