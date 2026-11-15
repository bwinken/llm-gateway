# 網頁樣式與前端檔案：怎麼 build、怎麼部署

這份文件寫給第一次碰這件事的人。照著做就好，不需要懂前端。

## 一句話版本

- **只改了 Python 程式，或只是部署**：什麼都不用 build，照常 `git pull` 再重啟。
- **改了 `app/templates/` 裡的 HTML（尤其是 `class="..."`）**：commit 之前先執行一次

  ```bash
  bash scripts/build_css.sh
  ```

  再把 `app/static/css/tailwind.css` 跟模板一起 commit。

忘了也不會壞到正式環境：CI 會檢查，沒有重新 build 的 PR 會顯示失敗（見[常見問題](#常見問題)）。

---

## 1. 這些檔案是什麼

網頁（dashboard、admin）需要三種「前端檔案」才能正常顯示。現在它們全部放在 repo 的 `app/static/` 裡，由我們自己的網站提供，不再從網路上的 CDN 載入：

| 檔案 | 用途 | 怎麼來的 | 多久要更新 |
|---|---|---|---|
| `app/static/css/tailwind.css` | 整個網站的樣式（顏色、間距、排版） | `scripts/build_css.sh` 從模板產生 | **改了模板的 class 就要重新產生** |
| `app/static/vendor/chart.umd.min.js` | 畫圖表的程式（Chart.js） | `scripts/vendor_frontend.sh` 下載 | 幾乎不用，要升級版本時才動 |
| `app/static/fonts/*.woff2`、`fonts.css` | 兩種字型（Plus Jakarta Sans、JetBrains Mono） | `scripts/vendor_frontend.sh` 下載 | 幾乎不用 |

這三種檔案都**已經 commit 在 repo 裡**，所以伺服器上不需要裝 Node.js，也不需要做任何 build。

### 為什麼 `tailwind.css` 要「build」？

網頁的樣式是用 Tailwind 寫的：HTML 裡寫 `class="text-sm font-bold text-t1"`，Tailwind 根據這些名字產生對應的 CSS。

以前是讓**每個使用者的瀏覽器**在開頁面時下載 Tailwind（約 400 KB）、當場產生 CSS。現在改成**我們事先產生好**一個約 30 KB 的檔案，所以：

- 頁面載入比較快，也不會先閃一下沒有樣式的畫面
- 公司網路連不到外網時，頁面也正常
- 不會有第三方網站的程式碼，跑在顯示 API key 的頁面上

代價是：**模板裡新增了一個以前沒用過的 class，就要重新產生一次 CSS**，否則那個 class 沒有效果。

---

## 2. 第一次準備（只做一次）

### 你需要什麼

- 一台 **Linux 或 macOS** 電腦（伺服器或你自己的筆電都可以）
  - Windows：請用 **WSL**（Windows 裡的 Ubuntu）或 **Git Bash**。在 PowerShell / 命令提示字元裡不能直接執行。
- 這兩個指令。打開終端機（Terminal）輸入下面兩行，**都有顯示版本號**就可以了：

  ```bash
  bash --version
  ```

  ```bash
  curl --version
  ```

- **第一次執行時要能連到 github.com**：腳本會從 GitHub 下載 Tailwind 的程式（一個檔案，約 40 MB），之後就放在 repo 底下的 `.cache/` 重複使用，不用再下載。
  - 公司要透過 proxy 上網的話，先設定好再執行。curl 會讀這個環境變數（網址換成你們的 proxy）：

    ```bash
    export HTTPS_PROXY=http://proxy.your-company:8080
    ```

  - 那台機器完全不能上網的話：在**能上網的電腦**上 build，commit 之後再 push，伺服器只要 `git pull` 就好。

### 不需要的東西

- 不需要 Node.js / npm
- 不需要管理員權限（sudo）
- 不需要另外安裝 Tailwind

---

## 3. 改了模板之後：一步一步 build

以下假設你剛改完 `app/templates/` 裡的某個 `.html` 檔。

**第 1 步：進到 repo 的根目錄**（有 `pyproject.toml` 的那一層）。路徑換成你自己的，例如：

```bash
cd ~/opt/llm-gateway
```

**第 2 步：執行 build**

```bash
bash scripts/build_css.sh
```

第一次會看到它在下載：

```
Downloading Tailwind 3.4.17 (linux-x64) from GitHub...
Built app/static/css/tailwind.css (27517 bytes).
Commit it together with the template change.
```

之後每次大約一秒就完成，只會看到後面兩行。bytes 的數字會隨模板改變，不用在意。

**第 3 步：看哪些檔案變了**

```bash
git status
```

應該會看到你改的模板，加上 `app/static/css/tailwind.css`。

- 如果 `tailwind.css` **沒有出現**：代表你的修改沒有用到新的 class，CSS 本來就涵蓋了，這很正常。

**第 4 步：一起 commit、push**（訊息和分支名稱換成你的）

```bash
git add app/templates app/static/css/tailwind.css
```

```bash
git commit -m "說明你改了什麼"
```

```bash
git push
```

### 只想確認、不想改檔案

```bash
bash scripts/build_css.sh --check
```

- 顯示 `app/static/css/tailwind.css is up to date.`：沒問題
- 顯示 `... is out of date ...`：照第 2 步重新 build

CI 每次跑的就是這個檢查。

### 在瀏覽器確認

部署後直接重新整理頁面即可，不用清快取：每個檔案的網址後面都帶著內容的雜湊值（例如 `tailwind.css?v=3f9a1c2b7d10`），檔案一改網址就變，瀏覽器會自動拿新的。

---

## 4. 部署（伺服器上）

### 一般部署（systemd）

跟以前完全一樣，照你們平常的更新步驟做，例如 `deploy/README.md` 的 *Quick update*：`git pull`、`rsync` 到 `~/opt/llm-gateway`、`alembic upgrade head`、最後

```bash
systemctl --user restart llm-gateway
```

伺服器上**不用**執行 `build_css.sh`：build 好的檔案已經在 `git pull` 拉下來的內容裡，`rsync` 會把 `app/static/` 一起複製過去。

重點是：沒有多出來的步驟。

### 容器部署（deploy/migration）

和其他程式更新一樣，重新 build image 再啟動即可。`app/static/` 會跟著整個 `app/` 一起包進 image，不用改 Dockerfile。

### 第一次上線前：確認 nginx 有 `/static/`

這件事**只要確認一次**。在伺服器上執行：

```bash
sudo nginx -T 2>/dev/null | grep -A4 "location /static/"
```

- **有輸出**，而且裡面有一行 `alias .../app/static/;`：好了，什麼都不用做。
- **沒有輸出**：頁面照樣能正常顯示（gateway 自己會提供這些檔案），只是少了 nginx 的快取。想補上的話，從 `deploy/llm-gateway-example.nginx.conf` 複製 `location /static/ { ... }` 那一整段，貼到你們 nginx 設定檔（通常是 `deploy/llm-gateway.nginx.conf`）裡的 `location / {` 前面。`__APP_DIR__` 換成 repo 的完整路徑，例如 `/home/gateway/opt/llm-gateway`。然後依序執行下面兩行：

  ```bash
  sudo nginx -t
  ```

  ```bash
  sudo systemctl reload nginx
  ```

  第一行顯示 `syntax is ok` 和 `test is successful` 才執行第二行。

- 容器部署：nginx 讀的是**主機上**的 repo。請確認它和 build image 用的是同一份 checkout（照 `deploy/migration` 的流程就是）。不確定的話就不要加這段，交給 gateway 提供。

---

## 5. 常見問題

### CI 的「Stylesheet up to date」失敗了

你（或某個 PR）改了模板，但沒有重新 build CSS。在自己電腦上：

```bash
bash scripts/build_css.sh
```

```bash
git add app/static/css/tailwind.css
```

```bash
git commit -m "Rebuild tailwind.css"
```

```bash
git push
```

### 我加了一個 class，但頁面上沒有效果

1. 先確認你有執行 `bash scripts/build_css.sh`，並且部署了新的 `tailwind.css`。
2. 確認 class 名稱**完整地**寫在模板裡。Tailwind 只認得原封不動出現在檔案裡的名字：
   - 可以：`class="text-ar"`；JavaScript 裡 `el.className = ok ? 'text-ag' : 'text-ar'`
   - 不行：`'text-' + color`（拼起來的名字 Tailwind 看不到，那個樣式就不會產生）
3. 顏色名稱（`t1`、`ab`、`ag` 等）定義在 `frontend/tailwind.config.js`。要新增顏色就加在那裡，再 build 一次。

### 出現 `Checksum mismatch`

下載回來的檔案和預期的不一樣，腳本為了安全不會使用它。通常是網路或 proxy 把下載內容換掉了（例如被擋，回傳了一個錯誤頁）。

1. 刪掉暫存資料夾再試一次：

   ```bash
   rm -rf .cache
   ```

   ```bash
   bash scripts/build_css.sh
   ```

2. 還是不行：換一台能直接連 github.com 的電腦 build。

### 出現 `Unsupported system` 或 `Unsupported processor`

你的系統不在支援清單（Linux / macOS / Windows 的 x64 或 arm64）。請換一台電腦 build，或用 WSL。

### 出現 `curl: (6) Could not resolve host` 或連線逾時

那台機器連不到 github.com。設定 proxy（見[第一次準備](#2-第一次準備只做一次)），或改在能上網的電腦 build。

### 伺服器上不能上網，會影響網站嗎？

不會。網站執行時完全不需要外網：所有檔案都在 `app/static/`。只有「build CSS」這個動作需要下載一次 Tailwind，而這件事不必在伺服器上做。

---

## 6. 進階：升級版本（很少需要）

版本號和檢查碼都寫在腳本最上面，改的時候這兩個值要一起換。

### 升級 Chart.js 或字型

1. 打開 `scripts/vendor_frontend.sh`，改最上面的版本號（`CHART_JS_VERSION` 或 `FONTSOURCE_VERSION`）。
2. 填入新版本的檢查碼。npm 上每個版本都有 `integrity` 值，例如查 Chart.js 4.6.1 的（版本號換成你要的；結果去掉 `sha512-` 前綴後，填進 `CHART_JS_SHA512`）：

   ```bash
   curl -s https://registry.npmjs.org/chart.js/4.6.1 | python3 -c "import json,sys; print(json.load(sys.stdin)['dist']['integrity'])"
   ```

3. 執行：

   ```bash
   bash scripts/vendor_frontend.sh
   ```

4. 打開 dashboard 和 admin，看圖表和字型是否正常，再 commit `app/static/vendor` 和 `app/static/fonts`。

### 升級 Tailwind

1. 打開 `scripts/build_css.sh`，改 `TAILWIND_VERSION`，並把 `sha256_for` 裡每一行的檢查碼換成新版本的。檢查碼在 GitHub 的 release 頁面有一個 `sha256sums.txt`，例如：
   `https://github.com/tailwindlabs/tailwindcss/releases/download/v3.4.17/sha256sums.txt`
2. 只建議在 **3.x** 之內升級。4.x 的設定方式完全不同，要改的不只是版本號。
3. 執行 `bash scripts/build_css.sh`，檢查頁面，commit `app/static/css/tailwind.css` 和腳本。

---

## 相關檔案一覽

| 路徑 | 是什麼 |
|---|---|
| `scripts/build_css.sh` | 產生 `tailwind.css`；`--check` 只檢查 |
| `scripts/vendor_frontend.sh` | 下載 Chart.js 和字型（升級版本時才用） |
| `frontend/tailwind.config.js` | Tailwind 設定：要掃描哪些模板、顏色、字型 |
| `frontend/tailwind.input.css` | build 的輸入檔（只有三行，不用改） |
| `app/static/` | 產生／下載好的檔案，**要 commit** |
| `.cache/` | 下載的 Tailwind 程式，**不 commit**（已在 `.gitignore`） |
| `app/core/static_assets.py` | 產生帶雜湊的網址（`static_url`），以及讓 gateway 提供 `/static` |
| `deploy/llm-gateway-example.nginx.conf` | `location /static/` 範例 |
