#!/bin/bash
FILE="/mnt/c/Users/USERR/Work/AB-group/Driving-for-dollars/results/logs/tps_search_819_nebraska_ave_toledo_oh.html"

echo "=== p1144 context (each occurrence) ==="
grep -n "p1144gfruekc4fkry39odjfgu" "$FILE" | while read -r line; do
  LINENUM=$(echo "$line" | cut -d: -f1)
  echo "--- Found at line $LINENUM ---"
  sed -n "$((LINENUM-5)),$((LINENUM+5))p" "$FILE"
  echo ""
done

echo "=== Card names + Lives in ==="
grep -A3 'class="content-header"' "$FILE" | head -60

echo "=== content-value after Lives in ==="
grep -A1 'Lives in' "$FILE" | grep 'content-value' | head -15
