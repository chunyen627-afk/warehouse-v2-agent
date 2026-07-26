#!/bin/bash
cd ~/warehouse_v2_en
# ⚠️ 跑守衛前先 reset：劇情批/探針會真的寫入資料（出貨、調撥），殘留會讓守衛的
#   寫入句因「庫存不足」誤報 FAIL。r5 踩過——那輪 890/892，多出來的 FAIL
#   （move 20 clothes iron）是前面 r5 出掉 5 個 Steam Iron 造成的資料污染，
#   不是回歸；reset 後單獨復驗即通過。實際仍是 891/892。
curl -sk -X POST https://localhost:8002/api/reset_demo \
     -H 'Content-Type: application/json' \
     -d '{"password":"0000"}' -o /dev/null 2>/dev/null
python3 regression_ws.py --rpi5 --file regression_corpus_en.txt > _guard_en.log 2>&1
echo "EXIT=$?" >> _guard_en.log
