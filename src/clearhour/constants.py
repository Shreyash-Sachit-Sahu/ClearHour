"""Dependency-free constants, safe to import from the zip Lambdas (no pandas there)."""

IST = "Asia/Kolkata"
TARGET_HOURS = list(range(8, 14))  # hour-beginning bins, 08:00 to 13:00 IST
ASSEMBLY_HOUR = TARGET_HOURS[0]
