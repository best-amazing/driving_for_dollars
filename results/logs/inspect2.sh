#!/bin/bash
FILE="/mnt/c/Users/USERR/Work/AB-group/Driving-for-dollars/results/logs/tps_search_819_nebraska_ave_toledo_oh.html"

echo "=== 1. The p1144gfruekc4fkry39odjfgu href in context ==="
grep -B10 -A10 "p1144gfruekc4fkry39odjfgu" "$FILE" | head -30

echo ""
echo "=== 2. Lives in + content-value (city matches) ==="
grep -A2 'content-label">Lives in' "$FILE" | head -40

echo ""
echo "=== 3. All card names (content-header text) ==="
# Extract text between content-header tags
grep -A1 'class="content-header"' "$FILE" | grep -v 'content-header' | grep -v '\-\-' | sed 's/^[[:space:]]*//' | head -15

echo ""
echo "=== 4. Bot/block lines ==="
grep -in "captcha\|challenge\|robot\|blocked\|access denied" "$FILE" | head -5

echo ""
echo "=== 5. Is p1144gfruekc4fkry39odjfgu in a visible card or hidden/JS? ==="
grep -c "p1144gfruekc4fkry39odjfgu" "$FILE"
