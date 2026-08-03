# fqt — 用螢幕和鏡頭傳檔案

[English](readme.md)

一邊的螢幕播 QR code 動畫,另一邊拿相機對著拍,檔案就傳過去了。不用網路、
不用配對,兩台裝置之間唯一的通道是光。

實測:Logitech C270 webcam 可以跑到 **41.9 KB/s**,iPhone 當鏡頭大約
**195 KB/s**。完整數據在 [docs/results.md](docs/results.md)。

## 安裝

```bash
python3 -m venv .venv
.venv/bin/pip install -e .
```

依賴(numpy、opencv、zxing-cpp、zstandard)都有預編譯 wheel,不用裝編譯器。
macOS / Windows / Linux 都能跑。

## 發送

```bash
fqt send 要傳的檔案.pdf
```

會開一個視窗開始播 QR 動畫,放在螢幕亮度最高的那塊。

視窗開著可以直接按鍵調參數:

- `[` / `]`:fps 調慢 / 調快
- `g`:每幀擺幾顆 QR(1x1 → 2x1 → 2x2 → 3x2,越多越快,前提是相機看得清楚)
- `p`:單顆 QR 的容量(close 塞好塞滿,far 給距離遠或鏡頭差的場合)
- `q`:結束

視窗下方的狀態列顯示目前設定和理論上限。改 grid 或 profile 會重新開始傳
(接收端會自動跟上),改 fps 不會中斷。

## 接收

```bash
fqt recv --preview --out 收檔資料夾
```

`--preview` 開一個相機畫面小視窗方便對準,然後看終端機的狀態列:

- `sharp`:對焦程度,100 以上能用,300 以上很清楚,移動相機讓它越高越好
- `yield`:每張畫面平均解到幾顆碼,太低就是擺位、對焦或光線有問題
- 多顆鏡頭用 `--camera 1`、`--camera 2` 切換

收完自動驗 SHA-256 再存檔。中途重開發送端也沒關係,接收端會自己重新鎖定。

實測出來的訣竅:

- 相機一定要架穩,手持是吞吐量殺手
- **發送端 fps 不要跟相機一樣**,故意錯開反而快(相機 30fps 就開 40fps)。
  同頻會讓取樣相位鎖死,速度變成看運氣
- 定焦鏡頭(像 C270)只有一個清楚距離,大約 40cm,前後移動找 sharp 最高點
- 房間越亮,曝光越短,拍到「兩幀疊影」的廢片就越少

## 找出你設備的最佳參數

```bash
fqt sweep --camera 0 --kb 300 --configs "30:2x1:far,40:2x1:far,40:2x2:far"
```

sweep 把發送和接收放在同一個程序,一輪一輪實傳測完印成表格,相機架好放著
等就行。config 格式是 `fps:grid:profile`,profile 也可以直接填 bytes 數字。
手邊沒相機的話,`fqt bench --kb 512 --grid 2x2 --loss 0.2` 可以純軟體測
pipeline。

## 目前的限制,以及怎麼擴充

甜蜜點是**小而重要的檔案**:10 MB 以下幾秒到幾分鐘,50 MB 還行,再大就是乾等,
而且格式本身在 ~190 MB 就擋住了。可壓縮的內容(文字、log、JSON)實際快好幾倍
—— 傳輸前都會先過 zstd。

限制的來源:每顆 QR 都帶一個 24 bytes 的表頭,欄位寬度是固定的,寫得下的數字
就那麼大:

| 限制 | 原因 | 解法 |
|---|---|---|
| 單次傳輸 ~190 MB | 塊數 `k` 是 u16(65,535 × 2,927 B) | v2 表頭:`k` 加寬成 u32 |
| 絕對上限 4 GB | `totalLen` 是 u32 | v2 表頭:加寬成 u64 |
| 整個檔案要放在接收端 RAM | fountain 解碼是亂序拼裝 | 分段:切成多個獨立的 ≤190 MB 傳輸,每段完成就落盤 |
| 中斷就前功盡棄 | 接收端用 sender 的隨機 session id 認流 | 續傳:改用內容指紋認檔案 + 已解出的塊定期存磁碟 |

這些都不是架構上的死牆 —— 表頭有 version 欄位可以演進,而 fountain code 天生
適合續傳(任何沒看過的幀都有用,順序無所謂)。沒做只是因為實際場景是小檔:
以 ~195 KB/s 來說,連 4 GB 都要盯著閃爍的螢幕六小時。

## 原理

跟 [decimen-optical-transfer](https://github.com/bashalarmistalt/decimen-optical-transfer)
同一個概念:QR 動畫 + fountain code — 每一幀是資料塊的某種 XOR 組合,接收端
集滿任意一批不重複的幀就能還原,漏拍只是多等一下,不影響正確性。

這版的主要改動:

- **systematic 傳法**:前 k 幀直接送原始資料塊,收訊好幾乎零浪費,漏掉的
  再靠 LT repair 補
- **一幀多碼**:一個畫面最多擺 3x2 顆 QR
- **旋轉重掃**:rolling shutter 常固定殺掉畫面某個位置,重送時輪換資料塊的
  位置,不讓同一批資料永遠等不到
- 解碼用 zxing-cpp(C++),灰階直進、關掉用不到的選項,Python 只負責調度

設計細節在 [docs/design.md](docs/design.md),背後調研在
[docs/research-survey.md](docs/research-survey.md) 和
[docs/decimen-review.md](docs/decimen-review.md)。

## 測試

```bash
.venv/bin/python -m pytest tests/ -q
```

## License

MIT
