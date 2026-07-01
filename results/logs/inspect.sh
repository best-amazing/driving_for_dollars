#!/bin/bash
FILE="/mnt/c/Users/USERR/Work/AB-group/Driving-for-dollars/results/logs/tps_search_819_nebraska_ave_toledo_oh.html"

echo "=== 1. All /find/person/ hrefs ==="
grep -oP 'href="/find/person/[^"]*"' "$FILE" | head -10

echo ""
echo "=== 2. data-detail-link attributes ==="
grep -oP 'data-detail-link="[^"]*"' "$FILE" | head -15

echo ""
echo "=== 3. Lives in fields ==="
grep -i "Lives in" "$FILE" | head -15

echo ""
echo "=== 4. Card count ==="
grep -c "card-summary" "$FILE"

echo ""
echo "=== 5. Bot/block detection ==="
grep -ci "captcha\|challenge\|robot\|blocked\|access denied" "$FILE"

echo ""
echo "=== 6. Content-header names (who's on the page) ==="
grep -oP 'class="content-header">\s*\K[^<]+' "$FILE" | head -15

echo ""
echo "=== 7. a.detail-link hrefs specifically ==="
grep -oP '<a[^>]*class="[^"]*detail-link[^"]*"[^>]*href="([^"]*)"' "$FILE" | head -10
