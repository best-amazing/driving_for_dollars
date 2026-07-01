#!/bin/bash
FILE="/mnt/c/Users/USERR/Work/AB-group/Driving-for-dollars/results/logs/tps_search_819_nebraska_ave_toledo_oh.html"

echo "=== Card containing Toledo, OH (the matched one) ==="
# Find the line with Toledo, OH, then go back to find the card div
TOLEDO_LINE=$(grep -n "Toledo, OH" "$FILE" | head -1 | cut -d: -f1)
echo "Toledo found at line: $TOLEDO_LINE"

# Show 40 lines before and 10 after to see the full card
if [ -n "$TOLEDO_LINE" ]; then
  START=$((TOLEDO_LINE - 40))
  END=$((TOLEDO_LINE + 10))
  echo "--- Card context (lines $START-$END) ---"
  sed -n "${START},${END}p" "$FILE"
fi

echo ""
echo "=== Key finding: the d-none card ==="
echo "The p1144 card has class 'd-none' (CSS display:none = HIDDEN card)"
echo "It's a duplicate/extended version of Leona Jones that TPS hides by default"
echo ""
echo "=== Visible cards with detail-link class (a.detail-link) ==="
# The actual clickable links in visible cards
grep -n "a.*detail-link.*href" "$FILE" | grep -v "d-none" | head -20
